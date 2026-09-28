"""Independent C23 acceptance probes; intentionally synthetic and read-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.documents.document_catalog import document_catalog_database
from neocortex.documents.document_organization import (
    apply_document_organization,
    capture_organization_input_scope,
    plan_document_organization,
)
from neocortex.documents.document_cache_sync import (
    DocumentMoveTransition,
    _synchronize_dedup_owner_batch,
)
from neocortex.deduplication import snapshot_path

pytest_plugins = ("tests.test_linux_organization_application",)


TEST_CAPABILITIES = ("base", "inference")


def test_catalog_resource_binding_must_follow_applied_path(organization_fixture) -> None:
    fixture = organization_fixture
    result = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )
    assert result.applied == 1
    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT path,resource_binding_json FROM documents WHERE active=1"
        ).fetchone()
    assert row is not None
    assert Path(str(row["path"])) == fixture.destination
    # Current owner path and both binding locators must move atomically.
    binding = json.loads(str(row["resource_binding_json"]))
    assert binding["physical_anchor_path"] == str(fixture.destination)
    assert binding["resource_ref"]["current_path"] == str(fixture.destination)


def test_rebound_catalog_owner_survives_new_scope_plan_and_replay(
    organization_fixture,
) -> None:
    fixture = organization_fixture
    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        historical = connection.execute(
            """SELECT path,resource_binding_json FROM catalog_generation_documents
            ORDER BY generation_id DESC LIMIT 1"""
        ).fetchone()
    assert historical is not None
    historical_path = str(historical["path"])
    historical_binding = json.loads(str(historical["resource_binding_json"]))

    first = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )
    assert (first.selected, first.applied, first.remaining) == (1, 1, 0)

    # A fresh scope/plan observes the current owner, while the published
    # generation remains immutable historical evidence of the source locator.
    next_scope = capture_organization_input_scope(fixture.catalog, fixture.corpus)
    next_plan = plan_document_organization(
        fixture.catalog,
        fixture.destination_root,
        source_scope=next_scope,
    )
    assert next_plan.planned == 0
    assert next_plan.already_organized == 1
    replay = apply_document_organization(
        fixture.catalog,
        fixture.destination_root,
        mutation_guard=fixture.guard,
        max_actions=1,
    )
    assert (replay.selected, replay.applied, replay.remaining) == (0, 0, 0)

    with document_catalog_database(fixture.catalog, readonly=True) as connection:
        current = connection.execute(
            "SELECT path,resource_binding_json FROM documents WHERE active=1"
        ).fetchone()
        preserved = connection.execute(
            """SELECT path,resource_binding_json FROM catalog_generation_documents
            ORDER BY generation_id DESC LIMIT 1"""
        ).fetchone()
    assert current is not None and Path(str(current["path"])) == fixture.destination
    current_binding = json.loads(str(current["resource_binding_json"]))
    assert current_binding["physical_anchor_path"] == str(fixture.destination)
    assert current_binding["resource_ref"]["current_path"] == str(fixture.destination)
    assert current_binding["physical_identity"] == historical_binding["physical_identity"]
    assert current_binding["physical_anchor_revision"] == historical_binding[
        "physical_anchor_revision"
    ]
    assert preserved is not None
    assert str(preserved["path"]) == historical_path
    assert json.loads(str(preserved["resource_binding_json"])) == historical_binding


def test_dedup_batch_rebinding_uses_index_owner_not_loop_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("synthetic", encoding="utf-8")
    old.rename(new)
    observed = snapshot_path(new)
    database = tmp_path / "dedup.sqlite3"
    database.write_bytes(b"fixture-owner")

    class _Cursor:
        def fetchone(self):
            return None

    class _Connection:
        def execute(self, *_args: object, **_kwargs: object):
            return _Cursor()

    class _Index:
        def __init__(self, _path: Path):
            self._connection = _Connection()

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def current_scan_for_path(self, _path: str, **_kwargs: object) -> int:
            return 1

        def apply_reconciliation(self, *_args: object, **_kwargs: object) -> None:
            return None

    import neocortex.documents.document_cache_sync as sync

    monkeypatch.setattr(sync, "DedupIndex", _Index)
    transition = DocumentMoveTransition(
        "docx", "fixture-key", str(old), str(new), str(observed.volume_id), str(observed.file_id)
    )
    result = _synchronize_dedup_owner_batch(database, (transition,), verify_only=False)
    assert result.status == "synced"
