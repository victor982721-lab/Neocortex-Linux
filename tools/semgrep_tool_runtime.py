#!/usr/bin/env python3
"""Install and verify the isolated, scan-only Semgrep tool runtime.

The NeoCortex application environment deliberately does not depend on Semgrep.
Semgrep 1.172.0 requires an older MCP release, so the canonical release owns a
separate exact-pinned virtual environment and exposes only a fixed scan wrapper.
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
import urllib.parse
import uuid
import venv
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

from neocortex.code import pip_bootstrap
from neocortex.code.semgrep_tool_contract import (
    PIP_BOOTSTRAP_FILENAME,
    PIP_BOOTSTRAP_SHA256,
    PIP_BOOTSTRAP_URL,
    SEMGREP_SCAN_WRAPPER,
    SEMGREP_SCAN_WRAPPER_NAME,
    SEMGREP_TOOL_ALLOWED_SURFACES,
    SEMGREP_TOOL_CONSTRAINTS_NAME,
    SEMGREP_TOOL_CONSTRAINTS_SHA256,
    SEMGREP_TOOL_DENIED_ENTRYPOINTS,
    SEMGREP_TOOL_MCP_VERSION,
    SEMGREP_TOOL_PIP_VERSION,
    SEMGREP_TOOL_RECEIPT_KIND,
    SEMGREP_TOOL_RECEIPT_NAME,
    SEMGREP_TOOL_SCHEMA_VERSION,
    SEMGREP_TOOL_VERSION,
    SEMGREP_TOOL_VULNERABILITY_EXCEPTIONS,
    canonical_json,
    resolve_semgrep_tool_runtime,
    runtime_digest,
    sha256_file,
)

_PIP_WHEEL_RUNNER = pip_bootstrap.PIP_WHEEL_RUNNER
_INVENTORY_SCRIPT = (
    "import importlib.metadata as m,json,re;"
    "rows=[{'name':re.sub(r'[-_.]+','-',d.metadata['Name']).lower(),"
    "'version':d.version} for d in m.distributions()];"
    "print(json.dumps(sorted(rows,key=lambda x:x['name']),"
    "sort_keys=True,separators=(',',':')))"
)
_CANONICAL_NAME = re.compile(r"[-_.]+")


class SemgrepToolRuntimeError(RuntimeError):
    """The isolated Semgrep runtime failed a strict installation contract."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Downloader = Callable[[str, Path], None]


