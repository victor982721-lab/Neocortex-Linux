"""Conservative local adult-content cascade for photographic images."""

from __future__ import annotations

from . import _preserve_legacy_module

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from .models import (
    AdultContentEvidence,
    AdultDetection,
    DocumentCandidate,
    Features,
)
from ....processing_provenance import distribution_component


# region [01] Candidate and decision policy

ADULT_POLICY_VERSION = "adult-content-policy-v1"
ADULT_MODEL_FAMILY = "nudenet-320n"
# Historical public name retained without claiming a pinned installed release.
ADULT_MODEL_VERSION = ADULT_MODEL_FAMILY
ADULT_ADAPTER_VERSION = "nudenet-unicode-stream-v2"
ADULT_ANALYSIS_VERSION = (
    f"{ADULT_POLICY_VERSION}|{ADULT_MODEL_FAMILY}|{ADULT_ADAPTER_VERSION}"
)
ADULT_MODEL_PHYSICAL_BYTES = 96 * 1024 * 1024

_PHOTOGRAPHIC_CATEGORIES = frozenset({"foto", "animada", "otro"})
_SEXUAL_CLASSES = frozenset(
    {
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
        "FEMALE_BREAST_EXPOSED",
        "BUTTOCKS_EXPOSED",
    }
)
_CORE_EXPLICIT_CLASSES = frozenset(
    {
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
    }
)
_MAX_STORED_DETECTIONS = 16


def adult_model_component() -> dict[str, Any]:
    """Return the installed NudeNet release and bundled ONNX fingerprint."""

    return distribution_component(
        "adult-model",
        "nudenet",
        artifact_relative_path="nudenet/320n.onnx",
    )


def adult_model_identity() -> str:
    """Return compact evidence provenance tied to the effective model bytes."""

    component = adult_model_component()
    artifact = component.get("artifact")
    fingerprint = artifact.get("xxh3_128") if isinstance(artifact, dict) else None
    return (
        f"{ADULT_MODEL_FAMILY}|distribution={component.get('version') or 'unavailable'}|"
        f"model_xxh3_128={fingerprint or 'unavailable'}"
    )


def is_adult_model_candidate(
    path: Path,
    category: str,
    features: Features,
    document_candidate: DocumentCandidate,
) -> tuple[bool, tuple[str, ...]]:
    """Select broad photographic candidates without trusting skin color alone."""

    del path
    reasons: list[str] = []
    if category in _PHOTOGRAPHIC_CATEGORIES:
        reasons.append(f"photographic_category:{category}")
    if features.skin_fraction >= 0.05:
        reasons.append(f"skin_fraction:{features.skin_fraction:.3f}")
    if features.central_skin_fraction >= 0.04:
        reasons.append(f"central_skin_fraction:{features.central_skin_fraction:.3f}")
    if document_candidate.is_candidate:
        reasons.append("excluded_document_candidate")
    if min(features.width, features.height) < 160:
        reasons.append("excluded_small_image")
    candidate = bool(
        category in _PHOTOGRAPHIC_CATEGORIES
        and not document_candidate.is_candidate
        and min(features.width, features.height) >= 160
    )
    return candidate, tuple(reasons)


def _bounded_detections(raw: Any, features: Features) -> tuple[AdultDetection, ...]:
    if not isinstance(raw, list):
        raise TypeError("adult detector result must be a list")
    image_area = max(1, features.width * features.height)
    detections: list[AdultDetection] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("class", ""))
        if label not in _SEXUAL_CLASSES:
            continue
        score = float(item.get("score", 0.0))
        box = item.get("box")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        x, y, width, height = (max(0, int(value)) for value in box)
        area_fraction = min(1.0, (width * height) / image_area)
        detections.append(
            AdultDetection(
                label=label,
                score=max(0.0, min(1.0, score)),
                box=(x, y, width, height),
                area_fraction=round(area_fraction, 6),
            )
        )
    detections.sort(key=lambda value: (-value.score, value.label, value.box))
    return tuple(detections[:_MAX_STORED_DETECTIONS])


