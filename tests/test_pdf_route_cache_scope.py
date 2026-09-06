from __future__ import annotations

import sqlite3
from pathlib import Path

from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.capabilities.formats.pdf.pdf_route_cache import (
    PdfRouteCacheMixin,
    file_key,
)
from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteConfig
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state


def _insert_done_document(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    signature: str,
    run_id: int,
) -> None:
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        last_seen_run_id,updated_ns) VALUES(?,?,?,?,?,?, 'done', ?, ?)""",
        (
            file_key(snapshot),
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
            run_id,
            1,
        ),
    )
    connection.execute(
        """INSERT INTO pdf_inventory(
        file_key,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
        VALUES(?,?,?,?,?,?)""",
        (
            file_key(snapshot),
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            run_id,
        ),
    )


def test_prune_does_not_delete_cache_from_another_source_root(tmp_path: Path) -> None:
    state = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(state)
    real_root = tmp_path / "corpus"
    smoke_root = tmp_path / "smoke"
    real_root.mkdir()
    smoke_root.mkdir()
    real = FileSnapshot(str(real_root / "real.pdf"), 1, 1, 100, 11, 12)
    smoke = FileSnapshot(str(smoke_root / "smoke.pdf"), 1, 2, 100, 11, 13)
    route = object.__new__(PdfRouteCacheMixin)
    route.config = PdfRouteConfig(state, ocr_mode="never")
    route.cancellation = CancellationToken()
    route.run_id = 2

    with sqlite3.connect(state) as connection:
        _insert_done_document(connection, real, route.config.processing_signature, 1)
        _insert_done_document(connection, smoke, route.config.processing_signature, 2)
        connection.execute(
            "UPDATE pdf_inventory SET last_seen_run_id=1 WHERE file_key=?",
            (file_key(real),),
        )
        connection.commit()

    assert route._prune_pdf_cache() == (0, 0)
    assert route._is_cache_hit(real, touch=False)

    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM pdf_inventory").fetchone()[0] == 2
