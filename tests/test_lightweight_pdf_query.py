"""The public persisted PDF reader does not load the PDF processing engines."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


_REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("published", (False, True))
def test_public_pdf_search_uses_the_existing_lightweight_query_owner(
    published: bool, tmp_path: Path
) -> None:
    from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state, pdf_database

    state = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(state)
    if published:
        # Publish fixture text into the real schema, using the PDF owner rather
        # than inventing a parallel test schema or an alternate search function.
        with pdf_database(state) as connection:
            connection.execute(
                """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                    status,page_count,completed_pages,updated_ns
                ) VALUES('fixture','transformador.pdf',0,1,-1,'fixture-v1','done',1,1,1)"""
            )
            connection.execute(
                "INSERT INTO page_fts(file_key,path,page_number,text) VALUES(?,?,?,?)",
                ("fixture", "transformador.pdf", 0, "transformador de potencia, revisión"),
            )
            connection.commit()
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    script = f"""
import importlib.abc
import json
import sys
from pathlib import Path
sys.path.insert(0, {str(_REPOSITORY)!r})
blocked = ('PIL', 'pymupdf', 'fitz', 'pdfminer', 'pytesseract', 'numpy',
           'neocortex.capabilities.formats.pdf.pdf_derived',
           'neocortex.capabilities.formats.pdf.pdf_isolation',
           'neocortex.capabilities.formats.pdf.pdf_profile')
attempted = []
class NoProcessingEngine(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == prefix or fullname.startswith(prefix + '.') for prefix in blocked):
            attempted.append(fullname)
            raise ModuleNotFoundError('processing engine forbidden: ' + fullname, name=fullname)
sys.meta_path.insert(0, NoProcessingEngine())
before = frozenset(sys.modules)
from neocortex.api.public import search_pdf_state
from neocortex.capabilities.formats.pdf.pdf_derived_queries import search_pdf_state as owner
assert search_pdf_state is owner
result = search_pdf_state(Path(sys.argv[1]), 'transformador', 5)
replay = search_pdf_state(Path(sys.argv[1]), 'transformador', 5)
assert result == replay
introduced = sorted(set(sys.modules) - before)
assert not any(any(name == prefix or name.startswith(prefix + '.') for prefix in blocked)
               for name in introduced)
print(json.dumps(dict(result=result, attempted=attempted)))
"""
    probe = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(state)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout)
    assert payload["attempted"] == []
    assert len(payload["result"]) == int(published)
    if published:
        result = payload["result"][0]
        assert result["path"] == "transformador.pdf"
        assert result["page_number"] == 0
        assert "[transformador]" in result["snippet"]
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before
