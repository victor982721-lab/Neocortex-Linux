"""Contained organization fixtures: source membership never follows a destination."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.documents import document_organization_application as application
from neocortex.documents import document_organization_planning as planning
from neocortex.documents.document_catalog import (
    document_catalog_database,
    initialize_document_catalog,
)
from neocortex.documents.document_organization import (
    OrganizationInputScope,
    capture_organization_input_scope,
    list_organization_plans,
)
from neocortex.documents.document_resource_binding import build_resource_binding
from neocortex.foundation.file_identity import FileIdentity
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.safety.corpus_access import CorpusAccessPolicy, CorpusMutationGuard
from tests.internal_paths_test_support import disjoint_internal_paths_policy


def _catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / "state" / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    return catalog


def _seed(
    catalog: Path,
    source: Path,
    *,
    kind: str = "pdf",
    member: str | None = None,
    status: str = "classified",
    binding_present: bool = True,
    representation_kind: str | None = None,
    representation_metadata: dict[str, object] | None = None,
) -> str:
    observed = source.stat()
    identity = FileIdentity(observed.st_dev, observed.st_ino)
    birth = stat_birthtime_ns(observed)
    # Generic ZIP members are no longer durable catalog resources.  Organization
    # receives only physically published files, so every fixture is bound to its
    # own observed path and identity.
    file_key = identity.packed_key
    locator = str(source)
    binding = build_resource_binding(
        source_kind=kind,
        file_key=file_key,
        path=locator,
        identity=identity,
        birthtime_ns=birth,
            size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            representation_kind=representation_kind,
        representation_metadata=representation_metadata,
    )
    row = {
        "source_kind": kind,
        "file_key": file_key,
        "path": locator,
        "volume_id": str(identity.volume_id),
        "file_id": str(identity.file_id),
        "size": observed.st_size,
        "mtime_ns": observed.st_mtime_ns,
        "birthtime_ns": birth,
        "source_status": "done",
        "processing_signature": "fixture-v1",
        "text_fingerprint": "text-v1",
        "classifier_signature": "classifier-v1",
        "primary_kind": "normativa",
        "primary_authority": "CFE",
        "confidence": 0.96,
        "uncertainty": "baja",
        "standard_references_json": "[]",
        "organizations_json": "[]",
        "topics_json": "[]",
        "classification_json": "{}",
        "catalog_status": status,
        "last_seen_catalog_run_id": 1,
        "updated_ns": 1,
        "resource_binding_json": json.dumps(binding, sort_keys=True) if binding_present else None,
    }
    with document_catalog_database(catalog) as connection:
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        connection.execute(f"INSERT INTO documents({columns}) VALUES({marks})", tuple(row.values()))
        connection.commit()
    return file_key


def _file(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(b"contained organization fixture")
    return path


def _plan(catalog: Path, root: Path, destination: Path):
    scope = capture_organization_input_scope(catalog, root)
    return planning.plan_document_organization(catalog, destination, source_scope=scope)


def test_scope_is_required_before_creating_catalog_or_destination(tmp_path: Path) -> None:
    catalog = tmp_path / "missing.sqlite3"
    with pytest.raises(TypeError, match="source_scope"):
        planning.plan_document_organization(catalog, tmp_path / "destination")  # type: ignore[call-arg]
    assert not catalog.exists()
    assert not (tmp_path / "destination").exists()


def test_source_scope_excludes_other_roots(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    selected = _file(tmp_path / "corpus", "standard.pdf")
    fixture = _file(tmp_path / "fixture", "sample.pdf")
    _seed(catalog, selected)
    _seed(catalog, fixture)
    destination = tmp_path / "different-destination"
    summary = _plan(catalog, selected.parent, destination)
    assert (summary.considered, summary.planned, summary.excluded_out_of_scope) == (1, 1, 1)
    assert summary.source_root == str(selected.parent)
    assert summary.executable == 0
    views = list_organization_plans(catalog, limit=20)
    assert [view.source_path for view in views] == [str(selected)]
    assert views[0].eligibility_status == "eligible"
    assert views[0].executable is False
    assert set(views[0].blockers) == {"backend_unavailable", "authorization_required"}
    assert not destination.exists()


def test_scope_selection_not_the_destination_selects_a_fixture(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    corpus = _file(tmp_path / "corpus", "original.pdf")
    fixture = _file(tmp_path / "fixture", "sample.pdf")
    _seed(catalog, corpus)
    _seed(catalog, fixture)
    summary = _plan(catalog, fixture.parent, corpus.parent / "organized")
    assert summary.considered == 1
    assert list_organization_plans(catalog, limit=2)[0].source_path == str(fixture)


def test_decompressed_ooxml_directory_keeps_all_members_together(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    root = tmp_path / "unzipped"
    names = ("[Content_Types].xml", "word/document.xml", "word/header1.xml", "_rels/.rels")
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<package-part>technical evidence</package-part>")
        _seed(catalog, path, kind="text")
    summary = _plan(catalog, root, tmp_path / "destination")
    assert (summary.considered, summary.planned, summary.excluded_components) == (0, 0, 4)
    assert list_organization_plans(catalog, limit=10) == ()


def test_partial_decompressed_ooxml_directory_is_review_only(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    root = tmp_path / "partial"
    path = root / "word" / "document.xml"
    path.parent.mkdir(parents=True)
    path.write_text("<document>partial package</document>")
    _seed(catalog, path, kind="text")
    summary = _plan(catalog, root, tmp_path / "destination")
    assert (summary.considered, summary.planned, summary.excluded_components) == (0, 0, 1)
    assert list_organization_plans(catalog, limit=10) == ()


def test_ooxml_content_types_only_is_a_partial_package_hypothesis(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    root = tmp_path / "partial-content-types"
    path = root / "[Content_Types].xml"
    path.parent.mkdir(parents=True)
    path.write_text("<Types/>")
    _seed(catalog, path, kind="text")
    summary = _plan(catalog, root, tmp_path / "destination")
    assert (summary.considered, summary.planned, summary.excluded_components) == (0, 0, 1)
    assert list_organization_plans(catalog, limit=10) == ()


def test_partial_high_confidence_keeps_suggestion_without_eligibility(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    source = _file(tmp_path / "corpus", "partial.pdf")
    _seed(catalog, source, status="review")
    summary = _plan(catalog, source.parent, tmp_path / "destination")
    assert (summary.planned, summary.review_required) == (0, 1)
    view = list_organization_plans(catalog, limit=1)[0]
    assert view.destination_path is not None
    assert view.eligibility_status == "blocked"
    assert "source_classification_requires_review" in view.blockers


@pytest.mark.parametrize(
    ("taxonomy_status", "reason"),
    [
        ("outside_taxonomy", "outside_organization_taxonomy"),
        ("insufficient_identification", "insufficient_document_identification"),
    ],
)
def test_taxonomy_gap_is_not_an_objective_file_anomaly(
    tmp_path: Path, taxonomy_status: str, reason: str
) -> None:
    catalog = _catalog(tmp_path)
    source = _file(tmp_path / "corpus", "record.txt")
    _seed(catalog, source, kind="text")
    with document_catalog_database(catalog) as connection:
        connection.execute(
            "UPDATE documents SET classification_json=?",
            (json.dumps({"taxonomy_status": taxonomy_status}),),
        )
        connection.commit()
    summary = _plan(catalog, source.parent, tmp_path / "destination")
    assert (summary.planned, summary.review_required) == (0, 1)
    view = list_organization_plans(catalog, limit=1)[0]
    assert view.reason == reason
    assert view.taxonomy_status == taxonomy_status
    assert view.destination_path is None
    assert view.classification_status == "classified"


@pytest.mark.parametrize("case", ["missing_binding", "changed_revision", "symlink"])
def test_unproved_scope_does_not_create_proposals(tmp_path: Path, case: str) -> None:
    catalog = _catalog(tmp_path)
    source = _file(tmp_path / "corpus", "source.pdf")
    if case == "symlink":
        outside = _file(tmp_path / "outside", "source.pdf")
        source.unlink()
        source.symlink_to(outside)
    _seed(catalog, source, binding_present=case != "missing_binding")
    if case == "changed_revision":
        source.write_bytes(b"different revision")
    summary = _plan(catalog, source.parent, tmp_path / "destination")
    assert (summary.considered, summary.unresolved_scope) == (0, 1)
    assert list_organization_plans(catalog, limit=1) == ()


def test_scope_roundtrip_and_changed_root_identity(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    root = tmp_path / "corpus"
    root.mkdir()
    scope = capture_organization_input_scope(catalog, root)
    assert OrganizationInputScope.from_json(scope.serialized) == scope
    assert OrganizationInputScope.from_json(scope.serialized).scope_id == scope.scope_id
    root.rename(tmp_path / "previous-root")
    root.mkdir()
    with pytest.raises(ValueError, match="root_identity_changed"):
        planning.plan_document_organization(catalog, tmp_path / "destination", source_scope=scope)


def test_scope_rejects_changed_publication_heads(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    root = tmp_path / "corpus"
    root.mkdir()
    scope = capture_organization_input_scope(catalog, root)
    with document_catalog_database(catalog) as connection:
        connection.execute(
            "INSERT INTO catalog_generations(generation_id,source_kind,status,started_ns) VALUES(1,'pdf','published',1)"
        )
        connection.execute(
            "INSERT INTO catalog_publications(source_kind,generation_id,published_ns) VALUES('pdf',1,1)"
        )
        connection.commit()
    with pytest.raises(ValueError, match="catalog_heads_changed"):
        planning.plan_document_organization(catalog, tmp_path / "destination", source_scope=scope)


def test_replay_keeps_one_plan_even_with_a_destination_collision(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    source = _file(tmp_path / "corpus", "standard.pdf")
    _seed(catalog, source)
    destination = tmp_path / "destination"
    occupied = _file(destination / "Normativa" / "CFE", source.name)
    first = _plan(catalog, source.parent, destination)
    view = list_organization_plans(catalog, limit=2)[0]
    assert view.destination_path != str(occupied)
    second = _plan(catalog, source.parent, destination)
    assert (first.considered, second.considered) == (1, 1)
    assert list_organization_plans(catalog, limit=2) == (view,)


def test_failed_replan_keeps_previous_complete_plans(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path)
    first = _file(tmp_path / "corpus", "a.pdf")
    second = _file(tmp_path / "corpus", "b.pdf")
    _seed(catalog, first)
    _seed(catalog, second)
    _plan(catalog, first.parent, tmp_path / "destination")
    previous = list_organization_plans(catalog, limit=20)
    with document_catalog_database(catalog) as connection:
        connection.execute(
            "UPDATE documents SET primary_authority='IEC' WHERE path=?", (str(first),)
        )
        connection.commit()
    original = planning._insert_plan
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture_plan_interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(planning, "_insert_plan", fail_second)
    with pytest.raises(RuntimeError, match="fixture_plan_interrupted"):
        _plan(catalog, first.parent, tmp_path / "destination")
    assert list_organization_plans(catalog, limit=20) == previous


@pytest.mark.parametrize("legacy", [False, True])
def test_apply_refuses_advisory_and_legacy_before_creating_destinations(
    tmp_path: Path, monkeypatch, legacy: bool
) -> None:
    catalog = _catalog(tmp_path)
    source = _file(tmp_path / "corpus", "standard.pdf")
    _seed(catalog, source)
    destination = tmp_path / "destination"
    _plan(catalog, source.parent, destination)
    if legacy:
        with document_catalog_database(catalog) as connection:
            connection.execute(
                "UPDATE organization_plans SET source_scope_json=NULL,source_scope_id=NULL"
            )
            connection.commit()
        assert list_organization_plans(catalog, limit=1)[0].blockers == ("legacy_unscoped",)
    monkeypatch.setattr(
        application,
        "_prepare_apply_root",
        lambda *args, **kwargs: pytest.fail("must not create destination"),
    )
    guard = CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", source.parent),
        disjoint_internal_paths_policy(source.parent),
    )
    result = application.apply_document_organization(catalog, destination, mutation_guard=guard)
    assert (result.applied, result.blocked) == (0, 1)
    assert source.exists()
    assert not destination.exists()


def test_executable_database_bit_is_not_an_authorization_grant(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    source = _file(tmp_path / "corpus", "standard.pdf")
    _seed(catalog, source)
    _plan(catalog, source.parent, tmp_path / "destination")
    with document_catalog_database(catalog) as connection:
        connection.execute("UPDATE organization_plans SET executable=1,blockers_json='[]'")
        connection.commit()
        rows = connection.execute("SELECT * FROM organization_plans").fetchall()
        denials = application._organization_execution_denials(connection, rows)
    assert set(denials.values()) == {"organization_authorized_backend_unavailable"}
    view = list_organization_plans(catalog, limit=1)[0]
    assert not view.executable
    assert "authorization_required" in view.blockers
