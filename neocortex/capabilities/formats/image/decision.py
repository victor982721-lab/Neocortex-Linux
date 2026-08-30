"""Conservative image decision policy over bounded extracted evidence."""

from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .document import (
    DocumentTextEvidence,
    DocumentVerifierRuntime,
    verify_document_text,
)
from .decode import RECOVERED_DECODE_CONFIDENCE_CAP
from .features import ImageMemoryGate, extract_features
from .models import (
    Decision,
    DocumentCandidate,
    Features,
    PhotoAttributes,
    VisualSemanticEvidence,
)
from .policy import (
    CATEGORY_DIRS,
    NAME_HINT_POINTS,
    NAME_HINTS,
    PHOTO_NAME_RE,
    SCREEN_DIMENSIONS,
)
from .semantics import (
    classify_industrial_context,
    normalize_text,
    phrase_matches,
    textual_context,
)
from .visual import DEFAULT_VISUAL_CLASSIFIER, VisualSemanticClassifier


# region [01] Reusable score primitives


DOCUMENT_CANDIDATE_VERSION = "document-candidate-v2"


def add(
    scores: dict[str, float],
    reasons: dict[str, list[str]],
    category: str,
    points: float,
    reason: str,
) -> None:
    scores[category] += points
    reasons[category].append(reason)


def attribute_confidence(score: float, margin: float) -> float:
    return round(min(0.98, 0.38 + score * 0.065 + max(0.0, margin) * 0.045), 3)


def _document_pixel_score(f: Features) -> float:
    score = 0.0
    if f.light_fraction >= 0.68 and f.neutral_fraction >= 0.68:
        score += 2.5
    if f.white_fraction >= 0.48 and f.edge_fraction >= 0.018:
        score += 2.2
    if f.border_white_fraction >= 0.62 and f.dark_fraction >= 0.004:
        score += 1.4
    if f.text_band_fraction >= 0.46 and f.edge_fraction >= 0.025:
        score += 1.8
    return score


def _diagram_pixel_score(f: Features, ratio: float) -> float:
    score = 0.0
    if (
        f.light_fraction >= 0.55
        and f.neutral_fraction >= 0.58
        and f.edge_fraction >= 0.045
    ):
        score += 2.2
    if f.long_horizontal_lines + f.long_vertical_lines >= 0.018:
        score += 2.4
    if ratio >= 1.30 and f.border_white_fraction >= 0.48:
        score += 1.2
    if f.edge_fraction >= 0.085 and f.entropy <= 6.2:
        score += 1.4
    return score


def _screenshot_pixel_score(f: Features, ratio: float) -> float:
    exact_screen = (f.width, f.height) in SCREEN_DIMENSIONS or (
        f.height,
        f.width,
    ) in SCREEN_DIMENSIONS
    score = 0.0
    if exact_screen and not f.has_camera_exif and f.format in {"PNG", "BMP", "WEBP"}:
        score += 5.2
    if (
        f.width >= 640
        and f.height >= 360
        and 1.20 <= ratio <= 2.45
        and f.format in {"PNG", "BMP", "WEBP"}
    ):
        score += 2.2
    if not f.has_camera_exif and f.edge_fraction >= 0.045 and f.entropy >= 3.5:
        score += 1.4
    if f.long_horizontal_lines + f.long_vertical_lines >= 0.012:
        score += 0.9
    return score


def requires_document_verification(path: Path, root: Path, f: Features) -> bool:
    """Prescreen OCR without decoding every image in the corpus."""

    ratio = max(f.width, f.height) / max(1, min(f.width, f.height))
    if ratio > 1.90:
        return False
    long_lines = f.long_horizontal_lines + f.long_vertical_lines
    document_name = bool(
        phrase_matches(textual_context(path, root), NAME_HINTS["documento_pagina"])
    )
    if not document_name and (
        (f.has_transparency and f.alpha_fraction >= 0.20)
        or min(f.width, f.height) < 192
    ):
        return False
    return bool(
        document_name
        or _document_pixel_score(f) >= 3.2
        or (_diagram_pixel_score(f, ratio) >= 4.2 and f.light_fraction >= 0.45)
        or _screenshot_pixel_score(f, ratio) >= 3.5
        or (
            f.edge_fraction >= 0.10
            and f.text_band_fraction >= 0.45
            and long_lines >= 0.04
        )
    )