def decide_adult_classification(
    detections: tuple[AdultDetection, ...],
    *,
    recovered_decode: bool,
) -> tuple[Literal["not_explicit", "ambiguous", "explicit"], float, tuple[str, ...]]:
    """Require very strong anatomical evidence before authorizing recycling."""

    core = tuple(d for d in detections if d.label in _CORE_EXPLICIT_CLASSES)
    strong_core = tuple(d for d in core if d.score >= 0.88)
    corroborated_core = tuple(d for d in core if d.score >= 0.75)
    corroborating = tuple(d for d in detections if d.score >= 0.70)
    reasons: list[str] = []
    if strong_core:
        reasons.append("core_exposure_score>=0.88")
    if corroborated_core and len({d.label for d in corroborating}) >= 2:
        reasons.append("core_exposure_with_distinct_corroboration")
    explicit = bool(reasons)
    confidence = max((d.score for d in detections), default=0.0)
    if recovered_decode and explicit:
        reasons.append("downgraded_recovered_decode")
        return "ambiguous", round(confidence, 4), tuple(reasons)
    if explicit:
        return "explicit", round(confidence, 4), tuple(reasons)
    if any(d.score >= 0.40 for d in detections):
        reasons.append("sexual_region_requires_context")
        return "ambiguous", round(confidence, 4), tuple(reasons)
    reasons.append("no_strong_explicit_detection")
    return "not_explicit", round(confidence, 4), tuple(reasons)


# endregion [01]


# region [02] Lazy local model adapter


class AdultContentClassifier(Protocol):
    @property
    def signature(self) -> str: ...

    def classify(
        self,
        path: Path,
        category: str,
        features: Features,
        document_candidate: DocumentCandidate,
    ) -> AdultContentEvidence: ...


@dataclass(slots=True)
class NudeNetAdultClassifier:
    """Load the bundled ONNX model once per process and retain no image pixels."""

    _lock: threading.Lock = field(init=False, repr=False)
    _detector: Any | None = field(init=False, default=None, repr=False)
    _signature: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._signature = f"{ADULT_ANALYSIS_VERSION}|{adult_model_identity()}"

    @property
    def signature(self) -> str:
        return self._signature

    def _runtime(self) -> Any:
        if self._detector is None:
            from nudenet import NudeDetector  # type: ignore[import-untyped]

            self._detector = NudeDetector()
        return self._detector

    def classify(
        self,
        path: Path,
        category: str,
        features: Features,
        document_candidate: DocumentCandidate,
    ) -> AdultContentEvidence:
        candidate, candidate_reasons = is_adult_model_candidate(
            path,
            category,
            features,
            document_candidate,
        )
        if not candidate:
            return AdultContentEvidence(
                candidate=False,
                analyzed=False,
                classification="not_analyzed",
                confidence=0.0,
                detections=(),
                evidence=candidate_reasons,
                provenance=(ADULT_POLICY_VERSION,),
            )
        try:
            with self._lock:
                # NudeNet delegates string paths to ``cv2.imread``.  On Windows
                # that path API cannot reliably open non-ASCII names.  A
                # buffered stream makes NudeNet use ``cv2.imdecode`` instead,
                # while avoiding an additional unbounded ``read_bytes`` copy.
                with path.open("rb") as image_stream:
                    raw = self._runtime().detect(image_stream)
            detections = _bounded_detections(raw, features)
            classification, confidence, decision_reasons = decide_adult_classification(
                detections,
                recovered_decode=features.decode_quality != "strict",
            )
            return AdultContentEvidence(
                candidate=True,
                analyzed=True,
                classification=classification,
                confidence=confidence,
                detections=detections,
                evidence=(*candidate_reasons, *decision_reasons),
                provenance=(ADULT_POLICY_VERSION, self.signature),
            )
        except Exception as exc:
            return AdultContentEvidence(
                candidate=True,
                analyzed=False,
                classification="unavailable",
                confidence=0.0,
                detections=(),
                evidence=(*candidate_reasons, f"model_error:{type(exc).__name__}"),
                provenance=(ADULT_POLICY_VERSION, f"{self.signature}:error"),
            )


DEFAULT_ADULT_CLASSIFIER: AdultContentClassifier = NudeNetAdultClassifier()


# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.image_adult")
