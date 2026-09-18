from __future__ import annotations

import argparse
import io
import json
import sqlite3
from contextlib import closing, redirect_stdout
from pathlib import Path

import pytest

from neocortex.api.cli.cli_curation import run_curation_preview
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.curation import build_curation_preview
from neocortex.curation.preview import (
    CurationStateError,
    build_curation_plan_page,
)
from neocortex.deduplication import (
    DedupIndex,
    DedupPlanner,
    InventoryCheckpoint,
)
from neocortex.documents.document_catalog import initialize_document_catalog


def _build_state(
    state: Path,
    corpus: Path,
    *,
    exact_compare: bool = False,
) -> int:
    corpus.mkdir()
    (corpus / "keep.txt").write_bytes(b"same content")
    (corpus / "duplicate.txt").write_bytes(b"same content")
    (corpus / "empty.txt").write_bytes(b"")
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(
                str(corpus),
                summary.scan_id,
                None,
                None,
                None,
                True,
            )
        )
        DedupPlanner(index, partial_threshold=0).plan(
            summary.scan_id,
            exact_compare=exact_compare,
        )

    catalog = state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(
            """INSERT INTO catalog_runs(
            catalog_run_id,source_kind,mode,status,started_ns,completed_ns,summary_json)
            VALUES (1,'all','plan','completed',1,2,'{}')"""
        )
        connection.execute(
            """INSERT INTO organization_plans(
            catalog_run_id,source_kind,file_key,source_path,destination_path,
            organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
            classifier_signature,primary_kind,confidence,status,reason,
            evidence_json,planned_ns)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                1,
                "pdf",
                "pdf:1:2",
                str(corpus / "keep.txt"),
                str(state / "organized" / "manual.pdf"),
                str(state / "organized"),
                "1",
                "2",
                12,
                1,
                -1,
                "technical-document-classifier-v-test",
                "manual_equipo",
                0.95,
                "planned",
                "classification_above_threshold",
                '{"uncertainty":"baja","topics":["transformadores"]}',
                1,
            ),
        )
    return summary.scan_id


def _state_bytes(state: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in state.iterdir()
        if path.is_file()
    }


def test_preview_composes_durable_sources_without_writing_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus")
    before = _state_bytes(state)

    preview = build_curation_preview(state, limit=5)

    assert preview.coverage == "complete"
    assert preview.missing_owners == ()
    assert preview.inventory_files == 3
    assert preview.duplicate_groups == 1
    assert preview.duplicate_members == 1
    assert preview.reclaimable_bytes == 12
    assert preview.organization_plans == 1
    assert preview.empty_files == 1
    assert [item.kind for item in preview.items] == [
        "duplicate_group",
        "organization_plan",
        "empty_file",
    ]
    assert all(item.status == "review" for item in preview.items)
    assert preview.items[0].reason == "duplicate_content_candidate"
    assert preview.items[0].evidence["verification_mode"] == "fast"
    assert preview.items[1].reason == "classification_above_threshold"
    assert preview.items[2].reason == "empty_file_requires_human_review"
    assert preview.preview_fingerprint.startswith("sha256:")
    assert preview.preview_fingerprint == build_curation_preview(state, limit=5).preview_fingerprint
    assert _state_bytes(state) == before


def test_preview_limit_is_bounded_and_marks_truncation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus")

    preview = build_curation_preview(state, limit=2)

    assert len(preview.items) == 2
    assert preview.items_total == 3
    assert preview.items_truncated is True


def test_plan_digest_is_independent_of_page_size(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus")

    one = build_curation_plan_page(state, 1)
    all_items = build_curation_plan_page(state, 10)

    assert len(one.items) == 1
    assert len(all_items.items) == 3
    assert one.plan_digest == all_items.plan_digest
    assert one.snapshot_id == all_items.snapshot_id
    assert one.next_cursor is not None
    assert all_items.next_cursor is None


def test_plan_pages_have_no_gaps_or_duplicates(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus")
    expected = [item.item_id for item in build_curation_plan_page(state, 100).items]

    observed: list[str] = []
    cursors: set[str] = set()
    cursor = None
    while True:
        page = build_curation_plan_page(state, 1, cursor)
        assert page.cursor == cursor
        observed.extend(item.item_id for item in page.items)
        if page.next_cursor is None:
            break
        assert page.next_cursor not in cursors
        cursors.add(page.next_cursor)
        cursor = page.next_cursor

    assert observed == expected
    assert len(observed) == len(set(observed))


def test_plan_page_rejects_invalid_cursor_and_changed_snapshot(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus")

    with pytest.raises(CurationStateError, match="cursor is invalid"):
        build_curation_plan_page(state, 1, "not-a-cursor")

    first = build_curation_plan_page(state, 1)
    assert first.next_cursor is not None
    with sqlite3.connect(state / "document_catalog.sqlite3") as connection:
        connection.execute(
            "UPDATE organization_plans SET reason='changed_after_first_page'"
        )

    with pytest.raises(CurationStateError, match="snapshot changed"):
        build_curation_plan_page(state, 1, first.next_cursor)


def test_partial_duplicate_plan_is_not_visible(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    scan_id = _build_state(state, tmp_path / "corpus")
    with sqlite3.connect(state / "dedup.sqlite3") as connection:
        connection.execute(
            "DELETE FROM duplicate_plan_summaries WHERE scan_id=?",
            (scan_id,),
        )

    page = build_curation_plan_page(state, 10)

    assert page.coverage == "partial"
    assert page.duplicate_groups == 0
    assert page.duplicate_members == 0
    assert page.reclaimable_bytes == 0
    assert all(item.kind != "duplicate_group" for item in page.items)
    assert page.items_total == 2


def test_complete_unpublished_scan_is_not_selected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    published_scan_id = _build_state(state, corpus)
    (corpus / "new.txt").write_text("new", encoding="utf-8")
    with DedupIndex(state / "dedup.sqlite3") as index:
        unpublished = index.scan(corpus)
        DedupPlanner(index).plan(unpublished.scan_id, exact_compare=False)

    page = build_curation_plan_page(state, 10)

    assert unpublished.scan_id != published_scan_id
    assert page.scan_id == published_scan_id
    assert page.inventory_files == 3


def test_fast_plan_is_neutral_and_never_claims_exact_verification(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus", exact_compare=False)

    page = build_curation_plan_page(state, 10)
    duplicate = next(item for item in page.items if item.kind == "duplicate_group")

    assert "exact" not in duplicate.reason
    assert duplicate.evidence["verification_mode"] == "fast"
    serialized = json.dumps(duplicate.to_dict(), sort_keys=True).casefold()
    assert "byte-for-byte" not in serialized
    assert "bytewise" not in serialized


def test_organization_page_selects_one_completed_run_and_inventory_root(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    _build_state(state, corpus)
    catalog = state / "document_catalog.sqlite3"
    selected_root = state / "organized-v2"
    foreign_source = tmp_path / "other-corpus" / "outside.txt"
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            """INSERT INTO catalog_runs(
            catalog_run_id,source_kind,mode,status,started_ns,completed_ns,summary_json)
            VALUES (2,'all','plan','completed',3,4,'{}')"""
        )
        for source_path, destination_path in (
            (corpus / "keep.txt", selected_root / "keep.txt"),
            (foreign_source, selected_root / "outside.txt"),
        ):
            connection.execute(
                """INSERT INTO organization_plans(
                catalog_run_id,source_kind,file_key,source_path,destination_path,
                organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
                classifier_signature,primary_kind,confidence,status,reason,
                evidence_json,planned_ns)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    2,
                    "text",
                    f"text:{source_path.name}",
                    str(source_path),
                    str(destination_path),
                    str(selected_root),
                    "1",
                    "2",
                    4,
                    1,
                    -1,
                    "test-classifier-v2",
                    "text",
                    0.9,
                    "planned",
                    "newer_run",
                    "{}",
                    3,
                ),
            )
        connection.execute(
            """INSERT INTO catalog_runs(
            catalog_run_id,source_kind,mode,status,started_ns,completed_ns,summary_json)
            VALUES (3,'all','plan','failed',5,6,'{}')"""
        )
        connection.execute(
            """INSERT INTO organization_plans(
            catalog_run_id,source_kind,file_key,source_path,destination_path,
            organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
            classifier_signature,primary_kind,confidence,status,reason,
            evidence_json,planned_ns)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                3,
                "text",
                "text:failed",
                str(corpus / "empty.txt"),
                str(state / "organized-v3" / "empty.txt"),
                str(state / "organized-v3"),
                "1",
                "2",
                0,
                1,
                -1,
                "test-classifier-v3",
                "text",
                0.9,
                "planned",
                "failed_run",
                "{}",
                5,
            ),
        )

    page = build_curation_plan_page(state, 10)
    organization_items = [item for item in page.items if item.kind == "organization_plan"]

    assert page.organization_plans == 1
    assert len(organization_items) == 1
    assert organization_items[0].source_path == str(corpus / "keep.txt")
    assert organization_items[0].reason == "newer_run"
    assert organization_items[0].evidence["catalog_run_id"] == 2


def test_curation_refuses_a_symlinked_sqlite_owner(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus")
    owner = state / "dedup.sqlite3"
    external_owner = tmp_path / "external-dedup.sqlite3"
    owner.rename(external_owner)
    owner.symlink_to(external_owner)

    with pytest.raises(CurationStateError, match="symlink"):
        build_curation_plan_page(state, 10)

    assert external_owner.is_file()


def test_preview_reports_unavailable_without_creating_missing_state(tmp_path: Path) -> None:
    state = tmp_path / "missing-state"

    preview = build_curation_preview(state, limit=5)

    assert preview.coverage == "unavailable"
    assert preview.missing_owners == ("dedup.sqlite3", "document_catalog.sqlite3")
    assert not state.exists()


def test_cli_json_is_deterministic_and_read_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus")
    before = _state_bytes(state)
    args = argparse.Namespace(
        state_directory=state,
        curation_preview=5,
        curation_json=True,
    )

    output = io.StringIO()
    with redirect_stdout(output):
        assert run_curation_preview(args) == 0
    payload = json.loads(output.getvalue())

    assert payload["kind"] == "curation-preview"
    assert payload["coverage"] == "complete"
    assert payload["missing_owners"] == []
    assert payload["items_total"] == 3
    assert payload["preview_fingerprint"].startswith("sha256:")
    assert _state_bytes(state) == before


def test_cli_reports_missing_state_without_initializing(tmp_path: Path) -> None:
    state = tmp_path / "missing-state"
    args = argparse.Namespace(
        state_directory=state,
        curation_preview=5,
        curation_json=False,
    )
    output = io.StringIO()

    with redirect_stdout(output):
        exit_code = run_curation_preview(args)

    assert exit_code == 2
    assert "coverage=unavailable" in output.getvalue()
    assert not state.exists()


def test_preview_reports_partial_coverage_when_catalog_is_missing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _build_state(state, tmp_path / "corpus")
    (state / "document_catalog.sqlite3").unlink()

    preview = build_curation_preview(state, limit=5)

    assert preview.coverage == "partial"
    assert preview.missing_owners == ("document_catalog.sqlite3",)
    assert preview.duplicate_groups == 1
    assert preview.organization_plans == 0
    assert preview.empty_files == 1


@pytest.mark.parametrize(
    ("argv", "message"),
    (
        (("--curation-json",), "--curation-json requires --curation-preview"),
        (("--curation-preview", "0"), "--curation-preview must be between 1 and 10000"),
        (
            ("--curation-preview", "2", "--apply"),
            "--curation-preview is read-only and cannot be combined with --apply",
        ),
    ),
)
def test_curation_cli_validation(argv: tuple[str, ...], message: str) -> None:
    args = build_parser().parse_args(argv)
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)
