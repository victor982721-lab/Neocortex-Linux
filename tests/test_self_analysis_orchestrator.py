"""End-to-end contracts for protected code-only orchestration."""
# region [00] Contexto del módulo
# Módulo: tests/test_self_analysis_orchestrator.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import inspect
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import neocortex.runtime.orchestration.orchestrator as orchestrator_module
from neocortex.enumeration import VolumeAccessError
from neocortex.deduplication import DedupIndex
from neocortex.deduplication.inventory.index import (
    DEFAULT_EXCLUDED_PATHS,
    DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
    DEFAULT_GENERATED_DIRECTORY_PREFIXES,
)
from neocortex.code.code_contracts import CodeRouteSummary
from neocortex.runtime.models import FrameworkConfig, SelfAnalysisRunResult
from neocortex.runtime.orchestration.orchestrator import (
    FrameworkOrchestrator,
    RouteExecutionError,
)
from neocortex.safety.route_filters import CandidateSelection
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.workflow.self_analysis.self_analysis import (
    SELF_ANALYSIS_MANIFEST_PHASE,
    build_self_analysis_inventory_policy,
)
from tests.synthetic_usn import SyntheticUsnJournal
# endregion [01]

# region [02] Implementación


_FIXTURE_CODE_SIGNATURE = "code-v2:fixture|code-analyzers-v1:fixture"


def _config(root: Path, state: Path) -> FrameworkConfig:
    return FrameworkConfig(
        root=root,
        state_directory=state,
        self_analysis=True,
        corpus_access_mode="analyze_only",
        route="code",
        document_catalog_enabled=False,
        code_include_generated=False,
        code_include_vendored=False,
        heartbeat_interval_seconds=0.01,
    )


def _inventory_adapter(seen: list[tuple[str, ...]]) -> RouteAdapter:
    def execute(context) -> CodeRouteSummary:
        with DedupIndex(context.config.dedup_database) as index:
            paths = tuple(snapshot.path for snapshot in index.snapshots(context.scan_id))
        seen.append(paths)
        return CodeRouteSummary(
            processing_signature=_FIXTURE_CODE_SIGNATURE,
            candidates=len(paths),
            processed=len(paths),
        )

    return RouteAdapter("code", execute, input_source="inventory_snapshot")


def test_locked_self_analysis_signature_and_phase_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert str(inspect.signature(FrameworkOrchestrator._run_self_analysis_locked)) == (
        "(self, access_policy: 'CorpusAccessPolicy', "
        "inventory_policy: 'InventoryExclusionPolicy', "
        "state_identity: 'CorpusAccessPolicy', "
        "internal_paths_policy: 'InternalPathsPolicy') -> 'SelfAnalysisRunResult'"
    )
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    observed: list[str] = []

    original_commands = orchestrator_module.self_analysis_commands
    original_gate = FrameworkOrchestrator._self_analysis_incremental_gate
    original_prepare = orchestrator_module.prepare_inventory
    original_publish = orchestrator_module.FrameworkState.publish_initial_routing_snapshot
    original_routes = FrameworkOrchestrator._run_content_routes
    original_complete = orchestrator_module.FrameworkState.complete_self_analysis_run

    def commands(*args, **kwargs):
        observed.append("commands")
        return original_commands(*args, **kwargs)

    def gate(self, *args, **kwargs):
        observed.append("incremental_gate")
        return original_gate(self, *args, **kwargs)

    def prepare(*args, **kwargs):
        observed.append("inventory")
        return original_prepare(*args, **kwargs)

    def publish(self, *args, **kwargs):
        observed.append("inventory_snapshot")
        return original_publish(self, *args, **kwargs)

    def routes(self, *args, **kwargs):
        observed.append("content_routes")
        return original_routes(self, *args, **kwargs)

    def complete(self, *args, **kwargs):
        observed.append("manifest_commit")
        return original_complete(self, *args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "self_analysis_commands", commands)
    monkeypatch.setattr(FrameworkOrchestrator, "_self_analysis_incremental_gate", gate)
    monkeypatch.setattr(orchestrator_module, "prepare_inventory", prepare)
    monkeypatch.setattr(
        orchestrator_module.FrameworkState,
        "publish_initial_routing_snapshot",
        publish,
    )
    monkeypatch.setattr(FrameworkOrchestrator, "_run_content_routes", routes)
    monkeypatch.setattr(
        orchestrator_module.FrameworkState,
        "complete_self_analysis_run",
        complete,
    )

    def progress(event) -> None:
        if event.operation == "framework" and event.phase in {"prepare", "complete"}:
            observed.append(f"progress:{event.phase}:{event.completed}")

    with SyntheticUsnJournal(root):
        result = FrameworkOrchestrator(
            _config(root, state),
            progress=progress,
            route_registry={"code": _inventory_adapter([])},
        ).run()

    assert isinstance(result, SelfAnalysisRunResult)
    expected = [
        "progress:prepare:0",
        "progress:prepare:1",
        "commands",
        *(["incremental_gate"] if os.name == "nt" else []),
        "inventory",
        "inventory_snapshot",
        "content_routes",
        "manifest_commit",
        "progress:complete:1",
    ]
    assert observed == expected


