"""Contracts for the canonical local Linux source-change gate."""

from __future__ import annotations

import _thread
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from _04_Nucleo_Operativo.code_change_validation import (
    AffectedTestSelection,
    CODE_CHANGE_VALIDATION_POLICY,
    ChangeValidationError,
    GitChangeSnapshot,
    ValidationGate,
    _build_result,
    _default_runner,
    _experiment_gate,
    _fresh_review_gate,
    _global_change_fallback_tests,
    _pip_audit_snapshot_preflight,
    _provider_failure,
    _public_review_stability_gate,
    _relevant_question_state,
    _replay_gate,
    _replay_technical_disposition_gate,
    _scope_relevance,
    _trusted_deep_command,
    _trusted_deep_timeout_seconds,
    _validation_question_scopes,
    capture_git_change,
    select_affected_tests,
    validate_code_change,
)
from _04_Nucleo_Operativo.code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisSubjectRef,
    analysis_question_spec_fingerprint,
)
from _04_Nucleo_Operativo.code_architecture_questions import ARCHITECTURE_CONTRACT_QUESTION
from _04_Nucleo_Operativo.code_change_evolution_analysis import (
    CODE_SCHEMA_EVOLUTION_QUESTION,
)
from _04_Nucleo_Operativo.code_interface_surface_analysis import CLI_SURFACE_QUESTION
from _04_Nucleo_Operativo.code_invariant_contracts import (
    EXPERIMENT_SCENARIO_IDS,
    RUNTIME_SCENARIOS,
)
from _04_Nucleo_Operativo.code_knowledge_asset_health_analysis import (
    KNOWLEDGE_ASSET_HEALTH_QUESTION,
)
from _04_Nucleo_Operativo.code_knowledge_pdf_asset_health_analysis import (
    KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION,
)
from _04_Nucleo_Operativo.code_route_capability_analysis import ROUTE_CAPABILITY_QUESTION
from _04_Nucleo_Operativo.code_retention_analysis import RETENTION_HOLD_QUESTION
from _04_Nucleo_Operativo.code_review_serialization import CodeReviewDigest
from _04_Nucleo_Operativo.code_review_task_analysis import (
    FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION,
)
from _04_Nucleo_Operativo.code_security_dependency_questions import (
    DEPENDENCY_EVIDENCE_QUESTION,
    SECURITY_EVIDENCE_QUESTION,
)
from _04_Nucleo_Operativo.code_state_interaction_analysis import WORKFLOW_SQL_QUESTION
from _04_Nucleo_Operativo.code_state_projection_analysis import (
    TEXT_SEMANTIC_PROJECTION_QUESTION,
)
from _04_Nucleo_Operativo.external_evidence_providers import (
    INSTALLED_PACKAGE_PROVIDER_ID,
    PIP_AUDIT_PROVIDER_ID,
)
from _04_Nucleo_Operativo.platform.shared.capability_registry import CAPABILITY_REGISTRY
from _04_Nucleo_Operativo.semantic_models import canonical_json


def test_optional_mutation_abstention_is_not_a_failed_machine_gate() -> None:
    provider = SimpleNamespace(
        provider_id="cosmic-ray-focal-mutation",
        status="abstained",
        gate="not_evaluated",
        reason="provider_abstained:mutation_target_not_declared",
    )

    assert _provider_failure(provider) is False


def test_global_fallback_covers_every_allowlisted_experiment_test_module() -> None:
    root = Path(__file__).resolve().parents[1]
    experiment_ids = frozenset(EXPERIMENT_SCENARIO_IDS)
    required = {
        nodeid.split("::", 1)[0]
        for scenario in RUNTIME_SCENARIOS
        if scenario.scenario_id in experiment_ids
        for nodeid in scenario.test_nodeids
    }

    assert required <= set(_global_change_fallback_tests(root))


def test_full_suite_coverage_consumes_the_canonical_no_regression_baseline() -> None:
    from _04_Nucleo_Operativo import code_change_validation

    root = Path(__file__).resolve().parents[1]
    totals = SimpleNamespace(
        executable_lines=71_019,
        covered_lines=59_779,
        branch_exits=22_270,
        covered_branch_exits=15_410,
    )
    analysis = SimpleNamespace(
        status="ready",
        reason=None,
        outcomes=SimpleNamespace(collected=100, selected=100, failed=0),
        provider_id="pytest-coverage",
        tool_run_id=1,
        effective_tool_run_id=1,
        suite_selection="full",
        measurement_complete=True,
        content_executed=True,
        suite_signature="suite",
        measurement_scope_signature="scope",
        limitations=(),
        totals=totals,
        tool_versions=(SimpleNamespace(name="coverage", version="7.14.1"),),
        gates=(),
    )
    selection = AffectedTestSelection(
        strategy="full",
        selectors=("tests/test_logic.py",),
        direct_tests=(),
        dependency_tests=(),
        convention_tests=(),
        uncovered_sources=(),
        reasons=("change_crosses_full_suite_boundary",),
    )

    passed = code_change_validation._coverage_gate(
        SimpleNamespace(test_coverage=analysis),
        root=root,
        selection=selection,
    )
    regressed = code_change_validation._coverage_gate(
        SimpleNamespace(
            test_coverage=SimpleNamespace(
                **{
                    **analysis.__dict__,
                    "totals": SimpleNamespace(**{**totals.__dict__, "covered_lines": 1}),
                }
            )
        ),
        root=root,
        selection=selection,
    )

    assert passed.status == "passed"
    assert passed.evidence["coverage_baseline_status"] == "passed"
    assert regressed.status == "failed"
    assert "lines_coverage_regressed" in regressed.evidence["coverage_regressions"]


def test_full_suite_producer_omits_every_test_selector(tmp_path: Path) -> None:
    command = tuple(
        str(item)
        for item in _trusted_deep_command(
            tmp_path / "Repository",
            tmp_path / "state",
            ("tests/test_one.py", "tests/test_two.py"),
            max_tests=5_000,
            time_budget_seconds=900,
            full_suite=True,
        )
    )

    assert "--analysis-profile" in command
    assert "trusted-deep" in command
    assert "--deep-test-selector" not in command
    assert command[command.index("--deep-max-tests") + 1] == "10000"
    assert command[command.index("--deep-shard-size") + 1] == "250"


def test_full_suite_timeout_reserves_noncoverage_providers_and_finalization() -> None:
    assert _trusted_deep_timeout_seconds(900, full_suite=True) == 60 * 60
    assert _trusted_deep_timeout_seconds(900, full_suite=False) == 45 * 60
    assert _trusted_deep_timeout_seconds(30, full_suite=False) == 16 * 60


def test_default_runner_interrupts_and_reaps_a_timed_out_process(tmp_path: Path) -> None:
    marker = tmp_path / "interrupted"
    script = (
        "import signal,sys,time; from pathlib import Path; "
        "marker=Path(sys.argv[1]); "
        "signal.signal(signal.SIGINT, lambda *_: "
        "(marker.write_text('interrupted', encoding='utf-8'), sys.exit(130))); "
        "time.sleep(60)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _default_runner(
            (sys.executable, "-c", script, marker),
            cwd=tmp_path,
            timeout=1.0,
        )

    assert marker.read_text(encoding="utf-8") == "interrupted"


def test_default_runner_streams_both_channels_while_preserving_capture(
    tmp_path: Path,
) -> None:
    progress: list[tuple[str, str]] = []

    completed = _default_runner(
        (
            sys.executable,
            "-c",
            "import sys; print('visible-out', flush=True); "
            "print('visible-err', file=sys.stderr, flush=True)",
        ),
        cwd=tmp_path,
        timeout=30.0,
        progress=lambda stream, line: progress.append((stream, line)),
    )

    assert completed.returncode == 0
    assert completed.stdout == "visible-out\n"
    assert completed.stderr == "visible-err\n"
    assert sorted(progress) == [
        ("stderr", "visible-err"),
        ("stdout", "visible-out"),
    ]


def test_default_runner_reaps_a_finished_adopted_child_without_false_timeout(
    tmp_path: Path,
) -> None:
    child_code = "import os, time; os.setsid(); time.sleep(0.05)"
    middle_code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)"
    )
    leader_code = (
        "import subprocess, sys, time; "
        f"subprocess.run([sys.executable, '-c', {middle_code!r}], check=True); "
        "time.sleep(0.3); print('leader-complete', flush=True)"
    )

    completed = _default_runner(
        (sys.executable, "-c", leader_code),
        cwd=tmp_path,
        timeout=10.0,
    )

    assert completed.returncode == 0
    assert completed.stdout == "leader-complete\n"


def test_default_runner_drains_volume_when_progress_callback_fails(tmp_path: Path) -> None:
    callback_calls = 0

    def broken_progress(_stream: str, _line: str) -> None:
        nonlocal callback_calls
        callback_calls += 1
        raise RuntimeError("reporter unavailable")

    completed = _default_runner(
        (
            sys.executable,
            "-c",
            "import sys; "
            "[(print(f'out-{i}'), print(f'err-{i}', file=sys.stderr)) for i in range(250)]",
        ),
        cwd=tmp_path,
        timeout=30.0,
        progress=broken_progress,
    )

    assert completed.returncode == 0
    assert len(completed.stdout.splitlines()) == 250
    assert len(completed.stderr.splitlines()) == 250
    assert callback_calls == 500


