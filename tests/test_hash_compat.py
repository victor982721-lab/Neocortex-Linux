"""Optional XXH3 acceleration and deterministic SHA-256 fallback contracts."""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from neocortex.foundation import hash_compat


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_native_backend_keeps_the_existing_xxh3_vectors_when_installed() -> None:
    if not hash_compat.HAS_NATIVE_XXHASH:
        pytest.skip("optional native xxhash wheel is not installed")
    native_spec = importlib.util.find_spec("xxhash")
    assert native_spec is not None
    native = importlib.import_module("xxhash")
    payload = b"neo-cortex-hash-vector"

    assert hash_compat.xxhash.xxh3_128_hexdigest(payload) == native.xxh3_128_hexdigest(payload)
    assert hash_compat.xxhash.xxh3_64_hexdigest(payload, seed=7) == native.xxh3_64_hexdigest(
        payload, seed=7
    )


def test_sha256_fallback_is_streaming_seeded_and_explicit() -> None:
    script = f"""
import hashlib
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
class BlockXXHash:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xxhash" or fullname.startswith("xxhash."):
            raise ModuleNotFoundError("blocked optional xxhash", name="xxhash")
sys.meta_path.insert(0, BlockXXHash())
from neocortex.foundation.hash_compat import (
    HASH_ALGORITHM_128,
    HASH_ALGORITHM_64,
    HASH_BACKEND,
    HAS_NATIVE_XXHASH,
    STABLE_KEY_ALGORITHM,
    hash_backend_component,
    stable_sha256_128_hexdigest,
    xxhash,
)
assert not HAS_NATIVE_XXHASH
assert HASH_BACKEND == "sha256-fallback"
assert STABLE_KEY_ALGORITHM == "sha256-128-stable-v1"
assert HASH_ALGORITHM_128 == "sha256-128-fallback-v1"
assert HASH_ALGORITHM_64 == "sha256-64-fallback-v1"
payload = b"abc" * 10000
one_shot = xxhash.xxh3_128_hexdigest(payload)
stream = xxhash.xxh3_128()
stream.update(payload[:123])
stream.update(memoryview(payload[123:]))
assert stream.hexdigest() == one_shot
assert len(one_shot) == 32
assert len(xxhash.xxh3_64_hexdigest(payload, seed=1)) == 16
assert xxhash.xxh3_64_intdigest(payload, seed=1) != xxhash.xxh3_64_intdigest(payload, seed=2)
assert xxhash.xxh3_64_hexdigest(payload, 1) == xxhash.xxh3_64_hexdigest(payload, seed=1)
assert xxhash.xxh3_128_hexdigest("abc") == xxhash.xxh3_128_hexdigest(b"abc")
assert xxhash.xxh3_64_hexdigest(payload, seed=-1) == xxhash.xxh3_64_hexdigest(
    payload, seed=(1 << 64) - 1
)
try:
    xxhash.xxh3_128(None)
except TypeError:
    pass
else:
    raise AssertionError("None must not be treated as an empty payload")
assert hash_backend_component()["status"] == "fallback"
assert stable_sha256_128_hexdigest(payload) == hashlib.sha256(
    b"neocortex-stable-idempotency-v1\\0" + payload
).hexdigest()[:32]
print("HASH_FALLBACK_OK")
"""
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", script),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "HASH_FALLBACK_OK"


def test_watcher_lock_filename_is_independent_of_hash_backend() -> None:
    native_script = f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from neocortex.runtime.control.watcher_life_lease import WatcherLifeLease
print(WatcherLifeLease('/fixture/root', '/fixture/state').path.name)
"""
    fallback_script = f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
import importlib.abc
class BlockXXHash:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xxhash" or fullname.startswith("xxhash."):
            raise ModuleNotFoundError("blocked optional xxhash", name="xxhash")
sys.meta_path.insert(0, BlockXXHash())
from neocortex.runtime.control.watcher_life_lease import WatcherLifeLease
print(WatcherLifeLease('/fixture/root', '/fixture/state').path.name)
"""
    native = subprocess.run(
        (sys.executable, "-I", "-B", "-c", native_script),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    fallback = subprocess.run(
        (sys.executable, "-I", "-B", "-c", fallback_script),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert native.returncode == fallback.returncode == 0
    assert native.stdout == fallback.stdout


def test_durable_idempotency_keys_are_independent_of_hash_backend() -> None:
    native_script = f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from neocortex.persistence.framework_state_common import _action_idempotency_key
from neocortex.semantic.semantic_lineage_repository import _stable_key
from neocortex.workflow.actions.file_action_reconciliation_store import _reconciliation_key
print(_action_idempotency_key(7, "trash", "/source", "/target", "text/plain", "evidence", True))
print(_reconciliation_key("{{}}", None, "victor", "{{}}"))
print(_stable_key("semantic.stage", (7, "fixture")))
"""
    fallback_script = f"""
import importlib.abc
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
class BlockXXHash(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xxhash" or fullname.startswith("xxhash."):
            raise ModuleNotFoundError("blocked optional xxhash", name="xxhash")
sys.meta_path.insert(0, BlockXXHash())
from neocortex.persistence.framework_state_common import _action_idempotency_key
from neocortex.semantic.semantic_lineage_repository import _stable_key
from neocortex.workflow.actions.file_action_reconciliation_store import _reconciliation_key
print(_action_idempotency_key(7, "trash", "/source", "/target", "text/plain", "evidence", True))
print(_reconciliation_key("{{}}", None, "victor", "{{}}"))
print(_stable_key("semantic.stage", (7, "fixture")))
"""
    native = subprocess.run(
        (sys.executable, "-I", "-B", "-c", native_script),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    fallback = subprocess.run(
        (sys.executable, "-I", "-B", "-c", fallback_script),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert native.returncode == fallback.returncode == 0, native.stderr + fallback.stderr
    assert native.stdout == fallback.stdout
