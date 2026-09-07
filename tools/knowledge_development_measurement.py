"""Verify and grade recorded development diagnostics, never independent acceptance.

This driver executes no retrieval, embedding, ingestion, or production-state access.
Producer metadata is a claim carried with captures, not proof of model execution.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib
import json
from pathlib import Path
import re
from typing import Any

if __package__:
    from . import benchmark_knowledge_functional as benchmark
else:
    import benchmark_knowledge_functional as benchmark


FROZEN_V1_SHA256 = "02c43c7100db4493785b3bd69ae43358115e800050eac8ac1d0fff4817921ce9"
DEVELOPMENT_SPLIT = "development-expanded-r1"
DEVELOPMENT_SCOPE = "DEVELOPMENT_ONLY_NOT_INDEPENDENT_ACCEPTANCE"
GROUPS = (
    ("original_dev", "original-dev", "dev", 24, 23, 16, 4),
    ("retired_r1", "retired-r1", "reserve", 16, 16, 8, 2),
)
CAPTURE_KINDS = ("search", "context_search", "legacy_context", "context_v2")
COVERAGE_SECTION_STATES = {
    "retrieval": {"complete", "partial"},
    "evidence": {"complete", "partial", "no_evidence"},
    "presentation": {"complete", "partial"},
    "relations": {"complete", "partial", "not_assessed"},
    "witness_checks": {"complete", "partial", "missing", "not_assessed"},
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    value = json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"expected a nonsymlink directory: {path}")
    return path.resolve(strict=True)


def _relative_file(root: Path, name: object) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or "\0" in name
        or Path(name).is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise ValueError("capture or fixture path must be strictly relative and contained")
    path = root
    for part in name.split("/"):
        path /= part
        if path.is_symlink():
            raise ValueError("symlink in capture or fixture path")
    if not path.is_file() or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError(f"missing or uncontained file: {name}")
    return path


def _plain_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a nonsymlink file: {path}")
    return path.read_bytes()


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _corpus_entries(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    root = _directory(root)
    entries: dict[str, dict[str, Any]] = {}
    for entry in manifest["files"]:
        parts = entry["path"].split("/")
        if len(parts) != 2 or parts[0] != "corpus":
            raise ValueError("development corpus must contain direct, unrenamed fixture files")
        path = _relative_file(root, parts[1])
        data = path.read_bytes()
        if len(data) != entry["bytes"] or _digest(data) != entry["sha256"]:
            raise ValueError("development corpus bytes differ from the frozen union")
        if str(path) in entries:
            raise ValueError("duplicate corpus path")
        entries[str(path)] = entry
    if len(entries) != 40 or set(root.iterdir()) != {Path(path) for path in entries}:
        raise ValueError("development corpus must contain exactly the 40 pinned files")
    return entries


def load_development_union(
    root: Path, freeze_path: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, tuple[str, ...]]]:
    """Require the exact original DEV + retired R1 view, with unchanged judgments."""
    root = _directory(Path(root))
    frozen_bytes = _plain_file(Path(freeze_path))
    if _digest(frozen_bytes) != FROZEN_V1_SHA256:
        raise ValueError("development diagnostics require the unchanged original V1 freeze")
    freeze = _object(frozen_bytes)
    if freeze.get("schema") != "neocortex.functional-freeze/v1":
        raise ValueError("unknown original V1 freeze schema")
    manifest = _object(_relative_file(root, "manifest.json").read_bytes())
    judgments = _object(_relative_file(root, "queries.json").read_bytes())
    if any(value.get("split") != DEVELOPMENT_SPLIT for value in (manifest, judgments)):
        raise ValueError("retired R1 is development-only; an independent split is forbidden")

    source_files: list[dict[str, Any]] = []
    source_queries: list[dict[str, Any]] = []
    groups: dict[str, tuple[str, ...]] = {}
    for name, role, split, files, logical, positives, negatives in GROUPS:
        source: dict[str, dict[str, Any]] = {}
        for filename, pin in (
            ("manifest.json", "manifest_sha256"),
            ("queries.json", "queries_sha256"),
        ):
            data = _relative_file(root, f"provenance/{role}/{filename}").read_bytes()
            if _digest(data) != freeze[split][pin]:
                raise ValueError(f"frozen {role} provenance bytes changed: {filename}")
            source[filename] = _object(data)
        original_manifest, original_queries = source["manifest.json"], source["queries.json"]
        if (
            original_manifest.get("schema") != "neocortex.functional-fixtures/v1"
            or original_queries.get("schema") != "neocortex.functional-judgments/v1"
            or original_manifest.get("split") != split
            or original_queries.get("split") != split
            or len(original_manifest["files"]) != files
            or len({item["logical_resource_id"] for item in original_manifest["files"]}) != logical
            or len(original_queries["queries"]) != positives + negatives
            or Counter(item["kind"] for item in original_queries["queries"])
            != {"positive": positives, "negative": negatives}
        ):
            raise ValueError(f"invalid frozen development group: {name}")
        source_files.extend(original_manifest["files"])
        source_queries.extend(original_queries["queries"])
        groups[name] = tuple(item["query_id"] for item in original_queries["queries"])
    if manifest != {
        "schema": "neocortex.functional-fixtures/v1",
        "split": DEVELOPMENT_SPLIT,
        "files": source_files,
    } or judgments != {
        "schema": "neocortex.functional-judgments/v1",
        "split": DEVELOPMENT_SPLIT,
        "queries": source_queries,
    }:
        raise ValueError("development union differs from exact original files or judgments")
    if (
        len(source_files) != 40
        or len({item["fixture_id"] for item in source_files}) != 40
        or len({item["logical_resource_id"] for item in source_files}) != 39
        or len(source_queries) != 30
        or len({item["query_id"] for item in source_queries}) != 30
        or Counter(item["kind"] for item in source_queries) != {"positive": 24, "negative": 6}
    ):
        raise ValueError("development union counts or identities changed")
    _corpus_entries(root / "corpus", manifest)
    loaded_manifest, loaded_queries = benchmark.load_fixtures(root)
    if loaded_manifest != manifest or loaded_queries != judgments:
        raise ValueError("fixture inputs changed while loading the development union")
    return manifest, judgments, groups


def _metrics_module() -> Any:
    name = ".knowledge_functional_v2_metrics" if __package__ else "knowledge_functional_v2_metrics"
    return importlib.import_module(name, package=__package__ or None)


def _capture_metadata(
    value: dict[str, Any], query_ids: set[str], *, allow_projection: bool = True
) -> dict[str, dict[str, Any]]:
    if (
        value.get("schema") != "neocortex.development-captures/v1"
        or value.get("evaluation_scope") != "development_only"
        or value.get("dataset_split") != DEVELOPMENT_SPLIT
    ):
        raise ValueError("captures must declare development-only scope and union split")
    mode = value.get("execution_mode", "retrieval")
    if mode not in {"retrieval", "projection_only"}:
        raise ValueError("unknown capture execution_mode")
    projection = mode == "projection_only"
    if projection and not allow_projection:
        raise ValueError("projection provenance must reference one original retrieval, not a chain")
    if not projection and "retrieval_provenance" in value:
        raise ValueError("original retrieval cannot carry another retrieval provenance")
    for key, count in (
        ("unique_query_count", 30),
        ("model_query_count", 0 if projection else 60),
        ("ingestion_count", 0),
    ):
        if type(value.get(key)) is not int or value[key] != count:
            raise ValueError(f"invalid capture operation count: {key}")
    embeddings = value.get("model_embedding_query_count")
    if type(embeddings) is not int or (embeddings != 0 if projection else embeddings < 60):
        raise ValueError(
            "model_embedding_query_count must be zero for projection or at least 60 for retrieval"
        )
    producer = value.get("producer")
    if (
        not isinstance(producer, dict)
        or producer.get("kind") not in {"checkout", "installed_artifact"}
        or (projection and producer.get("kind") != "checkout")
        or not _hex(producer.get("source_sha"), 40)
        or not _hex(producer.get("runtime_tree_sha256_before"), 64)
        or producer.get("runtime_tree_sha256_after") != producer["runtime_tree_sha256_before"]
        or (
            "working_tree_modified" in producer
            and not isinstance(producer["working_tree_modified"], bool)
        )
    ):
        raise ValueError("invalid producer identity or changed runtime tree")
    rows = value.get("queries")
    if not isinstance(rows, list) or len(rows) != len(query_ids):
        raise ValueError("capture query pool is incomplete or contains extras")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("query_id"), str)
            or row["query_id"] not in query_ids
        ):
            raise ValueError("unexpected capture query identity")
        qid = row["query_id"]
        if qid in by_id:
            raise ValueError("duplicate capture query identity")
        by_id[qid] = row
    if set(by_id) != query_ids:
        raise ValueError("missing capture query identity")
    return by_id


def _check_query(payload: dict[str, Any], key: str, text: str, label: str) -> None:
    value = payload.get(key)
    if not isinstance(value, str) or " ".join(value.split()) != " ".join(text.split()):
        raise ValueError(f"captured query mismatch: {label}")


def _check_search(payload: dict[str, Any], limit: int) -> None:
    hits = payload.get("hits")
    if (
        not isinstance(hits, list)
        or len(hits) > limit
        or any(not isinstance(hit, dict) for hit in hits)
    ):
        raise ValueError(f"captured search must preserve a hit pool of at most {limit}")
    if "plan" in payload:
        plan = payload["plan"]
        if not isinstance(plan, dict):
            raise ValueError("invalid captured search plan")
        if "limit" in plan and (type(plan["limit"]) is not int or plan["limit"] != limit):
            raise ValueError(f"captured search plan limit is not {limit}; retagging is forbidden")


def _read_capture_pool(
    root: Path,
    rows: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    *,
    verify_v2_query: bool = False,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    captures: dict[str, dict[str, dict[str, Any]]] = {}
    pinned: list[dict[str, Any]] = []
    for query in queries:
        qid, row = query["query_id"], rows[query["query_id"]]
        payloads: dict[str, dict[str, Any]] = {}
        for kind in CAPTURE_KINDS:
            path = _relative_file(root, row.get(kind))
            data = path.read_bytes()
            expected_sha = row.get(f"{kind}_sha256")
            if not _hex(expected_sha, 64) or _digest(data) != expected_sha:
                raise ValueError(f"captured response hash mismatch: {qid}/{kind}")
            payloads[kind] = _object(data)
            pinned.append(
                {
                    "query_id": qid,
                    "kind": kind,
                    "path": str(path),
                    "bytes": len(data),
                    "sha256": expected_sha,
                }
            )
        for kind in ("search", "context_search"):
            _check_query(payloads[kind], "query", query["text"], f"{qid}/{kind}")
            _check_search(payloads[kind], 100 if kind == "search" else 10)
        _check_query(
            payloads["legacy_context"], "normalized_query", query["text"], f"{qid}/legacy_context"
        )
        if verify_v2_query:
            _check_query(payloads["context_v2"], "query", query["text"], f"{qid}/context_v2")
        if "query_sha256" in row and row["query_sha256"] != _digest(query["text"].encode()):
            raise ValueError(f"captured query text pin mismatch: {qid}")
        captures[qid] = payloads
    return captures, pinned


def _coherent_coverage_notice(payload: dict[str, Any]) -> bool:
    """Recognize only the typed partial-coverage notice, never general errors."""
    error, coverage = payload.get("error"), payload.get("coverage")
    if (
        payload.get("schema") != "neocortex.context-response/v2"
        or type(payload.get("response_version")) is not int
        or payload["response_version"] != 2
        or payload.get("status") != "partial"
        or not isinstance(error, dict)
        or error.get("code") != "incomplete_context"
        or error.get("retryable") is not False
        or not isinstance(coverage, dict)
        or set(coverage) != set(COVERAGE_SECTION_STATES) | {"scopes"}
        or not isinstance(coverage.get("scopes"), list)
        or any(not isinstance(scope, dict) for scope in coverage["scopes"])
    ):
        return False
    if "exit_code" in payload and (
        type(payload["exit_code"]) is not int or payload["exit_code"] != 4
    ):
        return False
    partial = False
    for name, states in COVERAGE_SECTION_STATES.items():
        section = coverage[name]
        if not isinstance(section, dict) or section.get("status") not in states:
            return False
        reasons = section.get("reasons")
        if not isinstance(reasons, list) or any(
            not isinstance(reason, str) or not reason.strip() for reason in reasons
        ):
            return False
        if section["status"] in {"partial", "missing"} and not reasons:
            return False
        if section["status"] in {"complete", "no_evidence"} and reasons:
            return False
        partial |= section["status"] == "partial"
    return partial


def _capture_error_records(
    rows: dict[str, dict[str, Any]], captures: dict[str, dict[str, dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    raw, notices, execution = [], [], {}
    for qid, payloads in captures.items():
        execution[qid] = []
        for kind, payload in (("manifest", rows[qid]), *payloads.items()):
            for field in ("error", "errors"):
                if not payload.get(field):
                    continue
                record = {
                    "query_id": qid,
                    "capture": kind,
                    "field": field,
                    "details": payload[field],
                }
                if "coverage" in payload:
                    record["coverage"] = payload["coverage"]
                raw.append(record)
                if kind == "context_v2" and field == "error" and _coherent_coverage_notice(payload):
                    notices.append(
                        {
                            **record,
                            "classification": "incomplete_context_coverage_notice",
                            "raw_capture_error_record_index": len(raw) - 1,
                        }
                    )
                else:
                    execution[qid].append(record)
    return raw, notices, execution


def _projection_origin(
    capture_manifest: dict[str, Any],
    current_rows: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    manifest: dict[str, Any],
    corpus_root: Path,
) -> dict[str, Any] | None:
    if capture_manifest.get("execution_mode") != "projection_only":
        return None
    provenance = capture_manifest.get("retrieval_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("projection_only requires explicit original retrieval provenance")
    for key in ("capture_manifest_path", "captured_responses_root"):
        value = provenance.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"original retrieval {key} must be an explicit absolute path")
    path = Path(provenance["capture_manifest_path"])
    original_bytes = _plain_file(path)
    if (
        not _hex(provenance.get("capture_manifest_sha256"), 64)
        or _digest(original_bytes) != provenance["capture_manifest_sha256"]
    ):
        raise ValueError("original retrieval capture manifest hash mismatch")
    original = _object(original_bytes)
    rows = _capture_metadata(
        original, {query["query_id"] for query in queries}, allow_projection=False
    )
    root = _directory(Path(provenance["captured_responses_root"]))
    original_corpus = original.get("corpus_root")
    if not isinstance(original_corpus, str) or not Path(original_corpus).is_absolute():
        raise ValueError("original retrieval corpus_root must be absolute")
    original_corpus_root = _directory(Path(original_corpus))
    _corpus_entries(original_corpus_root, manifest)
    if original_corpus_root != corpus_root:
        raise ValueError("projection must retain the original retrieval corpus paths")
    captures, pinned = _read_capture_pool(root, rows, queries, verify_v2_query=True)
    for qid, row in current_rows.items():
        for kind in ("search", "context_search"):
            if row[f"{kind}_sha256"] != rows[qid][f"{kind}_sha256"]:
                raise ValueError(f"projection altered original retrieval capture: {qid}/{kind}")
    raw_errors, notices, _ = _capture_error_records(rows, captures)
    return {
        "manifest": original,
        "manifest_bytes": original_bytes,
        "manifest_path": path,
        "capture_root": root,
        "corpus_root": original_corpus_root,
        "rows": rows,
        "pinned_captures": pinned,
        "raw_capture_error_records": raw_errors,
        "coverage_notices": notices,
    }


def grade_development_captures(args: argparse.Namespace) -> dict[str, Any]:
    """Grade a complete, pinned capture pool without running or certifying a model."""
    if args.label != "development":
        raise ValueError(
            "retired R1 captures cannot be retagged baseline or independent acceptance"
        )
    version = args.context_response_version
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("context_response_version must be 1 or 2")
    workspace = Path(args.workspace).absolute()
    if workspace.exists() or workspace.is_symlink():
        raise ValueError("development grading requires a new workspace")
    fixture_root = _directory(Path(args.fixtures))
    manifest, judgments, groups = load_development_union(fixture_root, Path(args.freeze))
    capture_root = _directory(Path(args.captured_responses))
    manifest_path = Path(args.capture_manifest)
    capture_bytes = _plain_file(manifest_path)
    capture_manifest = _object(capture_bytes)
    rows = _capture_metadata(capture_manifest, {item["query_id"] for item in judgments["queries"]})
    corpus_name = capture_manifest.get("corpus_root")
    if not isinstance(corpus_name, str) or not Path(corpus_name).is_absolute():
        raise ValueError("captured corpus_root must be an absolute path")
    corpus_root = _directory(Path(corpus_name))
    if any(workspace.resolve().is_relative_to(root) for root in (fixture_root, corpus_root)):
        raise ValueError("measurement workspace cannot mutate a fixture or captured corpus")
    entries_by_path = _corpus_entries(corpus_root, manifest)
    metrics = _metrics_module()
    operationalization_sha = metrics.verify_operationalization(
        Path(args.operationalization), frozen_dataset_sha256=FROZEN_V1_SHA256
    )
    helpers = {
        str(Path(module.__file__).resolve()): benchmark.sha256(Path(module.__file__))
        for module in (benchmark, metrics)
    }
    captures, pinned_captures = _read_capture_pool(capture_root, rows, judgments["queries"])
    origin = _projection_origin(capture_manifest, rows, judgments["queries"], manifest, corpus_root)
    raw_errors, coverage_notices, execution_errors = _capture_error_records(rows, captures)
    predictions = {
        qid: {
            "search": payloads["search"],
            "context": payloads["legacy_context"],
            "errors": execution_errors[qid],
        }
        for qid, payloads in captures.items()
    }

    scored = benchmark.score_predictions(judgments["queries"], predictions, entries_by_path)
    report: dict[str, Any] = {
        "schema": "neocortex.development-capture-measurement/v1",
        "label": "development",
        "evaluation_scope": "development_only",
        "scope": DEVELOPMENT_SCOPE,
        "dataset_split": DEVELOPMENT_SPLIT,
        "execution_mode": capture_manifest.get("execution_mode", "retrieval"),
        "model_execution_by_this_driver": False,
        "model_execution_independently_verified": False,
        "producer": capture_manifest["producer"],
        "unique_query_count": 30,
        "model_query_count": capture_manifest["model_query_count"],
        "model_embedding_query_count": capture_manifest["model_embedding_query_count"],
        "model_query_semantics": (
            "Current producer reports zero retrieval and embedding operations; the 30 search(limit=100) and 30 context search(limit=10) captures are byte-identical to the single linked original retrieval. Original operation counts are separate, not new execution."
            if origin is not None
            else "Producer-reported 30 search(limit=100) + 30 context search(limit=10) retrieval operations, not 60 embedding computations; variants and model execution are not independently attested by JSON captures."
        ),
        "retrieval_operation_counts": {
            "current_producer": capture_manifest["model_query_count"],
            "original_producer": origin["manifest"]["model_query_count"] if origin else None,
            "driver": 0,
        },
        "capture_metric_caveat": "All metrics, including real_vector_queries, are computed from captured payloads and do not prove real model execution.",
        "ingestion_count": 0,
        "context_response_version": version,
        "operationalization_sha256": operationalization_sha,
        "grading_helpers_sha256": helpers,
        "raw_capture_error_records": raw_errors,
        "coverage_notices": coverage_notices,
        "provenance": {
            "fixture_root": str(fixture_root),
            "freeze_path": str(Path(args.freeze).resolve()),
            "freeze_sha256": FROZEN_V1_SHA256,
            "manifest_sha256": benchmark.sha256(fixture_root / "manifest.json"),
            "queries_sha256": benchmark.sha256(fixture_root / "queries.json"),
            "originals": {
                role: {
                    filename: benchmark.sha256(fixture_root / "provenance" / role / filename)
                    for filename in ("manifest.json", "queries.json")
                }
                for _, role, *_ in GROUPS
            },
            "capture_manifest": str(manifest_path.resolve()),
            "capture_manifest_sha256": _digest(capture_bytes),
            "corpus_root": str(corpus_root),
            "corpus_files_verified": len(entries_by_path),
            "captured_files": pinned_captures,
        },
        **scored,
        "groups": {},
    }
    if origin is not None:
        report["retrieval_provenance"] = {
            "capture_manifest_path": str(origin["manifest_path"].resolve()),
            "capture_manifest_sha256": _digest(origin["manifest_bytes"]),
            "captured_responses_root": str(origin["capture_root"]),
            "producer": origin["manifest"]["producer"],
            "unique_query_count": origin["manifest"]["unique_query_count"],
            "model_query_count": origin["manifest"]["model_query_count"],
            "model_embedding_query_count": origin["manifest"]["model_embedding_query_count"],
            "ingestion_count": origin["manifest"]["ingestion_count"],
            "model_execution_independently_verified": False,
            "retrieval_capture_sha256_pairs_verified": 60,
            "captured_files": origin["pinned_captures"],
            "raw_capture_error_records": origin["raw_capture_error_records"],
            "coverage_notices": origin["coverage_notices"],
        }
    v2_rows = []
    if version == 2:
        v2_rows = [
            metrics.score_context_v2(
                query,
                captures[query["query_id"]]["context_v2"],
                entries_by_path,
                captures[query["query_id"]]["context_search"]["hits"],
            )
            for query in judgments["queries"]
        ]
        report["context_v2"] = {
            "aggregate": metrics.aggregate_context_v2(v2_rows),
            "queries": v2_rows,
        }
    for name, ids in groups.items():
        group_queries = [query for query in judgments["queries"] if query["query_id"] in ids]
        group = {
            "scope": DEVELOPMENT_SCOPE,
            "query_ids": list(ids),
            "coverage_notices": [
                notice for notice in coverage_notices if notice["query_id"] in ids
            ],
            **benchmark.score_predictions(group_queries, predictions, entries_by_path),
        }
        if name == "retired_r1":
            group["status"] = "RETIRED_HOLDOUT_R1_NOW_DEVELOPMENT"
        if version == 2:
            subset = [row for row in v2_rows if row["query_id"] in ids]
            group["context_v2"] = {
                "aggregate": metrics.aggregate_context_v2(subset),
                "queries": subset,
            }
        report["groups"][name] = group

    # Nothing is written until grading and a second input-integrity pass complete.
    if load_development_union(fixture_root, Path(args.freeze)) != (manifest, judgments, groups):
        raise ValueError("development fixtures changed during grading")
    _corpus_entries(corpus_root, manifest)
    if _plain_file(manifest_path) != capture_bytes:
        raise ValueError("capture manifest changed during grading")
    for item in pinned_captures:
        path = _relative_file(capture_root, rows[item["query_id"]][item["kind"]])
        if str(path) != item["path"] or _digest(path.read_bytes()) != item["sha256"]:
            raise ValueError("captured response changed during grading")
    if origin is not None:
        if _plain_file(origin["manifest_path"]) != origin["manifest_bytes"]:
            raise ValueError("original retrieval manifest changed during grading")
        _corpus_entries(origin["corpus_root"], manifest)
        for item in origin["pinned_captures"]:
            path = _relative_file(
                origin["capture_root"], origin["rows"][item["query_id"]][item["kind"]]
            )
            if str(path) != item["path"] or _digest(path.read_bytes()) != item["sha256"]:
                raise ValueError("original retrieval response changed during grading")
    if any(benchmark.sha256(Path(path)) != digest for path, digest in helpers.items()):
        raise ValueError("grading helper changed during measurement")
    if (
        metrics.verify_operationalization(
            Path(args.operationalization), frozen_dataset_sha256=FROZEN_V1_SHA256
        )
        != operationalization_sha
    ):
        raise ValueError("operationalization changed during grading")
    workspace.mkdir(parents=True, exist_ok=False)
    with (workspace / "measurement.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report
