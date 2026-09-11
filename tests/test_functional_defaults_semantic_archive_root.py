"""Functional Semantic adapters for physical Archive logical-document roots."""

from __future__ import annotations

import sqlite3
import io
import zlib
import zipfile
from pathlib import Path

import pytest

from neocortex.semantic import semantic_sources
from neocortex.semantic.semantic_models import fingerprint_text
from neocortex.semantic.semantic_sources import (
    SemanticSourceError,
    iter_text_source_records,
    semantic_source_database,
)
from neocortex.capabilities.formats.archive.route import (
    ARCHIVE_MIME,
    ArchiveRoute,
    ArchiveRouteConfig,
)
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.route_filters import CandidateSelection


class _ArchiveRouteFramework:
    def __init__(self, snapshot: FileSnapshot) -> None:
        self.snapshot = snapshot

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        _max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        return 1, 1

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection: CandidateSelection,
    ):
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        yield self.snapshot


def _schema(connection: sqlite3.Connection, *, modern: bool) -> None:
    role_columns = (
        ",document_role TEXT,logical_document_chain TEXT" if modern else ""
    )
    connection.executescript(
        f"""
        CREATE TABLE containers(
            container_key TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE documents(
            file_key TEXT PRIMARY KEY,
            path TEXT,
            processing_signature TEXT,
            status TEXT,
            size INTEGER,
            mtime_ns INTEGER,
            birthtime_ns INTEGER,
            last_seen_run_id INTEGER,
            text_xxh3_128 TEXT,
            text_chars INTEGER,
            text_zlib BLOB,
            container_path TEXT,
            container_key TEXT,
            member_chain TEXT,
            member_path TEXT,
            archive_depth INTEGER,
            content_kind TEXT,
            media_type TEXT
            {role_columns}
        );
        """
    )


def _row(
    *,
    file_key: str,
    path: str | None,
    container_path: str,
    member_chain: str,
    member_path: str,
    archive_depth: int,
    text: str,
    modern: bool,
    role: str | None = None,
    logical_chain: str | None = None,
) -> tuple[object, ...]:
    encoded = text.encode("utf-8")
    values: tuple[object, ...] = (
        file_key,
        path,
        "archive-fixture-v1",
        "indexed",
        len(encoded),
        1,
        -1,
        1,
        fingerprint_text(text).xxh3_128,
        len(text),
        zlib.compress(encoded),
        container_path,
        "container-fixture",
        member_chain,
        member_path,
        archive_depth,
        "docx" if archive_depth == 0 else "xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if archive_depth == 0
        else "application/xml",
    )
    return (*values, role, logical_chain) if modern else values


def _create_state(
    root: Path,
    *,
    modern: bool,
    rows: tuple[tuple[object, ...], ...],
) -> Path:
    database = semantic_source_database(root, "archive")
    container_path = "/private/renamed.docx.zip"
    with sqlite3.connect(database) as connection:
        _schema(connection, modern=modern)
        connection.execute(
            "INSERT INTO containers VALUES('container-fixture',?,'complete')",
            (container_path,),
        )
        columns = (
            "file_key,path,processing_signature,status,size,mtime_ns,birthtime_ns,"
            "last_seen_run_id,text_xxh3_128,text_chars,text_zlib,container_path,"
            "container_key,member_chain,member_path,archive_depth,content_kind,media_type"
        )
        if modern:
            columns += ",document_role,logical_document_chain"
        placeholders = ",".join("?" for _ in columns.split(","))
        connection.executemany(
            f"INSERT INTO documents({columns}) VALUES({placeholders})",
            rows,
        )
        connection.commit()
    return database


def _modern_rows() -> tuple[tuple[object, ...], ...]:
    container = "/private/renamed.docx.zip"
    return (
        _row(
            file_key="archive:logical-root",
            path=container,
            container_path=container,
            member_chain="",
            member_path="",
            archive_depth=0,
            text="contenido raíz lógico durable",
            modern=True,
            role="logical_document",
            logical_chain="",
        ),
        _row(
            file_key="archive:modern-member",
            path=f"{container}!/word/document.xml",
            container_path=container,
            member_chain="word/document.xml",
            member_path="word/document.xml",
            archive_depth=1,
            text="contenido del miembro virtual durable",
            modern=True,
            role="document_component",
            logical_chain="",
        ),
    )


def test_archive_adapter_distinguishes_physical_logical_root_and_virtual_member(
    tmp_path: Path,
) -> None:
    database = _create_state(tmp_path, modern=True, rows=_modern_rows())

    records = tuple(iter_text_source_records(tmp_path, "archive"))

    root = next(item for item in records if item.section.section_kind == "archive_document")
    member = next(item for item in records if item.section.section_kind == "archive_member")
    assert root.section.section_id == "body"
    assert root.item.path == "/private/renamed.docx.zip"
    assert root.section.provenance["inside_zip"] is False
    assert root.section.provenance["physical_root"] is True
    assert root.section.provenance["document_role"] == "logical_document"
    assert "!/" not in root.item.path
    assert member.section.section_id == "word/document.xml"
    assert member.item.path == "/private/renamed.docx.zip!/word/document.xml"
    assert member.section.provenance == {
        "adapter": "semantic-source-adapters-v3",
        "inside_zip": True,
        "container_path": "/private/renamed.docx.zip",
        "container_key": "container-fixture",
        "member_chain": "word/document.xml",
        "member_path": "word/document.xml",
        "archive_depth": 1,
        "content_kind": "xml",
        "media_type": "application/xml",
        "container_status": "complete",
    }

    with sqlite3.connect(database) as connection:
        query, parameters = semantic_sources._source_head_query(connection, "archive")
        head_rows = connection.execute(query, parameters).fetchall()
    assert {tuple(row[-2:]) for row in head_rows} == {
        ("logical_document", ""),
        ("document_component", ""),
    }


