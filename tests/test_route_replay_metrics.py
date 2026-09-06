"""Uniform replay counters at the orchestration/status boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

from neocortex.runtime.orchestration.replay_metrics import (
    normalize_route_replay_metrics,
    route_replay_metrics,
)
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.runtime.orchestration.run_status import (
    PhaseStatus,
    RouteStatus,
    RunStatus,
    serialized_run_status,
)


def test_mixed_text_run_reports_new_work_without_false_replay() -> None:
    metrics = normalize_route_replay_metrics(
        "text",
        {"candidates": 20, "processed": 1, "cache_hits": 19, "cached_errors": 0},
        replayability="safe_replay",
    )

    assert metrics["new_work"] == 1
    assert metrics["replay_status"] == "mixed"
    assert metrics["replayability"] == "safe_replay"


def test_cached_error_is_reused_work_not_new_work() -> None:
    metrics = normalize_route_replay_metrics(
        "docx",
        {"candidates": 1, "processed": 1, "cache_hits": 0, "cached_errors": 1},
        replayability="safe_replay",
    )

    assert metrics["new_work"] == 0
    assert metrics["replay_status"] == "replayed"


def test_partial_cache_does_not_claim_full_replay() -> None:
    metrics = normalize_route_replay_metrics(
        "archive",
        {"candidates": 10, "processed": 10, "cache_hits": 8, "cached_errors": 0},
        replayability="safe_replay",
    )

    assert metrics["new_work"] == 2
    assert metrics["replay_status"] == "mixed"


def test_route_adapter_persists_normalized_counters() -> None:
    adapter = RouteAdapter("text", lambda _context: None)
    summary = adapter.summary_mapping(
        {"candidates": 3, "processed": 0, "cache_hits": 3, "cached_errors": 0}
    )

    assert summary["new_work"] == 0
    assert summary["replay_status"] == "replayed"
    assert summary["replayability"] == "safe_replay"


def test_status_serializes_replay_counters() -> None:
    route = RouteStatus(
        route_name="text",
        status="completed",
        current_phase="completed",
        started_ns=1,
        completed_ns=2,
        heartbeat_ns=2,
        error_type=None,
        phases=(
            PhaseStatus("text", "extract", "completed", 1, 2, None),
        ),
        resume_capability="safe_replay",
        candidates=20,
        processed=1,
        cache_hits=19,
        new_work=1,
        cached_errors=0,
        replay_status="mixed",
    )
    status = RunStatus(
        run_id=1,
        run_kind="initial",
        status="completed",
        root="/tmp/fixture",
        source_run_id=None,
        current_phase="completed",
        owner_pid=None,
        owner_alive=None,
        heartbeat_ns=2,
        heartbeat_stale=False,
        started_ns=1,
        completed_ns=2,
        routes=(route,),
    )

    payload = json.loads(serialized_run_status(status))
    serialized = payload["routes"][0]
    assert serialized["candidates"] == 20
    assert serialized["cache_hits"] == 19
    assert serialized["new_work"] == 1
    assert serialized["elapsed_ns"] == 1
    assert serialized["phases"][0]["elapsed_ns"] == 1
    assert payload["elapsed_ns"] == 1
    assert payload["routes"][0]["elapsed_ns"] == 1
    assert payload["lifecycle"]["routes"][0]["elapsed_ns"] == 1
    assert serialized["replayability"] == "safe_replay"
    assert serialized["replay_status"] == "mixed"
    assert payload["lifecycle"]["routes"][0]["new_work"] == 1


def test_object_summary_adapter_accepts_slots_without_false_cache_hits() -> None:
    summary = SimpleNamespace(candidates=4, processed=4, cache_hits=4, cached_errors=0)
    metrics = route_replay_metrics("archive", summary)
    assert metrics["new_work"] == 0
    assert metrics["replay_status"] == "replayed"
