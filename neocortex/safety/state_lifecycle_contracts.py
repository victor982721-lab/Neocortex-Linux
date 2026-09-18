"""Pure lifecycle contracts shared by persistence consumers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

TableLifecycleRole = Literal["authoritative", "derived", "operational", "schema_metadata"]

@dataclass(frozen=True, slots=True)
class TableLifecycleRule:
    table: str
    role: TableLifecycleRole
    reconstruction_source: str | None
    retention_policy: str
    dependency_selector: str
    durability_boundary: str
    reset_action: Literal["preserve", "owner-transform"]

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OwnerResetAssessment:
    owner_id: str
    policy_version: int
    schema_version: int | None
    operational_rows: int
    authoritative_tables: tuple[str, ...]
    unknown_tables: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    identity_floor: int
    barrier_present: bool
    authoritative_digest: str
    table_counts: tuple[tuple[str, int], ...]
    observation_complete: bool = True

    @property
    def needs_transform(self) -> bool:
        return self.operational_rows > 0 or not self.barrier_present

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OwnerResetVerification:
    owner_id: str
    selected_effects_verified: bool
    operationally_fresh: bool
    authoritative_rows_preserved: bool
    references_valid: bool
    identity_floor: int
    residuals: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return asdict(self)
