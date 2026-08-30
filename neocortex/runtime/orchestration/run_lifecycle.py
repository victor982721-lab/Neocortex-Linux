"""Bounded heartbeat support for durable framework executions."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from neocortex.persistence.framework_connection import connect_existing_framework

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module


# region [01] Process and heartbeat constants


DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
DEFAULT_STALE_HEARTBEAT_SECONDS = 30.0


def process_is_alive(process_id: int | None) -> bool | None:
    """Return process liveness, or ``None`` when it cannot be determined."""

    if process_id is None or process_id <= 0:
        return None
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                process_query_limited_information,
                False,
                process_id,
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        except (AttributeError, OSError):
            return None
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


# endregion [01]


# region [02] In-process heartbeat


class RunHeartbeat:
    """Refresh one running row from a short-lived daemon thread."""

    def __init__(
        self,
        database_path: Path,
        run_id: int,
        *,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.database_path = database_path
        self.run_id = run_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _touch(self) -> None:
        now = time.time_ns()
        connection = connect_existing_framework(
            self.database_path, readonly=False, timeout_seconds=10
        )
        try:
            with connection:
                connection.execute(
                    """UPDATE initial_runs SET heartbeat_ns=?
                    WHERE run_id=? AND status='running'""",
                    (now, self.run_id),
                )
                connection.execute(
                    """UPDATE route_runs SET heartbeat_ns=?
                    WHERE run_id=? AND status='running'""",
                    (now, self.run_id),
                )
                connection.execute(
                    """UPDATE route_phase_runs SET heartbeat_ns=?
                    WHERE run_id=? AND status='running'""",
                    (now, self.run_id),
                )
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._touch()
            except sqlite3.Error:
                # The foreground path remains authoritative and will persist the
                # final status. A transient heartbeat failure must not abort work.
                continue

    def start(self) -> "RunHeartbeat":
        if self._thread is not None:
            return self
        self._touch()
        self._thread = threading.Thread(
            target=self._run,
            name=f"neocortex-heartbeat-{self.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        try:
            self._touch()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "RunHeartbeat":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.run_lifecycle")


# endregion [02]
