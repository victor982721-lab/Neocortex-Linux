"""Configuration and results for the integrated framework."""
# region [00] Contexto del módulo
# Módulo: neocortex/runtime/models.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import DedupPlan, ScanSummary
from neocortex.platform_policy import (
    default_corpus_root,
    default_local_models_only,
    default_whisper_compute_type,
    default_whisper_device,
    default_whisper_model_cache,
)

from neocortex.runtime.config.app_paths import (
    default_code_project_roots,
    default_state_directory,
)
from neocortex.safety.ocr_profiles import OcrProfileName
from neocortex.safety.route_filters import CandidateSelection

# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.models import ArchiveRouteSummary
    from neocortex.capabilities.formats.audio.models import AudioRouteSummary
    from neocortex.code.code_contracts import CodeRouteSummary
    from neocortex.documents.document_organization import (
        OrganizationApplySummary,
        OrganizationPlanSummary,
    )
    from neocortex.capabilities.formats.docx.route import DocxRouteSummary
    from neocortex.runtime.control.global_resources import GlobalResourceSummary
    from neocortex.capabilities.formats.image.route import ImageRouteSummary
    from neocortex.capabilities.formats.office.route import OfficeRouteSummary
    from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteSummary
    from neocortex.capabilities.formats.text.text_route import TextRouteSummary
    from neocortex.capabilities.formats.video.models import VideoRouteSummary


