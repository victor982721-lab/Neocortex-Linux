"""Direct read-only CLI operations for dedicated visual-video evidence."""

from __future__ import annotations
import argparse
import json
import sqlite3

__all__ = ("run_video_doctor", "run_video_search", "run_video_status")


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


def run_video_search(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.video.state import search_video_state

    try:
        results = search_video_state(
            args.state_directory / "video.sqlite3",
            args.video_search,
            args.video_search_limit,
            audio_state_path=args.state_directory / "audio.sqlite3",
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR video-search {_safe_human(exc)}")
        return 2
    for result in results:
        confidence = result.get("ocr_mean_confidence")
        confidence_text = "-" if confidence is None else f"{float(confidence):.1f}"
        print(
            f"VIDEO path={_safe_human(result['path'])} "
            f"at={_safe_human(result['evidence'])} "
            f"channel={_safe_human(result['channel'])} confidence={confidence_text} "
            f"snippet={_safe_human(result['snippet'])}"
        )
    return 0


def run_video_status(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.video.state import video_state_status

    try:
        status = video_state_status(args.state_directory / "video.sqlite3")
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR video-status {_safe_human(exc)}")
        return 2
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0


def run_video_doctor(args: argparse.Namespace) -> int:
    from neocortex.capabilities.formats.video.frames import resolve_video_ffmpeg
    from neocortex.capabilities.formats.video.probe import resolve_video_ffprobe

    report: dict[str, object] = {"ok": False}
    failures: list[str] = []
    try:
        report["ffmpeg"] = resolve_video_ffmpeg(args.video_ffmpeg_path)
    except (OSError, ValueError) as exc:
        failures.append(f"ffmpeg:{type(exc).__name__}")
        report["ffmpeg"] = None
    try:
        report["ffprobe"] = resolve_video_ffprobe(args.video_ffprobe_path)
    except (OSError, ValueError) as exc:
        failures.append(f"ffprobe:{type(exc).__name__}")
        report["ffprobe"] = None
    try:
        from neocortex.capabilities.formats.image.document import (
            DocumentVerifierConfig,
            resolve_document_verifier,
        )

        runtime = resolve_document_verifier(
            DocumentVerifierConfig(
                mode=args.video_ocr,
                lang=args.video_ocr_lang or args.ocr_lang,
                profile=args.video_ocr_profile or args.ocr_profile,
                timeout_seconds=args.video_ocr_timeout,
                tesseract_cmd=args.tesseract_cmd,
                tessdata_dir=args.tessdata_dir,
            )
        )
        report["frame_ocr"] = {
            "enabled": runtime.enabled,
            "reason": runtime.unavailable_reason,
            "signature": runtime.signature,
            "profile": runtime.profile,
            "requested_languages": list(runtime.requested_languages),
            "osd_enabled": runtime.osd_enabled,
        }
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["frame_ocr"] = {
            "enabled": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
        if args.video_ocr != "never" and not isinstance(exc, ImportError):
            failures.append(f"frame_ocr:{type(exc).__name__}")
    report["failures"] = failures
    report["ok"] = not failures
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2
