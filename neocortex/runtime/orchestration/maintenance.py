"""Execute exact plans through their existing owners and retain their receipts.

Hygiene remains a neutral preview. This coordinator carries explicit scope
authority separately from plan fingerprints and never resets a database.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any, Protocol

MAINTENANCE_SCHEMA = "neocortex.maintenance/v1"
MAINTENANCE_SCOPES = frozenset({"owned-temp", "audit-work", "archive-materialized", "terminal-retention", "historical-temp"})


class MaintenanceBlocked(RuntimeError):
    """An owner or bounded request cannot prove the next effect safe."""


@dataclass(frozen=True, slots=True)
class ScopeAuthority:
    scope: str
    authority_ref: str

    def __post_init__(self) -> None:
        if self.scope not in MAINTENANCE_SCOPES or not self.authority_ref or len(self.authority_ref) > 1024:
            raise ValueError("maintenance authority must name one configured scope and its source")


@dataclass(frozen=True, slots=True)
class MaintenanceRequest:
    scopes: tuple[str, ...] = ("owned-temp",)
    selected_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    apply_requested: bool = False
    authorities: tuple[ScopeAuthority, ...] = ()
    deadline_ns: int | None = None
    max_entries: int = 100_000
    max_bytes: int = 1 << 40
    max_depth: int = 64
    cancelled: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(set(self.scopes)) != len(self.scopes) or any(s not in MAINTENANCE_SCOPES for s in self.scopes):
            raise ValueError("maintenance requires a unique selection of supported scopes")
        if set(self.selected_ids) - set(self.scopes):
            raise ValueError("selected ids exceed requested maintenance scopes")
        for value in (self.max_entries, self.max_bytes, self.max_depth):
            if type(value) is not int or value < 1:
                raise ValueError("maintenance budgets must be positive integers")
        if self.max_entries > 100_000 or self.max_bytes > 1 << 40 or self.max_depth > 64:
            raise ValueError("maintenance request exceeds hard budget")


class MaintenanceBudget:
    """One cooperative account shared by planning and owner execution."""

    def __init__(self, request: MaintenanceRequest) -> None:
        self.request = request
        self.entries = self.bytes = 0

    @property
    def remaining_entries(self) -> int:
        return max(0, self.request.max_entries - self.entries)

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.request.max_bytes - self.bytes)

    def checkpoint(self) -> None:
        if self.request.cancelled is not None and self.request.cancelled():
            raise MaintenanceBlocked("cancelled")
        if self.request.deadline_ns is not None and time.monotonic_ns() >= self.request.deadline_ns:
            raise MaintenanceBlocked("deadline_exceeded")
        if self.remaining_entries == 0 or self.remaining_bytes == 0:
            raise MaintenanceBlocked("maintenance_budget_exhausted")

    def consume(self, *, entries: int, apparent_bytes: int) -> None:
        self.entries += max(0, entries)
        self.bytes += max(0, apparent_bytes)
        if self.entries > self.request.max_entries or self.bytes > self.request.max_bytes:
            raise MaintenanceBlocked("maintenance_budget_exhausted")


class MaintenanceOwner(Protocol):
    def plan(self, selection: Sequence[str], budget: MaintenanceBudget) -> object: ...
    def verify(self, plan: object, budget: MaintenanceBudget) -> None: ...
    def apply(self, plan: object, budget: MaintenanceBudget) -> Mapping[str, Any]: ...
    def reconcile(self, receipt_ref: str, budget: MaintenanceBudget) -> Mapping[str, Any]: ...
    def claims(self, plan: object) -> Sequence[Mapping[str, Any]]: ...


def _fingerprint(plan: object) -> str:
    if hasattr(plan, "fingerprint") and plan.fingerprint:
        return str(plan.fingerprint)
    if hasattr(plan, "to_dict"):
        payload = plan.to_dict()
    elif isinstance(plan, Mapping):
        payload = dict(plan)
    elif is_dataclass(plan) and not isinstance(plan, type):
        payload = asdict(plan)
    else:
        raise MaintenanceBlocked("owner_plan_has_no_public_contract")
    digest = hashlib.sha256()
    for chunk in json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).iterencode(payload):
        digest.update(chunk.encode("ascii"))
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True, slots=True)
class MaintenancePlan:
    request: MaintenanceRequest
    component_plans: Mapping[str, object]
    component_fingerprints: Mapping[str, str]
    component_claims: Mapping[str, tuple[Mapping[str, Any], ...]]
    blocked: Mapping[str, str]
    order: tuple[str, ...]
    observed_entries: int
    observed_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {"schema": MAINTENANCE_SCHEMA, "mode": "plan", "effects_enabled": False,
                "requested_scopes": list(self.request.scopes), "order": list(self.order),
                "component_fingerprints": dict(self.component_fingerprints),
                "blocked_scopes": dict(self.blocked), "observed_entries": self.observed_entries,
                "observed_bytes": self.observed_bytes, "exclusive_reclaimable_bytes": None,
                "complete_coverage": not self.blocked}


class MaintenanceCoordinator:
    def __init__(self, owners: Mapping[str, MaintenanceOwner], *,
                 record_outcome: Callable[[Mapping[str, Any]], None] | None = None) -> None:
        if set(owners) - MAINTENANCE_SCOPES:
            raise ValueError("maintenance owner table contains unsupported scopes")
        self.owners = dict(owners)
        self.record_outcome = record_outcome

    def plan(self, request: MaintenanceRequest, *, hygiene_plan: Any = None) -> MaintenancePlan:
        budget = MaintenanceBudget(request)
        plans: dict[str, object] = {}
        fingerprints: dict[str, str] = {}
        claims: dict[str, tuple[Mapping[str, Any], ...]] = {}
        blocked: dict[str, str] = {}
        existing = getattr(hygiene_plan, "component_plans", {})
        for scope in request.scopes:
            owner = self.owners.get(scope)
            if owner is None:
                blocked[scope] = "owner_unavailable"
                continue
            try:
                budget.checkpoint()
                if scope in existing and not request.selected_ids.get(scope):
                    plan = existing[scope]
                    owner.verify(plan, budget)
                else:
                    plan = owner.plan(request.selected_ids.get(scope, ()), budget)
                component_claims = tuple(owner.claims(plan))
                if len(component_claims) > request.max_entries:
                    raise MaintenanceBlocked("owner_claim_budget_exceeded")
                claims[scope] = component_claims
                plans[scope] = plan
                fingerprints[scope] = _fingerprint(plan)
            except Exception as exc:
                blocked[scope] = str(exc)[:512]
        # Claims are the owners' existing records, not a second inventory.
        by_identity: dict[tuple[int, ...], list[str]] = {}
        by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for scope, records in claims.items():
            for record in records:
                identity = record.get("identity")
                if identity is not None:
                    by_identity.setdefault(tuple(identity), []).append(scope)
                artifact_id = record.get("artifact_id")
                if isinstance(artifact_id, str):
                    if artifact_id in by_id and by_id[artifact_id][0] != scope:
                        blocked[scope] = blocked[by_id[artifact_id][0]] = "conflicting_owner_claim"
                    by_id[artifact_id] = scope, record
        for scopes in by_identity.values():
            if len(scopes) > 1:
                for scope in scopes:
                    blocked[scope] = "overlapping_physical_claims"
        edges: dict[str, set[str]] = {scope: set() for scope in plans}
        artifact_edges: dict[str, set[str]] = {key: set() for key in by_id}
        for artifact_id, (scope, record) in by_id.items():
            for dependency in record.get("dependencies", ()):
                if dependency not in by_id:
                    continue  # The owner validates external/live dependencies.
                dependency_scope = by_id[dependency][0]
                artifact_edges[artifact_id].add(dependency)
                if record.get("state") in {"active", "failed", "failed-retained", "recovery_required"}:
                    blocked[dependency_scope] = "active_or_recovery_dependency"
                if dependency_scope != scope:
                    edges[scope].add(dependency_scope)
        incoming = dict.fromkeys(artifact_edges, 0)
        for targets in artifact_edges.values():
            for target in targets:
                incoming[target] += 1
        ready_ids = [key for key, count in incoming.items() if count == 0]
        while ready_ids:
            key = ready_ids.pop()
            for target in artifact_edges[key]:
                incoming[target] -= 1
                if incoming[target] == 0:
                    ready_ids.append(target)
        for key, count in incoming.items():
            if count:
                blocked[by_id[key][0]] = "dependency_cycle"
        # Consumers retire before their inputs. A failed consumer's dependent
        # scope is withheld at execution as well as by owner revalidation.
        order: list[str] = []
        remaining = set(plans)
        while remaining:
            ready = sorted(scope for scope in remaining
                           if not any(scope in edges[source] for source in remaining))
            if not ready:
                for scope in remaining:
                    blocked[scope] = "dependency_cycle"
                order.extend(sorted(remaining))
                break
            order.extend(ready)
            remaining.difference_update(ready)
        return MaintenancePlan(request, plans, fingerprints, claims, blocked, tuple(order),
                               budget.entries, budget.bytes)

    def execute(self, plan: MaintenancePlan, *, primary_work_status: str = "complete") -> dict[str, Any]:
        request = plan.request
        if not request.apply_requested:
            status = "partial" if plan.blocked else "planned"
            return {**plan.to_dict(), "operation_status": status,
                    "primary_work_status": primary_work_status, "maintenance_status": status,
                    "completed_scopes": [], "receipt_refs": []}
        authorities = {a.scope: a.authority_ref for a in request.authorities}
        blocked = dict(plan.blocked)
        completed: list[str] = []
        receipts: list[str] = []
        outcomes: dict[str, Mapping[str, Any]] = {}
        budget = MaintenanceBudget(request)
        # Account for the original observation before spending on verification.
        try:
            budget.consume(entries=plan.observed_entries, apparent_bytes=plan.observed_bytes)
        except MaintenanceBlocked as exc:
            for scope in request.scopes:
                blocked.setdefault(scope, str(exc))
        for scope in plan.order:
            if scope in blocked:
                continue
            if scope not in authorities:
                blocked[scope] = "explicit_scope_authority_required"
                continue
            owner = self.owners.get(scope)
            if owner is None:
                blocked[scope] = "owner_unavailable"
                continue
            try:
                budget.checkpoint()
                original = plan.component_plans[scope]
                if _fingerprint(original) != plan.component_fingerprints[scope]:
                    raise MaintenanceBlocked("retained_owner_plan_changed")
                owner.verify(original, budget)
                if self.record_outcome is not None:
                    self.record_outcome({"schema": MAINTENANCE_SCHEMA, "scope": scope,
                                         "phase": "prepared", "authority_ref": authorities[scope],
                                         "budget_entries": budget.entries, "budget_bytes": budget.bytes,
                                         "fingerprint": plan.component_fingerprints[scope]})
                result = dict(owner.apply(original, budget))
                outcomes[scope] = result
                receipts.extend(str(r) for r in result.get("receipt_refs", ()))
                if result.get("status") != "complete" or not result.get("verified", False):
                    raise MaintenanceBlocked(str(result.get("reason", "owner_effect_not_verified")))
                if self.record_outcome is not None:
                    self.record_outcome({"schema": MAINTENANCE_SCHEMA, "scope": scope,
                                         "phase": "confirmed", "result": result})
                completed.append(scope)
            except Exception as exc:
                blocked[scope] = str(exc)[:512]
                # A selected input remains protected if a selected consumer
                # did not reach its owner-confirmed postcondition.
                ids = {r.get("artifact_id") for r in plan.component_claims.get(scope, ())}
                dependencies = {d for r in plan.component_claims.get(scope, ())
                                if r.get("artifact_id") in ids for d in r.get("dependencies", ())}
                for target, records in plan.component_claims.items():
                    if any(r.get("artifact_id") in dependencies for r in records):
                        blocked.setdefault(target, "consumer_maintenance_incomplete")
        complete = len(completed) == len(request.scopes) and not blocked
        return {"schema": MAINTENANCE_SCHEMA, "operation_status": "complete" if complete and primary_work_status == "complete" else "partial",
                "primary_work_status": primary_work_status,
                "maintenance_status": "complete" if complete else "partial",
                "requested_scopes": list(request.scopes), "completed_scopes": completed,
                "blocked_scopes": blocked, "deferred_scopes": [s for s in request.scopes if s not in completed and s not in blocked],
                "complete_coverage": complete, "receipt_refs": receipts, "scopes": outcomes,
                "exclusive_reclaimable_bytes": None, "concurrent_free_space_observation": True}


class ScratchMaintenanceOwner:
    """Adapter for registered scratch; all unlink authority stays in Scratch."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def plan(self, selection: Sequence[str], budget: MaintenanceBudget) -> object:
        budget.checkpoint()
        plan = self.manager.plan(max_entries=budget.remaining_entries,
                                 max_depth=budget.request.max_depth, max_bytes=budget.remaining_bytes)
        if plan.root_blocked == "scratch root is absent":
            return plan
        if not plan.complete:
            raise MaintenanceBlocked(plan.reason or plan.root_blocked or "scratch_observation_incomplete")
        eligible = {r.record_id for r in plan.records if r.eligible}
        if selection:
            if len(set(selection)) != len(selection) or not set(selection).issubset(eligible):
                raise MaintenanceBlocked("requested_ids_do_not_match_exact_owner_plan")
            excluded = tuple(r for r in plan.records if r.eligible and r.record_id not in selection)
            plan = replace(plan, records=tuple(replace(r, eligible=False) if r in excluded else r
                                               for r in plan.records),
                           planned=plan.planned - len(excluded),
                           planned_bytes=plan.planned_bytes - sum(r.size_bytes for r in excluded),
                           kept=plan.kept + len(excluded),
                           kept_bytes=plan.kept_bytes + sum(r.size_bytes for r in excluded))
        budget.consume(entries=max(len(plan.records), getattr(plan, "observed_members", 0)),
                       apparent_bytes=max(sum(r.size_bytes for r in plan.records), getattr(plan, "observed_bytes", 0)))
        return plan

    def claims(self, plan: Any) -> Sequence[Mapping[str, Any]]:
        return tuple({"artifact_id": r.artifact_id or f"scratch:{r.record_id}",
                      "identity": r.path_identity, "owner": r.owner,
                      "state": r.status, "dependencies": tuple(r.metadata.get("dependencies", ())),
                      "eligible": r.eligible} for r in plan.records)

    def _require_registered_targets(self, plan: Any, budget: MaintenanceBudget) -> None:
        targets = tuple(record for record in plan.records if record.eligible)
        if not targets:
            return
        registry = self.manager._artifact_registry_instance()
        if registry is None:
            raise MaintenanceBlocked("configured_registry_claim_required_before_retirement")
        for target in targets:
            budget.checkpoint()
            if target.artifact_id is None:
                raise MaintenanceBlocked("configured_registry_claim_required_before_retirement")
            claimed = registry.verify(target.artifact_id)
            if (not getattr(claimed, "verified", False) or claimed.owner != self.manager.owner
                    or claimed.path != target.path or claimed.path_identity != target.path_identity
                    or claimed.digest != target.manifest_digest or claimed.state != "completed"):
                raise MaintenanceBlocked("configured_registry_claim_does_not_match_owner_selection")

    def verify(self, plan: Any, budget: MaintenanceBudget) -> None:
        budget.checkpoint()
        if plan.root_blocked == "scratch root is absent":
            if self.manager.root.exists():
                raise MaintenanceBlocked("scratch_root_appeared_after_plan")
            return
        self._require_registered_targets(plan, budget)
        verified = self.manager.verify(plan)
        if (verified is False or getattr(verified, "status", None) in {"blocked", "changed", "recovery_required"}
                or any(getattr(r, "issue", None) == "selection_changed" for r in getattr(verified, "records", ()))):
            raise MaintenanceBlocked("scratch_plan_changed")
        budget.consume(entries=max(len(verified.records), getattr(verified, "observed_members", 0)),
                       apparent_bytes=max(sum(r.size_bytes for r in verified.records),
                                          getattr(verified, "observed_bytes", 0)))

    def apply(self, plan: Any, budget: MaintenanceBudget) -> Mapping[str, Any]:
        budget.checkpoint()
        if plan.root_blocked == "scratch root is absent":
            return {"status": "complete", "verified": True, "retired": 0, "receipt_refs": []}
        self._require_registered_targets(plan, budget)
        targets = tuple(r for r in plan.records if r.eligible)
        try:
            before = os.statvfs(self.manager.root)
            free_before = before.f_bavail * before.f_frsize
        except OSError:
            free_before = None
        result = self.manager.apply(plan=plan)
        try:
            after = os.statvfs(self.manager.root)
            free_after = after.f_bavail * after.f_frsize
        except OSError:
            free_after = None
        absent = all(not os.path.lexists(r.path) for r in targets)
        registry = self.manager._artifact_registry_instance()
        receipt_refs: list[str] = []
        if registry is not None:
            for r in targets:
                record = registry.verify(r.artifact_id or f"scratch:{r.record_id}")
                if getattr(record, "state", None) != "retired":
                    absent = False
                else:
                    receipt_refs.append(str(registry.manifest_path(record.artifact_id)))
        verified = absent and not result.recovery_required and not result.failed and not result.blocked
        return {"status": "complete" if verified else "partial", "verified": verified,
                "retired": result.applied, "protected": result.kept,
                "blocked": result.blocked, "deleted_apparent_bytes": result.applied_bytes,
                "exclusive_reclaimable_bytes": None, "receipt_refs": receipt_refs,
                "free_space_delta": None if free_before is None or free_after is None else free_after - free_before,
                "free_space_delta_is_concurrent_observation": True}

    def reconcile(self, receipt_ref: str, budget: MaintenanceBudget) -> Mapping[str, Any]:
        budget.checkpoint()
        registry = self.manager._artifact_registry_instance()
        if registry is None:
            raise MaintenanceBlocked("registry_owner_unavailable")
        receipt = Path(receipt_ref)
        if receipt.parent != registry.root:
            raise MaintenanceBlocked("receipt_outside_owner_registry")
        return registry.recover_retirements()


