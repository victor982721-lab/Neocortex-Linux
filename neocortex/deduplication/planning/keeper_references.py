"""Bounded, optional keeper evidence from the published Code content graph.

Only confirmed, resolved relations from the understood legacy-bridge publication
are eligible. Owner-scoped keys, strings in source text and path similarity are
never treated as references or as authority to discard a file.
"""

from __future__ import annotations

import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from neocortex.code.code_graph_generations import (
    CodeGraphGenerationStore,
    GenerationError,
    GraphMembership,
    _hash as _graph_hash,
)
from neocortex.code.code_schema import CODE_SCHEMA_VERSION, code_schema_contract
from neocortex.foundation.file_identity import FileIdentity, FileIdentityEncoding
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadSession,
    SQLiteSnapshotBudget,
    SQLiteSnapshotBudgetExceeded,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
)
from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    read_metadata_schema_version,
    validate_sqlite_schema_contract,
)
from neocortex.platform.policy import stat_birthtime_ns

from ..domain.errors import InventoryError
from ..domain.evidence import KeeperPolicy
from ..domain.models import FileSnapshot
from ..fingerprinting import snapshot_path
from ..inventory.index import DedupIndex

_TIMEOUT_SECONDS = 10.0
_SNAPSHOT_BYTES = 64 * 1024 * 1024
_GRAPH_PAYLOAD_BYTES = 4 * 1024 * 1024
_REFERENCE_COLUMNS = (
    "reference_id",
    "version_id",
    "source_symbol_id",
    "target_symbol_id",
    "target_version_id",
    "kind",
    "name",
    "target_hint",
    "confirmed",
    "confidence",
    "evidence",
    "start_line",
    "start_column",
    "end_line",
    "end_column",
    "start_byte",
    "end_byte",
)
_DEPENDENCY_COLUMNS = (
    "dependency_id",
    "version_id",
    "resolved_version_id",
    "name",
    "kind",
    "scope",
    "version_spec",
    "confirmed",
    "confidence",
    "evidence",
    "start_line",
    "start_column",
    "end_line",
    "end_column",
    "start_byte",
    "end_byte",
)
_VERSION_COLUMNS = (
    "version_id",
    "file_id",
    "path_observed",
    "size",
    "mtime_ns",
    "birthtime_ns",
    "raw_xxh3_128",
    "text_xxh3_128",
    "normalized_xxh3_128",
    "token_xxh3_128",
    "structure_xxh3_128",
    "language",
    "artifact_kind",
    "analysis_status",
    "processing_signature",
    "analyzer_id",
    "analyzer_version",
    "parser_kind",
    "text_chars",
    "text_truncated",
    "provenance_json",
)


class KeeperReferenceChanged(InventoryError):
    """Previously used reference evidence changed before plan publication."""


class _StaleReference(ValueError):
    pass


class _ReferenceBudgetExceeded(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _EndpointResolution:
    """Resolved endpoint plus whether its owner-observed path is stale."""

    snapshot: FileSnapshot
    owner_path_stale: bool = False


@dataclass(frozen=True, slots=True)
class KeeperReferenceResolution:
    policy: KeeperPolicy
    status: str
    reason: str | None
    evidence_count: int
    _validation: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def verify(self) -> None:
        """Fence successful evidence again; an initially unavailable provider is optional."""
        if self._validation is not None:
            self._validation()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "verified_identity_count": len(self.policy.verified_reference_identities),
        }


def _unavailable(status: str, reason: str) -> KeeperReferenceResolution:
    return KeeperReferenceResolution(KeeperPolicy(), status, reason, 0)


def _scan_root(index: DedupIndex, scan_id: int) -> tuple[Path, tuple[object, ...]]:
    row = index._connection.execute(
        "SELECT root,root_volume_id,root_file_id,root_birthtime_ns,status,errors,completed_ns "
        "FROM scans WHERE scan_id=?",
        (scan_id,),
    ).fetchone()
    if row is None or row[4] != "complete" or row[5] != 0 or row[6] is None:
        raise _StaleReference("selected_inventory_is_not_complete")
    if (
        any(not isinstance(value, bytes) or len(value) != 16 for value in row[1:3])
        or type(row[3]) is not int
        or row[3] < -1
    ):
        raise _StaleReference("selected_inventory_root_identity_unavailable")
    identity = FileIdentity(int.from_bytes(row[1], "little"), int.from_bytes(row[2], "little"))
    root = Path(row[0])
    observed = root.lstat()
    if (
        not root.is_absolute()
        or root.resolve(strict=True) != root
        or not stat.S_ISDIR(observed.st_mode)
    ):
        raise _StaleReference("selected_inventory_root_not_canonical")
    if (observed.st_dev, observed.st_ino, stat_birthtime_ns(observed)) != (
        identity.volume_id,
        identity.file_id,
        row[3],
    ):
        raise _StaleReference("selected_inventory_root_changed")
    return root, tuple(row)


