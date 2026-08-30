"""Compatibility contracts for the capability-owned Video namespace."""

from __future__ import annotations

import importlib
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = "neocortex.capabilities.formats.video"
PRODUCT_ROOT = "neocortex.capabilities.formats.video"
LEGACY_ROOT = "_04_Nucleo_Operativo"
MODULE_NAMES = ("frames", "models", "probe", "route", "state")
HISTORICAL_SYMBOLS = {
    "frames": ("VideoFrameSamplingConfig", "build_frame_plan", "sampled_video_frames"),
    "models": (
        "VideoStreamProbe",
        "SubtitleStreamProbe",
        "VideoMediaProbe",
        "VideoProcessingError",
        "VideoRouteSummary",
    ),
    "probe": ("resolve_video_ffprobe", "probe_video", "decode_video_probe"),
    "route": ("VideoRouteConfig", "VideoRoute", "_review_candidate"),
    "state": ("video_database", "initialize_video_state", "validate_video_schema"),
}


def _canonical_module(name: str):
    return importlib.import_module(f"{CANONICAL_ROOT}.{name}")


def _legacy_module(name: str):
    return importlib.import_module(f"{LEGACY_ROOT}.video_{name}")


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_legacy_video_modules_are_exact_canonical_aliases(name: str) -> None:
    canonical = _canonical_module(name)
    legacy = _legacy_module(name)

    assert legacy is canonical
    assert sys.modules[f"{LEGACY_ROOT}.video_{name}"] is canonical


def test_video_implementation_lives_under_the_product_namespace() -> None:
    product_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "video"
    for name in MODULE_NAMES:
        module = importlib.import_module(f"{PRODUCT_ROOT}.{name}")
        assert Path(module.__file__).resolve().is_relative_to(product_root)

    for relative_path in (
        "neocortex/runtime/config/application_config_projections.py",
        "neocortex/api/cli/cli_video.py",
        "neocortex/api/cli/cli_video_surface.py",
        "neocortex/knowledge/knowledge_snapshot.py",
        "neocortex/runtime/models.py",
        "neocortex/runtime/orchestration/orchestrator.py",
        "neocortex/runtime/orchestration/route_registry.py",
        "neocortex/safety/state_topology_contracts.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert PRODUCT_ROOT in source


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_video_symbols_keep_historical_pickle_fqns(name: str) -> None:
    module = _canonical_module(name)
    historical_module = f"{LEGACY_ROOT}.video_{name}"

    for symbol_name in HISTORICAL_SYMBOLS[name]:
        symbol = getattr(module, symbol_name)
        assert symbol.__module__ == historical_module
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol


def test_video_summary_instance_remains_pickle_compatible() -> None:
    models = _canonical_module("models")
    summary = models.VideoRouteSummary()

    restored = pickle.loads(pickle.dumps(summary, protocol=5))

    assert restored == summary
    assert type(restored) is models.VideoRouteSummary
    assert type(restored).__module__ == "_04_Nucleo_Operativo.video_models"


def test_legacy_video_monkeypatch_reaches_canonical_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        ("frames", "resolve_video_ffmpeg"),
        ("probe", "run_bounded_capture"),
        ("route", "resolve_video_ffmpeg"),
        ("state", "_migrate_video_v1"),
    )
    sentinel = object()
    for module_name, attribute_name in cases:
        legacy = _legacy_module(module_name)
        canonical = _canonical_module(module_name)
        monkeypatch.setattr(legacy, attribute_name, sentinel)
        assert getattr(canonical, attribute_name) is sentinel


def test_video_package_import_is_light_in_a_fresh_process() -> None:
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        importlib.import_module({CANONICAL_ROOT!r})
        forbidden = {{
            {", ".join(repr(f"{CANONICAL_ROOT}.{name}") for name in MODULE_NAMES)},
            "cv2",
            "PIL",
            "neocortex.deduplication",
        }}
        loaded = forbidden.intersection(sys.modules)
        if loaded:
            raise SystemExit("Video package eagerly loaded: " + ",".join(sorted(loaded)))
        """
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_video_schema_and_route_contracts_remain_stable() -> None:
    models = _canonical_module("models")
    route = _canonical_module("route")
    state = _canonical_module("state")

    assert models.VIDEO_ROUTE_VERSION == "video-route-v1"
    assert state.VIDEO_SCHEMA_VERSION == 2
    assert route.VIDEO_MIME_TYPES
