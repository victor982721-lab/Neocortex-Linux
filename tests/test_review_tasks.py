from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

import neocortex.persistence.framework_schema as framework_schema
import neocortex.workflow.review.review_task_repository as review_task_repository
from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    PhysicalIdentityRef,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.workflow.review.review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskActorKind,
    ReviewTaskCoverage,
    ReviewTaskDraft,
    ReviewTaskInput,
    ReviewTaskPublication,
    ReviewTaskSourceFence,
    ReviewTaskState,
    ReviewTaskTransition,
)
from neocortex.workflow.review.review_task_repository import (
    ReviewTaskCASConflict,
    ReviewTaskRepositoryError,
    append_review_task_event,
    has_review_task_scan_history,
    list_current_review_tasks,
    lookup_review_task_version_heads,
    publish_review_task_page,
    read_review_task_progress,
)


def _database(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "framework.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        initialize_framework_schema(connection, lambda: None)
    return database


def _source_fence(
    generation: int,
    *,
    scope: str = "personal",
    task_type: str = "value-review",
    selector: str = "value-review-personal-v1",
) -> ReviewTaskSourceFence:
    return ReviewTaskSourceFence.create(
        scope=scope,
        task_type=task_type,
        selector_signature=selector,
        source_snapshot={"generation": generation, "owner": "inventory"},
    )


def _input(number: int) -> ReviewTaskInput:
    resource = ResourceRef(
        resource_id=f"resource-{number}",
        source_kind="inventory",
        owner="inventory",
        physical_identity=PhysicalIdentityRef(
            scheme="fixture-identity",
            value=f"volume:{number}",
            identity_version=1,
        ),
        current_path=f"/fixture/{number}.txt",
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id=f"revision-{number}",
        producer="fixture",
        processing_signature="fixture-signature-v1",
        generation=number,
        state=RevisionState.CURRENT,
        observed_at_utc="2026-08-11T12:00:00Z",
    )
    return ReviewTaskInput(
        input_id=f"input-{number}",
        fingerprint_algorithm="sha256",
        fingerprint=f"{number:064x}",
        resource=resource,
        revision=revision,
    )


def _task(
    number: int,
    *,
    scope: str = "personal",
    task_type: str = "value-review",
    logical_key: str | None = None,
    task_version: int = 1,
    supersedes_task_id: str | None = None,
    created_ns: int | None = None,
    impact: float = 0.8,
    source_input_id: str | None = None,
) -> ReviewTaskDraft:
    item = _input(number)
    effective_source_input_id = source_input_id or item.input_id
    evidence = EvidenceRef(
        evidence_id=f"evidence-{number}-v{task_version}",
        resource_id=item.resource.resource_id if item.resource is not None else "missing",
        revision_id=item.revision.revision_id if item.revision is not None else "missing",
        method=EvidenceMethod.STRUCTURAL,
        identifiers=(("fixture", str(number)),),
    )
    return ReviewTaskDraft(
        task_id=f"task-{number}-v{task_version}",
        logical_key=logical_key or f"logical-{number}",
        task_version=task_version,
        task_type=task_type,
        scope=scope,
        source_kind="review-decision",
        source_input_id=effective_source_input_id,
        snapshot=CanonicalJsonObject.from_mapping({"candidate": number, "version": task_version}),
        evidence=(evidence,),
        reason_code="uncertain-value",
        uncertainty_detail=CanonicalJsonObject.from_mapping({"basis": "controlled-fixture"}),
        impact=impact,
        uncertainty=0.5,
        irreversibility=0.25,
        suggestions=("review source evidence",),
        supersedes_task_id=supersedes_task_id,
        created_ns=created_ns or (100 + number + task_version),
    )


def _publication(
    generation: int,
    numbers: tuple[int, ...],
    *,
    tasks: tuple[ReviewTaskDraft, ...] | None = None,
    coverage: ReviewTaskCoverage = ReviewTaskCoverage.COMPLETE,
    cursor_before: CanonicalJsonObject | None = None,
    cursor_after: CanonicalJsonObject | None = None,
    confirmed_ns: int | None = None,
    evidence_complete: bool = True,
    evidence_reason: str | None = None,
) -> ReviewTaskPublication:
    selected = tasks if tasks is not None else tuple(_task(number) for number in numbers)
    return ReviewTaskPublication(
        batch_id=f"batch-{generation}-{numbers or ('empty',)}",
        batch_key=f"batch-key-{generation}-{numbers or ('empty',)}",
        fence=_source_fence(generation),
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        inputs=tuple(_input(number) for number in numbers),
        tasks=selected,
        coverage=coverage,
        producer_signature="value-review-preselection-v1",
        confirmed_ns=confirmed_ns or (1_000 + generation),
        evidence_complete=evidence_complete,
        evidence_reason=evidence_reason,
    )


def _counts(database: Path) -> tuple[int, int, int, int]:
    with closing(sqlite3.connect(database)) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "review_task_batches",
                "review_tasks",
                "review_task_events",
                "review_task_scan_progress",
            )
        )  # type: ignore[return-value]


def _publish_numbered_snapshot(
    database: Path,
    *,
    generation: int,
    count: int,
    confirmed_base: int,
) -> ReviewTaskPublication:
    fence = _source_fence(generation)
    cursor_before: CanonicalJsonObject | None = None
    revision: int | None = None
    final_publication: ReviewTaskPublication | None = None
    for page_index, start in enumerate(range(1, count + 1, 100), start=1):
        numbers = tuple(range(start, min(start + 100, count + 1)))
        final = start + len(numbers) > count
        cursor_after = None if final else CanonicalJsonObject.from_mapping({"offset": start + 99})
        publication = ReviewTaskPublication(
            batch_id=f"bulk-{generation}-{page_index}",
            batch_key=f"bulk-key-{generation}-{page_index}",
            fence=fence,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            inputs=tuple(_input(number) for number in numbers),
            tasks=tuple(_task(number) for number in numbers),
            coverage=(ReviewTaskCoverage.COMPLETE if final else ReviewTaskCoverage.PARTIAL),
            producer_signature="bulk-fixture-v1",
            confirmed_ns=confirmed_base + page_index,
        )
        result = publish_review_task_page(
            database,
            publication,
            expected_progress_revision=revision,
        )
        revision = result.progress.revision
        cursor_before = cursor_after
        final_publication = publication
    assert final_publication is not None
    return final_publication


