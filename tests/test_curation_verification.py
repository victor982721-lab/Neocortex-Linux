"""Exact curation verification over bounded, isolated fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from neocortex.api import curation_api, curation_verification_api
from neocortex.api.cli import human
from neocortex.curation import verification as verification_module
from neocortex.curation.preview import build_curation_plan_page
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog


def _build_state(
    tmp_path: Path,
    *,
    exact_compare: bool,
    pair_count: int = 8,
    include_empty: bool = False,
    nested: bool = False,
) -> tuple[Path, Path]:
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    state.mkdir(parents=True)
    corpus.mkdir(parents=True)
    pair_root = corpus / "nested" if nested else corpus
    pair_root.mkdir(exist_ok=True)
    for pair in range(pair_count):
        payload = f"exact-pair-{pair}".encode("utf-8")
        (pair_root / f"pair-{pair}-a.txt").write_bytes(payload)
        (pair_root / f"pair-{pair}-b.txt").write_bytes(payload)
    for unique in range(4):
        (corpus / f"unique-{unique}.txt").write_bytes(f"unique-{unique}".encode("utf-8"))
    if include_empty:
        (corpus / "empty.bin").write_bytes(b"")
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(index, partial_threshold=0).plan(
            summary.scan_id,
            exact_compare=exact_compare,
        )
    initialize_document_catalog(state / "document_catalog.sqlite3")
    return state, corpus


def _patch_default_state(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    monkeypatch.setattr(curation_api, "default_state_directory", lambda: state)
    monkeypatch.setattr(curation_verification_api, "default_state_directory", lambda: state)


def test_persisted_fast_and_exact_modes_reach_the_plan_page(tmp_path: Path) -> None:
    fast_state, _ = _build_state(tmp_path / "fast", exact_compare=False)
    exact_state, _ = _build_state(tmp_path / "exact", exact_compare=True)

    fast = next(
        item
        for item in build_curation_plan_page(fast_state, 100).items
        if item.kind == "duplicate_group"
    )
    exact = next(
        item
        for item in build_curation_plan_page(exact_state, 100).items
        if item.kind == "duplicate_group"
    )

    assert fast.evidence["verification_mode"] == "fast"
    assert exact.evidence["verification_mode"] == "full_hash"


def test_verify_exactly_checks_fast_plan_without_authorizing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, corpus = _build_state(tmp_path, exact_compare=False)
    _patch_default_state(monkeypatch, state)
    scan = curation_verification_api.curation_scan_payload(request_id="scan")
    plan_id = scan["result"]["plan_digest"]  # type: ignore[index]

    result = curation_verification_api.curation_verify_payload(
        plan_id,
        request_id="verify",
    )

    assert result["status"] == "complete"
    verification = result["result"]
    assert verification["items_verified"] == 8  # type: ignore[index]
    duplicate = next(
        item
        for item in verification["items"]  # type: ignore[index]
        if item["kind"] == "duplicate_group"
    )
    assert duplicate["persisted_mode"] == "fast"
    assert duplicate["observed_mode"] == "full_hash"
    assert result["snapshot"]["source_heads"] == scan["snapshot"]["source_heads"]  # type: ignore[index]
    assert result["effects"] == {"state": "none", "corpus": "none", "external": "none"}
    assert result["trust"]["actions_authorized"] is False  # type: ignore[index]
    assert sorted(path.name for path in corpus.iterdir()) == sorted(
        path.name for path in corpus.iterdir()
    )


def test_verify_detects_in_place_content_change_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True)
    _patch_default_state(monkeypatch, state)
    page = build_curation_plan_page(state, 100)
    duplicate = next(item for item in page.items if item.kind == "duplicate_group")
    keep_path = Path(duplicate.evidence["keep_path"])
    original = keep_path.read_bytes()
    original_stat = keep_path.stat()
    mutated = bytes((original[0] ^ 1,)) + original[1:]
    keep_path.write_bytes(mutated)
    os.utime(keep_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    try:
        result = curation_verification_api.curation_verify_payload(
            page.plan_digest,
            request_id="verify-mutated",
        )
    finally:
        keep_path.write_bytes(original)
        os.utime(keep_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert result["status"] == "snapshot_changed"
    assert result["error"]["code"] == "snapshot_changed"  # type: ignore[index]
    assert result["result"]["items_failed"] == 1  # type: ignore[index]
    assert not (state / "framework.sqlite3").exists()


def test_scan_and_verify_cli_and_lazy_facades_share_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True)
    _patch_default_state(monkeypatch, state)

    scan_exit = human.run_human_command(("curate", "scan", "--json"))
    scan_output = capsys.readouterr()
    assert scan_exit == 0
    scan = curation_verification_api.curation_scan_payload(request_id="direct-scan")
    assert scan_output.err == ""
    assert scan_output.out
    scan_page = scan["result"]["plan_digest"]  # type: ignore[index]

    verify_exit = human.run_human_command(
        ("curate", "verify", scan_page, "--json")
    )
    verify_output = capsys.readouterr()
    assert verify_exit == 0
    assert '"operation":"curation-verify"' in verify_output.out

    from neocortex.api import public
    from neocortex import sdk

    assert public.curation_scan_payload is curation_verification_api.curation_scan_payload
    assert public.curation_verify_payload is curation_verification_api.curation_verify_payload
    assert sdk.curation_scan_payload is curation_verification_api.curation_scan_payload
    assert sdk.curation_verify_payload is curation_verification_api.curation_verify_payload


def test_verification_does_not_create_or_change_sqlite_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, corpus = _build_state(tmp_path, exact_compare=True)
    _patch_default_state(monkeypatch, state)
    before = {
        str(path.relative_to(corpus)): (path.stat(), path.read_bytes())
        for path in corpus.rglob("*")
        if path.is_file()
    }
    page = build_curation_plan_page(state, 100)
    result = curation_verification_api.curation_verify_payload(page.plan_digest)
    after = {
        str(path.relative_to(corpus)): (path.stat(), path.read_bytes())
        for path in corpus.rglob("*")
        if path.is_file()
    }
    assert result["status"] == "complete"
    assert before == after
    assert not (state / "framework.sqlite3").exists()


def test_verify_rejects_an_empty_item_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True)
    _patch_default_state(monkeypatch, state)
    page = build_curation_plan_page(state, 100)

    result = curation_verification_api.curation_verify_payload(
        page.plan_digest,
        item_ids=[],
    )

    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "invalid_request"  # type: ignore[index]
    assert result["exit_code"] == 2


def test_verify_handles_the_tail_page_with_a_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, pair_count=101)
    _patch_default_state(monkeypatch, state)
    first = curation_verification_api.curation_scan_payload(limit=100)
    first_page = first["result"]["page"]  # type: ignore[index]
    cursor = first_page["next_cursor"]  # type: ignore[index]
    assert isinstance(cursor, str)

    result = curation_verification_api.curation_verify_payload(
        first["plan_id"],  # type: ignore[arg-type]
        cursor=cursor,
        limit=100,
    )

    assert result["status"] == "complete"
    assert result["result"]["items_verified"] == 1  # type: ignore[index]
    assert result["snapshot"]["cursor"] == cursor  # type: ignore[index]


def test_verify_rejects_a_symlinked_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, corpus = _build_state(tmp_path, exact_compare=True, nested=True)
    _patch_default_state(monkeypatch, state)
    page = build_curation_plan_page(state, 100)
    nested = corpus / "nested"
    moved = tmp_path / "moved-nested"
    external = tmp_path / "external"
    nested.rename(moved)
    external.mkdir()
    for child in moved.iterdir():
        (external / child.name).write_bytes(child.read_bytes())
    nested.symlink_to(external, target_is_directory=True)
    try:
        result = curation_verification_api.curation_verify_payload(page.plan_digest)
    finally:
        nested.unlink()
        moved.rename(nested)

    assert result["status"] == "snapshot_changed"
    assert result["error"]["code"] == "snapshot_changed"  # type: ignore[index]


def test_verify_byte_budget_covers_the_single_read_per_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, pair_count=1)
    page = build_curation_plan_page(state, 100)
    observed = 0
    original_read = verification_module.os.read

    def counted_read(descriptor: int, size: int) -> bytes:
        nonlocal observed
        data = original_read(descriptor, size)
        observed += len(data)
        return data

    monkeypatch.setattr(verification_module.os, "read", counted_read)
    result = verification_module.verify_curation_page(page, max_bytes=24)

    assert result.status == "complete"
    assert result.bytes_checked == observed == 24


def test_mixed_plan_is_complete_when_all_applicable_duplicates_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _corpus = _build_state(tmp_path, exact_compare=True, include_empty=True)
    _patch_default_state(monkeypatch, state)
    page = build_curation_plan_page(state, 100)

    result = curation_verification_api.curation_verify_payload(page.plan_digest)

    assert result["status"] == "complete"
    assert result["result"]["items_verified"] == 8  # type: ignore[index]
    assert result["result"]["items_skipped"] == 0  # type: ignore[index]
    assert any(
        item["status"] == "not_applicable"  # type: ignore[index]
        for item in result["result"]["items"]  # type: ignore[index]
    )


def test_scan_preserves_the_cardinality_of_a_large_valid_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        {
            "item_id": f"duplicate:{index}",
            "kind": "duplicate_group",
            "status": "review",
            "action": "review_duplicate_group",
            "source_path": f"/fixture/{index}",
            "destination_path": None,
            "reason": "duplicate_content_candidate",
            "evidence": {"members": [{"path": f"/fixture/{index}/{member}"} for member in range(64)]},
        }
        for index in range(100)
    ]
    expected = {
        "coverage": "complete",
        "snapshot": {"snapshot_id": "snapshot", "scan_id": 1, "root": "/fixture"},
        "page": {
            "plan_digest": "sha256:" + "a" * 64,
            "items": items,
            "items_total": 100,
            "next_cursor": None,
        },
        "error": None,
    }
    monkeypatch.setattr(curation_verification_api, "curation_plan_payload", lambda **_: expected)

    result = curation_verification_api.curation_scan_payload(limit=100)

    assert result["status"] == "complete"
    assert len(result["result"]["page"]["items"]) == 100  # type: ignore[index]


@pytest.mark.parametrize(
    ("error_code", "expected_exit"),
    (("snapshot_changed", 5), ("corrupt", 7), ("unavailable", 1)),
)
def test_scan_preserves_typed_plan_error_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_exit: int,
) -> None:
    expected = {
        "coverage": "unavailable",
        "snapshot": {"snapshot_id": None, "scan_id": None, "root": None, "source_heads": []},
        "page": {"items": []},
        "error": {"code": error_code, "message": "fixture", "retryable": False},
    }
    monkeypatch.setattr(curation_verification_api, "curation_plan_payload", lambda **_: expected)

    result = curation_verification_api.curation_scan_payload()

    assert result["error"]["code"] == error_code  # type: ignore[index]
    assert result["exit_code"] == expected_exit
