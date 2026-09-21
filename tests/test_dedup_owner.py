"""Focused ownership regressions for the shared route inventory owner."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from neocortex.deduplication import DedupIndex
from neocortex.persistence import sqlite_immutable
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.orchestration.dedup_owner import dedup_owner_lock


def test_route_owned_dedup_indexes_are_serialized_before_schema_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """PDF/image-style owners cannot race a WAL probe into a full snapshot."""

    database = tmp_path / "dedup.sqlite3"
    with DedupIndex(database):
        pass

    # A concurrent opener seeing the first owner's WAL would fail before it
    # could enter its route.  The tiny allowance makes that regression
    # deterministic without creating a large fixture database.
    monkeypatch.setattr(sqlite_immutable, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)

    active = 0
    peak_active = 0
    active_guard = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()
    errors: list[BaseException] = []

    def route_owner(route_number: int) -> None:
        nonlocal active, peak_active
        try:
            with dedup_owner_lock(database):
                with DedupIndex(database):
                    with active_guard:
                        active += 1
                        peak_active = max(peak_active, active)
                        if route_number == 0:
                            first_entered.set()
                    if route_number == 0:
                        assert release_first.wait(5)
                    with active_guard:
                        active -= 1
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    first = threading.Thread(target=route_owner, args=(0,))
    second = threading.Thread(target=route_owner, args=(1,))
    first.start()
    assert first_entered.wait(5)
    second.start()
    # The second route must remain outside its DedupIndex lifetime while the
    # first owner is active; no sleep is needed to establish the gate.
    time.sleep(0.05)
    assert second.is_alive()
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert peak_active == 1


def test_owner_wait_is_interruptible_before_the_second_database_open(
    tmp_path: Path,
) -> None:
    database = tmp_path / "dedup.sqlite3"
    with DedupIndex(database):
        pass
    token = CancellationToken()
    waiting = threading.Event()
    opened = threading.Event()
    errors: list[BaseException] = []

    def queued_owner() -> None:
        def checkpoint() -> None:
            waiting.set()
            token.checkpoint()

        try:
            with dedup_owner_lock(database, cancellation_check=checkpoint):
                opened.set()
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    with dedup_owner_lock(database):
        worker = threading.Thread(target=queued_owner)
        worker.start()
        assert waiting.wait(5)
        token.cancel()
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert not opened.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], CancellationRequested)


def test_owner_aliases_share_the_same_physical_lease(tmp_path: Path) -> None:
    database = tmp_path / "dedup.sqlite3"
    with DedupIndex(database):
        pass
    alias = tmp_path / "dedup-alias.sqlite3"
    alias.symlink_to(database)

    active = 0
    peak_active = 0
    guard = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()
    errors: list[BaseException] = []

    def owner(path: Path, number: int) -> None:
        nonlocal active, peak_active
        try:
            with dedup_owner_lock(path):
                with guard:
                    active += 1
                    peak_active = max(peak_active, active)
                    if number == 0:
                        first_entered.set()
                if number == 0:
                    assert release_first.wait(5)
                with guard:
                    active -= 1
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    first = threading.Thread(target=owner, args=(database, 0))
    second = threading.Thread(target=owner, args=(alias, 1))
    first.start()
    assert first_entered.wait(5)
    second.start()
    time.sleep(0.05)
    assert second.is_alive()
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert errors == []
    assert peak_active == 1
