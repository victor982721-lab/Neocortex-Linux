"""Read-only CLI adapter for the federated machine-inventory owner.

The command in this module is intentionally a leaf of the public CLI.  It
accepts explicit machine roots, delegates the observation to the owner lazily,
and renders one small, renderer-safe envelope.  It does not know how to walk a
machine root and it never imports the inventory implementation while the
parser or the ordinary framework CLI is being imported.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text

__all__ = [
    "MACHINE_INVENTORY_SCHEMA",
    "run_machine_inventory",
]


MACHINE_INVENTORY_SCHEMA = "neocortex.machine-inventory/v1"
_OWNER_MODULE = "neocortex.runtime.machine_inventory"
_MAX_HUMAN_TEXT = 800

# The runtime owner is allowed to evolve its report without widening the CLI
# envelope.  These fields are the intentionally small summary projection.  A
# complete owner report remains available below ``result`` after the shared
# sanitizer has bounded it; no owner key can replace envelope metadata.
_SUMMARY_FIELDS = (
    "status",
    "coverage",
    "truncated",
    "scanned",
    "returned",
    "entries",
    "entries_returned",
    "errors",
    "warnings",
    "counts",
    "machines",
    "records",
    "root_results",
    "reason",
    "reason_code",
    "records_returned",
    "records_truncated",
    "truncation_reasons",
    "state_counts",
    "category_counts",
    "reason_counts",
    "root_status_counts",
    "reason_summary",
    "reason_explanations",
    "aggregates",
    "categories",
    "category_registry",
    "category_specs",
    "owners",
    "owner_registry",
    "provenance",
    "provenance_registry",
    "observed_bytes",
    "observed_apparent_bytes",
    "observed_allocated_bytes",
    "bytes",
    "metrics",
    "metadata_only",
    "content_read",
    "sqlite_read",
    "network_used",
    "kio_used",
    "mutated",
)


def _owner_mapping(value: object) -> dict[str, object]:
    """Convert a bounded owner result to a mapping without importing policy."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}

    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        to_dict = getattr(value, "as_dict", None)
    if callable(to_dict):
        try:
            converted = to_dict()
        except Exception:
            # The call is only a presentation conversion.  The owner result
            # itself is retained as a safe scalar below if conversion fails.
            converted = None
        if isinstance(converted, Mapping):
            return {str(key): item for key, item in converted.items()}

    if is_dataclass(value) and not isinstance(value, type):
        try:
            converted = asdict(value)
        except Exception:
            converted = None
        if isinstance(converted, Mapping):
            return {str(key): item for key, item in converted.items()}

    if isinstance(value, (list, tuple)):
        return {"items": value}
    return {"value": value}


def _safe_payload(value: object) -> object:
    """Sanitize hostile owner data without allowing rendering to fail."""

    try:
        return sanitize_untrusted_payload(value)
    except Exception:
        # A malformed mapping (for example, keys that collide after ANSI
        # removal) is a diagnostic condition, not permission to print raw
        # producer data.  Keep the envelope closed and useful.
        return {"__neocortex_sanitization_truncated__": "[contenido omitido por límite]"}


def _safe_text(value: object, *, limit: int = _MAX_HUMAN_TEXT) -> str:
    try:
        return sanitize_untrusted_text(value, limit=limit, single_line=True)
    except Exception:
        return "[contenido omitido por límite]"


def _enum_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    return value


def _owner_callable(
    module: object,
    roots: list[Path] | None,
    limits: Mapping[str, int],
) -> Callable[[], object] | Callable[..., object] | None:
    """Resolve a supported owner callable after the leaf has been selected."""

    # Prefer functions: they are the smallest stable owner seam for a
    # diagnostic.  The class fallbacks preserve compatibility with owners
    # modelled after ExternalMaintenanceManager.
    function_names = (
        "collect_machine_inventory",
        "run_machine_inventory",
        "inspect_machine_inventory",
        "scan_machine_inventory",
        "diagnose_machine_inventory",
        "machine_inventory",
        "collect_inventory",
        "collect",
        "run",
    )
    for name in function_names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate

    class_names = (
        "MachineInventory",
        "MachineInventoryManager",
        "MachineInventoryCollector",
        "MachineInventoryService",
    )
    method_names = ("collect", "inspect", "diagnose", "scan", "run", "plan")
    for class_name in class_names:
        candidate = getattr(module, class_name, None)
        if not callable(candidate):
            continue
        # Return a small closure so constructor and report method invocation
        # use the same argument-shape adaptation as function owners.
        def invoke_class(
            *,
            _candidate: Callable[..., object] = candidate,
            _method_names: tuple[str, ...] = method_names,
        ) -> object:
            instance = _invoke_with_limits(_candidate, roots, limits)
            for method_name in _method_names:
                method = getattr(instance, method_name, None)
                if callable(method):
                    return _invoke_with_limits(method, roots, limits)
            return instance

        return invoke_class
    return None


