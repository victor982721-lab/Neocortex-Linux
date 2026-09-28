"""Synthetic C05/C22 Framework EML integration tests."""

from __future__ import annotations

import email
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.capabilities.formats.archive.intake import SourceIdentity
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.progress import ProgressEvent
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator_pipeline import (
    _BoundedZipProgress,
    _select_new_zip_snapshots,
)
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.workflow.email_intake_orchestrator import run_email_intake_stage


TEST_CAPABILITIES = ("base", "inference")


def _real_stage_manifest(run_id: int, root: Path) -> dict[str, object]:
    return RunManifest(
        run_id=run_id,
        run_kind="initial",
        root=str(root),
        root_identity=(1, 2, -1),
        selected_routes=("text",),
        route_capabilities={"text": "safe_replay"},
        configuration={"fixture": "email-zip-stage"},
    ).event_payload()


def test_real_framework_state_keeps_initial_and_nested_zip_stage_identity(
    tmp_path: Path,
) -> None:
    """A completed initial ZIP stage must not block the EML-child ZIP stage."""

    root = tmp_path / "corpus"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _real_stage_manifest(run_id, root))
        assert state.publish_run_stage(
            run_id,
            "zip-intake",
            "running",
            details={"stage_name": "zip-intake", "source_scan_id": 1},
            idempotency_key="zip-intake:running",
        )
        assert state.publish_run_stage(
            run_id,
            "zip-intake",
            "completed",
            details={"stage_name": "zip-intake", "source_scan_id": 1, "successor_scan_id": 2},
            idempotency_key="zip-intake:completed",
        )
        assert state.publish_run_stage(
            run_id,
            "email-zip-intake",
            "running",
            details={"stage_name": "email-zip-intake", "source_scan_id": 3},
            idempotency_key="email-zip-intake:running",
        )
        assert state.publish_run_stage(
            run_id,
            "email-zip-intake",
            "completed",
            details={
                "stage_name": "email-zip-intake",
                "source_scan_id": 3,
                "successor_scan_id": 4,
                "nested": True,
            },
            idempotency_key="email-zip-intake:completed",
        )
        assert not state.publish_run_stage(
            run_id,
            "email-zip-intake",
            "completed",
            details={
                "stage_name": "email-zip-intake",
                "source_scan_id": 3,
                "successor_scan_id": 4,
                "nested": True,
            },
            idempotency_key="email-zip-intake:completed",
        )
        latest = state.read_run_stage_state(run_id)
        assert latest["zip-intake"]["status"] == "completed"
        assert latest["email-zip-intake"]["status"] == "completed"
        stages = state.read_run_stages(run_id)
        assert [stage["stage"] for stage in stages] == [
            "zip-intake",
            "zip-intake",
            "email-zip-intake",
            "email-zip-intake",
        ]


def test_nested_zip_progress_has_separate_operation_identity() -> None:
    events: list[ProgressEvent] = []
    progress = _BoundedZipProgress(
        events.append,
        total_hint=1,
        operation="email-zip-intake",
    )
    progress(
        ProgressEvent(
            "zip-intake",
            "process",
            "engine fixture",
            1,
            1,
            "ZIPs",
        )
    )
    progress.finish({"status": "applied", "containers_examined": 1})
    assert events
    assert {event.operation for event in events} == {"email-zip-intake"}
    assert {event.phase for event in events} == {"process"}


def test_real_framework_config_enables_safe_content_equivalent_policy() -> None:
    assert FrameworkConfig().email_allow_content_equivalent_reuse is True


def test_nested_zip_fixed_point_selects_only_new_identified_children(tmp_path: Path) -> None:
    old = tmp_path / "old.zip"
    fresh = tmp_path / "email-child.zip"
    pdf = tmp_path / "email-child.pdf"
    for path in (old, fresh, pdf):
        path.write_bytes(b"synthetic")

    def snap(path: Path) -> FileSnapshot:
        observed = path.stat()
        return FileSnapshot(
            str(path), observed.st_dev, observed.st_ino, observed.st_size,
            observed.st_mtime_ns, getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
        )

    old_snapshot, fresh_snapshot, pdf_snapshot = map(snap, (old, fresh, pdf))
    detected = {
        (fresh_snapshot.volume_id, fresh_snapshot.file_id, fresh_snapshot.size,
         fresh_snapshot.mtime_ns, fresh_snapshot.birthtime_ns): SimpleNamespace(
            mime="application/zip"
        ),
        (pdf_snapshot.volume_id, pdf_snapshot.file_id, pdf_snapshot.size,
         pdf_snapshot.mtime_ns, pdf_snapshot.birthtime_ns): SimpleNamespace(
            mime="application/pdf"
        ),
    }
    selected = _select_new_zip_snapshots(
        (old_snapshot, fresh_snapshot, pdf_snapshot),
        (str(fresh), str(pdf)),
        detected,
    )
    assert selected == (fresh_snapshot,)


