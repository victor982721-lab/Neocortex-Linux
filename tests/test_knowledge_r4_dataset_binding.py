"""Binding checks for the sealed synthetic CA-12 R4 reserve."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "knowledge_functional_v4"
VARIANTS = ["full", "no_expansion", "no_semantic", "no_catalog"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_r4_reserve_is_sealed_and_predeclared() -> None:
    freeze = json.loads((FIXTURES / "freeze.json").read_text(encoding="utf-8"))
    reserve = freeze["reserve"]
    assert freeze["schema"] == "neocortex.functional-freeze/v2"
    assert freeze["dataset_id"] == "FUNCTIONAL_R4_CA12"
    assert freeze["synthetic_only"] is True
    assert "steward-only" in freeze["reserve_visibility"]
    assert freeze["status"] == "FROZEN_BEFORE_CANDIDATE_ABLATION"
    assert freeze["ca12_variants"] == VARIANTS
    assert freeze["variants_predeclared"] == VARIANTS
    assert reserve["files"] == reserve["logical_resources"] == 16
    assert reserve["queries"] == 10
    assert reserve["positive_queries"] == 8
    assert reserve["negative_queries"] == 2
    assert _sha256(FIXTURES / "reserve-r4.tar.aes") == freeze["reserve_archive_sha256"]
    assert not (FIXTURES / "reserve").exists()
    assert not (FIXTURES / "queries.json").exists()
    assert not (FIXTURES / "reserve-r4.key").exists()


def test_r4_public_checksum_manifest_is_complete() -> None:
    rows = (FIXTURES / "SHA256SUMS.tsv").read_text(encoding="utf-8").splitlines()
    names = {row.split("  ", 1)[1] for row in rows if row.strip()}
    assert names == {
        "freeze.json",
        "README.md",
        "operationalization-v2.2-r4.json",
        "reserve-r4.tar.aes",
    }
    for row in rows:
        digest, name = row.split("  ", 1)
        assert _sha256(FIXTURES / name) == digest
