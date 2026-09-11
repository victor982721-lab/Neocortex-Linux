from __future__ import annotations

# region [01] Imports and result fixtures

import argparse
import inspect
import json
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Literal
from unittest.mock import patch

import pytest

from neocortex.api.cli.cli_app import dispatch_direct
from neocortex.api.cli import cli_semantic as semantic_cli
from neocortex.api.cli.cli_semantic import (
    run_integrated_all_semantic_index,
    run_semantic_index,
)
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.semantic.semantic_config import COMPACT_TEXT_MODEL_ID
from neocortex.semantic.semantic_lexical import (
    LexicalAvailability,
    LexicalRanking,
)
from neocortex.safety.protected_content import (
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from neocortex.semantic.semantic_models import (
    CalibrationStatus,
    EvidenceDisposition,
    FusedHit,
    FusionEvidence,
    GenerationSummary,
    SemanticEvidence,
)
from neocortex.semantic.semantic_service import (
    FusedResolvedHit,
    GenerationWorkResult,
    ModelPreparation,
    SemanticClassificationResult,
    SemanticEvidencePassResult,
    SemanticIndexResult,
    SemanticPlan,
    SemanticRanking,
    SemanticSearchResult,
    SemanticSourcePlan,
    SemanticWorkloadPlan,
)
from neocortex.semantic.semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
)
from tests.internal_paths_test_support import disjoint_internal_paths_policy


@pytest.fixture(autouse=True)
def _safe_state_write_policies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_policy = disjoint_internal_paths_policy(tmp_path.parent / f"{tmp_path.name}-policy")
    protected_policy = ProtectedContentPolicy.capture(())
    monkeypatch.setattr(
        "neocortex.safety.internal_paths.canonical_internal_paths_policy",
        lambda: internal_policy,
    )
    monkeypatch.setattr(
        "neocortex.safety.protected_content.canonical_protected_content_policy",
        lambda: protected_policy,
    )


def _generation(
    *,
    pending: int = 0,
    errors: int = 0,
    stale: int = 0,
) -> GenerationWorkResult:
    return GenerationWorkResult(
        GenerationSummary(
            generation_id=7,
            model_signature="model-signature",
            processing_signature="pipeline-signature",
            status=("ready" if not pending and not errors and not stale else "ready_partial"),
            pending=pending,
            leased=0,
            done=3,
            errors=errors,
            stale=stale,
            cursor={"completed_source": "pdf"},
        ),
        queued=3,
        reused=1,
        embedded=2,
        failed=errors,
    )


def _index_result(
    state_directory: Path,
    sources: tuple[str, ...],
    *,
    pending: int = 0,
    stale: int = 0,
    truncated: bool = False,
    truncation_reason: str | None = None,
) -> SemanticIndexResult:
    return SemanticIndexResult(
        semantic_database=state_directory / "semantic.sqlite3",
        sources=sources,
        items_staged=4,
        chunks_staged=6,
        generations=(_generation(pending=pending, stale=stale),),
        truncated=truncated,
        truncation_reason=truncation_reason,
    )


def _plan_result(state_directory: Path) -> SemanticPlan:
    source = SemanticSourcePlan(
        source_kind="pdf",
        database=state_directory / "pdf.sqlite3",
        schema_version=11,
        resources=2,
        sections=2,
        chunks=2,
        embedding_entities=2,
        source_bytes=200,
        section_text_bytes=40,
        input_bytes=40,
        snapshot_xxh3_128="c" * 32,
    )
    workload = SemanticWorkloadPlan(
        name="text",
        modality="text",
        role="passage",
        model_signature="model-signature",
        vector_space="model-space",
        model_id="model-id",
        model_version="model-v1",
        dimensions=768,
        provider="fixture-provider",
        supported_roles=("query", "passage"),
        vector_dtype="float16",
        normalization="l2",
        distance="cosine",
        model_provenance_json="{}",
        processing_signature="processing-signature",
        embedding_entities=2,
        unique_contents=1,
        preexisting_reusable_contents=0,
        planned_reusable_contents=0,
        new_unique_contents=1,
        input_bytes=40,
        unique_input_bytes=20,
        new_vector_blob_bytes_lower_bound=1536,
        model_request_contents_lower_bound=1,
        model_request_contents_upper_bound=2,
        estimated_model_seconds_lower_bound=None,
        estimated_model_seconds_upper_bound=None,
        cost_calibration_signature=None,
        cost_execution_signature=None,
        cost_calibration_contents_per_second=None,
        cost_calibration_sample_contents=None,
        cost_calibration_sample_input_bytes=None,
        cost_unavailable_reason="no_exact_cost_calibration",
    )
    return SemanticPlan(
        scope="text",
        selected_sources=("pdf",),
        semantic_database=state_directory / "semantic.sqlite3",
        semantic_schema_version=None,
        source_plans=(source,),
        workloads=(workload,),
        text_chunking_signature="chunking-v1",
        content_set_xxh3_128="a" * 32,
        semantic_snapshot_xxh3_128="d" * 32,
        plan_signature="semantic-readonly-plan-v2:xxh3-128:" + "b" * 32,
        resources=2,
        sections=2,
        chunks=2,
        embedding_entities=2,
        source_bytes=200,
        section_text_bytes=40,
        input_bytes=40,
        unique_contents=1,
        unique_input_bytes=20,
        reusable_unique_contents=0,
        new_unique_contents=1,
        new_vector_blob_bytes_lower_bound=1536,
        model_request_contents_lower_bound=1,
        model_request_contents_upper_bound=2,
        estimated_model_seconds_lower_bound=None,
        estimated_model_seconds_upper_bound=None,
        scratch_storage_bytes=4096,
        max_scratch_bytes=512 * 1024 * 1024,
        originals_verified=None,
        execution_ready=None,
    )


