"""Hydration budgets are explicit and do not turn partial reads into full owners."""
from __future__ import annotations

import copy

import pytest

from neocortex.knowledge import knowledge_context_hydration as hydration


class _Result:
    def __init__(self, count):
        self.data = {"hits": [], "complete": True, "rankings": [],
            "snapshot": {"snapshot_id": "fixture", "consistency": "stable", "owners": []}}
        for number in range(count):
            resource_id, revision_id = f"resource:{number}", f"revision:{number}"
            self.data["hits"].append({
                "rank": number + 1,
                "resource": {"owner": "text", "source_kind": "text", "resource_id": resource_id},
                "revision": {"revision_id": revision_id, "state": "current", "processing_signature": "fixture"},
                "evidence": {"resource_id": resource_id, "revision_id": revision_id,
                    "evidence_id": f"evidence:{number}", "snippet": "original fragment",
                    "method": "extracted", "identifiers": []},
            })

    def to_dict(self):
        return copy.deepcopy(self.data)


def _lookup(body, calls):
    def lookup(_state, source, citation):
        calls.append(citation["evidence_id"])
        return {"hits": [{"evidence": {
            "resource_id": source["resource_id"], "revision_id": source["revision_id"],
            "evidence_id": citation["evidence_id"], "snippet": body, "method": "extracted",
        }, "evidence_extent": {"bounded": False, "units": "characters",
                               "returned_range": {"start_char": 0, "end_char": len(body)}}}]}
    return lookup


@pytest.mark.parametrize(("count", "body", "expected"), [(21, "owner body", 20), (30, "a" * 4096, 8)])
def test_hydration_reference_and_character_bounds_are_not_hidden(tmp_path, monkeypatch, count, body, expected):
    calls = []
    monkeypatch.setattr(hydration, "lookup_owner_evidence", _lookup(body, calls))
    original = _Result(count)
    data = hydration.hydrate_context_result(original, state_directory=tmp_path,
                                            scope="personal", clock=lambda: 0.0)
    assert len(calls) == expected
    assert data["context_hydration"]["attempted_references"] == expected
    assert data["context_hydration"]["available_references"] == expected
    assert data["context_hydration"]["returned_characters"] <= 32768
    assert data["context_hydration"]["status"] == "partial"
    assert data["context_hydration"]["bounded"]
    assert data["hits"][expected]["evidence_hydration"]["status"] == "unavailable"
    assert data["hits"][expected]["evidence"]["snippet"] == "original fragment"
    assert all(hit["evidence"]["snippet"] == "original fragment" for hit in original.data["hits"])


def test_hydration_deadline_does_not_commit_a_late_read(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hydration, "lookup_owner_evidence", _lookup("late owner body", calls))
    times = iter((0.0, 1.0, 11.0, 11.0))
    data = hydration.hydrate_context_result(_Result(2), state_directory=tmp_path,
        scope="personal", clock=lambda: next(times))
    assert len(calls) == 1
    assert data["context_hydration"]["available_references"] == 0
    assert data["context_hydration"]["returned_characters"] == 0
    assert data["context_hydration"]["bounded"]
    assert all(hit["evidence_hydration"]["reason"] == "hydration_deadline" for hit in data["hits"])


def test_hydration_cancellation_is_not_degraded_to_a_missing_snippet(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hydration, "lookup_owner_evidence", _lookup("owner", calls))

    def cancel():
        raise KeyboardInterrupt("fixture cancelled")

    with pytest.raises(KeyboardInterrupt, match="fixture cancelled"):
        hydration.hydrate_context_result(_Result(1), state_directory=tmp_path,
            scope="personal", cancellation_check=cancel)
    assert calls == []