def _eml(*, attachment: bool) -> bytes:
    message = email.message.EmailMessage()
    message["From"] = "sender@example.invalid"
    message["To"] = "receiver@example.invalid"
    message["Subject"] = "Synthetic message"
    message.set_content("body only" if not attachment else "body with attachment")
    if attachment:
        message.add_attachment(
            b"%PDF-1.7 synthetic child\n",
            maintype="application",
            subtype="pdf",
            filename="report.pdf",
        )
    return message.as_bytes()


def _eml_zip_attachment() -> bytes:
    message = email.message.EmailMessage()
    message["From"] = "sender@example.invalid"
    message["To"] = "receiver@example.invalid"
    message["Subject"] = "Synthetic nested ZIP"
    message.set_content("body with nested ZIP")
    message.add_attachment(
        b"synthetic ZIP payload",
        maintype="application",
        subtype="zip",
        filename="nested.zip",
    )
    return message.as_bytes()


class _Guard:
    def require_paths_allowed(self, *_paths: object) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def corpus_mutation_guard(self, _run_id: int) -> _Guard:
        return _Guard()

    def record_event(self, *args: object, **kwargs: object) -> None:
        self.events.append((*args, kwargs))


class _Boundary:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.access_policy = SimpleNamespace(root=root)

    def verify(self) -> None:
        return None


class _Dedup:
    def __init__(self, snapshot: FileSnapshot) -> None:
        self.snapshot = snapshot
        self.child: FileSnapshot | None = None

    def snapshots(self, _scan_id: int):
        yield self.snapshot

    def unique_snapshot_for_identity(self, _scan_id: int, _device: int, _inode: int):
        return self.child

    def snapshots_by_size(self, *_args: object, **_kwargs: object):
        if self.child is not None:
            yield self.child

    def observe_fingerprint(self, snapshot: FileSnapshot, _algorithm: str):
        from neocortex.deduplication.content_observation import observe_content_fingerprint

        return observe_content_fingerprint(snapshot, _algorithm)


def _run(tmp_path: Path, *, attachment: bool, apply: bool = True):
    root = tmp_path / "corpus"
    root.mkdir()
    parent = root / ("message.eml" if attachment else "body.eml")
    parent.write_bytes(_eml(attachment=attachment))
    observed = parent.stat()
    snapshot = FileSnapshot(
        str(parent), observed.st_dev, observed.st_ino, observed.st_size,
        observed.st_mtime_ns, getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
    )
    dedup = _Dedup(snapshot)
    state = _State()
    detected = SimpleNamespace(mime="message/rfc822")
    config = SimpleNamespace(
        email_intake_enabled=True,
        email_max_parts=100,
        email_max_depth=8,
        email_max_part_bytes=1_000_000,
        email_max_total_bytes=2_000_000,
        email_max_source_bytes=2_000_000,
        email_allow_content_equivalent_reuse=False,
        max_file_bytes=None,
        route="all",
        route_only=False,
    )
    cancellation = SimpleNamespace(checkpoint=lambda: None)
    result = run_email_intake_stage(
        root=root,
        state_directory=tmp_path / "state",
        config=config,
        state=state,
        run_id=1,
        boundary=_Boundary(root),
        dedup_index=dedup,
        scan_id=1,
        identified_types={
            (
                snapshot.volume_id,
                snapshot.file_id,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
            ): detected
        },
        apply=apply,
        cancellation=cancellation,
        progress=None,
    )
    return root, parent, snapshot, dedup, state, result


