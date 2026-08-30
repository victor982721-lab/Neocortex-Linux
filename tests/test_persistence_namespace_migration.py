"""Compatibility contracts for the canonical persistence namespace."""

from __future__ import annotations

import pickle
import subprocess
import sys
from pathlib import Path

from neocortex.deduplication import FileSnapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PERSISTENCE_ROOT = PROJECT_ROOT / "neocortex" / "persistence"
MODULES = (
    "framework_connection",
    "framework_route_state",
    "framework_schema",
    "framework_state_common",
    "framework_state_writer",
    "sqlite_immutable",
    "sqlite_paths",
    "state",
)


def test_persistence_modules_are_owned_by_the_canonical_tree() -> None:
    for name in MODULES:
        product = __import__(f"neocortex.persistence.{name}", fromlist=[name])
        assert Path(product.__file__).resolve().is_relative_to(PERSISTENCE_ROOT)


def test_persistence_package_remains_import_light() -> None:
    script = """
import importlib
import sys

importlib.import_module("neocortex.persistence")
forbidden = {
    "neocortex.persistence.framework_schema",
    "neocortex.persistence.framework_state_writer",
    "neocortex.persistence.sqlite_immutable",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("persistence loaded eagerly: " + ",".join(loaded))
print("PERSISTENCE_IMPORT_LIGHT")
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
    assert completed.stdout.strip() == "PERSISTENCE_IMPORT_LIGHT"


def test_persistence_models_keep_historical_pickle_fqns() -> None:
    state = __import__("neocortex.persistence.framework_route_state", fromlist=["state"])
    sqlite = __import__("neocortex.persistence.sqlite_immutable", fromlist=["sqlite"])

    values = (
        state.ReviewCandidateReconciliation(
            FileSnapshot("/tmp/fixture", 1, 2, 3, 4, -1),
            "fixture",
            (),
            (),
        ),
        sqlite.SQLiteFileIdentity(1, 2, 3, 4, 5, 6),
    )
    for value in values:
        restored = pickle.loads(pickle.dumps(value, protocol=5))
        assert type(restored) is type(value)
        assert restored == value
