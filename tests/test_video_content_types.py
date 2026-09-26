"""Focused content-routing contracts for Matroska and WebM video."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.platform.content_types import DETECTOR_VERSION, detect_content_type


def _ebml_header(doc_type: bytes) -> bytes:
    return (
        b"\x1aE\xdf\xa3"
        + b"\x9f"
        + b"fixture"
        + b"\x42\x82"
        + bytes((0x80 | len(doc_type),))
        + doc_type
    )


@pytest.mark.parametrize(
    ("name", "doc_type", "mime", "canonical_extension"),
    (
        ("recording.webm", b"webm", "video/webm", ".webm"),
        ("recording.mkv", b"matroska", "video/x-matroska", ".mkv"),
    ),
)
def test_detects_ebml_video_by_declared_doctype(
    tmp_path: Path,
    name: str,
    doc_type: bytes,
    mime: str,
    canonical_extension: str,
) -> None:
    source = tmp_path / name
    source.write_bytes(_ebml_header(doc_type))

    detected = detect_content_type(source)

    assert detected is not None
    assert detected.mime == mime
    assert detected.canonical_extension == canonical_extension
    assert detected.accepts(source)
    assert detected.evidence == f"ebml:doctype:{doc_type.decode('ascii')}"


def test_unknown_ebml_doctype_is_not_guessed_as_video(tmp_path: Path) -> None:
    source = tmp_path / "unknown.ebml"
    source.write_bytes(_ebml_header(b"test"))

    assert detect_content_type(source) is None


def test_video_detection_revision_is_explicit() -> None:
    assert DETECTOR_VERSION == "content-types-v5"
