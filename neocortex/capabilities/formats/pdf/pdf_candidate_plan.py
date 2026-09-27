"""A frozen, disk-backed metadata selection belonging to the PDF writer.

This is not a public SQLite read mode. Only the route's existing owner can
create/drain this TEMP plan; no connection, cursor or mutable row crosses its
thread. Full text, page blobs, FTS and historical generations are never copied.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Generator, Sequence
from typing import Protocol, cast

from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudgetExceeded
from neocortex.persistence.sqlite_temporary_space import SQLiteTemporarySpace, sqlite_temporary_directory


class _PdfOwner(Protocol):
    def call(self, operation: Callable[[sqlite3.Connection], object]) -> object: ...


PDF_CANDIDATE_PAGE_SIZE = 256


def owned_pdf_candidates(
    owner: _PdfOwner,
    select_sql: str,
    parameters: Sequence[object],
    *,
    cancellation: CancellationToken,
    decode: Callable[[sqlite3.Row], FileSnapshot],
    min_free_bytes: int,
    estimate_sql: str | None = None,
    estimate_parameters: Sequence[object] = (),
    limit: int | None = None,
) -> Generator[FileSnapshot, None, None]:
    """Pin ordered metadata once, with bounded Python/SQLite page caches.

    TEMP storage is disposable and local to this owner connection. SQLite's
    file-backed sorter/plan can grow with the selected generation, not with
    unrelated historical content. A cancellation or failed projection is
    never treated as an empty selection. Closing the generator drops only its
    own TEMP table, including on worker failure.
    """
    last_check = 0.0
    failure: BaseException | None = None
    space = SQLiteTemporarySpace(sqlite_temporary_directory())

    def progress() -> int:
        nonlocal last_check, failure
        try:
            cancellation.checkpoint()
            now = time.monotonic()
            if now - last_check >= 0.1:
                last_check = now
                free = shutil.disk_usage(space.root).free
                if free < min_free_bytes:
                    raise SQLiteSnapshotBudgetExceeded(
                        "disk_space", owner="pdf", operation="candidate_projection",
                        free_bytes=free, required_headroom_bytes=min_free_bytes,
                    )
        except BaseException as exc:
            failure = exc
            return 1
        return 0

    def create(connection: sqlite3.Connection) -> None:
        # The coordinator owns this connection and has no external callback.
        # Do not borrow an untracked connection or change public read policy.
        cancellation.checkpoint()
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA temp.cache_size=-2048")
        connection.set_progress_handler(progress, 1000)
        try:
            if progress():
                assert failure is not None
                raise failure
            query = estimate_sql or (
                "SELECT count(*),coalesce(max(length(CAST(path AS BLOB))+length(file_key)+128),0) "
                f"FROM ({select_sql})"
            )
            count, width = connection.execute(
                query, estimate_parameters if estimate_sql is not None else parameters,
            ).fetchone()
            count = int(count) if limit is None else min(int(count), limit)
            # Reserve the metadata table plus a conservative file-backed sort
            # allowance, not historical page blobs. The estimate grows with
            # selected metadata; it is not an arbitrary corpus-size ceiling.
            required = 64 * 1024 + count * int(width) * 4
            space.reserve(required, checkpoint=cancellation.checkpoint, deadline=time.monotonic() + 60)
            page_size = int(connection.execute("PRAGMA temp.page_size").fetchone()[0])
            connection.execute(f"PRAGMA temp.max_page_count={max(16, required // (2 * page_size))}")
            connection.execute(
                "CREATE TEMP TABLE pdf_candidate_plan("
                "sequence INTEGER PRIMARY KEY, file_key TEXT NOT NULL, path TEXT NOT NULL, "
                "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, birthtime_ns INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO temp.pdf_candidate_plan(file_key,path,size,mtime_ns,birthtime_ns) "
                + select_sql,
                parameters,
            )
            actual = int(connection.execute("PRAGMA temp.page_count").fetchone()[0]) * page_size
            space.observe(actual)
        except sqlite3.Error as exc:
            if failure is not None:
                raise failure from exc
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                page_size = int(connection.execute("PRAGMA temp.page_size").fetchone()[0])
                allocated = int(connection.execute("PRAGMA temp.page_count").fetchone()[0]) * page_size
                allowed = int(connection.execute("PRAGMA temp.max_page_count").fetchone()[0]) * page_size
                free = shutil.disk_usage(space.root).free
                raise SQLiteSnapshotBudgetExceeded(
                    "disk_space" if free < page_size else "temporary_bytes",
                    owner="pdf", operation="candidate_projection",
                    reserved_bytes=space.reserved, temporary_root=space.root,
                    allowed_bytes=allowed, allocated_bytes=allocated,
                    required_bytes="unknown_after_sqlite_rollback", free_bytes=free,
                    origin="filesystem_full" if free < page_size else "page_ceiling_or_storage_quota",
                ) from exc
            raise
        finally:
            connection.set_progress_handler(None, 0)

    def drop(connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE IF EXISTS temp.pdf_candidate_plan")

    try:
        owner.call(create)
        sequence = 0
        while True:
            cancellation.checkpoint()

            def page(connection: sqlite3.Connection, after: int = sequence) -> list[sqlite3.Row]:
                cancellation.checkpoint()
                connection.set_progress_handler(progress, 1000)
                try:
                    return connection.execute(
                        "SELECT sequence,file_key,path,size,mtime_ns,birthtime_ns "
                        "FROM temp.pdf_candidate_plan WHERE sequence>? "
                        "ORDER BY sequence LIMIT ?",
                        (after, PDF_CANDIDATE_PAGE_SIZE),
                    ).fetchall()
                except sqlite3.Error as exc:
                    if failure is not None:
                        raise failure from exc
                    raise
                finally:
                    connection.set_progress_handler(None, 0)

            rows = cast(list[sqlite3.Row], owner.call(page))
            if not rows:
                return
            for row in rows:
                cancellation.checkpoint()
                yield decode(row)
            sequence = int(rows[-1]["sequence"])
    finally:
        primary = sys.exception()
        try:
            owner.call(drop)
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note(f"PDF candidate plan cleanup failed: {cleanup_error}")
        finally:
            space.close()
