"""Lazy faster-whisper runtime detection and isolated persistent transcription."""

from __future__ import annotations

import importlib.metadata
import multiprocessing
import os
import queue
import shutil
import time
from pathlib import Path
from typing import Any, Mapping
from neocortex.runtime.control.global_resources import current_resource_grant
from neocortex.runtime.control.gpu_runtime import cuda_memory_snapshot, register_gpu_process
from ..media_resources import checkpoint_before_deadline, current_media_resource, register_media_process

from neocortex.platform.policy import default_whisper_model_cache
from .models import (
    AudioProcessingError,
    AudioRuntimeUnavailableError,
    AudioRouteConfig,
    TranscriptResult,
    TranscriptSegment,
    WhisperRuntime,
    WhisperRuntimeError,
)
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.isolated_process import (
    close_isolated_process,
    isolated_spawn_process,
    terminate_isolated_process,
)


_RUNTIME_UNAVAILABLE_PREFIX = "[audio-runtime-unavailable] "


# region [01] Runtime discovery without model loading


def resolve_whisper_runtime(
    device: str = "auto",
    compute_type: str = "auto",
) -> WhisperRuntime:
    """Resolve the installed backend and effective device without loading weights."""

    backend_version, ctranslate2_version, cuda_devices = _whisper_environment()
    resolved_device = _resolve_whisper_device(device, cuda_devices)
    resolved_compute = _resolve_whisper_compute_type(compute_type, resolved_device)
    return WhisperRuntime(
        backend_version=backend_version,
        ctranslate2_version=ctranslate2_version,
        cuda_devices=cuda_devices,
        resolved_device=resolved_device,
        resolved_compute_type=resolved_compute,
    )


