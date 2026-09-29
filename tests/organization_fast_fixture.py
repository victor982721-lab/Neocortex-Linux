"""Small measured Fast Curation fixtures for legacy organization tests."""

from __future__ import annotations

import json
from pathlib import Path

from neocortex.documents.curation_state import (
    CurationDecisionRecord,
    CurationTopCandidate,
    upsert_curation_decision_batch,
)
from neocortex.documents.document_catalog import document_catalog_database
from neocortex.semantic.fast_curation_policy import CalibrationParameters, FastCurationPolicy
from neocortex.semantic.fast_curation_policy_bundle import FastCurationPolicyBundle


FINGERPRINT = "a" * 64


def organization_fast_bundle() -> FastCurationPolicyBundle:
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
        "neocortex.organization-test",
    )


def seed_organization_fast_decisions(
    catalog: Path,
    *,
    label_by_kind: dict[str, str] | None = None,
) -> FastCurationPolicyBundle:
    """Publish current calibrated decisions for every active physical fixture."""

    bundle = organization_fast_bundle()
    labels = label_by_kind or {}
    with document_catalog_database(catalog) as connection:
        records: list[CurationDecisionRecord] = []
        for row in connection.execute("SELECT * FROM documents WHERE active=1"):
            if str(row["catalog_status"]) != "classified":
                continue
            raw_binding = row["resource_binding_json"]
            if not isinstance(raw_binding, str):
                continue
            try:
                binding = json.loads(raw_binding)
            except ValueError:
                continue
            if not isinstance(binding, dict):
                continue
            try:
                classification = json.loads(str(row["classification_json"]))
            except ValueError:
                classification = {}
            if isinstance(classification, dict) and classification.get("taxonomy_status") in {
                "outside_taxonomy",
                "insufficient_identification",
            }:
                continue
            kind = str(row["primary_kind"] or "otro")
            label = labels.get(kind, f"document.kind.{kind}")
            input_signature = row["text_fingerprint"] or row["processing_signature"]
            if not isinstance(input_signature, str) or not input_signature:
                continue
            records.append(
                CurationDecisionRecord(
                    source_kind=str(row["source_kind"]),
                    file_key=str(row["file_key"]),
                    source_binding=binding,
                    input_signature=input_signature,
                    semantic_representation_fingerprint=FINGERPRINT,
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
                    top1_label=label,
                    top1_score=0.91,
                    top2_label="document.kind.manual_equipo",
                    top2_score=0.20,
                    margin=0.71,
                    top_k=(
                        CurationTopCandidate(label, 0.91),
                        CurationTopCandidate("document.kind.manual_equipo", 0.20),
                    ),
                    evidence={
                        "prototype_set_fingerprint": bundle.prototype_set_fingerprint,
                        "source": "organization-test-fixture",
                    },
                    context_provenance={"original_path": str(row["path"])},
                )
            )
        if records:
            upsert_curation_decision_batch(connection, records)
            connection.commit()
    return bundle


__all__ = ["organization_fast_bundle", "seed_organization_fast_decisions"]
