"""Regression contracts for large Semantic owners under a writer lock."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.api.cli import cli_semantic
from neocortex.semantic.semantic_application import (
    _SemanticIndexExecution,
    _execute_semantic_image_index,
    _execute_semantic_text_index,
    _integrated_stage_details,
    _semantic_index_failure,
)
from neocortex.semantic.semantic_models import GenerationSummary
from neocortex.semantic.semantic_service_contracts import (
    GenerationWorkResult,
    SemanticIndexResult,
)
from neocortex.semantic.semantic_work_budget import SemanticWorkBudget
from neocortex.semantic.semantic_config import multilingual_text_model
from neocortex.persistence.framework_state_writer import FrameworkState


def test_semantic_index_failure_preserves_typed_cause_and_sink_event(
    tmp_path: Path,
) -> None:
    failure = RuntimeError("SQLite snapshot temporary bytes budget exhausted")
    args = Namespace(state_directory=tmp_path)
    events: list[tuple[str, object]] = []
    execution = SimpleNamespace(
        args=args,
        results=(),
        scope_timings=(),
        result_sink=lambda scope, value: events.append((scope, value)),
    )

    assert _semantic_index_failure(execution, failure, print_output=False) == 2
    assert args._semantic_failure is failure
    assert events[0][0] == "__error__"
    assert events[0][1]["error_type"] == "RuntimeError"
    assert "temporary bytes" in events[0][1]["error"]
    details = _integrated_stage_details(
        args,
        selected_sources=("text",),
        image_available=False,
        semantic_exit_code=2,
        error=args._semantic_failure,
    )
    assert details["error_type"] == "RuntimeError"
    assert "temporary bytes" in details["error"]


def test_writer_coordinated_flag_reaches_text_replay_operation(tmp_path: Path) -> None:
    args = Namespace(
        semantic_index="text",
        semantic_model_cache=None,
        semantic_threads=None,
        state_directory=tmp_path,
    )
    execution = _SemanticIndexExecution(
        args,
        multilingual_text_model(),
        ("text",),
        SemanticWorkBudget(),
        None,
        None,
        writer_coordinated=True,
    )
    seen: dict[str, object] = {}

    def operation(_state_directory: Path, **kwargs: object) -> SemanticIndexResult:
        seen.update(kwargs)
        from neocortex.semantic.semantic_service import _writer_coordinated_enabled

        seen["writer_scope"] = _writer_coordinated_enabled()
        summary = GenerationSummary(1, "fixture-model", "fixture", "ready", 0, 0, 1, 0, 0, {})
        return SemanticIndexResult(
            tmp_path / "semantic.sqlite3",
            ("text",),
            0,
            0,
            (GenerationWorkResult(summary, 0, 0, 0, 0),),
        )

    _execute_semantic_text_index(execution, operation)

    assert seen["writer_scope"] is True


def test_writer_coordinated_flag_reaches_image_replay_operation(tmp_path: Path) -> None:
    args = Namespace(
        semantic_index="image",
        semantic_model_cache=None,
        semantic_threads=None,
        semantic_no_ocr=False,
        state_directory=tmp_path,
    )
    execution = _SemanticIndexExecution(
        args,
        multilingual_text_model(),
        (),
        SemanticWorkBudget(),
        None,
        None,
        writer_coordinated=True,
    )
    seen: dict[str, object] = {}

    def operation(_state_directory: Path, **kwargs: object) -> SemanticIndexResult:
        seen.update(kwargs)
        from neocortex.semantic.semantic_service import _writer_coordinated_enabled

        seen["writer_scope"] = _writer_coordinated_enabled()
        summary = GenerationSummary(1, "fixture-model", "fixture", "ready", 0, 0, 1, 0, 0, {})
        return SemanticIndexResult(
            tmp_path / "semantic.sqlite3",
            ("image",),
            0,
            0,
            (GenerationWorkResult(summary, 0, 0, 0, 0),),
        )

    _execute_semantic_image_index(execution, operation)

    assert seen["writer_scope"] is True


def test_integrated_real_semantic_failure_reaches_stage_details_and_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_lifecycle_stage import _manifest
    from tests.test_semantic_source_heads import _pdf_state

    root = tmp_path / "corpus"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    _pdf_state(state / "pdf.sqlite3")
    with FrameworkState(state / "framework.sqlite3") as framework:
        run_id = framework.begin_initial_run(root, None)
        framework.publish_run_manifest(run_id, _manifest(run_id, root))

    def fail_index(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("SQLite snapshot temporary bytes budget exhausted")

    monkeypatch.setattr(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        fail_index,
    )
    captured: list[tuple[str, object]] = []
    args = Namespace(
        all=True,
        state_directory=state,
        semantic_source=["pdf"],
        semantic_index="text",
        semantic_max_items=10,
        semantic_max_new_jobs=10,
        semantic_time_budget_seconds=30.0,
        semantic_model_cache=None,
        semantic_threads=None,
        semantic_no_ocr=False,
        semantic_text_profile="quality",
    )

    assert (
        cli_semantic.run_integrated_all_semantic_index(
            args,
            result_sink=lambda scope, value: captured.append((scope, value)),
            print_output=False,
            run_id=run_id,
        )
        == 2
    )
    assert captured and captured[0][0] == "__error__"
    assert "temporary bytes" in captured[0][1]["error"]
    with FrameworkState(state / "framework.sqlite3", existing_only=True) as framework:
        stage = framework.read_run_stages(run_id)[-1]
    assert stage["status"] == "partial"
    assert stage["details"]["error_type"] == "RuntimeError"
    assert "temporary bytes" in stage["details"]["error"]
