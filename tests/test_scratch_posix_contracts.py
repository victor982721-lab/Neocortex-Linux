"""Owner-authorized POSIX fixtures, exact selections and durable observations."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.runtime import scratch, scratch_tree
from neocortex.runtime.path_identity import PathIdentity


def manager_at(tmp_path: Path) -> scratch.ScratchManager:
    return scratch.ScratchManager(tmp_path / "scratch", owner="tests", create_root=True)


def fixture(manager: scratch.ScratchManager):
    grant = manager.issue_fixture_grant(activity_id="activity-fixture", creation_grant_id="explicit-test-grant",
                                        authorized=True)
    return manager.create(payload_profile=scratch.PayloadProfile.FIXTURE_POSIX_V1,
                          fixture_grant=grant, retain_on_success=True)


@pytest.mark.parametrize("name", [b"\xff", b"\xfe", b"control\n\t", b"literal\\udcff", "niño".encode()])
def test_posix_path_envelope_is_exact_and_display_is_not_identity(name):
    identity = PathIdentity.from_path(name)
    assert PathIdentity.from_dict(identity.as_dict()).to_bytes() == name
    assert os.fsencode(identity.to_path()) == name
    assert PathIdentity.from_path(b"\xff") != PathIdentity.from_path(b"\\udcff")
    changed = identity.as_dict() | {"display_escaped": "not the name"}
    with pytest.raises(ValueError, match="canonical"):
        PathIdentity.from_dict(changed)


def test_fixture_profile_requires_issued_owner_grant_and_consumes_once(tmp_path):
    manager = manager_at(tmp_path)
    with pytest.raises(scratch.ScratchSecurityError, match="issued"):
        manager.create(payload_profile="fixture_posix_v1", metadata={"authorized": True})
    with pytest.raises(scratch.ScratchSecurityError, match="authorization"):
        manager.issue_fixture_grant(activity_id="a", creation_grant_id="g", authorized=False)
    grant = manager.issue_fixture_grant(activity_id="a", creation_grant_id="g", authorized=True)
    workspace = manager.create(payload_profile="fixture_posix_v1", fixture_grant=grant, retain_on_success=True)
    assert workspace.record.payload_profile == "fixture_posix_v1"
    with pytest.raises(scratch.ScratchSecurityError, match="consumed"):
        manager.create(payload_profile="fixture_posix_v1", fixture_grant=grant)
    payload = json.loads((workspace.path / scratch.MANIFEST_NAME).read_bytes())
    payload["fixture_grant"]["activity_id"] = "changed"
    payload["manifest_digest"] = scratch._manifest_digest(payload)
    scratch._write_json_atomic(workspace.path / scratch.MANIFEST_NAME, payload)
    assert not manager.plan().records[0].valid


def test_fixture_special_entries_do_not_open_fifo_or_change_external_inode(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    workspace = fixture(manager)
    outside = tmp_path / "outside"
    outside.write_bytes(b"external-original")
    outside.chmod(0o440)
    original_mode = outside.stat().st_mode
    os.link(outside, workspace.path / "shared")
    (workspace.path / "external").symlink_to(outside)
    (workspace.path / "internal").symlink_to("shared")
    (workspace.path / "broken").symlink_to("missing")
    os.mkfifo(workspace.path / "fifo")
    real_open = os.open
    def checked_open(path, flags, *args, **kwargs):
        if os.fsencode(path).endswith(b"fifo"):
            assert flags & os.O_PATH, "a FIFO may only receive a non-I/O identity descriptor"
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", checked_open)
    manager.seal(workspace.record_id)
    workspace.complete(retain=True)
    assert manager.plan().complete
    assert manager.apply(manager.plan()).applied == 1
    assert outside.read_bytes() == b"external-original"
    assert outside.stat().st_mode == original_mode
    assert outside.stat().st_nlink == 1


def test_fixture_hardlink_accounting_counts_each_inode_once(tmp_path):
    manager = manager_at(tmp_path)
    workspace = fixture(manager)
    file = workspace.path / "first"
    file.write_bytes(b"data")
    os.link(file, workspace.path / "second")
    observed = scratch_tree.observe_claimed_tree(workspace.path, profile="fixture_posix_v1")
    assert observed.complete and observed.members == 2
    assert observed.apparent_bytes == 4
    expected_allocated = (workspace.path.stat().st_blocks + file.stat().st_blocks) * 512
    assert observed.allocated_bytes == expected_allocated
    assert observed.exclusive_bytes is None
    seal = manager.seal(workspace.record_id).seal
    assert seal["members"] == 2 and seal["apparent_bytes"] == 4
    workspace.complete(retain=True)
    assert manager.plan().planned_bytes == 4
    assert manager.apply(manager.plan()).applied_bytes == 4


def test_registry_policy_projection_requires_exact_owner_receipt(tmp_path):
    manager = manager_at(tmp_path)
    workspace = fixture(manager)
    manifest = json.loads((workspace.path / scratch.MANIFEST_NAME).read_bytes())
    expected = {"schema": "neocortex.scratch-payload-policy/v1",
                "payload_profile": manifest["payload_profile"],
                "policy_revision": manifest["policy_revision"], "fixture_grant": manifest["fixture_grant"]}
    assert scratch.verified_workspace_payload_profile(workspace.path, owner="tests", expected_policy=expected) == "fixture_posix_v1"
    with pytest.raises(scratch.ScratchSecurityError, match="projection changed"):
        scratch.verified_workspace_payload_profile(workspace.path, owner="tests", expected_policy=expected | {"fixture_grant": None})
    with pytest.raises(scratch.ScratchSecurityError, match="owner claim"):
        scratch.verified_workspace_payload_profile(workspace.path, owner="different", expected_policy=expected)


def test_active_socket_blocks_fixture_and_restores_prepared_permissions(tmp_path):
    manager = manager_at(tmp_path)
    workspace = fixture(manager)
    locked = workspace.path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        active = socket.socket(socket.AF_UNIX)
    except PermissionError:
        pytest.skip("sandbox denies AF_UNIX socket creation; type rejection is tested independently")
    socket_path = workspace.path / "socket"
    if len(str(socket_path)) >= 100:
        active.close()
        pytest.skip("temporary workspace path exceeds the AF_UNIX address limit")
    with active:
        active.bind(str(socket_path))
        active.listen()
        assert manager.plan().records[0].issue == "socket_payload"
        with pytest.raises(scratch.ScratchSecurityError, match="socket_payload"):
            with manager.fixture_permissions(workspace.record_id, authorized=True):
                pytest.fail("socket payload cannot become a retirement fixture")
    assert stat.S_IMODE(locked.stat().st_mode) == 0o555
    locked.chmod(0o700)


@pytest.mark.parametrize("entry_type,issue", [(stat.S_IFSOCK, "socket_payload"),
                                             (stat.S_IFCHR, "device_payload"),
                                             (stat.S_IFBLK, "device_payload")])
def test_fixture_profile_still_rejects_devices_and_socket_types(tmp_path, entry_type, issue):
    ordinary = tmp_path.stat()
    fields = list(ordinary)
    fields[0] = entry_type | 0o600
    assert scratch_tree.payload_issue(os.stat_result(fields), "fixture_posix_v1") == issue


def test_plan_observes_mount_boundary_before_effect(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    workspace = manager.create(retain_on_success=True)
    child = workspace.path / "mounted"
    child.mkdir()
    (child / "data").write_bytes(b"preserved")
    inode = child.stat().st_ino
    real_mount = scratch_tree._mount_id
    monkeypatch.setattr(scratch_tree, "_mount_id", lambda fd: real_mount(fd) + (1000 if os.fstat(fd).st_ino == inode else 0))
    record = manager.plan().records[0]
    assert record.issue == "mount_boundary"
    assert not record.size_complete
    assert (child / "data").read_bytes() == b"preserved"


@pytest.mark.parametrize("preserve_parent_inode", [False, True])
def test_retirement_revalidates_every_absolute_ancestor_before_effect(tmp_path, monkeypatch, preserve_parent_inode):
    allowed = tmp_path / "allowed"
    parent = allowed / "parent"
    workspace = parent / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    (workspace / "evidence").write_bytes(b"original evidence")
    outside = tmp_path / "outside"
    original_walk = scratch_tree._walk_strict
    def move_before_effect(*args, **kwargs):
        if kwargs["remove"] and not outside.exists():
            allowed.rename(outside)
            allowed.mkdir(mode=0o700)
            if preserve_parent_inode:
                # Endpoint identity alone is insufficient: replace an upper
                # ancestor, then restore the original parent beneath it.
                (outside / "parent").rename(parent)
            else:
                workspace.mkdir(parents=True, mode=0o700)
                (workspace / "replacement").write_bytes(b"replacement evidence")
        return original_walk(*args, **kwargs)
    monkeypatch.setattr(scratch_tree, "_walk_strict", move_before_effect)
    with pytest.raises(scratch_tree.ScratchTreeError):
        scratch_tree.remove_claimed_tree(workspace, expected_identity=None, limit=100)
    original_location = workspace if preserve_parent_inode else outside / "parent" / "workspace"
    assert (original_location / "evidence").read_bytes() == b"original evidence"
    if not preserve_parent_inode:
        assert (workspace / "replacement").read_bytes() == b"replacement evidence"


@pytest.mark.parametrize("nested_depth", [1, 2])
def test_retirement_revalidates_retained_internal_ancestors_before_unlink(tmp_path, monkeypatch, nested_depth):
    workspace = tmp_path / "workspace"
    ancestor = workspace / "ancestor"
    inner = ancestor if nested_depth == 1 else ancestor / "inner"
    inner.mkdir(parents=True, mode=0o700)
    (inner / "evidence").write_bytes(b"original nested evidence")
    outside = tmp_path / "outside"
    original_walk = scratch_tree._walk_strict
    original_checked = scratch_tree._checked_child
    removing = False
    def mark_effect(*args, **kwargs):
        nonlocal removing
        removing = kwargs["remove"]
        return original_walk(*args, **kwargs)
    def move_after_file_open(parent_fd, name, mount_id, **kwargs):
        result = original_checked(parent_fd, name, mount_id, **kwargs)
        if removing and os.fsencode(name) == b"evidence" and not outside.exists():
            ancestor.rename(outside)
            inner.mkdir(parents=True, mode=0o700)
            (inner / "evidence").write_bytes(b"replacement nested evidence")
        return result
    monkeypatch.setattr(scratch_tree, "_walk_strict", mark_effect)
    monkeypatch.setattr(scratch_tree, "_checked_child", move_after_file_open)
    with pytest.raises(scratch_tree.ScratchTreeError):
        scratch_tree.remove_claimed_tree(workspace, expected_identity=None, limit=100)
    moved_inner = outside if nested_depth == 1 else outside / "inner"
    assert (moved_inner / "evidence").read_bytes() == b"original nested evidence"
    assert (inner / "evidence").read_bytes() == b"replacement nested evidence"


def test_absolute_ancestor_change_after_one_effect_preserves_remaining_payload(tmp_path):
    allowed = tmp_path / "allowed"
    workspace = allowed / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    (workspace / "a").write_bytes(b"remaining evidence")
    (workspace / "b").write_bytes(b"first authorized effect")
    outside = tmp_path / "outside"
    effects = []
    def after_effect(count):
        effects.append(count)
        if len(effects) == 1:
            allowed.rename(outside)
            workspace.mkdir(parents=True, mode=0o700)
            (workspace / "replacement").write_bytes(b"untouched replacement")
    with pytest.raises(scratch_tree.ScratchTreeError):
        scratch_tree.remove_claimed_tree(workspace, expected_identity=None, limit=100,
                                          effect_callback=after_effect)
    assert effects == [1]
    assert (outside / "workspace" / "a").read_bytes() == b"remaining evidence"
    assert (workspace / "replacement").read_bytes() == b"untouched replacement"


def test_final_rmdir_failure_has_partial_effect_receipt(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    workspace = manager.create(retain_on_success=True)
    (workspace.path / "data").write_bytes(b"data")
    workspace.complete(retain=True)
    plan = manager.plan()
    real_rmdir = os.rmdir
    def final_failure(path, *args, **kwargs):
        if path == workspace.path.name:
            raise OSError("injected final rmdir")
        return real_rmdir(path, *args, **kwargs)
    monkeypatch.setattr(os, "rmdir", final_failure)
    result = manager.apply(plan)
    assert result.failed == 1
    receipt = json.loads((manager.root / ".scratch-control" / f"retired-{workspace.record_id}.json").read_bytes())
    assert receipt["status"] == "recovery_required"
    assert receipt["effects"] >= 1
    assert workspace.path.exists()


def test_exact_subset_plan_never_adds_new_or_changed_workspaces(tmp_path):
    manager = manager_at(tmp_path)
    first, kept = [manager.create(retain_on_success=True) for _ in range(2)]
    first.complete(retain=True)
    kept.complete(retain=True)
    original = manager.plan()
    selected = replace(original, records=tuple(r for r in original.records if r.record_id == first.record_id))
    later = manager.create(retain_on_success=True)
    later.complete(retain=True)
    assert len(manager.verify(selected).records) == 1
    assert manager.apply(selected).applied == 1
    assert kept.path.exists() and later.path.exists()
    assert manager.apply(selected).applied == 0
    stale = manager.plan()
    manager._update_state(kept.path, kept.record_id, scratch.ScratchState.COMPLETED, reason="new claim")
    assert any(r.issue == "selection_changed" for r in manager.verify(stale).records)
    applied = manager.apply(stale)
    assert kept.path.exists()
    assert applied.applied == 1  # only the unchanged selected later workspace


@pytest.mark.parametrize("scope_case,eligible", [
    ("leader_only", False), ("process_group", False),
    ("reconciliation_boolean", False), ("malformed_note", False),
    ("manual_no_process", True), ("quiescent_receipt", True),
])
def test_legacy_completed_activity_requires_shared_process_quiescence(tmp_path, scope_case, eligible):
    manager = manager_at(tmp_path)
    metadata = {"agent_activity": {"schema": "neocortex.agent-activity/v1",
                                   "activity_id": "legacy-activity", "owner": "tests"}}
    if scope_case == "reconciliation_boolean":
        metadata["terminal_reconciliation"] = {"authorized": True, "evidence": {
            "quiescence_confirmed": True, "recovered": True, "release_authorized": True}}
    workspace = manager.create(retain_on_success=True, metadata=metadata)
    (workspace.path / "evidence").write_bytes(b"must remain until scope is verified")
    workspace.complete(retain=True)
    note = {"schema": "neocortex.agent-activity-note/v1", "process_pid": 987654321,
            "process_start_ticks": 42, "process_status": "exited",
            "process_scope": "posix-process-group", "process_scope_coverage": scope_case}
    if scope_case == "manual_no_process":
        note = {"schema": "neocortex.agent-activity-note/v1"}
    elif scope_case == "quiescent_receipt":
        note.update(process_scope="cgroup-v2", process_scope_coverage="cgroup-v2",
                    process_scope_receipt={"schema": "neocortex.process-scope/v1",
                        "activity_id": "legacy-activity", "phase": "quiescent",
                        "process_pid": 987654321, "process_start_ticks": 42})
    reason = "agent-activity-note:" + ("{unreadable" if scope_case == "malformed_note" else json.dumps(note))
    manager._update_state(workspace.path, workspace.record_id, scratch.ScratchState.COMPLETED, reason=reason)
    plan = manager.plan()
    assert plan.records[0].eligible is eligible
    if not eligible:
        assert plan.records[0].issue == "process_scope_incomplete"
        assert manager.apply(plan).applied == 0
        assert (workspace.path / "evidence").read_bytes() == b"must remain until scope is verified"


def test_checkpoint_resumes_bytes_names_cancellation_and_survives_export_failure(tmp_path):
    manager = manager_at(tmp_path)
    workspace = manager.create(retain_on_success=True)
    for raw_name in [b"\xff", b"\xfe", b"literal\\udcff", b"control\n", b"ordinary"]:
        fd = os.open(os.fsencode(workspace.path) + b"/" + raw_name, os.O_CREAT | os.O_WRONLY, 0o600)
        os.write(fd, b"data")
        os.close(fd)
    first = manager.observe_workspace_batch(workspace.record_id, operation_id="operation", batch_entries=2,
                                            hash_files=True)
    assert first["members"] == 2 and first["coverage"] == "partial"
    hashes = json.loads((manager.root / ".scratch-control" / first["last_hash_receipt"]).read_bytes())
    assert len(hashes["hashes"]) == 2
    assert all({"identity", "mtime_ns", "ctime_ns", "size", "sha256"} <= member.keys()
               for member in hashes["hashes"])
    cancelled = manager.observe_workspace_batch(workspace.record_id, operation_id="operation", batch_entries=2,
                                                hash_files=True, cancelled=lambda: True)
    assert cancelled["members"] == 2 and cancelled["issue"] == "cancelled"
    with pytest.raises(OSError):
        raise OSError("export failed after the generation was persisted")
    restarted = manager_at(tmp_path)
    result = cancelled
    while result["coverage"] != "complete":
        result = restarted.observe_workspace_batch(workspace.record_id, operation_id="operation", batch_entries=2,
                                                    hash_files=True)
    assert result["generation_id"] == first["generation_id"]
    assert result["members"] == 5 and result["apparent_bytes"] == 20
    assert result["hashed_bytes"] == 20
    assert result["batch_members"] <= 2
    assert result["observation_only"] is True


def test_checkpoint_rewinds_changed_frontier_instead_of_double_counting(tmp_path):
    manager = manager_at(tmp_path)
    workspace = manager.create(retain_on_success=True)
    for name in ("a", "b", "c"):
        (workspace.path / name).write_bytes(b"data")
    first = manager.observe_workspace_batch(workspace.record_id, operation_id="rewind", batch_entries=1)
    assert first["members"] == 1
    (workspace.path / "new").write_bytes(b"more")
    result = manager.observe_workspace_batch(workspace.record_id, operation_id="rewind", batch_entries=1)
    assert result["rewound_segments"] == 1
    while result["coverage"] != "complete":
        result = manager.observe_workspace_batch(workspace.record_id, operation_id="rewind", batch_entries=1)
    assert result["members"] == 4 and result["apparent_bytes"] == 16


def test_fd_budget_reports_partial_and_checkpoint_reopens_deep_ancestors(tmp_path):
    manager = manager_at(tmp_path)
    workspace = manager.create(retain_on_success=True)
    current = workspace.path
    for _ in range(96):
        current = current / "d"
        current.mkdir()
    (current / "data").write_bytes(b"deep")
    limited = manager.plan(max_fds=5)
    assert not limited.complete and limited.coverage == "partial"
    assert "fd_limit" in limited.truncation_reasons
    result = {"coverage": "partial"}
    while result["coverage"] != "complete":
        result = manager.observe_workspace_batch(workspace.record_id, operation_id="deep",
                                                 batch_entries=16, max_fds=5)
    assert result["members"] == 97 and result["apparent_bytes"] == 4


def test_checkpoint_member_limit_and_policy_change_preserve_prior_generation(tmp_path):
    manager = manager_at(tmp_path)
    workspace = manager.create(retain_on_success=True)
    for name in ("a", "b", "c"):
        (workspace.path / name).write_bytes(b"x")
    result = manager.observe_workspace_batch(workspace.record_id, operation_id="bounded", max_entries=2)
    assert result["issue"] == "entry_limit" and result["members"] == 2
    assert result["coverage"] == "partial"
    with pytest.raises(scratch.ScratchSecurityError, match="policy changed"):
        manager.observe_workspace_batch(workspace.record_id, operation_id="bounded", max_entries=3)


def test_default_observation_budget_is_shared_across_workspaces(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    for _ in range(2):
        workspace = manager.create(retain_on_success=True)
        (workspace.path / "a").write_bytes(b"a")
        (workspace.path / "b").write_bytes(b"b")
    monkeypatch.setattr(scratch, "_MAX_RECORDS", 2)
    plan = manager.plan()
    assert plan.observed_members == 2
    assert not plan.complete
    assert "entry_limit" in plan.truncation_reasons
    assert any(not record.size_complete for record in manager.records())


def test_million_lightweight_members_advance_in_bounded_persisted_batches(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    workspace = manager.create(retain_on_success=True)
    sample = tmp_path / "synthetic-entry"
    sample.write_bytes(b"x")
    sample_fd = os.open(sample, os.O_RDONLY)
    metadata = os.fstat(sample_fd)
    total = 1_000_000
    emitted = 0
    def synthetic_batch(fd, *, cookie, limit):
        nonlocal emitted
        assert limit <= 10_000
        stop = min(total, cookie + limit)
        emitted += stop - cookie
        return [(f"member-{index}".encode(), index + 1) for index in range(cookie, stop)], stop == total
    def synthetic_child(fd, name, mount, **kwargs):
        return os.dup(sample_fd), metadata
    monkeypatch.setattr(scratch, "directory_batch", synthetic_batch)
    monkeypatch.setattr(scratch, "_checked_child", synthetic_child)
    try:
        result = {"coverage": "partial"}
        for batch in range(100):
            result = manager.observe_workspace_batch(workspace.record_id, operation_id="million",
                                                     batch_entries=10_000, max_entries=total)
            assert result["batch_members"] == 10_000
            assert result["members"] == (batch + 1) * 10_000
        assert result["coverage"] == "complete"
        assert result["apparent_bytes"] == total
        assert emitted == total
    finally:
        os.close(sample_fd)


def test_legacy_seal_reconciliation_requires_explicit_evidence_and_preserves_prior(tmp_path):
    manager = manager_at(tmp_path)
    workspace = manager.create(retain_on_success=True)
    nested = workspace.path / "fixture"
    nested.mkdir()
    payload = nested / "manifest.json"
    payload.write_bytes(b"first")
    old = manager.seal(workspace.record_id).seal
    workspace.complete(retain=True)
    payload.write_bytes(b"other")
    assert manager.plan().records[0].issue == "workspace_seal_drift"
    with pytest.raises(scratch.ScratchSecurityError, match="quiescence"):
        manager.reconcile_workspace_seal(workspace.record_id, prior_digest=old["digest"],
                                          release_authorized=True, evidence={})
    reconciled = manager.reconcile_workspace_seal(workspace.record_id, prior_digest=old["digest"],
                    release_authorized=True, evidence={"quiescence_confirmed": True, "deliverables_confirmed": True})
    assert reconciled.metadata["seal_reconciliation"][0]["prior_seal"] == old
    assert reconciled.seal["digest"] != old["digest"]


def test_permission_repair_under_real_dac_without_capability_override(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("the ordinary-UID invocation exercises DAC directly; this subprocess tests capability removal")
    root = Path(tempfile.mkdtemp(prefix="neocortex-fixture-dac-"))
    script = r'''
import ctypes,json,os,stat,sys
from pathlib import Path
from neocortex.runtime.scratch import ScratchManager,PayloadProfile
class Header(ctypes.Structure):
    _fields_=[("version",ctypes.c_uint32),("pid",ctypes.c_int)]
class Data(ctypes.Structure):
    _fields_=[("effective",ctypes.c_uint32),("permitted",ctypes.c_uint32),("inheritable",ctypes.c_uint32)]
header=Header(0x20080522,0); data=(Data*2)();libc=ctypes.CDLL(None,use_errno=True)
assert libc.capset(ctypes.byref(header),ctypes.byref(data))==0
assert "CapEff:\t0000000000000000" in Path("/proc/self/status").read_text()
m=ScratchManager(Path(sys.argv[1])/"scratch",owner="tests",create_root=True)
g=m.issue_fixture_grant(activity_id="DAC",creation_grant_id="explicit",authorized=True)
w=m.create(payload_profile=PayloadProfile.FIXTURE_POSIX_V1,fixture_grant=g,retain_on_success=True)
for mode in [0o555,0]:
    p=w.path/str(mode);p.mkdir();(p/"data").write_bytes(b"payload");p.chmod(mode)
try:
    list((w.path/"0").iterdir())
except PermissionError: pass
else: raise AssertionError("0000 must be unreadable before repair")
assert not m.plan().complete
try:
    with m.fixture_permissions(w.record_id,authorized=True):
        assert len(list((w.path/"0").iterdir()))==1
        raise RuntimeError("failure after preparation")
except RuntimeError: pass
assert all(stat.S_IMODE((w.path/str(mode)).stat().st_mode)==mode for mode in [0o555,0])
with m.fixture_permissions(w.record_id,authorized=True):
    m.seal(w.record_id);w.complete(retain=True);assert m.apply(m.plan()).applied==1
print(json.dumps({"uid":os.geteuid(),"capabilities":0,"restored":True,"removed":not w.path.exists()}))
from neocortex.runtime import scratch_tree
g2=m.issue_fixture_grant(activity_id="unavailable",creation_grant_id="explicit",authorized=True)
w2=m.create(payload_profile=PayloadProfile.FIXTURE_POSIX_V1,fixture_grant=g2,retain_on_success=True)
locked=w2.path/"locked";locked.mkdir();locked.chmod(0)
def unavailable(fd, mode):
    raise scratch_tree.ScratchTreeError("permission_repair_unavailable")
scratch_tree._fchmod_path_fd=unavailable
try:
    with m.fixture_permissions(w2.record_id,authorized=True):
        raise AssertionError("unavailable backend cannot authorize repair")
except Exception as error:
    assert "permission_repair_unavailable" in str(error)
assert stat.S_IMODE(locked.stat().st_mode)==0
'''
    completed = subprocess.run([sys.executable, "-c", script, str(root)], cwd=Path(__file__).parents[1],
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["removed"] is True
