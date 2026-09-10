"""Read-only desktop presentation for curation grants and recovery."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from neocortex.api.read_contract import sanitize_untrusted_text
from neocortex.curation.read import (
    CURATION_READ_MAX_ITEMS,
    CurationReadSnapshot,
    read_curation_snapshot,
)

from .models import ReadPresentation, ReadVisualState


class CurationReadRepository:
    """Read the fixed Framework owner without importing any writer."""

    def __init__(
        self,
        state_directory: Path,
        *,
        reader: Callable[..., CurationReadSnapshot] = read_curation_snapshot,
    ) -> None:
        self.state_directory = Path(state_directory)
        self._reader = reader

    @property
    def database_path(self) -> Path:
        return self.state_directory / "framework.sqlite3"

    def read(self, *, limit: int = CURATION_READ_MAX_ITEMS) -> CurationReadSnapshot:
        return self._reader(self.database_path, limit=limit)


def _safe(value: object, *, limit: int = 800) -> str:
    return sanitize_untrusted_text(value, limit=limit, single_line=True)


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


def present_curation_snapshot(snapshot: CurationReadSnapshot) -> ReadPresentation:
    """Render a bounded read-only view; no apply/restore controls are created."""

    if snapshot.status == "unavailable":
        detail = _safe(snapshot.error_detail or "estado Framework no disponible", limit=800)
        return ReadPresentation(
            title="Curación no disponible",
            summary=detail,
            body=(
                "No se pudo leer la vista durable de curación.\n"
                "No se creó, migró ni modificó estado, corpus o sistemas externos."
            ),
            state="warning",
        )
    lines = [
        "Vista durable de curación y recovery",
        "Lectura local bounded; el actor declarado no prueba un principal autenticado.",
        "No hay controles de aplicar o restaurar en esta superficie.",
        "",
    ]
    _render_grants(snapshot, lines)
    lines.append("")
    _render_attempts(snapshot, lines)
    lines.append("")
    _render_recovery(snapshot, lines)
    return ReadPresentation(
        title="Curación y recovery",
        summary=(
            f"{len(snapshot.grants)} grants · {len(snapshot.attempts)} intentos · "
            f"{len(snapshot.recovery)} requieren atención"
        ),
        body="\n".join(lines),
        state=_state(snapshot),
    )


__all__ = (
    "CurationReadRepository",
    "present_curation_snapshot",
)
