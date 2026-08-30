"""Bounded local Semgrep CE adapter for Neocortex project invariants.

The adapter scans only inventory-owned staged Python files with the versioned
rules shipped by Neocortex.  It never selects registry rules, enables network
telemetry, applies fixes, or grants mutation authority.
"""

from __future__ import annotations
import hashlib
import json
import os
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from neocortex.runtime.control.bounded_subprocess import run_bounded_capture
from .code_external_evidence import ExternalEvidenceFile
from .external_evidence_models import ExternalProviderFinding, external_signature
from neocortex.semantic.semantic_models import fingerprint_bytes
from neocortex.semgrep_tool_contract import (
    ManagedSemgrepRuntime,
    resolve_semgrep_tool_runtime,
)

SEMGREP_INVARIANTS_PROVIDER_ID = "semgrep-neocortex-invariants"
SEMGREP_INVARIANTS_PROVIDER_SCHEMA = "neocortex.semgrep-neocortex-invariants/v1"
SEMGREP_RULESET_VERSION = "2026.08.03-v1"
SEMGREP_RULESET_SHA256 = "f8a7c4f6aeb21a8d3e434a9d3aee4fafd17ebc1a31c98dc5ed2b934b83066404"
SEMGREP_INVARIANT_RULE_IDS = (
    "neocortex.no-shell-true",
    "neocortex.no-provider-mutation-authority",
    "neocortex.no-external-provider-autofix",
)

SemgrepCliVariant = Literal["semgrep", "pysemgrep"]

_TIMEOUT_SECONDS = 180.0
_BATCH_TIMEOUT_SECONDS = 30.0
_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
_STDERR_LIMIT_BYTES = 128 * 1024
_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
_MAX_FILES = 2_000
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_INPUT_BYTES = 512 * 1024 * 1024
_MAX_FINDINGS = 10_000
_MAX_RULESET_BYTES = 64 * 1024
_MAX_BATCH_FILES = 50
_MAX_BATCH_ARGV_CHARS = 24 * 1024
_MAX_TREE_ENTRIES = 20_000
_WINDOWS_TEMP_PARENT_MAX_CHARS = 80
_RULE_SCHEMA = "neocortex.semgrep-project-invariant-rule/v1"
_BASE_LIMITATIONS = (
    "semgrep_ce_single_file_analysis",
    "local_neocortex_rules_only",
    "advisory_only_no_mutation_authority",
    "autofix_disabled",
)


@dataclass(frozen=True, slots=True)
class _RuleContract:
    code: str
    message: str
    metadata: Mapping[str, object]


def _rule_metadata(code: str) -> dict[str, object]:
    return {
        "category": "project_invariant",
        "confidence": "HIGH",
        "technology": ["python"],
        "neocortex_code": code,
        "neocortex_rule_schema": _RULE_SCHEMA,
    }


_RULE_CONTRACTS: Mapping[str, _RuleContract] = {
    "neocortex.no-shell-true": _RuleContract(
        "NEOCORTEX_NO_SHELL_TRUE",
        "Neocortex subprocesses must never enable shell=True.",
        _rule_metadata("NEOCORTEX_NO_SHELL_TRUE"),
    ),
    "neocortex.no-provider-mutation-authority": _RuleContract(
        "NEOCORTEX_NO_PROVIDER_MUTATION_AUTHORITY",
        "External evidence providers must never claim mutation authority.",
        _rule_metadata("NEOCORTEX_NO_PROVIDER_MUTATION_AUTHORITY"),
    ),
    "neocortex.no-external-provider-autofix": _RuleContract(
        "NEOCORTEX_NO_EXTERNAL_PROVIDER_AUTOFIX",
        "External evidence providers must never activate autofix or fix mode.",
        _rule_metadata("NEOCORTEX_NO_EXTERNAL_PROVIDER_AUTOFIX"),
    ),
}


@dataclass(frozen=True, slots=True)
class SemgrepInvariantExecution:
    findings: tuple[ExternalProviderFinding, ...]
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int
    scanned_files: int
    scanned_bytes: int
    rule_count: int
    cli_variant: SemgrepCliVariant
    ruleset_sha256: str
    input_manifest_sha256: str
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StagedContract:
    owners: Mapping[str, ExternalEvidenceFile]
    targets: tuple[str, ...]
    total_bytes: int
    manifest_sha256: str


def _is_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & reparse)


