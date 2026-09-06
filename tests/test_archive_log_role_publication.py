"""Published catalog roles for real text/Archive producer fixtures."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

import neocortex.documents.document_taxonomy as taxonomy_module
from neocortex.capabilities.formats.archive.route import ArchiveRoute, ArchiveRouteConfig
from neocortex.capabilities.formats.archive.state import list_archive_members
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.deduplication import snapshot_path
from neocortex.documents.document_catalog import (
    document_catalog_database,
    list_catalog_documents,
    update_document_catalog,
)
from neocortex.documents.document_taxonomy import document_classifier_signature, load_taxonomy
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.route_filters import CandidateSelection


TEST_CAPABILITIES = ("base",)
STRUCTURED_LOG = (
    "2026-09-06T10:00:00 INFO command=inspect ANDRITZ CFE reporte de anomalías\n"
    "2026-09-06T10:00:01 ERROR exit_code=1 stderr=incidencia de transformador\n"
    "2026-09-06T10:00:02 INFO stdout=consulta sobre reporte de anomalías ANDRITZ\n"
)


class _FixtureFramework:
    def __init__(self, route_name: str, mime: str, sources: tuple[Path, ...]):
        self.route_name = route_name
        self.mime = mime
        self.candidates = tuple(snapshot_path(source) for source in sources)

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert route_name == self.route_name
        candidates = self.candidates if mime == self.mime else ()
        return len(candidates), sum(
            max_file_bytes is None or item.size <= max_file_bytes for item in candidates
        )

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection: CandidateSelection,
    ):
        assert route_name == self.route_name
        if mime == self.mime:
            yield from self.candidates


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return stream.getvalue()


def _producers(state: Path, physical: tuple[Path, ...], archives: tuple[Path, ...]):
    text = TextRoute(
        TextRouteConfig(state / "text.sqlite3"),
        _FixtureFramework("text", "text/plain", physical),  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    )
    archive = ArchiveRoute(
        ArchiveRouteConfig(state / "archive.sqlite3", ocr_mode="never"),
        _FixtureFramework("archive", "application/zip", archives),  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    )
    return text, archive


def _source_hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _assert_current_unknown_issuers(catalog: Path, paths: set[str]) -> None:
    # The public list API exposes document roles but not issuer evidence or
    # classifier signatures; inspect only those extra published fields here.
    signature = document_classifier_signature(load_taxonomy())
    with document_catalog_database(catalog, readonly=True) as connection:
        rows = connection.execute(
            "SELECT path,classifier_signature,classification_json FROM documents WHERE active=1"
        ).fetchall()
    selected = [row for row in rows if row["path"] in paths]
    assert {row["path"] for row in selected} == paths
    for row in selected:
        assert row["classifier_signature"] == signature
        classification = json.loads(row["classification_json"])
        assert classification["issuer_status"] == "unknown"
        assert not any(item["role"] == "issuer" for item in classification["entity_roles"])


def test_structured_log_role_is_published_for_physical_and_nested_archive_with_scope_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "A"
    neighbor = tmp_path / "A-extra"
    root.mkdir()
    neighbor.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    payload = STRUCTURED_LOG.encode()
    physical = root / "capture.log"
    excluded_physical = neighbor / "capture.log"
    physical.write_bytes(payload)
    excluded_physical.write_bytes(payload)
    nested = _zip_bytes({"nested/session.log": payload})
    outer = root / "capture.zip"
    outer.write_bytes(_zip_bytes({"session.log": payload, "inner.zip": nested}))
    excluded_archive = neighbor / "capture.zip"
    excluded_archive.write_bytes(_zip_bytes({"session.log": payload}))
    originals = (physical, excluded_physical, outer, excluded_archive)
    before_hashes = _source_hashes(originals)
    text_route, archive_route = _producers(
        state, (physical, excluded_physical), (outer, excluded_archive)
    )
    assert text_route.run().errors == 0
    assert archive_route.run().errors == 0
    members_before = list_archive_members(state / "archive.sqlite3")
    catalog = state / "document_catalog.sqlite3"
    expected_paths = {
        str(physical),
        f"{outer}!/session.log",
        f"{outer}!/inner.zip!/nested/session.log",
    }
    current_signature = document_classifier_signature(load_taxonomy())
    # Build the first publication through the real publisher with its legacy
    # classifier signature, not by altering stored rows to fake a cache hit.
    with monkeypatch.context() as patch:
        patch.setattr(taxonomy_module, "CLASSIFIER_VERSION", "technical-document-classifier-v15")
        old_signature = document_classifier_signature(load_taxonomy())
        published = update_document_catalog(state, source_root=root)
        _assert_current_unknown_issuers(catalog, expected_paths)
    assert current_signature != old_signature
    summaries = {item.source_kind: item for item in published}
    assert summaries["text"].classified == 1
    assert summaries["archive"].classified == 3
    assert summaries["text"].errors == summaries["archive"].errors == 0

    refreshed = {
        item.source_kind: item for item in update_document_catalog(state, source_root=root)
    }
    assert refreshed["text"].classified == 1 and refreshed["text"].cache_hits == 0
    assert refreshed["archive"].classified == 3 and refreshed["archive"].cache_hits == 0
    logs = list_catalog_documents(catalog, limit=20, primary_kind="registro_log")
    assert {item.path for item in logs} == expected_paths
    assert {item.source_kind for item in logs} == {"text", "archive"}
    all_published = list_catalog_documents(catalog, limit=20)
    assert len(all_published) == 4  # the nested ZIP itself remains a separate archive record
    assert not any(item.path.startswith(str(neighbor) + "/") for item in all_published)
    _assert_current_unknown_issuers(catalog, expected_paths)

    assert text_route.run().cache_hits == 2
    assert archive_route.run().cache_hits == 2
    replay = {item.source_kind: item for item in update_document_catalog(state, source_root=root)}
    assert replay["text"].cache_hits == 1 and replay["text"].classified == 0
    assert replay["archive"].cache_hits == 3 and replay["archive"].classified == 0
    assert list_catalog_documents(catalog, limit=20, primary_kind="registro_log") == logs
    assert list_archive_members(state / "archive.sqlite3") == members_before
    assert _source_hashes(originals) == before_hashes
    with zipfile.ZipFile(outer) as archive:
        assert archive.read("session.log") == payload
        inner = archive.read("inner.zip")
        assert hashlib.sha256(inner).digest() == hashlib.sha256(nested).digest()
    with zipfile.ZipFile(io.BytesIO(inner)) as archive:
        assert archive.read("nested/session.log") == payload
    _assert_current_unknown_issuers(catalog, expected_paths)


@pytest.mark.parametrize(
    "heading", ("Reporte de anomalías de transformadores", "Bitácora de actividades")
)
def test_explicit_document_heading_prevails_over_structured_log_lines_in_publication(
    tmp_path: Path,
    heading: str,
) -> None:
    root = tmp_path / "A"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    payload = (heading + "\n" + STRUCTURED_LOG).encode()
    physical = root / "transcript.log"
    physical.write_bytes(payload)
    archive = root / "transcript.zip"
    archive.write_bytes(_zip_bytes({"transcript.log": payload}))
    originals = _source_hashes((physical, archive))
    text_route, archive_route = _producers(state, (physical,), (archive,))
    assert text_route.run().errors == archive_route.run().errors == 0
    summaries = {
        item.source_kind: item for item in update_document_catalog(state, source_root=root)
    }
    assert summaries["text"].errors == summaries["archive"].errors == 0
    catalog = state / "document_catalog.sqlite3"
    assert not list_catalog_documents(catalog, limit=10, primary_kind="registro_log")
    documents = list_catalog_documents(catalog, limit=10)
    assert {item.path for item in documents} == {str(physical), f"{archive}!/transcript.log"}
    assert all(item.primary_kind != "registro_log" for item in documents)
    if heading.startswith("Reporte"):
        assert all(item.primary_kind == "reporte_anomalias" for item in documents)
    else:
        assert all(item.primary_kind == "registro_bitacora" for item in documents)
    assert _source_hashes((physical, archive)) == originals
