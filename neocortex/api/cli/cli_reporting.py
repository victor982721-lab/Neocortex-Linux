"""Terminal summaries and strict-exit evaluation for completed framework runs."""

from __future__ import annotations
import argparse
import json
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, cast

from neocortex.deduplication.domain.models import DuplicateGroup
from neocortex.runtime.orchestration.replay_metrics import route_replay_metrics
from neocortex.api.read_contract import sanitize_untrusted_text

if TYPE_CHECKING:
    from neocortex.runtime.control.watcher import (
        WatcherEvent,
        WatcherRunSummary,
        WatcherSummary,
    )


# region [01] Route summaries


def _print_inventory_report(result) -> None:
    print(
        f"run_id={result.run_id} "
        f"files={result.scan.files_seen} "
        f"excluded_directories={result.scan.excluded_directories} "
        f"skipped_links={getattr(result.scan, 'skipped_links', 'no_verificado')} "
        f"inventory_errors={result.scan.errors}"
    )


def _print_pdf_report(result) -> None:
    if result.pdf is None:
        return
    pdf = result.pdf
    print(
        f"route=pdf "
        f"candidate_pool={pdf.candidate_pool} "
        f"candidates={pdf.candidates} "
        f"resumed_from_run_id={pdf.resumed_from_run_id} "
        f"extraction_phase_skipped={pdf.extraction_phase_skipped} "
        f"text_dedup_phase_skipped={pdf.text_dedup_phase_skipped} "
        f"skipped_by_size={pdf.skipped_by_size} "
        f"skipped_by_count={pdf.skipped_by_count} "
        f"processed={pdf.processed} "
        f"cache_hits={pdf.cache_hits} "
        f"cached_errors={pdf.cached_errors} "
        f"new_documents={pdf.new_documents} "
        f"cache_refreshes={pdf.cache_refreshes} "
        f"retried_documents={pdf.retried_documents} "
        f"retry_pages_planned={pdf.retry_pages_planned} "
        f"extracted={pdf.extracted} "
        f"protected={pdf.protected} "
        f"errors={pdf.errors} "
        f"unrecoverable_recycled={pdf.unrecoverable_recycled} "
        f"native_pages={pdf.native_pages} "
        f"ocr_pages={pdf.ocr_pages} "
        f"text_duplicate_groups={pdf.text_duplicate_groups} "
        f"text_duplicate_candidates={pdf.text_duplicate_candidates} "
        f"text_duplicate_policy={pdf.text_duplicate_policy} "
        f"text_duplicates_trashed={pdf.text_duplicates_trashed} "
        f"text_duplicate_skips={pdf.text_duplicate_skips} "
        f"fts_pages_indexed={pdf.fts_pages_indexed} "
        f"profiles_built={pdf.profiles_built} "
        f"profile_errors={pdf.profile_errors} "
        f"text_similarity_pairs={pdf.text_similarity_pairs} "
        f"template_similarity_pairs={pdf.template_similarity_pairs}"
        f" layout_similarity_pairs={pdf.layout_similarity_pairs} "
        f"layout_groups={pdf.layout_groups} "
        f"layout_pages_mapped={pdf.layout_pages_mapped}"
        f" partial_documents={pdf.partial_documents} "
        f"page_errors={pdf.page_errors} "
        f"document_timeouts={pdf.document_timeouts}"
        f" warning_documents={pdf.warning_documents} "
        f"mupdf_warnings={pdf.mupdf_warnings}"
        f" pdf_cache_documents_pruned={pdf.pdf_cache_documents_pruned} "
        f"pdf_cache_rows_pruned={pdf.pdf_cache_rows_pruned} "
        f"fts_rows_repaired={pdf.fts_rows_repaired} "
        f"memory_waits={pdf.memory_waits} "
        f"catalog_candidates={pdf.catalog_candidates} "
        f"catalog_classified={pdf.catalog_classified} "
        f"catalog_cache_hits={pdf.catalog_cache_hits} "
        f"catalog_review={pdf.catalog_review_required} "
        f"catalog_errors={pdf.catalog_errors} "
        f"catalog_source_stale={pdf.catalog_source_stale} "
        f"catalog_stale_marked={pdf.catalog_stale_marked}"
    )


def _print_docx_report(result) -> None:
    if result.docx is None:
        return
    docx = result.docx
    print(
        f"route=docx candidate_pool={docx.candidate_pool} candidates={docx.candidates} "
        f"skipped_by_size={docx.skipped_by_size} skipped_by_count={docx.skipped_by_count} "
        f"processed={docx.processed} cache_hits={docx.cache_hits} "
        f"cached_errors={docx.cached_errors} "
        f"new_documents={docx.new_documents} "
        f"retried_documents={docx.retried_documents} "
        f"extracted={docx.extracted} "
        f"partial_documents={docx.partial_documents} "
        f"cached_partial_documents={docx.cached_partial_documents} "
        f"errors={docx.errors} fts_documents_indexed={docx.fts_documents_indexed} "
        f"layouts_classified={docx.layouts_classified} layout_groups={docx.layout_groups} "
        f"pdf_matched={docx.pdf_matched} pdf_ambiguous={docx.pdf_ambiguous} "
        f"pdf_missing={docx.pdf_missing} "
        f"pdf_stale_candidates={docx.pdf_stale_candidates} "
        f"cache_documents_pruned={docx.cache_documents_pruned} "
        f"review_candidates={docx.review_candidates} "
        f"deletion_candidates={docx.deletion_candidates} "
        f"retryable_errors={docx.retryable_errors} "
        f"peak_reserved_bytes={docx.peak_reserved_bytes} "
        f"memory_waits={docx.memory_waits} "
        f"catalog_candidates={docx.catalog_candidates} "
        f"catalog_classified={docx.catalog_classified} "
        f"catalog_cache_hits={docx.catalog_cache_hits} "
        f"catalog_review={docx.catalog_review_required} "
        f"catalog_errors={docx.catalog_errors} "
        f"catalog_source_stale={docx.catalog_source_stale} "
        f"catalog_stale_marked={docx.catalog_stale_marked}"
    )


