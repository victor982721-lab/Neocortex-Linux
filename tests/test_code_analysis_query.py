"""Functional, adversarial and property-style contracts for Code queries."""

from __future__ import annotations

import json
import random
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from _04_Nucleo_Operativo.code_analysis_query import (
    CODE_ANALYSIS_QUERY_SCHEMA,
    CodeAnalysisQuery,
    query_code_analysis,
)

FIXTURE = Path(__file__).parent / "fixtures" / "code_analysis_query" / "public_surfaces_v1.json"


def _fixture() -> dict[str, object]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _surface(name: str) -> dict[str, object]:
    value = _fixture()[name]
    assert isinstance(value, dict)
    return value


def test_review_query_combines_all_dimensions_without_a_magic_score() -> None:
    query = CodeAnalysisQuery(
        surface="review",
        providers=("COSMIC-RAY-FOCAL-MUTATION",),
        categories=("mutation",),
        modules=("PKG.WORKER",),
        statuses=("passed",),
        work_packages=("WP-1",),
        limit=10,
    )

    first = query_code_analysis(_surface("review"), query)
    second = query_code_analysis(_surface("review"), query)

    assert first == second
    assert first["schema"] == CODE_ANALYSIS_QUERY_SCHEMA
    assert first["status"] == "ready"
    assert first["authority"] == "advisory"
    assert first["mutation_authority"] is False
    assert first["aggregate_score"] is None
    assert first["defect_probability"] is None
    assert first["filters"] == {
        "providers": ["cosmic-ray-focal-mutation"],
        "categories": ["mutation"],
        "modules": ["pkg.worker"],
        "statuses": ["passed"],
        "deltas": [],
        "work_packages": ["wp-1"],
    }
    assert first["counts"] == {
        "available": 4,
        "matched": 1,
        "returned": 1,
        "truncated": False,
    }
    matches = first["matches"]
    assert isinstance(matches, list)
    assert matches[0]["record_type"] == "work_package"
    assert matches[0]["facts"]["primary_module"] == "pkg.worker"
    serialized = json.dumps(first, sort_keys=True)
    assert '"aggregate_score": null' in serialized
    assert '"defect_probability": null' in serialized


def test_review_query_keeps_question_and_decision_readiness_dimensioned() -> None:
    payload = {
        "kind": "code-review",
        "schema": "neocortex.code-review/v11",
        "status": "ready",
        "reason": None,
        "recommendation_status": "abstained",
        "recommendation_reason": "no_evidence_ready_change_decision",
        "recommendations": [],
        "work_package_status": "abstained",
        "work_package_reason": "no_calibrated_characterization_candidate",
        "work_packages": [],
        "snapshot": {"processing_signature": "fixture"},
        "coverage": {"candidate_hotspots": 1},
        "digest": {"xxh3_128": "fixture"},
        "findings": [
            {
                "finding_id": "finding-1",
                "category": "long_function_hotspot",
                "actionability": "characterize_first",
                "construction": "unknown",
                "observation_confidence": "confirmed_static_evidence",
                "change_risk": "unknown",
                "recommended_change": False,
                "diagnostics": [{"code": "long_function"}],
                "epistemic_state": {
                    "observation_status": "confirmed",
                    "inference_status": "abstained",
                    "question_readiness": "ready",
                    "decision_readiness": "experiment_required",
                    "decision": None,
                    "authority": "advisory",
                    "mutation_authority": False,
                },
            }
        ],
    }

    question = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="review", statuses=("question:ready",)),
    )
    bare_ready = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="review", statuses=("ready",)),
    )
    decision = query_code_analysis(
        payload,
        CodeAnalysisQuery(surface="review", statuses=("decision:experiment_required",)),
    )

    assert question["counts"]["matched"] == 1
    assert decision["counts"]["matched"] == 1
    assert bare_ready["counts"]["matched"] == 0


