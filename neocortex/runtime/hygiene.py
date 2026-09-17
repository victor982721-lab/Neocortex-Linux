"""Read-only hygiene orchestration for NeoCortex-owned state.

The hygiene surface is intentionally an orchestrator, not another owner.  It
federates the bounded plans produced by the artifact registry, the registered
scratch manager, and the retention owner.  In particular, it does not walk a
generic filesystem, open SQLite itself, or expose an apply/delete operation.

Roots are opt-in.  With no explicit root (or owner object) this module only
reports deferred scopes; it never guesses ``/tmp``, ``/`` or the corpus root.
The owner modules are imported at the point at which they are needed so
importing this diagnostic does not create an import cycle or make an optional
owner a requirement for callers that do not select it.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast


HYGIENE_SCHEMA = "neocortex.hygiene/v1"

# The orchestrator has four fixed, independently owned surfaces.  Keeping the
# order stable makes receipts and tests deterministic and keeps JSON bounded.
HYGIENE_COMPONENTS: tuple[str, ...] = (
    "artifact_registry",
    "owned-temp",
    "audit-work",
    "retention",
)
SCRATCH_SCOPES: tuple[str, ...] = ("owned-temp", "audit-work")

MAX_HYGIENE_COMPONENTS = 4
MAX_HYGIENE_ITEMS = 64
MAX_HYGIENE_REASONS = 64
MAX_HYGIENE_REASON_BYTES = 512
MAX_HYGIENE_PATH_BYTES = 1_024
MAX_HYGIENE_JSON_BYTES = 64 * 1024


def _bounded_text(value: object, *, limit: int = MAX_HYGIENE_REASON_BYTES) -> str:
    """Return deterministic bounded text suitable for diagnostic JSON."""

    text = str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    # Decode after truncating so a multi-byte code point cannot make the
    # output exceed the declared byte bound.
    return encoded[:limit].decode("utf-8", errors="replace")


def _bounded_path(value: object) -> str:
    return _bounded_text(value, limit=MAX_HYGIENE_PATH_BYTES)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _stable_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    return default


def _count(value: object, default: int = 0) -> int:
    """Read a count from owner payloads without trusting arbitrary objects."""

    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, (str, bytes, bytearray)):
        return default
    if isinstance(value, Mapping):
        for key in ("count", "total", "items", "records", "value"):
            if key in value:
                found = _count(value[key], default=-1)
                if found >= 0:
                    return found
        return default
    try:
        return max(0, len(value))  # type: ignore[arg-type]
    except (TypeError, AttributeError):
        return default


def _attr_or_key(owner: object, payload: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in payload:
            return payload[name]
        # Canonical owner serializers put counters under ``counts`` and
        # physical estimates under ``bytes``.  Keep the lookup central so a
        # mapping plan and a typed plan classify identically without requiring
        # either owner to duplicate top-level aliases.
        section_names = ("bytes", "counts", "accounting") if name.endswith("_bytes") else (
            "counts",
            "accounting",
            "bytes",
        )
        for section_name in section_names:
            section = payload.get(section_name)
            if not isinstance(section, Mapping):
                continue
            if name in section:
                return section[name]
            if name.endswith("_bytes") and name.removesuffix("_bytes") in section:
                return section[name.removesuffix("_bytes")]
        try:
            value = getattr(owner, name)
        except AttributeError:
            continue
        if value is not None:
            return value
    return None


def _owner_payload(owner: object) -> dict[str, object]:
    """Extract a bounded, JSON-shaped view from a producer plan.

    The full owner plan is retained privately for the registry's revalidation
    call.  Only scalar counters and short diagnostics cross the hygiene JSON
    boundary; an owner cannot make this orchestrator emit an unbounded list.
    """

    if owner is None:
        return {}
    if isinstance(owner, Mapping):
        return dict(owner)
    # The canonical owner plans expose bounded scalar counters directly.  Do
    # not call a serializer that may materialize a large record page merely to
    # classify those counters; retain the object itself privately for verify.
    # Small test/embedding plans without these fields still use ``to_dict``.
    has_plan_shape = any(
        hasattr(owner, name)
        for name in ("records", "stores", "eligible", "planned", "protected", "blocked")
    )
    try:
        to_dict = getattr(owner, "to_dict", None)
    except Exception:
        to_dict = None
    if callable(to_dict) and not has_plan_shape:
        try:
            converted = to_dict()
        except Exception:
            return {}
        if isinstance(converted, Mapping):
            return dict(converted)
    # Dataclass-like owner plans in tests and embedders often expose fields but
    # no serializer.  Read only the known names rather than introspecting an
    # arbitrary object graph.
    result: dict[str, object] = {}
    for name in (
        "status",
        "reason",
        "root",
        "root_blocked",
        "records",
        "items",
        "stores",
        "unmanaged",
        "planned",
        "eligible",
        "candidates",
        "protected",
        "kept",
        "blocked",
        "unknown",
        "failed",
        "recovery_required",
        "limits",
        "max_entries",
        "max_depth",
        "max_bytes",
        "observed",
        "scanned",
    ):
        try:
            value = getattr(owner, name)
        except AttributeError:
            continue
        result[name] = value
    return result


def _bounded_paths(value: object, *, limit: int = MAX_HYGIENE_ITEMS) -> tuple[Path, ...]:
    """Normalize an owner ``unmanaged`` collection with a hard item bound."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, Path)):
        values: Sequence[object] = (value,)
    elif isinstance(value, Mapping):
        values = tuple(value.values())
    else:
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError:
            values = (value,)
    result: list[Path] = []
    seen: set[str] = set()
    for item in values:
        if len(result) >= limit:
            break
        if item is None:
            continue
        if isinstance(item, Mapping):
            item = item.get("path", item.get("root", ""))
        path = _bounded_path(item)
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(Path(path))
    return tuple(result)


def _bounded_reasons(
    owner: object,
    payload: Mapping[str, object],
    *extra: object,
) -> tuple[str, ...]:
    values: list[str] = []

    def add(value: object) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                add(f"{key}: {item}")
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                add(item)
            return
        text = _bounded_text(value)
        if text and text not in values and len(values) < MAX_HYGIENE_REASONS:
            values.append(text)

    add(_attr_or_key(owner, payload, "reason", "root_blocked", "detail", "error"))
    add(_attr_or_key(owner, payload, "reasons", "reason_codes"))
    for value in extra:
        add(value)
    return tuple(values)


def _status(owner: object, payload: Mapping[str, object], *, default: str = "ready") -> str:
    value = _attr_or_key(owner, payload, "status", "state")
    if value is None:
        return default
    text = _bounded_text(value, limit=128).strip().lower().replace("_", "-")
    return text or default


def _limits(owner: object, payload: Mapping[str, object]) -> dict[str, int]:
    raw = _attr_or_key(owner, payload, "limits")
    result: dict[str, int] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if len(result) >= 8:
                break
            result[_bounded_text(key, limit=64)] = _int(value)
    for name in ("max_entries", "max_depth", "max_bytes", "batch_size"):
        value = _attr_or_key(owner, payload, name)
        if value is not None and name not in result:
            result[name] = _int(value)
    return result


