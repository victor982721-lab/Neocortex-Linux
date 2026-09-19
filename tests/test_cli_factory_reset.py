"""Contracts for the exclusive flat ``--factory-reset`` CLI boundary."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from neocortex.api.cli import cli_app, cli_validation, human
from neocortex.api.cli.cli_parser import build_parser
from neocortex.interface.entrypoint import entrypoint


def _install_fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, object] | None = None,
    failure: BaseException | None = None,
) -> list[object]:
    """Install a side-effect-free factory-reset owner for CLI tests."""

    calls: list[object] = []
    engine = ModuleType("neocortex.persistence.factory_reset")

    def factory_reset(state_directory: str | Path | None = None) -> dict[str, object]:
        calls.append(state_directory)
        if failure is not None:
            raise failure
        return result or {"status": "complete", "deleted_count": 2}

    engine.factory_reset = factory_reset  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "neocortex.persistence.factory_reset", engine)
    return calls


def test_factory_reset_is_a_flat_parser_operation() -> None:
    args = build_parser().parse_args(("--factory-reset",))
    cli_validation.validate_arguments(args)
    assert args.factory_reset is True


def test_factory_reset_forwards_fixture_path_and_renders_simple_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_fake_engine(monkeypatch)
    state = tmp_path / "fixture-state"

    assert cli_app.main(("--factory-reset", "--state-directory", str(state))) == 0

    assert calls == [state]
    output = capsys.readouterr()
    assert output.out.strip() == "FACTORY_RESET status=complete deleted_count=2"
    assert output.err == ""


def test_factory_reset_uses_engine_default_without_state_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_fake_engine(monkeypatch)

    assert cli_app.main(("--factory-reset",)) == 0

    assert calls == [None]
    assert capsys.readouterr().out.startswith("FACTORY_RESET status=complete")


@pytest.mark.parametrize(
    "arguments",
    (
        ("--factory-reset", "--all"),
        ("--factory-reset", "--apply"),
        ("--factory-reset", "--root", "/fixture/corpus"),
        ("--factory-reset", "--json"),
        ("--factory-reset", "--yes"),
    ),
)
def test_factory_reset_rejects_extra_operation_controls_before_dispatch(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as failure:
        cli_app.main(arguments)
    assert failure.value.code


def test_factory_reset_failure_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FactoryResetError(RuntimeError):
        pass

    _install_fake_engine(monkeypatch, failure=FactoryResetError("fixture failed"))

    assert cli_app.main(("--factory-reset",)) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "ERROR factory_reset: fixture failed" in output.err


def test_factory_reset_partial_result_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_engine(monkeypatch, result={"status": "partial", "deleted_count": 1})

    assert cli_app.main(("--factory-reset",)) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "did not complete" in output.err


def test_old_state_reset_subcommand_is_removed() -> None:
    parser = human.build_human_parser()
    with pytest.raises(SystemExit) as failure:
        parser.parse_args(("state", "reset"))
    assert failure.value.code == 2


def test_installed_root_help_mentions_factory_reset_not_old_state_reset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert entrypoint(("--help",)) == 0
    output = capsys.readouterr().out
    assert "--factory-reset" in output
    assert "state reset" not in output.casefold()


def test_factory_reset_help_does_not_dispatch_engine(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_fake_engine(monkeypatch)

    assert entrypoint(("--factory-reset", "--help")) == 0
    output = capsys.readouterr().out
    assert "--factory-reset" in output
    assert calls == []
