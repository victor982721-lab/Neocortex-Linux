"""Invocation-owned read allowance and reusable, fenced observations.

There is no process-global cache. Nested retrieval adapters borrow this scope;
service retries retain spent allowance and discard their observation cache.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn, Protocol
import sys

class ReadAllowance(Protocol):
    @property
    def interruptible(self) -> bool: ...
    @property
    def rows_remaining(self) -> int | None: ...
    @property
    def vectors_remaining(self) -> int | None: ...
    @property
    def temporary_bytes_remaining(self) -> int | None: ...
    def checkpoint(self, *, rows: int = 0, vectors: int = 0, temporary_bytes: int = 0) -> None: ...
    def reject(self, reason: str) -> NoReturn: ...
    def remaining_seconds(self) -> float | None: ...


@dataclass(slots=True)
class ReadOperation:
    budget: ReadAllowance | None
    cancellation: Callable[[], None] | None
    failure: BaseException | None = None
    observations: dict[object, Any] = field(default_factory=dict)
    observations_reused: int = 0
    closers: list[Callable[[], None]] = field(default_factory=list)

    @property
    def requires_supervision(self) -> bool:
        return self.cancellation is not None or (self.budget is not None and self.budget.interruptible)

    def checkpoint(self, *, rows: int = 0, vectors: int = 0, temporary_bytes: int = 0) -> None:
        if self.failure is not None:
            raise self.failure
        try:
            if self.cancellation is not None:
                self.cancellation()
            if self.budget is not None:
                self.budget.checkpoint(rows=rows, vectors=vectors, temporary_bytes=temporary_bytes)
        except BaseException as exc:
            self.failure = exc
            raise


_OPERATION: ContextVar[ReadOperation | None] = ContextVar("read_operation", default=None)


def current_read_operation() -> ReadOperation | None:
    return _OPERATION.get()


@contextmanager
def read_operation(
    budget: ReadAllowance | None, cancellation: Callable[[], None] | None,
) -> Iterator[ReadOperation]:
    operation = ReadOperation(budget, cancellation)
    token = _OPERATION.set(operation)
    try:
        operation.checkpoint()
        yield operation
        operation.checkpoint()
    except BaseException as exc:
        if operation.failure is not None and operation.failure is not exc:
            raise operation.failure from exc
        raise
    finally:
        primary = sys.exception()
        cleanup_error = None
        for close in reversed(operation.closers):
            try:
                close()
            except BaseException as exc:
                if primary is not None:
                    primary.add_note(f"read resource cleanup failed: {type(exc).__name__}: {exc}")
                elif cleanup_error is None:
                    cleanup_error = exc
        operation.closers.clear()
        operation.observations.clear()
        _OPERATION.reset(token)
        if cleanup_error is not None:
            raise cleanup_error


def read_checkpoint(*, rows: int = 0, vectors: int = 0) -> None:
    operation = _OPERATION.get()
    if operation is not None:
        operation.checkpoint(rows=rows, vectors=vectors)


def remaining_read_limit(limit: int, *, vectors: bool = False) -> int:
    operation = _OPERATION.get()
    if operation is None or operation.budget is None:
        return limit
    operation.checkpoint()
    remaining = operation.budget.vectors_remaining if vectors else operation.budget.rows_remaining
    if remaining is not None:
        if remaining == 0:
            # Preserve the typed signal even through adapters that translate
            # RuntimeError into an unavailable owner or ranking.
            operation.checkpoint(vectors=1 if vectors else 0, rows=0 if vectors else 1)
        return min(limit, remaining)
    return limit


def read_query_limit(limit: int) -> int:
    """Bound SQL output while retaining one unadmitted exhaustion probe.

    Use with ``read_rows`` (or a per-row admission callback). A query that
    needs N+1 rows cannot appear complete merely because its allowance is N.
    """
    operation = _OPERATION.get()
    if operation is None or operation.budget is None:
        return limit
    operation.checkpoint()
    remaining = operation.budget.rows_remaining
    return limit if remaining is None else min(limit, remaining + 1)


def read_rows(cursor: Any) -> list[Any]:
    """Materialize bounded batches, charging each before consumers see it."""
    operation = _OPERATION.get()
    if operation is None or operation.budget is None:
        return list(cursor.fetchall())
    result: list[Any] = []
    while True:
        operation.checkpoint()
        if operation.budget.rows_remaining == 0:
            # A single exhaustion probe distinguishes exactly N rows from
            # N+1. The lookahead is never admitted or handed to consumers.
            if cursor.fetchone() is None:
                return result
            operation.checkpoint(rows=1)
        size = remaining_read_limit(128)
        batch = cursor.fetchmany(size)
        operation.checkpoint(rows=len(batch))
        result.extend(batch)
        if len(batch) < size:
            return result


def snapshot_read_budget():
    """Pass the shared deadline/cancellation and remaining bytes to the kernel."""
    operation = _OPERATION.get()
    if operation is None or (operation.budget is None and operation.cancellation is None):
        return None
    from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudget

    operation.checkpoint()
    remaining = None if operation.budget is None else operation.budget.temporary_bytes_remaining
    # Zero bytes is useful for strict, zero-copy owners. A copying caller
    # must call admit_snapshot before opening; SQLite's budget requires > 0.
    return SQLiteSnapshotBudget(
        max_temporary_bytes=256 * 1024 * 1024 if remaining is None else max(1, remaining),
        cancellation_check=operation.checkpoint,
    )


def admit_snapshot(path, mode: str) -> None:
    operation = _OPERATION.get()
    if operation is None or operation.budget is None or mode != "snapshot_temp":
        return
    from neocortex.persistence.sqlite_immutable import capture_sqlite_read_fence

    fence = capture_sqlite_read_fence(path)
    minimum = fence.main.size + sum(value.size for _suffix, value in fence.sidecars)
    remaining = operation.budget.temporary_bytes_remaining
    if remaining is not None and minimum > remaining:
        try:
            operation.budget.reject("temporary_bytes_exhausted")
        except BaseException as exc:
            operation.failure = exc
            raise
    operation.checkpoint()


def charge_snapshot(temporary_bytes: int) -> None:
    operation = _OPERATION.get()
    if operation is not None:
        operation.checkpoint(temporary_bytes=temporary_bytes)


@contextmanager
def snapshot_allowance() -> Iterator[None]:
    """Keep the caller's typed byte-limit failure across SQLite adapters."""
    from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudgetExceeded
    try:
        yield
    except SQLiteSnapshotBudgetExceeded as exc:
        operation = _OPERATION.get()
        if (operation is not None and operation.budget is not None
            and operation.budget.temporary_bytes_remaining is not None
            and exc.reason == "temporary_bytes"):
            try:
                operation.budget.reject("temporary_bytes_exhausted")
            except BaseException as failure:
                operation.failure = failure
                raise failure from exc
        raise


