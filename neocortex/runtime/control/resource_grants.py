"""Resource grant lease implementation.

The lease delegates all mutation to its owning coordinator.  Its methods must
only touch coordinator counters while the coordinator's condition is held; no
second resource owner is created by this module.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from contextvars import Context, ContextVar, copy_context
from contextlib import contextmanager
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken
from .memory_runtime import MemoryBudgetExceeded


_CURRENT_RESOURCE_GRANT: ContextVar["ResourceGrant | None"] = ContextVar(
    "neocortex_resource_grant", default=None
)
_DRAINING_RESOURCE_GRANT: ContextVar["ResourceGrant | None"] = ContextVar(
    "neocortex_draining_resource_grant", default=None
)


def _compatibility_runtime():
    """Resolve the facade's patchable OS/path hooks without a module cycle."""

    # The historical public module exposed ``os`` and ``Path`` and tests and
    # bounded host adapters patch those names to model /proc. Resolve them at
    # call time so moving the lease implementation does not change that seam.
    # Resolve through ``sys.modules`` instead of importing the facade.  This
    # keeps the lease implementation acyclic while preserving the historical
    # monkeypatch surface (tests/adapters patch ``global_resources.os`` and
    # ``global_resources.Path`` in place).
    return sys.modules.get(
        "neocortex.runtime.control.global_resources",
        sys.modules[__name__],
    )


class _CombinedCancellationToken(CancellationToken):
    """Check an owner token and a temporary renewal token together."""

    def __init__(self, owner: CancellationToken | None, extra: CancellationToken):
        super().__init__(parent=extra)
        self._owner = owner
        self._extra = extra

    @property
    def is_cancelled(self) -> bool:
        return super().is_cancelled or (self._owner is not None and self._owner.is_cancelled)

    def checkpoint(self) -> None:
        if self._owner is not None:
            self._owner.checkpoint()
        self._extra.checkpoint()
        super().checkpoint()


