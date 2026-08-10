"""Strict contract for the release-managed, scan-only Semgrep tool runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .pip_bootstrap import (
    PIP_BOOTSTRAP_FILENAME,
    PIP_BOOTSTRAP_SHA256,
    PIP_BOOTSTRAP_URL,
    PIP_BOOTSTRAP_VERSION,
)

SEMGREP_TOOL_SCHEMA_VERSION = 1
SEMGREP_TOOL_RECEIPT_KIND = "neocortex_semgrep_tool_runtime"
SEMGREP_TOOL_RECEIPT_NAME = "neocortex-tool-runtime.json"
SEMGREP_TOOL_VERSION = "1.172.0"
SEMGREP_TOOL_MCP_VERSION = "1.23.3"
SEMGREP_TOOL_PIP_VERSION = PIP_BOOTSTRAP_VERSION
SEMGREP_TOOL_CONSTRAINTS_NAME = "semgrep_tool_constraints.txt"
SEMGREP_TOOL_CONSTRAINTS_SHA256 = "61457161ee91f3908d4447b50311d2e54f63db62f1b342e9ceddda2de2b7595f"

SEMGREP_TOOL_ALLOWED_SURFACES = ("local_scan_only",)
SEMGREP_TOOL_DENIED_ENTRYPOINTS = ("mcp", "pysemgrep", "semgrep")
SEMGREP_TOOL_VULNERABILITY_EXCEPTIONS: tuple[Mapping[str, object], ...] = (
    {
        "id": "GHSA-hvrp-rf83-w775",
        "package": "mcp",
        "version": SEMGREP_TOOL_MCP_VERSION,
        "reason": "semgrep_1.172_exact_dependency_in_isolated_scan_only_runtime",
        "surface": "experimental_tasks_not_invoked",
        "reachable": False,
        "expires": "2026-09-30",
    },
    {
        "id": "GHSA-jpw9-pfvf-9f58",
        "package": "mcp",
        "version": SEMGREP_TOOL_MCP_VERSION,
        "reason": "semgrep_1.172_exact_dependency_in_isolated_scan_only_runtime",
        "surface": "authenticated_http_transport_not_started",
        "reachable": False,
        "expires": "2026-09-30",
    },
    {
        "id": "GHSA-vj7q-gjh5-988w",
        "package": "mcp",
        "version": SEMGREP_TOOL_MCP_VERSION,
        "reason": "semgrep_1.172_exact_dependency_in_isolated_scan_only_runtime",
        "surface": "websocket_transport_not_started",
        "reachable": False,
        "expires": "2026-09-30",
    },
)

SEMGREP_SCAN_WRAPPER = b'''"""NeoCortex release-managed Semgrep scan-only entrypoint."""
from __future__ import annotations

import importlib.metadata
import sys


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--neocortex-tool-version"]:
        print(importlib.metadata.version("semgrep"))
        return 0
    sys.argv = [sys.argv[0], "scan", *arguments]
    # Call the Python scanner directly: Semgrep's generic dispatcher can expose
    # unrelated subcommands and attempts an execvp("pysemgrep") fallback.
    from semgrep.console_scripts.pysemgrep import main as semgrep_main

    result = semgrep_main()
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
'''
SEMGREP_SCAN_WRAPPER_NAME = "neocortex_semgrep_scan.py"
SEMGREP_SCAN_WRAPPER_SHA256 = hashlib.sha256(SEMGREP_SCAN_WRAPPER).hexdigest()

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 512 * 1024
_EXPECTED_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "tool",
        "version",
        "python_version",
        "platform",
        "python_executable",
        "scan_wrapper",
        "scan_wrapper_sha256",
        "constraints_filename",
        "constraints_sha256",
        "pip_bootstrap_version",
        "pip_bootstrap_filename",
        "pip_bootstrap_sha256",
        "installed_packages",
        "installed_packages_sha256",
        "install_artifacts",
        "install_artifacts_sha256",
        "runtime_digest_sha256",
        "allowed_surfaces",
        "denied_console_entrypoints",
        "vulnerability_exceptions",
    }
)


@dataclass(frozen=True, slots=True)
class ManagedSemgrepRuntime:
    """Validated executable identity for the isolated Semgrep tool runtime."""

    tool_root: Path
    python: Path
    wrapper: Path
    receipt: Path
    receipt_sha256: str
    version: str

    @property
    def command_prefix(self) -> tuple[str, ...]:
        return (str(self.python), "-I", str(self.wrapper))


def canonical_json(value: object) -> bytes:
    """Encode a receipt component in the single canonical representation."""

    return (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_digest(payload: Mapping[str, object]) -> str:
    """Bind executable, resolver and inventory identities into one digest."""

    bound = {
        "constraints_sha256": payload.get("constraints_sha256"),
        "install_artifacts_sha256": payload.get("install_artifacts_sha256"),
        "installed_packages_sha256": payload.get("installed_packages_sha256"),
        "pip_bootstrap_sha256": payload.get("pip_bootstrap_sha256"),
        "python_executable": payload.get("python_executable"),
        "python_version": payload.get("python_version"),
        "scan_wrapper": payload.get("scan_wrapper"),
        "scan_wrapper_sha256": payload.get("scan_wrapper_sha256"),
        "tool": payload.get("tool"),
        "version": payload.get("version"),
    }
    return hashlib.sha256(canonical_json(bound)).hexdigest()


def _is_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & reparse)


def _regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"managed Semgrep {label} is unavailable") from exc
    if _is_reparse(path, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"managed Semgrep {label} is not a regular file")


def _contained_file(tool_root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str):
        raise ValueError(f"managed Semgrep {label} path is invalid")
    portable = PurePosixPath(relative)
    if (
        portable.is_absolute()
        or not portable.parts
        or any(part in {"", ".", ".."} for part in portable.parts)
    ):
        raise ValueError(f"managed Semgrep {label} path is not relative")
    candidate = tool_root.joinpath(*portable.parts)
    if os.path.commonpath(
        (os.fspath(tool_root.absolute()), os.fspath(candidate.absolute()))
    ) != os.fspath(tool_root.absolute()):
        raise ValueError(f"managed Semgrep {label} escapes its tool runtime")
    _regular_file(candidate, label=label)
    return candidate


def _package_rows(value: object, *, label: str, with_filename: bool) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"managed Semgrep {label} is invalid")
    keys = {"name", "version", "sha256", "filename"} if with_filename else {"name", "version"}
    rows: list[dict[str, str]] = []
    names: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != keys:
            raise ValueError(f"managed Semgrep {label} row is invalid")
        if not all(isinstance(raw[key], str) and raw[key] for key in keys):
            raise ValueError(f"managed Semgrep {label} row has an invalid value")
        name = str(raw["name"])
        if name != name.casefold() or name in names:
            raise ValueError(f"managed Semgrep {label} package identity is invalid")
        if with_filename and not _SHA256.fullmatch(str(raw["sha256"])):
            raise ValueError(f"managed Semgrep {label} artifact digest is invalid")
        names.add(name)
        rows.append({key: str(raw[key]) for key in sorted(keys)})
    if rows != sorted(rows, key=lambda item: item["name"]):
        raise ValueError(f"managed Semgrep {label} is not canonically ordered")
    return rows


def validate_semgrep_tool_receipt(
    payload: object,
    *,
    tool_root: Path,
) -> ManagedSemgrepRuntime:
    """Validate an exact receipt and return only its contained scan command."""

    if not isinstance(payload, dict) or set(payload) != _EXPECTED_RECEIPT_KEYS:
        raise ValueError("managed Semgrep receipt shape is invalid")
    expected_scalars: Mapping[str, object] = {
        "schema_version": SEMGREP_TOOL_SCHEMA_VERSION,
        "kind": SEMGREP_TOOL_RECEIPT_KIND,
        "tool": "semgrep",
        "version": SEMGREP_TOOL_VERSION,
        "scan_wrapper": SEMGREP_SCAN_WRAPPER_NAME,
        "scan_wrapper_sha256": SEMGREP_SCAN_WRAPPER_SHA256,
        "constraints_filename": SEMGREP_TOOL_CONSTRAINTS_NAME,
        "constraints_sha256": SEMGREP_TOOL_CONSTRAINTS_SHA256,
        "pip_bootstrap_version": SEMGREP_TOOL_PIP_VERSION,
        "pip_bootstrap_filename": PIP_BOOTSTRAP_FILENAME,
        "pip_bootstrap_sha256": PIP_BOOTSTRAP_SHA256,
    }
    if any(payload.get(key) != value for key, value in expected_scalars.items()):
        raise ValueError("managed Semgrep receipt policy is incompatible")
    if payload.get("allowed_surfaces") != list(SEMGREP_TOOL_ALLOWED_SURFACES):
        raise ValueError("managed Semgrep allowed surface is incompatible")
    if payload.get("denied_console_entrypoints") != list(SEMGREP_TOOL_DENIED_ENTRYPOINTS):
        raise ValueError("managed Semgrep denied entrypoints are incompatible")
    if payload.get("vulnerability_exceptions") != [
        dict(item) for item in SEMGREP_TOOL_VULNERABILITY_EXCEPTIONS
    ]:
        raise ValueError("managed Semgrep vulnerability exceptions are incompatible")
    python_version = payload.get("python_version")
    platform = payload.get("platform")
    if not isinstance(python_version, str) or not re.fullmatch(
        r"3\.(?:13|14)\.\d+", python_version
    ):
        raise ValueError("managed Semgrep Python version is incompatible")
    if not isinstance(platform, str) or not platform or len(platform) > 256:
        raise ValueError("managed Semgrep platform is invalid")

    inventory = _package_rows(
        payload.get("installed_packages"), label="inventory", with_filename=False
    )
    artifacts = _package_rows(
        payload.get("install_artifacts"), label="artifacts", with_filename=True
    )
    inventory_by_name = {row["name"]: row["version"] for row in inventory}
    if inventory_by_name.get("semgrep") != SEMGREP_TOOL_VERSION:
        raise ValueError("managed Semgrep inventory has an incompatible Semgrep")
    if inventory_by_name.get("mcp") != SEMGREP_TOOL_MCP_VERSION:
        raise ValueError("managed Semgrep inventory has an incompatible MCP")
    if inventory_by_name.get("pip") != SEMGREP_TOOL_PIP_VERSION:
        raise ValueError("managed Semgrep inventory has an incompatible pip")
    artifact_versions = {row["name"]: row["version"] for row in artifacts}
    expected_artifacts = {
        name: version for name, version in inventory_by_name.items() if name != "pip"
    }
    if artifact_versions != expected_artifacts:
        raise ValueError("managed Semgrep artifacts disagree with its inventory")
    inventory_digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
    artifact_digest = hashlib.sha256(canonical_json(artifacts)).hexdigest()
    if payload.get("installed_packages_sha256") != inventory_digest:
        raise ValueError("managed Semgrep inventory digest disagrees")
    if payload.get("install_artifacts_sha256") != artifact_digest:
        raise ValueError("managed Semgrep artifact digest disagrees")
    if payload.get("runtime_digest_sha256") != runtime_digest(payload):
        raise ValueError("managed Semgrep runtime digest disagrees")

    expected_python = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    if payload.get("python_executable") != expected_python:
        raise ValueError("managed Semgrep Python executable is incompatible")
    python = _contained_file(tool_root, payload["python_executable"], label="Python executable")
    wrapper = _contained_file(tool_root, payload["scan_wrapper"], label="scan wrapper")
    if sha256_file(wrapper) != SEMGREP_SCAN_WRAPPER_SHA256:
        raise ValueError("managed Semgrep scan wrapper digest disagrees")
    receipt = tool_root / SEMGREP_TOOL_RECEIPT_NAME
    _regular_file(receipt, label="receipt")
    return ManagedSemgrepRuntime(
        tool_root=tool_root,
        python=python,
        wrapper=wrapper,
        receipt=receipt,
        receipt_sha256=sha256_file(receipt),
        version=SEMGREP_TOOL_VERSION,
    )


def resolve_semgrep_tool_runtime(runtime_root: Path | None = None) -> ManagedSemgrepRuntime:
    """Resolve only the immutable tool runtime owned by the active release."""

    root = Path(sys.prefix) if runtime_root is None else Path(runtime_root)
    tool_root = root / "tools" / "semgrep"
    try:
        metadata = os.lstat(tool_root)
    except OSError as exc:
        raise ValueError("managed Semgrep tool runtime is unavailable") from exc
    if _is_reparse(tool_root, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("managed Semgrep tool runtime is not a regular directory")
    receipt = tool_root / SEMGREP_TOOL_RECEIPT_NAME
    _regular_file(receipt, label="receipt")
    raw = receipt.read_bytes()
    if not raw or len(raw) > _MAX_RECEIPT_BYTES:
        raise ValueError("managed Semgrep receipt size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("managed Semgrep receipt is malformed") from exc
    return validate_semgrep_tool_receipt(payload, tool_root=tool_root)


def managed_semgrep_version(runtime_root: Path | None = None) -> str | None:
    """Return the managed tool version, or None when its receipt is unavailable."""

    try:
        return resolve_semgrep_tool_runtime(runtime_root).version
    except (OSError, TypeError, ValueError):
        return None


__all__ = [
    "PIP_BOOTSTRAP_FILENAME",
    "PIP_BOOTSTRAP_SHA256",
    "PIP_BOOTSTRAP_URL",
    "SEMGREP_SCAN_WRAPPER",
    "SEMGREP_SCAN_WRAPPER_NAME",
    "SEMGREP_SCAN_WRAPPER_SHA256",
    "SEMGREP_TOOL_ALLOWED_SURFACES",
    "SEMGREP_TOOL_CONSTRAINTS_NAME",
    "SEMGREP_TOOL_CONSTRAINTS_SHA256",
    "SEMGREP_TOOL_DENIED_ENTRYPOINTS",
    "SEMGREP_TOOL_MCP_VERSION",
    "SEMGREP_TOOL_PIP_VERSION",
    "SEMGREP_TOOL_RECEIPT_KIND",
    "SEMGREP_TOOL_RECEIPT_NAME",
    "SEMGREP_TOOL_SCHEMA_VERSION",
    "SEMGREP_TOOL_VERSION",
    "SEMGREP_TOOL_VULNERABILITY_EXCEPTIONS",
    "ManagedSemgrepRuntime",
    "canonical_json",
    "managed_semgrep_version",
    "resolve_semgrep_tool_runtime",
    "runtime_digest",
    "sha256_file",
    "validate_semgrep_tool_receipt",
]
