"""Invocation-owned model residence and renewable Semantic/Knowledge work."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, copy_context
from functools import wraps
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any, TypeVar, ParamSpec

from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits,
    current_resource_coordinator, resource_gate, resource_scope,
)
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded

from .semantic_models import BackendEmbedding, EmbeddingModality, EmbeddingModelSpec, EmbeddingRequest

if TYPE_CHECKING:
    from .semantic_backends import EmbeddingBackend

_P = ParamSpec("_P")
_R = TypeVar("_R")
MIB = 1024 * 1024
_BACKENDS: ContextVar[list["CoordinatedEmbeddingBackend"] | None] = ContextVar(
    "semantic_resource_backends", default=None,
)
_STAGING_WORK: ContextVar[tuple[Any, int] | None] = ContextVar("semantic_staging_work", default=None)
_DEPENDENT_MEMORY: ContextVar[int] = ContextVar("retrieval_dependent_memory", default=0)


def dependent_memory_bytes() -> int:
    """Bytes the current invocation must keep while a nested phase executes."""
    return _DEPENDENT_MEMORY.get()


@contextmanager
def retained_dependency(estimated_bytes: int) -> Iterator[None]:
    token = _DEPENDENT_MEMORY.set(_DEPENDENT_MEMORY.get() + estimated_bytes)
    try:
        yield
    finally:
        _DEPENDENT_MEMORY.reset(token)


class CheckpointCancellation(CancellationToken):
    """Preserve the caller's typed deadline/cancellation during resource waits."""

    def __init__(self, checkpoint: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._checkpoint_callback = checkpoint

    @property
    def is_cancelled(self) -> bool:
        if self._checkpoint_callback is not None:
            self._checkpoint_callback()
        return super().is_cancelled


@contextmanager
def retrieval_resource_scope(route: str) -> Iterator[None]:
    shared = current_resource_coordinator()
    owned = shared is None
    coordinator = shared or GlobalResourceCoordinator((route,), GlobalResourceLimits())
    coordinator.register_route(route)
    backends = _BACKENDS.get()
    token = None if backends is not None else _BACKENDS.set([])
    try:
        with resource_scope(coordinator) if owned else nullcontext():
            try:
                yield
            finally:
                if token is not None:
                    first_error: BaseException | None = None
                    for backend in reversed(_BACKENDS.get() or []):
                        try:
                            backend.close()
                        except BaseException as exc:
                            if first_error is None:
                                first_error = exc
                    if first_error is not None:
                        raise first_error
    finally:
        if token is not None:
            _BACKENDS.reset(token)


def governed_retrieval(route: str):
    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with retrieval_resource_scope(route):
                return function(*args, **kwargs)
        return invoke
    return decorate


def staging_batch_capacity(default: int) -> int:
    active = _STAGING_WORK.get()
    return default if active is None else min(default, active[1])


def staging_commit_checkpoint() -> None:
    """Called only after COMMIT, never while the writer waits on resources."""

    active = _STAGING_WORK.get()
    if active is not None:
        active[0].checkpoint()


def governed_staging(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        from .semantic_source_budget import source_read_checkpoint

        gate = resource_gate("semantic")
        if gate is None:
            return function(*args, **kwargs)
        chunking: Any = kwargs["chunking"]
        owner_check = kwargs.get("cancellation_check")
        work_budget: Any = kwargs.get("work_budget")
        def check():
            source_read_checkpoint()
            if callable(owner_check):
                owner_check()
            if work_budget is not None:
                work_budget.checkpoint()
        per_chunk = int(chunking.max_chars) * 8 + 4096
        summary = gate.coordinator.summary()
        remaining = max(0, summary.effective_memory_budget_bytes - summary.resident_bytes)
        token_workspace = 0
        if kwargs.get("token_counter") is not None:
            token_windows = max(1, min(32, 2 * MIB // chunking.max_chars))
            token_workspace = max(MIB, token_windows * chunking.max_chars * 8)
        own_models = summary.routes["semantic"].resident_bytes
        if dependent_memory_bytes() + own_models + 4 * MIB + per_chunk + token_workspace > gate.coordinator.memory_budget_bytes:
            raise MemoryBudgetExceeded("semantic model and one staging window exceed the memory budget")
        # Reserve the bounded writer buffer while leaving room for the model's
        # exact-tokenizer workspace. Large caller-selected windows reduce the
        # slice length instead of materializing 128 unbounded-size chunks.
        capacity = max(1, min(128, max(0, remaining // 2 - 4 * MIB) // per_chunk))
        buffer_bytes = 4 * MIB + capacity * per_chunk
        with gate.admit(
            buffer_bytes, native_threads=1, io_slots=1,
            phase="text-staging", cancellation=CheckpointCancellation(check),
        ) as grant, retained_dependency(buffer_bytes):
            token = _STAGING_WORK.set((grant, capacity))
            try:
                return function(*args, **kwargs)
            finally:
                _STAGING_WORK.reset(token)
    return invoke


def native_vector_operation(estimate: Callable[..., int]):
    """Keep numerical temporary memory and BLAS execution in the shared gate."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            from neocortex.runtime.control.native_library_resources import native_library_operation
            from neocortex.runtime.control.read_operation import read_checkpoint

            with retrieval_resource_scope("semantic"):
                gate = resource_gate("semantic")
                assert gate is not None
                with native_library_operation(
                    gate, max(1, estimate(*args, **kwargs)),
                    cancellation=CheckpointCancellation(read_checkpoint),
                ):
                    return function(*args, **kwargs)
        return invoke
    return decorate


@contextmanager
def vector_scoring_scope(rows: int, dimensions: int, *,
                         checkpoint: Callable[[], None] | None = None) -> Iterator[None]:
    from neocortex.runtime.control.native_library_resources import native_library_operation

    with retrieval_resource_scope("semantic"):
        gate = resource_gate("semantic")
        assert gate is not None
        with native_library_operation(
            gate, max(1, rows * dimensions * 16 + rows * 64),
            cancellation=CheckpointCancellation(checkpoint),
        ):
            yield


@contextmanager
def vector_workspace_scope(
    estimated_bytes: int, *, score_bytes: int,
    checkpoint: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Retain numeric grouping RAM between independently admitted score batches."""

    with retrieval_resource_scope("semantic"):
        gate = resource_gate("semantic")
        assert gate is not None
        backends = _BACKENDS.get() or ()
        model_bytes = sum(
            backend._result_bytes
            + (backend._resident_bytes if backend._residence is not None else 0)
            for backend in backends
        )
        if (dependent_memory_bytes() + model_bytes + estimated_bytes + score_bytes
                > gate.coordinator.memory_budget_bytes):
            raise MemoryBudgetExceeded(
                "numeric search workspace and one score batch exceed the memory budget"
            )
        with gate.admit(
            estimated_bytes, cpu_slots=0, native_threads=0,
            phase="vector-workspace", cancellation=CheckpointCancellation(checkpoint),
        ), retained_dependency(estimated_bytes):
            yield


def model_resident_estimate(model: EmbeddingModelSpec, cache_dir: Path) -> int:
    """Account local model bytes and runtime overhead before loading a model."""

    from .semantic_config import fastembed_cache_contract, local_fastembed_snapshot

    if not model.provider.startswith("fastembed"):
        return 0
    snapshot = local_fastembed_snapshot(model, cache_dir)
    contract = fastembed_cache_contract(model.model_signature)
    total = sum(snapshot.joinpath(*relative.split("/")).stat().st_size
                for relative in contract.required_files)
    # Weight decoding plus the owned interpreter/session, separate from batch
    # activations.  The global sampler can credit a registered child process.
    return max(64 * MIB, total * 2 + 64 * MIB)


class CoordinatedEmbeddingBackend:
    """One model identity; reconfigure only between bounded native calls.

    An idle model owns RAM and no CPU/native slots.  Before another model is
    loaded, inactive peers release their sessions and reservations.  Facade
    scopes close all retained models; publication and SQLite remain outside.
    """

    def __init__(
        self, model: EmbeddingModelSpec, factory: Callable[[int], EmbeddingBackend],
        *, gate: CoordinatedMemoryGate, max_threads: int | None = None,
        resident_bytes: int = 0, checkpoint: Callable[[], None] | None = None,
        request_bytes: int | None = None,
    ) -> None:
        if max_threads is not None and max_threads < 1:
            raise ValueError("semantic threads must be positive")
        self._model = model
        self._factory = factory
        self._gate = gate
        self._max_threads = max_threads
        self._resident_bytes = resident_bytes
        self._cancellation = CheckpointCancellation(checkpoint)
        self._request_bytes = (
            8 * MIB if model.modality is EmbeddingModality.TEXT else 16 * MIB
        ) if request_bytes is None else request_bytes
        self._batch_bound = 64 if model.modality is EmbeddingModality.TEXT else 8
        self._backend: EmbeddingBackend | None = None
        self._residence: Any = None
        self._residence_context: Any = None
        self._residence_grant: Any = None
        self._threads: int | None = None
        self._results: list[tuple[Any, Any]] = []
        self._result_bytes = 0
        self._active = False
        backends = _BACKENDS.get()
        if backends is not None:
            backends.append(self)

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._model

    @property
    def max_batch_size(self) -> int:
        self._cancellation.checkpoint()
        self._reserve_model()
        available = self._gate.worker_capacity(
            max_workers=self._max_threads, estimated_bytes=self._request_bytes,
        )
        return min(self._batch_bound, max(1, available))

    def _invocation_result_bytes(self) -> int:
        backends = _BACKENDS.get() or ()
        return sum(backend._result_bytes for backend in backends) + (
            0 if self in backends else self._result_bytes
        )

    def _reserve_model(self, working_bytes: int | None = None) -> None:
        working = self._request_bytes if working_bytes is None else working_bytes
        if (dependent_memory_bytes() + self._resident_bytes + self._invocation_result_bytes() + working
                > self._gate.coordinator.memory_budget_bytes):
            raise MemoryBudgetExceeded("semantic model and dependent work exceed the memory budget")
        if self._gate.worker_capacity(estimated_bytes=0) == 0:
            # A drained boundary may return model RAM to other applications.
            # Already returned vectors keep their separate result leases.
            self._unload_model()
            with self._gate.admit(
                0, native_threads=1, phase="model-resume", cancellation=self._cancellation,
            ):
                pass
        if self._residence is not None:
            return
        for other in _BACKENDS.get() or ():
            if other is not self and not other._active:
                other._unload_model()
        context = copy_context()
        residence = self._gate.admit(
            0, cpu_slots=0, native_threads=0, resident_bytes=self._resident_bytes,
            resident_key=f"semantic-model:{id(self)}:{self.model.model_signature}",
            phase="model-resident", cancellation=self._cancellation,
        )
        grant = context.run(residence.__enter__)
        self._residence, self._residence_context, self._residence_grant = residence, context, grant

    def _ensure_backend(self, threads: int, *, reconfigure: bool = True) -> EmbeddingBackend:
        if self._backend is None:
            self._backend = self._factory(threads)
            self._threads = threads
            self._batch_bound = self._backend.max_batch_size
            process_id = getattr(self._backend, "process_id", None)
            if process_id is not None and self._residence_grant is not None:
                self._residence_grant.register_process(process_id)
        elif reconfigure and self._threads != threads:
            configure = getattr(self._backend, "configure_resources", None)
            if not callable(configure):
                raise RuntimeError("embedding backend cannot honor a changed native-thread grant")
            configure(threads=threads)
            self._threads = threads
        return self._backend

    def _invoke(self, operation: str, payload: Any, estimated_bytes: int,
                *, tokenization: bool = False) -> Any:
        self._cancellation.checkpoint()
        if (dependent_memory_bytes() + self._resident_bytes + self._invocation_result_bytes() + estimated_bytes
                > self._gate.coordinator.memory_budget_bytes):
            raise MemoryBudgetExceeded("semantic model and one work batch exceed the memory budget")
        context = copy_context()
        lease = self._gate.native_budget(
            estimated_bytes, max_threads=1 if tokenization else self._max_threads,
            phase=operation, cancellation=self._cancellation,
        )
        entered = False
        staging = _STAGING_WORK.get()
        if staging is not None:
            # Exact tokenization happens before BEGIN IMMEDIATE. Keep the
            # prepared-buffer RAM while the isolated tokenizer borrows CPU.
            staging[0].release_cpu()
        try:
            self._reserve_model(estimated_bytes)
            self._active = True
            grant = context.run(lease.__enter__)
            entered = True
            def execute():
                backend = self._ensure_backend(
                    max(1, grant.native_threads), reconfigure=not tokenization,
                )
                method = getattr(backend, operation)
                return method() if payload is None else method(payload)
            result = context.run(execute)
            self._cancellation.checkpoint()
            if operation == "embed":
                # Native activations are gone; retain Python vectors and their
                # metadata while the owner persists this ordered result batch.
                retained = sum(len(value.vector) * 32 + 4096 for value in result)
                grant.release_cpu()
                grant.shrink_transient_bytes(min(estimated_bytes, retained))
                self._results.append((lease, context))
                self._result_bytes += min(estimated_bytes, retained)
                entered = False
            return result
        finally:
            failed = sys.exc_info()[0] is not None
            if entered:
                context.run(lease.__exit__, *sys.exc_info())
            self._active = False
            if staging is not None and not failed:
                staging[0].checkpoint()

    def embed(self, requests: Sequence[EmbeddingRequest]) -> Sequence[BackendEmbedding]:
        self._release_results()
        results: list[BackendEmbedding] = []
        offset = 0
        while offset < len(requests):
            size = min(self.max_batch_size, len(requests) - offset)
            batch = tuple(requests[offset:offset + size])
            payload_bytes = sum(len(request.text or "") * 4 for request in batch)
            results.extend(self._invoke("embed", batch, size * self._request_bytes + payload_bytes))
            offset += size
        return tuple(results)

    def _release_results(self) -> None:
        retained, self._results = self._results, []
        self._result_bytes = 0
        for lease, context in reversed(retained):
            context.run(lease.__exit__, None, None, None)

    def text_token_counts(self, texts: Sequence[str]) -> tuple[tuple[int, ...], int]:
        if not texts:
            raise ValueError("token counts require at least one text")
        return self._invoke(
            "text_token_counts", tuple(texts),
            max(MIB, sum(len(text) * 8 for text in texts)), tokenization=True,
        )

    def text_tokenizer_contract(self) -> tuple[str, int]:
        return self._invoke("text_tokenizer_contract", None, MIB, tokenization=True)

    def unload(self) -> None:
        if self._active:
            raise RuntimeError("cannot unload an active embedding backend")
        self._release_results()
        self._unload_model()

    def _unload_model(self) -> None:
        if self._active:
            raise RuntimeError("cannot unload an active embedding backend")
        backend, self._backend = self._backend, None
        try:
            if backend is not None:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
        finally:
            self._threads = None
            if self._residence is not None:
                residence, context = self._residence, self._residence_context
                self._residence = self._residence_context = self._residence_grant = None
                context.run(residence.__exit__, None, None, None)

    def close(self) -> None:
        self.unload()


def semantic_backend_gate() -> CoordinatedMemoryGate | None:
    return resource_gate("semantic")
