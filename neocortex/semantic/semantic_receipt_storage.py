"""Lossless, bounded storage of logical v1 receipts in semantic owner v11.

Public WorkReceipt, fingerprints and outbox hashes remain the canonical v1
bytes. The private v2 envelope compresses those bytes and retains only the
output lookup projection needed by SQLite. Historical rows are never rewritten.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zlib

from .derivation_contracts import MAX_WORK_RECEIPT_JSON_BYTES
from .semantic_models import canonical_json


RECEIPT_STORAGE_SCHEMA = "neocortex.semantic-receipt-storage/v2"
_STORAGE_FIELDS = frozenset({"schema", "encoding", "logical_bytes", "logical_sha256", "body", "outputs"})


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("receipt storage contains duplicate JSON keys")
        result[key] = value
    return result


def _output_projection(payload: dict[str, object]) -> list[dict[str, object]]:
    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        raise ValueError("receipt outputs must be a list")
    projected: list[dict[str, object]] = []
    for binding in outputs:
        if not isinstance(binding, dict) or not isinstance(binding.get("materialization"), dict):
            raise ValueError("receipt output lacks a materialization")
        materialization_id = binding["materialization"].get("materialization_id")
        if not isinstance(materialization_id, str):
            raise ValueError("receipt output lacks its materialization identity")
        projected.append({
            "materialization": {"materialization_id": materialization_id},
            "fingerprint": binding.get("fingerprint"),
            "fingerprint_algorithm": binding.get("fingerprint_algorithm"),
        })
    return projected


def encode_receipt_storage(logical_json: str) -> str:
    """Encode an already validated canonical WorkReceipt without losing bytes."""
    raw = logical_json.encode("utf-8")
    if len(raw) > MAX_WORK_RECEIPT_JSON_BYTES:
        raise ValueError("logical receipt exceeds its byte bound")
    payload = json.loads(logical_json, object_pairs_hook=_object_pairs)
    if not isinstance(payload, dict) or payload.get("kind") != "work_receipt":
        raise ValueError("receipt storage requires a logical WorkReceipt")
    encoded = canonical_json({
        "schema": RECEIPT_STORAGE_SCHEMA,
        "encoding": "zlib-base64",
        "logical_bytes": len(raw),
        "logical_sha256": hashlib.sha256(raw).hexdigest(),
        "body": base64.b64encode(zlib.compress(raw, level=6)).decode("ascii"),
        "outputs": _output_projection(payload),
    })
    # Small/nonredundant receipts keep their original v1 storage unchanged.
    return encoded if len(encoded.encode("utf-8")) < len(raw) else logical_json


def decode_receipt_storage(stored_json: str, *, owner_schema_version: int | None = None) -> str:
    """Recover exact v1 bytes and authenticate the SQLite lookup projection."""
    if len(stored_json.encode("utf-8")) > MAX_WORK_RECEIPT_JSON_BYTES:
        raise ValueError("stored receipt exceeds its byte bound")
    payload = json.loads(stored_json, object_pairs_hook=_object_pairs)
    if not isinstance(payload, dict):
        raise ValueError("receipt storage must be a JSON object")
    if payload.get("schema") != RECEIPT_STORAGE_SCHEMA:
        return stored_json  # WorkReceipt.from_json performs legacy validation.
    if owner_schema_version is not None and owner_schema_version < 11:
        raise ValueError("compact receipt storage requires semantic schema 11")
    if set(payload) != _STORAGE_FIELDS or payload["encoding"] != "zlib-base64":
        raise ValueError("unsupported or noncanonical receipt storage envelope")
    size = payload["logical_bytes"]
    body = payload["body"]
    if type(size) is not int or not 0 < size <= MAX_WORK_RECEIPT_JSON_BYTES or not isinstance(body, str):
        raise ValueError("invalid logical receipt size or compressed body")
    try:
        compressed = base64.b64decode(body, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, size + 1)
    except (binascii.Error, zlib.error) as exc:
        raise ValueError("malformed compressed receipt") from exc
    if (len(raw) != size or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data
            or hashlib.sha256(raw).hexdigest() != payload["logical_sha256"]):
        raise ValueError("receipt body size, stream or digest mismatch")
    logical = raw.decode("utf-8")
    original = json.loads(logical, object_pairs_hook=_object_pairs)
    if not isinstance(original, dict) or _output_projection(original) != payload["outputs"]:
        raise ValueError("receipt output projection does not match logical evidence")
    if canonical_json(payload) != stored_json:
        raise ValueError("receipt storage envelope is not canonical")
    return logical


def receipt_storage_logical_bytes(stored_json: str) -> int:
    """Conservative hydration cost before selecting an outbox page.

    A corrupt/unrecognized envelope cannot earn a smaller page charge. Full
    validation occurs only for selected rows, preserving bounded-page semantics.
    """
    length = len(stored_json.encode("utf-8"))
    if length > MAX_WORK_RECEIPT_JSON_BYTES:
        return max(length, MAX_WORK_RECEIPT_JSON_BYTES)
    try:
        payload = json.loads(stored_json, object_pairs_hook=_object_pairs)
    except (ValueError, TypeError):
        return MAX_WORK_RECEIPT_JSON_BYTES
    if isinstance(payload, dict) and payload.get("schema") == RECEIPT_STORAGE_SCHEMA:
        size = payload.get("logical_bytes")
        if type(size) is int and 0 < size <= MAX_WORK_RECEIPT_JSON_BYTES:
            return max(size, length)
        return MAX_WORK_RECEIPT_JSON_BYTES
    return length