def _whisper_environment() -> tuple[str, str, int]:
    try:
        backend_version = importlib.metadata.version("faster-whisper")
        ctranslate2_version = importlib.metadata.version("ctranslate2")
        import ctranslate2  # type: ignore[import-untyped]
    except (ImportError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        raise AudioRuntimeUnavailableError(
            "the audio route requires an importable faster-whisper/CTranslate2 runtime; "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        cuda_devices = int(ctranslate2.get_cuda_device_count())
    except (RuntimeError, TypeError, ValueError):
        cuda_devices = 0
    return backend_version, ctranslate2_version, cuda_devices


def _resolve_whisper_device(device: str, cuda_devices: int) -> str:
    if device not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"unsupported Whisper device: {device}")
    resolved_device = "cuda" if device == "auto" and cuda_devices > 0 else device
    if resolved_device == "auto":
        resolved_device = "cpu"
    if resolved_device == "cuda" and cuda_devices < 1:
        raise AudioRuntimeUnavailableError(
            "Whisper device 'cuda' was requested but CTranslate2 found no CUDA device"
        )
    if resolved_device == "cuda" and cuda_memory_snapshot() is None:
        if device == "auto":
            return "cpu"
        raise AudioRuntimeUnavailableError(
            "Whisper CUDA memory/identity could not be observed for resource admission"
        )
    return resolved_device


def _resolve_whisper_compute_type(compute_type: str, resolved_device: str) -> str:
    resolved_compute = compute_type.strip().casefold()
    if not resolved_compute:
        raise ValueError("Whisper compute type must be non-empty")
    if resolved_compute == "auto":
        resolved_compute = "float16" if resolved_device == "cuda" else "int8"
    return resolved_compute


def audio_runtime_doctor(
    *,
    device: str = "auto",
    compute_type: str = "auto",
    ffprobe_path: str | None = None,
) -> dict[str, object]:
    """Report dependencies and resolution without downloading a Whisper model."""

    result: dict[str, object] = {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which(ffprobe_path or "ffprobe"),
        "model_loaded": False,
        "model_downloaded": False,
    }
    try:
        runtime = resolve_whisper_runtime(device, compute_type)
    except (ValueError, WhisperRuntimeError) as exc:
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        return result
    result.update(
        ok=bool(result["ffprobe"]),
        backend="faster-whisper",
        backend_version=runtime.backend_version,
        ctranslate2_version=runtime.ctranslate2_version,
        cuda_devices=runtime.cuda_devices,
        device=runtime.resolved_device,
        compute_type=runtime.resolved_compute_type,
    )
    if not result["ffprobe"]:
        result["error"] = "FFprobe executable was not found"
    return result


# endregion [01]


# region [02] Child protocol and bounded text assembly


def local_whisper_model(
    model_name: str, cache_directory: Path | None,
) -> tuple[str, Path]:
    """Resolve complete local weights/tokenizer without calling a downloader.

    Whisper's local_files_only flag does not protect its tokenizer fallback:
    passing a local directory without tokenizer.json can still fetch a tokenizer.
    Resolve and validate the directory before entering that backend.
    """

    from neocortex.runtime.config.model_management import (
        WHISPER_MODEL_ID,
        WHISPER_REQUIRED_FILES,
        _valid_whisper_directory,
        _whisper_snapshot_directory,
    )

    candidate = Path(model_name).expanduser()
    canonical_small = model_name in {"small", WHISPER_MODEL_ID}
    if not canonical_small and candidate.is_dir():
        if _valid_whisper_directory(candidate):
            return model_name, candidate.resolve()
        missing = [
            name for name in WHISPER_REQUIRED_FILES
            if not (candidate / name).is_file() or not (candidate / name).stat().st_size
        ]
        raise AudioRuntimeUnavailableError(
            f"Whisper local model {candidate} is incomplete: {', '.join(missing)}; "
            "offline transcription will not download weights or a tokenizer"
        )
    if canonical_small:
        model_id = WHISPER_MODEL_ID
    elif "/" in model_name and not candidate.is_absolute() and len(candidate.parts) == 2:
        model_id = model_name
    elif "/" in model_name or candidate.is_absolute():
        raise WhisperRuntimeError(f"Whisper local model directory is missing: {candidate}")
    else:
        # Preserve the backend's aliases without maintaining a second registry.
        try:
            from faster_whisper.utils import _MODELS  # type: ignore[import-untyped]
        except (ImportError, OSError) as exc:
            raise AudioRuntimeUnavailableError(
                f"faster-whisper is required to resolve model alias {model_name!r}; "
                "provide a canonical repository ID or a complete local directory"
            ) from exc
        model_id = _MODELS.get(model_name)
        if not model_id:
            raise WhisperRuntimeError(f"Whisper model alias is not supported: {model_name!r}")
    cache = default_whisper_model_cache() if cache_directory is None else cache_directory
    snapshot = None if cache is None else _whisper_snapshot_directory(cache, model_id)
    if snapshot is None:
        raise AudioRuntimeUnavailableError(
            f"Whisper model {model_id} is not complete in {cache}; required local files: "
            f"{', '.join(WHISPER_REQUIRED_FILES)}; offline transcription will not download them"
        )
    return str(model_id), snapshot.resolve()


def whisper_local_provenance(
    model_name: str, cache_directory: Path | None,
) -> dict[str, Any]:
    """Bind available local model bytes to the existing processing provenance."""

    from neocortex.foundation.processing_provenance import file_artifact
    from neocortex.runtime.config.model_management import WHISPER_REQUIRED_FILES

    try:
        model_id, snapshot = local_whisper_model(model_name, cache_directory)
    except (OSError, WhisperRuntimeError) as exc:
        return {
            "name": "whisper-local-model", "kind": "model-artifact",
            "model_id": model_name, "files_available": False,
            "reason": type(exc).__name__,
        }
    names = (
        *WHISPER_REQUIRED_FILES, "preprocessor_config.json", "vocabulary.json", "vocabulary.txt",
    )
    return {
        "name": "whisper-local-model", "kind": "model-artifact",
        "model_id": model_id, "files_available": True,
        "artifacts": [
            file_artifact(snapshot / name, label=name) for name in names
            if (snapshot / name).is_file()
        ],
    }


def _normalized_segment_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _configuration_int(config: Mapping[str, object], key: str) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"invalid integer Whisper setting: {key}")
    return int(value)


