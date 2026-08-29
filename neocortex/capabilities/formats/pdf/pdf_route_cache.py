"""Incremental PDF cache policy, touch, resumption and bounded pruning."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import sqlite3
import time

from neocortex.deduplication import DedupIndex, FileChangedError, FileSnapshot
from neocortex.platform_policy import sqlite_path_collation

from _04_Nucleo_Operativo.cancellation import CancellationToken
from _04_Nucleo_Operativo.file_identity import file_key_from_snapshot as file_key
from .pdf_cache import binary_fingerprint
from .pdf_route_models import (
    PDF_PAGE_SEQUENCE_ERROR_LIMIT,
    CacheDecision,
    PdfRouteConfig,
)
from .pdf_state import UNKNOWN_BIRTHTIME_NS, pdf_database
from .pdf_writer import serialized_pdf_write
from _04_Nucleo_Operativo.retry_policy import (
    PDF_RETRYABLE_PAGE_ERROR_SQL,
    automatic_retry_due,
    is_retryable_pdf_document_error,
)


# region [01] Cache constants and host contract

PDF_CACHE_PRUNE_BATCH = 256
PDF_CACHE_TOUCH_BATCH = 256
MAX_RETRY_PAGE_SET = 10_000
_PATH_COLLATION = sqlite_path_collation()
# Compatibility name retained for the storage mixin and older integrations.
RETRYABLE_PAGE_ERROR_SQL = PDF_RETRYABLE_PAGE_ERROR_SQL
CACHE_QUERY = f"""SELECT size,mtime_ns,birthtime_ns,processing_signature,status,
    binary_xxh3_128,page_count,completed_pages,page_errors_count,error_type,
    error_message,metadata_json,transient_retry_count,next_retry_ns,
    (SELECT COUNT(*) FROM pages p WHERE p.file_key=documents.file_key)
        AS persisted_page_count,
    (SELECT COUNT(*) FROM page_errors e WHERE e.file_key=documents.file_key
        AND e.processing_signature=documents.processing_signature)
        AS persisted_page_error_count,
    EXISTS(SELECT 1 FROM page_errors e WHERE e.file_key=documents.file_key
        AND e.processing_signature=documents.processing_signature
        AND {RETRYABLE_PAGE_ERROR_SQL}) AS has_retryable_page_error,
    CASE WHEN documents.status='partial' THEN (SELECT e.error_type FROM page_errors e
        WHERE e.file_key=documents.file_key
        AND e.processing_signature=documents.processing_signature
        ORDER BY e.page_number DESC LIMIT 1) END AS latest_page_error_type,
    CASE WHEN documents.status='partial' THEN (SELECT e.error_message FROM page_errors e
        WHERE e.file_key=documents.file_key
        AND e.processing_signature=documents.processing_signature
        ORDER BY e.page_number DESC LIMIT 1) END AS latest_page_error_message
    FROM documents WHERE file_key=?"""


class PdfRouteCacheMixin:
    """Cache responsibilities shared by the route without owning orchestration."""

    config: PdfRouteConfig
    index: DedupIndex
    cancellation: CancellationToken
    run_id: int

    # endregion [01]

    # region [02] Bounded stale-state pruning

    def _reconcile_legacy_recovered_documents(self) -> int:
        """Promote complete legacy recoveries that were mislabeled partial."""

        promoted = 0
        with serialized_pdf_write(), pdf_database(self.config.state_path) as connection:
            while True:
                self.cancellation.checkpoint()
                rows = connection.execute(
                    """SELECT d.file_key FROM documents d
                    WHERE d.status='partial' AND d.is_partial=0
                    AND d.page_count>0 AND d.completed_pages=d.page_count
                    AND d.page_errors_count=0 AND d.error_type IS NULL
                    AND instr(d.metadata_json,'\"neocortex_recovery\"')>0
                    AND (SELECT COUNT(*) FROM pages p WHERE p.file_key=d.file_key)
                        =d.completed_pages
                    ORDER BY d.file_key LIMIT 256"""
                ).fetchall()
                if not rows:
                    return promoted
                keys = tuple((str(row["file_key"]),) for row in rows)
                now = time.time_ns()
                connection.executemany(
                    """UPDATE documents SET status='done',is_partial=0,
                    transient_retry_count=0,next_retry_ns=NULL,updated_ns=?
                    WHERE file_key=?""",
                    ((now, key[0]) for key in keys),
                )
                connection.executemany(
                    "DELETE FROM page_staging WHERE file_key=?",
                    keys,
                )
                connection.commit()
                promoted += len(keys)

    def _reconcile_interrupted_documents(self) -> tuple[int, int]:
        """Recover rows left in ``processing`` by an earlier interrupted run.

        A complete persisted page layer is restored as a cacheable document.
        Incomplete staging becomes an explicitly resumable partial document.
        Rows owned by the current run are never touched.
        """

        completed = partial = 0
        now = time.time_ns()
        with serialized_pdf_write(), pdf_database(self.config.state_path) as connection:
            rows = connection.execute(
                """SELECT d.file_key,d.page_count,d.completed_pages,
                d.processing_signature,
                (SELECT COUNT(*) FROM pages p WHERE p.file_key=d.file_key)
                    AS persisted_pages,
                (SELECT COUNT(*) FROM page_staging s WHERE s.file_key=d.file_key
                    AND s.processing_signature=d.processing_signature
                    AND s.source<>'error') AS staged_pages,
                (SELECT COUNT(*) FROM page_errors e WHERE e.file_key=d.file_key
                    AND e.processing_signature=d.processing_signature)
                    AS persisted_errors
                FROM documents d JOIN pdf_inventory i ON i.file_key=d.file_key
                WHERE d.status='processing' AND i.last_seen_run_id=?
                AND COALESCE(d.last_seen_run_id,-1)<>?""",
                (self.run_id, self.run_id),
            ).fetchall()
            for row in rows:
                self.cancellation.checkpoint()
                page_count = int(row["page_count"] or 0)
                completed_pages = int(row["completed_pages"] or 0)
                persisted_pages = int(row["persisted_pages"] or 0)
                staged_pages = int(row["staged_pages"] or 0)
                persisted_errors = int(row["persisted_errors"] or 0)
                if (
                    page_count > 0
                    and completed_pages == page_count
                    and persisted_pages == page_count
                    and staged_pages == 0
                    and persisted_errors == 0
                ):
                    connection.execute(
                        """UPDATE documents SET status='done',is_partial=0,
                        error_type=NULL,error_message=NULL,
                        transient_retry_count=0,next_retry_ns=NULL,updated_ns=?
                        WHERE file_key=? AND status='processing'""",
                        (now, row["file_key"]),
                    )
                    completed += 1
                    continue
                connection.execute(
                    """UPDATE documents SET status='partial',is_partial=1,
                    completed_pages=?,error_type='InterruptedPdfProcessing',
                    error_message=?,transient_retry_count=0,next_retry_ns=NULL,
                    updated_ns=? WHERE file_key=? AND status='processing'""",
                    (
                        staged_pages,
                        f"[durable-progress:{staged_pages}] previous run interrupted",
                        now,
                        row["file_key"],
                    ),
                )
                partial += 1
        return completed, partial

    def _prune_pdf_cache(self) -> tuple[int, int]:
        """Remove only stale cache rows after all selected PDF work succeeds."""

        self.cancellation.checkpoint()
        documents_pruned = rows_pruned = 0
        with serialized_pdf_write(), pdf_database(self.config.state_path) as connection:
            connection.execute(
                """CREATE TEMP TABLE IF NOT EXISTS stale_pdf_keys(
                file_key TEXT PRIMARY KEY) WITHOUT ROWID"""
            )
            connection.execute("DELETE FROM stale_pdf_keys")
            connection.execute(
                """INSERT OR IGNORE INTO stale_pdf_keys(file_key)
                SELECT d.file_key FROM documents d
                LEFT JOIN pdf_inventory i ON i.file_key=d.file_key
                AND i.last_seen_run_id=? AND i.size=d.size AND i.mtime_ns=d.mtime_ns
                AND (i.birthtime_ns=d.birthtime_ns
                    OR d.birthtime_ns=-1)
                WHERE i.file_key IS NULL""",
                (self.run_id,),
            )
            rows_pruned += self._prune_stale_fts_rows(connection)
            while True:
                self.cancellation.checkpoint()
                keys = [
                    str(row[0])
                    for row in connection.execute(
                        """SELECT file_key FROM stale_pdf_keys
                        ORDER BY file_key LIMIT ?""",
                        (PDF_CACHE_PRUNE_BATCH,),
                    ).fetchall()
                ]
                if not keys:
                    break
                for key in keys:
                    self.cancellation.checkpoint()
                    rows_pruned += self._delete_document_cache(connection, key)
                    documents_pruned += int(
                        connection.execute(
                            "DELETE FROM documents WHERE file_key=?", (key,)
                        ).rowcount
                    )
                connection.executemany(
                    "DELETE FROM stale_pdf_keys WHERE file_key=?",
                    ((key,) for key in keys),
                )
                connection.commit()

            connection.execute(
                f"""UPDATE documents SET
                path=(SELECT i.path FROM pdf_inventory i WHERE i.file_key=documents.file_key),
                updated_ns=? WHERE EXISTS(
                    SELECT 1 FROM pdf_inventory i WHERE i.file_key=documents.file_key
                    AND i.last_seen_run_id=? AND i.size=documents.size
                    AND i.mtime_ns=documents.mtime_ns
                    AND (i.birthtime_ns=documents.birthtime_ns
                        OR documents.birthtime_ns=-1)
                    AND i.path<>documents.path COLLATE {_PATH_COLLATION})""",
                (time.time_ns(), self.run_id),
            )
            connection.execute(
                f"""UPDATE page_fts SET path=(SELECT d.path FROM documents d
                WHERE d.file_key=page_fts.file_key) WHERE EXISTS(
                SELECT 1 FROM documents d WHERE d.file_key=page_fts.file_key
                AND d.path<>page_fts.path COLLATE {_PATH_COLLATION})"""
            )
            while True:
                self.cancellation.checkpoint()
                old_inventory = connection.execute(
                    "SELECT file_key FROM pdf_inventory WHERE last_seen_run_id<>? LIMIT 1000",
                    (self.run_id,),
                ).fetchall()
                if not old_inventory:
                    break
                rows_pruned += int(
                    connection.executemany(
                        "DELETE FROM pdf_inventory WHERE file_key=?", old_inventory
                    ).rowcount
                )
                connection.commit()
            keep_runs = {
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT relation_run_id FROM similarity_state"
                )
            }
            history_specs = (
                (
                    "similarity_relations",
                    ("run_id", "file_key_a", "file_key_b", "kind"),
                ),
                (
                    "similarity_buckets",
                    ("run_id", "signature_kind", "band", "bucket", "file_key"),
                ),
                (
                    "layout_group_members",
                    ("relation_run_id", "group_key", "file_key"),
                ),
                (
                    "layout_groups",
                    ("relation_run_id", "group_key"),
                ),
            )
            if keep_runs:
                placeholders = ",".join("?" for _ in keep_runs)
                for table, columns in history_specs:
                    while True:
                        self.cancellation.checkpoint()
                        names = ",".join(columns)
                        run_column = columns[0]
                        rows = connection.execute(
                            f"SELECT {names} FROM {table} WHERE {run_column} NOT IN "
                            f"({placeholders}) LIMIT 1000",
                            tuple(sorted(keep_runs)),
                        ).fetchall()
                        if not rows:
                            break
                        predicate = " AND ".join(f"{name}=?" for name in columns)
                        rows_pruned += int(
                            connection.executemany(
                                f"DELETE FROM {table} WHERE {predicate}", rows
                            ).rowcount
                        )
                        connection.commit()
            else:
                for table, columns in history_specs:
                    while True:
                        names = ",".join(columns)
                        rows = connection.execute(
                            f"SELECT {names} FROM {table} LIMIT 1000"
                        ).fetchall()
                        if not rows:
                            break
                        predicate = " AND ".join(f"{name}=?" for name in columns)
                        rows_pruned += int(
                            connection.executemany(
                                f"DELETE FROM {table} WHERE {predicate}", rows
                            ).rowcount
                        )
                        connection.commit()
        return documents_pruned, rows_pruned

    def _prune_stale_fts_rows(self, connection: sqlite3.Connection) -> int:
        """Scan FTS5 once, then delete stale rowids in bounded transactions."""

        connection.execute(
            """CREATE TEMP TABLE IF NOT EXISTS stale_fts_rowids(
            rowid INTEGER PRIMARY KEY) WITHOUT ROWID"""
        )
        connection.execute("DELETE FROM stale_fts_rowids")
        connection.execute(
            """INSERT OR IGNORE INTO stale_fts_rowids(rowid)
            SELECT f.rowid FROM page_fts f WHERE EXISTS(
                SELECT 1 FROM stale_pdf_keys s WHERE s.file_key=f.file_key)"""
        )
        connection.commit()
        removed = 0
        while True:
            rowids = connection.execute(
                "SELECT rowid FROM stale_fts_rowids ORDER BY rowid LIMIT 500"
            ).fetchall()
            if not rowids:
                return removed
            removed += int(
                connection.executemany("DELETE FROM page_fts WHERE rowid=?", rowids).rowcount
            )
            connection.executemany("DELETE FROM stale_fts_rowids WHERE rowid=?", rowids)
            connection.commit()

    def _delete_document_cache(
        self,
        connection: sqlite3.Connection,
        cache_key: str,
    ) -> int:
        """Delete one document's dependent rows with bounded commits."""

        removed = 0
        specs = (
            ("page_fts", ("rowid",), "file_key=?"),
            ("page_fts_state", ("file_key", "page_number"), "file_key=?"),
            (
                "page_staging",
                ("file_key", "processing_signature", "page_number"),
                "file_key=?",
            ),
            (
                "page_errors",
                ("file_key", "processing_signature", "page_number"),
                "file_key=?",
            ),
            (
                "document_warnings",
                ("file_key", "processing_signature", "stage"),
                "file_key=?",
            ),
            ("pages", ("file_key", "page_number"), "file_key=?"),
            ("page_layouts", ("file_key", "page_number"), "file_key=?"),
            ("document_layouts", ("file_key",), "file_key=?"),
            (
                "layout_group_members",
                ("relation_run_id", "group_key", "file_key"),
                "file_key=?",
            ),
            (
                "similarity_buckets",
                ("run_id", "signature_kind", "band", "bucket", "file_key"),
                "file_key=?",
            ),
            (
                "similarity_relations",
                ("run_id", "file_key_a", "file_key_b", "kind"),
                "file_key_a=? OR file_key_b=?",
            ),
        )
        for table, columns, where in specs:
            parameters = (cache_key, cache_key) if " OR " in where else (cache_key,)
            while True:
                self.cancellation.checkpoint()
                names = ",".join(columns)
                rows = connection.execute(
                    f"SELECT {names} FROM {table} WHERE {where} LIMIT 500",
                    parameters,
                ).fetchall()
                if not rows:
                    break
                predicate = " AND ".join(f"{name}=?" for name in columns)
                removed += int(
                    connection.executemany(f"DELETE FROM {table} WHERE {predicate}", rows).rowcount
                )
                connection.commit()
        removed += int(
            connection.execute(
                "DELETE FROM text_signatures WHERE file_key=?",
                (cache_key,),
            ).rowcount
        )
        return removed

    # endregion [02]

    # region [03] Validation, touch and resumption

    def _page_bounds(self, page_count: int) -> tuple[int, int]:
        start = 0 if self.config.page_start is None else self.config.page_start - 1
        end = page_count if self.config.page_end is None else min(page_count, self.config.page_end)
        if self.config.max_pages is not None:
            end = min(end, start + self.config.max_pages)
        if start >= page_count:
            raise ValueError(
                f"page range starts at {start + 1}, but document has {page_count} pages"
            )
        return start, end

    def _is_cache_hit(
        self,
        snapshot: FileSnapshot,
        *,
        connection=None,
        touch: bool = True,
    ) -> CacheDecision:
        row = self._read_cache_row(snapshot, connection)
        if row is None:
            return CacheDecision(False)
        prior_status = str(row["status"])
        retry_pages = self._cached_retry_page_count(row, prior_status)
        invalid = CacheDecision(False, prior_status, retry_pages)
        legacy_verified = False
        if not self._cached_snapshot_matches(row, snapshot):
            if not self._cached_legacy_snapshot_matches(row, snapshot):
                return invalid
            if not self._promote_legacy_birthtime(row, snapshot, connection):
                return invalid
            legacy_verified = True
        if not self._cached_layers_are_consistent(row):
            return invalid
        if not legacy_verified and not self._cached_fingerprint_matches(row, snapshot):
            return invalid
        if touch:
            self._touch_cache_snapshot(snapshot, connection)
        return self._cached_status_decision(row, prior_status, retry_pages)

    def _read_cache_row(self, snapshot: FileSnapshot, connection):
        if connection is None:
            with pdf_database(self.config.state_path) as owned_connection:
                return owned_connection.execute(
                    CACHE_QUERY,
                    (file_key(snapshot),),
                ).fetchone()
        return connection.execute(
            CACHE_QUERY,
            (file_key(snapshot),),
        ).fetchone()

    def _cached_snapshot_matches(self, row, snapshot: FileSnapshot) -> bool:
        return (
            int(row["size"]) == snapshot.size
            and int(row["mtime_ns"]) == snapshot.mtime_ns
            and int(row["birthtime_ns"]) == snapshot.birthtime_ns
            and row["processing_signature"] == self.config.processing_signature
        )

    def _cached_legacy_snapshot_matches(self, row, snapshot: FileSnapshot) -> bool:
        return (
            int(row["size"]) == snapshot.size
            and int(row["mtime_ns"]) == snapshot.mtime_ns
            and int(row["birthtime_ns"]) == UNKNOWN_BIRTHTIME_NS
            and row["processing_signature"] == self.config.processing_signature
        )

    def _promote_legacy_birthtime(
        self,
        row,
        snapshot: FileSnapshot,
        connection,
    ) -> bool:
        legacy_digest = row["binary_xxh3_128"]
        if not legacy_digest:
            return False
        self.cancellation.checkpoint()
        try:
            current_digest = binary_fingerprint(
                self.index,
                snapshot,
                required=True,
                refresh=False,
            )
        except FileChangedError:
            self.cancellation.checkpoint()
            return False
        self.cancellation.checkpoint()
        if current_digest != legacy_digest:
            return False

        def promote(target: sqlite3.Connection) -> bool:
            with serialized_pdf_write():
                updated = target.execute(
                    """UPDATE documents SET birthtime_ns=?,updated_ns=?
                    WHERE file_key=? AND birthtime_ns=? AND size=? AND mtime_ns=?
                    AND processing_signature=? AND binary_xxh3_128=?""",
                    (
                        snapshot.birthtime_ns,
                        time.time_ns(),
                        file_key(snapshot),
                        UNKNOWN_BIRTHTIME_NS,
                        snapshot.size,
                        snapshot.mtime_ns,
                        self.config.processing_signature,
                        legacy_digest,
                    ),
                ).rowcount
            return updated == 1

        if connection is not None:
            # The route already batches cache touches. Defer this write to that
            # batch so a verified legacy hit never holds SQLite's single-writer
            # lock while extraction workers use their own connections.
            return True
        with pdf_database(self.config.state_path) as owned_connection:
            promoted = promote(owned_connection)
        self.cancellation.checkpoint()
        return promoted

    @staticmethod
    def _cached_retry_page_count(row, prior_status: str) -> int:
        return int(row["persisted_page_error_count"]) if prior_status == "partial" else 0

    @staticmethod
    def _cached_layers_are_consistent(row) -> bool:
        return row["status"] not in {"done", "partial"} or not (
            int(row["persisted_page_count"]) != int(row["completed_pages"])
            or int(row["persisted_page_error_count"]) != int(row["page_errors_count"])
        )

    def _cached_fingerprint_matches(self, row, snapshot: FileSnapshot) -> bool:
        if self.config.cache_validation != "full":
            return True
        current = binary_fingerprint(
            self.index,
            snapshot,
            required=True,
            refresh=True,
        )
        return row["binary_xxh3_128"] is not None and row["binary_xxh3_128"] == current

    def _touch_cache_snapshot(self, snapshot: FileSnapshot, connection) -> None:
        if connection is None:
            with pdf_database(self.config.state_path) as owned_connection:
                self._touch_cache_hits(owned_connection, [snapshot])
            return
        self._touch_cache_hits(connection, [snapshot])

    def _cached_retry_explicitly_requested(self, status: str) -> bool:
        return (self.config.retry_errors and status in {"error", "protected", "partial"}) or (
            self.config.selection.force_incomplete_retry
            and status in {"error", "protected", "partial", "processing"}
        )

    def _cached_structural_revalidation_required(
        self,
        status: str,
        error_type: str,
    ) -> bool:
        return (
            self.config.apply_actions
            and status == "error"
            and error_type == "PdfStructuralRecoveryFailed"
        )

    @staticmethod
    def _cached_page_sequence_retry_required(
        row,
        status: str,
        error_type: str,
    ) -> bool:
        if status != "partial":
            return False
        return error_type == "PdfPageSequenceAborted" or (
            not error_type
            and int(row["persisted_page_error_count"]) >= PDF_PAGE_SEQUENCE_ERROR_LIMIT
        )

    @staticmethod
    def _cached_timeout_retry_required(
        row,
        status: str,
        error_type: str,
        error_message: str,
    ) -> bool:
        if status not in {"error", "partial"} or error_type != "PdfDocumentTimeout":
            return False
        if error_message.startswith("[durable-progress:"):
            return True
        timeout_policy_recorded = error_message.startswith("[no-durable-progress:")
        return not timeout_policy_recorded and 0 < int(row["completed_pages"]) < int(
            row["page_count"]
        )

    @staticmethod
    def _cached_automatic_retry_required(
        row,
        status: str,
        error_type: str,
        error_message: str,
    ) -> bool:
        retry_due = automatic_retry_due(
            int(row["transient_retry_count"]),
            None if row["next_retry_ns"] is None else int(row["next_retry_ns"]),
        )
        if not retry_due:
            return False
        if status == "partial" and bool(row["has_retryable_page_error"]):
            return True
        return status in {"error", "partial"} and is_retryable_pdf_document_error(
            error_type,
            error_message,
        )

    def _cached_policy_retry_required(
        self,
        row,
        status: str,
        error_type: str,
        error_message: str,
    ) -> bool:
        if self._cached_structural_revalidation_required(status, error_type):
            # The destructive policy is explicit and scoped. Revalidate all
            # recovery engines and the source snapshot before recycling.
            return True
        if self._cached_page_sequence_retry_required(row, status, error_type):
            # One bounded structural-repair pass. Legacy rows have no document
            # marker, but retain the exact consecutive page-error evidence.
            return True
        if self._cached_timeout_retry_required(
            row,
            status,
            error_type,
            error_message,
        ):
            # Persisted durable progress and legacy unmarked partial progress
            # get another attempt; recorded no-progress uses bounded backoff.
            return True
        return self._cached_automatic_retry_required(
            row,
            status,
            error_type,
            error_message,
        )

    def _cached_status_decision(
        self,
        row,
        prior_status: str,
        retry_pages: int,
    ) -> CacheDecision:
        status = row["status"]
        if status == "done":
            return self._cache_hit_with_review_evidence(
                row,
                prior_status,
                retry_pages,
            )
        if self._cached_retry_explicitly_requested(status):
            return CacheDecision(False, prior_status, retry_pages)
        error_type = str(row["error_type"] or "")
        error_message = str(row["error_message"] or "")
        if self._cached_policy_retry_required(
            row,
            status,
            error_type,
            error_message,
        ):
            return CacheDecision(False, prior_status, retry_pages)
        if status in {"error", "protected", "partial"}:
            return self._cache_hit_with_review_evidence(
                row,
                prior_status,
                retry_pages,
            )
        return CacheDecision(False, prior_status, retry_pages)

    @staticmethod
    def _cache_hit_with_review_evidence(
        row,
        prior_status: str,
        retry_pages: int,
    ) -> CacheDecision:
        """Carry bounded persisted diagnostics with a cache decision."""

        return CacheDecision(
            True,
            prior_status,
            retry_pages,
            error_type=(None if row["error_type"] is None else str(row["error_type"])),
            error_message=(None if row["error_message"] is None else str(row["error_message"])),
            metadata_json=(None if row["metadata_json"] is None else str(row["metadata_json"])),
            page_error_type=(
                None
                if row["latest_page_error_type"] is None
                else str(row["latest_page_error_type"])
            ),
            page_error_message=(
                None
                if row["latest_page_error_message"] is None
                else str(row["latest_page_error_message"])
            ),
        )

    def _touch_cache_hits(
        self,
        connection,
        snapshots: list[FileSnapshot],
        *,
        cancellable: bool = True,
    ) -> None:
        if not snapshots:
            return
        if cancellable:
            self.cancellation.checkpoint()
        now = time.time_ns()
        with serialized_pdf_write():
            stale_path_owners = self._stale_path_owner_keys(
                connection,
                snapshots,
            )
            for stale_key in stale_path_owners:
                if cancellable:
                    self.cancellation.checkpoint()
                self._delete_document_cache(connection, stale_key)
                connection.execute(
                    "DELETE FROM documents WHERE file_key=?",
                    (stale_key,),
                )
                connection.commit()
            keys = [file_key(snapshot) for snapshot in snapshots]
            placeholders = ",".join("?" for _ in keys)
            previous_paths = dict(
                connection.execute(
                    f"SELECT file_key,path FROM documents WHERE file_key IN ({placeholders})",
                    keys,
                )
            )
            renamed = [
                (snapshot.path, key)
                for snapshot, key in zip(snapshots, keys, strict=True)
                if previous_paths.get(key) != snapshot.path
            ]
            connection.executemany(
                "UPDATE documents SET path=?,birthtime_ns=?,last_seen_run_id=?,updated_ns=? "
                "WHERE file_key=?",
                (
                    (snapshot.path, snapshot.birthtime_ns, self.run_id, now, key)
                    for snapshot, key in zip(snapshots, keys, strict=True)
                ),
            )
            if renamed:
                connection.executemany(
                    "UPDATE page_fts SET path=? WHERE file_key=?",
                    renamed,
                )
            connection.commit()
        if cancellable:
            self.cancellation.checkpoint()

    def _stale_path_owner_keys(
        self,
        connection: sqlite3.Connection,
        snapshots: list[FileSnapshot],
    ) -> tuple[str, ...]:
        """Classify path owners before a cache-hit rename mutates any row."""

        if not snapshots:
            return ()
        incoming_values = ",".join("(?,?)" for _snapshot in snapshots)
        parameters = tuple(
            value for snapshot in snapshots for value in (snapshot.path, file_key(snapshot))
        )
        conflicts = connection.execute(
            f"""WITH incoming(path,file_key) AS (VALUES {incoming_values})
            SELECT incoming.file_key AS incoming_key,d.file_key,EXISTS(
                SELECT 1 FROM pdf_inventory i
                WHERE i.file_key=d.file_key AND i.last_seen_run_id={int(self.run_id)}
                AND i.size=d.size AND i.mtime_ns=d.mtime_ns
                AND (i.birthtime_ns=d.birthtime_ns
                    OR d.birthtime_ns={UNKNOWN_BIRTHTIME_NS})
            ) AS owner_is_live
            FROM incoming JOIN documents d
              ON d.path=incoming.path COLLATE {_PATH_COLLATION}
             AND d.file_key<>incoming.file_key
            ORDER BY incoming.file_key,d.file_key""",
            parameters,
        ).fetchall()
        stale: dict[str, None] = {}
        for row in conflicts:
            owner_key = str(row["file_key"])
            if bool(row["owner_is_live"]):
                raise RuntimeError(
                    "cached PDF path is still owned by a live inventory identity: "
                    f"incoming_key={row['incoming_key']} owner_key={owner_key}"
                )
            stale[owner_key] = None
        return tuple(stale)

    def _resumable_pages(
        self,
        snapshot: FileSnapshot,
    ) -> tuple[int, frozenset[int], int]:
        key = file_key(snapshot)
        signature = self.config.processing_signature
        range_start = 0 if self.config.page_start is None else self.config.page_start - 1
        with pdf_database(self.config.state_path) as connection:
            row = connection.execute(
                "SELECT size,mtime_ns,birthtime_ns,processing_signature,status,"
                "transient_retry_count,next_retry_ns FROM documents "
                "WHERE file_key=?",
                (key,),
            ).fetchone()
            if row is None or (
                int(row["size"]) != snapshot.size
                or int(row["mtime_ns"]) != snapshot.mtime_ns
                or int(row["birthtime_ns"]) != snapshot.birthtime_ns
                or row["processing_signature"] != signature
            ):
                return range_start, frozenset(), 0
            prior_ocr_pages = int(
                connection.execute(
                    "SELECT COUNT(*) FROM page_staging WHERE file_key=? "
                    "AND processing_signature=? AND source='ocr'",
                    (key, signature),
                ).fetchone()[0]
            )
            automatic_page_retry = bool(
                connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM page_errors WHERE file_key=? "
                    "AND processing_signature=? AND " + RETRYABLE_PAGE_ERROR_SQL + ")",
                    (key, signature),
                ).fetchone()[0]
            ) and automatic_retry_due(
                int(row["transient_retry_count"]),
                None if row["next_retry_ns"] is None else int(row["next_retry_ns"]),
            )
            if (self.config.retry_errors or automatic_page_retry) and row["status"] in {
                "partial",
                "error",
            }:
                failed_rows = connection.execute(
                    "SELECT page_number FROM page_errors WHERE file_key=? "
                    "AND processing_signature=? ORDER BY page_number LIMIT ?",
                    (key, signature, MAX_RETRY_PAGE_SET + 1),
                ).fetchall()
                if 0 < len(failed_rows) <= MAX_RETRY_PAGE_SET:
                    return (
                        range_start,
                        frozenset(int(item[0]) for item in failed_rows),
                        prior_ocr_pages,
                    )
            completed = connection.execute(
                "SELECT MAX(page_number) FROM page_staging WHERE file_key=? "
                "AND processing_signature=? AND source<>'error'",
                (key, signature),
            ).fetchone()[0]
        skip_before = range_start if completed is None else int(completed) + 1
        return skip_before, frozenset(), prior_ocr_pages

    @staticmethod
    def _is_transient_error(error_type: str, error_message: str) -> bool:
        return is_retryable_pdf_document_error(error_type, error_message)


# endregion [03]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_route_cache")