def test_locked_self_analysis_preserves_interrupt_identity_and_cancels_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    cancellation = KeyboardInterrupt("self-analysis cancellation sentinel")

    def cancel(_context):
        raise cancellation

    registry = {"code": RouteAdapter("code", cancel, input_source="inventory_snapshot")}
    with SyntheticUsnJournal(root):
        with pytest.raises(KeyboardInterrupt) as raised:
            FrameworkOrchestrator(_config(root, state), route_registry=registry).run()

    assert raised.value is cancellation
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        run = connection.execute("SELECT status,current_phase FROM initial_runs").fetchone()
        event = connection.execute(
            "SELECT level,message FROM run_events WHERE message='Autoanálisis cancelado por el usuario'"
        ).fetchone()
    assert run == ("cancelled", "cancelled")
    assert event == ("warning", "Autoanálisis cancelado por el usuario")


def test_self_analysis_policy_is_explicit_and_has_no_home_defaults(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    transient_roots = (root / ".pytest-codex", root / ".test-tmp")
    for transient in transient_roots:
        transient.mkdir()
    policy = build_self_analysis_inventory_policy(root, state)

    assert policy.signature.startswith("inventory-exclusion-policy-v4:xxh3_128:")
    assert set(policy.explicit_roots) == {
        str(state.resolve()),
        str((root / ".codex-lab").resolve()),
        str((root / "docs" / "audit_evidence").resolve()),
        str((root / "Laboratory").resolve()),
        str((root / "neocortex_framework.egg-info").resolve()),
        *(str(path.resolve()) for path in transient_roots),
    }
    default_keys = {str(path.resolve()).casefold() for path in DEFAULT_EXCLUDED_PATHS}
    assert not default_keys.intersection(path.casefold() for path in policy.explicit_roots)
    assert {".git", ".venv", "__pycache__", "node_modules", "dist", "target"}.issubset(
        policy.directory_names
    )
    assert set(DEFAULT_GENERATED_DIRECTORY_PREFIXES).issubset(policy.directory_prefixes)
    assert set(DEFAULT_GENERATED_DIRECTORY_FRAGMENTS).issubset(policy.directory_fragments)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda config: replace(config, corpus_access_mode="normal"),
        lambda config: replace(config, apply_actions=True),
        lambda config: replace(config, route="pdf"),
        lambda config: replace(config, route_only=True),
        lambda config: replace(config, candidate_run_id=1),
        lambda config: replace(config, resume_run_id=1),
        lambda config: replace(config, document_catalog_enabled=True),
        lambda config: replace(config, organization_root=Path("organization")),
        lambda config: replace(config, code_include_generated=True),
        lambda config: replace(config, code_include_vendored=True),
        lambda config: replace(config, code_candidate_scope="broad"),
        lambda config: replace(
            config,
            selection=CandidateSelection.from_values(paths=("source.py",)),
        ),
    ),
)
def test_programmatic_preflight_fails_before_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    state = tmp_path / "state"
    config = mutation(_config(root, state))
    monkeypatch.setattr(
        orchestrator_module,
        "query_journal_cursor",
        lambda _volume: (_ for _ in ()).throw(
            AssertionError("journal queried before self-analysis preflight")
        ),
    )

    with pytest.raises(ValueError):
        FrameworkOrchestrator(config).run()

    assert not state.exists()


