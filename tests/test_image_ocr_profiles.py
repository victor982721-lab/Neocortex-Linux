from __future__ import annotations

import io
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from _04_Nucleo_Operativo.image_document import (
    DocumentVerifierConfig,
    DocumentVerifierRuntime,
    resolve_document_verifier,
    verify_document_text,
)
from _04_Nucleo_Operativo.processing_provenance import (
    TesseractRuntimeProvenance,
)
from _04_Nucleo_Operativo.image_route import ImageRoute, ImageRouteConfig


def _image(root: Path, *, size: tuple[int, int] = (1_800, 1_200)) -> Path:
    path = root / "ocr-profile.png"
    with Image.new("RGB", size, "white") as image:
        image.save(path)
    return path


def _result(stdout: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["tesseract-test"],
        returncode=0,
        stdout=stdout,
        stderr=b"",
    )


def _osd(script: str) -> subprocess.CompletedProcess[bytes]:
    return _result(
        (
            "Orientation in degrees: 0\n"
            "Rotate: 0\n"
            "Orientation confidence: 13.5\n"
            f"Script: {script}\n"
            "Script confidence: 8.25\n"
        ).encode("utf-8")
    )


def _tsv(words: list[tuple[str, float]]) -> subprocess.CompletedProcess[bytes]:
    rows = [
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext"
    ]
    for index, (word, confidence) in enumerate(words, start=1):
        rows.append(
            f"5\t1\t1\t1\t1\t{index}\t1\t1\t80\t20\t{confidence}\t{word}"
        )
    return _result(("\n".join(rows) + "\n").encode("utf-8"))


def _runtime() -> DocumentVerifierRuntime:
    return DocumentVerifierRuntime(
        enabled=True,
        lang="spa+eng",
        timeout_seconds=12.0,
        tesseract_cmd="tesseract-test",
        tessdata_dir=None,
        signature="test-profile",
        provenance="test-tesseract",
        profile="auto-multilingual",
        requested_languages=("spa", "eng", "deu", "chi_sim", "chi_tra", "osd"),
        traineddata_hashes=(
            ("spa", "spa-hash"),
            ("eng", "eng-hash"),
            ("deu", "deu-hash"),
            ("chi_sim", "sim-hash"),
            ("chi_tra", "tra-hash"),
            ("osd", "osd-hash"),
        ),
        osd_enabled=True,
    )


def test_latin_profile_uses_deu_without_han_and_preserves_more_than_768px() -> None:
    commands: list[list[str]] = []
    sample_sizes: list[tuple[int, int]] = []

    def run(command, *, input_bytes: bytes, **_kwargs):
        commands.append(command)
        with Image.open(io.BytesIO(input_bytes)) as sample:
            sample_sizes.append(sample.size)
        if command[command.index("-l") + 1] == "osd":
            return _osd("Latin")
        return _tsv([("Prüfbericht", 94.0), ("Transformator", 92.0)])

    with tempfile.TemporaryDirectory() as temporary:
        path = _image(Path(temporary))
        with patch(
            "_04_Nucleo_Operativo.image_document.run_bounded_capture",
            side_effect=run,
        ):
            evidence = verify_document_text(path, _runtime())

    recognition_languages = [
        command[command.index("-l") + 1]
        for command in commands
        if command[command.index("-l") + 1] != "osd"
    ]
    assert recognition_languages == ["spa+eng+deu"]
    assert all("chi_sim" not in value and "chi_tra" not in value for value in recognition_languages)
    assert max(max(size) for size in sample_sizes) > 768
    assert evidence.available
    assert evidence.effective_languages == ("spa", "eng", "deu")
    assert evidence.detected_script == "Latin"
    assert evidence.orientation_confidence == 13.5
    assert evidence.recognition_attempts == 1
    assert not evidence.fallback_attempted


