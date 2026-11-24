"""Core service logic: mask submission / correction and gap queries.

Design guarantees (see README for the full rationale):

* A submission runs inside one transaction. On any failure nothing is
  committed, so operators can still query the previous committed result.
* Re-submitting a band only reads *that scene's* source masks and only
  rewrites coverage rows inside *that scene's* grid extent.
* Queries never read per-scene masks: they read the committed ``coverage``
  table only, at most one row per rectangle cell.
* A coverage repair recomputes the authoritative scene-id sets from the
  intersecting source masks, rewrites only cells whose sets differ, and
  aborts if a submission flipped a band validity bit inside the rectangle
  while it ran. The write path never re-reads source masks.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import date as date_type
from typing import Iterable

from .db import connect  # re-exported so callers can pass it as conn_factory
from .models import (
    GAP_BOTH_MISSING,
    GAP_CATEGORY_LABELS,
    GAP_DIFFERENT_SCENES,
    GAP_NIR_ONLY,
    GAP_RED_ONLY,
    GapRun,
    MaskSubmission,
)


class GeometryConflictError(Exception):
    """A re-submitted scene uses a different origin or size."""


class RepairConflictError(Exception):
    """A submission changed band validity bits inside the repair rectangle.

    Raised when a mask submission committed after the repair started changed
    the validity bit of any band of any scene at any cell inside the repair
    rectangle -- even if a later submission changed the bit back. Changes
    outside the rectangle never raise this error.
    """


class CorruptSourceMaskError(Exception):
    """A stored source mask's length does not match its registered geometry."""


def _flatten(mask: list[list[int]]) -> bytes:
    return bytes(v for row in mask for v in row)


def _pack_cells(cells: Iterable[tuple[int, int]]) -> bytes:
    """Pack grid coordinates as little-endian int64 (x, y) pairs."""
    return b"".join(struct.pack("<qq", x, y) for x, y in cells)


def _unpack_cells(blob: bytes) -> Iterable[tuple[int, int]]:
    return struct.iter_unpack("<qq", blob)