# endregion [01]


# region [02] Photographic attributes


def _lighting_attributes(f: Features) -> tuple[str, float, list[str]]:
    day = 0.0
    night = 0.0
    reasons: list[str] = []
    if f.brightness_mean >= 0.62:
        day += 3.0
    elif f.brightness_mean <= 0.34:
        night += 3.0
    if f.light_fraction >= 0.30 and f.dark_fraction <= 0.14:
        day += 2.2
    if f.dark_fraction >= 0.34 and f.light_fraction <= 0.10:
        night += 2.5
    if f.top_blue_fraction >= 0.08:
        day += 1.2
    if f.iso is not None:
        if f.iso <= 200:
            day += 0.8
        elif f.iso >= 800:
            night += 1.8
    if f.exposure_time is not None:
        if f.exposure_time <= 1 / 160:
            day += 0.7
        elif f.exposure_time >= 1 / 15:
            night += 1.0
    if f.flash_fired:
        night += 0.8

    margin = abs(day - night)
    score = max(day, night)
    confidence = attribute_confidence(score, margin)
    if score >= 4.2 and margin >= 1.3:
        lighting = "dia" if day > night else "noche"
        reasons.append(
            "luminosidad y EXIF compatibles con día"
            if lighting == "dia"
            else "sombras, exposición o ISO compatibles con noche"
        )
        return lighting, confidence, reasons
    return "ambigua", min(confidence, 0.69), reasons


def _scene_attributes(f: Features) -> tuple[str, float, list[str]]:
    exterior = 0.0
    interior = 0.0
    reasons: list[str] = []
    if f.top_blue_fraction >= 0.10:
        exterior += 3.0
    elif f.top_blue_fraction >= 0.05:
        exterior += 1.5
    if f.green_fraction >= 0.16:
        exterior += 2.5
    elif f.green_fraction >= 0.09:
        exterior += 1.2
    if f.flash_fired:
        interior += 2.8
    if f.warm_fraction >= 0.50 and f.top_blue_fraction < 0.035:
        interior += 2.6
    elif f.warm_fraction >= 0.34 and f.top_blue_fraction < 0.035:
        interior += 1.8
    long_lines = f.long_horizontal_lines + f.long_vertical_lines
    if long_lines >= 0.030:
        interior += 1.8
    elif long_lines >= 0.020:
        interior += 1.3
    if f.top_blue_fraction < 0.015 and f.green_fraction < 0.035:
        interior += 1.2

    margin = abs(exterior - interior)
    score = max(exterior, interior)
    confidence = attribute_confidence(score, margin)
    if score >= 5.2 and margin >= 2.3:
        scene = (
            "exterior_muy_probable" if exterior > interior else "interior_muy_probable"
        )
        reasons.append(
            "cielo/vegetación dominantes"
            if exterior > interior
            else "flash, luz cálida y geometría interior"
        )
        return scene, confidence, reasons
    return "indeterminada", min(confidence, 0.77), reasons


def _selfie_candidate(path: Path, f: Features, orientation: str) -> bool:
    score = 0.0
    normalized_name = normalize_text(path.stem)
    if " selfie " in f" {normalized_name} ":
        score += 7.0
    if orientation == "vertical":
        score += 0.8
    if f.central_skin_fraction >= 0.20:
        score += 2.5
    elif f.central_skin_fraction >= 0.12:
        score += 1.2
    if f.skin_fraction >= 0.10 and f.central_skin_fraction >= f.skin_fraction * 1.20:
        score += 1.5
    if f.focal_length_35mm is not None and f.focal_length_35mm <= 28:
        score += 0.8
    return score >= 5.0


