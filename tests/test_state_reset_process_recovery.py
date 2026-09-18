"""Real process death verifies reset receipts, recovery and conflict refusal."""
from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.persistence.state_reset import STATE_RESET_CONFIRMATION, StateResetError, reconcile_state_reset
from neocortex.runtime.artifact_registry import ArtifactRegistry


def _crash(state: Path, phase: str) -> str:
    script = r'''
import os,signal,sys
from pathlib import Path
from neocortex.persistence import state_reset as reset
phase=sys.argv[2]
def kill(): os.kill(os.getpid(),signal.SIGKILL)
if phase=='acquired':
    original=reset.ResetOperation.acquired
    def crash(*args,**kwargs):
        original(*args,**kwargs)
        kill()
    reset.ResetOperation.acquired=crash
elif phase=='copy':
    def crash(*args,**kwargs): kill()
    reset._copy_raw_file=crash
elif phase=='unlink':
    original=reset._delete_entries
    def crash(*args,**kwargs):
        original(*args,**kwargs)
        kill()
    reset._delete_entries=crash
else:
    original=reset._apply_inventory_owner_staged
    def crash(*args,**kwargs):
        original(*args,**kwargs)
        kill()
    reset._apply_inventory_owner_staged=crash
plan=reset.plan_state_reset(Path(sys.argv[1]),scope='all')
reset.apply_state_reset(plan,confirmation=reset.STATE_RESET_CONFIRMATION)
'''
    result = subprocess.run([sys.executable, "-c", script, str(state), phase], capture_output=True, text=True, timeout=30)
    assert result.returncode == -signal.SIGKILL, result.stderr
    records = ArtifactRegistry(state / "artifacts", owner="state-reset").records()
    return next(record.artifact_id for record in records if record.purpose == "state-reset-operation")


@pytest.mark.parametrize("phase", ["acquired", "copy", "unlink", "promotion"])
def test_process_death_has_discoverable_intent_and_safe_reconciliation(tmp_path: Path, phase: str):
    state = tmp_path / "state"
    state.mkdir()
    if phase == "promotion":
        initialize_inventory_schema(state / "dedup.sqlite3")
        with closing(sqlite3.connect(state / "dedup.sqlite3")) as connection, connection:
            connection.execute("INSERT INTO scans(scan_id,root,started_ns,status) VALUES(71,'/fixture',1,'complete')")
        before = (state / "dedup.sqlite3").read_bytes()
    else:
        cache = state / "runtime-cache"
        cache.mkdir()
        (cache / "a").write_bytes(b"first")
        (cache / "b").write_bytes(b"second")
    operation = _crash(state, phase)
    preview = reconcile_state_reset(state, operation)
    assert preview["action"] == ("cleanup-transient" if phase in {"acquired", "copy"} else "restore-verified-rollback")
    applied = reconcile_state_reset(state, operation, apply=True,
        expected_receipt_digest=str(preview["receipt_digest"]), confirmation=STATE_RESET_CONFIRMATION)
    assert applied["status"] == "complete"
    assert not Path(str(preview["storage"])).exists()
    assert reconcile_state_reset(state, operation)["status"] == "no_changes"
    if phase == "promotion":
        assert (state / "dedup.sqlite3").read_bytes() == before
    else:
        assert (state / "runtime-cache" / "a").read_bytes() == b"first"
        assert (state / "runtime-cache" / "b").read_bytes() == b"second"


def test_recovery_refuses_a_personal_change_after_process_death(tmp_path: Path):
    state = tmp_path / "state"
    cache = state / "runtime-cache"
    cache.mkdir(parents=True)
    (cache / "a").write_bytes(b"initial")
    operation = _crash(state, "unlink")
    cache.mkdir()
    (cache / "a").write_bytes(b"new personal content")
    with pytest.raises(StateResetError, match="refuses overwrite"):
        reconcile_state_reset(state, operation)
    assert (cache / "a").read_bytes() == b"new personal content"


def test_recovery_receipt_digest_is_required_and_tampering_is_preserved(tmp_path: Path):
    state = tmp_path / "state"
    cache = state / "runtime-cache"
    cache.mkdir(parents=True)
    (cache / "a").write_bytes(b"initial")
    operation = _crash(state, "copy")
    preview = reconcile_state_reset(state, operation)
    with pytest.raises(StateResetError, match="exact receipt digest"):
        reconcile_state_reset(state, operation, apply=True, expected_receipt_digest="0" * 64,
                              confirmation=STATE_RESET_CONFIRMATION)
    receipt = Path(str(preview["storage"])) / "state-reset-manifest.json"
    payload = json.loads(receipt.read_text())
    payload["status"] = "applied"
    receipt.write_text(json.dumps(payload))
    with pytest.raises(StateResetError, match="digest changed"):
        reconcile_state_reset(state, operation)
    assert receipt.exists() and (cache / "a").read_bytes() == b"initial"