def test_default_runner_kills_a_descendant_that_keeps_pipes_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    monkeypatch.setattr(code_change_validation, "_COMMAND_INTERRUPT_GRACE_SECONDS", 0.25)
    subreaper_before = code_change_validation._subreaper_state()
    file_descriptors_before = len(tuple(Path("/proc/self/fd").iterdir()))
    pid_path = tmp_path / "descendant.pid"
    child_code = (
        "import os, signal, time; from pathlib import Path; "
        "os.setsid(); "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii'); "
        "print('descendant-ready', flush=True); time.sleep(60)"
    )
    middle_code = (
        "import subprocess, sys, threading; "
        f"thread=threading.Thread(target=lambda: subprocess.Popen([sys.executable, '-c', {child_code!r}])); "
        "thread.start(); thread.join()"
    )
    leader_code = (
        "import subprocess, sys, time; "
        f"subprocess.run([sys.executable, '-c', {middle_code!r}], check=True); "
        "print('leader-ready', flush=True); time.sleep(60)"
    )

    with pytest.raises(subprocess.TimeoutExpired) as captured:
        _default_runner(
            (sys.executable, "-c", leader_code),
            cwd=tmp_path,
            timeout=0.5,
        )

    assert "leader-ready" in str(captured.value.output)
    assert "descendant-ready" in str(captured.value.output)
    descendant_pid = int(pid_path.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        stat = Path(f"/proc/{descendant_pid}/stat")
        if not stat.exists() or stat.read_text(encoding="ascii").split()[2] == "Z":
            break
        time.sleep(0.02)
    else:
        os.kill(descendant_pid, 0)
        pytest.fail("validation descendant survived process-group termination")
    assert code_change_validation._subreaper_state() is subreaper_before
    assert len(tuple(Path("/proc/self/fd").iterdir())) == file_descriptors_before
    assert not any(
        thread.name.startswith("neocortex-validation-") for thread in threading.enumerate()
    )


def test_default_runner_restores_subreaper_after_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    subreaper_before = code_change_validation._subreaper_state()
    file_descriptors_before = len(tuple(Path("/proc/self/fd").iterdir()))
    pid_path = tmp_path / "interrupted.pid"
    timer = threading.Timer(0.2, _thread.interrupt_main)
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            _default_runner(
                (
                    sys.executable,
                    "-c",
                    "import os, time; from pathlib import Path; "
                    f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii'); "
                    "time.sleep(60)",
                ),
                cwd=tmp_path,
                timeout=30.0,
            )
    finally:
        timer.cancel()
        timer.join()

    interrupted_pid = int(pid_path.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(interrupted_pid, 0)
    assert code_change_validation._subreaper_state() is subreaper_before
    assert len(tuple(Path("/proc/self/fd").iterdir())) == file_descriptors_before
    assert not any(
        thread.name.startswith("neocortex-validation-") for thread in threading.enumerate()
    )


def test_ready_provider_delta_is_advisory_after_static_no_regression() -> None:
    provider = SimpleNamespace(
        provider_id="mypy-trusted-project",
        status="ready",
        gate="failed",
        reason=None,
        added=115,
        resolved=59,
    )

    assert _provider_failure(provider) is False


def test_linux_publication_only_snapshot_is_an_eligible_review_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    monkeypatch.setattr(
        code_change_validation,
        "_TRUSTED_DEEP_REQUIRED_PROVIDER_IDS",
        frozenset({"fixture-provider"}),
    )
    provider = SimpleNamespace(
        provider_id="fixture-provider",
        status="ready",
        gate="baseline",
        reason=None,
    )
    result = SimpleNamespace(
        status="ready",
        reason=None,
        snapshot=SimpleNamespace(
            analysis_run_id=9,
            freshness="publication_only",
            current=False,
            processing_signature="fixture",
        ),
        external_evidence_suite=SimpleNamespace(
            profile="trusted-deep",
            providers=(provider,),
        ),
        question_evaluations=(),
        supply_chain=None,
        recommendations=(),
        digest=None,
        as_payload=lambda: {"schema": "neocortex.code-review/v22"},
    )
    monkeypatch.setattr(
        code_change_validation,
        "review_code_state",
        lambda *_args, **_kwargs: result,
    )

    gate, observed = _fresh_review_gate(tmp_path)

    assert observed is result
    assert gate.status == "passed"
    assert gate.reason == "fresh_review_has_no_failed_machine_gate"


def _network_abstained_pip_provider() -> SimpleNamespace:
    return SimpleNamespace(
        provider_id=PIP_AUDIT_PROVIDER_ID,
        status="abstained",
        gate="not_evaluated",
        reason="provider_abstained:pip_audit_network_unavailable:fixture",
        result_digest=None,
        comparability_signature="pip-fixture",
        execution="full",
    )


def _review_with_providers(
    analysis_run_id: int,
    providers: tuple[SimpleNamespace, ...],
) -> SimpleNamespace:
    return SimpleNamespace(
        status="ready",
        reason=None,
        snapshot=SimpleNamespace(
            analysis_run_id=analysis_run_id,
            freshness="publication_only",
            current=False,
            processing_signature=f"fixture-{analysis_run_id}",
        ),
        external_evidence_suite=SimpleNamespace(
            profile="trusted-deep",
            providers=providers,
        ),
        question_evaluations=(),
        supply_chain=None,
        recommendations=(),
        digest=None,
        as_payload=lambda: {"schema": "neocortex.code-review/v22"},
    )


def _question_evaluation(
    spec: AnalysisQuestionSpec,
    *,
    evaluation_id: str,
    subject_key: str,
    readiness: Literal[
        "human_review_required", "experiment_required", "abstained"
    ] = "experiment_required",
) -> AnalysisQuestionEvaluation:
    return AnalysisQuestionEvaluation(
        evaluation_id,
        spec.question_id,
        spec.version,
        analysis_question_spec_fingerprint(spec),
        1,
        AnalysisSubjectRef(
            spec.subject_kinds[0],
            subject_key,
            "Fixture subject",
            "code",
            "snapshot:fixture",
            "current",
        ),
        (),
        (),
        "confirmed",
        "abstained",
        (),
        spec.hypotheses,
        "ready",
        readiness,
        None,
        "fixture_decision_evidence_state",
        "evaluated" if readiness == "human_review_required" else "not_evaluated",
        tuple(item.action_id for item in spec.next_actions),
        ("fixture_is_bounded",),
    )


def _change_for(*paths: str) -> GitChangeSnapshot:
    return GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        tuple(sorted(paths)),
        (),
        (),
        (),
        "b" * 64,
    )


def _selection(*selectors: str) -> AffectedTestSelection:
    ordered = tuple(sorted(selectors))
    return AffectedTestSelection(
        strategy="affected" if ordered else "none",
        selectors=ordered,
        direct_tests=ordered,
        dependency_tests=(),
        convention_tests=(),
        uncovered_sources=(),
        reasons=("fixture_selection",),
    )


def test_change_validation_payload_is_json_native_for_the_receipt_boundary() -> None:
    result = _build_result(
        {
            "status": "passed",
            "reason": None,
            "policy_id": CODE_CHANGE_VALIDATION_POLICY,
            "source_root": "/fixture/source",
            "state_directory": "/fixture/state",
            "git": _change_for("neocortex/logic.py"),
            "selection": _selection("tests/test_fixture.py"),
            "gates": (
                ValidationGate(
                    "source_snapshot_unchanged",
                    "passed",
                    "source_unchanged",
                    0,
                    ("git", "diff"),
                    {"content_digest": "b" * 64},
                ),
            ),
            "experiment_proposals": (),
            "executable_experiments": (),
            "experiment_receipts": (),
            "resource_boundary": None,
            "source_unchanged": True,
            "authority": "validation",
            "mutation_authority": False,
        }
    )

    payload = result.as_payload()
    git = payload["git"]
    selection = payload["selection"]

    assert isinstance(payload["gates"], list)
    assert isinstance(git, dict) and isinstance(git["changed_paths"], list)
    assert isinstance(selection, dict) and isinstance(selection["selectors"], list)
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload


def _public_review_fixture() -> SimpleNamespace:
    provider = SimpleNamespace(
        provider_id="provider:fixture",
        as_payload=lambda: {
            "schema": "neocortex.external-provider-status/v4",
            "provider_id": "provider:fixture",
            "status": "ready",
        },
    )
    return SimpleNamespace(
        status="ready",
        reason=None,
        snapshot=SimpleNamespace(
            analysis_run_id=17,
            processing_signature="snapshot:fixture",
            freshness="publication_only",
        ),
        digest=CodeReviewDigest("a" * 32, "b" * 16, 100),
        external_evidence_suite=SimpleNamespace(
            profile="trusted-deep",
            providers=(provider,),
        ),
        experiment_receipts=(
            SimpleNamespace(receipt=SimpleNamespace(receipt_id="receipt:fixture")),
        ),
        question_evaluations=(object(), object()),
        materialization_limit=50,
        mutation_authority=False,
    )


def test_public_review_stability_uses_two_repeatable_fresh_process_reads(
    tmp_path: Path,
) -> None:
    from _04_Nucleo_Operativo.code_validation_public_review import (
        code_review_identity,
        validation_stable_public_review_identity,
    )

    review = _public_review_fixture()
    public_identity = code_review_identity(review)
    public_identity["digest"] = {
        "xxh3_128": "c" * 32,
        "xxh3_64_guard": "d" * 16,
        "byte_count": 200,
    }
    public_identity["question_evaluations"] = 1
    public_identity["experiment_receipt_ids"] = []
    stable_identity = validation_stable_public_review_identity(public_identity)
    commands: list[tuple[str, ...]] = []
    timeouts: list[float] = []

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        commands.append(command)
        timeouts.append(timeout)
        return subprocess.CompletedProcess(command, 0, canonical_json(stable_identity), "")

    gate = _public_review_stability_gate(
        tmp_path,
        tmp_path,
        review,
        runner=runner,
    )

    assert gate.status == "passed"
    assert gate.evidence["fresh_process_reads"] == 2
    assert gate.evidence["validation_stable_identity"] == stable_identity
    assert gate.evidence["public_identity"] == code_review_identity(review)
    assert len(commands) == 2
    assert timeouts == [120, 120]
    assert all(command[-1] == "--validation-stable" for command in commands)


def test_public_review_stability_abstains_when_fresh_reads_disagree(tmp_path: Path) -> None:
    from _04_Nucleo_Operativo.code_validation_public_review import (
        code_review_identity,
        validation_stable_public_review_identity,
    )

    review = _public_review_fixture()
    identities = [
        validation_stable_public_review_identity(code_review_identity(review)),
        validation_stable_public_review_identity(code_review_identity(review)),
    ]
    snapshot = dict(identities[1]["snapshot"])
    snapshot["processing_signature"] = "snapshot:displaced"
    identities[1]["snapshot"] = snapshot

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        return subprocess.CompletedProcess(command, 0, canonical_json(identities.pop(0)), "")

    gate = _public_review_stability_gate(
        tmp_path,
        tmp_path,
        review,
        runner=runner,
    )

    assert gate.status == "abstained"
    assert "fresh_process_public_review_not_repeatable" in gate.evidence["blockers"]


def _pip_audit_preflight_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fresh_until: float | None,
    findings: tuple[str, ...] = (),
    exact_snapshot: bool = True,
) -> SimpleNamespace:
    from _04_Nucleo_Operativo import code_change_validation

    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "code.sqlite3").touch()
    descriptor = SimpleNamespace(
        provider_id=PIP_AUDIT_PROVIDER_ID,
        profile="trusted-static",
        configuration_signature="configuration:fixture",
        environment_signature="environment:fixture",
        root_identity="root:fixture",
        comparability_signature="comparability:fixture",
    )
    provider = SimpleNamespace(
        descriptor=descriptor,
        tool_version=lambda: "2.10.1",
        baseline_input_signature=lambda _files: "input:fixture",
    )
    exact = SimpleNamespace(
        tool_run_id=71,
        portable_finding_ids=findings,
        fresh_until_unix_seconds=fresh_until,
    )
    monkeypatch.setattr(
        code_change_validation,
        "PipAuditKnownVulnerabilitiesProvider",
        lambda _source: provider,
    )
    monkeypatch.setattr(
        code_change_validation,
        "readonly_code_database",
        lambda _database: nullcontext(object()),
    )
    monkeypatch.setattr(code_change_validation, "validate_code_schema", lambda _connection: None)
    monkeypatch.setattr(
        code_change_validation,
        "read_external_provider_baselines",
        lambda _connection, **_kwargs: ((exact if exact_snapshot else None), None),
    )
    return SimpleNamespace(state=state)


