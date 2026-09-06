from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.persistence.sqlite_immutable import capture_sqlite_read_fence
from neocortex.workflow import state_health


def _fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, count: int = 1
) -> tuple[state_health._OwnerDescriptor, ...]:
    descriptors = tuple(
        state_health._OwnerDescriptor(f"fixture{index:02}", f"owner{index:02}.sqlite3", 1)
        for index in range(count)
    )
    for descriptor in descriptors:
        with closing(sqlite3.connect(tmp_path / descriptor.filename)) as connection, connection:
            connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO metadata VALUES(?, ?)",
                (("schema_version", "1"), ("fixture", descriptor.name)),
            )
    monkeypatch.setattr(state_health, "_state_store_descriptors", lambda: descriptors)
    monkeypatch.setattr(state_health, "_exact_validator", lambda *_args: lambda _connection: None)
    monkeypatch.setattr(state_health, "_unknown_state_entries", lambda _path: ())
    return descriptors


def _fingerprint(tmp_path: Path, descriptors: tuple[state_health._OwnerDescriptor, ...]):
    return tuple(
        (capture_sqlite_read_fence(tmp_path / item.filename), (tmp_path / item.filename).read_bytes())
        for item in descriptors
    )


@pytest.mark.parametrize(
    ("scope", "status", "checks"),
    [
        ("compatibility", "metadata_compatible", ("metadata",)),
        ("integrity", "integrity_verified", ("metadata", "quick_integrity", "fts")),
        ("referential", "referential_verified", ("metadata", "foreign_keys", "exact_schema")),
        (
            "full", "healthy",
            ("metadata", "quick_integrity", "foreign_keys", "fts", "exact_schema", "status_observations"),
        ),
    ],
)
def test_scope_runs_only_its_checks_and_never_claims_unchecked_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, status: str, checks: tuple[str, ...]
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch)
    before = _fingerprint(tmp_path, descriptors)
    calls: list[str] = []

    def check(name: str):
        def run(*_args: object) -> None:
            calls.append(name)
            assert name in checks, f"scope {scope} invoked unrequested stage {name}"

        return run

    monkeypatch.setattr(state_health, "_check_quick_integrity", check("quick_integrity"))
    monkeypatch.setattr(state_health, "_check_foreign_keys", check("foreign_keys"))
    monkeypatch.setattr(state_health, "_check_fts", check("fts"))
    monkeypatch.setattr(state_health, "_exact_validator", lambda *_args: check("exact_schema"))

    def observations(*_args: object) -> dict[str, dict[str, int]]:
        check("status_observations")()
        return {}

    monkeypatch.setattr(state_health, "_status_observations", observations)
    result = state_health.inspect_state_health(tmp_path, scope=scope)
    owner = result.owners[0]
    assert owner.status == status
    assert owner.checks_completed == owner.checks_attempted == checks
    assert calls == list(checks[1:])
    assert owner.checks_not_run == tuple(check for check in state_health._SCOPE_CHECKS["full"] if check not in checks)
    assert result.scope_complete and result.scope_complete_count == 1
    assert result.healthy_count == int(scope == "full")
    assert result.overall == ("healthy" if scope == "full" else "partial")
    payload = result.to_dict()
    assert payload["schema_version"] == 2
    assert payload["scope"] == scope
    assert payload["coverage"]["checks_requested"] == list(checks)
    assert payload["coverage"]["budget_kind"] == "cooperative"
    assert _fingerprint(tmp_path, descriptors) == before


def test_compatibility_does_not_load_exact_validator_or_collect_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixtures(tmp_path, monkeypatch)

    def forbidden(*_args: object) -> None:
        pytest.fail("lightweight inspection cannot load owner validators or inspect status rows")

    monkeypatch.setattr(state_health, "_exact_validator", forbidden)
    monkeypatch.setattr(state_health, "_status_observations", forbidden)
    result = state_health.inspect_state_health(tmp_path, scope="compatibility")
    assert result.owners[0].status == "metadata_compatible"
    assert "exact_schema" in result.owners[0].checks_not_run
    assert result.overall != "healthy"


