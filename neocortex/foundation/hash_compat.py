"""Canonical SHA-256 primitives used by NeoCortex.

Content identity has one implementation and one durable contract.  Full
content evidence uses :data:`FULL_ALGORITHM`; consumers that genuinely need a
compact identifier must derive it deterministically from the same SHA-256
digest and use the honest ``sha256_128``/``sha256_64`` labels.  There is no
optional native backend and no algorithm-dependent fallback here.
"""

from __future__ import annotations

import hashlib
from typing import Any


FULL_ALGORITHM = "sha256_full_v1"
HASH_BACKEND = "hashlib"
HASH_ALGORITHM_128 = "sha256_128"
HASH_ALGORITHM_64 = "sha256_64"
STABLE_KEY_ALGORITHM = "sha256_128_stable_v1"
_MISSING = object()


def _coerce_data(data: Any) -> Any:
    """Retain the small text convenience used by existing callers."""

    if isinstance(data, str):
        return data.encode("utf-8")
    return data


class _CompactHasher:
    """Streaming SHA-256 view with an explicit compact output width."""

    __slots__ = ("_digest_size", "_hasher")

    def __init__(self, digest_size: int, seed: int | None = None) -> None:
        self._digest_size = digest_size
        self._hasher = hashlib.sha256()
        if seed is not None:
            if type(seed) is not int:
                raise TypeError("hash seed must be an integer")
            self._hasher.update(b"neocortex-sha256-compact-v1\0")
            self._hasher.update(str(digest_size * 8).encode("ascii"))
            self._hasher.update(b"\0")
            self._hasher.update((seed & ((1 << 64) - 1)).to_bytes(8, "big"))

    def update(self, data: bytes | bytearray | memoryview | str) -> None:
        self._hasher.update(_coerce_data(data))

    def digest(self) -> bytes:
        return self._hasher.digest()[: self._digest_size]

    def hexdigest(self) -> str:
        return self.digest().hex()

    def intdigest(self) -> int:
        return int.from_bytes(self.digest(), "big")

    def copy(self) -> "_CompactHasher":
        clone = object.__new__(_CompactHasher)
        clone._digest_size = self._digest_size
        clone._hasher = self._hasher.copy()
        return clone


class _Sha256Facade:
    """Explicit compact SHA-256 constructors for non-dedupe identities."""

    def sha256_128(self, data: Any = _MISSING, seed: int | None = None) -> _CompactHasher:
        hasher = _CompactHasher(16, seed)
        if data is not _MISSING:
            hasher.update(_coerce_data(data))
        return hasher

    def sha256_128_hexdigest(self, data: Any = _MISSING, seed: int | None = None) -> str:
        return self.sha256_128(data, seed=seed).hexdigest()

    def sha256_128_digest(self, data: Any = _MISSING, seed: int | None = None) -> bytes:
        return self.sha256_128(data, seed=seed).digest()

    def sha256_128_intdigest(self, data: Any = _MISSING, seed: int | None = None) -> int:
        return self.sha256_128(data, seed=seed).intdigest()

    def sha256_64(self, data: Any = _MISSING, seed: int | None = None) -> _CompactHasher:
        hasher = _CompactHasher(8, seed)
        if data is not _MISSING:
            hasher.update(_coerce_data(data))
        return hasher

    def sha256_64_hexdigest(self, data: Any = _MISSING, seed: int | None = None) -> str:
        return self.sha256_64(data, seed=seed).hexdigest()

    def sha256_64_digest(self, data: Any = _MISSING, seed: int | None = None) -> bytes:
        return self.sha256_64(data, seed=seed).digest()

    def sha256_64_intdigest(self, data: Any = _MISSING, seed: int | None = None) -> int:
        return self.sha256_64(data, seed=seed).intdigest()


def sha256_hasher(data: Any = _MISSING) -> Any:
    """Return a standard-library SHA-256 hasher.

    The returned object intentionally is the regular ``hashlib`` object, so
    its ``update``, ``copy``, ``digest`` and ``hexdigest`` semantics are not
    wrapped or changed.  ``None`` is passed through and therefore remains a
    programmer error rather than being treated as an empty payload.
    """

    hasher = hashlib.sha256()
    if data is not _MISSING:
        hasher.update(_coerce_data(data))
    return hasher


def sha256_digest(data: bytes | bytearray | memoryview | str) -> bytes:
    """Return the complete 32-byte SHA-256 digest for *data*."""

    return sha256_hasher(data).digest()


def sha256_hexdigest(data: bytes | bytearray | memoryview | str) -> str:
    """Return the complete 64-character lowercase SHA-256 digest."""

    return sha256_hasher(data).hexdigest()


def sha256_128_hexdigest(data: bytes | bytearray | memoryview | str) -> str:
    """Derive an honest 128-bit identifier from the complete SHA-256 digest."""

    return sha256_hasher(data).hexdigest()[:32]


def sha256_64_hexdigest(data: bytes | bytearray | memoryview | str) -> str:
    """Derive an honest 64-bit identifier from the complete SHA-256 digest."""

    return sha256_hasher(data).hexdigest()[:16]


def hash_backend_component(name: str = "sha256") -> dict[str, object]:
    """Return stable provenance for the standard-library hash component."""

    return {
        "name": name,
        "kind": "python-standard-library",
        "distribution": "hashlib",
        "status": "available",
        "backend": HASH_BACKEND,
        "algorithm": FULL_ALGORITHM,
        "algorithm_128": HASH_ALGORITHM_128,
        "algorithm_64": HASH_ALGORITHM_64,
    }


def stable_sha256_128_hexdigest(data: bytes | bytearray | memoryview) -> str:
    """Return the domain-separated 128-bit idempotency key derivation."""

    digest = hashlib.sha256(b"neocortex-stable-idempotency-v1\0" + data).hexdigest()
    return digest[:32]


sha256 = _Sha256Facade()


__all__ = [
    "FULL_ALGORITHM",
    "HASH_ALGORITHM_64",
    "HASH_ALGORITHM_128",
    "HASH_BACKEND",
    "STABLE_KEY_ALGORITHM",
    "hash_backend_component",
    "sha256",
    "sha256_64_hexdigest",
    "sha256_128_hexdigest",
    "sha256_digest",
    "sha256_hasher",
    "sha256_hexdigest",
    "stable_sha256_128_hexdigest",
]