def test_pip_audit_preflight_requires_freshness_through_the_hard_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    fixture = _pip_audit_preflight_fixture(
        tmp_path,
        monkeypatch,
        fresh_until=now + 7200,
    )
    monotonic = time.monotonic_ns()
    window = SimpleNamespace(hard_deadline_monotonic_ns=monotonic + 3600 * 1_000_000_000)

    passed = _pip_audit_snapshot_preflight(
        tmp_path,
        fixture.state,
        change=_change_for("neocortex/logic.py"),
        runtime_window=window,
    )

    assert passed.status == "passed"
    assert passed.evidence["tool_run_id"] == 71
    assert passed.evidence["known_vulnerability_findings"] == 0

    stale_fixture = _pip_audit_preflight_fixture(
        tmp_path / "stale",
        monkeypatch,
        fresh_until=now + 60,
    )
    stale = _pip_audit_snapshot_preflight(
        tmp_path,
        stale_fixture.state,
        change=_change_for("neocortex/logic.py"),
        runtime_window=window,
    )
    assert stale.status == "abstained"
    assert stale.reason == "pip_audit_snapshot_expires_before_validation_deadline"


def test_pip_audit_preflight_requires_a_source_bound_snapshot_for_supply_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _pip_audit_preflight_fixture(
        tmp_path,
        monkeypatch,
        fresh_until=time.time() + 7200,
        findings=("vulnerability:fixture",),
    )
    monotonic = time.monotonic_ns()
    window = SimpleNamespace(hard_deadline_monotonic_ns=monotonic + 3600 * 1_000_000_000)

    vulnerable = _pip_audit_snapshot_preflight(
        tmp_path,
        fixture.state,
        change=_change_for("neocortex/logic.py"),
        runtime_window=window,
    )
    clean_fixture = _pip_audit_preflight_fixture(
        tmp_path / "clean",
        monkeypatch,
        fresh_until=time.time() + 7200,
    )
    source_bound = _pip_audit_snapshot_preflight(
        tmp_path,
        clean_fixture.state,
        change=_change_for("pyproject.toml"),
        runtime_window=window,
    )
    missing_fixture = _pip_audit_preflight_fixture(
        tmp_path / "missing",
        monkeypatch,
        fresh_until=time.time() + 7200,
        exact_snapshot=False,
    )
    lock_missing = _pip_audit_snapshot_preflight(
        tmp_path,
        missing_fixture.state,
        change=_change_for("constraints-linux-cp314.lock"),
        runtime_window=window,
    )

    assert vulnerable.status == "failed"
    assert vulnerable.reason == "pip_audit_snapshot_reports_known_vulnerabilities"
    assert source_bound.status == "passed"
    assert source_bound.evidence["supply_chain_paths"] == ["pyproject.toml"]
    assert lock_missing.status == "abstained"
    assert lock_missing.reason == "pip_audit_exact_snapshot_missing"
    assert lock_missing.evidence["supply_chain_paths"] == ["constraints-linux-cp314.lock"]


def test_canonical_validation_stops_before_static_when_supply_preflight_abstains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    source = tmp_path / "source"
    state = tmp_path / "state"
    source.mkdir()
    state.mkdir()
    change = _change_for("neocortex/logic.py")
    selection = _selection("tests/test_fixture.py")
    commands: list[tuple[object, ...]] = []

    def runner(command, **_kwargs):
        commands.append(tuple(command))
        raise AssertionError("an expensive command ran after a failed supply preflight")

    monkeypatch.setattr(code_change_validation, "_default_runner", runner)
    monkeypatch.setattr(code_change_validation, "capture_git_change", lambda *_a, **_k: change)
    monkeypatch.setattr(
        code_change_validation,
        "select_affected_tests",
        lambda *_a, **_k: selection,
    )
    monkeypatch.setattr(code_change_validation, "_unpublished_source_paths", lambda *_a: ())
    monkeypatch.setattr(
        code_change_validation,
        "_pip_audit_snapshot_preflight",
        lambda *_a, **_k: code_change_validation.ValidationGate(
            "pip_audit_snapshot_preflight",
            "abstained",
            "pip_audit_snapshot_expires_before_validation_deadline",
            1,
            (),
            {},
        ),
    )
    window = SimpleNamespace(hard_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000)

    result = validate_code_change(
        root=source,
        state_directory=state,
        runner=runner,
        runtime_window=window,
    )

    assert commands == []
    assert [gate.gate_id for gate in result.gates] == [
        "pip_audit_snapshot_preflight",
        "source_snapshot_unchanged",
    ]
    assert result.status == "abstained"


def test_network_only_pip_failure_uses_only_a_resolved_fresh_exact_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    monkeypatch.setattr(
        code_change_validation,
        "_TRUSTED_DEEP_REQUIRED_PROVIDER_IDS",
        frozenset({PIP_AUDIT_PROVIDER_ID}),
    )
    result = _review_with_providers(9, (_network_abstained_pip_provider(),))
    monkeypatch.setattr(code_change_validation, "review_code_state", lambda *_a, **_k: result)
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("neocortex/logic.py",),
        (),
        (),
        (),
        "b" * 64,
    )
    receipt = {
        "tool_run_id": 71,
        "analysis_run_id": 7,
        "result_digest": "audit-digest",
        "inventory_versions_identical": True,
    }
    monkeypatch.setattr(
        code_change_validation,
        "_historical_pip_audit_fallback",
        lambda *_a, **_k: receipt,
    )

    gate, observed = _fresh_review_gate(tmp_path, change=change)

    assert observed is result
    assert gate.status == "passed"
    assert gate.evidence["historical_pip_audit_fallback"] == receipt
    assert gate.evidence["provider_failures"] == []

    monkeypatch.setattr(
        code_change_validation,
        "_historical_pip_audit_fallback",
        lambda *_a, **_k: None,
    )
    failed, _ = _fresh_review_gate(tmp_path, change=change)
    assert failed.status == "failed"
    assert failed.evidence["provider_failures"] == [PIP_AUDIT_PROVIDER_ID]


def test_replay_accepts_the_same_resolved_fresh_pip_snapshot_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    fixture_provider_id = "fixture-provider"
    monkeypatch.setattr(
        code_change_validation,
        "_TRUSTED_DEEP_REQUIRED_PROVIDER_IDS",
        frozenset(
            {
                fixture_provider_id,
                INSTALLED_PACKAGE_PROVIDER_ID,
                PIP_AUDIT_PROVIDER_ID,
            }
        ),
    )
    first_fixture = SimpleNamespace(
        provider_id=fixture_provider_id,
        status="ready",
        gate="baseline",
        reason=None,
        result_digest="fixture-result",
        comparability_signature="fixture-comparability",
        execution="full",
    )
    replay_fixture = SimpleNamespace(**{**vars(first_fixture), "execution": "cache_replay"})
    first_inventory = SimpleNamespace(
        provider_id=INSTALLED_PACKAGE_PROVIDER_ID,
        status="ready",
        gate="baseline",
        reason=None,
        result_digest="first-clock-bound-result",
        comparability_signature="inventory-comparability",
        execution="full",
    )
    replay_inventory = SimpleNamespace(
        **{**vars(first_inventory), "result_digest": "replay-clock-bound-result"}
    )
    first = _review_with_providers(
        10,
        (first_fixture, first_inventory, _network_abstained_pip_provider()),
    )
    replay = _review_with_providers(
        11,
        (replay_fixture, replay_inventory, _network_abstained_pip_provider()),
    )
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("neocortex/logic.py",),
        (),
        (),
        (),
        "b" * 64,
    )
    monkeypatch.setattr(
        code_change_validation,
        "_historical_pip_audit_fallback",
        lambda *_a, **_k: {"tool_run_id": 71, "result_digest": "audit-digest"},
    )
    inventory_receipt = {
        "semantic_projection_digest": "sha256:inventory",
        "semantics_identical": True,
    }
    monkeypatch.setattr(
        code_change_validation,
        "_installed_inventory_replay_receipt",
        lambda *_a, **_k: inventory_receipt,
    )

    gate = _replay_gate(
        first,
        replay,
        state_directory=tmp_path,
        change=change,
    )

    assert gate.status == "passed"
    assert gate.evidence["cache_replays"] == [fixture_provider_id]
    fallback = cast(dict[str, object], gate.evidence["historical_pip_audit_fallback"])
    assert fallback["tool_run_id"] == 71
    assert gate.evidence["installed_inventory_replay"] == inventory_receipt


