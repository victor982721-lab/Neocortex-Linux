"""Public human CLI regressions for malformed read requests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from neocortex.api import read_api
from neocortex.api.cli import human
from neocortex.api.read_contract import ReadOperation, validate_read_payload


@pytest.mark.parametrize(
    ("arguments", "operation", "scope", "query", "limit"),
    (
        (("search", "relay", "--json"), ReadOperation.SEARCH, "all", "relay", 10),
        (
            ("ask", "relay", "--json", "--response-version", "1"),
            ReadOperation.CONTEXT,
            "all",
            "relay",
            10,
        ),
    ),
)
def test_read_value_error_emits_typed_json_and_exit_two(
    arguments: tuple[str, ...],
    operation: ReadOperation,
    scope: str,
    query: str,
    limit: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = {
        ReadOperation.SEARCH: "search_payload",
        ReadOperation.CONTEXT: "context_payload",
    }[operation]

    def reject(*_args: object, **_kwargs: object) -> object:
        raise ValueError("entrada inválida\x1b[31m")

    monkeypatch.setattr(human, producer, reject)

    assert human.run_human_command(arguments) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    validate_read_payload(
        payload,
        operation,
        scope=scope,
        query=query,
        mode="evidence",
        include_history=False,
        limit=limit,
    )
    assert payload["exit_code"] == 2
    assert payload["status"] == "usage_error"
    assert payload["coverage"] == "unavailable"
    assert payload["error"]["code"] == "usage_error"
    assert payload["error"]["message"] == "entrada inválida"
    assert "\x1b" not in captured.out
    assert "Traceback" not in captured.out


def test_review_value_error_emits_the_review_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_import = human.importlib.import_module

    def fake_import(name: str) -> object:
        if name == "neocortex.api.cli.value_review":
            return SimpleNamespace(
                run_value_review=lambda **_kwargs: (_ for _ in ()).throw(
                    ValueError("limit inválido\n")
                )
            )
        return original_import(name)

    monkeypatch.setattr(human.importlib, "import_module", fake_import)

    assert human.run_human_command(("review", "value", "--limit", "7", "--json")) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    validate_read_payload(payload, ReadOperation.REVIEW, scope="personal", limit=7)
    assert payload["exit_code"] == 2
    assert payload["status"] == "usage_error"
    assert payload["error"]["code"] == "usage_error"
    assert payload["error"]["message"] == "limit inválido"


@pytest.mark.parametrize(
    ("arguments", "producer", "expected_usage"),
    (
        (("search", "relay"), "search_payload", "Neocortex search --help"),
        (
            ("ask", "relay", "--response-version", "1"),
            "context_payload",
            "Neocortex ask --help",
        ),
    ),
)
def test_read_value_error_text_is_actionable_and_traceback_free(
    arguments: tuple[str, ...],
    producer: str,
    expected_usage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        human,
        producer,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("malformed request")),
    )

    assert human.run_human_command(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR" in captured.err
    assert "uso inválido" in captured.err
    assert "malformed request" in captured.err
    assert expected_usage in captured.err
    assert "Traceback" not in captured.err


def test_blank_search_query_is_handled_before_state_lookup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(read_api, "scope_bindings", lambda *_args: pytest.fail("no state lookup"))

    assert human.run_human_command(("search", "", "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage_error"
    assert payload["error"]["message"] == "query cannot be blank"


@pytest.mark.parametrize(
    ("arguments", "usage_line"),
    (
        (("help", "status"), "usage: Neocortex status"),
        (("help", "curate", "plan"), "usage: Neocortex curate plan"),
    ),
)
def test_contextual_help_reuses_the_canonical_subparser(
    arguments: tuple[str, ...],
    usage_line: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert human.run_human_command(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert usage_line in captured.out
