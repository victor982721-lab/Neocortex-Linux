"""Safe cooperative-cancellation bridge for bounded SQLite work."""
# region [00] Contexto del módulo
# Módulo: neocortex/persistence/sqlite_cancellation.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
# endregion [01]

# region [02] Implementación


CancellationCheck = Callable[[], None]
DEFAULT_PROGRESS_INSTRUCTIONS = 1_000


class SQLiteCancellationBridge:
    """Turn a raising checkpoint into SQLite's integer progress protocol.

    ``sqlite3`` cannot propagate an exception raised by a progress callback
    directly. The callback therefore records the original ``BaseException``
    and asks SQLite to interrupt. The surrounding scope then re-raises that
    exact exception instead of leaking a generic ``OperationalError``.
    """

    __slots__ = ("_cancellation_check", "_captured")

    def __init__(self, cancellation_check: CancellationCheck | None) -> None:
        self._cancellation_check = cancellation_check
        self._captured: BaseException | None = None

    @property
    def enabled(self) -> bool:
        return self._cancellation_check is not None

    @property
    def captured_exception(self) -> BaseException | None:
        return self._captured

    def checkpoint(self) -> None:
        if self._cancellation_check is None:
            return
        try:
            self._cancellation_check()
        except BaseException as exc:
            if self._captured is None:
                self._captured = exc
            raise

    def sqlite_progress(self) -> int:
        if self._captured is not None:
            return 1
        try:
            self.checkpoint()
        except BaseException:
            return 1
        return 0

    def reraise_if_captured(self, cause: BaseException) -> None:
        if self._captured is not None and self._captured is not cause:
            raise self._captured from cause


@contextmanager
def sqlite_cancellation_scope(
    connection: sqlite3.Connection,
    bridge: SQLiteCancellationBridge,
    *,
    instructions: int = DEFAULT_PROGRESS_INSTRUCTIONS,
) -> Iterator[SQLiteCancellationBridge]:
    """Install and always clear a progress handler around SQLite work.

    Transaction ownership remains with the caller. For write transactions,
    nest the connection transaction inside this scope so rollback occurs before
    a generic SQLite interruption is remapped to the captured exception.
    """

    if instructions <= 0:
        raise ValueError("SQLite progress instructions must be positive")
    if bridge.enabled:
        connection.set_progress_handler(bridge.sqlite_progress, instructions)
    primary_error: BaseException | None = None
    try:
        try:
            yield bridge
        except BaseException as exc:
            bridge.reraise_if_captured(exc)
            raise
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if bridge.enabled:
            try:
                connection.set_progress_handler(None, 0)
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    "SQLite progress handler cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )


__all__ = [
    "DEFAULT_PROGRESS_INSTRUCTIONS",
    "CancellationCheck",
    "SQLiteCancellationBridge",
    "sqlite_cancellation_scope",
]
# endregion [02]
