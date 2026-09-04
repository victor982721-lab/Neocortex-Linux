"""Versioned, per-user NeoCortex release installation for Kubuntu/Linux.

The tool builds a wheel, creates an immutable release-local virtual environment
with product dependencies, verifies it, and only then atomically updates
``current`` under a POSIX ``flock``. Development analyzers are not bundled in
the application release. After a successful install, only ``current`` and the
immediate previous release remain available for rollback.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import venv
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

from neocortex import __version__
from tools import pip_bootstrap
from tools.pip_bootstrap import (
    PIP_BOOTSTRAP_FILENAME,
    PIP_BOOTSTRAP_SHA256,
    PIP_BOOTSTRAP_VERSION,
)
from neocortex.platform.policy import PlatformPolicy, current_platform_policy
from neocortex.runtime.source_staging import (
    SourceStagingError,
    parse_git_tracked_paths,
    stage_tracked_source,
)
from tools.release_artifacts import (
    SOURCE_DATE_EPOCH,
    ArtifactValidationError,
    validate_release_artifact,
)

RECEIPT_SCHEMA_VERSION = 1
RELEASE_PLATFORM_TAG = "linux-x86_64"
RELEASE_MANIFEST_NAME = "neocortex-release.json"
RUNTIME_DEPENDENCY_LOCK_NAME = "constraints-linux-cp314.lock"
RUNTIME_PROFILE = "product-only-v1"
WHEELHOUSE_MANIFEST_NAME = "wheelhouse-manifest.json"
WHEELHOUSE_SCHEMA_VERSION = 1
WHEELHOUSE_ENVIRONMENT = "NEOCORTEX_WHEELHOUSE"
# Kept as a compatibility-visible constant for bootstrap diagnostics.  Release
# installation itself never dereferences it and has no network fallback.
PIP_BOOTSTRAP_URL = pip_bootstrap.PIP_BOOTSTRAP_URL
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(
    r"(?P<version>"
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r")"
    r"-(?P<source_sha>[0-9a-f]{12}(?:[0-9a-f]{28})?)-cp314-linux-x86_64\Z"
)
_LOCKED_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;]+)$")
_MAX_RUNTIME_DEPENDENCIES = 512
_STAGING_STALE_SECONDS = 24 * 60 * 60
_STAGING_MARKER = ".installing.json"
_GC_MARKER = ".gc.json"
_GC_PREFIX = ".gc-"
_IMPORT_MODULES = (
    "PIL",
    "PySide6",
    "ctranslate2",
    "fastembed",
    "faster_whisper",
    "fitz",
    "numpy",
    "pytesseract",
)


@dataclass(frozen=True, slots=True)
class _WheelhouseArtifact:
    """One hash-authenticated wheel available to an offline release build."""

    name: str
    version: str
    filename: str
    sha256: str
    path: Path


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


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _offline_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a subprocess environment that cannot inherit package indexes.

    Release builds and installations are deliberately offline.  Removing every
    ambient ``PIP_*`` variable is important because a user's shell may inject an
    index, trusted host, credentials, cache, or configuration file even when the
    command line contains ``--no-index``.  The explicit values below make the
    policy observable to pip while keeping the rest of the caller's environment
    (notably ``PATH`` and XDG locations) intact.
    """

    environment = dict(os.environ if base is None else base)
    for name in tuple(environment):
        if name.upper().startswith("PIP_") or name in {
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONUSERBASE",
        }:
            environment.pop(name, None)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def _absolute_real_directory(path: Path, *, label: str) -> Path:
    """Resolve one caller-provided directory without following a symlink root."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise LinuxReleaseError(f"{label} must be an absolute path")
    cursor = candidate
    while cursor != cursor.parent:
        if os.path.lexists(cursor):
            try:
                metadata = cursor.lstat()
            except OSError as exc:
                raise LinuxReleaseError(f"{label} is unavailable") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise LinuxReleaseError(f"{label} cannot contain symlink components")
            if not stat.S_ISDIR(metadata.st_mode):
                raise LinuxReleaseError(f"{label} must be a real directory")
        cursor = cursor.parent
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise LinuxReleaseError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LinuxReleaseError(f"{label} must be a real directory")
    return candidate.resolve(strict=True)


def _wheel_metadata(path: Path) -> tuple[str, str]:
    """Read the distribution identity from a bounded wheel metadata member."""

    try:
        with zipfile.ZipFile(path) as archive:
            metadata_members = [
                member
                for member in archive.infolist()
                # A wheel may vendor libraries containing nested dist-info
                # metadata (setuptools is one example).  Only the distribution
                # metadata at the archive root identifies the wheel itself.
                if member.filename.endswith(".dist-info/METADATA")
                and member.filename.count("/") == 1
            ]
            if len(metadata_members) != 1:
                raise LinuxReleaseError(f"wheel metadata is not unique: {path.name}")
            member = metadata_members[0]
            if member.file_size > 1024 * 1024:
                raise LinuxReleaseError(f"wheel metadata exceeds its bound: {path.name}")
            payload = archive.read(member)
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
        raise LinuxReleaseError(f"wheelhouse artifact is not a readable wheel: {path.name}") from exc
    try:
        metadata_text = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise LinuxReleaseError(f"wheel metadata is not UTF-8: {path.name}") from exc
    headers: dict[str, str] = {}
    for raw_line in metadata_text.splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        if key in {"Name", "Version"} and key not in headers:
            headers[key] = value.strip()
    name = headers.get("Name")
    version = headers.get("Version")
    if not name or not version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise LinuxReleaseError(f"wheel metadata identity is invalid: {path.name}")
    if not version or any(character.isspace() for character in version):
        raise LinuxReleaseError(f"wheel metadata version is invalid: {path.name}")
    return _normalized_distribution_name(name), version


def _validate_wheelhouse(
    path: Path,
    *,
    required: Mapping[str, str | None] | None = None,
) -> dict[str, _WheelhouseArtifact]:
    """Validate a flat, hash-manifested wheelhouse for offline pip use.

    The manifest is intentionally local and small.  Every wheel must be listed
    exactly once with its SHA-256 and distribution identity, and every wheel in
    the directory must be listed.  This prevents pip from selecting an ambient
    cache or an unreviewed extra artifact while still allowing a wheelhouse to
    carry the full transitive closure for more than one release operation.
    """

    root = _absolute_real_directory(path, label="offline wheelhouse")
    manifest_path = root / WHEELHOUSE_MANIFEST_NAME
    try:
        manifest_metadata = manifest_path.lstat()
        if stat.S_ISLNK(manifest_metadata.st_mode) or not stat.S_ISREG(manifest_metadata.st_mode):
            raise LinuxReleaseError("wheelhouse manifest must be a regular file")
        if manifest_metadata.st_size > 8 * 1024 * 1024:
            raise LinuxReleaseError("wheelhouse manifest exceeds its byte bound")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except LinuxReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LinuxReleaseError("wheelhouse manifest is unavailable or malformed") from exc
    if not isinstance(payload, dict):
        raise LinuxReleaseError("wheelhouse manifest must be an object")
    if (
        payload.get("schema_version") != WHEELHOUSE_SCHEMA_VERSION
        or payload.get("kind") != "neocortex_wheelhouse"
        or payload.get("python") != "cp314"
        or payload.get("platform") != "linux_x86_64"
        or not isinstance(payload.get("artifacts"), list)
    ):
        raise LinuxReleaseError("wheelhouse manifest identity is unsupported")

    artifacts: dict[str, _WheelhouseArtifact] = {}
    listed_filenames: set[str] = set()
    for entry in payload["artifacts"]:
        if not isinstance(entry, dict):
            raise LinuxReleaseError("wheelhouse manifest artifact is malformed")
        filename = entry.get("filename")
        declared_name = entry.get("name")
        declared_version = entry.get("version")
        digest = entry.get("sha256")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".whl")
            or not isinstance(declared_name, str)
            or not isinstance(declared_version, str)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise LinuxReleaseError("wheelhouse manifest artifact identity is invalid")
        if filename in listed_filenames:
            raise LinuxReleaseError(f"wheelhouse manifest duplicates {filename}")
        listed_filenames.add(filename)
        artifact_path = root / filename
        try:
            metadata = artifact_path.lstat()
        except OSError as exc:
            raise LinuxReleaseError(f"wheelhouse artifact is missing: {filename}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise LinuxReleaseError(f"wheelhouse artifact is not a regular file: {filename}")
        try:
            observed_digest = _sha256_file(artifact_path)
        except OSError as exc:
            raise LinuxReleaseError(f"wheelhouse artifact cannot be hashed: {filename}") from exc
        if observed_digest != digest:
            raise LinuxReleaseError(f"wheelhouse artifact hash mismatch: {filename}")
        observed_name, observed_version = _wheel_metadata(artifact_path)
        normalized_name = _normalized_distribution_name(declared_name)
        if (
            observed_name != normalized_name
            or observed_version != declared_version
            or normalized_name in artifacts
        ):
            raise LinuxReleaseError(f"wheelhouse artifact metadata mismatch: {filename}")
        artifacts[normalized_name] = _WheelhouseArtifact(
            name=normalized_name,
            version=declared_version,
            filename=filename,
            sha256=digest,
            path=artifact_path,
        )

    if not artifacts:
        raise LinuxReleaseError("wheelhouse manifest contains no artifacts")
    children = tuple(root.iterdir())
    unexpected_children = sorted(
        child.name
        for child in children
        if child.name != WHEELHOUSE_MANIFEST_NAME and not child.name.endswith(".whl")
    )
    if unexpected_children:
        raise LinuxReleaseError(
            "wheelhouse contains unallowlisted entries: " + ",".join(unexpected_children[:5])
        )
    actual_wheels = {child.name for child in children if child.name.endswith(".whl")}
    if actual_wheels != listed_filenames:
        missing = sorted(listed_filenames - actual_wheels)
        extra = sorted(actual_wheels - listed_filenames)
        detail = ", ".join(
            (
                f"missing={','.join(missing[:3])}" if missing else "",
                f"extra={','.join(extra[:3])}" if extra else "",
            )
        ).strip(", ")
        raise LinuxReleaseError(f"wheelhouse artifact inventory differs ({detail})")
    if required:
        for raw_name, expected_version in required.items():
            name = _normalized_distribution_name(raw_name)
            artifact = artifacts.get(name)
            if artifact is None:
                raise LinuxReleaseError(f"wheelhouse is missing required artifact: {name}")
            if expected_version is not None and artifact.version != expected_version:
                raise LinuxReleaseError(
                    f"wheelhouse artifact version differs for {name}: {artifact.version}"
                )
    return artifacts


def _hashed_requirements(
    path: Path,
    artifacts: Sequence[_WheelhouseArtifact],
    *,
    project: tuple[str, str, str] | None = None,
) -> Path:
    """Write a deterministic requirements file consumed with ``--require-hashes``."""

    rows = [
        f"{artifact.name}=={artifact.version} --hash=sha256:{artifact.sha256}"
        for artifact in sorted(artifacts, key=lambda item: item.name)
    ]
    if project is not None:
        name, version, digest = project
        if _SHA256.fullmatch(digest) is None:
            raise LinuxReleaseError("project wheel hash is malformed")
        rows.insert(0, f"{name}[full]=={version} --hash=sha256:{digest}")
    if not rows:
        raise LinuxReleaseError("hashed requirements are empty")
    try:
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    except OSError as exc:
        raise LinuxReleaseError("cannot write hashed wheelhouse requirements") from exc
    return path


def _resolve_wheelhouse(path: Path | None) -> Path:
    """Resolve an explicitly local wheelhouse, never falling back to a network index."""

    if path is None:
        configured = os.environ.get(WHEELHOUSE_ENVIRONMENT)
        if not configured:
            raise LinuxReleaseError(
                f"offline wheelhouse is required; pass --wheelhouse or set {WHEELHOUSE_ENVIRONMENT}"
            )
        path = Path(configured)
    return _absolute_real_directory(path, label="offline wheelhouse")


def _runtime_dependency_lock(path: Path) -> dict[str, str]:
    """Read one exact, bounded Linux CPython runtime lock."""

    try:
        metadata = path.lstat()
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LinuxReleaseError("runtime dependency lock is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise LinuxReleaseError("runtime dependency lock must be a regular file")
    if len(raw.encode("utf-8")) > 128 * 1024:
        raise LinuxReleaseError("runtime dependency lock exceeds its byte bound")
    entries: dict[str, str] = {}
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCKED_REQUIREMENT.fullmatch(line)
        if match is None:
            raise LinuxReleaseError(
                f"runtime dependency lock line {line_number} is not an exact pin"
            )
        name = _normalized_distribution_name(match.group(1))
        version = match.group(2)
        if name == "neocortex-framework":
            raise LinuxReleaseError("runtime dependency lock must exclude the project wheel")
        if name in entries:
            raise LinuxReleaseError(f"runtime dependency lock duplicates {name}")
        entries[name] = version
    if not entries or len(entries) > _MAX_RUNTIME_DEPENDENCIES:
        raise LinuxReleaseError("runtime dependency lock count is outside its bound")
    if entries.get("pip") != PIP_BOOTSTRAP_VERSION:
        raise LinuxReleaseError("runtime dependency lock disagrees with pinned pip")
    return entries


_RUNTIME_INVENTORY_SCRIPT = r"""
import importlib.metadata as metadata
import json
import re

