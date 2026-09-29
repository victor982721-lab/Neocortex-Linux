"""Bounded adaptive text evidence, with the existing resource/cancellation owner.

Escalation is opt-in for a structured-text candidate, never a full read of every
file. The reservation is a conservative workspace bound, not a CPU-savings
claim. No parser may retain this payload after leaving the context.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from typing import BinaryIO

STRUCTURED_PROBE_LIMIT = 8 * 1024 * 1024
PROBE_CHUNK_BYTES = 64 * 1024
TEXT_WORKSPACE_FACTOR = 24


def _workspace_can_fit(coordinator: object, route_name: str, workspace: int) -> bool:
    """Check the hard memory ceiling before nesting a lease.

    Identify's worker already owns a CPU/I/O lease.  A second admission that
    can never fit because of that same lease would wait forever: the worker
    cannot release its outer lease until the detector returns.  This is only a
    conservative preflight; the coordinator remains the authority and the
    nested admission still performs the live check under its condition.
    """

    try:
        summary = coordinator.summary()  # type: ignore[attr-defined]
        configured = int(summary.memory_budget_bytes)  # type: ignore[attr-defined]
        effective = int(summary.effective_memory_budget_bytes or 0)  # type: ignore[attr-defined]
        budget = effective if effective > 0 else configured
        route_budget = int(coordinator.route_memory_budget_bytes(route_name))  # type: ignore[attr-defined]
        budget = min(budget, route_budget)
        used = int(summary.resident_bytes) + int(summary.transient_bytes)  # type: ignore[attr-defined]
        return workspace >= 0 and used + workspace <= budget
    except (AttributeError, TypeError, ValueError, OverflowError, OSError, RuntimeError):
        # An unverifiable headroom observation is not permission to block a
        # worker on an admission it cannot later release.  Abstention keeps the
        # detector conservative and lets the owner record UNKNOWN/recovery.
        return False


def probe_checkpoint(check: Callable[[], None] | None = None) -> None:
    if check is not None:
        check()
    from neocortex.runtime.control.global_resources import current_resource_grant, current_resource_coordinator
    grant = current_resource_grant()
    if grant is not None:
        grant.check_cancellation()
    elif (coordinator := current_resource_coordinator()) is not None:
        coordinator.checkpoint()


def structured_candidate(value: str) -> bool:
    sample = value.lstrip('\ufeff \t\r\n')
    if sample.startswith(('{', '[', '<')):
        return True
    lines = sample.splitlines()[:8]
    return len(lines) >= 2 and any(
        sum(delimiter in line for line in lines) >= 2 for delimiter in (',', '\t')
    )


@contextmanager
def structured_probe(
    stream: BinaryIO,
    prefix: bytes,
    *,
    size: int,
    limit: int = STRUCTURED_PROBE_LIMIT,
    checkpoint: Callable[[], None] | None = None,
) -> Iterator[tuple[bytes, bool]]:
    """Read only the remainder of a capped candidate, checking every chunk.

    The caller's initial prefix stays charged to its original grant. A nested
    memory-only admission does not claim a second CPU or I/O slot: those are
    held by Identify. Direct callers with a bound coordinator acquire their
    own lease.
    """
    if limit < len(prefix):
        raise ValueError('probe limit cannot be below the captured prefix')
    from neocortex.runtime.control.global_resources import (
        current_resource_coordinator, current_resource_grant, resource_gate,
    )
    outer = current_resource_grant()
    coordinator = outer.coordinator if outer is not None else current_resource_coordinator()
    workspace = min(max(size, len(prefix)), limit) * TEXT_WORKSPACE_FACTOR
    admission = nullcontext()
    if coordinator is not None:
        gate = resource_gate('actions.identify.escalation', coordinator)
        # An outer Identify worker owns the execution slot and cannot release
        # it while this detector is waiting.  Refuse an impossible nested
        # reservation instead of creating a coordinator self-deadlock.
        if outer is not None and not _workspace_can_fit(
            coordinator, 'actions.identify.escalation', workspace
        ):
            probe_checkpoint(checkpoint)
            yield prefix, False
            probe_checkpoint(checkpoint)
            return
        admission = gate.admit(
            workspace, cpu_slots=0 if outer is not None else 1,
            native_threads=0 if outer is not None else 1,
            io_slots=0 if outer is not None else 1,
            phase='identify.structured-text', cancellation=coordinator.cancellation,
        )

    try:
        with admission:
            probe_checkpoint(checkpoint)
            chunks = [prefix]
            total = len(prefix)
            complete = total >= size
            while not complete and total < limit:
                probe_checkpoint(checkpoint)
                chunk = stream.read(min(PROBE_CHUNK_BYTES, limit - total))
                if not chunk:
                    complete = True
                    break
                chunks.append(chunk)
                total += len(chunk)
                complete = total >= size
            probe_checkpoint(checkpoint)
            yield b''.join(chunks), complete
            probe_checkpoint(checkpoint)
    except Exception as exc:
        # A concurrent pressure transition may invalidate the preflight after
        # another worker wins the same budget.  With an outer grant, safe
        # abstention is preferable to waiting indefinitely; standalone callers
        # retain the coordinator's original error semantics.
        from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded
        from neocortex.runtime.control.resource_admission import ResourceWaitTimeout

        if outer is None or not isinstance(exc, (MemoryBudgetExceeded, ResourceWaitTimeout)):
            raise
        probe_checkpoint(checkpoint)
        yield prefix, False
        probe_checkpoint(checkpoint)
