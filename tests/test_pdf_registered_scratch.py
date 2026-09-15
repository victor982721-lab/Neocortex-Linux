"""Registered scratch lifecycle for PDF structural recovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from neocortex.deduplication import snapshot_path
from neocortex.capabilities.formats.pdf import pdf_isolation
from neocortex.runtime.scratch import ScratchManager, ScratchState


def _config(root: Path) -> pdf_isolation.IsolatedExtractionConfig:
    return pdf_isolation.IsolatedExtractionConfig(
        ocr_mode="never",
        ocr_lang="eng",
        dpi=200,
        min_page_chars=40,
        max_page_text_chars=10_000,
        max_render_pixels=1_000_000,
        max_ocr_pages=None,
        ocr_timeout_seconds=5,
        pdfminer_fallback=True,
        max_pages=None,
        page_start=None,
        page_end=None,
        fail_fast_pages=False,
        skip_before=0,
        only_pages=frozenset(),
        prior_ocr_pages=0,
        tesseract_cmd=None,
        tessdata_dir=None,
        scratch_root=root,
    )


def _capture_copy(command, **_kwargs):
    source = Path(command[-2])
    destination = Path(command[-1])
    destination.write_bytes(source.read_bytes())
    return subprocess.CompletedProcess(command, 0, b"", b"")


def test_qpdf_recovery_uses_registered_workspace_and_retires_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    original = source.read_bytes()
    scratch_root = tmp_path / "state" / "scratch" / "pdf-recovery"
    snapshot = snapshot_path(source)

    monkeypatch.setattr(pdf_isolation.shutil, "which", lambda _name: "qpdf")
    monkeypatch.setattr(pdf_isolation, "run_bounded_capture", _capture_copy)
    manager = ScratchManager(
        scratch_root,
        owner=pdf_isolation.PDF_RECOVERY_SCRATCH_OWNER,
        create_root=False,
    )

    with pdf_isolation._qpdf_repaired_copy(
        snapshot,
        _config(scratch_root),
        primary_error="primary",
        fallback_error="fallback",
    ) as (repaired, evidence):
        repaired_path = Path(repaired)
        assert repaired_path.is_file()
        assert repaired_path.parent.parent == scratch_root
        assert manager.records()[0].state is ScratchState.ACTIVE
        assert evidence["qpdf_exit_code"] == 0

    assert manager.records() == ()
    assert source.read_bytes() == original


def test_qpdf_recovery_retains_registered_workspace_after_consumer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    scratch_root = tmp_path / "state" / "scratch" / "pdf-recovery"
    snapshot = snapshot_path(source)
    monkeypatch.setattr(pdf_isolation.shutil, "which", lambda _name: "qpdf")
    monkeypatch.setattr(pdf_isolation, "run_bounded_capture", _capture_copy)

    with pytest.raises(RuntimeError, match="consumer failed"):
        with pdf_isolation._qpdf_repaired_copy(
            snapshot,
            _config(scratch_root),
            primary_error="primary",
            fallback_error="fallback",
        ):
            raise RuntimeError("consumer failed")

    records = ScratchManager(
        scratch_root,
        owner=pdf_isolation.PDF_RECOVERY_SCRATCH_OWNER,
        create_root=False,
    ).records()
    assert len(records) == 1
    assert records[0].state is ScratchState.FAILED_RETAINED
    assert records[0].metadata == {
        "component": "pdf-isolation",
        "operation": "qpdf-recovery",
        "scope": pdf_isolation.PDF_RECOVERY_SCRATCH_SCOPE,
    }
    assert (records[0].path / "recovered.pdf").is_file()


def test_pdf_recovery_root_is_state_owned_and_absolute(tmp_path: Path) -> None:
    state = tmp_path / "state" / "pdf.sqlite3"
    root = pdf_isolation.pdf_recovery_scratch_root(state)

    assert root.is_absolute()
    assert root == state.parent / "scratch" / "pdf-recovery"
    assert not root.exists()
