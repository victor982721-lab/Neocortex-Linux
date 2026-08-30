"""Flat CLI argument and validation contract for dedicated visual video work."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import argparse
from collections.abc import Callable

from .cli_operations import DirectOperationFamily, selected_direct_operations
from neocortex.safety.ocr_profiles import OCR_PROFILE_CHOICES
from neocortex.capabilities.formats.video.frames import MAX_VIDEO_FRAME_PIXELS, MAX_VIDEO_FRAMES

__all__ = (
    "register_video_arguments",
    "validate_video_arguments",
    "validate_video_direct_operation",
)


def register_video_arguments(
    parser: argparse.ArgumentParser,
    *,
    megabyte_type: Callable[[str], int],
) -> None:
    video = parser.add_argument_group("Visual video route")
    video.add_argument(
        "--video-max-mb",
        dest="video_max_file_bytes",
        type=megabyte_type,
        default=None,
        metavar="MB",
        help="inspect only videos at or below this decimal size",
    )
    video.add_argument(
        "--video-max-count",
        dest="video_max_documents",
        type=int,
        default=None,
        metavar="N",
    )
    video.add_argument("--video-max-duration-seconds", type=float, default=6 * 60 * 60)
    video.add_argument("--video-max-frames", type=int, default=48)
    video.add_argument("--video-interval-seconds", type=float, default=30.0)
    video.add_argument("--video-scene-threshold", type=float, default=0.35)
    video.add_argument(
        "--video-include-scenes",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    video.add_argument(
        "--video-include-keyframes",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    video.add_argument("--video-max-frame-pixels", type=int, default=2_073_600)
    video.add_argument("--video-max-frame-side", type=int, default=1920)
    video.add_argument("--video-probe-timeout", type=float, default=30.0)
    video.add_argument("--video-discovery-timeout", type=float, default=60.0)
    video.add_argument("--video-frame-timeout", type=float, default=20.0)
    video.add_argument("--video-file-timeout", type=float, default=300.0)
    video.add_argument("--video-worker-memory-mb", type=int, default=2048)
    video.add_argument(
        "--retry-video-errors",
        action="store_true",
        help="retry unchanged cached video failures once",
    )
    video.add_argument("--video-ffmpeg-path")
    video.add_argument("--video-ffprobe-path")
    video.add_argument(
        "--video-ocr",
        choices=("auto", "never"),
        default="auto",
        help="run bounded existing image OCR over ephemeral sampled frames",
    )
    video.add_argument(
        "--video-ocr-lang",
        default=None,
        help="frame OCR languages; defaults to --ocr-lang",
    )
    video.add_argument(
        "--video-ocr-profile",
        choices=OCR_PROFILE_CHOICES,
        default=None,
        help="frame OCR profile; defaults to --ocr-profile",
    )
    video.add_argument("--video-ocr-timeout", type=float, default=12.0)
    video.add_argument("--video-search", metavar="QUERY")
    video.add_argument("--video-search-limit", type=int, default=20)
    video.add_argument(
        "--video-status",
        action="store_true",
        help="show the dedicated video owner without opening media",
    )
    video.add_argument(
        "--video-doctor",
        action="store_true",
        help="check FFmpeg, FFprobe and configured frame OCR without reading videos",
    )


def validate_video_arguments(args: argparse.Namespace) -> None:
    if args.video_max_documents is not None and args.video_max_documents < 1:
        raise SystemExit("--video-max-count must be positive")
    if args.video_max_file_bytes is not None and args.video_max_file_bytes < 1:
        raise SystemExit("--video-max-mb must be positive")
    for name in (
        "video_max_duration_seconds",
        "video_interval_seconds",
        "video_probe_timeout",
        "video_discovery_timeout",
        "video_frame_timeout",
        "video_file_timeout",
        "video_ocr_timeout",
        "video_worker_memory_mb",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if not 1 <= args.video_max_frames <= MAX_VIDEO_FRAMES:
        raise SystemExit(f"--video-max-frames must be between 1 and {MAX_VIDEO_FRAMES}")
    if not 0.0 < args.video_scene_threshold < 1.0:
        raise SystemExit("--video-scene-threshold must be between 0 and 1")
    if not 1 <= args.video_max_frame_pixels <= MAX_VIDEO_FRAME_PIXELS:
        raise SystemExit(f"--video-max-frame-pixels must be between 1 and {MAX_VIDEO_FRAME_PIXELS}")
    if args.video_max_frame_side < 1:
        raise SystemExit("--video-max-frame-side must be positive")
    if args.video_ocr_lang is not None and not args.video_ocr_lang.strip("+"):
        raise SystemExit("--video-ocr-lang must be non-empty")
    if not 1 <= args.video_search_limit <= 1000:
        raise SystemExit("--video-search-limit must be between 1 and 1000")
    if args.video_search is not None and not args.video_search.strip():
        raise SystemExit("--video-search must be non-empty")


def validate_video_direct_operation(args: argparse.Namespace) -> None:
    actions = selected_direct_operations(args, family=DirectOperationFamily.VIDEO)
    if not actions:
        return
    if args.apply:
        raise SystemExit("video direct actions cannot be combined with file-action --apply")
    if args.route != "none":
        raise SystemExit("video direct actions cannot be combined with --route")


_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.cli_video_surface')
