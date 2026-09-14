"""Focused contracts for the small 0.14 CLI surface extension."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from neocortex.api.cli import cli_app, cli_validation
from neocortex.api.cli.cli_app import dispatch_direct
from neocortex.api.cli.cli_parser import build_parser
from neocortex.interface.entrypoint import _translate_canonical_arguments, entrypoint


def test_dedupe_is_an_explicit_non_routed_selector() -> None:
    args = build_parser().parse_args(("--root", "/tmp/corpus", "--dedupe"))

    cli_validation.validate_arguments(args)

    assert args.dedupe is True
    assert args.route == "none"
    assert args.root.as_posix() == "/tmp/corpus"


@pytest.mark.parametrize(
    "arguments",
    (
        ("--dedupe", "--all"),
        ("--dedupe", "--route", "pdf"),
        ("--dedupe", "--route-only", "--route", "pdf"),
        ("--dedupe-json",),
        ("--json",),
    ),
)
def test_dedupe_rejects_ambiguous_combinations(arguments: tuple[str, ...]) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit):
        cli_validation.validate_arguments(args)


def test_dedupe_dispatches_one_domain_service_without_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(("--dedupe", "--dedupe-json"))
    calls: list[object] = []

    def run_service(received: object) -> dict[str, object]:
        calls.append(received)
        return {"status": "complete", "code": "ok", "exit_code": 0}

    monkeypatch.setattr(cli_app, "_load_dedupe_service", lambda: (run_service, None))

    assert dispatch_direct(args) == 0
    assert calls == [args]


def test_dedupe_command_alias_converges_on_the_flat_service() -> None:
    assert _translate_canonical_arguments(
        ["dedupe", "--root", "/tmp/corpus", "--dedupe-json"]
    ) == ["--dedupe", "--root", "/tmp/corpus", "--dedupe-json"]
    assert _translate_canonical_arguments(
        ["--root", "/tmp/corpus", "dedupe", "--dedupe-json"]
    ) == ["--root", "/tmp/corpus", "--dedupe", "--dedupe-json"]


def test_dedupe_unavailable_is_structured_on_json_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = build_parser().parse_args(("--dedupe", "--dedupe-json"))
    monkeypatch.setattr(cli_app, "_load_dedupe_service", lambda: (None, "service missing"))

    assert dispatch_direct(args) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["code"] == "dedupe_service_unavailable"
    assert payload["operation"] == "dedupe"
    assert payload["status"] == "unavailable"


def test_linux_apply_is_gated_by_live_platform_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(("--all", "--apply"))
    monkeypatch.setattr(
        cli_validation,
        "current_platform_policy",
        lambda: SimpleNamespace(mutation_available=True),
    )

    cli_validation.validate_arguments(args)


def test_installed_root_help_is_brief_and_does_not_run_framework(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert entrypoint(("--help",)) == 0
    output = capsys.readouterr().out
    assert "--all" in output
    assert "--dedupe" in output
    assert "--route ROUTES" in output
    assert "--json" in output
    assert "no inicia inventario" in output
