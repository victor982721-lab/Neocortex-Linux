from __future__ import annotations

from pathlib import Path

from neocortex.api import read_api
from neocortex.api.read_contract import ReadOperation, validate_read_payload
from neocortex.knowledge.knowledge_asset_diagnosis_contracts import (
    AssetDiagnosticCertainty,
    AssetProblemScope,
)
from neocortex.knowledge.knowledge_operational_query import (
    OperationalFact,
    OperationalIntent,
    OperationalOwner,
    OperationalQueryResult,
)


def test_operational_query_payload_is_scope_bound_and_advisory(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    monkeypatch.setattr(read_api, "default_state_directory", lambda: state)
    from neocortex.platform import policy

    monkeypatch.setattr(policy, "default_corpus_root", lambda: corpus)

    def fake_query(self, request):
        return OperationalQueryResult(
            query=request.query,
            intent=OperationalIntent.PDF_ERROR,
            owner=OperationalOwner.PDF,
            status="ok",
            facts=(OperationalFact(
                scope=AssetProblemScope.PROCESSING,
                code="pdf_decode_error",
                certainty=AssetDiagnosticCertainty.OBSERVED,
                owner="pdf",
                record_id="pdf:bad",
                snapshot_id="sha256:" + "a" * 64,
                provenance={"record": {"path": "/corpus/bad.pdf"}},
            ),),
            snapshot_id="sha256:" + "b" * 64,
            next_cursor="next",
            coverage={"status": "observed", "owner_snapshot_consistent": True},
        )

    from neocortex.knowledge import knowledge_operational_query

    monkeypatch.setattr(knowledge_operational_query.KnowledgeOperationalQueryService, "query", fake_query)
    payload = read_api.operational_query_payload(
        "¿Qué error tiene este PDF?", "all", limit=2, cursor="cursor-1", request_id="req-1"
    )
    validate_read_payload(
        payload,
        ReadOperation.OPERATIONAL_QUERY,
        scope="all",
        query="¿Qué error tiene este PDF?",
        limit=2,
    )
    assert payload["request_id"] == "req-1"
    assert payload["scopes"][0]["operational"]["next_cursor"] == "next"
    assert payload["scopes"][0]["operational"]["mutation_authorized"] is False


def test_operational_snapshot_drift_uses_snapshot_changed_exit_code(
    tmp_path: Path, monkeypatch,
) -> None:
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    monkeypatch.setattr(read_api, "default_state_directory", lambda: state)
    from neocortex.platform import policy

    monkeypatch.setattr(policy, "default_corpus_root", lambda: corpus)
    from neocortex.knowledge import knowledge_operational_query

    def changed(self, request):
        return OperationalQueryResult(
            query=request.query,
            intent=OperationalIntent.CORPUS_ERROR,
            owner=OperationalOwner.FEDERATED,
            status="snapshot_changed",
            facts=(),
            snapshot_id="sha256:" + "c" * 64,
            next_cursor=None,
            coverage={"status": "snapshot_changed", "owner_snapshot_consistent": False},
            error={"code": "snapshot_changed", "message": "owner drift"},
        )

    monkeypatch.setattr(knowledge_operational_query.KnowledgeOperationalQueryService, "query", changed)
    payload = read_api.operational_query_payload(
        "¿Qué errores tienen mis archivos?", "personal", limit=2,
    )
    assert payload["exit_code"] == 5
    assert payload["scopes"][0]["exit_code"] == 5
