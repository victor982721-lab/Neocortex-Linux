"""Read-only CLI status for durable protected self-analysis manifests."""
# region [00] Contexto del módulo
# Módulo: tests/test_self_analysis_status.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from _01_Enumeracion import JournalCursor, JournalDiscontinuityError, VolumeAccessError
from _02_Deduplicacion import (
    DedupIndex,
    InventoryCheckpoint,
    InventoryExclusionPolicy,
)
from _04_Nucleo_Operativo import framework_schema
from _04_Nucleo_Operativo import self_analysis_status as status_module
from _04_Nucleo_Operativo.cli_app import dispatch_direct
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from _04_Nucleo_Operativo.code_contracts import (
    deep_configuration_payload,
    deep_configuration_signature,
)
from _04_Nucleo_Operativo.code_state import CodeState
from _04_Nucleo_Operativo.corpus_access import CorpusAccessPolicy
from _04_Nucleo_Operativo.framework_state_writer import FrameworkState
from _04_Nucleo_Operativo.self_analysis import (
    MAX_SELF_ANALYSIS_MANIFEST_BYTES,
    SELF_ANALYSIS_MANIFEST_MESSAGE,
    SELF_ANALYSIS_MANIFEST_PHASE,
    build_self_analysis_inventory_policy,
)
from _04_Nucleo_Operativo.self_analysis_status import (
    CodeRunStatusEvidence,
    JournalStatus,
    QuiescentSQLiteUnavailable,
    probe_self_analysis_journal,
    quiescent_sqlite_database,
    read_self_analysis_status,
)
from neocortex.cli import entrypoint
# endregion [01]

# region [02] Implementación


@dataclass(frozen=True, slots=True)
class _PreparedStatus:
    root: Path
    state: Path
    run_id: int
    scan_id: int
    analysis_run_id: int
    processing_signature: str

    @property
    def evidence(self) -> CodeRunStatusEvidence:
        return CodeRunStatusEvidence(
            self.analysis_run_id,
            self.run_id,
            self.scan_id,
            self.processing_signature,
            "completed",
        )


def _commands(root: Path, state: Path) -> dict[str, list[str]]:
    return {
        "analyze": [
            "Neocortex",
            "--self-analysis",
            "--root",
            str(root),
            "--state-directory",
            str(state),
        ],
        "status": [
            "Neocortex",
            "--state-directory",
            str(state),
            "--code-status",
            "--code-json",
        ],
    }


def _append_completed_run(
    prepared: _PreparedStatus,
    *,
    cursor: JournalCursor,
) -> _PreparedStatus:
    inventory_policy = build_self_analysis_inventory_policy(prepared.root, prepared.state)
    access_policy = CorpusAccessPolicy.capture("analyze_only", prepared.root)
    with FrameworkState(prepared.state / "framework.sqlite3") as framework:
        run_id = framework.begin_self_analysis_run(
            access_policy,
            cursor,
            state_directory=prepared.state,
            inventory_policy_signature=inventory_policy.signature,
        )
        assert framework.publish_initial_routing_snapshot(
            run_id,
            prepared.scan_id,
            0,
            1,
            "full",
            0,
        )
        framework.begin_route_runs(run_id, ("code",))
        framework.complete_route_run(
            run_id,
            "code",
            {
                "processed": 1,
                "processing_signature": prepared.processing_signature,
            },
        )
        framework.complete_self_analysis_run(
            run_id,
            cursor,
            inventory_policy=inventory_policy,
            code_processing_signature=prepared.processing_signature,
            commands=_commands(prepared.root, prepared.state),
        )
    with CodeState(prepared.state / "code.sqlite3") as code:
        analysis_run_id = code.begin_run(
            run_id,
            prepared.scan_id,
            prepared.processing_signature,
        )
        code.complete_run(
            analysis_run_id,
            {"candidates": 1, "processed": 1, "cache_hits": 0, "errors": 0},
            partial=False,
        )
    return _PreparedStatus(
        prepared.root,
        prepared.state,
        run_id,
        prepared.scan_id,
        analysis_run_id,
        prepared.processing_signature,
    )


