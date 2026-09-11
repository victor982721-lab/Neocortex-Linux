"""Read-only desktop presentation for curation grants and recovery."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

from neocortex.api.curation_api import CurationPlanOutput, _curation_plan_payload_for_state
from neocortex.api.read_contract import sanitize_untrusted_text
from neocortex.curation.read import (
    CURATION_READ_MAX_ITEMS,
    CurationReadSnapshot,
    read_curation_snapshot,
)

from .models import MAX_PRESENTATION_CHARACTERS, ReadPresentation, ReadVisualState


class CurationReadRepository:
    """Read fixed curation owners without importing any writer."""

    def __init__(
        self,
        state_directory: Path,
        *,
        reader: Callable[..., CurationReadSnapshot] = read_curation_snapshot,
        plan_reader: Callable[..., CurationPlanOutput] = _curation_plan_payload_for_state,
    ) -> None:
        self.state_directory = Path(state_directory)
        self._reader = reader
        self._plan_reader = plan_reader

    @property
    def database_path(self) -> Path:
        return self.state_directory / "framework.sqlite3"

    def read(self, *, limit: int = CURATION_READ_MAX_ITEMS) -> CurationReadSnapshot:
        return self._reader(self.database_path, limit=limit)

    def read_plan(self, *, limit: int = 50) -> CurationPlanOutput:
        """Read the persisted curation plan from this GUI's configured state."""

        return self._plan_reader(
            limit=limit,
            state_directory=self.state_directory,
        )


def _safe(value: object, *, limit: int = 800) -> str:
    return sanitize_untrusted_text(value, limit=limit, single_line=True)


def _safe_optional(value: object, *, limit: int = 800) -> str:
    return "" if value is None else _safe(value, limit=limit)


def _structured_text(value: object, *, limit: int = 640) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        encoded = str(value)
    return _safe(encoded, limit=limit)


def _plan_locator(item: Mapping[str, object]) -> str:
    evidence = item.get("evidence")
    if not isinstance(evidence, Mapping):
        return ""
    locator = evidence.get("locator")
    if locator is None:
        locator = evidence.get("locators")
    if locator is not None:
        return _structured_text(locator, limit=640)
    fields = (
        "plan_id",
        "catalog_run_id",
        "scan_id",
        "group_id",
        "member_count",
        "source_kind",
        "file_key",
        "source_scope_id",
        "section_kind",
        "section_id",
        "page",
        "sheet",
        "cell_range",
        "start_line",
        "end_line",
        "timestamp_ms",
    )
    values = [
        f"{name}={_safe_optional(evidence.get(name), limit=160)}"
        for name in fields
        if evidence.get(name) is not None
    ]
    return " · ".join(values)


def _plan_next_step(item: Mapping[str, object]) -> str:
    evidence = item.get("evidence")
    if isinstance(evidence, Mapping):
        explicit = evidence.get("next_step") or evidence.get("next_step_code")
        if isinstance(explicit, str) and explicit.strip():
            return _safe(explicit, limit=320)
        blockers = evidence.get("blockers")
        if isinstance(blockers, (list, tuple)) and blockers:
            labels = ", ".join(_safe(value, limit=120) for value in blockers[:8])
            return f"revisión humana de bloqueadores ({labels}); sin efecto automático"
    action = _safe_optional(item.get("action"), limit=120)
    if action.startswith("review_"):
        return "revisión humana consultiva; esta vista no autoriza ni ejecuta cambios"
    return "observar la propuesta; esta vista no ejecuta efectos"


def _plan_requires_attention(plan: Mapping[str, object]) -> bool:
    coverage = plan.get("coverage")
    if coverage != "complete":
        return True
    error = plan.get("error")
    return error not in (None, {})


def _plan_items(plan: Mapping[str, object]) -> list[Mapping[str, object]]:
    page = plan.get("page")
    if not isinstance(page, Mapping):
        return []
    items = page.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _normalize_plan(value: object) -> Mapping[str, object] | None:
    """Accept the public payload or a direct immutable ``CurationPlanPage``."""

    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        return None
    mapped = to_dict()
    if not isinstance(mapped, Mapping):
        return None
    if "page" in mapped:
        return mapped
    if "items" in mapped and "plan_digest" in mapped:
        return {"coverage": mapped.get("coverage", "unavailable"), "page": mapped}
    return None


