"""Context v2 preserves exact upstream reasons for non-executed rankings."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2
from neocortex.knowledge.knowledge_planner import KnowledgeQuery
from neocortex.knowledge.knowledge_search import KnowledgeSearchResult, RankingExecution
from tests.test_knowledge_intentional_omission import _execute, _omission


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")

_TYPED_REASON = "image_retrieval_not_calibrated"
_QUERY = KnowledgeQuery("condición del equipo")


def _context(
    result: KnowledgeSearchResult,
    *,
    legacy_executed: str | None = None,
) -> dict[str, object]:
    serialized = result.to_dict()
    if legacy_executed is not None:
        rankings = serialized["rankings"]
        assert isinstance(rankings, list)
        image_ranking = next(
            item for item in rankings
            if isinstance(item, dict) and item.get("name") == "semantic_image"
        )
        if legacy_executed == "missing":
            image_ranking.pop("executed")
        elif legacy_executed == "true":
            image_ranking["executed"] = True
        else:  # pragma: no cover - protected by parametrization
            raise AssertionError(legacy_executed)
    return build_context_response_v2(
        [{"scope": "personal", "result": serialized}],
        query=result.plan.normalized_query,
        scope="personal",
        request_id="fixture-unexecuted-ranking",
        max_characters=8_000,
    )


def test_real_legal_image_noop_context_remains_empty_not_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute(tmp_path, monkeypatch, _QUERY, _omission())

    assert result.complete is True
    assert result.warnings == ()
    payload = _context(result)

    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["retrieval"] == {"status": "complete", "reasons": []}
    assert coverage["evidence"]["status"] == "no_evidence"
    assert payload["status"] == "empty"
    assert payload["exit_code"] == 3
    assert "hydration" not in coverage["scopes"][0]


def test_real_text_failure_does_not_attribute_legal_image_noop_to_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute(tmp_path, monkeypatch, _QUERY, _omission(), text_failed=True)

    assert result.complete is False
    assert "ranking_unavailable:semantic_text" in result.warnings
    assert "ranking_unavailable:semantic_image" not in result.warnings
    payload = _context(result)

    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["retrieval"] == {
        "status": "partial",
        "reasons": ["personal:semantic_text:fixture_modality_unavailable"],
    }
    assert payload["status"] == "partial"
    assert payload["exit_code"] == 4
    assert "semantic_image" not in str(coverage["retrieval"])


def test_real_exact_same_name_warning_preserves_typed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute(
        tmp_path,
        monkeypatch,
        _QUERY,
        _omission(_TYPED_REASON),
    )

    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings
    payload = _context(result)

    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["retrieval"] == {
        "status": "partial",
        "reasons": [f"personal:semantic_image:{_TYPED_REASON}"],
    }
    assert payload["status"] == "partial"
    assert payload["exit_code"] == 4


def test_real_exact_same_name_warning_without_reason_uses_existing_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute(tmp_path, monkeypatch, _QUERY, _omission(reason=None))

    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings
    payload = _context(result)

    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["retrieval"] == {
        "status": "partial",
        "reasons": ["personal:semantic_image:incomplete_or_unavailable"],
    }
    assert payload["status"] == "partial"
    assert payload["exit_code"] == 4


@pytest.mark.parametrize(
    "changes",
    (
        {"name": "other_ranking"},
        {"name": "other_ranking", "channel": "lexical", "owner": "other"},
    ),
)
def test_real_warning_for_other_name_keeps_unclassified_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, str],
) -> None:
    renamed = replace(_omission(_TYPED_REASON), **changes)
    result = _execute(tmp_path, monkeypatch, _QUERY, renamed)

    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings
    assert "ranking_unavailable:other_ranking" not in result.warnings
    payload = _context(result)

    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["retrieval"] == {
        "status": "partial",
        "reasons": ["personal:unclassified_result_incomplete"],
    }
    assert payload["status"] == "partial"
    assert payload["exit_code"] == 4


@pytest.mark.parametrize("legacy_executed", ("true", "missing"))
def test_legacy_executed_true_or_missing_preserves_historical_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_executed: str,
) -> None:
    result = _execute(tmp_path, monkeypatch, _QUERY, _omission(_TYPED_REASON))

    assert result.complete is False
    assert "ranking_unavailable:semantic_image" in result.warnings
    payload = _context(result, legacy_executed=legacy_executed)

    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["retrieval"] == {
        "status": "partial",
        "reasons": ["personal:unclassified_result_incomplete"],
    }
    assert payload["status"] == "partial"
    assert payload["exit_code"] == 4


def test_real_result_shape_uses_ranking_execution_serialization() -> None:
    ranking = _omission(_TYPED_REASON)
    assert isinstance(ranking, RankingExecution)
    payload = ranking.to_dict()
    assert payload == {
        "name": "semantic_image",
        "channel": "semantic",
        "executed": False,
        "available": True,
        "complete": True,
        "returned": 0,
        "rows_scanned": 0,
        "vectors_scanned": 0,
        "reason": _TYPED_REASON,
        "owner": "semantic",
        "row_count_semantics": "materialized_lower_bound",
    }
