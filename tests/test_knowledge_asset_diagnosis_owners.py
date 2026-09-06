"""Federated diagnosis consumes actual published owner fixtures, never files."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from neocortex.capabilities.formats.archive.state import initialize_archive_state
from neocortex.deduplication.domain.evidence import (
    PROOF_VERSION, DuplicateGroupProof, DuplicateMemberProof,
)
from neocortex.deduplication.inventory.plan_evidence import encode_proof
from neocortex.knowledge.knowledge_asset_health import inspect_knowledge_asset_health
from test_knowledge_asset_health import _blob, _create_health_fixture, _quiesce, _state_fingerprint


def _exact_duplicates(path: Path, *, exact: bool = True, current_member: bool = True) -> None:
    group_proof = DuplicateGroupProof(
        proof_version=PROOF_VERSION, requested_policy="exact" if exact else "fast",
        comparison_method="byte_for_byte" if exact else "full_xxh3",
        comparison_result="equal" if exact else "fingerprint_match", missing_checks=(),
        keeper_policy_version="keeper-fixture-v1", keeper_reason="stable_path_tiebreaker",
    )
    keeper_proof = DuplicateMemberProof(
        proof_version=PROOF_VERSION, comparison_method="full_xxh3", comparison_result="reference",
        fingerprint_algorithm="xxh3_128", fingerprint_source="computed", missing_checks=(),
        aliases=("/corpus/docs/asset.txt",), alias_count=1, observed_link_count=1,
    )
    redundant_proof = replace(
        keeper_proof, comparison_method="byte_for_byte" if exact else "full_xxh3",
        comparison_result="equal" if exact else "fingerprint_match", compared_to_identity=(11, 3),
        comparison_bytes=800 if exact else None,
        missing_checks=() if exact else ("byte_for_byte_comparison",),
        aliases=("/corpus/docs/asset2.txt",),
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO files VALUES(1,'/corpus/docs/asset2.txt',?,?,800,?,-1)""",
            (_blob(11), _blob(4), 123 if current_member else 124),
        )
        connection.execute(
            """INSERT INTO duplicate_plan_summaries(
            scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns,verification_mode,coverage)
            VALUES(1,1,1,800,10,?,'complete')""", ("full_hash" if exact else "fast",),
        )
        connection.execute(
            """INSERT INTO planned_duplicate_groups(
            group_id,scan_id,size,keep_path,redundant_count,reclaimable_bytes,full_fingerprint,
            verification_mode,proof_json) VALUES(1,1,800,'/corpus/docs/asset.txt',1,800,?, ?, ?)""",
            ("a" * 32, "full_hash" if exact else "fast", encode_proof(group_proof)),
        )
        connection.executemany(
            """INSERT INTO planned_duplicate_members(
            group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns,proof_json)
            VALUES(1,?,?,?,?,?,800,123,-1,?)""",
            ((0, "keep", "/corpus/docs/asset.txt", _blob(11), _blob(3), encode_proof(keeper_proof)),
             (1, "redundant", "/corpus/docs/asset2.txt", _blob(11), _blob(4), encode_proof(redundant_proof))),
        )
    _quiesce(path)


@pytest.mark.parametrize("exact,current_member,proved", [(True, True, True), (False, True, False),
                                                         (True, False, False)])
def test_duplicate_diagnosis_requires_complete_exact_current_member_proofs(
    tmp_path: Path, exact: bool, current_member: bool, proved: bool,
) -> None:
    fixture = _create_health_fixture(tmp_path)
    _exact_duplicates(fixture.paths.inventory, exact=exact, current_member=current_member)
    before = _state_fingerprint(fixture.paths.inventory.parent)
    report = inspect_knowledge_asset_health(fixture.paths, fixture.query)
    findings = {item["code"] for item in report.to_dict()["diagnosis"]["findings"]}
    assert ("duplicate_content_proved" in findings) is proved
    if proved:
        assert "preferred_keeper_location_unresolved" in findings
        assert report.diagnostic_observations[0].evidence_refs[0].owner == "inventory"
    assert report.to_dict() == inspect_knowledge_asset_health(fixture.paths, fixture.query).to_dict()
    assert _state_fingerprint(fixture.paths.inventory.parent) == before


def test_zip_ott_identification_is_consumed_from_the_archive_owner(tmp_path: Path) -> None:
    fixture = _create_health_fixture(tmp_path)
    assert fixture.paths.archive is not None
    initialize_archive_state(fixture.paths.archive)
    with closing(sqlite3.connect(fixture.paths.archive)) as connection, connection:
        connection.execute(
            """INSERT INTO containers(container_key,path,size,mtime_ns,birthtime_ns,
            processing_signature,status,last_seen_run_id,updated_ns)
            VALUES(?,'/corpus/docs/asset.txt',800,123,-1,'archive-fixture','complete',1,10)""",
            (fixture.file_key,),
        )
        connection.execute(
            """INSERT INTO archive_logical_documents VALUES(
            ?,'','application/zip','application/vnd.oasis.opendocument.text-template',
            'ott','.ott',?,'identified','not_verified','not_verified')""",
            (fixture.file_key, json.dumps(["mimetype", "content.xml", "META-INF/manifest.xml"])),
        )
    _quiesce(fixture.paths.archive)
    before = _state_fingerprint(fixture.paths.archive.parent)
    report = inspect_knowledge_asset_health(fixture.paths, fixture.query)
    codes = {item["code"] for item in report.to_dict()["diagnosis"]["findings"]}
    assert "logical_format_ott" in codes
    assert "format_identification_not_disposal_evidence" in codes
    assert report.diagnostic_observations[0].evidence_refs[0].owner == "archive"
    assert report.to_dict() == inspect_knowledge_asset_health(fixture.paths, fixture.query).to_dict()
    assert _state_fingerprint(fixture.paths.archive.parent) == before


def test_keeper_preference_factor_survives_a_deterministic_tiebreak(tmp_path: Path) -> None:
    fixture = _create_health_fixture(tmp_path)
    _exact_duplicates(fixture.paths.inventory)
    with closing(sqlite3.connect(fixture.paths.inventory)) as connection, connection:
        raw = connection.execute("SELECT proof_json FROM planned_duplicate_groups").fetchone()[0]
        proof = json.loads(raw)
        proof["keeper_factors"] = ["preferred_location"]
        connection.execute("UPDATE planned_duplicate_groups SET proof_json=?", (json.dumps(proof),))
    _quiesce(fixture.paths.inventory)
    report = inspect_knowledge_asset_health(fixture.paths, fixture.query)
    codes = {item["code"] for item in report.to_dict()["diagnosis"]["findings"]}
    assert "keeper_preference_recorded" in codes
    assert "preferred_keeper_location_unresolved" not in codes
