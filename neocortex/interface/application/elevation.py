"""Explicit Windows elevation boundary for USN-backed operational execution."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


# region [01] Elevation state and launch specification


@dataclass(frozen=True, slots=True)
class ElevationLaunchSpec:
    executable: Path
    parameters: str
    working_directory: Path


def is_elevated() -> bool:
    if os.name != "nt":
        return False
    return bool(cast(Any, ctypes).windll.shell32.IsUserAnAdmin())


def elevation_launch_spec(root: Path) -> ElevationLaunchSpec:
    project_root = Path(__file__).resolve().parents[3]
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        arguments = ["--ui", "--root", str(root)]
        working_directory = root
    else:
        executable = Path(sys.executable)
        arguments = [
            "-m",
            "neocortex",
            "--ui",
            "--root",
            str(root),
        ]
        working_directory = project_root
    return ElevationLaunchSpec(
        executable=executable,
        parameters=subprocess.list2cmdline(arguments),
        working_directory=working_directory,
    )


# endregion [01]


# region [02] Shell elevation


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def start_elevated_ui(root: Path) -> int:
    """Launch one visible elevated UI and return its exact process identifier."""

    if os.name != "nt":
        raise OSError("Explicit elevation is only supported on Windows")
    spec = elevation_launch_spec(root)
    windows_ctypes = cast(Any, ctypes)
    windll = windows_ctypes.windll
    shell32 = windll.shell32
    kernel32 = windll.kernel32
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(spec.executable)
    info.lpParameters = spec.parameters
    info.lpDirectory = str(spec.working_directory)
    info.nShow = 1
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise windows_ctypes.WinError()
    if not info.hProcess:
        raise OSError("Windows did not return the elevated UI process handle")
    try:
        process_id = int(kernel32.GetProcessId(info.hProcess))
    finally:
        kernel32.CloseHandle(info.hProcess)
    if process_id <= 0:
        raise OSError("Windows did not return the elevated UI process identifier")
    return process_id


# endregion [02]
