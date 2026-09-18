"""Every current owner is classified and additive reader fences preserve data."""
from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.deduplication.domain.errors import InventoryError
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


_INITIALIZERS = {
    "inventory": ("neocortex.deduplication.persistence", "initialize_inventory_schema"),
    "catalog": ("neocortex.documents.document_catalog", "initialize_document_catalog"),
    "pdf": ("neocortex.capabilities.formats.pdf.pdf_state", "initialize_pdf_state"),
    "docx": ("neocortex.capabilities.formats.docx.state", "initialize_docx_state"),
    "office": ("neocortex.capabilities.formats.office.state", "initialize_office_state"),
    "audio": ("neocortex.capabilities.formats.audio.state", "initialize_audio_state"),
    "video": ("neocortex.capabilities.formats.video.state", "initialize_video_state"),
    "image": ("neocortex.capabilities.formats.image.state", "initialize_image_state"),
    "archive": ("neocortex.capabilities.formats.archive.state", "initialize_archive_state"),
    "text": ("neocortex.capabilities.formats.text.text_state", "initialize_text_state"),
    "semantic": ("neocortex.semantic.semantic_schema", "initialize_semantic_state"),
    "code": ("neocortex.code.code_schema", "initialize_code_state"),
}


def _initialize(owner: str, database: Path) -> None:
    if owner == "framework":
        with FrameworkState(database):
            pass
    else:
        module, symbol = _INITIALIZERS[owner]
        getattr(importlib.import_module(module), symbol)(database)


@pytest.mark.parametrize("owner", tuple(store.state_owner_id for store in STATE_STORE_REGISTRY.stores))
def test_lifecycle_policy_covers_every_current_owner_table(tmp_path: Path, owner: str) -> None:
    contract = STATE_STORE_REGISTRY.by_owner(owner)
    database = tmp_path / contract.database_name
    _initialize(owner, database)
    with closing(sqlite3.connect(database)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    # Some owners create registered extensions on first use (for example Code
    # external run contracts); a fresh owner need not contain those tables yet.
    declared = {rule.table for rule in contract.lifecycle_rules}
    assert tables <= declared
    assert len(contract.lifecycle_rules) == len(declared)
    assert contract.lifecycle_rule("future_empty_extension") is None
    assert all(rule.reset_action == "preserve" for rule in contract.lifecycle_rules if rule.role == "authoritative")


@pytest.mark.parametrize("owner,version,version_module,version_symbol", [
    ("framework", 22, "neocortex.persistence.framework_schema", "SCHEMA_VERSION"),
    ("inventory", 13, "neocortex.deduplication.persistence.lifecycle", "SCHEMA_VERSION"),
    ("catalog", 10, "neocortex.documents.document_catalog", "CATALOG_SCHEMA_VERSION"),
])
def test_additive_reader_fence_preserves_prior_schema_and_rejects_older_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, version: int,
    version_module: str, version_symbol: str,
) -> None:
    contract = STATE_STORE_REGISTRY.by_owner(owner)
    database = tmp_path / contract.database_name
    _initialize(owner, database)
    # These immediate predecessor versions have the exact same physical DDL.
    # The migration only raises the supported-reader fence in metadata.
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(version),))
        connection.execute("INSERT INTO metadata(key,value) VALUES('fixture_evidence','retained')")
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
    _initialize(owner, database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (str(version + 1),)
        assert connection.execute("SELECT value FROM metadata WHERE key='fixture_evidence'").fetchone() == ("retained",)
        assert connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    before = database.read_bytes()
    monkeypatch.setattr(importlib.import_module(version_module), version_symbol, version)
    with pytest.raises((RuntimeError, InventoryError), match=r"newer|unsupported|supported"):
        _initialize(owner, database)
    assert database.read_bytes() == before


@pytest.mark.parametrize("owner,previous,missing_table", [
    ("framework", 22, "route_candidates"),
    ("inventory", 13, "inventory_generation_heads"),
    ("catalog", 10, "catalog_publications"),
])
def test_additive_reader_fence_does_not_repair_malformed_previous_schema(
    tmp_path: Path, owner: str, previous: int, missing_table: str,
) -> None:
    database = tmp_path / STATE_STORE_REGISTRY.by_owner(owner).database_name
    _initialize(owner, database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(previous),))
        connection.execute(f'DROP TABLE "{missing_table}"')
    with pytest.raises((RuntimeError, InventoryError)):
        _initialize(owner, database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == (str(previous),)
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (missing_table,)).fetchone() is None
