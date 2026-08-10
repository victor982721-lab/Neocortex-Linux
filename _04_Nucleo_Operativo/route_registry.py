"""Extensible route registry and built-in content-route adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, TYPE_CHECKING
from warnings import warn

from .route_selection import BUILTIN_ROUTE_ORDER as BUILTIN_ROUTE_ORDER
from .route_selection import normalize_route_selection as normalize_route_selection

if TYPE_CHECKING:
    from _03_Progreso import ProgressCallback

    from .archive_route import ArchiveRoute as ArchiveRoute
    from .archive_route import ArchiveRouteConfig as ArchiveRouteConfig
    from .audio_models import AudioRouteConfig as AudioRouteConfig
    from .audio_route import AudioRoute as AudioRoute
    from .code_contracts import CodeRouteConfig as CodeRouteConfig
    from .code_route import CodeRoute as CodeRoute
    from .cancellation import CancellationToken
    from .docx_route import DocxRoute as DocxRoute
    from .docx_route import DocxRouteConfig as DocxRouteConfig
    from .document_catalog import CatalogUpdateSummary, SourceKind
    from .global_resources import GlobalResourceCoordinator
    from .image_route import ImageRoute as ImageRoute
    from .image_route import ImageRouteConfig as ImageRouteConfig
    from .models import FrameworkConfig
    from .office_route import OfficeRoute as OfficeRoute
    from .office_route import OfficeRouteConfig as OfficeRouteConfig
    from .pdf_route import PdfRoute as PdfRoute
    from .pdf_route import PdfRouteConfig as PdfRouteConfig
    from .state import FrameworkRouteState
    from .text_route import TextRoute as TextRoute
    from .text_route import TextRouteConfig as TextRouteConfig
    from .video_route import VideoRoute as VideoRoute
    from .video_route import VideoRouteConfig as VideoRouteConfig


# region [01] Generic route contracts and selection reexports


@dataclass(frozen=True, slots=True)
class RouteExecutionContext:
    config: "FrameworkConfig"
    root: Path
    framework_state: "FrameworkRouteState"
    run_id: int
    scan_id: int
    progress: "ProgressCallback | None"
    resource_coordinator: GlobalResourceCoordinator | None
    cancellation: "CancellationToken"


@dataclass(frozen=True, slots=True)
class RouteAdapter:
    name: str
    execute: Callable[[RouteExecutionContext], object]
    input_source: Literal["route_candidates", "inventory_snapshot"] = "route_candidates"

    def summary_mapping(self, summary: object) -> Mapping[str, Any]:
        if is_dataclass(summary) and not isinstance(summary, type):
            return asdict(summary)
        if isinstance(summary, Mapping):
            return dict(summary)
        raise TypeError(
            f"route {self.name} returned a non-serializable summary: {type(summary).__name__}"
        )


# endregion [01]


# region [02] Deferred compatibility exports


_DEFERRED_ROUTE_EXPORTS = {
    "AudioRoute": (".audio_route", "AudioRoute"),
    "AudioRouteConfig": (".audio_models", "AudioRouteConfig"),
    "ArchiveRoute": (".archive_route", "ArchiveRoute"),
    "ArchiveRouteConfig": (".archive_route", "ArchiveRouteConfig"),
    "CodeRoute": (".code_route", "CodeRoute"),
    "CodeRouteConfig": (".code_contracts", "CodeRouteConfig"),
    "PdfRoute": (".pdf_route", "PdfRoute"),
    "PdfRouteConfig": (".pdf_route", "PdfRouteConfig"),
    "DocxRoute": (".docx_route", "DocxRoute"),
    "DocxRouteConfig": (".docx_route", "DocxRouteConfig"),
    "ImageRoute": (".image_route", "ImageRoute"),
    "ImageRouteConfig": (".image_route", "ImageRouteConfig"),
    "OfficeRoute": (".office_route", "OfficeRoute"),
    "OfficeRouteConfig": (".office_route", "OfficeRouteConfig"),
    "TextRoute": (".text_route", "TextRoute"),
    "TextRouteConfig": (".text_route", "TextRouteConfig"),
    "VideoRoute": (".video_route", "VideoRoute"),
    "VideoRouteConfig": (".video_route", "VideoRouteConfig"),
}


def __getattr__(name: str) -> Any:
    """Resolve historical route-registry attributes without eager imports."""

    target = _DEFERRED_ROUTE_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warn(
        f"route_registry.{name} is deprecated; import {name} from its route module",
        DeprecationWarning,
        stacklevel=2,
    )
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __package__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose deferred compatibility names to interactive introspection."""

    return sorted({*globals(), *_DEFERRED_ROUTE_EXPORTS})


