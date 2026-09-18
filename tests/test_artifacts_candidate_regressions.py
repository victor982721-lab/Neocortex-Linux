"""Focused candidate regressions; fixtures only, no host state or mount setup."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from neocortex.api.agent_activity import (
    AgentActivity, AgentActivityChanged, AgentActivityProcessError,
    AgentActivityRecoveryRequired,
    _sealed_workspace_digest as activity_seal,
)
from neocortex.runtime import scratch, scratch_tree
from neocortex.runtime.control.bounded_subprocess import run_bounded_capture


def test_activity_temporaries_use_private_workspace_even_with_env_override(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    activity = AgentActivity.prepare(tmp_path / "state", "private-temp")
    result = activity.run(
        [sys.executable, "-c", "import tempfile,os,json; "
         "p=tempfile.mkdtemp(); print(json.dumps([os.getcwd(),p])); os.rmdir(p)"],
        env={key: str(foreign) for key in ("TMPDIR", "TMP", "TEMP")},
    )
    cwd, temp = json.loads(result.stdout)
    assert Path(cwd) == activity.path
    assert Path(temp).is_relative_to(activity.path)
    with pytest.raises(AgentActivityRecoveryRequired, match="producer quiescence"):
        activity.close()
    assert activity.path.exists()
    assert not tuple(foreign.iterdir())


def test_output_bound_is_enforced_in_bytes_before_decode(tmp_path):
    activity = AgentActivity.prepare(tmp_path / "state", "byte-limit")
    with pytest.raises(AgentActivityProcessError, match="output limit"):
        activity.run([sys.executable, "-c", "import sys; sys.stdout.write('é' * (1 << 20))"])
    assert activity.state == "failed-retained"


def test_invalid_output_bytes_are_losslessly_represented(tmp_path):
    activity = AgentActivity.prepare(tmp_path / "state", "output-bytes")
    result = activity.run([sys.executable, "-c", "import os; os.write(1,b'\\xff')"])
    assert result.stdout.encode("utf-8", errors="surrogateescape") == b"\xff"
    assert len(result.stdout.encode("utf-8", errors="surrogateescape")) <= 1 << 20
    with pytest.raises(AgentActivityRecoveryRequired, match="producer quiescence"):
        activity.close()
    assert activity.path.exists()


def test_activity_timeout_kills_pipe_holding_process_group(tmp_path):
    activity = AgentActivity.prepare(tmp_path / "state", "group-timeout")
    child = ("from pathlib import Path; import time,os; "
             "Path('child.ready').write_text(str(os.getpid())); "
             "time.sleep(5); Path('child.finished').write_text('bad')")
    leader = (f"import subprocess,sys,time; from pathlib import Path; "
              f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
              "\nwhile not Path('child.ready').exists(): time.sleep(.005)"
              "\ntime.sleep(30)")
    started = time.monotonic()
    with pytest.raises(AgentActivityProcessError, match="timed out"):
        activity.run([sys.executable, "-c", leader], timeout=0.4)
    assert time.monotonic() - started < 2
    assert not (activity.path / "child.finished").exists()
    assert activity.state == "failed-retained"
    pid = int((activity.path / "child.ready").read_text())
    proc = Path(f"/proc/{pid}/stat")
    if proc.exists():
        assert proc.read_text().rpartition(")")[2].split()[0] == "Z"


def test_process_registration_failure_reaps_before_propagation(tmp_path):
    observed = []
    def fail_registration(pid, start_ticks):
        observed.append(pid)
        raise RuntimeError("synthetic claim write failure")
    with pytest.raises(RuntimeError, match="claim write failure"):
        run_bounded_capture(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=1, stdout_limit_bytes=32, stderr_limit_bytes=32,
            on_started=fail_registration,
        )
    with pytest.raises(ProcessLookupError):
        os.kill(observed[0], 0)


def test_none_timeout_does_not_set_five_second_execution_limit(tmp_path):
    started = time.monotonic()
    result = run_bounded_capture(
        [sys.executable, "-c", "import time; time.sleep(5.1); print('done')"],
        timeout_seconds=None, stdout_limit_bytes=32, stderr_limit_bytes=32,
    )
    assert time.monotonic() - started >= 5.1
    assert result.stdout == b"done\n"


def test_uncompleted_process_claim_blocks_activity_close(tmp_path):
    activity = AgentActivity.prepare(tmp_path / "state", "external-process")
    activity.associate_process(os.getpid())
    with pytest.raises(AgentActivityRecoveryRequired, match="process claim"):
        activity.close()
    assert activity.state == "active"


def test_prepared_external_process_claim_cannot_bypass_close_guard(tmp_path):
    activity = AgentActivity.prepare(tmp_path / "state", "prepared-process", process_pid=os.getpid())
    with pytest.raises(AgentActivityRecoveryRequired, match="process claim"):
        activity.close()


def test_failure_preserves_pid_identity_in_existing_activity_note(tmp_path):
    activity = AgentActivity.prepare(tmp_path / "state", "failure-identity")
    with pytest.raises(AgentActivityProcessError, match="timed out"):
        activity.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
    from neocortex.api.agent_activity import _read_note
    note = _read_note(activity.record.reason)
    assert type(note["process_pid"]) is int
    assert type(note["process_start_ticks"]) is int
    assert note["process_status"] == "cleanup_unverified"
    assert "timed out" in note["failure"]


@pytest.mark.parametrize("seal", [scratch._sealed_workspace_digest, activity_seal])
def test_nested_control_basename_is_payload_and_invalidates_seal(tmp_path, seal):
    root = tmp_path / "workspace"
    (root / "fixture").mkdir(parents=True, mode=0o700)
    nested = root / "fixture" / "manifest.json"
    nested.write_bytes(b"first")
    before = seal(root)
    nested.write_bytes(b"other")
    assert seal(root) != before


@pytest.mark.parametrize("seal", [scratch._sealed_workspace_digest, activity_seal])
def test_non_utf8_payload_has_exact_byte_identity(tmp_path, seal):
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    fd = os.open(os.fsencode(root) + b"/name-\xff", os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"x")
    os.close(fd)
    original = seal(root)
    os.rename(os.fsencode(root) + b"/name-\xff", os.fsencode(root) + b"/name-\xfe")
    assert seal(root) != original


def test_root_control_is_excluded_but_publication_leftover_is_not(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    (root / "manifest.json").write_bytes(b"control")
    first = scratch._sealed_workspace_digest(root)
    (root / "manifest.json").write_bytes(b"updated control")
    assert scratch._sealed_workspace_digest(root) == first
    (root / ".manifest.json.unowned.tmp").write_bytes(b"payload")
    assert scratch._sealed_workspace_digest(root) != first


def test_valid_unicode_old_seal_format_is_unchanged(tmp_path):
    payload = tmp_path / "ñ.txt"
    payload.write_bytes(b"value")
    info = payload.lstat()
    content = "sha256:" + hashlib.sha256(b"value").hexdigest()
    expected = "sha256:" + hashlib.sha256(
        f"F:ñ.txt:{scratch._identity(info)}:5:{info.st_mtime_ns}:{content}\n".encode()
    ).hexdigest()
    assert scratch._sealed_workspace_digest(tmp_path) == (expected, 1, 5)


def test_canonical_seal_preserves_activity_per_file_limit(tmp_path, monkeypatch):
    from neocortex.api import agent_activity
    (tmp_path / "payload").write_bytes(b"123")
    monkeypatch.setattr(agent_activity, "_MAX_FILE_BYTES", 2)
    with pytest.raises(AgentActivityChanged):
        agent_activity._sealed_workspace_digest(tmp_path)


def test_json_preserves_non_utf8_paths_without_changing_valid_json():
    value = {"path": "ñ/\udcff", "literal": r"\udcff"}
    encoded = scratch._canonical_json(value).encode("utf-8")
    assert json.loads(encoded) == value
    ordinary = {"path": "ñ/file"}
    assert scratch._canonical_json(ordinary) == json.dumps(
        ordinary, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def test_manifest_reader_rejects_size_before_opening_payload(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_bytes(b"x" * (scratch._MAX_MANIFEST_BYTES + 1))
    path.chmod(0o600)
    original_open = os.open
    opened_payload = []
    def observed_open(name, flags, *args, **kwargs):
        if name == path.name:
            opened_payload.append(name)
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(scratch_tree.os, "open", observed_open)
    with pytest.raises(scratch.ScratchManifestError, match="too large"):
        scratch._read_manifest_bytes(path)
    assert opened_payload == []


def test_scratch_member_limit_covers_members_inside_one_record(tmp_path):
    manager = scratch.ScratchManager(tmp_path / "scratch", create_root=True)
    workspace = manager.create(retain_on_success=True)
    for index in range(128):
        (workspace.path / str(index)).write_bytes(b"x")
    workspace.complete(retain=True)
    plan = manager.plan(max_entries=2, max_depth=4, max_bytes=1024, now_ns=10**30)
    assert plan.truncated
    assert "entry_limit" in plan.truncation_reasons
    assert plan.planned == 0
    assert not plan.complete
    assert workspace.path.exists()


def test_size_observation_reports_read_error_and_invalid_record_coverage(tmp_path, monkeypatch):
    manager = scratch.ScratchManager(tmp_path / "scratch", create_root=True)
    workspace = manager.create(retain_on_success=True)
    inner = workspace.path / "inner"
    inner.mkdir()
    (inner / "data").write_bytes(b"x" * 100)
    real_scandir = os.scandir
    def fail_subtree(path):
        same_descriptor = (isinstance(path, int) and
                           os.fstat(path).st_ino == inner.stat().st_ino)
        if same_descriptor or (isinstance(path, (str, Path)) and Path(path) == inner):
            raise PermissionError("synthetic unreadable subtree")
        return real_scandir(path)
    monkeypatch.setattr(scratch.os, "scandir", fail_subtree)
    with pytest.raises(scratch.ScratchSecurityError, match="incomplete"):
        scratch._directory_size(workspace.path)
    plan = manager.plan()
    assert not plan.complete
    assert not plan.records[0].size_complete
    assert not plan.records[0].eligible


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_strict_payload_profile_is_consistent_and_preserves_external(tmp_path, kind):
    outside = tmp_path / "original"
    outside.write_bytes(b"keep")
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    if kind == "fifo":
        os.mkfifo(root / "fixture", 0o600)
    else:
        os.symlink(outside, root / "fixture")
    assert scratch._workspace_payload_issue(root) is not None
    with pytest.raises(scratch.ScratchSecurityError):
        scratch._sealed_workspace_digest(root)
    with pytest.raises(scratch.ScratchSecurityError):
        scratch._remove_tree_no_follow(root)
    assert root.exists()
    assert outside.read_bytes() == b"keep"


def test_mount_id_mismatch_blocks_before_any_effect(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    inner = root / "mounted"
    inner.mkdir(parents=True, mode=0o700)
    (root / "ordinary").write_bytes(b"keep")
    (inner / "external").write_bytes(b"keep")
    real_mount_id = scratch_tree._mount_id
    mount_identity = (inner.stat().st_dev, inner.stat().st_ino)
    def injected_mount_id(fd):
        info = os.fstat(fd)
        return (real_mount_id(fd) + 100000 if (info.st_dev, info.st_ino) == mount_identity
                else real_mount_id(fd))
    monkeypatch.setattr(scratch_tree, "_mount_id", injected_mount_id)
    with pytest.raises(scratch.ScratchSecurityError, match="mount boundary"):
        scratch._remove_tree_no_follow(root)
    assert (root / "ordinary").read_bytes() == b"keep"
    assert (inner / "external").read_bytes() == b"keep"


def test_unavailable_mount_observation_blocks_before_effect(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    (root / "payload").write_bytes(b"keep")
    def unavailable():
        raise scratch_tree.ScratchTreeError("mount topology unavailable")
    monkeypatch.setattr(scratch_tree, "_mount_ids", unavailable)
    with pytest.raises(scratch.ScratchSecurityError, match="mount topology"):
        scratch._remove_tree_no_follow(root)
    assert (root / "payload").read_bytes() == b"keep"


def test_ancestor_symlink_cannot_be_retirement_authority(tmp_path):
    root = tmp_path / "original"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    (workspace / "payload").write_bytes(b"keep")
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises((OSError, scratch.ScratchSecurityError)):
        scratch._remove_tree_no_follow(link / "workspace")
    assert (workspace / "payload").read_bytes() == b"keep"


def test_deep_count_and_retirement_are_iterative(tmp_path):
    import resource
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 1200:
        pytest.skip("descriptor budget below this 1050-level isolated fixture")
    root = tmp_path / "deep"
    root.mkdir(mode=0o700)
    current = root
    directories = []
    try:
        for _ in range(1050):
            current = current / "d"
            current.mkdir(mode=0o700)
            directories.append(current)
        (current / "payload").write_bytes(b"x")
        assert scratch._directory_size(root) == 1
        scratch._remove_tree_no_follow(root)
        assert not root.exists()
    finally:
        if root.exists():
            (current / "payload").unlink(missing_ok=True)
            for directory in reversed(directories):
                directory.rmdir()
            root.rmdir()