def test_replay_accepts_identical_full_reobservation_when_physical_replay_is_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    provider_id = "fixture-version-bound-provider"
    monkeypatch.setattr(
        code_change_validation,
        "_TRUSTED_DEEP_REQUIRED_PROVIDER_IDS",
        frozenset({provider_id}),
    )
    provider = SimpleNamespace(
        provider_id=provider_id,
        status="ready",
        gate="baseline",
        reason=None,
        result_digest="stable-semantic-result",
        comparability_signature="stable-comparability",
        execution="full",
    )

    gate = _replay_gate(
        _review_with_providers(20, (provider,)),
        _review_with_providers(21, (provider,)),
        state_directory=tmp_path,
        change=_change_for("neocortex/logic.py"),
    )

    assert gate.status == "passed"
    assert gate.reason == "all_required_provider_evidence_replayed_or_reobserved_identically"
    assert gate.evidence["cache_replays"] == []
    assert gate.evidence["identical_full_reobservations"] == [provider_id]
    assert gate.evidence["unresolved_execution_ids"] == []


def test_canonical_experiment_gate_persists_each_exact_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_experiment_executor as executor
    import _04_Nucleo_Operativo.code_experiment_store as store

    evaluation = _question_evaluation(
        ROUTE_CAPABILITY_QUESTION,
        evaluation_id="evaluation:exact",
        subject_key="capability:route:text",
    )
    proposal = SimpleNamespace(
        proposal_id="proposal:exact",
        evaluation_id=evaluation.evaluation_id,
        question_id=evaluation.question_id,
        subject_key=evaluation.subject.subject_key,
        template_id="capability.public_route_acceptance",
        template_version="v1",
        planning_status="planned",
        runner_kind="trusted_deep_declared_scenarios",
    )
    review = SimpleNamespace(
        experiment_plan=SimpleNamespace(
            proposals=(proposal,),
            planned_count=1,
            registry_gap_count=0,
        ),
        question_evaluations=(evaluation,),
        technical_verification=SimpleNamespace(reviews=()),
        snapshot=SimpleNamespace(
            analysis_run_id=17,
            processing_signature="snapshot:exact",
        ),
        test_coverage=SimpleNamespace(
            tool_run_id=71,
            effective_tool_run_id=70,
            suite_selection="full",
            configuration_signature="coverage-config:exact",
            suite_signature="coverage-suite:exact",
            measurement_scope_signature="coverage-scope:exact",
        ),
        digest=SimpleNamespace(),
    )
    receipt = SimpleNamespace(
        receipt_id="receipt:exact",
        status="passed",
        as_payload=lambda: {"receipt_id": "receipt:exact", "status": "passed"},
    )
    stored = SimpleNamespace(receipt=receipt)
    observed: list[tuple[object, ...]] = []

    monkeypatch.setattr(
        executor,
        "attest_code_experiments",
        lambda *args, **kwargs: (receipt,),
    )
    monkeypatch.setattr(store, "code_review_digest_identity", lambda _digest: "review:exact")

    def record(*args, **kwargs):
        observed.append((*args, kwargs))
        return (stored,)

    monkeypatch.setattr(store, "record_code_experiment_receipts", record)

    gate, receipts = _experiment_gate(
        review,
        root=tmp_path,
        state_directory=tmp_path,
        change=_change_for("tests/test_code_public_route_experiments.py"),
        selection=_selection("tests/test_code_public_route_experiments.py"),
    )

    assert gate.status == "passed"
    assert gate.evidence["stored_receipt_ids"] == ["receipt:exact"]
    assert receipts == ({"receipt_id": "receipt:exact", "status": "passed"},)
    assert len(observed) == 1
    assert observed[0][0] == tmp_path / "code.sqlite3"
    assert observed[0][1] == ((receipt, proposal),)
    assert observed[0][2:5] == (17, "snapshot:exact", "review:exact")
    assert observed[0][-1] == {}


def test_supply_questions_use_their_measured_allowlisted_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_experiment_executor as executor
    import _04_Nucleo_Operativo.code_experiment_store as store

    evaluations = tuple(
        _question_evaluation(
            spec,
            evaluation_id=f"evaluation:{scope_id}",
            subject_key=subject_key,
        )
        for scope_id, spec, subject_key in (
            (
                "security",
                SECURITY_EVIDENCE_QUESTION,
                "project:neocortex-security-evidence",
            ),
            (
                "dependency",
                DEPENDENCY_EVIDENCE_QUESTION,
                "dependency:neocortex-environment",
            ),
        )
    )
    proposals = tuple(
        SimpleNamespace(
            proposal_id=f"proposal:{evaluation.evaluation_id}",
            evaluation_id=evaluation.evaluation_id,
            question_id=evaluation.question_id,
            subject_key=evaluation.subject.subject_key,
            template_id="security.bounded_boundary_scenarios",
            template_version="v2",
            planning_status="planned",
            runner_kind="trusted_deep_declared_scenarios",
        )
        for evaluation in evaluations
    )
    review = SimpleNamespace(
        experiment_plan=SimpleNamespace(
            proposals=proposals,
            planned_count=2,
            registry_gap_count=0,
        ),
        question_evaluations=evaluations,
        technical_verification=SimpleNamespace(reviews=()),
        snapshot=SimpleNamespace(
            analysis_run_id=18,
            processing_signature="snapshot:supply",
        ),
        test_coverage=SimpleNamespace(
            tool_run_id=81,
            effective_tool_run_id=80,
            suite_selection="full",
            configuration_signature="coverage-config:supply",
            suite_signature="coverage-suite:supply",
            measurement_scope_signature="coverage-scope:supply",
        ),
        digest=SimpleNamespace(),
    )

    observed_attestations: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def attest(selected_proposals, **kwargs):
        selected = tuple(selected_proposals)
        observed_attestations.append((selected, kwargs))
        return tuple(
            SimpleNamespace(
                receipt_id=f"receipt:{proposal.evaluation_id}",
                status="passed",
                as_payload=lambda proposal=proposal: {
                    "receipt_id": f"receipt:{proposal.evaluation_id}",
                    "status": "passed",
                    "process_invocations": 0,
                },
            )
            for proposal in selected
        )

    monkeypatch.setattr(executor, "attest_code_experiments", attest)
    monkeypatch.setattr(store, "code_review_digest_identity", lambda _digest: "review:supply")
    monkeypatch.setattr(
        store,
        "record_code_experiment_receipts",
        lambda _database, pairs, *_args, **_kwargs: tuple(
            SimpleNamespace(receipt=receipt) for receipt, _proposal in pairs
        ),
    )

    gate, receipts = _experiment_gate(
        review,
        root=tmp_path,
        state_directory=tmp_path,
        change=_change_for("pyproject.toml"),
        selection=_selection(),
    )

    assert gate.status == "passed"
    assert gate.reason == "unique_allowlisted_experiments_attested"
    assert gate.evidence["evidence_reuse"] == "current_trusted_deep_declared_test_outcomes"
    assert gate.evidence["provider_process_invocations"] == 0
    assert gate.evidence["proposal_count"] == 2
    assert gate.evidence["unique_template_count"] == 1
    assert {
        item["expected_template_id"]
        for item in cast(list[dict[str, object]], gate.evidence["relevant_questions"])
    } == {"security.bounded_boundary_scenarios"}
    assert {item["receipt_id"] for item in receipts} == {
        "receipt:evaluation:security",
        "receipt:evaluation:dependency",
    }
    assert len(observed_attestations) == 1
    assert observed_attestations[0][0] == tuple(
        sorted(proposals, key=lambda proposal: proposal.proposal_id)
    )
    assert observed_attestations[0][1] == {
        "source_root": tmp_path,
        "code_database_path": tmp_path / "code.sqlite3",
        "source_version": "snapshot:supply",
        "analysis_run_id": 18,
        "provider_tool_run_id": 81,
        "provider_effective_tool_run_id": 80,
        "provider_suite_selection": "full",
        "provider_configuration_signature": "coverage-config:supply",
        "provider_suite_signature": "coverage-suite:supply",
        "provider_measurement_scope_signature": "coverage-scope:supply",
    }


def test_relevant_schema_question_requires_its_exact_allowlisted_runner(
    tmp_path: Path,
) -> None:
    evaluation = _question_evaluation(
        CODE_SCHEMA_EVOLUTION_QUESTION,
        evaluation_id="evaluation:schema",
        subject_key="code-owner-schema-subject-v1:fixture",
    )
    proposal = SimpleNamespace(
        proposal_id="proposal:schema-gap",
        evaluation_id=evaluation.evaluation_id,
        planning_status="registry_gap",
        runner_kind=None,
        template_id=None,
    )
    review = SimpleNamespace(
        experiment_plan=SimpleNamespace(
            proposals=(proposal,),
            planned_count=0,
            registry_gap_count=1,
        ),
        question_evaluations=(evaluation,),
        technical_verification=SimpleNamespace(reviews=()),
    )

    gate, receipts = _experiment_gate(
        review,
        root=tmp_path,
        state_directory=tmp_path,
        change=_change_for("tests/test_code_schema_migration_v1_v2.py"),
        selection=_selection("tests/test_code_schema_migration_v1_v2.py"),
    )

    assert gate.status == "abstained"
    assert gate.reason == "affected_question_requires_unresolved_evidence"
    assert gate.evidence["blocking_reasons"] == [
        "affected_question_experiment_unavailable:code_schema_migration"
    ]
    assert receipts == ()


def test_manual_question_is_not_required_only_when_diff_binding_proves_disjoint(
    tmp_path: Path,
) -> None:
    evaluation = _question_evaluation(
        CODE_SCHEMA_EVOLUTION_QUESTION,
        evaluation_id="evaluation:schema",
        subject_key="code-owner-schema-subject-v1:fixture",
    )
    review = SimpleNamespace(
        experiment_plan=SimpleNamespace(
            proposals=(),
            planned_count=0,
            registry_gap_count=1,
        ),
        question_evaluations=(evaluation,),
        technical_verification=SimpleNamespace(reviews=()),
    )

    gate, receipts = _experiment_gate(
        review,
        root=tmp_path,
        state_directory=tmp_path,
        change=_change_for("docs/OPERATIONS.md"),
        selection=_selection(),
    )

    assert gate.status == "not_required"
    assert gate.reason == "no_validation_required_question_is_affected"
    binding = next(
        item
        for item in cast(list[dict[str, object]], gate.evidence["question_bindings"])
        if item["scope_id"] == "code_schema_migration"
    )
    assert binding["relevance"] == "not_affected"
    assert receipts == ()


