"""R2 publication/binding checks only; reserved questions remain opaque."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

import pytest

from tools.benchmark_knowledge_functional import (
    FROZEN_DATASET_SHA256,
    FROZEN_R2_DATASET_SHA256,
    run,
    sha256,
    verify_freeze,
)
from tools.knowledge_functional_v2_metrics import verify_operationalization


FIXTURES = Path(__file__).parent / "fixtures"


def test_r2_is_new_pinned_pool_without_rewriting_original_dev_or_v1():
    original = FIXTURES / "knowledge_functional_v1"
    replacement = FIXTURES / "knowledge_functional_v2"
    old = json.loads((original / "freeze.json").read_text())
    new = json.loads((replacement / "freeze.json").read_text())
    assert sha256(original / "freeze.json") == FROZEN_DATASET_SHA256
    assert sha256(replacement / "freeze.json") == FROZEN_R2_DATASET_SHA256
    assert new["dev"] == old["dev"]
    assert new["thresholds"] == old["thresholds"]
    assert new["total_fixture_files"] == 40 and new["total_queries"] == 30
    assert new["reserve"]["files"] == 16
    assert new["reserve"]["positive_queries"] == 8
    assert new["reserve"]["negative_queries"] == 2
    assert new["family_disjoint_from_original_dev_and_retired_r1"] is True
    assert sha256(replacement / "reserve-r2.tar.aes") == new["reserve_archive_sha256"]
    assert not (replacement / "reserve" / "queries.json").exists()
    verify_freeze(original / "dev", replacement / "freeze.json")


def test_r2_binding_reuses_the_same_metric_implementation():
    replacement = FIXTURES / "knowledge_functional_v2"
    path = replacement / "operationalization-v2.1-r2.json"
    contract = json.loads(path.read_text())
    assert contract["dataset_binding_only"] is True
    assert contract["metric_implementation_changed"] is False
    assert (
        contract["adapter_sha256"]
        == "acb0728fb351c67457d03fff4da7b745de21c2cfb6fd9ad26ec1840c2ea82795"
    )
    with pytest.raises(ValueError, match="incompatible"):
        verify_operationalization(path, frozen_dataset_sha256=FROZEN_R2_DATASET_SHA256)
    updated = replacement / "operationalization-v2.2-r2.json"
    assert json.loads(updated.read_text())["dataset_binding_only"] is True
    verify_operationalization(updated, frozen_dataset_sha256=FROZEN_R2_DATASET_SHA256)


def test_retired_r1_cannot_be_silently_run_as_an_independent_candidate(tmp_path):
    # The retired material is already authorized development evidence. This
    # check reads only its preserved manifest, judgments and synthetic files.
    retired = FIXTURES / "knowledge_development_expanded_r1"
    source = tmp_path / "retired"
    source.mkdir()
    provenance = retired / "provenance"
    manifests = [
        path
        for path in provenance.rglob("*.json")
        if json.loads(path.read_text()).get("split") == "reserve"
    ]
    manifest_path = next(path for path in manifests if "files" in json.loads(path.read_text()))
    query_path = next(path for path in manifests if "queries" in json.loads(path.read_text()))
    shutil.copyfile(manifest_path, source / "manifest.json")
    shutil.copyfile(query_path, source / "queries.json")
    manifest = json.loads((source / "manifest.json").read_text())
    (source / "corpus").mkdir()
    for item in manifest["files"]:
        shutil.copyfile(retired / "corpus" / Path(item["path"]).name, source / item["path"])
    args = argparse.Namespace(
        fixtures=source,
        freeze=FIXTURES / "knowledge_functional_v1" / "freeze.json",
        label="candidate",
    )
    with pytest.raises(ValueError, match="retired R1 cannot be reused"):
        run(args)
