"""POSIX process-tree and mandatory memory-containment contracts."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from neocortex.runtime.control import bounded_subprocess as bounded_module
from neocortex.runtime.control.bounded_subprocess import run_bounded_capture


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX containment contract")


def _python(script: str, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-c", script, *arguments)


def _terminated_or_zombie(process_id: int) -> bool:
    status = Path(f"/proc/{process_id}/stat")
    try:
        fields = status.read_text(encoding="ascii").split()
    except FileNotFoundError:
        return True
    return len(fields) > 2 and fields[2] == "Z"


def test_timeout_terminates_child_and_grandchild_process_group(tmp_path: Path) -> None:
    published = tmp_path / "grandchild.pid"
    parent = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii');"
        "time.sleep(30)"
    )
    captured: list[BaseException] = []

    def invoke() -> None:
        try:
            run_bounded_capture(
                _python(parent, str(published)),
                timeout_seconds=1,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
            )
        except BaseException as exc:
            captured.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    deadline = time.monotonic() + 5
    process_id: int | None = None
    while time.monotonic() < deadline and process_id is None:
        try:
            process_id = int(published.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.01)
    assert process_id is not None
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(captured) == 1 and isinstance(captured[0], subprocess.TimeoutExpired)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _terminated_or_zombie(process_id):
        time.sleep(0.02)
    assert _terminated_or_zombie(process_id)


def test_requested_memory_limit_is_enforced_with_prlimit() -> None:
    result = run_bounded_capture(
        _python(
            "import sys;"
            "\ntry: bytearray(256*1024*1024)"
            "\nexcept MemoryError: raise SystemExit(73)"
            "\nraise SystemExit(0)"
        ),
        timeout_seconds=10,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
        memory_limit_bytes=96 * 1024 * 1024,
    )
    assert result.returncode == 73


def test_missing_prlimit_abstains_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bounded_module, "_PRLIMIT_PATH", str(tmp_path / "missing-prlimit"))
    popen = pytest.fail
    monkeypatch.setattr(bounded_module.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="posix_memory_containment_unavailable"):
        run_bounded_capture(
            _python("raise SystemExit(0)"),
            timeout_seconds=5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            memory_limit_bytes=64 * 1024 * 1024,
        )
