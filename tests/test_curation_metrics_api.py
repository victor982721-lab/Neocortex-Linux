"""Public curation verification responses retain bounded work metrics."""

from __future__ import annotations

from neocortex.api.curation_verification_api import _verification_success


def test_verification_success_preserves_bounded_metrics() -> None:
    plan_id = "sha256:" + "a" * 64
    payload = _verification_success(
        {
            "bytes_checked": 12,
            "coverage": "complete",
            "files_checked": 2,
            "items": [],
            "items_failed": 0,
            "items_skipped": 0,
            "items_total": 0,
            "items_verified": 0,
            "metrics": {"chunks": 2, "keeper_replays": 1},
            "plan_digest": plan_id,
            "snapshot_id": "sha256:" + "b" * 64,
            "source_heads": [],
            "status": "complete",
        },
        request_id="verify-metrics",
        plan_id=plan_id,
        cursor=None,
    )

    assert payload["result"]["metrics"] == {"chunks": 2, "keeper_replays": 1}
