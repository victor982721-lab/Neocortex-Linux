"""Code block storage must preserve bounded, non-migrating legacy observation."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.code import code_schema
from neocortex.knowledge.knowledge_contracts import OwnerAvailability
from neocortex.knowledge.knowledge_snapshot import (
    KnowledgeStatePaths,
    collect_knowledge_snapshot,
)


@pytest.mark.parametrize("malformed", [False, True])
def test_knowledge_observes_exact_code_v8_without_migrating(
    tmp_path: Path, malformed: bool,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "code.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        code_schema._build_v8_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','8')")
        connection.execute("PRAGMA user_version=8")
        connection.executemany(
            "INSERT INTO schema_migrations VALUES(?,?,?)",
            [(version, f"fixture-v{version}", version) for version in range(1, 9)],
        )
        connection.execute(
            "INSERT INTO graph_generation_metadata VALUES('schema_version','1')"
        )
        connection.execute(
            "INSERT INTO graph_generation_migrations VALUES(1,'fixture graph v1',1)"
        )
        connection.execute("INSERT INTO metadata VALUES('fixture_evidence','retained')")
        code_schema.validate_code_schema_v8(connection)
        if malformed:
            connection.execute("ALTER TABLE graph_memberships ADD COLUMN unrecognized TEXT")
    before = database.read_bytes()

    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state), source_version="fixture-code-v9-reader",
    )

    owner = next(item for item in snapshot.owners if item.owner == "code")
    assert owner.expected_schema_version == code_schema.CODE_SCHEMA_VERSION
    if malformed:
        assert owner.state is OwnerAvailability.INCOMPATIBLE
        assert owner.publications == ()
    else:
        assert owner.state is OwnerAvailability.AVAILABLE
        assert owner.observed_schema_version == 8
        assert owner.warning == f"legacy_schema_read_compatible:8->{code_schema.CODE_SCHEMA_VERSION}"
    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='fixture_evidence'"
        ).fetchone() == ("retained",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='graph_input_blocks'"
        ).fetchone() is None
