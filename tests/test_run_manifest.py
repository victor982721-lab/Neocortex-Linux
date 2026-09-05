"""Durable lifecycle manifest and read-only envelope regressions."""

from __future__ import annotations

import json

import pytest

from neocortex.runtime.orchestration.run_manifest import (
    RunManifest,
    lifecycle_envelope,
    verify_event_payload,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.orchestration.run_status import list_run_status


def _manifest() -> RunManifest:
    return RunManifest(
        run_id=7,
        run_kind="resume",
        source_run_id=3,
        root="/tmp/fixture",
        root_identity=(1, 2, -1),
        selected_routes=("text", "pdf", "text"),
        configuration={"policy": "bounded", "routes": ["text", "pdf"]},
        budget={"items": 50, "bytes": 1_000_000},
        input_snapshot={"scan_id": 11, "candidate_rows": 2},
    )


def test_manifest_digest_is_canonical_and_verifiable() -> None:
    manifest = _manifest()
    payload = manifest.event_payload()

    assert payload["schema"] == "neocortex.run-manifest/v1"
    assert payload["selected_routes"] == ["pdf", "text"]
    assert verify_event_payload(payload) == payload
    assert manifest.digest().startswith("sha256:")

    tampered = dict(payload)
    tampered["budget"] = {"items": 51}
    with pytest.raises(ValueError, match="digest"):
        verify_event_payload(tampered)


def test_lifecycle_envelope_is_bounded_and_json_serializable() -> None:
    envelope = lifecycle_envelope(
        manifest=_manifest().event_payload(),
        status="interrupted",
        routes=({"route_name": "pdf", "status": "interrupted"},),
        errors=({"route_name": "pdf", "error_type": "KeyboardInterrupt"},),
        resumed_from=3,
        replayed=True,
        non_replayable=("image",),
    )

    assert envelope["schema"] == "neocortex.lifecycle-envelope/v1"
    assert envelope["replayed"] is True
    json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def test_framework_state_publishes_manifest_idempotently_and_status_reads_it(
    tmp_path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        template = _manifest()
        payload = RunManifest(
            run_id=run_id,
            run_kind=template.run_kind,
            source_run_id=template.source_run_id,
            root=str(root),
            root_identity=template.root_identity,
            selected_routes=template.selected_routes,
            configuration=template.configuration,
            budget=template.budget,
            input_snapshot=template.input_snapshot,
        ).event_payload()
        assert state.publish_run_manifest(run_id, payload) is True
        assert state.publish_run_manifest(run_id, payload) is False
        assert state.read_run_manifest(run_id) == payload

    status = list_run_status(database, run_id=run_id)[0]
    assert status.manifest == payload
