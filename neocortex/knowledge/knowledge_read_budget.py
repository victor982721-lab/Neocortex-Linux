"""Optional, local budgets for bounded Knowledge read operations.

The budget is deliberately an in-memory guard.  It does not become durable
state, does not change an owner, and does not authorize a corpus operation.
Callers may pass it to a read adapter that can account for the corresponding
work.  A missing budget keeps the historical bounded defaults of that adapter.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Callable, Any


KNOWLEDGE_READ_BUDGET_SCHEMA = "neocortex.knowledge-read-budget/v1"
_MAX_COUNTER = 10_000_000
_MAX_TEMPORARY_BYTES = 256 * 1024 * 1024 * 1024


class KnowledgeReadBudgetExceeded(RuntimeError):
    """A read reached one of its caller-owned bounded-work limits."""

    def __init__(self, reason: str) -> None:
        if reason not in {
            "deadline_exceeded",
            "rows_exhausted",
            "vectors_exhausted",
            "temporary_bytes_exhausted",
            "cancelled",
        }:
            raise ValueError(f"unsupported Knowledge read budget reason: {reason}")
        self.reason = reason
        super().__init__(f"Knowledge read budget {reason.replace('_', ' ')}")


def _optional_counter(name: str, value: object, *, maximum: int = _MAX_COUNTER) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum} when provided")


@dataclass(slots=True)
class KnowledgeReadBudget:
    """Caller-owned read limits for rows, vectors, snapshots and time.

    ``deadline_ns`` is an absolute value from ``monotonic_clock``.  The
    optional ``deadline_seconds`` convenience is relative to the first
    checkpoint and is intentionally not serialized as a wall-clock deadline.
    Counters are exposed for telemetry and are resettable only by constructing
    another budget, so a replay cannot silently reuse spent allowance.
    """

    max_rows: int | None = None
    max_vectors: int | None = None
    max_temporary_bytes: int | None = None
    deadline_ns: int | None = None
    deadline_monotonic_ns: int | None = field(default=None, repr=False)
    deadline_seconds: float | None = None
    cancellation_check: Callable[[], bool | None] | None = None
    monotonic_clock: Callable[[], int] = time.monotonic_ns
    rows_used: int = field(default=0, init=False)
    vectors_used: int = field(default=0, init=False)
    temporary_bytes_used: int = field(default=0, init=False)
    checkpoints: int = field(default=0, init=False)
    _relative_deadline_ns: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        _optional_counter("max_rows", self.max_rows)
        _optional_counter("max_vectors", self.max_vectors)
        _optional_counter(
            "max_temporary_bytes",
            self.max_temporary_bytes,
            maximum=_MAX_TEMPORARY_BYTES,
        )
        if self.deadline_ns is not None and self.deadline_monotonic_ns is not None:
            if self.deadline_ns != self.deadline_monotonic_ns:
                raise ValueError("deadline_ns and deadline_monotonic_ns must agree")
        elif self.deadline_monotonic_ns is not None:
            self.deadline_ns = self.deadline_monotonic_ns
        if self.deadline_ns is not None and (
            isinstance(self.deadline_ns, bool)
            or not isinstance(self.deadline_ns, int)
            or self.deadline_ns < 0
        ):
            raise ValueError("deadline_ns must be a non-negative monotonic nanosecond value")
        if self.deadline_seconds is not None and (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not math.isfinite(float(self.deadline_seconds))
            or float(self.deadline_seconds) <= 0
        ):
            raise ValueError("deadline_seconds must be finite and positive")
        if self.cancellation_check is not None and not callable(self.cancellation_check):
            raise TypeError("cancellation_check must be callable or None")
        if not callable(self.monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        if self.deadline_ns is not None and self.deadline_seconds is not None:
            raise ValueError("deadline_ns and deadline_seconds cannot both be provided")
        if self.deadline_seconds is not None:
            self.deadline_seconds = float(self.deadline_seconds)

    @property
    def rows_remaining(self) -> int | None:
        return None if self.max_rows is None else max(0, self.max_rows - self.rows_used)

    @property
    def items_remaining(self) -> int | None:
        """Compatibility alias for adapters that call materialized rows items."""

        return self.rows_remaining

    @property
    def vectors_remaining(self) -> int | None:
        return None if self.max_vectors is None else max(0, self.max_vectors - self.vectors_used)

    @property
    def temporary_bytes_remaining(self) -> int | None:
        return (
            None
            if self.max_temporary_bytes is None
            else max(0, self.max_temporary_bytes - self.temporary_bytes_used)
        )

    def _now_ns(self) -> int:
        value = self.monotonic_clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("Knowledge read budget clock returned an invalid value")
        return value

    def checkpoint(
        self,
        *,
        rows: int = 0,
        vectors: int = 0,
        temporary_bytes: int = 0,
    ) -> None:
        """Account one bounded unit and stop before work exceeds its limit."""

        for label, value in (
            ("rows", rows),
            ("vectors", vectors),
            ("temporary_bytes", temporary_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        self.checkpoints += 1
        if self.cancellation_check is not None:
            try:
                cancelled = self.cancellation_check()
            except BaseException as exc:
                raise KnowledgeReadBudgetExceeded("cancelled") from exc
            if cancelled is not None and cancelled is not False:
                raise KnowledgeReadBudgetExceeded("cancelled")
        now = self._now_ns()
        if self._relative_deadline_ns is None and self.deadline_seconds is not None:
            self._relative_deadline_ns = now + round(self.deadline_seconds * 1_000_000_000)
        deadline = self.deadline_ns if self.deadline_ns is not None else self._relative_deadline_ns
        if deadline is not None and now > deadline:
            raise KnowledgeReadBudgetExceeded("deadline_exceeded")
        if self.max_rows is not None and self.rows_used + rows > self.max_rows:
            raise KnowledgeReadBudgetExceeded("rows_exhausted")
        if self.max_vectors is not None and self.vectors_used + vectors > self.max_vectors:
            raise KnowledgeReadBudgetExceeded("vectors_exhausted")
        if (
            self.max_temporary_bytes is not None
            and self.temporary_bytes_used + temporary_bytes > self.max_temporary_bytes
        ):
            raise KnowledgeReadBudgetExceeded("temporary_bytes_exhausted")
        self.rows_used += rows
        self.vectors_used += vectors
        self.temporary_bytes_used += temporary_bytes

    def to_dict(self) -> dict[str, object]:
        """Return bounded accounting without exposing callback internals."""

        return {
            "schema": KNOWLEDGE_READ_BUDGET_SCHEMA,
            "max_rows": self.max_rows,
            "max_vectors": self.max_vectors,
            "max_temporary_bytes": self.max_temporary_bytes,
            "deadline_configured": self.deadline_ns is not None
            or self.deadline_seconds is not None,
            "rows_used": self.rows_used,
            "vectors_used": self.vectors_used,
            "temporary_bytes_used": self.temporary_bytes_used,
            "checkpoints": self.checkpoints,
        }

    @classmethod
    def from_time_budget(
        cls,
        *,
        max_rows: int | None = None,
        max_vectors: int | None = None,
        max_temporary_bytes: int | None = None,
        time_budget_seconds: float | None = None,
        monotonic_clock: Callable[[], int] = time.monotonic_ns,
        cancellation_check: Callable[[], bool | None] | None = None,
    ) -> "KnowledgeReadBudget":
        """Build a budget with an absolute deadline in one clock domain."""

        deadline_ns = None
        if time_budget_seconds is not None:
            if (
                isinstance(time_budget_seconds, bool)
                or not isinstance(time_budget_seconds, (int, float))
                or not math.isfinite(float(time_budget_seconds))
                or float(time_budget_seconds) <= 0
            ):
                raise ValueError("time_budget_seconds must be finite and positive")
            now = monotonic_clock()
            if isinstance(now, bool) or not isinstance(now, int) or now < 0:
                raise ValueError("monotonic_clock returned an invalid value")
            deadline_ns = now + round(float(time_budget_seconds) * 1_000_000_000)
        return cls(
            max_rows=max_rows,
            max_vectors=max_vectors,
            max_temporary_bytes=max_temporary_bytes,
            deadline_ns=deadline_ns,
            monotonic_clock=monotonic_clock,
            cancellation_check=cancellation_check,
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        monotonic_clock: Callable[[], int] = time.monotonic_ns,
        cancellation_check: Callable[[], bool | None] | None = None,
    ) -> "KnowledgeReadBudget":
        """Parse the bounded wire spelling without accepting unknown fields."""

        if value is None:
            return cls(monotonic_clock=monotonic_clock, cancellation_check=cancellation_check)
        if not isinstance(value, Mapping):
            raise ValueError("Knowledge read budget must be an object")
        allowed = {
            "max_rows",
            "rows",
            "max_vectors",
            "vectors",
            "max_temporary_bytes",
            "temporary_bytes",
            "deadline_ns",
            "deadline_monotonic_ns",
            "deadline_seconds",
            "time_budget_seconds",
        }
        if set(value) - allowed:
            raise ValueError("Knowledge read budget contains unsupported fields")

        def first(*names: str) -> Any:
            for name in names:
                if name in value:
                    return value[name]
            return None

        deadline_seconds = first("deadline_seconds", "time_budget_seconds")
        if deadline_seconds is not None:
            return cls.from_time_budget(
                max_rows=first("max_rows", "rows"),
                max_vectors=first("max_vectors", "vectors"),
                max_temporary_bytes=first("max_temporary_bytes", "temporary_bytes"),
                time_budget_seconds=deadline_seconds,
                monotonic_clock=monotonic_clock,
                cancellation_check=cancellation_check,
            )
        return cls(
            max_rows=first("max_rows", "rows"),
            max_vectors=first("max_vectors", "vectors"),
            max_temporary_bytes=first("max_temporary_bytes", "temporary_bytes"),
            deadline_ns=first("deadline_ns", "deadline_monotonic_ns"),
            monotonic_clock=monotonic_clock,
            cancellation_check=cancellation_check,
        )


__all__ = [
    "KNOWLEDGE_READ_BUDGET_SCHEMA",
    "KnowledgeReadBudget",
    "KnowledgeReadBudgetExceeded",
]
