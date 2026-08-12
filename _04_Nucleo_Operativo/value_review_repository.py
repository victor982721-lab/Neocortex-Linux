"""Read-only adapters for published file value evidence.

Every SQLite connection is opened with ``mode=ro`` and verified with
``PRAGMA query_only=ON``.  This module never creates, migrates, checkpoints, or
repairs owner state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar, cast

from _02_Deduplicacion.inventory_schema import (
    SCHEMA_VERSION as INVENTORY_SCHEMA_VERSION,
    validate_inventory_schema,
)
from neocortex.sqlite_schema_contract import read_application_schema_version

from . import audio_state, document_catalog_schema, office_state, text_state
from .docx_schema import DOCX_SCHEMA_VERSION, validate_docx_schema
from .pdf_schema import PDF_SCHEMA_VERSION, validate_pdf_schema
from .sqlite_paths import readonly_sqlite_uri
from .sqlite_schema_contract import SQLiteSchemaContract, validate_sqlite_schema_contract
from .value_review_contracts import (
    ValueEvidence,
    ValueEvidenceFact,
    ValueEvidenceStrength,
    ValueFileObservation,
    ValueOwnerHealth,
    ValueProvenance,
    ValueReviewAvailability,
    ValueReviewPaths,
    ValueReviewQuery,
)


MAX_PUBLISHED_HEADS = 1_024
MAX_SQLITE_CANDIDATES = 25_000
MAX_VALUE_REVIEW_PAGE_INPUTS = 1_000
_SQLITE_BATCH = 300
_KEYSET_IDENTITY_BATCH = 200
_INACTIVE_SHM_SIZE_BYTES = 32_768
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ValueObservationLoad:
    availability: ValueReviewAvailability
    complete: bool
    reason: str | None
    observations: tuple[ValueFileObservation, ...]
    provenance: tuple[ValueProvenance, ...] = ()
    uncertainties: tuple[str, ...] = ()
    cursor_before: ValueReviewPageCursor | None = None
    cursor_after: ValueReviewPageCursor | None = None
    scanned_count: int = 0


@dataclass(frozen=True, slots=True)
class ValueReviewPageCursor:
    """Stable keyset over one immutable published inventory snapshot."""

    volume_id_hex: str
    file_id_hex: str
    birthtime_ns: int

    def __post_init__(self) -> None:
        for label, value in (
            ("volume_id_hex", self.volume_id_hex),
            ("file_id_hex", self.file_id_hex),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 32
                or value != value.upper()
                or any(character not in "0123456789ABCDEF" for character in value)
            ):
                raise ValueError(f"{label} must be 32 uppercase hexadecimal characters")
        if (
            isinstance(self.birthtime_ns, bool)
            or not isinstance(self.birthtime_ns, int)
            or self.birthtime_ns < -1
        ):
            raise ValueError("birthtime_ns must be an integer greater than or equal to -1")

    def to_dict(self) -> dict[str, object]:
        return {
            "birthtime_ns": self.birthtime_ns,
            "file_id_hex": self.file_id_hex,
            "volume_id_hex": self.volume_id_hex,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ValueReviewPageCursor:
        if not isinstance(value, dict) or set(value) != {
            "birthtime_ns",
            "file_id_hex",
            "volume_id_hex",
        }:
            raise ValueError("value review page cursor has an invalid shape")
        return cls(
            volume_id_hex=cast(str, value["volume_id_hex"]),
            file_id_hex=cast(str, value["file_id_hex"]),
            birthtime_ns=cast(int, value["birthtime_ns"]),
        )


@dataclass(frozen=True, slots=True)
class ValueReviewSourceSnapshot:
    """Bounded owner-head snapshot used to fence a durable review scan."""

    availability: ValueReviewAvailability
    reason: str | None
    payload_json: str
    uncertainties: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload = json.loads(self.payload_json)
        if not isinstance(payload, dict):  # pragma: no cover - constructor invariant
            raise AssertionError("source snapshot payload is not an object")
        return payload


@dataclass(frozen=True, slots=True)
class _InventoryHead:
    root: str
    scan_id: int
    updated_ns: int
    publication_id: str
    plan_available: bool
    plan_valid: bool
    plan_completed_ns: int | None


@dataclass(frozen=True, slots=True)
class _InventoryFile:
    head: _InventoryHead
    path: str
    volume_blob: bytes
    file_blob: bytes
    volume_id: int
    file_id: int
    size: int
    mtime_ns: int
    birthtime_ns: int
    conflicting_publication: bool = False

    @property
    def resource_id(self) -> str:
        return _resource_id(self.volume_id, self.file_id, self.birthtime_ns)


@dataclass(frozen=True, slots=True)
class _DuplicateFact:
    role: str
    full_fingerprint: str
    group_id: str
    keeper_resource_id: str


@dataclass(frozen=True, slots=True)
class _DuplicateMember:
    order: int
    role: str
    path: str
    size: int
    resource_id: str


@dataclass(slots=True)
class _DuplicateGroup:
    group_size: int
    keep_path: str
    redundant_count: int
    reclaimable_bytes: int
    full_fingerprint: str
    members: list[_DuplicateMember]
    keeper_resource_id: str | None = None


@dataclass(frozen=True, slots=True)
class _CatalogRecord:
    publication_id: str
    generation_id: int
    source_kind: str
    file_key: str
    path: str
    volume_id: str
    file_id: str
    size: int
    mtime_ns: int
    birthtime_ns: int
    source_status: str
    text_fingerprint: str | None
    primary_project: str | None
    catalog_status: str
    catalog_uncertainty: str
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class _OwnerSpec:
    owner: str
    expected_version: int
    path: Path | None
    validator: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True, slots=True)
class _SQLiteFileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _SQLiteReadSnapshot:
    main: _SQLiteFileIdentity
    sidecars: tuple[tuple[str, _SQLiteFileIdentity], ...]


class _StateContractError(RuntimeError):
    pass


class _StateIncompatibleError(_StateContractError):
    pass


def load_value_review_observations(
    paths: ValueReviewPaths,
    query: ValueReviewQuery,
    *,
    _after: ValueReviewPageCursor | None = None,
    _page_size: int | None = None,
) -> ValueObservationLoad:
    """Load bounded published facts without creating or changing owner state."""

    query.validate()
    try:
        inventory_stat = paths.inventory.stat()
    except FileNotFoundError:
        return _unavailable("inventory_state_absent")
    except OSError as exc:
        return _unavailable("inventory_state_inaccessible", _error_detail(exc))
    if not stat.S_ISREG(inventory_stat.st_mode):
        return _unavailable("inventory_state_not_a_file")

    try:
        with _readonly_connection(paths.inventory) as inventory:
            observed_version = _validate_owner_schema(
                inventory,
                owner="inventory",
                expected_version=INVENTORY_SCHEMA_VERSION,
                validator=validate_inventory_schema,
            )
            heads = _inventory_heads(inventory)
            inventory_provenance = tuple(
                ValueProvenance(
                    owner="inventory",
                    schema_version=observed_version,
                    publication_id=head.publication_id,
                )
                for head in heads
            )
            cursor_after: ValueReviewPageCursor | None = None
            if _page_size is None:
                files = _inventory_files(inventory, query, heads)
                if files is None:
                    return _unavailable(
                        "scope_too_broad",
                        "published_inventory_candidate_limit_exceeded",
                        provenance=inventory_provenance,
                    )
            else:
                files, cursor_after = _inventory_file_page(
                    inventory,
                    query,
                    heads,
                    after=_after,
                    page_size=_page_size,
                )
            duplicate_facts, duplicate_uncertainties = _duplicate_facts(
                inventory,
                files,
            )
    except _StateIncompatibleError as exc:
        return _unavailable("inventory_state_incompatible", str(exc))
    except (sqlite3.DatabaseError, _StateContractError, OSError, ValueError) as exc:
        inventory_reason = (
            "inventory_state_corrupt" if _is_corrupt_error(exc) else "inventory_state_invalid"
        )
        return _unavailable(inventory_reason, _error_detail(exc))

    if not heads:
        return ValueObservationLoad(
            ValueReviewAvailability.PARTIAL,
            True,
            "inventory_has_no_published_scans",
            (),
            (),
            ("inventory_has_no_published_scans",),
            _after,
            None,
            0,
        )

    catalog_records: dict[str, _CatalogRecord] = {}
    catalog_mismatches: set[str] = set()
    text_counts: dict[str, int] = {}
    owner_health: dict[
        str, tuple[ValueOwnerHealth, ValueEvidence | None, ValueProvenance | None]
    ] = {}
    catalog_provenance: tuple[ValueProvenance, ...] = ()
    global_uncertainties = list(duplicate_uncertainties)
    availability = (
        ValueReviewAvailability.PARTIAL
        if duplicate_uncertainties
        else ValueReviewAvailability.READY
    )
    reason: str | None = "duplicate_evidence_incomplete" if duplicate_uncertainties else None
    catalog_failure_health: ValueOwnerHealth | None = None

    catalog_status = _path_status(paths.catalog)
    if catalog_status != "file":
        availability = ValueReviewAvailability.PARTIAL
        reason = f"catalog_state_{catalog_status}"
        global_uncertainties.append(reason)
        if catalog_status != "absent":
            catalog_failure_health = ValueOwnerHealth.FAILED
    else:
        try:
            with _readonly_connection(paths.catalog) as catalog:
                catalog_version = _validate_owner_schema(
                    catalog,
                    owner="catalog",
                    expected_version=document_catalog_schema.CATALOG_SCHEMA_VERSION,
                    validator=_validate_catalog,
                )
                publications = _catalog_publications(catalog)
                catalog_provenance = tuple(
                    ValueProvenance(
                        owner="catalog",
                        schema_version=catalog_version,
                        publication_id=publication_id,
                    )
                    for _, _, publication_id in publications
                )
                matched, mismatched = _catalog_records(catalog, files)
                catalog_records = matched
                catalog_mismatches = mismatched
                if mismatched:
                    availability = ValueReviewAvailability.PARTIAL
                    reason = "catalog_snapshot_mismatch"
                    global_uncertainties.append("catalog_snapshot_mismatch")
                text_counts = _text_fingerprint_counts(
                    catalog,
                    tuple(
                        value.text_fingerprint
                        for value in matched.values()
                        if _valid_text_fingerprint(value.text_fingerprint)
                    ),
                )
                owner_health = _inspect_source_owners(paths, tuple(matched.values()))
                if not publications:
                    availability = ValueReviewAvailability.PARTIAL
                    reason = "catalog_has_no_publications"
                    global_uncertainties.append(reason)
                degraded = tuple(
                    health.value
                    for health, _, _ in owner_health.values()
                    if health is not ValueOwnerHealth.HEALTHY
                )
                if degraded:
                    availability = ValueReviewAvailability.PARTIAL
                    reason = reason or "source_owner_health_incomplete"
                    global_uncertainties.extend(
                        f"source_owner_health:{value}" for value in degraded
                    )
        except _StateIncompatibleError as exc:
            availability = ValueReviewAvailability.PARTIAL
            reason = "catalog_state_incompatible"
            global_uncertainties.extend((reason, str(exc)))
            catalog_records = {}
            catalog_failure_health = ValueOwnerHealth.INCOMPATIBLE
        except (sqlite3.DatabaseError, _StateContractError, OSError, ValueError) as exc:
            availability = ValueReviewAvailability.PARTIAL
            reason = "catalog_state_corrupt" if _is_corrupt_error(exc) else "catalog_state_invalid"
            global_uncertainties.extend((reason, _error_detail(exc)))
            catalog_records = {}
            catalog_failure_health = (
                ValueOwnerHealth.CORRUPT if _is_corrupt_error(exc) else ValueOwnerHealth.FAILED
            )

    observations = tuple(
        _observation(
            value,
            duplicate_facts.get(value.resource_id),
            catalog_records.get(value.resource_id),
            value.resource_id in catalog_mismatches,
            text_counts,
            owner_health,
            inventory_version=INVENTORY_SCHEMA_VERSION,
            catalog_failure_health=catalog_failure_health,
        )
        for value in files
    )
    return ValueObservationLoad(
        availability=availability,
        complete=cursor_after is None,
        reason=reason,
        observations=observations,
        provenance=_unique_provenance((*inventory_provenance, *catalog_provenance)),
        uncertainties=_unique_strings(tuple(global_uncertainties)),
        cursor_before=_after,
        cursor_after=cursor_after,
        scanned_count=len(files),
    )


def load_value_review_observation_page(
    paths: ValueReviewPaths,
    query: ValueReviewQuery,
    *,
    page_size: int,
    after: ValueReviewPageCursor | None = None,
) -> ValueObservationLoad:
    """Read one keyset page without weakening the legacy whole-scope bound."""

    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise ValueError("page_size must be an integer")
    if not 1 <= page_size <= MAX_VALUE_REVIEW_PAGE_INPUTS:
        raise ValueError(f"page_size must be between 1 and {MAX_VALUE_REVIEW_PAGE_INPUTS}")
    if after is not None and not isinstance(after, ValueReviewPageCursor):
        raise ValueError("after must be a ValueReviewPageCursor when present")
    return load_value_review_observations(
        paths,
        query,
        _after=after,
        _page_size=page_size,
    )


def read_value_review_source_snapshot(
    paths: ValueReviewPaths,
) -> ValueReviewSourceSnapshot:
    """Capture bounded owner-head facts without scanning candidate rows."""

    inventory_status = _path_status(paths.inventory)
    if inventory_status != "file":
        unavailable_reason = f"inventory_state_{inventory_status}"
        return _source_snapshot(
            ValueReviewAvailability.UNAVAILABLE,
            unavailable_reason,
            inventory={"status": inventory_status},
            catalog={"status": _path_status(paths.catalog)},
            source_owners=(),
            uncertainties=(unavailable_reason,),
        )
    try:
        with _readonly_connection(paths.inventory) as inventory:
            inventory_version = _validate_owner_schema(
                inventory,
                owner="inventory",
                expected_version=INVENTORY_SCHEMA_VERSION,
                validator=validate_inventory_schema,
            )
            heads = _inventory_heads(inventory)
            inventory_payload: dict[str, object] = {
                "status": "published" if heads else "empty",
                "schema_version": inventory_version,
                "publication_count": len(heads),
                "publications_sha256": _canonical_sha256(
                    [
                        {
                            "plan_available": head.plan_available,
                            "plan_completed_ns": head.plan_completed_ns,
                            "plan_valid": head.plan_valid,
                            "publication_id": head.publication_id,
                            "root": head.root,
                            "scan_id": head.scan_id,
                            "updated_ns": head.updated_ns,
                        }
                        for head in heads
                    ]
                ),
            }
    except _StateIncompatibleError as exc:
        return _source_snapshot(
            ValueReviewAvailability.UNAVAILABLE,
            "inventory_state_incompatible",
            inventory={"status": "incompatible"},
            catalog={"status": _path_status(paths.catalog)},
            source_owners=(),
            uncertainties=(_error_detail(exc),),
        )
    except (sqlite3.DatabaseError, _StateContractError, OSError, ValueError) as exc:
        inventory_reason = (
            "inventory_state_corrupt" if _is_corrupt_error(exc) else "inventory_state_invalid"
        )
        return _source_snapshot(
            ValueReviewAvailability.UNAVAILABLE,
            inventory_reason,
            inventory={"status": inventory_reason.removeprefix("inventory_state_")},
            catalog={"status": _path_status(paths.catalog)},
            source_owners=(),
            uncertainties=(_error_detail(exc),),
        )

    inventory_evidence_reason = (
        "inventory_has_no_published_scans"
        if not heads
        else (
            "duplicate_evidence_incomplete"
            if any(not head.plan_available or not head.plan_valid for head in heads)
            else None
        )
    )
    availability = (
        ValueReviewAvailability.READY
        if inventory_evidence_reason is None
        else ValueReviewAvailability.PARTIAL
    )
    reason: str | None = inventory_evidence_reason
    uncertainties: list[str] = [] if reason is None else [reason]
    catalog_status = _path_status(paths.catalog)
    catalog_payload: dict[str, object] = {"status": catalog_status}
    source_owner_payload: tuple[dict[str, object], ...] = ()
    if catalog_status != "file":
        availability = ValueReviewAvailability.PARTIAL
        catalog_reason = f"catalog_state_{catalog_status}"
        reason = reason or catalog_reason
        uncertainties.append(catalog_reason)
    else:
        try:
            with _readonly_connection(paths.catalog) as catalog:
                catalog_version = _validate_owner_schema(
                    catalog,
                    owner="catalog",
                    expected_version=document_catalog_schema.CATALOG_SCHEMA_VERSION,
                    validator=_validate_catalog,
                )
                publications = _catalog_publications(catalog)
                catalog_payload = {
                    "status": "published" if publications else "empty",
                    "schema_version": catalog_version,
                    "publication_count": len(publications),
                    "publications_sha256": _canonical_sha256(
                        [
                            {
                                "generation_id": generation_id,
                                "publication_id": publication_id,
                                "source_kind": source_kind,
                            }
                            for source_kind, generation_id, publication_id in publications
                        ]
                    ),
                }
                source_owner_payload = _source_owner_snapshot(
                    paths,
                    tuple(source_kind for source_kind, _, _ in publications),
                )
                degraded_owners = tuple(
                    str(item["status"])
                    for item in source_owner_payload
                    if item["status"] != ValueOwnerHealth.HEALTHY.value
                )
                if degraded_owners:
                    availability = ValueReviewAvailability.PARTIAL
                    reason = reason or "source_owner_health_incomplete"
                    uncertainties.extend(
                        f"source_owner_health:{status}" for status in degraded_owners
                    )
                if not publications:
                    availability = ValueReviewAvailability.PARTIAL
                    catalog_reason = "catalog_has_no_publications"
                    reason = reason or catalog_reason
                    uncertainties.append(catalog_reason)
        except _StateIncompatibleError as exc:
            availability = ValueReviewAvailability.PARTIAL
            reason = "catalog_state_incompatible"
            catalog_payload = {"status": "incompatible"}
            uncertainties.extend((reason, _error_detail(exc)))
        except (sqlite3.DatabaseError, _StateContractError, OSError, ValueError) as exc:
            availability = ValueReviewAvailability.PARTIAL
            reason = "catalog_state_corrupt" if _is_corrupt_error(exc) else "catalog_state_invalid"
            catalog_payload = {"status": reason.removeprefix("catalog_state_")}
            uncertainties.extend((reason, _error_detail(exc)))
    return _source_snapshot(
        availability,
        reason,
        inventory=inventory_payload,
        catalog=catalog_payload,
        source_owners=source_owner_payload,
        uncertainties=tuple(uncertainties),
    )


def _source_snapshot(
    availability: ValueReviewAvailability,
    reason: str | None,
    *,
    inventory: dict[str, object],
    catalog: dict[str, object],
    source_owners: tuple[dict[str, object], ...],
    uncertainties: tuple[str, ...],
) -> ValueReviewSourceSnapshot:
    payload = {
        "availability": availability.value,
        "catalog": catalog,
        "inventory": inventory,
        "kind": "neocortex_value_review_source_snapshot",
        "reason": reason,
        "schema_version": 1,
        "source_owners": list(source_owners),
    }
    return ValueReviewSourceSnapshot(
        availability=availability,
        reason=reason,
        payload_json=_canonical_json(payload),
        uncertainties=_unique_strings(uncertainties),
    )


def _unavailable(
    reason: str,
    detail: str | None = None,
    *,
    provenance: tuple[ValueProvenance, ...] = (),
) -> ValueObservationLoad:
    uncertainties = () if not detail else (detail,)
    return ValueObservationLoad(
        ValueReviewAvailability.UNAVAILABLE,
        False,
        reason,
        (),
        provenance,
        uncertainties,
    )


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    before = _sqlite_read_snapshot(path)
    confirmed = _sqlite_read_snapshot(path)
    if before != confirmed:
        raise _StateContractError("SQLite owner changed before immutable read")
    _validate_inactive_sidecar_layout(before)
    # ``immutable=1`` ignores the proven-inactive WAL/SHM pair and therefore
    # cannot create or update ``-shm``.  Main and every sidecar are observed
    # twice before opening and once after close; concurrent change fails closed.
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{readonly_sqlite_uri(path)}&immutable=1",
            uri=True,
            timeout=60.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        query_only = connection.execute("PRAGMA query_only").fetchone()
        trusted_schema = connection.execute("PRAGMA trusted_schema").fetchone()
        if (
            foreign_keys is None
            or int(foreign_keys[0]) != 1
            or query_only is None
            or int(query_only[0]) != 1
            or trusted_schema is None
            or int(trusted_schema[0]) != 0
        ):
            raise _StateContractError("SQLite read-only safeguards could not be enabled")
        yield connection
    finally:
        if connection is not None:
            connection.close()
        after = _sqlite_read_snapshot(path)
        if before != after:
            raise _StateContractError("SQLite owner changed during immutable read")


def _sqlite_read_snapshot(path: Path) -> _SQLiteReadSnapshot:
    main = _sqlite_file_identity(path, label="SQLite owner")
    sidecars: list[tuple[str, _SQLiteFileIdentity]] = []
    for suffix in ("-journal", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        try:
            identity = _sqlite_file_identity(candidate, label=f"SQLite sidecar {suffix}")
        except FileNotFoundError:
            continue
        sidecars.append((suffix, identity))
    return _SQLiteReadSnapshot(main=main, sidecars=tuple(sidecars))


def _sqlite_file_identity(path: Path, *, label: str) -> _SQLiteFileIdentity:
    try:
        value = path.stat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _StateContractError(f"{label} cannot be inspected: {path.name}") from exc
    if not stat.S_ISREG(value.st_mode):
        raise _StateContractError(f"{label} is not a regular file: {path.name}")
    return _SQLiteFileIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        mode=int(value.st_mode),
        size=int(value.st_size),
        mtime_ns=int(value.st_mtime_ns),
        ctime_ns=int(value.st_ctime_ns),
    )


def _validate_inactive_sidecar_layout(snapshot: _SQLiteReadSnapshot) -> None:
    sidecars = dict(snapshot.sidecars)
    journal = sidecars.get("-journal")
    wal = sidecars.get("-wal")
    shm = sidecars.get("-shm")
    if journal is not None and journal.size > 0:
        raise _StateContractError("SQLite owner has a non-empty rollback journal")
    if wal is not None and wal.size > 0:
        raise _StateContractError("SQLite owner has a non-empty WAL")
    if not sidecars:
        return
    if (
        set(sidecars) == {"-wal", "-shm"}
        and wal is not None
        and wal.size == 0
        and shm is not None
        and shm.size == _INACTIVE_SHM_SIZE_BYTES
    ):
        return
    raise _StateContractError("SQLite owner sidecars are not a proven-inactive layout")


def _validate_owner_schema(
    connection: sqlite3.Connection,
    *,
    owner: str,
    expected_version: int,
    validator: Callable[[sqlite3.Connection], None],
) -> int:
    observed = read_application_schema_version(connection, label=owner)
    if observed != expected_version:
        raise _StateIncompatibleError(
            f"{owner} schema {observed!r} is incompatible with {expected_version}"
        )
    validator(connection)
    return observed


def _validate_catalog(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        document_catalog_schema.document_catalog_schema_contract(),
        label="document catalog",
        exact=True,
    )


def _inventory_heads(connection: sqlite3.Connection) -> tuple[_InventoryHead, ...]:
    rows = connection.execute(
        """SELECT c.root,c.scan_id,c.updated_ns,s.scan_id AS matched_scan_id,
        s.status AS scan_status,p.group_count,p.redundant_files,
        p.reclaimable_bytes,p.completed_ns
        FROM inventory_checkpoints c
        LEFT JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root
        LEFT JOIN duplicate_plan_summaries p ON p.scan_id=c.scan_id
        WHERE c.valid=1 ORDER BY c.root COLLATE BINARY LIMIT ?""",
        (MAX_PUBLISHED_HEADS + 1,),
    ).fetchall()
    if len(rows) > MAX_PUBLISHED_HEADS:
        raise _StateContractError("published inventory head limit exceeded")
    heads: list[_InventoryHead] = []
    orphan_members = int(
        connection.execute(
            """SELECT COUNT(*) FROM planned_duplicate_members m
            LEFT JOIN planned_duplicate_groups g ON g.group_id=m.group_id
            WHERE g.group_id IS NULL"""
        ).fetchone()[0]
    )
    for row in rows:
        if row["matched_scan_id"] is None or str(row["scan_status"]) != "complete":
            raise _StateContractError(
                "valid inventory checkpoint does not point to its complete scan"
            )
        scan_id = int(row["scan_id"])
        plan_available = row["completed_ns"] is not None
        plan_valid = False
        completed_ns: int | None = None
        if plan_available:
            completed_ns = int(row["completed_ns"])
            plan_valid = orphan_members == 0 and _duplicate_plan_is_valid(
                connection,
                scan_id,
                expected_group_count=int(row["group_count"]),
                expected_redundant_files=int(row["redundant_files"]),
                expected_reclaimable_bytes=int(row["reclaimable_bytes"]),
                completed_ns=completed_ns,
            )
        else:
            dangling_groups = int(
                connection.execute(
                    "SELECT COUNT(*) FROM planned_duplicate_groups WHERE scan_id=?",
                    (scan_id,),
                ).fetchone()[0]
            )
            plan_valid = dangling_groups == 0
        heads.append(
            _InventoryHead(
                root=str(row["root"]),
                scan_id=scan_id,
                updated_ns=int(row["updated_ns"]),
                publication_id=f"inventory-scan:{scan_id}",
                plan_available=plan_available,
                plan_valid=plan_valid,
                plan_completed_ns=completed_ns,
            )
        )
    return tuple(heads)


def _duplicate_plan_is_valid(
    connection: sqlite3.Connection,
    scan_id: int,
    *,
    expected_group_count: int,
    expected_redundant_files: int,
    expected_reclaimable_bytes: int,
    completed_ns: int,
) -> bool:
    if (
        min(
            expected_group_count,
            expected_redundant_files,
            expected_reclaimable_bytes,
            completed_ns,
        )
        < 0
    ):
        return False
    aggregate = connection.execute(
        """SELECT COUNT(*) AS group_count,
        COALESCE(SUM(redundant_count),0) AS redundant_files,
        COALESCE(SUM(reclaimable_bytes),0) AS reclaimable_bytes
        FROM planned_duplicate_groups WHERE scan_id=?""",
        (scan_id,),
    ).fetchone()
    if aggregate is None or (
        int(aggregate["group_count"]) != expected_group_count
        or int(aggregate["redundant_files"]) != expected_redundant_files
        or int(aggregate["reclaimable_bytes"]) != expected_reclaimable_bytes
    ):
        return False
    invalid_groups = int(
        connection.execute(
            """SELECT COUNT(*) FROM planned_duplicate_groups g
            WHERE g.scan_id=? AND (
                g.size<0 OR g.redundant_count<1
                OR g.reclaimable_bytes<>g.size*g.redundant_count
                OR length(g.full_fingerprint)<>32
                OR g.full_fingerprint GLOB '*[^0-9a-f]*'
                OR (SELECT COUNT(*) FROM planned_duplicate_members m
                    WHERE m.group_id=g.group_id)<>g.redundant_count+1
                OR (SELECT COUNT(DISTINCT m.member_order)
                    FROM planned_duplicate_members m
                    WHERE m.group_id=g.group_id)<>g.redundant_count+1
                OR (SELECT COUNT(*) FROM planned_duplicate_members m
                    WHERE m.group_id=g.group_id AND m.role='keep'
                    AND m.member_order=0 AND m.path=g.keep_path)=0
                OR (SELECT COUNT(*) FROM planned_duplicate_members m
                    WHERE m.group_id=g.group_id AND m.role='keep')<>1
                OR (SELECT COUNT(*) FROM planned_duplicate_members m
                    WHERE m.group_id=g.group_id AND m.role='redundant')<>g.redundant_count
                OR (SELECT COUNT(*) FROM planned_duplicate_members m
                    WHERE m.group_id=g.group_id AND NOT (
                        (m.role='keep' AND m.member_order=0)
                        OR (m.role='redundant' AND m.member_order BETWEEN 1 AND g.redundant_count)
                    ))<>0
            )""",
            (scan_id,),
        ).fetchone()[0]
    )
    if invalid_groups:
        return False
    invalid_snapshots = int(
        connection.execute(
            """SELECT COUNT(*) FROM planned_duplicate_groups g
            JOIN planned_duplicate_members m ON m.group_id=g.group_id
            LEFT JOIN files f ON f.scan_id=g.scan_id AND f.path=m.path
            AND f.volume_id=m.volume_id AND f.file_id=m.file_id
            AND f.size=m.size AND f.mtime_ns=m.mtime_ns
            AND f.birthtime_ns=m.birthtime_ns
            WHERE g.scan_id=? AND (f.path IS NULL OR m.size<>g.size)""",
            (scan_id,),
        ).fetchone()[0]
    )
    return invalid_snapshots == 0


def _inventory_files(
    connection: sqlite3.Connection,
    query: ValueReviewQuery,
    heads: tuple[_InventoryHead, ...],
) -> tuple[_InventoryFile, ...] | None:
    if not heads:
        return ()
    clauses, parameters = _inventory_file_filters(query)
    rows = connection.execute(
        f"""SELECT c.root,c.scan_id,c.updated_ns,f.path,f.volume_id,f.file_id,
        f.size,f.mtime_ns,f.birthtime_ns
        FROM inventory_checkpoints c
        JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root
        JOIN files f ON f.scan_id=c.scan_id
        WHERE {" AND ".join(clauses)}
        ORDER BY f.path COLLATE BINARY,c.updated_ns DESC,c.scan_id DESC,c.root COLLATE BINARY
        LIMIT ?""",
        (*parameters, MAX_SQLITE_CANDIDATES + 1),
    ).fetchall()
    if len(rows) > MAX_SQLITE_CANDIDATES:
        return None
    return _inventory_files_from_rows(rows, heads)


def _inventory_file_page(
    connection: sqlite3.Connection,
    query: ValueReviewQuery,
    heads: tuple[_InventoryHead, ...],
    *,
    after: ValueReviewPageCursor | None,
    page_size: int,
) -> tuple[tuple[_InventoryFile, ...], ValueReviewPageCursor | None]:
    if not heads:
        return (), None
    clauses, parameters = _inventory_file_filters(query)
    if after is not None:
        after_volume_id = bytes.fromhex(after.volume_id_hex)
        after_file_id = bytes.fromhex(after.file_id_hex)
        clauses.append(
            """(
            f.volume_id>? OR
            (f.volume_id=? AND f.file_id>?) OR
            (f.volume_id=? AND f.file_id=? AND f.birthtime_ns>?)
            )"""
        )
        parameters.extend(
            (
                after_volume_id,
                after_volume_id,
                after_file_id,
                after_volume_id,
                after_file_id,
                after.birthtime_ns,
            )
        )
    identity_rows = connection.execute(
        f"""SELECT f.volume_id,f.file_id,f.birthtime_ns
        FROM inventory_checkpoints c
        JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root
        JOIN files f ON f.scan_id=c.scan_id
        WHERE {" AND ".join(clauses)}
        GROUP BY f.volume_id,f.file_id,f.birthtime_ns
        ORDER BY f.volume_id,f.file_id,f.birthtime_ns
        LIMIT ?""",
        (*parameters, page_size + 1),
    ).fetchall()
    has_more = len(identity_rows) > page_size
    selected_identities = identity_rows[:page_size]
    if not selected_identities:
        return (), None

    rows: list[sqlite3.Row] = []
    for identity_batch in _batches(
        tuple(selected_identities),
        _KEYSET_IDENTITY_BATCH,
    ):
        selected_clauses, selected_parameters = _inventory_file_filters(query)
        identity_clauses: list[str] = []
        for row in identity_batch:
            volume_blob, _ = _identity_blob(row["volume_id"])
            file_blob, _ = _identity_blob(row["file_id"])
            identity_clauses.append("(f.volume_id=? AND f.file_id=? AND f.birthtime_ns=?)")
            selected_parameters.extend((volume_blob, file_blob, int(row["birthtime_ns"])))
        selected_clauses.append(f"({' OR '.join(identity_clauses)})")
        rows.extend(
            connection.execute(
                f"""SELECT c.root,c.scan_id,c.updated_ns,f.path,f.volume_id,f.file_id,
                f.size,f.mtime_ns,f.birthtime_ns
                FROM inventory_checkpoints c
                JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root
                JOIN files f ON f.scan_id=c.scan_id
                WHERE {" AND ".join(selected_clauses)}
                ORDER BY f.volume_id,f.file_id,f.birthtime_ns,
                c.updated_ns DESC,c.scan_id DESC,c.root COLLATE BINARY,
                f.path COLLATE BINARY""",
                tuple(selected_parameters),
            ).fetchall()
        )
    files = _inventory_files_from_rows(rows, heads)
    if len(files) != len(selected_identities):
        raise _StateContractError("published inventory keyset page lost an identity")
    cursor_after: ValueReviewPageCursor | None = None
    if has_more:
        last = selected_identities[-1]
        volume_blob, _ = _identity_blob(last["volume_id"])
        file_blob, _ = _identity_blob(last["file_id"])
        cursor_after = ValueReviewPageCursor(
            volume_id_hex=volume_blob.hex().upper(),
            file_id_hex=file_blob.hex().upper(),
            birthtime_ns=int(last["birthtime_ns"]),
        )
    return files, cursor_after


def _inventory_file_filters(
    query: ValueReviewQuery,
) -> tuple[list[str], list[object]]:
    clauses = ["c.valid=1", "s.status='complete'"]
    parameters: list[object] = []
    if query.scope is not None:
        requested_scope = query.scope
        windows = _is_windows_path(requested_scope)
        separator = "\\" if windows else "/"
        if windows:
            scope = requested_scope.rstrip("/\\")
            prefix = scope + separator
        else:
            scope = requested_scope.rstrip("/") or "/"
            prefix = "/" if scope == "/" else scope + "/"
        collation = "NOCASE" if windows else "BINARY"
        clauses.append(
            f"(f.path=? COLLATE {collation} OR substr(f.path,1,?)=? COLLATE {collation})"
        )
        parameters.extend((scope, len(prefix), prefix))
    if query.minimum_size_bytes is not None:
        clauses.append("f.size>=?")
        parameters.append(query.minimum_size_bytes)
    if query.maximum_size_bytes is not None:
        clauses.append("f.size<=?")
        parameters.append(query.maximum_size_bytes)
    if query.extensions:
        extension_clauses: list[str] = []
        for extension in query.extensions:
            normalized = extension.strip().casefold()
            if not normalized.startswith("."):
                normalized = f".{normalized}"
            extension_clauses.append("lower(substr(f.path,-?))=?")
            parameters.extend((len(normalized), normalized))
        clauses.append(f"({' OR '.join(extension_clauses)})")
    return clauses, parameters


def _inventory_files_from_rows(
    rows: list[sqlite3.Row],
    heads: tuple[_InventoryHead, ...],
) -> tuple[_InventoryFile, ...]:
    heads_by_key = {(head.root, head.scan_id): head for head in heads}
    grouped: dict[str, list[_InventoryFile]] = defaultdict(list)
    for row in rows:
        head = heads_by_key.get((str(row["root"]), int(row["scan_id"])))
        if head is None:
            raise _StateContractError("inventory row is not owned by an observed publication")
        path = str(row["path"])
        if not path or not _path_within(path, head.root):
            raise _StateContractError("published inventory path lies outside its scan root")
        volume_blob, volume_id = _identity_blob(row["volume_id"])
        file_blob, file_id = _identity_blob(row["file_id"])
        size = int(row["size"])
        mtime_ns = int(row["mtime_ns"])
        birthtime_ns = int(row["birthtime_ns"])
        if size < 0 or birthtime_ns < -1:
            raise _StateContractError("published inventory file metadata is invalid")
        value = _InventoryFile(
            head,
            path,
            volume_blob,
            file_blob,
            volume_id,
            file_id,
            size,
            mtime_ns,
            birthtime_ns,
        )
        grouped[value.resource_id].append(value)
    chosen: list[_InventoryFile] = []
    for resource_id in sorted(grouped):
        values = grouped[resource_id]
        values.sort(key=lambda item: (-item.head.updated_ns, -item.head.scan_id, item.path))
        first = values[0]
        snapshots = {(item.path, item.size, item.mtime_ns, item.birthtime_ns) for item in values}
        chosen.append(replace(first, conflicting_publication=len(snapshots) > 1))
    return tuple(chosen)


def _duplicate_facts(
    connection: sqlite3.Connection,
    files: tuple[_InventoryFile, ...],
) -> tuple[dict[str, _DuplicateFact], tuple[str, ...]]:
    facts: dict[str, _DuplicateFact] = {}
    uncertainties: list[str] = []
    candidates_by_scan: dict[int, list[_InventoryFile]] = defaultdict(list)
    for value in files:
        if value.conflicting_publication:
            uncertainties.append(f"inventory_publication_conflict:{value.resource_id}")
            continue
        if value.head.plan_available and value.head.plan_valid:
            candidates_by_scan[value.head.scan_id].append(value)
        elif value.head.plan_available and not value.head.plan_valid:
            uncertainties.append(f"duplicate_plan_invalid:{value.head.publication_id}")
        elif not value.head.plan_available:
            uncertainties.append(f"duplicate_plan_unavailable:{value.head.publication_id}")
    for scan_id, candidates in sorted(candidates_by_scan.items()):
        by_snapshot = {
            (
                value.path,
                value.volume_blob,
                value.file_blob,
                value.size,
                value.mtime_ns,
                value.birthtime_ns,
            ): value
            for value in candidates
        }
        matched_groups: dict[str, list[int]] = defaultdict(list)
        for batch in _batches(tuple(candidates), _SQLITE_BATCH):
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""SELECT m.group_id,m.path,m.volume_id,m.file_id,m.size,m.mtime_ns,
                m.birthtime_ns FROM planned_duplicate_members m
                JOIN planned_duplicate_groups g ON g.group_id=m.group_id
                WHERE g.scan_id=? AND m.path IN ({placeholders})
                ORDER BY m.path COLLATE BINARY,m.group_id""",
                (scan_id, *(value.path for value in batch)),
            ).fetchall()
            for row in rows:
                key = (
                    str(row["path"]),
                    bytes(row["volume_id"]),
                    bytes(row["file_id"]),
                    int(row["size"]),
                    int(row["mtime_ns"]),
                    int(row["birthtime_ns"]),
                )
                candidate_match = by_snapshot.get(key)
                if candidate_match is not None:
                    matched_groups[candidate_match.resource_id].append(int(row["group_id"]))
        group_ids = sorted({group for groups in matched_groups.values() for group in groups})
        valid_groups = _load_valid_duplicate_groups(connection, scan_id, tuple(group_ids))
        for resource_id, groups in matched_groups.items():
            unique_groups = sorted(set(groups))
            if len(unique_groups) != 1 or unique_groups[0] not in valid_groups:
                uncertainties.append(f"duplicate_membership_ambiguous:{resource_id}")
                continue
            group_id = unique_groups[0]
            group = valid_groups[group_id]
            member = next(item for item in group.members if item.resource_id == resource_id)
            if group.keeper_resource_id is None:
                uncertainties.append(f"duplicate_keeper_missing:{resource_id}")
                continue
            facts[resource_id] = _DuplicateFact(
                role=member.role,
                full_fingerprint=group.full_fingerprint,
                group_id=str(group_id),
                keeper_resource_id=group.keeper_resource_id,
            )
    return facts, _unique_strings(tuple(uncertainties))


