"""Bounded legacy Office extraction behind an isolated worker process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .bounded_subprocess import SubprocessOutputLimitError, run_bounded_capture

_MAX_BACKEND_BYTES = 16 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--kind", choices=("doc", "xls", "ppt"), required=True)
    parser.add_argument("--max-input-bytes", type=int, required=True)
    parser.add_argument("--max-chars", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument(
        "--backend",
        choices=("soffice", "libreoffice", "catdoc", "xls2csv", "catppt"),
        required=True,
    )
    parser.add_argument("--backend-command", required=True)
    parser.add_argument("--backend-sha256", required=True)
    parser.add_argument("--backend-size", type=int, required=True)
    return parser


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _decode_output(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return payload.decode(encoding, "strict")
        except UnicodeError:
            continue
    return payload.decode("utf-8", "replace")


def _selected_ooxml_part(name: str, kind: str) -> bool:
    lower = name.replace("\\", "/").casefold()
    if kind == "xls":
        return lower in {"xl/sharedstrings.xml", "xl/workbook.xml"} or (
            lower.startswith("xl/worksheets/") and lower.endswith(".xml")
        )
    return lower.startswith(("ppt/slides/", "ppt/notesslides/")) and lower.endswith(".xml")


def _ooxml_text(path: Path, kind: str, max_chars: int) -> tuple[str, bool]:
    parts: list[str] = []
    characters = 0
    truncated = False
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 20_000:
            raise ValueError("converted Office document exceeds member limit")
        for info in archive.infolist():
            if info.is_dir() or not _selected_ooxml_part(info.filename, kind):
                continue
            if info.file_size > 64 * 1024 * 1024:
                raise ValueError("converted Office XML part exceeds size limit")
            payload = archive.read(info)
            root = ET.fromstring(payload)
            text = "\n".join(value.strip() for value in root.itertext() if value.strip())
            if not text:
                continue
            remaining = max_chars - characters - (1 if parts else 0)
            if remaining <= 0:
                truncated = True
                break
            if len(text) > remaining:
                text = text[:remaining]
                truncated = True
            parts.append(text)
            characters += len(text) + (1 if len(parts) > 1 else 0)
            if truncated:
                break
    return "\n".join(parts), truncated


def _fallback_extract(
    source: Path,
    executable: Path,
    max_chars: int,
    timeout: float,
) -> str | None:
    result = run_bounded_capture(
        (str(executable), str(source)),
        timeout_seconds=timeout,
        stdout_limit_bytes=max(64 * 1024, max_chars * 6 + 64 * 1024),
        stderr_limit_bytes=256 * 1024,
    )
    if result.returncode != 0:
        return None
    return _decode_output(result.stdout)


def _libreoffice_extract(
    source: Path,
    output: Path,
    profile: Path,
    args: argparse.Namespace,
    executable: Path,
) -> tuple[str, bool, str] | None:
    target = "txt:Text" if args.kind == "doc" else "xlsx" if args.kind == "xls" else "pptx"
    environment = dict(os.environ)
    environment.update(
        HOME=str(profile.parent),
        USERPROFILE=str(profile.parent),
    )
    command = (
        str(executable),
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        target,
        "--outdir",
        str(output),
        str(source),
    )
    result = run_bounded_capture(
        command,
        timeout_seconds=args.timeout,
        stdout_limit_bytes=256 * 1024,
        stderr_limit_bytes=256 * 1024,
        cwd=str(source.parent),
        environment=environment,
    )
    if result.returncode != 0:
        return None
    converted = output / f"source.{target.split(':', 1)[0]}"
    if not converted.is_file() or converted.stat().st_size > args.max_input_bytes * 8 + 64 * 1024:
        return None
    if args.kind == "doc":
        value = _decode_output(converted.read_bytes())
        return value[: args.max_chars], len(value) > args.max_chars, "libreoffice_txt"
    value, truncated = _ooxml_text(converted, args.kind, args.max_chars)
    return value, truncated, f"libreoffice_{target}"


def _verified_backend(args: argparse.Namespace) -> Path:
    allowed = {
        "doc": {"soffice", "libreoffice", "catdoc"},
        "xls": {"soffice", "libreoffice", "xls2csv"},
        "ppt": {"soffice", "libreoffice", "catppt"},
    }
    if args.backend not in allowed[args.kind]:
        raise ValueError("legacy Office backend is incompatible with document kind")
    if args.backend_size < 0 or args.backend_size > _MAX_BACKEND_BYTES:
        raise ValueError("legacy Office backend size is invalid")
    if len(args.backend_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in args.backend_sha256
    ):
        raise ValueError("legacy Office backend SHA-256 is invalid")
    command = Path(args.backend_command).expanduser().resolve(strict=True)
    before = command.stat()
    if not command.is_file() or not os.access(command, os.X_OK):
        raise ValueError("legacy Office backend is not an executable file")
    if before.st_size != args.backend_size:
        raise ValueError("legacy Office backend size changed after selection")
    digest = hashlib.sha256()
    hashed_bytes = 0
    with command.open("rb") as stream:
        while chunk := stream.read(min(1024 * 1024, _MAX_BACKEND_BYTES + 1 - hashed_bytes)):
            hashed_bytes += len(chunk)
            if hashed_bytes > _MAX_BACKEND_BYTES:
                raise ValueError("legacy Office backend exceeds the verified size limit")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or digest.hexdigest() != args.backend_sha256
    ):
        raise ValueError("legacy Office backend identity changed after selection")
    return command


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_input_bytes < 1 or args.max_chars < 1 or args.timeout <= 0:
        _emit({"ok": False, "reason": "invalid_worker_limits"})
        return 2
    payload = sys.stdin.buffer.read(args.max_input_bytes + 1)
    if len(payload) > args.max_input_bytes:
        _emit({"ok": False, "reason": "legacy_office_input_limit"})
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="neocortex-legacy-office-") as temporary:
            root = Path(temporary)
            source = root / f"source.{args.kind}"
            output = root / "output"
            profile = root / "profile"
            output.mkdir()
            profile.mkdir()
            source.write_bytes(payload)
            backend = _verified_backend(args)
            if args.backend in {"soffice", "libreoffice"}:
                extracted = _libreoffice_extract(
                    source,
                    output,
                    profile,
                    args,
                    backend,
                )
            else:
                fallback = _fallback_extract(
                    source,
                    backend,
                    args.max_chars,
                    args.timeout,
                )
                extracted = (
                    None
                    if fallback is None
                    else (
                        fallback[: args.max_chars],
                        len(fallback) > args.max_chars,
                        args.backend,
                    )
                )
            if extracted is None:
                _emit({"ok": False, "reason": "legacy_office_selected_backend_failed"})
                return 3
            if _verified_backend(args) != backend:
                raise ValueError("legacy Office backend changed during extraction")
    except (
        OSError,
        RuntimeError,
        SubprocessOutputLimitError,
        ValueError,
        zipfile.BadZipFile,
    ) as exc:
        _emit(
            {
                "ok": False,
                "reason": "legacy_office_extraction_error",
                "detail": f"{type(exc).__name__}: {exc}"[:500],
            }
        )
        return 2
    text, truncated, conversion = extracted
    _emit(
        {
            "ok": True,
            "backend": args.backend,
            "conversion": conversion,
            "text": text,
            "truncated": truncated,
        }
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
