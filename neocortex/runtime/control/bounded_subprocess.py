"""Hard-bounded subprocess capture with deterministic POSIX cleanup.

POSIX pipes are consumed directly through non-blocking file descriptors. This is
deliberate: closing a buffered IO object from another thread is not a reliable
way to interrupt a blocked read and can make a nominal timeout unbounded when a
descendant keeps a pipe open.
"""

from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol, cast


_READ_CHUNK_BYTES = 64 * 1024
_PROCESS_POLL_INTERVAL_SECONDS = 0.05
_READER_JOIN_SECONDS = 5.0
_PROCESS_REAP_SECONDS = 5.0
_CLEANUP_TOLERANCE_SECONDS = 0.05
_POSIX_TERMINATION_GRACE_SECONDS = 0.5
_PRLIMIT_PATH = "/usr/bin/prlimit"


class _WindowsJob(Protocol):
    def assign_suspended(self, process: subprocess.Popen[bytes]) -> None: ...

    def close(self) -> None: ...

    @staticmethod
    def suspended_creation_flag() -> int: ...

    def terminate(self) -> None: ...


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
class _CaptureStream:
    fd: int
    stream_name: str
    output: bytearray
    limit_bytes: int
    eof: bool = False
    registered: bool = True


@dataclass(slots=True)
class _TerminationController:
    process: subprocess.Popen[bytes]
    job: _WindowsJob | None
    process_start_ticks: int | None = None
    termination_errors: list[BaseException] = field(default_factory=list)
    termination_lock: threading.Lock = field(default_factory=threading.Lock)
    terminated: bool = False

    def _terminate_posix_group(self, deadline: float | None) -> None:
        process_group_id = self.process.pid
        if process_group_id is None or process_group_id <= 1:
            raise RuntimeError("subprocess did not expose a safe POSIX process group")
        if process_group_id == os.getpgrp():
            raise RuntimeError("refusing to signal the supervisor process group")

        # If the leader is still visible, reject a reused PID before sending a
        # group signal. A reaped leader may no longer have a /proc entry while
        # its descendants remain in the group, so absence is not treated as
        # proof that the group disappeared; killpg itself is still scoped to
        # the original process-group ID.
        if self.process_start_ticks is not None:
            observed_ticks = _process_start_ticks(process_group_id)
            if observed_ticks is not None and observed_ticks != self.process_start_ticks:
                raise RuntimeError("refusing to signal a replaced subprocess process group")

        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return

        if deadline is None:
            grace_deadline = time.monotonic() + _POSIX_TERMINATION_GRACE_SECONDS
        else:
            grace_deadline = min(
                deadline,
                time.monotonic() + _POSIX_TERMINATION_GRACE_SECONDS,
            )
        while time.monotonic() < grace_deadline:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                return
            sleep_for = min(
                0.02,
                max(0.0, grace_deadline - time.monotonic()),
            )
            if sleep_for:
                time.sleep(sleep_for)
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return

    def terminate(self, deadline: float | None = None) -> None:
        """Terminate the owned child tree without waiting past deadline."""

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
                    self._terminate_posix_group(deadline)
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
    cleanup_incomplete: bool = False


def _process_start_ticks(process_id: int) -> int | None:
    """Return Linux /proc start ticks, or None when unavailable."""

    try:
        stat = Path(f"/proc/{process_id}/stat").read_bytes()
    except (FileNotFoundError, OSError):
        return None
    try:
        fields = stat.rpartition(b")")[2].split()
        return int(fields[19])
    except (IndexError, ValueError):
        return None


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
    """Windows fallback reader; POSIX uses os.read and selectors instead."""

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
    *,
    deadline: float,
) -> list[BaseException]:
    """Join Windows fallback readers only for the remaining total budget."""

    cleanup_errors: list[BaseException] = []
    for reader in readers:
        remaining = max(0.0, deadline - time.monotonic())
        reader.join(timeout=min(_READER_JOIN_SECONDS, remaining))
    if all(not reader.is_alive() for reader in readers):
        return cleanup_errors

    # The readers are daemon threads, so a retained Windows pipe cannot keep
    # interpreter shutdown alive. Closing is best effort and remains within
    # the same budget; unlike POSIX this is only the platform fallback because
    # Windows anonymous pipes are not selector-compatible.
    for stream in streams:
        try:
            stream.close()
        except (OSError, ValueError) as error:
            cleanup_errors.append(error)
    for reader in readers:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        reader.join(timeout=min(_READER_JOIN_SECONDS, remaining))
    if any(reader.is_alive() for reader in readers):
        cleanup_errors.append(RuntimeError("subprocess output reader did not terminate"))
    return cleanup_errors