rows = {}
for distribution in metadata.distributions():
    raw_name = distribution.metadata.get("Name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise SystemExit("installed distribution name is unavailable")
    name = re.sub(r"[-_.]+", "-", raw_name.strip()).casefold()
    if name == "neocortex-framework":
        continue
    version = distribution.version
    if not isinstance(version, str) or not version.strip():
        raise SystemExit("installed distribution version is unavailable")
    if name in rows and rows[name] != version.strip():
        raise SystemExit("installed distribution identity is duplicated")
    rows[name] = version.strip()
print(json.dumps(rows, sort_keys=True, separators=(",", ":")))
"""


def _verify_runtime_dependency_lock(
    python: Path,
    lock: Path,
    *,
    runner: CommandRunner,
    environment: dict[str, str],
) -> None:
    expected = _runtime_dependency_lock(lock)
    completed = runner(
        (python, "-I", "-c", _RUNTIME_INVENTORY_SCRIPT),
        timeout=120,
        environment=environment,
    )
    try:
        observed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LinuxReleaseError("release dependency inventory is malformed") from exc
    if not isinstance(observed, dict) or any(
        not isinstance(name, str) or not isinstance(version, str)
        for name, version in observed.items()
    ):
        raise LinuxReleaseError("release dependency inventory is malformed")
    if observed != expected:
        names = sorted(set(expected) | set(observed))
        drift = [name for name in names if expected.get(name) != observed.get(name)]
        raise LinuxReleaseError(
            "release dependency inventory differs from its lock: " + ",".join(drift[:20])
        )


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


def _ensure_directory(path: Path, *, mode: int = 0o700) -> None:
    """Create one directory tree without following a pre-existing symlink."""

    candidate = path.expanduser()
    missing: list[Path] = []
    cursor = candidate
    while not os.path.lexists(cursor):
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    if os.path.lexists(cursor):
        metadata = os.lstat(cursor)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise LinuxReleaseError(f"release path component is not a real directory: {cursor}")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=mode)
        except FileExistsError:
            metadata = os.lstat(directory)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise LinuxReleaseError(
                    f"release path component is not a real directory: {directory}"
                ) from None
        os.chmod(directory, mode)


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    _ensure_directory(path.parent)
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


def _tracked_source_paths(
    source_root: Path,
    runner: CommandRunner = _run,
) -> tuple[str, ...]:
    result = runner(
        ("git", "-C", source_root, "ls-files", "--cached", "-z"),
        timeout=60,
    )
    try:
        return parse_git_tracked_paths(result.stdout)
    except SourceStagingError as error:
        raise LinuxReleaseError(str(error)) from error


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


def parse_release_id(value: str) -> tuple[str, str] | None:
    """Parse a release directory name without accepting arbitrary path names.

    The installer currently emits a twelve-character source prefix, while
    accepting a full forty-character source SHA keeps the namespace compatible
    with older/manual attestations.  Both forms remain bound to the Linux
    CPython 3.14 platform suffix.
    """

    match = _RELEASE_ID.fullmatch(value)
    if match is None:
        return None
    return match.group("version"), match.group("source_sha")


def _require_release_id(value: str) -> tuple[str, str]:
    parsed = parse_release_id(value)
    if parsed is None:
        raise LinuxReleaseError(f"release identifier is invalid: {value}")
    return parsed


def _validate_tracked_source_links(
    source_root: Path,
    relative_paths: tuple[str, ...],
) -> None:
    """Reject tracked symlinks whose target escapes the staged source tree."""

    source = source_root.resolve(strict=True)
    for relative in relative_paths:
        cursor = source
        for part in Path(relative).parts:
            cursor = cursor / part
            try:
                metadata = cursor.lstat()
            except OSError as exc:
                raise LinuxReleaseError(
                    f"tracked source owner is unavailable: {relative}"
                ) from exc
            if not stat.S_ISLNK(metadata.st_mode):
                continue
            target = cursor.resolve(strict=True)
            try:
                target.relative_to(source)
            except ValueError as exc:
                raise LinuxReleaseError(
                    f"tracked source symlink escapes source root: {relative}"
                ) from exc


def _venv_python(root: Path) -> Path:
    return root / "bin" / "python"


def _venv_command(root: Path) -> Path:
    return root / "bin" / "Neocortex"


def _prepare_pip_bootstrap(workspace: Path, *, wheelhouse: Path) -> Path:
    """Copy the authenticated pip wheel from the local wheelhouse only.

    The release installer intentionally has no network fallback.  A missing or
    unverified wheelhouse is a configuration error, not a reason to invoke the
    ambient index or download the pinned bootstrap wheel.
    """

    artifacts = _validate_wheelhouse(
        wheelhouse,
        required={"pip": PIP_BOOTSTRAP_VERSION},
    )
    source = artifacts["pip"].path
    destination = workspace / PIP_BOOTSTRAP_FILENAME
    try:
        shutil.copyfile(source, destination)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
    except OSError as exc:
        raise LinuxReleaseError("cannot stage the local pip bootstrap wheel") from exc
    _require_pip_bootstrap(destination)
    return destination


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
    wheelhouse: Path | None = None,
    runner: CommandRunner = _run,
) -> Path:
    staged_source = workspace / "source"
    tracked_paths = _tracked_source_paths(layout.source_root, runner)
    _validate_tracked_source_links(layout.source_root, tracked_paths)
    try:
        stage_tracked_source(
            layout.source_root,
            staged_source,
            tracked_paths,
        )
    except SourceStagingError as error:
        raise LinuxReleaseError(str(error)) from error
    dependency_wheelhouse = pip_wheel.parent if wheelhouse is None else wheelhouse
    wheelhouse_artifacts: dict[str, _WheelhouseArtifact] | None = None
    if wheelhouse is not None:
        runtime_lock = _runtime_dependency_lock(layout.source_root / RUNTIME_DEPENDENCY_LOCK_NAME)
        required = {
            **runtime_lock,
            "build": "1.5.0",
            "setuptools": "83.0.0",
            "wheel": None,
        }
        wheelhouse_artifacts = _validate_wheelhouse(wheelhouse, required=required)
    build_environment = workspace / "build-environment"
    _create_pip_environment(build_environment, pip_wheel, runner=runner)
    python = _venv_python(build_environment)
    constraints = staged_source / "constraints.txt"
    build_process_environment = _offline_environment()
    build_process_environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
        }
    )
    requirements_path: Path | None = None
    if wheelhouse_artifacts is not None:
        requirements_path = workspace / "build-requirements.txt"
        requirements_path = _hashed_requirements(
            requirements_path,
            tuple(
                artifact
                for name, artifact in wheelhouse_artifacts.items()
                if name != "neocortex-framework"
            ),
        )
    install_command: tuple[str | os.PathLike[str], ...] = (
        python,
        "-I",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-index",
        "--require-hashes",
        "--only-binary=:all:",
        "--find-links",
        dependency_wheelhouse,
        "--constraint",
        constraints,
    )
    if (staged_runtime_lock := staged_source / RUNTIME_DEPENDENCY_LOCK_NAME).is_file():
        install_command += ("--constraint", staged_runtime_lock)
    if requirements_path is not None:
        install_command += ("--requirement", requirements_path)
    else:
        # Internal callers predating the wheelhouse contract retain the old
        # argument shape, but still cannot reach an index or ambient cache.
        install_command += ("build==1.5.0", "setuptools==83.0.0", "wheel")
    runner(install_command, timeout=900, environment=build_process_environment)
    wheelhouse = workspace / "wheelhouse"
    wheelhouse.mkdir()
    runner(
        (
            python,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            wheelhouse,
            staged_source,
        ),
        timeout=900,
        environment=build_process_environment,
    )
    wheels = tuple(wheelhouse.glob("neocortex_framework-*.whl"))
    if len(wheels) != 1:
        raise LinuxReleaseError("wheel build did not produce exactly one artifact")
    return wheels[0]


def _install_wheel(
    release_root: Path,
    wheel: Path,
    constraints: Path,
    runtime_lock: Path,
    *,
    pip_wheel: Path,
    wheelhouse: Path | None = None,
    runner: CommandRunner = _run,
) -> None:
    if os.path.lexists(release_root):
        raise LinuxReleaseError(f"release install destination already exists: {release_root}")
    _ensure_directory(release_root.parent)
    locked_dependencies = _runtime_dependency_lock(runtime_lock)
    dependency_wheelhouse = wheel.parent if wheelhouse is None else wheelhouse
    wheelhouse_artifacts: dict[str, _WheelhouseArtifact] | None = None
    if wheelhouse is not None:
        wheelhouse_artifacts = _validate_wheelhouse(
            wheelhouse,
            required=locked_dependencies,
        )
    _create_pip_environment(release_root, pip_wheel, runner=runner)
    requirements_path: Path | None = None
    if wheelhouse_artifacts is not None:
        requirements_path = wheel.parent / "release-requirements.txt"
        requirements_path = _hashed_requirements(
            requirements_path,
            tuple(wheelhouse_artifacts.values()),
            project=("neocortex-framework", __version__, _sha256_file(wheel)),
        )
    install_command: tuple[str | os.PathLike[str], ...] = (
        _venv_python(release_root),
        "-I",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-index",
        "--require-hashes",
        "--only-binary=:all:",
        "--find-links",
        wheel.parent,
        "--find-links",
        dependency_wheelhouse,
        "--constraint",
        constraints,
        "--constraint",
        runtime_lock,
    )
    if requirements_path is not None:
        install_command += ("--requirement", requirements_path)
    else:
        install_command += (f"{wheel}[full]",)
    try:
        runner(
            install_command,
            timeout=3600,
            environment=_offline_environment(),
        )
    finally:
        if requirements_path is not None:
            requirements_path.unlink(missing_ok=True)


def _candidate_environment(
    layout: LinuxReleaseLayout,
    corpus_root: Path,
) -> dict[str, str]:
    environment = _offline_environment()
    environment["NEOCORTEX_CORPUS_ROOT"] = str(corpus_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["XDG_CONFIG_HOME"] = str(layout.policy.config_directory.parent)
    environment["XDG_STATE_HOME"] = str(layout.policy.state_directory.parents[1])
    environment["XDG_DATA_HOME"] = str(layout.policy.data_directory.parent)
    return environment


def _verify_python_release(
    release_root: Path,
    layout: LinuxReleaseLayout,
    corpus_root: Path,
    *,
    runtime_lock: Path | None = None,
    runner: CommandRunner = _run,
) -> dict[str, str]:
    environment = _candidate_environment(layout, corpus_root)
    python = _venv_python(release_root)
    runner((python, "-m", "pip", "check"), timeout=300, environment=environment)
    pip_version = runner(
        (python, "-I", "-c", "import pip; print(pip.__version__)"),
        timeout=60,
        environment=environment,
    ).stdout.strip()
    if pip_version != PIP_BOOTSTRAP_VERSION:
        raise LinuxReleaseError(f"unexpected release pip version: {pip_version}")
    if runtime_lock is not None:
        _verify_runtime_dependency_lock(
            python,
            runtime_lock,
            runner=runner,
            environment=environment,
        )
    runner(
        (python, "-c", ";".join(f"import {module}" for module in _IMPORT_MODULES)),
        timeout=300,
        environment=environment,
    )
    version_report = runner(
        (_venv_command(release_root), "--version"), timeout=60, environment=environment
    )
    version_text = version_report.stdout.strip()
    if not version_text.startswith("Neocortex "):
        raise LinuxReleaseError("release version output is malformed")
    reported_version = version_text.removeprefix("Neocortex ").strip()
    release_version = parse_release_id(release_root.name)
    if release_version is not None and reported_version != release_version[0]:
        raise LinuxReleaseError("release version does not match its release identifier")
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
    return {"pip": pip_version}


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
    _validate_release_tree(root, expected_tree_sha256=None)
    for current_root, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            path = Path(current_root) / name
            if not path.is_symlink() and path.stat(follow_symlinks=False).st_mode & 0o222:
                raise LinuxReleaseError(f"release artifact remains writable: {path}")
    if root.stat(follow_symlinks=False).st_mode & 0o222:
        raise LinuxReleaseError(f"release root remains writable: {root}")


def _allowed_release_symlink(relative: str, target: str) -> bool:
    """Allow only the links emitted by a symlinked CPython virtualenv."""

    if relative == "lib64":
        return target == "lib"
    # ``𝜋thon`` was emitted by an earlier venv bootstrap and is retained as a
    # compatibility alias; unlike arbitrary links it is still confined to the
    # interpreter aliases and must point at the release-local python3 entry.
    if relative in {"bin/python", "bin/python3.14", "bin/𝜋thon"}:
        return target in {"python3", "python3.14", "/usr/bin/python3.14"}
    if relative == "bin/python3":
        return target in {"python3.14", "/usr/bin/python3", "/usr/bin/python3.14"}
    return False


def _release_tree_entries(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    """Return a deterministic, symlink-aware release tree inventory."""

    if root.is_symlink() or not root.is_dir():
        raise LinuxReleaseError(f"release root is not a real directory: {root}")
    entries: list[tuple[str, str, int, str]] = []
    for current_root, directories, files in os.walk(root, followlinks=False):
        # ``os.walk`` otherwise descends in filesystem enumeration order, so
        # the same release could receive different tree digests on filesystems
        # that enumerate entries differently.
        directories.sort()
        files.sort()
        current = Path(current_root)
        for name in (*directories, *files):
            path = current / name
            relative = path.relative_to(root).as_posix()
            metadata = os.lstat(path)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                if not _allowed_release_symlink(relative, target):
                    raise LinuxReleaseError(f"release contains an unsafe symlink: {relative}")
                entries.append((relative, "l", mode, target))
            elif stat.S_ISDIR(metadata.st_mode):
                entries.append((relative, "d", mode, ""))
            elif stat.S_ISREG(metadata.st_mode):
                entries.append((relative, "f", mode, _sha256_file(path)))
            else:
                raise LinuxReleaseError(f"release contains a non-regular entry: {relative}")
    return tuple(entries)


def _release_tree_digest(root: Path, *, exclude: frozenset[str] = frozenset()) -> str:
    entries = tuple(entry for entry in _release_tree_entries(root) if entry[0] not in exclude)
    digest = hashlib.sha256()
    for relative, kind, _mode, payload in entries:
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(kind.encode("ascii"))
        payload_bytes = payload.encode("utf-8")
        digest.update(len(payload_bytes).to_bytes(8, "big"))
        digest.update(payload_bytes)
    return digest.hexdigest()


def _validate_release_tree(root: Path, *, expected_tree_sha256: str | None) -> str:
    """Validate release entry containment and optionally its recorded digest."""

    digest = _release_tree_digest(root, exclude=frozenset({RELEASE_MANIFEST_NAME}))
    if expected_tree_sha256 is not None and digest != expected_tree_sha256:
        raise LinuxReleaseError("release tree digest failed validation")
    return digest


def _remove_bytecode(root: Path) -> None:
    """Remove interpreter-generated bytecode before release immutability."""

    for current_root, directories, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(current_root) / name
            if path.suffix in {".pyc", ".pyo"} and not path.is_symlink():
                path.unlink()
        for name in directories:
            path = Path(current_root) / name
            if path.name == "__pycache__" and not path.is_symlink():
                shutil.rmtree(path)


def _rewrite_virtualenv_paths(staging_root: Path, final_root: Path) -> None:
    """Rebind venv metadata/scripts and remove transient staging provenance."""

    old_prefix = os.fsencode(os.fspath(staging_root))
    new_prefix = os.fsencode(os.fspath(final_root))
    bin_directory = staging_root / "bin"
    if not bin_directory.is_dir():
        raise LinuxReleaseError("release virtualenv bin directory is missing")

    # pip writes PEP 610 direct_url.json files for the wheel and bootstrap
    # wheel.  They point at the throw-away wheelhouse and would leave a stale
    # staging path in an otherwise immutable release.  Remove only those
    # generated files and their corresponding RECORD rows.
    for path in sorted(staging_root.rglob("direct_url.json"), key=lambda item: str(item)):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise LinuxReleaseError(f"release metadata cannot be inspected: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode) or not path.parent.name.endswith(".dist-info"):
            continue
        record = path.parent / "RECORD"
        try:
            path.unlink()
            if record.is_file():
                rows = record.read_text(encoding="utf-8").splitlines(keepends=True)
                relative = path.relative_to(path.parent.parent).as_posix()
                filtered = [row for row in rows if row.split(",", 1)[0] != relative]
                if filtered != rows:
                    with record.open("w", encoding="utf-8", newline="") as stream:
                        stream.writelines(filtered)
                        stream.flush()
                        os.fsync(stream.fileno())
        except (OSError, UnicodeError) as exc:
            raise LinuxReleaseError(f"release metadata cannot be normalized: {path}") from exc

    for path in sorted(staging_root.rglob("*"), key=lambda item: str(item)):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise LinuxReleaseError(f"release script cannot be inspected: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            continue
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise LinuxReleaseError(f"release script cannot be read: {path}") from exc
        if old_prefix not in payload:
            continue
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            raise LinuxReleaseError(f"release text contains an invalid encoding: {path}") from None
        rebound = payload.replace(old_prefix, new_prefix)
        try:
            with path.open("wb") as stream:
                stream.write(rebound)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise LinuxReleaseError(f"release path cannot be rebound: {path}") from exc


def _rewrite_virtualenv_shebangs(staging_root: Path, final_root: Path) -> None:
    """Compatibility wrapper for callers that only need venv path rebinding."""

    _rewrite_virtualenv_paths(staging_root, final_root)


def _remove_incomplete_release(root: Path) -> None:
    if not os.path.lexists(root):
        return
    if root.is_symlink() or not root.is_dir():
        raise LinuxReleaseError(f"incomplete release path is unsafe: {root}")
    users = _release_in_use(root)
    if users:
        joined = ",".join(str(pid) for pid in users)
        raise LinuxReleaseError(f"release is in use by host processes: {root} ({joined})")
    root.chmod(0o755)
    for current_root, directories, _files in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(current_root) / name
            if not path.is_symlink():
                path.chmod(0o755)
    shutil.rmtree(root)


def _release_in_use(root: Path) -> tuple[int, ...]:
    """Return host PIDs whose executable, cwd, or mapped files use *root*."""

    selected = root.resolve(strict=False)
    users: set[int] = set()

    def link_is_in_release(link: Path) -> bool:
        try:
            target = os.path.realpath(link)
        except OSError:
            return False
        # Linux appends this marker when a process retains an open descriptor
        # after its directory entry has been unlinked.  The inode is still in
        # use and must prevent release collection.
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        candidate = Path(target)
        return candidate == selected or candidate.is_relative_to(selected)

    for process in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(process.name)
        except ValueError:
            continue
        for link_name in ("exe", "cwd"):
            try:
                if link_is_in_release(process / link_name):
                    users.add(pid)
                    break
            except OSError:
                continue
        if pid in users:
            continue
        try:
            descriptors = tuple((process / "fd").iterdir())
        except OSError:
            descriptors = ()
        for descriptor in descriptors:
            if link_is_in_release(descriptor):
                users.add(pid)
                break
        if pid in users:
            continue
        try:
            with (process / "maps").open(encoding="utf-8", errors="ignore") as stream:
                if any(str(selected) in line for line in stream):
                    users.add(pid)
        except OSError:
            continue
    return tuple(sorted(users))


def _release_inventory(layout: LinuxReleaseLayout) -> tuple[Path, ...]:
    """Enumerate only strict release slots, refusing unknown entries."""

    _ensure_directory(layout.releases)
    entries: list[Path] = []
    for path in sorted(layout.releases.iterdir(), key=lambda item: item.name):
        if path.name == ".staging":
            continue
        if parse_release_id(path.name) is None:
            raise LinuxReleaseError(f"release directory contains unknown entry: {path.name}")
        if path.is_symlink():
            raise LinuxReleaseError(f"release path is an unsafe symlink: {path}")
        if not path.is_dir():
            raise LinuxReleaseError(f"release path is not a directory: {path}")
        entries.append(path)
    return tuple(entries)


def _proc_starttime(pid: int) -> str | None:
    """Read Linux process start ticks for PID identity checks."""

    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    _prefix, separator, suffix = raw.rpartition(") ")
    if not separator:
        return None
    fields = suffix.split()
    # ``suffix`` starts at field 3, so field 22 is index 19.
    return fields[19] if len(fields) > 19 else None


def _write_staging_marker(path: Path, *, release_name: str) -> None:
    marker = {
        "schema_version": 1,
        "kind": "linux_release_installing",
        "release_id": release_name,
        "pid": os.getpid(),
        "starttime": _proc_starttime(os.getpid()),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write(path / _STAGING_MARKER, _canonical_json(marker), mode=0o600)


def _staging_marker_status(marker: Path) -> str:
    """Return ``missing``, ``active``, ``stale`` or ``invalid`` for a marker."""

    if not os.path.lexists(marker):
        return "missing"
    try:
        metadata = marker.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return "invalid"
        if metadata.st_size > 16 * 1024:
            return "invalid"
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(payload, dict):
        return "invalid"
    release_name = payload.get("release_id")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "linux_release_installing"
        or not isinstance(release_name, str)
        or parse_release_id(release_name) is None
    ):
        return "invalid"
    pid = payload.get("pid")
    starttime = payload.get("starttime")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "invalid"
    if not isinstance(starttime, str) or not starttime:
        return "invalid"
    return "active" if _proc_starttime(pid) == starttime else "stale"


def _staging_marker_active(marker: Path) -> bool:
    return _staging_marker_status(marker) == "active"


def _write_gc_marker(
    transaction: Path,
    *,
    current: Path,
    rollback: Path | None,
    candidates: tuple[str, ...],
) -> None:
    payload = {
        "schema_version": 1,
        "kind": "linux_release_gc",
        "current": current.name,
        "rollback": None if rollback is None else rollback.name,
        "candidates": list(candidates),
        "pid": os.getpid(),
        "starttime": _proc_starttime(os.getpid()),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write(transaction / _GC_MARKER, _canonical_json(payload), mode=0o600)


def _read_gc_marker(transaction: Path) -> dict[str, object]:
    marker = transaction / _GC_MARKER
    try:
        metadata = marker.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise LinuxReleaseError("release GC marker is unsafe")
        if metadata.st_size > 16 * 1024:
            raise LinuxReleaseError("release GC marker exceeds its byte bound")
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except LinuxReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LinuxReleaseError("release GC marker is unavailable or malformed") from exc
    if not isinstance(payload, dict):
        raise LinuxReleaseError("release GC marker is malformed")
    if payload.get("schema_version") != 1 or payload.get("kind") != "linux_release_gc":
        raise LinuxReleaseError("release GC marker is unsupported")
    current = payload.get("current")
    rollback = payload.get("rollback")
    candidates = payload.get("candidates")
    if not isinstance(current, str) or parse_release_id(current) is None:
        raise LinuxReleaseError("release GC marker current is invalid")
    if rollback is not None and (not isinstance(rollback, str) or parse_release_id(rollback) is None):
        raise LinuxReleaseError("release GC marker rollback is invalid")
    if rollback == current:
        raise LinuxReleaseError("release GC marker keeps current as its own rollback")
    if (
        not isinstance(candidates, list)
        or any(not isinstance(item, str) or parse_release_id(item) is None for item in candidates)
        or len(set(candidates)) != len(candidates)
    ):
        raise LinuxReleaseError("release GC marker candidates are invalid")
    pid = payload.get("pid")
    starttime = payload.get("starttime")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise LinuxReleaseError("release GC marker PID is invalid")
    if not isinstance(starttime, str) or not starttime:
        raise LinuxReleaseError("release GC marker starttime is invalid")
    return payload


def _gc_marker_active(payload: dict[str, object]) -> bool:
    pid = payload["pid"]
    starttime = payload["starttime"]
    assert isinstance(pid, int)
    assert isinstance(starttime, str)
    return _proc_starttime(pid) == starttime


def _reap_staging(layout: LinuxReleaseLayout) -> tuple[str, ...]:
    """Remove stale bounded staging workspaces, preserving active workers."""

    if not os.path.lexists(layout.staging):
        _ensure_directory(layout.staging)
        return ()
    if layout.staging.is_symlink() or not layout.staging.is_dir():
        raise LinuxReleaseError(f"staging path is unsafe: {layout.staging}")
    removed: list[str] = []
    now = datetime.now(UTC).timestamp()
    for path in sorted(layout.staging.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_dir():
            raise LinuxReleaseError(f"staging entry is unsafe: {path}")
        if path.name.startswith(_GC_PREFIX):
            _recover_gc_transaction(layout, path)
            removed.append(path.name)
            continue
        marker = path / _STAGING_MARKER
        marker_status = _staging_marker_status(marker)
        if marker_status == "active":
            raise LinuxReleaseError(f"staging workspace is active: {path}")
        if marker_status == "invalid":
            raise LinuxReleaseError(f"staging marker is invalid: {path}")
        try:
            age = now - path.stat(follow_symlinks=False).st_mtime
        except OSError as exc:
            raise LinuxReleaseError(f"cannot inspect staging workspace: {path}") from exc
        if age < _STAGING_STALE_SECONDS:
            # A workspace without a marker may be a live legacy worker. Leave it
            # intact and make the caller fail closed rather than guessing.
            raise LinuxReleaseError(f"staging workspace is not stale: {path}")
        users = _release_in_use(path)
        if users:
            joined = ",".join(str(pid) for pid in users)
            raise LinuxReleaseError(f"staging workspace is in use: {path} ({joined})")
        _remove_incomplete_release(path)
        removed.append(path.name)
    return tuple(removed)


def _prune_old_releases(
    layout: LinuxReleaseLayout,
    *,
    current: Path,
    rollback: Path | None,
) -> tuple[str, ...]:
    """Remove releases older than the current/rollback pair after preflight."""

    transaction, names = _stage_old_releases(layout, current=current, rollback=rollback)
    try:
        _commit_old_releases(transaction)
    except BaseException:
        _restore_old_releases(transaction)
        raise
    return names


def _stage_old_releases(
    layout: LinuxReleaseLayout,
    *,
    current: Path,
    rollback: Path | None,
) -> tuple[Path, tuple[str, ...]]:
    """Move GC candidates to a same-filesystem tombstone before receipt commit."""

    current = current.resolve(strict=True)
    _require_release_id(current.name)
    releases_root = layout.releases.resolve(strict=True)
    if current.parent != releases_root:
        raise LinuxReleaseError("release retention current must be directly under releases")
    keep = {current}
    if rollback is not None:
        rollback = rollback.resolve(strict=True)
        _require_release_id(rollback.name)
        if rollback.parent != releases_root:
            raise LinuxReleaseError("release retention rollback must be directly under releases")
        keep.add(rollback)
    for item in keep:
        try:
            item.relative_to(releases_root)
        except ValueError as exc:
            raise LinuxReleaseError("release retention target is outside releases") from exc
    candidates = [path for path in _release_inventory(layout) if path.resolve() not in keep]
    candidates_tuple = tuple(candidates)
    for path in candidates_tuple:
        users = _release_in_use(path)
        if users:
            joined = ",".join(str(pid) for pid in users)
            raise LinuxReleaseError(f"release is in use by host processes: {path} ({joined})")
    transaction = layout.staging / f".gc-{uuid.uuid4().hex}"
    _ensure_directory(transaction)
    try:
        _write_gc_marker(
            transaction,
            current=current,
            rollback=rollback,
            candidates=tuple(path.name for path in candidates_tuple),
        )
    except BaseException:
        # No release has been moved yet, so a failed marker write can be
        # removed directly instead of leaving an unrecoverable tombstone.
        _remove_incomplete_release(transaction)
        raise
    try:
        for path in candidates_tuple:
            # An immutable release root is intentionally not writable.  Some
            # POSIX filesystems nevertheless require the directory being
            # renamed to have its write bit set, so temporarily make only the
            # tombstoned root writable; its contents remain immutable until
            # the bounded removal path handles them.
            path.chmod(0o755)
            os.replace(path, transaction / path.name)
    except BaseException:
        _restore_old_releases(transaction)
        raise
    return transaction, tuple(path.name for path in candidates_tuple)


def _restore_old_releases(transaction: Path) -> None:
    if not os.path.lexists(transaction):
        return
    if transaction.is_symlink() or not transaction.is_dir():
        raise LinuxReleaseError(f"release GC tombstone is unsafe: {transaction}")
    candidates = _read_gc_marker(transaction).get("candidates")
    assert isinstance(candidates, list)
    expected = set(candidates)
    releases = transaction.parent.parent
    for path in sorted(transaction.iterdir(), key=lambda item: item.name):
        if path.name == _GC_MARKER:
            continue
        if path.is_symlink() or not path.is_dir():
            raise LinuxReleaseError(f"release GC tombstone entry is unsafe: {path}")
        if parse_release_id(path.name) is None:
            raise LinuxReleaseError(f"release GC tombstone entry is unknown: {path.name}")
        if path.name not in expected:
            raise LinuxReleaseError(f"release GC tombstone entry is not recorded: {path.name}")
        destination = releases / path.name
        if os.path.lexists(destination):
            raise LinuxReleaseError(f"release GC restore destination exists: {destination}")
        os.replace(path, destination)
    (transaction / _GC_MARKER).unlink(missing_ok=True)
    transaction.rmdir()


def _commit_old_releases(transaction: Path) -> None:
    if not os.path.lexists(transaction):
        return
    if transaction.is_symlink() or not transaction.is_dir():
        raise LinuxReleaseError(f"release GC tombstone is unsafe: {transaction}")
    candidates_payload = _read_gc_marker(transaction).get("candidates")
    assert isinstance(candidates_payload, list)
    expected = set(candidates_payload)
    candidates: list[Path] = []
    for path in sorted(transaction.iterdir(), key=lambda item: item.name):
        if path.name == _GC_MARKER:
            continue
        if path.is_symlink() or not path.is_dir() or parse_release_id(path.name) is None:
            raise LinuxReleaseError(f"release GC tombstone entry is unsafe: {path}")
        if path.name not in expected:
            raise LinuxReleaseError(f"release GC tombstone entry is not recorded: {path.name}")
        candidates.append(path)
    for path in candidates:
        users = _release_in_use(path)
        if users:
            joined = ",".join(str(pid) for pid in users)
            raise LinuxReleaseError(f"release is in use by host processes: {path} ({joined})")
    for path in candidates:
        _remove_incomplete_release(path)
    (transaction / _GC_MARKER).unlink(missing_ok=True)
    transaction.rmdir()


def _gc_receipt_commits(
    layout: LinuxReleaseLayout,
    *,
    current: Path,
    payload: dict[str, object],
) -> bool:
    receipt = _latest_receipt(layout)
    if receipt is None or receipt.get("result") != "success":
        return False
    if receipt.get("release_id") != current.name:
        return False
    if receipt.get("retention_policy") != "current_and_immediate_rollback_v1":
        return False
    rollback = payload.get("rollback")
    if rollback is not None and not isinstance(rollback, str):
        return False
    expected_retained: list[str] = [current.name]
    if rollback is not None:
        expected_retained.append(rollback)
    retained = receipt.get("retained_releases")
    pruned = receipt.get("pruned_releases")
    candidates = payload.get("candidates")
    if not isinstance(retained, list) or not all(isinstance(item, str) for item in retained):
        return False
    retained_names = cast(list[str], retained)
    return (
        sorted(retained_names) == sorted(expected_retained)
        and isinstance(pruned, list)
        and pruned == candidates
    )


def _recover_gc_transaction(layout: LinuxReleaseLayout, transaction: Path) -> str:
    """Recover a GC tombstone after an interrupted publication."""

    payload = _read_gc_marker(transaction)
    if _gc_marker_active(payload):
        raise LinuxReleaseError(f"release GC workspace is active: {transaction}")
    current = _current_target(layout)
    if current is not None and current.name == payload["current"] and _gc_receipt_commits(
        layout,
        current=current,
        payload=payload,
    ):
        _commit_old_releases(transaction)
        return "committed"
    _restore_old_releases(transaction)
    return "restored"


def _validate_retention_receipt(
    layout: LinuxReleaseLayout,
    *,
    current: Path,
    receipt: dict[str, object],
) -> None:
    """Verify the current receipt's two-release retention contract."""

    policy = receipt.get("retention_policy")
    if policy is None:
        return
    if policy != "current_and_immediate_rollback_v1":
        raise LinuxReleaseError(f"unsupported release retention policy: {policy!r}")
    _require_release_id(current.name)
    try:
        current_resolved = current.resolve(strict=True)
        releases_root = layout.releases.resolve(strict=True)
        current_resolved.relative_to(releases_root)
        if current_resolved.parent != releases_root:
            raise LinuxReleaseError("retention current is not directly under releases")
    except ValueError as exc:
        raise LinuxReleaseError("retention current points outside releases") from exc
    previous = receipt.get("previous_release")
    expected = [current.name]
    if previous is not None:
        previous_path = Path(str(previous))
        if previous_path.is_symlink():
            raise LinuxReleaseError("retention rollback is an unsafe symlink")
        previous_path = previous_path.resolve(strict=True)
        try:
            previous_path.relative_to(releases_root)
            if previous_path.parent != releases_root:
                raise LinuxReleaseError("retention rollback is not directly under releases")
        except ValueError as exc:
            raise LinuxReleaseError("retention rollback points outside releases") from exc
        expected.append(previous_path.name)
    retained = receipt.get("retained_releases")
    if not isinstance(retained, list) or sorted(str(value) for value in retained) != sorted(
        expected
    ):
        raise LinuxReleaseError("retention receipt does not identify current and rollback")
    actual = sorted(path.name for path in _release_inventory(layout))
    if actual != sorted(expected):
        raise LinuxReleaseError(
            "release retention drift: "
            f"expected={sorted(expected)!r} actual={actual!r}"
        )


@contextmanager
def _release_lock(layout: LinuxReleaseLayout):
    try:
        fcntl = importlib.import_module("fcntl")
    except ImportError as exc:
        raise LinuxReleaseError("release activation requires POSIX fcntl") from exc
    _ensure_directory(layout.lock.parent)
    if os.path.lexists(layout.lock) and layout.lock.is_symlink():
        raise LinuxReleaseError(f"release lock is an unsafe symlink: {layout.lock}")
    with layout.lock.open("a+b") as stream:
        os.fchmod(stream.fileno(), 0o600)
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
    _ensure_directory(layout.current.parent)
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


def _launcher_payload(
    corpus_root: Path,
    current_release: Path,
    *,
    config_home: Path | None = None,
    state_home: Path | None = None,
    data_home: Path | None = None,
) -> bytes:
    exports = ""
    for name, value in (
        ("XDG_CONFIG_HOME", config_home),
        ("XDG_STATE_HOME", state_home),
        ("XDG_DATA_HOME", data_home),
    ):
        if value is not None:
            exports += f"export {name}={shlex.quote(str(value))}\n"
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        # The stable launcher must not inherit import paths or pip index
        # configuration from an interactive/development shell.
        "unset PYTHONPATH PYTHONHOME PYTHONUSERBASE PIP_CONFIG_FILE "
        "PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_NO_INDEX\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        f"export NEOCORTEX_CORPUS_ROOT={shlex.quote(str(corpus_root))}\n"
        f"{exports}"
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
    _ensure_directory(path.parent)
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
        current_release = _current_target(layout)
        if current_release is None:
            raise LinuxReleaseError("cannot publish public access without an active release")
        _atomic_write(
            layout.launcher,
            _launcher_payload(
                corpus_root,
                current_release,
                config_home=layout.policy.config_directory.parent,
                state_home=layout.policy.state_directory.parents[1],
                data_home=layout.policy.data_directory.parent,
            ),
            mode=0o755,
        )
        _ensure_directory(layout.alias.parent)
        alias_stage = layout.alias.parent / f".{layout.alias.name}.{uuid.uuid4().hex}"
        try:
            os.symlink(layout.launcher, alias_stage)
            os.replace(alias_stage, layout.alias)
        finally:
            alias_stage.unlink(missing_ok=True)
        runner((layout.alias, "--version"), timeout=60)
        artifacts = {"launcher_sha256": _sha256_file(layout.launcher)}
        if desktop:
            source_icon = (
                layout.source_root
                / "neocortex"
                / "interface"
                / "presentation"
                / "assets"
                / "neocortex-app-icon.png"
            )
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
    _atomic_write(path, _canonical_json(payload), mode=0o600)
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


def _receipt_release_target(
    layout: LinuxReleaseLayout,
    value: object,
    *,
    label: str,
) -> Path | None:
    """Resolve a receipt release path without accepting path ambiguity."""

    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise LinuxReleaseError(f"receipt {label} is invalid")
    candidate = Path(value)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise LinuxReleaseError(f"receipt {label} is unsafe")
    try:
        resolved = candidate.resolve(strict=True)
        releases_root = layout.releases.resolve(strict=True)
        if resolved.parent != releases_root:
            raise LinuxReleaseError(f"receipt {label} is outside releases")
        _require_release_id(resolved.name)
    except LinuxReleaseError:
        raise
    except (OSError, RuntimeError) as exc:
        raise LinuxReleaseError(f"receipt {label} is unavailable") from exc
    return resolved


def _repromotion_rollback(
    layout: LinuxReleaseLayout,
    *,
    current: Path,
    latest: dict[str, object] | None,
) -> Path | None:
    """Find the only safe rollback when recovering an already-current release."""

    candidate: Path | None = None
    if latest is not None:
        if latest.get("release_id") == current.name:
            candidate = _receipt_release_target(
                layout,
                latest.get("previous_release"),
                label="previous_release",
            )
        else:
            candidate = _receipt_release_target(
                layout,
                latest.get("release_path"),
                label="release_path",
            )
    if candidate is not None and candidate != current:
        return candidate
    inventory = [path for path in _release_inventory(layout) if path.resolve() != current]
    if len(inventory) == 1:
        return inventory[0].resolve(strict=True)
    if len(inventory) > 1:
        raise LinuxReleaseError(
            "already-current release has no unambiguous rollback after interrupted publication"
        )
    return None


def _release_manifest(
    *,
    release_name: str,
    source_sha: str,
    wheel: Path,
    wheel_sha: str,
    runtime_dependency_lock: Path,
    versions: dict[str, str],
) -> dict[str, object]:
    locked_dependencies = _runtime_dependency_lock(runtime_dependency_lock)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "linux_release_manifest",
        "runtime_profile": RUNTIME_PROFILE,
        "release_id": release_name,
        "source_sha": source_sha,
        "python": platform.python_version(),
        "wheel_filename": wheel.name,
        "wheel_sha256": wheel_sha,
        "pip_bootstrap_wheel_filename": PIP_BOOTSTRAP_FILENAME,
        "pip_bootstrap_wheel_sha256": PIP_BOOTSTRAP_SHA256,
        "runtime_dependency_lock_filename": runtime_dependency_lock.name,
        "runtime_dependency_lock_sha256": _sha256_file(runtime_dependency_lock),
        "runtime_dependency_count": len(locked_dependencies),
        **versions,
    }


