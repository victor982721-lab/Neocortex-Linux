"""Strict serialization for persisted duplicate-content evidence."""

from __future__ import annotations

import json
from dataclasses import fields
from typing import cast

from ..domain.errors import InventoryError
from ..domain.evidence import (
    PROOF_VERSION,
    DuplicateGroupProof,
    DuplicateMemberProof,
)


def encode_proof(proof: DuplicateGroupProof | DuplicateMemberProof | None) -> str:
    if proof is None:
        return "{}"
    value = json.dumps(proof.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if isinstance(proof, DuplicateGroupProof):
        decode_group_proof(value)
    else:
        decode_member_proof(value)
    return value


def _payload(raw: str, model: type) -> dict[str, object] | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise InventoryError("duplicate proof is not valid JSON") from error
    if value == {}:
        return None
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(model)}:
        raise InventoryError("duplicate proof has an unsupported shape")
    if value["proof_version"] != PROOF_VERSION:
        raise InventoryError("duplicate proof version is unsupported")
    for field in ("missing_checks", "keeper_factors", "aliases"):
        if field in value:
            sequence = value[field]
            if not isinstance(sequence, list) or any(
                not isinstance(item, str) for item in sequence
            ):
                raise InventoryError(f"duplicate proof {field} is invalid")
            value[field] = tuple(sequence)
    if value["comparison_method"] not in {"full_xxh3", "byte_for_byte"}:
        raise InventoryError("duplicate proof comparison method is invalid")
    if value["comparison_result"] not in {"reference", "fingerprint_match", "equal"}:
        raise InventoryError("duplicate proof comparison result is invalid")
    return value


def decode_group_proof(raw: str) -> DuplicateGroupProof | None:
    value = _payload(raw, DuplicateGroupProof)
    if value is None:
        return None
    if (
        value["requested_policy"] not in {"fast", "exact"}
        or value["scope"] != "physical_files"
        or value["actionability"] != "review_required"
        or not isinstance(value["keeper_reason"], str)
        or not isinstance(value["keeper_policy_version"], str)
    ):
        raise InventoryError("duplicate group proof policy is invalid")
    expected = (
        ("byte_for_byte", "equal")
        if value["requested_policy"] == "exact"
        else ("full_xxh3", "fingerprint_match")
    )
    if (value["comparison_method"], value["comparison_result"]) != expected:
        raise InventoryError("duplicate group proof does not match its requested policy")
    return DuplicateGroupProof(**value)  # type: ignore[arg-type]


def decode_member_proof(raw: str) -> DuplicateMemberProof:
    value = _payload(raw, DuplicateMemberProof)
    if value is None:
        return DuplicateMemberProof()
    identity = value["compared_to_identity"]
    if identity is not None:
        if (
            not isinstance(identity, list)
            or len(identity) != 2
            or any(type(item) is not int or item < 0 for item in identity)
        ):
            raise InventoryError("duplicate proof comparison identity is invalid")
        value["compared_to_identity"] = tuple(identity)
    for field in ("comparison_bytes", "alias_count", "observed_link_count"):
        number = value[field]
        if number is not None and (type(number) is not int or number < 0):
            raise InventoryError(f"duplicate proof {field} is invalid")
    aliases_value = value["aliases"]
    if not isinstance(aliases_value, tuple) or any(
        not isinstance(alias, str) for alias in aliases_value
    ):
        raise InventoryError("duplicate proof aliases are invalid")
    aliases = cast(tuple[str, ...], aliases_value)
    count = value["alias_count"]
    observed_links = value["observed_link_count"]
    if (
        not isinstance(count, int)
        or count < 1
        or count < len(aliases)
        or any(not alias.strip() for alias in aliases)
        or len(set(aliases)) != len(aliases)
        or type(observed_links) is not int
        or observed_links < count
    ):
        raise InventoryError("duplicate proof alias topology is inconsistent")
    if type(value["aliases_truncated"]) is not bool or value["aliases_truncated"] != (
        count > len(aliases)
    ):
        raise InventoryError("duplicate proof alias truncation is inconsistent")
    missing_checks_value = value["missing_checks"]
    if not isinstance(missing_checks_value, tuple) or any(
        not isinstance(check, str) for check in missing_checks_value
    ):
        raise InventoryError("duplicate proof missing checks are invalid")
    missing_checks = cast(tuple[str, ...], missing_checks_value)
    if value["fingerprint_source"] not in {"computed", "cached"} or not isinstance(
        value["fingerprint_algorithm"], str
    ):
        raise InventoryError("duplicate proof fingerprint evidence is invalid")
    result = value["comparison_result"]
    if result == "reference":
        valid = (
            value["comparison_method"] == "full_xxh3"
            and identity is None
            and value["comparison_bytes"] is None
        )
    elif result == "fingerprint_match":
        valid = (
            value["comparison_method"] == "full_xxh3"
            and identity is not None
            and value["comparison_bytes"] is None
            and "byte_for_byte_comparison" in missing_checks
        )
    else:
        valid = (
            value["comparison_method"] == "byte_for_byte"
            and identity is not None
            and type(value["comparison_bytes"]) is int
        )
    if not valid:
        raise InventoryError("duplicate proof comparison receipt is inconsistent")
    return DuplicateMemberProof(**value)  # type: ignore[arg-type]


__all__ = ["decode_group_proof", "decode_member_proof", "encode_proof"]
