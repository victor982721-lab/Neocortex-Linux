"""Inventory relationship support for the Knowledge Search facade."""
# region [00] Contexto del módulo
# Módulo: neocortex/knowledge_search_inventory.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations
from neocortex.knowledge.knowledge_read_operation import read_rows, read_query_limit
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from neocortex.platform import policy as platform_policy
from neocortex.persistence.sqlite_paths import readonly_sqlite_uri as _CANONICAL_READONLY_SQLITE_URI
from neocortex.deduplication.inventory.plan_evidence import decode_group_proof, decode_member_proof
from neocortex.deduplication.domain.errors import InventoryError

from .knowledge_contracts import KnowledgeSnapshot, ResourceRef
from .knowledge_search_contracts import KnowledgeCandidate, RankingExecution
from .knowledge_snapshot import KnowledgeStatePaths
# endregion [01]
# region [02] Implementación
InventoryIdentity = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class InventoryHead:
    """One inventory publication, retaining its owner scope and identity."""

    scan_id: int
    completed_ns: int
    group_count: int
    redundant_files: int
    reclaimable_bytes: int
    scope: str | None = None
    publication_id: str | None = None

    @property
    def sql_values(self) -> tuple[int, int, int, int, int]:
        return (
            self.scan_id,
            self.completed_ns,
            self.group_count,
            self.redundant_files,
            self.reclaimable_bytes,
        )


@dataclass(frozen=True, slots=True)
class InventoryPlanIssue:
    """A malformed publication kept local to one inventory scan/scope."""

    scan_id: int
    scope: str | None
    publication_id: str | None
    reason: str
    signature: str | None = None


def _head_sql_values(head: InventoryHead | Sequence[int]) -> tuple[int, int, int, int, int]:
    if isinstance(head, InventoryHead):
        return head.sql_values
    values = tuple(int(value) for value in head)
    if len(values) != 5:
        raise ValueError("inventory plan head has an invalid shape")
    return values


def _issue_sql_values(issue: InventoryPlanIssue) -> tuple[int, str | None]:
    return issue.scan_id, issue.scope


InventoryChoice = tuple[str, InventoryIdentity]
InventoryRow = sqlite3.Row
_Connection = Any
_CleanupPreservingPrimary = Callable[..., None]
_IdentityBlob = Callable[[int], bytes]
_ValidatedBlob = Callable[[object], int]
_RelationRow = Callable[
    [InventoryRow],
    tuple[InventoryIdentity, str, InventoryIdentity] | None,
]
_PhysicalIdentity = Callable[[ResourceRef], InventoryIdentity | None]
_Replace = Callable[..., Any]
_RankingFactory = type[RankingExecution]
_PATH_COLLATION = platform_policy.sqlite_path_collation()


@dataclass(frozen=True, slots=True)
class _ReadDependencies:
    open_sqlite: Callable[[Path], _Connection]
    identity_blob: _IdentityBlob
    validated_blob: _ValidatedBlob
    relation_row: _RelationRow
    cleanup: _CleanupPreservingPrimary
    identity_batch_size: int
    head_batch_size: int
    max_relations: int
    sqlite_error: type[BaseException]
    ranking_factory: _RankingFactory


@dataclass(slots=True)
class _CancellationCapture:
    callback: Callable[[], None] | None
    captured: BaseException | None = None

    def checkpoint(self) -> None:
        if self.callback is None:
            return
        try:
            self.callback()
        except BaseException as exc:
            self.captured = exc
            raise

    def raised(self, exc: BaseException) -> bool:
        return self.captured is exc


@dataclass(slots=True)
class _InventoryReadState:
    decisions: dict[InventoryIdentity, set[InventoryChoice]] = field(default_factory=dict)
    covered_identities: set[InventoryIdentity] = field(default_factory=set)
    invalid_identities: set[InventoryIdentity] = field(default_factory=set)
    rows_scanned: int = 0


@dataclass(frozen=True, slots=True)
class _RelationValues:
    matched: InventoryIdentity
    member: InventoryIdentity
    keeper: InventoryIdentity
    role: str
    member_order: int
    group_size: int
    redundant_count: int
    member_count: int
    distinct_member_order_count: int
    keep_count: int
    redundant_role_count: int
    invalid_role_order_count: int
    keeper_member_order: int
    keeper_role: str
    file_size: int
    member_size: int
    keeper_size: int
    keeper_file_size: int
    reclaimable_bytes: int
    keep_path_matches: int


def _report(
    factory: _RankingFactory,
    executed: bool,
    available: bool,
    complete: bool,
    *,
    returned: int = 0,
    rows_scanned: int = 0,
    reason: str | None = None,
) -> RankingExecution:
    return factory(
        "inventory_duplicate_plan",
        "relationship",
        executed,
        available,
        complete,
        returned,
        rows_scanned=rows_scanned,
        reason=reason,
    )


