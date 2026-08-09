"""Process-level exclusion for foreground watchers on controlled fixtures."""
# region [00] Contexto del módulo
# Módulo: tests/test_watcher_life_lease.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.models import FrameworkConfig
from _04_Nucleo_Operativo.watcher import IncrementalWatcher
from _04_Nucleo_Operativo.watcher_life_lease import (
    WatcherLifeLease,
    WatcherLifeLeaseConflict,
)
# endregion [01]

# region [02] Implementación


_CHILD_SCRIPT = r"""
import json
import os
import pathlib
import sys

from _04_Nucleo_Operativo.watcher_life_lease import WatcherLifeLease

root = pathlib.Path(sys.argv[1])
state = pathlib.Path(sys.argv[2])
ready = pathlib.Path(sys.argv[3])
lease = WatcherLifeLease(root, state)
lease.__enter__()
ready_temp = ready.with_suffix(ready.suffix + ".tmp")
ready_temp.write_text(json.dumps(lease.owner), encoding="utf-8")
ready_temp.replace(ready)
command = sys.stdin.readline().strip()
if command == "crash":
    os._exit(23)
lease.__exit__(None, None, None)
"""


def _start_owner(root: Path, state: Path, ready: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, str(root), str(state), str(ready)],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 8
    while True:
        try:
            owner = json.loads(ready.read_text(encoding="utf-8"))
            if isinstance(owner, dict) and owner.get("pid") == process.pid:
                return process
        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
            pass
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"lease child exited early ({process.returncode}): {stdout} {stderr}"
            )
        if time.monotonic() >= deadline:
            process.terminate()
            process.wait(5)
            raise AssertionError("lease child did not become ready")
        time.sleep(0.01)


def _finish_owner(process: subprocess.Popen[str], command: str = "release") -> int:
    assert process.stdin is not None
    process.stdin.write(command + "\n")
    process.stdin.flush()
    return process.wait(8)


def _cleanup_owner(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        _finish_owner(process)
    except (BrokenPipeError, subprocess.TimeoutExpired):
        process.terminate()
        process.wait(5)


def test_second_process_and_watcher_abstain_for_same_root_and_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    process = _start_owner(root, state, tmp_path / "ready.json")
    try:
        with pytest.raises(WatcherLifeLeaseConflict) as raised:
            with WatcherLifeLease(root, state):
                pytest.fail("a second life lease was granted")

        conflict = raised.value
        assert conflict.owner_status in {"live", "unknown"}
        assert conflict.owner is not None
        assert conflict.owner["pid"] == process.pid
        assert conflict.owner["root"] == os.path.normcase(os.path.realpath(root))
        assert conflict.owner["state_directory"] == os.path.normcase(os.path.realpath(state))
        if conflict.owner_status == "live":
            assert isinstance(conflict.owner["process_creation_time_ns"], int)
        else:
            assert conflict.owner["process_creation_time_ns"] is None
        assert isinstance(conflict.owner["started_ns"], int)
        assert isinstance(conflict.owner["host"], str)
        assert conflict.owner["host"]
        assert isinstance(conflict.owner["version"], str)
        assert conflict.owner["version"]
        assert isinstance(conflict.owner["argv"], list)

        watcher = IncrementalWatcher(
            FrameworkConfig(root=root, state_directory=state),
            checkpoint_loader=lambda _root: pytest.fail(
                "conflicting watcher reached its checkpoint"
            ),
        )
        with pytest.raises(WatcherLifeLeaseConflict):
            watcher.run_foreground()

        command = subprocess.run(
            [
                sys.executable,
                "-m",
                "neocortex",
                "--root",
                str(root),
                "--state-directory",
                str(state),
                "--watch",
                "--watch-bootstrap",
                "never",
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert command.returncode == 2
        assert "ERROR watch WatcherLifeLeaseConflict" in command.stdout
        assert any(f"owner_status={status}" in command.stdout for status in ("live", "unknown"))
    finally:
        _cleanup_owner(process)

    with WatcherLifeLease(root, state) as acquired:
        assert acquired.owner is not None
        assert acquired.owner["pid"] == os.getpid()


def test_different_roots_in_the_same_state_have_distinct_life_leases(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "corpus-a"
    root_b = tmp_path / "corpus-b"
    state = tmp_path / "state"
    root_a.mkdir()
    root_b.mkdir()
    process = _start_owner(root_a, state, tmp_path / "ready-a.json")
    try:
        with WatcherLifeLease(root_b, state) as second:
            assert second.owner is not None
            assert second.owner["root"] == os.path.normcase(os.path.realpath(root_b))
    finally:
        _cleanup_owner(process)


def test_abnormal_child_exit_releases_lock_and_stale_metadata_is_replaced(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    ready = tmp_path / "ready.json"
    process = _start_owner(root, state, ready)
    try:
        recorded = json.loads(ready.read_text(encoding="utf-8"))

        assert _finish_owner(process, "crash") == 23

        with WatcherLifeLease(root, state) as recovered:
            assert recovered.replaced_stale_metadata
            assert recovered.previous_metadata is not None
            assert recovered.previous_metadata["pid"] == recorded["pid"]
            assert recovered.owner is not None
            assert recovered.owner["pid"] == os.getpid()
    finally:
        _cleanup_owner(process)


def test_base_exception_releases_life_lease(tmp_path: Path) -> None:
    class InjectedStop(BaseException):
        pass

    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()

    with pytest.raises(InjectedStop):
        with WatcherLifeLease(root, state):
            raise InjectedStop

    with WatcherLifeLease(root, state) as reacquired:
        assert reacquired.replaced_stale_metadata
        assert reacquired.owner is not None

    def injected_checkpoint(_root: Path) -> None:
        raise InjectedStop

    watcher = IncrementalWatcher(
        FrameworkConfig(root=root, state_directory=state),
        checkpoint_loader=injected_checkpoint,
    )
    with pytest.raises(InjectedStop):
        watcher.run_foreground()

    with WatcherLifeLease(root, state):
        pass


# endregion [02]
