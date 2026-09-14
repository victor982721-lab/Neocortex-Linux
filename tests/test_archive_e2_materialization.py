from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from pathlib import Path

from neocortex.capabilities.formats.archive.materialization import (
    ArchiveMaterializationLimits,
    materialize_archive,
    scan_archive,
)
from neocortex.capabilities.formats.archive.units import classify_archive_bytes
from neocortex.capabilities.formats.archive.route import (
    ARCHIVE_MIME,
    ArchiveRoute,
    ArchiveRouteConfig,
)
from neocortex.capabilities.formats.archive.state import archive_database
from neocortex.deduplication import snapshot_path
from neocortex.safety.route_filters import CandidateSelection
from neocortex.runtime.control.cancellation import CancellationToken


def _zip(entries: list[tuple[str, bytes | str]]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return stream.getvalue()


def test_duplicate_names_keep_ordinal_offset_and_distinct_bytes(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("same.txt", b"first")
        archive.writestr("same.txt", b"second")

    original = source.read_bytes()
    manifest = materialize_archive(source, tmp_path / "out", apply=False)

    entries = [entry for entry in manifest.entries if entry.name == "same.txt"]
    assert len(entries) == 2
    assert {entry.ordinal for entry in entries} == {0, 1}
    assert len({entry.header_offset for entry in entries}) == 2
    assert len({entry.identity for entry in entries}) == 2
    assert len({entry.sha256 for entry in entries}) == 2
    assert source.read_bytes() == original


def test_storage_archive_nested_materialization_is_no_replace_and_replayable(tmp_path: Path) -> None:
    source = tmp_path / "nested.zip"
    source.write_bytes(_zip([("outer.txt", b"outer"), ("inner.zip", _zip([("deep.txt", b"deep")]))]))
    original = source.read_bytes()
    destination = tmp_path / "materialized"

    first = materialize_archive(source, destination, apply=True)
    before = (destination / "outer.txt").read_bytes()
    second = materialize_archive(source, destination, apply=True)

    assert first.status == second.status == "complete"
    assert first.container_normalized
    assert (destination / "inner.zip" / "deep.txt").read_bytes() == b"deep"
    assert before == b"outer"
    assert {output.status for output in second.outputs} >= {"reused", "skipped"}
    assert source.read_bytes() == original


def test_functional_office_zip_is_preserved_as_one_unit(tmp_path: Path) -> None:
    source = tmp_path / "report.zip"
    payload = _zip(
        [
            ("[Content_Types].xml", b"<Types />"),
            ("word/document.xml", b"<document>evidence</document>"),
        ]
    )
    source.write_bytes(payload)

    classification = classify_archive_bytes(payload)
    result = materialize_archive(source, tmp_path / "out", apply=True)

    assert classification.unit_kind == "office"
    assert classification.kind == "docx"
    assert result.normalization_disposition == "functional_unit_preserved"
    assert not result.container_normalized
    assert (tmp_path / "out" / "report.zip").read_bytes() == payload
    assert not (tmp_path / "out" / "word").exists()


def test_slip_and_bomb_are_partial_with_separate_bounded_evidence(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.zip"
    source.write_bytes(_zip([("../escape.txt", b"blocked"), ("safe.txt", b"kept")]))
    manifest = scan_archive(source)
    assert manifest.status == "partial"
    assert any(entry.error_code == "archive_unsafe_member_name" for entry in manifest.entries)
    assert any(entry.name == "safe.txt" and entry.status == "validated" for entry in manifest.entries)

    bomb = tmp_path / "bomb.zip"
    bomb.write_bytes(_zip([("bomb.txt", b"A" * 200_000)]))
    bounded = scan_archive(
        bomb,
        limits=ArchiveMaterializationLimits(
            max_member_bytes=1_000_000,
            max_total_uncompressed_bytes=1_000_000,
            max_compression_ratio=5,
        ),
    )
    assert bounded.status == "budget"
    assert bounded.entries[0].status == "budget"


def test_empty_zip_is_complete_and_no_apply_does_not_create_destination(tmp_path: Path) -> None:
    source = tmp_path / "empty.zip"
    source.write_bytes(_zip([]))
    destination = tmp_path / "out"

    manifest = materialize_archive(source, destination, apply=False)

    assert manifest.status == "complete"
    assert manifest.container_normalized is False
    assert not destination.exists()


class _OneArchiveCandidate:
    def __init__(self, source: Path) -> None:
        self.snapshot = snapshot_path(source)

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


def test_archive_route_materializes_only_when_explicitly_enabled(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "apply.zip"
    source.write_bytes(_zip([("evidence.txt", b"evidence")]))
    state = tmp_path / "state" / "archive.sqlite3"
    materialization_root = tmp_path / "state" / "materialized"
    calls: list[tuple[Path, Path, bool]] = []

    def fake_materialize(source_path, destination, *, apply, limits):
        del limits
        calls.append((Path(source_path), Path(destination), apply))
        return SimpleNamespace(
            status="complete",
            manifest_digest="manifest-digest",
            outputs=(),
            container_normalized=True,
            classification=SimpleNamespace(preserve_as_unit=False, kind="storage_archive"),
        )

    monkeypatch.setattr(
        "neocortex.capabilities.formats.archive.route.materialize_archive",
        fake_materialize,
    )
    route = ArchiveRoute(
        ArchiveRouteConfig(
            state,
            ocr_mode="never",
            materialize_on_apply=True,
            materialization_directory=materialization_root,
        ),
        _OneArchiveCandidate(source),  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    )

    summary = route.run()

    assert len(calls) == 1
    assert calls[0][0] == source and calls[0][2] is True
    assert calls[0][1].parent == materialization_root
    assert summary.materialization_manifest_digest == "manifest-digest"
    assert summary.materialization_pending == 0
    with archive_database(state, readonly=True) as connection:
        reason = connection.execute(
            "SELECT reason_code FROM archive_issues "
            "WHERE reason_code='archive_materialization_complete'"
        ).fetchone()
    assert reason is not None
