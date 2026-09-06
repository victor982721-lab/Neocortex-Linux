"""Resolve speech runtime only after an eligible current audio-stream probe."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.audio.models import (
    AudioProcessingError,
    AudioRouteConfig,
    MediaProbe,
    TranscriptResult,
    WhisperRuntime,
    WhisperRuntimeError,
)
from neocortex.capabilities.formats.audio.route import AudioRoute
from neocortex.deduplication import snapshot_path


PROBE = MediaProbe(4.0, "ogg", "opus", 48_000, 1, 1, 0)


def _route(
    tmp_path: Path, *, source: Path | None, mime: str = "audio/ogg", probe, resolver, factory
):
    candidates = {mime: (snapshot_path(source),)} if source is not None else {}
    framework = SimpleNamespace(
        selected_route_candidate_counts=lambda run_id, kind, max_size, route, selection: (
            len(candidates.get(kind, ())),
            sum(max_size is None or item.size <= max_size for item in candidates.get(kind, ())),
        ),
        iter_selected_route_candidates=lambda run_id, kind, route, selection: iter(
            candidates.get(kind, ())
        ),
        store_review_candidates=lambda run_id, values: None,
        reconcile_review_candidates_batch=lambda run_id, route, values: None,
    )
    gate = SimpleNamespace(peak_reserved_bytes=0, wait_count=0, admit=lambda amount: nullcontext())
    return AudioRoute(
        AudioRouteConfig(
            state_path=tmp_path / "audio.sqlite3",
            model_cache_directory=tmp_path / "models",
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,
        1,
        memory_gate=gate,
        media_probe=probe,
        runtime_resolver=resolver,
        transcriber_factory=factory,
    )


def _forbidden(*args, **kwargs):
    raise AssertionError("speech dependencies must not be requested")


def test_empty_audio_selection_does_not_probe_resolve_or_claim_runtime(tmp_path: Path) -> None:
    route = _route(tmp_path, source=None, probe=_forbidden, resolver=_forbidden, factory=_forbidden)
    summary = route.run()
    assert summary.candidates == summary.processed == summary.errors == 0
    assert summary.processing_signature is None
    assert summary.processing_provenance is None


@pytest.mark.parametrize("raises_no_audio", (False, True))
def test_visual_only_video_is_probe_only_and_replays_without_runtime(
    tmp_path: Path, raises_no_audio: bool
) -> None:
    source = tmp_path / "visual.webm"
    source.write_bytes(b"contained visual fixture")
    calls = []

    def probe(*args, **kwargs):
        calls.append("probe")
        if raises_no_audio:
            raise AudioProcessingError(
                "media_without_audio_stream",
                "fixture no audio",
                recommendation="manual_review",
                retryable=False,
                evidence={"video_streams": 1, "duration_seconds": 4.0, "format_name": "webm"},
            )
        return MediaProbe(4.0, "webm", "", None, None, 0, 1)

    route = _route(
        tmp_path,
        source=source,
        mime="video/webm",
        probe=probe,
        resolver=_forbidden,
        factory=_forbidden,
    )
    first, replay = route.run(), route.run()
    assert first.no_audio == replay.no_audio == replay.cache_hits == 1
    assert first.errors == replay.errors == 0
    assert calls == ["probe"]
    assert first.processing_provenance["configuration"]["operation"] == "media_probe_only"
    assert first.processing_provenance["configuration"]["speech_runtime_resolved"] is False
    assert "faster-whisper" not in str(first.processing_provenance)


def test_audio_stream_preserves_explicit_missing_runtime_failure_after_probe(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sound.ogg"
    source.write_bytes(b"OggS fixture")
    order = []

    def probe(*args, **kwargs):
        order.append("probe")
        return PROBE

    def resolver(*args, **kwargs):
        order.append("resolve")
        raise WhisperRuntimeError("fixture speech backend unavailable")

    route = _route(tmp_path, source=source, probe=probe, resolver=resolver, factory=_forbidden)
    with pytest.raises(WhisperRuntimeError, match="fixture speech backend unavailable"):
        route.run()
    assert order == ["probe", "resolve"]


def test_successful_audio_keeps_runtime_signature_drift_and_cache_semantics(tmp_path: Path) -> None:
    source = tmp_path / "sound.ogg"
    source.write_bytes(b"OggS fixture")
    order = []
    backend = ["fixture-v1"]

    def probe(*args, **kwargs):
        order.append("probe")
        return PROBE

    def resolver(*args, **kwargs):
        order.append("resolve")
        return WhisperRuntime(backend[0], "fixture-ct2", 0, "cpu", "int8")

    def factory(config, runtime):
        order.append("load")

        def transcribe(*args, **kwargs):
            order.append("transcribe")
            return TranscriptResult(
                "",
                None,
                None,
                4.0,
                0.0,
                (),
                config.model_name,
                runtime.backend_version,
                "cpu",
                "int8",
            )

        return SimpleNamespace(transcribe=transcribe, close=lambda: order.append("close"))

    route = _route(tmp_path, source=source, probe=probe, resolver=resolver, factory=factory)
    first, replay = route.run(), route.run()
    assert first.no_speech == replay.no_speech == replay.cache_hits == 1
    assert order[:4] == ["probe", "resolve", "load", "transcribe"]
    assert order.count("transcribe") == 1
    assert first.processing_signature == replay.processing_signature
    backend[0] = "fixture-v2"
    changed = route.run()
    assert changed.cache_hits == 0
    assert changed.processing_signature != first.processing_signature
    assert order.count("transcribe") == 2
