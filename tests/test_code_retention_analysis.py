from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from _02_Deduplicacion.inventory_schema import initialize_inventory_schema
from _04_Nucleo_Operativo.code_retention_analysis import (
    RETENTION_HOLD_QUESTION,
    CodeRetentionResolutionError,
    analyze_code_retention,
    parse_code_retention_analysis_payload,
    resolve_code_retention,
    retention_questions,
)
from _04_Nucleo_Operativo.document_catalog import initialize_document_catalog
from _04_Nucleo_Operativo.framework_state_writer import FrameworkState
from _04_Nucleo_Operativo.semantic_state import initialize_semantic_state


REFERENCE_NS = 10_000_000_000
SOURCE_VERSION = "neocortex.code-review/v20"


def _initialized_state(path: Path) -> Path:
    path.mkdir()
    initialize_semantic_state(path / "semantic.sqlite3")
    initialize_document_catalog(path / "document_catalog.sqlite3")
    initialize_inventory_schema(path / "dedup.sqlite3")
    with FrameworkState(path / "framework.sqlite3"):
        pass
    return path


def test_retention_analysis_projects_all_declared_holds_without_mutation(
    tmp_path: Path,
) -> None:
    state = _initialized_state(tmp_path / "state")
    databases = tuple(
        state / name
        for name in (
            "semantic.sqlite3",
            "document_catalog.sqlite3",
            "dedup.sqlite3",
            "framework.sqlite3",
        )
    )
    before = {item.name: item.read_bytes() for item in databases}

    analysis = analyze_code_retention(
        state,
        source_version=SOURCE_VERSION,
        reference_time_ns=REFERENCE_NS,
    )
    specs, evaluations = retention_questions(analysis, rank=1)

    assert analysis.status == "ready"
    assert analysis.observation == "declared_holds_resolved"
    assert analysis.missing_hold_ids == ()
    assert tuple(item.store for item in analysis.stores) == (
        "semantic",
        "catalog",
        "inventory",
        "framework",
    )
    assert all(item.status == "ready" for item in analysis.stores)
    assert set(analysis.stores[0].hold_names) == {
        "semantic_model_registry",
        "shared_semantic_payload_and_evidence",
        "shared_semantic_source_content",
    }
    assert set(analysis.stores[3].hold_names) == {
        "file_action_audit_evidence",
        "human_review_evidence",
        "published_review_task_state",
    }
    assert specs == (RETENTION_HOLD_QUESTION,)
    evaluation = evaluations[0]
    assert evaluation.observation_status == "confirmed"
    assert evaluation.question_readiness == "ready"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.decision is None
    assert evaluation.authority == "advisory"
    assert evaluation.mutation_authority is False
    assert {item.status for item in evaluation.requirements} == {"satisfied", "missing"}
    assert evaluation.requirements[-1].requirement_id == (
        "isolated_retention_safety_experiment_result"
    )
    assert evaluation.requirements[-1].status == "missing"
    assert {item.name: item.read_bytes() for item in databases} == before


def test_retention_analysis_payload_is_exact_and_tamper_evident(tmp_path: Path) -> None:
    analysis = analyze_code_retention(
        _initialized_state(tmp_path / "state"),
        source_version=SOURCE_VERSION,
        reference_time_ns=REFERENCE_NS,
    )
    payload = analysis.as_payload()

    assert parse_code_retention_analysis_payload(payload) == analysis

    forged = dict(payload)
    forged["observation"] = "retention_gap_observed"
    with pytest.raises(ValueError, match="retention observation is not derived"):
        parse_code_retention_analysis_payload(forged)

    forged = dict(payload)
    forged["analysis_id"] = "code-retention-analysis-v1:forged"
    with pytest.raises(ValueError, match="identity is invalid"):
        parse_code_retention_analysis_payload(forged)


