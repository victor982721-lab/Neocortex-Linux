"""Synthetic C04/C05 format coverage: XML fields and EML child lineage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.text.email_intake import (
    EmailAttachmentError,
    EmailAttachmentResolution,
    EmailAttachmentLimits,
    materialize_email_attachments,
)
from neocortex.capabilities.formats.text.text_route import TextRouteConfig, _extract_builtin
from neocortex.capabilities.runtime import TEXT_BUILTIN_IMPLEMENTATION_ID


def _selection() -> SimpleNamespace:
    return SimpleNamespace(
        selected=SimpleNamespace(implementation_id=TEXT_BUILTIN_IMPLEMENTATION_ID)
    )


def test_cfdi_attributes_are_searchable_but_seals_are_not() -> None:
    payload = (
        b'<cfdi:Comprobante xmlns:cfdi="urn:cfdi" Version="4.0" '
        b'Fecha="2026-01-01" Total="112.455" Moneda="MXN" '
        b'Sello="' + b"A" * 300 + b'" Certificado="' + b"B" * 300 + b'">'
        b'<cfdi:Emisor Rfc="AAA010101AAA"/>'
        b'<cfdi:CfdiRelacionados><cfdi:CfdiRelacionado UUID="u-1"/></cfdi:CfdiRelacionados>'
        b'<cfdi:Concepto Descripcion="Servicio de mantenimiento" Importe="112.455"/>'
        b"</cfdi:Comprobante>"
    )
    extracted = _extract_builtin(
        payload,
        "application/xml",
        "factura.xml",
        TextRouteConfig(Path("/unused"), max_text_chars=4_096),
        _selection(),
    )

    assert "Total='112.455'" in extracted.text
    assert "Descripcion='Servicio de mantenimiento'" in extracted.text
    assert "AAA010101AAA" in extracted.text
    assert "A" * 300 not in extracted.text
    assert "B" * 300 not in extracted.text
    provenance = extracted.metadata["xml_provenance"]
    assert provenance["excluded_attribute_count"] == 2
    assert provenance["relationship_classification"]["valued"] == 2
    assert "cfdi" in {row["prefix"] for row in provenance["namespace_map"]}


def test_empty_xml_relationship_is_preserved_as_classification() -> None:
    payload = b'<r xmlns="urn:test"><Relationship/></r>'
    extracted = _extract_builtin(
        payload,
        "application/xml",
        "relationships.xml",
        TextRouteConfig(Path("/unused"), max_text_chars=256),
        _selection(),
    )
    assert "empty relationship" in extracted.text
    assert extracted.metadata["xml_provenance"]["relationship_classification"]["empty"] == 1


def _write_eml(path: Path) -> None:
    path.write_bytes(
        b"From: sender@example.test\n"
        b"To: receiver@example.test\n"
        b"Subject: Synthetic attachments\n"
        b"MIME-Version: 1.0\n"
        b"Content-Type: multipart/mixed; boundary=fixture\n\n"
        b"--fixture\nContent-Type: text/plain; charset=utf-8\n\nBody\n"
        b"--fixture\nContent-Type: application/pdf\n"
        b"Content-Disposition: attachment; filename=../../report.pdf\n"
        b"Content-Transfer-Encoding: base64\n\n"
        b"cGRmLW9uZQ==\n"
        b"--fixture\nContent-Type: application/pdf\n"
        b"Content-Disposition: attachment; filename=report.pdf\n"
        b"Content-Transfer-Encoding: base64\n\n"
        b"cGRmLXR3bw==\n"
        b"--fixture--\n"
    )


def test_eml_children_are_no_replace_lineage_bound_and_replayable(tmp_path: Path) -> None:
    parent = tmp_path / "parent.eml"
    _write_eml(parent)
    original = parent.read_bytes()
    destination = tmp_path / "children"

    plan = materialize_email_attachments(parent, destination)
    assert plan.status == "planned"
    assert not destination.exists()

    applied = materialize_email_attachments(parent, destination, apply=True)
    assert applied.status == "applied"
    assert parent.read_bytes() == original
    assert [item.status for item in applied.attachments] == ["materialized", "materialized"]
    names = [Path(item.child_path).name for item in applied.attachments]
    assert names[0].startswith("0001--attachment-") and names[0].endswith(".pdf")
    assert names[1] == "0002--report.pdf"
    manifest = json.loads(Path(applied.manifest_path).read_text(encoding="utf-8"))
    assert manifest["parent_sha256"]
    assert manifest["parent_path"] == str(parent)
    assert materialize_email_attachments(parent, destination, apply=True).status == "replayed"


def test_eml_attachment_budgets_fail_before_any_child_is_written(tmp_path: Path) -> None:
    parent = tmp_path / "parent.eml"
    _write_eml(parent)
    with pytest.raises(EmailAttachmentError, match="attachment_part_budget"):
        materialize_email_attachments(
            parent,
            tmp_path / "children",
            apply=True,
            limits=EmailAttachmentLimits(max_part_bytes=2, max_total_bytes=8),
        )
    assert not (tmp_path / "children" / "0001--report.pdf").exists()


def test_manifest_cannot_shrink_or_add_unbound_children_on_replay(tmp_path: Path) -> None:
    parent = tmp_path / "parent.eml"
    _write_eml(parent)
    destination = tmp_path / "children"
    applied = materialize_email_attachments(parent, destination, apply=True)
    manifest_path = Path(applied.manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["attachments"] = []
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EmailAttachmentError, match="manifest_attachment_count_mismatch"):
        materialize_email_attachments(parent, destination, apply=True)


def test_manifest_rejects_unbound_extra_child(tmp_path: Path) -> None:
    parent = tmp_path / "parent.eml"
    _write_eml(parent)
    destination = tmp_path / "children"
    materialize_email_attachments(parent, destination, apply=True)
    (destination / "9999--foreign.pdf").write_bytes(b"foreign")
    with pytest.raises(EmailAttachmentError, match="manifest_extra_or_missing_child"):
        materialize_email_attachments(parent, destination, apply=True)


def test_parent_move_is_replayed_by_stable_physical_identity(tmp_path: Path) -> None:
    parent = tmp_path / "parent.eml"
    _write_eml(parent)
    destination = tmp_path / "children"
    first = materialize_email_attachments(parent, destination, apply=True)
    moved = tmp_path / "moved-parent.eml"
    parent.rename(moved)

    replay = materialize_email_attachments(moved, destination, apply=True)
    assert replay.status == "replayed"
    assert replay.parent_path == str(moved)
    assert replay.parent_identity.inode == first.parent_identity.inode


def test_children_can_rebind_through_verified_physical_resolver(tmp_path: Path) -> None:
    parent = tmp_path / "parent.eml"
    _write_eml(parent)
    destination = tmp_path / "children"
    first = materialize_email_attachments(parent, destination, apply=True)
    rebound_root = tmp_path / "relocated"
    rebound_root.mkdir()
    relocated: dict[int, Path] = {}
    for item in first.attachments:
        original = Path(item.child_path)
        target = rebound_root / original.name
        original.rename(target)
        relocated[item.ordinal] = target

    def resolver(item):
        return relocated[item.ordinal]

    replay = materialize_email_attachments(parent, destination, apply=True, resolver=resolver)
    assert replay.status == "replayed"
    assert {Path(item.child_path).parent for item in replay.attachments} == {rebound_root}

    # A resolver cannot substitute a same-sized but different file.
    wrong = rebound_root / "wrong.pdf"
    wrong.write_bytes(b"wrong")
    relocated[1] = wrong
    with pytest.raises(EmailAttachmentError):
        materialize_email_attachments(parent, destination, apply=True, resolver=resolver)


def test_content_equivalent_keeper_reuse_is_explicit_and_does_not_recreate(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.eml"
    _write_eml(parent)
    destination = tmp_path / "children"
    state_manifest = tmp_path / "state" / "attachments.json"
    first = materialize_email_attachments(
        parent,
        destination,
        apply=True,
        manifest_path=state_manifest,
    )
    keeper_root = tmp_path / "dedup-keeper"
    keeper_root.mkdir()
    keepers: dict[int, Path] = {}
    for item in first.attachments:
        original = Path(item.child_path)
        keeper = keeper_root / original.name
        keeper.write_bytes(original.read_bytes())
        original.unlink()
        keepers[item.ordinal] = keeper
    destination.rmdir()

    def resolver(item):
        return EmailAttachmentResolution(keepers[item.ordinal], "content_equivalent")

    with pytest.raises(EmailAttachmentError, match="content_equivalent_reuse_not_authorized"):
        materialize_email_attachments(
            parent,
            destination,
            apply=True,
            manifest_path=state_manifest,
            resolver=resolver,
        )
    replay = materialize_email_attachments(
        parent,
        destination,
        apply=True,
        manifest_path=state_manifest,
        resolver=resolver,
        allow_content_equivalent=True,
    )
    assert replay.status == "replayed"
    assert not destination.exists()
    assert all(item.child_reuse_kind == "content_equivalent" for item in replay.attachments)
    assert all(item.previous_child_inode is not None for item in replay.attachments)

    keepers[1].write_bytes(b"altered")
    with pytest.raises(EmailAttachmentError):
        materialize_email_attachments(
            parent,
            destination,
            apply=True,
            manifest_path=state_manifest,
            resolver=resolver,
            allow_content_equivalent=True,
        )
