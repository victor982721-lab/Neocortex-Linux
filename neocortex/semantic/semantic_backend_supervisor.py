"""Terminable FastEmbed worker owned by the SQLite generation coordinator."""

from __future__ import annotations
import json
import multiprocessing
import queue
from pathlib import Path
from typing import Any, Sequence

from neocortex.runtime.control.isolated_process import (
    close_isolated_process,
    isolated_spawn_process,
    terminate_isolated_process,
)
from .semantic_backends import (
    EmbeddingBackend,
    SourceRevisionMismatchError,
    TextTokenLimitExceededError,
)
from .semantic_models import (
    BackendEmbedding,
    EmbeddingModelSpec,
    EmbeddingRequest,
)
from .semantic_preparation import SemanticModelUnavailableError
from .semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
)


def _remote_failure(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, SourceRevisionMismatchError):
        kind = "source_revision_mismatch"
    elif isinstance(exc, TextTokenLimitExceededError):
        kind = "text_token_limit"
    elif isinstance(exc, SemanticModelUnavailableError):
        if exc.detail is not None:
            return "model_unavailable_detail", json.dumps({
                "reason": exc.reason, "detail": exc.detail[:4000],
            }, ensure_ascii=True)
        kind = "model_unavailable"
    elif isinstance(exc, OSError):
        kind = "os_error"
    elif isinstance(exc, ValueError):
        kind = "value_error"
    else:
        kind = "runtime_error"
    return kind, str(exc).encode("utf-8", "replace").decode("utf-8")[:8_000]


def _raise_remote_failure(kind: str, message: str) -> None:
    if kind == "source_revision_mismatch":
        raise SourceRevisionMismatchError(message)
    if kind == "text_token_limit":
        raise TextTokenLimitExceededError(message)
    if kind == "model_unavailable":
        raise SemanticModelUnavailableError(message)
    if kind == "model_unavailable_detail":
        payload = json.loads(message)
        if not isinstance(payload, dict) or not all(
            isinstance(payload.get(name), str) for name in ("reason", "detail")
        ):
            raise RuntimeError("invalid semantic model prerequisite response")
        raise SemanticModelUnavailableError(payload["reason"], payload["detail"])
    if kind == "os_error":
        raise OSError(message)
    if kind == "value_error":
        raise ValueError(message)
    raise RuntimeError(message)


def _semantic_backend_worker(
    task_channel,
    result_channel,
    model: EmbeddingModelSpec,
    cache_dir: str,
    local_files_only: bool,
    threads: int | None,
) -> None:
    """Own the non-picklable ONNX runtime and never open Semantic SQLite."""

    from .semantic_preparation import backend as build_backend

    try:
        embedding_backend = build_backend(
            model,
            cache_dir=Path(cache_dir),
            local_files_only=local_files_only,
            threads=threads,
        )
    except BaseException as exc:
        result_channel.put(("init_error", *_remote_failure(exc)))
        return
    result_channel.put(("ready", embedding_backend.max_batch_size))
    while True:
        task = task_channel.get()
        if task is None:
            return
        operation, request_id, payload = task
        try:
            if operation == "embed":
                output = tuple(embedding_backend.embed(payload))
            elif operation == "text_token_counts":
                token_counter = getattr(embedding_backend, "text_token_counts", None)
                if not callable(token_counter):
                    raise RuntimeError(
                        "semantic text backend has no exact tokenizer contract"
                    )
                output = token_counter(payload)
            elif operation == "text_tokenizer_contract":
                contract_provider = getattr(
                    embedding_backend,
                    "text_tokenizer_contract",
                    None,
                )
                if not callable(contract_provider):
                    raise RuntimeError(
                        "semantic text backend has no signed tokenizer contract"
                    )
                output = contract_provider()
            else:
                raise RuntimeError("semantic backend worker operation is invalid")
        except BaseException as exc:
            result_channel.put(("error", request_id, *_remote_failure(exc)))
        else:
            result_channel.put(("ok", request_id, output))


