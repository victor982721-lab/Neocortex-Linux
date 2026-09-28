"""Synthetic C06/C07 ZIP budget, marker and recovery regressions."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive import intake as intake_module
from neocortex.capabilities.formats.archive.intake import (
    TrashDisposition,
    ZipIntakeLimits,
    classify_zip,
    intake_zip,
)
from neocortex.platform.zip_safety import ZipMemberStructure, ZipStructure


class _Trash:
    def __init__(self, root: Path) -> None:
        self.root = root

    def __call__(self, source: Path, identity: object) -> TrashDisposition:
        assert identity.matches(source)
        self.root.mkdir(mode=0o700)
        source.rename(self.root / source.name)
        return TrashDisposition("applied", evidence="synthetic-trash")


def test_large_xml_marker_is_bounded_by_xml_policy_not_small_marker(tmp_path: Path) -> None:
    source = tmp_path / "large.docx"
    body = b"<w:document xmlns:w='urn:w'><w:body>" + b"x" * (3 * 1024 * 1024) + b"</w:body></w:document>"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", body)

    classification = classify_zip(source)
    assert classification.kind == "atomic_package"
    assert classification.unit_kind == "docx"


def test_small_high_ratio_member_is_not_rejected_by_ratio_alone(tmp_path: Path) -> None:
    source = tmp_path / "metadata.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.txt", b"A" * 32_768)

    classification = classify_zip(source)
    assert classification.kind == "generic_zip"
    assert classification.status == "validated"


def test_hard_budget_policy_does_not_widen_equal_or_larger_caps(tmp_path: Path) -> None:
    source = tmp_path / "ordinary.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("payload.txt", b"payload")
    limits = ZipIntakeLimits(auto_measured_budget=False)
    decision = intake_module.decide_zip(source, limits=limits)
    assert decision.admitted_limits == limits


def _metadata_admission_fixture(
    path: Path,
    *,
    source_bytes: int,
    total_bytes: int,
    largest_member: int,
    compressed_member: int,
    limits: ZipIntakeLimits | None = None,
):
    with path.open("wb") as stream:
        stream.truncate(source_bytes)
    entry = ZipMemberStructure(
        0,
        "payload.bin",
        0,
        0,
        compressed_member,
        largest_member,
        0,
        8,
        0,
        source_bytes,
    )
    structure = ZipStructure(1, 64, 0, False, (entry,))
    return intake_module._measured_limits_for_structure(
        path,
        structure,
        ZipIntakeLimits() if limits is None else limits,
    )


def test_auto_admission_accepts_observed_mdcorpus_five_x_member(tmp_path: Path) -> None:
    limits = _metadata_admission_fixture(
        tmp_path / "mdcorpus.zip",
        source_bytes=26_677_945,
        total_bytes=134_157_393,
        largest_member=134_157_393,
        compressed_member=26_677_945,
    )
    assert limits.max_member_bytes >= 134_157_393


def test_auto_admission_accepts_observed_codex_three_x_tree(tmp_path: Path) -> None:
    _metadata_admission_fixture(
        tmp_path / "codex.zip",
        source_bytes=428_854_394,
        total_bytes=1_287_170_377,
        largest_member=200 * 1024 * 1024,
        compressed_member=100 * 1024 * 1024,
    )
    # Feed the tree total through a second entry set so the cumulative envelope
    # is exercised without allocating a 1.2 GiB fixture.
    path = tmp_path / "codex.zip"
    entries = tuple(
        ZipMemberStructure(
            index,
            f"member-{index}.bin",
            0,
            0,
            100_000_000,
            (368_189_901, 306_326_825, 306_326_825, 306_326_826)[index],
            0,
            8,
            0,
            0,
        )
        for index in range(4)
    )
    admitted = intake_module._measured_limits_for_structure(
        path,
        ZipStructure(4, 128, 0, False, entries),
        ZipIntakeLimits(),
    )
    assert admitted.max_total_uncompressed_bytes >= 1_287_170_377


def test_auto_admission_rejects_absolute_quota_ratio_and_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bounded.zip"
    with source.open("wb") as stream:
        stream.truncate(428_854_394)
    hard_quota = ZipIntakeLimits(auto_max_total_uncompressed_bytes=600 * 1024 * 1024)
    entries = tuple(
        ZipMemberStructure(
            index,
            f"member-{index}.bin",
            0,
            0,
            100_000_000,
            (368_189_901, 306_326_825, 306_326_825, 306_326_826)[index],
            0,
            8,
            0,
            0,
        )
        for index in range(4)
    )
    bounded = intake_module._measured_limits_for_structure(
        source,
        ZipStructure(4, 128, 0, False, entries),
        hard_quota,
    )
    assert bounded == hard_quota
    bomb = _metadata_admission_fixture(
        tmp_path / "bomb.zip",
        source_bytes=1 * 1024 * 1024,
        total_bytes=100 * 1024 * 1024,
        largest_member=100 * 1024 * 1024,
        compressed_member=1 * 1024 * 1024,
    )
    assert bomb.max_total_uncompressed_bytes == ZipIntakeLimits().max_total_uncompressed_bytes
    monkeypatch.setattr(
        intake_module.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 1})(),
    )
    no_space = _metadata_admission_fixture(
        tmp_path / "space.zip",
        source_bytes=26_677_945,
        total_bytes=134_157_393,
        largest_member=134_157_393,
        compressed_member=26_677_945,
    )
    assert no_space.max_member_bytes == ZipIntakeLimits().max_member_bytes


def test_destination_collision_is_deterministic_no_replace(tmp_path: Path) -> None:
    source = tmp_path / "x.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("new.txt", b"new")
    destination = tmp_path / "x"
    destination.write_bytes(b"canonical-existing")

    result = intake_zip(source, destination, apply=True, trash=_Trash(tmp_path / "trash"))
    assert result.status == "applied"
    assert destination.read_bytes() == b"canonical-existing"
    assert not source.exists()
    assert result.destination and result.destination != str(destination)


def test_file_collision_uses_hash_bound_sibling_directory(tmp_path: Path) -> None:
    source = tmp_path / "x.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("new.txt", b"new")
    existing = tmp_path / "x"
    existing.write_bytes(b"canonical-existing")

    result = intake_zip(source, apply=True, trash=_Trash(tmp_path / "trash"))
    assert result.status == "applied"
    assert existing.read_bytes() == b"canonical-existing"
    assert result.destination and result.destination != str(existing)
    published = Path(result.destination)
    assert published.name.startswith("x--sha256-")
    assert (published / "new.txt").read_bytes() == b"new"


def test_corrupt_read_keeps_original_and_never_promotes_partial_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repair-failure.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("payload.txt", b"payload")
    original = source.read_bytes()

    def fail(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise intake_module.ZipIntakeError("corrupt", "synthetic_member_read_failed")

    monkeypatch.setattr(intake_module, "_extract_zip_tree", fail)
    result = intake_zip(source, tmp_path / "published", apply=True, trash=_Trash(tmp_path / "trash"))
    assert result.status == "corrupt"
    assert source.read_bytes() == original
    assert not (tmp_path / "published").exists()