@pytest.mark.parametrize("depth", (0.5, -0.5))
def test_archive_adapter_does_not_truncate_fractional_depth_into_a_root(
    tmp_path: Path, depth: float,
) -> None:
    database = _create_state(tmp_path, modern=True, rows=_modern_rows()[:1])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE documents SET archive_depth=?", (depth,))
    with pytest.raises(semantic_sources.SemanticSourceError, match="invalid archive depth"):
        tuple(iter_text_source_records(tmp_path, "archive"))


def test_archive_adapter_reads_a_renamed_odf_zip_root_without_inventing_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "renamed-container.bin"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "mimetype", "application/vnd.oasis.opendocument.text-template"
        )
        archive.writestr("content.xml", "<document>ODF raíz durable</document>")
        archive.writestr("styles.xml", "<styles/>")
        archive.writestr(
            "META-INF/manifest.xml",
            "<manifest/>",
        )
    source.write_bytes(payload.getvalue())
    state = tmp_path / "state" / "archive.sqlite3"
    state.parent.mkdir()
    snapshot = snapshot_path(source)

    summary = ArchiveRoute(
        ArchiveRouteConfig(state_path=state, ocr_mode="never"),
        _ArchiveRouteFramework(snapshot),
        1,
        cancellation=CancellationToken(),
    ).run()

    assert summary.containers_complete == 1
    records = tuple(iter_text_source_records(tmp_path / "state", "archive"))
    root = next(item for item in records if item.section.section_kind == "archive_document")
    member = next(
        item
        for item in records
        if item.section.section_kind == "archive_member"
        and item.section.section_id == "content.xml"
    )
    assert root.section.section_id == "body"
    assert root.item.path == str(source)
    assert root.section.provenance["inside_zip"] is False
    assert member.section.section_id == "content.xml"
    assert member.item.path == f"{source}!/content.xml"
    assert member.section.provenance["inside_zip"] is True


def test_archive_adapter_keeps_nested_logical_package_as_virtual_member(
    tmp_path: Path,
) -> None:
    inner_payload = io.BytesIO()
    with zipfile.ZipFile(inner_payload, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "mimetype", "application/vnd.oasis.opendocument.text-template"
        )
        archive.writestr("content.xml", "<document>nested ODF durable</document>")
        archive.writestr("styles.xml", "<styles/>")
        archive.writestr("META-INF/manifest.xml", "<manifest/>")

    source = tmp_path / "outer-container.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("template.zip", inner_payload.getvalue())
    state = tmp_path / "archive.sqlite3"
    snapshot = snapshot_path(source)

    summary = ArchiveRoute(
        ArchiveRouteConfig(state_path=state, ocr_mode="never"),
        _ArchiveRouteFramework(snapshot),
        1,
        cancellation=CancellationToken(),
    ).run()

    assert summary.containers_complete == 1
    records = tuple(iter_text_source_records(tmp_path, "archive"))
    nested = next(item for item in records if item.section.section_id == "template.zip")
    assert nested.section.section_kind == "archive_member"
    assert nested.item.path == f"{source}!/template.zip"
    assert nested.section.provenance["inside_zip"] is True
    assert nested.section.provenance["member_chain"] == "template.zip"
    assert nested.section.provenance["archive_depth"] == 1
    assert nested.section.provenance["content_kind"] == "ott"
    assert nested.section.provenance["media_type"] == (
        "application/vnd.oasis.opendocument.text-template"
    )
    assert "document_role" not in nested.section.provenance
    assert "physical_root" not in nested.section.provenance


def test_archive_adapter_keeps_legacy_empty_chain_physical_root_readable(
    tmp_path: Path,
) -> None:
    container = "/private/legacy-container.zip"
    row = _row(
        file_key="archive:legacy-root",
        path=container,
        container_path=container,
        member_chain="",
        member_path="",
        archive_depth=0,
        text="contenido legacy raíz",
        modern=False,
    )
    _create_state(tmp_path, modern=False, rows=(row,))

    records = tuple(iter_text_source_records(tmp_path, "archive"))

    assert len(records) == 1
    record = records[0]
    assert record.section.section_kind == "archive_document"
    assert record.section.section_id == "body"
    assert record.section.provenance["inside_zip"] is False
    assert record.section.provenance["section_identity_evidence"] == (
        "legacy_physical_root_shape"
    )
    assert record.item.path == container


@pytest.mark.parametrize(
    ("role", "member_chain", "member_path", "depth", "path", "message"),
    (
        (
            "archive_member",
            "",
            "",
            1,
            "/private/invalid.zip",
            "empty member_chain",
        ),
        (
            "unsupported_role",
            "word/document.xml",
            "word/document.xml",
            1,
            "/private/invalid.zip!/word/document.xml",
            "unsupported document role",
        ),
        (
            "archive_member",
            "member.txt",
            "member.txt",
            1,
            None,
            "missing physical path or identity",
        ),
    ),
)
def test_archive_adapter_rejects_ambiguous_or_missing_virtual_identity(
    tmp_path: Path,
    role: str,
    member_chain: str,
    member_path: str,
    depth: int,
    path: str | None,
    message: str,
) -> None:
    container = "/private/invalid.zip"
    row = _row(
        file_key="archive:invalid",
        path=path,
        container_path=container,
        member_chain=member_chain,
        member_path=member_path,
        archive_depth=depth,
        text="contenido inválido",
        modern=True,
        role=role,
        logical_chain="",
    )
    _create_state(tmp_path, modern=True, rows=(row,))

    with pytest.raises(SemanticSourceError, match=message):
        tuple(iter_text_source_records(tmp_path, "archive"))
