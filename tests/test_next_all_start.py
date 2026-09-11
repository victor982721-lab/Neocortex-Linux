"""Fresh integrated --all starts do not inherit an unusable Semantic stage."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.api.cli import cli_semantic
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.state_publication import (
    StateOwnerHead,
    begin_state_publication,
    publication_idempotency_key,
    read_state_publication_state,
    record_state_publication,
)
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.runtime.orchestration.run_manifest import RunManifest


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _fresh_args(state_directory: Path, *, resume_run: int | None = None) -> Namespace:
    return Namespace(
        all=resume_run is None,
        resume_run=resume_run,
        root=state_directory.parent / "corpus",
        state_directory=state_directory,
        run_time_budget_seconds=5.0,
        semantic_time_budget_seconds=10.0,
        semantic_max_items=7,
        semantic_max_new_jobs=11,
        semantic_source=None,
    )


def test_legacy_missing_semantic_budget_starts_fresh_all_without_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    root = state_directory.parent / "corpus"
    root.mkdir()
    args = _fresh_args(state_directory)
    root_metadata = root.stat()
    metadata = cli_semantic._PendingIntegratedMetadata(
        event_id="epoch:4:event:legacy",
        expected_epoch=4,
        pending_owners=("semantic",),
        previous_owners=("semantic",),
        manifest={
            "root": str(root),
            "root_identity": [root_metadata.st_dev, root_metadata.st_ino, stat_birthtime_ns(root_metadata)],
            "selected_routes": ["text"],
        },
    )
    monkeypatch.setattr(cli_semantic, "_read_pending_integrated_metadata", lambda *_args: metadata)
    monkeypatch.setattr(cli_semantic, "_semantic_resume_args", lambda *_args: pytest.fail("fresh --all must not resume"))
    monkeypatch.setattr(
        "neocortex.persistence.state_publication.read_state_publication_state",
        lambda *_args: SimpleNamespace(status="blocked", epoch=SimpleNamespace(owners=())),
    )

    observed_heads = (
        StateOwnerHead("semantic", 1, "a" * 64, 7),
        StateOwnerHead("code", 0, "b" * 64, 7),
    )
    observer_calls: list[bool] = []

    def observe(*_args: object, include_code: bool, **_kwargs: object) -> tuple[StateOwnerHead, ...]:
        observer_calls.append(include_code)
        return observed_heads

    monkeypatch.setattr(
        "neocortex.semantic.semantic_publication_heads.observe_integrated_owner_heads",
        observe,
    )
    restart_calls: list[dict[str, object]] = []

    now = [100.0]

    def restart(_state: Path, **kwargs: object) -> object:
        restart_calls.append(kwargs)
        verify = kwargs["verify_owner_heads"]
        assert callable(verify)
        assert verify() == observed_heads
        now[0] = 100.5
        return SimpleNamespace(owners=("semantic", "code"), epoch=5)

    monkeypatch.setattr(
        "neocortex.persistence.state_publication.restart_state_publication_checkpoint",
        restart,
        raising=False,
    )
    def clock() -> float:
        return now[0]

    assert (
        cli_semantic.prepare_integrated_semantic_start(
            args,
            print_output=False,
            clock=clock,
        )
        == 0
    )

    assert len(restart_calls) == 1
    assert restart_calls[0]["event_id"] == "epoch:4:event:legacy"
    assert restart_calls[0]["expected_epoch"] == 4
    assert restart_calls[0]["owner_heads"] == observed_heads
    assert observer_calls == [True, True]
    assert args._semantic_publication_owners == ("semantic", "code")
    assert args.run_time_budget_seconds == pytest.approx(4.5)
    assert args.semantic_time_budget_seconds == pytest.approx(9.5)
    assert args.semantic_max_items == 7
    assert args.semantic_max_new_jobs == 11
    assert not hasattr(args, "_semantic_preserve_generations")


def test_stage_absent_is_restarted_from_real_framework_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    root_identity = root.stat()
    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(root, None)
        manifest = RunManifest(
            run_id=run_id,
            run_kind="initial",
            root=str(root),
            root_identity=(root_identity.st_dev, root_identity.st_ino, stat_birthtime_ns(root_identity)),
            selected_routes=("text",),
        ).event_payload()
        state.publish_run_manifest(run_id, manifest)
        state.fail_initial_run(run_id)

    old_head = StateOwnerHead("semantic", 1, "a" * 64)
    record_state_publication(
        state_directory,
        operation="framework-all-semantic",
        owners=("semantic",),
        status="complete",
        idempotency_key="old-complete",
        owner_heads=(old_head,),
    )
    pending = begin_state_publication(
        state_directory,
        operation="framework-all-semantic",
        owners=("semantic",),
        idempotency_key=publication_idempotency_key(
            "framework-all-semantic", run_id, ("text",), False
        ),
        manifest_sha256=manifest["digest"][7:],
        owner_heads=(old_head,),
    ).prepared
    args = _fresh_args(state_directory)
    observed_heads = (old_head, StateOwnerHead("code", 0, "c" * 64, 7))
    observer_calls: list[bool] = []

    def observe(*_args: object, include_code: bool, **_kwargs: object) -> tuple[StateOwnerHead, ...]:
        observer_calls.append(include_code)
        return observed_heads

    monkeypatch.setattr(
        "neocortex.semantic.semantic_publication_heads.observe_integrated_owner_heads",
        observe,
    )
    monkeypatch.setattr(
        cli_semantic,
        "_semantic_resume_args",
        lambda *_args: pytest.fail("fresh --all must not use the strict resume reader"),
    )

    assert cli_semantic.prepare_integrated_semantic_start(args, print_output=False) == 0
    view = read_state_publication_state(state_directory)
    assert not view.pending
    assert view.epoch.epoch == 2
    assert observer_calls == [True, True, True]
    assert args._semantic_publication_owners == ("semantic", "code")
    assert pending.event_id not in {item.event_id for item in view.pending}
    with FrameworkState(state_directory / "framework.sqlite3", existing_only=True) as state:
        assert state._connection.execute("SELECT COUNT(*) FROM initial_runs").fetchone()[0] == 1


def test_checkpoint_owner_union_survives_following_source_selection() -> None:
    args = Namespace(_semantic_publication_owners=("semantic", "code"))
    assert cli_semantic._integrated_publication_owners(args, ("pdf",)) == (
        "semantic",
        "code",
    )


@pytest.mark.parametrize("repair", [False, True])
def test_new_prepare_baseline_repairs_links_invalidated_by_code_route_only_when_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repair: bool,
) -> None:
    from neocortex.semantic import semantic_publication_heads

    heads = (StateOwnerHead("semantic", 3, "a" * 64, 7), StateOwnerHead("code", 2, "b" * 64, 7))
    calls = 0
    repairs = []

    def observe(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise semantic_publication_heads.PublicationHeadsRepairRequired("Code version advanced")
        return heads

    monkeypatch.setattr(semantic_publication_heads, "observe_integrated_owner_heads", observe)
    monkeypatch.setattr(semantic_publication_heads, "observe_semantic_generation_heads", lambda *_a, **_k: (("model", 3),))
    monkeypatch.setattr(
        "neocortex.code.search.code_semantic_links.deactivate_stale_code_embedding_links",
        lambda *_a, **kwargs: repairs.append(kwargs["published_heads"]),
    )
    if repair:
        assert cli_semantic._observe_integrated_heads(
            tmp_path, include_code=True, repair_stale_code_links=True
        ) == heads
        assert calls == 2 and repairs == [(("model", 3),)]
    else:
        with pytest.raises(cli_semantic.StatePublicationRecoveryRequired):
            cli_semantic._observe_integrated_heads(tmp_path, include_code=True)
        assert calls == 1 and repairs == []


def test_strict_resume_uses_explicit_stored_publication_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = {
        "selected_sources": ("pdf",),
        "selection_pending": False,
        "image_available": False,
        "max_items": 3,
        "max_new_jobs": 5,
        "time_budget_seconds": 7.0,
        "publication_owners": ("semantic", "code"),
        "details": {"semantic_text_profile": "quality"},
    }
    monkeypatch.setattr(cli_semantic, "_semantic_stage_for_resume", lambda *_args: spec)
    args = Namespace(
        state_directory=tmp_path,
        semantic_source=None,
        semantic_model_cache=None,
        semantic_threads=None,
        semantic_no_ocr=False,
    )
    effective = cli_semantic._semantic_resume_args(args, 25)
    assert effective is not None
    assert effective._semantic_publication_owners == ("semantic", "code")


def test_baseline_code_repair_failure_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neocortex.code.search import code_semantic_links
    from neocortex.semantic import semantic_publication_heads

    def observe(*_args: object, **_kwargs: object) -> None:
        raise semantic_publication_heads.PublicationHeadsRepairRequired("stale Code link")

    failure = code_semantic_links.CodeSemanticLinkError("repair unavailable")

    def repair(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(semantic_publication_heads, "observe_integrated_owner_heads", observe)
    monkeypatch.setattr(
        semantic_publication_heads, "observe_semantic_generation_heads", lambda *_a, **_k: (),
    )
    monkeypatch.setattr(code_semantic_links, "deactivate_stale_code_embedding_links", repair)
    with pytest.raises(cli_semantic.StatePublicationRecoveryRequired, match="repair unavailable") as caught:
        cli_semantic._observe_integrated_heads(
            tmp_path, include_code=True, repair_stale_code_links=True,
        )
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("boundary", ("owner_heads", "code_repair"))
def test_fresh_start_owner_failures_are_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    from neocortex.code.search.code_semantic_links import CodeSemanticLinkError
    from neocortex.semantic.semantic_publication_heads import PublicationHeadsError

    failure = (
        PublicationHeadsError("owner unavailable") if boundary == "owner_heads"
        else CodeSemanticLinkError("owner unavailable")
    )

    def reject(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(cli_semantic, "_fresh_integrated_checkpoint", reject)
    with pytest.raises(cli_semantic.StatePublicationRecoveryRequired, match="owner unavailable") as caught:
        cli_semantic.prepare_integrated_semantic_start(_fresh_args(tmp_path), print_output=False)
    assert caught.value.__cause__ is failure


def test_stale_code_projection_gets_one_bounded_repair_before_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neocortex.semantic import semantic_publication_heads

    args = _fresh_args(tmp_path / "state")
    controls = cli_semantic._IntegratedStartReadBudget(
        args,
        cancellation_check=None,
        clock=lambda: 100.0,
        metadata_timeout_seconds=10.0,
    )
    repair = semantic_publication_heads.PublicationHeadsRepairRequired(
        "stale code projection"
    )
    observer_calls = 0
    deactivated: list[tuple[object, ...]] = []

    def observe(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        nonlocal observer_calls
        observer_calls += 1
        if observer_calls == 1:
            raise repair
        return ("fresh-heads",)

    def generations(*_args: object, **_kwargs: object) -> tuple[tuple[str, int], ...]:
        return (("text-model", 17),)

    def deactivate(_state: Path, *, published_heads: tuple[tuple[str, int], ...], **_kwargs: object) -> int:
        deactivated.append(published_heads)
        return 1

    monkeypatch.setattr(semantic_publication_heads, "observe_integrated_owner_heads", observe)
    monkeypatch.setattr(semantic_publication_heads, "observe_semantic_generation_heads", generations)
    monkeypatch.setattr(
        "neocortex.code.search.code_semantic_links.deactivate_stale_code_embedding_links",
        deactivate,
    )
    assert cli_semantic._observe_fresh_integrated_heads(
        tmp_path,
        include_code=True,
        controls=controls,
    ) == ("fresh-heads",)
    assert observer_calls == 2
    assert deactivated == [(('text-model', 17),)]


def test_explicit_resume_keeps_the_strict_recovery_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _fresh_args(tmp_path / "state", resume_run=25)
    calls: list[tuple[Namespace, object, bool]] = []

    def recover(received: Namespace, *, progress: object, print_output: bool) -> int:
        calls.append((received, progress, print_output))
        return 7

    monkeypatch.setattr(cli_semantic, "recover_pending_integrated_semantic", recover)
    assert (
        cli_semantic.prepare_integrated_semantic_start(
            args,
            progress="progress",
            print_output=False,
        )
        == 7
    )
    assert calls == [(args, "progress", False)]


def test_fresh_start_preflight_honors_cancellation_before_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    args = _fresh_args(state_directory)
    monkeypatch.setattr(
        cli_semantic,
        "_read_pending_integrated_metadata",
        lambda *_args: pytest.fail("cancelled preflight must not read metadata"),
    )
    with pytest.raises(KeyboardInterrupt, match="preflight was cancelled"):
        cli_semantic.prepare_integrated_semantic_start(
            args,
            print_output=False,
            cancellation_check=lambda: True,
        )


def test_empty_existing_state_preflight_does_not_create_lock(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    assert cli_semantic.prepare_integrated_semantic_start(
        _fresh_args(directory), print_output=False
    ) == 0
    assert list(directory.iterdir()) == []


def test_pending_start_validates_effect_targets_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setattr(
        "neocortex.persistence.state_publication.read_state_publication_state",
        lambda *_args: SimpleNamespace(status="blocked", epoch=SimpleNamespace(owners=())),
    )

    def reject(*_args: object, **_kwargs: object) -> None:
        raise ValueError("protected state target")

    monkeypatch.setattr(cli_semantic, "_validate_semantic_state_write", reject)
    monkeypatch.setattr(
        "neocortex.runtime.control.locking.FrameworkRunLock",
        lambda *_args: pytest.fail("effect target must be validated before locking"),
    )
    with pytest.raises(ValueError, match="protected state target"):
        cli_semantic.prepare_integrated_semantic_start(_fresh_args(directory), print_output=False)
    assert list(directory.iterdir()) == []


def test_epoch_zero_unbound_marker_uses_existing_safe_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    args = _fresh_args(state_directory)
    begin_state_publication(
        state_directory,
        operation="framework-all-semantic",
        owners=("semantic",),
        idempotency_key="unbound-initial-fixture",
    )
    calls: list[Path] = []
    monkeypatch.setattr(cli_semantic, "_recover_pending_integrated_publication", calls.append)
    assert cli_semantic.prepare_integrated_semantic_start(args, print_output=False) == 0
    assert calls == [state_directory]
