"""Only published, current, physically bound Code relations influence keeper rank."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import xxhash

from neocortex.code.code_state import CodeState
from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.deduplication.planning.keeper_references import (
    KeeperReferenceChanged,
    resolve_keeper_references,
)


def publish_code_reference_fixture(
    database: Path,
    paths: tuple[Path, ...],
    *,
    edges: tuple[tuple[str, int, int | None, bool], ...] = (("dependency", 1, 2, True),),
    published: bool = True,
) -> None:
    """Minimal real Code publisher for API fixtures; IDs follow 1-based path order."""
    database.parent.mkdir(parents=True, exist_ok=True)
    with CodeState(database) as owner:
        run = owner.begin_run(1, 1, "keeper-ref-fixture-v1")
        with owner.connection as db:
            for version_id, path in enumerate(paths, 1):
                snapshot = path.stat()
                db.execute(
                    "INSERT INTO files(file_id,volume_id,physical_file_id,current_path,current_version_id,status,first_seen_run_id,last_seen_run_id) VALUES(?,?,?,?,NULL,'current',?,?)",
                    (
                        version_id,
                        format(snapshot.st_dev, "x"),
                        format(snapshot.st_ino, "x"),
                        str(path),
                        run,
                        run,
                    ),
                )
                db.execute(
                    "INSERT INTO file_versions(version_id,file_id,path_observed,size,mtime_ns,birthtime_ns,raw_xxh3_128,artifact_kind,generated,vendored,classification_confidence,classification_evidence_json,analysis_status,processing_signature,analyzer_id,analyzer_version,parser_kind,provenance_json,first_observed_run_id,last_observed_run_id,valid_from_ns) VALUES(?,?,?,?,?,-1,?,'source',0,0,1.0,'[]','complete','keeper-ref-fixture-v1','fixture','1','fixture','{}',?,?,1)",
                    (
                        version_id,
                        version_id,
                        str(path),
                        snapshot.st_size,
                        snapshot.st_mtime_ns,
                        xxhash.xxh3_128_hexdigest(path.read_bytes()),
                        run,
                        run,
                    ),
                )
                db.execute(
                    "UPDATE files SET current_version_id=? WHERE file_id=?",
                    (version_id, version_id),
                )
            for relation_id, (family, origin, target, confirmed) in enumerate(edges, 1):
                if family == "dependency":
                    db.execute(
                        "INSERT INTO dependencies(dependency_id,version_id,resolved_version_id,name,kind,confirmed,confidence,evidence) VALUES(?,?,?,'fixture_module','import',?,1.0,'fixture_reference')",
                        (relation_id, origin, target, int(confirmed)),
                    )
                else:
                    symbol_id = None
                    if target is not None:
                        symbol_id = relation_id
                        db.execute(
                            "INSERT INTO symbols(symbol_id,version_id,kind,name,qualified_name,confirmed,start_line,start_column,end_line,end_column,start_byte,end_byte,metadata_json) VALUES(?,?,'function','value','module.value',1,1,0,1,1,0,1,'{}')",
                            (symbol_id, target),
                        )
                    db.execute(
                        "INSERT INTO code_references(reference_id,version_id,target_symbol_id,target_version_id,kind,name,confirmed,confidence,evidence,start_line,start_column,end_line,end_column,start_byte,end_byte) VALUES(?,?,?,?,'call','value',?,1.0,'fixture_reference',1,0,1,1,0,1)",
                        (relation_id, origin, symbol_id, target, int(confirmed)),
                    )
        owner.complete_run(
            run,
            {"candidates": len(paths), "processed": len(paths), "cache_hits": 0, "errors": 0},
            partial=False,
            graph_current=published,
        )


def _fixture(tmp_path: Path, **kwargs):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    caller, preferred, clean = corpus / "caller.py", corpus / "module (1).py", corpus / "module.py"
    caller.write_text("value = 'fixture caller with a different size'\n")
    preferred.write_text("def value(): return 1\n")
    clean.write_bytes(preferred.read_bytes())
    code = tmp_path / "state" / "code.sqlite3"
    publish_code_reference_fixture(code, (caller, preferred, clean), **kwargs)
    return corpus, code, preferred, clean


def _owner_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.iterdir()
        if path.is_file()
    }


@pytest.mark.parametrize("family", ["dependency", "reference"])
def test_published_reference_selects_keeper_with_provenance_and_replays(
    tmp_path: Path, family: str
) -> None:
    corpus, code, preferred, _clean = _fixture(tmp_path, edges=((family, 1, 2, True),))
    before = _owner_hashes(code.parent)
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(corpus)
        result = resolve_keeper_references(index, scan.scan_id, code)
        assert (result.status, result.reason, result.evidence_count) == ("available", None, 1)
        identity = (preferred.stat().st_dev, preferred.stat().st_ino)
        assert result.policy.verified_reference_identities == (identity,)
        ids = result.policy.verified_reference_evidence[0][1]
        assert any("generation:analysis:" in value and ":revision:" in value for value in ids)
        assert any(f"code:{family}:1:source_version:1:target_version:2" == value for value in ids)
        first = DedupPlanner(
            index, keeper_policy=result.policy, keeper_validation=result.verify
        ).plan(scan.scan_id, exact_compare=True, preview_limit=5)
        second = DedupPlanner(
            index, keeper_policy=result.policy, keeper_validation=result.verify
        ).plan(scan.scan_id, exact_compare=True, preview_limit=5)
        assert first.groups[0].keep.path == second.groups[0].keep.path == str(preferred)
        assert first.groups[0].proof is not None
        assert first.groups[0].proof.keeper_reason == "verified_reference"
        assert any(
            value.startswith("verified_reference_evidence:code:")
            for value in first.groups[0].proof.keeper_factors
        )
    assert _owner_hashes(code.parent) == before


@pytest.mark.parametrize(
    "edges",
    [
        (),
        (("dependency", 1, 2, False),),
        (("dependency", 1, None, True),),
        (("dependency", 1, 1, True),),
    ],
)
def test_unconfirmed_unresolved_self_or_absent_relations_are_not_preferences(
    tmp_path: Path, edges
) -> None:
    corpus, code, _preferred, clean = _fixture(tmp_path, edges=edges)
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(corpus)
        result = resolve_keeper_references(index, scan.scan_id, code)
        assert result.status == "available" and result.evidence_count == 0
        assert result.reason == "no_eligible_reference_in_selected_inventory"
        assert result.policy.verified_reference_identities == ()
        plan = DedupPlanner(
            index, keeper_policy=result.policy, keeper_validation=result.verify
        ).plan(scan.scan_id, exact_compare=True, preview_limit=5)
        assert plan.groups[0].keep.path == str(clean)


def test_unpublished_owner_is_optional_not_reference_authority(tmp_path: Path) -> None:
    corpus, code, _preferred, _clean = _fixture(tmp_path, published=False)
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(corpus)
        result = resolve_keeper_references(index, scan.scan_id, code)
        assert result.status == "unavailable" and result.reason == "code_published_graph_absent"
        assert result.policy.verified_reference_identities == ()
        assert (
            DedupPlanner(index, keeper_policy=result.policy)
            .plan(scan.scan_id, exact_compare=True, preview_limit=5)
            .group_count
            == 1
        )


@pytest.mark.parametrize("mutation", ["owner_relation", "current_version", "physical_file"])
def test_stale_reference_returns_no_preferences_and_explicit_reason(
    tmp_path: Path, mutation: str
) -> None:
    corpus, code, preferred, _clean = _fixture(tmp_path)
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(corpus)
        if mutation == "physical_file":
            preferred.write_text("changed after Code publication")
        else:
            with CodeState(code) as owner, owner.connection as db:
                if mutation == "owner_relation":
                    db.execute("UPDATE dependencies SET confirmed=0")
                else:
                    db.execute(
                        "UPDATE files SET current_version_id=NULL,status='stale' WHERE file_id=2"
                    )
        result = resolve_keeper_references(index, scan.scan_id, code)
        assert result.status == "stale" and result.reason is not None
        assert result.policy.verified_reference_identities == () and result.evidence_count == 0


def test_reference_budget_is_explicit_and_empty_not_a_partial_verified_policy(
    tmp_path: Path,
) -> None:
    corpus, code, _preferred, _clean = _fixture(
        tmp_path, edges=(("dependency", 1, 2, True), ("reference", 1, 2, True))
    )
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(corpus)
        result = resolve_keeper_references(index, scan.scan_id, code, max_relations=1)
        assert result.status == "truncated" and result.policy.verified_reference_identities == ()
        assert result.reason == "published_reference_relation_budget_exceeded"


def test_revalidation_prevents_publishing_plan_with_changed_reference_evidence(
    tmp_path: Path,
) -> None:
    corpus, code, _preferred, _clean = _fixture(tmp_path)
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(corpus)
        result = resolve_keeper_references(index, scan.scan_id, code)
        assert result.status == "available" and result.evidence_count == 1
        with CodeState(code) as owner:
            run = owner.begin_run(2, 2, "keeper-ref-fixture-v1")
            owner.complete_run(
                run,
                {"candidates": 3, "processed": 0, "cache_hits": 3, "errors": 0},
                partial=False,
                graph_current=True,
            )
        with pytest.raises(KeeperReferenceChanged):
            DedupPlanner(index, keeper_policy=result.policy, keeper_validation=result.verify).plan(
                scan.scan_id, exact_compare=True
            )
        assert (
            index._connection.execute(
                "SELECT COUNT(*) FROM duplicate_plan_summaries WHERE scan_id=?", (scan.scan_id,)
            ).fetchone()[0]
            == 0
        )


def test_relation_outside_selected_inventory_does_not_affect_keepers(tmp_path: Path) -> None:
    _corpus, code, _preferred, _clean = _fixture(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "one.txt").write_text("unrelated")
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(unrelated)
        result = resolve_keeper_references(index, scan.scan_id, code)
        assert result.status == "available" and result.evidence_count == 0
        assert result.policy.verified_reference_identities == ()


@pytest.mark.parametrize("condition", ["missing", "incompatible"])
def test_missing_or_incompatible_owner_does_not_block_other_keeper_evidence(
    tmp_path: Path, condition: str
) -> None:
    corpus, code, _preferred, _clean = _fixture(tmp_path)
    if condition == "incompatible":
        with CodeState(code) as owner, owner.connection as db:
            db.execute("DROP INDEX dependencies_name_idx")
    else:
        code = tmp_path / "missing.sqlite3"
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(corpus)
        result = resolve_keeper_references(index, scan.scan_id, code)
        assert result.status == "unavailable" and result.evidence_count == 0
        assert result.policy.verified_reference_identities == ()
        assert (
            DedupPlanner(index, keeper_policy=result.policy, keeper_validation=result.verify)
            .plan(scan.scan_id, exact_compare=True)
            .group_count
            == 1
        )


def test_graph_materialization_is_bounded_before_loading_publication(
    tmp_path: Path, monkeypatch
) -> None:
    import neocortex.deduplication.planning.keeper_references as references

    corpus, code, _preferred, _clean = _fixture(tmp_path)
    monkeypatch.setattr(references, "_GRAPH_PAYLOAD_BYTES", 1)

    def forbidden_read(_store):
        raise AssertionError("the graph must not materialize beyond the preflight bound")

    monkeypatch.setattr(
        references.CodeGraphGenerationStore, "read_published_generation", forbidden_read
    )
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(corpus)
        result = resolve_keeper_references(index, scan.scan_id, code)
        assert result.status == "truncated" and result.evidence_count == 0
        assert result.reason == "published_graph_materialization_budget_exceeded"