def _print_office_report(result) -> None:
    if result.office is None:
        return
    office = result.office
    print(
        f"route=office candidate_pool={office.candidate_pool} "
        f"candidates={office.candidates} processed={office.processed} "
        f"cache_hits={office.cache_hits} cached_errors={office.cached_errors} "
        f"extracted={office.extracted} errors={office.errors} "
        f"review_candidates={office.review_candidates} "
        f"deletion_candidates={office.deletion_candidates} "
        f"retryable_errors={office.retryable_errors} "
        f"catalog_candidates={office.catalog_candidates} "
        f"catalog_classified={office.catalog_classified} "
        f"catalog_cache_hits={office.catalog_cache_hits} "
        f"catalog_review={office.catalog_review_required} "
        f"catalog_errors={office.catalog_errors}"
    )


def _print_archive_report(result) -> None:
    if result.archive is None:
        return
    archive = result.archive
    print(
        f"route=archive candidate_pool={archive.candidate_pool} "
        f"candidates={archive.candidates} processed={archive.processed} "
        f"cache_hits={archive.cache_hits} cached_errors={archive.cached_errors} "
        f"complete={archive.containers_complete} partial={archive.containers_partial} "
        f"errors={archive.errors} members={archive.members_seen} "
        f"indexed={archive.members_indexed} metadata_only={archive.metadata_only} "
        f"nested_archives={archive.nested_archives} text_chars={archive.text_chars} "
        f"safety_issues={archive.safety_issues} "
        f"cache_containers_pruned={archive.cache_containers_pruned} "
        f"cache_members_pruned={archive.cache_members_pruned}"
    )


def _print_text_report(result) -> None:
    if result.text is None:
        return
    text = result.text
    print(
        f"route=text candidate_pool={text.candidate_pool} candidates={text.candidates} "
        f"processed={text.processed} cache_hits={text.cache_hits} "
        f"cached_errors={text.cached_errors} extracted={text.extracted} "
        f"plain_text={text.plain_text} emails={text.emails} "
        f"text_chars={text.text_chars} "
        f"truncated={text.truncated} errors={text.errors} "
        f"retryable_errors={text.retryable_errors} "
        f"cache_documents_pruned={text.cache_documents_pruned} "
        f"catalog_candidates={text.catalog_candidates} "
        f"catalog_classified={text.catalog_classified} "
        f"catalog_cache_hits={text.catalog_cache_hits} "
        f"catalog_review={text.catalog_review_required} "
        f"catalog_errors={text.catalog_errors}"
    )


def _print_audio_report(result) -> None:
    if result.audio is None:
        return
    audio = result.audio
    print(
        f"route=audio candidate_pool={audio.candidate_pool} "
        f"candidates={audio.candidates} processed={audio.processed} "
        f"cache_hits={audio.cache_hits} cached_errors={audio.cached_errors} "
        f"transcribed={audio.transcribed} no_speech={audio.no_speech} "
        f"no_audio={getattr(audio, 'no_audio', 0)} "
        f"errors={audio.errors} review_candidates={audio.review_candidates} "
        f"deletion_candidates={audio.deletion_candidates} "
        f"retryable_errors={audio.retryable_errors} "
        f"transcript_chars={audio.transcript_chars} "
        f"transcript_segments={audio.transcript_segments} "
        f"catalog_candidates={audio.catalog_candidates} "
        f"catalog_classified={audio.catalog_classified} "
        f"catalog_cache_hits={audio.catalog_cache_hits} "
        f"catalog_review={audio.catalog_review_required} "
        f"catalog_errors={audio.catalog_errors}"
    )


def _print_image_report(result) -> None:
    if result.image is None:
        return
    image = result.image
    print(
        f"route=image candidate_pool={image.candidate_pool} candidates={image.candidates} "
        f"skipped_by_size={image.skipped_by_size} "
        f"skipped_by_count={image.skipped_by_count} processed={image.processed} "
        f"cache_hits={image.cache_hits} cached_errors={image.cached_errors} "
        f"feature_cache_hits={image.feature_cache_hits} "
        f"new_images={image.new_images} "
        f"retried_images={image.retried_images} "
        f"reclassified_images={image.reclassified_images} "
        f"classified={image.classified} document_candidates={image.document_candidates} "
        f"industrial_context_candidates={image.industrial_context_candidates} "
        f"photo_candidates={image.photo_candidates} errors={image.errors} "
        f"document_ocr_attempts={image.document_ocr_attempts} "
        f"document_ocr_positive={image.document_ocr_positive} "
        f"document_ocr_failures={image.document_ocr_failures} "
        f"document_verifier_available={int(image.document_verifier_available)} "
        f"recovered_decodes={image.recovered_decodes} "
        f"retryable_errors={image.retryable_errors} "
        f"manual_review_errors={image.manual_review_errors} "
        f"deletion_candidates={image.deletion_candidates} "
        f"review_candidates_stored={image.review_candidates_stored} "
        f"cache_rows_pruned={image.cache_rows_pruned} "
        f"full_fingerprint_cache_hits={image.full_fingerprint_cache_hits} "
        f"full_fingerprints_computed={image.full_fingerprints_computed} "
        f"peak_reserved_bytes={image.peak_reserved_bytes} "
        f"memory_waits={image.memory_waits}"
    )


def _print_video_report(result) -> None:
    video = getattr(result, "video", None)
    if video is None:
        return
    print(
        f"route=video candidate_pool={video.candidate_pool} "
        f"candidates={video.candidates} processed={video.processed} "
        f"cache_hits={video.cache_hits} cached_errors={video.cached_errors} "
        f"complete={video.complete} partial={video.partial} errors={video.errors} "
        f"visual_only={video.visual_only} frames_sampled={video.frames_sampled} "
        f"scene_frames={video.scene_frames} keyframes={video.keyframes} "
        f"interval_frames={video.interval_frames} ocr_attempts={video.ocr_attempts} "
        f"ocr_positive={video.ocr_positive} ocr_failures={video.ocr_failures} "
        f"ocr_text_chars={video.ocr_text_chars} audio_links={video.audio_links} "
        f"audio_links_added_on_replay={video.audio_links_added_on_replay} "
        f"review_candidates={video.review_candidates} "
        f"deletion_candidates={video.deletion_candidates} "
        f"retryable_errors={video.retryable_errors} "
        f"cache_documents_pruned={video.cache_documents_pruned} "
        f"peak_reserved_bytes={video.peak_reserved_bytes} "
        f"memory_waits={video.memory_waits} "
        f"ocr_available={int(video.ocr_available)}"
    )


