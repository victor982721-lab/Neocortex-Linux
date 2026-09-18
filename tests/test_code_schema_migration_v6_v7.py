from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.code import code_schema
from neocortex.code.code_schema import initialize_code_state, validate_code_schema


_RECEIPT_COLUMNS = code_schema._CODE_EXPERIMENT_RECEIPT_COLUMNS


def _receipt_values(receipt_id: str, *, status: str, payload: str) -> tuple[object, ...]:
    return (
        receipt_id,
        1,
        f"evaluation:{receipt_id}",
        "question:fixture",
        "subject:fixture",
        f"proposal:{receipt_id}",
        "template:fixture",
        "v1",
        "processing:fixture",
        "review:fixture",
        f"envelope:{receipt_id}",
        "neocortex.code-experiment-receipt/v3",
        status,
        payload,
        f"xxh3-128:{receipt_id}",
        f"xxh3-64:{receipt_id}",
        len(payload.encode("utf-8")),
        100 + len(receipt_id),
        "advisory",
        0,
    )


def _code_v6_database(path: Path) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(path)
    try:
        code_schema._build_legacy_schema(connection, 6)
        for version in range(1, 7):
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?)",
                (version, f"fixture-v{version}", version),
            )
        connection.execute("INSERT INTO metadata VALUES('schema_version','6')")
        connection.execute("PRAGMA user_version=6")
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns) VALUES(1,1,1,'processing:fixture','completed',1,2)"""
        )
        columns = ",".join(_RECEIPT_COLUMNS)
        placeholders = ",".join("?" for _column in _RECEIPT_COLUMNS)
        rows = (
            _receipt_values(
                "receipt:alpha",
                status="passed",
                payload='{"schema":"neocortex.code-experiment-receipt/v3","note":"á"}',
            ),
            _receipt_values(
                "receipt:beta",
                status="abstained",
                payload='{"schema":"neocortex.code-experiment-receipt/v3", "raw": true}',
            ),
        )
        connection.executemany(
            f"INSERT INTO code_experiment_receipts({columns}) VALUES({placeholders})",
            rows,
        )
        connection.commit()
        code_schema._validate_legacy_code_schema(connection, 6)
        return tuple(
            connection.execute(
                f"SELECT {columns} FROM code_experiment_receipts ORDER BY receipt_id"
            ).fetchall()
        )
    finally:
        connection.close()


def _receipt_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    columns = ",".join(_RECEIPT_COLUMNS)
    return tuple(
        connection.execute(
            f"SELECT {columns} FROM code_experiment_receipts ORDER BY receipt_id"
        ).fetchall()
    )


def test_v6_to_v7_preserves_every_v3_field_and_accepts_only_v3_or_v4(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code-v6.sqlite3"
    before = _code_v6_database(database)

    initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (code_schema.CODE_SCHEMA_VERSION,)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(code_schema.CODE_SCHEMA_VERSION),)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in range(1, code_schema.CODE_SCHEMA_VERSION + 1)]
        assert _receipt_rows(connection) == before
        assert connection.execute(
            """SELECT hex(CAST(payload_json AS BLOB))
            FROM code_experiment_receipts ORDER BY receipt_id"""
        ).fetchall() == [(bytes(str(row[13]), "utf-8").hex().upper(),) for row in before]
        validate_code_schema(connection)

        columns = ",".join(_RECEIPT_COLUMNS)
        placeholders = ",".join("?" for _column in _RECEIPT_COLUMNS)
        v4 = list(
            _receipt_values(
                "receipt:v4",
                status="passed",
                payload='{"schema":"neocortex.code-experiment-receipt/v4"}',
            )
        )
        v4[11] = "neocortex.code-experiment-receipt/v4"
        connection.execute(
            f"INSERT INTO code_experiment_receipts({columns}) VALUES({placeholders})",
            tuple(v4),
        )

        future = list(v4)
        future[0] = "receipt:future"
        future[10] = "envelope:future"
        future[11] = "neocortex.code-experiment-receipt/v5"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO code_experiment_receipts({columns}) VALUES({placeholders})",
                tuple(future),
            )
        with pytest.raises(sqlite3.IntegrityError, match="receipts are immutable"):
            connection.execute(
                "UPDATE code_experiment_receipts SET review_digest='changed' "
                "WHERE receipt_id='receipt:alpha'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="receipts are immutable"):
            connection.execute(
                "DELETE FROM code_experiment_receipts WHERE receipt_id='receipt:alpha'"
            )


def test_v6_to_v7_failure_rolls_back_table_rows_indexes_triggers_and_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rollback-code-v6.sqlite3"
    before = _code_v6_database(database)
    monkeypatch.setattr(
        code_schema,
        "_V7_DDL",
        (*code_schema._V7_DDL, "CREATE TABLE deliberately_incomplete("),
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("6",)
        assert _receipt_rows(connection) == before
        assert connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name LIKE 'code_experiment_receipts_%' "
            "ORDER BY name"
        ).fetchall() == [
            ("code_experiment_receipts_context_idx", "index"),
            ("code_experiment_receipts_no_delete", "trigger"),
            ("code_experiment_receipts_no_update", "trigger"),
            ("code_experiment_receipts_proposal_idx", "index"),
        ]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name='__neocortex_code_v6_experiment_receipts'"
            ).fetchone()
            is None
        )
        code_schema._validate_legacy_code_schema(connection, 6)
