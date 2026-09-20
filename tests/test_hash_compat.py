"""Single SHA-256 backend and explicit compact derivation contracts."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from neocortex.foundation import hash_compat


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_hash_contract_is_stdlib_sha256_only() -> None:
    payload = b"neo-cortex-hash-vector"
    assert hash_compat.FULL_ALGORITHM == "sha256_full_v1"
    assert hash_compat.HASH_BACKEND == "hashlib"
    assert hash_compat.HASH_ALGORITHM_128 == "sha256_128"
    assert hash_compat.HASH_ALGORITHM_64 == "sha256_64"
    assert hash_compat.sha256_hexdigest(payload) == hashlib.sha256(payload).hexdigest()
    assert hash_compat.sha256_128_hexdigest(payload) == hashlib.sha256(payload).hexdigest()[:32]
    assert hash_compat.sha256_64_hexdigest(payload) == hashlib.sha256(payload).hexdigest()[:16]


def test_streaming_compact_derivations_are_deterministic() -> None:
    payload = b"abc" * 10_000
    one_shot = hash_compat.sha256.sha256_128_hexdigest(payload)
    stream = hash_compat.sha256.sha256_128()
    stream.update(payload[:123])
    stream.update(memoryview(payload[123:]))
    assert stream.hexdigest() == one_shot
    assert len(one_shot) == 32
    assert len(hash_compat.sha256.sha256_64_hexdigest(payload, seed=1)) == 16
    assert hash_compat.sha256.sha256_64_intdigest(payload, seed=1) != hash_compat.sha256.sha256_64_intdigest(
        payload, seed=2
    )
    assert hash_compat.sha256.sha256_128_hexdigest("abc") == hash_compat.sha256.sha256_128_hexdigest(b"abc")


def test_hash_backend_component_has_no_optional_distribution() -> None:
    component = hash_compat.hash_backend_component()
    assert component["status"] == "available"
    assert component["distribution"] == "hashlib"
    assert component["algorithm"] == "sha256_full_v1"


def test_durable_idempotency_keys_are_stable_in_a_fresh_process() -> None:
    script = f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from neocortex.foundation.hash_compat import stable_sha256_128_hexdigest
print(stable_sha256_128_hexdigest(b'payload'))
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
    assert result.stdout.strip() == hashlib.sha256(b"neocortex-stable-idempotency-v1\0payload").hexdigest()[:32]
