from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

from neocortex.runtime.control import worker_priority as priority
from neocortex.runtime.control.elastic_workers import elastic_map


def _priority_in_worker(value):
    status = priority.current_worker_priority()
    return value, os.getpriority(os.PRIO_PROCESS, 0), status is not None and status.applied


def test_native_launcher_changes_only_child_priority_and_preserves_exit_and_streams():
    before = os.getpriority(os.PRIO_PROCESS, 0)
    program = (
        "import json,os,sys; "
        "print(json.dumps({'nice':os.getpriority(os.PRIO_PROCESS,0),'pid':os.getpid()})); "
        "print('diagnostic',file=sys.stderr); sys.exit(7)"
    )
    process = subprocess.Popen(priority.background_command([sys.executable, "-c", program]),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate(timeout=10)
    result = json.loads(stdout)
    assert process.returncode == 7 and stderr == "diagnostic\n"
    assert result["pid"] == process.pid
    assert result["nice"] >= max(10, before)
    assert os.getpriority(os.PRIO_PROCESS, 0) == before


def test_elastic_process_initialization_observes_actual_nice_without_parent_change():
    before = os.getpriority(os.PRIO_PROCESS, 0)
    with elastic_map(_priority_in_worker, [1, 2], executor_kind="process", max_workers=2,
                     process_resident_bytes=0) as results:
        values = list(results)
    assert [value for value, _nice, _applied in values] == [1, 2]
    assert all(nice >= max(10, before) and applied for _value, nice, applied in values)
    assert os.getpriority(os.PRIO_PROCESS, 0) == before


def test_denied_priority_is_observable_and_does_not_attempt_a_foreign_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(priority.os, "getpriority", lambda which, pid: calls.append((which, pid)) or 0)

    def denied(increment):
        assert increment == 10
        raise PermissionError(errno.EPERM, "denied")

    monkeypatch.setattr(priority.os, "nice", denied)
    result = priority.configure_worker_priority()
    assert not result.applied and result.reason == f"errno:{errno.EPERM}"
    assert calls == [(os.PRIO_PROCESS, 0)]


def test_private_autogroup_rejects_shared_session_without_writing(monkeypatch):
    monkeypatch.setattr(priority.os, "getpriority", lambda _which, _pid: 10)
    monkeypatch.setattr(priority.os, "getsid", lambda _pid: 100)
    monkeypatch.setattr(priority.os, "getpgrp", lambda: 100)
    monkeypatch.setattr(priority.os, "getpid", lambda: 101)

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("shared session autogroup was modified")

    monkeypatch.setattr(priority.Path, "write_text", unexpected_write)
    status = priority.configure_worker_priority(private_session=True)
    assert status.applied and not status.autogroup_applied
    assert status.autogroup_reason == "shared-session"


def test_private_session_policy_leaves_parent_autogroup_unchanged(tmp_path):
    parent_group = Path("/proc/self/autogroup")
    before = parent_group.read_text() if parent_group.exists() else None
    program = (
        "import dataclasses,json,os,runpy; "
        f"module=runpy.run_path({str(Path(priority.__file__).resolve())!r}); "
        "status=module['configure_worker_priority'](private_session=True); "
        "print(json.dumps(dataclasses.asdict(status)))"
    )
    result = subprocess.run([sys.executable, "-c", program], check=True,
                            start_new_session=True, cwd=tmp_path, env={},
                            capture_output=True, text=True, timeout=10)
    status = json.loads(result.stdout)
    assert status["applied"] and status["effective_nice"] >= 10
    assert status["autogroup_requested"]
    if status["autogroup_applied"]:
        assert status["autogroup_nice"] >= 10
    else:
        assert status["autogroup_reason"]
    if before is not None:
        assert parent_group.read_text() == before
