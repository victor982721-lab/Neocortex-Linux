"""Deterministic local and CI quality gates for the complete source tree.

The test inventory is discovered from the filesystem on every run.  Static
analysis debt is accepted only through a versioned, per-path/per-rule baseline;
removing debt is always allowed, while new or multiplied diagnostics fail.
Complete production coverage is branch-aware and can only ratchet against an
explicit, versioned baseline produced by the same canonical environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast


BASELINE_SCHEMA = "neocortex.quality-gate-static-baseline/v1"
STATIC_DIAGNOSTIC_SHADOW_SCHEMA = "neocortex.quality-gate-static-diagnostic-shadow/v1"
STATIC_DIAGNOSTIC_FINGERPRINT_ALGORITHM = (
    "sha256(canonical-json(tool,version,path,rule,severity,normalized-message,anchor,symbol))-v1"
)
COVERAGE_BASELINE_SCHEMA = "neocortex.quality-gate-coverage-baseline/v1"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = Path(__file__).with_name("quality_gate_static_baseline.json")
DEFAULT_COVERAGE_BASELINE = Path(__file__).with_name("quality_gate_coverage_baseline.json")
DEFAULT_SUPPLY_POLICY = Path(__file__).with_name("quality_gate_supply_policy.json")
PRODUCTION_ARCHITECTURE_WORKER = Path("_04_Nucleo_Operativo/external_architecture_worker.py")
EXPECTED_ARCHITECTURE_BASELINE_ID = "neocortex-production-imports-2026-08-10/v2"
EXPECTED_ARCHITECTURE_CONTRACTS = frozenset(
    {
        "core-does-not-depend-on-ui-v1",
        "dedup-core-boundary-v1",
        "foundation-does-not-depend-on-core-or-ui-v1",
        "neocortex-core-ui-boundary-v1",
        "no-new-production-import-cycles-v1",
        "production-does-not-import-nonproduction-namespaces-v1",
    }
)
TEST_PATTERNS = ("test_*.py", "*_test.py")
PRODUCTION_TYPE_TARGETS = (
    "Orquestador.py",
    "_01_Enumeracion",
    "_02_Deduplicacion",
    "_03_Progreso",
    "_04_Nucleo_Operativo",
    "_05_Interfaz",
    "neocortex",
)
PRODUCTION_COVERAGE_SOURCES = (
    "Orquestador",
    *PRODUCTION_TYPE_TARGETS[1:],
)
WHEEL_PACKAGE_ROOTS = (
    "_01_Enumeracion",
    "_02_Deduplicacion",
    "_03_Progreso",
    "_04_Nucleo_Operativo",
    "_05_Interfaz",
    "neocortex",
)
STATIC_TIMEOUT_SECONDS = 15 * 60
ARCHITECTURE_TIMEOUT_SECONDS = 5 * 60
AUDIT_TIMEOUT_SECONDS = 10 * 60

INSTALLED_WHEEL_PROBE = r"""
import importlib
import json
import pathlib
import sys
from importlib import metadata

contract = json.loads(sys.argv[1])
origins = {}
for name in [*contract["package_roots"], "Orquestador"]:
    module = importlib.import_module(name)
    origin = pathlib.Path(module.__file__).resolve()
    if "site-packages" not in origin.parts:
        raise SystemExit(f"{name} did not resolve from site-packages: {origin}")
    origins[name] = str(origin)
distribution = metadata.distribution("neocortex-framework")
if distribution.version != contract["version"]:
    raise SystemExit(f"wheel version mismatch: {distribution.version}")
public = importlib.import_module("neocortex")
if public.__version__ != contract["version"]:
    raise SystemExit(f"public version mismatch: {public.__version__}")
entrypoints = sorted(
    (item.name, item.value)
    for item in distribution.entry_points
    if item.group == "console_scripts"
)
if entrypoints != [("Neocortex", "neocortex.cli:entrypoint")]:
    raise SystemExit(f"console entrypoint mismatch: {entrypoints!r}")
installed_files = {str(item).replace("\\", "/") for item in (distribution.files or ())}
missing_data = sorted(set(contract["package_data"]) - installed_files)
if missing_data:
    raise SystemExit(f"wheel package data missing: {missing_data!r}")
