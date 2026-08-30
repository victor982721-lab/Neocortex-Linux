"""Disk-backed fingerprint planning and persisted duplicate groups."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator

from neocortex.platform.policy import sqlite_path_collation

from ..domain.errors import InventoryError
from ..domain.models import DuplicateGroup, FileSnapshot
from .scan import id_blob as _id_blob


_PATH_COLLATION = sqlite_path_collation()


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
                PRIMARY KEY(stage,volume_id,file_id)
            ) WITHOUT ROWID;
            CREATE INDEX planning_fingerprint_collision_idx
                ON planning_fingerprints(stage,digest);
            """
        )

    def clear_planning_fingerprints(self) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM planning_seen")
            self._connection.execute("DELETE FROM planning_fingerprints")

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
    ) -> None:
        with self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO planning_fingerprints(
                stage,digest,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                VALUES(?,?,?,?,?,?,?,?)""",
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
            WHERE w.stage=? ORDER BY w.digest,w.mtime_ns DESC,
            w.birthtime_ns DESC,w.path COLLATE {_PATH_COLLATION} DESC""",
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

        with self._connection:
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

    def store_duplicate_groups(self, scan_id: int, groups: Iterable[DuplicateGroup]) -> None:
        """Persist a bounded group batch and its immutable file snapshots."""

        with self._connection:
            for group in groups:
                result = self._connection.execute(
                    "INSERT INTO planned_duplicate_groups"
                    "(scan_id,size,keep_path,redundant_count,reclaimable_bytes,full_fingerprint) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        scan_id,
                        group.size,
                        group.keep.path,
                        len(group.redundant),
                        group.reclaimable_bytes,
                        group.full_fingerprint,
                    ),
                )
                if result.lastrowid is None:
                    raise InventoryError("SQLite did not return a duplicate-group identifier")
                group_id = int(result.lastrowid)
                members = (group.keep, *group.redundant)
                self._connection.executemany(
                    "INSERT INTO planned_duplicate_members"
                    "(group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
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
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO duplicate_plan_summaries VALUES(?,?,?,?,?)",
                (
                    scan_id,
                    group_count,
                    redundant_files,
                    reclaimable_bytes,
                    time.time_ns(),
                ),
            )

    def iter_duplicate_groups(self, scan_id: int) -> Iterator[DuplicateGroup]:
        """Stream a persisted plan in descending reclaimable-byte order."""

        rows = self._connection.execute(
            "SELECT g.group_id,g.size,g.full_fingerprint,m.member_order,m.path,"
            "m.volume_id,m.file_id,m.size,m.mtime_ns,m.birthtime_ns "
            "FROM planned_duplicate_groups g JOIN planned_duplicate_members m "
            "ON m.group_id=g.group_id WHERE g.scan_id=? "
            f"ORDER BY g.reclaimable_bytes DESC,g.keep_path COLLATE {_PATH_COLLATION},"
            "g.group_id,m.member_order",
            (scan_id,),
        )
        current_group: int | None = None
        group_size = 0
        fingerprint = ""
        members: list[FileSnapshot] = []
        for (
            group_id,
            size,
            digest,
            _order,
            path,
            volume,
            file_id,
            member_size,
            mtime,
            birth,
        ) in rows:
            if current_group is not None and group_id != current_group:
                yield DuplicateGroup(group_size, members[0], tuple(members[1:]), fingerprint)
                members = []
            current_group = group_id
            group_size = size
            fingerprint = digest
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
            yield DuplicateGroup(group_size, members[0], tuple(members[1:]), fingerprint)


__all__ = ["PlanRepositoryMixin"]
