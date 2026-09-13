"""Regression coverage for writer-bound, no-replace exact-index publication."""

from __future__ import annotations

import os
import stat
import struct
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import TypedDict, Unpack

import pytest

from neocortex.semantic import semantic_exact_index_format as fmt


TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


class _DescriptorOptions(TypedDict, total=False):
    cancellation_check: Callable[[], None] | None
    expected_identity: Mapping[str, int] | None
    expected_bytes: int | None
    expected_sha256: str | None


PAIR = fmt.PublishedPair("model-fixture", 1, "processing-fixture", "space-fixture", "text", "text_chunk")
BINDING = {
    "owner": "fixture-owner",
    "owner_fence": {"source": "fixture"},
    "schema_version": 1,
    "published_heads": [],
}


def _record() -> fmt.VectorRecord:
    return fmt.VectorRecord(
        ref_id=1,
        entity_id="entity-fixture",
        item_id="item-fixture",
        model_signature=PAIR.model_signature,
        vector_space=PAIR.vector_space,
        modality="text",
        generation_id=PAIR.generation_id,
        processing_signature=PAIR.processing_signature,
        entity_kind="text_chunk",
        vector_dtype="float32",
        dimensions=2,
        vector_blob=struct.pack("<2f", 1.0, 0.5),
        provenance_json={"source": "fixture"},
        owner_row_binding="row-fixture",
    )


def _prepare(tmp_path: Path) -> Path:
    destination = tmp_path / "derived-view"
    fmt.prepare_exact_view(
        [_record()],
        artifact_parent=tmp_path,
        owner_binding=BINDING,
        pairs=[PAIR],
        destination=destination,
    )
    return destination


def _write_foreign(path: Path, payload: bytes = b"foreign", mode: int = 0o640) -> None:
    path.write_bytes(payload)
    path.chmod(mode)


def test_normal_prepare_and_open_still_work(tmp_path: Path) -> None:
    destination = _prepare(tmp_path)
    view = fmt.validate_exact_view(destination, artifact_parent=tmp_path, live_owner_binding=BINDING)
    try:
        assert view.row_count == 1
        assert destination.joinpath("manifest.json").is_file()
    finally:
        view.close()


def test_foreign_final_collision_is_not_overwritten_or_chmodded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "derived-view"
    real_link = os.link

    def collide_before_rows(src: str, dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None, follow_symlinks: bool = True) -> None:
        if dst == "rows.bin" and dst_dir_fd is not None:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=0o640, dir_fd=dst_dir_fd)
            os.write(fd, b"foreign")
            os.fchmod(fd, 0o640)
            os.close(fd)
        real_link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(fmt.os, "link", collide_before_rows)
    with pytest.raises(fmt.DerivedViewContractError, match=r"overwrite|publish"):
        fmt.prepare_exact_view(
            [_record()],
            artifact_parent=tmp_path,
            owner_binding=BINDING,
            pairs=[PAIR],
            destination=destination,
        )
    foreign = destination / "rows.bin"
    assert foreign.read_bytes() == b"foreign"
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o640
    assert not (destination / "manifest.json").exists()


def test_foreign_replacement_survives_failure_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "derived-view"
    original_publish = fmt._publish_writer_at

    def replace_rows_then_fail(
        directory_fd: int,
        temporary_name: str,
        published_name: str,
        writer: fmt._Writer,
        *,
        cancellation_check: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        if published_name == "identity.bin":
            rows = destination / "rows.bin"
            rows.unlink()
            _write_foreign(rows, b"foreign-after-publish", 0o640)
            raise RuntimeError("injected publication failure")
        return original_publish(
            directory_fd,
            temporary_name,
            published_name,
            writer,
            cancellation_check=cancellation_check,
        )

    monkeypatch.setattr(fmt, "_publish_writer_at", replace_rows_then_fail)
    with pytest.raises(RuntimeError, match="injected publication failure"):
        fmt.prepare_exact_view(
            [_record()],
            artifact_parent=tmp_path,
            owner_binding=BINDING,
            pairs=[PAIR],
            destination=destination,
        )
    rows = destination / "rows.bin"
    assert rows.read_bytes() == b"foreign-after-publish"
    assert stat.S_IMODE(rows.stat().st_mode) == 0o640
    assert not (destination / "manifest.json").exists()


def test_corruption_between_link_and_descriptor_rejects_without_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "derived-view"
    original_descriptor = fmt._artifact_descriptor_at
    corrupted = False

    def corrupt_rows_once(directory_fd: int, name: str, **kwargs: Unpack[_DescriptorOptions]) -> dict[str, object]:
        nonlocal corrupted
        if name == "rows.bin" and not corrupted:
            corrupted = True
            rows = destination / "rows.bin"
            foreign = destination / ".foreign-corrupt"
            _write_foreign(foreign, b"\xcc" * fmt.ROW_STRUCT.size, 0o640)
            rows.unlink()
            foreign.rename(rows)
        return original_descriptor(directory_fd, name, **kwargs)

    monkeypatch.setattr(fmt, "_artifact_descriptor_at", corrupt_rows_once)
    with pytest.raises(fmt.DerivedViewContractError, match=r"writer|artifact|identity|digest"):
        fmt.prepare_exact_view(
            [_record()],
            artifact_parent=tmp_path,
            owner_binding=BINDING,
            pairs=[PAIR],
            destination=destination,
        )
    assert corrupted
    assert (destination / "rows.bin").read_bytes() == b"\xcc" * fmt.ROW_STRUCT.size
    assert not (destination / "manifest.json").exists()
