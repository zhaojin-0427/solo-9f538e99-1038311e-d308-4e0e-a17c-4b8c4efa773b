"""FastAPI application: band gap-checking service.

Interactive documentation is served at ``/docs`` (Swagger UI). On startup the
SQLite schema is created if missing.
"""

from contextlib import asynccontextmanager
from datetime import date as date_type
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, status

from .config import MAX_SIDE
from .db import connect, init_db, session
from .models import (
    GapQueryResponse,
    MaskSubmitResponse,
    MaskSubmission,
    RepairRequest,
    RepairResponse,
    SceneInfo,
)
from .services import (
    CorruptSourceMaskError,
    GeometryConflictError,
    RepairConflictError,
    get_scene,
    query_gaps,
    repair_coverage,
    submit_mask,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="波段查缺 API",
    description=(
        "红光 / 近红外波段有效性掩膜提交与查缺服务。\n\n"
        "植被指数要求**同一景**的红光与近红外在同一格都有效；"
        "掩膜与已提交覆盖保存在 SQLite 中，查询只读取已提交覆盖。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/masks",
    response_model=MaskSubmitResponse,
    status_code=status.HTTP_200_OK,
    tags=["掩膜提交"],
    summary="提交 / 更正某景某波段的 0/1 有效掩膜",
    description=(
        "按 UTC 日期、景号、网格原点和波段提交掩膜。同景的网格位置与尺寸"
        "固定，红光与近红外两个波段可分别提交、分别更正；更正只回读本景的"
        "源掩膜，仅重算本景范围内的覆盖。整个操作在一个事务内生效，失败时"
        "保留原结果。"
    ),
)
def upsert_mask(submission: MaskSubmission) -> MaskSubmitResponse:
    try:
        with session() as conn:
            result = submit_mask(conn, submission)
    except GeometryConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return MaskSubmitResponse(**result)  # type: ignore[arg-type]


@app.post(
    "/repairs",
    response_model=RepairResponse,
    status_code=status.HTTP_200_OK,
    tags=["覆盖修复"],
    summary="按源掩膜修复矩形内的已提交覆盖",
    description=(
        "输入 UTC 日期与矩形（宽、高各 <= 256 格）。以与该矩形相交的景的"
        "源掩膜为准，逐格比对各波段的景号集合；只改写集合有差异的格，即使"
        "缺口类别不变也修正。返回每个改动格的坐标及各波段补入、移除的景号。\n\n"
        "重算期间仍可补报。若开始后曾有补报改变矩形内任一景、任一波段、任一"
        "格的有效位（即使随后改回），返回 409 冲突；矩形外的改动不影响修复。"
        "写回时不再读取源掩膜；冲突或写入失败都会回滚，不留下本次修复的任何"
        "改动。"
    ),
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "修复开始后区内波段有效位被补报改变（含改回）。"
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "源掩膜与已登记几何不符，修复中止且未写入任何改动。"
        },
    },
)
def repairs(request: RepairRequest) -> RepairResponse:
    try:
        result = repair_coverage(connect, request)
    except RepairConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except CorruptSourceMaskError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return RepairResponse(**result)  # type: ignore[arg-type]


@app.get(
    "/gaps",
    response_model=GapQueryResponse,
    tags=["查缺"],
    summary="按 UTC 日期与网格矩形查询连续缺口",
    description=(
        "任一景的红光与近红外在某格都有效，该格即视为齐全；其余格子按行"
        "返回同类连续缺口，标明：仅红光有效、仅近红外有效、两波段分属不同"
        "景、或两波段都缺。查询读取同一已提交状态，不读取单景掩膜，读取的"
        "覆盖记录最多与矩形内像元数相同。"
    ),
)
def gaps(
    query_date: Annotated[date_type, Query(alias="date", description="UTC 日期 YYYY-MM-DD")],
    origin_x: Annotated[int, Query(description="矩形左上角网格 x 坐标")],
    origin_y: Annotated[int, Query(description="矩形左上角网格 y 坐标")],
    width: Annotated[
        int, Query(ge=1, le=MAX_SIDE, description=f"矩形宽（格），<= {MAX_SIDE}")
    ],
    height: Annotated[
        int, Query(ge=1, le=MAX_SIDE, description=f"矩形高（格），<= {MAX_SIDE}")
    ],
) -> GapQueryResponse:
    # A read-only connection sees one committed snapshot for the whole query.
    conn = connect()
    try:
        result = query_gaps(conn, query_date, origin_x, origin_y, width, height)
    finally:
        conn.close()
    return GapQueryResponse(**result)  # type: ignore[arg-type]


@app.get(
    "/scenes/{query_date}/{scene_id}",
    response_model=SceneInfo,
    tags=["掩膜查询"],
    summary="回读某景的固定几何与已提交波段掩膜信息",
)
def read_scene(query_date: date_type, scene_id: str) -> SceneInfo:
    conn = connect()
    try:
        result = get_scene(conn, query_date, scene_id)
    finally:
        conn.close()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scene {scene_id} not found on {query_date.isoformat()}",
        )
    return SceneInfo(**result)  # type: ignore[arg-type]
