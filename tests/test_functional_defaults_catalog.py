"""Functional defaults and route-to-catalog integration contracts."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, fields
from pathlib import Path
from types import SimpleNamespace
from typing import TypeAlias

import pytest

from neocortex.capabilities.formats.archive.models import ArchiveRouteSummary
from neocortex.capabilities.formats.audio.models import AudioRouteSummary
from neocortex.capabilities.formats.docx.models import DocxRouteSummary
from neocortex.capabilities.formats.image.contracts import ImageRouteSummary
from neocortex.capabilities.formats.office.models import OfficeRouteSummary
from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteSummary
from neocortex.capabilities.formats.text.text_route import TextRouteSummary
from neocortex.capabilities.formats.video.models import VideoRouteSummary
from neocortex.code.code_contracts import CodeRouteSummary
import neocortex.documents.document_catalog as catalog_module
from neocortex.documents.document_catalog import CatalogUpdateSummary
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration import route_registry
from neocortex.runtime.orchestration.route_registry import (
    RouteExecutionContext,
    builtin_route_registry,
)

RouteSummaryType: TypeAlias = type[object]


def test_functional_defaults_keep_catalog_enabled_and_select_nine_routes() -> None:
    config = FrameworkConfig()

    assert config.document_catalog_enabled
    assert tuple(builtin_route_registry()) == (
        "pdf",
        "docx",
        "office",
        "archive",
        "text",
        "audio",
        "video",
        "image",
        "code",
    )


@pytest.mark.parametrize(
    "summary_type",
    (
        PdfRouteSummary,
        DocxRouteSummary,
        OfficeRouteSummary,
        TextRouteSummary,
        AudioRouteSummary,
        ArchiveRouteSummary,
        ImageRouteSummary,
        VideoRouteSummary,
        CodeRouteSummary,
    ),
)
def test_all_route_summaries_expose_catalog_status_keyword_only(
    summary_type: RouteSummaryType,
) -> None:
    summary_fields = {item.name: item for item in fields(summary_type)}

    assert summary_fields["catalog_source_missing"].kw_only
    assert summary_fields["catalog_complete"].kw_only
    assert summary_fields["catalog_complete"].default is None
    assert summary_fields["catalog_source_missing"].default == 0


@pytest.mark.parametrize(
    "summary_type",
    (ArchiveRouteSummary, ImageRouteSummary, VideoRouteSummary, CodeRouteSummary),
)
def test_multimodal_summaries_add_catalog_counters_without_shifting_positionals(
    summary_type: RouteSummaryType,
) -> None:
    summary_fields = {item.name: item for item in fields(summary_type)}

    assert all(
        summary_fields[name].kw_only
        for name in (
            "catalog_candidates",
            "catalog_classified",
            "catalog_cache_hits",
            "catalog_review_required",
            "catalog_errors",
            "catalog_source_stale",
            "catalog_stale_marked",
            "catalog_source_missing",
            "catalog_complete",
        )
    )


@pytest.mark.parametrize(
    ("summary", "catalog", "expected_complete", "expected_missing"),
    (
        (
            ArchiveRouteSummary(candidates=0),
            CatalogUpdateSummary(
                catalog_run_id=1,
                source_kind="archive",
                source_missing=True,
            ),
            True,
            0,
        ),
        (
            ArchiveRouteSummary(candidates=2),
            CatalogUpdateSummary(
                catalog_run_id=2,
                source_kind="archive",
                source_missing=True,
            ),
            False,
            1,
        ),
        (
            ImageRouteSummary(candidates=1),
            CatalogUpdateSummary(
                catalog_run_id=3,
                source_kind="image",
                source_stale=1,
            ),
            False,
            0,
        ),
        (
            VideoRouteSummary(candidates=1),
            CatalogUpdateSummary(catalog_run_id=4, source_kind="video", errors=1),
            False,
            0,
        ),
        (
            CodeRouteSummary(candidates=1),
            CatalogUpdateSummary(
                catalog_run_id=5,
                source_kind="code",
                review_required=1,
            ),
            True,
            0,
        ),
    ),
)
def test_catalog_projection_reports_missing_stale_error_and_review_without_false_error(
    summary: object,
    catalog: CatalogUpdateSummary,
    expected_complete: bool,
    expected_missing: int,
) -> None:
    projected = route_registry._summary_with_catalog(summary, (catalog,))

    assert projected.catalog_complete is expected_complete
    assert projected.catalog_source_missing == expected_missing
    assert projected.catalog_errors == catalog.errors
    assert projected.catalog_source_stale == catalog.source_stale
    assert projected.catalog_review_required == catalog.review_required


def test_catalog_hook_maps_each_route_to_its_existing_owner_and_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_paths = {
        name: tmp_path / f"{name}.sqlite3"
        for name in (
            "pdf",
            "docx",
            "office",
            "text",
            "audio",
            "archive",
            "image",
            "video",
            "code",
        )
    }
    config = SimpleNamespace(
        document_catalog_enabled=True,
        document_catalog_database=tmp_path / "document_catalog.sqlite3",
        document_taxonomy_path=None,
        document_classification_max_chars=1024,
        resume_run_id=17,
        **{f"{name}_database": path for name, path in owner_paths.items()},
    )

    calls: list[tuple[str, Path, str, dict[str, object]]] = []

    def fake_update(
        catalog: Path,
        source: Path,
        source_kind: str,
        **kwargs: object,
    ) -> CatalogUpdateSummary:
        calls.append(
            (
                "update",
                source,
                source_kind,
                {str(key): value for key, value in kwargs.items()},
            )
        )
        return CatalogUpdateSummary(  # type: ignore[arg-type]
            catalog_run_id=len(calls),
            source_kind=source_kind,
            review_required=int(source_kind in {"archive", "image", "video", "code"}),
        )

    monkeypatch.setattr(catalog_module, "update_document_catalog_source", fake_update)

    class FakeState:
        def __init__(self) -> None:
            self.lifecycle: list[tuple[str, tuple[object, ...]]] = []
            self.events: list[tuple[object, ...]] = []

        def begin_route_phase(self, *args: object, **kwargs: object) -> None:
            self.lifecycle.append(("begin", (*args, kwargs)))

        def complete_route_phase(self, *args: object, **kwargs: object) -> None:
            self.lifecycle.append(("complete", (*args, kwargs)))

        def fail_route_phase(self, *args: object, **kwargs: object) -> None:
            self.lifecycle.append(("fail", (*args, kwargs)))

        def record_event(self, *args: object, **kwargs: object) -> None:
            self.events.append((*args, kwargs))

    state = FakeState()
    context = RouteExecutionContext(
        config=config,  # type: ignore[arg-type]
        root=tmp_path,
        framework_state=state,  # type: ignore[arg-type]
        run_id=23,
        scan_id=41,
        progress=None,
        resource_coordinator=None,
        cancellation=SimpleNamespace(),  # type: ignore[arg-type]
    )

    for source_kind in (
        "pdf",
        "docx",
        "office",
        "text",
        "audio",
        "archive",
        "image",
        "video",
        "code",
    ):
        summaries = route_registry._update_document_catalog_after_route(  # type: ignore[arg-type]
            context, source_kind
        )
        assert len(summaries) == (3 if source_kind == "office" else 1)

    expected = (
        (owner_paths["pdf"], "pdf"),
        (owner_paths["docx"], "docx"),
        (owner_paths["office"], "xlsx"),
        (owner_paths["office"], "pptx"),
        (owner_paths["office"], "odt"),
        (owner_paths["text"], "text"),
        (owner_paths["audio"], "audio"),
        (owner_paths["archive"], "archive"),
        (owner_paths["image"], "image"),
        (owner_paths["video"], "video"),
        (owner_paths["code"], "code"),
    )
    assert tuple((source, kind) for _operation, source, kind, _kwargs in calls) == expected
    assert all(kwargs["framework_run_id"] == 23 for _operation, _source, _kind, kwargs in calls)
    expected_progress = (
        "pdf",
        "docx",
        "office",
        "office",
        "office",
        "text",
        "audio",
        "archive",
        "image",
        "video",
        "code",
    )
    assert all(
        kwargs["progress_operation"] == progress
        for (_operation, _source, _kind, kwargs), progress in zip(
            calls, expected_progress, strict=True
        )
    )
    assert [item[0] for item in state.lifecycle].count("begin") == 9
    assert [item[0] for item in state.lifecycle].count("complete") == 9
    assert not any(item[0] == "fail" for item in state.lifecycle)
    assert len(state.events) == 9
    events_by_phase = {str(item[2]): item for item in state.events}
    complete_by_route = {
        str(item[1][1]): item
        for item in state.lifecycle
        if item[0] == "complete"
    }
    for source_kind in ("archive", "image", "video", "code"):
        event = events_by_phase[f"{source_kind}-catalog"]
        assert event[1] == "warning"
        assert event[4]["sources"][0]["review_required"] == 1
        phase = complete_by_route[source_kind]
        assert phase[1][3]["sources"][0]["review_required"] == 1


def test_catalog_failure_marks_the_existing_route_phase_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class State:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.events: list[object] = []

        def begin_route_phase(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.calls.append("begin")

        def complete_route_phase(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.calls.append("complete")

        def fail_route_phase(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.calls.append("fail")

        def record_event(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.events.append(object())

    def fail_update(*args: object, **kwargs: object) -> CatalogUpdateSummary:
        del args, kwargs
        raise RuntimeError("catalog fixture failure")

    monkeypatch.setattr(catalog_module, "update_document_catalog_source", fail_update)
    config = SimpleNamespace(
        document_catalog_enabled=True,
        document_catalog_database=tmp_path / "document_catalog.sqlite3",
        document_taxonomy_path=None,
        document_classification_max_chars=1024,
        resume_run_id=None,
        archive_database=tmp_path / "archive.sqlite3",
    )
    state = State()
    context = RouteExecutionContext(
        config=config,  # type: ignore[arg-type]
        root=tmp_path,
        framework_state=state,  # type: ignore[arg-type]
        run_id=1,
        scan_id=2,
        progress=None,
        resource_coordinator=None,
        cancellation=None,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="catalog fixture failure"):
        route_registry._update_document_catalog_after_route(context, "archive")

    assert state.calls == ["begin", "fail"]
    assert not state.events


def test_multimodal_wrappers_update_catalog_after_the_owner_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_names = ("archive", "image", "video", "code")
    fake_runs: list[str] = []

    def make_fake_route(route_name: str) -> type[object]:
        class FakeRoute:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def run(self) -> object:
                fake_runs.append(route_name)
                return object()

        return FakeRoute

    module_names = {
        "archive": "neocortex.capabilities.formats.archive.route",
        "image": "neocortex.capabilities.formats.image.route",
        "video": "neocortex.capabilities.formats.video.route",
        "code": "neocortex.code.code_route",
    }
    for route_name, module_name in module_names.items():
        module = types.ModuleType(module_name)
        class_name = {
            "archive": "ArchiveRoute",
            "image": "ImageRoute",
            "video": "VideoRoute",
            "code": "CodeRoute",
        }[route_name]
        setattr(module, class_name, make_fake_route(route_name))
        monkeypatch.setitem(sys.modules, module_name, module)

    class FakeDedupIndex:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def __enter__(self) -> "FakeDedupIndex":
            return self

        def __exit__(self, *args: object) -> bool:
            del args
            return False

    import neocortex.deduplication as deduplication

    monkeypatch.setattr(deduplication, "DedupIndex", FakeDedupIndex)
    for name in route_names:
        monkeypatch.setattr(
            route_registry,
            f"{name}_route_config_from_framework",
            lambda _config, **_kwargs: object(),
        )

    catalog_calls: list[str] = []

    def fake_catalog(_context: object, source_kind: str) -> tuple[object, ...]:
        catalog_calls.append(source_kind)
        return ()

    monkeypatch.setattr(route_registry, "_update_document_catalog_after_route", fake_catalog)
    config = SimpleNamespace(
        dedup_database=tmp_path / "dedup.sqlite3",
        archive_database=tmp_path / "archive.sqlite3",
        image_database=tmp_path / "image.sqlite3",
        video_database=tmp_path / "video.sqlite3",
        code_database=tmp_path / "code.sqlite3",
    )
    context = RouteExecutionContext(
        config=config,  # type: ignore[arg-type]
        root=tmp_path,
        framework_state=object(),  # type: ignore[arg-type]
        run_id=1,
        scan_id=2,
        progress=None,
        resource_coordinator=None,
        cancellation=SimpleNamespace(),  # type: ignore[arg-type]
    )

    route_registry._run_archive(context)
    route_registry._run_image(context)
    route_registry._run_video(context)
    route_registry._run_code(context)

    assert fake_runs == list(route_names)
    assert catalog_calls == list(route_names)


def test_catalog_projection_does_not_replace_fields_missing_from_multimodal_summaries() -> None:
    @dataclass(frozen=True, slots=True)
    class MultimodalSummary:
        processed: int = 1

    @dataclass(frozen=True, slots=True)
    class CatalogSummary:
        catalog_candidates: int = 0
        catalog_classified: int = 0

    catalog = CatalogUpdateSummary(
        catalog_run_id=1,
        source_kind="archive",
        candidates=3,
        classified=2,
    )
    multimodal = MultimodalSummary()
    projected = route_registry._summary_with_catalog(multimodal, (catalog,))
    assert projected is multimodal

    projected_catalog = route_registry._summary_with_catalog(CatalogSummary(), (catalog,))
    assert projected_catalog.catalog_candidates == 3
    assert projected_catalog.catalog_classified == 2
