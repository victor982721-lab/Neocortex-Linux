"""Human and machine-readable adapter for conservative value review."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from typing import TextIO

from neocortex.workflow.review.value_review_port import (
    KnowledgeExitCode,
    ValueReviewAvailability,
    ValueReviewPaths,
    ValueReviewQuery,
    ValueReviewReport,
    ValueReviewTaskQueueStatus,
    preview_value_review,
    read_value_review_task_queue,
    refresh_value_review_tasks,
)

from .read_api import (
    FEDERATION_POLICY,
    MAX_HUMAN_RESULTS_PER_SCOPE,
    ReadScope,
    federated_exit_code,
    scope_bindings,
)


VALUE_REVIEW_API_SCHEMA = "neocortex.value-review/v1"
VALUE_REVIEW_REFRESH_API_SCHEMA = "neocortex.value-review-refresh/v1"
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


def _report_mapping_exit_code(report: Mapping[str, object]) -> KnowledgeExitCode:
    availability = str(report.get("availability", "unavailable"))
    returned = report.get("returned_count", 0)
    if availability == ValueReviewAvailability.READY.value:
        return (
            KnowledgeExitCode.SUCCESS
            if isinstance(returned, int) and not isinstance(returned, bool) and returned > 0
            else KnowledgeExitCode.NO_RESULTS
        )
    if availability == ValueReviewAvailability.PARTIAL.value:
        return KnowledgeExitCode.PARTIAL
    reason = str(report.get("reason") or "").casefold()
    if "corrupt" in reason:
        return KnowledgeExitCode.CORRUPT
    if "incompatible" in reason or "future" in reason:
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    if "absent" in reason:
        return KnowledgeExitCode.NO_RESULTS
    return KnowledgeExitCode.FATAL


def _unavailable_reason_exit_code(reason: str | None) -> KnowledgeExitCode:
    normalized = (reason or "").casefold()
    if "corrupt" in normalized:
        return KnowledgeExitCode.CORRUPT
    if "incompatible" in normalized or "future" in normalized:
        return KnowledgeExitCode.SCHEMA_INCOMPATIBLE
    if "absent" in normalized:
        return KnowledgeExitCode.NO_RESULTS
    return KnowledgeExitCode.FATAL


def _state_error_entry(
    *,
    scope: str,
    state_directory: object,
    error: BaseException,
) -> dict[str, object]:
    reason = str(error)
    exit_code = _unavailable_reason_exit_code(reason)
    error_name = type(error).__name__.casefold()
    if exit_code is KnowledgeExitCode.FATAL and (
        error_name == "reviewtaskrepositoryerror"
        or "integrity" in error_name
        or "schemacontract" in error_name
    ):
        exit_code = KnowledgeExitCode.CORRUPT
    return {
        "scope": scope,
        "state_directory": str(state_directory),
        "status": ("error" if exit_code is KnowledgeExitCode.FATAL else "unavailable"),
        "exit_code": int(exit_code),
        "error_type": type(error).__name__,
        "reason": reason,
    }


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
            paths = ValueReviewPaths.from_directory(binding.state_directory)
            queue = read_value_review_task_queue(
                binding.state_directory / "framework.sqlite3",
                paths,
                scope=binding.scope.value,
                limit=bounded_limit,
                reference_time_ns=reference_time_ns,
            )
            entry: dict[str, object]
            if queue.status is ValueReviewTaskQueueStatus.ABSENT:
                report = preview_value_review(
                    paths,
                    ValueReviewQuery(
                        limit=bounded_limit,
                        reference_time_ns=reference_time_ns,
                    ),
                )
                entry = {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": report.availability.value,
                    "exit_code": int(_report_exit_code(report)),
                    "report": report.to_dict(),
                    "source": "published_owner_preview",
                }
            else:
                queue_report = queue.report_dict()
                entry = {
                    "scope": binding.scope.value,
                    "state_directory": str(binding.state_directory),
                    "status": queue.status.value,
                    "exit_code": int(_report_mapping_exit_code(queue_report)),
                    "report": queue_report,
                    "source": "durable_review_task_queue",
                }
            entries.append(entry)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            entries.append(
                _state_error_entry(
                    scope=binding.scope.value,
                    state_directory=binding.state_directory,
                    error=exc,
                )
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


def value_review_refresh_payload(
    scope: str | ReadScope = ReadScope.PERSONAL,
    *,
    limit: int = 50,
    clock_ns: ClockNs = time.time_ns,
) -> dict[str, object]:
    """Advance exactly one durable queue page in one fixed public scope."""

    bounded_limit = _validate_limit(limit)
    selected = scope if isinstance(scope, ReadScope) else ReadScope(scope)
    if selected is ReadScope.ALL:
        raise ValueError("review value --refresh requires personal or framework scope")
    bindings = scope_bindings(selected)
    if len(bindings) != 1:
        raise RuntimeError("review value refresh did not resolve exactly one scope")
    reference_time_ns = clock_ns()
    if (
        isinstance(reference_time_ns, bool)
        or not isinstance(reference_time_ns, int)
        or reference_time_ns <= 0
    ):
        raise RuntimeError("value review refresh clock returned an invalid timestamp")
    binding = bindings[0]
    paths = ValueReviewPaths.from_directory(binding.state_directory)
    try:
        refresh = refresh_value_review_tasks(
            binding.state_directory / "framework.sqlite3",
            paths,
            scope=binding.scope.value,
            clock_ns=lambda: reference_time_ns,
        )
        queue = read_value_review_task_queue(
            binding.state_directory / "framework.sqlite3",
            paths,
            scope=binding.scope.value,
            limit=bounded_limit,
            reference_time_ns=reference_time_ns,
        )
        report = queue.report_dict()
        if refresh.status == "snapshot_changed":
            exit_code = KnowledgeExitCode.SNAPSHOT_CHANGED
        elif refresh.status == "unavailable":
            exit_code = _unavailable_reason_exit_code(refresh.reason)
        else:
            exit_code = _report_mapping_exit_code(report)
        current_status = (
            refresh.status
            if refresh.status in {"snapshot_changed", "unavailable"}
            else queue.status.value
        )
        entry: dict[str, object] = {
            "scope": binding.scope.value,
            "state_directory": str(binding.state_directory),
            "status": current_status,
            "exit_code": int(exit_code),
            "refresh": refresh.to_dict(),
            "report": report,
            "source": "durable_review_task_queue",
        }
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        entry = _state_error_entry(
            scope=binding.scope.value,
            state_directory=binding.state_directory,
            error=exc,
        )
    return {
        "schema": VALUE_REVIEW_REFRESH_API_SCHEMA,
        "kind": "neocortex_scoped_value_review_refresh",
        "operation": "value-task-refresh",
        "read_only": False,
        "advisory_only": True,
        "mutation_authorized": False,
        "scope_requested": selected.value,
        "federation_policy": FEDERATION_POLICY,
        "reference_time_ns": reference_time_ns,
        "limit_per_scope": bounded_limit,
        "exit_code": federated_exit_code((entry,)),
        "scopes": [entry],
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
        review_task = item.get("review_task")
        if isinstance(review_task, dict):
            _print(
                "   Tarea durable: "
                f"{review_task.get('task_id', 'sin id')} · "
                f"{review_task.get('state', 'estado desconocido')} · "
                f"v{review_task.get('task_version', '?')}"
            )
    if not rows:
        _print("  No hay candidatos publicados dentro de este límite.")


def _payload_exit_code(payload: Mapping[str, object]) -> int:
    value = payload.get("exit_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def run_value_review(
    *,
    scope: str,
    limit: int,
    json_output: bool,
    refresh: bool = False,
) -> int:
    """Run the canonical human value-preview command."""

    payload = (
        value_review_refresh_payload(scope, limit=limit)
        if refresh
        else value_review_payload(scope, limit=limit)
    )
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
    _print(
        "Actualización acotada de la cola de revisión"
        if refresh
        else "Revisión conservadora de valor (solo lectura)"
    )
    _print("Las recomendaciones no autorizan mover, archivar ni borrar archivos.")
    entries = payload.get("scopes")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                _render_entry(entry)
    return _payload_exit_code(payload)


__all__ = (
    "VALUE_REVIEW_API_SCHEMA",
    "VALUE_REVIEW_REFRESH_API_SCHEMA",
    "run_value_review",
    "value_review_payload",
    "value_review_refresh_payload",
)