def _run(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [os.fspath(argument) for argument in arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=None if environment is None else dict(environment),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise SemgrepToolRuntimeError(
            f"command failed ({result.returncode}): {arguments[0]}: {detail}"
        )
    return result


_download = pip_bootstrap.download_pip_bootstrap


def _tool_root(runtime_root: Path) -> Path:
    return runtime_root / "tools" / "semgrep"


def _tool_python(tool_root: Path) -> Path:
    return tool_root / "Scripts" / "python.exe" if os.name == "nt" else tool_root / "bin" / "python"


def _canonical_name(value: str) -> str:
    return _CANONICAL_NAME.sub("-", value).lower()


def _require_supported_python() -> None:
    if sys.implementation.name != "cpython" or sys.version_info[:2] not in {(3, 13), (3, 14)}:
        raise SemgrepToolRuntimeError("Semgrep tool runtimes require CPython 3.13 or 3.14")


def _require_constraints(path: Path) -> None:
    if path.name != SEMGREP_TOOL_CONSTRAINTS_NAME:
        raise SemgrepToolRuntimeError("Semgrep constraints filename is incompatible")
    try:
        observed = sha256_file(path)
    except OSError as exc:
        raise SemgrepToolRuntimeError("Semgrep constraints are unavailable") from exc
    if observed != SEMGREP_TOOL_CONSTRAINTS_SHA256:
        raise SemgrepToolRuntimeError("Semgrep constraints failed exact SHA-256 validation")


def require_pip_bootstrap(path: Path) -> None:
    """Reject any initial pip wheel not matching the canonical content hash."""

    try:
        pip_bootstrap.require_pip_bootstrap(
            path,
            filename=PIP_BOOTSTRAP_FILENAME,
            sha256=PIP_BOOTSTRAP_SHA256,
        )
    except pip_bootstrap.PipBootstrapError as exc:
        raise SemgrepToolRuntimeError(str(exc)) from exc


def prepare_pip_bootstrap(
    workspace: Path,
    *,
    downloader: Downloader = _download,
) -> Path:
    """Download and authenticate pip without executing the venv-bundled copy."""

    try:
        return pip_bootstrap.prepare_pip_bootstrap(
            workspace,
            downloader=downloader,
            filename=PIP_BOOTSTRAP_FILENAME,
            url=PIP_BOOTSTRAP_URL,
            sha256=PIP_BOOTSTRAP_SHA256,
        )
    except pip_bootstrap.PipBootstrapError as exc:
        raise SemgrepToolRuntimeError(str(exc)) from exc


def _bootstrap_environment(
    tool_root: Path,
    pip_wheel: Path,
    *,
    runner: CommandRunner,
) -> None:
    require_pip_bootstrap(pip_wheel)
    try:
        # A contained, regular interpreter lets release and gate checks reject
        # executable escapes rather than accepting venv symlinks to a host Python.
        python = pip_bootstrap.create_pip_environment(
            tool_root,
            pip_wheel,
            runner=runner,
            symlinks=False,
            builder_factory=venv.EnvBuilder,
            expected_version=SEMGREP_TOOL_PIP_VERSION,
            filename=PIP_BOOTSTRAP_FILENAME,
            sha256=PIP_BOOTSTRAP_SHA256,
        )
    except pip_bootstrap.PipBootstrapError as exc:
        raise SemgrepToolRuntimeError(str(exc)) from exc
    if python != _tool_python(tool_root):
        raise SemgrepToolRuntimeError("pip bootstrap selected an incompatible tool interpreter")


def _remove_denied_entrypoints(tool_root: Path) -> None:
    scripts = tool_root / ("Scripts" if os.name == "nt" else "bin")
    if not scripts.is_dir():
        raise SemgrepToolRuntimeError("Semgrep tool scripts directory is unavailable")
    for item in scripts.iterdir():
        folded = item.name.casefold()
        denied = any(
            folded == name or folded.startswith((f"{name}.", f"{name}-"))
            for name in SEMGREP_TOOL_DENIED_ENTRYPOINTS
        )
        if denied:
            if not item.is_file() or item.is_symlink():
                raise SemgrepToolRuntimeError("denied Semgrep entrypoint is not a regular file")
            item.unlink()


def _require_no_denied_entrypoints(tool_root: Path) -> None:
    scripts = tool_root / ("Scripts" if os.name == "nt" else "bin")
    try:
        entries = tuple(scripts.iterdir())
    except OSError as exc:
        raise SemgrepToolRuntimeError("Semgrep tool scripts directory is unavailable") from exc
    for item in entries:
        folded = item.name.casefold()
        if any(
            folded == name or folded.startswith((f"{name}.", f"{name}-"))
            for name in SEMGREP_TOOL_DENIED_ENTRYPOINTS
        ):
            raise SemgrepToolRuntimeError(f"denied console entrypoint remains exposed: {item.name}")


def _inventory(python: Path, *, runner: CommandRunner) -> list[dict[str, str]]:
    raw = runner((python, "-I", "-c", _INVENTORY_SCRIPT), timeout=120).stdout
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SemgrepToolRuntimeError("Semgrep inventory output is malformed") from exc
    if not isinstance(payload, list) or not payload:
        raise SemgrepToolRuntimeError("Semgrep inventory is empty")
    rows: list[dict[str, str]] = []
    names: set[str] = set()
    for item in payload:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "version"}
            or not isinstance(item["name"], str)
            or not isinstance(item["version"], str)
        ):
            raise SemgrepToolRuntimeError("Semgrep inventory row is malformed")
        name = _canonical_name(item["name"])
        if not name or name in names:
            raise SemgrepToolRuntimeError("Semgrep inventory contains duplicate packages")
        names.add(name)
        rows.append({"name": name, "version": item["version"]})
    return sorted(rows, key=lambda item: item["name"])


