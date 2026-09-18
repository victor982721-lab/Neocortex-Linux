"""Linux OFD fallback exercises real locks rather than bypassing capability."""
from __future__ import annotations

import errno
import fcntl
import sqlite3
from contextlib import closing

import pytest

from neocortex.persistence import sqlite_immutable


def test_linux_uapi_fallback_holds_real_same_process_writer_exclusion(tmp_path, monkeypatch):
    database = tmp_path / "owner.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE payload(value)")
        connection.commit()
    monkeypatch.delattr(fcntl, "F_OFD_SETLK", raising=False)
    with sqlite_immutable.sqlite_owner_effect_guard(database):
        with closing(sqlite3.connect(database, timeout=0.01)) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("BEGIN IMMEDIATE")
    with closing(sqlite3.connect(database, timeout=0.01)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.rollback()


def test_unsupported_kernel_remains_fail_closed_with_uapi_fallback(tmp_path, monkeypatch):
    database = tmp_path / "owner.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE payload(value)")
        connection.commit()
    monkeypatch.delattr(fcntl, "F_OFD_SETLK", raising=False)
    original = fcntl.fcntl
    def unavailable(fd, command, *args):
        if command == 37:
            raise OSError(errno.EINVAL, "OFD locks are unsupported")
        return original(fd, command, *args)
    monkeypatch.setattr(fcntl, "fcntl", unavailable)
    with pytest.raises(sqlite_immutable.ImmutableSQLiteUnavailable):
        with sqlite_immutable.sqlite_owner_effect_guard(database):
            pytest.fail("an unsupported kernel cannot acquire the effect guard")