def _parameter_value(
    parameter: inspect.Parameter,
    roots: list[Path] | None,
    limits: Mapping[str, int],
) -> tuple[bool, object]:
    """Map common owner parameter names to the CLI request values."""

    aliases: dict[str, object] = {
        "roots": roots,
        "machine_roots": roots,
        "root_paths": roots,
        "paths": roots,
        "root": roots[0] if roots is not None and len(roots) == 1 else roots,
        "max_entries": limits["max_entries"],
        "entry_limit": limits["max_entries"],
        "max_depth": limits["max_depth"],
        "depth_limit": limits["max_depth"],
        "max_bytes": limits["max_bytes"],
        "byte_limit": limits["max_bytes"],
        "bytes_limit": limits["max_bytes"],
        "limits": dict(limits),
        "budget": dict(limits),
    }
    if parameter.name in aliases:
        return True, aliases[parameter.name]
    return False, None


def _invoke_with_limits(
    target: Callable[..., object],
    roots: list[Path] | None,
    limits: Mapping[str, int],
) -> object:
    """Invoke a known owner seam without guessing after a runtime TypeError.

    Owner functions normally use ``roots`` plus the three named limits.  The
    signature inspection only adds compatibility for equivalent names and
    positional-only parameters; a TypeError raised *inside* the owner is not
    retried, so an owner cannot be accidentally executed twice.
    """

    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(
            roots=roots,
            max_entries=limits["max_entries"],
            max_depth=limits["max_depth"],
            max_bytes=limits["max_bytes"],
        )

    parameters = tuple(signature.parameters.values())
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return target(
            roots=roots,
            max_entries=limits["max_entries"],
            max_depth=limits["max_depth"],
            max_bytes=limits["max_bytes"],
        )

    positional: list[object] = []
    keyword: dict[str, object] = {}
    fallback_values = iter(
        (
            roots,
            limits["max_entries"],
            limits["max_depth"],
            limits["max_bytes"],
        )
    )
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        found, value = _parameter_value(parameter, roots, limits)
        if not found:
            if parameter.default is not inspect.Parameter.empty:
                continue
            try:
                value = next(fallback_values)
            except StopIteration as exc:
                raise TypeError(
                    f"unsupported machine-inventory owner parameter {parameter.name!r}"
                ) from exc
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            keyword[parameter.name] = value
    return target(*positional, **keyword)


def _call_owner(
    roots: list[Path],
    *,
    max_entries: int,
    max_depth: int,
    max_bytes: int,
) -> object:
    """Import and call the runtime owner only after CLI validation."""

    limits = {
        "max_entries": max_entries,
        "max_depth": max_depth,
        "max_bytes": max_bytes,
    }
    module = importlib.import_module(_OWNER_MODULE)
    # ``None`` is the runtime owner's explicit signal to select its bounded
    # default profile.  Passing an empty iterable would instead request an
    # empty inventory and would silently defeat the documented default scan.
    owner_roots = roots if roots else None
    owner = _owner_callable(module, owner_roots, limits)
    if owner is None:
        raise RuntimeError("machine-inventory owner exposes no supported read method")
    return _invoke_with_limits(owner, owner_roots, limits)


def _error_payload(
    roots: Sequence[Path],
    limits: Mapping[str, int],
    *,
    error: BaseException,
) -> dict[str, object]:
    return {
        "schema": MACHINE_INVENTORY_SCHEMA,
        "operation": "machine-inventory",
        "kind": "machine_inventory_diagnostic",
        "read_only": True,
        "diagnostic_only": True,
        "mutation_authorized": False,
        "write_attempted": False,
        "status": "blocked",
        "coverage": "blocked",
        "exit_code": 0,
        "roots": [str(root) for root in roots],
        "root_count": len(roots),
        "limits": dict(limits),
        "result": {},
        "error": {
            "code": type(error).__name__,
            "message": sanitize_untrusted_text(error, limit=_MAX_HUMAN_TEXT, single_line=True),
        },
        "applied": 0,
    }


