"""Linux isolated-worker cleanup retains ownership after its leader exits."""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.runtime.control import isolated_process as isolated


TEST_CAPABILITIES = ("base", "platform")
pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Linux session identity contract"),
    pytest.mark.capability("base", "platform"),
]


def _worker_with_descendant(ready: str, exit_early: bool, ignore_term: bool) -> None:
    script = (
        "import os,pathlib,signal,sys,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN if sys.argv[2]=='1' else signal.SIG_DFL);"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
        "time.sleep(8)"
    )
    subprocess.Popen(
        [sys.executable, "-c", script, ready, str(int(ignore_term))],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not exit_early:
        time.sleep(8)


def _term_ignoring_worker(ready: str) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(ready).write_text("ready")
    time.sleep(8)


def _await_file(path: Path) -> str:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if path.exists() and (value := path.read_text()):
            return value
        time.sleep(0.01)
    pytest.fail("isolated worker did not publish fixture readiness")


def _terminated(process_id: int) -> bool:
    try:
        fields = Path(f"/proc/{process_id}/stat").read_bytes().rpartition(b")")[2].split()
    except FileNotFoundError:
        return True
    return fields[0] == b"Z"


def _assert_terminated(process_id: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not _terminated(process_id):
        time.sleep(0.01)
    assert _terminated(process_id)


def _cleanup_fixture(process, process_group_id: int | None = None) -> None:
    # Safety-net only for fixture PIDs, even when the regression assertion fails.
    if process_group_id is None:
        process_group_id = process.pid
    try:
        if process_group_id is not None:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                if not process._closed and process.is_alive():
                    process.kill()
            if not process._closed:
                process.join(timeout=2)
    finally:
        isolated.close_isolated_process(process)


@pytest.mark.parametrize("exit_early", [False, True])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_cleanup_stops_group_even_after_leader_exits(
    tmp_path: Path, exit_early: bool, ignore_term: bool,
) -> None:
    ready = tmp_path / "descendant.pid"
    process = isolated.isolated_spawn_process(
        target=_worker_with_descendant,
        args=(str(ready), exit_early, ignore_term),
    )
    process.start()
    descendant = None
    try:
        descendant = int(_await_file(ready))
        assert os.getpgid(descendant) == process.pid
        if exit_early:
            process.join(timeout=2)
            assert process.exitcode == 0
        started = time.monotonic()
        isolated.terminate_isolated_process(process, timeout_seconds=1)
        assert time.monotonic() - started < 1.5
        assert not process.is_alive()
        _assert_terminated(descendant)
        # A second cleanup must not target a numeric PGID again after release.
        isolated.terminate_isolated_process(process, timeout_seconds=1)
    finally:
        _cleanup_fixture(process)
        if descendant is not None:
            _assert_terminated(descendant)


def test_cleanup_reaps_a_term_ignoring_leader_within_total_bound(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    process = isolated.isolated_spawn_process(
        target=_term_ignoring_worker, args=(str(ready),),
    )
    process.start()
    try:
        _await_file(ready)
        started = time.monotonic()
        isolated.terminate_isolated_process(process, timeout_seconds=0.4)
        assert time.monotonic() - started < 0.8
        assert process.exitcode == -signal.SIGKILL
        assert not process.is_alive()
    finally:
        _cleanup_fixture(process)


def test_cleanup_without_descendants_preserves_unrelated_worker() -> None:
    normal = isolated.isolated_spawn_process(target=time.sleep, args=(0.01,))
    unrelated = isolated.isolated_spawn_process(target=time.sleep, args=(8,))
    normal.start()
    unrelated.start()
    try:
        normal.join(timeout=4)
        assert normal.exitcode == 0
        isolated.terminate_isolated_process(normal, timeout_seconds=0.4)
        assert unrelated.is_alive()
    finally:
        _cleanup_fixture(normal)
        _cleanup_fixture(unrelated)


@pytest.mark.parametrize("ignore_term", [False, True])
def test_close_cleans_owned_group_after_graceful_leader_exit(
    tmp_path: Path, ignore_term: bool,
) -> None:
    ready = tmp_path / "descendant.pid"
    process = isolated.isolated_spawn_process(
        target=_worker_with_descendant,
        args=(str(ready), True, ignore_term),
    )
    process.start()
    process_group_id = process.pid
    descendant = None
    try:
        descendant = int(_await_file(ready))
        process.join(timeout=2)
        assert process.exitcode == 0
        started = time.monotonic()
        isolated.close_isolated_process(process)
        assert time.monotonic() - started < 2
        assert process._closed
        assert process._neocortex_group_closed
        _assert_terminated(descendant)
        isolated.close_isolated_process(process)
    finally:
        _cleanup_fixture(process, process_group_id)
        if descendant is not None:
            _assert_terminated(descendant)


def test_close_without_descendants_releases_handles_promptly() -> None:
    process = isolated.isolated_spawn_process(target=time.sleep, args=(0.01,))
    process.start()
    process_group_id = process.pid
    try:
        process.join(timeout=4)
        assert process.exitcode == 0
        started = time.monotonic()
        isolated.close_isolated_process(process)
        assert time.monotonic() - started < 0.4
        assert process._closed
    finally:
        _cleanup_fixture(process, process_group_id)


@pytest.mark.parametrize("alive", [False, True])
def test_close_does_not_terminate_generic_or_active_processes(
    monkeypatch: pytest.MonkeyPatch, alive: bool,
) -> None:
    calls: list[str] = []
    process = SimpleNamespace(
        is_alive=lambda: alive,
        close=lambda: calls.append("close"),
    )
    if alive:
        process._neocortex_session_identity = [123456, 100]
        process._neocortex_group_closed = False
    monkeypatch.setattr(
        isolated, "terminate_isolated_process", lambda _process: pytest.fail("unowned cleanup"),
    )
    isolated.close_isolated_process(process)
    assert calls == ([] if alive else ["close"])


def test_close_retains_handles_when_group_identity_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(
        pid=123456,
        _neocortex_session_identity=[123456, 100],
        _neocortex_group_closed=False,
        is_alive=lambda: False,
        close=lambda: pytest.fail("closed handles despite ownership error"),
    )
    monkeypatch.setattr(isolated, "_process_session_identity", lambda _pid: (123456, 123456, 200))
    monkeypatch.setattr(isolated.os, "killpg", lambda *_args: pytest.fail("reused group"))
    with pytest.raises(RuntimeError, match="replaced isolated"):
        isolated.close_isolated_process(process)


def test_cleanup_before_bootstrap_only_signals_the_direct_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    process = SimpleNamespace(
        pid=123456,
        _neocortex_session_identity=[0, 0],
        _neocortex_group_closed=False,
        is_alive=lambda: "terminate" not in calls,
        terminate=lambda: calls.append("terminate"),
        kill=lambda: calls.append("kill"),
        join=lambda timeout: calls.append("join"),
    )
    monkeypatch.setattr(isolated.os, "killpg", lambda *_args: pytest.fail("unowned group"))
    isolated.terminate_isolated_process(process, timeout_seconds=0.4)
    assert calls == ["terminate", "join", "join"]
    assert process._neocortex_group_closed


def test_cleanup_rechecks_bootstrap_that_races_the_direct_child_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    identity = [0, 0]

    def terminate() -> None:
        identity[:] = [123456, 100]

    process = SimpleNamespace(
        pid=123456,
        _neocortex_session_identity=identity,
        _neocortex_group_closed=False,
        is_alive=lambda: not identity[0],
        terminate=terminate,
        join=lambda timeout: None,
    )

    def signal_group(_pid: int, _birth: int, event: int) -> bool:
        calls.append(event)
        return True

    monkeypatch.setattr(isolated, "_signal_owned_group", signal_group)
    isolated.terminate_isolated_process(process, timeout_seconds=0)
    assert calls == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.parametrize("current_identity", [
    (123456, 123456, 200),
    (123456, 98765, 100),
    (98765, 123456, 100),
])
def test_cleanup_refuses_a_reused_or_foreign_group_before_signalling(
    monkeypatch: pytest.MonkeyPatch, current_identity: tuple[int, int, int],
) -> None:
    process = SimpleNamespace(
        pid=123456,
        _neocortex_session_identity=[123456, 100],
        _neocortex_group_closed=False,
    )
    monkeypatch.setattr(isolated, "_process_session_identity", lambda _pid: current_identity)
    monkeypatch.setattr(isolated.os, "killpg", lambda *_args: pytest.fail("reused group"))
    with pytest.raises(RuntimeError, match="replaced isolated"):
        isolated.terminate_isolated_process(process, timeout_seconds=0.4)
    assert not process._neocortex_group_closed


def test_cleanup_revalidates_group_identity_before_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    identities = iter([(123456, 123456, 100), (123456, 123456, 200)])
    process = SimpleNamespace(
        pid=123456,
        _neocortex_session_identity=[123456, 100],
        _neocortex_group_closed=False,
        is_alive=lambda: False,
    )
    monkeypatch.setattr(isolated, "_process_session_identity", lambda _pid: next(identities))
    monkeypatch.setattr(isolated.os, "killpg", lambda _pid, event: calls.append(event))
    with pytest.raises(RuntimeError, match="replaced isolated"):
        isolated.terminate_isolated_process(process, timeout_seconds=0.4)
    assert calls == [signal.SIGTERM]


def test_cleanup_refuses_an_unowned_process_group() -> None:
    process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(8,))
    process.start()
    try:
        with pytest.raises(RuntimeError, match="not owned"):
            isolated.terminate_isolated_process(process, timeout_seconds=0.4)
        assert process.is_alive()
    finally:
        process.kill()
        process.join(timeout=2)
        process.close()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
def test_cleanup_rejects_unbounded_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        isolated.terminate_isolated_process(SimpleNamespace(), timeout_seconds=timeout)
