"""Owner-specific connection policy for existing framework state."""
# region [00] Contexto del módulo
# Módulo: neocortex/framework_connection.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import sqlite3
import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path

from neocortex.persistence.sqlite_immutable import open_sidecar_safe_sqlite_connection
from neocortex.persistence.sqlite_paths import existing_sqlite_uri
# endregion [01]

# region [02] Implementación


def _open_real_parent(path: Path) -> int:
    """Open the owner parent without following an ancestor symlink."""

    parent = path.parent
    if not parent.is_absolute():
        parent = parent.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        parts = parent.parts
        if not parts or parts[0] != os.sep:
            raise sqlite3.OperationalError(f"framework SQLite parent is not absolute: {parent}")
        descriptor = os.open(os.sep, flags)
        for component in parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise sqlite3.OperationalError(
                f"framework SQLite parent contains a symlink or non-directory: {parent}"
            ) from exc
        raise sqlite3.OperationalError(
            f"framework SQLite parent cannot be opened: {parent}"
        ) from exc


def _validate_existing_owner(path: Path) -> tuple[int, int]:
    """Reject linked/non-regular owners and return their physical identity."""

    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise sqlite3.OperationalError(f"unable to open database file: {path}") from exc
    except OSError as exc:
        raise sqlite3.OperationalError(
            f"framework SQLite owner cannot be inspected: {path}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise sqlite3.OperationalError(
            f"framework SQLite owner must be a regular file, not a symlink: {path}"
        )
    parent_fd = _open_real_parent(path)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise sqlite3.OperationalError(
                f"framework SQLite owner is a symlink or non-regular endpoint: {path}"
            ) from exc
        raise sqlite3.OperationalError(f"framework SQLite owner cannot be opened: {path}") from exc
    finally:
        os.close(parent_fd)
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        metadata.st_dev,
        metadata.st_ino,
    ):
        raise sqlite3.OperationalError(f"framework SQLite owner identity changed: {path}")
    return opened.st_dev, opened.st_ino


def connect_existing_framework(
    path: str | Path,
    *,
    readonly: bool,
    timeout_seconds: float = 60.0,
    force_snapshot: bool = False,
    cancellation_check: Callable[[], bool | None] | None = None,
) -> sqlite3.Connection:
    """Open existing framework state without ever creating a replacement file."""

    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("framework SQLite timeout must be positive")
    if type(force_snapshot) is not bool:
        raise TypeError("force_snapshot must be a boolean")
    if force_snapshot and not readonly:
        raise ValueError("force_snapshot is only valid for readonly framework connections")
    if cancellation_check is not None and not callable(cancellation_check):
        raise TypeError("cancellation_check must be callable or None")
    if cancellation_check is not None and not readonly:
        raise ValueError("cancellation_check is only valid for readonly framework connections")
    selected = Path(os.path.abspath(os.fspath(path)))
    try:
        expected_identity: tuple[int, int] | None = _validate_existing_owner(selected)
    except sqlite3.OperationalError:
        # Preserve the injected read-only connection seam used by embedders and
        # interruption tests; the real opener still rejects a missing owner.
        if not readonly or os.path.lexists(selected):
            raise
        expected_identity = None
    if readonly:
        try:
            if force_snapshot or cancellation_check is not None:
                connection = open_sidecar_safe_sqlite_connection(
                    selected,
                    timeout_seconds=timeout_seconds,
                    force_snapshot=force_snapshot,
                    cancellation_check=cancellation_check,
                )
            else:
                # Keep the long-standing injected opener seam source-compatible
                # for embedders that still expose only the original arguments.
                connection = open_sidecar_safe_sqlite_connection(
                    selected,
                    timeout_seconds=timeout_seconds,
                )
        except FileNotFoundError as exc:
            # Only an ENOENT for the authenticated main owner is a missing
            # Framework database.  A snapshot sidecar or temporary destination
            # can disappear during a bounded retry and must retain its own
            # provenance instead of being reported as a missing main owner.
            if exc.errno != errno.ENOENT or exc.filename != os.fspath(selected):
                raise
            raise sqlite3.OperationalError(f"unable to open database file: {selected}") from exc
    else:
        uri = existing_sqlite_uri(selected)
        connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    try:
        if not readonly:
            main = next(
                (
                    str(row[2])
                    for row in connection.execute("PRAGMA database_list")
                    if row[1] == "main"
                ),
                None,
            )
            if main is None or not Path(main).is_absolute():
                raise sqlite3.OperationalError(
                    "framework SQLite connection has no durable main owner"
                )
            current = Path(main).lstat()
            if (
                expected_identity is not None
                and (current.st_dev, current.st_ino) != expected_identity
            ) or stat.S_ISLNK(current.st_mode):
                raise sqlite3.OperationalError(
                    "framework SQLite owner identity changed during open"
                )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout_seconds * 1000))}")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError("framework connection could not enable foreign keys")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise RuntimeError("framework connection is not query-only")
    except BaseException:
        connection.close()
        raise
    return connection