@dataclass(frozen=True, slots=True)
class FrameworkConfig:
    root: Path = field(default_factory=default_corpus_root)
    state_directory: Path = field(default_factory=default_state_directory)
    self_analysis: bool = False
    analysis_profile: Literal["protected", "trusted-static", "trusted-deep"] = "protected"
    corpus_access_mode: Literal["normal", "analyze_only"] = "normal"
    apply_actions: bool = False
    preview_group_limit: int = 0
    dedup_policy: Literal["fast", "exact"] = "fast"
    route: str = "none"
    route_only: bool = False
    candidate_run_id: int | None = None
    resume_run_id: int | None = None
    selection: CandidateSelection = field(default_factory=CandidateSelection)
    heartbeat_interval_seconds: float = 5.0
    document_catalog_enabled: bool = True
    document_taxonomy_path: Path | None = None
    document_classification_max_chars: int = 64_000
    organization_root: Path | None = None
    organization_min_confidence: float = 0.72
    global_memory_budget_bytes: int | None = None
    global_min_free_memory_bytes: int | None = None
    global_min_free_commit_bytes: int | None = None
    global_cpu_slots: int | None = None
    global_max_cpu_load_percent: float = 90.0
    global_resource_wait_timeout_seconds: float = 300.0
    code_max_file_bytes: int = 8 * 1024 * 1024
    code_max_documents: int | None = None
    code_max_text_chars: int = 4_000_000
    code_chunk_chars: int = 12_000
    code_retry_errors: bool = False
    code_cache_validation: Literal["metadata", "full"] = "metadata"
    code_candidate_scope: Literal["projects", "broad"] = "projects"
    code_project_roots: tuple[Path, ...] = field(default_factory=default_code_project_roots)
    code_include_generated: bool = False
    code_include_vendored: bool = False
    code_complexity_warning: int = 15
    code_function_lines_warning: int = 200
    image_workers: int = 4
    image_max_file_bytes: int | None = None
    image_max_documents: int | None = None
    image_retry_errors: bool = False
    image_memory_budget_bytes: int = 512 * 1024 * 1024
    image_min_free_memory_bytes: int = 1024 * 1024 * 1024
    image_min_free_commit_bytes: int = 1024 * 1024 * 1024
    image_memory_wait_timeout_seconds: float = 60.0
    image_worker_timeout_seconds: float = 120.0
    image_document_ocr_mode: Literal["auto", "never"] = "auto"
    image_document_ocr_lang: str = "spa+eng"
    image_document_ocr_timeout_seconds: float = 12.0
    image_tesseract_cmd: str | None = None
    image_tessdata_dir: str | None = None
    docx_max_file_bytes: int | None = None
    docx_max_documents: int | None = None
    docx_max_text_chars: int = 20_000_000
    docx_retry_errors: bool = False
    docx_memory_budget_bytes: int = 512 * 1024 * 1024
    docx_min_free_memory_bytes: int = 1024 * 1024 * 1024
    docx_min_free_commit_bytes: int = 1024 * 1024 * 1024
    docx_memory_wait_timeout_seconds: float = 60.0
    office_max_file_bytes: int | None = None
    office_max_documents: int | None = None
    office_max_text_chars: int = 20_000_000
    office_retry_errors: bool = False
    office_memory_budget_bytes: int = 512 * 1024 * 1024
    office_min_free_memory_bytes: int = 1024 * 1024 * 1024
    office_min_free_commit_bytes: int = 1024 * 1024 * 1024
    office_memory_wait_timeout_seconds: float = 60.0
    archive_max_file_bytes: int | None = None
    archive_max_documents: int | None = None
    archive_retry_errors: bool = False
    archive_max_depth: int = 5
    archive_max_members: int = 20_000
    archive_max_central_directory_bytes: int = 32 * 1024 * 1024
    archive_max_member_bytes: int = 64 * 1024 * 1024
    archive_max_total_uncompressed_bytes: int = 512 * 1024 * 1024
    archive_max_text_chars: int = 2_000_000
    archive_max_total_text_chars: int = 20_000_000
    archive_max_compression_ratio: float = 200.0
    archive_pdf_max_pages: int = 500
    archive_pdf_timeout_seconds: float = 60.0
    archive_pdf_worker_memory_bytes: int = 768 * 1024 * 1024
    archive_ocr_mode: Literal["auto", "never", "always"] = "auto"
    archive_ocr_lang: str = "spa+eng"
    archive_ocr_dpi: int = 200
    archive_ocr_max_pages: int = 50
    archive_ocr_max_render_pixels: int = 40_000_000
    archive_ocr_timeout_seconds: float = 30.0
    archive_tesseract_cmd: str | None = None
    archive_tessdata_dir: str | None = None
    text_max_file_bytes: int | None = 64 * 1024 * 1024
    text_max_documents: int | None = None
    text_max_text_chars: int = 4_000_000
    text_worker_timeout_seconds: float = 60.0
    text_worker_memory_bytes: int = 1024 * 1024 * 1024
    text_retry_errors: bool = False
    text_libreoffice_cmd: str | None = None
    audio_model_name: str = "small"
    audio_device: Literal["auto", "cpu", "cuda"] = field(default_factory=default_whisper_device)
    audio_compute_type: str = field(default_factory=default_whisper_compute_type)
    audio_language: str | None = None
    audio_beam_size: int = 5
    audio_vad_filter: bool = True
    audio_include_video: bool = True
    audio_max_file_bytes: int | None = None
    audio_max_documents: int | None = None
    audio_max_duration_seconds: float = 6 * 60 * 60
    audio_max_transcript_chars: int = 5_000_000
    audio_max_segments: int = 100_000
    audio_file_timeout_seconds: float = 3600.0
    audio_worker_startup_timeout_seconds: float = 1800.0
    audio_worker_memory_bytes: int = 4 * 1024 * 1024 * 1024
    audio_retry_errors: bool = False
    audio_ffprobe_path: str | None = None
    audio_model_cache_directory: Path | None = field(default_factory=default_whisper_model_cache)
    audio_local_models_only: bool = field(default_factory=default_local_models_only)
    audio_memory_budget_bytes: int = 2 * 1024 * 1024 * 1024
    audio_min_free_memory_bytes: int = 2 * 1024 * 1024 * 1024
    audio_min_free_commit_bytes: int = 2 * 1024 * 1024 * 1024
    audio_memory_wait_timeout_seconds: float = 300.0
    video_max_file_bytes: int | None = None
    video_max_documents: int | None = None
    video_max_duration_seconds: float = 6 * 60 * 60
    video_max_frames: int = 48
    video_interval_seconds: float = 30.0
    video_scene_threshold: float = 0.35
    video_include_scenes: bool = True
    video_include_keyframes: bool = True
    video_max_frame_pixels: int = 2_073_600
    video_max_frame_side: int = 1920
    video_probe_timeout_seconds: float = 30.0
    video_discovery_timeout_seconds: float = 60.0
    video_frame_timeout_seconds: float = 20.0
    video_file_timeout_seconds: float = 300.0
    video_worker_memory_bytes: int = 2 * 1024 * 1024 * 1024
    video_retry_errors: bool = False
    video_ffmpeg_path: str | None = None
    video_ffprobe_path: str | None = None
    video_ocr_mode: Literal["auto", "never"] = "auto"
    video_ocr_lang: str = "spa+eng"
    video_ocr_profile: OcrProfileName = "configured"
    video_ocr_timeout_seconds: float = 12.0
    video_tesseract_cmd: str | None = None
    video_tessdata_dir: str | None = None
    pdf_ocr_mode: Literal["auto", "never", "always"] = "auto"
    pdf_ocr_lang: str = "spa+eng"
    pdf_dpi: int = 200
    pdf_workers: int = 4
    pdf_ocr_workers: int = 2
    pdf_min_page_chars: int = 40
    pdf_max_page_text_chars: int = 5_000_000
    pdf_max_render_pixels: int = 40_000_000
    pdf_max_pages: int | None = None
    pdf_max_file_bytes: int | None = None
    pdf_max_documents: int | None = None
    pdf_max_ocr_pages: int | None = None
    pdf_ocr_timeout_seconds: int = 120
    pdf_retry_errors: bool = False
    pdfminer_fallback: bool = True
    pdf_similarity_threshold: float = 0.92
    pdf_cache_validation: Literal["metadata", "full"] = "metadata"
    pdf_tesseract_cmd: str | None = None
    pdf_tessdata_dir: str | None = None
    pdf_page_start: int | None = None
    pdf_page_end: int | None = None
    pdf_fail_fast_pages: bool = False
    pdf_document_timeout_seconds: float | None = 600.0
    pdf_timeout_mode: Literal["fixed", "adaptive"] = "adaptive"
    pdf_max_document_timeout_seconds: float = 1200.0
    pdf_min_free_bytes: int = 512 * 1024 * 1024
    pdf_memory_backpressure_bytes: int | None = None
    pdf_commit_backpressure_bytes: int | None = None
    pdf_memory_budget_bytes: int | None = None
    pdf_worker_memory_bytes: int = 512 * 1024 * 1024
    pdf_memory_wait_timeout_seconds: float = 60.0
    pdf_large_document_bytes: int = 128 * 1024 * 1024
    pdf_large_document_workers: int = 2
    deep_test_selectors: tuple[str, ...] = ()
    deep_max_tests: int = 3000
    deep_time_budget_seconds: int = 600
    deep_shard_size: int = 20
    deep_mutation_target: str | None = None
    deep_mutation_symbol: str | None = None
    deep_mutation_max_mutants: int = 20
    deep_mutation_timeout_seconds: int = 30
    deep_mutation_time_budget_seconds: int = 600
    image_document_ocr_profile: OcrProfileName = "configured"
    pdf_ocr_profile: OcrProfileName = "configured"

    @property
    def dedup_database(self) -> Path:
        return self.state_directory / "dedup.sqlite3"

    @property
    def framework_database(self) -> Path:
        return self.state_directory / "framework.sqlite3"

    @property
    def document_catalog_database(self) -> Path:
        return self.state_directory / "document_catalog.sqlite3"

    @property
    def pdf_database(self) -> Path:
        return self.state_directory / "pdf.sqlite3"

    @property
    def docx_database(self) -> Path:
        return self.state_directory / "docx.sqlite3"

    @property
    def office_database(self) -> Path:
        return self.state_directory / "office.sqlite3"

    @property
    def archive_database(self) -> Path:
        return self.state_directory / "archive.sqlite3"

    @property
    def text_database(self) -> Path:
        return self.state_directory / "text.sqlite3"

    @property
    def audio_database(self) -> Path:
        return self.state_directory / "audio.sqlite3"

    @property
    def image_database(self) -> Path:
        return self.state_directory / "image.sqlite3"

    @property
    def video_database(self) -> Path:
        return self.state_directory / "video.sqlite3"

    @property
    def code_database(self) -> Path:
        return self.state_directory / "code.sqlite3"


