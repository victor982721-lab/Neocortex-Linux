"""Reset acceptance through actual owners, public selectors and new runs."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.deduplication.inventory import DedupIndex
from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.documents.document_catalog import (
    CatalogPublicationConflict, _begin_catalog_run, initialize_document_catalog,
    read_catalog_publication_manifest,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.operational_freshness import operational_identity_floor
from neocortex.persistence.state_reset import (
    STATE_RESET_CONFIRMATION, StateResetError, apply_state_reset, plan_state_reset,
)
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY
from tests.internal_paths_test_support import begin_signed_normal_run


def _three_owners(tmp_path: Path):
    state, corpus = tmp_path / "state", tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    (corpus / "original.txt").write_bytes(b"original")
    with FrameworkState(state / "framework.sqlite3") as framework:
        historical = begin_signed_normal_run(framework, corpus)
        framework.fail_initial_run(historical)
        action = framework.begin_file_action(historical, "fixture", str(corpus / "original.txt"),
                                             str(corpus / "target.txt"), None, "fixture", True)
        framework.require_file_action_recovery((action,), "uncertain fixture")
        retired = begin_signed_normal_run(framework, corpus)
        framework.fail_initial_run(retired)
    initialize_inventory_schema(state / "dedup.sqlite3")
    with closing(sqlite3.connect(state / "dedup.sqlite3")) as connection, connection:
        connection.executescript("""
            INSERT INTO scans(scan_id,root,started_ns,completed_ns,status)
                VALUES(5,'/history',1,2,'complete'),(9,'/derived',3,4,'complete');
            INSERT INTO duplicate_plan_summaries(scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns)
                VALUES(5,0,0,0,5);
            INSERT INTO inventory_generation_heads(scan_id,content_digest,created_ns)
                VALUES(9,X'01',5);
        """)
    initialize_document_catalog(state / "document_catalog.sqlite3")
    with closing(sqlite3.connect(state / "document_catalog.sqlite3")) as connection, connection:
        connection.execute("INSERT INTO catalog_runs(catalog_run_id,framework_run_id,source_kind,mode,status,started_ns) VALUES(11,?,'text','fixture','completed',1)", (historical,))
        connection.executescript("""
            INSERT INTO catalog_generations(generation_id,catalog_run_id,source_kind,status,started_ns,published_ns)
                VALUES(11,11,'text','published',1,3),(17,NULL,'text','cancelled',2,NULL);
            INSERT INTO catalog_generation_manifests(generation_id,source_kind,source_fence_json,created_ns)
                VALUES(11,'text','{}',3),(17,'text','{}',2);
            INSERT INTO catalog_publications(source_kind,generation_id,published_ns) VALUES('text',11,3);
        """)
    return state, corpus, historical, retired, action


def test_three_owner_reset_is_fresh_preserves_history_and_does_not_reuse_ids(tmp_path: Path):
    state, corpus, historical, retired, action = _three_owners(tmp_path)
    before = plan_state_reset(state, scope="all")
    result = apply_state_reset(before, confirmation=STATE_RESET_CONFIRMATION)
    payload = result.as_payload()
    assert payload["operational_freshness"] == "fresh"
    assert payload["selected_owner_count"] == payload["transformed_owner_count"] == 3
    assert payload["removed_owner_database_count"] == 0
    assert payload["preserved_owner_database_count"] == 3
    assert all(item.authoritative_rows_preserved and item.references_valid for item in result.owner_verifications)
    # Replay is a physical no-op, including all preserved owner inodes.
    names = ("framework.sqlite3", "dedup.sqlite3", "document_catalog.sqlite3")
    identities = {name: (state / name).stat().st_ino for name in names}
    replay = apply_state_reset(plan_state_reset(state, scope="all"), confirmation=STATE_RESET_CONFIRMATION)
    assert replay.as_payload()["effect_outcome"] == "no_changes"
    assert replay.as_payload()["transformed_owner_count"] == 0
    assert identities == {name: (state / name).stat().st_ino for name in names}
    with FrameworkState(state / "framework.sqlite3", existing_only=True) as framework:
        assert framework._connection.execute("SELECT status FROM file_actions WHERE action_id=?", (action,)).fetchone()[0] == "recovery_required"
        assert framework.resumable_route_candidate_run_ids() == ()
        assert framework.latest_durable_inventory_run(corpus) is None
        with pytest.raises(RuntimeError, match="retired"):
            framework.begin_operational_run(corpus, run_kind="resume", source_run_id=historical)
        new_run = begin_signed_normal_run(framework, corpus)
        assert new_run > retired
        framework.fail_initial_run(new_run)
        new_action = framework.begin_file_action(new_run, "fixture", str(corpus / "original.txt"), None, None, "fixture", True)
        assert new_action > action
    with DedupIndex(state / "dedup.sqlite3") as inventory:
        with pytest.raises(RuntimeError, match="retired"):
            inventory.scan_content_digest(5)
        assert inventory.scan(corpus).scan_id > 9
    with closing(sqlite3.connect(state / "document_catalog.sqlite3")) as connection:
        assert connection.execute("SELECT generation_id FROM catalog_generations").fetchall() == [(11,)]
        assert operational_identity_floor(connection, "catalog") == 17
        with pytest.raises(CatalogPublicationConflict):
            read_catalog_publication_manifest(connection, "text")
        build = _begin_catalog_run(connection, source_kind="text", framework_run_id=None)
        assert build.base_generation_id is None and build.generation_id > 17 and build.catalog_run_id > 17
    assert (corpus / "original.txt").read_bytes() == b"original"


def test_unknown_empty_owner_table_blocks_before_any_effect(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    initialize_inventory_schema(state / "dedup.sqlite3")
    with closing(sqlite3.connect(state / "dedup.sqlite3")) as connection, connection:
        connection.execute("CREATE TABLE unclassified_future_table(value TEXT)")
    cache = state / "runtime-cache"
    cache.mkdir()
    (cache / "x").write_bytes(b"preserve until policy exists")
    plan = plan_state_reset(state, scope="all")
    assert any("unknown-table" in value for value in plan.as_payload()["blocked_by"])
    with pytest.raises(StateResetError, match="unknown-table"):
        apply_state_reset(plan, confirmation=STATE_RESET_CONFIRMATION)
    assert (cache / "x").is_file()


def test_registry_declares_every_table_in_three_current_owners(tmp_path: Path):
    state, *_ = _three_owners(tmp_path)
    assert len(STATE_STORE_REGISTRY.stores) == 13
    for owner in ("framework", "inventory", "catalog"):
        contract = STATE_STORE_REGISTRY.by_owner(owner)
        with closing(sqlite3.connect(state / contract.database_name)) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        assert all(contract.lifecycle_rule(table) is not None for table in tables)
        assert len(contract.lifecycle_rules) == len({rule.table for rule in contract.lifecycle_rules})
        assert contract.lifecycle_rule("unknown") is None
