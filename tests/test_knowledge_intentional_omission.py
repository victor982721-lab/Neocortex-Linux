"""Finalize distinguishes intentional image routing from unavailable work."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_search
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
)
from neocortex.knowledge.knowledge_planner import KnowledgeQuery, plan_knowledge_query
from neocortex.knowledge.knowledge_search import RankingExecution, execute_knowledge_search
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from tests.test_knowledge_search_completeness import (
    _install_complete_catalog,
    _ranking,
    _ranking_stub,
)


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")

_AMBIGUOUS_REASON = "ambiguous_query_requires_text_evidence"
_TEXTUAL_REASON = "textual_query_routed_away_from_clip"
_OWNERS = ("inventory", "framework", "catalog", "pdf", "docx", "office", "audio",
           "image", "semantic", "archive", "text", "video")
_OMISSION_JSON = """{
  "name": "semantic_image", "channel": "semantic", "available": true,
  "complete": true, "executed": false, "returned": 0, "rows_scanned": 0,
  "vectors_scanned": 0, "owner": "semantic",
  "reason": "ambiguous_query_requires_text_evidence"
}"""


def _omission(reason: str = _AMBIGUOUS_REASON, **changes: object) -> RankingExecution:
    fields = json.loads(_OMISSION_JSON)
    fields.update(reason=reason, **changes)
    return RankingExecution(**fields)


def _all_available_snapshot() -> KnowledgeSnapshot:
    snapshot = KnowledgeSnapshot.create(
        source_version="0.12.0", captured_at_utc="2026-09-06T18:00:00Z",
        captured_monotonic_ns=1,
        owners=tuple(OwnerSnapshot(owner, OwnerAvailability.AVAILABLE, 1, 1) for owner in _OWNERS),
    )
    assert len(snapshot.owners) == 12
    assert all(owner.state is OwnerAvailability.AVAILABLE for owner in snapshot.owners)
    return snapshot


def _execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: KnowledgeQuery,
    image_report: RankingExecution | None, *, text_failed: bool = False,
):
    _install_complete_catalog(monkeypatch)
    lexical = tuple(_ranking(f"fts_{owner}", "lexical", available=True, complete=True)
                    for owner in ("pdf", "docx", "office", "audio", "video", "archive", "text"))
    monkeypatch.setattr(knowledge_search, "_lexical_rankings", _ranking_stub(lexical))
    text = _ranking("semantic_text", "semantic", available=not text_failed, complete=not text_failed)
    reports = (text,) if image_report is None else (text, image_report)
    monkeypatch.setattr(knowledge_search, "_semantic_rankings", _ranking_stub(reports))

    def inventory(_paths, _snapshot, rankings, **_kwargs):
        return dict(rankings), RankingExecution(
            "inventory_dispositions", "inventory", True, True, True, 0, owner="inventory",
        )

    monkeypatch.setattr(knowledge_search, "_apply_inventory_dispositions", inventory)
    return execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "unopened-state"),
        plan_knowledge_query(query), _all_available_snapshot(),
    )


@pytest.mark.parametrize(
    ("query", "reason"),
    (
        (KnowledgeQuery("condición del equipo"), _AMBIGUOUS_REASON),
        (KnowledgeQuery("documento de presión"), _TEXTUAL_REASON),
        (KnowledgeQuery("texto en la imagen", source_kinds=("image",)), _TEXTUAL_REASON),
        (KnowledgeQuery("texto en la imagen", formats=("png",)), _TEXTUAL_REASON),
    ),
)
def test_actual_zero_activity_image_report_finalizes_complete_for_original_plan_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: KnowledgeQuery, reason: str,
) -> None:
    report = _omission(reason)

    result = _execute(tmp_path, monkeypatch, query, report)

    assert report.intentional_omission is True
    assert result.complete is True and result.truncated is False
    assert result.warnings == () and result.blocking_owners == ()
    payload = json.loads(result.to_json())
    assert payload["complete"] is True and "warnings" not in payload
    serialized = next(value for value in payload["rankings"] if value["name"] == "semantic_image")
    assert serialized["available"] is True and serialized["complete"] is True
    assert serialized["executed"] is False and serialized["reason"] == reason
    assert serialized["returned"] == serialized["rows_scanned"] == serialized["vectors_scanned"] == 0
    assert tuple(tmp_path.iterdir()) == ()


def test_failed_text_ranking_remains_incomplete_despite_valid_image_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute(tmp_path, monkeypatch, KnowledgeQuery("condición del equipo"), _omission(), text_failed=True)

    assert result.complete is False
    assert "ranking_unavailable:semantic_text" in result.warnings
    assert "ranking_unavailable:semantic_image" not in result.warnings


def test_missing_required_image_report_is_not_an_implicit_intentional_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute(tmp_path, monkeypatch, KnowledgeQuery("condición del equipo"), None)

    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings


@pytest.mark.parametrize("changes", (
    {"returned": 1}, {"rows_scanned": 1}, {"vectors_scanned": 1},
    {"result_window_full": True}, {"next_cursor": 0}, {"cutoff_score": 0.5},
    {"complete": False}, {"available": False}, {"owner": "image"}, {"channel": "lexical"},
))
def test_malformed_or_active_noops_still_fail_required_ranking_completeness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    report = _omission(**changes)

    result = _execute(tmp_path, monkeypatch, KnowledgeQuery("condición del equipo"), report)

    assert report.intentional_omission is False
    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings


@pytest.mark.parametrize("reason", ("routing_disabled", "image_retrieval_not_calibrated", _TEXTUAL_REASON))
def test_unknown_or_wrong_plan_reason_does_not_satisfy_required_image_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    result = _execute(tmp_path, monkeypatch, KnowledgeQuery("condición del equipo"), _omission(reason))

    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings


@pytest.mark.parametrize("query", (
    KnowledgeQuery("foto del equipo"),
    KnowledgeQuery("condición del equipo", source_kinds=("image",)),
    KnowledgeQuery("condición del equipo", formats=("png",)),
))
@pytest.mark.parametrize("reason", (_AMBIGUOUS_REASON, _TEXTUAL_REASON))
def test_explicit_visual_intent_does_not_accept_textual_or_ambiguous_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: KnowledgeQuery, reason: str,
) -> None:
    report = _omission(reason)
    assert report.intentional_omission is True

    result = _execute(tmp_path, monkeypatch, query, report)

    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings


@pytest.mark.parametrize("changes", ({"executed": 0}, {"available": 1}, {"complete": 1}))
def test_nonboolean_noop_flags_do_not_gain_intentional_omission_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    report = replace(_omission(), **changes)

    result = _execute(tmp_path, monkeypatch, KnowledgeQuery("condición del equipo"), report)

    assert report.intentional_omission is False
    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings
