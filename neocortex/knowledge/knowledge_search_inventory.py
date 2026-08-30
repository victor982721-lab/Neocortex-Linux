"""Inventory relationship support for the Knowledge Search facade."""

# region [00] Contexto del módulo
# Módulo: neocortex/knowledge_search_inventory.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from neocortex import platform_policy

from .knowledge_contracts import KnowledgeSnapshot, ResourceRef
from .knowledge_search_contracts import KnowledgeCandidate, RankingExecution
from .knowledge_snapshot import KnowledgeStatePaths
# endregion [01]
# region [02] Implementación


InventoryIdentity = tuple[int, int, int]
InventoryHead = tuple[int, int, int, int, int]
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
        or identity.scheme not in platform_policy.PHYSICAL_IDENTITY_SCHEMES
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
    valid_birthtime = (
        birthtime_ns >= 0
        if identity.scheme == platform_policy.WINDOWS_PHYSICAL_IDENTITY_SCHEME
        else birthtime_ns == platform_policy.UNAVAILABLE_BIRTHTIME_NS
    )
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
) -> tuple[tuple[InventoryHead, ...], bool]:
    heads: set[InventoryHead] = set()
    malformed = False
    for owner in snapshot.owners:
        if owner.owner != "inventory" or owner.state is not available_state:
            continue
        for head in owner.publications:
            signature = head.model_signature
            if signature is None:
                continue
            parts = signature.split(":")
            try:
                values = tuple(int(value, 10) for value in parts[1:])
            except ValueError:
                malformed = True
                continue
            if (
                len(parts) != 5
                or parts[0] != "duplicate-plan-v1"
                or len(values) != 4
                or any(value < 0 for value in values)
                or any(part != str(value) for part, value in zip(parts[1:], values, strict=True))
            ):
                malformed = True
                continue
            completed_ns, group_count, redundant_files, reclaimable_bytes = values
            heads.add(
                (
                    head.generation,
                    completed_ns,
                    group_count,
                    redundant_files,
                    reclaimable_bytes,
                )
            )
    return tuple(sorted(heads)), malformed


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
        and len(value) == 32
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
    if values.matched[2] < 0 or values.keeper[2] < 0:
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
    if not _relation_role_is_valid(values):
        return None
    return values.matched, values.role, values.keeper


