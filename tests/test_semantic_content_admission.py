"""Focused synthetic contracts for Semantic content admission and reuse."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from neocortex.persistence.framework_content_admission import ContentAdmissionLedger
from neocortex.semantic.semantic_admission import (
    CONTENT_IDENTITY_ALGORITHM,
    ContentAdmissionPolicy,
    SemanticSingleFlight,
    evaluate_content_admission,
    filter_search_hits,
    filter_semantic_items,
    group_reusable_work,
    semantic_identity_for_item,
    work_identity_for_item,
)
from neocortex.semantic.semantic_models import SemanticItem, fingerprint_text
from neocortex.semantic import semantic_sources


def _item(item_id: str, source_identity: str, path: str) -> SemanticItem:
    return SemanticItem(
        item_id=item_id,
        source_kind="text",
        source_identity=source_identity,
        identity_version="fixture-v1",
        fingerprint=fingerprint_text("same synthetic content"),
        path=path,
        source_revision={"volume_id": 1, "file_id": int(source_identity), "processing_signature": "p"},
    )


def test_identity_separates_locations_but_reuses_path_independent_work() -> None:
    first = _item("item:first", "10", "/synthetic/first.txt")
    second = _item("item:second", "11", "/synthetic/second.txt")

    first_identity = semantic_identity_for_item(first)
    second_identity = semantic_identity_for_item(second)
    assert first_identity.physical != second_identity.physical
    assert first_identity.content == second_identity.content
    assert (
        work_identity_for_item(first, model_signature="model-v1", role="passage").key
        == work_identity_for_item(second, model_signature="model-v1", role="passage").key
    )
    groups = group_reusable_work((first, second), model_signature="model-v1", role="passage")
    assert list(groups.values()) == [(first, second)]


def test_policy_exclusion_hides_current_projection_without_changing_eligibility() -> None:
    item = _item("item:excluded", "12", "/synthetic/excluded.txt")
    policy = ContentAdmissionPolicy(excluded_item_ids=(item.item_id,))

    decision = evaluate_content_admission(item, policy)
    assert decision.eligible is True
    assert decision.visible is False
    assert decision.reason_code == "item_excluded"
    assert tuple(filter_semantic_items((item,), policy)) == ()
    visible_hit = SimpleNamespace(item_id="item:visible", provenance={})
    excluded_hit = SimpleNamespace(item_id=item.item_id, provenance={})
    assert tuple(filter_search_hits((visible_hit, excluded_hit), policy)) == (visible_hit,)


def test_single_flight_coalesces_same_content_across_locations() -> None:
    first = _item("item:first", "20", "/synthetic/first.txt")
    second = _item("item:second", "21", "/synthetic/second.txt")
    work_first = work_identity_for_item(first, model_signature="model-v1", role="passage")
    work_second = work_identity_for_item(second, model_signature="model-v1", role="passage")
    assert work_first.key == work_second.key

    calls: list[int] = []
    flight = SemanticSingleFlight()

    def produce() -> int:
        calls.append(1)
        return 42

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda work: flight.run(work, produce),
                (work_first, work_second),
            )
        )
    assert len(calls) == 1
    assert {result.value for result in results} == {42}
    assert sum(result.leader for result in results) == 1


def test_framework_ledger_persists_policy_and_correction_in_one_owner() -> None:
    item = _item("item:ledger", "30", "/synthetic/ledger.txt")
    connection = sqlite3.connect(":memory:")
    try:
        ledger = ContentAdmissionLedger(connection)
        first_policy = ContentAdmissionPolicy(version=1)
        ledger.record_policy("synthetic-corpus", first_policy, recorded_ns=1)
        first = ledger.record_admission("synthetic-corpus", item, recorded_ns=2)
        assert first.identity.content.algorithm == CONTENT_IDENTITY_ALGORITHM
        assert (
            ledger.current_admission("synthetic-corpus", item.item_id).identity.content.algorithm
            == CONTENT_IDENTITY_ALGORITHM
        )

        second_policy = ContentAdmissionPolicy(version=2, excluded_item_ids=(item.item_id,))
        corrected = ledger.record_admission(
            "synthetic-corpus",
            item,
            policy=second_policy,
            correction_of_id=first.admission_id,
            recorded_ns=3,
            diagnostics={"fixture": "synthetic"},
        )

        assert corrected.visible is False
        assert corrected.correction_of_id == first.admission_id
        assert ledger.read_policy("synthetic-corpus").policy.version == 2  # type: ignore[union-attr]
        assert ledger.current_admission("synthetic-corpus", item.item_id).admission_id == corrected.admission_id  # type: ignore[union-attr]
        assert connection.execute(
            "SELECT COUNT(*) FROM semantic_content_admission_events"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_source_iterator_factories_match_all_stage_callback_shapes(monkeypatch) -> None:
    item = _item("item:factory", "40", "/synthetic/factory.txt")
    policy = ContentAdmissionPolicy()
    text_record = SimpleNamespace(item=item)
    image_record = SimpleNamespace(item=item)

    def fake_text_records(state_directory, source_kind, *, policy, connection=None):
        assert state_directory == Path("/synthetic/state")
        assert source_kind == "text"
        assert policy is not None
        assert connection is None
        yield text_record

    def fake_image_records(state_directory, *, verify_snapshots=True):
        assert state_directory == Path("/synthetic/state")
        assert verify_snapshots is True
        yield image_record

    monkeypatch.setattr(semantic_sources, "iter_admitted_text_source_records", fake_text_records)
    monkeypatch.setattr(semantic_sources, "iter_image_source_records", fake_image_records)

    text_iterator = semantic_sources.admitted_text_source_iterator(policy)
    image_iterator = semantic_sources.admitted_image_source_iterator(policy)
    assert tuple(text_iterator(Path("/synthetic/state"), "text")) == (text_record,)
    assert tuple(image_iterator(Path("/synthetic/state"))) == (image_record,)