def _read_framework_cancellation_requested(
    path: str | Path,
    run_id: int,
    *,
    timeout_seconds: float,
    control_checkpoint: Callable[[], None] | None = None,
) -> bool:
    """Read the lifecycle cancellation event through the Framework owner.

    The integrated Semantic stage calls this only while its
    :class:`FrameworkRunLock` is held.  Unlike a public owner read, this
    control-plane probe deliberately opens the existing Framework owner through
    its read-write URI, enables connection-local ``query_only``, and performs
    one rollback-only read transaction.  The heartbeat and this probe therefore
    share SQLite's owner-coordinated view without copying a database whose WAL
    is changing between fences.

    ``control_checkpoint`` is an invocation-local callback.  It may raise
    ``KeyboardInterrupt`` for external cancellation or the caller's typed
    deadline exception; it must not consult a SemanticWorkBudget whose own
    cancellation callback is currently executing.
    """

    if control_checkpoint is not None and not callable(control_checkpoint):
        raise TypeError("control_checkpoint must be callable or None")
    selected = Path(os.path.abspath(os.fspath(path)))
    expected_identity = _validate_existing_owner(selected)

    from neocortex.persistence.sqlite_cancellation import (
        SQLiteCancellationBridge,
        sqlite_cancellation_scope,
    )

    connection = connect_existing_framework(
        selected,
        readonly=False,
        timeout_seconds=timeout_seconds,
    )
    bridge = SQLiteCancellationBridge(control_checkpoint)
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RuntimeError("framework cancellation probe is not query-only")
        with sqlite_cancellation_scope(connection, bridge):
            bridge.checkpoint()
            connection.execute("BEGIN")
            bridge.checkpoint()
            cancelled = connection.execute(
                """SELECT 1 FROM run_events WHERE run_id=?
                AND phase='lifecycle-budget'
                AND message='Run cancellation requested' LIMIT 1""",
                (run_id,),
            ).fetchone() is not None
            bridge.checkpoint()
        return cancelled
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if connection.in_transaction:
                connection.rollback()
        except BaseException as exc:
            if primary_error is None:
                cleanup_error = exc
            else:
                primary_error.add_note(
                    "Framework cancellation probe rollback failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        try:
            connection.close()
        except BaseException as exc:
            if primary_error is None:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    cleanup_error.add_note(
                        "Framework cancellation probe close failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            else:
                primary_error.add_note(
                    "Framework cancellation probe close failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        try:
            observed_identity = _validate_existing_owner(selected)
            if observed_identity != expected_identity:
                identity_error = sqlite3.OperationalError(
                    "framework SQLite owner identity changed during cancellation probe"
                )
                if primary_error is None:
                    if cleanup_error is None:
                        cleanup_error = identity_error
                    else:
                        cleanup_error.add_note(str(identity_error))
                else:
                    primary_error.add_note(str(identity_error))
        except BaseException as exc:
            if primary_error is None and cleanup_error is None:
                cleanup_error = exc
            elif primary_error is not None:
                primary_error.add_note(
                    "Framework cancellation probe final owner check failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                assert cleanup_error is not None
                cleanup_error.add_note(
                    "Framework cancellation probe final owner check failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


__all__ = ["connect_existing_framework"]
# endregion [02]