def _transcribe_loaded_model(
    model: Any,
    path: str,
    config: Mapping[str, object],
    runtime: WhisperRuntime,
    checkpoint=None,
) -> TranscriptResult:
    segments_iterator, info = model.transcribe(
        path,
        language=config["language"],
        beam_size=_configuration_int(config, "beam_size"),
        vad_filter=bool(config["vad_filter"]),
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=False,
        condition_on_previous_text=True,
    )
    max_chars = _configuration_int(config, "max_transcript_chars")
    max_segments = _configuration_int(config, "max_segments")
    result_segments: list[TranscriptSegment] = []
    text_parts: list[str] = []
    text_chars = 0
    speech_seconds = 0.0
    for segment in segments_iterator:
        if checkpoint is not None:
            checkpoint()
        if len(result_segments) >= max_segments:
            raise AudioProcessingError(
                "audio_segment_limit",
                f"transcript exceeded {max_segments} segments",
                recommendation="manual_review",
                retryable=False,
            )
        text = _normalized_segment_text(getattr(segment, "text", ""))
        if not text:
            continue
        added_chars = len(text) + int(bool(text_parts))
        if text_chars + added_chars > max_chars:
            raise AudioProcessingError(
                "audio_transcript_char_limit",
                f"transcript exceeded {max_chars} characters",
                recommendation="manual_review",
                retryable=False,
            )
        start_seconds = max(0.0, float(getattr(segment, "start", 0.0)))
        end_seconds = max(start_seconds, float(getattr(segment, "end", start_seconds)))
        result_segments.append(
            TranscriptSegment(
                index=len(result_segments),
                start_ms=round(start_seconds * 1000),
                end_ms=round(end_seconds * 1000),
                text=text,
                avg_logprob=_optional_float(getattr(segment, "avg_logprob", None)),
                no_speech_probability=_optional_float(getattr(segment, "no_speech_prob", None)),
            )
        )
        text_parts.append(text)
        text_chars += added_chars
        speech_seconds += max(0.0, end_seconds - start_seconds)
    duration = _optional_float(getattr(info, "duration", None)) or 0.0
    return TranscriptResult(
        text=" ".join(text_parts),
        language=str(getattr(info, "language", "") or "") or None,
        language_probability=_optional_float(getattr(info, "language_probability", None)),
        duration_seconds=max(0.0, duration),
        speech_duration_seconds=speech_seconds,
        segments=tuple(result_segments),
        model_name=str(config["model_name"]),
        backend_version=runtime.backend_version,
        device=runtime.resolved_device,
        compute_type=runtime.resolved_compute_type,
    )


