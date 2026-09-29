"""Deterministic, non-mutating destination planning for technical documents."""
# region [00] Contexto del módulo
# Módulo: neocortex/document_organization_planning.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from .document_kind_destinations import (
    COMPACT_KIND_DIRECTORIES as _COMPACT_KIND_DIRECTORIES,
    REVIEW_ONLY_KINDS as _REVIEW_ONLY_KINDS,
)
import json
import os
import re
import sqlite3
import stat
import sys
import time
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .document_catalog_replay import catalog_sql_cancellation

if TYPE_CHECKING:
    from neocortex.runtime.control.cancellation import CancellationToken

from neocortex.platform.policy import sqlite_path_collation
from neocortex.platform.logical_filename import LogicalFilename
from .semantic_curation_gate import (
    FastOrganizationCurationGate,
    FastCurationPolicySource,
    validate_current_fast_curation_decision,
)

from neocortex.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)

from neocortex.workflow.actions.action_policy import protected_path_reason, validate_descendant_path
from neocortex.safety.corpus_access import CorpusMutationGuard, path_trees_intersect
from .document_catalog import document_catalog_database, initialize_document_catalog
from .document_organization_models import (
    ORGANIZATION_FILENAME_LIMIT,
    ORGANIZATION_PROGRESS_INTERVAL,
    OrganizationPlanSummary,
    _begin_organization_run,
    _complete_organization_run,
    _fail_organization_run,
)
from .document_organization_scope import OrganizationInputScope, assess_organization_resource
from .document_resource_binding import parse_resource_binding
from neocortex.safety.protected_content import ProtectedContentError
# endregion [01]

# region [02] Implementación

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)

_CLIENT_ACCOUNT_ORGANIZATIONS = frozenset({"ANDRITZ"})
_PATH_COLLATION = sqlite_path_collation()
ORGANIZATION_CORPUS_POLICY_SCHEMA = "neocortex.organization-corpus-policy/v1"
_ORGANIZATION_CANDIDATE_PAGE_SIZE = 128
_LINUX_ORGANIZATION_BACKEND_AVAILABLE = os.name == "posix" and sys.platform == "linux"


@dataclass(frozen=True, slots=True)
class OrganizationCorpusPolicy:
    """Compatibility metadata for legacy policy buckets.

    Abstentions are never routed to a physical ``General``, ``Sensible``,
    ``No_tecnico`` or ``Revision_pendiente`` destination.  The residual owner
    materializes them under ``Sin_clasificar/_MIME``; these fields remain in
    plan provenance for callers that still persist the old policy shape.
    """

    allow_general: bool = False
    allow_uncertain: bool = False
    allow_sensitive: bool = False
    allow_nontechnical: bool = False
    reversible: bool = True
    policy_version: str = ORGANIZATION_CORPUS_POLICY_SCHEMA

    def __post_init__(self) -> None:
        if self.policy_version != ORGANIZATION_CORPUS_POLICY_SCHEMA:
            raise ValueError("organization corpus policy is unsupported")
        for name in (
            "allow_general",
            "allow_uncertain",
            "allow_sensitive",
            "allow_nontechnical",
            "reversible",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"organization corpus policy {name} must be boolean")

    def allows(self, category: str) -> bool:
        return self.reversible and bool(
            {
                "general": self.allow_general,
                "uncertain": self.allow_uncertain,
                "sensitive": self.allow_sensitive,
                "nontechnical": self.allow_nontechnical,
            }.get(category, False)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.policy_version,
            "allow_general": self.allow_general,
            "allow_uncertain": self.allow_uncertain,
            "allow_sensitive": self.allow_sensitive,
            "allow_nontechnical": self.allow_nontechnical,
            "reversible": self.reversible,
        }


# Descriptive aliases allow callers to use either vocabulary without creating
# a second policy contract.
CorpusOrganizationPolicy = OrganizationCorpusPolicy
OrganizationPolicy = OrganizationCorpusPolicy


def _normalize_corpus_policy(
    policy: OrganizationCorpusPolicy | Mapping[str, object] | None,
) -> OrganizationCorpusPolicy:
    if policy is None:
        return OrganizationCorpusPolicy()
    if isinstance(policy, OrganizationCorpusPolicy):
        return policy
    if not isinstance(policy, Mapping):
        raise ValueError("organization corpus policy must be explicit")
    accepted = {
        "allow_general",
        "allow_uncertain",
        "allow_sensitive",
        "allow_nontechnical",
        "reversible",
        "allow_reversible",
        "allow_review",
    }
    unknown = set(policy) - accepted
    if unknown:
        raise ValueError(f"organization corpus policy has unknown fields: {sorted(unknown)}")
    values = dict(policy)
    if "allow_reversible" in values:
        if "reversible" in values and values["reversible"] != values["allow_reversible"]:
            raise ValueError("organization corpus policy reversible fields disagree")
        values["reversible"] = values["allow_reversible"]
    if values.get("allow_review") is True:
        for category in ("general", "uncertain", "sensitive", "nontechnical"):
            values[f"allow_{category}"] = True
    values.pop("allow_reversible", None)
    values.pop("allow_review", None)
    return OrganizationCorpusPolicy(**values)

_OOXML_DIRECTORY_MARKERS = (
    ("word/document.xml", "docx"),
    ("xl/workbook.xml", "xlsx"),
    ("ppt/presentation.xml", "pptx"),
)


