from __future__ import annotations


import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from neocortex.capabilities.formats.image.analysis import (
    Features,
    cached_features_are_compatible,
    classify,
    requires_document_verification,
)
from neocortex.capabilities.formats.image.document import (
    DocumentTextEvidence,
    DocumentVerifierRuntime,
)


TEST_CAPABILITIES = ('image',)


# region [01] Deterministic feature/evidence fixtures


BASE_FEATURES = Features(
    width=1200,
    height=900,
    file_size=1000,
    format="PNG",
    frames=1,
    has_transparency=False,
    alpha_fraction=0.0,
    has_camera_exif=False,
    white_fraction=0.80,
    light_fraction=0.85,
    dark_fraction=0.05,
    neutral_fraction=0.90,
    colorfulness=0.04,
    brightness_mean=0.85,
    brightness_std=0.18,
    entropy=3.0,
    edge_strength=0.08,
    edge_fraction=0.08,
    quantized_colors=64,
    border_white_fraction=0.90,
    long_horizontal_lines=0.02,
    long_vertical_lines=0.01,
    text_band_fraction=0.55,
    top_blue_fraction=0.0,
    green_fraction=0.0,
    warm_fraction=0.0,
    skin_fraction=0.0,
    central_skin_fraction=0.0,
    flash_fired=False,
    iso=None,
    exposure_time=None,
    focal_length_35mm=None,
)

RUNTIME = DocumentVerifierRuntime(
    enabled=True,
    lang="spa+eng",
    timeout_seconds=12.0,
    tesseract_cmd="tesseract.exe",
    tessdata_dir=None,
    signature="test-document-verifier",
    provenance="test-layout-counts",
)


def evidence(
    words: int,
    lines: int,
    characters: int,
    *,
    document_terms: tuple[str, ...] = (),
    ui_terms: tuple[str, ...] = (),
    industrial_entities: tuple[str, ...] = (),
    industrial_activities: tuple[str, ...] = (),
    industrial_safety_conditions: tuple[str, ...] = (),
) -> DocumentTextEvidence:
    return DocumentTextEvidence(
        attempted=True,
        available=True,
        word_count=words,
        line_count=lines,
        character_count=characters,
        text_coverage=0.08,
        mean_confidence=80.0,
        document_terms=document_terms,
        ui_terms=ui_terms,
        industrial_entities=industrial_entities,
        industrial_activities=industrial_activities,
        industrial_safety_conditions=industrial_safety_conditions,
        provenance="test-layout-counts",
    )


def decide(features: Features, text: DocumentTextEvidence, name: str = "sample.png"):
    with patch(
        "neocortex.capabilities.formats.image.analysis.verify_document_text",
        return_value=text,
    ):
        return classify(
            Path("C:/root") / name,
            Path("C:/root"),
            features=features,
            document_verifier=RUNTIME,
        )


# endregion [01]


# region [02] Document verifier decision tests


