"""Opt-in, typed Text retry behavior and durable failure evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import neocortex.capabilities.formats.text.text_route as text_route_module
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.route_filters import CandidateSelection


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


class _Framework:
    def __init__(self, snapshots: tuple[FileSnapshot, ...]) -> None:
        self.snapshots = snapshots

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        _max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert route_name == "text"
        return (len(self.snapshots), len(self.snapshots)) if mime == "text/plain" else (0, 0)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection: CandidateSelection,
    ):
        assert route_name == "text"
        if mime == "text/plain":
            yield from self.snapshots


def _route(
    state: Path,
    source: Path,
    run_id: int,
    *,
    retry_recoverable_errors: bool = False,
    snapshots: tuple[FileSnapshot, ...] | None = None,
) -> TextRoute:
    current = snapshot_path(source)
    return TextRoute(
        TextRouteConfig(
            state_path=state,
            retry_recoverable_errors=retry_recoverable_errors,
        ),
        _Framework(snapshots or (current,)),
        run_id,
        cancellation=CancellationToken(),
    )


def _failed_receipt(state: Path) -> tuple[sqlite3.Row, dict[str, object]]:
    with sqlite3.connect(state) as connection:
        connection.row_factory = sqlite3.Row
        document = connection.execute("SELECT * FROM documents").fetchone()
        receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM text_work_receipts WHERE outcome='failed' "
                "ORDER BY recorded_ns DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert document is not None
    return document, receipt


def test_text_retry_flag_is_kw_only_and_defaults_disabled(tmp_path: Path) -> None:
    config = TextRouteConfig(tmp_path / "text.sqlite3")
    assert config.retry_recoverable_errors is False
    assert TextRouteConfig(
        tmp_path / "text.sqlite3", retry_recoverable_errors=True
    ).retry_recoverable_errors is True

    with pytest.raises(TypeError):
        TextRouteConfig(
            tmp_path / "text.sqlite3",
            None,
            None,
            100,
            60,
            1024,
            False,
            CandidateSelection(),
            True,
        )

    invalid = TextRouteConfig(tmp_path / "text.sqlite3")
    object.__setattr__(invalid, "retry_recoverable_errors", 1)
    with pytest.raises(ValueError, match="retry_recoverable_errors"):
        TextRoute(invalid, _Framework(()), 1).run()


def test_text_retries_one_typed_cached_failure_and_never_retries_success(
    tmp_path: Path,
) -> None:
    source = tmp_path / "retry.txt"
    source.write_text("contenido de reintento", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    original_extract = text_route_module._extract

    with patch.object(
        text_route_module,
        "_extract",
        side_effect=OSError("transient source unavailable"),
    ):
        first = _route(state, source, 1).run()
    assert (first.errors, first.retryable_errors) == (1, 1)
    document, receipt = _failed_receipt(state)
    assert document["retryable"] == 1
    assert receipt["failure"]["retryable"] is True
    assert receipt["failure"]["details"]["recommendation"] == "retry"

    with patch.object(
        text_route_module,
        "_extract",
        side_effect=AssertionError("default must retain typed failure"),
    ) as default_extract:
        retained = _route(state, source, 2).run()
    default_extract.assert_not_called()
    assert (retained.cache_hits, retained.cached_errors, retained.extracted) == (1, 1, 0)

    with patch.object(text_route_module, "_extract", wraps=original_extract) as retry_extract:
        recovered = _route(
            state,
            source,
            3,
            retry_recoverable_errors=True,
        ).run()
    assert retry_extract.call_count == 1
    assert (recovered.extracted, recovered.errors, recovered.cached_errors) == (1, 0, 0)

    with patch.object(
        text_route_module,
        "_extract",
        side_effect=AssertionError("a successful Text result must not rerun"),
    ) as success_extract:
        replay = _route(
            state,
            source,
            4,
            retry_recoverable_errors=True,
        ).run()
    success_extract.assert_not_called()
    assert (replay.cache_hits, replay.extracted, replay.errors) == (1, 0, 0)


def test_text_retry_claim_is_once_per_file_and_run_for_duplicate_candidates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "duplicate.txt"
    source.write_text("contenido duplicado", encoding="utf-8")
    snapshot = snapshot_path(source)
    state = tmp_path / "text.sqlite3"
    with patch.object(
        text_route_module,
        "_extract",
        side_effect=OSError("retryable first failure"),
    ):
        _route(state, source, 1).run()

    with patch.object(
        text_route_module,
        "_extract",
        side_effect=OSError("retryable second failure"),
    ) as retry_extract:
        replay = _route(
            state,
            source,
            2,
            retry_recoverable_errors=True,
            snapshots=(snapshot, snapshot),
        ).run()

    assert retry_extract.call_count == 1
    assert (replay.processed, replay.errors) == (1, 1)
    assert (replay.cache_hits, replay.cached_errors) == (1, 1)


def test_text_retry_requires_structured_evidence_not_error_message_or_unknown_flag(
    tmp_path: Path,
) -> None:
    source = tmp_path / "manual.txt"
    source.write_text("contenido con mensaje de retry", encoding="utf-8")
    state = tmp_path / "text.sqlite3"

    with patch.object(
        text_route_module,
        "_extract",
        side_effect=ValueError("retry this based on message only"),
    ):
        _route(state, source, 1).run()
    with patch.object(
        text_route_module,
        "_extract",
        side_effect=AssertionError("message text must not authorize retry"),
    ) as no_retry:
        retained = _route(
            state,
            source,
            2,
            retry_recoverable_errors=True,
        ).run()
    no_retry.assert_not_called()
    assert retained.cached_errors == 1

    source2 = tmp_path / "unknown.txt"
    source2.write_text("unknown retry marker", encoding="utf-8")
    state2 = tmp_path / "unknown.sqlite3"
    with patch.object(
        text_route_module,
        "_extract",
        side_effect=OSError("typed retryable failure"),
    ):
        _route(state2, source2, 1).run()
    with sqlite3.connect(state2) as connection:
        connection.execute("UPDATE documents SET retryable=2")
        connection.commit()
    with patch.object(
        text_route_module,
        "_extract",
        side_effect=AssertionError("unknown retry marker must not authorize retry"),
    ) as unknown_retry:
        retained_unknown = _route(
            state2,
            source2,
            2,
            retry_recoverable_errors=True,
        ).run()
    unknown_retry.assert_not_called()
    assert retained_unknown.cached_errors == 1