class ResourceGrant:
    """A real accounting lease with a renewable execution component."""

    def __init__(self, coordinator, request, cancellation=None):
        self.coordinator = coordinator
        self._request = request
        self._cancellation = cancellation
        self._execution_demand = (request.cpu_slots, request.native_threads, request.io_slots)
        self._resumed_context: tuple[Context, Any] | None = None
        self._resumed_grant: ResourceGrant | None = None
        self._owner_grant: ResourceGrant | None = None

    @property
    def cpu_slots(self) -> int:
        return (
            self._request.cpu_slots
            if self._resumed_grant is None
            else self._resumed_grant.cpu_slots
        )

    @property
    def native_threads(self) -> int:
        return (
            self._request.native_threads
            if self._resumed_grant is None
            else self._resumed_grant.native_threads
        )

    @property
    def native_env(self) -> dict[str, str]:
        threads = str(max(1, self.native_threads))
        return dict.fromkeys(
            (
                "OMP_NUM_THREADS",
                "OMP_THREAD_LIMIT",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
                "BLIS_NUM_THREADS",
            ),
            threads,
        )

    def subprocess_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        environment.update(self.native_env)
        return environment

    def check_cancellation(self) -> None:
        """Check both scope and local owner without changing this lease."""
        self.coordinator.checkpoint()
        if self._cancellation is not None:
            self._cancellation.checkpoint()
        if self._resumed_grant is not None and self._resumed_grant._cancellation is not None:
            self._resumed_grant._cancellation.checkpoint()

    def shrink_transient_bytes(self, new_total: int) -> None:
        """Return finished workspace while retaining the result's own bytes.

        This is a nonblocking reduction only. The owner calls it after the
        discarded workspace is no longer reachable; a result remains charged
        until its consumption finishes and the original lease is closed.
        """
        amount = int(new_total)
        with self.coordinator._condition:
            request = self._request
            if not request.admitted or request.released:
                raise RuntimeError("cannot shrink a released resource grant")
            if amount < 0 or amount > request.transient_bytes:
                raise ValueError("transient reduction cannot grow a reservation")
            released = request.transient_bytes - amount
            request.transient_bytes = amount
            request.memory_bytes -= released
            self.coordinator._reserved_bytes -= released
            self.coordinator._transient_bytes -= released
            metrics = self.coordinator._metrics[request.route_name]
            metrics.reserved_bytes -= released
            metrics.transient_bytes -= released
            self.coordinator._materialized_credit_locked()
            self.coordinator._condition.notify_all()

    def release_cpu(self) -> None:
        """Idempotently release execution while retaining resident/result bytes."""
        resumed = self._resumed_context
        if resumed is not None:
            self._resumed_context = None
            self._resumed_grant = None
            context, admission = resumed
            context.run(admission.__exit__, None, None, None)
        self.coordinator._release_execution(self._request)

    @contextmanager
    def drain_scope(self):
        """Mark bounded owner consumption of this already computed result.

        A checkpoint here uses one CPU/native/I/O unit and may proceed under
        pressure so the owner can persist or discard its retained bytes. This
        scope never authorizes new RAM, temporary or GPU allocations.
        """
        token = _DRAINING_RESOURCE_GRANT.set(self)
        try:
            yield self
        finally:
            _DRAINING_RESOURCE_GRANT.reset(token)

    @contextmanager
    def drain_admission(
        self, *, io_slots: int = 1, io_device: str | None = None, phase: str | None = None
    ):
        """Finalize a retained resident buffer with one bounded execution unit.

        Use this for the last owner batch after worker iteration has ended.
        The original residency remains charged until its owner closes it;
        this admission authorizes no additional RAM, temporary space or GPU.
        """
        self.check_cancellation()
        if not 0 <= io_slots <= 1:
            raise ValueError("drain admission permits at most one I/O unit")
        with self.coordinator.admit(
            self._request.route_name,
            0,
            cpu_slots=1,
            native_threads=1,
            transient_bytes=0,
            io_slots=io_slots,
            io_device=io_device,
            phase=phase or (self._request.phase or "resident") + ":drain",
            cancellation=self._cancellation,
            _draining_from=self._request,
        ) as grant:
            yield grant

    def checkpoint(
        self, *, drain: bool | None = None, cancellation: CancellationToken | None = None
    ) -> None:
        """Yield execution and renegotiate, optionally to finish a result.

        ``drain=True`` is only for bounded finalization of existing work, not
        another inference/parser task. Elastic owner consumption marks this
        automatically. Cancellation still takes precedence over finalization.
        """
        self.check_cancellation()
        if cancellation is not None:
            cancellation.checkpoint()
        if not self._request.admitted or self._request.released:
            raise RuntimeError("cannot renew a released resource grant")
        cpu, native, io = self._execution_demand
        draining = _DRAINING_RESOURCE_GRANT.get() is self if drain is None else drain
        if draining:
            # Finalization is a new bounded owner phase even if the producer
            # was a CPU0/native0 supervisor or had no I/O work of its own.
            cpu, native, io = 1, 1, 1
        if not (cpu or native or io):
            return
        self.release_cpu()
        admission = self.coordinator.admit(
            self._request.route_name,
            0,
            cpu_slots=cpu,
            native_threads=native,
            transient_bytes=0,
            io_slots=io,
            io_device=self._request.io_device,
            phase=(self._request.phase or "work") + ":checkpoint",
            cancellation=(
                self._cancellation
                if cancellation is None
                else _CombinedCancellationToken(self._cancellation, cancellation)
            ),
            _draining_from=self._request if draining else None,
            _renewing_from=None if draining else self._request,
        )
        # A lease moves sequentially between preparation, worker execution and
        # result consumption. Keep the renewal's ContextVar token in its own
        # context so another owner can release it without changing either
        # owner's ambient grant or resetting a token in the wrong context.
        context = copy_context()
        grant = context.run(admission.__enter__)
        grant._owner_grant = self
        self._resumed_context = context, admission
        self._resumed_grant = grant

    def register_process(self, pid: int, start_time_ticks: int | None = None) -> tuple[int, int]:
        """Bind a verified descendant identity for measured memory attribution."""
        runtime = _compatibility_runtime()

        def identity(process_id: int) -> tuple[int, int]:
            raw = runtime.Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2 :].split()
            return int(fields[1]), int(fields[19])

        process_id = int(pid)
        if process_id <= 0 or process_id == runtime.os.getpid():
            raise ValueError("a process lease binds a child, not the entire coordinator")
        parent, started = identity(process_id)
        if start_time_ticks is not None and int(start_time_ticks) != started:
            raise ValueError("process identity changed before resource registration")
        current = parent
        visited = {process_id}
        while current != runtime.os.getpid():
            if current <= 1 or current in visited:
                raise ValueError("resource process is outside this execution tree")
            visited.add(current)
            current, _ = identity(current)
        if identity(process_id) != (parent, started):
            raise ValueError("process identity changed during resource registration")
        result = process_id, started
        with self.coordinator._condition:
            if not self._request.admitted or self._request.released:
                raise RuntimeError("cannot register a process on a released grant")
            self._request.process_identities.add(result)
        # Observation may hold the sampler lock while reading proc. Keep this
        # handoff outside the accounting lock; only live leases can earn credit.
        sampler = self.coordinator._default_sampler
        if sampler is not None:
            sampler._track_verified_process(result)
        return result

    def resize_temp_bytes(
        self,
        new_total: int,
        *,
        directory: str | Path | None = None,
        file_descriptor: int | None = None,
    ) -> None:
        """Reserve spool growth before writing, without holding a blocking wait.

        Pass the owned file descriptor to credit its kernel-observed length;
        bytes still buffered in Python remain pending. Without a descriptor,
        all reserved bytes remain conservative future promises. Growth uses
        that descriptor's filesystem availability. A failure leaves the lease
        unchanged so the owner can publish an explicit bounded result.
        """
        amount = int(new_total)
        if amount < 0:
            raise ValueError("temporary resource size cannot be negative")
        self.coordinator.cancellation.checkpoint()
        if self._cancellation is not None:
            self._cancellation.checkpoint()
        with self.coordinator._condition:
            request = self._request
            if not request.admitted or request.released:
                raise RuntimeError("cannot resize a released resource grant")
            materialized = 0
            identity = None
            if file_descriptor is not None:
                observed = os.fstat(file_descriptor)
                if not stat.S_ISREG(observed.st_mode):
                    raise ValueError("temporary resource descriptor must be a regular file")
                identity = observed.st_dev, observed.st_ino
                if (
                    request.temp_file_identity is not None
                    and request.temp_file_identity != identity
                ):
                    raise ValueError("temporary resource descriptor changed identity")
                if any(
                    other is not request
                    and other.temp_bytes
                    and other.temp_file_identity == identity
                    for other in self.coordinator._active_reservations.values()
                ):
                    raise ValueError("temporary file already belongs to another live grant")
                materialized = min(request.temp_bytes, max(0, observed.st_size))
            delta = amount - request.temp_bytes
            future = self.coordinator._temp_bytes + delta
            if delta > 0:
                explicit = self.coordinator.limits.temp_budget_bytes
                if explicit is not None and future > explicit:
                    raise MemoryBudgetExceeded("temporary storage budget exceeded")
                if file_descriptor is None:
                    free = shutil.disk_usage(directory or tempfile.gettempdir()).free
                else:
                    filesystem = os.fstatvfs(file_descriptor)
                    free = filesystem.f_bavail * filesystem.f_frsize
                pending_other = sum(
                    max(0, other.temp_bytes - other.temp_materialized_bytes)
                    for other in self.coordinator._active_reservations.values()
                    if other is not request
                )
                if max(0, amount - materialized) + pending_other > free:
                    raise MemoryBudgetExceeded(
                        "insufficient filesystem capacity for temporary growth"
                    )
                if explicit is None:
                    other_materialized = sum(
                        other.temp_materialized_bytes
                        for other in self.coordinator._active_reservations.values()
                        if other is not request
                    )
                    self.coordinator.temp_budget_bytes = other_materialized + materialized + free
            request.temp_materialized_bytes = min(amount, materialized)
            request.temp_file_identity = identity if amount else None
            request.temp_bytes = amount
            self.coordinator._temp_bytes = future
            self.coordinator._peak_temp_bytes = max(self.coordinator._peak_temp_bytes, future)
            metrics = self.coordinator._metrics[request.route_name]
            metrics.temp_bytes += delta
            metrics.peak_temp_bytes = max(metrics.peak_temp_bytes, metrics.temp_bytes)
            self.coordinator._condition.notify_all()
