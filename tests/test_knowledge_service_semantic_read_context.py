"""Service attempts share real SQLite views across nested Semantic channels."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

import pytest

from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod, EvidenceRef, KnowledgeHit, KnowledgeSnapshot, LogicalWatermark,
    OwnerAvailability, OwnerSnapshot, PhysicalIdentityRef, RankingSignal, ResourceRef,
    RevisionRef, RevisionState, SnapshotConsistency,
)
from neocortex.knowledge.knowledge_planner import KnowledgeQuery
from neocortex.knowledge.knowledge_search_contracts import KnowledgeSearchResult
from neocortex.knowledge.knowledge_service import KnowledgeSearchService
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.semantic.semantic_schema import (
    SemanticStateError, semantic_database, semantic_read_context,
)


def _database(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE fixture(value INTEGER)")
        connection.execute("INSERT INTO fixture VALUES(1)")


def _read(path):
    with semantic_database(path, readonly=True, read_mode="snapshot_temp") as connection:
        return int(connection.execute("SELECT value FROM fixture").fetchone()[0])


def _snapshot(marker="same"):
    return KnowledgeSnapshot.create(source_version="fixture", captured_at_utc="2026-09-06T00:00:00Z",
        captured_monotonic_ns=1,
        owners=(OwnerSnapshot("semantic", OwnerAvailability.AVAILABLE, 1, 1,
            watermarks=(LogicalWatermark("fixture", marker),)),))


def _result(plan, snapshot, value, *, complete=True):
    resource = ResourceRef("resource:fixture", "text", "text", PhysicalIdentityRef("fixture", "one", 1))
    revision = RevisionRef(resource.resource_id, "revision:fixture", "fixture",
                           "fixture-v1", generation=None, state=RevisionState.CURRENT)
    evidence = EvidenceRef("evidence:fixture", resource.resource_id, revision.revision_id,
                           EvidenceMethod.EXTRACTED, snippet=f"fixture value {value}")
    hit = KnowledgeHit(1, resource, revision, evidence, (RankingSignal("fixture", "rank", 1.0, 1),),
                       1.0, ("read fixture",))
    return KnowledgeSearchResult(plan=plan, snapshot=snapshot, hits=(hit,), rankings=(),
        complete=complete, truncated=False, omitted_candidates=0,
        rows_scanned=2, vectors_scanned=0, elapsed_milliseconds=1)


def _service(tmp_path, executor, *, snapshots=None):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    path = state / "semantic.sqlite3"
    if not path.exists():
        _database(path)
    iterator = iter(snapshots) if snapshots is not None else None

    def collect(_paths, **_kwargs):
        return next(iterator) if iterator is not None else _snapshot()

    return KnowledgeSearchService(KnowledgeStatePaths.from_directory(state),
        snapshot_collector=collect, search_executor=executor), path


def test_real_two_channel_reads_use_one_preparation_and_metrics_do_not_change_result(tmp_path):
    contexts = []

    def execute(paths, plan, snapshot, **_kwargs):
        values = []
        for _channel in ("semantic_text", "semantic_image"):
            # This is the same nested facade boundary used by production.
            with semantic_read_context() as context:
                contexts.append(context)
                values.append(_read(paths.semantic))
        assert values == [1, 1]
        return _result(plan, snapshot, values[0])

    service, path = _service(tmp_path, execute)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    records = []
    result = service.search(KnowledgeQuery("fixture"), read_metrics_sink=records.append)
    plain = service.search(KnowledgeQuery("fixture"))
    assert result.hits == plain.hits
    assert result.complete == plain.complete
    assert contexts[0] is contexts[1]
    assert contexts[2] is contexts[3]
    assert contexts[0] is not contexts[2]
    assert records[0]["service_attempt"] == 1
    assert records[0]["outcome"] == "stable"
    counters = records[0]["semantic_read"]
    assert counters["prepared_views"] == 1
    assert counters["reused_views"] == 1
    assert counters["retained_views"] == 0
    assert counters["retained_temporary_bytes"] == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert list(path.parent.iterdir()) == [path]


def test_logical_snapshot_retry_overrides_ambient_context_and_gets_fresh_view(tmp_path):
    contexts = []

    def execute(paths, plan, snapshot, **_kwargs):
        with semantic_read_context() as context:
            contexts.append(context)
            value = _read(paths.semantic)
            assert _read(paths.semantic) == value
        return _result(plan, snapshot, value)

    service, _path = _service(tmp_path, execute,
        snapshots=[_snapshot("old"), _snapshot("new"), _snapshot("new"), _snapshot("new")])
    records = []
    with semantic_read_context() as outer:
        result = service.search(KnowledgeQuery("fixture"), read_metrics_sink=records.append)
        assert all(context is not outer for context in contexts)
        assert outer.metrics["prepared_views"] == 0
    assert result.complete
    assert "snapshot_retry_succeeded" in result.warnings
    assert contexts[0] is not contexts[1]
    assert [record["outcome"] for record in records] == ["snapshot_changed", "stable"]
    assert [record["semantic_read"]["prepared_views"] for record in records] == [1, 1]
    assert all(record["semantic_read"]["retained_views"] == 0 for record in records)


@pytest.mark.parametrize("swallowed", [False, True])
def test_fence_only_drift_discards_attempt_even_if_facade_swallows_failure(tmp_path, swallowed):
    attempts = 0
    contexts = []

    def execute(paths, plan, snapshot, **_kwargs):
        nonlocal attempts
        attempts += 1
        with semantic_read_context() as context:
            contexts.append(context)
            value = _read(paths.semantic)
        if attempts == 1:
            with closing(sqlite3.connect(paths.semantic)) as writer, writer:
                writer.execute("UPDATE fixture SET value=2")
        try:
            with semantic_read_context() as context:
                assert context is contexts[-1]
                _read(paths.semantic)
        except SemanticStateError:
            if not swallowed:
                raise
            return _result(plan, snapshot, value, complete=False)
        return _result(plan, snapshot, value)

    service, _path = _service(tmp_path, execute)
    records = []
    result = service.search(KnowledgeQuery("fixture"), read_metrics_sink=records.append)
    assert attempts == 2
    assert result.complete
    assert result.hits[0].evidence.snippet == "fixture value 2"
    assert contexts[0] is not contexts[1]
    assert [record["outcome"] for record in records] == ["owner_fence_changed", "stable"]
    assert all(record["semantic_read"]["prepared_views"] == 1 for record in records)
    assert all(record["semantic_read"]["retained_views"] == 0 for record in records)


def test_repeated_fence_drift_abstains_without_incoherent_hits(tmp_path):
    def execute(paths, plan, snapshot, **_kwargs):
        with semantic_read_context():
            value = _read(paths.semantic)
        with closing(sqlite3.connect(paths.semantic)) as writer, writer:
            writer.execute("UPDATE fixture SET value=value+1")
        # Simulate a facade that converts an inner failure to partial data.
        return _result(plan, snapshot, value, complete=False)

    service, _path = _service(tmp_path, execute)
    records = []
    result = service.search(KnowledgeQuery("fixture"), read_metrics_sink=records.append)
    assert not result.complete
    assert result.hits == ()
    assert result.snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED
    assert "semantic_owner_fence_changed" in result.warnings
    assert len(records) == 2
    assert all(record["outcome"] == "owner_fence_changed" for record in records)
    assert all(record["semantic_read"]["retained_views"] == 0 for record in records)


def test_cancellation_releases_attempt_view_without_retry(tmp_path):
    cancelled = False
    contexts = []

    def checkpoint():
        if cancelled:
            raise KeyboardInterrupt("fixture cancellation")

    def execute(paths, _plan, _snapshot, **_kwargs):
        nonlocal cancelled
        with semantic_read_context() as context:
            contexts.append(context)
            _read(paths.semantic)
            cancelled = True
            _read(paths.semantic)
        pytest.fail("cancellation must interrupt the cache hit")

    service, _path = _service(tmp_path, execute)
    with pytest.raises(KeyboardInterrupt, match="fixture cancellation"):
        service.search(KnowledgeQuery("fixture"), cancellation_check=checkpoint)
    assert len(contexts) == 1
    assert contexts[0].metrics["retained_views"] == 0
    assert contexts[0].metrics["retained_temporary_bytes"] == 0
