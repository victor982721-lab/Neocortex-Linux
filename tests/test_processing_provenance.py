from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from neocortex.capabilities.formats.audio.models import AudioRouteConfig, AudioRouteSummary
from neocortex.capabilities.formats.docx.models import DocxRouteConfig, DocxRouteSummary
from neocortex.capabilities.formats.image.contracts import ImageRouteConfig, ImageRouteSummary
from neocortex.capabilities.formats.office.route import OfficeRouteConfig, OfficeRouteSummary
from neocortex.capabilities.formats.pdf.pdf_route_models import (
    PdfRouteConfig,
    PdfRouteSummary,
    _pdf_processing_provenance,
)
from neocortex.foundation.processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    build_processing_provenance,
    clear_processing_provenance_caches,
    resolve_tesseract_runtime,
)


# region [01] Canonical signature behavior


class CanonicalProcessingProvenanceTests(unittest.TestCase):
    def test_mapping_and_component_order_do_not_change_signature(self) -> None:
        first = build_processing_provenance(
            "test-route",
            "algorithm-v1",
            {"second": 2, "first": 1},
            (
                {"name": "zeta", "version": "2"},
                {"name": "alpha", "version": "1"},
            ),
            compatibility_tag="contract-v1",
        )
        reordered = build_processing_provenance(
            "test-route",
            "algorithm-v1",
            {"first": 1, "second": 2},
            (
                {"version": "1", "name": "alpha"},
                {"version": "2", "name": "zeta"},
            ),
            compatibility_tag="contract-v1",
        )

        self.assertEqual(first, reordered)
        self.assertTrue(first.signature.startswith("psig-v1|test-route|contract-v1|"))

    def test_effective_configuration_changes_signature(self) -> None:
        first = build_processing_provenance(
            "test-route",
            "algorithm-v1",
            {"dpi": 200},
            ({"name": "engine", "version": "1"},),
            compatibility_tag="contract-v1",
        )
        changed = build_processing_provenance(
            "test-route",
            "algorithm-v1",
            {"dpi": 300},
            ({"name": "engine", "version": "1"},),
            compatibility_tag="contract-v1",
        )

        self.assertNotEqual(first.signature, changed.signature)


# endregion [01]


# region [02] Route dependency and model invalidation