# endregion [01]


# region [02] Parser and validation


def test_semantic_cli_defaults_are_offline_bounded_and_quality_first() -> None:
    args = build_parser().parse_args(["--semantic-search", "transformador"])

    validate_arguments(args)

    assert args.semantic_text_profile == "quality"
    assert args.semantic_search_mode == "all"
    assert args.semantic_search_limit == 20
    assert args.semantic_evidence_limit == 100
    assert args.semantic_max_vectors == 500_000
    assert args.semantic_plan_max_scratch_bytes == 512 * 1024 * 1024
    assert args.semantic_max_items == 50
    assert args.semantic_max_new_jobs == 1_500
    assert args.semantic_time_budget_seconds == 900.0
    assert args.semantic_source is None


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ["--semantic-status", "--semantic-search", "breaker"],
            "semantic direct actions are mutually exclusive",
        ),
        (
            ["--semantic-status", "--semantic-plan", "text"],
            "semantic direct actions are mutually exclusive",
        ),
        (["--semantic-search", ""], "--semantic-search must be non-empty"),
        (
            ["--semantic-search", "breaker", "--semantic-search-limit", "0"],
            "--semantic-search-limit must be between",
        ),
        (
            ["--semantic-evidence", "item:1", "--semantic-evidence-limit", "0"],
            "--semantic-evidence-limit must be between",
        ),
        (
            ["--semantic-evidence-limit", "10"],
            "semantic options require one semantic direct action",
        ),
        (
            ["--semantic-search", "breaker", "--semantic-max-vectors", "10000001"],
            "--semantic-max-vectors must be between",
        ),
        (
            ["--semantic-index", "image", "--semantic-source", "pdf"],
            "--semantic-source requires",
        ),
        (
            ["--semantic-plan", "image", "--semantic-source", "pdf"],
            "--semantic-source requires",
        ),
        (
            ["--semantic-index", "text", "--semantic-no-ocr"],
            "--semantic-no-ocr requires",
        ),
        (
            ["--semantic-plan", "text", "--semantic-no-ocr"],
            "--semantic-no-ocr requires",
        ),
        (
            ["--semantic-status", "--semantic-include-compact"],
            "--semantic-include-compact requires",
        ),
        (
            ["--semantic-prepare-models", "--semantic-text-profile", "compact"],
            "--semantic-text-profile requires",
        ),
        (
            ["--semantic-status", "--semantic-threads", "2"],
            "model cache/thread options require",
        ),
        (
            ["--semantic-index", "text", "--apply"],
            "cannot be combined with file-action --apply",
        ),
        (
            ["--semantic-search-mode", "lexical"],
            "semantic options require one semantic direct action",
        ),
        (
            ["--semantic-status", "--semantic-plan-json"],
            "--semantic-plan-json requires --semantic-plan",
        ),
        (
            ["--semantic-status", "--semantic-plan-max-scratch-bytes", "65536"],
            "--semantic-plan-max-scratch-bytes requires --semantic-plan",
        ),
        (
            ["--semantic-plan", "text", "--semantic-plan-max-scratch-bytes", "1"],
            "--semantic-plan-max-scratch-bytes must be between",
        ),
        (
            ["--semantic-index", "text", "--semantic-max-items", "0"],
            "--semantic-max-items must be between",
        ),
        (
            ["--semantic-index", "text", "--semantic-max-new-jobs", "0"],
            "--semantic-max-new-jobs must be between",
        ),
        (
            ["--semantic-index", "text", "--semantic-time-budget-seconds", "nan"],
            "--semantic-time-budget-seconds must be finite",
        ),
        (
            ["--semantic-index", "text", "--semantic-time-budget-seconds", "172801"],
            "--semantic-time-budget-seconds must be finite",
        ),
        (
            ["--semantic-status", "--semantic-max-items", "10"],
            "semantic index budget options require --semantic-index",
        ),
    ),
)
def test_semantic_cli_rejects_ambiguous_or_out_of_scope_options(
    arguments: list[str],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


# endregion [02]


# region [03] Status, preparation and incremental indexing


def test_semantic_plan_json_is_read_only_and_uses_selected_profile(
    tmp_path,
    capsys,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-plan",
            "text",
            "--semantic-plan-json",
            "--semantic-source",
            "pdf",
            "--semantic-text-profile",
            "compact",
        ]
    )
    validate_arguments(args)
    result = _plan_result(tmp_path)

    with patch(
        "neocortex.semantic.semantic_service.plan_semantic_index",
        return_value=result,
    ) as operation:
        assert dispatch_direct(args) == 0

    kwargs = operation.call_args.kwargs
    assert kwargs["scope"] == "text"
    assert kwargs["source_kinds"] == ("pdf",)
    assert kwargs["text_model"].model_id == COMPACT_TEXT_MODEL_ID
    assert kwargs["embed_ocr_text"] is True
    assert kwargs["max_scratch_bytes"] == 512 * 1024 * 1024
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["jobs_created"] == 0
    assert payload["unique_contents"] == 1
    assert payload["complete"] is False
    assert payload["model_request_contents_lower_bound"] == 1
    assert payload["model_request_contents_upper_bound"] == 2
    assert payload["cost_calibrated"] is False
    assert payload["workloads"][0]["role"] == "passage"
    assert payload["workloads"][0]["dimensions"] == 768
    assert not (tmp_path / "framework.lock").exists()
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_semantic_plan_text_output_exposes_ranges_without_claiming_calibration(
    tmp_path,
    capsys,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-plan",
            "text",
            "--semantic-source",
            "pdf",
        ]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.plan_semantic_index",
        return_value=_plan_result(tmp_path),
    ):
        assert dispatch_direct(args) == 0

    output = capsys.readouterr().out
    assert "complete=0" in output
    assert "request_lower=1 request_upper=2" in output
    assert "model_seconds_lower=- model_seconds_upper=-" in output
    assert "cost_calibrated=0 cost_complete=0" in output
    assert "role=passage" in output
    assert "dimensions=768 dtype=float16" in output


