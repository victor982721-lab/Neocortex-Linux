"""Hermetic Video-to-Semantic projection tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.capabilities.formats.audio.state import (
    audio_database,
    initialize_audio_state,
)
from neocortex.capabilities.formats.video.state import (
    initialize_video_state,
    video_database,
)
from neocortex.semantic.video_source import (
    VIDEO_SOURCE_ADAPTER_VERSION,
    VideoSourceBlocked,
    iter_video_source_records,
    video_source_head,
)
from neocortex.semantic.semantic_sources import (
    iter_text_source_records,
    semantic_source_heads,
)
from neocortex.semantic.semantic_plan_results import _select_plan_sources


def _create_video_fixture(root: Path, *, status: str = "partial") -> Path:
    path = root / "video.sqlite3"
    initialize_video_state(path)
    with video_database(path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,duration_seconds,frame_count,ocr_frame_count,ocr_text_chars,
            audio_file_key,audio_status,last_seen_run_id,updated_ns)
            VALUES('1:2','/tmp/sample.mp4','video/mp4',42,100,0,'video-v1',?,
            'Inspection video',12.5,2,1,18,NULL,NULL,1,1)""",
            (status,),
        )
        connection.execute(
            """INSERT INTO frames(
            file_key,frame_index,timestamp_ms,sampling_reasons_json,width,height,
            content_xxh3_128,ocr_available,ocr_text,ocr_mean_confidence)
            VALUES('1:2',0,250,'[\"interval\"]',640,480,
            '0123456789abcdef0123456789abcdef',1,'Breaker closed',92.0)"""
        )
        connection.execute(
            """INSERT INTO frames(
            file_key,frame_index,timestamp_ms,sampling_reasons_json,width,height,
            content_xxh3_128,ocr_available,ocr_text,ocr_mean_confidence)
            VALUES('1:2',1,500,'[\"scene\"]',640,480,
            'fedcba9876543210fedcba9876543210',0,'',NULL)"""
        )
        connection.execute(
            "INSERT INTO frame_fts(file_key,path,title,timestamp_ms,body) "
            "VALUES('1:2','/tmp/sample.mp4','Inspection video',250,'Breaker closed')"
        )
        connection.commit()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    return path


