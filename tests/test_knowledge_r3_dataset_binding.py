"""Binding checks for the sealed synthetic CA-12 R3 reserve."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "knowledge_functional_v3"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_r3_reserve_is_sealed_and_bound_before_execution() -> None:
    freeze = json.loads((FIXTURES / "freeze.json").read_text(encoding="utf-8"))
    assert freeze["schema"] == "neocortex.functional-freeze/v2"
    assert freeze["dataset_id"] == "FUNCTIONAL_R3_CA12"
    assert freeze["synthetic_only"] is True
    assert "steward-only" in freeze["reserve_visibility"]
    reserve = freeze["reserve"]
    assert reserve["files"] == reserve["logical_resources"] == 16
    assert reserve["queries"] == 10
    assert reserve["positive_queries"] == 8
    assert reserve["negative_queries"] == 2
    assert _sha256(FIXTURES / "reserve-r3.tar.aes") == freeze["reserve_archive_sha256"]
    assert not (FIXTURES / "reserve").exists()
    assert not (FIXTURES / "queries.json").exists()
    assert not (FIXTURES / "reserve-r3.key").exists()


def test_r3_manifest_checksum_lists_only_public_binding_files() -> None:
    rows = (FIXTURES / "SHA256SUMS.tsv").read_text(encoding="utf-8").splitlines()
    names = {row.split("  ", 1)[1] for row in rows if row.strip()}
    assert names == {
        "freeze.json",
        "README.md",
        "operationalization-v2.2-r3.json",
        "reserve-r3.tar.aes",
    }
    for row in rows:
        digest, name = row.split("  ", 1)
        assert _sha256(FIXTURES / name) == digest
