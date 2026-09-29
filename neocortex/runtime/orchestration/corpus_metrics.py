"""Observed curation work, kept distinct from estimates of saved compute."""

from __future__ import annotations

from collections.abc import Mapping


def _count(source: object, key: str) -> int | None:
    value = source.get(key) if isinstance(source, Mapping) else getattr(source, key, None)
    return value if type(value) is int and value >= 0 else None


def corpus_metrics(*, inventory_files: int, actions, plan, admission: Mapping,
                   zip_intake: Mapping, email_intake: Mapping, route_survivors: int,
                   organization_plan, organization_apply, residual: Mapping,
                   verification: Mapping, fast_curation: Mapping,
                   duplicate_bytes_removed: int | None, duplicates_removed: int,
                   empty_files_removed: int, wall_time_ns: int) -> dict[str, object]:
    """No unobserved CPU/RAM savings, no Trash bytes claimed as disk space."""
    nested = email_intake.get("nested_zip_intake", {})
    nested = nested if isinstance(nested, Mapping) else {}
    final = verification.get("metrics", {})
    final = final if isinstance(final, Mapping) else {}
    return {
        "schema": "neocortex.corpus-metrics/v1",
        "inventory_files": inventory_files,
        "preclean_matched": _count(admission, "preclean_matched"),
        "preclean_trashed": _count(admission, "preclean_trashed"),
        "identify_checked": actions.files_checked,
        "identify_cache_hits": actions.type_cache_hits,
        "identify_unknown": actions.unknown_types,
        "renamed": actions.files_renamed,
        "redlist_matched": _count(admission, "redlist_matched"),
        "redlist_trashed": _count(admission, "redlist_trashed"),
        "content_cleanup_matched": _count(admission, "content_cleanup_matched"),
        "content_cleanup_trashed": _count(admission, "content_cleanup_trashed"),
        "archives_examined": sum(_count(value, "containers_examined") or 0 for value in (zip_intake, nested)),
        "archives_extracted": sum(_count(value, "applied") or 0 for value in (zip_intake, nested)),
        "archives_trash_after_extract": sum(_count(value, "trashed") or 0 for value in (zip_intake, nested)),
        "duplicates_removed": duplicates_removed,
        "empty_files_removed": empty_files_removed,
        "duplicate_bytes_reclaimed": duplicate_bytes_removed,
        "duplicate_bytes_basis": "nominal_bytes_removed_from_corpus_not_disk_space_freed",
        "dedupe_hashed_files": plan.statistics.full_hash_files,
        "dedupe_hash_read_bytes": plan.statistics.hash_read_bytes,
        "dedupe_exact_comparison_bytes": plan.statistics.exact_comparison_bytes,
        "route_survivors": route_survivors,
        "classified": _count(final, "classified"),
        "organized": _count(organization_apply, "applied"),
        "unclassified": _count(residual, "unclassified"),
        "mime_buckets": _count(final, "mime_buckets"),
        "empty_directories_removed": actions.empty_directories_trashed,
        "blocked": _count(final, "blocked"),
        "recovery_required": _count(final, "recovery_required"),
        "final_files": _count(final, "final_files"),
        "final_bytes": _count(final, "final_bytes"),
        "expensive_work_avoided": _count(admission, "expensive_work_avoided"),
        "expensive_work_avoided_basis": admission.get("expensive_work_avoided_basis"),
        "curation": dict(fast_curation),
        "wall_time_ns": wall_time_ns,
    }
