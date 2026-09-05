from __future__ import annotations


import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from neocortex.deduplication import DedupIndex, FileSnapshot
from neocortex.capabilities.formats.image.models import Features
from neocortex.capabilities.formats.image.visual import FeatureVisualClassifier
from neocortex.capabilities.formats.pdf import pdf_schema
from neocortex.capabilities.formats.pdf.pdf_derived import PdfDerivedSummary
from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute
from neocortex.capabilities.formats.pdf.pdf_route_models import (
    PdfRouteConfig,
    effective_document_timeout_seconds,
)
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.safety.route_filters import CandidateSelection


TEST_CAPABILITIES = ('documents', 'image')


# region [01] Adaptive PDF timeout and cache metrics


def test_adaptive_timeout_scales_and_remains_bounded(tmp_path) -> None:
    config = PdfRouteConfig(
        tmp_path / "pdf.sqlite3",
        ocr_mode="auto",
        document_timeout_seconds=90,
        timeout_mode="adaptive",
        max_document_timeout_seconds=600,
    )
    small = effective_document_timeout_seconds(
        config,
        file_size=1_000_000,
        page_count=1,
    )
    large = effective_document_timeout_seconds(
        config,
        file_size=250_000_000,
        page_count=500,
        pending_pages=400,
    )
    assert small is not None and large is not None
    assert 90 <= small < large <= 600
    assert (
        effective_document_timeout_seconds(
            replace(config, timeout_mode="fixed"),
            file_size=250_000_000,
            page_count=500,
        )
        == 90
    )


def test_pdf_schema_migrates_only_known_legacy_ocr_control_error(
    tmp_path,
) -> None:
    database = tmp_path / "pdf.sqlite3"
    with sqlite3.connect(database) as connection:
        pdf_schema._build_pdf_v12_canonical_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','9')")
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            page_errors_count,updated_ns)
            VALUES('1:1','one.pdf',1,1,1,'sig','partial',2,1)"""
        )
        connection.executemany(
            """INSERT INTO page_errors(
            file_key,processing_signature,page_number,error_type,error_message,
            updated_ns) VALUES('1:1','sig',?,?,?,1)""",
            (
                (
                    0,
                    "AttributeError",
                    "'BoundedSemaphore' object has no attribute 'get'",
                ),
                (1, "ValueError", "unrelated"),
            ),
        )
        connection.commit()
    initialize_pdf_state(database)
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT page_number,error_type FROM page_errors ORDER BY page_number"
        ).fetchall()
        migrated = connection.execute(
            """SELECT value FROM metadata
            WHERE key='legacy_ocr_control_rows_migrated'"""
        ).fetchone()[0]
    assert rows == [(0, "LegacyOcrControlError"), (1, "ValueError")]
    assert migrated == "1"


def test_changed_cached_snapshot_retains_prior_status_for_metrics() -> None:
    route = object.__new__(PdfRoute)
    row = {
        "status": "error",
        "persisted_page_error_count": 0,
        "size": 1,
        "mtime_ns": 1,
        "birthtime_ns": 1,
        "processing_signature": "sig",
    }
    route.config = PdfRouteConfig(Path("unused.sqlite3"))
    route._read_cache_row = lambda snapshot, connection: row
    route._cached_snapshot_matches = lambda cached, snapshot: False
    route._cached_legacy_snapshot_matches = lambda cached, snapshot: False
    snapshot = FileSnapshot("changed.pdf", 1, 2, 1, 1, 1)
    decision = route._is_cache_hit(snapshot, connection=object(), touch=False)
    assert decision.hit is False
    assert decision.prior_status == "error"


def test_pdf_resume_skips_completed_extraction_and_text_dedup(tmp_path) -> None:
    database = tmp_path / "pdf.sqlite3"
    selection = CandidateSelection(statuses=("done",))
    config = PdfRouteConfig(
        database,
        ocr_mode="never",
        workers=1,
        ocr_workers=1,
        document_timeout_seconds=30,
        selection=selection,
        resume_source_run_id=7,
        min_free_bytes=0,
    )
    snapshot = FileSnapshot(str(tmp_path / "one.pdf"), 1, 2, 10, 20, 30)
    key = f"{snapshot.volume_id:032x}:{snapshot.file_id:032x}"
    initialize_pdf_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            completed_pages,page_errors_count,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?, 'done',0,0,7,1)""",
            (
                key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                config.processing_signature,
            ),
        )
        connection.commit()

    class State:
        def __init__(self):
            self.completed: list[str] = []

        def iter_route_candidates(self, run_id, mime):
            return iter((snapshot,))

        def iter_selected_route_candidates(
            self,
            run_id,
            mime,
            route_name,
            selection,
        ):
            return iter((snapshot,))

        def begin_file_actions(self, run_id, actions):
            return []

        def finish_file_actions(self, action_ids, status, detail=None):
            return None

        def completed_route_phases(self, run_id, route_name):
            return frozenset(("extraction", "text_dedup"))

        def begin_route_phase(self, run_id, route_name, phase, **kwargs):
            pass

        def complete_route_phase(self, run_id, route_name, phase, summary):
            self.completed.append(phase)

        def record_event(self, *args, **kwargs):
            pass

    state = State()
    route = PdfRoute(config, object.__new__(DedupIndex), state, 8, 1)
    with (
        patch.object(route, "_process_document") as process,
        patch.object(route, "_deduplicate_text") as deduplicate,
        patch(
            "neocortex.capabilities.formats.pdf.pdf_route.PdfDerivedIndexer.run",
            return_value=PdfDerivedSummary(),
        ),
    ):
        summary = route.run()
    process.assert_not_called()
    deduplicate.assert_not_called()
    assert summary.extraction_phase_skipped is True
    assert summary.text_dedup_phase_skipped is True
    assert state.completed == ["derived"]


# endregion [01]


# region [02] Visual multi-label evidence


def test_visual_classifier_emits_conservative_multilabel_provenance() -> None:
    features = Features(
        width=1600,
        height=1200,
        file_size=1,
        format="JPEG",
        frames=1,
        has_transparency=False,
        alpha_fraction=0.0,
        has_camera_exif=False,
        white_fraction=0.1,
        light_fraction=0.5,
        dark_fraction=0.1,
        neutral_fraction=0.8,
        colorfulness=0.1,
        brightness_mean=0.5,
        brightness_std=0.2,
        entropy=6.0,
        edge_strength=0.2,
        edge_fraction=0.2,
        quantized_colors=32,
        border_white_fraction=0.1,
        long_horizontal_lines=0.25,
        long_vertical_lines=0.2,
        text_band_fraction=0.1,
        top_blue_fraction=0.0,
        green_fraction=0.0,
        warm_fraction=0.0,
        skin_fraction=0.0,
        central_skin_fraction=0.0,
        flash_fired=False,
        iso=None,
        exposure_time=None,
        focal_length_35mm=None,
    )
    result = FeatureVisualClassifier().classify(Path("panel.jpg"), features)
    assert result.calibrated is False
    assert result.entities[0].label == "equipo_electrico_panelizado_candidato"
    assert result.operational_contexts
    assert result.entities[0].score < 0.5
    assert result.provenance == ("visual-features-multilabel-v1",)


# endregion [02]
