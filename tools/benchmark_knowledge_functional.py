"""Opt-in functional retrieval measurement over frozen synthetic documents.

This is development tooling, not a runtime validator or product capability.
It executes an explicitly selected installed launcher and its real offline
models. It never substitutes retrieval/model outputs, discovers the personal
corpus, changes model configuration, or grants permission to mutate files.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import time
from typing import Any


SCHEMA = "neocortex.knowledge-functional-measurement/v1"
MAX_TIMEOUT_SECONDS = 900
MAX_FILES = 50
MAX_BYTES = 20 * 1024 * 1024
MEMORY_LIMIT_BYTES = 12 * 1024 * 1024 * 1024
FROZEN_DATASET_SHA256 = "02c43c7100db4493785b3bd69ae43358115e800050eac8ac1d0fff4817921ce9"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_fixtures(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify exact fixture bytes and pinned author judgments before any run."""
    manifest = read_json(root / "manifest.json")
    judgments = read_json(root / "queries.json")
    if manifest.get("schema") != "neocortex.functional-fixtures/v1":
        raise ValueError("unknown fixture schema")
    if judgments.get("schema") != "neocortex.functional-judgments/v1":
        raise ValueError("unknown judgment schema")
    entries = manifest["files"]
    if not 1 <= len(entries) <= MAX_FILES:
        raise ValueError("fixture count outside the contained measurement limit")
    if sum(item["bytes"] for item in entries) > MAX_BYTES:
        raise ValueError("fixture byte bound exceeded")
    names: set[str] = set()
    pins: dict[str, str] = {}
    for entry in entries:
        path = root / entry["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("fixture escaped its root")
        if not entry.get("synthetic"):
            raise ValueError("only explicitly synthetic fixtures are allowed")
        if path.name in names:
            raise ValueError("ambiguous fixture basename")
        names.add(path.name)
        if path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            raise ValueError("frozen fixture bytes changed")
        pin = hashlib.sha256(entry["source_text"].encode()).hexdigest()
        if pin != entry["revision_pin"]:
            raise ValueError("fixture revision pin changed")
        logical = entry["logical_resource_id"]
        if logical in pins and pins[logical] != pin:
            raise ValueError("ambiguous logical document revisions")
        pins[logical] = pin
    query_ids: set[str] = set()
    for query in judgments["queries"]:
        if query["query_id"] in query_ids:
            raise ValueError("duplicate query id")
        query_ids.add(query["query_id"])
        if query["kind"] not in {"positive", "negative"}:
            raise ValueError("unknown query kind")
        if bool(query["relevance"]) != (query["kind"] == "positive"):
            raise ValueError("query kind conflicts with relevance judgments")
        for logical, grade in query["relevance"].items():
            if logical not in pins or isinstance(grade, bool) or grade not in {1, 2, 3}:
                raise ValueError("invalid pinned relevance judgment")
    return manifest, judgments


def verify_freeze(root: Path, freeze_path: Path) -> None:
    if sha256(freeze_path) != FROZEN_DATASET_SHA256:
        raise ValueError("frozen evaluation commitment changed")
    freeze = read_json(freeze_path)
    split = read_json(root / "manifest.json").get("split")
    if split not in {"dev", "reserve"}:
        raise ValueError("unknown frozen evaluation partition")
    for name, key in (("manifest.json", "manifest_sha256"), ("queries.json", "queries_sha256")):
        if sha256(root / name) != freeze[split][key]:
            raise ValueError("frozen manifest or judgments changed")


def logical_ranking(
    hits: list[dict[str, Any]], entries_by_path: dict[str, dict[str, Any]]
) -> list[str]:
    """Collapse aliases, owner projections, and chunks BEFORE truncating at K."""
    result: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        path = hit.get("resource", {}).get("current_path")
        entry = entries_by_path.get(path)
        logical = entry["logical_resource_id"] if entry else f"unknown:{path}"
        if logical not in seen:
            seen.add(logical)
            result.append(logical)
    return result


def ranking_metrics(ranking: list[str], relevance: dict[str, int]) -> dict[str, float]:
    """Success and recall deliberately have different numerators/denominators."""
    if not relevance:
        return {}
    top_five = set(ranking[:5])
    relevant = set(relevance)
    intersection = relevant & top_five
    dcg = sum(
        (2 ** relevance.get(logical, 0) - 1) / math.log2(position + 2)
        for position, logical in enumerate(ranking[:10])
    )
    ideal = sum(
        (2**grade - 1) / math.log2(position + 2)
        for position, grade in enumerate(sorted(relevance.values(), reverse=True)[:10])
    )
    return {
        "success_at_5": float(bool(intersection)),
        "recall_at_5": len(intersection) / len(relevant),
        "ndcg_at_10": dcg / ideal,
    }


def normalized(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def locator_errors(hit: dict[str, Any], entries_by_path: dict[str, dict[str, Any]]) -> list[str]:
    """Check source bytes, revision chain, source-backed snippet and locator shape.

    Character coordinates are extraction-relative, not offsets into compressed
    PDF/Office bytes. We check their direction and plausible source-text bound,
    and independently match the snippet to the authored source. This does not
    misrepresent structural checks as exact renderer coordinate validation.
    """
    resource_ref = hit.get("resource", {})
    revision = hit.get("revision", {})
    evidence = hit.get("evidence", {})
    path_value = resource_ref.get("current_path")
    entry = entries_by_path.get(path_value)
    errors = []
    if entry is None:
        return ["unknown_source_path"]
    path = Path(path_value)
    if path.is_symlink() or not path.is_file() or sha256(path) != entry["sha256"]:
        errors.append("source_bytes_changed")
    resource_id = resource_ref.get("resource_id")
    revision_id = revision.get("revision_id")
    if not resource_id or revision.get("resource_id") != resource_id:
        errors.append("revision_resource_mismatch")
    if not revision_id or evidence.get("revision_id") != revision_id:
        errors.append("evidence_revision_mismatch")
    if evidence.get("resource_id") != resource_id:
        errors.append("evidence_resource_mismatch")
    if revision.get("state") != "current" or not revision.get("processing_signature"):
        errors.append("revision_not_pinned_current")
    if not evidence.get("evidence_id") or not evidence.get("section_id"):
        errors.append("missing_locator_identity")
    source = normalized(entry["source_text"])
    snippet = normalized(evidence.get("snippet", "")).strip(" …")
    # FTS snippets mark matched tokens with brackets and ellipsize surrounding
    # text. Neither annotation is part of the document, unlike quoted words.
    unhighlighted = re.sub(r"\[([^\[\]\n]+)\]", r"\1", snippet)
    fragments = [part.strip() for part in re.split(r"\.{3}|…", unhighlighted) if part.strip()]
    raw = normalized(path.read_bytes().decode("utf-8", errors="ignore"))
    if not fragments or not any(
        all(part in candidate for part in fragments) for candidate in (source, raw)
    ):
        errors.append("snippet_not_in_frozen_source")
    start, end = evidence.get("start_char"), evidence.get("end_char")
    if start is not None or end is not None:
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
            errors.append("invalid_character_locator")
        elif end > max(len(entry["source_text"]), len(path.read_bytes())):
            errors.append("character_locator_outside_source_bound")
    page = evidence.get("page_index")
    if page is not None and page != 0:
        errors.append("page_outside_single_page_fixture")
    return errors


def citation_errors(bundle: dict[str, Any]) -> list[str]:
    selected = bundle.get("selected_hits", [])
    citations = bundle.get("citation_ids", [])
    evidence_ids = {hit.get("evidence", {}).get("evidence_id") for hit in selected}
    cited = [item.get("evidence_id") for item in citations]
    labels = [item.get("citation_id") for item in citations]
    errors = []
    if len(set(labels)) != len(labels) or any(not label for label in labels):
        errors.append("duplicate_or_empty_citation_id")
    if len(set(cited)) != len(cited) or set(cited) != evidence_ids:
        errors.append("citation_does_not_resolve_exactly_once")
    rendered = bundle.get("rendered_context", "")
    if any(label not in rendered for label in labels if label):
        errors.append("citation_missing_from_rendered_context")
    return errors


def score_predictions(
    queries: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
    entries_by_path: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Score each query exactly once without silently excluding failed requests."""
    rows = []
    for query in queries:
        prediction = predictions.get(query["query_id"], {})
        search = prediction.get("search", {})
        context = prediction.get("context", {})
        if context.get("schema") in {
            "neocortex.context-response/v2",
            "neocortex.evidence-response/v2",
        }:
            raise ValueError(
                "context v2 requires explicit supplementation, not empty legacy scoring"
            )
        hits = search.get("hits", [])
        ranking = logical_ranking(hits, entries_by_path)
        context_ranking = logical_ranking(context.get("selected_hits", []), entries_by_path)
        metrics = ranking_metrics(ranking, query["relevance"])
        locator_checks = [
            locator_errors(hit, entries_by_path) for hit in hits + context.get("selected_hits", [])
        ]
        citations = citation_errors(context)
        execution_errors = list(prediction.get("errors", []))
        if not search or not context:
            execution_errors.append("missing_search_or_context_payload")
        row = {
            "query_id": query["query_id"],
            "kind": query["kind"],
            **metrics,
            "logical_ranking": ranking,
            "raw_hits": len(hits),
            "unique_logical_hits": len(ranking),
            "context_relevant_resources": len(set(context_ranking) & set(query["relevance"])),
            "locator_checks": len(locator_checks),
            "locator_failures": sum(bool(error) for error in locator_checks),
            "locator_errors": sorted({error for errors in locator_checks for error in errors}),
            "citation_checks": len(context.get("citation_ids", [])),
            "citation_errors": citations,
            "unsupported_evidence": (
                len(context.get("selected_hits", [])) if query["kind"] == "negative" else 0
            ),
            "execution_errors": execution_errors,
            "coverage_partial": not search.get("complete", False),
            "coverage_warnings": search.get("warnings", []),
            "vectors_scanned": search.get("vectors_scanned", 0),
        }
        rows.append(row)
    positives = [row for row in rows if row["kind"] == "positive"]
    negatives = [row for row in rows if row["kind"] == "negative"]
    locator_count = sum(row["locator_checks"] for row in rows)
    locator_failures = sum(row["locator_failures"] for row in rows)
    aggregate = {
        "queries": len(rows),
        "positive_queries": len(positives),
        "negative_queries": len(negatives),
        "positive_successes_at_5": sum(int(row["success_at_5"]) for row in positives),
        "positive_context_supported_queries": sum(
            bool(row["context_relevant_resources"]) for row in positives
        ),
        **{
            metric: sum(row[metric] for row in positives) / len(positives) if positives else 0.0
            for metric in ("success_at_5", "recall_at_5", "ndcg_at_10")
        },
        "negative_unsupported_evidence": sum(row["unsupported_evidence"] for row in negatives),
        "legacy_negative_context_selections": sum(row["unsupported_evidence"] for row in negatives),
        "negative_queries_with_unsupported_evidence": sum(
            bool(row["unsupported_evidence"]) for row in negatives
        ),
        "locator_checks": locator_count,
        "locator_failures": locator_failures,
        "locator_integrity": 1.0 - locator_failures / locator_count if locator_count else None,
        "citation_checks": sum(row["citation_checks"] for row in rows),
        "citation_invalid_queries": sum(bool(row["citation_errors"]) for row in rows),
        "execution_invalid_queries": sum(bool(row["execution_errors"]) for row in rows),
        "coverage_partial_queries": sum(row["coverage_partial"] for row in rows),
        "coverage_warning_codes": sorted(
            {warning for row in rows for warning in row["coverage_warnings"]}
        ),
        "real_vector_queries": sum(row["vectors_scanned"] > 0 for row in rows),
    }
    return {"aggregate": aggregate, "queries": rows}


def acceptance(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, bool]:
    """Do not substitute Success@5 for true Recall@5 or reclassify errors away."""
    return {
        "success_at_5": candidate["success_at_5"] >= 0.90,
        "all_eight_reserved_positives": (
            candidate["positive_queries"] == 8 and candidate["positive_successes_at_5"] == 8
        ),
        "ndcg_not_below_baseline": candidate["ndcg_at_10"] + 1e-12 >= baseline["ndcg_at_10"],
        "negative_unsupported_evidence_zero": candidate["negative_unsupported_evidence"] == 0,
        "locator_integrity": candidate["locator_integrity"] == 1.0,
        "citation_integrity": candidate["citation_invalid_queries"] == 0
        and candidate["citation_checks"] > 0,
        "execution_complete": candidate["execution_invalid_queries"] == 0,
        "real_model_used": candidate["real_vector_queries"] == candidate["queries"],
    }


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (840, 840))
    resource.setrlimit(resource.RLIMIT_NOFILE, (2048, 2048))


class InstalledMeasurement:
    """Contained subprocess orchestration; no imported repository runtime."""

    def __init__(self, launcher: Path, workspace: Path, timeout: int) -> None:
        if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout must be between 1 and 900 seconds")
        self.launcher = launcher.resolve(strict=True)
        self.workspace = workspace.resolve()
        self.deadline = time.monotonic() + timeout
        self.started = time.monotonic()
        self.corpus = self.workspace / "corpus"
        self.state = self.workspace / "state"
        self.logs = self.workspace / "logs"
        self.env = dict(os.environ)
        for name in ("PYTHONPATH", "LOCALAPPDATA"):
            self.env.pop(name, None)
        self.env.update(
            {
                "HOME": str(self.workspace / "home"),
                "XDG_STATE_HOME": str(self.workspace / "state-home"),
                "XDG_CONFIG_HOME": str(self.workspace / "config"),
                "XDG_CACHE_HOME": str(self.workspace / "cache"),
                "XDG_DATA_HOME": str(self.workspace / "data"),
                "NEOCORTEX_CORPUS_ROOT": str(self.corpus),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "TOKENIZERS_PARALLELISM": "false",
            }
        )

    def call(
        self, name: str, arguments: list[str], *, json_output: bool = False
    ) -> tuple[int, dict[str, Any]]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("functional measurement exceeded its hard wall-clock budget")
        stdout, stderr = self.logs / f"{name}.json", self.logs / f"{name}.stderr"
        command = [
            str(self.launcher),
            "--root",
            str(self.corpus),
            "--state-directory",
            str(self.state),
            *arguments,
        ]
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(
                command,
                cwd=self.workspace,
                env=self.env,
                stdout=out,
                stderr=err,
                start_new_session=True,
                preexec_fn=_limits,
            )
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise TimeoutError(
                    "functional subprocess exceeded the shared hard timeout"
                ) from None
            except BaseException:
                # The private process group belongs exclusively to this run.
                # Do not orphan model/extractor children on an interrupt.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        payload = read_json(stdout) if json_output and stdout.stat().st_size else {}
        return process.returncode, payload


def run(args: argparse.Namespace) -> dict[str, Any]:
    fixture_root = args.fixtures.resolve(strict=True)
    verify_freeze(fixture_root, args.freeze)
    manifest, judgments = load_fixtures(fixture_root)
    sources = [(fixture_root, manifest)]
    if args.additional_fixtures:
        other = args.additional_fixtures.resolve(strict=True)
        verify_freeze(other, args.freeze)
        additional, _ = load_fixtures(other)
        sources.append((other, additional))
    if sum(len(item[1]["files"]) for item in sources) > MAX_FILES:
        raise ValueError("combined fixture count exceeds the contained bound")
    release_manifest = args.launcher.resolve(strict=True).parent.parent / "neocortex-release.json"
    release = read_json(release_manifest)
    if release.get("source_sha") != args.expected_sha:
        raise ValueError("installed release does not match the explicitly frozen SHA")
    if args.label == "candidate" and args.candidate_frozen_sha != args.expected_sha:
        raise ValueError("candidate measurement requires explicit predeclared SHA freeze")
    context_version = getattr(args, "context_response_version", 1)
    if context_version == 2 and args.label != "candidate":
        raise ValueError("the v2 supplement cannot overwrite the frozen legacy baseline")
    operationalization_sha = None
    if context_version == 2:
        if __package__:
            from .knowledge_functional_v2_metrics import verify_operationalization
        else:
            from knowledge_functional_v2_metrics import verify_operationalization
        operationalization_sha = verify_operationalization(
            args.operationalization, frozen_dataset_sha256=FROZEN_DATASET_SHA256
        )
    workspace = args.workspace.resolve()
    if workspace.exists() and not args.reuse_isolated_index:
        raise ValueError(
            "refusing an existing measurement workspace without explicit replay selection"
        )
    if workspace == fixture_root or fixture_root.is_relative_to(workspace):
        raise ValueError("measurement workspace must not contain the frozen fixture root")
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    measurement = InstalledMeasurement(args.launcher, workspace, args.timeout_seconds)
    for path in (
        measurement.corpus,
        measurement.state,
        measurement.logs,
        workspace / "home",
        workspace / "config",
        workspace / "cache",
    ):
        path.mkdir(mode=0o700, exist_ok=True)
    # Default query loading sees only the existing offline model cache through
    # an explicitly contained alias, never the user's state or corpus roots.
    cache_alias = workspace / "data" / "Neocortex" / "models" / "fastembed"
    cache_alias.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not cache_alias.exists():
        cache_alias.symlink_to(args.model_cache.resolve(strict=True), target_is_directory=True)
    if cache_alias.resolve() != args.model_cache.resolve(strict=True):
        raise ValueError("model-cache alias differs from explicitly selected local cache")
    sentinel = workspace / "measurement-isolation.json"
    isolation = {
        "schema": SCHEMA,
        "source_sha": args.expected_sha,
        "state": str(measurement.state),
        "corpus": str(measurement.corpus),
        "fixtures_sha256": sha256(fixture_root / "manifest.json"),
    }
    if args.reuse_isolated_index:
        if not sentinel.exists() or read_json(sentinel) != isolation:
            raise ValueError("existing index lacks the exact fixture/SHA isolation sentinel")
    else:
        write_json(sentinel, isolation)
    entries_by_path = {}
    for source_root, source_manifest in sources:
        for entry in source_manifest["files"]:
            source = source_root / entry["path"]
            destination = measurement.corpus / source.name
            if str(destination) in entries_by_path:
                raise ValueError("fixture basename collision across partitions")
            if not args.reuse_isolated_index:
                if destination.exists():
                    raise ValueError("refusing to overwrite a fixture")
                shutil.copyfile(source, destination)
            if sha256(destination) != entry["sha256"]:
                raise ValueError("isolated fixture differs from the frozen source")
            entries_by_path[str(destination)] = entry
    if set(measurement.corpus.iterdir()) != {Path(path) for path in entries_by_path}:
        raise ValueError("unexpected files in isolated corpus")
    if not args.reuse_isolated_index:
        rc, _ = measurement.call(
            "01-ingest",
            [
                "--route",
                "pdf,docx,office,text",
                "--strict-exit-codes",
                "--pdf-workers",
                "1",
                "--global-cpu-slots",
                "2",
            ],
        )
        if rc != 0:
            raise RuntimeError(f"real ingestion failed with exit {rc}; see contained log")
        semantic_args = [
            "--semantic-index",
            "text",
            "--semantic-model-cache",
            str(args.model_cache),
            "--semantic-threads",
            "2",
            "--semantic-max-items",
            "200",
            "--semantic-max-new-jobs",
            "200",
            "--semantic-time-budget-seconds",
            "550",
        ]
        for kind in ("pdf", "docx", "odt", "text"):
            semantic_args.extend(("--semantic-source", kind))
        rc, _ = measurement.call("02-semantic", semantic_args)
        if rc != 0:
            raise RuntimeError(f"real model indexing failed with exit {rc}; see contained log")
        model_log = (measurement.logs / "02-semantic.json").read_text()
        if "complete=1" not in model_log or "failed=0" not in model_log:
            raise RuntimeError("real model publication is not complete")
        item_count = re.search(r"SEMANTIC_INDEX scope=text [^\n]*?\bitems=(\d+)\b", model_log)
        if item_count is None or int(item_count.group(1)) != len(entries_by_path):
            raise RuntimeError("real text indexing did not cover every physical fixture")
        rc, _ = measurement.call("03-semantic-replay", semantic_args)
        replay_log = (measurement.logs / "03-semantic-replay.json").read_text()
        if rc != 0 or not all(
            token in replay_log for token in ("mode=exact_replay", "new_jobs=0", "complete=1")
        ):
            raise RuntimeError("unchanged isolated fixtures did not replay without new model jobs")
    predictions = {}
    for query in judgments["queries"]:
        qid = query["query_id"]
        search_rc, search = measurement.call(
            f"{qid}-search",
            [
                "--knowledge-search",
                query["text"],
                "--knowledge-mode",
                "evidence",
                "--knowledge-limit",
                "100",
                "--knowledge-json",
            ],
            json_output=True,
        )
        context_arguments = [
            "--knowledge-context",
            query["text"],
            "--knowledge-mode",
            "evidence",
            "--knowledge-limit",
            "10",
            "--knowledge-json",
        ]
        # The immutable original baseline predates this flag. New candidates
        # explicitly retain their v1 result in addition to optional v2 output.
        if args.label == "candidate":
            context_arguments.extend(("--knowledge-response-version", "1"))
        context_rc, context = measurement.call(
            f"{qid}-context",
            context_arguments,
            json_output=True,
        )
        errors = []
        if search_rc not in {0, 3, 4}:
            errors.append(f"search_exit_{search_rc}")
        if context_rc not in {0, 3, 4}:
            errors.append(f"context_exit_{context_rc}")
        predictions[qid] = {"search": search, "context": context, "errors": errors}
        if context_version == 2:
            v2_arguments = [*context_arguments[:-2], "--knowledge-response-version", "2"]
            v2_rc, v2 = measurement.call(f"{qid}-context-v2", v2_arguments, json_output=True)
            predictions[qid]["context_v2"] = v2
            predictions[qid]["context_v2_exit"] = v2_rc
    for path, entry in entries_by_path.items():
        if sha256(Path(path)) != entry["sha256"]:
            raise RuntimeError("fixture bytes mutated during measurement")
    scored = score_predictions(judgments["queries"], predictions, entries_by_path)
    report = {
        "schema": SCHEMA,
        "label": args.label,
        "split": judgments["split"],
        "source_sha": args.expected_sha,
        "release_manifest_sha256": sha256(release_manifest),
        "fixture_manifest_sha256": sha256(fixture_root / "manifest.json"),
        "judgments_sha256": sha256(fixture_root / "queries.json"),
        "fixture_files": len(entries_by_path),
        "logical_resources": len(
            {entry["logical_resource_id"] for entry in entries_by_path.values()}
        ),
        "elapsed_seconds": time.monotonic() - measurement.started,
        "timeout_seconds": args.timeout_seconds,
        "memory_limit_bytes": MEMORY_LIMIT_BYTES,
        "model_backend": "installed real offline models; no mocks or replacement embeddings",
        "measurement_unit": "logical resource with pinned content revision; aliases and chunks deduplicated before top-K",
        "locator_scope": "frozen bytes, revision chain, source-backed snippet, section identity and structural bounds",
        "isolation": isolation,
        "fixtures_unchanged": True,
        **scored,
    }
    if context_version == 2:
        if __package__:
            from .knowledge_functional_v2_metrics import aggregate_context_v2, score_context_v2
        else:
            from knowledge_functional_v2_metrics import aggregate_context_v2, score_context_v2
        rows_v2 = []
        for query in judgments["queries"]:
            prediction = predictions[query["query_id"]]
            row = score_context_v2(
                query,
                prediction["context_v2"],
                entries_by_path,
                prediction["search"].get("hits", []),
            )
            if prediction["context_v2_exit"] not in {0, 3, 4}:
                row["contract_errors"].append(f"context_v2_exit_{prediction['context_v2_exit']}")
            rows_v2.append(row)
        report["context_v2"] = {
            "operationalization_sha256": operationalization_sha,
            "aggregate": aggregate_context_v2(rows_v2),
            "queries": rows_v2,
            "legacy_v1_negative_context_selections": scored["aggregate"][
                "negative_unsupported_evidence"
            ],
            "note": "v2 typed sufficient-evidence metric is additional; legacy counts and immutable baseline are not reclassified",
        }
    write_json(workspace / "measurement.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument(
        "--freeze",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "knowledge_functional_v1"
        / "freeze.json",
    )
    parser.add_argument("--additional-fixtures", type=Path)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--candidate-frozen-sha")
    parser.add_argument("--context-response-version", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--operationalization",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "knowledge_functional_v1"
        / "operationalization-v2.1.json",
    )
    parser.add_argument("--label", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--reuse-isolated-index", action="store_true")
    args = parser.parse_args()
    report = run(args)
    summary = {
        "status": "measured_not_accepted",
        "report": str(args.workspace / "measurement.json"),
        "aggregate": report["aggregate"],
    }
    if "context_v2" in report:
        summary["context_v2"] = report["context_v2"]["aggregate"]
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
