"""Compatibility contracts for the capability-owned Audio namespace."""

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
CANONICAL_ROOT = "_04_Nucleo_Operativo.capabilities.formats.audio"
PRODUCT_ROOT = "neocortex.capabilities.formats.audio"
LEGACY_ROOT = "_04_Nucleo_Operativo"
MODULE_NAMES = ("models", "probe", "route", "state", "whisper")
HISTORICAL_SYMBOLS = {
    "models": (
        "AudioRouteConfig",
        "MediaProbe",
        "TranscriptSegment",
        "TranscriptResult",
        "WhisperRuntime",
        "AudioRouteSummary",
        "AudioProcessingError",
        "WhisperRuntimeError",
    ),
    "probe": ("resolve_ffprobe", "probe_media", "_decode_probe"),
    "route": ("AudioRoute", "search_audio_state"),
    "state": ("audio_database", "initialize_audio_state", "_audio_schema_contract"),
    "whisper": ("resolve_whisper_runtime", "_whisper_worker", "WhisperTranscriber"),
}


def _canonical_module(name: str):
    return importlib.import_module(f"{CANONICAL_ROOT}.{name}")


def _legacy_module(name: str):
    return importlib.import_module(f"{LEGACY_ROOT}.audio_{name}")


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_legacy_audio_modules_are_exact_canonical_aliases(name: str) -> None:
    canonical = _canonical_module(name)
    legacy = _legacy_module(name)

    assert legacy is canonical
    assert sys.modules[f"{LEGACY_ROOT}.audio_{name}"] is canonical


def test_audio_implementation_lives_under_the_product_namespace() -> None:
    product_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "audio"
    for name in MODULE_NAMES:
        module = importlib.import_module(f"{PRODUCT_ROOT}.{name}")
        assert Path(module.__file__).resolve().is_relative_to(product_root)

    for relative_path in (
        "_04_Nucleo_Operativo/application_config_projections.py",
        "_04_Nucleo_Operativo/cli_audio.py",
        "_04_Nucleo_Operativo/knowledge_snapshot.py",
        "_04_Nucleo_Operativo/models.py",
        "_04_Nucleo_Operativo/orchestrator.py",
        "_04_Nucleo_Operativo/route_registry.py",
        "_04_Nucleo_Operativo/semantic_plan_owners.py",
        "_04_Nucleo_Operativo/state_topology_contracts.py",
        "_04_Nucleo_Operativo/value_review_repository.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert PRODUCT_ROOT in source


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_audio_symbols_keep_historical_pickle_fqns(name: str) -> None:
    module = _canonical_module(name)
    historical_module = f"{LEGACY_ROOT}.audio_{name}"

    for symbol_name in HISTORICAL_SYMBOLS[name]:
        symbol = getattr(module, symbol_name)
        assert symbol.__module__ == historical_module
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol


def test_audio_summary_instance_remains_pickle_compatible() -> None:
    models = _canonical_module("models")
    summary = models.AudioRouteSummary()

    restored = pickle.loads(pickle.dumps(summary, protocol=5))

    assert restored == summary
    assert type(restored) is models.AudioRouteSummary
    assert type(restored).__module__ == "_04_Nucleo_Operativo.audio_models"


def test_legacy_audio_monkeypatch_reaches_canonical_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        ("probe", "run_bounded_capture"),
        ("state", "_migrate_audio_v1_path_collation"),
        ("whisper", "resolve_whisper_runtime"),
    )
    sentinel = object()

    for module_name, attribute_name in cases:
        legacy = _legacy_module(module_name)
        canonical = _canonical_module(module_name)
        monkeypatch.setattr(legacy, attribute_name, sentinel)
        assert getattr(canonical, attribute_name) is sentinel


def test_audio_package_import_is_light_in_a_fresh_process() -> None:
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        importlib.import_module({CANONICAL_ROOT!r})
        forbidden = {{
            {", ".join(repr(f"{CANONICAL_ROOT}.{name}") for name in MODULE_NAMES)},
            "faster_whisper",
            "ctranslate2",
            "neocortex.deduplication",
        }}
        loaded = forbidden.intersection(sys.modules)
        if loaded:
            raise SystemExit("Audio package eagerly loaded: " + ",".join(sorted(loaded)))
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


def test_audio_schema_and_route_contracts_remain_stable() -> None:
    models = _canonical_module("models")
    route = _canonical_module("route")
    state = _canonical_module("state")

    assert models.AUDIO_ROUTE_VERSION == "audio-route-v1"
    assert state.AUDIO_SCHEMA_VERSION == 2
    assert "audio/" in {mime.partition("/")[0] + "/" for mime in route.AUDIO_MIME_TYPES}
