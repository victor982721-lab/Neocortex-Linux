"""Differential regressions for the validated content hotfixes."""
from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.docx import layout as docx_layout
from neocortex.capabilities.formats.docx import route as docx_route










def test_docx_detachment_preserves_properties_nested_text_and_extensions(monkeypatch) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    payload = (f'<w:document xmlns:w="{namespace}"><w:body>'
        '<w:bookmarkStart w:id="1"/><w:p><w:pPr><w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '</w:sectPr><w:pStyle w:val="Heading1"/><w:jc w:val="center"/></w:pPr>'
        '<w:r><w:t>Hello</w:t><w:tab/><w:t>world</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:bookmarkEnd w:id="1"/></w:body></w:document>').encode()
    original = docx_layout.safe_xml_iterparse
    retained = []
    def observing(source, **kwargs):
        for event, element in original(source, **kwargs):
            if event == "end" and element.tag == docx_layout.W + "body":
                retained.extend(child.tag for child in element)
            yield event, element
    monkeypatch.setattr(docx_layout, "safe_xml_iterparse", observing)
    text, layout = docx_layout.xml_text_and_layout(io.BytesIO(payload), collect_layout=True,
                                                 budget=docx_layout.TextBudget(10_000))
    assert text == "Hello\tworld\nCell\n"
    assert layout["paragraphs"] == 2 and layout["tables"] == 1
    assert layout["styles"]["Heading1"] == 1 and layout["alignments"]["center"] == 1
    assert layout["sections"][0]["width"] == "11906"
    assert retained == [docx_layout.W + "bookmarkStart", docx_layout.W + "bookmarkEnd"]


def test_docx_prune_keeps_live_sentinel_and_deletes_fts_in_committed_key_batches(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE documents(file_key TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER);
        CREATE TABLE docx_inventory(file_key TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER);
        CREATE TABLE document_fts(file_key TEXT PRIMARY KEY);
    """)
    keys = ["", "a-live", "b-stale", "c-sentinel", "d-stale", "e-stale"]
    connection.executemany("INSERT INTO documents VALUES(?,1,2,3)", ((key,) for key in keys))
    connection.executemany("INSERT INTO document_fts VALUES(?)", ((key,) for key in keys))
    connection.execute("UPDATE documents SET birthtime_ns=? WHERE file_key='c-sentinel'",
                       (docx_route.UNKNOWN_BIRTHTIME_NS,))
    connection.execute("INSERT INTO docx_inventory VALUES('a-live',1,2,3)")
    connection.execute("INSERT INTO docx_inventory VALUES('c-sentinel',1,2,9)")
    connection.commit()
    def delete_fts(conn, table, batch):
        assert table == "document_fts"
        conn.executemany("DELETE FROM document_fts WHERE file_key=?", ((key,) for key in batch))
    monkeypatch.setattr(docx_route, "DOCX_PRUNE_BATCH", 2)
    monkeypatch.setattr(docx_route, "delete_format_fts_keys", delete_fts)
    owner = SimpleNamespace(cancellation=SimpleNamespace(checkpoint=lambda: None))
    assert docx_route.DocxRoute._prune_stale_documents(owner, connection) == 4
    assert list(connection.execute("SELECT file_key FROM documents ORDER BY file_key")) == [
        ("a-live",), ("c-sentinel",)]
    assert list(connection.execute("SELECT file_key FROM document_fts ORDER BY file_key")) == [
        ("a-live",), ("c-sentinel",)]
    assert not connection.in_transaction
    connection.close()


def test_xlsx_detaches_completed_rows_after_preserving_rich_values_and_formula(monkeypatch) -> None:
    from neocortex.capabilities.formats.office import xlsx
    xml = ("<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>"
           "<sheetData><extension/>" + "<row/>" * 1_000 +
           "<row><c r='A1' t='inlineStr'><is><r><t>A</t></r><r><t>B</t></r></is></c>"
           "<c r='B1'><f>1+1</f><v>2</v></c></row></sheetData></worksheet>").encode()
    original = xlsx.safe_xml_iterparse
    retained = []
    def observing(source, **kwargs):
        for event, element in original(source, **kwargs):
            if event == "end" and element.tag.endswith("}sheetData"):
                retained.extend(child.tag.rsplit("}", 1)[-1] for child in element)
            yield event, element
    monkeypatch.setattr(xlsx, "safe_xml_iterparse", observing)
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", xml)
    archive_bytes.seek(0)
    cells = []
    with zipfile.ZipFile(archive_bytes) as archive:
        xlsx._extract_xlsx_worksheet(
            archive, archive.getinfo("xl/worksheets/sheet1.xml"), workbook="book.xlsx",
            sheet=xlsx._XlsxSheet("Data", 1, None), shared_strings=(), styles=(), date_1904=False,
            cells=cells, accumulator=xlsx._TextAccumulator(10_000),
            budget=xlsx._ReadBudget(100_000), cancellation=SimpleNamespace(checkpoint=lambda: None),
            max_cells=10,
        )
    assert [(cell.cell_reference, cell.value, cell.formula) for cell in cells] == [
        ("A1", "AB", None), ("B1", "2", "1+1")]
    assert retained == ["extension"]


@pytest.mark.parametrize("transactional,expected_validations", [(False, 1), (True, 2)])
def test_text_receipt_reuse_stays_within_committed_observation(
    tmp_path: Path, monkeypatch, transactional: bool, expected_validations: int,
) -> None:
    from neocortex.capabilities.formats.text import text_derivation_repository as repository
    from neocortex.capabilities.formats.text.text_state import initialize_text_state, text_database
    from neocortex.semantic.derivation_contracts import ReproducibilityClass, WorkExecutionMode
    from tests.test_text_derivation_state import _start, _insert_document, _outputs, FINISHED
    path = tmp_path / "text.sqlite3"
    initialize_text_state(path)
    repository.begin_text_derivation_attempt(path, _start("producer"))
    with text_database(path, create=False) as connection:
        _insert_document(connection)
        repository.succeed_text_derivation_attempt(
            connection, "producer", receipt_id="receipt-producer", outputs=_outputs(),
            finished_at_utc=FINISHED, duration_ns=100, execution_mode=WorkExecutionMode.EXECUTED,
            reproducibility=ReproducibilityClass.EXACT, terminal_ns=2, document_file_key="file-a",
        )
        connection.commit()
        original = repository._validated_terminal_receipts
        validations = []
        def observed(conn, receipt_ids, **kwargs):
            validations.extend(receipt_ids)
            return original(conn, receipt_ids, **kwargs)
        monkeypatch.setattr(repository, "_validated_terminal_receipts", observed)
        if transactional:
            connection.execute("BEGIN")
        reusable = repository.read_reusable_text_derivation_from_connection(
            connection, "file-a", stage_id="text.extract", processing_signature="psig-text-v1",
        )
        assert reusable is not None and len(reusable.outputs) == 2
        assert validations == ["receipt-producer"] * expected_validations
        assert (reusable.observation is None) is transactional
        if transactional:
            connection.rollback()
        else:
            connection.execute("UPDATE documents SET updated_ns=updated_ns+1 WHERE file_key='file-a'")
            with pytest.raises(repository.TextDerivationIntegrityError):
                repository.validate_text_cache_observation(connection, reusable.observation)
            connection.rollback()
