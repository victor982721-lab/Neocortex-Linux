"""Run the CA-12 Knowledge retrieval ablation on a frozen reserve.

This is development tooling.  It executes an explicitly frozen installed
release in isolated workspaces and uses the real Knowledge API; it is not a
runtime validator and never changes the product, corpus, model cache, or
sealed reserve.  The normal output is an aggregate-only comparison, which is
also the only output permitted in steward mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import time
from typing import Any


SCHEMA = "neocortex.knowledge-ablation/v1"
VARIANTS = ("full", "no_expansion", "no_semantic", "no_catalog")
MAX_TIMEOUT_SECONDS = 900
MAX_FILES = 50
MAX_BYTES = 20 * 1024 * 1024
MEMORY_LIMIT_BYTES = 12 * 1024 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{40,64}$")


class AblationError(ValueError):
    """Raised when a frozen measurement cannot be demonstrated safely."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AblationError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _tree_digest(
    root: Path, *, allow_symlinks: bool = False
) -> dict[str, int | str]:
    """Hash a contained tree without silently following symlink escapes.

    Fixture and state trees must not contain aliases, but Hugging Face's local
    model cache deliberately stores snapshot files as symlinks to content
    addressed blobs.  For that explicitly selected cache we include the link
    target in the digest and hash the in-tree target separately, so a target
    change is still observed without treating a valid cache layout as a
    mutable input escape.
    """

    digest = hashlib.sha256()
    count = total = 0
    if not root.is_dir() or root.is_symlink():
        raise AblationError(f"expected a real directory: {root}")
    root_resolved = root.resolve(strict=True)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if not allow_symlinks:
                raise AblationError(f"symlink is not allowed in immutable input: {path}")
            target = path.resolve(strict=True)
            if not target.is_relative_to(root_resolved):
                raise AblationError(f"symlink escapes immutable input: {path}")
            relative = path.relative_to(root).as_posix().encode()
            digest.update(relative + b"\0L" + os.readlink(path).encode())
            count += 1
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative + b"\0")
        file_digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
                size += len(chunk)
                total += len(chunk)
                file_digest.update(chunk)
        digest.update(str(size).encode() + b":" + file_digest.digest())
        count += 1
    return {"files": count, "bytes": total, "sha256": digest.hexdigest()}


