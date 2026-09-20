"""In-process contracts for the bounded Archive text worker."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from neocortex.capabilities.formats.archive import text_worker as worker


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "kind": "pdf",
        "max_chars": 100,
        "max_input_bytes": 1_000,
        "max_pages": 10,
        "max_render_pixels": 40_000,
        "ocr_dpi": 72,
        "ocr_lang": "spa+eng",
        "ocr_max_pages": 10,
        "ocr_mode": "auto",
        "ocr_timeout": 5.0,
        "tessdata_dir": None,
        "tesseract_cmd": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _pytesseract_module(
    *,
    languages: Callable[..., list[str]] | None = None,
    image_to_string: Callable[..., str] | None = None,
) -> ModuleType:
    module = ModuleType("pytesseract")
    module.pytesseract = SimpleNamespace(tesseract_cmd=None)  # type: ignore[attr-defined]
    module.get_languages = languages or (lambda **_kwargs: ["eng", "spa"])  # type: ignore[attr-defined]
    module.image_to_string = image_to_string or (lambda *_args, **_kwargs: "ocr")  # type: ignore[attr-defined]
    return module


class _FakeImage:
    def __init__(
        self,
        *,
        size: tuple[int, int] = (40, 20),
        mode: str = "RGB",
        load_error: Exception | None = None,
    ) -> None:
        self.size = size
        self.mode = mode
        self.load_error = load_error
        self.closed = False
        self.converted_to: str | None = None
        self.loaded = False
        self.resized_to: tuple[int, int] | None = None

    def __enter__(self) -> _FakeImage:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def convert(self, mode: str) -> _FakeImage:
        self.converted_to = mode
        self.mode = mode
        return self

    def load(self) -> None:
        if self.load_error is not None:
            raise self.load_error
        self.loaded = True

    def resize(self, size: tuple[int, int]) -> _FakeImage:
        self.resized_to = size
        self.size = size
        return self


def _install_pil(
    monkeypatch: pytest.MonkeyPatch,
    *,
    opened: list[_FakeImage] | None = None,
    rendered: list[_FakeImage] | None = None,
) -> SimpleNamespace:
    opened_images = opened if opened is not None else [_FakeImage()]
    rendered_images = rendered if rendered is not None else []

    def open_image(_stream: object) -> _FakeImage:
        return opened_images.pop(0)

    def frombytes(_mode: str, size: tuple[int, int], _samples: bytes) -> _FakeImage:
        image = _FakeImage(size=size)
        rendered_images.append(image)
        return image

    image_api = SimpleNamespace(open=open_image, frombytes=frombytes)
    package = ModuleType("PIL")
    package.Image = image_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PIL", package)
    return image_api


class _FakePage:
    def __init__(self, text: str, *, width: float = 100.0, height: float = 100.0) -> None:
        self.text = text
        self.rect = SimpleNamespace(width=width, height=height)
        self.render_arguments: dict[str, object] | None = None

    def get_text(self, _kind: str) -> str:
        return self.text

    def get_pixmap(self, **kwargs: object) -> SimpleNamespace:
        self.render_arguments = kwargs
        return SimpleNamespace(width=10, height=10, samples=b"\0" * 300)


class _FakeDocument:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.page_count = len(pages)
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def load_page(self, page_number: int) -> _FakePage:
        return self.pages[page_number]


def _install_fitz(
    monkeypatch: pytest.MonkeyPatch,
    document: _FakeDocument,
) -> ModuleType:
    module = ModuleType("pymupdf")
    module.csRGB = object()  # type: ignore[attr-defined]
    module.Matrix = lambda x, y: (x, y)  # type: ignore[attr-defined]
    module.open = lambda **_kwargs: document  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymupdf", module)
    return module


def test_prepare_ocr_reports_missing_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pytesseract", None)

    assert worker._prepare_ocr(_args()) == (None, "ocr_adapter_unavailable")


def test_prepare_ocr_validates_executable_runtime_and_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _pytesseract_module()
    monkeypatch.setitem(sys.modules, "pytesseract", module)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)
    assert worker._prepare_ocr(_args()) == (None, "ocr_executable_unavailable")

    def runtime_error(**_kwargs: object) -> list[str]:
        raise RuntimeError("not ready")

    module.get_languages = runtime_error  # type: ignore[attr-defined]
    assert worker._prepare_ocr(_args(tesseract_cmd="/bin/tesseract")) == (
        None,
        "ocr_runtime_error:RuntimeError",
    )

    module.get_languages = lambda **_kwargs: ["eng"]  # type: ignore[attr-defined]
    assert worker._prepare_ocr(_args(tesseract_cmd="/bin/tesseract")) == (
        None,
        "ocr_language_unavailable",
    )
    assert worker._prepare_ocr(_args(tesseract_cmd="/bin/tesseract", ocr_lang="")) == (
        None,
        "ocr_language_unavailable",
    )

    module.get_languages = lambda **_kwargs: ["eng", "spa"]  # type: ignore[attr-defined]
    prepared, reason = worker._prepare_ocr(
        _args(tesseract_cmd="/bin/tesseract", tessdata_dir="/tess")
    )
    assert prepared is module
    assert reason is None
    assert module.pytesseract.tesseract_cmd == "/bin/tesseract"  # type: ignore[attr-defined]


def test_bounded_ocr_rejects_invalid_dimensions() -> None:
    with pytest.raises(ValueError, match="invalid dimensions"):
        worker._bounded_ocr_image(_FakeImage(size=(0, 3)), _args(), object())


def test_bounded_ocr_scales_and_normalizes_image() -> None:
    image = _FakeImage(size=(400, 200), mode="RGBA")
    calls: list[dict[str, object]] = []

    def image_to_string(received: object, **kwargs: object) -> str:
        calls.append({"image": received, **kwargs})
        return "bounded text"

    pytesseract = _pytesseract_module(image_to_string=image_to_string)
    result = worker._bounded_ocr_image(
        image,
        _args(max_render_pixels=20_000, tessdata_dir="/tess"),
        pytesseract,
    )

    assert result == "bounded text"
    assert image.resized_to == (200, 100)
    assert image.converted_to == "RGB"
    assert calls == [
        {
            "image": image,
            "lang": "spa+eng",
            "timeout": 5.0,
            "config": '--tessdata-dir "/tess"',
        }
    ]


def test_bounded_ocr_preserves_small_rgb_image() -> None:
    image = _FakeImage(size=(20, 10), mode="RGB")
    pytesseract = _pytesseract_module(image_to_string=lambda *_args, **_kwargs: "ok")

    assert worker._bounded_ocr_image(image, _args(), pytesseract) == "ok"
    assert image.resized_to is None
    assert image.converted_to is None


def test_extract_image_reports_missing_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)

    assert worker._extract_image(b"image", _args(kind="image")) == {
        "ok": False,
        "reason": "image_decoder_unavailable",
    }


def test_extract_image_returns_metadata_without_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _FakeImage(size=(12, 7))
    _install_pil(monkeypatch, opened=[image])

    assert worker._extract_image(b"image", _args(kind="image", ocr_mode="never")) == {
        "ok": True,
        "extraction_mode": "metadata",
        "height": 7,
        "text": "",
        "truncated": False,
        "width": 12,
    }
    assert image.loaded is True
    assert image.closed is True


def test_extract_image_reports_ocr_unavailability_and_decode_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _FakeImage()
    _install_pil(monkeypatch, opened=[image])
    monkeypatch.setattr(
        worker,
        "_prepare_ocr",
        lambda _args: (None, None),
    )
    assert worker._extract_image(b"image", _args(kind="image")) == {
        "ok": False,
        "reason": "ocr_runtime_unavailable",
    }

    broken = _FakeImage(load_error=OSError("bad pixels"))
    _install_pil(monkeypatch, opened=[broken])
    assert worker._extract_image(b"image", _args(kind="image")) == {
        "ok": False,
        "reason": "image_ocr_error",
        "detail": "OSError: bad pixels",
    }
    assert broken.closed is True


def test_extract_image_bounds_successful_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    image = _FakeImage(size=(8, 6))
    pytesseract = _pytesseract_module()
    _install_pil(monkeypatch, opened=[image])
    monkeypatch.setattr(worker, "_prepare_ocr", lambda _args: (pytesseract, None))
    monkeypatch.setattr(worker, "_bounded_ocr_image", lambda *_args: "abcdef")

    assert worker._extract_image(b"image", _args(kind="image", max_chars=3)) == {
        "ok": True,
        "extraction_mode": "ocr",
        "height": 6,
        "text": "abc",
        "truncated": True,
        "width": 8,
    }
    assert image.closed is True


@pytest.mark.parametrize(
    ("max_render_pixels", "expected_scale"),
    [(20_000, 1.0), (100, 0.1)],
)
def test_ocr_pdf_page_caps_rendering_and_closes_image(
    monkeypatch: pytest.MonkeyPatch,
    max_render_pixels: int,
    expected_scale: float,
) -> None:
    page = _FakePage("")
    document = _FakeDocument([page])
    fitz = _install_fitz(monkeypatch, document)
    rendered: list[_FakeImage] = []
    _install_pil(monkeypatch, rendered=rendered)
    monkeypatch.setattr(worker, "_bounded_ocr_image", lambda *_args: "page OCR")

    assert (
        worker._ocr_pdf_page(
            page,
            fitz,
            _args(max_render_pixels=max_render_pixels),
            object(),
        )
        == "page OCR"
    )
    assert page.render_arguments is not None
    assert page.render_arguments["matrix"] == pytest.approx((expected_scale, expected_scale))
    assert page.render_arguments["alpha"] is False
    assert page.render_arguments["colorspace"] is fitz.csRGB  # type: ignore[attr-defined]
    assert len(rendered) == 1
    assert rendered[0].closed is True


def test_extract_pdf_reports_missing_adapter_and_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    assert worker._extract_pdf(b"pdf", _args()) == {
        "ok": False,
        "reason": "pdf_extractor_unavailable",
    }

    document = _FakeDocument([_FakePage("one"), _FakePage("two")])
    _install_fitz(monkeypatch, document)
    assert worker._extract_pdf(b"pdf", _args(max_pages=1)) == {
        "ok": False,
        "pages": 2,
        "reason": "pdf_page_limit",
    }
    assert document.closed is True


@pytest.mark.parametrize(
    ("texts", "max_chars", "expected_text", "expected_truncated"),
    [
        ([""], 3, "", False),
        (["abc", "next"], 3, "abc", True),
        (["abcdef"], 3, "abc", True),
    ],
)
def test_extract_pdf_native_text_exercises_empty_exact_and_truncated_pages(
    monkeypatch: pytest.MonkeyPatch,
    texts: list[str],
    max_chars: int,
    expected_text: str,
    expected_truncated: bool,
) -> None:
    document = _FakeDocument([_FakePage(text) for text in texts])
    _install_fitz(monkeypatch, document)

    assert worker._extract_pdf(
        b"pdf",
        _args(ocr_mode="never", max_chars=max_chars),
    ) == {
        "ok": True,
        "extraction_mode": "native",
        "ocr_pages": 0,
        "pages": len(texts),
        "text": expected_text,
        "truncated": expected_truncated,
    }
    assert document.closed is True


def test_extract_pdf_reuses_unavailable_ocr_when_native_text_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _FakeDocument([_FakePage("first"), _FakePage("second")])
    _install_fitz(monkeypatch, document)
    preparations = 0

    def unavailable(_args: argparse.Namespace) -> tuple[None, str]:
        nonlocal preparations
        preparations += 1
        return None, "ocr_executable_unavailable"

    monkeypatch.setattr(worker, "_prepare_ocr", unavailable)

    result = worker._extract_pdf(b"pdf", _args())

    assert result["ok"] is True
    assert result["text"] == "first\nsecond"
    assert result["ocr_pages"] == 0
    assert preparations == 1


def test_extract_pdf_requires_ocr_for_empty_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _FakeDocument([_FakePage("")])
    _install_fitz(monkeypatch, document)
    monkeypatch.setattr(
        worker,
        "_prepare_ocr",
        lambda _args: (None, "ocr_executable_unavailable"),
    )

    assert worker._extract_pdf(b"pdf", _args()) == {
        "ok": False,
        "pages": 1,
        "reason": "ocr_executable_unavailable",
    }
    assert document.closed is True


def test_extract_pdf_uses_and_reuses_available_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _FakeDocument([_FakePage("first"), _FakePage("second")])
    _install_fitz(monkeypatch, document)
    pytesseract = object()
    preparations = 0
    ocr_texts = iter(("OCR first", ""))

    def prepare(_args: argparse.Namespace) -> tuple[object, None]:
        nonlocal preparations
        preparations += 1
        return pytesseract, None

    monkeypatch.setattr(worker, "_prepare_ocr", prepare)
    monkeypatch.setattr(worker, "_ocr_pdf_page", lambda *_args: next(ocr_texts))

    result = worker._extract_pdf(b"pdf", _args())

    assert result == {
        "ok": True,
        "extraction_mode": "ocr",
        "ocr_pages": 2,
        "pages": 2,
        "text": "OCR first\nsecond",
        "truncated": False,
    }
    assert preparations == 1
    assert document.closed is True


def test_extract_pdf_converts_parser_errors_to_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("pymupdf")

    def fail_open(**_kwargs: object) -> object:
        raise ValueError("broken document")

    module.open = fail_open  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymupdf", module)

    assert worker._extract_pdf(b"pdf", _args()) == {
        "ok": False,
        "reason": "pdf_parse_or_ocr_error",
        "detail": "ValueError: broken document",
    }


def _main_arguments(**overrides: object) -> list[str]:
    values: dict[str, object] = {
        "max-input-bytes": 10,
        "max-pages": 2,
        "max-chars": 20,
        "kind": "pdf",
    }
    values.update(overrides)
    return [item for key, value in values.items() for item in (f"--{key}", str(value))]


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    arguments: list[str],
) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(worker.sys, "stdout", stdout)
    status = worker.main(arguments)
    return status, json.loads(stdout.getvalue())


def test_main_rejects_invalid_limits_and_oversized_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, payload = _run_main(
        monkeypatch,
        b"",
        _main_arguments(**{"max-pages": 0}),
    )
    assert status == 2
    assert payload == {"ok": False, "reason": "invalid_worker_limits"}

    status, payload = _run_main(
        monkeypatch,
        b"",
        _main_arguments(**{"ocr-timeout": 0}),
    )
    assert status == 2
    assert payload == {"ok": False, "reason": "invalid_worker_limits"}

    status, payload = _run_main(monkeypatch, b"too long", _main_arguments(**{"max-input-bytes": 3}))
    assert status == 2
    assert payload == {"ok": False, "reason": "input_limit"}


@pytest.mark.parametrize(
    ("kind", "result", "expected_status"),
    [
        ("pdf", {"ok": False, "reason": "pdf_extractor_unavailable"}, 3),
        ("pdf", {"ok": False, "reason": "pdf_page_limit"}, 2),
        ("image", {"ok": True, "text": "done"}, 0),
    ],
)
def test_main_emits_extraction_result_and_maps_status(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    result: dict[str, object],
    expected_status: int,
) -> None:
    calls: list[tuple[str, bytes]] = []

    def extract_pdf(payload: bytes, _args: argparse.Namespace) -> dict[str, object]:
        calls.append(("pdf", payload))
        return result

    def extract_image(payload: bytes, _args: argparse.Namespace) -> dict[str, object]:
        calls.append(("image", payload))
        return result

    monkeypatch.setattr(worker, "_extract_pdf", extract_pdf)
    monkeypatch.setattr(worker, "_extract_image", extract_image)

    status, emitted = _run_main(monkeypatch, b"data", _main_arguments(kind=kind))

    assert status == expected_status
    assert emitted == result
    assert calls == [(kind, b"data")]


def test_worker_subprocess_stdout_is_one_json_object_for_pdf() -> None:
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "archive worker native text")
    payload = document.tobytes()
    document.close()

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "neocortex.capabilities.formats.archive.text_worker",
            "--max-input-bytes",
            str(len(payload)),
            "--max-pages",
            "2",
            "--max-chars",
            "100",
            "--kind",
            "pdf",
            "--ocr-mode",
            "never",
        ),
        cwd=Path(__file__).resolve().parents[1],
        input=payload,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    stdout = completed.stdout.decode("utf-8", "strict")
    result = json.loads(stdout)
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["extraction_mode"] == "native"
    assert result["text"] == "archive worker native text\n"