def test_missing_hold_is_preserved_as_counterevidence_not_a_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_retention_analysis as module

    state = _initialized_state(tmp_path / "state")
    original = module.plan_retention

    def without_framework_review_hold(*args: object, **kwargs: object):
        plan = original(*args, **kwargs)
        framework = plan.stores[-1]
        return replace(
            plan,
            stores=(
                *plan.stores[:-1],
                replace(
                    framework,
                    holds=tuple(
                        item
                        for item in framework.holds
                        if item.name != "published_review_task_state"
                    ),
                ),
            ),
        )

    monkeypatch.setattr(module, "plan_retention", without_framework_review_hold)
    analysis = module.analyze_code_retention(
        state,
        source_version=SOURCE_VERSION,
        reference_time_ns=REFERENCE_NS,
    )
    _specs, evaluations = module.retention_questions(analysis, rank=2)

    assert analysis.observation == "retention_gap_observed"
    assert analysis.missing_hold_ids == ("framework:published_review_task_state",)
    evaluation = evaluations[0]
    assert evaluation.observation_status == "abstained"
    assert evaluation.question_readiness == "abstained"
    assert evaluation.decision_readiness == "abstained"
    assert evaluation.decision is None
    assert evaluation.next_action_ids == ()


def test_resolution_rejects_a_changed_owner_projection(tmp_path: Path) -> None:
    state = _initialized_state(tmp_path / "state")
    analysis = analyze_code_retention(
        state,
        source_version=SOURCE_VERSION,
        reference_time_ns=REFERENCE_NS,
    )
    with sqlite3.connect(state / "semantic.sqlite3") as connection:
        connection.execute(
            """INSERT INTO embedding_generations(
            generation_id,model_signature,processing_signature,status,
            provenance_json,cursor_json,started_ns,completed_ns,base_clone_complete)
            VALUES(1,'fixture-model','fixture','failed','{}','{}',1,1,1)"""
        )
        connection.commit()

    with pytest.raises(CodeRetentionResolutionError, match="changed during review"):
        resolve_code_retention(
            state,
            analysis,
            reference_time_ns=REFERENCE_NS,
        )


def test_absent_state_is_an_explicit_gap_and_creates_nothing(tmp_path: Path) -> None:
    state = tmp_path / "absent"

    analysis = analyze_code_retention(
        state,
        source_version=SOURCE_VERSION,
        reference_time_ns=REFERENCE_NS,
    )
    _specs, evaluations = retention_questions(analysis, rank=1)

    assert analysis.status == "ready"
    assert analysis.observation == "retention_gap_observed"
    assert [item.status for item in analysis.stores] == ["absent"] * 4
    assert evaluations[0].question_readiness == "abstained"
    assert evaluations[0].decision is None
    assert not state.exists()


def test_truncated_page_remains_ready_with_an_exact_resume_cursor(tmp_path: Path) -> None:
    state = _initialized_state(tmp_path / "state")
    with sqlite3.connect(state / "semantic.sqlite3") as connection:
        connection.executemany(
            """INSERT INTO embedding_generations(
            generation_id,model_signature,processing_signature,status,
            provenance_json,cursor_json,started_ns,completed_ns,base_clone_complete)
            VALUES(?,'fixture-model','fixture','failed','{}','{}',1,1,1)""",
            ((item,) for item in range(1, 102)),
        )
        connection.commit()

    analysis = analyze_code_retention(
        state,
        source_version=SOURCE_VERSION,
        reference_time_ns=REFERENCE_NS,
    )
    _specs, evaluations = retention_questions(analysis, rank=1)
    semantic = analysis.stores[0]

    assert semantic.item_count == 100
    assert semantic.truncated is True
    assert semantic.next_after == 100
    assert evaluations[0].question_readiness == "ready"
    owner_evidence = next(
        item
        for item in evaluations[0].evidence
        if item.source_record_kind == "retention_store_projection"
    )
    assert owner_evidence.completeness == "partial"
    assert owner_evidence.bounded is True
    assert owner_evidence.truncated is True