def test_programmatic_preflight_rejects_non_inventory_code_adapter(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    state = tmp_path / "state"
    registry = {
        "code": RouteAdapter(
            "code",
            lambda _context: CodeRouteSummary(),
            input_source="route_candidates",
        )
    }

    with pytest.raises(ValueError, match="inventory_snapshot"):
        FrameworkOrchestrator(_config(root, state), route_registry=registry).run()

    assert not state.exists()


@pytest.mark.parametrize("state_kind", ("equal", "ancestor", "descendant"))
def test_programmatic_preflight_rejects_intersecting_state_tree(
    tmp_path: Path,
    state_kind: str,
) -> None:
    container = tmp_path / "container"
    root = container / "corpus"
    root.mkdir(parents=True)
    state = {
        "equal": root,
        "ancestor": container,
        "descendant": root / "state",
    }[state_kind]

    with pytest.raises(ValueError, match="must be disjoint"):
        FrameworkOrchestrator(_config(root, state)).run()

    assert not (state / "framework.sqlite3").exists()
    assert not (state / "framework.lock").exists()


def test_state_boundary_race_fails_before_lock_or_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    source = root / "source.py"
    root.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    state_key = str(state.absolute()).casefold()
    original_mkdir = Path.mkdir
    original_realpath = orchestrator_module.os.path.realpath

    def aliased_realpath(path, *, strict: bool = False):
        if str(Path(path).absolute()).casefold() == state_key:
            return str(root)
        return original_realpath(path, strict=strict)

    def racing_mkdir(path: Path, *args, **kwargs) -> None:
        original_mkdir(path, *args, **kwargs)
        if str(path.absolute()).casefold() == state_key:
            monkeypatch.setattr(
                orchestrator_module.os.path,
                "realpath",
                aliased_realpath,
            )

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    monkeypatch.setattr(
        orchestrator_module,
        "query_journal_cursor",
        lambda _volume: (_ for _ in ()).throw(
            AssertionError("journal queried after boundary race")
        ),
    )

    with pytest.raises(ValueError, match="corpus access root resolution is ambiguous"):
        FrameworkOrchestrator(_config(root, state)).run()

    assert source.read_text(encoding="utf-8") == "value = 1\n"
    assert set(root.iterdir()) == {source}
    assert state.is_dir()
    assert tuple(state.iterdir()) == ()


def test_self_analysis_real_code_route_omits_common_work_and_publishes_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    source = root / "pkg" / "source.py"
    source.parent.mkdir(parents=True)
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    original = source.read_bytes()
    excluded = (
        root / ".git" / "config",
        root / "dist" / "generated.py",
        root / "docs" / "audit_evidence" / "report.py",
        root / "Laboratory" / "candidate.py",
        root / "neocortex_framework.egg-info" / "generated.py",
        root / ".pytest-codex" / "temporary.py",
        root / ".test-tmp" / "temporary.py",
    )
    for path in excluded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded = True\n", encoding="utf-8")

    class ForbiddenCommonStage:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("self-analysis invoked a common corpus stage")

    monkeypatch.setattr(orchestrator_module, "DedupPlanner", ForbiddenCommonStage)
    monkeypatch.setattr(orchestrator_module, "FrameworkActions", ForbiddenCommonStage)
    monkeypatch.setattr(
        FrameworkOrchestrator,
        "_run_document_organization",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("self-analysis invoked organization")
        ),
    )

    with SyntheticUsnJournal(root) as journal:
        result = FrameworkOrchestrator(_config(root, state)).run()

    assert isinstance(result, SelfAnalysisRunResult)
    assert result.inventory_mode == "full"
    assert result.inventory_attempts == 1
    assert result.route_candidate_count == 0
    assert result.corpus_action_count == 0
    assert result.code.processed == 1
    assert source.read_bytes() == original
    assert all(path.read_text(encoding="utf-8") == "excluded = True\n" for path in excluded)
    assert journal.raw_volume_open_attempts == 0

    with DedupIndex(state / "dedup.sqlite3") as index:
        observed = {snapshot.path for snapshot in index.snapshots(result.scan.scan_id)}
    assert observed == {str(source)}
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        run_row = connection.execute(
            """SELECT run_kind,corpus_access_mode,status,current_phase,
            start_usn,end_usn
            FROM initial_runs WHERE run_id=?""",
            (result.run_id,),
        ).fetchone()
        counts = tuple(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id=?",
                (result.run_id,),
            ).fetchone()[0]
            for table in ("route_candidates", "file_actions", "run_actions")
        )
        route_row = connection.execute(
            "SELECT route_name,status,summary_json FROM route_runs WHERE run_id=?",
            (result.run_id,),
        ).fetchone()
        manifest_rows = connection.execute(
            "SELECT details_json FROM run_events WHERE run_id=? AND phase=?",
            (result.run_id, SELF_ANALYSIS_MANIFEST_PHASE),
        ).fetchall()
    with sqlite3.connect(state / "code.sqlite3") as connection:
        code_state_row = connection.execute(
            """SELECT framework_run_id,scan_id,processing_signature,status
            FROM analysis_runs ORDER BY analysis_run_id DESC LIMIT 1"""
        ).fetchone()
    if os.name == "nt":
        assert result.journal_before is not None
        assert result.journal_after is not None
        expected_start_usn = result.journal_before.next_usn
        expected_end_usn = result.journal_after.next_usn
    else:
        assert result.journal_before is None
        assert result.journal_after is None
        expected_start_usn = None
        expected_end_usn = None
    assert run_row == (
        "self_analysis",
        "analyze_only",
        "completed",
        "completed",
        expected_start_usn,
        expected_end_usn,
    )
    assert counts == (0, 0, 0)
    assert route_row[0:2] == ("code", "completed")
    assert len(manifest_rows) == 1
    manifest = json.loads(manifest_rows[0][0])
    route_summary = json.loads(route_row[2])
    assert manifest["run"]["run_id"] == result.run_id
    assert manifest["inventory"]["scan_id"] == result.scan.scan_id
    if os.name == "nt":
        assert manifest["inventory"]["journal"]["start_usn"] == expected_start_usn
        assert manifest["inventory"]["journal"]["end_usn"] == expected_end_usn
    else:
        assert manifest["inventory"]["journal"] == {"status": "unavailable"}
    assert manifest["inventory"]["policy"]["signature"] == (result.inventory_policy_signature)
    assert manifest["code"]["processing_signature"] == (result.code.processing_signature)
    assert route_summary["processing_signature"] == result.code.processing_signature
    assert code_state_row == (
        result.run_id,
        result.scan.scan_id,
        result.code.processing_signature,
        "completed",
    )
    assert manifest["safety"] == {
        "file_actions": 0,
        "organization_events": 0,
        "route_candidates": 0,
        "run_actions": 0,
    }
    assert isinstance(manifest["commands"]["analyze"], list)


