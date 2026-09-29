"""Run-local, owner-bound admission projection for expensive corpus work.

This is a TEMP projection on the Inventory writer, not a second state owner.
Physical generations and Framework receipts remain authoritative.  The table
remembers the exact observations which crossed the cheap gate, so producers
can introduce a delta without re-opening the stable population.  It is lost
on interruption intentionally: a new run must revalidate current inventory.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from ..domain.models import FileSnapshot
from .scan import id_blob
from neocortex.persistence.sqlite_cancellation import SQLiteCancellationBridge, sqlite_cancellation_scope


_COLUMNS = "path,volume_id,file_id,size,mtime_ns,birthtime_ns"
_MATCH = (
    "a.path=f.path AND a.volume_id=f.volume_id AND a.file_id=f.file_id "
    "AND a.size=f.size AND a.mtime_ns=f.mtime_ns AND a.birthtime_ns=f.birthtime_ns"
)


def _snapshot(row: tuple) -> FileSnapshot:
    path, volume, file_id, size, mtime, birth = row
    return FileSnapshot(path, int.from_bytes(volume, "little"),
                        int.from_bytes(file_id, "little"), size, mtime, birth)


class CurationAdmission:
    """Bounded metadata selection and a fail-closed snapshot predicate.

    One instance lives for the initial writer session.  SQL cursors never
    escape a page; workers never touch this connection.  The caller publishes
    aggregate phase evidence in the existing Framework lifecycle.
    """

    def __init__(self, index, *, checkpoint: Callable[[], None],
                 sql_checkpoint: Callable[[], None] | None = None) -> None:
        self.index = index
        self.connection = index._connection
        self.checkpoint = checkpoint
        self.sql_checkpoint = sql_checkpoint or checkpoint
        self.scan_id = 0
        self.rounds = 0
        self.checked = 0
        self._closed = False
        # Select disk-spillable TEMP before acquiring a table lease. Changing
        # temp_store later deletes *all* TEMP objects, not just planner tables.
        if int(self.connection.execute("PRAGMA temp_store").fetchone()[0]) != 1:
            if self.connection.execute("SELECT 1 FROM sqlite_temp_master LIMIT 1").fetchone():
                raise RuntimeError("curation admission cannot replace a live TEMP store")
            self.connection.execute("PRAGMA temp_store=FILE")
        # Fixed TEMP names prevent accidentally sharing two live admission
        # sessions on one writer. No DROP IF EXISTS can destroy another lease.
        self.connection.execute(
            "CREATE TEMP TABLE curation_admitted ("
            "path TEXT PRIMARY KEY,volume_id BLOB NOT NULL,file_id BLOB NOT NULL,"
            "size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,birthtime_ns INTEGER NOT NULL,"
            "eligible INTEGER NOT NULL,reason TEXT NOT NULL) WITHOUT ROWID"
        )
        try:
            self.connection.execute(
                "CREATE TEMP TABLE curation_pending ("
                "path TEXT PRIMARY KEY,volume_id BLOB NOT NULL,file_id BLOB NOT NULL,"
                "size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,birthtime_ns INTEGER NOT NULL"
                ") WITHOUT ROWID"
            )
        except BaseException:
            self.connection.execute("DROP TABLE temp.curation_admitted")
            raise

    def capture_delta(self, scan_id: int) -> int:
        """Select only observations absent/changed since the last gate."""
        self.checkpoint()
        self.scan_id = self.index.current_scan_id(scan_id)
        self.connection.execute("DELETE FROM temp.curation_pending")
        # Selection can traverse a large, already inventoried population, but
        # does not open payloads. Its SQL scan has an interruptible deadline;
        # only bounded pages leave the owner. Never query this connection from
        # its progress handler.
        with sqlite_cancellation_scope(self.connection, SQLiteCancellationBridge(self.sql_checkpoint)):
            self.connection.execute(
                "INSERT INTO temp.curation_pending SELECT "
                + ",".join("f." + column for column in _COLUMNS.split(","))
                + " FROM files f WHERE f.scan_id=? AND NOT EXISTS("
                "SELECT 1 FROM temp.curation_admitted a WHERE " + _MATCH + ")",
                (self.scan_id,),
            )
        return self.pending_count

    @property
    def pending_count(self) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM temp.curation_pending"
        ).fetchone()[0])

    def page(self, *, after_path: str = "", limit: int = 256) -> tuple[FileSnapshot, ...]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("admission page must be bounded")
        self.checkpoint()
        rows = self.connection.execute(
            "SELECT " + _COLUMNS + " FROM temp.curation_pending WHERE path>? ORDER BY path LIMIT ?",
            (after_path, limit),
        ).fetchall()
        return tuple(_snapshot(row) for row in rows)

    def snapshots(self) -> Iterator[FileSnapshot]:
        after = ""
        while page := self.page(after_path=after):
            after = page[-1].path
            yield from page

    def current_page(self, *, after_path: str = "", limit: int = 256) -> tuple[FileSnapshot, ...]:
        """Read the delta after its identity-preserving renames/Trash."""
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("admission page must be bounded")
        self.checkpoint()
        scan = self.index.current_scan_id(self.scan_id)
        rows = self.connection.execute(
            "SELECT " + ",".join("f." + column for column in _COLUMNS.split(","))
            + " FROM files f JOIN temp.curation_pending a ON " + _MATCH
            + " WHERE f.scan_id=? AND f.path>? ORDER BY f.path LIMIT ?",
            (scan, after_path, limit),
        ).fetchall()
        return tuple(_snapshot(row) for row in rows)

    def current_snapshots(self) -> Iterator[FileSnapshot]:
        after = ""
        while page := self.current_page(after_path=after):
            after = page[-1].path
            yield from page

    def rebind(self, normalized_paths: dict[str, str]) -> None:
        """Bind the pending selection to observed Normalize successors only."""
        current_scan = self.index.current_scan_id(self.scan_id)
        for old, new in normalized_paths.items():
            if old != new:
                # normalized_paths also contains preview/blocked intents. An
                # intent cannot remove the old observation from the pending
                # gate: require the exact identity in the published successor.
                self.connection.execute(
                    "UPDATE temp.curation_pending SET path=? WHERE path=? AND EXISTS("
                    "SELECT 1 FROM files f WHERE f.scan_id=? AND f.path=? "
                    "AND f.volume_id=curation_pending.volume_id AND f.file_id=curation_pending.file_id "
                    "AND f.size=curation_pending.size AND f.mtime_ns=curation_pending.mtime_ns "
                    "AND f.birthtime_ns=curation_pending.birthtime_ns)",
                    (new, old, current_scan, new),
                )

    def settle(self, decision: Callable[[FileSnapshot], tuple[bool, str]]) -> None:
        """Record every surviving delta member after all cheap decisions."""
        self.rounds += 1
        self.checked += self.pending_count
        for page_start in self._settlement_pages():
            records = []
            for snapshot in page_start:
                self.checkpoint()
                eligible, reason = decision(snapshot)
                records.append((snapshot.path, id_blob(snapshot.volume_id), id_blob(snapshot.file_id),
                                snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns,
                                int(eligible), str(reason)[:128]))
            self.connection.executemany(
                "INSERT OR REPLACE INTO temp.curation_admitted VALUES(?,?,?,?,?,?,?,?)", records
            )

    def _settlement_pages(self):
        after = ""
        while page := self.current_page(after_path=after):
            after = page[-1].path
            yield page

    def permits(self, snapshot: FileSnapshot) -> bool:
        if self._closed:
            return False
        row = self.connection.execute(
            "SELECT eligible FROM temp.curation_admitted WHERE path=? AND volume_id=? "
            "AND file_id=? AND size=? AND mtime_ns=? AND birthtime_ns=?",
            (snapshot.path, id_blob(snapshot.volume_id), id_blob(snapshot.file_id),
             snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns),
        ).fetchone()
        return row is not None and row[0] == 1

    def exclude(self, snapshot: FileSnapshot, reason: str) -> None:
        self.connection.execute(
            "UPDATE temp.curation_admitted SET eligible=0,reason=? WHERE path=? "
            "AND volume_id=? AND file_id=? AND size=? AND mtime_ns=? AND birthtime_ns=?",
            (reason[:128], snapshot.path, id_blob(snapshot.volume_id), id_blob(snapshot.file_id),
             snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns),
        )

    def exclude_planned_duplicates(self, scan_id: int) -> None:
        """Preview/blocked redundant originals are not expensive route inputs."""
        self.connection.execute(
            "UPDATE temp.curation_admitted SET eligible=0,reason='planned_duplicate' WHERE path IN("
            "SELECT m.path FROM planned_duplicate_members m JOIN planned_duplicate_groups g "
            "ON g.group_id=m.group_id WHERE g.scan_id=? AND m.role='redundant')",
            (self.index.current_scan_id(scan_id),),
        )

    def summary(self) -> dict[str, object]:
        rows = self.connection.execute(
            "SELECT eligible,reason,COUNT(*),COALESCE(SUM(size),0) "
            "FROM temp.curation_admitted GROUP BY eligible,reason"
        ).fetchall()
        return {
            "schema": "neocortex.curation-admission/v1", "rounds": self.rounds,
            "checked": self.checked,
            "admitted": sum(row[2] for row in rows if row[0]),
            "excluded": sum(row[2] for row in rows if not row[0]),
            "exclusion_reasons": {row[1]: row[2] for row in rows if not row[0]},
        }

    def excluded_pages(self):
        """Detach small exclusion pages before closing the Inventory writer."""
        after = ""
        while True:
            self.checkpoint()
            rows = self.connection.execute(
                "SELECT " + _COLUMNS + ",reason FROM temp.curation_admitted "
                "WHERE eligible=0 AND path>? ORDER BY path LIMIT 256", (after,)
            ).fetchall()
            if not rows:
                return
            after = rows[-1][0]
            yield tuple((_snapshot(row[:6]), row[6]) for row in rows)

    def close(self) -> None:
        if not self._closed:
            self.connection.execute("DROP TABLE temp.curation_pending")
            self.connection.execute("DROP TABLE temp.curation_admitted")
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
