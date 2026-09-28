"""Synthetic C05/C22 Framework EML integration tests."""

from __future__ import annotations

import email
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator_pipeline import _select_new_zip_snapshots
from neocortex.workflow.email_intake_orchestrator import run_email_intake_stage


TEST_CAPABILITIES = ("base", "inference")


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