def _drop_trigger(connection: sqlite3.Connection, name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    assert row is not None and isinstance(row[0], str)
    connection.execute(f'DROP TRIGGER "{name}"')
    return str(row[0])


def _transition(
    task_id: str,
    expected_event_id: str,
    expected_state: ReviewTaskState,
    to_state: ReviewTaskState,
    number: int,
) -> ReviewTaskTransition:
    terminal = to_state in {ReviewTaskState.RESOLVED, ReviewTaskState.DISMISSED}
    return ReviewTaskTransition(
        event_id=f"human-event-{number}",
        event_key=f"human-event-key-{number}",
        task_id=task_id,
        expected_event_id=expected_event_id,
        expected_state=expected_state,
        to_state=to_state,
        actor_kind=ReviewTaskActorKind.HUMAN,
        actor_id="victor",
        provenance=CanonicalJsonObject.from_mapping({"surface": "fixture"}),
        decision=(CanonicalJsonObject.from_mapping({"outcome": "confirmed"}) if terminal else None),
        note="reviewed" if terminal else None,
        observed_ns=20_000 + number,
        recorded_ns=20_000 + number,
    )


def test_contract_rejects_ambiguous_snapshot_and_unbounded_page() -> None:
    snapshot = CanonicalJsonObject.from_mapping({"generation": 1})
    with pytest.raises(ValueError, match="does not match"):
        ReviewTaskSourceFence(
            scope="personal",
            task_type="value-review",
            selector_signature="selector-v1",
            source_snapshot=snapshot,
            source_snapshot_fingerprint="review-task-source-snapshot-v1:sha256:" + "0" * 64,
        )


def test_contract_row_and_domain_limits_match_framework_schema(tmp_path: Path) -> None:
    scope = "s" * 128
    task_type = "t" * 128
    fence = ReviewTaskSourceFence.create(
        scope=scope,
        task_type=task_type,
        selector_signature="x" * 512,
        source_snapshot={"generation": 1},
    )
    task = replace(
        _task(1, scope=scope, task_type=task_type),
        source_kind="k" * 128,
        reason_code="r" * 128,
    )
    publication = ReviewTaskPublication(
        batch_id="boundary-batch",
        batch_key="boundary-batch-key",
        fence=fence,
        cursor_before=None,
        cursor_after=None,
        inputs=(_input(1),),
        tasks=(task,),
        coverage=ReviewTaskCoverage.COMPLETE,
        producer_signature="p" * 512,
        confirmed_ns=1_000,
    )
    database = _database(tmp_path)
    result = publish_review_task_page(database, publication, expected_progress_revision=None)
    assert result.task_ids == (task.task_id,)

    with pytest.raises(ValueError, match="cannot exceed 128 characters"):
        ReviewTaskSourceFence.create(
            scope="s" * 129,
            task_type="value-review",
            selector_signature="selector",
            source_snapshot={"generation": 2},
        )
    oversized_resource = ResourceRef(
        resource_id="oversized-resource",
        source_kind="inventory",
        owner="inventory",
        current_path="/" + "z" * 70_000,
    )
    with pytest.raises(ValueError, match="65536-byte limit"):
        ReviewTaskInput(
            input_id="oversized-input",
            fingerprint_algorithm="sha256",
            fingerprint="0" * 64,
            resource=oversized_resource,
        )
    with pytest.raises(ValueError, match="cannot exceed 1000 inputs"):
        ReviewTaskPublication(
            batch_id="batch",
            batch_key="batch-key",
            fence=_source_fence(1),
            cursor_before=None,
            cursor_after=CanonicalJsonObject.from_mapping({"offset": 1_001}),
            inputs=tuple(
                ReviewTaskInput(f"input-{number}", "sha256", f"{number:064x}")
                for number in range(1_001)
            ),
            tasks=(),
            coverage=ReviewTaskCoverage.PARTIAL,
            producer_signature="fixture",
            confirmed_ns=1,
        )


def test_publish_page_is_atomic_resumable_and_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    cursor = CanonicalJsonObject.from_mapping({"offset": 1})
    publication = _publication(
        1,
        (1,),
        coverage=ReviewTaskCoverage.PARTIAL,
        cursor_after=cursor,
    )

    result = publish_review_task_page(database, publication, expected_progress_revision=None)
    assert not result.idempotent
    assert result.progress.revision == 1
    assert result.progress.cursor == cursor
    assert _counts(database) == (1, 1, 1, 1)

    replay = publish_review_task_page(database, publication, expected_progress_revision=None)
    assert replay.idempotent
    assert replay.progress == result.progress
    page = list_current_review_tasks(
        database,
        limit=10,
        source_snapshot_fingerprint=publication.fence.source_snapshot_fingerprint,
    )
    assert [record.task.task_id for record in page.items] == ["task-1-v1"]
    assert page.items[0].source_snapshot_fingerprint == (
        publication.fence.source_snapshot_fingerprint
    )


def test_progress_cursor_and_revision_are_exact_cas(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first_cursor = CanonicalJsonObject.from_mapping({"offset": 1})
    first = _publication(
        2,
        (1,),
        coverage=ReviewTaskCoverage.PARTIAL,
        cursor_after=first_cursor,
        confirmed_ns=2_000,
    )
    publish_review_task_page(database, first, expected_progress_revision=None)
    wrong = _publication(
        2,
        (2,),
        coverage=ReviewTaskCoverage.COMPLETE,
        cursor_before=CanonicalJsonObject.from_mapping({"offset": 999}),
        confirmed_ns=2_001,
    )
    with pytest.raises(ReviewTaskCASConflict, match="cursor"):
        publish_review_task_page(database, wrong, expected_progress_revision=1)
    assert _counts(database) == (1, 1, 1, 1)

    second = _publication(
        2,
        (2,),
        coverage=ReviewTaskCoverage.COMPLETE,
        cursor_before=first_cursor,
        confirmed_ns=2_002,
    )
    result = publish_review_task_page(database, second, expected_progress_revision=1)
    assert result.progress.complete
    assert result.progress.revision == 2
    assert result.progress.scanned_count == 2
    assert result.progress.selected_count == 2
    assert read_review_task_progress(database, second.fence) == result.progress


def test_event_transition_is_cas_append_only_and_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    publication = _publication(3, (1,))
    publish_review_task_page(database, publication, expected_progress_revision=None)
    opened = list_current_review_tasks(database, limit=10).items[0].current_event
    claim = _transition(
        "task-1-v1", opened.event_id, ReviewTaskState.OPEN, ReviewTaskState.IN_REVIEW, 1
    )
    claimed = append_review_task_event(database, claim)
    assert not claimed.idempotent
    assert claimed.event.to_state is ReviewTaskState.IN_REVIEW
    assert append_review_task_event(database, claim).idempotent

    stale = _transition(
        "task-1-v1", opened.event_id, ReviewTaskState.OPEN, ReviewTaskState.RESOLVED, 2
    )
    with pytest.raises(ReviewTaskCASConflict):
        append_review_task_event(database, stale)
    resolution = _transition(
        "task-1-v1",
        claimed.event.event_id,
        ReviewTaskState.IN_REVIEW,
        ReviewTaskState.RESOLVED,
        3,
    )
    resolved = append_review_task_event(database, resolution)
    assert resolved.event.terminal
    assert list_current_review_tasks(database, limit=10).items == ()
    all_states = list_current_review_tasks(database, limit=10, states=(ReviewTaskState.RESOLVED,))
    assert all_states.items[0].current_event.decision is not None


def _scoped_decision(record: object, scope: str) -> CanonicalJsonObject:
    return CanonicalJsonObject.from_mapping(
        {
            "decision": "resolved",
            "schema": "neocortex.review-task-decision/v1",
            "scope": scope,
            "selector_signature": record.selector_signature,
            "source_input_fingerprint": record.source.fingerprint,
            "source_snapshot_fingerprint": record.source_snapshot_fingerprint,
        }
    )


def test_scoped_terminal_decision_can_be_reopened_only_by_exact_receipt(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    first = _publication(31, (1,), confirmed_ns=31_000)
    publish_review_task_page(database, first, expected_progress_revision=None)
    opened = list_current_review_tasks(database, limit=10).items[0]
    transition = replace(
        _transition(
            opened.task.task_id,
            opened.current_event.event_id,
            ReviewTaskState.OPEN,
            ReviewTaskState.RESOLVED,
            310,
        ),
        decision=_scoped_decision(opened, "until-source-change"),
        observed_ns=31_100,
        recorded_ns=31_100,
    )
    terminal = append_review_task_event(database, transition).event
    heads = lookup_review_task_version_heads(
        database, (opened.task.logical_key,), scope="personal", task_type="value-review"
    )
    assert heads[0].decision == transition.decision

    successor = _task(
        1,
        logical_key=opened.task.logical_key,
        task_version=2,
        supersedes_task_id=opened.task.task_id,
        created_ns=31_200,
    )
    changed_input = replace(_input(1), fingerprint="f" * 64)
    changed = _publication(
        32,
        (1,),
        tasks=(successor,),
        confirmed_ns=31_300,
    )
    changed = replace(changed, inputs=(changed_input,))
    publish_review_task_page(database, changed, expected_progress_revision=None)
    history = review_task_repository.read_review_task_history(database, opened.task.task_id)
    assert [event.to_state for event in history] == [
        ReviewTaskState.OPEN,
        ReviewTaskState.RESOLVED,
        ReviewTaskState.SUPERSEDED,
    ]
    assert history[-1].previous_event_id == terminal.event_id
    assert history[-1].provenance.to_dict()["reason_code"] == ("terminal_decision_scope_expired")
    assert list_current_review_tasks(database, limit=10).items[0].task.task_version == 2


@pytest.mark.parametrize("scope", ("permanent", "until-policy-change"))
def test_unexpired_terminal_decision_scope_cannot_be_replaced(
    tmp_path: Path,
    scope: str,
) -> None:
    database = _database(tmp_path)
    first = _publication(33, (1,), confirmed_ns=33_000)
    publish_review_task_page(database, first, expected_progress_revision=None)
    opened = list_current_review_tasks(database, limit=10).items[0]
    append_review_task_event(
        database,
        replace(
            _transition(
                opened.task.task_id,
                opened.current_event.event_id,
                ReviewTaskState.OPEN,
                ReviewTaskState.RESOLVED,
                330,
            ),
            decision=_scoped_decision(opened, scope),
            observed_ns=33_100,
            recorded_ns=33_100,
        ),
    )
    successor = _task(
        1,
        logical_key=opened.task.logical_key,
        task_version=2,
        supersedes_task_id=opened.task.task_id,
        created_ns=33_200,
    )
    with pytest.raises(ReviewTaskCASConflict, match=r"scope|permanent"):
        publish_review_task_page(
            database,
            _publication(34, (1,), tasks=(successor,), confirmed_ns=33_300),
            expected_progress_revision=None,
        )
    assert (
        review_task_repository.read_review_task_history(database, opened.task.task_id)[-1].to_state
        is ReviewTaskState.RESOLVED
    )


def test_policy_scoped_terminal_decision_reopens_only_after_selector_change(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    first = _publication(35, (1,), confirmed_ns=35_000)
    publish_review_task_page(database, first, expected_progress_revision=None)
    opened = list_current_review_tasks(database, limit=10).items[0]
    append_review_task_event(
        database,
        replace(
            _transition(
                opened.task.task_id,
                opened.current_event.event_id,
                ReviewTaskState.OPEN,
                ReviewTaskState.RESOLVED,
                350,
            ),
            decision=_scoped_decision(opened, "until-policy-change"),
            observed_ns=35_100,
            recorded_ns=35_100,
        ),
    )
    successor = _task(
        1,
        logical_key=opened.task.logical_key,
        task_version=2,
        supersedes_task_id=opened.task.task_id,
        created_ns=35_200,
    )
    changed = _publication(36, (1,), tasks=(successor,), confirmed_ns=35_300)
    changed_fence = _source_fence(36, selector="value-review-personal-v2")
    changed = replace(changed, fence=changed_fence)

    publish_review_task_page(database, changed, expected_progress_revision=None)

    current = list_current_review_tasks(database, limit=10).items
    assert len(current) == 1
    assert current[0].task.task_version == 2
    assert (
        review_task_repository.read_review_task_history(database, opened.task.task_id)[-1].to_state
        is ReviewTaskState.SUPERSEDED
    )


def test_progress_lookup_finds_complete_epoch_behind_multiple_incomplete_epochs(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    complete = _publication(37, (1,), confirmed_ns=37_000)
    publish_review_task_page(database, complete, expected_progress_revision=None)
    owner_snapshot = CanonicalJsonObject.from_mapping({"generation": 37, "owner": "inventory"})

    for index in (1, 2):
        fence = ReviewTaskSourceFence.create(
            scope="personal",
            task_type="value-review",
            selector_signature=complete.fence.selector_signature,
            source_snapshot={
                "generation": 37,
                "owner": "inventory",
                "reference_day_ns": index,
            },
        )
        partial = _publication(
            37 + index,
            (index + 1,),
            coverage=ReviewTaskCoverage.PARTIAL,
            cursor_after=CanonicalJsonObject.from_mapping({"offset": index}),
            confirmed_ns=38_000 + index,
        )
        publish_review_task_page(
            database,
            replace(partial, fence=fence),
            expected_progress_revision=None,
        )

    incomplete, found_complete = review_task_repository.find_review_task_scan_progress(
        database,
        scope="personal",
        task_type="value-review",
        selector_signature=complete.fence.selector_signature,
        owner_source_snapshot=owner_snapshot,
    )
    assert incomplete is not None
    assert found_complete is not None
    assert found_complete.fence == complete.fence


def test_batch_version_lookup_and_owner_local_supersession(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = _publication(4, (1,), confirmed_ns=4_000)
    publish_review_task_page(database, first, expected_progress_revision=None)
    heads = lookup_review_task_version_heads(
        database, ("logical-1",), scope="personal", task_type="value-review"
    )
    assert [(head.task_version, head.state) for head in heads] == [(1, ReviewTaskState.OPEN)]

    successor = _task(
        1,
        logical_key="logical-1",
        task_version=2,
        supersedes_task_id=heads[0].task_id,
        created_ns=4_100,
    )
    second = _publication(
        5,
        (1,),
        tasks=(successor,),
        confirmed_ns=4_200,
    )
    publish_review_task_page(database, second, expected_progress_revision=None)
    current = list_current_review_tasks(database, limit=10).items
    assert [record.task.task_id for record in current] == ["task-1-v2"]
    old = list_current_review_tasks(
        database,
        limit=10,
        states=(ReviewTaskState.SUPERSEDED,),
        source_snapshot_fingerprint=first.fence.source_snapshot_fingerprint,
    )
    assert old.items[0].task.task_id == "task-1-v1"
    assert old.items[0].current_event.provenance.to_dict()["replacement_task_id"] == ("task-1-v2")
    latest = lookup_review_task_version_heads(
        database, ("logical-1",), scope="personal", task_type="value-review"
    )
    assert latest[0].task_version == 2


def test_complete_refresh_supersedes_absent_active_not_human_terminal(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    old = _publication(6, (1, 2, 3), confirmed_ns=6_000)
    publish_review_task_page(database, old, expected_progress_revision=None)
    records = {
        record.task.task_id: record
        for record in list_current_review_tasks(database, limit=10).items
    }
    resolution = _transition(
        "task-3-v1",
        records["task-3-v1"].current_event.event_id,
        ReviewTaskState.OPEN,
        ReviewTaskState.RESOLVED,
        30,
    )
    append_review_task_event(database, resolution)

    empty_refresh = _publication(7, (), tasks=(), confirmed_ns=7_000)
    publish_review_task_page(database, empty_refresh, expected_progress_revision=None)
    assert list_current_review_tasks(database, limit=10).items == ()
    closed = list_current_review_tasks(
        database,
        limit=10,
        states=(ReviewTaskState.SUPERSEDED, ReviewTaskState.RESOLVED),
        source_snapshot_fingerprint=old.fence.source_snapshot_fingerprint,
    ).items
    states = {record.task.task_id: record.state for record in closed}
    assert states == {
        "task-1-v1": ReviewTaskState.SUPERSEDED,
        "task-2-v1": ReviewTaskState.SUPERSEDED,
        "task-3-v1": ReviewTaskState.RESOLVED,
    }


@pytest.mark.parametrize("task_count", (1_001, 2_501))
def test_bulk_source_head_supersedes_unbounded_absence_with_one_atomic_receipt(
    tmp_path: Path,
    task_count: int,
) -> None:
    database = _database(tmp_path)
    old = _publish_numbered_snapshot(
        database,
        generation=60,
        count=task_count,
        confirmed_base=1_000_000,
    )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_task_events").fetchone() == (
            task_count,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_source_publications"
        ).fetchone() == (1,)

    empty = _publication(61, (), tasks=(), confirmed_ns=1_001_000)
    result = publish_review_task_page(database, empty, expected_progress_revision=None)
    assert result.progress.complete and result.progress.evidence_complete
    assert list_current_review_tasks(database, limit=1).items == ()
    derived = list_current_review_tasks(
        database,
        limit=1,
        states=(ReviewTaskState.SUPERSEDED,),
        source_snapshot_fingerprint=old.fence.source_snapshot_fingerprint,
    ).items[0]
    assert derived.current_event.provenance.to_dict()["derived"] is True
    assert (
        derived.current_event.provenance.to_dict()["reason_code"]
        == "absent_from_complete_source_snapshot"
    )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_task_events").fetchone() == (
            task_count,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_source_publications"
        ).fetchone() == (2,)

    replay = publish_review_task_page(database, empty, expected_progress_revision=None)
    assert replay.idempotent
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_source_publications"
        ).fetchone() == (2,)


def test_final_page_crash_cannot_publish_source_head_or_partial_supersession(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    old = _publication(62, (1,), confirmed_ns=62_000)
    publish_review_task_page(database, old, expected_progress_revision=None)
    final = _publication(63, (), tasks=(), confirmed_ns=63_000)

    def crash(stage: str) -> None:
        if stage == "before_source_head":
            raise RuntimeError("crash before source head")

    with pytest.raises(RuntimeError, match="crash before source head"):
        publish_review_task_page(
            database,
            final,
            expected_progress_revision=None,
            _fault_injector=crash,
        )
    assert [item.task.task_id for item in list_current_review_tasks(database, limit=10).items] == [
        "task-1-v1"
    ]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_source_publications"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_batches WHERE batch_id=?",
            (final.batch_id,),
        ).fetchone() == (0,)

    recovered = publish_review_task_page(database, final, expected_progress_revision=None)
    assert recovered.progress.complete
    assert list_current_review_tasks(database, limit=10).items == ()


def test_latest_source_publication_validator_reconciles_exact_owner_receipts(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    publication = _publication(69, (1,), confirmed_ns=69_000)
    publish_review_task_page(database, publication, expected_progress_revision=None)

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        observed = (
            review_task_repository.validate_latest_review_task_source_publications_from_connection(
                connection,
                limit=10,
            )
        )

    assert len(observed) == 1
    assert observed[0].batch_id == publication.batch_id
    assert observed[0].fence == publication.fence
    assert observed[0].confirmed_ns == publication.confirmed_ns


@pytest.mark.parametrize("corruption", ("first_membership", "first_batch_receipt"))
def test_source_publication_audit_covers_every_published_batch(
    tmp_path: Path,
    corruption: str,
) -> None:
    database = _database(tmp_path)
    fence = _source_fence(70)
    cursor = CanonicalJsonObject.from_mapping({"offset": 1})
    first = ReviewTaskPublication(
        batch_id="audit-first",
        batch_key="audit-first-key",
        fence=fence,
        cursor_before=None,
        cursor_after=cursor,
        inputs=(_input(1),),
        tasks=(_task(1),),
        coverage=ReviewTaskCoverage.PARTIAL,
        producer_signature="audit-fixture-v1",
        confirmed_ns=70_000,
    )
    first_result = publish_review_task_page(
        database,
        first,
        expected_progress_revision=None,
    )
    final = ReviewTaskPublication(
        batch_id="audit-final",
        batch_key="audit-final-key",
        fence=fence,
        cursor_before=cursor,
        cursor_after=None,
        inputs=(),
        tasks=(),
        coverage=ReviewTaskCoverage.COMPLETE,
        producer_signature="audit-fixture-v1",
        confirmed_ns=70_001,
    )
    publish_review_task_page(
        database,
        final,
        expected_progress_revision=first_result.progress.revision,
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        audit = review_task_repository.audit_latest_review_task_source_publications_from_connection(
            connection,
            limit=10,
        )
        assert audit.batch_count == 2
        assert audit.membership_count == 1
        assert audit.progress_count == 1
        if corruption == "first_membership":
            trigger = _drop_trigger(
                connection,
                "review_task_batch_memberships_no_update",
            )
            connection.execute(
                """UPDATE review_task_batch_memberships
                SET source_input_id='wrong-input' WHERE batch_id=?""",
                (first.batch_id,),
            )
        else:
            trigger = _drop_trigger(connection, "review_task_batches_no_update")
            connection.execute(
                "UPDATE review_task_batches SET receipt_json='{}' WHERE batch_id=?",
                (first.batch_id,),
            )
        connection.execute(trigger)
        connection.commit()
        with pytest.raises(ReviewTaskRepositoryError, match="ReviewTask"):
            review_task_repository.audit_latest_review_task_source_publications_from_connection(
                connection,
                limit=10,
            )


def test_source_publication_audit_uses_indexed_membership_lookup(tmp_path: Path) -> None:
    database = _database(tmp_path)
    publication = _publication(71, (1,), confirmed_ns=71_000)
    publish_review_task_page(database, publication, expected_progress_revision=None)

    with closing(sqlite3.connect(database)) as connection:
        plan = tuple(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                + review_task_repository._PUBLISHED_BATCH_CHAIN_CTE
                + """ SELECT membership.membership_id
                FROM chain
                CROSS JOIN review_task_batch_memberships membership
                  ON membership.batch_id=chain.batch_id
                LIMIT ?""",
                (101,),
            )
        )

    assert any("SEARCH membership" in detail for detail in plan), plan
    assert not any("SCAN membership" in detail for detail in plan), plan


@pytest.mark.parametrize(
    ("constant", "limit", "message"),
    (
        ("MAX_REVIEW_TASK_SOURCE_CHAIN_BATCHES", 1, "batch chain exceeds"),
        ("MAX_REVIEW_TASK_SOURCE_CHAIN_MEMBERSHIPS", 0, "memberships exceed"),
        ("MAX_REVIEW_TASK_SOURCE_AUDIT_BYTES", 1, "audit exceeds"),
    ),
)
def test_source_publication_audit_enforces_aggregate_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    message: str,
) -> None:
    database = _database(tmp_path)
    fence = _source_fence(72)
    cursor = CanonicalJsonObject.from_mapping({"offset": 1})
    first = ReviewTaskPublication(
        batch_id="bounded-first",
        batch_key="bounded-first-key",
        fence=fence,
        cursor_before=None,
        cursor_after=cursor,
        inputs=(_input(1),),
        tasks=(_task(1),),
        coverage=ReviewTaskCoverage.PARTIAL,
        producer_signature="bounded-audit-fixture-v1",
        confirmed_ns=72_000,
    )
    first_result = publish_review_task_page(database, first, expected_progress_revision=None)
    final = ReviewTaskPublication(
        batch_id="bounded-final",
        batch_key="bounded-final-key",
        fence=fence,
        cursor_before=cursor,
        cursor_after=None,
        inputs=(),
        tasks=(),
        coverage=ReviewTaskCoverage.COMPLETE,
        producer_signature="bounded-audit-fixture-v1",
        confirmed_ns=72_001,
    )
    publish_review_task_page(
        database,
        final,
        expected_progress_revision=first_result.progress.revision,
    )
    monkeypatch.setattr(review_task_repository, constant, limit)

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(ReviewTaskRepositoryError, match=message):
            review_task_repository.audit_latest_review_task_source_publications_from_connection(
                connection,
                limit=10,
            )


@pytest.mark.parametrize("claimed", (False, True))
def test_incomplete_accumulated_evidence_never_advances_source_head(
    tmp_path: Path,
    claimed: bool,
) -> None:
    database = _database(tmp_path)
    old = _publication(64, (1,), confirmed_ns=10_000)
    publish_review_task_page(database, old, expected_progress_revision=None)
    if claimed:
        opened = list_current_review_tasks(database, limit=10).items[0].current_event
        append_review_task_event(
            database,
            _transition(
                "task-1-v1",
                opened.event_id,
                ReviewTaskState.OPEN,
                ReviewTaskState.IN_REVIEW,
                64,
            ),
        )

    fence = _source_fence(65)
    cursor = CanonicalJsonObject.from_mapping({"offset": 1})
    first = ReviewTaskPublication(
        batch_id=f"evidence-first-{int(claimed)}",
        batch_key=f"evidence-first-key-{int(claimed)}",
        fence=fence,
        cursor_before=None,
        cursor_after=cursor,
        inputs=(_input(2),),
        tasks=(),
        coverage=ReviewTaskCoverage.PARTIAL,
        producer_signature="evidence-fixture-v1",
        confirmed_ns=30_000,
        evidence_complete=False,
        evidence_reason="source_evidence_partial",
    )
    first_result = publish_review_task_page(database, first, expected_progress_revision=None)
    final = ReviewTaskPublication(
        batch_id=f"evidence-final-{int(claimed)}",
        batch_key=f"evidence-final-key-{int(claimed)}",
        fence=fence,
        cursor_before=cursor,
        cursor_after=None,
        inputs=(),
        tasks=(),
        coverage=ReviewTaskCoverage.COMPLETE,
        producer_signature="evidence-fixture-v1",
        confirmed_ns=30_001,
        evidence_complete=True,
        evidence_reason=None,
    )
    result = publish_review_task_page(
        database,
        final,
        expected_progress_revision=first_result.progress.revision,
    )
    assert result.progress.complete
    assert not result.progress.evidence_complete
    assert result.progress.evidence_reason == "source_evidence_partial"
    active = list_current_review_tasks(database, limit=10).items
    assert len(active) == 1
    assert active[0].state is (ReviewTaskState.IN_REVIEW if claimed else ReviewTaskState.OPEN)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_source_publications"
        ).fetchone() == (1,)


def test_reappearing_logical_task_materializes_only_its_effective_predecessor(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    first = _publication(66, (1,), confirmed_ns=10_000)
    publish_review_task_page(database, first, expected_progress_revision=None)
    empty = _publication(67, (), tasks=(), confirmed_ns=20_000)
    publish_review_task_page(database, empty, expected_progress_revision=None)
    predecessor = lookup_review_task_version_heads(
        database,
        ("logical-1",),
        scope="personal",
        task_type="value-review",
    )[0]
    assert predecessor.state is ReviewTaskState.SUPERSEDED

    successor = _task(
        1,
        task_version=2,
        supersedes_task_id=predecessor.task_id,
        created_ns=30_000,
    )
    reappeared = _publication(
        68,
        (1,),
        tasks=(successor,),
        confirmed_ns=31_000,
    )
    publish_review_task_page(database, reappeared, expected_progress_revision=None)
    current = list_current_review_tasks(database, limit=10).items
    assert [item.task.task_id for item in current] == ["task-1-v2"]
    with closing(sqlite3.connect(database)) as connection:
        predecessor_events = connection.execute(
            """SELECT to_state,provenance_json FROM review_task_events
            WHERE task_id='task-1-v1' ORDER BY sequence"""
        ).fetchall()
        assert len(predecessor_events) == 2
        assert predecessor_events[-1][0] == "superseded"
        assert json.loads(str(predecessor_events[-1][1]))["derived"] is True
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_events WHERE task_id='task-1-v2'"
        ).fetchone() == (1,)


def test_effective_supersession_is_terminal_for_repo_cas_and_direct_sql(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    first = _publication(69, (1,), confirmed_ns=10_000)
    publish_review_task_page(database, first, expected_progress_revision=None)
    opened = list_current_review_tasks(database, limit=10).items[0].current_event
    empty = _publication(70, (), tasks=(), confirmed_ns=20_000)
    publish_review_task_page(database, empty, expected_progress_revision=None)

    with pytest.raises(ReviewTaskCASConflict, match="current event/state changed"):
        append_review_task_event(
            database,
            _transition(
                "task-1-v1",
                opened.event_id,
                ReviewTaskState.OPEN,
                ReviewTaskState.RESOLVED,
                70,
            ),
        )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="CAS or transition conflict"):
            connection.execute(
                """INSERT INTO review_task_events(
                event_id,event_key,task_id,sequence,previous_event_id,from_state,
                to_state,actor_kind,actor_id,provenance_json,decision_json,note,
                observed_ns,recorded_ns,event_schema_version)
                VALUES('direct-resolution','direct-resolution-key','task-1-v1',2,?,
                'open','resolved','human','direct-fixture','{"fixture":"direct"}',
                '{"outcome":"resolved"}',NULL,21000,21000,1)""",
                (opened.event_id,),
            )


def test_superseded_is_reserved_for_exact_internal_receipts(tmp_path: Path) -> None:
    database = _database(tmp_path)
    publication = _publication(76, (1,), confirmed_ns=10_000)
    publish_review_task_page(database, publication, expected_progress_revision=None)
    opened = list_current_review_tasks(database, limit=10).items[0].current_event

    with pytest.raises(ValueError, match="reserved for receipt-backed"):
        _transition(
            "task-1-v1",
            opened.event_id,
            ReviewTaskState.OPEN,
            ReviewTaskState.SUPERSEDED,
            76,
        )

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for actor_kind, actor_id, provenance in (
            ("human", "victor", '{"surface":"forged"}'),
            (
                "system",
                "review-task-refresh",
                '{"reason_code":"replacement_task_published",'
                '"replacement_task_id":"missing-task",'
                '"source_snapshot_fingerprint":"'
                + publication.fence.source_snapshot_fingerprint
                + '"}',
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CAS or transition conflict"):
                connection.execute(
                    """INSERT INTO review_task_events(
                    event_id,event_key,task_id,sequence,previous_event_id,
                    from_state,to_state,actor_kind,actor_id,provenance_json,
                    decision_json,note,observed_ns,recorded_ns,event_schema_version)
                    VALUES(?,?, 'task-1-v1',2,?,'open','superseded',?,?,?,
                    NULL,NULL,11000,11000,1)""",
                    (
                        f"forged-{actor_kind}",
                        f"forged-key-{actor_kind}",
                        opened.event_id,
                        actor_kind,
                        actor_id,
                        provenance,
                    ),
                )


def test_equal_timestamp_source_facts_remain_active_and_transitionable(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    initial = _publication(72, (1,), confirmed_ns=10_000)
    publish_review_task_page(database, initial, expected_progress_revision=None)

    cursor = CanonicalJsonObject.from_mapping({"offset": 1})
    concurrent_partial = _publication(
        73,
        (2,),
        coverage=ReviewTaskCoverage.PARTIAL,
        cursor_after=cursor,
        confirmed_ns=20_000,
    )
    publish_review_task_page(database, concurrent_partial, expected_progress_revision=None)
    later_complete_view = _publication(74, (), tasks=(), confirmed_ns=20_000)
    publish_review_task_page(database, later_complete_view, expected_progress_revision=None)

    active = list_current_review_tasks(database, limit=10).items
    assert [(record.task.task_id, record.state) for record in active] == [
        ("task-2-v1", ReviewTaskState.OPEN)
    ]
    opened = active[0].current_event
    resolved = append_review_task_event(
        database,
        _transition(
            "task-2-v1",
            opened.event_id,
            ReviewTaskState.OPEN,
            ReviewTaskState.RESOLVED,
            75,
        ),
    )
    assert resolved.event.to_state is ReviewTaskState.RESOLVED


def test_empty_scan_history_remains_observable_without_task_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    publication = _publication(8, (), tasks=(), confirmed_ns=8_000)
    assert not has_review_task_scan_history(
        database,
        scope=publication.fence.scope,
        task_type=publication.fence.task_type,
        selector_signature=publication.fence.selector_signature,
    )

    publish_review_task_page(database, publication, expected_progress_revision=None)

    assert list_current_review_tasks(database, limit=10).items == ()
    assert has_review_task_scan_history(
        database,
        scope=publication.fence.scope,
        task_type=publication.fence.task_type,
        selector_signature=publication.fence.selector_signature,
    )


def test_current_queue_uses_keyset_pagination_and_snapshot_filter(tmp_path: Path) -> None:
    database = _database(tmp_path)
    tasks = (
        _task(1, impact=1.0),
        _task(2, impact=0.8),
        _task(3, impact=0.6),
    )
    publication = _publication(8, (1, 2, 3), tasks=tasks)
    publish_review_task_page(database, publication, expected_progress_revision=None)
    first = list_current_review_tasks(
        database,
        limit=2,
        source_snapshot_fingerprint=publication.fence.source_snapshot_fingerprint,
    )
    assert [record.task.task_id for record in first.items] == ["task-1-v1", "task-2-v1"]
    assert first.next_cursor is not None
    second = list_current_review_tasks(
        database,
        limit=2,
        after=first.next_cursor,
        source_snapshot_fingerprint=publication.fence.source_snapshot_fingerprint,
    )
    assert [record.task.task_id for record in second.items] == ["task-3-v1"]
    assert second.next_cursor is None
    assert (
        list_current_review_tasks(
            database,
            limit=2,
            source_snapshot_fingerprint=_source_fence(99).source_snapshot_fingerprint,
        ).items
        == ()
    )


@pytest.mark.parametrize(
    "stage", ("after_batch", "after_predecessors", "after_tasks", "after_progress")
)
def test_fault_injection_rolls_back_the_entire_page(tmp_path: Path, stage: str) -> None:
    database = _database(tmp_path)
    publication = _publication(9, (1,))

    def crash(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        publish_review_task_page(
            database,
            publication,
            expected_progress_revision=None,
            _fault_injector=crash,
        )
    assert _counts(database) == (0, 0, 0, 0)


def test_cancellation_rolls_back_page_and_preserves_original_exception(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    publication = _publication(10, (1,))

    class StopNow(RuntimeError):
        pass

    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise StopNow("cancelled")

    with pytest.raises(StopNow, match="cancelled"):
        publish_review_task_page(
            database,
            publication,
            expected_progress_revision=None,
            cancellation_check=cancel,
        )
    assert _counts(database) == (0, 0, 0, 0)


def test_busy_writer_fails_without_partial_facts(tmp_path: Path) -> None:
    database = _database(tmp_path)
    blocker = sqlite3.connect(database)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            publish_review_task_page(
                database,
                _publication(11, (1,)),
                expected_progress_revision=None,
                timeout_seconds=0.01,
            )
        blocker.rollback()
    finally:
        blocker.close()
    assert _counts(database) == (0, 0, 0, 0)


def test_readers_fail_closed_for_missing_event_and_aliased_snapshot(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    publication = _publication(12, (1,))
    publish_review_task_page(database, publication, expected_progress_revision=None)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        trigger_sql = _drop_trigger(connection, "review_task_events_no_delete")
        connection.execute("DELETE FROM review_task_events")
        connection.execute(trigger_sql)
        connection.commit()
    with pytest.raises(ReviewTaskRepositoryError, match="current event"):
        list_current_review_tasks(database, limit=10)


def test_readers_fail_closed_for_corrupt_event_chain_and_task_payload(
    tmp_path: Path,
) -> None:
    chain_database = _database(tmp_path / "chain")
    chain_publication = _publication(15, (1,))
    publish_review_task_page(chain_database, chain_publication, expected_progress_revision=None)
    with closing(sqlite3.connect(chain_database)) as connection:
        opened_id = str(
            connection.execute(
                "SELECT event_id FROM review_task_events WHERE task_id='task-1-v1'"
            ).fetchone()[0]
        )
        trigger_sql = _drop_trigger(connection, "review_task_events_validate_insert")
        connection.execute(
            """INSERT INTO review_task_events(
            event_id,event_key,task_id,sequence,previous_event_id,from_state,to_state,
            actor_kind,actor_id,provenance_json,decision_json,note,observed_ns,
            recorded_ns,event_schema_version)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "corrupt-gap-event",
                "corrupt-gap-key",
                "task-1-v1",
                3,
                opened_id,
                "open",
                "in_review",
                "system",
                "corruption-fixture",
                '{"fixture":"gap"}',
                None,
                None,
                3_000,
                3_000,
                1,
            ),
        )
        connection.execute(trigger_sql)
        connection.commit()
    with pytest.raises(ReviewTaskRepositoryError, match="event chain"):
        list_current_review_tasks(chain_database, limit=10)

    payload_database = _database(tmp_path / "payload")
    payload_publication = _publication(16, (1,))
    publish_review_task_page(payload_database, payload_publication, expected_progress_revision=None)
    with closing(sqlite3.connect(payload_database)) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        trigger_sql = _drop_trigger(connection, "review_tasks_no_update")
        connection.execute("UPDATE review_tasks SET snapshot_json='[]' WHERE task_id='task-1-v1'")
        connection.execute(trigger_sql)
        connection.commit()
    with pytest.raises(ReviewTaskRepositoryError, match="persisted ReviewTask"):
        list_current_review_tasks(payload_database, limit=10)
    with pytest.raises(ReviewTaskRepositoryError, match="persisted ReviewTask"):
        lookup_review_task_version_heads(
            payload_database,
            ("logical-1",),
            scope="personal",
            task_type="value-review",
        )
    with pytest.raises(ReviewTaskRepositoryError, match="persisted ReviewTask"):
        read_review_task_progress(payload_database, payload_publication.fence)

    database = _database(tmp_path / "other")
    publication = _publication(13, (1,))
    publish_review_task_page(database, publication, expected_progress_revision=None)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        trigger_sql = _drop_trigger(connection, "review_task_batches_no_update")
        connection.execute(
            "UPDATE review_task_batches SET source_snapshot_json=?",
            ('{"generation":999,"owner":"inventory"}',),
        )
        connection.execute(trigger_sql)
        connection.commit()
    with pytest.raises((ReviewTaskRepositoryError, ValueError), match=r"fingerprint|source"):
        list_current_review_tasks(database, limit=10)


def test_progress_fails_closed_when_prior_batch_task_is_missing(tmp_path: Path) -> None:
    database = _database(tmp_path)
    fence = _source_fence(71)
    cursor = CanonicalJsonObject.from_mapping({"offset": 1})
    first = ReviewTaskPublication(
        batch_id="historical-page-1",
        batch_key="historical-page-key-1",
        fence=fence,
        cursor_before=None,
        cursor_after=cursor,
        inputs=(_input(1),),
        tasks=(_task(1),),
        coverage=ReviewTaskCoverage.PARTIAL,
        producer_signature="history-fixture-v1",
        confirmed_ns=71_000,
    )
    first_result = publish_review_task_page(database, first, expected_progress_revision=None)
    final = ReviewTaskPublication(
        batch_id="historical-page-2",
        batch_key="historical-page-key-2",
        fence=fence,
        cursor_before=cursor,
        cursor_after=None,
        inputs=(_input(2),),
        tasks=(_task(2),),
        coverage=ReviewTaskCoverage.COMPLETE,
        producer_signature="history-fixture-v1",
        confirmed_ns=71_001,
    )
    progress = publish_review_task_page(
        database,
        final,
        expected_progress_revision=first_result.progress.revision,
    ).progress
    assert progress.selected_count == 2

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        event_trigger = _drop_trigger(connection, "review_task_events_no_delete")
        task_trigger = _drop_trigger(connection, "review_tasks_no_delete")
        connection.execute("DELETE FROM review_task_events WHERE task_id='task-1-v1'")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("DELETE FROM review_tasks WHERE task_id='task-1-v1'")
        connection.rollback()
        connection.execute(event_trigger)
        connection.execute(task_trigger)
        connection.commit()
    restored = read_review_task_progress(database, fence)
    assert restored is not None and restored.selected_count == 2


def test_progress_integrity_validation_has_page_count_independent_vm_cost(
    tmp_path: Path,
) -> None:
    def publish_pages(database: Path, *, generation: int, pages: int) -> ReviewTaskSourceFence:
        fence = _source_fence(generation)
        cursor_before: CanonicalJsonObject | None = None
        revision: int | None = None
        for page in range(1, pages + 1):
            number = generation * 100 + page
            final = page == pages
            cursor_after = None if final else CanonicalJsonObject.from_mapping({"page": page})
            result = publish_review_task_page(
                database,
                ReviewTaskPublication(
                    batch_id=f"vm-batch-{generation}-{page}",
                    batch_key=f"vm-batch-key-{generation}-{page}",
                    fence=fence,
                    cursor_before=cursor_before,
                    cursor_after=cursor_after,
                    inputs=(_input(number),),
                    tasks=(_task(number),),
                    coverage=(ReviewTaskCoverage.COMPLETE if final else ReviewTaskCoverage.PARTIAL),
                    producer_signature="vm-cost-fixture-v1",
                    confirmed_ns=1_000_000 + generation * 100 + page,
                ),
                expected_progress_revision=revision,
            )
            revision = result.progress.revision
            cursor_before = cursor_after
        return fence

    one_database = _database(tmp_path / "one")
    many_database = _database(tmp_path / "many")
    one_fence = publish_pages(one_database, generation=80, pages=1)
    many_fence = publish_pages(many_database, generation=81, pages=40)

    def validation_vm_steps(database: Path, fence: ReviewTaskSourceFence) -> int:
        progress = read_review_task_progress(database, fence)
        assert progress is not None
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            steps = 0

            def count_step() -> int:
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(count_step, 1)
            review_task_repository._validate_progress_cumulative_counts(connection, progress)
            connection.set_progress_handler(None, 0)
            return steps

    one_steps = validation_vm_steps(one_database, one_fence)
    many_steps = validation_vm_steps(many_database, many_fence)
    assert many_steps <= one_steps + 300

    with closing(sqlite3.connect(many_database)) as connection:
        plan = " ".join(
            str(row[3])
            for row in connection.execute(
                """EXPLAIN QUERY PLAN
                SELECT scan_revision,cumulative_scanned_count,
                cumulative_selected_count,
                (SELECT COUNT(*) FROM review_task_batch_memberships membership
                 WHERE membership.batch_id=review_task_batches.batch_id)
                FROM review_task_batches WHERE batch_id=?""",
                ("vm-batch-81-40",),
            )
        )
    assert "SEARCH review_task_batches USING PRIMARY KEY" in plan
    assert "review_task_batch_memberships" in plan and "SEARCH membership" in plan


@pytest.mark.parametrize(
    ("object_type", "name"),
    (
        ("TRIGGER", "review_task_events_no_delete"),
        ("INDEX", "review_tasks_queue_idx"),
    ),
)
def test_readers_require_the_exact_framework_v22_schema_objects(
    tmp_path: Path,
    object_type: str,
    name: str,
) -> None:
    database = _database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(f'DROP {object_type} "{name}"')
        connection.commit()

    with pytest.raises(ReviewTaskRepositoryError, match="exact ReviewTask contract"):
        list_current_review_tasks(database, limit=10)


def test_unreceipted_task_is_rejected_and_readers_fail_closed_if_injected(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    publication = _publication(18, (1,))
    publish_review_task_page(database, publication, expected_progress_revision=None)
    forged_source = _input(99).to_json()
    insert_sql = """INSERT INTO review_tasks(
        task_id,logical_key,task_version,task_type,scope,source_kind,
        source_input_id,source_ref_json,source_snapshot_fingerprint,
        snapshot_json,evidence_json,reason_code,uncertainty_json,impact,
        uncertainty,irreversibility,priority,priority_algorithm,
        suggestions_json,batch_id,supersedes_task_id,created_ns)
        SELECT 'task-forged-v1','logical-forged',1,task_type,scope,source_kind,
        'input-99',?,source_snapshot_fingerprint,snapshot_json,evidence_json,
        reason_code,uncertainty_json,impact,uncertainty,irreversibility,
        priority,priority_algorithm,suggestions_json,batch_id,NULL,created_ns+1
        FROM review_tasks WHERE task_id='task-1-v1'"""
    with closing(sqlite3.connect(database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="version chain conflict"):
            connection.execute(insert_sql, (forged_source,))
        connection.rollback()

        trigger_sql = _drop_trigger(connection, "review_tasks_validate_insert")
        connection.execute(insert_sql, (forged_source,))
        connection.execute(trigger_sql)
        connection.execute(
            """INSERT INTO review_task_events(
            event_id,event_key,task_id,sequence,previous_event_id,from_state,
            to_state,actor_kind,actor_id,provenance_json,decision_json,note,
            observed_ns,recorded_ns,event_schema_version)
            VALUES('forged-open','forged-open-key','task-forged-v1',1,NULL,NULL,
            'open','system','fixture','{"fixture":"forged"}',NULL,NULL,9999,9999,1)"""
        )
        connection.commit()

    with pytest.raises(ReviewTaskRepositoryError, match="exact batch receipt"):
        list_current_review_tasks(database, limit=10)
    with pytest.raises(ReviewTaskRepositoryError, match="exact batch receipt"):
        lookup_review_task_version_heads(
            database,
            ("logical-forged",),
            scope="personal",
            task_type="value-review",
        )


def test_human_transition_cannot_predate_task_or_current_event(tmp_path: Path) -> None:
    database = _database(tmp_path)
    publication = _publication(19, (1,))
    publish_review_task_page(database, publication, expected_progress_revision=None)
    opened = list_current_review_tasks(database, limit=10).items[0]
    transition = replace(
        _transition(
            opened.task.task_id,
            opened.current_event.event_id,
            ReviewTaskState.OPEN,
            ReviewTaskState.RESOLVED,
            91,
        ),
        observed_ns=1,
        recorded_ns=1,
    )

    with pytest.raises(ReviewTaskCASConflict, match="time does not advance"):
        append_review_task_event(database, transition)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_task_events WHERE task_id=?",
            (opened.task.task_id,),
        ).fetchone() == (1,)


def test_missing_or_old_framework_state_is_never_created_or_migrated(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        list_current_review_tasks(missing, limit=10)
    assert not missing.exists()

    old = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(old)) as connection:
        framework_schema._build_v21_exact_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','21')")
        connection.commit()
    before = old.read_bytes()
    with pytest.raises(ReviewTaskRepositoryError, match="schema 22"):
        list_current_review_tasks(old, limit=10)
    assert old.read_bytes() == before
    with closing(sqlite3.connect(old)) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("21",)


def test_online_backup_restores_review_task_owner_facts(tmp_path: Path) -> None:
    database = _database(tmp_path / "source")
    publication = _publication(17, (1, 2))
    published = publish_review_task_page(
        database,
        publication,
        expected_progress_revision=None,
    )
    expected_page = list_current_review_tasks(database, limit=10)
    expected_progress = read_review_task_progress(database, publication.fence)
    restored = tmp_path / "restored.sqlite3"

    with (
        closing(sqlite3.connect(database)) as source,
        closing(sqlite3.connect(restored)) as destination,
    ):
        source.backup(destination)

    with closing(sqlite3.connect(restored)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert published.progress == expected_progress
    assert list_current_review_tasks(restored, limit=10) == expected_page
    assert read_review_task_progress(restored, publication.fence) == expected_progress


def test_event_crash_rolls_back_without_changing_current_head(tmp_path: Path) -> None:
    database = _database(tmp_path)
    publication = _publication(14, (1,))
    publish_review_task_page(database, publication, expected_progress_revision=None)
    opened = list_current_review_tasks(database, limit=10).items[0].current_event
    transition = _transition(
        "task-1-v1", opened.event_id, ReviewTaskState.OPEN, ReviewTaskState.IN_REVIEW, 90
    )

    def crash(_stage: str) -> None:
        raise RuntimeError("event crash")

    with pytest.raises(RuntimeError, match="event crash"):
        append_review_task_event(database, transition, _fault_injector=crash)
    current = list_current_review_tasks(database, limit=10).items[0].current_event
    assert current == opened
    assert _counts(database) == (1, 1, 1, 1)