def classify_photo_attributes(path: Path, f: Features) -> PhotoAttributes:
    width_ratio = f.width / f.height
    if 0.88 <= width_ratio <= 1.14:
        orientation = "cuadrada"
    elif f.width > f.height:
        orientation = "horizontal"
    else:
        orientation = "vertical"
    panoramic = (
        max(f.width, f.height) / min(f.width, f.height) >= 3.0
        and min(f.width, f.height) >= 400
    )

    lighting, lighting_confidence, lighting_reasons = _lighting_attributes(f)
    scene, scene_confidence, scene_reasons = _scene_attributes(f)
    selfie_candidate = _selfie_candidate(path, f, orientation)

    if f.white_fraction >= 0.58 and f.brightness_mean >= 0.80:
        exposure = "sobreexpuesta_probable"
    elif f.dark_fraction >= 0.62 and f.brightness_mean <= 0.25:
        exposure = "subexpuesta_probable"
    else:
        exposure = "normal_o_indeterminada"

    attribute_reasons = lighting_reasons + scene_reasons
    if selfie_candidate:
        attribute_reasons.append("selfie candidata por nombre o tonos centrales")
    if panoramic:
        attribute_reasons.append("relación panorámica")
    if f.flash_fired:
        attribute_reasons.append("flash indicado por EXIF")
    return PhotoAttributes(
        orientation=orientation,
        lighting=lighting,
        lighting_confidence=lighting_confidence,
        scene=scene,
        scene_confidence=scene_confidence,
        panoramic=panoramic,
        flash=f.flash_fired,
        exposure=exposure,
        selfie_candidate=selfie_candidate,
        reasons=tuple(attribute_reasons),
    )


# endregion [02]


# region [03] Primary structural scoring


@dataclass
class _PrimaryScores:
    photo: float
    photo_reason: str
    document: float
    diagram: float
    screenshot: float
    logo: float
    transparent: float
    graphic: float


def _photo_score(path: Path, f: Features) -> tuple[float, str]:
    score = 0.0
    reason = "apariencia fotográfica"
    if f.has_camera_exif:
        score += 8.0
        reason = "metadatos de cámara"
    if PHOTO_NAME_RE.match(path.stem):
        score += 4.5
        reason = "nombre generado por cámara"
    if " selfie " in f" {normalize_text(path.stem)} ":
        score += 5.0
        reason = "nombre compatible con selfie"
    if f.format in {"JPEG", "JPG", "HEIC", "HEIF"}:
        score += 1.5
    if f.entropy >= 6.35 and f.quantized_colors >= 52 and f.white_fraction < 0.45:
        score += 2.4
    if f.colorfulness >= 0.12 and f.quantized_colors >= 48 and f.entropy >= 5.8:
        score += 1.3
    return score, reason


def _logo_score(f: Features, megapixels: float) -> float:
    score = 0.0
    if f.alpha_fraction >= 0.12:
        score += 3.2
    if f.quantized_colors <= 18 and f.edge_fraction >= 0.008:
        score += 2.2
    if f.border_white_fraction >= 0.76 and f.quantized_colors <= 32:
        score += 1.6
    if megapixels <= 1.5 and f.entropy <= 5.2:
        score += 1.0
    return score


def _transparent_score(f: Features) -> float:
    score = 0.0
    if f.alpha_fraction >= 0.30:
        score += 5.0
    elif f.alpha_fraction >= 0.08:
        score += 2.8
    if f.has_transparency and f.quantized_colors >= 20:
        score += 1.4
    return score


def _graphic_score(f: Features) -> float:
    score = 0.0
    if not f.has_camera_exif and f.quantized_colors <= 45:
        score += 1.8
    if f.edge_fraction >= 0.045 and f.entropy <= 6.50:
        score += 1.6
    if f.format in {"PNG", "GIF", "WEBP"}:
        score += 1.0
    if f.colorfulness >= 0.08 and f.brightness_std >= 0.12:
        score += 0.8
    return score


