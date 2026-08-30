"""Fail-closed eligibility for deterministic review of self-analysis state."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from .code_review_models import ReviewFreshness
from neocortex.workflow.self_analysis.self_analysis_status import SelfAnalysisStatus


def _manifest_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"self-analysis manifest {label} must be an object")
    return value


def code_review_eligibility(
    status: SelfAnalysisStatus | None,
) -> tuple[str | None, ReviewFreshness | None, str | None]:
    """Return the exact abstention or portable-publication eligibility state."""

    if status is None:
        return "self_analysis_manifest_missing", None, None
    if status.manifest_status != "valid" or status.manifest is None:
        return f"self_analysis_manifest_{status.manifest_status}", None, None
    freshness = status.freshness
    if not freshness.root_identity_current:
        return "self_analysis_root_identity_not_current", None, None
    if not freshness.framework_link_current:
        return "self_analysis_framework_link_not_current", None, None
    inventory = _manifest_mapping(status.manifest.get("inventory"), "inventory")
    journal = _manifest_mapping(inventory.get("journal"), "inventory journal")
    if freshness.current:
        return None, "current", None
    if freshness.journal_status == "unavailable":
        if inventory.get("mode") == "full" and journal.get("status") == "unavailable":
            return (
                None,
                "publication_only",
                ("live_tree_freshness_not_proven_without_journal"),
            )
        return "self_analysis_journal_probe_unavailable", None, None
    if freshness.journal_status == "advanced":
        return "self_analysis_journal_advanced", None, None
    if freshness.journal_status == "discontinuous":
        return "self_analysis_journal_discontinuous", None, None
    if not freshness.inventory_checkpoint_current:
        return "self_analysis_inventory_checkpoint_not_current", None, None
    return "self_analysis_not_current", None, None


def self_analysis_manifest_root(status: SelfAnalysisStatus) -> str:
    """Read the validated root used to interpret paths and source roles."""

    if status.manifest is None:
        raise ValueError("self-analysis manifest is unavailable")
    run = _manifest_mapping(status.manifest.get("run"), "run")
    root = run.get("root")
    if not isinstance(root, str) or not root:
        raise ValueError("self-analysis manifest root must be a non-empty string")
    return root


__all__ = ["code_review_eligibility", "self_analysis_manifest_root"]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_review_eligibility")
