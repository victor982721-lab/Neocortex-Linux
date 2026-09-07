"""Read-only curation preserves individual proof, requested policy and topology."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.curation import preview as preview_module
from neocortex.curation.preview import CurationStateError, build_curation_plan_page, build_curation_preview
from neocortex.deduplication import DedupIndex, DedupPlanner, FileChangedError, InventoryCheckpoint
from neocortex.deduplication.persistence.ddl import build_v11_schema
from neocortex.documents.document_catalog import initialize_document_catalog


def _state(tmp_path: Path, *, exact: bool = True, groups: int = 3, first_members: int = 2) -> Path:
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    for group in range(groups):
        for member in range(first_members if group == 0 else 2):
            (corpus / f"group-{group:04d}-member-{member:04d}.bin").write_bytes(f"payload-for-group-{group}".encode())
    with DedupIndex(state / "dedup.sqlite3") as index:
        scan = index.scan(corpus, excluded_paths=())
        index.bind_inventory_checkpoint(InventoryCheckpoint(str(corpus), scan.scan_id, None, None, None, True))
        DedupPlanner(index).plan(scan.scan_id, exact_compare=exact)
    initialize_document_catalog(state / "document_catalog.sqlite3")
    return state


def _manifest(state: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in state.iterdir() if path.is_file()}


@pytest.mark.parametrize("exact", [False, True])
def test_preview_exposes_requested_policy_and_individual_proof(tmp_path: Path, exact: bool) -> None:
    state = _state(tmp_path, exact=exact)
    before = _manifest(state)
    page = build_curation_plan_page(state, 20)
    preview = build_curation_preview(state, limit=20)
    for projection in (page.to_dict(), preview.to_dict()):
        summary = projection["dedup_verification"]
        assert summary["requested_policy"] == ("exact" if exact else "fast")
        assert summary["verification_coverage"] == "complete"
        assert summary["verification_scope"] == "plan"
        assert summary["exact_comparisons"] == (3 if exact else 0)
        assert projection["nominal_redundant_bytes"] == projection["reclaimable_bytes"]
        assert projection["physical_reclaimable_bytes"] is None
    for item in page.items:
        evidence = item.evidence
        assert evidence["verification_mode"] == ("full_hash" if exact else "fast")
        assert evidence["verification_scope"] == "group"
        assert evidence["group_proof"]["comparison_method"] == ("byte_for_byte" if exact else "full_xxh3")
        assert evidence["actionability"] == "review_required"
        assert evidence["physical_reclaimable_bytes"] is None
        assert evidence["members"][0]["proof"]["comparison_result"] == "reference"
        assert evidence["members"][1]["proof"]["comparison_result"] == ("equal" if exact else "fingerprint_match")
    assert _manifest(state) == before


def test_legacy_v11_global_label_does_not_invent_individual_proof(tmp_path: Path) -> None:
    state = _state(tmp_path)
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as connection:
        build_v11_schema(connection)
        connection.execute("ATTACH DATABASE ? AS source", (str(state / "dedup.sqlite3"),))
        tables = [row[0] for row in connection.execute("SELECT name FROM main.sqlite_master WHERE type='table'")]
        for table in tables:
            columns = ",".join(row[1] for row in connection.execute(f"PRAGMA main.table_info({table})"))
            connection.execute(f"INSERT INTO main.{table}({columns}) SELECT {columns} FROM source.{table}")
        connection.execute("UPDATE main.metadata SET value='11' WHERE key='schema_version'")
    os.replace(legacy, state / "dedup.sqlite3")
    before = _manifest(state)
    page = build_curation_plan_page(state, 20)
    assert page.to_dict()["dedup_verification"]["requested_policy"] == "legacy_unknown"
    assert page.to_dict()["dedup_verification"]["verification_mode"] == "full_hash"
    for item in page.items:
        assert item.evidence["verification_mode"] == "legacy_unknown"
        assert item.evidence["group_proof"] is None
        assert item.evidence["plan_verification_mode"] == "full_hash"
        assert all(member["proof"]["comparison_result"] == "not_recorded" for member in item.evidence["members"])
    assert _manifest(state) == before


def test_group_proof_stays_exact_when_other_candidate_failed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with DedupIndex(state / "dedup.sqlite3") as index:
        scan_id = index._connection.execute("SELECT MAX(scan_id) FROM scans").fetchone()[0]
        planner = DedupPlanner(index)
        original = planner._fingerprint

        def fail_one(snapshot, *, partial):
            if Path(snapshot.path).name == "group-0002-member-0001.bin":
                raise FileChangedError("fixture candidate changed")
            return original(snapshot, partial=partial)

        with patch.object(planner, "_fingerprint", side_effect=fail_one):
            planner.plan(scan_id, exact_compare=True)
    page = build_curation_plan_page(state, 20)
    assert page.coverage == "partial"
    assert page.to_dict()["dedup_verification"]["verification_coverage"] == "partial"
    assert all(item.evidence["verification_mode"] == "full_hash" for item in page.items)
    assert all(item.evidence["plan_verification_mode"] == "partial" for item in page.items)


def test_unsampled_member_proof_changes_digest_and_invalidates_cursor(tmp_path: Path) -> None:
    state = _state(tmp_path, first_members=70)
    first = build_curation_plan_page(state, 1)
    assert first.next_cursor is not None
    assert first.items[0].evidence["members_truncated"]
    group_id = first.items[0].evidence["group_id"]
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        raw = connection.execute(
            "SELECT proof_json FROM planned_duplicate_members WHERE group_id=? AND member_order=65", (group_id,),
        ).fetchone()[0]
        proof = json.loads(raw)
        proof["fingerprint_source"] = "cached"
        connection.execute(
            "UPDATE planned_duplicate_members SET proof_json=? WHERE group_id=? AND member_order=65",
            (json.dumps(proof), group_id),
        )
    changed = build_curation_plan_page(state, 1)
    assert changed.plan_digest != first.plan_digest
    assert changed.items[0] == first.items[0]
    with pytest.raises(CurationStateError, match="snapshot changed"):
        build_curation_plan_page(state, 1, first.next_cursor)


def test_group_proof_is_in_full_digest(tmp_path: Path) -> None:
    state = _state(tmp_path)
    first = build_curation_plan_page(state, 1)
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        raw = connection.execute("SELECT proof_json FROM planned_duplicate_groups ORDER BY group_id LIMIT 1").fetchone()[0]
        proof = json.loads(raw)
        proof["keeper_factors"].append("explicit_user_decision")
        connection.execute("UPDATE planned_duplicate_groups SET proof_json=? WHERE group_id=1", (json.dumps(proof),))
    changed = build_curation_plan_page(state, 1)
    assert changed.plan_digest != first.plan_digest


def test_pages_reuse_publication_and_stream_members_without_n_queries(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, groups=20)
    publications = 0
    supplied_member_streams = 0
    original_publish = preview_module._digest_plan_publication
    original_group = preview_module._digest_duplicate_group

    def publish(**kwargs):
        nonlocal publications
        publications += 1
        return original_publish(**kwargs)

    def digest_group(*args, **kwargs):
        nonlocal supplied_member_streams
        assert kwargs.get("members") is not None
        supplied_member_streams += 1
        return original_group(*args, **kwargs)

    monkeypatch.setattr(preview_module, "_digest_plan_publication", publish)
    monkeypatch.setattr(preview_module, "_digest_duplicate_group", digest_group)
    first = build_curation_plan_page(state, 1)
    second = build_curation_plan_page(state, 1, first.next_cursor)
    assert first.plan_digest == second.plan_digest
    assert publications == 1 and supplied_member_streams == 20


def test_bad_member_proof_reports_owner_group_and_member(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        connection.execute("UPDATE planned_duplicate_members SET proof_json='null' WHERE group_id=1 AND member_order=1")
    with pytest.raises(CurationStateError) as caught:
        build_curation_plan_page(state, 1)
    assert caught.value.code == "curation_duplicate_proof_invalid"
    assert caught.value.context["owner"] == "dedup.sqlite3"
    assert caught.value.context["record_id"] == {"group_id": 1, "member_order": 1}


def test_preview_rejects_duplicate_physical_member_identity(tmp_path: Path) -> None:
    state = _state(tmp_path, groups=1)
    database = state / "dedup.sqlite3"
    with sqlite3.connect(database) as connection:
        group_id = connection.execute(
            "SELECT group_id FROM planned_duplicate_groups LIMIT 1"
        ).fetchone()[0]
        keeper = connection.execute(
            "SELECT volume_id,file_id FROM planned_duplicate_members "
            "WHERE group_id=? AND member_order=0",
            (group_id,),
        ).fetchone()
        connection.execute(
            "UPDATE planned_duplicate_members SET volume_id=?,file_id=? "
            "WHERE group_id=? AND member_order=1",
            (*keeper, group_id),
        )
        connection.commit()

    with pytest.raises(CurationStateError, match="physical identity is duplicated"):
        build_curation_plan_page(state, 20)


def test_preview_exposes_hardlink_aliases_without_physical_savings(tmp_path: Path) -> None:
    state = _state(tmp_path, groups=1)
    corpus = tmp_path / "corpus"
    original = corpus / "group-0000-member-0000.bin"
    alias = corpus / "hardlink-alias.bin"
    os.link(original, alias)
    with DedupIndex(state / "dedup.sqlite3") as index:
        scan = index.scan(corpus, excluded_paths=())
        index.bind_inventory_checkpoint(InventoryCheckpoint(str(corpus), scan.scan_id, None, None, None, True))
        DedupPlanner(index).plan(scan.scan_id)
    page = build_curation_plan_page(state, 10)
    member = next(member for member in page.items[0].evidence["members"] if member["proof"]["alias_count"] == 2)
    assert set(member["proof"]["aliases"]) == {str(alias), str(original)}
    assert member["proof"]["observed_link_count"] == 2
    assert page.items[0].evidence["physical_reclaimable_bytes"] is None


def test_writer_checkpoint_changes_fence_not_semantic_cursor(tmp_path: Path, monkeypatch) -> None:
    """Two publications are required when an open fixture writer closes its WAL."""

    state = _state(tmp_path)
    database = state / "dedup.sqlite3"
    publications = 0
    original = preview_module._digest_plan_publication

    def publish(**kwargs):
        nonlocal publications
        publications += 1
        return original(**kwargs)

    monkeypatch.setattr(preview_module, "_digest_plan_publication", publish)
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE metadata SET value=value WHERE key='schema_version'")
        writer.commit()
        before_close = preview_module._owner_generation_key(database)
        first = build_curation_plan_page(state, 1)
        assert first.next_cursor is not None and publications == 1
    finally:
        writer.close()
    assert preview_module._owner_generation_key(database) != before_close
    second = build_curation_plan_page(state, 1, first.next_cursor)
    assert publications == 2
    assert second.plan_digest == first.plan_digest
    assert second.snapshot_id == first.snapshot_id
