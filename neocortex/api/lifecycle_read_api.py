"""Bounded read-only lifecycle status for API/MCP consumers."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, overload
from uuid import uuid4

from neocortex.api.read_contract import sanitize_untrusted_text
from neocortex.api.run_lifecycle import read_run_status
from neocortex.runtime.config.app_paths import default_state_directory
from neocortex.runtime.orchestration.run_manifest import (
    RUN_BUDGET_SCHEMA,
    RUN_MANIFEST_SCHEMA,
    RUN_STAGE_SCHEMA,
    verify_event_payload,
)
from neocortex.runtime.orchestration.run_status import serialized_run_status


LIFECYCLE_ENVELOPE_SCHEMA = "neocortex.lifecycle-envelope/v1"
LIFECYCLE_STATUS_KIND = "neocortex_lifecycle_status"
LIFECYCLE_STATUS_OPERATION = "lifecycle_status"
# Keep the reader import-light while allowing it to validate checkpoints from
# runtimes that already publish the 0.13 checkpoint contract.
RUN_CHECKPOINT_SCHEMA = "neocortex.lifecycle-checkpoint/v1"

MAX_RUNS = 20
MAX_ROUTES = 64
MAX_PHASES = 64
MAX_STAGES = 64
MAX_METADATA_KEYS = 64
MAX_METADATA_NODES = 512
MAX_METADATA_DEPTH = 5
MAX_PATH_CHARS = 4_096
MAX_TEXT_CHARS = 2_048
MAX_ERROR_CHARS = 1_000
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAPABILITIES = frozenset({"phase_resume", "safe_replay", "not_resumable"})
_RUN_KINDS = frozenset({"initial", "route_only", "resume"})

_RUN_FIELDS = frozenset(
    {
        "run_id",
        "run_kind",
        "status",
        "root",
        "source_run_id",
        "current_phase",
        "owner_pid",
        "owner_alive",
        "heartbeat_ns",
        "heartbeat_stale",
        "started_ns",
        "completed_ns",
        "elapsed_ns",
        "recovery_required_actions",
        "manifest",
        "budget",
        "recovery",
        "resumed",
        "replayed",
        "skipped_routes",
        "non_replayable_routes",
        "stages",
        "checkpoints",
        "route_capabilities",
        "lifecycle",
        "routes",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "run_kind",
        "source_run_id",
        "root",
        "root_identity",
        "selected_routes",
        "route_capabilities",
        "configuration",
        "budget",
        "input_snapshot",
        "digest",
    }
)
_BUDGET_FIELDS = frozenset(
    {
        "schema",
        "manifest_digest",
        "max_items",
        "max_bytes",
        "max_duration_seconds",
        "started_ns",
        "deadline_ns",
        "consumed_items",
        "consumed_bytes",
        "consumed_bytes_kind",
        "remaining_items",
        "remaining_bytes",
        "elapsed_ns",
        "elapsed_seconds",
        "elapsed_until_ns",
        "elapsed_scope",
        "expired",
        "cancel_requested",
        "cancel_reason",
        "reservation_count",
        "last_event_id",
    }
)
_PHASE_FIELDS = frozenset(
    {"phase_name", "status", "started_ns", "completed_ns", "elapsed_ns", "error_type"}
)
_ROUTE_FIELDS = frozenset(
    {
        "route_name",
        "status",
        "current_phase",
        "started_ns",
        "completed_ns",
        "elapsed_ns",
        "heartbeat_ns",
        "error_type",
        "resume_capability",
        "replayability",
        "candidates",
        "processed",
        "cache_hits",
        "new_work",
        "cached_errors",
        "replay_status",
        "phases",
    }
)
_STAGE_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "manifest_digest",
        "stage",
        "status",
        "details",
        "idempotency_key",
        "event_id",
    }
)
_CHECKPOINT_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "manifest_digest",
        "stage",
        "checkpoint",
        "idempotency_key",
        "event_id",
    }
)
_LIFECYCLE_FIELDS = frozenset(
    {
        "schema",
        "status",
        "run_id",
        "source_run_id",
        "manifest_digest",
        "resumed_from",
        "resumed",
        "replayed",
        "skipped",
        "non_replayable",
        "budget",
        "recovery",
        "stages",
        "checkpoints",
        "route_capabilities",
        "routes",
        "errors",
    }
)


class LifecycleStatusContractError(ValueError):
    """Persisted lifecycle data is not safe for the v1 public contract."""

    def __init__(self, message: str, *, kind: Literal["schema", "corrupt"] = "corrupt") -> None:
        self.kind = kind
        super().__init__(message)


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise LifecycleStatusContractError(f"{label} must be an object")
    return value


def _keys(value: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    if set(value) - expected:
        raise LifecycleStatusContractError(f"{label} contains unsupported fields", kind="schema")


@overload
def _text(
    value: object,
    *,
    label: str,
    limit: int = MAX_TEXT_CHARS,
    optional: Literal[False] = False,
) -> str: ...


@overload
def _text(
    value: object,
    *,
    label: str,
    limit: int = MAX_TEXT_CHARS,
    optional: Literal[True],
) -> str | None: ...


@overload
def _text(
    value: object,
    *,
    label: str,
    limit: int = MAX_TEXT_CHARS,
    optional: bool,
) -> str | None: ...


def _text(
    value: object,
    *,
    label: str,
    limit: int = MAX_TEXT_CHARS,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise LifecycleStatusContractError(f"{label} must be text")
    cleaned = sanitize_untrusted_text(value, limit=limit, single_line=True)
    if not cleaned.strip():
        raise LifecycleStatusContractError(f"{label} must not be empty")
    return cleaned


@overload
def _integer(
    value: object,
    *,
    label: str,
    optional: Literal[False] = False,
    minimum: int = 0,
) -> int: ...


@overload
def _integer(
    value: object,
    *,
    label: str,
    optional: Literal[True],
    minimum: int = 0,
) -> int | None: ...


@overload
def _integer(
    value: object,
    *,
    label: str,
    optional: bool,
    minimum: int = 0,
) -> int | None: ...


def _integer(
    value: object,
    *,
    label: str,
    optional: bool = False,
    minimum: int = 0,
) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < minimum or value > (1 << 63) - 1:
        raise LifecycleStatusContractError(f"{label} must be a bounded integer")
    return value


@overload
def _boolean(
    value: object,
    *,
    label: str,
    optional: Literal[False] = False,
) -> bool: ...


@overload
def _boolean(
    value: object,
    *,
    label: str,
    optional: Literal[True],
) -> bool | None: ...


@overload
def _boolean(
    value: object,
    *,
    label: str,
    optional: bool,
) -> bool | None: ...


def _boolean(value: object, *, label: str, optional: bool = False) -> bool | None:
    if value is None and optional:
        return None
    if type(value) is not bool:
        raise LifecycleStatusContractError(f"{label} must be boolean")
    return value


def _metadata(
    value: object, *, label: str, depth: int = 0, budget: list[int] | None = None
) -> object:
    """Return bounded JSON metadata with ANSI/C0 text removed."""

    if budget is None:
        budget = [MAX_METADATA_NODES]
    if budget[0] <= 0 or depth > MAX_METADATA_DEPTH:
        return "[metadata omitted by lifecycle bound]"
    budget[0] -= 1
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        # ``-1`` is the platform contract's unavailable birthtime sentinel,
        # used in manifest/stage root identities.  Other lifecycle metadata
        # remains non-negative and bounded.
        minimum = -1 if ".root_identity[" in label else 0
        return _integer(value, label=label, minimum=minimum)
    if type(value) is float:
        if not math.isfinite(value):
            raise LifecycleStatusContractError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, str):
        return sanitize_untrusted_text(value, limit=MAX_TEXT_CHARS, single_line=True)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, raw_item in list(value.items())[:MAX_METADATA_KEYS]:
            if not isinstance(raw_key, str):
                raise LifecycleStatusContractError(f"{label} has a non-text key")
            key = sanitize_untrusted_text(raw_key, limit=256, single_line=True)
            if not key or key in result:
                raise LifecycleStatusContractError(f"{label} has an invalid key")
            result[key] = _metadata(
                raw_item, label=f"{label}.{key}", depth=depth + 1, budget=budget
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _metadata(item, label=f"{label}[{index}]", depth=depth + 1, budget=budget)
            for index, item in enumerate(value[:MAX_METADATA_KEYS])
        ]
    raise LifecycleStatusContractError(f"{label} contains an unsupported value")


def _object_metadata(value: object, *, label: str) -> dict[str, object]:
    result = _metadata(value, label=label)
    if not isinstance(result, dict):
        raise LifecycleStatusContractError(f"{label} must be an object")
    return result


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    cleaned = _text(value, label=label, limit=71, optional=optional)
    if cleaned is None:
        return None
    if _SHA256.fullmatch(cleaned) is None:
        raise LifecycleStatusContractError(f"{label} is not a sha256 digest")
    return cleaned


def _items(value: object, *, label: str, limit: int) -> list[object]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise LifecycleStatusContractError(f"{label} exceeds its item bound")
    return list(value)


def _route_names(value: object, *, label: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(_items(value, label=label, limit=MAX_ROUTES)):
        name = _text(item, label=f"{label}[{index}]", limit=256)
        assert name is not None
        if name in seen:
            raise LifecycleStatusContractError(f"{label} contains duplicate routes")
        seen.add(name)
        result.append(name)
    return result


def _capabilities(value: object, *, label: str) -> dict[str, str]:
    raw = _mapping(value, label=label)
    if len(raw) > MAX_ROUTES:
        raise LifecycleStatusContractError(f"{label} exceeds its route bound")
    result: dict[str, str] = {}
    for raw_name, raw_capability in raw.items():
        name = _text(raw_name, label=f"{label}.route", limit=256)
        capability = _text(raw_capability, label=f"{label}.{raw_name}", limit=32)
        assert name is not None and capability is not None
        if capability not in _CAPABILITIES or name in result:
            raise LifecycleStatusContractError(f"{label} contains an unsupported route capability")
        result[name] = capability
    return result


def _manifest(
    value: object,
    *,
    run_id: int,
    label: str,
    verify_digest: bool = True,
) -> dict[str, object] | None:
    if value is None:
        return None
    raw = _mapping(value, label=label)
    _keys(raw, _MANIFEST_FIELDS, label=label)
    if verify_digest:
        try:
            verify_event_payload(raw)
        except ValueError as exc:
            raise LifecycleStatusContractError(f"{label} digest is invalid") from exc
    if raw.get("schema") != RUN_MANIFEST_SCHEMA:
        raise LifecycleStatusContractError(f"{label} schema is unsupported", kind="schema")
    if raw.get("run_id") != run_id:
        raise LifecycleStatusContractError(f"{label} owner does not match run")
    run_kind = _text(raw.get("run_kind"), label=f"{label}.run_kind", limit=32)
    root = _text(raw.get("root"), label=f"{label}.root", limit=MAX_PATH_CHARS)
    assert run_kind is not None and root is not None
    if run_kind not in _RUN_KINDS:
        raise LifecycleStatusContractError(f"{label}.run_kind is unsupported")
    identity = _items(raw.get("root_identity"), label=f"{label}.root_identity", limit=3)
    if len(identity) != 3 or any(type(item) is not int or item < -1 for item in identity):
        raise LifecycleStatusContractError(f"{label}.root_identity is invalid")
    selected = _route_names(raw.get("selected_routes", []), label=f"{label}.selected_routes")
    capabilities = _capabilities(
        raw.get("route_capabilities", {}), label=f"{label}.route_capabilities"
    )
    if set(selected) != set(capabilities):
        raise LifecycleStatusContractError(
            f"{label} route capabilities do not match selected routes"
        )
    return {
        "schema": RUN_MANIFEST_SCHEMA,
        "run_id": run_id,
        "run_kind": run_kind,
        "source_run_id": _integer(
            raw.get("source_run_id"), label=f"{label}.source_run_id", optional=True, minimum=1
        ),
        "root": root,
        "root_identity": list(identity),
        "selected_routes": selected,
        "route_capabilities": capabilities,
        "configuration": _object_metadata(
            raw.get("configuration", {}), label=f"{label}.configuration"
        ),
        "budget": _object_metadata(raw.get("budget", {}), label=f"{label}.budget"),
        "input_snapshot": _object_metadata(
            raw.get("input_snapshot", {}), label=f"{label}.input_snapshot"
        ),
        "digest": _digest(raw.get("digest"), label=f"{label}.digest"),
    }


def _budget(value: object, *, label: str) -> dict[str, object] | None:
    if value is None:
        return None
    raw = _mapping(value, label=label)
    _keys(raw, _BUDGET_FIELDS, label=label)
    if raw.get("schema") != RUN_BUDGET_SCHEMA:
        raise LifecycleStatusContractError(f"{label} schema is unsupported", kind="schema")
    result: dict[str, object] = {"schema": RUN_BUDGET_SCHEMA}
    integer_fields = {
        "max_items",
        "max_bytes",
        "started_ns",
        "deadline_ns",
        "consumed_items",
        "consumed_bytes",
        "remaining_items",
        "remaining_bytes",
        "elapsed_ns",
        "elapsed_until_ns",
        "reservation_count",
        "last_event_id",
    }
    text_fields = {"consumed_bytes_kind", "elapsed_scope"}
    boolean_fields = {"expired", "cancel_requested"}
    number_fields = {"max_duration_seconds", "elapsed_seconds"}
    for name, item in raw.items():
        if name == "schema":
            continue
        if name == "manifest_digest":
            result[name] = _digest(item, label=f"{label}.{name}", optional=True)
        elif name in integer_fields:
            result[name] = _integer(item, label=f"{label}.{name}", optional=True)
        elif name in text_fields:
            result[name] = _text(item, label=f"{label}.{name}", limit=256, optional=True)
        elif name in boolean_fields:
            result[name] = _boolean(item, label=f"{label}.{name}", optional=True)
        elif name in number_fields:
            if item is None:
                result[name] = None
            elif type(item) is int or (type(item) is float and math.isfinite(item) and item >= 0):
                result[name] = item
            else:
                raise LifecycleStatusContractError(f"{label}.{name} is invalid")
        elif name == "cancel_reason":
            result[name] = _text(
                item, label=f"{label}.{name}", limit=MAX_ERROR_CHARS, optional=True
            )
    return result


def _phase(value: object, *, label: str) -> dict[str, object]:
    raw = _mapping(value, label=label)
    _keys(raw, _PHASE_FIELDS, label=label)
    return {
        "phase_name": _text(raw.get("phase_name"), label=f"{label}.phase_name"),
        "status": _text(raw.get("status"), label=f"{label}.status", limit=64),
        "started_ns": _integer(raw.get("started_ns", 0), label=f"{label}.started_ns"),
        "completed_ns": _integer(
            raw.get("completed_ns"), label=f"{label}.completed_ns", optional=True
        ),
        "elapsed_ns": _integer(raw.get("elapsed_ns", 0), label=f"{label}.elapsed_ns"),
        "error_type": _text(
            raw.get("error_type"), label=f"{label}.error_type", limit=MAX_ERROR_CHARS, optional=True
        ),
    }


def _route(value: object, *, label: str) -> dict[str, object]:
    raw = _mapping(value, label=label)
    _keys(raw, _ROUTE_FIELDS, label=label)
    capability = _text(raw.get("resume_capability"), label=f"{label}.resume_capability", limit=32)
    replayability = _text(
        raw.get("replayability", capability), label=f"{label}.replayability", limit=32
    )
    assert capability is not None and replayability is not None
    if capability not in _CAPABILITIES or replayability != capability:
        raise LifecycleStatusContractError(f"{label} replay capability is invalid")
    phases = [
        _phase(item, label=f"{label}.phases[{index}]")
        for index, item in enumerate(
            _items(raw.get("phases", []), label=f"{label}.phases", limit=MAX_PHASES)
        )
    ]
    return {
        "route_name": _text(raw.get("route_name"), label=f"{label}.route_name"),
        "status": _text(raw.get("status"), label=f"{label}.status", limit=64),
        "current_phase": _text(
            raw.get("current_phase"), label=f"{label}.current_phase", optional=True
        ),
        "started_ns": _integer(raw.get("started_ns", 0), label=f"{label}.started_ns"),
        "completed_ns": _integer(
            raw.get("completed_ns"), label=f"{label}.completed_ns", optional=True
        ),
        "elapsed_ns": _integer(raw.get("elapsed_ns", 0), label=f"{label}.elapsed_ns"),
        "heartbeat_ns": _integer(
            raw.get("heartbeat_ns"), label=f"{label}.heartbeat_ns", optional=True
        ),
        "error_type": _text(
            raw.get("error_type"), label=f"{label}.error_type", limit=MAX_ERROR_CHARS, optional=True
        ),
        "resume_capability": capability,
        "replayability": replayability,
        "candidates": _integer(raw.get("candidates", 0), label=f"{label}.candidates"),
        "processed": _integer(raw.get("processed", 0), label=f"{label}.processed"),
        "cache_hits": _integer(raw.get("cache_hits", 0), label=f"{label}.cache_hits"),
        "new_work": _integer(raw.get("new_work", 0), label=f"{label}.new_work"),
        "cached_errors": _integer(raw.get("cached_errors", 0), label=f"{label}.cached_errors"),
        "replay_status": _text(
            raw.get("replay_status", "unobserved"), label=f"{label}.replay_status", limit=64
        ),
        "phases": phases,
    }


def _stage(value: object, *, label: str, run_id: int, digest: str | None) -> dict[str, object]:
    raw = _mapping(value, label=label)
    _keys(raw, _STAGE_FIELDS, label=label)
    if raw.get("schema") != RUN_STAGE_SCHEMA:
        raise LifecycleStatusContractError(f"{label} schema is unsupported", kind="schema")
    if raw.get("run_id") != run_id:
        raise LifecycleStatusContractError(f"{label} owner does not match run")
    stage_digest = _digest(
        raw.get("manifest_digest"), label=f"{label}.manifest_digest", optional=True
    )
    if digest is not None and stage_digest != digest:
        raise LifecycleStatusContractError(f"{label} is detached from its manifest")
    return {
        "schema": RUN_STAGE_SCHEMA,
        "run_id": run_id,
        "manifest_digest": stage_digest,
        "stage": _text(raw.get("stage"), label=f"{label}.stage"),
        "status": _text(raw.get("status"), label=f"{label}.status", limit=64),
        "details": _object_metadata(raw.get("details", {}), label=f"{label}.details"),
        "idempotency_key": _text(raw.get("idempotency_key"), label=f"{label}.idempotency_key"),
        "event_id": _integer(raw.get("event_id"), label=f"{label}.event_id", optional=True),
    }


def _checkpoint(
    value: object,
    *,
    label: str,
    run_id: int,
    digest: str | None,
) -> dict[str, object]:
    """Validate one manifest-bound checkpoint at the public read boundary."""

    raw = _mapping(value, label=label)
    _keys(raw, _CHECKPOINT_FIELDS, label=label)
    if raw.get("schema") != RUN_CHECKPOINT_SCHEMA:
        raise LifecycleStatusContractError(f"{label} schema is unsupported", kind="schema")
    if raw.get("run_id") != run_id:
        raise LifecycleStatusContractError(f"{label} owner does not match run")
    checkpoint_digest = _digest(
        raw.get("manifest_digest"), label=f"{label}.manifest_digest", optional=True
    )
    if digest is not None and checkpoint_digest != digest:
        raise LifecycleStatusContractError(f"{label} is detached from its manifest")
    stage = _text(raw.get("stage"), label=f"{label}.stage", limit=256)
    idempotency_key = _text(
        raw.get("idempotency_key"), label=f"{label}.idempotency_key", limit=256
    )
    assert stage is not None and idempotency_key is not None
    return {
        "schema": RUN_CHECKPOINT_SCHEMA,
        "run_id": run_id,
        "manifest_digest": checkpoint_digest,
        "stage": stage,
        "checkpoint": _object_metadata(raw.get("checkpoint", {}), label=f"{label}.checkpoint"),
        "idempotency_key": idempotency_key,
        "event_id": _integer(raw.get("event_id"), label=f"{label}.event_id", optional=True),
    }


def _lifecycle_error(value: object, *, label: str) -> dict[str, object]:
    raw = _mapping(value, label=label)
    _keys(raw, frozenset({"route_name", "error_type"}), label=label)
    return {
        "route_name": _text(raw.get("route_name"), label=f"{label}.route_name"),
        "error_type": _text(
            raw.get("error_type"), label=f"{label}.error_type", limit=MAX_ERROR_CHARS
        ),
    }


def _lifecycle(
    value: object,
    *,
    label: str,
    run_id: int | None,
    digest: str | None,
    run_status: str | None = None,
) -> dict[str, object]:
    raw = _mapping(value, label=label)
    _keys(raw, _LIFECYCLE_FIELDS, label=label)
    if raw.get("schema") != LIFECYCLE_ENVELOPE_SCHEMA:
        raise LifecycleStatusContractError(f"{label} schema is unsupported", kind="schema")
    actual_id = _integer(raw.get("run_id"), label=f"{label}.run_id", optional=True, minimum=1)
    if run_id is not None and actual_id != run_id:
        raise LifecycleStatusContractError(f"{label} owner does not match run")
    actual_digest = _digest(
        raw.get("manifest_digest"), label=f"{label}.manifest_digest", optional=True
    )
    if digest is not None and actual_digest != digest:
        raise LifecycleStatusContractError(f"{label} digest does not match manifest")
    status = _text(raw.get("status"), label=f"{label}.status", limit=64)
    assert status is not None
    if run_status is not None and status != run_status:
        raise LifecycleStatusContractError(f"{label} status does not match run")
    stage_id = actual_id if actual_id is not None else run_id
    return {
        "schema": LIFECYCLE_ENVELOPE_SCHEMA,
        "status": status,
        "run_id": actual_id,
        "source_run_id": _integer(
            raw.get("source_run_id"), label=f"{label}.source_run_id", optional=True, minimum=1
        ),
        "manifest_digest": actual_digest,
        "resumed_from": _integer(
            raw.get("resumed_from"), label=f"{label}.resumed_from", optional=True, minimum=1
        ),
        "resumed": _boolean(raw.get("resumed"), label=f"{label}.resumed"),
        "replayed": _boolean(raw.get("replayed"), label=f"{label}.replayed"),
        "skipped": _route_names(raw.get("skipped", []), label=f"{label}.skipped"),
        "non_replayable": _route_names(
            raw.get("non_replayable", []), label=f"{label}.non_replayable"
        ),
        "budget": _budget(raw.get("budget"), label=f"{label}.budget"),
        "recovery": None
        if raw.get("recovery") is None
        else _object_metadata(raw.get("recovery"), label=f"{label}.recovery"),
        "stages": [
            _stage(
                item,
                label=f"{label}.stages[{index}]",
                run_id=(
                    stage_id
                    if stage_id is not None
                    else _integer(
                        _mapping(item, label=f"{label}.stages[{index}]").get("run_id"),
                        label=f"{label}.stages[{index}].run_id",
                        minimum=1,
                    )
                ),
                digest=actual_digest,
            )
            for index, item in enumerate(
                _items(raw.get("stages", []), label=f"{label}.stages", limit=MAX_STAGES)
            )
        ],
        "checkpoints": [
            _checkpoint(
                item,
                label=f"{label}.checkpoints[{index}]",
                run_id=(
                    stage_id
                    if stage_id is not None
                    else _integer(
                        _mapping(item, label=f"{label}.checkpoints[{index}]").get("run_id"),
                        label=f"{label}.checkpoints[{index}].run_id",
                        minimum=1,
                    )
                ),
                digest=actual_digest,
            )
            for index, item in enumerate(
                _items(raw.get("checkpoints", []), label=f"{label}.checkpoints", limit=MAX_STAGES)
            )
        ],
        "route_capabilities": None
        if raw.get("route_capabilities") is None
        else _capabilities(raw.get("route_capabilities"), label=f"{label}.route_capabilities"),
        "routes": [
            _route(item, label=f"{label}.routes[{index}]")
            for index, item in enumerate(
                _items(raw.get("routes", []), label=f"{label}.routes", limit=MAX_ROUTES)
            )
        ],
        "errors": [
            _lifecycle_error(item, label=f"{label}.errors[{index}]")
            for index, item in enumerate(
                _items(raw.get("errors", []), label=f"{label}.errors", limit=MAX_ROUTES)
            )
        ],
    }


def _run(value: object, *, label: str, verify_manifest: bool = True) -> dict[str, object]:
    raw = _mapping(value, label=label)
    _keys(raw, _RUN_FIELDS, label=label)
    run_id = _integer(raw.get("run_id"), label=f"{label}.run_id", minimum=1)
    run_kind = _text(raw.get("run_kind"), label=f"{label}.run_kind", limit=32)
    status = _text(raw.get("status"), label=f"{label}.status", limit=64)
    assert run_kind is not None and status is not None
    if run_kind not in _RUN_KINDS:
        raise LifecycleStatusContractError(f"{label}.run_kind is unsupported", kind="schema")
    manifest = _manifest(
        raw.get("manifest"),
        run_id=run_id,
        label=f"{label}.manifest",
        verify_digest=verify_manifest,
    )
    digest = None if manifest is None else str(manifest["digest"])
    root = _text(raw.get("root"), label=f"{label}.root", limit=MAX_PATH_CHARS)
    assert root is not None
    if manifest is not None and manifest["root"] != root:
        raise LifecycleStatusContractError(f"{label}.root does not match manifest")
    routes = [
        _route(item, label=f"{label}.routes[{index}]")
        for index, item in enumerate(
            _items(raw.get("routes", []), label=f"{label}.routes", limit=MAX_ROUTES)
        )
    ]
    stages = [
        _stage(item, label=f"{label}.stages[{index}]", run_id=run_id, digest=digest)
        for index, item in enumerate(
            _items(raw.get("stages", []), label=f"{label}.stages", limit=MAX_STAGES)
        )
    ]
    checkpoints = [
        _checkpoint(
            item,
            label=f"{label}.checkpoints[{index}]",
            run_id=run_id,
            digest=digest,
        )
        for index, item in enumerate(
            _items(raw.get("checkpoints", []), label=f"{label}.checkpoints", limit=MAX_STAGES)
        )
    ]
    return {
        "run_id": run_id,
        "run_kind": run_kind,
        "status": status,
        "root": root,
        "source_run_id": _integer(
            raw.get("source_run_id"), label=f"{label}.source_run_id", optional=True, minimum=1
        ),
        "current_phase": _text(
            raw.get("current_phase"), label=f"{label}.current_phase", optional=True
        ),
        "owner_pid": _integer(
            raw.get("owner_pid"), label=f"{label}.owner_pid", optional=True, minimum=1
        ),
        "owner_alive": _boolean(
            raw.get("owner_alive"), label=f"{label}.owner_alive", optional=True
        ),
        "heartbeat_ns": _integer(
            raw.get("heartbeat_ns"), label=f"{label}.heartbeat_ns", optional=True
        ),
        "heartbeat_stale": _boolean(
            raw.get("heartbeat_stale"), label=f"{label}.heartbeat_stale", optional=True
        ),
        "started_ns": _integer(raw.get("started_ns"), label=f"{label}.started_ns"),
        "completed_ns": _integer(
            raw.get("completed_ns"), label=f"{label}.completed_ns", optional=True
        ),
        "elapsed_ns": _integer(raw.get("elapsed_ns"), label=f"{label}.elapsed_ns"),
        "recovery_required_actions": _integer(
            raw.get("recovery_required_actions", 0), label=f"{label}.recovery_required_actions"
        ),
        "manifest": manifest,
        "budget": _budget(raw.get("budget"), label=f"{label}.budget"),
        "recovery": None
        if raw.get("recovery") is None
        else _object_metadata(raw.get("recovery"), label=f"{label}.recovery"),
        "resumed": _boolean(raw.get("resumed"), label=f"{label}.resumed"),
        "replayed": _boolean(raw.get("replayed"), label=f"{label}.replayed"),
        "skipped_routes": _route_names(
            raw.get("skipped_routes", []), label=f"{label}.skipped_routes"
        ),
        "non_replayable_routes": _route_names(
            raw.get("non_replayable_routes", []), label=f"{label}.non_replayable_routes"
        ),
        "stages": stages,
        "checkpoints": checkpoints,
        "route_capabilities": None
        if raw.get("route_capabilities") is None
        else _capabilities(raw.get("route_capabilities"), label=f"{label}.route_capabilities"),
        "lifecycle": _lifecycle(
            raw.get("lifecycle"),
            label=f"{label}.lifecycle",
            run_id=run_id,
            digest=digest,
            run_status=status,
        ),
        "routes": routes,
    }


def _decode(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise LifecycleStatusContractError(f"{label} is not JSON text")
    try:
        decoded = json.loads(
            value, parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LifecycleStatusContractError(f"{label} is malformed JSON") from exc
    return dict(_mapping(decoded, label=label))


def _error_envelope(
    *,
    state: Path,
    limit: int,
    run_id: int | None,
    request_id: str,
    code: str,
    status: str,
    exit_code: int,
    message: str,
) -> dict[str, object]:
    return {
        "schema": LIFECYCLE_ENVELOPE_SCHEMA,
        "kind": LIFECYCLE_STATUS_KIND,
        "operation": LIFECYCLE_STATUS_OPERATION,
        "request_id": request_id,
        "read_only": True,
        "coverage": "unavailable",
        "status": status,
        "exit_code": exit_code,
        "error": {"code": code, "message": message},
        "state_directory": sanitize_untrusted_text(str(state), limit=MAX_PATH_CHARS),
        "limit": limit,
        "run_id": run_id,
        "runs": [],
        "result": {"count": 0, "run_ids": []},
        "lifecycle": {
            "schema": LIFECYCLE_ENVELOPE_SCHEMA,
            "status": status,
            "run_id": run_id,
            "source_run_id": None,
            "manifest_digest": None,
            "resumed_from": None,
            "resumed": False,
            "replayed": False,
            "skipped": [],
            "non_replayable": [],
            "budget": None,
            "recovery": None,
            "stages": [],
            "checkpoints": [],
            "route_capabilities": None,
            "routes": [],
            "errors": [],
        },
    }


def _classify(exc: BaseException) -> tuple[str, str, int, str]:
    text = str(exc).lower()
    if isinstance(exc, LifecycleStatusContractError) and exc.kind == "schema":
        return "schema_incompatible", "schema_incompatible", 6, "lifecycle schema is unsupported"
    if "unsupported" in text or "future" in text or ("schema" in text and "malformed" not in text):
        return "schema_incompatible", "schema_incompatible", 6, "lifecycle schema is unsupported"
    if isinstance(exc, (LifecycleStatusContractError, sqlite3.DatabaseError, json.JSONDecodeError)):
        return "state_corrupt", "corrupt", 7, "framework lifecycle snapshot is corrupt"
    detail = sanitize_untrusted_text(str(exc), limit=MAX_ERROR_CHARS)
    message = "framework lifecycle state is unavailable"
    if detail:
        message = f"{message}: {detail}"
    return "state_unavailable", "unavailable", 1, message


def _validate_envelope(payload: dict[str, object]) -> dict[str, object]:
    expected = {
        "schema",
        "kind",
        "operation",
        "request_id",
        "read_only",
        "coverage",
        "status",
        "exit_code",
        "error",
        "state_directory",
        "limit",
        "run_id",
        "runs",
        "result",
        "lifecycle",
    }
    _keys(payload, frozenset(expected), label="lifecycle envelope")
    if payload.get("schema") != LIFECYCLE_ENVELOPE_SCHEMA:
        raise LifecycleStatusContractError(
            "lifecycle envelope schema is unsupported", kind="schema"
        )
    if (
        payload.get("kind") != LIFECYCLE_STATUS_KIND
        or payload.get("operation") != LIFECYCLE_STATUS_OPERATION
    ):
        raise LifecycleStatusContractError("lifecycle envelope identity is invalid")
    if payload.get("read_only") is not True:
        raise LifecycleStatusContractError("lifecycle envelope is not read-only")
    request_id = _text(payload.get("request_id"), label="request_id")
    state_directory = _text(
        payload.get("state_directory"), label="state_directory", limit=MAX_PATH_CHARS
    )
    coverage = _text(payload.get("coverage"), label="coverage", limit=32)
    status = _text(payload.get("status"), label="status", limit=32)
    assert (
        request_id is not None
        and state_directory is not None
        and coverage is not None
        and status is not None
    )
    if coverage not in {"complete", "unavailable"} or status not in {
        "ok",
        "unavailable",
        "schema_incompatible",
        "corrupt",
    }:
        raise LifecycleStatusContractError("lifecycle outcome is unsupported", kind="schema")
    limit = _integer(payload.get("limit"), label="limit", minimum=1)
    assert limit is not None and limit <= MAX_RUNS
    requested = _integer(payload.get("run_id"), label="run_id", optional=True, minimum=1)
    exit_code = payload.get("exit_code")
    if type(exit_code) is not int or exit_code not in {0, 1, 6, 7}:
        raise LifecycleStatusContractError("exit_code is unsupported")
    raw_error = payload.get("error")
    error: dict[str, object] | None
    if raw_error is None:
        error = None
    else:
        error_map = _mapping(raw_error, label="error")
        _keys(error_map, frozenset({"code", "message", "retryable"}), label="error")
        error = {
            "code": _text(error_map.get("code"), label="error.code", limit=64),
            "message": _text(
                error_map.get("message"), label="error.message", limit=MAX_ERROR_CHARS
            ),
        }
        if "retryable" in error_map:
            error["retryable"] = _boolean(error_map["retryable"], label="error.retryable")
    runs = [
        _run(item, label=f"runs[{index}]", verify_manifest=False)
        for index, item in enumerate(_items(payload.get("runs"), label="runs", limit=MAX_RUNS))
    ]
    result = _mapping(payload.get("result"), label="result")
    if set(result) != {"count", "run_ids"}:
        raise LifecycleStatusContractError("result contains unsupported fields", kind="schema")
    count = _integer(result.get("count"), label="result.count")
    run_ids = [
        _integer(item, label="result.run_ids", minimum=1)
        for item in _items(result.get("run_ids"), label="result.run_ids", limit=MAX_RUNS)
    ]
    assert count is not None
    if count != len(runs) or run_ids != [run["run_id"] for run in runs]:
        raise LifecycleStatusContractError("result does not match runs")
    lifecycle = _lifecycle(
        payload.get("lifecycle"), label="lifecycle", run_id=requested, digest=None
    )
    if coverage == "complete" and (status != "ok" or exit_code != 0 or error is not None):
        raise LifecycleStatusContractError("complete lifecycle status has an error")
    if coverage == "unavailable" and (status == "ok" or exit_code == 0 or error is None):
        raise LifecycleStatusContractError("unavailable lifecycle status lacks an error")
    return {
        "schema": LIFECYCLE_ENVELOPE_SCHEMA,
        "kind": LIFECYCLE_STATUS_KIND,
        "operation": LIFECYCLE_STATUS_OPERATION,
        "request_id": request_id,
        "read_only": True,
        "coverage": coverage,
        "status": status,
        "exit_code": exit_code,
        "error": error,
        "state_directory": state_directory,
        "limit": limit,
        "run_id": requested,
        "runs": runs,
        "result": {"count": count, "run_ids": run_ids},
        "lifecycle": lifecycle,
    }


def lifecycle_status_payload(
    *,
    limit: int = 5,
    run_id: int | None = None,
    state_directory: str | Path | None = None,
) -> dict[str, object]:
    """Return one bounded lifecycle envelope without starting a run."""

    if type(limit) is not int or not 1 <= limit <= MAX_RUNS:
        raise ValueError("lifecycle status limit must be between 1 and 20")
    if run_id is not None and (type(run_id) is not int or run_id < 1):
        raise ValueError("lifecycle status run_id must be positive")
    if state_directory is None:
        state = default_state_directory()
    elif isinstance(state_directory, (str, Path)):
        state = Path(state_directory)
    else:
        raise ValueError("lifecycle status state_directory must be a path")
    request_id = f"lifecycle-{uuid4().hex}"
    database = state / "framework.sqlite3"
    if not database.is_file():
        return _validate_envelope(
            _error_envelope(
                state=state,
                limit=limit,
                run_id=run_id,
                request_id=request_id,
                code="state_unavailable",
                status="unavailable",
                exit_code=1,
                message="framework state is absent",
            )
        )
    try:
        statuses = read_run_status(database, limit=limit, run_id=run_id)
        runs = [
            _run(
                _decode(serialized_run_status(status), label=f"runs[{index}]"),
                label=f"runs[{index}]",
            )
            for index, status in enumerate(statuses)
        ]
        stages: list[object] = []
        for run in runs:
            raw_stages = run["stages"]
            if not isinstance(raw_stages, list):
                raise LifecycleStatusContractError("run stages are invalid")
            stages.extend(raw_stages)
        stages = stages[-MAX_STAGES:]
        checkpoints: list[object] = []
        for run in runs:
            raw_checkpoints = run["checkpoints"]
            if not isinstance(raw_checkpoints, list):
                raise LifecycleStatusContractError("run checkpoints are invalid")
            checkpoints.extend(raw_checkpoints)
        checkpoints = checkpoints[-MAX_STAGES:]
        payload = {
            "schema": LIFECYCLE_ENVELOPE_SCHEMA,
            "kind": LIFECYCLE_STATUS_KIND,
            "operation": LIFECYCLE_STATUS_OPERATION,
            "request_id": request_id,
            "read_only": True,
            "coverage": "complete",
            "status": "ok",
            "exit_code": 0,
            "error": None,
            "state_directory": str(state),
            "limit": limit,
            "run_id": run_id,
            "runs": runs,
            "result": {"count": len(runs), "run_ids": [run["run_id"] for run in runs]},
            "lifecycle": {
                "schema": LIFECYCLE_ENVELOPE_SCHEMA,
                "status": "ok",
                "run_id": run_id,
                "source_run_id": None,
                "manifest_digest": None,
                "resumed_from": None,
                "resumed": any(bool(run["resumed"]) for run in runs),
                "replayed": any(bool(run["replayed"]) for run in runs),
                "skipped": [],
                "non_replayable": [],
                "budget": None,
                "recovery": None,
                "stages": stages,
                "checkpoints": checkpoints,
                "route_capabilities": None,
                "routes": [],
                "errors": [],
            },
        }
        return _validate_envelope(payload)
    except (
        LifecycleStatusContractError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as exc:
        code, status, exit_code, message = _classify(exc)
        return _validate_envelope(
            _error_envelope(
                state=state,
                limit=limit,
                run_id=run_id,
                request_id=request_id,
                code=code,
                status=status,
                exit_code=exit_code,
                message=message,
            )
        )


__all__ = [
    "LIFECYCLE_ENVELOPE_SCHEMA",
    "LIFECYCLE_STATUS_KIND",
    "LIFECYCLE_STATUS_OPERATION",
    "RUN_CHECKPOINT_SCHEMA",
    "LifecycleStatusContractError",
    "lifecycle_status_payload",
]
