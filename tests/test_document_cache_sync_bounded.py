# region [00] Contexto del módulo
# Módulo: tests/test_document_cache_sync_bounded.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path

from neocortex.documents.document_cache_sync import (
    _PENDING_ACTION_SYNC_BATCH_SIZE,
    _sync_pending_file_actions,
)
# endregion [01]

# region [02] Implementación


def test_pending_file_action_sync_uses_bounded_batches() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE file_actions(
            action_id INTEGER PRIMARY KEY,
            action_type TEXT NOT NULL,
            source_path TEXT NOT NULL,
            target_path TEXT,
            status TEXT NOT NULL
        )"""
    )
    old_path = str(Path("C:/Incoming/document.pdf"))
    new_path = str(Path("C:/Organized/document.pdf"))
    source_count = (_PENDING_ACTION_SYNC_BATCH_SIZE * 2) + 89
    target_only_count = 7
    connection.executemany(
        """INSERT INTO file_actions(
            action_id,action_type,source_path,target_path,status
        ) VALUES(?, 'trash_duplicate', ?, NULL, 'planned')""",
        ((action_id, old_path) for action_id in range(1, source_count + 1)),
    )
    connection.executemany(
        """INSERT INTO file_actions(
            action_id,action_type,source_path,target_path,status
        ) VALUES(?, 'trash_duplicate', ?, ?, 'planned')""",
        (
            (source_count + offset, f"C:/Elsewhere/{offset}.pdf", old_path)
            for offset in range(1, target_only_count + 1)
        ),
    )
    connection.execute(
        """INSERT INTO file_actions(
            action_id,action_type,source_path,target_path,status
        ) VALUES(?, 'trash_duplicate', ?, ?, 'complete')""",
        (source_count + target_only_count + 1, old_path, old_path),
    )
    selected_batches: list[str] = []
    connection.set_trace_callback(
        lambda statement: (
            selected_batches.append(statement)
            if statement.startswith("SELECT action_id")
            else None
        )
    )

    updated = _sync_pending_file_actions(
        connection,
        old_path=old_path,
        new_path=new_path,
    )

    assert updated == source_count + target_only_count
    assert len(selected_batches) == 4
    assert all("LIMIT 256" in statement for statement in selected_batches)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM file_actions WHERE status='planned' AND source_path=?",
            (new_path,),
        ).fetchone()[0]
        == source_count
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM file_actions WHERE status='planned' AND target_path=?",
            (new_path,),
        ).fetchone()[0]
        == target_only_count
    )
    completed_row = connection.execute(
        "SELECT source_path,target_path FROM file_actions WHERE status='complete'"
    ).fetchone()
    assert completed_row is not None
    assert tuple(completed_row) == (old_path, old_path)
    connection.close()
# endregion [02]
