# region [00] Contexto del módulo
# Módulo: tests/test_bounded_subprocess.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from neocortex.runtime.control import isolated_process as isolated_process_module
from neocortex.runtime.control.bounded_subprocess import (
    SubprocessOutputLimitError,
    run_bounded_capture,
)
# endregion [01]

# region [02] Implementación


def _python(script: str) -> tuple[str, str, str]:
    return (sys.executable, "-c", script)


def test_bounded_capture_collects_both_streams_and_input() -> None:
    completed = run_bounded_capture(
        _python(
            "import sys; payload=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(payload.upper()); "
            "sys.stderr.buffer.write(b'diagnostic')"
        ),
        input_bytes=b"bounded",
        timeout_seconds=5,
        stdout_limit_bytes=64,
        stderr_limit_bytes=64,
    )

    assert completed.returncode == 0
    assert completed.stdout == b"BOUNDED"
    assert completed.stderr == b"diagnostic"


def test_bounded_capture_uses_exact_cwd_and_environment(tmp_path: Path) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in {"systemroot", "windir"}
    }
    environment["NEOCORTEX_BOUNDED_ENV"] = "controlled"
    completed = run_bounded_capture(
        _python(
            "import os,sys; "
            "sys.stdout.write(os.getcwd()+'\\n'); "
            "sys.stdout.write(os.environ['NEOCORTEX_BOUNDED_ENV']+'\\n'); "
            "sys.stdout.write(str('RUFF_CACHE_DIR' in os.environ))"
        ),
        timeout_seconds=5,
        stdout_limit_bytes=4096,
        stderr_limit_bytes=4096,
        cwd=tmp_path,
        environment=environment,
    )

    lines = completed.stdout.decode("utf-8").splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1:] == ["controlled", "False"]


def test_bounded_capture_without_input_does_not_create_a_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_temporary_file(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("stdin without a payload must use DEVNULL")

    monkeypatch.setattr(
        "neocortex.runtime.control.bounded_subprocess.tempfile.TemporaryFile",
        reject_temporary_file,
    )

    completed = run_bounded_capture(
        _python("import sys; sys.stdout.buffer.write(b'bounded')"),
        timeout_seconds=5,
        stdout_limit_bytes=64,
        stderr_limit_bytes=64,
    )

    assert completed.returncode == 0
    assert completed.stdout == b"bounded"


@pytest.mark.parametrize("stream", ("stdout", "stderr"))
def test_bounded_capture_terminates_output_overflow(stream: str) -> None:
    target = "stdout" if stream == "stdout" else "stderr"
    script = (
        "import sys; "
        f"sys.{target}.buffer.write(b'x' * 1048576); "
        f"sys.{target}.buffer.flush()"
    )

    with pytest.raises(SubprocessOutputLimitError) as captured:
        run_bounded_capture(
            _python(script),
            timeout_seconds=5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
        )

    assert captured.value.stream == stream
    assert captured.value.limit_bytes == 1024


def test_output_limit_error_preserves_builtin_exception_protocol() -> None:
    error = SubprocessOutputLimitError("stderr", 17)

    assert error.args == ("stderr", 17)
    error.add_note("capture cleanup completed")
    assert error.__notes__ == ["capture cleanup completed"]

    try:
        raise ValueError("upstream failure")
    except ValueError as cause:
        try:
            raise error from cause
        except SubprocessOutputLimitError as raised:
            assert raised.__cause__ is cause
            assert raised.__traceback__ is not None


def test_bounded_capture_kills_and_reaps_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        run_bounded_capture(
            _python("import time; time.sleep(5)"),
            timeout_seconds=0.05,
            stdout_limit_bytes=64,
            stderr_limit_bytes=64,
        )

    assert captured.value.timeout == 0.05


@pytest.mark.skipif(os.name == "nt", reason="POSIX selector capture behavior")
@pytest.mark.parametrize("retained_stream", ("stdout", "stderr", "both"))
def test_bounded_capture_does_not_wait_for_descendant_retained_pipes(
    tmp_path: Path,
    retained_stream: str,
) -> None:
    """A setsid descendant cannot turn pipe cleanup into an unbounded join."""

    pid_path = tmp_path / f"retained-{retained_stream}.pid"
    # The leader must exit only after the descendant has escaped its group and
    # retained the selected pipes. A sleep races both setsid and interpreter
    # startup, especially when the test environment has a cold bytecode cache.
    parent_code = """
import os, sys, time
ready_read, ready_write = os.pipe()
if os.fork() == 0:
    os.close(ready_read)
    os.setsid()
    stream = sys.argv[2]
    if stream == "stderr":
        os.close(1)
    elif stream == "stdout":
        os.close(2)
    with open(sys.argv[1], "w", encoding="ascii") as pid_file:
        pid_file.write(str(os.getpid()))
    os.write(ready_write, b"1")
    os.close(ready_write)
    time.sleep(30)
else:
    os.close(ready_write)
    assert os.read(ready_read, 1) == b"1"
    os.close(ready_read)
    os._exit(0)
"""
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            run_bounded_capture(
                (sys.executable, "-I", "-S", "-B", "-c", parent_code,
                 str(pid_path), retained_stream),
                timeout_seconds=0.25,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
            )
    finally:
        try:
            os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError, ValueError):
            pass

    assert time.monotonic() - started < 0.5
    assert not any(
        thread.is_alive() and thread.name.startswith("neocortex-subprocess-")
        for thread in threading.enumerate()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup behavior")
def test_bounded_capture_reaps_group_after_direct_child_exits_without_eof() -> None:
    descendant_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1]],stdin=subprocess.DEVNULL);"
    )
    completed = run_bounded_capture(
        (*_python(parent_code), descendant_code),
        timeout_seconds=2,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
    )

    assert completed.returncode == 0