# endregion [01]


# region [02] Coordinator, inventory and actions


def _print_global_resource_report(result) -> None:
    resources = result.global_resources
    if resources is None:
        return
    print(
        f"coordinator memory_budget_bytes={resources.memory_budget_bytes} "
        f"min_free_memory_bytes={resources.min_free_memory_bytes} "
        f"min_free_commit_bytes={resources.min_free_commit_bytes} "
        f"cpu_slots={resources.cpu_slots} "
        f"max_cpu_load_percent={resources.max_cpu_load_percent:g} "
        f"peak_reserved_bytes={resources.peak_reserved_bytes} "
        f"peak_cpu_slots={resources.peak_cpu_slots} "
        f"peak_active_requests={resources.peak_active_requests} "
        f"min_observed_available_memory_bytes="
        f"{resources.min_observed_available_memory_bytes} "
        f"min_observed_available_commit_bytes="
        f"{resources.min_observed_available_commit_bytes} "
        f"max_observed_cpu_load_percent="
        f"{resources.max_observed_cpu_load_percent} "
        f"min_effective_cpu_slots={resources.min_effective_cpu_slots}"
    )
    for route_name, route in resources.routes.items():
        print(
            f"coordinator_route={route_name} admissions={route.admissions} "
            f"waits={route.waits} wait_seconds={route.wait_seconds:.6f} "
            f"wait_ns={route.wait_ns} "
            f"peak_reserved_bytes={route.peak_reserved_bytes} "
            f"peak_cpu_slots={route.peak_cpu_slots}"
        )


def _print_dedup_report(result) -> None:
    plan = result.dedup_plan
    journal_span = result.journal_usn_span
    print(
        f"duplicate_groups={plan.group_count} "
        f"reclaimable_bytes={plan.reclaimable_bytes} "
        f"reclaimable_bytes_kind=nominal_redundant "
        f"physical_reclaimable_bytes=not_verified "
        f"requested_policy={getattr(plan, 'requested_policy', 'legacy_unknown')} "
        f"dedup_coverage={getattr(plan, 'coverage', 'legacy_unknown')} "
        f"journal_usn_span={journal_span if journal_span is not None else 'unavailable'} "
        f"reconciliation_records={result.reconciliation_records} "
        f"inventory_attempts={result.inventory_attempts} "
        f"inventory_mode={result.inventory_mode}"
    )


def _print_action_report(result, dedup_policy: str) -> None:
    actions = result.actions
    action_counts = _action_counts(actions)
    print(
        f"action_mode={'apply' if actions.apply_actions else 'dry-run'} "
        f"dedup_policy={dedup_policy} "
        f"duplicate_candidates={actions.duplicate_candidates} "
        f"duplicates_trashed={actions.duplicates_trashed} "
        f"planned={action_counts['planned']} "
        f"applied={action_counts['applied']} "
        f"action_candidates={action_counts['candidates']} "
        f"action_skips={action_counts['skips']} "
        f"files_checked={actions.files_checked} "
        f"types_detected={actions.types_detected} "
        f"unknown_types={actions.unknown_types} "
        f"type_cache_hits={actions.type_cache_hits} "
        f"type_cache_misses={actions.type_cache_misses} "
        f"type_cache_pruned={actions.type_cache_pruned} "
        f"stale_inventory={actions.stale_inventory} "
        f"rename_candidates={actions.rename_candidates} "
        f"files_renamed={actions.files_renamed} "
        f"empty_directory_candidates={actions.empty_directory_candidates} "
        f"empty_directories_trashed={actions.empty_directories_trashed} "
        f"third_party_candidates={_optional_counter(actions, 'third_party_candidates') or 0} "
        f"third_party_trashed={_optional_counter(actions, 'third_party_trashed') or 0} "
        f"third_party_skips={_optional_counter(actions, 'third_party_skips') or 0} "
        f"action_errors={actions.errors}"
    )


def _print_organization_report(result) -> None:
    plan = getattr(result, "organization_plan", None)
    applied = getattr(result, "organization_apply", None)
    if plan is not None:
        print(
            f"organization_plan_considered={plan.considered} "
            f"organization_planned={plan.planned} "
            f"organization_review={plan.review_required} "
            f"organization_plan_blocked={plan.blocked} "
            f"organization_already_organized={plan.already_organized}"
        )
    if applied is not None:
        print(
            f"organization_selected={applied.selected} "
            f"organization_applied={applied.applied} "
            f"organization_stale={applied.stale} "
            f"organization_blocked={applied.blocked} "
            f"organization_failed={applied.failed} "
            f"organization_cache_synced={applied.cache_synced} "
            f"organization_cache_pending={applied.cache_pending} "
            f"organization_batches={applied.batches} "
            f"organization_remaining={applied.remaining}"
        )


def _print_code_report(result) -> None:
    summary = getattr(result, "code", None)
    if summary is None:
        return
    print(
        f"code_candidates={summary.candidates} "
        f"code_project_scope="
        f"{'projects' if summary.project_scope_enabled else 'broad'} "
        f"code_project_roots={summary.project_roots} "
        f"code_outside_project_skips={summary.outside_project_skips} "
        f"code_dependency_skips={summary.dependency_skips} "
        f"code_generated_scope_skips={summary.generated_scope_skips} "
        f"code_cache_skips={summary.cache_skips} "
        f"code_processed={summary.processed} "
        f"code_cache_hits={summary.cache_hits} "
        f"code_cache_batches={summary.cache_batches} "
        f"code_symbols={summary.symbols} "
        f"code_references={summary.references} "
        f"code_diagnostics={summary.diagnostics} "
        f"code_projects={summary.projects} "
        f"code_errors={summary.errors} "
        f"code_partial={getattr(summary, 'partial', 'no_verificado')} "
        f"code_text_only={getattr(summary, 'text_only', 'no_verificado')} "
        f"code_skipped_limit={getattr(summary, 'skipped_limit', 'no_verificado')} "
        f"code_stale_inventory={getattr(summary, 'stale_inventory', 'no_verificado')} "
        f"code_bytes_read={summary.bytes_read} "
        f"code_read_ms={summary.read_milliseconds} "
        f"code_analyze_ms={summary.analyze_milliseconds} "
        f"code_persist_ms={summary.persist_milliseconds} "
        f"code_cache_lookup_ms={summary.cache_lookup_milliseconds} "
        f"code_cache_update_ms={summary.cache_update_milliseconds} "
        f"code_cache_commit_ms={summary.cache_commit_milliseconds} "
        f"code_graph_ms={summary.graph_milliseconds}"
    )


