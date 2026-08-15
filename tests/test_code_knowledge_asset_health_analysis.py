from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_knowledge_asset_health_analysis import (
    CODE_KNOWLEDGE_ASSET_HEALTH_ANALYSIS_SCHEMA,
    CODE_KNOWLEDGE_ASSET_HEALTH_POLICY,
    KNOWLEDGE_ASSET_HEALTH_QUESTION,
    KNOWLEDGE_ASSET_HEALTH_QUESTION_ID,
    KNOWLEDGE_ASSET_HEALTH_SUBJECT_KEY,
    build_knowledge_asset_health_contract_analysis,
    knowledge_asset_health_questions,
)
from _04_Nucleo_Operativo.file_identity import FileIdentity, encode_file_identity
from _04_Nucleo_Operativo.knowledge_asset_health import inspect_knowledge_asset_health
from _04_Nucleo_Operativo.knowledge_asset_health_contracts import (
    KnowledgeAssetHealthCompleteness,
    KnowledgeAssetHealthQuery,
    KnowledgeAssetHealthState,
)
from _04_Nucleo_Operativo.knowledge_contracts import PhysicalIdentityRef, ResourceRef
from _04_Nucleo_Operativo.knowledge_search_content import direct_resource_ref
from _04_Nucleo_Operativo.knowledge_snapshot import KnowledgeStatePaths
from neocortex.read_api import asset_health_payload


def test_contract_projection_pins_owner_identity_and_public_read_facts() -> None:
    analysis = build_knowledge_asset_health_contract_analysis()

    assert analysis.policy_id == CODE_KNOWLEDGE_ASSET_HEALTH_POLICY
    assert analysis.causal_stage_owners == ("inventory", "text", "catalog", "knowledge")
    assert analysis.state_owner_ids == ("inventory", "text", "catalog")
    assert analysis.state_store_ids == (
        "sqlite:dedup.sqlite3",
        "sqlite:text.sqlite3",
        "sqlite:document_catalog.sqlite3",
    )
    assert (
        analysis.inventory_schema_version,
        analysis.text_schema_version,
        analysis.catalog_schema_version,
        analysis.knowledge_contract_version,
        analysis.health_contract_version,
    ) == (10, 2, 7, 1, 1)
    assert analysis.text_route_version == "text-route-v2"
    assert analysis.resource_id_scheme == ("resource:file:{volume_id}:{file_id}:{birthtime_ns}")
    assert analysis.identity_components == ("volume_id", "file_id", "birthtime_ns")
    assert analysis.operation == "knowledge-health"
    assert analysis.read_only is analysis.advisory_only is True
    assert analysis.mutation_authority is False
    assert analysis.as_payload()["schema"] == CODE_KNOWLEDGE_ASSET_HEALTH_ANALYSIS_SCHEMA

    with pytest.raises(ValueError, match="fields are incompatible"):
        replace(analysis, read_only=False)  # type: ignore[arg-type]


def test_question_requires_runtime_counterevidence_and_isolated_experiment() -> None:
    specs, evaluations = knowledge_asset_health_questions(
        snapshot_id="snapshot:knowledge-health-fixture",
        snapshot_freshness="current",
        rank=7,
    )

    assert specs == (KNOWLEDGE_ASSET_HEALTH_QUESTION,)
    evaluation = evaluations[0]
    assert evaluation.question_id == KNOWLEDGE_ASSET_HEALTH_QUESTION_ID
    assert evaluation.subject.subject_key == KNOWLEDGE_ASSET_HEALTH_SUBJECT_KEY
    assert evaluation.rank == 7
    assert evaluation.question_readiness == "ready"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.counterevidence_status == "not_evaluated"
    assert evaluation.next_action_ids == ("run_knowledge_asset_health_causal_experiment",)
    assert tuple(item.requirement_id for item in evaluation.requirements) == (
        "knowledge_asset_health_owner_store_contract",
        "knowledge_asset_health_causal_identity_contract",
        "knowledge_asset_health_public_read_contract",
        "knowledge_asset_health_stale_mismatch_and_absence_counterevidence_evaluated",
        "isolated_knowledge_asset_health_causal_experiment_result",
    )
    assert tuple(item.status for item in evaluation.requirements) == (
        "satisfied",
        "satisfied",
        "satisfied",
        "not_evaluated",
        "missing",
    )
    assert {item.source_record_kind for item in evaluation.evidence} == {
        "knowledge_asset_health_owner_store_contract",
        "knowledge_asset_health_causal_identity_contract",
        "knowledge_asset_health_public_read_contract",
    }
    facts = {fact.name: fact.value for evidence in evaluation.evidence for fact in evidence.facts}
    assert facts["causal_stage_owners"] == "inventory,text,catalog,knowledge"
    assert facts["resource_id_scheme"] == ("resource:file:{volume_id}:{file_id}:{birthtime_ns}")
    assert facts["public_read"] == "neocortex.read_api.asset_health_payload"
    assert facts["read_only"] is True
    assert facts["mutation_authority"] is False
    assert all(item.mutation_authority is False for item in evaluation.evidence)


def test_public_read_is_read_only_and_resource_identity_is_strict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state-home"
    data_home = tmp_path / "data-home"
    config_home = tmp_path / "config-home"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with pytest.raises(ValueError):
        asset_health_payload("/corpus/docs/asset.txt", "personal")
    assert not state_home.exists()
    assert not data_home.exists()
    assert not config_home.exists()

    payload = asset_health_payload("resource:file:11:3:-1", "personal")

    assert payload["kind"] == "neocortex_scoped_asset_health"
    assert payload["read_only"] is True
    assert payload["resource_id"] == "resource:file:11:3:-1"
    assert len(payload["scopes"]) == 1  # type: ignore[arg-type]
    report = payload["scopes"][0]["asset_health"]  # type: ignore[index]
    assert report["health"] == "unknown"
    assert report["completeness"] == "no_evidence"
    assert report["reason_code"] == "published_inventory_evidence_absent"
    assert report["read_only"] is True
    assert report["advisory_only"] is True
    assert report["mutation_authorized"] is False
    assert not state_home.exists()
    assert not data_home.exists()
    assert not config_home.exists()


def test_search_and_health_share_identity_and_missing_state_never_becomes_healthy(
    tmp_path: Path,
) -> None:
    identity = FileIdentity(11, 3)
    search_resource, warnings = direct_resource_ref(
        source_kind="text",
        owner="text",
        source_identity=encode_file_identity(identity.volume_id, identity.file_id),
        identity=identity,
        birthtime_ns=-1,
        path="/corpus/docs/asset.txt",
        resource_ref_type=ResourceRef,
        physical_identity_ref_type=PhysicalIdentityRef,
    )
    query = KnowledgeAssetHealthQuery(search_resource.resource_id)
    state_root = tmp_path / "missing-state"

    report = inspect_knowledge_asset_health(
        KnowledgeStatePaths.from_directory(state_root),
        query,
    )

    assert warnings == ()
    assert search_resource.resource_id == query.resource_id == "resource:file:11:3:-1"
    assert query.identity.volume_id == identity.volume_id
    assert query.identity.file_id == identity.file_id
    assert query.identity.birthtime_ns == -1
    assert report.health is KnowledgeAssetHealthState.UNKNOWN
    assert report.completeness is KnowledgeAssetHealthCompleteness.NO_EVIDENCE
    assert report.reason_code == "published_inventory_evidence_absent"
    assert not state_root.exists()
