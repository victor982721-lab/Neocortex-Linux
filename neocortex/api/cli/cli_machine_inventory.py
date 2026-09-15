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
from dataclasses import fields, is_dataclass
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

# Presentation is a separate boundary from the runtime scanner.  The owner
# already supplies bounded records, but a caller may still request a very
# large global entry limit.  Keep the default renderer record-free and give an
# explicit full-record mode its own ceiling so a renderer cannot turn a
# bounded observation into an unbounded terminal/JSON write.
_MAX_PRESENTATION_ROOTS = 4_096
_MAX_PRESENTATION_RECORDS = 4_096
_MAX_PRESENTATION_COLLECTION_ITEMS = 2_048
_MAX_PRESENTATION_NODES = 20_000
_MAX_PRESENTATION_STRING = 128_000
_MAX_HUMAN_ROOTS = 64

_SCANNER_TRUNCATION_REASONS = frozenset(
    {
        "depth_limit",
        "entry_limit",
        "byte_limit",
        "cancelled",
        "scan_unavailable",
        "root_identity_changed",
        "identity_changed",
        "evidence_incomplete",
    }
)

# The runtime owner is allowed to evolve its report without widening the CLI
# envelope.  These fields are the intentionally small summary projection.  A
# complete owner report remains available below ``result`` after the shared
# sanitizer has bounded it; no owner key can replace envelope metadata.
_SUMMARY_FIELDS = (
    "schema",
    "operation",
    "kind",
    "status",
    "coverage",
    "scan_coverage",
    "presentation_coverage",
    "truncated",
    "scanner_truncated",
    "scanner_truncation_reasons",
    "presentation_truncated",
    "scanned",
    "records_scanned",
    "returned",
    "records_returned",
    "records_omitted",
    "records_omitted_known",
    "records_available",
    "records_included",
    "entries",
    "entries_returned",
    "errors",
    "warnings",
    "counts",
    "machines",
    "records",
    "root_results",
    "root_summaries",
    "summaries",
    "root_paths",
    "reason",
    "reason_code",
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
    "byte_semantics",
    "coverage_metadata",
    "omissions",
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

_ROOT_SUMMARY_FIELDS = (
    "root",
    "path",
    "root_path",
    "root_index",
    "category",
    "owner",
    "provenance",
    "profile",
    "status",
    "coverage",
    "reason_code",
    "reason",
    "root_identity",
    "identity",
    "root_uid",
    "root_gid",
    "root_mode",
    "root_nlink",
    "root_exists",
    "root_type",
    "root_is_directory",
    "counts",
    "scanned",
    "observed",
    "preserved",
    "blocked",
    "unknown",
    "out_of_profile",
    "absent",
    "records_scanned",
    "records_returned",
    "records_omitted",
    "records_omitted_known",
    "scanner_truncated",
    "scanner_truncation_reasons",
    "truncated",
    "truncation_reasons",
    "presentation_truncated",
    "bytes",
    "apparent_bytes",
    "allocated_bytes",
    "observed_bytes",
    "byte_semantics",
    "limits",
    "coverage_metadata",
    "omissions",
    "summary",
)

# The CLI root row is intentionally smaller than the Python owner's reusable
# summary.  Global ``limits``, byte semantics and scanner/presentation facets
# are emitted once at the envelope level; repeating them for every root was
# the main source of the oversized pasted JSON that motivated this change.
_ROOT_OUTPUT_FIELDS = tuple(
    field
    for field in _ROOT_SUMMARY_FIELDS
    if field
    not in {
        "byte_semantics",
        "limits",
        "coverage_metadata",
        "omissions",
        "summary",
    }
)

_OWNER_OBJECT_FIELDS = frozenset(
    {
        *_SUMMARY_FIELDS,
        "roots",
        "root_results",
        "root_summaries",
        "records",
        "items",
    }
)


def _object_mapping(value: object, *, field_names: Sequence[str]) -> dict[str, object]:
    """Read selected owner fields without recursively materializing records.

    ``MachineInventoryReport.to_dict()`` is intentionally the compatibility
    full projection and contains every record twice (at the top level and
    below each root).  The compact CLI must not call it merely to render the
    default summary.  Prefer the owner's ``to_summary_dict``/properties and
    inspect dataclass fields shallowly; old owners that expose only
    ``to_dict`` remain supported as a fallback.
    """

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}

    names = tuple(dict.fromkeys(field_names))
    result: dict[str, object] = {}
    for name in names:
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if callable(item) and name not in {"records", "roots", "root_results", "items"}:
            # A method with a public-looking name is not an owner field.  Do
            # not execute arbitrary producer code during a read projection.
            continue
        result[name] = item
    if result:
        return result

    if is_dataclass(value) and not isinstance(value, type):
        # This is intentionally shallow.  ``asdict`` recursively walks all
        # record payloads and would defeat the compact projection.
        for field in fields(value):
            if field.name not in names:
                continue
            try:
                result[field.name] = getattr(value, field.name)
            except Exception:
                continue
        if result:
            return result

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

    if isinstance(value, (list, tuple)):
        return {"items": value}
    return {"value": value}


