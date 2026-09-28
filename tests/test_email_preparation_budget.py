"""Synthetic preparation/budget regressions for the EML integration seam."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from neocortex.capabilities.formats.text.email_intake import (
    EmailAttachmentError,
    EmailAttachmentLimits,
    materialize_email_attachments,
    prepare_email_attachments,
)


def _message(path: Path, *, attachments: int = 2, filename: str = "report.pdf") -> None:
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "receiver@example.test"
    message["Subject"] = "bounded fixture"
    message.set_content("Body only remains owned by the parent message.")
    for index in range(attachments):
        message.add_attachment(
            (f"pdf-{index}".encode("ascii")),
            maintype="application",
            subtype="pdf",
            filename=filename,
        )
    path.write_bytes(bytes(message))


def test_prepared_decision_reuses_one_parse_and_matches_legacy_call(tmp_path: Path) -> None:
    parent = tmp_path / "message.eml"
    _message(parent)
    limits = EmailAttachmentLimits()
    prepared = prepare_email_attachments(parent, limits=limits)
    planned = materialize_email_attachments(
        parent,
        tmp_path / "prepared",
        limits=limits,
        prepared=prepared,
    )
    legacy = materialize_email_attachments(
        parent,
        tmp_path / "legacy",
        apply=True,
        limits=limits,
    )
    assert planned.status == "planned"
    assert legacy.status == "applied"
    assert [item.sha256 for item in planned.attachments] == [
        item.sha256 for item in legacy.attachments
    ]


def test_body_only_prepare_does_not_create_attachment_directories(tmp_path: Path) -> None:
    parent = tmp_path / "body.eml"
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "receiver@example.test"
    message.set_content("No attachments")
    parent.write_bytes(bytes(message))

    prepared = prepare_email_attachments(parent)
    assert prepared.attachments == ()
    assert not (tmp_path / "children").exists()


def test_source_change_after_prepare_is_fenced(tmp_path: Path) -> None:
    parent = tmp_path / "message.eml"
    _message(parent)
    prepared = prepare_email_attachments(parent)
    parent.write_bytes(parent.read_bytes() + b"changed")
    with pytest.raises(EmailAttachmentError, match="parent_identity_changed"):
        materialize_email_attachments(parent, tmp_path / "children", prepared=prepared)


def test_cancellation_is_checked_during_parent_read_parse_and_write(tmp_path: Path) -> None:
    parent = tmp_path / "large.eml"
    _message(parent, attachments=1)
    calls = 0

    def cancel_during_prepare() -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise RuntimeError("fixture cancellation")

    with pytest.raises(RuntimeError, match="fixture cancellation"):
        prepare_email_attachments(parent, checkpoint=cancel_during_prepare)

    prepared = prepare_email_attachments(parent)
    write_calls = 0

    def cancel_during_write() -> None:
        nonlocal write_calls
        write_calls += 1
        if write_calls >= 1:
            raise RuntimeError("fixture write cancellation")

    with pytest.raises(RuntimeError, match="fixture write cancellation"):
        materialize_email_attachments(
            parent,
            tmp_path / "write-cancel",
            apply=True,
            prepared=prepared,
            checkpoint=cancel_during_write,
        )
    assert not (tmp_path / "write-cancel").exists()


def test_part_count_and_depth_are_rejected_before_unbounded_tree_growth(tmp_path: Path) -> None:
    parent = tmp_path / "many-parts.eml"
    _message(parent, attachments=40)
    with pytest.raises(EmailAttachmentError, match="attachment_part_count_budget"):
        prepare_email_attachments(
            parent,
            limits=EmailAttachmentLimits(max_parts=5, max_total_bytes=64 * 1024 * 1024),
        )


def test_mime_depth_is_rejected_during_feed(tmp_path: Path) -> None:
    nested = "Content-Type: application/pdf\nContent-Disposition: attachment; filename=x.pdf\n\nbody\n"
    for index in range(20, -1, -1):
        boundary = f"b{index}"
        nested = (
            f'Content-Type: multipart/mixed; boundary="{boundary}"\n\n'
            f"--{boundary}\n{nested}--{boundary}--\n"
        )
    parent = tmp_path / "deep.eml"
    parent.write_text(nested, encoding="utf-8")
    with pytest.raises(EmailAttachmentError, match="attachment_depth_budget"):
        prepare_email_attachments(
            parent,
            limits=EmailAttachmentLimits(max_depth=4, max_total_bytes=64 * 1024 * 1024),
        )


def test_utf8_filename_is_bounded_in_bytes_including_ordinal_prefix(tmp_path: Path) -> None:
    parent = tmp_path / "unicode.eml"
    _message(parent, filename="é" * 180 + ".pdf")
    planned = materialize_email_attachments(parent, tmp_path / "children")
    for item in planned.attachments:
        assert len(Path(item.child_path).name.encode("utf-8")) <= 255
