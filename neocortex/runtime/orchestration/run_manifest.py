"""Durable, read-only metadata for one Framework lifecycle run.

The manifest is intentionally a small pure contract.  It does not grant any
authority and it does not contain corpus contents; it binds a run to the
effective boundary, selected routes, source run and bounded work policy so a
later status/resume reader can distinguish replay from a new execution.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping


RUN_MANIFEST_SCHEMA = "neocortex.run-manifest/v1"
RUN_BUDGET_SCHEMA = "neocortex.run-budget/v1"
RUN_STAGE_SCHEMA = "neocortex.lifecycle-stage/v1"
RUN_CHECKPOINT_SCHEMA = "neocortex.lifecycle-checkpoint/v1"
RUN_RECOVERY_SCHEMA = "neocortex.lifecycle-recovery/v1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _bounded_mapping(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    result = dict(value)
    encoded = _canonical_json(result).encode("utf-8")
    if len(encoded) > 512 * 1024:
        raise ValueError(f"{label} exceeds the durable manifest limit")
    return result


@dataclass(frozen=True, slots=True)
class RunBudget:
    """The durable, process-independent limits attached to one run.

    Framework routes have historically exposed several local limits (memory,
    pages, or documents), but none of those limits was shared by workers.  The
    lifecycle budget intentionally has a very small contract and is persisted
    separately from those route-specific knobs.  ``None`` means that a
    dimension is observed but not capped.
    """

    max_items: int | None = None
    max_bytes: int | None = None
    max_duration_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("max_items", "max_bytes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or null")
        if self.max_duration_seconds is not None and (
            type(self.max_duration_seconds) not in {int, float}
            or self.max_duration_seconds <= 0
            or not math.isfinite(float(self.max_duration_seconds))
        ):
            raise ValueError("max_duration_seconds must be positive or null")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "RunBudget":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("run budget must be an object")

        def first(*names: str) -> Any:
            for name in names:
                if name in value:
                    return value[name]
            return None

        return cls(
            max_items=first("max_items", "items"),
            max_bytes=first("max_bytes", "bytes"),
            max_duration_seconds=first(
                "max_duration_seconds", "time_budget_seconds", "time_seconds"
            ),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema": RUN_BUDGET_SCHEMA,
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "max_duration_seconds": self.max_duration_seconds,
        }

    def as_mapping(self) -> dict[str, Any]:
        """Return the bounded mapping used inside a run manifest."""

        return self.payload()


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Canonical identity of one initial, route-only or resumed run."""

    run_id: int
    run_kind: str
    root: str
    root_identity: tuple[int, int, int]
    selected_routes: tuple[str, ...]
    source_run_id: int | None = None
    configuration: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)
    input_snapshot: Mapping[str, Any] = field(default_factory=dict)
    route_capabilities: Mapping[str, str] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        if self.run_id < 1:
            raise ValueError("run_id must be positive")
        if self.run_kind not in {"initial", "route_only", "resume"}:
            raise ValueError(f"unsupported run kind: {self.run_kind}")
        if len(self.root.encode("utf-8")) > 8192 or not self.root:
            raise ValueError("manifest root is empty or too large")
        if len(self.root_identity) != 3 or any(type(item) is not int for item in self.root_identity):
            raise ValueError("manifest root identity is malformed")
        routes = tuple(sorted({str(route) for route in self.selected_routes}))
        if any(not route or len(route) > 128 for route in routes):
            raise ValueError("manifest route name is empty or too large")
        capabilities = {
            route: str(self.route_capabilities.get(route, "not_resumable"))
            for route in routes
        }
        if any(
            value not in {"phase_resume", "safe_replay", "not_resumable"}
            for value in capabilities.values()
        ):
            raise ValueError("manifest route lifecycle capability is unsupported")
        return {
            "schema": RUN_MANIFEST_SCHEMA,
            "run_id": self.run_id,
            "run_kind": self.run_kind,
            "source_run_id": self.source_run_id,
            "root": self.root,
            "root_identity": list(self.root_identity),
            "selected_routes": list(routes),
            "route_capabilities": capabilities,
            "configuration": _bounded_mapping(self.configuration, label="configuration"),
            "budget": _bounded_mapping(self.budget, label="budget"),
            "input_snapshot": _bounded_mapping(self.input_snapshot, label="input_snapshot"),
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.payload())

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def event_payload(self) -> dict[str, Any]:
        payload = self.payload()
        payload["digest"] = self.digest()
        return payload


