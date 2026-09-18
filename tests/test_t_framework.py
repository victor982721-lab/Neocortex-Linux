# region [00] Contexto del módulo
# Módulo: tests/test_t_framework.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations


import io
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import DedupIndex
from neocortex.api.public import FrameworkConfig, FrameworkOrchestrator, RouteAdapter
from neocortex.integrations.inventory.reconcile import reconcile_usn_window
from neocortex.runtime.orchestration.orchestrator import RouteExecutionError
from neocortex.documents.document_organization import (
    OrganizationApplySummary,
)
from neocortex.documents.document_organization_planning import (
    plan_document_organization as plan_document_organization_owner,
)
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.platform.policy import default_corpus_root
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    normalize_route_selection,
)
from neocortex.runtime.orchestration.route_registry import builtin_route_registry
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.progress import (
    LineProgress,
    ProgressEvent,
    ProgressMetric,
    RecordingProgress,
    RichProgress,
)
from neocortex.api.cli.cli_parser import build_parser as _parser
from neocortex.api.cli.cli_reporting import (
    has_strict_route_errors as _has_strict_route_errors,
)
from neocortex.api.cli.cli_validation import validate_arguments as _validate_arguments
from tests.internal_paths_test_support import (
    begin_signed_normal_run,
    disjoint_internal_paths_policy,
)
from tests.synthetic_usn import SyntheticUsnJournal


TEST_CAPABILITIES = ("base", "documents", "image", "inference")


@contextmanager
def _coordinator_effect_capabilities():
    """Supply prerequisite owners for ordering tests with no real file effects.

    prepare_framework_run itself still executes; its rejection paths are tested
    independently. These doubles do not certify the host's SQLite or KDE.
    """
    with (
        patch("neocortex.platform.sqlite_runtime_attestation.observe_platform_native_runtime",
              return_value={"status": "approved", "observed": {"identity_sha256": "fixture-owner"}}),
        patch("neocortex.safety.kio_trash.preflight_kio_trash",
              return_value=SimpleNamespace(client=Path("/fixture/kioclient"))),
    ):
        yield


@contextmanager
def _coordinator_available_memory():
    """A stable owner observation for coordination, independent of other tests.

    Route decoding and extraction remain real; host capacity is not under test.
    """
    from neocortex.runtime.control.memory_runtime import MemorySnapshot
    gib = 1024 ** 3
    snapshot = MemorySnapshot(8 * gib, 12 * gib, 16 * gib, 24 * gib)
    with (
        patch("neocortex.runtime.control.global_resources.memory_snapshot", return_value=snapshot),
        patch("neocortex.runtime.control.memory_runtime.memory_snapshot", return_value=snapshot),
    ):
        yield
# endregion [01]

# region [02] Implementación


