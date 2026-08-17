from __future__ import annotations

import sqlite3
import zlib
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import _04_Nucleo_Operativo.code_schema as code_schema
import _04_Nucleo_Operativo.code_experiment_store as experiment_store

from _04_Nucleo_Operativo.code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisEvidenceRequirementSpec,
    AnalysisFact,
    AnalysisNextActionSpec,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    AnalysisSubjectRef,
    analysis_identity,
    analysis_question_spec_fingerprint,
    validate_analysis_question_evaluation,
)
from _04_Nucleo_Operativo.code_experiment_executor import (
    CODE_EXPERIMENT_RECEIPT_SCHEMA,
    CODE_EXPERIMENT_RECEIPT_V3_SCHEMA,
    CodeExperimentGateOutcome,
    CodeExperimentOutcome,
    CodeExperimentReceipt,
)
from _04_Nucleo_Operativo.code_experiment_planner import plan_code_experiments
from _04_Nucleo_Operativo.code_experiment_store import (
    CodeExperimentStoreError,
    apply_code_experiment_receipts,
    parse_resolved_code_experiment_receipt_payload,
    read_code_experiment_receipts,
    record_code_experiment_receipt,
    record_code_experiment_receipts,
)
from _04_Nucleo_Operativo.code_invariant_contracts import runtime_scenario
from _04_Nucleo_Operativo.code_interface_surface_analysis import (
    CLI_SURFACE_QUESTION,
    interface_surface_questions,
    read_code_interface_surface_analysis,
)
from _04_Nucleo_Operativo.code_knowledge_asset_health_analysis import (
    knowledge_asset_health_questions,
)
from _04_Nucleo_Operativo.code_knowledge_pdf_asset_health_analysis import (
    knowledge_pdf_asset_health_questions,
)
from _04_Nucleo_Operativo.code_route_capability_analysis import ROUTE_CAPABILITY_QUESTION
from _04_Nucleo_Operativo.code_state_interaction_analysis import (
    analyze_code_state_interactions,
    state_interaction_questions,
)
from _04_Nucleo_Operativo.code_schema import (
    connect_code_state,
    initialize_code_state,
    readonly_code_database,
)
from _04_Nucleo_Operativo.code_technical_verification import (
    build_code_technical_verification,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_text


def _source_evidence(subject_key: str, snapshot_id: str) -> AnalysisEvidenceRef:
    return AnalysisEvidenceRef(
        evidence_id="contract:route-fixture",
        subject_key=subject_key,
        role="supporting",
        evidence_kind="contract",
        source_owner_id="code",
        producer_id="fixture-contract-resolver",
        producer_version="v1",
        source_schema="fixture/v1",
        source_record_kind="contract",
        source_record_id="route:text",
        source_projection_digest="projection:route:text",
        snapshot_id=snapshot_id,
        revision_id=None,
        facts=(AnalysisFact("route", "text"),),
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id="fixture-resolver",
        resolver_version="v1",
        limitations=("fixture_contract_only",),
    )


def _question(
    snapshot_id: str = "snapshot:fixture",
) -> tuple[AnalysisQuestionSpec, AnalysisQuestionEvaluation]:
    subject_key = "capability:route:text"
    spec = AnalysisQuestionSpec(
        question_id="capability.route_reaches_user_visible_outcome",
        version="v1",
        subject_kinds=("capability",),
        requirements=(
            AnalysisEvidenceRequirementSpec(
                "route_runtime_and_state_contract_resolved",
                "question",
                "supporting",
                ("contract",),
            ),
            AnalysisEvidenceRequirementSpec(
                "causal_durable_execution_path_observed",
                "decision",
                "supporting",
                ("experiment_result",),
            ),
            AnalysisEvidenceRequirementSpec(
                "public_read_consumer_observed",
                "decision",
                "supporting",
                ("experiment_result",),
            ),
            AnalysisEvidenceRequirementSpec(
                "public_acceptance_scenario_observed",
                "decision",
                "experiment_result",
                ("experiment_result",),
            ),
            AnalysisEvidenceRequirementSpec(
                "declaration_only_counterevidence_evaluated",
                "decision",
                "counterevidence",
                ("runtime_observation",),
            ),
        ),
        hypotheses=("route_is_only_declared", "route_reaches_user_visible_value"),
        counterevidence_rules=("declaration_without_execution_is_not_reachability",),
        next_actions=(
            AnalysisNextActionSpec(
                "exercise_text_capability_from_public_entrypoint",
                "experiment",
                "Exercise the exact bounded public Text route.",
            ),
        ),
    )
    evidence = _source_evidence(subject_key, snapshot_id)
    counterevidence = replace(
        evidence,
        evidence_id="counterevidence:route-fixture",
        role="counterevidence",
        evidence_kind="runtime_observation",
        producer_id="fixture-counterevidence-resolver",
        source_schema="fixture-runtime/v1",
        source_record_kind="declaration_counterevidence",
        source_record_id="counterevidence:text",
        source_projection_digest="projection:counterevidence:text",
        facts=(AnalysisFact("declaration_without_execution", True),),
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id="evaluation:route:text",
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=1,
        subject=AnalysisSubjectRef(
            "capability",
            subject_key,
            "Text route",
            "code",
            snapshot_id,
            "current",
        ),
        evidence=(evidence, counterevidence),
        requirements=(
            AnalysisRequirementEvaluation(
                "route_runtime_and_state_contract_resolved",
                "satisfied",
                (evidence.evidence_id,),
                "route_contract_resolved",
            ),
            AnalysisRequirementEvaluation(
                "causal_durable_execution_path_observed",
                "missing",
                (),
                "runtime_path_not_observed",
            ),
            AnalysisRequirementEvaluation(
                "public_read_consumer_observed",
                "missing",
                (),
                "public_consumer_not_observed",
            ),
            AnalysisRequirementEvaluation(
                "public_acceptance_scenario_observed",
                "missing",
                (),
                "acceptance_scenario_not_observed",
            ),
            AnalysisRequirementEvaluation(
                "declaration_only_counterevidence_evaluated",
                "satisfied",
                (counterevidence.evidence_id,),
                "declaration_counterevidence_resolved",
            ),
        ),
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="evaluated",
        next_action_ids=("exercise_text_capability_from_public_entrypoint",),
        limitations=("fixture_question_has_no_runtime_evidence",),
    )
    validate_analysis_question_evaluation(spec, evaluation)
    return spec, evaluation


def _framework_review_task_question(
    snapshot_id: str = "snapshot:fixture",
) -> tuple[AnalysisQuestionSpec, AnalysisQuestionEvaluation]:
    subject_key = "contract:framework-review-task-protocol"
    spec = AnalysisQuestionSpec(
        question_id=("framework.review_task_lifecycle_preserves_atomicity_and_human_authority"),
        version="v1",
        subject_kinds=("contract",),
        requirements=(
            AnalysisEvidenceRequirementSpec(
                "framework_review_task_owner_store_contract",
                "question",
                "supporting",
                ("contract",),
            ),
            AnalysisEvidenceRequirementSpec(
                "framework_review_task_public_protocol_contract",
                "question",
                "supporting",
                ("contract",),
            ),
            AnalysisEvidenceRequirementSpec(
                "review_task_stale_head_and_fault_counterevidence_evaluated",
                "decision",
                "counterevidence",
                ("experiment_result",),
            ),
            AnalysisEvidenceRequirementSpec(
                "isolated_review_task_protocol_experiment_result",
                "decision",
                "experiment_result",
                ("experiment_result",),
            ),
        ),
        hypotheses=(
            "review_task_protocol_preserves_atomicity_and_human_authority",
            "review_task_protocol_can_publish_partial_or_non_human_terminal_state",
        ),
        counterevidence_rules=("stale_heads_faults_and_changed_retries_must_fail_closed",),
        next_actions=(
            AnalysisNextActionSpec(
                "run_framework_review_task_protocol_experiment",
                "experiment",
                "Exercise the bounded Framework ReviewTask protocol controls.",
            ),
        ),
    )
    owner_contract = replace(
        _source_evidence(subject_key, snapshot_id),
        evidence_id="contract:framework-review-task-owner-store",
        source_record_kind="framework_review_task_owner_store_contract",
        source_record_id="framework-review-task-owner-store",
        source_projection_digest="projection:framework-review-task-owner-store",
        facts=(
            AnalysisFact("logical_owner_id", "review"),
            AnalysisFact("state_owner_id", "framework"),
            AnalysisFact("state_store_id", "sqlite:framework.sqlite3"),
        ),
    )
    public_contract = replace(
        owner_contract,
        evidence_id="contract:framework-review-task-public-protocol",
        source_record_kind="framework_review_task_public_protocol_contract",
        source_record_id="framework-review-task-public-protocol",
        source_projection_digest="projection:framework-review-task-public-protocol",
        facts=(
            AnalysisFact("public_adapter_module", "neocortex.review_task_cli_adapter"),
            AnalysisFact("terminal_decisions_require_human", True),
        ),
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id="evaluation:framework-review-task-protocol",
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=1,
        subject=AnalysisSubjectRef(
            "contract",
            subject_key,
            "Framework ReviewTask protocol",
            "framework",
            snapshot_id,
            "current",
        ),
        evidence=(owner_contract, public_contract),
        requirements=(
            AnalysisRequirementEvaluation(
                "framework_review_task_owner_store_contract",
                "satisfied",
                (owner_contract.evidence_id,),
                "owner_store_contract_resolved",
            ),
            AnalysisRequirementEvaluation(
                "framework_review_task_public_protocol_contract",
                "satisfied",
                (public_contract.evidence_id,),
                "public_protocol_contract_resolved",
            ),
            AnalysisRequirementEvaluation(
                "review_task_stale_head_and_fault_counterevidence_evaluated",
                "missing",
                (),
                "registered_experiment_not_recorded",
            ),
            AnalysisRequirementEvaluation(
                "isolated_review_task_protocol_experiment_result",
                "missing",
                (),
                "registered_experiment_not_recorded",
            ),
        ),
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=("run_framework_review_task_protocol_experiment",),
        limitations=("fixture_contract_requires_registered_runtime_evidence",),
    )
    validate_analysis_question_evaluation(spec, evaluation)
    return spec, evaluation


def _receipt(
    proposal,
    *,
    status: str = "passed",
    source_version: str = "snapshot:fixture",
    receipt_schema: str = CODE_EXPERIMENT_RECEIPT_V3_SCHEMA,
) -> CodeExperimentReceipt:
    if receipt_schema not in {
        CODE_EXPERIMENT_RECEIPT_V3_SCHEMA,
        CODE_EXPERIMENT_RECEIPT_SCHEMA,
    }:
        raise ValueError("unsupported fixture receipt schema")
    attested = receipt_schema == CODE_EXPERIMENT_RECEIPT_SCHEMA
    scenario_specs = tuple(runtime_scenario(item) for item in proposal.scenario_ids)
    outcome = "passed" if status == "passed" else "failed"
    outcomes = tuple(
        CodeExperimentOutcome(
            scenario.scenario_id,
            scenario.test_nodeids,
            outcome,
            tuple(
                f"relation:{scenario.scenario_id}:{index:04d}"
                for index, _ in enumerate(scenario.test_nodeids)
            ),
        )
        for scenario in scenario_specs
    )
    gate_outcomes = tuple(
        CodeExperimentGateOutcome(
            gate.gate_id,
            scenario.scenario_id,
            gate.test_nodeids,
            outcome,
            tuple(
                f"gate-relation:{gate.gate_id}:{index:04d}"
                for index, _ in enumerate(gate.test_nodeids)
            ),
            (
                "all_bound_test_contracts_passed"
                if outcome == "passed"
                else "one_or_more_bound_test_contracts_failed"
            ),
        )
        for scenario in scenario_specs
        for gate in scenario.gate_specs
    )
    values = {
        "status": status,
        "reason": None,
        "policy_id": (
            "allowlisted-measured-gates-trusted-deep-v5"
            if attested
            else "allowlisted-measured-gates-trusted-deep-v4"
        ),
        "proposal_id": proposal.proposal_id,
        "template_id": proposal.template_id,
        "template_version": proposal.template_version,
        "runner_kind": proposal.runner_kind,
        "source_root": "/fixture/repository",
        "source_version": source_version,
        "source_manifest_digest": "manifest:fixture",
        "code_database_digest_before": "digest:same",
        "code_database_digest_after": "digest:same",
        "code_database_unchanged": True,
        "configuration_signature": "config:fixture",
        "scenario_registry_fingerprint": "registry:fixture",
        "provider_id": "pytest-coverage-trusted-deep",
        "provider_schema": "neocortex.pytest-coverage-trusted-deep/v1",
        "provider_status": "completed",
        "provider_execution": "full",
        "provider_input_signature": "input:fixture",
        "provider_result_digest": "result:fixture",
        "selected_scenarios": tuple(item.scenario_id for item in scenario_specs),
        "selected_nodeids": tuple(
            nodeid for item in scenario_specs for nodeid in item.test_nodeids
        ),
        "outcomes": outcomes,
        "gate_outcomes": gate_outcomes,
        "passed": sum(item.outcome == "passed" for item in outcomes),
        "failed": sum(item.outcome == "failed" for item in outcomes),
        "skipped": 0,
        "duration_ms": 20,
        "process_invocations": 0 if attested else 1,
        "stdout_bytes": 0 if attested else 10,
        "stderr_bytes": 0,
        "limitations": (
            "receipt_proves_selected_test_outcomes_not_a_question_conclusion_or_formal_proof",
            "no_product_mutation_authority",
        ),
        "authority": "advisory",
        "mutation_authority": False,
    }
    if attested:
        values.update(
            {
                "evidence_mode": "primary_trusted_deep_projection",
                "analysis_run_id": 1,
                "provider_tool_run_id": 11,
                "provider_effective_tool_run_id": 11,
                "provider_tool_status": "completed",
                "provider_tool_name": "pytest",
                "provider_tool_version": "9.1.0",
                "provider_portable_publication_id": "publication:fixture",
                "provider_descriptor_configuration_signature": "descriptor:fixture",
                "provider_environment_signature": "environment:fixture",
                "provider_comparability_signature": "comparability:fixture",
                "provider_suite_selection": "full",
                "provider_suite_signature": "suite:fixture",
                "provider_measurement_scope_signature": "scope:fixture",
                "selected_relation_digest": "relations:fixture",
            }
        )
    identity_values = dict(values)
    identity_values["duration_ms"] = 0
    identity_values["outcomes"] = tuple(asdict(item) for item in outcomes)
    identity_values["gate_outcomes"] = tuple(asdict(item) for item in gate_outcomes)
    return CodeExperimentReceipt(
        receipt_id=analysis_identity(
            "code-experiment-receipt-v4" if attested else "code-experiment-receipt-v3",
            identity_values,
        ),
        **values,  # type: ignore[arg-type]
    )


def _database(tmp_path: Path, *, status: str = "completed") -> Path:
    database = tmp_path / "code.sqlite3"
    initialize_code_state(database)
    connection = connect_code_state(database, create=False)
    try:
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns,candidates,processed,cache_hits,errors)
            VALUES(1,1,1,'snapshot:fixture',?,1,2,0,0,0,0)""",
            (status,),
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _add_python_source(
    database: Path,
    *,
    text: str = "def noop():\n    return None\n",
    relative_path: str = "_04_Nucleo_Operativo/noop.py",
) -> None:
    raw = text.encode("utf-8")
    digest = fingerprint_text(text)
    path = f"/fixture/Repository/{relative_path}"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """INSERT INTO files(
            file_id,volume_id,physical_file_id,current_path,first_seen_run_id,
            last_seen_run_id,status) VALUES(1,'fixture-volume','fixture-file',?,1,1,'current')""",
            (path,),
        )
        connection.execute(
            """INSERT INTO file_versions(
            version_id,file_id,path_observed,size,mtime_ns,birthtime_ns,
            raw_xxh3_128,raw_xxh3_64_guard,text_xxh3_128,text_xxh3_64_guard,
            encoding,language,artifact_kind,generated,vendored,classification_confidence,
            classification_evidence_json,analysis_status,processing_signature,
            analyzer_id,analyzer_version,parser_kind,text_zlib,text_chars,text_truncated,
            provenance_json,first_observed_run_id,last_observed_run_id,valid_from_ns)
            VALUES(1,1,?,?,1,1,?,?,?,?,'utf-8','python','source',0,0,1.0,'{}',
            'complete','snapshot:fixture','fixture','v1','ast',?,?,0,'{}',1,1,1)""",
            (
                path,
                len(raw),
                digest.xxh3_128,
                digest.xxh3_64_guard,
                digest.xxh3_128,
                digest.xxh3_64_guard,
                zlib.compress(raw),
                len(text),
            ),
        )
        connection.execute("UPDATE files SET current_version_id=1 WHERE file_id=1")
        connection.commit()
    finally:
        connection.close()


def _code_v5_database(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        code_schema._build_legacy_schema(connection, 5)
        for version in range(1, 6):
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?)",
                (version, f"fixture-v{version}", version),
            )
        connection.execute("INSERT INTO metadata VALUES('schema_version','5')")
        connection.execute("PRAGMA user_version=5")
        connection.execute(
            """INSERT INTO files(file_id,volume_id,physical_file_id,current_path,status,
            first_seen_run_id,last_seen_run_id)
            VALUES(7,'volume','physical','/fixture/Case.py','current',1,1)"""
        )
        connection.commit()
    finally:
        connection.close()


def test_populated_code_v5_migrates_to_versioned_receipts_without_fact_drift(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code-v5.sqlite3"
    _code_v5_database(database)

    initialize_code_state(database)

    with sqlite3.connect(database) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone() == (7,)
        assert migrated.execute("SELECT file_id,current_path,status FROM files").fetchone() == (
            7,
            "/fixture/Case.py",
            "current",
        )
        assert migrated.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]
        assert migrated.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (0,)
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v5_to_v6_migration_failure_rolls_back_every_receipt_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rollback-code-v5.sqlite3"
    _code_v5_database(database)
    monkeypatch.setattr(
        code_schema,
        "_V6_DDL",
        (code_schema._V6_DDL[0], "CREATE TABLE deliberately_incomplete("),
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_code_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("5",)
        assert (
            connection.execute(
                "SELECT type FROM sqlite_master WHERE name='code_experiment_receipts'"
            ).fetchone()
            is None
        )
        assert connection.execute("SELECT current_path FROM files").fetchone() == (
            "/fixture/Case.py",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        code_schema._validate_legacy_code_schema(connection, 5)


def test_v5_to_v6_does_not_suspend_foreign_keys_or_rebuild_path_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "foreign-keys-code-v5.sqlite3"
    _code_v5_database(database)
    original = code_schema._migrate_five_to_six
    observed: list[tuple[int, int]] = []

    def guarded_migration(connection: sqlite3.Connection, applied_ns: int) -> None:
        observed.append(
            (
                int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                int(connection.execute("PRAGMA legacy_alter_table").fetchone()[0]),
            )
        )
        original(connection, applied_ns)

    monkeypatch.setattr(code_schema, "_migrate_five_to_six", guarded_migration)
    initialize_code_state(database)

    assert observed == [(1, 0)]


def test_passed_receipt_is_persisted_idempotently_and_closes_only_human_readiness(
    tmp_path: Path,
) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    assert plan.executable_count == 1
    proposal = plan.proposals[0]
    receipt = _receipt(proposal)
    database = _database(tmp_path)

    first = record_code_experiment_receipt(
        database,
        receipt,
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
        recorded_ns=10,
    )
    second = record_code_experiment_receipt(
        database,
        receipt,
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
        recorded_ns=99,
    )
    assert second == first
    assert parse_resolved_code_experiment_receipt_payload(first.as_payload()) == first

    resolved = read_code_experiment_receipts(
        database,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        plan=plan,
    )
    assert resolved == (first,)
    projected = apply_code_experiment_receipts((spec,), (evaluation,), plan, resolved)
    assert projected[0].decision_readiness == "human_review_required"
    assert projected[0].decision is None
    assert projected[0].next_action_ids == ()
    assert projected[0].counterevidence_status == "evaluated"
    assert plan_code_experiments((spec,), projected).status == "not_required"

    connection = connect_code_state(database, create=False)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone()[0] == 1
        )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute("UPDATE code_experiment_receipts SET review_digest='forged'")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute("DELETE FROM code_experiment_receipts")
    finally:
        connection.close()


def test_v3_and_v4_receipts_coexist_with_schema_bound_identity_and_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    receipt_v3 = _receipt(proposal, receipt_schema=CODE_EXPERIMENT_RECEIPT_V3_SCHEMA)
    receipt_v4 = _receipt(proposal, receipt_schema=CODE_EXPERIMENT_RECEIPT_SCHEMA)
    database = _database(tmp_path)
    monkeypatch.setattr(
        experiment_store,
        "_validate_attested_receipt_sources",
        lambda *_args, **_kwargs: None,
    )

    stored = record_code_experiment_receipts(
        database,
        ((receipt_v3, proposal), (receipt_v4, proposal)),
        1,
        "snapshot:fixture",
        "review:fixture",
    )

    assert receipt_v3.receipt_id != receipt_v4.receipt_id
    assert tuple(item.receipt.as_payload()["schema"] for item in stored) == (
        CODE_EXPERIMENT_RECEIPT_V3_SCHEMA,
        CODE_EXPERIMENT_RECEIPT_SCHEMA,
    )
    assert stored[0].envelope_digest != stored[1].envelope_digest
    assert stored[0].recorded_ns < stored[1].recorded_ns
    assert (
        tuple(parse_resolved_code_experiment_receipt_payload(item.as_payload()) for item in stored)
        == stored
    )
    for item, expected_schema in zip(
        stored,
        (CODE_EXPERIMENT_RECEIPT_V3_SCHEMA, CODE_EXPERIMENT_RECEIPT_SCHEMA),
        strict=True,
    ):
        projected = apply_code_experiment_receipts(
            (spec,),
            (evaluation,),
            plan,
            (item,),
        )[0]
        linked = tuple(
            evidence
            for evidence in projected.evidence
            if evidence.source_record_id == item.receipt.receipt_id
        )
        assert linked
        assert {evidence.source_schema for evidence in linked} == {expected_schema}
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT receipt_id,receipt_schema FROM code_experiment_receipts
            ORDER BY recorded_ns"""
        ).fetchall() == [
            (receipt_v3.receipt_id, CODE_EXPERIMENT_RECEIPT_V3_SCHEMA),
            (receipt_v4.receipt_id, CODE_EXPERIMENT_RECEIPT_SCHEMA),
        ]

    resolved = read_code_experiment_receipts(
        database,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        plan=plan,
    )
    assert resolved == (stored[1],)


