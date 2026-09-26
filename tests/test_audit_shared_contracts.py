"""Focused regressions for shared identity, progress, and runtime boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.foundation import processing_provenance
from neocortex.foundation.processing_provenance import resolve_tesseract_runtime
from neocortex.progress import LineProgress, ProgressEvent
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
)
from tests.internal_paths_test_support import disjoint_internal_paths_policy


def test_normal_mutation_guard_abstains_without_physical_root_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    policy = CorpusAccessPolicy("normal", root, None, None, None)
    guard = CorpusMutationGuard(policy, disjoint_internal_paths_policy(tmp_path / "internal"))

    with pytest.raises(PermissionError, match="identity is incomplete"):
        guard.reject_run_mutation()
    with pytest.raises(PermissionError, match="identity is incomplete"):
        guard.require_paths_allowed(root / "new.txt")


def test_line_progress_emits_absolute_counter_and_total_regressions(capsys) -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    reporter = LineProgress(clock=Clock(), item_interval=100, time_interval_seconds=100)
    reporter(ProgressEvent("run", "phase", "Trabajando", 10, 10))
    reporter(ProgressEvent("run", "phase", "Trabajando", 2, 4))

    payloads = [
        json.loads(line.removeprefix("NEOCORTEX_PROGRESS "))
        for line in capsys.readouterr().err.splitlines()
    ]
    assert [payload["completed"] for payload in payloads] == [10, 2]
    assert payloads[-1]["total"] == 4


@pytest.mark.parametrize("language", ("../secret", "/tmp/secret", "eng+../secret"))
def test_tesseract_runtime_rejects_path_like_language_codes(language: str) -> None:
    with pytest.raises(ValueError, match="language code"):
        resolve_tesseract_runtime(
            command="/does/not/run",
            tessdata_dir=None,
            language=language,
            timeout_seconds=5,
        )


def test_tesseract_runtime_deduplicates_and_preserves_language_order(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_probe(
        command: str | None,
        tessdata_dir: str | None,
        requested: tuple[str, ...],
        timeout: float,
    ) -> object:
        observed.update(
            command=command,
            tessdata_dir=tessdata_dir,
            requested=requested,
            timeout=timeout,
        )
        return object()

    monkeypatch.setattr(processing_provenance, "_resolve_tesseract_runtime_cached", fake_probe)
    resolve_tesseract_runtime(
        command="fixture-tesseract",
        tessdata_dir=None,
        language="eng+eng+spa",
        timeout_seconds=5,
    )
    assert observed["requested"] == ("eng", "spa")
