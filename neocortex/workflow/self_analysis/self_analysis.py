"""Pure contracts for protected source-tree self-analysis runs."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from neocortex.deduplication import InventoryExclusionPolicy
from neocortex.deduplication.inventory.index import (
    DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
    DEFAULT_GENERATED_DIRECTORY_PREFIXES,
)

from _04_Nucleo_Operativo.code_contracts import (
    _legacy_deep_configuration_payload,
    deep_configuration_payload,
    deep_configuration_signature,
    normalize_deep_test_selectors,
)

if TYPE_CHECKING:

    class FrameworkConfig(Protocol):
        """Read-only configuration surface consumed by self-analysis."""

        @property
        def analysis_profile(
            self,
        ) -> Literal["protected", "trusted-static", "trusted-deep"]: ...

        @property
        def code_max_file_bytes(self) -> int: ...

        @property
        def code_max_documents(self) -> int | None: ...

        @property
        def code_max_text_chars(self) -> int: ...

        @property
        def code_chunk_chars(self) -> int: ...

        @property
        def code_retry_errors(self) -> bool: ...

        @property
        def code_cache_validation(self) -> Literal["metadata", "full"]: ...

        @property
        def code_complexity_warning(self) -> int: ...

        @property
        def code_function_lines_warning(self) -> int: ...

        @property
        def deep_test_selectors(self) -> tuple[str, ...]: ...

        @property
        def deep_max_tests(self) -> int: ...

        @property
        def deep_time_budget_seconds(self) -> int: ...

        @property
        def deep_shard_size(self) -> int: ...

        @property
        def deep_mutation_target(self) -> str | None: ...

        @property
        def deep_mutation_symbol(self) -> str | None: ...

        @property
        def deep_mutation_max_mutants(self) -> int: ...

        @property
        def deep_mutation_timeout_seconds(self) -> int: ...

        @property
        def deep_mutation_time_budget_seconds(self) -> int: ...

# region [01] Stable profile and manifest contracts


SELF_ANALYSIS_PROFILE_VERSION = "neocortex.self-analysis-profile/v1"
SELF_ANALYSIS_MANIFEST_SCHEMA = "neocortex.self-analysis-manifest/v2"
LEGACY_SELF_ANALYSIS_MANIFEST_SCHEMAS = frozenset({"neocortex.self-analysis-manifest/v1"})
SELF_ANALYSIS_MANIFEST_PHASE = "self-analysis-manifest"
SELF_ANALYSIS_MANIFEST_MESSAGE = "Manifest de autoanálisis publicado"
MAX_SELF_ANALYSIS_MANIFEST_BYTES = 256 * 1024
# One selected pytest file contributes an option/value pair to the recorded
# command.  Keep producer and decoder on the same explicit bound so a completed
# selected-suite run cannot become unreadable solely because it selected more
# than the decoder's generic 128-item collection limit.
MAX_SELF_ANALYSIS_COMMAND_ARGUMENTS = 4_096
MAX_SELF_ANALYSIS_STATUS_ARGUMENTS = 16

_EXCLUDED_DIRECTORY_NAMES = (
    ".cache",
    ".codex-lab",
    ".complexipy_cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "backup",
    "backups",
    "build",
    "coverage",
    "dist",
    "env",
    "htmlcov",
    "node_modules",
    "out",
    "site-packages",
    "target",
    "temp",
    "third-party",
    "third_party",
    "tmp",
    "vendor",
    "vendors",
    "venv",
)
_EXCLUDED_FILE_NAMES = (
    ".coverage",
    "coverage.json",
    "coverage.xml",
)
_EXCLUDED_FILE_SUFFIXES = (
    ".backup",
    ".bak",
    ".coverage",
    ".db",
    ".db-shm",
    ".db-wal",
    ".db3",
    ".log",
    ".old",
    ".orig",
    ".prof",
    ".pstats",
    ".pyc",
    ".pyo",
    ".shm",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".temp",
    ".tmp",
    ".wal",
)
_TRANSIENT_DIRECTORY_PREFIXES = (".pytest-", ".test-tmp")
_MAX_TRANSIENT_DIRECTORY_ROOTS = 256


# endregion [01]


# region [02] Inventory profile


def _transient_project_directories(root: Path) -> tuple[Path, ...]:
    """Discover bounded top-level test artifacts absent from stable name rules."""

    matches: list[Path] = []
    with os.scandir(root) as entries:
        for entry in entries:
            name_key = entry.name.casefold()
            if not name_key.startswith(_TRANSIENT_DIRECTORY_PREFIXES):
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_directory:
                continue
            matches.append(Path(entry.path))
            if len(matches) > _MAX_TRANSIENT_DIRECTORY_ROOTS:
                raise ValueError("too many transient self-analysis directories")
    return tuple(sorted(matches, key=lambda path: os.path.normcase(str(path))))


def build_self_analysis_inventory_policy(
    root: Path,
    state_directory: Path,
) -> InventoryExclusionPolicy:
    """Compile the exact project-local exclusions for protected analysis."""

    explicit_roots = (
        state_directory,
        root / ".codex-lab",
        root / "docs" / "audit_evidence",
        root / "Laboratory",
        root / "neocortex_framework.egg-info",
        *_transient_project_directories(root),
    )
    return InventoryExclusionPolicy.compile(
        explicit_roots,
        directory_names=_EXCLUDED_DIRECTORY_NAMES,
        directory_prefixes=DEFAULT_GENERATED_DIRECTORY_PREFIXES,
        directory_fragments=DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
        file_names=_EXCLUDED_FILE_NAMES,
        file_suffixes=_EXCLUDED_FILE_SUFFIXES,
    )


def inventory_policy_manifest(
    policy: InventoryExclusionPolicy,
) -> dict[str, object]:
    """Return the bounded canonical rules that produced one policy signature."""

    explicit_roots = list(policy.explicit_roots)
    directory_names = sorted(policy.directory_names)
    directory_prefixes = list(policy.directory_prefixes)
    directory_fragments = list(policy.directory_fragments)
    file_names = sorted(policy.file_names)
    file_suffixes = list(policy.file_suffixes)
    manifest: dict[str, object] = {
        "profile": SELF_ANALYSIS_PROFILE_VERSION,
        "signature": policy.signature,
        "signature_version": policy.signature_version,
        "explicit_roots": explicit_roots,
        "directory_names": directory_names,
        "directory_prefixes": directory_prefixes,
        "directory_fragments": directory_fragments,
        "file_names": file_names,
        "file_suffixes": file_suffixes,
    }
    rebuilt = InventoryExclusionPolicy.compile(
        explicit_roots,
        directory_names=directory_names,
        directory_prefixes=directory_prefixes,
        directory_fragments=directory_fragments,
        file_names=file_names,
        file_suffixes=file_suffixes,
    )
    if rebuilt.signature != policy.signature:
        raise ValueError("self-analysis inventory policy manifest is inconsistent")
    return manifest


# endregion [02]


# region [03] Reproducible command and completion manifest helpers


def _decimal_megabytes(byte_count: int) -> str:
    whole, remainder = divmod(byte_count, 1_000_000)
    if remainder == 0:
        return str(whole)
    return f"{whole}.{remainder:06d}".rstrip("0")


def self_analysis_commands(
    config: FrameworkConfig,
    root: Path,
    state_directory: Path,
) -> dict[str, list[str]]:
    """Build exact argv arrays equivalent to the effective protected run."""

    analyze = [
        "Neocortex",
        "--self-analysis",
        "--analysis-profile",
        config.analysis_profile,
        "--root",
        str(root),
        "--state-directory",
        str(state_directory),
        "--code-max-mb",
        _decimal_megabytes(config.code_max_file_bytes),
        "--code-max-text-chars",
        str(config.code_max_text_chars),
        "--code-chunk-chars",
        str(config.code_chunk_chars),
        "--code-complexity-warning",
        str(config.code_complexity_warning),
        "--code-function-lines-warning",
        str(config.code_function_lines_warning),
        "--code-cache-validation",
        config.code_cache_validation,
        "--no-code-generated",
        "--no-code-vendored",
    ]
    if config.code_max_documents is not None:
        analyze.extend(("--code-max-count", str(config.code_max_documents)))
    if config.code_retry_errors:
        analyze.append("--retry-code-errors")
    deep_selectors = normalize_deep_test_selectors(config.deep_test_selectors)
    deep_payload = deep_configuration_payload(
        analysis_profile=config.analysis_profile,
        test_selectors=deep_selectors,
        max_tests=config.deep_max_tests,
        time_budget_seconds=config.deep_time_budget_seconds,
        shard_size=config.deep_shard_size,
        mutation_target=config.deep_mutation_target,
        mutation_symbol=config.deep_mutation_symbol,
        mutation_max_mutants=config.deep_mutation_max_mutants,
        mutation_timeout_seconds=config.deep_mutation_timeout_seconds,
        mutation_time_budget_seconds=config.deep_mutation_time_budget_seconds,
    )
    if config.analysis_profile == "trusted-deep":
        for selector in deep_selectors:
            analyze.extend(("--deep-test-selector", selector))
        analyze.extend(
            (
                "--deep-max-tests",
                str(config.deep_max_tests),
                "--deep-time-budget-seconds",
                str(config.deep_time_budget_seconds),
                "--deep-shard-size",
                str(config.deep_shard_size),
            )
        )
        mutation_target = deep_payload["mutation_target"]
        mutation_symbol = deep_payload["mutation_symbol"]
        if isinstance(mutation_target, str):
            analyze.extend(("--deep-mutation-target", mutation_target))
        if isinstance(mutation_symbol, str):
            analyze.extend(("--deep-mutation-symbol", mutation_symbol))
        analyze.extend(
            (
                "--deep-mutation-max-mutants",
                str(config.deep_mutation_max_mutants),
                "--deep-mutation-timeout-seconds",
                str(config.deep_mutation_timeout_seconds),
                "--deep-mutation-time-budget-seconds",
                str(config.deep_mutation_time_budget_seconds),
            )
        )
    return {
        "analyze": analyze,
        "status": [
            "Neocortex",
            "--state-directory",
            str(state_directory),
            "--code-status",
            "--code-json",
        ],
    }


def _bounded_argv(
    values: Sequence[str],
    *,
    label: str,
    maximum_items: int = MAX_SELF_ANALYSIS_COMMAND_ARGUMENTS,
) -> list[str]:
    result = list(values)
    if (
        not result
        # A full Linux test inventory contributes two argv items per selector.
        # Keep the manifest bounded, but do not reject the producer after it has
        # already completed solely because an explicit selected suite exceeds
        # the former 128-item envelope.
        or len(result) > maximum_items
        or any(
            not isinstance(value, str) or not value or len(value.encode("utf-8")) > 32_768
            for value in result
        )
    ):
        raise ValueError(f"invalid self-analysis {label} argv")
    return result


def _option_values(argv: Sequence[str], option: str) -> list[str]:
    """Read exact option/value pairs and reject a missing value."""

    values: list[str] = []
    for index, token in enumerate(argv):
        if token == option:
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                raise ValueError(f"self-analysis {option} value is missing")
            values.append(argv[index + 1])
        elif token.startswith(option + "="):
            value = token[len(option) + 1 :]
            if not value:
                raise ValueError(f"self-analysis {option} value is missing")
            values.append(value)
    return values


def _deep_analysis_from_argv(argv: Sequence[str]) -> dict[str, object] | None:
    """Reconstruct trusted-deep evidence exactly or fail closed."""

    deep_options = (
        "--deep-test-selector",
        "--deep-max-tests",
        "--deep-time-budget-seconds",
        "--deep-shard-size",
        "--deep-mutation-target",
        "--deep-mutation-symbol",
        "--deep-mutation-max-mutants",
        "--deep-mutation-timeout-seconds",
        "--deep-mutation-time-budget-seconds",
    )
    profile_values = _option_values(argv, "--analysis-profile")
    if len(profile_values) > 1:
        raise ValueError("self-analysis profile is duplicated")
    profile = profile_values[0] if profile_values else "protected"
    deep_present = any(
        token == option or token.startswith(option + "=")
        for token in argv
        for option in deep_options
    )
    if profile != "trusted-deep":
        if deep_present:
            raise ValueError("deep analysis controls require trusted-deep")
        return None
    scalar_values: dict[str, int] = {}
    for option in deep_options[1:4]:
        values = _option_values(argv, option)
        if len(values) != 1:
            raise ValueError(f"trusted-deep requires exactly one {option}")
        try:
            scalar_values[option] = int(values[0])
        except ValueError as exc:
            raise ValueError(f"trusted-deep {option} must be an integer") from exc
    test_selectors = _option_values(argv, "--deep-test-selector")
    max_tests = scalar_values["--deep-max-tests"]
    time_budget_seconds = scalar_values["--deep-time-budget-seconds"]
    shard_size = scalar_values["--deep-shard-size"]
    mutation_options = deep_options[4:]
    mutation_present = any(
        token == option or token.startswith(option + "=")
        for token in argv
        for option in mutation_options
    )
    if not mutation_present:
        payload = _legacy_deep_configuration_payload(
            analysis_profile=profile,
            test_selectors=test_selectors,
            max_tests=max_tests,
            time_budget_seconds=time_budget_seconds,
            shard_size=shard_size,
        )
    else:
        mutation_target_values = _option_values(argv, "--deep-mutation-target")
        mutation_symbol_values = _option_values(argv, "--deep-mutation-symbol")
        if len(mutation_target_values) > 1:
            raise ValueError("trusted-deep mutation target is duplicated")
        if len(mutation_symbol_values) > 1:
            raise ValueError("trusted-deep mutation symbol is duplicated")
        mutation_scalars: dict[str, int] = {}
        for option in mutation_options[2:]:
            values = _option_values(argv, option)
            if len(values) != 1:
                raise ValueError(f"trusted-deep requires exactly one {option}")
            try:
                mutation_scalars[option] = int(values[0])
            except ValueError as exc:
                raise ValueError(f"trusted-deep {option} must be an integer") from exc
        payload = deep_configuration_payload(
            analysis_profile=profile,
            test_selectors=test_selectors,
            max_tests=max_tests,
            time_budget_seconds=time_budget_seconds,
            shard_size=shard_size,
            mutation_target=(mutation_target_values[0] if mutation_target_values else None),
            mutation_symbol=(mutation_symbol_values[0] if mutation_symbol_values else None),
            mutation_max_mutants=mutation_scalars["--deep-mutation-max-mutants"],
            mutation_timeout_seconds=mutation_scalars["--deep-mutation-timeout-seconds"],
            mutation_time_budget_seconds=mutation_scalars["--deep-mutation-time-budget-seconds"],
        )
    return {
        **payload,
        "configuration_signature": deep_configuration_signature(payload),
    }


def build_self_analysis_completion_manifest(
    *,
    run: Mapping[str, object],
    inventory: Mapping[str, object],
    inventory_policy: InventoryExclusionPolicy,
    code_processing_signature: str,
    code_summary: Mapping[str, object],
    safety_counts: Mapping[str, int],
    commands: Mapping[str, Sequence[str]],
) -> tuple[dict[str, object], str]:
    """Build and serialize one fixed, bounded completion manifest."""

    if (
        not code_processing_signature
        or code_processing_signature.strip() != code_processing_signature
        or len(code_processing_signature.encode("utf-8")) > 4096
    ):
        raise ValueError("invalid code processing signature")
    expected_zeroes = {
        "route_candidates",
        "file_actions",
        "run_actions",
        "organization_events",
    }
    if set(safety_counts) != expected_zeroes or any(
        type(value) is not int or value != 0 for value in safety_counts.values()
    ):
        raise ValueError("self-analysis safety counts must be exact zeroes")
    if set(commands) != {"analyze", "status"}:
        raise ValueError("self-analysis commands are incomplete")
    analyze_argv = _bounded_argv(commands["analyze"], label="analyze")
    status_argv = _bounded_argv(
        commands["status"],
        label="status",
        maximum_items=MAX_SELF_ANALYSIS_STATUS_ARGUMENTS,
    )
    deep_analysis = _deep_analysis_from_argv(analyze_argv)
    manifest: dict[str, object] = {
        "schema": SELF_ANALYSIS_MANIFEST_SCHEMA,
        "run": dict(run),
        "inventory": {
            **dict(inventory),
            "policy": inventory_policy_manifest(inventory_policy),
        },
        "code": {
            "route_name": "code",
            "input_source": "inventory_snapshot",
            "processing_signature": code_processing_signature,
            "summary": dict(code_summary),
        },
        "safety": dict(safety_counts),
        "commands": {
            "analyze": analyze_argv,
            "status": status_argv,
        },
    }
    if deep_analysis is not None:
        manifest["deep_analysis"] = deep_analysis
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("utf-8")) > MAX_SELF_ANALYSIS_MANIFEST_BYTES:
        raise ValueError("self-analysis completion manifest exceeds its bound")
    return manifest, payload


# endregion [03]


__all__ = [
    "LEGACY_SELF_ANALYSIS_MANIFEST_SCHEMAS",
    "MAX_SELF_ANALYSIS_COMMAND_ARGUMENTS",
    "MAX_SELF_ANALYSIS_MANIFEST_BYTES",
    "MAX_SELF_ANALYSIS_STATUS_ARGUMENTS",
    "SELF_ANALYSIS_MANIFEST_MESSAGE",
    "SELF_ANALYSIS_MANIFEST_PHASE",
    "SELF_ANALYSIS_MANIFEST_SCHEMA",
    "SELF_ANALYSIS_PROFILE_VERSION",
    "build_self_analysis_completion_manifest",
    "build_self_analysis_inventory_policy",
    "inventory_policy_manifest",
    "self_analysis_commands",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.self_analysis")
