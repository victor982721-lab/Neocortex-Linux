"""Shared admission and deadline budget for one Semantic index invocation."""

from __future__ import annotations
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class SemanticIndexDeadlineExceeded(TimeoutError):
    """A bounded Semantic producer exhausted its monotonic deadline."""


@dataclass(slots=True)
class SemanticWorkBudget:
    """Mutable invocation budget shared by text, image, OCR and workers.

    ``max_items`` counts complete source items that add, reactivate or rebind
    durable generation state. Exact replay is inspected but refunded so
    bounded resumptions can advance beyond an already-staged prefix.
    ``max_new_jobs`` counts durable job rows first created or reactivated with
    a changed fingerprint. Exact replay does not consume that allowance.
    ``deadline`` is an absolute monotonic
    timestamp so an ``all`` run cannot reset its time budget between scopes.
    """

    max_items: int | None = None
    max_new_jobs: int | None = None
    deadline: float | None = None
    cancellation_check: Callable[[], bool | None] | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )
    preserve_existing_generations: bool = field(default=False, kw_only=True)
    retry_recoverable_errors: bool = field(default=False, kw_only=True)
    _clock: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )
    items_admitted: int = field(default=0, init=False)
    new_jobs_admitted: int = field(default=0, init=False)
    rebound_members: int = field(default=0, init=False)
    truncation_reason: str | None = field(default=None, init=False)
    _resource_closers: list[Callable[[], None]] = field(
        default_factory=list,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name, value in (
            ("max_items", self.max_items),
            ("max_new_jobs", self.max_new_jobs),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer when present")
        if self.deadline is not None and (
            isinstance(self.deadline, bool)
            or not isinstance(self.deadline, (int, float))
            or not math.isfinite(float(self.deadline))
        ):
            raise ValueError("deadline must be finite when present")
        if not callable(self._clock):
            raise TypeError("clock must be callable")
        if self.cancellation_check is not None and not callable(self.cancellation_check):
            raise TypeError("cancellation_check must be callable")
        if not isinstance(self.preserve_existing_generations, bool):
            raise TypeError("preserve_existing_generations must be a boolean")
        if not isinstance(self.retry_recoverable_errors, bool):
            raise TypeError("retry_recoverable_errors must be a boolean")

    @classmethod
    def from_time_budget(
        cls,
        *,
        max_items: int | None = None,
        max_new_jobs: int | None = None,
        time_budget_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        cancellation_check: Callable[[], bool | None] | None = None,
        preserve_existing_generations: bool = False,
        retry_recoverable_errors: bool = False,
    ) -> "SemanticWorkBudget":
        if time_budget_seconds is not None and (
            isinstance(time_budget_seconds, bool)
            or not isinstance(time_budget_seconds, (int, float))
            or not math.isfinite(float(time_budget_seconds))
            or float(time_budget_seconds) <= 0.0
        ):
            raise ValueError("time_budget_seconds must be finite and positive")
        deadline = (
            None
            if time_budget_seconds is None
            else clock() + float(time_budget_seconds)
        )
        return cls(
            max_items=max_items,
            max_new_jobs=max_new_jobs,
            deadline=deadline,
            _clock=clock,
            cancellation_check=cancellation_check,
            preserve_existing_generations=preserve_existing_generations,
            retry_recoverable_errors=retry_recoverable_errors,
        )

    @property
    def truncated(self) -> bool:
        return self.truncation_reason is not None

    def mark_truncated(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("semantic truncation reason cannot be blank")
        if self.truncation_reason is None:
            self.truncation_reason = reason

    def deadline_expired(self) -> bool:
        self._check_cancellation()
        if self.deadline is None:
            return False
        expired = self._clock() >= self.deadline
        if expired:
            self.mark_truncated("time_budget")
        return expired

    def remaining_seconds(self) -> float | None:
        self._check_cancellation()
        if self.deadline is None:
            return None
        remaining = self.deadline - self._clock()
        if remaining <= 0.0:
            self.mark_truncated("time_budget")
            raise SemanticIndexDeadlineExceeded(
                "semantic indexing exhausted its time budget"
            )
        return remaining

    def checkpoint(self) -> None:
        self._check_cancellation()
        if self.deadline is not None:
            self.remaining_seconds()

    def _check_cancellation(self) -> None:
        if self.cancellation_check is not None and self.cancellation_check() is True:
            self.mark_truncated("cancelled")
            raise KeyboardInterrupt("Semantic indexing was cancelled")

    def try_admit_item(self) -> bool:
        if self.truncated:
            return False
        if self.deadline_expired():
            return False
        if self.max_items is not None and self.items_admitted >= self.max_items:
            self.mark_truncated("max_items")
            return False
        self.items_admitted += 1
        return True

    def new_job_allowance(self, requested: int) -> int:
        if requested < 1:
            raise ValueError("requested job allowance must be positive")
        if self.deadline_expired():
            return 0
        if self.max_new_jobs is None:
            return requested
        remaining = self.max_new_jobs - self.new_jobs_admitted
        return min(requested, max(0, remaining))

    def record_new_jobs(self, count: int) -> None:
        if count < 0:
            raise ValueError("new job count cannot be negative")
        if self.max_new_jobs is not None and (
            self.new_jobs_admitted + count > self.max_new_jobs
        ):
            raise RuntimeError("semantic staging exceeded max_new_jobs")
        self.new_jobs_admitted += count

    def record_rebound_members(self, count: int) -> None:
        """Record metadata-only snapshot work that reused vector payloads."""

        if count < 0:
            raise ValueError("rebound member count cannot be negative")
        self.rebound_members += count

    def mark_job_limit(self) -> None:
        self.mark_truncated("max_new_jobs")

    def refund_replayed_item(self) -> None:
        if self.items_admitted < 1:
            raise RuntimeError("cannot refund an item that was not admitted")
        self.items_admitted -= 1

    def register_resource_closer(self, closer: Callable[[], None]) -> None:
        if not callable(closer):
            raise TypeError("semantic resource closer must be callable")
        self._resource_closers.append(closer)

    def close_registered_resources(self) -> None:
        closers, self._resource_closers = self._resource_closers, []
        first_error: BaseException | None = None
        for closer in reversed(closers):
            try:
                closer()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def unlimited_semantic_work_budget() -> SemanticWorkBudget:
    """Return an invocation-local unlimited budget for API compatibility."""

    return SemanticWorkBudget()