def test_review_query_rejects_a_forged_v11_change_package() -> None:
    payload = {
        "kind": "code-review",
        "schema": "neocortex.code-review/v11",
        "status": "ready",
        "reason": None,
        "recommendation_status": "abstained",
        "recommendation_reason": "no_evidence_ready_change_decision",
        "recommendations": [],
        "work_package_status": "ready",
        "work_package_reason": None,
        "snapshot": {"processing_signature": "forged"},
        "coverage": {"candidate_hotspots": 1},
        "digest": {"xxh3_128": "forged"},
        "findings": [],
        "work_packages": [
            {
                "package_id": "forged",
                "package_kind": "hotspot_maintenance",
                "title": "Delete now",
                "objective": "delete_production_symbol",
                "requires_human_confirmation": False,
                "mutation_authority": True,
                "steps": [{"phase": "change", "requirement": "delete"}],
            }
        ],
    }

    with pytest.raises(ValueError, match="characterization-only policy"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


@pytest.mark.parametrize("schema", (None, "neocortex.code-review/unknown"))
def test_review_query_rejects_missing_unknown_or_future_schemas(schema: object) -> None:
    payload = deepcopy(_surface("review"))
    if schema is None:
        payload.pop("schema", None)
    else:
        payload["schema"] = schema

    with pytest.raises(ValueError, match="unsupported code-review schema"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


def test_review_query_accepts_and_indexes_source_linked_v13_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = json.loads(json.dumps(review_code_state(state_directory, limit=1).as_payload()))

    result = query_code_analysis(
        payload,
        CodeAnalysisQuery(
            surface="review",
            categories=("maintenance.structural_hotspot_requires_change",),
            statuses=("decision:experiment_required",),
        ),
    )

    assert result["status"] == "ready"
    assert result["counts"]["matched"] == 1
    assert result["matches"][0]["record_type"] == "analysis_question"
    assert result["matches"][0]["facts"]["mutation_authority"] is False


def test_review_query_rejects_forged_v13_evidence_linkage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = review_code_state(state_directory, limit=1).as_payload()
    epistemics = cast("dict[str, object]", payload["epistemics"])
    evaluations = cast("list[dict[str, object]]", epistemics["evaluations"])
    evidence = cast("list[dict[str, object]]", evaluations[0]["evidence"])
    evidence[0]["source_record_id"] = "forged"

    with pytest.raises(ValueError, match="not source-linked"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


def test_review_query_rejects_forged_v13_question_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_review as review_module
    from _04_Nucleo_Operativo.code_review import review_code_state
    from tests.test_code_review import _build_state, _status

    state_directory = tmp_path / "state"
    _build_state(state_directory)
    monkeypatch.setattr(
        review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )
    payload = json.loads(json.dumps(review_code_state(state_directory, limit=1).as_payload()))
    epistemics = cast("dict[str, object]", payload["epistemics"])
    specs = cast("list[dict[str, object]]", epistemics["specs"])
    actions = cast("list[dict[str, object]]", specs[0]["next_actions"])
    actions[0]["description"] = "Delete the production symbol now."

    with pytest.raises(ValueError, match="epistemic projection is not source-linked"):
        query_code_analysis(payload, CodeAnalysisQuery(surface="review"))


def test_status_query_supports_exact_or_descendant_module_matching() -> None:
    result = query_code_analysis(
        _surface("status"),
        CodeAnalysisQuery(surface="status", modules=("pkg.worker",), limit=100),
    )

    modules = {module for match in result["matches"] for module in match["dimensions"]["modules"]}
    assert "pkg.worker" in modules
    assert "pkg.worker.child" in modules
    assert all(module == "pkg.worker" or module.startswith("pkg.worker.") for module in modules)
    assert result["source"] == {
        "kind": "code-status",
        "schema": "neocortex.code-status/schema-v4",
        "digest": "fixture-processing-v1",
    }


def test_diff_query_exposes_provider_category_status_and_delta_filters() -> None:
    provider = query_code_analysis(
        _surface("diff"),
        CodeAnalysisQuery(
            surface="diff",
            providers=("cosmic-ray-focal-mutation",),
            statuses=("comparable",),
            deltas=("added",),
        ),
    )
    category = query_code_analysis(
        _surface("diff"),
        CodeAnalysisQuery(
            surface="diff",
            categories=("dependency_hygiene",),
            deltas=("increased",),
        ),
    )

    assert provider["counts"]["matched"] == 1
    assert provider["matches"][0]["record_type"] == "provider_delta"
    assert category["counts"]["matched"] == 1
    assert category["matches"][0]["record_type"] == "supply_chain_category_delta"


def test_diff_query_exposes_typed_relocation_with_exact_positions() -> None:
    payload = {
        "kind": "code-publication-diff",
        "schema": "neocortex.code-publication-diff/v10",
        "status": "ready",
        "providers": [
            {
                "provider_id": "mypy-trusted-project",
                "status": "ready",
                "common": 3,
                "added": 0,
                "resolved": 0,
                "relocated": 1,
                "gate": "passed",
                "relocation_examples": [
                    {
                        "baseline_finding_id": "finding-before",
                        "current_finding_id": "finding-after",
                        "path": "pkg/checks.py",
                        "category": "typing",
                        "code": "return-value",
                        "severity": "error",
                        "message": "Expected str",
                        "baseline_start_line": 10,
                        "baseline_start_column": 4,
                        "baseline_end_line": 10,
                        "baseline_end_column": 12,
                        "current_start_line": 12,
                        "current_start_column": 4,
                        "current_end_line": 12,
                        "current_end_column": 12,
                    }
                ],
            }
        ],
    }

    result = query_code_analysis(
        payload,
        CodeAnalysisQuery(
            surface="diff",
            providers=("mypy-trusted-project",),
            categories=("typing",),
            modules=("pkg.checks",),
            deltas=("relocated",),
        ),
    )

    assert result["counts"]["matched"] == 1
    match = result["matches"][0]
    assert match["record_type"] == "provider_finding_relocation"
    facts = match["facts"]
    assert isinstance(facts, dict)
    assert facts["message"] == "Expected str"
    assert facts["baseline_start_line"] == 10
    assert facts["current_start_line"] == 12


def test_limit_is_hard_and_reports_honest_truncation() -> None:
    result = query_code_analysis(
        _surface("status"),
        CodeAnalysisQuery(surface="status", limit=1),
    )

    counts = result["counts"]
    matches = result["matches"]
    assert isinstance(counts, dict)
    assert isinstance(matches, list)
    available = counts["available"]
    assert isinstance(available, int)
    assert available > 1
    assert counts["matched"] == available
    assert counts["returned"] == len(matches) == 1
    assert counts["truncated"] is True


def test_normalization_is_idempotent_and_filtering_is_monotonic() -> None:
    normalized = CodeAnalysisQuery(
        surface=cast("object", " REVIEW "),  # type: ignore[arg-type]
        providers=("MYPY-TRUSTED-PROJECT", "mypy-trusted-project"),
        categories=(" Finding ", "finding"),
    )
    again = CodeAnalysisQuery(
        surface=normalized.surface,
        providers=normalized.providers,
        categories=normalized.categories,
    )
    assert normalized == again
    assert normalized.providers == ("mypy-trusted-project",)
    assert normalized.categories == ("finding",)

    payload = _surface("status")
    unfiltered = query_code_analysis(payload, CodeAnalysisQuery(surface="status", limit=500))
    randomizer = random.Random(20260803)
    choices = (
        ("providers", "cosmic-ray-focal-mutation"),
        ("providers", "complexipy-cognitive"),
        ("categories", "engineering"),
        ("categories", "dependency_hygiene"),
        ("modules", "pkg.worker"),
        ("statuses", "ready"),
    )
    for _ in range(40):
        name, value = randomizer.choice(choices)
        arguments: dict[str, object] = {name: (value,)}
        query = CodeAnalysisQuery(surface="status", limit=500, **arguments)  # type: ignore[arg-type]
        first = query_code_analysis(payload, query)
        second = query_code_analysis(payload, query)
        assert first == second
        first_counts = first["counts"]
        unfiltered_counts = unfiltered["counts"]
        assert isinstance(first_counts, dict)
        assert isinstance(unfiltered_counts, dict)
        first_matched = first_counts["matched"]
        unfiltered_matched = unfiltered_counts["matched"]
        assert isinstance(first_matched, int)
        assert isinstance(unfiltered_matched, int)
        assert first_matched <= unfiltered_matched


@pytest.mark.parametrize(
    "constructor",
    (
        lambda: CodeAnalysisQuery(surface=cast("object", "unknown")),
        lambda: CodeAnalysisQuery(surface="status", providers=(" ",)),
        lambda: CodeAnalysisQuery(surface="status", providers=cast("object", ["ruff"])),
        lambda: CodeAnalysisQuery(surface="status", limit=0),
        lambda: CodeAnalysisQuery(surface="status", limit=501),
        lambda: CodeAnalysisQuery(surface="status", limit=cast("object", True)),
    ),
)
def test_malformed_queries_fail_closed(constructor: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        cast("object", constructor)()  # type: ignore[operator]


def test_wrong_surface_and_arbitrary_nested_fields_are_not_projected() -> None:
    payload = _surface("review")
    payload["private_extension"] = {
        "secret": "must-not-leak",
        "aggregate_score": 999,
        "defect_probability": 1.0,
    }
    result = query_code_analysis(payload, CodeAnalysisQuery(surface="review", limit=500))
    serialized = json.dumps(result, sort_keys=True)

    assert "must-not-leak" not in serialized
    assert "999" not in serialized
    assert result["aggregate_score"] is None
    assert result["defect_probability"] is None

    wrong = deepcopy(payload)
    wrong["kind"] = "code-status"
    with pytest.raises(ValueError, match="requires kind"):
        query_code_analysis(wrong, CodeAnalysisQuery(surface="review"))


def test_missing_status_state_abstains_without_creating_evidence() -> None:
    result = query_code_analysis(
        {
            "kind": "code-status",
            "schema_version": 4,
            "exists": False,
            "limitations": ["code_state_missing"],
        },
        CodeAnalysisQuery(surface="status"),
    )

    assert result["status"] == "abstained"
    assert result["matches"] == []
    assert "code_state_missing" in result["limitations"]


def test_diff_extractor_signature_order_and_exact_fixture_are_frozen() -> None:
    from hashlib import sha256
    from inspect import signature

    import _04_Nucleo_Operativo.code_analysis_query as query_module

    assert str(signature(query_module._extract_diff)) == (
        "(payload: 'Mapping[str, object]') -> 'list[dict[str, object]]'"
    )
    payload = _surface("diff")
    before = deepcopy(payload)

    records = query_module._extract_diff(payload)

    assert payload == before
    assert records == query_module._extract_diff(deepcopy(payload))
    assert [(record["record_type"], record["id"], record["source_path"]) for record in records] == [
        (
            "provider_delta",
            "providers[0]:cosmic-ray-focal-mutation",
            "providers[0]",
        ),
        (
            "architecture_module_delta",
            "architecture.modules[0]:pkg.worker",
            "architecture.modules[0]",
        ),
        (
            "hotspot_delta",
            "hotspots.added_examples[0]:pkg/new_module.py::new_module.hotspot",
            "hotspots.added_examples[0]",
        ),
        (
            "hotspot_delta",
            "hotspots.removed_examples[0]:pkg/worker.py::worker.old",
            "hotspots.removed_examples[0]",
        ),
        (
            "supply_chain_category_delta",
            "supply_chain.categories[0]:dependency_hygiene",
            "supply_chain.categories[0]",
        ),
        (
            "supply_chain_provider_delta",
            "supply_chain.providers[0]:deptry-project-dependencies",
            "supply_chain.providers[0]",
        ),
        (
            "coverage_delta",
            "test_coverage:pytest-coverage-trusted-deep",
            "test_coverage",
        ),
        (
            "engineering_dimension_delta",
            "engineering_analytics.dimensions[0]:mutation",
            "engineering_analytics.dimensions[0]",
        ),
    ]
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert sha256(encoded).hexdigest() == (
        "d2288e5e152abbeaaac93be78699c52cca07a6a229f22b80593f65ecf055f4e6"
    )


def test_diff_extractor_ignores_unsupported_and_malformed_sections() -> None:
    import _04_Nucleo_Operativo.code_analysis_query as query_module

    assert query_module._extract_diff({}) == []
    records = query_module._extract_diff(
        {
            "providers": "not-a-sequence",
            "architecture": ["not-a-mapping"],
            "hotspots": {"added_examples": [None, 3, {"private": "ignored"}]},
            "supply_chain": {"categories": "not-a-sequence"},
            "test_coverage": "not-a-mapping",
            "engineering_analytics": {"dimensions": [None, "invalid"]},
            "private_extension": {"secret": "must-not-leak"},
        }
    )
    assert [record["id"] for record in records] == ["hotspots.added_examples[2]:2"]
    assert records[0]["facts"] == {}
    assert "must-not-leak" not in json.dumps(records, sort_keys=True)
    assert "ignored" not in json.dumps(records, sort_keys=True)
