"""CLI registration, validation, projection and direct-dispatch contracts for video."""

from __future__ import annotations


import json
from pathlib import Path

import pytest

from neocortex.runtime.config.application_config_projections import (
    video_route_config_from_application,
)
from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_operations import selected_direct_operations
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.api.cli.cli_video import run_video_doctor


TEST_CAPABILITIES = ('base', 'documents')


def test_video_route_arguments_project_every_safety_bound(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        (
            "--root",
            str(tmp_path / "corpus"),
            "--state-directory",
            str(tmp_path / "state"),
            "--route",
            "video",
            "--video-max-mb",
            "50",
            "--video-max-count",
            "7",
            "--video-max-duration-seconds",
            "600",
            "--video-max-frames",
            "24",
            "--video-interval-seconds",
            "15",
            "--video-scene-threshold",
            "0.4",
            "--no-video-include-scenes",
            "--video-include-keyframes",
            "--video-max-frame-pixels",
            "1000000",
            "--video-max-frame-side",
            "1280",
            "--video-probe-timeout",
            "11",
            "--video-discovery-timeout",
            "22",
            "--video-frame-timeout",
            "9",
            "--video-file-timeout",
            "120",
            "--video-worker-memory-mb",
            "768",
            "--retry-video-errors",
            "--video-ffmpeg-path",
            "ffmpeg-custom",
            "--video-ffprobe-path",
            "ffprobe-custom",
            "--video-ocr",
            "never",
            "--video-ocr-lang",
            "deu+chi_sim",
            "--video-ocr-profile",
            "auto-multilingual",
            "--video-ocr-timeout",
            "8",
        )
    )
    validate_arguments(args)
    application = framework_config_from_args(args)
    route = video_route_config_from_application(application)

    assert application.route == "video"
    assert application.video_database == tmp_path / "state" / "video.sqlite3"
    assert route.state_path == application.video_database
    assert route.audio_state_path == application.audio_database
    assert route.max_file_bytes == 50_000_000
    assert route.max_documents == 7
    assert route.max_duration_seconds == 600
    assert route.max_frames == 24
    assert route.interval_seconds == 15
    assert route.scene_threshold == 0.4
    assert not route.include_scenes
    assert route.include_keyframes
    assert route.max_frame_pixels == 1_000_000
    assert route.max_frame_side == 1280
    assert route.probe_timeout_seconds == 11
    assert route.discovery_timeout_seconds == 22
    assert route.frame_timeout_seconds == 9
    assert route.file_timeout_seconds == 120
    assert route.worker_memory_bytes == 768 * 1024 * 1024
    assert route.retry_errors
    assert route.ffmpeg_path == "ffmpeg-custom"
    assert route.ffprobe_path == "ffprobe-custom"
    assert route.ocr_mode == "never"
    assert route.ocr_lang == "deu+chi_sim"
    assert route.ocr_profile == "auto-multilingual"
    assert route.ocr_timeout_seconds == 8


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--video-max-count", "0"), "--video-max-count must be positive"),
        (("--video-max-frames", "0"), "--video-max-frames must be between"),
        (("--video-max-frames", "257"), "--video-max-frames must be between"),
        (("--video-interval-seconds", "0"), "--video-interval-seconds must be positive"),
        (("--video-scene-threshold", "0"), "--video-scene-threshold must be between"),
        (("--video-scene-threshold", "1"), "--video-scene-threshold must be between"),
        (("--video-max-frame-pixels", "0"), "--video-max-frame-pixels must be between"),
        (("--video-file-timeout", "0"), "--video-file-timeout must be positive"),
        (("--video-worker-memory-mb", "0"), "--video-worker-memory-mb must be positive"),
        (("--video-search-limit", "1001"), "--video-search-limit must be between"),
        (("--video-search", "   "), "--video-search must be non-empty"),
    ),
)
def test_invalid_video_bounds_fail_before_execution(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


@pytest.mark.parametrize(
    ("arguments", "destination"),
    (
        (("--video-status",), "video_status"),
        (("--video-search", "protección"), "video_search"),
        (("--video-doctor",), "video_doctor"),
    ),
)
def test_video_direct_operations_are_registered_lazily(
    arguments: tuple[str, ...],
    destination: str,
) -> None:
    args = build_parser().parse_args(arguments)
    validate_arguments(args)

    assert tuple(operation.destination for operation in selected_direct_operations(args)) == (
        destination,
    )


def test_video_direct_operations_reject_route_or_mutation_authority() -> None:
    for arguments in (
        ("--video-search", "query", "--route", "video"),
        ("--video-status", "--apply"),
    ):
        args = build_parser().parse_args(arguments)
        with pytest.raises(SystemExit, match="video direct actions cannot be combined"):
            validate_arguments(args)


@pytest.mark.capability('documents')
def test_video_doctor_fails_closed_when_configured_ocr_pack_preflight_fails(
    monkeypatch,
    capsys,
) -> None:
    args = build_parser().parse_args(("--video-doctor", "--video-ocr-profile", "auto-multilingual"))
    validate_arguments(args)
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.frames.resolve_video_ffmpeg",
        lambda _path: "/fixture/ffmpeg",
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.video.probe.resolve_video_ffprobe",
        lambda _path: "/fixture/ffprobe",
    )
    monkeypatch.setattr(
        "neocortex.capabilities.formats.image.document.resolve_document_verifier",
        lambda _config: (_ for _ in ()).throw(RuntimeError("missing chi_sim.traineddata")),
    )

    assert run_video_doctor(args) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["failures"] == ["frame_ocr:RuntimeError"]
