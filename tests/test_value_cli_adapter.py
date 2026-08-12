from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _04_Nucleo_Operativo.value_review_contracts import ValueReviewAvailability
from _04_Nucleo_Operativo.value_review_tasks import (
    ValueReviewTaskQueueStatus,
    ValueReviewTaskStateError,
)
from neocortex import human_cli, value_cli_adapter
from neocortex.read_api import ReadScope, ScopeBinding


def _fake_report(
    availability: ValueReviewAvailability,
    *,
    returned_count: int = 0,
    reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        availability=availability,
        returned_count=returned_count,
        reason=reason,
        to_dict=lambda: {
            "availability": availability.value,
            "matched_count": returned_count,
            "returned_count": returned_count,
            "reason": reason,
            "items": [],
            "advisory_only": True,
            "mutation_authorized": False,
        },
    )


def test_value_payload_uses_fixed_scope_current_reference_and_no_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "missing-state"
    seen: list[tuple[object, object]] = []
    monkeypatch.setattr(
        value_cli_adapter,
        "scope_bindings",
        lambda _scope: (ScopeBinding(ReadScope.PERSONAL, state),),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "preview_value_review",
        lambda paths, query: (
            seen.append((paths, query))
            or _fake_report(ValueReviewAvailability.READY, returned_count=2)
        ),
    )

    payload = value_cli_adapter.value_review_payload(
        "personal",
        limit=7,
        clock_ns=lambda: 123_456,
    )

    paths, query = seen[0]
    assert paths.inventory == state / "dedup.sqlite3"
    assert query.limit == 7
    assert query.reference_time_ns == 123_456
    assert payload["reference_time_ns"] == 123_456
    assert payload["advisory_only"] is True
    assert payload["mutation_authorized"] is False
    assert payload["exit_code"] == 0
    assert not state.exists()


def test_value_payload_prefers_current_durable_queue_without_rescanning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "framework.sqlite3").write_bytes(b"fixture")
    queue_report = {
        "availability": "ready",
        "returned_count": 1,
        "items": [{"path": "/corpus/review.txt"}],
        "reason": None,
    }
    monkeypatch.setattr(
        value_cli_adapter,
        "scope_bindings",
        lambda _scope: (ScopeBinding(ReadScope.PERSONAL, state),),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "read_value_review_task_queue",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=ValueReviewTaskQueueStatus.READY,
            report_dict=lambda: queue_report,
        ),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "preview_value_review",
        lambda *_args, **_kwargs: pytest.fail("durable queue must avoid a broad rescan"),
    )

    payload = value_cli_adapter.value_review_payload(
        "personal",
        limit=7,
        clock_ns=lambda: 123_456,
    )

    assert payload["exit_code"] == 0
    assert payload["scopes"][0]["source"] == "durable_review_task_queue"  # type: ignore[index]
    assert payload["scopes"][0]["report"] == queue_report  # type: ignore[index]


def test_refresh_payload_writes_only_review_state_in_one_fixed_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    seen: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        value_cli_adapter,
        "scope_bindings",
        lambda _scope: (ScopeBinding(ReadScope.PERSONAL, state),),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "refresh_value_review_tasks",
        lambda database, _paths, *, scope, **_kwargs: (
            seen.append((database, scope))
            or SimpleNamespace(
                status="partial",
                to_dict=lambda: {"status": "partial", "wrote_state": True},
            )
        ),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "read_value_review_task_queue",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=ValueReviewTaskQueueStatus.PARTIAL,
            report_dict=lambda: {
                "availability": "partial",
                "returned_count": 2,
                "reason": "review_task_scan_partial",
                "items": [],
            },
        ),
    )

    payload = value_cli_adapter.value_review_refresh_payload(
        "personal",
        limit=10,
        clock_ns=lambda: 123_456,
    )

    assert seen == [(state / "framework.sqlite3", "personal")]
    assert payload["schema"] == "neocortex.value-review-refresh/v1"
    assert payload["read_only"] is False
    assert payload["mutation_authorized"] is False
    assert payload["exit_code"] == 4
    with pytest.raises(ValueError, match="requires personal or framework"):
        value_cli_adapter.value_review_refresh_payload("all", clock_ns=lambda: 1)


def test_refresh_payload_never_reports_complete_when_post_commit_queue_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        value_cli_adapter,
        "scope_bindings",
        lambda _scope: (ScopeBinding(ReadScope.PERSONAL, tmp_path),),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "refresh_value_review_tasks",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="complete",
            reason=None,
            to_dict=lambda: {"status": "complete", "wrote_state": True},
        ),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "read_value_review_task_queue",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=ValueReviewTaskQueueStatus.STALE,
            report_dict=lambda: {
                "availability": "partial",
                "returned_count": 0,
                "reason": "review_task_source_stale",
                "items": [],
            },
        ),
    )

    payload = value_cli_adapter.value_review_refresh_payload(
        "personal",
        limit=10,
        clock_ns=lambda: 123_456,
    )

    assert payload["exit_code"] == 4
    entry = payload["scopes"][0]  # type: ignore[index]
    assert entry["status"] == "stale"
    assert entry["refresh"]["status"] == "complete"
    assert entry["report"]["reason"] == "review_task_source_stale"