def _canonical_regular_file(path: Path) -> bool:
    """Accept only a real file at an exact package marker path."""

    try:
        observed = path.lstat()
        return stat.S_ISREG(observed.st_mode) and path.resolve(strict=True) == path
    except OSError:
        return False


def _decompressed_ooxml_package(
    path: Path,
    scope_root: Path,
) -> tuple[Path, str] | None:
    """Find an exact OOXML directory package enclosing ``path``.

    A directory tree is only treated as a package when it contains the same
    exact marker names used by the ZIP detector.  A main part without
    ``[Content_Types].xml`` is retained as a partial package hypothesis so
    organization cannot move a member independently while the set is
    incomplete.
    """

    current = path.parent
    result: tuple[Path, str] | None = None
    while current.is_relative_to(scope_root):
        marker_paths = tuple(
            (current / relative, kind)
            for relative, kind in _OOXML_DIRECTORY_MARKERS
            if _canonical_regular_file(current / relative)
        )
        content_types = _canonical_regular_file(current / "[Content_Types].xml")
        if marker_paths or content_types:
            status = (
                "ambiguous"
                if len(marker_paths) > 1
                else "identified" if content_types and marker_paths else "partial"
            )
            result = (current, status)
            break
        if current == scope_root:
            break
        current = current.parent
    return result


def plan_document_organization(
    catalog_path: Path,
    organization_root: Path,
    *,
    source_scope: OrganizationInputScope,
    min_confidence: float = 0.72,
    progress: ProgressCallback | None = None,
    progress_operation: str = "framework",
    mutation_guard: CorpusMutationGuard | None = None,
    corpus_policy: OrganizationCorpusPolicy | Mapping[str, object] | None = None,
    organization_policy: OrganizationCorpusPolicy | Mapping[str, object] | None = None,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
    curation_policy_bundle: FastCurationPolicySource | None = None,
    cancellation: CancellationToken | None = None,
) -> OrganizationPlanSummary:
    """Persist proposed destinations; never create directories or move files."""

    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    if not isinstance(source_scope, OrganizationInputScope):
        raise ValueError("organization_input_scope_required")
    if corpus_policy is not None and organization_policy is not None:
        raise ValueError("organization corpus policy was supplied twice")
    if fast_curation_policy_bundle is not None and curation_policy_bundle is not None:
        raise ValueError("Fast Curation policy bundle was supplied twice")
    resolved_curation_bundle = (
        fast_curation_policy_bundle
        if fast_curation_policy_bundle is not None
        else curation_policy_bundle
    )
    resolved_policy = _normalize_corpus_policy(
        corpus_policy if corpus_policy is not None else organization_policy
    )
    source_scope.verify()
    root = Path(os.path.abspath(organization_root.expanduser()))
    if mutation_guard is not None:
        mutation_guard.require_paths_allowed(root)
    root_reason = protected_path_reason(root, check_attributes=False)
    if root_reason is not None:
        raise ValueError(f"organization root is protected: {root_reason}")
    _reject_state_destination(catalog_path, root)
    initialize_document_catalog(catalog_path)
    with document_catalog_database(catalog_path) as connection, catalog_sql_cancellation(connection, cancellation):
        source_scope.verify(connection)
        run_id = _begin_organization_run(connection, "plan", root, source_scope=source_scope)
        considered = planned = review = blocked = organized = executable = 0
        excluded_out_of_scope = unresolved_scope = excluded_components = 0
        try:
            # Publish one complete plan generation.  An interrupted rebuild must
            # not supersede the previous proposals or expose half a new scope.
            connection.execute("BEGIN IMMEDIATE")
            source_scope.verify(connection)
            # Freeze membership and SQLite's path ordering without retaining
            # every classification payload in Python. The TEMP table belongs
            # only to this writer connection and this transaction.
            connection.execute(
                """CREATE TEMP TABLE organization_plan_candidates(
                ordinal INTEGER PRIMARY KEY,source_kind TEXT NOT NULL,
                file_key TEXT NOT NULL)"""
            )
            total = 0
            for candidate in connection.execute(
                """SELECT source_kind,file_key,path,resource_binding_json
                FROM documents WHERE active=1
                ORDER BY path,source_kind,file_key"""
            ):
                if cancellation is not None:
                    cancellation.checkpoint()
                assessment = assess_organization_resource(candidate, source_scope)
                if assessment.included:
                    metadata = (assessment.binding or {}).get("representation_metadata", {})
                    if (
                        metadata.get("document_role") == "document_component"
                        and metadata.get("independently_organizable") is False
                    ):
                        excluded_components += 1
                    elif (
                        (assessment.binding or {}).get("representation_kind") == "physical_file"
                        and _decompressed_ooxml_package(
                            Path(str(candidate["path"])),
                            source_scope.root,
                        )
                        is not None
                    ):
                        # The directory itself is the logical owner.  Until a
                        # directory-package representation exists, retaining
                        # every member as a component is safer than proposing
                        # independent XML moves that split the package.
                        excluded_components += 1
                    else:
                        total += 1
                        connection.execute(
                            "INSERT INTO temp.organization_plan_candidates VALUES(?,?,?)",
                            (total, candidate["source_kind"], candidate["file_key"]),
                        )
                elif assessment.reason == "source_outside_scope":
                    excluded_out_of_scope += 1
                else:
                    unresolved_scope += 1
            managed_locations = {
                (
                    str(source_kind),
                    str(file_key),
                    os.path.normcase(os.path.abspath(str(destination_path))),
                )
                for source_kind, file_key, destination_path in connection.execute(
                    """SELECT source_kind,file_key,destination_path
                    FROM organization_plans
                    WHERE organization_root=? AND status='applied'
                    AND destination_path IS NOT NULL""",
                    (str(root),),
                )
            }
            _emit_organization_plan_progress(
                progress,
                operation=progress_operation,
                completed=0,
                total=total,
                planned=0,
                review=0,
                blocked=0,
                organized=0,
            )
            for row in _iter_organization_plan_candidates(connection, total):
                if cancellation is not None:
                    cancellation.checkpoint()
                considered += 1
                status = _plan_catalog_document(
                    connection,
                    run_id,
                    row,
                    root,
                    min_confidence=min_confidence,
                    managed_locations=managed_locations,
                    mutation_guard=mutation_guard,
                    source_scope=source_scope,
                    corpus_policy=resolved_policy,
                    fast_curation_policy_bundle=resolved_curation_bundle,
                )
                if status == "planned":
                    planned += 1
                elif status == "review":
                    review += 1
                elif status == "blocked":
                    blocked += 1
                elif status == "already_organized":
                    organized += 1
                if considered % ORGANIZATION_PROGRESS_INTERVAL == 0 or considered == total:
                    _emit_organization_plan_progress(
                        progress,
                        operation=progress_operation,
                        completed=considered,
                        total=total,
                        planned=planned,
                        review=review,
                        blocked=blocked,
                        organized=organized,
                    )
            source_scope.verify(connection)
            executable = int(
                connection.execute(
                    """SELECT COUNT(*) FROM organization_plans
                    WHERE catalog_run_id=? AND organization_root=? AND executable=1""",
                    (run_id, str(root)),
                ).fetchone()[0]
            )
            summary = OrganizationPlanSummary(
                catalog_run_id=run_id,
                considered=considered,
                planned=planned,
                review_required=review,
                blocked=blocked,
                already_organized=organized,
                excluded_out_of_scope=excluded_out_of_scope,
                unresolved_scope=unresolved_scope,
                excluded_components=excluded_components,
                source_scope_id=source_scope.scope_id,
                source_root=str(source_scope.root),
                executable=executable,
            )
            _complete_organization_run(connection, run_id, summary)
            _emit_organization_plan_progress(
                progress,
                operation=progress_operation,
                completed=considered,
                total=total,
                planned=planned,
                review=review,
                blocked=blocked,
                organized=organized,
                finished=True,
            )
            return summary
        except BaseException as exc:
            # Recovery writes must survive an interrupted SQL statement.
            connection.set_progress_handler(None, 0)
            connection.rollback()
            _fail_organization_run(connection, run_id, exc)
            raise


