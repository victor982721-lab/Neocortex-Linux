"""End-to-end checks for the fixed-root curation API on isolated state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.api.curation_api import curation_plan_payload
from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryCheckpoint
from neocortex.documents.document_catalog import initialize_document_catalog


def _build_state(state: Path, corpus: Path) -> None:
    state.mkdir(parents=True)
    corpus.mkdir()
    (corpus / "keep.txt").write_bytes(b"same")
    (corpus / "duplicate.txt").write_bytes(b"same")
    (corpus / "empty.txt").write_bytes(b"")
    with DedupIndex(state / "dedup.sqlite3") as index:
        summary = index.scan(corpus)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(str(corpus), summary.scan_id, None, None, None, True)
        )
        DedupPlanner(index, partial_threshold=0).plan(summary.scan_id, exact_compare=False)
    catalog = state / "document_catalog.sqlite3"
    initialize_document_catalog(catalog)
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            """INSERT INTO catalog_runs(
            catalog_run_id,source_kind,mode,status,started_ns,completed_ns,summary_json)
            VALUES (1,'all','plan','completed',1,2,'{}')"""
        )
        connection.execute(
            """INSERT INTO organization_plans(
            catalog_run_id,source_kind,file_key,source_path,destination_path,
            organization_root,volume_id,file_id,size,mtime_ns,birthtime_ns,
            classifier_signature,primary_kind,confidence,status,reason,
            evidence_json,planned_ns)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                1,
                "text",
                "text:1",
                str(corpus / "keep.txt"),
                str(state / "organized" / "keep.txt"),
                str(state / "organized"),
                "1",
                "2",
                4,
                1,
                -1,
                "test-classifier",
                "text",
                0.9,
                "planned",
                "classification_above_threshold",
                '{"uncertainty":"low"}',
                1,
            ),
        )


def test_public_curation_api_paginates_the_same_local_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    xdg_state = tmp_path / "xdg-state"
    xdg_data = tmp_path / "xdg-data"
    state = xdg_state / "Neocortex" / "state"
    _build_state(state, tmp_path / "corpus")
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

    first = curation_plan_payload(limit=1, request_id="fic-e2e-1")
    assert first["coverage"] == "complete"
    assert first["page"]["next_cursor"] is not None
    assert first["page"]["items"][0]["reason"] == "duplicate_content_candidate"
    assert first["trust"]["actions_authorized"] is False
    assert first["effects"]["corpus"] == "none"

    second = curation_plan_payload(
        limit=2,
        cursor=first["page"]["next_cursor"],
        request_id="fic-e2e-2",
    )
    assert second["coverage"] == "complete"
    assert second["page"]["plan_digest"] == first["page"]["plan_digest"]
    identifiers = [item["item_id"] for item in first["page"]["items"]]
    identifiers.extend(item["item_id"] for item in second["page"]["items"])
    assert len(identifiers) == first["page"]["items_total"]
    assert len(identifiers) == len(set(identifiers))
    json.dumps(first, ensure_ascii=False, allow_nan=False)


def test_public_curation_api_converts_incompatible_state_to_typed_error(
    monkeypatch,
) -> None:
    class StateFailure(RuntimeError):
        pass

    def failing_contract():
        def fail(*_args, **_kwargs):
            raise StateFailure("owner changed while reading")

        return StateFailure, fail

    monkeypatch.setattr("neocortex.api.curation_api._plan_contract", failing_contract)

    payload = curation_plan_payload(limit=3, request_id="fic-error")

    assert payload["coverage"] == "unavailable"
    assert payload["error"] == {
        "code": "unavailable",
        "message": "owner changed while reading",
        "retryable": False,
    }
    assert payload["page"]["items"] == []
    assert payload["effects"] == {"state": "none", "corpus": "none", "external": "none"}


