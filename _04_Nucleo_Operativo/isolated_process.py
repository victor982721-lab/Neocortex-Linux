"""Spawn isolated worker processes that do not receive console Ctrl+C events."""

from __future__ import annotations

import multiprocessing
import importlib
import os
import signal
import subprocess


def _posix_resource_module():
    try:
        return importlib.import_module("resource")
    except ImportError as exc:  # pragma: no cover - non-POSIX defensive branch
        raise RuntimeError(
            "posix_memory_containment_unavailable: resource module is required"
        ) from exc


def _validate_posix_memory_limit(memory_limit_bytes: int | None) -> None:
    if memory_limit_bytes is None:
        return
    resource = _posix_resource_module()
    if not hasattr(resource, "RLIMIT_AS"):
        raise RuntimeError("posix_memory_containment_unavailable: RLIMIT_AS is required")
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY and memory_limit_bytes > hard:
        raise RuntimeError(
            "posix_memory_containment_unavailable: requested limit exceeds hard RLIMIT_AS"
        )


def _posix_isolated_target(target, args: tuple, memory_limit_bytes: int | None) -> None:
    """Create the child session and limits before invoking untrusted work."""

    os.setsid()
    if memory_limit_bytes is not None:
        resource = _posix_resource_module()
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, hard))
    target(*args)


# region [01] Cross-platform process factory


def isolated_spawn_process(
    *,
    target,
    args: tuple,
    daemon: bool = False,
    memory_limit_bytes: int | None = None,
):
    """Return a spawn process detached from the Windows console control group."""

    if memory_limit_bytes is not None and memory_limit_bytes < 1:
        raise ValueError("isolated process memory limit must be positive")
    if os.name != "nt":
        _validate_posix_memory_limit(memory_limit_bytes)
        return multiprocessing.get_context("spawn").Process(
            target=_posix_isolated_target,
            args=(target, args, memory_limit_bytes),
            daemon=daemon,
        )
    return _WindowsIsolatedSpawnProcess(
        target=target,
        args=args,
        daemon=daemon,
        memory_limit_bytes=memory_limit_bytes,
    )


# endregion [01]


# region [02] Windows spawn implementation
# multiprocessing has no public creationflags option. Keep its standard spawn
# protocol and change only CreateProcess flags so workers and Tesseract have no
# console and cannot consume the coordinator's Ctrl+C event.


