"""Differential regressions for the validated content hotfixes."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.code.code_state import CodeState
from neocortex.code.ingestion.code_candidate_scope import ProjectCandidateScope
from neocortex.code.ingestion import code_rust
from neocortex.capabilities.formats.docx import layout as docx_layout
from neocortex.capabilities.formats.docx import route as docx_route


def test_manifest_roots_preserve_longest_first_tie_and_posix_paths() -> None:
    roots = (
        (1, "outer", "python", "/projects"),
        (2, "first", "python", "/projects/a"),
        (3, "same-root", "rust", "/projects/a/."),
        (4, "nested", "go", "/projects/a/sub"),
        (5, "double-slash", "python", "//projects/a"),
    )
    paths = (
        "/projects/a/main.py", "/projects/a/sub/file.go", "/projects/ab/file.py",
        "/elsewhere/file.py", "//projects/a/file.py", "/projects/a", "/projects/a/../a/x.py",
    )
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE files(current_path TEXT,current_version_id INTEGER,status TEXT);
        CREATE TABLE file_versions(version_id INTEGER,invalidated_ns INTEGER);
        CREATE TABLE project_memberships(project_id INTEGER,version_id INTEGER,
            proposed_path TEXT,relation TEXT,confidence REAL,selected INTEGER,evidence_json TEXT);
    """)
    for index, path in enumerate(paths, 1):
        connection.execute("INSERT INTO files VALUES(?,?,'current')", (path, index))
        connection.execute("INSERT INTO file_versions VALUES(?,NULL)", (index,))
    owner = SimpleNamespace(connection=connection, _manifest_roots=lambda: roots)
    CodeState._assign_manifest_roots(owner, 123)
    actual = {row[0]: (row[1], row[2], json.loads(row[3])) for row in connection.execute(
        "SELECT version_id,project_id,proposed_path,evidence_json FROM project_memberships"
    )}
    expected = {}
    for index, path in enumerate(paths, 1):
        normalized = os.path.normcase(os.path.abspath(path))
        matches = [root for root in roots if os.path.commonpath((
            normalized, os.path.normcase(os.path.abspath(root[3]))
        )) == os.path.normcase(os.path.abspath(root[3]))]
        if not matches:
            continue
        chosen = max(matches, key=lambda root: len(os.path.normcase(os.path.abspath(root[3]))))
        try:
            proposed = str(Path(path).relative_to(Path(chosen[3]))).replace("\\", "/")
        except ValueError:
            proposed = Path(path).name
        expected[index] = (chosen[0], proposed, {"root": chosen[3], "run_id": 123})
    assert actual == expected
    connection.close()


_BASE_SCOPED_QUERY = """
SELECT r.reference_id,MIN(target.symbol_id)
FROM code_references r JOIN _nc_current_versions current ON current.version_id=r.version_id
JOIN symbols source ON source.symbol_id=r.source_symbol_id
JOIN symbols module ON module.version_id=r.version_id AND module.kind='module'
JOIN symbols target ON target.version_id=r.version_id
LEFT JOIN symbols target_parent ON target_parent.symbol_id=target.parent_symbol_id
WHERE r.target_symbol_id IS NULL AND r.kind IN('call','inherits','implements_trait','decorator')
AND ((r.evidence='python-ast:call-expression-import-bound' AND r.target_hint=target.qualified_name)
 OR (r.evidence!='python-ast:call-expression-import-bound' AND (
    r.name=target.name OR substr(r.name,-(length(target.name)+1))='.'||target.name
    OR r.target_hint=target.qualified_name OR r.target_hint=target.name
    OR substr(r.target_hint,-(length(target.name)+1))='.'||target.name)))
AND ((target.kind IN ('function','class') AND target.parent_symbol_id=module.symbol_id)
 OR (target.kind='method' AND (
    (source.kind='class' AND target.parent_symbol_id=source.symbol_id)
    OR (source.kind IN ('method','nested_function') AND target.parent_symbol_id=source.parent_symbol_id)
    OR (target_parent.kind='class' AND (
        r.name=target_parent.name||'.'||target.name
        OR r.target_hint=target_parent.name||'.'||target.name)))))
GROUP BY r.reference_id HAVING COUNT(DISTINCT target.symbol_id)=1
"""


