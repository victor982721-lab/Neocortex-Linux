"""Preparation preserves the selected scope and cannot authorize missing effects."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.runtime.orchestration.preparation import (
    PreparationRequest, PreparationUnavailable, prepare_framework_run, prepare_run,
)


class Boundary:
    effective_signature = "fixture-boundary"
    def __init__(self):
        self.verifications = 0
    def verify(self):
        self.verifications += 1


def test_selected_missing_dependency_and_not_run_are_not_zero(tmp_path: Path):
    boundary = Boundary()
    request = PreparationRequest(tmp_path / "corpus", tmp_path / "state", ("audio",), False)
    result = prepare_run(request, boundary, probes=[
        ("audio", "audio", "route_execution", lambda: ("unavailable", "model_missing", None)),
    ])
    payload = result.to_dict()
    assert payload["selected_routes"] == ["audio"]
    assert payload["checks"][1]["status"] == "unavailable"
    assert payload["content_validation"]["executed"] is False
    assert payload["content_validation"]["count"] is None
    assert boundary.verifications == 2
    assert not (tmp_path / "state").exists() and not (tmp_path / "corpus").exists()
    result.require_effects_ready()


def test_exhausted_budget_keeps_unexecuted_selected_requirement(tmp_path: Path):
    called = []
    request = PreparationRequest(tmp_path / "c", tmp_path / "s", (), True, max_checks=1)
    result = prepare_run(request, Boundary(), probes=[
        ("sqlite", "runtime", "corpus_effects", lambda: called.append(1)),
    ])
    assert not called
    check = result.to_dict()["checks"][1]
    assert check["status"] == "not_checked" and check["executed"] is False
    assert check["observed_at_ns"] is None
    with pytest.raises(PreparationUnavailable, match="sqlite"):
        result.require_effects_ready()


def test_explicit_cancel_stops_before_owner_probe(tmp_path: Path):
    def forbidden():
        raise AssertionError("cancelled run reached owner")
    request = PreparationRequest(tmp_path / "c", tmp_path / "s", (), False, cancelled=lambda: True)
    with pytest.raises(CancellationRequested):
        prepare_run(request, Boundary(), probes=[("sqlite", "runtime", "corpus_effects", forbidden)])


def test_boundary_replacement_at_end_is_not_swallowed(tmp_path: Path):
    class Changed(Boundary):
        def verify(self):
            super().verify()
            if self.verifications == 2:
                raise RuntimeError("root replaced")
    request = PreparationRequest(tmp_path / "c", tmp_path / "s", (), False)
    with pytest.raises(RuntimeError, match="root replaced"):
        prepare_run(request, Changed(), probes=[])


def test_cancellation_during_last_probe_prevents_success(tmp_path: Path):
    cancelled = [False]
    def probe():
        cancelled[0] = True
        return "ready", "fixture", None
    request = PreparationRequest(tmp_path / "c", tmp_path / "s", (), False,
                                 cancelled=lambda: cancelled[0])
    with pytest.raises(CancellationRequested):
        prepare_run(request, Boundary(), probes=[("last", "runtime", "corpus_effects", probe)])


def test_last_probe_deadline_blocks_effects(tmp_path: Path, monkeypatch):
    ticks = iter((0, 0, 2, 2))
    monkeypatch.setattr("neocortex.runtime.orchestration.preparation.time.monotonic", lambda: next(ticks))
    request = PreparationRequest(tmp_path / "c", tmp_path / "s", (), True, time_budget_seconds=1)
    result = prepare_run(request, Boundary(), probes=[
        ("last", "runtime", "corpus_effects", lambda: ("ready", "fixture", None)),
    ])
    assert result.checks[1].executed is True
    assert result.checks[1].reason_code == "preparation_deadline_exceeded_after_probe"
    assert result.checks[-1].status == "blocked"
    with pytest.raises(PreparationUnavailable):
        result.require_effects_ready()


def test_cancellation_at_final_boundary_is_honored(tmp_path: Path):
    cancelled = [False]
    class FinalCancellation(Boundary):
        def verify(self):
            super().verify()
            if self.verifications == 2:
                cancelled[0] = True
    request = PreparationRequest(tmp_path / "c", tmp_path / "s", (), True,
                                 cancelled=lambda: cancelled[0])
    with pytest.raises(CancellationRequested):
        prepare_run(request, FinalCancellation(), probes=[])


@pytest.mark.parametrize("route,option", [
    ("pdf", "pdf_ocr_mode"), ("image", "image_document_ocr_mode"),
    ("video", "video_ocr_mode"),
])
@pytest.mark.parametrize("mode", ["never", "auto"])
def test_framework_prepares_only_the_selected_ocr_options(tmp_path, monkeypatch, route, option, mode):
    monkeypatch.setattr("neocortex.platform.sqlite_runtime_attestation.observe_platform_native_runtime",
                        lambda **kwargs: {"status": "approved", "observed": {}})
    calls = []
    def locate(executable):
        calls.append(executable)
        return "/fixture/bin/" + executable
    monkeypatch.setattr("neocortex.runtime.orchestration.preparation.shutil.which", locate)
    config = SimpleNamespace(root=tmp_path / "c", state_directory=tmp_path / "s", apply_actions=False,
                             **{option: mode, route + "_tesseract_cmd": "selected-ocr"})
    result = prepare_framework_run(config, Boundary(), (route,))
    assert result.request.effective_options[option] == mode
    assert ("selected-ocr" in calls) == (mode == "auto")
    assert any(check.check_id == route + "_ocr_executable" for check in result.checks) == (mode == "auto")
