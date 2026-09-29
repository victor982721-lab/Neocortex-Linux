"""Fast Curation gate for the physical organization contract."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.documents.curation_state import (
    CurationDecisionRecord,
    CurationTopCandidate,
    upsert_curation_decision_batch,
)
from neocortex.documents.document_catalog import (
    document_catalog_database,
    initialize_document_catalog,
)
from neocortex.documents.document_organization_models import DEFAULT_ORGANIZATION_DIRECTORY_NAME
from neocortex.documents.document_organization_planning import _proposed_destination
from neocortex.documents.document_resource_binding import build_resource_binding
from neocortex.documents.semantic_curation_gate import (
    controlled_document_kind,
    validate_current_fast_curation_decision,
)
from neocortex.foundation.file_identity import FileIdentity
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.semantic.fast_curation_policy import CalibrationParameters, FastCurationPolicy
from neocortex.semantic.fast_curation_policy_bundle import FastCurationPolicyBundle


FINGERPRINT = "a" * 64


def _bundle() -> FastCurationPolicyBundle:
    calibration = CalibrationParameters(
        "cal-v1", "model-v1", True, {"document_kind": 0.70}, {"document_kind": 0.10}
    )
    policy = FastCurationPolicy.from_calibration(calibration)
    return FastCurationPolicyBundle(
        policy,
        "model-v1",
        "fast-curation-document-representation-v1",
        "fixture-v1",
        "fixture-prototype-v1",
        policy.policy_version,
        "cal-v1",
        FINGERPRINT,
        "neocortex.synthetic-fast-curation",
    )


def _fixture(
    tmp_path: Path,
) -> tuple[Path, dict[str, object], CurationDecisionRecord, FastCurationPolicyBundle]:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"technical source")
    observed = source.stat()
    identity = FileIdentity(observed.st_dev, observed.st_ino)
    birth = stat_birthtime_ns(observed)
    binding = build_resource_binding(
        source_kind="pdf",
        file_key="pdf:1",
        path=str(source),
        identity=identity,
        birthtime_ns=birth,
        size=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
    )
    row = {
        "source_kind": "pdf",
        "file_key": "pdf:1",
        "path": str(source),
        "volume_id": str(identity.volume_id),
        "file_id": str(identity.file_id),
        "size": observed.st_size,
        "mtime_ns": observed.st_mtime_ns,
        "birthtime_ns": birth,
        "active": 1,
        "processing_signature": "input-v1",
        "text_fingerprint": "input-v1",
        "resource_binding_json": json.dumps(binding, sort_keys=True),
        "catalog_status": "classified",
        "classification_json": json.dumps({"primary_kind": "otro"}),
        "primary_kind": "otro",
        "primary_authority": None,
        "primary_organization": None,
        "primary_client": None,
        "primary_project": None,
        "primary_workstream": None,
    }
    bundle = _bundle()
    decision = CurationDecisionRecord(
        source_kind="pdf",
        file_key="pdf:1",
        source_binding=binding,
        input_signature="input-v1",
        semantic_representation_fingerprint="b" * 64,
        representation_version=bundle.representation_version,
        model_signature=bundle.model_signature,
        role="document",
        vector_space="curation-text",
        dimensions=3,
        ontology_version=bundle.ontology_version,
        prototype_version=bundle.prototype_version,
        policy_version=bundle.policy_version,
        calibration_version=bundle.calibration_version,
        decision="CLASSIFIED",
        top1_label="document.kind.informe_tecnico",
        top1_score=0.91,
        top2_label="document.kind.manual_equipo",
        top2_score=0.20,
        margin=0.71,
        top_k=(
            CurationTopCandidate("document.kind.informe_tecnico", 0.91),
            CurationTopCandidate("document.kind.manual_equipo", 0.20),
        ),
        evidence={"prototype_set_fingerprint": FINGERPRINT, "source": "fast-curation"},
        context_provenance={"original_path": str(source)},
        created_ns=20,
    )
    return tmp_path / "catalog.sqlite3", row, decision, bundle


def _open_fixture(tmp_path: Path):
    catalog, row, decision, bundle = _fixture(tmp_path)
    initialize_document_catalog(catalog)
    with document_catalog_database(catalog) as connection:
        columns = [str(item[1]) for item in connection.execute("PRAGMA table_info(documents)")]
        values: dict[str, object] = {
            **row,
            "source_status": "done",
            "classifier_signature": "classifier-v1",
            "primary_subtype": None,
            "confidence": 0.1,
            "uncertainty": "unknown",
            "standard_references_json": "[]",
            "organizations_json": "[]",
            "clients_json": "[]",
            "projects_json": "[]",
            "workstreams_json": "[]",
            "topics_json": "[]",
            "equipment_json": "[]",
            "activities_json": "[]",
            "error_type": None,
            "error_message": None,
            "last_seen_catalog_run_id": 1,
            "updated_ns": 1,
        }
        connection.execute(
            f"INSERT INTO documents({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(values.get(column) for column in columns),
        )
        upsert_curation_decision_batch(connection, [decision])
        connection.commit()
    return catalog, decision, bundle


def test_default_physical_root_is_corpus_ordenado() -> None:
    assert DEFAULT_ORGANIZATION_DIRECTORY_NAME == "Corpus_ordenado"


@pytest.mark.parametrize(
    ("label", "expected"),
    (
        ("document.kind.informe_tecnico", "informe_tecnico"),
        ("informe técnico", "informe_tecnico"),
        ("document.kind.normativa", "normativa"),
        ("document.kind.not_controlled", None),
        ("otro", "otro"),
    ),
)
def test_document_kind_directory_mapping_is_controlled(label: str, expected: str | None) -> None:
    assert controlled_document_kind(label) == expected


def test_current_classified_fast_decision_controls_destination(tmp_path: Path) -> None:
    catalog, _decision, bundle = _open_fixture(tmp_path)
    root = tmp_path / "organized"
    with document_catalog_database(catalog) as connection:
        row = connection.execute("SELECT * FROM documents").fetchone()
        destination, status, reason = _proposed_destination(
            row,
            root,
            min_confidence=0.99,
            managed_source=False,
            connection=connection,
            fast_curation_policy_bundle=bundle,
        )
    assert status == "planned"
    assert reason == "fast_curation_classified_current"
    assert destination is not None
    assert destination.parent == root / "Ingenieria_y_documentacion" / "Informes_y_referencias"


def test_missing_decision_abstains_without_destination(tmp_path: Path) -> None:
    catalog, _row, _decision, bundle = _fixture(tmp_path)
    initialize_document_catalog(catalog)
    with document_catalog_database(catalog) as connection:
        gate = validate_current_fast_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            policy_bundle=bundle,
        )
    assert not gate.eligible
    assert gate.reason == "catalog_current_document_missing"


def test_stale_version_abstains(tmp_path: Path) -> None:
    catalog, decision, bundle = _open_fixture(tmp_path)
    stale = replace(decision, model_signature="old-model")
    with document_catalog_database(catalog) as connection:
        upsert_curation_decision_batch(connection, [stale])
        gate = validate_current_fast_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            policy_bundle=bundle,
        )
    assert not gate.eligible
    assert gate.reason == "fast_curation_stale_model_signature"


def test_stale_plan_identity_abstains_even_when_binding_and_decision_match(
    tmp_path: Path,
) -> None:
    catalog, _decision, bundle = _open_fixture(tmp_path)
    with document_catalog_database(catalog) as connection:
        row = connection.execute("SELECT * FROM documents").fetchone()
        expected = (
            row["volume_id"],
            row["file_id"],
            row["size"] + 1,
            row["mtime_ns"],
            row["birthtime_ns"],
        )
        gate = validate_current_fast_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            policy_bundle=bundle,
            expected_identity=expected,
        )
    assert not gate.eligible
    assert gate.reason == "organization_source_identity_stale"


def test_wrong_label_abstains_without_using_taxonomy_primary_kind(tmp_path: Path) -> None:
    catalog, decision, bundle = _open_fixture(tmp_path)
    wrong = replace(
        decision,
        top1_label="untrusted-path-label",
        top_k=(CurationTopCandidate("untrusted-path-label", 0.91),),
    )
    with document_catalog_database(catalog) as connection:
        upsert_curation_decision_batch(connection, [wrong])
        gate = validate_current_fast_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            policy_bundle=bundle,
        )
    assert not gate.eligible
    assert gate.reason == "fast_curation_document_kind_uncontrolled"


def test_abstain_decision_never_routes(tmp_path: Path) -> None:
    catalog, decision, bundle = _open_fixture(tmp_path)
    abstain = replace(
        decision,
        decision="ABSTAIN",
        top1_label=None,
        top1_score=None,
        top_k=(),
        evidence={},
        context_provenance={},
    )
    with document_catalog_database(catalog) as connection:
        upsert_curation_decision_batch(connection, [abstain])
        gate = validate_current_fast_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            policy_bundle=bundle,
        )
    assert not gate.eligible
    assert gate.reason == "fast_curation_decision_abstain"


def test_persisted_classified_record_without_evidence_abstains(tmp_path: Path) -> None:
    catalog, _decision, bundle = _open_fixture(tmp_path)
    with document_catalog_database(catalog) as connection:
        connection.execute(
            "UPDATE curator_decisions SET evidence_json='{}' WHERE source_kind=? AND file_key=?",
            ("pdf", "pdf:1"),
        )
        gate = validate_current_fast_curation_decision(
            connection,
            source_kind="pdf",
            file_key="pdf:1",
            policy_bundle=bundle,
        )
    assert not gate.eligible
    assert gate.reason.startswith("fast_curation_decision_invalid:")