def _primary_scores(path: Path, f: Features, ratio: float) -> _PrimaryScores:
    photo, photo_reason = _photo_score(path, f)
    megapixels = f.width * f.height / 1_000_000
    return _PrimaryScores(
        photo=photo,
        photo_reason=photo_reason,
        document=_document_pixel_score(f),
        diagram=_diagram_pixel_score(f, ratio),
        screenshot=_screenshot_pixel_score(f, ratio),
        logo=_logo_score(f, megapixels),
        transparent=_transparent_score(f),
        graphic=_graphic_score(f),
    )


def _apply_name_evidence(
    context: str,
    scores: dict[str, float],
    reasons: dict[str, list[str]],
) -> dict[str, list[str]]:
    name_matches: dict[str, list[str]] = {}
    for category, hints in NAME_HINTS.items():
        matches = phrase_matches(context, hints)
        name_matches[category] = matches
        if matches:
            add(
                scores,
                reasons,
                category,
                NAME_HINT_POINTS[category] + min(1.0, 0.5 * (len(matches) - 1)),
                f"nombre/ruta: {', '.join(matches[:3])}",
            )
    return name_matches


# endregion [03]


# region [04] OCR evidence


DocumentVerifier = Callable[
    [Path, DocumentVerifierRuntime, ImageMemoryGate | None],
    DocumentTextEvidence,
]


def _page_surface(f: Features, long_lines: float) -> bool:
    return bool(
        (f.light_fraction >= 0.68 and f.neutral_fraction >= 0.68)
        or (f.white_fraction >= 0.38 and f.border_white_fraction >= 0.55)
        or (long_lines >= 0.05 and f.edge_fraction >= 0.06)
    )


def _credible_raster_page(
    f: Features,
    ratio: float,
    evidence: DocumentTextEvidence | None,
) -> bool:
    strong_text = bool(
        evidence is not None
        and evidence.available
        and (evidence.dense_text or evidence.document_terms)
    )
    if strong_text:
        return True
    return bool(
        min(f.width, f.height) >= 320
        and ratio >= 1.18
        and (not f.has_transparency or f.alpha_fraction < 0.20)
    )


def _strong_ui_evidence(
    f: Features,
    ratio: float,
    long_side: int,
    long_lines: float,
    document_name: bool,
    evidence: DocumentTextEvidence,
    screenshot_score: float,
) -> bool:
    compact_control_panel = bool(
        long_side <= 800
        and ratio >= 1.30
        and f.colorfulness >= 0.15
        and f.brightness_std >= 0.25
        and f.entropy >= 6.0
        and long_lines >= 0.08
    )
    sparse_web_capture = bool(
        not evidence.document_terms
        and not (long_lines >= 0.08 and ratio >= 1.45)
        and screenshot_score >= 3.0
        and f.white_fraction >= 0.85
        and 1.0 <= f.entropy <= 2.4
        and f.dark_fraction < 0.03
        and long_lines < 0.05
        and ratio >= 1.40
    )
    return bool(
        not document_name
        and (
            (evidence.ui_terms and screenshot_score >= 0.9)
            or compact_control_panel
            or sparse_web_capture
        )
    )


def _apply_dense_text_evidence(
    f: Features,
    ratio: float,
    long_side: int,
    document_name: bool,
    evidence: DocumentTextEvidence,
    primary: _PrimaryScores,
    reasons: dict[str, list[str]],
) -> None:
    long_lines = f.long_horizontal_lines + f.long_vertical_lines
    page_surface = _page_surface(f, long_lines)
    if ratio <= 1.82 and (page_surface or document_name):
        primary.document += 3.4
        reasons["documento_pagina"].append("OCR: texto distribuido en múltiples líneas")
    if ratio >= 1.45 and long_lines >= 0.08:
        primary.diagram += 3.5
        reasons["plano_diagrama"].append(
            "OCR: etiquetas sobre geometría técnica extensa"
        )

    strong_ui = _strong_ui_evidence(
        f,
        ratio,
        long_side,
        long_lines,
        document_name,
        evidence,
        primary.screenshot,
    )
    if strong_ui:
        primary.screenshot += 10.0
        reasons["captura_pantalla"].append(
            "OCR: texto de interfaz sobre lienzo de pantalla"
        )
    if (
        not page_surface
        and not document_name
        and ratio >= 1.45
        and f.colorfulness >= 0.05
    ):
        primary.graphic += 4.5
        reasons["grafico_ilustracion"].append(
            "OCR: texto promocional sin superficie de página"
        )


