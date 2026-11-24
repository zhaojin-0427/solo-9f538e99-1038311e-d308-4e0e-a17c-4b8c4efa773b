"""End-to-end tests for the mask submission and gap query API."""

from __future__ import annotations


def _post(client, **overrides):
    payload = {
        "date": "2026-11-23",
        "scene_id": "S1",
        "origin_x": 0,
        "origin_y": 0,
        "band": "red",
        "mask": [
            [1, 1, 0, 0],
            [1, 0, 0, 0],
        ],
    }
    payload.update(overrides)
    return client.post("/masks", json=payload)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_submit_red_then_query_classifies_red_only_and_both_missing(client):
    r = _post(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid_cells"] == 3
    assert body["coverage_cells_updated"] == 8

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 4, "height": 2},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["gap_cells"] == 8
    assert data["category_counts"]["red_only"] == 3
    assert data["category_counts"]["both_missing"] == 5

    # Row 0: red-only run x=0..1, then both-missing run x=2..3
    gaps = data["gaps"]
    assert gaps[0]["y"] == 0
    assert gaps[0]["category"] == "red_only"
    assert (gaps[0]["x_start"], gaps[0]["x_end"], gaps[0]["length"]) == (0, 1, 2)
    assert gaps[1]["category"] == "both_missing"
    assert (gaps[1]["x_start"], gaps[1]["x_end"]) == (2, 3)
    # Row 1: red-only single cell at x=0, then both-missing x=1..3
    assert gaps[2]["y"] == 1 and gaps[2]["category"] == "red_only"
    assert gaps[3]["category"] == "both_missing"
    assert (gaps[3]["x_start"], gaps[3]["x_end"], gaps[3]["length"]) == (1, 3, 3)


def test_same_scene_red_and_nir_marks_complete_cells(client):
    assert _post(client, band="red", mask=[[1, 1], [0, 1]]).status_code == 200
    assert _post(client, band="nir", mask=[[1, 0], [0, 1]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 2},
    )
    data = r.json()
    # Complete where both bands valid in S1: (0,0) and (1,1).
    assert data["complete_cells"] == 2
    assert data["gap_cells"] == 2
    counts = data["category_counts"]
    assert counts["red_only"] == 1   # cell (1,0): red only
    assert counts["nir_only"] == 0
    assert counts["both_missing"] == 1  # cell (0,1)
    assert counts["different_scenes"] == 0


def test_different_scenes_supply_bands_at_same_cell(client):
    # S1 covers origin (0,0); S2 is shifted and overlaps at cell (1,1).
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1], [1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=1,
                 band="nir", mask=[[1]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 2},
    )
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["category_counts"]["different_scenes"] == 1
    assert data["category_counts"]["red_only"] == 3

    diff = [g for g in data["gaps"] if g["category"] == "different_scenes"]
    assert len(diff) == 1
    assert (diff[0]["y"], diff[0]["x_start"], diff[0]["x_end"]) == (1, 1, 1)
    assert "不同景" in diff[0]["category_label"]


def test_complete_when_any_scene_supplies_both_bands(client):
    # S1: red only everywhere; S2 supplies both bands at (0,0) -> complete.
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", band="red",
                 mask=[[1, 0]]).status_code == 200
    assert _post(client, scene_id="S2", band="nir",
                 mask=[[1, 0]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    assert data["complete_cells"] == 1
    assert data["category_counts"]["red_only"] == 1


def test_correction_recomputes_only_this_scene(client):
    # Two overlapping scenes, then correct S1's red mask.
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=0,
                 band="nir", mask=[[1]]).status_code == 200
    # Cell (1,0): S1 red + S2 nir -> different_scenes before correction.

    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 0]]).status_code == 200

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    # (0,0): red only; (1,0): S1 red withdrawn, S2 nir remains -> nir_only.
    assert data["category_counts"]["red_only"] == 1
    assert data["category_counts"]["nir_only"] == 1
    assert data["category_counts"]["different_scenes"] == 0

    # The source mask read-back reflects the correction.
    info = client.get("/scenes/2026-11-23/S1").json()
    assert info["width"] == 2 and info["height"] == 1
    red_band = next(b for b in info["bands"] if b["band"] == "red")
    assert red_band["valid_cells"] == 1