def _load_valid_duplicate_groups(
    connection: sqlite3.Connection,
    scan_id: int,
    group_ids: tuple[int, ...],
) -> dict[int, _DuplicateGroup]:
    if not group_ids:
        return {}
    grouped: dict[int, _DuplicateGroup] = {}
    for batch in _batches(group_ids, _SQLITE_BATCH):
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT g.group_id,g.size AS group_size,g.keep_path,g.redundant_count,
            g.reclaimable_bytes,g.full_fingerprint,m.member_order,m.role,m.path,
            m.volume_id,m.file_id,m.size,m.mtime_ns,m.birthtime_ns
            FROM planned_duplicate_groups g
            JOIN planned_duplicate_members m ON m.group_id=g.group_id
            WHERE g.scan_id=? AND g.group_id IN ({placeholders})
            ORDER BY g.group_id,m.member_order""",
            (scan_id, *batch),
        ).fetchall()
        for row in rows:
            group_id = int(row["group_id"])
            group = grouped.setdefault(
                group_id,
                _DuplicateGroup(
                    group_size=int(row["group_size"]),
                    keep_path=str(row["keep_path"]),
                    redundant_count=int(row["redundant_count"]),
                    reclaimable_bytes=int(row["reclaimable_bytes"]),
                    full_fingerprint=str(row["full_fingerprint"]),
                    members=[],
                ),
            )
            _, volume_id = _identity_blob(row["volume_id"])
            _, file_id = _identity_blob(row["file_id"])
            group.members.append(
                _DuplicateMember(
                    order=int(row["member_order"]),
                    role=str(row["role"]),
                    path=str(row["path"]),
                    size=int(row["size"]),
                    resource_id=_resource_id(
                        volume_id,
                        file_id,
                        int(row["birthtime_ns"]),
                    ),
                )
            )
    valid: dict[int, _DuplicateGroup] = {}
    for group_id, group in grouped.items():
        members = group.members
        redundant_count = group.redundant_count
        keepers = [item for item in members if item.role == "keep"]
        orders = [item.order for item in members]
        if (
            len(members) != redundant_count + 1
            or orders != list(range(redundant_count + 1))
            or len(keepers) != 1
            or keepers[0].order != 0
            or keepers[0].path != group.keep_path
            or any(item.size != group.group_size for item in members)
            or not _valid_full_fingerprint(group.full_fingerprint)
        ):
            continue
        group.keeper_resource_id = keepers[0].resource_id
        valid[group_id] = group
    return valid


def _catalog_publications(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, int, str], ...]:
    rows = connection.execute(
        """SELECT p.source_kind,p.generation_id,p.published_ns,g.status,
        g.source_kind AS generation_source_kind
        FROM catalog_publications p
        LEFT JOIN catalog_generations g ON g.generation_id=p.generation_id
        ORDER BY p.source_kind LIMIT ?""",
        (MAX_PUBLISHED_HEADS + 1,),
    ).fetchall()
    if len(rows) > MAX_PUBLISHED_HEADS:
        raise _StateContractError("catalog publication head limit exceeded")
    result: list[tuple[str, int, str]] = []
    for row in rows:
        source_kind = str(row["source_kind"])
        generation_id = int(row["generation_id"])
        if row["generation_source_kind"] is None:
            raise _StateContractError("catalog publication generation is missing")
        if str(row["generation_source_kind"]) != source_kind or str(row["status"]) != "published":
            raise _StateContractError(
                "catalog publication does not point to its published generation"
            )
        result.append((source_kind, generation_id, f"catalog:{source_kind}:{generation_id}"))
    return tuple(result)


def _catalog_records(
    connection: sqlite3.Connection,
    files: tuple[_InventoryFile, ...],
) -> tuple[dict[str, _CatalogRecord], set[str]]:
    if not files:
        return {}, set()
    by_path: dict[str, list[_InventoryFile]] = defaultdict(list)
    for value in files:
        by_path[_path_key(value.path)].append(value)
    exact: dict[str, list[_CatalogRecord]] = defaultdict(list)
    mismatched: set[str] = set()
    for batch in _batches(files, _SQLITE_BATCH):
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT d.generation_id,d.source_kind,d.file_key,d.path,d.volume_id,
            d.file_id,d.size,d.mtime_ns,d.birthtime_ns,d.source_status,
            d.text_fingerprint,d.primary_project,d.catalog_status,d.uncertainty,
            d.error_type,d.error_message
            FROM catalog_publications p
            JOIN catalog_generations g ON g.generation_id=p.generation_id
            AND g.source_kind=p.source_kind AND g.status='published'
            JOIN catalog_generation_documents d ON d.generation_id=p.generation_id
            AND d.source_kind=p.source_kind
            WHERE d.active=1 AND d.path IN ({placeholders})
            ORDER BY d.path COLLATE BINARY,d.source_kind""",
            tuple(value.path for value in batch),
        ).fetchall()
        for row in rows:
            candidates = by_path.get(_path_key(str(row["path"])), ())
            record = _catalog_record(row)
            matched = next((value for value in candidates if _catalog_matches(record, value)), None)
            if matched is None:
                mismatched.update(value.resource_id for value in candidates)
            else:
                exact[matched.resource_id].append(record)
    selected: dict[str, _CatalogRecord] = {}
    for resource_id, values in exact.items():
        unique = {(value.source_kind, value.file_key, value.generation_id) for value in values}
        if len(unique) != 1:
            mismatched.add(resource_id)
            continue
        selected[resource_id] = values[0]
    return selected, mismatched


