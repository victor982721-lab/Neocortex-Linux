"""Deterministic, non-mutating destination planning for technical documents."""
# region [00] Contexto del módulo
# Módulo: neocortex/document_organization_planning.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import json
import os
import re
import sqlite3
import stat
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .document_catalog_replay import catalog_sql_cancellation

if TYPE_CHECKING:
    from neocortex.runtime.control.cancellation import CancellationToken

from neocortex.platform.policy import sqlite_path_collation

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


@dataclass(frozen=True, slots=True)
class OrganizationCorpusPolicy:
    """Explicit opt-in for reversible plans outside the technical default."""

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
_COMPACT_KIND_DIRECTORIES: dict[str, tuple[str, ...]] = {
    "accion_correctiva_preventiva": ("Pruebas_y_calidad", "Calidad"),
    "catalogo_equipo": ("Ingenieria_y_documentacion", "Manuales_catalogos_y_fichas"),
    "certificado_calibracion": ("Pruebas_y_calidad", "Laboratorio_y_metrologia"),
    "certificado_calidad": ("Pruebas_y_calidad", "Calidad"),
    "comprobante_viaje": ("Gestion_y_administracion", "Administracion"),
    "constancia_capacitacion": ("Capacitacion",),
    "control_metrologico": ("Pruebas_y_calidad", "Laboratorio_y_metrologia"),
    "correspondencia": ("Gestion_y_administracion", "Proyecto_y_correspondencia"),
    "credencial_visitante": ("Seguridad_y_ambiente", "Seguridad"),
    "curso_capacitacion": ("Capacitacion",),
    "descripcion_tecnica_sistema": (
        "Ingenieria_y_documentacion",
        "Ingenieria_y_calculos",
    ),
    "documento_empresa": ("Ingenieria_y_documentacion", "Informes_y_referencias"),
    "dossier_calidad": ("Pruebas_y_calidad", "Calidad"),
    "especificacion_tecnica": ("Ingenieria_y_documentacion", "Ingenieria_y_calculos"),
    "etiqueta_muestra_laboratorio": ("Pruebas_y_calidad", "Laboratorio_y_metrologia"),
    "factura_comprobante": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "ficha_tecnica": ("Ingenieria_y_documentacion", "Manuales_catalogos_y_fichas"),
    "formato_empresa": ("Gestion_y_administracion", "Formatos_y_registros"),
    "formato_inspeccion": ("Pruebas_y_calidad", "Inspecciones"),
    "hoja_asignacion_proyecto": (
        "Gestion_y_administracion",
        "Proyecto_y_correspondencia",
    ),
    "hoja_datos_seguridad": ("Seguridad_y_ambiente", "Seguridad"),
    "informe_analisis": ("Ingenieria_y_documentacion", "Informes_y_referencias"),
    "informe_auditoria": ("Pruebas_y_calidad", "Calidad"),
    "informe_inspeccion": ("Pruebas_y_calidad", "Inspecciones"),
    "informe_tecnico": ("Ingenieria_y_documentacion", "Informes_y_referencias"),
    "instructivo_trabajo": (
        "Operacion_y_mantenimiento",
        "Procedimientos_e_instructivos",
    ),
    "lista_empaque_embarque": ("Logistica_y_embarques",),
    "lista_materiales": ("Operacion_y_mantenimiento", "Planeacion_y_ordenes"),
    "lista_verificacion": ("Pruebas_y_calidad", "Inspecciones"),
    "manual_equipo": ("Ingenieria_y_documentacion", "Manuales_catalogos_y_fichas"),
    "manual_sistema_gestion": ("Pruebas_y_calidad", "Calidad"),
    "memoria_calculo": ("Ingenieria_y_documentacion", "Ingenieria_y_calculos"),
    "minuta_acta": ("Gestion_y_administracion", "Proyecto_y_correspondencia"),
    "orden_trabajo": ("Operacion_y_mantenimiento", "Planeacion_y_ordenes"),
    "plan_tecnico": ("Operacion_y_mantenimiento", "Planeacion_y_ordenes"),
    "plano_diagrama": ("Ingenieria_y_documentacion", "Planos_y_diagramas"),
    "procedimiento": ("Operacion_y_mantenimiento", "Procedimientos_e_instructivos"),
    "programa_cronograma": ("Operacion_y_mantenimiento", "Planeacion_y_ordenes"),
    "programa_gestion_ambiental": ("Seguridad_y_ambiente", "Ambiente"),
    "programa_seguridad_salud": ("Seguridad_y_ambiente", "Seguridad"),
    "protocolo_pruebas": ("Pruebas_y_calidad", "Pruebas_y_resultados"),
    "referencia_tecnica": ("Ingenieria_y_documentacion", "Informes_y_referencias"),
    "registro_asistencia": ("Gestion_y_administracion", "Formatos_y_registros"),
    "registro_auditores": ("Pruebas_y_calidad", "Calidad"),
    "registro_bitacora": ("Operacion_y_mantenimiento", "Bitacoras_y_reportes"),
    "registro_entrega_epp": ("Seguridad_y_ambiente", "Seguridad"),
    "registro_fotografico": ("Operacion_y_mantenimiento", "Bitacoras_y_reportes"),
    "registro_incidencias": ("Seguridad_y_ambiente", "Seguridad"),
    "registro_mediciones": ("Pruebas_y_calidad", "Pruebas_y_resultados"),
    "registro_tiempo_personal": ("Gestion_y_administracion", "Formatos_y_registros"),
    "reporte_actividades": ("Operacion_y_mantenimiento", "Bitacoras_y_reportes"),
    "reporte_anomalias": ("Operacion_y_mantenimiento", "Bitacoras_y_reportes"),
    "reporte_entrega_embarque": ("Logistica_y_embarques",),
    "reporte_fat_sat": ("Pruebas_y_calidad", "FAT_SAT"),
    "reporte_laboratorio": ("Pruebas_y_calidad", "Laboratorio_y_metrologia"),
    "reporte_no_conformidad": ("Pruebas_y_calidad", "Calidad"),
    "reporte_resultados_pruebas": ("Pruebas_y_calidad", "Pruebas_y_resultados"),
    "viaticos_gastos": ("Gestion_y_administracion", "Administracion"),
    "compra_requisicion": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "contrato_legal": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "cotizacion_propuesta": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "licitacion": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "entrevista_grabada": ("Reuniones_y_entrevistas",),
    "instruccion_verbal": ("Reuniones_y_entrevistas",),
    "reunion_grabada": ("Reuniones_y_entrevistas",),
}
_REVIEW_ONLY_KINDS = frozenset(
    {
        "audio_transcrito",
        "expediente_personal",
        "instruccion_cuenta_bancaria",
        "otro",
        "registro_log",
        "reporte_inventario_archivo",
    }
)

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
    cache: dict[str, tuple[Path, str] | None],
) -> tuple[Path, str] | None:
    """Find an exact OOXML directory package enclosing ``path``.

    A directory tree is only treated as a package when it contains the same
    exact marker names used by the ZIP detector.  A main part without
    ``[Content_Types].xml`` is retained as a partial package hypothesis so
    organization cannot move a member independently while the set is
    incomplete.
    """

    key = str(path)
    if key in cache:
        return cache[key]
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
    cache[key] = result
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
    cancellation: CancellationToken | None = None,
) -> OrganizationPlanSummary:
    """Persist proposed destinations; never create directories or move files."""

    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    if not isinstance(source_scope, OrganizationInputScope):
        raise ValueError("organization_input_scope_required")
    if corpus_policy is not None and organization_policy is not None:
        raise ValueError("organization corpus policy was supplied twice")
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
        considered = planned = review = blocked = organized = 0
        excluded_out_of_scope = unresolved_scope = excluded_components = 0
        try:
            # Publish one complete plan generation.  An interrupted rebuild must
            # not supersede the previous proposals or expose half a new scope.
            connection.execute("BEGIN IMMEDIATE")
            source_scope.verify(connection)
            rows: list[sqlite3.Row] = []
            decompressed_package_cache: dict[str, tuple[Path, str] | None] = {}
            for candidate in connection.execute(
                """SELECT * FROM documents WHERE active=1
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
                            decompressed_package_cache,
                        )
                        is not None
                    ):
                        # The directory itself is the logical owner.  Until a
                        # directory-package representation exists, retaining
                        # every member as a component is safer than proposing
                        # independent XML moves that split the package.
                        excluded_components += 1
                    else:
                        rows.append(candidate)
                elif assessment.reason == "source_outside_scope":
                    excluded_out_of_scope += 1
                else:
                    unresolved_scope += 1
            total = len(rows)
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
            for row in rows:
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
        )
    binding = parse_resource_binding(row["resource_binding_json"])
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
        )
    destination, status, reason = _proposed_destination(
        row,
        root,
        min_confidence=min_confidence,
        managed_source=managed_source,
        corpus_policy=corpus_policy,
    )
    if (
        status == "planned"
        and str(row["catalog_status"]) != "classified"
        and not reason.startswith("explicit_corpus_policy_reversible:")
    ):
        status, reason = "review", "source_classification_requires_review"
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
        reason = "classification_above_threshold_with_identity_disambiguation"
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
                )
                return "blocked"
            _insert_plan(
                connection,
                run_id,
                row,
                root,
                resolved,
                status,
                "classification_above_threshold_with_identity_disambiguation",
                source_scope=source_scope,
                corpus_policy=corpus_policy,
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


_SENSITIVE_ORGANIZATION_KINDS = frozenset(
    {
        "expediente_personal",
        "instruccion_cuenta_bancaria",
        "credencial_visitante",
        "registro_entrega_epp",
    }
)
_NONTECHNICAL_ORGANIZATION_KINDS = frozenset(
    {
        "audio_transcrito",
        "registro_log",
        "reporte_inventario_archivo",
        "codigo",
    }
)


def _classification_payload(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = json.loads(str(row["classification_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _organization_policy_category(row: sqlite3.Row, *, reason: str | None = None) -> str | None:
    """Map an advisory row to an explicit opt-in policy bucket."""

    kind = str(row["primary_kind"] or "")
    payload = _classification_payload(row)
    taxonomy_status = str(payload.get("taxonomy_status") or "")
    if kind == "otro" and taxonomy_status not in {"outside_taxonomy"}:
        # ``otro`` with insufficient identification is the one true unknown
        # bucket.  It must not be made actionable merely by enabling a broad
        # reversible policy.
        return None
    if kind in _SENSITIVE_ORGANIZATION_KINDS:
        return "sensitive"
    if kind in _NONTECHNICAL_ORGANIZATION_KINDS or taxonomy_status == "outside_taxonomy":
        return "nontechnical"
    if (
        str(row["catalog_status"]) != "classified"
        or str(row["uncertainty"]) == "alta"
        or reason in {
            "classification_confidence_below_threshold",
            "insufficient_document_identification",
            "outside_organization_taxonomy",
        }
    ):
        return "uncertain"
    return "general"


def _policy_destination(
    row: sqlite3.Row,
    root: Path,
    category: str,
) -> Path:
    kind = str(row["primary_kind"] or "")
    kind_segment = "Sin_clasificar" if kind == "otro" else _safe_segment(kind)
    directories = {
        "general": ("General", kind_segment),
        "uncertain": ("Revision_pendiente", kind_segment),
        "sensitive": ("Sensible", kind_segment),
        "nontechnical": ("No_tecnico", kind_segment),
    }
    parts = directories.get(category)
    if parts is None:  # pragma: no cover - caller validates category
        raise ValueError(f"unsupported organization policy category: {category}")
    destination = root.joinpath(*parts, Path(str(row["path"])).name)
    _validate_destination(root, destination)
    return destination


def _proposed_destination(
    row: sqlite3.Row,
    root: Path,
    *,
    min_confidence: float,
    managed_source: bool,
    corpus_policy: OrganizationCorpusPolicy | None = None,
) -> tuple[Path | None, str, str]:
    """Choose one compact semantic destination without redundant dimensions."""

    resolved_policy = corpus_policy or OrganizationCorpusPolicy()

    def review(reason: str) -> tuple[Path | None, str, str]:
        category = _organization_policy_category(row, reason=reason)
        if category is not None and resolved_policy.allows(category):
            return (
                _policy_destination(row, root, category),
                "planned",
                f"explicit_corpus_policy_reversible:{category}:{reason}",
            )
        if not managed_source:
            return None, "review", reason
        destination = root.joinpath(
            "Revision_pendiente",
            _safe_segment(str(row["source_kind"]).upper()),
            Path(str(row["path"])).name,
        )
        _validate_destination(root, destination)
        return destination, "planned", f"managed_reclassification:{reason}"

    if str(row["catalog_status"]) == "error":
        return review("classification_error")
    try:
        classification = json.loads(str(row["classification_json"]))
    except (TypeError, ValueError):
        classification = {}
    taxonomy_status = (
        classification.get("taxonomy_status") if isinstance(classification, dict) else None
    )
    if taxonomy_status == "outside_taxonomy":
        return review("outside_organization_taxonomy")
    if taxonomy_status == "insufficient_identification":
        return review("insufficient_document_identification")
    confidence = float(row["confidence"])
    if confidence < min_confidence:
        return review("classification_confidence_below_threshold")

    kind = str(row["primary_kind"])
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
    if str(row["catalog_status"]) != "classified":
        return destination, "review", "source_classification_requires_review"
    return destination, "planned", "classification_above_threshold"


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
    extension = requested.suffix
    stem = requested.name[: -len(extension)] if extension else requested.name
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
    if os.path.lexists(destination):
        return False
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
    return plan_conflict is None


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
) -> None:
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
            if status == "planned" and row["catalog_status"] == "classified"
            else "blocked"
        )
    )
    blockers = ["backend_unavailable", "authorization_required"]
    if virtual:
        blockers.append("virtual_resource_requires_materialization")
    representation_metadata = binding.get("representation_metadata", {})
    if (
        representation_metadata.get("document_role") == "document_component"
        and representation_metadata.get("independently_organizable") is False
    ):
        blockers.append("document_component_not_independently_organizable")
    if str(row["catalog_status"]) != "classified":
        blockers.append("source_classification_requires_review")
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
        )
        logical_destination = None if proposed is None else str(proposed)
    evidence = json.dumps(
        {
            "primary_kind": row["primary_kind"],
            "classification_status": row["catalog_status"],
            "classification_score_kind": classification.get(
                "confidence_kind", "uncalibrated_heuristic"
            ),
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
            "corpus_policy": corpus_policy.to_dict(),
            "reversible": corpus_policy.reversible,
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
            0,
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
    original_stem = source.stem
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
    safe_stem = _safe_filename_stem(suggested, extension=source.suffix)
    if os.path.normcase(safe_stem) == os.path.normcase(original_stem):
        return source.name
    return f"{safe_stem}{source.suffix}"


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
