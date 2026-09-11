"""Invocation-local cancellation for source metadata reads, not another budget."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import sqlite3

from neocortex.persistence.sqlite_immutable import SQLiteSnapshotBudget
from .semantic_work_budget import SemanticWorkBudget


@dataclass(slots=True)
class _SourceReadContext:
    budget: SemanticWorkBudget
    captured: BaseException | None = None


_SOURCE_READ: ContextVar[_SourceReadContext | None] = ContextVar("semantic_source_read", default=None)


def source_read_checkpoint() -> None:
    context = _SOURCE_READ.get()
    if context is not None:
        if context.captured is not None:
            raise context.captured
        try:
            context.budget.checkpoint()
        except BaseException as exc:
            context.captured = exc
            raise


def _sqlite_progress() -> int:
    context = _SOURCE_READ.get()
    try:
        source_read_checkpoint()
    except BaseException as exc:
        if context is not None:
            context.captured = exc
        return 1
    return 0


def install_source_progress(connection: sqlite3.Connection) -> None:
    """Install only on source-owned connections, which close after the read."""

    if _SOURCE_READ.get() is not None:
        source_read_checkpoint()
        connection.set_progress_handler(_sqlite_progress, 1_000)


def source_snapshot_budget() -> SQLiteSnapshotBudget | None:
    context = _SOURCE_READ.get()
    if context is None:
        return None
    remaining = context.budget.remaining_seconds()
    return SQLiteSnapshotBudget(
        prepare_timeout_seconds=60.0 if remaining is None else min(60.0, remaining),
        cancellation_check=source_read_checkpoint,
    )


@contextmanager
def semantic_source_read_budget(budget: SemanticWorkBudget) -> Iterator[None]:
    """Reuse the caller's budget across nested SQL, snapshot and digest work."""

    token = _SOURCE_READ.set(_SourceReadContext(budget))
    try:
        source_read_checkpoint()
        yield
        source_read_checkpoint()
    except BaseException as exc:
        context = _SOURCE_READ.get()
        if context is not None and context.captured is not None and context.captured is not exc:
            raise context.captured from exc
        raise
    finally:
        _SOURCE_READ.reset(token)