def _normalized_absolute(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _required_text(value: object, *, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"Semgrep {label} is invalid")
    return value


def _required_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Semgrep {label} is invalid")
    return value


def _required_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Semgrep {label} is not an object")
    return value


def _required_list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"Semgrep {label} is not a list")
    return value


def _bounded_json(value: object, *, label: str, maximum: int = 64 * 1024) -> None:
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Semgrep {label} is not JSON-compatible") from exc
    if len(encoded) > maximum:
        raise ValueError(f"Semgrep {label} exceeds its bound")


def _validate_tool_version(value: str) -> None:
    release = value.split("+", 1)[0].split("-", 1)[0].split(".")
    if len(release) != 3 or release[0] != "1" or release[1] != "172" or not release[2].isdigit():
        raise ValueError("Semgrep version is outside the supported 1.172 line")


def _managed_semgrep_runtime() -> ManagedSemgrepRuntime:
    runtime = resolve_semgrep_tool_runtime()
    _validate_tool_version(runtime.version)
    return runtime


def _ruleset_digest(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("Semgrep ruleset must not contain a UTF-8 BOM")
    canonical = raw.replace(b"\r\n", b"\n")
    if b"\r" in canonical:
        raise ValueError("Semgrep ruleset contains incompatible line endings")
    return hashlib.sha256(canonical).hexdigest()


def _ruleset_path() -> Path:
    ruleset = Path(__file__).with_name("semgrep_rules") / "neocortex_invariants.yml"
    metadata = os.lstat(ruleset)
    if _is_reparse(ruleset, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Semgrep ruleset is not a regular local file")
    if metadata.st_size < 1 or metadata.st_size > _MAX_RULESET_BYTES:
        raise ValueError("Semgrep ruleset size is outside its bound")
    raw = ruleset.read_bytes()
    after = os.lstat(ruleset)
    if after.st_size != metadata.st_size or after.st_mtime_ns != metadata.st_mtime_ns:
        raise ValueError("Semgrep ruleset changed during verification")
    observed = _ruleset_digest(raw)
    if observed != SEMGREP_RULESET_SHA256:
        raise ValueError("Semgrep ruleset digest is incompatible")
    return ruleset.resolve(strict=True)


def _assert_exact_tree(source_root: Path, expected: frozenset[str]) -> None:
    root_metadata = os.lstat(source_root)
    if _is_reparse(source_root, root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("Semgrep staged source root is not a regular directory")
    stack = [source_root]
    observed: set[str] = set()
    entries = 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > _MAX_TREE_ENTRIES:
                    raise ValueError("Semgrep staged tree exceeds its entry bound")
                path = Path(child.path)
                metadata = child.stat(follow_symlinks=False)
                if child.is_symlink() or _is_reparse(path, metadata):
                    raise ValueError("Semgrep staged tree contains a reparse point")
                if stat.S_ISDIR(metadata.st_mode):
                    stack.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Semgrep staged tree contains a non-regular file")
                normalized = _normalized_absolute(path)
                if normalized not in expected or normalized in observed:
                    raise ValueError("Semgrep staged tree contains an unowned file")
                observed.add(normalized)
    if observed != expected:
        raise ValueError("Semgrep staged tree does not contain every owned file")


def _verified_file_row(path: Path, owner: ExternalEvidenceFile) -> dict[str, object]:
    before = os.lstat(path)
    if _is_reparse(path, before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Semgrep staged input is not a regular file")
    if before.st_size != owner.size:
        raise ValueError("Semgrep staged input size disagrees")
    raw = path.read_bytes()
    after = os.lstat(path)
    if (
        len(raw) != owner.size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError("Semgrep staged input changed during verification")
    fingerprint = fingerprint_bytes(raw)
    if (
        fingerprint.xxh3_128 != owner.raw_xxh3_128
        or fingerprint.xxh3_64_guard != owner.raw_xxh3_64_guard
    ):
        raise ValueError("Semgrep staged input fingerprint disagrees")
    return {
        "relative_path": owner.relative_path,
        "size": owner.size,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _staged_contract(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
) -> _StagedContract:
    if not staged or len(staged) > _MAX_FILES:
        raise ValueError("Semgrep staged file count is outside its bound")
    source_root = (stage_root / "source").absolute()
    owners: dict[str, ExternalEvidenceFile] = {}
    relative_keys: set[str] = set()
    version_ids: set[int] = set()
    rows: list[dict[str, object]] = []
    targets: list[tuple[str, str]] = []
    total_bytes = 0
    for absolute_key, owner in staged.items():
        relative = owner.relative_path.replace("\\", "/")
        pure = PurePosixPath(relative)
        if (
            relative != owner.relative_path
            or relative != pure.as_posix()
            or pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
            or pure.suffix.casefold() not in {".py", ".pyi"}
        ):
            raise ValueError("Semgrep staged relative path is invalid")
        if (
            isinstance(owner.version_id, bool)
            or not isinstance(owner.version_id, int)
            or owner.version_id < 1
            or owner.version_id in version_ids
        ):
            raise ValueError("Semgrep staged version identity is invalid")
        if (
            isinstance(owner.size, bool)
            or not isinstance(owner.size, int)
            or owner.size < 0
            or owner.size > _MAX_FILE_BYTES
        ):
            raise ValueError("Semgrep staged file size is outside its bound")
        total_bytes += owner.size
        if total_bytes > _MAX_INPUT_BYTES:
            raise ValueError("Semgrep staged bytes exceed the input bound")
        expected = source_root.joinpath(*pure.parts).absolute()
        normalized = _normalized_absolute(expected)
        if _normalized_absolute(absolute_key) != normalized:
            raise ValueError("Semgrep staged mapping does not describe the exact source path")
        relative_key = relative.casefold()
        if normalized in owners or relative_key in relative_keys:
            raise ValueError("Semgrep staged path is duplicated")
        owners[normalized] = owner
        relative_keys.add(relative_key)
        version_ids.add(owner.version_id)
        rows.append(_verified_file_row(expected, owner))
        target = os.path.relpath(expected, stage_root)
        targets.append((relative_key, target))
    _assert_exact_tree(source_root, frozenset(owners))
    rows.sort(key=lambda item: str(item["relative_path"]).casefold())
    manifest = json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return _StagedContract(
        owners,
        tuple(item[1] for item in sorted(targets)),
        total_bytes,
        hashlib.sha256(manifest).hexdigest(),
    )


def _semgrep_environment(
    environment: Mapping[str, str],
    stage_root: Path,
) -> dict[str, str]:
    blocked = {
        "all_proxy",
        "home",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "path",
        "userprofile",
    }
    controlled: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Semgrep environment must contain only text")
        folded = key.casefold()
        if folded in blocked or folded.startswith(("semgrep_", "otel_")):
            continue
        controlled[key] = value
    if os.name != "nt":
        # Semgrep's native telemetry client probes the platform with
        # ``uname -s`` even when telemetry is disabled.  Supply only Python's
        # fixed POSIX default instead of inheriting a caller-controlled PATH.
        controlled["PATH"] = os.defpath
    settings = stage_root / "semgrep-settings.yml"
    settings.write_bytes(b"{}\n")
    cache = stage_root / "semgrep-xdg-cache"
    config = stage_root / "semgrep-xdg-config"
    temporary_parent = _semgrep_temporary_parent(stage_root)
    cache.mkdir(exist_ok=True)
    config.mkdir(exist_ok=True)
    controlled.update(
        {
            "NO_COLOR": "1",
            "OTEL_SDK_DISABLED": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "SEMGREP_ENABLE_VERSION_CHECK": "0",
            "SEMGREP_FORCE_COLOR": "0",
            "SEMGREP_LOG_FILE": str(stage_root / "semgrep.log"),
            "SEMGREP_SEND_METRICS": "off",
            "SEMGREP_SETTINGS_FILE": str(settings),
            "SEMGREP_VERSION_CACHE_PATH": str(stage_root / "semgrep-version-cache"),
            # pysemgrep 1.172 on Windows returns ``missing output`` when its
            # core handoff temporaries share the scan cwd.  The already
            # validated scratch parent preserves isolation without that bug.
            "TEMP": str(temporary_parent),
            "TMP": str(temporary_parent),
            "TMPDIR": str(temporary_parent),
            "XDG_CACHE_HOME": str(cache),
            "XDG_CONFIG_HOME": str(config),
        }
    )
    return controlled


def _semgrep_temporary_parent(stage_root: Path) -> Path:
    """Select a validated short ancestor for pysemgrep core handoff files."""

    candidates = (stage_root.parent, *stage_root.parent.parents)
    for candidate in candidates:
        if os.name == "nt" and len(os.fspath(candidate)) > _WINDOWS_TEMP_PARENT_MAX_CHARS:
            continue
        try:
            metadata = os.lstat(candidate)
        except OSError:
            continue
        if _is_reparse(candidate, metadata) or not stat.S_ISDIR(metadata.st_mode):
            continue
        if candidate == Path(candidate.anchor):
            continue
        return candidate
    raise ValueError("Semgrep short scratch parent is unavailable")


def _batches(paths: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    current_chars = 0
    for path in paths:
        argument_chars = len(path) + 3
        if argument_chars > _MAX_BATCH_ARGV_CHARS:
            raise ValueError("Semgrep staged target exceeds the argv bound")
        if current and (
            len(current) >= _MAX_BATCH_FILES
            or current_chars + argument_chars > _MAX_BATCH_ARGV_CHARS
        ):
            batches.append(tuple(current))
            current = []
            current_chars = 0
        current.append(path)
        current_chars += argument_chars
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _command_prefix(runtime: ManagedSemgrepRuntime, ruleset: Path) -> tuple[str, ...]:
    return (
        *runtime.command_prefix,
        "--config",
        str(ruleset),
        "--json",
        "--quiet",
        "--metrics",
        "off",
        "--disable-version-check",
        "--oss-only",
        "--no-autofix",
        "--no-secrets-validation",
        "--no-git-ignore",
        "--disable-nosem",
        "--strict",
        "--no-error",
        "--no-rewrite-rule-ids",
        "--no-force-color",
        "--no-trace",
        "--no-time",
        "--skip-unknown-extensions",
        "--jobs",
        "1",
        "--timeout",
        "5",
        "--timeout-threshold",
        "1",
        "--max-target-bytes",
        str(_MAX_FILE_BYTES),
        "--max-memory",
        str(_MEMORY_LIMIT_BYTES // (1024 * 1024)),
        "--max-match-context-size",
        "2048",
        "--max-log-list-entries",
        "20",
    )


def _decode_payload(raw: bytes) -> Mapping[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Semgrep JSON output is malformed") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Semgrep JSON output is not an object")
    return payload


def _owner_for_reported_path(
    value: object,
    *,
    stage_root: Path,
    owners: Mapping[str, ExternalEvidenceFile],
) -> tuple[str, ExternalEvidenceFile]:
    reported = _required_text(value, label="reported path", maximum=32 * 1024)
    candidate = Path(reported)
    if not candidate.is_absolute():
        candidate = stage_root / candidate
    normalized = _normalized_absolute(candidate)
    owner = owners.get(normalized)
    if owner is None:
        raise ValueError("Semgrep reported an unowned path")
    return normalized, owner


def _location(value: object, *, label: str) -> tuple[int, int, int]:
    payload = _required_mapping(value, label=label)
    if set(payload) != {"line", "col", "offset"}:
        raise ValueError(f"Semgrep {label} fields are incompatible")
    line = _required_int(payload.get("line"), label=f"{label} line", minimum=1)
    column = _required_int(payload.get("col"), label=f"{label} column", minimum=1)
    offset = _required_int(payload.get("offset"), label=f"{label} offset")
    return line, column - 1, offset


def _normalize_finding(
    raw: object,
    *,
    stage_root: Path,
    owners: Mapping[str, ExternalEvidenceFile],
    cli_variant: SemgrepCliVariant,
) -> ExternalProviderFinding:
    payload = _required_mapping(raw, label="finding")
    if set(payload) != {"check_id", "path", "start", "end", "extra"}:
        raise ValueError("Semgrep finding fields are incompatible")
    rule_id = _required_text(payload.get("check_id"), label="rule id", maximum=256)
    contract = _RULE_CONTRACTS.get(rule_id)
    if contract is None:
        raise ValueError("Semgrep reported an unauthorized rule")
    _, owner = _owner_for_reported_path(payload.get("path"), stage_root=stage_root, owners=owners)
    start_line, start_column, start_offset = _location(payload.get("start"), label="finding start")
    end_line, end_column, end_offset = _location(payload.get("end"), label="finding end")
    if end_line < start_line or end_offset < start_offset:
        raise ValueError("Semgrep finding span is invalid")
    if end_line == start_line and end_column < start_column:
        raise ValueError("Semgrep finding columns are invalid")
    extra = _required_mapping(payload.get("extra"), label="finding extra")
    allowed_extra = {
        "engine_kind",
        "fingerprint",
        "fix",
        "fixed_lines",
        "is_ignored",
        "lines",
        "message",
        "metadata",
        "metavars",
        "severity",
        "validation_state",
    }
    required_extra = {"message", "metadata", "severity"}
    if not required_extra <= set(extra) or not set(extra) <= allowed_extra:
        raise ValueError("Semgrep finding extra fields are incompatible")
    message = _required_text(extra.get("message"), label="finding message")
    if message != contract.message or extra.get("severity") != "ERROR":
        raise ValueError("Semgrep finding rule contract disagrees")
    metadata = _required_mapping(extra.get("metadata"), label="rule metadata")
    if dict(metadata) != dict(contract.metadata):
        raise ValueError("Semgrep finding metadata disagrees")
    if extra.get("fix") not in (None, "") or extra.get("fixed_lines") not in (None, ""):
        raise ValueError("Semgrep unexpectedly offered a fix")
    if extra.get("is_ignored") not in (None, False):
        raise ValueError("Semgrep returned an ignored finding")
    engine = extra.get("engine_kind")
    if engine is not None and engine != "OSS":
        raise ValueError("Semgrep finding did not use the OSS engine")
    for key in ("fingerprint", "lines", "validation_state"):
        value = extra.get(key)
        if value is not None:
            _required_text(value, label=f"finding {key}", maximum=8192)
    metavariables = extra.get("metavars")
    if metavariables is not None:
        _required_mapping(metavariables, label="finding metavariables")
        _bounded_json(metavariables, label="finding metavariables")
    identity = external_signature(
        "external-finding-v1",
        {
            "provider_id": SEMGREP_INVARIANTS_PROVIDER_ID,
            "path": owner.relative_path,
            "category": "project_invariant",
            "code": contract.code,
            "message": message,
            "start_line": start_line,
            "start_column": start_column,
            "end_line": end_line,
            "end_column": end_column,
        },
    )
    return ExternalProviderFinding(
        identity,
        owner.version_id,
        owner.relative_path,
        "project_invariant",
        contract.code,
        "error",
        message,
        True,
        1.0,
        None,
        "advisory",
        start_line,
        start_column,
        end_line,
        end_column,
        url=None,
        fix_available=False,
        metadata={
            "provider_schema": SEMGREP_INVARIANTS_PROVIDER_SCHEMA,
            "ruleset_version": SEMGREP_RULESET_VERSION,
            "ruleset_sha256": SEMGREP_RULESET_SHA256,
            "rule_id": rule_id,
            "rule_message": message,
            "semgrep_severity": "ERROR",
            "semgrep_cli_variant": cli_variant,
            "rule_metadata": dict(metadata),
        },
        mutation_authority=False,
    )


def _parse_batch(
    raw: bytes,
    *,
    stage_root: Path,
    owners: Mapping[str, ExternalEvidenceFile],
    expected_paths: frozenset[str],
    expected_version: str,
    cli_variant: SemgrepCliVariant,
) -> tuple[ExternalProviderFinding, ...]:
    payload = _decode_payload(raw)
    required = {"version", "results", "errors", "paths"}
    allowed = required | {
        "engine_requested",
        "interfile_languages_used",
        "profiling_results",
        "skipped_rules",
        "time",
    }
    if not required <= set(payload) or not set(payload) <= allowed:
        raise ValueError("Semgrep JSON fields are incompatible")
    observed_version = _required_text(payload.get("version"), label="output version", maximum=64)
    if observed_version != expected_version:
        raise ValueError("Semgrep output version disagrees with the installed distribution")
    if _required_list(payload.get("errors"), label="errors"):
        raise ValueError("Semgrep reported analysis errors")
    if payload.get("skipped_rules") not in (None, []):
        raise ValueError("Semgrep skipped a local invariant rule")
    if payload.get("interfile_languages_used") not in (None, []):
        raise ValueError("Semgrep unexpectedly used interfile analysis")
    if payload.get("profiling_results") not in (None, []):
        raise ValueError("Semgrep returned unexpected profiling results")
    timing = payload.get("time")
    if timing is not None:
        _required_mapping(timing, label="timing evidence")
        _bounded_json(timing, label="timing evidence", maximum=512 * 1024)
    if payload.get("engine_requested") not in (None, "OSS"):
        raise ValueError("Semgrep did not request the OSS engine")
    paths = _required_mapping(payload.get("paths"), label="paths")
    if "scanned" not in paths or not set(paths) <= {"scanned", "skipped"}:
        raise ValueError("Semgrep path evidence fields are incompatible")
    if paths.get("skipped") not in (None, []):
        raise ValueError("Semgrep skipped an owned target")
    scanned: set[str] = set()
    for value in _required_list(paths.get("scanned"), label="scanned paths"):
        normalized, _ = _owner_for_reported_path(value, stage_root=stage_root, owners=owners)
        if normalized in scanned:
            raise ValueError("Semgrep reported a duplicate scanned path")
        scanned.add(normalized)
    if scanned != set(expected_paths):
        raise ValueError("Semgrep scanned path evidence disagrees")
    raw_findings = _required_list(payload.get("results"), label="results")
    if len(raw_findings) > _MAX_FINDINGS:
        raise ValueError("Semgrep findings exceed their bound")
    return tuple(
        _normalize_finding(
            item,
            stage_root=stage_root,
            owners=owners,
            cli_variant=cli_variant,
        )
        for item in raw_findings
    )


def _unexpected_exit(completed: subprocess.CompletedProcess[bytes]) -> ValueError:
    raw = completed.stderr or completed.stdout
    detail = " ".join(raw.decode("utf-8", errors="replace").split())[:2048]
    message = f"semgrep_unexpected_exit:{completed.returncode}"
    return ValueError(message if not detail else f"{message}:{detail}")


def execute_semgrep_invariants(
    stage_root: Path,
    staged: Mapping[str, ExternalEvidenceFile],
    environment: Mapping[str, str],
) -> SemgrepInvariantExecution:
    """Run only local Neocortex invariants over exact staged Python inputs."""

    stage_root = stage_root.absolute()
    contract = _staged_contract(stage_root, staged)
    ruleset = _ruleset_path()
    runtime = _managed_semgrep_runtime()
    version = runtime.version
    cli_variant: SemgrepCliVariant = "pysemgrep"
    controlled_environment = _semgrep_environment(environment, stage_root)
    prefix = _command_prefix(runtime, ruleset)
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    stdout_bytes = 0
    stderr_bytes = 0
    invocations = 0
    findings_by_id: dict[str, ExternalProviderFinding] = {}
    for batch in _batches(contract.targets):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(prefix, _TIMEOUT_SECONDS)
        command = (*prefix, "--", *batch)
        completed = run_bounded_capture(
            command,
            timeout_seconds=min(_BATCH_TIMEOUT_SECONDS, remaining),
            stdout_limit_bytes=max(0, _STDOUT_LIMIT_BYTES - stdout_bytes),
            stderr_limit_bytes=max(0, _STDERR_LIMIT_BYTES - stderr_bytes),
            cwd=stage_root,
            environment=controlled_environment,
            memory_limit_bytes=_MEMORY_LIMIT_BYTES if os.name == "nt" else None,
        )
        invocations += 1
        if completed.returncode != 0:
            raise _unexpected_exit(completed)
        stdout_bytes += len(completed.stdout)
        stderr_bytes += len(completed.stderr)
        expected_paths = frozenset(
            _normalized_absolute(stage_root / Path(target)) for target in batch
        )
        findings = _parse_batch(
            completed.stdout,
            stage_root=stage_root,
            owners=contract.owners,
            expected_paths=expected_paths,
            expected_version=version,
            cli_variant=cli_variant,
        )
        for finding in findings:
            existing = findings_by_id.get(finding.portable_finding_id)
            if existing is not None and existing != finding:
                raise ValueError("Semgrep finding identity collision")
            findings_by_id[finding.portable_finding_id] = finding
        if len(findings_by_id) > _MAX_FINDINGS:
            raise ValueError("Semgrep findings exceed their total bound")
    after = _staged_contract(stage_root, staged)
    if after.manifest_sha256 != contract.manifest_sha256:
        raise ValueError("Semgrep staged inputs changed during analysis")
    if _ruleset_path() != ruleset:
        raise ValueError("Semgrep ruleset identity changed during analysis")
    limitations: tuple[str, ...] = _BASE_LIMITATIONS
    if os.name == "nt":
        limitations += ("windows_pysemgrep_x509_compatibility",)
    return SemgrepInvariantExecution(
        tuple(sorted(findings_by_id.values(), key=lambda item: item.portable_finding_id)),
        stdout_bytes,
        stderr_bytes,
        invocations,
        len(contract.owners),
        contract.total_bytes,
        len(SEMGREP_INVARIANT_RULE_IDS),
        cli_variant,
        SEMGREP_RULESET_SHA256,
        contract.manifest_sha256,
        limitations,
    )


__all__ = [
    "SEMGREP_INVARIANTS_PROVIDER_ID",
    "SEMGREP_INVARIANTS_PROVIDER_SCHEMA",
    "SEMGREP_INVARIANT_RULE_IDS",
    "SEMGREP_RULESET_SHA256",
    "SEMGREP_RULESET_VERSION",
    "SemgrepInvariantExecution",
    "execute_semgrep_invariants",
]