def _iter_organization_plan_candidates(
    connection: sqlite3.Connection, total: int,
) -> Iterator[sqlite3.Row]:
    """Read one frozen key page before writes, with no per-document SELECT."""

    for after in range(0, total, _ORGANIZATION_CANDIDATE_PAGE_SIZE):
        # CROSS JOIN keeps the bounded ordinal range outside the PK lookups;
        # payload size and unrelated catalog rows cannot amplify the page.
        page = connection.execute(
            """SELECT document.* FROM temp.organization_plan_candidates AS selected
            CROSS JOIN documents AS document
            ON document.source_kind=selected.source_kind
            AND document.file_key=selected.file_key
            WHERE selected.ordinal>? AND selected.ordinal<=?
            ORDER BY selected.ordinal""",
            (after, after + _ORGANIZATION_CANDIDATE_PAGE_SIZE),
        ).fetchall()
        yield from page
        del page


def _plan_catalog_document(
    connection: sqlite3.Connection,
    run_id: int,
    row: sqlite3.Row,
    root: Path,
    *,
    min_confidence: float,
    managed_locations: set[tuple[str, str, str]],
    mutation_guard: CorpusMutationGuard | None,
    source_scope: OrganizationInputScope,
    corpus_policy: OrganizationCorpusPolicy,
    fast_curation_policy_bundle: FastCurationPolicySource | None,
) -> str:
    assessment = assess_organization_resource(row, source_scope)
    if not assessment.included:
        return _persist_catalog_plan(
            connection,
            run_id,
            row,
            root,
            None,
            "blocked",
            assessment.reason or "resource_scope_unverified",
            mutation_guard=mutation_guard,
            source_scope=source_scope,
            corpus_policy=corpus_policy,
            binding=assessment.binding,
            fast_curation_policy_bundle=fast_curation_policy_bundle,
        )
    binding = assessment.binding
    if binding is None:  # pragma: no cover - included assessments carry a binding
        raise ValueError("organization resource binding is missing after scope assessment")
    managed_source = (
        binding["representation_kind"] == "physical_file"
        and (
            str(row["source_kind"]),
            str(row["file_key"]),
            os.path.normcase(os.path.abspath(str(row["path"]))),
        )
        in managed_locations
    )
    protected_reason = _protected_content_reason(
        mutation_guard,
        Path(binding["physical_anchor_path"]),
    )
    if protected_reason is not None:
        return _persist_catalog_plan(
            connection,
            run_id,
            row,
            root,
            None,
            "blocked",
            protected_reason,
            mutation_guard=mutation_guard,
            source_scope=source_scope,
            corpus_policy=corpus_policy,
            binding=binding,
            fast_curation_policy_bundle=fast_curation_policy_bundle,
        )
    destination, status, reason = _proposed_destination(
        row,
        root,
        min_confidence=min_confidence,
        managed_source=managed_source,
        corpus_policy=corpus_policy,
        connection=connection,
        fast_curation_policy_bundle=fast_curation_policy_bundle,
    )
    if binding["representation_kind"] != "physical_file":
        destination = None
        status, reason = "review", "virtual_resource_requires_logical_organization"
    metadata = binding.get("representation_metadata", {})
    if (
        metadata.get("document_role") == "document_component"
        and metadata.get("independently_organizable") is False
    ):
        destination = None
        status, reason = "review", "document_component_not_independently_organizable"
    if status == "planned" and destination is not None:
        protected_reason = _protected_content_reason(mutation_guard, destination)
        if protected_reason is not None:
            destination, status, reason = None, "blocked", protected_reason
        else:
            destination, status, reason = _resolve_initial_plan_destination(
                connection,
                row,
                destination,
                status,
                reason,
                mutation_guard=mutation_guard,
            )
    return _persist_catalog_plan(
        connection,
        run_id,
        row,
        root,
        destination,
        status,
        reason,
        mutation_guard=mutation_guard,
        source_scope=source_scope,
        corpus_policy=corpus_policy,
        binding=binding,
        fast_curation_policy_bundle=fast_curation_policy_bundle,
    )


