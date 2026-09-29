from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from neocortex.runtime.models import FrameworkConfig
import neocortex.runtime.orchestration.fast_curation_lifecycle as lifecycle


class _State:
    def __init__(self) -> None:
        self.stages: list[tuple[str, str, dict[str, object]]] = []

    def publish_run_stage(self, run_id, stage, status, *, details, idempotency_key=None):
        del run_id, idempotency_key
        self.stages.append((stage, status, dict(details)))


class _Cancellation:
    def checkpoint(self) -> None:
        return None


class _CatalogConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, parameters=()):
        if "SELECT path,active" in sql:
            row = self.rows[0]
            return _Rows([(row[2], 1, row[10], row[22])])
        if parameters and len(parameters) > 1:
            return _Rows([])
        if "SELECT source_kind,file_key,path FROM documents" in sql:
            values = [(row[0], row[1], row[2]) for row in self.rows]
            return _Rows(values)
        if "SELECT source_kind,file_key,path,volume_id" in sql:
            return _Rows(self.rows)
        raise AssertionError(f"unexpected catalog query: {sql}")


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _RouteSession:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True
        return False


def _config(tmp_path: Path) -> FrameworkConfig:
    root = tmp_path / "corpus"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    return FrameworkConfig(root=root, state_directory=state, route="all")


def test_missing_catalog_is_empty_and_does_not_load_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = _State()

    result = lifecycle.run_fast_curation_stage(
        config, root=config.root, state=state, run_id=7, cancellation=_Cancellation()
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "catalog_unavailable"
    assert result["candidates"] == 0
    assert state.stages[-1][1] == "skipped"


def test_current_route_snapshot_closes_before_decision_sink_and_model_failure_abstains(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    catalog_path = config.document_catalog_database
    catalog_path.touch()
    route_path = config.pdf_database
    route_path.touch()
    from neocortex.documents.document_resource_binding import legacy_resource_binding

    binding = json.dumps(
        legacy_resource_binding(
            source_kind="pdf", file_key="file-key", path=str(config.root / "report.pdf"),
            volume_id="1", file_id="2", birthtime_ns=1, size=10, mtime_ns=1,
        ),
        sort_keys=True,
    )
    row = (
        "pdf", "file-key", str(config.root / "report.pdf"), "v", "i", 10, 1, 1,
        "done", "route-v1", "text-v1", "report", None, None, None, None, None,
        None, "[]", "[]", "[]", 4, binding,
    )
    catalog = _CatalogConnection([row])
    route_connection = object()
    route_session = _RouteSession(route_connection)
    state = _State()
    persisted: list[object] = []

    @contextmanager
    def catalog_owner(path, *, readonly=False):
        del path, readonly
        yield catalog

    monkeypatch.setattr("neocortex.documents.document_catalog.document_catalog_database", catalog_owner)
    monkeypatch.setattr(lifecycle, "capture_sqlite_read_fence", lambda path: str(path))
    monkeypatch.setattr(lifecycle, "preferred_sqlite_read_mode", lambda path: "immutable_strict")
    monkeypatch.setattr(lifecycle, "SQLiteReadSession", lambda *args, **kwargs: route_session)
    monkeypatch.setattr(
        "neocortex.documents.document_catalog_text._load_leading_text",
        lambda connection, document, *, max_text_chars, cancellation=None: "Texto derivado de la ruta",
    )

    from neocortex.documents import curation_state

    monkeypatch.setattr(
        curation_state,
        "upsert_curation_decision_batch",
        lambda connection, records: persisted.extend(records) or len(records),
    )

    def fake_service(config, root, state_directory, **kwargs):
        inputs = kwargs["inputs"]
        representations = tuple(inputs)
        assert representations
        assert route_session.closed is True
        evidence = SimpleNamespace(
            source_kind="pdf", file_key="file-key", document_id="file-key",
            input_signature="text-v1", representation_fingerprint="a" * 32,
                representation_version="fast-curation-representation/v1",
            model_signature=config.model.model_signature, vector_space=config.model.vector_space,
            ontology_version="ontology-v1", prototype_version="prototype-v1",
            policy_version="policy-v1", calibration_version=None,
            calibrated_decision="abstain", decision_reason="capability_unavailable",
            top_candidates=(), top1_score=None, top2_score=None, margin=None,
            source_path=str(representations[0].path_context),
            deterministic_evidence=representations[0].deterministic_evidence,
            semantic_evidence={}, metadata_evidence={},
            structural_evidence={}, confidence_kind="not_calibrated",
        )
        kwargs["decision_sink"].persist_fast_curation_decisions(
            [evidence], root=root, state_directory=state_directory,
        )
        return SimpleNamespace(
            status="capability_partial",
            errors=("capability_unavailable",),
            metrics=SimpleNamespace(
                documents_seen=1, classified=0, abstained=1, cache_hits=0,
                cache_misses=0, embeddings_produced=0, vectors_produced=0,
                documents_escalated=0, model_available=False, calibration_loaded=False,
                persisted_decisions=1,
                as_dict=lambda: {"documents_seen": 1, "classified": 0, "abstained": 1,
                                  "persisted_decisions": 1, "model_available": False},
            ),
            decision_samples=(),
        )

    monkeypatch.setattr(
        "neocortex.semantic.fast_curation_service.run_fast_curation", fake_service
    )
    result = lifecycle.run_fast_curation_stage(
        config, root=config.root, state=state, run_id=8, cancellation=_Cancellation()
    )

    assert result["status"] == "partial"
    assert result["documents_seen"] == 1
    assert result["abstained"] == 1
    assert result["model_available"] is False
    assert len(persisted) == 1
    assert persisted[0].decision == "ABSTAIN"
    assert state.stages[-1][1] == "partial"


def test_catalog_without_current_documents_skips_without_service(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    config.document_catalog_database.touch()
    outside = tmp_path / "outside.pdf"
    row = (
        "pdf", "outside", str(outside), "v", "i", 1, 1, 1, "done", "route", "text",
        "report", None, None, None, None, None, None, "[]", "[]", "[]", 1,
    )
    catalog = _CatalogConnection([row])

    @contextmanager
    def catalog_owner(path, *, readonly=False):
        del path, readonly
        yield catalog

    monkeypatch.setattr("neocortex.documents.document_catalog.document_catalog_database", catalog_owner)
    monkeypatch.setattr(
        "neocortex.semantic.fast_curation_service.run_fast_curation",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model must not load")),
    )
    result = lifecycle.run_fast_curation_stage(
        config, root=config.root, state=_State(), run_id=9, cancellation=_Cancellation()
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_current_catalog_candidates"


def test_embedding_cache_persists_actual_query_and_passage_roles(monkeypatch) -> None:
    from neocortex.documents import curation_state

    captured: list[object] = []
    monkeypatch.setattr(
        curation_state,
        "upsert_embedding_cache_batch",
        lambda connection, records: captured.extend(records) or len(records),
    )
    model = SimpleNamespace(model_signature="model", vector_space="space", dimensions=2)
    cache = lifecycle._CatalogEmbeddingCache(
        object(), model=model, representation_version="fast-curation-representation/v1"
    )
    cache.put("a" * 64, (1.0, 0.0), metadata={"prototype_id": "prototype:report"})
    cache.put(
        "b" * 64,
        (0.0, 1.0),
        metadata={
            "representation_version": "fast-curation-representation/v1",
            "representation_fingerprint": "b" * 32,
        },
    )
    cache.flush()

    assert {record.key.role for record in captured} == {"query", "passage"}