def test_relevant_existing_technical_disposition_closes_without_reexecution(
    tmp_path: Path,
) -> None:
    evaluation = _question_evaluation(
        ROUTE_CAPABILITY_QUESTION,
        evaluation_id="evaluation:text-route",
        subject_key="capability:route:text",
        readiness="human_review_required",
    )
    technical = SimpleNamespace(
        reviews=(
            SimpleNamespace(
                evaluation_id=evaluation.evaluation_id,
                review_id="technical-review:text-route",
                disposition="no_change_required_within_verified_scope",
            ),
        )
    )
    review = SimpleNamespace(
        experiment_plan=SimpleNamespace(
            proposals=(),
            planned_count=0,
            registry_gap_count=0,
        ),
        question_evaluations=(evaluation,),
        technical_verification=technical,
    )
    change = _change_for("tests/test_code_public_route_experiments.py")
    selection = _selection("tests/test_code_public_route_experiments.py")

    gate, receipts = _experiment_gate(
        review,
        root=tmp_path,
        state_directory=tmp_path,
        change=change,
        selection=selection,
    )
    replay_gate = _replay_technical_disposition_gate(
        review,
        change=change,
        selection=selection,
    )

    assert gate.status == "passed"
    assert gate.reason == "affected_questions_have_verified_technical_dispositions"
    assert receipts == ()
    assert replay_gate.status == "passed"
    assert replay_gate.reason == "all_affected_questions_have_verified_technical_dispositions"


def test_unknown_question_contract_abstains_instead_of_becoming_advisory(
    tmp_path: Path,
) -> None:
    unknown_spec = replace(
        ROUTE_CAPABILITY_QUESTION,
        question_id="future.unclassified_acceptance_question",
    )
    evaluation = _question_evaluation(
        unknown_spec,
        evaluation_id="evaluation:unknown",
        subject_key="capability:route:text",
    )
    review = SimpleNamespace(
        experiment_plan=SimpleNamespace(
            proposals=(),
            planned_count=0,
            registry_gap_count=1,
        ),
        question_evaluations=(evaluation,),
        technical_verification=SimpleNamespace(reviews=()),
    )

    gate, receipts = _experiment_gate(
        review,
        root=tmp_path,
        state_directory=tmp_path,
        change=_change_for("docs/OPERATIONS.md"),
        selection=_selection(),
    )

    assert gate.status == "abstained"
    assert gate.reason == "change_question_relevance_unresolvable"
    assert gate.evidence["relevance_errors"] == [
        "unclassified_question_contract:future.unclassified_acceptance_question:v1"
    ]
    assert receipts == ()


def test_experiment_control_plane_change_binds_all_executable_question_scopes() -> None:
    evaluations = (
        _question_evaluation(
            ARCHITECTURE_CONTRACT_QUESTION,
            evaluation_id="evaluation:architecture-contract",
            subject_key="architecture:contract:fixture",
        ),
        _question_evaluation(
            ROUTE_CAPABILITY_QUESTION,
            evaluation_id="evaluation:route",
            subject_key="capability:route:text",
        ),
        _question_evaluation(
            WORKFLOW_SQL_QUESTION,
            evaluation_id="evaluation:sql",
            subject_key="workflow:text.derivation-publication:fixture",
        ),
        _question_evaluation(
            TEXT_SEMANTIC_PROJECTION_QUESTION,
            evaluation_id="evaluation:projection",
            subject_key="workflow:text-to-semantic-published-projection",
        ),
        _question_evaluation(
            CODE_SCHEMA_EVOLUTION_QUESTION,
            evaluation_id="evaluation:schema",
            subject_key="code-owner-schema-subject-v1:fixture",
        ),
        _question_evaluation(
            RETENTION_HOLD_QUESTION,
            evaluation_id="evaluation:retention",
            subject_key="retention:canonical-durable-holds",
        ),
        _question_evaluation(
            FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION,
            evaluation_id="evaluation:framework-review-task",
            subject_key="contract:framework-review-task-protocol",
        ),
        _question_evaluation(
            CLI_SURFACE_QUESTION,
            evaluation_id="evaluation:public-cli",
            subject_key="entrypoint:neocortex-interface-surface",
        ),
        _question_evaluation(
            KNOWLEDGE_ASSET_HEALTH_QUESTION,
            evaluation_id="evaluation:knowledge-asset-health",
            subject_key="capability:knowledge-asset-health",
        ),
        _question_evaluation(
            KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION,
            evaluation_id="evaluation:knowledge-pdf-asset-health",
            subject_key="capability:knowledge-asset-health:pdf",
        ),
    )
    review = SimpleNamespace(question_evaluations=evaluations)

    bindings, relevant, errors = _relevant_question_state(
        review,
        change=_change_for("_04_Nucleo_Operativo/code_change_validation.py"),
        selection=_selection(),
    )

    assert errors == ()
    assert {scope.scope_id for scope, _evaluation in relevant} == {
        "code_schema_migration",
        "declared_import_architecture_contracts",
        "public_text_route",
        "public_cli_contract",
        "durable_retention_holds",
        "framework_review_task_protocol",
        "knowledge_asset_health",
        "knowledge_pdf_asset_health",
        "text_publication_sql",
        "text_semantic_projection_recovery",
    }
    assert all(
        item["relevance"] == "affected"
        for item in bindings
        if item["scope_id"]
        in {
            "code_schema_migration",
            "declared_import_architecture_contracts",
            "public_text_route",
            "public_cli_contract",
            "durable_retention_holds",
            "framework_review_task_protocol",
            "knowledge_asset_health",
            "knowledge_pdf_asset_health",
            "text_publication_sql",
            "text_semantic_projection_recovery",
        }
    )


def test_review_task_protocol_change_binds_architecture_retention_and_its_exact_question() -> None:
    architecture = _question_evaluation(
        ARCHITECTURE_CONTRACT_QUESTION,
        evaluation_id="evaluation:architecture-contract",
        subject_key="architecture:contract:fixture",
    )
    retention = _question_evaluation(
        RETENTION_HOLD_QUESTION,
        evaluation_id="evaluation:retention",
        subject_key="retention:canonical-durable-holds",
    )
    review_task = _question_evaluation(
        FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION,
        evaluation_id="evaluation:framework-review-task",
        subject_key="contract:framework-review-task-protocol",
    )
    review = SimpleNamespace(question_evaluations=(architecture, retention, review_task))

    bindings, relevant, errors = _relevant_question_state(
        review,
        change=_change_for("_04_Nucleo_Operativo/review_task_repository.py"),
        selection=_selection("tests/test_review_tasks.py"),
    )

    assert errors == ()
    assert {scope.scope_id for scope, _evaluation in relevant} == {
        "declared_import_architecture_contracts",
        "durable_retention_holds",
        "framework_review_task_protocol",
    }
    binding = next(
        item for item in bindings if item["scope_id"] == "framework_review_task_protocol"
    )
    assert binding["matched_changed_paths"] == ["_04_Nucleo_Operativo/review_task_repository.py"]
    assert binding["matched_test_selectors"] == ["tests/test_review_tasks.py"]


def test_knowledge_asset_health_change_binds_its_exact_causal_question() -> None:
    health = _question_evaluation(
        KNOWLEDGE_ASSET_HEALTH_QUESTION,
        evaluation_id="evaluation:knowledge-asset-health",
        subject_key="capability:knowledge-asset-health",
    )
    architecture = _question_evaluation(
        ARCHITECTURE_CONTRACT_QUESTION,
        evaluation_id="evaluation:architecture-contract",
        subject_key="architecture:contract:fixture",
    )
    pdf_health = _question_evaluation(
        KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION,
        evaluation_id="evaluation:knowledge-pdf-asset-health",
        subject_key="capability:knowledge-asset-health:pdf",
    )
    review = SimpleNamespace(question_evaluations=(architecture, health, pdf_health))

    bindings, relevant, errors = _relevant_question_state(
        review,
        change=_change_for("_04_Nucleo_Operativo/knowledge_asset_health.py"),
        selection=_selection("tests/test_knowledge_asset_health.py"),
    )

    assert errors == ()
    assert {scope.scope_id for scope, _evaluation in relevant} == {
        "declared_import_architecture_contracts",
        "knowledge_asset_health",
        "knowledge_pdf_asset_health",
    }
    binding = next(item for item in bindings if item["scope_id"] == "knowledge_asset_health")
    assert binding["relevance"] == "affected"
    assert binding["matched_changed_paths"] == ["_04_Nucleo_Operativo/knowledge_asset_health.py"]
    assert binding["matched_test_selectors"] == ["tests/test_knowledge_asset_health.py"]


def test_pdf_asset_health_change_binds_only_its_exact_pdf_causal_question() -> None:
    pdf_health = _question_evaluation(
        KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION,
        evaluation_id="evaluation:knowledge-pdf-asset-health",
        subject_key="capability:knowledge-asset-health:pdf",
    )
    architecture = _question_evaluation(
        ARCHITECTURE_CONTRACT_QUESTION,
        evaluation_id="evaluation:architecture-contract",
        subject_key="architecture:contract:fixture",
    )
    review = SimpleNamespace(question_evaluations=(architecture, pdf_health))

    bindings, relevant, errors = _relevant_question_state(
        review,
        change=_change_for("_04_Nucleo_Operativo/knowledge_asset_health_pdf.py"),
        selection=_selection("tests/test_knowledge_asset_health_pdf.py"),
    )

    assert errors == ()
    assert {scope.scope_id for scope, _evaluation in relevant} == {
        "declared_import_architecture_contracts",
        "knowledge_pdf_asset_health",
    }
    binding = next(item for item in bindings if item["scope_id"] == "knowledge_pdf_asset_health")
    assert binding["relevance"] == "affected"
    assert binding["matched_changed_paths"] == [
        "_04_Nucleo_Operativo/knowledge_asset_health_pdf.py"
    ]
    assert binding["matched_test_selectors"] == ["tests/test_knowledge_asset_health_pdf.py"]


