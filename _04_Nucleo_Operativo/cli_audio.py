"""Direct read-only Audio CLI operations."""

from __future__ import annotations

import argparse
import json
import sqlite3

__all__ = ["run_audio_doctor", "run_audio_search"]


# region [01] Transcript queries


def run_audio_search(args: argparse.Namespace) -> int:
    """Search indexed transcript text without loading Whisper."""

    from neocortex.capabilities.formats.audio.route import search_audio_state

    try:
        results = search_audio_state(
            args.state_directory / "audio.sqlite3",
            args.audio_search,
            args.audio_search_limit,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR audio-search {exc}")
        return 2
    for result in results:
        print(
            f"AUDIO path={result['path']} language={result['language'] or '-'} "
            f"duration={result['duration_seconds'] or 0:.3f} "
            f"model={result['model_name']} snippet={result['snippet']}"
        )
    return 0


# endregion [01]


# region [02] Dependency diagnostics


def run_audio_doctor(args: argparse.Namespace) -> int:
    """Inspect dependencies only; never load or download model weights."""

    from neocortex.capabilities.formats.audio.whisper import audio_runtime_doctor

    report = audio_runtime_doctor(
        device=args.whisper_device,
        compute_type=args.whisper_compute_type,
        ffprobe_path=args.ffprobe_path,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") else 2


# endregion [02]