def verify_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return one persisted manifest without mutating it."""

    if not isinstance(payload, Mapping):
        raise ValueError("run manifest must be an object")
    if payload.get("schema") != RUN_MANIFEST_SCHEMA:
        raise ValueError("unsupported run manifest schema")
    expected = payload.get("digest")
    if (
        not isinstance(expected, str)
        or len(expected) != len("sha256:") + 64
        or not expected.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in expected[7:])
    ):
        raise ValueError("run manifest digest is missing")
    unsigned = dict(payload)
    unsigned.pop("digest", None)
    actual = "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()
    if actual != expected:
        raise ValueError("run manifest digest does not match its payload")
    run_id = payload.get("run_id")
    if type(run_id) is not int or run_id < 1:
        raise ValueError("run manifest run_id is invalid")
    if payload.get("run_kind") not in {"initial", "route_only", "resume"}:
        raise ValueError("run manifest run_kind is unsupported")
    root = payload.get("root")
    if not isinstance(root, str) or not root or len(root.encode("utf-8")) > 8192:
        raise ValueError("run manifest root is invalid")
    root_identity = payload.get("root_identity")
    if (
        not isinstance(root_identity, list)
        or len(root_identity) != 3
        or any(type(value) is not int for value in root_identity)
    ):
        raise ValueError("run manifest root identity is invalid")
    selected_routes = payload.get("selected_routes")
    if not isinstance(selected_routes, list) or any(
        not isinstance(route, str) or not route or len(route) > 128
        for route in selected_routes
    ):
        raise ValueError("run manifest selected routes are invalid")
    capabilities = payload.get("route_capabilities")
    if capabilities is not None:
        if not isinstance(capabilities, Mapping) or set(capabilities) != set(selected_routes):
            raise ValueError("run manifest route capabilities are invalid")
        if any(
            not isinstance(value, str)
            or value not in {"phase_resume", "safe_replay", "not_resumable"}
            for value in capabilities.values()
        ):
            raise ValueError("run manifest route capability is unsupported")
    for name in ("configuration", "budget", "input_snapshot"):
        if not isinstance(payload.get(name), Mapping):
            raise ValueError(f"run manifest {name} is invalid")
    source_run_id = payload.get("source_run_id")
    if source_run_id is not None and (type(source_run_id) is not int or source_run_id < 1):
        raise ValueError("run manifest source_run_id is invalid")
    return dict(payload)


def lifecycle_envelope(
    *,
    manifest: Mapping[str, Any] | None,
    status: str,
    run_id: int | None = None,
    source_run_id: int | None = None,
    routes: tuple[Mapping[str, Any], ...] = (),
    errors: tuple[Mapping[str, Any], ...] = (),
    resumed_from: int | None = None,
    resumed: bool | None = None,
    replayed: bool = False,
    skipped: tuple[str, ...] = (),
    non_replayable: tuple[str, ...] = (),
    budget: Mapping[str, Any] | None = None,
    recovery: Mapping[str, Any] | None = None,
    stages: tuple[Mapping[str, Any], ...] = (),
    checkpoints: tuple[Mapping[str, Any], ...] = (),
    route_capabilities: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a bounded read-only envelope shared by status callers."""

    return {
        "schema": "neocortex.lifecycle-envelope/v1",
        "status": status,
        "run_id": (
            manifest.get("run_id")
            if manifest is not None
            else run_id
        ),
        "source_run_id": (
            manifest.get("source_run_id")
            if manifest is not None
            else source_run_id
        ),
        "manifest_digest": None if manifest is None else manifest.get("digest"),
        "resumed_from": resumed_from,
        "resumed": bool(resumed) if resumed is not None else resumed_from is not None,
        "replayed": bool(replayed),
        "skipped": list(skipped),
        "non_replayable": list(non_replayable),
        "budget": None if budget is None else dict(budget),
        "recovery": None if recovery is None else dict(recovery),
        "stages": [dict(stage) for stage in stages],
        "checkpoints": [dict(checkpoint) for checkpoint in checkpoints],
        "route_capabilities": (
            None if route_capabilities is None else dict(route_capabilities)
        ),
        "routes": [dict(route) for route in routes],
        "errors": [dict(error) for error in errors],
    }


__all__ = [
    "RUN_BUDGET_SCHEMA",
    "RUN_CHECKPOINT_SCHEMA",
    "RUN_MANIFEST_SCHEMA",
    "RUN_RECOVERY_SCHEMA",
    "RUN_STAGE_SCHEMA",
    "RunBudget",
    "RunManifest",
    "lifecycle_envelope",
    "verify_event_payload",
]
