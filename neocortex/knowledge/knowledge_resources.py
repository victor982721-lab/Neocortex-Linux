"""Bounded Knowledge phases sharing the framework resource coordinator."""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable, TypeVar

from neocortex.runtime.control.global_resources import resource_gate
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded
from neocortex.semantic.semantic_resources import (
    CheckpointCancellation, dependent_memory_bytes, retained_dependency,
)

_R = TypeVar("_R")


def knowledge_result_budget(function: Callable[..., _R]) -> Callable[..., _R]:
    """Keep accumulated candidates charged across phases until owner fusion."""

    @wraps(function)
    def run(paths: Any, plan: Any, *args: Any, **kwargs: Any) -> _R:
        gate = resource_gate("knowledge")
        if gate is None:
            return function(paths, plan, *args, **kwargs)
        candidates = sum(step.candidate_limit for step in plan.steps)
        retained = 8 * 1024 * 1024 + max(plan.limit, candidates) * 8192
        working = 8 * 1024 * 1024 + max(1, plan.limit) * 8192
        if dependent_memory_bytes() + retained + working > gate.coordinator.memory_budget_bytes:
            raise MemoryBudgetExceeded("knowledge candidates and one phase exceed the memory budget")
        with gate.admit(
            retained, cpu_slots=0, native_threads=0, phase="query-candidates",
            cancellation=CheckpointCancellation(kwargs.get("cancellation_check")),
        ), retained_dependency(retained):
            return function(paths, plan, *args, **kwargs)
    return run


def knowledge_phase(function: Callable[..., _R]) -> Callable[..., _R]:
    @wraps(function)
    def run(execution: Any, *args: Any, **kwargs: Any) -> _R:
        gate = resource_gate("knowledge")
        if gate is None:
            return function(execution, *args, **kwargs)
        original = execution.cancellation_check
        cancellation = CheckpointCancellation(original)
        estimate = 8 * 1024 * 1024 + max(1, execution.plan.limit) * 8192
        with gate.admit(
            estimate, native_threads=1, io_slots=1,
            phase=function.__name__.removeprefix("_"), cancellation=cancellation,
        ) as grant:
            last_checkpoint = time.monotonic()

            def checkpoint() -> None:
                nonlocal last_checkpoint
                if original is not None:
                    original()
                now = time.monotonic()
                if now - last_checkpoint >= 0.1:
                    grant.checkpoint()
                    last_checkpoint = now

            execution.cancellation_check = checkpoint
            try:
                return function(execution, *args, **kwargs)
            finally:
                execution.cancellation_check = original

    return run