def test_public_cli_reconciles_a_discovered_operation_from_a_new_session(tmp_path: Path, capsys):
    from neocortex.api.cli import human
    state = tmp_path / "state"
    cache = state / "runtime-cache"
    cache.mkdir(parents=True)
    (cache / "original").write_bytes(b"retain")
    operation = _crash(state, "unlink")
    arguments = ("state", "reset", "--state-directory", str(state), "--scope", "all",
                 "--reconcile-operation", operation, "--json")
    assert human.run_human_command(arguments) == 0
    preview = json.loads(capsys.readouterr().out)["result"]
    assert preview["action"] == "restore-verified-rollback"
    assert human.run_human_command((*arguments, "--apply", "--receipt-digest", preview["receipt_digest"],
                                   "--confirm-state-reset", STATE_RESET_CONFIRMATION)) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["status"] == "complete"
    assert (cache / "original").read_bytes() == b"retain"


def test_reconciliation_interrupted_after_link_retires_only_its_exact_temporary(tmp_path: Path):
    state = tmp_path / "state"
    cache = state / "runtime-cache"
    cache.mkdir(parents=True)
    (cache / "a").write_bytes(b"original evidence")
    operation = _crash(state, "unlink")
    preview = reconcile_state_reset(state, operation)
    script = r'''
import os,signal,sys
from neocortex.persistence.state_reset import reconcile_state_reset,STATE_RESET_CONFIRMATION
link=os.link
def crash(*args,**kwargs):
    result=link(*args,**kwargs)
    if args[1]=='a': os.kill(os.getpid(),signal.SIGKILL)
    return result
os.link=crash
reconcile_state_reset(sys.argv[1],sys.argv[2],apply=True,expected_receipt_digest=sys.argv[3],confirmation=STATE_RESET_CONFIRMATION)
'''
    crashed = subprocess.run([sys.executable, "-c", script, str(state), operation, preview["receipt_digest"]],
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    replay = reconcile_state_reset(state, operation)
    result = reconcile_state_reset(state, operation, apply=True,
                                   expected_receipt_digest=replay["receipt_digest"], confirmation=STATE_RESET_CONFIRMATION)
    assert result["status"] == "complete"
    assert sorted(item.name for item in cache.iterdir()) == ["a"]
    assert (cache / "a").read_bytes() == b"original evidence"
    assert (cache / "a").stat().st_nlink == 1


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("after_compensation", [False, True])
def test_crashed_registered_retirement_restores_bytes_and_owner_claim(tmp_path: Path, kind: str, after_compensation: bool):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    artifact = state / "claimed-cache"
    if kind == "directory":
        artifact.mkdir(mode=0o700)
        payload = artifact / "content"
    else:
        payload = artifact
    payload.write_bytes(b"registered original")
    payload.chmod(0o600)
    registry = ArtifactRegistry(state / "artifacts", owner="fixture", create_root=True)
    registry.register("fixture-cache", producer="fixture", path=artifact, root=registry.root,
                      purpose="fixture-cache", state="completed", disposable=True)
    operation = _crash(state, "unlink")
    if after_compensation:
        script = r'''
import os,signal,sys
from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.persistence.state_reset import reconcile_state_reset,STATE_RESET_CONFIRMATION
original=ArtifactRegistry._write_existing_registration
def crash(self,path,payload):
    result=original(self,path,payload)
    if payload.get('metadata',{}).get('retirement_compensation_receipt'):
        os.kill(os.getpid(),signal.SIGKILL)
    return result
ArtifactRegistry._write_existing_registration=crash
preview=reconcile_state_reset(sys.argv[1],sys.argv[2])
reconcile_state_reset(sys.argv[1],sys.argv[2],apply=True,expected_receipt_digest=preview['receipt_digest'],confirmation=STATE_RESET_CONFIRMATION)
'''
        crashed = subprocess.run([sys.executable, "-c", script, str(state), operation],
                                 capture_output=True, text=True, timeout=30)
        assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    preview = reconcile_state_reset(state, operation)
    result = reconcile_state_reset(state, operation, apply=True,
                                   expected_receipt_digest=preview["receipt_digest"], confirmation=STATE_RESET_CONFIRMATION)
    assert result["status"] == "complete"
    assert payload.read_bytes() == b"registered original"
    restored = registry.verify("fixture-cache")
    assert restored.verified and restored.state == "completed"
    assert restored.metadata["retirement_compensation_receipt"]["recovery_artifact_id"] == operation
    assert reconcile_state_reset(state, operation)["status"] == "no_changes"
