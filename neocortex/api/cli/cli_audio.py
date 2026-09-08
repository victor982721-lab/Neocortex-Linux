"""Direct read-only Audio CLI operations."""

from __future__ import annotations
import argparse
import json
import sqlite3

__all__ = ["run_audio_doctor", "run_audio_search"]


def _safe_human(value: object, *, limit: int = 4096) -> str:
    """Keep corpus-controlled text from writing terminal controls or newlines."""

    escaped: list[str] = []
    for character in str(value):
        code = ord(character)
        if character == "\t" or 32 <= code < 127:
            escaped.append(character)
        elif code <= 0xFFFF:
            escaped.append(f"\\u{code:04x}")
        else:
            escaped.append(f"\\U{code:08x}")
    return "".join(escaped)[:limit]


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
        print(f"ERROR audio-search {_safe_human(exc)}")
        return 2
    for result in results:
        print(
            f"AUDIO path={_safe_human(result['path'])} "
            f"language={_safe_human(result['language'] or '-')} "
            f"duration={result['duration_seconds'] or 0:.3f} "
            f"model={_safe_human(result['model_name'])} "
            f"snippet={_safe_human(result['snippet'])}"
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
