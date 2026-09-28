"""A partially retained attachment tree must not break identity-bound replay."""
from email.message import EmailMessage
from pathlib import Path

import pytest

from neocortex.capabilities.formats.text.email_intake import (
    EmailAttachmentError,
    materialize_email_attachments,
)


def test_mixed_local_and_renamed_children_replay_without_creating_files(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "message.eml"
    message = EmailMessage()
    message.set_content("Synthetic attachment replay fixture.")
    message.add_attachment(b"first fixture", maintype="application", subtype="octet-stream", filename="one.bin")
    message.add_attachment(b"second fixture", maintype="application", subtype="octet-stream", filename="two.bin")
    source.write_bytes(message.as_bytes())
    destination = corpus / "children"
    manifest = tmp_path / "state" / "manifest.json"
    first = materialize_email_attachments(source, destination, apply=True, manifest_path=manifest)
    original = Path(first.attachments[0].child_path)
    moved = corpus / "renamed-child.bin"
    original.rename(moved)
    still_local = Path(first.attachments[1].child_path)
    paths = {1: moved, 2: still_local}
    before = {str(p): p.read_bytes() for p in corpus.rglob("*") if p.is_file()}
    for _ in range(2):
        replay = materialize_email_attachments(
            source, destination, apply=True, manifest_path=manifest,
            resolver=lambda child: paths[child.ordinal],
        )
        assert replay.status == "replayed"
        assert [item.child_path for item in replay.attachments] == [str(moved), str(still_local)]
        assert {str(p): p.read_bytes() for p in corpus.rglob("*") if p.is_file()} == before
    foreign = destination / "unbound.bin"
    foreign.write_bytes(b"not an attachment")
    with pytest.raises(EmailAttachmentError, match="manifest_extra_or_missing_child"):
        materialize_email_attachments(
            source, destination, apply=True, manifest_path=manifest,
            resolver=lambda child: paths[child.ordinal],
        )
    assert foreign.read_bytes() == b"not an attachment"


def test_consumed_zip_successor_is_not_an_unbound_attachment(tmp_path: Path) -> None:
    from neocortex.capabilities.formats.archive.intake import SourceIdentity
    from neocortex.capabilities.formats.text.email_intake import EmailAttachmentResolution
    import hashlib
    import io
    import zipfile

    root = tmp_path / "corpus"
    root.mkdir()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("note.txt", "fixture child")
    message = EmailMessage()
    message.set_content("Fixture message.")
    message.add_attachment(buffer.getvalue(), maintype="application", subtype="zip", filename="nested.zip")
    message.add_attachment(b"local fixture", maintype="application", subtype="octet-stream", filename="local.bin")
    source = root / "message.eml"
    source.write_bytes(message.as_bytes())
    destination = root / "children"
    manifest = tmp_path / "state" / "manifest.json"
    first = materialize_email_attachments(source, destination, apply=True, manifest_path=manifest)
    archive_path = Path(first.attachments[0].child_path)
    sibling = Path(first.attachments[1].child_path)
    successor = archive_path.with_suffix("")
    successor.mkdir()
    (successor / "note.txt").write_text("fixture child")
    receipt = {
        "status": "applied", "published": True, "trashed": True,
        "source_identity": SourceIdentity.capture(archive_path).to_dict(),
        "source_sha256": hashlib.sha256(buffer.getvalue()).hexdigest(),
        "successor_paths": [str(successor)],
    }
    archive_path.unlink()

    def resolve(child):
        return (EmailAttachmentResolution(None, "consumed_archive", receipt)
                if child.ordinal == 1 else sibling)

    for _ in range(2):
        replay = materialize_email_attachments(
            source, destination, apply=True, manifest_path=manifest, resolver=resolve,
        )
        assert replay.status == "replayed"
        assert replay.attachments[0].status == "consumed_archive"
        assert not archive_path.exists()
        assert (successor / "note.txt").read_text() == "fixture child"
    successor.rename(root / "old-successor")
    successor.symlink_to(root / "old-successor", target_is_directory=True)
    with pytest.raises(EmailAttachmentError, match="historical_successor_not_directory"):
        materialize_email_attachments(source, destination, apply=True, manifest_path=manifest, resolver=resolve)