def test_self_analysis_falls_back_to_full_scan_when_usn_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    source = root / "source.py"
    temporary = root / ".pytest-codex" / "temporary.py"
    root.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    temporary.parent.mkdir()
    temporary.write_text("temporary = True\n", encoding="utf-8")
    original = source.read_bytes()
    seen: list[tuple[str, ...]] = []
    registry = {"code": _inventory_adapter(seen)}

    def deny_volume(_volume: str) -> None:
        raise VolumeAccessError("CreateFileW", "C:", 5, "Acceso denegado")

    monkeypatch.setattr(orchestrator_module, "query_journal_cursor", deny_volume)

    result = FrameworkOrchestrator(
        _config(root, state),
        route_registry=registry,
    ).run()

    assert isinstance(result, SelfAnalysisRunResult)
    assert result.journal_before is None
    assert result.journal_after is None
    assert result.journal_usn_span is None
    assert result.inventory_mode == "full"
    assert result.inventory_attempts == 1
    assert result.reconciliation_records == 0
    assert seen == [(str(source),)]
    assert source.read_bytes() == original
    assert temporary.read_text(encoding="utf-8") == "temporary = True\n"

    with sqlite3.connect(state / "framework.sqlite3") as connection:
        row = connection.execute(
            """SELECT journal_volume,journal_id,start_usn,end_usn,status
            FROM initial_runs WHERE run_id=?""",
            (result.run_id,),
        ).fetchone()
        manifest_raw = connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase=?""",
            (result.run_id, SELF_ANALYSIS_MANIFEST_PHASE),
        ).fetchone()[0]
        gate = connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='self-analysis-incremental-gate'""",
            (result.run_id,),
        ).fetchone()[0]
    manifest = json.loads(manifest_raw)
    assert row == (None, None, None, None, "completed")
    assert manifest["schema"] == "neocortex.self-analysis-manifest/v2"
    assert manifest["inventory"]["journal"] == {"status": "unavailable"}
    assert json.loads(gate)["reason"] == "journal_unavailable_full_scan"


