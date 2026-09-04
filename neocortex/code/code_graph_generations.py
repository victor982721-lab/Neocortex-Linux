"""Additive generation contract for the Code graph publication boundary.

The store uses the existing Code connection.  Legacy readers continue to
query the established file/symbol/project tables and never observe a
``building`` generation; a caller must finish the batches and then advance a
named head with compare-and-swap before treating the graph as current.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


GENERATION_SCHEMA_VERSION = 1
DEFAULT_HEAD_NAME = "default"
_MAX_TEXT = 256
_MAX_JSON_BYTES = 256 * 1024
_MAX_CURSOR_BYTES = 16 * 1024
_TABLES = frozenset(
    {
        "graph_generation_metadata",
        "graph_generation_migrations",
        "graph_input_snapshots",
        "graph_snapshot_inputs",
        "graph_generations",
        "graph_batches",
        "graph_memberships",
        "graph_checkpoints",
        "graph_heads",
    }
)


class GenerationError(RuntimeError):
    """Base class for generation contract failures."""


class GenerationConflict(GenerationError):
    """An idempotent replay or publication precondition differs."""


class GenerationHeadConflict(GenerationConflict):
    """The requested head revision is stale."""


class GenerationSchemaError(GenerationError):
    """The additive graph tables are missing or incompatible."""


class GenerationStateError(GenerationError):
    """A lifecycle transition is not valid for the observed state."""


@dataclass(frozen=True, slots=True)
class CodeInput:
    """One immutable source observation captured by an input snapshot."""

    key: str
    content_digest: str
    source_version_id: int | None = None
    observed_path: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    snapshot_id: str
    source_run_id: int
    input_digest: str
    input_count: int
    status: str
    created_ns: int
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class GraphMembership:
    item_key: str
    item_digest: str
    source_version_id: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerationBatch:
    generation_id: str
    batch_index: int
    batch_digest: str
    item_count: int
    cursor: str | None
    status: str
    created_ns: int
    committed_ns: int | None


@dataclass(frozen=True, slots=True)
class GenerationCheckpoint:
    generation_id: str
    checkpoint_index: int
    batch_index: int
    cursor: str
    checkpoint_digest: str
    created_ns: int


@dataclass(frozen=True, slots=True)
class GraphGeneration:
    generation_id: str
    snapshot_id: str
    generation_digest: str | None
    status: str
    created_ns: int
    completed_ns: int | None
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class GenerationHead:
    head_name: str
    generation_id: str
    generation_digest: str
    revision: int
    updated_ns: int


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT:
        raise ValueError(f"{name} must be a non-empty bounded string")
    if any(ord(character) < 0x20 for character in value):
        raise ValueError(f"{name} cannot contain control characters")
    return value


def _digest(value: str, name: str) -> str:
    value = _text(value, name)
    if any(character.isspace() for character in value):
        raise ValueError(f"{name} cannot contain whitespace")
    return value


def _index(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _row_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GenerationSchemaError(f"{name} is not an integer")
    return value


def _json(value: object, name: str) -> str:
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serializable") from exc
    if len(result.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{name} exceeds the metadata limit")
    return result


def _cursor(value: str | None, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError("cursor is required")
        return None
    value = _text(value, "cursor")
    if len(value.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise ValueError("cursor exceeds the checkpoint limit")
    return value


def _hash(value: object, name: str) -> str:
    return hashlib.sha256(_json(value, name).encode("utf-8")).hexdigest()


def _now(value: int | None, name: str) -> int:
    result = time.time_ns() if value is None else value
    if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _input(item: CodeInput) -> CodeInput:
    if not isinstance(item, CodeInput):
        raise TypeError("snapshot inputs must be CodeInput records")
    return CodeInput(
        _text(item.key, "input key"),
        _digest(item.content_digest, "content digest"),
        None
        if item.source_version_id is None
        else _index(item.source_version_id, "source_version_id"),
        None if item.observed_path is None else _text(item.observed_path, "observed_path"),
        dict(item.metadata),
    )


def _member(item: GraphMembership) -> GraphMembership:
    if not isinstance(item, GraphMembership):
        raise TypeError("graph batches must contain GraphMembership records")
    return GraphMembership(
        _text(item.item_key, "item key"),
        _digest(item.item_digest, "item digest"),
        None
        if item.source_version_id is None
        else _index(item.source_version_id, "source_version_id"),
        dict(item.metadata),
    )


def _input_payload(items: Sequence[CodeInput]) -> list[dict[str, object]]:
    return [
        {
            "key": item.key,
            "content_digest": item.content_digest,
            "source_version_id": item.source_version_id,
            "observed_path": item.observed_path,
            "metadata": dict(item.metadata),
        }
        for item in items
    ]


def _member_payload(items: Sequence[GraphMembership]) -> list[dict[str, object]]:
    return [
        {
            "item_key": item.item_key,
            "item_digest": item.item_digest,
            "source_version_id": item.source_version_id,
            "metadata": dict(item.metadata),
        }
        for item in items
    ]


class CodeGraphGenerationStore:
    """Generation API bound to the existing :class:`CodeState` connection."""

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("graph generation store requires a sqlite3 connection")
        self._connection = connection
        self._validate_schema()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def _validate_schema(self) -> None:
        observed = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing = _TABLES - observed
        if missing:
            raise GenerationSchemaError(
                "Code graph generation schema is missing: " + ",".join(sorted(missing))
            )
        row = self._connection.execute(
            "SELECT value FROM graph_generation_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None or str(row[0]) != str(GENERATION_SCHEMA_VERSION):
            raise GenerationSchemaError("Code graph generation schema version is unsupported")
        versions = tuple(
            int(item[0])
            for item in self._connection.execute(
                "SELECT version FROM graph_generation_migrations ORDER BY version"
            )
        )
        if versions != (GENERATION_SCHEMA_VERSION,):
            raise GenerationSchemaError("Code graph generation migration history is incomplete")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection
        savepoint = "neocortex_graph_generation"
        nested = connection.in_transaction
        try:
            if nested:
                connection.execute(f"SAVEPOINT {savepoint}")
            else:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
        except BaseException:
            if nested:
                connection.execute(f"ROLLBACK TO {savepoint}")
                connection.execute(f"RELEASE {savepoint}")
            else:
                connection.rollback()
            raise
        else:
            if nested:
                connection.execute(f"RELEASE {savepoint}")
            else:
                connection.commit()

    @staticmethod
    def _snapshot(row: Sequence[object], snapshot_id: str) -> InputSnapshot:
        try:
            metadata = json.loads(str(row[5]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GenerationSchemaError(f"malformed snapshot metadata: {snapshot_id}") from exc
        if not isinstance(metadata, dict):
            raise GenerationSchemaError(f"snapshot metadata is not an object: {snapshot_id}")
        return InputSnapshot(
            snapshot_id,
            _row_int(row[0], "source_run_id"),
            str(row[1]),
            _row_int(row[2], "input_count"),
            str(row[3]),
            _row_int(row[4], "created_ns"),
            metadata,
        )

    @staticmethod
    def _generation(row: Sequence[object], generation_id: str) -> GraphGeneration:
        try:
            metadata = json.loads(str(row[5]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GenerationSchemaError(f"malformed generation metadata: {generation_id}") from exc
        if not isinstance(metadata, dict):
            raise GenerationSchemaError(f"generation metadata is not an object: {generation_id}")
        return GraphGeneration(
            generation_id,
            str(row[0]),
            None if row[1] is None else str(row[1]),
            str(row[2]),
            _row_int(row[3], "created_ns"),
            None if row[4] is None else _row_int(row[4], "completed_ns"),
            metadata,
        )

    def create_input_snapshot(
        self,
        snapshot_id: str,
        source_run_id: int,
        inputs: Iterable[CodeInput],
        *,
        metadata: Mapping[str, object] | None = None,
        created_ns: int | None = None,
    ) -> InputSnapshot:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        source_run_id = _index(source_run_id, "source_run_id")
        items = tuple(sorted((_input(item) for item in inputs), key=lambda item: item.key))
        if len({item.key for item in items}) != len(items):
            raise ValueError("snapshot input keys must be unique")
        input_digest = _hash(_input_payload(items), "snapshot inputs")
        metadata_json = _json(dict(metadata or {}), "snapshot metadata")
        created = _now(created_ns, "created_ns")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT source_run_id,input_digest,input_count,status,created_ns,metadata_json "
                "FROM graph_input_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row is not None:
                if (int(row[0]), str(row[1]), int(row[2]), str(row[5])) != (
                    source_run_id,
                    input_digest,
                    len(items),
                    metadata_json,
                ):
                    raise GenerationConflict(f"input snapshot differs: {snapshot_id}")
                return self._snapshot(row, snapshot_id)
            connection.execute(
                "INSERT INTO graph_input_snapshots(snapshot_id,source_run_id,input_digest,input_count,status,created_ns,metadata_json) "
                "VALUES(?,?,?,?,'sealed',?,?)",
                (snapshot_id, source_run_id, input_digest, len(items), created, metadata_json),
            )
            connection.executemany(
                "INSERT INTO graph_snapshot_inputs(snapshot_id,input_key,content_digest,source_version_id,observed_path,metadata_json) VALUES(?,?,?,?,?,?)",
                (
                    (
                        snapshot_id,
                        item.key,
                        item.content_digest,
                        item.source_version_id,
                        item.observed_path,
                        _json(dict(item.metadata), "input metadata"),
                    )
                    for item in items
                ),
            )
            return InputSnapshot(
                snapshot_id,
                source_run_id,
                input_digest,
                len(items),
                "sealed",
                created,
                json.loads(metadata_json),
            )

    def get_input_snapshot(self, snapshot_id: str) -> InputSnapshot | None:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        row = self._connection.execute(
            "SELECT source_run_id,input_digest,input_count,status,created_ns,metadata_json FROM graph_input_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        return None if row is None else self._snapshot(row, snapshot_id)

    def get_input_items(self, snapshot_id: str) -> tuple[CodeInput, ...]:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        result: list[CodeInput] = []
        for row in self._connection.execute(
            "SELECT input_key,content_digest,source_version_id,observed_path,metadata_json FROM graph_snapshot_inputs WHERE snapshot_id=? ORDER BY input_key",
            (snapshot_id,),
        ):
            try:
                metadata = json.loads(str(row[4]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise GenerationSchemaError("malformed input metadata") from exc
            if not isinstance(metadata, dict):
                raise GenerationSchemaError("input metadata is not an object")
            result.append(CodeInput(str(row[0]), str(row[1]), row[2], row[3], metadata))
        return tuple(result)

    def start_generation(
        self,
        snapshot_id: str,
        generation_id: str,
        *,
        metadata: Mapping[str, object] | None = None,
        created_ns: int | None = None,
    ) -> GraphGeneration:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        generation_id = _text(generation_id, "generation_id")
        metadata_json = _json(dict(metadata or {}), "generation metadata")
        created = _now(created_ns, "created_ns")
        with self._transaction() as connection:
            snapshot = connection.execute(
                "SELECT status FROM graph_input_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if snapshot is None or str(snapshot[0]) != "sealed":
                raise GenerationStateError(f"input snapshot is not sealed: {snapshot_id}")
            row = connection.execute(
                "SELECT snapshot_id,generation_digest,status,created_ns,completed_ns,metadata_json FROM graph_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if row is not None:
                if str(row[0]) != snapshot_id or str(row[5]) != metadata_json:
                    raise GenerationConflict(f"generation differs: {generation_id}")
                return self._generation(row, generation_id)
            connection.execute(
                "INSERT INTO graph_generations(generation_id,snapshot_id,generation_digest,status,created_ns,metadata_json) VALUES(?,?,NULL,'building',?,?)",
                (generation_id, snapshot_id, created, metadata_json),
            )
            return GraphGeneration(
                generation_id,
                snapshot_id,
                None,
                "building",
                created,
                None,
                json.loads(metadata_json),
            )

    def get_generation(self, generation_id: str) -> GraphGeneration | None:
        generation_id = _text(generation_id, "generation_id")
        row = self._connection.execute(
            "SELECT snapshot_id,generation_digest,status,created_ns,completed_ns,metadata_json FROM graph_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        return None if row is None else self._generation(row, generation_id)

    def append_batch(
        self,
        generation_id: str,
        batch_index: int,
        members: Iterable[GraphMembership],
        *,
        cursor: str | None = None,
        created_ns: int | None = None,
    ) -> GenerationBatch:
        generation_id = _text(generation_id, "generation_id")
        batch_index = _index(batch_index, "batch_index")
        cursor = _cursor(cursor)
        items = tuple(sorted((_member(item) for item in members), key=lambda item: item.item_key))
        if len({item.item_key for item in items}) != len(items):
            raise ValueError("graph membership keys must be unique")
        batch_digest = _hash(_member_payload(items), "graph batch")
        created = _now(created_ns, "created_ns")
        with self._transaction() as connection:
            generation = connection.execute(
                "SELECT status FROM graph_generations WHERE generation_id=?", (generation_id,)
            ).fetchone()
            if generation is None:
                raise GenerationStateError(f"generation does not exist: {generation_id}")
            row = connection.execute(
                "SELECT batch_digest,item_count,cursor,status,created_ns,committed_ns FROM graph_batches WHERE generation_id=? AND batch_index=?",
                (generation_id, batch_index),
            ).fetchone()
            if row is not None:
                if (str(row[0]), int(row[1]), row[2]) != (batch_digest, len(items), cursor):
                    raise GenerationConflict(f"batch differs: {generation_id}/{batch_index}")
                return GenerationBatch(
                    generation_id,
                    batch_index,
                    str(row[0]),
                    int(row[1]),
                    row[2],
                    str(row[3]),
                    int(row[4]),
                    row[5],
                )
            if str(generation[0]) != "building":
                raise GenerationStateError(f"generation is not building: {generation_id}")
            latest = connection.execute(
                "SELECT MAX(batch_index) FROM graph_batches WHERE generation_id=?", (generation_id,)
            ).fetchone()[0]
            expected = 0 if latest is None else int(latest) + 1
            if batch_index != expected:
                raise GenerationStateError(
                    f"batch index must be contiguous; expected {expected}, got {batch_index}"
                )
            existing_keys = {
                str(item[0])
                for item in connection.execute(
                    "SELECT item_key FROM graph_memberships WHERE generation_id=?", (generation_id,)
                )
            }
            duplicates = existing_keys.intersection(item.item_key for item in items)
            if duplicates:
                raise GenerationConflict(
                    f"membership already exists: {generation_id}/{min(duplicates)}"
                )
            committed = time.time_ns()
            connection.execute(
                "INSERT INTO graph_batches(generation_id,batch_index,batch_digest,item_count,cursor,status,created_ns,committed_ns) VALUES(?,?,?,?,?,'committed',?,?)",
                (generation_id, batch_index, batch_digest, len(items), cursor, created, committed),
            )
            connection.executemany(
                "INSERT INTO graph_memberships(generation_id,batch_index,item_key,item_digest,source_version_id,metadata_json) VALUES(?,?,?,?,?,?)",
                (
                    (
                        generation_id,
                        batch_index,
                        item.item_key,
                        item.item_digest,
                        item.source_version_id,
                        _json(dict(item.metadata), "membership metadata"),
                    )
                    for item in items
                ),
            )
            return GenerationBatch(
                generation_id,
                batch_index,
                batch_digest,
                len(items),
                cursor,
                "committed",
                created,
                committed,
            )

    def checkpoint(
        self,
        generation_id: str,
        batch_index: int,
        cursor: str,
        *,
        checkpoint_index: int | None = None,
        checkpoint_digest: str | None = None,
        created_ns: int | None = None,
    ) -> GenerationCheckpoint:
        generation_id = _text(generation_id, "generation_id")
        batch_index = _index(batch_index, "batch_index")
        checked_cursor = _cursor(cursor, required=True)
        assert checked_cursor is not None
        cursor = checked_cursor
        checkpoint_digest = _digest(
            checkpoint_digest
            or _hash({"batch_index": batch_index, "cursor": cursor}, "checkpoint"),
            "checkpoint_digest",
        )
        created = _now(created_ns, "created_ns")
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT status FROM graph_generations WHERE generation_id=?", (generation_id,)
            ).fetchone()
            if state is None or str(state[0]) != "building":
                raise GenerationStateError(f"generation is not building: {generation_id}")
            batch = connection.execute(
                "SELECT status FROM graph_batches WHERE generation_id=? AND batch_index=?",
                (generation_id, batch_index),
            ).fetchone()
            if batch is None or str(batch[0]) != "committed":
                raise GenerationStateError(f"batch is not committed: {generation_id}/{batch_index}")
            if checkpoint_index is None:
                checkpoint_index = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(checkpoint_index)+1,0) FROM graph_checkpoints WHERE generation_id=?",
                        (generation_id,),
                    ).fetchone()[0]
                )
            checkpoint_index = _index(checkpoint_index, "checkpoint_index")
            prior = connection.execute(
                "SELECT checkpoint_index,batch_index FROM graph_checkpoints WHERE generation_id=? ORDER BY checkpoint_index DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            if prior is not None and (
                checkpoint_index < int(prior[0]) or batch_index < int(prior[1])
            ):
                raise GenerationStateError("checkpoints must be monotonic")
            row = connection.execute(
                "SELECT batch_index,cursor,checkpoint_digest,created_ns FROM graph_checkpoints WHERE generation_id=? AND checkpoint_index=?",
                (generation_id, checkpoint_index),
            ).fetchone()
            if row is not None:
                if (int(row[0]), str(row[1]), str(row[2])) != (
                    batch_index,
                    cursor,
                    checkpoint_digest,
                ):
                    raise GenerationConflict(
                        f"checkpoint differs: {generation_id}/{checkpoint_index}"
                    )
                return GenerationCheckpoint(
                    generation_id,
                    checkpoint_index,
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                    int(row[3]),
                )
            connection.execute(
                "INSERT INTO graph_checkpoints(generation_id,checkpoint_index,batch_index,cursor,checkpoint_digest,created_ns) VALUES(?,?,?,?,?,?)",
                (generation_id, checkpoint_index, batch_index, cursor, checkpoint_digest, created),
            )
            return GenerationCheckpoint(
                generation_id, checkpoint_index, batch_index, cursor, checkpoint_digest, created
            )

    def complete_generation(
        self, generation_id: str, *, completed_ns: int | None = None
    ) -> GraphGeneration:
        generation_id = _text(generation_id, "generation_id")
        completed = _now(completed_ns, "completed_ns")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT snapshot_id,generation_digest,status,created_ns,completed_ns,metadata_json FROM graph_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if row is None:
                raise GenerationStateError(f"generation does not exist: {generation_id}")
            if str(row[2]) in {"completed", "published"}:
                return self._generation(row, generation_id)
            if str(row[2]) != "building":
                raise GenerationStateError(f"generation cannot complete: {generation_id}")
            batches = connection.execute(
                "SELECT batch_index,batch_digest,item_count FROM graph_batches WHERE generation_id=? AND status='committed' ORDER BY batch_index",
                (generation_id,),
            ).fetchall()
            if any(int(item[0]) != index for index, item in enumerate(batches)):
                raise GenerationStateError("generation batches are not contiguous")
            if batches:
                checkpoint = connection.execute(
                    "SELECT batch_index FROM graph_checkpoints WHERE generation_id=? ORDER BY checkpoint_index DESC LIMIT 1",
                    (generation_id,),
                ).fetchone()
                if checkpoint is None or int(checkpoint[0]) < int(batches[-1][0]):
                    raise GenerationStateError(
                        "generation requires a checkpoint for its final batch"
                    )
            # Validate the materialized members against every committed batch
            # before publishing a generation digest.  A deleted or modified
            # membership must never be silently accepted as a complete graph.
            for batch_index, batch_digest, item_count in batches:
                member_rows = connection.execute(
                    "SELECT item_key,item_digest,source_version_id,metadata_json "
                    "FROM graph_memberships WHERE generation_id=? AND batch_index=? "
                    "ORDER BY item_key",
                    (generation_id, int(batch_index)),
                ).fetchall()
                if len(member_rows) != int(item_count):
                    raise GenerationSchemaError(
                        f"generation batch membership count differs: {generation_id}/{batch_index}"
                    )
                members: list[GraphMembership] = []
                for member_row in member_rows:
                    try:
                        metadata = json.loads(str(member_row[3]))
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise GenerationSchemaError(
                            f"malformed membership metadata: {generation_id}/{batch_index}"
                        ) from exc
                    if not isinstance(metadata, dict):
                        raise GenerationSchemaError(
                            f"membership metadata is not an object: {generation_id}/{batch_index}"
                        )
                    members.append(
                        GraphMembership(
                            str(member_row[0]),
                            str(member_row[1]),
                            None if member_row[2] is None else int(member_row[2]),
                            metadata,
                        )
                    )
                if _hash(_member_payload(tuple(members)), "graph batch") != str(batch_digest):
                    raise GenerationSchemaError(
                        f"generation batch digest differs: {generation_id}/{batch_index}"
                    )
            snapshot = connection.execute(
                "SELECT input_digest FROM graph_input_snapshots WHERE snapshot_id=?", (str(row[0]),)
            ).fetchone()
            if snapshot is None:
                raise GenerationSchemaError("generation references a missing snapshot")
            digest = _hash(
                {
                    "snapshot_digest": str(snapshot[0]),
                    "batches": [
                        {
                            "batch_index": int(item[0]),
                            "batch_digest": str(item[1]),
                            "item_count": int(item[2]),
                        }
                        for item in batches
                    ],
                },
                "generation",
            )
            connection.execute(
                "UPDATE graph_generations SET generation_digest=?,status='completed',completed_ns=? WHERE generation_id=? AND status='building'",
                (digest, completed, generation_id),
            )
            row = connection.execute(
                "SELECT snapshot_id,generation_digest,status,created_ns,completed_ns,metadata_json FROM graph_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            assert row is not None
            return self._generation(row, generation_id)

    def abort_generation(self, generation_id: str) -> GraphGeneration:
        generation_id = _text(generation_id, "generation_id")
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE graph_generations SET status='aborted' WHERE generation_id=? AND status='building'",
                (generation_id,),
            ).rowcount
            row = connection.execute(
                "SELECT snapshot_id,generation_digest,status,created_ns,completed_ns,metadata_json FROM graph_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if row is None:
                raise GenerationStateError(f"generation does not exist: {generation_id}")
            if not changed and str(row[2]) != "aborted":
                raise GenerationStateError(f"generation cannot abort: {generation_id}")
            return self._generation(row, generation_id)

    def compare_and_swap_head(
        self,
        head_name: str,
        *,
        expected_revision: int,
        expected_generation_id: str | None,
        generation_id: str,
    ) -> GenerationHead:
        head_name = _text(head_name, "head_name")
        expected_revision = _index(expected_revision, "expected_revision")
        generation_id = _text(generation_id, "generation_id")
        if expected_generation_id is not None:
            expected_generation_id = _text(expected_generation_id, "expected_generation_id")
        with self._transaction() as connection:
            target = connection.execute(
                "SELECT generation_digest,status FROM graph_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if target is None or str(target[1]) not in {"completed", "published"}:
                raise GenerationStateError(f"generation is not complete: {generation_id}")
            current = connection.execute(
                "SELECT generation_id,generation_digest,revision FROM graph_heads WHERE head_name=?",
                (head_name,),
            ).fetchone()
            if current is None:
                if expected_revision != 0 or expected_generation_id is not None:
                    raise GenerationHeadConflict(f"head was absent: {head_name}")
                revision = 1
                updated = time.time_ns()
                connection.execute(
                    "INSERT INTO graph_heads(head_name,generation_id,generation_digest,revision,updated_ns) VALUES(?,?,?,?,?)",
                    (head_name, generation_id, str(target[0]), revision, updated),
                )
            else:
                if (
                    int(current[2]) != expected_revision
                    or str(current[0]) != expected_generation_id
                ):
                    raise GenerationHeadConflict(f"head changed before publish: {head_name}")
                revision = int(current[2]) + 1
                updated = time.time_ns()
                changed = connection.execute(
                    "UPDATE graph_heads SET generation_id=?,generation_digest=?,revision=?,updated_ns=? WHERE head_name=? AND revision=? AND generation_id=?",
                    (
                        generation_id,
                        str(target[0]),
                        revision,
                        updated,
                        head_name,
                        expected_revision,
                        expected_generation_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise GenerationHeadConflict(f"head changed before publish: {head_name}")
            connection.execute(
                "UPDATE graph_generations SET status='published' WHERE generation_id=? AND status='completed'",
                (generation_id,),
            )
            return GenerationHead(head_name, generation_id, str(target[0]), revision, updated)

    def get_head(self, head_name: str = DEFAULT_HEAD_NAME) -> GenerationHead | None:
        head_name = _text(head_name, "head_name")
        row = self._connection.execute(
            "SELECT generation_id,generation_digest,revision,updated_ns FROM graph_heads WHERE head_name=?",
            (head_name,),
        ).fetchone()
        if row is None:
            return None
        return GenerationHead(head_name, str(row[0]), str(row[1]), int(row[2]), int(row[3]))

    def list_memberships(self, generation_id: str) -> tuple[GraphMembership, ...]:
        generation_id = _text(generation_id, "generation_id")
        result: list[GraphMembership] = []
        for row in self._connection.execute(
            "SELECT item_key,item_digest,source_version_id,metadata_json FROM graph_memberships WHERE generation_id=? ORDER BY batch_index,item_key",
            (generation_id,),
        ):
            try:
                metadata = json.loads(str(row[3]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise GenerationSchemaError("malformed membership metadata") from exc
            if not isinstance(metadata, dict):
                raise GenerationSchemaError("membership metadata is not an object")
            result.append(GraphMembership(str(row[0]), str(row[1]), row[2], metadata))
        return tuple(result)


__all__ = [
    "DEFAULT_HEAD_NAME",
    "GENERATION_SCHEMA_VERSION",
    "CodeGraphGenerationStore",
    "CodeInput",
    "GenerationBatch",
    "GenerationCheckpoint",
    "GenerationConflict",
    "GenerationError",
    "GenerationHead",
    "GenerationHeadConflict",
    "GenerationSchemaError",
    "GenerationStateError",
    "GraphGeneration",
    "GraphMembership",
    "InputSnapshot",
]
