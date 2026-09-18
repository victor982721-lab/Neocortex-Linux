"""A current transcript needs no new media probe; drift still invalidates it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.audio.models import (
    AudioProcessingError,
    TranscriptResult,
    TranscriptSegment,
    WhisperRuntime,
)
from neocortex.capabilities.formats.audio.state import audio_database
from tests.test_audio_lazy_runtime_preflight import PROBE, _route


@pytest.mark.parametrize("damage", (None, "signature", "segments", "source"))
def test_current_transcript_skips_probe_without_hiding_drift(
    tmp_path: Path, damage: str | None,
) -> None:
    source = tmp_path / "sound.ogg"
    source.write_bytes(b"OggS fixture")
    calls = []
    backend = ["fixture-v1"]
    fail_probe = [False]

    def probe(*args, **kwargs):
        calls.append("probe")
        if fail_probe[0]:
            raise AudioProcessingError(
                "audio_probe_timeout", "injected transient failure",
                recommendation="retry", retryable=True,
            )
        return PROBE

    def resolver(*args, **kwargs):
        calls.append("resolve")
        return WhisperRuntime(backend[0], "fixture-ct2", 0, "cpu", "int8")

    def factory(config, runtime):
        calls.append("load")

        def transcribe(*args, **kwargs):
            calls.append("transcribe")
            return TranscriptResult(
                "hello", "en", 1.0, 4.0, 1.0,
                (TranscriptSegment(0, 0, 1000, "hello", None, None),),
                config.model_name, runtime.backend_version, "cpu", "int8",
            )

        return SimpleNamespace(transcribe=transcribe, close=lambda: None)

    def make_route():
        return _route(tmp_path, source=source, probe=probe, resolver=resolver, factory=factory)

    first = make_route().run()
    assert first.cache_hits == 0 and first.transcribed == 1
    calls.clear()
    if damage == "signature":
        backend[0] = "fixture-v2"
    elif damage == "segments":
        with audio_database(tmp_path / "audio.sqlite3") as conn:
            conn.execute("DELETE FROM segments")
            conn.commit()
    elif damage == "source":
        source.write_bytes(b"OggS changed source")
    else:
        fail_probe[0] = True
    replay = make_route().run()
    if damage is None:
        assert replay.cache_hits == replay.transcribed == 1
        assert replay.errors == 0
        assert calls == ["resolve"]
        with audio_database(tmp_path / "audio.sqlite3", readonly=True) as conn:
            row = conn.execute("SELECT status,text_chars,segment_count FROM documents").fetchone()
            assert tuple(row) == ("complete", 5, 1)
            assert conn.execute("SELECT COUNT(*) FROM transcript_fts").fetchone()[0] == 1
    else:
        assert replay.cache_hits == 0 and replay.transcribed == 1
        assert calls.count("probe") == calls.count("transcribe") == 1
        if damage == "signature":
            assert replay.processing_signature != first.processing_signature