def test_fair_thirty_second_budget_reaches_24_light_owners_after_expensive_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch, count=25)
    before = _fingerprint(tmp_path, descriptors)
    now = [100.0]
    monkeypatch.setattr(state_health.time, "monotonic", lambda: now[0])
    checked: list[str] = []

    def foreign_keys(connection: sqlite3.Connection) -> None:
        name = str(connection.execute("SELECT value FROM metadata WHERE key='fixture'").fetchone()[0])
        checked.append(name)
        # The first owner exceeds its 30/25 second slice but not the global
        # budget, so its overrun cannot starve the remaining light owners.
        now[0] += 1.21 if name == "fixture00" else 0.001

    monkeypatch.setattr(state_health, "_check_foreign_keys", foreign_keys)
    result = state_health.inspect_state_health(tmp_path)
    assert tuple(checked) == tuple(item.name for item in descriptors)
    assert result.owners[0].status == "not_verified"
    assert result.owners[0].checks_completed == ("metadata", "quick_integrity")
    assert result.owners[0].checks_attempted[-1] == "foreign_keys"
    assert "foreign_keys" not in result.owners[0].checks_not_run
    assert [owner.status for owner in result.owners[1:]] == ["healthy"] * 24
    assert result.retry_owners == ("fixture00",)
    assert result.not_verified_count == 1
    assert result.scope_complete_count == 24
    assert result.corrupt_count == result.blocked_count == 0
    assert now[0] < 130.0

    # Retry is fresh evidence, not a resume inside a connection or reuse of
    # the first attempt's metadata; selecting one owner gives it the budget.
    retried = state_health.inspect_state_health(tmp_path, owners=result.retry_owners)
    assert retried.owners[0].status == "healthy"
    assert retried.scope_complete
    assert retried.retry_owners == ()
    assert retried.overall == "partial"  # other owners were not reverified
    assert _fingerprint(tmp_path, descriptors) == before


def test_slow_sql_yields_slice_and_later_owner_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch, count=2)
    before = _fingerprint(tmp_path, descriptors)
    ordinary = state_health._check_foreign_keys

    def foreign_keys(connection: sqlite3.Connection) -> None:
        name = connection.execute("SELECT value FROM metadata WHERE key='fixture'").fetchone()[0]
        if name == "fixture00":
            connection.execute(
                "WITH RECURSIVE n(value) AS (VALUES(0) UNION ALL "
                "SELECT value+1 FROM n WHERE value < 50000000) SELECT sum(value) FROM n"
            ).fetchone()
            pytest.fail("the first owner's SQL must be cancelled at its fair deadline")
        ordinary(connection)

    monkeypatch.setattr(state_health, "_check_foreign_keys", foreign_keys)
    started = time.monotonic()
    result = state_health.inspect_state_health(tmp_path, timeout_seconds=0.2)
    assert time.monotonic() - started < 1.5
    assert [owner.status for owner in result.owners] == ["not_verified", "healthy"]
    assert result.corrupt_count == result.blocked_count == 0
    assert _fingerprint(tmp_path, descriptors) == before


@pytest.mark.parametrize("scope", state_health.HEALTH_SCOPES)
def test_continuation_visits_25_owners_once_in_stable_bounded_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch, count=25)
    before = _fingerprint(tmp_path, descriptors)
    visited: list[str] = []
    cursor: str | None = None
    for expected_size in (7, 7, 7, 4):
        result = state_health.inspect_state_health(
            tmp_path, scope=scope, max_owners=7, after_owner=cursor
        )
        assert len(result.owners) == expected_size
        assert result.selected_owners == tuple(owner.name for owner in result.owners)
        assert result.omitted_owners == tuple(visited)
        visited.extend(result.selected_owners)
        assert result.deferred_owners == tuple(item.name for item in descriptors[len(visited):])
        assert result.unknown_discovery == "not_requested"
        assert result.overall == "partial"
        assert result.scope_complete
        assert not result.retry_owners
        cursor = result.next_after_owner
        if result.deferred_owners:
            assert cursor == result.owners[-1].name
    assert cursor is None
    assert tuple(visited) == tuple(item.name for item in descriptors)
    assert _fingerprint(tmp_path, descriptors) == before


