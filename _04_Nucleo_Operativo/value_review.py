"""Conservative, explainable and read-only file value preview.

This module ranks review candidates.  It does not expose or call any mutation
primitive.  Missing evidence produces ``unknown`` rather than an invented zero
for coverage, citations, use, or uniqueness.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable

from .value_review_contracts import (
    ValueDimension,
    ValueDimensionAssessment,
    ValueDimensionName,
    ValueEvidence,
    ValueEvidenceFact,
    ValueEvidenceStrength,
    ValueFileObservation,
    ValueOwnerHealth,
    ValueProvenance,
    ValueReviewAvailability,
    ValueReviewItem,
    ValueReviewPaths,
    ValueReviewQuery,
    ValueReviewReport,
    ValueReviewState,
)


MAX_VALUE_REVIEW_OBSERVATIONS = 25_000
_DAY_NS = 86_400_000_000_000
_LOW_VALUE_AFTER_DAYS = 30
_ARCHIVE_AFTER_DAYS = 180

_PROTECTIVE_OWNER_HEALTH = frozenset(
    {
        ValueOwnerHealth.PARTIAL,
        ValueOwnerHealth.INCOMPATIBLE,
        ValueOwnerHealth.CORRUPT,
        ValueOwnerHealth.ENCRYPTED,
        ValueOwnerHealth.FAILED,
    }
)
_PROJECT_COMPONENTS = frozenset(
    {
        ".git",
        "project",
        "projects",
        "repo",
        "repos",
        "repositories",
        "repository",
        "src",
        "source",
        "test",
        "tests",
        "workspace",
        "workspaces",
    }
)
_PROJECT_FILENAMES = frozenset(
    {
        ".gitignore",
        "cargo.toml",
        "go.mod",
        "package.json",
        "pom.xml",
        "pyproject.toml",
    }
)
_PROJECT_EXTENSIONS = frozenset(
    {
        ".c",
        ".cpp",
        ".cs",
        ".fs",
        ".fsx",
        ".go",
        ".h",
        ".hpp",
        ".ipynb",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".php",
        ".ps1",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".swift",
        ".ts",
        ".tsx",
    }
)
_DISPOSABLE_COMPONENTS = frozenset(
    {
        ".cache",
        "build",
        "cache",
        "dist",
        "generated",
        "log",
        "logs",
        "out",
        "temp",
        "temporary",
        "tmp",
    }
)
_DISPOSABLE_EXTENSIONS = frozenset({".bak", ".cache", ".log", ".temp", ".tmp"})
_ARCHIVE_COMPONENTS = frozenset(
    {"archive", "archived", "archives", "backup", "backups", "export", "exports", "legacy"}
)
_HEALTHY_SOURCE_STATUSES = frozenset({"complete", "done"})


def preview_value_review(
    paths: ValueReviewPaths,
    query: ValueReviewQuery,
) -> ValueReviewReport:
    """Read published owners with SQLite ``query_only`` and rank the result."""

    query.validate()
    # Lazy import keeps the pure evaluator useful to federation callers and
    # prevents an import from touching optional owner runtimes.
    from .value_review_repository import load_value_review_observations

    loaded = load_value_review_observations(paths, query)
    if loaded.availability is ValueReviewAvailability.UNAVAILABLE:
        return ValueReviewReport(
            availability=loaded.availability,
            complete=False,
            reason=loaded.reason,
            candidate_count=0,
            matched_count=0,
            returned_count=0,
            truncated=False,
            items=(),
            provenance=loaded.provenance,
            uncertainties=loaded.uncertainties,
        )
    return rank_value_observations(
        loaded.observations,
        query,
        availability=loaded.availability,
        complete=loaded.complete,
        reason=loaded.reason,
        provenance=loaded.provenance,
        uncertainties=loaded.uncertainties,
    )


def rank_value_observations(
    observations: Iterable[ValueFileObservation],
    query: ValueReviewQuery,
    *,
    availability: ValueReviewAvailability = ValueReviewAvailability.READY,
    complete: bool = True,
    reason: str | None = None,
    provenance: tuple[ValueProvenance, ...] = (),
    uncertainties: tuple[str, ...] = (),
) -> ValueReviewReport:
    """Rank already-normalized observations for CLI or federated consumers."""

    query.validate()
    materialized = tuple(observations)
    if len(materialized) > MAX_VALUE_REVIEW_OBSERVATIONS:
        return _unavailable_report(
            "scope_too_broad",
            provenance=provenance,
            uncertainties=(*uncertainties, "observation_limit_exceeded"),
        )
    invalid: str | None = None
    for value in materialized:
        invalid = _observation_error(value)
        if invalid is not None:
            break
    if invalid is not None:
        return _unavailable_report(
            "invalid_observation",
            provenance=provenance,
            uncertainties=(*uncertainties, invalid),
        )
    resource_ids = [value.resource_id for value in materialized]
    if len(set(resource_ids)) != len(resource_ids):
        return _unavailable_report(
            "ambiguous_resource_identity",
            provenance=provenance,
            uncertainties=(*uncertainties, "duplicate_resource_id"),
        )

    candidates = tuple(value for value in materialized if _matches_prefilters(value, query))
    evaluated = tuple(_evaluate(value, query) for value in candidates)
    if query.states:
        wanted = set(query.states)
        matched = tuple(value for value in evaluated if value.state in wanted)
    else:
        matched = evaluated
    ordered = tuple(sorted(matched, key=_item_sort_key))
    returned = ordered[: query.limit]
    report_provenance = _unique_provenance(
        (*provenance, *(item for value in candidates for item in value.provenance))
    )
    return ValueReviewReport(
        availability=availability,
        complete=complete,
        reason=reason,
        candidate_count=len(candidates),
        matched_count=len(ordered),
        returned_count=len(returned),
        truncated=len(ordered) > query.limit,
        items=returned,
        provenance=report_provenance,
        uncertainties=_unique_strings(uncertainties),
    )


def _unavailable_report(
    reason: str,
    *,
    provenance: tuple[ValueProvenance, ...],
    uncertainties: tuple[str, ...],
) -> ValueReviewReport:
    return ValueReviewReport(
        availability=ValueReviewAvailability.UNAVAILABLE,
        complete=False,
        reason=reason,
        candidate_count=0,
        matched_count=0,
        returned_count=0,
        truncated=False,
        items=(),
        provenance=_unique_provenance(provenance),
        uncertainties=_unique_strings(uncertainties),
    )


def _observation_error(value: ValueFileObservation) -> str | None:
    if not value.resource_id or not value.path:
        return "resource_identity_or_path_missing"
    if (
        isinstance(value.size_bytes, bool)
        or not isinstance(value.size_bytes, int)
        or value.size_bytes < 0
    ):
        return "invalid_size"
    if not isinstance(value.mtime_ns, int) or isinstance(value.mtime_ns, bool):
        return "invalid_mtime"
    if not isinstance(value.birthtime_ns, int) or isinstance(value.birthtime_ns, bool):
        return "invalid_birthtime"
    for count, label in (
        (value.text_duplicate_count, "text_duplicate_count"),
        (value.citation_count, "citation_count"),
        (value.usage_count, "usage_count"),
    ):
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            return f"invalid_{label}"
    if value.coverage_ratio is not None and (
        isinstance(value.coverage_ratio, bool)
        or not isinstance(value.coverage_ratio, (int, float))
        or not math.isfinite(value.coverage_ratio)
        or not 0.0 <= value.coverage_ratio <= 1.0
    ):
        return "invalid_coverage_ratio"
    evidence_ids = [item.evidence_id for item in value.evidence]
    if any(not item for item in evidence_ids) or len(set(evidence_ids)) != len(evidence_ids):
        return "invalid_evidence_ids"
    return None


def _matches_prefilters(value: ValueFileObservation, query: ValueReviewQuery) -> bool:
    if query.scope is not None and not _path_within(value.path, query.scope):
        return False
    if query.minimum_size_bytes is not None and value.size_bytes < query.minimum_size_bytes:
        return False
    if query.maximum_size_bytes is not None and value.size_bytes > query.maximum_size_bytes:
        return False
    if query.extensions:
        suffix = _path_suffix(value.path)
        wanted = {_normalized_extension(item) for item in query.extensions}
        if suffix not in wanted:
            return False
    if query.source_kinds:
        if value.source_kind is None:
            return False
        wanted_sources = {item.strip().casefold() for item in query.source_kinds}
        if value.source_kind.casefold() not in wanted_sources:
            return False
    return True


def _evaluate(value: ValueFileObservation, query: ValueReviewQuery) -> ValueReviewItem:
    evidence = list(value.evidence)
    path_kind, path_evidence = _path_kind(value, query)
    evidence.append(path_evidence)
    evidence = list(_unique_evidence(tuple(evidence)))

    exact_valid, exact_evidence_ids = _strong_exact_duplicate(value)
    age_days, age_uncertainties = _age_days(value, query)
    history_protection = _has_positive_history(value)
    project_protected = path_kind == "project"
    owner_protected = value.owner_health in _PROTECTIVE_OWNER_HEALTH
    text_unique = value.text_duplicate_count == 1 and _valid_text_fingerprint(
        value.text_fingerprint
    )
    reliable_extraction = (
        value.owner_health is ValueOwnerHealth.HEALTHY
        and (value.source_status or "").casefold() in _HEALTHY_SOURCE_STATUSES
        and (value.catalog_status or "").casefold() == "classified"
    )

    reasons: list[str] = []
    item_uncertainties = list(value.uncertainties)
    if project_protected:
        state = ValueReviewState.KEEP
        priority = 0
        reasons.append("project_path_protected")
    elif owner_protected:
        state = ValueReviewState.UNKNOWN
        priority = 0
        reasons.append(f"owner_health_protected:{value.owner_health.value}")
    elif history_protection:
        state = ValueReviewState.KEEP
        priority = 0
        reasons.append("published_positive_use_or_citation_protects_file")
    elif exact_valid and value.exact_duplicate_role == "keep":
        state = ValueReviewState.KEEP
        priority = 0
        reasons.append("exact_duplicate_plan_selected_keeper")
    elif exact_valid and value.exact_duplicate_role == "redundant":
        state = ValueReviewState.EXACT_DUPLICATE_CANDIDATE
        priority = 100
        reasons.append("published_plan_proves_byte_exact_redundancy")
    elif text_unique:
        state = ValueReviewState.KEEP
        priority = 0
        reasons.append("published_extracted_text_is_unique")
    elif (
        reliable_extraction
        and _has_text_redundancy(value)
        and path_kind == "archive"
        and age_days is not None
        and age_days >= _ARCHIVE_AFTER_DAYS
    ):
        state = ValueReviewState.ARCHIVE_CANDIDATE
        priority = 60
        reasons.append("old_repeated_extracted_text_in_archive_path")
    elif (
        reliable_extraction
        and _has_text_redundancy(value)
        and path_kind == "disposable"
        and age_days is not None
        and age_days >= _LOW_VALUE_AFTER_DAYS
    ):
        state = ValueReviewState.REVIEW_LOW_VALUE
        priority = 80
        reasons.append("old_repeated_extracted_text_in_disposable_path")
    else:
        state = ValueReviewState.UNKNOWN
        priority = 0
        reasons.append("insufficient_positive_evidence_for_value_recommendation")

    dimensions = (
        _exact_dimension(value, exact_valid, exact_evidence_ids),
        _quality_dimension(value),
        _uniqueness_dimension(value),
        _age_dimension(age_days, age_uncertainties, value),
        _size_dimension(value),
        _path_dimension(path_kind, path_evidence),
        _history_dimension(
            ValueDimensionName.COVERAGE,
            value.coverage_ratio,
            value.coverage_evidence_ids,
            "coverage_history_unavailable",
            positive_is_protective=False,
            evidence=value.evidence,
        ),
        _history_dimension(
            ValueDimensionName.CITATIONS,
            value.citation_count,
            value.citation_evidence_ids,
            "citation_history_unavailable",
            positive_is_protective=True,
            evidence=value.evidence,
        ),
        _history_dimension(
            ValueDimensionName.USAGE,
            value.usage_count,
            value.usage_evidence_ids,
            "usage_history_unavailable",
            positive_is_protective=True,
            evidence=value.evidence,
        ),
    )
    for dimension in dimensions:
        item_uncertainties.extend(dimension.uncertainties)
    if state is ValueReviewState.UNKNOWN and value.owner_health is ValueOwnerHealth.UNKNOWN:
        item_uncertainties.append("owner_health_unknown")
    if not exact_valid and value.exact_duplicate_role is not None:
        item_uncertainties.append("exact_duplicate_evidence_invalid")
    return ValueReviewItem(
        resource_id=value.resource_id,
        path=value.path,
        state=state,
        review_priority=priority,
        size_bytes=value.size_bytes,
        source_kind=value.source_kind,
        dimensions=dimensions,
        provenance=_unique_provenance(value.provenance),
        evidence=_unique_evidence(tuple(evidence)),
        reasons=_unique_strings(tuple(reasons)),
        uncertainties=_unique_strings(tuple(item_uncertainties)),
    )


def _strong_exact_duplicate(value: ValueFileObservation) -> tuple[bool, tuple[str, ...]]:
    if (
        value.exact_duplicate_role not in {"keep", "redundant"}
        or not _valid_full_hash(value.exact_duplicate_hash)
        or not value.exact_duplicate_group_id
        or not value.exact_duplicate_keeper_id
        or (
            value.exact_duplicate_role == "keep"
            and value.exact_duplicate_keeper_id != value.resource_id
        )
        or (
            value.exact_duplicate_role == "redundant"
            and value.exact_duplicate_keeper_id == value.resource_id
        )
    ):
        return False, ()
    matches: list[str] = []
    for item in value.evidence:
        if (
            item.owner != "inventory"
            or item.kind != "exact_duplicate_plan"
            or item.strength is not ValueEvidenceStrength.STRONG
            or not item.publication_id
        ):
            continue
        facts = {fact.name: fact.value for fact in item.facts}
        if (
            len(facts) == len(item.facts)
            and facts.get("full_fingerprint") == value.exact_duplicate_hash
            and facts.get("group_id") == value.exact_duplicate_group_id
            and facts.get("role") == value.exact_duplicate_role
            and facts.get("keeper_resource_id") == value.exact_duplicate_keeper_id
        ):
            matches.append(item.evidence_id)
    return bool(matches), tuple(sorted(matches))


def _valid_full_hash(value: str | None) -> bool:
    return (
        value is not None
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_text_fingerprint(value: str | None) -> bool:
    return (
        value is not None
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _has_text_redundancy(value: ValueFileObservation) -> bool:
    return (
        value.text_duplicate_count is not None
        and value.text_duplicate_count >= 2
        and _valid_text_fingerprint(value.text_fingerprint)
    )


def _has_positive_history(value: ValueFileObservation) -> bool:
    known_ids = {item.evidence_id for item in value.evidence}
    cited = (
        value.citation_count is not None
        and value.citation_count > 0
        and bool(value.citation_evidence_ids)
        and set(value.citation_evidence_ids).issubset(known_ids)
    )
    used = (
        value.usage_count is not None
        and value.usage_count > 0
        and bool(value.usage_evidence_ids)
        and set(value.usage_evidence_ids).issubset(known_ids)
    )
    return cited or used


def _exact_dimension(
    value: ValueFileObservation,
    valid: bool,
    evidence_ids: tuple[str, ...],
) -> ValueDimension:
    if valid:
        assessment = (
            ValueDimensionAssessment.PROTECT
            if value.exact_duplicate_role == "keep"
            else ValueDimensionAssessment.SUPPORTS_EXACT_DUPLICATE_REVIEW
        )
        return ValueDimension(
            ValueDimensionName.EXACT_DUPLICATION,
            assessment,
            value.exact_duplicate_role,
            evidence_ids,
            ("published_exact_duplicate_plan",),
        )
    uncertainty = (
        "exact_duplicate_evidence_invalid"
        if value.exact_duplicate_role is not None
        else "exact_duplicate_evidence_unavailable"
    )
    return ValueDimension(
        ValueDimensionName.EXACT_DUPLICATION,
        ValueDimensionAssessment.UNKNOWN,
        None,
        uncertainties=(uncertainty,),
    )


def _quality_dimension(value: ValueFileObservation) -> ValueDimension:
    evidence_ids = tuple(
        item.evidence_id
        for item in value.evidence
        if item.kind in {"published_catalog_record", "owner_schema_health", "extraction_health"}
    )
    if value.owner_health is ValueOwnerHealth.HEALTHY:
        return ValueDimension(
            ValueDimensionName.EXTRACTION_QUALITY,
            ValueDimensionAssessment.NEUTRAL,
            "extractable",
            evidence_ids,
            ("published_owner_and_extraction_are_healthy",),
        )
    if value.owner_health in _PROTECTIVE_OWNER_HEALTH:
        return ValueDimension(
            ValueDimensionName.EXTRACTION_QUALITY,
            ValueDimensionAssessment.PROTECT,
            value.owner_health.value,
            evidence_ids,
            ("unreliable_extraction_must_not_reduce_value",),
        )
    return ValueDimension(
        ValueDimensionName.EXTRACTION_QUALITY,
        ValueDimensionAssessment.UNKNOWN,
        None,
        evidence_ids,
        uncertainties=("extraction_health_unavailable",),
    )


def _uniqueness_dimension(value: ValueFileObservation) -> ValueDimension:
    catalog_ids = tuple(
        item.evidence_id
        for item in value.evidence
        if item.kind in {"published_catalog_record", "published_text_fingerprint_count"}
    )
    if value.text_duplicate_count == 1 and _valid_text_fingerprint(value.text_fingerprint):
        return ValueDimension(
            ValueDimensionName.UNIQUENESS,
            ValueDimensionAssessment.PROTECT,
            "unique_extracted_text",
            catalog_ids,
            ("unique_published_text_fingerprint_protects_file",),
        )
    if _has_text_redundancy(value):
        return ValueDimension(
            ValueDimensionName.UNIQUENESS,
            ValueDimensionAssessment.NEUTRAL,
            value.text_duplicate_count,
            catalog_ids,
            ("repeated_text_is_not_byte_identity",),
        )
    return ValueDimension(
        ValueDimensionName.UNIQUENESS,
        ValueDimensionAssessment.UNKNOWN,
        None,
        catalog_ids,
        uncertainties=("uniqueness_evidence_unavailable",),
    )


def _age_days(
    value: ValueFileObservation,
    query: ValueReviewQuery,
) -> tuple[int | None, tuple[str, ...]]:
    if query.reference_time_ns is None:
        return None, ("age_reference_time_unavailable",)
    if value.mtime_ns < 0:
        return None, ("mtime_unavailable",)
    if value.mtime_ns > query.reference_time_ns:
        return None, ("mtime_after_reference_time",)
    return (query.reference_time_ns - value.mtime_ns) // _DAY_NS, ()


def _age_dimension(
    days: int | None,
    uncertainties: tuple[str, ...],
    value: ValueFileObservation,
) -> ValueDimension:
    inventory_ids = tuple(
        item.evidence_id for item in value.evidence if item.kind == "published_inventory_record"
    )
    if days is None:
        return ValueDimension(
            ValueDimensionName.AGE,
            ValueDimensionAssessment.UNKNOWN,
            None,
            inventory_ids,
            uncertainties=uncertainties,
        )
    assessment = (
        ValueDimensionAssessment.SUPPORTS_ARCHIVE_REVIEW
        if days >= _ARCHIVE_AFTER_DAYS
        else ValueDimensionAssessment.NEUTRAL
    )
    return ValueDimension(
        ValueDimensionName.AGE,
        assessment,
        days,
        inventory_ids,
        ("age_is_context_only_not_authorization",),
    )


def _size_dimension(value: ValueFileObservation) -> ValueDimension:
    inventory_ids = tuple(
        item.evidence_id for item in value.evidence if item.kind == "published_inventory_record"
    )
    assessment = (
        ValueDimensionAssessment.SUPPORTS_LOW_VALUE_REVIEW
        if value.size_bytes == 0
        else ValueDimensionAssessment.NEUTRAL
    )
    reason = "empty_payload" if value.size_bytes == 0 else "size_is_context_only"
    return ValueDimension(
        ValueDimensionName.SIZE,
        assessment,
        value.size_bytes,
        inventory_ids,
        (reason,),
    )


def _path_dimension(path_kind: str, evidence: ValueEvidence) -> ValueDimension:
    assessments = {
        "project": ValueDimensionAssessment.PROTECT,
        "disposable": ValueDimensionAssessment.SUPPORTS_LOW_VALUE_REVIEW,
        "archive": ValueDimensionAssessment.SUPPORTS_ARCHIVE_REVIEW,
        "ordinary": ValueDimensionAssessment.NEUTRAL,
    }
    return ValueDimension(
        ValueDimensionName.PATH_TYPE,
        assessments[path_kind],
        path_kind,
        (evidence.evidence_id,),
        ("path_type_is_weak_context_not_authorization",),
    )


def _history_dimension(
    name: ValueDimensionName,
    value: int | float | None,
    evidence_ids: tuple[str, ...],
    unavailable: str,
    *,
    positive_is_protective: bool,
    evidence: tuple[ValueEvidence, ...],
) -> ValueDimension:
    known = {item.evidence_id for item in evidence}
    if value is None or not evidence_ids or not set(evidence_ids).issubset(known):
        missing = unavailable if value is None else f"{name.value}_evidence_missing"
        return ValueDimension(
            name,
            ValueDimensionAssessment.UNKNOWN,
            None,
            uncertainties=(missing,),
        )
    assessment = (
        ValueDimensionAssessment.PROTECT
        if positive_is_protective and value > 0
        else ValueDimensionAssessment.NEUTRAL
    )
    return ValueDimension(
        name,
        assessment,
        value,
        tuple(sorted(evidence_ids)),
        ("published_history_observed",),
    )


def _path_kind(
    value: ValueFileObservation,
    query: ValueReviewQuery,
) -> tuple[str, ValueEvidence]:
    components = tuple(item.casefold() for item in re.split(r"[\\/]+", value.path) if item)
    filename = components[-1] if components else ""
    protected_root = next(
        (root for root in query.protected_roots if _path_within(value.path, root)),
        None,
    )
    if (
        protected_root is not None
        or bool(value.primary_project and value.primary_project.strip())
        or bool(set(components) & _PROJECT_COMPONENTS)
        or filename in _PROJECT_FILENAMES
        or _path_suffix(value.path) in _PROJECT_EXTENSIONS
    ):
        kind = "project"
        rule = "protected_root_or_project_evidence"
    elif bool(set(components) & _ARCHIVE_COMPONENTS):
        kind = "archive"
        rule = "archive_path_component"
    elif (
        bool(set(components) & _DISPOSABLE_COMPONENTS)
        or _path_suffix(value.path) in _DISPOSABLE_EXTENSIONS
    ):
        kind = "disposable"
        rule = "disposable_path_or_extension"
    else:
        kind = "ordinary"
        rule = "no_special_path_signal"
    digest = hashlib.sha256(value.path.encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
    evidence = ValueEvidence(
        evidence_id=f"value-rule:path:{digest}",
        owner="value_review",
        kind="path_type_rule",
        strength=ValueEvidenceStrength.WEAK,
        record_id=value.resource_id,
        facts=(
            ValueEvidenceFact("path_kind", kind),
            ValueEvidenceFact("rule", rule),
        ),
    )
    return kind, evidence


def _normalized_extension(value: str) -> str:
    normalized = value.strip().casefold()
    return normalized if normalized.startswith(".") else f".{normalized}"


def _path_suffix(path: str) -> str:
    filename = re.split(r"[\\/]+", path)[-1]
    dot = filename.rfind(".")
    return "" if dot <= 0 else filename[dot:].casefold()


def _path_within(path: str, root: str) -> bool:
    path_key, path_windows = _path_key(path)
    root_key, root_windows = _path_key(root)
    if path_windows != root_windows:
        return False
    if path_key == root_key:
        return True
    return path_key.startswith(root_key.rstrip("/") + "/")


def _path_key(value: str) -> tuple[str, bool]:
    windows = bool(re.match(r"^[A-Za-z]:[\\/]", value)) or "\\" in value
    normalized = re.sub(r"/+", "/", value.replace("\\", "/")).rstrip("/") or "/"
    return (normalized.casefold() if windows else normalized), windows


def _item_sort_key(value: ValueReviewItem) -> tuple[int, int, int, str, str, str]:
    state_order = {
        ValueReviewState.EXACT_DUPLICATE_CANDIDATE: 0,
        ValueReviewState.REVIEW_LOW_VALUE: 1,
        ValueReviewState.ARCHIVE_CANDIDATE: 2,
        ValueReviewState.UNKNOWN: 3,
        ValueReviewState.KEEP: 4,
    }
    return (
        -value.review_priority,
        state_order[value.state],
        -value.size_bytes,
        value.path.casefold(),
        value.path,
        value.resource_id,
    )


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def _unique_provenance(values: tuple[ValueProvenance, ...]) -> tuple[ValueProvenance, ...]:
    unique = {
        (value.owner, value.schema_version, value.publication_id, value.read_mode): value
        for value in values
    }
    return tuple(unique[key] for key in sorted(unique))


def _unique_evidence(values: tuple[ValueEvidence, ...]) -> tuple[ValueEvidence, ...]:
    unique: dict[str, ValueEvidence] = {}
    for value in values:
        unique.setdefault(value.evidence_id, value)
    return tuple(unique[key] for key in sorted(unique))


__all__ = [
    "MAX_VALUE_REVIEW_OBSERVATIONS",
    "ValueFileObservation",
    "ValueReviewAvailability",
    "ValueReviewPaths",
    "ValueReviewQuery",
    "ValueReviewReport",
    "ValueReviewState",
    "preview_value_review",
    "rank_value_observations",
]