def _apply_sparse_text_evidence(
    f: Features,
    document_name: bool,
    evidence: DocumentTextEvidence,
    primary: _PrimaryScores,
    reasons: dict[str, list[str]],
) -> None:
    isolated_symbol = bool(
        not document_name
        and evidence.word_count <= 2
        and evidence.line_count <= 1
        and f.entropy <= 1.4
        and f.light_fraction >= 0.65
        and f.dark_fraction >= 0.07
        and f.edge_fraction <= 0.06
    )
    if isolated_symbol:
        primary.logo += 8.5
        reasons["logo_icono"].append("OCR: símbolo aislado sin estructura textual")
    brief_graphic = bool(
        not document_name
        and evidence.word_count <= 10
        and evidence.line_count <= 4
        and f.entropy >= 2.5
        and f.dark_fraction >= 0.06
        and f.border_white_fraction >= 0.70
    )
    if brief_graphic:
        primary.graphic += 7.0
        reasons["grafico_ilustracion"].append(
            "OCR: texto breve integrado en composición gráfica"
        )
    elif bool(
        not document_name
        and evidence.word_count <= 10
        and evidence.line_count <= 5
        and primary.document <= 4.5
        and f.entropy >= 4.5
    ):
        primary.graphic += 5.0
        reasons["grafico_ilustracion"].append(
            "OCR: composición digital con poco texto verificable"
        )


def _document_evidence(
    path: Path,
    root: Path,
    f: Features,
    ratio: float,
    long_side: int,
    name_matches: dict[str, list[str]],
    primary: _PrimaryScores,
    reasons: dict[str, list[str]],
    memory_gate: ImageMemoryGate | None,
    document_verifier: DocumentVerifierRuntime | None,
    verifier: DocumentVerifier,
) -> DocumentTextEvidence | None:
    if document_verifier is None or not requires_document_verification(path, root, f):
        return None

    evidence = verifier(path, document_verifier, memory_gate)
    if not evidence.available:
        return evidence
    document_name = bool(name_matches["documento_pagina"])
    if evidence.dense_text:
        _apply_dense_text_evidence(
            f,
            ratio,
            long_side,
            document_name,
            evidence,
            primary,
            reasons,
        )
    _apply_sparse_text_evidence(
        f,
        document_name,
        evidence,
        primary,
        reasons,
    )
    return evidence


# endregion [04]


# region [05] Independent document candidacy


