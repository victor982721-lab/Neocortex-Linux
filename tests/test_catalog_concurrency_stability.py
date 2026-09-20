"""Source-scoped catalog publication fences and concurrent-owner contracts."""

from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
from pathlib import Path

import pytest

from neocortex.documents import document_catalog as catalog
from neocortex.documents.document_catalog import CatalogSourceDrift
from tests.test_catalog_adaptive_processing import _two_sources
from tests.test_catalog_binding_write_amplification import _source


@pytest.fixture
def serial_catalog_classifier(monkeypatch: pytest.MonkeyPatch):
    """Keep these small concurrency fixtures in one process and bounded."""

    original = catalog.classify_document

    def serial(*args: object, **kwargs: object):
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog, "classify_document", serial)


def _change_docx_source(source: Path, title: str = "changed fixture") -> None:
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET title=?", (title,))


def test_sibling_source_commit_does_not_stale_prepared_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serial_catalog_classifier: None,
) -> None:
    """A DOCX proof survives an unrelated IMAGE commit without retrying."""

    root, docx, image = _two_sources(tmp_path)
    target = tmp_path / "catalog.sqlite3"
    prepared = threading.Event()
    sibling_finished = threading.Event()
    original = catalog._prepare_catalog_publication
    observations: list[str] = []

    def hold_docx(connection, build, *args, **kwargs):
        result = original(connection, build, *args, **kwargs)
        observations.append(build.source_kind)
        if build.source_kind == "docx":
            prepared.set()
            assert sibling_finished.wait(8), "publication preparation unexpectedly blocked"
        return result

    monkeypatch.setattr(catalog, "_prepare_catalog_publication", hold_docx)
    with ThreadPoolExecutor(max_workers=2) as pool:
        primary = pool.submit(
            catalog.update_document_catalog_source,
            target,
            docx,
            "docx",
            source_root=root,
        )
        assert prepared.wait(8)
        sibling = catalog.update_document_catalog_source(
            target, image, "image", source_root=root,
        )
        sibling_finished.set()
        result = primary.result(timeout=15)

    assert result.classified == sibling.classified == 1
    assert observations.count("docx") == 1
    assert observations.count("image") == 1


def test_same_source_builders_use_generation_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serial_catalog_classifier: None,
) -> None:
    """Two builders with one base head cannot both publish."""

    root, source = _source(tmp_path, 1)
    target = tmp_path / "catalog.sqlite3"
    catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    _change_docx_source(source)

    @contextmanager
    def no_source_lock(*args, **kwargs):
        yield

    monkeypatch.setattr(catalog, "_catalog_source_update", no_source_lock)
    barrier = threading.Barrier(2)
    original = catalog._prepare_catalog_publication

    def hold_both(connection, build, *args, **kwargs):
        result = original(connection, build, *args, **kwargs)
        barrier.wait(timeout=8)
        return result

    monkeypatch.setattr(catalog, "_prepare_catalog_publication", hold_both)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                catalog.update_document_catalog_source,
                target,
                source,
                "docx",
                source_root=root,
            )
            for _ in range(2)
        ]
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=20))
            except BaseException as exc:
                outcomes.append(exc)

    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], catalog.CatalogPublicationConflict)


def test_corrections_change_fails_closed_after_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serial_catalog_classifier: None,
) -> None:
    root, source = _source(tmp_path, 1)
    target = tmp_path / "catalog.sqlite3"
    first = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    _change_docx_source(source)
    original = catalog._prepare_catalog_publication
    inserted = False

    def change_corrections(connection, build, *args, **kwargs):
        nonlocal inserted
        result = original(connection, build, *args, **kwargs)
        if not inserted:
            inserted = True
            with sqlite3.connect(target) as other:
                other.execute(
                    """INSERT INTO classification_corrections(
                    root,logical_identity,dimension,value_json,
                    observed_fingerprint,created_ns)
                    VALUES(?,?,?,?,?,?)""",
                    (str(root), "docx:fixture", "primary_kind", '"fixture"', "fixture", 1),
                )
        return result

    monkeypatch.setattr(catalog, "_prepare_catalog_publication", change_corrections)
    with pytest.raises((CatalogSourceDrift, catalog.CatalogPublicationConflict)):
        catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    with catalog.document_catalog_database(target, readonly=True) as connection:
        assert catalog.read_catalog_publication_manifest(connection, "docx").generation_id == first.generation_id


def test_source_database_change_fails_closed_after_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serial_catalog_classifier: None,
) -> None:
    root, source = _source(tmp_path, 1)
    target = tmp_path / "catalog.sqlite3"
    first = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    _change_docx_source(source)
    original = catalog._prepare_catalog_publication
    changed = False

    def change_source(connection, build, *args, **kwargs):
        nonlocal changed
        result = original(connection, build, *args, **kwargs)
        if not changed:
            changed = True
            _change_docx_source(source, "changed fixture after preparation")
        return result

    monkeypatch.setattr(catalog, "_prepare_catalog_publication", change_source)
    with pytest.raises(CatalogSourceDrift):
        catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    with catalog.document_catalog_database(target, readonly=True) as connection:
        assert catalog.read_catalog_publication_manifest(connection, "docx").generation_id == first.generation_id


def test_root_identity_change_fails_closed_after_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serial_catalog_classifier: None,
) -> None:
    root, source = _source(tmp_path, 1)
    target = tmp_path / "catalog.sqlite3"
    first = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    _change_docx_source(source)
    original = catalog._prepare_catalog_publication

    def change_root(connection, build, *args, **kwargs):
        result = original(connection, build, *args, **kwargs)
        replacement = root.with_name("replaced-corpus")
        root.rename(replacement)
        root.mkdir()
        return result

    monkeypatch.setattr(catalog, "_prepare_catalog_publication", change_root)
    with pytest.raises(CatalogSourceDrift):
        catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    with catalog.document_catalog_database(target, readonly=True) as connection:
        assert catalog.read_catalog_publication_manifest(connection, "docx").generation_id == first.generation_id


def test_staged_generation_change_fails_closed_and_keeps_previous_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serial_catalog_classifier: None,
) -> None:
    root, source = _source(tmp_path, 1)
    target = tmp_path / "catalog.sqlite3"
    first = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    _change_docx_source(source)
    original = catalog._prepare_catalog_publication

    def change_staged_rows(connection, build, *args, **kwargs):
        result = original(connection, build, *args, **kwargs)
        with sqlite3.connect(target) as other:
            other.execute(
                "UPDATE catalog_generation_documents SET primary_kind='changed' WHERE generation_id=?",
                (build.generation_id,),
            )
        return result

    monkeypatch.setattr(catalog, "_prepare_catalog_publication", change_staged_rows)
    with pytest.raises(catalog.CatalogPublicationConflict):
        catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    with catalog.document_catalog_database(target, readonly=True) as connection:
        assert catalog.read_catalog_publication_manifest(connection, "docx").generation_id == first.generation_id