class ImageAnalysisTests(unittest.TestCase):
    def test_decision_upgrade_reuses_only_known_feature_signatures(self):
        self.assertTrue(
            cached_features_are_compatible("image-route-v2|image-analysis-v3")
        )
        self.assertTrue(
            cached_features_are_compatible(
                "image-route-v3|image-features-v3|image-decisions-v4|anything"
            )
        )
        self.assertFalse(cached_features_are_compatible(None))
        self.assertFalse(
            cached_features_are_compatible("image-route-v1|image-analysis-v2")
        )

    def test_sparse_monochrome_symbol_is_a_logo_not_a_document(self):
        features = replace(
            BASE_FEATURES,
            width=1000,
            height=1000,
            white_fraction=0.83,
            light_fraction=0.84,
            dark_fraction=0.15,
            neutral_fraction=1.0,
            entropy=1.07,
            edge_fraction=0.038,
            long_horizontal_lines=0.005,
            long_vertical_lines=0.004,
            text_band_fraction=0.61,
        )
        decision = decide(features, evidence(0, 0, 0), "OpenAI.png")
        self.assertEqual(decision.category, "logo_icono")
        self.assertIsNotNone(decision.document_text)

    def test_ui_text_on_sparse_screen_beats_document_pixels(self):
        features = replace(
            BASE_FEATURES,
            width=1080,
            height=1309,
            format="JPEG",
            white_fraction=0.92,
            light_fraction=0.95,
            dark_fraction=0.01,
            neutral_fraction=0.98,
            entropy=1.61,
            edge_fraction=0.072,
            long_horizontal_lines=0.03,
            long_vertical_lines=0.018,
            text_band_fraction=0.29,
        )
        decision = decide(
            features,
            evidence(27, 9, 130, ui_terms=("busqueda", "operacion_app")),
        )
        self.assertEqual(decision.category, "captura_pantalla")

    def test_sparse_landscape_web_capture_beats_document_pixels(self):
        features = replace(
            BASE_FEATURES,
            width=1584,
            height=1111,
            white_fraction=0.95,
            light_fraction=0.97,
            dark_fraction=0.005,
            neutral_fraction=0.99,
            entropy=1.84,
            edge_fraction=0.056,
            long_horizontal_lines=0.013,
            long_vertical_lines=0.004,
            text_band_fraction=0.49,
        )
        decision = decide(features, evidence(34, 19, 184))
        self.assertEqual(decision.category, "captura_pantalla")

    def test_dense_scanned_contract_is_not_a_web_capture(self):
        features = replace(
            BASE_FEATURES,
            width=1100,
            height=850,
            white_fraction=0.94,
            light_fraction=0.95,
            dark_fraction=0.06,
            neutral_fraction=1.0,
            entropy=0.33,
            edge_fraction=0.186,
            long_horizontal_lines=0.12,
            long_vertical_lines=0.053,
            text_band_fraction=0.34,
        )
        decision = decide(features, evidence(379, 41, 1979))
        self.assertEqual(decision.category, "documento_pagina")

    def test_document_term_blocks_weak_screen_geometry(self):
        features = replace(
            BASE_FEATURES,
            width=827,
            height=1169,
            white_fraction=0.90,
            light_fraction=0.93,
            dark_fraction=0.001,
            neutral_fraction=0.998,
            entropy=1.75,
            edge_fraction=0.12,
            long_horizontal_lines=0.009,
            long_vertical_lines=0.006,
            text_band_fraction=0.89,
        )
        decision = decide(
            features,
            evidence(121, 31, 552, document_terms=("informe",)),
        )
        self.assertEqual(decision.category, "documento_pagina")

    def test_compact_high_contrast_control_panel_is_a_screen(self):
        features = replace(
            BASE_FEATURES,
            width=480,
            height=332,
            format="JPEG",
            white_fraction=0.40,
            light_fraction=0.52,
            dark_fraction=0.16,
            neutral_fraction=0.67,
            colorfulness=0.20,
            brightness_std=0.34,
            entropy=6.52,
            edge_fraction=0.22,
            border_white_fraction=0.42,
            long_horizontal_lines=0.126,
            long_vertical_lines=0.06,
            text_band_fraction=0.75,
        )
        decision = decide(features, evidence(38, 34, 145))
        self.assertEqual(decision.category, "captura_pantalla")

    def test_dense_table_page_recovers_from_graphic_score(self):
        features = replace(
            BASE_FEATURES,
            width=1200,
            height=927,
            white_fraction=0.40,
            light_fraction=0.82,
            dark_fraction=0.057,
            neutral_fraction=0.55,
            entropy=3.1,
            edge_fraction=0.16,
            border_white_fraction=0.80,
            long_horizontal_lines=0.10,
            long_vertical_lines=0.04,
            text_band_fraction=0.54,
        )
        decision = decide(features, evidence(128, 62, 648))
        self.assertEqual(decision.category, "documento_pagina")

    def test_wide_technical_map_overrides_spurious_camera_metadata(self):
        features = replace(
            BASE_FEATURES,
            width=1629,
            height=927,
            has_camera_exif=True,
            white_fraction=0.85,
            light_fraction=0.90,
            dark_fraction=0.0,
            neutral_fraction=1.0,
            entropy=2.09,
            edge_fraction=0.17,
            long_horizontal_lines=0.09,
            long_vertical_lines=0.05,
            text_band_fraction=0.83,
        )
        decision = decide(features, evidence(96, 63, 511))
        self.assertEqual(decision.category, "plano_diagrama")

    def test_short_text_composition_is_a_graphic_not_a_page(self):
        features = replace(
            BASE_FEATURES,
            width=1080,
            height=929,
            format="JPEG",
            white_fraction=0.69,
            light_fraction=0.72,
            dark_fraction=0.11,
            neutral_fraction=0.75,
            entropy=4.2,
            edge_fraction=0.13,
            border_white_fraction=0.99,
            long_horizontal_lines=0.01,
            long_vertical_lines=0.005,
            text_band_fraction=0.72,
        )
        decision = decide(features, evidence(7, 2, 23))
        self.assertEqual(decision.category, "grafico_ilustracion")

    def test_transparent_asset_is_not_promoted_to_document_by_white_pixels(self):
        features = replace(
            BASE_FEATURES,
            width=752,
            height=684,
            has_transparency=True,
            alpha_fraction=0.687,
            white_fraction=0.675,
            light_fraction=0.699,
            dark_fraction=0.033,
            neutral_fraction=0.702,
            colorfulness=0.149,
            brightness_std=0.247,
            entropy=3.393,
            edge_fraction=0.035,
            border_white_fraction=0.918,
            long_horizontal_lines=0.005,
            long_vertical_lines=0.004,
            text_band_fraction=0.732,
        )

        self.assertFalse(
            requires_document_verification(
                Path("C:/root/office-icons.png"), Path("C:/root"), features
            )
        )
        decision = decide(features, evidence(1, 1, 1), "office-icons.png")
        self.assertNotEqual(decision.category, "documento_pagina")
        self.assertFalse(decision.document_candidate.is_candidate)

    def test_near_square_promotional_art_needs_more_than_page_like_pixels(self):
        features = replace(
            BASE_FEATURES,
            width=640,
            height=713,
            has_transparency=True,
            alpha_fraction=0.008,
            white_fraction=0.082,
            light_fraction=0.750,
            dark_fraction=0.001,
            neutral_fraction=0.770,
            colorfulness=0.088,
            brightness_std=0.124,
            entropy=6.444,
            edge_fraction=0.065,
            border_white_fraction=0.059,
            long_horizontal_lines=0.004,
            long_vertical_lines=0.005,
            text_band_fraction=0.746,
        )

        decision = decide(features, evidence(14, 12, 43), "promo.png")
        self.assertEqual(decision.category, "grafico_ilustracion")
        self.assertFalse(decision.document_candidate.is_candidate)

    def test_prescreen_skips_unstructured_photographic_pixels(self):
        features = replace(
            BASE_FEATURES,
            format="JPEG",
            white_fraction=0.01,
            light_fraction=0.08,
            dark_fraction=0.20,
            neutral_fraction=0.10,
            entropy=7.2,
            edge_fraction=0.12,
            border_white_fraction=0.0,
            long_horizontal_lines=0.001,
            long_vertical_lines=0.001,
            text_band_fraction=0.20,
        )
        self.assertFalse(
            requires_document_verification(
                Path("C:/root/field.jpg"), Path("C:/root"), features
            )
        )

    def test_dense_text_photo_is_an_independent_document_candidate(self):
        features = replace(
            BASE_FEATURES,
            width=1480,
            height=800,
            format="JPEG",
            has_camera_exif=True,
            white_fraction=0.55,
            light_fraction=0.58,
            neutral_fraction=0.50,
            edge_fraction=0.10,
            border_white_fraction=0.70,
            long_horizontal_lines=0.025,
            long_vertical_lines=0.02,
            text_band_fraction=0.55,
            entropy=6.8,
            quantized_colors=64,
        )
        decision = decide(features, evidence(120, 30, 700), "IMG_0001.jpg")

        self.assertEqual(decision.category, "foto")
        self.assertTrue(decision.document_candidate.is_candidate)
        self.assertIn("photo_with_dense_text", decision.document_candidate.kinds)
        self.assertEqual(decision.confidence_kind, "heuristic_uncalibrated_v1")
        self.assertGreaterEqual(decision.winner_score, decision.runner_up_score)

    def test_ocr_industrial_mentions_remain_distinct_from_path_evidence(self):
        decision = decide(
            BASE_FEATURES,
            evidence(
                40,
                12,
                240,
                industrial_entities=("transformador",),
                industrial_activities=("mantenimiento",),
                industrial_safety_conditions=("epp",),
            ),
            "generic.png",
        )

        labels = decision.industrial_context
        self.assertEqual(labels.uncertainty, "evidencia_semantica_limitada_a_ocr")
        self.assertIn("ocr-keywords-v1", labels.provenance)
        transformer = next(
            item for item in labels.entities if item.label == "transformador"
        )
        self.assertEqual(transformer.provenance, "ocr-keywords-v1")
        self.assertEqual(transformer.evidence, ("ocr-keywords:transformador",))

    def test_recovered_decode_caps_uncalibrated_confidence(self):
        decision = decide(
            replace(
                BASE_FEATURES,
                has_camera_exif=True,
                format="JPEG",
                decode_quality="recovered_truncated",
                decode_provenance="pillow-truncated-recovery-v1",
            ),
            evidence(120, 30, 700),
            "IMG_0002.jpg",
        )

        self.assertLessEqual(decision.confidence, 0.72)


# endregion [02]


if __name__ == "__main__":
    unittest.main()