def test_v4_receipt_revalidates_exact_provider_binding_before_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    receipt = _receipt(proposal, receipt_schema=CODE_EXPERIMENT_RECEIPT_SCHEMA)
    database = _database(tmp_path)
    observed: list[dict[str, object]] = []
    attestation = SimpleNamespace(
        tool_run_id=receipt.provider_tool_run_id,
        effective_tool_run_id=receipt.provider_effective_tool_run_id,
        tool_status=receipt.provider_tool_status,
        tool_name=receipt.provider_tool_name,
        tool_version=receipt.provider_tool_version,
        execution=receipt.provider_execution,
        input_signature=receipt.provider_input_signature,
        result_digest=receipt.provider_result_digest,
        portable_publication_id=receipt.provider_portable_publication_id,
        descriptor_configuration_signature=(receipt.provider_descriptor_configuration_signature),
        environment_signature=receipt.provider_environment_signature,
        comparability_signature=receipt.provider_comparability_signature,
    )

    def read_attestation(_connection, **kwargs):
        observed.append(kwargs)
        return attestation

    monkeypatch.setattr(experiment_store, "read_external_provider_attestation", read_attestation)
    monkeypatch.setattr(
        experiment_store,
        "validate_code_experiment_declared_test_relations",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        experiment_store,
        "code_experiment_selected_relation_digest",
        lambda _relations, _nodeids: receipt.selected_relation_digest,
    )

    stored = record_code_experiment_receipts(
        database,
        ((receipt, proposal),),
        1,
        "snapshot:fixture",
        "review:fixture",
    )

    assert tuple(item.receipt for item in stored) == (receipt,)
    assert observed == [
        {
            "analysis_run_id": 1,
            "tool_run_id": receipt.provider_tool_run_id,
            "expected_processing_signature": "snapshot:fixture",
            "expected_provider_id": receipt.provider_id,
            "expected_provider_schema": receipt.provider_schema,
            "enforce_current_runtime": True,
        }
    ]