class DeadlineEmbeddingBackend(EmbeddingBackend):
    """Persistent backend proxy that can terminate a blocked inference call."""

    def __init__(
        self,
        model: EmbeddingModelSpec,
        *,
        cache_dir: Path,
        local_files_only: bool,
        threads: int | None,
        work_budget: SemanticWorkBudget,
        worker_target=_semantic_backend_worker,
    ) -> None:
        if work_budget.deadline is None and work_budget.cancellation_check is None:
            raise ValueError("deadline backend requires a finite invocation deadline or cancellation callback")
        self._model = model
        self._work_budget = work_budget
        self._context = multiprocessing.get_context("spawn")
        self._task_channel: Any | None = self._context.Queue(maxsize=1)
        self._result_channel: Any | None = self._context.Queue(maxsize=1)
        self._process: Any | None = isolated_spawn_process(
            target=worker_target,
            args=(
                self._task_channel,
                self._result_channel,
                model,
                str(cache_dir),
                local_files_only,
                threads,
            ),
            daemon=True,
        )
        self._request_id = 0
        self._max_batch_size = 0
        try:
            self._process.start()
            message = self._receive()
            if len(message) == 2 and message[0] == "ready":
                self._max_batch_size = int(message[1])
                if self._max_batch_size < 1:
                    raise RuntimeError(
                        "semantic backend reported an invalid batch size"
                    )
            elif len(message) == 3 and message[0] == "init_error":
                _raise_remote_failure(str(message[1]), str(message[2]))
            else:
                raise RuntimeError("semantic backend worker startup protocol mismatch")
        except BaseException:
            self._discard(terminate=True)
            raise

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._model

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    def _discard(self, *, terminate: bool) -> None:
        process = self._process
        channels = (self._task_channel, self._result_channel)
        self._process = self._task_channel = self._result_channel = None
        if process is not None:
            try:
                if terminate:
                    terminate_isolated_process(process)
                else:
                    process.join(timeout=1.0)
                    if process.is_alive():
                        terminate_isolated_process(process)
            finally:
                close_isolated_process(process)
        for channel in channels:
            if channel is not None:
                try:
                    channel.cancel_join_thread()
                finally:
                    channel.close()

    def _receive(self):
        assert self._result_channel is not None
        assert self._process is not None
        while True:
            remaining = self._work_budget.remaining_seconds()
            try:
                return self._result_channel.get(timeout=0.1 if remaining is None else min(0.1, remaining))
            except queue.Empty:
                if not self._process.is_alive():
                    raise RuntimeError(
                        "semantic backend worker exited with code "
                        f"{self._process.exitcode}"
                    ) from None

    def _submit(self, operation: str, payload: object) -> object:
        if self._process is None or not self._process.is_alive():
            raise RuntimeError("semantic backend worker is unavailable")
        self._request_id += 1
        request_id = self._request_id
        assert self._task_channel is not None
        try:
            remaining = self._work_budget.remaining_seconds()
            self._task_channel.put(
                (operation, request_id, payload),
                timeout=1.0 if remaining is None else min(1.0, remaining),
            )
            message = self._receive()
        except SemanticIndexDeadlineExceeded:
            self._discard(terminate=True)
            raise
        except queue.Full as exc:
            try:
                self._work_budget.remaining_seconds()
            except BaseException:
                self._discard(terminate=True)
                raise
            self._discard(terminate=True)
            raise RuntimeError("semantic backend task queue did not drain") from exc
        except BaseException:
            self._discard(terminate=True)
            raise
        if len(message) == 3 and message[0] == "ok" and message[1] == request_id:
            return message[2]
        if len(message) == 4 and message[0] == "error" and message[1] == request_id:
            _raise_remote_failure(str(message[2]), str(message[3]))
        self._discard(terminate=True)
        raise RuntimeError("semantic backend worker response protocol mismatch")

    def embed(
        self,
        requests: Sequence[EmbeddingRequest],
    ) -> Sequence[BackendEmbedding]:
        if not requests:
            return ()
        if len(requests) > self.max_batch_size:
            raise ValueError("request batch exceeds the isolated backend bound")
        output = self._submit("embed", tuple(requests))
        if not isinstance(output, tuple):
            self._discard(terminate=True)
            raise RuntimeError("semantic backend returned an invalid embedding payload")
        return output

    def text_token_counts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[int, ...], int]:
        """Count exact tokens in the terminable model-owning child process."""

        if not texts:
            raise ValueError("token counts require at least one text")
        if any(not isinstance(value, str) for value in texts):
            raise ValueError("token count payloads must be strings")
        output = self._submit("text_token_counts", tuple(texts))
        if (
            not isinstance(output, tuple)
            or len(output) != 2
            or not isinstance(output[0], tuple)
            or isinstance(output[1], bool)
            or not isinstance(output[1], int)
        ):
            self._discard(terminate=True)
            raise RuntimeError("semantic backend returned an invalid tokenizer payload")
        counts = output[0]
        if len(counts) != len(texts) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            self._discard(terminate=True)
            raise RuntimeError("semantic backend returned invalid token counts")
        if output[1] < 1:
            self._discard(terminate=True)
            raise RuntimeError("semantic backend returned an invalid token limit")
        return counts, output[1]

    def text_tokenizer_contract(self) -> tuple[str, int]:
        """Return the immutable model-snapshot/token-limit identity."""

        output = self._submit("text_tokenizer_contract", None)
        if (
            not isinstance(output, tuple)
            or len(output) != 2
            or not isinstance(output[0], str)
            or not output[0].strip()
            or isinstance(output[1], bool)
            or not isinstance(output[1], int)
            or output[1] < 1
        ):
            self._discard(terminate=True)
            raise RuntimeError(
                "semantic backend returned an invalid tokenizer contract"
            )
        return output[0], output[1]

    def close(self) -> None:
        if self._process is None:
            return
        if self._process.is_alive() and self._task_channel is not None:
            try:
                self._task_channel.put(None, timeout=0.1)
            except queue.Full:
                self._discard(terminate=True)
                return
        self._discard(terminate=False)
