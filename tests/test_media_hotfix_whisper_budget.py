"""Supervisor policy with a stub child; no models, native ASR or timing claims."""

from pathlib import Path
from types import SimpleNamespace

from neocortex.capabilities.formats.audio import whisper
from neocortex.capabilities.formats.audio.models import (
    AudioRouteConfig, TranscriptResult, WhisperRuntime,
)
from neocortex.runtime.control.cancellation import CancellationToken


class _Supervisor(whisper.WhisperTranscriber):
    def __init__(self):
        super().__init__(
            AudioRouteConfig(state_path=Path("unused.sqlite3")),
            WhisperRuntime("fixture", "fixture", 0, "cpu", "int8"),
        )
        self.starts = []
        self.sent = []

    def _start(self, cancellation):
        self._reset_native_growth()
        grant = whisper.current_resource_grant()
        self._native_threads = 1 if grant is None else max(1, grant.native_threads)
        self.starts.append(self._native_threads)
        self._process = SimpleNamespace(is_alive=lambda: True)

    def close(self):
        self._process = None
        self._reset_native_growth()

    def _request_transcription(self, path, cancellation, request_id):
        grant = whisper.current_resource_grant()
        ceiling = 1 if grant is None else max(1, grant.native_threads)
        assert self._native_threads <= ceiling
        self.sent.append((request_id, self._native_threads))
        return ("ok", request_id, TranscriptResult(
            "fixture", "en", 1.0, 1.0, 1.0, (),
            self.config.model_name, self.runtime.backend_version, "cpu", "int8",
        ))


def _sequence(monkeypatch, offers):
    now = [0.0]
    grant = [None]
    monkeypatch.setattr(whisper, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(whisper, "current_resource_grant", lambda: grant[0])
    supervisor = _Supervisor()
    for seconds, width in offers:
        now[0] = seconds
        grant[0] = None if width is None else SimpleNamespace(native_threads=width)
        supervisor.transcribe(Path("fixture.opus"), cancellation=CancellationToken())
    return supervisor


def test_short_headroom_rebound_does_not_reload_but_contraction_does(monkeypatch):
    result = _sequence(monkeypatch, ((0, 4), (1, 3), (2, 4), (3, 3), (4, 4)))
    assert result.starts == [4, 3]
    assert [width for _request, width in result.sent] == [4, 3, 3, 3, 3]


def test_growth_requires_both_elapsed_window_and_repeated_equal_offers(monkeypatch):
    result = _sequence(monkeypatch, ((0, 3), (1, 4), (2, 4), (3, 4), (31, 4)))
    assert result.starts == [3, 4]
    assert [width for _request, width in result.sent] == [3, 3, 3, 3, 4]


def test_one_late_offer_does_not_count_as_repeated_headroom(monkeypatch):
    result = _sequence(monkeypatch, ((0, 3), (1, 4), (100, 4)))
    assert result.starts == [3]


def test_changed_growth_offer_resets_hysteresis_and_missing_grant_contracts(monkeypatch):
    result = _sequence(monkeypatch, ((0, 2), (1, 4), (20, 4), (31, 3),
                                     (32, 3), (33, 3), (34, None)))
    assert result.starts == [2, 1]
    assert [request for request, _width in result.sent] == list(range(1, 8))
