"""Framework boundary tests for the physical ZIP Intake stage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.workflow import zip_intake_orchestrator as intake


def _snapshot(path: str, size: int) -> FileSnapshot:
    return FileSnapshot(path, 1, len(path), size, 1, 1)


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

    fake_module = SimpleNamespace(run_zip_intake=run_zip_intake)
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


def test_dry_run_cannot_request_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = SimpleNamespace(
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