@pytest.mark.parametrize(
    ("failure", "expected_code", "retryable"),
    (
        (RuntimeError("curation cursor is invalid"), "invalid_cursor", False),
        (RuntimeError("curation cursor snapshot changed"), "snapshot_changed", True),
        (RuntimeError("document catalog schema lacks required columns"), "schema_incompatible", False),
        (sqlite3.DatabaseError("database disk image is malformed"), "corrupt", False),
        (sqlite3.OperationalError("database is locked"), "unavailable", False),
        (OSError("state owner is unavailable"), "unavailable", False),
        (TypeError("curation plan page has an incompatible shape"), "schema_incompatible", False),
        (ValueError("producer returned an invalid value"), "schema_incompatible", False),
    ),
)
def test_public_curation_api_maps_known_failures_to_stable_codes(
    failure: BaseException,
    expected_code: str,
    retryable: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_contract():
        def fail(*_args, **_kwargs):
            raise failure

        return RuntimeError, fail

    monkeypatch.setattr("neocortex.api.curation_api._plan_contract", failing_contract)

    payload = curation_plan_payload(limit=2, request_id="typed-error")

    assert payload["error"]["code"] == expected_code
    assert payload["error"]["retryable"] is retryable
    assert payload["coverage"] == "unavailable"
    assert payload["page"]["items"] == []


def test_public_curation_api_maps_lazy_contract_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_contract():
        raise ImportError("preview contract is unavailable")

    monkeypatch.setattr("neocortex.api.curation_api._plan_contract", missing_contract)

    payload = curation_plan_payload(limit=2, request_id="import-error")

    assert payload["error"]["code"] == "unavailable"
    assert payload["error"]["message"] == "preview contract is unavailable"


@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    (
        ({"limit": 0}, "invalid_request"),
        ({"limit": 101}, "invalid_request"),
        ({"cursor": "\x00"}, "invalid_cursor"),
        ({"request_id": "\x00"}, "invalid_request"),
    ),
)
def test_public_curation_api_returns_typed_input_errors(
    kwargs: dict[str, object],
    expected_code: str,
) -> None:
    payload = curation_plan_payload(**kwargs)  # type: ignore[arg-type]

    assert payload["coverage"] == "unavailable"
    assert payload["error"]["code"] == expected_code
    assert payload["page"]["items"] == []


@dataclass(frozen=True)
class _LargeEvidenceItem:
    item_id: str
    evidence_size: int = 20_500

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "kind": "empty_file",
            "status": "review",
            "action": "review_empty_file",
            "source_path": "\x1b[31m/Corpus/untrusted",
            "destination_path": None,
            "reason": "fixture",
            "evidence": {"members": list(range(self.evidence_size))},
        }


def test_public_curation_api_sanitizes_evidence_without_truncating_page_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = SimpleNamespace(
        coverage="complete",
        missing_owners=(),
        root="/Corpus",
        scan_id=7,
        inventory_files=2,
        duplicate_groups=0,
        duplicate_members=0,
        reclaimable_bytes=0,
        organization_plans=0,
        empty_files=2,
        limit=2,
        cursor=None,
        next_cursor=None,
        snapshot_id="snapshot-fixture",
        plan_digest="sha256:fixture",
        items_total=2,
        items=(_LargeEvidenceItem("item-1"), _LargeEvidenceItem("item-2")),
    )

    def contract():
        return RuntimeError, lambda *_args, **_kwargs: page

    monkeypatch.setattr("neocortex.api.curation_api._plan_contract", contract)

    payload = curation_plan_payload(limit=2, request_id="sanitizer-regression")
    items = payload["page"]["items"]

    assert len(items) == 2
    assert all(
        set(item)
        == {
            "item_id",
            "kind",
            "status",
            "action",
            "source_path",
            "destination_path",
            "reason",
            "evidence",
        }
        for item in items
    )
    assert all(isinstance(item["evidence"], dict) for item in items)
    assert all(
        item["evidence"]["members"][-1] == "[contenido omitido por límite]"
        for item in items
    )
    assert all("\x1b" not in item["source_path"] for item in items)
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
