"""Large-state regressions: metadata size, not full history, drives route work."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute, _PdfOwnerCoordinator
from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteConfig
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.deduplication import DedupIndex
from neocortex.persistence import sqlite_immutable
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.sqlite_immutable import SQLiteReadSession, SQLiteSnapshotBudgetExceeded
from neocortex.persistence import sqlite_temporary_space as space
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from tests.test_pdf_route import _State, _write_pdf

TEST_CAPABILITIES = ("documents",)


class _PdfCandidates(_State):
    def iter_selected_route_candidates(self, run_id, mime, route_name, selection):
        yield from self.iter_route_candidates(run_id, mime)


def _large_pdf_state(path: Path) -> None:
    initialize_pdf_state(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,zeroblob(?))",
            ((f"history-{i}", 1024 * 1024) for i in range(260)),
        )
    assert path.stat().st_size > 256 * 1024 * 1024


def test_public_pdf_route_large_history_first_and_replay_never_copy_owner(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_pdf(corpus / "one.pdf", "Synthetic metadata scaling regression, no OCR", title="one")
    database = tmp_path / "pdf.sqlite3"
    _large_pdf_state(database)
    with (
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
        patch.object(sqlite_immutable, "_copy_regular_file_budgeted", side_effect=AssertionError("full owner copy")),
    ):
        scan = index.scan(corpus, excluded_paths=())
        state = _PdfCandidates(tuple(index.snapshots(scan.scan_id)))
        config = PdfRouteConfig(database, workers=2, ocr_mode="never", min_free_bytes=0, memory_backpressure_bytes=0)
        first = PdfRoute(config, index, state, 1, scan.scan_id).run()
        second = PdfRoute(config, index, state, 2, scan.scan_id).run()
    assert first.extracted == 1 and first.errors == 0
    assert second.cache_hits == 1 and second.extracted == 0 and second.errors == 0
    with SQLiteReadSession(database) as c:
        assert c.execute("SELECT COUNT(*) FROM metadata WHERE key LIKE 'history-%'").fetchone()[0] == 260
        assert c.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1


def test_pdf_metadata_plan_frozen_across_batched_owner_writes_and_cancel(tmp_path: Path) -> None:
    from neocortex.capabilities.formats.pdf.pdf_candidate_plan import owned_pdf_candidates
    path = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(path)
    owner = _PdfOwnerCoordinator(path)
    owner.start()
    cancellation = CancellationToken()

    def populate(c):
        c.executemany("INSERT INTO pdf_inventory VALUES(?,?,?,?,?,?)", (
            (f"1:{i:x}", f"/fixture/{i:06}.pdf", i, 1, -1, 1) for i in range(1000)
        ))
    owner.call(populate)
    candidates = owned_pdf_candidates(owner,
        "SELECT file_key,path,size,mtime_ns,birthtime_ns FROM pdf_inventory ORDER BY path", (),
        cancellation=cancellation, decode=PdfRoute._inventory_row_snapshot, min_free_bytes=0)
    try:
        first = next(candidates)
        assert first.file_id == 0
        owner.call(lambda c: c.execute("DELETE FROM pdf_inventory"))
        # The current generation is stable, not a cursor over changing rows.
        assert [next(candidates).file_id for _ in range(300)] == list(range(1, 301))
        cancellation.cancel()
        with pytest.raises(CancellationRequested):
            next(candidates)
        assert owner.call(lambda c: c.execute("SELECT count(*) FROM sqlite_temp_schema WHERE name='pdf_candidate_plan'").fetchone()[0]) == 0
    finally:
        candidates.close()
        owner.close()


def test_explicit_snapshot_ceiling_has_actionable_owner_and_space_evidence(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("CREATE TABLE content(value)")
        c.execute("INSERT INTO content VALUES(1)")
    with pytest.raises(SQLiteSnapshotBudgetExceeded) as caught:
        with SQLiteReadSession(path, mode="snapshot_temp", max_temporary_bytes=1, temp_root=tmp_path):
            pytest.fail("explicit byte limit must remain authoritative")
    assert caught.value.reason == "temporary_bytes"
    assert caught.value.context["main_bytes"] == path.stat().st_size
    assert caught.value.context["allowed_bytes"] == 1
    assert caught.value.context["retained_bytes"] == 0
    assert "free_bytes=" in str(caught.value) and "WAL/SHM" in str(caught.value)
    assert not list(tmp_path.glob("neocortex-sqlite-read-*"))


def test_automatic_budget_uses_resources_and_explicit_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(space, "_resources", lambda root: (4_000_000_000, 800_000_000, True))
    auto = sqlite_immutable.automatic_snapshot_budget(tmp_path, prepare_timeout_seconds=3)
    assert auto.max_temporary_bytes == 720_000_000 and auto.resource_aware
    explicit = sqlite_immutable.automatic_snapshot_budget(tmp_path, prepare_timeout_seconds=3, max_temporary_bytes=1024)
    assert explicit.max_temporary_bytes == 1024


def test_concurrent_projection_space_accounts_outstanding_writes_and_cancels(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(space, "_resources", lambda root: (100_000_000, 1_000_000_000, False))
    first = space.SQLiteTemporarySpace(tmp_path)
    second = space.SQLiteTemporarySpace(tmp_path)
    try:
        first.reserve(80_000_000, checkpoint=lambda: None, deadline=99, clock=lambda: 0)
        def cancel():
            raise CancellationRequested()
        with pytest.raises(CancellationRequested):
            second.reserve(30_000_000, checkpoint=cancel, deadline=99, clock=lambda: 0)
        # Once retained, fail rather than wait circularly for another holder.
        second.observe(1)
        with pytest.raises(SQLiteSnapshotBudgetExceeded) as caught:
            second.reserve(30_000_000, checkpoint=lambda: None, deadline=99, clock=lambda: 0)
        assert caught.value.reason == "disk_space"
    finally:
        first.close()
        second.close()
    assert not space._LIVE


def test_real_memory_pressure_is_distinct_from_disk_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(space, "_resources", lambda root: (10**9, 1024, False))
    reservation = space.SQLiteTemporarySpace(tmp_path)
    try:
        with pytest.raises(SQLiteSnapshotBudgetExceeded) as caught:
            reservation.reserve(4096, checkpoint=lambda: None, deadline=0, clock=lambda: 0)
        assert caught.value.reason == "memory_pressure"
    finally:
        reservation.close()


def test_framework_projection_explicit_limit_and_cleanup(tmp_path: Path) -> None:
    with FrameworkState(tmp_path / "framework.sqlite3") as state:
        run = state.begin_initial_run(tmp_path, None)
        with pytest.raises(SQLiteSnapshotBudgetExceeded):
            with state.route_candidate_snapshot(run_id=run, max_temporary_bytes=1):
                pytest.fail("small explicit limit must not be raised automatically")
        assert not state._connection.in_transaction
    assert not space._LIVE


def test_pdf_candidate_projection_rejects_real_space_pressure_without_losing_state(tmp_path: Path, monkeypatch) -> None:
    from neocortex.capabilities.formats.pdf.pdf_candidate_plan import owned_pdf_candidates
    path = tmp_path / 'pdf.sqlite3'
    initialize_pdf_state(path)
    owner = _PdfOwnerCoordinator(path)
    owner.start()
    owner.call(lambda c: c.execute("INSERT INTO pdf_inventory VALUES('1:1','/fixture/one.pdf',1,1,-1,1)"))
    monkeypatch.setattr(space, '_resources', lambda root: (1, 10**9, False))
    stream = owned_pdf_candidates(owner,
        'SELECT file_key,path,size,mtime_ns,birthtime_ns FROM pdf_inventory', (),
        cancellation=CancellationToken(), decode=PdfRoute._inventory_row_snapshot, min_free_bytes=0)
    try:
        with pytest.raises(SQLiteSnapshotBudgetExceeded, match='disk space'):
            next(stream)
        assert owner.call(lambda c: c.execute('SELECT count(*) FROM pdf_inventory').fetchone()[0]) == 1
        assert owner.call(lambda c: c.execute("SELECT count(*) FROM sqlite_temp_schema WHERE name='pdf_candidate_plan'").fetchone()[0]) == 0
    finally:
        stream.close()
        owner.close()
    assert not space._LIVE


def test_candidate_iterator_opens_one_view_across_pages_and_releases_on_close(tmp_path: Path, monkeypatch) -> None:
    from neocortex.persistence.framework_route_state import FrameworkRouteState
    from neocortex.deduplication import FileSnapshot
    with FrameworkState(tmp_path/'framework.sqlite3') as state:
        run = state.begin_initial_run(tmp_path, None)
        state.store_route_candidates(run, (
            ('text/plain', FileSnapshot(f'/fixture/{i:06}', 1, i+1, 1, 1, -1)) for i in range(2200)
        ))
        with state.route_candidate_snapshot(run_id=run) as view:
            route = FrameworkRouteState(state.path, candidate_database=view)
            opened = []
            real = route._connect_candidates
            def connect():
                c = real()
                opened.append(c)
                return c
            monkeypatch.setattr(route, '_connect_candidates', connect)
            stream = route.iter_route_candidates(run, 'text/plain')
            assert sum(1 for _ in stream) == 2200
            assert len(opened) == 1
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                opened[0].execute('SELECT 1')


def test_pdf_owned_plan_rejects_owner_replacement(tmp_path: Path) -> None:
    from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable
    path = tmp_path/'pdf.sqlite3'
    initialize_pdf_state(path)
    owner = _PdfOwnerCoordinator(path)
    owner.start()
    replacement = tmp_path/'replacement.sqlite3'
    initialize_pdf_state(replacement)
    try:
        replacement.replace(path)
        with pytest.raises(ImmutableSQLiteUnavailable, match='identity changed'):
            owner.call(lambda c: c.execute('SELECT 1').fetchone())
    finally:
        owner.close()


def test_initial_external_memory_pressure_waits_cancelably(tmp_path: Path, monkeypatch) -> None:
    import time
    monkeypatch.setattr(space, '_resources', lambda root: (10**9, 1024, False))
    reservation = space.SQLiteTemporarySpace(tmp_path)
    calls = 0
    def cancel_after_wait():
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise CancellationRequested('pressure wait cancelled')
    try:
        with pytest.raises(CancellationRequested):
            reservation.reserve(4096, checkpoint=cancel_after_wait, deadline=time.monotonic()+1)
        assert reservation.waits == 1
    finally:
        reservation.close()


def test_sqlite_temp_directory_uses_native_override_and_unix_fallback(tmp_path: Path, monkeypatch) -> None:
    import os
    custom = tmp_path / 'sqlite-native-temp'
    custom.mkdir()
    monkeypatch.setenv('SQLITE_TMPDIR', str(custom))
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    assert space.sqlite_temporary_directory() == custom
    monkeypatch.delenv('SQLITE_TMPDIR')
    assert space.sqlite_temporary_directory() == tmp_path
    monkeypatch.delenv('TMPDIR')
    monkeypatch.setenv('TEMP', str(custom))
    if Path('/var/tmp').is_dir() and os.access('/var/tmp', os.W_OK | os.X_OK):
        assert space.sqlite_temporary_directory() == Path('/var/tmp')


@pytest.mark.parametrize('context', ({}, {'owner': 'pdf', 'allowed_bytes': 1024}))
def test_snapshot_error_preserves_public_identity_and_diagnostics_through_pickle(context) -> None:
    import pickle
    original = SQLiteSnapshotBudgetExceeded('temporary_bytes', **context)
    original.add_note('fixture diagnostic note')
    restored = pickle.loads(pickle.dumps(original))
    assert type(restored) is SQLiteSnapshotBudgetExceeded
    assert restored.__class__.__module__ == 'neocortex.persistence.sqlite_immutable'
    assert (restored.reason, restored.context, str(restored), restored.__notes__) == (
        original.reason, original.context, str(original), original.__notes__,
    )
