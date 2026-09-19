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
