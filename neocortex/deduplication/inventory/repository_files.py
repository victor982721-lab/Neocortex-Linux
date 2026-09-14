"""File snapshots, collision candidates, and fingerprint cache operations."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path

from ..domain.models import FileSnapshot
from ..fingerprinting import FULL_ALGORITHM, snapshot_path
from .scan import id_blob as _id_blob
from .repository_scans import resolve_scan_id


def iter_size_collision_groups(
    connection: sqlite3.Connection,
    scan_id: int,
    *,
    snapshot_resolver: Callable[[str], FileSnapshot] | None = None,
) -> Iterator[tuple[FileSnapshot, ...]]:
    """Yield physically distinct, still-current files from equal-size buckets."""

    scan_id = resolve_scan_id(connection, scan_id)
    resolver = snapshot_path if snapshot_resolver is None else snapshot_resolver
    sizes = connection.execute(
        "SELECT size FROM files WHERE scan_id=? AND size>0 "
        "GROUP BY size HAVING COUNT(*) > 1 "
        "ORDER BY size",
        (scan_id,),
    )
    for (size,) in sizes:
        rows = connection.execute(
            "SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns "
            "FROM files WHERE scan_id=? AND size=? ORDER BY path",
            (scan_id, size),
        )
        # Multiple names for the same file (hard links) are not duplicate
        # physical files and therefore collapse to one identity here. On
        # Windows, DirEntry.stat() deliberately exposes st_dev=st_ino=0
        # from cached directory metadata. scan() therefore stores the root
        # volume ID plus DirEntry.inode(); candidates are refreshed here to
        # detect mutations without adding one os.stat() per unique-size file.
        unique: dict[tuple[int, int], FileSnapshot] = {}
        for (
            path,
            recorded_volume,
            recorded_file_id,
            recorded_size,
            recorded_mtime,
            recorded_birthtime,
        ) in rows:
            try:
                snapshot = resolver(path)
            except OSError:
                continue
            if (
                snapshot.identity
                != (
                    int.from_bytes(recorded_volume, "little"),
                    int.from_bytes(recorded_file_id, "little"),
                )
                or snapshot.size != recorded_size
                or snapshot.mtime_ns != recorded_mtime
                or snapshot.birthtime_ns != recorded_birthtime
            ):
                # The inventory is stale; never hash the new state under an
                # old candidate decision.
                continue
            unique.setdefault(snapshot.identity, snapshot)
        if len(unique) > 1:
            yield tuple(unique.values())


class FileRepositoryMixin:
    """Read inventory members and maintain their reusable fingerprints."""

    _connection: sqlite3.Connection

    def snapshots(self, scan_id: int) -> Iterator[FileSnapshot]:
        scan_id = resolve_scan_id(self._connection, scan_id)
        rows = self._connection.execute(
            "SELECT path, volume_id, file_id, size, mtime_ns, birthtime_ns "
            "FROM files WHERE scan_id=? ORDER BY path",
            (scan_id,),
        )
        for path, volume, file_id, size, mtime, birth in rows:
            yield FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                size,
                mtime,
                birth,
            )

    def published_snapshots(self, root: str | Path) -> Iterator[FileSnapshot]:
        """Stream one atomically selected published generation for *root*.

        The checkpoint selection and file read intentionally share one SQL
        statement. A caller must not emulate this by pairing
        :meth:`inventory_checkpoint` with :meth:`snapshots`, because a
        concurrent publisher may prune the selected generation between calls.
        """

        rows = self._connection.execute(
            """SELECT f.path,f.volume_id,f.file_id,f.size,f.mtime_ns,f.birthtime_ns
            FROM inventory_checkpoints c
            JOIN scans s ON s.scan_id=c.scan_id
            JOIN files f ON f.scan_id=c.scan_id
            WHERE c.root=? AND c.valid=1
              AND s.inventory_policy_signature IS NOT NULL
            ORDER BY f.path""",
            (os.path.abspath(os.fspath(root)),),
        )
        for path, volume, file_id, size, mtime, birth in rows:
            yield FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                size,
                mtime,
                birth,
            )

    def cached_fingerprint(self, snapshot: FileSnapshot, algorithm: str) -> bytes | None:
        row = self._connection.execute(
            "SELECT digest FROM fingerprints WHERE volume_id=? AND file_id=? "
            "AND size=? AND mtime_ns=? AND birthtime_ns=? AND algorithm=?",
            (
                _id_blob(snapshot.volume_id),
                _id_blob(snapshot.file_id),
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                algorithm,
            ),
        ).fetchone()
        return None if row is None else bytes(row[0])

    def validated_cached_fingerprint(
        self,
        snapshot: FileSnapshot,
        algorithm: str,
    ) -> bytes | None:
        """Use a cache hit only when its stored full-content digest still matches."""

        row = self._connection.execute(
            "SELECT f.digest,e.content_digest FROM fingerprints f "
            "JOIN fingerprint_content_evidence e ON "
            "e.volume_id=f.volume_id AND e.file_id=f.file_id AND e.size=f.size "
            "AND e.mtime_ns=f.mtime_ns AND e.birthtime_ns=f.birthtime_ns "
            "AND e.algorithm=f.algorithm WHERE f.volume_id=? AND f.file_id=? "
            "AND f.size=? AND f.mtime_ns=? AND f.birthtime_ns=? AND f.algorithm=?",
            (
                _id_blob(snapshot.volume_id),
                _id_blob(snapshot.file_id),
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                algorithm,
            ),
        ).fetchone()
        if row is None:
            return None
        from ..domain.errors import FileChangedError
        from ..fingerprinting import full_fingerprint

        try:
            current_content_digest = full_fingerprint(snapshot)
        except (OSError, FileChangedError):
            return None
        if current_content_digest != bytes(row[1]):
            return None
        return bytes(row[0])

    def store_fingerprint(self, snapshot: FileSnapshot, algorithm: str, digest: bytes) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO fingerprints"
                "(volume_id, file_id, size, mtime_ns, birthtime_ns, algorithm, digest) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    _id_blob(snapshot.volume_id),
                    _id_blob(snapshot.file_id),
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    algorithm,
                    digest,
                ),
            )
            if algorithm == FULL_ALGORITHM:
                self._connection.execute(
                    "INSERT OR REPLACE INTO fingerprint_content_evidence("
                    "volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,content_digest) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        _id_blob(snapshot.volume_id),
                        _id_blob(snapshot.file_id),
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        algorithm,
                        digest,
                    ),
                )

    def store_fingerprints(
        self,
        algorithm: str,
        rows: Iterable[tuple[FileSnapshot, bytes]],
        *,
        content_digests: Mapping[tuple[int, int], bytes] | None = None,
    ) -> None:
        """Persist a bounded fingerprint batch in one WAL transaction."""

        materialized = tuple(rows)
        with self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO fingerprints(
                volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,digest)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    (
                        _id_blob(snapshot.volume_id),
                        _id_blob(snapshot.file_id),
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        algorithm,
                        digest,
                    )
                    for snapshot, digest in materialized
                ),
            )
            if content_digests:
                self._connection.executemany(
                    "INSERT OR REPLACE INTO fingerprint_content_evidence("
                    "volume_id,file_id,size,mtime_ns,birthtime_ns,algorithm,content_digest) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        (
                            _id_blob(snapshot.volume_id),
                            _id_blob(snapshot.file_id),
                            snapshot.size,
                            snapshot.mtime_ns,
                            snapshot.birthtime_ns,
                            algorithm,
                            content_digests.get(snapshot.identity),
                        )
                        for snapshot, digest in materialized
                        if content_digests.get(snapshot.identity) is not None
                    ),
                )

    def file_count(self, scan_id: int) -> int:
        scan_id = resolve_scan_id(self._connection, scan_id)
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM files WHERE scan_id=?", (scan_id,)
            ).fetchone()[0]
        )

    def contains_identity(self, scan_id: int, volume_id: int, file_id: int) -> bool:
        scan_id = resolve_scan_id(self._connection, scan_id)
        return (
            self._connection.execute(
                "SELECT 1 FROM files WHERE scan_id=? AND volume_id=? AND file_id=? LIMIT 1",
                (scan_id, _id_blob(volume_id), _id_blob(file_id)),
            ).fetchone()
            is not None
        )

    def size_candidate_file_count(self, scan_id: int) -> int:
        scan_id = resolve_scan_id(self._connection, scan_id)
        row = self._connection.execute(
            "SELECT COALESCE(SUM(candidate_count), 0) FROM ("
            "SELECT COUNT(*) AS candidate_count FROM files WHERE scan_id=? AND size>0 "
            "GROUP BY size HAVING COUNT(*) > 1)",
            (scan_id,),
        ).fetchone()
        return int(row[0])

    def size_collision_sizes(self, scan_id: int) -> Iterator[tuple[int, int]]:
        """Stream size buckets without materializing their file members."""

        scan_id = resolve_scan_id(self._connection, scan_id)
        rows = self._connection.execute(
            "SELECT size,COUNT(*) FROM files WHERE scan_id=? AND size>0 "
            "GROUP BY size HAVING COUNT(*)>1 ORDER BY size",
            (scan_id,),
        )
        for size, count in rows:
            yield int(size), int(count)

    def snapshots_by_size(self, scan_id: int, size: int) -> Iterator[FileSnapshot]:
        scan_id = resolve_scan_id(self._connection, scan_id)
        rows = self._connection.execute(
            "SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns "
            "FROM files WHERE scan_id=? AND size=? ORDER BY path",
            (scan_id, size),
        )
        for path, volume, file_id, item_size, mtime, birth in rows:
            yield FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                item_size,
                mtime,
                birth,
            )

    def snapshots_by_size_page(
        self,
        scan_id: int,
        size: int,
        *,
        after_path: str = "",
        limit: int = 256,
    ) -> tuple[FileSnapshot, ...]:
        """Read one bounded size page using the already-open owner connection.

        Action phases must not construct a second :class:`DedupIndex` while
        the writer connection owns a WAL-backed inventory.  Returning a fully
        materialized page closes the SQLite cursor before callers perform any
        file effect, while ``after_path`` keeps the traversal bounded and
        deterministic for large zero-byte populations.
        """

        if not isinstance(after_path, str):
            raise TypeError("after_path must be text")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("snapshot page limit must be between 1 and 10000")
        scan_id = resolve_scan_id(self._connection, scan_id)
        rows = self._connection.execute(
            "SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns "
            "FROM files WHERE scan_id=? AND size=? AND path>? "
            "ORDER BY path LIMIT ?",
            (scan_id, size, after_path, limit),
        ).fetchall()
        return tuple(
            FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                item_size,
                mtime,
                birth,
            )
            for path, volume, file_id, item_size, mtime, birth in rows
        )

    def snapshots_page(
        self,
        scan_id: int,
        *,
        after_path: str = "",
        limit: int = 256,
    ) -> tuple[FileSnapshot, ...]:
        """Read one bounded inventory page without opening another owner.

        The action pipeline persists route candidates and cache observations
        while the inventory owner remains open.  A fetched tuple closes the
        cursor before those writes, avoiding a WAL-backed read connection and
        keeping memory bounded for large inventories.
        """

        if not isinstance(after_path, str):
            raise TypeError("after_path must be text")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("snapshot page limit must be between 1 and 10000")
        scan_id = resolve_scan_id(self._connection, scan_id)
        rows = self._connection.execute(
            "SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns "
            "FROM files WHERE scan_id=? AND path>? "
            "ORDER BY path LIMIT ?",
            (scan_id, after_path, limit),
        ).fetchall()
        return tuple(
            FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                item_size,
                mtime,
                birth,
            )
            for path, volume, file_id, item_size, mtime, birth in rows
        )

    def file_count_by_size(self, scan_id: int, size: int) -> int:
        scan_id = resolve_scan_id(self._connection, scan_id)
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM files WHERE scan_id=? AND size=?",
                (scan_id, size),
            ).fetchone()[0]
        )

    def snapshots_excluding_planned_redundant(self, scan_id: int) -> Iterator[FileSnapshot]:
        """Stream files that would survive the persisted dry-run plan."""

        scan_id = resolve_scan_id(self._connection, scan_id)
        rows = self._connection.execute(
            "SELECT f.path,f.volume_id,f.file_id,f.size,f.mtime_ns,f.birthtime_ns "
            "FROM files f WHERE f.scan_id=? AND NOT EXISTS("
            "SELECT 1 FROM planned_duplicate_groups g "
            "JOIN planned_duplicate_members m ON m.group_id=g.group_id "
            "WHERE g.scan_id=f.scan_id AND m.role='redundant' "
            "AND m.path=f.path) ORDER BY f.path",
            (scan_id,),
        )
        for path, volume, file_id, size, mtime, birth in rows:
            yield FileSnapshot(
                path,
                int.from_bytes(volume, "little"),
                int.from_bytes(file_id, "little"),
                size,
                mtime,
                birth,
            )


__all__ = ["FileRepositoryMixin", "iter_size_collision_groups"]