def _items_for_retention(owner: object, payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw = _attr_or_key(owner, payload, "stores")
    if raw is None:
        raw = _attr_or_key(owner, payload, "items", "records")
    if isinstance(raw, Mapping):
        values: tuple[object, ...] = tuple(raw.values())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = tuple(raw)
    else:
        values = ()
    result: list[Mapping[str, object]] = []
    for item in values[:MAX_HYGIENE_ITEMS]:
        if isinstance(item, Mapping):
            # Retention stores contain an ``items`` page; flatten it so the
            # disposition is counted even when the owner only exposes a
            # serializer.
            nested = item.get("items")
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                result.extend(
                    nested_item for nested_item in nested[:MAX_HYGIENE_ITEMS] if isinstance(nested_item, Mapping)
                )
            else:
                result.append(item)
        else:
            # ``RetentionPlan`` exposes typed ``RetentionStorePlan`` objects
            # rather than a ``to_dict`` method.  Keep this adapter read-only by
            # projecting only the fields needed for disposition accounting.
            nested = getattr(item, "items", None)
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                for nested_item in nested[:MAX_HYGIENE_ITEMS]:
                    disposition = getattr(nested_item, "disposition", None)
                    if disposition is not None:
                        result.append({"disposition": str(disposition)})
            else:
                status = getattr(item, "status", None)
                if status is not None:
                    result.append({"status": str(status)})
    return tuple(result[:MAX_HYGIENE_ITEMS])


@dataclass(frozen=True, slots=True)
class _ComponentSummary:
    name: str
    status: str = "deferred"
    observed: int = 0
    protected: int = 0
    eligible: int = 0
    blocked: int = 0
    unknown: int = 0
    unmanaged: tuple[Path, ...] = ()
    reasons: tuple[str, ...] = ()
    limits: Mapping[str, int] = field(default_factory=dict)
    root: str | None = None
    complete: bool = False
    observed_bytes: int = 0
    eligible_bytes: int = 0
    protected_bytes: int = 0
    blocked_bytes: int = 0
    fingerprint: str = ""
    # Selection identity is deliberately separate from the bounded diagnostic
    # page.  ``MAX_HYGIENE_ITEMS`` limits what we show in JSON; it must never
    # limit the claims used to decide whether a selection is still the same.
    selection_claims: int = 0
    selection_complete: bool = True
    selection_fingerprint: str = ""

    @property
    def available(self) -> bool:
        return self.status not in {"deferred", "absent", "blocked", "unknown"}

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "root": self.root,
            "complete": self.complete,
            "observed": self.observed,
            "protected": self.protected,
            "eligible": self.eligible,
            "blocked": self.blocked,
            "unknown": self.unknown,
            "observed_bytes": self.observed_bytes,
            "protected_bytes": self.protected_bytes,
            "eligible_bytes": self.eligible_bytes,
            "blocked_bytes": self.blocked_bytes,
            "selection_claims": self.selection_claims,
            "selection_complete": self.selection_complete,
            "selection_fingerprint": self.selection_fingerprint,
            "unmanaged": [str(path) for path in self.unmanaged[:MAX_HYGIENE_ITEMS]],
            "limits": dict(self.limits),
            "reasons": list(self.reasons[:MAX_HYGIENE_REASONS]),
        }


