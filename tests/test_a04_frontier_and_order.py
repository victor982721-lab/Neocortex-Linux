"""Adversarial acceptance for durable effects and exact-search total ordering.

All state and content belong to tmp_path. No model, live Corpus or shared
state is accessed. NumPy is explicit for the persisted numeric-path tests.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from itertools import permutations
from pathlib import Path
import sqlite3

import pytest

from neocortex.persistence import framework_state_common as actions
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence import state_publication as publication
from tests.internal_paths_test_support import begin_signed_normal_run

TEST_CAPABILITIES = ("base", "inference")


def _intent(tmp_path: Path) -> tuple[Path, int]:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "source.dat"
    source.write_bytes(b"controlled action fixture")
    database = tmp_path / "state" / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = begin_signed_normal_run(state, root)
        action_id = state.begin_file_action(run_id, "correct_extension", str(source), str(root / "source.bin"), None, None, True)
    return database, action_id


def _assert_intent_only(database: Path, action_id: int) -> None:
    with closing(sqlite3.connect(database)) as observer:
        assert observer.execute("SELECT status,expected_identity_json,applying_ns FROM file_actions WHERE action_id=?", (action_id,)).fetchone() == ("started", None, None)
        assert observer.execute("SELECT COUNT(*) FROM file_action_events WHERE action_id=? AND to_status='applying'", (action_id,)).fetchone() == (0,)


def test_framework_defaults_to_full(tmp_path: Path) -> None:
    with FrameworkState(tmp_path / "framework.sqlite3") as state:
        assert state._connection.execute("PRAGMA main.synchronous").fetchone()[0] >= 2


def test_frontier_promotes_and_keeps_full_for_receipts(tmp_path: Path) -> None:
    database, action_id = _intent(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA synchronous=NORMAL")
        actions.mark_file_actions_applying(connection, ((action_id, '{}'),))
        assert connection.execute("PRAGMA main.synchronous").fetchone()[0] >= 2
        assert not connection.in_transaction
    with closing(sqlite3.connect(database)) as observer:
        assert observer.execute("SELECT status FROM file_actions").fetchone() == ("applying",)


def test_ignored_full_policy_abstains_before_any_update(tmp_path: Path) -> None:
    database, action_id = _intent(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.set_authorizer(lambda op, a, b, *_: sqlite3.SQLITE_IGNORE if op == sqlite3.SQLITE_PRAGMA and a == 'synchronous' and b else sqlite3.SQLITE_OK)
        with pytest.raises(RuntimeError, match="requires synchronous FULL"):
            actions.mark_file_actions_applying(connection, ((action_id, '{}'),))
        assert not connection.in_transaction
    _assert_intent_only(database, action_id)


def test_commit_rejection_preserves_original_and_reverts_all_rows(tmp_path: Path) -> None:
    database, action_id = _intent(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.set_authorizer(lambda op, a, *_: sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_TRANSACTION and a == 'COMMIT' else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            actions.mark_file_actions_applying(connection, ((action_id, '{}'),))
        assert not connection.in_transaction
        connection.set_authorizer(None)
        connection.commit()  # An unrelated later commit cannot publish denied applying rows.
    _assert_intent_only(database, action_id)


def test_rollback_failure_quarantines_connection_and_preserves_commit_error(tmp_path: Path) -> None:
    database, action_id = _intent(tmp_path)

    class BrokenRollback(sqlite3.Connection):
        def commit(self) -> None:
            raise sqlite3.OperationalError("injected COMMIT failure")

        def rollback(self) -> None:
            raise sqlite3.OperationalError("injected ROLLBACK failure")

    connection = sqlite3.connect(database, factory=BrokenRollback)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected COMMIT failure") as caught:
            actions.mark_file_actions_applying(connection, ((action_id, '{}'),))
        assert any("ROLLBACK failure" in note for note in caught.value.__notes__)
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    finally:
        connection.close()
    _assert_intent_only(database, action_id)


def test_uncertain_commit_never_acknowledges_or_retries_effect(tmp_path: Path) -> None:
    database, action_id = _intent(tmp_path)

    class UncertainCommit(sqlite3.Connection):
        def commit(self) -> None:
            super().commit()
            raise sqlite3.OperationalError("lost COMMIT acknowledgement")

    with closing(sqlite3.connect(database, factory=UncertainCommit)) as connection:
        with pytest.raises(sqlite3.OperationalError, match="lost COMMIT acknowledgement") as caught:
            actions.mark_file_actions_applying(connection, ((action_id, '{}'),))
        assert not connection.in_transaction
        assert any("reconciliation" in note for note in caught.value.__notes__)
    with closing(sqlite3.connect(database)) as observer:
        assert observer.execute("SELECT status FROM file_actions").fetchone() == ("applying",)


def test_frontier_rejects_foreign_transaction_without_rolling_it_back(tmp_path: Path) -> None:
    database, action_id = _intent(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="transaction ownership"):
            actions.mark_file_actions_applying(connection, ((action_id, '{}'),))
        assert connection.in_transaction
        connection.rollback()
    _assert_intent_only(database, action_id)


@pytest.mark.parametrize("operation", ("abort", "recover"))
@pytest.mark.parametrize("change", ("reordered", "revision", "digest", "schema", "owner", "missing", "duplicate"))
def test_owner_order_is_ignored_but_every_identity_component_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, change: str) -> None:
    from neocortex.api.cli import cli_semantic

    semantic = publication.StateOwnerHead("semantic", 2, 'a' * 64, 1)
    code = publication.StateOwnerHead("code", 3, 'b' * 64, 1)
    transaction = publication.begin_state_publication(tmp_path, operation="framework-all-semantic", owners=("semantic", "code"), idempotency_key="a04-owner-order", owner_heads=(semantic, code))
    replacements = {"revision": {"revision": 4}, "digest": {"digest_sha256": 'c' * 64}, "schema": {"schema_version": 2}, "owner": {"owner": "other"}}
    observed = (semantic, code)
    if change in replacements:
        observed = (replace(semantic, **replacements[change]), code)
    elif change == "missing":
        observed = (semantic,)
    elif change == "duplicate":
        observed = (semantic, code, semantic)
    monkeypatch.setattr(cli_semantic, "_observe_integrated_heads", lambda *_a, **_k: observed)
    invoke = (lambda: cli_semantic._resolve_integrated_publication_after_nonterminal(tmp_path, transaction)) if operation == "abort" else (lambda: cli_semantic._recover_pending_integrated_publication(tmp_path))
    if change == "reordered":
        assert invoke() is True
        assert publication.read_state_publication_state(tmp_path).status == "absent"
    else:
        try:
            result = invoke()
        except (ValueError, cli_semantic.StatePublicationRecoveryRequired):
            pass
        else:
            assert result is False
        assert publication.read_state_publication_state(tmp_path).status == "blocked"


def _expected_order(hit):
    # Independent acceptance oracle: explicit public policy, not implementation helper.
    return (-hit.score, hit.item_id, hit.entity_id, hit.indexed_model_signature, -hit.ref_id)


@pytest.mark.capability("inference")
@pytest.mark.parametrize("evidence_mode", (False, True))
def test_all_permutations_limits_and_representatives_obey_one_total_order(evidence_mode: bool) -> None:
    from neocortex.semantic import semantic_search_repository as repository
    from neocortex.semantic.semantic_models import ExactSearchPage
    from tests.test_retrieval_target_diagnostics import _hit

    hits = (_hit("item:z", "entity:z", 90, 0.5), _hit("item:a", "entity:z", 80, 0.5), _hit("item:a", "entity:a", 2, 0.5), replace(_hit("item:b", "entity:b", 70, 0.5), indexed_model_signature="model:z"), replace(_hit("item:b", "entity:b", 1, 0.5), indexed_model_signature="model:a"))
    def key(hit):
        return (hit.item_id, hit.entity_id) if evidence_mode else hit.item_id
    representatives = {}
    for hit in sorted(hits, key=_expected_order):
        representatives.setdefault(key(hit), hit)
    expected = tuple(sorted(representatives.values(), key=_expected_order))
    for ordered_input in permutations(hits):
        for limit in range(1, len(expected) + 1):
            best, heap = {}, []
            diagnostics = repository._TargetedSearchDiagnostics(("item:a", "item:b", "item:z"), evidence_mode)
            for hit in ordered_input:
                diagnostics.observe(hit)
                if evidence_mode:
                    repository._retain_exact_evidence_hit(hit, limit=limit, best_by_evidence=best, heap=heap)
                else:
                    repository._retain_exact_search_hit(hit, limit=limit, best_by_item=best, heap=heap)
            selected = tuple(sorted((value[2] for value in best.values()), key=_expected_order))
            assert selected == expected[:limit]
            assert len(heap) <= max(limit * 2, limit + 64)
            for row in diagnostics.export(ExactSearchPage(selected, len(hits), None, True))["target_diagnostics"]:
                assert (row["raw_rank"] <= limit) == row["within_candidate_window"]


@pytest.mark.capability("inference")
@pytest.mark.parametrize("dtype", ("float16", "float32"))
@pytest.mark.parametrize("evidence_mode", (False, True))
@pytest.mark.parametrize("batch_size", (8, 16))
def test_persisted_numeric_and_scalar_all_tie_windows_match_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dtype: str, evidence_mode: bool, batch_size: int) -> None:
    from neocortex.semantic import semantic_exact_index_format as codec
    from tests import semantic_exact_index_fixtures as fixtures
    from tests.test_semantic_exact_index_equivalence import _build_and_open, _search, _page_oracle, _forbid_native_sql, repository

    monkeypatch.setattr(fixtures, "_vector_for", lambda *_a, **_k: (1.0, 0.0, 0.0, 0.0))
    fixture = fixtures.published_text_fixture(tmp_path, rows=48, dtype=dtype)
    native_all = _search(fixture, evidence_mode=evidence_mode, limit=48, max_vectors=48, batch_size=batch_size)
    expected = tuple(sorted(native_all.hits, key=_expected_order))
    assert native_all.hits == expected
    handle = _build_and_open(fixture, tmp_path / "index")
    try:
        for limit in (1, 2, 7, 8, 15, 16, 46, 48):
            native = _search(fixture, evidence_mode=evidence_mode, limit=limit, max_vectors=48, batch_size=batch_size)
            assert native.hits == expected[:limit]
            with monkeypatch.context() as guarded:
                guarded.setattr(repository, "_search_sql", _forbid_native_sql)
                numeric = _search(fixture, exact_index=handle, evidence_mode=evidence_mode, limit=limit, max_vectors=48, batch_size=batch_size)
            assert _page_oracle(numeric) == _page_oracle(native)
            diagnostics = {}
            scalar = codec.query_exact_view(handle._view, codec.ExactQuery(fixture.query.query_model_signature, fixture.query.vector_space, fixture.query.dimensions, fixture.query.vector, "text", fixture.query.indexed_model_signatures), live_owner_binding=handle._owner.binding, limit=limit, max_vectors=48, batch_size=batch_size, text_scope="content", evidence_mode=evidence_mode, numeric=False, diagnostic_item_ids=fixture.item_ids[:3], diagnostics=diagnostics, hydrate_provenance=True)
            assert _page_oracle(scalar) == _page_oracle(native)
            for row in diagnostics["target_diagnostics"]:
                assert (row["raw_rank"] <= limit) == row["within_candidate_window"]
        assert handle.usage_summary()["used_queries"] == 8
        assert handle.usage_summary()["fallback_queries"] == 0
    finally:
        handle.close()
