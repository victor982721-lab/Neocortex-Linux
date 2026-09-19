"""Persistent, memory-capped Pillow workers with cooperative supervision."""

from __future__ import annotations
import multiprocessing
import os
import queue
import time
from pathlib import Path
from typing import Any

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import current_resource_grant
from ..media_resources import current_media_resource, register_media_process
from .decode import open_image_no_follow, pillow_decode_scope
from .errors import (
    ImageFailure,
    classify_image_failure,
    refine_image_failure,
    worker_supervision_failure,
)
from .analysis import (
    Decision,
    Features,
    classify,
    estimated_image_memory_bytes,
)
from .document import (
    DOCUMENT_OCR_MEMORY_BYTES,
    DocumentVerifierRuntime,
)
from neocortex.runtime.control.isolated_process import (
    close_isolated_process,
    isolated_spawn_process,
    set_isolated_process_memory_limit,
    terminate_isolated_process,
)


# region [01] Worker protocol

MIB = 1024 * 1024
MIN_IMAGE_WORKER_BYTES = 192 * MIB


class ImageWorkerError(RuntimeError):
    """The isolated image worker failed or exited without a valid result."""

    def __init__(
        self,
        message: str,
        *,
        failure: ImageFailure | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure or worker_supervision_failure(message)


class ImageWorkerTimeout(TimeoutError):
    """An isolated image analysis exceeded its per-file deadline."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.failure = ImageFailure(
            "ImageWorkerTimeout",
            message[:2000],
            "worker_timeout",
            True,
            "retry",
        )


class RemoteImageWorkerError(ImageWorkerError):
    """A structured failure returned by the isolated analyzer."""


def image_worker_memory_reservation(
    path: Path,
    features: Features | None = None,
    *,
    document_ocr: bool = False,
) -> int:
    """Probe only container metadata and reserve decode plus interpreter space."""

    if features is None:
        file_size = path.lstat().st_size
        with pillow_decode_scope(allow_truncated=False):
            with open_image_no_follow(path) as source:
                width, height = source.size
    else:
        file_size = features.file_size
        width, height = features.width, features.height
    if width <= 0 or height <= 0:
        raise ValueError("invalid image dimensions")
    reservation = max(
        MIN_IMAGE_WORKER_BYTES,
        estimated_image_memory_bytes(width, height, file_size),
    )
    if document_ocr:
        reservation += DOCUMENT_OCR_MEMORY_BYTES
    return reservation


def _image_worker(task_channel, result_channel) -> None:
    """Classify one task at a time; the parent owns admission and deadlines."""

    while True:
        task = task_channel.get()
        if task is None:
            return
        request_id, path, root, features, document_verifier, *execution = task
        if execution:
            os.environ.update(execution[0])
        try:
            decision = classify(
                Path(path),
                Path(root),
                memory_gate=None,
                features=features,
                document_verifier=document_verifier,
            )
        except BaseException as exc:
            failure = refine_image_failure(
                Path(path),
                classify_image_failure(exc),
            )
            result_channel.put(
                (
                    "error",
                    request_id,
                    failure.error_type,
                    failure.message,
                    failure.phase,
                    failure.retryable,
                    failure.disposition,
                    failure.provenance,
                )
            )
        else:
            result_channel.put(("ok", request_id, decision))


# endregion [01]


# region [02] Parent supervision


class ImageWorkerSupervisor:
    """Reuse one isolated decoder while enforcing a limit for every image."""

    def __init__(self, worker_target=_image_worker) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._worker_target = worker_target
        self._task_channel: Any | None = None
        self._result_channel: Any | None = None
        self._process: Any | None = None
        self._request_id = 0

    def _discard_worker(self, *, terminate: bool) -> None:
        process = self._process
        task_channel = self._task_channel
        result_channel = self._result_channel
        self._process = None
        self._task_channel = None
        self._result_channel = None
        if process is not None:
            try:
                if terminate:
                    terminate_isolated_process(process)
                else:
                    process.join(timeout=2)
                    if process.is_alive():
                        terminate_isolated_process(process)
            finally:
                close_isolated_process(process)
        for channel in (task_channel, result_channel):
            if channel is None:
                continue
            try:
                channel.cancel_join_thread()
            finally:
                channel.close()

    def _start_worker(self, memory_limit_bytes: int) -> None:
        self._task_channel = self._context.Queue(maxsize=1)
        self._result_channel = self._context.Queue(maxsize=1)
        self._process = isolated_spawn_process(
            target=self._worker_target,
            args=(self._task_channel, self._result_channel),
            memory_limit_bytes=memory_limit_bytes,
        )
        try:
            self._process.start()
        except BaseException:
            self._discard_worker(terminate=True)
            raise

    def classify(
        self,
        path: Path,
        root: Path,
        *,
        memory_limit_bytes: int,
        timeout_seconds: float,
        cancellation: CancellationToken,
        features: Features | None = None,
        document_verifier: DocumentVerifierRuntime | None = None,
    ) -> Decision:
        if memory_limit_bytes < 1:
            raise ValueError("image worker memory limit must be positive")
        if memory_limit_bytes < MIN_IMAGE_WORKER_BYTES:
            raise ImageWorkerError(
                "image worker memory limit is below the minimum safe "
                f"reservation of {MIN_IMAGE_WORKER_BYTES} bytes"
            )
        if timeout_seconds <= 0:
            raise ValueError("image worker timeout must be positive")
        cancellation.checkpoint()
        if self._process is None or not self._process.is_alive():
            if self._process is not None:
                self._discard_worker(terminate=False)
            self._start_worker(memory_limit_bytes)
        else:
            set_isolated_process_memory_limit(
                self._process,
                memory_limit_bytes,
            )

        self._request_id += 1
        request_id = self._request_id
        assert self._task_channel is not None
        assert self._result_channel is not None
        assert self._process is not None
        grant = current_resource_grant()
        if current_media_resource() is not None:
            register_media_process(self._process.pid)
        elif grant is not None:
            grant.register_process(self._process.pid)
        try:
            self._task_channel.put(
                (
                    request_id,
                    str(path),
                    str(root),
                    features,
                    document_verifier,
                    {} if grant is None else grant.native_env,
                ),
                block=True,
                timeout=1,
            )
        except queue.Full as exc:
            self._discard_worker(terminate=True)
            raise ImageWorkerError("isolated image task queue did not drain") from exc

        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                cancellation.checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ImageWorkerTimeout(
                        f"image analysis exceeded {timeout_seconds:g} seconds: {path}"
                    )
                try:
                    message = self._result_channel.get(
                        block=True,
                        timeout=min(0.1, remaining),
                    )
                except queue.Empty:
                    if not self._process.is_alive():
                        raise ImageWorkerError(
                            "isolated image worker exited with code "
                            f"{self._process.exitcode}: {path}"
                        ) from None
                    continue
                if len(message) < 2 or message[1] != request_id:
                    raise ImageWorkerError("isolated image worker protocol mismatch")
                if message[0] == "ok" and len(message) == 3:
                    return message[2]
                if message[0] == "error" and len(message) == 8:
                    failure = ImageFailure(
                        error_type=str(message[2]),
                        message=str(message[3]),
                        phase=str(message[4]),
                        retryable=bool(message[5]),
                        disposition=str(message[6]),  # type: ignore[arg-type]
                        provenance=str(message[7]),
                    )
                    raise RemoteImageWorkerError(
                        f"{failure.error_type}: {failure.message}",
                        failure=failure,
                    )
                raise ImageWorkerError("isolated image worker returned invalid data")
        except (CancellationRequested, ImageWorkerError, ImageWorkerTimeout):
            self._discard_worker(terminate=True)
            raise

    def close(self) -> None:
        if self._process is None:
            return
        if self._process.is_alive() and self._task_channel is not None:
            try:
                self._task_channel.put(None, block=True, timeout=0.5)
            except queue.Full:
                self._discard_worker(terminate=True)
                return
        self._discard_worker(terminate=False)


# endregion [02]
