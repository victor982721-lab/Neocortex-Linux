# region [00] Contexto del módulo
# Módulo: tests/test_semantic_status_snapshot.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import argparse
import sqlite3
import threading
from pathlib import Path

import pytest

from neocortex.semantic import semantic_status_service
from neocortex.api.cli.cli_semantic import run_semantic_status
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
)
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
)


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')
# endregion [01]

# region [02] Implementación


def _populate_generations(database: Path, count: int) -> None:
    initialize_semantic_state(database)
    model = EmbeddingModelSpec(
        "status-model-v1",
        "status-space-v1",
        EmbeddingModality.TEXT,
        "fixture/status",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )
    register_embedding_model(database, model, allow_test_provider=True)
    with semantic_database(database) as connection:
        connection.executemany(
            """INSERT INTO embedding_generations(
            model_signature,processing_signature,status,provenance_json,
            cursor_json,started_ns,completed_ns,pending_count,leased_count,
            done_count,error_count,stale_count,base_generation_id,
            base_clone_complete)
            VALUES('status-model-v1',?,'ready','{}','{}',?,?,0,0,?,0,0,NULL,1)""",
            (
                (f"status-generation-{index}", index, index, index)
                for index in range(count)
            ),
        )


def test_semantic_status_uses_one_bounded_read_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_generations(database, 5)
    real_connect = sqlite3.connect
    connections = 0
    statements: list[str] = []

    def observed_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connections
        connections += 1
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed_connect)

    status = semantic_status_service.semantic_status(tmp_path, generation_limit=5)

    assert len(status.generations) == 5
    assert set(status.generation_timings) == {
        summary.generation_id for summary in status.generations
    }
    assert connections == 1
    assert len(statements) <= 60


def test_semantic_status_batches_a_large_generation_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_generations(database, 250)
    real_connect = sqlite3.connect
    connections = 0
    statements: list[str] = []

    def observed_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connections
        connections += 1
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed_connect)

    status = semantic_status_service.semantic_status(tmp_path, generation_limit=250)

    assert connections == 1
    assert len(status.generations) == 250
    assert status.generations[0].generation_id == 250
    assert status.generations[-1].generation_id == 1
    assert len(statements) <= 60


def test_semantic_status_generation_rows_share_the_count_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_generations(database, 2)
    real_connect = sqlite3.connect
    generation_ids_selected = threading.Event()
    writer_completed = threading.Event()
    writer_errors: list[BaseException] = []

    def observed_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        base_factory = kwargs.pop("factory", sqlite3.Connection)

        class _ObservedConnection(base_factory):  # type: ignore[misc,valid-type]
            def execute(
                self,
                sql: str,
                parameters: object = (),
            ) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)  # type: ignore[arg-type]
                if "SELECT generation_id FROM embedding_generations" in sql:
                    generation_ids_selected.set()
                    if not writer_completed.wait(10):
                        raise TimeoutError("concurrent status writer did not complete")
                return cursor

        return real_connect(*args, factory=_ObservedConnection, **kwargs)

    def writer() -> None:
        try:
            if not generation_ids_selected.wait(10):
                raise TimeoutError("semantic status did not select generation ids")
            with real_connect(database, timeout=10) as connection:
                connection.execute("DELETE FROM embedding_generations WHERE generation_id=1")
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_completed.set()

    thread = threading.Thread(target=writer, name="semantic-status-fixture-writer")
    thread.start()
    from neocortex.persistence import sqlite_immutable

    monkeypatch.setattr(
        sqlite_immutable,
        "preferred_sqlite_read_mode",
        lambda _path: sqlite_immutable.SQLiteReadMode.SNAPSHOT_TEMP,
    )
    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    try:
        status = semantic_status_service.semantic_status(tmp_path, generation_limit=2)
    finally:
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert writer_errors == []
    assert tuple(summary.generation_id for summary in status.generations) == (2, 1)


def test_semantic_status_rejects_future_schema_without_reporting_success(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_generations(database, 0)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        connection.execute("PRAGMA user_version=999")

    with pytest.raises(SemanticStateError, match="unsupported"):
        semantic_status_service.semantic_status(tmp_path, generation_limit=1)


def test_semantic_status_cli_is_blocked_for_future_schema(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_generations(database, 0)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        connection.execute("PRAGMA user_version=999")

    result = run_semantic_status(argparse.Namespace(state_directory=tmp_path))

    assert result == 2
    assert "ERROR semantic-status SemanticStateError" in capsys.readouterr().out


def test_semantic_status_rejects_current_schema_drift_without_reporting_success(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_generations(database, 0)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE embedding_generations ADD COLUMN unexpected TEXT")

    with pytest.raises(SemanticStateError, match="incompatible columns"):
        semantic_status_service.semantic_status(tmp_path, generation_limit=1)
# endregion [02]
