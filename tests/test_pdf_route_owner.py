"""Regression coverage for PDF-owner coordination during parallel extraction."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.capabilities.formats.pdf import pdf_route as pdf_route_module
from neocortex.capabilities.formats.pdf import pdf_route_cache, pdf_route_storage
from neocortex.capabilities.formats.pdf.pdf_route import (
    PdfRoute,
    _PdfOwnerCoordinator,
)
from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteConfig
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.deduplication import DedupIndex, FileSnapshot
from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadMode,
    SQLiteReadSession,
)
from tests.test_pdf_route import _State, _write_pdf


TEST_CAPABILITIES = ("documents",)


def _owner_fixture(path: Path) -> sqlite3.Connection:
    initialize_pdf_state(path)
    connection = sqlite3.connect(path, timeout=2)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('fixture','active-writer')"
    )
    connection.commit()
    return connection


def test_pdf_owner_coordinator_avoids_unstable_snapshot_under_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changing WAL rejects a detached reader, but coordinated reads remain usable."""

    database = tmp_path / "pdf.sqlite3"
    writer = _owner_fixture(database)
    mutations = 0
    copy = sqlite_immutable._copy_regular_file

    def mutate_after_main_copy(source: Path, destination: Path) -> None:
        nonlocal mutations
        copy(source, destination)
        if source == database:
            mutations += 1
            writer.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('fixture',?)",
                (f"active-writer-{mutations}",),
            )
            writer.commit()

    monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", mutate_after_main_copy)
    with pytest.raises(
        ImmutableSQLiteUnavailable,
        match="stable temporary snapshot",
    ):
        with SQLiteReadSession(
            database,
            mode=SQLiteReadMode.SNAPSHOT_TEMP,
            temp_root=tmp_path,
            max_attempts=2,
        ):
            pytest.fail("an unstable writer must not publish a detached snapshot")
    assert mutations >= 2

    route = object.__new__(PdfRoute)
    route.config = PdfRouteConfig(
        database,
        ocr_mode="never",
        document_timeout_seconds=30,
        min_free_bytes=0,
    )
    owner = _PdfOwnerCoordinator(database)
    owner.start()
    route._pdf_owner = owner
    snapshot = FileSnapshot(str(tmp_path / "document.pdf"), 1, 2, 100, 1, 1)
    try:
        assert route._effective_document_timeout(snapshot) == 30
    finally:
        route._pdf_owner = None
        owner.close()
        writer.close()

    assert not list(tmp_path.glob("pdf.sqlite3-*"))


def test_parallel_pdf_route_replay_keeps_owner_access_off_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel extraction and a second run remain successful and idempotent."""

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_pdf(corpus / "one.pdf", "alpha transformador", title="one")
    _write_pdf(corpus / "two.pdf", "beta transformador", title="two")
    database = tmp_path / "pdf.sqlite3"

    def forbid_worker_open(real_open):
        def guarded(*args, **kwargs):
            if threading.current_thread().name.startswith("neocortex-pdf-worker"):
                raise AssertionError("PDF extraction worker opened the owner")
            return real_open(*args, **kwargs)

        return guarded

    with (
        patch.object(
            pdf_route_module,
            "_database",
            forbid_worker_open(pdf_route_module._database),
        ),
        patch.object(
            pdf_route_cache,
            "pdf_database",
            forbid_worker_open(pdf_route_cache.pdf_database),
        ),
        patch.object(
            pdf_route_storage,
            "pdf_database",
            forbid_worker_open(pdf_route_storage.pdf_database),
        ),
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
    ):
        scan = index.scan(corpus, excluded_paths=())
        snapshots = tuple(index.snapshots(scan.scan_id))
        config = PdfRouteConfig(
            database,
            ocr_mode="never",
            workers=2,
            document_timeout_seconds=30,
            min_free_bytes=0,
            memory_backpressure_bytes=0,
        )
        first = PdfRoute(config, index, _State(snapshots), 1, scan.scan_id).run()
        second = PdfRoute(config, index, _State(snapshots), 2, scan.scan_id).run()

    assert first.extracted == 2
    assert first.errors == 0
    assert second.cache_hits == 2
    assert second.extracted == 0
    assert second.errors == 0
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 2