def test_bounded_capture_rejects_non_finite_timeout() -> None:
    for timeout_seconds in (math.inf, math.nan):
        with pytest.raises(ValueError, match="finite"):
            run_bounded_capture(
                _python("pass"),
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=1,
                stderr_limit_bytes=1,
            )


@pytest.mark.parametrize(
    "arguments",
    (
        {"timeout_seconds": 0, "stdout_limit_bytes": 1, "stderr_limit_bytes": 1},
        {"timeout_seconds": 1, "stdout_limit_bytes": -1, "stderr_limit_bytes": 1},
        {"timeout_seconds": 1, "stdout_limit_bytes": 1, "stderr_limit_bytes": -1},
        {
            "timeout_seconds": 1,
            "stdout_limit_bytes": 1,
            "stderr_limit_bytes": 1,
            "memory_limit_bytes": 0,
        },
    ),
)
def test_bounded_capture_rejects_invalid_bounds(
    arguments: dict[str, float | int],
) -> None:
    with pytest.raises(ValueError):
        run_bounded_capture(_python("pass"), **arguments)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_bounded_capture_terminates_grandchild_tree_on_timeout(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "grandchild.pid"
    token = "neocortex-bounded-grandchild-regression"
    grandchild_code = "import time; time.sleep(30)"
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen("
        "[sys.executable,'-c',sys.argv[2],sys.argv[3]],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True); "
        "pid_path=pathlib.Path(sys.argv[1]); "
        "pid_temp=pid_path.with_suffix(pid_path.suffix+'.tmp'); "
        "pid_temp.write_text(str(child.pid),encoding='ascii'); "
        "pid_temp.replace(pid_path); "
        "time.sleep(30)"
    )
    command = (
        sys.executable,
        "-c",
        parent_code,
        str(pid_path),
        grandchild_code,
        token,
    )
    captured: list[BaseException] = []

    def invoke() -> None:
        try:
            run_bounded_capture(
                command,
                timeout_seconds=1.0,
                stdout_limit_bytes=64,
                stderr_limit_bytes=64,
            )
        except BaseException as error:
            captured.append(error)

    runner = threading.Thread(target=invoke, name="bounded-grandchild-probe")
    runner.start()
    try:
        deadline = time.monotonic() + 5.0
        grandchild_pid: int | None = None
        while runner.is_alive() and time.monotonic() < deadline:
            try:
                published_pid = pid_path.read_text(encoding="ascii").strip()
                if published_pid:
                    grandchild_pid = int(published_pid)
                    break
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(0.01)
        assert grandchild_pid is not None, (
            "grandchild PID was not published before the bounded worker exited; "
            f"runner_alive={runner.is_alive()} captured={captured!r}"
        )

        process_terminate = 0x0001
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        wait_object_0 = 0
        wait_timeout = 258
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_terminate | process_query_limited_information | synchronize,
            False,
            grandchild_pid,
        )
        assert handle
        try:
            runner.join(timeout=10)
            assert not runner.is_alive()
            assert len(captured) == 1
            assert isinstance(captured[0], subprocess.TimeoutExpired)
            assert kernel32.WaitForSingleObject(handle, 5_000) == wait_object_0
        finally:
            if kernel32.WaitForSingleObject(handle, 0) == wait_timeout:
                assert kernel32.TerminateProcess(handle, 97)
                assert kernel32.WaitForSingleObject(handle, 5_000) == wait_object_0
            assert kernel32.CloseHandle(handle)
    finally:
        runner.join(timeout=10)


class _InjectedBaseException(BaseException):
    pass


@pytest.mark.parametrize(
    "error_type",
    (KeyboardInterrupt, RuntimeError, _InjectedBaseException),
)
def test_bounded_capture_reaps_when_wait_raises_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    original_wait = subprocess.Popen.wait
    injected = False

    def interrupted_wait(
        process: subprocess.Popen[bytes],
        timeout: float | None = None,
    ) -> int:
        nonlocal injected
        if not injected:
            injected = True
            raise error_type("injected wait failure")
        return original_wait(process, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_wait)
    with pytest.raises(error_type, match="injected wait failure"):
        run_bounded_capture(
            _python("import time; time.sleep(30)"),
            timeout_seconds=5,
            stdout_limit_bytes=64,
            stderr_limit_bytes=64,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_bounded_capture_rolls_back_incomplete_job_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_resume(_process_handle: int, _process_id: int) -> None:
        raise RuntimeError("injected resume failure")

    monkeypatch.setattr(
        isolated_process_module,
        "_resume_suspended_process",
        fail_resume,
    )
    with pytest.raises(RuntimeError, match="injected resume failure"):
        run_bounded_capture(
            _python("import time; time.sleep(30)"),
            timeout_seconds=5,
            stdout_limit_bytes=64,
            stderr_limit_bytes=64,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_windows_job_handle_close_is_idempotent() -> None:
    job = isolated_process_module.WindowsKillOnCloseJob()
    assert not job.closed
    job.close()
    assert job.closed
    job.close()
    assert job.closed


# endregion [02]
