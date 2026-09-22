"""Extensible route registry and built-in content-route adapters."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, is_dataclass, replace
import heapq
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, TYPE_CHECKING, cast

from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER as BUILTIN_ROUTE_ORDER,
)
from neocortex.runtime.orchestration.route_selection import (
    normalize_route_selection as normalize_route_selection,
)
from neocortex.runtime.orchestration.replay_metrics import (
    normalize_route_replay_metrics,
)
from neocortex.runtime.orchestration.dedup_owner import dedup_owner_lock

if TYPE_CHECKING:
    from neocortex.progress import ProgressCallback

    from neocortex.capabilities.formats.audio.models import AudioRouteConfig as AudioRouteConfig
    from neocortex.capabilities.formats.audio.route import AudioRoute as AudioRoute
    from neocortex.runtime.control.cancellation import CancellationToken
    from neocortex.capabilities.formats.docx.route import DocxRoute as DocxRoute
    from neocortex.capabilities.formats.docx.route import DocxRouteConfig as DocxRouteConfig
    from neocortex.documents.document_catalog import CatalogUpdateSummary, SourceKind
    from neocortex.runtime.control.global_resources import GlobalResourceCoordinator
    from neocortex.capabilities.formats.image.route import ImageRoute as ImageRoute
    from neocortex.capabilities.formats.image.route import ImageRouteConfig as ImageRouteConfig
    from neocortex.runtime.models import FrameworkConfig
    from neocortex.capabilities.formats.office.route import OfficeRoute as OfficeRoute
    from neocortex.capabilities.formats.office.route import OfficeRouteConfig as OfficeRouteConfig
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute as PdfRoute
    from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteConfig as PdfRouteConfig
    from neocortex.persistence.framework_route_state import FrameworkRouteState
    from neocortex.capabilities.formats.text.text_route import TextRoute as TextRoute
    from neocortex.capabilities.formats.text.text_route import TextRouteConfig as TextRouteConfig
    from neocortex.capabilities.formats.video.route import VideoRoute as VideoRoute
    from neocortex.capabilities.formats.video.route import VideoRouteConfig as VideoRouteConfig


# region [01] Generic route contracts and selection exports

RouteLifecycleCapability = Literal["phase_resume", "safe_replay", "not_resumable"]
RouteWorkload = tuple[int, int]


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
    source_published: Callable[[str], None] | None = None
    # The field is optional for legacy route doubles.  Built-in adapters use
    # the FrameworkConfig value through ``effective_route_config`` below so a
    # direct route invocation cannot widen the global ceiling.
    max_file_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class RouteAdapter:
    name: str
    execute: Callable[[RouteExecutionContext], object]
    input_source: Literal["route_candidates", "inventory_snapshot"] = "route_candidates"
    lifecycle_capability: RouteLifecycleCapability = "safe_replay"
    # Dependencies are ordering hints for the route scheduler.  They do not
    # turn an optional producer into a hard availability gate: if the
    # dependency is selected, its stage is drained first; a dependent route
    # still runs when that producer reports a typed failure.
    depends_on: tuple[str, ...] = ()
    estimate_workload: Callable[[RouteExecutionContext], RouteWorkload] | None = None

    def __post_init__(self) -> None:
        if self.lifecycle_capability not in {
            "phase_resume",
            "safe_replay",
            "not_resumable",
        }:
            raise ValueError(f"unsupported lifecycle capability: {self.lifecycle_capability}")
        if self.input_source not in {"route_candidates", "inventory_snapshot"}:
            raise ValueError(f"unsupported route input source: {self.input_source}")
        if not isinstance(self.depends_on, tuple):
            raise TypeError("route dependencies must be an immutable tuple")
        if any(not isinstance(name, str) or not name.strip() for name in self.depends_on):
            raise ValueError("route dependencies must be non-empty names")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("route dependencies cannot repeat")
        if self.estimate_workload is not None and not callable(self.estimate_workload):
            raise TypeError("route workload estimator must be callable")

    def summary_mapping(self, summary: object) -> Mapping[str, Any]:
        if is_dataclass(summary) and not isinstance(summary, type):
            mapping = asdict(summary)
        elif isinstance(summary, Mapping):
            mapping = dict(summary)
        else:
            raise TypeError(
                f"route {self.name} returned a non-serializable summary: {type(summary).__name__}"
            )
        return normalize_route_replay_metrics(
            self.name,
            mapping,
            replayability=self.lifecycle_capability,
        )

# region [01b] Bounded route workload projections


def _bounded_workload(
    values: Iterable[int],
    limit: int | None,
) -> RouteWorkload:
    """Return item/byte work while bounding retained candidate-size state."""

    eligible = 0
    total_bytes = 0
    largest: list[int] = []
    for value in values:
        eligible += 1
        if limit is None:
            total_bytes += value
            continue
        if limit <= 0:
            continue
        selected = min(eligible, limit)
        # Route limits are user-configurable.  Keep only the largest selected
        # values so the byte reservation cannot understate a route that chooses
        # a different deterministic subset, while memory remains proportional
        # to the bounded route limit rather than to the whole corpus.
        if len(largest) < selected:
            heapq.heappush(largest, value)
        elif value > largest[0]:
            heapq.heapreplace(largest, value)
    selected = eligible if limit is None else min(eligible, limit)
    return selected, total_bytes if limit is None else sum(largest)


def _candidate_route_workload(
    context: RouteExecutionContext,
    route_name: str,
) -> RouteWorkload:
    """Estimate selected route input work from the immutable candidate view.

    The old orchestration reservation counted every row in the shared routing
    snapshot for every route.  This projection applies the route's MIME
    contract, framework-visible selection predicates, size limit and count
    limit before reserving.  Owner-specific status/error filters remain the
    route's responsibility; their candidates are deliberately retained here
    rather than guessed away.
    """

    checkpoint = getattr(context.cancellation, "checkpoint", None)
    if callable(checkpoint):
        checkpoint()
    candidate_database = getattr(context.framework_state, "candidate_database", None)
    if candidate_database is None:
        # Legacy state doubles do not expose a detached candidate view.  The
        # caller keeps its compatibility fallback for those adapters.
        return (0, 0)

    from neocortex.platform.content_capability_manifest import content_capability_by_id

    capability = content_capability_by_id(route_name)
    selection = context.config.selection
    max_file_bytes = effective_route_max_file_bytes(context.config, route_name)
    max_documents = getattr(context.config, f"{route_name}_max_documents", None)
    if max_documents is not None and (type(max_documents) is not int or max_documents < 1):
        raise ValueError(f"{route_name} max documents is invalid")

    def candidate_sizes() -> Iterable[int]:
        prior_selectors: list[str] = []
        for mime in capability.mime_types:
            if callable(checkpoint):
                checkpoint()
            if mime.endswith("/"):
                rows = context.framework_state.iter_selected_route_candidates_by_prefix(
                    context.run_id, mime, route_name, selection,
                )
                candidates = rows
            else:
                snapshots = context.framework_state.iter_selected_route_candidates(
                    context.run_id, mime, route_name, selection,
                )
                candidates = ((mime, snapshot) for snapshot in snapshots)
            # The owner has one MIME per unique candidate path. An earlier
            # selector already consumed every matching row, so precedence can
            # remove overlaps without retaining corpus-sized path sets. Keep
            # MIME order and the conservative largest-N byte bound remain
            # identical to the previous projection.
            for observed_mime, snapshot in candidates:
                if callable(checkpoint):
                    checkpoint()
                if any(
                    observed_mime.startswith(previous) if previous.endswith("/")
                    else observed_mime == previous
                    for previous in prior_selectors
                ):
                    continue
                if max_file_bytes is not None and not _size_is_admitted(
                    snapshot.size, max_file_bytes
                ):
                    continue
                yield max(0, int(snapshot.size))
            prior_selectors.append(mime)

    workload = _bounded_workload(candidate_sizes(), max_documents)
    # The source may observe cancellation while finishing its last page,
    # including a page whose rows were all filtered out of the workload.
    if callable(checkpoint):
        checkpoint()
    return workload


def _candidate_workload_estimator(route_name: str) -> Callable[[RouteExecutionContext], RouteWorkload]:
    return lambda context: _candidate_route_workload(context, route_name)


def _validated_route_limit(value: object, *, name: str) -> int | None:
    """Validate only configured limits, preserving lazy route imports."""

    if value is None:
        return None
    # Keep the route registry import-light: most direct route calls have no
    # global/local limit and must not import the dedup package merely to
    # construct an adapter.  Configured values use the canonical validator.
    from neocortex.deduplication.admission import validate_max_file_bytes

    try:
        return validate_max_file_bytes(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"{name} is invalid") from exc


def _size_is_admitted(size: int, max_file_bytes: int) -> bool:
    from neocortex.deduplication.admission import size_is_admitted

    return size_is_admitted(size, max_file_bytes)


def effective_route_max_file_bytes(config: "FrameworkConfig", route_name: str) -> int | None:
    """Return ``min(global, route-specific)`` for one built-in route.

    The global option is a run admission ceiling, not a replacement for the
    existing route-local safety limit.  Keeping the combination at this
    projection boundary means direct ``--route`` calls and ``--all`` share the
    same contract, while each route owner continues to receive its ordinary
    config field.
    """

    global_limit = _validated_route_limit(
        getattr(config, "max_file_bytes", None), name="global max_file_bytes"
    )
    route_limit = _validated_route_limit(
        getattr(config, f"{route_name}_max_file_bytes", None),
        name=f"{route_name} max_file_bytes",
    )
    if global_limit is None:
        return route_limit
    if route_limit is None:
        return global_limit
    return min(global_limit, route_limit)


def effective_route_config(config: "FrameworkConfig", route_name: str) -> "FrameworkConfig":
    """Project a route config with its effective global/local size ceiling."""

    field_name = f"{route_name}_max_file_bytes"
    effective = effective_route_max_file_bytes(config, route_name)
    if getattr(config, field_name, None) == effective:
        return config
    # FrameworkConfig is frozen, but keeping this replacement at the
    # orchestration boundary avoids mutating the shared application config.
    return cast("FrameworkConfig", replace(cast(Any, config), **{field_name: effective}))


# endregion [01b]


# endregion [01]


# region [02] Built-in route adapters


def pdf_route_config_from_framework(config: "FrameworkConfig") -> "PdfRouteConfig":
    """Preserve the route-registry projection boundary for PDF execution."""

    from neocortex.runtime.config.application_config_projections import (
        pdf_route_config_from_application,
    )

    return pdf_route_config_from_application(effective_route_config(config, "pdf"))


def _run_pdf(context: RouteExecutionContext) -> object:
    from neocortex.deduplication import DedupIndex
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute

    config = effective_route_config(context.config, "pdf")
    # PDF and image both own the shared inventory fingerprint cache.  Keep the
    # complete DedupIndex lifetime under the per-path lease: locking only the
    # constructor would let the second schema probe see the first owner's WAL
    # and request a full temporary snapshot of a potentially multi-gigabyte
    # inventory database.
    cancellation_check = getattr(context.cancellation, "checkpoint", None)
    with dedup_owner_lock(
        config.dedup_database,
        cancellation_check=cancellation_check if callable(cancellation_check) else None,
    ):
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

    from neocortex.runtime.config.application_config_projections import (
        image_route_config_from_application,
    )

    return image_route_config_from_application(
        effective_route_config(config, "image"), root=root
    )


def _run_image(context: RouteExecutionContext) -> object:
    from neocortex.deduplication import DedupIndex

    from neocortex.runtime.control.cancellation import CancellationToken
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate

    from neocortex.capabilities.formats.image.route import ImageRoute

    config = effective_route_config(context.config, "image")
    # A fatal Image admission stops only its own executor. Framework retains
    # the original route error and decides how sibling routes should proceed.
    cancellation = CancellationToken(parent=context.cancellation)
    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(
            context.resource_coordinator, "image", cancellation=cancellation
        )
    )
    # Image has a route-local child token so a local admission failure also
    # cancels a queued owner before it can construct ``DedupIndex``.  Keep the
    # compatibility fallback for lightweight route doubles without a real
    # parent token.
    cancellation_check = (
        getattr(cancellation, "checkpoint", None)
        if callable(getattr(context.cancellation, "checkpoint", None))
        else None
    )
    with dedup_owner_lock(
        config.dedup_database,
        cancellation_check=cancellation_check if callable(cancellation_check) else None,
    ):
        with DedupIndex(config.dedup_database) as dedup_index:
            summary = ImageRoute(
                image_route_config_from_framework(config, root=context.root),
                context.framework_state,
                context.run_id,
                progress=context.progress,
                memory_gate=gate,
                cancellation=cancellation,
                dedup_index=dedup_index,
            ).run()
    catalog = _update_document_catalog_after_route(context, "image")
    if catalog:
        summary = _summary_with_catalog(summary, catalog)
    return summary


def docx_route_config_from_framework(config: "FrameworkConfig") -> "DocxRouteConfig":
    """Preserve the route-registry projection boundary for DOCX execution."""

    from neocortex.runtime.config.application_config_projections import (
        docx_route_config_from_application,
    )

    return docx_route_config_from_application(effective_route_config(config, "docx"))


def _run_docx(context: RouteExecutionContext) -> object:
    from neocortex.capabilities.formats.docx.route import DocxRoute
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate

    config = effective_route_config(context.config, "docx")
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

    from neocortex.runtime.config.application_config_projections import (
        office_route_config_from_application,
    )

    return office_route_config_from_application(effective_route_config(config, "office"))


def _run_office(context: RouteExecutionContext) -> object:
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate
    from neocortex.capabilities.formats.office.route import OfficeRoute

    config = effective_route_config(context.config, "office")
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


def text_route_config_from_framework(config: "FrameworkConfig") -> "TextRouteConfig":
    """Project application limits into generic text extraction."""

    from neocortex.runtime.config.application_config_projections import (
        text_route_config_from_application,
    )

    return text_route_config_from_application(effective_route_config(config, "text"))


def _run_text(context: RouteExecutionContext) -> object:
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate
    from neocortex.capabilities.formats.text.text_route import TextRoute

    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "text")
    )
    config = effective_route_config(context.config, "text")
    summary = TextRoute(
        text_route_config_from_framework(config),
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

    from neocortex.runtime.config.application_config_projections import (
        audio_route_config_from_application,
    )

    return audio_route_config_from_application(effective_route_config(config, "audio"))


def _run_audio(context: RouteExecutionContext) -> object:
    from neocortex.capabilities.formats.audio.route import AudioRoute
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate

    config = effective_route_config(context.config, "audio")
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
    # AudioRoute returned after closing its source writer. Video may now read
    # that complete source while the independent catalog obligation continues.
    if context.source_published is not None:
        context.source_published("audio")
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

    from neocortex.runtime.config.application_config_projections import (
        video_route_config_from_application,
    )

    return video_route_config_from_application(
        effective_route_config(config, "video"), root=root
    )


def _run_video(context: RouteExecutionContext) -> object:
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate
    from neocortex.capabilities.formats.video.route import VideoRoute

    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "video")
    )
    config = effective_route_config(context.config, "video")
    summary = VideoRoute(
        video_route_config_from_framework(config, root=context.root),
        context.framework_state,
        context.run_id,
        progress=context.progress,
        memory_gate=gate,
        cancellation=context.cancellation,
    ).run()
    catalog = _update_document_catalog_after_route(context, "video")
    if catalog:
        summary = _summary_with_catalog(summary, catalog)
    return summary


def _update_document_catalog_after_route(
    context: RouteExecutionContext,
    source_kind: Literal[
        "pdf",
        "docx",
        "office",
        "text",
        "audio",
        "image",
        "video",
    ],
) -> "tuple[CatalogUpdateSummary, ...]":
    """Classify only the source cache completed by this route."""

    if not context.config.document_catalog_enabled:
        return ()
    from neocortex.documents.document_catalog import (
        CatalogUpdateSummary,
        update_document_catalog_source,
    )
    from neocortex.runtime.control.cancellation import CancellationRequested

    sources: tuple[tuple[Path, "SourceKind"], ...]
    if source_kind == "pdf":
        sources = ((context.config.pdf_database, "pdf"),)
    elif source_kind == "docx":
        sources = ((context.config.docx_database, "docx"),)
    elif source_kind == "audio":
        sources = ((context.config.audio_database, "audio"),)
    elif source_kind == "text":
        sources = ((context.config.text_database, "text"),)
    elif source_kind == "image":
        sources = ((context.config.image_database, "image"),)
    elif source_kind == "video":
        sources = ((context.config.video_database, "video"),)
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
    summaries: list[CatalogUpdateSummary] = []
    failures: list[tuple["SourceKind", Exception]] = []
    # The catalog owner serializes its write/CAS boundaries and updates from
    # the same source.  Keep producer-route success independent from this
    # optional consumer: a source-reader failure belongs to the catalog phase,
    # while the route's durable extraction remains useful and replayable.
    for source_path, document_kind in sources:
        try:
            summaries.append(
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
                    source_root=context.root,
                )
            )
        except CancellationRequested as exc:
            # Cancellation is a run-wide control signal, not a recoverable
            # catalog partial.  Preserve the existing lifecycle contract.
            if fail_phase is not None:
                fail_phase(context.run_id, source_kind, phase_name, exc)
            raise
        except Exception as exc:
            failures.append((document_kind, exc))
            # No fabricated publication is exposed.  ``0`` is an explicit
            # non-durable sentinel used only in this in-memory route summary;
            # the failed catalog run/generation retains its real operational
            # identity in the catalog owner itself.
            run_id = getattr(exc, "catalog_run_id", 0)
            summaries.append(
                CatalogUpdateSummary(
                    catalog_run_id=run_id if type(run_id) is int and run_id > 0 else 0,
                    source_kind=document_kind,
                    errors=1,
                    publication_state="unavailable",
                )
            )

    summary_payload = {"sources": [asdict(summary) for summary in summaries]}
    if failures:
        primary = failures[0][1]
        if fail_phase is not None:
            fail_phase(context.run_id, source_kind, phase_name, primary)
        summary_payload["failures"] = [
            {
                "source_kind": document_kind,
                "error_type": type(exc).__name__,
                "detail": str(exc)[:8192],
            }
            for document_kind, exc in failures
        ]
        context.framework_state.record_event(
            context.run_id,
            "warning",
            f"{source_kind}-catalog",
            "Catálogo técnico no disponible; productor conservado",
            summary_payload,
        )
    else:
        if complete_phase is not None:
            complete_phase(context.run_id, source_kind, phase_name, summary_payload)
        catalog_attention = any(
            summary.errors
            or summary.review_required
            or summary.source_stale
            or summary.source_missing
            for summary in summaries
        )
        context.framework_state.record_event(
            context.run_id,
            "warning" if catalog_attention else "info",
            f"{source_kind}-catalog",
            "Catálogo técnico actualizado",
            summary_payload,
        )
    return tuple(summaries)


def _summary_with_catalog(
    summary: Any,
    catalogs: tuple["CatalogUpdateSummary", ...],
) -> Any:
    # The original five route summaries expose catalog counters.  The
    # multimodal summaries intentionally keep their own public contracts, so
    # catalog integration must not force fields into them with dataclasses.replace.
    # If a future/additive summary declares the counters, retain the projection.
    if not is_dataclass(summary) or isinstance(summary, type):
        return summary
    summary_fields = getattr(type(summary), "__dataclass_fields__", {})
    route_candidates = getattr(summary, "candidates", None)
    has_route_candidates = type(route_candidates) is int and route_candidates > 0
    source_missing = any(catalog.source_missing for catalog in catalogs)
    # A route with no candidates has no catalog work to lose.  Keep the
    # source-missing flag useful for real work only, so empty optional owners
    # do not turn a harmless no-op into a strict failure.
    missing_with_work = source_missing and has_route_candidates
    effective_source_missing = int(missing_with_work)
    # ``review_required`` is advisory classification state, not a catalog
    # publication error; errors and stale source observations are the strict
    # incomplete cases here.
    catalog_complete = not (
        missing_with_work
        or any(catalog.errors or catalog.source_stale for catalog in catalogs)
    )
    updates = {
        field: value
        for field, value in {
            "catalog_candidates": sum(catalog.candidates for catalog in catalogs),
            "catalog_classified": sum(catalog.classified for catalog in catalogs),
            "catalog_cache_hits": sum(catalog.cache_hits for catalog in catalogs),
            "catalog_review_required": sum(catalog.review_required for catalog in catalogs),
            "catalog_errors": sum(catalog.errors for catalog in catalogs),
            "catalog_source_stale": sum(catalog.source_stale for catalog in catalogs),
            "catalog_stale_marked": sum(catalog.stale_marked for catalog in catalogs),
            "catalog_source_missing": effective_source_missing,
            "catalog_complete": catalog_complete,
        }.items()
        if field in summary_fields
    }
    return summary if not updates else replace(summary, **updates)


def builtin_route_registry() -> dict[str, RouteAdapter]:
    adapters = (
        RouteAdapter(
            "pdf",
            _run_pdf,
            lifecycle_capability="phase_resume",
            estimate_workload=_candidate_workload_estimator("pdf"),
        ),
        RouteAdapter(
            "docx",
            _run_docx,
            estimate_workload=_candidate_workload_estimator("docx"),
        ),
        RouteAdapter(
            "office",
            _run_office,
            estimate_workload=_candidate_workload_estimator("office"),
        ),
        RouteAdapter(
            "text",
            _run_text,
            estimate_workload=_candidate_workload_estimator("text"),
        ),
        RouteAdapter(
            "audio",
            _run_audio,
            estimate_workload=_candidate_workload_estimator("audio"),
        ),
        RouteAdapter(
            "video",
            _run_video,
            depends_on=("audio",),
            estimate_workload=_candidate_workload_estimator("video"),
        ),
        RouteAdapter(
            "image",
            _run_image,
            estimate_workload=_candidate_workload_estimator("image"),
        ),
    )
    return {adapter.name: adapter for adapter in adapters}
# endregion [02]
