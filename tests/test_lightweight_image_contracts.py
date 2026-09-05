"""Image metadata is usable without making the Pillow decoder available."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _probe(body: str, *, expect_decoder_request: bool = False) -> None:
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys
        from pathlib import Path

        before = frozenset(sys.modules)
        preexisting_pillow = sorted(
            name for name in before if name == "PIL" or name.startswith("PIL.")
        )
        assert not preexisting_pillow, preexisting_pillow
        decoder_requests = []

        class PillowImportBlocked(RuntimeError):
            pass

        class BlockPillow(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "PIL" or fullname.startswith("PIL."):
                    decoder_requests.append(fullname)
                    raise PillowImportBlocked(fullname)
                return None

        sys.meta_path.insert(0, BlockPillow())
        """
    )
    script += textwrap.dedent(body)
    script += textwrap.dedent(
        f"""
        introduced = set(sys.modules) - before
        forbidden = {{
            "neocortex.capabilities.formats.image.route",
            "neocortex.capabilities.formats.image.analysis",
            "neocortex.capabilities.formats.image.decode",
            "neocortex.safety.ocr_image_preprocess",
        }}
        loaded = sorted(
            name for name in introduced
            if name == "PIL" or name.startswith("PIL.") or name in forbidden
        )
        assert not loaded, loaded
        assert bool(decoder_requests) is {expect_decoder_request!r}, decoder_requests
        print("LIGHTWEIGHT_IMAGE_CONTRACTS_OK")
        """
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "LIGHTWEIGHT_IMAGE_CONTRACTS_OK" in completed.stdout


def test_cli_public_contracts_and_projection_hints_do_not_request_pillow() -> None:
    _probe(
        """
        import pickle
        from dataclasses import asdict
        from typing import get_type_hints

        from neocortex.interface.entrypoint import entrypoint
        from neocortex.api.public import ApplicationConfig, ImageRouteConfig, ImageRouteSummary
        from neocortex.capabilities.formats.image import contracts
        from neocortex.runtime.config.application_config import image_route_config_from_application

        assert callable(entrypoint)
        assert ImageRouteConfig is contracts.ImageRouteConfig
        assert ImageRouteSummary is contracts.ImageRouteSummary
        hints = get_type_hints(image_route_config_from_application)
        assert hints["return"] is ImageRouteConfig
        requested = ApplicationConfig(
            root=Path("fixture-root"), state_directory=Path("fixture-state"),
            image_workers=2, image_document_ocr_mode="never",
        )
        config = image_route_config_from_application(requested)
        assert config.root == requested.root
        assert config.workers == 2
        assert config.document_ocr_mode == "never"
        assert pickle.loads(pickle.dumps(config)) == config
        summary = ImageRouteSummary(candidate_pool=3, processed=2, errors=1)
        assert asdict(summary)["candidate_pool"] == 3
        assert pickle.loads(pickle.dumps(summary)) == summary
        """
    )


def test_image_processing_provenance_and_replay_do_not_request_pillow() -> None:
    _probe(
        """
        from unittest.mock import patch

        from neocortex.capabilities.formats.image.contracts import ImageRouteConfig

        config = ImageRouteConfig(Path("image.sqlite3"), Path("."), document_ocr_mode="never")
        target = "neocortex.foundation.processing_provenance.installed_distribution_version"
        with patch(target, return_value="12.2.0"):
            initial = config.processing_provenance
            replay = config.processing_provenance
        with patch(target, return_value="13.0.0"):
            upgraded = config.processing_provenance
        assert initial == replay
        assert initial.signature != upgraded.signature
        assert initial.manifest["pipeline"] == "image"
        components = {item["name"]: item for item in initial.manifest["components"]}
        assert components["document-ocr"]["status"] == "disabled"
        assert components["pillow"]["distribution"] == "Pillow"
        """
    )


def test_document_verifier_runtime_resolution_does_not_request_pillow() -> None:
    _probe(
        """
        from types import SimpleNamespace
        from unittest.mock import patch

        from neocortex.capabilities.formats.image.document import (
            DocumentVerifierConfig, resolve_document_verifier,
        )

        disabled = resolve_document_verifier(DocumentVerifierConfig(mode="never"))
        assert not disabled.enabled
        assert disabled.unavailable_reason == "disabled_by_configuration"
        runtime = SimpleNamespace(
            available=True, component={"name": "tesseract", "status": "available"},
            version="5.5.0", command="/fixture/tesseract", tessdata_dir=None,
            requested_languages=("spa", "eng"), traineddata_hashes=(),
        )
        with patch(
            "neocortex.capabilities.formats.image.document.resolve_tesseract_runtime",
            return_value=runtime,
        ) as resolve:
            enabled = resolve_document_verifier(DocumentVerifierConfig())
        assert enabled.enabled
        assert enabled.requested_languages == ("spa", "eng")
        assert resolve.call_args.kwargs["language"] == "spa+eng"
        """
    )


def test_image_decode_still_requires_the_pillow_decoder() -> None:
    _probe(
        """
        from neocortex.capabilities.formats.image.document import _sample_document_image

        try:
            _sample_document_image(Path("unused-image.png"))
        except PillowImportBlocked:
            pass
        else:
            raise AssertionError("image decoding bypassed its Pillow dependency")
        """,
        expect_decoder_request=True,
    )
