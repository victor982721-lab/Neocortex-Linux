"""Single-process guard for one framework state directory."""
# region [00] Contexto del módulo
# Módulo: neocortex/locking.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import importlib
import errno
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
# endregion [01]

# region [02] Implementación


class FrameworkRunLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stream: BinaryIO | None = None

    def __enter__(self) -> "FrameworkRunLock":
        selected = Path(os.path.abspath(os.fspath(self.path)))
        parent = selected.parent
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_fd: int | None = None
        descriptor: int | None = None
        try:
            parts = parent.parts
            if not parts or parts[0] != os.sep:
                raise RuntimeError(f"framework lock parent is not absolute: {parent}")
            parent_fd = os.open(os.sep, flags)
            for component in parts[1:]:
                next_parent_fd = os.open(component, flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_parent_fd
            descriptor = os.open(
                selected.name,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "a+b", buffering=0)
            descriptor = None
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise RuntimeError(
                    f"framework lock path is a symlink or non-directory: {selected}"
                ) from exc
            raise RuntimeError(f"framework lock cannot be opened: {selected}") from exc
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        if stream.tell() == 0:
            stream.write(b"\0")
        stream.seek(0)
        try:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")

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
                msvcrt = importlib.import_module("msvcrt")

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl = importlib.import_module("fcntl")

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


# endregion [02]


@contextmanager
def state_directory_writer(path: Path) -> Iterator[None]:
    """Share the directory fence with factory reset before creating owner roots."""
    import fcntl

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    selected = path.absolute()
    descriptor = os.open(selected.anchor, flags)
    try:
        for component in selected.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("state directory is being factory-reset") from exc
        yield
    finally:
        os.close(descriptor)
