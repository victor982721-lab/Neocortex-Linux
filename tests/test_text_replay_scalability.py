"""Bounded Text replay regressions and replay transaction measurements."""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import neocortex.capabilities.formats.text.text_route as text_route_module
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.route_filters import CandidateSelection


class _Candidates:
    def __init__(self, snapshots: tuple[FileSnapshot, ...]) -> None:
        self.snapshots = snapshots

    def selected_route_candidate_counts(
        self,
        run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        selection: CandidateSelection,
    ) -> tuple[int, int]:
        del run_id, route_name, selection
        if mime != "text/plain":
            return 0, 0
        eligible = sum(
            max_file_bytes is None or snapshot.size <= max_file_bytes
            for snapshot in self.snapshots
        )
        return len(self.snapshots), eligible

    def iter_selected_route_candidates(
        self,
        run_id: int,
        mime: str,
        route_name: str,
        selection: CandidateSelection,
    ) -> Iterator[FileSnapshot]:
        del run_id, route_name, selection
        if mime == "text/plain":
            yield from self.snapshots


class _ElasticGate:
    """Small deterministic gate that selects Text's coordinated path."""

    peak_reserved_bytes = 0
    wait_count = 0

    def worker_capacity(self, **_kwargs: object) -> int:
        return 1

    @contextmanager
    def admit(self, estimated_bytes: int, **_kwargs: object):
        self.peak_reserved_bytes = max(self.peak_reserved_bytes, estimated_bytes)
        yield


def _corpus(root: Path, count: int) -> tuple[FileSnapshot, ...]:
    root.mkdir(parents=True, exist_ok=True)
    snapshots: list[FileSnapshot] = []
    for index in range(count):
        source = root / f"document-{index:04d}.txt"
        source.write_text(
            f"Documento {index}; evidencia de replay estricto y límites acotados.\n"
            * (1 + index % 5),
            encoding="utf-8",
        )
        snapshots.append(snapshot_path(source))
    return tuple(snapshots)


def _route(
    database: Path,
    candidates: _Candidates,
    run_id: int,
    *,
    memory_gate=None,
) -> TextRoute:
    return TextRoute(
        TextRouteConfig(state_path=database, max_file_bytes=2 * 1024 * 1024),
        candidates,
        run_id,
        memory_gate=memory_gate,
        cancellation=CancellationToken(),
    )


def test_text_replay_does_not_reparse_and_keeps_strict_source_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = _corpus(tmp_path / "corpus", 80)
    candidates = _Candidates(snapshots)
    database = tmp_path / "text.sqlite3"
    first = _route(database, candidates, 1).run()
    assert first.extracted == len(snapshots)

    reads: list[int] = []
    original_read = text_route_module._read_exact

    def read(*args, **kwargs):
        payload = original_read(*args, **kwargs)
        reads.append(len(payload))
        return payload

    def forbidden_extract(*_args, **_kwargs):
        raise AssertionError("a validated replay must not invoke the extractor")

    monkeypatch.setattr(text_route_module, "_read_exact", read)
    monkeypatch.setattr(text_route_module, "_extract", forbidden_extract)
    replay = _route(database, candidates, 2).run()

    assert replay.cache_hits == len(snapshots)
    assert replay.extracted == replay.processed == 0
    # Strict byte hashing/identity validation remains in the replay path; only
    # parser/model work is removed.
    assert sum(reads) == sum(snapshot.size for snapshot in snapshots)


def test_coordinated_replay_publishes_each_hit_in_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = _corpus(tmp_path / "corpus", 12)
    candidates = _Candidates(snapshots)
    database = tmp_path / "text.sqlite3"
    first = _route(database, candidates, 1).run()
    assert first.extracted == len(snapshots)

    statements: list[str] = []
    original_database = text_route_module.text_database

    @contextmanager
    def traced_database(
        path: Path, *, readonly: bool = False, create: bool = True,
    ):
        with original_database(path, readonly=readonly, create=create) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(text_route_module, "text_database", traced_database)
    replay = _route(database, candidates, 2, memory_gate=_ElasticGate()).run()

    assert replay.cache_hits == len(snapshots)
    begins = [sql for sql in statements if sql == "BEGIN IMMEDIATE"]
    # One transaction contains the running attempt and terminal cache-hit
    # receipt.  This is a structural A/B metric, not an elapsed-time claim.
    # The route's final stale-prune fence contributes one additional empty
    # transaction; every replay hit contributes exactly one of the rest.
    assert len(begins) == len(snapshots) + 1


def test_replay_profile_records_cold_and_hit_work_without_claiming_speedup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = _corpus(tmp_path / "corpus", 120)
    candidates = _Candidates(snapshots)
    database = tmp_path / "text.sqlite3"
    extract_calls = 0
    original_extract = text_route_module._extract

    def count_extract(*args, **kwargs):
        nonlocal extract_calls
        extract_calls += 1
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(text_route_module, "_extract", count_extract)
    started = time.perf_counter_ns()
    cold = _route(database, candidates, 1).run()
    cold_ns = time.perf_counter_ns() - started
    assert extract_calls == len(snapshots)

    extract_calls = 0
    started = time.perf_counter_ns()
    replay = _route(database, candidates, 2).run()
    replay_ns = time.perf_counter_ns() - started

    assert extract_calls == 0
    assert replay.cache_hits == len(snapshots)
    assert replay.text_chars == cold.text_chars
    # Keep the measurements available to a caller without making a brittle
    # wall-clock promise; environment noise and process startup are real.
    assert cold_ns > 0 and replay_ns > 0