def test_geometry_conflict_is_rejected(client):
    assert _post(client).status_code == 200
    r = _post(client, origin_x=5, origin_y=5)
    assert r.status_code == 409
    assert "fixed" in r.json()["detail"]

    # Size mismatch is also rejected.
    r = _post(client, mask=[[1, 1, 1]])
    assert r.status_code == 409


def test_failed_submission_keeps_previous_result(client):
    assert _post(client, band="red", mask=[[1, 1]]).status_code == 200

    # Oversized mask (>256) fails validation and must not change anything.
    big = [[1] * 257]
    r = _post(client, band="nir", mask=big)
    assert r.status_code == 422

    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 2, "height": 1},
    )
    data = r.json()
    assert data["complete_cells"] == 0
    assert data["category_counts"]["red_only"] == 2


def test_invalid_mask_shapes_rejected(client):
    # Empty mask.
    r = _post(client, mask=[])
    assert r.status_code == 422
    # Ragged rows.
    r = _post(client, mask=[[1, 0], [1]])
    assert r.status_code == 422


def test_query_rectangle_limits_enforced(client):
    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 257, "height": 1},
    )
    assert r.status_code == 422


def test_rectangle_partially_outside_coverage_treated_as_both_missing(client):
    assert _post(client, mask=[[1]]).status_code == 200
    r = client.get(
        "/gaps",
        params={"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
                "width": 3, "height": 1},
    )
    data = r.json()
    assert data["total_cells"] == 3
    assert data["category_counts"]["red_only"] == 1
    assert data["category_counts"]["both_missing"] == 2


def test_dates_are_isolated(client):
    assert _post(client, date="2026-11-23", mask=[[1]]).status_code == 200
    r = client.get(
        "/gaps",
        params={"date": "2026-11-24", "origin_x": 0, "origin_y": 0,
                "width": 1, "height": 1},
    )
    data = r.json()
    assert data["gap_cells"] == 1
    assert data["category_counts"]["both_missing"] == 1


def test_scene_endpoint_404(client):
    r = client.get("/scenes/2026-11-23/nope")
    assert r.status_code == 404


def test_max_256_mask_accepted(client):
    mask = [[0] * 256 for _ in range(256)]
    mask[0][0] = 1
    r = _post(client, mask=mask)
    assert r.status_code == 200, r.text
    assert r.json()["valid_cells"] == 1
    assert r.json()["coverage_cells_updated"] == 256 * 256


def test_coverage_rows_never_exceed_rectangle_cells(client, monkeypatch):
    """Storage invariant backing the query read bound: one coverage row per
    cell at most, so a rectangle scan returns at most width*height rows."""
    from app import config
    import sqlite3

    # Two overlapping scenes contribute to the same cells; they must share
    # rows rather than create one row per (cell, scene).
    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1], [1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=1,
                 band="nir", mask=[[1]]).status_code == 200

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM coverage WHERE date = '2026-11-23'"
        ).fetchone()[0]
        in_rect = conn.execute(
            "SELECT COUNT(*) FROM coverage "
            "WHERE date = '2026-11-23' AND x BETWEEN 0 AND 1 AND y BETWEEN 0 AND 1"
        ).fetchone()[0]
    finally:
        conn.close()

    rectangle_cells = 2 * 2
    assert total <= rectangle_cells
    assert in_rect <= rectangle_cells


# ---------------------------------------------------------------------------
# Coverage repair (/repairs)
# ---------------------------------------------------------------------------


def _repair(client, **overrides):
    payload = {
        "date": "2026-11-23",
        "origin_x": 0,
        "origin_y": 0,
        "width": 4,
        "height": 2,
    }
    payload.update(overrides)
    return client.post("/repairs", json=payload)