def test_body_only_email_never_creates_attachment_directories(tmp_path: Path) -> None:
    root, _parent, _snapshot, _dedup, _state, result = _run(tmp_path, attachment=False)
    assert result.status == "completed"
    assert result.parents_with_attachments == 0
    assert not (root / "Adjuntos_de_correos").exists()


def test_apply_and_replay_are_idempotent_and_replay_does_not_rescan(tmp_path: Path) -> None:
    root, _parent, snapshot, dedup, state, first = _run(tmp_path, attachment=True)
    assert first.filesystem_changed is True
    assert first.attachments_materialized == 1
    output_root = root / "Adjuntos_de_correos"
    destinations = tuple(path for path in output_root.iterdir() if path.is_dir())
    assert len(destinations) == 1
    assert any(path.suffix == ".pdf" for path in destinations[0].iterdir())

    # The second stage observes the same parent and manifest; no new physical
    # publication should request a successor inventory.
    config = SimpleNamespace(
        email_intake_enabled=True, email_max_parts=100, email_max_depth=8,
        email_max_part_bytes=1_000_000, email_max_total_bytes=2_000_000,
        email_max_source_bytes=2_000_000, email_allow_content_equivalent_reuse=False,
        max_file_bytes=None, route="all", route_only=False,
    )
    second = run_email_intake_stage(
        root=root, state_directory=tmp_path / "state", config=config, state=state,
        run_id=1, boundary=_Boundary(root), dedup_index=dedup, scan_id=1,
        identified_types={(
            snapshot.volume_id, snapshot.file_id, snapshot.size,
            snapshot.mtime_ns, snapshot.birthtime_ns,
        ): SimpleNamespace(mime="message/rfc822")},
        apply=True, cancellation=SimpleNamespace(checkpoint=lambda: None), progress=None,
    )
    assert second.filesystem_changed is False
    assert second.attachments_replayed == 1
    assert state.events