print(json.dumps({
    "entrypoint": "Neocortex=neocortex.cli:entrypoint",
    "origins": origins,
    "package_data": contract["package_data"],
    "version": distribution.version,
}, sort_keys=True))
"""


class GateError(RuntimeError):
    """A quality gate could not prove its contract."""


@dataclass(frozen=True, slots=True)
class TestFile:
    path: str
    size: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class StaticDiagnosticEvidence:
    tool: str
    version: str
    path: str
    rule: str
    severity: str
    normalized_message: str
    anchor: str | None = None
    symbol: str | None = None

    @property
    def fingerprint(self) -> str:
        payload = {
            "tool": self.tool,
            "version": self.version,
            "path": self.path,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.normalized_message,
            "anchor": self.anchor,
            "symbol": self.symbol,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "static-diagnostic-v1:sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class StaticObservation:
    tool: str
    version: str
    counts: Counter[tuple[str, str, str]]
    diagnostics: tuple[StaticDiagnosticEvidence, ...] = ()

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _fail(message: str) -> NoReturn:
    raise GateError(message)


def _root(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.is_dir():
        _fail(f"repository root is not a directory: {candidate}")
    if not (candidate / "tests").is_dir() or not (candidate / "pyproject.toml").is_file():
        _fail(f"repository root lacks tests/ or pyproject.toml: {candidate}")
    return candidate


def _portable_python_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes().replace(b"\r\n", b"\n")
    except OSError as error:
        _fail(f"could not read Python source {path}: {error}")


def discover_test_files(root: Path) -> tuple[TestFile, ...]:
    """Return every regular pytest module under tests/, with stable paths."""

    tests_root = root / "tests"
    candidates: set[Path] = set()
    for pattern in TEST_PATTERNS:
        candidates.update(tests_root.rglob(pattern))
    files: list[TestFile] = []
    for candidate in sorted(candidates):
        if candidate.is_symlink() or not candidate.is_file():
            _fail(f"test inventory contains a non-regular file: {candidate}")
        relative = candidate.relative_to(root).as_posix()
        content = _portable_python_bytes(candidate)
        files.append(TestFile(relative, len(content), hashlib.sha256(content).hexdigest()))
    if not files:
        _fail("test inventory is empty")
    return tuple(files)


def partition_test_files(
    files: Sequence[TestFile], shard_count: int
) -> tuple[tuple[TestFile, ...], ...]:
    """Balance files by byte size with a deterministic greedy partition."""

    if shard_count < 1:
        _fail("shard-count must be positive")
    if shard_count > len(files):
        _fail("shard-count cannot exceed the number of test files")
    shards: list[list[TestFile]] = [[] for _ in range(shard_count)]
    weights = [0] * shard_count
    for item in sorted(files, key=lambda value: (-value.size, value.path)):
        index = min(range(shard_count), key=lambda value: (weights[value], value))
        shards[index].append(item)
        weights[index] += max(1, item.size)
    return tuple(tuple(sorted(shard, key=lambda value: value.path)) for shard in shards)


def build_test_inventory_payload(root: Path, shard_count: int) -> dict[str, object]:
    files = discover_test_files(root)
    shards = partition_test_files(files, shard_count)
    assigned = [item.path for shard in shards for item in shard]
    expected = [item.path for item in files]
    if len(assigned) != len(set(assigned)) or sorted(assigned) != expected:
        _fail("test shards are not a disjoint, complete partition")
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(item.content_sha256))
        digest.update(b"\n")
    return {
        "schema": "neocortex.quality-gate-test-inventory/v1",
        "root": os.fspath(root),
        "file_count": len(files),
        "total_bytes": sum(item.size for item in files),
        "hash_algorithm": "sha256(path,lf-size,sha256(lf-content))-v1",
        "sha256": digest.hexdigest(),
        "files": expected,
        "shard_count": shard_count,
        "shards": [
            {
                "index": index,
                "file_count": len(shard),
                "total_bytes": sum(item.size for item in shard),
                "files": [item.path for item in shard],
            }
            for index, shard in enumerate(shards)
        ],
    }


def _selected_test_paths(root: Path, shard_count: int, shard_index: int) -> tuple[str, ...]:
    files = discover_test_files(root)
    shards = partition_test_files(files, shard_count)
    if not 0 <= shard_index < len(shards):
        _fail(f"shard-index must be between 0 and {len(shards) - 1}")
    return tuple(os.fspath(root / item.path) for item in shards[shard_index])


def _safe_pytest_path(path: Path, *, label: str) -> Path:
    candidate = path.expanduser().resolve()
    codex_home_raw = os.environ.get("CODEX_HOME", "").strip()
    codex_home = (
        Path(codex_home_raw).expanduser().resolve()
        if codex_home_raw
        else (Path.home() / ".codex").resolve()
    )
    if candidate == codex_home or candidate.is_relative_to(codex_home):
        _fail(f"{label} must remain outside protected Codex home and vault: {candidate}")
    return candidate


def _pytest_command(
    selected: Sequence[str],
    *,
    basetemp: Path | None,
    pytest_arguments: Sequence[str] = (),
) -> list[str]:
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if basetemp is not None:
        resolved_basetemp = _safe_pytest_path(basetemp, label="pytest basetemp")
        resolved_basetemp.parent.mkdir(parents=True, exist_ok=True)
        command.extend(("--basetemp", os.fspath(resolved_basetemp)))
    command.extend(pytest_arguments)
    command.extend(selected)
    return command


def _pytest_environment(basetemp: Path | None) -> dict[str, str]:
    """Bind pytest and tempfile users to one disposable non-protected boundary."""

    temporary_root = (
        _safe_pytest_path(Path(tempfile.gettempdir()), label="pytest temporary root")
        if basetemp is None
        else _safe_pytest_path(basetemp, label="pytest basetemp").parent
    )
    temporary_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    for name in ("TMPDIR", "TEMP", "TMP"):
        environment[name] = os.fspath(temporary_root)
    return environment


def run_test_shard(
    root: Path,
    *,
    shard_count: int,
    shard_index: int,
    basetemp: Path | None,
    pytest_arguments: Sequence[str] = (),
) -> int:
    selected = _selected_test_paths(root, shard_count, shard_index)
    command = _pytest_command(
        selected,
        basetemp=basetemp,
        pytest_arguments=pytest_arguments,
    )
    print(
        f"test shard {shard_index + 1}/{shard_count}: {len(selected)} dynamically discovered files",
        flush=True,
    )
    return subprocess.call(command, cwd=root, env=_pytest_environment(basetemp))


def discover_production_sources(root: Path) -> tuple[str, ...]:
    """Return the exact Python source inventory measured by the coverage gate."""

    orchestrator = root / "Orquestador.py"
    if orchestrator.is_symlink() or not orchestrator.is_file():
        _fail(f"production coverage root is missing or unsafe: {orchestrator}")
    candidates = [orchestrator]
    for package in PRODUCTION_COVERAGE_SOURCES[1:]:
        package_root = root / package
        if package_root.is_symlink() or not package_root.is_dir():
            _fail(f"production coverage root is missing or unsafe: {package_root}")
        package_files = list(package_root.rglob("*.py"))
        if not package_files:
            _fail(f"production coverage root contains no Python sources: {package_root}")
        candidates.extend(package_files)
    paths: list[str] = []
    for candidate in sorted(set(candidates)):
        if candidate.is_symlink() or not candidate.is_file():
            _fail(f"production coverage inventory contains a non-regular file: {candidate}")
        paths.append(candidate.relative_to(root).as_posix())
    if not paths:
        _fail("production coverage inventory is empty")
    # ``WindowsPath`` orders case-insensitively before conversion while the
    # persisted manifest is a platform-neutral POSIX string inventory.  Sort
    # only after normalization so one checkout produces the same ordered
    # contract on every supported platform.
    return tuple(sorted(paths))


def _run_captured(
    command: Sequence[str],
    *,
    root: Path,
    timeout: int,
    allowed_codes: frozenset[int],
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=None if environment is None else dict(environment),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _fail(f"could not run {command[0]}: {type(error).__name__}: {error}")
    if completed.returncode not in allowed_codes:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        _fail(f"{command[0]} exited {completed.returncode}: {detail}")
    return completed


def _source_distribution_version(root: Path) -> str:
    try:
        source = (root / "neocortex" / "__init__.py").read_text(encoding="utf-8")
    except OSError as error:
        _fail(f"could not read source version: {error}")
    matched = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', source, re.MULTILINE)
    if matched is None:
        _fail("source version declaration is missing")
    return matched.group(1)


def run_installed_wheel_gate(root: Path, probe_directory: Path) -> dict[str, object]:
    """Execute package roots, data, entrypoint and version outside the checkout."""

    probe = probe_directory.expanduser().resolve()
    if probe == root or probe.is_relative_to(root):
        _fail("installed-wheel probe directory must be outside the repository")
    probe.mkdir(parents=True, exist_ok=True)
    rules_root = root / "_04_Nucleo_Operativo" / "semgrep_rules"
    assets_root = root / "_05_Interfaz" / "assets"
    if any(path.is_symlink() or not path.is_dir() for path in (rules_root, assets_root)):
        _fail("source package-data roots are missing or unsafe")
    rules = sorted(
        path.relative_to(root).as_posix()
        for path in rules_root.glob("*.yml")
        if path.is_file() and not path.is_symlink()
    )
    assets = sorted(
        path.relative_to(root).as_posix()
        for path in assets_root.iterdir()
        if path.is_file() and not path.is_symlink() and path.suffix in {".ico", ".png", ".svg"}
    )
    if not rules or not assets:
        _fail("source package-data contract is empty")
    expected_version = _source_distribution_version(root)
    contract = {
        "package_roots": list(WHEEL_PACKAGE_ROOTS),
        "package_data": [*rules, *assets],
        "version": expected_version,
    }
    completed = _run_captured(
        (
            sys.executable,
            "-I",
            "-c",
            INSTALLED_WHEEL_PROBE,
            json.dumps(contract, sort_keys=True),
        ),
        root=probe,
        timeout=2 * 60,
        allowed_codes=frozenset({0}),
    )
    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _fail(f"installed-wheel probe returned invalid JSON: {error}")
    if not isinstance(summary, Mapping):
        _fail("installed-wheel probe returned a non-object payload")
    expected_output = f"Neocortex {expected_version}"
    module_version = _run_captured(
        (sys.executable, "-I", "-m", "neocortex", "--version"),
        root=probe,
        timeout=60,
        allowed_codes=frozenset({0}),
    ).stdout.strip()
    scripts = Path(sysconfig.get_path("scripts"))
    entrypoint = scripts / ("Neocortex.exe" if os.name == "nt" else "Neocortex")
    entrypoint_version = _run_captured(
        (os.fspath(entrypoint), "--version"),
        root=probe,
        timeout=60,
        allowed_codes=frozenset({0}),
    ).stdout.strip()
    if module_version != expected_output or entrypoint_version != expected_output:
        _fail(
            "installed wheel version output drifted: "
            f"module={module_version!r}, entrypoint={entrypoint_version!r}"
        )
    return dict(summary)


def evaluate_architecture_payload(payload: Mapping[str, object]) -> dict[str, object]:
    if payload.get("schema") != "neocortex.external-architecture-worker/grimp-v1":
        _fail("Grimp worker returned an unsupported schema")
    if payload.get("status") != "ready":
        _fail("Grimp worker did not return ready evidence")
    raw_counters = payload.get("counters")
    if not isinstance(raw_counters, Mapping):
        _fail("Grimp worker counters are missing")
    raw_violations = raw_counters.get("contract_violations")
    raw_cyclic = raw_counters.get("cyclic_components")
    raw_modules = raw_counters.get("modules")
    raw_relations = raw_counters.get("production_relations")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (raw_violations, raw_cyclic, raw_modules, raw_relations)
    ):
        _fail("Grimp worker counters are malformed")
    if cast(int, raw_modules) <= 0 or cast(int, raw_relations) < 0:
        _fail("Grimp worker module or relation counters are invalid")
    evaluations = payload.get("contract_evaluations")
    cycles = payload.get("cycles")
    if not isinstance(evaluations, list) or not isinstance(cycles, list):
        _fail("Grimp worker omitted contracts or cycles")
    raw_tool = payload.get("tool")
    if (
        not isinstance(raw_tool, Mapping)
        or raw_tool.get("name") != "grimp"
        or not isinstance(raw_tool.get("version"), str)
        or not raw_tool.get("version")
    ):
        _fail("Grimp worker omitted its tool version")
    raw_architecture = payload.get("architecture")
    if (
        not isinstance(raw_architecture, Mapping)
        or raw_architecture.get("baseline_id") != EXPECTED_ARCHITECTURE_BASELINE_ID
    ):
        _fail("Grimp worker omitted its architecture baseline identity")
    raw_inputs = payload.get("inputs")
    if (
        not isinstance(raw_inputs, Mapping)
        or re.fullmatch(r"[0-9a-f]{64}", str(raw_inputs.get("content_manifest_sha256", ""))) is None
    ):
        _fail("Grimp worker omitted its input manifest")
    observed_contracts: set[str] = set()
    failed_contracts: list[str] = []
    for item in evaluations:
        if not isinstance(item, Mapping):
            _fail("Grimp worker returned a malformed contract evaluation")
        contract = item.get("contract")
        contract_id = contract.get("contract_id") if isinstance(contract, Mapping) else None
        if not isinstance(contract_id, str) or contract_id in observed_contracts:
            _fail("Grimp worker returned a malformed or duplicate contract identity")
        observed_contracts.add(contract_id)
        if item.get("status") != "passed":
            failed_contracts.append(contract_id)
    failed_contracts.sort()
    if observed_contracts != EXPECTED_ARCHITECTURE_CONTRACTS:
        _fail(f"Grimp worker contract inventory drifted: observed={sorted(observed_contracts)!r}")
    if raw_violations or raw_cyclic or failed_contracts or cycles:
        _fail(
            "live architecture contracts failed: "
            f"violations={raw_violations}, contracts={failed_contracts}, "
            f"cyclic_components={raw_cyclic}, cycles={len(cycles)}"
        )
    return {
        "modules": cast(int, raw_modules),
        "production_relations": cast(int, raw_relations),
        "contract_violations": cast(int, raw_violations),
        "cyclic_components": cast(int, raw_cyclic),
        "contract_ids": sorted(observed_contracts),
        "grimp_version": raw_tool["version"],
        "architecture_baseline_id": raw_architecture["baseline_id"],
        "input_manifest_sha256": raw_inputs["content_manifest_sha256"],
    }


def run_architecture_gate(root: Path) -> dict[str, object]:
    worker = root / PRODUCTION_ARCHITECTURE_WORKER
    if not worker.is_file():
        _fail(f"architecture worker is missing: {worker}")
    completed = _run_captured(
        (
            sys.executable,
            "-I",
            os.fspath(worker),
            "grimp",
            "--root",
            os.fspath(root),
        ),
        root=root,
        timeout=ARCHITECTURE_TIMEOUT_SECONDS,
        allowed_codes=frozenset({0}),
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _fail(f"Grimp worker returned invalid JSON: {error}")
    if not isinstance(payload, Mapping):
        _fail("Grimp worker returned a non-object payload")
    return evaluate_architecture_payload(payload)


def _semantic_version(output: str, tool: str) -> str:
    match = re.search(r"\b(\d+\.\d+(?:\.\d+)*)\b", output)
    if match is None:
        _fail(f"could not parse {tool} version: {output.strip()!r}")
    return match.group(1)


def _relative_path(raw: object, root: Path) -> str:
    text = str(raw).replace("\\", "/")
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            return candidate.as_posix()
    return text.removeprefix("./")


def _normalize_static_diagnostic_message(raw: object, root: Path) -> str:
    """Normalize volatile layout and whitespace without erasing diagnostic meaning."""

    text = "unknown" if raw is None else str(raw)
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    root_variants = {
        os.fspath(root),
        os.fspath(root).replace("\\", "/"),
        os.fspath(root).replace("/", "\\"),
    }
    for variant in sorted((item for item in root_variants if item), key=len, reverse=True):
        text = text.replace(variant, "<root>")
    normalized = " ".join(text.split())
    return normalized or "unknown"


def _optional_static_symbol(item: Mapping[str, object]) -> str | None:
    for key in ("symbol", "symbolName"):
        raw = item.get(key)
        if isinstance(raw, str):
            normalized = " ".join(unicodedata.normalize("NFC", raw).split())
            if normalized:
                return normalized
    return None


def _static_coordinate(raw: object, *, offset: int = 0) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw + offset


def _static_anchor(
    start_line: object,
    start_column: object,
    end_line: object = None,
    end_column: object = None,
    *,
    offset: int = 0,
) -> str | None:
    line = _static_coordinate(start_line, offset=offset)
    if line is None:
        return None
    column = _static_coordinate(start_column, offset=offset)
    start = str(line) if column is None else f"{line}:{column}"
    last_line = _static_coordinate(end_line, offset=offset)
    if last_line is None:
        return start
    last_column = _static_coordinate(end_column, offset=offset)
    end = str(last_line) if last_column is None else f"{last_line}:{last_column}"
    return f"{start}-{end}"


def _nested_static_anchor(
    start: object,
    end: object,
    *,
    line_key: str,
    column_key: str,
    offset: int = 0,
) -> str | None:
    start_mapping = start if isinstance(start, Mapping) else {}
    end_mapping = end if isinstance(end, Mapping) else {}
    return _static_anchor(
        start_mapping.get(line_key),
        start_mapping.get(column_key),
        end_mapping.get(line_key),
        end_mapping.get(column_key),
        offset=offset,
    )


def _static_diagnostic_evidence(
    *,
    tool: str,
    version: str,
    path: str,
    rule: str,
    severity: str,
    message: object,
    root: Path,
    anchor: str | None,
    symbol: str | None,
) -> StaticDiagnosticEvidence:
    return StaticDiagnosticEvidence(
        tool=tool,
        version=version,
        path=path,
        rule=rule,
        severity=severity,
        normalized_message=_normalize_static_diagnostic_message(message, root),
        anchor=anchor,
        symbol=symbol,
    )


def _ruff_observation(root: Path) -> StaticObservation:
    version_run = _run_captured(
        (sys.executable, "-m", "ruff", "--version"),
        root=root,
        timeout=60,
        allowed_codes=frozenset({0}),
    )
    completed = _run_captured(
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format=json",
            ".",
        ),
        root=root,
        timeout=STATIC_TIMEOUT_SECONDS,
        allowed_codes=frozenset({0, 1}),
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _fail(f"Ruff returned invalid JSON: {error}")
    if not isinstance(payload, list):
        _fail("Ruff returned a non-list payload")
    version = _semantic_version(version_run.stdout, "Ruff")
    counts: Counter[tuple[str, str, str]] = Counter()
    diagnostics: list[StaticDiagnosticEvidence] = []
    for item in payload:
        if not isinstance(item, Mapping):
            _fail("Ruff returned a malformed diagnostic")
        path = _relative_path(item.get("filename", "unknown"), root)
        rule = str(item.get("code") or "unknown")
        severity = "error"
        counts[(path, rule, severity)] += 1
        diagnostics.append(
            _static_diagnostic_evidence(
                tool="ruff",
                version=version,
                path=path,
                rule=rule,
                severity=severity,
                message=item.get("message"),
                root=root,
                anchor=_nested_static_anchor(
                    item.get("location"),
                    item.get("end_location"),
                    line_key="row",
                    column_key="column",
                ),
                symbol=_optional_static_symbol(item),
            )
        )
    return StaticObservation("ruff", version, counts, tuple(diagnostics))


def _mypy_observation(root: Path) -> StaticObservation:
    version_run = _run_captured(
        (sys.executable, "-m", "mypy", "--version"),
        root=root,
        timeout=60,
        allowed_codes=frozenset({0}),
    )
    completed = _run_captured(
        (
            sys.executable,
            "-m",
            "mypy",
            "-O",
            "json",
            "--no-incremental",
            *PRODUCTION_TYPE_TARGETS,
        ),
        root=root,
        timeout=STATIC_TIMEOUT_SECONDS,
        allowed_codes=frozenset({0, 1}),
    )
    version = _semantic_version(version_run.stdout, "Mypy")
    counts: Counter[tuple[str, str, str]] = Counter()
    diagnostics: list[StaticDiagnosticEvidence] = []
    for line_number, raw_line in enumerate(completed.stdout.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError as error:
            _fail(f"Mypy returned invalid JSON on line {line_number}: {error}")
        if not isinstance(item, Mapping):
            _fail("Mypy returned a malformed diagnostic")
        severity = str(item.get("severity") or "error")
        if severity != "error":
            continue
        path = _relative_path(item.get("file", "unknown"), root)
        rule = str(item.get("code") or "unknown")
        counts[(path, rule, severity)] += 1
        diagnostics.append(
            _static_diagnostic_evidence(
                tool="mypy",
                version=version,
                path=path,
                rule=rule,
                severity=severity,
                message=item.get("message"),
                root=root,
                anchor=_static_anchor(
                    item.get("line"),
                    item.get("column"),
                    item.get("end_line"),
                    item.get("end_column"),
                ),
                symbol=_optional_static_symbol(item),
            )
        )
    return StaticObservation("mypy", version, counts, tuple(diagnostics))


def _pyright_command() -> tuple[Path, dict[str, str]]:
    environment = dict(os.environ)
    configured = environment.get("NEOCORTEX_PYRIGHT")
    candidates = [Path(configured)] if configured else []
    discovered = shutil.which("pyright")
    if discovered:
        candidates.append(Path(discovered))
    suffix = ".cmd" if os.name == "nt" else ""
    candidates.append(
        Path(sys.prefix) / "tools" / "pyright" / "node_modules" / ".bin" / f"pyright{suffix}"
    )
    executable = next((item.resolve() for item in candidates if item.is_file()), None)
    if executable is None:
        _fail("Pyright is unavailable; set NEOCORTEX_PYRIGHT or install the canonical tool")
    node_bin = Path(sys.prefix) / "tools" / "node" / ("" if os.name == "nt" else "bin")
    if node_bin.is_dir():
        environment["PATH"] = os.pathsep.join((os.fspath(node_bin), environment.get("PATH", "")))
    return executable, environment


def _pyright_config_payload(root: Path) -> dict[str, object]:
    """Bind Pyright to the project policy and this interpreter's installed packages."""

    try:
        with (root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        _fail(f"could not read Pyright project policy: {error}")
    raw_tool = project.get("tool")
    raw_pyright = raw_tool.get("pyright") if isinstance(raw_tool, Mapping) else None
    if not isinstance(raw_pyright, Mapping):
        _fail("pyproject.toml omits [tool.pyright]")
    if "extraPaths" in raw_pyright:
        _fail("[tool.pyright].extraPaths is owned by the canonical quality gate")
    package_paths: set[str] = set()
    for name in ("purelib", "platlib"):
        raw_path = sysconfig.get_path(name)
        if not raw_path:
            _fail(f"Python did not report its {name} package directory")
        package_path = Path(raw_path).resolve()
        if not package_path.is_dir():
            _fail(f"Python {name} package directory is unavailable: {package_path}")
        package_paths.add(os.fspath(package_path))
    return {
        **dict(raw_pyright),
        "extraPaths": [os.fspath(root), *sorted(package_paths)],
    }


def _pyright_observation(root: Path) -> StaticObservation:
    executable, environment = _pyright_command()
    version_run = _run_captured(
        (os.fspath(executable), "--version"),
        root=root,
        timeout=60,
        allowed_codes=frozenset({0}),
        environment=environment,
    )
    with tempfile.TemporaryDirectory(prefix="neocortex-pyright-policy-") as temporary:
        project = Path(temporary) / "pyrightconfig.json"
        project.write_text(
            json.dumps(_pyright_config_payload(root), sort_keys=True),
            encoding="utf-8",
        )
        completed = _run_captured(
            (
                os.fspath(executable),
                "--outputjson",
                "--pythonpath",
                sys.executable,
                "--project",
                os.fspath(project),
                *PRODUCTION_TYPE_TARGETS,
            ),
            root=root,
            timeout=STATIC_TIMEOUT_SECONDS,
            allowed_codes=frozenset({0, 1}),
            environment=environment,
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _fail(f"Pyright returned invalid JSON: {error}")
    if not isinstance(payload, Mapping):
        _fail("Pyright returned a non-object payload")
    diagnostics = payload.get("generalDiagnostics")
    if not isinstance(diagnostics, list):
        _fail("Pyright diagnostics are missing")
    version = _semantic_version(version_run.stdout, "Pyright")
    counts: Counter[tuple[str, str, str]] = Counter()
    evidence: list[StaticDiagnosticEvidence] = []
    for item in diagnostics:
        if not isinstance(item, Mapping):
            _fail("Pyright returned a malformed diagnostic")
        severity = str(item.get("severity") or "unknown")
        if severity not in {"error", "warning"}:
            continue
        path = _relative_path(item.get("file", "unknown"), root)
        rule = str(item.get("rule") or "unknown")
        counts[(path, rule, severity)] += 1
        raw_range = item.get("range")
        range_mapping = raw_range if isinstance(raw_range, Mapping) else {}
        evidence.append(
            _static_diagnostic_evidence(
                tool="pyright",
                version=version,
                path=path,
                rule=rule,
                severity=severity,
                message=item.get("message"),
                root=root,
                anchor=_nested_static_anchor(
                    range_mapping.get("start"),
                    range_mapping.get("end"),
                    line_key="line",
                    column_key="character",
                    offset=1,
                ),
                symbol=_optional_static_symbol(item),
            )
        )
    return StaticObservation("pyright", version, counts, tuple(evidence))


def collect_static_observations(root: Path) -> tuple[StaticObservation, ...]:
    return (_ruff_observation(root), _mypy_observation(root), _pyright_observation(root))


def baseline_payload(observations: Sequence[StaticObservation]) -> dict[str, object]:
    return {
        "schema": BASELINE_SCHEMA,
        "scope": {
            "ruff": "complete repository Python tree",
            "mypy": list(PRODUCTION_TYPE_TARGETS),
            "pyright": list(PRODUCTION_TYPE_TARGETS),
        },
        "tools": {
            observation.tool: {
                "version": observation.version,
                "total": observation.total,
                "by_path_rule": [
                    {
                        "path": path,
                        "rule": rule,
                        "severity": severity,
                        "count": count,
                    }
                    for (path, rule, severity), count in sorted(observation.counts.items())
                ],
            }
            for observation in observations
        },
    }


def static_diagnostic_shadow_payload(
    observations: Sequence[StaticObservation],
) -> dict[str, object]:
    """Return non-enforcing diagnostic identities alongside the count baseline."""

    tools: dict[str, object] = {}
    for observation in sorted(observations, key=lambda item: item.tool):
        fingerprint_counts = Counter(item.fingerprint for item in observation.diagnostics)
        evidence_by_fingerprint: dict[str, StaticDiagnosticEvidence] = {}
        for item in observation.diagnostics:
            evidence_by_fingerprint.setdefault(item.fingerprint, item)
        entries = []
        for fingerprint, count in sorted(fingerprint_counts.items()):
            item = evidence_by_fingerprint[fingerprint]
            entries.append(
                {
                    "fingerprint": fingerprint,
                    "count": count,
                    "path": item.path,
                    "rule": item.rule,
                    "severity": item.severity,
                    "message_sha256": hashlib.sha256(
                        item.normalized_message.encode("utf-8")
                    ).hexdigest(),
                    "anchor": item.anchor,
                    "symbol": item.symbol,
                }
            )
        encoded_entries = json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        tools[observation.tool] = {
            "version": observation.version,
            "count_baseline_total": observation.total,
            "fingerprinted_diagnostic_total": len(observation.diagnostics),
            "unique_fingerprints": len(entries),
            "coverage": (
                "complete" if len(observation.diagnostics) == observation.total else "partial"
            ),
            "manifest_sha256": hashlib.sha256(encoded_entries).hexdigest(),
            "diagnostics": entries,
        }
    return {
        "schema": STATIC_DIAGNOSTIC_SHADOW_SCHEMA,
        "mode": "shadow",
        "enforced": False,
        "fingerprint_algorithm": STATIC_DIAGNOSTIC_FINGERPRINT_ALGORITHM,
        "tools": tools,
    }


def _baseline_counts(raw: object, tool: str) -> tuple[str, int, Counter[tuple[str, str, str]]]:
    if not isinstance(raw, Mapping):
        _fail(f"baseline for {tool} is missing")
    version = raw.get("version")
    total = raw.get("total")
    entries = raw.get("by_path_rule")
    if not isinstance(version, str) or isinstance(total, bool) or not isinstance(total, int):
        _fail(f"baseline metadata for {tool} is malformed")
    if not isinstance(entries, list):
        _fail(f"baseline entries for {tool} are malformed")
    counts: Counter[tuple[str, str, str]] = Counter()
    for item in entries:
        if not isinstance(item, Mapping):
            _fail(f"baseline entry for {tool} is malformed")
        path = item.get("path")
        rule = item.get("rule")
        severity = item.get("severity")
        count = item.get("count")
        if (
            not isinstance(path, str)
            or not isinstance(rule, str)
            or not isinstance(severity, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            _fail(f"baseline entry for {tool} is malformed")
        key = (path, rule, severity)
        if key in counts:
            _fail(f"baseline entry for {tool} is duplicated: {key}")
        counts[key] = count
    if sum(counts.values()) != total:
        _fail(f"baseline total for {tool} disagrees with its entries")
    return version, total, counts


def compare_static_observations(
    observations: Sequence[StaticObservation], baseline: Mapping[str, object]
) -> dict[str, int]:
    """Enforce only the established version and count buckets, never shadow identities."""

    if baseline.get("schema") != BASELINE_SCHEMA:
        _fail("static baseline has an unsupported schema")
    raw_tools = baseline.get("tools")
    if not isinstance(raw_tools, Mapping):
        _fail("static baseline omits tools")
    observed_names = {item.tool for item in observations}
    if observed_names != {"ruff", "mypy", "pyright"}:
        _fail(f"static observations are incomplete: {sorted(observed_names)}")
    totals: dict[str, int] = {}
    regressions: list[str] = []
    for observation in observations:
        expected_version, expected_total, expected_counts = _baseline_counts(
            raw_tools.get(observation.tool), observation.tool
        )
        if observation.version != expected_version:
            regressions.append(
                f"{observation.tool}: version {observation.version} != {expected_version}"
            )
        if observation.total > expected_total:
            regressions.append(f"{observation.tool}: total {observation.total} > {expected_total}")
        for key, count in sorted(observation.counts.items()):
            expected = expected_counts.get(key, 0)
            if count > expected:
                path, rule, severity = key
                regressions.append(
                    f"{observation.tool}: {path} {rule}/{severity} {count} > {expected}"
                )
        totals[observation.tool] = observation.total
    if regressions:
        preview = "; ".join(regressions[:20])
        suffix = "" if len(regressions) <= 20 else f"; and {len(regressions) - 20} more"
        _fail(f"static no-regression baseline failed: {preview}{suffix}")
    return totals


def _load_json_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"could not read {label} {path}: {error}")
    if not isinstance(payload, Mapping):
        _fail(f"{label} must contain a JSON object")
    return payload


def build_production_source_inventory_payload(root: Path) -> dict[str, object]:
    files = discover_production_sources(root)
    digest = hashlib.sha256()
    total_bytes = 0
    for relative in files:
        content = _portable_python_bytes(root / relative)
        size = len(content)
        total_bytes += size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\n")
    return {
        "schema": "neocortex.quality-gate-production-source-inventory/v1",
        "file_count": len(files),
        "total_bytes": total_bytes,
        "hash_algorithm": "sha256(path,lf-size,sha256(lf-content))-v1",
        "sha256": digest.hexdigest(),
        "files": list(files),
    }


def _coverage_environment() -> dict[str, str]:
    return {
        "implementation": platform.python_implementation(),
        "python_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": platform.system(),
    }


def _coverage_observation(payload: Mapping[str, object]) -> tuple[str, dict[str, dict[str, int]]]:
    meta = payload.get("meta")
    totals = payload.get("totals")
    if not isinstance(meta, Mapping) or not isinstance(totals, Mapping):
        _fail("coverage report omits meta or totals")
    version = meta.get("version")
    if not isinstance(version, str) or not version:
        _fail("coverage report omits the Coverage version")
    if meta.get("branch_coverage") is not True:
        _fail("coverage report is not branch-aware")
    metrics: dict[str, dict[str, int]] = {}
    for label, covered_key, total_key in (
        ("lines", "covered_lines", "num_statements"),
        ("branches", "covered_branches", "num_branches"),
    ):
        covered = totals.get(covered_key)
        total = totals.get(total_key)
        if (
            isinstance(covered, bool)
            or not isinstance(covered, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total <= 0
            or covered < 0
            or covered > total
        ):
            _fail(f"coverage report has malformed {label} totals")
        metrics[label] = {"covered": covered, "total": total}
    return version, metrics


def _normalized_coverage_path(raw: object, root: Path) -> str:
    if not isinstance(raw, str) or not raw:
        _fail("coverage report contains a malformed file path")
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            _fail(f"coverage report contains a path outside the repository: {candidate}")
    normalized = PurePosixPath(raw.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        _fail(f"coverage report contains an unsafe relative path: {raw}")
    return normalized.as_posix()


def validate_coverage_source_inventory(
    payload: Mapping[str, object], root: Path
) -> dict[str, object]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, Mapping) or not raw_files:
        _fail("coverage report contains no file inventory")
    observed = {_normalized_coverage_path(raw, root) for raw in raw_files}
    expected = set(discover_production_sources(root))
    if observed != expected:
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        _fail(
            "coverage source inventory is incomplete: "
            f"missing={missing[:10]!r}; unexpected={unexpected[:10]!r}"
        )
    return build_production_source_inventory_payload(root)


def coverage_baseline_payload(
    report: Mapping[str, object],
    *,
    test_inventory: Mapping[str, object],
    source_inventory: Mapping[str, object],
) -> dict[str, object]:
    version, metrics = _coverage_observation(report)
    return {
        "schema": COVERAGE_BASELINE_SCHEMA,
        "scope": {
            "branch": True,
            "sources": list(PRODUCTION_COVERAGE_SOURCES),
            "environment": _coverage_environment(),
        },
        "tool": {"name": "coverage", "version": version},
        "approved": metrics,
        "approved_inventory": {
            "test_inventory": {
                "file_count": test_inventory["file_count"],
                "hash_algorithm": test_inventory["hash_algorithm"],
                "sha256": test_inventory["sha256"],
                "files": test_inventory["files"],
            },
            "production_source_inventory": {
                "file_count": source_inventory["file_count"],
                "hash_algorithm": source_inventory["hash_algorithm"],
                "sha256": source_inventory["sha256"],
                "files": source_inventory["files"],
            },
        },
    }


def _baseline_coverage_metric(raw: object, label: str) -> tuple[int, int]:
    if not isinstance(raw, Mapping):
        _fail(f"coverage baseline omits {label}")
    covered = raw.get("covered")
    total = raw.get("total")
    if (
        isinstance(covered, bool)
        or not isinstance(covered, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total <= 0
        or covered < 0
        or covered > total
    ):
        _fail(f"coverage baseline has malformed {label} totals")
    return covered, total


def _coverage_inventory_paths(raw: object, label: str) -> frozenset[str]:
    if not isinstance(raw, Mapping):
        _fail(f"coverage baseline omits {label} inventory")
    file_count = raw.get("file_count")
    algorithm = raw.get("hash_algorithm")
    digest = raw.get("sha256")
    files = raw.get("files")
    if (
        isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or file_count < 1
        or algorithm != "sha256(path,lf-size,sha256(lf-content))-v1"
        or re.fullmatch(r"[0-9a-f]{64}", str(digest or "")) is None
        or not isinstance(files, list)
        or any(not isinstance(path, str) or not path for path in files)
        or files != sorted(files)
        or len(files) != len(set(files))
        or len(files) != file_count
    ):
        _fail(f"coverage {label} inventory is malformed")
    return frozenset(cast(list[str], files))


def compare_coverage_report(
    report: Mapping[str, object],
    baseline: Mapping[str, object],
    *,
    test_inventory: Mapping[str, object],
    source_inventory: Mapping[str, object],
) -> dict[str, object]:
    if baseline.get("schema") != COVERAGE_BASELINE_SCHEMA:
        _fail("coverage baseline has an unsupported schema")
    scope = baseline.get("scope")
    expected_scope = {
        "branch": True,
        "sources": list(PRODUCTION_COVERAGE_SOURCES),
        "environment": _coverage_environment(),
    }
    if scope != expected_scope:
        _fail("coverage baseline scope or canonical environment does not match this run")
    tool = baseline.get("tool")
    approved = baseline.get("approved")
    approved_inventory = baseline.get("approved_inventory")
    if (
        not isinstance(tool, Mapping)
        or not isinstance(approved, Mapping)
        or not isinstance(approved_inventory, Mapping)
    ):
        _fail("coverage baseline omits tool, approved metrics, or approved inventory")
    version, current = _coverage_observation(report)
    if tool.get("name") != "coverage" or tool.get("version") != version:
        _fail(f"coverage version {version} does not match baseline {tool.get('version')}")
    regressions: list[str] = []
    inventory_summary: dict[str, dict[str, int]] = {}
    for label, current_raw in (
        ("test", test_inventory),
        ("production source", source_inventory),
    ):
        baseline_key = "test_inventory" if label == "test" else "production_source_inventory"
        approved_paths = _coverage_inventory_paths(approved_inventory.get(baseline_key), label)
        current_paths = _coverage_inventory_paths(current_raw, label)
        removed = sorted(approved_paths - current_paths)
        inventory_summary[label.replace(" ", "_")] = {
            "approved_paths": len(approved_paths),
            "current_paths": len(current_paths),
            "added_paths": len(current_paths - approved_paths),
        }
        if removed:
            regressions.append(f"{label} paths removed: {removed[:20]!r}")
    for label in ("lines", "branches"):
        approved_covered, approved_total = _baseline_coverage_metric(approved.get(label), label)
        observed = current[label]
        if observed["covered"] * approved_total < approved_covered * observed["total"]:
            regressions.append(
                f"{label} {observed['covered']}/{observed['total']} < "
                f"{approved_covered}/{approved_total}"
            )
    if regressions:
        _fail(f"coverage no-regression baseline failed: {'; '.join(regressions)}")
    return {
        "coverage_version": version,
        "inventory_ratchet": inventory_summary,
        "metrics": current,
    }


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def run_coverage_gate(
    root: Path,
    baseline_path: Path,
    *,
    data_file: Path,
    report_path: Path,
    basetemp: Path | None,
    write_baseline: bool = False,
) -> dict[str, object]:
    test_inventory = build_test_inventory_payload(root, 1)
    initial_source_inventory = build_production_source_inventory_payload(root)
    selected = _selected_test_paths(root, 1, 0)
    resolved_data = data_file.expanduser().resolve()
    resolved_report = report_path.expanduser().resolve()
    resolved_data.parent.mkdir(parents=True, exist_ok=True)
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        "--branch",
        f"--source={','.join(PRODUCTION_COVERAGE_SOURCES)}",
        f"--data-file={resolved_data}",
        *_pytest_command(selected, basetemp=basetemp)[1:],
    ]
    print(
        f"coverage suite: {test_inventory['file_count']} dynamically discovered files",
        flush=True,
    )
    test_result = subprocess.call(
        command,
        cwd=root,
        env=_pytest_environment(basetemp),
    )
    temporary_report = resolved_report.with_name(f".{resolved_report.name}.tmp")
    report_result = subprocess.call(
        (
            sys.executable,
            "-m",
            "coverage",
            "json",
            f"--data-file={resolved_data}",
            "--pretty-print",
            "-o",
            os.fspath(temporary_report),
        ),
        cwd=root,
    )
    if report_result:
        _fail(f"Coverage could not publish its JSON report (exit {report_result})")
    os.replace(temporary_report, resolved_report)
    report = _load_json_object(resolved_report, "coverage report")
    source_inventory = validate_coverage_source_inventory(report, root)
    final_test_inventory = build_test_inventory_payload(root, 1)
    if final_test_inventory["sha256"] != test_inventory["sha256"]:
        _fail("test inventory changed while coverage was running")
    if source_inventory["sha256"] != initial_source_inventory["sha256"]:
        _fail("production source inventory changed while coverage was running")
    if test_result:
        _fail(f"coverage test suite failed (pytest exit {test_result})")
    if write_baseline:
        _write_json_atomic(
            baseline_path,
            coverage_baseline_payload(
                report,
                test_inventory=test_inventory,
                source_inventory=source_inventory,
            ),
        )
        summary = compare_coverage_report(
            report,
            _load_json_object(baseline_path, "coverage baseline"),
            test_inventory=test_inventory,
            source_inventory=source_inventory,
        )
    else:
        summary = compare_coverage_report(
            report,
            _load_json_object(baseline_path, "coverage baseline"),
            test_inventory=test_inventory,
            source_inventory=source_inventory,
        )
    return {
        **summary,
        "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "report": os.fspath(resolved_report),
        "report_sha256": hashlib.sha256(resolved_report.read_bytes()).hexdigest(),
        "test_inventory": {
            "file_count": test_inventory["file_count"],
            "hash_algorithm": test_inventory["hash_algorithm"],
            "sha256": test_inventory["sha256"],
        },
        "production_source_inventory": {
            "file_count": source_inventory["file_count"],
            "hash_algorithm": source_inventory["hash_algorithm"],
            "sha256": source_inventory["sha256"],
        },
    }


def write_static_baseline(path: Path, observations: Sequence[StaticObservation]) -> None:
    _write_json_atomic(path, baseline_payload(observations))


def run_static_gate(
    root: Path, baseline_path: Path, *, write_baseline: bool = False
) -> dict[str, object]:
    observations = collect_static_observations(root)
    if write_baseline:
        write_static_baseline(baseline_path, observations)
        totals = {item.tool: item.total for item in observations}
    else:
        baseline = _load_json_object(baseline_path, "static baseline")
        totals = compare_static_observations(observations, baseline)
    return {
        "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "tools": {
            item.tool: {"version": item.version, "total": totals[item.tool]}
            for item in observations
        },
        "diagnostic_shadow": static_diagnostic_shadow_payload(observations),
    }


def _normalized_package_name(value: object) -> str:
    return re.sub(r"[-_.]+", "-", str(value).strip().lower())


def evaluate_audit_payload(
    payload: Mapping[str, object],
    *,
    allowed_skips: frozenset[str],
    allowed_vulnerabilities: frozenset[tuple[str, str, str]],
) -> dict[str, int]:
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        _fail("pip-audit returned no dependency inventory")
    vulnerabilities: set[tuple[str, str, str]] = set()
    accepted_vulnerabilities: set[tuple[str, str, str]] = set()
    unexpected_vulnerabilities: set[tuple[str, str, str]] = set()
    unexpected_skips: list[str] = []
    skipped = 0
    for item in dependencies:
        if not isinstance(item, Mapping):
            _fail("pip-audit returned a malformed dependency")
        name = _normalized_package_name(item.get("name") or "unknown")
        version = str(item.get("version") or "unknown")
        skip_reason = item.get("skip_reason")
        if skip_reason:
            skipped += 1
            if name not in allowed_skips:
                unexpected_skips.append(name)
        raw_vulnerabilities = item.get("vulns", [])
        if not isinstance(raw_vulnerabilities, list):
            _fail(f"pip-audit vulnerabilities are malformed for {name}")
        for vulnerability in raw_vulnerabilities:
            if not isinstance(vulnerability, Mapping):
                _fail(f"pip-audit vulnerability is malformed for {name}")
            identifier = str(vulnerability.get("id") or f"unknown:{name}")
            aliases = vulnerability.get("aliases", [])
            if not isinstance(aliases, list) or not all(
                isinstance(alias, str) for alias in aliases
            ):
                _fail(f"pip-audit vulnerability aliases are malformed for {name}")
            identifiers = {identifier, *aliases}
            primary = (name, version, identifier)
            vulnerabilities.add(primary)
            matches = {
                allowed
                for allowed in allowed_vulnerabilities
                if allowed[0] == name and allowed[1] == version and allowed[2] in identifiers
            }
            if matches:
                accepted_vulnerabilities.update(matches)
            else:
                unexpected_vulnerabilities.add(primary)
    if unexpected_skips:
        _fail(f"pip-audit unexpectedly skipped: {sorted(set(unexpected_skips))}")
    if unexpected_vulnerabilities:
        _fail(f"pip-audit found unaccepted vulnerabilities: {sorted(unexpected_vulnerabilities)}")
    return {
        "dependencies": len(dependencies),
        "skipped_local_projects": skipped,
        "vulnerabilities": len(vulnerabilities),
        "accepted_vulnerabilities": len(accepted_vulnerabilities),
    }


def evaluate_tool_audit_payload(
    payload: Mapping[str, object],
    exceptions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    expected: set[tuple[str, str, str]] = set()
    for item in exceptions:
        package = item.get("package")
        version = item.get("version")
        identifier = item.get("id")
        if not all(isinstance(value, str) and value for value in (package, version, identifier)):
            _fail("tool-runtime vulnerability policy is malformed")
        expected.add((_normalized_package_name(package), str(version), str(identifier)))
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        _fail("tool-runtime pip-audit returned no dependency inventory")
    matched: set[tuple[str, str, str]] = set()
    primary_ids: set[str] = set()
    unexpected: set[tuple[str, str, str]] = set()
    for raw_dependency in dependencies:
        if not isinstance(raw_dependency, Mapping):
            _fail("tool-runtime pip-audit returned a malformed dependency")
        package = _normalized_package_name(raw_dependency.get("name") or "unknown")
        version = str(raw_dependency.get("version") or "unknown")
        raw_vulnerabilities = raw_dependency.get("vulns", [])
        if not isinstance(raw_vulnerabilities, list):
            _fail("tool-runtime pip-audit returned malformed vulnerabilities")
        for raw_vulnerability in raw_vulnerabilities:
            if not isinstance(raw_vulnerability, Mapping):
                _fail("tool-runtime pip-audit returned a malformed vulnerability")
            primary = str(raw_vulnerability.get("id") or "unknown")
            aliases = raw_vulnerability.get("aliases", [])
            if not isinstance(aliases, list) or not all(
                isinstance(alias, str) for alias in aliases
            ):
                _fail("tool-runtime pip-audit returned malformed vulnerability aliases")
            identifiers = {primary, *aliases}
            matches = {
                item
                for item in expected
                if item[0] == package and item[1] == version and item[2] in identifiers
            }
            primary_ids.add(primary)
            if len(matches) == 1:
                matched.update(matches)
            else:
                unexpected.add((package, version, primary))
    if unexpected:
        _fail(f"tool-runtime pip-audit found unaccepted vulnerabilities: {sorted(unexpected)}")
    missing = expected - matched
    if missing:
        _fail(f"tool-runtime pip-audit did not confirm policy exceptions: {sorted(missing)}")
    return {
        "dependencies": len(dependencies),
        "vulnerabilities": len(primary_ids),
        "primary_ids": sorted(primary_ids),
        "matched_policy_exceptions": len(matched),
    }


def _supply_policy(path: Path) -> Mapping[str, object]:
    policy = _load_json_object(path, "supply policy")
    if policy.get("schema") != "neocortex.quality-gate-supply-policy/v1":
        _fail("supply policy has an unsupported schema")
    return policy


def _main_runtime_policy(
    policy: Mapping[str, object],
) -> tuple[frozenset[str], frozenset[tuple[str, str, str]]]:
    raw_main = policy.get("main_runtime")
    if not isinstance(raw_main, Mapping):
        _fail("supply policy omits main_runtime")
    raw_skips = raw_main.get("allowed_skips")
    raw_vulnerabilities = raw_main.get("allowed_vulnerabilities")
    if not isinstance(raw_skips, list) or not all(isinstance(item, str) for item in raw_skips):
        _fail("main runtime allowed_skips policy is malformed")
    if not isinstance(raw_vulnerabilities, list):
        _fail("main runtime allowed_vulnerabilities policy is malformed")
    accepted: set[tuple[str, str, str]] = set()
    for item in raw_vulnerabilities:
        if not isinstance(item, Mapping):
            _fail("main runtime vulnerability exception is malformed")
        package = item.get("package")
        version = item.get("version")
        identifier = item.get("id")
        if not all(isinstance(value, str) and value for value in (package, version, identifier)):
            _fail("main runtime vulnerability exception is malformed")
        accepted.add((_normalized_package_name(package), str(version), str(identifier)))
    return (
        frozenset(_normalized_package_name(item) for item in raw_skips),
        frozenset(accepted),
    )


def _semgrep_tool_policy(policy: Mapping[str, object]) -> Mapping[str, object]:
    runtimes = policy.get("tool_runtimes")
    if not isinstance(runtimes, Mapping):
        _fail("supply policy omits tool_runtimes")
    semgrep = runtimes.get("semgrep")
    if not isinstance(semgrep, Mapping):
        _fail("supply policy omits the Semgrep tool runtime")
    return semgrep


def evaluate_tool_runtime_receipt(
    receipt: Mapping[str, object],
    policy: Mapping[str, object],
    *,
    today: date | None = None,
) -> dict[str, object]:
    """Validate Semgrep isolation and its exact, expiring MCP exceptions."""

    semgrep = _semgrep_tool_policy(policy)
    exact_fields = (
        "schema_version",
        "kind",
        "tool",
        "version",
        "scan_wrapper",
        "scan_wrapper_sha256",
        "constraints_filename",
        "constraints_sha256",
        "pip_bootstrap_version",
        "pip_bootstrap_filename",
        "pip_bootstrap_sha256",
    )
    mismatches = [field for field in exact_fields if receipt.get(field) != semgrep.get(field)]
    if mismatches:
        _fail(f"Semgrep receipt disagrees with policy fields: {mismatches}")
    python_executable = receipt.get("python_executable")
    allowed_executables = semgrep.get("python_executables")
    if (
        not isinstance(python_executable, str)
        or not isinstance(allowed_executables, list)
        or python_executable not in allowed_executables
    ):
        _fail("Semgrep receipt Python executable is not allowed by policy")
    if receipt.get("allowed_surfaces") != semgrep.get("allowed_surfaces"):
        _fail("Semgrep receipt allowed surfaces disagree with policy")
    if receipt.get("denied_console_entrypoints") != semgrep.get("denied_console_entrypoints"):
        _fail("Semgrep receipt denied entrypoints disagree with policy")
    required_packages = semgrep.get("required_packages")
    inventory = receipt.get("installed_packages")
    if not isinstance(required_packages, Mapping) or not isinstance(inventory, list):
        _fail("Semgrep receipt package inventory is malformed")
    observed_packages: dict[str, str] = {}
    for item in inventory:
        if not isinstance(item, Mapping):
            _fail("Semgrep receipt package inventory is malformed")
        name = item.get("name")
        version = item.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            _fail("Semgrep receipt package inventory is malformed")
        observed_packages[name] = version
    for name, version in required_packages.items():
        if observed_packages.get(str(name)) != version:
            _fail(f"Semgrep receipt package {name} disagrees with policy")
    raw_expected = semgrep.get("vulnerability_exceptions")
    raw_observed = receipt.get("vulnerability_exceptions")
    if not isinstance(raw_expected, list) or not isinstance(raw_observed, list):
        _fail("Semgrep vulnerability exceptions are malformed")
    canonical_expected = sorted(
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in raw_expected
    )
    canonical_observed = sorted(
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in raw_observed
    )
    if canonical_observed != canonical_expected:
        _fail("Semgrep receipt vulnerability exceptions differ from policy")
    current_date = date.today() if today is None else today
    for item in raw_observed:
        if not isinstance(item, Mapping):
            _fail("Semgrep vulnerability exception is malformed")
        if item.get("reachable") is not False:
            _fail("Semgrep vulnerability exception must remain unreachable")
        raw_expiry = item.get("expires")
        if not isinstance(raw_expiry, str):
            _fail("Semgrep vulnerability exception expiry is malformed")
        try:
            expiry = date.fromisoformat(raw_expiry)
        except ValueError as error:
            _fail(f"Semgrep vulnerability exception expiry is malformed: {error}")
        if expiry < current_date:
            _fail(f"Semgrep vulnerability exception expired on {expiry.isoformat()}")
    return {
        "tool": receipt["tool"],
        "version": receipt["version"],
        "isolation": "separate-venv-scan-only",
        "python_executable": python_executable,
        "scan_wrapper": receipt["scan_wrapper"],
        "accepted_vulnerability_exceptions": len(raw_observed),
        "earliest_expiry": min(str(item["expires"]) for item in raw_observed),
    }


def _validate_tool_receipt_path(
    root: Path,
    receipt_path: Path,
    policy: Mapping[str, object],
) -> dict[str, object]:
    receipt_path = receipt_path.expanduser().resolve()
    expected_parts = ("tools", "semgrep", "neocortex-tool-runtime.json")
    if tuple(receipt_path.parts[-3:]) != expected_parts:
        _fail("Semgrep receipt is not at tools/semgrep/neocortex-tool-runtime.json")
    receipt = _load_json_object(receipt_path, "Semgrep tool-runtime receipt")
    summary = evaluate_tool_runtime_receipt(receipt, policy)
    runtime_root = receipt_path.parents[2]
    helper = root / "tools" / "semgrep_tool_runtime.py"
    if not helper.is_file():
        _fail("Semgrep tool-runtime verifier is missing")
    _run_captured(
        (
            sys.executable,
            os.fspath(helper),
            "verify",
            "--runtime-root",
            os.fspath(runtime_root),
        ),
        root=root,
        timeout=AUDIT_TIMEOUT_SECONDS,
        allowed_codes=frozenset({0}),
    )
    tool_root = receipt_path.parent
    raw_python = receipt.get("python_executable")
    if not isinstance(raw_python, str):
        _fail("Semgrep receipt Python executable is malformed")
    tool_python = tool_root.joinpath(*PurePosixPath(raw_python).parts)
    purelib_run = _run_captured(
        (
            os.fspath(tool_python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ),
        root=root,
        timeout=60,
        allowed_codes=frozenset({0}),
    )
    purelib = Path(purelib_run.stdout.strip()).resolve()
    try:
        contained = os.path.commonpath((os.fspath(tool_root.resolve()), os.fspath(purelib)))
    except ValueError:
        contained = ""
    if contained != os.fspath(tool_root.resolve()) or not purelib.is_dir():
        _fail("Semgrep tool-runtime site-packages is unavailable")
    audit_run = _run_captured(
        (
            sys.executable,
            "-m",
            "pip_audit",
            "--path",
            os.fspath(purelib),
            "--format=json",
            "--progress-spinner=off",
        ),
        root=root,
        timeout=AUDIT_TIMEOUT_SECONDS,
        allowed_codes=frozenset({0, 1}),
    )
    try:
        audit_payload = json.loads(audit_run.stdout)
    except json.JSONDecodeError as error:
        _fail(f"tool-runtime pip-audit returned invalid JSON: {error}")
    if not isinstance(audit_payload, Mapping):
        _fail("tool-runtime pip-audit returned a non-object payload")
    raw_exceptions = _semgrep_tool_policy(policy).get("vulnerability_exceptions")
    if not isinstance(raw_exceptions, list) or not all(
        isinstance(item, Mapping) for item in raw_exceptions
    ):
        _fail("Semgrep tool-runtime exception policy is malformed")
    tool_audit = evaluate_tool_audit_payload(
        audit_payload,
        cast(list[Mapping[str, object]], raw_exceptions),
    )
    version_run = _run_captured(
        (sys.executable, "-m", "pip_audit", "--version"),
        root=root,
        timeout=60,
        allowed_codes=frozenset({0}),
    )
    tool_audit["pip_audit_version"] = _semantic_version(version_run.stdout, "pip-audit")
    summary["audit"] = tool_audit
    receipt_bytes = receipt_path.read_bytes()
    summary["receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
    summary["status"] = "passed"
    return summary


def run_supply_chain_gate(
    root: Path,
    policy_path: Path,
    *,
    tool_receipt: Path | None = None,
) -> dict[str, object]:
    policy = _supply_policy(policy_path)
    allowed_skips, allowed_vulnerabilities = _main_runtime_policy(policy)
    version_run = _run_captured(
        (sys.executable, "-m", "pip_audit", "--version"),
        root=root,
        timeout=60,
        allowed_codes=frozenset({0}),
    )
    completed = _run_captured(
        (
            sys.executable,
            "-m",
            "pip_audit",
            "--local",
            "--format=json",
            "--progress-spinner=off",
        ),
        root=root,
        timeout=AUDIT_TIMEOUT_SECONDS,
        allowed_codes=frozenset({0, 1}),
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _fail(f"pip-audit returned invalid JSON: {error}")
    if not isinstance(payload, Mapping):
        _fail("pip-audit returned a non-object payload")
    runtime_summary: dict[str, object] = dict(
        evaluate_audit_payload(
            payload,
            allowed_skips=allowed_skips,
            allowed_vulnerabilities=allowed_vulnerabilities,
        )
    )
    runtime_summary["pip_audit_version"] = _semantic_version(version_run.stdout, "pip-audit")
    tool_summary: dict[str, object]
    if tool_receipt is None:
        tool_summary = {"status": "not_evaluated", "reason": "receipt_not_supplied"}
    else:
        tool_summary = _validate_tool_receipt_path(root, tool_receipt, policy)
    return {
        "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "main_runtime": runtime_summary,
        "semgrep_tool_runtime": tool_summary,
    }


def write_gate_receipt(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically write an explicitly requested pre-push evidence receipt."""

    _write_json_atomic(path, payload)


def require_git_snapshot(root: Path, expected_sha: str) -> dict[str, str]:
    """Fail unless HEAD and the complete worktree still match one bound SHA."""

    if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
        _fail(f"expected Git SHA is malformed: {expected_sha!r}")
    observed = _run_captured(
        ("git", "rev-parse", "HEAD"),
        root=root,
        timeout=30,
        allowed_codes=frozenset({0}),
    ).stdout.strip()
    if observed != expected_sha:
        _fail(f"Git HEAD changed during the gate: {observed} != {expected_sha}")
    status = _run_captured(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        root=root,
        timeout=30,
        allowed_codes=frozenset({0}),
    ).stdout.strip()
    if status:
        _fail("quality gate requires a clean worktree with no untracked files")
    return {"sha": observed, "worktree": "clean"}


def _git_pre_push_identity(root: Path) -> str:
    branch = _run_captured(
        ("git", "symbolic-ref", "--short", "HEAD"),
        root=root,
        timeout=30,
        allowed_codes=frozenset({0}),
    ).stdout.strip()
    if branch != "main":
        _fail(f"pre-push gate requires main, observed {branch!r}")
    sha = _run_captured(
        ("git", "rev-parse", "HEAD"),
        root=root,
        timeout=30,
        allowed_codes=frozenset({0}),
    ).stdout.strip()
    require_git_snapshot(root, sha)
    return sha


def _print_summary(label: str, payload: Mapping[str, object]) -> None:
    print(f"{label}: {json.dumps(payload, sort_keys=True, ensure_ascii=False)}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", allow_abbrev=False)
    inventory.add_argument("--shard-count", type=int, default=2)
    inventory.add_argument("--json", action="store_true")

    tests = subparsers.add_parser("tests", allow_abbrev=False)
    tests.add_argument("--shard-count", type=int, required=True)
    tests.add_argument("--shard-index", type=int, required=True)
    tests.add_argument("--basetemp", type=Path)
    tests.add_argument("pytest_arguments", nargs=argparse.REMAINDER)

    subparsers.add_parser("architecture", allow_abbrev=False)

    static = subparsers.add_parser("static", allow_abbrev=False)
    static.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    static.add_argument("--write-baseline", action="store_true")

    coverage = subparsers.add_parser("coverage", allow_abbrev=False)
    coverage.add_argument("--baseline", type=Path, default=DEFAULT_COVERAGE_BASELINE)
    coverage.add_argument("--data-file", type=Path, required=True)
    coverage.add_argument("--report", type=Path, required=True)
    coverage.add_argument("--basetemp", type=Path)
    coverage.add_argument("--write-baseline", action="store_true")

    supply_chain = subparsers.add_parser("supply-chain", allow_abbrev=False)
    supply_chain.add_argument("--policy", type=Path, default=DEFAULT_SUPPLY_POLICY)
    supply_chain.add_argument("--tool-receipt", type=Path)

    git_snapshot = subparsers.add_parser("git-snapshot", allow_abbrev=False)
    git_snapshot.add_argument("--sha", required=True)

    wheel_smoke = subparsers.add_parser("wheel-smoke", allow_abbrev=False)
    wheel_smoke.add_argument("--probe-directory", type=Path, required=True)

    pre_push = subparsers.add_parser("pre-push", allow_abbrev=False)
    pre_push.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    pre_push.add_argument("--coverage-baseline", type=Path, default=DEFAULT_COVERAGE_BASELINE)
    pre_push.add_argument("--supply-policy", type=Path, default=DEFAULT_SUPPLY_POLICY)
    pre_push.add_argument("--tool-receipt", type=Path)
    pre_push.add_argument("--basetemp", type=Path)
    pre_push.add_argument("--receipt", type=Path)
    return parser


def _pre_push_receipt_payload(
    *,
    root: Path,
    sha: str,
    inventory: Mapping[str, object],
    architecture: Mapping[str, object],
    static_summary: Mapping[str, object],
    supply_chain: Mapping[str, object],
    coverage_summary: Mapping[str, object],
    baseline_path: Path,
    coverage_baseline_path: Path,
    supply_policy: Path,
    tool_receipt: Path,
    basetemp: Path | None,
    coverage_data_file: Path,
    coverage_report: Path,
    post_coverage_snapshot: Mapping[str, object],
    final_snapshot: Mapping[str, object],
) -> dict[str, object]:
    receipt_coverage = dict(coverage_summary)
    receipt_coverage.pop("report", None)
    receipt_coverage["report_lifecycle"] = "ephemeral_verified_not_retained"
    coverage_command = [
        sys.executable,
        "tools/quality_gate.py",
        "coverage",
        "--baseline",
        os.fspath(coverage_baseline_path),
        "--data-file",
        os.fspath(coverage_data_file),
        "--report",
        os.fspath(coverage_report),
    ]
    if basetemp is not None:
        coverage_command.extend(("--basetemp", os.fspath(basetemp)))
    return {
        "schema": "neocortex.quality-gate-receipt/v1",
        "created_at": datetime.now(UTC).isoformat(),
        "repository": {"root": os.fspath(root), "sha": sha, "branch": "main"},
        "test_inventory": {
            "file_count": inventory["file_count"],
            "total_bytes": inventory["total_bytes"],
            "hash_algorithm": inventory["hash_algorithm"],
            "sha256": inventory["sha256"],
        },
        "gates": {
            "architecture": {
                "command": [sys.executable, "tools/quality_gate.py", "architecture"],
                "result": "passed",
                "evidence": architecture,
            },
            "static": {
                "command": [
                    sys.executable,
                    "tools/quality_gate.py",
                    "static",
                    "--baseline",
                    os.fspath(baseline_path),
                ],
                "result": "passed",
                "evidence": static_summary,
            },
            "supply_chain": {
                "command": [
                    sys.executable,
                    "tools/quality_gate.py",
                    "supply-chain",
                    "--policy",
                    os.fspath(supply_policy),
                    "--tool-receipt",
                    os.fspath(tool_receipt),
                ],
                "result": "passed",
                "evidence": supply_chain,
            },
            "coverage": {
                "command": coverage_command,
                "result": "passed",
                "evidence": receipt_coverage,
            },
            "post_coverage_git_snapshot": {
                "command": [
                    sys.executable,
                    "tools/quality_gate.py",
                    "git-snapshot",
                    "--sha",
                    sha,
                ],
                "result": "passed",
                "evidence": post_coverage_snapshot,
            },
            "final_git_snapshot": {
                "command": [
                    sys.executable,
                    "tools/quality_gate.py",
                    "git-snapshot",
                    "--sha",
                    sha,
                ],
                "result": "passed",
                "evidence": final_snapshot,
            },
        },
        "result": "passed",
    }


def _run_pre_push(namespace: argparse.Namespace, root: Path) -> int:
    sha = _git_pre_push_identity(root)
    inventory = build_test_inventory_payload(root, 1)
    _print_summary(
        "test inventory",
        {"file_count": inventory["file_count"], "sha256": inventory["sha256"]},
    )
    architecture = run_architecture_gate(root)
    _print_summary("live architecture", architecture)
    baseline_path = namespace.baseline.expanduser().resolve()
    static_summary = run_static_gate(root, baseline_path)
    _print_summary("static no-regression", static_summary)
    supply_policy = namespace.supply_policy.expanduser().resolve()
    tool_receipt = namespace.tool_receipt or (
        Path(sys.prefix) / "tools" / "semgrep" / "neocortex-tool-runtime.json"
    )
    tool_receipt = tool_receipt.expanduser().resolve()
    supply_chain = run_supply_chain_gate(
        root,
        supply_policy,
        tool_receipt=tool_receipt,
    )
    _print_summary("supply chain", supply_chain)
    coverage_baseline_path = namespace.coverage_baseline.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="neocortex-quality-gate-") as temporary:
        coverage_data_file = Path(temporary) / ".coverage"
        coverage_report = Path(temporary) / "coverage.json"
        coverage_summary = run_coverage_gate(
            root,
            coverage_baseline_path,
            data_file=coverage_data_file,
            report_path=coverage_report,
            basetemp=namespace.basetemp,
        )
        _print_summary("branch coverage no-regression", coverage_summary)
        post_coverage_snapshot = require_git_snapshot(root, sha)
        _print_summary("post-coverage Git snapshot", post_coverage_snapshot)
        destination: Path | None = None
        if namespace.receipt is not None:
            destination = namespace.receipt.expanduser().resolve()
            if destination.is_relative_to(root):
                _fail("pre-push receipt must be written outside the repository")
        final_snapshot = require_git_snapshot(root, sha)
        _print_summary("final Git snapshot", final_snapshot)
        if destination is not None:
            receipt = _pre_push_receipt_payload(
                root=root,
                sha=sha,
                inventory=inventory,
                architecture=architecture,
                static_summary=static_summary,
                supply_chain=supply_chain,
                coverage_summary=coverage_summary,
                baseline_path=baseline_path,
                coverage_baseline_path=coverage_baseline_path,
                supply_policy=supply_policy,
                tool_receipt=tool_receipt,
                basetemp=namespace.basetemp,
                coverage_data_file=coverage_data_file,
                coverage_report=coverage_report,
                post_coverage_snapshot=post_coverage_snapshot,
                final_snapshot=final_snapshot,
            )
            write_gate_receipt(destination, receipt)
            print(f"pre-push receipt: {destination}")
        _print_summary("pre-push gate", {"sha": sha, "status": "passed"})
    return 0


def _run_command(namespace: argparse.Namespace, root: Path) -> int:
    if namespace.command == "inventory":
        payload = build_test_inventory_payload(root, namespace.shard_count)
        if namespace.json:
            print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        else:
            shards = cast(list[dict[str, object]], payload["shards"])
            _print_summary(
                "test inventory",
                {
                    "file_count": payload["file_count"],
                    "total_bytes": payload["total_bytes"],
                    "sha256": payload["sha256"],
                    "shards": [
                        {
                            "index": item["index"],
                            "file_count": item["file_count"],
                            "total_bytes": item["total_bytes"],
                        }
                        for item in shards
                    ],
                },
            )
        return 0
    if namespace.command == "tests":
        pytest_arguments = list(namespace.pytest_arguments)
        if pytest_arguments[:1] == ["--"]:
            pytest_arguments.pop(0)
        return run_test_shard(
            root,
            shard_count=namespace.shard_count,
            shard_index=namespace.shard_index,
            basetemp=namespace.basetemp,
            pytest_arguments=pytest_arguments,
        )
    if namespace.command == "architecture":
        _print_summary("live architecture", run_architecture_gate(root))
        return 0
    if namespace.command == "static":
        summary = run_static_gate(
            root,
            namespace.baseline.expanduser().resolve(),
            write_baseline=namespace.write_baseline,
        )
        label = "static baseline written" if namespace.write_baseline else "static no-regression"
        _print_summary(label, summary)
        return 0
    if namespace.command == "coverage":
        summary = run_coverage_gate(
            root,
            namespace.baseline.expanduser().resolve(),
            data_file=namespace.data_file,
            report_path=namespace.report,
            basetemp=namespace.basetemp,
            write_baseline=namespace.write_baseline,
        )
        label = (
            "coverage baseline written"
            if namespace.write_baseline
            else "branch coverage no-regression"
        )
        _print_summary(label, summary)
        return 0
    if namespace.command == "supply-chain":
        summary = run_supply_chain_gate(
            root,
            namespace.policy.expanduser().resolve(),
            tool_receipt=namespace.tool_receipt,
        )
        _print_summary("supply chain", summary)
        return 0
    if namespace.command == "git-snapshot":
        _print_summary("Git snapshot", require_git_snapshot(root, namespace.sha))
        return 0
    if namespace.command == "wheel-smoke":
        _print_summary(
            "installed wheel",
            run_installed_wheel_gate(root, namespace.probe_directory),
        )
        return 0
    if namespace.command == "pre-push":
        return _run_pre_push(namespace, root)
    _fail(f"unsupported command: {namespace.command}")


def main(arguments: Sequence[str] | None = None) -> int:
    namespace = _parser().parse_args(arguments)
    try:
        return _run_command(namespace, _root(namespace.root))
    except GateError as error:
        print(f"quality gate failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
