"""Explicit route ceilings survive the CLI and the shared resource scope."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.api.public import ApplicationConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator


_MIB = 1024 * 1024
_OPTIONS = (
    ("image", "--image-memory-budget-mb", 512, 512 * _MIB),
    ("docx", "--docx-memory-budget-mb", 512, 512 * _MIB),
    ("office", "--office-memory-budget-mb", 512, 512 * _MIB),
    ("audio", "--audio-memory-budget-mb", 2048, 2048 * _MIB),
    ("pdf", "--pdf-memory-budget-bytes", 128 * _MIB, 128 * _MIB),
)


@pytest.mark.parametrize("route,option,value,expected", _OPTIONS)
def test_cli_distinguishes_automatic_from_explicit_route_ceiling(
    route: str, option: str, value: int, expected: int,
) -> None:
    parser = build_parser()
    automatic_args = parser.parse_args([])
    explicit_args = parser.parse_args([option, str(value)])
    validate_arguments(automatic_args)
    validate_arguments(explicit_args)
    automatic = framework_config_from_args(automatic_args)
    explicit = framework_config_from_args(explicit_args)
    field = f"{route}_memory_budget_bytes"

    assert getattr(automatic, field) is None
    assert getattr(explicit, field) == expected
    assert (asdict(explicit) | {field: None}) == asdict(automatic)


@pytest.mark.parametrize("route,option,value,expected", _OPTIONS)
def test_explicit_api_ceiling_limits_only_its_route_in_the_shared_scope(
    tmp_path: Path, route: str, option: str, value: int, expected: int,
) -> None:
    config = ApplicationConfig(
        root=tmp_path / "corpus",
        state_directory=tmp_path / "state",
        global_memory_budget_bytes=4096 * _MIB,
        global_cpu_slots=2,
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
        image_memory_budget_bytes=expected if route == "image" else None,
        docx_memory_budget_bytes=expected if route == "docx" else None,
        office_memory_budget_bytes=expected if route == "office" else None,
        audio_memory_budget_bytes=expected if route == "audio" else None,
        pdf_memory_budget_bytes=expected if route == "pdf" else None,
    )
    coordinator = FrameworkOrchestrator(config)._resource_coordinator()

    assert coordinator.route_memory_budget_bytes(route) == expected
    for other, *_ in _OPTIONS:
        if other != route:
            assert coordinator.route_memory_budget_bytes(other) == 4096 * _MIB


@pytest.mark.parametrize("_route,option,_value,_expected", _OPTIONS)
@pytest.mark.parametrize("invalid", ("0", "-1"))
def test_cli_rejects_nonpositive_explicit_route_ceiling(
    _route: str, option: str, _value: int, _expected: int, invalid: str,
) -> None:
    args = build_parser().parse_args([option, invalid])
    with pytest.raises(SystemExit, match="positive"):
        validate_arguments(args)