def _summary_for_owner(
    name: str,
    owner: object,
    *,
    fallback_reason: str | None = None,
    retention: bool = False,
) -> _ComponentSummary:
    """Classify one owner plan without calling any mutation API."""

    if owner is None:
        reason = fallback_reason or f"{name} owner is deferred"
        return _ComponentSummary(
            name,
            status="deferred",
            blocked=1,
            reasons=(reason,),
            selection_complete=False,
            fingerprint=_stable_digest({"name": name, "status": "deferred", "reason": reason}),
        )

    payload = _owner_payload(owner)
    status = _status(owner, payload)
    root_value = _attr_or_key(owner, payload, "root", "database", "state_directory")
    root = None if root_value is None else _bounded_path(root_value)
    unmanaged = _bounded_paths(_attr_or_key(owner, payload, "unmanaged"))
    reasons = _bounded_reasons(owner, payload, fallback_reason)
    limits = _limits(owner, payload)

    # ArtifactPlan publishes its counters under ``counts``/``bytes`` to keep
    # the owner envelope stable, while ScratchPlan exposes equivalent fields
    # directly.  Read both shapes so federation does not silently turn real
    # registry candidates into zeros.
    nested_counts = payload.get("counts")
    nested_counts = nested_counts if isinstance(nested_counts, Mapping) else {}
    nested_bytes = payload.get("bytes")
    nested_bytes = nested_bytes if isinstance(nested_bytes, Mapping) else {}

    def metric(names: Sequence[str], nested: Mapping[str, object], default: object = None) -> object:
        value = _attr_or_key(owner, payload, *names)
        if value is not None:
            return value
        for name in names:
            if name in nested:
                return nested[name]
        return default

    if retention:
        items = _items_for_retention(owner, payload)
        disposition_counts = {"eligible": 0, "protected": 0, "blocked": 0, "unknown": 0}
        for item in items:
            disposition = str(item.get("disposition", item.get("status", "unknown"))).lower()
            if disposition not in disposition_counts:
                disposition = "unknown"
            disposition_counts[disposition] += 1
        # The retention owner may expose store-level counts even when a page
        # is empty.  Prefer explicit aggregate values when they are present.
        eligible = _count(metric(("eligible", "proposed", "proposed_rows"), nested_counts), disposition_counts["eligible"])
        protected = _count(metric(("protected", "protected_rows"), nested_counts), disposition_counts["protected"])
        blocked = _count(metric(("blocked",), nested_counts), disposition_counts["blocked"])
        unknown = _count(metric(("unknown",), nested_counts), disposition_counts["unknown"])
        observed = _count(
            metric(("observed", "observed_rows", "scanned"), nested_counts),
            eligible + protected + blocked + unknown,
        )
        # ``plan_retention`` reports missing/unavailable stores in their
        # status/detail rather than as items.  A blocked store therefore
        # remains visible even on an empty page.
        stores = _attr_or_key(owner, payload, "stores")
        store_blocked = False
        if isinstance(stores, Sequence) and not isinstance(stores, (str, bytes)):
            for store in stores:
                if isinstance(store, Mapping):
                    store_status = str(store.get("status", "ready")).lower()
                    detail = store.get("detail") or store_status
                else:
                    try:
                        store_status = str(getattr(store, "status", "ready")).lower()
                    except Exception:
                        store_status = "ready"
                    try:
                        detail = getattr(store, "detail", None) or store_status
                    except Exception:
                        detail = store_status
                if store_status in {"absent", "blocked", "deferred", "failed"}:
                    store_blocked = True
                    blocked += 1
                    observed += 1
                    reasons = tuple(dict.fromkeys((*reasons, _bounded_text(detail))))[:MAX_HYGIENE_REASONS]
        if store_blocked:
            status = "blocked"
        protected_bytes = _int(metric(("protected_bytes", "protected"), nested_bytes))
        eligible_bytes = _int(metric(("eligible_bytes", "proposed_bytes", "eligible"), nested_bytes))
        blocked_bytes = _int(metric(("blocked_bytes", "blocked"), nested_bytes))
        observed_bytes = _int(
            metric(("observed_bytes", "observed"), nested_bytes),
            protected_bytes + eligible_bytes + blocked_bytes,
        )
    else:
        # Scratch uses ``planned`` and ``kept``; the artifact registry is
        # intentionally accepted with either those names or the more direct
        # eligible/protected vocabulary.
        eligible = _count(metric(("eligible", "planned", "candidates", "adoptable"), nested_counts))
        protected = _count(metric(("protected", "kept", "preserved", "held"), nested_counts))
        blocked = _count(metric(("blocked",), nested_counts))
        blocked += _count(metric(("failed",), nested_counts))
        blocked += _count(metric(("recovery_required",), nested_counts))
        unknown = _count(metric(("unknown",), nested_counts))
        observed = _count(
            metric(("observed", "scanned", "returned"), nested_counts),
            eligible + protected + blocked + unknown,
        )
        protected_bytes = _int(metric(("protected_bytes", "kept_bytes", "protected"), nested_bytes))
        eligible_bytes = _int(metric(("eligible_bytes", "planned_bytes", "eligible"), nested_bytes))
        blocked_bytes = _int(metric(("blocked_bytes", "failed_bytes", "recovery_required_bytes", "blocked"), nested_bytes))
        observed_bytes = _int(
            metric(("observed_bytes", "observed"), nested_bytes),
            protected_bytes + eligible_bytes + blocked_bytes,
        )

    root_blocked = _attr_or_key(owner, payload, "root_blocked")
    if root_blocked is not None:
        status = "blocked"
        blocked = max(1, blocked)
        reasons = tuple(dict.fromkeys((*reasons, _bounded_text(root_blocked))))[:MAX_HYGIENE_REASONS]
    if status in {"absent", "deferred"}:
        blocked = max(1, blocked)
    elif status in {"failed", "recovery-required", "recovery_required"}:
        status = "blocked"
        blocked = max(1, blocked)
    elif status not in {"ready", "planned", "kept", "observed", "protected", "blocked", "unknown"}:
        # Owner-specific future states are not safe to treat as available.
        status = "unknown"
        unknown = max(1, unknown)

    expected_claims = eligible + protected + blocked + unknown
    selection_fingerprint, selection_claims, selection_complete = _selection_claim_evidence(
        owner,
        payload,
        expected_count=expected_claims,
        require_count=not retention,
    )
    if not selection_complete:
        reasons = tuple(
            dict.fromkeys((*reasons, "selection_claims_incomplete"))
        )[:MAX_HYGIENE_REASONS]

    complete = (
        status not in {"deferred", "absent", "blocked", "unknown"}
        and selection_complete
    )
    if status == "unknown":
        unknown = max(1, unknown)
    stable = {
        "name": name,
        "status": status,
        "root": root,
        "observed": observed,
        "protected": protected,
        "eligible": eligible,
        "blocked": blocked,
        "unknown": unknown,
        "unmanaged": [str(path) for path in unmanaged],
        "reasons": list(reasons),
        "limits": dict(limits),
        "observed_bytes": observed_bytes,
        "protected_bytes": protected_bytes,
        "eligible_bytes": eligible_bytes,
        "blocked_bytes": blocked_bytes,
        "selection": {
            "count": selection_claims,
            "complete": selection_complete,
            "fingerprint": selection_fingerprint,
        },
    }
    return _ComponentSummary(
        name=name,
        status=status,
        observed=observed,
        protected=protected,
        eligible=eligible,
        blocked=blocked,
        unknown=unknown,
        unmanaged=unmanaged,
        reasons=reasons,
        limits=limits,
        root=root,
        complete=complete,
        observed_bytes=observed_bytes,
        eligible_bytes=eligible_bytes,
        protected_bytes=protected_bytes,
        blocked_bytes=blocked_bytes,
        fingerprint=_stable_digest(stable),
        selection_claims=selection_claims,
        selection_complete=selection_complete,
        selection_fingerprint=selection_fingerprint,
    )


def _source_payload(summary: _ComponentSummary) -> dict[str, object]:
    return summary.to_dict()


_SELECTION_FIELDS: tuple[str, ...] = (
    "artifact_id",
    "record_id",
    "id",
    "key",
    "owner",
    "producer",
    "purpose",
    "kind",
    "store",
    "database",
    "entity",
    "scope",
    "recorded_status",
    "disposition",
    "estimated_rows",
    "estimated_bytes",
    "schema_version",
    "state",
    "status",
    "classification",
    "eligible",
    "path",
    "root",
    "path_identity",
    "root_identity",
    "identity",
    "manifest_digest",
    "source_ref",
    "digest",
    "metadata",
    "dependencies",
    "reservations",
    "retain_until_ns",
    "ttl_ns",
    "retire_after_ns",
    "disposable",
    "retain_on_success",
    "result_paths",
    "size_bytes",
    "payload_size_bytes",
    "valid",
    "verified",
    "reason",
    "issue",
)


def _selection_values(owner: object, payload: Mapping[str, object]) -> tuple[object, ...]:
    """Return every owner claim, not merely the diagnostic page.

    The JSON envelope deliberately has a small presentation page.  Selection
    identity and physical accounting have a different contract: they must see
    the complete bounded owner plan.  Keeping this extraction in one helper
    prevents a future display limit from becoming an authorization limit.
    """

    raw = _attr_or_key(owner, payload, "records", "entries", "items", "stores")
    if isinstance(raw, Mapping):
        return tuple(raw.values())
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(raw)
    return ()


def _selection_source(item: object) -> Mapping[str, object]:
    if isinstance(item, Mapping):
        return item
    projected: dict[str, object] = {}
    for name in _SELECTION_FIELDS:
        try:
            value = getattr(item, name)
        except AttributeError:
            continue
        if value is not None:
            projected[name] = value
    # RetentionStorePlan and similar typed owner envelopes keep their actual
    # claim page in an ``items`` attribute.  It is intentionally not part of
    # a claim itself, but must remain visible to the flattening step below.
    nested = cast(Any, item).items if hasattr(item, "items") else None
    if nested is not None:
        projected["items"] = nested
    return projected


