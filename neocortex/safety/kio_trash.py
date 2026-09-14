"""Prepared, fail-closed KDE/KIO trash primitive for Linux.

The adapter is intentionally not wired into Linux ``--apply``.  KIO accepts a
path rather than an already-open file descriptor, so a successful operation is
classified as reversible and path-bound.  Callers must persist
``recovery_required`` whenever this module returns that status.
"""

from __future__ import annotations

import math
import ctypes
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import cast

from neocortex.deduplication import (
    FileChangedError,
    FileSnapshot,
    full_fingerprint,
    snapshot_path,
    stat_matches_snapshot,
)
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.workflow.actions.action_policy import validate_mutation_path


KIO_CLIENT_NAMES = ("kioclient6", "kioclient5", "kioclient")
KIO_TRASH_URL = "trash:/"
DBUS_RUN_SESSION_NAMES = ("dbus-run-session",)
DEFAULT_KIO_TIMEOUT_SECONDS = 120.0
MIN_KIO_TIMEOUT_SECONDS = 1.0
MAX_KIO_TIMEOUT_SECONDS = 300.0
MAX_DIAGNOSTIC_CHARS = 1_000
MAX_TRASH_INFO_BYTES = 8_192
MAX_TRASH_ENTRIES = 4_096
KIO_CLAIM_SCHEMA = "neocortex.kio-claim/v1"
KIO_RESTORE_SCHEMA = "neocortex.kio-restore/v1"

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class KioTrashStatus(StrEnum):
    """Terminal classification returned by the prepared primitive."""

    BLOCKED = "blocked"
    RECOVERY_REQUIRED = "recovery_required"
    APPLIED = "applied"