def test_retention_planner_change_makes_durable_hold_evidence_acceptance_critical() -> None:
    evaluation = _question_evaluation(
        RETENTION_HOLD_QUESTION,
        evaluation_id="evaluation:retention",
        subject_key="retention:canonical-durable-holds",
    )
    architecture = _question_evaluation(
        ARCHITECTURE_CONTRACT_QUESTION,
        evaluation_id="evaluation:architecture-contract",
        subject_key="architecture:contract:fixture",
    )
    review = SimpleNamespace(question_evaluations=(architecture, evaluation))

    bindings, relevant, errors = _relevant_question_state(
        review,
        change=_change_for("_04_Nucleo_Operativo/retention_planner.py"),
        selection=_selection("tests/test_retention_planner.py"),
    )

    assert errors == ()
    assert {scope.scope_id for scope, _evaluation in relevant} == {
        "declared_import_architecture_contracts",
        "durable_retention_holds",
    }
    binding = next(item for item in bindings if item["scope_id"] == "durable_retention_holds")
    assert binding["relevance"] == "affected"
    assert binding["matched_changed_paths"] == ["_04_Nucleo_Operativo/retention_planner.py"]


def test_production_python_change_makes_declared_architecture_contracts_acceptance_critical() -> (
    None
):
    evaluation = _question_evaluation(
        ARCHITECTURE_CONTRACT_QUESTION,
        evaluation_id="evaluation:architecture-contract",
        subject_key="architecture:contract:fixture",
    )
    review = SimpleNamespace(question_evaluations=(evaluation,))

    bindings, relevant, errors = _relevant_question_state(
        review,
        change=_change_for("neocortex/logic.py"),
        selection=_selection("tests/test_logic.py"),
    )

    assert errors == ()
    assert tuple(scope.scope_id for scope, _evaluation in relevant) == (
        "declared_import_architecture_contracts",
    )
    assert bindings[0]["relevance"] == "affected"
    assert bindings[0]["matched_changed_paths"] == ["neocortex/logic.py"]


def test_production_python_change_abstains_when_architecture_evaluation_is_missing() -> None:
    review = SimpleNamespace(question_evaluations=())

    _bindings, relevant, errors = _relevant_question_state(
        review,
        change=_change_for("neocortex/logic.py"),
        selection=_selection("tests/test_logic.py"),
    )

    assert relevant == ()
    assert errors == (
        "affected_question_evaluation_missing:declared_import_architecture_contracts",
    )


def test_replay_cannot_pass_until_relevant_technical_disposition_exists() -> None:
    evaluation = _question_evaluation(
        ROUTE_CAPABILITY_QUESTION,
        evaluation_id="evaluation:text-route",
        subject_key="capability:route:text",
        readiness="human_review_required",
    )
    review = SimpleNamespace(
        question_evaluations=(evaluation,),
        technical_verification=SimpleNamespace(reviews=()),
    )

    gate = _replay_technical_disposition_gate(
        review,
        change=_change_for("tests/test_code_public_route_experiments.py"),
        selection=_selection("tests/test_code_public_route_experiments.py"),
    )

    assert gate.status == "abstained"
    assert gate.reason == ("affected_question_lacks_verified_technical_disposition_after_replay")
    assert gate.evidence["unresolved_relevant_evaluations"] == [
        "public_text_route:evaluation:text-route"
    ]


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(("git", *arguments), cwd=root, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "Repository"
    (root / "neocortex").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "neocortex" / "logic.py").write_text("def choose():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_logic.py").write_text(
        "from neocortex.logic import choose\n\ndef test_choose():\n    assert choose() == 1\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    return root


def test_git_change_includes_tracked_and_untracked_content(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "neocortex" / "logic.py").write_text("def choose():\n    return 2\n", encoding="utf-8")
    (root / "tests" / "test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")

    first = capture_git_change(root)
    second = capture_git_change(root)

    assert first == second
    assert first.changed_paths == ("neocortex/logic.py", "tests/test_new.py")
    assert first.untracked_paths == ("tests/test_new.py",)
    assert len(first.content_digest) == 64


def test_git_change_preserves_both_sides_of_a_renamed_acceptance_boundary(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    schema = root / "_04_Nucleo_Operativo" / "code_schema.py"
    schema.parent.mkdir()
    schema.write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "add schema boundary")
    renamed = root / "neocortex" / "renamed_schema.py"
    schema.rename(renamed)

    change = capture_git_change(root)
    selection = _selection("tests/test_code_schema_migration_v1_v2.py")
    scope = next(
        item for item in _validation_question_scopes() if item.scope_id == "code_schema_migration"
    )

    matched_paths, _matched_selectors = _scope_relevance(scope, change, selection)

    assert change.changed_paths == (
        "_04_Nucleo_Operativo/code_schema.py",
        "neocortex/renamed_schema.py",
    )
    assert matched_paths == ("_04_Nucleo_Operativo/code_schema.py",)


def test_selection_preserves_changed_tests_and_convention(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("neocortex/logic.py", "tests/test_logic.py"),
        (),
        (),
        (),
        "b" * 64,
    )

    selection = select_affected_tests(root, tmp_path / "missing-state", change)

    assert selection.strategy == "affected"
    assert selection.selectors == ("tests/test_logic.py",)
    assert selection.direct_tests == ("tests/test_logic.py",)


def test_packaging_boundary_selects_full_suite(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("pyproject.toml",),
        (),
        (),
        (),
        "b" * 64,
    )

    selection = select_affected_tests(root, tmp_path / "state", change)

    assert selection.strategy == "full"
    assert selection.selectors == ("tests/test_logic.py",)
    assert selection.direct_tests == ()
    assert selection.dependency_tests == ()
    assert selection.convention_tests == ()
    assert selection.uncovered_sources == ()
    assert selection.reasons == ("change_crosses_full_suite_boundary",)

    lock_selection = select_affected_tests(
        root,
        tmp_path / "state",
        _change_for("constraints-linux-cp314.lock"),
    )
    assert lock_selection.strategy == "full"
    assert lock_selection.reasons == ("change_crosses_full_suite_boundary",)


def test_full_suite_selection_cannot_publish_module_prefixes_as_uncovered_sources() -> None:
    with pytest.raises(ValueError, match="not a Python source path"):
        AffectedTestSelection(
            strategy="full",
            selectors=("tests/test_logic.py",),
            direct_tests=(),
            dependency_tests=(),
            convention_tests=(),
            uncovered_sources=("_04_Nucleo_Operativo/semantic_",),
            reasons=("change_crosses_full_suite_boundary",),
        )


def test_validation_preserves_full_suite_strategy_without_a_false_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    boundary = root / "_04_Nucleo_Operativo" / "semantic_text_index.py"
    boundary.parent.mkdir()
    boundary.write_text("PIPELINE = 'fixture'\n", encoding="utf-8")
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("_04_Nucleo_Operativo/semantic_text_index.py",),
        (),
        (),
        (),
        "b" * 64,
    )
    from _04_Nucleo_Operativo import code_change_validation

    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: change,
    )

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        return subprocess.CompletedProcess(command, 1, "", "fixture stop after selection")

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        runner=runner,
    )

    assert result.status == "failed"
    assert result.selection.strategy == "full"
    assert result.selection.selectors == ("tests/test_logic.py",)
    assert result.selection.dependency_tests == ()
    assert result.selection.convention_tests == ()
    assert result.selection.uncovered_sources == ("_04_Nucleo_Operativo/semantic_text_index.py",)
    assert "change_crosses_full_suite_boundary" in result.selection.reasons
    assert "published_import_graph_stale_for_changed_source" in result.selection.reasons


def test_code_schema_boundary_selects_its_bounded_compatibility_matrix(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    schema = root / "_04_Nucleo_Operativo" / "code_schema.py"
    schema.parent.mkdir()
    schema.write_text("SCHEMA_VERSION = 2\n", encoding="utf-8")
    expected = (
        "tests/test_code_change_evolution_analysis.py",
        "tests/test_code_experiment_store.py",
        "tests/test_code_intelligence.py",
        "tests/test_code_publication_diff.py",
        "tests/test_code_schema_migration_v1_v2.py",
        "tests/test_code_schema_migration_v6_v7.py",
        "tests/test_external_provider_schema_v4.py",
        "tests/test_framework_code_path_collation.py",
    )
    for relative in expected:
        (root / relative).write_text("def test_schema_boundary(): pass\n", encoding="utf-8")
    (root / "tests" / "test_unrelated_media_route.py").write_text(
        "def test_unrelated(): pass\n",
        encoding="utf-8",
    )
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("_04_Nucleo_Operativo/code_schema.py",),
        (),
        (),
        (),
        "b" * 64,
    )

    selection = select_affected_tests(root, tmp_path / "state", change)

    assert selection.strategy == "affected"
    assert selection.selectors == expected
    assert selection.convention_tests == expected
    assert selection.uncovered_sources == ()
    assert "tests/test_unrelated_media_route.py" not in selection.selectors
    assert "change_crosses_full_suite_boundary" not in selection.reasons


def test_quality_gate_source_selects_its_bounded_compatibility_matrix(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    gate = root / "tools" / "quality_gate.py"
    gate.parent.mkdir()
    gate.write_text("def run(): pass\n", encoding="utf-8")
    expected = (
        "tests/test_code_change_validation.py",
        "tests/test_packaging_entrypoint.py",
        "tests/test_quality_gate.py",
        "tests/test_release_artifacts.py",
        "tests/test_release_linux.py",
    )
    for relative in expected:
        (root / relative).write_text("def test_quality_boundary(): pass\n", encoding="utf-8")
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("tools/quality_gate.py",),
        (),
        (),
        (),
        "b" * 64,
    )

    selection = select_affected_tests(root, tmp_path / "state", change)

    assert selection.strategy == "affected"
    assert selection.selectors == expected
    assert selection.convention_tests == expected
    assert selection.uncovered_sources == ()
    assert "change_crosses_full_suite_boundary" not in selection.reasons