def _selection_scalar(value: object) -> object:
    """Normalize a claim without a page-sized or string-length cutoff."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, (list, tuple)):
        return [_selection_scalar(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_selection_scalar(item) for item in value]
        return sorted(normalized, key=_canonical_json)
    if isinstance(value, Mapping):
        normalized = {
            str(key): _selection_scalar(item)
            for key, item in value.items()
        }
        return {key: normalized[key] for key in sorted(normalized)}
    # Owner records are typed dataclasses.  This fallback is only for a
    # lightweight embedding's scalar value and remains deterministic.
    return str(value)


def _selection_claim(source: Mapping[str, object]) -> dict[str, object]:
    claim: dict[str, object] = {}
    for name in _SELECTION_FIELDS:
        if name not in source:
            continue
        value = _selection_scalar(source[name])
        # Dependency/reservation order is not selection identity.  Canonicalize
        # those set-like references so a filesystem/JSON ordering change does
        # not cause drift while membership changes still do.
        if name in {"dependencies", "reservations"} and isinstance(value, list):
            value = sorted(value, key=_canonical_json)
        claim[name] = value
    return claim


def _selection_claims(owner: object, payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """Project stable identity/claim fields for selection verification.

    Counters and reason histograms are useful summaries but are not a
    selection identity.  This stable projection intentionally excludes
    volatile timestamps while retaining the claims that authorize an owner
    proposal.  The returned tuple contains the complete owner plan.  It is
    kept as private evidence only; public JSON carries the digest and count.
    """

    claims: list[dict[str, object]] = []

    for item in _selection_values(owner, payload):
        source = _selection_source(item)
        # Retention stores can wrap their actual selected rows in ``items``.
        nested = source.get("items")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
            for nested_item in nested:
                nested_source = _selection_source(nested_item)
                claim = _selection_claim(nested_source)
                if claim:
                    claims.append(claim)
        else:
            claim = _selection_claim(source)
            if claim:
                claims.append(claim)

    # Owner plans normally arrive in path order, but canonical selection
    # identity must not depend on an accidental mapping/page order.
    claims.sort(key=_canonical_json)
    return tuple(claims)


def _selection_claim_evidence(
    owner: object,
    payload: Mapping[str, object],
    *,
    expected_count: int,
    require_count: bool = True,
) -> tuple[str, int, bool]:
    claims = _selection_claims(owner, payload)
    raw_present = _attr_or_key(owner, payload, "records", "entries", "items", "stores") is not None
    raw_values = _selection_values(owner, payload)
    truncated = bool(_attr_or_key(owner, payload, "truncated", "incomplete", "partial"))
    complete = not truncated
    if raw_values and not claims:
        complete = False
    if expected_count > 0 and (
        not raw_present or (require_count and len(claims) < expected_count)
    ):
        complete = False
    identity_fields = {
        "artifact_id",
        "record_id",
        "id",
        "key",
        "path",
        "path_identity",
        "identity",
        "manifest_digest",
    }
    if expected_count > 0 and any(
        not identity_fields.intersection(claim) for claim in claims
    ):
        complete = False
    # Length-prefix each claim so concatenations cannot collide (e.g. [ab,c]
    # versus [a,bc]).  This is an identity digest, not a user-facing JSON
    # serialization, so no MAX_HYGIENE_ITEMS cutoff is appropriate here.
    digest = hashlib.sha256()
    for claim in claims:
        encoded = _canonical_json(claim).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return "sha256:" + digest.hexdigest(), len(claims), complete


def _unique_eligible_bytes(owners: Mapping[str, object]) -> tuple[int, bool]:
    """Count recoverable bytes once when owners project one physical claim twice."""

    seen: set[tuple[object, ...]] = set()
    total = 0
    claims_seen = False
    for name in HYGIENE_COMPONENTS:
        owner = owners.get(name)
        if owner is None:
            continue
        payload = _owner_payload(owner)
        for item in _selection_values(owner, payload):
            source = _selection_source(item)
            nested = source.get("items")
            nested_values: Sequence[object]
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
                nested_values = tuple(nested)
            else:
                nested_values = (item,)
            for nested_item in nested_values:
                claim_seen = [False]
                total += _add_unique_eligible_claim(nested_item, seen, claims_seen_ref=claim_seen)
                claims_seen = claims_seen or claim_seen[0]

    return total, claims_seen


def _add_unique_eligible_claim(
    item: object,
    seen: set[tuple[object, ...]],
    claims_seen_ref: list[bool] | None = None,
) -> int:
    """Return one claim's bytes if it is eligible and not already physical."""

    def value(key: str) -> object:
        if isinstance(item, Mapping):
            return item.get(key)
        return getattr(item, key, None)

    classification = value("classification") or value("status")
    eligible = value("eligible") is True or classification == "eligible"
    if not eligible:
        return 0
    size = value("size_bytes")
    if not isinstance(size, int) or size < 0:
        size = value("payload_size_bytes")
    if not isinstance(size, int) or size < 0:
        return 0
    identity = value("path_identity") or value("identity")
    path = value("path")
    artifact_id = value("artifact_id") or value("record_id")
    if identity is not None:
        if isinstance(identity, (list, tuple)):
            key = ("identity", tuple(identity))
        else:
            key = ("identity", identity)
    elif artifact_id is not None:
        key = ("artifact", str(artifact_id))
    elif path is not None:
        key = ("path", str(path))
    else:
        return 0
    if claims_seen_ref is not None:
        claims_seen_ref[0] = True
    if key in seen:
        return 0
    seen.add(key)
    return size


def _physical_claims_complete(owners: Mapping[str, object]) -> bool:
    """Whether every eligible projection has an auditable physical key.

    Logical owner counters remain useful when this is false, but they cannot
    be replaced by a partial physical sum: doing so would silently undercount
    a real candidate merely because one owner omitted identity evidence.
    """

    for name in HYGIENE_COMPONENTS:
        owner = owners.get(name)
        if owner is None:
            continue
        payload = _owner_payload(owner)
        for item in _selection_values(owner, payload):
            source = _selection_source(item)
            nested = source.get("items")
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
                values: Sequence[object] = tuple(nested)
            else:
                values = (item,)
            for candidate in values:
                def value(key: str, current: object = candidate) -> object:
                    if isinstance(current, Mapping):
                        return current.get(key)
                    return getattr(current, key, None)

                classification = value("classification") or value("status")
                if value("eligible") is not True and classification != "eligible":
                    continue
                size = value("size_bytes")
                if not isinstance(size, int) or size < 0:
                    size = value("payload_size_bytes")
                identity = value("path_identity") or value("identity")
                path = value("path")
                artifact_id = value("artifact_id") or value("record_id")
                if (
                    not isinstance(size, int)
                    or size < 0
                    or (identity is None and path is None and artifact_id is None)
                ):
                    return False
    return True


