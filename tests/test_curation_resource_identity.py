"""P0 regressions: a resource key is not necessarily a filesystem identity."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from neocortex.api.cli.cli_curation import run_curation_preview
from neocortex.curation.preview import CurationStateError, _identity_payload, build_curation_preview
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.documents.document_catalog_schema import _create_v7_schema
from neocortex.documents.document_resource_binding import (
    ResourceBindingError,
    binding_curation_identity,
    build_resource_binding,
    legacy_resource_binding,
    parse_resource_binding,
    physical_identity_from_components,
)
from neocortex.foundation.file_identity import FileIdentity

from test_curation_preview import _build_state
from test_document_catalog_code_identity import _make_hex_code_owner


def _bytes(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus", exact_compare=True)
    return state


@pytest.mark.parametrize("volume,inode", [(16, 32), (26, 43), (66306, 21138331)])
def test_code_hex_is_normalized_without_reinterpreting_digits(volume: int, inode: int) -> None:
    identity = physical_identity_from_components(
        format(volume, "x"), format(inode, "x"), encoding="code-owner-hex"
    )
    binding = build_resource_binding(
        source_kind="code",
        file_key="code:123",
        path="/fixture.py",
        identity=identity,
        birthtime_ns=-1,
        size=1,
        mtime_ns=2,
    )
    payload = binding_curation_identity(binding)
    assert payload == {
        "volume_id": format(volume, "x"),
        "file_id": format(inode, "x"),
        "birthtime_ns": -1,
    }
    assert binding["file_key"] == "code:123"


@pytest.mark.parametrize(
    "volume,inode,birth,field",
    [
        (-1, 2, -1, "volume_id"),
        (1 << 128, 2, -1, "volume_id"),
        (b"", b"", -1, "volume_id"),
        (b"a" * 17, b"b" * 16, -1, "volume_id"),
        (1, 2, -2, "birthtime_ns"),
        (1, 2, True, "birthtime_ns"),
        ("0x10", "2", -1, "volume_id"),
        ("01", "2", -1, "volume_id"),
    ],
)
def test_curation_identity_rejects_invalid_physical_components(volume, inode, birth, field) -> None:
    with pytest.raises(CurationStateError) as captured:
        _identity_payload(volume, inode, birth, context={"owner": "fixture", "record_id": 7})
    assert captured.value.context["field"] == field
    assert captured.value.context["record_id"] == 7


def test_incident_archive_plan_2676_remains_visible_without_physical_effect(tmp_path: Path) -> None:
    state = _state(tmp_path)
    digest = "f816a82ff9db5e9fcd70ff0b5c481805"
    with sqlite3.connect(state / "document_catalog.sqlite3") as db:
        db.execute(
            "UPDATE organization_plans SET plan_id=2676,source_kind='archive',file_key=?,volume_id='archive',file_id=?,source_path=?",
            (
                f"archive:{digest}",
                digest,
                str(tmp_path / "corpus" / "bundle.zip") + "!/fixture.pdf",
            ),
        )
    before = _bytes(state)
    first = build_curation_preview(state, limit=20)
    second = build_curation_preview(state, limit=20)
    item = next(item for item in first.items if item.item_id == "organization:2676")
    assert first.organization_plans == 1 and first.duplicate_groups == 1
    assert first.preview_fingerprint == second.preview_fingerprint
    assert item.evidence["identity"] is None
    assert item.evidence["executable"] is False
    assert "virtual_resource_requires_logical_review" in item.evidence["blockers"]
    assert item.evidence["resource_binding"]["file_key"] == f"archive:{digest}"
    assert _bytes(state) == before


def test_legacy_code_ambiguity_is_localizable_json_and_not_a_partial_plan(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with sqlite3.connect(state / "document_catalog.sqlite3") as db:
        db.execute(
            "UPDATE organization_plans SET source_kind='code',file_key='code:9',volume_id='10',file_id='20'"
        )
    before = _bytes(state)
    output = io.StringIO()
    with redirect_stdout(output):
        result = run_curation_preview(
            argparse.Namespace(state_directory=state, curation_preview=20, curation_json=True)
        )
    payload = json.loads(output.getvalue())
    assert result == 2 and payload["code"] == "identity_encoding_unresolved"
    assert payload["coverage"] == "unavailable" and payload["executable"] is False
    assert payload["context"]["owner"] == "document_catalog.sqlite3"
    assert payload["context"]["table"] == "organization_plans"
    assert payload["context"]["record_id"] == 1
    assert payload["context"]["publication"]["catalog_run_id"] == 1
    assert payload["context"]["publication"]["scan_id"] >= 1
    assert _bytes(state) == before


def test_new_code_binding_survives_preview(tmp_path: Path) -> None:
    state = _state(tmp_path)
    source = tmp_path / "corpus" / "keep.txt"
    binding = build_resource_binding(
        source_kind="code",
        file_key="code:9",
        path=str(source),
        identity=FileIdentity(16, 32),
        birthtime_ns=-1,
        size=12,
        mtime_ns=1,
    )
    with sqlite3.connect(state / "document_catalog.sqlite3") as db:
        db.execute(
            "UPDATE organization_plans SET source_kind='code',file_key='code:9',volume_id='16',file_id='32',resource_binding_json=?",
            (json.dumps(binding),),
        )
    preview = build_curation_preview(state, limit=20)
    item = next(item for item in preview.items if item.kind == "organization_plan")
    assert item.evidence["identity"] == {"volume_id": "10", "file_id": "20", "birthtime_ns": -1}


def test_v7_migration_is_additive_backed_up_and_replays_without_a_second_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(path) as db:
        _create_v7_schema(db)
        db.execute("INSERT INTO metadata VALUES('schema_version','7')")
        db.execute(
            "INSERT INTO catalog_runs(catalog_run_id,source_kind,mode,status,started_ns) VALUES(112,'all','plan','completed',1)"
        )
        db.execute(
            "INSERT INTO organization_plans(plan_id,catalog_run_id,source_kind,file_key,source_path,organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,classifier_signature,primary_kind,confidence,status,reason,evidence_json,planned_ns) VALUES(2676,112,'archive','archive:opaque','/fixture.zip!/member','/organized','archive','opaque',12,1,-1,'fixture','otro',0.1,'review','fixture','{}',1)"
        )
        prior = tuple(db.execute("SELECT * FROM organization_plans").fetchone())
    initialize_document_catalog(path)
    backups = list(tmp_path.glob("*.pre-v7-to-v8-*.sqlite3"))
    assert len(backups) == 1
    receipt = json.loads(backups[0].with_suffix(".sqlite3.json").read_text())
    assert receipt["sha256"] == hashlib.sha256(backups[0].read_bytes()).hexdigest()
    with sqlite3.connect(backups[0]) as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "7"
        )
        assert tuple(db.execute("SELECT * FROM organization_plans").fetchone()) == prior
    with sqlite3.connect(path) as db:
        current = tuple(db.execute("SELECT * FROM organization_plans").fetchone())
        assert current[: len(prior)] == prior
        assert db.execute(
            "SELECT resource_binding_json,executable FROM organization_plans"
        ).fetchone() == (None, 0)
    before = _bytes(tmp_path)
    initialize_document_catalog(path)
    assert _bytes(tmp_path) == before


def test_catalog_code_adapter_publishes_canonical_decimal_binding_and_replay(
    tmp_path: Path,
) -> None:
    from neocortex.documents.document_catalog import update_document_catalog_source

    source, owner, catalog = (
        tmp_path / "module.py",
        tmp_path / "code.sqlite3",
        tmp_path / "catalog.sqlite3",
    )
    source.write_text("def value(): return 7\n")
    _make_hex_code_owner(owner, source)
    first = update_document_catalog_source(catalog, owner, "code", verify_source_paths=True)
    second = update_document_catalog_source(catalog, owner, "code", verify_source_paths=True)
    assert first.classified == 1 and second.cache_hits == 1
    with sqlite3.connect(catalog) as db:
        row = db.execute(
            "SELECT file_key,volume_id,file_id,birthtime_ns,resource_binding_json FROM documents WHERE active=1"
        ).fetchone()
    snapshot = source.stat()
    assert row[:4] == ("code:1", str(snapshot.st_dev), str(snapshot.st_ino), -1)
    binding = parse_resource_binding(row[4])
    assert (
        binding["physical_identity"]["packed_key"]
        == FileIdentity(snapshot.st_dev, snapshot.st_ino).packed_key
    )


def test_legacy_code_is_not_guessed_even_when_hex_contains_letters() -> None:
    with pytest.raises(ResourceBindingError, match="owner-backed"):
        legacy_resource_binding(
            source_kind="code",
            file_key="code:1",
            path="/fixture.py",
            volume_id="1a",
            file_id="2b",
            birthtime_ns=-1,
            size=1,
            mtime_ns=1,
        )


def test_scoped_catalog_update_preserves_outside_rows_without_classifying_them(
    tmp_path: Path, monkeypatch
) -> None:
    import neocortex.documents.document_catalog as catalog_module

    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    selected, retained = inside / "selected.py", outside / "retained.py"
    selected.write_text("def value(): return 1\n")
    retained.write_text("def value(): return 2\n")
    owner, catalog = tmp_path / "code.sqlite3", tmp_path / "catalog.sqlite3"
    _make_hex_code_owner(owner, selected)
    snapshot = retained.stat()
    with sqlite3.connect(owner) as db:
        db.execute(
            "INSERT INTO files VALUES(2,?,?,?,2,'current')",
            (format(snapshot.st_dev, "x"), format(snapshot.st_ino, "x"), str(retained)),
        )
        db.execute(
            "INSERT INTO file_versions SELECT 2,2,?,?,birthtime_ns,analysis_status,processing_signature,language,artifact_kind,text_xxh3_128,text_truncated,text_zlib,provenance_json FROM file_versions WHERE version_id=1",
            (snapshot.st_size, snapshot.st_mtime_ns),
        )
    baseline = catalog_module.update_document_catalog_source(catalog, owner, "code")
    assert baseline.candidates == 2
    with sqlite3.connect(catalog) as db:
        outside_before = tuple(
            db.execute("SELECT * FROM documents WHERE file_key='code:2'").fetchone()
        )
    verifier = catalog_module._source_snapshot_is_current

    def selected_only(document):
        assert document.path == str(selected), "a scoped update inspected an outside source"
        return verifier(document)

    monkeypatch.setattr(catalog_module, "_source_snapshot_is_current", selected_only)
    result = catalog_module.update_document_catalog_source(
        catalog, owner, "code", source_root=inside
    )
    assert result.candidates == 1 and result.cache_hits == 1 and result.classified == 0
    assert result.stale_marked == 0
    with sqlite3.connect(catalog) as db:
        assert (
            tuple(db.execute("SELECT * FROM documents WHERE file_key='code:2'").fetchone())
            == outside_before
        )
        assert db.execute("SELECT COUNT(*) FROM documents WHERE active=1").fetchone()[0] == 2


def test_invalid_scoped_root_fails_before_catalog_creation(tmp_path: Path) -> None:
    from neocortex.documents.document_catalog import update_document_catalog_source

    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    catalog = tmp_path / "catalog.sqlite3"
    with pytest.raises(ValueError, match="symlink"):
        update_document_catalog_source(
            catalog, tmp_path / "absent.sqlite3", "code", source_root=alias
        )
    assert not catalog.exists()


@pytest.mark.parametrize("missing", ["resource_binding", "source_scope_json", "source_scope_id"])
def test_organization_grant_refuses_legacy_identity_or_scope(tmp_path: Path, missing: str) -> None:
    from dataclasses import replace
    from neocortex.curation.authorization import (
        CurationAuthorizationError,
        _validate_requested_effect,
    )
    from test_curation_lifecycle import _state as reviewed_state

    state, _framework, _digest = reviewed_state(tmp_path)
    page = build_curation_preview(state, limit=20)
    item = next(item for item in page.items if item.kind == "organization_plan")
    evidence = dict(item.evidence)
    evidence.pop(missing)
    before = _bytes(state)
    with pytest.raises(CurationAuthorizationError, match="identity/scope"):
        _validate_requested_effect(
            replace(item, evidence=evidence),
            "move",
            state_directory=state,
            inventory_root=page.root,
        )
    assert _bytes(state) == before


@pytest.mark.parametrize("broader_inventory", [False, True])
def test_organization_authorization_does_not_require_an_effect_backend(
    tmp_path: Path, broader_inventory: bool
) -> None:
    from neocortex.curation.authorization import _validate_requested_effect
    from test_curation_lifecycle import _state as reviewed_state

    state, _framework, _digest = reviewed_state(tmp_path)
    page = build_curation_preview(state, limit=20)
    item = next(item for item in page.items if item.kind == "organization_plan")
    assert (
        item.evidence["executable"] is False and "backend_unavailable" in item.evidence["blockers"]
    )
    before = _bytes(state)
    assert (
        _validate_requested_effect(
            item,
            "move",
            state_directory=state,
            inventory_root=str(tmp_path) if broader_inventory else page.root,
        )
        == 4
    )
    assert _bytes(state) == before


def test_organization_grant_refuses_changed_physical_anchor(tmp_path: Path) -> None:
    from neocortex.curation.authorization import (
        CurationAuthorizationError,
        _validate_requested_effect,
    )
    from test_curation_lifecycle import _state as reviewed_state

    state, _framework, _digest = reviewed_state(tmp_path)
    page = build_curation_preview(state, limit=20)
    item = next(item for item in page.items if item.kind == "organization_plan")
    Path(item.source_path).write_bytes(b"changed after review")
    before = _bytes(state)
    with pytest.raises(CurationAuthorizationError, match="identity/scope"):
        _validate_requested_effect(item, "move", state_directory=state, inventory_root=page.root)
    assert _bytes(state) == before


def test_current_code_codec_cannot_be_reinterpreted_by_a_path_match() -> None:
    from neocortex.documents.document_catalog import _code_source_identity

    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO metadata VALUES('schema_version','7')")
        identity = _code_source_identity(
            db,
            {"volume_id": "10", "physical_file_id": "20", "current_path": "/not-read"},
            verify_source_paths=True,
        )
    assert identity == FileIdentity(16, 32)


def test_resource_binding_rejects_duplicate_json_fields() -> None:
    binding = build_resource_binding(
        source_kind="code",
        file_key="code:1",
        path="/fixture.py",
        identity=FileIdentity(16, 32),
        birthtime_ns=-1,
        size=1,
        mtime_ns=1,
    )
    raw = json.dumps(binding)
    with pytest.raises(ResourceBindingError, match="duplicate"):
        parse_resource_binding(raw[:-1] + ',"source_kind":"code"}')