def configured_scratch_maintenance(state_directory: Path, *, owner: str = "neocortex-framework",
                                   scopes: Sequence[str] = ("owned-temp",),
                                   record_outcome: Callable[[Mapping[str, Any]], None] | None = None) -> MaintenanceCoordinator:
    """Compose only explicitly configured state-local scratch owners."""
    from neocortex.runtime.scratch import ScratchManager
    if any(scope not in {"owned-temp", "audit-work"} for scope in scopes):
        raise ValueError("configured scratch maintenance accepts only state-local scratch scopes")
    owners = {scope: ScratchMaintenanceOwner(ScratchManager(Path(state_directory) / "scratch" / scope,
               owner=owner, create_root=False, artifact_registry_root=Path(state_directory) / "artifacts"))
              for scope in scopes}
    return MaintenanceCoordinator(owners, record_outcome=record_outcome)


@dataclass(frozen=True, slots=True)
class TerminalMaintenancePlan:
    records: tuple[Any, ...]
    retained_plans: Mapping[str, Any]
    selected_ids: tuple[str, ...]
    now_ns: int

    def to_dict(self) -> dict[str, object]:
        return {"schema": MAINTENANCE_SCHEMA, "scope": "terminal-retention", "now_ns": self.now_ns,
                "selected_ids": list(self.selected_ids),
                "records": [{"artifact_id": r.artifact_id, "manifest_digest": r.manifest_digest,
                             "state": r.state, "owner": r.owner} for r in self.records],
                "purpose_plans": {purpose: p.to_dict() for purpose, p in self.retained_plans.items()}}


