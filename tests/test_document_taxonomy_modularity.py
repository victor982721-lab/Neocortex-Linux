# region [00] Contexto del módulo
# Módulo: tests/test_document_taxonomy_modularity.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest
from neocortex.foundation.hash_compat import sha256

from neocortex.documents import document_taxonomy_models
from neocortex.documents.document_taxonomy import (
    BUILTIN_TAXONOMY_VERSION,
    CLASSIFIER_VERSION,
    AuthoritySpec,
    ClientSpec,
    DocumentClassification,
    DocumentSignals,
    OrganizationSpec,
    ProjectSpec,
    ScoredLabel,
    StandardReference,
    TechnicalTaxonomy,
    builtin_taxonomy,
    classify_document,
    document_classifier_signature,
    semantic_label_inventory,
)

TEST_CAPABILITIES = ("documents",)
# endregion [01]

# region [02] Implementación


def _payload_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256.sha256_128_hexdigest(payload)


def test_taxonomy_facade_preserves_public_model_identity() -> None:
    assert (
        AuthoritySpec,
        ClientSpec,
        DocumentClassification,
        DocumentSignals,
        OrganizationSpec,
        ProjectSpec,
        ScoredLabel,
        StandardReference,
        TechnicalTaxonomy,
    ) == (
        document_taxonomy_models.AuthoritySpec,
        document_taxonomy_models.ClientSpec,
        document_taxonomy_models.DocumentClassification,
        document_taxonomy_models.DocumentSignals,
        document_taxonomy_models.OrganizationSpec,
        document_taxonomy_models.ProjectSpec,
        document_taxonomy_models.ScoredLabel,
        document_taxonomy_models.StandardReference,
        document_taxonomy_models.TechnicalTaxonomy,
    )


def test_builtin_inventory_and_signature_remain_exact() -> None:
    taxonomy = builtin_taxonomy()

    assert BUILTIN_TAXONOMY_VERSION == "electrical-document-taxonomy-v13"
    assert CLASSIFIER_VERSION == "technical-document-classifier-v17"
    assert document_classifier_signature(taxonomy) == (
        "technical-document-classifier-v17|electrical-document-taxonomy-v13|"
        "technical-document-naming-v9"
    )
    assert _payload_fingerprint(asdict(taxonomy)) == (
        "9ed8a23d87c3eed6d92d0cec7647cc2f"
    )
    assert _payload_fingerprint(semantic_label_inventory()) == (
        "90854a04700bd569d30082bef8a11d13"
    )


@pytest.mark.parametrize(
    ("signals", "expected_fingerprint"),
    (
        (
            DocumentSignals(
                "pdf",
                r"C:\Normativa\IEEE Std C37.20.2-2015.pdf",
                "done",
                title="IEEE Std C37.20.2-2015 Metal-Clad Switchgear",
                leading_text="This standard applies to circuit breakers.",
            ),
            "8295b2174dd4b6a758bcfbe1905e7277",
        ),
        (
            DocumentSignals(
                "docx",
                r"C:\ANDRITZ\Formato inspeccion.docx",
                "complete",
                title="Formato de inspección ANDRITZ",
                author="ANDRITZ HYDRO",
                leading_text="Lista de verificación de pruebas eléctricas.",
            ),
            "54611049d1afafd9cf159b04b7d3810c",
        ),
        (
            DocumentSignals(
                "pdf",
                r"C:\Manuales\OMICRON CMC 356.pdf",
                "done",
                title="CMC 356 User Manual",
                author="OMICRON electronics",
                leading_text="Test equipment for protection and control.",
            ),
            "f38b739a5eb8c2ba5468d1177ad9de1d",
        ),
        (
            DocumentSignals(
                "pdf",
                r"C:\Campo\Packing List PL-0042.pdf",
                "done",
                leading_text=(
                    "PACKING LIST\nPackage No: BX-14\nGross Weight: 118 kg\n"
                    "Net Weight: 104 kg\nDimensions: 90 x 60 x 40 cm"
                ),
            ),
            "738bd61325a1008a68f349609448d316",
        ),
        (
            DocumentSignals(
                "audio",
                r"C:\Audio\Reunion seguimiento.mp3",
                "done",
                title="Reunión de seguimiento",
                leading_text=(
                    "Grabación de la reunión de trabajo. Orden del día y acuerdos."
                ),
            ),
            "60fd10a3808ace3fc6624e68c18f6958",
        ),
    ),
)
def test_representative_classification_payload_is_byte_stable(
    signals: DocumentSignals,
    expected_fingerprint: str,
) -> None:
    classification = classify_document(signals)

    payload = asdict(classification)
    for field in (
        "document_role", "role_evidence", "entity_roles", "issuer_status",
        "taxonomy_status", "contradictions", "unknowns", "confidence_kind",
    ):
        payload.pop(field)
    payload["classifier_signature"] = payload["classifier_signature"].replace(
        "classifier-v17", "classifier-v14"
    )
    assert _payload_fingerprint(payload) == expected_fingerprint
# endregion [02]
