"""Isolated native/OCR extraction for bounded PDF and image ZIP members."""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from typing import Any, cast


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--max-input-bytes", type=int, required=True)
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--max-chars", type=int, required=True)
    parser.add_argument("--kind", choices=("pdf", "image"), default="pdf")
    parser.add_argument("--ocr-mode", choices=("auto", "never", "always"), default="auto")
    parser.add_argument("--ocr-lang", default="spa+eng")
    parser.add_argument("--ocr-dpi", type=int, default=200)
    parser.add_argument("--ocr-max-pages", type=int, default=50)
    parser.add_argument("--max-render-pixels", type=int, default=40_000_000)
    parser.add_argument("--ocr-timeout", type=float, default=30.0)
    parser.add_argument("--tesseract-cmd")
    parser.add_argument("--tessdata-dir")
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


def _ocr_config(args: argparse.Namespace) -> str:
    return f'--tessdata-dir "{args.tessdata_dir}"' if args.tessdata_dir else ""


def _prepare_ocr(args: argparse.Namespace) -> tuple[Any | None, str | None]:
    try:
        import pytesseract  # type: ignore[import-untyped]
    except ImportError:
        return None, "ocr_adapter_unavailable"
    command = args.tesseract_cmd or shutil.which("tesseract")
    if command is None:
        return None, "ocr_executable_unavailable"
    pytesseract.pytesseract.tesseract_cmd = command
    try:
        available = set(pytesseract.get_languages(config=_ocr_config(args)))
    except Exception as exc:
        return None, f"ocr_runtime_error:{type(exc).__name__}"
    requested = {part for part in str(args.ocr_lang).split("+") if part}
    if not requested or not requested <= available:
        return None, "ocr_language_unavailable"
    return pytesseract, None


def _bounded_ocr_image(image: Any, args: argparse.Namespace, pytesseract: Any) -> str:
    width, height = image.size
    if width < 1 or height < 1:
        raise ValueError("image has invalid dimensions")
    if width * height > args.max_render_pixels:
        scale = (args.max_render_pixels / float(width * height)) ** 0.5
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))
        image = image.resize((width, height))
    if image.mode not in {"L", "RGB"}:
        image = image.convert("RGB")
    return cast(
        str,
        pytesseract.image_to_string(
            image,
            lang=args.ocr_lang,
            timeout=args.ocr_timeout,
            config=_ocr_config(args),
        ),
    )


def _extract_image(payload: bytes, args: argparse.Namespace) -> dict[str, object]:
    try:
        from PIL import Image
    except ImportError:
        return {"ok": False, "reason": "image_decoder_unavailable"}
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            width, height = image.size
            if args.ocr_mode == "never":
                return {
                    "ok": True,
                    "extraction_mode": "metadata",
                    "height": height,
                    "text": "",
                    "truncated": False,
                    "width": width,
                }
            pytesseract, reason = _prepare_ocr(args)
            if pytesseract is None:
                return {"ok": False, "reason": reason or "ocr_runtime_unavailable"}
            text = _bounded_ocr_image(image, args, pytesseract)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "image_ocr_error",
            "detail": f"{type(exc).__name__}: {exc}"[:500],
        }
    truncated = len(text) > args.max_chars
    return {
        "ok": True,
        "extraction_mode": "ocr",
        "height": height,
        "text": text[: args.max_chars],
        "truncated": truncated,
        "width": width,
    }


def _ocr_pdf_page(page: Any, fitz: Any, args: argparse.Namespace, pytesseract: Any) -> str:
    requested_scale = max(1.0, args.ocr_dpi / 72.0)
    rect = page.rect
    pixels = max(1.0, rect.width * rect.height * requested_scale**2)
    scale = requested_scale
    if pixels > args.max_render_pixels:
        scale *= (args.max_render_pixels / pixels) ** 0.5
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        alpha=False,
        colorspace=fitz.csRGB,
    )
    try:
        from PIL import Image

        image = Image.frombytes(
            "RGB",
            (pixmap.width, pixmap.height),
            pixmap.samples,
        )
        try:
            return _bounded_ocr_image(image, args, pytesseract)
        finally:
            image.close()
    finally:
        del pixmap


def _extract_pdf(payload: bytes, args: argparse.Namespace) -> dict[str, object]:
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError:
        return {"ok": False, "reason": "pdf_extractor_unavailable"}

    try:
        document = fitz.open(stream=payload, filetype="pdf")
        try:
            page_count = document.page_count
            if page_count > args.max_pages:
                return {
                    "ok": False,
                    "pages": page_count,
                    "reason": "pdf_page_limit",
                }
            parts: list[str] = []
            characters = 0
            truncated = False
            ocr_pages = 0
            pytesseract = None
            ocr_unavailable_reason: str | None = None
            ocr_prepared = False
            for page_number in range(page_count):
                page = document.load_page(page_number)
                page_text = cast(str, page.get_text("text"))
                wants_ocr = args.ocr_mode == "always" or (
                    args.ocr_mode == "auto" and len(page_text.strip()) < 40
                )
                if wants_ocr and ocr_pages < args.ocr_max_pages:
                    if not ocr_prepared:
                        pytesseract, ocr_unavailable_reason = _prepare_ocr(args)
                        ocr_prepared = True
                    if pytesseract is None:
                        if not page_text.strip():
                            return {
                                "ok": False,
                                "pages": page_count,
                                "reason": (ocr_unavailable_reason or "ocr_runtime_unavailable"),
                            }
                    else:
                        ocr_text = _ocr_pdf_page(page, fitz, args, pytesseract)
                        ocr_pages += 1
                        if ocr_text.strip() or args.ocr_mode == "always":
                            page_text = ocr_text
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
    except Exception as exc:  # parser/OCR errors are data, not worker crashes
        return {
            "ok": False,
            "reason": "pdf_parse_or_ocr_error",
            "detail": f"{type(exc).__name__}: {exc}"[:500],
        }
    return {
        "ok": True,
        "extraction_mode": "ocr" if ocr_pages else "native",
        "ocr_pages": ocr_pages,
        "pages": page_count,
        "text": "\n".join(parts),
        "truncated": truncated,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        any(
            value < 1
            for value in (
                args.max_input_bytes,
                args.max_pages,
                args.max_chars,
                args.ocr_dpi,
                args.ocr_max_pages,
                args.max_render_pixels,
            )
        )
        or args.ocr_timeout <= 0
    ):
        _emit({"ok": False, "reason": "invalid_worker_limits"})
        return 2
    payload = sys.stdin.buffer.read(args.max_input_bytes + 1)
    if len(payload) > args.max_input_bytes:
        _emit({"ok": False, "reason": "input_limit"})
        return 2
    result = _extract_pdf(payload, args) if args.kind == "pdf" else _extract_image(payload, args)
    _emit(result)
    if not result.get("ok"):
        return 3 if str(result.get("reason", "")).endswith("unavailable") else 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
