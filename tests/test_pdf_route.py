# region [00] Contexto del módulo
# Módulo: tests/test_pdf_route.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import fitz  # type: ignore[import-untyped]

from neocortex.deduplication import DedupIndex, FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.capabilities.formats.pdf.pdf_route import (
    PdfRoute,
    PdfRouteConfig,
    _initialize,
    effective_pdf_job_memory_limit_bytes,
    effective_pdf_worker_memory_bytes,
)
from neocortex.capabilities.formats.pdf.pdf_derived import search_pdf_state
from neocortex.capabilities.formats.pdf.pdf_isolation import (
    MAX_CONSECUTIVE_PAGE_ERRORS,
    IsolatedExtractionConfig,
    PdfDocumentTimeout,
    _ParentOcrLease,
    _extract_child,
    _mupdf_warning_summary,
    _ocr_page,
    _qpdf_repaired_copy,
    stream_isolated_extraction,
)
from neocortex.capabilities.formats.pdf.pdf_route_models import CacheDecision
from neocortex.capabilities.formats.pdf.pdf_state import SCHEMA_VERSION as PDF_SCHEMA_VERSION
from neocortex.workflow.review.review import list_review_candidates
from neocortex.runtime.control.retry_policy import classify_pdf_failure
from neocortex.persistence.state import FrameworkRouteState, FrameworkState
# endregion [01]

# region [02] Implementación


class _State:
    def __init__(self, candidates=()) -> None:
        self.actions: list[dict] = []
        self.candidates = list(candidates)
        self.review_candidates: list[Any] = []
        self.review_store_runs: list[int] = []
        self.review_reconciliations: list[tuple[Any, ...]] = []
        self.review_generation_resolutions: list[tuple[Any, ...]] = []

    def iter_route_candidates(self, run_id, mime):
        if mime == "application/pdf":
            yield from (item for item in self.candidates if item.path.casefold().endswith(".pdf"))

    def begin_file_actions(self, run_id, actions):
        ids = []
        for action in actions:
            self.actions.append({"values": action, "status": "started"})
            ids.append(len(self.actions) - 1)
        return ids

    def finish_file_actions(self, action_ids, status, detail=None):
        for action_id in action_ids:
            self.actions[action_id]["status"] = status
            self.actions[action_id]["detail"] = detail

    def store_review_candidates(self, run_id, candidates):
        self.review_store_runs.append(int(run_id))
        self.review_candidates.extend(candidates)

    def reconcile_review_candidates(
        self,
        run_id,
        route_name,
        snapshot,
        resolution_note,
        *,
        evaluated_reason_codes,
        active_reason_codes,
    ):
        self.review_reconciliations.append(
            (
                run_id,
                route_name,
                snapshot,
                resolution_note,
                frozenset(evaluated_reason_codes),
                frozenset(active_reason_codes),
            )
        )
        return 0

    def reconcile_review_candidates_batch(
        self,
        run_id,
        route_name,
        reconciliations,
    ):
        for reconciliation in reconciliations:
            self.review_reconciliations.append(
                (
                    run_id,
                    route_name,
                    reconciliation.snapshot,
                    reconciliation.resolution_note,
                    frozenset(reconciliation.evaluated_reason_codes),
                    frozenset(reconciliation.active_reason_codes),
                )
            )
        return 0

    def resolve_review_candidate_generation(
        self,
        candidate_generation,
        route_name,
        snapshot,
        reason_code,
        resolution_note,
    ):
        self.review_generation_resolutions.append(
            (
                candidate_generation,
                route_name,
                snapshot,
                reason_code,
                resolution_note,
            )
        )
        return 1