def _verify_snapshot(index: DedupIndex, scan_id: int, snapshot: FileSnapshot, root: Path) -> None:
    path = Path(snapshot.path)
    if not path.is_absolute() or not path.is_relative_to(root) or path.resolve(strict=True) != path:
        raise _StaleReference("reference_endpoint_outside_canonical_scope")
    if not stat.S_ISREG(path.lstat().st_mode) or snapshot_path(path) != snapshot:
        raise _StaleReference("reference_endpoint_snapshot_changed")
    row = index._connection.execute(
        "SELECT volume_id,file_id,size,mtime_ns,birthtime_ns FROM files WHERE scan_id=? AND path=?",
        (scan_id, snapshot.path),
    ).fetchone()
    expected = (
        snapshot.volume_id.to_bytes(16, "little"),
        snapshot.file_id.to_bytes(16, "little"),
        snapshot.size,
        snapshot.mtime_ns,
        snapshot.birthtime_ns,
    )
    if row is None or tuple(row) != expected:
        raise _StaleReference("reference_endpoint_inventory_changed")


def _matched_member(
    members: Mapping[str, GraphMembership],
    key: str,
    table: str,
    row: tuple[object, ...],
    version_id: int,
) -> None:
    member = members.get(key)
    if (
        member is None
        or member.metadata.get("table") != table
        or member.source_version_id != version_id
    ):
        raise _StaleReference("relation_is_not_bound_to_published_membership")
    digest = _graph_hash(
        {"table": table, "row": {str(i): value for i, value in enumerate(row)}}, "graph row"
    )
    if digest != member.item_digest:
        raise _StaleReference("published_relation_or_version_changed")


def _inventory_snapshot_for_identity(
    index: DedupIndex,
    scan_id: int,
    identity: FileIdentity,
    *,
    path: str | None = None,
) -> FileSnapshot | None:
    """Resolve an inventory path from physical identity, not a Code path."""

    volume_id = identity.volume_id.to_bytes(16, "little")
    file_id = identity.file_id.to_bytes(16, "little")
    if path is None:
        row = index._connection.execute(
            "SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns FROM files "
            "WHERE scan_id=? AND volume_id=? AND file_id=? "
            "ORDER BY path COLLATE BINARY LIMIT 1",
            (scan_id, volume_id, file_id),
        ).fetchone()
    else:
        row = index._connection.execute(
            "SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns FROM files "
            "WHERE scan_id=? AND path=?",
            (scan_id, path),
        ).fetchone()
    if row is None:
        return None
    if (
        not isinstance(row[0], str)
        or not isinstance(row[1], bytes)
        or not isinstance(row[2], bytes)
        or len(row[1]) != 16
        or len(row[2]) != 16
        or any(type(value) is not int for value in row[3:])
        or row[5] < -1
    ):
        raise _StaleReference("reference_endpoint_inventory_snapshot_invalid")
    observed_identity = FileIdentity(
        int.from_bytes(row[1], "little"), int.from_bytes(row[2], "little")
    )
    if path is None and observed_identity != identity:
        raise _StaleReference("reference_endpoint_inventory_identity_changed")
    return FileSnapshot(
        str(row[0]), observed_identity.volume_id, observed_identity.file_id,
        row[3], row[4], row[5],
    )