def test_semantic_status_is_read_only_when_state_does_not_exist(
    tmp_path,
    capsys,
) -> None:
    state_directory = tmp_path / "missing"
    args = build_parser().parse_args(
        ["--state-directory", str(state_directory), "--semantic-status"]
    )
    validate_arguments(args)

    assert dispatch_direct(args) == 0
    assert "SEMANTIC_STATUS exists=0" in capsys.readouterr().out
    assert not state_directory.exists()


def test_semantic_prepare_is_the_only_cli_action_that_authorizes_downloads(
    tmp_path,
    capsys,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-prepare-models",
            "--semantic-include-compact",
            "--semantic-threads",
            "2",
        ]
    )
    validate_arguments(args)
    prepared = (ModelPreparation("signature", "model-id", 384, 0.25),)

    with patch(
        "neocortex.semantic.semantic_service.prepare_semantic_models",
        return_value=prepared,
    ) as operation:
        assert dispatch_direct(args) == 0

    operation.assert_called_once_with(
        tmp_path,
        model_cache=None,
        include_compact=True,
        local_files_only=False,
        threads=2,
    )
    assert "SEMANTIC_MODEL_READY id=model-id" in capsys.readouterr().out
    assert (tmp_path / "framework.lock").is_file()


def test_semantic_prepare_rejects_protected_missing_state_before_mkdir_or_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    protected_root = tmp_path / "protected"
    protected_root.mkdir()
    state_directory = protected_root / "missing-state"
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "protected-state",
                "tree",
                "exclude",
                protected_root,
            ),
        )
    )
    monkeypatch.setattr(
        "neocortex.safety.protected_content.canonical_protected_content_policy",
        lambda: protected_policy,
    )
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(state_directory),
            "--semantic-prepare-models",
        ]
    )
    validate_arguments(args)

    with (
        patch("neocortex.semantic.semantic_service.prepare_semantic_models") as operation,
        patch("neocortex.runtime.control.locking.FrameworkRunLock") as lock,
    ):
        assert dispatch_direct(args) == 2

    operation.assert_not_called()
    lock.assert_not_called()
    assert "protected content" in capsys.readouterr().out
    assert not state_directory.exists()


@pytest.mark.parametrize(
    ("command", "operation_path"),
    (
        (
            ("--semantic-index", "image"),
            "neocortex.semantic.semantic_service.index_image_embeddings",
        ),
        (
            ("--semantic-classify", "all"),
            "neocortex.semantic.semantic_service.classify_semantic_index",
        ),
    ),
)
def test_semantic_writes_reject_existing_protected_state_before_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    command: tuple[str, str],
    operation_path: str,
) -> None:
    state_directory = tmp_path / "protected-state"
    state_directory.mkdir()
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "protected-state",
                "tree",
                "exclude",
                state_directory,
            ),
        )
    )
    monkeypatch.setattr(
        "neocortex.safety.protected_content.canonical_protected_content_policy",
        lambda: protected_policy,
    )
    args = build_parser().parse_args(("--state-directory", str(state_directory), *command))
    validate_arguments(args)

    with (
        patch(operation_path) as operation,
        patch("neocortex.runtime.control.locking.FrameworkRunLock") as lock,
    ):
        assert dispatch_direct(args) == 2

    operation.assert_not_called()
    lock.assert_not_called()
    assert "protected content" in capsys.readouterr().out
    assert not (state_directory / "framework.lock").exists()


def test_semantic_index_all_runs_text_then_image_offline_with_selected_profile(
    tmp_path,
    capsys,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-index",
            "all",
            "--semantic-source",
            "pdf",
            "--semantic-source",
            "audio",
            "--semantic-text-profile",
            "compact",
            "--semantic-no-ocr",
            "--semantic-threads",
            "3",
        ]
    )
    validate_arguments(args)
    order: list[str] = []

    def text_index(*_args, **_kwargs):
        order.append("text")
        return _index_result(tmp_path, ("pdf", "audio"))

    def image_index(*_args, **_kwargs):
        order.append("image")
        return _index_result(tmp_path, ("image",))

    with (
        patch(
            "neocortex.semantic.semantic_service.index_text_embeddings",
            side_effect=text_index,
        ) as text_operation,
        patch(
            "neocortex.semantic.semantic_service.index_image_embeddings",
            side_effect=image_index,
        ) as image_operation,
    ):
        assert dispatch_direct(args) == 0

    assert order == ["text", "image"]
    text_kwargs = text_operation.call_args.kwargs
    assert text_kwargs["source_kinds"] == ("pdf", "audio")
    assert text_kwargs["local_files_only"] is True
    assert text_kwargs["model"].model_id == COMPACT_TEXT_MODEL_ID
    image_kwargs = image_operation.call_args.kwargs
    assert image_kwargs["local_files_only"] is True
    assert image_kwargs["embed_ocr_text"] is False
    assert image_kwargs["ocr_model"].model_id == COMPACT_TEXT_MODEL_ID
    assert text_kwargs["work_budget"] is image_kwargs["work_budget"]
    assert text_kwargs["work_budget"].max_items == 50
    assert text_kwargs["work_budget"].max_new_jobs == 1_500
    assert text_kwargs["work_budget"].deadline is not None
    output = capsys.readouterr().out
    assert "SEMANTIC_INDEX scope=text" in output
    assert "SEMANTIC_INDEX scope=image" in output
    assert "SEMANTIC_TIMING scope=text elapsed_ns=" in output
    assert "SEMANTIC_TIMING scope=image elapsed_ns=" in output
    assert (tmp_path / "framework.lock").is_file()