@dataclass(frozen=True, slots=True)
class HygienePlan:
    """Federated, preview-only hygiene result.

    ``component_plans`` and ``component_fingerprints`` are private evidence
    retained for :meth:`HygieneManager.verify`; ``to_dict`` never serializes
    arbitrary owner objects.  All public effect counters are hard-coded by
    construction to describe a diagnostic with no mutation capability.
    """

    schema: str = HYGIENE_SCHEMA
    status: str = "planned"
    read_only: bool = True
    effects_enabled: bool = False
    deletion_performed: int = 0
    coverage: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    protected: int = 0
    eligible: int = 0
    blocked: int = 0
    unknown: int = 0
    unmanaged: tuple[Path, ...] = ()
    limits: Mapping[str, int] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    observed: int = 0
    observed_bytes: int = 0
    eligible_bytes: int = 0
    protected_bytes: int = 0
    logical_eligible_bytes: int = 0
    blocked_bytes: int = 0
    preview_only: bool = True
    mode: str = "plan"
    actions_ready: bool = False
    physical_effect_applied: bool = False
    mutation_authorized: bool = False
    applied: int = 0
    file_actions: int = 0
    verification: str = "unverified"
    fingerprint: str = ""
    # ``selection_complete`` is distinct from JSON detail completeness.  A
    # plan with a short presentation page is fine; a plan whose owner claims
    # were not fully observed is not eligible for verification.
    json_max_bytes: int = MAX_HYGIENE_JSON_BYTES
    selection_complete: bool = False
    selection_claims: int = 0
    component_plans: Mapping[str, object] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    component_fingerprints: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @property
    def registry(self) -> Mapping[str, object]:
        """Compatibility alias for the artifact-registry component evidence."""

        return self.coverage.get("artifact_registry", {})

    @property
    def counts(self) -> dict[str, int]:
        return {
            "observed": self.observed,
            "protected": self.protected,
            "eligible": self.eligible,
            "blocked": self.blocked,
            "unknown": self.unknown,
            "unmanaged": len(self.unmanaged),
            "deletion_performed": 0,
            "file_actions": 0,
        }

    @property
    def bytes(self) -> dict[str, int]:
        return {
            "observed": self.observed_bytes,
            "protected": self.protected_bytes,
            "eligible": self.eligible_bytes,
            "logical_eligible": self.logical_eligible_bytes,
            "blocked": self.blocked_bytes,
            "deletion_performed": 0,
        }

    @property
    def deletions(self) -> int:
        return 0

    def to_dict(self) -> dict[str, object]:
        """Return a bounded JSON-ready representation."""

        coverage = {
            _bounded_text(name, limit=64): dict(value)
            for name, value in tuple(self.coverage.items())[:MAX_HYGIENE_COMPONENTS]
        }
        payload: dict[str, object] = {
            "schema": self.schema,
            "kind": "hygiene",
            "operation": "hygiene",
            "mode": self.mode,
            "status": self.status,
            "verification": self.verification,
            "read_only": True,
            "effects_enabled": False,
            "preview_only": True,
            "deletion_performed": 0,
            "actions_ready": False,
            "physical_effect_applied": False,
            "mutation_authorized": False,
            "applied": 0,
            "file_actions": 0,
            "coverage": coverage,
            "selection": {
                "complete": self.selection_complete,
                "claims": self.selection_claims,
            },
            "counts": self.counts,
            "protected": self.protected,
            "eligible": self.eligible,
            "blocked": self.blocked,
            "unknown": self.unknown,
            "observed": self.observed,
            "unmanaged": [str(path) for path in self.unmanaged[:MAX_HYGIENE_ITEMS]],
            "limits": dict(self.limits),
            "reasons": list(self.reasons[:MAX_HYGIENE_REASONS]),
            "bytes": self.bytes,
            "fingerprint": self.fingerprint,
        }
        # ``coverage`` is assembled by this module and is already bounded;
        # keep a final size guard for unusually long caller-supplied paths or
        # strings in a manually constructed HygienePlan.
        if len(_canonical_json(payload).encode("utf-8")) > self.json_max_bytes:
            payload["unmanaged"] = [str(path)[:256] for path in self.unmanaged[:8]]
            payload["reasons"] = [str(reason)[:128] for reason in self.reasons[:8]]
            compact_coverage: dict[str, object] = {}
            for name, value in coverage.items():
                compact_coverage[name] = {
                    key: value[key]
                    for key in (
                        "status",
                        "complete",
                        "observed",
                        "protected",
                        "eligible",
                        "blocked",
                        "unknown",
                    )
                    if key in value
                }
            payload["coverage"] = compact_coverage
        # A caller may explicitly request a very small bound.  Preserve the
        # required safety flags and counters even then, dropping explanatory
        # detail before returning the final bounded envelope.
        if len(_canonical_json(payload).encode("utf-8")) > self.json_max_bytes:
            payload = {
                "schema": self.schema,
                "kind": "hygiene",
                "operation": "hygiene",
                "mode": self.mode,
                "status": self.status,
                "verification": self.verification,
                "read_only": True,
                "effects_enabled": False,
                "preview_only": True,
                "deletion_performed": 0,
                "actions_ready": False,
                "physical_effect_applied": False,
                "mutation_authorized": False,
                "applied": 0,
                "file_actions": 0,
                "selection": {
                    "complete": self.selection_complete,
                    "claims": self.selection_claims,
                },
                "counts": self.counts,
                "limits": {"max_json_bytes": self.json_max_bytes},
                "reasons": ["hygiene JSON detail truncated"],
            }
        return payload

    as_dict = to_dict

    def to_json(self) -> str:
        """Serialize the plan with stable keys and no owner-controlled dump."""

        return _canonical_json(self.to_dict())

    @property
    def sources(self) -> Mapping[str, Mapping[str, object]]:
        """Compatibility alias for the per-owner coverage map."""

        return self.coverage


