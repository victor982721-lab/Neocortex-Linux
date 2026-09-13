"""Publish exact Code-to-Semantic coverage links after a Semantic head is ready."""

from __future__ import annotations
import json
import math
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..code_schema import (
    CODE_SCHEMA_VERSION,
    _read_version,
    checkpoint_code_wal,
    code_database,
    remove_checkpointed_code_sidecars,
    validate_code_schema,
)
from neocortex.semantic.semantic_models import canonical_json
from neocortex.semantic.semantic_schema import (
    SEMANTIC_SCHEMA_VERSION,
    _validate_semantic_read_schema,
    semantic_database,
)
from neocortex.persistence.sqlite_cancellation import (
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)


CODE_SEMANTIC_LINK_PROTOCOL = "code-semantic-link-v1"
_MAX_SOURCE_REVISION_JSON_BYTES = 1_048_576
_MAX_SEMANTIC_CHUNKS_PER_CODE_CHUNK = 100_000
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
_MAX_PUBLISHED_SEMANTIC_HEADS = 1_024
_REPAIR_PROGRESS_INSTRUCTIONS = 1_000


class CodeSemanticLinkError(RuntimeError):
    """Published Semantic evidence cannot be tied exactly to current Code rows."""


@dataclass(frozen=True, slots=True)
class CodeSemanticLinkSummary:
    """One idempotent projection of a published Semantic head into Code state."""

    generation_id: int
    model_signature: str
    vector_space: str
    semantic_chunks: int
    linked_code_chunks: int
    active_links: int
    deactivated_links: int
    changed: bool


@dataclass(frozen=True, slots=True)
class CodeSemanticAvailability:
    """Read-only availability of the default Code Semantic search channel."""

    available: bool
    reason: str
    model_signature: str
    generation_id: int | None
    current_links: int
    calibration: str = "uncalibrated_similarity"