def test_semantic_index_public_signature_is_stable() -> None:
    assert str(inspect.signature(run_semantic_index)) == (
        "(args: 'argparse.Namespace', *, incomplete_is_error: 'bool' = True, "
        "progress: 'ProgressCallback | None' = None, result_sink: "
        "'Callable[[str, object], None] | None' = None, print_output: 'bool' = "
        "True, framework_lock_held: 'bool' = False) -> 'int'"
    )


def test_integrated_semantic_seam_skips_nested_framework_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(
        [
            "--all",
            "--state-directory",
            str(tmp_path),
            "--semantic-source",
            "pdf",
        ]
    )
    validate_arguments(args)
    monkeypatch.setattr(semantic_cli, "_semantic_text_model", lambda _profile: object())
    monkeypatch.setattr(
        semantic_cli,
        "_selected_semantic_text_sources",
        lambda _args: ("pdf",),
    )
    monkeypatch.setattr(
        semantic_cli,
        "_validate_semantic_state_write",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(semantic_cli, "_execute_semantic_index_scopes", lambda *_args, **_kwargs: None)

    from neocortex.runtime.control.locking import FrameworkRunLock

    with FrameworkRunLock(tmp_path / "framework.lock"):
        assert (
            run_semantic_index(
                args,
                print_output=False,
                framework_lock_held=True,
            )
            == 0
        )


def test_direct_semantic_index_still_rejects_framework_lock_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(
        [
            "--semantic-index",
            "text",
            "--semantic-source",
            "pdf",
            "--state-directory",
            str(tmp_path),
        ]
    )
    validate_arguments(args)
    monkeypatch.setattr(semantic_cli, "_semantic_text_model", lambda _profile: object())
    monkeypatch.setattr(
        semantic_cli,
        "_selected_semantic_text_sources",
        lambda _args: ("pdf",),
    )
    monkeypatch.setattr(
        semantic_cli,
        "_validate_semantic_state_write",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(semantic_cli, "_execute_semantic_index_scopes", lambda *_args, **_kwargs: None)

    from neocortex.runtime.control.locking import FrameworkRunLock

    with FrameworkRunLock(tmp_path / "framework.lock"):
        assert run_semantic_index(args, print_output=False) == 2


def test_integrated_semantic_forwards_outer_lock_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(
        [
            "--all",
            "--state-directory",
            str(tmp_path),
            "--semantic-source",
            "pdf",
        ]
    )
    validate_arguments(args)
    from tests.test_semantic_source_heads import _pdf_state

    _pdf_state(tmp_path / "pdf.sqlite3")
    observed: dict[str, object] = {}

    def fake_index(_args, **kwargs):
        observed.update(kwargs)
        return 0

    monkeypatch.setattr(semantic_cli, "run_semantic_index", fake_index)
    assert (
        run_integrated_all_semantic_index(
            args,
            print_output=False,
            framework_lock_held=True,
        )
        == 2  # no generation receipt was supplied by this lock-only test double
    )
    assert observed["framework_lock_held"] is True


def test_semantic_index_preserves_preparation_execution_and_publication_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-index",
            "all",
            "--semantic-source",
            "code",
        ]
    )
    validate_arguments(args)
    events: list[str] = []
    text_result = _index_result(tmp_path, ("code",))
    image_result = _index_result(tmp_path, ("image",))
    original_model = semantic_cli._semantic_text_model
    original_sources = semantic_cli._selected_semantic_text_sources
    original_validate = semantic_cli._validate_semantic_state_write

    def record_model(profile: str) -> object:
        events.append("model")
        return original_model(profile)

    def record_sources(current_args: argparse.Namespace) -> tuple[str, ...]:
        events.append("sources")
        return original_sources(current_args)

    class RecordingBudget(SemanticWorkBudget):
        @classmethod
        def from_time_budget(
            cls,
            *,
            max_items: int | None = None,
            max_new_jobs: int | None = None,
            time_budget_seconds: float | None = None,
            clock: Callable[[], float] = time.monotonic,
            cancellation_check: Callable[[], bool | None] | None = None,
            preserve_existing_generations: bool = False,
            retry_recoverable_errors: bool = False,
        ) -> RecordingBudget:
            events.append("budget")
            assert time_budget_seconds is not None
            budget = super().from_time_budget(
                max_items=max_items,
                max_new_jobs=max_new_jobs,
                time_budget_seconds=time_budget_seconds,
                clock=clock,
                cancellation_check=cancellation_check,
                preserve_existing_generations=preserve_existing_generations,
                retry_recoverable_errors=retry_recoverable_errors,
            )
            assert isinstance(budget, RecordingBudget)
            return budget

    def record_validate(
        state_directory: Path,
        *,
        database: bool,
        extra_paths: tuple[Path, ...] = (),
    ) -> None:
        events.append("validate")
        assert database is True
        original_validate(
            state_directory,
            database=database,
            extra_paths=extra_paths,
        )

    class RecordingLock:
        def __init__(self, _path: Path) -> None:
            events.append("lock_construct")

        def __enter__(self) -> RecordingLock:
            events.append("lock_enter")
            return self

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: TracebackType | None,
        ) -> Literal[False]:
            events.append("lock_exit")
            return False

    def text_index(*_args: object, **_kwargs: object) -> SemanticIndexResult:
        events.append("text")
        return text_result

    def image_index(*_args: object, **_kwargs: object) -> SemanticIndexResult:
        events.append("image")
        return image_result

    def capture_result(scope: str, _result: object) -> None:
        events.append(f"sink:{scope}")

    def code_links(*_args: object, **_kwargs: object) -> tuple[int, int]:
        events.append("code_links")
        return 4, 3

    def print_result(scope: str, _result: object) -> None:
        events.append(f"print:{scope}")

    monkeypatch.setattr(semantic_cli, "_semantic_text_model", record_model)
    monkeypatch.setattr(
        semantic_cli,
        "_selected_semantic_text_sources",
        record_sources,
    )
    monkeypatch.setattr(
        "neocortex.semantic.semantic_work_budget.SemanticWorkBudget",
        RecordingBudget,
    )
    monkeypatch.setattr(semantic_cli, "_validate_semantic_state_write", record_validate)
    monkeypatch.setattr(
        "neocortex.runtime.control.locking.FrameworkRunLock",
        RecordingLock,
    )
    monkeypatch.setattr(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        text_index,
    )
    monkeypatch.setattr(
        "neocortex.semantic.semantic_service.index_image_embeddings",
        image_index,
    )
    monkeypatch.setattr(
        "neocortex.code.search.code_semantic_links.current_code_embedding_link_counts",
        code_links,
    )
    monkeypatch.setattr(semantic_cli, "_print_semantic_index_result", print_result)

    assert run_semantic_index(args, result_sink=capture_result) == 0
    assert events == [
        "model",
        "sources",
        "budget",
        "validate",
        "lock_construct",
        "lock_enter",
        "text",
        "sink:text",
        "code_links",
        "image",
        "sink:image",
        "lock_exit",
        "print:text",
        "print:image",
    ]


