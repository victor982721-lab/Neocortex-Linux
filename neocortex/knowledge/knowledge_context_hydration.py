"""Bounded owner hydration consumed inside the existing Knowledge attempt.

No ranking or model is rerun. The service commits this detached projection only
after final owner fences and the post-query snapshot have been verified.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from neocortex.knowledge.knowledge_context_v2 import _candidates
from neocortex.knowledge.knowledge_evidence_lookup import EvidenceLookupError, lookup_owner_evidence

MAX_HYDRATION_REFERENCES = 20
MAX_HYDRATION_CHARACTERS = 32768
HYDRATION_DEADLINE_SECONDS = 10.0
_HYDRATABLE_OWNERS = frozenset(
    {"text", "pdf", "docx", "office", "audio", "video", "image"}
)


def _identity(evidence: dict[str, Any]) -> tuple[object, object, object]:
    return evidence.get("resource_id"), evidence.get("revision_id"), evidence.get("evidence_id")


def hydrate_context_result(
    result: Any, *, state_directory, scope: str,
    cancellation_check: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    data = result.to_dict()
    candidates, projected_bound = _candidates([{"scope": scope, "result": data}], 100)
    deadline = clock() + HYDRATION_DEADLINE_SECONDS
    hydrated: dict[tuple[object, object, object], dict[str, Any]] = {}
    statuses: dict[tuple[object, object, object], dict[str, Any]] = {}
    attempted = available = characters = 0
    bounded = projected_bound
    for source, citation, _snippet in candidates:
        if source["owner"] not in _HYDRATABLE_OWNERS:
            continue
        key = (source["resource_id"], source["revision_id"], citation["evidence_id"])
        if key in statuses:
            continue
        status: dict[str, Any] = {
            "status": "unavailable",
            "inspected_scope": "published_evidence_reference",
        }
        statuses[key] = status
        if cancellation_check is not None:
            cancellation_check()
        if attempted >= MAX_HYDRATION_REFERENCES or characters >= MAX_HYDRATION_CHARACTERS:
            status["reason"] = "hydration_reference_or_character_budget"
            bounded = True
            continue
        if clock() >= deadline:
            status["reason"] = "hydration_deadline"
            bounded = True
            continue
        if source.get("revision_state") != "current":
            status["reason"] = "historical_reference_not_hydrated"
            continue
        attempted += 1
        # IDs are presentation aliases; the lookup binds the full owner/revision.
        source_ref = dict(source, source_id="hydration-source")
        evidence_ref = dict(citation, source_id="hydration-source")
        try:
            value = lookup_owner_evidence(state_directory, source_ref, evidence_ref)
            hits = value.get("hits") or []
            if len(hits) != 1:
                raise EvidenceLookupError("published_evidence_absent_or_ambiguous")
            owner_hit = hits[0]
            if _identity(owner_hit["evidence"]) != key:
                raise EvidenceLookupError("evidence_identity_changed")
            snippet = owner_hit["evidence"].get("snippet") or ""
            if characters + len(snippet) > MAX_HYDRATION_CHARACTERS:
                status["reason"] = "hydration_character_budget"
                bounded = True
                continue
            if clock() >= deadline:
                status["reason"] = "hydration_deadline"
                bounded = True
                continue
            hydrated[key] = owner_hit
            characters += len(snippet)
            available += 1
            status["status"] = "owner_verified"
        except EvidenceLookupError as exc:
            status["reason"] = exc.code
        except (OSError, RuntimeError, ValueError):
            # The enclosing service's final fence barrier still invalidates the
            # whole attempt if a facade/lookup caught a concurrent read failure.
            status["reason"] = "owner_hydration_unavailable"
        if cancellation_check is not None:
            cancellation_check()

    for hit in data.get("hits", []):
        containers = [hit, *[signal for signal in hit.get("signals", [])
                            if isinstance(signal.get("evidence"), dict)]]
        for container in containers:
            evidence = container.get("evidence") or {}
            key = _identity(evidence)
            if key not in statuses:
                continue
            container["evidence_hydration"] = statuses[key]
            if key in hydrated:
                replacement = hydrated[key]
                container["evidence"] = replacement["evidence"]
                if replacement.get("evidence_extent") is not None:
                    container["evidence_extent"] = replacement["evidence_extent"]
    unavailable = len(statuses) - available
    data["context_hydration"] = {
        "status": "partial" if unavailable or bounded else "complete" if statuses else "not_requested",
        "inspected_scope": "referenced_published_owner_units",
        "attempted_references": attempted, "available_references": available,
        "returned_characters": characters, "reference_limit": MAX_HYDRATION_REFERENCES,
        "character_limit": MAX_HYDRATION_CHARACTERS,
        "deadline_seconds": HYDRATION_DEADLINE_SECONDS,
        "deadline_semantics": "cooperative_between_bounded_owner_reads",
        "bounded": bounded,
    }
    return data


def search_context_evidence(
    service, query, *, scope: str,
    cancellation_check: Callable[[], None] | None = None,
    read_metrics_sink: Callable[[dict[str, object]], None] | None = None,
    read_budget=None,
):
    """Use the same retrieval attempt and commit only coherent hydration."""
    if not callable(getattr(service, "_search_with_consumer", None)):
        result = service.search(query, cancellation_check=cancellation_check)
        data = result.to_dict()
        data["context_hydration"] = {"status": "unavailable",
            "inspected_scope": "retrieved_excerpts_only",
            "reason": "adapter_has_no_attempt_consumer"}
        return result, data

    def consume(result):
        return hydrate_context_result(
            result, state_directory=service.paths.semantic.parent, scope=scope,
            cancellation_check=cancellation_check,
        )

    result, consumed = service._search_with_consumer(
        query, consume, cancellation_check=cancellation_check,
        read_metrics_sink=read_metrics_sink,
        read_budget=read_budget,
    )
    if consumed is None:
        data = result.to_dict()
        data["hits"] = []
        data["complete"] = False
        data["context_hydration"] = {
            "status": "unavailable", "inspected_scope": "no_committed_attempt",
            "reason": "context_consumer_attempt_not_committed",
        }
    else:
        data = consumed
        data["snapshot"] = result.snapshot.to_dict()
        data["warnings"] = list(result.warnings)
        data["complete"] = result.complete
    return result, data