def operation_strict_sqlite_connection(path, *, timeout_seconds: float = 60.0):
    """Compatibility factory whose bare connection retains its final fence."""
    from neocortex.persistence.sqlite_immutable import (
        SQLiteReadSession, SQLiteReadMode, open_immutable_sqlite_connection,
    )
    operation = _OPERATION.get()
    budget = snapshot_read_budget()
    if operation is None or budget is None:
        return open_immutable_sqlite_connection(path, timeout_seconds=timeout_seconds)
    budget = replace(budget, prepare_timeout_seconds=min(timeout_seconds, budget.prepare_timeout_seconds))
    session = SQLiteReadSession(path, mode=SQLiteReadMode.IMMUTABLE_STRICT,
                                timeout_seconds=timeout_seconds, budget=budget)
    connection = session.open()
    operation.closers.append(session.close)
    operation.checkpoint()
    return connection


@contextmanager
def operation_sqlite_session(path, *, mode=None, timeout_seconds: float = 60.0):
    """Read one detached/strict owner under the ambient operation allowance."""
    from neocortex.persistence.sqlite_immutable import (
        SQLiteReadSession, SQLiteReadMode, preferred_sqlite_read_mode,
    )
    selected_mode = preferred_sqlite_read_mode(path) if mode is None else SQLiteReadMode(mode)
    admit_snapshot(path, selected_mode.value)
    budget = snapshot_read_budget()
    if budget is not None:
        budget = replace(budget, prepare_timeout_seconds=min(timeout_seconds, budget.prepare_timeout_seconds))
    session = SQLiteReadSession(path, mode=selected_mode, timeout_seconds=timeout_seconds, budget=budget)
    with snapshot_allowance(), session as connection:
        # The kernel enforces the cap before each copy block; charge its
        # final retained owner size before exposing the connection.
        # A strict session's preparation is zero-copy.
        charge_snapshot(session.metrics.temporary_bytes)
        yield connection
        read_checkpoint()
