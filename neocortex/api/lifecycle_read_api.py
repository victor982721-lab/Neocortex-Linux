"""Bounded read-only lifecycle status for API/MCP consumers."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from neocortex.api.run_lifecycle import read_run_status
from neocortex.runtime.config.app_paths import default_state_directory
from neocortex.runtime.orchestration.run_status import serialized_run_status


def lifecycle_status_payload(
    *,
    limit: int = 5,
    run_id: int | None = None,
    state_directory: str | Path | None = None,
) -> dict[str, object]:
    """Return one bounded lifecycle envelope without starting a run."""

    if not 1 <= limit <= 20:
        raise ValueError("lifecycle status limit must be between 1 and 20")
    if run_id is not None and (isinstance(run_id, bool) or run_id < 1):
        raise ValueError("lifecycle status run_id must be positive")
    state = default_state_directory() if state_directory is None else Path(state_directory)
    database = state / "framework.sqlite3"
    request_id = f"lifecycle-{uuid4().hex}"
    if not database.is_file():
        return {
            "schema": "neocortex.lifecycle-envelope/v1",
            "kind": "neocortex_lifecycle_status",
            "operation": "lifecycle_status",
            "request_id": request_id,
            "read_only": True,
            "coverage": "unavailable",
            "status": "unavailable",
            "exit_code": 1,
            "error": {"code": "state_unavailable", "message": "framework state is absent"},
            "state_directory": str(state),
            "limit": limit,
            "run_id": run_id,
            "runs": [],
            "result": {"count": 0, "run_ids": []},
            "lifecycle": {
                "schema": "neocortex.lifecycle-envelope/v1",
                "status": "unavailable",
                "run_id": run_id,
                "source_run_id": None,
                "manifest_digest": None,
                "resumed_from": None,
                "resumed": False,
                "replayed": False,
                "skipped": [],
                "non_replayable": [],
                "budget": None,
                "recovery": None,
                "routes": [],
                "errors": [],
            },
        }
    statuses = read_run_status(database, limit=limit, run_id=run_id)
    runs = [json.loads(serialized_run_status(status)) for status in statuses]
    return {
        "schema": "neocortex.lifecycle-envelope/v1",
        "kind": "neocortex_lifecycle_status",
        "operation": "lifecycle_status",
        "request_id": request_id,
        "read_only": True,
        "coverage": "complete",
        "status": "ok",
        "exit_code": 0,
        "error": None,
        "state_directory": str(state),
        "limit": limit,
        "run_id": run_id,
        "runs": runs,
        "result": {"count": len(runs), "run_ids": [run["run_id"] for run in runs]},
        "lifecycle": {
            "schema": "neocortex.lifecycle-envelope/v1",
            "status": "ok",
            "run_id": run_id,
            "source_run_id": None,
            "manifest_digest": None,
            "resumed_from": None,
            "resumed": any(bool(run.get("resumed")) for run in runs),
            "replayed": any(bool(run.get("replayed")) for run in runs),
            "skipped": [],
            "non_replayable": [],
            "budget": None,
            "recovery": None,
            "routes": [],
            "errors": [],
        },
    }


__all__ = ["lifecycle_status_payload"]
