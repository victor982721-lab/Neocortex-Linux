"""Validation and normalization for immutable Knowledge telemetry contracts.
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/knowledge_contract_telemetry.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The public dataclasses remain in ``knowledge_contracts`` to preserve their
historical identity and pickle metadata. Runtime imports in this module never
point back to that facade.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar
# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from .knowledge_contract_protocols import (
        KnowledgePhaseTiming,
        KnowledgeQueryTelemetry,
        KnowledgeTelemetryClock,
    )

ClockT = TypeVar("ClockT")
RequiredText = Callable[[str, str], str]
OptionalText = Callable[[str, str | None], str | None]


def validate_telemetry_clock(
    contract: KnowledgeTelemetryClock,
    *,
    required_text_fn: RequiredText,
    default_signature: str,
    max_signature_chars: int,
    perf_counter_ns: Callable[[], int],
) -> None:
    if not callable(contract.read_ns):
        raise ValueError("Knowledge telemetry clock must be callable")
    if not isinstance(contract.signature, str):
        raise ValueError("Knowledge telemetry clock signature must be text")
    required_text_fn("Knowledge telemetry clock signature", contract.signature)
    if contract.signature != contract.signature.strip():
        raise ValueError("Knowledge telemetry clock signature cannot have outer whitespace")
    if len(contract.signature) > max_signature_chars:
        raise ValueError("Knowledge telemetry clock signature is too long")
    if contract.signature == default_signature and contract.read_ns is not perf_counter_ns:
        raise ValueError("python-perf-counter-ns-v1 is reserved for time.perf_counter_ns")


def telemetry_clock_from_legacy(
    cls: Callable[..., ClockT],
    clock_ns: Callable[[], int] | None,
    *,
    perf_counter_ns: Callable[[], int],
    unidentified_signature: str,
) -> ClockT:
    if clock_ns is None or clock_ns is perf_counter_ns:
        return cls()
    return cls(clock_ns, unidentified_signature)


def telemetry_clock_identified(signature: str, *, unidentified_signature: str) -> bool:
    return signature != unidentified_signature


def telemetry_clock_compatible(
    signature: str,
    expected_signature: str,
    *,
    identified: bool,
    trust_unidentified: bool,
) -> bool:
    return signature == expected_signature and (identified or trust_unidentified)


def telemetry_clock_now_ns(read_ns: Callable[[], int]) -> int:
    value = read_ns()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("Knowledge telemetry clock returned an invalid value")
    return value


def _require_non_negative_int(value: object, message: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(message)


def _validate_phase_scalar_types(
    contract: KnowledgePhaseTiming,
    *,
    timing_phase_type: Any,
) -> None:
    if not isinstance(contract.phase, timing_phase_type):
        raise ValueError("Knowledge timing phase is invalid")
    _require_non_negative_int(
        contract.duration_ns,
        "Knowledge timing duration_ns cannot be negative",
    )
    if (
        isinstance(contract.service_attempt, bool)
        or not isinstance(contract.service_attempt, int)
        or not 0 <= contract.service_attempt <= 2
    ):
        raise ValueError("Knowledge timing service_attempt must be zero, one or two")
    if not isinstance(contract.executed, bool):
        raise ValueError("Knowledge timing executed must be boolean")


def _validate_phase_optional_text(
    contract: KnowledgePhaseTiming,
    *,
    optional_text_fn: OptionalText,
    max_name_chars: int,
    max_snapshot_id_chars: int,
) -> None:
    if contract.owner is not None and not isinstance(contract.owner, str):
        raise ValueError("Knowledge timing owner must be text when present")
    if contract.snapshot_id is not None and not isinstance(contract.snapshot_id, str):
        raise ValueError("Knowledge timing snapshot_id must be text when present")
    optional_text_fn("Knowledge timing owner", contract.owner)
    optional_text_fn("Knowledge timing snapshot_id", contract.snapshot_id)
    if contract.owner is not None and len(contract.owner) > max_name_chars:
        raise ValueError("Knowledge timing owner is too long")
    if contract.snapshot_id is not None and len(contract.snapshot_id) > max_snapshot_id_chars:
        raise ValueError("Knowledge timing snapshot_id is too long")


def _validate_phase_rankings(
    contract: KnowledgePhaseTiming,
    *,
    required_text_fn: RequiredText,
    max_name_chars: int,
    max_rankings_per_phase: int,
    max_ranking_chars_per_phase: int,
) -> None:
    if not isinstance(contract.ranking_names, tuple):
        raise ValueError("Knowledge timing ranking_names must be a tuple")
    if len(contract.ranking_names) > max_rankings_per_phase:
        raise ValueError("Knowledge timing has too many ranking names")
    for ranking_name in contract.ranking_names:
        if not isinstance(ranking_name, str):
            raise ValueError("Knowledge timing ranking name must be text")
        required_text_fn("Knowledge timing ranking name", ranking_name)
        if len(ranking_name) > max_name_chars:
            raise ValueError("Knowledge timing ranking name is too long")
    if sum(len(name) for name in contract.ranking_names) > max_ranking_chars_per_phase:
        raise ValueError("Knowledge timing ranking names are too large")
    if len(set(contract.ranking_names)) != len(contract.ranking_names):
        raise ValueError("Knowledge timing ranking names must be unique")


def _validate_phase_scope(
    contract: KnowledgePhaseTiming,
    *,
    timing_phase_type: Any,
) -> None:
    attempt_phases = {
        timing_phase_type.SNAPSHOT_BEFORE,
        timing_phase_type.OWNER_RANKING,
        timing_phase_type.FUSION,
        timing_phase_type.BROKER,
        timing_phase_type.SNAPSHOT_AFTER,
    }
    if contract.phase in attempt_phases and contract.service_attempt not in {1, 2}:
        raise ValueError("attempt-scoped Knowledge timing requires attempt one or two")
    if contract.phase not in attempt_phases and contract.service_attempt != 0:
        raise ValueError("operation-scoped Knowledge timing must use attempt zero")
    if contract.phase is timing_phase_type.OWNER_RANKING:
        if contract.owner is None or not contract.ranking_names:
            raise ValueError("owner_ranking timing requires owner and ranking names")
    elif contract.owner is not None or contract.ranking_names:
        raise ValueError("only owner_ranking timing may identify owners or rankings")
    if contract.phase in {
        timing_phase_type.SNAPSHOT_BEFORE,
        timing_phase_type.SNAPSHOT_AFTER,
    }:
        if contract.snapshot_id is None:
            raise ValueError("snapshot timing requires snapshot_id")
    elif contract.snapshot_id is not None:
        raise ValueError("only snapshot timing may identify a snapshot")


def validate_phase_timing(
    contract: KnowledgePhaseTiming,
    *,
    timing_phase_type: Any,
    required_text_fn: RequiredText,
    optional_text_fn: OptionalText,
    max_name_chars: int,
    max_snapshot_id_chars: int,
    max_rankings_per_phase: int,
    max_ranking_chars_per_phase: int,
) -> None:
    _validate_phase_scalar_types(contract, timing_phase_type=timing_phase_type)
    _validate_phase_optional_text(
        contract,
        optional_text_fn=optional_text_fn,
        max_name_chars=max_name_chars,
        max_snapshot_id_chars=max_snapshot_id_chars,
    )
    _validate_phase_rankings(
        contract,
        required_text_fn=required_text_fn,
        max_name_chars=max_name_chars,
        max_rankings_per_phase=max_rankings_per_phase,
        max_ranking_chars_per_phase=max_ranking_chars_per_phase,
    )
    _validate_phase_scope(contract, timing_phase_type=timing_phase_type)


def _validate_query_header(
    contract: KnowledgeQueryTelemetry,
    *,
    telemetry_operation_type: Any,
    required_text_fn: RequiredText,
    max_clock_signature_chars: int,
) -> None:
    if not isinstance(contract.operation, telemetry_operation_type):
        raise ValueError("Knowledge telemetry operation is invalid")
    _require_non_negative_int(
        contract.total_duration_ns,
        "Knowledge telemetry total_duration_ns cannot be negative",
    )
    if not isinstance(contract.clock_signature, str):
        raise ValueError("Knowledge telemetry clock signature must be text")
    required_text_fn("Knowledge telemetry clock signature", contract.clock_signature)
    if len(contract.clock_signature) > max_clock_signature_chars:
        raise ValueError("Knowledge telemetry clock signature is too long")


def _validate_query_phases(
    contract: KnowledgeQueryTelemetry,
    *,
    phase_timing_type: type,
    max_phases: int,
) -> None:
    if not isinstance(contract.phases, tuple):
        raise ValueError("Knowledge telemetry phases must be a tuple")
    if not contract.phases:
        raise ValueError("Knowledge telemetry requires at least one phase")
    if len(contract.phases) > max_phases:
        raise ValueError("Knowledge telemetry has too many phase records")
    if any(not isinstance(phase, phase_timing_type) for phase in contract.phases):
        raise ValueError("Knowledge telemetry phases are invalid")


def _validate_query_topology(
    contract: KnowledgeQueryTelemetry,
    *,
    telemetry_operation_type: Any,
    timing_phase_type: Any,
) -> None:
    context_phases = sum(
        phase.phase is timing_phase_type.CONTEXT_COMPILE for phase in contract.phases
    )
    if contract.operation is telemetry_operation_type.SEARCH and context_phases:
        raise ValueError("search telemetry cannot contain context compilation")
    if contract.operation is telemetry_operation_type.CONTEXT and context_phases != 1:
        raise ValueError("context telemetry requires one context compilation phase")


def validate_query_telemetry(
    contract: KnowledgeQueryTelemetry,
    *,
    telemetry_operation_type: Any,
    phase_timing_type: type,
    timing_phase_type: Any,
    required_text_fn: RequiredText,
    max_clock_signature_chars: int,
    max_phases: int,
) -> None:
    _validate_query_header(
        contract,
        telemetry_operation_type=telemetry_operation_type,
        required_text_fn=required_text_fn,
        max_clock_signature_chars=max_clock_signature_chars,
    )
    _validate_query_phases(
        contract,
        phase_timing_type=phase_timing_type,
        max_phases=max_phases,
    )
    _validate_query_topology(
        contract,
        telemetry_operation_type=telemetry_operation_type,
        timing_phase_type=timing_phase_type,
    )


__all__ = [
    "telemetry_clock_compatible",
    "telemetry_clock_from_legacy",
    "telemetry_clock_identified",
    "telemetry_clock_now_ns",
    "validate_phase_timing",
    "validate_query_telemetry",
    "validate_telemetry_clock",
]
# endregion [02]
