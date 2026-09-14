"""CLI contracts for the selectable NeoCortex state reset boundary."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from neocortex.api.cli import human
from neocortex.api.cli.state_reset import STATE_RESET_CONFIRMATION


def _install_fake_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan_digest: str = "digest-1",
) -> list[dict[str, object]]:
    """Inject a side-effect-free persistence seam for adapter tests."""

    calls: list[dict[str, object]] = []
    engine = ModuleType("neocortex.persistence.state_reset")

    def plan_state_reset(state_directory: Path, *, scope: str) -> dict[str, object]:
        calls.append({"operation": "plan", "state_directory": state_directory, "scope": scope})
        return {
            "schema": "neocortex.state-reset/v1",
            "mode": "preview",
            "state_directory": str(state_directory),
            "scope": scope,
            "plan_digest": plan_digest,
            "file_count": 2,
            "run_count": 1,
            "cache_count": 1,
        }

    def execute_state_reset(
        state_directory: Path,
        *,
        scope: str,
        backup_directory: Path | None,
        apply: bool,
        confirmation: str | None,
    ) -> dict[str, object]:
        calls.append(
            {
                "operation": "execute",
                "state_directory": state_directory,
                "scope": scope,
                "backup_directory": backup_directory,
                "apply": apply,
                "confirmation": confirmation,
            }
        )
        return {
            "schema": "neocortex.state-reset/v1",
            "mode": "applied",
            "state_directory": str(state_directory),
            "scope": scope,
            "plan_digest": plan_digest,
            "deleted_file_count": 2,
        }

    engine.plan_state_reset = plan_state_reset  # type: ignore[attr-defined]
    engine.execute_state_reset = execute_state_reset  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "neocortex.persistence.state_reset", engine)
    import neocortex.persistence as persistence

    monkeypatch.setattr(persistence, "state_reset", engine, raising=False)
    return calls


def _json_output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_state_reset_parser_requires_one_scope_and_rejects_duplicates() -> None:
    parser = human.build_human_parser()
    state_parser = parser._subparsers._group_actions[0].choices["state"]
    reset_parser = state_parser._subparsers._group_actions[0].choices["reset"]

    parsed = reset_parser.parse_args(("--scope", "runs-and-caches"))
    assert parsed.scope == "runs-and-caches"
    with pytest.raises(SystemExit) as missing:
        reset_parser.parse_args(())
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as duplicate:
        reset_parser.parse_args(("--scope", "runs", "--scope", "all"))
    assert duplicate.value.code == 2


def test_state_reset_defaults_to_read_only_preview_and_keeps_backup_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_fake_engine(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    backup = tmp_path / "backup"

    assert (
        human.run_human_command(
            (
                "state",
                "reset",
                "--state-directory",
                str(state),
                "--scope",
                "runs",
                "--backup-directory",
                str(backup),
                "--json",
            )
        )
        == 0
    )
    payload = _json_output(capsys)
    assert payload["schema"] == "neocortex.state-reset/v1"
    assert payload["operation"] == "state-reset"
    assert payload["scope"] == "runs"
    assert payload["read_only"] is True
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["mode"] == "preview"
    assert result["plan_digest"] == "digest-1"
    assert result["requires_confirmation"] is True
    assert [call["operation"] for call in calls] == ["plan"]
    assert not backup.exists()


def test_state_reset_apply_requires_confirmation_and_plan_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_fake_engine(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()

    assert (
        human.run_human_command(
            (
                "state",
                "reset",
                "--state-directory",
                str(state),
                "--scope",
                "all",
                "--apply",
                "--json",
            )
        )
        == 3
    )
    payload = _json_output(capsys)
    assert payload["read_only"] is False
    assert payload["error"]["message"]
    assert [call["operation"] for call in calls] == []


def test_state_reset_apply_rechecks_digest_and_passes_fence_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_fake_engine(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    backup = tmp_path / "backup"

    assert (
        human.run_human_command(
            (
                "state",
                "reset",
                "--state-directory",
                str(state),
                "--scope",
                "runs-and-caches",
                "--backup-directory",
                str(backup),
                "--apply",
                "--confirm-state-reset",
                STATE_RESET_CONFIRMATION,
                "--plan-digest",
                "digest-1",
                "--json",
            )
        )
        == 0
    )
    payload = _json_output(capsys)
    assert payload["read_only"] is False
    assert payload["result"]["mode"] == "applied"
    assert [call["operation"] for call in calls] == ["plan", "execute"]
    assert calls[-1]["scope"] == "runs-and-caches"
    assert calls[-1]["backup_directory"] == backup
    assert calls[-1]["apply"] is True
    assert calls[-1]["confirmation"] == STATE_RESET_CONFIRMATION


def test_state_reset_apply_rejects_stale_digest_without_executing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_fake_engine(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()

    assert (
        human.run_human_command(
            (
                "state",
                "reset",
                "--state-directory",
                str(state),
                "--scope",
                "all",
                "--apply",
                "--confirm-state-reset",
                STATE_RESET_CONFIRMATION,
                "--plan-digest",
                "stale",
                "--json",
            )
        )
        == 2
    )
    payload = _json_output(capsys)
    assert "preview again" in payload["error"]["message"]
    assert [call["operation"] for call in calls] == ["plan"]