@dataclass(frozen=True, slots=True)
class InitialRunResult:
    run_id: int
    scan: ScanSummary
    dedup_plan: DedupPlan
    journal_before: JournalCursor | None
    journal_after: JournalCursor | None
    reconciliation_records: int
    inventory_attempts: int
    inventory_mode: Literal["full", "incremental"]
    actions: ActionSummary
    pdf: PdfRouteSummary | None = None
    docx: DocxRouteSummary | None = None
    office: OfficeRouteSummary | None = None
    archive: ArchiveRouteSummary | None = None
    text: TextRouteSummary | None = None
    audio: AudioRouteSummary | None = None
    video: VideoRouteSummary | None = None
    image: ImageRouteSummary | None = None
    code: CodeRouteSummary | None = None
    route_results: dict[str, object] = field(default_factory=dict)
    global_resources: GlobalResourceSummary | None = None
    organization_plan: OrganizationPlanSummary | None = None
    organization_apply: OrganizationApplySummary | None = None

    @property
    def journal_usn_span(self) -> int | None:
        if self.journal_before is None or self.journal_after is None:
            return None
        return self.journal_after.next_usn - self.journal_before.next_usn


@dataclass(frozen=True, slots=True)
class SelfAnalysisRunResult:
    """Results of protected code analysis without a corpus-action phase."""

    run_id: int
    scan: ScanSummary
    journal_before: JournalCursor | None
    journal_after: JournalCursor | None
    reconciliation_records: int
    inventory_attempts: int
    inventory_mode: Literal["full", "incremental"]
    inventory_policy_signature: str
    code: CodeRouteSummary
    route_results: dict[str, object] = field(default_factory=dict)
    global_resources: GlobalResourceSummary | None = None
    corpus_action_count: int = 0
    route_candidate_count: int = 0

    @property
    def journal_usn_span(self) -> int | None:
        if self.journal_before is None or self.journal_after is None:
            return None
        return self.journal_after.next_usn - self.journal_before.next_usn


