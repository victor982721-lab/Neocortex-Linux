"""Durable owner, replay-link and timestamped search contracts for video."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from _02_Deduplicacion import FileSnapshot
from _04_Nucleo_Operativo.audio_state import audio_database, initialize_audio_state
from _04_Nucleo_Operativo.file_identity import file_key_from_snapshot
from _04_Nucleo_Operativo.video_models import VideoMediaProbe, VideoStreamProbe
from _04_Nucleo_Operativo.video_state import (
    VIDEO_SCHEMA_VERSION,
    VideoFrameEvidence,
    find_published_audio_link,
    initialize_video_state,
    search_video_state,
    store_video_success,
    video_database,
    video_state_status,
)


def _snapshot(path: Path) -> FileSnapshot:
    return FileSnapshot(str(path), 11, 22, 1234, 5678, -1)


def _probe(*, audio_streams: int = 0) -> VideoMediaProbe:
    return VideoMediaProbe(
        duration_seconds=30.0,
        format_name="matroska,webm",
        video=(VideoStreamProbe(0, "vp9", 1280, 720, 30.0, 30.0, None),),
        audio_streams=audio_streams,
        subtitles=(),
        chapters=0,
    )


def test_schema_is_exact_idempotent_and_rejects_future_versions(tmp_path: Path) -> None:
    path = tmp_path / "video.sqlite3"
    initialize_video_state(path)
    before = path.read_bytes()

    initialize_video_state(path)

    assert path.read_bytes() == before
    assert video_state_status(path)["schema_version"] == VIDEO_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(VIDEO_SCHEMA_VERSION + 1),),
        )
    modified = path.read_bytes()
    with pytest.raises(RuntimeError, match="newer than supported"):
        initialize_video_state(path)
    with pytest.raises(RuntimeError, match="not the supported schema"):
        video_state_status(path)
    assert path.read_bytes() == modified


def test_frame_search_returns_typed_channel_and_millisecond_evidence(tmp_path: Path) -> None:
    state_path = tmp_path / "video.sqlite3"
    initialize_video_state(state_path)
    snapshot = _snapshot(tmp_path / "source.webm")
    frame = VideoFrameEvidence(
        frame_index=0,
        timestamp_ms=12_345,
        sampling_reasons=("scene", "keyframe"),
        width=1280,
        height=720,
        content_xxh3_128="a" * 32,
        ocr_available=True,
        ocr_text="protección diferencial transformador",
        ocr_mean_confidence=91.5,
        ocr_provenance="fixture-ocr",
    )
    with video_database(state_path, create=False) as connection:
        store_video_success(
            connection,
            snapshot,
            "video/webm",
            "fixture-signature",
            _probe(),
            (frame,),
            (),
            None,
            1,
        )
        connection.commit()

    result = search_video_state(state_path, "¿DIFERENCIAL?", 5)

    assert len(result) == 1
    assert result[0]["channel"] == "frame_ocr"
    assert result[0]["evidence"] == "00:00:12.345"
    assert result[0]["sampling_reasons"] == ["scene", "keyframe"]
    assert "[diferencial]" in result[0]["snippet"]
    assert video_state_status(state_path)["statuses"] == [
        {
            "status": "complete",
            "documents": 1,
            "frames": 1,
            "ocr_frames": 1,
            "ocr_chars": len(frame.ocr_text),
        }
    ]


@pytest.mark.parametrize(
    "frames",
    (
        (
            VideoFrameEvidence(
                0,
                0,
                ("interval",),
                1280,
                720,
                "a" * 32,
                True,
                "x" * (16 * 1024 + 1),
            ),
        ),
        tuple(
            VideoFrameEvidence(
                index,
                index,
                ("interval",),
                1,
                1,
                "a" * 32,
            )
            for index in range(257)
        ),
    ),
    ids=("ocr-text-bound", "frame-count-bound"),
)
def test_owner_rejects_unbounded_frame_evidence_without_partial_publication(
    tmp_path: Path,
    frames: tuple[VideoFrameEvidence, ...],
) -> None:
    state_path = tmp_path / "video.sqlite3"
    initialize_video_state(state_path)
    with video_database(state_path, create=False) as connection:
        with pytest.raises(ValueError):
            store_video_success(
                connection,
                _snapshot(tmp_path / "source.webm"),
                "video/webm",
                "fixture-signature",
                _probe(),
                frames,
                (),
                None,
                1,
            )
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_optional_published_audio_link_adds_timestamped_transcript_evidence(
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "video.sqlite3"
    audio_path = tmp_path / "audio.sqlite3"
    initialize_video_state(video_path)
    initialize_audio_state(audio_path)
    snapshot = _snapshot(tmp_path / "recording.mkv")
    key = file_key_from_snapshot(snapshot)
    with audio_database(audio_path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,media_metadata_json,text_chars,segment_count,retryable,
            review_disposition,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,?,'complete',?,'{}',?,?,0,'none',1,1)""",
            (
                key,
                snapshot.path,
                "video/x-matroska",
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                "audio-signature",
                "recording",
                len("introducción prueba de relación de transformación"),
                2,
            ),
        )
        connection.execute(
            """INSERT INTO segments(
            file_key,segment_index,start_ms,end_ms,text,avg_logprob,no_speech_probability)
            VALUES(?,1,9876,11000,?,-0.1,0.01)""",
            (key, "prueba de relación de transformación"),
        )
        connection.execute(
            """INSERT INTO segments(
            file_key,segment_index,start_ms,end_ms,text,avg_logprob,no_speech_probability)
            VALUES(?,0,0,2000,'introducción',-0.1,0.01)""",
            (key,),
        )
        connection.execute(
            "INSERT INTO transcript_fts(file_key,path,title,body) VALUES(?,?,?,?)",
            (
                key,
                snapshot.path,
                "recording",
                "introducción prueba de relación de transformación",
            ),
        )
        connection.commit()
    link = find_published_audio_link(audio_path, snapshot)
    assert link is not None
    with video_database(video_path, create=False) as connection:
        store_video_success(
            connection,
            snapshot,
            "video/x-matroska",
            "video-signature",
            _probe(audio_streams=1),
            (
                VideoFrameEvidence(
                    0,
                    0,
                    ("interval",),
                    1280,
                    720,
                    "b" * 32,
                ),
            ),
            (),
            link,
            1,
        )
        connection.commit()

    result = search_video_state(
        video_path,
        "¿TRANSFORMACIÓN?",
        audio_state_path=audio_path,
    )

    assert len(result) == 1
    assert result[0]["channel"] == "audio_transcript"
    assert result[0]["evidence"] == "00:00:09.876"
    assert result[0]["snippet"] == "prueba de relación de transformación"
