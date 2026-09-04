"""Focused contract tests for the additive Review service boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from neocortex.workflow.review import review as review_impl
from neocortex.workflow.review import review_task_repository as task_repository
from neocortex.workflow.review import value_review as value_impl
from neocortex.workflow.review import value_review_tasks as task_impl
from neocortex.workflow.review.review_service import ReviewReader, ReviewService
from neocortex.workflow.review.value_review_contracts import (
    ValueReviewAvailability,
)
from neocortex.workflow.review import value_review_port as legacy_port


def test_review_service_cold_import_does_not_load_owner_implementations() -> None:
    script = """
import sys
import neocortex.workflow.review.review_service

for name in (
    "neocortex.workflow.review.review",
    "neocortex.workflow.review.review_task_repository",
    "neocortex.workflow.review.value_review",
    "neocortex.workflow.review.value_review_repository",
    "neocortex.workflow.review.value_review_tasks",
):
    if name in sys.modules:
        raise SystemExit("eager Review implementation import: " + name)
print("REVIEW_SERVICE_IMPORT_LIGHT")
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "REVIEW_SERVICE_IMPORT_LIGHT"


def test_review_service_is_a_read_contract_and_forwards_candidate_filters() -> None:
    service = ReviewService()
    assert isinstance(service, ReviewReader)
    sentinel = object()
    with patch.object(review_impl, "list_review_candidates", return_value=sentinel) as reader:
        result = service.list_review_candidates(
            Path("/tmp/framework.sqlite3"),
            limit=7,
            route_name="pdf",
            recommendation="manual_review",
            status="open",
        )

    assert result is sentinel
    reader.assert_called_once_with(
        Path("/tmp/framework.sqlite3"),
        limit=7,
        route_name="pdf",
        recommendation="manual_review",
        status="open",
    )


def test_review_service_forwards_value_reader_and_task_reader_without_rebinding() -> None:
    service = ReviewService()
    paths = object()
    query = object()
    database = Path("/tmp/framework.sqlite3")
    sentinel_report = object()
    sentinel_queue = object()
    sentinel_task = object()

    with (
        patch.object(value_impl, "preview_value_review", return_value=sentinel_report) as preview,
        patch.object(task_impl, "read_value_review_task_queue", return_value=sentinel_queue) as queue,
        patch.object(task_repository, "read_review_task", return_value=sentinel_task) as task,
    ):
        assert service.preview_value_review(paths, query) is sentinel_report  # type: ignore[arg-type]
        assert service.read_value_review_task_queue(
            database,
            paths,  # type: ignore[arg-type]
            scope="personal",
            limit=3,
            reference_time_ns=11,
        ) is sentinel_queue
        assert service.read_review_task(database, "task-1") is sentinel_task

    preview.assert_called_once_with(paths, query)
    queue.assert_called_once_with(
        database,
        paths,
        scope="personal",
        limit=3,
        reference_time_ns=11,
        cancellation_check=None,
    )
    task.assert_called_once_with(database, "task-1", cancellation_check=None)


def test_legacy_value_port_routes_compatibility_calls_through_service() -> None:
    paths = object()
    query = object()
    sentinel = object()
    with patch.object(legacy_port._DEFAULT_REVIEW_SERVICE, "preview_value_review", return_value=sentinel) as method:
        result = legacy_port.preview_value_review(paths, query)  # type: ignore[arg-type]

    assert result is sentinel
    method.assert_called_once_with(paths, query)


def test_review_service_forwards_partial_value_ranking_metadata() -> None:
    service = ReviewService()
    sentinel = object()
    observations: tuple[object, ...] = ()
    with patch.object(value_impl, "rank_value_observations", return_value=sentinel) as rank:
        result = service.rank_value_observations(
            observations,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            availability=ValueReviewAvailability.PARTIAL,
            complete=False,
            reason="source_changed",
        )

    assert result is sentinel
    rank.assert_called_once_with(
        observations,
        rank.call_args.args[1],
        availability=ValueReviewAvailability.PARTIAL,
        complete=False,
        reason="source_changed",
        provenance=(),
        uncertainties=(),
    )