def test_semantic_index_cancellation_propagates_without_partial_console_publication(
    tmp_path: Path,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-index",
            "all",
            "--semantic-source",
            "pdf",
        ]
    )
    validate_arguments(args)
    text_result = _index_result(tmp_path, ("pdf",))
    captured: list[tuple[str, object]] = []
    lock_events: list[tuple[str, type[BaseException] | None]] = []
    cancellation = KeyboardInterrupt("injected semantic index cancellation")

    class RecordingLock:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self) -> RecordingLock:
            lock_events.append(("enter", None))
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: TracebackType | None,
        ) -> Literal[False]:
            lock_events.append(("exit", exc_type))
            return False

    with (
        patch(
            "neocortex.runtime.control.locking.FrameworkRunLock",
            RecordingLock,
        ),
        patch(
            "neocortex.semantic.semantic_service.index_text_embeddings",
            return_value=text_result,
        ),
        patch(
            "neocortex.semantic.semantic_service.index_image_embeddings",
            side_effect=cancellation,
        ),
        patch.object(semantic_cli, "_print_semantic_index_result") as print_result,
        patch.object(semantic_cli, "_semantic_failure") as semantic_failure,
    ):
        with pytest.raises(KeyboardInterrupt) as raised:
            run_semantic_index(
                args,
                result_sink=lambda scope, result: captured.append((scope, result)),
            )

    assert raised.value is cancellation
    assert captured == [("text", text_result)]
    assert lock_events == [("enter", None), ("exit", KeyboardInterrupt)]
    print_result.assert_not_called()
    semantic_failure.assert_not_called()


def test_semantic_index_reports_published_code_link_coverage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "code.sqlite3").touch()
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-index",
            "text",
            "--semantic-source",
            "code",
        ]
    )
    validate_arguments(args)
    with (
        patch(
            "neocortex.semantic.semantic_service.index_text_embeddings",
            return_value=_index_result(tmp_path, ("code",)),
        ),
        patch(
            "neocortex.code.search.code_semantic_links.current_code_embedding_link_counts",
            return_value=(4, 3),
        ),
    ):
        assert dispatch_direct(args) == 0

    output = capsys.readouterr().out
    assert "SEMANTIC_CODE_LINKS generation=7" in output
    assert "active=4 current=3 stale=1" in output
    assert "calibration=uncalibrated_similarity" in output


def test_semantic_index_reports_partial_generation_as_nonzero(
    tmp_path,
    capsys,
) -> None:
    (tmp_path / "pdf.sqlite3").touch()
    args = build_parser().parse_args(
        ["--state-directory", str(tmp_path), "--semantic-index", "text"]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        return_value=_index_result(tmp_path, ("pdf",), pending=1),
    ) as operation:
        assert dispatch_direct(args) == 2
    assert operation.call_args.kwargs["source_kinds"] == ("pdf",)
    assert "incomplete=1" in capsys.readouterr().out


