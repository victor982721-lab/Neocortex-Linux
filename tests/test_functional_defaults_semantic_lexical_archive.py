"""Functional lexical provenance for physical Archive logical roots."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.knowledge.knowledge_search import _candidate_from_resolved
from neocortex.semantic.semantic_lexical import (
    LexicalAvailability,
    LexicalStatePaths,
    search_lexical_source,
    search_lexical_sources,
)
from neocortex.semantic.semantic_models import ResolvedSearchHit


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


_ROOT_PATH = "/fixtures/renamed-document.ott"
_CONTAINER_KEY = "container-fixture"


def _create_archive_state(path: Path, *, forged_root_path: bool = False) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE containers(
                container_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                container_path TEXT NOT NULL,
                container_key TEXT NOT NULL,
                member_chain TEXT NOT NULL,
                member_path TEXT NOT NULL,
                archive_depth INTEGER NOT NULL,
                content_kind TEXT NOT NULL,
                media_type TEXT NOT NULL,
                status TEXT NOT NULL,
                text_chars INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL,
                document_role TEXT,
                logical_document_chain TEXT
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED,path UNINDEXED,container_path UNINDEXED,
                container_name,member_chain,content_kind,body,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        root_fts_path = f"{_ROOT_PATH}!/body" if forged_root_path else _ROOT_PATH
        root_text = "root durable lexical token"
        member_text = "member durable lexical token"
        connection.execute(
            """INSERT INTO documents(
                file_key,path,container_path,container_key,member_chain,member_path,
                archive_depth,content_kind,media_type,status,text_chars,size,mtime_ns,birthtime_ns,
                processing_signature,last_seen_run_id,document_role,logical_document_chain
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "archive:logical-root",
                _ROOT_PATH,
                _ROOT_PATH,
                _CONTAINER_KEY,
                "",
                "",
                0,
                "ott",
                "application/vnd.oasis.opendocument.text-template",
                "indexed",
                len(root_text),
                100,
                20,
                -1,
                "archive-fixture-v1",
                1,
                "logical_document",
                "",
            ),
        )
        connection.execute(
            """INSERT INTO documents(
                file_key,path,container_path,container_key,member_chain,member_path,
                archive_depth,content_kind,media_type,status,text_chars,size,mtime_ns,birthtime_ns,
                processing_signature,last_seen_run_id,document_role,logical_document_chain
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "archive:member",
                f"{_ROOT_PATH}!/word/document.xml",
                _ROOT_PATH,
                _CONTAINER_KEY,
                "word/document.xml",
                "word/document.xml",
                1,
                "xml",
                "application/xml",
                "indexed",
                len(member_text),
                90,
                20,
                -1,
                "archive-fixture-v1",
                1,
                "logical_document",
                "word/document.xml",
            ),
        )
        connection.execute(
            "INSERT INTO containers VALUES(?,?,?)",
            (_CONTAINER_KEY, _ROOT_PATH, "complete"),
        )
        connection.execute(
            """INSERT INTO document_fts VALUES(
                'archive:logical-root',?,?,?,?,?,?
            )""",
            (
                root_fts_path,
                _ROOT_PATH,
                Path(_ROOT_PATH).name,
                "",
                "ott",
                root_text,
            ),
        )
        connection.execute(
            """INSERT INTO document_fts VALUES(
                'archive:member',?,?,?,?,?,?
            )""",
            (
                f"{_ROOT_PATH}!/word/document.xml",
                _ROOT_PATH,
                Path(_ROOT_PATH).name,
                "word/document.xml",
                "xml",
                member_text,
            ),
        )


def _candidate(resolved: ResolvedSearchHit):
    return _candidate_from_resolved(
        resolved,
        ranking_name="fts_archive",
        source_rank=1,
        producer="semantic-lexical-fixture",
    )


def test_archive_lexical_root_materializes_body_without_fake_zip_locator(
    tmp_path: Path,
) -> None:
    state = tmp_path / "archive.sqlite3"
    _create_archive_state(state)

    root = search_lexical_source("archive", state, "root durable lexical", limit=1).hits[0]
    assert root.path == _ROOT_PATH
    assert root.section_kind == "archive_document"
    assert root.section_id == "body"
    assert root.section_provenance == {
        "inside_zip": False,
        "physical_root": True,
        "container_path": _ROOT_PATH,
        "container_key": _CONTAINER_KEY,
        "media_type": "application/vnd.oasis.opendocument.text-template",
        "container_status": "complete",
        "member_chain": "",
        "member_path": "",
        "archive_depth": 0,
        "content_kind": "ott",
        "document_role": "logical_document",
        "logical_document_chain": "",
    }
    root_candidate = _candidate(root)
    root_evidence = root_candidate.evidence
    assert root_candidate.resource.current_path == _ROOT_PATH
    assert root_evidence.section_kind == "archive_document"
    assert root_evidence.section_id == "body"
    assert "!/body" not in (root_candidate.resource.current_path or "")
    root_identifiers = dict(root_evidence.identifiers)
    assert root_identifiers["inside_zip"] == "0"
    assert "member_chain" not in root_identifiers
    assert "member_path" not in root_identifiers
    assert all(namespace and value for namespace, value in root_evidence.identifiers)

    member = search_lexical_source("archive", state, "member durable lexical", limit=1).hits[0]
    assert member.path == f"{_ROOT_PATH}!/word/document.xml"
    assert member.section_kind == "archive_member"
    assert member.section_id == "archive:member"
    assert member.section_provenance == {
        "inside_zip": True,
        "container_path": _ROOT_PATH,
        "member_chain": "word/document.xml",
        "member_path": "word/document.xml",
        "archive_depth": 1,
        "content_kind": "xml",
    }
    member_evidence = _candidate(member).evidence
    assert member_evidence.section_kind == "archive_member"
    assert member_evidence.section_id == "archive:member"
    assert dict(member_evidence.identifiers)["inside_zip"] == "1"
    assert dict(member_evidence.identifiers)["member_chain"] == "word/document.xml"


def test_forged_archive_root_path_abstains_as_unavailable_ranking(
    tmp_path: Path,
) -> None:
    state = tmp_path / "forged-archive.sqlite3"
    _create_archive_state(state, forged_root_path=True)

    rankings = search_lexical_sources(
        LexicalStatePaths(archive=state),
        "root durable lexical",
        limit=1,
    )
    archive = next(ranking for ranking in rankings if ranking.source_kind == "archive")
    assert archive.availability is LexicalAvailability.READ_FAILED
    assert archive.hits == ()
    assert archive.unavailable_reason == "state_database_read_failed:DataError"