def _resolve_initial_plan_destination(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    destination: Path,
    status: str,
    reason: str,
    *,
    mutation_guard: CorpusMutationGuard | None,
) -> tuple[Path | None, str, str]:
    if _same_path(destination, Path(row["path"])):
        return destination, "already_organized", "source_already_at_destination"
    # A previous collision resolution may already have placed this owner at
    # its identity-qualified destination.  Do not turn that stable path back
    # into a basename collision merely because the semantic filename is
    # recomputed from the current catalog row.  The helper only accepts a
    # destination generated from this row's identity and whose current stat
    # still matches the catalog snapshot; equal-looking names are never
    # sufficient.
    current_destination = _current_owner_disambiguated_destination(row, destination)
    if current_destination is not None and _plan_destination_available(
        connection, row, current_destination
    ):
        return current_destination, "already_organized", "source_already_at_destination"
    if not os.path.lexists(destination):
        return destination, status, reason
    resolved, disambiguated = _resolve_plan_destination(
        connection,
        row,
        destination,
    )
    if resolved is None:
        return (
            destination,
            "blocked",
            "destination_collision_could_not_be_disambiguated",
        )
    protected_reason = _protected_content_reason(mutation_guard, resolved)
    if protected_reason is not None:
        return None, "blocked", protected_reason
    if disambiguated:
        reason = "fast_curation_classified_with_identity_disambiguation"
    return resolved, status, reason


def _persist_catalog_plan(
    connection: sqlite3.Connection,
    run_id: int,
    row: sqlite3.Row,
    root: Path,
    destination: Path | None,
    status: str,
    reason: str,
    *,
    mutation_guard: CorpusMutationGuard | None,
    source_scope: OrganizationInputScope,
    corpus_policy: OrganizationCorpusPolicy,
    binding: Mapping[str, Any] | None = None,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
) -> str:
    try:
        _insert_plan(
            connection,
            run_id,
            row,
            root,
            destination,
            status,
            reason,
            source_scope=source_scope,
            corpus_policy=corpus_policy,
            binding=binding,
            fast_curation_policy_bundle=fast_curation_policy_bundle,
        )
        return status
    except sqlite3.IntegrityError:
        resolved = None
        if status == "planned" and destination is not None:
            resolved, _disambiguated = _resolve_plan_destination(
                connection,
                row,
                destination,
            )
        if resolved is not None:
            protected_reason = _protected_content_reason(mutation_guard, resolved)
            if protected_reason is not None:
                _insert_plan(
                    connection,
                    run_id,
                    row,
                    root,
                    None,
                    "blocked",
                    protected_reason,
                    source_scope=source_scope,
                    corpus_policy=corpus_policy,
                    binding=binding,
                    fast_curation_policy_bundle=fast_curation_policy_bundle,
                )
                return "blocked"
            _insert_plan(
                connection,
                run_id,
                row,
                root,
                resolved,
                status,
                "fast_curation_classified_with_identity_disambiguation",
                source_scope=source_scope,
                corpus_policy=corpus_policy,
                binding=binding,
                fast_curation_policy_bundle=fast_curation_policy_bundle,
            )
            return status
        _insert_plan(
            connection,
            run_id,
            row,
            root,
            destination,
            "blocked",
            "destination_conflict_with_another_plan",
            source_scope=source_scope,
            corpus_policy=corpus_policy,
            binding=binding,
            fast_curation_policy_bundle=fast_curation_policy_bundle,
        )
        return "blocked"


def _protected_content_reason(
    mutation_guard: CorpusMutationGuard | None,
    *paths: Path,
) -> str | None:
    """Classify only protected-content denials as per-document blocks."""

    if mutation_guard is None:
        return None
    try:
        mutation_guard.require_paths_allowed(*paths)
    except ProtectedContentError as exc:
        return exc.reason_code
    return None