def _catalog_record(row: sqlite3.Row) -> _CatalogRecord:
    source_kind = str(row["source_kind"])
    generation_id = int(row["generation_id"])
    return _CatalogRecord(
        publication_id=f"catalog:{source_kind}:{generation_id}",
        generation_id=generation_id,
        source_kind=source_kind,
        file_key=str(row["file_key"]),
        path=str(row["path"]),
        volume_id=str(row["volume_id"]),
        file_id=str(row["file_id"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
        birthtime_ns=int(row["birthtime_ns"]),
        source_status=str(row["source_status"]),
        text_fingerprint=None if row["text_fingerprint"] is None else str(row["text_fingerprint"]),
        primary_project=None if row["primary_project"] is None else str(row["primary_project"]),
        catalog_status=str(row["catalog_status"]),
        catalog_uncertainty=str(row["uncertainty"]),
        error_type=None if row["error_type"] is None else str(row["error_type"]),
        error_message=None if row["error_message"] is None else str(row["error_message"]),
    )


def _catalog_matches(record: _CatalogRecord, value: _InventoryFile) -> bool:
    return (
        _same_path(record.path, value.path)
        and record.volume_id == str(value.volume_id)
        and record.file_id == str(value.file_id)
        and record.size == value.size
        and record.mtime_ns == value.mtime_ns
        and record.birthtime_ns == value.birthtime_ns
    )


def _text_fingerprint_counts(
    connection: sqlite3.Connection,
    fingerprints: tuple[str | None, ...],
) -> dict[str, int]:
    wanted_values: set[str] = set()
    for value in fingerprints:
        if value is not None and _valid_text_fingerprint(value):
            wanted_values.add(value)
    wanted = tuple(sorted(wanted_values))
    result: dict[str, int] = {}
    for batch in _batches(wanted, _SQLITE_BATCH):
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT text_fingerprint,COUNT(*) AS resource_count FROM (
                SELECT d.text_fingerprint,d.volume_id,d.file_id,d.birthtime_ns
                FROM catalog_publications p
                JOIN catalog_generations g ON g.generation_id=p.generation_id
                AND g.source_kind=p.source_kind AND g.status='published'
                JOIN catalog_generation_documents d ON d.generation_id=p.generation_id
                AND d.source_kind=p.source_kind
                WHERE d.active=1 AND d.catalog_status IN ('classified','review')
                AND d.source_status IN ('complete','done')
                AND d.text_fingerprint IN ({placeholders})
                GROUP BY d.text_fingerprint,d.volume_id,d.file_id,d.birthtime_ns
            ) GROUP BY text_fingerprint ORDER BY text_fingerprint""",
            batch,
        ).fetchall()
        for row in rows:
            result[str(row["text_fingerprint"])] = int(row["resource_count"])
    return result


def _source_owner_snapshot(
    paths: ValueReviewPaths,
    source_kinds: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    """Capture the bounded owner-schema facts that can change Value ranking."""

    result: list[dict[str, object]] = []
    for source_kind in sorted(set(source_kinds)):
        spec = _owner_spec(paths, source_kind)
        if spec is None or spec.path is None:
            result.append(
                {
                    "owner": source_kind if spec is None else spec.owner,
                    "path_status": "unavailable",
                    "schema_version": None,
                    "source_kind": source_kind,
                    "status": ValueOwnerHealth.UNKNOWN.value,
                }
            )
            continue
        path_status = _path_status(spec.path)
        if path_status != "file":
            result.append(
                {
                    "owner": spec.owner,
                    "path_status": path_status,
                    "schema_version": None,
                    "source_kind": source_kind,
                    "status": ValueOwnerHealth.UNKNOWN.value,
                }
            )
            continue
        try:
            with _readonly_connection(spec.path) as connection:
                version = _validate_owner_schema(
                    connection,
                    owner=spec.owner,
                    expected_version=spec.expected_version,
                    validator=spec.validator,
                )
        except _StateIncompatibleError:
            health = ValueOwnerHealth.INCOMPATIBLE
            version = None
        except (sqlite3.DatabaseError, _StateContractError, OSError, ValueError) as exc:
            health = ValueOwnerHealth.CORRUPT if _is_corrupt_error(exc) else ValueOwnerHealth.FAILED
            version = None
        else:
            health = ValueOwnerHealth.HEALTHY
        result.append(
            {
                "owner": spec.owner,
                "path_status": path_status,
                "schema_version": version,
                "source_kind": source_kind,
                "status": health.value,
            }
        )
    return tuple(result)


def _inspect_source_owners(
    paths: ValueReviewPaths,
    records: tuple[_CatalogRecord, ...],
) -> dict[str, tuple[ValueOwnerHealth, ValueEvidence | None, ValueProvenance | None]]:
    source_kinds = tuple(sorted({record.source_kind for record in records}))
    result: dict[str, tuple[ValueOwnerHealth, ValueEvidence | None, ValueProvenance | None]] = {}
    for source_kind in source_kinds:
        spec = _owner_spec(paths, source_kind)
        if spec is None or spec.path is None:
            owner = source_kind if spec is None else spec.owner
            result[source_kind] = (
                ValueOwnerHealth.UNKNOWN,
                _owner_health_evidence(
                    owner,
                    ValueOwnerHealth.UNKNOWN,
                    "owner_path_unavailable",
                ),
                None,
            )
            continue
        status = _path_status(spec.path)
        if status != "file":
            result[source_kind] = (
                ValueOwnerHealth.UNKNOWN,
                _owner_health_evidence(
                    spec.owner,
                    ValueOwnerHealth.UNKNOWN,
                    f"owner_state_{status}",
                ),
                None,
            )
            continue
        try:
            with _readonly_connection(spec.path) as connection:
                version = _validate_owner_schema(
                    connection,
                    owner=spec.owner,
                    expected_version=spec.expected_version,
                    validator=spec.validator,
                )
        except _StateIncompatibleError as exc:
            result[source_kind] = (
                ValueOwnerHealth.INCOMPATIBLE,
                _owner_health_evidence(
                    spec.owner,
                    ValueOwnerHealth.INCOMPATIBLE,
                    _error_detail(exc),
                ),
                None,
            )
            continue
        except (sqlite3.DatabaseError, _StateContractError, OSError, ValueError) as exc:
            health = ValueOwnerHealth.CORRUPT if _is_corrupt_error(exc) else ValueOwnerHealth.FAILED
            result[source_kind] = (
                health,
                _owner_health_evidence(spec.owner, health, _error_detail(exc)),
                None,
            )
            continue
        publication_id = f"owner-schema:{spec.owner}:{version}"
        provenance = ValueProvenance(spec.owner, version, publication_id)
        evidence = ValueEvidence(
            evidence_id=f"owner-health:{spec.owner}:{version}",
            owner=spec.owner,
            kind="owner_schema_health",
            strength=ValueEvidenceStrength.STRONG,
            publication_id=publication_id,
            facts=(
                ValueEvidenceFact("schema_version", str(version)),
                ValueEvidenceFact("health", ValueOwnerHealth.HEALTHY.value),
            ),
        )
        result[source_kind] = (ValueOwnerHealth.HEALTHY, evidence, provenance)
    return result


def _owner_health_evidence(
    owner: str,
    health: ValueOwnerHealth,
    reason: str,
) -> ValueEvidence:
    return ValueEvidence(
        evidence_id=f"owner-health:{owner}:{health.value}",
        owner=owner,
        kind="owner_schema_health",
        strength=ValueEvidenceStrength.STRONG,
        facts=(
            ValueEvidenceFact("health", health.value),
            ValueEvidenceFact("reason", reason[:500]),
        ),
    )


def _owner_spec(paths: ValueReviewPaths, source_kind: str) -> _OwnerSpec | None:
    def schema_validator(
        contract: Callable[[], SQLiteSchemaContract], label: str
    ) -> Callable[[sqlite3.Connection], None]:
        return lambda connection: validate_sqlite_schema_contract(
            connection,
            contract(),
            label=label,
            exact=True,
        )

    specs = {
        "pdf": _OwnerSpec("pdf", PDF_SCHEMA_VERSION, paths.pdf, validate_pdf_schema),
        "docx": _OwnerSpec("docx", DOCX_SCHEMA_VERSION, paths.docx, validate_docx_schema),
        "text": _OwnerSpec(
            "text",
            text_state.TEXT_SCHEMA_VERSION,
            paths.text,
            schema_validator(text_state.text_schema_contract, "text"),
        ),
        "audio": _OwnerSpec(
            "audio",
            audio_state.AUDIO_SCHEMA_VERSION,
            paths.audio,
            schema_validator(audio_state._audio_schema_contract, "audio state"),
        ),
    }
    if source_kind in {"xlsx", "pptx", "odt"}:
        return _OwnerSpec(
            "office",
            office_state.OFFICE_SCHEMA_VERSION,
            paths.office,
            schema_validator(office_state._office_schema_contract, "Office state"),
        )
    return specs.get(source_kind)


def _observation(
    value: _InventoryFile,
    duplicate: _DuplicateFact | None,
    catalog: _CatalogRecord | None,
    catalog_mismatch: bool,
    text_counts: dict[str, int],
    owner_health_by_source: dict[
        str, tuple[ValueOwnerHealth, ValueEvidence | None, ValueProvenance | None]
    ],
    *,
    inventory_version: int,
    catalog_failure_health: ValueOwnerHealth | None,
) -> ValueFileObservation:
    evidence: list[ValueEvidence] = []
    provenance: list[ValueProvenance] = [
        ValueProvenance("inventory", inventory_version, value.head.publication_id)
    ]
    uncertainties: list[str] = []
    inventory_evidence = ValueEvidence(
        evidence_id=f"inventory:{value.head.scan_id}:{value.resource_id}",
        owner="inventory",
        kind="published_inventory_record",
        strength=ValueEvidenceStrength.STRONG,
        publication_id=value.head.publication_id,
        record_id=value.resource_id,
        facts=(
            ValueEvidenceFact("path", value.path),
            ValueEvidenceFact("size_bytes", str(value.size)),
            ValueEvidenceFact("mtime_ns", str(value.mtime_ns)),
            ValueEvidenceFact("birthtime_ns", str(value.birthtime_ns)),
        ),
    )
    evidence.append(inventory_evidence)
    if value.conflicting_publication:
        uncertainties.append("published_inventory_identity_conflict")

    if duplicate is not None:
        duplicate_evidence = ValueEvidence(
            evidence_id=f"duplicate-plan:{value.head.scan_id}:{duplicate.group_id}:{value.resource_id}",
            owner="inventory",
            kind="exact_duplicate_plan",
            strength=ValueEvidenceStrength.STRONG,
            publication_id=value.head.publication_id,
            record_id=value.resource_id,
            facts=(
                ValueEvidenceFact("full_fingerprint", duplicate.full_fingerprint),
                ValueEvidenceFact("group_id", duplicate.group_id),
                ValueEvidenceFact("role", duplicate.role),
                ValueEvidenceFact("keeper_resource_id", duplicate.keeper_resource_id),
            ),
        )
        evidence.append(duplicate_evidence)

    health = catalog_failure_health or ValueOwnerHealth.UNKNOWN
    source_kind: str | None = None
    if catalog_failure_health is not None:
        evidence.append(
            ValueEvidence(
                evidence_id=f"catalog-health:{catalog_failure_health.value}",
                owner="catalog",
                kind="owner_schema_health",
                strength=ValueEvidenceStrength.STRONG,
                facts=(
                    ValueEvidenceFact("health", catalog_failure_health.value),
                    ValueEvidenceFact("reason", "catalog_owner_read_failed_closed"),
                ),
            )
        )
    if catalog is not None:
        source_kind = catalog.source_kind
        provenance.append(
            ValueProvenance(
                "catalog",
                document_catalog_schema.CATALOG_SCHEMA_VERSION,
                catalog.publication_id,
            )
        )
        catalog_evidence = ValueEvidence(
            evidence_id=f"catalog:{catalog.generation_id}:{catalog.source_kind}:{catalog.file_key}",
            owner="catalog",
            kind="published_catalog_record",
            strength=ValueEvidenceStrength.MODERATE,
            publication_id=catalog.publication_id,
            record_id=catalog.file_key,
            facts=(
                ValueEvidenceFact("catalog_status", catalog.catalog_status),
                ValueEvidenceFact("source_kind", catalog.source_kind),
                ValueEvidenceFact("source_status", catalog.source_status),
                ValueEvidenceFact("text_fingerprint", catalog.text_fingerprint or "unknown"),
                ValueEvidenceFact("primary_project", catalog.primary_project or "unknown"),
            ),
        )
        evidence.append(catalog_evidence)
        health, health_evidence, health_provenance = owner_health_by_source.get(
            catalog.source_kind,
            (ValueOwnerHealth.UNKNOWN, None, None),
        )
        if health_evidence is not None:
            evidence.append(health_evidence)
        if health_provenance is not None:
            provenance.append(health_provenance)
        health = _record_health(catalog, health)
        evidence.append(
            ValueEvidence(
                evidence_id=(
                    f"extraction-health:{catalog.generation_id}:"
                    f"{catalog.source_kind}:{catalog.file_key}"
                ),
                owner=catalog.source_kind,
                kind="extraction_health",
                strength=ValueEvidenceStrength.STRONG,
                publication_id=catalog.publication_id,
                record_id=catalog.file_key,
                facts=(
                    ValueEvidenceFact("health", health.value),
                    ValueEvidenceFact("source_status", catalog.source_status),
                    ValueEvidenceFact("catalog_status", catalog.catalog_status),
                    ValueEvidenceFact("error_type", catalog.error_type or "none"),
                ),
            )
        )
        if catalog.catalog_status.casefold() == "review":
            uncertainties.append("catalog_classification_requires_review")
        if catalog.text_fingerprint is not None and not _valid_text_fingerprint(
            catalog.text_fingerprint
        ):
            uncertainties.append("catalog_text_fingerprint_invalid")
    if catalog_mismatch:
        health = ValueOwnerHealth.INCOMPATIBLE
        uncertainties.append("catalog_snapshot_mismatch")
        evidence.append(
            ValueEvidence(
                evidence_id=f"catalog-health:mismatch:{value.resource_id}",
                owner="catalog",
                kind="owner_schema_health",
                strength=ValueEvidenceStrength.STRONG,
                record_id=value.resource_id,
                facts=(
                    ValueEvidenceFact("health", ValueOwnerHealth.INCOMPATIBLE.value),
                    ValueEvidenceFact("reason", "published_snapshot_identity_mismatch"),
                ),
            )
        )
    if value.conflicting_publication:
        health = ValueOwnerHealth.INCOMPATIBLE
    text_fingerprint = (
        catalog.text_fingerprint
        if catalog is not None and _valid_text_fingerprint(catalog.text_fingerprint)
        else None
    )
    text_duplicate_count = None if text_fingerprint is None else text_counts.get(text_fingerprint)
    if text_fingerprint is not None and text_duplicate_count is not None and catalog is not None:
        evidence.append(
            ValueEvidence(
                evidence_id=f"catalog-text-count:{text_fingerprint}",
                owner="catalog",
                kind="published_text_fingerprint_count",
                strength=ValueEvidenceStrength.MODERATE,
                publication_id=catalog.publication_id,
                record_id=text_fingerprint,
                facts=(
                    ValueEvidenceFact("text_fingerprint", text_fingerprint),
                    ValueEvidenceFact("resource_count", str(text_duplicate_count)),
                    ValueEvidenceFact("equivalence", "extracted_text_not_bytes"),
                ),
            )
        )
    return ValueFileObservation(
        resource_id=value.resource_id,
        path=value.path,
        size_bytes=value.size,
        mtime_ns=value.mtime_ns,
        birthtime_ns=value.birthtime_ns,
        owner_health=health,
        source_kind=source_kind,
        source_status=None if catalog is None else catalog.source_status,
        catalog_status=None if catalog is None else catalog.catalog_status,
        catalog_uncertainty=None if catalog is None else catalog.catalog_uncertainty,
        primary_project=None if catalog is None else catalog.primary_project,
        text_fingerprint=text_fingerprint,
        text_duplicate_count=text_duplicate_count,
        exact_duplicate_role=None if duplicate is None else duplicate.role,
        exact_duplicate_hash=None if duplicate is None else duplicate.full_fingerprint,
        exact_duplicate_group_id=None if duplicate is None else duplicate.group_id,
        exact_duplicate_keeper_id=None if duplicate is None else duplicate.keeper_resource_id,
        evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
        provenance=_unique_provenance(tuple(provenance)),
        uncertainties=_unique_strings(tuple(uncertainties)),
    )


def _record_health(record: _CatalogRecord, schema_health: ValueOwnerHealth) -> ValueOwnerHealth:
    details = " ".join(
        value for value in (record.error_type, record.error_message) if value
    ).casefold()
    if any(marker in details for marker in ("encrypted", "password", "cipher", "crypto")):
        return ValueOwnerHealth.ENCRYPTED
    if any(marker in details for marker in ("corrupt", "damaged", "malformed", "truncated")):
        return ValueOwnerHealth.CORRUPT
    if record.source_status.casefold() == "partial":
        return ValueOwnerHealth.PARTIAL
    if record.catalog_status.casefold() == "error":
        return ValueOwnerHealth.FAILED
    if record.source_status.casefold() not in {"complete", "done"}:
        return ValueOwnerHealth.FAILED
    return schema_health


def _identity_blob(value: object) -> tuple[bytes, int]:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise _StateContractError("inventory identity is not a BLOB")
    encoded = bytes(value)
    if len(encoded) != 16:
        raise _StateContractError("inventory identity BLOB is not 16 bytes")
    return encoded, int.from_bytes(encoded, "little", signed=False)


def _resource_id(volume_id: int, file_id: int, birthtime_ns: int) -> str:
    return f"resource:file:{volume_id}:{file_id}:{birthtime_ns}"


def _valid_full_fingerprint(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _valid_text_fingerprint(value: str | None) -> bool:
    return value is not None and _valid_full_fingerprint(value)


def _path_status(path: Path) -> str:
    try:
        value = path.stat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "inaccessible"
    return "file" if stat.S_ISREG(value.st_mode) else "not_a_file"


def _path_within(path: str, root: str) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    return path_key == root_key or path_key.startswith(root_key.rstrip("/") + "/")


def _same_path(first: str, second: str) -> bool:
    return _path_key(first) == _path_key(second)


def _path_key(value: str) -> str:
    normalized = re.sub(r"/+", "/", value.replace("\\", "/")).rstrip("/") or "/"
    return normalized.casefold() if _is_windows_path(value) else normalized


def _is_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or "\\" in value


def _batches(values: tuple[_T, ...], size: int) -> Iterator[tuple[_T, ...]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _is_corrupt_error(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return True
    text = str(exc).casefold()
    return any(
        marker in text for marker in ("file is not a database", "database disk image is malformed")
    )


def _error_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}:{str(exc)[:500]}"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def _unique_provenance(values: tuple[ValueProvenance, ...]) -> tuple[ValueProvenance, ...]:
    unique = {
        (value.owner, value.schema_version, value.publication_id, value.read_mode): value
        for value in values
    }
    return tuple(unique[key] for key in sorted(unique))


__all__ = [
    "MAX_VALUE_REVIEW_PAGE_INPUTS",
    "ValueObservationLoad",
    "ValueReviewPageCursor",
    "ValueReviewSourceSnapshot",
    "load_value_review_observation_page",
    "load_value_review_observations",
    "read_value_review_source_snapshot",
]
