"""Disk-backed fingerprint planning and persisted duplicate groups."""

from __future__ import annotations

from neocortex.persistence.operational_freshness import require_operational_identity

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from itertools import islice

from neocortex.platform.policy import sqlite_path_collation

from ..domain.errors import InventoryError
from ..domain.models import (
    VALID_VERIFICATION_MODES,
    DuplicateGroup,
    FileSnapshot,
    VerificationMode,
)
from ..domain.evidence import DedupPolicy, PlanCoverage, DuplicateMemberProof
from ..domain.fingerprint_observation import FingerprintObservation
from ..planning.keeper import KeeperRank
from .plan_evidence import decode_group_proof, decode_member_proof, encode_proof
from .generation import duplicate_plan_digest
from .repository_scans import resolve_scan_id
from .scan import id_blob as _id_blob


_PATH_COLLATION = sqlite_path_collation()
PLANNING_METADATA_BATCH_SIZE = 128
MAX_PLANNING_ALIAS_SAMPLE = 128

type PlanningMemberMetadata = tuple[tuple[str, ...], int, int, bool]


class PlanRepositoryMixin:
    """Own temporary planning state and durable dry-run duplicate plans."""

    _connection: sqlite3.Connection

    def begin_planning_fingerprints(self) -> None:
        """Create disk-spillable temporary tables for one bounded planning run."""

        self._connection.execute("PRAGMA temp_store=FILE")
        self._connection.execute("PRAGMA cache_size=-32768")
        self._connection.executescript(
            f"""
            DROP TABLE IF EXISTS temp.planning_seen;
            DROP TABLE IF EXISTS temp.planning_fingerprints;
            DROP TABLE IF EXISTS temp.planning_observations;
            DROP TABLE IF EXISTS temp.planning_full_observations;
            CREATE TEMP TABLE planning_seen(
                volume_id BLOB NOT NULL,
                file_id BLOB NOT NULL,
                PRIMARY KEY(volume_id,file_id)
            ) WITHOUT ROWID;
            CREATE TEMP TABLE planning_fingerprints(
                stage TEXT NOT NULL,
                digest BLOB NOT NULL,
                path TEXT NOT NULL COLLATE {_PATH_COLLATION},
                volume_id BLOB NOT NULL,
                file_id BLOB NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                computed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(stage,volume_id,file_id)
            ) WITHOUT ROWID;
            CREATE INDEX planning_fingerprint_collision_idx
                ON planning_fingerprints(stage,digest);
            CREATE TEMP TABLE planning_observations(
                path TEXT PRIMARY KEY COLLATE {_PATH_COLLATION},
                volume_id BLOB NOT NULL, file_id BLOB NOT NULL,
                size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, birthtime_ns INTEGER NOT NULL,
                explicit_rank INTEGER NOT NULL, location_rank INTEGER NOT NULL,
                reference_rank INTEGER NOT NULL, name_rank INTEGER NOT NULL,
                identity_rank TEXT NOT NULL, link_count INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE INDEX planning_observation_identity_idx
                ON planning_observations(volume_id,file_id);
            CREATE TEMP TABLE planning_full_observations(
                volume_id BLOB NOT NULL, file_id BLOB NOT NULL,
                path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL, full_digest BLOB, ctime_ns INTEGER NOT NULL,
                PRIMARY KEY(volume_id,file_id)
            ) WITHOUT ROWID;
            """
        )

    def clear_planning_fingerprints(self) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM planning_seen")
            self._connection.execute("DELETE FROM planning_fingerprints")
            self._connection.execute("DELETE FROM planning_observations")
            self._connection.execute("DELETE FROM planning_full_observations")

    def store_planning_full_observations(self, rows: Iterable[FingerprintObservation]) -> None:
        """Spill observed change fences; a sample has no complete digest."""

        with self._connection:
            self._connection.executemany(
                "INSERT OR REPLACE INTO planning_full_observations VALUES(?,?,?,?,?,?,?,?)",
                ((_id_blob(item.snapshot.volume_id), _id_blob(item.snapshot.file_id),
                  item.snapshot.path, item.snapshot.size, item.snapshot.mtime_ns,
                  item.snapshot.birthtime_ns, item.full_digest, item.ctime_ns) for item in rows),
            )

    def planning_full_observation(self, snapshot: FileSnapshot) -> FingerprintObservation | None:
        """Reuse a full digest only while the exact physical observation holds."""

        from ..fingerprinting import FULL_ALGORITHM, require_fingerprint_change_version

        row = self._connection.execute(
            "SELECT full_digest,ctime_ns FROM planning_full_observations "
            "WHERE volume_id=? AND file_id=? AND path=? AND size=? AND mtime_ns=? AND birthtime_ns=?",
            (_id_blob(snapshot.volume_id), _id_blob(snapshot.file_id), snapshot.path,
             snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns),
        ).fetchone()
        if row is None:
            return None
        require_fingerprint_change_version(snapshot, int(row[1]))
        if row[0] is None:
            # The sample is still current, but the survivor must now read
            # full content. A NULL digest is never promoted to full proof.
            return None
        return FingerprintObservation(
            snapshot=snapshot, algorithm=FULL_ALGORITHM, digest=bytes(row[0]),
            full_digest=bytes(row[0]), ctime_ns=int(row[1]), computed=True,
            reused_full_digest=True,
        )

    def planning_observed_change_version(self, snapshot: FileSnapshot) -> int | None:
        """Read the sample fence on the writer thread for a detached worker."""

        row = self._connection.execute(
            "SELECT ctime_ns FROM planning_full_observations "
            "WHERE volume_id=? AND file_id=? AND path=? AND size=? AND mtime_ns=? AND birthtime_ns=?",
            (_id_blob(snapshot.volume_id), _id_blob(snapshot.file_id), snapshot.path,
             snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns),
        ).fetchone()
        return None if row is None else int(row[0])

    def store_planning_observations(
        self, rows: Iterable[tuple[FileSnapshot, KeeperRank, int]],
    ) -> None:
        with self._connection:
            self._connection.executemany(
                "INSERT INTO planning_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (snapshot.path, _id_blob(snapshot.volume_id), _id_blob(snapshot.file_id),
                     snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns,
                     *rank[:5], link_count)
                    for snapshot, rank, link_count in rows
                ),
            )

    def planning_has_multiple_identities(self) -> bool:
        """Find a second physical identity without walking its first aliases.

        Both probes use the temporary identity index. The first identity is
        only a range boundary; no path or fingerprint becomes authority.
        An empty observation set has no boundary and yields no match.
        """

        rows = self._connection.execute(
            """SELECT 1 FROM planning_observations
            WHERE (volume_id,file_id) > (
                SELECT volume_id,file_id FROM planning_observations
                ORDER BY volume_id,file_id LIMIT 1
            ) LIMIT 1"""
        )
        try:
            return rows.fetchone() is not None
        finally:
            rows.close()

    def iter_planning_identities(self) -> Iterator[FileSnapshot]:
        """Choose a preferred observed alias before hashing each object once."""

        rows = self._connection.execute(
            """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns FROM (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY volume_id,file_id ORDER BY
                    explicit_rank,location_rank,reference_rank,name_rank,identity_rank,path) AS ordinal
                FROM planning_observations
            ) WHERE ordinal=1 ORDER BY path"""
        )
        for path, volume, file_id, size, mtime, birth in rows:
            yield FileSnapshot(path, int.from_bytes(volume, "little"),
                               int.from_bytes(file_id, "little"), size, mtime, birth)

    def planning_member_metadata(
        self, snapshot: FileSnapshot, *, alias_limit: int = 128,
    ) -> tuple[tuple[str, ...], int, int, bool]:
        identity = (_id_blob(snapshot.volume_id), _id_blob(snapshot.file_id))
        count, links = self._connection.execute(
            "SELECT COUNT(*),MAX(link_count) FROM planning_observations "
            "WHERE volume_id=? AND file_id=?", identity,
        ).fetchone()
        aliases = tuple(row[0] for row in self._connection.execute(
            "SELECT path FROM planning_observations WHERE volume_id=? AND file_id=? "
            "ORDER BY CASE WHEN path=? THEN 0 ELSE 1 END,path LIMIT ?",
            (*identity, snapshot.path, alias_limit),
        ))
        computed = self._connection.execute(
            "SELECT computed FROM planning_fingerprints WHERE stage='full' "
            "AND volume_id=? AND file_id=?", identity,
        ).fetchone()
        if not count or links is None or computed is None:
            raise InventoryError("duplicate member lacks complete planning observations")
        return aliases, int(count), int(links), bool(computed[0])

    def iter_planning_member_metadata(
        self,
        snapshots: Iterable[FileSnapshot],
        *,
        alias_limit: int = MAX_PLANNING_ALIAS_SAMPLE,
        checkpoint: Callable[[], None] | None = None,
    ) -> Iterator[PlanningMemberMetadata]:
        """Read bounded member batches while their planning observations exist.

        Each request keeps its own preferred alias, even when two requests
        describe the same identity. Counts and link evidence cover every
        observed alias; only the returned path sample is limited. No evidence
        is cached across groups or planning runs.
        """

        if (
            isinstance(alias_limit, bool)
            or not isinstance(alias_limit, int)
            or not 1 <= alias_limit <= MAX_PLANNING_ALIAS_SAMPLE
        ):
            raise ValueError(f"alias_limit must be between 1 and {MAX_PLANNING_ALIAS_SAMPLE}")
        source = iter(snapshots)
        while True:
            if checkpoint is not None:
                checkpoint()
            batch = tuple(islice(source, PLANNING_METADATA_BATCH_SIZE))
            if checkpoint is not None:
                checkpoint()
            if not batch:
                return
            parameters: list[int | bytes | str] = []
            for ordinal, snapshot in enumerate(batch):
                parameters.extend((ordinal, _id_blob(snapshot.volume_id),
                                   _id_blob(snapshot.file_id), snapshot.path))
            values = ",".join("(?,?,?,?)" for _ in batch)
            # The identity index also orders its primary-key path suffix.
            # Sample only the first alias_limit other paths, without sorting
            # every alias. JSON transports bounded values within this read;
            # it does not become persisted evidence or an authority cache.
            rows = self._connection.execute(
                f"""WITH requested(ordinal,volume_id,file_id,preferred_path) AS (
                    VALUES {values}
                ) SELECT r.ordinal,
                    (SELECT json_array(COUNT(*),MAX(link_count)) FROM planning_observations
                        WHERE volume_id=r.volume_id AND file_id=r.file_id),
                    (SELECT computed FROM planning_fingerprints
                        WHERE stage='full' AND volume_id=r.volume_id AND file_id=r.file_id),
                    (SELECT path FROM planning_observations
                        WHERE path=r.preferred_path AND volume_id=r.volume_id AND file_id=r.file_id),
                    (SELECT json_group_array(path) FROM (
                        SELECT path FROM planning_observations
                        WHERE volume_id=r.volume_id AND file_id=r.file_id AND path<>r.preferred_path
                        ORDER BY path LIMIT ?
                    )) FROM requested r""",
                (*parameters, alias_limit),
            )
            try:
                if checkpoint is not None:
                    checkpoint()
                metadata: list[PlanningMemberMetadata | None] = [None] * len(batch)
                for ordinal, counts, computed, preferred, sample in rows:
                    count, links = json.loads(counts)
                    if not count or links is None or computed is None:
                        raise InventoryError("duplicate member lacks complete planning observations")
                    aliases: list[str] = json.loads(sample)
                    if preferred is not None:
                        aliases = [preferred, *aliases[:alias_limit - 1]]
                    if not aliases:
                        raise InventoryError("duplicate member lacks complete planning observations")
                    metadata[ordinal] = (tuple(aliases), int(count), int(links), bool(computed))
                    if checkpoint is not None:
                        checkpoint()
            finally:
                rows.close()
            if checkpoint is not None:
                checkpoint()
            # Avoid ORDER BY on the outer query: SQLite can then hand back a
            # row before evaluating every identity, keeping checks responsive.
            for observation in metadata:
                if observation is None:
                    raise InventoryError("duplicate member lacks complete planning observations")
                yield observation

    def claim_planning_identity(self, snapshot: FileSnapshot) -> bool:
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO planning_seen VALUES(?,?)",
            (_id_blob(snapshot.volume_id), _id_blob(snapshot.file_id)),
        )
        return cursor.rowcount == 1

    def store_planning_fingerprints(
        self,
        stage: str,
        rows: Iterable[tuple[FileSnapshot, bytes]],
        *,
        computed_identities: frozenset[tuple[int, int]] = frozenset(),
    ) -> None:
        with self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO planning_fingerprints(
                stage,digest,path,volume_id,file_id,size,mtime_ns,birthtime_ns,computed)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        stage,
                        digest,
                        snapshot.path,
                        _id_blob(snapshot.volume_id),
                        _id_blob(snapshot.file_id),
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        int(snapshot.identity in computed_identities),
                    )
                    for snapshot, digest in rows
                ),
            )

    def planning_collision_member_count(self, stage: str) -> int:
        row = self._connection.execute(
            """SELECT COALESCE(SUM(member_count),0) FROM(
            SELECT COUNT(*) member_count FROM planning_fingerprints
            WHERE stage=? GROUP BY digest HAVING COUNT(*)>1)""",
            (stage,),
        ).fetchone()
        return int(row[0])

    def iter_planning_collision_members(
        self,
        stage: str,
    ) -> Iterator[tuple[bytes, FileSnapshot]]:
        rows = self._connection.execute(
            f"""SELECT w.digest,w.path,w.volume_id,w.file_id,w.size,w.mtime_ns,
            w.birthtime_ns FROM planning_fingerprints w JOIN(
                SELECT digest FROM planning_fingerprints WHERE stage=?
                GROUP BY digest HAVING COUNT(*)>1
            ) collisions ON collisions.digest=w.digest
            LEFT JOIN planning_observations o ON o.path=w.path
            WHERE w.stage=? ORDER BY w.digest,o.explicit_rank,o.location_rank,
            o.reference_rank,o.name_rank,o.identity_rank,w.path COLLATE {_PATH_COLLATION}""",
            (stage, stage),
        )
        for digest, path, volume, file_id, size, mtime, birth in rows:
            yield (
                bytes(digest),
                FileSnapshot(
                    path,
                    int.from_bytes(volume, "little"),
                    int.from_bytes(file_id, "little"),
                    size,
                    mtime,
                    birth,
                ),
            )

    def begin_duplicate_plan(self, scan_id: int) -> None:
        """Discard any incomplete prior plan for this scan."""

        scan_id = resolve_scan_id(self._connection, scan_id)
        with self._connection:
            scan = self._connection.execute(
                "SELECT status FROM scans WHERE scan_id=?", (scan_id,),
            ).fetchone()
            if scan is None or scan[0] != "complete":
                raise InventoryError("duplicate planning requires a complete inventory scan")
            self._connection.execute(
                "DELETE FROM planned_duplicate_members WHERE group_id IN "
                "(SELECT group_id FROM planned_duplicate_groups WHERE scan_id=?)",
                (scan_id,),
            )
            self._connection.execute(
                "DELETE FROM planned_duplicate_groups WHERE scan_id=?", (scan_id,)
            )
            self._connection.execute(
                "DELETE FROM duplicate_plan_summaries WHERE scan_id=?", (scan_id,)
            )
            self._connection.execute(
                "UPDATE duplicate_plan_heads SET status='superseded' WHERE scan_id=?",
                (scan_id,),
            )

    def store_duplicate_groups(self, scan_id: int, groups: Iterable[DuplicateGroup]) -> None:
        """Persist a bounded group batch and its immutable file snapshots."""

        scan_id = resolve_scan_id(self._connection, scan_id)
        with self._connection:
            for group in groups:
                members = (group.keep, *group.redundant)
                if (
                    group.size <= 0 or not group.redundant
                    or any(member.size != group.size for member in members)
                    or len({member.identity for member in members}) != len(members)
                    or len({member.path for member in members}) != len(members)
                ):
                    raise InventoryError("duplicate group physical membership is inconsistent")
                if group.member_proofs and len(group.member_proofs) != 1 + len(group.redundant):
                    raise InventoryError("duplicate member proof count is inconsistent")
                group_proof = encode_proof(group.proof)
                if group.proof is not None:
                    expected_mode = "full_hash" if group.proof.requested_policy == "exact" else "fast"
                    if group.verification_mode != expected_mode or not group.member_proofs:
                        raise InventoryError("duplicate group proof does not cover its members")
                    for position, (member, proof) in enumerate(zip(members, group.member_proofs, strict=True)):
                        expected_result = "reference" if position == 0 else (
                            "equal" if expected_mode == "full_hash" else "fingerprint_match"
                        )
                        if proof.comparison_result != expected_result or member.path not in proof.aliases or (
                            position > 0 and proof.compared_to_identity != group.keep.identity
                        ) or (
                            proof.comparison_result == "equal" and proof.comparison_bytes != member.size
                        ):
                            raise InventoryError("duplicate member proof does not match the keeper")
                result = self._connection.execute(
                    "INSERT INTO planned_duplicate_groups"
                    "(scan_id,size,keep_path,redundant_count,reclaimable_bytes,full_fingerprint,"
                    "verification_mode,proof_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        scan_id,
                        group.size,
                        group.keep.path,
                        len(group.redundant),
                        group.reclaimable_bytes,
                        group.full_fingerprint,
                        group.verification_mode if group.proof is not None else "legacy_unknown",
                        group_proof,
                    ),
                )
                if result.lastrowid is None:
                    raise InventoryError("SQLite did not return a duplicate-group identifier")
                group_id = int(result.lastrowid)
                self._connection.executemany(
                    "INSERT INTO planned_duplicate_members"
                    "(group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns,"
                    "proof_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        (
                            group_id,
                            order,
                            "keep" if order == 0 else "redundant",
                            member.path,
                            _id_blob(member.volume_id),
                            _id_blob(member.file_id),
                            member.size,
                            member.mtime_ns,
                            member.birthtime_ns,
                            encode_proof(group.member_proofs[order]) if group.member_proofs else "{}",
                        )
                        for order, member in enumerate(members)
                    ),
                )

    def complete_duplicate_plan(
        self,
        scan_id: int,
        *,
        group_count: int,
        redundant_files: int,
        reclaimable_bytes: int,
        verification_mode: VerificationMode,
        requested_policy: DedupPolicy = "legacy_unknown",
        coverage: PlanCoverage = "legacy_unknown",
        exact_comparisons: int | None = None,
        changed_or_unreadable_files: int | None = None,
    ) -> None:
        if verification_mode not in VALID_VERIFICATION_MODES:
            raise InventoryError("dedup inventory plan has an invalid verification mode")
        if requested_policy not in {"legacy_unknown", "fast", "exact"} or coverage not in {
            "legacy_unknown", "complete", "partial"
        }:
            raise InventoryError("dedup inventory plan policy or coverage is invalid")
        scan_id = resolve_scan_id(self._connection, scan_id)
        with self._connection:
            # Publication is the last owner transaction, never a configured
            # policy masquerading as completed evidence.  Incomplete batches
            # stay invisible to readers until all groups/members reconcile.
            scan = self._connection.execute(
                "SELECT status FROM scans WHERE scan_id=?", (scan_id,),
            ).fetchone()
            if scan is None or scan[0] != "complete":
                raise InventoryError("duplicate plan cannot publish without a complete inventory scan")
            actual = self._connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(redundant_count),0),"
                "COALESCE(SUM(reclaimable_bytes),0) FROM planned_duplicate_groups WHERE scan_id=?",
                (scan_id,),
            ).fetchone()
            members = self._connection.execute(
                "SELECT COUNT(*) FROM planned_duplicate_members m JOIN planned_duplicate_groups g "
                "ON g.group_id=m.group_id WHERE g.scan_id=?", (scan_id,),
            ).fetchone()[0]
            if actual != (group_count, redundant_files, reclaimable_bytes) or members != (
                group_count + redundant_files
            ):
                raise InventoryError("duplicate plan cannot publish incomplete owner evidence")
            malformed = self._connection.execute(
                """SELECT COUNT(*) FROM planned_duplicate_groups g WHERE g.scan_id=? AND (
                    g.size<=0 OR g.redundant_count<1
                    OR g.reclaimable_bytes!=g.size*g.redundant_count
                    OR (SELECT COUNT(*) FROM planned_duplicate_members m WHERE m.group_id=g.group_id)
                        !=g.redundant_count+1
                    OR (SELECT MAX(member_order) FROM planned_duplicate_members m WHERE m.group_id=g.group_id)
                        !=g.redundant_count
                    OR NOT EXISTS(SELECT 1 FROM planned_duplicate_members m WHERE m.group_id=g.group_id
                        AND m.member_order=0 AND m.role='keep' AND m.path=g.keep_path AND m.size=g.size)
                    OR EXISTS(SELECT 1 FROM planned_duplicate_members m WHERE m.group_id=g.group_id AND (
                        m.member_order<0 OR m.size!=g.size
                        OR m.role!=CASE WHEN m.member_order=0 THEN 'keep' ELSE 'redundant' END))
                    OR (SELECT COUNT(DISTINCT hex(volume_id)||':'||hex(file_id))
                        FROM planned_duplicate_members m WHERE m.group_id=g.group_id)!=g.redundant_count+1
                )""", (scan_id,),
            ).fetchone()[0]
            if malformed:
                raise InventoryError("duplicate plan cannot publish inconsistent group membership")
            if requested_policy != "legacy_unknown":
                if (
                    type(exact_comparisons) is not int or exact_comparisons < 0
                    or type(changed_or_unreadable_files) is not int or changed_or_unreadable_files < 0
                    or coverage != ("partial" if changed_or_unreadable_files else "complete")
                    or verification_mode != (
                        "partial" if changed_or_unreadable_files else
                        "full_hash" if requested_policy == "exact" else "fast"
                    )
                    or (requested_policy == "fast" and exact_comparisons != 0)
                    or (requested_policy == "exact" and exact_comparisons < redundant_files)
                ):
                    raise InventoryError("duplicate plan comparison coverage is inconsistent")
                missing = self._connection.execute(
                    "SELECT COUNT(*) FROM planned_duplicate_groups g JOIN planned_duplicate_members m "
                    "ON m.group_id=g.group_id WHERE g.scan_id=? AND "
                    "(g.proof_json='{}' OR m.proof_json='{}' OR g.verification_mode!=?)",
                    (scan_id, "full_hash" if requested_policy == "exact" else "fast"),
                ).fetchone()[0]
                if missing:
                    raise InventoryError("duplicate plan cannot publish missing member proofs")
            self._connection.execute(
                "INSERT OR REPLACE INTO duplicate_plan_summaries"
                "(scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns,"
                "verification_mode,requested_policy,coverage,exact_comparisons,"
                "changed_or_unreadable_files) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id,
                    group_count,
                    redundant_files,
                    reclaimable_bytes,
                    time.time_ns(),
                    verification_mode,
                    requested_policy,
                    coverage,
                    exact_comparisons,
                    changed_or_unreadable_files,
                ),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO duplicate_plan_heads("
                "scan_id,inventory_content_digest,plan_digest,status,completed_ns) "
                "VALUES(?,?,?,?,?)",
                (
                    scan_id,
                    self._inventory_content_digest_for_plan(scan_id),
                    duplicate_plan_digest(self._connection, scan_id),
                    "published",
                    time.time_ns(),
                ),
            )

    def _inventory_content_digest_for_plan(self, scan_id: int) -> bytes:
        require_operational_identity(self._connection, "inventory", scan_id)
        row = self._connection.execute(
            "SELECT content_digest FROM inventory_generation_heads WHERE scan_id=?",
            (scan_id,),
        ).fetchone()
        if row is None:
            raise InventoryError("duplicate plan requires an inventory content identity")
        return bytes(row[0])

    def iter_duplicate_groups(self, scan_id: int) -> Iterator[DuplicateGroup]:
        """Stream a persisted plan in descending reclaimable-byte order."""

        scan_id = resolve_scan_id(self._connection, scan_id)
        rows = self._connection.execute(
            "SELECT g.group_id,g.size,g.full_fingerprint,g.verification_mode,g.proof_json,"
            "m.member_order,m.path,"
            "m.volume_id,m.file_id,m.size,m.mtime_ns,m.birthtime_ns,m.proof_json "
            "FROM planned_duplicate_groups g JOIN planned_duplicate_members m "
            "ON m.group_id=g.group_id "
            "JOIN duplicate_plan_summaries s ON s.scan_id=g.scan_id "
            "LEFT JOIN duplicate_plan_heads h ON h.scan_id=g.scan_id "
            "WHERE g.scan_id=? "
            "AND (h.scan_id IS NULL OR (h.status='published' "
            "AND h.inventory_content_digest=(SELECT content_digest "
            "FROM inventory_generation_heads WHERE scan_id=g.scan_id))) "
            f"ORDER BY g.reclaimable_bytes DESC,g.keep_path COLLATE {_PATH_COLLATION},"
            "g.group_id,m.member_order",
            (scan_id,),
        )
        current_group: int | None = None
        group_size = 0
        fingerprint = ""
        members: list[FileSnapshot] = []
        member_proofs: list[DuplicateMemberProof] = []
        group_proof = None
        group_mode: VerificationMode = "legacy_unknown"
        for (
            group_id,
            size,
            digest,
            verification_mode,
            proof_json,
            _order,
            path,
            volume,
            file_id,
            member_size,
            mtime,
            birth,
            member_proof_json,
        ) in rows:
            if current_group is not None and group_id != current_group:
                yield DuplicateGroup(
                    group_size,
                    members[0],
                    tuple(members[1:]),
                    fingerprint,
                    group_mode,
                    group_proof,
                    tuple(member_proofs),
                )
                members = []
                member_proofs = []
            current_group = group_id
            group_size = size
            fingerprint = digest
            group_mode = verification_mode
            group_proof = decode_group_proof(proof_json)
            member_proofs.append(decode_member_proof(member_proof_json))
            members.append(
                FileSnapshot(
                    path,
                    int.from_bytes(volume, "little"),
                    int.from_bytes(file_id, "little"),
                    member_size,
                    mtime,
                    birth,
                )
            )
        if current_group is not None:
            yield DuplicateGroup(
                group_size,
                members[0],
                tuple(members[1:]),
                fingerprint,
                group_mode,
                group_proof,
                tuple(member_proofs),
            )


__all__ = ["PlanRepositoryMixin"]
