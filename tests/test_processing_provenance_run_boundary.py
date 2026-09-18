"""Public run boundaries refresh tool provenance and preserve durable replay."""

from __future__ import annotations

import os
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteConfig
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator


def _write_qpdf(path: Path, probe_log: Path, version: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' {shlex.quote('qpdf fixture ' + version)}\n"
        f"printf 'x' >> {shlex.quote(str(probe_log))}\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


@pytest.mark.parametrize("route_only", (False, True), ids=("initial", "route-only"))
def test_two_public_runs_refresh_tool_signature_and_preserve_text_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_only: bool,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "source.txt"
    source.write_text("bounded provenance replay fixture\n", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    qpdf = tools / "qpdf"
    probe_log = tmp_path / "qpdf-probes"
    _write_qpdf(qpdf, probe_log, "A")
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))

    config = FrameworkConfig(
        root=corpus,
        state_directory=tmp_path / "state",
        route="text",
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
        heartbeat_interval_seconds=0.01,
    )
    if route_only:
        # A real initial run supplies durable inputs for both route-only runs.
        seed = FrameworkOrchestrator(config).run_initial()
        assert seed.text is not None
        assert seed.text.extracted == 1
        config = replace(config, route_only=True, candidate_run_id=seed.run_id)

    pdf_config = PdfRouteConfig(
        tmp_path / "pdf-provenance-only.sqlite3",
        ocr_mode="never",
        pdfminer_fallback=False,
    )
    signatures: list[str] = []
    locked_name = "_run_route_only_locked" if route_only else "_run_initial_locked"
    original_locked = getattr(FrameworkOrchestrator, locked_name)

    def observe_and_run(self, boundary):
        # Observe the real config cache at entry and then execute the real run.
        # The second read in one run must not re-probe an unchanged tool.
        signature = pdf_config.processing_signature
        signatures.append(signature)
        probes = probe_log.read_text(encoding="utf-8")
        assert pdf_config.processing_signature == signature
        assert probe_log.read_text(encoding="utf-8") == probes
        return original_locked(self, boundary)

    monkeypatch.setattr(FrameworkOrchestrator, locked_name, observe_and_run)
    framework = FrameworkOrchestrator(config)
    first = framework.run()
    assert first.text is not None
    assert first.text.errors == 0
    assert first.text.extracted == (0 if route_only else 1)
    assert first.text.cache_hits == (1 if route_only else 0)
    assert probe_log.read_text(encoding="utf-8") == "x"
    if route_only:
        # Route-only publishes retained candidates on its new run and retires
        # the older candidate set. Replay from that current public run id.
        framework.config = replace(framework.config, candidate_run_id=first.run_id)

    # Keep path, size and mtime: the second public run must replace the outer
    # config cache too, without any test calling a private or public clear.
    previous = qpdf.stat()
    replacement = tools / "replacement"
    _write_qpdf(replacement, probe_log, "B")
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(qpdf)
    assert qpdf.stat().st_size == previous.st_size
    assert qpdf.stat().st_mtime_ns == previous.st_mtime_ns

    second = framework.run()
    assert second.text is not None
    assert second.text.errors == 0
    assert second.text.processed == 0
    assert second.text.extracted == 0
    assert second.text.cache_hits == 1
    assert second.run_id != first.run_id
    assert source.read_text(encoding="utf-8") == "bounded provenance replay fixture\n"
    assert len(signatures) == 2
    assert signatures[0] != signatures[1]
    assert probe_log.read_text(encoding="utf-8") == "xx"
