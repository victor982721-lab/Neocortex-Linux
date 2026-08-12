"""Derived PDF indexes built from persisted page text and document structure."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/pdf_derived.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import re
import sqlite3
import time
import zlib
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import xxhash

from _03_Progreso import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress
from neocortex.platform_policy import sqlite_path_collation

from .cancellation import CancellationRequested, CancellationToken
from .pdf_derived_queries import (
    list_layout_groups as list_layout_groups,
    search_pdf_state as search_pdf_state,
)
from .pdf_isolation import _mupdf_warning_summary, stream_isolated_profiles
from .pdf_derived_schema import (
    initialize_derived_schema as initialize_derived_schema,
)
from .pdf_layout import (
    LAYOUT_VERSION,
    add_feature as _add_layout_feature,
    add_signature as _add_layout_signature,
    finish_signature as _finish_layout_signature,
    signature_similarity as _layout_similarity,
)
from .pdf_profile import profile_page as _profile_page
from .pdf_runtime import PdfResourceGate, ensure_free_space
from .pdf_state import pdf_database
from .pdf_writer import serialized_pdf_write
# endregion [01]

# region [02] Implementación


PROFILE_VERSION = 2
SIMILARITY_VERSION = 2
SHINGLE_TOKENS = 5
SIMHASH_BITS = 64
SIMILARITY_BANDS = 8
SIMILARITY_BAND_BITS = SIMHASH_BITS // SIMILARITY_BANDS
MAX_BUCKET_MEMBERS = 128
MAX_CANDIDATES_PER_DOCUMENT = 256
TOKEN_RE = re.compile(r"\w+", re.UNICODE)
PROFILE_PROGRESS_INTERVAL_SECONDS = 1.0
_PATH_COLLATION = sqlite_path_collation()


@dataclass(frozen=True, slots=True)
class PdfDerivedSummary:
    fts_pages_indexed: int = 0
    text_signatures_built: int = 0
    profiles_built: int = 0
    profile_errors: int = 0
    text_similarity_pairs: int = 0
    template_similarity_pairs: int = 0
    layout_similarity_pairs: int = 0
    layout_groups: int = 0
    layout_pages_mapped: int = 0
    fts_elapsed_ns: int = 0
    text_signatures_elapsed_ns: int = 0
    profiles_elapsed_ns: int = 0
    text_similarity_elapsed_ns: int = 0
    template_similarity_elapsed_ns: int = 0
    layout_similarity_elapsed_ns: int = 0
    fts_rows_repaired: int = 0


@contextmanager
def _database(path: Path):
    with pdf_database(path) as connection:
        yield connection


def _simhash(counters: list[int]) -> int:
    value = 0
    for bit, count in enumerate(counters):
        if count >= 0:
            value |= 1 << bit
    return value


def _add_feature(counters: list[int], feature: str, weight: int = 1) -> None:
    value = xxhash.xxh3_64_intdigest(feature.encode("utf-8"))
    for bit in range(SIMHASH_BITS):
        counters[bit] += weight if value & (1 << bit) else -weight


def _similarity(left: int, right: int) -> float:
    return 1.0 - ((left ^ right).bit_count() / SIMHASH_BITS)


class PdfDerivedIndexer:
    def __init__(
        self,
        state_path: Path,
        run_id: int,
        *,
        workers: int,
        similarity_threshold: float,
        profile_timeout_seconds: float | None = None,
        min_free_bytes: int = 0,
        resource_gate: PdfResourceGate | None = None,
        profile_memory_bytes: int = 512 * 1024 * 1024,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ):
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("PDF similarity threshold must be between 0 and 1")
        self.state_path = state_path
        self.run_id = run_id
        self.workers = max(1, workers)
        self.threshold = similarity_threshold
        self.profile_timeout_seconds = profile_timeout_seconds
        self.min_free_bytes = min_free_bytes
        self.resource_gate = resource_gate
        self.profile_memory_bytes = profile_memory_bytes
        self.progress = progress
        self.cancellation = cancellation or CancellationToken()

    def _check_disk(self) -> None:
        ensure_free_space(self.state_path, self.min_free_bytes)

    def _checkpoint_if_configured(self) -> None:
        cancellation = getattr(self, "cancellation", None)
        if cancellation is not None:
            cancellation.checkpoint()

    def run(self) -> PdfDerivedSummary:
        self.cancellation.checkpoint()
        self._check_disk()
        started = time.perf_counter_ns()
        fts_pages, fts_repaired = self._index_fts()
        fts_elapsed = time.perf_counter_ns() - started
        self.cancellation.checkpoint()
        self._check_disk()
        started = time.perf_counter_ns()
        text_signatures = self._build_text_signatures()
        text_signatures_elapsed = time.perf_counter_ns() - started
        self.cancellation.checkpoint()
        self._check_disk()
        started = time.perf_counter_ns()
        profiles, profile_errors = self._build_profiles()
        profiles_elapsed = time.perf_counter_ns() - started
        self.cancellation.checkpoint()
        self._check_disk()
        started = time.perf_counter_ns()
        text_pairs = self._build_similarity("text")
        text_similarity_elapsed = time.perf_counter_ns() - started
        self.cancellation.checkpoint()
        self._check_disk()
        started = time.perf_counter_ns()
        template_pairs = self._build_similarity("template")
        template_similarity_elapsed = time.perf_counter_ns() - started
        self.cancellation.checkpoint()
        self._check_disk()
        started = time.perf_counter_ns()
        layout_pairs = self._build_similarity("layout")
        self.cancellation.checkpoint()
        layout_groups = self._build_layout_groups()
        layout_similarity_elapsed = time.perf_counter_ns() - started
        layout_pages = self._layout_page_count()
        return PdfDerivedSummary(
            fts_pages_indexed=fts_pages,
            text_signatures_built=text_signatures,
            profiles_built=profiles,
            profile_errors=profile_errors,
            text_similarity_pairs=text_pairs,
            template_similarity_pairs=template_pairs,
            layout_similarity_pairs=layout_pairs,
            layout_groups=layout_groups,
            layout_pages_mapped=layout_pages,
            fts_elapsed_ns=fts_elapsed,
            text_signatures_elapsed_ns=text_signatures_elapsed,
            profiles_elapsed_ns=profiles_elapsed,
            text_similarity_elapsed_ns=text_similarity_elapsed,
            template_similarity_elapsed_ns=template_similarity_elapsed,
            layout_similarity_elapsed_ns=layout_similarity_elapsed,
            fts_rows_repaired=fts_repaired,
        )

    def _repair_fts_state(self) -> int:
        """Drop inconsistent FTS/state rows in bounded batches for regeneration."""

        repaired = 0
        with serialized_pdf_write(), _database(self.state_path) as connection:
            while True:
                self._checkpoint_if_configured()
                rowids = [
                    int(row[0])
                    for row in connection.execute(
                        """SELECT f.rowid FROM page_fts f
                        WHERE NOT EXISTS(SELECT 1 FROM pages p WHERE p.file_key=f.file_key
                            AND p.page_number=f.page_number)
                        OR NOT EXISTS(SELECT 1 FROM page_fts_state s
                            WHERE s.file_key=f.file_key AND s.page_number=f.page_number)
                        LIMIT 500"""
                    ).fetchall()
                ]
                if not rowids:
                    break
                placeholders = ",".join("?" for _ in rowids)
                repaired += int(
                    connection.execute(
                        f"DELETE FROM page_fts WHERE rowid IN ({placeholders})", rowids
                    ).rowcount
                )
                connection.commit()

            # FTS5 cannot use a conventional composite index for equality joins.
            # Materialize its keys once in SQLite temp storage instead of scanning
            # the complete virtual table for every page_fts_state row (O(n²)).
            connection.execute(
                """CREATE TEMP TABLE IF NOT EXISTS current_fts_keys(
                file_key TEXT NOT NULL,page_number INTEGER NOT NULL,
                PRIMARY KEY(file_key,page_number)) WITHOUT ROWID"""
            )
            connection.execute("DELETE FROM current_fts_keys")
            connection.execute(
                """INSERT OR IGNORE INTO current_fts_keys(file_key,page_number)
                SELECT file_key,CAST(page_number AS INTEGER) FROM page_fts"""
            )
            connection.commit()
            while True:
                self._checkpoint_if_configured()
                keys = connection.execute(
                    """SELECT s.file_key,s.page_number FROM page_fts_state s
                    WHERE NOT EXISTS(SELECT 1 FROM pages p WHERE p.file_key=s.file_key
                        AND p.page_number=s.page_number)
                    OR NOT EXISTS(SELECT 1 FROM current_fts_keys f
                        WHERE f.file_key=s.file_key
                        AND f.page_number=s.page_number) LIMIT 500"""
                ).fetchall()
                if not keys:
                    break
                repaired += int(
                    connection.executemany(
                        "DELETE FROM page_fts_state WHERE file_key=? AND page_number=?",
                        keys,
                    ).rowcount
                )
                connection.commit()
        return repaired

    def _index_fts(self) -> tuple[int, int]:
        indexed = 0
        repaired = self._repair_fts_state()
        emit_progress(
            self.progress,
            ProgressEvent("pdf", "fts", "Indexando texto PDF", 0, unit="páginas"),
        )
        with _database(self.state_path) as connection:
            rows = connection.execute(
                """SELECT p.file_key,p.page_number,p.text_zlib,d.path
                FROM pages p JOIN documents d ON d.file_key=p.file_key
                WHERE d.status IN ('done','partial') AND d.last_seen_run_id=?
                AND NOT EXISTS(
                    SELECT 1 FROM page_fts_state s WHERE s.file_key=p.file_key
                    AND s.page_number=p.page_number)
                ORDER BY p.file_key,p.page_number""",
                (self.run_id,),
            )
            for row in rows:
                self.cancellation.checkpoint()
                text = zlib.decompress(row["text_zlib"]).decode("utf-8")
                digest = xxhash.xxh3_128_hexdigest(text.encode("utf-8"))
                connection.execute(
                    "INSERT INTO page_fts(file_key,path,page_number,text) VALUES(?,?,?,?)",
                    (row["file_key"], row["path"], row["page_number"], text),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO page_fts_state VALUES(?,?,?)",
                    (row["file_key"], row["page_number"], digest),
                )
                indexed += 1
                if indexed % 200 == 0:
                    self._check_disk()
                    connection.commit()
                    emit_progress(
                        self.progress,
                        ProgressEvent("pdf", "fts", "Indexando texto PDF", indexed, unit="páginas"),
                    )
        emit_progress(
            self.progress,
            ProgressEvent(
                "pdf",
                "fts",
                "Índice textual actualizado",
                indexed,
                indexed,
                "páginas",
                True,
            ),
        )
        return indexed, repaired

    def _build_text_signatures(self) -> int:
        built = pending_writes = 0
        with _database(self.state_path) as connection:
            for file_key in self._text_signature_candidates():
                self.cancellation.checkpoint()
                counters = [0] * SIMHASH_BITS
                window: deque[str] = deque(maxlen=SHINGLE_TOKENS)
                token_count = 0
                for page in connection.execute(
                    "SELECT text_zlib FROM pages WHERE file_key=? ORDER BY page_number",
                    (file_key,),
                ):
                    self.cancellation.checkpoint()
                    text = zlib.decompress(page[0]).decode("utf-8").casefold()
                    for match in TOKEN_RE.finditer(text):
                        token = match.group(0)
                        token_count += 1
                        _add_feature(counters, "token:" + token)
                        window.append(token)
                        if len(window) == SHINGLE_TOKENS:
                            _add_feature(counters, "shingle:" + "\x1f".join(window), 2)
                if token_count:
                    connection.execute(
                        "INSERT OR REPLACE INTO text_signatures VALUES(?,?,?,?,?)",
                        (
                            file_key,
                            SIMILARITY_VERSION,
                            f"{_simhash(counters):016x}",
                            token_count,
                            time.time_ns(),
                        ),
                    )
                    built += 1
                    pending_writes += 1
                if pending_writes >= 100:
                    self._check_disk()
                    connection.commit()
                    pending_writes = 0
        return built

    def _text_signature_candidates(self):
        last_key = ""
        while True:
            self.cancellation.checkpoint()
            with _database(self.state_path) as connection:
                rows = connection.execute(
                    """SELECT d.file_key FROM documents d
                    WHERE d.status='done' AND d.is_partial=0
                    AND d.last_seen_run_id=? AND d.file_key>?
                    AND EXISTS(SELECT 1 FROM pages p WHERE p.file_key=d.file_key)
                    AND NOT EXISTS(SELECT 1 FROM text_signatures s
                        WHERE s.file_key=d.file_key AND s.algorithm_version=?)
                    ORDER BY d.file_key LIMIT 1000""",
                    (self.run_id, last_key, SIMILARITY_VERSION),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                self.cancellation.checkpoint()
                yield row[0]
            last_key = rows[-1][0]

    def _build_profiles(self) -> tuple[int, int]:
        self.cancellation.checkpoint()
        iterator = self._profile_candidates()
        total = self._profile_candidate_count()
        pending: set[Future[bool]] = set()
        completed = built = errors = 0
        progress_started = time.monotonic()
        last_progress_at = progress_started

        def report_progress(*, finished: bool = False) -> None:
            nonlocal last_progress_at
            now = time.monotonic()
            emit_progress(
                self.progress,
                ProgressEvent(
                    "pdf",
                    "profile",
                    "Perfiles PDF actualizados" if finished else "Perfilando PDF",
                    completed,
                    completed if finished else total,
                    "PDF",
                    finished,
                    (
                        ProgressMetric("in_flight", len(pending)),
                        ProgressMetric("remaining", max(0, total - completed)),
                        ProgressMetric("errors", errors),
                        ProgressMetric("elapsed_seconds", int(now - progress_started)),
                    ),
                ),
            )
            last_progress_at = now

        report_progress()
        executor = ThreadPoolExecutor(max_workers=self.workers)
        interrupted = False
        try:
            exhausted = False
            while pending or not exhausted:
                self.cancellation.checkpoint()
                while not exhausted and len(pending) < self.workers * 2:
                    self.cancellation.checkpoint()
                    try:
                        file_key, path, size = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    pending.add(
                        executor.submit(self._profile_document_admitted, file_key, path, size)
                    )
                if not pending:
                    continue
                done, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                if (
                    not done
                    and time.monotonic() - last_progress_at >= PROFILE_PROGRESS_INTERVAL_SECONDS
                ):
                    report_progress()
                for future in done:
                    completed += 1
                    try:
                        succeeded = future.result()
                    except CancellationRequested:
                        raise
                    if succeeded:
                        built += 1
                    else:
                        errors += 1
                    report_progress()
        except CancellationRequested:
            interrupted = True
            for future in pending:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=interrupted)
        report_progress(finished=True)
        return built, errors

    def _profile_candidate_count(self) -> int:
        with _database(self.state_path) as connection:
            return int(
                connection.execute(
                    """SELECT COUNT(*) FROM documents
                    WHERE (status='done' OR (status='partial'
                    AND COALESCE(error_type,'')<>'PdfDocumentTimeout'))
                    AND last_seen_run_id=?
                    AND (COALESCE(profile_version,0)<>? OR template_simhash64 IS NULL
                    OR EXISTS(SELECT 1 FROM pages p WHERE p.file_key=documents.file_key
                        AND (p.profile_json IS NULL OR NOT EXISTS(
                            SELECT 1 FROM page_layouts l WHERE l.file_key=p.file_key
                            AND l.page_number=p.page_number AND l.algorithm_version=?)))
                    OR NOT EXISTS(SELECT 1 FROM document_layouts dl
                        WHERE dl.file_key=documents.file_key
                        AND dl.algorithm_version=?))""",
                    (self.run_id, PROFILE_VERSION, LAYOUT_VERSION, LAYOUT_VERSION),
                ).fetchone()[0]
            )

    def _profile_candidates(self):
        """Yield profile work using bounded keyset pages, never a full corpus list."""

        last_size = -1
        last_path = ""
        while True:
            self.cancellation.checkpoint()
            with _database(self.state_path) as connection:
                rows = connection.execute(
                    f"""SELECT file_key,path,size FROM documents
                    WHERE (status='done' OR (status='partial'
                    AND COALESCE(error_type,'')<>'PdfDocumentTimeout'))
                    AND last_seen_run_id=? AND (COALESCE(profile_version,0)<>?
                    OR template_simhash64 IS NULL OR EXISTS(
                        SELECT 1 FROM pages p WHERE p.file_key=documents.file_key
                        AND (p.profile_json IS NULL OR NOT EXISTS(
                            SELECT 1 FROM page_layouts l WHERE l.file_key=p.file_key
                            AND l.page_number=p.page_number AND l.algorithm_version=?)))
                    OR NOT EXISTS(SELECT 1 FROM document_layouts dl
                        WHERE dl.file_key=documents.file_key
                        AND dl.algorithm_version=?))
                    AND (size>? OR (size=? AND path COLLATE {_PATH_COLLATION}>?))
                    ORDER BY size,path COLLATE {_PATH_COLLATION} LIMIT 1000""",
                    (
                        self.run_id,
                        PROFILE_VERSION,
                        LAYOUT_VERSION,
                        LAYOUT_VERSION,
                        last_size,
                        last_size,
                        last_path,
                    ),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                self.cancellation.checkpoint()
                yield row["file_key"], row["path"], int(row["size"])
            last_size = int(rows[-1]["size"])
            last_path = rows[-1]["path"]

    def _profile_document_admitted(self, file_key: str, path: str, size: int) -> bool:
        self.cancellation.checkpoint()
        if self.resource_gate is None:
            return self._profile_document(file_key, path)
        with self.resource_gate.admit(
            size,
            reservation_bytes=self.profile_memory_bytes,
        ):
            return self._profile_document(file_key, path)

    def _profile_document(self, file_key: str, path: str) -> bool:
        self.cancellation.checkpoint()
        warning_count = 0
        warning_samples: tuple[str, ...] = ()
        try:
            if self.profile_timeout_seconds is None:
                import fitz  # type: ignore[import-untyped]

                fitz.TOOLS.mupdf_display_errors(False)
                fitz.TOOLS.mupdf_display_warnings(False)
                fitz.TOOLS.reset_mupdf_warnings()

                with (
                    fitz.open(path) as document,
                    _database(self.state_path) as connection,
                ):
                    page_numbers = connection.execute(
                        """SELECT p.page_number FROM pages p
                        LEFT JOIN page_layouts l ON l.file_key=p.file_key
                        AND l.page_number=p.page_number AND l.algorithm_version=?
                        WHERE p.file_key=?
                        AND (p.profile_json IS NULL OR l.file_key IS NULL)
                        ORDER BY p.page_number""",
                        (LAYOUT_VERSION, file_key),
                    )

                    def local_profiles():
                        for row in page_numbers:
                            self.cancellation.checkpoint()
                            page_number = int(row[0])
                            yield (
                                page_number,
                                _profile_page(document.load_page(page_number)),
                            )

                    profiles = local_profiles()
                    stored = self._store_profiles(file_key, profiles)
                warning_count, warning_samples = _mupdf_warning_summary(fitz)
                self._store_profile_warnings(file_key, warning_count, warning_samples)
                if stored:
                    self._clear_profile_failure(file_key)
                else:
                    self._store_profile_failure(
                        file_key,
                        RuntimeError("persisted page profile set is incomplete"),
                    )
                return stored

            messages = stream_isolated_profiles(
                path,
                str(self.state_path),
                file_key,
                timeout_seconds=self.profile_timeout_seconds,
                cancellation=self.cancellation,
                memory_limit_bytes=self.profile_memory_bytes,
            )

            def isolated_profiles():
                nonlocal warning_count, warning_samples
                for message in messages:
                    self.cancellation.checkpoint()
                    if message[0] == "fatal":
                        raise RuntimeError(f"{message[1]}: {message[2]}")
                    if message[0] == "warnings":
                        warning_count += int(message[1])
                        warning_samples = tuple(dict.fromkeys((*warning_samples, *message[2])))[:20]
                    if message[0] == "page":
                        yield int(message[1]), message[2]

            profiles = isolated_profiles()
            stored = self._store_profiles(file_key, profiles)
            self._store_profile_warnings(file_key, warning_count, warning_samples)
            if stored:
                self._clear_profile_failure(file_key)
            else:
                self._store_profile_failure(
                    file_key,
                    RuntimeError("persisted page profile set is incomplete"),
                )
            return stored
        except CancellationRequested:
            raise
        except Exception as exc:
            self._store_profile_warnings(file_key, warning_count, warning_samples)
            self._store_profile_failure(file_key, exc)
            return False

    def _store_profile_failure(self, file_key: str, error: Exception) -> None:
        """Persist bounded diagnostic evidence for an incomplete profile."""

        detail = f"{type(error).__name__}: {error}"[:2000]
        try:
            with serialized_pdf_write(), _database(self.state_path) as connection:
                row = connection.execute(
                    "SELECT processing_signature FROM documents WHERE file_key=?",
                    (file_key,),
                ).fetchone()
                if row is None:
                    return
                signature = str(row[0])
                previous = connection.execute(
                    """SELECT warning_count FROM document_warnings
                    WHERE file_key=? AND processing_signature=?
                    AND stage='profile-error'""",
                    (file_key, signature),
                ).fetchone()
                attempts = 1 if previous is None else min(int(previous[0]) + 1, 1000)
                connection.execute(
                    """INSERT OR REPLACE INTO document_warnings(
                    file_key,processing_signature,stage,warning_count,samples_json,updated_ns)
                    VALUES(?,?,?,?,?,?)""",
                    (
                        file_key,
                        signature,
                        "profile-error",
                        attempts,
                        json.dumps((detail,), ensure_ascii=True),
                        time.time_ns(),
                    ),
                )
        except (sqlite3.Error, UnicodeError):
            return

    def _clear_profile_failure(self, file_key: str) -> None:
        try:
            with serialized_pdf_write(), _database(self.state_path) as connection:
                connection.execute(
                    "DELETE FROM document_warnings WHERE file_key=? AND stage='profile-error'",
                    (file_key,),
                )
        except sqlite3.Error:
            return

    def _store_profile_warnings(
        self, file_key: str, warning_count: int, samples: tuple[str, ...]
    ) -> None:
        """Replace bounded MuPDF warning evidence emitted while profiling."""

        try:
            with serialized_pdf_write(), _database(self.state_path) as connection:
                row = connection.execute(
                    "SELECT processing_signature FROM documents WHERE file_key=?",
                    (file_key,),
                ).fetchone()
                if row is None:
                    return
                signature = str(row[0])
                if warning_count <= 0:
                    connection.execute(
                        "DELETE FROM document_warnings WHERE file_key=? "
                        "AND processing_signature=? AND stage='profile'",
                        (file_key, signature),
                    )
                    return
                connection.execute(
                    """INSERT OR REPLACE INTO document_warnings(
                    file_key,processing_signature,stage,warning_count,samples_json,updated_ns)
                    VALUES(?,?,?,?,?,?)""",
                    (
                        file_key,
                        signature,
                        "profile",
                        warning_count,
                        json.dumps(samples, ensure_ascii=True),
                        time.time_ns(),
                    ),
                )
        except (sqlite3.Error, UnicodeError):
            return

    def _store_profiles(self, file_key: str, profiles) -> bool:
        try:
            profile_batch: list[tuple[str, str, int]] = []
            layout_batch: list[tuple] = []

            def flush() -> None:
                self.cancellation.checkpoint()
                if not profile_batch:
                    return
                self._check_disk()
                with serialized_pdf_write(), _database(self.state_path) as connection:
                    connection.executemany(
                        "UPDATE pages SET profile_json=? WHERE file_key=? AND page_number=?",
                        profile_batch,
                    )
                    connection.executemany(
                        """INSERT OR REPLACE INTO page_layouts(
                        file_key,page_number,algorithm_version,source_kind,
                        geometry_simhash64,visual_simhash64,header_simhash64,
                        footer_simhash64,layout_simhash64,layout_zlib,updated_ns)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        layout_batch,
                    )
                profile_batch.clear()
                layout_batch.clear()

            for offset, (page_number, profile) in enumerate(profiles, 1):
                self.cancellation.checkpoint()
                layout = dict(profile["layout"])
                profile_summary = {key: value for key, value in profile.items() if key != "layout"}
                profile_batch.append(
                    (
                        json.dumps(
                            profile_summary,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        file_key,
                        page_number,
                    )
                )
                now = time.time_ns()
                layout_batch.append(
                    (
                        file_key,
                        page_number,
                        LAYOUT_VERSION,
                        layout["source_kind"],
                        layout["geometry_simhash64"],
                        layout["visual_simhash64"],
                        layout["header_simhash64"],
                        layout["footer_simhash64"],
                        layout["layout_simhash64"],
                        zlib.compress(
                            json.dumps(
                                layout,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                            level=6,
                        ),
                        now,
                    )
                )
                if offset % 16 == 0:
                    flush()
            flush()
            return self._finalize_stored_profile(file_key)
        except CancellationRequested:
            raise

    def _finalize_stored_profile(self, file_key: str) -> bool:
        """Publish one document profile from complete persisted page evidence."""

        counters = [0] * SIMHASH_BITS
        layout_counters = [0] * SIMHASH_BITS
        geometry_counters = [0] * SIMHASH_BITS
        visual_counters = [0] * SIMHASH_BITS
        header_counters = [0] * SIMHASH_BITS
        footer_counters = [0] * SIMHASH_BITS
        for target, seed in (
            (layout_counters, "document-layout-v1"),
            (geometry_counters, "document-geometry-v1"),
            (visual_counters, "document-visual-v1"),
            (header_counters, "document-header-v1"),
            (footer_counters, "document-footer-v1"),
        ):
            _add_layout_feature(target, seed, 2)
        sequence = xxhash.xxh3_128()
        source_counts: dict[str, int] = {}
        visual_errors = mapped_pages = header_ink = footer_ink = 0

        with _database(self.state_path) as connection:
            rows = connection.execute(
                """SELECT p.page_number,p.profile_json,l.layout_zlib
                FROM pages p LEFT JOIN page_layouts l
                ON l.file_key=p.file_key AND l.page_number=p.page_number
                AND l.algorithm_version=? WHERE p.file_key=?
                ORDER BY p.page_number""",
                (LAYOUT_VERSION, file_key),
            )
            for page_number, profile_json, layout_zlib in rows:
                self.cancellation.checkpoint()
                if profile_json is None or layout_zlib is None:
                    return False
                profile = json.loads(str(profile_json))
                layout = json.loads(zlib.decompress(layout_zlib).decode("utf-8"))
                mapped_pages += 1
                source = str(layout["source_kind"])
                source_counts[source] = source_counts.get(source, 0) + 1
                visual_errors += int(bool(layout.get("visual_error")))
                header_ink += int(layout.get("header_ink", 0))
                footer_ink += int(layout.get("footer_ink", 0))
                sequence.update(int(page_number).to_bytes(4, "little", signed=False))
                sequence.update(bytes.fromhex(layout["layout_simhash64"]))
                _add_layout_signature(layout_counters, layout["layout_simhash64"], 3)
                _add_layout_signature(geometry_counters, layout["geometry_simhash64"], 3)
                _add_layout_signature(visual_counters, layout["visual_simhash64"], 2)
                _add_layout_signature(header_counters, layout["header_simhash64"], 3)
                _add_layout_signature(footer_counters, layout["footer_simhash64"], 2)
                aspect = round(profile["width"] / max(profile["height"], 1), 1)
                features = (
                    f"aspect:{aspect}",
                    f"rotation:{profile['rotation']}",
                    f"images:{min(profile['image_count'], 10)}",
                    f"drawings:{min(profile['drawing_count'] // 5, 20)}",
                    f"blocks:{min(profile['text_block_count'] // 5, 20)}",
                    *(f"font:{name}" for name in profile["font_names"]),
                )
                for feature in features:
                    _add_feature(counters, feature)
        if mapped_pages <= 0:
            return False

        try:
            layout_signature = _finish_layout_signature(layout_counters)
            geometry_signature = _finish_layout_signature(geometry_counters)
            visual_signature = _finish_layout_signature(visual_counters)
            header_signature = _finish_layout_signature(header_counters)
            footer_signature = _finish_layout_signature(footer_counters)
            evidence = json.dumps(
                {
                    "algorithm": "layout-map-v1",
                    "mapped_pages": mapped_pages,
                    "source_counts": source_counts,
                    "visual_errors": visual_errors,
                    "header_ink": header_ink,
                    "footer_ink": footer_ink,
                },
                separators=(",", ":"),
            )
            with serialized_pdf_write(), _database(self.state_path) as connection:
                connection.execute(
                    """INSERT OR REPLACE INTO document_layouts(
                    file_key,algorithm_version,mapped_pages,layout_simhash64,
                    geometry_simhash64,visual_simhash64,header_simhash64,
                    footer_simhash64,page_sequence_xxh3_128,evidence_json,updated_ns)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        file_key,
                        LAYOUT_VERSION,
                        mapped_pages,
                        layout_signature,
                        geometry_signature,
                        visual_signature,
                        header_signature,
                        footer_signature,
                        sequence.hexdigest(),
                        evidence,
                        time.time_ns(),
                    ),
                )
                connection.execute(
                    "UPDATE documents SET profile_version=?,template_simhash64=? WHERE file_key=?",
                    (PROFILE_VERSION, f"{_simhash(counters):016x}", file_key),
                )
            return True
        except CancellationRequested:
            raise

    def _build_similarity(self, kind: str) -> int:
        self.cancellation.checkpoint()
        if kind not in {"text", "template", "layout"}:
            raise ValueError(kind)
        active_digest = self._active_similarity_digest(kind)
        with _database(self.state_path) as connection:
            cached_count = self._cached_similarity_count(
                connection,
                kind,
                active_digest,
            )
            if cached_count is not None:
                return cached_count
            connection.execute("DELETE FROM similarity_buckets WHERE signature_kind=?", (kind,))
            connection.execute(
                "DELETE FROM similarity_relations WHERE kind=?", (f"{kind}_similar",)
            )
            inserted = 0
            for file_key, signature_hex in self._signature_rows(kind):
                self.cancellation.checkpoint()
                signature = int(signature_hex, 16)
                for band in range(SIMILARITY_BANDS):
                    bucket = (signature >> (band * SIMILARITY_BAND_BITS)) & (
                        (1 << SIMILARITY_BAND_BITS) - 1
                    )
                    connection.execute(
                        "INSERT INTO similarity_buckets VALUES(?,?,?,?,?)",
                        (self.run_id, kind, band, bucket, file_key),
                    )
                    inserted += 1
                if inserted >= 4000:
                    self._check_disk()
                    connection.commit()
                    inserted = 0
            connection.commit()
            relation_writes = 0
            for file_key, signature_hex in self._signature_rows(kind):
                self.cancellation.checkpoint()
                signature = int(signature_hex, 16)
                candidates: set[str] = set()
                for band in range(SIMILARITY_BANDS):
                    bucket = (signature >> (band * SIMILARITY_BAND_BITS)) & (
                        (1 << SIMILARITY_BAND_BITS) - 1
                    )
                    members = connection.execute(
                        """SELECT file_key FROM similarity_buckets WHERE run_id=?
                        AND signature_kind=? AND band=? AND bucket=? LIMIT ?""",
                        (self.run_id, kind, band, bucket, MAX_BUCKET_MEMBERS + 1),
                    ).fetchall()
                    if len(members) > MAX_BUCKET_MEMBERS:
                        continue
                    candidates.update(row[0] for row in members if row[0] > file_key)
                    if len(candidates) >= MAX_CANDIDATES_PER_DOCUMENT:
                        break
                for candidate in sorted(candidates)[:MAX_CANDIDATES_PER_DOCUMENT]:
                    self.cancellation.checkpoint()
                    candidate_signature = self._signature_for(connection, kind, candidate)
                    if candidate_signature is None:
                        continue
                    if kind == "layout":
                        score, relation_evidence = self._layout_relation(
                            connection, file_key, candidate
                        )
                    else:
                        score = _similarity(signature, candidate_signature)
                        relation_evidence = {
                            "algorithm": "simhash64",
                            "threshold": self.threshold,
                        }
                    if score < self.threshold:
                        continue
                    connection.execute(
                        "INSERT OR REPLACE INTO similarity_relations VALUES(?,?,?,?,?,?,?)",
                        (
                            self.run_id,
                            file_key,
                            candidate,
                            f"{kind}_similar",
                            score,
                            SIMILARITY_VERSION,
                            json.dumps(relation_evidence, separators=(",", ":")),
                        ),
                    )
                    relation_writes += 1
                    if relation_writes >= 1000:
                        self._check_disk()
                        connection.commit()
                        relation_writes = 0
            relation_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM similarity_relations WHERE run_id=? AND kind=?",
                    (self.run_id, f"{kind}_similar"),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT OR REPLACE INTO similarity_state VALUES(?,?,?,?,?,?)",
                (
                    kind,
                    active_digest,
                    self.threshold,
                    SIMILARITY_VERSION,
                    self.run_id,
                    relation_count,
                ),
            )
            return relation_count

    def _active_similarity_digest(self, kind: str) -> str:
        active = xxhash.xxh3_128()
        for file_key, signature_hex in self._signature_rows(kind):
            self.cancellation.checkpoint()
            active.update(file_key.encode("ascii"))
            active.update(bytes.fromhex(signature_hex))
        return active.hexdigest()

    def _cached_similarity_count(
        self,
        connection: sqlite3.Connection,
        kind: str,
        active_digest: str,
    ) -> int | None:
        cached = connection.execute(
            """SELECT relation_count,relation_run_id FROM similarity_state WHERE signature_kind=?
            AND active_xxh3_128=? AND threshold=? AND algorithm_version=?""",
            (kind, active_digest, self.threshold, SIMILARITY_VERSION),
        ).fetchone()
        if cached is None:
            return None
        expected = int(cached[0])
        actual = int(
            connection.execute(
                "SELECT COUNT(*) FROM similarity_relations WHERE run_id=? AND kind=?",
                (int(cached[1]), f"{kind}_similar"),
            ).fetchone()[0]
        )
        return expected if actual == expected else None

    def _signature_rows(self, kind: str):
        last_key = ""
        while True:
            with _database(self.state_path) as connection:
                if kind == "text":
                    rows = connection.execute(
                        """SELECT d.file_key,s.simhash64 FROM documents d
                        JOIN text_signatures s ON s.file_key=d.file_key
                        WHERE d.status='done' AND d.is_partial=0
                        AND d.last_seen_run_id=?
                        AND d.file_key>? ORDER BY d.file_key LIMIT 1000""",
                        (self.run_id, last_key),
                    ).fetchall()
                elif kind == "template":
                    rows = connection.execute(
                        """SELECT file_key,template_simhash64 FROM documents
                        WHERE status IN ('done','partial') AND last_seen_run_id=?
                        AND file_key>?
                        AND template_simhash64 IS NOT NULL
                        ORDER BY file_key LIMIT 1000""",
                        (self.run_id, last_key),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """SELECT d.file_key,l.layout_simhash64 FROM documents d
                        JOIN document_layouts l ON l.file_key=d.file_key
                        WHERE d.status IN ('done','partial') AND d.last_seen_run_id=?
                        AND l.algorithm_version=? AND d.file_key>?
                        ORDER BY d.file_key LIMIT 1000""",
                        (self.run_id, LAYOUT_VERSION, last_key),
                    ).fetchall()
            if not rows:
                return
            for row in rows:
                self.cancellation.checkpoint()
                yield row[0], row[1]
            last_key = rows[-1][0]

    @staticmethod
    def _signature_for(connection: sqlite3.Connection, kind: str, file_key: str) -> int | None:
        if kind == "text":
            row = connection.execute(
                "SELECT simhash64 FROM text_signatures WHERE file_key=?",
                (file_key,),
            ).fetchone()
        elif kind == "template":
            row = connection.execute(
                "SELECT template_simhash64 FROM documents WHERE file_key=?",
                (file_key,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT layout_simhash64 FROM document_layouts WHERE file_key=? "
                "AND algorithm_version=?",
                (file_key, LAYOUT_VERSION),
            ).fetchone()
        return int(row[0], 16) if row and row[0] else None

    def _layout_relation(
        self, connection: sqlite3.Connection, file_key_a: str, file_key_b: str
    ) -> tuple[float, dict]:
        rows = connection.execute(
            """SELECT file_key,mapped_pages,layout_simhash64,geometry_simhash64,
            visual_simhash64,header_simhash64,footer_simhash64,
            page_sequence_xxh3_128,evidence_json
            FROM document_layouts WHERE file_key IN (?,?)""",
            (file_key_a, file_key_b),
        ).fetchall()
        if len(rows) != 2:
            return 0.0, {"algorithm": "layout-map-v1", "missing": True}
        by_key = {str(row["file_key"]): row for row in rows}
        left, right = by_key[file_key_a], by_key[file_key_b]
        left_evidence = json.loads(left["evidence_json"])
        right_evidence = json.loads(right["evidence_json"])
        geometry = _layout_similarity(left["geometry_simhash64"], right["geometry_simhash64"])
        visual = _layout_similarity(left["visual_simhash64"], right["visual_simhash64"])
        header = _layout_similarity(left["header_simhash64"], right["header_simhash64"])
        footer = _layout_similarity(left["footer_simhash64"], right["footer_simhash64"])
        if (
            min(
                int(left_evidence.get("header_ink", 0)),
                int(right_evidence.get("header_ink", 0)),
            )
            < 5
        ):
            header = 0.0
        if (
            min(
                int(left_evidence.get("footer_ink", 0)),
                int(right_evidence.get("footer_ink", 0)),
            )
            < 3
        ):
            footer = 0.0
        same_sequence = left["page_sequence_xxh3_128"] == right["page_sequence_xxh3_128"]
        weighted = geometry * 0.45 + visual * 0.25 + header * 0.20 + footer * 0.05
        weighted += 0.05 if same_sequence else 0.0
        letterhead_score = header * 0.72 + geometry * 0.28 if header else 0.0
        score = max(weighted, letterhead_score)
        match_types: list[str] = []
        if header >= max(0.88, self.threshold):
            match_types.append("same_letterhead")
        if geometry >= self.threshold and visual >= self.threshold:
            match_types.append("same_format")
        if same_sequence:
            match_types.append("same_page_sequence")
        return score, {
            "algorithm": "layout-map-v1",
            "threshold": self.threshold,
            "geometry": round(geometry, 6),
            "visual": round(visual, 6),
            "header": round(header, 6),
            "footer": round(footer, 6),
            "same_page_sequence": same_sequence,
            "page_counts": [int(left["mapped_pages"]), int(right["mapped_pages"])],
            "match_types": match_types,
        }

    def _layout_page_count(self) -> int:
        with _database(self.state_path) as connection:
            return int(
                connection.execute(
                    """SELECT COUNT(*) FROM page_layouts l JOIN documents d USING(file_key)
                    WHERE d.last_seen_run_id=? AND l.algorithm_version=?""",
                    (self.run_id, LAYOUT_VERSION),
                ).fetchone()[0]
            )

    def _build_layout_groups(self) -> int:
        """Materialize connected layout families from the active relation generation."""

        with _database(self.state_path) as connection:
            state = connection.execute(
                "SELECT relation_run_id FROM similarity_state WHERE signature_kind='layout'"
            ).fetchone()
            if state is None:
                return 0
            relation_run_id = int(state[0])
            relations = connection.execute(
                """SELECT file_key_a,file_key_b,score FROM similarity_relations
                WHERE run_id=? AND kind='layout_similar' ORDER BY score DESC""",
                (relation_run_id,),
            )
            parent: dict[str, str] = {}

            def find(value: str) -> str:
                parent.setdefault(value, value)
                while parent[value] != value:
                    parent[value] = parent[parent[value]]
                    value = parent[value]
                return value

            for row in relations:
                self.cancellation.checkpoint()
                left, right, score = str(row[0]), str(row[1]), float(row[2])
                root_left, root_right = find(left), find(right)
                if root_left != root_right:
                    parent[root_right] = root_left
            components: dict[str, list[str]] = {}
            for file_key in parent:
                self.cancellation.checkpoint()
                components.setdefault(find(file_key), []).append(file_key)

            degree = dict.fromkeys(parent, 0)
            minimum_edge_score: dict[str, float] = {}
            for row in connection.execute(
                """SELECT file_key_a,file_key_b,score FROM similarity_relations
                WHERE run_id=? AND kind='layout_similar'""",
                (relation_run_id,),
            ):
                self.cancellation.checkpoint()
                left, right, score = str(row[0]), str(row[1]), float(row[2])
                component = find(left)
                if component != find(right):
                    continue
                degree[left] += 1
                degree[right] += 1
                minimum_edge_score[component] = min(
                    minimum_edge_score.get(component, score),
                    score,
                )
            connection.execute(
                "DELETE FROM layout_group_members WHERE relation_run_id=?",
                (relation_run_id,),
            )
            connection.execute(
                "DELETE FROM layout_groups WHERE relation_run_id=?",
                (relation_run_id,),
            )
            group_count = 0
            for members in components.values():
                self.cancellation.checkpoint()
                if len(members) < 2:
                    continue
                members.sort()
                digest = xxhash.xxh3_128()
                for member in members:
                    digest.update(member.encode("ascii"))
                group_key = digest.hexdigest()
                representative = min(members, key=lambda item: (-degree[item], item))
                connection.execute(
                    """INSERT INTO layout_groups VALUES(?,?,?,?,?,?,?)""",
                    (
                        relation_run_id,
                        group_key,
                        representative,
                        len(members),
                        minimum_edge_score.get(find(members[0]), 0.0),
                        LAYOUT_VERSION,
                        json.dumps(
                            {
                                "algorithm": "connected-components",
                                "threshold": self.threshold,
                            },
                            separators=(",", ":"),
                        ),
                    ),
                )
                connection.executemany(
                    "INSERT INTO layout_group_members VALUES(?,?,?)",
                    ((relation_run_id, group_key, member) for member in members),
                )
                group_count += 1
            return group_count


# endregion [02]