def test_selection_is_canonical_and_never_touches_omitted_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch, count=25)
    original = state_health._sidecars
    touched: list[str] = []

    def sidecars(path: Path):
        touched.append(path.name)
        return original(path)

    def no_discovery(_path: Path):
        pytest.fail("explicit selection must not enumerate unknown owners")

    monkeypatch.setattr(state_health, "_sidecars", sidecars)
    monkeypatch.setattr(state_health, "_unknown_state_entries", no_discovery)
    requested = ("fixture24", "fixture00", "fixture11")
    first = state_health.inspect_state_health(tmp_path, owners=requested, max_owners=2)
    assert first.selected_owners == ("fixture00", "fixture11")
    assert first.deferred_owners == ("fixture24",)
    assert first.next_after_owner == "fixture11"
    second = state_health.inspect_state_health(
        tmp_path, owners=requested, max_owners=2, after_owner=first.next_after_owner
    )
    assert second.selected_owners == ("fixture24",)
    assert second.next_after_owner is None
    assert set(touched) == {descriptors[index].filename for index in (0, 11, 24)}
    assert first.corrupt_count == second.corrupt_count == 0
    assert first.not_verified_count == second.not_verified_count == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "invalid"}, {"scope": None},
        {"owners": "fixture00"}, {"owners": []}, {"owners": ["missing"]},
        {"owners": ["../owner00.sqlite3"]}, {"owners": [None]},
        {"owners": ["unknown:owner00.sqlite3"]},
        {"owners": ["fixture00", "fixture00"]},
        {"max_owners": 0}, {"max_owners": True}, {"max_owners": 1.5},
        {"max_owners": state_health.MAX_INSPECTION_OWNER_BATCH + 1},
        {"after_owner": "missing"}, {"after_owner": "../fixture00"},
        {"after_owner": "fixture01", "owners": ["fixture00"]},
        {"timeout_seconds": True}, {"timeout_seconds": float("inf")},
    ],
)
def test_selection_is_fully_validated_before_resolving_or_touching_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object]
) -> None:
    _fixtures(tmp_path, monkeypatch, count=2)

    class ForbiddenPath:
        def __fspath__(self):
            pytest.fail("invalid selection must be rejected before resolving the target")

    with pytest.raises(ValueError, match="state-health"):
        state_health.inspect_state_health(ForbiddenPath(), **kwargs)


@pytest.mark.parametrize("scope", state_health.HEALTH_SCOPES)
def test_every_scope_preserves_sidecar_fail_closed_before_opening_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch)
    database = tmp_path / descriptors[0].filename
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"active-fixture-wal")
    before = database.read_bytes(), wal.read_bytes()

    def no_open(*_args: object, **_kwargs: object):
        pytest.fail("a lighter scope must not bypass immutable sidecar preflight")

    monkeypatch.setattr(state_health, "_health_read", no_open)
    monkeypatch.setattr(state_health, "_proc_processes", lambda *_args, **_kwargs: ())
    result = state_health.inspect_state_health(tmp_path, scope=scope)
    assert result.owners[0].status == "active"
    assert result.owners[0].checks_completed == ()
    assert result.scope_complete_count == 0
    assert result.corrupt_count == 0
    assert (database.read_bytes(), wal.read_bytes()) == before