def _read_release_manifest(
    release_root: Path,
    *,
    release_name: str,
    source_sha: str,
) -> dict[str, object]:
    _version, release_sha_prefix = _require_release_id(release_name)
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise LinuxReleaseError("release manifest source SHA is malformed")
    if not source_sha.startswith(release_sha_prefix):
        raise LinuxReleaseError("release manifest source SHA does not match its release ID")
    _require_immutable(release_root)
    path = release_root / RELEASE_MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LinuxReleaseError("existing release manifest is unavailable") from exc
    if not isinstance(payload, dict):
        raise LinuxReleaseError("existing release manifest is malformed")
    runtime_profile = payload.get("runtime_profile")
    if runtime_profile not in {None, RUNTIME_PROFILE}:
        raise LinuxReleaseError("existing release runtime profile is unsupported")
    if runtime_profile == RUNTIME_PROFILE and any(
        key in payload
        for key in (
            "node_archive_filename",
            "node_archive_sha256",
            "pyright",
            "pyright_integrity",
            "pyright_lock_sha256",
            "semgrep",
            "semgrep_runtime_sha256",
        )
    ):
        raise LinuxReleaseError("product-only release contains development-tool metadata")
    release_version, _ = _require_release_id(release_name)
    wheel_filename = payload.get("wheel_filename")
    expected_wheel = f"neocortex_framework-{release_version}-py3-none-any.whl"
    if wheel_filename != expected_wheel:
        raise LinuxReleaseError("existing release wheel identity is inconsistent")
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or payload.get("kind") != "linux_release_manifest"
        or payload.get("release_id") != release_name
        or payload.get("source_sha") != source_sha
        or not isinstance(wheel_filename, str)
        or not isinstance(payload.get("wheel_sha256"), str)
        or not _SHA256.fullmatch(str(payload["wheel_sha256"]))
        or payload.get("pip_bootstrap_wheel_filename") != PIP_BOOTSTRAP_FILENAME
        or payload.get("pip_bootstrap_wheel_sha256") != PIP_BOOTSTRAP_SHA256
        or payload.get("pip") != PIP_BOOTSTRAP_VERSION
    ):
        raise LinuxReleaseError("existing release manifest failed validation")
    lock_fields = {
        "runtime_dependency_lock_filename",
        "runtime_dependency_lock_sha256",
        "runtime_dependency_count",
    }
    present_lock_fields = lock_fields & set(payload)
    if present_lock_fields and present_lock_fields != lock_fields:
        raise LinuxReleaseError("existing release dependency lock identity is incomplete")
    if present_lock_fields:
        if (
            payload.get("runtime_dependency_lock_filename") != RUNTIME_DEPENDENCY_LOCK_NAME
            or not isinstance(payload.get("runtime_dependency_lock_sha256"), str)
            or not _SHA256.fullmatch(str(payload["runtime_dependency_lock_sha256"]))
            or isinstance(payload.get("runtime_dependency_count"), bool)
            or not isinstance(payload.get("runtime_dependency_count"), int)
        ):
            raise LinuxReleaseError("existing release dependency lock identity is invalid")
        lock = release_root / RUNTIME_DEPENDENCY_LOCK_NAME
        entries = _runtime_dependency_lock(lock)
        if (
            _sha256_file(lock) != payload["runtime_dependency_lock_sha256"]
            or len(entries) != payload["runtime_dependency_count"]
        ):
            raise LinuxReleaseError("existing release dependency lock failed validation")
    tree_digest = payload.get("release_tree_sha256")
    if tree_digest is not None:
        if not isinstance(tree_digest, str) or not _SHA256.fullmatch(tree_digest):
            raise LinuxReleaseError("existing release tree identity is invalid")
        _validate_release_tree(release_root, expected_tree_sha256=tree_digest)
    return payload


