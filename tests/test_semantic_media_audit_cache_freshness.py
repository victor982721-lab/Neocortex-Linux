"""Cached media must revalidate the same live snapshot as new processing."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from neocortex.capabilities.formats.audio.state import audio_database
from neocortex.capabilities.formats.video.frames import VideoFrameBatch
from neocortex.capabilities.formats.video.route import VideoRoute
from neocortex.capabilities.formats.video.state import video_database
from neocortex.deduplication import snapshot_path
from tests.test_audio_route import _audio_route
from tests.test_video_route import (
    _config,
    _Evidence,
    _frame,
    _FrameworkState,
    _probe,
    _Runtime,
    _snapshot,
)


def _change_source(source: Path, change: str) -> None:
    before = source.stat()
    if change == "removed":
        source.unlink()
    elif change == "replacement":
        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(b"x" * before.st_size)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(source)
    elif change == "mtime":
        source.write_bytes(b"x" * before.st_size)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    else:
        source.write_bytes(b"different content and a different source size")


@pytest.mark.parametrize("change", ("size", "mtime", "replacement", "removed"))
def test_audio_cache_rejects_inventory_that_no_longer_matches_source(
    tmp_path: Path, change: str
) -> None:
    source = tmp_path / "fixture.opus"
    source.write_bytes(b"OggS deterministic fixture")
    database = tmp_path / "audio.sqlite3"
    route, framework, transcriber, factory_calls = _audio_route(database, source)
    assert route.run().transcribed == 1
    assert route.run().cache_hits == 1
    reconciliations = len(framework.resolutions)

    _change_source(source, change)
    failed = route.run()

    assert failed.cache_hits == 0
    assert failed.retryable_errors == 1
    assert len(transcriber.calls) == len(factory_calls) == 1
    assert len(framework.resolutions) == reconciliations
    expected_error = "audio_io_error" if change == "removed" else "audio_source_changed"
    with audio_database(database, readonly=True) as connection:
        row = connection.execute("SELECT status,error_type FROM documents").fetchone()
        assert tuple(row) == ("error", expected_error)
        assert connection.execute("SELECT COUNT(*) FROM transcript_fts").fetchone()[0] == 0

    if change != "removed":
        framework.candidates = {"application/ogg": (snapshot_path(source),)}
        assert route.run().transcribed == 1
        assert route.run().cache_hits == 1
        assert len(transcriber.calls) == len(factory_calls) == 2


@pytest.mark.parametrize("change", ("size", "mtime", "replacement", "removed"))
def test_video_cache_rejects_inventory_that_no_longer_matches_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    snapshot = _snapshot(tmp_path)
    framework = _FrameworkState(snapshot)
    calls: list[str] = []

    @contextmanager
    def sampler(*_args: Any, **_kwargs: Any):
        calls.append("sample")
        yield VideoFrameBatch((_frame(tmp_path),))

    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffmpeg", lambda _path: "ffmpeg"
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.route.resolve_video_ffprobe", lambda _path: "ffprobe"
    )
    config = _config(tmp_path)
    route = VideoRoute(
        config,
        framework,  # type: ignore[arg-type]
        1,
        media_probe=lambda *_args, **_kwargs: _probe(),
        frame_sampler=sampler,
        ocr_runtime_resolver=lambda _config: _Runtime(),
        frame_ocr=lambda *_args, **_kwargs: _Evidence(),
    )
    assert route.run().complete == 1
    assert route.run().cache_hits == 1
    reconciliations = len(framework.reconciliations)

    _change_source(Path(snapshot.path), change)
    failed = route.run()

    assert failed.cache_hits == 0
    assert failed.errors == failed.retryable_errors == 1
    assert len(calls) == 1
    assert len(framework.reconciliations) == reconciliations
    expected_error = "video_io_error" if change == "removed" else "video_source_changed"
    with video_database(config.state_path, readonly=True) as connection:
        row = connection.execute("SELECT status,error_type FROM documents").fetchone()
        assert tuple(row) == ("error", expected_error)
        assert connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0

    if change != "removed":
        framework.snapshot = snapshot_path(snapshot.path)
        assert route.run().complete == 1
        assert route.run().cache_hits == 1
        assert len(calls) == 2