@pytest.mark.parametrize(
    ("relative", "capability_id", "shared_expected"),
    (
        ("_04_Nucleo_Operativo/docx_route.py", "docx", None),
        (
            "_04_Nucleo_Operativo/capabilities/formats/docx/route.py",
            "docx",
            None,
        ),
        (
            "_04_Nucleo_Operativo/platform/shared/zip_safety.py",
            None,
            (
                "tests/test_bounded_io_refactors.py",
                "tests/test_format_module_move_compatibility.py",
                "tests/test_zip_safety.py",
            ),
        ),
        (
            "_04_Nucleo_Operativo/zip_safety.py",
            None,
            (
                "tests/test_bounded_io_refactors.py",
                "tests/test_format_module_move_compatibility.py",
                "tests/test_zip_safety.py",
            ),
        ),
        (
            "_04_Nucleo_Operativo/content_types.py",
            None,
            (
                "tests/test_bounded_io_refactors.py",
                "tests/test_format_module_move_compatibility.py",
                "tests/test_framework_actions.py",
                "tests/test_video_content_types.py",
            ),
        ),
        (
            "_04_Nucleo_Operativo/platform/shared/content_types.py",
            None,
            (
                "tests/test_bounded_io_refactors.py",
                "tests/test_format_module_move_compatibility.py",
                "tests/test_framework_actions.py",
                "tests/test_video_content_types.py",
            ),
        ),
        ("_04_Nucleo_Operativo/archive_route.py", "archive", None),
        (
            "_04_Nucleo_Operativo/capabilities/formats/archive/route.py",
            "archive",
            None,
        ),
        ("_04_Nucleo_Operativo/image_route.py", "image", None),
        (
            "_04_Nucleo_Operativo/capabilities/formats/image/route.py",
            "image",
            None,
        ),
    ),
)
def test_format_boundaries_select_registry_matrices_without_a_published_graph(
    tmp_path: Path,
    relative: str,
    capability_id: str | None,
    shared_expected: tuple[str, ...] | None,
) -> None:
    root = _repository(tmp_path)
    expected = (
        CAPABILITY_REGISTRY.by_id(capability_id).test_roots
        if capability_id is not None
        else shared_expected
    )
    assert expected is not None
    source = root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("CONTRACT = 'fixture'\n", encoding="utf-8")
    for test_path in expected:
        target = root / test_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_contract(): pass\n", encoding="utf-8")
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        (relative,),
        (),
        (),
        (),
        "b" * 64,
    )

    selection = select_affected_tests(root, tmp_path / "missing-state", change)

    assert selection.strategy == "affected"
    assert selection.selectors == expected
    assert selection.convention_tests == expected
    assert selection.uncovered_sources == ()
    assert "published_import_graph_unavailable" in selection.reasons


def test_format_boundary_rejects_a_partial_registry_test_matrix(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    relative = "_04_Nucleo_Operativo/capabilities/formats/docx/route.py"
    source = root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("CONTRACT = 'fixture'\n", encoding="utf-8")
    expected = CAPABILITY_REGISTRY.by_id("docx").test_roots
    for test_path in expected[:-1]:
        target = root / test_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_contract(): pass\n", encoding="utf-8")
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        (relative,),
        (),
        (),
        (),
        "b" * 64,
    )

    with pytest.raises(
        ChangeValidationError,
        match=(
            r"source_boundary_test_evidence_unavailable:"
            r"tests/test_pdf_docx_schema_contracts\.py"
        ),
    ):
        select_affected_tests(root, tmp_path / "missing-state", change)


def test_full_suite_excludes_retired_windows_runtime_but_keeps_portable_usn(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "tests" / "test_release_windows.py").write_text(
        "raise AssertionError('retired Windows test must not execute')\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_synthetic_usn.py").write_text(
        "def test_portable_fixture(): pass\n",
        encoding="utf-8",
    )
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("pyproject.toml",),
        (),
        (),
        (),
        "b" * 64,
    )

    selection = select_affected_tests(root, tmp_path / "state", change)

    assert selection.selectors == (
        "tests/test_logic.py",
        "tests/test_synthetic_usn.py",
    )
    assert "tests/test_release_windows.py" not in selection.selectors


def test_rename_does_not_turn_an_abstention_into_success(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("neocortex/logic.py",),
        (),
        (),
        (),
        "b" * 64,
    )
    before = select_affected_tests(root, tmp_path / "state", change)
    (root / "neocortex" / "logic.py").rename(root / "neocortex" / "renamed.py")
    renamed = replace(change, changed_paths=("neocortex/renamed.py",))
    after = select_affected_tests(root, tmp_path / "state", renamed)

    assert before.strategy == "affected"
    assert before.selectors == ("tests/test_logic.py",)
    assert after.strategy == "none"
    assert after.selectors == ()
    assert after.uncovered_sources == ("neocortex/renamed.py",)
    assert "no_affected_test_evidence" in after.reasons


def test_clean_tree_is_a_verified_noop_without_running_external_gates(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
    )

    assert result.status == "passed"
    assert result.reason is None
    assert result.selection.strategy == "none"
    assert tuple(item.gate_id for item in result.gates) == ("source_change_present",)
    assert result.gates[0].status == "not_required"
    assert result.experiment_receipts == ()
    assert result.mutation_authority is False
    assert result.digest.startswith("sha256:")


def test_dirty_tree_fails_before_any_external_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    from _04_Nucleo_Operativo import code_change_validation

    change = GitChangeSnapshot(
        "b" * 40,
        "a" * 40,
        ("neocortex/logic.py",),
        (),
        (),
        ("neocortex/logic.py",),
        "c" * 64,
    )
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: change,
    )
    commands: list[tuple[str, ...]] = []

    def runner(arguments, *, cwd, timeout, environment=None):
        commands.append(tuple(str(item) for item in arguments))
        raise AssertionError("dirty validation must not start an external gate")

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        runner=runner,
    )

    assert result.status == "failed"
    assert result.reason == "failed_gate:clean_source_sha"
    assert result.gates[0].reason == "worktree_contains_uncommitted_changes"
    assert result.gates[0].evidence["dirty_paths"] == ["neocortex/logic.py"]
    assert commands == []


def test_non_executable_change_never_expands_empty_selection_to_full_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    from _04_Nucleo_Operativo import code_change_validation

    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("docs/OPERATIONS.md",),
        (),
        (),
        (),
        "b" * 64,
    )
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: change,
    )
    monkeypatch.setattr(code_change_validation, "_capture_unchanged", lambda *_args: True)
    observed_commands: list[tuple[str, ...]] = []

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        observed_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "fixture passed", "")

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        runner=runner,
    )

    assert result.status == "passed"
    assert result.selection.strategy == "none"
    assert tuple(item.gate_id for item in result.gates) == (
        "static_no_regression",
        "architecture_contracts",
        "affected_coverage",
        "trusted_deep_publication",
        "source_snapshot_unchanged",
    )
    assert result.gates[2].status == "not_required"
    assert result.gates[3].status == "not_required"
    assert not any("--analysis-profile" in command for command in observed_commands)


def test_unknown_change_with_empty_selection_abstains_without_running_full_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    from _04_Nucleo_Operativo import code_change_validation

    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("tools/maintenance.sh",),
        (),
        (),
        (),
        "b" * 64,
    )
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: change,
    )
    monkeypatch.setattr(code_change_validation, "_capture_unchanged", lambda *_args: True)
    observed_commands: list[tuple[str, ...]] = []

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        observed_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "fixture passed", "")

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        runner=runner,
    )

    assert result.status == "abstained"
    assert result.reason == "abstained_gate:affected_coverage"
    assert result.gates[2].reason == "no_affected_test_evidence"
    assert result.gates[3].reason == "empty_selection_must_not_expand_to_full_suite"
    assert not any("--analysis-profile" in command for command in observed_commands)


def test_partial_affected_evidence_is_not_silently_green(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "neocortex" / "uncovered.py").write_text("VALUE = 1\n", encoding="utf-8")
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("neocortex/logic.py", "neocortex/uncovered.py"),
        (),
        (),
        (),
        "b" * 64,
    )

    selection = select_affected_tests(root, tmp_path / "state", change)

    assert selection.strategy == "affected"
    assert selection.selectors == ("tests/test_logic.py",)
    assert selection.uncovered_sources == ("neocortex/uncovered.py",)
    assert "some_changed_sources_lack_affected_test_evidence" in selection.reasons


def test_changed_source_never_trusts_a_stale_published_import_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    (root / "tests" / "test_import_consumer.py").write_text(
        "from neocortex.logic import choose\n\ndef test_consumer(): assert choose() == 1\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_packaging_entrypoint.py").write_text(
        "def test_public_boundary(): pass\n",
        encoding="utf-8",
    )
    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("neocortex/logic.py",),
        (),
        (),
        (),
        "b" * 64,
    )
    from _04_Nucleo_Operativo import code_change_validation

    monkeypatch.setattr(
        code_change_validation,
        "select_affected_tests",
        lambda *_args, **_kwargs: code_change_validation.AffectedTestSelection(
            strategy="affected",
            selectors=("tests/test_import_consumer.py",),
            direct_tests=(),
            dependency_tests=("tests/test_import_consumer.py",),
            convention_tests=(),
            uncovered_sources=(),
            reasons=(),
        ),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_unpublished_source_paths",
        lambda *_args, **_kwargs: ("neocortex/logic.py",),
    )
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: change,
    )
    observed_commands: list[tuple[str, ...]] = []

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        observed_commands.append(command)
        return subprocess.CompletedProcess(command, 1, "", "stop after selection")

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        runner=runner,
    )

    assert result.status == "failed"
    assert "published_import_graph_stale_for_changed_source" in result.selection.reasons
    assert result.selection.uncovered_sources == ("neocortex/logic.py",)
    assert "tests/test_import_consumer.py" in result.selection.selectors
    assert "tests/test_packaging_entrypoint.py" in result.selection.selectors
    assert not any("--analysis-profile" in command for command in observed_commands)


