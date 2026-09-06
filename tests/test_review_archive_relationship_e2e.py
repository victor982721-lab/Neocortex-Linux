"""Actual Archive writer -> existing refresh -> automatic published grouping."""

from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from neocortex.capabilities.formats.archive.state import list_archive_issues, list_archive_members
from neocortex.workflow.review.review_service import ReviewService
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject, ReviewTaskActorKind, ReviewTaskState, ReviewTaskTransition,
)
from neocortex.workflow.review.review_task_query import ReviewTaskReadQuery, query_current_review_tasks
from neocortex.workflow.review.review_task_repository import append_review_task_event, read_review_task_history
from neocortex.workflow.review.value_review_contracts import ValueReviewPaths
from neocortex.runtime.control.cancellation import CancellationRequested
import pytest
from test_archive_logical_diagnostics import OTT_MIME, _odf, _run, _zip


NOW = 1_800_000_000_000_000_000


def _archive(tmp_path: Path, *, logical: bool = True, issues: bool = True) -> tuple[Path, Path]:
    archive = tmp_path / "archive.sqlite3"
    source = tmp_path / ("template.ott" if logical else "lookalike.zip")
    if not issues:
        source.write_bytes(_odf())
    else:
        entries: dict[str, str] = {
            "content.xml": "<document>" + "source evidence " * 200 + "</document>",
            "styles.xml": "<styles>" + "format " * 500 + "</styles>",
            "META-INF/manifest.xml": "<manifest/>",
        }
        if logical:
            entries["mimetype"] = OTT_MIME
        source.write_bytes(_zip(entries))
    _run(archive, source, max_member_bytes=1000)
    return archive, source


def _refresh(tmp_path: Path, *, now: int = NOW):
    return ReviewService().refresh_value_review_tasks(
        tmp_path / "framework.sqlite3", ValueReviewPaths.from_directory(tmp_path),
        scope="personal", clock_ns=lambda: now,
    )


def test_actual_component_findings_group_through_existing_review_service(tmp_path: Path) -> None:
    archive, source = _archive(tmp_path)
    source_bytes = source.read_bytes()
    owner_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    issues = list_archive_issues(archive).items
    assert len(issues) == 2 and {item.reason_code for item in issues} == {"archive_member_size_limit"}
    assert sum(item.document_role == "document_component" for item in list_archive_members(archive)) == 4
    refresh = _refresh(tmp_path)
    assert refresh.related_sources[0].status == "complete"
    assert refresh.related_sources[0].wrote_state
    database = tmp_path / "framework.sqlite3"
    result = query_current_review_tasks(database, ReviewTaskReadQuery(scope="personal"))
    payload = result.to_dict()
    assert payload["schema"] == "neocortex.review-task-query/v2"
    assert payload["counts"]["returned"] == 2  # type: ignore[index]
    assert payload["counts"]["grouped_members_in_page"] == 2  # type: ignore[index]
    assert len(payload["groups"]) == 1  # type: ignore[arg-type]
    assert payload["groups"][0]["member_count"] == 2  # type: ignore[index]
    assert payload["relationship_coverage"]["complete"] is True  # type: ignore[index]
    assert all(record.source.resource.physical_identity is None for record in result.page.items)  # type: ignore[union-attr]
    assert all(record.current_event.decision is None for record in result.page.items)
    queue = ReviewService().read_value_review_task_queue(database, ValueReviewPaths.from_directory(tmp_path),
                                                       scope="personal", limit=20, reference_time_ns=NOW)
    assert queue.report_dict()["related_review_tasks"] == payload
    before = database.read_bytes()
    assert query_current_review_tasks(database, ReviewTaskReadQuery(scope="personal")).to_dict() == payload
    assert database.read_bytes() == before
    replay = _refresh(tmp_path, now=NOW + 1)
    assert not replay.wrote_state and not replay.related_sources[0].wrote_state
    assert query_current_review_tasks(database, ReviewTaskReadQuery(scope="personal")).to_dict() == payload
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == owner_hash
    assert source.read_bytes() == source_bytes


def test_clean_components_do_not_create_review_findings(tmp_path: Path) -> None:
    archive, _ = _archive(tmp_path, issues=False)
    assert not list_archive_issues(archive).items
    refresh = _refresh(tmp_path)
    assert refresh.related_sources[0].publication is not None
    assert refresh.related_sources[0].publication.task_ids == ()
    result = query_current_review_tasks(tmp_path / "framework.sqlite3", ReviewTaskReadQuery())
    assert result.page.items == () and result.to_dict()["groups"] == []


def test_matching_names_without_logical_owner_proof_stay_singletons(tmp_path: Path) -> None:
    _archive(tmp_path, logical=False)
    _refresh(tmp_path)
    payload = query_current_review_tasks(tmp_path / "framework.sqlite3", ReviewTaskReadQuery()).to_dict()
    assert payload["counts"]["returned"] == 2  # type: ignore[index]
    assert payload["counts"]["groups_in_page"] == 2  # type: ignore[index]
    assert payload["relationship_coverage"]["complete"] is False  # type: ignore[index]
    assert {item["reason"] for item in payload["relationship_coverage"]["unresolved"]} == {  # type: ignore[index,union-attr]
        "logical_document_relationship_unproved",
    }


def test_changed_owner_makes_old_relationships_stale_without_changing_tasks(tmp_path: Path) -> None:
    archive, source = _archive(tmp_path)
    _refresh(tmp_path)
    database = tmp_path / "framework.sqlite3"
    before = database.read_bytes()
    with closing(sqlite3.connect(archive)) as connection, connection:
        connection.execute("UPDATE containers SET processing_signature=processing_signature || '-changed'")
    payload = query_current_review_tasks(database, ReviewTaskReadQuery()).to_dict()
    assert payload["counts"]["groups_in_page"] == 2  # type: ignore[index]
    assert {item["reason"] for item in payload["relationship_coverage"]["unresolved"]} == {  # type: ignore[index,union-attr]
        "archive_relationship_source_stale",
    }
    assert database.read_bytes() == before and source.exists()


