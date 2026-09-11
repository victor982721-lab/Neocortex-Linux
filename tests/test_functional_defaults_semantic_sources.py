"""Functional defaults for the shared Semantic text-source contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.capabilities.formats.audio.state import audio_database, initialize_audio_state
from neocortex.capabilities.formats.video.state import initialize_video_state, video_database
from neocortex.platform.content_capability_manifest import content_capability_for_source
from neocortex.semantic.semantic_plan_results import _select_plan_sources
from neocortex.semantic.semantic_planner import plan_semantic_index
from neocortex.semantic.semantic_sources import (
    TEXT_SOURCE_KINDS,
    iter_text_source_records,
    semantic_source_database,
    semantic_source_heads,
)
from neocortex.semantic.video_source import VideoSourceBlocked


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _create_video_owner(root: Path, *, audio_key: str | None = None) -> Path:
    path = semantic_source_database(root, "video")
    initialize_video_state(path)
    with video_database(path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,duration_seconds,frame_count,ocr_frame_count,ocr_text_chars,
            audio_streams,audio_file_key,audio_processing_signature,audio_status,
            last_seen_run_id,updated_ns)
            VALUES('video:1','/fixtures/inspection.mp4','video/mp4',42,100,0,
            'video-fixture-v1','complete','Inspection video',12.5,1,1,14,?,?,?, ?,1,1)""",
            (int(audio_key is not None), audio_key, "audio-fixture-v1" if audio_key else None,
             "complete" if audio_key else None),
        )
        connection.execute(
            """INSERT INTO frames(
            file_key,frame_index,timestamp_ms,sampling_reasons_json,width,height,
            content_xxh3_128,ocr_available,ocr_text,ocr_mean_confidence)
            VALUES('video:1',0,250,'[\"interval\"]',640,480,
            '0123456789abcdef0123456789abcdef',1,'Breaker closed',92.0)"""
        )
        connection.execute(
            """INSERT INTO frame_fts(file_key,path,title,timestamp_ms,body)
            VALUES('video:1','/fixtures/inspection.mp4','Inspection video',250,
            'Breaker closed')"""
        )
        connection.commit()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    return path


def _create_audio_owner(root: Path, *, file_key: str) -> Path:
    path = semantic_source_database(root, "audio")
    initialize_audio_state(path)
    with audio_database(path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,media_metadata_json,text_chars,segment_count,retryable,
            review_disposition,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,?,'complete',?,'{}',23,1,0,'none',1,1)""",
            (
                file_key,
                "/fixtures/inspection.mp4",
                "video/mp4",
                42,
                100,
                0,
                "audio-fixture-v1",
                "Inspection video",
            ),
        )
        connection.execute(
            """INSERT INTO segments(
            file_key,segment_index,start_ms,end_ms,text,avg_logprob,no_speech_probability)
            VALUES(?,?,?,?,?,-0.1,0.01)""",
            (file_key, 0, 1500, 2600, "Audio transcript evidence"),
        )
        connection.execute(
            """INSERT INTO transcript_fts(file_key,path,title,body)
            VALUES(?,?,?,?)""",
            (file_key, "/fixtures/inspection.mp4", "Inspection video", "Audio transcript evidence"),
        )
        connection.commit()
    return path


def test_default_text_source_contract_is_manifest_backed_and_keeps_existing_channels() -> None:
    video = content_capability_for_source("video")

    assert video.semantic_channel == "text"
    assert video.semantic_source_kinds == ("video",)
    assert video.state_database == "video.sqlite3"
    assert TEXT_SOURCE_KINDS[-1] == "video"
    assert {"archive", "code", "video"}.issubset(TEXT_SOURCE_KINDS)
    assert "image_ocr" not in TEXT_SOURCE_KINDS


def test_cli_and_planner_accept_explicit_video_text_source() -> None:
    args = build_parser().parse_args(
        ("--semantic-plan", "text", "--semantic-source", "video")
    )

    validate_arguments(args)

    assert args.semantic_source == ["video"]
    assert _select_plan_sources("text", ("video",)) == ("video",)


def test_public_planner_consumes_video_records_without_models_or_state_mutation(
    tmp_path: Path,
) -> None:
    owner = _create_video_owner(tmp_path)
    owner_before = owner.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    plan = plan_semantic_index(
        tmp_path,
        scope="text",
        source_kinds=("video",),
        scratch_directory=scratch,
    )

    assert plan.selected_sources == ("video",)
    assert len(plan.source_plans) == 1
    assert plan.source_plans[0].source_kind == "video"
    assert plan.source_plans[0].resources == 1
    assert plan.source_plans[0].sections >= 2
    assert plan.source_plans[0].chunks >= 2
    assert plan.jobs_created == 0
    assert plan.state_mutated is False
    assert owner.read_bytes() == owner_before
    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "semantic.sqlite3").exists()
    assert not (tmp_path / "framework.lock").exists()


def test_video_default_projection_uses_manifest_path_and_distinct_audio_link(
    tmp_path: Path,
) -> None:
    _create_video_owner(tmp_path, audio_key="audio:7")
    _create_audio_owner(tmp_path, file_key="audio:7")

    before = {
        path.name: (path.stat().st_ino, path.stat().st_size)
        for path in tmp_path.iterdir()
    }
    records = tuple(iter_text_source_records(tmp_path, "video"))

    assert [record.section.section_kind for record in records] == [
        "video_metadata_title",
        "video_audio_transcript",
        "video_frame_ocr",
    ]
    assert records[1].section.provenance["locator"] == {
        "kind": "audio_segment",
        "segment_index": 0,
        "start_ms": 1500,
        "end_ms": 2600,
    }
    assert records[2].section.provenance["locator"] == {
        "kind": "video_frame",
        "frame_index": 0,
        "timestamp_ms": 250,
    }
    assert records[0].item.source_kind == "video"
    assert records[0].item.provenance["coverage"] == "complete"
    assert before == {
        path.name: (path.stat().st_ino, path.stat().st_size)
        for path in tmp_path.iterdir()
    }


def test_video_head_tracks_same_size_ocr_rewrites_and_reader_freshness(tmp_path: Path) -> None:
    _create_video_owner(tmp_path)
    first = semantic_source_heads(tmp_path, ("video",))[0]

    with video_database(semantic_source_database(tmp_path, "video"), create=False) as connection:
        connection.execute(
            "UPDATE frames SET ocr_text='Breaker opened' WHERE file_key='video:1'"
        )
        connection.execute(
            "UPDATE frame_fts SET body='Breaker opened' WHERE file_key='video:1'"
        )
        connection.commit()

    second = semantic_source_heads(tmp_path, ("video",))[0]
    records = tuple(iter_text_source_records(tmp_path, "video"))

    assert second.digest != first.digest
    assert records[-1].section.text == "Breaker opened"
    assert records[-1].section.provenance["locator"]["timestamp_ms"] == 250


def test_video_reader_blocks_stale_frame_fts_projection(tmp_path: Path) -> None:
    _create_video_owner(tmp_path)
    with video_database(semantic_source_database(tmp_path, "video"), create=False) as connection:
        connection.execute(
            "UPDATE frames SET ocr_text='Only the frame owner changed' WHERE file_key='video:1'"
        )
        connection.commit()

    with pytest.raises(VideoSourceBlocked, match="projection is inconsistent"):
        tuple(iter_text_source_records(tmp_path, "video"))

    head = semantic_source_heads(tmp_path, ("video",))[0]
    assert head.coverage == "blocked"
    assert head.complete is False
