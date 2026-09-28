"""Lossless storage must not weaken causal evidence, budgets or old receipts."""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from dataclasses import replace

import pytest

from neocortex.semantic.derivation_contracts import MAX_WORK_RECEIPT_JSON_BYTES, WorkReceipt
from neocortex.semantic.semantic_lineage_repository import read_semantic_derivation_outbox
from neocortex.semantic.semantic_models import canonical_json
from neocortex.semantic.semantic_receipt_storage import (
    RECEIPT_STORAGE_SCHEMA,
    decode_receipt_storage,
    encode_receipt_storage,
    receipt_storage_logical_bytes,
)
from neocortex.semantic.semantic_state import initialize_semantic_state, semantic_database
from tests.test_semantic_derivation_lineage import _initialize, _stage
from tests.test_semantic_receipt_schema_compatibility import _v7_fixture


def _fixture_receipt(tmp_path) -> WorkReceipt:
    database = tmp_path / "semantic.sqlite3"
    _initialize(database)
    _stage(database, item_id="storage-fixture", source_revision_id="revision:fixture:storage",
           text="evidencia técnica del transformador con procedencia verificada", ordinal=1)
    event = read_semantic_derivation_outbox(database)[0]
    return WorkReceipt.from_dict(event.receipt)


def test_large_binding_fanin_is_lossless_and_substantially_smaller(tmp_path) -> None:
    receipt = _fixture_receipt(tmp_path)
    large = replace(receipt, inputs=tuple(replace(receipt.inputs[0], name=f"input-{i}") for i in range(128)))
    logical = large.to_json()
    stored = encode_receipt_storage(logical)
    assert json.loads(stored)["schema"] == RECEIPT_STORAGE_SCHEMA
    assert len(stored.encode()) < len(logical.encode()) // 3
    decoded = decode_receipt_storage(stored, owner_schema_version=11)
    assert decoded == logical
    assert WorkReceipt.from_json(decoded).contract_fingerprint == large.contract_fingerprint
    assert receipt_storage_logical_bytes(stored) == len(logical.encode())
    assert decode_receipt_storage(logical, owner_schema_version=7) == logical


@pytest.mark.parametrize("fault", ["digest", "size", "projection", "truncated", "trailing", "field", "base64"])
def test_corrupted_storage_never_authenticates(fault, tmp_path) -> None:
    receipt = _fixture_receipt(tmp_path)
    payload = json.loads(encode_receipt_storage(receipt.to_json()))
    assert payload["schema"] == RECEIPT_STORAGE_SCHEMA
    if fault == "digest":
        payload["logical_sha256"] = "0" * 64
    elif fault == "size":
        payload["logical_bytes"] += 1
    elif fault == "projection":
        payload["outputs"][0]["materialization"]["materialization_id"] = "forged"
    elif fault in {"truncated", "trailing"}:
        raw = base64.b64decode(payload["body"])
        raw = raw[:-1] if fault == "truncated" else raw + b"extra stream"
        payload["body"] = base64.b64encode(raw).decode()
    elif fault == "field":
        payload["ignored_field"] = "must fail closed"
    else:
        payload["body"] = "!not-base64!"
    with pytest.raises(ValueError):
        decode_receipt_storage(canonical_json(payload), owner_schema_version=11)


def test_size_lie_and_decompression_bomb_remain_bounded(tmp_path) -> None:
    receipt = _fixture_receipt(tmp_path)
    payload = json.loads(encode_receipt_storage(receipt.to_json()))
    raw = b"x" * (MAX_WORK_RECEIPT_JSON_BYTES + 1)
    payload.update(logical_bytes=MAX_WORK_RECEIPT_JSON_BYTES,
                   logical_sha256=hashlib.sha256(raw).hexdigest(),
                   body=base64.b64encode(zlib.compress(raw)).decode())
    with pytest.raises(ValueError, match="size, stream or digest"):
        decode_receipt_storage(canonical_json(payload), owner_schema_version=11)
    payload["logical_bytes"] = False
    stored = canonical_json(payload)
    assert receipt_storage_logical_bytes(stored) == MAX_WORK_RECEIPT_JSON_BYTES
    with pytest.raises(ValueError, match="size"):
        decode_receipt_storage(stored, owner_schema_version=11)


def test_storage_requires_new_owner_and_rejects_duplicate_fields(tmp_path) -> None:
    logical = _fixture_receipt(tmp_path).to_json()
    stored = encode_receipt_storage(logical)
    with pytest.raises(ValueError, match="schema 11"):
        decode_receipt_storage(stored, owner_schema_version=10)
    forged = stored[:-1] + ',"schema":"duplicate"}'
    with pytest.raises(ValueError, match="duplicate"):
        decode_receipt_storage(forged)


def test_v7_migration_preserves_every_old_receipt_byte_and_public_event(tmp_path) -> None:
    fixture = _v7_fixture(tmp_path)
    database = fixture["database"]
    with semantic_database(database, readonly=True) as connection:
        before = [tuple(row) for row in connection.execute(
            "SELECT receipt_id,receipt_key,receipt_json FROM semantic_work_receipts ORDER BY receipt_id"
        )]
    events_before = read_semantic_derivation_outbox(database)
    initialize_semantic_state(database)
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        after = [tuple(row) for row in connection.execute(
            "SELECT receipt_id,receipt_key,receipt_json FROM semantic_work_receipts ORDER BY receipt_id"
        )]
    assert before == after
    assert read_semantic_derivation_outbox(database) == events_before
