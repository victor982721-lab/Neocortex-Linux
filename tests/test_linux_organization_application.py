"""Linux-only synthetic fixtures for the document-organization effect owner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path

import pytest

import neocortex.documents.document_organization_application as organization_application
from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.documents.document_catalog import (
    document_catalog_database,
    update_document_catalog,
)
from neocortex.documents.document_organization import (
    apply_all_document_organization,
    apply_document_organization,
    capture_organization_input_scope,
    plan_document_organization,
)
from neocortex.safety.corpus_access import CorpusAccessPolicy, CorpusMutationGuard
from neocortex.runtime.control.locking import FrameworkRunLock
from tests.internal_paths_test_support import disjoint_internal_paths_policy


@dataclass(frozen=True, slots=True)
class _OrganizationFixture:
    base: Path
    state: Path
    corpus: Path
    source: Path
    catalog: Path
    destination_root: Path
    destination: Path
    guard: CorpusMutationGuard


def _seed_docx(state: Path, source: Path) -> None:
    """Create only the disposable owner state needed by cache rebinding."""

    initialize_docx_state(state)
    observed = source.stat()
    with sqlite3.connect(state) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            integrity_status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,
            updated_ns,title,author)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{observed.st_dev}:{observed.st_ino}",
                str(source),
                observed.st_size,
                observed.st_mtime_ns,
                getattr(observed, "st_birthtime_ns", -1),
                "docx-linux-organization-fixture-v1",
                "complete",
                "valid",
                zlib.compress(b"Formato SERINTRA formulario de control"),
                39,
                "fixture-text-fingerprint",
                1,
                1,
                "Formato SERINTRA",
                "SERINTRA",
            ),
        )


@pytest.fixture
def organization_fixture(tmp_path: Path) -> _OrganizationFixture:
    base = tmp_path
    state = base / "state"
    state.mkdir()
    corpus = base / "corpus"
    incoming = corpus / "incoming"
    incoming.mkdir(parents=True)
    source = incoming / "Formato SERINTRA.docx"
    source.write_bytes(b"synthetic Linux organization fixture")
    _seed_docx(state / "docx.sqlite3", source)
    update_document_catalog(state)
    catalog = state / "document_catalog.sqlite3"
    destination_root = corpus / "organized"
    scope = capture_organization_input_scope(catalog, corpus)
    plan = plan_document_organization(catalog, destination_root, source_scope=scope)
    assert plan.planned == 1
    assert plan.executable == 1
    with document_catalog_database(catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT destination_path FROM organization_plans WHERE status='planned'"
        ).fetchone()
    assert row is not None
    destination = Path(str(row[0]))
    guard = CorpusMutationGuard(
        CorpusAccessPolicy.capture("normal", base),
        disjoint_internal_paths_policy(base),
    )
    return _OrganizationFixture(
        base,
        state,
        corpus,
        source,
        catalog,
        destination_root,
        destination,
        guard,
    )


def test_linux_organization_applies_one_move_and_replay_is_zero(
    organization_fixture: _OrganizationFixture,
) -> None:
    fixture = organization_fixture
    first = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )
    assert first.applied == 1
    assert first.cache_synced == 1
    assert first.cache_pending == 0
    assert not fixture.source.exists()
    assert fixture.destination.read_bytes() == b"synthetic Linux organization fixture"

    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        plan = connection.execute(
            """SELECT status,cache_sync_status,cache_sync_json
            FROM organization_plans"""
        ).fetchone()
        document = connection.execute("SELECT path FROM documents").fetchone()
    assert plan is not None and plan["status"] == "applied"
    assert plan["cache_sync_status"] == "synced"
    payload = json.loads(str(plan["cache_sync_json"]))
    assert payload["physical_receipt"]["backend"] == "posix-link-unlink-no-replace-v1"
    assert document is not None and Path(str(document["path"])) == fixture.destination

    with sqlite3.connect(fixture.state / "docx.sqlite3") as connection:
        assert connection.execute("SELECT path FROM documents").fetchone()[0] == str(
            fixture.destination
        )

    replay = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )
    assert replay.selected == 0
    assert replay.applied == 0
    assert replay.cache_pending == 0
    assert replay.remaining == 0
    assert fixture.destination.read_bytes() == b"synthetic Linux organization fixture"


def test_linux_organization_collision_disambiguates_without_overwrite(
    organization_fixture: _OrganizationFixture,
) -> None:
    fixture = organization_fixture
    fixture.destination.parent.mkdir(parents=True)
    fixture.destination.write_bytes(b"pre-existing unrelated destination")

    result = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )

    assert result.applied == 1
    assert not fixture.source.exists()
    assert fixture.destination.read_bytes() == b"pre-existing unrelated destination"
    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT destination_path,reason FROM organization_plans"
        ).fetchone()
    assert row is not None
    disambiguated = Path(str(row["destination_path"]))
    assert disambiguated != fixture.destination
    assert disambiguated.read_bytes() == b"synthetic Linux organization fixture"
    assert "identity_disambiguation" in str(row["reason"])


def test_linux_organization_source_drift_is_stale_and_preserves_original(
    organization_fixture: _OrganizationFixture,
) -> None:
    fixture = organization_fixture
    fixture.source.write_bytes(b"changed after planning")

    result = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )

    assert result.applied == 0
    assert result.blocked == 1
    assert fixture.source.read_bytes() == b"changed after planning"
    assert not fixture.destination.exists()
    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        assert connection.execute("SELECT status FROM organization_plans").fetchone()[0] == "blocked"


def test_linux_organization_cache_pending_replays_receipt_and_rebinds_owners(
    organization_fixture: _OrganizationFixture,
) -> None:
    fixture = organization_fixture
    unavailable = fixture.state / "docx.sqlite3.unavailable"
    (fixture.state / "docx.sqlite3").replace(unavailable)
    try:
        first = apply_document_organization(
            fixture.catalog,
            fixture.destination_root,
            mutation_guard=fixture.guard,
            max_actions=1,
        )
    finally:
        unavailable.replace(fixture.state / "docx.sqlite3")

    assert first.applied == 0
    assert first.cache_pending == 1
    assert not fixture.source.exists()
    assert fixture.destination.is_file()
    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT status,cache_sync_status,cache_sync_json FROM organization_plans"
        ).fetchone()
    assert row is not None
    assert (row["status"], row["cache_sync_status"]) == ("moved_cache_pending", "pending")
    pending_payload = json.loads(str(row["cache_sync_json"]))
    assert pending_payload["physical_receipt"]["source_absent"] is True

    second = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )
    assert second.applied == 1
    assert second.cache_synced == 1
    assert second.cache_pending == 0
    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT status,cache_sync_status,cache_sync_json FROM organization_plans"
        ).fetchone()
    assert row is not None
    assert (row["status"], row["cache_sync_status"]) == ("applied", "synced")
    final_payload = json.loads(str(row["cache_sync_json"]))
    assert final_payload["physical_receipt"]["target_path"] == str(fixture.destination)


def test_linux_organization_interrupted_after_syscall_recovers_exact_destination(
    organization_fixture: _OrganizationFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = organization_fixture

    def interrupt_before_catalog_record(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("synthetic interruption after filesystem move")

    monkeypatch.setattr(
        organization_application,
        "_record_moved_path",
        interrupt_before_catalog_record,
    )
    with pytest.raises(KeyboardInterrupt, match="after filesystem move"):
        apply_document_organization(
            fixture.catalog,
            fixture.destination_root,
            mutation_guard=fixture.guard,
            max_actions=1,
        )
    monkeypatch.undo()

    assert not fixture.source.exists()
    assert fixture.destination.read_bytes() == b"synthetic Linux organization fixture"
    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        assert connection.execute("SELECT status FROM organization_plans").fetchone()[0] == "applying"

    replay = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )
    assert replay.applied == 1
    assert replay.cache_synced == 1
    assert fixture.destination.read_bytes() == b"synthetic Linux organization fixture"


def test_linux_organization_post_syscall_failure_is_recovery_required(
    organization_fixture: _OrganizationFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = organization_fixture

    real_fsync = organization_application.os.fsync

    def fail_directory_fsync(_descriptor: int) -> None:
        if not fixture.source.exists():
            raise OSError("synthetic post-syscall interruption")
        real_fsync(_descriptor)

    monkeypatch.setattr(organization_application.os, "fsync", fail_directory_fsync)
    result = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )

    assert result.applied == 0
    assert result.failed == 1
    assert not fixture.source.exists()
    assert fixture.destination.read_bytes() == b"synthetic Linux organization fixture"
    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        row = connection.execute("SELECT status FROM organization_plans").fetchone()
    assert row is not None and row[0] == "recovery_required"


def test_standalone_organization_apply_owns_lock_before_physical_effect(
    organization_fixture: _OrganizationFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = organization_fixture
    before_catalog = hashlib.sha256(fixture.catalog.read_bytes()).digest()
    initialized = False

    def unexpected_initialize(_path: Path) -> None:
        nonlocal initialized
        initialized = True
        raise AssertionError("catalog initialization must occur after framework lock")

    monkeypatch.setattr(
        organization_application,
        "initialize_document_catalog",
        unexpected_initialize,
    )
    with FrameworkRunLock(fixture.state / "framework.lock"):
        with pytest.raises(RuntimeError, match="another framework execution"):
            apply_document_organization(
                fixture.catalog,
                fixture.destination_root,
                mutation_guard=fixture.guard,
                max_actions=1,
            )
    assert initialized is False
    assert hashlib.sha256(fixture.catalog.read_bytes()).digest() == before_catalog
    assert fixture.source.exists()
    assert not fixture.destination.exists()


def test_standalone_apply_all_holds_one_lock_and_syncs_cache(
    organization_fixture: _OrganizationFixture,
) -> None:
    fixture = organization_fixture
    result = apply_all_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        batch_size=1,
    )
    assert result.applied == 1
    assert result.cache_synced == 1
    assert result.cache_pending == 0
    assert not fixture.source.exists()
    assert fixture.destination.exists()


def test_integrated_lock_held_path_does_not_relock(
    organization_fixture: _OrganizationFixture,
) -> None:
    fixture = organization_fixture
    with FrameworkRunLock(fixture.state / "framework.lock"):
        result = apply_document_organization(
            fixture.catalog,
            fixture.destination_root,
            mutation_guard=fixture.guard,
            max_actions=1,
            framework_lock_held=True,
        )
    assert result.applied == 1
    assert result.cache_synced == 1
    assert result.cache_pending == 0
