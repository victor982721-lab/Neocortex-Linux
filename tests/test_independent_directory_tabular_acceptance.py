"""Independent C02/C04/C12 acceptance probes with synthetic data only.

These tests deliberately exercise the public seams rather than copying the
author test fixtures wholesale.  They do not invoke native KIO or any real
Corpus/state database.
"""

from __future__ import annotations

import sqlite3
import json
import zlib
from pathlib import Path

import pytest

import neocortex.capabilities.formats.text.text_route as text_route_module
from neocortex.safety import kio_trash
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.deduplication import snapshot_path
from neocortex.safety.kio_trash import (
    KioTrashUnavailable,
    restore_trash_receipt,
    validate_empty_directory_source,
)
from neocortex.semantic.semantic_tabular_projection import metadata_table_section
from neocortex.runtime.control.cancellation import CancellationToken
from tests.test_text_route import FakeTextFrameworkState
from tests.test_linux_empty_directory_trash import _TrashFixture, _apply_directory


def test_c02_empty_directory_public_admission_rejects_symlink_race_and_contents(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = root / "empty"
    source.mkdir()
    expected = snapshot_path(source)

    source.rmdir()
    source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(KioTrashUnavailable):
        validate_empty_directory_source(source, expected, root=root)

    source.unlink()
    source.mkdir()
    expected = snapshot_path(source)
    (source / "late-entry").write_text("preserve", encoding="utf-8")
    with pytest.raises(KioTrashUnavailable, match=r"not_empty|identity_changed"):
        validate_empty_directory_source(source, expected, root=root)


def test_c02_restore_claim_preserves_foreign_trashinfo_on_claim_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _TrashFixture(tmp_path)
    source, expected = fixture.directory()
    outcome = _apply_directory(fixture, source, expected)
    assert outcome.receipt_json is not None
    receipt = json.loads(outcome.receipt_json)
    info_path = Path(receipt["trash"]["info_path"])
    original_rename = kio_trash._renameat2_noreplace

    def race(source_path, destination_path, *, expected, object_kind="regular_file"):
        if Path(source_path) == info_path:
            info_path.write_text("[Trash Info]\nPath=/foreign\n", encoding="utf-8")
        return original_rename(
            source_path,
            destination_path,
            expected=expected,
            object_kind=object_kind,
        )

    monkeypatch.setattr(kio_trash, "_renameat2_noreplace", race)
    with pytest.raises(KioTrashUnavailable, match="metadata"):
        restore_trash_receipt(outcome.receipt_json, root=fixture.root)
    assert info_path.read_text(encoding="utf-8") == "[Trash Info]\nPath=/foreign\n"
    assert not source.exists()
    assert Path(receipt["trash"]["trash_path"]).is_dir()


def test_c02_restore_positive_cleanup_leaves_no_private_metadata_claim(
    tmp_path: Path,
) -> None:
    fixture = _TrashFixture(tmp_path)
    source, expected = fixture.directory()
    outcome = _apply_directory(fixture, source, expected)
    assert outcome.receipt_json is not None
    restored = restore_trash_receipt(outcome.receipt_json, root=fixture.root)
    assert restored["status"] == "restored"
    assert not list(fixture.trash.joinpath("info").glob(".neocortex-kio-restore-info-*"))

def test_c04_old_text_signature_cannot_reuse_xml_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "invoice.xml"
    source.write_text(
        '<cfdi:Comprobante xmlns:cfdi="urn:synthetic" Total="112.455" '
        'Certificado="' + ("B" * 300) + '"><cfdi:Concepto Descripcion="Servicio"/></cfdi:Comprobante>',
        encoding="utf-8",
    )
    state = tmp_path / "text.sqlite3"
    candidates = {"application/xml": (snapshot_path(source),)}
    monkeypatch.setattr(text_route_module, "TEXT_ROUTE_VERSION", "text-route-v3")
    monkeypatch.setattr(
        text_route_module,
        "_TEXT_EXTRACTOR_CONTRACT_SHA256",
        "sha256:" + "0" * 64,
    )
    old = TextRoute(
        TextRouteConfig(state_path=state),
        FakeTextFrameworkState(candidates),
        1,
        cancellation=CancellationToken(),
    ).run()
    assert old.extracted == 1
    monkeypatch.undo()

    fresh = TextRoute(
        TextRouteConfig(state_path=state),
        FakeTextFrameworkState(candidates),
        2,
        cancellation=CancellationToken(),
    ).run()
    assert fresh.extracted == 1
    assert fresh.cache_hits == 0
    with sqlite3.connect(state) as connection:
        row = connection.execute(
            "SELECT processing_signature,text_zlib,text_chars FROM documents"
        ).fetchone()
    assert row is not None
    assert "text-route-v4" in row[0]
    assert int(row[2]) > 0
    assert "Total='112.455'" in zlib.decompress(row[1]).decode("utf-8")


def _synthetic_metadata_rows(count: int = 96) -> str:
    return "source,destination,kind\n" + "".join(
        f"/fixture/source-{index}.jsonl,/fixture/destination-{index}.jsonl,metadata\n"
        for index in range(count)
    )


def test_c12_late_narrative_row_abstains_after_full_scan() -> None:
    text = _synthetic_metadata_rows(96)
    text += '/fixture/late.jsonl,/fixture/late.jsonl,"' + ("narrative " * 30) + '"\n'
    checkpoints: list[int] = []
    section = metadata_table_section(
        text,
        "csv",
        checkpoint=lambda: checkpoints.append(len(checkpoints)),
    )
    assert section is None
    assert len(checkpoints) >= 2


def test_c12_projection_is_advisory_and_does_not_change_input_bytes() -> None:
    original = _synthetic_metadata_rows(100)
    section = metadata_table_section(original, "csv", checkpoint=lambda: None)
    assert section is not None
    assert section.provenance["advisory_only"] is True
    assert section.provenance["original_rows_unchanged"] is True
    assert "source-99" not in section.text
    assert "source-99" in original
