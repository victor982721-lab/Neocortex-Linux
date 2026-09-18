"""Derived replay receipts participate in the existing exact reset plan."""
from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.persistence import state_reset as reset
from neocortex.semantic.semantic_source_head_cache import REPLAY_RECEIPT_SUFFIX


def _receipt(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    receipt = state / ("semantic.sqlite3" + REPLAY_RECEIPT_SUFFIX)
    # Corruption does not turn a disposable cache into authoritative content.
    receipt.write_bytes(b"invalid optional receipt")
    receipt.chmod(0o600)
    return state, receipt


@pytest.mark.parametrize("scope", reset.STATE_RESET_SCOPES)
def test_reset_scope_selects_only_the_canonical_derived_receipt(tmp_path: Path, scope):
    state, receipt = _receipt(tmp_path)
    plan = reset.plan_state_reset(state, scope=scope)
    assert receipt not in plan.unmanaged_state_entries
    selected = [target for target in plan.targets if receipt in {entry.path for entry in target.entries}]
    if scope == "runs":
        assert selected == []
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
        assert receipt.read_bytes() == b"invalid optional receipt"
    else:
        assert len(selected) == 1
        assert selected[0].kind == "managed-artifact"
        assert selected[0].owner == "semantic"
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
        assert not receipt.exists()


def test_receipt_change_after_preview_blocks_reset_before_effect(tmp_path: Path):
    state, receipt = _receipt(tmp_path)
    plan = reset.plan_state_reset(state, scope="runs-and-caches")
    receipt.write_bytes(b"a newer receipt")
    with pytest.raises(reset.StateResetConfirmationError, match="exact preview plan digest"):
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
    assert receipt.read_bytes() == b"a newer receipt"


def test_receipt_symlink_is_rejected_without_following_it(tmp_path: Path):
    state, receipt = _receipt(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    receipt.unlink()
    receipt.symlink_to(outside)
    with pytest.raises(reset.StateResetError):
        reset.plan_state_reset(state, scope="all")
    assert outside.read_bytes() == b"preserve"


def test_similarly_named_unknown_file_still_blocks_total_reset(tmp_path: Path):
    state, receipt = _receipt(tmp_path)
    unknown = state / ("another.sqlite3" + REPLAY_RECEIPT_SUFFIX)
    unknown.write_bytes(b"unregistered content")
    unknown.chmod(0o600)
    plan = reset.plan_state_reset(state, scope="all")
    assert unknown in plan.unmanaged_state_entries
    assert plan.inventory.blockers
    with pytest.raises(reset.StateResetError):
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
    assert unknown.read_bytes() == b"unregistered content"
    assert receipt.exists()
