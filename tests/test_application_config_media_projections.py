"""Application projections for integrated route owners."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import neocortex.runtime.config.application_config_projections as runtime_projections
from neocortex.api.public import ApplicationConfig
from neocortex.runtime.config.application_config import (
    audio_route_config_from_application,
    code_route_config_from_application,
    image_route_config_from_application,
    office_route_config_from_application,
)
from neocortex.capabilities.formats.audio.models import AudioRouteConfig
from neocortex.runtime.config.app_paths import (
    default_code_project_roots,
    source_repository_directory,
)
from neocortex.capabilities.formats.image.route import ImageRouteConfig
from neocortex.capabilities.formats.office.route import OfficeRouteConfig
from neocortex.safety.route_filters import CandidateSelection
from neocortex.runtime.orchestration.route_registry import (
    audio_route_config_from_framework,
    image_route_config_from_framework,
    office_route_config_from_framework,
)


# region [01] Default parity and effective paths


def test_default_media_projections_use_current_canonical_paths_and_root() -> None:
    requested = ApplicationConfig(
        root=Path("requested-media-root"),
        state_directory=Path("requested-media-state"),
    )
    canonical = replace(
        requested,
        root=Path("canonical-media-root"),
        state_directory=Path("canonical-media-state"),
    )

    audio = audio_route_config_from_application(canonical)
    expected_audio = AudioRouteConfig(
        state_path=Path("canonical-media-state") / "audio.sqlite3",
        device=canonical.audio_device,
        compute_type=canonical.audio_compute_type,
        model_cache_directory=canonical.audio_model_cache_directory,
        local_models_only=canonical.audio_local_models_only,
    )
    office = office_route_config_from_application(canonical)
    expected_office = OfficeRouteConfig(
        state_path=Path("canonical-media-state") / "office.sqlite3",
    )
    image = image_route_config_from_application(canonical)
    expected_image = ImageRouteConfig(
        state_path=Path("canonical-media-state") / "image.sqlite3",
        root=Path("canonical-media-root"),
    )

    assert requested.image_database == Path("requested-media-state") / "image.sqlite3"
    assert audio == expected_audio
    assert office == expected_office
    assert image == expected_image
    assert image.isolate_decoders is True
    assert office.processing_signature == expected_office.processing_signature
    assert audio.processing_signature(
        backend_version="test-backend",
        ctranslate2_version="test-ctranslate2",
        resolved_device="cpu",
        resolved_compute_type="int8",
    ) == expected_audio.processing_signature(
        backend_version="test-backend",
        ctranslate2_version="test-ctranslate2",
        resolved_device="cpu",
        resolved_compute_type="int8",
    )


def test_image_projection_accepts_the_route_context_effective_root() -> None:
    config = ApplicationConfig(
        root=Path("configured-image-root"),
        state_directory=Path("effective-image-state"),
    )

    projected = image_route_config_from_application(
        config,
        root=Path("context-image-root"),
    )

    assert projected.root == Path("context-image-root")
    assert projected.state_path == Path("effective-image-state") / "image.sqlite3"
    assert projected.isolate_decoders is True


def test_code_projection_uses_only_the_configured_project_allowlist(tmp_path: Path) -> None:
    owned_roots = (tmp_path / "Neocortex", tmp_path / "Bitacoras-EPS")
    config = ApplicationConfig(
        root=tmp_path,
        state_directory=tmp_path / "state",
        code_project_roots=owned_roots,
    )

    projected = code_route_config_from_application(config)
    self_analysis = code_route_config_from_application(replace(config, self_analysis=True))

    assert projected.explicit_project_roots == owned_roots
    assert self_analysis.explicit_project_roots == (tmp_path,)
    assert default_code_project_roots() == (
        source_repository_directory(),
        Path.home() / "Frameworks" / "Generador de bitácoras EPS",
    )


# endregion [01]


# region [02] Exhaustive override and signature parity


def test_audio_projection_preserves_every_override_and_signature_input() -> None:
    selection = CandidateSelection(
        statuses=("partial",),
        error_types=("AudioProbeError",),
        recommendations=("retry",),
        paths=(r"C:\Corpus\recording.wav",),
    )
    config = ApplicationConfig(
        state_directory=Path("overridden-audio-state"),
        selection=selection,
        audio_model_name="medium",
        audio_device="cpu",
        audio_compute_type="int8",
        audio_language="es",
        audio_beam_size=3,
        audio_vad_filter=False,
        audio_include_video=False,
        audio_max_file_bytes=90_000_000,
        audio_max_documents=11,
        audio_max_duration_seconds=1_234.5,
        audio_max_transcript_chars=345_678,
        audio_max_segments=4_321,
        audio_file_timeout_seconds=456.5,
        audio_worker_startup_timeout_seconds=567.5,
        audio_worker_memory_bytes=678_000_000,
        audio_retry_errors=True,
        audio_ffprobe_path=r"C:\Tools\ffprobe.exe",
        audio_model_cache_directory=Path("audio-model-cache"),
        audio_local_models_only=True,
        audio_memory_budget_bytes=789_000_000,
        audio_min_free_memory_bytes=890_000_000,
        audio_min_free_commit_bytes=901_000_000,
        audio_memory_wait_timeout_seconds=33.5,
    )
    expected = AudioRouteConfig(
        state_path=Path("overridden-audio-state") / "audio.sqlite3",
        model_name="medium",
        device="cpu",
        compute_type="int8",
        language="es",
        beam_size=3,
        vad_filter=False,
        include_video=False,
        max_file_bytes=90_000_000,
        max_documents=11,
        max_duration_seconds=1_234.5,
        max_transcript_chars=345_678,
        max_segments=4_321,
        file_timeout_seconds=456.5,
        worker_startup_timeout_seconds=567.5,
        worker_memory_bytes=678_000_000,
        retry_errors=True,
        ffprobe_path=r"C:\Tools\ffprobe.exe",
        model_cache_directory=Path("audio-model-cache"),
        local_models_only=True,
        selection=selection,
        memory_budget_bytes=789_000_000,
        min_free_memory_bytes=890_000_000,
        min_free_commit_bytes=901_000_000,
        memory_wait_timeout_seconds=33.5,
    )

    projected = audio_route_config_from_application(config)

    assert projected == expected
    assert projected.processing_signature(
        backend_version="test-backend",
        ctranslate2_version="test-ctranslate2",
        resolved_device="cpu",
        resolved_compute_type="int8",
    ) == expected.processing_signature(
        backend_version="test-backend",
        ctranslate2_version="test-ctranslate2",
        resolved_device="cpu",
        resolved_compute_type="int8",
    )


def test_office_projection_preserves_every_override_and_signature_input() -> None:
    selection = CandidateSelection(
        statuses=("error",),
        recommendations=("manual_review",),
        paths=(r"C:\Corpus\workbook.xlsx",),
    )
    config = ApplicationConfig(
        state_directory=Path("overridden-office-state"),
        selection=selection,
        office_max_file_bytes=21_000_000,
        office_max_documents=8,
        office_max_text_chars=987_654,
        office_retry_errors=True,
        office_memory_budget_bytes=321_000_000,
        office_min_free_memory_bytes=432_000_000,
        office_min_free_commit_bytes=543_000_000,
        office_memory_wait_timeout_seconds=18.5,
    )
    expected = OfficeRouteConfig(
        state_path=Path("overridden-office-state") / "office.sqlite3",
        max_file_bytes=21_000_000,
        max_documents=8,
        max_text_chars=987_654,
        retry_errors=True,
        selection=selection,
        memory_budget_bytes=321_000_000,
        min_free_memory_bytes=432_000_000,
        min_free_commit_bytes=543_000_000,
        memory_wait_timeout_seconds=18.5,
    )

    projected = office_route_config_from_application(config)

    assert projected == expected
    assert projected.processing_signature == expected.processing_signature


def test_image_projection_preserves_every_override_and_signature_input() -> None:
    selection = CandidateSelection(
        statuses=("error",),
        error_types=("ImageDecodeError",),
        recommendations=("retry",),
        paths=(r"C:\Corpus\inspection.jpg",),
    )
    config = ApplicationConfig(
        root=Path("overridden-image-root"),
        state_directory=Path("overridden-image-state"),
        selection=selection,
        image_workers=2,
        image_max_file_bytes=31_000_000,
        image_max_documents=9,
        image_retry_errors=True,
        image_memory_budget_bytes=432_000_000,
        image_min_free_memory_bytes=543_000_000,
        image_min_free_commit_bytes=654_000_000,
        image_memory_wait_timeout_seconds=17.5,
        image_worker_timeout_seconds=98.5,
        image_document_ocr_mode="never",
        image_document_ocr_lang="eng",
        image_document_ocr_timeout_seconds=7.5,
        image_tesseract_cmd=r"C:\Tools\tesseract.exe",
        image_tessdata_dir=r"C:\Tools\tessdata",
    )
    expected = ImageRouteConfig(
        state_path=Path("overridden-image-state") / "image.sqlite3",
        root=Path("effective-context-root"),
        workers=2,
        max_file_bytes=31_000_000,
        max_documents=9,
        retry_errors=True,
        selection=selection,
        memory_budget_bytes=432_000_000,
        min_free_memory_bytes=543_000_000,
        min_free_commit_bytes=654_000_000,
        memory_wait_timeout_seconds=17.5,
        worker_timeout_seconds=98.5,
        isolate_decoders=True,
        document_ocr_mode="never",
        document_ocr_lang="eng",
        document_ocr_timeout_seconds=7.5,
        tesseract_cmd=r"C:\Tools\tesseract.exe",
        tessdata_dir=r"C:\Tools\tessdata",
    )

    projected = image_route_config_from_application(
        config,
        root=Path("effective-context-root"),
    )

    assert projected == expected
    assert projected.isolate_decoders is True
    assert projected.processing_signature == expected.processing_signature


# endregion [02]


# region [03] Facade and route-registry delegation


def test_application_facade_reexports_media_runtime_projections() -> None:
    assert (
        audio_route_config_from_application
        is runtime_projections.audio_route_config_from_application
    )
    assert (
        image_route_config_from_application
        is runtime_projections.image_route_config_from_application
    )
    assert (
        office_route_config_from_application
        is runtime_projections.office_route_config_from_application
    )


def test_route_registry_delegates_media_projections() -> None:
    config = ApplicationConfig(
        root=Path("legacy-media-root"),
        state_directory=Path("legacy-media-state"),
    )

    with patch(
        "neocortex.runtime.config.application_config_projections.audio_route_config_from_application",
        wraps=audio_route_config_from_application,
    ) as audio_projection:
        legacy_audio = audio_route_config_from_framework(config)
    with patch(
        "neocortex.runtime.config.application_config_projections.office_route_config_from_application",
        wraps=office_route_config_from_application,
    ) as office_projection:
        legacy_office = office_route_config_from_framework(config)
    with patch(
        "neocortex.runtime.config.application_config_projections.image_route_config_from_application",
        wraps=image_route_config_from_application,
    ) as image_projection:
        legacy_image = image_route_config_from_framework(
            config,
            root=Path("legacy-context-root"),
        )

    audio_projection.assert_called_once_with(config)
    office_projection.assert_called_once_with(config)
    image_projection.assert_called_once_with(
        config,
        root=Path("legacy-context-root"),
    )
    assert legacy_audio == audio_route_config_from_application(config)
    assert legacy_office == office_route_config_from_application(config)
    assert legacy_image == image_route_config_from_application(
        config,
        root=Path("legacy-context-root"),
    )


# endregion [03]