def _proposed_destination(
    row: sqlite3.Row,
    root: Path,
    *,
    min_confidence: float,
    managed_source: bool,
    corpus_policy: OrganizationCorpusPolicy | None = None,
    connection: sqlite3.Connection | None = None,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
) -> tuple[Path | None, str, str]:
    """Choose a physical destination only for calibrated high-confidence evidence.

    ``review``/unknown/out-of-taxonomy rows intentionally return no physical
    destination.  The residual materializer owns ``Sin_clasificar/_MIME``;
    keeping that decision here prevents legacy policy buckets from becoming a
    second physical organization contract.
    """

    def review(reason: str) -> tuple[Path | None, str, str]:
        return None, "review", reason

    if str(row["catalog_status"]) == "error":
        return review("classification_error")
    if connection is None:
        return review("fast_curation_current_decision_unavailable")
    gate = validate_current_fast_curation_decision(
        connection,
        source_kind=str(row["source_kind"]),
        file_key=str(row["file_key"]),
        policy_bundle=fast_curation_policy_bundle,
        expected_binding=parse_resource_binding(row["resource_binding_json"]),
        expected_path=str(row["path"]),
        expected_identity=(
            row["volume_id"],
            row["file_id"],
            row["size"],
            row["mtime_ns"],
            row["birthtime_ns"],
        ),
    )
    if not gate.eligible:
        return review(gate.reason)
    try:
        classification = json.loads(str(row["classification_json"]))
    except (TypeError, ValueError):
        classification = {}
    if not isinstance(classification, dict):
        classification = {}
    # Catalog taxonomy fields are auxiliary context only.  The current Fast
    # Curation document-kind label owns the physical directory decision.
    assert gate.document_kind is not None
    kind = gate.document_kind
    authority = str(row["primary_authority"] or "")
    organization = str(row["primary_organization"] or "")
    client = str(row["primary_client"] or "")
    project = str(row["primary_project"] or "")
    workstream = str(row["primary_workstream"] or "")
    filename = _proposed_filename(row)

    parts: tuple[str, ...]
    if kind == "normativa":
        if not authority:
            return review("normative_document_without_authority")
        parts = ("Normativa", _safe_segment(authority))
    else:
        review_reasons = {
            "audio_transcrito": "generic_audio_requires_review",
            "expediente_personal": "personal_or_sensitive_document_requires_review",
            "instruccion_cuenta_bancaria": ("financial_or_sensitive_document_requires_review"),
            "otro": "document_kind_not_safe_for_automatic_organization",
            "registro_log": "log_record_requires_organization_policy",
            "reporte_inventario_archivo": ("generated_file_inventory_report_requires_review"),
        }
        if kind in _REVIEW_ONLY_KINDS:
            return review(review_reasons[kind])
        if kind == "formato_empresa" and not organization:
            return review("company_form_without_identified_company")
        if kind == "documento_empresa" and not organization:
            return review("company_document_without_identified_company")
        classified_parts = _COMPACT_KIND_DIRECTORIES.get(kind)
        if classified_parts is None:
            return review("document_kind_not_safe_for_automatic_organization")
        if str(row["source_kind"]) == "audio":
            classified_parts = ("Audio", *classified_parts)

        routing_client = client
        if not routing_client and organization in _CLIENT_ACCOUNT_ORGANIZATIONS:
            routing_client = organization
        if routing_client:
            parts = _client_destination_parts(
                client=routing_client,
                project=project,
                workstream=workstream,
                classified_parts=classified_parts,
            )
        elif organization:
            parts = ("Empresas", _safe_segment(organization), *classified_parts)
        else:
            parts = classified_parts

    destination = root.joinpath(*parts, filename)
    _validate_destination(root, destination)
    # The high-precision gate above is the only path to a physical semantic
    # destination.  Retain a stable reason for provenance without inventing a
    # second numeric threshold.
    return destination, "planned", "fast_curation_classified_current"


def _client_destination_parts(
    *,
    client: str,
    project: str,
    workstream: str,
    classified_parts: tuple[str, ...],
) -> tuple[str, ...]:
    """Keep client/project context while limiting semantic nesting."""

    base = (
        "Clientes",
        _safe_segment(client),
        _safe_segment(project or "General"),
    )
    compact_contexts = {
        "control_presion_unidades": ("Presion_de_unidades",),
        "embarques_hcn": ("Embarques_HCN",),
        "muestreo_aceite_transformadores": ("Analisis_de_aceite",),
    }
    if context := compact_contexts.get(workstream):
        return (*base, *context)
    if workstream == "modernizacion_repotenciacion":
        return (*base, "Modernizacion_y_repotenciacion", *classified_parts)
    return (*base, *classified_parts)


def _resolve_plan_destination(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    requested: Path,
) -> tuple[Path | None, bool]:
    """Preserve both same-named documents using a stable filesystem identity."""

    current = _organization_row_path(row)
    if (
        _same_path(requested, current)
        and _current_owner_snapshot_matches(row, current)
        and _plan_destination_available(connection, row, current)
    ):
        return current, False
    current_destination = _current_owner_disambiguated_destination(row, requested)
    if current_destination is not None and _plan_destination_available(
        connection, row, current_destination
    ):
        return current_destination, True
    for collision_index in range(1, 1001):
        candidate = _identity_disambiguated_destination(
            requested,
            row,
            collision_index,
        )
        if _plan_destination_available(connection, row, candidate):
            return candidate, True
    return None, False


def _identity_disambiguated_destination(
    requested: Path,
    row: sqlite3.Row,
    collision_index: int,
) -> Path:
    logical = LogicalFilename.parse(requested)
    extension = logical.extension
    stem = logical.stem
    identity = "_".join(
        (
            _safe_segment(str(row["source_kind"])).replace(" ", "_"),
            _compact_identity(row["volume_id"], 8),
            _compact_identity(row["file_id"], 16),
        )
    )
    counter = "" if collision_index == 1 else f"_{collision_index}"
    suffix = f"__{identity}{counter}{extension}"
    stem_limit = max(1, ORGANIZATION_FILENAME_LIMIT - len(suffix))
    return requested.with_name(f"{stem[:stem_limit].rstrip(' .')}{suffix}")


