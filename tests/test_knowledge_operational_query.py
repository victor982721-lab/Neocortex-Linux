"""Focused contract tests for the read-only operational question seam."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.knowledge.knowledge_asset_diagnosis_contracts import (
    AssetDiagnosticCertainty,
    AssetProblemScope,
)
from neocortex.knowledge.knowledge_operational_query import (
    KnowledgeOperationalQueryService,
    OperationalFact,
    OperationalIntent,
    OperationalOwner,
    OperationalQueryRequest,
    OperationalQueryResult,
    detect_operational_intent,
)


def _request(tmp_path: Path, query: str, *, cursor: str | None = None) -> OperationalQueryRequest:
    root = tmp_path.absolute()
    return OperationalQueryRequest(query, root, root, limit=2, cursor=cursor)


@pytest.mark.parametrize(
    ("query", "intent"),
    (
        ("¿Qué PDFs están protegidos por contraseña?", OperationalIntent.PDF_PROTECTED),
        ("¿Qué error tiene este PDF?", OperationalIntent.PDF_ERROR),
        ("¿Qué problemas tiene la presentación PPTX?", OperationalIntent.OFFICE_ERROR),
        ("¿Qué problemas hay dentro de los ZIP?", OperationalIntent.ARCHIVE_ISSUE),
        ("¿Qué errores tienen mis archivos?", OperationalIntent.CORPUS_ERROR),
        ("¿Qué duplicados se pueden eliminar?", OperationalIntent.CURATION_DISPOSAL),
        ("¿Qué color tiene el archivo?", OperationalIntent.UNKNOWN),
    ),
)
def test_intent_detection_is_explicit_and_fail_closed(query: str, intent: OperationalIntent) -> None:
    assert detect_operational_intent(query) is intent


def test_unknown_intent_does_not_open_an_owner(tmp_path: Path) -> None:
    result = KnowledgeOperationalQueryService().query(_request(tmp_path, "¿Qué color tiene el archivo?"))
    assert result.status == "empty"
    assert result.owner is OperationalOwner.NONE
    assert result.error == {
        "code": "unsupported_operational_intent",
        "message": "no supported diagnostic intent",
    }
    assert result.to_dict()["mutation_authorized"] is False


def test_pdf_error_dispatches_existing_diagnostic_owner_and_preserves_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake(owner, state_directory, source_root, limit, **kwargs):
        calls.append({"owner": owner, "state_directory": state_directory, "source_root": source_root,
                      "limit": limit, **kwargs})
        return {
            "status": "ok", "owner": "pdf", "operation": "pdf-diagnostics",
            "requested_root": str(source_root), "reason_field": "error_type",
            "snapshot_id": "sha256:" + "a" * 64,
            "items": [{"file_key": "pdf-key", "path": "/corpus/bad.pdf", "error_type": "DecodeError"}],
            "matched_count": 1, "next_cursor": "next-pdf",
            "coverage": {"snapshot_consistent": True},
        }

    import neocortex.api.content_diagnostics_api as diagnostics

    monkeypatch.setattr(diagnostics, "content_diagnostics_payload", fake)
    result = KnowledgeOperationalQueryService().query(
        _request(tmp_path, "¿Qué error tiene este PDF?", cursor="cursor-pdf")
    )

    assert calls[0]["owner"] == "pdf"
    assert calls[0]["cursor"] == "cursor-pdf"
    assert result.status == "ok" and result.owner is OperationalOwner.PDF
    assert result.next_cursor == "next-pdf"
    assert result.facts[0].scope is AssetProblemScope.PROCESSING
    assert result.facts[0].certainty is AssetDiagnosticCertainty.OBSERVED
    assert result.facts[0].code == "DecodeError"
    assert result.to_dict()["read_only"] is True


def test_archive_issue_maps_reason_code_to_processing_fact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import neocortex.api.content_diagnostics_api as diagnostics

    monkeypatch.setattr(
        diagnostics,
        "content_diagnostics_payload",
        lambda *args, **kwargs: {
            "status": "ok", "owner": "archive", "operation": "archive-issues",
            "snapshot_id": "archive-revision-1", "requested_root": str(tmp_path),
            "reason_field": "reason_code", "items": [{
                "container_key": "container-1", "container_path": "/corpus/a.zip",
                "reason_code": "archive_unsafe_member_name",
            }], "matched_count": None, "next_cursor": None,
            "coverage": {"snapshot_consistent": True},
        },
    )
    result = KnowledgeOperationalQueryService().query(_request(tmp_path, "¿Qué problemas hay dentro de los ZIP?"))
    assert result.owner is OperationalOwner.ARCHIVE
    assert result.facts[0].code == "archive_unsafe_member_name"
    assert result.facts[0].provenance["reason_field"] == "reason_code"
    assert result.coverage["query_page_complete"] is True


def test_protected_pdf_and_disposal_use_advisory_framework_owner_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Cursor:
        def to_token(self) -> str:
            return "next-review"

    class Page:
        availability = "ready"
        snapshot_id = "sha256:" + "b" * 64
        total_matching = 1
        next_cursor = Cursor()

        def to_dict(self):
            return {"items": [{
                "route_name": "pdf", "volume_id": "1", "file_id": "2",
                "reason_code": "pdf_password_required", "recommendation": "keep_protected",
            }]}

    import neocortex.workflow.review.review_candidate_query as review_query

    def fake(database, **kwargs):
        calls.append({"database": database, **kwargs})
        return Page()

    monkeypatch.setattr(review_query, "list_review_candidates_page", fake)
    service = KnowledgeOperationalQueryService()
    protected = service.query(_request(tmp_path, "¿Qué PDFs están protegidos?", cursor="cursor-review"))
    disposal = service.query(_request(tmp_path, "¿Qué duplicados se pueden eliminar?"))

    assert calls[0]["route_name"] == "pdf" and calls[0]["recommendation"] == "keep_protected"
    assert calls[0]["after"] == "cursor-review"
    assert calls[1]["recommendation"] == "deletion_candidate"
    assert protected.facts[0].scope is AssetProblemScope.PROCESSING
    assert disposal.facts[0].scope is AssetProblemScope.POLICY
    assert disposal.facts[0].provenance["mutation_authorized"] is False
    assert protected.next_cursor == disposal.next_cursor == "next-review"


def test_owner_failure_is_typed_and_does_not_publish_partial_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neocortex.api.content_diagnostics_api as diagnostics

    monkeypatch.setattr(
        diagnostics,
        "content_diagnostics_payload",
        lambda *args, **kwargs: {
            "status": "unavailable", "error": {"kind": "owner_missing", "message": "absent"},
        },
    )
    result = KnowledgeOperationalQueryService().query(_request(tmp_path, "¿Qué error tiene este PDF?"))
    assert result.status == "unavailable"
    assert result.facts == ()
    assert result.error and result.error["code"] == "owner_missing"


def test_generic_file_errors_federate_owner_facts_without_mixing_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fact(owner: str, code: str) -> OperationalFact:
        return OperationalFact(
            scope=AssetProblemScope.PROCESSING,
            code=code,
            certainty=AssetDiagnosticCertainty.OBSERVED,
            owner=owner,
            record_id=f"{owner}:1",
            snapshot_id=f"{owner}-snapshot",
            provenance={"record": {"path": f"/corpus/{owner}.bin"}},
        )

    def diagnostics(self, request, intent, owner):
        return OperationalQueryResult(
            request.query, intent, OperationalOwner(owner), "ok", (fact(owner, f"{owner}_error"),),
            f"{owner}-snapshot", None, {"status": "observed"},
        )

    def reviews(self, request, intent, **kwargs):
        return OperationalQueryResult(
            request.query, intent, OperationalOwner.FRAMEWORK, "ok", (fact("framework", "ppt_error"),),
            "framework-snapshot", None, {"status": "observed"},
        )

    monkeypatch.setattr(KnowledgeOperationalQueryService, "_format_diagnostics", diagnostics)
    monkeypatch.setattr(KnowledgeOperationalQueryService, "_review_candidates", reviews)
    service = KnowledgeOperationalQueryService()
    result = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?"))
    assert result.owner is OperationalOwner.FEDERATED
    assert result.status == "ok"
    assert {item.code for item in result.facts} == {
        "pdf_error", "text_error", "archive_error", "ppt_error"
    }
    assert result.coverage["owners"]["office"]["fact_count"] == 1
    assert result.snapshot_id is not None

    continued = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?", cursor="owner-cursor"))
    assert continued.status == "blocked"
    assert continued.error and continued.error["code"] == "federated_cursor_requires_owner_scope"


def test_request_rejects_relative_or_traversal_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        OperationalQueryRequest("pdf error", Path("relative"), tmp_path.absolute())
    with pytest.raises(ValueError):
        OperationalQueryRequest("pdf error", tmp_path.absolute(), Path("/tmp/../corpus"))
