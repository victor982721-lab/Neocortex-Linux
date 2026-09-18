"""Office cancellation remains effective during XML reads and finalization."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.office.route import (
    ODT_MIME,
    PPTX_MIME,
    XLSX_MIME,
    OfficeRoute,
    OfficeRouteConfig,
    search_office_state,
)
from neocortex.capabilities.formats.office.state import office_database
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from tests.test_office_route import FakeFrameworkRouteState


TEST_CAPABILITIES = ("base",)


def _office_source(path: Path, kind: str) -> tuple[str, str]:
    text = "candidate evidence " * 3_000
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if kind == "odt":
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            member = "content.xml"
            payload = f"<document><p>{text}</p></document>"
            mime = ODT_MIME
        elif kind == "pptx":
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("ppt/presentation.xml", "<presentation/>")
            member = "ppt/slides/slide1.xml"
            payload = f"<slide><t>{text}</t></slide>"
            mime = PPTX_MIME
        else:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
            if kind == "xlsx-comments":
                member = "xl/comments1.xml"
                payload = f"<comments><t>{text}</t></comments>"
            else:
                member = "xl/sharedStrings.xml"
                payload = f"<sst><si><t>{text}</t></si></sst>"
            mime = XLSX_MIME
        archive.writestr(member, payload)
    return mime, member


def _route(tmp_path: Path, source: Path, mime: str, token: CancellationToken) -> OfficeRoute:
    return OfficeRoute(
        OfficeRouteConfig(
            tmp_path / "state" / "office.sqlite3",
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        FakeFrameworkRouteState({mime: (snapshot_path(source),)}),  # type: ignore[arg-type]
        1,
        cancellation=token,
    )


@pytest.mark.parametrize("kind", ("odt", "pptx", "xlsx-comments", "xlsx-shared"))
def test_office_stream_extraction_keeps_searchable_text(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "input.office"
    mime, _member = _office_source(source, kind)
    route = _route(tmp_path, source, mime, CancellationToken())

    summary = route.run()

    assert (summary.extracted, summary.errors) == (1, 0)
    assert search_office_state(route.config.state_path, "candidate")[0]["path"] == str(source)


@pytest.mark.parametrize("kind", ("odt", "pptx", "xlsx-comments", "xlsx-shared"))
@pytest.mark.parametrize("cancel_at", ("first_read", "eof"))
def test_office_cancellation_during_member_read_prevents_publication(
    tmp_path: Path, monkeypatch, kind: str, cancel_at: str,
) -> None:
    source = tmp_path / "input.office"
    mime, member = _office_source(source, kind)
    token = CancellationToken()
    route = _route(tmp_path, source, mime, token)
    original_read = zipfile.ZipExtFile.read
    observed_streams: list[zipfile.ZipExtFile] = []
    reads_after_cancel = 0

    def cancelling_read(stream, size=-1):
        nonlocal reads_after_cancel
        if stream.name == member:
            observed_streams.append(stream)
            reads_after_cancel += int(token.is_cancelled)
        payload = original_read(stream, size)
        if stream.name == member and (cancel_at == "first_read" or not payload):
            token.cancel()
        return payload

    monkeypatch.setattr(zipfile.ZipExtFile, "read", cancelling_read)
    with pytest.raises(CancellationRequested):
        route.run()

    assert token.is_cancelled
    assert observed_streams and all(stream.closed for stream in observed_streams)
    assert reads_after_cancel == 0
    with office_database(route.config.state_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0] == 0


def test_office_cancellation_on_archive_close_prevents_last_document_publication(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "input.odt"
    mime, _member = _office_source(source, "odt")
    token = CancellationToken()
    route = _route(tmp_path, source, mime, token)
    original_close = zipfile.ZipFile.close

    def cancelling_close(archive):
        cancel = archive.fp is not None and archive.filename == str(source)
        original_close(archive)
        if cancel:
            token.cancel()

    monkeypatch.setattr(zipfile.ZipFile, "close", cancelling_close)
    with pytest.raises(CancellationRequested):
        route.run()

    assert token.is_cancelled
    with office_database(route.config.state_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