@dataclass(frozen=True, slots=True)
class RouteOnlyRunResult:
    """Results of a route-only run over durable route inputs."""

    run_id: int
    source_run_id: int
    route_results: dict[str, object] = field(default_factory=dict)
    global_resources: GlobalResourceSummary | None = None
    pdf: PdfRouteSummary | None = None
    docx: DocxRouteSummary | None = None
    office: OfficeRouteSummary | None = None
    archive: ArchiveRouteSummary | None = None
    text: TextRouteSummary | None = None
    audio: AudioRouteSummary | None = None
    video: VideoRouteSummary | None = None
    image: ImageRouteSummary | None = None
    code: CodeRouteSummary | None = None
    actions: ActionSummary = field(default_factory=lambda: ActionSummary(False))


@dataclass(frozen=True, slots=True)
class ActionSummary:
    """Results of duplicate disposal and content-type validation."""

    apply_actions: bool
    duplicate_candidates: int = 0
    duplicates_trashed: int = 0
    duplicate_skips: int = 0
    files_checked: int = 0
    types_detected: int = 0
    extensions_matching: int = 0
    unknown_types: int = 0
    type_cache_hits: int = 0
    type_cache_misses: int = 0
    type_cache_pruned: int = 0
    stale_inventory: int = 0
    rename_candidates: int = 0
    files_renamed: int = 0
    rename_skips: int = 0
    empty_directory_candidates: int = 0
    empty_directories_trashed: int = 0
    empty_directory_skips: int = 0
    errors: int = 0


from neocortex.platform import preserve_legacy_module as _preserve_legacy_module


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.models")


# endregion [02]
