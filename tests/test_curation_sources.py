from __future__ import annotations

from types import SimpleNamespace

import pytest

from neocortex.documents.curation_sources import (
    CurationSource,
    CurationSourceFenceError,
    CurationSourceFences,
    CurationSourcePage,
    DerivedContent,
    PhysicalIdentity,
    iter_catalog_route_source_pages,
    iter_curation_source_pages,
    iter_semantic_record_pages,
)


def _source(number: int, *, route: str = "route-1", catalog: str = "catalog-1") -> CurationSource:
    return CurationSource(
        source_kind="text",
        file_key=f"file-{number}",
        path=f"/derived/{number}.txt",
        physical_identity=PhysicalIdentity("vol", f"inode-{number}"),
        content_signature=f"content-{number}",
        sections=(DerivedContent("text", "body", f"Contenido {number}"),),
        route_fence=route,
        catalog_fence=catalog,
    )


def test_pages_are_bounded_and_exact_multiple_has_no_phantom_page() -> None:
    pages = list(iter_curation_source_pages([_source(1), _source(2)], page_size=1))

    assert len(pages) == 2
    assert all(isinstance(page, CurationSourcePage) for page in pages)
    assert pages[0].next_page == 1
    assert pages[1].next_page is None
    assert pages[1].complete


def test_page_fences_are_checked_before_yield() -> None:
    expected = CurationSourceFences("route-1", "catalog-1")
    with pytest.raises(CurationSourceFenceError):
        tuple(
            iter_curation_source_pages(
                [_source(1, route="changed")],
                expected_fences=expected,
            )
        )


def test_fence_change_never_mixes_two_owner_revisions() -> None:
    pages = list(
        iter_curation_source_pages(
            [_source(1), _source(2, route="route-2")],
            page_size=10,
        )
    )

    assert [page.fences.route_owner for page in pages] == ["route-1", "route-2"]
    assert [page.next_page for page in pages] == [1, None]


def test_semantic_route_adapter_groups_sections_without_opening_paths() -> None:
    item = SimpleNamespace(
        source_kind="pdf",
        source_identity="file-key",
        path="/original/report.pdf",
        source_revision={"volume_id": "v", "file_id": "i", "birthtime_ns": 2},
        provenance={"source_status": "done"},
        fingerprint=SimpleNamespace(xxh3_128="abc123", byte_count=12),
    )
    records = [
        SimpleNamespace(item=item, section=SimpleNamespace(section_kind="pdf_page", section_id="1", text="Página uno", provenance={})),
        SimpleNamespace(item=item, section=SimpleNamespace(section_kind="pdf_page", section_id="2", text="Página dos", provenance={})),
    ]

    pages = list(iter_semantic_record_pages(records, page_size=1, route_fence="route", catalog_fence="catalog"))

    assert len(pages) == 1
    source = pages[0].items[0]
    assert len(source.sections) == 2
    assert source.content_signature == "route-descriptor:abc123:12"
    assert source.path == "/original/report.pdf"


def test_catalog_route_adapter_uses_existing_bounded_reader_and_one_connection(monkeypatch) -> None:
    connection = object()
    calls: list[object] = []

    def fake_load(conn, document, *, max_text_chars, cancellation=None):
        calls.append(conn)
        assert max_text_chars == 32
        return "Texto derivado con UTF-8: áéñ"

    monkeypatch.setattr("neocortex.documents.document_catalog_text._load_leading_text", fake_load)
    document = SimpleNamespace(
        source_kind="audio",
        file_key="audio-key",
        path="/original/interview.wav",
        volume_id="v",
        file_id="i",
        birthtime_ns=3,
        source_status="complete",
        text_fingerprint="transcript-v1",
        title="Entrevista",
        author="Operador",
        metadata="{}",
    )

    pages = list(
        iter_catalog_route_source_pages(
            [document], connection, page_size=1, max_text_chars=32,
            route_fence="route-current", catalog_fence="catalog-current",
        )
    )
    assert calls == [connection]
    source = pages[0].items[0]
    assert source.sections[0].text.startswith("Texto derivado")
    assert source.content_signature == "transcript-v1"
    assert source.fences == CurationSourceFences("route-current", "catalog-current")


def test_catalog_match_can_exclude_stale_or_missing_projection(monkeypatch) -> None:
    monkeypatch.setattr(
        "neocortex.documents.document_catalog_text._load_leading_text",
        lambda *args, **kwargs: "derived",
    )
    document = SimpleNamespace(
        source_kind="xlsx", file_key="k", path="/file.xlsx", volume_id="v", file_id="i",
        birthtime_ns=1, text_fingerprint="content", title="", author="", metadata="",
    )
    pages = list(
        iter_catalog_route_source_pages(
            [document], object(), metadata_lookup=lambda source: None,
            require_catalog_match=True,
        )
    )
    assert pages == []
