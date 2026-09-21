"""Framework boundary tests for the physical ZIP Intake stage."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.archive.intake import (
    FilesystemPublishHook,
    SourceIdentity,
    TrashDisposition,
)
from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.fingerprinting import snapshot_path
from neocortex.progress import ProgressEvent
from neocortex.workflow import zip_intake_orchestrator as intake


def _snapshot(path: str, size: int) -> FileSnapshot:
    return FileSnapshot(path, 1, len(path), size, 1, 1)


class _RecordingTrashBackend:
    def __init__(self, outcome: str = "applied") -> None:
        self.outcome = outcome
        self.calls: list[tuple[FileSnapshot, Path, str]] = []

    def apply_snapshot(
        self,
        snapshot: FileSnapshot,
        *,
        root: Path,
        source_digest: str,
    ) -> SimpleNamespace:
        self.calls.append((snapshot, root, source_digest))
        return SimpleNamespace(status=self.outcome, detail="fixture", receipt_json="{}")


def _live_trash_hook(
    tmp_path: Path,
) -> tuple[
    Path,
    FileSnapshot,
    SourceIdentity,
    _RecordingTrashBackend,
    intake._KioTrashHook,
]:
    source = tmp_path / "source.zip"
    source.write_bytes(b"PK\x03\x04fixture")
    inventory_snapshot = snapshot_path(source)
    identity = SourceIdentity.capture(source)
    backend = _RecordingTrashBackend()
    hook = intake._KioTrashHook(backend, tmp_path, inventory_snapshot)
    return source, inventory_snapshot, identity, backend, hook


def test_admission_keeps_inventory_visibility_but_excludes_oversize_before_engine() -> None:
    snapshots = (
        _snapshot("/corpus/small.zip", 10_000_000),
        _snapshot("/corpus/large.zip", 10_000_001),
        _snapshot("/corpus/mislabeled.bin", 9),
    )

    admission = intake.build_zip_intake_admission(snapshots, 10_000_000)

    assert admission.total_files == 3
    assert admission.eligible_files == 2
    assert admission.size_skipped_files == 1
    assert admission.size_skipped_bytes == 10_000_001
    assert tuple(snapshot.path for snapshot in admission.snapshots) == (
        "/corpus/small.zip",
        "/corpus/mislabeled.bin",
    )


def test_unlimited_admission_does_not_create_hidden_size_ceiling() -> None:
    snapshots = (_snapshot("/corpus/huge.zip", 2**40),)

    admission = intake.build_zip_intake_admission(snapshots, None)

    assert admission.max_file_bytes is None
    assert admission.eligible_files == 1
    assert admission.size_skipped_files == 0
    assert admission.snapshots == snapshots


def test_oversize_snapshot_never_reaches_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def run_zip_intake(source: str, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError(f"oversize source reached engine: {source}")

    monkeypatch.setattr(
        intake,
        "import_module",
        lambda name: SimpleNamespace(run_zip_intake=run_zip_intake),
    )
    admission = intake.build_zip_intake_admission(
        (_snapshot("/corpus/large.zip", 10_000_001),),
        10_000_000,
    )

    result = intake.run_zip_intake_stage(
        root=Path("/corpus"),
        admission=admission,
        config=SimpleNamespace(),
        apply=True,
        state_directory=Path("/state"),
        run_id=3,
        state=object(),
        progress=None,
        cancellation=object(),
    )

    assert called is False
    assert result.reconciliation_required is False
    assert result.details["size_skipped_files"] == 1
    assert result.details["size_skipped_bytes"] == 10_000_001


def test_failed_engine_outcome_does_not_claim_physical_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = SimpleNamespace(
        decide_zip_candidate=lambda source, **kwargs: object(),
        run_zip_intake=lambda source, **kwargs: {
            "status": "collision",
            "published": False,
            "trashed": False,
            "reason": "destination_collision",
        }
    )
    monkeypatch.setattr(intake, "import_module", lambda name: fake_module)
    admission = intake.build_zip_intake_admission(
        (_snapshot("/corpus/source.zip", 4),),
        None,
    )

    result = intake.run_zip_intake_stage(
        root=Path("/corpus"),
        admission=admission,
        config=SimpleNamespace(),
        apply=True,
        state_directory=Path("/state"),
        run_id=4,
        state=object(),
        progress=None,
        cancellation=object(),
    )

    assert result.status == "blocked"
    assert result.filesystem_changed is False
    assert result.reconciliation_required is False
    assert result.details["failures"] == {"collision": 1}


def test_engine_contract_is_lazy_and_apply_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def run_zip_intake(source: str, **kwargs: object) -> object:
        observed["source"] = source
        observed.update(kwargs)
        assert kwargs["max_file_bytes"] == 10_000_000
        return SimpleNamespace(
            to_dict=lambda: {
                "status": "applied",
                "filesystem_changed": True,
                "published_files": 1,
            }
        )

    fake_module = SimpleNamespace(
        decide_zip_candidate=lambda source, **kwargs: object(),
        run_zip_intake=run_zip_intake,
    )
    monkeypatch.setattr(intake, "import_module", lambda name: fake_module)
    admission = intake.build_zip_intake_admission(
        (_snapshot("/corpus/source.zip", 10_000_000),),
        10_000_000,
    )

    result = intake.run_zip_intake_stage(
        root=Path("/corpus"),
        admission=admission,
        config=SimpleNamespace(),
        apply=True,
        state_directory=Path("/state"),
        run_id=7,
        state=object(),
        progress=None,
        cancellation=object(),  # engine test double does not use it
    )

    assert result.filesystem_changed is True
    assert result.reconciliation_required is True
    assert result.details["schema"] == intake.ZIP_INTAKE_SCHEMA
    assert result.details["mode"] == "apply"
    assert observed["source"] == "/corpus/source.zip"
    assert observed["staging"] is not None
    assert observed["trash"] is not None


def test_stage_forwards_identity_decision_cancellation_and_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    events: list[ProgressEvent] = []
    token = object()
    decision = object()
    progress_callback = events.append

    def decide_zip_candidate(source: Path, **kwargs: object) -> object:
        observed["decision_source"] = source
        observed["decision_kwargs"] = kwargs
        return decision

    def run_zip_intake(source: str, **kwargs: object) -> object:
        observed["runner_source"] = source
        observed["runner_kwargs"] = kwargs
        callback = kwargs["progress"]
        assert callable(callback)
        for completed in range(1, 4):
            callback(
                ProgressEvent(
                    "zip-intake",
                    "classification",
                    "Procesando ZIPs",
                    completed,
                    3,
                    "ZIPs",
                )
            )
        return {"status": "planned", "classification": {"kind": "generic_zip"}}

    monkeypatch.setattr(
        intake,
        "import_module",
        lambda name: SimpleNamespace(
            decide_zip_candidate=decide_zip_candidate,
            run_zip_intake=run_zip_intake,
        ),
    )
    admission = intake.build_zip_intake_admission(
        (_snapshot("/corpus/source.bin", 4),),
        None,
    )

    result = intake.run_zip_intake_stage(
        root=Path("/corpus"),
        admission=admission,
        config=SimpleNamespace(),
        apply=False,
        state_directory=Path("/state"),
        run_id=8,
        state=object(),
        progress=progress_callback,
        cancellation=token,
    )

    assert result.status == "planned"
    decision_kwargs = observed["decision_kwargs"]
    runner_kwargs = observed["runner_kwargs"]
    assert isinstance(decision_kwargs, dict)
    assert isinstance(runner_kwargs, dict)
    assert decision_kwargs["cancellation"] is token
    assert decision_kwargs["progress"] is progress_callback
    assert runner_kwargs["cancellation"] is token
    assert runner_kwargs["cancellation_token"] is token
    assert runner_kwargs["progress"] is progress_callback
    assert runner_kwargs["decision"] is decision
    assert [event.description for event in events] == [
        "Procesando ZIPs",
        "Procesando ZIPs",
        "Procesando ZIPs",
    ]


def test_atomic_decision_exposes_identity_bound_cache_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot("/corpus/document.docx", 4)
    decision = SimpleNamespace(
        classification=SimpleNamespace(kind="atomic_package", unit_kind="docx")
    )

    def decide_zip_candidate(source: Path, **kwargs: object) -> object:
        return decision

    def run_zip_intake(source: str, **kwargs: object) -> object:
        return {
            "status": "atomic",
            "classification": {"kind": "atomic_package", "unit_kind": "docx"},
            "members": 2,
        }

    monkeypatch.setattr(
        intake,
        "import_module",
        lambda name: SimpleNamespace(
            decide_zip_candidate=decide_zip_candidate,
            run_zip_intake=run_zip_intake,
        ),
    )
    result = intake.run_zip_intake_stage(
        root=Path("/corpus"),
        admission=intake.build_zip_intake_admission((snapshot,), None),
        config=SimpleNamespace(),
        apply=False,
        state_directory=Path("/state"),
        run_id=9,
        state=object(),
        progress=None,
        cancellation=object(),
    )

    assert len(result.atomic_decisions) == 1
    cached_snapshot, detected = result.atomic_decisions[0]
    assert cached_snapshot == snapshot
    assert detected.mime.endswith("wordprocessingml.document")
    assert detected.canonical_extension == ".docx"


def test_stage_does_not_probe_wrong_extension_before_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    decision = object()

    def decide_zip_candidate(source: Path, **kwargs: object) -> object:
        calls.append(f"decide:{source}")
        return decision

    def run_zip_intake(source: str, **kwargs: object) -> object:
        calls.append(f"run:{source}")
        assert kwargs["decision"] is decision
        return {"status": "planned", "classification": {"kind": "atomic_package"}}

    monkeypatch.setattr(
        intake,
        "import_module",
        lambda name: SimpleNamespace(
            decide_zip_candidate=decide_zip_candidate,
            run_zip_intake=run_zip_intake,
        ),
    )
    admission = intake.build_zip_intake_admission(
        (_snapshot("/corpus/wrong-extension.bin", 4),),
        None,
    )
    monkeypatch.setattr(
        Path,
        "open",
        lambda *args, **kwargs: pytest.fail("orchestrator performed a ZIP header read"),
    )

    result = intake.run_zip_intake_stage(
        root=Path("/corpus"),
        admission=admission,
        config=SimpleNamespace(),
        apply=False,
        state_directory=Path("/state"),
        run_id=9,
        state=object(),
        progress=None,
        cancellation=object(),
    )

    assert result.status == "planned"
    assert calls == ["decide:/corpus/wrong-extension.bin", "run:/corpus/wrong-extension.bin"]


def test_dry_run_cannot_request_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = SimpleNamespace(
        decide_zip_candidate=lambda source, **kwargs: object(),
        run_zip_intake=lambda source, **kwargs: {
            "status": "planned",
            "filesystem_changed": True,
            "reconciliation_required": True,
        }
    )
    monkeypatch.setattr(intake, "import_module", lambda name: fake_module)
    admission = intake.build_zip_intake_admission(
        (_snapshot("/corpus/source.zip", 4),),
        None,
    )

    result = intake.run_zip_intake_stage(
        root=Path("/corpus"),
        admission=admission,
        config=SimpleNamespace(),
        apply=False,
        state_directory=Path("/state"),
        run_id=1,
        state=object(),
        progress=None,
        cancellation=object(),
    )

    assert result.filesystem_changed is False
    assert result.reconciliation_required is False
    assert result.details["mode"] == "plan"


def test_missing_engine_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake, "import_module", lambda name: SimpleNamespace())
    admission = intake.build_zip_intake_admission(
        (_snapshot("/corpus/source.zip", 4),),
        None,
    )

    with pytest.raises(RuntimeError, match="run_zip_intake"):
        intake.run_zip_intake_stage(
            root=Path("/corpus"),
            admission=admission,
            config=SimpleNamespace(),
            apply=False,
            state_directory=Path("/state"),
            run_id=1,
            state=object(),
            progress=None,
            cancellation=object(),
        )


def test_kio_hook_binds_backend_to_original_identity(tmp_path: Path) -> None:
    source, inventory_snapshot, identity, backend, hook = _live_trash_hook(tmp_path)

    result = hook.trash(source, identity)

    assert isinstance(result, TrashDisposition)
    assert result.status == "applied"
    assert len(backend.calls) == 1
    observed, root, digest = backend.calls[0]
    assert observed == inventory_snapshot
    assert root == tmp_path
    assert digest.startswith("metadata-v1:")


def test_kio_hook_rejects_replacement_with_same_size_and_mtime(
    tmp_path: Path,
) -> None:
    source, _, identity, backend, hook = _live_trash_hook(tmp_path)
    original = source.stat()
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"PK\x03\x04fixture")
    os.utime(
        replacement,
        ns=(original.st_atime_ns, original.st_mtime_ns),
    )
    os.replace(replacement, source)
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))

    result = hook.trash(source, identity)

    assert isinstance(result, TrashDisposition)
    assert result.status == "blocked"
    assert result.detail == "source_changed"
    assert backend.calls == []
    assert source.read_bytes() == b"PK\x03\x04fixture"


def test_kio_hook_rejects_inventory_snapshot_drift_before_backend(
    tmp_path: Path,
) -> None:
    source, inventory_snapshot, identity, backend, _ = _live_trash_hook(tmp_path)
    mismatched_snapshot = FileSnapshot(
        inventory_snapshot.path,
        inventory_snapshot.volume_id,
        inventory_snapshot.file_id,
        inventory_snapshot.size + 1,
        inventory_snapshot.mtime_ns,
        inventory_snapshot.birthtime_ns,
    )
    hook = intake._KioTrashHook(backend, tmp_path, mismatched_snapshot)

    result = hook.trash(source, identity)

    assert isinstance(result, TrashDisposition)
    assert result.status == "blocked"
    assert result.detail == "source_changed"
    assert backend.calls == []


def test_kio_hook_rejects_ctime_drift_before_backend(tmp_path: Path) -> None:
    source, _, identity, backend, hook = _live_trash_hook(tmp_path)
    original_mode = stat.S_IMODE(source.stat().st_mode)
    os.chmod(source, original_mode ^ stat.S_IXUSR)

    result = hook.trash(source, identity)

    assert isinstance(result, TrashDisposition)
    assert result.status == "blocked"
    assert result.detail == "source_changed"
    assert backend.calls == []


def test_kio_hook_rejects_nlink_drift_before_backend(tmp_path: Path) -> None:
    source, _, identity, backend, hook = _live_trash_hook(tmp_path)
    os.link(source, tmp_path / "hardlink.zip")

    result = hook.trash(source, identity)

    assert isinstance(result, TrashDisposition)
    assert result.status == "blocked"
    assert result.detail == "source_changed"
    assert backend.calls == []


def test_kio_hook_rejects_symlink_replacement_before_backend(tmp_path: Path) -> None:
    source, _, identity, backend, hook = _live_trash_hook(tmp_path)
    target = tmp_path / "target.zip"
    target.write_bytes(b"replacement")
    source.unlink()
    source.symlink_to(target.name)

    result = hook.trash(source, identity)

    assert isinstance(result, TrashDisposition)
    assert result.status == "blocked"
    assert result.detail == "source_changed"
    assert backend.calls == []


def test_source_replacement_before_kio_rolls_back_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.zip"
    source.write_bytes(b"original zip bytes")
    inventory_snapshot = snapshot_path(source)
    original_identity = SourceIdentity.capture(source)
    backend = _RecordingTrashBackend()
    delegate = intake._KioTrashHook(backend, tmp_path, inventory_snapshot)
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "payload.txt").write_text("published fixture", encoding="utf-8")
    destination = tmp_path / "published"
    publisher = FilesystemPublishHook()
    receipt = publisher.publish(staged, destination)
    source.unlink()
    source.write_bytes(b"replacement at the same path")

    result = delegate.trash(source, original_identity)

    assert isinstance(result, TrashDisposition)
    assert result.status == "blocked"
    assert result.detail == "source_changed"
    assert publisher.rollback(receipt) is True
    assert not destination.exists()
    assert source.read_bytes() == b"replacement at the same path"
    assert backend.calls == []
