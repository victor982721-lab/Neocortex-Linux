"""Bounded human presentation for published-state desktop reads."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from neocortex.api.read_contract import sanitize_untrusted_text

from .models import (
    MAX_PRESENTATION_CHARACTERS,
    MAX_PRESENTATION_ROWS,
    MAX_QUERY_CHARACTERS,
    ReadPresentation,
    ReadRequest,
    ReadVisualState,
)

_REASON_LABELS = {
    "source_owner_health_incomplete": (
        "una o más fuentes publicadas están incompletas o son incompatibles"
    ),
    "insufficient_positive_evidence_for_value_recommendation": (
        "faltan señales publicadas suficientes para reducir su protección"
    ),
    "catalog_classification_requires_review": (
        "la clasificación del catálogo todavía requiere revisión"
    ),
    "citation_history_unavailable": "historial de citas no disponible",
    "coverage_history_unavailable": "historial de cobertura no disponible",
    "exact_duplicate_evidence_unavailable": ("no hay evidencia publicada de duplicado exacto"),
    "extraction_health_unavailable": "salud de extracción no disponible",
    "owner_health_unknown": "salud de la fuente no determinada",
    "uniqueness_evidence_unavailable": "no hay evidencia publicada de unicidad",
    "usage_history_unavailable": "historial de uso no disponible",
    "published_plan_proves_byte_exact_redundancy": (
        "un plan publicado demuestra redundancia exacta byte por byte"
    ),
    "published_positive_use_or_citation_protects_file": (
        "el uso o las citas publicadas protegen el archivo"
    ),
    "unique_published_text_fingerprint_protects_file": (
        "su huella textual publicada es única y protege el archivo"
    ),
    "old_repeated_extracted_text_in_archive_path": (
        "texto extraído repetido y antiguo dentro de una ruta de archivo"
    ),
    "old_repeated_extracted_text_in_disposable_path": (
        "texto extraído repetido y antiguo dentro de una ruta temporal"
    ),
}


def _safe_line(value: object, *, limit: int = 800) -> str:
    return sanitize_untrusted_text(value, limit=limit)


class _Lines:
    def __init__(self) -> None:
        self._values: list[str] = []
        self._characters = 0
        self._truncated = False

    def add(self, value: object = "") -> None:
        if self._truncated:
            return
        text = str(value)
        projected = self._characters + len(text) + 1
        if projected > MAX_PRESENTATION_CHARACTERS:
            self._truncated = True
            return
        self._values.append(text)
        self._characters = projected

    def render(self) -> str:
        if self._truncated:
            self._values.append(
                "\nLa presentación alcanzó su límite; reduce el número de resultados."
            )
        return "\n".join(self._values)


def _rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _scope_label(value: object) -> str:
    return {
        "personal": "Personal",
        "framework": "Framework",
    }.get(str(value), _safe_line(value) or "Scope desconocido")


def _human_code(value: object) -> str:
    labels = {
        "keep": "conservar",
        "review_low_value": "revisar valor bajo",
        "archive_candidate": "revisar posible archivo",
        "exact_duplicate_candidate": "revisar duplicado exacto",
        "unknown": "desconocido o protegido",
        "ready": "listo",
        "partial": "parcial",
        "unavailable": "no disponible",
        "complete": "completa",
        "attention_required": "requiere atención",
        "empty": "sin fuentes publicadas",
        "ok": "listo",
        "no_results": "sin resultados",
    }
    text = str(value or "")
    if text.startswith("owner_health_protected:"):
        return "se protege porque la salud de su fuente es " + text.partition(":")[2]
    return labels.get(text, _REASON_LABELS.get(text, text.replace("_", " ")))


def _count_label(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


def _error_line(entry: Mapping[str, object]) -> str:
    return (
        f"{_scope_label(entry.get('scope'))}: no se pudo consultar "
        f"({_safe_line(entry.get('error_type') or 'error')}: "
        f"{_safe_line(entry.get('reason') or 'sin detalle')})."
    )


def _visual_state(payload: Mapping[str, object]) -> ReadVisualState:
    exit_code = payload.get("exit_code")
    if exit_code in {0, 3}:
        return "completed"
    if exit_code in {4, 5, 130}:
        return "warning"
    return "failed"


def _locator(evidence: Mapping[str, object]) -> str:
    parts: list[str] = []
    if evidence.get("page") is not None:
        parts.append(f"página {_safe_line(evidence['page'], limit=40)}")
    if evidence.get("sheet") is not None:
        parts.append(f"hoja {_safe_line(evidence['sheet'], limit=80)}")
    if evidence.get("cell_range") is not None:
        parts.append(f"celdas {_safe_line(evidence['cell_range'], limit=80)}")
    if evidence.get("start_line") is not None:
        start = _safe_line(evidence["start_line"], limit=40)
        end = _safe_line(evidence.get("end_line", start), limit=40)
        parts.append(f"líneas {start}-{end}")
    start_ms = evidence.get("start_ms")
    if isinstance(start_ms, (int, float)) and not isinstance(start_ms, bool):
        parts.append(f"{start_ms / 1000:.1f} s")
    return ", ".join(parts) or "ubicación estructurada"


def _add_hit(
    lines: _Lines,
    hit: Mapping[str, object],
    *,
    prefix: str,
) -> None:
    structured = _structured_hit(hit)
    if structured is None:
        lines.add(f"{prefix} Resultado sin evidencia estructurada compatible.")
        return
    resource, evidence = structured
    path = _safe_line(resource.get("current_path") or resource.get("resource_id") or "sin ruta")
    name = Path(path).name or path
    lines.add(f"{prefix} {name}  ·  {_locator(evidence)}")
    lines.add(f"   Ruta: {path}")
    _add_hit_details(lines, hit, evidence)


def _structured_hit(
    hit: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]] | None:
    resource = hit.get("resource")
    evidence = hit.get("evidence")
    if not isinstance(resource, Mapping) or not isinstance(evidence, Mapping):
        return None
    return resource, evidence


def _add_hit_details(
    lines: _Lines,
    hit: Mapping[str, object],
    evidence: Mapping[str, object],
) -> None:
    snippet = _safe_line(evidence.get("snippet"), limit=1_200)
    if snippet:
        lines.add(f"   Evidencia: {snippet}")
    reasons = _reason_text(hit.get("reasons"), limit=8, line_limit=160)
    if reasons:
        lines.add(f"   Coincidió por: {reasons}")
    lines.add(f"   ID: {_safe_line(evidence.get('evidence_id') or '-', limit=200)}")


def _reason_text(value: object, *, limit: int, line_limit: int) -> str:
    if not isinstance(value, list):
        return ""
    return "; ".join(_safe_line(_human_code(item), limit=line_limit) for item in value[:limit])


def _render_status(payload: Mapping[str, object], lines: _Lines) -> tuple[str, str]:
    lines.add("Estado publicado de NeoCortex")
    lines.add("Consulta local sobre snapshots publicados; no crea ni migra estado.\n")
    scopes = _rows(payload.get("scopes"))
    ready = sum(_render_status_scope(entry, lines) for entry in scopes)
    total = len(scopes)
    scope_label = "alcance listo" if total == 1 else "alcances listos"
    return "Estado publicado", f"{ready} de {total} {scope_label}"


def _render_status_scope(entry: Mapping[str, object], lines: _Lines) -> bool:
    snapshot = entry.get("snapshot")
    if not isinstance(snapshot, Mapping):
        lines.add(_error_line(entry))
        lines.add()
        return False
    owners = _rows(snapshot.get("owners"))
    available = sum(owner.get("state") == "available" for owner in owners)
    absent = sum(owner.get("state") == "absent" for owner in owners)
    models = snapshot.get("active_models")
    model_count = len(models) if isinstance(models, list) else 0
    status = str(entry.get("status"))
    lines.add(f"{_scope_label(entry.get('scope'))}  ·  {_human_code(status)}")
    lines.add(
        f"   {_count_label(available, 'fuente disponible', 'fuentes disponibles')}  ·  "
        f"{_count_label(absent, 'ausente', 'ausentes')}  ·  "
        f"{_count_label(model_count, 'modelo activo', 'modelos activos')}"
    )
    lines.add(f"   Snapshot: {_safe_line(snapshot.get('snapshot_id') or '-', limit=200)}")
    attention = _owner_attention(owners)
    if attention:
        lines.add("   Atención: " + ", ".join(attention[:20]))
    lines.add()
    return status == "ready"


def _owner_attention(owners: list[Mapping[str, object]]) -> list[str]:
    return [
        f"{_safe_line(owner.get('owner'), limit=100)}={_safe_line(owner.get('state'), limit=60)}"
        for owner in owners
        if owner.get("state") not in {"available", "absent"}
    ]


def _render_search(payload: Mapping[str, object], lines: _Lines) -> tuple[str, str]:
    query = _safe_line(payload.get("query"), limit=MAX_QUERY_CHARACTERS)
    lines.add(f"Resultados para: {query}")
    if payload.get("scope_requested") == "all":
        lines.add("Personal y Framework se ordenan por separado; sus scores no se mezclan.")
    lines.add()
    total_hits = 0
    displayed = 0
    for entry in _rows(payload.get("scopes")):
        scope_hits, displayed = _render_search_scope(entry, lines, displayed)
        total_hits += scope_hits
    lines.add("Los scores ordenan candidatos; no son probabilidades ni autorizan acciones.")
    return (
        "Búsqueda de evidencia",
        f"{_count_label(total_hits, 'resultado', 'resultados')} en estado publicado",
    )


def _render_search_scope(
    entry: Mapping[str, object],
    lines: _Lines,
    displayed: int,
) -> tuple[int, int]:
    result = entry.get("result")
    if not isinstance(result, Mapping):
        lines.add(_error_line(entry))
        lines.add()
        return 0, displayed
    hits = _rows(result.get("hits"))
    _add_search_scope_header(lines, entry, result, len(hits))
    displayed = _render_search_hits(lines, hits, displayed)
    if not hits:
        lines.add("   No se encontró evidencia para esta consulta.\n")
    _add_warnings(lines, result.get("warnings"))
    return len(hits), displayed


def _add_search_scope_header(
    lines: _Lines,
    entry: Mapping[str, object],
    result: Mapping[str, object],
    hit_count: int,
) -> None:
    coverage = "completa" if result.get("complete") is True else "parcial"
    suffix = (
        "; existen más candidatos fuera de este límite"
        if result.get("result_window_full") is True
        else ""
    )
    lines.add(
        f"{_scope_label(entry.get('scope'))}  ·  "
        f"{_count_label(hit_count, 'resultado', 'resultados')}  ·  "
        f"cobertura {coverage}{suffix}"
    )


def _render_search_hits(lines: _Lines, hits: list[Mapping[str, object]], displayed: int) -> int:
    for index, hit in enumerate(hits, start=1):
        if displayed >= MAX_PRESENTATION_ROWS:
            break
        _add_hit(lines, hit, prefix=f"{index}.")
        lines.add()
        displayed += 1
    return displayed


def _add_warnings(lines: _Lines, value: object) -> None:
    if not isinstance(value, list) or not value:
        return
    warnings = ", ".join(_safe_line(item, limit=200) for item in value[:20])
    lines.add("   Límites: " + warnings)


def _render_ask(payload: Mapping[str, object], lines: _Lines) -> tuple[str, str]:
    query = _safe_line(payload.get("query"), limit=MAX_QUERY_CHARACTERS)
    lines.add(f"Evidencia citada para responder: {query}")
    lines.add("NeoCortex prepara contexto local; no inventa una respuesta sin evidencia.\n")
    total_hits = 0
    displayed = 0
    for entry in _rows(payload.get("scopes")):
        scope_hits, displayed = _render_ask_scope(entry, lines, displayed)
        total_hits += scope_hits
    return (
        "Respuesta sustentada",
        _count_label(total_hits, "cita recuperada", "citas recuperadas"),
    )


def _render_ask_scope(
    entry: Mapping[str, object],
    lines: _Lines,
    displayed: int,
) -> tuple[int, int]:
    context = entry.get("context")
    if not isinstance(context, Mapping):
        lines.add(_error_line(entry))
        lines.add()
        return 0, displayed
    hits = _rows(context.get("selected_hits"))
    citations = _rows(context.get("citation_ids"))
    completeness = _human_code(context.get("completeness") or entry.get("status"))
    lines.add(
        f"{_scope_label(entry.get('scope'))}  ·  {completeness}  ·  "
        f"{_count_label(len(hits), 'cita', 'citas')}"
    )
    displayed = _render_cited_hits(lines, hits, citations, displayed)
    if not hits:
        _add_missing_context(lines, context.get("missing_information"))
    return len(hits), displayed


def _render_cited_hits(
    lines: _Lines,
    hits: list[Mapping[str, object]],
    citations: list[Mapping[str, object]],
    displayed: int,
) -> int:
    for index, hit in enumerate(hits):
        if displayed >= MAX_PRESENTATION_ROWS:
            break
        citation = citations[index] if index < len(citations) else {}
        citation_id = _safe_line(citation.get("citation_id") or f"K{index + 1}", limit=80)
        _add_hit(lines, hit, prefix=f"[{citation_id}]")
        lines.add()
        displayed += 1
    return displayed


def _add_missing_context(lines: _Lines, value: object) -> None:
    if isinstance(value, list) and value:
        for reason in value[:20]:
            lines.add(f"   Falta: {_safe_line(reason, limit=400)}")
    else:
        lines.add("   No hay evidencia suficiente para responder.")
    lines.add()


def _size(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "tamaño desconocido"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable size unit")


def _render_review(payload: Mapping[str, object], lines: _Lines) -> tuple[str, str]:
    lines.add("Revisión conservadora de valor")
    lines.add("Solo lectura: esta revisión no autoriza mover, archivar o borrar.\n")
    total_items = 0
    displayed = 0
    for entry in _rows(payload.get("scopes")):
        scope_items, displayed = _render_review_scope(entry, lines, displayed)
        total_items += scope_items
    return (
        "Revisión consultiva",
        f"{_count_label(total_items, 'candidato visible', 'candidatos visibles')}; "
        "0 acciones aplicadas",
    )


def _render_review_scope(
    entry: Mapping[str, object],
    lines: _Lines,
    displayed: int,
) -> tuple[int, int]:
    report = entry.get("report")
    if not isinstance(report, Mapping):
        lines.add(_error_line(entry))
        lines.add()
        return 0, displayed
    items = _rows(report.get("items"))
    lines.add(
        f"{_scope_label(entry.get('scope'))}  ·  "
        f"{_human_code(entry.get('status'))}  ·  {len(items)} de "
        f"{_safe_line(report.get('matched_count') or 0, limit=40)} candidatos"
    )
    reason = report.get("reason")
    if reason:
        lines.add(f"   Cobertura: {_safe_line(_human_code(reason), limit=400)}")
    displayed = _render_review_items(lines, items, displayed)
    if not items:
        lines.add("   No hay candidatos publicados dentro de este límite.\n")
    return len(items), displayed


def _render_review_items(
    lines: _Lines,
    items: list[Mapping[str, object]],
    displayed: int,
) -> int:
    for index, item in enumerate(items, start=1):
        if displayed >= MAX_PRESENTATION_ROWS:
            break
        _add_review_item(lines, item, index)
        displayed += 1
    return displayed


def _add_review_item(lines: _Lines, item: Mapping[str, object], index: int) -> None:
    lines.add(
        f"{index}. [{_human_code(item.get('state'))}] "
        f"{_safe_line(item.get('path') or 'sin ruta')}  ·  "
        f"{_size(item.get('size_bytes'))}"
    )
    reasons = _reason_text(item.get("reasons"), limit=12, line_limit=200)
    if reasons:
        lines.add("   Evidencia: " + reasons)
    uncertainties = _reason_text(item.get("uncertainties"), limit=12, line_limit=200)
    if uncertainties:
        lines.add("   Incertidumbre: " + uncertainties)
    lines.add()


def present_read_payload(
    request: ReadRequest,
    payload: Mapping[str, object],
) -> ReadPresentation:
    """Turn one validated shared contract into bounded, selectable Spanish text."""

    selected = request.validated()
    lines = _Lines()
    renderers = {
        "status": _render_status,
        "search": _render_search,
        "ask": _render_ask,
        "review": _render_review,
    }
    title, summary = renderers[selected.operation](payload, lines)
    return ReadPresentation(
        title=title,
        summary=summary,
        body=lines.render(),
        state=_visual_state(payload),
    )


__all__ = ["present_read_payload"]
