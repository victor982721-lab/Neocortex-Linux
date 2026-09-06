"""TOCTOU fences for the optional Audio projection consumed by Video."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.capabilities.formats.audio.state import audio_database, initialize_audio_state
from neocortex.capabilities.formats.video.state import initialize_video_state, video_database
from neocortex.semantic import video_source
from neocortex.semantic.video_source import video_source_head


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _create_video_owner(root: Path) -> None:
    path = root / "video.sqlite3"
    initialize_video_state(path)
    with video_database(path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,duration_seconds,frame_count,ocr_frame_count,ocr_text_chars,
            audio_file_key,audio_status,last_seen_run_id,updated_ns)
            VALUES('1:2','/tmp/sample.mp4','video/mp4',42,100,0,'video-v1','complete',
            'Inspection video',12.5,1,1,14,'1:2','complete',1,1)"""
        )
        connection.execute(
            """INSERT INTO frames(
            file_key,frame_index,timestamp_ms,sampling_reasons_json,width,height,
            content_xxh3_128,ocr_available,ocr_text,ocr_mean_confidence)
            VALUES('1:2',0,250,'[\"interval\"]',640,480,
            '0123456789abcdef0123456789abcdef',1,'Breaker closed',92.0)"""
        )
        connection.execute(
            """INSERT INTO frame_fts(file_key,path,title,timestamp_ms,body)
            VALUES('1:2','/tmp/sample.mp4','Inspection video',250,'Breaker closed')"""
        )
        connection.execute(
            "UPDATE documents SET audio_streams=1 WHERE file_key='1:2'"
        )
        connection.commit()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def _create_audio_owner(root: Path) -> None:
    path = root / "audio.sqlite3"
    initialize_audio_state(path)
    with audio_database(path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,media_metadata_json,text_chars,segment_count,retryable,
            review_disposition,last_seen_run_id,updated_ns)
            VALUES('1:2','/tmp/sample.mp4','video/mp4',42,100,0,'audio-v1','complete',
            'Inspection video','{}',23,1,0,'none',1,1)"""
        )
        connection.execute(
            """INSERT INTO segments(
            file_key,segment_index,start_ms,end_ms,text,avg_logprob,no_speech_probability)
            VALUES('1:2',0,1500,2600,'Audio transcript evidence',-0.1,0.01)"""
        )
        connection.execute(
            """INSERT INTO transcript_fts(file_key,path,title,body)
            VALUES('1:2','/tmp/sample.mp4','Inspection video','Audio transcript evidence')"""
        )
        connection.commit()


@pytest.mark.parametrize("mutate_call", (1, 2), ids=("before-open", "after-read"))
def test_video_head_marks_audio_dependency_partial_when_fence_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate_call: int,
) -> None:
    _create_video_owner(tmp_path)
    _create_audio_owner(tmp_path)
    real_capture = video_source.capture_sqlite_read_fence
    audio_calls = 0

    def capture(path: Path):
        nonlocal audio_calls
        if path.name == "audio.sqlite3":
            audio_calls += 1
            if audio_calls == mutate_call == 2:
                with sqlite3.connect(path) as connection:
                    connection.execute(
                        "UPDATE segments SET text='changed after audio snapshot' WHERE file_key='1:2'"
                    )
            fence = real_capture(path)
            if audio_calls == mutate_call == 1:
                with sqlite3.connect(path) as connection:
                    connection.execute(
                        "UPDATE segments SET text='changed before audio snapshot' WHERE file_key='1:2'"
                    )
            return fence
        return real_capture(path)

    monkeypatch.setattr(video_source, "capture_sqlite_read_fence", capture)

    head = video_source_head(tmp_path)

    assert head.coverage == "partial"
    assert head.complete is False
    assert head.reason == "VideoSourceBlocked"
    assert audio_calls == mutate_call