def _start_bounded_process(
    command: tuple[str, ...],
    *,
    stdin: int | IO[bytes],
    creationflags: int,
    cwd: str | None,
    environment: Mapping[str, str] | None,
    memory_limit_bytes: int | None,
) -> tuple[subprocess.Popen[bytes], _WindowsJob | None]:
    """Start a bounded process, retaining an optional Windows job seam.

    The current supported runtime is POSIX. A Windows WindowsKillOnCloseJob
    supplied by the platform control module is still honored when present so
    this module does not silently downgrade an existing Windows installation.
    """

    job: _WindowsJob | None = None
    effective_creationflags = creationflags
    if os.name == "nt":
        try:
            from . import isolated_process
        except ImportError:  # pragma: no cover - current supported platform is POSIX
            isolated_process = None  # type: ignore[assignment]
        job_factory = None if isolated_process is None else vars(isolated_process).get(
            "WindowsKillOnCloseJob"
        )
        if callable(job_factory):
            job = cast(_WindowsJob, job_factory(memory_limit_bytes))
            effective_creationflags |= job.suspended_creation_flag()

    effective_command = command
    if os.name != "nt" and memory_limit_bytes is not None:
        prlimit = Path(_PRLIMIT_PATH)
        if not prlimit.is_file() or not os.access(_PRLIMIT_PATH, os.X_OK):
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
            try:
                job.close()
            except OSError:
                pass
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
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
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
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("subprocess timeout must be a finite positive value")
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
    deadline: float,
) -> tuple[threading.Thread, ...]:
    """Create daemon readers for Windows, where selectors cannot drain pipes."""

    assert process.stdout is not None
    assert process.stderr is not None

    def terminate() -> None:
        controller.terminate(deadline)

    return (
        threading.Thread(
            target=_drain_bounded_stream,
            kwargs={
                "stream": process.stdout,
                "output": buffers.stdout,
                "limit_bytes": stdout_limit_bytes,
                "stream_name": "stdout",
                "terminate_process_tree": terminate,
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
                "terminate_process_tree": terminate,
                "overflow": buffers.overflow,
                "overflow_lock": buffers.overflow_lock,
                "reader_errors": buffers.reader_errors,
            },
            name="neocortex-subprocess-stderr",
            daemon=True,
        ),
    )


def _unregister_capture_stream(
    selector: selectors.BaseSelector,
    capture: _CaptureStream,
) -> None:
    if not capture.registered:
        return
    try:
        selector.unregister(capture.fd)
    except (KeyError, ValueError):
        pass
    capture.registered = False


def _drain_ready_capture_stream(
    selector: selectors.BaseSelector,
    capture: _CaptureStream,
    buffers: _CaptureBuffers,
    controller: _TerminationController,
    *,
    deadline: float,
) -> None:
    """Drain one ready POSIX pipe without ever entering a blocking read."""

    while capture.registered:
        try:
            chunk = os.read(capture.fd, _READ_CHUNK_BYTES)
        except BlockingIOError:
            return
        except InterruptedError:
            return
        except OSError as error:
            buffers.reader_errors.append(error)
            _unregister_capture_stream(selector, capture)
            return
        if not chunk:
            capture.eof = True
            _unregister_capture_stream(selector, capture)
            return

        retained = max(0, (capture.limit_bytes + 1) - len(capture.output))
        if retained:
            capture.output.extend(chunk[:retained])
        if len(capture.output) <= capture.limit_bytes:
            continue
        with buffers.overflow_lock:
            if not buffers.overflow:
                buffers.overflow.append((capture.stream_name, capture.limit_bytes))
                controller.terminate(deadline)
        _unregister_capture_stream(selector, capture)
        return


