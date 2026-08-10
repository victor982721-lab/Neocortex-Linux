#!/usr/bin/env python3
"""Install and verify the release-owned Pyright runtime from an exact npm lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path


NODE_VERSION = "24.18.1"
PYRIGHT_VERSION = "1.1.411"
PYRIGHT_PACKAGE_INTEGRITY = (
    "sha512-03S/vmS5lF1S/tVbKc2WNXCMq8JWCwta/qIYjj1jvqbQhoy+"
    "N3NgBzHTSmUlbYD6DJwqQ5XHf108QujoqeURvw=="
)
PYRIGHT_MANIFEST_SHA256 = "f57fd2185ace352eb136b73e0a575676b1f83a7b3ad89606eca77c9ab9a673c0"
PYRIGHT_LOCK_SHA256 = "9beef43ab80a3d87868e2fed68d272c612fcbb78f1b95c1f132b9b2164123ee4"
PYRIGHT_LOCK_DIRECTORY = Path(__file__).with_name("pyright_runtime_lock")
PYRIGHT_MANIFEST_NAME = "package.json"
PYRIGHT_LOCK_NAME = "package-lock.json"


class PyrightRuntimeError(RuntimeError):
    """The exact lock-driven Pyright runtime contract was not satisfied."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


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
        raise PyrightRuntimeError(
            f"Pyright runtime command failed ({result.returncode}): {arguments[0]}: {detail}"
        )
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PyrightRuntimeError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise PyrightRuntimeError(f"{label} must be a regular file")