def test_semantic_index_reports_stale_only_generation_as_nonzero(
    tmp_path,
    capsys,
) -> None:
    (tmp_path / "pdf.sqlite3").touch()
    args = build_parser().parse_args(
        ["--state-directory", str(tmp_path), "--semantic-index", "text"]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        return_value=_index_result(tmp_path, ("pdf",), stale=1),
    ):
        assert dispatch_direct(args) == 2
    output = capsys.readouterr().out
    assert "status=ready_partial" in output
    assert "stale=1" in output
    assert "complete=0" in output


def test_semantic_index_reports_budget_truncation_as_nonzero(
    tmp_path,
    capsys,
) -> None:
    (tmp_path / "pdf.sqlite3").touch()
    args = build_parser().parse_args(
        ["--state-directory", str(tmp_path), "--semantic-index", "text"]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        return_value=_index_result(
            tmp_path,
            ("pdf",),
            truncated=True,
            truncation_reason="max_items",
        ),
    ):
        assert dispatch_direct(args) == 2
    output = capsys.readouterr().out
    assert "truncated=1" in output
    assert "truncation_reason=max_items" in output


def test_all_includes_broad_archive_and_code_without_hidden_semantic_ceiling(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.test_semantic_sources import _create_archive_text_state, _create_code_text_state
    from tests.test_semantic_source_heads import _pdf_state

    _pdf_state(tmp_path / "pdf.sqlite3")
    _create_archive_text_state(tmp_path)
    _create_code_text_state(tmp_path)
    args = build_parser().parse_args(["--all", "--state-directory", str(tmp_path)])
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        return_value=_index_result(
            tmp_path,
            ("pdf", "archive", "code"),
            pending=1,
            truncated=True,
            truncation_reason="time_budget",
        ),
    ) as operation:
        assert run_integrated_all_semantic_index(args) == 2

    kwargs = operation.call_args.kwargs
    assert kwargs["source_kinds"] == ("pdf", "archive", "code")
    assert kwargs["work_budget"].max_items is None
    assert kwargs["work_budget"].max_new_jobs is None
    output = capsys.readouterr().out
    assert "SEMANTIC_ALL status=starting sources=pdf,archive,code" in output
    assert "code_explicit=1" in output
    assert "truncated=1" in output


def test_all_semantic_uses_shared_progress_and_captures_structured_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from neocortex.progress import RecordingProgress
    from tests.test_semantic_source_heads import _pdf_state

    _pdf_state(tmp_path / "pdf.sqlite3")
    args = build_parser().parse_args(["--all", "--state-directory", str(tmp_path)])
    validate_arguments(args)
    result = _index_result(tmp_path, ("pdf",))
    progress = RecordingProgress()
    captured: list[tuple[str, object]] = []
    with patch(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        return_value=result,
    ) as operation:
        assert (
            run_integrated_all_semantic_index(
                args,
                progress=progress,
                result_sink=lambda scope, value: captured.append((scope, value)),
                print_output=False,
            )
            == 0
        )

    assert operation.call_args.kwargs["progress"] is progress
    assert captured == [("text", result)]
    integrated = [event for event in progress.events if event.phase == "integrated"]
    assert [event.description for event in integrated] == [
        "Inicializando Semantic",
        "Semantic completado",
    ]
    assert integrated[-1].finished is True
    assert capsys.readouterr().out == ""


def test_all_accepts_explicit_code_semantic_selection(tmp_path: Path) -> None:
    from tests.test_semantic_sources import _create_code_text_state

    _create_code_text_state(tmp_path)
    args = build_parser().parse_args(
        [
            "--all",
            "--state-directory",
            str(tmp_path),
            "--semantic-source",
            "code",
            "--semantic-time-budget-seconds",
            "3600",
        ]
    )
    validate_arguments(args)
    with (
        patch(
            "neocortex.semantic.semantic_service.index_text_embeddings",
            return_value=_index_result(tmp_path, ("code",)),
        ) as operation,
        patch(
            "neocortex.code.search.code_semantic_links.current_code_embedding_link_counts",
            return_value=(0, 0),
        ),
    ):
        assert run_integrated_all_semantic_index(args) == 0

    assert operation.call_args.kwargs["source_kinds"] == ("code",)
    assert operation.call_args.kwargs["work_budget"].deadline is not None


def test_all_accepts_explicit_archive_semantic_selection(tmp_path: Path) -> None:
    from tests.test_semantic_sources import _create_archive_text_state

    _create_archive_text_state(tmp_path)
    args = build_parser().parse_args(
        [
            "--all",
            "--state-directory",
            str(tmp_path),
            "--semantic-source",
            "archive",
        ]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        return_value=_index_result(tmp_path, ("archive",)),
    ) as operation:
        assert run_integrated_all_semantic_index(args) == 0

    assert operation.call_args.kwargs["source_kinds"] == ("archive",)


def test_semantic_index_without_available_text_cache_fails_explicitly(
    tmp_path,
    capsys,
) -> None:
    args = build_parser().parse_args(
        ["--state-directory", str(tmp_path), "--semantic-index", "text"]
    )
    validate_arguments(args)

    with patch("neocortex.semantic.semantic_service.index_text_embeddings") as operation:
        assert dispatch_direct(args) == 2

    operation.assert_not_called()
    assert "no durable PDF, DOCX, Office, audio or code text cache" in capsys.readouterr().out