def has_organization_errors(result) -> bool:
    """Return whether an authorized organization action remained unresolved."""

    plan = getattr(result, "organization_plan", None)
    applied = getattr(result, "organization_apply", None)
    return bool(
        (plan is not None and plan.blocked)
        or (
            applied is not None
            and (
                applied.stale
                or applied.blocked
                or applied.failed
                or applied.cache_pending
                or applied.remaining
            )
        )
    )


def _print_duplicate_groups(
    result,
    limit: int,
    *,
    emit: Callable[[str], object] = print,
) -> None:
    """Use the same bounded evidence detail for terminals and pipes."""
    plan = getattr(result, "dedup_plan", None)
    if plan is None:
        return
    if hasattr(plan, "keeper_reference_status"):
        status = plan.keeper_reference_status
        emit(
            f"KEEPER_REFERENCES status={'available' if status == 'available' else 'refs_unverified'} "
            f"evaluation={status} evidence_count={plan.keeper_reference_count} "
            f"reason={plan.keeper_reference_reason or '-'}"
        )
    if limit <= 0:
        return
    groups = tuple(cast(Iterable[DuplicateGroup], getattr(plan, "groups", ())))[:limit]
    total = int(plan.group_count)
    emit(
        f"DUPLICATE_GROUPS shown={len(groups)} total={total} "
        f"limit={limit} truncated={int(len(groups) < total)} "
        "scope=physical_files physical_reclaimable_bytes=not_verified"
    )
    for ordinal, group in enumerate(groups, 1):
        proof = getattr(group, "proof", None)
        emit(
            f"GROUP position={ordinal} "
            f"verification_mode={getattr(group, 'verification_mode', 'legacy_unknown')} "
            f"evidence={'available' if proof is not None else 'legacy_unknown'} "
            f"keeper_reason={getattr(proof, 'keeper_reason', 'not_recorded')}"
        )
        emit(f"KEEP {group.keep.path}")
        for redundant in group.redundant:
            emit(f"CANDIDATE {redundant.path}")


def print_reports(result, args: argparse.Namespace) -> None:
    _print_catalog_reports(result)
    if hasattr(result, "source_run_id"):
        print(f"run_id={result.run_id} mode=route-only source_run_id={result.source_run_id}")
        _print_pdf_report(result)
        _print_docx_report(result)
        _print_office_report(result)
        _print_archive_report(result)
        _print_text_report(result)
        _print_audio_report(result)
        _print_video_report(result)
        _print_image_report(result)
        _print_code_report(result)
        _print_global_resource_report(result)
        _print_organization_report(result)
        return
    _print_inventory_report(result)
    _print_pdf_report(result)
    _print_docx_report(result)
    _print_office_report(result)
    _print_archive_report(result)
    _print_text_report(result)
    _print_audio_report(result)
    _print_video_report(result)
    _print_image_report(result)
    _print_code_report(result)
    _print_global_resource_report(result)
    _print_dedup_report(result)
    _print_action_report(result, args.dedup_policy)
    _print_organization_report(result)
    _print_duplicate_groups(result, args.show_groups)


def _print_catalog_reports(result) -> None:
    """Expose observed catalog consumers independently of extraction counters."""

    for route, summary in getattr(result, "route_results", {}).items():
        candidates, cache_hits, new_work, replay_evidence = _route_replay_view(route, summary)
        print(f"ROUTE_REPLAY route={sanitize_untrusted_text(route, limit=32)} "
              f"candidates={candidates if candidates is not None else 'no_verificado'} "
              f"cache_hits={cache_hits if cache_hits is not None else 'no_verificado'} "
              f"new_work={new_work if new_work is not None else 'no_verificado'} evidence={replay_evidence}")
        issues = {name: value for name in _strict_route_error_fields(route, summary)
                  for value in (_optional_counter(summary, name),) if value}
        if issues:
            print(f"ROUTE_COVERAGE route={sanitize_untrusted_text(route, limit=32)} complete=0 "
                  f"issues={json.dumps(issues, sort_keys=True, separators=(',', ':'))} "
                  "next_action=inspect_owner_diagnostics_in_same_state")
        observed = _field_value(summary, "catalog_complete")
        if not observed[0] or observed[1] is None:
            continue
        counters = " ".join(
            f"{name}={_optional_counter(summary, name)}"
            for name in _CATALOG_FIELDS
            if _optional_counter(summary, name) is not None
        )
        print(f"ROUTE_CATALOG route={route} complete={int(bool(observed[1]))} {counters}")
    for route, reason in getattr(result, "route_failures", {}).items():
        print(f"ROUTE_UNAVAILABLE route={sanitize_untrusted_text(route, limit=32)} "
              f"reason={json.dumps(sanitize_untrusted_text(reason, limit=800), ensure_ascii=True)}")


def _human_count(value: int | float) -> str:
    return f"{int(value):,}".replace(",", " ")


def _route_metric_count(metrics: Mapping[str, object], key: str) -> int:
    value = metrics.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return 0
    if isinstance(value, str):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return 0
    return 0


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024.0
    raise AssertionError("unreachable")


def _counter_value(value: object) -> int | None:
    """Return a non-negative counter, preserving an absent/invalid value."""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed >= 0 else None
    return None


def _field_value(summary: object, field: str) -> tuple[bool, object]:
    """Read a summary field without converting missing data into zero."""

    if isinstance(summary, Mapping):
        if field not in summary:
            return False, None
        return True, summary[field]
    try:
        return True, getattr(summary, field)
    except AttributeError:
        return False, None