def _endpoint(
    connection: sqlite3.Connection,
    members: Mapping[str, GraphMembership],
    index: DedupIndex,
    scan_id: int,
    root: Path,
    version_id: int,
) -> _EndpointResolution | None:
    row = connection.execute(
        "SELECT f.volume_id,f.physical_file_id,f.current_path,f.current_version_id,f.status,v.invalidated_ns,"
        "v.size,v.mtime_ns,v.birthtime_ns FROM file_versions v JOIN files f ON f.file_id=v.file_id "
        "WHERE v.version_id=?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise _StaleReference("published_reference_endpoint_missing")
    if row[3] != version_id or row[4] != "current" or row[5] is not None:
        raise _StaleReference("reference_endpoint_version_is_not_current")
    components = row[:2]
    if any(not isinstance(value, str) or not 1 <= len(value) <= 32 for value in components):
        raise _StaleReference("code_physical_identity_codec_invalid")
    identity = FileIdentity.decode(
        ":".join(value.zfill(32) for value in components),
        encoding=FileIdentityEncoding.PACKED_HEX_V1,
    )
    version = connection.execute(
        f"SELECT {','.join(_VERSION_COLUMNS)} FROM file_versions WHERE version_id=?",
        (version_id,),
    ).fetchone()
    if version is None:
        raise _StaleReference("published_reference_endpoint_version_missing")
    _matched_member(members, f"version:{version_id}", "file_versions", tuple(version), version_id)
    if (
        any(type(value) is not int or value < 0 for value in row[6:8])
        or type(row[8]) is not int
        or row[8] < -1
    ):
        raise _StaleReference("reference_endpoint_snapshot_invalid")
    owner_path = str(row[2])
    snapshot = _inventory_snapshot_for_identity(index, scan_id, identity)
    owner_path_snapshot = _inventory_snapshot_for_identity(
        index, scan_id, identity, path=owner_path,
    )
    if owner_path_snapshot is not None and owner_path_snapshot.identity != (
        identity.volume_id, identity.file_id
    ):
        raise _StaleReference("reference_endpoint_identity_changed")
    if snapshot is None:
        # A path disappearing from the selected inventory is not itself an
        # identity change. If the same path now belongs to another object,
        # however, the published Code identity is demonstrably stale.
        return None
    if (snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns) != (row[6], row[7], row[8]):
        raise _StaleReference("reference_endpoint_revision_changed")
    _verify_snapshot(index, scan_id, snapshot, root)
    return _EndpointResolution(snapshot, owner_path_snapshot is None or owner_path_snapshot.path != owner_path)


def resolve_keeper_references(
    index: DedupIndex, scan_id: int, code_database: Path, *, max_relations: int = 1000
) -> KeeperReferenceResolution:
    """Resolve proven inbound Code relations without changing either owner or files.

    The full published membership reader is preflight-bounded by relation and
    materialization budgets. Unavailable/truncated/stale evidence produces an
    empty reference preference, not a claim that references do not exist.
    """
    if (
        type(scan_id) is not int
        or scan_id < 1
        or type(max_relations) is not int
        or not 1 <= max_relations <= 10000
    ):
        raise ValueError("scan_id must be positive and max_relations must be between 1 and 10000")
    deadline = time.monotonic() + _TIMEOUT_SECONDS

    def check() -> None:
        if time.monotonic() >= deadline:
            raise _ReferenceBudgetExceeded("reference_resolution_deadline_exceeded")

    source = Path(code_database)
    try:
        root, inventory_fence = _scan_root(index, scan_id)
        check()
        session = SQLiteReadSession(
            source,
            mode=preferred_sqlite_read_mode(source),
            timeout_seconds=_TIMEOUT_SECONDS,
            budget=SQLiteSnapshotBudget(
                max_temporary_bytes=_SNAPSHOT_BYTES,
                prepare_timeout_seconds=_TIMEOUT_SECONDS,
                cancellation_check=check,
            ),
        )
        with session as connection:
            owner_fence = session.source_fence
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if read_metadata_schema_version(connection, label="Code") != CODE_SCHEMA_VERSION:
                return _unavailable("unavailable", "code_owner_schema_unsupported")
            validate_sqlite_schema_contract(
                connection, code_schema_contract(), label="Code", exact=True
            )
            store = CodeGraphGenerationStore(connection)
            head = store.get_head()
            if head is None:
                return _unavailable("unavailable", "code_published_graph_absent")
            member_limit = max_relations * 16 + 64
            materialization = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST(item_key AS BLOB))+length(CAST(item_digest AS BLOB))+length(CAST(metadata_json AS BLOB))),0) "
                "FROM (SELECT item_key,item_digest,metadata_json FROM graph_memberships WHERE generation_id=? LIMIT ?)",
                (head.generation_id, member_limit + 1),
            ).fetchone()
            if materialization[0] > member_limit or materialization[1] > _GRAPH_PAYLOAD_BYTES:
                return _unavailable("truncated", "published_graph_materialization_budget_exceeded")
            published = store.read_published_generation()
            if published is None or published.head != head:
                raise _StaleReference("code_graph_head_changed")
            if (
                published.generation.metadata.get("contract") != "code-graph-legacy-bridge-v1"
                or type(published.generation.metadata.get("source_run_id")) is not int
            ):
                return _unavailable("unavailable", "code_graph_reference_contract_unsupported")
            members = {member.item_key: member for member in published.memberships}
            relations = [
                member
                for member in published.memberships
                if member.metadata.get("table") in {"code_references", "dependencies"}
            ]
            if len(relations) > max_relations:
                return _unavailable("truncated", "published_reference_relation_budget_exceeded")
            evidence: dict[tuple[int, int], set[str]] = {}
            endpoints: dict[int, _EndpointResolution | None] = {}
            proof_count = 0
            head_evidence = f"code:head:{head.head_name}:generation:{head.generation_id}:digest:{head.generation_digest}:revision:{head.revision}"
            for membership in relations:
                check()
                table = str(membership.metadata["table"])
                prefix = "reference" if table == "code_references" else "dependency"
                identity_text = membership.item_key.removeprefix(prefix + ":")
                if (
                    not membership.item_key.startswith(prefix + ":")
                    or not identity_text.isascii()
                    or not identity_text.isdecimal()
                    or identity_text.startswith("0")
                ):
                    raise _StaleReference("published_relation_key_invalid")
                relation_id = int(identity_text)
                columns = _REFERENCE_COLUMNS if table == "code_references" else _DEPENDENCY_COLUMNS
                row = connection.execute(
                    f"SELECT {','.join(columns)} FROM {table} WHERE {columns[0]}=?", (relation_id,)
                ).fetchone()
                if row is None:
                    raise _StaleReference("published_relation_missing")
                source_version = int(row[1])
                _matched_member(members, membership.item_key, table, tuple(row), source_version)
                target_version = row[4] if table == "code_references" else row[2]
                confirmed = row[8] if table == "code_references" else row[7]
                if (
                    confirmed != 1
                    or type(target_version) is not int
                    or target_version < 1
                    or target_version == source_version
                ):
                    continue
                if table == "code_references":
                    if row[2] is not None:
                        source_symbol = connection.execute(
                            "SELECT version_id FROM symbols WHERE symbol_id=?", (row[2],)
                        ).fetchone()
                        if source_symbol is None or source_symbol[0] != source_version:
                            raise _StaleReference("reference_source_symbol_version_mismatch")
                    symbol = connection.execute(
                        "SELECT version_id FROM symbols WHERE symbol_id=?", (row[3],)
                    ).fetchone()
                    if symbol is None or symbol[0] != target_version:
                        continue  # unresolved references are not a preference
                for version in (source_version, target_version):
                    if version not in endpoints:
                        endpoints[version] = _endpoint(
                            connection, members, index, scan_id, root, version
                        )
                origin, target = endpoints[source_version], endpoints[target_version]
                if (
                    origin is None
                    or target is None
                    or origin.snapshot.identity == target.snapshot.identity
                ):
                    continue
                ids = evidence.setdefault(target.snapshot.identity, set())
                ids.add(head_evidence)
                ids.add(
                    f"code:{prefix}:{relation_id}:source_version:{source_version}:target_version:{target_version}"
                )
                for endpoint, endpoint_version in (
                    (origin, source_version), (target, target_version)
                ):
                    if endpoint.owner_path_stale:
                        ids.add(f"code:owner_path_stale:version:{endpoint_version}")
                proof_count += 1
            check()
            if capture_sqlite_read_fence(source) != owner_fence:
                raise _StaleReference("code_owner_changed_during_reference_resolution")
        snapshots = tuple(
            endpoint.snapshot for endpoint in endpoints.values() if endpoint is not None
        )
        policy = KeeperPolicy(
            verified_reference_identities=tuple(sorted(evidence)),
            verified_reference_evidence=tuple(
                (identity, tuple(sorted(ids))) for identity, ids in sorted(evidence.items())
            ),
        )

        def revalidate() -> None:
            try:
                if (
                    capture_sqlite_read_fence(source) != owner_fence
                    or _scan_root(index, scan_id)[1] != inventory_fence
                ):
                    raise _StaleReference("reference_owner_or_inventory_changed")
                for snapshot in snapshots:
                    _verify_snapshot(index, scan_id, snapshot, root)
            except (OSError, ValueError, sqlite3.Error, ImmutableSQLiteUnavailable) as error:
                raise KeeperReferenceChanged(
                    "keeper reference evidence changed before publication"
                ) from error

        if evidence:
            revalidate()
        return KeeperReferenceResolution(
            policy,
            "available",
            None if evidence else "no_eligible_reference_in_selected_inventory",
            proof_count,
            revalidate if evidence else None,
        )
    except (_ReferenceBudgetExceeded, SQLiteSnapshotBudgetExceeded) as error:
        return _unavailable("truncated", str(error))
    except (_StaleReference, KeeperReferenceChanged) as error:
        return _unavailable("stale", str(error))
    except (
        OSError,
        ValueError,
        TypeError,
        RecursionError,
        sqlite3.Error,
        GenerationError,
        SQLiteSchemaContractError,
        ImmutableSQLiteUnavailable,
    ) as error:
        status = "truncated" if time.monotonic() >= deadline else "unavailable"
        return _unavailable(status, f"code_reference_evidence_unavailable:{type(error).__name__}")


__all__ = ["KeeperReferenceChanged", "KeeperReferenceResolution", "resolve_keeper_references"]