def _compact_identity(value: object, width: int) -> str:
    """Format filesystem identity as bounded hexadecimal, never a content hash."""

    try:
        number = int(str(value), 10)
    except ValueError:
        clean = re.sub(r"[^A-Za-z0-9]", "", str(value))
        return (clean[-width:] or "0").rjust(width, "0")
    mask = (1 << (width * 4)) - 1
    return f"{number & mask:0{width}x}"


def _plan_destination_available(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    destination: Path,
) -> bool:
    catalog_conflict = connection.execute(
        f"""SELECT 1 FROM documents WHERE active=1 AND path=? COLLATE {_PATH_COLLATION}
        AND NOT(source_kind=? AND file_key=?) LIMIT 1""",
        (str(destination), row["source_kind"], row["file_key"]),
    ).fetchone()
    if catalog_conflict is not None:
        return False
    plan_conflict = connection.execute(
        f"""SELECT 1 FROM organization_plans
        WHERE destination_path=? COLLATE {_PATH_COLLATION}
        AND status IN (
            'planned','applying','moved_cache_pending','recovery_required'
        ) AND NOT (source_kind=? AND file_key=? AND status='planned') LIMIT 1""",
        (str(destination), row["source_kind"], row["file_key"]),
    ).fetchone()
    if plan_conflict is not None:
        return False
    # The current owner is allowed to retain its exact identity-qualified
    # destination, but only after foreign catalog/plan owners have been
    # checked.  Otherwise an active foreign plan could be silently ignored.
    if _same_path(destination, _organization_row_path(row)) and _current_owner_snapshot_matches(
        row, destination
    ):
        return True
    return not os.path.lexists(destination)


def _current_owner_disambiguated_destination(
    row: sqlite3.Row,
    requested: Path,
) -> Path | None:
    """Return the owner's existing identity-qualified path, if still exact.

    ``requested`` is the semantic destination calculated for the current
    catalog row.  An applied move can leave the source row's path carrying the
    stable identity suffix that was added during an earlier collision.  Check
    all bounded suffixes rather than parsing names: this keeps the comparison
    tied to the source identity and avoids accepting an unrelated basename.
    """

    current = _organization_row_path(row)
    if not _current_owner_snapshot_matches(row, current):
        return None
    for collision_index in range(1, 1001):
        candidate = _identity_disambiguated_destination(requested, row, collision_index)
        if _same_path(candidate, current):
            return current
    return None


def _organization_row_path(row: sqlite3.Row) -> Path:
    """Read the current locator from either a document or plan row."""

    key = "path" if "path" in row.keys() else "source_path"
    return Path(str(row[key]))


def _current_owner_snapshot_matches(row: sqlite3.Row, path: Path) -> bool:
    """Verify a candidate is this row's regular file, not just its name."""

    try:
        observed = path.lstat()
        if path.resolve(strict=True) != path:
            return False
        expected_identity = (int(row["volume_id"]), int(row["file_id"]))
        expected_size = int(row["size"])
        expected_mtime = int(row["mtime_ns"])
        expected_birthtime = int(row["birthtime_ns"])
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        return False
    observed_birthtime = getattr(observed, "st_birthtime_ns", -1)
    birthtime_matches = observed_birthtime == expected_birthtime or (
        observed_birthtime == -1
        and expected_birthtime >= 0
        and expected_birthtime == observed.st_ctime_ns
    )
    return (
        (observed.st_dev, observed.st_ino) == expected_identity
        and observed.st_size == expected_size
        and observed.st_mtime_ns == expected_mtime
        and birthtime_matches
    )


def _emit_organization_plan_progress(
    progress: ProgressCallback | None,
    *,
    operation: str,
    completed: int,
    total: int,
    planned: int,
    review: int,
    blocked: int,
    organized: int,
    finished: bool = False,
) -> None:
    emit_progress(
        progress,
        ProgressEvent(
            operation,
            "organization-plan",
            (
                "Organización técnica planificada"
                if finished
                else "Planificando organización técnica"
            ),
            completed,
            total,
            "documentos",
            finished,
            (
                ProgressMetric("planned", planned),
                ProgressMetric("review", review),
                ProgressMetric("blocked", blocked),
                ProgressMetric("already_organized", organized),
                ProgressMetric("remaining", max(0, total - completed)),
            ),
        ),
    )


