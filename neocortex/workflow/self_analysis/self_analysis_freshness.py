"""Pure freshness aggregation for protected self-analysis evidence."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/self_analysis_freshness.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from dataclasses import dataclass
from typing import Literal
# endregion [01]

# region [02] Implementación


JournalStatus = Literal["unchanged", "advanced", "discontinuous", "unavailable"]


@dataclass(frozen=True, slots=True)
class SelfAnalysisFreshness:
    """Positive-only freshness fences over independent durable owners."""

    root_identity_current: bool
    framework_link_current: bool
    inventory_checkpoint_current: bool
    journal_status: JournalStatus
    current: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "root_identity_current": self.root_identity_current,
            "framework_link_current": self.framework_link_current,
            "inventory_checkpoint_current": self.inventory_checkpoint_current,
            "journal_status": self.journal_status,
            "current": self.current,
        }


@dataclass(frozen=True, slots=True)
class FreshnessFences:
    """Independent pre/post observations needed for one freshness verdict."""

    root_before: bool
    root_after: bool
    framework_link_matches: bool
    code_before: bool
    framework_still_current: bool
    code_after: bool
    checkpoint_before_matches: bool
    checkpoint_unchanged: bool
    journal_status: JournalStatus


def evaluate_self_analysis_freshness(
    fences: FreshnessFences,
) -> SelfAnalysisFreshness:
    """Aggregate all positive fences; absence or change always fails closed."""

    root_identity_current = fences.root_before and fences.root_after
    framework_link_current = (
        fences.framework_link_matches
        and fences.code_before
        and fences.framework_still_current
        and fences.code_after
    )
    inventory_checkpoint_current = (
        fences.checkpoint_before_matches and fences.checkpoint_unchanged
    )
    current = (
        root_identity_current
        and framework_link_current
        and inventory_checkpoint_current
        and fences.journal_status == "unchanged"
    )
    return SelfAnalysisFreshness(
        root_identity_current,
        framework_link_current,
        inventory_checkpoint_current,
        fences.journal_status,
        current,
    )


__all__ = [
    "FreshnessFences",
    "JournalStatus",
    "SelfAnalysisFreshness",
    "evaluate_self_analysis_freshness",
]
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.self_analysis_freshness")