def _optional_counter(summary: object, field: str) -> int | None:
    present, value = _field_value(summary, field)
    return _counter_value(value) if present else None


def _archive_all_complete(summary: object) -> bool:
    """Recognize the Archive coverage contract before ignoring raw issue totals."""

    required = (
        "processed",
        "containers_complete",
        "containers_partial",
        "errors",
        "cached_errors",
    )
    values: dict[str, int] = {}
    for field in required:
        present, value = _field_value(summary, field)
        if not present or type(value) is not int or value < 0:
            return False
        values[field] = value
    return (
        values["processed"] > 0
        and values["containers_complete"] == values["processed"]
        and values["containers_partial"] == 0
        and values["errors"] == 0
        and values["cached_errors"] == 0
    )


def _strict_route_error_fields(route_name: str, summary: object) -> tuple[str, ...]:
    """Use owner-authoritative Archive coverage while retaining raw visibility."""

    if route_name == "archive" and _archive_all_complete(summary):
        return tuple(field for field in STRICT_ROUTE_ERROR_FIELDS if field != "safety_issues")
    return STRICT_ROUTE_ERROR_FIELDS


def _has_valid_counters(summary: object, fields: Iterable[str]) -> bool:
    return all(_optional_counter(summary, field) is not None for field in fields)


def _action_counts(actions: object) -> dict[str, int]:
    """Normalize action counters without presenting a dry-run as an effect."""

    candidates = sum(
        _optional_counter(actions, field) or 0
        for field in (
            "duplicate_candidates",
            "rename_candidates",
            "empty_directory_candidates",
            "third_party_candidates",
        )
    )
    skips = sum(
        _optional_counter(actions, field) or 0
        for field in (
            "duplicate_skips",
            "rename_skips",
            "empty_directory_skips",
            "third_party_skips",
        )
    )
    applied = sum(
        _optional_counter(actions, field) or 0
        for field in (
            "duplicates_trashed",
            "files_renamed",
            "empty_directories_trashed",
            "third_party_trashed",
        )
    )
    apply_requested = bool(_field_value(actions, "apply_actions")[1])
    return {
        "candidates": candidates,
        "skips": skips,
        "planned": 0 if apply_requested else max(0, candidates - skips),
        "applied": applied,
    }


def _route_issue_count(summary: object) -> int:
    partial = max(
        _optional_counter(summary, "partial_documents") or 0,
        _optional_counter(summary, "partial") or 0,
        _optional_counter(summary, "containers_partial") or 0,
        _optional_counter(summary, "page_errors") or 0,
        _optional_counter(summary, "document_timeouts") or 0,
    )
    fields = (
        "errors",
        "cached_errors",
        "profile_errors",
        "catalog_errors",
        "catalog_source_stale",
        "catalog_source_missing",
        "safety_issues",
        "protected",
        "retryable_errors",
        "manual_review_errors",
    )
    return partial + sum(_optional_counter(summary, field) or 0 for field in fields)


def _route_review_count(summary: object) -> int:
    direct = max(
        _optional_counter(summary, "review_candidates") or 0,
        _optional_counter(summary, "review_candidates_stored") or 0,
    )
    return direct + (_optional_counter(summary, "catalog_review_required") or 0)


def _route_review_value(summary: object) -> int | None:
    """Return review work only when the route summary exposes its counters."""

    fields = (
        "review_candidates",
        "review_candidates_stored",
        "catalog_review_required",
    )
    if not any(_optional_counter(summary, field) is not None for field in fields):
        return None
    return _route_review_count(summary)


def _route_issue_details(label: str, summary: object) -> tuple[str, ...]:
    """Describe typed route findings without treating benign outcomes as errors."""

    fields: list[tuple[str, str]] = [
        ("partial_documents", "parciales"),
        ("partial", "parciales"),
        ("containers_partial", "parciales"),
        ("protected", "protected"),
        ("safety_issues", "seguridad"),
        ("page_errors", "errores_pagina"),
        ("profile_errors", "errores_perfil"),
        ("document_timeouts", "timeouts"),
        ("errors", "errores"),
        ("cached_errors", "errores_cache"),
        ("catalog_source_stale", "catalogo_obsoleto"),
        ("catalog_source_missing", "owner_catalogo_ausente"),
        ("catalog_errors", "errores_catalogo"),
        ("retryable_errors", "reintentos"),
        ("manual_review_errors", "revision_manual"),
    ]
    if label == "Audio":
        # no_speech/no_audio are terminal observations, not extraction errors;
        # retain them as typed detail instead of silently collapsing them into OK.
        fields = [
            ("no_speech", "no_speech"),
            ("no_audio", "no_audio"),
            *fields,
        ]
    details: list[str] = []
    for field, label_text in fields:
        value = _optional_counter(summary, field)
        if value:
            details.append(f"{label_text}={value}")
    return tuple(dict.fromkeys(details))


_CATALOG_FIELDS = (
    "catalog_candidates",
    "catalog_classified",
    "catalog_cache_hits",
    "catalog_review_required",
    "catalog_errors",
    "catalog_source_stale",
    "catalog_source_missing",
    "catalog_stale_marked",
)
_FTS_FIELDS = (
    "fts_pages_indexed",
    "fts_documents_indexed",
    "fts_rows_repaired",
)
_DERIVED_ZERO_FIELDS = (*_FTS_FIELDS, "profiles_built")
_REPLAY_FIELDS = ("candidates", "processed", "cache_hits", "cached_errors", "new_work")
_ROUTE_NAMES = {
    "PDF": "pdf",
    "DOCX": "docx",
    "Office": "office",
    "ZIP": "archive",
    "Texto": "text",
    "Audio": "audio",
    "Video": "video",
    "Imágenes": "image",
    "Código": "code",
}


