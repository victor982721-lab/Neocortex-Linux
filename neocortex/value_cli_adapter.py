"""Human and machine-readable adapter for conservative value review."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from typing import TextIO

from _04_Nucleo_Operativo.value_review_port import (
    KnowledgeExitCode,
    ValueReviewAvailability,
    ValueReviewPaths,
    ValueReviewQuery,
    ValueReviewReport,
    preview_value_review,
)

from .read_api import (
    FEDERATION_POLICY,
    MAX_HUMAN_RESULTS_PER_SCOPE,
    ReadScope,
    federated_exit_code,
    scope_bindings,
)


VALUE_REVIEW_API_SCHEMA = "neocortex.value-review/v1"
ClockNs = Callable[[], int]


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_HUMAN_RESULTS_PER_SCOPE:
        raise ValueError(f"limit must be between 1 and {MAX_HUMAN_RESULTS_PER_SCOPE} per scope")
    return limit


def _report_exit_code(report: ValueReviewReport) -> KnowledgeExitCode:
    if report.availability is ValueReviewAvailability.READY:
        return KnowledgeExitCode.SUCCESS if report.returned_count else KnowledgeExitCode.NO_RESULTS
    if report.availability is ValueReviewAvailability.PARTIAL:
        return KnowledgeExitCode.PARTIAL
    reason = (report.reason or "").casefold()
    if "corrupt" in reason:
        return KnowledgeExitCode.CORRUPT
    if "incompatible" in reason:
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    if "absent" in reason:
        return KnowledgeExitCode.NO_RESULTS
    return KnowledgeExitCode.FATAL


def value_review_payload(
    scope: str | ReadScope = ReadScope.PERSONAL,
    *,
    limit: int = 50,
    clock_ns: ClockNs = time.time_ns,
) -> dict[str, object]:
    """Review fixed published scopes without accepting paths or mutation flags."""

    bounded_limit = _validate_limit(limit)
    bindings = scope_bindings(scope)
    selected = scope if isinstance(scope, ReadScope) else ReadScope(scope)
    reference_time_ns = clock_ns()
    if (
        isinstance(reference_time_ns, bool)
        or not isinstance(reference_time_ns, int)
        or reference_time_ns < 0
    ):
        raise RuntimeError("value review clock returned an invalid timestamp")
    entries: list[dict[str, object]] = []
    for binding in bindings:
        try:
            report = preview_value_review(
                ValueReviewPaths.from_directory(binding.state_directory),
                ValueReviewQuery(
                    limit=bounded_limit,
                    reference_time_ns=reference_time_ns,
                ),
            )
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": report.availability.value,
                    "exit_code": int(_report_exit_code(report)),
                    "report": report.to_dict(),
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            entries.append(
                {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": "error",
                    "exit_code": int(KnowledgeExitCode.FATAL),
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            )
    return {
        "schema": VALUE_REVIEW_API_SCHEMA,
        "kind": "neocortex_scoped_value_review",
        "operation": "value-preview",
        "read_only": True,
        "advisory_only": True,
        "mutation_authorized": False,
        "scope_requested": selected.value,
        "federation_policy": FEDERATION_POLICY,
        "reference_time_ns": reference_time_ns,
        "limit_per_scope": bounded_limit,
        "exit_code": federated_exit_code(entries),
        "scopes": entries,
    }


def _console_text(value: str, stream: object) -> str:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except UnicodeEncodeError:
        return value.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:  # pragma: no cover - custom stream defense
        return value
    return value


def _print(value: str = "", *, file: TextIO | None = None) -> None:
    stream = sys.stdout if file is None else file
    print(_console_text(value, stream), file=stream)


def _size(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "tamaño desconocido"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable size unit")


_STATE_LABELS = {
    "keep": "conservar",
    "review_low_value": "revisar valor bajo",
    "archive_candidate": "revisar posible archivo",
    "exact_duplicate_candidate": "revisar duplicado exacto",
    "unknown": "desconocido/protegido",
}

_CODE_EXPLANATIONS = {
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


def _explain_code(value: object) -> str:
    text = str(value)
    if text.startswith("owner_health_protected:"):
        health = text.partition(":")[2]
        return f"se protege porque la salud de su fuente es {health}"
    return _CODE_EXPLANATIONS.get(text, text.replace("_", " "))


def _render_entry(entry: Mapping[str, object]) -> None:
    scope_label = {
        "personal": "Personal",
        "framework": "Framework",
    }.get(str(entry.get("scope")), str(entry.get("scope")))
    report = entry.get("report")
    if not isinstance(report, dict):
        _print(
            f"{scope_label}: no se pudo revisar ({entry.get('error_type', 'no disponible')}: "
            f"{entry.get('reason', 'sin detalle')})."
        )
        return
    items = report.get("items")
    rows = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    _print(
        f"\n{scope_label}: {entry.get('status')} · {len(rows)} de "
        f"{report.get('matched_count', 0)} candidatos mostrados."
    )
    if report.get("reason"):
        _print(f"  Cobertura: {_explain_code(report['reason'])}")
    for index, item in enumerate(rows, start=1):
        state = str(item.get("state", "unknown"))
        label = _STATE_LABELS.get(state, state)
        _print(
            f"{index}. [{label}] {item.get('path', 'sin ruta')} · {_size(item.get('size_bytes'))}"
        )
        reasons = item.get("reasons")
        if isinstance(reasons, list) and reasons:
            _print("   Evidencia: " + "; ".join(_explain_code(value) for value in reasons))
        uncertainties = item.get("uncertainties")
        if isinstance(uncertainties, list) and uncertainties:
            _print(
                "   Incertidumbre: " + "; ".join(_explain_code(value) for value in uncertainties)
            )
    if not rows:
        _print("  No hay candidatos publicados dentro de este límite.")


def _payload_exit_code(payload: Mapping[str, object]) -> int:
    value = payload.get("exit_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def run_value_review(*, scope: str, limit: int, json_output: bool) -> int:
    """Run the canonical human value-preview command."""

    payload = value_review_payload(scope, limit=limit)
    if json_output:
        _print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return _payload_exit_code(payload)
    _print("Revisión conservadora de valor (solo lectura)")
    _print("Las recomendaciones no autorizan mover, archivar ni borrar archivos.")
    entries = payload.get("scopes")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                _render_entry(entry)
    return _payload_exit_code(payload)


__all__ = (
    "VALUE_REVIEW_API_SCHEMA",
    "run_value_review",
    "value_review_payload",
)