def _gaps(client, **overrides):
    params = {"date": "2026-11-23", "origin_x": 0, "origin_y": 0,
              "width": 4, "height": 2}
    params.update(overrides)
    return client.get("/gaps", params=params).json()


def _coverage(client, date="2026-11-23", x=0, y=0):
    """Read one committed coverage row straight from the database."""
    import sqlite3
    from app import config

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT red_scenes, nir_scenes FROM coverage "
            "WHERE date = ? AND x = ? AND y = ?",
            (date, x, y),
        ).fetchone()
    finally:
        conn.close()


def test_repair_is_noop_when_coverage_matches_source_masks(client):
    assert _post(client, band="red", mask=[[1, 1], [0, 1]]).status_code == 200
    assert _post(client, band="nir", mask=[[1, 0], [0, 1]]).status_code == 200

    r = _repair(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["cells_checked"] == 8
    assert data["cells_changed"] == 0
    assert data["changes"] == []


def test_repair_restores_coverage_from_masks_after_tampering(client):
    # One scene supplying both bands; then corrupt the committed coverage by
    # hand to simulate a stale coverage table after restoring masked sources.
    import sqlite3
    from app import config

    assert _post(client, band="red", mask=[[1, 1], [0, 1]]).status_code == 200
    assert _post(client, band="nir", mask=[[1, 1], [0, 1]]).status_code == 200

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        conn.execute(
            "UPDATE coverage SET red_scenes = '[]', nir_scenes = '[]' "
            "WHERE date = '2026-11-23' AND x = 0 AND y = 0"
        )
        conn.execute(
            "UPDATE coverage SET red_scenes = '[\"S9\"]', nir_scenes = '[\"S9\"]' "
            "WHERE date = '2026-11-23' AND x = 1 AND y = 0"
        )
        conn.commit()
    finally:
        conn.close()

    r = _repair(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["cells_changed"] == 2
    by_cell = {(c["x"], c["y"]): c for c in data["changes"]}
    assert by_cell[(0, 0)]["red_added"] == ["S1"]
    assert by_cell[(0, 0)]["nir_added"] == ["S1"]
    assert (1, 0) in by_cell
    assert by_cell[(1, 0)]["red_removed"] == ["S9"]
    assert by_cell[(1, 0)]["nir_removed"] == ["S9"]

    # After repair the view is complete again and the fabricated S9 is gone.
    data = _gaps(client)
    assert data["complete_cells"] == 3
    row = _coverage(client)
    import json as _json
    assert _json.loads(row["red_scenes"]) == ["S1"]
    assert _json.loads(row["nir_scenes"]) == ["S1"]


def test_repair_corrects_scene_sets_even_when_gap_category_unchanged(client):
    """Cell stays `red_only`, but the scene id behind the red sample changes.

    S1 and S2 both mark red at the cell; NIR only comes from a third scene, so
    the committed red set should contain both ids. If a stale coverage row
    lists only S1, the category is still `red_only`, yet the sets differ and
    the repair must add S2.
    """
    import sqlite3
    from app import config

    assert _post(client, scene_id="S1", band="red", mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S2", band="red", mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S3", band="nir", mask=[[1]]).status_code == 200
    # (0,0): S1/S2 red + S3 nir -> complete (same scene S1 may supply both
    # after later submissions; here use a second cell only red exists).

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        # Stale row at (1,0): only S1 listed for red, no nir -> red_only.
        conn.execute(
            "UPDATE coverage SET red_scenes = '[\"S1\"]', nir_scenes = '[]' "
            "WHERE date = '2026-11-23' AND x = 1 AND y = 0"
        )
        conn.commit()
    finally:
        conn.close()

    before = _gaps(client, width=2, height=1)
    assert before["category_counts"]["red_only"] == 1

    r = _repair(client, width=2, height=1)
    assert r.status_code == 200, r.text
    data = r.json()
    cell = next(c for c in data["changes"] if (c["x"], c["y"]) == (1, 0))
    assert cell["red_added"] == ["S2"]
    assert cell["red_removed"] == []
    # Category unchanged before/after, yet the cell was repaired.
    after = _gaps(client, width=2, height=1)
    assert after["category_counts"]["red_only"] == 1
    row = _coverage(client, x=1, y=0)
    import json as _json
    assert _json.loads(row["red_scenes"]) == ["S1", "S2"]


def test_repair_rectangle_bounded_and_validated(client):
    r = _repair(client, width=257)
    assert r.status_code == 422
    r = _repair(client, height=0)
    assert r.status_code == 422


def test_repair_conflict_when_submission_changes_bit_inside_rectangle(client):
    from app import services
    from app.models import RepairRequest

    assert _post(client, band="red", mask=[[1, 0]]).status_code == 200
    assert _post(client, band="nir", mask=[[1, 0]]).status_code == 200

    # Simulate a submission landing while the repair recomputes (between its
    # snapshot read and its write phase).
    def concurrent_submission():
        r = _post(client, band="red", mask=[[1, 1]])
        assert r.status_code == 200, r.text

    req = RepairRequest(date="2026-11-23", origin_x=0, origin_y=0,
                        width=2, height=1)
    import pytest
    with pytest.raises(services.RepairConflictError):
        services.repair_coverage(
            services.connect, req, after_read_hook=concurrent_submission
        )

    # The concurrent submission itself is kept; the repair left no trace.
    data = _gaps(client, width=2, height=1)
    assert data["complete_cells"] == 1
    assert data["category_counts"]["red_only"] == 1


def test_repair_conflict_even_if_bit_changed_and_changed_back(client):
    from app import services
    from app.models import RepairRequest
    import pytest

    assert _post(client, band="red", mask=[[1, 0]]).status_code == 200

    def flip_and_flip_back():
        assert _post(client, band="red", mask=[[0, 0]]).status_code == 200
        assert _post(client, band="red", mask=[[1, 0]]).status_code == 200

    req = RepairRequest(date="2026-11-23", origin_x=0, origin_y=0,
                        width=2, height=1)
    with pytest.raises(services.RepairConflictError):
        services.repair_coverage(
            services.connect, req, after_read_hook=flip_and_flip_back
        )


def test_repair_ignores_changes_outside_rectangle(client):
    from app import services
    from app.models import RepairRequest

    assert _post(client, band="red", mask=[[0, 1]]).status_code == 200
    assert _post(client, band="nir", mask=[[0, 1]]).status_code == 200

    def change_outside():
        # Flip the bit at x=1, which is outside the repaired rectangle 0..0.
        assert _post(client, band="red", mask=[[0, 0]]).status_code == 200

    req = RepairRequest(date="2026-11-23", origin_x=0, origin_y=0,
                        width=1, height=1)
    result = services.repair_coverage(
        services.connect, req, after_read_hook=change_outside
    )
    assert result["cells_checked"] == 1


def test_repair_write_failure_rolls_back_everything(client):
    from app import services
    from app.models import RepairRequest
    import pytest

    assert _post(client, band="red", mask=[[1, 0]]).status_code == 200
    assert _post(client, band="nir", mask=[[1, 0]]).status_code == 200
    # Tamper so the repair has real changes to write before it fails.
    import sqlite3
    from app import config

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        conn.execute(
            "UPDATE coverage SET red_scenes = '[]', nir_scenes = '[]' "
            "WHERE date = '2026-11-23' AND x = 0 AND y = 0"
        )
        conn.commit()
    finally:
        conn.close()
    before = _gaps(client, width=2, height=1)

    req = RepairRequest(date="2026-11-23", origin_x=0, origin_y=0,
                        width=2, height=1)
    with pytest.raises(RuntimeError):
        services.repair_coverage(services.connect, req, fail_during_write=True)

    after = _gaps(client, width=2, height=1)
    assert after == before
    # The tampered (but pre-repair) coverage row is still exactly as it was.
    row = _coverage(client, x=0, y=0)
    import json as _json
    assert _json.loads(row["red_scenes"]) == []
    assert _json.loads(row["nir_scenes"]) == []


def test_repair_write_phase_does_not_reread_source_masks(client):
    """Between the read and write phases, poison the source masks. The repair
    must still write the sets it derived during the read phase, proving the
    write path never consults ``band_masks`` again."""
    import sqlite3
    from app import config, services
    from app.models import RepairRequest

    assert _post(client, band="red", mask=[[1, 0]]).status_code == 200
    # Stale committed coverage to force a non-empty change set.
    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        conn.execute(
            "UPDATE coverage SET red_scenes = '[]', nir_scenes = '[]' "
            "WHERE date = '2026-11-23' AND x = 0 AND y = 0"
        )
        conn.commit()
    finally:
        conn.close()

    def poison_masks():
        conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
        try:
            conn.execute(
                "UPDATE band_masks SET mask = X'00' "
                "WHERE date = '2026-11-23' AND scene_id = 'S1' AND band = 'red'"
            )
            conn.commit()
        finally:
            conn.close()

    req = RepairRequest(date="2026-11-23", origin_x=0, origin_y=0,
                        width=1, height=1)
    result = services.repair_coverage(
        services.connect, req, after_read_hook=poison_masks
    )
    assert result["cells_changed"] == 1
    assert result["changes"][0]["red_added"] == ["S1"]
    import json as _json
    row = _coverage(client)
    assert _json.loads(row["red_scenes"]) == ["S1"]


def test_repair_endpoint_returns_409_on_conflict_and_keeps_submission(client, monkeypatch):
    """Route-level check: a submission during recompute yields HTTP 409, the
    submission survives, and the repair's own changes are absent."""
    import app.main as main_module
    import app.services as services

    assert _post(client, band="red", mask=[[1, 0]]).status_code == 200
    assert _post(client, band="nir", mask=[[1, 0]]).status_code == 200

    real_repair = services.repair_coverage

    def repair_with_concurrent_submission(conn_factory, request, **kwargs):
        def hook():
            assert _post(client, band="red", mask=[[1, 1]]).status_code == 200
        return real_repair(conn_factory, request, after_read_hook=hook)

    monkeypatch.setattr(main_module, "repair_coverage", repair_with_concurrent_submission)

    r = _repair(client, width=2, height=1)
    assert r.status_code == 409
    assert "changed inside the repair rectangle" in r.json()["detail"]

    data = _gaps(client, width=2, height=1)
    assert data["complete_cells"] == 1  # S1 red at x=1 only; nir still x=0 only
    assert data["category_counts"]["red_only"] == 1


def test_repair_reported_changes_match_actual_database_diff(client):
    """Cross-check the response against a raw before/after coverage diff."""
    import json as _json
    import sqlite3
    from app import config, services
    from app.models import RepairRequest

    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1], [0, 1]]).status_code == 200
    assert _post(client, scene_id="S2", origin_x=1, origin_y=1, band="nir",
                 mask=[[1]]).status_code == 200

    def snapshot():
        conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            return {
                (r["x"], r["y"]): (_json.loads(r["red_scenes"]),
                                   _json.loads(r["nir_scenes"]))
                for r in conn.execute(
                    "SELECT x, y, red_scenes, nir_scenes FROM coverage "
                    "WHERE date = '2026-11-23' AND x BETWEEN 0 AND 1 "
                    "AND y BETWEEN 0 AND 1"
                )
            }
        finally:
            conn.close()

    before = snapshot()
    req = RepairRequest(date="2026-11-23", origin_x=0, origin_y=0,
                        width=2, height=2)
    result = services.repair_coverage(services.connect, req)
    after = snapshot()

    expected_diff = {}
    for cell in set(before) | set(after):
        b = before.get(cell, ([], []))
        a = after.get(cell, ([], []))
        if b != a:
            expected_diff[cell] = (sorted(set(a[0]) - set(b[0])),
                                   sorted(set(b[0]) - set(a[0])),
                                   sorted(set(a[1]) - set(b[1])),
                                   sorted(set(b[1]) - set(a[1])))
    reported = {(c["x"], c["y"]): (c["red_added"], c["red_removed"],
                                   c["nir_added"], c["nir_removed"])
                for c in result["changes"]}
    assert reported == expected_diff
    assert result["cells_changed"] == len(expected_diff)