def _route_replay_view(
    route_name: str,
    summary: object,
) -> tuple[int | None, int | None, int | None, str]:
    """Return replay counters plus a truthful evidence status."""

    candidates = _optional_counter(summary, "candidates")
    cache_hits = _optional_counter(summary, "cache_hits")
    cached_errors = _optional_counter(summary, "cached_errors")
    if candidates is None:
        return None, None, None, "no_verificado"
    if candidates == 0:
        # Zero candidates is an observed empty selection, not complete coverage.
        return candidates, cache_hits, 0, "sin_candidatos"
    if route_name == "code" and _has_valid_counters(summary, ("candidates", "processed", "cache_hits")):
        # Code counts cache misses in processed, and includes cached errors in
        # cache_hits/errors. It has no separate cached_errors counter; requiring
        # one would hide known replay work from this built-in owner.
        work = _optional_counter(summary, "processed")
        observed = work is not None and cache_hits is not None and work + cache_hits > 0
        return candidates, cache_hits, work, "observado" if observed else "no_verificado"
    explicit_new_work = _optional_counter(summary, "new_work")
    new_work: int | None
    if explicit_new_work is not None:
        new_work = explicit_new_work
    elif _has_valid_counters(summary, _REPLAY_FIELDS[:4]):
        new_work = _counter_value(route_replay_metrics(route_name, summary).get("new_work"))
    else:
        new_work = None
    if (
        cache_hits is None
        or cached_errors is None
        or new_work is None
        or not _has_valid_counters(summary, _REPLAY_FIELDS[:4])
    ):
        return candidates, cache_hits, new_work, "no_verificado"
    if new_work + cache_hits + cached_errors == 0:
        # A positive candidate count with no observed work/reuse is not a
        # successful zero-work replay; it lacks route coverage evidence.
        return candidates, cache_hits, new_work, "no_verificado"
    return candidates, cache_hits, new_work, "observado"


def _route_coverage_notes(
    summary: object,
    *,
    candidates: int | None,
    replay_status: str,
) -> tuple[str, ...]:
    """Surface absent owner/catalog/index evidence instead of printing false zeroes."""

    notes: list[str] = []
    catalog_complete = _field_value(summary, "catalog_complete")
    if catalog_complete[0] and catalog_complete[1] is False:
        notes.append("catalogo=incompleto; revisar owner/errores antes de consultar")
    catalog_candidates = _optional_counter(summary, "catalog_candidates")
    if catalog_complete == (True, None) or not any(
        _optional_counter(summary, field) is not None for field in _CATALOG_FIELDS
    ):
        notes.append("catalogo=no_verificado")
    elif (
        candidates is not None
        and candidates > 0
        and catalog_candidates is not None
        and catalog_candidates != candidates
    ):
        notes.append(f"catalogo={catalog_candidates}/{candidates}")
    candidate_pool = _optional_counter(summary, "candidate_pool")
    if (
        candidates is not None
        and candidate_pool is not None
        and candidate_pool != candidates
    ):
        notes.append(f"candidatos={candidates}/{candidate_pool}")
    fts_values = {field: _optional_counter(summary, field) for field in _FTS_FIELDS}
    if not any(value is not None for value in fts_values.values()):
        notes.append("fts=no_verificado")
    else:
        notes.extend(
            f"{field}={value}"
            for field, value in fts_values.items()
            if value is not None and (value == 0 or candidates == 0)
        )
    notes.extend(
        f"{field}={value}"
        for field in _DERIVED_ZERO_FIELDS
        if field not in fts_values
        for value in (_optional_counter(summary, field),)
        if value is not None and (value == 0 or candidates == 0)
    )
    notes.extend(
        f"{field}={value}"
        for field in (
            "skipped_by_size",
            "skipped_by_count",
            "cache_refreshes",
            "recovered_decodes",
            "cache_documents_pruned",
            "cache_rows_pruned",
        )
        for value in (_optional_counter(summary, field),)
        if value
    )
    if replay_status == "no_verificado":
        notes.append("replay=no_verificado")
    elif replay_status == "sin_candidatos":
        notes.append("replay=sin_candidatos")
    owner_evidence = (
        _field_value(summary, "processing_provenance"),
        _field_value(summary, "processing_signature"),
    )
    if not any(present and value not in (None, "") for present, value in owner_evidence):
        notes.append("owner=no_verificado")
    return tuple(notes)


def _professional_route_rows(result) -> tuple[tuple[str, object], ...]:
    labels = (
        ("PDF", "pdf"),
        ("DOCX", "docx"),
        ("Office", "office"),
        ("ZIP", "archive"),
        ("Texto", "text"),
        ("Audio", "audio"),
        ("Video", "video"),
        ("Imágenes", "image"),
        ("Código", "code"),
    )
    return tuple(
        (label, summary)
        for label, attribute in labels
        if (summary := getattr(result, attribute, None)) is not None
    )


def _semantic_totals(
    semantic_results: tuple[tuple[str, object], ...],
) -> dict[str, int]:
    totals = {
        "items": 0,
        "chunks": 0,
        "new_jobs": 0,
        "reused": 0,
        "embedded": 0,
        "pending": 0,
        "errors": 0,
        "stale": 0,
        "incomplete": 0,
        "truncated": 0,
        "scopes_incomplete": 0,
    }
    for _scope, result in semantic_results:
        totals["scopes_incomplete"] += int(getattr(result, "complete", None) is not True)
        totals["items"] += int(getattr(result, "items_staged", 0))
        totals["chunks"] += int(getattr(result, "chunks_staged", 0))
        totals["new_jobs"] += int(getattr(result, "new_jobs_staged", 0))
        totals["errors"] += int(getattr(result, "errors", 0))
        totals["stale"] += int(getattr(result, "stale", 0))
        totals["incomplete"] += int(getattr(result, "incomplete", 0))
        totals["truncated"] += int(bool(getattr(result, "truncated", False)))
        for work in getattr(result, "generations", ()):
            summary = work.summary
            embedded = int(getattr(work, "embedded", 0))
            totals["reused"] += max(
                int(getattr(work, "reused", 0)),
                int(summary.done) - embedded,
            )
            totals["embedded"] += embedded
            totals["pending"] += int(summary.pending) + int(summary.leased)
    return totals


