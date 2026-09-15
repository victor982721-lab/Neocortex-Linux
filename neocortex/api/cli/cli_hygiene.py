"""Read-only CLI adapter for the bounded hygiene planner.

The runtime hygiene owner is the only component that observes the selected
roots.  This module owns only argument-to-owner mapping and the stable CLI
projection; it deliberately has no cleaner, unlink, move, trash or other
physical-effect implementation.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text

__all__ = ["HYGIENE_SCHEMA", "run_hygiene"]


HYGIENE_SCHEMA = "neocortex.hygiene/v1"
_MAX_HUMAN_TEXT = 800
_DEFAULT_MAX_ENTRIES = 10_000
_DEFAULT_MAX_DEPTH = 2
_DEFAULT_MAX_BYTES = 1 << 40


def _roots(args: argparse.Namespace) -> list[Path]:
    values = getattr(args, "hygiene_root", None)
    if values is None:
        # These are the only implicit roots owned by NeoCortex.  Path
        # composition is intentionally side-effect free: the runtime owners
        # decide whether an absent directory is deferred and never create it
        # merely because this CLI selected the safe profile.
        state = getattr(args, "state_directory", None)
        if isinstance(state, (Path, str)):
            state_root = Path(state)
            return [
                state_root / "artifacts",
                state_root / "scratch" / "owned-temp",
                state_root / "scratch" / "audit-work",
                state_root,
            ]
        return []
    if isinstance(values, (list, tuple)):
        return [value if isinstance(value, Path) else Path(value) for value in values]
    return [values if isinstance(values, Path) else Path(values)]


def _limits(args: argparse.Namespace) -> dict[str, int]:
    return {
        "max_entries": int(getattr(args, "hygiene_max_entries", _DEFAULT_MAX_ENTRIES)),
        "max_depth": int(getattr(args, "hygiene_max_depth", _DEFAULT_MAX_DEPTH)),
        "max_bytes": int(getattr(args, "hygiene_max_bytes", _DEFAULT_MAX_BYTES)),
    }


def _owner_mapping(value: object) -> dict[str, object]:
    """Convert one bounded owner result to a shallow mapping."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}

    # Prefer the owner serializer before inspecting dataclass fields.  A plan
    # dataclass may retain private owner objects for later verification, while
    # its serializer is the bounded public projection.
    for name in ("to_dict", "as_dict", "to_payload"):
        converter = getattr(value, name, None)
        if not callable(converter):
            continue
        try:
            converted = converter()
        except Exception:
            continue
        if isinstance(converted, Mapping):
            return {str(key): item for key, item in converted.items()}

    if is_dataclass(value) and not isinstance(value, type):
        result: dict[str, object] = {}
        for field in fields(value):
            try:
                result[field.name] = getattr(value, field.name)
            except Exception:
                continue
        if result:
            return result

    return {"result": value}


def _safe_mapping(value: object) -> dict[str, object]:
    """Sanitize owner data without allowing it to replace envelope fields."""

    try:
        safe = sanitize_untrusted_payload(_owner_mapping(value))
    except Exception:
        return {}
    return dict(safe) if isinstance(safe, Mapping) else {"result": safe}


def _parameter_value(
    parameter: inspect.Parameter,
    roots: list[Path] | None,
    limits: Mapping[str, int],
    preview: bool,
) -> tuple[bool, object]:
    """Map compatible owner parameter names without retrying owner execution."""

    aliases: dict[str, object] = {
        "roots": roots,
        "hygiene_roots": roots,
        "root_paths": roots,
        "paths": roots,
        "root": roots[0] if roots else None,
        "artifact_root": roots[0] if roots else None,
        "owned_temp_root": roots[1] if roots and len(roots) > 1 else None,
        "audit_work_root": roots[2] if roots and len(roots) > 2 else None,
        "state_directory": roots[3] if roots and len(roots) > 3 else None,
        "max_entries": limits["max_entries"],
        "scan_max_entries": limits["max_entries"],
        "hygiene_max_entries": limits["max_entries"],
        "max_records": limits["max_entries"],
        "entry_limit": limits["max_entries"],
        "max_depth": limits["max_depth"],
        "scan_max_depth": limits["max_depth"],
        "hygiene_max_depth": limits["max_depth"],
        "depth_limit": limits["max_depth"],
        "max_bytes": limits["max_bytes"],
        "scan_max_bytes": limits["max_bytes"],
        "hygiene_max_bytes": limits["max_bytes"],
        "byte_limit": limits["max_bytes"],
        "bytes_limit": limits["max_bytes"],
        "max_items": min(limits["max_entries"], 10_000),
        "limits": dict(limits),
        "budget": dict(limits),
        "preview": preview,
        "include_preview": preview,
        "verify": preview,
    }
    if parameter.name in aliases:
        return True, aliases[parameter.name]
    return False, None


