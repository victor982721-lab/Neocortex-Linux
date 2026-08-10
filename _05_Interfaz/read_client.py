"""Read-only bridge and bounded human presentation for the desktop UI."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


ReadOperation = Literal["status", "search", "ask", "review"]
ReadVisualState = Literal["completed", "warning", "failed"]

MAX_QUERY_CHARACTERS = 4_096
MAX_RESULTS_PER_SCOPE = 100
MAX_PRESENTATION_CHARACTERS = 128_000
MAX_PRESENTATION_ROWS = 200

_OPERATIONS = frozenset({"status", "search", "ask", "review"})
_SCOPES = frozenset({"personal", "framework", "all"})
_EXPECTED_CONTRACTS = {
    "status": ("neocortex.read-api/v1", "neocortex_scoped_status"),
    "search": ("neocortex.read-api/v1", "neocortex_scoped_search"),
    "ask": ("neocortex.read-api/v1", "neocortex_scoped_context"),
    "review": ("neocortex.value-review/v1", "neocortex_scoped_value_review"),
}

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


class ReadClientError(RuntimeError):
    """The shared read API could not provide a safe display contract."""


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """One bounded desktop request over a fixed published-state scope."""

    operation: ReadOperation
    scope: str = "all"
    query: str = ""
    limit: int = 10

    def validated(self) -> ReadRequest:
        if self.operation not in _OPERATIONS:
            raise ValueError("operation must be status, search, ask or review")
        if self.scope not in _SCOPES:
            raise ValueError("scope must be personal, framework or all")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= self.limit <= MAX_RESULTS_PER_SCOPE:
            raise ValueError(f"limit must be between 1 and {MAX_RESULTS_PER_SCOPE} per scope")
        query = self.query.strip()
        if self.operation in {"search", "ask"} and not query:
            raise ValueError("Escribe una consulta antes de continuar.")
        if len(query) > MAX_QUERY_CHARACTERS:
            raise ValueError(f"La consulta no puede exceder {MAX_QUERY_CHARACTERS} caracteres.")
        return ReadRequest(
            operation=self.operation,
            scope=self.scope,
            query=query,
            limit=self.limit,
        )


@dataclass(frozen=True, slots=True)
class ReadPresentation:
    """Plain-text view model safe to expose in a selectable Qt widget."""

    title: str
    summary: str
    body: str
    state: ReadVisualState


class ReadClient(Protocol):
    def execute(self, request: ReadRequest) -> dict[str, object]: ...


class SharedReadClient:
    """Call the canonical local facade without accepting caller-owned paths."""

    def execute(self, request: ReadRequest) -> dict[str, object]:
        selected = request.validated()
        if selected.operation == "review":
            adapter = importlib.import_module("neocortex.value_cli_adapter")
            raw_payload = adapter.value_review_payload(
                selected.scope,
                limit=selected.limit,
            )
        else:
            read_api = importlib.import_module("neocortex.read_api")
            if selected.operation == "status":
                raw_payload = read_api.status_payload(selected.scope)
            elif selected.operation == "search":
                raw_payload = read_api.search_payload(
                    selected.query,
                    selected.scope,
                    limit=selected.limit,
                    mode="evidence",
                )
            else:
                raw_payload = read_api.context_payload(
                    selected.query,
                    selected.scope,
                    limit=selected.limit,
                    max_characters=12_000,
                    mode="evidence",
                )
        return _validated_payload(selected, raw_payload)


def _validated_payload(
    request: ReadRequest,
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ReadClientError("El read API devolvió una respuesta no estructurada.")
    payload = dict(value)
    expected_schema, expected_kind = _EXPECTED_CONTRACTS[request.operation]
    if payload.get("schema") != expected_schema or payload.get("kind") != expected_kind:
        raise ReadClientError("El read API devolvió un contrato incompatible.")
    if payload.get("read_only") is not True:
        raise ReadClientError("El read API no confirmó el modo de solo lectura.")
    if payload.get("scope_requested") != request.scope:
        raise ReadClientError("El read API respondió para un scope distinto al solicitado.")
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ReadClientError("El read API no devolvió un código de salida válido.")
    if not isinstance(payload.get("scopes"), list):
        raise ReadClientError("El read API no devolvió scopes consultables.")
    if request.operation == "review" and (
        payload.get("operation") != "value-preview"
        or payload.get("advisory_only") is not True
        or payload.get("mutation_authorized") is not False
    ):
        raise ReadClientError("La revisión no confirmó su carácter consultivo.")
    return payload


def _safe_line(value: object, *, limit: int = 800) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


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
    resource = hit.get("resource")
    evidence = hit.get("evidence")
    if not isinstance(resource, Mapping) or not isinstance(evidence, Mapping):
        lines.add(f"{prefix} Resultado sin evidencia estructurada compatible.")
        return
    path = _safe_line(resource.get("current_path") or resource.get("resource_id") or "sin ruta")
    name = Path(path).name or path
    lines.add(f"{prefix} {name}  ·  {_locator(evidence)}")
    lines.add(f"   Ruta: {path}")
    snippet = _safe_line(evidence.get("snippet"), limit=1_200)
    if snippet:
        lines.add(f"   Evidencia: {snippet}")
    evidence_reasons = hit.get("reasons")
    if isinstance(evidence_reasons, list) and evidence_reasons:
        reasons = "; ".join(
            _safe_line(_human_code(item), limit=160) for item in evidence_reasons[:8]
        )
        lines.add(f"   Coincidió por: {reasons}")
    lines.add(f"   ID: {_safe_line(evidence.get('evidence_id') or '-', limit=200)}")


def _render_status(payload: Mapping[str, object], lines: _Lines) -> tuple[str, str]:
    lines.add("Estado publicado de NeoCortex")
    lines.add("Consulta local sobre snapshots publicados; no crea ni migra estado.\n")
    ready = 0
    total = 0
    for entry in _rows(payload.get("scopes")):
        total += 1
        snapshot = entry.get("snapshot")
        if not isinstance(snapshot, Mapping):
            lines.add(_error_line(entry))
            lines.add()
            continue
        owners = _rows(snapshot.get("owners"))
        available = sum(owner.get("state") == "available" for owner in owners)
        absent = sum(owner.get("state") == "absent" for owner in owners)
        attention = [
            f"{_safe_line(owner.get('owner'), limit=100)}="
            f"{_safe_line(owner.get('state'), limit=60)}"
            for owner in owners
            if owner.get("state") not in {"available", "absent"}
        ]
        models = snapshot.get("active_models")
        model_count = len(models) if isinstance(models, list) else 0
        status = str(entry.get("status"))
        ready += int(status == "ready")
        lines.add(f"{_scope_label(entry.get('scope'))}  ·  {_human_code(status)}")
        lines.add(
            f"   {_count_label(available, 'fuente disponible', 'fuentes disponibles')}  ·  "
            f"{_count_label(absent, 'ausente', 'ausentes')}  ·  "
            f"{_count_label(model_count, 'modelo activo', 'modelos activos')}"
        )
        lines.add(f"   Snapshot: {_safe_line(snapshot.get('snapshot_id') or '-', limit=200)}")
        if attention:
            lines.add("   Atención: " + ", ".join(attention[:20]))
        lines.add()
    scope_label = "alcance listo" if total == 1 else "alcances listos"
    return "Estado publicado", f"{ready} de {total} {scope_label}"


def _render_search(payload: Mapping[str, object], lines: _Lines) -> tuple[str, str]:
    query = _safe_line(payload.get("query"), limit=MAX_QUERY_CHARACTERS)
    lines.add(f"Resultados para: {query}")
    if payload.get("scope_requested") == "all":
        lines.add("Personal y Framework se ordenan por separado; sus scores no se mezclan.")
    lines.add()
    total_hits = 0
    displayed = 0
    for entry in _rows(payload.get("scopes")):
        result = entry.get("result")
        if not isinstance(result, Mapping):
            lines.add(_error_line(entry))
            lines.add()
            continue
        hits = _rows(result.get("hits"))
        total_hits += len(hits)
        coverage = "completa" if result.get("complete") is True else "parcial"
        suffix = (
            "; existen más candidatos fuera de este límite"
            if result.get("result_window_full") is True
            else ""
        )
        lines.add(
            f"{_scope_label(entry.get('scope'))}  ·  "
            f"{_count_label(len(hits), 'resultado', 'resultados')}  ·  "
            f"cobertura {coverage}{suffix}"
        )
        for index, hit in enumerate(hits, start=1):
            if displayed >= MAX_PRESENTATION_ROWS:
                break
            _add_hit(lines, hit, prefix=f"{index}.")
            lines.add()
            displayed += 1
        if not hits:
            lines.add("   No se encontró evidencia para esta consulta.\n")
        warnings = result.get("warnings")
        if isinstance(warnings, list) and warnings:
            lines.add(
                "   Límites: " + ", ".join(_safe_line(item, limit=200) for item in warnings[:20])
            )
    lines.add("Los scores ordenan candidatos; no son probabilidades ni autorizan acciones.")
    return (
        "Búsqueda de evidencia",
        f"{_count_label(total_hits, 'resultado', 'resultados')} en estado publicado",
    )


def _render_ask(payload: Mapping[str, object], lines: _Lines) -> tuple[str, str]:
    query = _safe_line(payload.get("query"), limit=MAX_QUERY_CHARACTERS)
    lines.add(f"Evidencia citada para responder: {query}")
    lines.add("NeoCortex prepara contexto local; no inventa una respuesta sin evidencia.\n")
    total_hits = 0
    displayed = 0
    for entry in _rows(payload.get("scopes")):
        context = entry.get("context")
        if not isinstance(context, Mapping):
            lines.add(_error_line(entry))
            lines.add()
            continue
        hits = _rows(context.get("selected_hits"))
        citations = _rows(context.get("citation_ids"))
        total_hits += len(hits)
        completeness = _human_code(context.get("completeness") or entry.get("status"))
        lines.add(
            f"{_scope_label(entry.get('scope'))}  ·  {completeness}  ·  "
            f"{_count_label(len(hits), 'cita', 'citas')}"
        )
        for index, hit in enumerate(hits):
            if displayed >= MAX_PRESENTATION_ROWS:
                break
            citation = citations[index] if index < len(citations) else {}
            citation_id = _safe_line(citation.get("citation_id") or f"K{index + 1}", limit=80)
            _add_hit(lines, hit, prefix=f"[{citation_id}]")
            lines.add()
            displayed += 1
        if not hits:
            missing = context.get("missing_information")
            if isinstance(missing, list) and missing:
                for reason in missing[:20]:
                    lines.add(f"   Falta: {_safe_line(reason, limit=400)}")
            else:
                lines.add("   No hay evidencia suficiente para responder.")
            lines.add()
    return (
        "Respuesta sustentada",
        _count_label(total_hits, "cita recuperada", "citas recuperadas"),
    )


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
        report = entry.get("report")
        if not isinstance(report, Mapping):
            lines.add(_error_line(entry))
            lines.add()
            continue
        items = _rows(report.get("items"))
        total_items += len(items)
        lines.add(
            f"{_scope_label(entry.get('scope'))}  ·  "
            f"{_human_code(entry.get('status'))}  ·  {len(items)} de "
            f"{_safe_line(report.get('matched_count') or 0, limit=40)} candidatos"
        )
        reason = report.get("reason")
        if reason:
            lines.add(f"   Cobertura: {_safe_line(_human_code(reason), limit=400)}")
        for index, item in enumerate(items, start=1):
            if displayed >= MAX_PRESENTATION_ROWS:
                break
            lines.add(
                f"{index}. [{_human_code(item.get('state'))}] "
                f"{_safe_line(item.get('path') or 'sin ruta')}  ·  "
                f"{_size(item.get('size_bytes'))}"
            )
            reasons = item.get("reasons")
            if isinstance(reasons, list) and reasons:
                lines.add(
                    "   Evidencia: "
                    + "; ".join(_safe_line(_human_code(value), limit=200) for value in reasons[:12])
                )
            uncertainties = item.get("uncertainties")
            if isinstance(uncertainties, list) and uncertainties:
                lines.add(
                    "   Incertidumbre: "
                    + "; ".join(
                        _safe_line(_human_code(value), limit=200) for value in uncertainties[:12]
                    )
                )
            lines.add()
            displayed += 1
        if not items:
            lines.add("   No hay candidatos publicados dentro de este límite.\n")
    return (
        "Revisión consultiva",
        f"{_count_label(total_items, 'candidato visible', 'candidatos visibles')}; "
        "0 acciones aplicadas",
    )


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


__all__ = (
    "MAX_PRESENTATION_CHARACTERS",
    "MAX_QUERY_CHARACTERS",
    "MAX_RESULTS_PER_SCOPE",
    "ReadClient",
    "ReadClientError",
    "ReadOperation",
    "ReadPresentation",
    "ReadRequest",
    "SharedReadClient",
    "present_read_payload",
)
