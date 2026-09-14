from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.runtime.scratch import ScratchManager, ScratchState
from neocortex.semantic import semantic_planner
from neocortex.semantic.semantic_service_contracts import SemanticSourcePlan


def _patch_empty_owner_snapshots(monkeypatch, database: Path) -> None:
    image_source = SemanticSourcePlan(
        source_kind="image",
        database=database,
        schema_version=1,
        resources=0,
        sections=0,
        chunks=0,
        embedding_entities=0,
        source_bytes=0,
        section_text_bytes=0,
        input_bytes=0,
        snapshot_xxh3_128="0" * 32,
    )
    monkeypatch.setattr(
        semantic_planner,
        "_plan_source_snapshots",
        lambda *_args, **_kwargs: (image_source,),
    )
    monkeypatch.setattr(
        semantic_planner,
        "_semantic_reuse_snapshot",
        lambda *_args, **_kwargs: (None, "0" * 32),
    )


def test_semantic_plan_retires_registered_workspace_on_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    _patch_empty_owner_snapshots(monkeypatch, tmp_path / "images.sqlite3")

    plan = semantic_planner.plan_semantic_index(
        tmp_path,
        scope="image",
        embed_ocr_text=False,
        scratch_directory=scratch,
    )

    assert plan.state_mutated is False
    assert plan.jobs_created == 0
    assert plan.scratch_storage_bytes > 0
    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()
    assert ScratchManager(
        scratch,
        owner="semantic-planner",
        create_root=False,
    ).records() == ()


def test_semantic_plan_retains_registered_workspace_after_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)

    class PlannedFailure(RuntimeError):
        pass

    def fail_after_workspace_creation(*_args, **_kwargs):
        raise PlannedFailure("fixture planner failure")

    monkeypatch.setattr(
        semantic_planner,
        "_plan_source_snapshots",
        fail_after_workspace_creation,
    )
    monkeypatch.setattr(
        semantic_planner,
        "_semantic_reuse_snapshot",
        lambda *_args, **_kwargs: (None, "0" * 32),
    )

    with pytest.raises(PlannedFailure, match="fixture planner failure"):
        semantic_planner.plan_semantic_index(
            tmp_path,
            scope="image",
            embed_ocr_text=False,
            scratch_directory=scratch,
        )

    records = ScratchManager(
        scratch,
        owner="semantic-planner",
        create_root=False,
    ).records()
    assert len(records) == 1
    record = records[0]
    assert record.state is ScratchState.FAILED_RETAINED
    assert record.owner == "semantic-planner"
    assert record.run_id is None
    assert record.metadata["component"] == "semantic-planner"
    assert record.metadata["operation"] == "plan_semantic_index"
    assert record.path.parent == scratch
    assert record.path.is_dir()
    assert (record.path / "manifest.json").is_file()
    assert not (tmp_path / "semantic.sqlite3").exists()


def test_registered_adapter_passes_contract_to_runtime_service(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    calls: list[tuple[str, object]] = []

    class FakeWorkspace:
        path = scratch / "scratch-fixture"

        def complete(self):
            calls.append(("complete", None))

        def fail(self, reason):
            calls.append(("fail", reason))

    class FakeManager:
        def __init__(self, root, *, owner, create_root):
            calls.append(("manager", (root, owner, create_root)))

        def create(self, *, run_id, retain_on_success, metadata):
            calls.append(("create", (run_id, retain_on_success, metadata)))
            return FakeWorkspace()

    runtime_module = __import__(
        "neocortex.runtime.scratch",
        fromlist=["ScratchManager"],
    )
    monkeypatch.setattr(runtime_module, "ScratchManager", FakeManager)

    with pytest.raises(RuntimeError, match="service seam"):
        with semantic_planner._registered_scratch_workspace(
            scratch,
            metadata={"component": "semantic-planner"},
        ):
                raise RuntimeError("service seam")

    assert calls == [
        ("manager", (scratch, "semantic-planner", True)),
        ("create", (None, False, {"component": "semantic-planner"})),
        ("fail", "RuntimeError: service seam"),
    ]
