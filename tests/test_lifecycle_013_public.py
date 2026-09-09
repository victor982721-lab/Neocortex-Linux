"""Public lifecycle contracts for the 0.13 durable ``--all`` surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from neocortex.api import public
from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_direct import run_operational_status
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.api.lifecycle_read_api import lifecycle_status_payload
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.orchestration.run_manifest import RunBudget, RunManifest


def _framework_state(tmp_path: Path) -> tuple[Path, int]:
    root = tmp_path / "root"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(root),
                root_identity=(1, 2, -1),
                selected_routes=("text",),
                budget={"durable": RunBudget(max_items=7, max_bytes=8192).payload()},
            ).event_payload(),
        )
    return state_directory, run_id


def test_run_budget_flags_project_to_framework_config_without_shifting_positionals(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--all",
            "--root",
            str(tmp_path / "root"),
            "--state-directory",
            str(tmp_path / "state"),
            "--run-max-items",
            "7",
            "--run-max-bytes",
            "8192",
            "--run-time-budget-seconds",
            "12.5",
        ]
    )
    validate_arguments(args)
    config = framework_config_from_args(args)

    assert (config.run_max_items, config.run_max_bytes) == (7, 8192)
    assert config.run_time_budget_seconds == 12.5
    # The existing positional construction remains source-compatible.
    positional = type(config)(tmp_path / "root", tmp_path / "state", False, 0, "fast", "text")
    assert positional.route == "text"
    assert positional.run_max_items is None


def test_status_json_is_the_same_bounded_read_only_envelope_as_api(
    tmp_path: Path,
    capsys,
) -> None:
    state_directory, run_id = _framework_state(tmp_path)
    args = build_parser().parse_args(
        [
            "--status",
            "--status-json",
            "--status-run",
            str(run_id),
            "--status-limit",
            "1",
            "--state-directory",
            str(state_directory),
        ]
    )
    validate_arguments(args)

    assert run_operational_status(args) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    api_payload = lifecycle_status_payload(
        limit=1,
        run_id=run_id,
        state_directory=state_directory,
    )

    for payload in (cli_payload, api_payload):
        payload_map = cast(dict[str, Any], payload)
        assert payload["schema"] == "neocortex.lifecycle-envelope/v1"
        assert payload["kind"] == "neocortex_lifecycle_status"
        assert payload["operation"] == "lifecycle_status"
        assert payload["read_only"] is True
        run = cast(dict[str, Any], payload_map["runs"][0])
        assert run["run_id"] == run_id
        assert "budget" in run
        assert "stages" in run

    # request_id is intentionally per read; the bounded result is otherwise
    # structurally equivalent across the two public read surfaces. Live status
    # timings are expected to advance between the two read-only snapshots.
    cli_payload.pop("request_id")
    api_payload.pop("request_id")
    for payload in (cli_payload, api_payload):
        payload_map = cast(dict[str, Any], payload)
        run = cast(dict[str, Any], payload_map["runs"][0])
        run["elapsed_ns"] = 0
        run["budget"]["elapsed_ns"] = 0
        run["budget"]["elapsed_seconds"] = 0
        run["budget"]["elapsed_until_ns"] = 0
        run["lifecycle"]["budget"]["elapsed_ns"] = 0
        run["lifecycle"]["budget"]["elapsed_seconds"] = 0
        run["lifecycle"]["budget"]["elapsed_until_ns"] = 0
    assert cli_payload == api_payload


def test_lifecycle_status_is_exported_lazily_by_both_python_facades() -> None:
    import neocortex.sdk as sdk
    from neocortex.api import lifecycle_read_api

    assert public.lifecycle_status_payload is lifecycle_read_api.lifecycle_status_payload
    assert sdk.lifecycle_status_payload is lifecycle_read_api.lifecycle_status_payload
    assert public.RunBudget is RunBudget
    assert sdk.RunBudget is RunBudget
    assert public.LIFECYCLE_ENVELOPE_SCHEMA == "neocortex.lifecycle-envelope/v1"
    assert sdk.LIFECYCLE_STATUS_OPERATION == "lifecycle_status"
