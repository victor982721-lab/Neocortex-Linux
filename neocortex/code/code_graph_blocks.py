"""Immutable, directly referenced blocks for the Code generation ledger.

The v1 tables remain historical data. Every v2 snapshot and generation owns a
complete manifest; no read follows a chain of prior generations. A block's
digest selects reuse candidates, while the publisher compares the actual rows
before sharing one. Original producer locators survive payload collection.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator


GRAPH_BLOCK_TABLES = (
    "graph_input_blocks",
    "graph_input_block_items",
    "graph_snapshot_blocks",
    "graph_member_blocks",
    "graph_member_block_items",
    "graph_batch_blocks",
)

GRAPH_BLOCK_DDL = (
    """CREATE TABLE graph_input_blocks(
        block_id TEXT PRIMARY KEY,
        block_digest TEXT NOT NULL CHECK(length(block_digest)=64),
        item_count INTEGER NOT NULL CHECK(item_count>0),
        source_snapshot_id TEXT NOT NULL,
        FOREIGN KEY(source_snapshot_id) REFERENCES graph_input_snapshots(snapshot_id)
            ON DELETE RESTRICT
    ) WITHOUT ROWID""",
    "CREATE INDEX graph_input_blocks_digest_idx ON graph_input_blocks(block_digest,block_id)",
    """CREATE TABLE graph_input_block_items(
        block_id TEXT NOT NULL,
        input_key TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        source_version_id INTEGER,
        observed_path TEXT,
        metadata_json TEXT NOT NULL,
        PRIMARY KEY(block_id,input_key),
        FOREIGN KEY(block_id) REFERENCES graph_input_blocks(block_id) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE graph_snapshot_blocks(
        snapshot_id TEXT NOT NULL,
        block_index INTEGER NOT NULL CHECK(block_index>=0),
        block_id TEXT NOT NULL,
        PRIMARY KEY(snapshot_id,block_index),
        UNIQUE(snapshot_id,block_id),
        FOREIGN KEY(snapshot_id) REFERENCES graph_input_snapshots(snapshot_id) ON DELETE CASCADE,
        FOREIGN KEY(block_id) REFERENCES graph_input_blocks(block_id) ON DELETE RESTRICT
    ) WITHOUT ROWID""",
    "CREATE INDEX graph_snapshot_blocks_block_idx ON graph_snapshot_blocks(block_id,snapshot_id)",
    """CREATE TABLE graph_member_blocks(
        block_id TEXT PRIMARY KEY,
        block_digest TEXT NOT NULL CHECK(length(block_digest)=64),
        item_count INTEGER NOT NULL CHECK(item_count>0),
        source_generation_id TEXT NOT NULL,
        FOREIGN KEY(source_generation_id) REFERENCES graph_generations(generation_id)
            ON DELETE RESTRICT
    ) WITHOUT ROWID""",
    "CREATE INDEX graph_member_blocks_digest_idx ON graph_member_blocks(block_digest,block_id)",
    """CREATE TABLE graph_member_block_items(
        block_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        item_digest TEXT NOT NULL,
        source_version_id INTEGER,
        metadata_json TEXT NOT NULL,
        PRIMARY KEY(block_id,item_key),
        FOREIGN KEY(block_id) REFERENCES graph_member_blocks(block_id) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE graph_batch_blocks(
        generation_id TEXT NOT NULL,
        batch_index INTEGER NOT NULL CHECK(batch_index>=0),
        block_id TEXT NOT NULL,
        PRIMARY KEY(generation_id,batch_index),
        UNIQUE(generation_id,block_id),
        FOREIGN KEY(generation_id,batch_index)
            REFERENCES graph_batches(generation_id,batch_index) ON DELETE CASCADE,
        FOREIGN KEY(block_id) REFERENCES graph_member_blocks(block_id) ON DELETE RESTRICT
    ) WITHOUT ROWID""",
    "CREATE INDEX graph_batch_blocks_block_idx ON graph_batch_blocks(block_id,generation_id)",
)

# Keep the two bindings explicit so each branch can use its owner-key index.
INPUT_ROWS_SQL = """
SELECT input_key,content_digest,source_version_id,observed_path,metadata_json
FROM graph_snapshot_inputs WHERE snapshot_id=?
UNION ALL
SELECT i.input_key,i.content_digest,i.source_version_id,i.observed_path,i.metadata_json
FROM graph_snapshot_blocks r JOIN graph_input_block_items i ON i.block_id=r.block_id
WHERE r.snapshot_id=?"""

MEMBER_ROWS_SQL = """
SELECT batch_index,item_key,item_digest,source_version_id,metadata_json
FROM graph_memberships WHERE generation_id=?
UNION ALL
SELECT r.batch_index,i.item_key,i.item_digest,i.source_version_id,i.metadata_json
FROM graph_batch_blocks r JOIN graph_member_block_items i ON i.block_id=r.block_id
WHERE r.generation_id=?"""


def partition_keys(
    keys: Iterable[str],
    maximum_items: int,
    cancellation_check: Callable[[], None] | None = None,
) -> Iterator[tuple[str, ...]]:
    """Partition by key hash, splitting only an overfull binary-trie leaf.

    Changing a value leaves every partition boundary unchanged. Inserting or
    deleting a key splits or merges only its ancestor leaf, instead of shifting
    all later fixed-offset batches. Payloads and source contents are not held
    here: only keys and their 32-byte hashes. Capture remains O(N), and sorting
    these keys remains O(N log N); sharing reduces durable payload writes.
    """

    keyed: list[tuple[bytes, str]] = []
    for key in keys:
        if cancellation_check is not None:
            cancellation_check()
        keyed.append((hashlib.sha256(key.encode("utf-8")).digest(), key))
    keyed.sort()
    pending = [(0, len(keyed), 0)]
    while pending:
        if cancellation_check is not None:
            cancellation_check()
        start, stop, bit = pending.pop()
        if stop == start:
            continue
        if stop - start <= maximum_items:
            yield tuple(sorted(key for _, key in keyed[start:stop]))
            continue
        if bit >= 256:
            raise ValueError("Code block partition key hashes collide")
        byte_index, bit_index = divmod(bit, 8)
        mask = 1 << (7 - bit_index)
        split = start
        while split < stop and not keyed[split][0][byte_index] & mask:
            split += 1
        pending.append((split, stop, bit + 1))
        pending.append((start, split, bit + 1))
