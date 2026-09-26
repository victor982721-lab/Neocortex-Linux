"""Organization apply denial classification and bounded aggregation contracts."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

import neocortex.documents.document_organization_application as application
from neocortex.documents.document_catalog import document_catalog_database
from neocortex.documents.document_organization_models import (
    OrganizationApplySummary,
    organization_apply_has_unresolved,
)
from tests.test_document_organization_scope import _catalog, _file, _plan, _seed


class _NoopMutationGuard:
    def reject_run_mutation(self) -> None:
        return None


def _summary_from_denial(
    catalog_path: Path,
    *,
    denial: str | None = None,
) -> tuple[OrganizationApplySummary, str]:
    with document_catalog_database(catalog_path) as connection:
        row = connection.execute(
            "SELECT * FROM organization_plans ORDER BY plan_id LIMIT 1"
        ).fetchone()
        assert row is not None
        if denial is not None:
            detail = denial
        else:
            denials = application._organization_execution_denials(connection, [row])
            detail = next(iter(denials.values()))
        outcome = application._record_protected_organization_plan(connection, row, detail)
        counters = application._OrganizationApplyCounters()
        counters.record(outcome)
        return counters.summary(run_id=1, selected=1, remaining=0), detail


def test_advisory_backend_denials_are_counted_but_not_unresolved(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    source = _file(tmp_path / "corpus", "standard.pdf")
    _seed(catalog, source)
    _plan(catalog, source.parent, tmp_path / "destination")

    first, first_reason = _summary_from_denial(catalog)
    assert first_reason == "organization_plan_advisory_only"
    assert (first.blocked, first.advisory_blocked) == (1, 1)
    assert not first.has_unresolved
    assert asdict(first)["advisory_blocked"] == 1

    # A validated, executable-looking row reaches the backend-availability
    # denial only after scope and resource assessment have succeeded.
    with document_catalog_database(catalog) as connection:
        connection.execute(
            "UPDATE organization_plans SET status='planned',executable=1,blockers_json='[]'"
        )
        connection.commit()
        row = connection.execute("SELECT * FROM organization_plans LIMIT 1").fetchone()
        assert row is not None
        denials = application._organization_execution_denials(connection, [row])
        assert set(denials.values()) == {"organization_authorized_backend_unavailable"}

    second, second_reason = _summary_from_denial(
        catalog,
        denial="organization_authorized_backend_unavailable",
    )
    assert second_reason == "organization_authorized_backend_unavailable"
    assert (second.blocked, second.advisory_blocked) == (1, 1)
    assert not organization_apply_has_unresolved(second)


@pytest.mark.parametrize(
    "mutation,expected_reason",
    [
        (
            lambda connection: connection.execute(
                "UPDATE organization_plans SET source_scope_json=NULL,source_scope_id=NULL"
            ),
            "legacy_unscoped_organization_plan",
        ),
        (
            lambda connection: connection.execute(
                "UPDATE organization_plans SET source_scope_json='{}'"
            ),
            "organization_contract_invalid:ValueError",
        ),
    ],
)
def test_invalid_or_uncertain_denials_remain_unresolved(
    tmp_path: Path,
    mutation,
    expected_reason: str,
) -> None:
    catalog = _catalog(tmp_path)
    source = _file(tmp_path / "corpus", "standard.pdf")
    _seed(catalog, source)
    _plan(catalog, source.parent, tmp_path / "destination")
    with document_catalog_database(catalog) as connection:
        mutation(connection)
        connection.commit()
        row = connection.execute("SELECT * FROM organization_plans LIMIT 1").fetchone()
        assert row is not None
        denials = application._organization_execution_denials(connection, [row])
        assert set(denials.values()) == {expected_reason}

    summary, reason = _summary_from_denial(catalog, denial=expected_reason)
    assert reason == expected_reason
    assert (summary.blocked, summary.advisory_blocked) == (1, 0)
    assert summary.has_unresolved


@pytest.mark.parametrize(
    ("first", "second", "expected_advisory", "expected_unresolved"),
    [
        (
            OrganizationApplySummary(
                catalog_run_id=1, selected=2, blocked=2, advisory_blocked=2, remaining=1,
            ),
            OrganizationApplySummary(
                catalog_run_id=2, selected=1, blocked=1, advisory_blocked=1,
            ),
            3,
            False,
        ),
        (
            OrganizationApplySummary(
                catalog_run_id=1, selected=2, blocked=2, advisory_blocked=1, remaining=1,
            ),
            OrganizationApplySummary(
                catalog_run_id=2, selected=1, blocked=1,
            ),
            1,
            True,
        ),
    ],
)
def test_apply_batch_aggregation_preserves_advisory_count_and_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: OrganizationApplySummary,
    second: OrganizationApplySummary,
    expected_advisory: int,
    expected_unresolved: bool,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"fixture")
    values = iter((first, second))
    monkeypatch.setattr(application, "_organization_actionable_count", lambda *_args: 3)
    monkeypatch.setattr(application, "apply_document_organization", lambda *_args, **_kwargs: next(values))

    summary = application.apply_all_document_organization(
        catalog,
        tmp_path / "destination",
        mutation_guard=_NoopMutationGuard(),
        batch_size=2,
    )

    assert summary.blocked == first.blocked + second.blocked
    assert summary.advisory_blocked == expected_advisory
    assert 0 <= summary.advisory_blocked <= summary.blocked
    assert summary.has_unresolved is expected_unresolved