def test_cancelled_stage_does_not_create_destination(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    parent = root / "message.eml"
    parent.write_bytes(_eml(attachment=True))
    observed = parent.stat()
    snapshot = FileSnapshot(
        str(parent), observed.st_dev, observed.st_ino, observed.st_size,
        observed.st_mtime_ns, getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
    )
    dedup = _Dedup(snapshot)
    state = _State()
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls >= 1:
            raise KeyboardInterrupt("synthetic cancellation")

    config = SimpleNamespace(
        email_intake_enabled=True, email_max_parts=100, email_max_depth=8,
        email_max_part_bytes=1_000_000, email_max_total_bytes=2_000_000,
        email_max_source_bytes=2_000_000, email_allow_content_equivalent_reuse=False,
        max_file_bytes=None, route="all", route_only=False,
    )
    with pytest.raises(KeyboardInterrupt):
        run_email_intake_stage(
            root=root, state_directory=tmp_path / "state", config=config, state=state,
            run_id=1, boundary=_Boundary(root), dedup_index=dedup, scan_id=1,
            identified_types={(
                snapshot.volume_id, snapshot.file_id, snapshot.size,
                snapshot.mtime_ns, snapshot.birthtime_ns,
            ): SimpleNamespace(mime="message/rfc822")},
            apply=True, cancellation=SimpleNamespace(checkpoint=cancel), progress=None,
        )
    assert not (root / "Adjuntos_de_correos").exists()


def test_parent_rename_and_removed_child_directory_rebinds_by_identity(tmp_path: Path) -> None:
    root, parent, _snapshot, dedup, state, _first = _run(tmp_path, attachment=True)
    destination_root = root / "Adjuntos_de_correos"
    destination = next(path for path in destination_root.iterdir() if path.is_dir())
    child = next(path for path in destination.iterdir() if path.suffix == ".pdf")
    retained = root / "retained-child.pdf"
    child.rename(retained)
    destination.rmdir()
    renamed_parent = root / "renamed-message.eml"
    parent.rename(renamed_parent)

    observed_parent = renamed_parent.stat()
    dedup.snapshot = FileSnapshot(
        str(renamed_parent), observed_parent.st_dev, observed_parent.st_ino,
        observed_parent.st_size, observed_parent.st_mtime_ns,
        getattr(observed_parent, "st_birthtime_ns", observed_parent.st_ctime_ns),
    )
    observed_child = retained.stat()
    dedup.child = FileSnapshot(
        str(retained), observed_child.st_dev, observed_child.st_ino,
        observed_child.st_size, observed_child.st_mtime_ns,
        getattr(observed_child, "st_birthtime_ns", observed_child.st_ctime_ns),
    )
    config = SimpleNamespace(
        email_intake_enabled=True, email_max_parts=100, email_max_depth=8,
        email_max_part_bytes=1_000_000, email_max_total_bytes=2_000_000,
        email_max_source_bytes=2_000_000, email_allow_content_equivalent_reuse=False,
        max_file_bytes=None, route="all", route_only=False,
    )
    result = run_email_intake_stage(
        root=root, state_directory=tmp_path / "state", config=config, state=state,
        run_id=1, boundary=_Boundary(root), dedup_index=dedup, scan_id=1,
        identified_types={(
            dedup.snapshot.volume_id, dedup.snapshot.file_id, dedup.snapshot.size,
            dedup.snapshot.mtime_ns, dedup.snapshot.birthtime_ns,
        ): SimpleNamespace(mime="message/rfc822")},
        apply=True, cancellation=SimpleNamespace(checkpoint=lambda: None), progress=None,
    )
    assert result.status == "completed"
    assert result.filesystem_changed is False
    assert result.attachments_replayed == 1
    assert retained.read_bytes().startswith(b"%PDF")


def test_content_equivalent_rebind_is_explicit_and_hash_bound(tmp_path: Path) -> None:
    root, parent, _snapshot, dedup, state, _first = _run(tmp_path, attachment=True)
    destination = next(path for path in (root / "Adjuntos_de_correos").iterdir() if path.is_dir())
    child = next(path for path in destination.iterdir() if path.suffix == ".pdf")
    retained = root / "retained-equivalent.pdf"
    child.rename(retained)
    destination.rmdir()
    manifest_path = next((tmp_path / "state" / "email-intake").glob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["attachments"]:
        item["child_device"] = None
        item["child_inode"] = None
        item["child_mtime_ns"] = None
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    observed_parent = parent.stat()
    dedup.snapshot = FileSnapshot(
        str(parent), observed_parent.st_dev, observed_parent.st_ino,
        observed_parent.st_size, observed_parent.st_mtime_ns,
        getattr(observed_parent, "st_birthtime_ns", observed_parent.st_ctime_ns),
    )
    observed_child = retained.stat()
    dedup.child = FileSnapshot(
        str(retained), observed_child.st_dev, observed_child.st_ino,
        observed_child.st_size, observed_child.st_mtime_ns,
        getattr(observed_child, "st_birthtime_ns", observed_child.st_ctime_ns),
    )
    config = replace(
        FrameworkConfig(),
        root=root,
        state_directory=tmp_path / "state",
        route="all",
    )
    result = run_email_intake_stage(
        root=root, state_directory=tmp_path / "state", config=config, state=state,
        run_id=1, boundary=_Boundary(root), dedup_index=dedup, scan_id=1,
        identified_types={(
            dedup.snapshot.volume_id, dedup.snapshot.file_id, dedup.snapshot.size,
            dedup.snapshot.mtime_ns, dedup.snapshot.birthtime_ns,
        ): SimpleNamespace(mime="message/rfc822")},
        apply=True, cancellation=SimpleNamespace(checkpoint=lambda: None), progress=None,
    )
    assert result.status == "completed"
    assert result.filesystem_changed is False
    assert result.attachments_replayed == 1


def test_missing_replayed_child_requires_recovery_without_recreation(tmp_path: Path) -> None:
    root, _parent, snapshot, dedup, state, _first = _run(tmp_path, attachment=True)
    destination = next(path for path in (root / "Adjuntos_de_correos").iterdir() if path.is_dir())
    child = next(path for path in destination.iterdir() if path.suffix == ".pdf")
    child.unlink()
    config = SimpleNamespace(
        email_intake_enabled=True, email_max_parts=100, email_max_depth=8,
        email_max_part_bytes=1_000_000, email_max_total_bytes=2_000_000,
        email_max_source_bytes=2_000_000, email_allow_content_equivalent_reuse=False,
        max_file_bytes=None, route="all", route_only=False,
    )
    result = run_email_intake_stage(
        root=root, state_directory=tmp_path / "state", config=config, state=state,
        run_id=2, boundary=_Boundary(root), dedup_index=dedup, scan_id=1,
        identified_types={(
            snapshot.volume_id, snapshot.file_id, snapshot.size,
            snapshot.mtime_ns, snapshot.birthtime_ns,
        ): SimpleNamespace(mime="message/rfc822")},
        apply=True, cancellation=SimpleNamespace(checkpoint=lambda: None), progress=None,
    )
    assert result.status == "recovery_required"
    assert result.filesystem_changed is False
    assert result.attachments_replayed == 0
    assert not child.exists()


@pytest.mark.parametrize("unrelated_recent", (False, True))
def test_historical_consumed_zip_is_typed_without_claiming_current_children(
    tmp_path: Path, unrelated_recent: bool,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    parent = root / "message.eml"
    parent.write_bytes(_eml_zip_attachment())
    observed = parent.stat()
    snapshot = FileSnapshot(
        str(parent), observed.st_dev, observed.st_ino, observed.st_size,
        observed.st_mtime_ns, getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
    )
    dedup = _Dedup(snapshot)
    config = SimpleNamespace(
        email_intake_enabled=True, email_max_parts=100, email_max_depth=8,
        email_max_part_bytes=1_000_000, email_max_total_bytes=2_000_000,
        email_max_source_bytes=2_000_000, email_allow_content_equivalent_reuse=False,
        max_file_bytes=None, route="all", route_only=False,
    )
    identified = {(
        snapshot.volume_id, snapshot.file_id, snapshot.size,
        snapshot.mtime_ns, snapshot.birthtime_ns,
    ): SimpleNamespace(mime="message/rfc822")}
    first = run_email_intake_stage(
        root=root, state_directory=tmp_path / "state", config=config, state=_State(),
        run_id=1, boundary=_Boundary(root), dedup_index=dedup, scan_id=1,
        identified_types=identified, apply=True,
        cancellation=SimpleNamespace(checkpoint=lambda: None), progress=None,
    )
    assert first.attachments_materialized == 1
    destination = next(path for path in (root / "Adjuntos_de_correos").iterdir() if path.is_dir())
    child = next(path for path in destination.iterdir() if path.name != "manifest.json")
    manifest = json.loads(next((tmp_path / "state" / "email-intake").glob("*.json")).read_text())
    source_outcome = {
        "source_path": str(child),
        "source_identity": SourceIdentity.capture(child).to_dict(),
        "source_sha256": manifest["attachments"][0]["sha256"],
        "status": "applied",
        "published": True,
        "trashed": True,
        "successor_paths": [],
    }
    with FrameworkState(tmp_path / "state" / "framework.sqlite3") as state:
        run_one = state.begin_initial_run(root, None, inventory_policy_signature="fixture-policy")
        state.publish_run_manifest(run_one, _real_stage_manifest(run_one, root))
        state.publish_run_stage(
            run_one,
            "zip-intake",
            "completed",
            details={"schema": "neocortex.zip-intake/v1", "source_outcomes": [source_outcome]},
            idempotency_key="zip-intake:completed",
        )
        state.fail_initial_run(run_one)
        child.unlink()
        destination.rmdir()
        destination.parent.rmdir()
        run_two = state.begin_initial_run(root, None, inventory_policy_signature="fixture-policy")
        state.publish_run_manifest(run_two, _real_stage_manifest(run_two, root))
        if unrelated_recent:
            # Same archive bytes do not authorize choosing another child's
            # newer consumption fact over this child's older identity binding.
            wrong_identity = dict(source_outcome["source_identity"])
            wrong_identity["inode"] += 1
            wrong_identity["path"] = str(root / "different.zip")
            state.publish_run_stage(
                run_two, "email-zip-intake", "completed",
                details={"schema": "neocortex.zip-intake/v1", "source_outcomes": [
                    {**source_outcome, "source_identity": wrong_identity},
                ]},
                idempotency_key="email-zip-intake:completed",
            )
        class _RealHistoryState:
            def __init__(self, inner: FrameworkState) -> None:
                self.inner = inner

            def corpus_mutation_guard(self, _run_id: int) -> _Guard:
                return _Guard()

            def __getattr__(self, name: str):
                return getattr(self.inner, name)

        second = run_email_intake_stage(
            root=root, state_directory=tmp_path / "state", config=config, state=_RealHistoryState(state),
            run_id=run_two, boundary=_Boundary(root), dedup_index=dedup, scan_id=1,
            identified_types=identified, apply=True,
            cancellation=SimpleNamespace(checkpoint=lambda: None), progress=None,
        )
        assert second.status == "completed"
        assert second.filesystem_changed is False
        assert second.attachments_replayed == 0
        assert second.attachments_consumed == 1
        assert second.consumption_provenance[0]["coverage"].startswith("historical_source_consumed")
        assert not child.exists()
        assert not (root / "Adjuntos_de_correos").exists()
        forged = json.loads(next((tmp_path / "state" / "email-intake").glob("*.json")).read_text())
        forged["attachments"][0]["child_device"] = None
        forged["attachments"][0]["child_inode"] = None
        forged["attachments"][0]["child_mtime_ns"] = None
        manifest_path = next((tmp_path / "state" / "email-intake").glob("*.json"))
        manifest_path.write_text(json.dumps(forged, sort_keys=True), encoding="utf-8")
        run_three = state.begin_initial_run(root, None, inventory_policy_signature="fixture-policy")
        state.publish_run_manifest(run_three, _real_stage_manifest(run_three, root))
        third = run_email_intake_stage(
            root=root, state_directory=tmp_path / "state", config=config,
            state=_RealHistoryState(state), run_id=run_three, boundary=_Boundary(root),
            dedup_index=dedup, scan_id=1, identified_types=identified, apply=True,
            cancellation=SimpleNamespace(checkpoint=lambda: None), progress=None,
        )
        assert third.status == "recovery_required"
        assert third.attachments_consumed == 0
        assert not child.exists()


def test_historical_zip_lookup_preserves_cancellation_boundary(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    with FrameworkState(tmp_path / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(root, None, inventory_policy_signature="fixture-policy")
        state.publish_run_manifest(run_id, _real_stage_manifest(run_id, root))
        for index in range(100):
            state.publish_run_stage(
                run_id,
                f"fixture-{index}",
                "completed",
                details={"fixture": index},
                idempotency_key=f"fixture-{index}:completed",
            )
        state.fail_initial_run(run_id)
        calls = 0

        def cancel() -> None:
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise CancellationRequested("fixture cancellation")

        with pytest.raises(CancellationRequested):
            state.read_historical_zip_consumption(
                root,
                source_sha256="a" * 64,
                checkpoint=cancel,
            )
        assert calls >= 2


def test_historical_zip_lookup_accepts_terminal_partial_not_running_stage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    outcome = {
        "source_path": str(root / "nested.zip"),
        "source_identity": {
            "path": str(root / "nested.zip"), "device": 1, "inode": 2,
            "size": 3, "mtime_ns": 4, "ctime_ns": 5, "nlink": 1,
        },
        "source_sha256": "b" * 64,
        "status": "applied", "published": True, "trashed": True,
        "successor_paths": [],
    }
    with FrameworkState(tmp_path / "framework.sqlite3") as state:
        failed_run = state.begin_initial_run(root, None, inventory_policy_signature="fixture-policy")
        state.publish_run_manifest(failed_run, _real_stage_manifest(failed_run, root))
        state.publish_run_stage(
            failed_run, "zip-intake", "failed",
            details={"schema": "neocortex.zip-intake/v1", "source_outcomes": [outcome]},
            idempotency_key="zip-intake:failed",
        )
        state.fail_initial_run(failed_run)
        found = state.read_historical_zip_consumption(root, source_sha256="b" * 64)
        assert len(found) == 1
        running = state.begin_initial_run(root, None, inventory_policy_signature="fixture-policy")
        state.publish_run_manifest(running, _real_stage_manifest(running, root))
        state.publish_run_stage(
            running, "email-zip-intake", "running",
            details={"schema": "neocortex.zip-intake/v1", "source_outcomes": [outcome]},
            idempotency_key="email-zip-intake:running",
        )
        running_results = state.read_historical_zip_consumption(
            root, source_sha256="b" * 64, current_run_id=running
        )
        assert running_results
        assert all(item["run_id"] != running for item in running_results)