def submit_mask(conn: sqlite3.Connection, sub: MaskSubmission) -> dict:
    """Insert/correct one band mask and recompute this scene's coverage.

    Only coverage cells inside the submitted scene's extent are touched, and
    only the submitted scene's contribution to those cells changes.
    """
    date_str = sub.date.isoformat()
    flat = _flatten(sub.mask)
    valid_cells = sum(flat)

    # 1. Register the scene, or verify that the fixed geometry still matches.
    row = conn.execute(
        "SELECT origin_x, origin_y, width, height FROM scenes "
        "WHERE date = ? AND scene_id = ?",
        (date_str, sub.scene_id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO scenes (date, scene_id, origin_x, origin_y, width, height) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                date_str,
                sub.scene_id,
                sub.origin_x,
                sub.origin_y,
                sub.width,
                sub.height,
            ),
        )
    elif (row["origin_x"], row["origin_y"], row["width"], row["height"]) != (
        sub.origin_x,
        sub.origin_y,
        sub.width,
        sub.height,
    ):
        raise GeometryConflictError(
            f"scene {sub.scene_id} on {date_str} is already registered with origin "
            f"({row['origin_x']}, {row['origin_y']}) and size "
            f"{row['width']}x{row['height']}; a scene's grid position and size are fixed"
        )

    # 2. Read this scene's own current mask for the *other* band. No masks of
    #    any other scene are read on the correction path.
    other_band = "nir" if sub.band == "red" else "red"
    other = conn.execute(
        "SELECT mask FROM band_masks WHERE date = ? AND scene_id = ? AND band = ?",
        (date_str, sub.scene_id, other_band),
    ).fetchone()
    other_by_index = (
        {i for i, v in enumerate(other["mask"]) if v} if other is not None else set()
    )
    new_by_index = {i for i, v in enumerate(flat) if v}

    # Cells whose validity bit for this band flips. Recorded in the same
    # transaction so a repair that started earlier can detect the change.
    old_band = conn.execute(
        "SELECT mask FROM band_masks WHERE date = ? AND scene_id = ? AND band = ?",
        (date_str, sub.scene_id, sub.band),
    ).fetchone()
    old_by_index = (
        {i for i, v in enumerate(old_band["mask"]) if v}
        if old_band is not None
        else set()
    )
    flipped = old_by_index ^ new_by_index
    if flipped:
        flipped_cells = [
            (sub.origin_x + i % sub.width, sub.origin_y + i // sub.width)
            for i in sorted(flipped)
        ]
        conn.execute(
            "INSERT INTO mask_changes "
            "(date, scene_id, band, min_x, max_x, min_y, max_y, changed_cells) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                date_str,
                sub.scene_id,
                sub.band,
                min(x for x, _ in flipped_cells),
                max(x for x, _ in flipped_cells),
                min(y for _, y in flipped_cells),
                max(y for _, y in flipped_cells),
                _pack_cells(flipped_cells),
            ),
        )

    # 3. Upsert the source mask for the submitted band.
    conn.execute(
        "INSERT INTO band_masks (date, scene_id, band, mask) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (date, scene_id, band) DO UPDATE SET "
        "mask = excluded.mask, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        (date_str, sub.scene_id, sub.band, flat),
    )

    # 4. Recompute coverage for every cell in this scene's extent. Existing
    #    coverage rows are loaded in one indexed range scan; other scenes'
    #    contributions are preserved in their scene-id lists.
    ox, oy, w, h = sub.origin_x, sub.origin_y, sub.width, sub.height
    existing: dict[tuple[int, int], sqlite3.Row] = {
        (r["x"], r["y"]): r
        for r in conn.execute(
            "SELECT x, y, red_scenes, nir_scenes FROM coverage "
            "WHERE date = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
            (date_str, ox, ox + w - 1, oy, oy + h - 1),
        )
    }

    touched = 0
    for iy in range(h):
        for ix in range(w):
            idx = iy * w + ix
            x, y = ox + ix, oy + iy
            row = existing.get((x, y))
            red_scenes = (
                set(json.loads(row["red_scenes"])) if row is not None else set()
            )
            nir_scenes = (
                set(json.loads(row["nir_scenes"])) if row is not None else set()
            )

            # Drop this scene's previous contribution, then apply the new one.
            red_scenes.discard(sub.scene_id)
            nir_scenes.discard(sub.scene_id)
            if sub.band == "red":
                if idx in new_by_index:
                    red_scenes.add(sub.scene_id)
                if idx in other_by_index:
                    nir_scenes.add(sub.scene_id)
            else:
                if idx in new_by_index:
                    nir_scenes.add(sub.scene_id)
                if idx in other_by_index:
                    red_scenes.add(sub.scene_id)

            if not red_scenes and not nir_scenes:
                # No scene contributes anything here anymore.
                conn.execute(
                    "DELETE FROM coverage WHERE date = ? AND x = ? AND y = ?",
                    (date_str, x, y),
                )
                touched += 1
                continue

            conn.execute(
                "INSERT INTO coverage (date, x, y, red_scenes, nir_scenes) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (date, x, y) DO UPDATE SET "
                "red_scenes = excluded.red_scenes, "
                "nir_scenes = excluded.nir_scenes, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (
                    date_str,
                    x,
                    y,
                    json.dumps(sorted(red_scenes), separators=(",", ":")),
                    json.dumps(sorted(nir_scenes), separators=(",", ":")),
                ),
            )
            touched += 1

    return {
        "date": sub.date,
        "scene_id": sub.scene_id,
        "band": sub.band,
        "origin_x": ox,
        "origin_y": oy,
        "width": w,
        "height": h,
        "valid_cells": valid_cells,
        "coverage_cells_updated": touched,
    }


def _classify(red: set[str], nir: set[str]) -> str | None:
    """Return the gap category, or None when the cell is complete."""
    if red & nir:
        return None
    if red and nir:
        return GAP_DIFFERENT_SCENES
    if red:
        return GAP_RED_ONLY
    if nir:
        return GAP_NIR_ONLY
    return GAP_BOTH_MISSING


