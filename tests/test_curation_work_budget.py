"""Read-only work-budget contracts for exact curation verification."""

from __future__ import annotations

from pathlib import Path

import pytest

import neocortex.curation as curation
from neocortex.curation import verification
from neocortex.curation.preview import build_curation_plan_page
from neocortex.curation.verification import CurationWorkBudget, verify_curation_page
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog


def _page(tmp_path: Path, *, pair_count: int = 3):
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir()
    corpus.mkdir()
    for index in range(pair_count):
        payload = f"budget-pair-{index}".encode()
        (corpus / f"pair-{index}-a.txt").write_bytes(payload)
        (corpus / f"pair-{index}-b.txt").write_bytes(payload)
    with DedupIndex(state / "dedup.sqlite3") as owner:
        summary = owner.scan(corpus)
        owner.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(owner, partial_threshold=0).plan(summary.scan_id, exact_compare=True)
    initialize_document_catalog(state / "document_catalog.sqlite3")
    return state, corpus, build_curation_plan_page(state, 100)


def test_default_budget_preserves_the_existing_result(tmp_path: Path) -> None:
    state, corpus, page = _page(tmp_path)
    before = {path.name: path.read_bytes() for path in corpus.iterdir()}

    existing = verify_curation_page(page)
    explicit_default = verify_curation_page(page, budget=CurationWorkBudget())

    assert explicit_default.to_dict() == existing.to_dict()
    assert {path.name: path.read_bytes() for path in corpus.iterdir()} == before
    assert not (state / "framework.sqlite3").exists()


def test_budget_is_available_from_the_lazy_curation_facade() -> None:
    assert curation.CurationWorkBudget is CurationWorkBudget


def test_none_returning_cancellation_checkpoint_is_not_a_stop(
    tmp_path: Path,
) -> None:
    _state, _corpus, page = _page(tmp_path, pair_count=1)

    result = verify_curation_page(
        page,
        budget=CurationWorkBudget(cancellation_check=lambda: None),
    )

    assert result.status == "complete"
    assert result.items_verified == 1


def test_item_budget_keeps_completed_and_marks_remaining_items(tmp_path: Path) -> None:
    _state, _corpus, page = _page(tmp_path)

    result = verify_curation_page(page, budget=CurationWorkBudget(max_items=1))

    assert result.status == "partial"
    assert result.coverage == "partial"
    assert len(result.items) == 3
    assert result.items[0].status == "verified"
    assert [item.reason for item in result.items[1:]] == [
        "budget_exhausted",
        "budget_exhausted",
    ]
    assert result.items_verified == 1
    assert result.items_skipped == 2


def test_file_budget_preserves_partial_counts_and_stops_the_page(tmp_path: Path) -> None:
    _state, _corpus, page = _page(tmp_path)
    first_size = int(page.items[0].evidence["size"])

    result = verify_curation_page(page, budget=CurationWorkBudget(max_files=1))

    assert result.status == "partial"
    assert result.files_checked == 1
    assert result.bytes_checked == first_size
    assert result.items[0].reason == "budget_exhausted"
    assert result.items[0].checked_files == 1
    assert result.items[0].verified_files == 1
    assert all(item.reason == "budget_exhausted" for item in result.items[1:])


def test_byte_budget_preserves_the_exact_bytes_already_read(tmp_path: Path) -> None:
    _state, _corpus, page = _page(tmp_path)
    first_size = int(page.items[0].evidence["size"])

    result = verify_curation_page(page, budget=CurationWorkBudget(max_bytes=first_size))

    assert result.status == "partial"
    assert result.files_checked == 1
    assert result.bytes_checked == first_size
    assert result.items[0].reason == "budget_exhausted"
    assert result.items[0].checked_files == 1
    assert result.items[0].verified_files == 1


def test_cancellation_during_a_read_preserves_observed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _state, _corpus, page = _page(tmp_path, pair_count=1)
    cancelled = False
    original_read = verification.os.read

    def cancelling_read(descriptor: int, size: int) -> bytes:
        nonlocal cancelled
        payload = original_read(descriptor, size)
        cancelled = True
        return payload

    monkeypatch.setattr(verification.os, "read", cancelling_read)
    result = verify_curation_page(
        page,
        budget=CurationWorkBudget(cancellation_check=lambda: cancelled),
    )

    assert result.status == "partial"
    assert result.files_checked == 1
    assert result.bytes_checked == int(page.items[0].evidence["size"])
    assert result.items[0].reason == "cancelled"
    assert result.items[0].checked_files == 1
    assert result.items[0].verified_files == 0


def test_expired_monotonic_deadline_stops_before_file_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _state, _corpus, page = _page(tmp_path)

    def unexpected_read(_descriptor: int, _size: int) -> bytes:
        raise AssertionError("deadline must stop before corpus reads")

    monkeypatch.setattr(verification.os, "read", unexpected_read)
    result = verify_curation_page(
        page,
        budget=CurationWorkBudget(
            deadline_monotonic=10.0,
            monotonic_clock=lambda: 10.0,
        ),
    )

    assert result.status == "partial"
    assert result.files_checked == 0
    assert result.bytes_checked == 0
    assert {item.reason for item in result.items} == {"deadline_exceeded"}


def test_callback_failure_fails_closed_as_cancelled(tmp_path: Path) -> None:
    _state, _corpus, page = _page(tmp_path)

    def broken_callback() -> bool:
        raise RuntimeError("untrusted callback detail")

    result = verify_curation_page(
        page,
        budget=CurationWorkBudget(cancellation_check=broken_callback),
    )

    assert result.status == "partial"
    assert {item.reason for item in result.items} == {"cancelled"}
    assert "untrusted callback detail" not in str(result.to_dict())


@pytest.mark.parametrize(
    "kwargs",
    (
        {"max_items": 0},
        {"max_files": 513},
        {"max_bytes": True},
        {"deadline_monotonic": float("inf")},
        {"cancellation_check": False},
        {"monotonic_clock": None},
    ),
)
def test_invalid_budget_contracts_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        CurationWorkBudget(**kwargs)  # type: ignore[arg-type]
