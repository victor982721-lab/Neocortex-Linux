"""Synthetic Linux regressions for stable organization destinations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.documents import document_organization_planning as planning
from neocortex.documents.document_catalog import document_catalog_database
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.documents.document_organization import (
    capture_organization_input_scope,
    plan_document_organization,
)
from neocortex.documents.document_organization_application import apply_document_organization
from neocortex.foundation.file_identity import FileIdentity
from neocortex.safety.corpus_access import CorpusAccessPolicy, CorpusMutationGuard
from tests.internal_paths_test_support import disjoint_internal_paths_policy
from tests.test_linux_organization_application import _seed_docx


def _normal_mutation_guard(root: Path) -> CorpusMutationGuard:
    return CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", root),
        disjoint_internal_paths_policy(root),
    )


@pytest.fixture
def organization_idempotency_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    incoming = corpus / "incoming"
    incoming.mkdir(parents=True)
    source = incoming / "Formato SERINTRA.docx"
    source.write_bytes(b"synthetic identity-qualified destination fixture")
    _seed_docx(state / "docx.sqlite3", source)

    from neocortex.documents.document_catalog import update_document_catalog

    update_document_catalog(state)
    catalog = state / "document_catalog.sqlite3"
    destination_root = corpus / "organized"
    scope = capture_organization_input_scope(catalog, corpus)
    summary = plan_document_organization(catalog, destination_root, source_scope=scope)
    assert (summary.planned, summary.executable) == (1, 1)
    with document_catalog_database(catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT destination_path FROM organization_plans WHERE status='planned'"
        ).fetchone()
    assert row is not None
    return (
        catalog,
        corpus,
        source,
        destination_root,
        Path(str(row["destination_path"])),
    )


def test_replan_keeps_current_identity_destination_after_foreign_collision(
    organization_idempotency_fixture: tuple[Path, Path, Path, Path, Path],
) -> None:
    catalog, corpus, source, destination_root, requested = organization_idempotency_fixture
    requested.parent.mkdir(parents=True)
    requested.write_bytes(b"unrelated pre-existing destination")

    first = apply_document_organization(
        catalog,
        destination_root,
        mutation_guard=_normal_mutation_guard(corpus.parent),
        max_actions=1,
    )
    assert first.applied == 1
    assert not source.exists()
    with document_catalog_database(catalog, readonly=True) as connection:
        document = connection.execute("SELECT path FROM documents").fetchone()
    assert document is not None
    current = Path(str(document["path"]))
    assert current != requested
    assert current.read_bytes() == b"synthetic identity-qualified destination fixture"
    assert requested.read_bytes() == b"unrelated pre-existing destination"
    with document_catalog_database(catalog, readonly=True) as connection:
        document_row = connection.execute("SELECT * FROM documents").fetchone()
        assert document_row is not None
        resolved_current, was_disambiguated = planning._resolve_plan_destination(
            connection,
            document_row,
            current,
        )
    assert (resolved_current, was_disambiguated) == (current, False)

    next_scope = capture_organization_input_scope(catalog, corpus)
    replay_plan = plan_document_organization(catalog, destination_root, source_scope=next_scope)
    assert (replay_plan.considered, replay_plan.planned, replay_plan.already_organized) == (
        1,
        0,
        1,
    )

    replay = apply_document_organization(
        catalog,
        destination_root,
        mutation_guard=_normal_mutation_guard(corpus.parent),
        max_actions=1,
    )
    assert (replay.selected, replay.applied, replay.remaining) == (0, 0, 0)
    assert current.exists()
    assert current.read_bytes() == b"synthetic identity-qualified destination fixture"


def test_foreign_plan_reserves_identity_slot_without_becoming_owner(tmp_path: Path) -> None:
    catalog = tmp_path / "state" / "document_catalog.sqlite3"
    catalog.parent.mkdir()
    initialize_document_catalog(catalog)
    from tests.test_document_organization_scope import _seed, _file

    source = _file(tmp_path / "corpus", "standard.pdf")
    _seed(catalog, source)
    requested = tmp_path / "organized" / "Normativa" / "CFE" / source.name
    scope = capture_organization_input_scope(catalog, source.parent)
    with document_catalog_database(catalog) as connection:
        row = connection.execute("SELECT * FROM documents").fetchone()
        assert row is not None
        first_candidate = planning._identity_disambiguated_destination(requested, row, 1)
        first_candidate.parent.mkdir(parents=True)
        source.rename(first_candidate)
        connection.execute(
            "UPDATE documents SET path=? WHERE source_kind=? AND file_key=?",
            (str(first_candidate), row["source_kind"], row["file_key"]),
        )
        row = connection.execute("SELECT * FROM documents").fetchone()
        assert row is not None
        connection.execute(
            """INSERT INTO organization_plans(
                catalog_run_id,source_kind,file_key,source_path,destination_path,
                organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
                classifier_signature,primary_kind,confidence,status,reason,evidence_json,
                planned_ns,source_scope_json,source_scope_id,resource_binding_json,
                representation_kind,operation_kind,eligibility_status,executable,blockers_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                99,
                "pdf",
                "foreign-owner",
                str(tmp_path / "foreign.pdf"),
                str(first_candidate),
                str(tmp_path / "organized"),
                "foreign-volume",
                "foreign-file",
                1,
                1,
                -1,
                "fixture-classifier",
                "normativa",
                0.96,
                "planned",
                "foreign_plan_fixture",
                "{}",
                1,
                scope.serialized,
                scope.scope_id,
                row["resource_binding_json"],
                "physical_file",
                "move_physical",
                "eligible",
                1,
                "[]",
            ),
        )
        connection.commit()
        resolved, disambiguated = planning._resolve_plan_destination(
            connection,
            row,
            requested,
        )

    assert disambiguated is True
    assert resolved is not None
    assert resolved != first_candidate
    assert resolved == planning._identity_disambiguated_destination(requested, row, 2)


