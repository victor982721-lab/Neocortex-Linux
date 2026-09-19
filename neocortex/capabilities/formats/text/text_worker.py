"""Bounded stdin/stdout adapter for direct Text extraction callers."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from .text_route import TextRouteConfig, _extract_builtin
from neocortex.capabilities.broker import CapabilitySelection
from neocortex.capabilities.runtime import TEXT_BUILTIN_IMPLEMENTATION_ID


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mime", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--max-input-bytes", type=int, required=True)
    parser.add_argument("--max-text-chars", type=int, required=True)
    parser.add_argument("--extractor", choices=("_extract_builtin",), required=True)
    args = parser.parse_args()
    try:
        payload = sys.stdin.buffer.read(args.max_input_bytes + 1)
        if len(payload) > args.max_input_bytes:
            raise ValueError("Text input exceeds its admitted input bound")
        selection = SimpleNamespace(
            selected=SimpleNamespace(implementation_id=TEXT_BUILTIN_IMPLEMENTATION_ID)
        )
        result = _extract_builtin(
            payload, args.mime, args.source,
            TextRouteConfig(Path("unused"), max_text_chars=args.max_text_chars),
            cast(CapabilitySelection, selection),
        )
        response = {"ok": True, "result": asdict(result)}
    except Exception as exc:
        response = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)[:4096]}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