def test_exception_after_manifest_commit_cannot_degrade_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    registry = {"code": _inventory_adapter([])}
    original_finalizer = orchestrator_module.FrameworkState.complete_self_analysis_run

    def finalize_then_raise(self, *args, **kwargs):
        original_finalizer(self, *args, **kwargs)
        raise RuntimeError("post-commit failure sentinel")

    monkeypatch.setattr(
        orchestrator_module.FrameworkState,
        "complete_self_analysis_run",
        finalize_then_raise,
    )

    with SyntheticUsnJournal(root):
        with pytest.raises(RuntimeError, match="post-commit failure sentinel"):
            FrameworkOrchestrator(
                _config(root, state),
                route_registry=registry,
            ).run()

    with sqlite3.connect(state / "framework.sqlite3") as connection:
        run_row = connection.execute("SELECT status,current_phase FROM initial_runs").fetchone()
        route_row = connection.execute(
            "SELECT status,current_phase FROM route_runs WHERE route_name='code'"
        ).fetchone()
        manifest_count = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE phase=?",
            (SELF_ANALYSIS_MANIFEST_PHASE,),
        ).fetchone()[0]
        failure_count = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE message='Autoanálisis fallido'"
        ).fetchone()[0]
    assert run_row == ("completed", "completed")
    assert route_row == ("completed", "completed")
    assert manifest_count == 1
    assert failure_count == 0


