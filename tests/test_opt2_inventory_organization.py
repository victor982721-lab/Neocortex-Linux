"""D2-CATALOG reuses already validated organization bindings."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.deduplication import snapshot_path
import neocortex.documents.document_catalog as catalog
from neocortex.documents.document_catalog import update_document_catalog_source
import neocortex.documents.document_organization_planning as planning
import neocortex.documents.document_organization_scope as scope_module
from neocortex.documents.document_organization_scope import capture_organization_input_scope


def test_organization_plan_does_not_reparse_an_assessed_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    source = state / "docx.sqlite3"
    catalog_path = state / "catalog.sqlite3"
    organization_root = tmp_path / "organized"
    initialize_docx_state(source)
    with sqlite3.connect(source) as connection:
        for number in range(2):
            path = root / f"{number:02d}-standard.docx"
            path.write_bytes(b"synthetic document")
            snapshot = snapshot_path(path)
            connection.execute(
                """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
                integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
                updated_ns,title,author) VALUES(?,?,?,?,?,?,'complete','valid',?,?,?,?,?,?,?)""",
                (
                    f"{snapshot.volume_id}:{snapshot.file_id}",
                    snapshot.path,
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    "fixture-v1",
                    zlib.compress(b"IEEE switchgear standard"),
                    23,
                    "fixture-text-v1",
                    1,
                    1,
                    "IEEE C37.20.2",
                    "",
                ),
            )

    original_classifier = catalog.classify_document

    def serial_classifier(signals, taxonomy):
        return original_classifier(signals, taxonomy)

    monkeypatch.setattr(catalog, "classify_document", serial_classifier)
    update_document_catalog_source(
        catalog_path,
        source,
        "docx",
        source_root=root,
        verify_source_paths=False,
    )
    scope = capture_organization_input_scope(catalog_path, root)

    planning_calls = 0
    scope_calls = 0
    original_planning_parser = planning.parse_resource_binding
    original_scope_parser = scope_module.parse_resource_binding

    def count_planning_parse(value):
        nonlocal planning_calls
        planning_calls += 1
        return original_planning_parser(value)

    def count_scope_parse(value):
        nonlocal scope_calls
        scope_calls += 1
        return original_scope_parser(value)

    monkeypatch.setattr(planning, "parse_resource_binding", count_planning_parse)
    monkeypatch.setattr(scope_module, "parse_resource_binding", count_scope_parse)
    summary = planning.plan_document_organization(
        catalog_path,
        organization_root,
        source_scope=scope,
    )

    assert (summary.considered, summary.planned, summary.blocked) == (2, 2, 0)
    assert planning_calls == 0
    assert scope_calls == 4  # two scope/identity assessments per document
