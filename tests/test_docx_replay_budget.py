"""DOCX replay admits durable cache data before estimating package work."""

from __future__ import annotations

import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

import neocortex.capabilities.formats.docx.route as docx_route
from neocortex.capabilities.formats.docx.route import DOCX_MIME, DocxRoute, DocxRouteConfig
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from tests.test_docx_route import _State, _make_docx


class _ReplayGate:
    """Small real elastic-map gate with isolated accounting for this test."""

    def __init__(self) -> None:
        self.wait_count = 0
        self.peak_reserved_bytes = 0
        self.active = 0
        self.resident = 0

    def worker_capacity(self, **_kwargs) -> int:
        return 1

    @contextmanager
    def admit(self, estimated_bytes: int, **resources):
        if resources.get("cpu_slots", 1) == 0:
            self.resident += 1
        else:
            self.active += 1
        self.peak_reserved_bytes = max(self.peak_reserved_bytes, int(estimated_bytes))
        try:
            yield None
        finally:
            if resources.get("cpu_slots", 1) == 0:
                self.resident -= 1
            else:
                self.active -= 1


@pytest.fixture(autouse=True)
def private_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _route(tmp_path: Path, run_id: int, gate: _ReplayGate) -> DocxRoute:
    source = tmp_path / "source.docx"
    if not source.exists():
        _make_docx(source, "Replay cache evidence")
    state = _State({DOCX_MIME: (snapshot_path(source),)})
    return DocxRoute(
        DocxRouteConfig(
            tmp_path / "state" / "docx.sqlite3",
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        state,
        run_id,
        memory_gate=gate,
        cancellation=CancellationToken(),
    )


def test_docx_replay_uses_cache_metadata_without_opening_source_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _ReplayGate()
    cold = _route(tmp_path, 1, gate)
    assert cold.run().extracted == 1

    openings: list[str] = []
    original_zip_file = zipfile.ZipFile

    def counted_zip_file(*args, **kwargs):
        openings.append(str(args[0]) if args else "")
        return original_zip_file(*args, **kwargs)

    monkeypatch.setattr(docx_route.zipfile, "ZipFile", counted_zip_file)
    monkeypatch.setattr(
        docx_route,
        "_extract_docx_work",
        lambda _work: pytest.fail("a validated replay must not invoke DOCX extraction"),
    )
    replay = _route(tmp_path, 2, gate).run()

    assert (replay.cache_hits, replay.extracted, replay.errors) == (1, 0, 0)
    assert openings == []
    assert gate.active == gate.resident == 0


def test_docx_invalidated_representation_reopens_zip_and_reextracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _ReplayGate()
    cold = _route(tmp_path, 1, gate)
    assert cold.run().extracted == 1

    # Removing a cache-owned part makes the metadata preflight refuse the
    # cheap reservation.  The original bounded ZIP estimate and parser remain
    # the fallback, preserving revalidation rather than serving stale text.
    import sqlite3

    database = tmp_path / "state" / "docx.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM document_parts")
        connection.commit()

    openings: list[str] = []
    original_zip_file = zipfile.ZipFile

    def counted_zip_file(*args, **kwargs):
        openings.append(str(args[0]) if args else "")
        return original_zip_file(*args, **kwargs)

    monkeypatch.setattr(docx_route.zipfile, "ZipFile", counted_zip_file)
    replay = _route(tmp_path, 2, gate).run()

    assert (replay.cache_hits, replay.extracted, replay.errors) == (0, 1, 0)
    assert openings
    assert gate.active == gate.resident == 0
