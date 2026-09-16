"""Optional XXH3 acceleration with a deterministic stdlib fallback.

NeoCortex historically exposed XXH3-shaped fingerprints (128-bit primary and
64-bit guard) in several durable schemas.  This module keeps those widths and
the incremental ``update/digest/hexdigest`` API while making the native
``xxhash`` wheel optional.  When it is unavailable, SHA-256 from Python's
standard library is domain-separated by width/seed and truncated to the same
8- or 16-byte output size.

The fallback is intentionally *not* byte-compatible with XXH3.  The active
backend is exposed for provenance, and callers that compare persisted hashes
will naturally invalidate/rebuild values produced by the other backend.
"""

from __future__ import annotations

import hashlib
from typing import Any

_native_xxhash: Any
try:  # Keep the only optional native import in this module.
    import xxhash as _native_xxhash
except (ImportError, OSError):  # pragma: no cover - exercised in isolated probes
    _native_xxhash = None


HAS_NATIVE_XXHASH = _native_xxhash is not None
HASH_BACKEND = "xxh3" if HAS_NATIVE_XXHASH else "sha256-fallback"
HASH_ALGORITHM_128 = "xxh3-128" if HAS_NATIVE_XXHASH else "sha256-128-fallback-v1"
HASH_ALGORITHM_64 = "xxh3-64" if HAS_NATIVE_XXHASH else "sha256-64-fallback-v1"
STABLE_KEY_ALGORITHM = "sha256-128-stable-v1"
_MAX_SEED = (1 << 64) - 1
_MISSING = object()


def _validated_seed(seed: int) -> int:
    if not isinstance(seed, int):
        raise TypeError("hash seed must be an integer")
    # xxhash's native constructors accept the full Python integer domain and
    # apply uint64 wraparound.  Mirroring that behavior keeps the fallback
    # interchangeable for callers that use a negative or oversized seed.
    return seed & _MAX_SEED


def _coerce_data(data: Any) -> Any:
    """Normalize the one convenience accepted by the native extension."""

    if isinstance(data, str):
        return data.encode("utf-8")
    return data


class _Sha256Hasher:
    """Small subset of the xxhash hasher protocol used by NeoCortex."""

    __slots__ = ("_digest_size", "_hasher")

    def __init__(self, digest_size: int, seed: int) -> None:
        self._digest_size = digest_size
        self._hasher = hashlib.sha256()
        self._hasher.update(b"neocortex-sha256-fallback-v1\0")
        self._hasher.update(str(digest_size * 8).encode("ascii"))
        self._hasher.update(b"\0")
        self._hasher.update(_validated_seed(seed).to_bytes(8, "big"))

    def update(self, data: bytes | bytearray | memoryview) -> None:
        self._hasher.update(_coerce_data(data))

    def digest(self) -> bytes:
        return self._hasher.digest()[: self._digest_size]

    def hexdigest(self) -> str:
        return self.digest().hex()

    def intdigest(self) -> int:
        return int.from_bytes(self.digest(), "big")

    def copy(self) -> _Sha256Hasher:
        clone = object.__new__(_Sha256Hasher)
        clone._digest_size = self._digest_size
        clone._hasher = self._hasher.copy()
        return clone


class _HashCompat:
    """Proxy exposing the XXH3 constructors used by product modules."""

    def xxh3_128(self, data: Any = _MISSING, seed: int = 0, **kwargs: Any) -> Any:
        if _native_xxhash is not None:
            if data is _MISSING:
                return _native_xxhash.xxh3_128(seed=seed, **kwargs)
            return _native_xxhash.xxh3_128(_coerce_data(data), seed=seed, **kwargs)
        if kwargs:
            raise TypeError("the SHA-256 fallback does not support secret parameters")
        hasher = _Sha256Hasher(16, _validated_seed(seed))
        if data is not _MISSING:
            hasher.update(_coerce_data(data))
        return hasher

    def xxh3_128_hexdigest(self, data: Any = _MISSING, seed: int = 0, **kwargs: Any) -> str:
        return self.xxh3_128(data, seed=seed, **kwargs).hexdigest()

    def xxh3_128_digest(self, data: Any = _MISSING, seed: int = 0, **kwargs: Any) -> bytes:
        return self.xxh3_128(data, seed=seed, **kwargs).digest()

    def xxh3_128_intdigest(self, data: Any = _MISSING, seed: int = 0, **kwargs: Any) -> int:
        return self.xxh3_128(data, seed=seed, **kwargs).intdigest()

    def xxh3_64(self, data: Any = _MISSING, seed: int = 0, **kwargs: Any) -> Any:
        if _native_xxhash is not None:
            if data is _MISSING:
                return _native_xxhash.xxh3_64(seed=seed, **kwargs)
            return _native_xxhash.xxh3_64(_coerce_data(data), seed=seed, **kwargs)
        if kwargs:
            raise TypeError("the SHA-256 fallback does not support secret parameters")
        hasher = _Sha256Hasher(8, _validated_seed(seed))
        if data is not _MISSING:
            hasher.update(_coerce_data(data))
        return hasher

    def xxh3_64_hexdigest(self, data: Any = _MISSING, seed: int = 0, **kwargs: Any) -> str:
        return self.xxh3_64(data, seed=seed, **kwargs).hexdigest()

    def xxh3_64_digest(self, data: Any = _MISSING, seed: int = 0, **kwargs: Any) -> bytes:
        return self.xxh3_64(data, seed=seed, **kwargs).digest()

    def xxh3_64_intdigest(self, data: Any = _MISSING, seed: int = 0, **kwargs: Any) -> int:
        return self.xxh3_64(data, seed=seed, **kwargs).intdigest()

    def __getattr__(self, name: str) -> Any:
        if _native_xxhash is None:
            raise AttributeError(name)
        return getattr(_native_xxhash, name)


def hash_backend_component(name: str = "xxhash") -> dict[str, object]:
    """Return provenance for the effective native or stdlib hash backend."""

    if HAS_NATIVE_XXHASH:
        return {
            "name": name,
            "kind": "python-distribution",
            "distribution": "xxhash",
            "status": "available",
            "backend": HASH_BACKEND,
            "algorithm_128": HASH_ALGORITHM_128,
            "algorithm_64": HASH_ALGORITHM_64,
        }
    return {
        "name": name,
        "kind": "python-standard-library",
        "distribution": "xxhash",
        "status": "fallback",
        "backend": HASH_BACKEND,
        "algorithm_128": HASH_ALGORITHM_128,
        "algorithm_64": HASH_ALGORITHM_64,
    }


def stable_sha256_128_hexdigest(data: bytes | bytearray | memoryview) -> str:
    """Return a backend-independent 128-bit key for durable idempotency.

    Content fingerprints intentionally follow the selected XXH3/SHA-256
    backend, but idempotency keys must not change when an optional accelerator
    is installed or removed.  Keep this small cryptographic key primitive
    separate from the content-hash proxy and retain the historical 128-bit
    width used by the durable action tables.
    """

    digest = hashlib.sha256(b"neocortex-stable-idempotency-v1\0" + data).hexdigest()
    return digest[:32]


xxhash = _HashCompat()


__all__ = [
    "HASH_ALGORITHM_64",
    "HASH_ALGORITHM_128",
    "HASH_BACKEND",
    "HAS_NATIVE_XXHASH",
    "STABLE_KEY_ALGORITHM",
    "hash_backend_component",
    "stable_sha256_128_hexdigest",
    "xxhash",
]