class TerminalRetentionOwner:
    """Apply the existing terminal policy only to confirmed registry tombstones."""

    def __init__(self, registry: Any, *, policy: Any,
                 purpose_policies: Mapping[str, Any] | None = None) -> None:
        self.registry = registry
        self.policy = policy
        self.purpose_policies = dict(purpose_policies or {})

    def plan(self, selection: Sequence[str], budget: MaintenanceBudget) -> TerminalMaintenancePlan:
        return self._plan(selection, budget, now_ns=time.time_ns())

    def _plan(self, selection: Sequence[str], budget: MaintenanceBudget, *, now_ns: int) -> TerminalMaintenancePlan:
        from neocortex.workflow.retention.planner import TerminalRetentionRecord, plan_terminal_retention
        budget.checkpoint()
        source = self.registry.plan(max_records=budget.remaining_entries, max_bytes=budget.remaining_bytes)
        if source.truncated or source.unmanaged:
            raise MaintenanceBlocked("terminal_registry_observation_incomplete")
        records = tuple(r for r in source.records if r.owner == self.registry.owner)
        budget.consume(entries=max(len(records), sum(getattr(r, "observed_payload_entries", 0) for r in records)),
                       apparent_bytes=sum(r.size_bytes for r in records))
        groups: dict[str, list[Any]] = {}
        for record in records:
            metadata = record.metadata
            claim = metadata.get("neocortex_retirement", {})
            confirmed = isinstance(claim, Mapping) and claim.get("phase") == "confirmed"
            tombstone = record.state == "retired"
            groups.setdefault(record.purpose, []).append(TerminalRetentionRecord(
                record_id=record.artifact_id, status=record.state,
                category="tombstone" if tombstone else "other", terminal_ns=record.updated_ns,
                apparent_bytes=record.manifest_path.stat().st_size if record.manifest_path else 0,
                physical_identity=record.path_identity, reconciled=confirmed,
                recovery_required=record.state == "recovery_required",
                replay_required=metadata.get("replay_required") is True,
                pinned=metadata.get("pinned") is True or not tombstone,
                grant_active=metadata.get("grant_active") is True,
                authorization_active=metadata.get("authorization_active") is True,
                release_authorized=confirmed, evidence_required=metadata.get("evidence_required") is True,
                tombstone=tombstone, owner=record.owner))
        plans = {purpose: plan_terminal_retention(group, now_ns=now_ns,
                    policy=self.purpose_policies.get(purpose, self.policy)) for purpose, group in groups.items()}
        if any(p.status != "ready" or p.truncated for p in plans.values()):
            raise MaintenanceBlocked("terminal_policy_observation_incomplete")
        eligible = {item.record.record_id for p in plans.values() for item in p.eligible_items
                    if item.record.category == "tombstone"}
        if selection and (len(set(selection)) != len(selection) or not set(selection).issubset(eligible)):
            raise MaintenanceBlocked("terminal_selection_not_eligible")
        return TerminalMaintenancePlan(records, plans, tuple(sorted(selection or eligible)), now_ns)

    def claims(self, plan: TerminalMaintenancePlan) -> Sequence[Mapping[str, Any]]:
        # Tombstone retention acts on the receipt inode, not the retired path.
        return tuple({"artifact_id": "tombstone:" + r.artifact_id, "owner": r.owner,
                      "identity": None, "state": r.state, "dependencies": ()}
                     for r in plan.records if r.artifact_id in plan.selected_ids)

    def verify(self, plan: TerminalMaintenancePlan, budget: MaintenanceBudget) -> None:
        if not plan.selected_ids:
            budget.checkpoint()
            return
        current = self._plan(plan.selected_ids, budget, now_ns=plan.now_ns)
        selected = set(plan.selected_ids)
        prior_claims = {r.artifact_id: r.manifest_digest for r in plan.records if r.artifact_id in selected}
        current_claims = {r.artifact_id: r.manifest_digest for r in current.records if r.artifact_id in selected}
        if prior_claims != current_claims or current.selected_ids != plan.selected_ids:
            raise MaintenanceBlocked("terminal_plan_changed")

    def apply(self, plan: TerminalMaintenancePlan, budget: MaintenanceBudget) -> Mapping[str, Any]:
        budget.checkpoint()
        if not plan.selected_ids:
            return {"status": "complete", "verified": True, "retired": 0, "receipt_refs": []}
        operation_id = "maintenance-" + _fingerprint(plan).removeprefix("sha256:")[:48]
        receipt = self.registry.apply_tombstone_retention(plan.selected_ids,
                    release_authorized=True, evidence={"plan_fingerprint": _fingerprint(plan)},
                    operation_id=operation_id)
        verified = all(not self.registry.manifest_path(identifier).exists() for identifier in plan.selected_ids)
        return {"status": "complete" if verified else "partial", "verified": verified,
                "retired": len(plan.selected_ids) if verified else 0, "receipt": receipt,
                "receipt_refs": [str(self.registry.tombstone_retention_receipt_path(operation_id))]}

    def reconcile(self, receipt_ref: str, budget: MaintenanceBudget) -> Mapping[str, Any]:
        budget.checkpoint()
        if Path(receipt_ref).parent != self.registry.root / ".tombstone-retention":
            raise MaintenanceBlocked("receipt_outside_owner_registry")
        return self.registry.recover_tombstone_retention()