def test_semantic_index_deadline_failure_returns_two(
    tmp_path,
    capsys,
) -> None:
    (tmp_path / "pdf.sqlite3").touch()
    args = build_parser().parse_args(
        ["--state-directory", str(tmp_path), "--semantic-index", "text"]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.index_text_embeddings",
        side_effect=SemanticIndexDeadlineExceeded("model startup deadline"),
    ):
        assert dispatch_direct(args) == 2
    assert "model startup deadline" in capsys.readouterr().out


# endregion [03]


# region [04] Search, classification and advisory evidence


def _search_result(
    *,
    complete: bool = True,
    source_identity: str = "page:1",
    path: str = r"C:\corpus\transformador.pdf",
    snippet: str = "transformador de potencia",
) -> SemanticSearchResult:
    semantic_ranking = SemanticRanking(
        name="semantic_text",
        hits=(),
        resolved=(),
        scanned=25,
        complete=complete,
        cutoff_reason=None if complete else "max_vectors_reached",
        next_cursor=None if complete else 25,
    )
    lexical_ranking = LexicalRanking(
        source_kind="pdf",
        state_path=Path("pdf.sqlite3"),
        availability=LexicalAvailability.AVAILABLE,
        normalized_query="transformador",
        hits=(),
    )
    fused = FusedResolvedHit(
        fused=FusedHit(
            item_id="item:pdf:1",
            score=0.032,
            evidence=(
                FusionEvidence(
                    ranking="semantic_text",
                    rank=1,
                    raw_score=0.81,
                    contribution=0.016,
                    entity_id="chunk:1",
                    indexed_model_signature="model-signature",
                ),
            ),
        ),
        path=path,
        source_kind="pdf",
        source_identity=source_identity,
        snippet=snippet,
    )
    return SemanticSearchResult(
        "transformador",
        (semantic_ranking,),
        (lexical_ranking,),
        (fused,),
    )


def test_semantic_search_reports_rank_availability_fusion_and_completeness(
    tmp_path,
    capsys,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-search",
            "transformador",
            "--semantic-search-limit",
            "7",
            "--semantic-max-vectors",
            "99",
            "--semantic-search-mode",
            "all",
        ]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.search_semantic_index",
        return_value=_search_result(),
    ) as operation:
        assert dispatch_direct(args) == 0

    kwargs = operation.call_args.kwargs
    assert kwargs["local_files_only"] is True
    assert kwargs["include_text"] is True
    assert kwargs["include_images"] is True
    assert kwargs["include_lexical"] is True
    assert kwargs["limit"] == 7
    assert kwargs["max_vectors"] == 99
    output = capsys.readouterr().out
    assert "SEMANTIC_RANKING name=semantic_text available=1 complete=1" in output
    assert "LEXICAL_RANKING name=fts_pdf availability=available" in output
    assert "SEMANTIC_HIT rank=1" in output
    assert not (tmp_path / "framework.lock").exists()


def test_semantic_search_emits_bounded_item_diagnostic_trace(
    tmp_path, capsys,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory", str(tmp_path),
            "--semantic-search", "transformador",
            "--semantic-diagnostic-item", "item:pdf:1",
        ]
    )
    validate_arguments(args)
    result = _search_result()
    ranking = replace(
        result.rankings[0],
        provenance={
            "target_diagnostics": [{
                "item_id": "item:pdf:1", "stage": "candidate_selected",
                "observed_in_published_scope": True, "within_candidate_window": True,
                "raw_score": 0.81, "candidate_rank": 1, "ref_id": 1,
                "source_kind": "pdf", "source_status": "done", "snippet": "transformador",
            }],
            "candidate_selection": {"cutoff_reason": "top_k"},
        },
    )
    with patch(
        "neocortex.semantic.semantic_service.search_semantic_index",
        return_value=replace(result, rankings=(ranking,)),
    ):
        assert dispatch_direct(args) == 0
    output = capsys.readouterr().out
    trace_lines = [line for line in output.splitlines() if line.startswith("SEMANTIC_ITEM_DIAGNOSTIC ")]
    assert len(trace_lines) == 1
    assert 'item="item:pdf:1"' in trace_lines[0]
    trace = json.loads(trace_lines[0].split(" trace=", 1)[1])
    assert trace["stages"]["threshold"]["candidate_selection"]["cutoff_reason"] == "top_k"
    assert trace["stages"]["presentation"]["status"] == "present"


def test_semantic_search_incomplete_exact_scan_returns_two(tmp_path, capsys) -> None:
    args = build_parser().parse_args(
        ["--state-directory", str(tmp_path), "--semantic-search", "breaker"]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.search_semantic_index",
        return_value=_search_result(complete=False),
    ):
        assert dispatch_direct(args) == 2
    output = capsys.readouterr().out
    assert "complete=0" in output
    assert "reason=max_vectors_reached" in output
    assert "next_cursor=25" in output


def test_semantic_search_reports_calibrated_abstention(tmp_path, capsys) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-search",
            "tema ausente",
            "--semantic-search-mode",
            "text",
        ]
    )
    validate_arguments(args)
    result = _search_result()
    ranking = replace(
        result.rankings[0],
        provenance={
            "retrieval_abstention": {
                "query_abstained": True,
                "abstention_reason": "all_candidates_below_calibrated_source_floor",
            }
        },
    )
    result = replace(result, rankings=(ranking,), lexical_rankings=(), fused=())
    with patch(
        "neocortex.semantic.semantic_service.search_semantic_index",
        return_value=result,
    ):
        assert dispatch_direct(args) == 0

    output = capsys.readouterr().out
    assert "hits=0 abstained=1" in output
    assert "abstention_reason=all_candidates_below_calibrated_source_floor" in output
    assert "calibrated_abstentions=1 fused_hits=0" in output


