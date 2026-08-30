"""Compatibility contracts for the canonical runtime configuration namespace."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / "neocortex" / "runtime"

CANONICAL_MODULES = (
    "neocortex.runtime.config.app_paths",
    "neocortex.runtime.config.application_config",
    "neocortex.runtime.config.application_config_projections",
    "neocortex.runtime.config.model_management",
    "neocortex.runtime.models",
)


def test_runtime_configuration_implementation_lives_under_product_namespace() -> None:
    for product_name in CANONICAL_MODULES:
        module = importlib.import_module(product_name)
        assert Path(module.__file__).resolve().is_relative_to(RUNTIME_ROOT)


def test_runtime_configuration_parent_packages_remain_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.runtime")
importlib.import_module("neocortex.runtime.config")
forbidden = {
    "neocortex.runtime.models",
    "neocortex.runtime.config.app_paths",
    "neocortex.runtime.config.application_config",
    "neocortex.runtime.config.application_config_projections",
    "neocortex.runtime.config.model_management",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("runtime configuration loaded eagerly: " + ",".join(loaded))
print("RUNTIME_CONFIG_IMPORT_LIGHT")
"""
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "RUNTIME_CONFIG_IMPORT_LIGHT"


def test_runtime_models_keep_historical_pickle_fqns() -> None:
    models = importlib.import_module("neocortex.runtime.models")
    config = models.FrameworkConfig(root=Path("runtime-config-fixture"))

    assert models.FrameworkConfig.__module__ == "neocortex.runtime.models"
    restored = pickle.loads(pickle.dumps(config, protocol=5))
    assert type(restored) is type(config)
    assert restored == config


def test_model_management_keeps_historical_status_identity() -> None:
    module = importlib.import_module("neocortex.runtime.config.model_management")
    status = module.ManagedModelStatus(
        "fixture-model",
        "fixture-kind",
        False,
        "not_cached",
        "/tmp/fixture-model",
    )

    assert module.ManagedModelStatus.__module__ == "neocortex.runtime.config.model_management"
    restored = pickle.loads(pickle.dumps(status, protocol=5))
    assert type(restored) is type(status)
    assert restored == status