def print_professional_summary(
    result,
    args: argparse.Namespace,
    *,
    semantic_results: tuple[tuple[str, object], ...] = (),
    semantic_exit_code: int = 0,
    semantic_attempted: bool = True,
) -> None:
    """Render one concise human terminal report while raw output stays pipe-safe."""

    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()
    route_rows = _professional_route_rows(result)
    route_failures = getattr(result, "route_failures", {})
    catalog_unknown = not getattr(args, "no_document_catalog", False) and any(
        _field_value(summary, "catalog_complete") == (True, None)
        and (_optional_counter(summary, "candidates") or 0) > 0
        for _, summary in route_rows
    )
    route_issues = sum(_route_issue_count(summary) for _, summary in route_rows)
    route_replay_unknown = any(
        _route_replay_view(_ROUTE_NAMES.get(label, label.casefold()), summary)[3]
        == "no_verificado"
        for label, summary in route_rows
    )
    action_errors = _optional_counter(getattr(result, "actions", None), "errors") or 0
    semantic_totals = _semantic_totals(semantic_results)
    semantic_issues = (
        semantic_exit_code != 0
        or semantic_totals["errors"] > 0
        or semantic_totals["stale"] > 0
        or semantic_totals["incomplete"] > 0
        or semantic_totals["truncated"] > 0
        or semantic_totals["scopes_incomplete"] > 0
    )
    has_attention = bool(
        route_issues
        or route_failures
        or catalog_unknown
        or route_replay_unknown
        or action_errors
        or semantic_issues
        or has_organization_errors(result)
    )
    status = Text(
        "COMPLETADA CON INCIDENCIAS" if has_attention else "COMPLETADA",
        style="bold yellow" if has_attention else "bold green",
    )
    run_id = getattr(result, "run_id", "-")
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    command_label = (
        "Neocortex --all"
        if args.all
        else f"Neocortex · ruta {getattr(args, 'route', 'seleccionada')}"
    )
    header.add_row(
        Text(f"{command_label} · ejecución {run_id}", style="bold cyan"),
        status,
    )
    console.print(Panel(header, border_style="cyan", padding=(0, 1)))

    routes = Table(
        title="Cobertura del framework",
        header_style="bold",
        border_style="bright_black",
        show_lines=False,
        expand=True,
    )
    routes.add_column("Ruta", style="bold")
    routes.add_column("Estado", min_width=8, no_wrap=True)
    routes.add_column("Candidatos", justify="right")
    routes.add_column("Caché", justify="right")
    routes.add_column("Trabajo real", justify="right")
    routes.add_column("Incidencias", justify="right")
    routes.add_column("Revisión", justify="right")
    coverage_notes: list[str] = []
    for label, summary in route_rows:
        route_name = _ROUTE_NAMES.get(label, label.casefold())
        candidates, cache_hits, work, replay_status = _route_replay_view(
            route_name,
            summary,
        )
        issues = _route_issue_count(summary)
        review = _route_review_value(summary)
        issue_details = _route_issue_details(label, summary)
        issue_text = _human_count(issues)
        if issue_details:
            issue_text += f" ({', '.join(issue_details)})"
        if candidates is None:
            route_status = "NO VERIFICADO"
        elif issues:
            route_status = "ATENCIÓN"
        elif candidates == 0:
            route_status = "SIN CANDIDATOS"
        elif replay_status == "no_verificado":
            route_status = "NO VERIFICADO"
        elif _field_value(summary, "catalog_complete") == (True, None) and not getattr(args, "no_document_catalog", False):
            route_status = "NO VERIFICADO"
        else:
            route_status = "OK"
        coverage_notes.extend(
            f"{label}: {note}" for note in _route_coverage_notes(
                summary,
                candidates=candidates,
                replay_status=replay_status,
            )
        )
        if issue_details:
            coverage_notes.append(f"{label}: " + ", ".join(issue_details))
        routes.add_row(
            label,
            Text(
                route_status,
                style="yellow" if route_status != "OK" else "green",
            ),
            "no_verificado" if candidates is None else _human_count(candidates),
            "no_verificado" if cache_hits is None else _human_count(cache_hits),
            "no_verificado" if work is None else _human_count(work),
            Text(issue_text, style="yellow" if issues else "bright_black"),
            (
                "no_verificado"
                if review is None
                else Text(_human_count(review), style="yellow" if review else "bright_black")
            ),
        )
    for name, reason in route_failures.items():
        label = next((label for label, route in _ROUTE_NAMES.items() if route == name), name)
        routes.add_row(label, Text("NO DISPONIBLE", style="yellow"),
                       "no_verificado", "no_verificado", "no_verificado", "1", "pendiente")
        coverage_notes.append(f"{label}: {sanitize_untrusted_text(reason, limit=800)}. "
                              "Revisar la dependencia local; el resto de rutas se conserva.")
    console.print(routes)
    if coverage_notes:
        console.print(
            Panel(
                Text("\n".join(coverage_notes)),
                title="Límites de cobertura",
                border_style="yellow",
            )
        )

    if semantic_results:
        semantic_ok = not semantic_issues and semantic_totals["pending"] == 0
        semantic_status = "PUBLICADO" if semantic_ok else "REANUDABLE"
        semantic = Table(
            title=Text.assemble(
                "Semantic · ",
                (semantic_status, "green" if semantic_ok else "yellow"),
            ),
            header_style="bold",
            border_style="bright_black",
            expand=True,
        )
        semantic.add_column("Fuentes")
        semantic.add_column("Elementos", justify="right")
        semantic.add_column("Fragmentos", justify="right")
        semantic.add_column("Reutilizados", justify="right")
        semantic.add_column("Vectores nuevos", justify="right")
        semantic.add_column("Pendientes", justify="right")
        sources = tuple(
            dict.fromkeys(
                source
                for _scope, semantic_result in semantic_results
                for source in getattr(semantic_result, "sources", ())
            )
        )
        semantic.add_row(
            ", ".join(sources) or "-",
            _human_count(semantic_totals["items"]),
            _human_count(semantic_totals["chunks"]),
            _human_count(semantic_totals["reused"]),
            _human_count(semantic_totals["embedded"]),
            Text(
                _human_count(semantic_totals["pending"]),
                style="yellow" if semantic_totals["pending"] else "green",
            ),
        )
        console.print(semantic)
    elif args.all:
        if semantic_exit_code:
            semantic_state = "ERROR"
        elif semantic_attempted:
            semantic_state = "SIN TRABAJO PUBLICADO"
        else:
            semantic_state = "OMITIDO POR INCIDENCIAS PREVIAS"
        console.print(
            Panel(
                f"Semantic: {semantic_state}",
                title="Semantic",
                border_style="red" if semantic_exit_code else "yellow",
            )
        )

    unavailable_models = getattr(args, "_semantic_scope_unavailable", {})
    if unavailable_models:
        console.print(Panel(Text("\n".join(
            f"{sanitize_untrusted_text(scope, limit=32)}: {sanitize_untrusted_text(reason, limit=800)}"
            for scope, reason in tuple(unavailable_models.items())[:2]
        ) + "\nVerificar el modelo local requerido; no se descarga automáticamente."),
            title="Dependencias Semantic", border_style="yellow"))

    details: list[Text] = []
    scan = getattr(result, "scan", None)
    if scan is not None:
        details.append(
            Text.assemble(
                ("Inventario: ", "bold"),
                _human_count(scan.files_seen),
                " archivos · ",
                _human_count(scan.errors),
                " errores",
                " · exclusiones: directorios=",
                str(getattr(scan, "excluded_directories", "no_verificado")),
                ", enlaces=",
                str(getattr(scan, "skipped_links", "no_verificado")),
            )
        )
    plan = getattr(result, "dedup_plan", None)
    if plan is not None:
        details.append(
            Text.assemble(
                ("Duplicados: ", "bold"),
                _human_count(plan.group_count),
                " grupos · ",
                _human_bytes(plan.reclaimable_bytes),
                " redundantes nominales · espacio liberable no verificado",
            )
        )
    actions = getattr(result, "actions", None)
    if actions is not None:
        action_counts = _action_counts(actions)
        details.append(
            Text.assemble(
                ("Acciones: ", "bold"),
                ("aplicadas" if getattr(actions, "apply_actions", False) else "simulación segura"),
                " · ",
                _human_count(action_counts["planned"]),
                " planeadas · ",
                _human_count(action_counts["applied"]),
                " aplicadas · ",
                _human_count(action_counts["skips"]),
                " omitidas",
            )
        )
        details.append(
            Text.assemble(
                ("Tipos: ", "bold"),
                _human_count(getattr(actions, "types_detected", 0) or 0),
                " identificados · ",
                _human_count(getattr(actions, "unknown_types", 0) or 0),
                " aún desconocidos",
            )
        )
    if getattr(result, "inventory_mode", None) == "full":
        details.append(
            Text(
                "Esta ejecución hizo inventario completo; no acredita extracción completa.",
                style="yellow",
            )
        )
    console.print(
        Panel(
            Group(*details),
            title="Resultado operativo",
            border_style="yellow" if has_attention else "green",
        )
    )
    _print_duplicate_groups(
        result,
        int(getattr(args, "show_groups", 0)),
        emit=lambda line: console.print(Text(line), soft_wrap=True),
    )