def _check_repair_conflicts(
    conn: sqlite3.Connection,
    date_str: str,
    start_seq: int,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
) -> None:
    """Reject a repair if a later submission flipped a bit inside the rectangle.

    Rows whose changed-cell bounding box misses the rectangle are skipped
    cheaply. For the rest every packed cell is examined. A bit that flipped
    and then flipped back still counts: both submissions leave a row, and
    either one intersecting the rectangle raises the conflict.
    """
    lo_x, hi_x = origin_x, origin_x + width - 1
    lo_y, hi_y = origin_y, origin_y + height - 1
    rows = conn.execute(
        "SELECT band, min_x, max_x, min_y, max_y, changed_cells "
        "FROM mask_changes WHERE date = ? AND seq > ? "
        "AND max_x >= ? AND min_x <= ? AND max_y >= ? AND min_y <= ?",
        (date_str, start_seq, lo_x, hi_x, lo_y, hi_y),
    ).fetchall()
    for row in rows:
        for x, y in _unpack_cells(row["changed_cells"]):
            if lo_x <= x <= hi_x and lo_y <= y <= hi_y:
                raise RepairConflictError(
                    "band validity changed inside the repair rectangle after "
                    f"repair started (band {row['band']} at cell ({x}, {y})); "
                    "retry the repair"
                )


