"""Bounded direct CLI operations over durable NeoCortex state."""

from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from neocortex.safety.internal_paths import InternalPathsPolicy
    from neocortex.safety.protected_content import ProtectedContentPolicy


def _capture_authorized_direct_state_policies(
    state_directory: Path,
    *,
    lock: bool,
    databases: tuple[Path, ...],
) -> tuple[InternalPathsPolicy, ProtectedContentPolicy]:
    """Capture one fail-closed policy pair before any direct state write."""

    from neocortex.safety.internal_paths import canonical_internal_paths_policy
    from neocortex.integrations.inventory.inventory_boundary import (
        state_sqlite_mutation_paths,
        validate_authorized_state_path,
    )
    from neocortex.safety.protected_content import canonical_protected_content_policy

    internal_policy = canonical_internal_paths_policy()
    protected_policy = canonical_protected_content_policy()
    mutation_paths = (
        *((state_directory / "framework.lock",) if lock else ()),
        *(target for database in databases for target in state_sqlite_mutation_paths(database)),
    )
    validate_authorized_state_path(
        state_directory,
        internal_paths_policy=internal_policy,
        protected_content_policy=protected_policy,
        mutation_paths=mutation_paths,
    )
    return internal_policy, protected_policy


# region [01] Operational status


def run_operational_status(args: argparse.Namespace) -> int:
    """Print bounded execution state without initializing or migrating it."""

    if args.status_json:
        # The JSON CLI is the same read-only lifecycle envelope exposed by the
        # Python facade and MCP.  Keep the legacy human-readable output below
        # independent so terminal users retain its compact route table.
        from neocortex.api.lifecycle_read_api import lifecycle_status_payload

        try:
            payload = lifecycle_status_payload(
                limit=args.status_limit,
                run_id=args.status_run,
                state_directory=args.state_directory,
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            print(f"ERROR status {exc}")
            return 2
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        exit_code = payload.get("exit_code")
        if payload.get("coverage") == "unavailable" and exit_code == 1:
            # Preserve the CLI's historical operational failure code while
            # the shared read envelope keeps its finer-grained API code.
            return 2
        return exit_code if type(exit_code) is int else 2

    from neocortex.runtime.orchestration.run_status import list_run_status

    database_path = args.state_directory / "framework.sqlite3"
    try:
        statuses = list_run_status(
            database_path,
            limit=args.status_limit,
            run_id=args.status_run,
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR status {exc}")
        return 2
    for status in statuses:
        print(
            f"RUN id={status.run_id} kind={status.run_kind} status={status.status} "
            f"phase={status.current_phase or '-'} source={status.source_run_id or '-'} "
            f"pid={status.owner_pid or '-'} owner_alive={status.owner_alive} "
            f"heartbeat_stale={status.heartbeat_stale} "
            f"elapsed_ns={status.elapsed_ns} "
            f"recovery_required={status.recovery_required_actions} root={status.root}"
        )
        for route in status.routes:
            print(
                f"ROUTE run={status.run_id} name={route.route_name} "
                f"status={route.status} phase={route.current_phase or '-'} "
                f"candidates={route.candidates} processed={route.processed} "
                f"cache_hits={route.cache_hits} new_work={route.new_work} "
                f"cached_errors={route.cached_errors} "
                f"elapsed_ns={route.elapsed_ns} "
                f"replayability={route.resume_capability} "
                f"replay_status={route.replay_status} error={route.error_type or '-'}"
            )
            for phase in route.phases:
                print(
                    f"PHASE run={status.run_id} route={route.route_name} "
                    f"name={phase.phase_name} status={phase.status} "
                    f"elapsed_ns={phase.elapsed_ns} "
                    f"error={phase.error_type or '-'}"
                )
    return 0


# endregion [01]


# region [02] Uncertain file-action recovery


def run_file_action_recovery_status(args: argparse.Namespace) -> int:
    """Classify uncertain effects without migrating state or repeating actions."""

    from neocortex.workflow.actions.file_action_recovery import list_file_action_reconciliations

    database_path = args.state_directory / "framework.sqlite3"
    try:
        database_path.lstat()
        results = list_file_action_reconciliations(
            database_path,
            limit=args.action_recovery_limit,
            after_action_id=args.action_recovery_after,
            run_id=args.action_recovery_run,
        )
    except FileNotFoundError:
        if args.action_recovery_json and not getattr(args, "action_recovery_json_lines", False):
            print(
                json.dumps(
                    {
                        "kind": "file-action-reconciliation-page",
                        "schema_version": 2,
                        "availability": "absent",
                        "returned": 0,
                        "total_matching": None,
                        "has_more": None,
                        "next_cursor": None,
                        "items": [],
                        "complete": False,
                        "reason_code": "owner_absent",
                        "read_only": True,
                        "actions_applied": False,
                        "scope": {
                            "owner": "framework",
                            "limit": args.action_recovery_limit,
                            "after_action_id": args.action_recovery_after,
                            "run_id": args.action_recovery_run,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print(
                "ACTION_RECOVERY_PAGE returned=0 availability=absent complete=false reason=owner_absent"
            )
        return 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        if args.action_recovery_json and not getattr(args, "action_recovery_json_lines", False):
            print(
                json.dumps(
                    {
                        "kind": "file-action-reconciliation-page",
                        "complete": False,
                        "availability": "failed",
                        "returned": 0,
                        "total_matching": None,
                        "items": [],
                        "reason_code": "recovery_query_failed",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print(f"ERROR action-recovery-status {exc}")
        return 2
    ready = database_path.is_file()
    limit_reached = len(results) == args.action_recovery_limit
    if args.action_recovery_json and not getattr(args, "action_recovery_json_lines", False):
        items = []
        for result in results:
            items.append(
                {
                    "kind": "file-action-reconciliation",
                    **{
                        name: getattr(result, name)
                        for name in (
                            "action_id",
                            "action_type",
                            "classification",
                            "detail",
                            "idempotency_key",
                            "recommendation",
                            "recorded_status",
                            "reconciler_signature",
                            "run_id",
                            "source_path",
                            "target_path",
                        )
                    },
                }
            )
        unsafe = any(
            item["classification"] in {"ambiguous", "impossible_to_check"} for item in items
        )
        print(
            json.dumps(
                {
                    "kind": "file-action-reconciliation-page",
                    "schema_version": 2,
                    "availability": "ready" if ready else "absent",
                    "returned": len(items),
                    "total_matching": None,
                    "has_more": None if limit_reached else False,
                    "next_cursor": results[-1].action_id if results and limit_reached else None,
                    "complete": ready and not limit_reached and not unsafe,
                    "reason_code": (
                        "owner_absent"
                        if not ready
                        else "limit_reached_more_not_verified"
                        if limit_reached
                        else "uncertain_actions"
                        if unsafe
                        else None
                    ),
                    "scope": {
                        "owner": "framework",
                        "limit": args.action_recovery_limit,
                        "after_action_id": args.action_recovery_after,
                        "run_id": args.action_recovery_run,
                    },
                    "items": items,
                    "read_only": True,
                    "actions_applied": False,
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2 if unsafe or not ready else 0
    if not args.action_recovery_json:
        print(
            f"ACTION_RECOVERY_PAGE returned={len(results)} limit={args.action_recovery_limit} "
            f"availability={'ready' if ready else 'absent'} "
            f"has_more={'not_verified' if limit_reached else 'false'} "
            f"after_action_id={args.action_recovery_after}"
        )
    unsafe = False
    for result in results:
        unsafe = unsafe or result.classification in {
            "ambiguous",
            "impossible_to_check",
        }
        if args.action_recovery_json:
            print(
                json.dumps(
                    {
                        "action_id": result.action_id,
                        "action_type": result.action_type,
                        "classification": result.classification,
                        "detail": result.detail,
                        "idempotency_key": result.idempotency_key,
                        "kind": "file-action-reconciliation",
                        "recommendation": result.recommendation,
                        "recorded_status": result.recorded_status,
                        "reconciler_signature": result.reconciler_signature,
                        "run_id": result.run_id,
                        "source_path": result.source_path,
                        "target_path": result.target_path,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            continue
        print(
            f"ACTION_RECOVERY action={result.action_id} run={result.run_id} "
            f"type={result.action_type} status={result.recorded_status} "
            f"classification={result.classification} "
            f"reconciler={result.reconciler_signature} "
            f"recommendation={result.recommendation} "
            f"source={result.source_path} target={result.target_path or '-'} "
            f"detail={result.detail}"
        )
    return 2 if unsafe or not ready else 0


def run_file_action_recovery_record(args: argparse.Namespace) -> int:
    """Explicitly append one reconciliation observation without mutating files."""

    from neocortex.workflow.actions.file_action_reconciliation_store import (
        FileActionReconciliationConflict,
    )
    from neocortex.workflow.actions.file_action_recovery import list_file_action_reconciliations
    from neocortex.persistence.framework_state_writer import FrameworkState

    database_path = args.state_directory / "framework.sqlite3"
    action_id = args.action_recovery_record
    try:
        _capture_authorized_direct_state_policies(
            args.state_directory,
            lock=False,
            databases=(database_path,),
        )
        results = list_file_action_reconciliations(
            database_path,
            limit=1,
            after_action_id=action_id - 1,
        )
        if not results or results[0].action_id != action_id:
            print(f"ERROR action-recovery-record action {action_id} is not recoverable")
            return 2
        reconciliation = results[0]
        provenance_json = json.dumps(
            {
                "interface": "Neocortex CLI",
                "operation": "action-recovery-record",
                "schema_version": 1,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with FrameworkState(database_path, existing_only=True) as state:
            recorded = state.record_file_action_reconciliation(
                reconciliation,
                actor=args.action_recovery_actor,
                provenance_json=provenance_json,
                expected_previous_event_id=args.action_recovery_expected_event,
            )
    except (
        FileActionReconciliationConflict,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"ERROR action-recovery-record {exc}")
        return 2
    unsafe = reconciliation.classification in {
        "ambiguous",
        "impossible_to_check",
    }
    if args.action_recovery_json:
        print(
            json.dumps(
                {
                    "action_id": recorded.action_id,
                    "actor": recorded.actor,
                    "classification": recorded.classification,
                    "event_id": recorded.event_id,
                    "event_schema_version": recorded.event_schema_version,
                    "filesystem_mutation_authorized": False,
                    "kind": "file-action-reconciliation-event",
                    "previous_event_id": recorded.previous_event_id,
                    "recommendation": recorded.recommendation,
                    "reconciler_signature": recorded.reconciler_signature,
                    "recorded_ns": recorded.recorded_ns,
                    "sequence": recorded.sequence,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        print(
            f"ACTION_RECOVERY_RECORDED event={recorded.event_id} "
            f"action={recorded.action_id} sequence={recorded.sequence} "
            f"previous={recorded.previous_event_id or '-'} "
            f"classification={recorded.classification} actor={recorded.actor} "
            "filesystem_mutation_authorized=0"
        )
    return 2 if unsafe else 0


# endregion [02]


# region [04] Technical document catalog and organization


def _resolved_organization_root(args: argparse.Namespace) -> Path:
    if args.organization_root is not None:
        return args.organization_root
    from neocortex.documents.document_organization import default_organization_root

    return default_organization_root(
        args.state_directory / "framework.sqlite3",
        analysis_root=args.root,
    )


def run_document_catalog(args: argparse.Namespace) -> int:
    """Update classifications from existing content caches without inventory."""

    from neocortex.documents.document_catalog import update_document_catalog
    from neocortex.runtime.control.locking import FrameworkRunLock

    try:
        _capture_authorized_direct_state_policies(
            args.state_directory,
            lock=True,
            databases=(args.state_directory / "document_catalog.sqlite3",),
        )
        with FrameworkRunLock(args.state_directory / "framework.lock"):
            summaries = update_document_catalog(
                args.state_directory,
                taxonomy_path=args.document_taxonomy,
            )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR document-catalog {type(exc).__name__}: {exc}")
        return 2
    for summary in summaries:
        print(
            f"CATALOG source={summary.source_kind} candidates={summary.candidates} "
            f"classified={summary.classified} cache_hits={summary.cache_hits} "
            f"review={summary.review_required} errors={summary.errors} "
            f"stale={summary.stale_marked} source_stale={summary.source_stale} "
            f"source_missing={summary.source_missing}"
        )
    return 0 if not any(summary.errors for summary in summaries) else 2


def run_document_catalog_preview(args: argparse.Namespace) -> int:
    """List bounded active classifications without opening writable state."""

    from neocortex.documents.document_catalog import list_catalog_documents

    try:
        documents = list_catalog_documents(
            args.state_directory / "document_catalog.sqlite3",
            limit=args.catalog_preview,
            primary_kind=args.catalog_kind,
            authority=args.catalog_authority,
            organization=args.catalog_organization,
            client=args.catalog_client,
            project=args.catalog_project,
            workstream=args.catalog_workstream,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR catalog-preview {type(exc).__name__}: {exc}")
        return 2
    for document in documents:
        print(
            f"DOCUMENT source={document.source_kind} kind={document.primary_kind} "
            f"subtype={document.primary_subtype or '-'} "
            f"authority={document.primary_authority or '-'} "
            f"organization={document.primary_organization or '-'} "
            f"client={document.primary_client or '-'} "
            f"project={document.primary_project or '-'} "
            f"workstream={document.primary_workstream or '-'} "
            f"standards={','.join(document.standard_identifiers) or '-'} "
            f"topics={','.join(document.topics) or '-'} "
            f"equipment={','.join(document.equipment) or '-'} "
            f"activities={','.join(document.activities) or '-'} "
            f"confidence={document.confidence:.6f} "
            f"uncertainty={document.uncertainty} status={document.catalog_status} "
            f"path={document.path}"
        )
    return 0


def run_organization_plan(args: argparse.Namespace) -> int:
    """Refresh the catalog and persist destinations without moving files."""

    from neocortex.documents.document_catalog import update_document_catalog
    from neocortex.documents.document_organization import (
        capture_organization_input_scope,
        plan_document_organization,
    )
    from neocortex.safety.corpus_access import CorpusAccessPolicy, CorpusMutationGuard
    from neocortex.runtime.control.locking import FrameworkRunLock

    try:
        access_policy = CorpusAccessPolicy.capture("normal", args.root)
        internal_policy, protected_policy = _capture_authorized_direct_state_policies(
            args.state_directory,
            lock=True,
            databases=(args.state_directory / "document_catalog.sqlite3",),
        )
        internal_policy.validate_corpus_access(access_policy)
        protected_policy.validate_corpus_access(access_policy)
        organization_root = _resolved_organization_root(args)
        mutation_guard = CorpusMutationGuard(
            access_policy,
            internal_policy,
            protected_policy,
        )
        with FrameworkRunLock(args.state_directory / "framework.lock"):
            catalog_summaries = update_document_catalog(
                args.state_directory,
                taxonomy_path=args.document_taxonomy,
                source_root=args.root,
            )
            source_scope = capture_organization_input_scope(
                args.state_directory / "document_catalog.sqlite3",
                args.root,
            )
            summary = plan_document_organization(
                args.state_directory / "document_catalog.sqlite3",
                organization_root,
                source_scope=source_scope,
                min_confidence=args.organization_min_confidence,
                mutation_guard=mutation_guard,
            )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR organization-plan {type(exc).__name__}: {exc}")
        return 2
    for catalog in catalog_summaries:
        print(
            f"CATALOG source={catalog.source_kind} candidates={catalog.candidates} "
            f"classified={catalog.classified} cache_hits={catalog.cache_hits} "
            f"review={catalog.review_required} errors={catalog.errors} "
            f"source_stale={catalog.source_stale}"
        )
    print(
        f"ORGANIZATION_PLAN considered={summary.considered} "
        f"planned={summary.planned} review={summary.review_required} "
        f"blocked={summary.blocked} already_organized={summary.already_organized} "
        f"source_root={source_scope.root} scope_id={source_scope.scope_id} "
        f"organization_root={organization_root} executable=false"
    )
    return 0 if not any(item.errors for item in catalog_summaries) else 2


def run_organization_preview(args: argparse.Namespace) -> int:
    """List persisted plans through a read-only catalog connection."""

    from neocortex.documents.document_organization import list_organization_plans

    try:
        plans = list_organization_plans(
            args.state_directory / "document_catalog.sqlite3",
            limit=args.organization_preview,
            status=args.organization_preview_status,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR organization-preview {type(exc).__name__}: {exc}")
        return 2
    for plan in plans:
        print(
            f"ORGANIZATION plan_id={plan.plan_id} status={plan.status} "
            f"kind={plan.primary_kind} confidence={plan.confidence:.6f} "
            f"operation={getattr(plan, 'operation_kind', 'legacy_unscoped')} "
            f"executable={str(getattr(plan, 'executable', False)).lower()} "
            f"blockers={','.join(getattr(plan, 'blockers', ()))} "
            f"reason={plan.reason} source={plan.source_path} "
            f"destination={plan.destination_path or '-'} detail={plan.detail or '-'}"
        )
    return 0


def run_organization_apply(args: argparse.Namespace) -> int:
    """Apply only existing plans under the exclusive framework lock."""

    from neocortex.safety.corpus_access import CorpusAccessPolicy, CorpusMutationGuard
    from neocortex.documents.document_organization import apply_document_organization
    from neocortex.runtime.control.locking import FrameworkRunLock

    try:
        access_policy = CorpusAccessPolicy.capture("normal", args.root)
        internal_policy, protected_policy = _capture_authorized_direct_state_policies(
            args.state_directory,
            lock=True,
            databases=(args.state_directory / "document_catalog.sqlite3",),
        )
        internal_policy.validate_corpus_access(access_policy)
        protected_policy.validate_corpus_access(access_policy)
        organization_root = _resolved_organization_root(args)
        mutation_guard = CorpusMutationGuard(
            access_policy,
            internal_policy,
            protected_policy,
        )
        with FrameworkRunLock(args.state_directory / "framework.lock"):
            summary = apply_document_organization(
                args.state_directory / "document_catalog.sqlite3",
                organization_root,
                mutation_guard=mutation_guard,
                max_actions=args.organization_max_actions,
            )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR organization-apply {type(exc).__name__}: {exc}")
        return 2
    print(
        f"ORGANIZATION_APPLY selected={summary.selected} applied={summary.applied} "
        f"stale={summary.stale} blocked={summary.blocked} failed={summary.failed} "
        f"cache_synced={summary.cache_synced} "
        f"cache_pending={summary.cache_pending} "
        f"remaining={summary.remaining} "
        f"organization_root={organization_root}"
    )
    return (
        0
        if not (summary.stale or summary.blocked or summary.failed or summary.cache_pending)
        else 2
    )


# endregion [04]


# region [05] PDF commands


def run_pdf_search(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.pdf.pdf_derived_queries import search_pdf_state

    database_path = args.state_directory / "pdf.sqlite3"
    try:
        results = search_pdf_state(
            database_path,
            args.pdf_search,
            args.pdf_search_limit,
        )
    except (OSError, sqlite3.Error) as exc:
        print(f"ERROR pdf-search {exc}")
        return 2
    for result in results:
        page_number = int(result["page_number"]) + 1
        print(f"{result['path']} page={page_number} rank={result['rank']:.6f} {result['snippet']}")
    return 0


def run_pdf_layout_groups(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.pdf.pdf_derived_queries import list_layout_groups

    database_path = args.state_directory / "pdf.sqlite3"
    try:
        groups = list_layout_groups(database_path, args.pdf_layout_groups)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR pdf-layout-groups {exc}")
        return 2
    for group in groups:
        print(
            f"LAYOUT_GROUP key={group['group_key']} members={group['member_count']} "
            f"minimum_edge_score={group['minimum_edge_score']:.6f} "
            f"representative={group['representative_path']}"
        )
        for path in group["members"]:
            print(f"MEMBER {path}")
        if group["members_truncated"]:
            print("MEMBER ...")
    return 0


def run_pdf_doctor(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.pdf.pdf_admin import doctor_pdf_runtime

    report = doctor_pdf_runtime(
        ocr_mode=args.ocr,
        ocr_lang=args.ocr_lang,
        ocr_profile=args.ocr_profile,
        tesseract_cmd=args.tesseract_cmd,
        tessdata_dir=args.tessdata_dir,
    )
    for check in report.checks:
        print(f"{'OK' if check.ok else 'ERROR'} {check.name} {check.detail}")
    return 0 if report.ok else 2


def run_pdf_verify(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.pdf.pdf_admin import verify_pdf_state

    try:
        report = verify_pdf_state(args.state_directory / "pdf.sqlite3")
    except (FileNotFoundError, sqlite3.DatabaseError) as exc:
        print(f"ERROR pdf-state {exc}")
        return 2
    print(
        f"quick_check={report.quick_check} "
        f"foreign_key_errors={report.foreign_key_errors} "
        f"page_count_mismatches={report.page_count_mismatches} "
        f"page_error_mismatches={report.page_error_mismatches} "
        f"missing_fts_pages={report.missing_fts_pages} "
        f"orphan_fts_pages={report.orphan_fts_pages} "
        f"corrupt_page_payloads={report.corrupt_page_payloads} "
        f"missing_layout_pages={report.missing_layout_pages} "
        f"orphan_layout_pages={report.orphan_layout_pages} "
        f"corrupt_layout_payloads={report.corrupt_layout_payloads}"
    )
    return 0 if report.ok else 2


# endregion [05]


# region [06] DOCX commands


def run_docx_search(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.docx.route import search_docx_state

    try:
        results = search_docx_state(
            args.state_directory / "docx.sqlite3",
            args.docx_search,
            args.docx_search_limit,
        )
    except (OSError, sqlite3.Error) as exc:
        print(f"ERROR docx-search {exc}")
        return 2
    for result in results:
        print(f"{result['path']}\t{result['snippet']}")
    return 0


def run_docx_layout_groups(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.docx.route import list_docx_layout_groups

    try:
        groups = list_docx_layout_groups(
            args.state_directory / "docx.sqlite3",
            args.docx_layout_groups,
        )
    except (OSError, sqlite3.Error) as exc:
        print(f"ERROR docx-layout-groups {exc}")
        return 2
    for group in groups:
        print(
            f"members={group['member_count']} class={group['layout_class']} "
            f"representative={group['representative_path']}"
        )
    return 0


def run_docx_missing_pdf(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.docx.route import list_missing_pdf_counterparts

    try:
        paths = list_missing_pdf_counterparts(
            args.state_directory / "docx.sqlite3",
            args.docx_missing_pdf,
        )
    except (OSError, sqlite3.Error) as exc:
        print(f"ERROR docx-missing-pdf {exc}")
        return 2
    for path in paths:
        print(path)
    return 0


# endregion [06]


# region [07] Office commands


def run_office_search(args: argparse.Namespace) -> int:
    """Search indexed XLSX, PPTX and ODT text without extracting files."""

    from neocortex.capabilities.formats.office.state import search_office_state

    try:
        results = search_office_state(
            args.state_directory / "office.sqlite3",
            args.office_search,
            args.office_search_limit,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR office-search {exc}")
        return 2
    for result in results:
        print(
            f"OFFICE format={result['format']} rank={float(result['rank']):.6f} "
            f"title={result['title'] or '-'} author={result['author'] or '-'} "
            f"path={result['path']} snippet={result['snippet']}"
        )
    return 0


# endregion [07]
