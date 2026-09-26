"""Focused bounded Identify coverage independent from the action pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.platform.content_types import (
    DETECTOR_VERSION,
    FileTypeDecision,
    detect_content_type,
    identify,
)


@pytest.mark.parametrize("name", ("payload.txt", "payload.md", "payload.bak", "payload"))
def test_json_is_identified_from_content_before_suffix(tmp_path: Path, name: str) -> None:
    source = tmp_path / name
    source.write_text('{"unit": "U1", "value": 42}\n', encoding="utf-8")

    detected = detect_content_type(source)
    decision = identify(source)

    assert detected is not None
    assert detected.mime == "application/json"
    assert detected.canonical_extension == ".json"
    assert not detected.accepts(source)
    assert isinstance(decision, FileTypeDecision)
    assert decision.status == "known"
    assert decision.path == str(source)
    assert decision.detected_type == detected
    assert decision.mime == "application/json"
    assert decision.canonical_extension == ".json"
    assert decision.confidence == "high"


def test_xml_without_extension_is_structurally_identified(tmp_path: Path) -> None:
    source = tmp_path / "export"
    source.write_text('<?xml version="1.0"?><root><item id="1" /></root>', encoding="utf-8")

    decision = identify(source)

    assert decision.status == "known"
    assert decision.mime == "application/xml"
    assert decision.canonical_extension == ".xml"
    assert decision.evidence.endswith(":xml")


def test_html_without_extension_is_not_misclassified_as_plain_text(tmp_path: Path) -> None:
    source = tmp_path / "page"
    source.write_text(
        "<!doctype html><html><head><title>Fixture</title></head>"
        "<body><p>Contenido</p></body></html>",
        encoding="utf-8",
    )

    decision = identify(source)

    assert decision.status == "known"
    assert decision.mime == "text/html"
    assert decision.canonical_extension == ".html"
    assert decision.accepts(source) is False


def test_rfc822_without_eml_suffix_uses_header_evidence(tmp_path: Path) -> None:
    source = tmp_path / "message"
    source.write_bytes(
        b"From: sender@example.test\r\n"
        b"To: recipient@example.test\r\n"
        b"Subject: Fixture\r\n\r\n"
        b"Body\r\n"
    )

    decision = identify(source)

    assert decision.status == "known"
    assert decision.mime == "message/rfc822"
    assert decision.canonical_extension == ".eml"


def test_csv_and_tsv_require_consistent_rows(tmp_path: Path) -> None:
    csv_source = tmp_path / "table.txt"
    csv_source.write_text("name,value\nunit,42\n", encoding="utf-8")
    tsv_source = tmp_path / "table.data"
    tsv_source.write_text("name\tvalue\nunit\t42\n", encoding="utf-8")

    csv_decision = identify(csv_source)
    tsv_decision = identify(tsv_source)

    assert csv_decision.mime == "text/csv"
    assert csv_decision.canonical_extension == ".csv"
    assert tsv_decision.mime == "text/tab-separated-values"
    assert tsv_decision.canonical_extension == ".tsv"


def test_prose_with_unmatched_delimiters_remains_unknown_when_suffix_is_ambiguous(
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.bak"
    source.write_text(
        "Esta es prosa, con una coma ocasional.\n"
        "La segunda línea no forma una tabla.\n",
        encoding="utf-8",
    )

    decision = identify(source)

    assert decision.status == "unknown"
    assert decision.mime is None
    assert decision.canonical_extension is None
    assert decision.accepted_extensions == frozenset()


def test_markdown_structure_is_detected_without_forcing_plain_text(tmp_path: Path) -> None:
    source = tmp_path / "README"
    source.write_text("# Fixture\n\n- conserva originales\n", encoding="utf-8")

    decision = identify(source)

    assert decision.status == "known"
    assert decision.mime == "text/markdown"
    assert decision.canonical_extension == ".md"
    assert decision.evidence.endswith(":markup-structure")


def test_unknown_binary_or_ambiguous_payload_has_explicit_unknown_decision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ambiguous.bin"
    source.write_bytes(b"\x00\x01\x02not a recognized bounded format")

    decision = identify(source)

    assert decision == FileTypeDecision(
        path=str(source),
        detected_type=None,
        mime=None,
        canonical_extension=None,
        accepted_extensions=frozenset(),
        confidence="none",
        evidence="unknown",
        status="unknown",
    )


def test_detector_version_is_bumped_for_structural_text_behavior() -> None:
    assert DETECTOR_VERSION == "content-types-v5"
