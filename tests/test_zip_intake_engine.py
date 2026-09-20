from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

from neocortex.capabilities.formats.archive.intake import (
    SourceIdentity,
    TrashDisposition,
    classify_zip,
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
