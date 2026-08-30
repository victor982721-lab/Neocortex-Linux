"""Hard-bounded subprocess capture with timeout and deterministic cleanup."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/bounded_subprocess.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from .isolated_process import WindowsKillOnCloseJob
# endregion [01]

# region [02] Implementación


_READ_CHUNK_BYTES = 64 * 1024
_READER_JOIN_SECONDS = 5.0
_PROCESS_REAP_SECONDS = 5.0
_POSIX_TERMINATION_GRACE_SECONDS = 0.5
_PRLIMIT_PATH = "/usr/bin/prlimit"


@dataclass(frozen=True, slots=True)
class SubprocessOutputLimitError(RuntimeError):
    """Raised after terminating a child whose captured stream exceeded its limit."""

    stream: str
    limit_bytes: int

    def __str__(self) -> str:
        return f"subprocess {self.stream} exceeded {self.limit_bytes} bytes"


@dataclass(slots=True)
class _CaptureBuffers:
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    overflow: list[tuple[str, int]] = field(default_factory=list)
    overflow_lock: threading.Lock = field(default_factory=threading.Lock)
    reader_errors: list[BaseException] = field(default_factory=list)


@dataclass(slots=True)
class _TerminationController:
    process: subprocess.Popen[bytes]
    job: WindowsKillOnCloseJob | None
    termination_errors: list[BaseException] = field(default_factory=list)
    termination_lock: threading.Lock = field(default_factory=threading.Lock)
    terminated: bool = False

    def _terminate_posix_group(self) -> None:
        process_group_id = self.process.pid
        if process_group_id is None or process_group_id <= 1:
            raise RuntimeError("subprocess did not expose a safe POSIX process group")
        if process_group_id == os.getpgrp():
            raise RuntimeError("refusing to signal the supervisor process group")
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + _POSIX_TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return

    def terminate(self) -> None:
        with self.termination_lock:
            if self.terminated:
                return
            self.terminated = True
            if self.job is not None:
                try:
                    self.job.terminate()
                    return
                except OSError as error:
                    self.termination_errors.append(error)
            try:
                if os.name != "nt":
                    self._terminate_posix_group()
                elif self.process.poll() is None:
                    self.process.kill()
            except (OSError, RuntimeError) as error:
                self.termination_errors.append(error)


@dataclass(frozen=True, slots=True)
class _CaptureWait:
    started_readers: tuple[threading.Thread, ...]
    timed_out: bool
    primary_error: BaseException | None
    returncode: int | None


def _drain_bounded_stream(
    stream: IO[bytes],
    output: bytearray,
    *,
    limit_bytes: int,
    stream_name: str,
    terminate_process_tree: Callable[[], None],
    overflow: list[tuple[str, int]],
    overflow_lock: threading.Lock,
    reader_errors: list[BaseException],
) -> None:
    try:
        while chunk := stream.read(_READ_CHUNK_BYTES):
            retained = max(0, (limit_bytes + 1) - len(output))
            if retained:
                output.extend(chunk[:retained])
            if len(output) <= limit_bytes:
                continue
            with overflow_lock:
                if not overflow:
                    overflow.append((stream_name, limit_bytes))
                    terminate_process_tree()
            return
    except (OSError, ValueError) as exc:
        reader_errors.append(exc)


def _join_readers(
    readers: tuple[threading.Thread, ...],
    streams: tuple[IO[bytes], IO[bytes]],
) -> None:
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)
    if all(not reader.is_alive() for reader in readers):
        return
    for stream in streams:
        stream.close()
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)
    if any(reader.is_alive() for reader in readers):
        raise RuntimeError("subprocess output reader did not terminate")


def _start_bounded_process(
    command: tuple[str, ...],
    *,
    stdin: int | IO[bytes],
    creationflags: int,
    cwd: str | None,
    environment: Mapping[str, str] | None,
    memory_limit_bytes: int | None,
) -> tuple[subprocess.Popen[bytes], WindowsKillOnCloseJob | None]:
    job: WindowsKillOnCloseJob | None = None
    effective_creationflags = creationflags
    if os.name == "nt":
        job = WindowsKillOnCloseJob(memory_limit_bytes)
        effective_creationflags |= job.suspended_creation_flag()
    effective_command = command
    if os.name != "nt" and memory_limit_bytes is not None:
        prlimit = Path(_PRLIMIT_PATH)
        if not prlimit.is_file() or not os.access(prlimit, os.X_OK):
            raise RuntimeError("posix_memory_containment_unavailable: /usr/bin/prlimit is required")
        effective_command = (
            _PRLIMIT_PATH,
            f"--as={memory_limit_bytes}",
            "--",
            *command,
        )
    try:
        process = subprocess.Popen(
            effective_command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=effective_creationflags,
            cwd=cwd,
            env=environment,
            start_new_session=os.name != "nt",
        )
    except BaseException:
        if job is not None:
            job.close()
        raise
    if job is None:
        return process, None
    try:
        job.assign_suspended(process)
    except BaseException:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_PROCESS_REAP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        job.close()
        raise
    return process, job


def _cleanup_note(error: BaseException) -> str:
    return f"subprocess cleanup: {type(error).__name__}: {error}"


def _validate_capture_bounds(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    memory_limit_bytes: int | None,
) -> None:
    if not arguments:
        raise ValueError("subprocess arguments cannot be empty")
    if timeout_seconds <= 0:
        raise ValueError("subprocess timeout must be positive")
    if stdout_limit_bytes < 0 or stderr_limit_bytes < 0:
        raise ValueError("subprocess output limits cannot be negative")
    if memory_limit_bytes is not None and memory_limit_bytes < 1:
        raise ValueError("subprocess memory limit must be positive")


def _prepare_stdin(resources: ExitStack, input_bytes: bytes | None) -> int | IO[bytes]:
    stdin: int | IO[bytes] = subprocess.DEVNULL
    if input_bytes is None:
        return stdin
    input_stream = resources.enter_context(tempfile.TemporaryFile())
    input_stream.write(input_bytes)
    input_stream.seek(0)
    return input_stream


def _capture_readers(
    process: subprocess.Popen[bytes],
    buffers: _CaptureBuffers,
    controller: _TerminationController,
    *,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> tuple[threading.Thread, ...]:
    assert process.stdout is not None
    assert process.stderr is not None
    return (
        threading.Thread(
            target=_drain_bounded_stream,
            kwargs={
                "stream": process.stdout,
                "output": buffers.stdout,
                "limit_bytes": stdout_limit_bytes,
                "stream_name": "stdout",
                "terminate_process_tree": controller.terminate,
                "overflow": buffers.overflow,
                "overflow_lock": buffers.overflow_lock,
                "reader_errors": buffers.reader_errors,
            },
            name="neocortex-subprocess-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded_stream,
            kwargs={
                "stream": process.stderr,
                "output": buffers.stderr,
                "limit_bytes": stderr_limit_bytes,
                "stream_name": "stderr",
                "terminate_process_tree": controller.terminate,
                "overflow": buffers.overflow,
                "overflow_lock": buffers.overflow_lock,
                "reader_errors": buffers.reader_errors,
            },
            name="neocortex-subprocess-stderr",
            daemon=True,
        ),
    )


def _wait_for_capture(
    process: subprocess.Popen[bytes],
    readers: tuple[threading.Thread, ...],
    buffers: _CaptureBuffers,
    controller: _TerminationController,
    *,
    timeout_seconds: float,
) -> _CaptureWait:
    started_readers: list[threading.Thread] = []
    timed_out = False
    primary_error: BaseException | None = None
    returncode: int | None = None
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            controller.terminate()
    except BaseException as error:
        primary_error = error
        controller.terminate()
    if buffers.overflow:
        controller.terminate()
    return _CaptureWait(tuple(started_readers), timed_out, primary_error, returncode)


def _finalize_capture(
    process: subprocess.Popen[bytes],
    job: WindowsKillOnCloseJob | None,
    started_readers: tuple[threading.Thread, ...],
    initial_returncode: int | None,
) -> tuple[int | None, list[BaseException]]:
    cleanup_errors: list[BaseException] = []
    if job is not None:
        try:
            job.close()
        except OSError as error:
            cleanup_errors.append(error)
    returncode = initial_returncode
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        if process.poll() is None:
            returncode = process.wait(timeout=_PROCESS_REAP_SECONDS)
        else:
            returncode = process.returncode
    except BaseException as error:
        cleanup_errors.append(error)
        try:
            process.kill()
        except OSError as kill_error:
            cleanup_errors.append(kill_error)
        try:
            returncode = process.wait(timeout=_PROCESS_REAP_SECONDS)
        except BaseException as reap_error:
            cleanup_errors.append(reap_error)
    try:
        _join_readers(started_readers, (process.stdout, process.stderr))
    except BaseException as error:
        cleanup_errors.append(error)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except (OSError, ValueError) as error:
            cleanup_errors.append(error)
    return returncode, cleanup_errors


def _resolve_capture_result(
    command: tuple[str, ...],
    *,
    timeout_seconds: float,
    buffers: _CaptureBuffers,
    controller: _TerminationController,
    wait: _CaptureWait,
    returncode: int | None,
    cleanup_errors: list[BaseException],
) -> subprocess.CompletedProcess[bytes]:
    captured_stdout = bytes(buffers.stdout)
    captured_stderr = bytes(buffers.stderr)
    diagnostic_errors = controller.termination_errors + cleanup_errors
    if wait.primary_error is not None:
        for diagnostic_error in diagnostic_errors:
            wait.primary_error.add_note(_cleanup_note(diagnostic_error))
        raise wait.primary_error
    if wait.timed_out:
        timeout_error = subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=captured_stdout,
            stderr=captured_stderr,
        )
        for cleanup_error in diagnostic_errors:
            timeout_error.add_note(_cleanup_note(cleanup_error))
        raise timeout_error
    if buffers.overflow:
        stream_name, limit_bytes = buffers.overflow[0]
        overflow_error = SubprocessOutputLimitError(stream_name, limit_bytes)
        for cleanup_error in diagnostic_errors:
            overflow_error.add_note(_cleanup_note(cleanup_error))
        raise overflow_error
    if buffers.reader_errors:
        raise RuntimeError("subprocess output capture failed") from buffers.reader_errors[0]
    if diagnostic_errors:
        raise RuntimeError("subprocess cleanup failed") from diagnostic_errors[0]
    if returncode is None:
        raise RuntimeError("subprocess did not expose a terminal return code")
    return subprocess.CompletedProcess(
        command,
        returncode,
        captured_stdout,
        captured_stderr,
    )


def _execute_bounded_capture(
    command: tuple[str, ...],
    *,
    stdin: int | IO[bytes],
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    creationflags: int,
    cwd: str | None,
    environment: Mapping[str, str] | None,
    memory_limit_bytes: int | None,
) -> subprocess.CompletedProcess[bytes]:
    process, job = _start_bounded_process(
        command,
        stdin=stdin,
        creationflags=creationflags,
        cwd=cwd,
        environment=environment,
        memory_limit_bytes=memory_limit_bytes,
    )
    buffers = _CaptureBuffers()
    controller = _TerminationController(process, job)
    readers = _capture_readers(
        process,
        buffers,
        controller,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
    )
    wait = _wait_for_capture(
        process,
        readers,
        buffers,
        controller,
        timeout_seconds=timeout_seconds,
    )
    if os.name != "nt":
        # A command can exit after spawning descendants that inherited the
        # capture pipes. Always close the dedicated process group before
        # joining readers, including on an otherwise successful return.
        controller.terminate()
    returncode, cleanup_errors = _finalize_capture(
        process,
        job,
        wait.started_readers,
        wait.returncode,
    )
    return _resolve_capture_result(
        command,
        timeout_seconds=timeout_seconds,
        buffers=buffers,
        controller=controller,
        wait=wait,
        returncode=returncode,
        cleanup_errors=cleanup_errors,
    )


def run_bounded_capture(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    creationflags: int = 0,
    cwd: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
    memory_limit_bytes: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a child with bounded capture and exact Windows descendant cleanup.

    Output pipes are drained concurrently. On Windows the child is created
    suspended, assigned by exact process handle to a kill-on-close Job Object,
    and only then resumed. Timeout, overflow, caller exceptions, and normal
    return all reap the direct child and release the Job so descendants cannot
    survive the call.
    """

    _validate_capture_bounds(
        arguments,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        memory_limit_bytes=memory_limit_bytes,
    )
    command = tuple(os.fspath(argument) for argument in arguments)
    working_directory = None if cwd is None else os.fspath(cwd)
    with ExitStack() as resources:
        stdin = _prepare_stdin(resources, input_bytes)
        return _execute_bounded_capture(
            command,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            creationflags=creationflags,
            cwd=working_directory,
            environment=environment,
            memory_limit_bytes=memory_limit_bytes,
        )


__all__ = ["SubprocessOutputLimitError", "run_bounded_capture"]
_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.bounded_subprocess")
# endregion [02]
