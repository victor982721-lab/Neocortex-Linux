"""Logical naming crosses the existing no-replace effect once, without SHA."""

import io
import zipfile

import pytest

from neocortex.deduplication import DedupIndex
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


def _docx():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as package:
        package.writestr("[Content_Types].xml", '<Types/>')
        package.writestr("word/document.xml", '<document/>')
    return data.getvalue()


@pytest.mark.parametrize("source_name,target_name,content", [
    ("report.pdf.~25~", "report.pdf", b"%PDF-1.7\nfixture\n%%EOF\n"),
    ("report.pdf.~1~.~2~", "report.pdf", b"%PDF-1.7\nfixture\n%%EOF\n"),
    ("report__a1b2c3d4.txt.~1~", "report__a1b2c3d4.pdf", b"%PDF-1.7\nfixture\n%%EOF\n"),
    ("report~a1b2c3d4.pdf.~1~", "report~a1b2c3d4.pdf", b"%PDF-1.7\nfixture\n%%EOF\n"),
    ("file.docx.~1~", "file.docx", _docx()),
], ids=["gnu_pdf", "repeated_gnu", "wrong_suffix_decorated", "tilde_decorated", "gnu_docx"])
def test_gnu_normalization_preserves_bytes_identity_and_replays(
    tmp_path, monkeypatch, source_name, target_name, content,
):
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / source_name
    source.write_bytes(content)
    before = source.stat()
    state_root = tmp_path / "state"
    state_root.mkdir()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("logical normalization must not full-hash content")

    monkeypatch.setattr("neocortex.workflow.actions.actions.full_fingerprint", forbidden)
    with DedupIndex(state_root / "dedup.sqlite3") as index, FrameworkState(state_root / "framework.sqlite3") as state:
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        first = FrameworkActions(index, state, run_id, scan.scan_id, apply=True).identify_and_normalize()
        second = FrameworkActions(
            index, state, run_id, index.current_scan_id(scan.scan_id), apply=True,
        ).identify_and_normalize()
    target = root / target_name
    assert sorted(path.name for path in root.iterdir()) == [target_name]
    assert target.read_bytes() == content
    assert target.stat().st_ino == before.st_ino
    assert first.files_renamed == 1
    assert second.files_renamed == 0
    assert second.type_cache_hits == 1
