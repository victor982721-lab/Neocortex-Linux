"""Delegated cgroup adapter integration without claiming host delegation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from neocortex.api.agent_activity import AgentActivity, AgentActivityRecoveryRequired
from neocortex.runtime.control.bounded_subprocess import run_bounded_capture
from neocortex.runtime.control.process_scope import DelegatedProcessScope, ProcessScopeError


def test_regular_directory_cannot_forge_cgroup_scope(tmp_path: Path):
    fake = tmp_path / "delegated"
    fake.mkdir(mode=0o700)
    (fake / "cgroup.events").write_text("populated 0\n")
    written = []
    with pytest.raises(ProcessScopeError, match="cgroup v2"):
        DelegatedProcessScope(fake, "activity", receipt_writer=written.append)
    assert written == []
    assert list(fake.iterdir()) == [fake / "cgroup.events"]


def test_trusted_preexec_barrier_waits_for_assignment_and_quiescence_precedes_return(tmp_path: Path):
    events = []
    marker = tmp_path / "producer-entered"

    class ScopeFixture:
        def prepare_command(self, command):
            return DelegatedProcessScope.prepare_command(self, command)
        def attach(self, pid, start_ticks, *, deadline):
            assert start_ticks is not None
            while time.monotonic() < deadline:
                if Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2].split()[0] == b"T":
                    break
                time.sleep(0.005)
            assert not marker.exists()
            events.append("assigned")
            os.kill(pid, signal.SIGCONT)
        def terminate(self, deadline=None):
            events.append("terminate")
        def verify_quiescent(self, *, deadline):
            assert marker.read_text() == "complete"
            events.append("quiescent")

    result = run_bounded_capture([sys.executable, "-c", "from pathlib import Path;"
                                 f"Path({str(marker)!r}).write_text('complete')"],
                                 timeout_seconds=3.0, stdout_limit_bytes=1024, stderr_limit_bytes=1024,
                                 on_started=lambda pid, ticks: events.append("registered"),
                                 process_scope=ScopeFixture())
    assert result.returncode == 0
    assert events == ["registered", "assigned", "terminate", "quiescent"]


def test_group_only_and_unknown_temp_contract_report_their_limits(tmp_path: Path):
    activity = AgentActivity.prepare(tmp_path / "state", "bounded")
    result = activity.run([sys.executable, "-c", "print('ok')"])
    assert result.tool_temp_coverage == "incomplete"
    assert result.process_scope_coverage == "incomplete_for_detached_descendants"
    assert result.stdout == "ok\n"
    with pytest.raises(AgentActivityRecoveryRequired, match="producer quiescence"):
        activity.close()
    assert activity.path.exists()


def test_detached_live_descendant_blocks_close_retire_and_manual_release(tmp_path: Path):
    activity = AgentActivity.prepare(tmp_path / "state", "detached")
    child = None
    try:
        result = activity.run([sys.executable, "-c", "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
            "start_new_session=True); print(p.pid)"])
        child = int(result.stdout.strip())
        os.kill(child, 0)
        with pytest.raises(AgentActivityRecoveryRequired, match="producer quiescence"):
            activity.close()
        activity.reconcile("fail")
        with pytest.raises(AgentActivityRecoveryRequired, match="producer quiescence"):
            activity.reconcile("release", release_authorized=True,
                               evidence={"quiescence_confirmed": True, "approved": True})
        resumed = AgentActivity.resume(tmp_path / "state", "detached")
        with pytest.raises(AgentActivityRecoveryRequired, match="producer quiescence"):
            resumed.reconcile("release", release_authorized=True)
        assert activity.path.exists()
        assert activity.state == "failed-retained"
        os.kill(child, 0)
    finally:
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_failed_process_claim_cannot_be_released_by_operator_boolean(tmp_path: Path):
    activity = AgentActivity.prepare(tmp_path / "state", "uncertain", process_pid=os.getpid())
    activity.reconcile("fail")
    with pytest.raises(AgentActivityRecoveryRequired, match="producer quiescence"):
        activity.reconcile("release", release_authorized=True, evidence={"approved": True})
    assert activity.path.exists()


def test_launch_failure_before_pid_publication_cannot_be_released_as_manual_activity(tmp_path: Path, monkeypatch):
    activity = AgentActivity.prepare(tmp_path / "state", "missing-process-claim")
    child = None

    def launch_without_confirmation(*args, **kwargs):
        nonlocal child
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        raise RuntimeError("process identity publication interrupted")

    monkeypatch.setattr("neocortex.api.agent_activity.run_bounded_capture", launch_without_confirmation)
    try:
        with pytest.raises(RuntimeError, match="publication interrupted"):
            activity.run([sys.executable, "-c", "pass"])
        assert child is not None and child.poll() is None
        with pytest.raises(AgentActivityRecoveryRequired, match="producer quiescence"):
            activity.reconcile("release", release_authorized=True)
        assert activity.path.exists()
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=3)


def test_unchecked_nonzero_result_retains_failed_activity(tmp_path: Path):
    activity = AgentActivity.prepare(tmp_path / "state", "failed")
    result = activity.run([sys.executable, "-c", "raise SystemExit(2)"], check=False)
    assert result.returncode == 2
    assert activity.state == "failed-retained"
