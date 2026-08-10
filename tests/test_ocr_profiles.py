from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from PIL import Image

from _04_Nucleo_Operativo.ocr_image_preprocess import (
    bounded_grayscale,
    estimate_deskew_degrees,
)
from _04_Nucleo_Operativo.ocr_profiles import (
    HAN_SIMPLIFIED_LANGUAGES,
    HAN_TRADITIONAL_LANGUAGES,
    LATIN_LANGUAGES,
    OcrOrientation,
    contains_traditional_han,
    native_text_quality,
    parse_osd_output,
    resolve_ocr_profile,
    route_ocr_languages,
)


FIXTURE = Path(__file__).parent / "fixtures" / "ocr_multilingual_samples.json"


def _samples() -> list[dict[str, str]]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


def _character_error_count(reference: str, hypothesis: str) -> int:
    reference = unicodedata.normalize("NFC", reference)
    hypothesis = unicodedata.normalize("NFC", hypothesis)
    prior = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, start=1):
        current = [row]
        for column, actual in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    prior[column] + 1,
                    prior[column - 1] + int(expected != actual),
                )
            )
        prior = current
    return prior[-1]


def test_profiles_preflight_exact_packs_without_all_languages_per_pass() -> None:
    configured = resolve_ocr_profile("configured", "spa+eng")
    latin = resolve_ocr_profile("latin", "spa+eng")
    simplified = resolve_ocr_profile("han-simplified", "spa+eng")
    traditional = resolve_ocr_profile("han-traditional", "spa+eng")
    automatic = resolve_ocr_profile("auto-multilingual", "spa+eng")

    assert configured.required_languages == ("spa", "eng")
    assert latin.required_languages == (*LATIN_LANGUAGES, "osd")
    assert simplified.required_languages == (*HAN_SIMPLIFIED_LANGUAGES, "osd")
    assert traditional.required_languages == (*HAN_TRADITIONAL_LANGUAGES, "osd")
    assert automatic.required_languages == (
        "spa",
        "eng",
        "deu",
        "chi_sim",
        "chi_tra",
        "osd",
    )
    assert len(set(automatic.required_languages)) == 6


def test_osd_parser_preserves_orientation_and_confidence() -> None:
    orientation = parse_osd_output(
        """Page number: 0
Orientation in degrees: 270
Rotate: 90
Orientation confidence: 12.75
Script: Han
Script confidence: 4.50
"""
    )

    assert orientation == OcrOrientation(
        orientation_degrees=270,
        rotate_degrees=90,
        orientation_confidence=12.75,
        script="Han",
        script_confidence=4.5,
        available=True,
    )


def test_twenty_four_de_zh_and_mixed_samples_route_and_pass_unicode_gate() -> None:
    samples = _samples()
    assert len(samples) == 24
    plan = resolve_ocr_profile("auto-multilingual", "spa+eng")

    for sample in samples:
        decision = route_ocr_languages(
            plan,
            OcrOrientation(script=sample["script"], available=True),
        )
        if sample["script"] in {"Latin", "Fraktur"}:
            assert decision.primary_languages == LATIN_LANGUAGES
            assert decision.fallback_languages is None
        else:
            assert decision.primary_languages == HAN_SIMPLIFIED_LANGUAGES
            assert decision.fallback_languages == HAN_TRADITIONAL_LANGUAGES
        quality = native_text_quality(sample["reference"], min_characters=8)
        assert quality.usable, (sample["id"], quality)


def test_traditional_signal_is_specific_across_fixture_variants() -> None:
    samples = _samples()
    for sample in samples:
        detected = contains_traditional_han(sample["reference"])
        if sample["kind"] == "zh-traditional":
            assert detected, sample["id"]
        elif sample["kind"] == "zh-simplified":
            assert not detected, sample["id"]


def test_native_quality_gate_rejects_substantial_corrupt_text() -> None:
    cases = {
        "mojibake": "Ã" * 80,
        "replacement": "Informe " + "�" * 60,
        "symbols": "!@#$%^&*()[]{}<>?/\\|~`" * 8,
        "run": "A" * 120,
    }
    reasons = {native_text_quality(value, min_characters=40).reason for value in cases.values()}

    assert "suspicious_unicode_or_mojibake" in reasons
    assert "low_alphanumeric_density" in reasons
    assert reasons & {"low_character_diversity", "pathological_character_run"}


def test_deterministic_cer_fixture_harness_covers_twenty_four_samples() -> None:
    samples = _samples()
    hypotheses = [sample["reference"][:-1] for sample in samples]
    errors = sum(
        _character_error_count(sample["reference"], hypothesis)
        for sample, hypothesis in zip(samples, hypotheses, strict=True)
    )
    reference_characters = sum(len(sample["reference"]) for sample in samples)

    assert errors == 24
    assert round(errors / reference_characters, 6) == 0.042179


def test_image_preprocessing_does_not_rotate_blank_or_upscale_small_inputs() -> None:
    with Image.new("RGB", (1_600, 1_000), "white") as large:
        bounded = bounded_grayscale(large, max_side=1_200, max_pixels=900_000)
    try:
        assert max(bounded.size) <= 1_200
        assert bounded.width * bounded.height <= 900_000
        assert estimate_deskew_degrees(bounded) == 0.0
    finally:
        bounded.close()

    with Image.new("L", (320, 200), "white") as small:
        preserved = bounded_grayscale(small, max_side=1_200, max_pixels=900_000)
    try:
        assert preserved.size == (320, 200)
    finally:
        preserved.close()