@pytest.fixture
def prepared_status(tmp_path: Path) -> _PreparedStatus:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    inventory_policy = build_self_analysis_inventory_policy(root, state)
    cursor = JournalCursor(root.drive or root.anchor, 7, 100)
    with DedupIndex(state / "dedup.sqlite3") as inventory:
        scan = inventory.scan(root, exclusion_policy=inventory_policy)
        inventory.bind_inventory_checkpoint(
            InventoryCheckpoint(
                str(root),
                scan.scan_id,
                cursor.volume,
                cursor.journal_id,
                cursor.next_usn,
            )
        )
    seed = _PreparedStatus(root, state, 0, scan.scan_id, 0, "code-v2:test")
    return _append_completed_run(seed, cursor=cursor)


def _validated_status(state: Path):
    args = build_parser().parse_args(
        ("--state-directory", str(state), "--code-status", "--code-json")
    )
    validate_arguments(args)
    return args


def _set_unchanged_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status_module,
        "probe_self_analysis_journal",
        lambda _cursor: "unchanged",
    )


def _publish_trusted_deep_manifest(prepared: _PreparedStatus) -> dict[str, object]:
    database = prepared.state / "framework.sqlite3"
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase=? AND message=?""",
            (
                prepared.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        manifest = json.loads(row[0])
        selectors = ("tests/test_self_analysis_status.py",)
        deep = deep_configuration_payload(
            analysis_profile="trusted-deep",
            test_selectors=selectors,
            max_tests=120,
            time_budget_seconds=240,
            shard_size=12,
        )
        manifest["commands"]["analyze"].extend(
            (
                "--analysis-profile",
                "trusted-deep",
                "--deep-test-selector",
                selectors[0],
                "--deep-max-tests",
                "120",
                "--deep-time-budget-seconds",
                "240",
                "--deep-shard-size",
                "12",
                "--deep-mutation-max-mutants",
                "20",
                "--deep-mutation-timeout-seconds",
                "30",
                "--deep-mutation-time-budget-seconds",
                "600",
            )
        )
        manifest["deep_analysis"] = {
            **deep,
            "configuration_signature": deep_configuration_signature(deep),
        }
        raw = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            """UPDATE run_events SET details_json=?
            WHERE run_id=? AND phase=? AND message=?""",
            (
                raw,
                prepared.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        )
        connection.commit()
        return manifest
    finally:
        connection.close()


def _publish_many_selector_manifest(
    prepared: _PreparedStatus,
    *,
    selector_count: int,
) -> dict[str, object]:
    """Publish lexical manifest evidence for a realistically wide selected suite."""

    database = prepared.state / "framework.sqlite3"
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase=? AND message=?""",
            (
                prepared.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        manifest = json.loads(row[0])
        selectors = tuple(f"tests/test_selected_{index:04d}.py" for index in range(selector_count))
        analyze = list(manifest["commands"]["analyze"])
        analyze.extend(("--analysis-profile", "trusted-deep"))
        for selector in selectors:
            analyze.extend(("--deep-test-selector", selector))
        analyze.extend(
            (
                "--deep-max-tests",
                "3000",
                "--deep-time-budget-seconds",
                "900",
                "--deep-shard-size",
                "20",
                "--deep-mutation-max-mutants",
                "20",
                "--deep-mutation-timeout-seconds",
                "30",
                "--deep-mutation-time-budget-seconds",
                "600",
            )
        )
        deep = deep_configuration_payload(
            analysis_profile="trusted-deep",
            test_selectors=selectors,
            max_tests=3000,
            time_budget_seconds=900,
            shard_size=20,
        )
        manifest["commands"]["analyze"] = analyze
        manifest["deep_analysis"] = {
            **deep,
            "configuration_signature": deep_configuration_signature(deep),
        }
        raw = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            """UPDATE run_events SET details_json=?
            WHERE run_id=? AND phase=? AND message=?""",
            (
                raw,
                prepared.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        )
        connection.commit()
        return manifest
    finally:
        connection.close()


def _prepare_unavailable_status(tmp_path: Path) -> _PreparedStatus:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    inventory_policy = build_self_analysis_inventory_policy(root, state)
    with DedupIndex(state / "dedup.sqlite3") as inventory:
        scan = inventory.scan(root, exclusion_policy=inventory_policy)
    access_policy = CorpusAccessPolicy.capture("analyze_only", root)
    processing_signature = "code-v2:journal-unavailable"
    with FrameworkState(state / "framework.sqlite3") as framework:
        run_id = framework.begin_self_analysis_run(
            access_policy,
            None,
            state_directory=state,
            inventory_policy_signature=inventory_policy.signature,
        )
        assert framework.publish_initial_routing_snapshot(
            run_id,
            scan.scan_id,
            0,
            1,
            "full",
            0,
        )
        framework.begin_route_runs(run_id, ("code",))
        framework.complete_route_run(
            run_id,
            "code",
            {"processed": 1, "processing_signature": processing_signature},
        )
        framework.complete_self_analysis_run(
            run_id,
            None,
            inventory_policy=inventory_policy,
            code_processing_signature=processing_signature,
            commands=_commands(root, state),
        )
    with CodeState(state / "code.sqlite3") as code:
        analysis_run_id = code.begin_run(run_id, scan.scan_id, processing_signature)
        code.complete_run(
            analysis_run_id,
            {"candidates": 1, "processed": 1, "cache_hits": 0, "errors": 0},
            partial=False,
        )
    return _PreparedStatus(
        root,
        state,
        run_id,
        scan.scan_id,
        analysis_run_id,
        processing_signature,
    )


def _state_file_snapshot(state: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(state.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        metadata = path.stat()
        try:
            contents: bytes | None = path.read_bytes()
        except PermissionError:
            contents = None
        snapshot[path.name] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            getattr(metadata, "st_birthtime_ns", None),
            getattr(metadata, "st_file_attributes", None),
            contents,
        )
    return snapshot


def test_journal_free_manifest_is_valid_but_never_claimed_current(
    tmp_path: Path,
) -> None:
    prepared = _prepare_unavailable_status(tmp_path)

    def unexpected_probe(_cursor: JournalCursor) -> JournalStatus:
        raise AssertionError("an unavailable journal must not be probed")

    result = read_self_analysis_status(
        prepared.state,
        prepared.evidence,
        journal_probe=unexpected_probe,
    )

    assert result is not None
    assert result.manifest_status == "valid"
    assert result.manifest is not None
    assert result.manifest["inventory"]["journal"] == {  # type: ignore[index]
        "status": "unavailable"
    }
    assert result.freshness.as_payload() == {
        "root_identity_current": True,
        "framework_link_current": True,
        "inventory_checkpoint_current": False,
        "journal_status": "unavailable",
        "current": False,
    }


@pytest.mark.parametrize(
    ("schema", "birthtime_ns", "valid"),
    (
        ("neocortex.self-analysis-manifest/v2", -2, False),
        ("neocortex.self-analysis-manifest/v2", -1, True),
        ("neocortex.self-analysis-manifest/v2", 0, True),
        ("neocortex.self-analysis-manifest/v2", 987_654_321, True),
        ("neocortex.self-analysis-manifest/v2", True, False),
        ("neocortex.self-analysis-manifest/v1", -1, False),
        ("neocortex.self-analysis-manifest/v1", 0, True),
    ),
)
def test_manifest_root_birthtime_domain_is_schema_compatible(
    tmp_path: Path,
    schema: str,
    birthtime_ns: object,
    valid: bool,
) -> None:
    prepared_status = _prepare_unavailable_status(tmp_path)
    database = prepared_status.state / "framework.sqlite3"
    with quiescent_sqlite_database(database) as connection:
        row = connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase=? AND message=?""",
            (
                prepared_status.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        ).fetchone()
    assert row is not None and isinstance(row[0], str)
    manifest = json.loads(row[0])
    manifest["schema"] = schema
    manifest["run"]["root_identity"]["birthtime_ns"] = birthtime_ns
    if schema == "neocortex.self-analysis-manifest/v1":
        manifest["inventory"]["journal"] = {
            "volume": "C:",
            "journal_id": "7",
            "start_usn": 0,
            "end_usn": 100,
        }
    raw = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    if valid:
        decoded = status_module._decode_manifest(raw, len(raw.encode("utf-8")))
        assert decoded["run"]["root_identity"]["birthtime_ns"] == birthtime_ns  # type: ignore[index]
    else:
        with pytest.raises(ValueError, match="manifest root birthtime"):
            status_module._decode_manifest(raw, len(raw.encode("utf-8")))


def test_code_status_json_exposes_valid_current_manifest_read_only(
    prepared_status: _PreparedStatus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_unchanged_journal(monkeypatch)
    databases = tuple(
        prepared_status.state / name
        for name in ("code.sqlite3", "framework.sqlite3", "dedup.sqlite3")
    )
    for database in databases:
        connection = sqlite3.connect(database)
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    assert not any(
        Path(f"{database}{suffix}").exists()
        for database in databases
        for suffix in ("-wal", "-shm", "-journal")
    )
    before = _state_file_snapshot(prepared_status.state)

    assert dispatch_direct(_validated_status(prepared_status.state)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["latest_run"]["scan_id"] == prepared_status.scan_id
    assert payload["latest_run"]["processing_signature"] == "code-v2:test"
    self_analysis = payload["self_analysis"]
    assert self_analysis["manifest_status"] == "valid"
    assert self_analysis["manifest"]["run"]["run_id"] == prepared_status.run_id
    policy = self_analysis["manifest"]["inventory"]["policy"]
    assert policy["directory_prefixes"]
    assert policy["directory_fragments"]
    assert self_analysis["manifest"]["safety"] == {
        "route_candidates": 0,
        "file_actions": 0,
        "run_actions": 0,
        "organization_events": 0,
    }
    assert (
        self_analysis["manifest"]["commands"]["status"]
        == _commands(prepared_status.root, prepared_status.state)["status"]
    )
    assert self_analysis["freshness"] == {
        "root_identity_current": True,
        "framework_link_current": True,
        "inventory_checkpoint_current": True,
        "journal_status": "unchanged",
        "current": True,
    }
    assert _state_file_snapshot(prepared_status.state) == before


def test_trusted_deep_manifest_is_valid_and_bound_to_recorded_argv(
    prepared_status: _PreparedStatus,
) -> None:
    expected = _publish_trusted_deep_manifest(prepared_status)

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None
    assert result.manifest_status == "valid"
    assert result.manifest == expected
    assert result.freshness.current


def test_trusted_deep_manifest_accepts_a_wide_selected_suite_command(
    prepared_status: _PreparedStatus,
) -> None:
    expected = _publish_many_selector_manifest(prepared_status, selector_count=298)

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None
    assert result.manifest_status == "valid"
    assert result.manifest == expected
    assert len(expected["commands"]["analyze"]) > 128
    assert result.freshness.current


@pytest.mark.parametrize("mutation", ("missing", "signature", "limits"))
def test_trusted_deep_manifest_inconsistency_is_structured_invalid(
    prepared_status: _PreparedStatus,
    mutation: str,
) -> None:
    manifest = _publish_trusted_deep_manifest(prepared_status)
    if mutation == "missing":
        manifest.pop("deep_analysis")
    elif mutation == "signature":
        manifest["deep_analysis"]["configuration_signature"] = "code-deep-v1:bad"
    else:
        manifest["deep_analysis"]["max_tests"] = 121
    raw = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection = sqlite3.connect(prepared_status.state / "framework.sqlite3")
    try:
        connection.execute(
            """UPDATE run_events SET details_json=?
            WHERE run_id=? AND phase=? AND message=?""",
            (
                raw,
                prepared_status.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None
    assert result.manifest_status == "invalid"
    assert result.manifest is None
    assert not result.freshness.current


@pytest.mark.parametrize(
    "database_name",
    ("code.sqlite3", "framework.sqlite3", "dedup.sqlite3"),
)
def test_code_status_accepts_exact_inactive_wal_shm_without_touching_state(
    prepared_status: _PreparedStatus,
    database_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_unchanged_journal(monkeypatch)
    database = prepared_status.state / database_name
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        writer.execute("BEGIN IMMEDIATE")
        wal = Path(f"{database}-wal")
        shm = Path(f"{database}-shm")
        assert wal.is_file() and wal.stat().st_size == 0
        assert shm.is_file() and shm.stat().st_size > 0
        before = _state_file_snapshot(prepared_status.state)

        with quiescent_sqlite_database(database) as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert dispatch_direct(_validated_status(prepared_status.state)) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["latest_run"]["analysis_run_id"] == prepared_status.analysis_run_id
        assert payload["self_analysis"]["manifest_status"] == "valid"
        assert payload["self_analysis"]["freshness"]["current"] is True
        assert _state_file_snapshot(prepared_status.state) == before
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize(
    "database_name",
    ("code.sqlite3", "framework.sqlite3", "dedup.sqlite3"),
)
def test_code_status_accepts_detached_inactive_wal_and_shm(
    prepared_status: _PreparedStatus,
    database_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_unchanged_journal(monkeypatch)
    database = prepared_status.state / database_name
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    wal.write_bytes(b"")
    shm.write_bytes(bytes(32_768))
    before = _state_file_snapshot(prepared_status.state)

    assert dispatch_direct(_validated_status(prepared_status.state)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["latest_run"]["analysis_run_id"] == prepared_status.analysis_run_id
    assert payload["self_analysis"]["manifest_status"] == "valid"
    assert payload["self_analysis"]["freshness"]["current"] is True
    assert _state_file_snapshot(prepared_status.state) == before


@pytest.mark.parametrize(
    "database_name",
    ("code.sqlite3", "framework.sqlite3", "dedup.sqlite3"),
)
def test_code_status_rejects_nonempty_wal_without_touching_state(
    prepared_status: _PreparedStatus,
    database_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = prepared_status.state / database_name
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE sidecar_probe(value INTEGER)")
        writer.commit()
        wal = Path(f"{database}-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        before = _state_file_snapshot(prepared_status.state)

        with pytest.raises(QuiescentSQLiteUnavailable, match="non-empty WAL"):
            with quiescent_sqlite_database(database):
                pytest.fail("an unpublished WAL must prevent immutable status")
        assert dispatch_direct(_validated_status(prepared_status.state)) == 2
        captured = capsys.readouterr()

        assert not captured.out
        assert "non-empty WAL" in captured.err
        assert _state_file_snapshot(prepared_status.state) == before
    finally:
        writer.close()


def test_code_status_rejects_malformed_inactive_sidecars(
    prepared_status: _PreparedStatus,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = prepared_status.state / "code.sqlite3"
    Path(f"{database}-wal").write_bytes(b"")
    Path(f"{database}-shm").write_bytes(b"not-a-valid-shm")
    before = _state_file_snapshot(prepared_status.state)

    assert (
        entrypoint(
            (
                "--state-directory",
                str(prepared_status.state),
                "--code-status",
                "--code-json",
            )
        )
        == 2
    )
    captured = capsys.readouterr()

    assert not captured.out
    assert "sidecars are not proven inactive" in captured.err
    assert _state_file_snapshot(prepared_status.state) == before


def test_historical_manifests_are_not_ambiguous(
    prepared_status: _PreparedStatus,
) -> None:
    newest = _append_completed_run(
        prepared_status,
        cursor=JournalCursor(
            prepared_status.root.drive or prepared_status.root.anchor,
            7,
            100,
        ),
    )

    result = read_self_analysis_status(
        newest.state,
        newest.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None
    assert result.manifest_status == "valid"
    assert result.manifest is not None
    assert result.manifest["run"]["run_id"] == newest.run_id  # type: ignore[index]
    assert result.freshness.current


@pytest.mark.parametrize("terminal_status", (None, "failed"))
def test_latest_code_link_never_falls_back_to_historical_manifest(
    prepared_status: _PreparedStatus,
    terminal_status: str | None,
) -> None:
    cursor = JournalCursor(
        prepared_status.root.drive or prepared_status.root.anchor,
        7,
        100,
    )
    policy = CorpusAccessPolicy.capture("analyze_only", prepared_status.root)
    inventory_policy = build_self_analysis_inventory_policy(
        prepared_status.root, prepared_status.state
    )
    with FrameworkState(prepared_status.state / "framework.sqlite3") as framework:
        run_id = framework.begin_self_analysis_run(
            policy,
            cursor,
            state_directory=prepared_status.state,
            inventory_policy_signature=inventory_policy.signature,
        )
        if terminal_status == "failed":
            framework.fail_initial_run(run_id)
    with CodeState(prepared_status.state / "code.sqlite3") as code:
        analysis_run_id = code.begin_run(
            run_id,
            prepared_status.scan_id,
            prepared_status.processing_signature,
        )
        if terminal_status == "failed":
            code.fail_run(analysis_run_id, RuntimeError("injected failure"))
    evidence = CodeRunStatusEvidence(
        analysis_run_id,
        run_id,
        prepared_status.scan_id,
        prepared_status.processing_signature,
        "running" if terminal_status is None else terminal_status,
    )

    result = read_self_analysis_status(
        prepared_status.state,
        evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None
    assert result.manifest_status == "missing"
    assert result.manifest is None
    assert not result.freshness.current


def test_newer_framework_self_analysis_breaks_historical_current_fence(
    prepared_status: _PreparedStatus,
) -> None:
    cursor = JournalCursor(
        prepared_status.root.drive or prepared_status.root.anchor,
        7,
        100,
    )
    policy = CorpusAccessPolicy.capture("analyze_only", prepared_status.root)
    inventory_policy = build_self_analysis_inventory_policy(
        prepared_status.root, prepared_status.state
    )
    with FrameworkState(prepared_status.state / "framework.sqlite3") as framework:
        newer_run_id = framework.begin_self_analysis_run(
            policy,
            cursor,
            state_directory=prepared_status.state,
            inventory_policy_signature=inventory_policy.signature,
        )

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert newer_run_id > prepared_status.run_id
    assert result is not None and result.manifest_status == "valid"
    assert result.manifest is not None
    assert result.manifest["run"]["run_id"] == prepared_status.run_id  # type: ignore[index]
    assert not result.freshness.framework_link_current
    assert not result.freshness.current


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    (
        ("missing", "missing"),
        ("duplicate", "ambiguous"),
        ("malformed", "invalid"),
        ("oversized", "invalid"),
    ),
)
def test_manifest_event_is_unique_and_bounded(
    prepared_status: _PreparedStatus,
    mutation: str,
    expected_status: str,
) -> None:
    database = prepared_status.state / "framework.sqlite3"
    connection = sqlite3.connect(database)
    try:
        if mutation == "missing":
            connection.execute(
                "DELETE FROM run_events WHERE run_id=? AND phase=?",
                (prepared_status.run_id, SELF_ANALYSIS_MANIFEST_PHASE),
            )
        elif mutation == "duplicate":
            row = connection.execute(
                """SELECT occurred_ns,level,phase,message,details_json
                FROM run_events WHERE run_id=? AND phase=?""",
                (prepared_status.run_id, SELF_ANALYSIS_MANIFEST_PHASE),
            ).fetchone()
            assert row is not None
            connection.execute(
                """INSERT INTO run_events(
                run_id,occurred_ns,level,phase,message,details_json)
                VALUES(?,?,?,?,?,?)""",
                (prepared_status.run_id, *row),
            )
        elif mutation == "malformed":
            connection.execute(
                """UPDATE run_events SET details_json='{' WHERE run_id=?
                AND phase=? AND message=?""",
                (
                    prepared_status.run_id,
                    SELF_ANALYSIS_MANIFEST_PHASE,
                    SELF_ANALYSIS_MANIFEST_MESSAGE,
                ),
            )
        else:
            connection.execute(
                """UPDATE run_events SET details_json=? WHERE run_id=?
                AND phase=? AND message=?""",
                (
                    "x" * (MAX_SELF_ANALYSIS_MANIFEST_BYTES + 1),
                    prepared_status.run_id,
                    SELF_ANALYSIS_MANIFEST_PHASE,
                    SELF_ANALYSIS_MANIFEST_MESSAGE,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None
    assert result.manifest_status == expected_status
    assert result.manifest is None
    assert not result.freshness.current


@pytest.mark.parametrize("mutation", ("device_identity", "file_identity", "cursor"))
def test_corrupt_manifest_evidence_is_structured_invalid_in_cli(
    prepared_status: _PreparedStatus,
    mutation: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = prepared_status.state / "framework.sqlite3"
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase=? AND message=?""",
            (
                prepared_status.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        manifest = json.loads(row[0])
        if mutation in {"device_identity", "file_identity"}:
            trigger = connection.execute(
                """SELECT sql FROM sqlite_schema
                WHERE type='trigger'
                AND name='initial_runs_corpus_policy_no_update'"""
            ).fetchone()
            assert trigger is not None and isinstance(trigger[0], str)
            connection.execute("DROP TRIGGER initial_runs_corpus_policy_no_update")
            if mutation == "device_identity":
                manifest["run"]["root_identity"]["device_id_hex"] = "-1"
                connection.execute(
                    "UPDATE initial_runs SET root_device_id_hex='-1' WHERE run_id=?",
                    (prepared_status.run_id,),
                )
            else:
                manifest["run"]["root_identity"]["file_id_hex"] = "-1"
                connection.execute(
                    "UPDATE initial_runs SET root_file_id_hex='-1' WHERE run_id=?",
                    (prepared_status.run_id,),
                )
            connection.execute(trigger[0])
        else:
            manifest["inventory"]["journal"]["journal_id"] = "01"
        connection.execute(
            """UPDATE run_events SET details_json=?
            WHERE run_id=? AND phase=? AND message=?""",
            (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                prepared_status.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    assert dispatch_direct(_validated_status(prepared_status.state)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["self_analysis"]["manifest_status"] == "invalid"
    assert payload["self_analysis"]["manifest"] is None
    assert not payload["self_analysis"]["freshness"]["current"]


@pytest.mark.parametrize("column", ("valid", "scan_id", "volume", "journal_id", "next_usn"))
def test_checkpoint_mismatch_never_claims_freshness(
    prepared_status: _PreparedStatus,
    column: str,
) -> None:
    replacements: dict[str, object] = {
        "valid": 0,
        "scan_id": prepared_status.scan_id + 1,
        "volume": "Z:",
        "journal_id": "8",
        "next_usn": 101,
    }
    connection = sqlite3.connect(prepared_status.state / "dedup.sqlite3")
    try:
        if column == "scan_id":
            connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            f"UPDATE inventory_checkpoints SET {column}=? WHERE root=?",
            (replacements[column], str(prepared_status.root)),
        )
        connection.commit()
    finally:
        connection.close()

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None and result.manifest_status == "valid"
    assert not result.freshness.inventory_checkpoint_current
    assert not result.freshness.current


def test_advanced_volume_journal_is_not_claimed_current(
    prepared_status: _PreparedStatus,
) -> None:
    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "advanced",
    )

    assert result is not None and result.manifest_status == "valid"
    assert result.freshness.root_identity_current
    assert result.freshness.framework_link_current
    assert result.freshness.inventory_checkpoint_current
    assert result.freshness.journal_status == "advanced"
    assert not result.freshness.current


def test_code_processing_signature_mismatch_breaks_only_framework_link(
    prepared_status: _PreparedStatus,
) -> None:
    mismatched = CodeRunStatusEvidence(
        prepared_status.analysis_run_id,
        prepared_status.run_id,
        prepared_status.scan_id,
        "code-v2:different",
        "completed",
    )

    result = read_self_analysis_status(
        prepared_status.state,
        mismatched,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None and result.manifest_status == "valid"
    assert not result.freshness.framework_link_current
    assert result.freshness.root_identity_current
    assert result.freshness.inventory_checkpoint_current
    assert not result.freshness.current


def test_current_policy_signature_must_match_recorded_policy(
    prepared_status: _PreparedStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed_policy = InventoryExclusionPolicy.compile(
        (prepared_status.state,),
        directory_names=("new-profile-rule",),
    )
    monkeypatch.setattr(
        status_module,
        "build_self_analysis_inventory_policy",
        lambda _root, _state: changed_policy,
    )

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None and result.manifest_status == "valid"
    assert not result.freshness.inventory_checkpoint_current
    assert not result.freshness.current


def test_root_identity_probe_fails_closed(
    prepared_status: _PreparedStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_policy: CorpusAccessPolicy) -> None:
        raise OSError("injected root identity failure")

    monkeypatch.setattr(CorpusAccessPolicy, "verify_root_identity", unavailable)

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None and result.manifest_status == "valid"
    assert not result.freshness.root_identity_current
    assert not result.freshness.current


def test_checkpoint_change_during_probe_fails_closed(
    prepared_status: _PreparedStatus,
) -> None:
    def advance_checkpoint(_cursor: JournalCursor) -> JournalStatus:
        connection = sqlite3.connect(prepared_status.state / "dedup.sqlite3")
        try:
            connection.execute(
                "UPDATE inventory_checkpoints SET next_usn=next_usn+1 WHERE root=?",
                (str(prepared_status.root),),
            )
            connection.commit()
        finally:
            connection.close()
        return "unchanged"

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=advance_checkpoint,
    )

    assert result is not None and result.manifest_status == "valid"
    assert not result.freshness.inventory_checkpoint_current
    assert not result.freshness.current


def test_latest_normal_code_run_keeps_self_analysis_addition_null(
    prepared_status: _PreparedStatus,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cursor = JournalCursor(
        prepared_status.root.drive or prepared_status.root.anchor,
        7,
        100,
    )
    with FrameworkState(prepared_status.state / "framework.sqlite3") as framework:
        normal_run_id = framework.begin_initial_run(prepared_status.root, cursor)
    with CodeState(prepared_status.state / "code.sqlite3") as code:
        analysis_run_id = code.begin_run(
            normal_run_id,
            prepared_status.scan_id,
            prepared_status.processing_signature,
        )
        code.complete_run(analysis_run_id, {}, partial=False)

    assert dispatch_direct(_validated_status(prepared_status.state)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["latest_run"]["framework_run_id"] == normal_run_id
    assert payload["self_analysis"] is None


@pytest.mark.parametrize(
    ("reader_result", "error", "expected"),
    (
        (None, None, "unchanged"),
        (object(), None, "advanced"),
        (None, JournalDiscontinuityError("lost"), "discontinuous"),
        (
            None,
            VolumeAccessError("probe", "C:", 5, "denied"),
            "unavailable",
        ),
    ),
)
def test_journal_probe_classifies_one_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
    reader_result: object | None,
    error: BaseException | None,
    expected: str,
) -> None:
    class _Reader:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            if error is not None:
                raise error
            return self

        def __exit__(self, *_args) -> None:
            pass

        def poll(self):
            return reader_result

    monkeypatch.setattr(status_module, "UsnJournalReader", _Reader)

    assert probe_self_analysis_journal(JournalCursor("C:", 7, 100)) == expected


def test_legacy_framework_is_not_migrated_or_initialized(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code_database = state / "code.sqlite3"
    with CodeState(code_database) as code:
        analysis_run_id = code.begin_run(1, 1, "code-v2:test")
        code.complete_run(analysis_run_id, {}, partial=False)
    framework_database = state / "framework.sqlite3"
    with sqlite3.connect(framework_database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version','19');
            CREATE TABLE initial_runs(
                run_id INTEGER PRIMARY KEY,
                run_kind TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO initial_runs VALUES(1,'self_analysis','completed');
            CREATE TABLE run_events(
                event_id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                occurred_ns INTEGER NOT NULL,
                level TEXT NOT NULL,
                phase TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT
            );
            """
        )
    before = framework_database.read_bytes()

    result = read_self_analysis_status(
        state,
        CodeRunStatusEvidence(analysis_run_id, 1, 1, "code-v2:test", "completed"),
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is None
    assert framework_database.read_bytes() == before
    with sqlite3.connect(framework_database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("19",)
    assert not (state / "dedup.sqlite3").exists()


def test_future_framework_schema_returns_structured_invalid_without_mutation(
    prepared_status: _PreparedStatus,
) -> None:
    database = prepared_status.state / "framework.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(framework_schema.SCHEMA_VERSION + 1),),
        )
        connection.commit()
    finally:
        connection.close()
    before_bytes = database.read_bytes()
    before_names = {path.name for path in prepared_status.state.iterdir()}

    result = read_self_analysis_status(
        prepared_status.state,
        prepared_status.evidence,
        journal_probe=lambda _cursor: "unchanged",
    )

    assert result is not None
    assert result.manifest_status == "invalid"
    assert result.manifest is None
    assert not result.freshness.current
    assert database.read_bytes() == before_bytes
    assert {path.name for path in prepared_status.state.iterdir()} == before_names
    with quiescent_sqlite_database(database) as readonly:
        row = readonly.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        assert row is not None
        assert row[0] == str(framework_schema.SCHEMA_VERSION + 1)


def test_historical_manifest_decode_does_not_probe_filesystem(
    prepared_status: _PreparedStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = prepared_status.state / "framework.sqlite3"
    with quiescent_sqlite_database(database) as connection:
        row = connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase=? AND message=?""",
            (
                prepared_status.run_id,
                SELF_ANALYSIS_MANIFEST_PHASE,
                SELF_ANALYSIS_MANIFEST_MESSAGE,
            ),
        ).fetchone()
    assert row is not None and isinstance(row[0], str)
    manifest = json.loads(row[0])
    manifest["schema"] = "neocortex.self-analysis-manifest/v1"
    manifest["run"]["root_identity"]["birthtime_ns"] = 0
    manifest["inventory"]["journal"] = {
        "volume": "C:",
        "journal_id": "7",
        "start_usn": 0,
        "end_usn": 100,
    }
    raw = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    def unexpected_realpath(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("historical decode must not resolve live paths")

    monkeypatch.setattr(status_module.os.path, "realpath", unexpected_realpath)

    decoded = status_module._decode_manifest(raw, len(raw.encode("utf-8")))

    assert decoded["schema"] == "neocortex.self-analysis-manifest/v1"
    assert decoded["run"]["run_id"] == prepared_status.run_id  # type: ignore[index]


# endregion [02]
