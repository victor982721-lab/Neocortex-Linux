"""Public-API robustness regressions for bounded input and runtime type hints."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import get_type_hints

import pytest

import _04_Nucleo_Operativo.application_config_projections as projections
from _04_Nucleo_Operativo import ApplicationConfig, FrameworkConfig
from _04_Nucleo_Operativo.audio_models import AudioRouteConfig
from _04_Nucleo_Operativo.code_contracts import CodeRouteConfig
from _04_Nucleo_Operativo.docx_models import DocxRouteConfig
from _04_Nucleo_Operativo.global_resources import GlobalResourceLimits
from _04_Nucleo_Operativo.image_route import ImageRouteConfig
from _04_Nucleo_Operativo.office_route import OfficeRouteConfig
from _04_Nucleo_Operativo.pdf_route_models import PdfRouteConfig
from _04_Nucleo_Operativo.video_route import VideoRouteConfig
from neocortex.capabilities import CAPABILITY_SPECS, inspect_runtime_capabilities

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# region [01] Bounded capability selection


class _OverflowSentinel:
    """Fail if a consumer asks beyond the advertised finite lookahead."""

    def __init__(self) -> None:
        self.consumed = 0

    def __iter__(self) -> Iterator[str]:
        while True:
            if self.consumed > len(CAPABILITY_SPECS):
                raise AssertionError("capability iterable was consumed past its bound")
            index = self.consumed
            self.consumed += 1
            yield f"unknown-capability-{index}"


def _inspect_available(
    capabilities: Iterable[str] | None = None,
):
    return inspect_runtime_capabilities(
        capabilities,
        module_finder=lambda _name: object(),
        distribution_version=lambda _name: "fixture-version",
        executable_finder=lambda name: f"C:/fixture/{name}.exe",
    )


def test_capability_selection_stops_after_universe_plus_one() -> None:
    selection = _OverflowSentinel()

    with pytest.raises(ValueError, match="cannot exceed the declared capability count"):
        inspect_runtime_capabilities(selection)

    assert selection.consumed == len(CAPABILITY_SPECS) + 1


def test_capability_selection_preserves_duplicate_and_unknown_errors() -> None:
    with pytest.raises(ValueError, match="cannot be duplicated"):
        inspect_runtime_capabilities(("code", "code"))

    with pytest.raises(
        ValueError,
        match="unknown runtime capability: not-declared",
    ):
        inspect_runtime_capabilities(("not-declared",))


def test_capability_selection_preserves_explicit_and_default_order() -> None:
    explicit = _inspect_available(("image", "code", "docx"))
    default = _inspect_available()

    assert tuple(status.capability for status in explicit) == (
        "image",
        "code",
        "docx",
    )
    assert tuple(status.capability for status in default) == tuple(CAPABILITY_SPECS)


# endregion [01]


# region [02] Runtime-resolvable projection annotations


@pytest.mark.parametrize(
    ("projection", "expected_return"),
    (
        (projections.audio_route_config_from_application, AudioRouteConfig),
        (projections.code_route_config_from_application, CodeRouteConfig),
        (projections.docx_route_config_from_application, DocxRouteConfig),
        (
            projections.global_resource_limits_from_application,
            GlobalResourceLimits,
        ),
        (projections.image_route_config_from_application, ImageRouteConfig),
        (projections.office_route_config_from_application, OfficeRouteConfig),
        (projections.pdf_route_config_from_application, PdfRouteConfig),
        (projections.video_route_config_from_application, VideoRouteConfig),
    ),
)
def test_projection_runtime_type_hints_resolve_to_public_contracts(
    projection: Callable[..., object],
    expected_return: type[object],
) -> None:
    hints = get_type_hints(projection)

    assert ApplicationConfig is FrameworkConfig
    assert hints["config"] is ApplicationConfig
    assert hints["return"] is expected_return


def test_projection_module_cold_import_keeps_owner_contracts_deferred() -> None:
    script = textwrap.dedent(
        """
        import sys

        forbidden = {
            "_01_Enumeracion",
            "_02_Deduplicacion",
            "_04_Nucleo_Operativo.application_config",
            "_04_Nucleo_Operativo.archive_route",
            "_04_Nucleo_Operativo.audio_models",
            "_04_Nucleo_Operativo.code_contracts",
            "_04_Nucleo_Operativo.docx_models",
            "_04_Nucleo_Operativo.global_resources",
            "_04_Nucleo_Operativo.image_route",
            "_04_Nucleo_Operativo.models",
            "_04_Nucleo_Operativo.office_route",
            "_04_Nucleo_Operativo.pdf_route_models",
            "_04_Nucleo_Operativo.text_route",
            "_04_Nucleo_Operativo.video_route",
        }

        import _04_Nucleo_Operativo.application_config_projections as projections

        if tuple(projections.__all__) != (
            "archive_route_config_from_application",
            "audio_route_config_from_application",
            "code_route_config_from_application",
            "docx_route_config_from_application",
            "global_resource_limits_from_application",
            "image_route_config_from_application",
            "office_route_config_from_application",
            "pdf_route_config_from_application",
            "text_route_config_from_application",
            "video_route_config_from_application",
        ):
            raise SystemExit("projection public surface changed")
        loaded = sorted(forbidden.intersection(sys.modules))
        if loaded:
            raise SystemExit("cold projection imports loaded: " + ",".join(loaded))
        print("PROJECTION_TYPES_DEFERRED")
        """
    )
    environment = os.environ.copy()
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
    assert "PROJECTION_TYPES_DEFERRED" in completed.stdout


# endregion [02]
