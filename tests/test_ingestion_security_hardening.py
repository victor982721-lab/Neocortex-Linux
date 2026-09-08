"""Focused regressions for untrusted ingestion boundaries."""

from __future__ import annotations

import io
import stat
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest
from PIL import Image

from neocortex.api.cli import cli_audio, cli_video
from neocortex.capabilities.formats.archive.route import _xml_text
from neocortex.capabilities.formats.image.isolation import image_worker_memory_reservation
from neocortex.capabilities.formats.office.extraction import extract_office_document
from neocortex.capabilities.formats.office.models import OfficeExtractionError
from neocortex.capabilities.formats.pdf import pdf_isolation
from neocortex.capabilities.formats.text.text_route import _email_text
from neocortex.capabilities.formats.xml_safety import (
    UnsafeXmlDeclarationError,
    safe_xml_fromstring,
    safe_xml_iterparse,
)
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.bounded_subprocess import SubprocessOutputLimitError


def test_xml_declarations_are_rejected_before_entity_expansion() -> None:
    payload = b'<!DOCTYPE x [<!ENTITY value "expanded">]><x>&value;</x>'

    with pytest.raises(UnsafeXmlDeclarationError):
        safe_xml_fromstring(payload)
    with pytest.raises(UnsafeXmlDeclarationError):
        tuple(safe_xml_iterparse(io.BytesIO(payload), events=("end",)))
    with pytest.raises(UnsafeXmlDeclarationError):
        _xml_text(payload)


def test_office_rejects_special_zip_members(tmp_path: Path) -> None:
    path = tmp_path / "special.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in (
            ("[Content_Types].xml", b"<Types/>"),
            ("xl/workbook.xml", b"<workbook><sheet name='x'/></workbook>"),
        ):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, payload)

    with pytest.raises(OfficeExtractionError, match="special office member"):
        extract_office_document(
            path,
            "xlsx",
            max_text_chars=1000,
            cancellation=CancellationToken(),
        )


def test_email_text_cuts_off_after_bounded_part_count() -> None:
    lines = ['Content-Type: multipart/mixed; boundary="parts"', "", "--parts"]
    for index in range(4_200):
        lines.append(f"Content-Type: text/plain\n\npart-{index}\n--parts")
    lines.append("--parts--")

    result = _email_text("\n".join(lines).encode(), 32)

    assert result.truncated is True
    assert len(result.text) <= 32


def test_qpdf_recovery_uses_bounded_subprocess_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    snapshot = snapshot_path(source)
    observed: dict[str, object] = {}

    def bounded(_arguments, **kwargs):
        observed.update(kwargs)
        raise SubprocessOutputLimitError("stderr", int(kwargs["stderr_limit_bytes"]))

    monkeypatch.setattr(pdf_isolation.shutil, "which", lambda _name: "/tmp/qpdf")
    monkeypatch.setattr(pdf_isolation, "run_bounded_capture", bounded)
    config = pdf_isolation.IsolatedExtractionConfig(
        "never",
        "eng",
        200,
        40,
        1000,
        1_000_000,
        None,
        5,
        False,
        None,
        None,
        None,
        False,
        0,
        frozenset(),
        0,
        None,
        None,
    )

    with pytest.raises(SubprocessOutputLimitError):
        with pdf_isolation._qpdf_repaired_copy(
            snapshot,
            config,
            primary_error="primary",
            fallback_error="fallback",
        ):
            pass

    assert observed["stderr_limit_bytes"] == 256 * 1024


def test_image_decode_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.png"
    link = tmp_path / "candidate.png"
    Image.new("RGB", (16, 16), "white").save(target)
    link.symlink_to(target.name)

    with pytest.raises(OSError):
        image_worker_memory_reservation(link)


def test_audio_and_video_human_output_escape_controls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from neocortex.capabilities.formats.audio import route as audio_route
    from neocortex.capabilities.formats.video import state as video_state

    monkeypatch.setattr(
        audio_route,
        "search_audio_state",
        lambda *_args: (
            {
                "path": "bad\x1b[31m\npath",
                "language": "es",
                "duration_seconds": 1.0,
                "model_name": "small",
                "snippet": "bad\x1b[2J\ntext",
            },
        ),
    )
    args = Namespace(
        state_directory=Path("/tmp"),
        audio_search="x",
        audio_search_limit=1,
    )
    assert cli_audio.run_audio_search(args) == 0
    audio_output = capsys.readouterr().out
    assert "\x1b" not in audio_output
    assert "\\u001b" in audio_output

    monkeypatch.setattr(
        video_state,
        "search_video_state",
        lambda *_args, **_kwargs: (
            {
                "path": "bad\x1b[31m\npath",
                "evidence": "00:01\x1b[2J",
                "channel": "ocr",
                "ocr_mean_confidence": None,
                "snippet": "bad\ntext",
            },
        ),
    )
    video_args = Namespace(
        state_directory=Path("/tmp"),
        video_search="x",
        video_search_limit=1,
    )
    assert cli_video.run_video_search(video_args) == 0
    video_output = capsys.readouterr().out
    assert "\x1b" not in video_output
    assert "\\u001b" in video_output