def _classify_document_candidate(
    f: Features,
    winner: str,
    primary: _PrimaryScores,
    name_matches: dict[str, list[str]],
    evidence: DocumentTextEvidence | None,
) -> DocumentCandidate:
    """Preserve document suspicion independently from the structural winner."""

    score = 0.0
    reasons: list[str] = []
    kinds: list[str] = []
    provenance = {DOCUMENT_CANDIDATE_VERSION}
    document_name = bool(name_matches["documento_pagina"])
    long_lines = f.long_horizontal_lines + f.long_vertical_lines
    page_surface = _page_surface(f, long_lines)

    if primary.document >= 4.0:
        score += 0.32
        reasons.append("pixels:geometria_de_pagina")
        provenance.add("image-features-v4")
    if document_name:
        score += 0.28
        reasons.append("path-keywords:documento")
        kinds.append("document_path_hint")
        provenance.add("path-keywords-v1")
    if evidence is not None and evidence.available:
        if evidence.dense_text:
            score += 0.38
            reasons.append(
                "ocr-keywords:texto_denso"
                f"({evidence.word_count} palabras/{evidence.line_count} lineas)"
            )
            kinds.append("photo_with_dense_text" if winner == "foto" else "dense_text")
            provenance.add("ocr-layout-v2")
        if evidence.document_terms:
            score += 0.20
            reasons.append(
                "ocr-keywords:terminos_documentales="
                + ",".join(evidence.document_terms[:3])
            )
            kinds.append("document_terms")
            provenance.add("ocr-keywords-v1")

    if winner == "documento_pagina":
        score = max(score, 0.64)
        kinds.append("raster_document")
        reasons.append("structural:documento_pagina")
    if winner == "captura_pantalla" and evidence is not None:
        if evidence.ui_terms and not document_name and not evidence.document_terms:
            score = min(score, 0.35)
    if (
        winner == "plano_diagrama"
        and not document_name
        and not (evidence and evidence.document_terms)
    ):
        score = min(score, 0.55)

    score = round(min(0.98, score), 3)
    is_candidate = winner == "documento_pagina" or score >= 0.62
    if is_candidate and evidence is not None and evidence.dense_text and page_surface:
        uncertainty = "media"
    elif is_candidate and (document_name or (evidence and evidence.document_terms)):
        uncertainty = "media"
    elif is_candidate:
        uncertainty = "alta"
    else:
        uncertainty = "sin_evidencia_suficiente"
    return DocumentCandidate(
        is_candidate=is_candidate,
        heuristic_score=score,
        uncertainty=uncertainty,
        kinds=tuple(dict.fromkeys(kinds)),
        evidence=tuple(reasons),
        provenance=tuple(sorted(provenance)),
    )


# endregion [05]


# region [06] Final category selection


def _apply_primary_scores(
    scores: dict[str, float],
    reasons: dict[str, list[str]],
    primary: _PrimaryScores,
    f: Features,
    ratio: float,
    document_text: DocumentTextEvidence | None,
) -> None:
    if (
        ratio <= 1.82
        and primary.document >= 4.0
        and _credible_raster_page(f, ratio, document_text)
    ):
        add(
            scores,
            reasons,
            "documento_pagina",
            primary.document,
            "fondo claro/neutro con bandas de trazos",
        )
    if primary.diagram >= 4.2:
        add(
            scores,
            reasons,
            "plano_diagrama",
            primary.diagram,
            "líneas largas y trazos densos sobre fondo uniforme",
        )
    if primary.screenshot >= 3.5:
        add(
            scores,
            reasons,
            "captura_pantalla",
            primary.screenshot,
            "dimensiones, formato y contornos de interfaz",
        )
    if primary.logo >= 4.0 and primary.photo < 5.0:
        add(
            scores,
            reasons,
            "logo_icono",
            primary.logo,
            "pocos colores, margen uniforme o transparencia",
        )
    if primary.transparent >= 4.0 and primary.logo < 5.0:
        add(
            scores,
            reasons,
            "recurso_transparente",
            primary.transparent,
            "área transparente significativa",
        )
    if primary.graphic >= 3.2:
        add(
            scores,
            reasons,
            "grafico_ilustracion",
            primary.graphic,
            "paleta discreta y contornos digitales",
        )

    structural = max(
        scores["documento_pagina"],
        scores["plano_diagrama"],
        scores["captura_pantalla"],
        scores["logo_icono"],
        scores["recurso_transparente"],
    )
    if primary.photo >= 3.0 and structural < 7.5:
        add(scores, reasons, "foto", primary.photo, primary.photo_reason)