def _append_plan_line(lines: list[str], value: object, state: list[int]) -> bool:
    """Append one plan line under the bounded GUI presentation budget."""

    if state[0] < 0:
        return False
    text = str(value)
    if state[0] + len(text) + 1 > MAX_PRESENTATION_CHARACTERS:
        lines.append("  Presentación de propuestas acotada por tamaño.")
        state[0] = -1
        return False
    lines.append(text)
    state[0] += len(text) + 1
    return True


def _render_plan(plan: object | None, lines: list[str]) -> tuple[int, bool]:
    """Render persisted proposals without exposing mutation controls."""

    line_state = [sum(len(line) + 1 for line in lines)]

    def add(value: object) -> bool:
        return _append_plan_line(lines, value, line_state)

    add("Propuestas persistidas del CurationPlanPage")
    normalized_plan = _normalize_plan(plan)
    if normalized_plan is None:
        add("  No hay una página de propuestas disponible.")
        return 0, True
    page = normalized_plan.get("page")
    if not isinstance(page, Mapping):
        add("  La página de propuestas no tiene un contrato legible.")
        return 0, True
    coverage = _safe_optional(normalized_plan.get("coverage"), limit=32) or "unavailable"
    digest = _safe_optional(page.get("plan_digest"), limit=160)
    if not digest:
        digest = _safe_optional(normalized_plan.get("plan_digest"), limit=160)
    items = _plan_items(normalized_plan)
    raw_total = page.get("items_total")
    total = raw_total if isinstance(raw_total, int) and raw_total >= 0 else len(items)
    add(f"  Cobertura: {coverage} · propuestas: {len(items)} de {total}")
    if digest:
        add(f"  Plan digest: {digest}")
    counts = (
        ("inventario", page.get("inventory_files")),
        ("grupos duplicados", page.get("duplicate_groups")),
        ("propuestas de organización", page.get("organization_plans")),
        ("archivos vacíos", page.get("empty_files")),
    )
    count_text = " · ".join(
        f"{label}: {value}"
        for label, value in counts
        if isinstance(value, int) and value >= 0
    )
    if count_text:
        add(f"  Conteo publicado: {count_text}")
    error = normalized_plan.get("error")
    if isinstance(error, Mapping):
        detail = _safe_optional(error.get("message") or error.get("code"), limit=320)
        if detail:
            add(f"  Límite de cobertura: {detail}")
    next_cursor = page.get("next_cursor")
    if next_cursor is not None:
        add("  Página acotada: existen más propuestas en el estado persistido.")
    if not items:
        add("  No hay propuestas materializadas en esta página.")
        return 0, _plan_requires_attention(normalized_plan)
    for item in items[:50]:
        item_id = _safe_optional(item.get("item_id"), limit=200) or "sin identificador"
        kind = _safe_optional(item.get("kind"), limit=100) or "tipo desconocido"
        status = _safe_optional(item.get("status"), limit=100) or "estado desconocido"
        action = _safe_optional(item.get("action"), limit=140) or "acción no indicada"
        source = _safe_optional(item.get("source_path"), limit=320) or "origen no indicado"
        destination = _safe_optional(item.get("destination_path"), limit=320)
        add(f"  {item_id} · {kind} · {status} · {action}")
        add(f"    Origen: {source}")
        if destination:
            add(f"    Destino propuesto: {destination}")
        add(
            f"    Razón: {_safe_optional(item.get('reason'), limit=640) or 'no indicada'}"
        )
        locator = _plan_locator(item)
        if locator:
            add(f"    Localizador: {locator}")
        add(f"    Siguiente paso: {_plan_next_step(item)}")
    return len(items), _plan_requires_attention(normalized_plan)


def _state(snapshot: CurationReadSnapshot) -> ReadVisualState:
    if snapshot.status == "unavailable":
        return "warning"
    return "warning" if snapshot.recovery else "completed"


def _render_grants(snapshot: CurationReadSnapshot, lines: list[str]) -> None:
    lines.append(f"Grants registrados: {len(snapshot.grants)}")
    for grant in snapshot.grants:
        lines.append(
            f"  { _safe(grant.grant_id, limit=160) } · { _safe(grant.action, limit=32) } "
            f"· actor declarado: { _safe(grant.actor, limit=128) }"
        )
        lines.append(
            f"    Plan: {_safe(grant.plan_digest, limit=80)} · "
            f"raíz: {_safe(grant.root, limit=240)}"
        )
        lines.append(
            f"    Efectos: {grant.effect_count}/{grant.max_actions} · bytes máximos: "
            f"{grant.max_bytes:,} · principal: no autenticado"
        )
        lines.append(
            f"    Receipt: {_safe(grant.receipt_digest, limit=80)} · "
            f"estado {_safe(grant.receipt_state, limit=32)}"
        )
    if not snapshot.grants:
        lines.append("  No hay AuthorizationGrants publicados.")


