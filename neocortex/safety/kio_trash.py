"""Prepared, fail-closed KDE/KIO trash primitive for Linux.

The adapter is intentionally not wired into Linux ``--apply``.  KIO accepts a
path rather than an already-open file descriptor, so a successful operation is
classified as reversible and path-bound.  Callers must persist
``recovery_required`` whenever this module returns that status.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from neocortex.deduplication import FileSnapshot, stat_matches_snapshot


KIO_CLIENT_NAMES = ("kioclient6", "kioclient5", "kioclient")
KIO_TRASH_URL = "trash:/"
DEFAULT_KIO_TIMEOUT_SECONDS = 120.0
MIN_KIO_TIMEOUT_SECONDS = 1.0
MAX_KIO_TIMEOUT_SECONDS = 300.0
MAX_DIAGNOSTIC_CHARS = 1_000

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
class KioTrashPreflight:
    """Read-only environment evidence collected before starting KIO."""

    client: Path
    config_home: Path


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
    if not math.isfinite(timeout) or not MIN_KIO_TIMEOUT_SECONDS <= timeout <= MAX_KIO_TIMEOUT_SECONDS:
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


def discover_kio_client(*, which: ClientResolver = shutil.which) -> Path:
    """Resolve the first safe executable path without starting a process."""

    for name in KIO_CLIENT_NAMES:
        discovered = which(name)
        if discovered is None:
            continue
        try:
            candidate = _absolute_path(discovered, label="client")
            metadata = os.stat(candidate, follow_symlinks=True)
        except (KioTrashUnavailable, OSError):
            continue
        if not stat.S_ISREG(metadata.st_mode) or not os.access(candidate, os.X_OK):
            continue
        return candidate
    raise KioTrashUnavailable(
        "kio_client_unavailable",
        "no absolute executable kioclient6, kioclient5 or kioclient path is available",
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
    config_home = _config_home(effective_environment, home_directory=home_directory)
    _validate_config_home(config_home, client=client)
    return KioTrashPreflight(client=client, config_home=config_home)


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
        effective_environment = dict(os.environ if environment is None else environment)
        preflight = preflight_kio_trash(
            environment=effective_environment,
            home_directory=home_directory,
            which=which,
        )
        # This is intentionally adjacent to subprocess creation.  KIO remains
        # path-bound, so the residual dentry race is reflected in the receipt's
        # guarantee rather than hidden behind an identity-bound claim.
        _validate_source(source_path, expected)
    except KioTrashUnavailable as exc:
        return _blocked(source_path, exc)

    command = [os.fspath(preflight.client), "move", os.fspath(source_path), KIO_TRASH_URL]
    effective_runner = subprocess.run if runner is None else runner
    try:
        completed = effective_runner(
            command,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=effective_environment,
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

    returncode = completed.returncode
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
        process_diagnostic = completed.stderr if completed.stderr is not None else completed.stdout
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
    except BaseException as exc:
        return _recovery_required(
            source_path,
            reason="kio_verification_failed",
            detail=f"{type(exc).__name__}: {exc}",
            client=preflight.client,
            command=command,
            returncode=returncode,
        )
    if not isinstance(verification, KioTrashVerification):
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
            detail=verification.detail or "source absence and trash evidence were not both confirmed",
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
]
