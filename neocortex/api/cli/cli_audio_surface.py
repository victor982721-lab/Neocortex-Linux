"""Flat argument and validation contract for Audio/video Whisper CLI work."""


# region [01] Lightweight imports and public contract

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import argparse
from collections.abc import Callable
from pathlib import Path

from neocortex.platform_policy import (
    default_local_models_only,
    default_whisper_compute_type,
    default_whisper_device,
    default_whisper_model_cache,
)

from .cli_operations import DirectOperationFamily, selected_direct_operations

__all__ = [
    "register_audio_arguments",
    "validate_audio_arguments",
    "validate_audio_direct_operation",
]

# endregion [01]


# region [02] Stable flat argument registration


def register_audio_arguments(
    parser: argparse.ArgumentParser,
    *,
    megabyte_type: Callable[[str], int],
) -> None:
    """Append the existing flat Audio group using its legacy size converter."""

    audio = parser.add_argument_group("Audio/video Whisper transcription route")
    audio.add_argument(
        "--audio-max-mb",
        dest="audio_max_file_bytes",
        type=megabyte_type,
        default=None,
        metavar="MB",
        help="transcribe only media files at or below this decimal size",
    )
    audio.add_argument(
        "--audio-max-count",
        dest="audio_max_documents",
        type=int,
        default=None,
        metavar="N",
    )
    audio.add_argument(
        "--audio-max-duration-seconds",
        type=float,
        default=6 * 60 * 60,
    )
    audio.add_argument("--audio-max-transcript-chars", type=int, default=5_000_000)
    audio.add_argument("--audio-max-segments", type=int, default=100_000)
    audio.add_argument(
        "--audio-include-video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="also transcribe audio streams inside detected video containers",
    )
    audio.add_argument("--whisper-model", default="small")
    audio.add_argument(
        "--whisper-device",
        choices=("auto", "cpu", "cuda"),
        default=default_whisper_device(),
    )
    audio.add_argument(
        "--whisper-compute-type",
        default=default_whisper_compute_type(),
    )
    audio.add_argument(
        "--audio-language",
        default="auto",
        help="language code such as es or auto for Whisper language detection",
    )
    audio.add_argument("--whisper-beam-size", type=int, default=5)
    audio.add_argument(
        "--audio-vad",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="use voice activity detection before transcription",
    )
    audio.add_argument("--audio-file-timeout", type=float, default=3600.0)
    audio.add_argument(
        "--audio-worker-startup-timeout",
        type=float,
        default=1800.0,
    )
    audio.add_argument("--audio-worker-memory-mb", type=int, default=4096)
    audio.add_argument("--audio-memory-budget-mb", type=int, default=2048)
    audio.add_argument("--audio-min-free-memory-mb", type=int, default=2048)
    audio.add_argument("--audio-min-free-commit-mb", type=int, default=2048)
    audio.add_argument("--audio-memory-wait-timeout", type=float, default=300.0)
    audio.add_argument(
        "--audio-model-cache",
        type=Path,
        default=default_whisper_model_cache(),
        metavar="DIRECTORY",
    )
    audio.add_argument(
        "--audio-local-models-only",
        action=argparse.BooleanOptionalAction,
        default=default_local_models_only(),
        help="never download model weights; require an existing local model cache",
    )
    audio.add_argument(
        "--retry-audio-errors",
        action="store_true",
        help="force one new attempt for unchanged cached audio errors",
    )
    audio.add_argument("--ffprobe-path")
    audio.add_argument("--audio-search", metavar="QUERY")
    audio.add_argument("--audio-search-limit", type=int, default=20)
    audio.add_argument(
        "--audio-doctor",
        action="store_true",
        help="check Whisper and FFprobe without loading or downloading a model",
    )


# endregion [02]


# region [03] Stable validation and error precedence


def validate_audio_arguments(args: argparse.Namespace) -> None:
    """Validate Audio route and direct values in the established order."""

    if args.audio_max_documents is not None and args.audio_max_documents < 1:
        raise SystemExit("--audio-max-count must be positive")
    for name in (
        "audio_max_duration_seconds",
        "audio_max_transcript_chars",
        "audio_max_segments",
        "whisper_beam_size",
        "audio_file_timeout",
        "audio_worker_startup_timeout",
        "audio_worker_memory_mb",
        "audio_memory_budget_mb",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.audio_min_free_memory_mb < 0 or args.audio_min_free_commit_mb < 0:
        raise SystemExit("audio memory headroom cannot be negative")
    if args.audio_memory_wait_timeout < 0:
        raise SystemExit("--audio-memory-wait-timeout cannot be negative")
    if not args.whisper_model.strip():
        raise SystemExit("--whisper-model must be non-empty")
    if not args.whisper_compute_type.strip():
        raise SystemExit("--whisper-compute-type must be non-empty")
    if not args.audio_language.strip():
        raise SystemExit("--audio-language must be non-empty")
    if not 1 <= args.audio_search_limit <= 1000:
        raise SystemExit("--audio-search-limit must be between 1 and 1000")
    if args.audio_search is not None and not args.audio_search.strip():
        raise SystemExit("--audio-search must be non-empty")


def validate_audio_direct_operation(args: argparse.Namespace) -> None:
    """Reject framework mutations silently ignored by direct Audio actions."""

    audio_actions = bool(selected_direct_operations(args, family=DirectOperationFamily.AUDIO))
    if not audio_actions:
        return
    if args.apply:
        raise SystemExit("audio direct actions cannot be combined with file-action --apply")
    if args.route != "none":
        raise SystemExit("audio direct actions cannot be combined with --route")


# endregion [03]


_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.cli_audio_surface')
