from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from neocortex.deduplication import snapshot_path
from neocortex.capabilities.formats.image.adult import (
    NudeNetAdultClassifier,
    decide_adult_classification,
    is_adult_model_candidate,
)
from neocortex.capabilities.formats.image.models import (
    AdultDetection,
    DocumentCandidate,
    Features,
)
from neocortex.capabilities.formats.image.route import ImageRouteSummary
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator


# region [01] Bounded fixtures


def _features(**overrides) -> Features:
    values: dict[str, Any] = {
        "width": 900,
        "height": 700,
        "file_size": 1000,
        "format": "JPEG",
        "frames": 1,
        "has_transparency": False,
        "alpha_fraction": 0.0,
        "has_camera_exif": True,
        "white_fraction": 0.0,
        "light_fraction": 0.2,
        "dark_fraction": 0.1,
        "neutral_fraction": 0.1,
        "colorfulness": 40.0,
        "brightness_mean": 120.0,
        "brightness_std": 45.0,
        "entropy": 7.0,
        "edge_strength": 20.0,
        "edge_fraction": 0.2,
        "quantized_colors": 64,
        "border_white_fraction": 0.0,
        "long_horizontal_lines": 0.0,
        "long_vertical_lines": 0.0,
        "text_band_fraction": 0.0,
        "top_blue_fraction": 0.0,
        "green_fraction": 0.0,
        "warm_fraction": 0.2,
        "skin_fraction": 0.2,
        "central_skin_fraction": 0.2,
        "flash_fired": False,
        "iso": None,
        "exposure_time": None,
        "focal_length_35mm": None,
    }
    values.update(overrides)
    return Features(**values)


def _document(candidate: bool = False) -> DocumentCandidate:
    return DocumentCandidate(candidate, 0.9 if candidate else 0.0, "baja", (), (), ())


class _FakeDetector:
    def __init__(self, detections):
        self.detections = detections
        self.calls = 0

    def detect(self, path):
        self.calls += 1
        return self.detections


class _StreamInspectingDetector:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.source = None
        self.was_open_during_detection = False
        self.prefix = b""

    def detect(self, source):
        self.source = source
        self.was_open_during_detection = not source.closed
        self.prefix = source.read(8)
        if self.error is not None:
            raise self.error
        return []


# endregion [01]


# region [02] Candidate, model and conservative policy tests


class AdultImagePolicyTests(unittest.TestCase):
    def test_document_candidate_never_reaches_adult_model(self):
        detector = _FakeDetector([])
        classifier = NudeNetAdultClassifier()
        classifier._detector = detector

        evidence = classifier.classify(Path("document.jpg"), "foto", _features(), _document(True))

        self.assertFalse(evidence.candidate)
        self.assertFalse(evidence.analyzed)
        self.assertEqual(evidence.classification, "not_analyzed")
        self.assertEqual(detector.calls, 0)

    def test_strong_core_detection_is_explicit_but_breast_alone_is_ambiguous(self):
        explicit, confidence, _ = decide_adult_classification(
            (AdultDetection("FEMALE_GENITALIA_EXPOSED", 0.91, (1, 2, 30, 40), 0.02),),
            recovered_decode=False,
        )
        ambiguous, _, _ = decide_adult_classification(
            (AdultDetection("FEMALE_BREAST_EXPOSED", 0.96, (1, 2, 30, 40), 0.02),),
            recovered_decode=False,
        )

        self.assertEqual(explicit, "explicit")
        self.assertEqual(confidence, 0.91)
        self.assertEqual(ambiguous, "ambiguous")

    def test_recovered_decode_cannot_authorize_recycling(self):
        classification, _, reasons = decide_adult_classification(
            (AdultDetection("MALE_GENITALIA_EXPOSED", 0.94, (1, 2, 30, 40), 0.02),),
            recovered_decode=True,
        )

        self.assertEqual(classification, "ambiguous")
        self.assertIn("downgraded_recovered_decode", reasons)

    def test_candidate_stage_is_broad_for_photos_and_excludes_small_images(self):
        candidate, _ = is_adult_model_candidate(
            Path("photo.jpg"), "foto", _features(skin_fraction=0.0), _document()
        )
        small, _ = is_adult_model_candidate(
            Path("small.jpg"), "foto", _features(width=100), _document()
        )

        self.assertTrue(candidate)
        self.assertFalse(small)

    def test_unicode_windows_path_is_sent_as_a_bounded_binary_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subestación_áéñ.jpg"
            Image.new("RGB", (200, 200), "black").save(path)
            detector = _StreamInspectingDetector()
            classifier = NudeNetAdultClassifier()
            classifier._detector = detector

            evidence = classifier.classify(path, "foto", _features(), _document())

            self.assertTrue(detector.was_open_during_detection)
            self.assertTrue(detector.prefix.startswith(b"\xff\xd8"))
            self.assertIsNotNone(detector.source)
            self.assertTrue(detector.source.closed)
            self.assertTrue(evidence.analyzed)
            self.assertEqual(evidence.classification, "not_explicit")

    def test_detector_error_closes_stream_and_preserves_error_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inspección_ñ.jpg"
            Image.new("RGB", (200, 200), "black").save(path)
            detector = _StreamInspectingDetector(error=AttributeError("failure"))
            classifier = NudeNetAdultClassifier()
            classifier._detector = detector

            evidence = classifier.classify(path, "foto", _features(), _document())

            self.assertIsNotNone(detector.source)
            self.assertTrue(detector.source.closed)
            self.assertFalse(evidence.analyzed)
            self.assertEqual(evidence.classification, "unavailable")
            self.assertIn("model_error:AttributeError", evidence.evidence)


# endregion [02]


# region [03] Automatic apply wiring


class _ActionRunner:
    def __init__(self):
        self.action_type = None
        self.candidates = []

    def recycle_verified_files(self, action_type, candidates):
        self.action_type = action_type
        self.candidates = list(candidates)
        return len(self.candidates), 0, 0


class _EventState:
    def __init__(self):
        self.events = []

    def record_event(self, *args):
        self.events.append(args)


class AdultImageApplyTests(unittest.TestCase):
    def test_all_apply_routes_explicit_snapshot_through_verified_recycle_action(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "candidate.jpg"
            Image.new("RGB", (200, 200), "black").save(image)
            snapshot = snapshot_path(image)
            orchestrator = FrameworkOrchestrator(
                FrameworkConfig(
                    root=root,
                    state_directory=root / "state",
                    apply_actions=True,
                    route="all",
                )
            )
            runner = _ActionRunner()
            state = _EventState()
            summary = ImageRouteSummary(
                processing_signature="current-signature",
                adult_explicit=1,
            )

            with patch(
                "neocortex.capabilities.formats.image.state.iter_explicit_adult_candidates",
                return_value=iter(((snapshot, "model=evidence"),)),
            ):
                updated = orchestrator._apply_explicit_adult_images(runner, summary, state, 7)

            self.assertIsNotNone(updated)
            self.assertEqual(updated.adult_recycled, 1)
            self.assertEqual(runner.action_type, "trash_explicit_adult_image")
            self.assertEqual(runner.candidates, [(snapshot, "model=evidence")])
            self.assertEqual(len(state.events), 1)


# endregion [03]


if __name__ == "__main__":
    unittest.main()