def _wait_for_capture_posix(
    process: subprocess.Popen[bytes],
    buffers: _CaptureBuffers,
    controller: _TerminationController,
    *,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    deadline: float,
) -> _CaptureWait:
    """Wait, drain, and clean up POSIX pipes under one total deadline."""

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    captures = (
        _CaptureStream(process.stdout.fileno(), "stdout", buffers.stdout, stdout_limit_bytes),
        _CaptureStream(process.stderr.fileno(), "stderr", buffers.stderr, stderr_limit_bytes),
    )
    timed_out = False
    primary_error: BaseException | None = None
    returncode: int | None = None
    cleanup_incomplete = False
    normal_exit = False
    try:
        for capture in captures:
            os.set_blocking(capture.fd, False)
            selector.register(capture.fd, selectors.EVENT_READ, capture)

        # Keep one non-blocking wait call in the path. Besides avoiding an
        # unbounded wait, this preserves the established caller-exception
        # behavior for cancellation injected at Popen.wait.
        try:
            returncode = process.wait(timeout=0)
        except subprocess.TimeoutExpired:
            pass
        except BaseException as error:
            primary_error = error
            controller.terminate(deadline)
        if returncode is not None:
            normal_exit = True
            controller.terminate(deadline)

        while True:
            if primary_error is not None or timed_out or buffers.overflow:
                # There is already a primary reason. Do not wait for EOF from
                # an unauthorised descendant; closing our descriptors in the
                # finalizer is enough and keeps cancellation/overflow bounded.
                controller.terminate(deadline)
                break

            if returncode is None:
                try:
                    observed_returncode = process.poll()
                except BaseException as error:
                    primary_error = error
                    controller.terminate(deadline)
                    break
                if observed_returncode is not None:
                    returncode = observed_returncode
                    normal_exit = True
                    # A normal direct-child exit does not imply that a
                    # descendant closed inherited pipes. Terminate only the
                    # dedicated group, never a setsid descendant outside it.
                    controller.terminate(deadline)

            active_captures = tuple(capture for capture in captures if capture.registered)
            if normal_exit and not active_captures:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if returncode is None:
                    timed_out = True
                else:
                    cleanup_incomplete = True
                controller.terminate(deadline)
                break

            wait_for = min(_PROCESS_POLL_INTERVAL_SECONDS, remaining)
            if active_captures:
                events = selector.select(wait_for)
                for key, _mask in events:
                    _drain_ready_capture_stream(
                        selector,
                        key.data,
                        buffers,
                        controller,
                        deadline=deadline,
                    )
            else:
                # Registration can fail for a single stream, but the process
                # still needs polling and bounded cleanup.
                time.sleep(wait_for)
    except BaseException as error:
        if primary_error is None:
            primary_error = error
        controller.terminate(deadline)
    finally:
        active_captures = tuple(capture for capture in captures if capture.registered)
        if returncode is not None and active_captures and not primary_error and not timed_out:
            cleanup_incomplete = True
        selector.close()

    if returncode is None:
        try:
            returncode = process.poll()
        except BaseException as error:
            if primary_error is None:
                primary_error = error
    return _CaptureWait(
        (),
        timed_out,
        primary_error,
        returncode,
        cleanup_incomplete,
    )


def _wait_for_capture_windows(
    process: subprocess.Popen[bytes],
    readers: tuple[threading.Thread, ...],
    buffers: _CaptureBuffers,
    controller: _TerminationController,
    *,
    timeout_seconds: float,
    deadline: float,
) -> _CaptureWait:
    """Retain the Job Object/thread fallback for Windows installations."""

    started_readers: list[threading.Thread] = []
    timed_out = False
    primary_error: BaseException | None = None
    returncode: int | None = None
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        remaining = max(0.0, min(timeout_seconds, deadline - time.monotonic()))
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            controller.terminate(deadline)
    except BaseException as error:
        primary_error = error
        controller.terminate(deadline)
    if buffers.overflow:
        controller.terminate(deadline)
    return _CaptureWait(tuple(started_readers), timed_out, primary_error, returncode)


def _wait_for_capture(
    process: subprocess.Popen[bytes],
    readers: tuple[threading.Thread, ...],
    buffers: _CaptureBuffers,
    controller: _TerminationController,
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    deadline: float,
) -> _CaptureWait:
    if os.name != "nt":
        return _wait_for_capture_posix(
            process,
            buffers,
            controller,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            deadline=deadline,
        )
    return _wait_for_capture_windows(
        process,
        readers,
        buffers,
        controller,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
    )


