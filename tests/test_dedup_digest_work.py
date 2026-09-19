"""Exact plan digests avoid JSON decoding for ordinary identity values."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from neocortex.deduplication.inventory import generation


def _legacy_stable(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not isinstance(decoded, dict):
        return value
    decoded.pop("fingerprint_source", None)
    return json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("value", (
    None, 0, 1.5, b"bytes", "", "/fixture/é/duplicate", "keep", "full_hash",
    "abcd0123456789", "0", "true", "null", "NaN", "[1,2,3]", '"string"',
    "{}", ' \r\n\t{"fingerprint_source":"cached","evidence":"é"} \n',
    '{"fingerprint_source":"computed","nested":{"fingerprint_source":"cached"}}',
    '{"key":1,"key":2,"fingerprint_source":"computed"}', '{"broken":',
    '\ufeff{"fingerprint_source":"cached"}', ' \v{"fingerprint_source":"cached"}',
    '\t{"text":"\\ud800"}',
))
def test_proof_normalization_preserves_every_sql_value(value: object) -> None:
    assert generation._stable_proof_json(value) == _legacy_stable(value)


def _seed_groups(connection: sqlite3.Connection, count: int) -> None:
    connection.executescript(
        "CREATE TABLE planned_duplicate_groups("
        "group_id INTEGER PRIMARY KEY,scan_id INTEGER,size INTEGER,keep_path TEXT,"
        "redundant_count INTEGER,reclaimable_bytes INTEGER,full_fingerprint TEXT,"
        "verification_mode TEXT,proof_json TEXT);"
        "CREATE TABLE planned_duplicate_members("
        "group_id INTEGER,member_order INTEGER,role TEXT,path TEXT,volume_id BLOB,"
        "file_id BLOB,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER,proof_json TEXT,"
        "PRIMARY KEY(group_id,member_order)) WITHOUT ROWID;"
    )
    connection.executemany(
        "INSERT INTO planned_duplicate_groups VALUES(?,7,4,?,1,4,?,'full_hash',?)",
        ((number, f"/fixture/{number}/a", "ab" * 16, '{"method":"exact"}') for number in range(count)),
    )
    connection.executemany(
        "INSERT INTO planned_duplicate_members VALUES(?,?,?,?,?,?,4,1,-1,?)",
        (
            (number, order, "keep" if order == 0 else "redundant",
             f"/fixture/{number}/{name}", b"\1" * 16, (number * 2 + order).to_bytes(16, "little"),
             '{"fingerprint_source":"computed","evidence":"equal"}')
            for number in range(count) for order, name in enumerate(("a", "b"))
        ),
    )


def _legacy_digest(connection: sqlite3.Connection) -> bytes:
    digest = hashlib.sha256(b"NEOCORTEX_DUPLICATE_PLAN_V1\0")

    def add_row(marker: bytes, row) -> None:
        digest.update(marker)
        for value in row:
            value = _legacy_stable(value)
            if value is None:
                digest.update(b"N\0")
            else:
                binary = isinstance(value, bytes)
                payload = value if isinstance(value, bytes) else str(value).encode("utf-8", "surrogatepass")
                digest.update(b"B" if binary else b"T")
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)

    for group in connection.execute(
        "SELECT group_id,size,keep_path,redundant_count,reclaimable_bytes,"
        "full_fingerprint,verification_mode,proof_json FROM planned_duplicate_groups "
        "WHERE scan_id=7 ORDER BY size,keep_path,redundant_count,reclaimable_bytes,"
        "full_fingerprint,verification_mode,proof_json",
    ):
        add_row(b"G", group[1:])
        for member in connection.execute(
            "SELECT member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns,"
            "proof_json FROM planned_duplicate_members WHERE group_id=? ORDER BY member_order",
            (group[0],),
        ):
            add_row(b"M", member)
    return digest.digest()


def test_plan_digest_only_parses_possible_objects_and_keeps_exact_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        _seed_groups(connection, 160)
        expected = _legacy_digest(connection)
        parsed = []
        original_loads = json.loads

        def measured_loads(value, *args, **kwargs):
            parsed.append(value)
            return original_loads(value, *args, **kwargs)

        monkeypatch.setattr(generation.json, "loads", measured_loads)
        assert generation.duplicate_plan_digest(connection, 7) == expected
        assert len(parsed) == 160 * 3
        assert all(value.lstrip(" \t\r\n").startswith("{") for value in parsed)
        # Compute/cache provenance is still normalized, without changing any
        # member order, role, identity, timestamp, or framing byte.
        connection.execute(
            "UPDATE planned_duplicate_members SET proof_json=replace(proof_json,'computed','cached')",
        )
        assert generation.duplicate_plan_digest(connection, 7) == expected
        connection.execute("UPDATE planned_duplicate_members SET mtime_ns=2 WHERE group_id=3 AND member_order=1")
        assert generation.duplicate_plan_digest(connection, 7) != expected