# endregion [02]


# region [03] Built-in route adapters


def pdf_route_config_from_framework(config: "FrameworkConfig") -> "PdfRouteConfig":
    """Preserve the route-registry projection boundary for PDF execution."""

    from .application_config_projections import pdf_route_config_from_application

    return pdf_route_config_from_application(config)


def _run_pdf(context: RouteExecutionContext) -> object:
    from _02_Deduplicacion import DedupIndex

    from .pdf_route import PdfRoute

    config = context.config
    with DedupIndex(config.dedup_database) as dedup_index:
        summary = PdfRoute(
            pdf_route_config_from_framework(config),
            dedup_index,
            context.framework_state,
            context.run_id,
            context.scan_id,
            progress=context.progress,
            global_coordinator=context.resource_coordinator,
            cancellation=context.cancellation,
        ).run()
    catalog = _update_document_catalog_after_route(context, "pdf")
    if catalog:
        summary = _summary_with_catalog(summary, catalog)
    return summary


def image_route_config_from_framework(
    config: "FrameworkConfig",
    *,
    root: Path | None = None,
) -> "ImageRouteConfig":
    """Preserve the route-registry projection boundary for image execution."""

    from .application_config_projections import image_route_config_from_application

    return image_route_config_from_application(config, root=root)


def _run_image(context: RouteExecutionContext) -> object:
    from _02_Deduplicacion import DedupIndex

    from .global_resources import CoordinatedMemoryGate
    from .image_route import ImageRoute

    config = context.config
    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "image")
    )
    with DedupIndex(config.dedup_database) as dedup_index:
        return ImageRoute(
            image_route_config_from_framework(config, root=context.root),
            context.framework_state,
            context.run_id,
            progress=context.progress,
            memory_gate=gate,
            cancellation=context.cancellation,
            dedup_index=dedup_index,
        ).run()


def docx_route_config_from_framework(config: "FrameworkConfig") -> "DocxRouteConfig":
    """Preserve the route-registry projection boundary for DOCX execution."""

    from .application_config_projections import docx_route_config_from_application

    return docx_route_config_from_application(config)


def _run_docx(context: RouteExecutionContext) -> object:
    from .docx_route import DocxRoute
    from .global_resources import CoordinatedMemoryGate

    config = context.config
    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "docx")
    )
    summary = DocxRoute(
        docx_route_config_from_framework(config),
        context.framework_state,
        context.run_id,
        progress=context.progress,
        memory_gate=gate,
        cancellation=context.cancellation,
    ).run()
    catalog = _update_document_catalog_after_route(context, "docx")
    if catalog:
        summary = _summary_with_catalog(summary, catalog)
    return summary


def office_route_config_from_framework(
    config: "FrameworkConfig",
) -> "OfficeRouteConfig":
    """Preserve the route-registry projection boundary for Office execution."""

    from .application_config_projections import office_route_config_from_application

    return office_route_config_from_application(config)


def _run_office(context: RouteExecutionContext) -> object:
    from .global_resources import CoordinatedMemoryGate
    from .office_route import OfficeRoute

    config = context.config
    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "office")
    )
    summary = OfficeRoute(
        office_route_config_from_framework(config),
        context.framework_state,
        context.run_id,
        progress=context.progress,
        memory_gate=gate,
        cancellation=context.cancellation,
    ).run()
    catalogs = _update_document_catalog_after_route(context, "office")
    if catalogs:
        summary = _summary_with_catalog(summary, catalogs)
    return summary