def test_human_decision_is_preserved_by_grouping_and_refresh(tmp_path: Path) -> None:
    archive, source = _archive(tmp_path)
    _refresh(tmp_path)
    database = tmp_path / "framework.sqlite3"
    record = query_current_review_tasks(database, ReviewTaskReadQuery()).page.items[0]
    decision = CanonicalJsonObject.from_mapping({"decision": "retain_for_review", "fixture": True})
    transition = ReviewTaskTransition(
        "human:archive:1", "human:archive:1", record.task.task_id, record.current_event.event_id,
        record.state, ReviewTaskState.RESOLVED, ReviewTaskActorKind.HUMAN, "fixture-reviewer",
        CanonicalJsonObject.from_mapping({"kind": "explicit_fixture_decision"}), decision,
        None, NOW + 10, NOW + 10,
    )
    append_review_task_event(database, transition)
    history = read_review_task_history(database, record.task.task_id)
    all_states = ReviewTaskReadQuery(states=(ReviewTaskState.OPEN, ReviewTaskState.RESOLVED))
    payload = query_current_review_tasks(database, all_states).to_dict()
    assert payload["counts"]["grouped_members_in_page"] == 2  # type: ignore[index]
    _refresh(tmp_path, now=NOW + 20)
    assert read_review_task_history(database, record.task.task_id) == history
    assert history[-1].decision == decision
    _run(archive, source, max_member_bytes=1000)
    _refresh(tmp_path, now=NOW + 30)
    assert read_review_task_history(database, record.task.task_id) == history


def test_refresh_resumes_pages_and_query_does_not_publish_partial_groups(tmp_path: Path) -> None:
    source = tmp_path / "large.ott"
    entries = {"mimetype": OTT_MIME, "META-INF/manifest.xml": "<manifest/>",
               "content.xml": "x" * 1100}
    entries.update({f"part-{index}.xml": "x" * 1100 for index in range(104)})
    source.write_bytes(_zip(entries))
    archive = tmp_path / "archive.sqlite3"
    _run(archive, source, max_member_bytes=1000)
    assert len(list_archive_issues(archive, limit=200).items) == 105
    first = _refresh(tmp_path)
    assert first.related_sources[0].status == "partial"
    assert first.related_sources[0].publication.progress.scanned_count == 100  # type: ignore[union-attr]
    database = tmp_path / "framework.sqlite3"
    staged = query_current_review_tasks(database, ReviewTaskReadQuery(limit=100)).to_dict()
    assert staged["counts"]["grouped_members_in_page"] == 0  # type: ignore[index]
    assert staged["counts"]["total_matching"] is None  # type: ignore[index]
    assert staged["complete"] is False
    assert {item["relationship_evidence_state"] for item in staged["items"]} == {"staged"}  # type: ignore[union-attr]
    second = _refresh(tmp_path, now=NOW + 1)
    assert second.related_sources[0].status == "complete"
    assert second.related_sources[0].publication.progress.scanned_count == 105  # type: ignore[union-attr]
    first_page = query_current_review_tasks(database, ReviewTaskReadQuery(limit=100))
    assert first_page.page.has_more
    assert first_page.to_dict()["counts"]["grouped_members_in_page"] == 100  # type: ignore[index]
    last_page = query_current_review_tasks(database, ReviewTaskReadQuery(limit=100, after=first_page.page.next_cursor))
    assert len(last_page.page.items) == 5 and not last_page.page.has_more
    assert last_page.to_dict()["groups"][0]["complete"] is False  # type: ignore[index]
    assert len({record.task.task_id for record in (*first_page.page.items, *last_page.page.items)}) == 105


def test_cancelled_existing_refresh_does_not_create_framework_state(tmp_path: Path) -> None:
    archive, _ = _archive(tmp_path)
    before = archive.read_bytes()

    def cancel() -> None:
        raise CancellationRequested("fixture cancelled")

    with pytest.raises(CancellationRequested):
        ReviewService().refresh_value_review_tasks(
            tmp_path / "framework.sqlite3", ValueReviewPaths.from_directory(tmp_path),
            scope="personal", clock_ns=lambda: NOW, cancellation_check=cancel,
        )
    assert not (tmp_path / "framework.sqlite3").exists()
    assert archive.read_bytes() == before


def test_missing_final_publication_never_upgrades_stored_members_to_a_proved_group(tmp_path: Path) -> None:
    _archive(tmp_path)
    _refresh(tmp_path)
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        # Simulate an already damaged private fixture, restoring the original
        # schema before the product reader sees it; normal writes cannot delete
        # this append-only publication.
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='review_task_source_publications' AND lower(sql) LIKE '%before delete%'"
        ).fetchall()
        assert triggers
        for name, _ in triggers:
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        connection.execute("DELETE FROM review_task_source_publications")
        for _, sql in triggers:
            connection.execute(sql)
    payload = query_current_review_tasks(database, ReviewTaskReadQuery()).to_dict()
    assert payload["counts"]["grouped_members_in_page"] == 0  # type: ignore[index]
    assert payload["counts"]["total_matching"] is None  # type: ignore[index]
    assert {item["reason"] for item in payload["relationship_coverage"]["unresolved"]} == {  # type: ignore[index,union-attr]
        "archive_review_source_publication_missing",
    }
    refresh = _refresh(tmp_path, now=NOW + 1)
    assert refresh.related_sources[0].status == "unavailable"
    assert not refresh.related_sources[0].wrote_state
