"""Public full-run defaults, recovery identity and cooperative work limits."""

from argparse import Namespace
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_semantic import (
    _integrated_semantic_budget,
    _pending_integrated_source_run,
    _semantic_resume_args,
    _select_integrated_sources,
)
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.state_publication import (
    StateOwnerHead,
    StatePublicationConflictError,
    StatePublicationRecoveryRequired,
    begin_state_publication,
    publication_idempotency_key,
    record_state_publication,
)
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.semantic.semantic_work_budget import SemanticWorkBudget


def _args(root: Path, *extra: str):
    args = build_parser().parse_args([
        "--root", str(root), "--state-directory", str(root.parent / "state"), *extra,
    ])
    validate_arguments(args)
    return args






def test_explicit_semantic_limit_does_not_restore_other_hidden_limits(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    args = _args(root, "--all", "--semantic-max-items", "2")
    budget = _integrated_semantic_budget(args, None)
    assert budget.max_items == 2
    assert budget.max_new_jobs is None and budget.deadline is None
    assert budget.try_admit_item() and budget.try_admit_item()
    assert not budget.try_admit_item()
    assert budget.truncation_reason == "max_items"


def test_cancellation_is_checked_even_without_a_deadline():
    cancelled = False
    budget = SemanticWorkBudget(cancellation_check=lambda: cancelled)
    assert budget.try_admit_item()
    cancelled = True
    with pytest.raises(KeyboardInterrupt):
        budget.checkpoint()
    assert budget.truncation_reason == "cancelled"


def test_fresh_state_recovery_preflight_is_read_only(tmp_path: Path):
    from neocortex.api.cli.cli_semantic import recover_pending_integrated_semantic

    state = tmp_path / "not-created" / "state"
    assert recover_pending_integrated_semantic(Namespace(state_directory=state)) == 0
    assert not state.exists()


def test_missing_text_model_keeps_independent_visual_scope_and_partial_status(tmp_path: Path):
    from neocortex.api.cli import cli_semantic
    from neocortex.semantic.semantic_config import SemanticModelUnavailableError
    from neocortex.semantic.semantic_models import GenerationSummary
    from neocortex.semantic.semantic_service_contracts import GenerationWorkResult, SemanticIndexResult

    args = _args(tmp_path, "--all")
    args.semantic_index = "all"
    execution = cli_semantic._SemanticIndexExecution(
        args, cli_semantic._semantic_text_model("quality"), ("pdf",),
        SemanticWorkBudget(), None, None,
    )
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append("text")
        raise SemanticModelUnavailableError("local_snapshot_missing")

    def visual(*_args, **kwargs):
        calls.append("image")
        assert kwargs["embed_ocr_text"] is False
        summary = GenerationSummary(1, "image-fixture", "fixture", "ready", 0, 0, 1, 0, 0, {})
        return SemanticIndexResult(tmp_path / "semantic.sqlite3", ("image",), 1, 0,
                                   (GenerationWorkResult(summary, 1, 0, 1, 0),))

    cli_semantic._execute_semantic_index_scopes(execution, text_operation=unavailable, image_operation=visual)
    assert calls == ["text", "image"]
    assert cli_semantic._complete_semantic_index_execution(
        execution, incomplete_is_error=True, print_output=False
    ) == 2


def _pending_fixture(tmp_path: Path, *, modern_budget: bool = False):
    root = tmp_path / "corpus"
    root.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    metadata = root.stat()
    with FrameworkState(state_root / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(root, None)
        manifest = RunManifest(
            run_id=run_id, run_kind="initial", root=str(root),
            root_identity=(metadata.st_dev, metadata.st_ino, stat_birthtime_ns(metadata)),
            selected_routes=("text",),
        ).event_payload()
        state.publish_run_manifest(run_id, manifest)
        state.publish_run_stage(
            run_id, "semantic", "partial",
            details={
                "selected_sources": ["text"], "image_available": False,
                "complete_all": modern_budget,
                "semantic_budget_version": 2 if modern_budget else 1,
                "semantic_budget": {
                    "max_items": None if modern_budget else 17,
                    "max_new_jobs": None if modern_budget else 37,
                    "time_budget_seconds": None if modern_budget else 120.0,
                },
            },
        )
    for number in range(3):
        record_state_publication(
            state_root, operation="framework-all-semantic", owners=("semantic",),
            status="complete", idempotency_key=f"fixture:complete:{number}",
            owner_heads=(StateOwnerHead("semantic", 16, "a" * 64),),
        )
    transaction = begin_state_publication(
        state_root, operation="framework-all-semantic", owners=("semantic",),
        idempotency_key=publication_idempotency_key("framework-all-semantic", run_id, ("text",), False),
        manifest_sha256=manifest["digest"][7:],
    )
    return root, state_root, run_id, transaction


def test_pending_producer_is_resolved_from_manifest_and_original_raw_key(tmp_path: Path):
    _root, state, run_id, _pending = _pending_fixture(tmp_path)
    before = (state / "state-publication-journal.jsonl").read_bytes()
    assert _pending_integrated_source_run(state) == run_id
    assert (state / "state-publication-journal.jsonl").read_bytes() == before


def test_recovery_rejects_a_descendant_with_another_corpus(tmp_path: Path):
    from neocortex.api.cli.cli_semantic import recover_pending_integrated_semantic

    root, state_root, source_id, _pending = _pending_fixture(tmp_path)
    other = tmp_path / "other-corpus"
    other.mkdir()
    metadata = other.stat()
    with FrameworkState(state_root / "framework.sqlite3") as state:
        state.fail_initial_run(source_id)
        child = state.begin_operational_run(other, run_kind="resume", source_run_id=source_id)
        state.publish_run_manifest(child, RunManifest(
            run_id=child, run_kind="resume", root=str(other),
            root_identity=(metadata.st_dev, metadata.st_ino, stat_birthtime_ns(metadata)),
            selected_routes=(), source_run_id=source_id,
        ).event_payload())
        state.fail_initial_run(child)
    args = _args(root, "--all")
    args.resume_run = child
    before = (state_root / "state-publication-journal.jsonl").read_bytes()
    with pytest.raises(StatePublicationRecoveryRequired, match="different corpus"):
        recover_pending_integrated_semantic(args, print_output=False)
    assert (state_root / "state-publication-journal.jsonl").read_bytes() == before


def test_pending_source_selection_cannot_be_changed_silently(tmp_path: Path):
    _root, state_root, run_id, _pending = _pending_fixture(tmp_path)
    with FrameworkState(state_root / "framework.sqlite3") as state:
        state.publish_run_stage(
            run_id, "semantic", "partial",
            details={"selected_sources": ["pdf"], "image_available": False},
            idempotency_key="changed:selection",
        )
    with pytest.raises(StatePublicationConflictError, match="input contract changed"):
        _pending_integrated_source_run(state_root)


def test_modern_unlimited_semantic_budget_can_be_resumed(tmp_path: Path):
    _root, state_root, run_id, _pending = _pending_fixture(tmp_path, modern_budget=True)
    result = _semantic_resume_args(Namespace(state_directory=state_root), run_id)
    assert result is not None
    assert result.semantic_source == ["text"]
    assert result.semantic_max_items is None
    assert result.semantic_max_new_jobs is None
    assert result.semantic_time_budget_seconds is None


def test_orchestrator_rejects_pending_publication_before_inventory(tmp_path: Path, monkeypatch):
    root, state_root, _run_id, _pending = _pending_fixture(tmp_path)
    orchestrator = FrameworkOrchestrator(FrameworkConfig(root=root, state_directory=state_root))

    def forbidden(*_args, **_kwargs):
        pytest.fail("inventory must not run before publication recovery")

    monkeypatch.setattr(orchestrator, "_prepare_initial_run", forbidden)
    with pytest.raises(StatePublicationRecoveryRequired):
        orchestrator.run()


def test_public_cli_reports_recovery_without_traceback(tmp_path: Path, capsys):
    root = tmp_path / "corpus"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    record_state_publication(state, operation="fixture-prior", owners=("semantic",),
                             status="complete", idempotency_key="prior",
                             owner_heads=(StateOwnerHead("semantic", 1, "a" * 64),))
    begin_state_publication(state, operation="framework-all-semantic", owners=("semantic",), idempotency_key="orphan")
    result = main(["--all", "--root", str(root), "--state-directory", str(state)])
    output = capsys.readouterr()
    assert result == 2
    assert "recovery_required" in output.err
    assert "Traceback" not in output.err + output.out


def test_initial_unbound_marker_keeps_epoch_zero_compatibility(tmp_path: Path):
    from neocortex.api.cli.cli_semantic import recover_pending_integrated_semantic
    from neocortex.persistence.state_publication import read_state_publication_state

    pending = begin_state_publication(tmp_path, operation="framework-all-semantic",
                                       owners=("semantic",), idempotency_key="unbound")
    assert recover_pending_integrated_semantic(Namespace(state_directory=tmp_path)) == 0
    view = read_state_publication_state(tmp_path)
    assert not view.pending and view.epoch.epoch == 0
    assert pending.prepared.owner_heads == ()


@pytest.mark.parametrize("selection", (None, ["text"]))
def test_empty_source_cache_does_not_require_a_model(tmp_path: Path, selection):
    from neocortex.capabilities.formats.text.text_state import initialize_text_state

    state = tmp_path / "state"
    state.mkdir()
    initialize_text_state(state / "text.sqlite3")
    args = Namespace(state_directory=state, semantic_source=selection)
    sources, images = _select_integrated_sources(args, None)
    assert sources == () and images is False
    assert "text" in args._semantic_source_empty
    assert args._semantic_source_unavailable == {}


def test_unavailable_route_does_not_hide_independent_explicit_source(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    from tests.test_semantic_source_heads import _pdf_state

    _pdf_state(state_root / "pdf.sqlite3")
    (state_root / "audio.sqlite3").touch()
    with FrameworkState(state_root / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", 0)
        state.begin_route_runs(run_id, ("pdf", "audio"))
        state.complete_route_run(run_id, "pdf", {"candidates": 1})
        state.fail_route_run(run_id, "audio", RuntimeError("fixture unavailable"))
    args = Namespace(state_directory=state_root, semantic_source=["pdf", "audio"])
    sources, images = _select_integrated_sources(args, run_id)
    assert sources == ("pdf",) and images is False
    assert args._semantic_source_unavailable == {"audio": "route_unavailable"}


def test_source_budget_interrupts_sql_not_only_before_or_after(tmp_path: Path, monkeypatch):
    import sqlite3
    from neocortex.semantic import semantic_source_budget as controls
    from neocortex.semantic.semantic_sources import _readonly_database
    from neocortex.semantic.semantic_work_budget import SemanticIndexDeadlineExceeded

    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture(value INTEGER)")
    now = 0.0
    calls = 0
    original = controls._sqlite_progress

    def progress():
        nonlocal now, calls
        calls += 1
        now = 11.0
        return original()

    monkeypatch.setattr(controls, "_sqlite_progress", progress)
    budget = SemanticWorkBudget(deadline=10.0, _clock=lambda: now)
    with pytest.raises(SemanticIndexDeadlineExceeded):
        with controls.semantic_source_read_budget(budget):
            with _readonly_database(database) as connection:
                connection.execute(
                    "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM numbers WHERE n<1000000) SELECT sum(n) FROM numbers"
                ).fetchone()
    assert calls > 0


def test_incompatible_recovery_preserves_building_generation(tmp_path: Path):
    from neocortex.semantic.semantic_generation_repository import start_embedding_generation
    from neocortex.semantic.semantic_models import EmbeddingModality
    from neocortex.semantic.semantic_schema import SemanticStateError, initialize_semantic_state, semantic_database
    from neocortex.semantic.semantic_state import register_embedding_model
    from tests.test_functional_defaults_publication_heads import _model

    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    model = _model("preserved-building", EmbeddingModality.TEXT)
    register_embedding_model(database, model, allow_test_provider=True)
    generation = start_embedding_generation(
        database, model_signature=model.model_signature,
        processing_signature="original", provenance={"sources": ["text"]},
        materialize_base=False,
    )
    with semantic_database(database, readonly=True) as connection:
        before = tuple(connection.execute("SELECT * FROM embedding_generations").fetchone())
    budget = SemanticWorkBudget(preserve_existing_generations=True)
    with pytest.raises(SemanticStateError, match="incompatible"):
        start_embedding_generation(
            database, model_signature=model.model_signature,
            processing_signature="changed", provenance={"sources": ["text"]},
            materialize_base=False, work_budget=budget,
        )
    with semantic_database(database, readonly=True) as connection:
        rows = connection.execute("SELECT * FROM embedding_generations").fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == before
    assert start_embedding_generation(
        database, model_signature=model.model_signature,
        processing_signature="original", provenance={"sources": ["text"]},
        materialize_base=False, work_budget=budget,
    ) == generation


@pytest.mark.parametrize("descendant,unclean", ((False, False), (True, False), (False, True)))
def test_integrated_recovery_rolls_same_pending_forward_with_a_durable_run(tmp_path: Path, monkeypatch, descendant, unclean):
    import hashlib
    import json
    from neocortex.api.cli import cli_semantic
    from neocortex.capabilities.formats.image.state import initialize_image_state
    from neocortex.capabilities.formats.text.text_state import initialize_text_state
    from neocortex.persistence.state_publication import read_state_publication_state
    from neocortex.semantic.semantic_generation_repository import (
        finalize_embedding_generation, prepare_embedding_generation, start_embedding_generation,
    )
    from neocortex.semantic.semantic_models import EmbeddingModality
    from neocortex.semantic.semantic_schema import initialize_semantic_state, semantic_database
    from neocortex.semantic.semantic_service_contracts import GenerationWorkResult, SemanticIndexResult
    from neocortex.semantic.semantic_state import register_embedding_model
    from tests.test_functional_defaults_publication_heads import _empty_generation, _model

    root = tmp_path / "corpus"
    root.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    database = state_root / "semantic.sqlite3"
    initialize_semantic_state(database)
    initialize_text_state(state_root / "text.sqlite3")
    initialize_image_state(state_root / "image.sqlite3")
    text_model = _model("recovery-text", EmbeddingModality.TEXT)
    image_model = _model("recovery-image", EmbeddingModality.IMAGE)
    for model in (text_model, image_model):
        register_embedding_model(database, model, allow_test_provider=True)
    _empty_generation(database, text_model, processing_signature="base-text", started_ns=100)
    image_generation = _empty_generation(database, image_model, processing_signature="base-image", started_ns=200)
    pending_generation = start_embedding_generation(
        database, model_signature=text_model.model_signature, processing_signature="pending-text",
        provenance={"sources": ["text"]}, materialize_base=False, started_ns=300,
    )
    stat = root.stat()
    with FrameworkState(state_root / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(root, None)
        manifest = RunManifest(
            run_id=run_id, run_kind="initial", root=str(root),
            root_identity=(stat.st_dev, stat.st_ino, stat_birthtime_ns(stat)),
            selected_routes=("text", "image"),
        ).event_payload()
        state.publish_run_manifest(run_id, manifest)
        state.publish_run_stage(run_id, "semantic", "partial", details={
            "selected_sources": ["text"], "image_available": True,
            "semantic_budget_version": 2, "complete_all": True,
            "semantic_budget": {"max_items": None, "max_new_jobs": None, "time_budget_seconds": None},
        })
        if not unclean:
            state.fail_initial_run(run_id)
        policy_run_id = run_id
        if descendant:
            policy_run_id = state.begin_operational_run(root, run_kind="resume", source_run_id=run_id)
            child_manifest = RunManifest(
                run_id=policy_run_id, run_kind="resume", root=str(root),
                root_identity=(stat.st_dev, stat.st_ino, stat_birthtime_ns(stat)),
                selected_routes=(), source_run_id=run_id,
            ).event_payload()
            state.publish_run_manifest(policy_run_id, child_manifest)
            state.publish_run_stage(policy_run_id, "semantic", "partial", details={
                "selected_sources": ["text"], "image_available": False,
                "semantic_budget_version": 2,
                "semantic_budget": {"max_items": 1, "max_new_jobs": 1, "time_budget_seconds": 1.0},
            })
            state.fail_initial_run(policy_run_id)
    legacy_digest = hashlib.sha256(json.dumps(
        {"owner": "semantic", "generation_id": image_generation,
         "model_signature": image_model.model_signature, "sources": ["text"]},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    for number in range(3):
        record_state_publication(
            state_root, operation="framework-all-semantic", owners=("semantic",),
            status="complete", idempotency_key=f"past:{number}",
            owner_heads=(StateOwnerHead("semantic", image_generation, legacy_digest),),
        )
    pending = begin_state_publication(
        state_root, operation="framework-all-semantic", owners=("semantic",),
        idempotency_key=publication_idempotency_key("framework-all-semantic", run_id, ("text",), True),
        manifest_sha256=manifest["digest"][7:],
    ).prepared
    calls = []

    def finish_owner(args, *, result_sink, **_kwargs):
        assert args._semantic_work_budget.preserve_existing_generations
        assert args.semantic_source == ["text"]
        assert args._semantic_publication_event_id == pending.event_id
        generation = start_embedding_generation(
            database, model_signature=text_model.model_signature, processing_signature="pending-text",
            provenance={"sources": ["text"]}, materialize_base=True,
            work_budget=args._semantic_work_budget,
        )
        assert generation == pending_generation
        assert prepare_embedding_generation(database, generation, enumeration_complete=True) is None
        text_summary = finalize_embedding_generation(database, generation, completed_ns=400)
        from neocortex.semantic.semantic_generation_repository import generation_summary

        image_summary = generation_summary(database, image_generation)
        result_sink("text", SemanticIndexResult(database, ("text",), 0, 0, (GenerationWorkResult(text_summary, 0, 0, 0, 0),)))
        result_sink("image", SemanticIndexResult(database, ("image",), 0, 0, (GenerationWorkResult(image_summary, 0, 0, 0, 0),)))
        calls.append(generation)
        return 0

    monkeypatch.setattr(cli_semantic, "run_semantic_index", finish_owner)
    args = _args(root, "--all")
    if descendant:
        args.resume_run = policy_run_id
    assert cli_semantic.recover_pending_integrated_semantic(args, print_output=False) == 0
    assert calls == [pending_generation]
    view = read_state_publication_state(state_root)
    assert view.status == "complete" and view.epoch.epoch == 4
    assert view.publication is not None and view.publication.idempotency_key == pending.idempotency_key
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("SELECT status FROM embedding_generations WHERE generation_id=?", (pending_generation,)).fetchone()[0] == "ready"
        assert connection.execute("SELECT COUNT(*) FROM embedding_generations").fetchone()[0] == 3
    with FrameworkState(state_root / "framework.sqlite3") as state:
        rows = state._connection.execute("SELECT run_id,status,source_run_id FROM initial_runs ORDER BY run_id").fetchall()
        assert len(rows) == 2 + int(descendant)
        assert rows[-1][1] == "completed" and rows[-1][2] == policy_run_id
    assert cli_semantic.recover_pending_integrated_semantic(args, print_output=False) == 0
    assert calls == [pending_generation]