def open_direct_readonly_sqlite(
    path: Path,
    *,
    sqlite_connect: Callable[..., sqlite3.Connection],
    readonly_sqlite_uri: Callable[[Path], str],
    sqlite_row_factory: Any,
    sqlite_operational_error: type[BaseException],
    cleanup_preserving_primary: _CleanupPreservingPrimary,
) -> sqlite3.Connection:
    """Open an existing SQLite owner with read-only behavior verified live."""

    # Production callers pass the module's canonical providers.  Route those
    # calls through the fenced immutable kernel; retaining the injected branch
    # below keeps the historical extraction seam usable for fixtures and
    # callers that provide their own connection policy.
    if (
        path.is_file()
        and sqlite_connect is sqlite3.connect
        and readonly_sqlite_uri is _CANONICAL_READONLY_SQLITE_URI
        and sqlite_row_factory is sqlite3.Row
        and sqlite_operational_error is sqlite3.OperationalError
    ):
        from neocortex.runtime.control.read_operation import operation_strict_sqlite_connection
        return operation_strict_sqlite_connection(path, timeout_seconds=60.0)

    connection = sqlite_connect(
        readonly_sqlite_uri(path),
        uri=True,
        timeout=60,
    )
    try:
        connection.row_factory = sqlite_row_factory
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=60000")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if (
            foreign_keys is None
            or int(foreign_keys[0]) != 1
            or query_only is None
            or int(query_only[0]) != 1
        ):
            raise sqlite_operational_error("read-only SQLite safeguards could not be enabled")
        return connection
    except BaseException as exc:
        cleanup_preserving_primary(
            connection.close,
            exc,
            label="direct read-only SQLite close cleanup",
        )
        raise


def physical_identity_tuple(
    resource: ResourceRef,
    *,
    file_identity_type: Callable[[int, int], object],
    file_identity_errors: tuple[type[BaseException], ...],
) -> InventoryIdentity | None:
    identity = resource.physical_identity
    if (
        identity is None
        or identity.scheme != platform_policy.POSIX_PHYSICAL_IDENTITY_SCHEME
        or identity.identity_version != 1
        or resource.resource_id != f"resource:file:{identity.value}"
    ):
        return None
    components = identity.value.split(":")
    if len(components) != 3:
        return None
    try:
        volume_id, file_id, birthtime_ns = (int(component, 10) for component in components)
        file_identity_type(volume_id, file_id)
    except file_identity_errors:
        return None
    valid_birthtime = birthtime_ns >= platform_policy.UNAVAILABLE_BIRTHTIME_NS
    if not valid_birthtime or any(
        component != str(value)
        for component, value in zip(
            components,
            (volume_id, file_id, birthtime_ns),
            strict=True,
        )
    ):
        return None
    return volume_id, file_id, birthtime_ns


def inventory_plan_heads(
    snapshot: KnowledgeSnapshot,
    *,
    available_state: object,
) -> tuple[tuple[InventoryHead, ...], tuple[InventoryPlanIssue, ...]]:
    heads: dict[tuple[int, str | None], InventoryHead] = {}
    issues: list[InventoryPlanIssue] = []
    for owner in snapshot.owners:
        if owner.owner != "inventory" or owner.state is not available_state:
            continue
        for head in owner.publications:
            signature = head.model_signature
            if signature is None:
                # A publication with no duplicate plan is a valid inventory
                # head, but it cannot participate in relation joins.
                continue
            parts = signature.split(":")
            try:
                values = tuple(int(value, 10) for value in parts[1:])
            except ValueError:
                issues.append(
                    InventoryPlanIssue(
                        head.generation,
                        head.scope,
                        head.publication_id,
                        "invalid_inventory_plan_watermark",
                        signature,
                    )
                )
                continue
            if (
                len(parts) != 5
                or parts[0] != "duplicate-plan-v1"
                or len(values) != 4
                or any(value < 0 for value in values)
                or any(part != str(value) for part, value in zip(parts[1:], values, strict=True))
            ):
                issues.append(
                    InventoryPlanIssue(
                        head.generation,
                        head.scope,
                        head.publication_id,
                        "invalid_inventory_plan_watermark",
                        signature,
                    )
                )
                continue
            completed_ns, group_count, redundant_files, reclaimable_bytes = values
            key = (head.generation, head.scope)
            current = InventoryHead(
                head.generation,
                completed_ns,
                group_count,
                redundant_files,
                reclaimable_bytes,
                head.scope,
                head.publication_id,
            )
            prior = heads.get(key)
            if prior is not None and prior.sql_values != current.sql_values:
                issues.append(
                    InventoryPlanIssue(
                        head.generation,
                        head.scope,
                        head.publication_id,
                        "conflicting_inventory_plan_watermark",
                        signature,
                    )
                )
                heads.pop(key, None)
            elif prior is None:
                heads[key] = current
    return tuple(sorted(heads.values(), key=lambda item: (item.scan_id, item.scope or ""))), tuple(issues)


def inventory_identity_blob(value: int) -> bytes:
    return value.to_bytes(16, "little", signed=False)