def test_self_analysis_reuses_only_a_matching_durable_checkpoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    source = root / "source.py"
    root.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    seen: list[tuple[str, ...]] = []
    registry = {"code": _inventory_adapter(seen)}

    with SyntheticUsnJournal(root) as journal:
        first = FrameworkOrchestrator(_config(root, state), route_registry=registry).run()
        second = FrameworkOrchestrator(_config(root, state), route_registry=registry).run()
        renamed = root / "renamed.py"
        source.rename(renamed)
        renamed.write_text("value = 2\n", encoding="utf-8")
        third = FrameworkOrchestrator(_config(root, state), route_registry=registry).run()

    assert isinstance(first, SelfAnalysisRunResult)
    assert isinstance(second, SelfAnalysisRunResult)
    assert isinstance(third, SelfAnalysisRunResult)
    expected_modes = (
        ("full", "incremental", "incremental") if os.name == "nt" else ("full", "full", "full")
    )
    assert (first.inventory_mode, second.inventory_mode, third.inventory_mode) == expected_modes
    if os.name == "nt":
        assert first.scan.scan_id == second.scan.scan_id == third.scan.scan_id
    else:
        assert first.scan.scan_id < second.scan.scan_id < third.scan.scan_id
    assert seen[-1] == (str(renamed),)
    assert journal.raw_volume_open_attempts == 0


def test_failed_inventory_owner_checkpoint_cannot_authorize_incremental(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")

    def fail(_context):
        raise RuntimeError("route failure sentinel")

    failing = {"code": RouteAdapter("code", fail, input_source="inventory_snapshot")}
    seen: list[tuple[str, ...]] = []
    succeeding = {"code": _inventory_adapter(seen)}
    with SyntheticUsnJournal(root) as journal:
        with pytest.raises(RouteExecutionError):
            FrameworkOrchestrator(_config(root, state), route_registry=failing).run()
        completed = FrameworkOrchestrator(_config(root, state), route_registry=succeeding).run()

    assert isinstance(completed, SelfAnalysisRunResult)
    assert completed.inventory_mode == "full"
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        rows = connection.execute(
            "SELECT status,scan_id FROM initial_runs ORDER BY run_id"
        ).fetchall()
        manifests = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE phase=?",
            (SELF_ANALYSIS_MANIFEST_PHASE,),
        ).fetchone()[0]
    assert rows[0][0] == "failed"
    assert rows[1][0] == "completed"
    assert rows[0][1] != rows[1][1]
    assert manifests == 1
    assert journal.raw_volume_open_attempts == 0


@pytest.mark.skipif(os.name != "nt", reason="incremental USN checkpoints are Windows-only")
def test_failed_incremental_checkpoint_cannot_advance_past_durable_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    source = root / "source.py"
    root.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    seen: list[tuple[str, ...]] = []
    succeeding = {"code": _inventory_adapter(seen)}

    def fail(_context):
        raise RuntimeError("incremental route failure sentinel")

    failing = {"code": RouteAdapter("code", fail, input_source="inventory_snapshot")}
    with SyntheticUsnJournal(root) as journal:
        durable = FrameworkOrchestrator(_config(root, state), route_registry=succeeding).run()
        assert isinstance(durable, SelfAnalysisRunResult)
        source.write_text("value = 2\n", encoding="utf-8")
        with pytest.raises(RouteExecutionError):
            FrameworkOrchestrator(_config(root, state), route_registry=failing).run()
        with sqlite3.connect(state / "framework.sqlite3") as connection:
            durable_end_usn = int(
                connection.execute(
                    "SELECT end_usn FROM initial_runs WHERE run_id=?",
                    (durable.run_id,),
                ).fetchone()[0]
            )
        with DedupIndex(state / "dedup.sqlite3") as index:
            failed_checkpoint = index.inventory_checkpoint(root)
        assert failed_checkpoint is not None
        assert failed_checkpoint.scan_id == durable.scan.scan_id
        assert failed_checkpoint.next_usn is not None
        assert failed_checkpoint.next_usn > durable_end_usn

        recovered = FrameworkOrchestrator(_config(root, state), route_registry=succeeding).run()

    assert isinstance(recovered, SelfAnalysisRunResult)
    assert recovered.inventory_mode == "full"
    assert recovered.scan.scan_id != durable.scan.scan_id
    assert journal.raw_volume_open_attempts == 0


# endregion [02]
