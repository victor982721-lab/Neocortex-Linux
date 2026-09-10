# region [00] Contexto del módulo
# Módulo: tests/test_document_catalog_generation_publication.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
import zlib
import json
import os
from pathlib import Path

import pytest

import neocortex.documents.document_catalog as catalog_module
from neocortex.documents.document_catalog import (
    CatalogSourceDrift,
    document_catalog_database,
    list_catalog_documents,
    read_catalog_publication_manifest,
    validate_catalog_publication_scope,
    update_document_catalog_source,
)
from neocortex.documents.document_catalog_schema import catalog_generation_digest
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.documents.document_organization_models import _begin_organization_run
from neocortex.deduplication import snapshot_path
# endregion [01]

# region [02] Implementación


def _upsert_docx_source(
    database: Path,
    source: Path,
    *,
    title: str,
    text: str,
    signature: str,
) -> None:
    initialize_docx_state(database)
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
            updated_ns,title,author)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(file_key) DO UPDATE SET
            path=excluded.path,size=excluded.size,mtime_ns=excluded.mtime_ns,
            birthtime_ns=excluded.birthtime_ns,
            processing_signature=excluded.processing_signature,
            text_zlib=excluded.text_zlib,text_chars=excluded.text_chars,
            text_xxh3_128=excluded.text_xxh3_128,title=excluded.title""",
            (
                file_key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                signature,
                "complete",
                "valid",
                zlib.compress(text.encode("utf-8")),
                len(text),
                f"text-{signature}",
                1,
                1,
                title,
                "",
            ),
        )


def _published_kinds(catalog: Path) -> dict[str, str]:
    return {
        document.path: document.primary_kind
        for document in list_catalog_documents(catalog, limit=100)
    }


def test_failed_catalog_build_keeps_previous_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database = tmp_path / "docx.sqlite3"
    first = tmp_path / "a-ieee.docx"
    second = tmp_path / "b-second.docx"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _upsert_docx_source(
        source_database,
        first,
        title="IEEE C37.20.2",
        text="IEEE switchgear standard",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(
        catalog,
        source_database,
        "docx",
        verify_source_paths=False,
    )
    before = _published_kinds(catalog)
    with document_catalog_database(catalog) as connection:
        connection.execute(
            """INSERT INTO organization_plans(
            source_kind,file_key,source_path,destination_path,organization_root,
            volume_id,file_id,size,mtime_ns,birthtime_ns,classifier_signature,
            primary_kind,confidence,status,reason,evidence_json,planned_ns)
            SELECT source_kind,file_key,path,NULL,?,volume_id,file_id,size,mtime_ns,
            birthtime_ns,classifier_signature,primary_kind,confidence,'planned',
            'fixture','{}',1 FROM documents WHERE active=1""",
            (str(tmp_path / "organized"),),
        )
        history_before = int(
            connection.execute("SELECT COUNT(*) FROM classification_history").fetchone()[0]
        )
        connection.commit()

    _upsert_docx_source(
        source_database,
        first,
        title="Factura proveedor",
        text="Factura compra",
        signature="v2",
    )
    _upsert_docx_source(
        source_database,
        second,
        title="Segundo documento",
        text="segundo",
        signature="v1",
    )
    original = catalog_module.classify_document
    calls = 0

    def fail_second(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected build failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog_module, "classify_document", fail_second)

    with pytest.raises(RuntimeError, match="injected build failure"):
        update_document_catalog_source(
            catalog,
            source_database,
            "docx",
            verify_source_paths=False,
        )

    assert _published_kinds(catalog) == before
    with document_catalog_database(catalog, readonly=True) as connection:
        status = connection.execute(
            "SELECT status FROM catalog_runs ORDER BY catalog_run_id DESC LIMIT 1"
        ).fetchone()[0]
        plan_status = connection.execute("SELECT status FROM organization_plans").fetchone()[0]
        history_after = int(
            connection.execute("SELECT COUNT(*) FROM classification_history").fetchone()[0]
        )
    assert status == "failed"
    assert plan_status == "planned"
    assert history_after == history_before


def test_reader_during_committed_build_keeps_previous_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database = tmp_path / "docx.sqlite3"
    first = tmp_path / "a-ieee.docx"
    second = tmp_path / "b-second.docx"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _upsert_docx_source(
        source_database,
        first,
        title="IEEE C37.20.2",
        text="IEEE switchgear standard",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(
        catalog,
        source_database,
        "docx",
        verify_source_paths=False,
    )
    before = _published_kinds(catalog)

    _upsert_docx_source(
        source_database,
        first,
        title="Factura proveedor",
        text="Factura compra",
        signature="v2",
    )
    _upsert_docx_source(
        source_database,
        second,
        title="Segundo documento",
        text="segundo",
        signature="v1",
    )
    original = catalog_module.classify_document
    observed: dict[str, str] | None = None
    calls = 0

    def observe_second(*args: object, **kwargs: object):
        nonlocal calls, observed
        calls += 1
        if calls == 2:
            observed = _published_kinds(catalog)
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog_module, "classify_document", observe_second)
    monkeypatch.setattr(catalog_module, "CATALOG_WRITE_BATCH", 1)

    update_document_catalog_source(
        catalog,
        source_database,
        "docx",
        verify_source_paths=False,
    )

    assert observed == before
    assert _published_kinds(catalog) != before


def test_cancelled_catalog_build_keeps_previous_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database = tmp_path / "docx.sqlite3"
    first = tmp_path / "a-ieee.docx"
    second = tmp_path / "b-second.docx"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _upsert_docx_source(
        source_database,
        first,
        title="IEEE C37.20.2",
        text="IEEE standard",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(catalog, source_database, "docx", verify_source_paths=False)
    before = _published_kinds(catalog)
    _upsert_docx_source(
        source_database,
        first,
        title="Factura proveedor",
        text="Factura compra",
        signature="v2",
    )
    _upsert_docx_source(
        source_database,
        second,
        title="Segundo documento",
        text="segundo",
        signature="v1",
    )
    token = CancellationToken()
    original = catalog_module.classify_document

    def cancel_after_first(*args: object, **kwargs: object):
        result = original(*args, **kwargs)
        token.cancel()
        return result

    monkeypatch.setattr(catalog_module, "classify_document", cancel_after_first)
    monkeypatch.setattr(catalog_module, "CATALOG_WRITE_BATCH", 1)

    with pytest.raises(CancellationRequested):
        update_document_catalog_source(
            catalog,
            source_database,
            "docx",
            verify_source_paths=False,
            cancellation=token,
        )

    assert _published_kinds(catalog) == before
    with document_catalog_database(catalog, readonly=True) as connection:
        statuses = tuple(
            connection.execute(
                """SELECT r.status,g.status FROM catalog_runs AS r
                JOIN catalog_generations AS g USING(catalog_run_id)
                ORDER BY r.catalog_run_id DESC LIMIT 1"""
            ).fetchone()
        )
    assert statuses == ("cancelled", "cancelled")


def test_failure_inside_publication_rolls_back_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database = tmp_path / "docx.sqlite3"
    source = tmp_path / "a-ieee.docx"
    source.write_bytes(b"first")
    _upsert_docx_source(
        source_database,
        source,
        title="IEEE C37.20.2",
        text="IEEE standard",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(catalog, source_database, "docx", verify_source_paths=False)
    before = _published_kinds(catalog)
    _upsert_docx_source(
        source_database,
        source,
        title="Factura proveedor",
        text="Factura compra",
        signature="v2",
    )
    original = catalog_module._replace_catalog_projection

    def fail_after_projection(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr(catalog_module, "_replace_catalog_projection", fail_after_projection)

    with pytest.raises(RuntimeError, match="injected publication failure"):
        update_document_catalog_source(catalog, source_database, "docx", verify_source_paths=False)

    assert _published_kinds(catalog) == before
    with document_catalog_database(catalog, readonly=True) as connection:
        generation_status = connection.execute(
            """SELECT status FROM catalog_generations
            ORDER BY generation_id DESC LIMIT 1"""
        ).fetchone()[0]
    assert generation_status == "failed"


def test_late_builder_cannot_overwrite_newer_publication(tmp_path: Path) -> None:
    source_database = tmp_path / "docx.sqlite3"
    source = tmp_path / "a-ieee.docx"
    source.write_bytes(b"first")
    _upsert_docx_source(
        source_database,
        source,
        title="IEEE C37.20.2",
        text="IEEE standard",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(catalog, source_database, "docx", verify_source_paths=False)
    with catalog_module._readonly_source(source_database) as source_connection:
        source_document = next(catalog_module._iter_source_documents(source_connection, "docx"))
    builds = []
    for _index in range(2):
        with document_catalog_database(catalog) as connection:
            build = catalog_module._begin_catalog_run(
                connection, source_kind="docx", framework_run_id=None
            )
            catalog_module._stage_cached_document(connection, build, source_document)
            connection.commit()
            builds.append(build)
    with document_catalog_database(catalog) as connection:
        organization_run_id = _begin_organization_run(connection, "plan", tmp_path / "organized")
        classification_statuses = tuple(
            str(row[0])
            for row in connection.execute(
                """SELECT status FROM catalog_runs
                WHERE catalog_run_id IN (?,?) ORDER BY catalog_run_id""",
                (builds[0].catalog_run_id, builds[1].catalog_run_id),
            )
        )
        connection.execute(
            """UPDATE catalog_runs SET status='completed',completed_ns=1
            WHERE catalog_run_id=?""",
            (organization_run_id,),
        )
        connection.commit()
    assert classification_statuses == ("running", "running")
    summary = catalog_module.CatalogUpdateSummary(
        catalog_run_id=builds[0].catalog_run_id,
        source_kind="docx",
        candidates=1,
        cache_hits=1,
    )
    with document_catalog_database(catalog) as connection:
        catalog_module._publish_catalog_build(connection, builds[0], summary)
    late_summary = catalog_module.CatalogUpdateSummary(
        catalog_run_id=builds[1].catalog_run_id,
        source_kind="docx",
        candidates=1,
        cache_hits=1,
    )

    with document_catalog_database(catalog) as connection:
        with pytest.raises(catalog_module.CatalogPublicationConflict):
            catalog_module._publish_catalog_build(connection, builds[1], late_summary)

    with document_catalog_database(catalog, readonly=True) as connection:
        published = connection.execute(
            "SELECT generation_id FROM catalog_publications WHERE source_kind='docx'"
        ).fetchone()[0]
        late_status = connection.execute(
            "SELECT status FROM catalog_generations WHERE generation_id=?",
            (builds[1].generation_id,),
        ).fetchone()[0]
    assert published == builds[0].generation_id
    assert late_status == "superseded"


def test_publish_handles_add_modify_delete_and_rename(tmp_path: Path) -> None:
    source_database = tmp_path / "docx.sqlite3"
    first = tmp_path / "a-ieee.docx"
    removed = tmp_path / "b-removed.docx"
    first.write_bytes(b"first")
    removed.write_bytes(b"removed")
    _upsert_docx_source(
        source_database,
        first,
        title="IEEE C37.20.2",
        text="IEEE standard",
        signature="v1",
    )
    _upsert_docx_source(
        source_database,
        removed,
        title="Documento temporal",
        text="temporal",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(catalog, source_database, "docx", verify_source_paths=False)
    with document_catalog_database(catalog) as connection:
        connection.execute(
            """INSERT INTO organization_plans(
            source_kind,file_key,source_path,destination_path,organization_root,
            volume_id,file_id,size,mtime_ns,birthtime_ns,classifier_signature,
            primary_kind,confidence,status,reason,evidence_json,planned_ns)
            SELECT source_kind,file_key,path,NULL,?,volume_id,file_id,size,mtime_ns,
            birthtime_ns,classifier_signature,primary_kind,confidence,'planned',
            'fixture','{}',1 FROM documents WHERE path=? COLLATE NOCASE""",
            (str(tmp_path / "organized"), str(removed)),
        )
        connection.commit()

    renamed = tmp_path / "renamed-invoice.docx"
    first.rename(renamed)
    _upsert_docx_source(
        source_database,
        renamed,
        title="Factura proveedor",
        text="Factura compra",
        signature="v2",
    )
    added = tmp_path / "c-added.docx"
    added.write_bytes(b"added")
    _upsert_docx_source(
        source_database,
        added,
        title="Nuevo informe de inspección",
        text="informe de inspección",
        signature="v1",
    )
    removed_key = f"{snapshot_path(removed).volume_id}:{snapshot_path(removed).file_id}"
    with sqlite3.connect(source_database) as connection:
        connection.execute("DELETE FROM documents WHERE file_key=?", (removed_key,))

    summary = update_document_catalog_source(
        catalog, source_database, "docx", verify_source_paths=False
    )

    current = _published_kinds(catalog)
    assert str(first) not in current
    assert current[str(renamed)] == "factura_comprobante"
    assert str(added) in current
    assert str(removed) not in current
    assert summary.stale_marked == 1
    with document_catalog_database(catalog, readonly=True) as connection:
        stale = connection.execute(
            "SELECT active FROM documents WHERE path=? COLLATE NOCASE",
            (str(removed),),
        ).fetchone()[0]
        plan_status = connection.execute("SELECT status FROM organization_plans").fetchone()[0]
    assert stale == 0
    assert plan_status == "superseded"


def test_published_catalog_records_replayable_manifest_and_digest(tmp_path: Path) -> None:
    source_database = tmp_path / "docx.sqlite3"
    source = tmp_path / "a-ieee.docx"
    source.write_bytes(b"first")
    _upsert_docx_source(
        source_database,
        source,
        title="IEEE C37.20.2",
        text="IEEE switchgear standard",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(
        catalog,
        source_database,
        "docx",
        verify_source_paths=False,
        source_root=tmp_path,
    )

    with document_catalog_database(catalog, readonly=True) as connection:
        manifest = read_catalog_publication_manifest(connection, "docx")
        actual_digest = catalog_generation_digest(connection, manifest.generation_id)
        assert manifest.generation_digest == actual_digest
        assert manifest.source_path == str(source_database.absolute())
        fence = json.loads(manifest.source_fence_json)
        assert fence["path"] == str(source_database.absolute())
        assert manifest.input_manifest_digest is not None
        scoped = validate_catalog_publication_scope(connection, "docx", tmp_path)
        assert scoped.generation_id == manifest.generation_id
        generation_status = connection.execute(
            "SELECT status FROM catalog_generations WHERE generation_id=?",
            (manifest.generation_id,),
        ).fetchone()[0]
    assert generation_status == "published"

    # A replay creates a successor generation with the same logical content
    # digest, while its source fence/input manifest remains independently bound.
    update_document_catalog_source(
        catalog,
        source_database,
        "docx",
        verify_source_paths=False,
        source_root=tmp_path,
    )
    with document_catalog_database(catalog, readonly=True) as connection:
        replay = read_catalog_publication_manifest(connection, "docx")
    assert replay.generation_id != manifest.generation_id
    assert replay.generation_digest == manifest.generation_digest


def test_catalog_source_drift_aborts_build_and_keeps_previous_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database = tmp_path / "docx.sqlite3"
    source = tmp_path / "a-ieee.docx"
    source.write_bytes(b"first")
    _upsert_docx_source(
        source_database,
        source,
        title="IEEE C37.20.2",
        text="IEEE switchgear standard",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(catalog, source_database, "docx", verify_source_paths=False)
    before = _published_kinds(catalog)
    _upsert_docx_source(
        source_database,
        source,
        title="Factura proveedor",
        text="Factura compra",
        signature="v2",
    )
    original = catalog_module.classify_document

    def drift_source(*args: object, **kwargs: object):
        result = original(*args, **kwargs)
        metadata = source_database.stat()
        os.utime(source_database, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1))
        return result

    monkeypatch.setattr(catalog_module, "classify_document", drift_source)
    with pytest.raises(CatalogSourceDrift, match="changed"):
        update_document_catalog_source(catalog, source_database, "docx", verify_source_paths=False)

    assert _published_kinds(catalog) == before
    with document_catalog_database(catalog, readonly=True) as connection:
        latest = connection.execute(
            "SELECT status FROM catalog_generations ORDER BY generation_id DESC LIMIT 1"
        ).fetchone()[0]
    assert latest == "failed"


def test_published_catalog_generation_rows_are_immutable(tmp_path: Path) -> None:
    source_database = tmp_path / "docx.sqlite3"
    source = tmp_path / "a-ieee.docx"
    source.write_bytes(b"first")
    _upsert_docx_source(
        source_database,
        source,
        title="IEEE C37.20.2",
        text="IEEE switchgear standard",
        signature="v1",
    )
    catalog = tmp_path / "document_catalog.sqlite3"
    update_document_catalog_source(catalog, source_database, "docx", verify_source_paths=False)
    with document_catalog_database(catalog) as connection:
        generation_id = connection.execute(
            "SELECT generation_id FROM catalog_publications WHERE source_kind='docx'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE catalog_generation_documents SET path=? WHERE generation_id=?",
                (str(tmp_path / "tampered.docx"), generation_id),
            )


# endregion [02]