class RouteProcessingSignatureTests(unittest.TestCase):
    def tearDown(self) -> None:
        _pdf_processing_provenance.cache_clear()
        clear_processing_provenance_caches()

    def test_pdf_dependency_version_and_configuration_invalidate_cache(self) -> None:
        versions = {
            "Pillow": "12.2.0",
            "PyMuPDF": "1.27.2.3",
            "pdfminer.six": "20260107",
            "pytesseract": "0.3.13",
        }

        def installed_version(distribution: str) -> str | None:
            return versions.get(distribution)

        tesseract = SimpleNamespace(
            component={
                "name": "tesseract",
                "kind": "native-executable",
                "status": "available",
                "version": "5.4.0",
                "traineddata": [{"name": "spa.traineddata", "xxh3_128": "a" * 32}],
            }
        )
        config = PdfRouteConfig(Path("pdf.sqlite3"))
        with (
            patch(
                "neocortex.foundation.processing_provenance.installed_distribution_version",
                side_effect=installed_version,
            ),
            patch(
                "neocortex.capabilities.formats.pdf.pdf_route_models.executable_component",
                return_value={
                    "name": "qpdf",
                    "kind": "native-executable",
                    "status": "available",
                    "version": "12.3.2",
                },
            ),
            patch(
                "neocortex.capabilities.formats.pdf.pdf_route_models.resolve_tesseract_runtime",
                return_value=tesseract,
            ),
        ):
            _pdf_processing_provenance.cache_clear()
            initial = config.processing_signature
            versions["PyMuPDF"] = "1.28.0"
            _pdf_processing_provenance.cache_clear()
            upgraded = config.processing_signature
            _pdf_processing_provenance.cache_clear()
            different_dpi = replace(config, dpi=300).processing_signature

        self.assertNotEqual(initial, upgraded)
        self.assertNotEqual(upgraded, different_dpi)

    def test_pdf_selected_traineddata_fingerprint_invalidates_cache(self) -> None:
        component: dict[str, Any] = {
            "name": "tesseract",
            "kind": "native-executable",
            "status": "available",
            "version": "5.4.0",
            "traineddata": [{"name": "spa.traineddata", "xxh3_128": "a" * 32}],
        }
        config = PdfRouteConfig(Path("pdf.sqlite3"))
        with (
            patch(
                "neocortex.capabilities.formats.pdf.pdf_route_models.executable_component",
                return_value={"name": "qpdf", "status": "available"},
            ),
            patch(
                "neocortex.capabilities.formats.pdf.pdf_route_models.resolve_tesseract_runtime",
                return_value=SimpleNamespace(component=component),
            ),
        ):
            _pdf_processing_provenance.cache_clear()
            initial = config.processing_signature
            component["traineddata"][0]["xxh3_128"] = "b" * 32
            _pdf_processing_provenance.cache_clear()
            changed = config.processing_signature

        self.assertNotEqual(initial, changed)

    def test_image_classifier_version_invalidates_cache_without_retired_model_artifact(
        self,
    ) -> None:
        config = ImageRouteConfig(
            Path("image.sqlite3"),
            Path("."),
            document_ocr_mode="never",
        )

        def version_12(distribution: str) -> str | None:
            return "12.2.0" if distribution == "Pillow" else "3.4.2"

        def version_13(distribution: str) -> str | None:
            return "13.0.0" if distribution == "Pillow" else "3.4.2"

        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "320n.onnx"
            model_path.write_bytes(b"fixture-model")
            distribution = SimpleNamespace(locate_file=lambda _relative: model_path)
            with patch(
                "neocortex.foundation.processing_provenance.metadata.distribution",
                return_value=distribution,
            ):
                with (
                    patch(
                        "neocortex.foundation.processing_provenance.installed_distribution_version",
                        side_effect=version_12,
                    ),
                    patch(
                        "neocortex.foundation.processing_provenance.fingerprint_file_xxh3_128",
                        return_value="a" * 32,
                    ),
                ):
                    initial = config.processing_signature
                with (
                    patch(
                        "neocortex.foundation.processing_provenance.installed_distribution_version",
                        side_effect=version_12,
                    ),
                    patch(
                        "neocortex.foundation.processing_provenance.fingerprint_file_xxh3_128",
                        return_value="b" * 32,
                    ),
                ):
                    changed_model = config.processing_signature
                with (
                    patch(
                        "neocortex.foundation.processing_provenance.installed_distribution_version",
                        side_effect=version_13,
                    ),
                    patch(
                        "neocortex.foundation.processing_provenance.fingerprint_file_xxh3_128",
                        return_value="b" * 32,
                    ),
                ):
                    changed_pillow = config.processing_signature

        self.assertEqual(initial, changed_model)
        self.assertNotEqual(changed_model, changed_pillow)

    def test_pdf_and_image_summaries_expose_uniform_provenance(self) -> None:
        manifest = {"schema": "neocortex.processing-provenance/v1"}

        pdf = PdfRouteSummary(
            processing_signature="pdf-signature",
            processing_provenance=manifest,
        )
        image = ImageRouteSummary(
            processing_signature="image-signature",
            processing_provenance=manifest,
        )

        self.assertEqual(pdf.processing_provenance, manifest)
        self.assertEqual(image.processing_provenance, manifest)

    def test_stdlib_document_routes_include_runtime_and_effective_limits(self) -> None:
        docx = DocxRouteConfig(Path("docx.sqlite3"), max_text_chars=1_000)
        changed_docx = replace(docx, max_text_chars=2_000)
        office = OfficeRouteConfig(Path("office.sqlite3"), max_text_chars=1_000)
        changed_office = replace(office, max_text_chars=2_000)

        self.assertNotEqual(docx.processing_signature, changed_docx.processing_signature)
        self.assertNotEqual(
            office.processing_signature,
            changed_office.processing_signature,
        )
        self.assertEqual(
            {item["name"] for item in docx.processing_provenance.manifest["components"]},
            {"python-runtime", "xxhash"},
        )

    def test_audio_signature_includes_ctranslate2_and_ffprobe(self) -> None:
        config = AudioRouteConfig(Path("audio.sqlite3"), model_name="small")
        ffprobe = {
            "name": "ffprobe",
            "kind": "native-executable",
            "status": "available",
            "version": "8.0",
        }
        with patch(
            "neocortex.capabilities.formats.audio.models.executable_component",
            return_value=ffprobe,
        ) as executable:
            initial = config.processing_provenance(
                backend_version="1.2.1",
                ctranslate2_version="4.8.0",
                resolved_device="cpu",
                resolved_compute_type="int8",
            )
            changed = config.processing_provenance(
                backend_version="1.2.1",
                ctranslate2_version="4.9.0",
                resolved_device="cpu",
                resolved_compute_type="int8",
            )

        self.assertNotEqual(initial.signature, changed.signature)
        self.assertIn("ffprobe", {item["name"] for item in initial.manifest["components"]})
        executable.assert_called_with(
            "ffprobe",
            default_name="ffprobe",
            explicit=None,
            version_arguments=("-version",),
        )

    def test_every_route_summary_exposes_one_schema_contract(self) -> None:
        summaries = (
            PdfRouteSummary(),
            ImageRouteSummary(),
            DocxRouteSummary(),
            OfficeRouteSummary(),
            AudioRouteSummary(),
        )

        self.assertEqual(
            {summary.summary_schema for summary in summaries},
            {ROUTE_SUMMARY_SCHEMA},
        )


