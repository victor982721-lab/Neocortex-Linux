"""Page/OCR phases renegotiate native threads without retaining a CPU slot."""

import shutil
import subprocess
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.pdf.pdf_isolation import _controlled_tesseract, pdf_ocr_execution
from neocortex.capabilities.formats.media_resources import checkpoint_before_deadline
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits, ResourceSample,
)

TEST_CAPABILITIES = ("documents", "image")
GIB = 1024**3


def _coordinator():
    effective = [4]
    coordinator = GlobalResourceCoordinator(
        ("pdf",), GlobalResourceLimits(memory_budget_bytes=GIB,
                                       min_free_memory_bytes=0, min_free_commit_bytes=0,
                                       cpu_slots=4, native_thread_slots=4,
                                       sample_interval_seconds=0.001, wait_timeout_seconds=None),
        effective_cpu_probe=lambda: effective[0],
        resource_probe=lambda: ResourceSample(
            available_physical=8 * GIB, total_physical=8 * GIB,
            available_commit=8 * GIB, total_commit=8 * GIB,
            external_cpu_cores=0, own_cpu_cores=0, effective_cpu_capacity=effective[0],
        ),
    )
    return coordinator, effective


def test_ocr_replaces_page_cpu_and_recovers_full_native_capacity():
    coordinator, effective = _coordinator()
    gate = CoordinatedMemoryGate(coordinator, "pdf")
    with gate.admit(128 * 1024**2) as page:
        for capacity in (4, 1, 4):
            effective[0] = capacity
            time.sleep(0.002)
            with pdf_ocr_execution(page) as ocr:
                assert ocr.native_threads == capacity
                assert coordinator.summary().cpu_slots_in_use == capacity
                assert coordinator.summary().transient_bytes == 128 * 1024**2
            assert coordinator.summary().cpu_slots_in_use == 0
            page.checkpoint()
    assert coordinator.summary().transient_bytes == 0


def test_checkpoint_respects_producer_deadline_during_indefinite_resource_wait():
    coordinator, effective = _coordinator()
    gate = CoordinatedMemoryGate(coordinator, "pdf")
    with gate.admit(128) as grant:
        effective[0] = 0
        # Explicit zero external availability, not an invalid effective CPU count.
        coordinator._resource_probe = lambda: ResourceSample(
            total_physical=8 * GIB, available_physical=8 * GIB,
            total_commit=8 * GIB, available_commit=8 * GIB,
            external_cpu_cores=4, own_cpu_cores=0, effective_cpu_capacity=4,
        )
        deadline = time.monotonic() + 0.05
        with pytest.raises(TimeoutError, match="producer deadline"):
            checkpoint_before_deadline(grant, deadline, CancellationToken(), TimeoutError("producer deadline"))
    assert coordinator.summary().cpu_slots_in_use == 0


def test_local_tesseract_deadline_includes_resource_renewal_before_encoding(monkeypatch):
    coordinator, _effective = _coordinator()
    coordinator.limits = replace(coordinator.limits, wait_timeout_seconds=0.4)
    gate = CoordinatedMemoryGate(coordinator, "pdf")
    config = SimpleNamespace(tesseract_cmd="tesseract", tessdata_dir=None,
                             ocr_timeout_seconds=0.05, max_page_text_chars=100000)
    image = SimpleNamespace(save=lambda *_args, **_kwargs:
                            pytest.fail("PNG encoded after the producer deadline"))
    monkeypatch.setattr("neocortex.runtime.control.bounded_subprocess.run_bounded_capture",
                        lambda *_args, **_kwargs: pytest.fail("Tesseract started after deadline"))
    with gate.admit(128):
        coordinator._resource_probe = lambda: ResourceSample(
            total_physical=8 * GIB, available_physical=8 * GIB,
            total_commit=8 * GIB, available_commit=8 * GIB,
            external_cpu_cores=4, own_cpu_cores=0, effective_cpu_capacity=4,
        )
        time.sleep(0.002)
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _controlled_tesseract(image, config, languages="eng", mode="txt", psm=6)
        assert time.monotonic() - started < 0.3
    assert coordinator.summary().cpu_slots_in_use == 0
    assert coordinator.summary().transient_bytes == 0


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is unavailable")
def test_real_tesseract_receives_counted_native_environment_without_mutating_parent():
    import os
    from PIL import Image, ImageDraw, ImageFont

    coordinator, _effective = _coordinator()
    gate = CoordinatedMemoryGate(coordinator, "pdf")
    config = SimpleNamespace(tesseract_cmd=shutil.which("tesseract"), tessdata_dir=None,
                             ocr_timeout_seconds=10, max_page_text_chars=100000)
    before = os.environ.get("OMP_THREAD_LIMIT")
    with Image.new("RGB", (900, 150), "white") as image:
        font = ImageFont.load_default(size=50)
        ImageDraw.Draw(image).text((25, 35), "POWER TRANSFORMER", font=font, fill="black")
        with gate.admit(128 * 1024**2) as page, pdf_ocr_execution(page):
            text = _controlled_tesseract(image, config, languages="eng", mode="txt", psm=6)
            assert "POWER" in text.upper()
    assert os.environ.get("OMP_THREAD_LIMIT") == before
    assert coordinator.summary().peak_native_threads == 4


def test_auto_pdf_parallelism_runs_real_mupdf_in_children_without_artificial_deadline(tmp_path, monkeypatch):
    import fitz
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute, PdfRouteConfig
    from neocortex.deduplication import DedupIndex, snapshot_path
    from neocortex.runtime.control.global_resources import ResourceGrant
    from tests.test_pdf_route import _State

    snapshots = []
    for index in range(2):
        path = tmp_path / f"source-{index}.pdf"
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), f"Distinct technical document {index}")
            document.save(path)
        snapshots.append(snapshot_path(path))
    coordinator, _effective = _coordinator()
    registered = []
    register = ResourceGrant.register_process

    def observed_register(self, pid, start_time_ticks=None):
        identity = register(self, pid, start_time_ticks)
        registered.append(identity)
        return identity

    monkeypatch.setattr(ResourceGrant, "register_process", observed_register)
    config = PdfRouteConfig(tmp_path / "pdf.sqlite3", ocr_mode="never", min_free_bytes=0)
    assert config.workers is None and config.document_timeout_seconds is None
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        route = PdfRoute(config, index, _State(snapshots), 1, 1, global_coordinator=coordinator)
        monkeypatch.setattr(route, "_process_document_local", lambda *args, **kwargs: pytest.fail("MuPDF ran in parent threads"))
        summary = route.run()
    assert summary.extracted == 2
    assert len(set(registered)) >= 2
    assert coordinator.summary().transient_bytes == 0
