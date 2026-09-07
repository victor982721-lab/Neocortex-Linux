"""Focal safety and aggregate-contract tests for the CA-12 runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import benchmark_knowledge_ablation as ablation


def _controls() -> dict[str, int]:
    return {
        "expansion_nonempty": 1,
        "expansion_calls": 10,
        "semantic_planned_queries": 10,
        "catalog_planned_queries": 10,
        "semantic_executed_queries": 10,
        "catalog_executed_queries": 10,
    }


def test_runner_has_exactly_the_predeclared_variants() -> None:
    assert ablation.VARIANTS == ("full", "no_expansion", "no_semantic", "no_catalog")


def test_effective_controls_are_required_and_missing_control_fails_closed() -> None:
    full = {"control": _controls()}
    results = {
        "full": full,
        "no_expansion": {
            "control": {
                **_controls(),
                "expansion_nonempty": 0,
            }
        },
        "no_semantic": {
            "control": {
                **_controls(),
                "semantic_planned_queries": 0,
                "semantic_executed_queries": 0,
            }
        },
        "no_catalog": {
            "control": {
                **_controls(),
                "catalog_planned_queries": 0,
                "catalog_executed_queries": 0,
            }
        },
    }
    ablation._check_controls(results)
    results["no_catalog"]["control"]["catalog_planned_queries"] = 1
    with pytest.raises(ablation.AblationError, match="no_catalog"):
        ablation._check_controls(results)


def test_tree_digest_rejects_symlinked_immutable_input(tmp_path: Path) -> None:
    root = tmp_path / "input"
    root.mkdir()
    target = tmp_path / "target"
    target.write_text("not an input", encoding="utf-8")
    (root / "alias").symlink_to(target)
    with pytest.raises(ablation.AblationError, match="symlink"):
        ablation._tree_digest(root)


def test_tree_digest_allows_contained_model_cache_aliases(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    blob = root / "blobs" / "abc"
    blob.parent.mkdir()
    blob.write_bytes(b"model-bytes")
    alias = root / "snapshots" / "current"
    alias.parent.mkdir()
    alias.symlink_to("../blobs/abc")
    before = ablation._tree_digest(root, allow_symlinks=True)
    blob.write_bytes(b"changed-model-bytes")
    after = ablation._tree_digest(root, allow_symlinks=True)
    assert before != after


def test_worker_protocol_contains_no_per_query_output_path() -> None:
    source = ablation._worker_code()
    assert 'print(json.dumps({"aggregate": aggregate, "control": control}' in source
    assert "query_id" not in source


def test_validate_spec_rejects_variant_substitution_before_any_run(tmp_path: Path) -> None:
    spec = {
        "schema": ablation.SCHEMA,
        "candidate_sha": "a" * 40,
        "reserve_freeze_sha256": "b" * 64,
        "variants": ["full", "no_expansion", "no_semantic", "pretend_no_catalog"],
        "reserve_freeze": str(tmp_path / "freeze.json"),
        "reserve_root": str(tmp_path / "reserve"),
        "launcher": str(tmp_path / "launcher"),
        "model_cache": str(tmp_path / "cache"),
        "workspace_root": str(tmp_path / "workspace"),
    }
    with pytest.raises(ablation.AblationError, match="variants"):
        ablation.validate_spec(spec)


def test_load_reserve_rejects_logical_or_query_binding_changes(tmp_path: Path) -> None:
    root = tmp_path / "reserve"
    (root / "corpus").mkdir(parents=True)
    source = "Registro Alfa confirmó la recepción del lote 9.\n"
    item = root / "corpus" / "A.txt"
    item.write_text(source, encoding="utf-8")
    digest = ablation.hashlib.sha256(source.encode()).hexdigest()
    manifest = {
        "schema": "neocortex.functional-fixtures/v1",
        "split": "reserve",
        "files": [
            {
                "bytes": item.stat().st_size,
                "logical_resource_id": "alpha",
                "path": "corpus/A.txt",
                "revision_pin": digest,
                "sha256": ablation.sha256(item),
                "source_text": source,
                "synthetic": True,
            }
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    queries = {
        "schema": "neocortex.functional-judgments/v1",
        "queries": [
            {"query_id": "Q1", "kind": "positive", "relevance": {"other": 3}, "text": "x"}
        ],
    }
    (root / "queries.json").write_text(json.dumps(queries), encoding="utf-8")
    freeze = {
        "reserve": {
            "files": 1,
            "logical_resources": 1,
            "queries": 1,
            "positive_queries": 1,
            "negative_queries": 0,
            "manifest_sha256": ablation.sha256(root / "manifest.json"),
            "queries_sha256": ablation.sha256(root / "queries.json"),
        }
    }
    with pytest.raises(ablation.AblationError, match="unknown or invalid resource"):
        ablation._load_reserve(root, freeze)


def test_report_shape_is_aggregate_only() -> None:
    report = {
        "schema": ablation.SCHEMA,
        "candidate_sha": "a" * 40,
        "reserve_freeze_sha256": "b" * 64,
        "criteria": {"aggregate_only": True},
        "variants": {name: {"aggregate": {}, "control": {}} for name in ablation.VARIANTS},
    }
    encoded = json.dumps(report)
    assert "query_id" not in encoded
    assert '"text"' not in encoded
