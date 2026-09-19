"""Exact review reads must not materialize unrelated source history."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest

from neocortex.persistence import framework_schema
from neocortex.workflow.review import review_task_repository as repository
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskCoverage,
    ReviewTaskSourceFence,
    ReviewTaskState,
)
from tests.test_review_tasks import (
    _database,
    _drop_trigger,
    _input,
    _publication,
    _publish_numbered_snapshot,
    _task,
)


class _ReviewPageArguments(TypedDict):
    limit: int
    scope: NotRequired[str]
    task_type: NotRequired[str]
    source_snapshot_fingerprint: NotRequired[str]
    source_snapshot_as_published: NotRequired[bool]


@pytest.fixture(params=(False, True), ids=("canonical-unique-order", "reversed-unique-order"))
def ordered_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    if not request.param:
        return _database(tmp_path)
    original = "UNIQUE(logical_key,task_version),\n    UNIQUE(batch_id,source_input_id)"
    reversed_order = "UNIQUE(batch_id,source_input_id),\n    UNIQUE(logical_key,task_version)"
    with monkeypatch.context() as patch:
        patch.setattr(
            framework_schema, "_TABLE_STATEMENTS",
            tuple(statement.replace(original, reversed_order) for statement in framework_schema._TABLE_STATEMENTS),
        )
        database = _database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        framework_schema.validate_framework_schema(connection)
        columns = tuple(row[2] for row in connection.execute("PRAGMA index_info(sqlite_autoindex_review_tasks_2)"))
        assert columns == ("batch_id", "source_input_id")
    return database


def _unrelated_source(database: Path, *, count: int = 400, same_scope: bool = False) -> None:
    for offset in range(0, count, 100):
        scope = "personal" if same_scope else f"unrelated-{offset}"
        publication = _publication(
            10 + offset,
            (),
            confirmed_ns=1_000_000 + offset,
        )
        publication = replace(
            publication,
            fence=ReviewTaskSourceFence.create(
                scope=scope,
                task_type=publication.fence.task_type,
                selector_signature=f"unrelated-{offset}",
                source_snapshot={"generation": 10 + offset, "owner": "inventory"},
            ),
            inputs=tuple(_input(number) for number in range(1_001 + offset, 1_001 + offset + 100)),
            tasks=tuple(
                _task(number, scope=scope)
                for number in range(1_001 + offset, 1_001 + offset + 100)
            ),
        )
        repository.publish_review_task_page(database, publication, expected_progress_revision=None)


def _query_cost(database: Path, statement: str) -> tuple[int, tuple[tuple[object, ...], ...], str]:
    with closing(sqlite3.connect(database)) as connection:
        steps = 0

        def count_step() -> int:
            nonlocal steps
            steps += 1
            return 0

        connection.set_progress_handler(count_step, 1)
        rows = tuple(tuple(row) for row in connection.execute(statement))
        connection.set_progress_handler(None, 0)
        plan = " | ".join(str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + statement))
        return steps, rows, plan


@pytest.mark.parametrize("published", (False, True))
def test_review_page_cost_is_independent_of_unrelated_sources(
    ordered_database: Path, monkeypatch: pytest.MonkeyPatch, published: bool,
) -> None:
    database = ordered_database
    publication = _publication(1, (1,))
    repository.publish_review_task_page(database, publication, expected_progress_revision=None)
    statements: list[str] = []
    original_connect = repository.connect_existing_framework

    def observed_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(repository, "connect_existing_framework", observed_connect)
    arguments: _ReviewPageArguments = {
        "limit": 1,
        "scope": "personal",
        "task_type": "value-review",
        "source_snapshot_fingerprint": publication.fence.source_snapshot_fingerprint,
        "source_snapshot_as_published": published,
    }
    first = repository.list_current_review_tasks(database, **arguments)
    statement = next(sql for sql in statements if "ORDER BY t.priority DESC,t.created_ns,t.task_id LIMIT" in sql)
    before_steps, before_rows, _ = _query_cost(database, statement)
    _unrelated_source(database)
    second = repository.list_current_review_tasks(database, **arguments)
    after_steps, after_rows, plan = _query_cost(database, statement)

    assert second == first
    assert after_rows == before_rows
    print(f"review-page published={published}: before={before_steps}, after={after_steps}; {plan}")
    assert after_steps <= before_steps + 300, plan


@pytest.mark.parametrize("same_scope", (False, True))
def test_exact_review_lookup_cost_is_independent_of_unrelated_sources(
    ordered_database: Path, same_scope: bool,
) -> None:
    database = ordered_database
    repository.publish_review_task_page(database, _publication(1, (1,)), expected_progress_revision=None)
    repository.publish_review_task_page(database, _publication(2, ()), expected_progress_revision=None)

    def observe() -> tuple[int, object]:
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            steps = 0
            statements: list[str] = []
            connection.set_trace_callback(statements.append)

            def count_step() -> int:
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(count_step, 1)
            result = repository._validated_records_by_task_ids(connection, ("task-1-v1",))
            connection.set_progress_handler(None, 0)
            statement = next(sql for sql in statements if "WITH current_events AS" in sql)
            plan = " | ".join(str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + statement))
            print(plan)
            assert "SEARCH replacement" in plan and "(logical_key=?)" in plan
            return steps, result

    before_steps, before = observe()
    _unrelated_source(database, same_scope=same_scope)
    after_steps, after = observe()

    assert after == before
    print(f"review-exact same_scope={same_scope}: before={before_steps}, after={after_steps}")
    assert after_steps <= before_steps + 300


@pytest.mark.parametrize("published", (False, True))
def test_published_review_source_selects_the_exact_predecessor_event(
    tmp_path: Path, published: bool,
) -> None:
    database = _database(tmp_path)
    first = _publication(1, (1,), confirmed_ns=4_000)
    repository.publish_review_task_page(database, first, expected_progress_revision=None)
    expected = repository.list_current_review_tasks(database, limit=10)
    successor = _task(1, task_version=2, supersedes_task_id="task-1-v1", created_ns=4_100)
    staged = _publication(
        2, (1,), tasks=(successor,), confirmed_ns=4_200,
        coverage=ReviewTaskCoverage.COMPLETE if published else ReviewTaskCoverage.PARTIAL,
        cursor_after=None if published else CanonicalJsonObject.from_mapping({"offset": 1}),
    )
    repository.publish_review_task_page(database, staged, expected_progress_revision=None)
    arguments: _ReviewPageArguments = {
        "limit": 10,
        "source_snapshot_as_published": True,
        "source_snapshot_fingerprint": first.fence.source_snapshot_fingerprint,
    }
    if not published:
        assert repository.list_current_review_tasks(database, **arguments) == expected
        return
    assert repository.list_current_review_tasks(database, **arguments).items == ()
    superseded = repository.list_current_review_tasks(
        database, **arguments, states=(ReviewTaskState.SUPERSEDED,),
    )
    assert [record.task.task_id for record in superseded.items] == ["task-1-v1"]
    assert superseded.items[0].current_event.provenance.to_dict()["replacement_task_id"] == "task-1-v2"


@pytest.mark.parametrize("corruption", ("missing_predecessor", "foreign_predecessor", "null_predicate"))
def test_published_review_source_rejects_uncertain_predecessor(
    tmp_path: Path, corruption: str,
) -> None:
    database = _database(tmp_path)
    first = _publication(1, (1, 2), confirmed_ns=4_000)
    repository.publish_review_task_page(database, first, expected_progress_revision=None)
    successor = _task(1, task_version=2, supersedes_task_id="task-1-v1", created_ns=4_100)
    repository.publish_review_task_page(
        database,
        _publication(
            2, (1,), tasks=(successor,), confirmed_ns=4_200,
            coverage=ReviewTaskCoverage.PARTIAL,
            cursor_after=CanonicalJsonObject.from_mapping({"offset": 1}),
        ),
        expected_progress_revision=None,
    )
    with closing(sqlite3.connect(database)) as connection:
        trigger = _drop_trigger(connection, "review_task_events_no_update")
        if corruption == "null_predicate":
            # Missing JSON member makes the old UNION predicate SQL NULL.
            connection.execute(
                "UPDATE review_task_events SET provenance_json='{}' "
                "WHERE task_id='task-1-v1' AND sequence=2"
            )
        else:
            previous = "missing-event"
            if corruption == "foreign_predecessor":
                previous = connection.execute(
                    "SELECT event_id FROM review_task_events WHERE task_id='task-2-v1'"
                ).fetchone()[0]
            connection.execute(
                "UPDATE review_task_events SET previous_event_id=? "
                "WHERE task_id='task-1-v1' AND sequence=2", (previous,),
            )
        connection.execute(trigger)
        connection.commit()

    with pytest.raises(repository.ReviewTaskRepositoryError):
        repository.list_current_review_tasks(
            database, limit=10, source_snapshot_as_published=True,
            source_snapshot_fingerprint=first.fence.source_snapshot_fingerprint,
        )


def test_published_review_source_maximum_page_validates_the_lookahead_record(tmp_path: Path) -> None:
    database = _database(tmp_path)
    publication = _publish_numbered_snapshot(
        database, generation=1, count=101, confirmed_base=4_000,
    )
    arguments: _ReviewPageArguments = {
        "limit": 100,
        "source_snapshot_as_published": True,
        "source_snapshot_fingerprint": publication.fence.source_snapshot_fingerprint,
    }
    first = repository.list_current_review_tasks(database, **arguments)
    second = repository.list_current_review_tasks(database, **arguments, after=first.next_cursor)
    assert repository.list_current_review_tasks(database, **arguments) == first
    assert len(first.items) == 100 and first.has_more
    assert len(second.items) == 1 and not second.has_more
    assert len({record.task.task_id for record in (*first.items, *second.items)}) == 101

    with closing(sqlite3.connect(database)) as connection:
        trigger = _drop_trigger(connection, "review_task_events_no_delete")
        connection.execute("DELETE FROM review_task_events WHERE task_id=?", (second.items[0].task.task_id,))
        connection.execute(trigger)
        connection.commit()
    with pytest.raises(repository.ReviewTaskRepositoryError):
        repository.list_current_review_tasks(database, **arguments)
