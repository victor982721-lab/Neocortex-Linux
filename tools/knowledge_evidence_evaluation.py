"""Offline evaluation of frozen independent labels against emitted evidence.

Labels are authored outside the rule implementation and never generated from
its predictions. Synthetic results describe only this dataset; they cannot
establish personal-corpus relevance, entailment, or action authority.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import platform
from typing import Any

from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2
from neocortex.semantic.semantic_query_evidence import (
    query_role_counterevidence, requested_evidence_checks,
)
from tools.knowledge_functional_v2_metrics import _role_errors, expected_necessary_checks
from tools.knowledge_scoped_observation_metrics import validate_scoped_checks

SCHEMA = "neocortex.independent-evidence-evaluation/v1"
STATES = {"support", "contradicted", "not_seen", "not_assessed"}


def load_frozen_cases(path: Path, manifest: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    frozen = json.loads(manifest.read_text(encoding="utf-8"))
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != frozen["sha256"]:
        raise ValueError("frozen evaluation dataset digest mismatch")
    cases = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    if len(cases) != frozen["count"] or not cases:
        raise ValueError("frozen evaluation case count mismatch")
    identifiers: set[str] = set()
    groups: dict[str, str] = {}
    for case in cases:
        if case["case_id"] in identifiers:
            raise ValueError("evaluation case identifiers must be unique")
        identifiers.add(case["case_id"])
        if case["expected_requirement"] not in STATES or case["split"] not in {"development", "heldout"}:
            raise ValueError("unrecognized independent label or split")
        start, end = case["emitted_start"], case["emitted_end"]
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(case["source_text"]):
            raise ValueError("invalid frozen emitted range")
        group = case["paraphrase_group"]
        if groups.setdefault(group, case["split"]) != case["split"]:
            raise ValueError("paraphrase group leaks across evaluation splits")
    return cases, frozen


def _project(case: dict[str, Any], max_characters: int) -> dict[str, Any]:
    start, end = case["emitted_start"], case["emitted_end"]
    fragment = case["source_text"][start:end]
    evidence = {
        "evidence_id": f"evidence:{case['case_id']}", "resource_id": f"resource:{case['case_id']}",
        "revision_id": "revision:synthetic-frozen", "method": "extracted", "snippet": fragment,
        "section_kind": "document", "section_id": case["locator"], "start_char": start, "end_char": end,
    }
    hit = {
        "rank": 1,
        "resource": {"resource_id": evidence["resource_id"], "source_kind": "text", "owner": "text", "current_path": f"/synthetic/{case['filename']}"},
        "revision": {"revision_id": evidence["revision_id"], "state": "current", "processing_signature": "independent-synthetic-v1"},
        "evidence": evidence,
        "evidence_hydration": {"status": "owner_verified", "inspected_scope": "published_evidence_reference"},
        "evidence_extent": {"units": "characters", "bounded": start != 0 or end != len(case["source_text"]), "source_total_chars": len(case["source_text"]), "returned_range": {"start_char": start, "end_char": end, "basis": "source_section"}},
        # Deliberately strong retrieval hints must not become answerability or
        # replace checks of the final excerpt.
        "signals": [{"source": "fts_text", "evidence": dict(evidence), "raw_score": 999999, "query_support": {"support": "full_terms", "missing_negation_terms": []}}],
    }
    return build_context_response_v2(
        [{"scope": "personal", "result": {"complete": True, "rankings": [], "hits": [hit], "snapshot": {"snapshot_id": "snapshot:synthetic-frozen", "consistency": "stable", "owners": []}}}],
        query=case["query"], scope="personal", request_id=case["case_id"],
        max_characters=max_characters, transport="json",
    )


def _state(checks: dict[str, Any], roles: list[dict[str, Any]]) -> str:
    if any(item.get("state") == "negated" for item in checks.get("scoped_observations", [])) or any(
        "literal_requested_event_occurrence_is_negated" in item.get("reasons", []) for item in roles
    ):
        return "contradicted"
    if checks["status"] == "not_assessed":
        return "not_assessed"
    if checks["status"] == "missing":
        return "not_seen"
    if checks["required_witnesses"] and checks["status"] == "necessary_checks_not_failed":
        return "support"
    return "not_assessed"


def evaluate_case(case: dict[str, Any], *, max_characters: int = 20_000) -> dict[str, Any]:
    payload = _project(case, max_characters)
    citations = payload.get("citations", [])
    if not citations:
        return {**{name: case[name] for name in ("case_id", "domain", "family", "split", "expected_subject", "expected_requirement")}, "observed_requirement": "not_assessed", "abstention_reason": "excerpt_not_emitted", "contract_errors": []}
    citation = citations[0]
    excerpt = citation["excerpt"]
    raw = requested_evidence_checks(case["query"], excerpt)
    roles = query_role_counterevidence(case["query"], excerpt)
    projected = citation["witness_checks"]
    errors: list[str] = []
    for name in ("status", "required_witnesses", "missing_necessary_witnesses", "counterevidence", "applicability", "scoped_observations", "retrieval_disposition"):
        if projected.get(name) != raw.get(name):
            errors.append(f"public_excerpt_check_mismatch:{name}")
    if citation.get("role_counterevidence") != roles:
        errors.append("public_role_check_mismatch")
    if citation.get("answer_sufficiency") != "not_assessed" or citation.get("emitted_extent", {}).get("document_completeness") != "not_asserted":
        errors.append("unsupported_sufficiency_or_completeness")
    if projected.get("recomputed_for") != "emitted_excerpt" or projected.get("evaluated_chars") != min(len(excerpt), 32768):
        errors.append("checks_not_bound_to_final_excerpt")
    original_fragment = case["source_text"][case["emitted_start"]:case["emitted_end"]]
    if excerpt != original_fragment:
        errors.append("emitted_excerpt_differs_from_frozen_evaluation_range")
    observations = raw.get("scoped_observations", [])
    if not isinstance(observations, list):
        raise ValueError("scoped evidence observations must be a list")
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError("each scoped evidence observation must be an object")
        start, end = observation.get("start_char"), observation.get("end_char")
        if start is not None and (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(excerpt)):
            errors.append("observation_outside_emitted_excerpt")
    independent_errors = []
    for witness in roles:
        independent_errors.extend(_role_errors(case["query"], excerpt, witness))
    required, missing = expected_necessary_checks(case["query"], excerpt)
    independent_errors.extend(validate_scoped_checks(case["query"], excerpt, raw, required, missing)[-1])
    observed = _state(raw, roles)
    return {
        **{name: case[name] for name in ("case_id", "domain", "family", "split", "expected_subject", "expected_requirement")},
        "observed_requirement": observed,
        "abstention_reason": raw.get("not_assessed_reason", "query_family_not_assessed") if observed == "not_assessed" else None,
        "contract_errors": errors, "independent_literal_validator_errors": independent_errors,
        "query": case["query"], "emitted_excerpt": excerpt, "filename": case["filename"], "locator": case["locator"],
        "required_witnesses": raw["required_witnesses"], "missing_necessary_witnesses": raw["missing_necessary_witnesses"],
        "scoped_observations": raw.get("scoped_observations", []), "applicability": raw.get("applicability"),
        "role_counterevidence": roles, "evidence_disposition": citation.get("evidence_disposition"),
        "answer_sufficiency": citation.get("answer_sufficiency"), "emitted_extent": citation.get("emitted_extent"),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for row in rows:
        expected, observed = row["expected_requirement"], row["observed_requirement"]
        counts["cases"] += 1
        counts["contract_errors"] += len(row["contract_errors"])
        counts["literal_validator_errors"] += len(row.get("independent_literal_validator_errors", []))
        if observed == "not_assessed":
            counts["abstentions"] += 1
            counts["abstained_positive" if expected == "support" else "abstained_other"] += 1
            continue  # Abstention is neither an accurate negative nor a match.
        if expected == "not_assessed":
            counts["assessed_unlabeled"] += 1
            counts["unsafe_assessment"] += int(observed in {"support", "contradicted"})
            continue
        counts["assessed_labeled"] += 1
        counts["label_matches"] += int(expected == observed)
        counts["tp" if expected == observed == "support" else "fn" if expected == "support" else "fp" if observed == "support" else "tn"] += 1
        counts["wrong_negative_polarity"] += int(expected != observed and expected != "support" and observed != "support")
    positive_predictions = counts["tp"] + counts["fp"]
    labeled = sum(row["expected_requirement"] != "not_assessed" for row in rows)
    return {**{key: counts[key] for key in ("cases", "assessed_labeled", "label_matches", "tp", "fp", "fn", "tn", "abstentions", "abstained_positive", "abstained_other", "assessed_unlabeled", "unsafe_assessment", "wrong_negative_polarity", "contract_errors", "literal_validator_errors")},
            "labeled_cases": labeled, "assessment_coverage": counts["assessed_labeled"] / labeled if labeled else None,
            "support_precision_assessed": counts["tp"] / positive_predictions if positive_predictions else None,
            "support_precision_denominator": positive_predictions}


def evaluate(cases: list[dict[str, Any]], manifest: dict[str, Any], *, split: str = "all") -> dict[str, Any]:
    rows = [evaluate_case(case) for case in cases if split == "all" or case["split"] == split]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for dimension in ("domain", "family", "expected_subject", "split"):
            groups[f"{dimension}:{row[dimension]}"].append(row)
    summary = summarize(rows)
    return {
        "schema": SCHEMA, "python": platform.python_version(), "dataset": manifest, "split": split,
        "summary": summary, "by_category": {name: summarize(group) for name, group in sorted(groups.items())},
        "safety_gate": "pass" if not (summary["fp"] or summary["unsafe_assessment"] or summary["contract_errors"] or summary["literal_validator_errors"]) else "fail",
        "acceptance_policy": "zero false support, unsupported contradiction of an unknown event, or sufficiency claims in this synthetic matrix; abstention is reported separately, never counted as a correct answer",
        "limits": ["independent synthetic labels do not establish personal corpus representativeness", "support means necessary markers, not answer entailment or permission", "confusion matrix assesses the labeled query event; individual internal markers have no independent labels", "semantic-label matching and independent literal-validator errors are reported separately"],
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=("all", "development", "heldout"), default="all")
    args = parser.parse_args(argv)
    cases, manifest = load_frozen_cases(args.dataset, args.manifest)
    report = evaluate(cases, manifest, split=args.split)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"summary": report["summary"], "safety_gate": report["safety_gate"]}, ensure_ascii=False))
    return int(report["safety_gate"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