def _strict_path(value: object, *, name: str, directory: bool = False, allow_symlink: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AblationError(f"{name} must be a non-empty path")
    raw_path = Path(value).expanduser()
    if raw_path.is_symlink() and not allow_symlink:
        raise AblationError(f"{name} must not be a symlink")
    path = raw_path.resolve(strict=True)
    if directory and not path.is_dir():
        raise AblationError(f"{name} must be a real directory")
    if not directory and not path.is_file():
        raise AblationError(f"{name} must be a regular file")
    return path


def _load_reserve(root: Path, freeze: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = read_json(root / "manifest.json")
    judgments = read_json(root / "queries.json")
    if manifest.get("schema") != "neocortex.functional-fixtures/v1":
        raise AblationError("reserve manifest schema is not supported")
    if judgments.get("schema") != "neocortex.functional-judgments/v1":
        raise AblationError("reserve judgments schema is not supported")
    expected = freeze.get("reserve")
    if not isinstance(expected, dict):
        raise AblationError("freeze is missing reserve composition")
    for key in ("files", "queries", "positive_queries", "negative_queries"):
        if type(expected.get(key)) is not int:
            raise AblationError(f"freeze reserve composition is missing {key}")
    entries = manifest.get("files")
    queries = judgments.get("queries")
    if not isinstance(entries, list) or not isinstance(queries, list):
        raise AblationError("reserve manifest or judgments has invalid arrays")
    if len(entries) != expected["files"] or len(queries) != expected["queries"]:
        raise AblationError("reserve composition does not match its frozen commitment")
    if expected["queries"] != expected["positive_queries"] + expected["negative_queries"]:
        raise AblationError("reserve composition is internally inconsistent")
    if len(entries) > MAX_FILES or sum(int(item.get("bytes", -1)) for item in entries) > MAX_BYTES:
        raise AblationError("reserve exceeds the contained measurement bound")
    names: set[str] = set()
    logical: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise AblationError("reserve manifest entry is invalid")
        path = root / entry["path"]
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise AblationError("reserve fixture escaped its root")
        if not entry.get("synthetic") or Path(entry["path"]).name in names:
            raise AblationError("reserve fixtures must be synthetic with unique basenames")
        names.add(Path(entry["path"]).name)
        if type(entry.get("bytes")) is not int or entry["bytes"] != path.stat().st_size or sha256(path) != entry.get("sha256"):
            raise AblationError("reserve fixture bytes changed")
        if not isinstance(entry.get("logical_resource_id"), str):
            raise AblationError("reserve fixture has no logical resource id")
        logical.add(entry["logical_resource_id"])
    if sum(query.get("kind") == "positive" for query in queries) != expected["positive_queries"]:
        raise AblationError("reserve positive-query composition changed")
    if sum(query.get("kind") == "negative" for query in queries) != expected["negative_queries"]:
        raise AblationError("reserve negative-query composition changed")
    if sha256(root / "manifest.json") != expected.get("manifest_sha256"):
        raise AblationError("reserve manifest hash differs from freeze")
    if sha256(root / "queries.json") != expected.get("queries_sha256"):
        raise AblationError("reserve judgments hash differs from freeze")
    return manifest, queries


def _release_root(launcher: Path) -> Path:
    candidates = [launcher.parent.parent]
    text = launcher.read_text(encoding="utf-8", errors="replace")
    for match in re.finditer(r"(?m)(/[^\s'\"]+/releases/[^\s'\"]+?/bin/Neocortex)", text):
        candidates.append(Path(match.group(1)).parent.parent)
    for candidate in candidates:
        if (candidate / "neocortex-release.json").is_file():
            return candidate.resolve(strict=True)
    raise AblationError("launcher does not identify a release manifest")


def _candidate_python(release_root: Path) -> Path:
    executable = release_root / "bin" / "Neocortex"
    first = executable.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    if not first or not first[0].startswith("#!"):
        raise AblationError("installed launcher has no explicit interpreter")
    # Preserve the venv path from the shebang.  Resolving it to the system
    # interpreter would lose the installed release's site-packages.
    python = Path(first[0][2:].strip()).expanduser()
    if not python.is_file():
        raise AblationError("installed interpreter path is missing")
    if not os.access(python, os.X_OK):
        raise AblationError("installed interpreter is not executable")
    return python


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("schema") != SCHEMA:
        raise AblationError("unknown CA-12 specification schema")
    candidate_sha = spec.get("candidate_sha")
    freeze_sha = spec.get("reserve_freeze_sha256")
    if not isinstance(candidate_sha, str) or len(candidate_sha) != 40 or SHA256_RE.fullmatch(candidate_sha) is None:
        raise AblationError("candidate_sha is not a lowercase commit hash")
    if not isinstance(freeze_sha, str) or len(freeze_sha) != 64 or re.fullmatch(r"[0-9a-f]{64}", freeze_sha) is None:
        raise AblationError("reserve_freeze_sha256 is invalid")
    variants = spec.get("variants")
    if tuple(variants or ()) != VARIANTS:
        raise AblationError(f"variants must be exactly {VARIANTS}")
    freeze_path = _strict_path(spec.get("reserve_freeze"), name="reserve_freeze")
    reserve_root = _strict_path(spec.get("reserve_root"), name="reserve_root", directory=True)
    launcher = _strict_path(spec.get("launcher"), name="launcher", allow_symlink=True)
    model_cache = _strict_path(spec.get("model_cache"), name="model_cache", directory=True)
    if sha256(freeze_path) != freeze_sha:
        raise AblationError("reserve freeze hash differs from the pre-frozen specification")
    freeze = read_json(freeze_path)
    if freeze.get("schema") != "neocortex.functional-freeze/v2" or not freeze.get("synthetic_only"):
        raise AblationError("CA-12 requires a synthetic v2 reserve freeze")
    if "steward-only" not in str(freeze.get("reserve_visibility", "")):
        raise AblationError("reserve is not explicitly steward-only")
    manifest, queries = _load_reserve(reserve_root, freeze)
    release_root = _release_root(launcher)
    release = read_json(release_root / "neocortex-release.json")
    if release.get("source_sha") != candidate_sha:
        raise AblationError("installed release does not match candidate_sha")
    interpreter = _candidate_python(release_root)
    if release_root == Path(__file__).resolve().parents[1] or release_root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise AblationError("candidate must be an installed release, not the checkout")
    workspace = Path(spec.get("workspace_root", "")).expanduser().resolve()
    if not str(workspace) or workspace == Path.home() or workspace == reserve_root or reserve_root.is_relative_to(workspace):
        raise AblationError("workspace_root is not isolated from the reserve")
    if workspace.exists():
        raise AblationError("workspace_root must not already exist")
    output = Path(spec.get("output", workspace.with_suffix(".json"))).expanduser().resolve()
    if output.exists() or output == freeze_path or output.is_relative_to(reserve_root):
        raise AblationError("output path is not a fresh non-reserve path")
    return {
        "candidate_sha": candidate_sha,
        "reserve_freeze_sha256": freeze_sha,
        "freeze_path": freeze_path,
        "reserve_root": reserve_root,
        "launcher": launcher,
        "release_root": release_root,
        "release_manifest": release_root / "neocortex-release.json",
        "interpreter": interpreter,
        "model_cache": model_cache,
        "workspace_root": workspace,
        "output": output,
        "freeze": freeze,
        "manifest": manifest,
        "queries": queries,
    }


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (840, 840))
    resource.setrlimit(resource.RLIMIT_NOFILE, (2048, 2048))


class _Runner:
    def __init__(self, cfg: dict[str, Any], timeout: int) -> None:
        if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise AblationError("timeout must be between 1 and 900 seconds")
        self.cfg = cfg
        self.deadline = time.monotonic() + timeout

    def env(self, workspace: Path) -> dict[str, str]:
        env = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PIP_CONFIG_FILE", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS"):
            env.pop(name, None)
        env.update({
            "HOME": str(workspace / "home"),
            "XDG_CONFIG_HOME": str(workspace / "config"),
            "XDG_STATE_HOME": str(workspace / "xdg-state"),
            "XDG_DATA_HOME": str(workspace / "xdg-data"),
            "XDG_CACHE_HOME": str(workspace / "xdg-cache"),
            "NEOCORTEX_CORPUS_ROOT": str(workspace / "corpus"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "OMP_NUM_THREADS": "2",
            "OPENBLAS_NUM_THREADS": "2",
            "MKL_NUM_THREADS": "2",
            "TOKENIZERS_PARALLELISM": "false",
        })
        return env

    def call(self, command: list[str], workspace: Path, log_name: str) -> tuple[int, str]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("CA-12 exceeded its shared hard wall-clock budget")
        log_dir = workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = log_dir / f"{log_name}.stdout", log_dir / f"{log_name}.stderr"
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = subprocess.Popen(command, cwd=workspace, env=self.env(workspace), stdout=out, stderr=err, start_new_session=True, preexec_fn=_limits)
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise TimeoutError("CA-12 subprocess exceeded its shared hard timeout") from None
        return process.returncode, stdout_path.read_text(encoding="utf-8", errors="replace")


def _copy_reserve(manifest: dict[str, Any], reserve_root: Path, corpus: Path) -> dict[str, str]:
    corpus.mkdir(parents=True)
    mapping: dict[str, str] = {}
    for entry in manifest["files"]:
        source = reserve_root / entry["path"]
        destination = corpus / Path(entry["path"]).name
        if destination.name in {Path(path).name for path in mapping} or destination.exists():
            raise AblationError("reserve basename collision")
        shutil.copyfile(source, destination)
        if sha256(destination) != entry["sha256"]:
            raise AblationError("isolated reserve fixture differs from frozen bytes")
        mapping[str(destination)] = entry["logical_resource_id"]
    return mapping


def _prepare_workspace(runner: _Runner, cfg: dict[str, Any], workspace: Path) -> tuple[dict[str, str], dict[str, Any]]:
    workspace.mkdir(mode=0o700, parents=True)
    for name in ("home", "config", "xdg-state", "xdg-cache", "xdg-data"):
        (workspace / name).mkdir(mode=0o700)
    corpus_map = _copy_reserve(cfg["manifest"], cfg["reserve_root"], workspace / "corpus")
    alias = workspace / "xdg-data" / "Neocortex" / "models" / "fastembed"
    alias.parent.mkdir(mode=0o700, parents=True)
    alias.symlink_to(cfg["model_cache"], target_is_directory=True)
    rc, _ = runner.call([
        str(cfg["release_root"] / "bin" / "Neocortex"), "--root", str(workspace / "corpus"), "--state-directory", str(workspace / "state"),
        "--route", "pdf,docx,office,text", "--strict-exit-codes", "--pdf-workers", "1", "--global-cpu-slots", "2",
    ], workspace, "01-ingest")
    if rc != 0:
        raise AblationError("installed reserve ingestion failed; inspect the contained log")
    semantic_args = [
        str(cfg["release_root"] / "bin" / "Neocortex"), "--root", str(workspace / "corpus"), "--state-directory", str(workspace / "state"),
        "--semantic-index", "text", "--semantic-model-cache", str(cfg["model_cache"]), "--semantic-threads", "2", "--semantic-max-items", "200", "--semantic-max-new-jobs", "200", "--semantic-time-budget-seconds", "550",
    ]
    for kind in ("pdf", "docx", "odt", "text"):
        semantic_args.extend(("--semantic-source", kind))
    rc, semantic_stdout = runner.call(semantic_args, workspace, "02-semantic")
    if rc != 0 or "complete=1" not in semantic_stdout or "failed=0" not in semantic_stdout:
        raise AblationError("installed semantic preparation was not complete")
    state_digest = _tree_digest(workspace / "state")
    corpus_digest = _tree_digest(workspace / "corpus")
    return corpus_map, {"state": state_digest, "corpus": corpus_digest}


def _worker_code() -> str:
    # Kept as a child program so the benchmark never imports the checkout's
    # product modules into the coordinator process.
    return r'''
import json, sys
from dataclasses import fields
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
release_root = Path(cfg["release_root"]).resolve()
import neocortex
module_path = Path(neocortex.__file__).resolve()
if not module_path.is_relative_to(release_root):
    raise RuntimeError("candidate worker imported code outside the installed release")
from neocortex.knowledge.knowledge_planner import KnowledgeQuery, RetrievalMode, plan_knowledge_query
from neocortex.knowledge.knowledge_service import KnowledgeSearchService
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.knowledge.knowledge_search import execute_knowledge_search
from neocortex.semantic import semantic_query_variants, semantic_search_service

variant = cfg["variant"]
entries = cfg["entries"]
queries = cfg["queries"]
expansion_calls = 0
expansion_nonempty = 0

original_expansions = semantic_query_variants.text_query_expansions
if variant == "no_expansion":
    def disabled_expansions(query):
        global expansion_calls
        expansion_calls += 1
        return ()
    semantic_query_variants.text_query_expansions = disabled_expansions
elif variant == "full":
    def observed_expansions(query):
        global expansion_calls, expansion_nonempty
        expansion_calls += 1
        values = original_expansions(query)
        expansion_nonempty += int(bool(values))
        return values
    semantic_query_variants.text_query_expansions = observed_expansions

def ablated_plan(plan):
    if variant not in {"no_semantic", "no_catalog"}:
        return plan
    excluded = "semantic" if variant == "no_semantic" else "catalog"
    values = object.__new__(type(plan))
    for field in fields(plan):
        value = getattr(plan, field.name)
        if field.name == "steps":
            value = tuple(step for step in value if step.channel != excluded)
        object.__setattr__(values, field.name, value)
    return values

def planner(query):
    return ablated_plan(plan_knowledge_query(query))

def executor(paths, plan, snapshot, *, cancellation_check=None):
    return execute_knowledge_search(paths, plan, snapshot, cancellation_check=cancellation_check)

service = KnowledgeSearchService(KnowledgeStatePaths.from_directory(Path(cfg["state"])), query_planner=planner, search_executor=executor)
logical_by_path = entries
aggregate = {"queries": 0, "positive_queries": 0, "negative_queries": 0, "positive_successes_at_5": 0, "success_at_5": 0.0, "recall_at_5": 0.0, "ndcg_at_10": 0.0, "negative_unsupported_evidence": 0, "citation_invalid_queries": 0, "execution_invalid_queries": 0, "coverage_partial_queries": 0, "real_vector_queries": 0, "context_supported_queries": 0}
control = {"semantic_planned_queries": 0, "catalog_planned_queries": 0, "semantic_executed_queries": 0, "catalog_executed_queries": 0}

def ranking(result):
    values, seen = [], set()
    for hit in result.hits:
        path = hit.resource.current_path
        logical = logical_by_path.get(path, "unknown:" + str(path))
        if logical not in seen:
            seen.add(logical); values.append(logical)
    return values

for query_data in queries:
    aggregate["queries"] += 1
    positive = query_data["kind"] == "positive"
    aggregate["positive_queries"] += int(positive)
    aggregate["negative_queries"] += int(not positive)
    try:
        query = KnowledgeQuery(query_data["text"], retrieval_mode=RetrievalMode.EVIDENCE, limit=100)
        result = service.search(query)
        steps = {step.channel for step in result.plan.steps}
        control["semantic_planned_queries"] += int("semantic" in steps)
        control["catalog_planned_queries"] += int("catalog" in steps)
        names = {item.name for item in result.rankings if item.executed}
        control["semantic_executed_queries"] += int(any(name.startswith("semantic") for name in names))
        control["catalog_executed_queries"] += int("catalog_metadata" in names)
        aggregate["real_vector_queries"] += int(result.vectors_scanned > 0)
        aggregate["coverage_partial_queries"] += int(not result.complete)
        ranked = ranking(result)
        relevance = query_data["relevance"]
        if positive:
            relevant = set(relevance)
            found = relevant.intersection(ranked[:5])
            aggregate["success_at_5"] += int(bool(found))
            aggregate["positive_successes_at_5"] += int(bool(found))
            aggregate["recall_at_5"] += len(found) / len(relevant) if relevant else 0.0
            gains = sum((2 ** relevance.get(item, 0) - 1) / __import__("math").log2(pos + 2) for pos, item in enumerate(ranked[:10]))
            ideal = sum((2 ** grade - 1) / __import__("math").log2(pos + 2) for pos, grade in enumerate(sorted(relevance.values(), reverse=True)[:10]))
            aggregate["ndcg_at_10"] += gains / ideal if ideal else 0.0
        context = service.context(query, max_characters=12000, max_hits=10)
        selected = {hit.evidence.evidence_id for hit in context.selected_hits}
        cited = {pair[1] for pair in context.citation_ids}
        aggregate["citation_invalid_queries"] += int(selected != cited or not context.citation_ids)
        aggregate["context_supported_queries"] += int(bool(set(relevance).intersection(ranking(type("R", (), {"hits": context.selected_hits})())))) if positive else 0
        if not positive:
            aggregate["negative_unsupported_evidence"] += len(context.selected_hits)
    except Exception:
        aggregate["execution_invalid_queries"] += 1

for key in ("success_at_5", "recall_at_5", "ndcg_at_10"):
    aggregate[key] = aggregate[key] / aggregate["positive_queries"] if aggregate["positive_queries"] else 0.0
aggregate["citation_integrity"] = int(aggregate["citation_invalid_queries"] == 0 and aggregate["queries"] > 0)
aggregate["execution_complete"] = int(aggregate["execution_invalid_queries"] == 0)
aggregate["real_model_used"] = int(aggregate["real_vector_queries"] == aggregate["queries"])
control["expansion_calls"] = expansion_calls
control["expansion_nonempty"] = expansion_nonempty
print(json.dumps({"aggregate": aggregate, "control": control}, sort_keys=True))
'''


def _run_variant(runner: _Runner, cfg: dict[str, Any], variant: str, workspace: Path, corpus_map: dict[str, str], baseline_digests: dict[str, Any]) -> dict[str, Any]:
    variant_map = {
        str(workspace / "corpus" / Path(source).name): logical
        for source, logical in corpus_map.items()
    }
    write_json(workspace / "worker-config.json", {"variant": variant, "state": str(workspace / "state"), "release_root": str(cfg["release_root"]), "entries": variant_map, "queries": cfg["queries"]})
    command = [str(cfg["interpreter"]), "-c", _worker_code(), str(workspace / "worker-config.json")]
    rc, output = runner.call(command, workspace, "03-query")
    if rc != 0:
        raise AblationError(f"variant {variant} failed; inspect its contained log")
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AblationError(f"variant {variant} emitted a non-aggregate worker payload")
    payload = json.loads(lines[0])
    if set(payload) != {"aggregate", "control"}:
        raise AblationError(f"variant {variant} worker payload is not aggregate-only")
    if _tree_digest(workspace / "corpus") != baseline_digests["corpus"] or _tree_digest(workspace / "state") != baseline_digests["state"]:
        raise AblationError(f"variant {variant} changed isolated corpus or state")
    return payload


def _check_controls(results: dict[str, dict[str, Any]]) -> None:
    full = results["full"]["control"]
    if full["expansion_nonempty"] < 1:
        raise AblationError("no_expansion could not be demonstrated: full had no expansion")
    if results["no_expansion"]["control"]["expansion_nonempty"] != 0 or results["no_expansion"]["control"]["expansion_calls"] == 0:
        raise AblationError("no_expansion control did not alter the executed expansion flow")
    if full["semantic_planned_queries"] == 0 or results["no_semantic"]["control"]["semantic_planned_queries"] != 0 or results["no_semantic"]["control"]["semantic_executed_queries"] != 0:
        raise AblationError("no_semantic control could not be demonstrated")
    if full["catalog_planned_queries"] == 0 or results["no_catalog"]["control"]["catalog_planned_queries"] != 0 or results["no_catalog"]["control"]["catalog_executed_queries"] != 0:
        raise AblationError("no_catalog control could not be demonstrated")


def run(spec: dict[str, Any], *, timeout_seconds: int = MAX_TIMEOUT_SECONDS) -> dict[str, Any]:
    cfg = validate_spec(spec)
    runner = _Runner(cfg, timeout_seconds)
    cfg["workspace_root"].mkdir(mode=0o700, parents=True)
    source_cache_digest = _tree_digest(cfg["model_cache"], allow_symlinks=True)
    full_workspace = cfg["workspace_root"] / "full"
    corpus_map, digests = _prepare_workspace(runner, cfg, full_workspace)
    results: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        workspace = cfg["workspace_root"] / variant
        if variant != "full":
            shutil.copytree(full_workspace, workspace, symlinks=True)
            # Each variant owns its isolation directories and logs, while the
            # corpus and state bytes remain an exact copy of the prepared view.
            for name in ("home", "config", "xdg-state", "xdg-cache", "xdg-data", "logs"):
                shutil.rmtree(workspace / name, ignore_errors=True)
                (workspace / name).mkdir(mode=0o700, parents=True)
            alias = workspace / "xdg-data" / "Neocortex" / "models" / "fastembed"
            alias.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            alias.symlink_to(cfg["model_cache"], target_is_directory=True)
        results[variant] = _run_variant(runner, cfg, variant, workspace, corpus_map, digests)
    if _tree_digest(cfg["model_cache"], allow_symlinks=True) != source_cache_digest:
        raise AblationError("model cache changed during CA-12; cache is read-only")
    _check_controls(results)
    criteria = {
        "reserve_queries": cfg["freeze"]["reserve"]["queries"],
        "reserve_positive_queries": cfg["freeze"]["reserve"]["positive_queries"],
        "reserve_negative_queries": cfg["freeze"]["reserve"]["negative_queries"],
        "aggregate_only": True,
        "effective_controls": True,
        "no_query_or_case_payload": True,
    }
    report = {
        "schema": SCHEMA,
        "candidate_sha": cfg["candidate_sha"],
        "candidate_release_manifest_sha256": sha256(cfg["release_manifest"]),
        "reserve_freeze_sha256": cfg["reserve_freeze_sha256"],
        "reserve_manifest_sha256": sha256(cfg["reserve_root"] / "manifest.json"),
        "reserve_judgments_sha256": sha256(cfg["reserve_root"] / "queries.json"),
        "model_cache_sha256": source_cache_digest["sha256"],
        "criteria": criteria,
        "variants": {name: results[name] for name in VARIANTS},
    }
    full_aggregate = results["full"]["aggregate"]
    report["deltas_vs_full"] = {
        name: {
            metric: results[name]["aggregate"].get(metric, 0) - full_aggregate.get(metric, 0)
            for metric in ("success_at_5", "recall_at_5", "ndcg_at_10", "positive_successes_at_5", "negative_unsupported_evidence", "execution_invalid_queries")
        }
        for name in VARIANTS
        if name != "full"
    }
    write_json(cfg["output"], report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=MAX_TIMEOUT_SECONDS)
    parser.add_argument("--mode", choices=("steward", "development"), default="steward")
    args = parser.parse_args(argv)
    spec = read_json(args.spec.resolve(strict=True))
    report = run(spec, timeout_seconds=args.timeout_seconds)
    output = Path(spec.get("output", Path(spec["workspace_root"]).expanduser().resolve().with_suffix(".json"))).expanduser().resolve()
    print(json.dumps({"status": "measured", "report": str(output), "candidate_sha": report["candidate_sha"], "variants": list(VARIANTS)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