def validated_inventory_blob(
    value: object,
    *,
    file_identity_type: Callable[[int, int], object],
) -> int:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("inventory identity is not a BLOB")
    encoded = bytes(value)
    if len(encoded) != 16:
        raise ValueError("inventory identity BLOB is not 16 bytes")
    decoded = int.from_bytes(encoded, "little")
    file_identity_type(decoded, 0)
    return decoded


def valid_full_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _parse_relation_values(
    row: InventoryRow,
    *,
    validated_inventory_blob: Callable[[object], int],
    file_identity_type: Callable[[int, int], object],
) -> _RelationValues | None:
    try:
        matched = (
            validated_inventory_blob(row["file_volume_id"]),
            validated_inventory_blob(row["file_id"]),
            int(row["file_birthtime_ns"]),
        )
        member = (
            validated_inventory_blob(row["member_volume_id"]),
            validated_inventory_blob(row["member_file_id"]),
            int(row["member_birthtime_ns"]),
        )
        keeper = (
            validated_inventory_blob(row["keeper_volume_id"]),
            validated_inventory_blob(row["keeper_file_id"]),
            int(row["keeper_birthtime_ns"]),
        )
        file_identity_type(matched[0], matched[1])
        file_identity_type(keeper[0], keeper[1])
        return _RelationValues(
            matched=matched,
            member=member,
            keeper=keeper,
            role=str(row["member_role"]),
            member_order=int(row["member_order"]),
            group_size=int(row["group_size"]),
            redundant_count=int(row["redundant_count"]),
            member_count=int(row["member_count"]),
            distinct_member_order_count=int(row["distinct_member_order_count"]),
            keep_count=int(row["keep_count"]),
            redundant_role_count=int(row["redundant_role_count"]),
            invalid_role_order_count=int(row["invalid_role_order_count"]),
            keeper_member_order=int(row["keeper_member_order"]),
            keeper_role=str(row["keeper_role"]),
            file_size=int(row["file_size"]),
            member_size=int(row["member_size"]),
            keeper_size=int(row["keeper_size"]),
            keeper_file_size=int(row["keeper_file_size"]),
            reclaimable_bytes=int(row["group_reclaimable_bytes"]),
            keep_path_matches=int(row["keep_path_matches"]),
        )
    except (TypeError, ValueError):
        return None


def _paths_match(row: InventoryRow, first: str, second: str) -> bool:
    first_value = row[first]
    second_value = row[second]
    return (
        isinstance(first_value, str)
        and isinstance(second_value, str)
        and first_value.casefold() == second_value.casefold()
    )


def _relation_metrics_are_valid(values: _RelationValues) -> bool:
    sizes = {
        values.group_size,
        values.file_size,
        values.member_size,
        values.keeper_size,
        values.keeper_file_size,
    }
    return (
        values.group_size >= 0
        and values.redundant_count >= 1
        and values.member_count == values.redundant_count + 1
        and values.distinct_member_order_count == values.member_count
        and 0 <= values.member_order < values.member_count
        and values.keep_count == 1
        and values.redundant_role_count == values.redundant_count
        and values.invalid_role_order_count == 0
        and values.keeper_member_order == 0
        and values.keeper_role == "keep"
        and len(sizes) == 1
        and values.reclaimable_bytes == values.group_size * values.redundant_count
    )


def _proof_contract_is_valid(row: InventoryRow) -> bool:
    """Validate the persisted policy/proof contract when this owner exposes it.

    Older fixtures and migrated owners do not expose these columns; those rows
    remain advisory.  A current owner must not let a structurally plausible
    relation masquerade as exact evidence when its policy or proof is broken.
    """
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    if "plan_contract_present" not in keys or int(row["plan_contract_present"]) != 1:
        return True
    try:
        policy = str(row["plan_requested_policy"])
        coverage = str(row["plan_coverage"])
        verification_mode = str(row["plan_verification_mode"])
        comparisons = row["plan_exact_comparisons"]
        failures = row["plan_changed_or_unreadable_files"]
        group_mode = str(row["group_verification_mode"])
        group_proof = decode_group_proof(str(row["group_proof_json"]))
        member_proof = decode_member_proof(str(row["member_proof_json"]))
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError, InventoryError):
        return False
    if policy == "legacy_unknown":
        return verification_mode == "legacy_unknown" and coverage == "legacy_unknown"
    if policy not in {"fast", "exact"} or coverage not in {"complete", "partial"}:
        return False
    if type(comparisons) is not int or comparisons < 0:
        return False
    if type(failures) is not int or failures < 0:
        return False
    expected_mode = "partial" if failures else "full_hash" if policy == "exact" else "fast"
    expected_group_mode = "full_hash" if policy == "exact" else "fast"
    if verification_mode != expected_mode or group_mode != expected_group_mode:
        return False
    if coverage != ("partial" if failures else "complete"):
        return False
    if policy == "fast" and comparisons != 0:
        return False
    if policy == "exact" and comparisons < int(row["redundant_count"]):
        return False
    if group_proof is None or member_proof.proof_version == "legacy_unknown":
        return False
    if group_proof.requested_policy != policy:
        return False
    expected_result = "reference" if row["member_role"] == "keep" else (
        "equal" if policy == "exact" else "fingerprint_match"
    )
    expected_identity = None if expected_result == "reference" else (
        int.from_bytes(bytes(row["keeper_volume_id"]), "little"),
        int.from_bytes(bytes(row["keeper_file_id"]), "little"),
    )
    if (
        member_proof.comparison_result != expected_result
        or member_proof.compared_to_identity != expected_identity
        or str(row["member_path"]) not in member_proof.aliases
        or (expected_result == "equal" and member_proof.comparison_bytes != int(row["member_size"]))
    ):
        return False
    return True