def _insert_plan(
    connection: sqlite3.Connection,
    catalog_run_id: int,
    row: sqlite3.Row,
    root: Path,
    destination: Path | None,
    status: str,
    reason: str,
    *,
    source_scope: OrganizationInputScope,
    corpus_policy: OrganizationCorpusPolicy,
    binding: Mapping[str, Any] | None = None,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
) -> None:
    if binding is None:
        binding = parse_resource_binding(row["resource_binding_json"])
    try:
        classification = json.loads(str(row["classification_json"]))
    except (TypeError, ValueError):
        classification = {}
    if not isinstance(classification, dict):
        classification = {}
    representation = binding["representation_kind"]
    virtual = representation != "physical_file"
    operation = "logical_organization" if virtual else "move_physical"
    eligibility = (
        "logical_only"
        if virtual
        else (
            "eligible"
            if status == "planned"
            else "blocked"
        )
    )
    source_scope_contains_destination = False
    if destination is not None:
        try:
            source_scope_contains_destination = Path(destination).is_relative_to(source_scope.root)
        except (TypeError, ValueError):
            source_scope_contains_destination = False
    executable = bool(
        _LINUX_ORGANIZATION_BACKEND_AVAILABLE
        and representation == "physical_file"
        and status == "planned"
        and eligibility == "eligible"
        and destination is not None
        and source_scope_contains_destination
    )
    blockers: list[str] = []
    if not executable:
        blockers.append("backend_unavailable")
        if status == "planned" and eligibility == "eligible":
            blockers.append("authorization_required")
    if virtual:
        blockers.append("virtual_resource_requires_materialization")
    representation_metadata = binding.get("representation_metadata", {})
    if (
        representation_metadata.get("document_role") == "document_component"
        and representation_metadata.get("independently_organizable") is False
    ):
        blockers.append("document_component_not_independently_organizable")
    if eligibility == "blocked":
        blockers.append(reason)
    # A logical location is classification evidence, not a writable file path.
    logical_destination = None
    if virtual:
        proposed, _, _ = _proposed_destination(
            row,
            root,
            min_confidence=0.0,
            managed_source=False,
            corpus_policy=corpus_policy,
            connection=connection,
            fast_curation_policy_bundle=fast_curation_policy_bundle,
        )
        logical_destination = None if proposed is None else str(proposed)
    try:
        fast_gate = validate_current_fast_curation_decision(
            connection,
            source_kind=str(row["source_kind"]),
            file_key=str(row["file_key"]),
            policy_bundle=fast_curation_policy_bundle,
            expected_binding=binding,
            expected_path=str(row["path"]),
        )
    except (OSError, TypeError, ValueError) as exc:
        fast_gate = FastOrganizationCurationGate(
            False, f"fast_curation_gate_error:{type(exc).__name__}"
        )
    decision = fast_gate.decision
    fast_decision_evidence: dict[str, object] = {
        "gate_status": fast_gate.reason,
        "eligible": fast_gate.eligible,
        "document_kind": fast_gate.document_kind,
    }
    if decision is not None:
        fast_decision_evidence.update(
            {
                "source_kind": decision.source_kind,
                "file_key": decision.file_key,
                "input_signature": decision.input_signature,
                "representation_version": decision.representation_version,
                "model_signature": decision.model_signature,
                "ontology_version": decision.ontology_version,
                "prototype_version": decision.prototype_version,
                "policy_version": decision.policy_version,
                "calibration_version": decision.calibration_version,
                "decision": decision.decision,
                "top1_label": decision.top1_label,
                "top1_score": decision.top1_score,
                "top2_label": decision.top2_label,
                "top2_score": decision.top2_score,
                "margin": decision.margin,
                "source_binding": dict(decision.source_binding),
            }
        )
    evidence = json.dumps(
        {
            "primary_kind": row["primary_kind"],
            "classification_status": row["catalog_status"],
            "classification_score_kind": classification.get("confidence_kind", "catalog_auxiliary"),
            "taxonomy_status": classification.get("taxonomy_status", "unverified"),
            "suggested_logical_location": logical_destination,
            "primary_subtype": row["primary_subtype"],
            "primary_authority": row["primary_authority"],
            "primary_organization": row["primary_organization"],
            "primary_client": row["primary_client"],
            "primary_project": row["primary_project"],
            "primary_workstream": row["primary_workstream"],
            "standard_references": json.loads(row["standard_references_json"]),
            "clients": json.loads(row["clients_json"]),
            "projects": json.loads(row["projects_json"]),
            "workstreams": json.loads(row["workstreams_json"]),
            "topics": json.loads(row["topics_json"]),
            "equipment": json.loads(row["equipment_json"]),
            "activities": json.loads(row["activities_json"]),
            "uncertainty": row["uncertainty"],
            "fast_curation_decision": fast_decision_evidence,
            "corpus_policy": corpus_policy.to_dict(),
            "organization_decision_owner": "fast_curation_current_decision",
            "organization_decision_status": (
                "accepted" if status in {"planned", "already_organized"} else "abstained"
            ),
            "organization_decision_reason": reason,
            "reversible": corpus_policy.reversible,
            "organization_backend": (
                "posix-link-unlink-no-replace-v1" if executable else None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    prior = connection.execute(
        """SELECT * FROM organization_plans WHERE source_kind=? AND file_key=?
        AND organization_root=? AND status IN ('planned','review','blocked','already_organized')
        ORDER BY plan_id DESC LIMIT 1""",
        (row["source_kind"], row["file_key"], str(root)),
    ).fetchone()
    destination_value = None if destination is None else str(destination)
    blockers_json = json.dumps(sorted(set(blockers)), separators=(",", ":"))
    if prior is not None and all(
        (
            prior["source_scope_id"] == source_scope.scope_id,
            prior["source_scope_json"] == source_scope.serialized,
            prior["resource_binding_json"] == row["resource_binding_json"],
            prior["classifier_signature"] == row["classifier_signature"],
            prior["primary_kind"] == row["primary_kind"],
            prior["confidence"] == row["confidence"],
            prior["representation_kind"] == representation,
            prior["operation_kind"] == operation,
            prior["eligibility_status"] == eligibility,
            prior["destination_path"] == destination_value,
            prior["status"] == status,
            prior["reason"] == reason,
            prior["evidence_json"] == evidence,
            prior["blockers_json"] == blockers_json,
            not prior["executable"],
        )
    ):
        return
    connection.execute(
        """UPDATE organization_plans SET status='superseded',completed_ns=?,
        detail='replaced by a complete scoped organization plan'
        WHERE source_kind=? AND file_key=? AND organization_root=?
        AND status IN ('planned','review','blocked','already_organized')""",
        (time.time_ns(), row["source_kind"], row["file_key"], str(root)),
    )
    connection.execute(
        """INSERT INTO organization_plans(
        catalog_run_id,source_kind,file_key,source_path,destination_path,
        organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
        classifier_signature,primary_kind,confidence,status,reason,evidence_json,
        planned_ns,source_scope_json,source_scope_id,resource_binding_json,
        representation_kind,operation_kind,eligibility_status,executable,blockers_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            catalog_run_id,
            row["source_kind"],
            row["file_key"],
            row["path"],
            destination_value,
            str(root),
            row["volume_id"],
            row["file_id"],
            row["size"],
            row["mtime_ns"],
            row["birthtime_ns"],
            row["classifier_signature"],
            row["primary_kind"],
            row["confidence"],
            status,
            reason,
            evidence,
            time.time_ns(),
            source_scope.serialized,
            source_scope.scope_id,
            row["resource_binding_json"],
            representation,
            operation,
            eligibility,
            int(executable),
            blockers_json,
        ),
    )


_LOW_QUALITY_FILENAME_PATTERNS = (
    re.compile(r"(?i)^x(?:_|$)"),
    re.compile(r"(?i)^[^~]{1,6}~[0-9]$"),
    re.compile(r"(?i)~[0-9a-f]{6,}"),
    re.compile(r"(?i)--[0-9a-f]{8,}"),
    re.compile(r"(?i)^(?:document|documento|service|archivo)[a-z ]*~[0-9a-f]{6,}$"),
    re.compile(r"(?i)^[df]_[0-9a-f]{5,}$"),
    re.compile(r"(?i)^f\d{5,}$"),
    re.compile(r"(?i)^certcal_\d{8}_[0-9a-f]{6,}(?:__?[0-9a-f]{6,})?$"),
    re.compile(r"(?i)^[0-9a-f]{32,}__"),
    re.compile(r"(?i)__[a-z]+_\d{6,}_\d{6,}(?:_\d+)?$"),
)
_SEMANTIC_RENAME_KINDS = frozenset(
    {
        "accion_correctiva_preventiva",
        "audio_transcrito",
        "certificado_calibracion",
        "comprobante_viaje",
        "correspondencia",
        "credencial_visitante",
        "hoja_asignacion_proyecto",
        "manual_sistema_gestion",
        "normativa",
        "programa_gestion_ambiental",
        "programa_seguridad_salud",
        "registro_auditores",
        "registro_entrega_epp",
        "registro_fotografico",
        "registro_incidencias",
        "registro_mediciones",
        "reporte_actividades",
    }
)


def _proposed_filename(row: sqlite3.Row) -> str:
    source = Path(str(row["path"]))
    logical = LogicalFilename.parse(source)
    original_stem = logical.stem
    try:
        classification = json.loads(str(row["classification_json"]))
    except (TypeError, ValueError):
        return source.name
    if not isinstance(classification, dict):
        return source.name
    suggested = classification.get("suggested_stem")
    if not isinstance(suggested, str) or not suggested.strip():
        return source.name
    primary_kind = str(classification.get("primary_kind") or "")
    if primary_kind not in _SEMANTIC_RENAME_KINDS and not _filename_needs_semantic_rename(
        original_stem
    ):
        return source.name
    safe_stem = _safe_filename_stem(suggested, extension=logical.extension)
    if os.path.normcase(safe_stem) == os.path.normcase(original_stem):
        return source.name
    return f"{safe_stem}{logical.extension}"


def _filename_needs_semantic_rename(stem: str) -> bool:
    normalized = unicodedata.normalize("NFKC", stem).strip()
    if "�" in normalized or len(normalized) > 180:
        return True
    return any(pattern.search(normalized) is not None for pattern in _LOW_QUALITY_FILENAME_PATTERNS)


def _safe_filename_stem(value: str, *, extension: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalized)
    clean = re.sub(r"\s+", " ", clean).rstrip(" .")
    if not clean:
        clean = "Documento tecnico"
    if clean.upper() in _WINDOWS_RESERVED_NAMES:
        clean = f"_{clean}"
    limit = max(1, ORGANIZATION_FILENAME_LIMIT - len(extension))
    return clean[:limit].rstrip(" .") or "Documento tecnico"


def _safe_segment(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalized)
    clean = re.sub(r"\s+", " ", clean).rstrip(" .")
    if not clean:
        clean = "Sin_clasificar"
    if clean.upper() in _WINDOWS_RESERVED_NAMES:
        clean = f"_{clean}"
    return clean[:80].rstrip(" .") or "Sin_clasificar"


def _validate_destination(root: Path, destination: Path) -> None:
    try:
        validate_descendant_path(
            root,
            destination,
            role="organization destination",
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _reject_state_destination(catalog_path: Path, root: Path) -> None:
    try:
        intersects = path_trees_intersect(catalog_path.parent, root)
    except (OSError, ValueError) as exc:
        raise ValueError(
            "organization root/framework state directory boundary cannot be verified"
        ) from exc
    if intersects:
        raise ValueError("organization root and framework state directory must be disjoint")


# endregion [02]
