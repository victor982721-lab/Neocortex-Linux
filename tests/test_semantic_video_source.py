"""Hermetic Video-to-Semantic projection tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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