def _invoke_owner(
    target: Callable[..., object],
    roots: list[Path],
    limits: Mapping[str, int],
    *,
    preview: bool,
) -> object:
    """Invoke the runtime seam once, supporting additive compatible spellings."""

    owner_roots: list[Path] | None = roots if roots else None
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(
            roots=owner_roots,
            max_entries=limits["max_entries"],
            max_depth=limits["max_depth"],
            max_bytes=limits["max_bytes"],
            preview=preview,
        )

    parameters = tuple(signature.parameters.values())
    generic_owner = any(
        parameter.name in {"roots", "hygiene_roots", "root_paths", "paths"}
        for parameter in parameters
    )
    component_owner = not generic_owner and any(
        parameter.name
        in {"artifact_root", "owned_temp_root", "audit_work_root", "state_directory"}
        for parameter in parameters
    )
    has_var_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )
    if has_var_keyword and not component_owner:
        return target(
            roots=owner_roots,
            max_entries=limits["max_entries"],
            max_depth=limits["max_depth"],
            max_bytes=limits["max_bytes"],
            preview=preview,
        )

    positional: list[object] = []
    keyword: dict[str, object] = {}
    for parameter in parameters:
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        found, value = _parameter_value(parameter, owner_roots, limits, preview)
        if generic_owner and parameter.name in {
            "artifact_root",
            "owned_temp_root",
            "audit_work_root",
            "state_directory",
        }:
            found = False
        if component_owner and parameter.name == "max_bytes":
            # The canonical owner currently uses max_bytes as its JSON
            # serialization ceiling, not as the CLI traversal budget.  Keep
            # the two limits independent and let the owner default stand.
            found = False
        if not found:
            if parameter.default is not inspect.Parameter.empty:
                continue
            raise TypeError(f"unsupported hygiene owner parameter {parameter.name!r}")
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            keyword[parameter.name] = value
    return target(*positional, **keyword)


def _call_owner(
    roots: list[Path],
    limits: Mapping[str, int],
    *,
    preview: bool,
) -> object:
    """Import and invoke the hygiene owner only after CLI validation."""

    from neocortex.runtime.hygiene import plan_hygiene

    return _invoke_owner(plan_hygiene, roots, limits, preview=preview)


def _error_payload(
    args: argparse.Namespace,
    roots: Sequence[Path],
    limits: Mapping[str, int],
    error: BaseException,
) -> dict[str, object]:
    return _payload(
        args,
        roots,
        limits,
        {
            "status": "error",
            "exit_code": 2,
            "error": {
                "code": type(error).__name__,
                "message": sanitize_untrusted_text(
                    error, limit=_MAX_HUMAN_TEXT, single_line=False
                ),
            },
        },
    )


def _payload(
    args: argparse.Namespace,
    roots: Sequence[Path],
    limits: Mapping[str, int],
    owner_result: object,
) -> dict[str, object]:
    """Project one owner result into the closed hygiene safety envelope."""

    payload = _safe_mapping(owner_result)

    requested_roots = [str(root) for root in roots]
    payload["roots"] = requested_roots
    if len(requested_roots) == 1:
        payload["root"] = requested_roots[0]
    payload["schema"] = HYGIENE_SCHEMA
    payload["kind"] = "hygiene"
    payload["operation"] = "hygiene"
    payload["read_only"] = True
    payload["effects_enabled"] = False
    payload["preview_only"] = True
    payload["deletion_performed"] = 0
    payload["actions_ready"] = False
    payload["next_gate"] = "human_review"
    payload["physical_effect_applied"] = False
    payload["mutation_authorized"] = False
    payload["applied"] = 0
    payload["limits"] = dict(limits)
    payload["preview"] = bool(getattr(args, "hygiene_preview", False))
    requested_mode = "preview" if payload["preview"] else "plan"
    if payload.get("mode") not in {"plan", "preview", "verify"}:
        payload["mode"] = requested_mode
    payload.setdefault("status", "planned")
    raw_exit_code = payload.get("exit_code", 0)
    payload["exit_code"] = (
        raw_exit_code
        if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool)
        and 0 <= raw_exit_code <= 255
        else 0
    )
    return payload


def _json_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "hygiene_json", False) or getattr(args, "json_output", False))


def _emit_human(payload: Mapping[str, object]) -> None:
    def safe(value: object, *, limit: int = _MAX_HUMAN_TEXT) -> str:
        return sanitize_untrusted_text(value, limit=limit)

    roots = payload.get("roots")
    root_count = len(roots) if isinstance(roots, (list, tuple)) else 0
    candidates = payload.get(
        "candidate_count", payload.get("candidates", payload.get("eligible", 0))
    )
    if isinstance(candidates, (list, tuple, Mapping)):
        candidates = len(candidates)
    scanned = payload.get(
        "entries_scanned",
        payload.get("records_scanned", payload.get("observed", payload.get("scanned", 0))),
    )
    print(
        "HYGIENE "
        f"mode={safe(payload.get('mode', 'plan'), limit=64)} "
        f"status={safe(payload.get('status', 'planned'), limit=64)} "
        f"roots={root_count} scanned={safe(scanned, limit=64)} "
        f"candidates={safe(candidates, limit=64)} "
        "deletion_performed=0 effects_enabled=false actions_ready=false "
        f"next_gate={safe(payload.get('next_gate', 'human_review'), limit=128)}"
    )
    error = payload.get("error")
    if isinstance(error, Mapping):
        print(
            "ERROR hygiene "
            f"code={safe(error.get('code', 'error'), limit=128)}: "
            f"{safe(error.get('message', '-'))}",
            file=sys.stderr,
        )


def run_hygiene(args: argparse.Namespace) -> int:
    """Run the read-only hygiene plan and render one bounded result."""

    roots = _roots(args)
    limits = _limits(args)
    preview = bool(getattr(args, "hygiene_preview", False))
    try:
        for name in ("apply", "all", "dedupe", "organization_apply", "catalog_documents"):
            if bool(getattr(args, name, False)):
                raise ValueError(f"hygiene is read-only and cannot use {name}")
        if "root" in getattr(args, "_explicit_options", ()):
            raise ValueError("hygiene cannot be combined with --root")
        owner_result = _call_owner(roots, limits, preview=preview)
        payload = _payload(args, roots, limits, owner_result)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        payload = _error_payload(args, roots, limits, exc)

    safe_payload = _safe_mapping(payload)
    if _json_requested(args):
        print(
            json.dumps(
                safe_payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        _emit_human(safe_payload)

    exit_code = safe_payload.get("exit_code")
    return (
        exit_code
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        and 0 <= exit_code <= 255
        else 2
    )