def _manifest_runtime_dependency_lock(
    release_root: Path,
    manifest: dict[str, object],
) -> Path | None:
    if "runtime_dependency_lock_filename" not in manifest:
        return None
    return release_root / RUNTIME_DEPENDENCY_LOCK_NAME


def _require_corpus_root(corpus_root: Path) -> None:
    """Require one existing non-symlink corpus directory without modifying it."""

    try:
        metadata = corpus_root.lstat()
    except OSError as exc:
        raise LinuxReleaseError(f"cannot inspect corpus root {corpus_root}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LinuxReleaseError(f"corpus root must be a real directory: {corpus_root}")
    cursor = corpus_root.parent
    while cursor != cursor.parent:
        if os.path.lexists(cursor):
            metadata = os.lstat(cursor)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise LinuxReleaseError(f"corpus parent is not a real directory: {cursor}")
        cursor = cursor.parent


def _prepare_corpus_root(corpus_root: Path) -> bool:
    """Create the selected corpus root and reject non-directory endpoints."""

    existed = os.path.lexists(corpus_root)
    if existed:
        _require_corpus_root(corpus_root)
        return False
    try:
        _ensure_directory(corpus_root)
    except OSError as exc:
        raise LinuxReleaseError(f"cannot prepare corpus root {corpus_root}: {exc}") from exc
    _require_corpus_root(corpus_root)
    return True


def _validate_layout(layout: LinuxReleaseLayout) -> None:
    """Validate all writable release roots before any operation begins."""

    paths = (
        layout.policy.data_directory,
        layout.policy.state_directory,
        layout.policy.config_directory,
        layout.policy.models_directory,
        layout.policy.runtimes_directory,
        layout.releases,
        layout.staging,
        layout.receipts,
        layout.current.parent,
        layout.launcher.parent,
        layout.alias.parent,
        layout.desktop.parent,
        layout.icon.parent,
    )
    for path in paths:
        cursor = path
        while cursor != cursor.parent:
            if os.path.lexists(cursor):
                metadata = os.lstat(cursor)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise LinuxReleaseError(f"release root component is unsafe: {cursor}")
            cursor = cursor.parent


def install_release(
    layout: LinuxReleaseLayout,
    *,
    corpus_root: Path,
    prepare_models: bool,
    desktop: bool,
    wheelhouse: Path | None = None,
    runner: CommandRunner = _run,
) -> dict[str, object]:
    _require_reference_platform()
    wheelhouse = _resolve_wheelhouse(wheelhouse)
    _validate_layout(layout)
    corpus_root = corpus_root.expanduser()
    if not corpus_root.is_absolute():
        raise LinuxReleaseError("corpus root must be absolute")
    with _release_lock(layout):
        _reap_staging(layout)
        corpus_root_created = _prepare_corpus_root(corpus_root)
        source_sha = _source_sha(layout.source_root, runner)
        source_runtime_lock = layout.source_root / RUNTIME_DEPENDENCY_LOCK_NAME
        _runtime_dependency_lock(source_runtime_lock)
        name = release_id(source_sha)
        final_release = layout.releases / name
        _ensure_directory(layout.releases)
        previous = _current_target(layout)
        latest_receipt = _latest_receipt(layout)
        release_exists = os.path.lexists(final_release)
        if release_exists:
            if final_release.is_symlink() or not final_release.is_dir():
                raise LinuxReleaseError(f"release slot is unsafe: {final_release}")
            manifest_path = final_release / RELEASE_MANIFEST_NAME
            if not manifest_path.is_file():
                # A process killed before publication may leave an old partial
                # slot.  It is safe to recover only this exact generated name.
                _remove_incomplete_release(final_release)
                release_exists = False
        if release_exists:
            try:
                release_artifacts = _read_release_manifest(
                    final_release,
                    release_name=name,
                    source_sha=source_sha,
                )
                runtime_lock = _manifest_runtime_dependency_lock(final_release, release_artifacts)
                candidate_versions = _verify_python_release(
                    final_release,
                    layout,
                    corpus_root,
                    runtime_lock=runtime_lock,
                    runner=runner,
                )
                if any(
                    release_artifacts.get(key) != value for key, value in candidate_versions.items()
                ):
                    raise LinuxReleaseError("existing release versions differ from its manifest")
            except (LinuxReleaseError, OSError, subprocess.SubprocessError):
                # A prior SIGKILL can leave this exact generated slot with a
                # manifest but an incomplete runtime.  Rebuild only when it is
                # not the active release, after the in-use fence has passed.
                if previous is not None and final_release.resolve(strict=False) == previous:
                    raise
                _remove_incomplete_release(final_release)
                release_exists = False
        else:
            with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=layout.staging) as temporary:
                workspace = Path(temporary)
                _write_staging_marker(workspace, release_name=name)
                pip_wheel = _prepare_pip_bootstrap(workspace, wheelhouse=wheelhouse)
                wheel = _build_wheel(
                    layout,
                    workspace,
                    pip_wheel=pip_wheel,
                    wheelhouse=wheelhouse,
                    runner=runner,
                )
                try:
                    validate_release_artifact(wheel, expected_version=__version__)
                except ArtifactValidationError as exc:
                    raise LinuxReleaseError(f"built wheel failed artifact validation: {exc}") from exc
                wheel_sha = _sha256_file(wheel)
                candidate_root = workspace / "release"
                _install_wheel(
                    candidate_root,
                    wheel,
                    layout.source_root / "constraints.txt",
                    source_runtime_lock,
                    pip_wheel=pip_wheel,
                    wheelhouse=wheelhouse,
                    runner=runner,
                )
                runtime_lock = candidate_root / RUNTIME_DEPENDENCY_LOCK_NAME
                shutil.copyfile(source_runtime_lock, runtime_lock)
                _remove_bytecode(candidate_root)
                candidate_versions = _verify_python_release(
                    candidate_root,
                    layout,
                    corpus_root,
                    runtime_lock=runtime_lock,
                    runner=runner,
                )
                _remove_bytecode(candidate_root)
                _rewrite_virtualenv_paths(candidate_root, final_release)
                tree_digest = _validate_release_tree(candidate_root, expected_tree_sha256=None)
                release_artifacts = _release_manifest(
                    release_name=name,
                    source_sha=source_sha,
                    wheel=wheel,
                    wheel_sha=wheel_sha,
                    runtime_dependency_lock=runtime_lock,
                    versions=candidate_versions,
                )
                release_artifacts["release_tree_sha256"] = tree_digest
                _atomic_write(
                    candidate_root / RELEASE_MANIFEST_NAME,
                    _canonical_json(release_artifacts),
                )
                _make_immutable(candidate_root)
                _require_immutable(candidate_root)
                # ``os.replace`` removes the candidate from its temporary
                # workspace, so restore the workspace directory's write bit
                # after hardening the release tree itself.
                candidate_root.parent.chmod(0o700)
                # POSIX rename also requires the source directory itself to
                # be writable.  Move the validated candidate with a private
                # mode, then re-apply immutability at its final pathname
                # before it can become current.
                candidate_root.chmod(0o755)
                os.replace(candidate_root, final_release)
                _make_immutable(final_release)
                _require_immutable(final_release)
                _fsync_directory(layout.releases)

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
        model_status: dict[str, object] | None = None
        if prepare_models or desktop:
            model_status = _decode_json_object(
                runner(
                    (_venv_command(final_release), "models", "status", "--json"),
                    timeout=300,
                    environment=environment,
                ),
                label="model status",
            )
            if prepare_models and model_status.get("all_prepared") is not True:
                raise LinuxReleaseError("model preparation did not produce a complete status")

        if previous == final_release.resolve(strict=True):
            operation = "repromote"
            rollback = _repromotion_rollback(
                layout,
                current=final_release,
                latest=latest_receipt,
            )
        else:
            operation = "install"
            rollback = previous
        retained_releases: tuple[str, ...] = (final_release.name,)
        if rollback is not None:
            retained_releases = (final_release.name, rollback.name)
        public_snapshots: dict[Path, _PathSnapshot] = {}
        _replace_current(layout, final_release)
        gc_transaction: Path | None = None
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
                "previous_release": None if rollback is None else str(rollback),
                "current_link": str(layout.current),
                "corpus_root": str(corpus_root),
                "corpus_root_created": corpus_root_created,
                "models_prepared": prepare_models,
                "desktop_published": desktop,
                "retention_policy": "current_and_immediate_rollback_v1",
                "retained_releases": retained_releases,
                "artifacts": {
                    **release_artifacts,
                    **public_hashes,
                },
                "result": "success",
            }
            gc_transaction, pruned_releases = _stage_old_releases(
                layout,
                current=final_release,
                rollback=rollback,
            )
            receipt["pruned_releases"] = pruned_releases
            try:
                receipt_path = _write_receipt(layout, receipt)
            except BaseException:
                if gc_transaction is not None:
                    _restore_old_releases(gc_transaction)
                raise
            if gc_transaction is not None:
                _commit_old_releases(gc_transaction)
        except BaseException:
            if gc_transaction is not None and os.path.lexists(gc_transaction):
                _restore_old_releases(gc_transaction)
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


