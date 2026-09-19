"""Durable repository for incremental code observations and derived graphs."""

from __future__ import annotations
import json
import os
import sqlite3
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from neocortex.deduplication import FileSnapshot
from neocortex.platform.policy import sqlite_path_collation

from neocortex.runtime.control.cancellation import CancellationRequested
from .code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    CodeAnalysis,
    DiagnosticRecord,
)
from .code_schema import connect_code_state, initialize_code_state
from .code_fts_lookup import CodeFTSLookup
from .code_graph_revision import graph_revision
from .code_graph_generations import GenerationConflict, GenerationError
from .code_retention import (
    CodeRetentionPolicy,
    CodeRetentionResult,
    apply_code_retention,
)
from neocortex.safety.route_filters import CandidateSelection
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text
from neocortex.persistence.sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)

if TYPE_CHECKING:
    from .code_graph_generations import CodeGraphGenerationStore, GraphObservation

# region [01] Repository records and helpers


CODE_STATE_WRITE_BATCH = 512
CODE_GRAPH_RESOLVER_SIGNATURE = "code-graph-resolver-v7"
_PATH_COLLATION = sqlite_path_collation()
_GRAPH_COMPLETION_KEY = "code_graph_completion_v3"
_GRAPH_FENCE_SCHEMA_VERSION = 1
_MAX_RELATIVE_DEPENDENCY_NAME_BYTES = 4_096
_DERIVED_DIAGNOSTIC_SOURCES = (
    "neocortex-project-resolver",
    "neocortex-project-graph",
    "neocortex-reference-graph",
)


@dataclass(frozen=True, slots=True)
class CachedCodeVersion:
    """Counters needed to report one cache hit without loading analysis rows."""

    version_id: int
    status: str
    generated: bool
    vendored: bool
    symbols: int
    references: int
    diagnostics: int
    fts_rows_repaired: int = 0


@dataclass(frozen=True, slots=True)
class _GraphReuseProof:
    analysis_run_id: int
    observation: GraphObservation


@dataclass(frozen=True, slots=True)
class SkippedCodeObservation:
    """Persistable evidence for binary, oversized or failed candidates."""

    snapshot: FileSnapshot
    classification: ArtifactClassification
    processing_signature: str
    status: AnalysisStatus
    analyzer_id: str
    analyzer_version: str
    parser_kind: str
    diagnostic: DiagnosticRecord
    encoding: str | None = None
    text_excerpt: str = ""
    text_truncated: bool = False
    raw_xxh3_128: str | None = None
    raw_xxh3_64_guard: str | None = None
    provenance: Mapping[str, object] | None = None


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _identity(snapshot: FileSnapshot) -> tuple[str, str]:
    return f"{snapshot.volume_id:x}", f"{snapshot.file_id:x}"


def _relative_import_candidate_paths(
    source_path: str,
    dependency_name: str,
) -> tuple[str, ...]:
    """Return lexical native paths for one analyzer-v3 relative import."""

    if (
        not dependency_name.startswith(".")
        or len(dependency_name.encode("utf-8")) > _MAX_RELATIVE_DEPENDENCY_NAME_BYTES
    ):
        return ()
    level = len(dependency_name) - len(dependency_name.lstrip("."))
    module = dependency_name[level:]
    parts = tuple(module.split(".")) if module else ()
    if any(not part.isidentifier() for part in parts):
        return ()
    base = Path(source_path).parent
    for _ in range(level - 1):
        parent = base.parent
        if parent == base:
            return ()
        base = parent
    if not parts:
        return (str(base / "__init__.py"),)
    module_path = base.joinpath(*parts)
    return (str(module_path.with_suffix(".py")), str(module_path / "__init__.py"))


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    value = cursor.lastrowid
    if value is None:
        raise RuntimeError("SQLite did not return an inserted row identifier")
    return value


def _summary_int(summary: Mapping[str, object], key: str) -> int:
    value = summary.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    raise TypeError(f"summary field {key!r} must be an integer")


def _range_values(source_range) -> tuple[int | None, ...]:
    if source_range is None:
        return (None, None, None, None, None, None)
    return (
        source_range.start_line,
        source_range.start_column,
        source_range.end_line,
        source_range.end_column,
        source_range.start_byte,
        source_range.end_byte,
    )


def _validate_optional_raw_fingerprint(
    raw_xxh3_128: str | None,
    raw_xxh3_64_guard: str | None,
) -> None:
    """Require one complete, canonical XXH3 collision-guard pair when supplied."""

    if raw_xxh3_128 is None and raw_xxh3_64_guard is None:
        return
    if raw_xxh3_128 is None or raw_xxh3_64_guard is None:
        raise ValueError("raw fingerprint and guard must be supplied together")
    if len(raw_xxh3_128) != 32 or any(
        character not in "0123456789abcdef" for character in raw_xxh3_128
    ):
        raise ValueError("raw_xxh3_128 must be 32 lowercase hexadecimal characters")
    if len(raw_xxh3_64_guard) != 16 or any(
        character not in "0123456789abcdef" for character in raw_xxh3_64_guard
    ):
        raise ValueError("raw_xxh3_64_guard must be 16 lowercase hexadecimal characters")


def _normalized_project_root(value: str) -> str:
    """Return a lexical local root key without requiring that the root still exists."""

    return os.path.normcase(os.path.abspath(os.path.normpath(value))).replace("\\", "/")


def _project_identity_keys(
    ecosystem: str,
    name: str,
    root: str,
) -> tuple[str, str]:
    """Return separate family and rooted-instance identities for one project hint."""

    family_payload = {
        "ecosystem": ecosystem.casefold(),
        "name": name.casefold(),
    }
    family_key = fingerprint_text(canonical_json(family_payload)).xxh3_128
    instance_key = fingerprint_text(
        canonical_json(
            {
                **family_payload,
                "root": _normalized_project_root(root),
                "identity_version": 1,
            }
        )
    ).xxh3_128
    return family_key, instance_key


_RETRY_RECOMMENDATIONS = frozenset({"retry", "retry_source", "retryable"})
_CODE_CHUNKING_ALGORITHM = "searchable_chunks-v1"
_CODE_EXCERPT_CHUNKING_ALGORITHM = "bounded_excerpt-v1"
_CODE_CHUNKING_ALGORITHMS = frozenset(
    {_CODE_CHUNKING_ALGORITHM, _CODE_EXCERPT_CHUNKING_ALGORITHM}
)


def _explicit_retryable_marker(value: object) -> bool:
    """Accept only structured retry evidence, never free-form error text."""

    if not isinstance(value, Mapping):
        return False
    return value.get("retryable") is True or (
        isinstance(value.get("recommendation"), str)
        and value["recommendation"] in _RETRY_RECOMMENDATIONS
    )


def _with_chunk_contract(
    provenance: Mapping[str, object],
    chunk_count: int,
    algorithm: str,
) -> dict[str, object]:
    """Persist the expected durable chunk cardinality beside analyzer evidence."""

    return {
        **provenance,
        "code_chunk_count": chunk_count,
        "code_chunking_algorithm": algorithm,
    }


def _cached_chunk_contract(value: object) -> tuple[int, str] | None:
    """Read the optional chunk contract; absent legacy evidence stays readable."""

    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return (-1, "invalid")
    if not isinstance(payload, dict):
        return (-1, "invalid")
    if "code_chunk_count" not in payload and "code_chunking_algorithm" not in payload:
        return None
    count = payload.get("code_chunk_count")
    algorithm = payload.get("code_chunking_algorithm")
    if (
        type(count) is not int
        or count < 0
        or not isinstance(algorithm, str)
        or algorithm not in _CODE_CHUNKING_ALGORITHMS
    ):
        return (-1, "invalid")
    return count, algorithm


# endregion [01]


# region [02] Lifecycle and cache reuse