def test_scoped_candidates_equal_base_predicate_for_names_types_and_ambiguity() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE symbols(symbol_id INTEGER PRIMARY KEY,version_id INTEGER,
            parent_symbol_id INTEGER,kind TEXT,name TEXT,qualified_name TEXT);
        CREATE INDEX symbols_name_idx ON symbols(name,kind,version_id);
        CREATE INDEX symbols_qualified_idx ON symbols(qualified_name,version_id);
        CREATE INDEX symbols_version_fixture_idx ON symbols(version_id,kind);
        CREATE TABLE code_references(reference_id INTEGER PRIMARY KEY,version_id INTEGER,
            source_symbol_id INTEGER,target_symbol_id INTEGER,kind TEXT,name TEXT,
            target_hint TEXT,evidence TEXT);
        CREATE TEMP TABLE _nc_current_versions(version_id INTEGER PRIMARY KEY);
        INSERT INTO _nc_current_versions VALUES(1),(2);
    """)
    symbols = [
        (1, 1, None, "module", "m", "m"), (2, 1, 1, "function", "f", "m.f"),
        (3, 1, 1, "function", "a.b", "m.a.b"), (4, 1, 1, "class", "A", "m.A"),
        (5, 1, 4, "method", "go", "m.A.go"), (6, 1, 1, "class", "B", "m.B"),
        (7, 1, 6, "method", "go", "m.B.go"), (8, 1, 1, "function", "dup", "m.dup1"),
        (9, 1, 1, "function", "dup", "m.dup2"), (10, 1, 1, "function", "", "m.empty"),
        (11, 1, 1, "function", "Case", "m.Case"), (12, 1, 1, "function", "case", "m.case"),
        (13, 2, None, "module", "n", "n"), (14, 2, 13, "function", "f", "n.f"),
        (15, 1, 1, "function", "dot.", "m.dot."),
        (16, 1, 1, "function", sqlite3.Binary(b"blob"), "m.blob"),
        (17, 1, 1, "function", "nul", "m.nul"),
    ]
    connection.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?)", symbols)
    cases = [
        (1, "f", None, "lexical"), (1, "prefix.a.b", None, "lexical"),
        (1, "unused", "m.a.b", "lexical"), (1, "A.go", None, "lexical"),
        (4, "go", None, "lexical"), (5, "go", None, "lexical"),
        (1, "dup", None, "lexical"), (1, "Case", None, "lexical"),
        (1, "case", None, "lexical"), (1, "trailing.", None, "lexical"),
        (1, "prefix.dot.", None, "lexical"), (1, "prefix.blob", None, "lexical"),
        (1, "prefix.nul\0ignored", None, "lexical"),
        (1, "f", "m.f", "python-ast:call-expression-import-bound"),
        (1, "f", "f", "python-ast:call-expression-import-bound"),
        (1, "f", None, None), (None, "f", None, "lexical"),
        (1, "prefix." * 100 + "f", None, "lexical"),
        (1, sqlite3.Binary(b"blob"), None, "lexical"),
    ]
    for index, (source, name, hint, evidence) in enumerate(cases, 1):
        connection.execute("INSERT INTO code_references VALUES(?,1,?,NULL,'call',?,?,?)",
                           (index, source, name, hint, evidence))
    expected = dict(connection.execute(_BASE_SCOPED_QUERY))
    CodeState._resolve_scoped_reference_targets(SimpleNamespace(connection=connection))
    actual = dict(connection.execute("SELECT reference_id,symbol_id FROM _nc_scoped_reference_targets"))
    assert actual == expected
    assert actual[2] == 3 and actual[12] == 16
    assert 7 not in actual and 15 not in actual and 16 not in actual
    connection.close()


def test_scope_discovery_preserves_nested_policy_and_frozen_result(tmp_path: Path) -> None:
    roots = [tmp_path / "p", tmp_path / "p" / "src", tmp_path / "p" / "node_modules" / "q",
             tmp_path / "p" / "build" / "r", tmp_path / "p" / ".cache" / "s"]
    scope = ProjectCandidateScope.discover((), explicit_roots=roots,
                                           include_generated=False, include_vendored=False)
    assert set(scope.roots) == {str(roots[0]), str(roots[1])}
    assert isinstance(scope._root_keys, frozenset)
    assert scope.decision(roots[1] / "module.py") == "admit"
    assert scope.decision(tmp_path / "outside.py") == "outside_project"


def test_rust_impl_cursor_preserves_first_overlap_and_open_boundaries() -> None:
    spans = [(0, 100, "outer", None), (10, 20, "inner", None),
             (30, 150, "overlap", "Trait"), (150, 170, "next", None)]
    cursor = code_rust._ImplCursor(spans)
    for offset in (0, 1, 10, 11, 20, 30, 31, 99, 100, 149, 150, 151, 169, 170, 171):
        expected = next((span for span in spans if span[0] < offset < span[1]), None)
        assert cursor.containing(offset) == expected


@pytest.mark.parametrize("text", [
    "const FIRST: i32 = 1;\nconst SECOND: i32 = 2;\n", "type Alias = u8;",
    "fn item() {\n    next();\n}\n", "  static NAME: &str = \"π\";\n",
    "struct S;\n" + "const X: i32 = 0;\n" * 100,
])
def test_rust_signature_matches_base_without_unbounded_tail_copy(text: str) -> None:
    for match in code_rust._ITEM.finditer(text):
        end = code_rust._matching_brace(text, match.start())
        signature_end = text.find("{", match.end(), min(len(text), match.end() + 8192))
        if signature_end < 0 or signature_end > end:
            signature_end = min(end, text.find("\n", match.end()) if "\n" in text[match.end():] else end)
        expected = text[match.start():signature_end].strip()[:4096]
        assert code_rust._rust_item_signature(text, match, end) == expected


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
