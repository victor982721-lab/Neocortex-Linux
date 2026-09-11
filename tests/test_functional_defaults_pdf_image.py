"""Focused PDF/image cache replay and opt-in recovery coverage."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import fitz  # type: ignore[import-untyped]
from PIL import Image

from neocortex.capabilities.formats.image.contracts import ImageRouteConfig
from neocortex.capabilities.formats.image.route import ImageRoute
from neocortex.capabilities.formats.image.state import (
    image_database,
    iter_ocr_text_records,
    prepare_ocr_text_storage,
)
from neocortex.capabilities.formats.pdf.pdf_derived import search_pdf_state
from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute
from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteConfig
from neocortex.capabilities.formats.pdf.pdf_state import pdf_database
from neocortex.deduplication import DedupIndex, snapshot_path


TEST_CAPABILITIES = ("documents", "image")


class _PdfState:
    def __init__(self, snapshots: list) -> None:
        self.snapshots = list(snapshots)
        self.phases: set[str] = set()
        self.completed_phases: list[str] = []
        self.review_candidates: list = []

    def iter_route_candidates(self, run_id, mime):
        del run_id
        if mime == "application/pdf":
            yield from (
                snapshot
                for snapshot in self.snapshots
                if snapshot.path.casefold().endswith(".pdf")
            )

    def begin_file_actions(self, run_id, actions):
        del run_id, actions
        return []

    def finish_file_actions(self, action_ids, status, detail=None):
        del action_ids, status, detail

    def store_review_candidates(self, run_id, candidates):
        del run_id
        self.review_candidates.extend(candidates)

    def reconcile_review_candidates_batch(self, run_id, route_name, reconciliations):
        del run_id, route_name, reconciliations
        return 0

    def completed_route_phases(self, run_id, route_name):
        del run_id, route_name
        return frozenset(self.phases)

    def begin_route_phase(self, run_id, route_name, phase_name, **kwargs):
        del run_id, route_name, phase_name, kwargs

    def complete_route_phase(self, run_id, route_name, phase_name, summary):
        del run_id, route_name, summary
        self.completed_phases.append(phase_name)


class _ImageState:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.review_candidates: list = []

    def iter_route_candidates_by_prefix(self, run_id, mime_prefix):
        del run_id
        if mime_prefix == "image/":
            yield "image/png", self.snapshot

    def iter_selected_route_candidates_by_prefix(
        self, run_id, mime_prefix, route_name, selection
    ):
        del route_name, selection
        yield from self.iter_route_candidates_by_prefix(run_id, mime_prefix)

    def store_review_candidates(self, run_id, candidates):
        del run_id
        self.review_candidates.extend(candidates)

    def reconcile_review_candidates_batch(self, run_id, route_name, reconciliations):
        del run_id, route_name, reconciliations
        return 0


def _write_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def _pdf_config(path: Path, **overrides) -> PdfRouteConfig:
    values = {
        "ocr_mode": "never",
        "workers": 1,
        "ocr_workers": 1,
        "min_free_bytes": 0,
    }
    values.update(overrides)
    return PdfRouteConfig(path, **values)


def _image_config(path: Path, root: Path, **overrides) -> ImageRouteConfig:
    values = {
        "workers": 1,
        "memory_budget_bytes": 256 * 1024 * 1024,
        "min_free_memory_bytes": 0,
        "min_free_commit_bytes": 0,
        "document_ocr_mode": "never",
        "isolate_decoders": False,
    }
    values.update(overrides)
    return ImageRouteConfig(path, root, **values)


def test_pdf_resume_repairs_missing_derivatives_without_new_extraction_or_ocr(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Informe.pdf"
    _write_pdf(source, "Control protección interruptor")
    pdf_state = tmp_path / "pdf.sqlite3"

    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(tmp_path, excluded_paths=())
        snapshots = list(index.snapshots(scan.scan_id))
        state = _PdfState(snapshots)
        first = PdfRoute(
            _pdf_config(pdf_state), index, state, 1, scan.scan_id
        ).run()
        assert (first.new_documents, first.fts_pages_indexed, first.profiles_built) == (1, 1, 1)

        with pdf_database(pdf_state) as connection:
            connection.execute("DELETE FROM page_fts")
            connection.execute("DELETE FROM page_layouts")
            connection.execute("DELETE FROM document_layouts")
            connection.execute("UPDATE pages SET profile_json=NULL")
            connection.execute(
                "UPDATE documents SET profile_version=NULL,template_simhash64=NULL"
            )

        state.phases = {"extraction", "text_dedup", "derived"}
        resumed_config = _pdf_config(pdf_state, resume_source_run_id=1)
        with (
            patch.object(PdfRoute, "_process_document", side_effect=AssertionError("new extraction")),
            patch.object(PdfRoute, "_ocr_page", side_effect=AssertionError("new OCR")),
        ):
            repaired = PdfRoute(
                resumed_config, index, state, 2, scan.scan_id
            ).run()

        assert repaired.extraction_phase_skipped is True
        assert repaired.text_dedup_phase_skipped is True
        assert repaired.derived_phase_skipped is False
        assert repaired.derived_cache_repair_required is True
        assert repaired.new_documents == 0
        assert repaired.fts_pages_indexed == 1
        assert repaired.profiles_built == 1
        assert search_pdf_state(pdf_state, "Control")

        with patch(
            "neocortex.capabilities.formats.pdf.pdf_route.PdfDerivedIndexer.run",
            side_effect=AssertionError("complete derived cache was rerun"),
        ):
            intact = PdfRoute(
                resumed_config, index, state, 3, scan.scan_id
            ).run()
        assert intact.derived_phase_skipped is True
        assert intact.derived_cache_repair_required is False
        assert intact.new_documents == 0
        assert intact.fts_pages_indexed == 0
        assert intact.profiles_built == 0
        assert search_pdf_state(pdf_state, "Control")


def test_pdf_resume_repairs_missing_document_owner_with_bounded_extraction(
    tmp_path: Path,
) -> None:
    source = tmp_path / "two-pages.pdf"
    document = fitz.open()
    for text in ("page one token", "page two missing token"):
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(source)
    document.close()
    pdf_state = tmp_path / "pdf.sqlite3"

    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(tmp_path, excluded_paths=())
        snapshots = list(index.snapshots(scan.scan_id))
        state = _PdfState(snapshots)
        config = _pdf_config(pdf_state)
        PdfRoute(config, index, state, 1, scan.scan_id).run()
        state.phases = {"extraction", "text_dedup", "derived"}
        with pdf_database(pdf_state) as connection:
            key = str(
                connection.execute(
                    "SELECT file_key FROM documents WHERE path=?", (str(source),)
                ).fetchone()[0]
            )
            connection.execute("DELETE FROM documents WHERE file_key=?", (key,))

        calls: list[str] = []
        original_process_document = PdfRoute._process_document

        def wrapped_process_document(route, snapshot, *args, **kwargs):
            calls.append(snapshot.path)
            return original_process_document(route, snapshot, *args, **kwargs)

        with patch.object(PdfRoute, "_process_document", new=wrapped_process_document):
            repaired = PdfRoute(
                _pdf_config(pdf_state, resume_source_run_id=1),
                index,
                state,
                2,
                scan.scan_id,
            ).run()

    assert calls == [str(source)]
    assert repaired.extraction_phase_skipped is False
    assert repaired.extraction_cache_repair_required is True
    assert repaired.extraction_missing_documents == 1
    assert repaired.extraction_missing_pages == 0
    assert (repaired.new_documents, repaired.extracted, repaired.cache_hits) == (1, 1, 0)
    assert repaired.derived_cache_repair_required is True
    assert repaired.fts_pages_indexed == 2
    assert search_pdf_state(pdf_state, "missing")
    assert search_pdf_state(pdf_state, "one")


def test_pdf_resume_repairs_missing_page_from_inventory_coverage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "one-page-missing.pdf"
    document = fitz.open()
    for text in ("page one token", "page two missing token"):
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(source)
    document.close()
    pdf_state = tmp_path / "pdf.sqlite3"

    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(tmp_path, excluded_paths=())
        snapshots = list(index.snapshots(scan.scan_id))
        state = _PdfState(snapshots)
        config = _pdf_config(pdf_state)
        PdfRoute(config, index, state, 1, scan.scan_id).run()
        state.phases = {"extraction", "text_dedup", "derived"}
        with pdf_database(pdf_state) as connection:
            key = str(
                connection.execute(
                    "SELECT file_key FROM documents WHERE path=?", (str(source),)
                ).fetchone()[0]
            )
            connection.execute(
                "DELETE FROM pages WHERE file_key=? AND page_number=1", (key,)
            )

        calls: list[str] = []
        original_process_document = PdfRoute._process_document

        def wrapped_process_document(route, snapshot, *args, **kwargs):
            calls.append(snapshot.path)
            return original_process_document(route, snapshot, *args, **kwargs)

        with patch.object(PdfRoute, "_process_document", new=wrapped_process_document):
            repaired = PdfRoute(
                _pdf_config(pdf_state, resume_source_run_id=1),
                index,
                state,
                2,
                scan.scan_id,
            ).run()

    assert calls == [str(source)]
    assert repaired.extraction_phase_skipped is False
    assert repaired.extraction_cache_repair_required is True
    assert repaired.extraction_missing_documents == 0
    assert repaired.extraction_missing_pages == 1
    assert repaired.new_documents == 0
    assert repaired.cache_refreshes == 1
    assert (repaired.extracted, repaired.cache_hits) == (1, 0)
    assert repaired.fts_pages_indexed == 2
    assert search_pdf_state(pdf_state, "missing")
    assert search_pdf_state(pdf_state, "one")

def test_pdf_recoverable_retry_is_opt_in_and_claimed_once_per_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Temporal.pdf"
    _write_pdf(source, "Control protección interruptor")
    pdf_state = tmp_path / "pdf.sqlite3"

    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(tmp_path, excluded_paths=())
        snapshots = list(index.snapshots(scan.scan_id))
        state = _PdfState(snapshots)
        initial_config = _pdf_config(pdf_state)
        PdfRoute(initial_config, index, state, 1, scan.scan_id).run()
        with pdf_database(pdf_state) as connection:
            connection.execute(
                """UPDATE documents SET status='error',error_type='PdfResourceError',
                error_message='message says retry but type is explicit',
                transient_retry_count=1,next_retry_ns=NULL"""
            )

        with patch.object(PdfRoute, "_process_document", side_effect=AssertionError("default retry")):
            retained = PdfRoute(
                initial_config, index, state, 2, scan.scan_id
            ).run()
        assert (retained.cache_hits, retained.cached_errors, retained.new_documents) == (1, 1, 0)

        retry_config = _pdf_config(pdf_state, retry_recoverable_errors=True)
        extraction_calls: list[str] = []
        original_process_document = PdfRoute._process_document

        def wrapped_process_document(route, snapshot, *args, **kwargs):
            extraction_calls.append(snapshot.path)
            return original_process_document(route, snapshot, *args, **kwargs)

        with patch.object(
            PdfRoute,
            "_process_document",
            new=wrapped_process_document,
        ):
            recovered = PdfRoute(
                retry_config, index, state, 3, scan.scan_id
            ).run()
        assert extraction_calls == [source.as_posix()]
        assert (recovered.retried_documents, recovered.extracted, recovered.errors) == (1, 1, 0)

        with patch.object(PdfRoute, "_process_document", side_effect=AssertionError("success retry")):
            intact = PdfRoute(
                retry_config, index, state, 4, scan.scan_id
            ).run()
        assert (intact.cache_hits, intact.new_documents, intact.extracted) == (1, 0, 0)

        route = object.__new__(PdfRoute)
        route.config = _pdf_config(pdf_state, retry_recoverable_errors=True)
        row = {
            "status": "error",
            "transient_retry_count": 1,
            "next_retry_ns": None,
            "has_retryable_page_error": 0,
            "error_type": "PdfResourceError",
            "error_message": "message says retry but type is explicit",
            "metadata_json": None,
            "latest_page_error_type": None,
            "latest_page_error_message": None,
        }
        snapshot = snapshots[0]
        route._read_cache_row = lambda _snapshot, _connection: row
        route._cached_snapshot_matches = lambda _row, _snapshot: True
        route._cached_fingerprint_matches = lambda _row, _snapshot: True
        route._cached_layers_are_consistent = lambda _row: True
        route._touch_cache_snapshot = lambda _snapshot, _connection: None
        decision = route._is_cache_hit(snapshot, connection=object(), touch=False)
        assert decision.hit is False
        assert route._is_cache_hit(snapshot, connection=object(), touch=False).hit is True

        for status, error_type, error_message in (
            ("protected", "EncryptedPdf", "password required"),
            ("error", "ValueError", "retry because this message says retry"),
            ("error", "PdfChildReportedError", "legacy worker failure"),
        ):
            row.update(
                status=status,
                error_type=error_type,
                error_message=error_message,
                transient_retry_count=0,
            )
            assert route._is_cache_hit(snapshot, connection=object(), touch=False).hit is True


def test_image_cache_validates_searchable_ocr_without_redecoding_success(
    tmp_path: Path,
) -> None:
    source = tmp_path / "factura.png"
    with Image.new("RGB", (640, 480), "white") as image:
        image.save(source)
    state = _ImageState(snapshot_path(source))
    config = _image_config(tmp_path / "image.sqlite3", tmp_path)
    first = ImageRoute(config, state, 1).run()
    assert first.classified == 1

    stored = prepare_ocr_text_storage(
        "transformador protección", truncated=False
    )
    with image_database(config.state_path) as connection:
        evidence = json.loads(
            connection.execute(
                "SELECT evidence_json FROM images WHERE status='done'"
            ).fetchone()[0]
        )
        evidence["document_text"] = {
            "available": True,
            "recognized_text_chars": stored.characters,
            "recognized_text_xxh3_128": stored.xxh3_128,
        }
        connection.execute(
            """UPDATE images SET ocr_text_zlib=?,ocr_text_chars=?,
            ocr_text_xxh3_128=?,ocr_text_truncated=0,evidence_json=?
            WHERE status='done'""",
            (
                stored.compressed,
                stored.characters,
                stored.xxh3_128,
                json.dumps(evidence),
            ),
        )

    with patch(
        "neocortex.capabilities.formats.image.route.classify",
        side_effect=AssertionError("valid image cache was decoded"),
    ):
        replay = ImageRoute(config, state, 2).run()
    assert (replay.cache_hits, replay.classified, replay.new_images) == (1, 0, 0)
    records = list(iter_ocr_text_records(config.state_path))
    assert len(records) == 1
    assert records[0].text == "transformador protección"


def test_image_recoverable_retry_is_typed_opt_in_and_success_stays_cached(
    tmp_path: Path,
) -> None:
    source = tmp_path / "temporal.png"
    with Image.new("RGB", (640, 480), "navy") as image:
        image.save(source)
    state = _ImageState(snapshot_path(source))
    database = tmp_path / "image.sqlite3"
    config = _image_config(database, tmp_path)

    with patch(
        "neocortex.capabilities.formats.image.route.classify",
        side_effect=PermissionError("temporary input access"),
    ):
        failed = ImageRoute(config, state, 1).run()
    assert (failed.errors, failed.retryable_errors) == (1, 1)

    with patch(
        "neocortex.capabilities.formats.image.route.classify",
        side_effect=AssertionError("default retry"),
    ):
        retained = ImageRoute(config, state, 2).run()
    assert (retained.cache_hits, retained.cached_errors, retained.classified) == (0, 1, 0)

    retry_config = _image_config(
        database, tmp_path, retry_recoverable_errors=True
    )
    from neocortex.capabilities.formats.image.analysis import classify as real_classify

    with patch(
        "neocortex.capabilities.formats.image.route.classify",
        wraps=real_classify,
    ) as extractor:
        recovered = ImageRoute(retry_config, state, 3).run()
    assert extractor.call_count == 1
    assert (recovered.retried_images, recovered.classified, recovered.errors) == (1, 1, 0)

    with patch(
        "neocortex.capabilities.formats.image.route.classify",
        side_effect=AssertionError("successful image cache was redecoded"),
    ):
        intact = ImageRoute(retry_config, state, 4).run()
    assert (intact.cache_hits, intact.new_images, intact.classified) == (1, 0, 0)


def test_image_manual_review_text_does_not_trigger_recoverable_retry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "manual.png"
    with Image.new("RGB", (640, 480), "navy") as image:
        image.save(source)
    state = _ImageState(snapshot_path(source))
    database = tmp_path / "image.sqlite3"
    config = _image_config(database, tmp_path)
    with patch(
        "neocortex.capabilities.formats.image.route.classify",
        side_effect=PermissionError("temporary input access"),
    ):
        ImageRoute(config, state, 1).run()
    with image_database(database) as connection:
        connection.execute(
            "UPDATE images SET error_retryable=0,error_disposition='manual_review',"
            "error_message='retry because this message says retry'"
        )

    retry_config = _image_config(
        database, tmp_path, retry_recoverable_errors=True
    )
    with patch(
        "neocortex.capabilities.formats.image.route.classify",
        side_effect=AssertionError("manual review text triggered retry"),
    ) as extractor:
        retained = ImageRoute(retry_config, state, 2).run()
    extractor.assert_not_called()
    assert (retained.cache_hits, retained.cached_errors, retained.classified) == (0, 1, 0)