def archive_route_config_from_framework(
    config: "FrameworkConfig",
) -> "ArchiveRouteConfig":
    """Project application limits into the recursive ZIP route."""

    from .application_config_projections import archive_route_config_from_application

    return archive_route_config_from_application(config)


def _run_archive(context: RouteExecutionContext) -> object:
    from .archive_route import ArchiveRoute

    gate = None
    if context.resource_coordinator is not None:
        from .global_resources import CoordinatedMemoryGate

        gate = CoordinatedMemoryGate(context.resource_coordinator, "archive")
    return ArchiveRoute(
        archive_route_config_from_framework(context.config),
        context.framework_state,
        context.run_id,
        progress=context.progress,
        memory_gate=gate,
        cancellation=context.cancellation,
    ).run()


def text_route_config_from_framework(config: "FrameworkConfig") -> "TextRouteConfig":
    """Project application limits into generic text extraction."""

    from .application_config_projections import text_route_config_from_application

    return text_route_config_from_application(config)


def _run_text(context: RouteExecutionContext) -> object:
    from .global_resources import CoordinatedMemoryGate
    from .text_route import TextRoute

    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "text")
    )
    summary = TextRoute(
        text_route_config_from_framework(context.config),
        context.framework_state,
        context.run_id,
        progress=context.progress,
        memory_gate=gate,
        cancellation=context.cancellation,
    ).run()
    catalog = _update_document_catalog_after_route(context, "text")
    if catalog:
        summary = _summary_with_catalog(summary, catalog)
    return summary


def audio_route_config_from_framework(config: "FrameworkConfig") -> "AudioRouteConfig":
    """Preserve the route-registry projection boundary for audio execution."""

    from .application_config_projections import audio_route_config_from_application

    return audio_route_config_from_application(config)


def _run_audio(context: RouteExecutionContext) -> object:
    from .audio_route import AudioRoute
    from .global_resources import CoordinatedMemoryGate

    config = context.config
    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "audio")
    )
    summary = AudioRoute(
        audio_route_config_from_framework(config),
        context.framework_state,
        context.run_id,
        progress=context.progress,
        memory_gate=gate,
        cancellation=context.cancellation,
    ).run()
    catalogs = _update_document_catalog_after_route(context, "audio")
    if catalogs:
        summary = _summary_with_catalog(summary, catalogs)
    return summary


def video_route_config_from_framework(
    config: "FrameworkConfig",
    *,
    root: Path | None = None,
) -> "VideoRouteConfig":
    """Project application limits into dedicated visual-video inspection."""

    from .application_config_projections import video_route_config_from_application

    return video_route_config_from_application(config, root=root)


def _run_video(context: RouteExecutionContext) -> object:
    from .global_resources import CoordinatedMemoryGate
    from .video_route import VideoRoute

    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "video")
    )
    return VideoRoute(
        video_route_config_from_framework(context.config, root=context.root),
        context.framework_state,
        context.run_id,
        progress=context.progress,
        memory_gate=gate,
        cancellation=context.cancellation,
    ).run()


def code_route_config_from_framework(config: "FrameworkConfig") -> "CodeRouteConfig":
    """Preserve the historical projection name at the route-registry boundary."""

    from .application_config_projections import code_route_config_from_application

    return code_route_config_from_application(config)


def _run_code(context: RouteExecutionContext) -> object:
    from _02_Deduplicacion import DedupIndex

    from .code_route import CodeRoute

    config = context.config
    gate = None
    if context.resource_coordinator is not None:
        from .global_resources import CoordinatedMemoryGate

        gate = CoordinatedMemoryGate(context.resource_coordinator, "code")
    with DedupIndex(config.dedup_database) as dedup_index:
        return CodeRoute(
            code_route_config_from_framework(config),
            dedup_index,
            context.framework_state,
            context.run_id,
            context.scan_id,
            progress=context.progress,
            cancellation=context.cancellation,
            memory_gate=gate,
        ).run()