def _decode_json_object(
    result: subprocess.CompletedProcess[str],
    *,
    label: str,
    allow_failed: bool = False,
) -> dict[str, object]:
    if result.returncode != 0:
        if allow_failed:
            return {"status": "unavailable", "exit_code": result.returncode}
        raise LinuxReleaseError(f"{label} command failed")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LinuxReleaseError(f"{label} output is malformed") from exc
    if not isinstance(payload, dict):
        raise LinuxReleaseError(f"{label} output is not an object")
    return payload


def _receipt_artifact_hash(receipt: dict[str, object], name: str) -> str | None:
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    value = artifacts.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise LinuxReleaseError(f"installation receipt artifact hash is invalid: {name}")
    return value


def _verify_release_unlocked(
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
    _validate_retention_receipt(layout, current=current, receipt=receipt)
    corpus_root = Path(str(receipt.get("corpus_root", layout.policy.corpus_root)))
    _require_corpus_root(corpus_root)
    source_sha = receipt.get("source_sha")
    if source_sha == "rollback":
        try:
            rollback_manifest = json.loads(
                (current / RELEASE_MANIFEST_NAME).read_text(encoding="utf-8")
            )
            source_sha = rollback_manifest.get("source_sha")
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            source_sha = None
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise LinuxReleaseError("installation receipt source SHA is invalid")
    manifest_path = current / RELEASE_MANIFEST_NAME
    expected_manifest_hash = _receipt_artifact_hash(receipt, "release_manifest_sha256")
    if (
        expected_manifest_hash is not None
        and _sha256_file(manifest_path) != expected_manifest_hash
    ):
        raise LinuxReleaseError("release manifest differs from its installation receipt")
    manifest = _read_release_manifest(
        current,
        release_name=current.name,
        source_sha=source_sha,
    )
    runtime_lock = _manifest_runtime_dependency_lock(current, manifest)
    versions = _verify_python_release(
        current,
        layout,
        corpus_root,
        runtime_lock=runtime_lock,
        runner=runner,
    )
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
    platform_payload = _decode_json_object(platform_report, label="platform status")
    models_payload = _decode_json_object(
        model_report,
        label="model status",
        allow_failed=not bool(receipt.get("models_prepared")),
    )
    qpdf_report = runner((_require_executable("qpdf"), "--version"), timeout=60)
    ffprobe_report = runner(
        (_require_executable("ffprobe"), "-version"),
        timeout=60,
    )
    try:
        qpdf = qpdf_report.stdout.splitlines()[0]
        ffprobe = ffprobe_report.stdout.splitlines()[0]
    except IndexError as exc:
        raise LinuxReleaseError("native tool version output is malformed") from exc
    tesseract = runner(
        (_require_executable("tesseract"), "--list-langs"),
        timeout=60,
    )
    languages = frozenset(line.strip() for line in tesseract.stdout.splitlines()[1:])
    if not {"spa", "eng"} <= languages:
        raise LinuxReleaseError("Tesseract must expose spa and eng language data")
    if not layout.alias.is_symlink() or layout.alias.resolve(strict=True) != layout.launcher:
        raise LinuxReleaseError("user alias does not resolve to the stable launcher")
    try:
        launcher_text = layout.launcher.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LinuxReleaseError("stable launcher is unavailable") from exc
    expected_exec = f'exec {shlex.quote(str(current / "bin" / "Neocortex"))} "$@"'
    if expected_exec not in launcher_text:
        raise LinuxReleaseError("stable launcher does not target the active release")
    expected_launcher_hash = _receipt_artifact_hash(receipt, "launcher_sha256")
    if expected_launcher_hash is not None and _sha256_file(layout.launcher) != expected_launcher_hash:
        raise LinuxReleaseError("stable launcher differs from its installation receipt")
    if bool(receipt.get("desktop_published")):
        runner(("desktop-file-validate", layout.desktop), timeout=60)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "linux_release_verification",
        "verified": True,
        "release_id": current.name,
        "release_path": str(current),
        "receipt_path": receipt.get("_path"),
        "runtime_profile": manifest.get("runtime_profile", "legacy-qa-bundle"),
        "pip": versions["pip"],
        "qpdf": qpdf,
        "ffprobe": ffprobe,
        "tesseract_languages": sorted(languages),
        "platform": platform_payload,
        "models": models_payload,
    }


