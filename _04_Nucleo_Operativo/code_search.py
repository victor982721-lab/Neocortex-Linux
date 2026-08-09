"""Exact, full-text and structural search over current code observations."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .code_contracts import (
    CodeRelationEndpoint,
    CodeSearchHit,
    CodeSearchQuery,
    CodeSearchRelation,
)
from .code_schema import connect_code_state, readonly_code_database
from .sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)

if TYPE_CHECKING:
    from .semantic_models import ResolvedSearchHit


# region [01] Query vocabulary and row projection


SEARCH_MODES = frozenset(
    {
        "literal",
        "fts",
        "path",
        "language",
        "symbol",
        "definition",
        "reference",
        "import",
        "dependency",
        "call",
        "signature",
        "diagnostic",
        "complexity",
        "semantic",
        "hybrid",
    }
)

_CANCELLATION_BATCH_ROWS = 128

_HYBRID_MODES = (
    "literal",
    "fts",
    "symbol",
    "definition",
    "reference",
    "import",
    "dependency",
    "call",
    "signature",
    "diagnostic",
    "semantic",
)

_MODE_WEIGHT = {
    "literal": 5.0,
    "fts": 4.0,
    "path": 2.5,
    "language": 2.0,
    "symbol": 5.0,
    "definition": 6.0,
    "reference": 4.0,
    "import": 4.5,
    "dependency": 4.5,
    "call": 4.5,
    "signature": 5.0,
    "diagnostic": 4.0,
    "complexity": 3.5,
    "semantic": 3.0,
}


@dataclass(frozen=True, slots=True)
class _SearchRow:
    version_id: int
    path: str
    project: str | None
    language: str | None
    artifact_kind: str
    symbol: str | None
    signature: str | None
    start_line: int
    end_line: int
    snippet: str
    size: int
    mtime_ns: int
    status: str
    evidence: str
    relation: CodeSearchRelation | None = None

    @classmethod
    def from_sql(
        cls,
        row: sqlite3.Row,
        *,
        relation: CodeSearchRelation | None = None,
    ) -> "_SearchRow":
        return cls(
            version_id=int(row[0]),
            path=str(row[1]),
            project=None if row[2] is None else str(row[2]),
            language=None if row[3] is None else str(row[3]),
            artifact_kind=str(row[4]),
            symbol=None if row[5] is None else str(row[5]),
            signature=None if row[6] is None else str(row[6]),
            start_line=max(1, int(row[7] or 1)),
            end_line=max(1, int(row[8] or row[7] or 1)),
            snippet=_bounded_snippet(str(row[9] or "")),
            size=int(row[10]),
            mtime_ns=int(row[11]),
            status=str(row[12]),
            evidence=str(row[13]),
            relation=relation,
        )


def _optional_positive_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str, bytes, bytearray)):
        raise RuntimeError(f"{label} is malformed")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"{label} is malformed") from exc
    if parsed < 1:
        raise RuntimeError(f"{label} is malformed")
    return parsed


def _reference_relation(row: sqlite3.Row) -> CodeSearchRelation:
    source_symbol_id = _optional_positive_int(
        row["relation_source_symbol_id"], label="reference source symbol id"
    )
    joined_source_symbol_id = _optional_positive_int(
        row["joined_source_symbol_id"], label="joined reference source symbol id"
    )
    if source_symbol_id != joined_source_symbol_id:
        raise RuntimeError("reference source symbol does not belong to source version")
    source_symbol = None if joined_source_symbol_id is None else str(row["relation_source_symbol"])
    source = CodeRelationEndpoint(
        version_id=int(row[0]),
        path=str(row[1]),
        symbol_id=joined_source_symbol_id,
        symbol=source_symbol,
    )

    target_symbol_id = _optional_positive_int(
        row["relation_target_symbol_id"], label="reference target symbol id"
    )
    target_version_id = _optional_positive_int(
        row["relation_target_version_id"], label="reference target version id"
    )
    if (target_symbol_id is None) != (target_version_id is None):
        raise RuntimeError("reference target binding is incomplete")
    target: CodeRelationEndpoint | None = None
    if target_symbol_id is not None and target_version_id is not None:
        joined_symbol_id = _optional_positive_int(
            row["joined_target_symbol_id"], label="joined reference target symbol id"
        )
        joined_version_id = _optional_positive_int(
            row["joined_target_version_id"], label="joined reference target version id"
        )
        symbol_version_id = _optional_positive_int(
            row["target_symbol_version_id"], label="reference symbol version id"
        )
        current_version_id = _optional_positive_int(
            row["target_current_version_id"], label="reference current version id"
        )
        target_is_current = (
            joined_symbol_id == target_symbol_id
            and joined_version_id == target_version_id
            and symbol_version_id == target_version_id
            and current_version_id == target_version_id
            and row["target_invalidated_ns"] is None
            and row["target_file_status"] == "current"
            and row["relation_target_path"] is not None
            and row["relation_target_symbol"] is not None
        )
        if target_is_current:
            target = CodeRelationEndpoint(
                version_id=target_version_id,
                path=str(row["relation_target_path"]),
                symbol_id=target_symbol_id,
                symbol=str(row["relation_target_symbol"]),
            )

    return CodeSearchRelation(
        family="reference",
        kind=str(row["relation_kind"]),
        name=str(row["relation_name"]),
        source=source,
        target=target,
        target_hint=(
            None if row["relation_target_hint"] is None else str(row["relation_target_hint"])
        ),
        resolved=target is not None,
        confirmed=bool(int(row["relation_confirmed"])),
        confidence=float(row["relation_confidence"]),
        provenance=str(row["relation_provenance"]),
        source_table="code_references",
        source_row_id=int(row["owner_relation_row_id"]),
    )


def _dependency_relation(row: sqlite3.Row) -> CodeSearchRelation:
    source = CodeRelationEndpoint(version_id=int(row[0]), path=str(row[1]))
    target_version_id = _optional_positive_int(
        row["relation_target_version_id"], label="dependency target version id"
    )
    target: CodeRelationEndpoint | None = None
    if target_version_id is not None:
        joined_version_id = _optional_positive_int(
            row["joined_target_version_id"], label="joined dependency target version id"
        )
        current_version_id = _optional_positive_int(
            row["target_current_version_id"], label="dependency current version id"
        )
        target_is_current = (
            joined_version_id == target_version_id
            and current_version_id == target_version_id
            and row["target_invalidated_ns"] is None
            and row["target_file_status"] == "current"
            and row["relation_target_path"] is not None
        )
        if target_is_current:
            target = CodeRelationEndpoint(
                version_id=target_version_id,
                path=str(row["relation_target_path"]),
            )
    return CodeSearchRelation(
        family="dependency",
        kind=str(row["relation_kind"]),
        name=str(row["relation_name"]),
        source=source,
        target=target,
        target_hint=None,
        resolved=target is not None,
        confirmed=bool(int(row["relation_confirmed"])),
        confidence=float(row["relation_confidence"]),
        provenance=str(row["relation_provenance"]),
        source_table="dependencies",
        source_row_id=int(row["owner_relation_row_id"]),
        scope=None if row["relation_scope"] is None else str(row["relation_scope"]),
        version_spec=(
            None if row["relation_version_spec"] is None else str(row["relation_version_spec"])
        ),
    )


def _bounded_snippet(value: str, limit: int = 800) -> str:
    clean = value.replace("\x00", " ")
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _fts_query(text: str) -> str | None:
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)[:32]
    if not tokens:
        return None
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _mode_plan(query: CodeSearchQuery) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(item.casefold() for item in query.modes))
    unknown = set(requested) - SEARCH_MODES
    if unknown:
        raise ValueError(f"unsupported code search modes: {', '.join(sorted(unknown))}")
    expanded: list[str] = []
    for mode in requested:
        expanded.extend(_HYBRID_MODES if mode == "hybrid" else (mode,))
    if query.path and "path" not in expanded:
        expanded.append("path")
    if query.language and "language" not in expanded:
        expanded.append("language")
    if query.minimum_complexity is not None and "complexity" not in expanded:
        expanded.append("complexity")
    if query.diagnostic and "diagnostic" not in expanded:
        expanded.append("diagnostic")
    return tuple(dict.fromkeys(expanded))


# endregion [01]


# region [02] Set-oriented mode queries


_COMMON_FILTER = """
v.invalidated_ns IS NULL AND f.status='current'
AND (? IS NULL OR f.current_path LIKE '%' || ? || '%' ESCAPE '\\')
AND (? IS NULL OR v.language=? COLLATE NOCASE)
AND (? IS NULL OR EXISTS(
    SELECT 1 FROM project_memberships pmf
    JOIN projects pf ON pf.project_id=pmf.project_id
    WHERE pmf.version_id=v.version_id AND pf.name LIKE '%' || ? || '%'
))
"""


def _filter_parameters(query: CodeSearchQuery) -> tuple[object, ...]:
    return (
        query.path,
        query.path,
        query.language,
        query.language,
        query.project,
        query.project,
    )


def _project_sql(version_alias: str = "v") -> str:
    return f"""(SELECT p.name FROM project_memberships pm
        JOIN projects p ON p.project_id=pm.project_id
        WHERE pm.version_id={version_alias}.version_id
        ORDER BY pm.confidence DESC,p.project_id LIMIT 1)"""


def _text_rows(
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
    mode: str,
    fetch_limit: int,
) -> tuple[_SearchRow, ...]:
    project = _project_sql()
    if mode == "fts":
        expression = _fts_query(query.text)
        if expression is None:
            return ()
        sql = f"""SELECT v.version_id,f.current_path,{project},v.language,
        v.artifact_kind,NULL,NULL,c.start_line,c.end_line,c.text,v.size,v.mtime_ns,
        v.analysis_status,'fts:' || ? FROM code_fts ft
        JOIN code_chunks c ON c.chunk_id=ft.chunk_id
        JOIN file_versions v ON v.version_id=c.version_id
        JOIN files f ON f.current_version_id=v.version_id
        WHERE ft.code_fts MATCH ? AND {_COMMON_FILTER}
        ORDER BY bm25(code_fts),v.version_id,c.chunk_index LIMIT ?"""
        params = (query.text, expression, *_filter_parameters(query), fetch_limit)
    else:
        if mode == "literal" and not query.text:
            return ()
        predicate = "instr(c.text,?)>0" if mode == "literal" else "1=1"
        evidence = query.text if mode == "literal" else (query.path or query.language or "")
        sql = f"""SELECT v.version_id,f.current_path,{project},v.language,
        v.artifact_kind,NULL,NULL,c.start_line,c.end_line,c.text,v.size,v.mtime_ns,
        v.analysis_status,? FROM code_chunks c
        JOIN file_versions v ON v.version_id=c.version_id
        JOIN files f ON f.current_version_id=v.version_id
        WHERE {predicate} AND {_COMMON_FILTER}
        ORDER BY v.version_id,c.chunk_index LIMIT ?"""
        prefix: tuple[object, ...] = (f"{mode}:{evidence}",)
        if mode == "literal":
            prefix = (*prefix, query.text)
        params = (*prefix, *_filter_parameters(query), fetch_limit)
    return tuple(_SearchRow.from_sql(row) for row in connection.execute(sql, params))


def _symbol_rows(
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
    mode: str,
    fetch_limit: int,
) -> tuple[_SearchRow, ...]:
    search = query.symbol or query.text
    if mode not in {"complexity"} and not search:
        return ()
    predicates = {
        "symbol": "(s.name LIKE '%' || ? || '%' OR s.qualified_name LIKE '%' || ? || '%')",
        "definition": "(s.name LIKE '%' || ? || '%' OR s.qualified_name LIKE '%' || ? || '%')",
        "signature": "s.signature LIKE '%' || ? || '%'",
        "complexity": "COALESCE(s.complexity,0)>=?",
    }
    predicate = predicates[mode]
    mode_params: tuple[object, ...]
    if mode in {"symbol", "definition"}:
        mode_params = (search, search)
    elif mode == "signature":
        mode_params = (search,)
    else:
        mode_params = (query.minimum_complexity or 0,)
    project = _project_sql()
    sql = f"""SELECT v.version_id,f.current_path,{project},v.language,
    v.artifact_kind,s.qualified_name,s.signature,s.start_line,s.end_line,
    COALESCE(s.docstring,s.signature,s.qualified_name),v.size,v.mtime_ns,
    v.analysis_status,? || ':' || s.kind FROM symbols s
    JOIN file_versions v ON v.version_id=s.version_id
    JOIN files f ON f.current_version_id=v.version_id
    WHERE {predicate} AND {_COMMON_FILTER}
    ORDER BY COALESCE(s.complexity,0) DESC,s.qualified_name LIMIT ?"""
    params = (mode, *mode_params, *_filter_parameters(query), fetch_limit)
    return tuple(_SearchRow.from_sql(row) for row in connection.execute(sql, params))


def _reference_rows(
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
    mode: str,
    fetch_limit: int,
) -> tuple[_SearchRow, ...]:
    search = query.text or query.symbol
    if not search:
        return ()
    kind_predicate = {
        "reference": "1=1",
        "import": "r.kind IN ('import','from_import','use','module')",
        "call": "r.kind='call'",
    }[mode]
    project = _project_sql()
    sql = f"""SELECT v.version_id,f.current_path,{project},v.language,
    v.artifact_kind,ss.qualified_name,ss.signature,r.start_line,r.end_line,
    r.kind || ' ' || r.name || COALESCE(' -> ' || r.target_hint,''),
    v.size,v.mtime_ns,v.analysis_status,? || ':' || r.evidence,
    r.reference_id AS owner_relation_row_id,r.kind AS relation_kind,
    r.name AS relation_name,r.target_hint AS relation_target_hint,
    r.confirmed AS relation_confirmed,r.confidence AS relation_confidence,
    r.evidence AS relation_provenance,
    r.source_symbol_id AS relation_source_symbol_id,
    ss.symbol_id AS joined_source_symbol_id,
    ss.qualified_name AS relation_source_symbol,
    r.target_symbol_id AS relation_target_symbol_id,
    ts.symbol_id AS joined_target_symbol_id,
    ts.qualified_name AS relation_target_symbol,
    ts.version_id AS target_symbol_version_id,
    r.target_version_id AS relation_target_version_id,
    tv.version_id AS joined_target_version_id,
    tv.invalidated_ns AS target_invalidated_ns,
    tf.current_path AS relation_target_path,
    tf.current_version_id AS target_current_version_id,
    tf.status AS target_file_status
    FROM code_references r
    JOIN file_versions v ON v.version_id=r.version_id
    JOIN files f ON f.file_id=v.file_id AND f.current_version_id=v.version_id
    LEFT JOIN symbols ss ON ss.symbol_id=r.source_symbol_id
        AND ss.version_id=v.version_id
    LEFT JOIN symbols ts ON ts.symbol_id=r.target_symbol_id
    LEFT JOIN file_versions tv ON tv.version_id=r.target_version_id
    LEFT JOIN files tf ON tf.file_id=tv.file_id
    WHERE {kind_predicate}
    AND (r.name LIKE '%' || ? || '%' OR r.target_hint LIKE '%' || ? || '%')
    AND {_COMMON_FILTER}
    ORDER BY r.confirmed DESC,r.confidence DESC,r.reference_id LIMIT ?"""
    params = (mode, search, search, *_filter_parameters(query), fetch_limit)
    return tuple(
        _SearchRow.from_sql(row, relation=_reference_relation(row))
        for row in connection.execute(sql, params)
    )


def _dependency_rows(
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
    fetch_limit: int,
) -> tuple[_SearchRow, ...]:
    if not query.text:
        return ()
    project = _project_sql()
    sql = f"""SELECT v.version_id,f.current_path,{project},v.language,
    v.artifact_kind,NULL,NULL,COALESCE(d.start_line,1),COALESCE(d.end_line,1),
    d.kind || ' ' || d.name || COALESCE(' ' || d.version_spec,''),
    v.size,v.mtime_ns,v.analysis_status,'dependency:' || d.evidence,
    d.dependency_id AS owner_relation_row_id,d.kind AS relation_kind,
    d.name AS relation_name,d.confirmed AS relation_confirmed,
    d.confidence AS relation_confidence,d.evidence AS relation_provenance,
    d.scope AS relation_scope,d.version_spec AS relation_version_spec,
    d.resolved_version_id AS relation_target_version_id,
    tv.version_id AS joined_target_version_id,
    tv.invalidated_ns AS target_invalidated_ns,
    tf.current_path AS relation_target_path,
    tf.current_version_id AS target_current_version_id,
    tf.status AS target_file_status
    FROM dependencies d JOIN file_versions v ON v.version_id=d.version_id
    JOIN files f ON f.file_id=v.file_id AND f.current_version_id=v.version_id
    LEFT JOIN file_versions tv ON tv.version_id=d.resolved_version_id
    LEFT JOIN files tf ON tf.file_id=tv.file_id
    WHERE d.name LIKE '%' || ? || '%' AND {_COMMON_FILTER}
    ORDER BY d.confirmed DESC,d.confidence DESC,d.dependency_id LIMIT ?"""
    params = (query.text, *_filter_parameters(query), fetch_limit)
    return tuple(
        _SearchRow.from_sql(row, relation=_dependency_relation(row))
        for row in connection.execute(sql, params)
    )


def _diagnostic_rows(
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
    fetch_limit: int,
) -> tuple[_SearchRow, ...]:
    search = query.diagnostic or query.text
    if not search:
        return ()
    project = _project_sql()
    sql = f"""SELECT v.version_id,f.current_path,{project},v.language,
    v.artifact_kind,NULL,NULL,COALESCE(d.start_line,1),COALESCE(d.end_line,1),
    d.code || ': ' || d.message,v.size,v.mtime_ns,v.analysis_status,
    'diagnostic:' || d.source || ':' || d.tool_version
    FROM diagnostics d JOIN file_versions v ON v.version_id=d.version_id
    JOIN files f ON f.current_version_id=v.version_id
    WHERE (d.code LIKE '%' || ? || '%' OR d.message LIKE '%' || ? || '%')
    AND {_COMMON_FILTER}
    ORDER BY CASE d.severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
    d.diagnostic_id LIMIT ?"""
    params = (search, search, *_filter_parameters(query), fetch_limit)
    return tuple(_SearchRow.from_sql(row) for row in connection.execute(sql, params))


_SQLITE_SIGNED_INTEGER_MAX = 9_223_372_036_854_775_807


@dataclass(frozen=True, slots=True)
class _SemanticCodeResolution:
    source_identity: str
    chunk_kind: str
    chunk_index: int
    version_id: int
    semantic_item_id: str
    indexed_model_signature: str
    vector_space: str
    generation_id: int
    score: float
    snippet: str


def _semantic_search_ready(
    code_database: Path,
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
) -> bool:
    if not query.text or not (code_database.parent / "semantic.sqlite3").is_file():
        return False
    return bool(
        connection.execute(
            """SELECT EXISTS(
            SELECT 1 FROM embedding_links e
            JOIN code_chunks c ON c.chunk_id=e.chunk_id
            JOIN file_versions v ON v.version_id=c.version_id
            JOIN files f ON f.current_version_id=v.version_id
            WHERE e.active=1 AND f.status='current'
            AND v.invalidated_ns IS NULL)"""
        ).fetchone()[0]
    )


def _semantic_ranked_hits(
    code_database: Path,
    query: CodeSearchQuery,
    fetch_limit: int,
    cancellation: SQLiteCancellationBridge,
    *,
    model_cache: Path | None,
    threads: int | None,
) -> tuple[ResolvedSearchHit, ...]:
    from .semantic_service import search_semantic_index

    cancellation.checkpoint()
    result = search_semantic_index(
        code_database.parent,
        query.text,
        limit=fetch_limit,
        max_vectors=500_000,
        include_text=True,
        include_images=False,
        include_lexical=False,
        model_cache=model_cache,
        local_files_only=True,
        threads=threads,
        cancellation_check=cancellation.checkpoint,
    )
    cancellation.checkpoint()
    ranking = next((item for item in result.rankings if item.name == "semantic_text"), None)
    if ranking is None or not ranking.available:
        return ()
    return ranking.resolved


def _semantic_chunk_locator(
    resolved: ResolvedSearchHit,
) -> tuple[str, int] | None:
    if resolved.source_kind != "code":
        return None
    if not isinstance(resolved.section_kind, str) or not resolved.section_kind.startswith("code_"):
        return None
    chunk_kind = resolved.section_kind.removeprefix("code_")
    if not chunk_kind:
        return None
    section_id = resolved.section_id
    if not isinstance(section_id, str) or not section_id.isdecimal():
        return None
    chunk_index = int(section_id)
    if str(chunk_index) != section_id or chunk_index > _SQLITE_SIGNED_INTEGER_MAX:
        return None
    return chunk_kind, chunk_index


def _semantic_source_version(resolved: ResolvedSearchHit) -> int | None:
    source_revision: object = resolved.source_revision
    if not isinstance(source_revision, Mapping):
        return None
    version_id = source_revision.get("version_id")
    if (
        isinstance(version_id, bool)
        or not isinstance(version_id, int)
        or not 0 < version_id <= _SQLITE_SIGNED_INTEGER_MAX
    ):
        return None
    return version_id


def _semantic_hit_binding(
    resolved: ResolvedSearchHit,
) -> tuple[str, str, str, int] | None:
    semantic_item_id = resolved.hit.item_id
    if not isinstance(semantic_item_id, str) or not semantic_item_id.strip():
        return None
    indexed_model_signature = resolved.hit.indexed_model_signature
    if not isinstance(indexed_model_signature, str) or not indexed_model_signature.strip():
        return None
    vector_space = resolved.hit.vector_space
    if not isinstance(vector_space, str) or not vector_space.strip():
        return None
    generation_id = resolved.hit.generation_id
    if (
        isinstance(generation_id, bool)
        or not isinstance(generation_id, int)
        or not 0 < generation_id <= _SQLITE_SIGNED_INTEGER_MAX
    ):
        return None
    return semantic_item_id, indexed_model_signature, vector_space, generation_id


def _semantic_code_resolution(
    resolved: ResolvedSearchHit,
) -> _SemanticCodeResolution | None:
    locator = _semantic_chunk_locator(resolved)
    if locator is None:
        return None
    version_id = _semantic_source_version(resolved)
    if version_id is None:
        return None
    binding = _semantic_hit_binding(resolved)
    if binding is None:
        return None
    chunk_kind, chunk_index = locator
    semantic_item_id, indexed_model_signature, vector_space, generation_id = binding
    return _SemanticCodeResolution(
        source_identity=resolved.source_identity,
        chunk_kind=chunk_kind,
        chunk_index=chunk_index,
        version_id=version_id,
        semantic_item_id=semantic_item_id,
        indexed_model_signature=indexed_model_signature,
        vector_space=vector_space,
        generation_id=generation_id,
        score=resolved.hit.score,
        snippet=resolved.snippet or "",
    )


def _resolve_semantic_code_row(
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
    resolved: _SemanticCodeResolution,
) -> _SearchRow | None:
    row = connection.execute(
        f"""SELECT v.version_id,f.current_path,{_project_sql()},v.language,
        v.artifact_kind,s.qualified_name,s.signature,c.start_line,c.end_line,
        ?,v.size,v.mtime_ns,v.analysis_status,?
        FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
        JOIN code_chunks c ON c.version_id=v.version_id
        JOIN embedding_links e ON e.chunk_id=c.chunk_id
        LEFT JOIN symbols s ON s.symbol_id=c.symbol_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        AND f.volume_id || ':' || f.physical_file_id=?
        AND v.version_id=? AND c.chunk_index=? AND c.kind=?
        AND e.semantic_item_id=? AND e.model_signature=?
        AND e.vector_space=? AND e.generation_id=? AND e.active=1
        AND {_COMMON_FILTER}
        LIMIT 1""",
        (
            resolved.snippet,
            f"semantic:{resolved.indexed_model_signature}:{resolved.score:.8f}:"
            f"generation={resolved.generation_id}:space={resolved.vector_space}:"
            "calibration=uncalibrated",
            resolved.source_identity,
            resolved.version_id,
            resolved.chunk_index,
            resolved.chunk_kind,
            resolved.semantic_item_id,
            resolved.indexed_model_signature,
            resolved.vector_space,
            resolved.generation_id,
            *_filter_parameters(query),
        ),
    ).fetchone()
    return None if row is None else _SearchRow.from_sql(row)


def _semantic_rows(
    code_database: Path,
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
    fetch_limit: int,
    cancellation: SQLiteCancellationBridge,
    *,
    model_cache: Path | None,
    threads: int | None,
) -> tuple[_SearchRow, ...]:
    """Resolve optional text-vector hits back to current structured code rows."""

    if not _semantic_search_ready(code_database, connection, query):
        return ()
    hits = _semantic_ranked_hits(
        code_database,
        query,
        fetch_limit,
        cancellation,
        model_cache=model_cache,
        threads=threads,
    )
    rows: list[_SearchRow] = []
    for position, hit in enumerate(hits, start=1):
        if position % _CANCELLATION_BATCH_ROWS == 0:
            cancellation.checkpoint()
        resolution = _semantic_code_resolution(hit)
        if resolution is None:
            continue
        row = _resolve_semantic_code_row(connection, query, resolution)
        if row is not None:
            rows.append(row)
    return tuple(rows)


# endregion [02]


# region [03] Public hybrid ranking


def _add_search_cleanup_note(
    primary: BaseException,
    cleanup_error: BaseException,
    *,
    label: str,
) -> None:
    primary.add_note(f"{label} failed: {type(cleanup_error).__name__}: {cleanup_error}")


def _cleanup_search_connection(
    connection: sqlite3.Connection,
    primary_error: BaseException | None,
    *,
    close: bool = True,
) -> None:
    cleanup_error = primary_error
    should_rollback = False
    try:
        should_rollback = bool(connection.in_transaction)
    except BaseException as exc:
        if cleanup_error is None:
            cleanup_error = exc
        else:
            _add_search_cleanup_note(
                cleanup_error,
                exc,
                label="code search transaction-state cleanup",
            )
    if should_rollback:
        try:
            connection.rollback()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            else:
                _add_search_cleanup_note(
                    cleanup_error,
                    exc,
                    label="code search rollback cleanup",
                )
    if close:
        try:
            connection.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            else:
                _add_search_cleanup_note(
                    cleanup_error,
                    exc,
                    label="code search connection close cleanup",
                )
    if primary_error is None and cleanup_error is not None:
        raise cleanup_error


def _search_rows_for_mode(
    code_database: Path,
    connection: sqlite3.Connection,
    query: CodeSearchQuery,
    mode: str,
    fetch_limit: int,
    cancellation: SQLiteCancellationBridge,
    *,
    semantic_model_cache: Path | None,
    semantic_threads: int | None,
) -> tuple[_SearchRow, ...]:
    if mode in {"literal", "fts", "path", "language"}:
        return _text_rows(connection, query, mode, fetch_limit)
    if mode in {"symbol", "definition", "signature", "complexity"}:
        return _symbol_rows(connection, query, mode, fetch_limit)
    if mode in {"reference", "import", "call"}:
        return _reference_rows(connection, query, mode, fetch_limit)
    if mode == "dependency":
        return _dependency_rows(connection, query, fetch_limit)
    if mode == "diagnostic":
        return _diagnostic_rows(connection, query, fetch_limit)
    if mode == "semantic":
        return _semantic_rows(
            code_database,
            connection,
            query,
            fetch_limit,
            cancellation,
            model_cache=semantic_model_cache,
            threads=semantic_threads,
        )
    raise AssertionError(f"unhandled code search mode: {mode}")


def _search_rankings(
    path: Path,
    query: CodeSearchQuery,
    modes: tuple[str, ...],
    fetch_limit: int,
    cancellation: SQLiteCancellationBridge,
    *,
    semantic_model_cache: Path | None,
    semantic_threads: int | None,
) -> tuple[tuple[str, tuple[_SearchRow, ...]], ...]:
    rankings: list[tuple[str, tuple[_SearchRow, ...]]] = []
    with readonly_code_database(
        path,
        connect=connect_code_state,
        close_connection=False,
    ) as connection:
        primary_error: BaseException | None = None
        try:
            connection.execute("BEGIN")
            with sqlite_cancellation_scope(connection, cancellation):
                for mode in modes:
                    rows = _search_rows_for_mode(
                        path,
                        connection,
                        query,
                        mode,
                        fetch_limit,
                        cancellation,
                        semantic_model_cache=semantic_model_cache,
                        semantic_threads=semantic_threads,
                    )
                    rankings.append((mode, rows))
                    cancellation.checkpoint()
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            _cleanup_search_connection(connection, primary_error)
    return tuple(rankings)


def _rank_search_rows(
    rankings: tuple[tuple[str, tuple[_SearchRow, ...]], ...],
    query: CodeSearchQuery,
    cancellation: SQLiteCancellationBridge,
) -> tuple[CodeSearchHit, ...]:
    score: defaultdict[tuple[object, ...], float] = defaultdict(float)
    match_types: defaultdict[tuple[object, ...], list[str]] = defaultdict(list)
    evidence: defaultdict[tuple[object, ...], list[str]] = defaultdict(list)
    relations: defaultdict[tuple[object, ...], dict[tuple[str, int], CodeSearchRelation]] = (
        defaultdict(dict)
    )
    projected: dict[tuple[object, ...], _SearchRow] = {}
    fused_rows = 0
    for mode, rows in rankings:
        weight = _MODE_WEIGHT[mode]
        for rank, row in enumerate(rows, start=1):
            fused_rows += 1
            if fused_rows % _CANCELLATION_BATCH_ROWS == 0:
                cancellation.checkpoint()
            key = (row.version_id, row.symbol, row.start_line, row.end_line)
            projected.setdefault(key, row)
            score[key] += weight / (60.0 + rank)
            match_types[key].append(mode)
            evidence[key].append(row.evidence)
            if row.relation is not None:
                relation_key = (row.relation.source_table, row.relation.source_row_id)
                relations[key].setdefault(relation_key, row.relation)

    cancellation.checkpoint()
    ordered = sorted(
        projected,
        key=lambda key: (-score[key], projected[key].path.casefold(), key),
    )[: query.limit]
    cancellation.checkpoint()
    hits: list[CodeSearchHit] = []
    for position, ordered_key in enumerate(ordered, start=1):
        if position % _CANCELLATION_BATCH_ROWS == 0:
            cancellation.checkpoint()
        hits.append(
            CodeSearchHit(
                path=projected[ordered_key].path,
                project=projected[ordered_key].project,
                language=projected[ordered_key].language,
                artifact_kind=projected[ordered_key].artifact_kind,
                symbol=projected[ordered_key].symbol,
                signature=projected[ordered_key].signature,
                start_line=projected[ordered_key].start_line,
                end_line=projected[ordered_key].end_line,
                snippet=projected[ordered_key].snippet,
                score=score[ordered_key],
                match_types=tuple(dict.fromkeys(match_types[ordered_key])),
                evidence=tuple(dict.fromkeys(evidence[ordered_key])),
                version_id=projected[ordered_key].version_id,
                observed_size=projected[ordered_key].size,
                observed_mtime_ns=projected[ordered_key].mtime_ns,
                analysis_status=projected[ordered_key].status,
                relations=tuple(
                    sorted(
                        relations[ordered_key].values(),
                        key=lambda relation: (
                            relation.family,
                            relation.source_table,
                            relation.source_row_id,
                        ),
                    )
                ),
            )
        )
    cancellation.checkpoint()
    return tuple(hits)


def search_code(
    path: Path,
    query: CodeSearchQuery,
    *,
    semantic_model_cache: Path | None = None,
    semantic_threads: int | None = None,
    cancellation_check: CancellationCheck | None = None,
) -> tuple[CodeSearchHit, ...]:
    """Return explained current hits using reciprocal-rank signal fusion.

    ``semantic`` is deliberately not fabricated from lexical signals. It
    contributes only when the published Semantic head has an exact active link
    to the current Code chunk; exact and structural modes remain independently
    usable.
    """

    cancellation = SQLiteCancellationBridge(cancellation_check)
    cancellation.checkpoint()
    modes = _mode_plan(query)
    fetch_limit = min(5000, max(query.limit * 8, 64))
    rankings = _search_rankings(
        path,
        query,
        modes,
        fetch_limit,
        cancellation,
        semantic_model_cache=semantic_model_cache,
        semantic_threads=semantic_threads,
    )
    return _rank_search_rows(rankings, query, cancellation)


def available_search_modes() -> tuple[str, ...]:
    return tuple(sorted(SEARCH_MODES))


# endregion [03]


__all__ = ["SEARCH_MODES", "available_search_modes", "search_code"]
