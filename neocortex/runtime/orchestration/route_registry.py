"""Extensible route registry and built-in content-route adapters."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, is_dataclass, replace
import heapq
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Literal, Mapping, Protocol, TYPE_CHECKING

from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER as BUILTIN_ROUTE_ORDER,
)
from neocortex.runtime.orchestration.route_selection import (
    normalize_route_selection as normalize_route_selection,
)
from neocortex.runtime.orchestration.replay_metrics import (
    normalize_route_replay_metrics,
)

if TYPE_CHECKING:
    from neocortex.progress import ProgressCallback

    from neocortex.capabilities.formats.archive.route import ArchiveRoute as ArchiveRoute
    from neocortex.capabilities.formats.archive.route import ArchiveRouteConfig as ArchiveRouteConfig
    from neocortex.capabilities.formats.audio.models import AudioRouteConfig as AudioRouteConfig
    from neocortex.capabilities.formats.audio.route import AudioRoute as AudioRoute
    from neocortex.code.code_contracts import CodeRouteConfig as CodeRouteConfig
    from neocortex.code.code_route import CodeRoute as CodeRoute
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
    from neocortex.code.code_route import CodeInventory
    from neocortex.deduplication import FileSnapshot


# region [01] Generic route contracts and selection exports

RouteLifecycleCapability = Literal["phase_resume", "safe_replay", "not_resumable"]
RouteWorkload = tuple[int, int]


class _InventorySnapshotSource(Protocol):
    """Minimal inventory owner/projection seam used by the Code route."""

    def snapshots(self, scan_id: int) -> Iterable[FileSnapshot]: ...

# Route workers run concurrently, while all source kinds publish into the
# same document-catalog owner.  Keep extraction parallel and serialize only
# the catalog generation/CAS boundary; the catalog module's writer lock is a
# second defense for callers outside orchestration, not the lifecycle gate.
_CATALOG_UPDATE_LOCK = RLock()


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
    # Normal --all runs may provide a bounded projection built by the already
    # open inventory owner.  Keeping it optional preserves route-only and
    # legacy test adapters, while avoiding a second WAL-backed DedupIndex.
    inventory_view: "CodeInventory | None" = None


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


@dataclass(frozen=True, slots=True)
class CodeInventoryProjection:
    """Bounded immutable Code input copied from the active inventory owner.

    The projection contains only paths that the Code route can admit before
    reading bytes.  It is intentionally ephemeral and in-memory: the owning
    orchestration scope controls its lifetime and no corpus or inventory
    database is rewritten.
    """

    records: tuple[FileSnapshot, ...]

    def snapshots(self, scan_id: int) -> Iterable[FileSnapshot]:
        if type(scan_id) is not int or scan_id <= 0:
            raise ValueError("Code inventory projection scan_id must be positive")
        yield from self.records


def _project_roots_relevant_to_corpus(
    corpus_root: Path,
    project_roots: Iterable[Path],
) -> tuple[Path, ...]:
    """Keep allowlist roots that overlap the selected corpus lexically.

    The default personal project roots normally live outside the controlled
    corpus.  Treating those unrelated paths as an always-nonempty allowlist
    would suppress marker discovery for an intentionally copied project.  An
    explicitly overlapping root remains authoritative; no filesystem walk or
    symlink resolution is performed here.
    """

    corpus = Path(corpus_root).absolute()
    relevant: list[Path] = []
    for root in project_roots:
        candidate = Path(root).absolute()
        try:
            corpus.relative_to(candidate)
        except ValueError:
            try:
                candidate.relative_to(corpus)
            except ValueError:
                continue
        relevant.append(candidate)
    return tuple(relevant)


def build_code_inventory_projection(
    index: _InventorySnapshotSource,
    scan_id: int,
    *,
    cancellation: object | None = None,
) -> CodeInventoryProjection:
    """Materialize the Code-admissible inventory rows from an open owner.

    ``DedupIndex`` remains the sole reader of the live WAL-backed owner.  The
    route workers consume this detached tuple, so no worker needs to run the
    inventory schema validator or create a large temporary SQLite snapshot.
    """

    from neocortex.code.ingestion.code_candidate_scope import is_project_marker
    from neocortex.code.ingestion.code_detection import likely_code_candidate
    from neocortex.deduplication import FileSnapshot

    checkpoint = getattr(cancellation, "checkpoint", None)
    records: list[FileSnapshot] = []
    for snapshot in index.snapshots(scan_id):
        if callable(checkpoint):
            checkpoint()
        if not isinstance(snapshot, FileSnapshot):
            raise TypeError("inventory owner returned an invalid FileSnapshot")
        # Keep this projection semantically identical to CodeRoute's first
        # admission predicate.  Project-scope discovery needs marker files;
        # arbitrary non-code inventory rows cannot affect either pass.
        if likely_code_candidate(snapshot.path) or is_project_marker(snapshot.path):
            records.append(snapshot)
    return CodeInventoryProjection(tuple(records))


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

    candidate_database = getattr(context.framework_state, "candidate_database", None)
    if candidate_database is None:
        # Legacy state doubles do not expose a detached candidate view.  The
        # caller keeps its compatibility fallback for those adapters.
        return (0, 0)

    from neocortex.platform.content_capability_manifest import content_capability_by_id

    capability = content_capability_by_id(route_name)
    selection = context.config.selection
    max_file_bytes = getattr(context.config, f"{route_name}_max_file_bytes", None)
    max_documents = getattr(context.config, f"{route_name}_max_documents", None)
    if max_documents is not None and (type(max_documents) is not int or max_documents < 1):
        raise ValueError(f"{route_name} max documents is invalid")

    def candidate_sizes() -> Iterable[int]:
        seen_paths: set[str] = set()
        for mime in capability.mime_types:
            if mime.endswith("/"):
                rows = context.framework_state.iter_selected_route_candidates_by_prefix(
                    context.run_id,
                    mime,
                    route_name,
                    selection,
                )
                iterator = (snapshot for _observed_mime, snapshot in rows)
            else:
                iterator = context.framework_state.iter_selected_route_candidates(
                    context.run_id,
                    mime,
                    route_name,
                    selection,
                )
            for snapshot in iterator:
                if snapshot.path in seen_paths:
                    continue
                seen_paths.add(snapshot.path)
                if max_file_bytes is not None and snapshot.size > max_file_bytes:
                    continue
                yield max(0, int(snapshot.size))

    return _bounded_workload(candidate_sizes(), max_documents)


def _code_route_workload(context: RouteExecutionContext) -> RouteWorkload:
    """Bound Code work from the durable inventory, not route candidates."""

    from neocortex.code.ingestion.code_candidate_scope import (
        ProjectCandidateScope,
        is_project_marker,
    )
    from neocortex.code.ingestion.code_detection import likely_code_candidate
    config = context.config
    selected_paths = {
        str(Path(value).expanduser().absolute()) for value in config.selection.paths
    }
    project_roots = _project_roots_relevant_to_corpus(
        context.root,
        config.code_project_roots,
    )

    def estimate(index: _InventorySnapshotSource) -> RouteWorkload:
        project_scope = None
        if config.code_candidate_scope == "projects" and not selected_paths:
            project_scope = ProjectCandidateScope.discover(
                (snapshot.path for snapshot in index.snapshots(context.scan_id)),
                include_generated=config.code_include_generated,
                include_vendored=config.code_include_vendored,
                explicit_roots=project_roots,
            )

        def code_sizes() -> Iterable[int]:
            for snapshot in index.snapshots(context.scan_id):
                if selected_paths and str(Path(snapshot.path).absolute()) not in selected_paths:
                    continue
                if not likely_code_candidate(snapshot.path) and not is_project_marker(snapshot.path):
                    continue
                if project_scope is not None and project_scope.decision(snapshot.path) != "admit":
                    continue
                if snapshot.size > config.code_max_file_bytes:
                    # Code records this as a bounded skip, not a candidate that
                    # enters analysis; do not reserve it as route work.
                    continue
                yield max(0, int(snapshot.size))

        return _bounded_workload(code_sizes(), config.code_max_documents)

    projected = context.inventory_view
    if projected is not None:
        return estimate(projected)

    # Route-only and legacy callers may not have an owner-provided projection.
    # Preserve their historical behavior; normal --all runs always inject the
    # bounded view before workers are created.
    from neocortex.deduplication import DedupIndex

    with DedupIndex(config.dedup_database) as index:
        return estimate(index)


def _candidate_workload_estimator(route_name: str) -> Callable[[RouteExecutionContext], RouteWorkload]:
    return lambda context: _candidate_route_workload(context, route_name)


# endregion [01b]


# endregion [01]


# region [02] Built-in route adapters


def pdf_route_config_from_framework(config: "FrameworkConfig") -> "PdfRouteConfig":
    """Preserve the route-registry projection boundary for PDF execution."""

    from neocortex.runtime.config.application_config_projections import (
        pdf_route_config_from_application,
    )

    return pdf_route_config_from_application(config)


def _run_pdf(context: RouteExecutionContext) -> object:
    from neocortex.deduplication import DedupIndex
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute

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

    from neocortex.runtime.config.application_config_projections import (
        image_route_config_from_application,
    )

    return image_route_config_from_application(config, root=root)


def _run_image(context: RouteExecutionContext) -> object:
    from neocortex.deduplication import DedupIndex

    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate

    from neocortex.capabilities.formats.image.route import ImageRoute

    config = context.config
    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "image")
    )
    with DedupIndex(config.dedup_database) as dedup_index:
        summary = ImageRoute(
            image_route_config_from_framework(config, root=context.root),
            context.framework_state,
            context.run_id,
            progress=context.progress,
            memory_gate=gate,
            cancellation=context.cancellation,
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

    return docx_route_config_from_application(config)


def _run_docx(context: RouteExecutionContext) -> object:
    from neocortex.capabilities.formats.docx.route import DocxRoute
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate

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

    from neocortex.runtime.config.application_config_projections import (
        office_route_config_from_application,
    )

    return office_route_config_from_application(config)


def _run_office(context: RouteExecutionContext) -> object:
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate
    from neocortex.capabilities.formats.office.route import OfficeRoute

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

    from neocortex.runtime.config.application_config_projections import (
        archive_route_config_from_application,
    )

    return archive_route_config_from_application(config)


def _run_archive(context: RouteExecutionContext) -> object:
    from neocortex.capabilities.formats.archive.route import ArchiveRoute

    gate = None
    if context.resource_coordinator is not None:
        from neocortex.runtime.control.global_resources import CoordinatedMemoryGate

        gate = CoordinatedMemoryGate(context.resource_coordinator, "archive")
    summary = ArchiveRoute(
        archive_route_config_from_framework(context.config),
        context.framework_state,
        context.run_id,
        progress=context.progress,
        memory_gate=gate,
        cancellation=context.cancellation,
    ).run()
    catalog = _update_document_catalog_after_route(context, "archive")
    if catalog:
        summary = _summary_with_catalog(summary, catalog)
    return summary


def text_route_config_from_framework(config: "FrameworkConfig") -> "TextRouteConfig":
    """Project application limits into generic text extraction."""

    from neocortex.runtime.config.application_config_projections import (
        text_route_config_from_application,
    )

    return text_route_config_from_application(config)


def _run_text(context: RouteExecutionContext) -> object:
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate
    from neocortex.capabilities.formats.text.text_route import TextRoute

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

    from neocortex.runtime.config.application_config_projections import (
        audio_route_config_from_application,
    )

    return audio_route_config_from_application(config)


def _run_audio(context: RouteExecutionContext) -> object:
    from neocortex.capabilities.formats.audio.route import AudioRoute
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate

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

    from neocortex.runtime.config.application_config_projections import (
        video_route_config_from_application,
    )

    return video_route_config_from_application(config, root=root)


def _run_video(context: RouteExecutionContext) -> object:
    from neocortex.runtime.control.global_resources import CoordinatedMemoryGate
    from neocortex.capabilities.formats.video.route import VideoRoute

    gate = (
        None
        if context.resource_coordinator is None
        else CoordinatedMemoryGate(context.resource_coordinator, "video")
    )
    summary = VideoRoute(
        video_route_config_from_framework(context.config, root=context.root),
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


def code_route_config_from_framework(config: "FrameworkConfig") -> "CodeRouteConfig":
    """Project application values into the canonical Code route contract."""

    from neocortex.runtime.config.application_config_projections import (
        code_route_config_from_application,
    )

    return code_route_config_from_application(config)


def _run_code(context: RouteExecutionContext) -> object:
    from neocortex.code.code_route import CodeRoute

    config = context.config
    code_config = code_route_config_from_framework(config)
    # Keep lightweight route-config test doubles and legacy adapters working;
    # the canonical CodeRouteConfig always exposes these fields.
    if getattr(code_config, "candidate_scope", None) == "projects":
        relevant_roots = _project_roots_relevant_to_corpus(
            context.root,
            getattr(code_config, "explicit_project_roots", ()),
        )
        if relevant_roots != getattr(code_config, "explicit_project_roots", ()):
            code_config = replace(code_config, explicit_project_roots=relevant_roots)
    gate = None
    if context.resource_coordinator is not None:
        from neocortex.runtime.control.global_resources import CoordinatedMemoryGate

        gate = CoordinatedMemoryGate(context.resource_coordinator, "code")
    inventory_view = context.inventory_view
    if inventory_view is not None:
        summary = CodeRoute(
            code_config,
            inventory_view,
            context.framework_state,
            context.run_id,
            context.scan_id,
            progress=context.progress,
            cancellation=context.cancellation,
            memory_gate=gate,
        ).run()
    else:
        # Route-only and legacy callers may not have the owner-provided
        # projection.  Keep their existing fallback while normal --all avoids
        # reopening a WAL-backed inventory database from a worker.
        from neocortex.deduplication import DedupIndex

        with DedupIndex(config.dedup_database) as dedup_index:
            summary = CodeRoute(
                code_config,
                dedup_index,
                context.framework_state,
                context.run_id,
                context.scan_id,
                progress=context.progress,
                cancellation=context.cancellation,
                memory_gate=gate,
            ).run()
    catalog = _update_document_catalog_after_route(context, "code")
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
        "archive",
        "image",
        "video",
        "code",
    ],
) -> "tuple[CatalogUpdateSummary, ...]":
    """Classify only the source cache completed by this route."""

    if not context.config.document_catalog_enabled:
        return ()
    from neocortex.documents.document_catalog import update_document_catalog_source

    sources: tuple[tuple[Path, "SourceKind"], ...]
    if source_kind == "pdf":
        sources = ((context.config.pdf_database, "pdf"),)
    elif source_kind == "docx":
        sources = ((context.config.docx_database, "docx"),)
    elif source_kind == "audio":
        sources = ((context.config.audio_database, "audio"),)
    elif source_kind == "text":
        sources = ((context.config.text_database, "text"),)
    elif source_kind == "archive":
        sources = ((context.config.archive_database, "archive"),)
    elif source_kind == "image":
        sources = ((context.config.image_database, "image"),)
    elif source_kind == "video":
        sources = ((context.config.video_database, "video"),)
    elif source_kind == "code":
        sources = ((context.config.code_database, "code"),)
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
        # The lock covers the whole owner publication, not only the SQLite
        # BEGIN IMMEDIATE section.  Otherwise independent route workers could
        # build against the same catalog head concurrently and collide during
        # generation/CAS even though extraction itself should remain parallel.
        with _CATALOG_UPDATE_LOCK:
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
                    source_root=context.root,
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
        {"sources": [asdict(summary) for summary in summaries]},
    )
    return summaries


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
            "archive",
            _run_archive,
            estimate_workload=_candidate_workload_estimator("archive"),
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
        RouteAdapter(
            "code",
            _run_code,
            input_source="inventory_snapshot",
            estimate_workload=_code_route_workload,
        ),
    )
    return {adapter.name: adapter for adapter in adapters}
# endregion [02]