class CommandLineTests(unittest.TestCase):
    def test_all_expands_to_complete_safe_maintenance_preset(self) -> None:
        parser = _parser()
        self.assertEqual(parser.prog, "Neocortex")
        args = parser.parse_args(["--all", *(["--apply"] if os.name == "nt" else [])])
        _validate_arguments(args)
        self.assertEqual(args.apply, os.name == "nt")
        self.assertEqual(args.route, "all")
        selected_routes = normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
        self.assertEqual(
            selected_routes,
            (
                "pdf",
                "docx",
                "office",
                "archive",
                "text",
                "audio",
                "video",
                "image",
                "code",
            ),
        )
        self.assertEqual(selected_routes, tuple(builtin_route_registry()))
        self.assertEqual(args.ocr, "auto")
        self.assertEqual(args.pdf_cache_validation, "metadata")
        self.assertEqual(args.pdf_large_document_workers, 2)
        self.assertFalse(args.retry_pdf_errors)
        self.assertFalse(args.retry_docx_errors)
        self.assertFalse(args.retry_office_errors)
        self.assertFalse(args.retry_audio_errors)
        self.assertFalse(args.retry_video_errors)
        self.assertFalse(args.retry_image_errors)
        self.assertFalse(args.retry_code_errors)
        self.assertEqual(args.image_document_ocr, "auto")
        self.assertIsNone(args.semantic_max_items)
        self.assertIsNone(args.semantic_max_new_jobs)
        self.assertIsNone(args.semantic_time_budget_seconds)
        self.assertEqual(args.code_candidate_scope, "projects")
        self.assertIsNone(args.global_memory_budget_mb)
        self.assertIsNone(args.global_min_free_memory_mb)
        self.assertIsNone(args.global_min_free_commit_mb)
        self.assertIsNone(args.global_cpu_slots)
        self.assertEqual(args.global_max_cpu_load_percent, 90.0)
        self.assertIsNone(args.pdf_max_documents)
        self.assertIsNone(args.docx_max_documents)
        self.assertIsNone(args.image_max_documents)
        self.assertIsNone(args.video_max_documents)

    def test_all_preserves_explicit_manual_retry_requests(self) -> None:
        args = _parser().parse_args(
            [
                "--all",
                "--retry-pdf-errors",
                "--retry-docx-errors",
                "--retry-office-errors",
                "--retry-audio-errors",
                "--retry-image-errors",
            ]
        )
        _validate_arguments(args)
        self.assertTrue(args.retry_pdf_errors)
        self.assertTrue(args.retry_docx_errors)
        self.assertTrue(args.retry_office_errors)
        self.assertTrue(args.retry_audio_errors)
        self.assertTrue(args.retry_image_errors)

    def test_all_preserves_explicit_compatible_overrides(self) -> None:
        args = _parser().parse_args(
            [
                "--all",
                "--global-cpu-slots",
                "8",
                "--global-max-cpu-load-percent=70",
                "--ocr",
                "never",
                "--pdf-cache-validation",
                "full",
                "--image-document-ocr",
                "never",
            ]
        )
        _validate_arguments(args)
        self.assertEqual(args.route, "all")
        self.assertEqual(args.global_cpu_slots, 8)
        self.assertEqual(args.global_max_cpu_load_percent, 70.0)
        self.assertEqual(args.ocr, "never")
        self.assertEqual(args.pdf_cache_validation, "full")
        self.assertEqual(args.image_document_ocr, "never")

    def test_all_rejects_a_narrower_explicit_route(self) -> None:
        args = _parser().parse_args(["--all", "--route", "pdf"])
        with self.assertRaisesRegex(SystemExit, "cannot be combined"):
            _validate_arguments(args)

    def test_strict_exit_detection_includes_partial_and_cached_route_errors(
        self,
    ) -> None:
        clean = type("Result", (), {"route_results": {"pdf": {"errors": 0}}})()
        partial = type(
            "Result",
            (),
            {"route_results": {"pdf": {"partial_documents": 1}}},
        )()
        cached = type(
            "Result",
            (),
            {"route_results": {"docx": {"cached_errors": 2}}},
        )()
        route_error = type(
            "Result",
            (),
            {"route_results": {"code": {"errors": 1}}},
        )()
        self.assertFalse(_has_strict_route_errors(clean))
        self.assertTrue(_has_strict_route_errors(partial))
        self.assertTrue(_has_strict_route_errors(cached))
        self.assertTrue(_has_strict_route_errors(route_error))

    def test_defaults_to_platform_corpus_and_unlimited_pdf_controls(self) -> None:
        args = _parser().parse_args([])
        self.assertEqual(args.root, default_corpus_root())
        self.assertIsNone(args.pdf_max_file_bytes)
        self.assertIsNone(args.pdf_max_documents)

    def test_decimal_megabytes_and_max_count_are_human_facing(self) -> None:
        args = _parser().parse_args(["--MaxMB", "1000", "--MaxCount", "25"])
        _validate_arguments(args)
        self.assertEqual(args.pdf_max_file_bytes, 1_000_000_000)
        self.assertEqual(args.pdf_max_documents, 25)
        decimal = _parser().parse_args(["--max-mb", "1.5"])
        self.assertEqual(decimal.pdf_max_file_bytes, 1_500_000)

    def test_rejects_nonpositive_max_count(self) -> None:
        args = _parser().parse_args(["--MaxCount", "0"])
        with self.assertRaises(SystemExit):
            _validate_arguments(args)

    def test_integrated_image_route_arguments(self) -> None:
        args = _parser().parse_args(
            [
                "--route",
                "image",
                "--image-max-mb",
                "12.5",
                "--image-max-count",
                "200",
                "--image-memory-budget-mb",
                "384",
                "--image-document-ocr",
                "never",
                "--image-ocr-lang",
                "spa",
                "--image-ocr-timeout",
                "8",
            ]
        )
        _validate_arguments(args)
        self.assertEqual(args.route, "image")
        self.assertEqual(args.image_max_file_bytes, 12_500_000)
        self.assertEqual(args.image_max_documents, 200)
        self.assertEqual(args.image_memory_budget_mb, 384)
        self.assertEqual(args.image_worker_timeout, 120.0)
        self.assertEqual(args.image_document_ocr, "never")
        self.assertEqual(args.image_ocr_lang, "spa")
        self.assertEqual(args.image_ocr_timeout, 8.0)

    def test_accepts_multiple_routes_and_global_resource_limits(self) -> None:
        args = _parser().parse_args(
            [
                "--route",
                "pdf,image,docx",
                "--global-memory-budget-mb",
                "1024",
                "--global-min-free-memory-mb",
                "512",
                "--global-min-free-commit-mb",
                "512",
                "--global-cpu-slots",
                "3",
                "--global-max-cpu-load-percent",
                "85",
            ]
        )
        _validate_arguments(args)
        self.assertEqual(args.route, "pdf,image,docx")
        self.assertEqual(args.global_memory_budget_mb, 1024)
        self.assertEqual(args.global_cpu_slots, 3)
        self.assertEqual(args.global_max_cpu_load_percent, 85)
        with self.assertRaises(SystemExit):
            _validate_arguments(_parser().parse_args(["--route", "pdf,unknown"]))

    def test_layout_group_query_is_bounded(self) -> None:
        args = _parser().parse_args(["--pdf-layout-groups", "20"])
        _validate_arguments(args)
        self.assertEqual(args.pdf_layout_groups, 20)
        with self.assertRaises(SystemExit):
            _validate_arguments(_parser().parse_args(["--pdf-layout-groups", "101"]))