def _artifacts(report: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemgrepToolRuntimeError("pip installation report is malformed") from exc
    installs = payload.get("install") if isinstance(payload, dict) else None
    if not isinstance(installs, list) or not installs:
        raise SemgrepToolRuntimeError("pip installation report has no artifacts")
    rows: list[dict[str, str]] = []
    names: set[str] = set()
    for item in installs:
        if not isinstance(item, dict):
            raise SemgrepToolRuntimeError("pip installation report row is malformed")
        metadata = item.get("metadata")
        download = item.get("download_info")
        archive = download.get("archive_info") if isinstance(download, dict) else None
        hashes = archive.get("hashes") if isinstance(archive, dict) else None
        url = download.get("url") if isinstance(download, dict) else None
        name_value = metadata.get("name") if isinstance(metadata, dict) else None
        version = metadata.get("version") if isinstance(metadata, dict) else None
        sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
        if (
            not isinstance(name_value, str)
            or not name_value
            or not isinstance(version, str)
            or not version
            or not isinstance(url, str)
            or not url
            or not isinstance(sha256, str)
            or not sha256
        ):
            raise SemgrepToolRuntimeError("pip installation artifact identity is incomplete")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise SemgrepToolRuntimeError("pip installation artifact SHA-256 is malformed")
        filename = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        name = _canonical_name(name_value)
        if not filename or name in names:
            raise SemgrepToolRuntimeError("pip installation artifact is duplicated")
        names.add(name)
        rows.append(
            {
                "filename": filename,
                "name": name,
                "sha256": sha256,
                "version": version,
            }
        )
    return sorted(rows, key=lambda item: item["name"])


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _receipt(
    *,
    tool_root: Path,
    inventory: list[dict[str, str]],
    artifacts: list[dict[str, str]],
) -> dict[str, object]:
    inventory_versions = {item["name"]: item["version"] for item in inventory}
    expected = {
        "mcp": SEMGREP_TOOL_MCP_VERSION,
        "pip": SEMGREP_TOOL_PIP_VERSION,
        "semgrep": SEMGREP_TOOL_VERSION,
    }
    if any(inventory_versions.get(name) != version for name, version in expected.items()):
        raise SemgrepToolRuntimeError("isolated Semgrep package versions are incompatible")
    artifact_versions = {item["name"]: item["version"] for item in artifacts}
    if artifact_versions != {
        name: version for name, version in inventory_versions.items() if name != "pip"
    }:
        raise SemgrepToolRuntimeError("pip report disagrees with the installed inventory")
    python_relative = _tool_python(tool_root).relative_to(tool_root).as_posix()
    payload: dict[str, object] = {
        "schema_version": SEMGREP_TOOL_SCHEMA_VERSION,
        "kind": SEMGREP_TOOL_RECEIPT_KIND,
        "tool": "semgrep",
        "version": SEMGREP_TOOL_VERSION,
        "python_version": platform.python_version(),
        "platform": sysconfig.get_platform(),
        "python_executable": python_relative,
        "scan_wrapper": SEMGREP_SCAN_WRAPPER_NAME,
        "scan_wrapper_sha256": hashlib.sha256(SEMGREP_SCAN_WRAPPER).hexdigest(),
        "constraints_filename": SEMGREP_TOOL_CONSTRAINTS_NAME,
        "constraints_sha256": SEMGREP_TOOL_CONSTRAINTS_SHA256,
        "pip_bootstrap_version": SEMGREP_TOOL_PIP_VERSION,
        "pip_bootstrap_filename": PIP_BOOTSTRAP_FILENAME,
        "pip_bootstrap_sha256": PIP_BOOTSTRAP_SHA256,
        "installed_packages": inventory,
        "installed_packages_sha256": hashlib.sha256(canonical_json(inventory)).hexdigest(),
        "install_artifacts": artifacts,
        "install_artifacts_sha256": hashlib.sha256(canonical_json(artifacts)).hexdigest(),
        "runtime_digest_sha256": "",
        "allowed_surfaces": list(SEMGREP_TOOL_ALLOWED_SURFACES),
        "denied_console_entrypoints": list(SEMGREP_TOOL_DENIED_ENTRYPOINTS),
        "vulnerability_exceptions": [dict(item) for item in SEMGREP_TOOL_VULNERABILITY_EXCEPTIONS],
    }
    payload["runtime_digest_sha256"] = runtime_digest(payload)
    return payload


def install_semgrep_tool_runtime(
    runtime_root: Path,
    *,
    pip_wheel: Path | None = None,
    constraints: Path | None = None,
    runner: CommandRunner = _run,
    downloader: Downloader = _download,
) -> Path:
    """Install one exact isolated tool environment and return its receipt."""

    _require_supported_python()
    runtime_root = runtime_root.expanduser().absolute()
    constraints_path = (
        Path(__file__).with_name(SEMGREP_TOOL_CONSTRAINTS_NAME)
        if constraints is None
        else constraints
    )
    _require_constraints(constraints_path)
    runtime_root.mkdir(parents=True, exist_ok=True)
    tool_root = _tool_root(runtime_root)
    if os.path.lexists(tool_root):
        raise SemgrepToolRuntimeError("Semgrep tool runtime already exists")
    tool_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="neocortex-semgrep-tool-") as temporary:
        workspace = Path(temporary)
        bootstrap = (
            prepare_pip_bootstrap(workspace, downloader=downloader)
            if pip_wheel is None
            else pip_wheel
        )
        require_pip_bootstrap(bootstrap)
        report = workspace / "semgrep-install-report.json"
        try:
            _bootstrap_environment(tool_root, bootstrap, runner=runner)
            python = _tool_python(tool_root)
            runner(
                (
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--only-binary=:all:",
                    "--constraint",
                    constraints_path,
                    "--report",
                    report,
                    f"semgrep=={SEMGREP_TOOL_VERSION}",
                ),
                timeout=1800,
            )
            _remove_denied_entrypoints(tool_root)
            wrapper = tool_root / SEMGREP_SCAN_WRAPPER_NAME
            _atomic_write(wrapper, SEMGREP_SCAN_WRAPPER)
            inventory = _inventory(python, runner=runner)
            artifacts = _artifacts(report)
            receipt_payload = _receipt(
                tool_root=tool_root,
                inventory=inventory,
                artifacts=artifacts,
            )
            receipt = tool_root / SEMGREP_TOOL_RECEIPT_NAME
            _atomic_write(receipt, canonical_json(receipt_payload))
            verify_semgrep_tool_runtime(runtime_root, runner=runner)
        except BaseException:
            shutil.rmtree(tool_root, ignore_errors=True)
            raise
    return tool_root / SEMGREP_TOOL_RECEIPT_NAME