def _whisper_worker(task_channel, result_channel, settings: Mapping[str, object]) -> None:
    """Load one model, then service sequential bounded requests."""

    try:
        environment = settings.get("native_environment", {})
        if isinstance(environment, dict):
            os.environ.update(environment)
        download_root = settings.get("model_cache_directory")
        if download_root is not None and not isinstance(download_root, str):
            raise ValueError("invalid Whisper model cache directory")
        model_name = str(settings["model_name"])
        if bool(settings["local_models_only"]):
            _model_id, snapshot = local_whisper_model(
                model_name, None if download_root is None else Path(download_root),
            )
            model_name = str(snapshot)
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]
        except (ImportError, OSError) as exc:
            raise AudioRuntimeUnavailableError(
                "the audio route requires an importable faster-whisper runtime; "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        runtime = resolve_whisper_runtime(str(settings["device"]), str(settings["compute_type"]))
        model = WhisperModel(
            model_name,
            device=runtime.resolved_device,
            compute_type=runtime.resolved_compute_type,
            download_root=download_root,
            local_files_only=bool(settings["local_models_only"]),
            cpu_threads=_configuration_int(settings, "cpu_threads") if "cpu_threads" in settings else 1,
            num_workers=1,
        )
    except AudioRuntimeUnavailableError as exc:
        # Keep the established three-field startup protocol.  The private
        # prefix lets the parent restore the narrower typed exception without
        # breaking callers that inspect ``("init_error", "WhisperRuntimeError", ...)``.
        result_channel.put(
            (
                "init_error",
                "WhisperRuntimeError",
                f"{_RUNTIME_UNAVAILABLE_PREFIX}{str(exc)[:4000]}",
            )
        )
        return
    except BaseException as exc:
        result_channel.put(("init_error", type(exc).__name__, str(exc)[:4000]))
        return
    result_channel.put(("ready", runtime))
    while True:
        task = task_channel.get()
        if task is None:
            return
        request_id, path, config = task
        def checkpoint(request_id=request_id):
            result_channel.put(("checkpoint", request_id))
            response = task_channel.get()
            if response != ("resume", request_id):
                raise RuntimeError("invalid Whisper execution grant response")
        try:
            result = _transcribe_loaded_model(
                model, path, config, runtime,
                checkpoint=checkpoint if config.get("resource_checkpoints") else None,
            )
        except AudioProcessingError as exc:
            result_channel.put(
                (
                    "processing_error",
                    request_id,
                    exc.code,
                    str(exc)[:4000],
                    exc.recommendation,
                    exc.retryable,
                    exc.evidence,
                )
            )
        except BaseException as exc:
            result_channel.put(
                (
                    "processing_error",
                    request_id,
                    "audio_transcription_error",
                    f"{type(exc).__name__}: {exc}"[:4000],
                    "retry",
                    True,
                    {},
                )
            )
        else:
            result_channel.put(("ok", request_id, result))


# endregion [02]


# region [03] Persistent parent-side supervisor


class WhisperTranscriber:
    """Reuse one isolated Whisper model while enforcing deadlines and cancellation."""

    def __init__(
        self,
        config: AudioRouteConfig,
        runtime: WhisperRuntime,
        *,
        worker_target=_whisper_worker,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self._context = multiprocessing.get_context("spawn")
        self._worker_target = worker_target
        self._task_channel: Any | None = None
        self._result_channel: Any | None = None
        self._process: Any | None = None
        self._request_id = 0
        self._native_threads = 1

    def _discard_worker(self, *, terminate: bool) -> None:
        process = self._process
        channels = (self._task_channel, self._result_channel)
        self._process = self._task_channel = self._result_channel = None
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
        for channel in channels:
            if channel is not None:
                try:
                    channel.cancel_join_thread()
                finally:
                    channel.close()

    def _receive_until(
        self,
        deadline: float,
        cancellation: CancellationToken,
        *,
        timeout_message: str,
    ):
        assert self._result_channel is not None
        assert self._process is not None
        while True:
            cancellation.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(timeout_message)
            try:
                message = self._result_channel.get(timeout=min(0.25, remaining))
                if message == ("checkpoint", self._request_id):
                    grant = current_resource_grant()
                    if grant is not None:
                        checkpoint_before_deadline(
                            grant, deadline, cancellation, TimeoutError(timeout_message),
                        )
                    assert self._task_channel is not None
                    self._task_channel.put(("resume", self._request_id), timeout=1)
                    continue
                return message
            except queue.Empty:
                if not self._process.is_alive():
                    raise WhisperRuntimeError(
                        f"isolated Whisper worker exited with code {self._process.exitcode}"
                    ) from None

    def _start(self, cancellation: CancellationToken) -> None:
        grant = current_resource_grant()
        self._native_threads = 1 if grant is None else max(1, grant.native_threads)
        settings: dict[str, object] = {
            "model_name": self.config.model_name,
            "device": self.runtime.resolved_device,
            "compute_type": self.runtime.resolved_compute_type,
            "model_cache_directory": (
                str(self.config.model_cache_directory)
                if self.config.model_cache_directory is not None
                else None
            ),
            "local_models_only": self.config.local_models_only,
            "cpu_threads": self._native_threads,
            "native_environment": {} if grant is None else grant.native_env,
        }
        self._task_channel = self._context.Queue(maxsize=1)
        self._result_channel = self._context.Queue(maxsize=1)
        self._process = isolated_spawn_process(
            target=self._worker_target,
            args=(self._task_channel, self._result_channel, settings),
            memory_limit_bytes=self.config.worker_memory_bytes,
        )
        try:
            self._process.start()
            identity = None
            if current_media_resource() is not None:
                identity = register_media_process(self._process.pid)
            elif grant is not None:
                identity = grant.register_process(self._process.pid)
            if self.runtime.resolved_device == "cuda" and identity is not None:
                register_gpu_process(*identity)
            message = self._receive_until(
                time.monotonic() + self.config.worker_startup_timeout_seconds,
                cancellation,
                timeout_message="Whisper model startup exceeded its deadline",
            )
            if len(message) == 2 and message[0] == "ready":
                child_runtime = message[1]
                if child_runtime != self.runtime:
                    raise WhisperRuntimeError(
                        "Whisper worker runtime differs from the processing signature"
                    )
                return
            if len(message) == 3 and message[0] == "init_error":
                detail = str(message[2])
                if detail.startswith(_RUNTIME_UNAVAILABLE_PREFIX):
                    detail = detail.removeprefix(_RUNTIME_UNAVAILABLE_PREFIX)
                    raise AudioRuntimeUnavailableError(f"{message[1]}: {detail}")
                raise WhisperRuntimeError(f"{message[1]}: {detail}")
            if len(message) == 3 and message[0] == "runtime_unavailable":
                raise AudioRuntimeUnavailableError(f"{message[1]}: {message[2]}")
            raise WhisperRuntimeError("invalid Whisper worker startup response")
        except BaseException:
            self._discard_worker(terminate=True)
            raise

    def transcribe(
        self,
        path: Path,
        *,
        cancellation: CancellationToken,
    ) -> TranscriptResult:
        cancellation.checkpoint()
        grant = current_resource_grant()
        desired = 1 if grant is None else max(1, grant.native_threads)
        if self._process is not None and desired != self._native_threads:
            self.close()
        self._ensure_started(cancellation)
        self._request_id += 1
        request_id = self._request_id
        message = self._request_transcription(path, cancellation, request_id)
        return self._decode_transcription_response(message, request_id)

    def _ensure_started(self, cancellation: CancellationToken) -> None:
        if self._process is None or not self._process.is_alive():
            if self._process is not None:
                self._discard_worker(terminate=False)
            try:
                self._start(cancellation)
            except TimeoutError as exc:
                raise WhisperRuntimeError(str(exc)) from exc

    def _transcription_request(self) -> dict[str, object]:
        return {
            "model_name": self.config.model_name,
            "language": self.config.language,
            "beam_size": self.config.beam_size,
            "vad_filter": self.config.vad_filter,
            "max_transcript_chars": self.config.max_transcript_chars,
            "max_segments": self.config.max_segments,
            "resource_checkpoints": current_resource_grant() is not None,
        }

    def _request_transcription(
        self,
        path: Path,
        cancellation: CancellationToken,
        request_id: int,
    ) -> object:
        assert self._task_channel is not None
        try:
            self._task_channel.put(
                (request_id, str(path), self._transcription_request()), timeout=1
            )
            return self._receive_until(
                time.monotonic() + self.config.file_timeout_seconds,
                cancellation,
                timeout_message=(
                    f"transcription exceeded {self.config.file_timeout_seconds:g} seconds: {path}"
                ),
            )
        except queue.Full as exc:
            self._discard_worker(terminate=True)
            raise WhisperRuntimeError("Whisper task queue did not drain") from exc
        except TimeoutError as exc:
            self._discard_worker(terminate=True)
            raise AudioProcessingError(
                "audio_transcription_timeout",
                str(exc),
                recommendation="retry",
                retryable=True,
            ) from exc
        except (CancellationRequested, WhisperRuntimeError):
            self._discard_worker(terminate=True)
            raise

    def _decode_transcription_response(
        self,
        message: object,
        request_id: int,
    ) -> TranscriptResult:
        if not isinstance(message, tuple) or len(message) < 2 or message[1] != request_id:
            self._discard_worker(terminate=True)
            raise WhisperRuntimeError("Whisper worker protocol mismatch")
        if len(message) == 3 and message[0] == "ok":
            result = message[2]
            if not isinstance(result, TranscriptResult):
                self._discard_worker(terminate=True)
                raise WhisperRuntimeError("Whisper worker returned an invalid result")
            return result
        if len(message) == 7 and message[0] == "processing_error":
            raise AudioProcessingError(
                str(message[2]),
                str(message[3]),
                recommendation=str(message[4]),  # type: ignore[arg-type]
                retryable=bool(message[5]),
                evidence=message[6],
            )
        self._discard_worker(terminate=True)
        raise WhisperRuntimeError("Whisper worker returned an invalid response")

    def close(self) -> None:
        if self._process is None:
            return
        if self._process.is_alive() and self._task_channel is not None:
            try:
                self._task_channel.put(None, timeout=0.5)
            except queue.Full:
                self._discard_worker(terminate=True)
                return
        self._discard_worker(terminate=False)

    def __enter__(self) -> WhisperTranscriber:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


# endregion [03]


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.audio.whisper"
del _defined_value
