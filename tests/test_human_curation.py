"""Human CLI adapter for fixed-root, read-only curation-plan pages."""

from __future__ import annotations

import json

import pytest

from neocortex.api import curation_api
from neocortex.api.cli import human
from neocortex.interface.entrypoint import entrypoint


def _payload(
    coverage: str = "complete",
    *,
    items: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    rows = [] if items is None else items
    return {
        "schema": "neocortex.curation-plan/v1",
        "kind": "neocortex_curation_plan",
        "operation": "curation-plan",
        "request_id": "curation-test",
        "read_only": True,
        "effects": {"state": "none", "corpus": "none", "external": "none"},
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "tools_authorized": False,
            "actions_authorized": False,
        },
        "coverage": coverage,
        "snapshot": {
            "schema": "neocortex.curation-snapshot/v1",
            "snapshot_id": "snapshot-test" if coverage != "unavailable" else None,
            "coverage": coverage,
            "root": "/Corpus" if coverage != "unavailable" else None,
            "scan_id": 7 if coverage != "unavailable" else None,
            "missing_owners": [] if coverage == "complete" else ["catalog"],
        },
        "page": {
            "limit": 2,
            "cursor": None,
            "next_cursor": "cursor-next" if rows else None,
            "complete": coverage == "complete" and not rows,
            "plan_digest": "sha256:plan-test" if coverage != "unavailable" else None,
            "items_total": len(rows),
            "items": rows,
        },
        "error": (
            None
            if coverage == "complete"
            else {
                "code": f"curation_state_{coverage}",
                "message": f"estado publicado {coverage}",
                "retryable": coverage == "partial",
            }
        ),
    }


def _install_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> list[tuple[int, str | None]]:
    calls: list[tuple[int, str | None]] = []

    def return_payload(*, limit: int, cursor: str | None) -> dict[str, object]:
        calls.append((limit, cursor))
        return payload

    monkeypatch.setattr(curation_api, "curation_plan_payload", return_payload)
    return calls


def test_curate_plan_parser_and_human_dispatch_contract() -> None:
    assert "curate" in human.HUMAN_COMMANDS
    assert human.handles_human_command(("curate", "plan"))

    args = human.build_human_parser().parse_args(
        ("curate", "plan", "--limit", "7", "--cursor", "cursor-7", "--json")
    )

    assert args.command == "curate"
    assert args.curate_command == "plan"
    assert args.limit == 7
    assert args.cursor == "cursor-7"
    assert args.json is True


@pytest.mark.parametrize("value", ("0", "101", "not-a-number"))
def test_curate_plan_rejects_out_of_range_limits(value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        human.build_human_parser().parse_args(("curate", "plan", "--limit", value))

    assert raised.value.code == 2


@pytest.mark.parametrize("option", ("--root", "--state-directory"))
def test_curate_plan_accepts_no_corpus_or_state_path(option: str) -> None:
    with pytest.raises(SystemExit) as raised:
        human.build_human_parser().parse_args(("curate", "plan", option, "/tmp/state"))

    assert raised.value.code == 2


def test_curate_plan_json_preserves_the_api_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _payload()
    calls = _install_payload(monkeypatch, payload)

    exit_code = human.run_human_command(
        ("curate", "plan", "--limit", "2", "--cursor", "cursor-current", "--json")
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == payload
    assert calls == [(2, "cursor-current")]


def test_installed_entrypoint_routes_curate_without_loading_legacy_parser(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _payload()
    _install_payload(monkeypatch, payload)

    assert entrypoint(("curate", "plan", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_curate_plan_human_output_is_brief_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _payload(
        items=[
            {
                "item_id": "organization:9",
                "kind": "organization_plan",
                "status": "review",
                "action": "review_organization_proposal",
                "source_path": "\x1b[31m/Corpus/origen.txt",
                "destination_path": "/Organizado/destino.txt",
                "reason": "classification_above_threshold",
                "evidence": {},
            }
        ]
    )
    _install_payload(monkeypatch, payload)

    exit_code = human.run_human_command(("curate", "plan", "--limit", "1"))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "coverage=complete" in captured.out
    assert "total=1" in captured.out
    assert "digest=sha256:plan-test" in captured.out
    assert "page_items=1" in captured.out
    assert "ITEM id=organization:9" in captured.out
    assert "Origen: /Corpus/origen.txt" in captured.out
    assert "Destino: /Organizado/destino.txt" in captured.out
    assert "Razón: classification_above_threshold" in captured.out
    assert "\x1b" not in captured.out


@pytest.mark.parametrize("coverage", ("partial", "unavailable"))
def test_curate_plan_non_complete_state_returns_two_without_writing(
    coverage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _payload(coverage)
    calls = _install_payload(monkeypatch, payload)

    exit_code = human.run_human_command(("curate", "plan"))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err == ""
    assert f"coverage={coverage}" in captured.out
    assert f"Estado: estado publicado {coverage}" in captured.out
    assert "no creó, migró ni modificó estado o archivos" in captured.out
    assert calls == [(50, None)]


def test_curate_plan_rejects_an_invalid_cursor_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def reject_cursor(*, limit: int, cursor: str | None) -> dict[str, object]:
        raise ValueError(f"cursor inválido para página {limit}: {cursor}")

    monkeypatch.setattr(curation_api, "curation_plan_payload", reject_cursor)

    assert human.run_human_command(("curate", "plan", "--cursor", "bad")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "curate plan: cursor inválido" in captured.err


def test_curate_requires_a_concrete_action() -> None:
    with pytest.raises(SystemExit) as raised:
        human.run_human_command(("curate",))

    assert raised.value.code == 2
