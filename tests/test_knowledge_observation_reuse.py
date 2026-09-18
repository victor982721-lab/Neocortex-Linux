"""A stable read attempt reuses verified observations, never a stale owner."""
from __future__ import annotations

from collections import Counter

from neocortex.knowledge import knowledge_snapshot as snapshot_module
from neocortex.knowledge.knowledge_planner import KnowledgeQuery
from neocortex.knowledge.knowledge_service import KnowledgeSearchService
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from tests.test_knowledge_snapshot import _published_fixture


def test_stable_public_query_observes_each_owner_once_per_attempt(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _published_fixture(state)
    counts = Counter()
    original = snapshot_module._logical_observation
    def observe(connection, spec):
        counts[spec.owner] += 1
        return original(connection, spec)
    monkeypatch.setattr(snapshot_module, "_logical_observation", observe)
    service = KnowledgeSearchService(KnowledgeStatePaths.from_directory(state))
    for _ in range(2):
        counts.clear()
        result = service.search(KnowledgeQuery("transformador"))
        assert result.snapshot.consistency.value == "stable"
        assert dict(counts) == {"inventory": 1, "catalog": 1, "semantic": 1, "code": 1}