class CodeState:
    """Single-writer route repository with bounded, atomic file publications."""

    def __init__(
        self,
        path: Path,
        *,
        retention_policy: CodeRetentionPolicy | None = None,
    ):
        self.path = Path(path)
        initialize_code_state(self.path)
        self.connection = connect_code_state(self.path, create=False)
        self.retention_policy = retention_policy
        self._version_count_cache: dict[int, tuple[int, int, int]] | None = None
        self._graph_generation_store: CodeGraphGenerationStore | None = None
        self._fts_lookup = CodeFTSLookup(self.connection)
        self._graph_reuse_proof: _GraphReuseProof | None = None
        self.last_graph_publication_reused = False
        self.last_graph_publication_milliseconds = 0

    def close(self) -> None:
        self._graph_generation_store = None
        self.connection.close()

    @property
    def graph_generation_store(self) -> CodeGraphGenerationStore:
        """Return the additive generation API on this Code owner connection."""

        if self._graph_generation_store is None:
            from .code_graph_generations import CodeGraphGenerationStore

            self._graph_generation_store = CodeGraphGenerationStore(self.connection)
        return self._graph_generation_store

    def __enter__(self) -> CodeState:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def begin_run(
        self,
        framework_run_id: int,
        scan_id: int,
        processing_signature: str,
    ) -> int:
        now = time.time_ns()
        self._graph_reuse_proof = None
        self.last_graph_publication_reused = False
        self.last_graph_publication_milliseconds = 0
        with self.connection:
            self.connection.execute(
                """UPDATE analysis_runs SET status='interrupted',completed_ns=?,
                error_type='AbandonedRun',error_message='process ended before completion'
                WHERE status='running'""",
                (now,),
            )
            if self.retention_policy is not None:
                apply_code_retention(
                    self.connection,
                    policy=self.retention_policy,
                    now_ns=now,
                )
            cursor = self.connection.execute(
                """INSERT INTO analysis_runs(
                framework_run_id,scan_id,processing_signature,status,started_ns)
                VALUES(?,?,?,'running',?)""",
                (framework_run_id, scan_id, processing_signature, now),
            )
        return _lastrowid(cursor)

    def complete_run(
        self,
        analysis_run_id: int,
        summary: Mapping[str, object],
        *,
        partial: bool,
        graph_current: bool = False,
        retention_policy: CodeRetentionPolicy | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> CodeRetentionResult | None:
        """Complete one run and optionally publish its graph-completion fence."""

        cancellation = SQLiteCancellationBridge(cancellation_check)
        cancellation.checkpoint()
        with sqlite_cancellation_scope(self.connection, cancellation), self.connection:
            updated = self.connection.execute(
                """UPDATE analysis_runs SET status=?,completed_ns=?,candidates=?,
                processed=?,cache_hits=?,errors=?,summary_json=?,error_type=NULL,
                error_message=NULL WHERE analysis_run_id=? AND status='running'""",
                (
                    "partial" if partial else "completed",
                    time.time_ns(),
                    _summary_int(summary, "candidates"),
                    _summary_int(summary, "processed"),
                    _summary_int(summary, "cache_hits"),
                    _summary_int(summary, "errors"),
                    _json(summary),
                    analysis_run_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("code analysis run completion requires one running owner row")
            if graph_current and not partial:
                # Publish the additive graph generation before releasing the
                # producer transaction.  The generation bridge validates that
                # this exact analysis run is complete, captures the reconciled
                # legacy graph, and advances its head with CAS.  Nested
                # savepoints keep the run status, graph fence and generation
                # head atomic on failure or cancellation.
                publication_started = time.perf_counter_ns()
                proof = self._graph_reuse_proof
                if proof is not None and proof.analysis_run_id == analysis_run_id:
                    if graph_revision(self.connection) != proof.observation.graph_revision:
                        raise GenerationConflict("Code graph changed after its reuse proof")
                    verified = self.graph_generation_store.reusable_observation(
                        proof.observation.observer_run_id,
                        processing_signature=proof.observation.processing_signature,
                        resolver_signature=CODE_GRAPH_RESOLVER_SIGNATURE,
                    )
                    if verified != proof.observation:
                        raise GenerationConflict("Code graph head changed after its reuse proof")
                    publication = proof.observation.publication()
                else:
                    publication = self.graph_generation_store.publish_legacy_graph(
                        analysis_run_id, cancellation_check=cancellation_check
                    )
                observation = self.graph_generation_store.observe_publication(
                    analysis_run_id, publication, resolver_signature=CODE_GRAPH_RESOLVER_SIGNATURE
                )
                cancellation.checkpoint()
                self.last_graph_publication_reused = publication.reused
                self.last_graph_publication_milliseconds = (
                    time.perf_counter_ns() - publication_started
                ) // 1_000_000
                persisted_summary = {
                    **summary, "graph_publication": asdict(observation),
                    "graph_generation_reused": int(publication.reused),
                    "publication_milliseconds": self.last_graph_publication_milliseconds,
                }
                self.connection.execute(
                    "UPDATE analysis_runs SET summary_json=? WHERE analysis_run_id=?",
                    (_json(persisted_summary), analysis_run_id),
                )
                self.connection.execute(
                    """INSERT INTO metadata(key,value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (
                        _GRAPH_COMPLETION_KEY,
                        _json(
                            {
                                "analysis_run_id": analysis_run_id,
                                "resolver_signature": CODE_GRAPH_RESOLVER_SIGNATURE,
                                "schema_version": _GRAPH_FENCE_SCHEMA_VERSION,
                            }
                        ),
                    ),
                )
            effective_retention = retention_policy or self.retention_policy
            if effective_retention is not None:
                return apply_code_retention(
                    self.connection,
                    current_run_id=analysis_run_id,
                    policy=effective_retention,
                )
        return None

    def fail_run(self, analysis_run_id: int, exc: BaseException) -> None:
        status = (
            "cancelled" if isinstance(exc, (KeyboardInterrupt, CancellationRequested)) else "failed"
        )
        with self.connection:
            self.connection.execute(
                """UPDATE analysis_runs SET status=?,completed_ns=?,error_type=?,
                error_message=? WHERE analysis_run_id=? AND status='running'""",
                (
                    status,
                    time.time_ns(),
                    type(exc).__name__,
                    str(exc)[:8192],
                    analysis_run_id,
                ),
            )

    def reusable_graph_project_count(
        self,
        analysis_run_id: int,
        processing_signature: str,
    ) -> int | None:
        """Return the current project count only behind an exact graph fence.

        The fence advances atomically with a completed analysis run.  Reuse is
        rejected when a later partial, failed, cancelled, or interrupted run
        may have published graph-affecting file state without finalizing it.
        """

        self._graph_reuse_proof = None
        marker = self.connection.execute(
            "SELECT value FROM metadata WHERE key=?",
            (_GRAPH_COMPLETION_KEY,),
        ).fetchone()
        if marker is None:
            return None
        try:
            fence = json.loads(str(marker[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(fence, dict):
            return None
        schema_version = fence.get("schema_version")
        completed_analysis_run_id = fence.get("analysis_run_id")
        if (
            isinstance(schema_version, bool)
            or schema_version != _GRAPH_FENCE_SCHEMA_VERSION
            or isinstance(completed_analysis_run_id, bool)
            or not isinstance(completed_analysis_run_id, int)
            or fence.get("resolver_signature") != CODE_GRAPH_RESOLVER_SIGNATURE
        ):
            return None
        if not 0 < completed_analysis_run_id < analysis_run_id:
            return None
        previous = self.connection.execute(
            """SELECT analysis_run_id,status,processing_signature,summary_json
            FROM analysis_runs WHERE analysis_run_id<?
            ORDER BY analysis_run_id DESC LIMIT 1""",
            (analysis_run_id,),
        ).fetchone()
        if (
            previous is None
            or int(previous["analysis_run_id"]) != completed_analysis_run_id
            or str(previous["status"]) != "completed"
            or str(previous["processing_signature"]) != processing_signature
            or previous["summary_json"] is None
        ):
            return None
        try:
            summary = json.loads(str(previous["summary_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(summary, dict):
            return None
        graph_milliseconds = summary.get("graph_milliseconds")
        if (
            isinstance(graph_milliseconds, bool)
            or not isinstance(graph_milliseconds, int)
            or graph_milliseconds < 0
        ):
            return None
        try:
            observation = self.graph_generation_store.reusable_observation(
                completed_analysis_run_id,
                processing_signature=processing_signature,
                resolver_signature=CODE_GRAPH_RESOLVER_SIGNATURE,
            )
        except (GenerationError, ValueError, TypeError):
            return None
        if observation is None:
            return None
        self._graph_reuse_proof = _GraphReuseProof(analysis_run_id, observation)
        row = self.connection.execute(
            "SELECT COUNT(*) FROM projects WHERE status='current'"
        ).fetchone()
        return int(row[0])

    def matches_selection(
        self,
        snapshot: FileSnapshot,
        selection: CandidateSelection,
    ) -> bool:
        """Evaluate code-owned status/diagnostic filters without loading rows."""

        if not selection.active:
            return True
        if selection.recommendations or selection.failed_pages_only:
            return False
        if not selection.statuses and not selection.error_types:
            # Path filtering is evaluated by the route before this repository
            # lookup and must also work before a file has any persisted state.
            return True
        volume_id, physical_file_id = _identity(snapshot)
        row = self.connection.execute(
            """SELECT v.version_id,v.analysis_status FROM files f
            JOIN file_versions v ON v.version_id=f.current_version_id
            WHERE f.volume_id=? AND f.physical_file_id=? AND f.status='current'
            AND v.invalidated_ns IS NULL""",
            (volume_id, physical_file_id),
        ).fetchone()
        if row is None:
            return False
        if selection.statuses and str(row[1]).casefold() not in selection.statuses:
            return False
        if selection.error_types:
            placeholders = ",".join("?" for _ in selection.error_types)
            matched = self.connection.execute(
                f"""SELECT 1 FROM diagnostics WHERE version_id=?
                AND code IN ({placeholders}) LIMIT 1""",
                (int(row[0]), *selection.error_types),
            ).fetchone()
            if matched is None:
                return False
        return True

    def _load_version_count_cache(self) -> dict[int, tuple[int, int, int]]:
        """Aggregate immutable analyzer-owned child counts once per state open.

        Cache replay previously embedded three correlated ``COUNT`` subqueries
        in every physical-file lookup.  The reference and diagnostic indexes do
        not lead with ``version_id``, so SQLite scanned their complete retained
        history once for every candidate.  Each child row belongs to one
        immutable file version; one grouped pass therefore provides the exact
        same counters without weakening the metadata, analyzer or fingerprint
        checks performed by :meth:`reuse_cached`.
        """

        counts: dict[int, list[int]] = {}

        def merge(rows: Iterable[sqlite3.Row], position: int) -> None:
            for row in rows:
                version_id = int(row[0])
                count = int(row[1])
                if version_id <= 0 or count < 0:
                    raise RuntimeError("cached Code child count is invalid")
                values = counts.setdefault(version_id, [0, 0, 0])
                values[position] = count

        merge(
            self.connection.execute("SELECT version_id,COUNT(*) FROM symbols GROUP BY version_id"),
            0,
        )
        merge(
            self.connection.execute(
                "SELECT version_id,COUNT(*) FROM code_references GROUP BY version_id"
            ),
            1,
        )
        merge(
            self.connection.execute(
                "SELECT version_id,COUNT(*) FROM diagnostics "
                "WHERE source NOT LIKE 'external:%' "
                "AND source NOT IN (?,?,?) GROUP BY version_id",
                _DERIVED_DIAGNOSTIC_SOURCES,
            ),
            2,
        )
        return {
            version_id: (values[0], values[1], values[2]) for version_id, values in counts.items()
        }

    def _version_counts(self, version_id: int) -> tuple[int, int, int]:
        if self._version_count_cache is None:
            self._version_count_cache = self._load_version_count_cache()
        return self._version_count_cache.get(version_id, (0, 0, 0))

    def _cache_version_counts(
        self,
        version_id: int,
        *,
        symbols: int,
        references: int,
        diagnostics: int,
    ) -> None:
        """Keep an already-materialized cache coherent after a local publish."""

        if self._version_count_cache is not None:
            self._version_count_cache[version_id] = (
                symbols,
                references,
                diagnostics,
            )

    def _cached_code_fts_rows(
        self,
        version_id: int,
        *,
        validate_coverage: bool = True,
    ) -> tuple[tuple[int, int, str, str, str | None, str, str, str], ...] | None:
        """Build the expected FTS projection from durable Code rows only.

        A cache replay must never need to read or analyze the source again just
        because the rebuildable FTS table lost rows.  The file representation
        and bounded chunks remain the authoritative durable inputs.  Returning
        ``None`` means that one of those inputs is incomplete or malformed, so
        the route must fall back to normal bounded ingestion.  ``validate_coverage``
        may be skipped for a current state carrying the durable chunk contract,
        but an unmarked legacy state always requires the canonical extent proof
        before it can be treated as a cache hit.
        """

        version = self.connection.execute(
            """SELECT analysis_status,size,text_zlib,text_chars,text_xxh3_128,
            text_xxh3_64_guard,provenance_json FROM file_versions
            WHERE version_id=? AND invalidated_ns IS NULL""",
            (version_id,),
        ).fetchone()
        if version is None:
            return None
        try:
            text_chars = int(version["text_chars"])
            source_bytes = int(version["size"])
        except (TypeError, ValueError, OverflowError):
            return None
        # Code text is decoded from the physically bounded source bytes; its
        # character count cannot exceed that source byte count. Do not let
        # corrupt cache metadata turn the decompression bound into an allocation
        # chosen by the damaged owner row.
        if text_chars < 0 or source_bytes < 0 or text_chars > source_bytes:
            return None
        canonical_bytes = b""
        if text_chars:
            payload = version["text_zlib"]
            if payload is None:
                return None
            try:
                decoder = zlib.decompressobj()
                output_limit = text_chars * 4 + 1
                decoded = decoder.decompress(bytes(payload), output_limit)
                if decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
                    return None
                text = decoded.decode("utf-8")
            except (TypeError, UnicodeError, ValueError, OverflowError, zlib.error):
                return None
            fingerprint = fingerprint_text(text)
            if (
                len(text) != text_chars
                or version["text_xxh3_128"] is None
                or version["text_xxh3_64_guard"] is None
                or str(version["text_xxh3_128"]) != fingerprint.xxh3_128
                or str(version["text_xxh3_64_guard"]) != fingerprint.xxh3_64_guard
            ):
                return None
            canonical_bytes = text.encode("utf-8")
        elif version["text_zlib"] is not None:
            # ``_insert_version`` stores no compressed payload for an empty
            # representation; accepting an unexpected blob would hide a
            # damaged durable state behind a cache hit.
            return None

        rows = self.connection.execute(
            """SELECT c.chunk_id,c.version_id,c.chunk_index,c.start_byte,c.end_byte,
            f.current_path,
            COALESCE((SELECT p.name FROM project_memberships m
                JOIN projects p ON p.project_id=m.project_id
                WHERE m.version_id=c.version_id
                AND m.relation IN('under_manifest_root','inferred_root','manifest')
                ORDER BY CASE m.relation
                    WHEN 'under_manifest_root' THEN 0
                    WHEN 'inferred_root' THEN 1 ELSE 2 END,
                    m.project_id LIMIT 1),'') AS project,
            v.language,COALESCE(s.qualified_name,'') AS symbol,
            COALESCE(s.signature,'') AS signature,c.text,c.text_xxh3_128
            FROM code_chunks c
            JOIN file_versions v ON v.version_id=c.version_id
            JOIN files f ON f.current_version_id=v.version_id AND f.status='current'
            LEFT JOIN symbols s ON s.symbol_id=c.symbol_id
                AND s.version_id=c.version_id
            WHERE c.version_id=? AND v.invalidated_ns IS NULL
            ORDER BY c.chunk_index,c.chunk_id""",
            (version_id,),
        ).fetchall()
        contract = _cached_chunk_contract(version["provenance_json"])
        if contract is not None and (contract[0] < 0 or contract[0] != len(rows)):
            return None
        if contract is None:
            validate_coverage = True
        if validate_coverage and text_chars and not rows:
            return None

        expected: list[tuple[int, int, str, str, str | None, str, str, str]] = []
        cursor = 0
        for row in rows:
            try:
                text = str(row["text"])
                if str(row["text_xxh3_128"]) != fingerprint_text(text).xxh3_128:
                    return None
                chunk_index = int(row["chunk_index"])
                start_byte = int(row["start_byte"])
                end_byte = int(row["end_byte"])
                if validate_coverage:
                    encoded = text.encode("utf-8")
                    if (
                        chunk_index != len(expected)
                        or start_byte != cursor
                        or end_byte < start_byte
                        or end_byte > len(canonical_bytes)
                        or encoded != canonical_bytes[start_byte:end_byte]
                    ):
                        return None
                    cursor = end_byte
                language = None if row["language"] is None else str(row["language"])
                expected.append(
                    (
                        int(row["chunk_id"]),
                        int(row["version_id"]),
                        str(row["current_path"]),
                        str(row["project"]),
                        language,
                        str(row["symbol"]),
                        str(row["signature"]),
                        text,
                    )
                )
            except (TypeError, ValueError, OverflowError):
                return None
        if validate_coverage and cursor != len(canonical_bytes):
            return None
        return tuple(expected)

    def _repair_cached_fts(self, version_id: int) -> int | None:
        """Rebuild one Code FTS projection from valid durable chunks.

        The operation is intentionally scoped to one immutable version and is
        part of the caller's writer transaction.  No source bytes, analyzer or
        execution-capable tooling is involved.
        """

        expected = self._cached_code_fts_rows(version_id, validate_coverage=False)
        if expected is None:
            # Re-run the canonical extent proof when the durable chunk
            # contract itself is missing or inconsistent.  This keeps the
            # fallback explicitly tied to the full representation rather than
            # accepting an unproven remainder as a cache result.
            self._cached_code_fts_rows(version_id, validate_coverage=True)
            return None
        predicate, parameters = self._fts_lookup.predicate(version_id)
        actual_rows = self.connection.execute(
            f"""SELECT chunk_id,version_id,path,project,language,symbol,signature,body
            FROM code_fts WHERE {predicate}""",
            parameters,
        ).fetchall()
        actual_by_chunk: dict[int, list[tuple[object, ...]]] = {}
        malformed = False
        for row in actual_rows:
            try:
                chunk_id = int(row["chunk_id"])
                actual = (
                    chunk_id,
                    int(row["version_id"]),
                    str(row["path"]),
                    str(row["project"]),
                    None if row["language"] is None else str(row["language"]),
                    str(row["symbol"]),
                    str(row["signature"]),
                    str(row["body"]),
                )
            except (TypeError, ValueError, OverflowError):
                malformed = True
                continue
            actual_by_chunk.setdefault(chunk_id, []).append(actual)
        expected_by_chunk = {row[0]: row for row in expected}
        complete = not malformed and len(actual_rows) == len(expected) and all(
            actual_by_chunk.get(chunk_id) == [expected_row]
            for chunk_id, expected_row in expected_by_chunk.items()
        )
        if complete:
            # An empty FTS projection can mask a completely missing chunk set;
            # prove the canonical extent in that case before accepting it.
            if expected or actual_rows:
                return 0
            complete_expected = self._cached_code_fts_rows(
                version_id,
                validate_coverage=True,
            )
            return 0 if complete_expected == () else None

        expected = self._cached_code_fts_rows(version_id, validate_coverage=True)
        if expected is None:
            return None

        self._fts_lookup.delete(predicate, parameters)
        for expected_row in expected:
            cursor = self.connection.execute(
                """INSERT INTO code_fts(
                chunk_id,version_id,path,project,language,symbol,signature,body)
                VALUES(?,?,?,?,?,?,?,?)""",
                expected_row,
            )
            self._fts_lookup.record(_lastrowid(cursor), expected_row[1], expected_row[0])
        self._fts_lookup.acknowledge()
        return max(len(actual_rows), len(expected))

    def _cached_error_is_retryable(self, version_id: int, provenance_json: object) -> bool:
        """Read an explicit retry marker from durable structured error evidence."""

        try:
            provenance = json.loads(str(provenance_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            provenance = None
        if _explicit_retryable_marker(provenance):
            return True
        for row in self.connection.execute(
            "SELECT metadata_json FROM diagnostics WHERE version_id=?",
            (version_id,),
        ):
            try:
                metadata = json.loads(str(row[0]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if _explicit_retryable_marker(metadata):
                return True
        return False

    def reuse_cached(
        self,
        snapshot: FileSnapshot,
        processing_signature: str,
        framework_run_id: int,
        *,
        retry_errors: bool,
        retry_recoverable_errors: bool = False,
        raw_xxh3_128: str | None = None,
        raw_xxh3_64_guard: str | None = None,
        resolve_analyzer_identity: (Callable[[str | None, bool], tuple[str, str]] | None) = None,
        commit: bool = True,
        elapsed_nanoseconds: dict[str, int] | None = None,
    ) -> CachedCodeVersion | None:
        """Reuse one current observation under metadata or explicit full validation.

        Supplying an XXH3 pair selects full validation.  Omitting both values is
        the intentionally faster metadata-only strategy chosen by the caller.
        """

        _validate_optional_raw_fingerprint(raw_xxh3_128, raw_xxh3_64_guard)
        self._fts_lookup.invalidate_if_changed()
        volume_id, physical_file_id = _identity(snapshot)
        lookup_started = time.perf_counter_ns()
        row = self.connection.execute(
            """SELECT f.file_id,f.current_path,v.version_id,v.analysis_status,
            v.generated,v.vendored,v.size,v.mtime_ns,v.birthtime_ns,v.language,
            v.analyzer_id,v.analyzer_version,v.text_truncated,
            v.processing_signature,v.raw_xxh3_128,v.raw_xxh3_64_guard,
            v.provenance_json
            FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
            WHERE f.volume_id=? AND f.physical_file_id=? AND f.status='current'
            AND v.invalidated_ns IS NULL""",
            (volume_id, physical_file_id),
        ).fetchone()
        if elapsed_nanoseconds is not None:
            elapsed_nanoseconds["cache_lookup"] = elapsed_nanoseconds.get("cache_lookup", 0) + (
                time.perf_counter_ns() - lookup_started
            )
        if row is None:
            return None
        if str(row["current_path"]) != snapshot.path:
            # A path is a graph input: manifests, inferred roots and project
            # memberships are version-owned.  Publish a successor version
            # instead of mutating only the files/FTS projection in place.
            return None
        if (
            int(row["size"]) != snapshot.size
            or int(row["mtime_ns"]) != snapshot.mtime_ns
            or int(row["birthtime_ns"]) != snapshot.birthtime_ns
            or str(row["processing_signature"]) != processing_signature
        ):
            return None
        if raw_xxh3_128 is not None and (
            row["raw_xxh3_128"] is None
            or row["raw_xxh3_64_guard"] is None
            or str(row["raw_xxh3_128"]) != raw_xxh3_128
            or str(row["raw_xxh3_64_guard"]) != raw_xxh3_64_guard
        ):
            return None
        if (
            resolve_analyzer_identity is not None
            and str(row["analyzer_id"]) != "neocortex-code-route"
        ):
            language = None if row["language"] is None else str(row["language"])
            try:
                expected_analyzer = resolve_analyzer_identity(
                    language,
                    bool(row["text_truncated"]),
                )
            except (ImportError, AttributeError, RuntimeError):
                return None
            if expected_analyzer != (
                str(row["analyzer_id"]),
                str(row["analyzer_version"]),
            ):
                return None
        version_id = int(row["version_id"])
        status = str(row["analysis_status"])
        if retry_errors and status in {AnalysisStatus.ERROR, AnalysisStatus.PARTIAL}:
            return None
        if (
            retry_recoverable_errors
            and status == AnalysisStatus.ERROR
            and self._cached_error_is_retryable(version_id, row["provenance_json"])
        ):
            return None
        counts_started = time.perf_counter_ns()
        symbol_count, reference_count, diagnostic_count = self._version_counts(version_id)
        if elapsed_nanoseconds is not None:
            elapsed_nanoseconds["cache_lookup"] += time.perf_counter_ns() - counts_started
        fts_rows_repaired = self._repair_cached_fts(version_id)
        if fts_rows_repaired is None:
            return None
        update_started = time.perf_counter_ns()

        def update_observation() -> None:
            self._release_path_owner(
                snapshot.path,
                volume_id,
                physical_file_id,
                framework_run_id,
            )
            self.connection.execute(
                """UPDATE files SET current_path=?,last_seen_run_id=?,status='current'
                WHERE file_id=?""",
                (snapshot.path, framework_run_id, int(row["file_id"])),
            )
            self.connection.execute(
                """UPDATE file_versions SET last_observed_run_id=?
                WHERE version_id=?""",
                (framework_run_id, version_id),
            )

        if commit:
            with self.connection:
                update_observation()
        else:
            update_observation()
        if elapsed_nanoseconds is not None:
            elapsed_nanoseconds["cache_update"] = elapsed_nanoseconds.get("cache_update", 0) + (
                time.perf_counter_ns() - update_started
            )
        self._fts_lookup.acknowledge()
        return CachedCodeVersion(
            version_id,
            status,
            bool(row["generated"]),
            bool(row["vendored"]),
            symbol_count,
            reference_count,
            diagnostic_count,
            fts_rows_repaired,
        )

    # endregion [02]

    # region [03] Atomic version publication

    def _release_path_owner(
        self,
        path: str,
        volume_id: str,
        physical_file_id: str,
        framework_run_id: int,
    ) -> tuple[int, ...]:
        rows = self.connection.execute(
            f"""SELECT file_id,current_version_id FROM files
            WHERE current_path=? COLLATE {_PATH_COLLATION} AND status='current'
            AND NOT(volume_id=? AND physical_file_id=?)""",
            (path, volume_id, physical_file_id),
        ).fetchall()
        now = time.time_ns()
        version_ids: list[int] = []
        for row in rows:
            file_id = int(row[0])
            version_id = None if row[1] is None else int(row[1])
            if version_id is not None:
                version_ids.append(version_id)
                self.connection.execute(
                    """UPDATE file_versions SET invalidated_ns=?,
                    invalidation_reason='path_reused_by_new_identity'
                    WHERE version_id=? AND invalidated_ns IS NULL""",
                    (now, version_id),
                )
                self.connection.execute(
                    """INSERT INTO invalidation_history(
                    version_id,invalidated_ns,reason,evidence_json)
                    VALUES(?,?,'path_reused_by_new_identity',?)""",
                    (
                        version_id,
                        now,
                        _json({"path": path, "run_id": framework_run_id}),
                    ),
                )
            self.connection.execute(
                "UPDATE files SET status='stale',current_version_id=NULL WHERE file_id=?",
                (file_id,),
            )
        return tuple(version_ids)

    def _claim_file(
        self,
        snapshot: FileSnapshot,
        framework_run_id: int,
    ) -> tuple[int, int | None, tuple[int, ...]]:
        volume_id, physical_file_id = _identity(snapshot)
        path_conflicts = self._release_path_owner(
            snapshot.path, volume_id, physical_file_id, framework_run_id
        )
        existing = self.connection.execute(
            """SELECT file_id,current_version_id FROM files
            WHERE volume_id=? AND physical_file_id=?""",
            (volume_id, physical_file_id),
        ).fetchone()
        if existing is None:
            cursor = self.connection.execute(
                """INSERT INTO files(volume_id,physical_file_id,current_path,status,
                first_seen_run_id,last_seen_run_id)
                VALUES(?,?,?,'current',?,?)""",
                (
                    volume_id,
                    physical_file_id,
                    snapshot.path,
                    framework_run_id,
                    framework_run_id,
                ),
            )
            return _lastrowid(cursor), None, path_conflicts
        file_id = int(existing[0])
        previous = None if existing[1] is None else int(existing[1])
        self.connection.execute(
            """UPDATE files SET current_path=?,status='current',last_seen_run_id=?
            WHERE file_id=?""",
            (snapshot.path, framework_run_id, file_id),
        )
        return file_id, previous, path_conflicts

    def _invalidate_previous(
        self,
        previous_version_id: int | None,
        framework_run_id: int,
    ) -> int | None:
        if previous_version_id is None:
            return None
        now = time.time_ns()
        changed = self.connection.execute(
            """UPDATE file_versions SET invalidated_ns=?,
            invalidation_reason='superseded_observation'
            WHERE version_id=? AND invalidated_ns IS NULL""",
            (now, previous_version_id),
        ).rowcount
        if changed:
            self.connection.execute(
                """UPDATE files SET current_version_id=NULL
                WHERE current_version_id=?""",
                (previous_version_id,),
            )
            return now
        return None

    def store_analysis(
        self,
        analysis: CodeAnalysis,
        framework_run_id: int,
    ) -> tuple[int, bool]:
        source = analysis.input
        self._fts_lookup.invalidate_if_changed()
        with self.connection:
            file_id, previous, path_conflicts = self._claim_file(source.snapshot, framework_run_id)
            invalidated_ns = self._invalidate_previous(previous, framework_run_id)
            version_id = self._insert_version(
                file_id=file_id,
                snapshot=source.snapshot,
                classification=source.classification,
                processing_signature=source.processing_signature,
                status=analysis.status,
                analyzer_id=analysis.analyzer_id,
                analyzer_version=analysis.analyzer_version,
                parser_kind=analysis.parser_kind,
                encoding=source.encoding,
                text=source.text,
                text_truncated=analysis.text_truncated,
                raw_xxh3_128=analysis.raw_xxh3_128,
                raw_xxh3_64_guard=analysis.raw_xxh3_64_guard,
                text_xxh3_128=analysis.text_xxh3_128,
                text_xxh3_64_guard=analysis.text_xxh3_64_guard,
                normalized_xxh3_128=analysis.normalized_xxh3_128,
                token_xxh3_128=analysis.token_xxh3_128,
                structure_xxh3_128=analysis.structure_xxh3_128,
                provenance=_with_chunk_contract(
                    analysis.provenance,
                    len(analysis.chunks),
                    _CODE_CHUNKING_ALGORITHM,
                ),
                framework_run_id=framework_run_id,
            )
            self.connection.execute(
                "UPDATE files SET current_version_id=? WHERE file_id=?",
                (version_id, file_id),
            )
            if previous is not None and invalidated_ns is not None:
                self._record_replacement(previous, version_id, invalidated_ns, framework_run_id)
                self._insert_relation(
                    previous,
                    version_id,
                    "predecessor",
                    1.0,
                    {"physical_identity": True},
                )
            for conflict in path_conflicts:
                self._insert_relation(
                    conflict,
                    version_id,
                    "divergent_same_name",
                    1.0,
                    {"path": source.snapshot.path, "identity_replaced": True},
                )
            symbol_ids = self._insert_symbols(version_id, analysis)
            self._insert_references(version_id, analysis, symbol_ids)
            self._insert_dependencies(version_id, analysis)
            self._insert_diagnostics(version_id, analysis.diagnostics)
            self._insert_metrics(version_id, analysis, symbol_ids)
            self._insert_chunks(version_id, analysis, symbol_ids)
            self._insert_project_hints(version_id, analysis, framework_run_id)
        self._cache_version_counts(
            version_id,
            symbols=len(analysis.symbols),
            references=len(analysis.references),
            diagnostics=len(analysis.diagnostics),
        )
        self._fts_lookup.acknowledge()
        return version_id, previous is not None

    def store_skipped(
        self,
        observation: SkippedCodeObservation,
        framework_run_id: int,
    ) -> tuple[int, bool]:
        self._fts_lookup.invalidate_if_changed()
        with self.connection:
            file_id, previous, path_conflicts = self._claim_file(
                observation.snapshot, framework_run_id
            )
            invalidated_ns = self._invalidate_previous(previous, framework_run_id)
            text_fingerprint = (
                fingerprint_text(observation.text_excerpt) if observation.text_excerpt else None
            )
            version_id = self._insert_version(
                file_id=file_id,
                snapshot=observation.snapshot,
                classification=observation.classification,
                processing_signature=observation.processing_signature,
                status=observation.status,
                analyzer_id=observation.analyzer_id,
                analyzer_version=observation.analyzer_version,
                parser_kind=observation.parser_kind,
                encoding=observation.encoding,
                text=observation.text_excerpt,
                text_truncated=observation.text_truncated,
                raw_xxh3_128=observation.raw_xxh3_128,
                raw_xxh3_64_guard=observation.raw_xxh3_64_guard,
                text_xxh3_128=(None if text_fingerprint is None else text_fingerprint.xxh3_128),
                text_xxh3_64_guard=(
                    None if text_fingerprint is None else text_fingerprint.xxh3_64_guard
                ),
                normalized_xxh3_128=None,
                token_xxh3_128=None,
                structure_xxh3_128=None,
                provenance=_with_chunk_contract(
                    observation.provenance or {},
                    int(bool(observation.text_excerpt)),
                    _CODE_EXCERPT_CHUNKING_ALGORITHM,
                ),
                framework_run_id=framework_run_id,
            )
            self.connection.execute(
                "UPDATE files SET current_version_id=? WHERE file_id=?",
                (version_id, file_id),
            )
            if previous is not None and invalidated_ns is not None:
                self._record_replacement(previous, version_id, invalidated_ns, framework_run_id)
                self._insert_relation(
                    previous,
                    version_id,
                    "predecessor",
                    1.0,
                    {"physical_identity": True},
                )
            for conflict in path_conflicts:
                self._insert_relation(
                    conflict,
                    version_id,
                    "divergent_same_name",
                    1.0,
                    {"path": observation.snapshot.path, "identity_replaced": True},
                )
            self._insert_diagnostics(version_id, (observation.diagnostic,))
            if observation.text_excerpt:
                cursor = self.connection.execute(
                    """INSERT INTO code_chunks(version_id,chunk_index,kind,start_line,
                    end_line,start_byte,end_byte,text,text_xxh3_128)
                    VALUES(?,0,'bounded_excerpt',1,1,0,?,?,?)""",
                    (
                        version_id,
                        len(observation.text_excerpt.encode("utf-8")),
                        observation.text_excerpt,
                        fingerprint_text(observation.text_excerpt).xxh3_128,
                    ),
                )
                chunk_id = _lastrowid(cursor)
                fts_cursor = self.connection.execute(
                    """INSERT INTO code_fts(chunk_id,version_id,path,project,language,
                    symbol,signature,body) VALUES(?,?,?,'',?,'','',?)""",
                    (
                        chunk_id,
                        version_id,
                        observation.snapshot.path,
                        observation.classification.language,
                        observation.text_excerpt,
                    ),
                )
                self._fts_lookup.record(_lastrowid(fts_cursor), version_id, chunk_id)
        self._cache_version_counts(
            version_id,
            symbols=0,
            references=0,
            diagnostics=1,
        )
        self._fts_lookup.acknowledge()
        return version_id, previous is not None

    def _insert_version(
        self,
        *,
        file_id: int,
        snapshot: FileSnapshot,
        classification: ArtifactClassification,
        processing_signature: str,
        status: AnalysisStatus,
        analyzer_id: str,
        analyzer_version: str,
        parser_kind: str,
        encoding: str | None,
        text: str,
        text_truncated: bool,
        raw_xxh3_128: str | None,
        raw_xxh3_64_guard: str | None,
        text_xxh3_128: str | None,
        text_xxh3_64_guard: str | None,
        normalized_xxh3_128: str | None,
        token_xxh3_128: str | None,
        structure_xxh3_128: str | None,
        provenance: Mapping[str, object],
        framework_run_id: int,
    ) -> int:
        now = time.time_ns()
        text_payload = None if not text else zlib.compress(text.encode("utf-8"), 6)
        cursor = self.connection.execute(
            """INSERT INTO file_versions(
            file_id,path_observed,size,mtime_ns,birthtime_ns,raw_xxh3_128,
            raw_xxh3_64_guard,text_xxh3_128,text_xxh3_64_guard,
            normalized_xxh3_128,token_xxh3_128,structure_xxh3_128,encoding,
            language,artifact_kind,generated,vendored,classification_confidence,
            classification_evidence_json,analysis_status,processing_signature,
            analyzer_id,analyzer_version,parser_kind,text_zlib,text_chars,
            text_truncated,provenance_json,first_observed_run_id,
            last_observed_run_id,valid_from_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_id,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                raw_xxh3_128,
                raw_xxh3_64_guard,
                text_xxh3_128,
                text_xxh3_64_guard,
                normalized_xxh3_128,
                token_xxh3_128,
                structure_xxh3_128,
                encoding,
                classification.language,
                classification.artifact_kind.value,
                int(classification.generated),
                int(classification.vendored),
                classification.confidence,
                _json(classification.evidence),
                status.value,
                processing_signature,
                analyzer_id,
                analyzer_version,
                parser_kind,
                text_payload,
                len(text),
                int(text_truncated),
                _json(provenance),
                framework_run_id,
                framework_run_id,
                now,
            ),
        )
        return _lastrowid(cursor)

    def _record_replacement(
        self,
        old_version_id: int,
        new_version_id: int,
        invalidated_ns: int,
        framework_run_id: int,
    ) -> None:
        self.connection.execute(
            """INSERT INTO invalidation_history(
            version_id,invalidated_ns,reason,replacement_version_id,evidence_json)
            VALUES(?,?,'superseded_observation',?,?)""",
            (
                old_version_id,
                invalidated_ns,
                new_version_id,
                _json({"run_id": framework_run_id}),
            ),
        )

    def _insert_relation(
        self,
        left: int,
        right: int,
        kind: str,
        confidence: float,
        evidence: Mapping[str, object],
    ) -> None:
        if left == right:
            return
        first, second = sorted((left, right))
        self.connection.execute(
            """INSERT OR IGNORE INTO version_relations(
            left_version_id,right_version_id,relation_kind,confidence,
            evidence_json,created_ns) VALUES(?,?,?,?,?,?)""",
            (first, second, kind, confidence, _json(evidence), time.time_ns()),
        )

    # endregion [03]

    # region [04] Structured child rows

    def _insert_symbols(
        self,
        version_id: int,
        analysis: CodeAnalysis,
    ) -> dict[str, int]:
        identifiers: dict[str, int] = {}
        parent_updates: list[tuple[str, int]] = []
        for symbol in analysis.symbols:
            source_range = symbol.source_range
            cursor = self.connection.execute(
                """INSERT INTO symbols(
                version_id,kind,name,qualified_name,signature,visibility,docstring,
                confirmed,complexity,start_line,start_column,end_line,end_column,
                start_byte,end_byte,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    symbol.kind,
                    symbol.name,
                    symbol.qualified_name,
                    symbol.signature,
                    symbol.visibility,
                    None if symbol.docstring is None else symbol.docstring[:65_536],
                    int(symbol.confirmed),
                    symbol.complexity,
                    source_range.start_line,
                    source_range.start_column,
                    source_range.end_line,
                    source_range.end_column,
                    source_range.start_byte,
                    source_range.end_byte,
                    _json(symbol.metadata),
                ),
            )
            symbol_id = _lastrowid(cursor)
            identifiers.setdefault(symbol.qualified_name, symbol_id)
            if symbol.parent_qualified_name:
                parent_updates.append((symbol.parent_qualified_name, symbol_id))
        self.connection.executemany(
            "UPDATE symbols SET parent_symbol_id=? WHERE symbol_id=?",
            (
                (identifiers[parent_name], symbol_id)
                for parent_name, symbol_id in parent_updates
                if parent_name in identifiers
            ),
        )
        return identifiers

    def _insert_references(
        self,
        version_id: int,
        analysis: CodeAnalysis,
        symbol_ids: Mapping[str, int],
    ) -> None:
        self.connection.executemany(
            """INSERT INTO code_references(
            version_id,source_symbol_id,kind,name,target_hint,confirmed,confidence,
            evidence,start_line,start_column,end_line,end_column,start_byte,end_byte)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    version_id,
                    symbol_ids.get(reference.source_qualified_name or ""),
                    reference.kind,
                    reference.name,
                    reference.target_hint,
                    int(reference.confirmed),
                    reference.confidence,
                    reference.evidence,
                    *_range_values(reference.source_range),
                )
                for reference in analysis.references
            ),
        )

    def _insert_dependencies(self, version_id: int, analysis: CodeAnalysis) -> None:
        self.connection.executemany(
            """INSERT INTO dependencies(
            version_id,name,kind,scope,version_spec,confirmed,confidence,evidence,
            start_line,start_column,end_line,end_column,start_byte,end_byte)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    version_id,
                    dependency.name,
                    dependency.kind,
                    dependency.scope,
                    dependency.version_spec,
                    int(dependency.confirmed),
                    dependency.confidence,
                    dependency.evidence,
                    *_range_values(dependency.source_range),
                )
                for dependency in analysis.dependencies
            ),
        )

    def _insert_diagnostics(
        self,
        version_id: int,
        diagnostics: Iterable[DiagnosticRecord],
    ) -> None:
        self.connection.executemany(
            """INSERT INTO diagnostics(
            version_id,source,code,severity,message,tool_name,tool_version,
            confirmed,confidence,start_line,start_column,end_line,end_column,
            start_byte,end_byte,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    version_id,
                    diagnostic.source,
                    diagnostic.code,
                    diagnostic.severity.value,
                    diagnostic.message[:8192],
                    diagnostic.tool_name,
                    diagnostic.tool_version,
                    int(diagnostic.confirmed),
                    diagnostic.confidence,
                    *_range_values(diagnostic.source_range),
                    _json(diagnostic.metadata),
                )
                for diagnostic in diagnostics
            ),
        )

    def _insert_metrics(
        self,
        version_id: int,
        analysis: CodeAnalysis,
        symbol_ids: Mapping[str, int],
    ) -> None:
        self.connection.executemany(
            """INSERT OR REPLACE INTO metrics(
            version_id,symbol_id,name,value,confirmed,provenance)
            VALUES(?,?,?,?,?,?)""",
            (
                (
                    version_id,
                    symbol_ids.get(metric.symbol_qualified_name or ""),
                    metric.name,
                    metric.value,
                    int(metric.confirmed),
                    metric.provenance,
                )
                for metric in analysis.metrics
            ),
        )

    def _insert_chunks(
        self,
        version_id: int,
        analysis: CodeAnalysis,
        symbol_ids: Mapping[str, int],
    ) -> None:
        signature_by_symbol = {
            symbol.qualified_name: symbol.signature for symbol in analysis.symbols
        }
        for chunk in analysis.chunks:
            owner_id = symbol_ids.get(chunk.symbol_qualified_name or "")
            chunk_fingerprint = fingerprint_text(chunk.text).xxh3_128
            cursor = self.connection.execute(
                """INSERT INTO code_chunks(
                version_id,symbol_id,chunk_index,kind,start_line,end_line,start_byte,
                end_byte,text,text_xxh3_128) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    owner_id,
                    chunk.index,
                    chunk.kind,
                    chunk.source_range.start_line,
                    chunk.source_range.end_line,
                    chunk.source_range.start_byte,
                    chunk.source_range.end_byte,
                    chunk.text,
                    chunk_fingerprint,
                ),
            )
            chunk_id = _lastrowid(cursor)
            fts_cursor = self.connection.execute(
                """INSERT INTO code_fts(
                chunk_id,version_id,path,project,language,symbol,signature,body)
                VALUES(?,?,?,'',?,?,?,?)""",
                (
                    chunk_id,
                    version_id,
                    analysis.input.snapshot.path,
                    analysis.input.classification.language,
                    chunk.symbol_qualified_name or "",
                    signature_by_symbol.get(chunk.symbol_qualified_name or "") or "",
                    chunk.text,
                ),
            )
            self._fts_lookup.record(_lastrowid(fts_cursor), version_id, chunk_id)

    def _insert_project_hints(
        self,
        version_id: int,
        analysis: CodeAnalysis,
        framework_run_id: int,
    ) -> None:
        path = Path(analysis.input.snapshot.path)
        for hint in analysis.project_hints:
            root = os.path.abspath(os.path.normpath(hint.root_hint))
            family_key, project_key = _project_identity_keys(
                hint.ecosystem,
                hint.name,
                root,
            )
            self.connection.execute(
                """INSERT INTO projects(
                project_key,name,ecosystem,probable_root,manifest_kind,confidence,
                evidence_json,first_seen_run_id,last_seen_run_id,status)
                VALUES(?,?,?,?,?,?,?,?,?,'current')
                ON CONFLICT(project_key) DO UPDATE SET
                last_seen_run_id=excluded.last_seen_run_id,status='current',
                confidence=MAX(projects.confidence,excluded.confidence),
                evidence_json=CASE WHEN excluded.confidence>=projects.confidence
                    THEN excluded.evidence_json ELSE projects.evidence_json END""",
                (
                    project_key,
                    hint.name,
                    hint.ecosystem,
                    root,
                    hint.manifest_kind,
                    hint.confidence,
                    _json(
                        {
                            "family_key": family_key,
                            "instance_root_key": _normalized_project_root(root),
                            "evidence": hint.evidence,
                            "metadata": hint.metadata,
                        }
                    ),
                    framework_run_id,
                    framework_run_id,
                ),
            )
            project_id = int(
                self.connection.execute(
                    "SELECT project_id FROM projects WHERE project_key=?",
                    (project_key,),
                ).fetchone()[0]
            )
            try:
                proposed = str(path.relative_to(Path(root))).replace("\\", "/")
            except ValueError:
                proposed = path.name
            self.connection.execute(
                """INSERT INTO project_memberships(
                project_id,version_id,proposed_path,relation,confidence,selected,
                evidence_json) VALUES(?,?,?,'manifest',?,1,?)
                ON CONFLICT(project_id,version_id) DO UPDATE SET
                proposed_path=excluded.proposed_path,relation='manifest',
                confidence=excluded.confidence,selected=1,
                evidence_json=excluded.evidence_json""",
                (
                    project_id,
                    version_id,
                    proposed,
                    hint.confidence,
                    _json({"manifest_kind": hint.manifest_kind, "evidence": hint.evidence}),
                ),
            )
            self.connection.execute(
                "UPDATE code_fts SET project=? WHERE version_id=?",
                (hint.name, version_id),
            )

    # endregion [04]

    # region [05] Graph resolution, lineage and generation reconciliation

    def finalize_graph(
        self,
        framework_run_id: int,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> int:
        """Resolve current graph evidence using set-oriented SQLite operations."""

        cancellation = SQLiteCancellationBridge(cancellation_check)
        cancellation.checkpoint()
        with sqlite_cancellation_scope(self.connection, cancellation), self.connection:
            self._reset_current_derived_graph()
            self._assign_manifest_roots(framework_run_id)
            self._infer_incomplete_projects(framework_run_id)
            self._resolve_symbols_and_dependencies()
            self._record_duplicate_relations("raw_xxh3_128", "exact_duplicate", 1.0)
            self._record_duplicate_relations("normalized_xxh3_128", "normalized_duplicate", 0.9)
            self._resolve_membership_conflicts()
            self._synchronize_current_fts_projects()
            self._reconcile_project_statuses()
            self._record_project_edges()
            self._record_probable_dead_symbols()
            row = self.connection.execute(
                "SELECT COUNT(*) FROM projects WHERE status='current'"
            ).fetchone()
            cancellation.checkpoint()
        return int(row[0])

    def _reset_current_derived_graph(self) -> None:
        """Discard only rebuildable facts for current versions.

        Parser-owned manifest memberships and every invalidated-version row are
        historical evidence.  Derived current memberships are rebuilt so a
        removed, renamed, or changed manifest cannot keep a source selected
        under an obsolete project.  FTS labels are reconciled once after all
        memberships and conflicts have reached their final state.
        """

        self.connection.execute(
            """DELETE FROM project_memberships
            WHERE relation IN ('under_manifest_root','inferred_root')
            AND version_id IN(
                SELECT version_id FROM file_versions WHERE invalidated_ns IS NULL)"""
        )
        self.connection.execute(
            """UPDATE projects SET status='current'
            WHERE EXISTS(SELECT 1 FROM project_memberships m
                JOIN file_versions v ON v.version_id=m.version_id
                WHERE m.project_id=projects.project_id AND m.relation='manifest'
                AND v.invalidated_ns IS NULL)"""
        )

    def _manifest_roots(self) -> tuple[tuple[int, str, str, str], ...]:
        rows = self.connection.execute(
            """SELECT DISTINCT p.project_id,p.name,p.ecosystem,v.path_observed
            FROM projects p JOIN project_memberships m ON m.project_id=p.project_id
            JOIN file_versions v ON v.version_id=m.version_id
            WHERE p.status='current' AND m.relation='manifest'
            AND v.invalidated_ns IS NULL"""
        ).fetchall()
        return tuple(
            (int(row[0]), str(row[1]), str(row[2]), str(Path(str(row[3])).parent)) for row in rows
        )

    def _assign_manifest_roots(self, framework_run_id: int) -> None:
        roots = self._manifest_roots()
        if not roots:
            return
        roots_by_path: dict[str, list[tuple[int, str, str, str]]] = {}
        for item in roots:
            normalized_root = os.path.normcase(os.path.abspath(item[3]))
            # commonpath collapses a POSIX double leading slash; such a root
            # never matched the former equality test. Keep that exact rule.
            if os.path.commonpath((normalized_root, normalized_root)) != normalized_root:
                continue
            roots_by_path.setdefault(normalized_root, []).append(item)
        rows = self.connection.execute(
            """SELECT v.version_id,f.current_path FROM files f
            JOIN file_versions v ON v.version_id=f.current_version_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
            ORDER BY f.current_path"""
        )
        for row in rows:
            version_id = int(row[0])
            path = str(row[1])
            normalized_path = os.path.normcase(os.path.abspath(path))
            candidate = os.path.commonpath((normalized_path, normalized_path))
            matches = roots_by_path.get(candidate)
            while matches is None:
                parent = os.path.dirname(candidate)
                if parent == candidate:
                    break
                candidate = parent
                matches = roots_by_path.get(candidate)
            if matches is None:
                continue
            # The deepest ancestor is the longest normalized root. Within
            # one root, max(..., key=len) chose the first original candidate.
            project_id, _project_name, _ecosystem, root = matches[0]
            try:
                proposed = str(Path(path).relative_to(Path(root))).replace("\\", "/")
            except ValueError:
                proposed = Path(path).name
            self.connection.execute(
                """INSERT OR IGNORE INTO project_memberships(
                project_id,version_id,proposed_path,relation,confidence,selected,
                evidence_json) VALUES(?,?,?,'under_manifest_root',0.98,1,?)""",
                (
                    project_id,
                    version_id,
                    proposed,
                    _json({"root": root, "run_id": framework_run_id}),
                ),
            )

    def _infer_incomplete_projects(self, framework_run_id: int) -> None:
        rows = self.connection.execute(
            """SELECT v.version_id,f.current_path,v.language FROM files f
            JOIN file_versions v ON v.version_id=f.current_version_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
            AND v.artifact_kind IN ('source','script','example','fixture')
            AND NOT EXISTS(SELECT 1 FROM project_memberships m
                WHERE m.version_id=v.version_id)
            ORDER BY f.current_path"""
        )
        for row in rows:
            version_id = int(row[0])
            path = Path(str(row[1]))
            language = str(row[2] or "unknown")
            root = path.parent
            for parent in path.parents:
                if parent.name.casefold() in {"src", "lib", "app", "tests", "test"}:
                    root = parent.parent
                    break
            project_name = root.name or path.parent.name or "unknown-project"
            family_key, project_key = _project_identity_keys(
                language,
                project_name,
                str(root),
            )
            self.connection.execute(
                """INSERT INTO projects(
                project_key,name,ecosystem,probable_root,confidence,evidence_json,
                first_seen_run_id,last_seen_run_id,status)
                VALUES(?,?,?,?,0.55,?,?,?,'ambiguous')
                ON CONFLICT(project_key) DO UPDATE SET
                last_seen_run_id=excluded.last_seen_run_id,status='ambiguous'""",
                (
                    project_key,
                    project_name,
                    language,
                    str(root),
                    _json(
                        {
                            "family_key": family_key,
                            "instance_root_key": _normalized_project_root(str(root)),
                            "evidence": "nearest-source-root",
                            "path": str(path),
                        }
                    ),
                    framework_run_id,
                    framework_run_id,
                ),
            )
            project_id = int(
                self.connection.execute(
                    "SELECT project_id FROM projects WHERE project_key=?",
                    (project_key,),
                ).fetchone()[0]
            )
            try:
                proposed = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                proposed = path.name
            self.connection.execute(
                """INSERT OR IGNORE INTO project_memberships(
                project_id,version_id,proposed_path,relation,confidence,selected,
                evidence_json) VALUES(?,?,?,'inferred_root',0.55,1,?)""",
                (
                    project_id,
                    version_id,
                    proposed,
                    _json({"root": str(root), "method": "nearest-source-root"}),
                ),
            )

    def _synchronize_current_fts_projects(self) -> None:
        """Reconcile current FTS project labels with one virtual-table scan.

        The indexed temporary map makes membership resolution independent of
        the unindexed ``code_fts.version_id`` column.  The final label prefers
        ``under_manifest_root``, then ``inferred_root``, then the parser-owned
        ``manifest`` membership used by manifest files themselves.
        Conflict selection does not erase that label, matching the previous
        resolver behavior when every conflicting membership had ``selected=0``.
        """

        table = "_nc_fts_project_map"
        self.connection.execute(f"DROP TABLE IF EXISTS temp.{table}")
        try:
            self.connection.execute(
                f"""CREATE TEMP TABLE {table}(
                version_id INTEGER PRIMARY KEY,
                project TEXT NOT NULL) WITHOUT ROWID"""
            )
            self.connection.execute(
                f"""INSERT INTO {table}(version_id,project)
                SELECT v.version_id,COALESCE((
                    SELECT p.name FROM project_memberships m
                    JOIN projects p ON p.project_id=m.project_id
                    WHERE m.version_id=v.version_id
                    AND m.relation IN(
                        'under_manifest_root','inferred_root','manifest')
                    ORDER BY CASE m.relation
                        WHEN 'under_manifest_root' THEN 0
                        WHEN 'inferred_root' THEN 1 ELSE 2 END,
                        m.project_id
                    LIMIT 1
                ),'')
                FROM file_versions v WHERE v.invalidated_ns IS NULL"""
            )
            self.connection.execute(
                f"""UPDATE code_fts AS fts
                SET project=(SELECT map.project FROM {table} AS map
                    WHERE map.version_id=fts.version_id)
                WHERE EXISTS(SELECT 1 FROM {table} AS map
                    WHERE map.version_id=fts.version_id
                    AND map.project<>fts.project)"""
            )
        finally:
            self.connection.execute(f"DROP TABLE IF EXISTS temp.{table}")

    def _reconcile_project_statuses(self) -> None:
        self.connection.execute(
            """UPDATE projects SET status=CASE
            WHEN EXISTS(SELECT 1 FROM project_memberships m
                JOIN file_versions v ON v.version_id=m.version_id
                WHERE m.project_id=projects.project_id AND m.relation='manifest'
                AND v.invalidated_ns IS NULL) THEN 'current'
            WHEN EXISTS(SELECT 1 FROM project_memberships m
                JOIN file_versions v ON v.version_id=m.version_id
                WHERE m.project_id=projects.project_id
                AND v.invalidated_ns IS NULL) THEN 'ambiguous'
            ELSE 'historical' END"""
        )

    def _create_symbol_resolution_lookups(self) -> None:
        """Build bounded current-symbol, module and submodule lookup tables."""

        self.connection.execute(
            """CREATE TEMP TABLE _nc_symbol_lookup(
            lookup_kind TEXT NOT NULL,
            lookup_value TEXT NOT NULL,
            symbol_id INTEGER NOT NULL,
            match_count INTEGER NOT NULL,
            PRIMARY KEY(lookup_kind,lookup_value)) WITHOUT ROWID"""
        )
        self.connection.execute(
            """INSERT INTO _nc_symbol_lookup(
            lookup_kind,lookup_value,symbol_id,match_count)
            SELECT 'qualified',s.qualified_name,MIN(s.symbol_id),COUNT(*)
            FROM symbols s JOIN _nc_current_versions v
                ON v.version_id=s.version_id
            GROUP BY s.qualified_name"""
        )
        self.connection.execute(
            """INSERT INTO _nc_symbol_lookup(
            lookup_kind,lookup_value,symbol_id,match_count)
            SELECT 'name',s.name,MIN(s.symbol_id),COUNT(*)
            FROM symbols s JOIN _nc_current_versions v
                ON v.version_id=s.version_id
            GROUP BY s.name"""
        )
        self.connection.execute(
            """CREATE TEMP TABLE _nc_internal_module_lookup(
            name TEXT PRIMARY KEY,
            version_id INTEGER NOT NULL,
            match_count INTEGER NOT NULL) WITHOUT ROWID"""
        )
        internal_modules: dict[str, set[int]] = {}
        internal_submodules: dict[tuple[str, str], set[int]] = {}
        for version_id, current_path, module_name in self.connection.execute(
            """SELECT v.version_id,f.current_path,s.name
            FROM file_versions v JOIN files f ON f.current_version_id=v.version_id
            JOIN symbols s ON s.version_id=v.version_id AND s.kind='module'
            WHERE f.status='current' AND v.invalidated_ns IS NULL"""
        ):
            path = Path(str(current_path))
            names = {str(module_name)}
            if path.name.casefold() == "__init__.py":
                names.add(path.parent.name)
            else:
                internal_submodules.setdefault((path.parent.name, str(module_name)), set()).add(
                    int(version_id)
                )
            for name in names:
                if name:
                    internal_modules.setdefault(name, set()).add(int(version_id))
        for name, version_ids in sorted(internal_modules.items()):
            self.connection.execute(
                """INSERT INTO _nc_internal_module_lookup(
                name,version_id,match_count) VALUES(?,?,?)""",
                (name, min(version_ids), len(version_ids)),
            )
        self.connection.execute(
            """CREATE TEMP TABLE _nc_internal_submodule_lookup(
            package_name TEXT NOT NULL,
            name TEXT NOT NULL,
            version_id INTEGER NOT NULL,
            match_count INTEGER NOT NULL,
            PRIMARY KEY(package_name,name)) WITHOUT ROWID"""
        )
        for (package_name, name), version_ids in sorted(internal_submodules.items()):
            self.connection.execute(
                """INSERT INTO _nc_internal_submodule_lookup(
                package_name,name,version_id,match_count) VALUES(?,?,?,?)""",
                (package_name, name, min(version_ids), len(version_ids)),
            )

    def _resolve_scoped_reference_targets(self) -> None:
        """Resolve unambiguous targets that belong to the source module."""

        self.connection.execute(
            """CREATE TEMP TABLE _nc_reference_targets(
            reference_id INTEGER PRIMARY KEY,
            symbol_id INTEGER NOT NULL) WITHOUT ROWID"""
        )
        self.connection.execute(
            """CREATE TEMP TABLE _nc_scoped_reference_targets(
            reference_id INTEGER PRIMARY KEY,
            symbol_id INTEGER NOT NULL) WITHOUT ROWID"""
        )
        self.connection.execute(
            """CREATE TEMP TABLE _nc_scoped_reference_candidates(
            reference_id INTEGER NOT NULL,
            symbol_id INTEGER NOT NULL,
            PRIMARY KEY(reference_id,symbol_id)) WITHOUT ROWID"""
        )
        # Enumerate every dotted suffix, including empty trailing suffixes.
        # A symbol name may itself contain dots. Keep SQLite substr/NULL and
        # BINARY semantics, and retain the original predicate below as well.
        # Dense dotted names keep the former candidate scan rather than
        # materializing an unbounded quadratic collection of long suffixes.
        self.connection.execute(
            """INSERT OR IGNORE INTO _nc_scoped_reference_candidates(
            reference_id,symbol_id)
            WITH RECURSIVE eligible AS (
                SELECT r.reference_id,r.version_id,r.name,r.target_hint,r.evidence,
                    CASE WHEN length(r.name)-length(replace(r.name,'.',''))>64
                        OR length(r.target_hint)-length(replace(r.target_hint,'.',''))>64
                        THEN 1 ELSE 0 END AS dense_suffixes
                FROM code_references r JOIN _nc_current_versions current
                  ON current.version_id=r.version_id
                WHERE r.target_symbol_id IS NULL AND r.kind IN(
                    'call','inherits','implements_trait','decorator')
            ), nontext_targets AS MATERIALIZED (
                SELECT target.symbol_id,target.version_id FROM symbols target
                JOIN _nc_current_versions current
                  ON current.version_id=target.version_id
                WHERE typeof(target.name)!='text'
                  AND target.kind IN ('function','class','method')
            ), dense_eligible AS MATERIALIZED (
                SELECT reference_id,version_id
                FROM eligible WHERE dense_suffixes=1
            ), lookup_keys(reference_id,version_id,lookup_kind,lookup_value) AS (
                SELECT reference_id,version_id,'qualified',target_hint
                FROM eligible WHERE target_hint IS NOT NULL AND evidence IS NOT NULL
                UNION
                SELECT reference_id,version_id,'name',name FROM eligible
                WHERE evidence!='python-ast:call-expression-import-bound'
                  AND dense_suffixes=0
                UNION
                SELECT reference_id,version_id,'name',target_hint FROM eligible
                WHERE evidence!='python-ast:call-expression-import-bound'
                  AND target_hint IS NOT NULL AND dense_suffixes=0
                UNION
                SELECT reference_id,version_id,'name',
                    substr(lookup_value,instr(lookup_value,'.')+1)
                FROM lookup_keys WHERE lookup_kind='name'
                    AND instr(lookup_value,'.')>0
            )
            SELECT k.reference_id,target.symbol_id FROM lookup_keys k
            CROSS JOIN symbols target INDEXED BY symbols_name_idx
                ON target.name=k.lookup_value AND target.version_id=k.version_id
                AND target.kind IN ('function','class','method')
            WHERE k.lookup_kind='name'
            UNION ALL
            SELECT k.reference_id,target.symbol_id FROM lookup_keys k
            CROSS JOIN symbols target INDEXED BY symbols_qualified_idx
                ON target.qualified_name=k.lookup_value
                AND target.version_id=k.version_id
                AND target.kind IN ('function','class','method')
            WHERE k.lookup_kind='qualified'
            UNION ALL
            SELECT e.reference_id,target.symbol_id
            FROM nontext_targets target CROSS JOIN eligible e
            WHERE target.version_id=e.version_id
            UNION ALL
            SELECT e.reference_id,target.symbol_id FROM dense_eligible e
            CROSS JOIN symbols target ON target.version_id=e.version_id
                AND target.kind IN ('function','class','method')"""
        )
        self.connection.execute(
            """INSERT INTO _nc_scoped_reference_targets(
            reference_id,symbol_id)
            SELECT r.reference_id,MIN(target.symbol_id)
            FROM code_references r
            JOIN _nc_current_versions current
                ON current.version_id=r.version_id
            JOIN symbols source ON source.symbol_id=r.source_symbol_id
            JOIN symbols module ON module.version_id=r.version_id
                AND module.kind='module'
            JOIN _nc_scoped_reference_candidates candidate
                ON candidate.reference_id=r.reference_id
            CROSS JOIN symbols target
            LEFT JOIN symbols target_parent
                ON target_parent.symbol_id=target.parent_symbol_id
            WHERE r.target_symbol_id IS NULL AND r.kind IN(
                'call','inherits','implements_trait','decorator')
            AND target.symbol_id=candidate.symbol_id
            AND target.version_id=r.version_id
            AND (
                (r.evidence='python-ast:call-expression-import-bound'
                    AND r.target_hint=target.qualified_name)
                OR (
                    r.evidence!='python-ast:call-expression-import-bound'
                    AND (
                        r.name=target.name
                        OR substr(r.name,-(length(target.name)+1))
                            ='.'||target.name
                        OR r.target_hint=target.qualified_name
                        OR r.target_hint=target.name
                        OR substr(r.target_hint,-(length(target.name)+1))
                            ='.'||target.name)))
            AND (
                (target.kind IN ('function','class')
                    AND target.parent_symbol_id=module.symbol_id)
                OR (target.kind='method' AND (
                    (source.kind='class'
                        AND target.parent_symbol_id=source.symbol_id)
                    OR (source.kind IN ('method','nested_function')
                        AND target.parent_symbol_id=source.parent_symbol_id)
                    OR (target_parent.kind='class' AND (
                        r.name=target_parent.name||'.'||target.name
                        OR r.target_hint=target_parent.name||'.'||target.name
                    ))
                )))
            GROUP BY r.reference_id
            HAVING COUNT(DISTINCT target.symbol_id)=1"""
        )
        self.connection.execute(
            """INSERT INTO _nc_reference_targets(reference_id,symbol_id)
            SELECT reference_id,symbol_id
            FROM _nc_scoped_reference_targets"""
        )

    def _resolve_import_bound_reexports(self) -> None:
        """Follow one confirmed internal facade or package-submodule hop."""

        self.connection.execute(
            """CREATE TEMP TABLE _nc_reexport_reference_targets(
            reference_id INTEGER PRIMARY KEY,
            symbol_id INTEGER NOT NULL) WITHOUT ROWID"""
        )
        self.connection.execute(
            """WITH import_calls AS (
                SELECT r.reference_id,
                    substr(r.target_hint,1,instr(r.target_hint,'.')-1)
                        AS module_name,
                    substr(r.target_hint,instr(r.target_hint,'.')+1)
                        AS exported_path
                FROM code_references r JOIN _nc_current_versions v
                    ON v.version_id=r.version_id
                WHERE r.target_symbol_id IS NULL AND r.kind='call'
                AND r.evidence='python-ast:call-expression-import-bound'
                AND instr(r.target_hint,'.')>0
            ), export_parts AS (
                SELECT reference_id,module_name,exported_path,
                    CASE WHEN instr(exported_path,'.')=0
                        THEN exported_path
                        ELSE substr(exported_path,1,
                            instr(exported_path,'.')-1) END AS exported_name,
                    CASE WHEN instr(exported_path,'.')=0 THEN ''
                        ELSE substr(exported_path,
                            instr(exported_path,'.')) END AS member_suffix
                FROM import_calls
            )
            INSERT INTO _nc_reexport_reference_targets(reference_id,symbol_id)
            SELECT part.reference_id,MIN(q.symbol_id)
            FROM export_parts part
            JOIN _nc_internal_module_lookup module
                ON module.name=part.module_name AND module.match_count=1
            JOIN code_references binding ON binding.version_id=module.version_id
                AND binding.kind='import_binding'
                AND binding.name=part.exported_name
                AND binding.target_hint IS NOT NULL
            JOIN symbols binding_source
                ON binding_source.symbol_id=binding.source_symbol_id
                AND binding_source.kind='module'
            JOIN _nc_symbol_lookup q ON q.lookup_kind='qualified'
                AND q.lookup_value=binding.target_hint||part.member_suffix
                AND q.match_count=1
            GROUP BY part.reference_id
            HAVING COUNT(DISTINCT q.symbol_id)=1"""
        )
        self.connection.execute(
            """WITH import_calls AS (
                SELECT r.reference_id,
                    substr(r.target_hint,1,instr(r.target_hint,'.')-1)
                        AS package_name,
                    substr(r.target_hint,instr(r.target_hint,'.')+1)
                        AS exported_path
                FROM code_references r JOIN _nc_current_versions v
                    ON v.version_id=r.version_id
                WHERE r.target_symbol_id IS NULL AND r.kind='call'
                AND r.evidence='python-ast:call-expression-import-bound'
                AND instr(r.target_hint,'.')>0
            ), export_parts AS (
                SELECT reference_id,package_name,exported_path,
                    CASE WHEN instr(exported_path,'.')=0
                        THEN exported_path
                        ELSE substr(exported_path,1,
                            instr(exported_path,'.')-1) END AS exported_name,
                    CASE WHEN instr(exported_path,'.')=0 THEN ''
                        ELSE substr(exported_path,
                            instr(exported_path,'.')) END AS member_suffix
                FROM import_calls
            )
            INSERT INTO _nc_reexport_reference_targets(reference_id,symbol_id)
            SELECT part.reference_id,MIN(q.symbol_id)
            FROM export_parts part
            JOIN _nc_internal_submodule_lookup submodule
                ON submodule.package_name=part.package_name
                AND submodule.name=part.exported_name
                AND submodule.match_count=1
            JOIN _nc_symbol_lookup q ON q.lookup_kind='qualified'
                AND q.lookup_value=submodule.name||part.member_suffix
                AND q.match_count=1
            WHERE NOT EXISTS(
                SELECT 1 FROM _nc_reexport_reference_targets existing
                WHERE existing.reference_id=part.reference_id)
            GROUP BY part.reference_id
            HAVING COUNT(DISTINCT q.symbol_id)=1"""
        )
        self.connection.execute(
            """INSERT INTO _nc_reference_targets(reference_id,symbol_id)
            SELECT reexport.reference_id,reexport.symbol_id
            FROM _nc_reexport_reference_targets reexport
            WHERE NOT EXISTS(SELECT 1 FROM _nc_reference_targets existing
                WHERE existing.reference_id=reexport.reference_id)"""
        )

    def _resolve_remaining_reference_targets(self) -> None:
        """Apply the legacy exact qualified-or-unique-name resolver."""

        self.connection.execute(
            """INSERT INTO _nc_reference_targets(reference_id,symbol_id)
            SELECT r.reference_id,
                CASE
                WHEN q.match_count=1 AND n.match_count IS NULL
                    THEN q.symbol_id
                WHEN n.match_count=1 AND q.match_count IS NULL
                    THEN n.symbol_id
                ELSE q.symbol_id END
            FROM code_references r JOIN _nc_current_versions v
                ON v.version_id=r.version_id
            LEFT JOIN _nc_symbol_lookup q
                ON q.lookup_kind='qualified'
                AND q.lookup_value=r.target_hint
            LEFT JOIN _nc_symbol_lookup n
                ON n.lookup_kind='name' AND n.lookup_value=r.name
                AND r.evidence!='python-ast:call-expression-import-bound'
            WHERE r.target_symbol_id IS NULL AND r.kind IN(
                'call','inherits','implements_trait','decorator')
            AND NOT EXISTS(SELECT 1 FROM _nc_reference_targets scoped
                WHERE scoped.reference_id=r.reference_id)
            AND (
                (q.match_count=1 AND n.match_count IS NULL)
                OR (n.match_count=1 AND q.match_count IS NULL)
                OR (q.match_count=1 AND n.match_count=1
                    AND q.symbol_id=n.symbol_id))"""
        )
        self.connection.execute(
            """UPDATE code_references AS r SET target_symbol_id=t.symbol_id
            FROM _nc_reference_targets AS t
            WHERE r.reference_id=t.reference_id"""
        )
        self.connection.execute(
            """UPDATE code_references AS r SET target_version_id=s.version_id
            FROM symbols AS s WHERE s.symbol_id=r.target_symbol_id
            AND r.target_symbol_id IS NOT NULL"""
        )

    def _resolve_symbols_and_dependencies(self) -> None:
        # Target bindings are graph-derived rather than parser facts.  The
        # lookup tables aggregate the current symbol population once in SQLite
        # instead of rescanning it for every reference and dependency.  A
        # qualified-name hit and a short-name hit still form the exact OR-union
        # used by resolver v1: it resolves only when that union has one symbol.
        temporary_tables = (
            "_nc_unresolved_relative_versions",
            "_nc_relative_dependency_targets",
            "_nc_relative_dependency_candidates",
            "_nc_module_lookup",
            "_nc_internal_module_lookup",
            "_nc_internal_submodule_lookup",
            "_nc_reexport_reference_targets",
            "_nc_reference_targets",
            "_nc_scoped_reference_targets",
            "_nc_scoped_reference_candidates",
            "_nc_symbol_lookup",
            "_nc_current_versions",
        )
        for table in temporary_tables:
            self.connection.execute(f"DROP TABLE IF EXISTS temp.{table}")
        try:
            self.connection.execute(
                """CREATE TEMP TABLE _nc_current_versions(
                version_id INTEGER PRIMARY KEY) WITHOUT ROWID"""
            )
            self.connection.execute(
                """INSERT INTO _nc_current_versions(version_id)
                SELECT version_id FROM file_versions WHERE invalidated_ns IS NULL"""
            )

            # Sever current bindings and any historical binding whose target
            # is no longer current, preserving all other historical evidence.
            self.connection.execute(
                """UPDATE code_references
                SET target_symbol_id=NULL,target_version_id=NULL
                WHERE version_id IN(SELECT version_id FROM _nc_current_versions)
                OR target_version_id IN(
                    SELECT version_id FROM file_versions
                    WHERE invalidated_ns IS NOT NULL)
                OR target_symbol_id IN(
                    SELECT s.symbol_id FROM symbols s JOIN file_versions v
                        ON v.version_id=s.version_id
                    WHERE v.invalidated_ns IS NOT NULL)"""
            )
            self.connection.execute(
                """UPDATE dependencies SET resolved_version_id=NULL
                WHERE version_id IN(SELECT version_id FROM _nc_current_versions)
                OR resolved_version_id IN(
                    SELECT version_id FROM file_versions
                    WHERE invalidated_ns IS NOT NULL)"""
            )
            placeholders = ",".join("?" for _ in _DERIVED_DIAGNOSTIC_SOURCES)
            self.connection.execute(
                f"""DELETE FROM diagnostics WHERE source IN ({placeholders})
                AND version_id IN(SELECT version_id FROM _nc_current_versions)""",
                _DERIVED_DIAGNOSTIC_SOURCES,
            )
            self._create_symbol_resolution_lookups()
            self._resolve_scoped_reference_targets()
            self._resolve_import_bound_reexports()
            self._resolve_remaining_reference_targets()

            self.connection.execute(
                """CREATE TEMP TABLE _nc_module_lookup(
                name TEXT PRIMARY KEY,
                version_id INTEGER NOT NULL) WITHOUT ROWID"""
            )
            self.connection.execute(
                """INSERT INTO _nc_module_lookup(name,version_id)
                SELECT s.name,MIN(s.version_id) FROM symbols s
                JOIN _nc_current_versions v ON v.version_id=s.version_id
                WHERE s.kind='module' GROUP BY s.name HAVING COUNT(*)=1"""
            )
            self.connection.execute(
                """UPDATE dependencies AS d
                SET resolved_version_id=m.version_id
                FROM _nc_current_versions AS v, _nc_module_lookup AS m
                WHERE d.version_id=v.version_id AND d.name=m.name
                AND d.resolved_version_id IS NULL"""
            )

            self.connection.execute(
                f"""CREATE TEMP TABLE _nc_relative_dependency_candidates(
                dependency_id INTEGER NOT NULL,
                candidate_path TEXT NOT NULL COLLATE {_PATH_COLLATION},
                PRIMARY KEY(dependency_id,candidate_path)) WITHOUT ROWID"""
            )
            relative_dependencies = self.connection.execute(
                """SELECT d.dependency_id,f.current_path,d.name
                FROM dependencies d
                JOIN _nc_current_versions current
                    ON current.version_id=d.version_id
                JOIN file_versions v ON v.version_id=d.version_id
                JOIN files f ON f.file_id=v.file_id
                WHERE d.kind='python_relative_import'
                AND d.resolved_version_id IS NULL
                AND d.name GLOB '.*'
                ORDER BY d.dependency_id"""
            )
            while True:
                batch = relative_dependencies.fetchmany(CODE_STATE_WRITE_BATCH)
                if not batch:
                    break
                candidates = tuple(
                    (int(row[0]), candidate)
                    for row in batch
                    for candidate in _relative_import_candidate_paths(str(row[1]), str(row[2]))
                )
                if candidates:
                    self.connection.executemany(
                        """INSERT OR IGNORE INTO
                        _nc_relative_dependency_candidates(
                        dependency_id,candidate_path) VALUES(?,?)""",
                        candidates,
                    )
            self.connection.execute(
                """CREATE TEMP TABLE _nc_relative_dependency_targets(
                dependency_id INTEGER PRIMARY KEY,
                version_id INTEGER NOT NULL) WITHOUT ROWID"""
            )
            self.connection.execute(
                f"""INSERT INTO _nc_relative_dependency_targets(
                dependency_id,version_id)
                SELECT candidates.dependency_id,MIN(files.current_version_id)
                FROM _nc_relative_dependency_candidates candidates
                JOIN files ON files.current_path=candidates.candidate_path
                    COLLATE {_PATH_COLLATION} AND files.status='current'
                JOIN _nc_current_versions current
                    ON current.version_id=files.current_version_id
                GROUP BY candidates.dependency_id
                HAVING COUNT(DISTINCT files.current_version_id)=1"""
            )
            self.connection.execute(
                """UPDATE dependencies AS dependency
                SET resolved_version_id=target.version_id
                FROM _nc_relative_dependency_targets AS target
                WHERE dependency.dependency_id=target.dependency_id"""
            )

            self.connection.execute(
                """CREATE TEMP TABLE _nc_unresolved_relative_versions(
                version_id INTEGER PRIMARY KEY) WITHOUT ROWID"""
            )
            self.connection.execute(
                """INSERT INTO _nc_unresolved_relative_versions(version_id)
                SELECT DISTINCT d.version_id FROM dependencies d
                JOIN _nc_current_versions v ON v.version_id=d.version_id
                WHERE d.kind='python_relative_import'
                AND d.resolved_version_id IS NULL"""
            )
            self.connection.execute(
                """DELETE FROM _nc_unresolved_relative_versions
                WHERE version_id IN(SELECT version_id FROM diagnostics
                    WHERE code='unresolved_relative_import')"""
            )
            self.connection.execute(
                """INSERT INTO diagnostics(
                version_id,source,code,severity,message,tool_name,tool_version,
                confirmed,confidence,metadata_json)
                SELECT version_id,'neocortex-project-resolver',
                'unresolved_relative_import','warning',
                'relative import could not be resolved in the indexed corpus',
                'project-resolver','1',0,0.75,'{}'
                FROM _nc_unresolved_relative_versions"""
            )
        except BaseException:
            # TEMP DDL participates in the surrounding transaction.  Roll it
            # back before dropping the work tables so rollback cannot resurrect
            # a table whose DROP was part of the failed transaction.
            self.connection.rollback()
            for table in temporary_tables:
                self.connection.execute(f"DROP TABLE IF EXISTS temp.{table}")
            raise
        else:
            for table in temporary_tables:
                self.connection.execute(f"DROP TABLE IF EXISTS temp.{table}")

    def _record_duplicate_relations(
        self,
        column: str,
        relation_kind: str,
        confidence: float,
    ) -> None:
        if column not in {"raw_xxh3_128", "normalized_xxh3_128"}:
            raise ValueError("unsupported duplicate fingerprint column")
        rows = self.connection.execute(
            f"""SELECT {column},size,version_id FROM file_versions
            WHERE invalidated_ns IS NULL AND {column} IS NOT NULL
            ORDER BY {column},size,version_id"""
        )
        group_key: tuple[str, int] | None = None
        representative: int | None = None
        for row in rows:
            key = (str(row[0]), int(row[1]))
            version_id = int(row[2])
            if key != group_key:
                group_key = key
                representative = version_id
                continue
            assert representative is not None
            self._insert_relation(
                representative,
                version_id,
                relation_kind,
                confidence,
                {"algorithm": column, "grouping": "representative-star"},
            )

    def _resolve_membership_conflicts(self) -> None:
        self.connection.execute(
            """UPDATE project_memberships SET selected=0,conflict_group=NULL
            WHERE version_id IN(
                SELECT version_id FROM file_versions WHERE invalidated_ns IS NOT NULL)"""
        )
        self.connection.execute(
            """UPDATE project_memberships SET selected=1,conflict_group=NULL
            WHERE version_id IN(
                SELECT version_id FROM file_versions WHERE invalidated_ns IS NULL)"""
        )
        groups = self.connection.execute(
            f"""SELECT m.project_id,MIN(m.proposed_path) AS proposed_path
            FROM project_memberships m
            JOIN file_versions v ON v.version_id=m.version_id
            WHERE v.invalidated_ns IS NULL
            GROUP BY m.project_id,m.proposed_path COLLATE {_PATH_COLLATION}
            HAVING COUNT(*)>1
            ORDER BY m.project_id,proposed_path COLLATE NOCASE"""
        )
        while rows := groups.fetchmany(CODE_STATE_WRITE_BATCH):
            for row in rows:
                project_id = int(row[0])
                proposed_path = str(row[1])
                normalized_path = (
                    proposed_path.casefold() if _PATH_COLLATION == "NOCASE" else proposed_path
                )
                conflict_group = fingerprint_text(f"{project_id}\0{normalized_path}").xxh3_128
                self.connection.execute(
                    f"""UPDATE project_memberships SET selected=0,conflict_group=?
                    WHERE project_id=? AND proposed_path=? COLLATE {_PATH_COLLATION}
                    AND version_id IN(SELECT version_id FROM file_versions
                        WHERE invalidated_ns IS NULL)""",
                    (conflict_group, project_id, proposed_path),
                )
                members = self.connection.execute(
                    f"""SELECT m.version_id FROM project_memberships m
                    JOIN file_versions v ON v.version_id=m.version_id
                    WHERE m.project_id=? AND m.proposed_path=? COLLATE {_PATH_COLLATION}
                    AND v.invalidated_ns IS NULL ORDER BY m.version_id""",
                    (project_id, proposed_path),
                )
                representative: int | None = None
                for (raw_version_id,) in members:
                    version_id = int(raw_version_id)
                    if representative is None:
                        representative = version_id
                        continue
                    self._insert_relation(
                        representative,
                        version_id,
                        "divergent_same_name",
                        0.5,
                        {
                            "project_id": project_id,
                            "proposed_path": normalized_path,
                            "selection": "ambiguous",
                        },
                    )

    def _record_project_edges(self) -> None:
        self.connection.execute("DELETE FROM project_edges")
        self.connection.execute(
            """INSERT INTO project_edges(
            source_project_id,target_project_id,dependency_name,edge_kind,
            confidence,evidence_json)
            SELECT source.project_id,
            CASE WHEN COUNT(DISTINCT target.project_id)=1
                THEN MIN(target.project_id) ELSE NULL END,
            d.name,d.kind,MAX(d.confidence),
            '{"resolver":"unique-normalized-project-name"}'
            FROM dependencies d
            JOIN project_memberships source ON source.version_id=d.version_id
            JOIN file_versions source_version
                ON source_version.version_id=d.version_id
            LEFT JOIN projects target ON
                REPLACE(LOWER(target.name),'-','_')=REPLACE(LOWER(d.name),'-','_')
                AND target.status<>'historical'
            WHERE source.selected=1 AND source_version.invalidated_ns IS NULL
            GROUP BY source.project_id,d.name,d.kind"""
        )
        self.connection.execute(
            """INSERT INTO diagnostics(
            version_id,source,code,severity,message,tool_name,tool_version,
            confirmed,confidence,metadata_json)
            SELECT DISTINCT m.version_id,'neocortex-project-graph','dependency_cycle',
            'warning','probable project dependency cycle','project-graph','1',
            0,0.8,'{}' FROM project_edges a
            JOIN project_edges b ON b.source_project_id=a.target_project_id
                AND b.target_project_id=a.source_project_id
            JOIN project_memberships m ON m.project_id=a.source_project_id
            JOIN file_versions v ON v.version_id=m.version_id
            WHERE a.target_project_id IS NOT NULL
            AND m.selected=1 AND v.invalidated_ns IS NULL
            AND NOT EXISTS(SELECT 1 FROM diagnostics d
                WHERE d.version_id=m.version_id AND d.code='dependency_cycle')"""
        )

    def _record_probable_dead_symbols(self) -> None:
        self.connection.execute(
            """INSERT INTO diagnostics(
            version_id,source,code,severity,message,tool_name,tool_version,
            confirmed,confidence,start_line,start_column,end_line,end_column,
            start_byte,end_byte,metadata_json)
            SELECT s.version_id,'neocortex-reference-graph','probable_dead_symbol',
            'info','private symbol has no indexed references','reference-graph','1',
            0,0.55,s.start_line,s.start_column,s.end_line,s.end_column,
            s.start_byte,s.end_byte,'{}' FROM symbols s
            JOIN file_versions v ON v.version_id=s.version_id
            WHERE v.invalidated_ns IS NULL AND s.visibility='private'
            AND s.kind IN ('function','class','method')
            AND NOT EXISTS(SELECT 1 FROM code_references r
                JOIN file_versions source_version
                    ON source_version.version_id=r.version_id
                WHERE r.target_symbol_id=s.symbol_id
                AND source_version.invalidated_ns IS NULL)
            AND NOT EXISTS(SELECT 1 FROM diagnostics d
                WHERE d.version_id=s.version_id AND d.code='probable_dead_symbol'
                AND d.start_byte=s.start_byte)"""
        )

    def mark_missing(
        self,
        framework_run_id: int,
        *,
        batch_size: int = CODE_STATE_WRITE_BATCH,
    ) -> int:
        """Invalidate unseen identities in resumable, keyset-ordered transactions.

        The caller must invoke this only after an uncapped inventory pass has
        completed.  Committed batches are idempotent, so a later invocation with
        the same run identifier safely resumes after interruption.
        """

        if not 1 <= batch_size <= 10_000:
            raise ValueError("code missing-state batch_size must be between 1 and 10000")
        removed = 0
        last_file_id = 0
        while True:
            rows = self.connection.execute(
                """SELECT file_id,current_version_id,current_path FROM files
                WHERE file_id>? AND status='current' AND last_seen_run_id<>?
                AND current_version_id IS NOT NULL
                ORDER BY file_id LIMIT ?""",
                (last_file_id, framework_run_id, batch_size),
            ).fetchall()
            if not rows:
                break
            last_file_id = int(rows[-1][0])
            now = time.time_ns()
            with self.connection:
                for row in rows:
                    file_id = int(row[0])
                    version_id = int(row[1])
                    changed = self.connection.execute(
                        """UPDATE file_versions SET invalidated_ns=?,
                        invalidation_reason='not_seen_in_complete_inventory'
                        WHERE version_id=? AND invalidated_ns IS NULL""",
                        (now, version_id),
                    ).rowcount
                    if changed != 1:
                        continue
                    self.connection.execute(
                        """INSERT INTO invalidation_history(
                        version_id,invalidated_ns,reason,evidence_json)
                        VALUES(?,?,'not_seen_in_complete_inventory',?)""",
                        (
                            version_id,
                            now,
                            _json(
                                {
                                    "run_id": framework_run_id,
                                    "path": str(row[2]),
                                }
                            ),
                        ),
                    )
                    current = self.connection.execute(
                        """UPDATE files SET status='missing',current_version_id=NULL
                        WHERE file_id=? AND current_version_id=?""",
                        (file_id, version_id),
                    )
                    removed += max(0, int(current.rowcount))
        with self.connection:
            self.connection.execute(
                """UPDATE projects SET status='historical'
                WHERE status<>'historical' AND NOT EXISTS(
                    SELECT 1 FROM project_memberships m
                    JOIN file_versions v ON v.version_id=m.version_id
                    WHERE m.project_id=projects.project_id
                    AND v.invalidated_ns IS NULL)"""
            )
        return removed


# endregion [05]


__all__ = [
    "CachedCodeVersion",
    "CodeState",
    "SkippedCodeObservation",
]
