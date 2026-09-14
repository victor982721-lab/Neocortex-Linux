"""Public exact-deduplication service used by the installed CLI.

The service intentionally reuses :class:`FrameworkOrchestrator` with no
content routes.  This gives ``--dedupe`` the same inventory, keeper, action
ledger and recovery boundaries as ``--all`` without importing or starting
OCR, transcription, Semantic or Knowledge workers.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Mapping


def run_dedupe(args) -> Mapping[str, object]:
    """Run one exact duplicate plan through the existing Framework lifecycle.

    ``args`` is the validated CLI namespace.  The function returns a bounded
    JSON-compatible mapping; it never falls back to a second inventory engine.
    """

    from neocortex.api.cli.cli_config import framework_config_from_args
    from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator

    config = framework_config_from_args(args)
    config = replace(
        config,
        route="none",
        dedup_policy="exact",
        apply_actions=bool(getattr(args, "apply", False)),
    )
    result = FrameworkOrchestrator(config).run()
    # A dedupe-only invocation always returns InitialRunResult; retain a
    # defensive shape for embedded callers that provide a compatible result.
    scan = getattr(result, "scan", None)
    plan = getattr(result, "dedup_plan", None)
    actions = getattr(result, "actions", None)
    scan_payload = {} if scan is None else asdict(scan)
    plan_payload: dict[str, object] = {}
    if plan is not None:
        statistics = getattr(plan, "statistics", None)
        coverage = getattr(plan, "coverage", None)
        plan_payload = {
            "groups": int(getattr(plan, "group_count", 0)),
            "redundant_files": int(getattr(plan, "redundant_files", 0)),
            "reclaimable_bytes_nominal": int(getattr(plan, "reclaimable_bytes", 0)),
            "requested_policy": str(getattr(plan, "requested_policy", "exact")),
            "verification_mode": str(getattr(plan, "verification_mode", "full_hash")),
            "coverage": str(coverage),
            "statistics": {} if statistics is None else asdict(statistics),
        }
    actions_payload = {} if actions is None else asdict(actions)
    errors = int(actions_payload.get("errors", 0)) if actions_payload else 0
    skipped = int(actions_payload.get("duplicate_skips", 0)) if actions_payload else 0
    apply_requested = bool(getattr(args, "apply", False))
    status = "planned" if not apply_requested else ("partial" if errors or skipped else "complete")
    return {
        "schema": "neocortex.dedupe/v1",
        "operation": "dedupe",
        "status": status,
        "exit_code": 0 if status in {"planned", "complete"} else 4,
        "apply": apply_requested,
        "root": str(getattr(config, "root", getattr(args, "root", ""))),
        "state_directory": str(getattr(config, "state_directory", "")),
        "scan": scan_payload,
        "plan": plan_payload,
        "actions": actions_payload,
        "physical_bytes_in_trash": None,
        "physical_bytes_freed": None,
        "notes": [
            "Papelera no equivale a espacio libre; los bytes físicos se reportan por separado.",
        ],
    }


dedupe = run_dedupe
execute_dedupe = run_dedupe

__all__ = ["dedupe", "execute_dedupe", "run_dedupe"]