def _owner_mapping(value: object, *, include_records: bool = False) -> dict[str, object]:
    """Convert one owner result to a shallow mapping for the CLI renderer."""

    if not include_records:
        # The new owner seam is intentionally record-free.  ``summary`` is a
        # property on some compatible owners and ``to_summary_dict`` is the
        # preferred explicit method; neither is called more than once.
        summary_method = getattr(value, "to_summary_dict", None)
        if callable(summary_method):
            try:
                converted = summary_method()
            except Exception:
                converted = None
            if isinstance(converted, Mapping):
                return {str(key): item for key, item in converted.items()}

        try:
            summary = object.__getattribute__(value, "summary")
        except Exception:
            summary = None
        if isinstance(summary, Mapping):
            return {str(key): item for key, item in summary.items()}

    if include_records:
        # Detail mode intentionally opts into the compatibility/full result.
        # The caller still applies an independent presentation cap below.
        return _object_mapping(value, field_names=(*_SUMMARY_FIELDS, "roots", "records", "items"))

    return _object_mapping(value, field_names=tuple(_OWNER_OBJECT_FIELDS))


def _root_mapping(value: object) -> dict[str, object]:
    """Read one root result shallowly, keeping nested records out."""

    return _object_mapping(value, field_names=(*_ROOT_SUMMARY_FIELDS, "records", "items"))


def _sequence(value: object) -> list[object] | None:
    """Return a materialized bounded sequence, excluding text-like values."""

    if isinstance(value, (list, tuple)):
        return list(value)
    return None


