"""Transactional, writer-local identity lookup for the Code FTS projection.

Equality on FTS5 UNINDEXED columns scans the entire projection. Preserve all
rowids, duplicate identities and anomalous values in an ordinary TEMP table;
the durable rows and existing validation remain authoritative.
"""

from __future__ import annotations

import sqlite3


class CodeFTSLookup:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._ready = False
        self._changes = connection.total_changes
        self._data_version: int | None = None
        self._generation = 0

    def invalidate_if_changed(self) -> None:
        """Discard observations after writes outside the repository operation."""

        if not self._ready:
            return
        current = int(self.connection.execute("PRAGMA data_version").fetchone()[0])
        tables = {str(row[0]) for row in self.connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name IN ('_code_fts_lookup','_code_fts_lookup_state') AND type='table'"
        )}
        generation = None if len(tables) != 2 else self.connection.execute(
            "SELECT generation FROM temp._code_fts_lookup_state"
        ).fetchone()
        if (
            self.connection.total_changes != self._changes or current != self._data_version
            or generation is None or generation[0] != self._generation
        ):
            self._ready = False

    def acknowledge(self) -> None:
        """Remember repository-owned changes after their lookup updates."""

        self._changes = self.connection.total_changes

    def ensure(self) -> None:
        self.invalidate_if_changed()
        if self._ready:
            return
        # A savepoint preserves any caller transaction, including rollback of
        # lookup creation. Only initialization on an idle connection commits.
        self.connection.execute("SAVEPOINT code_fts_lookup_build")
        try:
            self._generation += 1
            self.connection.execute("DROP TABLE IF EXISTS temp._code_fts_lookup")
            self.connection.execute(
                # Match the affinity of code_chunks.chunk_id for its indexed
                # join. The original FTS predicate still checks stored values.
                "CREATE TEMP TABLE _code_fts_lookup(fts_rowid INTEGER PRIMARY KEY,version_id,chunk_id INTEGER)"
            )
            self.connection.execute(
                "INSERT INTO _code_fts_lookup SELECT rowid,version_id,chunk_id FROM main.code_fts"
            )
            self.connection.execute(
                "CREATE INDEX temp._code_fts_lookup_version ON _code_fts_lookup(version_id)"
            )
            self.connection.execute(
                "CREATE INDEX temp._code_fts_lookup_chunk ON _code_fts_lookup(chunk_id)"
            )
            # Rolling back a rebuild can resurrect an older, stale lookup.
            # This transaction-owned token distinguishes that from rolling
            # back ordinary FTS writes whose lookup changes roll back too.
            self.connection.execute("CREATE TEMP TABLE IF NOT EXISTS _code_fts_lookup_state(generation INTEGER NOT NULL)")
            self.connection.execute("DELETE FROM temp._code_fts_lookup_state")
            self.connection.execute("INSERT INTO temp._code_fts_lookup_state VALUES(?)", (self._generation,))
        except BaseException:
            self.connection.execute("ROLLBACK TO code_fts_lookup_build")
            self.connection.execute("RELEASE code_fts_lookup_build")
            raise
        self.connection.execute("RELEASE code_fts_lookup_build")
        self._ready = True
        self._data_version = int(self.connection.execute("PRAGMA data_version").fetchone()[0])
        self.acknowledge()

    def predicate(self, version_id: int) -> tuple[str, tuple[int, ...]]:
        self.ensure()
        # Recheck the original predicate against FTS rows too: the lookup is
        # only an access path, never evidence that a projected row is valid.
        return (
            "rowid IN (SELECT fts_rowid FROM temp._code_fts_lookup WHERE version_id=? "
            "UNION SELECT l.fts_rowid FROM code_chunks AS c "
            "JOIN temp._code_fts_lookup AS l ON l.chunk_id=c.chunk_id WHERE c.version_id=?) "
            "AND (version_id=? OR chunk_id IN(SELECT chunk_id FROM code_chunks WHERE version_id=?))",
            (version_id,) * 4,
        )

    def record(self, rowid: int, version_id: int, chunk_id: int) -> None:
        if self._ready:
            self.connection.execute(
                "INSERT INTO temp._code_fts_lookup(fts_rowid,version_id,chunk_id) VALUES(?,?,?)",
                (rowid, version_id, chunk_id),
            )

    def delete(self, predicate: str, parameters: tuple[int, ...]) -> None:
        # Capture exact authoritative rowids before removing the projection.
        rowids = tuple(
            (int(row[0]),)
            for row in self.connection.execute(
                f"SELECT rowid FROM code_fts WHERE {predicate}", parameters
            )
        )
        self.connection.executemany("DELETE FROM code_fts WHERE rowid=?", rowids)
        self.connection.executemany(
            "DELETE FROM temp._code_fts_lookup WHERE fts_rowid=?", rowids
        )
