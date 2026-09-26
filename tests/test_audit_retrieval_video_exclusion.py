"""E2E Video-owner exclusion and Semantic source-head regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.capabilities.formats.audio.state import audio_database, initialize_audio_state
from neocortex.capabilities.formats.video.models import (
    VideoMediaProbe,
    VideoNotApplicable,
    VideoStreamProbe,
)
from neocortex.capabilities.formats.video.route import VideoRoute, VideoRouteConfig, _VideoMetrics
from neocortex.capabilities.formats.video.state import (
    VideoFrameEvidence,
    initialize_video_state,
    video_database,
)
from neocortex.deduplication import snapshot_path
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.semantic.semantic_sources import iter_text_source_records, semantic_source_heads
from neocortex.semantic.video_source import iter_video_source_records, video_source_head


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _route_publish(
    root: Path,
    *,
    name: str,
    probe: VideoMediaProbe,
    prepared: object,
    audio_state_path: Path | None = None,
) -> tuple[Path, Path]:
    source = root / name
    source.write_bytes(b"bounded video-route fixture")
    state_path = root / "video.sqlite3"
    framework_path = root / "framework.sqlite3"
    initialize_video_state(state_path)
    with FrameworkState(framework_path):
        pass
    route = VideoRoute(
        VideoRouteConfig(
            state_path=state_path,
            root=root,
            audio_state_path=audio_state_path,
            ocr_mode="never",
        ),
        FrameworkRouteState(framework_path),
        1,
    )
    snapshot = snapshot_path(source)
    with video_database(state_path, create=False) as connection:
        route._process_candidate(
            connection,
            snapshot,
            "video/webm" if name.endswith("webm") else "video/mp4",
            "video-route-audit-v1",
            route.ocr_runtime_resolver(route.config),
            _VideoMetrics(),
            prepared=prepared,
        )
        connection.commit()
    return source, state_path


def _audio_projection(root: Path, source: Path) -> Path:
    audio_path = root / "audio.sqlite3"
    initialize_audio_state(audio_path)
    snapshot = snapshot_path(source)
    key = file_key_from_snapshot(snapshot)
    with audio_database(audio_path, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,media_metadata_json,text_chars,segment_count,retryable,
            review_disposition,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,?,'complete',?,'{}',23,1,0,'none',1,1)""",
            (
                key,
                snapshot.path,
                "video/webm",
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                "audio-route-audit-v1",
                Path(snapshot.path).stem,
            ),
        )
        connection.execute(
            """INSERT INTO segments(
            file_key,segment_index,start_ms,end_ms,text,avg_logprob,no_speech_probability)
            VALUES(?,?,?,?,?,?,?)""",
            (key, 0, 100, 900, "Audio-only transcript", -0.1, 0.01),
        )
        connection.execute(
            "INSERT INTO transcript_fts(file_key,path,title,body) VALUES(?,?,?,?)",
            (key, snapshot.path, Path(snapshot.path).stem, "Audio-only transcript"),
        )
        connection.commit()
    return audio_path


def _audio_only_probe() -> VideoMediaProbe:
    return VideoMediaProbe(0.25, "matroska,webm", (), 1, (), 0)


def _visual_probe() -> VideoMediaProbe:
    return VideoMediaProbe(
        1.0,
        "mp4",
        (VideoStreamProbe(0, "h264", 640, 480, 30.0, 1.0, None),),
        0,
        (),
        0,
    )


def test_audio_only_route_exclusion_bridges_to_semantic_without_duplicate_transcript(
    tmp_path: Path,
) -> None:
    source = tmp_path / "audio-only.webm"
    source.write_bytes(b"audio-only")
    audio_path = _audio_projection(tmp_path, source)
    _route_publish(
        tmp_path,
        name=source.name,
        probe=_audio_only_probe(),
        prepared=VideoNotApplicable(_audio_only_probe()),
        audio_state_path=audio_path,
    )

    head = semantic_source_heads(tmp_path, ("video",))[0]
    video_records = tuple(iter_text_source_records(tmp_path, "video"))
    audio_records = tuple(iter_text_source_records(tmp_path, "audio"))

    assert head.complete is True
    assert head.coverage == "complete"
    assert head.reason == "video_source_status_not_applicable"
    assert head.source_status == "complete"
    assert video_records == ()
    assert len(audio_records) == 1
    assert audio_records[0].section.text == "Audio-only transcript"
    assert tuple(iter_video_source_records(tmp_path)) == ()


def test_true_video_route_publishes_visual_records_through_semantic_reader(
    tmp_path: Path,
) -> None:
    probe = _visual_probe()
    frame = VideoFrameEvidence(
        0,
        100,
        ("interval",),
        640,
        480,
        "0" * 32,
        True,
        "Breaker closed",
        90.0,
    )
    _route_publish(
        tmp_path,
        name="visual.mp4",
        probe=probe,
        prepared=(probe, (frame,), ()),
    )

    head = semantic_source_heads(tmp_path, ("video",))[0]
    records = tuple(iter_text_source_records(tmp_path, "video"))

    assert head.complete is True
    assert head.reason is None
    assert [record.section.section_kind for record in records] == [
        "video_metadata_title",
        "video_frame_ocr",
    ]
    assert records[-1].section.text == "Breaker closed"


@pytest.mark.parametrize("status", ("unknown", "future", "corrupt"))
def test_unknown_video_status_cannot_fall_through_to_complete_head(
    tmp_path: Path,
    status: str,
) -> None:
    probe = _visual_probe()
    frame = VideoFrameEvidence(0, 100, ("interval",), 640, 480, "1" * 32)
    _source, state_path = _route_publish(
        tmp_path,
        name=f"{status}.mp4",
        probe=probe,
        prepared=(probe, (frame,), ()),
    )
    with video_database(state_path, create=False) as connection:
        connection.execute("UPDATE documents SET status=?", (status,))
        connection.commit()

    head = video_source_head(tmp_path)
    records = tuple(iter_video_source_records(tmp_path))

    assert head.complete is False
    assert head.coverage == "partial"
    assert head.reason == "video_source_status_unknown"
    assert records == ()


def test_partial_video_status_remains_partial_and_head_digest_tracks_status(
    tmp_path: Path,
) -> None:
    probe = _visual_probe()
    frame = VideoFrameEvidence(
        0,
        100,
        ("interval",),
        640,
        480,
        "2" * 32,
        True,
        "Partial visual evidence",
        90.0,
    )
    _source, state_path = _route_publish(
        tmp_path,
        name="partial.mp4",
        probe=probe,
        prepared=(probe, (frame,), ()),
    )
    complete_head = video_source_head(tmp_path)
    with video_database(state_path, create=False) as connection:
        connection.execute("UPDATE documents SET status='partial'")
        connection.commit()

    partial_head = video_source_head(tmp_path)

    assert complete_head.digest != partial_head.digest
    assert partial_head.complete is False
    assert partial_head.coverage == "partial"
    assert partial_head.reason is None
    assert tuple(iter_video_source_records(tmp_path))
