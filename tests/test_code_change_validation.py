"""Contracts for the canonical local Linux source-change gate."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from _04_Nucleo_Operativo.code_change_validation import (
    AffectedTestSelection,
    GitChangeSnapshot,
    _experiment_gate,
    _fresh_review_gate,
    _provider_failure,
    _relevant_question_state,
    _replay_gate,
    _replay_technical_disposition_gate,
    _scope_relevance,
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
from _04_Nucleo_Operativo.code_change_evolution_analysis import (
    CODE_SCHEMA_EVOLUTION_QUESTION,
)
from _04_Nucleo_Operativo.external_evidence_providers import (
    INSTALLED_PACKAGE_PROVIDER_ID,
    PIP_AUDIT_PROVIDER_ID,
)
from _04_Nucleo_Operativo.code_route_capability_analysis import ROUTE_CAPABILITY_QUESTION
from _04_Nucleo_Operativo.code_state_interaction_analysis import WORKFLOW_SQL_QUESTION
from _04_Nucleo_Operativo.code_state_projection_analysis import (
    TEXT_SEMANTIC_PROJECTION_QUESTION,
)


def test_optional_mutation_abstention_is_not_a_failed_machine_gate() -> None:
    provider = SimpleNamespace(
        provider_id="cosmic-ray-focal-mutation",
        status="abstained",
        gate="not_evaluated",
        reason="provider_abstained:mutation_target_not_declared",
    )

    assert _provider_failure(provider) is False


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
        as_payload=lambda: {"schema": "neocortex.code-review/v18"},
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
        as_payload=lambda: {"schema": "neocortex.code-review/v18"},
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
        digest=SimpleNamespace(),
    )
    receipt = SimpleNamespace(
        receipt_id="receipt:exact",
        status="passed",
        as_payload=lambda: {"receipt_id": "receipt:exact", "status": "passed"},
    )
    stored = SimpleNamespace(receipt=receipt)
    observed: list[tuple[object, ...]] = []

    monkeypatch.setattr(executor, "execute_code_experiment", lambda *args, **kwargs: receipt)
    monkeypatch.setattr(store, "code_review_digest_identity", lambda _digest: "review:exact")

    def record(*args, **kwargs):
        observed.append((*args, kwargs))
        return stored

    monkeypatch.setattr(store, "record_code_experiment_receipt", record)

    gate, receipts = _experiment_gate(
        review,
        root=tmp_path,
        state_directory=tmp_path,
        change=_change_for("_04_Nucleo_Operativo/code_route_capability_analysis.py"),
        selection=_selection("tests/test_code_public_route_experiments.py"),
    )

    assert gate.status == "passed"
    assert gate.evidence["stored_receipt_ids"] == ["receipt:exact"]
    assert receipts == ({"receipt_id": "receipt:exact", "status": "passed"},)
    assert len(observed) == 1
    assert observed[0][0] == tmp_path / "code.sqlite3"
    assert observed[0][-1] == {
        "analysis_run_id": 17,
        "processing_signature": "snapshot:exact",
        "review_digest": "review:exact",
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
        change=_change_for("_04_Nucleo_Operativo/code_schema.py"),
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
        change=_change_for("neocortex/logic.py"),
        selection=_selection("tests/test_logic.py"),
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
    change = _change_for("_04_Nucleo_Operativo/code_route_capability_analysis.py")
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
        change=_change_for("neocortex/logic.py"),
        selection=_selection("tests/test_logic.py"),
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
        "public_text_route",
        "text_publication_sql",
        "text_semantic_projection_recovery",
    }
    assert all(
        item["relevance"] == "affected"
        for item in bindings
        if item["scope_id"]
        in {
            "code_schema_migration",
            "public_text_route",
            "text_publication_sql",
            "text_semantic_projection_recovery",
        }
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
        change=_change_for("_04_Nucleo_Operativo/code_route_capability_analysis.py"),
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
    assert result.selection.uncovered_sources == ()
    assert result.selection.reasons == ("change_crosses_full_suite_boundary",)


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