if os.name == "nt":
    import ctypes
    import msvcrt
    import sys
    import _winapi
    from ctypes import wintypes
    from multiprocessing import reduction, spawn, util
    from multiprocessing.context import (
        SpawnProcess,
        get_spawning_popen,
        set_spawning_popen,
    )
    from multiprocessing.popen_spawn_win32 import Popen as _StandardWindowsSpawnPopen
    from subprocess import STARTF_FORCEOFFFEEDBACK, STARTUPINFO

    _windows_spawn = importlib.import_module("multiprocessing.popen_spawn_win32")
    WINENV = _windows_spawn.WINENV
    _close_handles = _windows_spawn._close_handles
    _path_eq = _windows_spawn._path_eq

    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    CREATE_SUSPENDED = 0x00000004
    RESUME_THREAD_FAILED = 0xFFFFFFFF
    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        )

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = (
            ("size", wintypes.DWORD),
            ("usage_count", wintypes.DWORD),
            ("thread_id", wintypes.DWORD),
            ("owner_process_id", wintypes.DWORD),
            ("base_priority", wintypes.LONG),
            ("priority_delta", wintypes.LONG),
            ("flags", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_ulonglong)
            for name in (
                "read_operation_count",
                "write_operation_count",
                "other_operation_count",
                "read_transfer_count",
                "write_transfer_count",
                "other_transfer_count",
            )
        )

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("basic_limit_information", _JobObjectBasicLimitInformation),
            ("io_info", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessId.restype = wintypes.DWORD

    def _close_isolated_handles(
        process_handle: int,
        read_handle: int,
        job_handle: int,
    ) -> None:
        try:
            _close_handles(process_handle, read_handle)
        finally:
            _winapi.CloseHandle(job_handle)

    def _configure_job_limits(
        job_handle: int,
        memory_limit_bytes: int | None,
    ) -> None:
        information = _JobObjectExtendedLimitInformation()
        information.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit_bytes is not None:
            information.basic_limit_information.limit_flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            information.job_memory_limit = memory_limit_bytes
        if not _kernel32.SetInformationJobObject(
            job_handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def _create_kill_on_close_job(memory_limit_bytes: int | None) -> int:
        job_handle = _kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        try:
            _configure_job_limits(int(job_handle), memory_limit_bytes)
        except BaseException:
            _winapi.CloseHandle(job_handle)
            raise
        return int(job_handle)

    def _resume_suspended_process(process_handle: int, process_id: int) -> None:
        observed_process_id = int(_kernel32.GetProcessId(process_handle))
        if observed_process_id != process_id:
            raise RuntimeError("suspended process handle does not match the expected process ID")
        snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if int(snapshot) == INVALID_HANDLE_VALUE:
            raise OSError(
                ctypes.get_last_error(),
                "CreateToolhelp32Snapshot failed",
            )
        thread_ids: list[int] = []
        try:
            entry = _ThreadEntry32()
            entry.size = ctypes.sizeof(entry)
            found = bool(_kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while found:
                if int(entry.owner_process_id) == process_id:
                    thread_ids.append(int(entry.thread_id))
                found = bool(_kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            _winapi.CloseHandle(snapshot)
        if len(thread_ids) != 1:
            raise RuntimeError("suspended process must expose exactly one primary thread")
        thread_handle = _kernel32.OpenThread(
            THREAD_SUSPEND_RESUME,
            False,
            thread_ids[0],
        )
        if not thread_handle:
            raise OSError(ctypes.get_last_error(), "OpenThread failed")
        try:
            if _kernel32.ResumeThread(thread_handle) == RESUME_THREAD_FAILED:
                raise OSError(ctypes.get_last_error(), "ResumeThread failed")
        finally:
            _winapi.CloseHandle(thread_handle)

    class _WindowsIsolatedPopen(_StandardWindowsSpawnPopen):
        def __init__(self, process_obj):
            prep_data = spawn.get_preparation_data(process_obj._name)
            read_handle, write_handle = _winapi.CreatePipe(None, 0)
            write_fd = msvcrt.open_osfhandle(write_handle, 0)
            try:
                job_handle = _create_kill_on_close_job(process_obj._memory_limit_bytes)
            except BaseException:
                os.close(write_fd)
                _winapi.CloseHandle(read_handle)
                raise
            command = spawn.get_command_line(
                parent_pid=os.getpid(),
                pipe_handle=read_handle,
            )
            python_executable = spawn.get_executable()
            environment = os.environ.copy()
            if WINENV and _path_eq(python_executable, sys.executable):
                python_executable = getattr(sys, "_base_executable", sys.executable)
                command[0] = python_executable
                environment["__PYVENV_LAUNCHER__"] = sys.executable
            command_line = " ".join(f'"{item}"' for item in command)
            creation_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED
            )

            with open(write_fd, "wb", closefd=True) as child_stream:
                process_handle = None
                thread_handle = None
                try:
                    process_handle, thread_handle, pid, _tid = _winapi.CreateProcess(
                        python_executable,
                        command_line,
                        None,
                        None,
                        False,
                        creation_flags,
                        environment,
                        None,
                        STARTUPINFO(dwFlags=STARTF_FORCEOFFFEEDBACK),
                    )
                    if not _kernel32.AssignProcessToJobObject(job_handle, int(process_handle)):
                        raise OSError(
                            ctypes.get_last_error(),
                            "AssignProcessToJobObject failed",
                        )
                    if _kernel32.ResumeThread(int(thread_handle)) == RESUME_THREAD_FAILED:
                        raise OSError(ctypes.get_last_error(), "ResumeThread failed")
                    _winapi.CloseHandle(thread_handle)
                    thread_handle = None
                except BaseException:
                    try:
                        if process_handle is not None:
                            try:
                                # This is the exact handle returned by CreateProcess;
                                # it cannot select an unrelated process after PID reuse.
                                _winapi.TerminateProcess(process_handle, 1)
                            except OSError:
                                pass
                            finally:
                                _winapi.CloseHandle(process_handle)
                        if thread_handle is not None:
                            _winapi.CloseHandle(thread_handle)
                        _winapi.CloseHandle(read_handle)
                    finally:
                        # KILL_ON_JOB_CLOSE is the final containment fallback when
                        # assignment succeeded but a later initialization step failed.
                        _winapi.CloseHandle(job_handle)
                    raise

                self.pid = pid
                self.returncode = None
                self._handle = process_handle
                self._job_handle = job_handle
                self.sentinel = int(process_handle)
                self.finalizer = util.Finalize(
                    self,
                    _close_isolated_handles,
                    (self.sentinel, int(read_handle), job_handle),
                )
                set_spawning_popen(self)
                try:
                    reduction.dump(prep_data, child_stream)
                    reduction.dump(process_obj, child_stream)
                except BaseException:
                    try:
                        self.terminate_tree()
                    except OSError:
                        pass
                    try:
                        self.wait(timeout=5)
                    except (OSError, TimeoutError):
                        pass
                    self.finalizer()
                    raise
                finally:
                    set_spawning_popen(None)

        def duplicate_for_child(self, handle):
            assert self is get_spawning_popen()
            return reduction.duplicate(handle, self.sentinel)

        def terminate_tree(self) -> None:
            if not _kernel32.TerminateJobObject(self._job_handle, 1):
                # The exact process handle is still safer than any PID lookup.
                _winapi.TerminateProcess(self._handle, 1)

        def set_memory_limit(self, memory_limit_bytes: int | None) -> None:
            _configure_job_limits(self._job_handle, memory_limit_bytes)

    class _WindowsIsolatedSpawnProcess(SpawnProcess):
        def __init__(
            self,
            *,
            target,
            args: tuple,
            daemon: bool,
            memory_limit_bytes: int | None,
        ) -> None:
            self._memory_limit_bytes = memory_limit_bytes
            super().__init__(target=target, args=args, daemon=daemon)

        @staticmethod
        def _Popen(process_obj):
            return _WindowsIsolatedPopen(process_obj)

        def terminate_tree(self) -> None:
            popen = getattr(self, "_popen", None)
            if popen is not None:
                popen.terminate_tree()

        def set_memory_limit(self, memory_limit_bytes: int | None) -> None:
            if memory_limit_bytes is not None and memory_limit_bytes < 1:
                raise ValueError("isolated process memory limit must be positive")
            popen = getattr(self, "_popen", None)
            if popen is None:
                self._memory_limit_bytes = memory_limit_bytes
            else:
                popen.set_memory_limit(memory_limit_bytes)


# endregion [02]


# region [03] Supervision helpers


class WindowsKillOnCloseJob:
    # Own one Windows Job Object used to contain an exact suspended child.

    __slots__ = ("_assigned", "_handle")

    def __init__(self, memory_limit_bytes: int | None = None) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows Job Objects are unavailable on this platform")
        if memory_limit_bytes is not None and memory_limit_bytes < 1:
            raise ValueError("Windows Job memory limit must be positive")
        self._handle = _create_kill_on_close_job(memory_limit_bytes)
        self._assigned = False

    @staticmethod
    def suspended_creation_flag() -> int:
        return CREATE_SUSPENDED if os.name == "nt" else 0

    @property
    def closed(self) -> bool:
        return self._handle == 0

    def assign_suspended(self, process: subprocess.Popen[bytes]) -> None:
        if self.closed:
            raise RuntimeError("Windows Job Object is already closed")
        if self._assigned:
            raise RuntimeError("Windows Job Object already owns a process")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None or process.pid is None:
            raise RuntimeError("Popen did not expose an exact Windows process handle")
        if not _kernel32.AssignProcessToJobObject(
            self._handle,
            int(process_handle),
        ):
            raise OSError(
                ctypes.get_last_error(),
                "AssignProcessToJobObject failed",
            )
        self._assigned = True
        _resume_suspended_process(int(process_handle), int(process.pid))

    def terminate(self, exit_code: int = 1) -> None:
        if self.closed or not self._assigned:
            return
        if not _kernel32.TerminateJobObject(self._handle, exit_code):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        handle = self._handle
        self._handle = 0
        if handle:
            _winapi.CloseHandle(handle)

    def __enter__(self) -> "WindowsKillOnCloseJob":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def set_isolated_process_memory_limit(
    process,
    memory_limit_bytes: int | None,
) -> None:
    """Adjust the exact worker Job limit before its next bounded task."""

    if memory_limit_bytes is not None and memory_limit_bytes < 1:
        raise ValueError("isolated process memory limit must be positive")
    setter = getattr(process, "set_memory_limit", None)
    if callable(setter):
        setter(memory_limit_bytes)
    elif os.name == "nt":
        raise RuntimeError("isolated Windows process has no supervised job handle")
    else:
        _validate_posix_memory_limit(memory_limit_bytes)
        if process.pid is None or not process.is_alive():
            raise RuntimeError("isolated POSIX process is not running")
        resource = _posix_resource_module()
        prlimit = getattr(resource, "prlimit", None)
        if not callable(prlimit):
            raise RuntimeError("posix_memory_containment_unavailable: resource.prlimit is required")
        _soft, hard = prlimit(process.pid, resource.RLIMIT_AS)
        requested = hard if memory_limit_bytes is None else memory_limit_bytes
        if hard != resource.RLIM_INFINITY and requested > hard:
            raise RuntimeError(
                "posix_memory_containment_unavailable: requested limit exceeds hard RLIMIT_AS"
            )
        prlimit(process.pid, resource.RLIMIT_AS, (requested, hard))


def terminate_isolated_process(process, timeout_seconds: float = 5.0) -> None:
    """Terminate only the supplied supervised child and its own descendants."""

    if not process.is_alive():
        return
    terminate_tree = getattr(process, "terminate_tree", None)
    if callable(terminate_tree):
        terminate_tree()
    elif os.name != "nt":
        process_group_id = process.pid
        if process_group_id is None or process_group_id <= 1:
            raise RuntimeError("isolated POSIX process has no safe process group")
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            process.terminate()
        process.join(timeout=max(0.0, timeout_seconds))
        if process.is_alive():
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                process.kill()
    else:
        raise RuntimeError("isolated Windows process has no supervised job handle")
    process.join(timeout=max(0.0, timeout_seconds))


def close_isolated_process(process) -> None:
    """Release process and Job handles after a supervised worker has stopped."""

    try:
        if not process.is_alive():
            process.close()
    except (AttributeError, ValueError):
        return


# endregion [03]