def _attach_audio_fixture(root: Path, *, status: str = "complete") -> Path:
    """Attach one same-identity Audio projection to the isolated Video owner."""

    path = root / "audio.sqlite3"
    initialize_audio_state(path)
    with audio_database(path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,media_metadata_json,text_chars,segment_count,retryable,
            review_disposition,last_seen_run_id,updated_ns)
            VALUES('1:2','/tmp/sample.mp4','video/mp4',42,100,0,
            'audio-v1',?,'Inspection video','{}',34,1,0,'none',1,1)""",
            (status,),
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
    with video_database(root / "video.sqlite3", create=False) as connection:
        connection.execute(
            """UPDATE documents SET audio_streams=1,audio_file_key='1:2',
            audio_processing_signature='audio-v1',audio_status=? WHERE file_key='1:2'""",
            (status,),
        )
        connection.commit()
    return path


def test_video_projection_preserves_locator_status_and_sidecars(tmp_path: Path) -> None:
    _create_video_fixture(tmp_path)
    before = {
        candidate.name: (candidate.stat().st_ino, candidate.stat().st_size)
        for candidate in tmp_path.iterdir()
    }

    records = tuple(iter_video_source_records(tmp_path))

    after = {
        candidate.name: (candidate.stat().st_ino, candidate.stat().st_size)
        for candidate in tmp_path.iterdir()
    }
    assert before == after
    assert len(records) == 2
    assert records[0].item.source_kind == "video"
    assert records[0].item.provenance["source_status"] == "partial"
    assert records[0].item.provenance["coverage"] == "partial"
    assert records[0].section.section_kind == "video_metadata_title"
    frame = records[1].section
    assert frame.section_kind == "video_frame_ocr"
    assert frame.provenance["locator"] == {
        "kind": "video_frame",
        "frame_index": 0,
        "timestamp_ms": 250,
    }
    assert frame.provenance["source_status"] == "partial"
    assert records[0].item.provenance["adapter"] == VIDEO_SOURCE_ADAPTER_VERSION


def test_video_head_is_deterministic_and_reports_partial_coverage(tmp_path: Path) -> None:
    _create_video_fixture(tmp_path, status="partial")
    first = video_source_head(tmp_path)
    second = video_source_head(tmp_path)
    assert first == second
    assert first.coverage == "partial"
    assert first.complete is False
    assert first.row_count == 1
    assert first.digest.startswith("sha256:")
    assert first.as_payload()["schema"] == "neocortex.semantic-video-source-head/v1"


def test_video_is_exposed_through_common_semantic_source_contract(
    tmp_path: Path,
) -> None:
    _create_video_fixture(tmp_path, status="partial")

    head = semantic_source_heads(tmp_path, ("video",))[0]
    records = tuple(iter_text_source_records(tmp_path, "video"))

    assert head.source_kind == "video"
    assert head.database_name == "video.sqlite3"
    assert head.schema_version == 2
    assert head.complete is False
    assert head.reason is None
    assert records[0].item.source_kind == "video"
    assert records[1].section.provenance["locator"] == {
        "kind": "video_frame",
        "frame_index": 0,
        "timestamp_ms": 250,
    }


def test_video_can_be_selected_explicitly_by_the_semantic_text_planner() -> None:
    assert _select_plan_sources("text", ("video",)) == ("video",)


def test_video_projection_blocks_active_wal_without_creating_reader_sidecars(
    tmp_path: Path,
) -> None:
    path = _create_video_fixture(tmp_path)
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE documents SET title='unpublished' WHERE file_key='1:2'")
        writer.commit()
        wal = Path(str(path) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        with pytest.raises(VideoSourceBlocked, match="active wal"):
            tuple(iter_video_source_records(tmp_path))
        assert wal.exists() and wal.stat().st_size > 0
    finally:
        writer.rollback()
        writer.close()


def test_video_projection_consumes_declared_audio_dependency_and_replays_exactly(
    tmp_path: Path,
) -> None:
    _create_video_fixture(tmp_path, status="complete")
    _attach_audio_fixture(tmp_path)
    before = {
        candidate.name: (
            candidate.stat().st_ino,
            candidate.stat().st_size,
            candidate.stat().st_mtime_ns,
        )
        for candidate in tmp_path.iterdir()
    }

    first = tuple(iter_video_source_records(tmp_path))
    first_head = video_source_head(tmp_path)
    second = tuple(iter_video_source_records(tmp_path))
    second_head = video_source_head(tmp_path)

    assert [record.section.section_kind for record in first] == [
        "video_metadata_title",
        "video_audio_transcript",
        "video_frame_ocr",
    ]
    audio = first[1]
    assert audio.section.text == "Audio transcript evidence"
    assert audio.section.provenance["dependency"] == "audio"
    assert audio.section.provenance["locator"] == {
        "kind": "audio_segment",
        "segment_index": 0,
        "start_ms": 1500,
        "end_ms": 2600,
    }
    assert audio.item.provenance["coverage"] == "complete"
    assert first == second
    assert first_head == second_head
    assert first_head.coverage == "complete"
    assert first_head.source_status == "complete"
    assert before == {
        candidate.name: (
            candidate.stat().st_ino,
            candidate.stat().st_size,
            candidate.stat().st_mtime_ns,
        )
        for candidate in tmp_path.iterdir()
    }


def test_video_audio_source_change_changes_head_and_evidence_without_live_reads(
    tmp_path: Path,
) -> None:
    _create_video_fixture(tmp_path, status="complete")
    audio_path = _attach_audio_fixture(tmp_path)
    first_head = video_source_head(tmp_path)
    with audio_database(audio_path, create=False) as connection:
        connection.execute(
            "UPDATE segments SET text='Changed transcript evidence' WHERE file_key='1:2'"
        )
        connection.commit()

    changed_head = video_source_head(tmp_path)
    records = tuple(iter_video_source_records(tmp_path))

    assert changed_head.digest != first_head.digest
    assert changed_head.coverage == "complete"
    assert any(
        record.section.text == "Changed transcript evidence" for record in records
    )


def test_video_linked_audio_gap_is_partial_not_ready_complete(tmp_path: Path) -> None:
    _create_video_fixture(tmp_path, status="complete")
    with video_database(tmp_path / "video.sqlite3", create=False) as connection:
        connection.execute(
            "UPDATE documents SET audio_streams=1,audio_file_key='missing-audio',"
            "audio_status='partial' WHERE file_key='1:2'"
        )
        connection.commit()

    head = video_source_head(tmp_path)
    records = tuple(iter_video_source_records(tmp_path))

    assert head.coverage == "partial"
    assert head.complete is False
    assert records[0].item.provenance["coverage"] == "partial"
    assert records[0].section.provenance["coverage"] == "partial"
