"""Single-process guard for one framework state directory."""
# region [00] Contexto del módulo
# Módulo: neocortex/locking.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import os
import importlib
from pathlib import Path
from typing import BinaryIO
# endregion [01]

# region [02] Implementación


class FrameworkRunLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stream: BinaryIO | None = None

    def __enter__(self) -> "FrameworkRunLock":
        stream = open(self.path, "a+b", buffering=0)
        if stream.tell() == 0:
            stream.write(b"\0")
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - framework execution itself is Windows-only
                fcntl = importlib.import_module("fcntl")

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError(
                f"another framework execution is using state directory: {self.path.parent}"
            ) from exc
        self._stream = stream
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl = importlib.import_module("fcntl")

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None
# endregion [02]
