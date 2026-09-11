"""Archive identification-only diagnostics stay visible without forcing RC2."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from neocortex.api.cli import cli_reporting
from neocortex.capabilities.formats.archive.models import ArchiveRouteSummary
from tests.test_archive_logical_diagnostics import _odf, _run


def _archive_summary(**overrides: object) -> ArchiveRouteSummary:
    values: dict[str, object] = {
        "candidates": 3,
        "processed": 3,
        "cache_hits": 3,
        "cached_errors": 0,
        "containers_complete": 3,
        "containers_partial": 0,
        "errors": 0,
        "safety_issues": 2,
    }
    values.update(overrides)
    return ArchiveRouteSummary(**values)


def _result(summary: object) -> SimpleNamespace:
    return SimpleNamespace(route_results={"archive": summary})


def test_complete_archive_keeps_raw_issue_visible_but_not_strict_coverage(capsys) -> None:
    summary = _archive_summary()

    assert cli_reporting._route_issue_count(summary) == 2
    assert not cli_reporting.has_strict_route_errors(_result(summary))

    cli_reporting._print_catalog_reports(_result(summary))
    replay = capsys.readouterr().out
    assert "ROUTE_REPLAY route=archive" in replay
    assert "new_work=0 evidence=observado" in replay
    assert "ROUTE_COVERAGE route=archive" not in replay

    cli_reporting._print_archive_report(SimpleNamespace(archive=summary))
    assert "safety_issues=2" in capsys.readouterr().out


@pytest.mark.parametrize(
    "overrides",
    (
        {"containers_partial": 1},
        {"errors": 1},
        {"cached_errors": 1},
        {"processed": 0, "containers_complete": 0},
        {"containers_complete": True},
        {"catalog_source_missing": 1},
        {"catalog_errors": 1},
    ),
)
def test_archive_unknown_or_degraded_coverage_remains_strict(overrides: dict[str, object]) -> None:
    summary = _archive_summary(**overrides)

    assert cli_reporting.has_strict_route_errors(_result(summary))


def test_archive_legacy_summary_without_classification_remains_strict() -> None:
    summary = SimpleNamespace(
        candidates=3,
        processed=3,
        cache_hits=3,
        cached_errors=0,
        safety_issues=1,
    )

    assert cli_reporting.has_strict_route_errors(_result(summary))


def test_identification_only_real_archive_route_is_not_a_content_failure(
    tmp_path: Path, capsys,
) -> None:
    source = tmp_path / "logical-document.zip"
    source.write_bytes(_odf())
    summary = _run(tmp_path / "archive.sqlite3", source)
    assert summary.safety_issues == 1
    assert summary.processed == summary.containers_complete == 1
    assert summary.containers_partial == summary.errors == 0
    assert cli_reporting._route_issue_count(summary) == 1
    assert not cli_reporting.has_strict_route_errors(_result(summary))
    cli_reporting._print_archive_report(SimpleNamespace(archive=summary))
    assert "safety_issues=1" in capsys.readouterr().out


def test_non_archive_safety_diagnostics_remain_strict() -> None:
    summary = _archive_summary()
    assert cli_reporting.has_strict_route_errors(SimpleNamespace(route_results={"custom": summary}))
