"""Identity-bound input selection for advisory organization plans.

An output directory is not an input scope.  Scope membership is demonstrated by
the physical resource, never by a virtual
locator or a detector's confidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neocortex.foundation.file_identity import FileIdentity, FileIdentityEncoding
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.safety.corpus_access import CorpusAccessPolicy

from .document_catalog import document_catalog_database
from .document_resource_binding import parse_resource_binding

SCOPE_SCHEMA = "neocortex.organization-input-scope/v1"
SCOPE_POLICY = "physical-anchor-containment/v1"


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _publication_heads(connection: sqlite3.Connection) -> tuple[tuple[str, int], ...]:
    rows = connection.execute(
        """SELECT p.source_kind,p.generation_id,g.status,g.source_kind FROM catalog_publications p
        LEFT JOIN catalog_generations g ON g.generation_id=p.generation_id
        ORDER BY p.source_kind"""
    ).fetchall()
    if any(str(row[2]) != "published" or row[3] != row[0] for row in rows):
        raise ValueError("organization_scope_catalog_head_not_published")
    return tuple((str(row[0]), int(row[1])) for row in rows)


@dataclass(frozen=True, slots=True)
class OrganizationInputScope:
    """One selected root identity and the exact published catalog heads."""

    root: Path
    root_identity: FileIdentity
    root_birthtime_ns: int
    publication_heads: tuple[tuple[str, int], ...]
    policy_version: str = SCOPE_POLICY

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("organization_scope_root_must_be_absolute")
        if str(self.root) != os.path.abspath(self.root):
            raise ValueError("organization_scope_root_must_be_canonical")
        if not isinstance(self.root_identity, FileIdentity):
            raise ValueError("organization_scope_root_identity_missing")
        if type(self.root_birthtime_ns) is not int or self.root_birthtime_ns < -1:
            raise ValueError("organization_scope_birthtime_invalid")
        if self.policy_version != SCOPE_POLICY:
            raise ValueError("organization_scope_policy_unsupported")
        if not isinstance(self.publication_heads, tuple):
            raise ValueError("organization_scope_heads_must_be_immutable")
        seen: set[str] = set()
        for kind, generation in self.publication_heads:
            if not isinstance(kind, str) or not kind or kind in seen:
                raise ValueError("organization_scope_heads_invalid")
            if type(generation) is not int or generation < 1:
                raise ValueError("organization_scope_generation_invalid")
            seen.add(kind)
        if self.publication_heads != tuple(sorted(self.publication_heads)):
            raise ValueError("organization_scope_heads_not_canonical")

    @classmethod
    def capture(
        cls, root: Path, *, publication_heads: tuple[tuple[str, int], ...]
    ) -> OrganizationInputScope:
        policy = CorpusAccessPolicy.capture("normal", root)
        if policy.root_device_id is None or policy.root_file_id is None:
            raise ValueError("organization_scope_root_identity_missing")
        if policy.root_birthtime_ns is None:
            raise ValueError("organization_scope_root_birthtime_missing")
        return cls(
            policy.root.resolve(strict=True),
            FileIdentity(policy.root_device_id, policy.root_file_id),
            policy.root_birthtime_ns,
            publication_heads,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCOPE_SCHEMA,
            "canonical_root": str(self.root),
            "root_identity": {
                "packed_key": self.root_identity.packed_key,
                "birthtime_ns": self.root_birthtime_ns,
            },
            "policy_version": self.policy_version,
            "publication_heads": [
                {"source_kind": kind, "generation_id": generation}
                for kind, generation in self.publication_heads
            ],
        }

    @property
    def serialized(self) -> str:
        return _canonical_json(self.to_dict())

    @property
    def scope_id(self) -> str:
        return "sha256:" + hashlib.sha256(self.serialized.encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, raw: object) -> OrganizationInputScope:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32_768:
            raise ValueError("organization_scope_missing_or_invalid")
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"schema", "canonical_root", "root_identity", "policy_version", "publication_heads"}
            or payload["schema"] != SCOPE_SCHEMA
        ):
            raise ValueError("organization_scope_schema_invalid")
        identity = payload["root_identity"]
        if not isinstance(identity, dict) or set(identity) != {"packed_key", "birthtime_ns"}:
            raise ValueError("organization_scope_identity_invalid")
        heads = payload["publication_heads"]
        if not isinstance(heads, list) or any(
            not isinstance(head, dict) or set(head) != {"source_kind", "generation_id"}
            for head in heads
        ):
            raise ValueError("organization_scope_heads_invalid")
        return cls(
            Path(payload["canonical_root"]),
            FileIdentity.decode(
                identity["packed_key"], encoding=FileIdentityEncoding.PACKED_HEX_V1
            ),
            identity["birthtime_ns"],
            tuple((head["source_kind"], head["generation_id"]) for head in heads),
            payload["policy_version"],
        )

    def verify(self, connection: sqlite3.Connection | None = None) -> None:
        observed = self.root.lstat()
        if not stat.S_ISDIR(observed.st_mode) or self.root.resolve(strict=True) != self.root:
            raise ValueError("organization_scope_root_changed")
        if (
            observed.st_dev != self.root_identity.volume_id
            or observed.st_ino != self.root_identity.file_id
            or stat_birthtime_ns(observed) != self.root_birthtime_ns
        ):
            raise ValueError("organization_scope_root_identity_changed")
        if connection is not None and _publication_heads(connection) != self.publication_heads:
            raise ValueError("organization_scope_catalog_heads_changed")


def capture_organization_input_scope(catalog_path: Path, root: Path) -> OrganizationInputScope:
    """Capture a selected runtime root without consulting latest-analysis history."""

    with document_catalog_database(catalog_path, readonly=True) as connection:
        scope = OrganizationInputScope.capture(
            root, publication_heads=_publication_heads(connection)
        )
        scope.verify(connection)
        return scope


@dataclass(frozen=True, slots=True)
class ScopedOrganizationResource:
    binding: Mapping[str, Any] | None
    included: bool
    reason: str | None = None


def assess_organization_resource(
    row: Mapping[str, Any] | sqlite3.Row, scope: OrganizationInputScope
) -> ScopedOrganizationResource:
    """Admit only a current, contained physical anchor from a typed owner binding."""

    raw = row["resource_binding_json"] if "resource_binding_json" in row.keys() else None
    if raw is None:
        return ScopedOrganizationResource(None, False, "resource_binding_missing")
    try:
        binding = parse_resource_binding(raw)
        if binding["source_kind"] != row["source_kind"] or binding["file_key"] != row["file_key"]:
            raise ValueError("resource_binding_owner_mismatch")
        source_path = row["path"] if "path" in row.keys() else row["source_path"]
        if binding["resource_ref"]["current_path"] != source_path:
            raise ValueError("resource_binding_locator_mismatch")
        anchor_value = binding["physical_anchor_path"]
        if anchor_value is None:
            return ScopedOrganizationResource(binding, False, "resource_anchor_unresolved")
        anchor = Path(anchor_value)
        if not anchor.is_relative_to(scope.root):
            return ScopedOrganizationResource(binding, False, "source_outside_scope")
        observed = anchor.lstat()
        if not stat.S_ISREG(observed.st_mode) or anchor.resolve(strict=True) != anchor:
            raise ValueError("resource_anchor_not_canonical_regular_file")
        physical = binding["physical_identity"]
        identity = FileIdentity.decode(
            physical["packed_key"], encoding=FileIdentityEncoding.PACKED_HEX_V1
        )
        revision = binding["physical_anchor_revision"]
        if (
            observed.st_dev != identity.volume_id
            or observed.st_ino != identity.file_id
            or stat_birthtime_ns(observed) != physical["birthtime_ns"]
            or observed.st_size != revision["size"]
            or observed.st_mtime_ns != revision["mtime_ns"]
        ):
            raise ValueError("resource_anchor_snapshot_changed")
        return ScopedOrganizationResource(binding, True)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return ScopedOrganizationResource(
            None, False, f"resource_scope_unverified:{type(exc).__name__}"
        )


__all__ = [
    "OrganizationInputScope",
    "assess_organization_resource",
    "capture_organization_input_scope",
]