def repair_coverage(
    conn_factory,
    request,
    fail_after_scan: bool = False,
    fail_during_write: bool = False,
    after_read_hook=None,
) -> dict:
    """Rebuild committed coverage in a rectangle from the source band masks.

    Work is split into two phases so submissions remain possible while the
    repair recomputes:

    1. **Read phase** (deferred read transaction; never blocks writers): take
       a snapshot delimiter ``start_seq``, read the source masks of every
       scene intersecting the rectangle, and derive the authoritative
       scene-id set for each band / cell.
    2. **Write phase** (``BEGIN IMMEDIATE``): first reject the repair with
       :class:`RepairConflictError` if any submission recorded after
       ``start_seq`` flipped a band validity bit inside the rectangle (flip
       and flip back still counts); changes outside the rectangle are
       ignored. Then read the committed coverage rows in the rectangle and
       rewrite only cells whose scene-id sets differ, even when the gap
       category is unchanged. The write path never reads ``band_masks``.

    Any exception rolls the write transaction back, so a conflict or write
    failure leaves the previous committed coverage intact. A conflict is also
    raised if a submission lands between the two phases and touches the
    rectangle.
    """
    date_str = request.date.isoformat()
    ox, oy, w, h = request.origin_x, request.origin_y, request.width, request.height
    lo_x, hi_x = ox, ox + w - 1
    lo_y, hi_y = oy, oy + h - 1

    conn = conn_factory()
    # Drive BEGIN / COMMIT explicitly so the read snapshot and the write phase
    # have exact boundaries and submissions are never blocked by a reader.
    conn.isolation_level = None
    try:
        # ---- Phase 1: snapshot read; no locks that block submissions. ----
        conn.execute("BEGIN")
        try:
            start_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM mask_changes"
            ).fetchone()[0]

            # Source masks of every scene whose extent meets the rectangle.
            # Only these masks can contribute to the rectangle.
            scene_rows = conn.execute(
                "SELECT scene_id, origin_x, origin_y, width, height FROM scenes "
                "WHERE date = ? AND origin_x <= ? AND origin_x + width - 1 >= ? "
                "AND origin_y <= ? AND origin_y + height - 1 >= ?",
                (date_str, hi_x, lo_x, hi_y, lo_y),
            ).fetchall()
            geometries = {
                r["scene_id"]: (r["origin_x"], r["origin_y"], r["width"], r["height"])
                for r in scene_rows
            }
            band_masks: dict[str, dict[str, bytes]] = {}
            if geometries:
                ids = sorted(geometries)
                placeholders = ",".join("?" * len(ids))
                for r in conn.execute(
                    "SELECT scene_id, band, mask FROM band_masks "
                    f"WHERE date = ? AND scene_id IN ({placeholders})",
                    (date_str, *ids),
                ):
                    band_masks.setdefault(r["scene_id"], {})[r["band"]] = r["mask"]

            # Authoritative scene-id sets per cell within the rectangle.
            expected: dict[tuple[int, int], tuple[set[str], set[str]]] = {}
            for scene_id, (sx, sy, sw, sh) in geometries.items():
                x0, x1 = max(sx, lo_x), min(sx + sw - 1, hi_x)
                y0, y1 = max(sy, lo_y), min(sy + sh - 1, hi_y)
                masks = band_masks.get(scene_id, {})
                red_mask = masks.get("red")
                nir_mask = masks.get("nir")
                if red_mask is None and nir_mask is None:
                    continue
                for mask_name, mask in (("red", red_mask), ("nir", nir_mask)):
                    if mask is not None and len(mask) != sw * sh:
                        raise CorruptSourceMaskError(
                            f"source {mask_name} mask of scene {scene_id} has "
                            f"{len(mask)} bytes but its registered geometry is "
                            f"{sw}x{sh}; repair aborted without changes"
                        )
                for y in range(y0, y1 + 1):
                    local_row = (y - sy) * sw
                    for x in range(x0, x1 + 1):
                        local = local_row + (x - sx)
                        red, nir = expected.setdefault((x, y), (set(), set()))
                        if red_mask is not None and red_mask[local]:
                            red.add(scene_id)
                        if nir_mask is not None and nir_mask[local]:
                            nir.add(scene_id)
        finally:
            # End the read snapshot before taking the write lock.
            conn.rollback()

        if after_read_hook is not None:
            after_read_hook()

        if fail_after_scan:
            raise RuntimeError("injected failure after scan")

        # ---- Phase 2: conflict check and write, atomically. ----
        changes: list[dict] = []
        conn.execute("BEGIN IMMEDIATE")
        try:
            _check_repair_conflicts(conn, date_str, start_seq, ox, oy, w, h)

            existing = {
                (r["x"], r["y"]): (
                    set(json.loads(r["red_scenes"])),
                    set(json.loads(r["nir_scenes"])),
                )
                for r in conn.execute(
                    "SELECT x, y, red_scenes, nir_scenes FROM coverage "
                    "WHERE date = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                    (date_str, lo_x, hi_x, lo_y, hi_y),
                )
            }

            # A missing coverage row means both sets are empty. Cells inside
            # the rectangle that no scene covers also have to be corrected if
            # committed coverage still exists there.
            for x, y in set(expected) | set(existing):
                exp_red, exp_nir = expected.get((x, y), (set(), set()))
                cur_red, cur_nir = existing.get((x, y), (set(), set()))
                if cur_red == exp_red and cur_nir == exp_nir:
                    continue
                changes.append(
                    {
                        "x": x,
                        "y": y,
                        "red_added": sorted(exp_red - cur_red),
                        "red_removed": sorted(cur_red - exp_red),
                        "nir_added": sorted(exp_nir - cur_nir),
                        "nir_removed": sorted(cur_nir - exp_nir),
                        "red": exp_red,
                        "nir": exp_nir,
                    }
                )
            changes.sort(key=lambda c: (c["y"], c["x"]))

            if fail_during_write:
                # Write part of the batch, then fail, to prove the rollback
                # discards already-written repair rows too.
                if changes:
                    first = changes[0]
                    conn.execute(
                        "INSERT INTO coverage (date, x, y, red_scenes, nir_scenes) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (date, x, y) DO UPDATE SET "
                        "red_scenes = excluded.red_scenes, "
                        "nir_scenes = excluded.nir_scenes",
                        (
                            date_str,
                            first["x"],
                            first["y"],
                            json.dumps(sorted(first["red"]), separators=(",", ":")),
                            json.dumps(sorted(first["nir"]), separators=(",", ":")),
                        ),
                    )
                raise RuntimeError("injected failure during write")

            # Write. Source masks are not read again on this path.
            for change in changes:
                x, y = change["x"], change["y"]
                red, nir = change["red"], change["nir"]
                if not red and not nir:
                    conn.execute(
                        "DELETE FROM coverage WHERE date = ? AND x = ? AND y = ?",
                        (date_str, x, y),
                    )
                    continue
                conn.execute(
                    "INSERT INTO coverage (date, x, y, red_scenes, nir_scenes) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (date, x, y) DO UPDATE SET "
                    "red_scenes = excluded.red_scenes, "
                    "nir_scenes = excluded.nir_scenes, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                    (
                        date_str,
                        x,
                        y,
                        json.dumps(sorted(red), separators=(",", ":")),
                        json.dumps(sorted(nir), separators=(",", ":")),
                    ),
                )

            # Post-write verification: re-read the rectangle's coverage rows
            # and confirm they now equal the expected sets exactly. This
            # checks the actual rows written, not the in-memory changes; a
            # mismatch rolls the whole repair back.
            written = {
                (r["x"], r["y"]): (
                    set(json.loads(r["red_scenes"])),
                    set(json.loads(r["nir_scenes"])),
                )
                for r in conn.execute(
                    "SELECT x, y, red_scenes, nir_scenes FROM coverage "
                    "WHERE date = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                    (date_str, lo_x, hi_x, lo_y, hi_y),
                )
            }
            for x, y in set(expected) | set(written):
                exp_red, exp_nir = expected.get((x, y), (set(), set()))
                got_red, got_nir = written.get((x, y), (set(), set()))
                if got_red != exp_red or got_nir != exp_nir:
                    raise RuntimeError(
                        "repair verification failed at cell "
                        f"({x}, {y}): committed coverage does not match source masks"
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return {
        "date": request.date,
        "origin_x": ox,
        "origin_y": oy,
        "width": w,
        "height": h,
        "cells_checked": w * h,
        "cells_changed": len(changes),
        "changes": [
            {k: v for k, v in c.items() if k not in ("red", "nir")}
            for c in changes
        ],
    }


def query_gaps(
    conn: sqlite3.Connection,
    query_date: date_type,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
) -> dict:
    """Find gap cells in a rectangle and merge same-category runs per row.

    A single indexed range read returns at most ``width * height`` coverage
    rows; masks are never read here.
    """
    date_str = query_date.isoformat()
    rows = conn.execute(
        "SELECT x, y, red_scenes, nir_scenes FROM coverage "
        "WHERE date = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
        (date_str, origin_x, origin_x + width - 1, origin_y, origin_y + height - 1),
    ).fetchall()
    by_cell: dict[tuple[int, int], tuple[set[str], set[str]]] = {
        (r["x"], r["y"]): (
            set(json.loads(r["red_scenes"])),
            set(json.loads(r["nir_scenes"])),
        )
        for r in rows
    }

    gaps: list[GapRun] = []
    category_counts = {
        GAP_RED_ONLY: 0,
        GAP_NIR_ONLY: 0,
        GAP_DIFFERENT_SCENES: 0,
        GAP_BOTH_MISSING: 0,
    }
    complete = 0
    gap_total = 0

    for iy in range(height):
        y = origin_y + iy
        run_category: str | None = None
        run_start = origin_x

        def _flush(end_x: int) -> None:
            nonlocal run_category, run_start
            if run_category is not None:
                gaps.append(
                    GapRun(
                        y=y,
                        x_start=run_start,
                        x_end=end_x,
                        category=run_category,  # type: ignore[arg-type]
                        category_label=GAP_CATEGORY_LABELS[run_category],
                        length=end_x - run_start + 1,
                    )
                )
            run_category = None

        for ix in range(width):
            x = origin_x + ix
            red, nir = by_cell.get((x, y), (set(), set()))
            category = _classify(red, nir)
            if category is None:
                _flush(x - 1)
                complete += 1
                continue
            gap_total += 1
            category_counts[category] += 1
            if run_category is None:
                run_category = category
                run_start = x
            elif category != run_category:
                _flush(x - 1)
                run_category = category
                run_start = x
        _flush(origin_x + width - 1)

    return {
        "date": query_date,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "width": width,
        "height": height,
        "total_cells": width * height,
        "complete_cells": complete,
        "gap_cells": gap_total,
        "category_counts": category_counts,
        "gaps": gaps,
    }


def get_scene(
    conn: sqlite3.Connection, query_date: date_type, scene_id: str
) -> dict | None:
    date_str = query_date.isoformat()
    scene = conn.execute(
        "SELECT origin_x, origin_y, width, height FROM scenes "
        "WHERE date = ? AND scene_id = ?",
        (date_str, scene_id),
    ).fetchone()
    if scene is None:
        return None
    bands = [
        {
            "band": r["band"],
            "valid_cells": sum(r["mask"]),
            "updated_at": r["updated_at"],
        }
        for r in conn.execute(
            "SELECT band, mask, updated_at FROM band_masks "
            "WHERE date = ? AND scene_id = ? ORDER BY band",
            (date_str, scene_id),
        )
    ]
    return {
        "date": query_date,
        "scene_id": scene_id,
        "origin_x": scene["origin_x"],
        "origin_y": scene["origin_y"],
        "width": scene["width"],
        "height": scene["height"],
        "bands": bands,
    }