# endregion [02]


# region [03] Tesseract artifact probing


class TesseractRuntimeProvenanceTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_processing_provenance_caches()

    def test_selected_traineddata_is_stream_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "tesseract-test.exe"
            tessdata = root / "tessdata"
            tessdata.mkdir()
            executable.write_bytes(b"fake-tesseract")
            spa = tessdata / "spa.traineddata"
            eng = tessdata / "eng.traineddata"
            spa.write_bytes(b"spanish-v1")
            eng.write_bytes(b"english-v1")

            language_output = (
                f'List of available languages in "{tessdata}" (2):\nspa\neng\n'
            ).encode("utf-8")

            def completed(args, **_kwargs):
                if "--version" in args:
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout=b"tesseract 5.4.0\n",
                        stderr=b"",
                    )
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=language_output,
                    stderr=b"",
                )

            with patch(
                "neocortex.foundation.processing_provenance.run_bounded_capture",
                side_effect=completed,
            ):
                clear_processing_provenance_caches()
                initial = resolve_tesseract_runtime(
                    command=str(executable),
                    tessdata_dir=str(tessdata),
                    language="spa+eng",
                    timeout_seconds=5.0,
                )
                spa.write_bytes(b"spanish-v2")
                clear_processing_provenance_caches()
                changed = resolve_tesseract_runtime(
                    command=str(executable),
                    tessdata_dir=str(tessdata),
                    language="spa+eng",
                    timeout_seconds=5.0,
                )

            initial_fingerprints = {
                item["name"]: item["xxh3_128"] for item in initial.component["traineddata"]
            }
            changed_fingerprints = {
                item["name"]: item["xxh3_128"] for item in changed.component["traineddata"]
            }
            self.assertNotEqual(
                initial_fingerprints["spa.traineddata"],
                changed_fingerprints["spa.traineddata"],
            )
            self.assertEqual(
                initial_fingerprints["eng.traineddata"],
                changed_fingerprints["eng.traineddata"],
            )


# endregion [03]


if __name__ == "__main__":
    unittest.main()
