"""End-to-end Video owner coverage through the canonical Knowledge surfaces."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from _02_Deduplicacion import FileSnapshot
from _04_Nucleo_Operativo.audio_state import audio_database, initialize_audio_state
from _04_Nucleo_Operativo.cli_app import main
from _04_Nucleo_Operativo.cli_knowledge import KnowledgeExitCode
from _04_Nucleo_Operativo.file_identity import file_key_from_snapshot
from _04_Nucleo_Operativo.knowledge_contracts import OwnerAvailability
from _04_Nucleo_Operativo.knowledge_planner import KnowledgeQuery
from _04_Nucleo_Operativo.knowledge_service import KnowledgeSearchService
from _04_Nucleo_Operativo.knowledge_snapshot import KnowledgeStatePaths
from _04_Nucleo_Operativo.video_models import VideoMediaProbe, VideoStreamProbe
from _04_Nucleo_Operativo.video_state import (
    VIDEO_SCHEMA_VERSION,
    VideoFrameEvidence,
    find_published_audio_link,
    initialize_video_state,
    store_video_success,
    video_database,
)


def _snapshot(path: Path) -> FileSnapshot:
    observed = path.stat()
    return FileSnapshot(
        str(path),
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        getattr(observed, "st_birthtime_ns", -1),
    )


def _probe(*, audio_streams: int = 0) -> VideoMediaProbe:
    return VideoMediaProbe(
        6.0,
        "mp4",
        (VideoStreamProbe(0, "h264", 16, 16, 1.0, 6.0, None),),
        audio_streams,
        (),
        0,
    )


def _publish_video(
    paths: KnowledgeStatePaths,
    source: Path,
    *,
    text: str = "MALPASO transformer plate",
    timestamp_ms: int = 2500,
    audio_link: object = None,
) -> FileSnapshot:
    initialize_video_state(paths.video)
    snapshot = _snapshot(source)
    with video_database(paths.video, create=False) as connection:
        store_video_success(
            connection,
            snapshot,
            "video/mp4",
            "fixture-video-v1",
            _probe(audio_streams=int(audio_link is not None)),
            (
                VideoFrameEvidence(
                    0,
                    timestamp_ms,
                    ("interval",),
                    16,
                    16,
                    "a" * 32,
                    bool(text),
                    text,
                    95.0 if text else None,
                    "fixture-ocr-v1" if text else None,
                ),
            ),
            (),
            audio_link,
            1,
        )
        connection.commit()
    return snapshot


def _owner(service: KnowledgeSearchService, name: str):
    return next(owner for owner in service.status().owners if owner.owner == name)


def test_published_frame_ocr_reaches_canonical_service_and_cli_with_timestamp(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    paths = KnowledgeStatePaths.from_directory(state)
    source = tmp_path / "Case.mp4"
    source.write_bytes(b"video fixture")
    _publish_video(paths, source)
    before = paths.video.read_bytes()

    result = KnowledgeSearchService(paths).search(
        KnowledgeQuery("MALPASO", source_kinds=("video",), limit=5)
    )

    hit = next(hit for hit in result.hits if hit.resource.owner == "video")
    assert (hit.evidence.start_ms, hit.evidence.end_ms) == (2500, 2501)
    assert (hit.evidence.section_kind, hit.evidence.section_id) == (
        "video_frame_ocr",
        "0",
    )
    assert hit.evidence.identifiers == (("neocortex.video.timestamp", "00:00:02.500"),)
    assert hit.resource.current_path == str(source)
    assert hit.revision.processing_signature == "fixture-video-v1"
    assert _owner(KnowledgeSearchService(paths), "video").state is OwnerAvailability.AVAILABLE

    code = main(
        (
            "--state-directory",
            str(state),
            "--knowledge-search",
            "MALPASO",
            "--knowledge-json",
        )
    )
    payload = json.loads(capsys.readouterr().out)
    cli_evidence = next(
        item["evidence"] for item in payload["hits"] if item["resource"]["owner"] == "video"
    )
    assert code == int(KnowledgeExitCode.PARTIAL)
    assert (cli_evidence["start_ms"], cli_evidence["end_ms"]) == (2500, 2501)
    assert paths.video.read_bytes() == before


@pytest.mark.parametrize(
    ("fixture", "expected", "error_code"),
    (
        ("absent", OwnerAvailability.ABSENT, None),
        ("empty", OwnerAvailability.INCOMPATIBLE, "schema_version_absent"),
        ("future", OwnerAvailability.FUTURE, "future_schema"),
        ("corrupt", OwnerAvailability.CORRUPT, "SQLITE_NOTADB"),
    ),
)
def test_knowledge_status_classifies_video_owner_fail_closed_without_writes(
    tmp_path: Path,
    fixture: str,
    expected: OwnerAvailability,
    error_code: str | None,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    paths = KnowledgeStatePaths.from_directory(state)
    if fixture == "empty":
        with sqlite3.connect(paths.video):
            pass
    elif fixture == "future":
        initialize_video_state(paths.video)
        with sqlite3.connect(paths.video) as connection:
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='schema_version'",
                (str(VIDEO_SCHEMA_VERSION + 1),),
            )
    elif fixture == "corrupt":
        paths.video.write_bytes(b"not a SQLite database")
    before = paths.video.read_bytes() if paths.video.exists() else None

    owner = _owner(KnowledgeSearchService(paths), "video")

    assert owner.state is expected
    assert owner.error_code == error_code
    assert (paths.video.read_bytes() if paths.video.exists() else None) == before


def test_linked_video_audio_is_ranked_once_by_audio_owner_not_as_video(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    paths = KnowledgeStatePaths.from_directory(state)
    initialize_audio_state(paths.audio)
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"video with audio")
    snapshot = _snapshot(source)
    key = file_key_from_snapshot(snapshot)
    transcript = "prueba de relación de transformación"
    with audio_database(paths.audio, create=False) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
            title,media_metadata_json,text_chars,segment_count,retryable,
            review_disposition,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,?,'complete',?,'{}',?,?,0,'none',1,1)""",
            (
                key,
                snapshot.path,
                "video/mp4",
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                "fixture-audio-v1",
                "recording",
                len(transcript),
                1,
            ),
        )
        connection.execute(
            """INSERT INTO segments(
            file_key,segment_index,start_ms,end_ms,text,avg_logprob,no_speech_probability)
            VALUES(?,0,1000,2000,?,-0.1,0.01)""",
            (key, transcript),
        )
        connection.execute(
            "INSERT INTO transcript_fts(file_key,path,title,body) VALUES(?,?,?,?)",
            (key, snapshot.path, "recording", transcript),
        )
        connection.commit()
    link = find_published_audio_link(paths.audio, snapshot)
    assert link is not None
    _publish_video(paths, source, text="frame without target words", audio_link=link)

    result = KnowledgeSearchService(paths).search(KnowledgeQuery("transformación", limit=10))

    matched = tuple(hit for hit in result.hits if "transformación" in (hit.evidence.snippet or ""))
    assert len(matched) == 1
    assert matched[0].resource.owner == "audio"
    assert all(hit.resource.owner != "video" for hit in matched)
