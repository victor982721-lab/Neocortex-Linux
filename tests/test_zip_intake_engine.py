from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive import intake as intake_module
from neocortex.capabilities.formats.archive.intake import (
    SourceIdentity,
    TrashDisposition,
    classify_zip,
    decide_zip,
    decide_zip_candidate,
    intake_zip,
    plan_zip_intake,
)


class _Trash:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[Path] = []

    def __call__(self, source: Path, identity: SourceIdentity) -> TrashDisposition:
        assert identity.matches(source)
        self.calls.append(source)
        self.root.mkdir(mode=0o700)
        source.rename(self.root / source.name)
        return TrashDisposition("applied", evidence="fixture-trash")


def _zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


def test_classification_distinguishes_atomic_packages_without_extracting(tmp_path: Path) -> None:
    source = tmp_path / "doc.zip"
    _zip(source, [("[Content_Types].xml", b"<Types/>"), ("word/document.xml", b"<document/>")])

    result = classify_zip(source)

    assert result.kind == "atomic_package"
    assert result.unit_kind == "docx"
    assert not (tmp_path / "doc").exists()


def test_project_is_generic_and_plan_is_read_only(tmp_path: Path) -> None:
    source = tmp_path / "project.zip"
    _zip(source, [("pyproject.toml", b"[project]\n"), ("src/app.py", b"print(1)\n")])

    result = plan_zip_intake(source, destination=tmp_path / "project")

    assert result.status == "planned"
    assert result.classification.unit_kind == "project"
    assert not (tmp_path / "project").exists()
    assert source.exists()


def test_apply_expands_nested_generic_zip_with_safe_modes(tmp_path: Path) -> None:
    nested_stream = io.BytesIO()
    with zipfile.ZipFile(nested_stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("document.txt", b"nested")
    source = tmp_path / "outer.zip"
    _zip(source, [("folder/inner.zip", nested_stream.getvalue()), ("root.txt", b"root")])
    destination = tmp_path / "outer"
    trash = _Trash(tmp_path / "trash")

    result = intake_zip(source, destination, apply=True, trash=trash)

    assert result.status == "applied"
    assert (destination / "folder" / "inner" / "document.txt").read_bytes() == b"nested"
    assert (destination / "root.txt").read_bytes() == b"root"
    assert stat.S_IMODE((destination / "folder").stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "root.txt").stat().st_mode) == 0o600
    assert not source.exists()
    assert len(trash.calls) == 1


def test_global_size_gate_happens_before_zip_open(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "large.zip"
    source.write_bytes(b"x" * 32)

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("oversize input was opened")

    monkeypatch.setattr(zipfile, "ZipFile", fail)
    result = intake_zip(source, tmp_path / "large", max_file_bytes=16, apply=True, trash=_Trash(tmp_path / "trash"))

    assert result.status == "skipped_by_size"
    assert source.exists()


def test_destination_collision_is_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "collision.zip"
    _zip(source, [("new.txt", b"new")])
    destination = tmp_path / "collision"
    destination.mkdir()
    (destination / "old.txt").write_bytes(b"old")

    result = intake_zip(source, destination, apply=True, trash=_Trash(tmp_path / "trash"))

    assert result.status == "collision"
    assert (destination / "old.txt").read_bytes() == b"old"
    assert source.exists()


def test_apply_reuses_one_canonical_preflight_without_duplicate_structure_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "generic.zip"
    _zip(source, [("payload.txt", b"payload")])
    destination = tmp_path / "generic"
    trash_root = tmp_path / "trash"
    counts = {"structure": 0, "zipfile": 0}
    original_inspect = intake_module.inspect_zip_structure
    original_zipfile = zipfile.ZipFile

    def inspect(*args: object, **kwargs: object) -> object:
        counts["structure"] += 1
        return original_inspect(*args, **kwargs)

    class CountingZipFile(original_zipfile):
        def __init__(self, *args: object, **kwargs: object) -> None:
            counts["zipfile"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(intake_module, "inspect_zip_structure", inspect)
    monkeypatch.setattr(zipfile, "ZipFile", CountingZipFile)

    result = intake_zip(
        source,
        destination,
        apply=True,
        trash=_Trash(trash_root),
    )

    assert result.status == "applied"
    assert counts == {"structure": 1, "zipfile": 2}


def test_atomic_decision_is_reused_and_stale_identity_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "document.zip"
    _zip(source, [("[Content_Types].xml", b"<Types/>") , ("word/document.xml", b"<document/>")])
    decision = decide_zip(source)

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("stale decision was unexpectedly reclassified")

    monkeypatch.setattr(intake_module, "_decide_zip", unexpected)
    reused = intake_module.run_zip_intake(source, decision=decision)
    assert reused.status == "atomic"
    assert reused.classification == decision.classification

    # Same path, changed physical metadata: a decision is not reusable merely
    # because the inventory path string survived.
    monkeypatch.undo()
    source.write_bytes(source.read_bytes() + b"changed")
    calls = 0
    original_decide = intake_module._decide_zip

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original_decide(*args, **kwargs)

    monkeypatch.setattr(intake_module, "_decide_zip", counted)
    refreshed = intake_module.run_zip_intake(source, decision=decision)
    assert calls == 1
    assert refreshed.status != "atomic"


def test_generic_decision_reuse_extracts_physical_successors_without_reclassifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "generic.zip"
    _zip(source, [("extracted.txt", b"physical")])
    decision = decide_zip(source)
    destination = tmp_path / "generic"
    trash_root = tmp_path / "trash"

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("generic decision was reclassified")

    monkeypatch.setattr(intake_module, "_decide_zip", unexpected)
    result = intake_zip(
        source,
        destination,
        apply=True,
        trash=_Trash(trash_root),
        decision=decision,
    )

    assert result.status == "applied"
    assert (destination / "extracted.txt").read_bytes() == b"physical"
    assert not source.exists()


def test_inner_cancellation_is_checked_during_member_observation(tmp_path: Path) -> None:
    source = tmp_path / "many.zip"
    _zip(source, [(f"item-{index}.txt", b"payload") for index in range(96)])

    class CancellationRequested(Exception):
        pass

    class Token:
        checks = 0

        def checkpoint(self) -> None:
            self.checks += 1
            if self.checks >= 4:
                raise CancellationRequested()

    token = Token()
    result = intake_module.run_zip_intake(source, cancellation=token)

    assert result.status == "blocked"
    assert result.reason == "blocked"
    assert result.detail and "cancellation" in result.detail
    assert token.checks >= 4


def test_progress_updates_are_coalesced_for_large_marker_inventory(tmp_path: Path) -> None:
    source = tmp_path / "many.zip"
    _zip(source, [(f"item-{index}.txt", b"payload") for index in range(256)])
    events = []

    result = classify_zip(source, progress=events.append)

    assert result.kind == "generic_zip"
    assert len(events) < 32
    assert events[-1].finished is True


def test_candidate_owner_does_not_classify_non_zip_files(tmp_path: Path) -> None:
    text = tmp_path / "wrong-extension.bin"
    text.write_bytes(b"plain text")
    assert decide_zip_candidate(text) is None