class HistoricalMaintenanceOwner:
    """Consume only an already prepared exact historical proposal."""

    def __init__(self, manager: Any, proposal_digest: str) -> None:
        self.manager = manager
        self.proposal_digest = proposal_digest

    def plan(self, selection: Sequence[str], budget: MaintenanceBudget) -> object:
        budget.checkpoint()
        plan = self.manager.adoption_plan(self.proposal_digest)
        eligible = {r["selected_id"] for r in plan.records if r["status"] == "eligible"}
        if selection and set(selection) != eligible:
            raise MaintenanceBlocked("historical_selection_must_match_approved_proposal")
        budget.consume(entries=sum(r.get("members", 0) for r in plan.records),
                       apparent_bytes=sum(r.get("apparent_bytes", 0) for r in plan.records))
        return plan

    def claims(self, plan: Any) -> Sequence[Mapping[str, Any]]:
        return tuple({"artifact_id": r["provenance_artifact_id"], "owner": plan.owner,
                      "identity": r.get("ancestors", [{}])[-1].get("identity"),
                      "state": "completed" if r["status"] == "eligible" else "recovery_required",
                      "dependencies": tuple(r.get("dependencies", ()))} for r in plan.records)

    def verify(self, plan: Any, budget: MaintenanceBudget) -> None:
        budget.checkpoint()
        if self.manager.adoption_plan(self.proposal_digest).digest != plan.digest:
            raise MaintenanceBlocked("historical_proposal_changed")

    def apply(self, plan: Any, budget: MaintenanceBudget) -> Mapping[str, Any]:
        result = self.manager.apply_selected(plan.digest, deadline_ns=budget.request.deadline_ns,
                                             cancelled=budget.request.cancelled)
        return {"status": result["operation_status"], "verified": result["coverage_complete"],
                "retired": sum(r["state"] == "retired" and not r.get("replayed") for r in result["records"]),
                "receipt_refs": [result["receipt_directory"]], "result": result}

    def reconcile(self, receipt_ref: str, budget: MaintenanceBudget) -> Mapping[str, Any]:
        if Path(receipt_ref) != self.manager.state_directory / "historical-adoptions":
            raise MaintenanceBlocked("receipt_outside_historical_owner")
        return self.manager.apply_selected(self.proposal_digest, deadline_ns=budget.request.deadline_ns,
                                            cancelled=budget.request.cancelled)


__all__ = [
    "HistoricalMaintenanceOwner",
    "MaintenanceBudget",
    "MaintenanceCoordinator",
    "MaintenanceOwner",
    "MaintenancePlan",
    "MaintenanceRequest",
    "ScopeAuthority",
    "ScratchMaintenanceOwner",
    "TerminalRetentionOwner",
    "configured_scratch_maintenance",
]