def _render_attempts(snapshot: CurationReadSnapshot, lines: list[str]) -> None:
    lines.append(f"Intentos registrados: {len(snapshot.attempts)}")
    for attempt in snapshot.attempts:
        target = (
            ""
            if attempt.target_path is None
            else f" → {_safe(attempt.target_path, limit=240)}"
        )
        lines.append(
            f"  #{attempt.action_id} · {_safe(attempt.action_type, limit=64)} · "
            f"{_safe(attempt.status, limit=32)}"
        )
        lines.append(f"    {_safe(attempt.source_path, limit=240)}{target}")
        if attempt.grant_id or attempt.effect_id:
            lines.append(
                f"    Grant: {_safe(attempt.grant_id or 'no identificado', limit=160)} · "
                f"efecto: {_safe(attempt.effect_id or 'no identificado', limit=160)}"
            )
        receipt = attempt.receipt_digest or "sin receipt"
        lines.append(
            f"    Receipt: {_safe(receipt, limit=80)} · {_safe(attempt.receipt_state, limit=32)}"
        )
        if attempt.detail:
            lines.append(f"    Detalle: {_safe(attempt.detail, limit=320)}")
    if not snapshot.attempts:
        lines.append("  No hay file_actions registrados.")


def _render_recovery(snapshot: CurationReadSnapshot, lines: list[str]) -> None:
    lines.append(f"Recovery pendiente: {len(snapshot.recovery)}")
    for item in snapshot.recovery:
        classification = item.classification or "sin observación"
        recommendation = item.recommendation or "revisión manual"
        lines.append(
            f"  Acción #{item.action_id} · {_safe(item.status, limit=32)} · "
            f"{_safe(classification, limit=96)}"
        )
        lines.append(f"    Recomendación: {_safe(recommendation, limit=160)}")
        if item.detail:
            lines.append(f"    Detalle: {_safe(item.detail, limit=320)}")
    if not snapshot.recovery:
        lines.append("  No hay acciones applying o recovery_required.")


def present_curation_snapshot(
    snapshot: CurationReadSnapshot,
    *,
    plan: object | None = None,
) -> ReadPresentation:
    """Render a bounded read-only view; no apply/restore controls are created."""

    if snapshot.status == "unavailable":
        detail = _safe(snapshot.error_detail or "estado Framework no disponible", limit=800)
        if plan is None:
            body = (
                "No se pudo leer la vista durable de curación.\n"
                "No se creó, migró ni modificó estado, corpus o sistemas externos."
            )
        else:
            lines = [
                "La vista de grants/recovery no está disponible.",
                f"Detalle: {detail}",
                "Las propuestas se muestran sólo si su página persistida es legible.",
                "",
            ]
            _render_plan(plan, lines)
            lines.extend(
                (
                    "",
                    "No se creó, migró ni modificó estado, corpus o sistemas externos.",
                )
            )
            body = "\n".join(lines)
        return ReadPresentation(
            title="Curación no disponible",
            summary=detail,
            body=body,
            state="warning",
        )
    lines = [
        "Vista durable de curación y recovery",
        "Lectura local bounded; el actor declarado no prueba un principal autenticado.",
        "No hay controles de aplicar o restaurar en esta superficie.",
        "",
    ]
    plan_count, plan_attention = _render_plan(plan, lines) if plan is not None else (0, False)
    lines.append("")
    _render_grants(snapshot, lines)
    lines.append("")
    _render_attempts(snapshot, lines)
    lines.append("")
    _render_recovery(snapshot, lines)
    summary = (
        f"{len(snapshot.grants)} grants · {len(snapshot.attempts)} intentos · "
        f"{len(snapshot.recovery)} requieren atención"
    )
    if plan is not None:
        summary += f" · {plan_count} propuestas"
    return ReadPresentation(
        title="Curación y recovery",
        summary=summary,
        body="\n".join(lines),
        state="warning" if plan_attention else _state(snapshot),
    )


__all__ = (
    "CurationReadRepository",
    "present_curation_snapshot",
)
