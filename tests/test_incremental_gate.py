"""Ordering and fail-closed contracts for the shared incremental gate."""
# region [00] Contexto del módulo
# Módulo: tests/test_incremental_gate.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import InventoryError
from neocortex.runtime.control.incremental_gate import (
    IncrementalGateDecision,
    IncrementalGateRequest,
    evaluate_incremental_gate,
)
# endregion [01]

# region [02] Implementación


_ROOT = Path("C:/fixture/corpus")
_IDENTITY = (11, 22, 33)
_CURSOR = JournalCursor("C:", 7, 100)


@dataclass(frozen=True, slots=True)
class _Policy:
    mode: str = "normal"
    root: Path = _ROOT
    root_device_id: int | None = _IDENTITY[0]
    root_file_id: int | None = _IDENTITY[1]
    root_birthtime_ns: int | None = _IDENTITY[2]


@dataclass(frozen=True, slots=True)
class _Guard:
    policy: _Policy


@dataclass(frozen=True, slots=True)
class _Owner:
    run_id: int = 41
    scan_id: int = 71
    end_cursor: JournalCursor | None = _CURSOR


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    scan_id: int = 71
    volume: str = "C:"
    journal_id: int = 7
    next_usn: int = 100
    valid: bool = True
    inventory_policy_signature: str | None = "raw-policy"


@dataclass(frozen=True, slots=True)
class _Summary:
    root: str = str(_ROOT)
    files_seen: int = 3


_DEFAULT_POLICY = _Policy()
_DEFAULT_OWNER = _Owner()
_DEFAULT_CHECKPOINT = _Checkpoint()
_DEFAULT_SUMMARY = _Summary()


class _State:
    def __init__(
        self,
        trace: list[str],
        *,
        owner: _Owner | None = _DEFAULT_OWNER,
        policy: _Policy = _DEFAULT_POLICY,
        guard_error: BaseException | None = None,
    ) -> None:
        self._trace = trace
        self._owner = owner
        self._policy = policy
        self._guard_error = guard_error

    def latest_durable_inventory_binding(
        self,
        root: Path,
        *,
        corpus_access_mode: str | None = None,
        inventory_policy_signature: str | None = None,
    ) -> _Owner | None:
        self._trace.append("newest_owner")
        assert root == _ROOT
        assert corpus_access_mode == "normal"
        assert inventory_policy_signature == "effective-policy"
        return self._owner

    def corpus_mutation_guard(self, run_id: int) -> _Guard:
        self._trace.append("persisted_guard")
        assert run_id == 41
        if self._guard_error is not None:
            raise self._guard_error
        return _Guard(self._policy)


class _Inventory:
    def __init__(
        self,
        trace: list[str],
        *,
        checkpoint: _Checkpoint | None = _DEFAULT_CHECKPOINT,
        scan_error: bool = False,
        summary: _Summary = _DEFAULT_SUMMARY,
        scan_identity: tuple[int, int, int] = _IDENTITY,
        file_count: int = 3,
    ) -> None:
        self._trace = trace
        self._checkpoint = checkpoint
        self._scan_error = scan_error
        self._summary = summary
        self._scan_identity = scan_identity
        self._file_count = file_count

    def inventory_checkpoint(self, root: str | Path) -> _Checkpoint | None:
        self._trace.append("raw_checkpoint")
        assert Path(root) == _ROOT
        return self._checkpoint

    def require_scan_inventory_policy_signature(
        self,
        scan_id: int,
        expected_signature: str,
    ) -> None:
        self._trace.append("scan_signature")
        assert (scan_id, expected_signature) == (71, "raw-policy")
        if self._scan_error:
            raise InventoryError("fixture scan signature mismatch")

    def scan_summary(self, scan_id: int) -> _Summary:
        self._trace.append("scan_summary")
        assert scan_id == 71
        return self._summary

    def scan_root_identity(self, scan_id: int) -> tuple[int, int, int]:
        self._trace.append("scan_identity")
        assert scan_id == 71
        return self._scan_identity

    def file_count(self, scan_id: int) -> int:
        self._trace.append("scan_count")
        assert scan_id == 71
        return self._file_count


def _request(trace: list[str]) -> IncrementalGateRequest:
    return IncrementalGateRequest.from_access_policy(
        _Policy(),
        framework_policy_signature="effective-policy",
        inventory_policy_signature="raw-policy",
        journal_before=_CURSOR,
        verify_final=lambda: trace.append("verify_final"),
    )