class ProgressTests(unittest.TestCase):
    def test_framework_stream_environment_selects_line_reporter(self) -> None:
        from neocortex.api.cli.cli_app import run_framework

        observed: list[object] = []

        def record(_args, progress):
            observed.append(progress)
            return "ok"

        with (
            patch.dict(os.environ, {"NEOCORTEX_PROGRESS_STREAM": "1"}),
            patch(
                "neocortex.api.cli.cli_app._run_framework_with_progress",
                side_effect=record,
            ),
        ):
            self.assertEqual(run_framework(object()), "ok")

        self.assertEqual(len(observed), 1)
        self.assertIsInstance(observed[0], LineProgress)

    def test_line_reporter_flushes_machine_readable_progress_to_stderr(self) -> None:
        output = io.StringIO()
        with patch("sys.stderr", output), LineProgress() as progress:
            progress(
                ProgressEvent(
                    "code",
                    "analysis",
                    "Code analysis",
                    3,
                    5,
                    "shards",
                    metrics=(ProgressMetric("status", "shard_completed"),),
                )
            )

        rendered = output.getvalue()
        self.assertTrue(rendered.startswith("NEOCORTEX_PROGRESS "))
        self.assertIn('"completed":3', rendered)
        self.assertIn('"phase":"analysis"', rendered)
        self.assertIn('"status":"shard_completed"', rendered)

    def test_line_reporter_coalesces_noise_but_never_hides_terminal_progress(self) -> None:
        output = io.StringIO()
        now = [0.0]
        progress = LineProgress(clock=lambda: now[0])

        with patch("sys.stderr", output), progress:
            for completed in range(24):
                now[0] = float(completed)
                progress(
                    ProgressEvent(
                        "code",
                        "analysis",
                        "Incremental",
                        completed,
                        1000,
                        "files",
                    )
                )
            now[0] = 24.0
            progress(
                ProgressEvent(
                    "code",
                    "analysis",
                    "Incremental",
                    24,
                    1000,
                    "files",
                    finished=True,
                )
            )
            now[0] = 25.0
            progress(
                ProgressEvent(
                    "code",
                    "analysis",
                    "Incremental",
                    0,
                    1000,
                    "files",
                )
            )

        lines = output.getvalue().splitlines()
        assert len(lines) == 7  # first, four five-second heartbeats, terminal, next run
        terminal = json.loads(lines[-2].removeprefix("NEOCORTEX_PROGRESS "))
        assert terminal["finished"] is True
        assert terminal["elapsed_seconds"] == 24.0
        assert terminal["rate_per_second"] == 1.0
        assert terminal["eta_seconds"] == 976.0
        restarted = json.loads(lines[-1].removeprefix("NEOCORTEX_PROGRESS "))
        assert restarted["finished"] is False
        assert restarted["elapsed_seconds"] == 0.0

    def test_rich_reporter_renders_normalized_event(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=120)
        with RichProgress(console=console) as progress:
            progress(
                ProgressEvent(
                    "test",
                    "scan",
                    "Inventariando",
                    0,
                    2,
                    "archivos",
                    metrics=(
                        ProgressMetric("cache_hits", 3),
                        ProgressMetric("errors", 1),
                        ProgressMetric("remaining", 2),
                    ),
                )
            )
            progress(
                ProgressEvent(
                    "test",
                    "scan",
                    "Inventario completo",
                    2,
                    2,
                    "archivos",
                    True,
                    (
                        ProgressMetric("cache_hits", 3),
                        ProgressMetric("errors", 1),
                        ProgressMetric("remaining", 0),
                    ),
                )
            )
        rendered = output.getvalue()
        self.assertIn("Inventario completo", rendered)
        self.assertIn("caché 3", rendered)
        self.assertIn("errores 1", rendered)
        self.assertIn("faltan 0", rendered)

    def test_finished_unknown_total_becomes_a_complete_terminal_total(self) -> None:
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, width=120)
        with RichProgress(console=console) as progress:
            progress(
                ProgressEvent(
                    "semantic",
                    "stage:pdf",
                    "Preparando texto PDF",
                    3,
                    None,
                    "documentos",
                )
            )
            progress(
                ProgressEvent(
                    "semantic",
                    "stage:pdf",
                    "Texto PDF preparado",
                    3,
                    None,
                    "documentos",
                    True,
                    (
                        ProgressMetric("chunks", 7),
                        ProgressMetric("embedded", 5),
                    ),
                )
            )

        rendered = output.getvalue()
        self.assertIn("Texto PDF preparado", rendered)
        self.assertIn("3/3", rendered)
        self.assertNotIn("3/?", rendered)
        self.assertIn("fragmentos 7", rendered)
        self.assertIn("vectores 5", rendered)


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        policy_root = tempfile.TemporaryDirectory()
        self.addCleanup(policy_root.cleanup)
        policy = disjoint_internal_paths_policy(Path(policy_root.name))
        self._internal_paths_policy = policy
        for target in (
            "neocortex.integrations.inventory.inventory_boundary.canonical_internal_paths_policy",
            "neocortex.persistence.framework_state_common.canonical_internal_paths_policy",
        ):
            policy_patch = patch(target, return_value=policy)
            policy_patch.start()
            self.addCleanup(policy_patch.stop)

    def test_locked_initial_run_signature_is_frozen(self) -> None:
        self.assertEqual(
            str(inspect.signature(FrameworkOrchestrator._run_initial_locked)),
            "(self, boundary: 'NormalInventoryBoundary') -> 'InitialRunResult'",
        )

    def test_effective_exclusions_include_custom_state_but_not_same_named_dirs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            state = corpus / "custom-state"
            nested_appdata = corpus / "Workspace" / "AppData"
            state.mkdir(parents=True)
            nested_appdata.mkdir(parents=True)
            (state / "framework.sqlite3").write_bytes(b"state")
            visible = nested_appdata / "visible.bin"
            visible.write_bytes(b"visible")
            orchestrator = FrameworkOrchestrator(
                FrameworkConfig(root=corpus, state_directory=state)
            )
            excluded = orchestrator._effective_excluded_paths(corpus)
            with DedupIndex(base / "dedup.sqlite3") as index:
                scan = index.scan(corpus, excluded_paths=excluded)
                paths = {item.path for item in index.snapshots(scan.scan_id)}
            self.assertEqual(paths, {str(visible)})

    def test_state_directory_cannot_equal_inventory_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orchestrator = FrameworkOrchestrator(FrameworkConfig(root=root, state_directory=root))
            with self.assertRaisesRegex(ValueError, "cannot equal or contain"):
                orchestrator._effective_excluded_paths(root)

    def test_registry_accepts_a_future_route_without_orchestrator_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            adapter = RouteAdapter(
                "audio",
                lambda context: {"processed": 0, "run_id": context.run_id},
            )
            result = FrameworkOrchestrator(
                FrameworkConfig(
                    root=corpus,
                    state_directory=base / "state",
                    route="audio",
                ),
                route_registry={"audio": adapter},
            ).run_initial()
            audio_result = result.route_results["audio"]
            self.assertIsInstance(audio_result, dict)
            assert isinstance(audio_result, dict)
            self.assertEqual(audio_result["processed"], 0)

    def test_one_failed_route_does_not_abort_other_route_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            state_directory = base / "state"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)

            def fail(context):
                assert context.progress is not None
                context.progress(
                    ProgressEvent(
                        "bad",
                        "work",
                        "Ruta de prueba",
                        2,
                        10,
                        "elementos",
                    )
                )
                raise RuntimeError("route failure sentinel")

            registry = {
                "good": RouteAdapter("good", lambda context: {"processed": 1}),
                "bad": RouteAdapter("bad", fail),
            }
            progress = RecordingProgress()
            orchestrator = FrameworkOrchestrator(
                FrameworkConfig(
                    root=corpus,
                    state_directory=state_directory,
                    route="good,bad",
                    global_memory_budget_bytes=128 * 1024 * 1024,
                    global_min_free_memory_bytes=0,
                    global_min_free_commit_bytes=0,
                    global_cpu_slots=2,
                ),
                progress=progress,
                route_registry=registry,
            )
            with self.assertRaises(RouteExecutionError):
                orchestrator.run_initial()

            connection = sqlite3.connect(state_directory / "framework.sqlite3")
            try:
                statuses = dict(
                    connection.execute(
                        "SELECT route_name,status FROM route_runs "
                        "WHERE run_id=(SELECT MAX(run_id) FROM initial_runs)"
                    )
                )
                run_status = connection.execute(
                    "SELECT status FROM initial_runs ORDER BY run_id DESC LIMIT 1"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(statuses, {"good": "completed", "bad": "failed"})
            self.assertEqual(run_status, "failed")
            terminal = tuple(
                event
                for event in progress.events
                if event.key == ("bad", "work") and event.finished
            )
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0].completed, 2)
            self.assertIsNone(terminal[0].total)
            self.assertIn("falló", terminal[0].description)

    @pytest.mark.capability("image")
    def test_primary_orchestrator_runs_and_resumes_image_route(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            state = base / "state"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            with Image.new("RGB", (640, 480), "white") as image:
                image.save(corpus / "invoice_page.png")
            with Image.new("RGB", (640, 480), (20, 90, 45)) as image:
                image.save(corpus / "field_photo.png")
            config = FrameworkConfig(
                root=corpus,
                state_directory=state,
                route="image",
                image_workers=1,
                image_memory_budget_bytes=256 * 1024 * 1024,
                image_min_free_memory_bytes=0,
                image_min_free_commit_bytes=0,
                global_memory_budget_bytes=256 * 1024 * 1024,
                global_min_free_memory_bytes=128 * 1024 * 1024,
                global_min_free_commit_bytes=128 * 1024 * 1024,
            )
            first = FrameworkOrchestrator(config).run_initial()
            self.assertIsNotNone(first.image)
            assert first.image is not None
            self.assertEqual(first.image.classified, 2)
            self.assertTrue((state / "image.sqlite3").is_file())
            second = FrameworkOrchestrator(config).run_initial()
            assert second.image is not None
            self.assertEqual(second.image.cache_hits, 2)
            self.assertEqual(second.image.classified, 0)

    @pytest.mark.capability("inference")
    def test_primary_orchestrator_coordinates_all_builtin_routes(self) -> None:
        import fitz
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            state_directory = base / "state"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            with Image.effect_noise((800, 600), 60).convert("RGB") as image:
                image.save(corpus / "transformador_mantenimiento.jpg")

            document_xml = b"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Informe de subestacion</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>"""
            content_types_xml = b"""<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"""
            relationships_xml = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>"""
            with zipfile.ZipFile(corpus / "report.docx", "w") as archive:
                archive.writestr("[Content_Types].xml", content_types_xml)
                archive.writestr("_rels/.rels", relationships_xml)
                archive.writestr("word/document.xml", document_xml)

            with zipfile.ZipFile(corpus / "materials.xlsx", "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr(
                    "xl/workbook.xml",
                    "<workbook><sheet name='Transformadores'/></workbook>",
                )
                archive.writestr(
                    "xl/sharedStrings.xml",
                    "<sst><si><t>Lista de materiales eléctricos</t></si></sst>",
                )

            pdf = fitz.open()
            page = pdf.new_page()
            page.insert_text((72, 72), "Substation report")
            pdf.save(corpus / "report.pdf")
            pdf.close()

            config = FrameworkConfig(
                root=corpus,
                state_directory=state_directory,
                route="all",
                image_workers=2,
                image_document_ocr_mode="never",
                image_min_free_memory_bytes=0,
                image_min_free_commit_bytes=0,
                docx_min_free_memory_bytes=0,
                docx_min_free_commit_bytes=0,
                pdf_ocr_mode="never",
                pdf_workers=1,
                pdf_ocr_workers=1,
                pdf_worker_memory_bytes=256 * 1024 * 1024,
                global_memory_budget_bytes=512 * 1024 * 1024,
                global_min_free_memory_bytes=0,
                global_min_free_commit_bytes=0,
                global_cpu_slots=3,
            )
            with _coordinator_available_memory():
                result = FrameworkOrchestrator(config).run_initial()

            self.assertEqual(
                set(result.route_results),
                {
                    "pdf",
                    "docx",
                    "office",
                    "archive",
                    "text",
                    "audio",
                    "video",
                    "image",
                    "code",
                },
            )
            self.assertIsNotNone(result.pdf)
            self.assertIsNotNone(result.docx)
            self.assertIsNotNone(result.office)
            self.assertIsNotNone(result.audio)
            self.assertIsNotNone(result.video)
            self.assertIsNotNone(result.image)
            self.assertIsNotNone(result.code)
            assert result.pdf is not None
            assert result.docx is not None
            assert result.office is not None
            assert result.audio is not None
            assert result.video is not None
            assert result.image is not None
            assert result.code is not None
            self.assertEqual(result.pdf.errors, 0)
            self.assertEqual(result.docx.errors, 0)
            self.assertEqual(result.office.errors, 0)
            self.assertEqual(result.office.extracted, 1)
            self.assertEqual(result.audio.errors, 0)
            self.assertEqual(result.video.errors, 0)
            self.assertEqual(result.image.errors, 0)
            self.assertEqual(result.code.errors, 0)
            self.assertIsNotNone(result.global_resources)
            assert result.global_resources is not None
            self.assertLessEqual(
                result.global_resources.peak_reserved_bytes,
                result.global_resources.memory_budget_bytes,
            )
            self.assertGreater(result.global_resources.routes["pdf"].admissions, 0)
            self.assertGreater(result.global_resources.routes["docx"].admissions, 0)
            self.assertGreater(result.global_resources.routes["office"].admissions, 0)
            self.assertGreater(result.global_resources.routes["image"].admissions, 0)

            connection = sqlite3.connect(state_directory / "framework.sqlite3")
            try:
                statuses = dict(
                    connection.execute(
                        "SELECT route_name,status FROM route_runs WHERE run_id=?",
                        (result.run_id,),
                    )
                )
            finally:
                connection.close()
            self.assertEqual(
                statuses,
                {
                    "pdf": "completed",
                    "docx": "completed",
                    "office": "completed",
                    "archive": "completed",
                    "text": "completed",
                    "audio": "completed",
                    "video": "completed",
                    "image": "completed",
                    "code": "completed",
                },
            )

    @pytest.mark.capability("inference")
    @unittest.skipUnless(
        shutil.which("ffmpeg") is not None
        and shutil.which("ffprobe") is not None
        and (os.name == "nt" or Path("/usr/bin/prlimit").is_file()),
        "bounded FFmpeg video pilot is unavailable",
    )
    def test_all_abstains_benignly_from_audio_for_visual_only_video(self) -> None:
        with _coordinator_available_memory(), tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            state_directory = base / "state"
            corpus.mkdir()
            source = corpus / "visual-only.mkv"
            created = subprocess.run(
                (
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-nostdin",
                    "-nostats",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=10:duration=1",
                    "-an",
                    "-c:v",
                    "ffv1",
                    "-y",
                    str(source),
                ),
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(
                created.returncode,
                0,
                created.stderr.decode("utf-8", "replace"),
            )
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            result = FrameworkOrchestrator(
                FrameworkConfig(
                    root=corpus,
                    state_directory=state_directory,
                    route="all",
                    video_max_frames=2,
                    video_include_scenes=False,
                    video_include_keyframes=False,
                    video_ocr_mode="never",
                    pdf_ocr_mode="never",
                    image_document_ocr_mode="never",
                    global_memory_budget_bytes=3 * 1024 * 1024 * 1024,
                    global_min_free_memory_bytes=0,
                    global_min_free_commit_bytes=0,
                    global_cpu_slots=2,
                    audio_min_free_memory_bytes=0,
                    audio_min_free_commit_bytes=0,
                )
            ).run_initial()

            assert result.audio is not None
            assert result.video is not None
            self.assertEqual(result.audio.no_audio, 1)
            self.assertEqual(result.audio.transcribed, 0)
            self.assertEqual(result.audio.errors, 0)
            self.assertEqual(result.audio.review_candidates, 0)
            self.assertEqual(result.video.visual_only, 1)
            self.assertEqual(result.video.complete, 1)
            self.assertEqual(result.video.errors, 0)
            self.assertFalse(_has_strict_route_errors(result))
            with closing(sqlite3.connect(state_directory / "audio.sqlite3")) as connection:
                self.assertEqual(
                    connection.execute("SELECT status FROM documents").fetchone()[0],
                    "no_audio",
                )
            with closing(sqlite3.connect(state_directory / "video.sqlite3")) as connection:
                self.assertEqual(
                    tuple(
                        connection.execute("SELECT status,audio_status FROM documents").fetchone()
                    ),
                    ("complete", None),
                )

    def test_marks_interrupted_pre_frontier_action_failed_without_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "corpus"
            state_directory = base / "state"
            root.mkdir()
            state_directory.mkdir()
            database = state_directory / "framework.sqlite3"
            with FrameworkState(database) as state:
                run_id = begin_signed_normal_run(
                    state,
                    root,
                    internal_paths_policy=self._internal_paths_policy,
                )
                action_id = state.begin_file_action(
                    run_id,
                    "trash_duplicate",
                    str(root / "source"),
                    None,
                    None,
                    None,
                    True,
                )
                self.assertEqual(state.mark_abandoned_actions(), 1)
            connection = sqlite3.connect(database)
            status, detail = connection.execute(
                "SELECT status,detail FROM file_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            connection.close()
            self.assertEqual(status, "failed")
            self.assertIn("before the mutation frontier", detail)

    def test_framework_state_migrates_schema_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "framework.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
                INSERT INTO metadata VALUES('schema_version', '1');
                CREATE TABLE initial_runs(
                    run_id INTEGER PRIMARY KEY, root TEXT NOT NULL,
                    started_ns INTEGER NOT NULL, completed_ns INTEGER,
                    status TEXT NOT NULL, scan_id INTEGER, journal_volume TEXT,
                    journal_id TEXT, start_usn INTEGER, end_usn INTEGER
                );
                """
            )
            connection.commit()
            connection.close()
            with FrameworkState(database):
                pass
            connection = sqlite3.connect(database)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(initial_runs)")}
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(version, str(SCHEMA_VERSION))
            self.assertIn("reconciliation_records", columns)
            self.assertIn("inventory_attempts", columns)
            self.assertIn("inventory_mode", columns)
            connection = sqlite3.connect(database)
            action_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(run_actions)")
            }
            connection.close()
            self.assertIn("empty_directory_candidates", action_columns)
            self.assertIn("empty_directories_trashed", action_columns)
            self.assertIn("type_cache_hits", action_columns)
            self.assertIn("type_cache_misses", action_columns)
            self.assertIn("type_cache_pruned", action_columns)
            self.assertIn("stale_inventory", action_columns)
            connection = sqlite3.connect(database)
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            connection.close()
            self.assertIn("run_events", tables)
            self.assertIn("content_type_cache", tables)
            self.assertIn("route_runs", tables)

    def test_cancelled_run_is_distinct_from_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "framework.sqlite3"
            with FrameworkState(database) as state:
                run_id = state.begin_initial_run(Path(directory), JournalCursor("C:", 1, 0))
                state.cancel_initial_run(run_id)
                status, completed_ns = state._connection.execute(
                    "SELECT status,completed_ns FROM initial_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            self.assertEqual(status, "cancelled")
            self.assertIsNotNone(completed_ns)

    def test_concurrent_route_state_records_and_completes_action_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "corpus"
            state_directory = base / "state"
            root.mkdir()
            state_directory.mkdir()
            database = state_directory / "framework.sqlite3"
            with FrameworkState(database) as state:
                run_id = begin_signed_normal_run(
                    state,
                    root,
                    internal_paths_policy=self._internal_paths_policy,
                )
            route_state = FrameworkRouteState(database)

            action_ids = route_state.begin_file_actions(
                run_id,
                (
                    (
                        "trash_pdf_text_duplicate",
                        str(root / "duplicate.pdf"),
                        None,
                        "application/pdf",
                        "test",
                        False,
                    ),
                ),
            )
            route_state.finish_file_actions(action_ids, "planned")

            connection = sqlite3.connect(database)
            try:
                row = connection.execute(
                    "SELECT action_type,status FROM file_actions WHERE action_id=?",
                    (action_ids[0],),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("trash_pdf_text_duplicate", "planned"))

    def test_reconciles_file_create_modify_delete_and_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            existing = corpus / "existing.bin"
            removed = corpus / "removed.bin"
            old_name = corpus / "old_name.bin"
            existing.write_bytes(b"old")
            removed.write_bytes(b"remove-me")
            old_name.write_bytes(b"rename-me")
            database = base / "inventory.sqlite3"

            with DedupIndex(database) as index:
                scan = index.scan(corpus)
                start = journal.capture(corpus.drive)
                existing.write_bytes(b"modified-and-longer")
                removed.unlink()
                old_name.rename(corpus / "new_name.bin")
                (corpus / "created.bin").write_bytes(b"created")
                target = journal.capture(corpus.drive)

                result = reconcile_usn_window(index, scan.scan_id, corpus, start, target)
                snapshots = list(index.snapshots(scan.scan_id))

            by_name = {Path(item.path).name: item for item in snapshots}
            self.assertIn("existing.bin", by_name)
            self.assertEqual(by_name["existing.bin"].size, len(b"modified-and-longer"))
            self.assertIn("created.bin", by_name)
            self.assertIn("new_name.bin", by_name)
            self.assertNotIn("old_name.bin", by_name)
            self.assertNotIn("removed.bin", by_name)
            self.assertFalse(result.requires_rescan)
            self.assertGreater(result.records_seen, 0)

    def test_initial_run_coordinates_modules_and_emits_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            state = base / "state"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            (corpus / "older.bin").write_bytes(b"duplicate")
            (corpus / "newer.bin").write_bytes(b"duplicate")
            (corpus / "unique.bin").write_bytes(b"unique")
            progress = RecordingProgress()
            result = FrameworkOrchestrator(
                FrameworkConfig(root=corpus, state_directory=state),
                progress=progress,
            ).run_initial()

            self.assertEqual(result.scan.files_seen, 3)
            self.assertEqual(result.dedup_plan.group_count, 1)
            self.assertEqual(len(result.dedup_plan.groups), 0)
            self.assertFalse(result.actions.apply_actions)
            self.assertEqual(result.actions.duplicate_candidates, 1)
            self.assertEqual(result.dedup_plan.statistics.exact_compare_files, 0)
            journal_usn_span = result.journal_usn_span
            if os.name == "nt":
                self.assertIsNotNone(journal_usn_span)
                assert journal_usn_span is not None
                self.assertGreaterEqual(journal_usn_span, 0)
            else:
                self.assertIsNone(journal_usn_span)
            self.assertTrue((state / "framework.sqlite3").is_file())
            self.assertTrue((state / "dedup.sqlite3").is_file())
            event_sequence = [event.key for event in progress.events]
            event_keys = set(event_sequence)
            self.assertIn(("framework", "prepare"), event_keys)
            self.assertIn(("dedup", "inventory"), event_keys)
            self.assertIn(("dedup", "verify"), event_keys)
            self.assertIn(("framework", "complete"), event_keys)
            expected_phase_order = (
                ("framework", "prepare"),
                ("dedup", "inventory"),
                ("dedup", "verify"),
                ("framework", "complete"),
            )
            phase_positions = [event_sequence.index(key) for key in expected_phase_order]
            self.assertEqual(phase_positions, sorted(phase_positions))

            second = FrameworkOrchestrator(
                FrameworkConfig(root=corpus, state_directory=state)
            ).run_initial()
            expected_replay_mode = "incremental" if os.name == "nt" else "full"
            self.assertEqual(second.inventory_mode, expected_replay_mode)
            self.assertEqual(second.inventory_attempts, 0 if os.name == "nt" else 1)
            (corpus / "created-after-checkpoint.bin").write_bytes(b"new")
            third = FrameworkOrchestrator(
                FrameworkConfig(root=corpus, state_directory=state)
            ).run_initial()
            self.assertEqual(third.inventory_mode, expected_replay_mode)
            self.assertEqual(third.scan.files_seen, 4)
            connection = sqlite3.connect(state / "framework.sqlite3")
            modes = [
                row[0]
                for row in connection.execute(
                    "SELECT inventory_mode FROM initial_runs ORDER BY run_id"
                )
            ]
            event_count = connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
            connection.close()
            self.assertEqual(modes, ["full", expected_replay_mode, expected_replay_mode])
            self.assertGreaterEqual(event_count, 9)

    def test_real_empty_pdf_route_has_an_empty_scoped_organization_plan(self) -> None:
        for filename in (None, "unclassified.bin"):
            with (self.subTest(filename=filename), _coordinator_effect_capabilities(),
                  tempfile.TemporaryDirectory() as directory):
                base = Path(directory)
                corpus = base / "corpus"
                corpus.mkdir()
                if filename is not None:
                    (corpus / filename).write_bytes(b"\x00unclassified fixture\xff")
                state_directory = base / "state"
                journal = SyntheticUsnJournal(corpus).start()
                self.addCleanup(journal.close)
                result = FrameworkOrchestrator(
                    FrameworkConfig(
                        root=corpus,
                        state_directory=state_directory,
                        route="pdf",
                        apply_actions=True,
                        pdf_ocr_mode="never",
                        pdf_workers=1,
                        pdf_ocr_workers=1,
                        global_min_free_memory_bytes=0,
                        global_min_free_commit_bytes=0,
                    )
                ).run_initial()
                self.assertIsNotNone(result.pdf)
                self.assertIsNotNone(result.organization_plan)
                self.assertIsNotNone(result.organization_apply)
                assert result.organization_plan is not None
                assert result.organization_apply is not None
                self.assertEqual(result.organization_plan.considered, 0)
                self.assertEqual(result.organization_plan.source_root, str(corpus))
                self.assertEqual(result.organization_apply.applied, 0)
                self.assertFalse((corpus / "Consulta_Tecnica_Organizada").exists())
                self.assertTrue((state_directory / "document_catalog.sqlite3").is_file())
                self.assertEqual(len(tuple(corpus.iterdir())), int(filename is not None))

    def test_all_apply_runs_organization_after_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            state = base / "state"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            (corpus / "manual.txt").write_text("manual técnico", encoding="utf-8")
            order: list[str] = []

            def route(_context):
                order.append("route")
                # Real content routes publish even an empty catalog.  This
                # ordering fixture must supply that owner, not mock its scope.
                initialize_document_catalog(state / "document_catalog.sqlite3")
                return {"processed": 1}

            def plan(
                _catalog,
                destination,
                *,
                source_scope,
                min_confidence,
                progress,
                mutation_guard,
            ):
                order.append("plan")
                self.assertEqual(source_scope.root, corpus)
                self.assertEqual(source_scope.publication_heads, ())
                source_scope.verify()
                self.assertIsNotNone(progress)
                self.assertIsNone(mutation_guard.reason_code)
                self.assertIsNotNone(mutation_guard.protected_content_policy)
                self.assertEqual(
                    destination,
                    corpus / "Consulta_Tecnica_Organizada",
                )
                self.assertEqual(min_confidence, 0.8)
                # The ordering spy still executes the owner so Framework can
                # reconcile a real completed receipt and scoped plan checkpoint.
                return plan_document_organization_owner(
                    _catalog,
                    destination,
                    source_scope=source_scope,
                    min_confidence=min_confidence,
                    progress=progress,
                    mutation_guard=mutation_guard,
                )

            def apply_all(_catalog, destination, *, progress, mutation_guard):
                order.append("apply")
                self.assertIsNotNone(progress)
                self.assertIsNone(mutation_guard.reason_code)
                self.assertEqual(mutation_guard.policy.root, corpus)
                self.assertEqual(
                    destination,
                    corpus / "Consulta_Tecnica_Organizada",
                )
                return OrganizationApplySummary(
                    2,
                    selected=2,
                    applied=2,
                    cache_synced=2,
                    remaining=0,
                )

            orchestrator = FrameworkOrchestrator(
                FrameworkConfig(
                    root=corpus,
                    state_directory=state,
                    route="all",
                    apply_actions=True,
                    organization_min_confidence=0.8,
                ),
                route_registry={"pdf": RouteAdapter("pdf", route)},
            )
            with (
                _coordinator_effect_capabilities(),
                patch(
                    "neocortex.documents.document_organization.plan_document_organization",
                    side_effect=plan,
                ),
                patch(
                    "neocortex.documents.document_organization.apply_all_document_organization",
                    side_effect=apply_all,
                ),
            ):
                result = orchestrator.run_initial()

            self.assertEqual(order, ["route", "plan", "apply"])
            self.assertIsNotNone(result.organization_plan)
            self.assertIsNotNone(result.organization_apply)
            assert result.organization_apply is not None
            self.assertEqual(result.organization_apply.applied, 2)

    def test_routes_without_apply_never_start_organization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            orchestrator = FrameworkOrchestrator(
                FrameworkConfig(
                    root=corpus,
                    state_directory=base / "state",
                    route="all",
                ),
                route_registry={"pdf": RouteAdapter("pdf", lambda _context: {})},
            )
            with patch(
                "neocortex.documents.document_organization.plan_document_organization"
            ) as plan:
                result = orchestrator.run_initial()
            plan.assert_not_called()
            self.assertIsNone(result.organization_plan)
            self.assertIsNone(result.organization_apply)


if __name__ == "__main__":
    unittest.main()
# endregion [02]
