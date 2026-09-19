"""Archive member admission keeps mixed ZIPs out of Code/Text/Semantic."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive.route import (
    ARCHIVE_MIME,
    ArchiveMemberAdmissionContext,
    ArchiveRoute,
    ArchiveRouteConfig,
)
from neocortex.capabilities.formats.archive.state import (
    archive_database,
    list_archive_members,
    search_archive_state,
)
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.safety.route_filters import CandidateSelection


class _Framework:
    def __init__(self, source: Path) -> None:
        self.snapshot = snapshot_path(source)

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        eligible = max_file_bytes is None or self.snapshot.size <= max_file_bytes
        return 1, int(eligible)

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


def _zip(entries: dict[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


def _docx() -> bytes:
    return _zip(
        {
            "[Content_Types].xml": "<Types/>",
            "word/document.xml": (
                '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>'
                "OOXML evidencia segura"
                "</w:t></w:r></w:p></w:body></w:document>"
            ),
        }
    )


def _route(
    state: Path,
    source: Path,
    *,
    callback=None,
    signature: str = "archive-member-policy-test-v1",
    cancellation: CancellationToken | None = None,
) -> ArchiveRoute:
    return ArchiveRoute(
        ArchiveRouteConfig(
            state_path=state,
            ocr_mode="never",
            member_admission=callback,
            member_admission_signature=signature,
        ),
        _Framework(source),  # type: ignore[arg-type]
        1,
        cancellation=cancellation or CancellationToken(),
    )


def test_mixed_archive_admission_preserves_documents_and_denies_foreign_code_and_secrets(
    tmp_path: Path,
) -> None:
    fitz = pytest.importorskip("fitz")
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "PDF evidencia técnica segura")
    pdf_bytes = pdf.tobytes()
    pdf.close()

    source = tmp_path / "mixed.zip"
    source.write_bytes(
        _zip(
            {
                "foreign/module.py": "SECRET_SOURCE = 'no'\n",
                "foreign/module.js": "const secretSource = true;\n",
                "credentials.json": '{"token":"do-not-persist"}\n',
                "docs/evidence.json": '{"field":"document evidence"}\n',
                "docs/evidence.csv": "field\ndeliverable evidence\n",
                "docs/evidence.md": "# Markdown evidence\n",
                "docs/evidence.pdf": pdf_bytes,
                "docs/evidence.docx": _docx(),
            }
        )
    )
    state = tmp_path / "archive.sqlite3"
    seen: list[ArchiveMemberAdmissionContext] = []

    def admit(context: ArchiveMemberAdmissionContext) -> str:
        seen.append(context)
        if context.member_name.casefold().endswith((".py", ".js")):
            return "metadata_only"
        if context.member_name.casefold() == "credentials.json":
            return "sensitive"
        return "process"

    original = source.read_bytes()
    summary = _route(state, source, callback=admit).run()

    assert summary.errors == 0
    assert summary.containers_partial == 1
    assert source.read_bytes() == original
    assert {item.member_name for item in seen} == {
        "foreign/module.py",
        "foreign/module.js",
        "credentials.json",
        "docs/evidence.json",
        "docs/evidence.csv",
        "docs/evidence.md",
        "docs/evidence.pdf",
        "docs/evidence.docx",
    }
    with zipfile.ZipFile(source) as archive:
        expected_crc = {name: archive.getinfo(name).CRC for name in archive.namelist()}
    assert all(item.crc32 == expected_crc[item.member_chain] for item in seen)

    with archive_database(state, readonly=True) as connection:
        rows = {
            str(row["member_chain"]): row
            for row in connection.execute(
                """SELECT member_chain,status,text_chars,text_zlib,text_xxh3_128
                FROM documents WHERE member_chain<>''"""
            )
        }
        fts = {
            str(row["member_chain"]): str(row["body"])
            for row in connection.execute(
                """SELECT d.member_chain,f.body FROM documents d
                JOIN document_fts f ON f.file_key=d.file_key
                WHERE d.member_chain<>''"""
            )
        }
    for denied in ("foreign/module.py", "foreign/module.js", "credentials.json"):
        assert rows[denied]["status"] == "metadata_only"
        assert rows[denied]["text_chars"] == 0
        assert rows[denied]["text_zlib"] is None
        assert rows[denied]["text_xxh3_128"] is None
        assert fts[denied] == ""
    for document in (
        "docs/evidence.json",
        "docs/evidence.csv",
        "docs/evidence.md",
        "docs/evidence.pdf",
        "docs/evidence.docx",
    ):
        assert rows[document]["status"] == "indexed"
        assert rows[document]["text_chars"] > 0
    assert search_archive_state(state, "document evidence")
    assert search_archive_state(state, "PDF evidencia")
    assert search_archive_state(state, "OOXML evidencia")
    assert not any(path.name in {"module.py", "module.js", "credentials.json"}
                   for path in tmp_path.iterdir())
    assert {item.member_chain for item in list_archive_members(state)} >= set(rows)


def test_admission_policy_signature_forces_replay_and_changes_durable_signature(
    tmp_path: Path,
) -> None:
    source = tmp_path / "policy.zip"
    source.write_bytes(_zip({"foreign.py": "VALUE = 1\n"}))
    state = tmp_path / "archive.sqlite3"

    first = _route(state, source, callback=lambda _context: "process", signature="policy-v1").run()
    second = _route(
        state,
        source,
        callback=lambda _context: "metadata_only",
        signature="policy-v2",
    ).run()

    assert first.cache_hits == 0
    assert second.cache_hits == 0
    assert second.processing_signature != first.processing_signature
    with archive_database(state, readonly=True) as connection:
        row = connection.execute(
            "SELECT status,text_chars,text_zlib FROM documents WHERE member_chain='foreign.py'"
        ).fetchone()
    assert tuple(row) == ("metadata_only", 0, None)


def test_crc_failure_never_invokes_member_admission_callback(tmp_path: Path, monkeypatch) -> None:
    from neocortex.capabilities.formats.archive import route as archive_route

    source = tmp_path / "crc.zip"
    source.write_bytes(_zip({"foreign.py": "VALUE = 1\n"}))
    state = tmp_path / "archive.sqlite3"
    called: list[str] = []

    def admit(context: ArchiveMemberAdmissionContext) -> str:
        called.append(context.member_name)
        return "process"

    original_read = archive_route._read_zip_member

    def fail_crc(*args, **kwargs):
        raise archive_route.ArchiveExtractionError(
            "archive_member_size_mismatch", "fixture CRC/size verification failure"
        )

    monkeypatch.setattr(archive_route, "_read_zip_member", fail_crc)
    try:
        summary = _route(state, source, callback=admit).run()
    finally:
        monkeypatch.setattr(archive_route, "_read_zip_member", original_read)
    assert summary.containers_partial == 1
    assert called == []


def test_admission_callback_observes_cancellation_before_extraction(tmp_path: Path) -> None:
    source = tmp_path / "cancel.zip"
    source.write_bytes(_zip({"foreign.py": "VALUE = 1\n"}))
    state = tmp_path / "archive.sqlite3"
    token = CancellationToken()

    def admit(_context: ArchiveMemberAdmissionContext) -> str:
        token.cancel()
        return "process"

    with pytest.raises(CancellationRequested):
        _route(state, source, callback=admit, cancellation=token).run()