class KioTrashUnavailable(RuntimeError):
    """The local environment cannot safely start the KIO client."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True, slots=True)
class KioTrashClaim:
    """One same-filesystem, no-replace claim made before invoking KIO.

    KIO itself accepts a path, not an open descriptor.  Native operation mode
    therefore moves the already validated source to a private sibling path by
    ``renameat2(RENAME_NOREPLACE)`` first.  The claim is never a copy and is
    never removed with ``unlink``; an uncertain claim is retained for recovery.
    """

    source_path: Path
    claim_path: Path
    claim_directory: Path
    snapshot: FileSnapshot

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": KIO_CLAIM_SCHEMA,
            "source_path": os.fspath(self.source_path),
            "claim_path": os.fspath(self.claim_path),
            "claim_directory": os.fspath(self.claim_directory),
            "volume_id": f"{self.snapshot.volume_id:x}",
            "file_id": f"{self.snapshot.file_id:x}",
            "size": self.snapshot.size,
            "mtime_ns": self.snapshot.mtime_ns,
            "birthtime_ns": self.snapshot.birthtime_ns,
        }


@dataclass(frozen=True, slots=True)
class KioTrashPreflight:
    """Read-only environment evidence collected before starting KIO."""

    client: Path
    config_home: Path
    client_snapshot: FileSnapshot | None = None


@dataclass(frozen=True, slots=True)
class KioTrashVerification:
    """Post-effect evidence supplied by a caller-owned verifier."""

    source_absent: bool
    trash_evidence: str | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class KioTrashReceipt:
    """Evidence for one verified, reversible path-bound KIO move."""

    source_path: str
    client_path: str
    trash_evidence: str
    volume_id: int
    file_id: int
    size: int
    mtime_ns: int
    birthtime_ns: int
    verified_ns: int
    backend: str = "kio"
    guarantee: str = "reversible_path_bound"
    operation: str = "trash"
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class KioTrashResult:
    """One explicit outcome; only ``APPLIED`` carries a receipt."""

    status: KioTrashStatus
    reason: str
    source_path: str
    detail: str | None = None
    client_path: str | None = None
    command: tuple[str, ...] = ()
    returncode: int | None = None
    receipt: KioTrashReceipt | None = None


KioRunner = Callable[..., subprocess.CompletedProcess[str]]
KioVerifier = Callable[[Path, FileSnapshot, Path], object]
ClientResolver = Callable[[str], str | None]


def _curation_trash_paths(
    evidence: object,
    expected: FileSnapshot,
    source_digest: str,
) -> tuple[Path, Path, Path]:
    """Bind a v1 Trash receipt to the original object, without observing paths."""

    if not isinstance(evidence, dict):
        raise ValueError("trash receipt lacks destination evidence")
    paths: list[Path] = []
    for field in ("trash_root", "trash_path", "info_path"):
        value = evidence.get(field)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("trash receipt paths are invalid")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("trash receipt paths are not absolute descendants")
        paths.append(path)
    root, trash_path, info_path = paths
    if (
        trash_path.parent != root / "files"
        or info_path.parent != root / "info"
        or info_path.name != trash_path.name + ".trashinfo"
        or Path(expected.path) in {trash_path, info_path}
    ):
        raise ValueError("trash receipt paths are outside the declared Trash layout")
    if (
        evidence.get("volume_id") != f"{expected.volume_id:x}"
        or evidence.get("file_id") != f"{expected.file_id:x}"
        or type(evidence.get("size")) is not int
        or evidence.get("size") != expected.size
        or evidence.get("digest") != source_digest
        # Original v1 receipts carry birthtime in the grant snapshot rather
        # than the Trash object.  If duplicated here it must agree as well.
        or (
            "birthtime_ns" in evidence
            and (
                type(evidence["birthtime_ns"]) is not int
                or evidence["birthtime_ns"] != expected.birthtime_ns
            )
        )
    ):
        raise ValueError("trash receipt identity or digest differs from the source")
    return root, trash_path, info_path


def _validate_trash_info(path: Path, source_path: str) -> None:
    """Read a bounded, non-link .trashinfo and require its exact source."""

    # O_NONBLOCK prevents a concurrently substituted FIFO from blocking before
    # fstat can reject it; it has no effect on the supported regular files.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("trash info is not a regular unique file")
        raw = stream.read(MAX_TRASH_INFO_BYTES + 1)
    if len(raw) > MAX_TRASH_INFO_BYTES:
        raise ValueError("trash info exceeds its bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("trash info is not valid UTF-8") from exc
    lines = text.splitlines()
    if not any(line.strip() == "[Trash Info]" for line in lines):
        raise ValueError("trash info section is missing")
    paths = [
        line.split("=", 1)[1]
        for line in lines
        if line.strip().casefold().startswith("path=") and "=" in line
    ]
    if len(paths) != 1 or paths[0] != source_path:
        raise ValueError("trash info source path differs from the grant effect")


def _read_regular_bounded(path: Path, *, limit: int) -> bytes:
    """Read a regular unique file without following a final symlink."""

    fd = os.open(
        path,
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("file is not a regular unique file")
        raw = b""
        while len(raw) <= limit:
            chunk = os.read(fd, limit + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > limit:
            raise ValueError("file exceeds its bound")
        return raw
    finally:
        os.close(fd)


def _parse_ktrashrc(path: Path) -> tuple[bool, bool]:
    """Return whether a KDE trash config enables automatic pruning.

    ``ktrashrc`` is an INI-like file, but it is user-controlled input and KDE
    permits repeated sections/keys.  Parse only the two safety-sensitive keys;
    malformed bytes are rejected by the caller rather than interpreted.
    """

    try:
        raw = _read_regular_bounded(path, limit=MAX_TRASH_INFO_BYTES)
    except FileNotFoundError:
        return False, False
    except OSError as exc:
        raise KioTrashUnavailable(
            "kio_ktrashrc_unavailable",
            "KDE trash configuration cannot be inspected safely",
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KioTrashUnavailable(
            "kio_ktrashrc_invalid",
            "KDE trash configuration is not valid UTF-8",
        ) from exc
    use_time_limit = False
    limit_action = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().casefold()
        value = value.strip().strip('"\'').casefold()
        if key == "usetimeLimit".casefold() and value in {"1", "true", "yes", "on"}:
            use_time_limit = True
        elif key == "limitreachedaction" and value in {"1", "2"}:
            limit_action = True
    return use_time_limit, limit_action


def _reject_ktrashrc_auto_prune(config_home: Path) -> None:
    """Reject an existing KDE trash policy that can delete old trash items."""

    path = config_home / "ktrashrc"
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise KioTrashUnavailable(
            "kio_ktrashrc_unavailable",
            "KDE trash configuration cannot be inspected",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise KioTrashUnavailable(
            "kio_ktrashrc_unsafe",
            "KDE trash configuration must be a regular file",
        )
    if metadata.st_nlink != 1:
        raise KioTrashUnavailable(
            "kio_ktrashrc_hardlink",
            "KDE trash configuration must not have additional hard links",
        )
    use_time_limit, limit_action = _parse_ktrashrc(path)
    if use_time_limit or limit_action:
        raise KioTrashUnavailable(
            "kio_trash_auto_prune_configured",
            "ktrashrc enables automatic trash pruning; refusing the KIO effect",
        )


def _resolved_home(
    environment: Mapping[str, str],
    *,
    home_directory: Path | None,
) -> Path:
    """Resolve the effective HOME used by the child without shell expansion."""

    value: str | os.PathLike[str]
    if home_directory is not None:
        value = home_directory
    else:
        value = environment.get("HOME", os.fspath(Path.home()))
    return _absolute_path(value, label="home")


def _complete_environment(
    environment: Mapping[str, str],
    *,
    home_directory: Path | None,
    config_home: Path,
) -> dict[str, str]:
    """Build an explicit child environment for a native KIO operation."""

    home = _resolved_home(environment, home_directory=home_directory)
    completed = dict(environment)
    completed["HOME"] = os.fspath(home)
    completed.setdefault("XDG_CONFIG_HOME", os.fspath(config_home))
    completed.setdefault("XDG_DATA_HOME", os.fspath(home / ".local" / "share"))
    completed.setdefault("XDG_CACHE_HOME", os.fspath(home / ".cache"))
    completed.setdefault("XDG_RUNTIME_DIR", os.fspath(home / ".run"))
    completed.setdefault("KDEHOME", os.fspath(home / ".kde"))
    completed.setdefault("QT_QPA_PLATFORM", "offscreen")
    return completed


@contextmanager
def private_kio_context(
    environment: Mapping[str, str] | None = None,
    *,
    home_directory: Path | None = None,
) -> Iterator[tuple[dict[str, str], Path]]:
    """Yield an operation-private KDE config without changing user config.

    The user's data home is retained so a real KIO move reaches the normal
    Trash owner; only the configuration tree is private and seeded with a
    non-pruning policy.  The caller owns the lifetime of the context and must
    keep the yielded environment for the whole subprocess call.
    """

    supplied = os.environ if environment is None else environment
    merged = dict(os.environ) | dict(supplied)
    home = _resolved_home(merged, home_directory=home_directory)
    original_config = _config_home(merged, home_directory=home)
    _reject_ktrashrc_auto_prune(original_config)
    with tempfile.TemporaryDirectory(prefix="neocortex-kio-context-") as directory:
        base = Path(directory)
        config_home = base / "config"
        config_home.mkdir(mode=0o700)
        # Keep the generated policy explicit even when KDE would otherwise
        # synthesize defaults.  This also gives the native client no reason to
        # update the user's ktrashrc.
        ktrashrc = config_home / "ktrashrc"
        ktrashrc.write_text(
            "[Trash]\nUseTimeLimit=false\nLimitReachedAction=0\n",
            encoding="utf-8",
        )
        os.chmod(ktrashrc, 0o600)
        with ktrashrc.open("rb") as stream:
            os.fsync(stream.fileno())
        child_environment = _complete_environment(
            merged,
            home_directory=home,
            config_home=config_home,
        )
        child_environment["XDG_CONFIG_HOME"] = os.fspath(config_home)
        child_environment["KDEHOME"] = os.fspath(base / "kdehome")
        yield child_environment, home


def _verify_curation_trash_evidence(
    evidence: object,
    expected: FileSnapshot,
    source_digest: str,
) -> FileSnapshot:
    """Reobserve the exact moved object and its source-bound restoration data."""

    root, trash_path, info_path = _curation_trash_paths(evidence, expected, source_digest)
    if root.resolve(strict=True) != root:
        raise ValueError("trash root traverses a symbolic link")
    metadata = validate_mutation_path(root, trash_path, role="trash file")
    info_metadata = validate_mutation_path(root, info_path, role="trash info")
    if metadata is None or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("trash destination is not a regular unique file")
    if (
        info_metadata is None
        or not stat.S_ISREG(info_metadata.st_mode)
        or info_metadata.st_nlink != 1
    ):
        raise ValueError("trash info is not a regular unique file")
    relocated = replace(expected, path=str(trash_path))
    observed = snapshot_path(trash_path)
    if observed != relocated or not stat_matches_snapshot(relocated, metadata):
        raise ValueError("trash destination no longer identifies the original source")
    if "xxh3_128_full_v1:" + full_fingerprint(observed).hex() != source_digest:
        raise ValueError("trash destination digest changed")
    _validate_trash_info(info_path, expected.path)
    if os.path.lexists(expected.path):
        raise ValueError("trash source is present")
    return observed


def _sanitize_diagnostic(value: object) -> str | None:
    """Return a bounded, single-line diagnostic without terminal controls."""

    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = _ANSI_ESCAPE.sub("", text)
    text = "".join(character if character.isprintable() else " " for character in text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > MAX_DIAGNOSTIC_CHARS:
        text = "..." + text[-(MAX_DIAGNOSTIC_CHARS - 3) :]
    return text


def _validated_timeout(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("KIO timeout must be a real number")
    timeout = float(timeout_seconds)
    if (
        not math.isfinite(timeout)
        or not MIN_KIO_TIMEOUT_SECONDS <= timeout <= MAX_KIO_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "KIO timeout must be between "
            f"{MIN_KIO_TIMEOUT_SECONDS:g} and {MAX_KIO_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def _absolute_path(path: str | os.PathLike[str], *, label: str) -> Path:
    raw = os.fspath(path)
    if not raw or "\x00" in raw:
        raise KioTrashUnavailable(f"kio_{label}_invalid", f"{label} path is empty or contains NUL")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise KioTrashUnavailable(f"kio_{label}_not_absolute", f"{label} path must be absolute")
    return Path(os.path.normpath(candidate))


def _snapshot_kio_client(path: Path) -> FileSnapshot:
    """Capture one executable identity without following a final symlink."""

    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise KioTrashUnavailable(
            "kio_client_unavailable",
            "KIO client cannot be inspected",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise KioTrashUnavailable(
            "kio_client_symlink",
            "KIO client must be a regular executable rather than a symbolic link",
        )
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise KioTrashUnavailable(
            "kio_client_not_executable",
            "KIO client is not a regular executable",
        )
    return FileSnapshot(
        path=os.fspath(path),
        volume_id=metadata.st_dev,
        file_id=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        birthtime_ns=stat_birthtime_ns(metadata),
    )


def _validate_kio_client_identity(path: Path, expected: FileSnapshot) -> None:
    """Reject a client path that changed after preflight admission."""

    try:
        current = _snapshot_kio_client(path)
    except KioTrashUnavailable as exc:
        raise KioTrashUnavailable(
            "kio_client_changed",
            "KIO client is no longer the preflighted executable",
        ) from exc
    if current != expected:
        raise KioTrashUnavailable(
            "kio_client_changed",
            "KIO client identity changed after preflight",
        )


def _open_kio_client(path: Path, expected: FileSnapshot) -> int:
    """Open the preflighted client so the real subprocess cannot follow a swapped dentry."""

    flags = getattr(os, "O_PATH", os.O_RDONLY)
    flags |= os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KioTrashUnavailable(
            "kio_client_changed",
            "KIO client could not be opened as the preflighted executable",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise KioTrashUnavailable(
                "kio_client_changed",
                "KIO client is no longer a regular executable",
            )
        current = FileSnapshot(
            path=os.fspath(path),
            volume_id=metadata.st_dev,
            file_id=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            birthtime_ns=stat_birthtime_ns(metadata),
        )
        if current != expected or not os.access(path, os.X_OK):
            raise KioTrashUnavailable(
                "kio_client_changed",
                "KIO client identity or executable permission changed",
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _fsync_directory(path: Path) -> None:
    """Flush one real directory entry before classifying an effect as applied."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _renameat2_noreplace(
    source: Path,
    destination: Path,
    *,
    expected: FileSnapshot,
) -> None:
    """Move one regular file with descriptor-relative no-replace semantics."""

    if _absolute_path(expected.path, label="snapshot") != source:
        raise KioTrashUnavailable(
            "kio_claim_snapshot_path_mismatch",
            "claim snapshot does not identify the requested source path",
        )
    source_parent_fd: int | None = None
    destination_parent_fd: int | None = None
    source_fd: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        source_parent_fd = os.open(source.parent, flags)
        destination_parent_fd = os.open(destination.parent, flags)
        source_fd = os.open(
            source.name,
            getattr(os, "O_PATH", os.O_RDONLY)
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=source_parent_fd,
        )
        source_metadata = os.fstat(source_fd)
        if (
            stat.S_ISLNK(source_metadata.st_mode)
            or not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_nlink != 1
            or not stat_matches_snapshot(expected, source_metadata)
        ):
            raise KioTrashUnavailable(
                "kio_claim_source_changed",
                "claim source identity or metadata changed",
            )
        try:
            os.lstat(destination)
        except FileNotFoundError:
            pass
        else:
            raise KioTrashUnavailable(
                "kio_claim_collision",
                "private KIO claim destination already exists",
            )
        if source_metadata.st_dev != os.fstat(destination_parent_fd).st_dev:
            raise KioTrashUnavailable(
                "kio_claim_exdev",
                "private KIO claim requires one filesystem",
            )
        # Recheck the path-bound dentry immediately before the syscall.  The
        # retained descriptor proves the original inode, while this second
        # observation prevents a concurrent unlink/create from being moved by
        # ``renameat2`` under the old pathname.
        current_metadata = os.lstat(source)
        if (
            stat.S_ISLNK(current_metadata.st_mode)
            or not stat.S_ISREG(current_metadata.st_mode)
            or current_metadata.st_nlink != 1
            or not stat_matches_snapshot(expected, current_metadata)
            or (current_metadata.st_dev, current_metadata.st_ino)
            != (source_metadata.st_dev, source_metadata.st_ino)
        ):
            raise KioTrashUnavailable(
                "kio_claim_source_changed",
                "claim source changed immediately before rename",
            )
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise KioTrashUnavailable(
                "kio_claim_unavailable",
                "renameat2(RENAME_NOREPLACE) is unavailable",
            )
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                source_parent_fd,
                os.fsencode(source.name),
                destination_parent_fd,
                os.fsencode(destination.name),
                1,
            )
            != 0
        ):
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise KioTrashUnavailable(
                    "kio_claim_collision",
                    "private KIO claim destination already exists",
                )
            if error_number == errno.EXDEV:
                raise KioTrashUnavailable(
                    "kio_claim_exdev",
                    "private KIO claim requires one filesystem",
                )
            if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
                raise KioTrashUnavailable(
                    "kio_claim_unavailable",
                    "renameat2(RENAME_NOREPLACE) is unavailable",
                )
            raise KioTrashUnavailable(
                "kio_claim_failed",
                f"private KIO claim failed: {os.strerror(error_number)}",
            )
        try:
            destination_metadata = os.lstat(destination)
            if (
                stat.S_ISLNK(destination_metadata.st_mode)
                or not stat.S_ISREG(destination_metadata.st_mode)
                or destination_metadata.st_nlink != 1
                or not stat_matches_snapshot(expected, destination_metadata)
                or os.path.lexists(source)
            ):
                raise KioTrashUnavailable(
                    "kio_claim_unverified",
                    "private KIO claim destination does not identify the source",
                )
        except KioTrashUnavailable:
            raise
        except OSError as exc:
            raise KioTrashUnavailable(
                "kio_claim_unverified",
                "private KIO claim destination cannot be verified",
            ) from exc
        _fsync_directory(source.parent)
        if destination.parent != source.parent:
            _fsync_directory(destination.parent)
    except KioTrashUnavailable:
        raise
    except OSError as exc:
        reason = "kio_claim_exdev" if exc.errno == errno.EXDEV else "kio_claim_failed"
        raise KioTrashUnavailable(reason, f"private KIO claim failed: {exc}") from exc
    finally:
        for descriptor in (source_fd, source_parent_fd, destination_parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _claim_source(source: Path, expected: FileSnapshot) -> KioTrashClaim:
    """Create a private sibling claim and retain it on all uncertain paths."""

    claim_directory: Path | None = None
    try:
        claim_directory = Path(
            tempfile.mkdtemp(prefix=".neocortex-kio-claim-", dir=os.fspath(source.parent))
        )
        os.chmod(claim_directory, 0o700)
        claim_path = claim_directory / source.name
        _renameat2_noreplace(source, claim_path, expected=expected)
        return KioTrashClaim(source, claim_path, claim_directory, expected)
    except BaseException:
        if claim_directory is not None:
            try:
                # The directory is only ever empty before a successful claim.
                # Once a claim crossed the frontier it is retained by the
                # caller, so a failed setup cannot hide recovery evidence.
                if not any(claim_directory.iterdir()):
                    os.rmdir(claim_directory)
            except OSError:
                pass
        raise


def _restore_claim(claim: KioTrashClaim) -> None:
    """Restore a claim with the same no-replace primitive, idempotently."""

    if os.path.lexists(claim.source_path):
        current = snapshot_path(claim.source_path)
        if current != claim.snapshot:
            raise KioTrashUnavailable(
                "kio_claim_restore_collision",
                "original source path contains a different object",
            )
        if os.path.lexists(claim.claim_path):
            raise KioTrashUnavailable(
                "kio_claim_restore_collision",
                "both original and claimed paths are present",
            )
    elif os.path.lexists(claim.claim_path):
        _renameat2_noreplace(claim.claim_path, claim.source_path, expected=replace(claim.snapshot, path=str(claim.claim_path)))
    else:
        raise KioTrashUnavailable(
            "kio_claim_restore_missing",
            "neither the original nor private KIO claim is present",
        )
    try:
        os.rmdir(claim.claim_directory)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise KioTrashUnavailable(
            "kio_claim_cleanup_failed",
            "private KIO claim directory could not be removed",
        ) from exc


def discover_kio_client(*, which: ClientResolver = shutil.which) -> Path:
    """Resolve the first safe executable path without starting a process."""

    for name in KIO_CLIENT_NAMES:
        discovered = which(name)
        if discovered is None:
            continue
        try:
            candidate = _absolute_path(discovered, label="client")
            _snapshot_kio_client(candidate)
        except (KioTrashUnavailable, OSError):
            continue
        return candidate
    raise KioTrashUnavailable(
        "kio_client_unavailable",
        "no absolute executable kioclient6, kioclient5 or kioclient path is available",
    )


def discover_private_bus_launcher(*, which: ClientResolver = shutil.which) -> Path:
    """Resolve ``dbus-run-session`` for an operation-private bus."""

    for name in DBUS_RUN_SESSION_NAMES:
        discovered = which(name)
        if discovered is None:
            continue
        try:
            candidate = _absolute_path(discovered, label="bus_launcher")
            _snapshot_kio_client(candidate)
        except (KioTrashUnavailable, OSError):
            continue
        return candidate
    raise KioTrashUnavailable(
        "kio_private_bus_unavailable",
        "dbus-run-session is not available as a regular executable",
    )


def _config_home(
    environment: Mapping[str, str],
    *,
    home_directory: Path | None,
) -> Path:
    configured = environment.get("XDG_CONFIG_HOME", "")
    if configured:
        return _absolute_path(configured, label="config_home")
    home = Path.home() if home_directory is None else home_directory
    home = _absolute_path(home, label="home")
    return home / ".config"


def _validate_config_home(path: Path, *, client: Path) -> None:
    """Check writability without creating a directory or configuration file."""

    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        parent = path.parent
        try:
            parent_metadata = os.stat(parent, follow_symlinks=False)
        except OSError as exc:
            raise KioTrashUnavailable(
                "kio_config_parent_unavailable",
                "KIO config home is absent and its direct parent cannot be inspected",
            ) from exc
        if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
            raise KioTrashUnavailable(
                "kio_config_parent_unsafe",
                "KIO config home parent is not a real directory",
            ) from None
        if not os.access(parent, os.W_OK | os.X_OK):
            raise KioTrashUnavailable(
                "kio_config_parent_unwritable",
                "KIO config home is absent and its direct parent is not writable",
            ) from None
        return
    except OSError as exc:
        raise KioTrashUnavailable(
            "kio_config_home_unavailable",
            "KIO config home cannot be inspected",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise KioTrashUnavailable(
            "kio_config_home_unsafe",
            "KIO config home is not a real directory",
        )
    if not os.access(path, os.W_OK | os.X_OK):
        raise KioTrashUnavailable(
            "kio_config_home_unwritable",
            "KIO config home is not writable",
        )

    _reject_ktrashrc_auto_prune(path)

    config_name = {
        "kioclient6": "kioclient6rc",
        "kioclient5": "kioclient5rc",
        "kioclient": "kioclientrc",
    }.get(client.name)
    if config_name is None:
        return
    config_file = path / config_name
    try:
        config_metadata = os.stat(config_file, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise KioTrashUnavailable(
            "kio_config_file_unavailable",
            "KIO client configuration file cannot be inspected",
        ) from exc
    if stat.S_ISLNK(config_metadata.st_mode) or not stat.S_ISREG(config_metadata.st_mode):
        raise KioTrashUnavailable(
            "kio_config_file_unsafe",
            "KIO client configuration path is not a regular file",
        )
    if not os.access(config_file, os.W_OK):
        raise KioTrashUnavailable(
            "kio_config_file_unwritable",
            "KIO client configuration file is not writable",
        )


def preflight_kio_trash(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
    which: ClientResolver = shutil.which,
) -> KioTrashPreflight:
    """Perform the process/configuration preflight without writing or spawning."""

    effective_environment = os.environ if environment is None else environment
    client = discover_kio_client(which=which)
    client_snapshot = _snapshot_kio_client(client)
    config_home = _config_home(effective_environment, home_directory=home_directory)
    _validate_config_home(config_home, client=client)
    return KioTrashPreflight(
        client=client,
        client_snapshot=client_snapshot,
        config_home=config_home,
    )


def _validate_source(source: Path, expected: FileSnapshot) -> None:
    expected_path = _absolute_path(expected.path, label="snapshot")
    if expected_path != source:
        raise KioTrashUnavailable(
            "kio_snapshot_path_mismatch",
            "expected snapshot does not identify the requested source path",
        )
    try:
        metadata = os.stat(source, follow_symlinks=False)
    except OSError as exc:
        raise KioTrashUnavailable(
            "kio_source_unavailable",
            "KIO trash source cannot be inspected",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise KioTrashUnavailable("kio_source_symlink", "KIO trash source is a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise KioTrashUnavailable(
            "kio_source_not_regular",
            "KIO trash source is not a regular file",
        )
    if metadata.st_nlink != 1:
        raise KioTrashUnavailable(
            "kio_source_hardlink",
            "KIO trash source has additional hard links",
        )
    if not stat_matches_snapshot(expected, metadata):
        raise KioTrashUnavailable(
            "kio_source_changed",
            "KIO trash source changed after its snapshot was captured",
        )


def _candidate_trash_roots(
    source: Path,
    *,
    environment: Mapping[str, str],
    home_directory: Path | None,
) -> tuple[Path, ...]:
    """Return bounded KDE Trash roots that may own a moved source."""

    home = _resolved_home(environment, home_directory=home_directory)
    data_home = environment.get("XDG_DATA_HOME")
    if data_home:
        data_root = _absolute_path(data_home, label="data_home")
    else:
        data_root = home / ".local" / "share"
    roots: list[Path] = [data_root / "Trash"]
    try:
        source_device = os.stat(source.parent, follow_symlinks=False).st_dev
        current = source.parent
        # The mount point is the last directory whose device matches the
        # source.  Five hundred components is far beyond a practical POSIX
        # path but keeps a hostile path bounded.
        for _ in range(512):
            parent = current.parent
            try:
                parent_device = os.stat(parent, follow_symlinks=False).st_dev
            except OSError:
                break
            if parent_device != source_device or parent == current:
                break
            current = parent
        uid = os.getuid()
        roots.extend((current / f".Trash-{uid}", current / ".Trash"))
    except (AttributeError, OSError):
        pass
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.normcase(os.path.abspath(os.fspath(root)))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return tuple(unique)


def _trash_info_path_value(raw: bytes) -> str | None:
    """Return one exact Path= value from a bounded Trash metadata file."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    paths: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.casefold().startswith("path=") and "=" in stripped:
            paths.append(stripped.split("=", 1)[1])
    return paths[0] if len(paths) == 1 else None


def _default_kio_verifier(
    source: Path,
    expected: FileSnapshot,
    _client: Path,
    *,
    environment: Mapping[str, str],
    home_directory: Path | None,
    source_digest: str,
) -> KioTrashVerification:
    """Locate one KIO-created item from its source-bound ``.trashinfo``."""

    matches: list[tuple[Path, Path, Path]] = []
    for root in _candidate_trash_roots(
        source,
        environment=environment,
        home_directory=home_directory,
    ):
        try:
            root_stat = os.lstat(root)
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                continue
            info_root = root / "info"
            files_root = root / "files"
            info_stat = os.lstat(info_root)
            files_stat = os.lstat(files_root)
            if (
                stat.S_ISLNK(info_stat.st_mode)
                or not stat.S_ISDIR(info_stat.st_mode)
                or stat.S_ISLNK(files_stat.st_mode)
                or not stat.S_ISDIR(files_stat.st_mode)
            ):
                continue
            with os.scandir(info_root) as entries:
                for position, entry in enumerate(entries):
                    if position >= MAX_TRASH_ENTRIES:
                        break
                    if not entry.name.endswith(".trashinfo"):
                        continue
                    info_path = info_root / entry.name
                    try:
                        info_metadata = os.lstat(info_path)
                        if (
                            stat.S_ISLNK(info_metadata.st_mode)
                            or not stat.S_ISREG(info_metadata.st_mode)
                            or info_metadata.st_nlink != 1
                        ):
                            continue
                        raw = _read_regular_bounded(
                            info_path,
                            limit=MAX_TRASH_INFO_BYTES,
                        )
                    except (OSError, ValueError):
                        continue
                    if _trash_info_path_value(raw) != os.fspath(source):
                        continue
                    trash_name = entry.name[: -len(".trashinfo")]
                    trash_path = files_root / trash_name
                    try:
                        trash_metadata = os.lstat(trash_path)
                    except OSError:
                        continue
                    if (
                        stat.S_ISLNK(trash_metadata.st_mode)
                        or not stat.S_ISREG(trash_metadata.st_mode)
                        or trash_metadata.st_nlink != 1
                        or trash_metadata.st_dev != expected.volume_id
                        or not stat_matches_snapshot(expected, trash_metadata)
                    ):
                        continue
                    relocated = replace(expected, path=os.fspath(trash_path))
                    try:
                        if full_fingerprint(relocated).hex() != source_digest.split(":", 1)[-1]:
                            continue
                    except (FileChangedError, OSError, ValueError):
                        continue
                    matches.append((root, trash_path, info_path))
        except OSError:
            continue
    if len(matches) != 1:
        detail = (
            "no unique source-bound KIO Trash item was observed"
            if not matches
            else "multiple source-bound KIO Trash items were observed"
        )
        raise RuntimeError(detail)
    root, trash_path, info_path = matches[0]
    return KioTrashVerification(
        True,
        json.dumps(
            {
                "trash_root": os.fspath(root),
                "trash_path": os.fspath(trash_path),
                "info_path": os.fspath(info_path),
                "volume_id": f"{expected.volume_id:x}",
                "file_id": f"{expected.file_id:x}",
                "size": expected.size,
                "digest": source_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _rewrite_trash_info_path(path: Path, *, old_source: Path, new_source: Path) -> None:
    """Update only ``Path=`` in-place, preserving every other Trash field."""

    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("trash info is not a regular unique file")
        raw = os.read(descriptor, MAX_TRASH_INFO_BYTES + 1)
        if len(raw) > MAX_TRASH_INFO_BYTES:
            raise ValueError("trash info exceeds its bound")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("trash info is not valid UTF-8") from exc
        lines = text.splitlines(keepends=True)
        replaced = 0
        output: list[str] = []
        old_value = os.fspath(old_source)
        new_value = os.fspath(new_source)
        for line in lines:
            body = line.rstrip("\r\n")
            newline = line[len(body) :]
            stripped = body.strip()
            if stripped.casefold().startswith("path=") and "=" in stripped:
                prefix, value = stripped.split("=", 1)
                if prefix.casefold() != "path":
                    output.append(line)
                    continue
                if value != old_value:
                    raise ValueError("trash info source path differs from the private claim")
                # Keep the standard KDE spelling and the original newline;
                # all non-Path fields remain byte-for-byte intact.
                output.append(f"Path={new_value}{newline}")
                replaced += 1
            else:
                output.append(line)
        if replaced != 1:
            raise ValueError("trash info has no unique Path field")
        updated = "".join(output).encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        view = memoryview(updated)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.EIO, "trash info write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _claim_recovery_detail(claim: KioTrashClaim, *, reason: str, detail: object) -> str:
    """Serialize bounded, non-sensitive recovery evidence for the ledger."""

    return json.dumps(
        {
            "claim": claim.as_dict(),
            "detail": _sanitize_diagnostic(detail),
            "reason": reason,
            "schema": "neocortex.kio-claim-recovery/v1",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def restore_trash_receipt(receipt_json: str, *, root: Path) -> dict[str, object]:
    """Restore one verified KIO receipt with atomic no-replace semantics.

    The helper is receipt-bound and independent of grant tables, so exact
    dedupe actions can expose the same recovery path as grant-bound effects.
    It never overwrites an existing destination and removes ``.trashinfo``
    only after the restored bytes are verified.
    """

    if not isinstance(receipt_json, str) or not receipt_json.strip():
        raise ValueError("KIO restore receipt must be non-empty JSON")
    try:
        receipt = json.loads(receipt_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("KIO restore receipt is malformed") from exc
    if not isinstance(receipt, dict) or receipt.get("operation") != "trash":
        raise ValueError("KIO restore receipt is not a trash operation")
    source_value = receipt.get("source_path")
    digest_value = receipt.get("source_digest")
    evidence = receipt.get("trash")
    if not isinstance(source_value, str) or not isinstance(digest_value, str):
        raise ValueError("KIO restore receipt lacks source identity")
    if not isinstance(evidence, Mapping):
        raise ValueError("KIO restore receipt lacks Trash evidence")
    source = _absolute_path(source_value, label="restore source")
    root = _absolute_path(root, label="restore root")
    validate_mutation_path(root, source, role="restore source", allow_missing_leaf=True)
    try:
        trash_root = _absolute_path(evidence["trash_root"], label="trash root")
        trash_path = _absolute_path(evidence["trash_path"], label="trash file")
        info_path = _absolute_path(evidence["info_path"], label="trash info")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("KIO restore receipt has invalid Trash paths") from exc
    validate_mutation_path(
        trash_root,
        trash_path,
        role="trash file",
        allow_missing_leaf=True,
    )
    validate_mutation_path(
        trash_root,
        info_path,
        role="trash info",
        allow_missing_leaf=True,
    )
    if info_path.name != trash_path.name + ".trashinfo":
        raise ValueError("KIO restore metadata name does not match its file")
    expected_digest = digest_value.split(":", 1)[-1]
    if len(expected_digest) != 32 or any(c not in "0123456789abcdef" for c in expected_digest):
        raise ValueError("KIO restore digest is invalid")

    source_exists = os.path.lexists(source)
    trash_exists = os.path.lexists(trash_path)
    info_exists = os.path.lexists(info_path)
    if source_exists:
        metadata = os.lstat(source)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise KioTrashUnavailable("kio_restore_destination_unsafe", "restore source is not a unique regular file")
        current = snapshot_path(source)
        if full_fingerprint(current).hex() != expected_digest:
            raise KioTrashUnavailable("kio_restore_destination_collision", "restore destination contains different bytes")
        if trash_exists or info_exists:
            raise KioTrashUnavailable("kio_restore_collision", "restored source and Trash evidence both exist")
        return {
            "schema": KIO_RESTORE_SCHEMA,
            "status": "already_restored",
            "source_path": str(source),
            "trash_path": str(trash_path),
            "info_path": str(info_path),
            "digest": digest_value,
            "idempotent": True,
        }
    if not trash_exists or not info_exists:
        raise KioTrashUnavailable("kio_restore_evidence_missing", "Trash file or metadata is missing")

    trash_metadata = os.lstat(trash_path)
    info_metadata = os.lstat(info_path)
    if (
        stat.S_ISLNK(trash_metadata.st_mode)
        or not stat.S_ISREG(trash_metadata.st_mode)
        or trash_metadata.st_nlink != 1
        or stat.S_ISLNK(info_metadata.st_mode)
        or not stat.S_ISREG(info_metadata.st_mode)
        or info_metadata.st_nlink != 1
    ):
        raise KioTrashUnavailable("kio_restore_evidence_unsafe", "Trash evidence is not a unique regular pair")
    info_value = _trash_info_path_value(_read_regular_bounded(info_path, limit=MAX_TRASH_INFO_BYTES))
    if info_value != str(source):
        raise KioTrashUnavailable("kio_restore_origin_mismatch", "Trash metadata does not name the original source")
    trash_snapshot = snapshot_path(trash_path)
    if full_fingerprint(trash_snapshot).hex() != expected_digest:
        raise KioTrashUnavailable("kio_restore_content_changed", "Trash bytes differ from the receipt digest")
    if trash_snapshot.volume_id != os.stat(source.parent, follow_symlinks=False).st_dev:
        raise KioTrashUnavailable("kio_restore_exdev", "restore requires one filesystem")
    _renameat2_noreplace(trash_path, source, expected=trash_snapshot)
    _fsync_directory(trash_path.parent)
    _fsync_directory(source.parent)
    restored = snapshot_path(source)
    if full_fingerprint(restored).hex() != expected_digest:
        return {
            "schema": KIO_RESTORE_SCHEMA,
            "status": "recovery_required",
            "source_path": str(source),
            "trash_path": str(trash_path),
            "info_path": str(info_path),
            "digest": digest_value,
            "idempotent": False,
        }
    os.unlink(info_path)
    _fsync_directory(info_path.parent)
    return {
        "schema": KIO_RESTORE_SCHEMA,
        "status": "restored",
        "source_path": str(source),
        "trash_path": str(trash_path),
        "info_path": str(info_path),
        "digest": digest_value,
        "info_removed": True,
        "idempotent": False,
    }


def _blocked(source: Path, error: KioTrashUnavailable) -> KioTrashResult:
    return KioTrashResult(
        status=KioTrashStatus.BLOCKED,
        reason=error.reason,
        source_path=os.fspath(source),
        detail=_sanitize_diagnostic(error.detail),
    )


def _recovery_required(
    source: Path,
    *,
    reason: str,
    detail: object,
    client: Path,
    command: list[str],
    returncode: int | None,
) -> KioTrashResult:
    return KioTrashResult(
        status=KioTrashStatus.RECOVERY_REQUIRED,
        reason=reason,
        source_path=os.fspath(source),
        detail=_sanitize_diagnostic(detail),
        client_path=os.fspath(client),
        command=tuple(command),
        returncode=returncode,
    )


def move_to_trash(
    source: str | os.PathLike[str],
    expected: FileSnapshot,
    *,
    verifier: KioVerifier,
    runner: KioRunner | None = None,
    which: ClientResolver = shutil.which,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
    timeout_seconds: float = DEFAULT_KIO_TIMEOUT_SECONDS,
    private_bus: bool = False,
) -> KioTrashResult:
    """Attempt one KIO move and classify its observable effect.

    A zero process return is necessary but never sufficient for ``APPLIED``.
    Timeout and nonzero return are always ambiguous because KIO may already have
    crossed its filesystem frontier.
    """

    timeout = _validated_timeout(timeout_seconds)
    try:
        source_path = _absolute_path(source, label="source")
    except KioTrashUnavailable as exc:
        return _blocked(Path(os.fspath(source)), exc)
    try:
        _validate_source(source_path, expected)
        supplied_environment = os.environ if environment is None else environment
        # Preserve the historical injected-runner seam exactly: tests and
        # callers that provide a runner own the process environment.  A real
        # subprocess receives the complete inherited environment plus the
        # explicit per-operation HOME/XDG context.
        effective_environment = dict(supplied_environment)
        if runner is None:
            effective_environment = _complete_environment(
                dict(os.environ) | dict(supplied_environment),
                home_directory=home_directory,
                config_home=_config_home(supplied_environment, home_directory=home_directory),
            )
        preflight = preflight_kio_trash(
            environment=effective_environment,
            home_directory=home_directory,
            which=which,
        )
        # This is intentionally adjacent to subprocess creation.  KIO remains
        # path-bound, and the executable itself is revalidated at the same
        # boundary so a replaced dentry cannot silently select another client.
        _validate_source(source_path, expected)
        client_snapshot = preflight.client_snapshot
        if client_snapshot is None:
            client_snapshot = _snapshot_kio_client(preflight.client)
        _validate_kio_client_identity(preflight.client, client_snapshot)
    except KioTrashUnavailable as exc:
        return _blocked(source_path, exc)

    bus_launcher: Path | None = None
    if private_bus and runner is None:
        try:
            bus_launcher = discover_private_bus_launcher(which=which)
        except KioTrashUnavailable as exc:
            return _blocked(source_path, exc)
    effective_runner: KioRunner = cast(KioRunner, subprocess.run if runner is None else runner)
    client_descriptor: int | None = None
    try:
        # Keep the descriptor open through the real exec.  Injected runners keep
        # the historical test seam and still receive the human-readable path.
        client_descriptor = _open_kio_client(preflight.client, client_snapshot)
    except KioTrashUnavailable as exc:
        return _blocked(source_path, exc)
    if bus_launcher is not None:
        # ``dbus-run-session`` performs a second exec and rejects an O_PATH
        # ``/proc/self/fd`` executable with ELOOP.  The client was already
        # identity-checked immediately before this command is built; use its
        # absolute path only inside the private wrapper.  The source itself is
        # still protected by the same-filesystem claim in the native backend.
        command = [
            os.fspath(bus_launcher),
            "--",
            os.fspath(preflight.client),
            "move",
            os.fspath(source_path),
            KIO_TRASH_URL,
        ]
    else:
        command = [os.fspath(preflight.client), "move", os.fspath(source_path), KIO_TRASH_URL]
    runner_kwargs: dict[str, object] = {
        "check": False,
        "shell": False,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": effective_environment,
    }
    if runner is None and bus_launcher is None:
        runner_kwargs.update(
            {
                "executable": f"/proc/self/fd/{client_descriptor}",
                "pass_fds": (client_descriptor,),
            }
        )
    try:
        completed = effective_runner(
            command,
            **runner_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_diagnostic = exc.stderr if exc.stderr is not None else exc.stdout
        return _recovery_required(
            source_path,
            reason="kio_timeout_effect_ambiguous",
            detail=timeout_diagnostic or "KIO command timed out after it was started",
            client=preflight.client,
            command=command,
            returncode=None,
        )
    except OSError as exc:
        return KioTrashResult(
            status=KioTrashStatus.BLOCKED,
            reason="kio_process_start_failed",
            source_path=os.fspath(source_path),
            detail=_sanitize_diagnostic(f"{type(exc).__name__}: {exc}"),
            client_path=os.fspath(preflight.client),
            command=tuple(command),
        )
    except subprocess.SubprocessError as exc:
        return _recovery_required(
            source_path,
            reason="kio_process_effect_ambiguous",
            detail=f"{type(exc).__name__}: {exc}",
            client=preflight.client,
            command=command,
            returncode=None,
        )
    except BaseException as exc:
        return _recovery_required(
            source_path,
            reason="kio_process_interrupted",
            detail=f"{type(exc).__name__}: KIO process outcome is unknown",
            client=preflight.client,
            command=command,
            returncode=None,
        )
    finally:
        if client_descriptor is not None:
            try:
                os.close(client_descriptor)
            except OSError:
                pass

    try:
        returncode = completed.returncode
        process_stderr = completed.stderr
        process_stdout = completed.stdout
    except BaseException as exc:
        return _recovery_required(
            source_path,
            reason="kio_process_result_invalid",
            detail=f"{type(exc).__name__}: KIO runner returned an unsupported result",
            client=preflight.client,
            command=command,
            returncode=None,
        )
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return _recovery_required(
            source_path,
            reason="kio_process_result_invalid",
            detail="KIO runner returned no valid integer exit status",
            client=preflight.client,
            command=command,
            returncode=None,
        )
    if returncode != 0:
        process_diagnostic = process_stderr if process_stderr is not None else process_stdout
        return _recovery_required(
            source_path,
            reason="kio_nonzero_effect_ambiguous",
            detail=process_diagnostic or f"KIO command returned {returncode}",
            client=preflight.client,
            command=command,
            returncode=returncode,
        )

    try:
        verification = verifier(source_path, expected, preflight.client)
        if isinstance(verification, KioTrashVerification):
            verification = KioTrashVerification(
                verification.source_absent,
                verification.trash_evidence,
                verification.detail,
            )
    except BaseException as exc:
        return _recovery_required(
            source_path,
            reason="kio_verification_failed",
            detail=f"{type(exc).__name__}: {exc}",
            client=preflight.client,
            command=command,
            returncode=returncode,
        )
    if (
        not isinstance(verification, KioTrashVerification)
        or type(verification.source_absent) is not bool
        or (
            verification.trash_evidence is not None
            and not isinstance(verification.trash_evidence, str)
        )
        or (verification.detail is not None and not isinstance(verification.detail, str))
    ):
        return _recovery_required(
            source_path,
            reason="kio_verification_invalid",
            detail="KIO verifier returned an unsupported result",
            client=preflight.client,
            command=command,
            returncode=returncode,
        )
    evidence = _sanitize_diagnostic(verification.trash_evidence)
    source_absent = not os.path.lexists(source_path)
    if not verification.source_absent or not source_absent or evidence is None:
        return _recovery_required(
            source_path,
            reason="kio_effect_unverified",
            detail=verification.detail
            or "source absence and trash evidence were not both confirmed",
            client=preflight.client,
            command=command,
            returncode=returncode,
        )
    try:
        _fsync_directory(source_path.parent)
    except OSError as exc:
        return _recovery_required(
            source_path,
            reason="kio_directory_fsync_failed",
            detail=exc,
            client=preflight.client,
            command=command,
            returncode=returncode,
        )

    return KioTrashResult(
        status=KioTrashStatus.APPLIED,
        reason="kio_trash_verified",
        source_path=os.fspath(source_path),
        client_path=os.fspath(preflight.client),
        command=tuple(command),
        returncode=returncode,
        receipt=KioTrashReceipt(
            source_path=os.fspath(source_path),
            client_path=os.fspath(preflight.client),
            trash_evidence=evidence,
            volume_id=expected.volume_id,
            file_id=expected.file_id,
            size=expected.size,
            mtime_ns=expected.mtime_ns,
            birthtime_ns=expected.birthtime_ns,
            verified_ns=time.time_ns(),
        ),
    )


__all__ = [
    "DEFAULT_KIO_TIMEOUT_SECONDS",
    "KIO_CLIENT_NAMES",
    "KIO_RESTORE_SCHEMA",
    "KIO_TRASH_URL",
    "MAX_KIO_TIMEOUT_SECONDS",
    "MIN_KIO_TIMEOUT_SECONDS",
    "KioRunner",
    "KioTrashPreflight",
    "KioTrashReceipt",
    "KioTrashResult",
    "KioTrashStatus",
    "KioTrashUnavailable",
    "KioTrashVerification",
    "KioVerifier",
    "discover_kio_client",
    "move_to_trash",
    "preflight_kio_trash",
    "restore_trash_receipt",
]