def _require_clean_staging(layout: LinuxReleaseLayout) -> None:
    if not os.path.lexists(layout.staging):
        return
    if layout.staging.is_symlink() or not layout.staging.is_dir():
        raise LinuxReleaseError(f"staging path is unsafe: {layout.staging}")
    residual = tuple(layout.staging.iterdir())
    if residual:
        raise LinuxReleaseError("release staging contains residual workspaces")


def verify_release(
    layout: LinuxReleaseLayout,
    *,
    runner: CommandRunner = _run,
) -> dict[str, object]:
    """Verify one stable generation while holding the release activation lock."""

    _validate_layout(layout)
    with _release_lock(layout):
        _require_clean_staging(layout)
        return _verify_release_unlocked(layout, runner=runner)


def rollback_release(
    layout: LinuxReleaseLayout,
    *,
    target_release: str | None = None,
    runner: CommandRunner = _run,
) -> dict[str, object]:
    if os.name != "posix" or sys.platform != "linux":
        raise LinuxReleaseError("Linux release rollback is available only on Linux")
    _validate_layout(layout)
    with _release_lock(layout):
        _reap_staging(layout)
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
        _require_release_id(target.name)
        if target.parent.resolve(strict=True) != layout.releases.resolve(strict=True):
            raise LinuxReleaseError("rollback target must be directly under releases")
        if target.is_symlink():
            raise LinuxReleaseError("rollback target is an unsafe symlink")
        target = target.resolve(strict=True)
        try:
            target.relative_to(layout.releases.resolve(strict=True))
        except ValueError as exc:
            raise LinuxReleaseError("rollback target is outside the releases directory") from exc
        if target == current:
            raise LinuxReleaseError("rollback target is already active")
        if not _venv_command(target).is_file():
            raise LinuxReleaseError("rollback target is not a complete release")
        manifest_path = target / RELEASE_MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LinuxReleaseError("rollback target manifest is unavailable") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("source_sha"), str):
            raise LinuxReleaseError("rollback target manifest is malformed")
        _read_release_manifest(
            target,
            release_name=target.name,
            source_sha=str(manifest["source_sha"]),
        )
        target_tree_digest = manifest.get("release_tree_sha256")
        if target_tree_digest is not None and not isinstance(target_tree_digest, str):
            raise LinuxReleaseError("rollback target tree identity is invalid")
        _validate_release_tree(target, expected_tree_sha256=target_tree_digest)
        latest = _latest_receipt(layout)
        corpus_root = (
            Path(str(latest["corpus_root"]))
            if latest is not None and latest.get("corpus_root") is not None
            else layout.policy.corpus_root
        )
        if os.path.lexists(corpus_root):
            _require_corpus_root(corpus_root)
        desktop_published = bool(latest and latest.get("desktop_published"))
        public_snapshots: dict[Path, _PathSnapshot] = {}
        _replace_current(layout, target)
        try:
            public_snapshots, public_hashes = _publish_public_access(
                layout,
                corpus_root,
                desktop=desktop_published,
                runner=runner,
            )
            gc_transaction, pruned_releases = _stage_old_releases(
                layout,
                current=target,
                rollback=current,
            )
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
                "corpus_root": str(corpus_root),
                "models_prepared": False,
                "desktop_published": desktop_published,
                "retention_policy": "current_and_immediate_rollback_v1",
                "retained_releases": (target.name, current.name),
                "pruned_releases": pruned_releases,
                "artifacts": {
                    "release_manifest_sha256": _sha256_file(target / RELEASE_MANIFEST_NAME),
                    **public_hashes,
                },
                "result": "success",
            }
            try:
                receipt_path = _write_receipt(layout, receipt)
            except BaseException:
                _restore_old_releases(gc_transaction)
                raise
            _commit_old_releases(gc_transaction)
        except BaseException:
            _replace_current(layout, current)
            for path, snapshot in reversed(tuple(public_snapshots.items())):
                _restore_path(path, snapshot)
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
    install.add_argument(
        "--wheelhouse",
        type=Path,
        help=(
            "local hash-manifested wheelhouse; defaults to "
            f"${WHEELHOUSE_ENVIRONMENT} (network indexes are never used)"
        ),
    )
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
                wheelhouse=args.wheelhouse,
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
    "LinuxReleaseError",
    "LinuxReleaseLayout",
    "build_parser",
    "install_release",
    "main",
    "parse_release_id",
    "release_id",
    "rollback_release",
    "verify_release",
]
