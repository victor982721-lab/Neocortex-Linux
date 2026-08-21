"""Durable owner, replay-link and timestamped search contracts for video."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot
from _04_Nucleo_Operativo import video_state as video_state_module
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
from neocortex.platform_policy import sqlite_path_collation


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


@pytest.mark.skipif(sqlite_path_collation() != "BINARY", reason="POSIX path identity contract")
@pytest.mark.parametrize("reverse", (False, True))
def test_linux_case_distinct_video_paths_survive_both_insertion_orders(
    tmp_path: Path,
    reverse: bool,
) -> None:
    state_path = tmp_path / "video.sqlite3"
    initialize_video_state(state_path)
    snapshots = (
        FileSnapshot(str(tmp_path / "Case.mp4"), 11, 101, 10, 20, -1),
        FileSnapshot(str(tmp_path / "case.mp4"), 11, 102, 10, 20, -1),
    )
    ordered = tuple(reversed(snapshots)) if reverse else snapshots
    frames = (VideoFrameEvidence(0, 0, ("interval",), 1, 1, "a" * 32, True, "Malpaso"),)
    with video_database(state_path, create=False) as connection:
        for run_id, snapshot in enumerate(ordered, 1):
            store_video_success(
                connection,
                snapshot,
                "video/mp4",
                "fixture-signature",
                _probe(),
                frames,
                (),
                None,
                run_id,
            )
        connection.commit()
        rows = connection.execute(
            "SELECT file_key,path FROM documents ORDER BY path COLLATE BINARY"
        ).fetchall()
        assert [(str(row[0]), str(row[1])) for row in rows] == [
            (file_key_from_snapshot(snapshots[0]), snapshots[0].path),
            (file_key_from_snapshot(snapshots[1]), snapshots[1].path),
        ]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(search_video_state(state_path, "Malpaso", 5)) == 2


@pytest.mark.skipif(
    sqlite_path_collation() != "BINARY", reason="POSIX migration changes path collation"
)
def test_populated_video_v1_migrates_atomically_with_frames_and_fts_rowids(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "video.sqlite3"
    snapshot = _snapshot(tmp_path / "legacy.mp4")
    frame = VideoFrameEvidence(
        0,
        12_345,
        ("scene",),
        320,
        200,
        "e" * 32,
        True,
        "evidencia legado",
        88.0,
        "fixture-ocr-v1",
    )
    with sqlite3.connect(state_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        video_state_module._create_video_v1_schema(connection)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','1')")
        store_video_success(
            connection,
            snapshot,
            "video/mp4",
            "legacy-signature",
            _probe(),
            (frame,),
            (),
            None,
            1,
        )
        connection.execute(
            "UPDATE frame_fts SET rowid=41 WHERE file_key=?",
            (file_key_from_snapshot(snapshot),),
        )
        connection.commit()

    initialize_video_state(state_path)

    with video_database(state_path, readonly=True) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "2"
        )
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM video_inventory").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 1
        assert connection.execute("SELECT rowid FROM frame_fts").fetchone()[0] == 41
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    before = state_path.read_bytes()
    initialize_video_state(state_path)
    assert state_path.read_bytes() == before
    assert search_video_state(state_path, "legado", 5)[0]["frame_index"] == 0


@pytest.mark.skipif(
    sqlite_path_collation() != "BINARY", reason="POSIX migration changes path collation"
)
def test_video_v1_migration_rolls_back_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "video.sqlite3"
    with sqlite3.connect(state_path) as connection:
        video_state_module._create_video_v1_schema(connection)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','1')")
        connection.commit()
    original = video_state_module._migrate_video_v1

    def fail_after_rebuild(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(video_state_module, "_migrate_video_v1", fail_after_rebuild)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        initialize_video_state(state_path)

    with sqlite3.connect(state_path) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "1"
        )
        video_state_module.validate_sqlite_schema_contract(
            connection,
            video_state_module._video_v1_schema_contract(),
            label="video schema 1 after rollback",
            exact=True,
        )


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