def _relation_role_is_valid(values: _RelationValues) -> bool:
    if values.role == "keep":
        return values.member_order == 0 and values.matched == values.keeper
    if values.role == "redundant":
        return values.member_order >= 1 and values.matched != values.keeper
    return False


def inventory_relation_row(
    row: InventoryRow,
    *,
    validated_inventory_blob: Callable[[object], int],
    file_identity_type: Callable[[int, int], object],
    valid_full_fingerprint: Callable[[object], bool],
) -> tuple[InventoryIdentity, str, InventoryIdentity] | None:
    values = _parse_relation_values(
        row,
        validated_inventory_blob=validated_inventory_blob,
        file_identity_type=file_identity_type,
    )
    if values is None or values.matched != values.member:
        return None
    # Linux legitimately uses -1 when the filesystem exposes no birth time;
    # only values below that sentinel are malformed.
    if values.matched[2] < -1 or values.keeper[2] < -1:
        return None
    if not _paths_match(row, "member_path", "file_path"):
        return None
    if not _paths_match(row, "keeper_path", "keeper_file_path"):
        return None
    if values.keep_path_matches != 1:
        return None
    if not _relation_metrics_are_valid(values):
        return None
    if not valid_full_fingerprint(row["full_fingerprint"]):
        return None
    if not _proof_contract_is_valid(row):
        return None
    if not _relation_role_is_valid(values):
        return None
    return values.matched, values.role, values.keeper


def _supports_persisted_proof_contract(connection: _Connection) -> bool:
    """Return whether the concrete owner exposes v12 policy/proof columns."""
    if not isinstance(connection, sqlite3.Connection):
        return False
    try:
        summary = {str(row[1]) for row in connection.execute(
            "PRAGMA table_info(duplicate_plan_summaries)"
        )}
        groups = {str(row[1]) for row in connection.execute(
            "PRAGMA table_info(planned_duplicate_groups)"
        )}
        members = {str(row[1]) for row in connection.execute(
            "PRAGMA table_info(planned_duplicate_members)"
        )}
    except sqlite3.Error:
        return False
    return (
        {"requested_policy", "coverage", "exact_comparisons", "changed_or_unreadable_files", "verification_mode"}
        <= summary
        and {"verification_mode", "proof_json"} <= groups
        and "proof_json" in members
    )


