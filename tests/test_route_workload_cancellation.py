"""Public candidate-backed workload adapters stop lazy reads cooperatively."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.route_registry import (
    RouteExecutionContext,
    builtin_route_registry,
)
from neocortex.safety.route_filters import CandidateSelection


class _LazyCandidates:
    def __init__(
        self,
        database: Path,
        cancellation: CancellationToken,
        *,
        count: int = 5000,
        cancel_at: int | None = None,
        cancel_on_exhaustion: bool = False,
    ) -> None:
        self.candidate_database = database
        self.cancellation = cancellation
        self.count = count
        self.cancel_at = cancel_at
        self.cancel_on_exhaustion = cancel_on_exhaustion
        self.opened = 0
        self.consumed = 0

    def _rows(self) -> Iterator[FileSnapshot]:
        self.opened += 1
        for number in range(self.count):
            self.consumed += 1
            if self.consumed == self.cancel_at:
                self.cancellation.cancel()
            yield FileSnapshot(
                str(self.candidate_database.parent / f"{number:06d}"),
                1, number + 1, 100, 1, -1,
            )
        if self.cancel_on_exhaustion:
            self.cancellation.cancel()

    def iter_selected_route_candidates(self, *_args: object) -> Iterator[FileSnapshot]:
        yield from self._rows()

    def iter_selected_route_candidates_by_prefix(
        self, *_args: object,
    ) -> Iterator[tuple[str, FileSnapshot]]:
        for snapshot in self._rows():
            yield "image/png", snapshot


def _estimate(
    tmp_path: Path,
    route: str,
    view: _LazyCandidates,
    *,
    max_file_bytes: int | None = None,
    max_documents: int | None = None,
) -> tuple[int, int]:
    config = Namespace(selection=CandidateSelection())
    setattr(config, f"{route}_max_file_bytes", max_file_bytes)
    setattr(config, f"{route}_max_documents", max_documents)
    context = RouteExecutionContext(
        config=cast(FrameworkConfig, config), root=tmp_path,
        framework_state=cast(Any, view), run_id=1, scan_id=1,
        progress=None, resource_coordinator=None, cancellation=view.cancellation,
    )
    adapter = builtin_route_registry()[route]
    assert adapter.estimate_workload is not None
    return adapter.estimate_workload(context)


@pytest.mark.parametrize("route", ("pdf", "image"))
def test_workload_cancelled_before_reading_does_not_open_candidates(
    tmp_path: Path, route: str,
) -> None:
    token = CancellationToken()
    token.cancel()
    view = _LazyCandidates(tmp_path / "candidates.sqlite3", token)
    with pytest.raises(CancellationRequested, match="framework cancellation requested"):
        _estimate(tmp_path, route, view)
    assert (view.opened, view.consumed) == (0, 0)


@pytest.mark.parametrize("route", ("pdf", "image"))
@pytest.mark.parametrize("max_file_bytes", (None, 1))
def test_workload_stops_at_cancelled_row_even_when_all_rows_are_filtered(
    tmp_path: Path, route: str, max_file_bytes: int | None,
) -> None:
    token = CancellationToken()
    view = _LazyCandidates(tmp_path / "candidates.sqlite3", token, cancel_at=10)
    with pytest.raises(CancellationRequested):
        _estimate(tmp_path, route, view, max_file_bytes=max_file_bytes)
    assert (view.opened, view.consumed) == (1, 10)


@pytest.mark.parametrize("route", ("pdf", "image"))
@pytest.mark.parametrize("count", (0, 10))
def test_workload_checks_cancellation_after_the_final_source_read(
    tmp_path: Path, route: str, count: int,
) -> None:
    token = CancellationToken()
    view = _LazyCandidates(
        tmp_path / "candidates.sqlite3", token, count=count, cancel_on_exhaustion=True,
    )
    with pytest.raises(CancellationRequested):
        _estimate(tmp_path, route, view)
    assert (view.opened, view.consumed) == (1, count)


@pytest.mark.parametrize("route", ("pdf", "image"))
def test_workload_count_limit_preserves_the_full_eligible_byte_bound(
    tmp_path: Path, route: str,
) -> None:
    token = CancellationToken()
    view = _LazyCandidates(tmp_path / "candidates.sqlite3", token, count=12)
    assert _estimate(tmp_path, route, view, max_documents=2) == (2, 200)
    assert view.consumed == 12
    assert _estimate(tmp_path, route, view, max_file_bytes=1) == (0, 0)
    assert view.consumed == 24