# endregion [02]


# region [03] Foreground watcher reporting


def print_watcher_event(event: WatcherEvent) -> None:
    """Print one structured watcher lifecycle event."""

    message = json.dumps(event.message, ensure_ascii=False)
    details = json.dumps(
        event.details,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    print(
        f"WATCH_EVENT sequence={event.sequence} kind={event.kind} "
        f"timestamp_ns={event.timestamp_ns} message={message} details={details}"
    )


def print_watcher_run_summary(summary: WatcherRunSummary) -> None:
    """Print the result of one serialized integrated watcher run."""

    checkpoint_usn = (
        "-" if summary.checkpoint_before is None else summary.checkpoint_before.next_usn
    )
    error_detail = json.dumps(summary.error_detail, ensure_ascii=False)
    print(
        f"WATCH_RUN reason={summary.reason} succeeded={int(summary.succeeded)} "
        f"run_id={summary.run_id or '-'} inventory_mode={summary.inventory_mode or '-'} "
        f"elapsed_ns={summary.elapsed_ns} checkpoint_usn={checkpoint_usn} "
        f"error_type={summary.error_type or '-'} error_detail={error_detail}"
    )


def print_watcher_summary(summary: WatcherSummary) -> None:
    """Print bounded counters for a completed foreground watcher."""

    print(
        f"WATCH_SUMMARY cancelled={int(summary.cancelled)} "
        f"bootstrap_runs={summary.bootstrap_runs} change_runs={summary.change_runs} "
        f"discontinuity_runs={summary.discontinuity_runs} "
        f"portable_runs={summary.portable_runs} "
        f"successful_runs={summary.successful_runs} failed_runs={summary.failed_runs} "
        f"signal_batches={summary.signal_batches} "
        f"signal_records={summary.signal_records} idle_polls={summary.idle_polls} "
        f"source_restarts={summary.source_restarts} "
        f"source_errors={summary.source_errors} backoff_waits={summary.backoff_waits} "
        f"checkpoint_loads={summary.checkpoint_loads} "
        f"started_ns={summary.started_ns} finished_ns={summary.finished_ns}"
    )


def print_watcher_interrupted() -> None:
    """Report an interactive interruption when no final summary was returned."""

    print("WATCH_SUMMARY cancelled=1 interrupted=1")


def watcher_exit_code(summary: WatcherSummary) -> int:
    """Distinguish interactive cancellation from retained watcher errors."""

    if summary.cancelled:
        return 130
    return 2 if summary.failed_runs or summary.source_errors else 0


# endregion [03]


# region [04] Strict exit policy

STRICT_ROUTE_ERROR_FIELDS = (
    "errors",
    "cached_errors",
    "profile_errors",
    "page_errors",
    "partial_documents",
    "partial",
    "document_timeouts",
    "catalog_errors",
    "catalog_source_stale",
    "catalog_source_missing",
    "containers_partial",
    "protected",
    "safety_issues",
    "retryable_errors",
    "manual_review_errors",
)


def has_strict_route_errors(result) -> bool:
    """Return whether any completed route reported incomplete content work."""

    for route_name, summary in result.route_results.items():
        for field in _strict_route_error_fields(route_name, summary):
            value = (
                summary.get(field, 0)
                if isinstance(summary, Mapping)
                else getattr(summary, field, 0)
            )
            if isinstance(value, (int, float)) and value > 0:
                return True
    return False


# endregion [04]
