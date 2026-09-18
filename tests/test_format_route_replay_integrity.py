"""Public video replay preserves multi-frame identity and repairs duplicates."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from neocortex.capabilities.formats.video import route as video_route
from neocortex.capabilities.formats.video.frames import VideoFrameBatch
from neocortex.capabilities.formats.video.state import video_database
from tests.test_video_route import (
    _Evidence,
    _FrameworkState,
    _MemoryGate,
    _Runtime,
    _config,
    _frame,
    _probe,
    _snapshot,
)


def test_video_public_replay_preserves_multiple_frames_and_anomalous_keys(
    tmp_path: Path, monkeypatch,
) -> None:
    snapshot = _snapshot(tmp_path)
    config = _config(tmp_path)
    framework = _FrameworkState(snapshot)
    calls = []

    @contextmanager
    def sample(*args, **kwargs):
        calls.append("sample")
        yield VideoFrameBatch((
            _frame(tmp_path),
            replace(_frame(tmp_path), index=1, timestamp_ms=5321, content_xxh3_128="b" * 32),
        ))

    def ocr(*args, **kwargs):
        calls.append("ocr")
        return _Evidence()

    monkeypatch.setattr(video_route, "resolve_video_ffmpeg", lambda path: "ffmpeg")
    monkeypatch.setattr(video_route, "resolve_video_ffprobe", lambda path: "ffprobe")

    def run(run_id):
        return video_route.VideoRoute(
            config, framework, run_id, memory_gate=_MemoryGate(),
            media_probe=lambda *args, **kwargs: _probe(), frame_sampler=sample,
            ocr_runtime_resolver=lambda cfg: _Runtime(), frame_ocr=ocr,
        ).run()

    first = run(1)
    assert first.frames_sampled == 2
    calls.clear()
    statements = []

    @contextmanager
    def traced(*args, **kwargs):
        with video_database(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(video_route, "video_database", traced)
    second = run(2)
    assert second.cache_hits == 1 and second.frames_sampled == 2
    assert calls == []
    assert not any(s.startswith(("UPDATE frame_fts", "DELETE FROM frame_fts", "INSERT INTO frame_fts")) for s in statements)
    with video_database(config.state_path) as conn:
        conn.execute("INSERT INTO frame_fts SELECT * FROM frame_fts LIMIT 1")
        for key in (None, b"anomalous"):
            conn.execute("INSERT INTO frame_fts VALUES(?, 'orphan', 'title', 0, 'body')", (key,))
        conn.commit()
    repaired = run(3)
    assert repaired.cache_hits == 1 and repaired.frames_sampled == 2
    assert calls == []
    with video_database(config.state_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM frame_fts").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM frame_fts WHERE file_key IS NULL").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM frame_fts WHERE typeof(file_key)='blob'").fetchone()[0] == 1