def test_generic_han_runs_only_simplified_then_traditional_bounded_fallback() -> None:
    responses = iter(
        (
            _osd("Han"),
            _tsv([("變壓器", 88.0), ("檢查", 88.0)]),
            _tsv([("變壓器", 96.0), ("檢查", 96.0)]),
        )
    )
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        return next(responses)

    with tempfile.TemporaryDirectory() as temporary:
        path = _image(Path(temporary), size=(640, 480))
        with patch(
            "_04_Nucleo_Operativo.image_document.run_bounded_capture",
            side_effect=run,
        ):
            evidence = verify_document_text(path, _runtime())

    languages = [command[command.index("-l") + 1] for command in commands]
    assert languages == ["osd", "chi_sim+eng", "chi_tra+eng"]
    assert evidence.available
    assert evidence.effective_languages == ("chi_tra", "eng")
    assert evidence.fallback_attempted
    assert evidence.fallback_languages == ("chi_tra", "eng")
    assert evidence.fallback_reason == "traditional_han_signal"
    assert evidence.recognition_attempts == 2
    assert evidence.traineddata_hashes[-2:] == (
        ("chi_tra", "tra-hash"),
        ("osd", "osd-hash"),
    )


def test_han_text_is_preserved_below_latin_word_confidence_cutoff() -> None:
    responses = iter(
        (
            _osd("Han"),
            _tsv([("变压器", 88.0), ("检测报告", 14.0)]),
        )
    )
    with tempfile.TemporaryDirectory() as temporary:
        path = _image(Path(temporary), size=(640, 480))
        with patch(
            "_04_Nucleo_Operativo.image_document.run_bounded_capture",
            side_effect=lambda *_args, **_kwargs: next(responses),
        ):
            evidence = verify_document_text(path, _runtime())

    assert evidence.available
    assert evidence.recognized_text == "变压器 检测报告"
    assert evidence.character_count == 7
    assert evidence.mean_confidence == 51.0
    assert evidence.recognition_attempts == 1


def test_multilingual_preflight_requests_all_potential_packs_and_reports_missing() -> None:
    component = {
        "name": "tesseract",
        "kind": "native-executable",
        "status": "missing-languages",
        "requested_languages": [
            "spa",
            "eng",
            "deu",
            "chi_sim",
            "chi_tra",
            "osd",
        ],
        "traineddata": [],
    }
    unavailable = TesseractRuntimeProvenance(
        False,
        "tesseract-test",
        None,
        "5.5.0",
        ("spa", "eng", "osd"),
        json.dumps(component),
        "missing OCR languages: deu, chi_sim, chi_tra",
    )
    with patch(
        "_04_Nucleo_Operativo.image_document.resolve_tesseract_runtime",
        return_value=unavailable,
    ) as resolve:
        runtime = resolve_document_verifier(
            DocumentVerifierConfig(profile="auto-multilingual")
        )

    assert not runtime.enabled
    assert runtime.unavailable_reason == "missing OCR languages: deu, chi_sim, chi_tra"
    assert runtime.requested_languages == (
        "spa",
        "eng",
        "deu",
        "chi_sim",
        "chi_tra",
        "osd",
    )
    assert resolve.call_args.kwargs["language"] == (
        "spa+eng+deu+chi_sim+chi_tra+osd"
    )


def test_configured_image_profile_fails_closed_when_requested_pack_is_missing() -> None:
    runtime = DocumentVerifierRuntime(
        enabled=False,
        lang="spa+eng",
        timeout_seconds=12.0,
        tesseract_cmd="tesseract-test",
        tessdata_dir=None,
        signature="missing-spa",
        unavailable_reason="missing OCR languages: spa",
        profile="configured",
        requested_languages=("spa", "eng"),
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = ImageRouteConfig(root / "image.sqlite3", root)
        with patch(
            "_04_Nucleo_Operativo.image_route.resolve_document_verifier",
            return_value=runtime,
        ):
            with pytest.raises(RuntimeError, match="missing OCR languages: spa"):
                ImageRoute(config, object(), 1)