def _result_payload(
    owner_result: object,
    roots: Sequence[Path],
    limits: Mapping[str, int],
) -> dict[str, object]:
    """Project one owner result into the closed machine-inventory envelope."""

    raw = _owner_mapping(owner_result)
    safe_raw = _safe_payload(raw)
    safe_result = safe_raw if isinstance(safe_raw, Mapping) else {"value": safe_raw}

    raw_status = _enum_value(raw.get("status", "complete"))
    status = _safe_text(raw_status, limit=128) or "complete"
    raw_coverage = _enum_value(raw.get("coverage", status))
    coverage = _safe_text(raw_coverage, limit=128) or status

    # With no explicit ``--machine-root`` the runtime owner selects its
    # conservative profile roots.  Reflect those effective roots when the
    # owner publishes them; explicit CLI roots always retain precedence.
    effective_roots: object = [str(root) for root in roots]
    effective_root_count = len(roots)
    owner_roots = raw.get("roots")
    safe_owner_roots = _safe_payload(owner_roots)
    # Root result records carry the category/owner/provenance registry.  Keep
    # that structured projection at the envelope boundary instead of reducing
    # it to paths; a minimal test/double that does not publish roots still gets
    # the explicit path fallback above.
    if isinstance(safe_owner_roots, list) and safe_owner_roots:
        effective_roots = safe_owner_roots
        effective_root_count = len(safe_owner_roots)
    owner_root_count = raw.get("root_count")
    if isinstance(owner_root_count, int) and not isinstance(owner_root_count, bool):
        effective_root_count = max(0, owner_root_count)

    payload: dict[str, object] = {
        "schema": MACHINE_INVENTORY_SCHEMA,
        "operation": "machine-inventory",
        "kind": "machine_inventory_diagnostic",
        "read_only": True,
        "diagnostic_only": True,
        "mutation_authorized": False,
        "write_attempted": False,
        "status": status,
        "coverage": coverage,
        "exit_code": 0,
        "roots": effective_roots,
        "root_count": effective_root_count,
        "limits": dict(limits),
        "result": safe_result,
        "error": None,
        "applied": 0,
    }

    # These fields are stable summary aliases, not a second unbounded owner
    # object.  Their values are copied from the sanitized result only when the
    # owner supplies them and cannot overwrite the envelope's fixed fields.
    if isinstance(safe_result, Mapping):
        for field in _SUMMARY_FIELDS:
            if field in safe_result and field not in payload:
                payload[field] = safe_result[field]

        bytes_payload = safe_result.get("bytes")
        if isinstance(bytes_payload, Mapping):
            for output_name, source_name in (
                ("observed_bytes", "observed"),
                ("apparent_bytes", "apparent"),
                ("allocated_bytes", "allocated"),
            ):
                if output_name not in payload and source_name in bytes_payload:
                    payload[output_name] = bytes_payload[source_name]

        if "reason_summary" not in payload:
            reason_counts = safe_result.get("reason_counts")
            if isinstance(reason_counts, Mapping):
                payload["reason_summary"] = reason_counts

        if "category_registry" not in payload and isinstance(effective_roots, list):
            registry: list[object] = []
            seen: set[str] = set()
            for root_result in effective_roots:
                if not isinstance(root_result, Mapping):
                    continue
                entry = {
                    key: root_result[key]
                    for key in ("category", "owner", "provenance", "profile")
                    if key in root_result
                }
                if not entry:
                    continue
                marker = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
                if marker in seen:
                    continue
                seen.add(marker)
                registry.append(entry)
            if registry:
                payload["category_registry"] = registry
    return payload


def _json_requested(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "machine_json", False)
        or getattr(args, "json_output", False)
    )


def _human_counts(payload: Mapping[str, object]) -> tuple[object, object, object]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        result = {}
    scanned = result.get("scanned", result.get("entries", 0))
    returned = result.get("returned", result.get("entries_returned", scanned))
    truncated = result.get("truncated", False)
    return scanned, returned, truncated


def _emit(payload: Mapping[str, object], *, json_output: bool) -> None:
    safe_payload = _safe_payload(payload)
    if json_output:
        print(
            json.dumps(
                safe_payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return

    scanned, returned, truncated = _human_counts(payload)
    print(
        "MACHINE-INVENTORY "
        f"status={_safe_text(payload.get('status', 'complete'), limit=128)} "
        f"roots={payload.get('root_count', 0)} "
        f"scanned={_safe_text(scanned, limit=64)} "
        f"returned={_safe_text(returned, limit=64)} "
        f"truncated={_safe_text(truncated, limit=16)} "
        "read_only=true"
    )
    error = payload.get("error")
    if isinstance(error, Mapping):
        print(
            "DIAGNOSTIC machine-inventory "
            f"code={_safe_text(error.get('code'), limit=128)}: "
            f"{_safe_text(error.get('message'), limit=_MAX_HUMAN_TEXT)}"
        )


def run_machine_inventory(args: argparse.Namespace) -> int:
    """Run a bounded machine-inventory diagnostic and return the CLI code.

    Validation owns configuration errors and therefore returns code 2 before
    this function is called.  Once the request is valid, owner availability,
    permissions, truncation and other observation outcomes remain diagnostics;
    they are represented in the envelope and intentionally return code 0.
    """

    roots: list[Path] = list(getattr(args, "machine_root", None) or ())
    limits = {
        "max_entries": int(args.machine_max_entries),
        "max_depth": int(args.machine_max_depth),
        "max_bytes": int(args.machine_max_bytes),
    }
    try:
        owner_result = _call_owner(
            roots,
            max_entries=limits["max_entries"],
            max_depth=limits["max_depth"],
            max_bytes=limits["max_bytes"],
        )
        payload = _result_payload(owner_result, roots, limits)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        payload = _error_payload(roots, limits, error=exc)

    _emit(payload, json_output=_json_requested(args))
    return 0