def _json_object(path: Path, *, label: str) -> dict[str, object]:
    _regular_file(path, label=label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PyrightRuntimeError(f"{label} is malformed") from exc
    if not isinstance(payload, dict):
        raise PyrightRuntimeError(f"{label} is malformed")
    return payload


def validate_pyright_lock(
    directory: Path = PYRIGHT_LOCK_DIRECTORY,
) -> tuple[Path, Path]:
    """Validate exact manifest bytes and the locked Pyright package integrity."""

    manifest = directory / PYRIGHT_MANIFEST_NAME
    lock = directory / PYRIGHT_LOCK_NAME
    manifest_payload = _json_object(manifest, label="Pyright npm manifest")
    lock_payload = _json_object(lock, label="Pyright npm lock")
    if _sha256_file(manifest) != PYRIGHT_MANIFEST_SHA256:
        raise PyrightRuntimeError("Pyright npm manifest failed exact SHA-256 validation")
    if _sha256_file(lock) != PYRIGHT_LOCK_SHA256:
        raise PyrightRuntimeError("Pyright npm lock failed exact SHA-256 validation")
    if manifest_payload != {
        "name": "neocortex-pyright-runtime",
        "version": "1.0.0",
        "private": True,
        "engines": {"node": NODE_VERSION},
        "dependencies": {"pyright": PYRIGHT_VERSION},
    }:
        raise PyrightRuntimeError("Pyright npm manifest contract is incompatible")
    packages = lock_payload.get("packages")
    if (
        lock_payload.get("name") != "neocortex-pyright-runtime"
        or lock_payload.get("version") != "1.0.0"
        or lock_payload.get("lockfileVersion") != 3
        or lock_payload.get("requires") is not True
        or not isinstance(packages, dict)
        or set(packages) != {"", "node_modules/fsevents", "node_modules/pyright"}
    ):
        raise PyrightRuntimeError("Pyright npm lock graph is incompatible")
    root = packages[""]
    pyright = packages["node_modules/pyright"]
    fsevents = packages["node_modules/fsevents"]
    if (
        not isinstance(root, dict)
        or root.get("dependencies") != {"pyright": PYRIGHT_VERSION}
        or root.get("engines") != {"node": NODE_VERSION}
        or not isinstance(pyright, dict)
        or pyright.get("version") != PYRIGHT_VERSION
        or pyright.get("resolved")
        != f"https://registry.npmjs.org/pyright/-/pyright-{PYRIGHT_VERSION}.tgz"
        or pyright.get("integrity") != PYRIGHT_PACKAGE_INTEGRITY
        or pyright.get("bin")
        != {"pyright": "index.js", "pyright-langserver": "langserver.index.js"}
        or not isinstance(fsevents, dict)
        or fsevents.get("optional") is not True
        or fsevents.get("os") != ["darwin"]
    ):
        raise PyrightRuntimeError("Pyright npm lock package evidence is incompatible")
    return manifest, lock


def _runtime_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    sanitized = {
        key: value
        for key, value in source.items()
        if not key.casefold().startswith("npm_config_")
        and key.casefold() not in {"node_options", "node_path"}
    }
    sanitized["NPM_CONFIG_AUDIT"] = "false"
    sanitized["NPM_CONFIG_BIN_LINKS"] = "true"
    sanitized["NPM_CONFIG_FUND"] = "false"
    sanitized["NPM_CONFIG_IGNORE_SCRIPTS"] = "true"
    return sanitized


def _prefer_node_on_path(environment: dict[str, str], node: str) -> None:
    node_directory = os.fspath(Path(node).parent)
    current_path = environment.get("PATH", "")
    environment["PATH"] = os.pathsep.join(item for item in (node_directory, current_path) if item)


def _resolve_command(candidate: str | os.PathLike[str] | None, name: str) -> str:
    if candidate is None:
        resolved = shutil.which(name)
        if resolved is None and os.name == "nt":
            resolved = shutil.which(f"{name}.cmd")
    else:
        value = os.fspath(candidate)
        resolved = shutil.which(value) if not Path(value).is_absolute() else value
    if resolved is None or not Path(resolved).is_file():
        raise PyrightRuntimeError(f"canonical {name} executable is unavailable")
    return resolved


def _require_node_version(
    node: str,
    *,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> str:
    observed = runner((node, "--version"), timeout=60, environment=environment).stdout.strip()
    if observed != f"v{NODE_VERSION}":
        raise PyrightRuntimeError(f"unexpected Node version for Pyright runtime: {observed}")
    return observed


def _installed_lock(target: Path) -> dict[str, object]:
    payload = _json_object(
        target / "node_modules" / ".package-lock.json",
        label="installed Pyright npm lock",
    )
    packages = payload.get("packages")
    pyright = packages.get("node_modules/pyright") if isinstance(packages, dict) else None
    if (
        payload.get("lockfileVersion") != 3
        or not isinstance(packages, dict)
        or set(packages) != {"node_modules/pyright"}
        or not isinstance(pyright, dict)
        or pyright.get("version") != PYRIGHT_VERSION
        or pyright.get("resolved")
        != f"https://registry.npmjs.org/pyright/-/pyright-{PYRIGHT_VERSION}.tgz"
        or pyright.get("integrity") != PYRIGHT_PACKAGE_INTEGRITY
        or pyright.get("bin")
        != {"pyright": "index.js", "pyright-langserver": "langserver.index.js"}
    ):
        raise PyrightRuntimeError("installed Pyright npm lock is incompatible")
    return payload


def verify_pyright_runtime(
    target: Path,
    *,
    node: str | os.PathLike[str] | None = None,
    runner: CommandRunner = _run,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Verify the copied lock, installed graph, Node and live Pyright version."""

    target = target.expanduser().absolute()
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise PyrightRuntimeError("Pyright runtime directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise PyrightRuntimeError("Pyright runtime must be a real directory")
    validate_pyright_lock(target)
    installed_manifest = _json_object(
        target / "node_modules" / "pyright" / "package.json",
        label="installed Pyright package manifest",
    )
    if (
        installed_manifest.get("name") != "pyright"
        or installed_manifest.get("version") != PYRIGHT_VERSION
    ):
        raise PyrightRuntimeError("installed Pyright package version is incompatible")
    _installed_lock(target)
    if (target / "node_modules" / "fsevents").exists():
        raise PyrightRuntimeError("optional fsevents escaped the Linux/Windows Pyright runtime")
    shim = target / "node_modules" / ".bin" / ("pyright.cmd" if os.name == "nt" else "pyright")
    node_command = _resolve_command(node, "node")
    runtime_environment = _runtime_environment(environment)
    _prefer_node_on_path(runtime_environment, node_command)
    node_version = _require_node_version(
        node_command,
        runner=runner,
        environment=runtime_environment,
    )
    index = target / "node_modules" / "pyright" / "index.js"
    _regular_file(index, label="installed Pyright entrypoint")
    if os.name == "nt":
        _regular_file(shim, label="Pyright console shim")
    else:
        try:
            if not shim.is_symlink() or shim.resolve(strict=True) != index.resolve(strict=True):
                raise PyrightRuntimeError("Pyright console shim escapes its locked entrypoint")
        except OSError as exc:
            raise PyrightRuntimeError("Pyright console shim is unavailable") from exc
    pyright_version = runner(
        (node_command, index, "--version"),
        timeout=60,
        environment=runtime_environment,
    ).stdout.strip()
    if pyright_version != f"pyright {PYRIGHT_VERSION}":
        raise PyrightRuntimeError(f"unexpected Pyright version: {pyright_version}")
    return {
        "node": node_version,
        "pyright": pyright_version,
        "pyright_integrity": PYRIGHT_PACKAGE_INTEGRITY,
        "pyright_lock_sha256": PYRIGHT_LOCK_SHA256,
    }


def install_pyright_runtime(
    target: Path,
    *,
    npm: str | os.PathLike[str] | None = None,
    node: str | os.PathLike[str] | None = None,
    runner: CommandRunner = _run,
    environment: Mapping[str, str] | None = None,
    lock_directory: Path = PYRIGHT_LOCK_DIRECTORY,
) -> dict[str, str]:
    """Install a new contained runtime with ``npm ci`` from the exact lock."""

    manifest, lock = validate_pyright_lock(lock_directory)
    target = target.expanduser().absolute()
    if os.path.lexists(target):
        raise PyrightRuntimeError("Pyright runtime target already exists")
    npm_command = _resolve_command(npm, "npm")
    node_command = _resolve_command(node, "node")
    runtime_environment = _runtime_environment(environment)
    _prefer_node_on_path(runtime_environment, node_command)
    _require_node_version(
        node_command,
        runner=runner,
        environment=runtime_environment,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    try:
        shutil.copyfile(manifest, target / PYRIGHT_MANIFEST_NAME)
        shutil.copyfile(lock, target / PYRIGHT_LOCK_NAME)
        runner(
            (
                npm_command,
                "ci",
                "--prefix",
                target,
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--omit=optional",
            ),
            timeout=1800,
            environment=runtime_environment,
        )
        return verify_pyright_runtime(
            target,
            node=node_command,
            runner=runner,
            environment=runtime_environment,
        )
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--target", type=Path, required=True)
        command.add_argument("--node")
    subparsers.choices["install"].add_argument("--npm")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "install":
            result = install_pyright_runtime(
                arguments.target,
                npm=arguments.npm,
                node=arguments.node,
            )
        else:
            result = verify_pyright_runtime(arguments.target, node=arguments.node)
    except (OSError, PyrightRuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NODE_VERSION",
    "PYRIGHT_LOCK_SHA256",
    "PYRIGHT_MANIFEST_SHA256",
    "PYRIGHT_PACKAGE_INTEGRITY",
    "PYRIGHT_VERSION",
    "PyrightRuntimeError",
    "install_pyright_runtime",
    "main",
    "validate_pyright_lock",
    "verify_pyright_runtime",
]
