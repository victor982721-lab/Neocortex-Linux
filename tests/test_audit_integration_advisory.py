from __future__ import annotations

from types import SimpleNamespace

import pytest

from neocortex.api.cli.cli_reporting import has_organization_errors
from neocortex.documents.document_organization_models import OrganizationApplySummary


@pytest.mark.parametrize(
    ("counters", "unresolved"),
    [
        ({"selected": 11, "blocked": 11, "advisory_blocked": 11}, False),
        ({"selected": 11, "blocked": 11, "advisory_blocked": 10}, True),
        ({"selected": 1, "blocked": 1}, True),
        ({"selected": 2, "blocked": 1, "advisory_blocked": 1, "failed": 1}, True),
        ({"selected": 2, "blocked": 1, "advisory_blocked": 1, "stale": 1}, True),
        ({"selected": 1, "cache_pending": 1}, True),
        ({"remaining": 1}, True),
    ],
)
def test_cli_and_lifecycle_share_typed_organization_outcome(
    counters: dict[str, int], unresolved: bool
) -> None:
    summary = OrganizationApplySummary(catalog_run_id=1, **counters)
    result = SimpleNamespace(organization_plan=None, organization_apply=summary)

    assert summary.has_unresolved is unresolved
    assert has_organization_errors(result) is unresolved
    # Advisory classification is not evidence that any physical move occurred.
    assert summary.applied == 0


def test_organization_summary_addition_preserves_old_positional_fields() -> None:
    summary = OrganizationApplySummary(1, 2, 0, 0, 2, 0, 0, 0, 3, 0)

    assert summary.batches == 3
    assert summary.remaining == 0
    assert summary.advisory_blocked == 0
    assert summary.has_unresolved
