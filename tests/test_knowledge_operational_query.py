"""Focused contract tests for the read-only operational question seam."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from neocortex.knowledge.knowledge_asset_diagnosis_contracts import (
    AssetDiagnosticCertainty,
    AssetProblemScope,
)
from neocortex.knowledge.knowledge_operational_query import (
    KnowledgeOperationalQueryService,
    OperationalFact,
    OperationalFederatedCursor,
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


def test_archive_issue_record_ids_distinguish_rows_sharing_container_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neocortex.api.content_diagnostics_api as diagnostics

    monkeypatch.setattr(
        diagnostics,
        "content_diagnostics_payload",
        lambda *args, **kwargs: {
            "status": "ok", "owner": "archive", "operation": "archive-issues",
            "snapshot_id": "archive-revision-1", "requested_root": str(tmp_path),
            "reason_field": "reason_code", "items": [
                {"issue_id": 3, "container_key": "same", "member_chain": "inner.zip!/a",
                 "reason_code": "archive_pdf_extraction_error"},
                {"issue_id": 4, "container_key": "same", "member_chain": "inner.zip!/a",
                 "reason_code": "archive_pdf_extraction_error"},
            ], "matched_count": None, "next_cursor": None,
            "coverage": {"snapshot_consistent": True},
        },
    )
    result = KnowledgeOperationalQueryService().query(
        _request(tmp_path, "¿Qué problemas hay dentro de los ZIP?")
    )
    assert len(result.facts) == 2
    assert len({fact.record_id for fact in result.facts}) == 2
    assert {fact.record_id for fact in result.facts} == {
        "archive:same:issue:3", "archive:same:issue:4"
    }


def test_pdf_multi_error_records_keep_distinct_ids_for_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neocortex.api.content_diagnostics_api as diagnostics

    monkeypatch.setattr(
        diagnostics,
        "content_diagnostics_payload",
        lambda *args, **kwargs: {
            "status": "ok", "owner": "pdf", "operation": "pdf-diagnostics",
            "snapshot_id": "pdf-revision-1", "requested_root": str(tmp_path),
            "reason_field": "error_type", "items": [
                {"file_key": "same", "path": "/corpus/a.pdf", "error_type": "DecodeError"},
                {"file_key": "same", "path": "/corpus/a.pdf", "error_type": "PdfDocumentTimeout"},
            ], "matched_count": 2, "next_cursor": None,
            "coverage": {"snapshot_consistent": True},
        },
    )
    result = KnowledgeOperationalQueryService().query(
        _request(tmp_path, "¿Qué error tiene este PDF?")
    )
    assert {fact.record_id for fact in result.facts} == {
        "pdf:same:DecodeError", "pdf:same:PdfDocumentTimeout"
    }


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


def test_federated_cursor_continues_each_owner_without_repeating_exhausted_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners = ("pdf", "text", "archive", "office")

    def fact(owner: str, number: int) -> OperationalFact:
        return OperationalFact(
            AssetProblemScope.PROCESSING, f"{owner}_error_{number}",
            AssetDiagnosticCertainty.OBSERVED, owner, f"{owner}:{number}",
            f"{owner}-snapshot", {"record": {"path": f"/corpus/{owner}-{number}"}},
        )

    def owner_result(self, request, intent, owner):
        number = 2 if request.cursor is not None else 1
        return OperationalQueryResult(
            request.query, intent, OperationalOwner(owner), "ok", (fact(owner, number),),
            f"{owner}-snapshot", f"{owner}-cursor" if request.cursor is None else None,
            {"status": "observed"},
        )

    monkeypatch.setattr(KnowledgeOperationalQueryService, "_owner_result", owner_result)
    service = KnowledgeOperationalQueryService()
    first = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?"))
    assert first.next_cursor is not None
    token = OperationalFederatedCursor.from_token(first.next_cursor)
    assert dict(token.owner_cursors) == {owner: f"{owner}-cursor" for owner in owners}
    assert dict(token.owner_snapshots) == {owner: f"{owner}-snapshot" for owner in owners}

    second = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?", cursor=first.next_cursor))
    assert second.status == "ok"
    assert {item.code for item in second.facts} == {f"{owner}_error_2" for owner in owners}
    assert second.next_cursor is None


def test_federated_cursor_rejects_adulteration_and_binding_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def owner_result(self, request, intent, owner):
        return OperationalQueryResult(
            request.query, intent, OperationalOwner(owner), "ok", (),
            f"{owner}-snapshot", f"{owner}-cursor",
            {"status": "observed"},
        )

    monkeypatch.setattr(KnowledgeOperationalQueryService, "_owner_result", owner_result)
    service = KnowledgeOperationalQueryService()
    first = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?"))
    assert first.next_cursor
    adulterated = first.next_cursor[:-1] + ("A" if first.next_cursor[-1] != "A" else "B")
    invalid = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?", cursor=adulterated))
    rebound = service.query(_request(tmp_path, "¿Qué otros errores tienen mis archivos?", cursor=first.next_cursor))
    assert invalid.error and invalid.error["code"] == "invalid_federated_cursor"
    assert rebound.error and rebound.error["code"] == "federated_cursor_binding_mismatch"


@pytest.mark.parametrize("token", ("!", "%%%%", "eyJkaWdlc3Qi", "A" * 7))
def test_federated_cursor_rejects_malformed_base64_without_leaking_decoder_errors(token: str) -> None:
    with pytest.raises(ValueError, match="invalid federated cursor token"):
        OperationalFederatedCursor.from_token(token)


def test_federated_query_normalizes_non_utf8_cursor_payload(tmp_path: Path) -> None:
    token = base64.urlsafe_b64encode(b'{"digest":\xff').decode("ascii").rstrip("=")
    result = KnowledgeOperationalQueryService().query(
        _request(tmp_path, "¿Qué errores tienen mis archivos?", cursor=token)
    )
    assert result.status == "blocked"
    assert result.error and result.error["code"] == "invalid_federated_cursor"


def test_federated_continuation_abstains_without_mixing_when_owner_snapshot_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def owner_result(self, request, intent, owner):
        changed = owner == "archive" and request.cursor is not None
        snapshot = f"{owner}-snapshot-changed" if changed else f"{owner}-snapshot"
        return OperationalQueryResult(
            request.query, intent, OperationalOwner(owner), "ok",
            (OperationalFact(
                AssetProblemScope.PROCESSING, f"{owner}_error", AssetDiagnosticCertainty.OBSERVED,
                owner, f"{owner}:1", snapshot, {"record": {"path": f"/corpus/{owner}"}},
            ),), snapshot, f"{owner}-cursor" if request.cursor is None else None,
            {"status": "observed"},
        )

    monkeypatch.setattr(KnowledgeOperationalQueryService, "_owner_result", owner_result)
    service = KnowledgeOperationalQueryService()
    first = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?"))
    second = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?", cursor=first.next_cursor))
    assert second.status == "snapshot_changed"
    assert second.facts == ()
    assert second.error and second.error["code"] == "snapshot_changed"
    assert second.coverage["changed_owners"] == ["archive"]


def test_federated_continuation_keeps_an_exhausted_owner_cursor_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {}

    def owner_result(self, request, intent, owner):
        calls[owner] = calls.get(owner, 0) + 1
        if owner == "archive" and calls[owner] > 1:
            # A fresh first-page read can naturally expose a cursor again;
            # the federated continuation must not resurrect it.
            next_cursor = "archive-fresh-cursor"
        elif owner == "archive":
            next_cursor = None
        elif owner == "pdf":
            next_cursor = "pdf-cursor" if request.cursor is None else "pdf-next-2"
        else:
            next_cursor = f"{owner}-cursor" if request.cursor is None else None
        return OperationalQueryResult(
            request.query, intent, OperationalOwner(owner), "ok",
            (OperationalFact(
                AssetProblemScope.PROCESSING, f"{owner}_error_{calls[owner]}",
                AssetDiagnosticCertainty.OBSERVED, owner, f"{owner}:{calls[owner]}",
                f"{owner}-snapshot", {"record": {"path": f"/corpus/{owner}"}},
            ),), f"{owner}-snapshot", next_cursor, {"status": "observed"},
        )

    monkeypatch.setattr(KnowledgeOperationalQueryService, "_owner_result", owner_result)
    service = KnowledgeOperationalQueryService()
    first = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?"))
    assert first.next_cursor
    first_cursor = OperationalFederatedCursor.from_token(first.next_cursor)
    assert dict(first_cursor.owner_cursors)["archive"] is None

    second = service.query(_request(tmp_path, "¿Qué errores tienen mis archivos?", cursor=first.next_cursor))
    assert second.next_cursor
    second_cursor = OperationalFederatedCursor.from_token(second.next_cursor)
    assert dict(second_cursor.owner_cursors)["archive"] is None
    assert second.coverage["owners"]["archive"]["next_cursor"] == "archive-fresh-cursor"
    assert all(not fact.code.startswith("archive_error") for fact in second.facts)
