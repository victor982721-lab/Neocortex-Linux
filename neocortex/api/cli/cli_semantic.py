"""Flat Semantic CLI boundary.

Argument parsing lives in :mod:`cli_parser`/``cli_semantic_surface``.  The
Semantic application workflow lives in ``neocortex.semantic.semantic_application``;
this module deliberately keeps the established import surface for dispatchers
and direct callers while forwarding the implementation to that owner.
"""

from __future__ import annotations

from functools import wraps
import inspect
from typing import Any

from neocortex.semantic import semantic_application as _application


# The public flat CLI has historically exposed these handlers, and a few
# focused tests/callers use the private seams to inject bounded fixtures.  Keep
# forwarding wrappers rather than duplicating application logic here.  The
# synchronization step makes those explicit seams continue to work while the
# implementation is owned by Semantic.
_ORIGINALS: dict[str, object] = {}
_WRAPPERS: dict[str, object] = {}


def _sync_application_seams() -> None:
    """Copy explicitly patched facade seams into the Semantic owner module."""

    namespace = globals()
    for name, original in _ORIGINALS.items():
        current = namespace.get(name, original)
        # Restore the owner to its original callable after a fixture seam is
        # undone, and install the replacement while that seam is active.
        # Without the explicit restore, a monkeypatch on the facade would
        # leak into later direct operations because the owner keeps its own
        # module globals.
        setattr(_application, name, original if current is _WRAPPERS.get(name) else current)


def _forward(name: str):
    target = getattr(_application, name)

    if inspect.isclass(target):
        _ORIGINALS[name] = target
        return target

    @wraps(target)
    def forwarded(*args: Any, **kwargs: Any) -> Any:
        _sync_application_seams()
        return getattr(_application, name)(*args, **kwargs)

    _ORIGINALS[name] = target
    _WRAPPERS[name] = forwarded
    return forwarded


# Forward every implementation symbol used by the public dispatcher or by
# existing direct callers.  Keeping the list explicit makes this boundary
# reviewable and prevents accidental exposure of arbitrary Semantic internals.
_FORWARD_NAMES = (
    "_SemanticSearchKeywordArgs",
    "_SemanticIndexExecution",
    "_PendingIntegratedMetadata",
    "_IntegratedStartReadBudget",
    "_console_text",
    "_print_console_line",
    "_semantic_text_model",
    "_persisted_semantic_admission_policy",
    "_validate_semantic_state_write",
    "_semantic_failure",
    "_selected_semantic_text_sources",
    "_print_semantic_index_result",
    "_bounded_framework_metadata_read",
    "_exact_index_json",
    "_print_exact_index_result",
    "_print_exact_index_usage",
    "_close_exact_index_handle",
    "_semantic_cancellation_checkpoint",
    "run_semantic_exact_index_build",
    "_open_exact_index_for_search",
    "run_semantic_status",
    "run_semantic_plan",
    "run_semantic_prepare_models",
    "run_semantic_index",
    "_validate_integrated_publication_token",
    "_observe_integrated_heads",
    "_integrated_semantic_budget",
    "_execute_semantic_index_scopes",
    "_execute_semantic_text_index",
    "_execute_semantic_image_index",
    "_record_semantic_index_result",
    "_semantic_index_failure",
    "_complete_semantic_index_execution",
    "_semantic_index_result_failed",
    "_publication_observation",
    "_semantic_stage_for_resume",
    "semantic_resume_available",
    "_semantic_resume_args",
    "_stored_publication_owners",
    "_integrated_publication_owners",
    "_validate_integrated_manifest_root",
    "_read_pending_integrated_metadata",
    "_observe_fresh_integrated_heads",
    "_fresh_integrated_checkpoint",
    "prepare_integrated_semantic_start",
    "_pending_integrated_source_run",
    "recover_pending_integrated_semantic",
    "_recover_pending_integrated_semantic",
    "_recover_pending_integrated_publication",
    "_integrated_stage_details",
    "_record_integrated_semantic_stage",
    "_begin_integrated_publication",
    "_final_publication_owner_heads",
    "_semantic_results_ready",
    "_record_integrated_semantic_work",
    "_resolve_integrated_publication_after_nonterminal",
    "_select_integrated_sources",
    "run_integrated_all_semantic_index",
    "run_semantic_search",
    "_run_semantic_search_with_handle",
    "run_semantic_image_calibrate",
    "run_semantic_classify",
    "run_semantic_evidence",
)

for _name in _FORWARD_NAMES:
    globals()[_name] = _forward(_name)

# Static analyzers cannot infer the explicit forwarding loop above; retain
# typed aliases for the public dispatcher while the implementation remains
# owned by ``semantic_application``.
semantic_resume_available = _application.semantic_resume_available
run_integrated_all_semantic_index = _application.run_integrated_all_semantic_index
prepare_integrated_semantic_start = _application.prepare_integrated_semantic_start

__all__ = list(_application.__all__)

# A small set of implementation-level imports is part of the established
# direct-call test seam.  They are aliases, not a second owner; the wrappers
# above synchronize any explicit monkeypatch before invoking the application.
for _name in ("StatePublicationRecoveryRequired", "time", "sqlite3", "math", "json", "sys"):
    if hasattr(_application, _name):
        globals()[_name] = getattr(_application, _name)
        _ORIGINALS[_name] = globals()[_name]


del _name, _forward, _FORWARD_NAMES
