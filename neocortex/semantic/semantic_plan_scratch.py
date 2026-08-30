"""Bounded private scratch storage for exact Semantic planning."""
# region [00] Contexto del módulo
# Módulo: neocortex/semantic_plan_scratch.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations
import shutil
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol

from .semantic_models import (
    ContentFingerprint,
    EmbeddingModelSpec,
    EmbeddingRole,
    fingerprint_chunks,
)
from .semantic_plan_errors import (
    SemanticPlanBlocked,
    SemanticScratchLimitExceeded,
    cleanup_preserving_primary as _cleanup_preserving_primary,
)
from neocortex.persistence.sqlite_cancellation import SQLiteCancellationBridge
# endregion [01]

# region [02] Implementación


CONTENT_BATCH_SIZE = 512
REUSE_LOOKUP_BATCH_SIZE = 100
SCRATCH_CACHE_KIB = 16 * 1024
DEFAULT_MAX_SCRATCH_BYTES = 512 * 1024 * 1024
MIN_MAX_SCRATCH_BYTES = 64 * 1024

_CONTENT_COLUMNS = (
    "model_signature,role,modality,content_xxh3_128,content_bytes,content_xxh3_64_guard"
)


class _ScratchWorkload(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> EmbeddingModelSpec: ...

    @property
    def role(self) -> EmbeddingRole: ...

    @property
    def vector_blob_bytes(self) -> int: ...


@dataclass(slots=True)
class _ScratchBudget:
    directory: Path
    maximum_bytes: int
    peak_bytes: int = 0

    def checkpoint(self) -> int:
        try:
            observed = sum(
                entry.stat().st_size
                for entry in self.directory.iterdir()
                if entry.is_file()
            )
        except OSError as exc:
            raise SemanticPlanBlocked(
                f"semantic planner could not inspect scratch storage: {exc}"
            ) from exc
        self.peak_bytes = max(self.peak_bytes, observed)
        if observed > self.maximum_bytes:
            raise SemanticScratchLimitExceeded(
                "semantic planner scratch limit exceeded: "
                f"observed={observed} max_scratch_bytes={self.maximum_bytes}"
            )
        return observed

    def require_write_space(self, *, page_size: int) -> None:
        self.checkpoint()
        try:
            free = shutil.disk_usage(self.directory).free
        except OSError as exc:
            raise SemanticPlanBlocked(
                f"semantic planner could not inspect scratch free space: {exc}"
            ) from exc
        if free < page_size:
            raise SemanticScratchLimitExceeded(
                "semantic planner scratch storage has less than one SQLite page "
                f"available: free={free} page_size={page_size}"
            )


def _raise_scratch_error(
    exc: sqlite3.Error,
    budget: _ScratchBudget,
) -> NoReturn:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if (
        error_code == sqlite3.SQLITE_FULL
        or "database or disk is full" in str(exc).lower()
    ):
        raise SemanticScratchLimitExceeded(
            "semantic planner scratch limit or backing storage was exhausted: "
            f"max_scratch_bytes={budget.maximum_bytes}"
        ) from exc
    raise exc


class _ContentAccumulator:
    """Bounded batch writer for exact cross-workload content cardinality."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        bridge: SQLiteCancellationBridge,
        budget: _ScratchBudget,
    ) -> None:
        self._connection = connection
        self._bridge = bridge
        self._budget = budget
        self._content_rows: list[tuple[object, ...]] = []
        self._workload_rows: list[tuple[object, ...]] = []

    def add(
        self,
        workload: _ScratchWorkload,
        fingerprint: ContentFingerprint,
        *,
        source_payload_bytes: int,
    ) -> None:
        if source_payload_bytes < 0:
            raise SemanticPlanBlocked("semantic input payload bytes cannot be negative")
        model = workload.model
        key = (
            model.model_signature,
            workload.role.value,
            model.modality.value,
            fingerprint.xxh3_128,
            fingerprint.byte_count,
            fingerprint.xxh3_64_guard,
        )
        self._content_rows.append(
            (
                *key,
                source_payload_bytes,
                workload.name,
                model.dimensions,
                model.vector_dtype.value,
                workload.vector_blob_bytes,
            )
        )
        self._workload_rows.append((workload.name, *key))
        if len(self._content_rows) >= CONTENT_BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self._content_rows:
            return
        self._bridge.checkpoint()
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        self._budget.require_write_space(page_size=page_size)
        try:
            self._connection.execute("BEGIN")
            self._connection.executemany(
                f"""INSERT INTO content_keys(
                    {_CONTENT_COLUMNS},source_payload_bytes,first_workload,
                    dimensions,vector_dtype,vector_blob_bytes,occurrences,reusable)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,1,0)
                ON CONFLICT({_CONTENT_COLUMNS}) DO UPDATE SET
                    occurrences=content_keys.occurrences+1""",
                self._content_rows,
            )
            self._connection.executemany(
                f"""INSERT INTO workload_keys(
                    workload,{_CONTENT_COLUMNS},occurrences)
                VALUES(?,?,?,?,?,?,?,1)
                ON CONFLICT(workload,{_CONTENT_COLUMNS}) DO UPDATE SET
                    occurrences=workload_keys.occurrences+1""",
                self._workload_rows,
            )
            self._connection.commit()
            self._budget.checkpoint()
        except sqlite3.Error as exc:
            _cleanup_preserving_primary(
                self._connection.rollback,
                exc,
                label="semantic planner scratch rollback cleanup",
            )
            _raise_scratch_error(exc, self._budget)
        except BaseException as exc:
            _cleanup_preserving_primary(
                self._connection.rollback,
                exc,
                label="semantic planner scratch rollback cleanup",
            )
            raise
        finally:
            self._content_rows.clear()
            self._workload_rows.clear()

    def content_set_xxh3_128(self) -> str:
        self.flush()

        def rows() -> Iterator[bytes]:
            query = self._connection.execute(
                f"""SELECT {_CONTENT_COLUMNS} FROM content_keys
                ORDER BY {_CONTENT_COLUMNS}"""
            )
            for row in query:
                self._bridge.checkpoint()
                yield ("\0".join(str(value) for value in row) + "\n").encode("utf-8")

        return fingerprint_chunks(rows()).xxh3_128

    def reuse_lookup_batches(
        self,
    ) -> Iterator[tuple[tuple[object, ...], ...]]:
        """Page planned identities without holding a cursor across scratch writes."""

        self.flush()
        last_key: tuple[object, ...] | None = None
        while True:
            self._bridge.checkpoint()
            if last_key is None:
                rows = self._connection.execute(
                    f"""SELECT {_CONTENT_COLUMNS},dimensions,vector_dtype
                    FROM content_keys ORDER BY {_CONTENT_COLUMNS}
                    LIMIT ?""",
                    (REUSE_LOOKUP_BATCH_SIZE,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    f"""SELECT {_CONTENT_COLUMNS},dimensions,vector_dtype
                    FROM content_keys
                    WHERE ({_CONTENT_COLUMNS})>(?,?,?,?,?,?)
                    ORDER BY {_CONTENT_COLUMNS} LIMIT ?""",
                    (*last_key, REUSE_LOOKUP_BATCH_SIZE),
                ).fetchall()
            if not rows:
                return
            batch = tuple(tuple(row) for row in rows)
            yield batch
            last_key = tuple(rows[-1][:6])

    def mark_preexisting_reuse(
        self,
        rows: Sequence[tuple[object, ...]],
    ) -> None:
        if not rows:
            return
        self._bridge.checkpoint()
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        self._budget.require_write_space(page_size=page_size)
        try:
            self._connection.execute("BEGIN")
            self._connection.executemany(
                """UPDATE content_keys SET reusable=1
                WHERE model_signature=? AND role=? AND modality=?
                  AND content_xxh3_128=? AND content_bytes=?
                  AND content_xxh3_64_guard=? AND dimensions=?
                  AND vector_dtype=?""",
                rows,
            )
            self._connection.commit()
            self._budget.checkpoint()
        except sqlite3.Error as exc:
            _cleanup_preserving_primary(
                self._connection.rollback,
                exc,
                label="semantic planner scratch rollback cleanup",
            )
            _raise_scratch_error(exc, self._budget)
        except BaseException as exc:
            _cleanup_preserving_primary(
                self._connection.rollback,
                exc,
                label="semantic planner scratch rollback cleanup",
            )
            raise

    def global_summary(self) -> Mapping[str, int]:
        self.flush()
        row = self._connection.execute(
            """SELECT COUNT(*) AS unique_contents,
            COALESCE(SUM(source_payload_bytes),0) AS unique_input_bytes,
            COALESCE(SUM(source_payload_bytes*occurrences),0) AS input_bytes,
            COALESCE(SUM(reusable),0) AS reusable_unique_contents,
            COALESCE(SUM(CASE WHEN reusable=0 THEN 1 ELSE 0 END),0)
                AS new_unique_contents,
            COALESCE(SUM(CASE WHEN reusable=0 THEN vector_blob_bytes ELSE 0 END),0)
                AS vector_blob_bytes
            FROM content_keys"""
        ).fetchone()
        assert row is not None
        return {name: int(row[name]) for name in row.keys()}

    def workload_summary(self, workload: str) -> Mapping[str, int]:
        self.flush()
        row = self._connection.execute(
            f"""SELECT COUNT(*) AS unique_contents,
            COALESCE(SUM(work.occurrences),0) AS embedding_entities,
            COALESCE(SUM(content.source_payload_bytes),0) AS unique_input_bytes,
            COALESCE(SUM(content.source_payload_bytes*work.occurrences),0)
                AS input_bytes,
            COALESCE(SUM(content.reusable),0) AS preexisting_reusable,
            COALESCE(SUM(CASE WHEN content.reusable=0
                AND content.first_workload=? THEN 1 ELSE 0 END),0) AS new_unique,
            COALESCE(SUM(CASE WHEN content.reusable=0
                AND content.first_workload<>? THEN 1 ELSE 0 END),0)
                AS planned_reusable,
            COALESCE(SUM(CASE WHEN content.reusable=0
                AND content.first_workload=? THEN content.vector_blob_bytes
                ELSE 0 END),0) AS vector_blob_bytes,
            COALESCE(SUM(CASE WHEN content.reusable=0
                THEN work.occurrences ELSE 0 END),0)
                AS uncached_embedding_entities
            FROM workload_keys AS work
            JOIN content_keys AS content USING({_CONTENT_COLUMNS})
            WHERE work.workload=?""",
            (workload, workload, workload, workload),
        ).fetchone()
        assert row is not None
        return {name: int(row[name]) for name in row.keys()}


def _create_scratch_database(
    path: Path,
    budget: _ScratchBudget,
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve(strict=False).as_uri()}?mode=rwc",
        uri=True,
        timeout=60.0,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA page_size=4096")
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        maximum_pages = budget.maximum_bytes // page_size
        if maximum_pages < 1:
            raise ValueError("max_scratch_bytes cannot hold one SQLite page")
        observed_limit = int(
            connection.execute(f"PRAGMA max_page_count={maximum_pages}").fetchone()[0]
        )
        if observed_limit != maximum_pages:
            raise SemanticPlanBlocked(
                "SQLite rejected the requested scratch page limit"
            )
        journal_mode = str(
            connection.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]
        ).lower()
        if journal_mode != "memory":
            raise SemanticPlanBlocked(
                "SQLite could not keep the scratch rollback journal in memory"
            )
        connection.execute("PRAGMA synchronous=OFF")
        cache_kib = max(
            64,
            min(SCRATCH_CACHE_KIB, budget.maximum_bytes // (4 * 1024)),
        )
        connection.execute(f"PRAGMA cache_size=-{cache_kib}")
        connection.execute("PRAGMA temp_store=MEMORY")
        if int(connection.execute("PRAGMA temp_store").fetchone()[0]) != 2:
            raise SemanticPlanBlocked(
                "SQLite could not keep planner temporary tables in memory"
            )
        budget.require_write_space(page_size=page_size)
        connection.executescript(
            f"""CREATE TABLE content_keys(
                model_signature TEXT NOT NULL,
                role TEXT NOT NULL,
                modality TEXT NOT NULL,
                content_xxh3_128 TEXT NOT NULL,
                content_bytes INTEGER NOT NULL CHECK(content_bytes>=0),
                content_xxh3_64_guard TEXT NOT NULL,
                source_payload_bytes INTEGER NOT NULL
                    CHECK(source_payload_bytes>=0),
                first_workload TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK(dimensions>0),
                vector_dtype TEXT NOT NULL,
                vector_blob_bytes INTEGER NOT NULL CHECK(vector_blob_bytes>0),
                occurrences INTEGER NOT NULL CHECK(occurrences>0),
                reusable INTEGER NOT NULL DEFAULT 0 CHECK(reusable IN (0,1)),
                PRIMARY KEY({_CONTENT_COLUMNS})
            ) WITHOUT ROWID;
            CREATE TABLE workload_keys(
                workload TEXT NOT NULL,
                model_signature TEXT NOT NULL,
                role TEXT NOT NULL,
                modality TEXT NOT NULL,
                content_xxh3_128 TEXT NOT NULL,
                content_bytes INTEGER NOT NULL CHECK(content_bytes>=0),
                content_xxh3_64_guard TEXT NOT NULL,
                occurrences INTEGER NOT NULL CHECK(occurrences>0),
                PRIMARY KEY(workload,{_CONTENT_COLUMNS})
            ) WITHOUT ROWID;"""
        )
        connection.commit()
        budget.checkpoint()
        return connection
    except sqlite3.Error as exc:
        _cleanup_preserving_primary(
            connection.close,
            exc,
            label="semantic planner scratch setup close cleanup",
        )
        _raise_scratch_error(exc, budget)
    except BaseException as exc:
        _cleanup_preserving_primary(
            connection.close,
            exc,
            label="semantic planner scratch setup close cleanup",
        )
        raise


__all__ = [
    "CONTENT_BATCH_SIZE",
    "DEFAULT_MAX_SCRATCH_BYTES",
]
# endregion [02]