def _inventory_rows(
    connection: _Connection,
    identity_batch: Sequence[InventoryIdentity],
    head_batch: Sequence[InventoryHead],
    remaining: int,
    identity_blob: _IdentityBlob,
) -> list[InventoryRow]:
    wanted_values = ",".join("(?,?,?)" for _ in identity_batch)
    head_values = ",".join("(?,?,?,?,?)" for _ in head_batch)
    parameters: list[object] = []
    for volume_id, file_id, birthtime_ns in identity_batch:
        parameters.extend((identity_blob(volume_id), identity_blob(file_id), birthtime_ns))
    for head in head_batch:
        parameters.extend(head)
    result = connection.execute(
        f"""WITH wanted(volume_id,file_id,birthtime_ns) AS (VALUES {wanted_values}),
        heads(scan_id,completed_ns,group_count,redundant_files,reclaimable_bytes) AS (VALUES {head_values})
        SELECT f.volume_id AS file_volume_id,f.file_id AS file_id,f.birthtime_ns AS file_birthtime_ns,
        f.path AS file_path,f.size AS file_size,member.volume_id AS member_volume_id,
        member.file_id AS member_file_id,member.birthtime_ns AS member_birthtime_ns,member.member_order,CASE WHEN member.path IS NULL THEN 0 ELSE 1 END AS member_present,
        member.role AS member_role,member.path AS member_path,member.size AS member_size,g.size AS group_size,g.redundant_count,
        g.reclaimable_bytes AS group_reclaimable_bytes,g.full_fingerprint,keeper.volume_id AS keeper_volume_id,keeper.file_id AS keeper_file_id,
        keeper.birthtime_ns AS keeper_birthtime_ns,keeper.member_order AS keeper_member_order,keeper.role AS keeper_role,keeper.path AS keeper_path,keeper.size AS keeper_size,
        keeper_file.path AS keeper_file_path,keeper_file.size AS keeper_file_size,
        CASE WHEN keeper.path=g.keep_path COLLATE {_PATH_COLLATION} THEN 1 ELSE 0 END AS keep_path_matches,(SELECT COUNT(*) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id) AS member_count,
        (SELECT COUNT(DISTINCT counted.member_order) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id) AS distinct_member_order_count,
        (SELECT COUNT(*) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id AND counted.role='keep') AS keep_count,
        (SELECT COUNT(*) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id AND counted.role='redundant') AS redundant_role_count,
        (SELECT COUNT(*) FROM planned_duplicate_members counted WHERE counted.group_id=g.group_id AND NOT ((counted.role='keep' AND counted.member_order=0) OR (counted.role='redundant' AND counted.member_order BETWEEN 1 AND g.redundant_count))) AS invalid_role_order_count
        FROM wanted w CROSS JOIN files f ON f.volume_id=w.volume_id AND f.file_id=w.file_id AND f.birthtime_ns=w.birthtime_ns
        JOIN heads h ON h.scan_id=f.scan_id JOIN duplicate_plan_summaries summary ON summary.scan_id=h.scan_id AND summary.completed_ns=h.completed_ns AND summary.group_count=h.group_count AND summary.redundant_files=h.redundant_files AND summary.reclaimable_bytes=h.reclaimable_bytes
        LEFT JOIN planned_duplicate_members member
        ON member.path=f.path COLLATE {_PATH_COLLATION} AND member.volume_id=f.volume_id
        AND member.file_id=f.file_id AND member.birthtime_ns=f.birthtime_ns
        LEFT JOIN planned_duplicate_groups g ON g.group_id=member.group_id
        AND g.scan_id=f.scan_id LEFT JOIN planned_duplicate_members keeper
        ON keeper.group_id=g.group_id AND keeper.member_order=0
        LEFT JOIN files keeper_file ON keeper_file.scan_id=g.scan_id
        AND keeper_file.path=keeper.path COLLATE {_PATH_COLLATION}
        AND keeper_file.volume_id=keeper.volume_id
        AND keeper_file.file_id=keeper.file_id
        AND keeper_file.birthtime_ns=keeper.birthtime_ns
        ORDER BY f.volume_id,f.file_id,f.birthtime_ns, g.scan_id,g.group_id LIMIT ?""",
        (*parameters, remaining + 1),
    )
    return cast(list[InventoryRow], result.fetchall())


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
        row_keys = set(row.keys())
        member_evidence = any(
            row[name] is not None
            for name in "member_volume_id member_file_id member_birthtime_ns member_order member_role member_path member_size".split()
            if name in row_keys
        )
        member_present = (
            row["member_present"] if "member_present" in row_keys else int(member_evidence)
        )
        if type(member_present) is not int or member_present not in (0, 1):
            raise ValueError
        if member_present != int(member_evidence):
            raise ValueError
        relation = dependencies.relation_row(row) if member_present else None
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        state.invalid_identities.add(matched_identity)
        return
    if not member_present:
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
        for head_start in range(0, len(plan_heads), dependencies.head_batch_size):
            cancellation.checkpoint()
            head_batch = plan_heads[head_start : head_start + dependencies.head_batch_size]
            remaining = dependencies.max_relations - state.rows_scanned
            rows = _inventory_rows(
                connection,
                identity_batch,
                head_batch,
                remaining,
                dependencies.identity_blob,
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
        tuple[tuple[InventoryHead, ...], bool],
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
    if malformed_heads:
        return unchanged, _report(
            ranking_execution_type,
            True,
            True,
            False,
            reason="invalid_inventory_plan_watermark",
        )
    if not plan_heads:
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