def _inventory_rows(
    connection: _Connection,
    identity_batch: Sequence[InventoryIdentity],
    head_batch: Sequence[InventoryHead | Sequence[int]],
    remaining: int,
    identity_blob: _IdentityBlob,
    plan_issues: Sequence[InventoryPlanIssue] = (),
) -> list[InventoryRow]:
    wanted_values = ",".join("(?,?,?)" for _ in identity_batch)
    issue_values = tuple(plan_issues)
    extended_heads = bool(issue_values)
    head_values = ",".join(
        "(?,?,?,?,?,?)" if extended_heads else "(?,?,?,?,?)" for _ in head_batch
    )
    parameters: list[object] = []
    for volume_id, file_id, birthtime_ns in identity_batch:
        parameters.extend((identity_blob(volume_id), identity_blob(file_id), birthtime_ns))
    for head in head_batch:
        parameters.extend(_head_sql_values(head))
        if extended_heads:
            parameters.append(1)
    if extended_heads:
        issue_sql = ",".join("(?,?,?,?,?,?)" for _ in issue_values)
        head_values = ",".join(value for value in (head_values, issue_sql) if value)
        for issue in issue_values:
            parameters.extend((issue.scan_id, None, None, None, None, 0))
    contract = _supports_persisted_proof_contract(connection)
    proof_projection = (
        ",summary.requested_policy AS plan_requested_policy,summary.coverage AS plan_coverage,"
        "summary.exact_comparisons AS plan_exact_comparisons,"
        "summary.changed_or_unreadable_files AS plan_changed_or_unreadable_files,"
        "summary.verification_mode AS plan_verification_mode,"
        "g.verification_mode AS group_verification_mode,g.proof_json AS group_proof_json,"
        "member.proof_json AS member_proof_json,1 AS plan_contract_present"
        if contract else ",0 AS plan_contract_present"
    )
    valid_projection = ",h.plan_valid AS inventory_plan_valid" if extended_heads else ",1 AS inventory_plan_valid"
    head_cte = (
        "heads(scan_id,completed_ns,group_count,redundant_files,reclaimable_bytes,plan_valid)"
        if extended_heads else
        "heads(scan_id,completed_ns,group_count,redundant_files,reclaimable_bytes)"
    )
    summary_join = (
        "LEFT JOIN duplicate_plan_summaries summary ON summary.scan_id=h.scan_id "
        "AND h.plan_valid=1 AND summary.completed_ns=h.completed_ns "
        "AND summary.group_count=h.group_count AND summary.redundant_files=h.redundant_files "
        "AND summary.reclaimable_bytes=h.reclaimable_bytes"
        if extended_heads else
        "JOIN duplicate_plan_summaries summary ON summary.scan_id=h.scan_id "
        "AND summary.completed_ns=h.completed_ns AND summary.group_count=h.group_count "
        "AND summary.redundant_files=h.redundant_files AND summary.reclaimable_bytes=h.reclaimable_bytes"
    )
    validity_where = " WHERE h.plan_valid=0 OR summary.scan_id IS NOT NULL" if extended_heads else ""
    valid_join_guard = "h.plan_valid=1 AND " if extended_heads else ""
    remaining = read_query_limit(remaining)
    result = connection.execute(
        f"""WITH wanted(volume_id,file_id,birthtime_ns) AS (VALUES {wanted_values}),
        {head_cte} AS (VALUES {head_values})
        SELECT f.volume_id AS file_volume_id,f.file_id AS file_id,f.birthtime_ns AS file_birthtime_ns,
        f.path AS file_path,f.size AS file_size,member.volume_id AS member_volume_id,
        member.file_id AS member_file_id,member.birthtime_ns AS member_birthtime_ns,member.member_order,CASE WHEN member.path IS NULL THEN 0 ELSE 1 END AS member_present,
        CASE WHEN EXISTS(SELECT 1 FROM planned_duplicate_members identity_member JOIN planned_duplicate_groups identity_group ON identity_group.group_id=identity_member.group_id WHERE identity_group.scan_id=f.scan_id AND identity_member.volume_id=f.volume_id AND identity_member.file_id=f.file_id AND identity_member.birthtime_ns=f.birthtime_ns)
        THEN CASE WHEN EXISTS(SELECT 1 FROM planned_duplicate_members identity_member JOIN planned_duplicate_groups identity_group ON identity_group.group_id=identity_member.group_id JOIN files identity_file ON identity_file.scan_id=f.scan_id AND identity_file.path=identity_member.path COLLATE {_PATH_COLLATION} AND identity_file.volume_id=identity_member.volume_id AND identity_file.file_id=identity_member.file_id AND identity_file.birthtime_ns=identity_member.birthtime_ns WHERE identity_group.scan_id=f.scan_id AND identity_member.volume_id=f.volume_id AND identity_member.file_id=f.file_id AND identity_member.birthtime_ns=f.birthtime_ns) THEN 1 ELSE 2 END ELSE 0 END AS member_identity_state,
        member.role AS member_role,member.path AS member_path,member.size AS member_size,g.size AS group_size,g.redundant_count,
        g.reclaimable_bytes AS group_reclaimable_bytes,g.full_fingerprint,keeper.volume_id AS keeper_volume_id,keeper.file_id AS keeper_file_id,
        keeper.birthtime_ns AS keeper_birthtime_ns,keeper.member_order AS keeper_member_order,keeper.role AS keeper_role,keeper.path AS keeper_path,keeper.size AS keeper_size,
        keeper_file.path AS keeper_file_path,keeper_file.size AS keeper_file_size,
        CASE WHEN keeper.path=g.keep_path COLLATE {_PATH_COLLATION} THEN 1 ELSE 0 END AS keep_path_matches,(SELECT COUNT(*) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id) AS member_count,
        (SELECT COUNT(DISTINCT counted.member_order) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id) AS distinct_member_order_count,
        (SELECT COUNT(*) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id AND counted.role='keep') AS keep_count,
        (SELECT COUNT(*) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id AND counted.role='redundant') AS redundant_role_count,
        (SELECT COUNT(*) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id AND NOT ((counted.role='keep' AND counted.member_order=0) OR (counted.role='redundant' AND counted.member_order BETWEEN 1 AND g.redundant_count))) AS invalid_role_order_count
        {proof_projection}{valid_projection}
        FROM wanted w CROSS JOIN files f ON f.volume_id=w.volume_id AND f.file_id=w.file_id AND f.birthtime_ns=w.birthtime_ns
        JOIN heads h ON h.scan_id=f.scan_id {summary_join}
        LEFT JOIN planned_duplicate_members member
        ON {valid_join_guard}member.path=f.path COLLATE {_PATH_COLLATION} AND member.volume_id=f.volume_id
        AND member.file_id=f.file_id AND member.birthtime_ns=f.birthtime_ns
        AND EXISTS(SELECT 1 FROM planned_duplicate_groups member_group WHERE member_group.group_id=member.group_id AND member_group.scan_id=f.scan_id)
        LEFT JOIN planned_duplicate_groups g ON {valid_join_guard}g.group_id=member.group_id
        AND g.scan_id=f.scan_id LEFT JOIN planned_duplicate_members keeper
        ON {valid_join_guard}keeper.group_id=g.group_id AND keeper.member_order=0
        LEFT JOIN files keeper_file ON keeper_file.scan_id=g.scan_id
        AND keeper_file.path=keeper.path COLLATE {_PATH_COLLATION}
        AND keeper_file.volume_id=keeper.volume_id
        AND keeper_file.file_id=keeper.file_id
        AND keeper_file.birthtime_ns=keeper.birthtime_ns
        {validity_where}
        ORDER BY f.volume_id,f.file_id,f.birthtime_ns, g.scan_id,g.group_id LIMIT ?""",
        (*parameters, remaining + 1),
    )
    return cast(list[InventoryRow], read_rows(result))