def _update_document_catalog_after_route(
    context: RouteExecutionContext,
    source_kind: Literal["pdf", "docx", "office", "text", "audio"],
) -> "tuple[CatalogUpdateSummary, ...]":
    """Classify only the source cache completed by this route."""

    if not context.config.document_catalog_enabled:
        return ()
    from .document_catalog import update_document_catalog_source

    sources: tuple[tuple[Path, "SourceKind"], ...]
    if source_kind == "pdf":
        sources = ((context.config.pdf_database, "pdf"),)
    elif source_kind == "docx":
        sources = ((context.config.docx_database, "docx"),)
    elif source_kind == "audio":
        sources = ((context.config.audio_database, "audio"),)
    elif source_kind == "text":
        sources = ((context.config.text_database, "text"),)
    else:
        sources = (
            (context.config.office_database, "xlsx"),
            (context.config.office_database, "pptx"),
            (context.config.office_database, "odt"),
        )
    phase_name = "catalog"
    begin_phase = getattr(context.framework_state, "begin_route_phase", None)
    complete_phase = getattr(context.framework_state, "complete_route_phase", None)
    fail_phase = getattr(context.framework_state, "fail_route_phase", None)
    if begin_phase is not None:
        begin_phase(
            context.run_id,
            source_kind,
            phase_name,
            source_run_id=context.config.resume_run_id,
        )
    try:
        summaries = tuple(
            update_document_catalog_source(
                context.config.document_catalog_database,
                source_path,
                document_kind,
                framework_run_id=context.run_id,
                taxonomy_path=context.config.document_taxonomy_path,
                max_text_chars=context.config.document_classification_max_chars,
                verify_source_paths=False,
                progress=context.progress,
                progress_operation=source_kind,
                cancellation=context.cancellation,
            )
            for source_path, document_kind in sources
        )
    except BaseException as exc:
        if fail_phase is not None:
            fail_phase(context.run_id, source_kind, phase_name, exc)
        raise
    if complete_phase is not None:
        complete_phase(
            context.run_id,
            source_kind,
            phase_name,
            {"sources": [asdict(summary) for summary in summaries]},
        )
    context.framework_state.record_event(
        context.run_id,
        "info" if not any(summary.errors for summary in summaries) else "warning",
        f"{source_kind}-catalog",
        "Catálogo técnico actualizado",
        {"sources": [asdict(summary) for summary in summaries]},
    )
    return summaries


def _summary_with_catalog(
    summary: Any,
    catalogs: tuple["CatalogUpdateSummary", ...],
) -> Any:
    return replace(
        summary,
        catalog_candidates=sum(catalog.candidates for catalog in catalogs),
        catalog_classified=sum(catalog.classified for catalog in catalogs),
        catalog_cache_hits=sum(catalog.cache_hits for catalog in catalogs),
        catalog_review_required=sum(catalog.review_required for catalog in catalogs),
        catalog_errors=sum(catalog.errors for catalog in catalogs),
        catalog_source_stale=sum(catalog.source_stale for catalog in catalogs),
        catalog_stale_marked=sum(catalog.stale_marked for catalog in catalogs),
    )


def builtin_route_registry() -> dict[str, RouteAdapter]:
    adapters = (
        RouteAdapter("pdf", _run_pdf),
        RouteAdapter("docx", _run_docx),
        RouteAdapter("office", _run_office),
        RouteAdapter("archive", _run_archive),
        RouteAdapter("text", _run_text),
        RouteAdapter("audio", _run_audio),
        RouteAdapter("video", _run_video),
        RouteAdapter("image", _run_image),
        RouteAdapter("code", _run_code, input_source="inventory_snapshot"),
    )
    return {adapter.name: adapter for adapter in adapters}


# endregion [03]
