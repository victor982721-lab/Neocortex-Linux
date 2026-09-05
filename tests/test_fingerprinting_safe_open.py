"""Regression tests for fail-closed POSIX fingerprinting I/O."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from neocortex.deduplication import (
    FileChangedError,
    files_equal_exact,
    full_fingerprint,
    partial_fingerprint,
    snapshot_path,
)


PAYLOAD = b"fingerprint-payload-" * 32_768
Operation = Callable[..., bytes | bool]


def _operation(name: str) -> Operation:
    if name == "full":
        return full_fingerprint
    if name == "partial":
        return partial_fingerprint
    return files_equal_exact


def _run_without_blocking(operation: Callable[[], object]) -> Exception | None:
    outcome: list[Exception] = []

    def invoke() -> None:
        try:
            operation()
        except Exception as exc:  # the assertion below checks the public failure type
            outcome.append(exc)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive(), "fingerprinting call blocked on an unsafe source"
    return outcome[0] if outcome else None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO fixtures require POSIX")
@pytest.mark.parametrize("name", ("full", "partial", "exact"))
def test_fifo_replacement_fails_without_blocking(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source.bin"
    peer = tmp_path / "peer.bin"
    source.write_bytes(PAYLOAD)
    peer.write_bytes(PAYLOAD)
    source_snapshot = snapshot_path(source)
    peer_snapshot = snapshot_path(peer)
    source.unlink()
    os.mkfifo(source)

    operation = _operation(name)
    error = _run_without_blocking(
        lambda: operation(source_snapshot)
        if name != "exact"
        else operation(source_snapshot, peer_snapshot)
    )

    assert isinstance(error, FileChangedError)


@pytest.mark.parametrize("name", ("full", "partial", "exact"))
def test_symlink_replacement_fails_closed(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source.bin"
    peer = tmp_path / "peer.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(PAYLOAD)
    peer.write_bytes(PAYLOAD)
    target.write_bytes(PAYLOAD)
    source_snapshot = snapshot_path(source)
    peer_snapshot = snapshot_path(peer)
    source.unlink()
    source.symlink_to(target)

    operation = _operation(name)
    error = _run_without_blocking(
        lambda: operation(source_snapshot)
        if name != "exact"
        else operation(source_snapshot, peer_snapshot)
    )

    assert isinstance(error, FileChangedError)


@pytest.mark.parametrize("name", ("full", "partial", "exact"))
def test_identity_replacement_fails_closed(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source.bin"
    peer = tmp_path / "peer.bin"
    replacement = tmp_path / "replacement.bin"
    source.write_bytes(PAYLOAD)
    peer.write_bytes(PAYLOAD)
    source_snapshot = snapshot_path(source)
    peer_snapshot = snapshot_path(peer)
    replacement.write_bytes(PAYLOAD)
    source.unlink()
    os.replace(replacement, source)

    operation = _operation(name)
    error = _run_without_blocking(
        lambda: operation(source_snapshot)
        if name != "exact"
        else operation(source_snapshot, peer_snapshot)
    )

    assert isinstance(error, FileChangedError)


class _MutatingStream:
    def __init__(self, stream: object, mutate: Callable[[], None]) -> None:
        self._stream = stream
        self._mutate = mutate
        self._mutated = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)

    def _before_read(self) -> None:
        if not self._mutated:
            self._mutated = True
            self._mutate()

    def readinto(self, buffer: bytearray) -> int:
        self._before_read()
        return self._stream.readinto(buffer)  # type: ignore[attr-defined]

    def read(self, size: int = -1) -> bytes:
        self._before_read()
        return self._stream.read(size)  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ("full", "partial", "exact"))
@pytest.mark.parametrize("mutation", ("truncate", "grow"))
def test_size_change_during_read_is_reported(
    tmp_path: Path,
    name: str,
    mutation: str,
) -> None:
    source = tmp_path / "source.bin"
    peer = tmp_path / "peer.bin"
    source.write_bytes(PAYLOAD)
    peer.write_bytes(PAYLOAD)
    source_snapshot = snapshot_path(source)
    peer_snapshot = snapshot_path(peer)

    def mutate() -> None:
        if mutation == "truncate":
            with source.open("r+b") as stream:
                stream.truncate(len(PAYLOAD) // 2)
        else:
            with source.open("ab") as stream:
                stream.write(b"growth")

    real_fdopen = os.fdopen

    def fdopen(descriptor: int, mode: str = "r", buffering: int = -1) -> _MutatingStream:
        stream = real_fdopen(descriptor, mode, buffering)
        return _MutatingStream(stream, mutate)

    operation = _operation(name)
    with patch.object(os, "fdopen", fdopen):
        error = _run_without_blocking(
            lambda: operation(source_snapshot)
            if name != "exact"
            else operation(source_snapshot, peer_snapshot)
        )

    assert isinstance(error, FileChangedError)
