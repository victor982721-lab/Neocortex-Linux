from __future__ import annotations

import argparse
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from neocortex.api.cli.cli_curation import run_curation_preview
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.curation import CurationStateError, build_curation_preview
from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.documents.document_catalog import initialize_document_catalog


def _build_state(state: Path, corpus: Path) -> None:
    corpus.mkdir()
    (corpus / "keep.txt").write_bytes(b"same content")
    (corpus / "duplicate.txt").write_bytes(b"same content")
    (corpus / "empty.txt").write_bytes(b"")
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        DedupPlanner(index, partial_threshold=0).plan(summary.scan_id)

    catalog = state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    with sqlite3.connect(catalog) as connection:
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
    assert preview.items[0].reason == "exact_duplicate_content"
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


def test_preview_rejects_missing_state_without_creating_it(tmp_path: Path) -> None:
    state = tmp_path / "missing-state"

    with pytest.raises(CurationStateError, match="dedup state database does not exist"):
        build_curation_preview(state, limit=5)

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
    assert "ERROR curation-preview CurationStateError" in output.getvalue()
    assert not state.exists()


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
