"""Isolated native-text extraction for a bounded PDF held inside a ZIP."""

from __future__ import annotations

import argparse
import json
import sys
from typing import cast


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--max-input-bytes", type=int, required=True)
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--max-chars", type=int, required=True)
    return parser


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_input_bytes < 1 or args.max_pages < 1 or args.max_chars < 1:
        _emit({"ok": False, "reason": "invalid_worker_limits"})
        return 2
    payload = sys.stdin.buffer.read(args.max_input_bytes + 1)
    if len(payload) > args.max_input_bytes:
        _emit({"ok": False, "reason": "pdf_input_limit"})
        return 2
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError:
        _emit({"ok": False, "reason": "pdf_extractor_unavailable"})
        return 3

    try:
        document = fitz.open(stream=payload, filetype="pdf")
        try:
            page_count = document.page_count
            if page_count > args.max_pages:
                _emit(
                    {
                        "ok": False,
                        "pages": page_count,
                        "reason": "pdf_page_limit",
                    }
                )
                return 2
            parts: list[str] = []
            characters = 0
            truncated = False
            for page_number in range(page_count):
                page_text = cast(
                    str,
                    document.load_page(page_number).get_text("text"),
                )
                if not page_text:
                    continue
                remaining = args.max_chars - characters
                if remaining <= 0:
                    truncated = True
                    break
                if len(page_text) > remaining:
                    page_text = page_text[:remaining]
                    truncated = True
                parts.append(page_text)
                characters += len(page_text)
                if truncated:
                    break
        finally:
            document.close()
    except Exception as exc:  # native parser errors are data, not worker crashes
        _emit(
            {
                "ok": False,
                "reason": "pdf_parse_error",
                "detail": f"{type(exc).__name__}: {exc}"[:500],
            }
        )
        return 2

    _emit(
        {
            "ok": True,
            "pages": page_count,
            "text": "\n".join(parts),
            "truncated": truncated,
        }
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