def _non_negative_int(value: object, default: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _bool_value(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _contains_marker(value: object) -> bool:
    """Detect sanitizer omission markers without using them as scanner state."""

    if isinstance(value, str):
        return value == "[contenido omitido por límite]"
    if isinstance(value, Mapping):
        return any(
            key == "__neocortex_sanitization_truncated__"
            or _contains_marker(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_marker(item) for item in value)
    return False


def _presentation_bound(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> tuple[object, bool]:
    """Bound renderer-owned collections before the shared sanitizer runs.

    The returned boolean describes only presentation/serialization clipping.
    It is deliberately never used to derive ``scanner_truncated`` or scanner
    reason codes; those values come from the owner result and root summaries.
    """

    if budget is None:
        budget = [_MAX_PRESENTATION_NODES]
    if budget[0] <= 0 or depth > 16:
        return None, True
    budget[0] -= 1

    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        truncated = False
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_PRESENTATION_COLLECTION_ITEMS or budget[0] <= 0:
                truncated = True
                break
            bounded, child_truncated = _presentation_bound(
                item, depth=depth + 1, budget=budget
            )
            result[key] = bounded
            truncated = truncated or child_truncated
        return result, truncated

    if isinstance(value, (list, tuple)):
        result_list: list[object] = []
        truncated = False
        for index, item in enumerate(value):
            if index >= _MAX_PRESENTATION_COLLECTION_ITEMS or budget[0] <= 0:
                truncated = True
                break
            bounded, child_truncated = _presentation_bound(
                item, depth=depth + 1, budget=budget
            )
            result_list.append(bounded)
            truncated = truncated or child_truncated
        return result_list, truncated

    if isinstance(value, str) and len(value.encode("utf-8", errors="replace")) > _MAX_PRESENTATION_STRING:
        # Keep a renderer-safe, deterministic prefix.  The final shared
        # sanitizer still strips controls/ANSI; this flag records that the
        # CLI itself shortened the producer value first.
        encoded = value.encode("utf-8", errors="replace")[:_MAX_PRESENTATION_STRING]
        return encoded.decode("utf-8", errors="replace"), True

    return value, False


def _bounded_list(values: Sequence[object], *, limit: int) -> tuple[list[object], bool]:
    """Bound one list and report whether its tail was presentation-clipped."""

    bounded = list(values[:limit])
    return bounded, len(values) > limit


def _safe_payload_with_status(value: object) -> tuple[object, bool]:
    """Sanitize one value and report renderer-side clipping separately."""

    try:
        safe = sanitize_untrusted_payload(value)
    except Exception:
        return (
            {"__neocortex_sanitization_truncated__": "[contenido omitido por límite]"},
            True,
        )
    return safe, _contains_marker(safe)


def _mark_presentation_truncated(value: object, *, reason: str = "sanitizer_limit") -> object:
    """Annotate a sanitized payload when its final renderer clipped it.

    The shared sanitizer is intentionally the last safety boundary and may
    insert its generic omission marker after the CLI has built its own
    ``serialization`` facet.  Keep scanner coverage untouched while making
    that renderer-side loss explicit at every envelope level that survived
    sanitization.
    """

    if not isinstance(value, Mapping):
        return value

    payload: dict[str, object] = {str(key): item for key, item in value.items()}

    def annotate_facet(facet: object) -> dict[str, object] | None:
        if not isinstance(facet, Mapping):
            return None
        updated = {str(key): item for key, item in facet.items()}
        updated["status"] = "partial"
        updated["truncated"] = True
        reasons = updated.get("reasons")
        reason_list = list(reasons) if isinstance(reasons, list) else []
        if reason not in reason_list:
            reason_list.append(reason)
        updated["reasons"] = reason_list
        return updated

    payload["presentation_truncated"] = True
    payload["serialization_truncated"] = True
    serialization = annotate_facet(payload.get("serialization"))
    if serialization is not None:
        payload["serialization"] = serialization

    for container_name in ("coverage_metadata", "omissions"):
        container = payload.get(container_name)
        if not isinstance(container, Mapping):
            continue
        updated_container = {
            str(key): item for key, item in container.items()
        }
        presentation = annotate_facet(updated_container.get("presentation"))
        if presentation is not None:
            updated_container["presentation"] = presentation
        payload[container_name] = updated_container

    nested_result = payload.get("result")
    if isinstance(nested_result, Mapping):
        updated_result = {str(key): item for key, item in nested_result.items()}
        updated_result["presentation_truncated"] = True
        updated_result["serialization_truncated"] = True
        nested_serialization = annotate_facet(updated_result.get("serialization"))
        if nested_serialization is not None:
            updated_result["serialization"] = nested_serialization
        payload["result"] = updated_result
    return payload


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


def _root_values(raw: Mapping[str, object]) -> list[object]:
    """Resolve whichever compatible root collection the owner publishes."""

    for name in ("root_summaries", "roots", "root_results", "machines"):
        candidate = raw.get(name)
        values = _sequence(candidate)
        if values is not None:
            return values
        if isinstance(candidate, Mapping):
            # A mapping keyed by path is accepted for older doubles.  A
            # single root summary mapping is not split into its scalar values.
            if any(key in candidate for key in ("root", "path", "root_path", "status")):
                return [candidate]
            return list(candidate.values())
    return []


def _root_path(value: object) -> str | None:
    """Extract one root path for the compact path list, without resolving it."""

    if isinstance(value, (str, Path)):
        return str(value)
    raw = _root_mapping(value)
    for name in ("root", "path", "root_path"):
        candidate = raw.get(name)
        if isinstance(candidate, (str, Path)):
            return str(candidate)
    return None


def _record_values(raw: Mapping[str, object], roots: Sequence[object]) -> list[object]:
    """Return owner records when available, falling back to root records."""

    records = _sequence(raw.get("records"))
    if records is not None:
        return records
    records = _sequence(raw.get("items"))
    if records is not None:
        return records

    collected: list[object] = []
    for root in roots:
        root_raw = _root_mapping(root)
        root_records = _sequence(root_raw.get("records"))
        if root_records is not None:
            collected.extend(root_records)
    return collected


def _record_mapping(value: object) -> object:
    """Convert one optional detail record without recursively copying peers."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        result: dict[str, object] = {}
        for field in fields(value):
            try:
                result[field.name] = getattr(value, field.name)
            except Exception:
                continue
        if result:
            return result
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        to_dict = getattr(value, "as_dict", None)
    if callable(to_dict):
        try:
            converted = to_dict()
        except Exception:
            converted = None
        if isinstance(converted, Mapping):
            return {str(key): item for key, item in converted.items()}
    return value


def _compact_root(
    value: object,
    index: int,
    *,
    scanner_truncated: bool | None = None,
) -> tuple[dict[str, object], int]:
    """Project one root result while retaining every root-level counter."""

    if isinstance(value, (str, Path)):
        raw: dict[str, object] = {"root": str(value), "path": str(value)}
    else:
        raw = _root_mapping(value)

    summary: dict[str, object] = {}
    for field in _ROOT_OUTPUT_FIELDS:
        # ``summary`` can itself be a full nested projection on older owners;
        # never copy that recursively into the compact root list.
        if field == "summary" or field == "records":
            continue
        if field in raw:
            summary[field] = _enum_value(raw[field])

    path = _root_path(value)
    if path is not None:
        summary.setdefault("root", path)
        summary.setdefault("path", path)
    summary.setdefault("root_index", index)

    records = _sequence(raw.get("records"))
    if records is None:
        records = _sequence(raw.get("items"))
    available = len(records) if records is not None else None
    returned = _non_negative_int(raw.get("records_returned"))
    if returned is None:
        returned = _non_negative_int(raw.get("returned"))
    if returned is None:
        returned = available
    if available is None:
        available = returned or 0
    returned = returned or 0

    records_scanned = _non_negative_int(raw.get("records_scanned"))
    if records_scanned is None:
        records_scanned = _non_negative_int(raw.get("scanned"))
    if records_scanned is None:
        records_scanned = available

    root_scanner_truncated = _bool_value(raw.get("scanner_truncated"))
    if "scanner_truncated" not in raw:
        root_scanner_truncated = _bool_value(raw.get("truncated"))
    if raw.get("coverage") == "partial" and raw.get("status") not in {"absent", "blocked"}:
        root_scanner_truncated = True
    if scanner_truncated is not None:
        root_scanner_truncated = root_scanner_truncated or scanner_truncated
    root_reasons = _sequence(raw.get("scanner_truncation_reasons"))
    if root_reasons is None:
        root_reasons = _sequence(raw.get("truncation_reasons")) or []
    if root_reasons:
        root_scanner_truncated = True

    summary.setdefault("records_scanned", records_scanned)
    summary.setdefault("records_returned", returned)
    if "records_omitted" not in summary:
        summary["records_omitted"] = None if root_scanner_truncated else 0
    summary.setdefault("records_omitted_known", summary["records_omitted"] is not None)
    summary.setdefault("scanner_truncated", root_scanner_truncated)
    summary.setdefault("scanner_truncation_reasons", list(root_reasons))
    summary.setdefault("truncated", root_scanner_truncated)
    summary.setdefault("presentation_truncated", False)
    # Keep the detail count available to the CLI's presentation facet without
    # putting the records themselves under each root a second time.
    summary["presentation_records_available"] = available
    return summary, available


def _scanner_facets(
    raw: Mapping[str, object],
    root_summaries: Sequence[Mapping[str, object]],
    *,
    records_scanned: int,
    records_returned: int,
) -> tuple[bool, list[object], int | None]:
    """Resolve scanner coverage and counters independently from presentation."""

    metadata = raw.get("coverage_metadata")
    scanner_metadata = metadata.get("scanner") if isinstance(metadata, Mapping) else None

    scanner_truncated = _bool_value(raw.get("scanner_truncated"))
    if "scanner_truncated" not in raw:
        scanner_truncated = _bool_value(raw.get("truncated"))
    raw_coverage = _enum_value(raw.get("scan_coverage", raw.get("coverage")))
    if raw_coverage == "partial" and raw.get("status") not in {"absent", "blocked"}:
        scanner_truncated = True
    reasons = _sequence(raw.get("scanner_truncation_reasons"))
    if reasons is None:
        reasons = _sequence(raw.get("truncation_reasons"))
    if isinstance(scanner_metadata, Mapping):
        if "truncated" in scanner_metadata:
            scanner_truncated = scanner_truncated or _bool_value(scanner_metadata.get("truncated"))
        if reasons is None:
            reasons = _sequence(scanner_metadata.get("truncation_reasons"))
            if reasons is None:
                reasons = _sequence(scanner_metadata.get("reasons"))
    reasons = list(dict.fromkeys(reasons or []))
    scanner_truncated = scanner_truncated or any(
        str(reason) in _SCANNER_TRUNCATION_REASONS for reason in reasons
    )
    for root in root_summaries:
        scanner_truncated = scanner_truncated or _bool_value(root.get("scanner_truncated"))
        for reason in _sequence(root.get("scanner_truncation_reasons")) or []:
            if reason not in reasons:
                reasons.append(reason)

    omitted: int | None
    if "records_omitted" in raw:
        candidate = raw.get("records_omitted")
        omitted = _non_negative_int(candidate)
        if candidate is None:
            omitted = None
    elif isinstance(scanner_metadata, Mapping) and "records_omitted" in scanner_metadata:
        candidate = scanner_metadata.get("records_omitted")
        omitted = _non_negative_int(candidate)
        if candidate is None:
            omitted = None
    else:
        omitted = None if scanner_truncated else max(0, records_scanned - records_returned)
    return scanner_truncated, reasons, omitted


def _presentation_metadata(
    *,
    include_records: bool,
    records_available: int,
    records_included: int,
    root_summaries_returned: int,
    root_summaries_omitted: int,
    truncated: bool,
    reasons: Sequence[str],
) -> dict[str, object]:
    """Build explicit renderer metadata; this never changes scanner fields."""

    records_omitted = max(0, records_available - records_included)
    return {
        "status": "partial" if truncated else "complete",
        "mode": "records" if include_records else "compact",
        "truncated": truncated,
        "reasons": list(dict.fromkeys(reasons)),
        "records_available": records_available,
        "records_included": records_included,
        "records_omitted": records_omitted,
        "records_omitted_known": True,
        "root_summaries_returned": root_summaries_returned,
        "root_summaries_omitted": root_summaries_omitted,
    }


def _error_payload(
    roots: Sequence[Path],
    limits: Mapping[str, int],
    *,
    error: BaseException,
) -> dict[str, object]:
    root_summaries = [
        {
            "root": str(root),
            "path": str(root),
            "root_index": index,
            "status": "blocked",
            "coverage": "blocked",
            "reason_code": type(error).__name__,
            "reason": str(error),
            "records_scanned": 0,
            "records_returned": 0,
            "records_omitted": None,
            "records_omitted_known": False,
            "scanner_truncated": False,
            "scanner_truncation_reasons": [],
            "presentation_truncated": False,
            "presentation_records_available": 0,
        }
        for index, root in enumerate(roots)
    ]
    presentation = _presentation_metadata(
        include_records=False,
        records_available=0,
        records_included=0,
        root_summaries_returned=len(root_summaries),
        root_summaries_omitted=0,
        truncated=False,
        reasons=(),
    )
    scanner = {
        "status": "blocked",
        "truncated": False,
        "truncation_reasons": [],
        "records_scanned": 0,
        "records_returned": 0,
        "records_omitted": None,
        "records_omitted_known": False,
        "roots_requested": len(roots),
        "root_summaries_returned": len(root_summaries),
        "root_summaries_omitted": 0,
    }
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
        "root_paths": [str(root) for root in roots],
        "root_summaries": root_summaries,
        "root_results": root_summaries,
        "root_count": len(roots),
        "limits": dict(limits),
        "result": {},
        "scanner_truncated": False,
        "presentation_truncated": False,
        "serialization_truncated": False,
        "records_scanned": 0,
        "records_returned": 0,
        "records_omitted": None,
        "records_omitted_known": False,
        "records_available": 0,
        "records_included": 0,
        "coverage_metadata": {"scanner": scanner, "presentation": presentation},
        "omissions": {"scanner": scanner, "presentation": presentation},
        "serialization": presentation,
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
    *,
    include_records: bool = False,
) -> dict[str, object]:
    """Project one owner result into a closed compact/detail envelope.

    The default path asks the owner for ``to_summary_dict`` (when available)
    and removes record arrays before sanitization.  This is important for the
    real owner: its compatibility ``to_dict`` contains the records both at the
    top level and below each root.  ``--machine-json records`` is the explicit
    compatibility/detail opt-in and remains separately bounded by the CLI.
    """

    raw = _owner_mapping(owner_result, include_records=include_records)
    raw_roots = _root_values(raw)
    owner_published_roots = bool(raw_roots)

    root_summaries: list[dict[str, object]] = []
    root_values_omitted = max(0, len(raw_roots) - _MAX_PRESENTATION_ROOTS)
    for index, root in enumerate(raw_roots[:_MAX_PRESENTATION_ROOTS]):
        summary, _ = _compact_root(root, index)
        root_summaries.append(summary)

    # With no explicit ``--machine-root`` the runtime owner selects its
    # conservative profile roots.  Reflect all owner summaries where present;
    # otherwise retain an explicit path-only fallback for compatibility.
    if not root_summaries and roots:
        for index, root in enumerate(roots[:_MAX_PRESENTATION_ROOTS]):
            summary, _ = _compact_root(root, index)
            root_summaries.append(summary)
        root_values_omitted = max(0, len(roots) - len(root_summaries))

    owner_root_count = _non_negative_int(raw.get("root_count"))
    effective_root_count = max(
        len(root_summaries),
        owner_root_count if owner_root_count is not None else len(roots),
    )
    # A compatible owner may publish only a root count (or may lose its root
    # rows to its own bounded projection).  Preserve that missing-row count
    # instead of claiming that the presentation covered every root.
    root_values_omitted = max(0, root_values_omitted, effective_root_count - len(root_summaries))
    root_paths = [path for root in root_summaries if (path := _root_path(root)) is not None]
    if not root_paths and roots:
        root_paths = [str(root) for root in roots]

    raw_records = _record_values(raw, raw_roots)
    records_scanned = _non_negative_int(raw.get("records_scanned"))
    if records_scanned is None:
        records_scanned = _non_negative_int(raw.get("scanned"))
    if records_scanned is None:
        records_scanned = sum(
            _non_negative_int(root.get("records_scanned"), 0) or 0
            for root in root_summaries
        )
    records_returned = _non_negative_int(raw.get("records_returned"))
    if records_returned is None:
        records_returned = _non_negative_int(raw.get("returned"))
    if records_returned is None:
        records_returned = len(raw_records)
    records_available = max(len(raw_records), records_returned)

    scanner_truncated, scanner_reasons, scanner_omitted = _scanner_facets(
        raw,
        root_summaries,
        records_scanned=records_scanned,
        records_returned=records_returned,
    )

    presentation_reasons: list[str] = []
    records: list[object] = []
    if include_records:
        detail_records = [_record_mapping(record) for record in raw_records]
        records, records_clipped = _bounded_list(
            detail_records, limit=_MAX_PRESENTATION_RECORDS
        )
        if records_clipped:
            presentation_reasons.append("record_limit")
        bounded_records, records_nested_clipped = _presentation_bound(records)
        if isinstance(bounded_records, list):
            records = bounded_records
        if records_nested_clipped:
            presentation_reasons.append("record_serialization_limit")
    records_included = len(records)
    if root_values_omitted:
        presentation_reasons.append("root_summary_limit")
    bounded_roots, roots_nested_clipped = _presentation_bound(root_summaries)
    if isinstance(bounded_roots, list):
        root_summaries = [
            item if isinstance(item, dict) else {"value": item}
            for item in bounded_roots
        ]
    if roots_nested_clipped:
        presentation_reasons.append("root_summary_serialization_limit")
        root_values_omitted = max(
            root_values_omitted,
            effective_root_count - len(root_summaries),
        )
    presentation_truncated = bool(presentation_reasons)
    presentation = _presentation_metadata(
        include_records=include_records,
        records_available=records_available,
        records_included=records_included,
        root_summaries_returned=len(root_summaries),
        root_summaries_omitted=root_values_omitted,
        truncated=presentation_truncated,
        reasons=presentation_reasons,
    )
    scanner = {
        "status": _enum_value(raw.get("scan_coverage", raw.get("coverage", "complete"))),
        "truncated": scanner_truncated,
        "truncation_reasons": scanner_reasons,
        "records_scanned": records_scanned,
        "records_returned": records_returned,
        "records_omitted": scanner_omitted,
        "records_omitted_known": scanner_omitted is not None,
        "roots_requested": effective_root_count,
        "root_summaries_returned": len(root_summaries),
        "root_summaries_omitted": root_values_omitted,
    }
    coverage_metadata = {"scanner": scanner, "presentation": presentation}
    omissions = {
        "scanner": scanner,
        "presentation": presentation,
    }

    # Keep only the owner summary projection in ``result``.  Unknown producer
    # keys and all record arrays are excluded in compact mode, rather than
    # being sanitized after the node budget was already consumed.
    result: dict[str, object] = {}
    for field in _SUMMARY_FIELDS:
        if field not in raw or field in {
            "records",
            "roots",
            "root_results",
            "root_summaries",
            "summaries",
            "root_paths",
            "coverage_metadata",
            "omissions",
        }:
            continue
        result[field] = _enum_value(raw[field])
    result.update(
        {
            "roots": root_summaries,
            "root_summaries": root_summaries,
            "root_results": root_summaries,
            "root_paths": root_paths,
            "root_count": effective_root_count,
            "scanner_truncated": scanner_truncated,
            "scanner_truncation_reasons": scanner_reasons,
            "truncated": scanner_truncated,
            "records_scanned": records_scanned,
            "records_returned": records_returned,
            "records_omitted": scanner_omitted,
            "records_omitted_known": scanner_omitted is not None,
            "records_available": records_available,
            "records_included": records_included,
            "presentation_records_omitted": presentation["records_omitted"],
            "presentation_truncated": presentation_truncated,
            "coverage_metadata": coverage_metadata,
            "omissions": omissions,
            "serialization": presentation,
        }
    )
    bounded_result, result_nested_clipped = _presentation_bound(result)
    if isinstance(bounded_result, Mapping):
        result = {str(key): value for key, value in bounded_result.items()}
    if result_nested_clipped:
        presentation_reasons.append("summary_serialization_limit")
        presentation_truncated = True
        presentation = _presentation_metadata(
            include_records=include_records,
            records_available=records_available,
            records_included=records_included,
            root_summaries_returned=len(root_summaries),
            root_summaries_omitted=root_values_omitted,
            truncated=presentation_truncated,
            reasons=presentation_reasons,
        )
        # Rebind the presentation facet in the already bounded result.  The
        # scanner facet remains untouched, so renderer clipping cannot become
        # a false scanner truncation.
        result["presentation_truncated"] = True
        result["coverage_metadata"] = {"scanner": scanner, "presentation": presentation}
        result["omissions"] = {"scanner": scanner, "presentation": presentation}
        result["serialization"] = presentation
        coverage_metadata = {"scanner": scanner, "presentation": presentation}
        omissions = {"scanner": scanner, "presentation": presentation}

    # Preserve the historical ``result.records`` location for the explicit
    # detail mode.  The default compact mode never adds this array.  The same
    # bounded list is also exposed at the envelope boundary below for
    # compatibility with callers that consumed the old top-level alias.
    if include_records:
        result["records"] = records

    raw_status = _enum_value(raw.get("status", "complete"))
    status = _safe_text(raw_status, limit=128) or "complete"
    raw_coverage = _enum_value(raw.get("coverage", status))
    coverage = _safe_text(raw_coverage, limit=128) or status
    # Preserve the original path-only fallback when a compatibility owner
    # does not publish root result objects.  The additive ``root_summaries``
    # field above still gives callers one summary slot per explicit root.
    effective_roots: object = (
        root_summaries if owner_published_roots else root_paths
    )

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
        "root_paths": root_paths,
        "root_summaries": root_summaries,
        "root_results": root_summaries,
        "root_count": effective_root_count,
        "limits": dict(limits),
        "result": result,
        "error": None,
        "applied": 0,
        "scanner_truncated": scanner_truncated,
        "presentation_truncated": presentation_truncated,
        "serialization_truncated": presentation_truncated,
        "truncated": scanner_truncated,
        "records_scanned": records_scanned,
        "records_returned": records_returned,
        "records_omitted": scanner_omitted,
        "records_omitted_known": scanner_omitted is not None,
        "records_available": records_available,
        "records_included": records_included,
        "coverage_metadata": coverage_metadata,
        "omissions": omissions,
        "serialization": presentation,
    }
    if include_records:
        payload["records"] = records

    # Stable summary aliases remain available at the envelope boundary.  They
    # are copied from the already compact result and can never replace fixed
    # envelope metadata or reintroduce the omitted record array.
    for field, value in result.items():
        if field not in payload:
            payload[field] = value

    bytes_payload = result.get("bytes")
    if isinstance(bytes_payload, Mapping):
        for output_name, source_name in (
            ("observed_bytes", "observed"),
            ("observed_apparent_bytes", "apparent"),
            ("observed_allocated_bytes", "allocated"),
        ):
            if output_name not in payload and source_name in bytes_payload:
                payload[output_name] = bytes_payload[source_name]

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
    scanned = result.get(
        "records_scanned",
        result.get("scanned", result.get("entries", 0)),
    )
    returned = result.get(
        "records_returned",
        result.get("returned", result.get("entries_returned", scanned)),
    )
    truncated = result.get(
        "scanner_truncated",
        result.get("truncated", payload.get("scanner_truncated", False)),
    )
    return scanned, returned, truncated


def _human_scalar(value: object, *, limit: int = 64) -> str:
    """Render bounded scalar values without hiding ``False`` or zero."""

    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _safe_text(value, limit=limit)


def _emit(payload: Mapping[str, object], *, json_output: bool) -> None:
    safe_payload, renderer_truncated = _safe_payload_with_status(payload)
    if renderer_truncated:
        safe_payload = _mark_presentation_truncated(safe_payload)
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
        f"status={_human_scalar(payload.get('status', 'complete'), limit=128)} "
        f"roots={_human_scalar(payload.get('root_count', 0))} "
        f"scanned={_human_scalar(scanned)} "
        f"returned={_human_scalar(returned)} "
        f"truncated={_human_scalar(truncated, limit=16)} "
        "read_only=true"
    )

    # Human mode is a compact diagnostic too: show bounded root rows rather
    # than forcing the caller to request a giant JSON record stream just to
    # learn which roots were incomplete.  The JSON envelope remains the
    # canonical machine-readable form.
    root_values = payload.get("root_summaries")
    if isinstance(root_values, (list, tuple)):
        for index, root in enumerate(root_values[:_MAX_HUMAN_ROOTS]):
            if not isinstance(root, Mapping):
                print(f"  ROOT index={index} value={_human_scalar(root, limit=160)}")
                continue
            path = root.get("path", root.get("root", "unknown"))
            bytes_value = root.get("bytes")
            bytes_map = bytes_value if isinstance(bytes_value, Mapping) else {}
            print(
                "  ROOT "
                f"index={_human_scalar(root.get('root_index', index))} "
                f"path={_human_scalar(path, limit=240)} "
                f"status={_human_scalar(root.get('status', 'unknown'), limit=64)} "
                f"coverage={_human_scalar(root.get('coverage', 'unknown'), limit=64)} "
                f"scanned={_human_scalar(root.get('records_scanned', root.get('scanned', 0)))} "
                f"reason={_human_scalar(root.get('reason_code', 'none'), limit=96)} "
                f"apparent={_human_scalar(bytes_map.get('apparent', 0))} "
                f"allocated={_human_scalar(bytes_map.get('allocated', 0))} "
                f"observed={_human_scalar(bytes_map.get('observed', 0))}"
            )
        omitted = len(root_values) - min(len(root_values), _MAX_HUMAN_ROOTS)
        if omitted:
            print(f"  ROOT_SUMMARIES_OMITTED count={omitted} reason=human_output_limit")
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
    include_records = getattr(args, "machine_json_mode", "compact") == "records"
    # Keep this additive hook for callers that construct a Namespace directly
    # or for a future parser alias.  Ordinary ``--machine-json`` remains
    # compact and therefore does not expose record arrays.
    include_records = include_records or bool(
        getattr(args, "machine_include_records", False)
    )
    try:
        owner_result = _call_owner(
            roots,
            max_entries=limits["max_entries"],
            max_depth=limits["max_depth"],
            max_bytes=limits["max_bytes"],
        )
        payload = _result_payload(
            owner_result,
            roots,
            limits,
            include_records=include_records,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        payload = _error_payload(roots, limits, error=exc)

    _emit(payload, json_output=_json_requested(args))
    return 0
