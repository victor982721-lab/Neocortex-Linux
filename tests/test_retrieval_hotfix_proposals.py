"""Focused regressions for the validated retrieval hotfixes.

These tests use synthetic owners and deterministic local fixtures only. They do
not load models, read user corpus, or exercise the experimental full-fit path.
"""
from __future__ import annotations

import copy
import random
import weakref
from pathlib import Path

import pytest

from neocortex.knowledge import knowledge_context_v2 as context
from neocortex.semantic import semantic_classification_service as classification
from neocortex.semantic import semantic_text_index as indexing
from neocortex.semantic.semantic_generation_worker import run_generation
from neocortex.semantic.semantic_state import start_embedding_generation
from neocortex.semantic.semantic_work_budget import SemanticWorkBudget
from tests.test_knowledge_context_v2 import _entry, _hit
from tests.test_semantic_incremental_resources import _revision, _revision_database
from tests.test_semantic_text_staging_session import (
    CHUNKING,
    _FixtureBackend,
    _generation,
    _model,
    _records,
    _stage,
)

TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


@pytest.mark.parametrize("order", ("ascending", "descending", "unrelated"))
def test_revision_speculation_preserves_replay_and_limits_transfer(
    tmp_path, monkeypatch, order
):
    database = tmp_path / "semantic.sqlite3"
    baseline = _generation(database, "retrieval-hotfix-base")
    records = _records(260)
    assert _stage(database, baseline, records) == (260, 520, 520)
    assert run_generation(
        database, baseline, _FixtureBackend(_model()), queued=520
    ).summary.status == "ready"
    generation = start_embedding_generation(
        database,
        model_signature=_model().model_signature,
        processing_signature="retrieval-hotfix-successor",
        materialize_base=False,
    )
    groups = {}
    for record in records:
        groups.setdefault(record.item.item_id, []).append(record)
    identities = sorted(groups)
    if order == "descending":
        identities.reverse()
    elif order == "unrelated":
        random.Random(6213).shuffle(identities)
    consumed = []
    windows = []
    original = indexing._published_item_revision_keys

    def lookup(*args, **kwargs):
        result = original(*args, **kwargs)
        windows.append((kwargs["batch_size"], len(result)))
        return result

    def source_records(_state, _source):
        for identity in identities:
            consumed.append(identity)
            yield from groups[identity]

    monkeypatch.setattr(indexing, "_published_item_revision_keys", lookup)
    budget = SemanticWorkBudget(max_items=1, max_new_jobs=1)
    result = indexing._stage_source(
        database,
        database.parent,
        "pdf",
        generation_id=generation,
        base_generation_id=baseline,
        refresh_token="retrieval-hotfix-refresh",
        chunking=CHUNKING,
        source_record_iterator=source_records,
        work_budget=budget,
    )
    assert result == (0, 0, 0, True)
    assert consumed == identities
    assert budget.items_admitted == budget.new_jobs_admitted == 0
    assert all(size <= 128 for _, size in windows)
    if order == "unrelated":
        assert any(requested == 1 for requested, _ in windows)
        assert sum(size for _, size in windows) <= 260 + 4 * 128
    else:
        assert len(windows) == (4 if order == "descending" else 3)
        assert sum(size for _, size in windows) == 260


def test_single_item_revision_read_does_not_hide_historical_conflict():
    connection = _revision_database()
    try:
        _revision(connection, 1, "item-conflict")
        _revision(connection, 2, "item-conflict", '{"changed":true}')
        connection.executemany(
            "INSERT INTO embedding_generation_members VALUES(1,'text_chunk','item-conflict',?)",
            ((1,), (2,)),
        )
        assert indexing._published_item_revision_keys(
            connection,
            1,
            "pdf",
            first_item_id="item-conflict",
            batch_size=1,
        ) == {"item-conflict": None}
    finally:
        connection.close()


def _context_entries(kind):
    if kind == "long":
        hits = [
            _hit(
                f"e:{index}",
                resource=f"file:{index}",
                snippet=(f"Manual de presión del equipo {index}. " * 200)[:4096],
            )
            for index in range(8)
        ]
        hits[0]["evidence"]["symbol"] = "fixture_symbol"
        return [_entry(*hits)]
    if kind == "counterwitness":
        return [
            _entry(
                _hit(
                    "e:positive",
                    snippet=(
                        "Se exige registrar la presión. " * 100
                        + " No se exige reemplazar la válvula."
                    ),
                ),
                _hit("e:negative", snippet="No basta con medir presión. " * 140),
                _hit("e:visual", owner="image", resource="image:1", snippet=None),
            )
        ]
    return [
        _entry(_hit("e:personal", snippet="Presión sin pérdidas. " * 100)),
        _entry(
            _hit("e:framework", snippet="Registro del manual. " * 150),
            scope="framework",
        ),
    ]


def test_prototype_refresh_releases_obsolete_decoded_vectors_before_next_phase(
    monkeypatch,
):
    concept = classification.all_concepts()[0]
    backend = _FixtureBackend(_model())
    expected = classification.label_prototype(
        concept, backend.model, backend.model.modality
    )
    weak = []
    loads = 0

    class Loaded:
        def __init__(self):
            self.prototype = expected
            self.vector = (1.0, 0.0, 0.0, 0.0)

    def load(*_args, **_kwargs):
        nonlocal loads
        loads += 1
        if loads == 2:
            assert weak[0]() is None
        value = Loaded()
        if loads == 1:
            weak.append(weakref.ref(value))
        return (value,)

    def embed(_database, _backend, missing):
        assert missing == ()
        assert weak[0]() is None

    monkeypatch.setattr(classification, "load_label_prototypes", load)
    monkeypatch.setattr(classification, "_embed_missing_prototypes", embed)
    monkeypatch.setattr(
        classification,
        "finalize_label_prototype_refresh",
        lambda *_args, **_kwargs: None,
    )
    result = classification.prepare_label_prototypes(
        Path("unused-synthetic-owner"),
        backend,
        target_modality=backend.model.modality,
        concepts_provider=lambda _modality: (concept,),
    )
    assert loads == 2 and result[0].prototype == expected
    assert result[0].vector == (1.0, 0.0, 0.0, 0.0)


def test_excerpt_expansion_reuses_graph_without_changing_projection(monkeypatch):
    entries = _context_entries("long")
    original_graph = context._refresh_graph_projection
    original_measure = context._measure
    calls = {"graph": 0, "measure": 0}

    def graph(*args):
        calls["graph"] += 1
        return original_graph(*args)

    def measure(*args):
        calls["measure"] += 1
        return original_measure(*args)

    monkeypatch.setattr(context, "_refresh_graph_projection", graph)
    monkeypatch.setattr(context, "_measure", measure)
    payload = context.build_context_response_v2(
        entries,
        query="manual presión",
        scope="all",
        request_id="retrieval-hotfix",
        limit=8,
        max_characters=50000,
        transport="json",
    )
    projected_again = copy.deepcopy(payload)
    original_graph(projected_again, entries)
    assert projected_again == payload
    assert payload.get("citations")
    assert calls["graph"] <= 9
    assert calls["measure"] > calls["graph"]
    assert all(
        citation["answer_sufficiency"] == "not_assessed"
        for citation in payload["citations"]
    )