def test_gate_reads_each_owner_in_order_and_verifies_last() -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(trace),
        inventory=_Inventory(trace),
    )

    assert decision == IncrementalGateDecision(
        True,
        "latest_durable_checkpoint_match",
        41,
    )
    assert trace == [
        "newest_owner",
        "persisted_guard",
        "raw_checkpoint",
        "scan_signature",
        "scan_summary",
        "scan_identity",
        "scan_count",
        "verify_final",
    ]


def test_gate_checks_persisted_guard_before_a_missing_durable_cursor() -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(
            trace,
            owner=_Owner(end_cursor=None),
            guard_error=PermissionError("fixture guard failure"),
        ),
        inventory=_Inventory(trace),
    )

    assert decision.reason == "durable_policy_not_reusable"
    assert decision.source_run_id == 41
    assert trace == ["newest_owner", "persisted_guard"]


def test_gate_checks_checkpoint_before_a_missing_durable_cursor() -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(trace, owner=_Owner(end_cursor=None)),
        inventory=_Inventory(trace, checkpoint=None),
    )

    assert decision.reason == "missing_checkpoint"
    assert trace == ["newest_owner", "persisted_guard", "raw_checkpoint"]


def test_gate_reports_a_missing_durable_cursor_after_checkpoint_validation() -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(trace, owner=_Owner(end_cursor=None)),
        inventory=_Inventory(trace),
    )

    assert decision.reason == "durable_cursor_missing"
    assert trace == ["newest_owner", "persisted_guard", "raw_checkpoint"]


def test_gate_checks_cursor_before_loading_scan_evidence() -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(trace),
        inventory=_Inventory(trace, checkpoint=_Checkpoint(next_usn=99)),
    )

    assert decision.reason == "checkpoint_not_at_durable_boundary"
    assert "scan_signature" not in trace
    assert "verify_final" not in trace


def test_gate_rejects_a_live_cursor_from_another_journal() -> None:
    trace: list[str] = []
    request = IncrementalGateRequest.from_access_policy(
        _Policy(),
        framework_policy_signature="effective-policy",
        inventory_policy_signature="raw-policy",
        journal_before=JournalCursor("C:", 8, 100),
        verify_final=lambda: trace.append("verify_final"),
    )

    decision = evaluate_incremental_gate(
        request,
        state=_State(trace),
        inventory=_Inventory(trace),
    )

    assert decision.reason == "live_cursor_incompatible"
    assert "scan_signature" not in trace


def test_gate_rejects_a_replaced_persisted_root_before_checkpoint_read() -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(trace, policy=_Policy(root_file_id=999)),
        inventory=_Inventory(trace),
    )

    assert decision.reason == "durable_root_identity_mismatch"
    assert trace == ["newest_owner", "persisted_guard"]


def test_gate_abstains_on_scan_failure_without_final_verification() -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(trace),
        inventory=_Inventory(trace, scan_error=True),
    )

    assert decision.reason == "checkpoint_scan_not_reusable"
    assert trace[-1] == "scan_signature"
    assert "verify_final" not in trace


@pytest.mark.parametrize(
    "inventory",
    (
        lambda trace: _Inventory(trace, summary=_Summary(root="C:/other")),
        lambda trace: _Inventory(trace, scan_identity=(11, 22, 999)),
        lambda trace: _Inventory(trace, file_count=2),
    ),
)
def test_gate_rejects_inconsistent_scan_evidence(inventory) -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(trace),
        inventory=inventory(trace),
    )

    assert decision.reason == "checkpoint_evidence_mismatch"
    assert trace[-1] == "scan_count"
    assert "verify_final" not in trace


def test_gate_does_not_fallback_when_newest_exact_owner_is_absent() -> None:
    trace: list[str] = []

    decision = evaluate_incremental_gate(
        _request(trace),
        state=_State(trace, owner=None),
        inventory=_Inventory(trace),
    )

    assert decision == IncrementalGateDecision(
        False,
        "no_matching_latest_durable_run",
        None,
    )
    assert trace == ["newest_owner"]


def test_final_boundary_failure_propagates_after_all_durable_evidence() -> None:
    trace: list[str] = []

    def reject_final_boundary() -> None:
        trace.append("verify_final")
        raise ValueError("fixture boundary changed")

    request = IncrementalGateRequest.from_access_policy(
        _Policy(),
        framework_policy_signature="effective-policy",
        inventory_policy_signature="raw-policy",
        journal_before=_CURSOR,
        verify_final=reject_final_boundary,
    )

    with pytest.raises(ValueError, match="boundary changed"):
        evaluate_incremental_gate(
            request,
            state=_State(trace),
            inventory=_Inventory(trace),
        )

    assert trace[-1] == "verify_final"


# endregion [02]
