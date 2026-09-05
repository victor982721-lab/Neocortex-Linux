from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.persistence.sqlite_immutable import capture_sqlite_read_fence
from neocortex.workflow import state_health


def _database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES('schema_version', '1')")


def _inspect_one(monkeypatch: pytest.MonkeyPatch, *, unknown: bool = False) -> None:
    descriptors = () if unknown else (state_health._OwnerDescriptor("fixture", "owner.sqlite3", 1),)
    monkeypatch.setattr(state_health, "_state_store_descriptors", lambda: descriptors)
    monkeypatch.setattr(state_health, "_exact_validator", lambda *_args: lambda _connection: None)
    if not unknown:
        monkeypatch.setattr(state_health, "_unknown_state_entries", lambda _path: ())


@pytest.mark.parametrize("unknown", [False, True], ids=["registered", "unknown"])
@pytest.mark.parametrize(
    "stage",
    ["_table_names", "_check_quick_integrity", "_check_foreign_keys", "_check_fts"],
)
def test_sql_in_every_health_stage_is_interrupted_and_not_misclassified_as_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown: bool, stage: str
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    before = capture_sqlite_read_fence(path), path.read_bytes()
    _inspect_one(monkeypatch, unknown=unknown)

    def expensive_check(connection: sqlite3.Connection) -> None:
        try:
            connection.execute(
                "WITH RECURSIVE n(value) AS (VALUES(0) UNION ALL "
                "SELECT value+1 FROM n WHERE value < 50000000) SELECT sum(value) FROM n"
            ).fetchone()
        except sqlite3.Error as exc:
            # Integrity helpers wrap sqlite3 errors; the shared cancellation
            # scope must recover the original budget cause through that wrapper.
            raise state_health._HealthCorruptError(f"wrapped SQL error: {exc}") from exc
        pytest.fail("the long SQLite statement must be interrupted")

    monkeypatch.setattr(state_health, stage, expensive_check)
    started = time.monotonic()
    result = state_health.inspect_state_health(tmp_path, timeout_seconds=0.03)
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert len(result.owners) == 1
    assert result.owners[0].status == "blocked"
    assert "time budget exhausted" in (result.owners[0].detail or "")
    assert result.corrupt_count == 0
    assert (capture_sqlite_read_fence(path), path.read_bytes()) == before


def test_validator_sql_is_covered_by_the_global_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    _inspect_one(monkeypatch)

    def validate(connection: sqlite3.Connection) -> None:
        connection.execute(
            "WITH RECURSIVE n(value) AS (VALUES(0) UNION ALL "
            "SELECT value+1 FROM n WHERE value < 50000000) SELECT sum(value) FROM n"
        ).fetchone()

    monkeypatch.setattr(state_health, "_exact_validator", lambda *_args: validate)
    started = time.monotonic()
    result = state_health.inspect_state_health(tmp_path, timeout_seconds=0.03)
    assert time.monotonic() - started < 1.5
    assert result.owners[0].status == "blocked"
    assert "time budget exhausted" in (result.owners[0].detail or "")


@pytest.mark.parametrize("stage", ["loader", "validator", "observations"])
def test_python_stage_overrun_cannot_report_healthy_or_start_the_next_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    path = tmp_path / "owner.sqlite3"
    _database(path)
    _inspect_one(monkeypatch)
    now = [100.0]
    calls: list[str] = []
    monkeypatch.setattr(state_health.time, "monotonic", lambda: now[0])

    def validate(_connection: sqlite3.Connection) -> None:
        calls.append("validator")
        if stage == "validator":
            now[0] += 1.0

    def load(*_args: object):
        calls.append("loader")
        if stage == "loader":
            now[0] += 1.0
        return validate

    def observations(*_args: object) -> dict[str, dict[str, int]]:
        calls.append("observations")
        now[0] += 1.0
        return {}

    monkeypatch.setattr(state_health, "_exact_validator", load)
    monkeypatch.setattr(state_health, "_status_observations", observations)
    result = state_health.inspect_state_health(tmp_path, timeout_seconds=0.5)
    assert result.owners[0].status == "blocked"
    assert "time budget exhausted" in (result.owners[0].detail or "")
    stages = ["loader", "validator", "observations"]
    assert calls == stages[: stages.index(stage) + 1]


def test_exhausted_budget_keeps_registered_owners_in_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _database(tmp_path / "owner.sqlite3")
    _inspect_one(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(state_health.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        state_health,
        "_state_store_descriptors",
        lambda: (
            state_health._OwnerDescriptor("fixture", "owner.sqlite3", 1),
            state_health._OwnerDescriptor("next", "next.sqlite3", 1),
        ),
    )

    def validate(_connection: sqlite3.Connection) -> None:
        now[0] += 1.0

    monkeypatch.setattr(state_health, "_exact_validator", lambda *_args: validate)
    result = state_health.inspect_state_health(tmp_path, timeout_seconds=0.5)
    assert [owner.name for owner in result.owners] == ["fixture", "next"]
    assert [owner.status for owner in result.owners] == ["blocked", "blocked"]