def test_validation_fallback_stays_bounded_and_runs_public_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    (root / "neocortex" / "uncovered.py").write_text("VALUE = 1\n", encoding="utf-8")
    for name in (
        "test_cli_code_surface.py",
        "test_code_review_epistemics.py",
        "test_packaging_entrypoint.py",
    ):
        (root / "tests" / name).write_text("def test_boundary(): pass\n", encoding="utf-8")

    from _04_Nucleo_Operativo import code_change_validation

    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("neocortex/uncovered.py",),
        (),
        (),
        (),
        "b" * 64,
    )
    monkeypatch.setattr(
        code_change_validation, "capture_git_change", lambda *_args, **_kwargs: change
    )
    observed_commands: list[tuple[str, ...]] = []

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        observed_commands.append(command)
        if "static" in command or "architecture" in command:
            return subprocess.CompletedProcess(command, 0, "fixture passed", "")
        return subprocess.CompletedProcess(command, 1, "", "fixture stop after selection")

    monkeypatch.setattr(
        code_change_validation,
        "_fresh_review_gate",
        lambda *_args, **_kwargs: (
            code_change_validation.ValidationGate(
                "autoanalysis_verdict", "abstained", "fixture", 0, (), {}
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_candidate_wheel_gate",
        lambda *_args, **_kwargs: code_change_validation.ValidationGate(
            "candidate_wheel_smoke", "abstained", "fixture", 0, (), {}
        ),
    )

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        runner=runner,
    )

    assert result.status == "failed"
    assert result.selection.strategy == "affected"
    assert result.selection.selectors == (
        "tests/test_cli_code_surface.py",
        "tests/test_code_review_epistemics.py",
        "tests/test_packaging_entrypoint.py",
    )
    producer = next(command for command in observed_commands if "--analysis-profile" in command)
    assert producer.count("--deep-test-selector") == 3
    assert producer[producer.index("--deep-shard-size") + 1] == "50"


def test_primary_keeps_its_budget_and_replay_has_a_bounded_closure_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    root = _repository(tmp_path)
    change = _change_for("neocortex/logic.py")
    selection = _selection("tests/test_logic.py")

    def passed(gate_id: str) -> object:
        return code_change_validation.ValidationGate(
            gate_id,
            "passed",
            "fixture_passed",
            0,
            (),
            {},
        )

    review = SimpleNamespace(experiment_plan=SimpleNamespace(proposals=()))
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: change,
    )
    monkeypatch.setattr(
        code_change_validation,
        "select_affected_tests",
        lambda *_args, **_kwargs: selection,
    )
    monkeypatch.setattr(
        code_change_validation,
        "_unpublished_source_paths",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_fresh_review_gate",
        lambda *_args, **_kwargs: (passed("autoanalysis_verdict"), review),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_coverage_gate",
        lambda *_args, **_kwargs: passed("affected_coverage"),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_experiment_gate",
        lambda *_args, **_kwargs: (passed("allowlisted_experiments"), ()),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_candidate_wheel_gate",
        lambda *_args, **_kwargs: passed("candidate_wheel_smoke"),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_replay_gate",
        lambda *_args, **_kwargs: passed("trusted_deep_replay"),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_replay_technical_disposition_gate",
        lambda *_args, **_kwargs: passed("replay_technical_disposition"),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_public_review_stability_gate",
        lambda *_args, **_kwargs: passed("public_review_stability"),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_capture_unchanged",
        lambda *_args, **_kwargs: True,
    )
    observed: list[tuple[tuple[str, ...], float, dict[str, str]]] = []

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        observed.append((command, timeout, dict(environment or {})))
        return subprocess.CompletedProcess(command, 0, "fixture passed", "")

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        time_budget_seconds=30,
        runner=runner,
    )

    assert result.status == "passed"
    trusted_deep = tuple(item for item in observed if "--analysis-profile" in item[0])
    assert len(trusted_deep) == 2
    assert trusted_deep[0][1] == 960
    assert trusted_deep[1][1] == 1_200
    assert trusted_deep[0][2] == trusted_deep[1][2]
    assert all(item[2]["NEOCORTEX_PROGRESS_STREAM"] == "1" for item in trusted_deep)
    assert all(item[2]["PYTHONDONTWRITEBYTECODE"] == "1" for item in trusted_deep)


def test_replay_budget_reserves_finalization_inside_the_global_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation
    from _04_Nucleo_Operativo.code_validation_resources import (
        CodeValidationRuntimeWindow,
    )

    active = 10_000_000_000
    hard = active + 4_500 * 1_000_000_000
    window = CodeValidationRuntimeWindow(
        "neocortex-code-validate-123-aaaaaaaaaaaa",
        active,
        hard,
        4_500,
    )
    required = 1_200 + 300

    monkeypatch.setattr(
        code_change_validation.time,
        "monotonic_ns",
        lambda: hard - (required + 1) * 1_000_000_000,
    )
    admitted = code_change_validation._runtime_budget_gate(
        "trusted_deep_replay_budget",
        window,
        required_seconds=required,
        command=("Neocortex", "--self-analysis"),
    )
    assert admitted.status == "passed"
    assert admitted.evidence["hard_remaining_seconds"] == required + 1

    monkeypatch.setattr(
        code_change_validation.time,
        "monotonic_ns",
        lambda: hard - (required - 1) * 1_000_000_000,
    )
    rejected = code_change_validation._runtime_budget_gate(
        "trusted_deep_replay_budget",
        window,
        required_seconds=required,
        command=("Neocortex", "--self-analysis"),
    )
    assert rejected.status == "abstained"
    assert rejected.reason == "insufficient_global_runtime_for_remaining_phases"
    assert rejected.evidence["shortfall_seconds"] == 1


def test_candidate_subprocess_timeout_consumes_only_surplus_before_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation
    from _04_Nucleo_Operativo.code_validation_resources import (
        CodeValidationRuntimeWindow,
    )

    active = 10_000_000_000
    hard = active + 4_500 * 1_000_000_000
    window = CodeValidationRuntimeWindow(
        "neocortex-code-validate-123-aaaaaaaaaaaa",
        active,
        hard,
        4_500,
    )
    reserve = 1_200 + 180
    monkeypatch.setattr(
        code_change_validation.time,
        "monotonic_ns",
        lambda: hard - (reserve + 95) * 1_000_000_000,
    )

    assert (
        code_change_validation._bounded_runtime_timeout(
            window,
            maximum_seconds=600,
            reserve_seconds=reserve,
        )
        == 95
    )
    assert (
        code_change_validation._bounded_runtime_timeout(
            None,
            maximum_seconds=600,
            reserve_seconds=reserve,
        )
        == 600
    )

    monkeypatch.setattr(
        code_change_validation.time,
        "monotonic_ns",
        lambda: hard - reserve * 1_000_000_000,
    )
    with pytest.raises(
        code_change_validation.ChangeValidationError,
        match="code_validation_global_runtime_reserve_unavailable",
    ):
        code_change_validation._bounded_runtime_timeout(
            window,
            maximum_seconds=600,
            reserve_seconds=reserve,
        )


def test_failed_replay_publication_stops_before_replay_review_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_change_validation

    root = _repository(tmp_path)
    change = _change_for("neocortex/logic.py")
    selection = _selection("tests/test_logic.py")

    def passed(gate_id: str) -> object:
        return code_change_validation.ValidationGate(
            gate_id,
            "passed",
            "fixture_passed",
            0,
            (),
            {},
        )

    review = SimpleNamespace(experiment_plan=SimpleNamespace(proposals=()))
    review_reads: list[int] = []

    def review_gate(*_args, **_kwargs):
        review_reads.append(1)
        return passed("autoanalysis_verdict"), review

    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: change,
    )
    monkeypatch.setattr(
        code_change_validation,
        "select_affected_tests",
        lambda *_args, **_kwargs: selection,
    )
    monkeypatch.setattr(
        code_change_validation,
        "_unpublished_source_paths",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(code_change_validation, "_fresh_review_gate", review_gate)
    monkeypatch.setattr(
        code_change_validation,
        "_coverage_gate",
        lambda *_args, **_kwargs: passed("affected_coverage"),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_experiment_gate",
        lambda *_args, **_kwargs: (passed("allowlisted_experiments"), ()),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_candidate_wheel_gate",
        lambda *_args, **_kwargs: passed("candidate_wheel_smoke"),
    )
    monkeypatch.setattr(
        code_change_validation,
        "_capture_unchanged",
        lambda *_args, **_kwargs: True,
    )
    trusted_runs = 0

    def runner(arguments, *, cwd, timeout, environment=None):
        nonlocal trusted_runs
        command = tuple(str(item) for item in arguments)
        if "--analysis-profile" in command:
            trusted_runs += 1
            return subprocess.CompletedProcess(
                command,
                0 if trusted_runs == 1 else 2,
                "fixture",
                "replay failed",
            )
        return subprocess.CompletedProcess(command, 0, "fixture", "")

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        time_budget_seconds=30,
        runner=runner,
    )

    assert result.status == "failed"
    assert result.reason == "failed_gate:trusted_deep_replay_publication"
    assert review_reads == [1]
    assert trusted_runs == 2
    assert "autoanalysis_replay_verdict" not in {gate.gate_id for gate in result.gates}


def test_failed_static_gate_stops_before_trusted_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    (root / "neocortex" / "logic.py").write_text(
        "def choose():\n    return 2\n",
        encoding="utf-8",
    )
    observed_commands: list[tuple[str, ...]] = []

    def runner(arguments, *, cwd, timeout, environment=None):
        command = tuple(str(item) for item in arguments)
        observed_commands.append(command)
        if command[:2] == ("git", "rev-parse"):
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[:2] == ("git", "diff") or command[:2] == ("git", "ls-files"):
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 2, "", "static fixture failure")

    change = GitChangeSnapshot(
        "a" * 40,
        "a" * 40,
        ("neocortex/logic.py",),
        (),
        (),
        (),
        "b" * 64,
    )
    from _04_Nucleo_Operativo import code_change_validation

    monkeypatch.setattr(
        code_change_validation, "capture_git_change", lambda *_args, **_kwargs: change
    )

    result = validate_code_change(
        root=root,
        state_directory=tmp_path / "state",
        runner=runner,
    )

    assert result.status == "failed"
    assert result.reason == "failed_gate:static_no_regression"
    assert tuple(item.gate_id for item in result.gates) == (
        "static_no_regression",
        "source_snapshot_unchanged",
    )
    assert not any("--analysis-profile" in command for command in observed_commands)