def test_semantic_search_escapes_unencodable_corpus_text_on_cp1252(
    tmp_path,
    monkeypatch,
) -> None:
    class StrictCp1252Console:
        encoding = "cp1252"

        def __init__(self) -> None:
            self.parts: list[str] = []

        def write(self, value: str) -> int:
            value.encode(self.encoding)
            self.parts.append(value)
            return len(value)

        def flush(self) -> None:
            return None

        def getvalue(self) -> str:
            return "".join(self.parts)

    args = build_parser().parse_args(
        ["--state-directory", str(tmp_path), "--semantic-search", "breaker"]
    )
    validate_arguments(args)
    console = StrictCp1252Console()
    with patch(
        "neocortex.semantic.semantic_service.search_semantic_index",
        return_value=_search_result(
            source_identity="page:\ufeff1",
            path="C:/corpus/ficha\uf0b7.pdf",
            snippet="protección \uf0b7 diferencial",
        ),
    ):
        monkeypatch.setattr(sys, "stdout", console)
        assert dispatch_direct(args) == 0

    output = console.getvalue()
    assert "SEMANTIC_HIT rank=1" in output
    assert "\\ufeff" in output
    assert output.count("\\uf0b7") == 2


def test_offline_semantic_failure_names_explicit_local_model_cache(
    tmp_path,
    capsys,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-search",
            "breaker",
            "--semantic-search-mode",
            "text",
        ]
    )
    validate_arguments(args)
    with patch(
        "neocortex.semantic.semantic_service.search_semantic_index",
        side_effect=RuntimeError("model weights are not cached"),
    ):
        assert dispatch_direct(args) == 2
    output = capsys.readouterr().out
    assert "ERROR semantic-search RuntimeError" in output
    assert "--semantic-model-cache" in output
    assert "outside an offline run" in output


def test_semantic_classification_is_reported_as_advisory(tmp_path, capsys) -> None:
    args = build_parser().parse_args(
        ["--state-directory", str(tmp_path), "--semantic-classify", "all"]
    )
    validate_arguments(args)
    result = SemanticClassificationResult(
        semantic_database=tmp_path / "semantic.sqlite3",
        ontology_id="neocortex-industrial",
        ontology_version="ontology-v1",
        passes=(
            SemanticEvidencePassResult(
                indexed_model_signature="indexed",
                query_model_signature="query",
                vector_space="space",
                prototypes=12,
                entities_scored=4,
                evidence_staged=8,
                stale_evidence_deactivated=1,
            ),
        ),
    )
    with patch(
        "neocortex.semantic.semantic_service.classify_semantic_index",
        return_value=result,
    ) as operation:
        assert dispatch_direct(args) == 0
    assert operation.call_args.kwargs["local_files_only"] is True
    output = capsys.readouterr().out
    assert "SEMANTIC_CLASSIFICATION" in output
    assert "advisory=1" in output
    assert "abstained=0" in output
    assert "authority=advisory" in output
    assert (tmp_path / "framework.lock").is_file()


def test_semantic_evidence_is_read_only_and_never_grants_policy_authority(
    tmp_path,
    capsys,
) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-evidence",
            "item:pdf:1",
        ]
    )
    validate_arguments(args)
    evidence = SemanticEvidence(
        item_id="item:pdf:1",
        source_entity_id="chunk:1",
        ontology_id="neocortex-industrial",
        ontology_version="ontology-v1",
        concept_id="industrial.equipment.transformer",
        prototype_id="prototype:1",
        query_model_signature="query",
        indexed_model_signature="indexed",
        vector_space="space",
        score=0.77,
        rank=1,
        generation_id=7,
        calibration_status=CalibrationStatus.UNCALIBRATED,
        disposition=EvidenceDisposition.ADVISORY,
        provenance={"authority": "advisory-only"},
    )
    with patch(
        "neocortex.semantic.semantic_state.list_semantic_evidence",
        return_value=(evidence,),
    ) as operation:
        assert dispatch_direct(args) == 0

    assert operation.call_args.kwargs["item_id"] == "item:pdf:1"
    assert operation.call_args.kwargs["limit"] == 101
    output = capsys.readouterr().out
    assert "SEMANTIC_EVIDENCE item=item:pdf:1 count=1" in output
    assert "limit=100 truncated=0" in output
    assert "disposition=advisory authority=advisory" in output
    assert not (tmp_path / "framework.lock").exists()


def test_semantic_evidence_limit_reports_truncation(tmp_path, capsys) -> None:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--semantic-evidence",
            "item:pdf:1",
            "--semantic-evidence-limit",
            "1",
        ]
    )
    validate_arguments(args)
    evidence = SemanticEvidence(
        item_id="item:pdf:1",
        source_entity_id="chunk:1",
        ontology_id="neocortex-industrial",
        ontology_version="ontology-v1",
        concept_id="industrial.equipment.transformer",
        prototype_id="prototype:1",
        query_model_signature="query",
        indexed_model_signature="indexed",
        vector_space="space",
        score=0.77,
        rank=1,
    )
    with patch(
        "neocortex.semantic.semantic_state.list_semantic_evidence",
        return_value=(evidence, evidence),
    ) as operation:
        assert dispatch_direct(args) == 0

    assert operation.call_args.kwargs["limit"] == 2
    output = capsys.readouterr().out
    assert "count=1 limit=1 truncated=1" in output


# endregion [04]