def verify_semgrep_tool_runtime(
    runtime_root: Path,
    *,
    runner: CommandRunner = _run,
) -> dict[str, str]:
    """Validate receipt, containment, live inventory and scan-only exposure."""

    try:
        runtime = resolve_semgrep_tool_runtime(runtime_root.expanduser().absolute())
    except (OSError, TypeError, ValueError) as exc:
        raise SemgrepToolRuntimeError(str(exc)) from exc
    _require_no_denied_entrypoints(runtime.tool_root)
    runner((runtime.python, "-m", "pip", "check"), timeout=300)
    observed_version = runner(
        (runtime.python, "-I", runtime.wrapper, "--neocortex-tool-version"),
        timeout=60,
    ).stdout.strip()
    if observed_version != SEMGREP_TOOL_VERSION:
        raise SemgrepToolRuntimeError(f"unexpected Semgrep wrapper version: {observed_version}")
    observed_inventory = _inventory(runtime.python, runner=runner)
    try:
        receipt_payload = json.loads(runtime.receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemgrepToolRuntimeError("Semgrep receipt changed during verification") from exc
    if receipt_payload.get("installed_packages") != observed_inventory:
        raise SemgrepToolRuntimeError("Semgrep live inventory disagrees with its receipt")
    return {
        "receipt": str(runtime.receipt),
        "receipt_sha256": runtime.receipt_sha256,
        "runtime_digest_sha256": str(receipt_payload["runtime_digest_sha256"]),
        "semgrep": observed_version,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--runtime-root", type=Path, required=True)
    install = subparsers.choices["install"]
    install.add_argument("--pip-wheel", type=Path)
    install.add_argument("--constraints", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "install":
            receipt = install_semgrep_tool_runtime(
                arguments.runtime_root,
                pip_wheel=arguments.pip_wheel,
                constraints=arguments.constraints,
            )
            result: Mapping[str, object] = {
                "status": "installed",
                **verify_semgrep_tool_runtime(arguments.runtime_root),
                "receipt": str(receipt),
            }
        else:
            result = {
                "status": "verified",
                **verify_semgrep_tool_runtime(arguments.runtime_root),
            }
    except (OSError, SemgrepToolRuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
