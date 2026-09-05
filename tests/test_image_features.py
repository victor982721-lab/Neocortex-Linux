"""Focused decode-recovery and effective-geometry regressions."""

from __future__ import annotations


import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageFile

from neocortex.capabilities.formats.image.decode import RecoveredImageContentError
from neocortex.capabilities.formats.image.errors import classify_image_failure
from neocortex.capabilities.formats.image.features import extract_features


TEST_CAPABILITIES = ('image',)


# region [01] Temporary image fixtures


def _truncated_jpeg(path: Path, *, uniform: bool) -> None:
    if uniform:
        image = Image.new("RGB", (512, 384), "white")
    else:
        image = Image.effect_noise((512, 384), 80).convert("RGB")
    with image:
        image.save(path, quality=90)
    payload = path.read_bytes()
    path.write_bytes(payload[:-2])


# endregion [01]


# region [02] Recovery policy and EXIF geometry


class ImageFeatureRecoveryTests(unittest.TestCase):
    def test_known_truncation_recovers_and_restores_pillow_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "truncated.jpg"
            _truncated_jpeg(path, uniform=False)
            original = ImageFile.LOAD_TRUNCATED_IMAGES
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            try:
                features = extract_features(path)
                self.assertTrue(ImageFile.LOAD_TRUNCATED_IMAGES)
            finally:
                ImageFile.LOAD_TRUNCATED_IMAGES = original

        self.assertEqual(features.decode_quality, "recovered_truncated")
        self.assertEqual(features.decode_provenance, "pillow-truncated-recovery-v1")

    def test_uniform_tolerant_decode_is_a_deletion_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "uniform-truncated.jpg"
            _truncated_jpeg(path, uniform=True)
            original = ImageFile.LOAD_TRUNCATED_IMAGES
            with self.assertRaises(RecoveredImageContentError) as raised:
                extract_features(path)
            self.assertEqual(ImageFile.LOAD_TRUNCATED_IMAGES, original)

        failure = classify_image_failure(raised.exception)
        self.assertEqual(failure.phase, "decode")
        self.assertFalse(failure.retryable)
        self.assertEqual(failure.disposition, "deletion_candidate")

    def test_unknown_os_error_never_enables_tolerant_decode(self) -> None:
        with patch(
            "neocortex.capabilities.formats.image.features._extract_features_once",
            side_effect=OSError("unrelated storage failure"),
        ) as extractor:
            with self.assertRaisesRegex(OSError, "storage failure"):
                extract_features(Path("unused.jpg"))
        extractor.assert_called_once()

    def test_exif_rotation_uses_effective_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rotated.jpg"
            exif = Image.Exif()
            exif[274] = 6
            with Image.new("RGB", (40, 80), "navy") as image:
                image.save(path, exif=exif)

            features = extract_features(path)

        self.assertEqual((features.width, features.height), (80, 40))


# endregion [02]


if __name__ == "__main__":
    unittest.main()