def _semantic_read_schema_version(connection: sqlite3.Connection) -> int:
    try:
        return _validate_semantic_read_schema(connection)
    except (RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        raise CodeSemanticLinkError("Semantic read schema is incompatible") from exc


def _published_head(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    model_signature: str,
) -> str:
    if _semantic_read_schema_version(connection) != SEMANTIC_SCHEMA_VERSION:
        raise CodeSemanticLinkError(
            f"Semantic schema must be {SEMANTIC_SCHEMA_VERSION} before linking Code"
        )
    row = connection.execute(
        """SELECT h.generation_id,g.status,m.vector_space
        FROM published_embedding_heads h
        JOIN embedding_generations g ON g.generation_id=h.generation_id
          AND g.model_signature=h.model_signature
        JOIN embedding_models m ON m.model_signature=h.model_signature
        WHERE h.model_signature=?""",
        (model_signature,),
    ).fetchone()
    if row is None:
        raise CodeSemanticLinkError(
            "Semantic model has no published head for Code linking"
        )
    if int(row[0]) != generation_id or str(row[1]) != "ready":
        raise CodeSemanticLinkError(
            "Code links require the exact ready Semantic head returned by indexing"
        )
    vector_space = str(row[2])
    if not vector_space.strip():
        raise CodeSemanticLinkError("published Semantic head has no vector space")
    return vector_space


def _source_version_id(payload: object) -> int:
    if not isinstance(payload, str):
        raise CodeSemanticLinkError("Semantic source_revision_json must be text")
    if len(payload.encode("utf-8")) > _MAX_SOURCE_REVISION_JSON_BYTES:
        raise CodeSemanticLinkError("Semantic source revision exceeds the safe bound")
    try:
        revision = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CodeSemanticLinkError("Semantic source revision is invalid JSON") from exc
    if not isinstance(revision, dict):
        raise CodeSemanticLinkError("Semantic source revision must be an object")
    version_id = revision.get("version_id")
    if (
        isinstance(version_id, bool)
        or not isinstance(version_id, int)
        or not 0 < version_id <= _SQLITE_MAX_INTEGER
    ):
        raise CodeSemanticLinkError(
            "Semantic Code revision has no valid positive version_id"
        )
    return version_id


def _code_chunk_index(section_id: object) -> int:
    if not isinstance(section_id, str) or not section_id.isdecimal():
        raise CodeSemanticLinkError("Semantic Code section_id is not canonical decimal")
    value = int(section_id)
    if str(value) != section_id or value > _SQLITE_MAX_INTEGER:
        raise CodeSemanticLinkError("Semantic Code section_id is outside bounds")
    return value


def _code_chunk_kind(section_kind: object) -> str:
    if not isinstance(section_kind, str) or not section_kind.startswith("code_"):
        raise CodeSemanticLinkError("Semantic Code section_kind is invalid")
    value = section_kind.removeprefix("code_")
    if not value or len(value.encode("utf-8")) > 256:
        raise CodeSemanticLinkError("Semantic Code chunk kind is outside bounds")
    return value


def _create_desired_links_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TEMP TABLE desired_code_semantic_links(
        chunk_id INTEGER PRIMARY KEY,
        semantic_item_id TEXT NOT NULL,
        model_signature TEXT NOT NULL,
        vector_space TEXT NOT NULL,
        generation_id INTEGER NOT NULL,
        provenance_json TEXT NOT NULL
        )"""
    )


def _resolve_code_chunk_id(
    connection: sqlite3.Connection,
    *,
    source_identity: str,
    version_id: int,
    chunk_index: int,
    chunk_kind: str,
) -> int:
    rows = connection.execute(
        """SELECT c.chunk_id
        FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
        JOIN code_chunks c ON c.version_id=v.version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        AND f.volume_id || ':' || f.physical_file_id=?
        AND v.version_id=? AND c.chunk_index=? AND c.kind=?
        LIMIT 2""",
        (source_identity, version_id, chunk_index, chunk_kind),
    ).fetchall()
    if len(rows) != 1:
        raise CodeSemanticLinkError(
            "published Semantic Code chunk does not resolve to one current Code row"
        )
    return int(rows[0][0])


def _stage_link_group(
    connection: sqlite3.Connection,
    *,
    source_identity: str,
    version_id: int,
    chunk_index: int,
    chunk_kind: str,
    semantic_item_id: str,
    model_signature: str,
    vector_space: str,
    generation_id: int,
    semantic_chunk_ids: list[str],
    chunking_signature: str,
) -> None:
    if not semantic_chunk_ids:
        raise CodeSemanticLinkError("Semantic Code link group cannot be empty")
    if len(semantic_chunk_ids) > _MAX_SEMANTIC_CHUNKS_PER_CODE_CHUNK:
        raise CodeSemanticLinkError("one Code chunk expands beyond the safe link bound")
    code_chunk_id = _resolve_code_chunk_id(
        connection,
        source_identity=source_identity,
        version_id=version_id,
        chunk_index=chunk_index,
        chunk_kind=chunk_kind,
    )
    provenance = canonical_json(
        {
            "authority": "retrieval_evidence_only",
            "calibration": "uncalibrated_similarity",
            "chunking_signature": chunking_signature,
            "link_protocol": CODE_SEMANTIC_LINK_PROTOCOL,
            "semantic_chunk_ids": semantic_chunk_ids,
            "semantic_section_id": str(chunk_index),
            "semantic_section_kind": f"code_{chunk_kind}",
            "source_identity": source_identity,
            "source_version_id": version_id,
        }
    )
    try:
        connection.execute(
            """INSERT INTO desired_code_semantic_links(
            chunk_id,semantic_item_id,model_signature,vector_space,generation_id,
            provenance_json) VALUES(?,?,?,?,?,?)""",
            (
                code_chunk_id,
                semantic_item_id,
                model_signature,
                vector_space,
                generation_id,
                provenance,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise CodeSemanticLinkError(
            "published Semantic head maps one Code chunk more than once"
        ) from exc


def _stage_desired_links(
    semantic: sqlite3.Connection,
    code: sqlite3.Connection,
    *,
    generation_id: int,
    model_signature: str,
    vector_space: str,
) -> tuple[int, int]:
    rows = semantic.execute(
        """SELECT gm.entity_id,gm.item_id,ir.source_identity,
        ir.source_revision_json,ir.item_revision_id,cr.section_kind,
        cr.section_id,cr.chunking_signature
        FROM embedding_generation_members gm
        JOIN semantic_item_revisions ir
            ON ir.item_revision_id=gm.item_revision_id AND ir.item_id=gm.item_id
        JOIN semantic_chunk_revisions cr
            ON cr.chunk_revision_id=gm.chunk_revision_id
            AND cr.chunk_id=gm.entity_id AND cr.item_id=gm.item_id
        WHERE gm.generation_id=? AND gm.model_signature=?
        AND gm.entity_kind='text_chunk' AND ir.source_kind='code'
        AND cr.section_kind GLOB 'code_*'
        ORDER BY ir.source_identity,ir.item_revision_id,cr.section_kind,
                 cr.section_id,gm.entity_id""",
        (generation_id, model_signature),
    )
    semantic_chunks = 0
    group_key: tuple[str, int, int, str, str, str] | None = None
    group_chunks: list[str] = []

    def flush() -> None:
        if group_key is None:
            return
        (
            source_identity,
            version_id,
            chunk_index,
            chunk_kind,
            semantic_item_id,
            chunking_signature,
        ) = group_key
        _stage_link_group(
            code,
            source_identity=source_identity,
            version_id=version_id,
            chunk_index=chunk_index,
            chunk_kind=chunk_kind,
            semantic_item_id=semantic_item_id,
            model_signature=model_signature,
            vector_space=vector_space,
            generation_id=generation_id,
            semantic_chunk_ids=group_chunks,
            chunking_signature=chunking_signature,
        )

    for row in rows:
        semantic_chunks += 1
        source_identity = str(row[2])
        semantic_item_id = str(row[1])
        chunking_signature = str(row[7])
        if not source_identity.strip() or not semantic_item_id.strip():
            raise CodeSemanticLinkError("Semantic Code identity cannot be blank")
        if not chunking_signature.strip():
            raise CodeSemanticLinkError("Semantic Code chunking signature is blank")
        key = (
            source_identity,
            _source_version_id(row[3]),
            _code_chunk_index(row[6]),
            _code_chunk_kind(row[5]),
            semantic_item_id,
            chunking_signature,
        )
        if group_key is not None and key != group_key:
            flush()
            group_chunks = []
        group_key = key
        semantic_chunk_id = str(row[0])
        if not semantic_chunk_id.strip():
            raise CodeSemanticLinkError("Semantic chunk identity cannot be blank")
        group_chunks.append(semantic_chunk_id)
    flush()
    linked_code_chunks = int(
        code.execute("SELECT COUNT(*) FROM desired_code_semantic_links").fetchone()[0]
    )
    return semantic_chunks, linked_code_chunks


def _require_complete_current_coverage(connection: sqlite3.Connection) -> None:
    missing = int(
        connection.execute(
            """SELECT COUNT(*)
            FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
            JOIN code_chunks c ON c.version_id=v.version_id
            LEFT JOIN desired_code_semantic_links d ON d.chunk_id=c.chunk_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
            AND v.analysis_status IN ('complete','partial','text_only')
            AND trim(c.text)<>'' AND d.chunk_id IS NULL"""
        ).fetchone()[0]
    )
    if missing:
        raise CodeSemanticLinkError(
            f"published Semantic head leaves {missing} current Code chunks unlinked"
        )


def _links_differ(
    connection: sqlite3.Connection,
    *,
    model_signature: str,
) -> bool:
    extra = int(
        connection.execute(
            """SELECT COUNT(*) FROM embedding_links e
            WHERE e.model_signature=? AND e.active=1
            AND NOT EXISTS(
                SELECT 1 FROM desired_code_semantic_links d
                WHERE d.chunk_id=e.chunk_id
                AND d.semantic_item_id=e.semantic_item_id
                AND d.model_signature=e.model_signature
                AND d.vector_space=e.vector_space
                AND d.generation_id=e.generation_id
                AND d.provenance_json=e.provenance_json)""",
            (model_signature,),
        ).fetchone()[0]
    )
    missing_or_changed = int(
        connection.execute(
            """SELECT COUNT(*) FROM desired_code_semantic_links d
            WHERE NOT EXISTS(
                SELECT 1 FROM embedding_links e
                WHERE e.chunk_id=d.chunk_id AND e.active=1
                AND e.semantic_item_id=d.semantic_item_id
                AND e.model_signature=d.model_signature
                AND e.vector_space=d.vector_space
                AND e.generation_id=d.generation_id
                AND e.provenance_json=d.provenance_json)"""
        ).fetchone()[0]
    )
    return bool(extra or missing_or_changed)


def _checkpoint_code_wal(connection: sqlite3.Connection) -> None:
    """Leave direct Semantic-link publication readable by quiescent Code status."""

    checkpoint_code_wal(connection, error_type=CodeSemanticLinkError)


def _remove_checkpointed_code_sidecars(code_path: Path) -> None:
    """Remove only reconstructible sidecars after a verified empty WAL."""

    remove_checkpointed_code_sidecars(
        code_path,
        error_type=CodeSemanticLinkError,
    )


def _repair_published_heads(
    published_heads: Sequence[tuple[str, int]],
) -> tuple[tuple[str, int], ...]:
    if not isinstance(published_heads, Sequence):
        raise TypeError("published_heads must be a sequence of (model_signature, generation_id)")
    if len(published_heads) > _MAX_PUBLISHED_SEMANTIC_HEADS:
        raise ValueError("published_heads exceed their bound")
    values = tuple(published_heads)
    if len(values) > _MAX_PUBLISHED_SEMANTIC_HEADS:
        raise ValueError("published_heads exceed their bound")
    normalized: list[tuple[str, int]] = []
    for value in values:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError("published_heads contain an invalid entry")
        model_signature, generation_id = value
        if (
            not isinstance(model_signature, str)
            or not model_signature
            or model_signature.strip() != model_signature
            or len(model_signature.encode("utf-8")) > 4_096
        ):
            raise ValueError("published head model_signature is invalid")
        if (
            isinstance(generation_id, bool)
            or not isinstance(generation_id, int)
            or not 0 < generation_id <= _SQLITE_MAX_INTEGER
        ):
            raise ValueError("published head generation_id is invalid")
        normalized.append((model_signature, generation_id))
    if len(set(normalized)) != len(normalized):
        raise ValueError("published_heads contain duplicates")
    return tuple(sorted(normalized))


def _repair_checkpoint(
    *,
    deadline_monotonic: float | None,
    cancellation_check: Callable[[], bool | None] | None,
) -> Callable[[], None]:
    if deadline_monotonic is not None and (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(float(deadline_monotonic))
        or float(deadline_monotonic) < 0
    ):
        raise ValueError("deadline_monotonic must be finite and non-negative")
    if cancellation_check is not None and not callable(cancellation_check):
        raise TypeError("cancellation_check must be callable")
    deadline = None if deadline_monotonic is None else float(deadline_monotonic)

    def checkpoint() -> None:
        if cancellation_check is not None:
            decision = cancellation_check()
            if decision is not None and decision is not False:
                raise CodeSemanticLinkError("Code Semantic link repair was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise CodeSemanticLinkError("Code Semantic link repair deadline exceeded")

    return checkpoint


def deactivate_stale_code_embedding_links(
    state_directory: Path,
    *,
    published_heads: Sequence[tuple[str, int]],
    deadline_monotonic: float | None = None,
    cancellation_check: Callable[[], bool | None] | None = None,
) -> int:
    """Deactivate only stale Code-to-Semantic projection links.

    The caller owns the enclosing ``FrameworkRunLock`` and supplies a fresh,
    validated complete Semantic head set.  This repair never deletes or
    rewrites Code chunks, files, generations, or Semantic state; it only flips
    ``active`` to zero for links whose model/generation is no longer in that
    set or whose source Code chunk is no longer current.
    """

    selected_heads = _repair_published_heads(published_heads)
    code_path = Path(state_directory) / "code.sqlite3"
    if not code_path.is_file():
        raise FileNotFoundError(code_path)
    checkpoint = _repair_checkpoint(
        deadline_monotonic=deadline_monotonic,
        cancellation_check=cancellation_check,
    )
    bridge = SQLiteCancellationBridge(checkpoint if (deadline_monotonic is not None or cancellation_check is not None) else None)
    changed = 0
    try:
        checkpoint()
        with code_database(code_path, create=False) as code:
            with sqlite_cancellation_scope(
                code,
                bridge,
                instructions=_REPAIR_PROGRESS_INSTRUCTIONS,
            ):
                checkpoint()
                if _read_version(code) != CODE_SCHEMA_VERSION:
                    raise CodeSemanticLinkError(
                        f"Code schema must be {CODE_SCHEMA_VERSION} for link repair"
                    )
                validate_code_schema(code)
                checkpoint()
                code.execute("BEGIN IMMEDIATE")
                try:
                    code.execute(
                        """CREATE TEMP TABLE repair_published_heads(
                        model_signature TEXT PRIMARY KEY,
                        generation_id INTEGER NOT NULL
                        ) WITHOUT ROWID"""
                    )
                    code.executemany(
                        "INSERT INTO repair_published_heads(model_signature,generation_id) VALUES(?,?)",
                        selected_heads,
                    )
                    checkpoint()
                    changed = int(
                        code.execute(
                            """UPDATE embedding_links AS link SET active=0
                            WHERE link.active=1 AND (
                                NOT EXISTS(
                                    SELECT 1 FROM repair_published_heads published
                                    WHERE published.model_signature=link.model_signature
                                      AND published.generation_id=link.generation_id
                                ) OR NOT EXISTS(
                                    SELECT 1
                                    FROM code_chunks chunk
                                    JOIN file_versions version
                                      ON version.version_id=chunk.version_id
                                    JOIN files file
                                      ON file.current_version_id=version.version_id
                                    WHERE chunk.chunk_id=link.chunk_id
                                      AND file.status='current'
                                      AND version.invalidated_ns IS NULL
                                )
                            )"""
                        ).rowcount
                    )
                    checkpoint()
                    code.commit()
                except BaseException:
                    code.rollback()
                    raise
        return changed
    except CodeSemanticLinkError:
        raise
    except Exception as exc:
        if bridge.captured_exception is not None:
            raise bridge.captured_exception from exc
        raise CodeSemanticLinkError(
            f"Code Semantic link repair could not complete: {type(exc).__name__}"
        ) from exc


def synchronize_code_embedding_links(
    state_directory: Path,
    *,
    generation_id: int,
    model_signature: str,
) -> CodeSemanticLinkSummary:
    """Make Code links exactly mirror one published Semantic text head."""

    if isinstance(generation_id, bool) or not 0 < generation_id <= _SQLITE_MAX_INTEGER:
        raise ValueError("generation_id must be a positive SQLite integer")
    if not model_signature.strip():
        raise ValueError("model_signature cannot be blank")
    semantic_path = state_directory / "semantic.sqlite3"
    code_path = state_directory / "code.sqlite3"
    if not semantic_path.is_file() or not code_path.is_file():
        raise FileNotFoundError("Code and Semantic databases are required for linking")

    result: CodeSemanticLinkSummary | None = None
    with semantic_database(semantic_path, readonly=True) as semantic:
        semantic.execute("BEGIN")
        vector_space = _published_head(
            semantic,
            generation_id=generation_id,
            model_signature=model_signature,
        )
        with code_database(code_path, create=False) as code:
            validate_code_schema(code)
            code.execute("BEGIN IMMEDIATE")
            try:
                _create_desired_links_table(code)
                semantic_chunks, linked_code_chunks = _stage_desired_links(
                    semantic,
                    code,
                    generation_id=generation_id,
                    model_signature=model_signature,
                    vector_space=vector_space,
                )
                _require_complete_current_coverage(code)
                if not _links_differ(code, model_signature=model_signature):
                    code.rollback()
                    _checkpoint_code_wal(code)
                    result = CodeSemanticLinkSummary(
                        generation_id,
                        model_signature,
                        vector_space,
                        semantic_chunks,
                        linked_code_chunks,
                        linked_code_chunks,
                        0,
                        False,
                    )
                else:
                    deactivated = code.execute(
                        """UPDATE embedding_links SET active=0
                        WHERE model_signature=? AND active=1""",
                        (model_signature,),
                    ).rowcount
                    code.execute(
                        """INSERT INTO embedding_links(
                        chunk_id,semantic_item_id,model_signature,vector_space,
                        generation_id,active,provenance_json)
                        SELECT chunk_id,semantic_item_id,model_signature,vector_space,
                        generation_id,1,provenance_json
                        FROM desired_code_semantic_links WHERE 1
                        ON CONFLICT(chunk_id,model_signature,generation_id) DO UPDATE SET
                        semantic_item_id=excluded.semantic_item_id,
                        vector_space=excluded.vector_space,active=1,
                        provenance_json=excluded.provenance_json"""
                    )
                    active_links = int(
                        code.execute(
                            """SELECT COUNT(*) FROM embedding_links
                            WHERE model_signature=? AND generation_id=? AND active=1""",
                            (model_signature, generation_id),
                        ).fetchone()[0]
                    )
                    if active_links != linked_code_chunks:
                        raise CodeSemanticLinkError(
                            "Code link publication count changed during synchronization"
                        )
                    code.commit()
                    _checkpoint_code_wal(code)
                    result = CodeSemanticLinkSummary(
                        generation_id,
                        model_signature,
                        vector_space,
                        semantic_chunks,
                        linked_code_chunks,
                        active_links,
                        int(deactivated),
                        True,
                    )
            except BaseException:
                code.rollback()
                raise
    if result is None:  # pragma: no cover - every successful branch assigns it
        raise CodeSemanticLinkError("Code link synchronization produced no result")
    _remove_checkpointed_code_sidecars(code_path)
    return result


def current_code_embedding_link_counts(
    state_directory: Path,
    *,
    generation_id: int,
    model_signature: str,
) -> tuple[int, int]:
    """Return active and current active links for one published generation."""

    from neocortex.persistence.sqlite_immutable import immutable_sqlite_database

    with immutable_sqlite_database(state_directory / "code.sqlite3") as connection:
        validate_code_schema(connection)
        active = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_links
                WHERE model_signature=? AND generation_id=? AND active=1""",
                (model_signature, generation_id),
            ).fetchone()[0]
        )
        current = int(
            connection.execute(
                """SELECT COUNT(*) FROM embedding_links e
                JOIN code_chunks c ON c.chunk_id=e.chunk_id
                JOIN file_versions v ON v.version_id=c.version_id
                JOIN files f ON f.current_version_id=v.version_id
                WHERE e.model_signature=? AND e.generation_id=? AND e.active=1
                AND f.status='current' AND v.invalidated_ns IS NULL""",
                (model_signature, generation_id),
            ).fetchone()[0]
        )
    return active, current


