"""Versioned, per-user NeoCortex release installation for Kubuntu/Linux.

The tool never modifies global Python or Node installations. It builds a wheel,
creates an immutable release-local virtual environment, installs the pinned
Node/Pyright runtime, verifies it, and only then atomically updates ``current``
under a POSIX ``flock``. Previous releases are deliberately retained.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import venv
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

from neocortex import __version__, pip_bootstrap
from neocortex.pip_bootstrap import (
    PIP_BOOTSTRAP_FILENAME,
    PIP_BOOTSTRAP_SHA256,
    PIP_BOOTSTRAP_URL,
    PIP_BOOTSTRAP_VERSION,
)
from neocortex.platform_policy import PlatformPolicy, current_platform_policy
from neocortex.semgrep_tool_contract import (
    SEMGREP_TOOL_VERSION,
)
from tools.build_binary_inputs import build_source_only_wheels
from tools.pyright_runtime import (
    NODE_VERSION,
    PYRIGHT_LOCK_SHA256,
    PYRIGHT_PACKAGE_INTEGRITY,
    PYRIGHT_VERSION,
    PyrightRuntimeError,
    install_pyright_runtime,
    verify_pyright_runtime,
)
from tools.semgrep_tool_runtime import (
    SemgrepToolRuntimeError,
    install_semgrep_tool_runtime,
    verify_semgrep_tool_runtime,
)

RECEIPT_SCHEMA_VERSION = 1
RELEASE_PLATFORM_TAG = "linux-x86_64"
RELEASE_MANIFEST_NAME = "neocortex-release.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMPORT_MODULES = (
    "PIL",
    "PySide6",
    "ctranslate2",
    "fastembed",
    "faster_whisper",
    "fitz",
    "nudenet",
    "numpy",
    "pytesseract",
)


class LinuxReleaseError(RuntimeError):
    """A required release safety or verification condition failed."""


@dataclass(frozen=True, slots=True)
class LinuxReleaseLayout:
    source_root: Path
    policy: PlatformPolicy

    @property
    def releases(self) -> Path:
        return self.policy.releases_directory

    @property
    def current(self) -> Path:
        return self.policy.current_release

    @property
    def state(self) -> Path:
        return self.policy.state_directory

    @property
    def receipts(self) -> Path:
        return self.state / "installation-receipts"

    @property
    def lock(self) -> Path:
        return self.state / "release.lock"

    @property
    def staging(self) -> Path:
        return self.releases / ".staging"

    @property
    def launcher(self) -> Path:
        return self.policy.stable_launcher

    @property
    def alias(self) -> Path:
        return self.policy.user_alias

    @property
    def desktop(self) -> Path:
        return self.policy.desktop_file

    @property
    def icon(self) -> Path:
        return self.policy.data_directory / "icons" / "neocortex-app-icon.png"


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [os.fspath(argument) for argument in arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise LinuxReleaseError(f"command failed ({result.returncode}): {arguments[0]}: {detail}")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _source_sha(source_root: Path, runner: CommandRunner = _run) -> str:
    result = runner(
        ("git", "-C", source_root, "rev-parse", "HEAD"),
        timeout=30,
    )
    sha = result.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise LinuxReleaseError("source Git SHA is malformed")
    dirty = runner(
        ("git", "-C", source_root, "status", "--porcelain"),
        timeout=30,
    ).stdout
    if dirty.strip():
        raise LinuxReleaseError("source tree must be clean before creating a release")
    return sha


def _require_reference_platform() -> None:
    if os.name != "posix" or sys.platform != "linux":
        raise LinuxReleaseError("Linux release tooling is available only on Linux")
    if platform.machine() not in {"x86_64", "AMD64"}:
        raise LinuxReleaseError("Linux releases require x86_64")
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 14):
        raise LinuxReleaseError("Linux releases require CPython 3.14")


def release_id(source_sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("release source SHA must contain 40 lowercase hex digits")
    return f"{__version__}-{source_sha[:12]}-cp314-{RELEASE_PLATFORM_TAG}"


def _venv_python(root: Path) -> Path:
    return root / "bin" / "python"


def _venv_command(root: Path) -> Path:
    return root / "bin" / "Neocortex"


def _prepare_pip_bootstrap(workspace: Path) -> Path:
    """Download the pinned pip wheel without invoking the bundled venv pip."""

    try:
        return pip_bootstrap.prepare_pip_bootstrap(
            workspace,
            downloader=_download,
            filename=PIP_BOOTSTRAP_FILENAME,
            url=PIP_BOOTSTRAP_URL,
            sha256=PIP_BOOTSTRAP_SHA256,
        )
    except pip_bootstrap.PipBootstrapError as exc:
        raise LinuxReleaseError(str(exc)) from exc


def _require_pip_bootstrap(wheel: Path) -> None:
    try:
        pip_bootstrap.require_pip_bootstrap(
            wheel,
            filename=PIP_BOOTSTRAP_FILENAME,
            sha256=PIP_BOOTSTRAP_SHA256,
        )
    except pip_bootstrap.PipBootstrapError as exc:
        raise LinuxReleaseError(str(exc)) from exc


_PIP_WHEEL_RUNNER = pip_bootstrap.PIP_WHEEL_RUNNER


def _create_pip_environment(
    root: Path,
    pip_wheel: Path,
    *,
    runner: CommandRunner = _run,
) -> None:
    """Create a venv and seed only the verified pip wheel into it."""

    _require_pip_bootstrap(pip_wheel)
    try:
        python = pip_bootstrap.create_pip_environment(
            root,
            pip_wheel,
            runner=runner,
            symlinks=True,
            builder_factory=venv.EnvBuilder,
            expected_version=PIP_BOOTSTRAP_VERSION,
            filename=PIP_BOOTSTRAP_FILENAME,
            sha256=PIP_BOOTSTRAP_SHA256,
        )
    except pip_bootstrap.PipBootstrapError as exc:
        raise LinuxReleaseError(str(exc)) from exc
    if python != _venv_python(root):
        raise LinuxReleaseError("pip bootstrap selected an incompatible release interpreter")


def _build_wheel(
    layout: LinuxReleaseLayout,
    workspace: Path,
    *,
    pip_wheel: Path,
    runner: CommandRunner = _run,
) -> tuple[Path, tuple[Path, ...]]:
    build_environment = workspace / "build-environment"
    _create_pip_environment(build_environment, pip_wheel, runner=runner)
    python = _venv_python(build_environment)
    constraints = layout.source_root / "constraints.txt"
    runner(
        (
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--constraint",
            constraints,
            "build==1.5.0",
            "setuptools==83.0.0",
            "wheel",
        ),
        timeout=900,
    )
    wheelhouse = workspace / "wheelhouse"
    wheelhouse.mkdir()
    source_only_wheels = build_source_only_wheels(
        python,
        wheelhouse,
        constraints,
        runner=runner,
    )
    runner(
        (
            python,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            wheelhouse,
            layout.source_root,
        ),
        timeout=900,
    )
    wheels = tuple(wheelhouse.glob("neocortex_framework-*.whl"))
    if len(wheels) != 1:
        raise LinuxReleaseError("wheel build did not produce exactly one artifact")
    return wheels[0], source_only_wheels


def _install_wheel(
    release_root: Path,
    wheel: Path,
    constraints: Path,
    *,
    pip_wheel: Path,
    runner: CommandRunner = _run,
) -> None:
    _create_pip_environment(release_root, pip_wheel, runner=runner)
    runner(
        (
            _venv_python(release_root),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--find-links",
            wheel.parent,
            "--constraint",
            constraints,
            f"{wheel}[full]",
        ),
        timeout=3600,
    )


def _install_semgrep_runtime(
    release_root: Path,
    pip_wheel: Path,
    *,
    runner: CommandRunner = _run,
) -> None:
    """Install the exact scan-only tool env without polluting the main venv."""

    try:
        install_semgrep_tool_runtime(
            release_root,
            pip_wheel=pip_wheel,
            constraints=Path(__file__).with_name("semgrep_tool_constraints.txt"),
            runner=runner,
        )
    except SemgrepToolRuntimeError as exc:
        raise LinuxReleaseError(f"Semgrep tool runtime installation failed: {exc}") from exc


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "NeoCortex-release/1"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("xb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())


def _node_archive(workspace: Path) -> tuple[Path, str]:
    filename = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
    base = f"https://nodejs.org/dist/v{NODE_VERSION}"
    archive = workspace / filename
    sums = workspace / "SHASUMS256.txt"
    _download(f"{base}/{filename}", archive)
    _download(f"{base}/SHASUMS256.txt", sums)
    expected: str | None = None
    for line in sums.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == filename:
            expected = fields[0]
            break
    if expected is None or not _SHA256.fullmatch(expected):
        raise LinuxReleaseError("Node checksum manifest lacks the requested archive")
    actual = _sha256_file(archive)
    if actual != expected:
        raise LinuxReleaseError("Node archive SHA-256 does not match its manifest")
    return archive, actual


def _install_node_pyright(
    release_root: Path,
    workspace: Path,
    *,
    runner: CommandRunner = _run,
) -> str:
    archive, archive_sha = _node_archive(workspace)
    tools_root = release_root / "tools"
    tools_root.mkdir()
    with tarfile.open(archive, mode="r:xz") as source:
        source.extractall(workspace / "node-extract", filter="data")
    extracted = workspace / "node-extract" / f"node-v{NODE_VERSION}-linux-x64"
    if not (extracted / "bin" / "node").is_file():
        raise LinuxReleaseError("Node archive did not contain the expected runtime")
    shutil.move(extracted, tools_root / "node")
    node_bin = tools_root / "node" / "bin"
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(node_bin), environment.get("PATH", "")))
    try:
        install_pyright_runtime(
            tools_root / "pyright",
            npm=node_bin / "npm",
            node=node_bin / "node",
            runner=runner,
            environment=environment,
        )
    except PyrightRuntimeError as exc:
        raise LinuxReleaseError(f"Pyright runtime installation failed: {exc}") from exc
    return archive_sha


def _candidate_environment(
    layout: LinuxReleaseLayout,
    corpus_root: Path,
    *,
    release_root: Path | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment["NEOCORTEX_CORPUS_ROOT"] = str(corpus_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["XDG_CONFIG_HOME"] = str(layout.policy.config_directory.parent)
    environment["XDG_STATE_HOME"] = str(layout.policy.state_directory.parents[1])
    environment["XDG_DATA_HOME"] = str(layout.policy.data_directory.parent)
    if release_root is not None:
        owned_paths = (
            release_root / "tools" / "pyright" / "node_modules" / ".bin",
            release_root / "tools" / "node" / "bin",
        )
        environment["PATH"] = os.pathsep.join(
            (*(str(path) for path in owned_paths), environment.get("PATH", ""))
        )
    return environment


def _verify_python_release(
    release_root: Path,
    layout: LinuxReleaseLayout,
    corpus_root: Path,
    *,
    runner: CommandRunner = _run,
) -> dict[str, str]:
    environment = _candidate_environment(layout, corpus_root, release_root=release_root)
    python = _venv_python(release_root)
    runner((python, "-m", "pip", "check"), timeout=300, environment=environment)
    pip_version = runner(
        (python, "-I", "-c", "import pip; print(pip.__version__)"),
        timeout=60,
        environment=environment,
    ).stdout.strip()
    if pip_version != PIP_BOOTSTRAP_VERSION:
        raise LinuxReleaseError(f"unexpected release pip version: {pip_version}")
    try:
        semgrep_runtime = verify_semgrep_tool_runtime(release_root, runner=runner)
    except SemgrepToolRuntimeError as exc:
        raise LinuxReleaseError(f"Semgrep tool runtime verification failed: {exc}") from exc
    runner(
        (python, "-c", ";".join(f"import {module}" for module in _IMPORT_MODULES)),
        timeout=300,
        environment=environment,
    )
    node = release_root / "tools" / "node" / "bin" / "node"
    try:
        pyright_runtime = verify_pyright_runtime(
            release_root / "tools" / "pyright",
            node=node,
            runner=runner,
            environment=environment,
        )
    except PyrightRuntimeError as exc:
        raise LinuxReleaseError(f"Pyright runtime verification failed: {exc}") from exc
    runner((_venv_command(release_root), "--version"), timeout=60, environment=environment)
    runner(
        (_venv_command(release_root), "doctor", "platform", "--json"),
        timeout=60,
        environment=environment,
    )
    runner(
        (
            python,
            "-c",
            "from PySide6.QtWidgets import QApplication; a=QApplication([]); assert a is not None",
        ),
        timeout=120,
        environment={**environment, "QT_QPA_PLATFORM": "offscreen"},
    )
    return {
        "node": pyright_runtime["node"],
        "pip": pip_version,
        "pyright": pyright_runtime["pyright"],
        "pyright_integrity": pyright_runtime["pyright_integrity"],
        "pyright_lock_sha256": pyright_runtime["pyright_lock_sha256"],
        "semgrep": semgrep_runtime["semgrep"],
        "semgrep_runtime_sha256": semgrep_runtime["runtime_digest_sha256"],
    }


def _make_immutable(root: Path) -> None:
    for current_root, directories, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(current_root) / name
            if path.is_symlink():
                continue
            mode = path.stat(follow_symlinks=False).st_mode
            executable = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            path.chmod(0o555 if executable else 0o444)
        for name in directories:
            path = Path(current_root) / name
            if not path.is_symlink():
                path.chmod(0o555)
    root.chmod(0o555)


def _require_immutable(root: Path) -> None:
    for current_root, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            path = Path(current_root) / name
            if not path.is_symlink() and path.stat(follow_symlinks=False).st_mode & 0o222:
                raise LinuxReleaseError(f"release artifact remains writable: {path}")
    if root.stat(follow_symlinks=False).st_mode & 0o222:
        raise LinuxReleaseError(f"release root remains writable: {root}")


def _remove_incomplete_release(root: Path) -> None:
    if not os.path.lexists(root):
        return
    if root.is_symlink() or not root.is_dir():
        raise LinuxReleaseError(f"incomplete release path is unsafe: {root}")
    root.chmod(0o755)
    for current_root, directories, _files in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(current_root) / name
            if not path.is_symlink():
                path.chmod(0o755)
    shutil.rmtree(root)


@contextmanager
def _release_lock(layout: LinuxReleaseLayout):
    layout.lock.parent.mkdir(parents=True, exist_ok=True)
    with layout.lock.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield stream
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _current_target(layout: LinuxReleaseLayout) -> Path | None:
    if not os.path.lexists(layout.current):
        return None
    if not layout.current.is_symlink():
        raise LinuxReleaseError("current must be a symbolic link")
    target = Path(os.readlink(layout.current))
    if not target.is_absolute():
        target = layout.current.parent / target
    resolved = target.resolve(strict=True)
    try:
        resolved.relative_to(layout.releases.resolve(strict=True))
    except ValueError as exc:
        raise LinuxReleaseError("current points outside the releases directory") from exc
    return resolved


def _replace_current(layout: LinuxReleaseLayout, target: Path | None) -> None:
    layout.current.parent.mkdir(parents=True, exist_ok=True)
    temporary = layout.current.parent / f".current.{uuid.uuid4().hex}"
    try:
        if target is None:
            layout.current.unlink(missing_ok=True)
        else:
            os.symlink(os.path.relpath(target, layout.current.parent), temporary)
            os.replace(temporary, layout.current)
        _fsync_directory(layout.current.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _launcher_payload(corpus_root: Path, current_release: Path) -> bytes:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        f"export NEOCORTEX_CORPUS_ROOT={shlex.quote(str(corpus_root))}\n"
        f'exec {shlex.quote(str(current_release / "bin" / "Neocortex"))} "$@"\n'
    ).encode("utf-8")


def _desktop_exec_argument(path: Path) -> str:
    value = str(path)
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise LinuxReleaseError("desktop executable path contains an invalid character")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _desktop_payload(layout: LinuxReleaseLayout) -> bytes:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=NeoCortex\n"
        "Comment=Inventario, búsqueda y procesamiento portátil\n"
        f"Exec={_desktop_exec_argument(layout.launcher)} --ui\n"
        f"Icon={layout.icon}\n"
        "Terminal=false\n"
        "Categories=Utility;FileTools;\n"
        "StartupNotify=true\n"
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _PathSnapshot:
    kind: str
    payload: bytes | str | None
    mode: int | None


def _snapshot_path(path: Path) -> _PathSnapshot:
    if not os.path.lexists(path):
        return _PathSnapshot("missing", None, None)
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return _PathSnapshot("symlink", os.readlink(path), None)
    if stat.S_ISREG(metadata.st_mode):
        return _PathSnapshot("file", path.read_bytes(), stat.S_IMODE(metadata.st_mode))
    raise LinuxReleaseError(f"public path is neither a file nor symlink: {path}")


def _restore_path(path: Path, snapshot: _PathSnapshot) -> None:
    path.unlink(missing_ok=True)
    if snapshot.kind == "missing":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.kind == "symlink":
        assert isinstance(snapshot.payload, str)
        os.symlink(snapshot.payload, path)
        return
    assert isinstance(snapshot.payload, bytes)
    _atomic_write(path, snapshot.payload, mode=snapshot.mode or 0o644)


def _publish_public_access(
    layout: LinuxReleaseLayout,
    corpus_root: Path,
    *,
    desktop: bool,
    runner: CommandRunner = _run,
) -> tuple[dict[Path, _PathSnapshot], dict[str, str]]:
    paths = [layout.launcher, layout.alias]
    if desktop:
        paths.extend((layout.icon, layout.desktop))
    snapshots = {path: _snapshot_path(path) for path in paths}
    try:
        _atomic_write(
            layout.launcher,
            _launcher_payload(corpus_root, layout.current),
            mode=0o755,
        )
        layout.alias.parent.mkdir(parents=True, exist_ok=True)
        alias_stage = layout.alias.parent / f".{layout.alias.name}.{uuid.uuid4().hex}"
        try:
            os.symlink(layout.launcher, alias_stage)
            os.replace(alias_stage, layout.alias)
        finally:
            alias_stage.unlink(missing_ok=True)
        runner((layout.alias, "--version"), timeout=60)
        artifacts = {"launcher_sha256": _sha256_file(layout.launcher)}
        if desktop:
            source_icon = layout.source_root / "_05_Interfaz" / "assets" / "neocortex-app-icon.png"
            _atomic_write(layout.icon, source_icon.read_bytes())
            _atomic_write(layout.desktop, _desktop_payload(layout))
            runner(("desktop-file-validate", layout.desktop), timeout=60)
            runner(("update-desktop-database", layout.desktop.parent), timeout=60)
            artifacts["icon_sha256"] = _sha256_file(layout.icon)
            artifacts["desktop_sha256"] = _sha256_file(layout.desktop)
        return snapshots, artifacts
    except BaseException:
        for path, snapshot in reversed(tuple(snapshots.items())):
            _restore_path(path, snapshot)
        raise


def _receipt_path(layout: LinuxReleaseLayout, release_name: str, operation: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return layout.receipts / f"{timestamp}-{operation}-{release_name}.json"


def _write_receipt(layout: LinuxReleaseLayout, payload: dict[str, object]) -> Path:
    path = _receipt_path(layout, str(payload["release_id"]), str(payload["operation"]))
    _atomic_write(path, _canonical_json(payload))
    return path


def _latest_receipt(layout: LinuxReleaseLayout) -> dict[str, object] | None:
    if not layout.receipts.is_dir():
        return None
    candidates = sorted(layout.receipts.glob("*.json"), reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == RECEIPT_SCHEMA_VERSION:
            payload["_path"] = str(path)
            return payload
    return None


def _release_manifest(
    *,
    release_name: str,
    source_sha: str,
    wheel: Path,
    wheel_sha: str,
    source_only_wheels: dict[str, str],
    node_sha: str,
    versions: dict[str, str],
) -> dict[str, object]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "linux_release_manifest",
        "release_id": release_name,
        "source_sha": source_sha,
        "python": platform.python_version(),
        "wheel_filename": wheel.name,
        "wheel_sha256": wheel_sha,
        "pip_bootstrap_wheel_filename": PIP_BOOTSTRAP_FILENAME,
        "pip_bootstrap_wheel_sha256": PIP_BOOTSTRAP_SHA256,
        "source_only_wheels": source_only_wheels,
        "node_archive_filename": f"node-v{NODE_VERSION}-linux-x64.tar.xz",
        "node_archive_sha256": node_sha,
        **versions,
    }


def _read_release_manifest(
    release_root: Path,
    *,
    release_name: str,
    source_sha: str,
) -> dict[str, object]:
    _require_immutable(release_root)
    path = release_root / RELEASE_MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LinuxReleaseError("existing release manifest is unavailable") from exc
    if not isinstance(payload, dict):
        raise LinuxReleaseError("existing release manifest is malformed")
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or payload.get("kind") != "linux_release_manifest"
        or payload.get("release_id") != release_name
        or payload.get("source_sha") != source_sha
        or not isinstance(payload.get("wheel_filename"), str)
        or not isinstance(payload.get("node_archive_filename"), str)
        or not isinstance(payload.get("wheel_sha256"), str)
        or not _SHA256.fullmatch(str(payload["wheel_sha256"]))
        or payload.get("pip_bootstrap_wheel_filename") != PIP_BOOTSTRAP_FILENAME
        or payload.get("pip_bootstrap_wheel_sha256") != PIP_BOOTSTRAP_SHA256
        or payload.get("pip") != PIP_BOOTSTRAP_VERSION
        or payload.get("semgrep") != SEMGREP_TOOL_VERSION
        or payload.get("pyright") != f"pyright {PYRIGHT_VERSION}"
        or payload.get("pyright_integrity") != PYRIGHT_PACKAGE_INTEGRITY
        or payload.get("pyright_lock_sha256") != PYRIGHT_LOCK_SHA256
        or not isinstance(payload.get("semgrep_runtime_sha256"), str)
        or not _SHA256.fullmatch(str(payload["semgrep_runtime_sha256"]))
        or not isinstance(payload.get("source_only_wheels"), dict)
        or not payload["source_only_wheels"]
        or not all(
            isinstance(filename, str)
            and filename.endswith(".whl")
            and isinstance(digest, str)
            and _SHA256.fullmatch(digest)
            for filename, digest in payload["source_only_wheels"].items()
        )
        or not isinstance(payload.get("node_archive_sha256"), str)
        or not _SHA256.fullmatch(str(payload["node_archive_sha256"]))
    ):
        raise LinuxReleaseError("existing release manifest failed validation")
    return payload


def _require_corpus_root(corpus_root: Path) -> None:
    """Require one existing non-symlink corpus directory without modifying it."""

    try:
        metadata = corpus_root.lstat()
    except OSError as exc:
        raise LinuxReleaseError(f"cannot inspect corpus root {corpus_root}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LinuxReleaseError(f"corpus root must be a real directory: {corpus_root}")


def _prepare_corpus_root(corpus_root: Path) -> bool:
    """Create the selected corpus root and reject non-directory endpoints."""

    existed = corpus_root.exists()
    if existed:
        _require_corpus_root(corpus_root)
        return False
    try:
        corpus_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LinuxReleaseError(f"cannot prepare corpus root {corpus_root}: {exc}") from exc
    _require_corpus_root(corpus_root)
    return True


def install_release(
    layout: LinuxReleaseLayout,
    *,
    corpus_root: Path,
    prepare_models: bool,
    desktop: bool,
    runner: CommandRunner = _run,
) -> dict[str, object]:
    _require_reference_platform()
    corpus_root = corpus_root.expanduser().resolve(strict=False)
    if not corpus_root.is_absolute():
        raise LinuxReleaseError("corpus root must be absolute")
    corpus_root_created = _prepare_corpus_root(corpus_root)
    source_sha = _source_sha(layout.source_root, runner)
    name = release_id(source_sha)
    final_release = layout.releases / name
    layout.staging.mkdir(parents=True, exist_ok=True)
    layout.releases.mkdir(parents=True, exist_ok=True)
    if final_release.exists():
        release_artifacts = _read_release_manifest(
            final_release,
            release_name=name,
            source_sha=source_sha,
        )
        candidate_versions = _verify_python_release(
            final_release,
            layout,
            corpus_root,
            runner=runner,
        )
        if any(release_artifacts.get(key) != value for key, value in candidate_versions.items()):
            raise LinuxReleaseError("existing release versions differ from its manifest")
    else:
        with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=layout.staging) as temporary:
            workspace = Path(temporary)
            pip_wheel = _prepare_pip_bootstrap(workspace)
            wheel, source_only_wheels = _build_wheel(
                layout,
                workspace,
                pip_wheel=pip_wheel,
                runner=runner,
            )
            wheel_sha = _sha256_file(wheel)
            try:
                _install_wheel(
                    final_release,
                    wheel,
                    layout.source_root / "constraints.txt",
                    pip_wheel=pip_wheel,
                    runner=runner,
                )
                _install_semgrep_runtime(final_release, pip_wheel, runner=runner)
                node_sha = _install_node_pyright(final_release, workspace, runner=runner)
                candidate_versions = _verify_python_release(
                    final_release,
                    layout,
                    corpus_root,
                    runner=runner,
                )
                release_artifacts = _release_manifest(
                    release_name=name,
                    source_sha=source_sha,
                    wheel=wheel,
                    wheel_sha=wheel_sha,
                    source_only_wheels={
                        dependency.name: _sha256_file(dependency)
                        for dependency in source_only_wheels
                    },
                    node_sha=node_sha,
                    versions=candidate_versions,
                )
                _atomic_write(
                    final_release / RELEASE_MANIFEST_NAME,
                    _canonical_json(release_artifacts),
                )
                _make_immutable(final_release)
                _require_immutable(final_release)
                _fsync_directory(layout.releases)
            except BaseException:
                _remove_incomplete_release(final_release)
                raise

    release_artifacts = {
        **release_artifacts,
        "release_manifest_sha256": _sha256_file(final_release / RELEASE_MANIFEST_NAME),
    }

    environment = _candidate_environment(layout, corpus_root)
    if prepare_models:
        runner(
            (_venv_command(final_release), "models", "prepare", "--json"),
            timeout=14_400,
            environment=environment,
        )
    if prepare_models or desktop:
        runner(
            (_venv_command(final_release), "models", "status", "--json"),
            timeout=300,
            environment=environment,
        )

    with _release_lock(layout):
        previous = _current_target(layout)
        if previous == final_release.resolve(strict=True):
            operation = "repromote"
        else:
            operation = "install"
        public_snapshots: dict[Path, _PathSnapshot] = {}
        _replace_current(layout, final_release)
        try:
            public_snapshots, public_hashes = _publish_public_access(
                layout,
                corpus_root,
                desktop=desktop,
                runner=runner,
            )
            receipt = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "kind": "linux_release_receipt",
                "operation": operation,
                "created_at": datetime.now(UTC).isoformat(),
                "release_id": name,
                "release_path": str(final_release),
                "source_sha": source_sha,
                "previous_release": None if previous is None else str(previous),
                "current_link": str(layout.current),
                "corpus_root": str(corpus_root),
                "corpus_root_created": corpus_root_created,
                "models_prepared": prepare_models,
                "desktop_published": desktop,
                "artifacts": {
                    **release_artifacts,
                    **public_hashes,
                },
                "result": "success",
            }
            receipt_path = _write_receipt(layout, receipt)
        except BaseException:
            _replace_current(layout, previous)
            for path, snapshot in reversed(tuple(public_snapshots.items())):
                _restore_path(path, snapshot)
            raise
    return {**receipt, "receipt_path": str(receipt_path)}


def _require_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise LinuxReleaseError(f"required executable is unavailable: {name}")
    return path


def verify_release(
    layout: LinuxReleaseLayout,
    *,
    runner: CommandRunner = _run,
) -> dict[str, object]:
    _require_reference_platform()
    current = _current_target(layout)
    if current is None:
        raise LinuxReleaseError("no active release")
    _require_immutable(current)
    receipt = _latest_receipt(layout)
    if receipt is None:
        raise LinuxReleaseError("no valid installation receipt")
    corpus_root = Path(str(receipt.get("corpus_root", layout.policy.corpus_root)))
    _require_corpus_root(corpus_root)
    versions = _verify_python_release(current, layout, corpus_root, runner=runner)
    environment = _candidate_environment(layout, corpus_root)
    capability = runner(
        (layout.launcher, "doctor", "capabilities", "--json"),
        timeout=300,
        environment=environment,
        check=False,
    )
    if capability.returncode != 0:
        raise LinuxReleaseError(
            "doctor capabilities did not confirm the complete runtime: "
            + (capability.stderr or capability.stdout).strip()[-2000:]
        )
    platform_report = runner(
        (layout.launcher, "doctor", "platform", "--json"),
        timeout=60,
        environment=environment,
    )
    model_report = runner(
        (layout.launcher, "models", "status", "--json"),
        timeout=300,
        environment=environment,
        check=False,
    )
    if bool(receipt.get("models_prepared")) and model_report.returncode != 0:
        raise LinuxReleaseError("prepared model status is incomplete")
    qpdf = runner((_require_executable("qpdf"), "--version"), timeout=60).stdout.splitlines()[0]
    ffprobe = runner(
        (_require_executable("ffprobe"), "-version"),
        timeout=60,
    ).stdout.splitlines()[0]
    tesseract = runner(
        (_require_executable("tesseract"), "--list-langs"),
        timeout=60,
    )
    languages = frozenset(line.strip() for line in tesseract.stdout.splitlines()[1:])
    if not {"spa", "eng"} <= languages:
        raise LinuxReleaseError("Tesseract must expose spa and eng language data")
    if not layout.alias.is_symlink() or layout.alias.resolve(strict=True) != layout.launcher:
        raise LinuxReleaseError("user alias does not resolve to the stable launcher")
    if bool(receipt.get("desktop_published")):
        runner(("desktop-file-validate", layout.desktop), timeout=60)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "linux_release_verification",
        "verified": True,
        "release_id": current.name,
        "release_path": str(current),
        "receipt_path": receipt.get("_path"),
        "node": versions["node"],
        "pip": versions["pip"],
        "pyright": versions["pyright"],
        "pyright_integrity": versions["pyright_integrity"],
        "pyright_lock_sha256": versions["pyright_lock_sha256"],
        "semgrep": versions["semgrep"],
        "semgrep_runtime_sha256": versions["semgrep_runtime_sha256"],
        "qpdf": qpdf,
        "ffprobe": ffprobe,
        "tesseract_languages": sorted(languages),
        "platform": json.loads(platform_report.stdout),
        "models": json.loads(model_report.stdout),
    }


def rollback_release(
    layout: LinuxReleaseLayout,
    *,
    target_release: str | None = None,
) -> dict[str, object]:
    if os.name != "posix" or sys.platform != "linux":
        raise LinuxReleaseError("Linux release rollback is available only on Linux")
    with _release_lock(layout):
        current = _current_target(layout)
        if current is None:
            raise LinuxReleaseError("no active release to roll back")
        if target_release is None:
            latest = _latest_receipt(layout)
            if latest is None or latest.get("previous_release") is None:
                raise LinuxReleaseError("no previous release is recorded")
            target = Path(str(latest["previous_release"]))
        else:
            if Path(target_release).name != target_release:
                raise LinuxReleaseError("rollback release must be one release identifier")
            target = layout.releases / target_release
        target = target.resolve(strict=True)
        try:
            target.relative_to(layout.releases.resolve(strict=True))
        except ValueError as exc:
            raise LinuxReleaseError("rollback target is outside the releases directory") from exc
        if target == current:
            raise LinuxReleaseError("rollback target is already active")
        if not _venv_command(target).is_file():
            raise LinuxReleaseError("rollback target is not a complete release")
        _replace_current(layout, target)
        try:
            receipt = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "kind": "linux_release_receipt",
                "operation": "rollback",
                "created_at": datetime.now(UTC).isoformat(),
                "release_id": target.name,
                "release_path": str(target),
                "source_sha": "rollback",
                "previous_release": str(current),
                "current_link": str(layout.current),
                "corpus_root": str(layout.policy.corpus_root),
                "models_prepared": False,
                "desktop_published": layout.desktop.is_file(),
                "artifacts": {},
                "result": "success",
            }
            receipt_path = _write_receipt(layout, receipt)
        except BaseException:
            _replace_current(layout, current)
            raise
    return {**receipt, "receipt_path": str(receipt_path)}


def _layout(source_root: Path) -> LinuxReleaseLayout:
    return LinuxReleaseLayout(source_root.resolve(strict=True), current_platform_policy())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_linux.py")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    install = subcommands.add_parser("install")
    install.add_argument("--corpus-root", type=Path, required=True)
    install.add_argument("--prepare-models", action="store_true")
    install.add_argument("--desktop", action="store_true")
    subcommands.add_parser("verify")
    rollback = subcommands.add_parser("rollback")
    rollback.add_argument("--release")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    layout = _layout(args.source_root)
    try:
        if args.command == "install":
            report = install_release(
                layout,
                corpus_root=args.corpus_root,
                prepare_models=args.prepare_models,
                desktop=args.desktop,
            )
        elif args.command == "verify":
            report = verify_release(layout)
        else:
            report = rollback_release(layout, target_release=args.release)
    except (LinuxReleaseError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR release-linux {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NODE_VERSION",
    "PYRIGHT_VERSION",
    "LinuxReleaseError",
    "LinuxReleaseLayout",
    "build_parser",
    "install_release",
    "main",
    "release_id",
    "rollback_release",
    "verify_release",
]