def test_two_applied_moves_replan_to_zero_effects(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    left = corpus / "left"
    right = corpus / "right"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    sources = (left / "Formato SERINTRA.docx", right / "Formato SERINTRA.docx")
    for source in sources:
        source.write_bytes(b"synthetic multiple identity fixture")
        _seed_docx(state / "docx.sqlite3", source)

    catalog = state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    from tests.test_document_organization_scope import _seed

    # Populate the disposable owner and catalog directly.  This keeps the
    # regression independent of the catalog classifier's process pool while
    # preserving the same physical identities used by the DOCX owner.
    with sqlite3.connect(state / "docx.sqlite3") as owner:
        owner_rows = owner.execute("SELECT file_key,path FROM documents").fetchall()
        for old_key, path in owner_rows:
            observed = Path(str(path)).stat()
            packed_key = FileIdentity(observed.st_dev, observed.st_ino).packed_key
            owner.execute(
                "UPDATE documents SET file_key=? WHERE file_key=?",
                (packed_key, old_key),
            )
    for source in sources:
        _seed(catalog, source, kind="docx")

    destination_root = corpus / "organized"
    first_scope = capture_organization_input_scope(catalog, corpus)
    first_plan = plan_document_organization(catalog, destination_root, source_scope=first_scope)
    assert (first_plan.planned, first_plan.executable) == (2, 2)

    first_apply = apply_document_organization(
        catalog,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=2,
    )
    assert (first_apply.applied, first_apply.cache_pending) == (2, 0)
    assert all(not source.exists() for source in sources)

    second_scope = capture_organization_input_scope(catalog, corpus)
    second_plan = plan_document_organization(catalog, destination_root, source_scope=second_scope)
    assert (second_plan.considered, second_plan.planned, second_plan.already_organized) == (
        2,
        0,
        2,
    )
    replay = apply_document_organization(
        catalog,
        destination_root,
        mutation_guard=_normal_mutation_guard(tmp_path),
        max_actions=2,
    )
    assert (replay.selected, replay.applied, replay.remaining) == (0, 0, 0)


def test_same_name_foreign_file_is_not_accepted_as_current_owner(tmp_path: Path) -> None:
    catalog = tmp_path / "state" / "document_catalog.sqlite3"
    catalog.parent.mkdir()
    initialize_document_catalog(catalog)
    from tests.test_document_organization_scope import _seed, _file

    source = _file(tmp_path / "corpus", "standard.pdf")
    _seed(catalog, source)
    scope = capture_organization_input_scope(catalog, source.parent)
    requested = tmp_path / "organized" / "Normativa" / "CFE" / source.name
    with document_catalog_database(catalog, readonly=True) as connection:
        row = connection.execute("SELECT * FROM documents").fetchone()
    assert row is not None
    current_candidate = planning._identity_disambiguated_destination(requested, row, 1)
    current_candidate.parent.mkdir(parents=True)
    current_candidate.write_bytes(b"different physical owner")

    with document_catalog_database(catalog, readonly=True) as connection:
        resolved, disambiguated = planning._resolve_plan_destination(
            connection,
            row,
            requested,
        )
    assert disambiguated is True
    assert resolved != current_candidate
    assert resolved == planning._identity_disambiguated_destination(requested, row, 2)
    assert scope.scope_id


def test_preexisting_hardlink_is_not_retained_as_current_owner(tmp_path: Path) -> None:
    catalog = tmp_path / "state" / "document_catalog.sqlite3"
    catalog.parent.mkdir()
    initialize_document_catalog(catalog)
    from tests.test_document_organization_scope import _seed, _file

    source = _file(tmp_path / "corpus", "standard.pdf")
    _seed(catalog, source)
    requested = tmp_path / "organized" / "Normativa" / "CFE" / source.name
    with document_catalog_database(catalog) as connection:
        row = connection.execute("SELECT * FROM documents").fetchone()
        assert row is not None
        current_candidate = planning._identity_disambiguated_destination(requested, row, 1)
    current_candidate.parent.mkdir(parents=True)
    current_candidate.hardlink_to(source)
    assert current_candidate.stat().st_nlink == 2

    with document_catalog_database(catalog) as connection:
        connection.execute(
            "UPDATE documents SET path=? WHERE source_kind=? AND file_key=?",
            (str(current_candidate), row["source_kind"], row["file_key"]),
        )
        connection.commit()

    with document_catalog_database(catalog, readonly=True) as connection:
        current_row = connection.execute("SELECT * FROM documents").fetchone()
        assert current_row is not None
        resolved, disambiguated = planning._resolve_plan_destination(
            connection,
            current_row,
            requested,
        )
    assert disambiguated is True
    assert resolved == planning._identity_disambiguated_destination(requested, current_row, 2)
