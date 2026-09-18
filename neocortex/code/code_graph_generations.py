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
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .code_graph_revision import graph_revision


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


@dataclass(frozen=True, slots=True)
class GraphPublicationResult:
    """Receipt for one graph generation publication.

    The receipt deliberately identifies both the source analysis run and the
    published head.  A generation is useful only when a reader can prove
    which completed producer run supplied it; a digest alone is not enough to
    establish that lineage.
    """

    source_run_id: int
    snapshot_id: str
    generation_id: str
    generation_digest: str
    head_name: str
    head_revision: int
    item_count: int
    reused: bool = False


@dataclass(frozen=True, slots=True)
class GraphObservation:
    """A completed run's observation, preserving the original graph producer."""

    observer_run_id: int
    producer_run_id: int
    snapshot_id: str
    input_digest: str
    generation_id: str
    generation_digest: str
    head_name: str
    head_revision: int
    item_count: int
    graph_revision: int
    resolver_signature: str
    processing_signature: str
    contract: str = "code-graph-observation-v1"

    def publication(self) -> GraphPublicationResult:
        return GraphPublicationResult(
            self.producer_run_id, self.snapshot_id, self.generation_id,
            self.generation_digest, self.head_name, self.head_revision,
            self.item_count, reused=True,
        )


@dataclass(frozen=True, slots=True)
class PublishedGraph:
    """Validated view of a published generation and its immutable members."""

    head: GenerationHead
    generation: GraphGeneration
    memberships: tuple[GraphMembership, ...]


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
    """Hash canonical JSON without applying a per-metadata limit to a graph."""

    digest = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    try:
        for piece in encoder.iterencode(value):
            digest.update(piece.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serializable") from exc
    return digest.hexdigest()


def _hash_input_rows(
    rows: Iterable[sqlite3.Row],
    cancellation_check: Callable[[], None] | None,
) -> str:
    """Stream the exact historical canonical array, ordered by input key."""

    digest = hashlib.sha256(b"[")
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    for index, row in enumerate(rows):
        if cancellation_check is not None:
            cancellation_check()
        if index:
            digest.update(b",")
        # Emit the historical sorted field order. Keep the already canonical
        # metadata bytes: decoding and re-encoding would reorder numeric keys
        # in nested mappings after JSON converts them into strings.
        digest.update(b'{"content_digest":')
        digest.update(encoder.encode(row[1]).encode("utf-8"))
        digest.update(b',"key":')
        digest.update(encoder.encode(row[0]).encode("utf-8"))
        digest.update(b',"metadata":')
        digest.update(str(row[4]).encode("utf-8"))
        digest.update(b',"observed_path":')
        digest.update(encoder.encode(row[3]).encode("utf-8"))
        digest.update(b',"source_version_id":')
        digest.update(encoder.encode(row[2]).encode("utf-8"))
        digest.update(b'}')
    digest.update(b"]")
    return digest.hexdigest()


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

    def validate_source_run_id(
        self,
        source_run_id: int,
        *,
        expected_framework_run_id: int | None = None,
        expected_processing_signature: str | None = None,
    ) -> Mapping[str, object]:
        """Validate the completed Code producer run used by a generation.

        ``source_run_id`` is an analysis-run identity, not an arbitrary cursor
        supplied by a caller.  Keeping this check in the generation boundary
        prevents a graph head from being advanced from a missing, partial or
        unrelated producer run.  The method is intentionally read-only and
        can be called while the producer transaction is open.
        """

        if (
            isinstance(source_run_id, bool)
            or not isinstance(source_run_id, int)
            or source_run_id <= 0
        ):
            raise GenerationStateError("source_run_id must identify a positive analysis run")
        if expected_framework_run_id is not None:
            expected_framework_run_id = _index(
                expected_framework_run_id, "expected_framework_run_id"
            )
        if expected_processing_signature is not None:
            expected_processing_signature = _text(
                expected_processing_signature, "expected_processing_signature"
            )
        row = self._connection.execute(
            """SELECT framework_run_id,processing_signature,status,started_ns,
            completed_ns,summary_json FROM analysis_runs WHERE analysis_run_id=?""",
            (source_run_id,),
        ).fetchone()
        if row is None:
            raise GenerationStateError(f"source analysis run does not exist: {source_run_id}")
        try:
            framework_run_id = _row_int(row[0], "framework_run_id")
            started_ns = _row_int(row[3], "started_ns")
            completed_ns = None if row[4] is None else _row_int(row[4], "completed_ns")
        except GenerationSchemaError:
            raise
        status = str(row[2])
        if status != "completed":
            raise GenerationStateError(
                f"source analysis run is not completed: {source_run_id} ({status})"
            )
        if started_ns <= 0 or completed_ns is None or completed_ns <= 0:
            raise GenerationSchemaError(f"source analysis run timestamps are invalid: {source_run_id}")
        processing_signature = str(row[1])
        if expected_framework_run_id is not None and framework_run_id != expected_framework_run_id:
            raise GenerationConflict(f"source framework run differs: {source_run_id}")
        if (
            expected_processing_signature is not None
            and processing_signature != expected_processing_signature
        ):
            raise GenerationConflict(f"source processing signature differs: {source_run_id}")
        if row[5] is None:
            raise GenerationSchemaError(f"source analysis summary is missing: {source_run_id}")
        try:
            summary = json.loads(str(row[5]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GenerationSchemaError(f"source analysis summary is malformed: {source_run_id}") from exc
        if not isinstance(summary, dict):
            raise GenerationSchemaError(f"source analysis summary is not an object: {source_run_id}")
        return {
            "source_run_id": source_run_id,
            "framework_run_id": framework_run_id,
            "processing_signature": processing_signature,
            "status": status,
            "started_ns": started_ns,
            "completed_ns": completed_ns,
            "summary": summary,
        }

    def observe_publication(
        self, observer_run_id: int, publication: GraphPublicationResult,
        *, resolver_signature: str,
    ) -> GraphObservation:
        observer = self.validate_source_run_id(observer_run_id)
        signature = str(observer["processing_signature"])
        self.validate_source_run_id(publication.source_run_id, expected_processing_signature=signature)
        head = self.get_head(publication.head_name)
        generation = self.get_generation(publication.generation_id)
        snapshot = self.get_input_snapshot(publication.snapshot_id)
        if (
            head is None or generation is None or snapshot is None
            or head.generation_id != publication.generation_id
            or head.generation_digest != publication.generation_digest
            or head.revision != publication.head_revision
            or generation.status != "published"
            or generation.snapshot_id != snapshot.snapshot_id
            or generation.generation_digest != publication.generation_digest
            or generation.metadata.get("source_run_id") != publication.source_run_id
            or generation.metadata.get("processing_signature") != signature
            or generation.metadata.get("input_digest") != snapshot.input_digest
            or snapshot.source_run_id != publication.source_run_id
            or snapshot.status != "sealed"
        ):
            raise GenerationConflict("Code graph publication observation differs from the current head")
        return GraphObservation(
            observer_run_id, publication.source_run_id, snapshot.snapshot_id,
            snapshot.input_digest, generation.generation_id, publication.generation_digest,
            head.head_name, head.revision, publication.item_count,
            graph_revision(self._connection), _text(resolver_signature, "resolver_signature"), signature,
        )

    def reusable_observation(
        self, observer_run_id: int, *, processing_signature: str, resolver_signature: str,
    ) -> GraphObservation | None:
        """Validate a receipt and transactional revision without scanning graph rows."""

        source = self.validate_source_run_id(observer_run_id, expected_processing_signature=processing_signature)
        summary = source["summary"]
        if not isinstance(summary, dict):
            return None
        payload = summary.get("graph_publication")
        if payload is None:
            return None  # a v7 producer gets one normal v8 publication
        if not isinstance(payload, dict):
            raise GenerationSchemaError("Code graph observation is malformed")
        try:
            observation = GraphObservation(**payload)
            for name in ("observer_run_id", "producer_run_id", "head_revision", "item_count", "graph_revision"):
                _index(getattr(observation, name), name)
            for name in ("snapshot_id", "input_digest", "generation_id", "generation_digest", "head_name"):
                _text(getattr(observation, name), name)
        except (TypeError, ValueError) as exc:
            raise GenerationSchemaError("Code graph observation is malformed") from exc
        if (
            observation.contract != "code-graph-observation-v1"
            or observation.observer_run_id != observer_run_id
            or observation.resolver_signature != resolver_signature
            or observation.processing_signature != processing_signature
            or observation.graph_revision != graph_revision(self._connection)
        ):
            return None
        expected = self.observe_publication(
            observer_run_id, observation.publication(), resolver_signature=resolver_signature
        )
        if expected != observation:
            raise GenerationConflict("Code graph observation no longer matches its publication")
        return observation

    def _legacy_input_snapshot_items(self) -> Iterable[CodeInput]:
        """Capture current file identities without opening source files."""

        for row in self._connection.execute(
            """SELECT file_id,version_id,path_observed,size,mtime_ns,
            raw_xxh3_128,text_xxh3_128,structure_xxh3_128,analysis_status,
            processing_signature,language,artifact_kind
            FROM file_versions WHERE invalidated_ns IS NULL ORDER BY version_id"""
        ):
            file_id = _row_int(row[0], "file_id")
            version_id = _row_int(row[1], "version_id")
            path = str(row[2])
            payload = {
                "file_id": file_id,
                "version_id": version_id,
                "path": path,
                "size": _row_int(row[3], "size"),
                "mtime_ns": _row_int(row[4], "mtime_ns"),
                "raw_xxh3_128": row[5],
                "text_xxh3_128": row[6],
                "structure_xxh3_128": row[7],
                "analysis_status": str(row[8]),
                "processing_signature": str(row[9]),
                "language": None if row[10] is None else str(row[10]),
                "artifact_kind": str(row[11]),
            }
            digest = next(
                (
                    str(value)
                    for value in (row[5], row[6], row[7])
                    if (
                        isinstance(value, str)
                        and value
                        and not any(character.isspace() for character in value)
                    )
                ),
                None,
            )
            if digest is None:
                digest = _hash(payload, "file input")
            yield CodeInput(
                f"file:{file_id}",
                digest,
                version_id,
                path,
                {
                    "analysis_status": str(row[8]),
                    "artifact_kind": str(row[11]),
                    "language": None if row[10] is None else str(row[10]),
                },
            )

    def _legacy_graph_memberships(
        self, *, cancellation_check: Callable[[], None] | None = None,
    ) -> tuple[GraphMembership, ...]:
        """Materialize a deterministic, current graph projection.

        This is a bridge for the existing Code producer: the legacy tables
        remain authoritative and readable, while the additive ledger records a
        content-addressed snapshot of the graph that was actually published.
        No source file or second SQLite owner is opened here.
        """

        members: list[GraphMembership] = []

        def add(
            table: str,
            key: str,
            payload: Mapping[str, object],
            source_version_id: int | None = None,
        ) -> None:
            if cancellation_check is not None:
                cancellation_check()
            members.append(
                GraphMembership(
                    key,
                    _hash({"table": table, "row": dict(payload)}, "graph row"),
                    source_version_id,
                    {"table": table},
                )
            )

        for row in self._connection.execute(
            """SELECT version_id,file_id,path_observed,size,mtime_ns,birthtime_ns,
            raw_xxh3_128,text_xxh3_128,normalized_xxh3_128,token_xxh3_128,
            structure_xxh3_128,language,artifact_kind,analysis_status,
            processing_signature,analyzer_id,analyzer_version,parser_kind,
            text_chars,text_truncated,provenance_json
            FROM file_versions WHERE invalidated_ns IS NULL ORDER BY version_id"""
        ):
            version_id = _row_int(row[0], "version_id")
            add(
                "file_versions",
                f"version:{version_id}",
                {str(index): value for index, value in enumerate(row)},
                version_id,
            )

        for row in self._connection.execute(
            """SELECT s.symbol_id,s.version_id,s.parent_symbol_id,s.kind,s.name,
            s.qualified_name,s.signature,s.visibility,s.docstring,s.confirmed,
            s.complexity,s.start_line,s.start_column,s.end_line,s.end_column,
            s.start_byte,s.end_byte,s.metadata_json
            FROM symbols s JOIN file_versions v ON v.version_id=s.version_id
            WHERE v.invalidated_ns IS NULL ORDER BY s.symbol_id"""
        ):
            symbol_id = _row_int(row[0], "symbol_id")
            add("symbols", f"symbol:{symbol_id}", {str(index): value for index, value in enumerate(row)}, int(row[1]))

        for row in self._connection.execute(
            """SELECT r.reference_id,r.version_id,r.source_symbol_id,
            r.target_symbol_id,r.target_version_id,r.kind,r.name,r.target_hint,
            r.confirmed,r.confidence,r.evidence,r.start_line,r.start_column,
            r.end_line,r.end_column,r.start_byte,r.end_byte
            FROM code_references r JOIN file_versions v ON v.version_id=r.version_id
            WHERE v.invalidated_ns IS NULL ORDER BY r.reference_id"""
        ):
            reference_id = _row_int(row[0], "reference_id")
            add("code_references", f"reference:{reference_id}", {str(index): value for index, value in enumerate(row)}, int(row[1]))

        for row in self._connection.execute(
            """SELECT d.dependency_id,d.version_id,d.resolved_version_id,d.name,
            d.kind,d.scope,d.version_spec,d.confirmed,d.confidence,d.evidence,
            d.start_line,d.start_column,d.end_line,d.end_column,d.start_byte,d.end_byte
            FROM dependencies d JOIN file_versions v ON v.version_id=d.version_id
            WHERE v.invalidated_ns IS NULL ORDER BY d.dependency_id"""
        ):
            dependency_id = _row_int(row[0], "dependency_id")
            add("dependencies", f"dependency:{dependency_id}", {str(index): value for index, value in enumerate(row)}, int(row[1]))

        for row in self._connection.execute(
            """SELECT d.diagnostic_id,d.version_id,d.source,d.code,d.severity,
            d.message,d.tool_name,d.tool_version,d.confirmed,d.confidence,
            d.start_line,d.start_column,d.end_line,d.end_column,d.start_byte,
            d.end_byte,d.metadata_json
            FROM diagnostics d JOIN file_versions v ON v.version_id=d.version_id
            WHERE v.invalidated_ns IS NULL ORDER BY d.diagnostic_id"""
        ):
            diagnostic_id = _row_int(row[0], "diagnostic_id")
            add("diagnostics", f"diagnostic:{diagnostic_id}", {str(index): value for index, value in enumerate(row)}, int(row[1]))

        for row in self._connection.execute(
            """SELECT m.metric_id,m.version_id,m.symbol_id,m.name,m.value,
            m.confirmed,m.provenance
            FROM metrics m JOIN file_versions v ON v.version_id=m.version_id
            WHERE v.invalidated_ns IS NULL ORDER BY m.metric_id"""
        ):
            metric_id = _row_int(row[0], "metric_id")
            add("metrics", f"metric:{metric_id}", {str(index): value for index, value in enumerate(row)}, int(row[1]))

        for row in self._connection.execute(
            """SELECT c.chunk_id,c.version_id,c.symbol_id,c.chunk_index,c.kind,
            c.start_line,c.end_line,c.start_byte,c.end_byte,c.text,c.text_xxh3_128
            FROM code_chunks c JOIN file_versions v ON v.version_id=c.version_id
            WHERE v.invalidated_ns IS NULL ORDER BY c.chunk_id"""
        ):
            chunk_id = _row_int(row[0], "chunk_id")
            add("code_chunks", f"chunk:{chunk_id}", {str(index): value for index, value in enumerate(row)}, int(row[1]))

        for row in self._connection.execute(
            """SELECT project_id,project_key,name,ecosystem,probable_root,
            manifest_kind,confidence,evidence_json,first_seen_run_id,
            last_seen_run_id,status FROM projects
            WHERE status IN ('current','ambiguous') ORDER BY project_id"""
        ):
            project_id = _row_int(row[0], "project_id")
            add("projects", f"project:{project_id}", {str(index): value for index, value in enumerate(row)})

        for row in self._connection.execute(
            """SELECT m.project_id,m.version_id,m.proposed_path,m.relation,
            m.confidence,m.selected,m.conflict_group,m.evidence_json
            FROM project_memberships m
            JOIN projects p ON p.project_id=m.project_id
            JOIN file_versions v ON v.version_id=m.version_id
            WHERE p.status IN ('current','ambiguous') AND v.invalidated_ns IS NULL
            ORDER BY m.project_id,m.version_id"""
        ):
            project_id = _row_int(row[0], "project_id")
            version_id = _row_int(row[1], "version_id")
            add(
                "project_memberships",
                f"project-membership:{project_id}:{version_id}",
                {str(index): value for index, value in enumerate(row)},
                version_id,
            )

        for row in self._connection.execute(
            """SELECT e.source_project_id,e.target_project_id,e.dependency_name,
            e.edge_kind,e.confidence,e.evidence_json
            FROM project_edges e JOIN projects p ON p.project_id=e.source_project_id
            WHERE p.status IN ('current','ambiguous')
            ORDER BY e.source_project_id,e.dependency_name,e.edge_kind"""
        ):
            payload = {str(index): value for index, value in enumerate(row)}
            key_digest = _hash(payload, "project edge key")
            add("project_edges", f"project-edge:{key_digest}", payload)

        for row in self._connection.execute(
            """SELECT r.relation_id,r.left_version_id,r.right_version_id,
            r.relation_kind,r.confidence,r.evidence_json,r.created_ns
            FROM version_relations r
            JOIN file_versions left_version ON left_version.version_id=r.left_version_id
            JOIN file_versions right_version ON right_version.version_id=r.right_version_id
            WHERE left_version.invalidated_ns IS NULL OR right_version.invalidated_ns IS NULL
            ORDER BY r.relation_id"""
        ):
            relation_id = _row_int(row[0], "relation_id")
            add("version_relations", f"relation:{relation_id}", {str(index): value for index, value in enumerate(row)})

        return tuple(sorted(members, key=lambda item: item.item_key))

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
        cancellation_check: Callable[[], None] | None = None,
    ) -> InputSnapshot:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        source_run_id = _index(source_run_id, "source_run_id")
        metadata_json = _json(dict(metadata or {}), "snapshot metadata")
        created = _now(created_ns, "created_ns")
        with self._transaction() as connection:
            # Sort in SQLite instead of holding the inputs, their payload
            # copies and a complete JSON byte string in memory simultaneously.
            # Every item's metadata keeps its original 256 KiB bound.
            connection.execute(
                "CREATE TEMP TABLE _code_snapshot_inputs("
                "input_key TEXT PRIMARY KEY,content_digest TEXT NOT NULL,"
                "source_version_id INTEGER,observed_path TEXT,metadata_json TEXT NOT NULL) WITHOUT ROWID"
            )
            try:
                batch: list[tuple[object, ...]] = []
                input_count = 0
                insert = "INSERT INTO temp._code_snapshot_inputs VALUES(?,?,?,?,?)"
                for raw_item in inputs:
                    if cancellation_check is not None:
                        cancellation_check()
                    item = _input(raw_item)
                    batch.append((item.key, item.content_digest, item.source_version_id,
                                  item.observed_path, _json(dict(item.metadata), "input metadata")))
                    input_count += 1
                    if len(batch) >= 256:
                        connection.executemany(insert, batch)
                        batch.clear()
                if batch:
                    connection.executemany(insert, batch)
                input_digest = _hash_input_rows(
                    connection.execute(
                        "SELECT input_key,content_digest,source_version_id,observed_path,metadata_json "
                        "FROM temp._code_snapshot_inputs ORDER BY input_key"
                    ),
                    cancellation_check,
                )
                return self._store_input_snapshot(
                    snapshot_id, source_run_id, input_digest, input_count, metadata_json, created
                )
            except sqlite3.IntegrityError as exc:
                if "_code_snapshot_inputs.input_key" in str(exc):
                    raise ValueError("snapshot input keys must be unique") from exc
                raise
            finally:
                connection.execute("DROP TABLE temp._code_snapshot_inputs")

    def _store_input_snapshot(
        self, snapshot_id: str, source_run_id: int, input_digest: str,
        input_count: int, metadata_json: str, created: int,
    ) -> InputSnapshot:
        """Publish the sorted staging rows inside create_input_snapshot's transaction."""

        connection = self._connection
        with self._transaction():
            row = connection.execute(
                "SELECT source_run_id,input_digest,input_count,status,created_ns,metadata_json "
                "FROM graph_input_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row is not None:
                if (int(row[0]), str(row[1]), int(row[2]), str(row[5])) != (
                    source_run_id,
                    input_digest,
                    input_count,
                    metadata_json,
                ):
                    raise GenerationConflict(f"input snapshot differs: {snapshot_id}")
                return self._snapshot(row, snapshot_id)
            connection.execute(
                "INSERT INTO graph_input_snapshots(snapshot_id,source_run_id,input_digest,input_count,status,created_ns,metadata_json) "
                "VALUES(?,?,?,?,'sealed',?,?)",
                (snapshot_id, source_run_id, input_digest, input_count, created, metadata_json),
            )
            connection.execute(
                "INSERT INTO graph_snapshot_inputs(snapshot_id,input_key,content_digest,source_version_id,observed_path,metadata_json) "
                "SELECT ?,input_key,content_digest,source_version_id,observed_path,metadata_json "
                "FROM temp._code_snapshot_inputs ORDER BY input_key",
                (snapshot_id,),
            )
            return InputSnapshot(
                snapshot_id,
                source_run_id,
                input_digest,
                input_count,
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

    def publish_legacy_graph(
        self,
        source_run_id: int,
        *,
        head_name: str = DEFAULT_HEAD_NAME,
        batch_size: int = 256,
        cancellation_check: Callable[[], None] | None = None,
    ) -> GraphPublicationResult:
        """Publish the current legacy Code graph through the generation ledger.

        ``CodeRoute`` still writes the established product tables because
        legacy readers are part of the compatibility surface.  This bridge
        captures those rows only after graph reconciliation, then commits an
        immutable generation and advances the named head with CAS.  The whole
        operation is one writer transaction (nested savepoints are used when
        called from ``CodeState.complete_run``), so a cancelled or failed
        publication cannot leave a head pointing at a partial generation.
        """

        head_name = _text(head_name, "head_name")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 4096:
            raise ValueError("batch_size must be between 1 and 4096")
        if cancellation_check is not None and not callable(cancellation_check):
            raise TypeError("cancellation_check must be callable")

        def check_cancelled() -> None:
            if cancellation_check is not None:
                cancellation_check()

        with self._transaction():
            source = self.validate_source_run_id(source_run_id)
            source_id = _row_int(source["source_run_id"], "source_run_id")
            framework_id = _row_int(source["framework_run_id"], "framework_run_id")
            processing_signature = _text(
                str(source["processing_signature"]), "processing_signature"
            )
            snapshot_id = f"analysis:{source_id}:inputs"
            generation_id = f"analysis:{source_id}:graph"
            snapshot_metadata = {
                "contract": "code-graph-legacy-bridge-v1",
                "source_run_id": source_id,
                "framework_run_id": framework_id,
                "processing_signature": processing_signature,
            }
            inputs = self._legacy_input_snapshot_items()
            check_cancelled()
            snapshot = self.create_input_snapshot(
                snapshot_id,
                source_id,
                inputs,
                metadata=snapshot_metadata,
                cancellation_check=cancellation_check,
            )
            generation = self.start_generation(
                snapshot.snapshot_id,
                generation_id,
                metadata={**snapshot_metadata, "input_digest": snapshot.input_digest},
            )
            if generation.status == "building":
                members = self._legacy_graph_memberships(cancellation_check=cancellation_check)
                for batch_index, start in enumerate(range(0, len(members), batch_size)):
                    check_cancelled()
                    batch = members[start : start + batch_size]
                    cursor = batch[-1].item_key if batch else None
                    self.append_batch(
                        generation_id,
                        batch_index,
                        batch,
                        cursor=cursor,
                    )
                    self.checkpoint(generation_id, batch_index, cursor or "empty")
                check_cancelled()
                generation = self.complete_generation(generation_id)
            elif generation.status in {"completed", "published"}:
                # ``start_generation`` is idempotent; an already-complete
                # generation can be resumed directly at the CAS boundary.
                pass
            else:
                raise GenerationStateError(
                    f"generation cannot be published from state {generation.status}: {generation_id}"
                )
            if generation.generation_digest is None:
                raise GenerationSchemaError(f"generation digest is missing: {generation_id}")
            current = self.get_head(head_name)
            if current is not None and current.generation_id == generation_id:
                if current.generation_digest != generation.generation_digest:
                    raise GenerationSchemaError(f"published generation digest differs: {generation_id}")
                head = current
                reused = True
            else:
                try:
                    head = self.compare_and_swap_head(
                        head_name,
                        expected_revision=0 if current is None else current.revision,
                        expected_generation_id=None if current is None else current.generation_id,
                        generation_id=generation_id,
                    )
                except GenerationHeadConflict:
                    # A concurrent replay may have published this exact
                    # generation between the read and the CAS.  Accept only
                    # that idempotent outcome; a different head remains a
                    # genuine conflict and must fail closed.
                    raced = self.get_head(head_name)
                    if raced is None or raced.generation_id != generation_id:
                        raise
                    if raced.generation_digest != generation.generation_digest:
                        raise GenerationSchemaError(
                            f"concurrent generation digest differs: {generation_id}"
                        ) from None
                    head = raced
                    reused = True
                else:
                    reused = False
            # Graph projections are reconstructible; keep current plus one
            # rollback generation and tombstone older rows without touching a
            # head or an in-progress generation.
            self.prune_generations(keep=2)
            item_count = int(self._connection.execute(
                "SELECT COUNT(*) FROM graph_memberships WHERE generation_id=?", (generation_id,)
            ).fetchone()[0])
            return GraphPublicationResult(
                source_id,
                snapshot.snapshot_id,
                generation_id,
                generation.generation_digest,
                head.head_name,
                head.revision,
                item_count,
                reused,
            )

    def cancel_generation(self, generation_id: str) -> GraphGeneration:
        """Cancel one building generation without touching the published head."""

        return self.abort_generation(generation_id)

    def recover_stale_generations(
        self,
        *,
        stale_after_ns: int,
        now_ns: int | None = None,
    ) -> tuple[str, ...]:
        """Abort abandoned ``building`` generations after a bounded lease."""

        stale_after_ns = _index(stale_after_ns, "stale_after_ns")
        if stale_after_ns <= 0:
            raise ValueError("stale_after_ns must be positive")
        now = _now(now_ns, "now_ns")
        cutoff = now - stale_after_ns
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT generation_id FROM graph_generations
                WHERE status='building' AND created_ns<=?
                AND generation_id NOT IN (SELECT generation_id FROM graph_heads)
                ORDER BY created_ns,generation_id""",
                (cutoff,),
            ).fetchall()
            ids = tuple(str(row[0]) for row in rows)
            if ids:
                connection.executemany(
                    "UPDATE graph_generations SET status='aborted' WHERE generation_id=? AND status='building'",
                    ((generation_id,) for generation_id in ids),
                )
            return ids

    def prune_generations(
        self,
        *,
        keep: int = 2,
    ) -> tuple[str, ...]:
        """Tombstone old reconstructible generations while protecting heads.

        Pruning never deletes a generation row or a snapshot identity, which
        preserves lineage and makes an interrupted cleanup recoverable.  Child
        batches/memberships/checkpoints are removed only after the generation
        is marked ``pruned``; all heads and the newest ``keep`` generations are
        retained.
        """

        keep = _index(keep, "keep")
        if keep < 1:
            raise ValueError("keep must be at least one")
        with self._transaction() as connection:
            protected = {
                str(row[0])
                for row in connection.execute("SELECT generation_id FROM graph_heads")
            }
            rows = connection.execute(
                """SELECT generation_id,snapshot_id,status FROM graph_generations
                WHERE status IN ('completed','published','aborted')
                ORDER BY created_ns DESC,generation_id DESC"""
            ).fetchall()
            retained = {str(row[0]) for row in rows[:keep]}
            pruned: list[str] = []
            for row in rows:
                generation_id = str(row[0])
                if generation_id in protected or generation_id in retained:
                    continue
                snapshot_id = str(row[1])
                changed = connection.execute(
                    "UPDATE graph_generations SET status='pruned' WHERE generation_id=? AND status IN ('completed','published','aborted')",
                    (generation_id,),
                ).rowcount
                if changed != 1:
                    continue
                connection.execute("DELETE FROM graph_checkpoints WHERE generation_id=?", (generation_id,))
                connection.execute("DELETE FROM graph_memberships WHERE generation_id=?", (generation_id,))
                connection.execute("DELETE FROM graph_batches WHERE generation_id=?", (generation_id,))
                connection.execute(
                    "DELETE FROM graph_snapshot_inputs WHERE snapshot_id=?", (snapshot_id,)
                )
                connection.execute(
                    "UPDATE graph_input_snapshots SET status='pruned' WHERE snapshot_id=? AND status='sealed'",
                    (snapshot_id,),
                )
                pruned.append(generation_id)
            return tuple(pruned)

    def get_head(self, head_name: str = DEFAULT_HEAD_NAME) -> GenerationHead | None:
        head_name = _text(head_name, "head_name")
        row = self._connection.execute(
            "SELECT generation_id,generation_digest,revision,updated_ns FROM graph_heads WHERE head_name=?",
            (head_name,),
        ).fetchone()
        if row is None:
            return None
        return GenerationHead(head_name, str(row[0]), str(row[1]), int(row[2]), int(row[3]))

    def read_published_generation(
        self,
        head_name: str = DEFAULT_HEAD_NAME,
    ) -> PublishedGraph | None:
        """Read one head without exposing building, aborted or pruned data."""

        head = self.get_head(head_name)
        if head is None:
            return None
        generation = self.get_generation(head.generation_id)
        if generation is None:
            raise GenerationSchemaError(f"head references a missing generation: {head.generation_id}")
        if generation.status != "published":
            raise GenerationStateError(
                f"head references a non-published generation: {head.generation_id}"
            )
        if generation.generation_digest is None or generation.generation_digest != head.generation_digest:
            raise GenerationSchemaError(f"published head digest differs: {head.generation_id}")
        metadata_source = generation.metadata.get("source_run_id")
        if metadata_source is not None and (
            isinstance(metadata_source, bool)
            or not isinstance(metadata_source, int)
            or metadata_source <= 0
        ):
            raise GenerationSchemaError(f"published generation source_run_id is invalid: {head.generation_id}")
        if metadata_source is not None:
            self.validate_source_run_id(metadata_source)
        return PublishedGraph(head, generation, self.list_memberships(generation.generation_id))

    # A short alias keeps callers independent of the storage-oriented name.
    read_published = read_published_generation

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
    "GraphObservation",
    "GraphPublicationResult",
    "InputSnapshot",
    "PublishedGraph",
]