def test_integrity_scope_does_not_imply_referential_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch)
    path = tmp_path / descriptors[0].filename
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE parents(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE children(parent_id INTEGER REFERENCES parents(id))")
        connection.execute("INSERT INTO children VALUES(42)")
    before = _fingerprint(tmp_path, descriptors)
    physical = state_health.inspect_state_health(tmp_path, scope="integrity")
    assert physical.owners[0].status == "integrity_verified"
    assert physical.overall == "partial"
    assert "foreign_keys" in physical.owners[0].checks_not_run
    referential = state_health.inspect_state_health(tmp_path, scope="referential")
    assert referential.owners[0].status == "corrupt"
    assert referential.owners[0].checks_completed == ("metadata",)
    assert referential.owners[0].checks_attempted == ("metadata", "foreign_keys")
    assert referential.corrupt_count == 1
    assert _fingerprint(tmp_path, descriptors) == before


def test_cursor_does_not_reuse_or_cache_evidence_from_a_prior_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch, count=2)
    first = state_health.inspect_state_health(tmp_path, max_owners=1)
    assert first.owners[0].status == "healthy"
    first_path = tmp_path / descriptors[0].filename
    with closing(sqlite3.connect(first_path)) as connection, connection:
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    next_page = state_health.inspect_state_health(
        tmp_path, max_owners=1, after_owner=first.next_after_owner
    )
    assert next_page.omitted_owners == (descriptors[0].name,)
    assert next_page.overall == "partial"
    repeated = state_health.inspect_state_health(tmp_path, max_owners=1)
    assert repeated.owners[0].status == "future"
    assert not repeated.scope_complete


def test_unknown_timeouts_are_not_offered_as_invalid_registry_retry_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch, count=2)
    before = _fingerprint(tmp_path, descriptors)
    monkeypatch.setattr(state_health, "_state_store_descriptors", lambda: descriptors[:1])
    monkeypatch.setattr(
        state_health, "_unknown_state_entries", lambda _path: (tmp_path / descriptors[1].filename,)
    )
    now = [100.0]
    monkeypatch.setattr(state_health.time, "monotonic", lambda: now[0])

    def check(connection: sqlite3.Connection) -> None:
        name = connection.execute("SELECT value FROM metadata WHERE key='fixture'").fetchone()[0]
        if name == descriptors[1].name:
            now[0] += 11.0

    monkeypatch.setattr(state_health, "_check_foreign_keys", check)
    result = state_health.inspect_state_health(tmp_path, timeout_seconds=10.0)
    assert [owner.status for owner in result.owners] == ["healthy", "not_verified"]
    assert result.retry_owners == ()
    assert result.retry_unknown_owners == ("unknown:owner01.sqlite3",)
    assert result.to_dict()["coverage"]["retry_unknown_owners"] == ["unknown:owner01.sqlite3"]
    assert result.corrupt_count == 0
    assert _fingerprint(tmp_path, descriptors) == before


@pytest.mark.parametrize("scope", state_health.HEALTH_SCOPES)
def test_future_schema_is_never_accepted_by_a_lighter_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    descriptors = _fixtures(tmp_path, monkeypatch)
    path = tmp_path / descriptors[0].filename
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    result = state_health.inspect_state_health(tmp_path, scope=scope)
    assert result.owners[0].status == "future"
    assert result.owners[0].checks_completed == ()
    assert result.owners[0].checks_attempted == ("metadata",)
    assert result.scope_complete_count == result.healthy_count == 0
    assert not result.scope_complete


def test_failed_unknown_discovery_preserves_known_results_without_full_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixtures(tmp_path, monkeypatch)

    def discovery(_path: Path):
        raise PermissionError("fixture directory cannot be enumerated")

    monkeypatch.setattr(state_health, "_unknown_state_entries", discovery)
    result = state_health.inspect_state_health(tmp_path)
    assert result.owners[0].status == "healthy"
    assert result.unknown_discovery == "not_verified"
    assert "PermissionError" in (result.unknown_discovery_detail or "")
    assert result.overall == "partial"
    assert not result.scope_complete
    assert result.corrupt_count == 0


def test_unknown_discovery_does_not_hide_a_failed_directory_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(_path: Path):
        raise PermissionError("fixture enumeration denied")

    monkeypatch.setattr(Path, "iterdir", unreadable)
    with pytest.raises(PermissionError, match="fixture enumeration denied"):
        state_health._unknown_state_entries(tmp_path)
