from __future__ import annotations

import json

import pytest

from neocortex import cli, human_cli


def _hit() -> dict[str, object]:
    return {
        "rank": 1,
        "resource": {
            "current_path": "/corpus/Transformador.pdf",
            "resource_id": "resource:1",
        },
        "evidence": {
            "evidence_id": "evidence:1",
            "page": 7,
            "snippet": "Prueba TTR del transformador de potencia.",
        },
        "reasons": ["fts_pdf returned this concrete evidence"],
    }


def test_human_search_explains_path_locator_and_non_authority(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        human_cli,
        "search_payload",
        lambda *_args, **_kwargs: {
            "query": "transformador",
            "scope_requested": "personal",
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "result": {
                        "complete": True,
                        "result_window_full": False,
                        "hits": [_hit()],
                    },
                }
            ],
        },
    )

    assert human_cli.run_human_command(("search", "transformador")) == 0
    output = capsys.readouterr().out
    assert "Transformador.pdf · página 7" in output
    assert "Prueba TTR" in output
    assert "no son probabilidades ni autorizan acciones" in output


def test_human_ask_labels_citations_without_fabricating_an_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        human_cli,
        "context_payload",
        lambda *_args, **_kwargs: {
            "query": "TTR",
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "context": {
                        "completeness": "complete",
                        "selected_hits": [_hit()],
                        "citation_ids": [{"citation_id": "K1", "evidence_id": "evidence:1"}],
                    },
                }
            ],
        },
    )

    assert human_cli.run_human_command(("ask", "TTR")) == 0
    output = capsys.readouterr().out
    assert "[K1] Transformador.pdf" in output
    assert "no inventa una respuesta" in output


def test_human_status_json_is_one_machine_readable_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "kind": "neocortex_scoped_status",
        "exit_code": 0,
        "scopes": [],
    }
    monkeypatch.setattr(human_cli, "status_payload", lambda _scope: payload)

    assert human_cli.run_human_command(("status", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_installed_entrypoint_dispatches_human_commands_before_flat_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(human_cli, "run_human_command", lambda args: seen.append(tuple(args)) or 17)

    assert cli.entrypoint(("status", "--scope", "personal")) == 17
    assert seen == [("status", "--scope", "personal")]


def test_help_lists_useful_facade_and_legacy_compatibility(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert human_cli.run_human_command(("help",)) == 0
    output = capsys.readouterr().out
    assert "status" in output
    assert "search" in output
    assert "ask" in output
    assert "flags siguen disponibles" in " ".join(output.split())


@pytest.mark.parametrize("arguments", [("inspect",), ("review",), ("agent",)])
def test_nested_commands_require_a_concrete_read_only_action(arguments) -> None:
    with pytest.raises(SystemExit) as raised:
        human_cli.run_human_command(arguments)
    assert raised.value.code == 2
