"""Independent acceptance checks for the bounded route-snapshot contract.

This file deliberately exercises the lifecycle deadline and the PDF owner
boundary separately from the large-state regression fixtures.  The deadline
case is expected to fail until an expired durable run is rejected before the
snapshot context is allocated; keeping it explicit prevents a 1 ms clamp from
being mistaken for a valid deadline gate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from neocortex.capabilities.formats.pdf.pdf_candidate_plan import owned_pdf_candidates
from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute, _PdfOwnerCoordinator
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.persistence import framework_state_writer
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.persistence import sqlite_temporary_space, sqlite_immutable
from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudget
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken


def test_expired_durable_deadline_is_not_extended_by_route_snapshot(tmp_path: Path, monkeypatch) -> None:
    """An expired run must fail closed, not receive the historical 1 ms grace."""

    now = [1_000_000_000]
    monkeypatch.setattr(framework_state_writer.time, "time_ns", lambda: now[0])
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(tmp_path, None)
        state.publish_run_budget(run_id, {"max_duration_seconds": 1.0})
        now[0] = 3_000_000_000
        observed: dict[str, float] = {}

        def budget_factory(*args, **kwargs):
            observed["prepare_timeout_seconds"] = float(kwargs["prepare_timeout_seconds"])
            return SQLiteSnapshotBudget(
                max_temporary_bytes=64 * 1024 * 1024,
                prepare_timeout_seconds=60,
            )

        monkeypatch.setattr(sqlite_immutable, "automatic_snapshot_budget", budget_factory)
        try:
            with state.route_candidate_snapshot(run_id=run_id):
                pass
        except RunBudgetExceeded:
            # A fail-closed lifecycle gate is the preferred implementation.
            return
        assert observed["prepare_timeout_seconds"] <= 0, (
            "expired durable deadlines must not be clamped to a fresh positive "
            "snapshot timeout"
        )


def test_pdf_candidate_plan_cancellation_releases_owner_temp_table(tmp_path: Path) -> None:
    """Cancellation after a page does not strand TEMP state or the owner thread."""

    database = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(database)
    owner = _PdfOwnerCoordinator(database)
    owner.start()
    cancellation = CancellationToken()

    def populate(connection: sqlite3.Connection) -> None:
        connection.executemany(
            "INSERT INTO pdf_inventory VALUES(?,?,?,?,?,?)",
            (
                (f"1:{index:x}", f"/fixture/{index:06}.pdf", index, 1, -1, 1)
                for index in range(600)
            ),
        )

    owner.call(populate)
    candidates = owned_pdf_candidates(
        owner,
        "SELECT file_key,path,size,mtime_ns,birthtime_ns FROM pdf_inventory ORDER BY path",
        (),
        cancellation=cancellation,
        decode=PdfRoute._inventory_row_snapshot,
        min_free_bytes=0,
    )
    try:
        assert next(candidates).file_id == 0
        cancellation.cancel()
        with pytest.raises(CancellationRequested):
            next(candidates)
        assert owner.call(
            lambda connection: connection.execute(
                "SELECT count(*) FROM sqlite_temp_schema "
                "WHERE name='pdf_candidate_plan'"
            ).fetchone()[0]
        ) == 0
    finally:
        candidates.close()
        owner.close()


def test_pdf_candidate_estimate_reserves_above_real_temp_pages(tmp_path: Path) -> None:
    """A long metadata selection stays below its reserved/capped TEMP budget."""

    database = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(database)
    owner = _PdfOwnerCoordinator(database)
    owner.start()

    def populate(connection: sqlite3.Connection) -> None:
        connection.executemany(
            "INSERT INTO pdf_inventory VALUES(?,?,?,?,?,?)",
            (
                (
                    f"1:{index:x}",
                    f"/fixture/{index:06d}-{'x' * 400}.pdf",
                    index,
                    1,
                    -1,
                    1,
                )
                for index in range(5_000)
            ),
        )

    owner.call(populate)
    candidates = owned_pdf_candidates(
        owner,
        "SELECT file_key,path,size,mtime_ns,birthtime_ns FROM pdf_inventory ORDER BY path",
        (),
        cancellation=CancellationToken(),
        decode=PdfRoute._inventory_row_snapshot,
        min_free_bytes=0,
    )
    try:
        assert next(candidates).file_id == 0
        page_count, page_size = cast(tuple[int, int], owner.call(
            lambda connection: (
                connection.execute("PRAGMA temp.page_count").fetchone()[0],
                connection.execute("PRAGMA temp.page_size").fetchone()[0],
            )
        ))
        actual_bytes = int(page_count) * int(page_size)
        reservations = [value[2] for value in sqlite_temporary_space._LIVE.values()]
        assert reservations and actual_bytes <= max(reservations)
        assert sum(1 for _ in candidates) == 4_999
    finally:
        candidates.close()
        owner.close()
    assert not sqlite_temporary_space._LIVE
