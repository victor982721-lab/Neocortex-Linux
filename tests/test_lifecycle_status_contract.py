from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from neocortex.api import agent_server, lifecycle_read_api
from neocortex.api.run_lifecycle import read_run_status
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.runtime.orchestration.run_status import serialized_run_status


TEST_CAPABILITIES = ("base", "agent")


def _fixture_status(tmp_path: Path) -> tuple[Path, object]:
    root = tmp_path / "root"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(root),
                root_identity=(1, 2, -1),
                selected_routes=("text",),
            ).event_payload(),
        )
    return state_directory, read_run_status(database, limit=1)[0]


def _patch_serialized(
    monkeypatch: pytest.MonkeyPatch,
    state_directory: Path,
    status: object,
    mutate: object,
) -> None:
    raw = json.loads(serialized_run_status(status))
    assert callable(mutate)
    mutate(raw)
    monkeypatch.setattr(lifecycle_read_api, "read_run_status", lambda *_args, **_kwargs: (status,))
    monkeypatch.setattr(
        lifecycle_read_api,
        "serialized_run_status",
        lambda _status: json.dumps(raw, ensure_ascii=False),
    )


def test_lifecycle_payload_sanitizes_paths_and_bounds_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, status = _fixture_status(tmp_path)

    def mutate(raw: dict[str, object]) -> None:
        root = "/tmp/\x1b[31munsafe\nroot"
        raw["root"] = root
        manifest = raw["manifest"]
        assert isinstance(manifest, dict)
        manifest["root"] = root
        manifest["configuration"] = {
            f"key-{index}": "value\x1b[32m\n" + ("x" * 4_000)
            for index in range(100)
        }
        unsigned = dict(manifest)
        unsigned.pop("digest")
        manifest["digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            ).hexdigest()
        lifecycle = raw["lifecycle"]
        assert isinstance(lifecycle, dict)
        lifecycle["manifest_digest"] = manifest["digest"]

    _patch_serialized(monkeypatch, state_directory, status, mutate)
    payload = lifecycle_read_api.lifecycle_status_payload(state_directory=state_directory)

    assert payload["status"] == "ok"
    assert "\x1b" not in payload["runs"][0]["root"]
    assert "\n" not in payload["runs"][0]["root"]
    configuration = payload["runs"][0]["manifest"]["configuration"]
    assert len(configuration) <= lifecycle_read_api.MAX_METADATA_KEYS
    assert all(len(str(value)) <= lifecycle_read_api.MAX_TEXT_CHARS for value in configuration.values())


def test_lifecycle_payload_rejects_future_schema_as_typed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, status = _fixture_status(tmp_path)
    _patch_serialized(
        monkeypatch,
        state_directory,
        status,
        lambda raw: raw["lifecycle"].update({"schema": "neocortex.lifecycle-envelope/v2"}),
    )

    payload = lifecycle_read_api.lifecycle_status_payload(state_directory=state_directory)

    assert payload["coverage"] == "unavailable"
    assert payload["status"] == "schema_incompatible"
    assert payload["exit_code"] == 6
    assert payload["error"]["code"] == "schema_incompatible"


def test_lifecycle_payload_rejects_corrupt_json_and_absent_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_directory, status = _fixture_status(tmp_path)
    monkeypatch.setattr(lifecycle_read_api, "read_run_status", lambda *_args, **_kwargs: (status,))
    monkeypatch.setattr(lifecycle_read_api, "serialized_run_status", lambda _status: "not-json")

    corrupt = lifecycle_read_api.lifecycle_status_payload(state_directory=state_directory)
    assert corrupt["status"] == "corrupt"
    assert corrupt["error"]["code"] == "state_corrupt"

    absent = lifecycle_read_api.lifecycle_status_payload(state_directory=tmp_path / "absent")
    assert absent["status"] == "unavailable"
    assert absent["error"]["code"] == "state_unavailable"


@pytest.mark.capability("agent")
def test_lifecycle_status_tool_publishes_closed_output_schema() -> None:
    server = agent_server.create_server()
    tool = next(item for item in asyncio.run(server.list_tools()) if item.name == "lifecycle_status")

    assert tool.outputSchema is not None
    assert tool.outputSchema["additionalProperties"] is False
    assert tool.outputSchema["properties"]["schema"]["const"] == "neocortex.lifecycle-envelope/v1"
    assert tool.outputSchema["properties"]["kind"]["const"] == "neocortex_lifecycle_status"
    assert tool.outputSchema["properties"]["lifecycle"]["$ref"]