def _record_inventory_row(
    row: InventoryRow,
    state: _InventoryReadState,
    dependencies: _ReadDependencies,
) -> None:
    try:
        matched_identity = (
            dependencies.validated_blob(row["file_volume_id"]),
            dependencies.validated_blob(row["file_id"]),
            int(row["file_birthtime_ns"]),
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return
    state.covered_identities.add(matched_identity)
    try:
        if int(row["inventory_plan_valid"]) != 1:
            state.invalid_identities.add(matched_identity)
            return
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        # Legacy injected rows predate per-head validity and remain compatible.
        pass
    try:
        row_keys = set(row.keys())
        member_evidence = any(
            row[name] is not None
            for name in "member_volume_id member_file_id member_birthtime_ns member_order member_role member_path member_size".split()
            if name in row_keys
        )
        member_present = (
            row["member_present"] if "member_present" in row_keys else int(member_evidence)
        )
        member_identity_state = row["member_identity_state"] if "member_identity_state" in row_keys else 0
        if type(member_present) is not int or member_present not in (0, 1):
            raise ValueError
        if member_present != int(member_evidence):
            raise ValueError
        if type(member_identity_state) is not int or member_identity_state not in (0, 1, 2):
            raise ValueError
        relation = dependencies.relation_row(row) if member_present else None
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        state.invalid_identities.add(matched_identity)
        return
    if not member_present:
        if member_identity_state == 2:
            state.invalid_identities.add(matched_identity)
        return
    if relation is None:
        state.invalid_identities.add(matched_identity)
        return
    matched, role, keeper = relation
    state.decisions.setdefault(matched, set()).add((role, keeper))


def _scan_inventory_batches(
    connection: _Connection,
    identities: Sequence[InventoryIdentity],
    plan_heads: Sequence[InventoryHead],
    state: _InventoryReadState,
    cancellation: _CancellationCapture,
    dependencies: _ReadDependencies,
    plan_issues: Sequence[InventoryPlanIssue] = (),
) -> bool:
    for identity_start in range(
        0,
        len(identities),
        dependencies.identity_batch_size,
    ):
        cancellation.checkpoint()
        identity_batch = identities[
            identity_start : identity_start + dependencies.identity_batch_size
        ]
        head_batches = (
            [plan_heads[start : start + dependencies.head_batch_size]
             for start in range(0, len(plan_heads), dependencies.head_batch_size)]
            or [()]
        )
        for head_batch in head_batches:
            cancellation.checkpoint()
            remaining = dependencies.max_relations - state.rows_scanned
            rows = _inventory_rows(
                connection,
                identity_batch,
                head_batch,
                remaining,
                dependencies.identity_blob,
                plan_issues=plan_issues,
            )
            if len(rows) > remaining:
                connection.execute("ROLLBACK")
                return True
            for row in rows:
                if (state.rows_scanned + 1) % 128 == 0:
                    cancellation.checkpoint()
                state.rows_scanned += 1
                _record_inventory_row(row, state, dependencies)
    return False


def _rollback_preserving_primary(
    connection: _Connection,
    primary: BaseException,
    cleanup_preserving_primary: _CleanupPreservingPrimary,
) -> None:
    if connection.in_transaction:
        cleanup_preserving_primary(
            lambda: connection.execute("ROLLBACK"),
            primary,
            label="inventory read rollback cleanup",
        )


def _sqlite_failure_report(
    connection: _Connection,
    failure: BaseException,
    state: _InventoryReadState,
    dependencies: _ReadDependencies,
) -> RankingExecution:
    rollback_error: BaseException | None = None
    if connection.in_transaction:
        try:
            connection.execute("ROLLBACK")
        except BaseException as error:
            if not isinstance(error, dependencies.sqlite_error):
                raise
            rollback_error = error
    reason = f"owner_read_failed:{type(failure).__name__}"
    if rollback_error is not None:
        reason += f":rollback_failed:{type(rollback_error).__name__}"
    return _report(
        dependencies.ranking_factory,
        True,
        True,
        False,
        rows_scanned=state.rows_scanned,
        reason=reason,
    )


def _read_inventory_relations(
    paths: KnowledgeStatePaths,
    identities: Sequence[InventoryIdentity],
    plan_heads: Sequence[InventoryHead],
    cancellation: _CancellationCapture,
    dependencies: _ReadDependencies,
    plan_issues: Sequence[InventoryPlanIssue] = (),
) -> tuple[_InventoryReadState | None, RankingExecution | None]:
    try:
        connection = dependencies.open_sqlite(paths.inventory)
    except BaseException as exc:
        if not isinstance(exc, dependencies.sqlite_error):
            raise
        return None, _report(
            dependencies.ranking_factory,
            True,
            False,
            False,
            reason=f"owner_read_failed:{type(exc).__name__}",
        )

    state = _InventoryReadState()
    primary_error: BaseException | None = None
    try:
        try:
            connection.execute("BEGIN")
            limit_exceeded = _scan_inventory_batches(
                connection,
                identities,
                plan_heads,
                state,
                cancellation,
                dependencies,
                plan_issues,
            )
            if not limit_exceeded:
                connection.execute("COMMIT")
        except BaseException as exc:
            if cancellation.raised(exc):
                primary_error = exc
                _rollback_preserving_primary(connection, exc, dependencies.cleanup)
                raise
            if isinstance(exc, dependencies.sqlite_error):
                try:
                    report = _sqlite_failure_report(
                        connection,
                        exc,
                        state,
                        dependencies,
                    )
                except BaseException as rollback_failure:
                    primary_error = rollback_failure
                    raise
                return None, report
            primary_error = exc
            _rollback_preserving_primary(connection, exc, dependencies.cleanup)
            raise
        if limit_exceeded:
            return None, _report(
                dependencies.ranking_factory,
                True,
                True,
                False,
                rows_scanned=dependencies.max_relations,
                reason="inventory_relation_limit_exceeded",
            )
        return state, None
    finally:
        if primary_error is None:
            connection.close()
        else:
            dependencies.cleanup(
                connection.close,
                primary_error,
                label="inventory read connection close cleanup",
            )


def _warning_candidate(
    candidate: KnowledgeCandidate,
    warning: str,
    replace_fn: _Replace,
) -> KnowledgeCandidate:
    warnings = tuple(sorted({*candidate.warnings, warning}))
    return replace_fn(candidate, warnings=warnings)


def _planned_candidate(
    candidate: KnowledgeCandidate,
    keeper: InventoryIdentity,
    replace_fn: _Replace,
) -> KnowledgeCandidate:
    keeper_id = f"resource:file:{keeper[0]}:{keeper[1]}:{keeper[2]}"
    identifiers = tuple(
        dict.fromkeys((*candidate.evidence.identifiers, ("planned_duplicate_of", keeper_id)))
    )
    evidence = replace_fn(candidate.evidence, identifiers=identifiers)
    warnings = tuple(sorted({*candidate.warnings, "inventory_planned_duplicate_unverified"}))
    return replace_fn(candidate, evidence=evidence, warnings=warnings)


def _choice_disposition(
    identity: InventoryIdentity,
    choices: set[InventoryChoice],
) -> tuple[str, InventoryIdentity | None]:
    keepers = {keeper for role, keeper in choices if role == "keep"}
    redundant = {keeper for role, keeper in choices if role == "redundant"}
    if (
        not keepers
        and len(redundant) == 1
        and identity not in redundant
        and all(role == "redundant" for role, _ in choices)
    ):
        return "planned", next(iter(redundant))
    if keepers and not redundant and all(role == "keep" for role, _ in choices):
        return "keep", None
    return "ambiguous", None


def _materialize_candidate(
    candidate: KnowledgeCandidate,
    state: _InventoryReadState,
    *,
    physical_identity_tuple: _PhysicalIdentity,
    replace_fn: _Replace,
) -> tuple[KnowledgeCandidate, str | None]:
    identity = physical_identity_tuple(candidate.resource)
    choices = state.decisions.get(identity) if identity is not None else None
    if identity in state.invalid_identities:
        return (
            _warning_candidate(
                candidate,
                "inventory_duplicate_plan_ambiguous",
                replace_fn,
            ),
            "ambiguous",
        )
    if identity is not None and identity not in state.covered_identities:
        return (
            _warning_candidate(
                candidate,
                "inventory_duplicate_plan_coverage_unknown",
                replace_fn,
            ),
            "uncovered",
        )
    if not choices or identity is None:
        return candidate, None
    disposition, keeper = _choice_disposition(identity, choices)
    if disposition == "planned" and keeper is not None:
        return _planned_candidate(candidate, keeper, replace_fn), "planned"
    if disposition == "keep":
        return candidate, None
    return (
        _warning_candidate(
            candidate,
            "inventory_duplicate_plan_ambiguous",
            replace_fn,
        ),
        "ambiguous",
    )


def _materialize_rankings(
    rankings: Mapping[str, Sequence[KnowledgeCandidate]],
    state: _InventoryReadState,
    *,
    physical_identity_tuple: Callable[[ResourceRef], InventoryIdentity | None],
    replace_fn: Callable[..., Any],
) -> tuple[
    dict[str, tuple[KnowledgeCandidate, ...]],
    set[str],
    set[str],
    set[str],
]:
    planned_resources: set[str] = set()
    ambiguous_resources: set[str] = set()
    uncovered_resources: set[str] = set()
    updated: dict[str, tuple[KnowledgeCandidate, ...]] = {}
    disposition_sets = {
        "planned": planned_resources,
        "ambiguous": ambiguous_resources,
        "uncovered": uncovered_resources,
    }
    for name, candidates in rankings.items():
        ranking: list[KnowledgeCandidate] = []
        for candidate in candidates:
            materialized, disposition = _materialize_candidate(
                candidate,
                state,
                physical_identity_tuple=physical_identity_tuple,
                replace_fn=replace_fn,
            )
            ranking.append(materialized)
            if disposition is not None:
                disposition_sets[disposition].add(candidate.resource.resource_id)
        updated[name] = tuple(ranking)
    return updated, planned_resources, ambiguous_resources, uncovered_resources


def _disposition_reason(
    planned_resources: set[str],
    ambiguous_resources: set[str],
    uncovered_resources: set[str],
) -> str | None:
    if ambiguous_resources:
        return "invalid_or_conflicting_duplicate_plan"
    if planned_resources:
        return "inventory_exact_verification_unavailable"
    if uncovered_resources:
        return "inventory_plan_coverage_unknown"
    return None


def apply_inventory_dispositions(
    paths: KnowledgeStatePaths,
    snapshot: KnowledgeSnapshot,
    rankings: Mapping[str, Sequence[KnowledgeCandidate]],
    *,
    cancellation_check: Callable[[], None] | None,
    owner_available: Callable[[KnowledgeSnapshot, str], bool],
    inventory_plan_heads: Callable[
        [KnowledgeSnapshot],
        tuple[tuple[InventoryHead, ...], tuple[InventoryPlanIssue, ...]],
    ],
    physical_identity_tuple: _PhysicalIdentity,
    open_direct_readonly_sqlite: Callable[[Path], _Connection],
    inventory_identity_blob: _IdentityBlob,
    validated_inventory_blob: _ValidatedBlob,
    inventory_relation_row: _RelationRow,
    cleanup_preserving_primary: _CleanupPreservingPrimary,
    identity_batch_size: int,
    head_batch_size: int,
    max_inventory_relations: int,
    sqlite_error_type: type[BaseException],
    ranking_execution_type: _RankingFactory,
    replace_fn: _Replace,
) -> tuple[dict[str, tuple[KnowledgeCandidate, ...]], RankingExecution]:
    """Read planned duplicate relations, but abstain without exact provenance."""

    unchanged = {name: tuple(candidates) for name, candidates in rankings.items()}
    if not owner_available(snapshot, "inventory"):
        return unchanged, _report(
            ranking_execution_type,
            False,
            False,
            True,
            reason="inventory_owner_unavailable",
        )
    plan_heads, malformed_heads = inventory_plan_heads(snapshot)
    if not plan_heads and not malformed_heads:
        return unchanged, _report(
            ranking_execution_type,
            False,
            True,
            True,
            reason="no_completed_inventory_plans",
        )
    identities = tuple(
        sorted(
            {
                identity
                for candidates in rankings.values()
                for candidate in candidates
                if (identity := physical_identity_tuple(candidate.resource)) is not None
            }
        )
    )
    if not identities:
        return unchanged, _report(
            ranking_execution_type,
            False,
            True,
            True,
            reason="no_physical_candidates",
        )
    dependencies = _ReadDependencies(
        open_direct_readonly_sqlite,
        inventory_identity_blob,
        validated_inventory_blob,
        inventory_relation_row,
        cleanup_preserving_primary,
        identity_batch_size,
        head_batch_size,
        max_inventory_relations,
        sqlite_error_type,
        ranking_execution_type,
    )
    state, read_report = _read_inventory_relations(
        paths,
        identities,
        plan_heads,
        _CancellationCapture(cancellation_check),
        dependencies,
        malformed_heads,
    )
    if read_report is not None:
        return unchanged, read_report
    if state is None:
        raise AssertionError("inventory read completed without state or report")
    updated, planned, ambiguous, uncovered = _materialize_rankings(
        rankings,
        state,
        physical_identity_tuple=physical_identity_tuple,
        replace_fn=replace_fn,
    )
    return updated, _report(
        ranking_execution_type,
        True,
        True,
        not ambiguous and not planned and not uncovered,
        returned=len(planned),
        rows_scanned=state.rows_scanned,
        reason=_disposition_reason(planned, ambiguous, uncovered),
    )


__all__ = (
    "apply_inventory_dispositions",
    "inventory_identity_blob",
    "inventory_plan_heads",
    "inventory_relation_row",
    "open_direct_readonly_sqlite",
    "physical_identity_tuple",
    "valid_full_fingerprint",
    "validated_inventory_blob",
)
# endregion [02]