def _write_pdf(path: Path, text: str, *, title: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.set_metadata({"title": title})
    document.save(path)
    document.close()


def _write_pdf_pages(path: Path, texts: list[str]) -> None:
    document = fitz.open()
    for text in texts:
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()


class _ProtocolQueue:
    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def put(self, item, block=True, timeout=None) -> None:
        self._queue.put(item, block=block, timeout=timeout)

    def get(self, block=True, timeout=None):
        return self._queue.get(block=block, timeout=timeout)

    def close(self) -> None:
        return None

    def cancel_join_thread(self) -> None:
        return None


class _ProtocolContext:
    def Queue(self, maxsize=0):
        return _ProtocolQueue()


class _ProtocolProcess:
    def __init__(self, args, *, release_lease: bool) -> None:
        self._result_channel = args[2]
        self._control_channel = args[3]
        self._release_lease = release_lease
        self._stop = threading.Event()
        self.granted = threading.Event()
        self._alive = False
        self.exitcode: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._alive = True

        def run() -> None:
            self._result_channel.put(("ocr_request",))
            self._control_channel.get()
            self.granted.set()
            if self._release_lease:
                self._result_channel.put(("ocr_release",))
                self._result_channel.put(("done", 1, 0, 1))
                self.exitcode = 0
            else:
                self._stop.wait(2)
                self.exitcode = 1
            self._alive = False

        self._thread = threading.Thread(target=run)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout=None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def terminate_tree(self) -> None:
        self._stop.set()

    def close(self) -> None:
        return None


def _isolated_config() -> IsolatedExtractionConfig:
    return IsolatedExtractionConfig(
        ocr_mode="auto",
        ocr_lang="spa+eng",
        dpi=200,
        min_page_chars=24,
        max_page_text_chars=1_000,
        max_render_pixels=400_000,
        max_ocr_pages=None,
        ocr_timeout_seconds=30,
        pdfminer_fallback=True,
        max_pages=None,
        page_start=None,
        page_end=None,
        fail_fast_pages=False,
        skip_before=0,
        only_pages=frozenset(),
        prior_ocr_pages=0,
        tesseract_cmd=None,
        tessdata_dir=None,
    )


class PdfRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Functional route tests must not depend on machine-wide commit pressure;
        # test_pdf_runtime.py retains the dedicated resource-admission coverage.
        automatic_limit_patcher = patch(
            "neocortex.capabilities.formats.pdf.pdf_runtime._automatic_limit",
            return_value=0,
        )
        automatic_limit_patcher.start()
        self.addCleanup(automatic_limit_patcher.stop)

    def test_active_page_progress_reads_only_durable_staging_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "pdf.sqlite3"
            snapshot = FileSnapshot(str(root / "active.pdf"), 1, 2, 100, 3, 4)
            config = PdfRouteConfig(
                state_path,
                ocr_mode="never",
                workers=1,
                page_start=3,
                page_end=8,
            )
            with DedupIndex(root / "dedup.sqlite3") as index:
                route = PdfRoute(config, index, _State(), 1, 1)
                key = f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}"
                with closing(sqlite3.connect(state_path)) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute(
                        """INSERT INTO documents(
                        file_key,path,size,mtime_ns,birthtime_ns,
                        processing_signature,status,page_count,updated_ns)
                        VALUES(?,?,?,?,?,?,'processing',10,1)""",
                        (
                            key,
                            snapshot.path,
                            snapshot.size,
                            snapshot.mtime_ns,
                            snapshot.birthtime_ns,
                            config.processing_signature,
                        ),
                    )
                    for page_number, source in (
                        (2, "native"),
                        (3, "native"),
                        (4, "ocr"),
                        (5, "error"),
                    ):
                        connection.execute(
                            """INSERT INTO page_staging(
                            file_key,processing_signature,page_number,source,
                            text_zlib,text_chars) VALUES(?,?,?,?,?,?)""",
                            (
                                key,
                                config.processing_signature,
                                page_number,
                                source,
                                zlib.compress(b"text"),
                                4,
                            ),
                        )
                    connection.commit()

                    progress = route._active_page_progress(connection, (snapshot,))

            self.assertEqual(progress, "3/6")

    def test_child_stops_after_bounded_consecutive_page_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "broken-page-tree.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            snapshot = snapshot_path(source)
            result_channel = _ProtocolQueue()
            control_channel = _ProtocolQueue()

            class BrokenPageTree:
                needs_pass = False
                page_count = 100
                metadata = {}

                @staticmethod
                def load_page(_page_number):
                    raise RuntimeError("cannot find page 10 in page tree")

                @staticmethod
                def close():
                    return None

            with patch("fitz.open", return_value=BrokenPageTree()):
                _extract_child(
                    snapshot,
                    _isolated_config(),
                    result_channel,
                    control_channel,
                )

            messages = []
            while True:
                try:
                    messages.append(result_channel.get(block=False))
                except queue.Empty:
                    break
            page_errors = [item for item in messages if item[0] == "page_error"]
            limits = [item for item in messages if item[0] == "page_error_limit"]
            self.assertEqual(len(page_errors), MAX_CONSECUTIVE_PAGE_ERRORS)
            self.assertEqual(len(limits), 1)
            self.assertEqual(limits[0][3], 100 - MAX_CONSECUTIVE_PAGE_ERRORS)
            self.assertTrue(any(item[0] == "done" for item in messages))

    def test_forced_structural_recovery_uses_pdfminer_after_qpdf_page_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "broken-page-tree.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            snapshot = snapshot_path(source)
            result_channel = _ProtocolQueue()
            control_channel = _ProtocolQueue()

            class BrokenPageTree:
                needs_pass = False
                page_count = 100
                metadata = {}

                @staticmethod
                def load_page(_page_number):
                    raise RuntimeError("cannot find page in page tree")

                @staticmethod
                def close():
                    return None

            def pdfminer_success(_snapshot, _config, channel, *, recovery):
                channel.put(
                    (
                        "header",
                        1,
                        0,
                        1,
                        {"engine": "pdfminer", "neocortex_recovery": recovery},
                    )
                )
                channel.put(("page", 0, "pdfminer", "contenido recuperado"))
                return 1, 0, 1

            config = replace(
                _isolated_config(),
                structural_recovery_reason="legacy page sequence failure",
            )
            with (
                patch("fitz.open", return_value=BrokenPageTree()),
                patch(
                    "neocortex.capabilities.formats.pdf.pdf_isolation._qpdf_repaired_copy",
                    return_value=nullcontext(("repaired.pdf", {"engine": "qpdf+pymupdf"})),
                ),
                patch(
                    "neocortex.capabilities.formats.pdf.pdf_isolation._extract_with_pdfminer",
                    side_effect=pdfminer_success,
                ),
            ):
                _extract_child(snapshot, config, result_channel, control_channel)

            messages = []
            while True:
                try:
                    messages.append(result_channel.get(block=False))
                except queue.Empty:
                    break
            self.assertTrue(any(item[0] == "restart" for item in messages))
            self.assertTrue(any(item[0] == "done" for item in messages))
            self.assertFalse(any(item[0] == "fatal" for item in messages))

    def test_ocr_reservation_covers_the_complete_process_tree(self):
        automatic = PdfRouteConfig(Path("state.sqlite3"), ocr_mode="auto")
        native_only = PdfRouteConfig(
            Path("state.sqlite3"),
            ocr_mode="never",
            worker_memory_bytes=256 * 1024 * 1024,
        )
        automatic_estimate = effective_pdf_worker_memory_bytes(automatic)
        native_estimate = effective_pdf_worker_memory_bytes(native_only)
        self.assertGreater(automatic_estimate, 1024 * 1024 * 1024)
        self.assertGreater(native_estimate, 256 * 1024 * 1024)
        self.assertGreater(effective_pdf_job_memory_limit_bytes(automatic), automatic_estimate)

    def test_mupdf_warning_samples_are_always_utf8_serializable(self):
        class Tools:
            @staticmethod
            def mupdf_warnings(*, reset):
                return "warning with invalid byte \udcb0"

        count, samples = _mupdf_warning_summary(SimpleNamespace(TOOLS=Tools()))
        encoded = json.dumps(samples, ensure_ascii=False).encode("utf-8")
        self.assertEqual(count, 1)
        self.assertIn(b"\\udcb0", encoded)

    def test_ocr_adapts_dpi_and_retries_windows_temp_collision(self):
        scales = []

        class FakeFitz:
            csGRAY = object()

            @staticmethod
            def Matrix(x, y):
                scales.append((x, y))
                return x, y

        class FakePage:
            rect = SimpleNamespace(width=400.0, height=250.0)

            @staticmethod
            def get_pixmap(*, matrix, colorspace, alpha):
                self = SimpleNamespace(n=1, width=2, height=2, samples=b"\x00" * 4)
                return self

        config = _isolated_config()
        collision = PermissionError("temporary file busy")
        collision.winerror = 32
        with (
            patch("pytesseract.image_to_string", side_effect=(collision, "texto")) as ocr,
            patch("neocortex.capabilities.formats.pdf.pdf_isolation.time.sleep"),
        ):
            text = _ocr_page(FakePage(), FakeFitz, config, nullcontext())
        self.assertEqual(text, "texto")
        self.assertEqual(ocr.call_count, 2)
        self.assertLess(scales[0][0], 2.0)
        self.assertGreaterEqual(scales[0][0], 1.0)

    def test_ocr_retries_allocation_failure_at_lower_resolution(self):
        scales = []

        class FakeFitz:
            csGRAY = object()

            @staticmethod
            def Matrix(x, y):
                scales.append((x, y))
                return x, y

        class FakePage:
            rect = SimpleNamespace(width=400.0, height=250.0)

            @staticmethod
            def get_pixmap(*, matrix, colorspace, alpha):
                return SimpleNamespace(
                    n=1,
                    width=2,
                    height=2,
                    samples=b"\x00" * 4,
                )

        with patch(
            "pytesseract.image_to_string",
            side_effect=(MemoryError("allocation failed"), "texto reducido"),
        ) as ocr:
            text = _ocr_page(
                FakePage(),
                FakeFitz,
                _isolated_config(),
                nullcontext(),
            )
        self.assertEqual(text, "texto reducido")
        self.assertEqual(ocr.call_count, 2)
        self.assertLess(scales[1][0], scales[0][0])

    def test_persisted_ocr_backoff_starts_below_requested_resolution(self):
        scales = []

        class FakeFitz:
            csGRAY = object()

            @staticmethod
            def Matrix(x, y):
                scales.append((x, y))
                return x, y

        class FakePage:
            rect = SimpleNamespace(width=400.0, height=250.0)

            @staticmethod
            def get_pixmap(*, matrix, colorspace, alpha):
                return SimpleNamespace(width=2, height=2, samples=b"\x00" * 4)

        config = _isolated_config()
        config = type(config)(
            **{
                field: getattr(config, field)
                for field in config.__dataclass_fields__
                if field != "ocr_scale_factor"
            },
            ocr_scale_factor=0.75,
        )
        with patch("pytesseract.image_to_string", return_value="texto"):
            _ocr_page(FakePage(), FakeFitz, config, nullcontext())
        self.assertLess(scales[0][0], 200 / 72)

    def test_qpdf_recovery_is_temporary_and_original_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "damaged.pdf"
            _write_pdf(source, "proteccion", title="original")
            original = source.read_bytes()
            snapshot = snapshot_path(source)
            yielded: Path | None = None

            def fake_run(command, **kwargs):
                Path(command[-1]).write_bytes(original)
                return subprocess.CompletedProcess(command, 3)

            with (
                patch(
                    "neocortex.capabilities.formats.pdf.pdf_isolation.shutil.which",
                    return_value="qpdf",
                ),
                patch(
                    "neocortex.capabilities.formats.pdf.pdf_isolation.subprocess.run",
                    side_effect=fake_run,
                ),
                _qpdf_repaired_copy(
                    snapshot,
                    _isolated_config(),
                    primary_error="Unexpected EOF",
                    fallback_error="pdfminer failed",
                ) as (repaired, evidence),
            ):
                yielded = Path(repaired)
                self.assertTrue(yielded.is_file())
                self.assertEqual(evidence["qpdf_exit_code"], 3)
            self.assertIsNotNone(yielded)
            self.assertFalse(yielded.exists())
            self.assertEqual(source.read_bytes(), original)

    def test_parent_owned_ocr_lease_is_recoverable_after_child_failure(self):
        slots = threading.BoundedSemaphore(1)
        lease = _ParentOcrLease(slots)
        process = SimpleNamespace(is_alive=lambda: True, exitcode=None)
        lease.acquire(
            process=process,
            deadline=time.monotonic() + 1,
            path="document.pdf",
            cancellation=None,
        )
        self.assertFalse(slots.acquire(blocking=False))

        # The supervisor's finally block can restore the token even if the
        # process holding the remote permission was forcibly terminated.
        lease.release()
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()
        lease.release()

    def test_isolated_protocol_returns_ocr_lease_normally(self):
        slots = threading.BoundedSemaphore(1)
        created: list[_ProtocolProcess] = []

        def process_factory(**kwargs):
            process = _ProtocolProcess(kwargs["args"], release_lease=True)
            created.append(process)
            return process

        with (
            patch(
                "neocortex.capabilities.formats.pdf.pdf_isolation.multiprocessing.get_context",
                return_value=_ProtocolContext(),
            ),
            patch(
                "neocortex.capabilities.formats.pdf.pdf_isolation.isolated_spawn_process",
                side_effect=process_factory,
            ),
        ):
            messages = list(
                stream_isolated_extraction(
                    FileSnapshot("document.pdf", 1, 1, 1, 1, 1),
                    _isolated_config(),
                    timeout_seconds=1,
                    ocr_slots=slots,
                )
            )
        self.assertEqual(messages, [("done", 1, 0, 1)])
        self.assertTrue(created[0].granted.is_set())
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_isolated_timeout_recovers_parent_owned_ocr_lease(self):
        slots = threading.BoundedSemaphore(1)
        created: list[_ProtocolProcess] = []

        def process_factory(**kwargs):
            process = _ProtocolProcess(kwargs["args"], release_lease=False)
            created.append(process)
            return process

        with (
            patch(
                "neocortex.capabilities.formats.pdf.pdf_isolation.multiprocessing.get_context",
                return_value=_ProtocolContext(),
            ),
            patch(
                "neocortex.capabilities.formats.pdf.pdf_isolation.isolated_spawn_process",
                side_effect=process_factory,
            ),
            self.assertRaises(PdfDocumentTimeout),
        ):
            list(
                stream_isolated_extraction(
                    FileSnapshot("document.pdf", 1, 1, 1, 1, 1),
                    _isolated_config(),
                    timeout_seconds=0.05,
                    ocr_slots=slots,
                )
            )
        self.assertTrue(created[0].granted.is_set())
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_candidate_stream_applies_size_then_count_limits(self):
        candidates = [
            FileSnapshot("first.pdf", 1, 1, 500_000, 1, 1),
            FileSnapshot("oversize.pdf", 1, 2, 2_000_000, 1, 1),
            FileSnapshot("second.pdf", 1, 3, 1_000_000, 1, 1),
            FileSnapshot("third.pdf", 1, 4, 750_000, 1, 1),
        ]
        route = object.__new__(PdfRoute)
        route.config = PdfRouteConfig(
            Path("state.sqlite3"),
            max_file_bytes=1_000_000,
            max_documents=2,
        )
        route.framework_state = _State(candidates)
        route.run_id = 1
        selected = list(route._candidate_snapshots())
        self.assertEqual([item.path for item in selected], ["first.pdf", "second.pdf"])

        route.config = PdfRouteConfig(Path("state.sqlite3"), max_documents=2)
        priority = (
            f"{candidates[2].volume_id:032x}:{candidates[2].file_id:032x}",
            f"{candidates[0].volume_id:032x}:{candidates[0].file_id:032x}",
        )
        with patch.object(route, "_priority_candidate_keys", return_value=priority):
            prioritized = list(route._candidate_snapshots())
        self.assertEqual([item.path for item in prioritized], ["second.pdf", "first.pdf"])

        route.config = PdfRouteConfig(Path("state.sqlite3"))
        self.assertEqual(len(list(route._candidate_snapshots())), 4)

    def test_normal_unbounded_run_validates_likely_cache_before_extreme_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "pdf.sqlite3"
            _initialize(state)
            snapshots = {
                name: FileSnapshot(f"{name}.pdf", 1, number, size, 10, 20 + number)
                for name, number, size in (
                    ("cached", 1, 100),
                    ("legacy", 2, 100),
                    ("uncertain", 3, 100),
                    ("new", 4, 100),
                    ("processing", 5, 1_000),
                    ("timeout", 6, 900_000_000),
                )
            }
            config = PdfRouteConfig(state)
            with closing(sqlite3.connect(state)) as connection:
                connection.executemany(
                    """INSERT INTO pdf_inventory(
                    file_key,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
                    VALUES(?,?,?,?,?,7)""",
                    (
                        (
                            f"{item.volume_id:032x}:{item.file_id:032x}",
                            item.path,
                            item.size,
                            item.mtime_ns,
                            item.birthtime_ns,
                        )
                        for item in snapshots.values()
                    ),
                )
                for name, status, birthtime, binary, error_type in (
                    ("cached", "done", snapshots["cached"].birthtime_ns, None, None),
                    ("legacy", "done", -1, "full-digest", None),
                    ("uncertain", "done", -1, None, None),
                    (
                        "processing",
                        "processing",
                        snapshots["processing"].birthtime_ns,
                        None,
                        None,
                    ),
                    (
                        "timeout",
                        "partial",
                        snapshots["timeout"].birthtime_ns,
                        None,
                        "PdfDocumentTimeout",
                    ),
                ):
                    item = snapshots[name]
                    connection.execute(
                        """INSERT INTO documents(
                        file_key,path,size,mtime_ns,birthtime_ns,
                        processing_signature,status,error_type,updated_ns)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            f"{item.volume_id:032x}:{item.file_id:032x}",
                            item.path,
                            item.size,
                            item.mtime_ns,
                            birthtime,
                            config.processing_signature,
                            status,
                            error_type,
                            time.time_ns(),
                        ),
                    )
                    if binary is not None:
                        connection.execute(
                            "UPDATE documents SET binary_xxh3_128=? WHERE path=?",
                            (binary, item.path),
                        )
                connection.commit()

            route = object.__new__(PdfRoute)
            route.config = config
            route.run_id = 7
            route.cancellation = CancellationToken()
            ordered = [Path(item.path).stem for item in route._candidate_snapshots()]

        self.assertEqual(
            ordered,
            ["cached", "legacy", "uncertain", "new", "processing", "timeout"],
        )

    def test_migrates_pdf_schema_one_without_discarding_core_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "pdf.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
                    INSERT INTO metadata VALUES('schema_version','1');
                    CREATE TABLE documents(
                        file_key TEXT PRIMARY KEY,path TEXT,
                        normalized_text_xxh3_128 TEXT,
                        normalized_text_chars INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'done') WITHOUT ROWID;
                    CREATE TABLE pages(
                        file_key TEXT NOT NULL,page_number INTEGER NOT NULL,
                        source TEXT NOT NULL,text_zlib BLOB NOT NULL,text_chars INTEGER NOT NULL,
                        PRIMARY KEY(file_key,page_number)) WITHOUT ROWID;
                    """
                )
                connection.commit()
            _initialize(database)
            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                page_columns = {row[1] for row in connection.execute("PRAGMA table_info(pages)")}
                document_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(documents)")
                }
                fts_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='page_fts'"
                ).fetchone()
            self.assertEqual(version, str(PDF_SCHEMA_VERSION))
            self.assertIn("profile_json", page_columns)
            self.assertIn("template_simhash64", document_columns)
            self.assertIn("binary_xxh3_128", document_columns)
            self.assertIn("page_errors_count", document_columns)
            self.assertIn("transient_retry_count", document_columns)
            self.assertIn("next_retry_ns", document_columns)
            self.assertIn("birthtime_ns", document_columns)
            self.assertIsNotNone(fts_exists)
            with closing(sqlite3.connect(database)) as connection:
                warning_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='document_warnings'"
                ).fetchone()
            self.assertIsNotNone(warning_table)
            with closing(sqlite3.connect(database)) as connection:
                inventory_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='pdf_inventory'"
                ).fetchone()
                inventory_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(pdf_inventory)")
                }
            self.assertIsNotNone(inventory_table)
            self.assertIn("birthtime_ns", inventory_columns)
            with closing(sqlite3.connect(database)) as connection:
                layout_tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%layout%'"
                    )
                }
            self.assertTrue(
                {
                    "page_layouts",
                    "document_layouts",
                    "layout_groups",
                    "layout_group_members",
                }
                <= layout_tables
            )

    def test_extracts_incrementally_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "one.pdf"
            _write_pdf(pdf, "Subestacion transformador interruptor", title="one")
            database = root / "dedup.sqlite3"
            pdf_state = root / "pdf.sqlite3"
            with DedupIndex(database) as index:
                scan = index.scan(root, excluded_paths=())
                first_state = _State(index.snapshots(scan.scan_id))
                first = PdfRoute(
                    PdfRouteConfig(pdf_state, ocr_mode="never", workers=1),
                    index,
                    first_state,
                    1,
                    scan.scan_id,
                ).run()
                self.assertEqual(first.extracted, 1)
                self.assertEqual(first.cache_hits, 0)
                self.assertEqual(first.new_documents, 1)
                self.assertEqual(first.cache_refreshes, 0)
                self.assertEqual(first.retried_documents, 0)
                self.assertEqual(first.fts_pages_indexed, 1)
                self.assertEqual(first.profiles_built, 1)
                second_state = _State(index.snapshots(scan.scan_id))
                second = PdfRoute(
                    PdfRouteConfig(pdf_state, ocr_mode="never", workers=1),
                    index,
                    second_state,
                    2,
                    scan.scan_id,
                ).run()
                self.assertEqual(second.cache_hits, 1)
                self.assertEqual(second.new_documents, 0)
                self.assertEqual(second.cache_refreshes, 0)
                self.assertEqual(second.retried_documents, 0)
                self.assertEqual(second.extracted, 0)
                self.assertEqual(len(first_state.review_reconciliations), 1)
                self.assertEqual(len(second_state.review_reconciliations), 1)
                for state in (first_state, second_state):
                    reconciliation = state.review_reconciliations[0]
                    self.assertIn("pdf_password_required", reconciliation[4])
                    self.assertEqual(reconciliation[5], frozenset())
            with closing(sqlite3.connect(pdf_state)) as connection:
                row = connection.execute(
                    "SELECT status,page_count,native_pages,normalized_text_xxh3_128 FROM documents"
                ).fetchone()
                self.assertEqual(row[:3], ("done", 1, 1))
                self.assertIsNotNone(row[3])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1)
                profile = connection.execute("SELECT profile_json FROM pages").fetchone()[0]
                self.assertIn('"font_count"', profile)
            results = search_pdf_state(pdf_state, "transformador", 5)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["page_number"], 0)

    def test_done_cache_signature_refresh_is_not_reported_as_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "one.pdf"
            _write_pdf(pdf, "Subestacion", title="one")
            pdf_state = root / "pdf.sqlite3"
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                config = PdfRouteConfig(pdf_state, ocr_mode="never", workers=1)
                PdfRoute(
                    config,
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    1,
                    scan.scan_id,
                ).run()
                with closing(sqlite3.connect(pdf_state)) as connection:
                    connection.execute(
                        "UPDATE documents SET processing_signature='legacy-signature'"
                    )
                    connection.commit()
                refreshed = PdfRoute(
                    config,
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    2,
                    scan.scan_id,
                ).run()
            self.assertEqual(refreshed.cache_refreshes, 1)
            self.assertEqual(refreshed.retried_documents, 0)

    def test_cached_pdf_findings_are_republished_with_current_generation(self):
        state = _State()
        route = object.__new__(PdfRoute)
        route.framework_state = state
        route.run_id = 19
        route._review_reconciliation_lock = threading.Lock()
        route._review_reconciliations = []
        snapshots = tuple(
            FileSnapshot(f"cached-{number}.pdf", 1, number, 100, 10, 11) for number in range(1, 6)
        )
        decisions = (
            CacheDecision(
                True,
                "error",
                error_type="PdfDocumentTimeout",
                error_message="worker deadline exceeded",
            ),
            CacheDecision(
                True,
                "partial",
                error_type="PdfStructuralRecoveryFailed",
                error_message="all structural recovery engines failed",
            ),
            CacheDecision(
                True,
                "partial",
                error_type="PdfPageSequenceAborted",
                error_message='{"consecutive_errors":8}',
            ),
            CacheDecision(
                True,
                "protected",
                error_type="EncryptedPdf",
                error_message="password required",
            ),
            CacheDecision(
                True,
                "done",
                metadata_json=json.dumps(
                    {
                        "neocortex_recovery": {
                            "engine": "qpdf",
                            "primary_error": "invalid xref",
                        }
                    }
                ),
            ),
        )

        for snapshot, decision in zip(snapshots, decisions, strict=True):
            self.assertTrue(route._publish_cached_review(snapshot, decision))

        self.assertEqual(state.review_store_runs, [19, 19, 19, 19, 19])
        self.assertEqual(
            [candidate.reason_code for candidate in state.review_candidates],
            [
                "pdf_document_timeout",
                "pdf_structural_damage_with_recoverable_pages",
                "pdf_structural_damage",
                "pdf_password_required",
                "pdf_structural_recovered",
            ],
        )
        self.assertEqual(
            state.review_candidates[3].snapshot.path,
            "cached-4.pdf",
        )
        route._flush_review_reconciliations()
        self.assertEqual(len(state.review_reconciliations), 1)
        self.assertEqual(
            state.review_reconciliations[0][5],
            frozenset({"pdf_structural_recovered"}),
        )

    def test_pdf_route_republishes_incomplete_cache_hits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = tuple(
                root / f"cached-{name}.pdf"
                for name in (
                    "error",
                    "partial",
                    "protected",
                )
            )
            for path in paths:
                _write_pdf(path, "contenido", title=path.stem)
            pdf_state = root / "pdf.sqlite3"
            config = PdfRouteConfig(
                pdf_state,
                ocr_mode="never",
                workers=1,
                min_free_bytes=0,
            )
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                snapshots = tuple(
                    snapshot
                    for snapshot in index.snapshots(scan.scan_id)
                    if snapshot.path.casefold().endswith(".pdf")
                )
                by_name = {Path(snapshot.path).stem: snapshot for snapshot in snapshots}
                _initialize(pdf_state)
                cached_rows = (
                    (
                        by_name["cached-error"],
                        "error",
                        "PdfSyntaxError",
                        "unexpected EOF while reading xref",
                    ),
                    (
                        by_name["cached-partial"],
                        "partial",
                        "PdfStructuralRecoveryFailed",
                        "all structural recovery engines failed",
                    ),
                    (
                        by_name["cached-protected"],
                        "protected",
                        "EncryptedPdf",
                        "password required",
                    ),
                )
                with closing(sqlite3.connect(pdf_state)) as connection:
                    connection.executemany(
                        """INSERT INTO documents(
                        file_key,path,size,mtime_ns,birthtime_ns,
                        processing_signature,status,completed_pages,
                        page_errors_count,error_type,error_message,updated_ns)
                        VALUES(?,?,?,?,?,?,?,0,0,?,?,?)""",
                        (
                            (
                                f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}",
                                snapshot.path,
                                snapshot.size,
                                snapshot.mtime_ns,
                                snapshot.birthtime_ns,
                                config.processing_signature,
                                status,
                                error_type,
                                error_message,
                                time.time_ns(),
                            )
                            for snapshot, status, error_type, error_message in cached_rows
                        ),
                    )
                    connection.commit()
                state = _State(snapshots)

                summary = PdfRoute(
                    config,
                    index,
                    state,
                    33,
                    scan.scan_id,
                ).run()

            self.assertEqual(summary.cache_hits, 3)
            self.assertEqual(summary.cached_errors, 1)
            self.assertEqual(state.review_store_runs, [33, 33, 33])
            self.assertEqual(
                {candidate.reason_code for candidate in state.review_candidates},
                {
                    "pdf_structural_damage",
                    "pdf_structural_damage_with_recoverable_pages",
                    "pdf_password_required",
                },
            )
            self.assertEqual(
                {candidate.snapshot.path for candidate in state.review_candidates},
                {str(path) for path in paths},
            )

    def test_complete_legacy_recovery_is_promoted_to_cache_hit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "one.pdf"
            _write_pdf(pdf, "Subestacion", title="one")
            pdf_state = root / "pdf.sqlite3"
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                config = PdfRouteConfig(pdf_state, ocr_mode="never", workers=1)
                PdfRoute(
                    config,
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    1,
                    scan.scan_id,
                ).run()
                with closing(sqlite3.connect(pdf_state)) as connection:
                    connection.execute(
                        """UPDATE documents SET status='partial',is_partial=0,
                        metadata_json='{"neocortex_recovery":{"engine":"qpdf"}}'"""
                    )
                    connection.commit()
                reused = PdfRoute(
                    config,
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    2,
                    scan.scan_id,
                ).run()
            self.assertEqual(reused.cache_hits, 1)
            self.assertEqual(reused.retried_documents, 0)
            with closing(sqlite3.connect(pdf_state)) as connection:
                status = connection.execute("SELECT status FROM documents").fetchone()[0]
            self.assertEqual(status, "done")

    def test_full_cache_validation_rechecks_binary_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "strict.pdf"
            _write_pdf(pdf, "Proteccion de transformador", title="strict")
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                snapshots = [
                    item for item in index.snapshots(scan.scan_id) if item.path == str(pdf)
                ]
                config = PdfRouteConfig(
                    root / "pdf.sqlite3",
                    ocr_mode="never",
                    workers=1,
                    cache_validation="full",
                )
                first = PdfRoute(
                    config,
                    index,
                    _State(snapshots),
                    1,
                    scan.scan_id,
                ).run()
                self.assertEqual(first.extracted, 1)
                route = PdfRoute(config, index, _State(snapshots), 2, scan.scan_id)
                with patch(
                    "neocortex.capabilities.formats.pdf.pdf_cache.full_fingerprint",
                    return_value=b"\xff" * 16,
                ):
                    self.assertFalse(route._is_cache_hit(snapshots[0]))

            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                digest = connection.execute("SELECT binary_xxh3_128 FROM documents").fetchone()[0]
            self.assertEqual(len(digest), 32)

    def test_unexpected_pdf_worker_failure_is_not_silently_counted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "worker-failure.pdf"
            _write_pdf(pdf, "Protección", title="failure")
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                route = PdfRoute(
                    PdfRouteConfig(
                        root / "pdf.sqlite3",
                        ocr_mode="never",
                        workers=1,
                    ),
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    1,
                    scan.scan_id,
                )
                with (
                    patch.object(
                        route,
                        "_process_document",
                        side_effect=RuntimeError("controlled worker failure"),
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "controlled worker failure",
                    ),
                ):
                    route.run()

    def test_automatic_page_retry_is_visible_and_stops_at_durable_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "pdf.sqlite3"
            config = PdfRouteConfig(state, ocr_mode="auto", workers=1)
            snapshot = FileSnapshot(
                str(root / "partial.pdf"),
                1,
                2,
                100,
                11,
                12,
            )
            key = f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}"
            _initialize(state)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
                    completed_pages,page_errors_count,transient_retry_count,
                    updated_ns) VALUES(?,?,?,?,?,?,'partial',1,1,0,?)""",
                    (
                        key,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        config.processing_signature,
                        time.time_ns(),
                    ),
                )
                connection.execute(
                    "INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars) "
                    "VALUES(?,0,'error',?,0)",
                    (key, zlib.compress(b"")),
                )
                connection.execute(
                    """INSERT INTO page_errors(
                    file_key,processing_signature,page_number,error_type,
                    error_message,updated_ns) VALUES(?,?,0,'AttributeError',?,?)""",
                    (
                        key,
                        config.processing_signature,
                        "'BoundedSemaphore' object has no attribute 'get'",
                        time.time_ns(),
                    ),
                )
                connection.commit()

            route = object.__new__(PdfRoute)
            route.config = config
            decision = route._is_cache_hit(snapshot, touch=False)
            self.assertFalse(decision)
            self.assertEqual(decision.retry_pages, 1)

            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    "UPDATE documents SET transient_retry_count=3,next_retry_ns=NULL"
                )
                connection.commit()
            exhausted = route._is_cache_hit(snapshot, touch=False)
            self.assertTrue(exhausted)

            route.config = PdfRouteConfig(
                state,
                ocr_mode="auto",
                workers=1,
                retry_errors=True,
            )
            forced = route._is_cache_hit(snapshot, touch=False)
            self.assertFalse(forced)
            self.assertEqual(forced.retry_pages, 1)

    def test_legacy_and_progressive_timeouts_resume_past_retry_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "pdf.sqlite3"
            config = PdfRouteConfig(state, ocr_mode="never", workers=1)
            snapshot = FileSnapshot(
                str(root / "large.pdf"),
                1,
                3,
                100,
                11,
                12,
            )
            key = f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}"
            _initialize(state)
            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,page_count,completed_pages,page_errors_count,error_type,
                    error_message,transient_retry_count,updated_ns)
                    VALUES(?,?,?,?,?,?,'error',100,50,0,'PdfDocumentTimeout',
                    'legacy timeout',3,?)""",
                    (
                        key,
                        snapshot.path,
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                        config.processing_signature,
                        time.time_ns(),
                    ),
                )
                connection.commit()

            route = object.__new__(PdfRoute)
            route.config = config
            self.assertFalse(route._is_cache_hit(snapshot, touch=False))

            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """UPDATE documents SET
                    error_message='[no-durable-progress:50] timeout'"""
                )
                connection.commit()
            self.assertTrue(route._is_cache_hit(snapshot, touch=False))

            with closing(sqlite3.connect(state)) as connection:
                connection.execute(
                    """UPDATE documents SET
                    error_message='[durable-progress:40->50] timeout'"""
                )
                connection.commit()
            self.assertFalse(route._is_cache_hit(snapshot, touch=False))

    def test_repairs_missing_pdf_cache_layers_selectively(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "repair.pdf"
            _write_pdf(pdf, "Control protección interruptor", title="repair")
            pdf_state = root / "pdf.sqlite3"
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                snapshots = list(index.snapshots(scan.scan_id))
                config = PdfRouteConfig(pdf_state, ocr_mode="never", workers=1)
                PdfRoute(config, index, _State(snapshots), 1, scan.scan_id).run()
                with closing(sqlite3.connect(pdf_state)) as connection:
                    connection.execute("DELETE FROM page_fts")
                    connection.execute("DELETE FROM page_layouts")
                    connection.commit()
                repaired_derived = PdfRoute(config, index, _State(snapshots), 2, scan.scan_id).run()
                self.assertEqual(repaired_derived.cache_hits, 1)
                self.assertEqual(repaired_derived.fts_pages_indexed, 1)
                self.assertEqual(repaired_derived.profiles_built, 1)
                self.assertEqual(repaired_derived.layout_pages_mapped, 1)
                with closing(sqlite3.connect(pdf_state)) as connection:
                    connection.execute("DELETE FROM pages")
                    connection.commit()
                repaired_extraction = PdfRoute(
                    config, index, _State(snapshots), 3, scan.scan_id
                ).run()
            self.assertEqual(repaired_extraction.cache_hits, 0)
            self.assertEqual(repaired_extraction.extracted, 1)
            with closing(sqlite3.connect(pdf_state)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0], 1
                )

    def test_pdf_cache_pruning_respects_limits_and_removes_deleted_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            _write_pdf(first_pdf, "Primer documento", title="first")
            _write_pdf(second_pdf, "Segundo documento", title="second")
            pdf_state = root / "pdf.sqlite3"
            with DedupIndex(root / "dedup.sqlite3") as index:
                first_scan = index.scan(root, excluded_paths=())
                candidates = list(index.snapshots(first_scan.scan_id))
                PdfRoute(
                    PdfRouteConfig(pdf_state, ocr_mode="never", workers=1),
                    index,
                    _State(candidates),
                    1,
                    first_scan.scan_id,
                ).run()
                limited = PdfRoute(
                    PdfRouteConfig(pdf_state, ocr_mode="never", workers=1, max_documents=1),
                    index,
                    _State(candidates),
                    2,
                    first_scan.scan_id,
                ).run()
                self.assertEqual(limited.pdf_cache_documents_pruned, 0)
                second_pdf.unlink()
                second_scan = index.scan(root, excluded_paths=())
                pruned = PdfRoute(
                    PdfRouteConfig(pdf_state, ocr_mode="never", workers=1, max_documents=1),
                    index,
                    _State(index.snapshots(second_scan.scan_id)),
                    3,
                    second_scan.scan_id,
                ).run()
            self.assertEqual(pruned.pdf_cache_documents_pruned, 1)
            with closing(sqlite3.connect(pdf_state)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                    1,
                )

    def test_page_range_is_persisted_and_excluded_from_full_document_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "range.pdf"
            _write_pdf_pages(pdf, ["Pagina uno", "Pagina dos", "Pagina tres"])
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                summary = PdfRoute(
                    PdfRouteConfig(
                        root / "pdf.sqlite3",
                        ocr_mode="never",
                        workers=1,
                        page_start=2,
                        page_end=2,
                    ),
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    1,
                    scan.scan_id,
                ).run()
            self.assertEqual(summary.errors, 0)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                document = connection.execute(
                    "SELECT page_start,page_end,is_partial FROM documents"
                ).fetchone()
                pages = connection.execute(
                    "SELECT page_number FROM pages ORDER BY page_number"
                ).fetchall()
            self.assertEqual(document, (2, 2, 1))
            self.assertEqual(pages, [(1,)])

    def test_page_error_preserves_other_pages_as_partial_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "partial.pdf"
            _write_pdf_pages(pdf, ["Primera", "Segunda", "Tercera"])
            real_open = fitz.open

            class FailingDocument:
                def __init__(self, path):
                    self._document = real_open(path)

                def __getattr__(self, name):
                    return getattr(self._document, name)

                def load_page(self, page_number):
                    if page_number == 1:
                        raise RuntimeError("controlled page failure")
                    return self._document.load_page(page_number)

            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                with patch("fitz.open", side_effect=FailingDocument):
                    summary = PdfRoute(
                        PdfRouteConfig(root / "pdf.sqlite3", ocr_mode="never", workers=1),
                        index,
                        _State(index.snapshots(scan.scan_id)),
                        1,
                        scan.scan_id,
                    ).run()
            self.assertEqual(summary.partial_documents, 1)
            self.assertEqual(summary.page_errors, 1)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                status = connection.execute(
                    "SELECT status,page_errors_count FROM documents"
                ).fetchone()
                page_sources = connection.execute(
                    "SELECT source FROM pages ORDER BY page_number"
                ).fetchall()
            self.assertEqual(status, ("partial", 1))
            self.assertEqual(page_sources, [("native",), ("error",), ("native",)])

    def test_isolated_extraction_and_hard_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "isolated.pdf"
            _write_pdf(pdf, "Subestacion aislada", title="isolated")
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                snapshots = list(index.snapshots(scan.scan_id))
                completed = PdfRoute(
                    PdfRouteConfig(
                        root / "pdf-ok.sqlite3",
                        ocr_mode="never",
                        workers=1,
                        document_timeout_seconds=30,
                        min_free_bytes=0,
                        memory_backpressure_bytes=0,
                    ),
                    index,
                    _State(snapshots),
                    1,
                    scan.scan_id,
                ).run()
                timed_out = PdfRoute(
                    PdfRouteConfig(
                        root / "pdf-timeout.sqlite3",
                        ocr_mode="never",
                        workers=1,
                        document_timeout_seconds=0.001,
                        min_free_bytes=0,
                        memory_backpressure_bytes=0,
                    ),
                    index,
                    _State(snapshots),
                    2,
                    scan.scan_id,
                ).run()
            self.assertEqual(completed.extracted, 1)
            self.assertEqual(completed.errors, 0)
            self.assertEqual(timed_out.document_timeouts, 1)
            self.assertEqual(timed_out.errors, 1)

    def test_timeout_flushes_sub_batch_progress_for_next_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "resume-timeout.pdf"
            _write_pdf_pages(pdf, ["Uno", "Dos", "Tres", "Cuatro"])
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                snapshot = next(index.snapshots(scan.scan_id))
                route = PdfRoute(
                    PdfRouteConfig(
                        root / "pdf.sqlite3",
                        ocr_mode="never",
                        workers=1,
                        document_timeout_seconds=30,
                        min_free_bytes=0,
                    ),
                    index,
                    _State((snapshot,)),
                    1,
                    scan.scan_id,
                )

                def interrupted_stream(*args, **kwargs):
                    yield ("header", 4, 0, 4, {})
                    for page_number in range(3):
                        yield ("page", page_number, "native", str(page_number))
                    raise PdfDocumentTimeout("controlled timeout", phase="page_extraction")

                with patch(
                    "neocortex.capabilities.formats.pdf.pdf_route.stream_isolated_extraction",
                    side_effect=interrupted_stream,
                ):
                    result = route._process_document_isolated(snapshot, None)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.status, "partial")
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                staged = connection.execute("SELECT COUNT(*) FROM page_staging").fetchone()[0]
                status, completed, retry_count, next_retry_ns, error_message = connection.execute(
                    """SELECT status,completed_pages,transient_retry_count,
                        next_retry_ns,error_message FROM documents"""
                ).fetchone()
            self.assertEqual((staged, completed), (3, 3))
            self.assertEqual(status, "partial")
            self.assertEqual(retry_count, 0)
            self.assertIsNone(next_retry_ns)
            self.assertTrue(error_message.startswith("[durable-progress:0->3]"))

    def test_document_error_retry_replaces_stale_page_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "retry-timeout.pdf"
            _write_pdf_pages(pdf, ["Uno", "Dos"])
            snapshot = snapshot_path(pdf)
            route = PdfRoute(
                PdfRouteConfig(
                    root / "pdf.sqlite3",
                    ocr_mode="never",
                    workers=1,
                    document_timeout_seconds=30,
                    min_free_bytes=0,
                ),
                SimpleNamespace(),
                _State((snapshot,)),
                1,
                1,
            )

            def timed_out_stream(*args, **kwargs):
                yield ("header", 2, 0, 2, {})
                yield ("page", 0, "native", "Uno")
                yield (
                    "page_error",
                    1,
                    "FzErrorFormat",
                    "cannot find page 2 in page tree",
                )
                raise PdfDocumentTimeout(
                    "controlled timeout",
                    phase="page_extraction",
                )

            with patch(
                "neocortex.capabilities.formats.pdf.pdf_route.stream_isolated_extraction",
                side_effect=timed_out_stream,
            ):
                first = route._process_document_isolated(snapshot, None)
            self.assertTrue(first.timed_out)

            def resumed_stream(*args, **kwargs):
                yield ("header", 2, 0, 2, {})
                yield ("page", 1, "native", "Dos")
                yield ("done", 2, 0, 2)

            with patch(
                "neocortex.capabilities.formats.pdf.pdf_route.stream_isolated_extraction",
                side_effect=resumed_stream,
            ):
                resumed = route._process_document_isolated(snapshot, None)

            self.assertEqual(resumed.status, "done")
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                stored = connection.execute(
                    "SELECT status,completed_pages,page_errors_count FROM documents"
                ).fetchone()
                page_errors = connection.execute("SELECT COUNT(*) FROM page_errors").fetchone()[0]
                pages = connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            self.assertEqual(stored, ("done", 2, 0))
            self.assertEqual(page_errors, 0)
            self.assertEqual(pages, 2)

    def test_page_error_limit_promotes_bounded_partial_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "page-tree.pdf"
            _write_pdf(pdf, "Página recuperable", title="partial")
            snapshot = snapshot_path(pdf)
            route = PdfRoute(
                PdfRouteConfig(
                    root / "pdf.sqlite3",
                    ocr_mode="never",
                    workers=1,
                    document_timeout_seconds=30,
                    min_free_bytes=0,
                ),
                SimpleNamespace(),
                _State((snapshot,)),
                1,
                1,
            )

            def bounded_failure_stream(*args, **kwargs):
                yield ("header", 100, 0, 100, {})
                for page_number in range(MAX_CONSECUTIVE_PAGE_ERRORS):
                    yield (
                        "page_error",
                        page_number,
                        "FzErrorFormat",
                        "cannot find page 10 in page tree",
                    )
                yield (
                    "page_error_limit",
                    MAX_CONSECUTIVE_PAGE_ERRORS - 1,
                    MAX_CONSECUTIVE_PAGE_ERRORS,
                    100 - MAX_CONSECUTIVE_PAGE_ERRORS,
                    "FzErrorFormat",
                    "cannot find page 10 in page tree",
                )
                yield ("done", 100, 0, 100)

            with patch(
                "neocortex.capabilities.formats.pdf.pdf_route.stream_isolated_extraction",
                side_effect=bounded_failure_stream,
            ):
                result = route._process_document_isolated(snapshot, None)

            self.assertEqual(result.status, "partial")
            self.assertEqual(result.page_errors, MAX_CONSECUTIVE_PAGE_ERRORS)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                stored = connection.execute(
                    """SELECT status,page_end,is_partial,completed_pages,
                    page_errors_count,error_type FROM documents"""
                ).fetchone()
            self.assertEqual(
                stored,
                (
                    "partial",
                    MAX_CONSECUTIVE_PAGE_ERRORS,
                    1,
                    MAX_CONSECUTIVE_PAGE_ERRORS,
                    MAX_CONSECUTIVE_PAGE_ERRORS,
                    "PdfPageSequenceAborted",
                ),
            )
            self.assertIsNotNone(route._structural_recovery_reason(snapshot))
            self.assertFalse(route._is_cache_hit(snapshot))

    def test_recovery_restart_clears_failed_qpdf_pages_and_promotes_done(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "recovered.pdf"
            _write_pdf(pdf, "contenido", title="recovered")
            snapshot = snapshot_path(pdf)
            route = PdfRoute(
                PdfRouteConfig(
                    root / "pdf.sqlite3",
                    ocr_mode="never",
                    workers=1,
                    document_timeout_seconds=30,
                    min_free_bytes=0,
                ),
                SimpleNamespace(),
                _State((snapshot,)),
                1,
                1,
            )

            def recovered_stream(*args, **kwargs):
                yield ("header", 100, 0, 100, {})
                for page_number in range(MAX_CONSECUTIVE_PAGE_ERRORS):
                    yield (
                        "page_error",
                        page_number,
                        "FzErrorFormat",
                        "broken qpdf page tree",
                    )
                yield ("restart", "pdfminer_recovery", "qpdf failed")
                recovery = {"engine": "pdfminer", "qpdf_error": "page tree"}
                yield ("recovery", recovery)
                yield (
                    "header",
                    1,
                    0,
                    1,
                    {"engine": "pdfminer", "neocortex_recovery": recovery},
                )
                yield ("page", 0, "pdfminer", "contenido recuperado")
                yield ("done", 1, 0, 1)

            with patch(
                "neocortex.capabilities.formats.pdf.pdf_route.stream_isolated_extraction",
                side_effect=recovered_stream,
            ):
                result = route._process_document_isolated(snapshot, None)

            self.assertEqual(result.status, "done")
            self.assertEqual(result.page_errors, 0)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                stored = connection.execute(
                    "SELECT status,page_errors_count,error_type FROM documents"
                ).fetchone()
                errors = connection.execute("SELECT COUNT(*) FROM page_errors").fetchone()[0]
            self.assertEqual(stored, ("done", 0, None))
            self.assertEqual(errors, 0)

    def test_unrecoverable_pdf_with_useful_page_is_not_recycled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "partly-readable.pdf"
            _write_pdf(pdf, "contenido", title="partial")
            snapshot = snapshot_path(pdf)
            route = PdfRoute(
                PdfRouteConfig(
                    root / "pdf.sqlite3",
                    apply_actions=True,
                    ocr_mode="never",
                    document_timeout_seconds=30,
                    min_free_bytes=0,
                ),
                SimpleNamespace(),
                _State((snapshot,)),
                1,
                1,
            )

            def failed_stream(*args, **kwargs):
                yield ("header", 2, 0, 2, {})
                yield ("page", 0, "native", "contenido útil")
                yield (
                    "fatal",
                    "PdfStructuralRecoveryFailed",
                    "all engines failed",
                    "pdfminer_recovery",
                    False,
                    "deletion_candidate",
                    "pdf_unrecoverable_structural_damage",
                )

            with (
                patch(
                    "neocortex.capabilities.formats.pdf.pdf_route.stream_isolated_extraction",
                    side_effect=failed_stream,
                ),
                patch.object(route, "_recycle_unrecoverable_pdf") as recycle,
            ):
                result = route._process_document_isolated(snapshot, None)
            self.assertEqual(result.status, "partial")
            self.assertFalse(result.recycled)
            recycle.assert_not_called()

    def test_apply_authorizes_recycle_for_contentless_unrecoverable_pdf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "unreadable.pdf"
            _write_pdf(pdf, "contenido", title="unreadable")
            snapshot = snapshot_path(pdf)
            route = PdfRoute(
                PdfRouteConfig(
                    root / "pdf.sqlite3",
                    apply_actions=True,
                    ocr_mode="never",
                    document_timeout_seconds=30,
                    min_free_bytes=0,
                ),
                SimpleNamespace(),
                _State((snapshot,)),
                1,
                1,
            )

            def failed_stream(*args, **kwargs):
                yield (
                    "fatal",
                    "PdfStructuralRecoveryFailed",
                    "all engines failed",
                    "pdfminer_recovery",
                    False,
                    "deletion_candidate",
                    "pdf_unrecoverable_structural_damage",
                )

            with (
                patch(
                    "neocortex.capabilities.formats.pdf.pdf_route.stream_isolated_extraction",
                    side_effect=failed_stream,
                ),
                patch.object(
                    route,
                    "_recycle_unrecoverable_pdf",
                    return_value=True,
                ) as recycle,
            ):
                result = route._process_document_isolated(snapshot, None)
            self.assertEqual(result.status, "error")
            self.assertTrue(result.recycled)
            recycle.assert_called_once()

    def test_verified_recycle_resolves_only_current_unrecoverable_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "terminal-unreadable.pdf"
            _write_pdf(pdf, "contenido", title="unreadable")
            snapshot = snapshot_path(pdf)
            framework_database = root / "framework.sqlite3"
            with FrameworkState(framework_database):
                pass
            state = FrameworkRouteState(framework_database)
            route = PdfRoute(
                PdfRouteConfig(
                    root / "pdf.sqlite3",
                    apply_actions=True,
                    min_free_bytes=0,
                ),
                SimpleNamespace(),
                state,
                27,
                1,
            )
            diagnostic = classify_pdf_failure(
                "PdfStructuralRecoveryFailed",
                "all engines failed",
                phase="pdfminer_recovery",
            )
            route._publish_review(
                snapshot,
                diagnostic,
                "error",
                evidence={"message": "all engines failed"},
            )
            route._publish_review(
                snapshot,
                classify_pdf_failure(
                    "EncryptedPdf",
                    "password required",
                    phase="open",
                ),
                "protected",
                evidence={"message": "password required"},
            )

            with patch("neocortex.workflow.actions.actions.FrameworkActions") as actions_type:
                actions_type.return_value.recycle_verified_files.return_value = (
                    1,
                    0,
                    0,
                )
                self.assertTrue(
                    route._recycle_unrecoverable_pdf(
                        snapshot,
                        {"message": "all engines failed"},
                    )
                )

            self.assertEqual(
                {
                    record.reason_code
                    for record in list_review_candidates(
                        framework_database,
                        limit=10,
                    )
                },
                {"pdf_password_required"},
            )
            resolved = list_review_candidates(
                framework_database,
                limit=10,
                status="resolved",
            )
            self.assertEqual(len(resolved), 1)
            self.assertEqual(
                resolved[0].reason_code,
                "pdf_unrecoverable_structural_damage",
            )
            self.assertEqual(resolved[0].resolved_generation, 27)

    def test_contentless_unrecoverable_pdf_is_preserved_without_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "unreadable.pdf"
            _write_pdf(pdf, "contenido", title="unreadable")
            snapshot = snapshot_path(pdf)
            route = PdfRoute(
                PdfRouteConfig(
                    root / "pdf.sqlite3",
                    ocr_mode="never",
                    document_timeout_seconds=30,
                    min_free_bytes=0,
                ),
                SimpleNamespace(),
                _State((snapshot,)),
                1,
                1,
            )

            def failed_stream(*args, **kwargs):
                yield (
                    "fatal",
                    "PdfStructuralRecoveryFailed",
                    "all engines failed",
                    "pdfminer_recovery",
                    False,
                    "deletion_candidate",
                    "pdf_unrecoverable_structural_damage",
                )

            with (
                patch(
                    "neocortex.capabilities.formats.pdf.pdf_route.stream_isolated_extraction",
                    side_effect=failed_stream,
                ),
                patch.object(route, "_recycle_unrecoverable_pdf") as recycle,
            ):
                result = route._process_document_isolated(snapshot, None)
            self.assertEqual(result.status, "error")
            self.assertFalse(result.recycled)
            recycle.assert_not_called()

    def test_parallel_isolated_documents_use_one_sqlite_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdfs = []
            for number in range(8):
                path = root / f"parallel-{number}.pdf"
                _write_pdf(path, f"Documento paralelo {number}", title=str(number))
                pdfs.append(path)
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                state = _State(index.snapshots(scan.scan_id))
                summary = PdfRoute(
                    PdfRouteConfig(
                        root / "pdf.sqlite3",
                        ocr_mode="never",
                        workers=4,
                        document_timeout_seconds=30,
                        min_free_bytes=0,
                    ),
                    index,
                    state,
                    1,
                    scan.scan_id,
                ).run()
            self.assertEqual(summary.processed, len(pdfs))
            self.assertEqual(summary.extracted, len(pdfs))
            self.assertEqual(summary.errors, 0)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                statuses = connection.execute(
                    "SELECT status,COUNT(*) FROM documents GROUP BY status"
                ).fetchall()
            self.assertEqual(statuses, [("done", len(pdfs))])

    def test_retry_reprocesses_only_failed_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "retry.pdf"
            _write_pdf_pages(pdf, ["Uno", "Dos", "Tres"])
            real_open = fitz.open

            class FailingDocument:
                def __init__(self, path):
                    self._document = real_open(path)

                def __getattr__(self, name):
                    return getattr(self._document, name)

                def load_page(self, page_number):
                    if page_number == 1:
                        raise RuntimeError("controlled retry failure")
                    return self._document.load_page(page_number)

            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                snapshots = list(index.snapshots(scan.scan_id))
                with patch("fitz.open", side_effect=FailingDocument):
                    first = PdfRoute(
                        PdfRouteConfig(root / "pdf.sqlite3", ocr_mode="never", workers=1),
                        index,
                        _State(snapshots),
                        1,
                        scan.scan_id,
                    ).run()
                second = PdfRoute(
                    PdfRouteConfig(
                        root / "pdf.sqlite3",
                        ocr_mode="never",
                        workers=1,
                        retry_errors=True,
                    ),
                    index,
                    _State(snapshots),
                    2,
                    scan.scan_id,
                ).run()
            self.assertEqual(first.page_errors, 1)
            self.assertEqual(second.native_pages, 1)
            self.assertEqual(second.retried_documents, 1)
            self.assertEqual(second.retry_pages_planned, 1)
            self.assertEqual(second.partial_documents, 0)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                status = connection.execute(
                    "SELECT status,page_errors_count FROM documents"
                ).fetchone()
                remaining_errors = connection.execute(
                    "SELECT COUNT(*) FROM page_errors"
                ).fetchone()[0]
            self.assertEqual(status, ("done", 0))
            self.assertEqual(remaining_errors, 0)

    def test_text_dedup_is_advisory_even_when_apply_is_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = root / "older.pdf"
            newer = root / "newer.pdf"
            text = "Mantenimiento de transformador de potencia"
            _write_pdf(older, text, title="old metadata")
            _write_pdf(newer, text, title="new metadata")
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
            state = _State()
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                state.candidates = list(index.snapshots(scan.scan_id))

                summary = PdfRoute(
                    PdfRouteConfig(
                        root / "pdf.sqlite3",
                        apply_actions=True,
                        ocr_mode="never",
                        workers=1,
                    ),
                    index,
                    state,
                    1,
                    scan.scan_id,
                ).run()
                self.assertEqual(summary.text_duplicate_groups, 1)
                self.assertEqual(summary.text_duplicate_candidates, 1)
                self.assertEqual(summary.text_duplicate_policy, "advisory")
                self.assertEqual(summary.text_duplicates_trashed, 0)
                self.assertTrue(older.exists())
                self.assertTrue(newer.exists())
                indexed_pdfs = [
                    snapshot.path
                    for snapshot in index.snapshots(scan.scan_id)
                    if snapshot.path.casefold().endswith(".pdf")
                ]
                self.assertEqual(indexed_pdfs, sorted((str(older), str(newer))))
                self.assertEqual(state.actions[0]["values"][0], "review_pdf_text_duplicate")
                self.assertFalse(state.actions[0]["values"][5])
                self.assertEqual(state.actions[0]["status"], "planned")
                self.assertIn("Advisory only", state.actions[0]["detail"])

    def test_auto_ocr_only_for_page_without_native_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "scan.pdf"
            document = fitz.open()
            document.new_page()
            document.save(pdf)
            document.close()
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                route = PdfRoute(
                    PdfRouteConfig(root / "pdf.sqlite3", ocr_mode="auto", workers=1),
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    1,
                    scan.scan_id,
                )
                with patch.object(route, "_ocr_page", return_value="Tablero de control"):
                    summary = route.run()
                self.assertEqual(summary.ocr_pages, 1)
                self.assertEqual(summary.errors, 0)

    def test_corrupt_pdf_error_is_persisted_and_not_retried_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            damaged = root / "damaged.pdf"
            damaged.write_bytes(b"%PDF-1.7\nnot a valid PDF body")
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                first = PdfRoute(
                    PdfRouteConfig(root / "pdf.sqlite3", ocr_mode="never", workers=1),
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    1,
                    scan.scan_id,
                ).run()
                second = PdfRoute(
                    PdfRouteConfig(root / "pdf.sqlite3", ocr_mode="never", workers=1),
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    2,
                    scan.scan_id,
                ).run()
                self.assertEqual(first.errors, 1)
                self.assertEqual(second.cache_hits, 1)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute("SELECT status FROM documents").fetchone()[0],
                    "error",
                )

    def test_pdfminer_fallback_extracts_when_pymupdf_open_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "fallback.pdf"
            _write_pdf(pdf, "Proteccion y control", title="fallback")
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                route = PdfRoute(
                    PdfRouteConfig(root / "pdf.sqlite3", ocr_mode="never", workers=1),
                    index,
                    _State(index.snapshots(scan.scan_id)),
                    1,
                    scan.scan_id,
                )
                with patch("fitz.open", side_effect=RuntimeError("forced primary failure")):
                    summary = route.run()
                self.assertEqual(summary.extracted, 1)
                self.assertEqual(summary.errors, 0)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                metadata = connection.execute("SELECT metadata_json FROM documents").fetchone()[0]
                self.assertIn('"engine":"pdfminer"', metadata)

    def test_records_bounded_text_and_template_similarity_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_pdf(
                root / "one.pdf",
                "mantenimiento preventivo transformador potencia subestacion norte",
                title="one",
            )
            _write_pdf(
                root / "two.pdf",
                "mantenimiento preventivo transformador potencia subestacion sur",
                title="two",
            )
            with DedupIndex(root / "dedup.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                candidates = list(index.snapshots(scan.scan_id))
                config = PdfRouteConfig(
                    root / "pdf.sqlite3",
                    ocr_mode="never",
                    workers=2,
                    similarity_threshold=0.70,
                )
                summary = PdfRoute(
                    config,
                    index,
                    _State(candidates),
                    1,
                    scan.scan_id,
                ).run()
                self.assertGreaterEqual(summary.text_similarity_pairs, 1)
                self.assertGreaterEqual(summary.template_similarity_pairs, 1)
                self.assertGreaterEqual(summary.layout_similarity_pairs, 1)
                self.assertGreaterEqual(summary.layout_groups, 1)
                self.assertEqual(summary.layout_pages_mapped, 2)
                with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                    connection.execute("DELETE FROM similarity_relations")
                    connection.commit()
                repaired = PdfRoute(config, index, _State(candidates), 2, scan.scan_id).run()
                self.assertGreaterEqual(repaired.text_similarity_pairs, 1)
                self.assertGreaterEqual(repaired.template_similarity_pairs, 1)
                self.assertGreaterEqual(repaired.layout_similarity_pairs, 1)
            with closing(sqlite3.connect(root / "pdf.sqlite3")) as connection:
                kinds = {
                    row[0]
                    for row in connection.execute("SELECT DISTINCT kind FROM similarity_relations")
                }
                self.assertEqual(kinds, {"text_similar", "template_similar", "layout_similar"})
                layout_payload = json.loads(
                    zlib.decompress(
                        connection.execute(
                            "SELECT layout_zlib FROM page_layouts LIMIT 1"
                        ).fetchone()[0]
                    ).decode("utf-8")
                )
                self.assertEqual(layout_payload["algorithm_version"], 1)
                self.assertEqual(len(layout_payload["visual_grid"]), 320)
                self.assertTrue(layout_payload["blocks"])


if __name__ == "__main__":
    unittest.main()
# endregion [02]
