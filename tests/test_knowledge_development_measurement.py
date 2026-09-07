"""Structural development-capture contracts, not scientific queries or model runs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from tools import knowledge_development_measurement as development


def _json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    root = tmp_path / "union"
    all_files, all_queries, freeze = [], [], {"schema": "neocortex.functional-freeze/v1"}
    for group, role, split, count, logical_count, positives, negatives in development.GROUPS:
        files, queries = [], []
        for index in range(count):
            logical = index if index < logical_count else 0
            source = f"Unit title {group}-{logical}\n\nUnit body {group}-{logical}"
            path = f"corpus/{group}-{index:03d}.txt"
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
            files.append(
                {
                    "fixture_id": f"{group}-{index:03d}",
                    "path": path,
                    "logical_resource_id": f"{group}-resource-{logical}",
                    "source_text": source,
                    "synthetic": True,
                    "bytes": len(source.encode()),
                    "sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "revision_pin": hashlib.sha256(source.encode()).hexdigest(),
                }
            )
        for index in range(positives + negatives):
            qid = f"{group}-unit-{index:03d}"
            queries.append(
                {
                    "query_id": qid,
                    "text": f"Unit query {qid}",
                    "kind": "positive" if index < positives else "negative",
                    "relevance": {f"{group}-resource-{index}": 3} if index < positives else {},
                }
            )
        manifest = {"schema": "neocortex.functional-fixtures/v1", "split": split, "files": files}
        judgments = {
            "schema": "neocortex.functional-judgments/v1",
            "split": split,
            "queries": queries,
        }
        freeze[split] = {
            "manifest_sha256": _json(root / "provenance" / role / "manifest.json", manifest),
            "queries_sha256": _json(root / "provenance" / role / "queries.json", judgments),
        }
        all_files.extend(files)
        all_queries.extend(queries)
    _json(
        root / "manifest.json",
        {
            "schema": "neocortex.functional-fixtures/v1",
            "split": development.DEVELOPMENT_SPLIT,
            "files": all_files,
        },
    )
    _json(
        root / "queries.json",
        {
            "schema": "neocortex.functional-judgments/v1",
            "split": development.DEVELOPMENT_SPLIT,
            "queries": all_queries,
        },
    )
    freeze_path = tmp_path / "unit-freeze.json"
    freeze_sha = _json(freeze_path, freeze)
    monkeypatch.setattr(development, "FROZEN_V1_SHA256", freeze_sha)
    return {"root": root, "freeze": freeze_path, "files": all_files, "queries": all_queries}


@pytest.fixture
def captured(dataset: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    capture_root = tmp_path / "captures"
    corpus = tmp_path / "captured-corpus"
    shutil.copytree(dataset["root"] / "corpus", corpus)
    rows = []
    for query in dataset["queries"]:
        qid = query["query_id"]
        row = {"query_id": qid}
        for kind in development.CAPTURE_KINDS:
            if kind in {"search", "context_search"}:
                payload = {
                    "query": query["text"],
                    "hits": [],
                    "complete": True,
                    "warnings": [],
                    "vectors_scanned": 0,
                    "plan": {"limit": 100 if kind == "search" else 10},
                }
            elif kind == "legacy_context":
                payload = {
                    "normalized_query": query["text"],
                    "selected_hits": [],
                    "citation_ids": [],
                    "rendered_context": "",
                }
            else:
                payload = {
                    "schema": "neocortex.context-response/v2",
                    "response_version": 2,
                    "read_only": True,
                    "query": query["text"],
                    "sources": [],
                    "citations": [],
                }
            row[kind] = f"{qid}-{kind}.json"
            row[f"{kind}_sha256"] = _json(capture_root / row[kind], payload)
        rows.append(row)
    manifest = {
        "schema": "neocortex.development-captures/v1",
        "evaluation_scope": "development_only",
        "dataset_split": development.DEVELOPMENT_SPLIT,
        "corpus_root": str(corpus),
        "producer": {
            "kind": "checkout",
            "source_sha": "a" * 40,
            "runtime_tree_sha256_before": "b" * 64,
            "runtime_tree_sha256_after": "b" * 64,
            "working_tree_modified": True,
        },
        "unique_query_count": 30,
        "model_query_count": 60,
        "model_embedding_query_count": 90,
        "ingestion_count": 0,
        "queries": rows,
    }
    manifest_path = capture_root / "capture-manifest.json"
    _json(manifest_path, manifest)
    operationalization = tmp_path / "unit-operationalization.json"
    _json(operationalization, {"unit_test_only": True})
    metrics = development._metrics_module()

    def verify(path: Path, *, frozen_dataset_sha256: str) -> str:
        assert path == operationalization
        assert frozen_dataset_sha256 == development.FROZEN_V1_SHA256
        return development.benchmark.sha256(path)

    monkeypatch.setattr(metrics, "verify_operationalization", verify)
    args = argparse.Namespace(
        label="development",
        fixtures=dataset["root"],
        freeze=dataset["freeze"],
        captured_responses=capture_root,
        capture_manifest=manifest_path,
        workspace=tmp_path / "measurement",
        context_response_version=1,
        operationalization=operationalization,
    )
    return {
        **dataset,
        "args": args,
        "capture_root": capture_root,
        "capture_manifest": manifest,
        "manifest_path": manifest_path,
        "corpus": corpus,
        "metrics": metrics,
    }


def _save_manifest(case: dict) -> None:
    _json(case["manifest_path"], case["capture_manifest"])


def _edit_payload(case: dict, kind: str, edit, index: int = 0) -> None:
    row = case["capture_manifest"]["queries"][index]
    path = case["capture_root"] / row[kind]
    payload = json.loads(path.read_text())
    edit(payload)
    row[f"{kind}_sha256"] = _json(path, payload)
    _save_manifest(case)


def test_real_retired_union_remains_exact_development_only() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    manifest, judgments, groups = development.load_development_union(
        fixtures / "knowledge_development_expanded_r1",
        fixtures / "knowledge_functional_v1" / "freeze.json",
    )
    assert len(manifest["files"]) == 40
    assert len(judgments["queries"]) == 30
    assert {name: len(ids) for name, ids in groups.items()} == {
        "original_dev": 20,
        "retired_r1": 10,
    }


def test_structural_union_preserves_groups_and_grades(dataset: dict) -> None:
    manifest, judgments, groups = development.load_development_union(
        dataset["root"], dataset["freeze"]
    )
    assert manifest["files"] == dataset["files"]
    assert judgments["queries"] == dataset["queries"]
    assert all(isinstance(ids, tuple) for ids in groups.values())
    assert set(groups["original_dev"]).isdisjoint(groups["retired_r1"])


@pytest.mark.parametrize("role", ["original-dev", "retired-r1"])
@pytest.mark.parametrize("filename", ["manifest.json", "queries.json"])
def test_provenance_requires_byte_identity_not_json_equivalence(
    dataset: dict, role: str, filename: str
) -> None:
    path = dataset["root"] / "provenance" / role / filename
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="provenance bytes changed"):
        development.load_development_union(dataset["root"], dataset["freeze"])


def test_freeze_pin_cannot_be_replaced(dataset: dict) -> None:
    dataset["freeze"].write_bytes(dataset["freeze"].read_bytes() + b"\n")
    with pytest.raises(ValueError, match="unchanged original V1 freeze"):
        development.load_development_union(dataset["root"], dataset["freeze"])


@pytest.mark.parametrize("change", ["grade", "text", "id", "order", "drop"])
def test_union_cannot_relabel_reorder_or_prune_queries(dataset: dict, change: str) -> None:
    path = dataset["root"] / "queries.json"
    data = json.loads(path.read_text())
    row = data["queries"][0]
    if change == "grade":
        row["relevance"][next(iter(row["relevance"]))] = 1
    elif change == "text":
        row["text"] += " altered"
    elif change == "id":
        row["query_id"] = "another-unit-id"
    elif change == "order":
        data["queries"].reverse()
    else:
        data["queries"].pop()
    _json(path, data)
    with pytest.raises(ValueError, match="exact original files or judgments"):
        development.load_development_union(dataset["root"], dataset["freeze"])


@pytest.mark.parametrize("split", ["reserve", "dev", "independent", "baseline"])
def test_retired_union_cannot_be_an_independent_split(dataset: dict, split: str) -> None:
    path = dataset["root"] / "manifest.json"
    data = json.loads(path.read_text())
    data["split"] = split
    _json(path, data)
    with pytest.raises(ValueError, match="independent split is forbidden"):
        development.load_development_union(dataset["root"], dataset["freeze"])


@pytest.mark.parametrize("version", [1, 2])
def test_grades_all_queries_separately_without_acceptance_or_model_execution(
    captured: dict, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("development diagnostics must not execute or certify a model")

    monkeypatch.setattr(development.benchmark, "acceptance", forbidden)
    monkeypatch.setattr(development.benchmark, "InstalledMeasurement", forbidden)
    monkeypatch.setattr(captured["metrics"], "acceptance_v2", forbidden)
    captured["args"].context_response_version = version
    report = development.grade_development_captures(captured["args"])
    assert report["scope"] == development.DEVELOPMENT_SCOPE
    assert report["model_execution_by_this_driver"] is False
    assert report["model_execution_independently_verified"] is False
    assert report["unique_query_count"] == 30
    assert report["model_query_count"] == 60
    assert report["aggregate"]["positive_queries"] == 24
    assert report["aggregate"]["negative_queries"] == 6
    assert len(report["queries"]) == 30
    assert report["groups"]["original_dev"]["aggregate"]["positive_queries"] == 16
    assert report["groups"]["original_dev"]["aggregate"]["negative_queries"] == 4
    assert report["groups"]["retired_r1"]["aggregate"]["positive_queries"] == 8
    assert report["groups"]["retired_r1"]["aggregate"]["negative_queries"] == 2
    assert report["groups"]["retired_r1"]["status"] == "RETIRED_HOLDOUT_R1_NOW_DEVELOPMENT"
    assert "acceptance" not in report and "baseline" not in report
    assert ("context_v2" in report) == (version == 2)
    if version == 2:
        assert report["context_v2"]["aggregate"]["positive_sufficiency_unknown_queries"] == 24
        assert report["context_v2"]["aggregate"]["contract_invalid_queries"] == 0
    assert json.loads((captured["args"].workspace / "measurement.json").read_text()) == report


@pytest.mark.parametrize("label", ["baseline", "candidate", "reserve"])
def test_label_cannot_reclassify_retired_data(captured: dict, label: str) -> None:
    captured["args"].label = label
    with pytest.raises(ValueError, match="cannot be retagged"):
        development.grade_development_captures(captured["args"])
    assert not captured["args"].workspace.exists()


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "unknown", "nonstring"])
def test_incomplete_or_ambiguous_capture_pool_is_not_measured(captured: dict, change: str) -> None:
    rows = captured["capture_manifest"]["queries"]
    if change == "missing":
        rows.pop()
    elif change == "extra":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif change == "nonstring":
        rows[0]["query_id"] = {"invalid": "unit-query"}
    else:
        rows[0]["query_id"] = "unknown-unit-query"
    _save_manifest(captured)
    with pytest.raises(ValueError, match="capture query"):
        development.grade_development_captures(captured["args"])
    assert not captured["args"].workspace.exists()


@pytest.mark.parametrize("kind", development.CAPTURE_KINDS)
@pytest.mark.parametrize("change", ["missing", "hash"])
def test_every_capture_is_required_and_hash_pinned(captured: dict, kind: str, change: str) -> None:
    row = captured["capture_manifest"]["queries"][0]
    path = captured["capture_root"] / row[kind]
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match=r"missing or uncontained|hash mismatch"):
        development.grade_development_captures(captured["args"])
    assert not captured["args"].workspace.exists()


@pytest.mark.parametrize(
    "name", ["../escape.json", "/absolute.json", "folder/../capture.json", "./capture.json"]
)
def test_capture_paths_cannot_escape_or_traverse(captured: dict, name: str) -> None:
    captured["capture_manifest"]["queries"][0]["search"] = name
    _save_manifest(captured)
    with pytest.raises(ValueError, match="strictly relative"):
        development.grade_development_captures(captured["args"])


@pytest.mark.parametrize("directory", [False, True])
def test_capture_symlinks_are_rejected(captured: dict, directory: bool) -> None:
    row = captured["capture_manifest"]["queries"][0]
    original = row["search"]
    if directory:
        (captured["capture_root"] / "alias").symlink_to(
            captured["capture_root"], target_is_directory=True
        )
        row["search"] = f"alias/{original}"
    else:
        (captured["capture_root"] / "alias.json").symlink_to(captured["capture_root"] / original)
        row["search"] = "alias.json"
    _save_manifest(captured)
    with pytest.raises(ValueError, match="symlink"):
        development.grade_development_captures(captured["args"])


@pytest.mark.parametrize(
    "kind,key",
    [("search", "query"), ("context_search", "query"), ("legacy_context", "normalized_query")],
)
def test_hash_valid_query_swap_or_casefold_is_rejected(captured: dict, kind: str, key: str) -> None:
    _edit_payload(captured, kind, lambda payload: payload.update({key: payload[key].upper()}))
    with pytest.raises(ValueError, match="captured query mismatch"):
        development.grade_development_captures(captured["args"])


def test_only_whitespace_query_normalization_is_allowed(captured: dict) -> None:
    for kind, key in (
        ("search", "query"),
        ("context_search", "query"),
        ("legacy_context", "normalized_query"),
    ):
        _edit_payload(
            captured,
            kind,
            lambda payload, key=key: payload.update(
                {key: "\n  " + payload[key].replace(" ", "\t") + "  \n"}
            ),
        )
    assert development.grade_development_captures(captured["args"])["aggregate"]["queries"] == 30


@pytest.mark.parametrize("change", ["bytes", "extra", "missing", "symlink"])
def test_actual_captured_corpus_must_match_all_40_union_files(captured: dict, change: str) -> None:
    path = captured["corpus"] / Path(captured["files"][0]["path"]).name
    if change == "bytes":
        path.write_text("altered unit bytes")
    elif change == "extra":
        (captured["corpus"] / "extra.txt").write_text("extra")
    else:
        path.unlink()
        if change == "symlink":
            path.symlink_to(captured["root"] / captured["files"][0]["path"])
    with pytest.raises(ValueError):
        development.grade_development_captures(captured["args"])
    assert not captured["args"].workspace.exists()


@pytest.mark.parametrize("change", ["limit", "hits"])
def test_context_pool_cannot_be_retagged_from_100_to_10(captured: dict, change: str) -> None:
    def edit(payload):
        if change == "limit":
            payload["plan"]["limit"] = 100
        else:
            payload["hits"] = [{} for _ in range(11)]

    _edit_payload(captured, "context_search", edit)
    with pytest.raises(ValueError, match=r"limit is not 10|at most 10"):
        development.grade_development_captures(captured["args"])


@pytest.mark.parametrize(
    "key,value",
    [
        ("unique_query_count", 29),
        ("model_query_count", 30),
        ("model_embedding_query_count", 59),
        ("ingestion_count", True),
        ("ingestion_count", 1),
    ],
)
def test_operation_counts_do_not_misstate_embedding_or_ingestion_work(
    captured: dict, key: str, value: object
) -> None:
    captured["capture_manifest"][key] = value
    _save_manifest(captured)
    with pytest.raises(ValueError, match="count"):
        development.grade_development_captures(captured["args"])


def test_runtime_tree_change_rejects_producer_claim(captured: dict) -> None:
    captured["capture_manifest"]["producer"]["runtime_tree_sha256_after"] = "c" * 64
    _save_manifest(captured)
    with pytest.raises(ValueError, match="changed runtime tree"):
        development.grade_development_captures(captured["args"])


def test_existing_measurement_workspace_is_never_overwritten(captured: dict) -> None:
    captured["args"].workspace.mkdir()
    target = captured["args"].workspace / "measurement.json"
    target.write_bytes(b"preserved")
    with pytest.raises(ValueError, match="new workspace"):
        development.grade_development_captures(captured["args"])
    assert target.read_bytes() == b"preserved"


@pytest.mark.parametrize("root", ["corpus", "root"])
def test_workspace_cannot_be_created_inside_input_corpus(captured: dict, root: str) -> None:
    captured["args"].workspace = captured[root] / "measurement"
    with pytest.raises(ValueError, match="workspace cannot mutate"):
        development.grade_development_captures(captured["args"])
    assert not captured["args"].workspace.exists()


def test_capture_errors_and_v2_query_mismatch_remain_visible(captured: dict) -> None:
    captured["args"].context_response_version = 2
    _edit_payload(captured, "search", lambda payload: payload.update(errors=["unit-search-error"]))
    _edit_payload(
        captured,
        "context_v2",
        lambda payload: payload.update(query="different unit query", errors=["unit-v2-error"]),
    )
    report = development.grade_development_captures(captured["args"])
    assert report["aggregate"]["queries"] == 30
    assert report["aggregate"]["execution_invalid_queries"] == 1
    errors = report["queries"][0]["execution_errors"]
    assert {item["capture"] for item in errors} == {"search", "context_v2"}
    assert "response_query_mismatch" in report["context_v2"]["queries"][0]["contract_errors"]


def test_v2_uses_context_search_not_the_larger_ranking_pool(
    captured: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured["args"].context_response_version = 2
    _edit_payload(
        captured,
        "search",
        lambda payload: payload.update(
            hits=[{"resource": {"current_path": "unit-unknown-source"}}]
        ),
    )
    original = captured["metrics"].score_context_v2
    seen = []

    def score(query, payload, entries, hits):
        seen.append(query["query_id"])
        assert hits == []
        return original(query, payload, entries, hits)

    monkeypatch.setattr(captured["metrics"], "score_context_v2", score)
    report = development.grade_development_captures(captured["args"])
    assert len(seen) == 30
    assert report["queries"][0]["raw_hits"] == 1
    assert report["queries"][0]["logical_ranking"] == ["unknown:unit-unknown-source"]


def test_mid_grading_capture_change_is_rejected_before_output(
    captured: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = development.benchmark.score_predictions
    changed = False

    def score(*args, **kwargs):
        nonlocal changed
        report = original(*args, **kwargs)
        if not changed:
            row = captured["capture_manifest"]["queries"][0]
            path = captured["capture_root"] / row["context_v2"]
            path.write_bytes(path.read_bytes() + b"\n")
            changed = True
        return report

    monkeypatch.setattr(development.benchmark, "score_predictions", score)
    with pytest.raises(ValueError, match="response changed during grading"):
        development.grade_development_captures(captured["args"])
    assert not captured["args"].workspace.exists()


def _partial_notice(payload: dict) -> None:
    payload.update(
        status="partial",
        exit_code=4,
        error={"code": "incomplete_context", "message": "See coverage reasons", "retryable": False},
        coverage={
            "retrieval": {"status": "partial", "reasons": ["unit-owner:unavailable"]},
            "evidence": {"status": "no_evidence", "reasons": []},
            "presentation": {"status": "complete", "reasons": []},
            "relations": {"status": "complete", "reasons": []},
            "witness_checks": {"status": "not_assessed", "reasons": []},
            "scopes": [],
        },
    )


def test_typed_partial_coverage_is_notice_with_raw_error_and_reasons(captured: dict) -> None:
    captured["args"].context_response_version = 2
    _edit_payload(captured, "context_v2", _partial_notice)
    report = development.grade_development_captures(captured["args"])
    assert report["aggregate"]["queries"] == 30
    assert report["aggregate"]["execution_invalid_queries"] == 0
    assert report["queries"][0]["execution_errors"] == []
    assert len(report["raw_capture_error_records"]) == len(report["coverage_notices"]) == 1
    raw = report["raw_capture_error_records"][0]
    assert raw["details"] == {
        "code": "incomplete_context",
        "message": "See coverage reasons",
        "retryable": False,
    }
    assert raw["coverage"]["retrieval"]["reasons"] == ["unit-owner:unavailable"]
    assert report["coverage_notices"][0]["raw_capture_error_record_index"] == 0
    assert len(report["groups"]["original_dev"]["coverage_notices"]) == 1
    assert report["context_v2"]["aggregate"]["positive_sufficiency_unknown_queries"] == 24


@pytest.mark.parametrize(
    "change",
    [
        "code",
        "retryable",
        "schema",
        "version",
        "status",
        "exit",
        "no_partial",
        "empty_partial",
        "complete_reason",
        "missing_section",
        "bad_reason",
    ],
)
def test_incoherent_or_other_v2_errors_remain_execution_errors(captured: dict, change: str) -> None:
    def edit(payload):
        _partial_notice(payload)
        if change == "code":
            payload["error"]["code"] = "runtime_failed"
        elif change == "retryable":
            payload["error"]["retryable"] = True
        elif change == "schema":
            payload["schema"] = "unknown-schema"
        elif change == "version":
            payload["response_version"] = 1
        elif change == "status":
            payload["status"] = "failed"
        elif change == "exit":
            payload["exit_code"] = 1
        elif change == "no_partial":
            payload["coverage"]["retrieval"] = {"status": "complete", "reasons": []}
        elif change == "empty_partial":
            payload["coverage"]["retrieval"]["reasons"] = []
        elif change == "complete_reason":
            payload["coverage"]["presentation"]["reasons"] = ["contradictory-complete"]
        elif change == "missing_section":
            del payload["coverage"]["evidence"]
        else:
            payload["coverage"]["retrieval"]["reasons"] = [None]

    _edit_payload(captured, "context_v2", edit)
    report = development.grade_development_captures(captured["args"])
    assert report["aggregate"]["queries"] == 30
    assert report["aggregate"]["execution_invalid_queries"] == 1
    assert len(report["raw_capture_error_records"]) == 1
    assert report["coverage_notices"] == []


def test_notice_does_not_hide_other_raw_errors_or_witness_missing_reasons(captured: dict) -> None:
    def edit(payload):
        _partial_notice(payload)
        payload["coverage"]["witness_checks"] = {
            "status": "missing",
            "reasons": ["unit-required-marker-missing"],
        }
        payload["errors"] = ["independent-packing-error"]

    _edit_payload(captured, "context_v2", edit)
    report = development.grade_development_captures(captured["args"])
    assert len(report["raw_capture_error_records"]) == 2
    assert len(report["coverage_notices"]) == 1
    assert report["aggregate"]["execution_invalid_queries"] == 1
    assert report["queries"][0]["execution_errors"][0]["details"] == ["independent-packing-error"]
    assert report["coverage_notices"][0]["coverage"]["witness_checks"]["reasons"] == [
        "unit-required-marker-missing"
    ]


def test_not_assessed_witness_counterevidence_reason_is_preserved_as_notice(captured: dict) -> None:
    def edit(payload):
        _partial_notice(payload)
        payload["coverage"]["witness_checks"]["reasons"] = ["unit-citation:scoped_counterevidence"]

    _edit_payload(captured, "context_v2", edit)
    report = development.grade_development_captures(captured["args"])
    assert report["aggregate"]["execution_invalid_queries"] == 0
    assert len(report["coverage_notices"]) == 1
    assert report["raw_capture_error_records"][0]["coverage"]["witness_checks"]["reasons"] == [
        "unit-citation:scoped_counterevidence"
    ]


@pytest.fixture
def projection(captured: dict, tmp_path: Path) -> dict:
    capture_root = tmp_path / "projection" / "responses"
    shutil.copytree(captured["capture_root"], capture_root)
    manifest = copy.deepcopy(captured["capture_manifest"])
    manifest.update(
        execution_mode="projection_only",
        model_query_count=0,
        model_embedding_query_count=0,
        retrieval_provenance={
            "capture_manifest_path": str(captured["manifest_path"]),
            "capture_manifest_sha256": development.benchmark.sha256(captured["manifest_path"]),
            "captured_responses_root": str(captured["capture_root"]),
        },
    )
    manifest["producer"].update(
        runtime_tree_sha256_before="c" * 64, runtime_tree_sha256_after="c" * 64
    )
    manifest_path = tmp_path / "projection" / "capture-manifest.json"
    _json(manifest_path, manifest)
    args = copy.copy(captured["args"])
    args.captured_responses, args.capture_manifest = capture_root, manifest_path
    args.workspace = tmp_path / "projection-measurement"
    args.context_response_version = 2
    return {
        **captured,
        "args": args,
        "capture_root": capture_root,
        "manifest_path": manifest_path,
        "capture_manifest": manifest,
        "original": captured,
    }


def _repin_origin(case: dict) -> None:
    original = case["original"]
    _save_manifest(original)
    case["capture_manifest"]["retrieval_provenance"]["capture_manifest_sha256"] = (
        development.benchmark.sha256(original["manifest_path"])
    )
    _save_manifest(case)


def test_projection_counts_zero_current_and_pins_every_original_retrieval(projection: dict) -> None:
    _edit_payload(
        projection,
        "legacy_context",
        lambda payload: payload.update(unit_projection="new-v1-packing"),
    )
    _edit_payload(
        projection, "context_v2", lambda payload: payload.update(unit_projection="new-v2-packing")
    )
    report = development.grade_development_captures(projection["args"])
    assert report["execution_mode"] == "projection_only"
    assert report["model_query_count"] == report["model_embedding_query_count"] == 0
    assert report["retrieval_operation_counts"] == {
        "current_producer": 0,
        "original_producer": 60,
        "driver": 0,
    }
    assert report["model_execution_by_this_driver"] is False
    origin = report["retrieval_provenance"]
    assert origin["model_query_count"] == 60 and origin["model_embedding_query_count"] == 90
    assert origin["retrieval_capture_sha256_pairs_verified"] == 60
    assert len(origin["captured_files"]) == 120
    assert report["aggregate"]["queries"] == 30
    assert report["groups"]["retired_r1"]["aggregate"]["queries"] == 10


@pytest.mark.parametrize("kind", ["search", "context_search"])
def test_projection_cannot_replace_hash_valid_retrieval_with_equal_size_pool(
    projection: dict, kind: str
) -> None:
    _edit_payload(
        projection,
        kind,
        lambda payload: payload.update(unit_changed="same counts but different capture bytes"),
    )
    with pytest.raises(ValueError, match="projection altered original retrieval"):
        development.grade_development_captures(projection["args"])
    assert not projection["args"].workspace.exists()


@pytest.mark.parametrize(
    "change",
    [
        "model_count",
        "embedding_count",
        "producer_kind",
        "missing_provenance",
        "relative_root",
        "missing_root",
        "bad_manifest_hash",
        "manifest_symlink",
        "root_symlink",
    ],
)
def test_projection_metadata_and_explicit_original_paths_are_strict(
    projection: dict, change: str, tmp_path: Path
) -> None:
    manifest = projection["capture_manifest"]
    provenance = manifest["retrieval_provenance"]
    if change == "model_count":
        manifest["model_query_count"] = 60
    elif change == "embedding_count":
        manifest["model_embedding_query_count"] = 90
    elif change == "producer_kind":
        manifest["producer"]["kind"] = "installed_artifact"
    elif change == "missing_provenance":
        del manifest["retrieval_provenance"]
    elif change == "relative_root":
        provenance["captured_responses_root"] = "responses"
    elif change == "missing_root":
        del provenance["captured_responses_root"]
    elif change == "bad_manifest_hash":
        provenance["capture_manifest_sha256"] = "0" * 64
    elif change == "manifest_symlink":
        alias = tmp_path / "manifest-alias.json"
        alias.symlink_to(projection["original"]["manifest_path"])
        provenance["capture_manifest_path"] = str(alias)
    else:
        alias = tmp_path / "capture-root-alias"
        alias.symlink_to(projection["original"]["capture_root"], target_is_directory=True)
        provenance["captured_responses_root"] = str(alias)
    _save_manifest(projection)
    with pytest.raises(ValueError):
        development.grade_development_captures(projection["args"])
    assert not projection["args"].workspace.exists()


@pytest.mark.parametrize(
    "change",
    [
        "projection",
        "nested_provenance",
        "bad_count",
        "missing_query",
        "missing_payload",
        "query_swap",
    ],
)
def test_projection_requires_complete_single_original_execution(
    projection: dict, change: str
) -> None:
    original = projection["original"]
    if change == "projection":
        original["capture_manifest"].update(
            execution_mode="projection_only", model_query_count=0, model_embedding_query_count=0
        )
    elif change == "nested_provenance":
        original["capture_manifest"]["retrieval_provenance"] = {"unit_chain": True}
    elif change == "bad_count":
        original["capture_manifest"]["model_query_count"] = 30
    elif change == "missing_query":
        original["capture_manifest"]["queries"].pop()
    elif change == "missing_payload":
        row = original["capture_manifest"]["queries"][0]
        (original["capture_root"] / row["context_v2"]).unlink()
    else:
        _edit_payload(
            original, "context_v2", lambda payload: payload.update(query="another unit query")
        )
    _repin_origin(projection)
    with pytest.raises(ValueError):
        development.grade_development_captures(projection["args"])
    assert not projection["args"].workspace.exists()
