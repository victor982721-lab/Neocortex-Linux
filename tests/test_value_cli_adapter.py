from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _04_Nucleo_Operativo.value_review_contracts import ValueReviewAvailability
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
        lambda paths, query: seen.append((paths, query))
        or _fake_report(ValueReviewAvailability.READY, returned_count=2),
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

    assert value_cli_adapter.run_value_review(
        scope="personal",
        limit=5,
        json_output=True,
    ) == 3
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