def _finalize_capture(
    process: subprocess.Popen[bytes],
    job: _WindowsJob | None,
    started_readers: tuple[threading.Thread, ...],
    initial_returncode: int | None,
    *,
    deadline: float,
) -> tuple[int | None, list[BaseException]]:
    """Reap and close every local resource without exceeding deadline."""

    cleanup_errors: list[BaseException] = []
    if job is not None:
        try:
            job.close()
        except OSError as error:
            cleanup_errors.append(error)

    returncode = initial_returncode
    assert process.stdout is not None
    assert process.stderr is not None
    # A direct child that receives SIGKILL can need one scheduler turn before
    # waitpid(WNOHANG) observes it. Permit only this small, explicit tolerance
    # after the operation deadline for reaping that exact child and joining a
    # Windows fallback reader; no pipe read is allowed to consume it.
    cleanup_deadline = deadline + _CLEANUP_TOLERANCE_SECONDS
    try:
        if returncode is None:
            returncode = process.poll()
        if returncode is None:
            remaining = max(0.0, cleanup_deadline - time.monotonic())
            if remaining:
                returncode = process.wait(timeout=min(_PROCESS_REAP_SECONDS, remaining))
        if returncode is None:
            # The group termination path may have failed or may not own a
            # platform-specific descendant tree. The exact direct child is
            # still safe to kill, and the call remains bounded.
            try:
                process.kill()
            except OSError as error:
                cleanup_errors.append(error)
            remaining = max(0.0, cleanup_deadline - time.monotonic())
            if remaining:
                returncode = process.wait(timeout=min(_PROCESS_REAP_SECONDS, remaining))
            else:
                returncode = process.poll()
    except BaseException as error:
        cleanup_errors.append(error)
        try:
            process.kill()
        except OSError as kill_error:
            cleanup_errors.append(kill_error)
        remaining = max(0.0, cleanup_deadline - time.monotonic())
        if remaining:
            try:
                returncode = process.wait(timeout=min(_PROCESS_REAP_SECONDS, remaining))
            except BaseException as reap_error:
                cleanup_errors.append(reap_error)

    if started_readers:
        cleanup_errors.extend(
            _join_readers(
                started_readers,
                (process.stdout, process.stderr),
                deadline=cleanup_deadline,
            )
        )
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
    if wait.cleanup_incomplete:
        incomplete_error = RuntimeError(
            "subprocess output cleanup incomplete: inherited pipe did not reach EOF"
        )
        for cleanup_error in diagnostic_errors:
            incomplete_error.add_note(_cleanup_note(cleanup_error))
        raise incomplete_error
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
    # The deadline starts before process creation so start/wait/termination,
    # reaping and descriptor cleanup share one finite budget.
    deadline = time.monotonic() + timeout_seconds
    process, job = _start_bounded_process(
        command,
        stdin=stdin,
        creationflags=creationflags,
        cwd=cwd,
        environment=environment,
        memory_limit_bytes=memory_limit_bytes,
    )
    buffers = _CaptureBuffers()
    controller = _TerminationController(
        process,
        job,
        process_start_ticks=(
            _process_start_ticks(process.pid)
            if os.name != "nt" and process.pid
            else None
        ),
    )
    readers = (
        _capture_readers(
            process,
            buffers,
            controller,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            deadline=deadline,
        )
        if os.name == "nt"
        else ()
    )
    wait = _wait_for_capture(
        process,
        readers,
        buffers,
        controller,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        deadline=deadline,
    )
    returncode, cleanup_errors = _finalize_capture(
        process,
        job,
        wait.started_readers,
        wait.returncode,
        deadline=deadline,
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
    """Run a child with bounded capture and deterministic descendant cleanup.

    On POSIX the timeout is a total deadline shared by non-blocking capture,
    process-group termination, direct-child reaping, and local descriptor
    cleanup. A descendant that escaped the dedicated group and retained a
    pipe therefore cannot block the caller indefinitely; normal completion reports
    an explicit cleanup-incomplete error if EOF is not observed.
    Windows retains the optional Job Object and daemon-reader fallback.
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