def code_semantic_search_availability(
    state_directory: Path,
    *,
    model_cache_override: Path | None = None,
    verify_model_cache: bool = True,
) -> CodeSemanticAvailability:
    """Explain whether default-profile Code vectors are published and current."""

    from neocortex.semantic.semantic_config import TEXT_MODEL_SIGNATURE

    semantic_path = state_directory / "semantic.sqlite3"
    code_path = state_directory / "code.sqlite3"
    if not code_path.is_file():
        return CodeSemanticAvailability(
            False,
            "code_state_missing",
            TEXT_MODEL_SIGNATURE,
            None,
            0,
        )
    if not semantic_path.is_file():
        return CodeSemanticAvailability(
            False,
            "semantic_state_missing",
            TEXT_MODEL_SIGNATURE,
            None,
            0,
        )
    with code_database(code_path, readonly=True) as code:
        validate_code_schema(code)
        rows = code.execute(
            """SELECT e.generation_id,e.vector_space,COUNT(*)
            FROM embedding_links e
            JOIN code_chunks c ON c.chunk_id=e.chunk_id
            JOIN file_versions v ON v.version_id=c.version_id
            JOIN files f ON f.current_version_id=v.version_id
            WHERE e.model_signature=? AND e.active=1
            AND f.status='current' AND v.invalidated_ns IS NULL
            GROUP BY e.generation_id,e.vector_space ORDER BY e.generation_id DESC""",
            (TEXT_MODEL_SIGNATURE,),
        ).fetchall()
    if not rows:
        return CodeSemanticAvailability(
            False,
            "no_current_default_profile_links",
            TEXT_MODEL_SIGNATURE,
            None,
            0,
        )
    with semantic_database(semantic_path, readonly=True) as semantic:
        semantic.execute("BEGIN")
        _semantic_read_schema_version(semantic)
        head = semantic.execute(
            """SELECT h.generation_id,m.vector_space,g.status
            FROM published_embedding_heads h
            JOIN embedding_models m ON m.model_signature=h.model_signature
            JOIN embedding_generations g ON g.generation_id=h.generation_id
              AND g.model_signature=h.model_signature
            WHERE h.model_signature=?""",
            (TEXT_MODEL_SIGNATURE,),
        ).fetchone()
    if head is None or str(head[2]) != "ready":
        return CodeSemanticAvailability(
            False,
            "default_profile_head_not_published",
            TEXT_MODEL_SIGNATURE,
            None,
            0,
        )
    head_generation = int(head[0])
    head_space = str(head[1])
    matching = [
        row
        for row in rows
        if int(row[0]) == head_generation and str(row[1]) == head_space
    ]
    if len(matching) != 1:
        return CodeSemanticAvailability(
            False,
            "code_links_do_not_match_published_semantic_head",
            TEXT_MODEL_SIGNATURE,
            head_generation,
            0,
        )
    current_links = int(matching[0][2])
    if verify_model_cache:
        from neocortex.semantic.semantic_config import multilingual_text_model
        from neocortex.semantic.semantic_preparation import (
            SemanticModelUnavailableError,
            model_cache,
            require_local_fastembed_model,
        )

        try:
            require_local_fastembed_model(
                multilingual_text_model(),
                model_cache(state_directory, model_cache_override),
            )
        except SemanticModelUnavailableError as exc:
            return CodeSemanticAvailability(
                False,
                str(exc),
                TEXT_MODEL_SIGNATURE,
                head_generation,
                current_links,
            )
    return CodeSemanticAvailability(
        True,
        "available",
        TEXT_MODEL_SIGNATURE,
        head_generation,
        current_links,
    )


__all__ = [  # noqa: RUF022
    "CODE_SEMANTIC_LINK_PROTOCOL",
    "CodeSemanticAvailability",
    "CodeSemanticLinkError",
    "CodeSemanticLinkSummary",
    "current_code_embedding_link_counts",
    "code_semantic_search_availability",
    "deactivate_stale_code_embedding_links",
    "synchronize_code_embedding_links",
]
