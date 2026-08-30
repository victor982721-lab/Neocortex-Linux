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
import time
import unicodedata
from pathlib import Path

from neocortex.platform_policy import sqlite_path_collation

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
        "reporte_inventario_archivo",
    }
)


def plan_document_organization(
    catalog_path: Path,
    organization_root: Path,
    *,
    min_confidence: float = 0.72,
    progress: ProgressCallback | None = None,
    progress_operation: str = "framework",
    mutation_guard: CorpusMutationGuard | None = None,
) -> OrganizationPlanSummary:
    """Persist proposed destinations; never create directories or move files."""

    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    root = Path(os.path.abspath(organization_root.expanduser()))
    if mutation_guard is not None:
        mutation_guard.require_paths_allowed(root)
    root_reason = protected_path_reason(root, check_attributes=False)
    if root_reason is not None:
        raise ValueError(f"organization root is protected: {root_reason}")
    _reject_state_destination(catalog_path, root)
    initialize_document_catalog(catalog_path)
    with document_catalog_database(catalog_path) as connection:
        run_id = _begin_organization_run(connection, "plan", root)
        considered = planned = review = blocked = organized = 0
        try:
            total = int(
                connection.execute("SELECT COUNT(*) FROM documents WHERE active=1").fetchone()[0]
            )
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
            rows = connection.execute(
                """SELECT * FROM documents WHERE active=1
                ORDER BY path,source_kind,file_key"""
            )
            for row in rows:
                considered += 1
                status = _plan_catalog_document(
                    connection,
                    run_id,
                    row,
                    root,
                    min_confidence=min_confidence,
                    managed_locations=managed_locations,
                    mutation_guard=mutation_guard,
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
                if considered % 100 == 0:
                    connection.commit()
            summary = OrganizationPlanSummary(
                catalog_run_id=run_id,
                considered=considered,
                planned=planned,
                review_required=review,
                blocked=blocked,
                already_organized=organized,
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
) -> str:
    managed_source = (
        str(row["source_kind"]),
        str(row["file_key"]),
        os.path.normcase(os.path.abspath(str(row["path"]))),
    ) in managed_locations
    connection.execute(
        """UPDATE organization_plans SET status='superseded',
        completed_ns=?,detail='replaced by a newer organization plan'
        WHERE source_kind=? AND file_key=? AND organization_root=?
        AND status='planned'""",
        (time.time_ns(), row["source_kind"], row["file_key"], str(root)),
    )
    protected_reason = _protected_content_reason(
        mutation_guard,
        Path(str(row["path"])),
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
        )
    destination, status, reason = _proposed_destination(
        row,
        root,
        min_confidence=min_confidence,
        managed_source=managed_source,
    )
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
) -> tuple[Path | None, str, str]:
    """Choose one compact semantic destination without redundant dimensions."""

    def review(reason: str) -> tuple[Path | None, str, str]:
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
        ) LIMIT 1""",
        (str(destination),),
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
) -> None:
    evidence = json.dumps(
        {
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
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """INSERT INTO organization_plans(
        catalog_run_id,source_kind,file_key,source_path,destination_path,
        organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
        classifier_signature,primary_kind,confidence,status,reason,evidence_json,
        planned_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            catalog_run_id,
            row["source_kind"],
            row["file_key"],
            row["path"],
            None if destination is None else str(destination),
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