class HygieneManager:
    """Federate explicit owner planners without owning any cleanup effect."""

    def __init__(
        self,
        artifact_root: Path | str | None = None,
        owned_temp_root: Path | str | None = None,
        audit_work_root: Path | str | None = None,
        state_directory: Path | str | None = None,
        *,
        artifact_registry: object | None = None,
        registry: object | None = None,
        registry_root: Path | str | None = None,
        roots: Sequence[Path | str | None] | None = None,
        hygiene_roots: Sequence[Path | str | None] | None = None,
        scratch_roots: Mapping[str, Path | str | None] | None = None,
        scratch_scopes: Mapping[str, Path | str | None] | None = None,
        scratch_managers: Mapping[str, object] | None = None,
        retention: object | None = None,
        retention_planner: Callable[..., object] | object | None = None,
        retention_state_directory: Path | str | None = None,
        preview: bool = False,
        owner: str = "neocortex",
        max_items: int = MAX_HYGIENE_ITEMS,
        max_reasons: int = MAX_HYGIENE_REASONS,
        max_bytes: int = MAX_HYGIENE_JSON_BYTES,
        max_entries: int | None = None,
        max_depth: int | None = None,
        scan_max_entries: int | None = None,
        scan_max_depth: int | None = None,
        scan_max_bytes: int | None = None,
        now_ns: int | None = None,
    ) -> None:
        # The aliases keep the public surface usable by callers that name the
        # owner rather than its root.  They are resolved once and never infer
        # a root from environment, HOME, or NeoCortex configuration.
        if artifact_root is None:
            artifact_root = registry_root
        if roots is None:
            roots = hygiene_roots
        if roots is not None:
            selected_roots = tuple(roots)
            if len(selected_roots) > len(HYGIENE_COMPONENTS):
                raise ValueError("hygiene roots cannot exceed four owner scopes")
            if artifact_root is None and selected_roots:
                artifact_root = selected_roots[0]
            if owned_temp_root is None and len(selected_roots) > 1:
                owned_temp_root = selected_roots[1]
            if audit_work_root is None and len(selected_roots) > 2:
                audit_work_root = selected_roots[2]
            if state_directory is None and len(selected_roots) > 3:
                state_directory = selected_roots[3]
        if artifact_registry is None:
            artifact_registry = registry
        if state_directory is None:
            state_directory = retention_state_directory
        if scratch_roots is None:
            scratch_roots = scratch_scopes
        if max_entries is not None and scan_max_entries is None:
            # ``max_entries`` is the traversal bound used by the CLI.  JSON
            # remains capped independently by max_items/max_bytes.
            scan_max_entries = max_entries
        if scan_max_depth is None:
            scan_max_depth = max_depth
        scope_roots = dict(scratch_roots or {})
        if owned_temp_root is not None:
            scope_roots["owned-temp"] = owned_temp_root
        if audit_work_root is not None:
            scope_roots["audit-work"] = audit_work_root
        invalid_scopes = sorted(set(scope_roots).difference(SCRATCH_SCOPES))
        if invalid_scopes:
            raise ValueError(f"unsupported hygiene scratch scope: {invalid_scopes[0]}")
        if not isinstance(owner, str) or not owner.strip() or "\x00" in owner:
            raise ValueError("hygiene owner must be a non-empty string")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10_000:
            raise ValueError("max_items must be between 1 and 10000")
        if isinstance(max_reasons, bool) or not isinstance(max_reasons, int) or not 1 <= max_reasons <= 10_000:
            raise ValueError("max_reasons must be between 1 and 10000")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1_024 <= max_bytes <= 1 << 20:
            raise ValueError("max_bytes must be between 1024 and 1048576")
        if max_depth is not None and (
            isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 0 <= max_depth <= 64
        ):
            raise ValueError("max_depth must be between 0 and 64")
        for name, value in (
            ("scan_max_entries", scan_max_entries),
            ("scan_max_depth", scan_max_depth),
            ("scan_max_bytes", scan_max_bytes),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if scan_max_entries is not None and scan_max_entries > 1_000_000:
            raise ValueError("scan_max_entries must be at most 1000000")
        if scan_max_depth is not None and scan_max_depth > 64:
            raise ValueError("scan_max_depth must be at most 64")
        if scan_max_bytes is not None and scan_max_bytes > 1 << 50:
            raise ValueError("scan_max_bytes must be at most 1125899906842624")
        if now_ns is not None and (isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0):
            raise ValueError("now_ns must be a non-negative integer or None")
        if type(preview) is not bool:
            raise TypeError("preview must be a boolean")

        self.artifact_root = None if artifact_root is None else Path(artifact_root)
        self.state_directory = None if state_directory is None else Path(state_directory)
        self.scratch_roots: dict[str, Path | str | None] = {
            scope: scope_roots.get(scope) for scope in SCRATCH_SCOPES
        }
        self._artifact_registry = artifact_registry
        self._scratch_managers = dict(scratch_managers or {})
        self._retention = retention
        self._retention_planner = retention_planner
        self.preview = preview
        self.owner = owner
        self.max_items = max_items
        self.max_reasons = max_reasons
        self.max_bytes = max_bytes
        self.max_depth = max_depth
        self.scan_max_entries = scan_max_entries
        self.scan_max_depth = scan_max_depth
        self.scan_max_bytes = scan_max_bytes
        self.now_ns = now_ns
        self._owner_errors: dict[str, str] = {}

    @staticmethod
    def _load_class(module_name: str, class_name: str) -> type[Any]:
        module = importlib.import_module(module_name)
        candidate = getattr(module, class_name)
        if not isinstance(candidate, type):
            raise TypeError(f"{module_name}.{class_name} is not a class")
        return candidate

    def _artifact_owner(self) -> object | None:
        if self._artifact_registry is not None:
            return self._artifact_registry
        if self.artifact_root is None:
            return None
        try:
            cls = self._load_class("neocortex.runtime.artifact_registry", "ArtifactRegistry")
            # The positional form is the producer contract.  The keyword
            # fallbacks keep this adapter compatible with embedders that use a
            # named root while retaining lazy import and no side effects.
            # A hygiene registry is a federated read view: producer scratch
            # owners share the canonical artifact root, so an exact
            # ``neocortex`` owner would incorrectly block their manifests.
            # The registry rejects writes from this None-owner view.
            try:
                owner = cls(self.artifact_root, owner=None)
            except TypeError:
                try:
                    owner = cls(root=self.artifact_root, owner=None)
                except TypeError:
                    try:
                        owner = cls(self.artifact_root)
                    except TypeError:
                        owner = cls(root=self.artifact_root)
            self._artifact_registry = owner
            return owner
        except Exception as exc:
            self._owner_errors["artifact_registry"] = (
                f"artifact registry owner unavailable: {type(exc).__name__}: {_bounded_text(exc)}"
            )
            return None

    def _scratch_owner(self, scope: str) -> object | None:
        if scope in self._scratch_managers:
            return self._scratch_managers[scope]
        root = self.scratch_roots.get(scope)
        if root is None:
            return None
        try:
            cls = self._load_class("neocortex.runtime.scratch", "ScratchManager")
            try:
                # A shared scope can contain workspaces from multiple
                # producer owners.  ``owner=None`` is the scratch owner's
                # read-only federated view; it cannot create/update/retire.
                manager = cls(Path(root), owner=None, create_root=False)
            except TypeError:
                manager = cls(Path(root), owner=None)
            self._scratch_managers[scope] = manager
            return manager
        except Exception as exc:
            self._owner_errors[scope] = (
                f"{scope} owner unavailable: {type(exc).__name__}: {_bounded_text(exc)}"
            )
            return None

    def _retention_owner(self, now_ns: int) -> object | None:
        if self._retention is not None:
            return self._retention
        if self.state_directory is None:
            return None
        if self._retention_planner is not None:
            if callable(self._retention_planner) and not callable(
                getattr(self._retention_planner, "plan", None)
            ):
                return _CallableRetentionPlanner(
                    self._retention_planner,
                    self.state_directory,
                    now_ns,
                )
            return self._retention_planner
        # Store the callable only after importing it.  No sqlite module is
        # imported here; the owner planner owns its read snapshot contract.
        try:
            module = importlib.import_module("neocortex.workflow.retention.planner")
            planner = module.plan_retention
            return _CallableRetentionPlanner(planner, self.state_directory, now_ns)
        except Exception as exc:
            self._owner_errors["retention"] = (
                f"retention owner unavailable: {type(exc).__name__}: {_bounded_text(exc)}"
            )
            return None

    @staticmethod
    def _call_plan(
        owner: object,
        *,
        now_ns: int,
        scan_max_entries: int | None = None,
        scan_max_depth: int | None = None,
        scan_max_bytes: int | None = None,
    ) -> object:
        if isinstance(owner, Mapping):
            return owner
        plan = getattr(owner, "plan", None)
        if not callable(plan):
            if callable(owner):
                return owner()
            raise TypeError("hygiene owner does not expose plan()")
        kwargs: dict[str, object] = {"now_ns": now_ns}
        try:
            signature = inspect.signature(plan)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            parameter_names = set(signature.parameters)
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if scan_max_entries is not None and (
                "max_entries" in parameter_names or accepts_kwargs
            ):
                kwargs["max_entries"] = scan_max_entries
            if scan_max_depth is not None and (
                "max_depth" in parameter_names or accepts_kwargs
            ):
                kwargs["max_depth"] = scan_max_depth
            if scan_max_bytes is not None and (
                "max_bytes" in parameter_names or accepts_kwargs
            ):
                kwargs["max_bytes"] = scan_max_bytes
            if "now_ns" not in parameter_names and not accepts_kwargs:
                kwargs.pop("now_ns", None)
        try:
            # Canonical ArtifactRegistry and ScratchManager accept the
            # optional now_ns fence.  Passing it first keeps a preview
            # deterministic; strict zero-argument embedders receive plan().
            return plan(**kwargs)
        except TypeError as first_error:
            try:
                return plan()
            except TypeError:
                raise first_error from None

    def _collect(self, *, now_ns: int) -> HygienePlan:
        owners: dict[str, object] = {}
        summaries: dict[str, _ComponentSummary] = {}

        artifact = self._artifact_owner()
        if artifact is None:
            summaries["artifact_registry"] = _summary_for_owner(
                "artifact_registry",
                None,
                fallback_reason=self._owner_errors.get(
                    "artifact_registry",
                    "artifact registry root omitted"
                    if self.artifact_root is None
                    else "artifact registry owner unavailable",
                ),
            )
        else:
            try:
                artifact_plan = self._call_plan(
                    artifact,
                    now_ns=now_ns,
                    scan_max_entries=self.scan_max_entries,
                    scan_max_depth=self.scan_max_depth,
                    scan_max_bytes=self.scan_max_bytes,
                )
            except Exception as exc:
                artifact_plan = None
                summaries["artifact_registry"] = _summary_for_owner(
                    "artifact_registry",
                    None,
                    fallback_reason=(
                        f"artifact registry plan deferred: {type(exc).__name__}: "
                        f"{_bounded_text(exc)}"
                    ),
                )
            else:
                owners["artifact_registry"] = artifact_plan
                summaries["artifact_registry"] = _summary_for_owner("artifact_registry", artifact_plan)

        for scope in SCRATCH_SCOPES:
            manager = self._scratch_owner(scope)
            if manager is None:
                summaries[scope] = _summary_for_owner(
                    scope,
                    None,
                    fallback_reason=self._owner_errors.get(
                        scope,
                        f"scratch scope {scope} root omitted"
                        if self.scratch_roots.get(scope) is None
                        else f"scratch scope {scope} owner unavailable",
                    ),
                )
                continue
            try:
                scratch_plan = self._call_plan(
                    manager,
                    now_ns=now_ns,
                    scan_max_entries=self.scan_max_entries,
                    scan_max_depth=self.scan_max_depth,
                    scan_max_bytes=self.scan_max_bytes,
                )
            except Exception as exc:
                summaries[scope] = _summary_for_owner(
                    scope,
                    None,
                    fallback_reason=(
                        f"scratch scope {scope} plan deferred: {type(exc).__name__}: "
                        f"{_bounded_text(exc)}"
                    ),
                )
            else:
                owners[scope] = scratch_plan
                summaries[scope] = _summary_for_owner(scope, scratch_plan)

        retention_owner = self._retention_owner(now_ns)
        if retention_owner is None:
            summaries["retention"] = _summary_for_owner(
                    "retention",
                    None,
                    fallback_reason=self._owner_errors.get(
                        "retention",
                        "retention state root omitted"
                        if self.state_directory is None and self._retention is None
                        else "retention owner unavailable",
                    ),
            )
        else:
            try:
                retention_plan = self._call_plan(
                    retention_owner,
                    now_ns=now_ns,
                    scan_max_entries=self.scan_max_entries,
                    scan_max_depth=self.scan_max_depth,
                    scan_max_bytes=self.scan_max_bytes,
                )
            except Exception as exc:
                summaries["retention"] = _summary_for_owner(
                    "retention",
                    None,
                    fallback_reason=(
                        f"retention owner deferred: {type(exc).__name__}: "
                        f"{_bounded_text(exc)}"
                    ),
                )
            else:
                owners["retention"] = retention_plan
                summaries["retention"] = _summary_for_owner("retention", retention_plan, retention=True)

        coverage = {name: _source_payload(summaries[name]) for name in HYGIENE_COMPONENTS}
        protected = sum(item.protected for item in summaries.values())
        eligible = sum(item.eligible for item in summaries.values())
        blocked = sum(item.blocked for item in summaries.values())
        unknown = sum(item.unknown for item in summaries.values())
        observed = sum(item.observed for item in summaries.values())
        selection_claims = sum(item.selection_claims for item in summaries.values())
        selection_complete = all(item.selection_complete for item in summaries.values())
        logical_eligible_bytes = sum(item.eligible_bytes for item in summaries.values())
        unique_eligible_bytes, has_physical_claims = _unique_eligible_bytes(owners)
        physical_complete = _physical_claims_complete(owners)
        eligible_bytes = (
            unique_eligible_bytes
            if has_physical_claims and physical_complete
            else logical_eligible_bytes
        )
        unmanaged: list[Path] = []
        reasons: list[str] = []
        limits: dict[str, int] = {
            "max_components": MAX_HYGIENE_COMPONENTS,
            "max_items": self.max_items,
            "max_reasons": self.max_reasons,
            "max_json_bytes": self.max_bytes,
        }
        if self.max_depth is not None:
            limits["max_depth"] = self.max_depth
        if self.scan_max_entries is not None:
            limits["scan_max_entries"] = self.scan_max_entries
        if self.scan_max_depth is not None:
            limits["scan_max_depth"] = self.scan_max_depth
        if self.scan_max_bytes is not None:
            limits["scan_max_bytes"] = self.scan_max_bytes
        for name in HYGIENE_COMPONENTS:
            item = summaries[name]
            prefix = f"{name}:"
            for path in item.unmanaged:
                if len(unmanaged) >= self.max_items:
                    break
                if path not in unmanaged:
                    unmanaged.append(path)
            for reason in item.reasons:
                if len(reasons) >= self.max_reasons:
                    break
                text = _bounded_text(f"{prefix} {reason}")
                if text not in reasons:
                    reasons.append(text)
            for key, value in item.limits.items():
                if len(limits) >= 32:
                    break
                limits.setdefault(f"{name}.{key}", _int(value))

        if logical_eligible_bytes and not physical_complete:
            if len(reasons) < self.max_reasons:
                reasons.append("physical accounting incomplete; logical bytes retained")

        status = "blocked" if blocked else "unknown" if unknown else "planned"
        if not any(item.complete for item in summaries.values()) and not blocked and not unknown:
            status = "deferred"
        component_fingerprints = {name: summaries[name].fingerprint for name in HYGIENE_COMPONENTS}
        fingerprint = _stable_digest(
            {
                "schema": HYGIENE_SCHEMA,
                "components": component_fingerprints,
                "counts": {
                    "observed": observed,
                    "protected": protected,
                    "eligible": eligible,
                    "blocked": blocked,
                    "unknown": unknown,
                },
                "unmanaged": [str(path) for path in unmanaged],
            }
        )
        return HygienePlan(
            schema=HYGIENE_SCHEMA,
            status=status,
            read_only=True,
            effects_enabled=False,
            deletion_performed=0,
            coverage=coverage,
            protected=protected,
            eligible=eligible,
            blocked=blocked,
            unknown=unknown,
            unmanaged=tuple(unmanaged),
            limits=limits,
            reasons=tuple(reasons),
            observed=observed,
            observed_bytes=sum(item.observed_bytes for item in summaries.values()),
            eligible_bytes=eligible_bytes,
            protected_bytes=sum(item.protected_bytes for item in summaries.values()),
            blocked_bytes=sum(item.blocked_bytes for item in summaries.values()),
            logical_eligible_bytes=logical_eligible_bytes,
            preview_only=True,
            mode="preview" if self.preview else "plan",
            actions_ready=False,
            physical_effect_applied=False,
            mutation_authorized=False,
            applied=0,
            file_actions=0,
            verification="unverified",
            fingerprint=fingerprint,
            selection_complete=selection_complete,
            selection_claims=selection_claims,
            json_max_bytes=self.max_bytes,
            component_plans=owners,
            component_fingerprints=component_fingerprints,
        )

    def plan(self, *, now_ns: int | None = None) -> HygienePlan:
        """Collect one bounded preview from every explicitly selected owner."""

        now = self.now_ns if now_ns is None else now_ns
        if now is None:
            now = time.time_ns()
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("hygiene now_ns must be a non-negative integer")
        return self._collect(now_ns=now)

    def verify(self, plan: HygienePlan) -> HygienePlan:
        """Re-plan and degrade on any owner or filesystem drift.

        The artifact registry's own ``verify`` method is called when present;
        scratch and retention are re-read through their owners' plan methods.
        A verification result is still a read-only plan, and a failed check
        turns every current eligibility proposal into a blocked diagnostic.
        """

        if not isinstance(plan, HygienePlan):
            raise TypeError("hygiene verify requires a HygienePlan")
        now = self.now_ns if self.now_ns is not None else time.time_ns()
        current = self._collect(now_ns=now)
        drift_reasons: list[str] = []
        artifact = self._artifact_registry
        previous_artifact_plan = plan.component_plans.get("artifact_registry")
        if artifact is not None and previous_artifact_plan is not None:
            verifier = getattr(artifact, "verify", None)
            if callable(verifier):
                try:
                    # The canonical registry verifies all registered records
                    # with no target.  A small embedding may expose the
                    # equally valid ``verify(plan)`` shape; retain that
                    # fallback for adapters and tests.
                    try:
                        result = verifier()
                    except TypeError:
                        result = verifier(previous_artifact_plan)
                    if result is False:
                        drift_reasons.append(
                            "artifact_registry: owner verification rejected the plan"
                        )
                    elif isinstance(result, Mapping) and (
                        result.get("drift") is True
                        or result.get("valid") is False
                        or result.get("ok") is False
                    ):
                        drift_reasons.append(
                            "artifact_registry: owner verification detected drift"
                        )
                    else:
                        verified_values = (
                            tuple(result)
                            if isinstance(result, Sequence)
                            and not isinstance(result, (str, bytes, bytearray))
                            else ()
                        )
                        if any(
                            getattr(item, "verified", True) is False
                            or getattr(item, "valid", True) is False
                            for item in verified_values
                        ):
                            drift_reasons.append(
                                "artifact_registry: owner verification detected drift"
                            )
                        valid = getattr(result, "valid", None)
                        if valid is False:
                            drift_reasons.append(
                                "artifact_registry: owner verification detected drift"
                            )
                except Exception as exc:
                    drift_reasons.append(
                        f"artifact_registry: verification unavailable ({type(exc).__name__})"
                    )
        if plan.fingerprint and plan.fingerprint != current.fingerprint:
            drift_reasons.append("hygiene plan drift detected")
        if not plan.selection_complete or not current.selection_complete:
            drift_reasons.append("hygiene selection claims incomplete")
        if current.status in {"blocked", "unknown", "deferred"}:
            drift_reasons.append(
                f"hygiene owner observation is not complete: {current.status}"
            )
        if not drift_reasons:
            return replace(current, status="verified", verification="verified")

        # A fresh observation is retained as evidence, but no eligibility from
        # it may be carried forward once the prior plan changed.  The explicit
        # extra blocked row represents the verification boundary itself.
        reasons = tuple(dict.fromkeys((*current.reasons, *drift_reasons)))[: self.max_reasons]
        blocked = current.blocked + max(1, current.eligible)
        return replace(
            current,
            status="blocked",
            blocked=blocked,
            eligible=0,
            eligible_bytes=0,
            reasons=reasons,
            verification="drift",
        )


class _CallableRetentionPlanner:
    """Small lazy adapter for the owner function ``plan_retention``."""

    def __init__(self, function: Callable[..., object], root: Path, now_ns: int) -> None:
        self.function = function
        self.root = root
        self.now_ns = now_ns

    def plan(self) -> object:
        try:
            return self.function(self.root, now_ns=self.now_ns)
        except TypeError:
            return self.function(self.root)


def plan_hygiene(
    artifact_root: Path | str | None = None,
    owned_temp_root: Path | str | None = None,
    audit_work_root: Path | str | None = None,
    state_directory: Path | str | None = None,
    *,
    artifact_registry: object | None = None,
    registry: object | None = None,
    registry_root: Path | str | None = None,
    roots: Sequence[Path | str | None] | None = None,
    hygiene_roots: Sequence[Path | str | None] | None = None,
    scratch_roots: Mapping[str, Path | str | None] | None = None,
    scratch_scopes: Mapping[str, Path | str | None] | None = None,
    scratch_managers: Mapping[str, object] | None = None,
    retention: object | None = None,
    retention_planner: Callable[..., object] | object | None = None,
    retention_state_directory: Path | str | None = None,
    owner: str = "neocortex",
    max_items: int = MAX_HYGIENE_ITEMS,
    max_reasons: int = MAX_HYGIENE_REASONS,
    max_bytes: int = MAX_HYGIENE_JSON_BYTES,
    max_entries: int | None = None,
    max_depth: int | None = None,
    scan_max_entries: int | None = None,
    scan_max_depth: int | None = None,
    scan_max_bytes: int | None = None,
    now_ns: int | None = None,
    preview: bool = False,
) -> HygienePlan:
    """Convenience function for one bounded, read-only hygiene preview."""

    return HygieneManager(
        artifact_root,
        owned_temp_root,
        audit_work_root,
        state_directory,
        artifact_registry=artifact_registry,
        registry=registry,
        registry_root=registry_root,
        roots=roots,
        hygiene_roots=hygiene_roots,
        scratch_roots=scratch_roots,
        scratch_scopes=scratch_scopes,
        scratch_managers=scratch_managers,
        retention=retention,
        retention_planner=retention_planner,
        retention_state_directory=retention_state_directory,
        preview=preview,
        owner=owner,
        max_items=max_items,
        max_reasons=max_reasons,
        max_bytes=max_bytes,
        max_entries=max_entries,
        max_depth=max_depth,
        scan_max_entries=scan_max_entries,
        scan_max_depth=scan_max_depth,
        scan_max_bytes=scan_max_bytes,
        now_ns=now_ns,
    ).plan(now_ns=now_ns)


__all__ = [
    "HYGIENE_COMPONENTS",
    "HYGIENE_SCHEMA",
    "MAX_HYGIENE_JSON_BYTES",
    "SCRATCH_SCOPES",
    "HygieneManager",
    "HygienePlan",
    "plan_hygiene",
]