def test_refresh_payload_preserves_corrupt_source_exit_when_queue_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        value_cli_adapter,
        "scope_bindings",
        lambda _scope: (ScopeBinding(ReadScope.PERSONAL, tmp_path),),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "refresh_value_review_tasks",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="unavailable",
            reason="inventory_state_corrupt",
            to_dict=lambda: {
                "reason": "inventory_state_corrupt",
                "status": "unavailable",
                "wrote_state": False,
            },
        ),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "read_value_review_task_queue",
        lambda *_args, **_kwargs: SimpleNamespace(
            report_dict=lambda: {
                "availability": "unavailable",
                "returned_count": 0,
                "reason": "review_task_queue_absent",
                "items": [],
            }
        ),
    )

    payload = value_cli_adapter.value_review_refresh_payload(
        "personal",
        limit=10,
        clock_ns=lambda: 123_456,
    )

    assert payload["exit_code"] == 7
    entry = payload["scopes"][0]  # type: ignore[index]
    assert entry["refresh"]["reason"] == "inventory_state_corrupt"
    assert entry["report"]["reason"] == "review_task_queue_absent"


@pytest.mark.parametrize("refresh", (False, True))
def test_value_payloads_map_corrupt_framework_to_stable_corrupt_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    refresh: bool,
) -> None:
    monkeypatch.setattr(
        value_cli_adapter,
        "scope_bindings",
        lambda _scope: (ScopeBinding(ReadScope.PERSONAL, tmp_path),),
    )

    def corrupt_state(*_args: object, **_kwargs: object) -> object:
        raise ValueReviewTaskStateError("framework_state_corrupt")

    monkeypatch.setattr(
        value_cli_adapter,
        "refresh_value_review_tasks" if refresh else "read_value_review_task_queue",
        corrupt_state,
    )
    payload = (
        value_cli_adapter.value_review_refresh_payload(
            "personal", limit=10, clock_ns=lambda: 123_456
        )
        if refresh
        else value_cli_adapter.value_review_payload("personal", limit=10, clock_ns=lambda: 123_456)
    )

    assert payload["exit_code"] == 7
    entry = payload["scopes"][0]  # type: ignore[index]
    assert entry["status"] == "unavailable"
    assert entry["reason"] == "framework_state_corrupt"


@pytest.mark.parametrize(
    ("availability", "reason", "expected"),
    [
        (ValueReviewAvailability.READY, None, 3),
        (ValueReviewAvailability.PARTIAL, "catalog_state_absent", 4),
        (ValueReviewAvailability.UNAVAILABLE, "inventory_state_corrupt", 7),
        (ValueReviewAvailability.UNAVAILABLE, "inventory_state_incompatible", 6),
        (ValueReviewAvailability.UNAVAILABLE, "inventory_state_absent", 3),
        (ValueReviewAvailability.UNAVAILABLE, "inventory_state_invalid", 1),
    ],
)
def test_value_payload_maps_coverage_to_stable_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    availability: ValueReviewAvailability,
    reason: str | None,
    expected: int,
) -> None:
    monkeypatch.setattr(
        value_cli_adapter,
        "scope_bindings",
        lambda _scope: (ScopeBinding(ReadScope.PERSONAL, tmp_path),),
    )
    monkeypatch.setattr(
        value_cli_adapter,
        "preview_value_review",
        lambda *_args: _fake_report(availability, reason=reason),
    )

    payload = value_cli_adapter.value_review_payload(
        "personal",
        clock_ns=lambda: 1,
    )

    assert payload["exit_code"] == expected


def test_human_value_review_explains_advisory_only_recommendations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        value_cli_adapter,
        "value_review_payload",
        lambda *_args, **_kwargs: {
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "status": "ready",
                    "report": {
                        "matched_count": 1,
                        "items": [
                            {
                                "state": "review_low_value",
                                "path": "/corpus/tmp/old.log",
                                "size_bytes": 2048,
                                "reasons": ["old repeated disposable content"],
                                "uncertainties": ["usage_history_unavailable"],
                            }
                        ],
                    },
                }
            ],
        },
    )

    assert human_cli.run_human_command(("review", "value", "--limit", "8")) == 0
    output = capsys.readouterr().out
    assert "revisar valor bajo" in output
    assert "/corpus/tmp/old.log" in output
    assert "no autorizan mover, archivar ni borrar" in output
    assert "historial de uso no disponible" in output


def test_value_review_json_is_one_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = {"kind": "neocortex_scoped_value_review", "exit_code": 3, "scopes": []}
    monkeypatch.setattr(
        value_cli_adapter,
        "value_review_payload",
        lambda *_args, **_kwargs: expected,
    )

    assert (
        value_cli_adapter.run_value_review(
            scope="personal",
            limit=5,
            json_output=True,
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out) == expected


def test_human_value_refresh_dispatches_opt_in_write_surface(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = {
        "schema": "neocortex.value-review-refresh/v1",
        "read_only": False,
        "mutation_authorized": False,
        "exit_code": 4,
        "scopes": [],
    }
    monkeypatch.setattr(
        value_cli_adapter,
        "value_review_refresh_payload",
        lambda *_args, **_kwargs: expected,
    )

    assert (
        human_cli.run_human_command(
            ("review", "value", "--scope", "personal", "--refresh", "--json")
        )
        == 4
    )
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize(
    "call",
    [
        lambda: value_cli_adapter.value_review_payload("personal", limit=0),
        lambda: value_cli_adapter.value_review_payload(
            "personal",
            clock_ns=lambda: -1,
        ),
    ],
)
def test_value_payload_rejects_unbounded_or_invalid_runtime_inputs(call) -> None:
    with pytest.raises((RuntimeError, ValueError)):
        call()