def _select_winner(
    scores: dict[str, float],
    reasons: dict[str, list[str]],
    f: Features,
    primary: _PrimaryScores,
    short_side: int,
    megapixels: float,
) -> tuple[str, float, str | None, float]:
    if f.frames > 1:
        add(scores, reasons, "animada", 20.0, f"archivo con {f.frames} fotogramas")

    low_resolution = short_side < 200 or megapixels < 0.075
    if low_resolution and max(scores.values()) < 7.0:
        add(
            scores,
            reasons,
            "baja_resolucion",
            7.2,
            f"resolución {f.width}x{f.height}",
        )

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    winner, winner_score = ordered[0]
    runner_up, runner_score = ordered[1]
    if winner_score < 3.0:
        if primary.photo >= 2.0 or (f.entropy >= 6.1 and f.quantized_colors >= 48):
            winner = "foto"
            winner_score = max(3.0, primary.photo)
            reasons[winner].append("textura fotográfica sin metadatos concluyentes")
        elif primary.graphic >= 2.2:
            winner = "grafico_ilustracion"
            winner_score = max(3.0, primary.graphic)
            reasons[winner].append("imagen digital sin clase estructural más firme")
        else:
            winner = "otro"
            winner_score = 1.0
            reasons[winner].append("evidencia heurística insuficiente")
    return winner, winner_score, runner_up if runner_score > 0 else None, runner_score


# endregion [06]


# region [07] Public decision entry point


FeatureExtractor = Callable[[Path, ImageMemoryGate | None], Features]


def classify(
    path: Path,
    root: Path,
    memory_gate: ImageMemoryGate | None = None,
    *,
    features: Features | None = None,
    document_verifier: DocumentVerifierRuntime | None = None,
    feature_extractor: FeatureExtractor = extract_features,
    verifier: DocumentVerifier = verify_document_text,
    visual_classifier: VisualSemanticClassifier = DEFAULT_VISUAL_CLASSIFIER,
) -> Decision:
    f = features if features is not None else feature_extractor(path, memory_gate)
    scores = dict.fromkeys(CATEGORY_DIRS, 0.0)
    reasons: dict[str, list[str]] = {category: [] for category in CATEGORY_DIRS}
    context = textual_context(path, root)
    name_matches = _apply_name_evidence(context, scores, reasons)

    short_side = min(f.width, f.height)
    long_side = max(f.width, f.height)
    ratio = long_side / short_side
    megapixels = f.width * f.height / 1_000_000
    primary = _primary_scores(path, f, ratio)
    document_text = _document_evidence(
        path,
        root,
        f,
        ratio,
        long_side,
        name_matches,
        primary,
        reasons,
        memory_gate,
        document_verifier,
        verifier,
    )
    _apply_primary_scores(scores, reasons, primary, f, ratio, document_text)
    winner, winner_score, runner_up, runner_score = _select_winner(
        scores,
        reasons,
        f,
        primary,
        short_side,
        megapixels,
    )

    margin = max(0.0, winner_score - runner_score)
    confidence = min(0.98, 0.37 + 0.045 * winner_score + 0.035 * margin)
    if winner == "otro":
        confidence = 0.28
    if f.decode_quality != "strict":
        confidence = min(confidence, RECOVERED_DECODE_CONFIDENCE_CAP)
    document_candidate = _classify_document_candidate(
        f,
        winner,
        primary,
        name_matches,
        document_text,
    )
    try:
        visual_semantics = visual_classifier.classify(path, f)
    except Exception as exc:
        visual_semantics = VisualSemanticEvidence(
            (),
            (),
            (),
            (),
            calibrated=False,
            uncertainty=f"clasificador_visual_no_disponible:{type(exc).__name__}",
            provenance=(f"{visual_classifier.signature}:error",),
        )
    industrial_context = classify_industrial_context(
        context,
        document_text,
        visual_semantics,
    )
    photo_attributes = classify_photo_attributes(path, f) if winner == "foto" else None
    return Decision(
        category=winner,
        confidence=round(confidence, 3),
        confidence_kind="heuristic_uncalibrated_v1",
        winner_score=round(winner_score, 2),
        reasons=tuple(reasons[winner][:3]),
        runner_up=runner_up,
        runner_up_score=round(runner_score, 2),
        score_margin=round(margin, 2),
        features=f,
        photo_attributes=photo_attributes,
        industrial_context=industrial_context,
        visual_semantics=visual_semantics,
        document_candidate=document_candidate,
        document_text=document_text,
    )


# endregion [07]