def test_v4_receipt_provider_binding_change_rolls_back_without_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    receipt = _receipt(proposal, receipt_schema=CODE_EXPERIMENT_RECEIPT_SCHEMA)
    database = _database(tmp_path)
    monkeypatch.setattr(
        experiment_store,
        "read_external_provider_attestation",
        lambda *_args, **_kwargs: SimpleNamespace(
            tool_run_id=receipt.provider_tool_run_id,
            effective_tool_run_id=receipt.provider_effective_tool_run_id,
            tool_status=receipt.provider_tool_status,
            tool_name=receipt.provider_tool_name,
            tool_version=receipt.provider_tool_version,
            execution=receipt.provider_execution,
            input_signature=receipt.provider_input_signature,
            result_digest="result:changed-after-attestation",
            portable_publication_id=receipt.provider_portable_publication_id,
            descriptor_configuration_signature=(
                receipt.provider_descriptor_configuration_signature
            ),
            environment_signature=receipt.provider_environment_signature,
            comparability_signature=receipt.provider_comparability_signature,
        ),
    )

    with pytest.raises(
        CodeExperimentStoreError,
        match="experiment_receipt_provider_binding_changed",
    ):
        record_code_experiment_receipts(
            database,
            ((receipt, proposal),),
            1,
            "snapshot:fixture",
            "review:fixture",
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (
            0,
        )


def test_store_rejects_receipt_schema_beyond_strict_v3_v4_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    receipt = _receipt(proposal)
    database = _database(tmp_path)
    original_payload = CodeExperimentReceipt.as_payload

    def future_payload(self: CodeExperimentReceipt) -> dict[str, object]:
        payload = original_payload(self)
        payload["schema"] = "neocortex.code-experiment-receipt/v5"
        return payload

    monkeypatch.setattr(CodeExperimentReceipt, "as_payload", future_payload)
    with pytest.raises(ValueError, match="payload schema"):
        record_code_experiment_receipt(
            database,
            receipt,
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (
            0,
        )


def test_receipt_batch_rolls_back_atomically_when_a_later_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    pairs = (
        (_receipt(proposal), proposal),
        (_receipt(proposal, receipt_schema=CODE_EXPERIMENT_RECEIPT_SCHEMA), proposal),
    )
    database = _database(tmp_path)
    original_connect = experiment_store.connect_code_state
    opened = 0

    class RejectingSecondReceiptInsert:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection
            self._receipt_inserts = 0

        @property
        def in_transaction(self) -> bool:
            return self._connection.in_transaction

        def execute(self, statement: str, parameters=()):
            if statement.lstrip().startswith("INSERT INTO code_experiment_receipts"):
                self._receipt_inserts += 1
                if self._receipt_inserts == 2:
                    raise sqlite3.IntegrityError("fixture rejects second receipt")
            return self._connection.execute(statement, parameters)

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    def rejecting_connect(path: Path, *, readonly: bool = False, create: bool = True):
        nonlocal opened
        opened += 1
        return RejectingSecondReceiptInsert(
            original_connect(path, readonly=readonly, create=create)
        )

    monkeypatch.setattr(experiment_store, "connect_code_state", rejecting_connect)
    monkeypatch.setattr(
        experiment_store,
        "_validate_attested_receipt_sources",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(sqlite3.IntegrityError, match="second receipt"):
        record_code_experiment_receipts(
            database,
            pairs,
            1,
            "snapshot:fixture",
            "review:fixture",
        )

    assert opened == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (
            0,
        )


def test_framework_review_task_receipt_projects_only_its_two_bound_requirements(
    tmp_path: Path,
) -> None:
    spec, evaluation = _framework_review_task_question()
    plan = plan_code_experiments((spec,), (evaluation,))
    assert plan.status == "ready"
    assert plan.executable_count == 1
    proposal = plan.proposals[0]
    assert proposal.template_id == "framework.review_task_protocol_acceptance"
    assert proposal.template_version == "v1"
    database = _database(tmp_path)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:framework-review-task-fixture",
        recorded_ns=10,
    )

    projected = apply_code_experiment_receipts(
        (spec,),
        (evaluation,),
        plan,
        (stored,),
    )[0]
    requirements = {item.requirement_id: item for item in projected.requirements}
    original_requirements = {item.requirement_id: item for item in evaluation.requirements}
    experiment_requirements = {
        "review_task_stale_head_and_fault_counterevidence_evaluated",
        "isolated_review_task_protocol_experiment_result",
    }

    assert projected.decision_readiness == "human_review_required"
    assert projected.decision is None
    assert projected.next_action_ids == ()
    assert projected.counterevidence_status == "evaluated"
    assert all(requirements[item].status == "satisfied" for item in experiment_requirements)
    assert all(
        requirements[item] == original_requirements[item]
        for item in set(requirements) - experiment_requirements
    )
    linked = tuple(
        evidence for evidence in projected.evidence if evidence.evidence_kind == "experiment_result"
    )
    assert len(linked) == 2
    assert {item.role for item in linked} == {"counterevidence", "experiment_result"}
    assert {item.source_record_id for item in linked} == {stored.receipt.receipt_id}
    assert plan_code_experiments((spec,), (projected,)).status == "not_required"


@pytest.mark.parametrize(
    ("field", "forged_value"),
    (
        ("analysis_run_id", 2),
        ("review_digest", "review:forged"),
        ("recorded_ns", 11),
    ),
)
def test_public_receipt_envelope_rejects_context_tampering(
    tmp_path: Path,
    field: str,
    forged_value: object,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    database = _database(tmp_path)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
        recorded_ns=10,
    )
    payload = stored.as_payload()
    payload[field] = forged_value

    with pytest.raises(ValueError, match="envelope digest"):
        parse_resolved_code_experiment_receipt_payload(payload)


def test_insert_failure_rolls_back_the_receipt_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    database = _database(tmp_path)
    original_connect = experiment_store.connect_code_state

    def rejecting_connect(path: Path, *, readonly: bool = False, create: bool = True):
        connection = original_connect(path, readonly=readonly, create=create)

        def deny_receipt_insert(
            action: int,
            argument_one: str | None,
            _argument_two: str | None,
            _database_name: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_INSERT and argument_one == "code_experiment_receipts":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_receipt_insert)
        return connection

    monkeypatch.setattr(experiment_store, "connect_code_state", rejecting_connect)
    with pytest.raises(sqlite3.DatabaseError):
        record_code_experiment_receipt(
            database,
            _receipt(proposal),
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (
            0,
        )


def test_checkpoint_failure_is_recoverable_by_an_idempotent_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    receipt = _receipt(proposal)
    database = _database(tmp_path)
    checkpoints: list[int] = []

    def fail_first_checkpoint(*_args, **_kwargs) -> None:
        checkpoints.append(len(checkpoints) + 1)
        if len(checkpoints) == 1:
            raise CodeExperimentStoreError("injected_checkpoint_failure")

    monkeypatch.setattr(experiment_store, "checkpoint_code_wal", fail_first_checkpoint)
    monkeypatch.setattr(
        experiment_store,
        "remove_checkpointed_code_sidecars",
        lambda *_args, **_kwargs: True,
    )
    with pytest.raises(CodeExperimentStoreError, match="injected_checkpoint_failure"):
        record_code_experiment_receipt(
            database,
            receipt,
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
            recorded_ns=10,
        )

    recovered = record_code_experiment_receipt(
        database,
        receipt,
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
        recorded_ns=99,
    )
    assert recovered.recorded_ns == 10
    assert checkpoints == [1, 2]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (
            1,
        )


def test_sidecar_cleanup_failure_does_not_duplicate_a_committed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    receipt = _receipt(proposal)
    database = _database(tmp_path)
    cleanups: list[int] = []

    def fail_first_cleanup(*_args, **_kwargs) -> bool:
        cleanups.append(len(cleanups) + 1)
        if len(cleanups) == 1:
            raise CodeExperimentStoreError("injected_sidecar_cleanup_failure")
        return True

    monkeypatch.setattr(experiment_store, "checkpoint_code_wal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        experiment_store,
        "remove_checkpointed_code_sidecars",
        fail_first_cleanup,
    )
    with pytest.raises(CodeExperimentStoreError, match="injected_sidecar_cleanup_failure"):
        record_code_experiment_receipt(
            database,
            receipt,
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
            recorded_ns=10,
        )

    recovered = record_code_experiment_receipt(
        database,
        receipt,
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
        recorded_ns=99,
    )
    assert recovered.recorded_ns == 10
    assert cleanups == [1, 2]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (
            1,
        )


def test_failed_receipt_remains_durable_but_cannot_satisfy_evidence(tmp_path: Path) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    database = _database(tmp_path)
    failed = _receipt(proposal, status="failed")

    record_code_experiment_receipt(
        database,
        failed,
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    assert (
        read_code_experiment_receipts(
            database,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            plan=plan,
        )
        == ()
    )
    assert apply_code_experiment_receipts((spec,), (evaluation,), plan, ()) == (evaluation,)


def test_newer_failed_receipt_prevents_reuse_of_older_passed_evidence(tmp_path: Path) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    database = _database(tmp_path)
    for receipt, recorded_ns in (
        (_receipt(proposal), 10),
        (_receipt(proposal, status="failed"), 20),
    ):
        record_code_experiment_receipt(
            database,
            receipt,
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
            recorded_ns=recorded_ns,
        )

    assert (
        read_code_experiment_receipts(
            database,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            plan=plan,
        )
        == ()
    )


@pytest.mark.parametrize(
    "owner_status",
    ["running", "failed", "partial", "cancelled", "interrupted"],
)
def test_receipt_store_rejects_a_noncompleted_source_owner(
    tmp_path: Path,
    owner_status: str,
) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    database = _database(tmp_path, status=owner_status)
    with pytest.raises(CodeExperimentStoreError, match="not_completed"):
        record_code_experiment_receipt(
            database,
            _receipt(proposal),
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
        )


@pytest.mark.parametrize(
    "owner_status",
    ["running", "failed", "partial", "cancelled", "interrupted"],
)
def test_prior_receipt_is_hidden_when_its_source_owner_is_no_longer_completed(
    tmp_path: Path,
    owner_status: str,
) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    database = _database(tmp_path)
    record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE analysis_runs SET status=? WHERE analysis_run_id=1",
            (owner_status,),
        )

    assert (
        read_code_experiment_receipts(
            database,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            plan=plan,
        )
        == ()
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (
            1,
        )


@pytest.mark.parametrize("newer_status", ["running", "completed"])
def test_receipt_store_rejects_a_completed_source_that_is_no_longer_latest(
    tmp_path: Path,
    newer_status: str,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns,candidates,processed,cache_hits,errors)
            VALUES(2,2,2,'snapshot:newer',?,3,4,0,0,0,0)""",
            (newer_status,),
        )

    with pytest.raises(CodeExperimentStoreError, match="not_latest"):
        record_code_experiment_receipt(
            database,
            _receipt(proposal),
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone() == (
            0,
        )


def test_exact_signature_replay_reuses_prior_completed_receipt_but_new_signature_does_not(
    tmp_path: Path,
) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    database = _database(tmp_path)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns,candidates,processed,cache_hits,errors)
            VALUES(2,2,2,'snapshot:fixture','completed',3,4,0,0,0,0)"""
        )

    replay_evidence = replace(
        evaluation.evidence[0],
        evidence_id="contract:route-fixture:exact-replay",
        source_record_id="route:text:exact-replay-capture",
        source_projection_digest="projection:route:text:exact-replay-capture",
        snapshot_id="snapshot:exact-replay",
    )
    replay_counterevidence = replace(
        evaluation.evidence[1],
        evidence_id="counterevidence:route-fixture:exact-replay",
        source_record_id="counterevidence:text:exact-replay-capture",
        source_projection_digest="projection:counterevidence:text:exact-replay-capture",
        snapshot_id="snapshot:exact-replay",
    )
    replay_evidence_ids = {
        evaluation.evidence[0].evidence_id: replay_evidence.evidence_id,
        evaluation.evidence[1].evidence_id: replay_counterevidence.evidence_id,
    }
    replay_evaluation = replace(
        evaluation,
        evaluation_id="evaluation:route:text:exact-replay",
        subject=replace(evaluation.subject, snapshot_id="snapshot:exact-replay"),
        evidence=(replay_evidence, replay_counterevidence),
        requirements=tuple(
            replace(
                item,
                evidence_ids=tuple(replay_evidence_ids[value] for value in item.evidence_ids),
            )
            if item.evidence_ids
            else item
            for item in evaluation.requirements
        ),
    )
    replay_plan = plan_code_experiments((spec,), (replay_evaluation,))
    assert replay_plan.proposals[0].proposal_id == proposal.proposal_id
    resolved = read_code_experiment_receipts(
        database,
        analysis_run_id=2,
        processing_signature="snapshot:fixture",
        plan=replay_plan,
    )
    assert resolved == (stored,)
    projected = apply_code_experiment_receipts(
        (spec,),
        (replay_evaluation,),
        replay_plan,
        resolved,
    )
    assert projected[0].decision_readiness == "human_review_required"
    assert any(
        fact.name == "source_evaluation_replayed" and fact.value is True
        for evidence in projected[0].evidence
        for fact in evidence.facts
    )
    changed_evidence = replace(
        replay_evidence,
        facts=(replace(replay_evidence.facts[0], value="text-state-changed"),),
    )
    changed_evaluation = replace(
        replay_evaluation,
        evaluation_id="evaluation:route:text:changed-evidence",
        evidence=(changed_evidence, replay_counterevidence),
    )
    changed_plan = plan_code_experiments((spec,), (changed_evaluation,))
    assert changed_plan.proposals[0].proposal_id != proposal.proposal_id
    assert (
        read_code_experiment_receipts(
            database,
            analysis_run_id=2,
            processing_signature="snapshot:fixture",
            plan=changed_plan,
        )
        == ()
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns,candidates,processed,cache_hits,errors)
            VALUES(3,3,3,'snapshot:different','completed',5,6,0,0,0,0)"""
        )
    assert (
        read_code_experiment_receipts(
            database,
            analysis_run_id=2,
            processing_signature="snapshot:fixture",
            plan=plan,
        )
        == ()
    )


def test_public_cli_receipt_rebinds_across_interface_capture_runs_and_is_verified(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _add_python_source(
        database,
        text=(
            'import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--alpha")\n'
        ),
        relative_path="neocortex/cli_fixture.py",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO code_references(
            reference_id,version_id,source_symbol_id,target_symbol_id,target_version_id,
            kind,name,target_hint,confirmed,confidence,evidence,start_line,start_column,
            end_line,end_column,start_byte,end_byte)
            VALUES(1,1,NULL,NULL,NULL,'call','parser.add_argument',NULL,1,1.0,
            'fixture-static-call',3,0,3,30,0,1)"""
        )
    with readonly_code_database(database) as connection:
        primary_analysis = read_code_interface_surface_analysis(
            connection,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            database=str(database),
        )
    primary_specs, primary_evaluations = interface_surface_questions(
        primary_analysis,
        snapshot_freshness="current",
        rank_offset=0,
    )
    primary_plan = plan_code_experiments(primary_specs, primary_evaluations)
    primary_cli = next(
        item for item in primary_evaluations if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    primary_proposal = next(
        item
        for item in primary_plan.proposals
        if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    stored = record_code_experiment_receipt(
        database,
        _receipt(primary_proposal),
        primary_proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:public-cli-primary",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns,candidates,processed,cache_hits,errors)
            VALUES(2,2,2,'snapshot:fixture','completed',3,4,0,0,0,0)"""
        )

    with readonly_code_database(database) as connection:
        replay_analysis = read_code_interface_surface_analysis(
            connection,
            analysis_run_id=2,
            processing_signature="snapshot:fixture",
            database=str(database),
        )
    replay_specs, replay_evaluations = interface_surface_questions(
        replay_analysis,
        snapshot_freshness="current",
        rank_offset=0,
    )
    replay_plan = plan_code_experiments(replay_specs, replay_evaluations)
    replay_cli = next(
        item for item in replay_evaluations if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    replay_proposal = next(
        item
        for item in replay_plan.proposals
        if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    assert replay_cli.evaluation_id != primary_cli.evaluation_id
    assert replay_proposal.proposal_id == primary_proposal.proposal_id

    resolved = read_code_experiment_receipts(
        database,
        analysis_run_id=2,
        processing_signature="snapshot:fixture",
        plan=replay_plan,
    )
    assert resolved == (stored,)
    projected = apply_code_experiment_receipts(
        replay_specs,
        replay_evaluations,
        replay_plan,
        resolved,
    )
    projected_cli = next(
        item for item in projected if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    assert projected_cli.decision_readiness == "human_review_required"
    assert any(
        fact.name == "source_evaluation_replayed" and fact.value is True
        for evidence in projected_cli.evidence
        for fact in evidence.facts
    )
    technical = build_code_technical_verification(replay_specs, projected, resolved)
    assert technical.status == "ready"
    assert technical.reviewed_count == 1
    assert technical.unresolved_count == 0
    assert technical.reviews[0].evaluation_id == projected_cli.evaluation_id
    assert technical.reviews[0].disposition == "no_change_required_within_verified_scope"


def test_two_receipts_link_to_their_own_current_evaluations(tmp_path: Path) -> None:
    route_spec, route_evaluation = _question()
    database = _database(tmp_path)
    _add_python_source(database)
    state_analysis = analyze_code_state_interactions(tmp_path)
    assert state_analysis.status != "abstained", state_analysis.reason
    state_specs, state_evaluations = state_interaction_questions(
        state_analysis,
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank_offset=1,
    )
    workflow_spec = state_specs[1]
    workflow_evaluation = replace(state_evaluations[1], rank=2)
    assert workflow_evaluation.decision_readiness == "experiment_required"
    specs = (route_spec, workflow_spec)
    evaluations = (route_evaluation, workflow_evaluation)
    plan = plan_code_experiments(specs, evaluations)
    proposals = tuple(
        item
        for item in plan.proposals
        if item.planning_status == "planned" and item.runner_kind != "none"
    )
    assert len(proposals) == 2, tuple(
        (
            item.question_id,
            item.subject_key,
            item.planning_status,
            item.runner_kind,
            item.reason,
        )
        for item in plan.proposals
    )
    for index, proposal in enumerate(proposals, start=1):
        record_code_experiment_receipt(
            database,
            _receipt(proposal),
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
            recorded_ns=index,
        )

    resolved = read_code_experiment_receipts(
        database,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        plan=plan,
    )
    projected = apply_code_experiment_receipts(specs, evaluations, plan, resolved)
    projected_reversed = apply_code_experiment_receipts(
        specs,
        evaluations,
        plan,
        tuple(reversed(resolved)),
    )

    assert len(resolved) == 2
    assert projected_reversed == projected
    assert all(item.decision_readiness == "human_review_required" for item in projected)
    assert {
        evidence.source_record_id
        for item in projected
        for evidence in item.evidence
        if evidence.evidence_kind == "experiment_result"
    } == {item.receipt.receipt_id for item in resolved}
    assert (
        read_code_experiment_receipts(
            database,
            analysis_run_id=3,
            processing_signature="snapshot:different",
            plan=plan,
        )
        == ()
    )


def test_public_route_receipt_satisfies_the_real_question_contract(tmp_path: Path) -> None:
    spec = ROUTE_CAPABILITY_QUESTION
    subject_key = "capability:route:text"
    snapshot_id = "snapshot:fixture"
    contract = _source_evidence(subject_key, snapshot_id)
    runtime = replace(
        contract,
        evidence_id="runtime:route-fixture",
        evidence_kind="runtime_observation",
        producer_id="fixture-runtime-resolver",
        source_schema="fixture-runtime/v1",
        source_record_kind="runtime_prerequisites",
        source_record_id="runtime:text",
        source_projection_digest="projection:runtime:text",
        facts=(AnalysisFact("runtime_state", "available"),),
    )
    owner = replace(
        runtime,
        evidence_id="owner:route-fixture",
        producer_id="fixture-owner-resolver",
        source_record_kind="owner_snapshot",
        source_record_id="owner:text",
        source_projection_digest="projection:owner:text",
        facts=(AnalysisFact("owner_state", "available"),),
    )
    counter = replace(
        runtime,
        evidence_id="counter:route-fixture",
        role="counterevidence",
        producer_id="fixture-counterevidence-resolver",
        source_record_kind="declaration_counterevidence",
        source_record_id="counter:text",
        source_projection_digest="projection:counter:text",
        facts=(AnalysisFact("declaration_only", False),),
    )
    evidence_by_requirement = {
        "route_runtime_and_state_contract_resolved": contract,
        "runtime_prerequisites_observed": runtime,
        "state_owner_snapshot_observed": owner,
        "declaration_only_counterevidence_evaluated": counter,
    }
    requirements = tuple(
        AnalysisRequirementEvaluation(
            requirement.requirement_id,
            "satisfied" if requirement.requirement_id in evidence_by_requirement else "missing",
            (
                (evidence_by_requirement[requirement.requirement_id].evidence_id,)
                if requirement.requirement_id in evidence_by_requirement
                else ()
            ),
            "fixture_evidence_resolved"
            if requirement.requirement_id in evidence_by_requirement
            else "registered_experiment_not_recorded",
        )
        for requirement in spec.requirements
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id="evaluation:real-route-contract",
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=1,
        subject=AnalysisSubjectRef(
            "capability",
            subject_key,
            "Text route",
            "text",
            snapshot_id,
            "current",
        ),
        evidence=(contract, runtime, owner, counter),
        requirements=requirements,
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="evaluated",
        next_action_ids=tuple(item.action_id for item in spec.next_actions),
        limitations=("fixture_contract_only",),
    )
    validate_analysis_question_evaluation(spec, evaluation)
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    database = _database(tmp_path)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )

    projected = apply_code_experiment_receipts(
        (spec,),
        (evaluation,),
        plan,
        (stored,),
    )

    assert projected[0].decision_readiness == "human_review_required"
    assert counter.evidence_id in {
        evidence_id
        for requirement in projected[0].requirements
        for evidence_id in requirement.evidence_ids
    }


def test_public_cli_receipt_projects_typed_runtime_counter_and_result_evidence(
    tmp_path: Path,
) -> None:
    spec = CLI_SURFACE_QUESTION
    subject_key = "entrypoint:neocortex-interface-surface"
    snapshot_id = "snapshot:fixture"
    static_projection = replace(
        _source_evidence(subject_key, snapshot_id),
        evidence_id="metric:public-cli-static-projection",
        evidence_kind="internal_metric",
        producer_id="fixture-interface-surface-resolver",
        source_schema="fixture-interface-surface/v1",
        source_record_kind="entrypoint_surface_projection",
        source_record_id=subject_key,
        source_projection_digest="projection:public-cli-static",
        facts=(AnalysisFact("static_argument_calls", 17, "count"),),
    )
    requirements = tuple(
        AnalysisRequirementEvaluation(
            requirement.requirement_id,
            (
                "satisfied"
                if requirement.requirement_id == "published_static_argparse_call_projection"
                else "missing"
            ),
            (
                (static_projection.evidence_id,)
                if requirement.requirement_id == "published_static_argparse_call_projection"
                else ()
            ),
            (
                "static_projection_resolved"
                if requirement.requirement_id == "published_static_argparse_call_projection"
                else "registered_experiment_not_recorded"
            ),
        )
        for requirement in spec.requirements
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id="evaluation:public-cli-contract",
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=1,
        subject=AnalysisSubjectRef(
            "entrypoint",
            subject_key,
            "NeoCortex public CLI surface",
            "code",
            snapshot_id,
            "current",
        ),
        evidence=(static_projection,),
        requirements=requirements,
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=tuple(item.action_id for item in spec.next_actions),
        limitations=("fixture_requires_registered_public_cli_runtime_evidence",),
    )
    validate_analysis_question_evaluation(spec, evaluation)
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    assert proposal.template_id == "interfaces.public_cli_contract_acceptance"
    assert proposal.mutation_authority is False

    database = _database(tmp_path)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature=snapshot_id,
        review_digest="review:public-cli-fixture",
    )
    projected = apply_code_experiment_receipts((spec,), (evaluation,), plan, (stored,))[0]

    assert projected.decision_readiness == "human_review_required"
    assert projected.counterevidence_status == "evaluated"
    assert projected.next_action_ids == ()
    evidence_by_id = {item.evidence_id: item for item in projected.evidence}
    requirement_evidence = {
        item.requirement_id: tuple(evidence_by_id[evidence_id] for evidence_id in item.evidence_ids)
        for item in projected.requirements
    }
    expected = {
        "effective_runtime_parser_contract_observed": (
            "supporting",
            "runtime_observation",
            3,
            13,
        ),
        "dynamic_cli_construction_counterevidence_evaluated": (
            "counterevidence",
            "runtime_observation",
            2,
            13,
        ),
        "public_cli_acceptance_scenario_result": (
            "experiment_result",
            "experiment_result",
            5,
            26,
        ),
    }
    for requirement_id, (role, kind, gate_count, relation_count) in expected.items():
        linked = requirement_evidence[requirement_id]
        assert len(linked) == 1
        evidence = linked[0]
        assert (evidence.role, evidence.evidence_kind) == (role, kind)
        assert evidence.source_record_id == stored.receipt.receipt_id
        facts = {item.name: item.value for item in evidence.facts}
        assert facts["gate_count"] == gate_count
        assert facts["relation_count"] == relation_count
    assert requirement_evidence["published_static_argparse_call_projection"] == (static_projection,)
    replay_plan = plan_code_experiments((spec,), (projected,))
    assert replay_plan.status == "not_required"
    assert replay_plan.proposals == ()


def test_knowledge_health_receipt_projects_counterevidence_and_exact_causal_result(
    tmp_path: Path,
) -> None:
    specs, evaluations = knowledge_asset_health_questions(
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank=1,
    )
    spec = specs[0]
    evaluation = evaluations[0]
    base_evidence = evaluation.evidence
    plan = plan_code_experiments(specs, evaluations)
    proposal = plan.proposals[0]
    assert proposal.template_id == "knowledge.asset_health_causal_acceptance"
    assert proposal.mutation_authority is False

    database = _database(tmp_path)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:knowledge-health-fixture",
    )
    projected = apply_code_experiment_receipts(specs, evaluations, plan, (stored,))[0]

    assert projected.decision_readiness == "human_review_required"
    assert projected.counterevidence_status == "evaluated"
    assert projected.next_action_ids == ()
    assert all(item in projected.evidence for item in base_evidence)
    evidence_by_id = {item.evidence_id: item for item in projected.evidence}
    requirement_evidence = {
        item.requirement_id: tuple(evidence_by_id[evidence_id] for evidence_id in item.evidence_ids)
        for item in projected.requirements
    }
    expected = {
        ("knowledge_asset_health_stale_mismatch_and_absence_counterevidence_evaluated"): (
            "counterevidence",
            "runtime_observation",
            2,
            9,
        ),
        "isolated_knowledge_asset_health_causal_experiment_result": (
            "experiment_result",
            "experiment_result",
            4,
            12,
        ),
    }
    for requirement_id, (role, kind, gate_count, relation_count) in expected.items():
        linked = requirement_evidence[requirement_id]
        assert len(linked) == 1
        evidence = linked[0]
        assert (evidence.role, evidence.evidence_kind) == (role, kind)
        assert evidence.source_record_id == stored.receipt.receipt_id
        facts = {item.name: item.value for item in evidence.facts}
        assert facts["gate_count"] == gate_count
        assert facts["relation_count"] == relation_count
    replay_plan = plan_code_experiments((spec,), (projected,))
    assert replay_plan.status == "not_required"
    assert replay_plan.proposals == ()


def test_pdf_health_receipt_projects_negative_controls_and_exact_causal_result(
    tmp_path: Path,
) -> None:
    specs, evaluations = knowledge_pdf_asset_health_questions(
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank=1,
    )
    spec = specs[0]
    evaluation = evaluations[0]
    base_evidence = evaluation.evidence
    plan = plan_code_experiments(specs, evaluations)
    proposal = plan.proposals[0]
    assert proposal.template_id == "knowledge.pdf_asset_health_causal_acceptance"
    assert proposal.mutation_authority is False

    database = _database(tmp_path)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:knowledge-pdf-health-fixture",
    )
    projected = apply_code_experiment_receipts(specs, evaluations, plan, (stored,))[0]

    assert projected.decision_readiness == "human_review_required"
    assert projected.counterevidence_status == "evaluated"
    assert projected.next_action_ids == ()
    assert all(item in projected.evidence for item in base_evidence)
    evidence_by_id = {item.evidence_id: item for item in projected.evidence}
    requirement_evidence = {
        item.requirement_id: tuple(evidence_by_id[evidence_id] for evidence_id in item.evidence_ids)
        for item in projected.requirements
    }
    expected = {
        "knowledge_pdf_asset_health_partial_protected_recovery_counterevidence_evaluated": (
            "counterevidence",
            "runtime_observation",
            3,
            9,
        ),
        "isolated_knowledge_pdf_asset_health_causal_experiment_result": (
            "experiment_result",
            "experiment_result",
            4,
            12,
        ),
    }
    for requirement_id, (role, kind, gate_count, relation_count) in expected.items():
        linked = requirement_evidence[requirement_id]
        assert len(linked) == 1
        evidence = linked[0]
        assert (evidence.role, evidence.evidence_kind) == (role, kind)
        assert evidence.source_record_id == stored.receipt.receipt_id
        facts = {item.name: item.value for item in evidence.facts}
        assert facts["gate_count"] == gate_count
        assert facts["relation_count"] == relation_count
    replay_plan = plan_code_experiments((spec,), (projected,))
    assert replay_plan.status == "not_required"
    assert replay_plan.proposals == ()


@pytest.mark.parametrize("second_recorded_ns", [10, 9])
def test_receipt_publication_order_rejects_timestamp_ties_and_inversion(
    tmp_path: Path,
    second_recorded_ns: int,
) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    database = _database(tmp_path)
    record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
        recorded_ns=10,
    )

    with pytest.raises(CodeExperimentStoreError, match="order_not_monotonic"):
        record_code_experiment_receipt(
            database,
            _receipt(proposal, status="failed"),
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
            recorded_ns=second_recorded_ns,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT receipt_status FROM code_experiment_receipts"
        ).fetchall() == [("passed",)]


def test_receipt_cannot_be_rebound_to_another_snapshot_or_proposal(tmp_path: Path) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    receipt = _receipt(proposal)
    database = _database(tmp_path)

    with pytest.raises(ValueError, match="selected proposal"):
        record_code_experiment_receipt(
            database,
            receipt,
            proposal,
            analysis_run_id=1,
            processing_signature="another-snapshot",
            review_digest="review:fixture",
        )


def test_idempotent_retry_rejects_denormalized_context_drift(tmp_path: Path) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    receipt = _receipt(proposal)
    database = _database(tmp_path)
    record_code_experiment_receipt(
        database,
        receipt,
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER code_experiment_receipts_no_update")
        connection.execute("UPDATE code_experiment_receipts SET template_version='forged-version'")
        connection.execute(code_schema._V6_DDL[3])

    with pytest.raises(CodeExperimentStoreError, match="envelope_invalid"):
        record_code_experiment_receipt(
            database,
            receipt,
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
        )


def test_reader_rejects_durable_envelope_context_drift(tmp_path: Path) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    database = _database(tmp_path)
    record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER code_experiment_receipts_no_update")
        connection.execute("UPDATE code_experiment_receipts SET review_digest='review:forged'")
        connection.execute(code_schema._V6_DDL[3])

    with pytest.raises(CodeExperimentStoreError, match="envelope_invalid"):
        read_code_experiment_receipts(
            database,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            plan=plan,
        )


def test_reader_rejects_semantically_equivalent_but_noncanonical_payload_bytes(
    tmp_path: Path,
) -> None:
    spec, evaluation = _question()
    plan = plan_code_experiments((spec,), (evaluation,))
    proposal = plan.proposals[0]
    database = _database(tmp_path)
    record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER code_experiment_receipts_no_update")
        connection.execute(
            """UPDATE code_experiment_receipts
            SET payload_json=' '||payload_json,payload_bytes=payload_bytes+1"""
        )
        connection.execute(code_schema._V6_DDL[3])

    with pytest.raises(CodeExperimentStoreError, match="payload_invalid"):
        read_code_experiment_receipts(
            database,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            plan=plan,
        )


def test_receipt_schema_rejects_payload_length_and_schema_drift(tmp_path: Path) -> None:
    spec, evaluation = _question()
    proposal = plan_code_experiments((spec,), (evaluation,)).proposals[0]
    database = _database(tmp_path)
    record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER code_experiment_receipts_no_update")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE code_experiment_receipts SET payload_bytes=payload_bytes+1")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE code_experiment_receipts SET receipt_schema='future-receipt/v99'"
            )
        connection.execute(code_schema._V6_DDL[3])


def test_review_consumes_a_persisted_receipt_and_does_not_propose_it_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    import _04_Nucleo_Operativo.code_review_epistemics as epistemics_module
    from _04_Nucleo_Operativo.code_experiment_store import code_review_digest_identity
    from tests.test_code_review import PROCESSING_SIGNATURE, _build_state, _status

    state_directory = tmp_path / "state"
    database = _build_state(state_directory, hotspots=False)
    spec, evaluation = _question(PROCESSING_SIGNATURE)

    def exact_questions(*_args, **_kwargs):
        return (spec,), (evaluation,)

    monkeypatch.setattr(review_module, "read_self_analysis_status", lambda *_: _status(tmp_path))
    monkeypatch.setattr(
        review_module,
        "expected_integrated_code_review_questions",
        exact_questions,
    )
    monkeypatch.setattr(
        epistemics_module,
        "expected_integrated_code_review_questions",
        exact_questions,
    )

    before = review_module.review_code_state(state_directory, limit=1)
    assert before.status == "ready"
    assert before.experiment_plan is not None
    assert before.experiment_plan.executable_count == 1
    assert before.experiment_receipts == ()
    proposal = before.experiment_plan.proposals[0]
    assert before.snapshot is not None and before.digest is not None

    record_code_experiment_receipt(
        database,
        _receipt(proposal, source_version=PROCESSING_SIGNATURE),
        proposal,
        analysis_run_id=before.snapshot.analysis_run_id,
        processing_signature=PROCESSING_SIGNATURE,
        review_digest=code_review_digest_identity(before.digest),
    )

    after = review_module.review_code_state(state_directory, limit=1)
    assert after.status == "ready"
    assert len(after.experiment_receipts) == 1
    assert after.question_evaluations[0].decision_readiness == "human_review_required"
    assert after.question_evaluations[0].decision is None
    assert after.experiment_plan is not None
    assert after.experiment_plan.status == "not_required"
    assert after.experiment_plan.executable_count == 0
    assert after.digest != before.digest
    payload = after.as_payload()
    assert payload["schema"] == "neocortex.code-review/v22"
    assert len(payload["experiment_receipts"]) == 1
