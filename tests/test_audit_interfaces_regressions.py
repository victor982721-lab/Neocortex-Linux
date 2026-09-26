"""Adversarial public-interface regressions for incomplete outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.api.cli import cli_agent_activity, cli_app, cli_hygiene, cli_models, cli_semantic
from neocortex.api.cli.cli_reporting import has_strict_route_errors
from neocortex.api import read_api
from neocortex.api.read_contract import ReadOperation, validate_read_payload
from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudgetExceeded
from neocortex.interface.entrypoint import entrypoint
from neocortex.persistence.framework_state_writer import FrameworkState


def test_canonical_help_preserves_global_path_overrides(capsys: pytest.CaptureFixture[str]) -> None:
    code = entrypoint(["--root", "/private/corpus", "doctor", "platform", "--help"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert captured.out.startswith("usage: Neocortex doctor platform")
    assert "--doctor-platform" not in captured.out


def test_models_prepare_does_not_publish_incomplete_report_as_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = {
        "schema_version": 1,
        "kind": "models_report",
        "models_root": "/private/models",
        "all_prepared": False,
        "models": [],
    }
    monkeypatch.setattr(cli_models, "prepare_models", lambda **_kwargs: report)

    code = cli_app.main(["--models-prepare", "--models-json"])

    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["all_prepared"] is False
    assert captured.err == ""


def test_semantic_preparation_nonzero_stops_framework_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    called = False

    def forbidden_framework(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("Framework must not start after failed Semantic preparation")

    monkeypatch.setattr(cli_app, "run_framework", forbidden_framework)
    monkeypatch.setattr(
        "neocortex.api.cli.cli_semantic.prepare_integrated_semantic_start",
        lambda *_args, **_kwargs: 7,
    )

    code = cli_app.main(
        [
            "--all",
            "--root",
            str(tmp_path / "corpus"),
            "--state-directory",
            str(tmp_path / "state"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert called is False
    assert "semantic_stage_failed" in captured.err
    assert "completion=incomplete" in captured.err
    assert "Traceback" not in captured.err


def test_semantic_preparation_cancellation_keeps_public_exit_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "neocortex.api.cli.cli_semantic.prepare_integrated_semantic_start",
        lambda *_args, **_kwargs: 130,
    )

    code = entrypoint(
        [
            "--all",
            "--json",
            "--root",
            str(tmp_path / "corpus"),
            "--state-directory",
            str(tmp_path / "state"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 130
    assert "cancelled" in captured.err.casefold()
    assert "completion incomplete" in captured.err


@pytest.mark.parametrize("origin", ["prepare", "stage", "framework"])
def test_cancelled_all_json_emits_machine_envelope(
    origin: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    if origin == "prepare":
        monkeypatch.setattr(
            cli_semantic,
            "prepare_integrated_semantic_start",
            lambda *_args, **_kwargs: 130,
        )
    else:
        monkeypatch.setattr(
            cli_semantic,
            "prepare_integrated_semantic_start",
            lambda *_args, **_kwargs: 0,
        )

    if origin == "stage":
        monkeypatch.setattr(
            cli_semantic,
            "run_integrated_all_semantic_index",
            lambda *_args, **_kwargs: 130,
        )
        monkeypatch.setattr(
            cli_app,
            "run_framework",
            lambda _args, *, progress, lifecycle_stage_runner, lifecycle_stage_details: (
                lifecycle_stage_runner(9)
            ),
        )
    elif origin == "framework":
        monkeypatch.setattr(
            cli_app,
            "run_framework",
            lambda _args, *, progress, lifecycle_stage_runner, lifecycle_stage_details: (
                (_ for _ in ()).throw(KeyboardInterrupt())
            ),
        )

    code = entrypoint(
        [
            "--all",
            "--json",
            "--root",
            str(tmp_path / "corpus"),
            "--state-directory",
            str(tmp_path / "state"),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 130
    assert payload["schema"] == "neocortex.lifecycle-envelope/v1"
    assert payload["status"] == "cancelled"
    assert payload["completion"] == "incomplete"
    assert payload["exit_code"] == 130
    assert payload["error"]["code"] == "execution_cancelled"
    assert "Traceback" not in captured.err


def test_nonzero_semantic_stage_is_not_followed_by_framework_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def fake_framework(
        _args: argparse.Namespace,
        *,
        progress: object,
        lifecycle_stage_runner,
        lifecycle_stage_details,
    ) -> object:
        del progress, lifecycle_stage_details
        lifecycle_stage_runner(41)
        return SimpleNamespace(run_id=41, route_results={}, actions=None)

    def failed_semantic(*_args: object, **_kwargs: object) -> int:
        return 2

    monkeypatch.setattr(cli_app, "run_framework", fake_framework)
    monkeypatch.setattr(
        "neocortex.api.cli.cli_semantic.prepare_integrated_semantic_start",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "neocortex.api.cli.cli_semantic.run_integrated_all_semantic_index",
        failed_semantic,
    )

    code = cli_app.main(
        [
            "--all",
            "--json",
            "--root",
            str(tmp_path / "corpus"),
            "--state-directory",
            str(tmp_path / "state"),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 2
    assert payload["run_id"] == 41
    assert "failed_routes" not in payload
    assert "exit code 2" in payload["error"]["message"]
    assert payload["error"]["code"] == "semantic_stage_partial"
    assert "COMPLETADA" not in captured.err
    assert "Traceback" not in captured.err


def test_real_framework_lifecycle_stays_failed_after_semantic_partial(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Exercise the installed Framework callback, not only a test double."""

    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(
        cli_semantic,
        "prepare_integrated_semantic_start",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        cli_semantic,
        "run_integrated_all_semantic_index",
        lambda *_args, **_kwargs: 2,
    )

    code = cli_app.main(
        [
            "--all",
            "--json",
            "--root",
            str(root),
            "--state-directory",
            str(state),
            "--global-min-free-memory-mb",
            "0",
            "--global-min-free-commit-mb",
            "0",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["run_id"] == 1
    assert "failed_routes" not in payload
    with FrameworkState(state / "framework.sqlite3", existing_only=True) as owner:
        rows = owner._connection.execute(
            "SELECT run_id, status FROM initial_runs ORDER BY run_id"
        ).fetchall()
        assert rows
        assert rows[-1][1] == "failed"


def test_hygiene_blocked_owner_without_code_is_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_hygiene,
        "_call_owner",
        lambda *_args, **_kwargs: {
            "status": "blocked",
            "exit_code": 0,
            "candidate_count": 0,
        },
    )

    code = cli_app.main(["hygiene", "--hygiene-json"])

    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["exit_code"] == 2


@pytest.mark.parametrize("status", ["blocked", "failed", "partial", "recovery_required"])
def test_all_route_statuses_cannot_hide_incomplete_zip_stage(status: str) -> None:
    result = SimpleNamespace(route_results={"zip_intake": {"status": status}})

    assert has_strict_route_errors(result) is True


def test_activity_status_recovery_is_not_reported_as_ok(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from neocortex.api.agent_activity import ActivitySnapshot

    snapshot = ActivitySnapshot(
        activity_id="audit",
        owner="neocortex-framework",
        workspace_id="workspace",
        workspace_path=tmp_path,
        state="recovery_required",
        created_ns=1,
        updated_ns=2,
        run_id=None,
        process_pid=None,
    )
    monkeypatch.setattr(
        cli_agent_activity,
        "_open_or_prepare",
        lambda _args: SimpleNamespace(snapshot=lambda: snapshot),
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-directory", type=Path, required=True)
    cli_agent_activity.register_agent_activity_arguments(parser)
    args = parser.parse_args(
        [
            "--state-directory",
            str(tmp_path / "state"),
            "--agent-activity-id",
            "audit",
            "--agent-action",
            "status",
            "--agent-json",
        ]
    )

    code = cli_agent_activity.run_agent_activity(args)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 2
    assert payload["status"] == "blocked"
    assert payload["code"] == "activity_recovery_required"
    assert payload["result"]["state"] == "recovery_required"


def test_read_cancellation_keeps_exit_130_at_api_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binding = read_api.ScopeBinding(read_api.ReadScope.PERSONAL, tmp_path / "state")

    class CancelledService:
        def search(self, *_args: object, **_kwargs: object) -> object:
            raise KnowledgeReadBudgetExceeded("cancelled")

    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: (binding,))
    monkeypatch.setattr(read_api, "_service", lambda _binding: CancelledService())

    payload = read_api.search_payload("fixture", scope="personal")

    validate_read_payload(payload, ReadOperation.SEARCH, scope="personal", query="fixture", mode="evidence", include_history=False, limit=10)
    assert payload["exit_code"] == 130
    assert payload["status"] == "cancelled"
    assert payload["coverage"] == "blocked"
    assert payload["scopes"][0]["exit_code"] == 130