def test_repair_after_direct_mask_restore_rebuilds_stale_coverage(client):
    """Simulate restoring source masks from a backup outside the API: the
    band_masks table changes but coverage does not. The repair must bring
    coverage back in line with the restored masks."""
    import sqlite3
    from app import config

    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S1", band="nir",
                 mask=[[1, 1]]).status_code == 200
    # (0,0) and (1,0) are complete now.
    assert _gaps(client, width=2, height=1)["complete_cells"] == 2

    # Restore a different NIR mask directly into the source table.
    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        conn.execute(
            "UPDATE band_masks SET mask = X'0100' "
            "WHERE date = '2026-11-23' AND scene_id = 'S1' AND band = 'nir'"
        )
        conn.commit()
    finally:
        conn.close()
    # Committed coverage is now stale: still reports two complete cells.
    assert _gaps(client, width=2, height=1)["complete_cells"] == 2

    r = _repair(client, width=2, height=1)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["cells_changed"] == 1
    cell = data["changes"][0]
    assert (cell["x"], cell["y"]) == (1, 0)
    assert cell["nir_removed"] == ["S1"]

    after = _gaps(client, width=2, height=1)
    assert after["complete_cells"] == 1
    assert after["category_counts"]["red_only"] == 1


def test_repair_fixes_different_scenes_cells_using_intersecting_masks(client):
    """A cell committed as `different_scenes` because a scene's band mask was
    missing from coverage; after restore both bands come from one scene."""
    import sqlite3
    from app import config

    assert _post(client, scene_id="S1", band="red",
                 mask=[[1, 1]]).status_code == 200
    assert _post(client, scene_id="S1", band="nir",
                 mask=[[1, 1]]).status_code == 200

    # Simulate stale coverage where S1's NIR contribution at (0,0) was lost
    # and a different scene's NIR was recorded instead.
    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        conn.execute(
            "UPDATE coverage SET nir_scenes = '[\"S2\"]' "
            "WHERE date = '2026-11-23' AND x = 0 AND y = 0"
        )
        conn.commit()
    finally:
        conn.close()

    before = _gaps(client, width=2, height=1)
    assert before["category_counts"]["different_scenes"] == 1

    r = _repair(client, width=2, height=1)
    assert r.status_code == 200, r.text
    cell = next(c for c in r.json()["changes"] if (c["x"], c["y"]) == (0, 0))
    assert cell["nir_added"] == ["S1"]
    assert cell["nir_removed"] == ["S2"]

    after = _gaps(client, width=2, height=1)
    assert after["complete_cells"] == 2
    assert after["category_counts"]["different_scenes"] == 0


def test_repair_rejects_corrupt_source_mask_without_changes(client):
    """A restored mask whose byte length disagrees with the registered
    geometry aborts the repair with 500 and leaves coverage untouched."""
    import sqlite3
    from app import config

    assert _post(client, band="red", mask=[[1, 1], [0, 1]]).status_code == 200
    before = _gaps(client, width=2, height=2)

    conn = sqlite3.connect(str(config.DEFAULT_DB_PATH))
    try:
        # Two bytes for a 2x2 geometry -> corrupt.
        conn.execute(
            "UPDATE band_masks SET mask = X'0101' "
            "WHERE date = '2026-11-23' AND scene_id = 'S1' AND band = 'red'"
        )
        conn.commit()
    finally:
        conn.close()

    r = _repair(client)
    assert r.status_code == 500
    assert "repair aborted without changes" in r.json()["detail"]
    assert _gaps(client, width=2, height=2) == before
