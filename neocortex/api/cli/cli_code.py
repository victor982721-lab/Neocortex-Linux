"""Direct CLI operations for the user-facing Code knowledge capability."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import TextIO


_CODE_SCOPE_NO_CANDIDATES = "code_scope_no_candidates"


def _path_key(path: str | Path) -> str:
    """Normalize a CLI path using the same lexical rule as Code admission."""

    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _path_contains(parent: str, candidate: str) -> bool:
    """Return whether ``candidate`` is ``parent`` or one of its descendants."""

    try:
        return os.path.commonpath((parent, candidate)) == parent
    except ValueError:
        # Distinct Windows drives have no common path.  The product is
        # Linux-only today, but keeping this branch makes the diagnostic
        # deterministic for legacy callers too.
        return False


def _quoted_path(path: str | Path) -> str:
    """Quote a user path so control characters cannot forge CLI lines."""

    return json.dumps(_path_key(path), ensure_ascii=False)


def _code_scope_feedback(
    *,
    root: str | Path,
    project_roots: Iterable[str | Path],
    candidate_scope: str,
) -> dict[str, str] | None:
    """Describe a deterministic Code no-op caused by an explicit root.

    ``ProjectCandidateScope`` admits paths only below its configured roots.
    When the inventory root is disjoint from every configured project root,
    ``projects`` cannot admit a candidate, even when the root contains valid
    source files.  Keep this as a small reusable CLI diagnostic so the parser
    and other CLI surfaces can report the same actionable reason without
    opening Code SQLite or walking the corpus.
    """

    if candidate_scope != "projects":
        return None
    root_key = _path_key(root)
    normalized_roots = tuple(_path_key(value) for value in project_roots)
    # An empty explicit collection preserves ProjectCandidateScope's marker
    # discovery fallback, so it cannot prove a zero-candidate outcome.
    if not normalized_roots:
        return None
    if any(
        _path_contains(root_key, project_root) or _path_contains(project_root, root_key)
        for project_root in normalized_roots
    ):
        return None
    root_text = _quoted_path(root)
    return {
        "code": _CODE_SCOPE_NO_CANDIDATES,
        "severity": "warning",
        "message": (
            "Code scope=projects would admit 0 candidates for explicit --root "
            f"{root_text}: the root does not overlap the configured project roots. "
            "Use --code-project-root PATH for that project, or "
            "--code-scope broad for an intentional broad scan."
        ),
    }


def _state_path(args: argparse.Namespace) -> Path:
    return Path(args.state_directory) / "code.sqlite3"


def _print_console_line(value: str, *, file: TextIO | None = None) -> None:
    import sys

    print(value, file=sys.stdout if file is None else file)


def _emit(value: object, *, json_output: bool) -> None:
    if json_output:
        _print_console_line(json.dumps(value, ensure_ascii=True, sort_keys=True))
    else:
        _print_console_line(str(value))


def _error(operation: str, exc: BaseException) -> int:
    import sys

    _print_console_line(
        f"ERROR {operation} {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    return 2


def _status_payload(path: Path) -> dict[str, object]:
    from neocortex.code.ingestion.code_analyzers import builtin_analyzer_registry
    from neocortex.code.code_schema import code_database, validate_code_schema

    payload: dict[str, object] = {
        "kind": "code-status",
        "schema": "neocortex.code-status/v2",
        "database": str(path),
        "exists": path.is_file(),
        "analyzers": builtin_analyzer_registry().status(),
    }
    if not path.is_file():
        payload["state"] = "not_initialized"
        payload["counts"] = {}
        payload["latest_run"] = None
        return payload
    with code_database(path, readonly=True) as connection:
        validate_code_schema(connection)
        payload["state"] = "ready"
        payload["schema_version"] = int(connection.execute("PRAGMA user_version").fetchone()[0])
        payload["counts"] = {
            "current_files": int(
                connection.execute("SELECT COUNT(*) FROM files WHERE status='current'").fetchone()[0]
            ),
            "versions": int(connection.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]),
            "current_symbols": int(
                connection.execute(
                    """SELECT COUNT(*) FROM symbols s JOIN file_versions v
                    ON v.version_id=s.version_id WHERE v.invalidated_ns IS NULL"""
                ).fetchone()[0]
            ),
            "current_references": int(
                connection.execute(
                    """SELECT COUNT(*) FROM code_references r JOIN file_versions v
                    ON v.version_id=r.version_id WHERE v.invalidated_ns IS NULL"""
                ).fetchone()[0]
            ),
            "current_diagnostics": int(
                connection.execute(
                    """SELECT COUNT(*) FROM diagnostics d JOIN file_versions v
                    ON v.version_id=d.version_id WHERE v.invalidated_ns IS NULL"""
                ).fetchone()[0]
            ),
            "projects": int(
                connection.execute("SELECT COUNT(*) FROM projects WHERE status='current'").fetchone()[0]
            ),
        }
        row = connection.execute(
            """SELECT analysis_run_id,scan_id,status,started_ns,completed_ns,
            candidates,processed,cache_hits,errors
            FROM analysis_runs ORDER BY analysis_run_id DESC LIMIT 1"""
        ).fetchone()
        payload["latest_run"] = None if row is None else dict(row)
    return payload


def run_code_status(args: argparse.Namespace) -> int:
    """Show product Code index state without development-audit evidence."""

    try:
        payload = _status_payload(_state_path(args))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _error("code-status", exc)
    if args.code_json:
        _emit(payload, json_output=True)
    else:
        counts = payload.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        _print_console_line(
            "CODE_STATUS "
            f"state={payload.get('state')} "
            + " ".join(f"{name}={value}" for name, value in counts.items())
        )
        latest = payload.get("latest_run")
        if isinstance(latest, dict):
            _print_console_line(
                "CODE_RUN "
                f"id={latest.get('analysis_run_id')} status={latest.get('status')} "
                f"candidates={latest.get('candidates', 0)} processed={latest.get('processed', 0)} "
                f"cache_hits={latest.get('cache_hits', 0)} errors={latest.get('errors', 0)}"
            )
    return 0


def run_code_search(args: argparse.Namespace) -> int:
    from neocortex.code.code_contracts import CodeSearchQuery
    from neocortex.code.search.code_search import search_code
    from neocortex.code.search.code_semantic_links import code_semantic_search_availability

    try:
        query = CodeSearchQuery(
            text=args.code_search,
            modes=tuple(args.code_search_mode or ("hybrid",)),
            path=args.code_path,
            language=args.code_language,
            project=args.code_project,
            symbol=args.code_symbol,
            diagnostic=args.code_diagnostic,
            minimum_complexity=args.code_min_complexity,
            limit=args.code_search_limit,
        )
        semantic_requested = any(mode in {"semantic", "hybrid"} for mode in query.modes)
        semantic_availability = (
            code_semantic_search_availability(
                args.state_directory,
                model_cache_override=args.semantic_model_cache,
            )
            if semantic_requested
            else None
        )
        hits = search_code(
            _state_path(args),
            query,
            semantic_model_cache=args.semantic_model_cache,
            semantic_threads=args.semantic_threads,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _error("code-search", exc)
    if semantic_availability is not None:
        semantic_payload = {
            "kind": "code-search-channel",
            "channel": "semantic",
            **asdict(semantic_availability),
        }
        if args.code_json:
            _emit(semantic_payload, json_output=True)
        else:
            _print_console_line(
                "CODE_SEARCH_CHANNEL name=semantic "
                f"available={int(semantic_availability.available)} "
                f"reason={semantic_availability.reason} "
                f"generation={semantic_availability.generation_id or '-'} "
                f"current_links={semantic_availability.current_links} "
                f"calibration={semantic_availability.calibration}"
            )
    for hit in hits:
        if args.code_json:
            _emit({"kind": "code-search-hit", **asdict(hit)}, json_output=True)
        else:
            _print_console_line(
                f"CODE_HIT score={hit.score:.6f} matches={','.join(hit.match_types)} "
                f"language={hit.language or '-'} project={hit.project or '-'} "
                f"path={json.dumps(hit.path, ensure_ascii=False)} "
                f"lines={hit.start_line}-{hit.end_line} "
                f"symbol={json.dumps(hit.symbol, ensure_ascii=False)} "
                f"snippet={json.dumps(hit.snippet, ensure_ascii=False)}"
            )
    if (
        semantic_availability is not None
        and query.modes == ("semantic",)
        and not semantic_availability.available
    ):
        return 2
    return 0


def run_code_projects(args: argparse.Namespace) -> int:
    from neocortex.code.ingestion.code_projects import list_projects

    try:
        projects = list_projects(_state_path(args))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _error("code-projects", exc)
    for project in projects:
        if args.code_json:
            _emit({"kind": "code-project", **asdict(project)}, json_output=True)
        else:
            _print_console_line(
                f"CODE_PROJECT id={project.project_id} name={json.dumps(project.name)} "
                f"ecosystem={project.ecosystem} status={project.status} "
                f"confidence={project.confidence:.3f} current={project.current_files} "
                f"historical={project.historical_files} "
                f"root={json.dumps(project.probable_root, ensure_ascii=False)}"
            )
    return 0


def run_code_reconstruct(args: argparse.Namespace) -> int:
    from neocortex.code.ingestion.code_projects import reconstruct_project

    project: str | int = args.code_reconstruct
    if str(project).isdigit():
        project = int(project)
    try:
        manifest = reconstruct_project(
            _state_path(args),
            project,
            strategy=args.code_reconstruct_strategy,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError, LookupError) as exc:
        return _error("code-reconstruct", exc)
    if args.code_json:
        _emit({"kind": "code-reconstruction", **asdict(manifest)}, json_output=True)
        return 0
    _print_console_line(
        f"CODE_RECONSTRUCTION project_id={manifest.project_id} "
        f"name={json.dumps(manifest.project_name)} ecosystem={manifest.ecosystem} "
        f"strategy={manifest.strategy} conflicts={len(manifest.conflicts)}"
    )
    for entry in manifest.entries:
        _print_console_line(
            f"CODE_RECONSTRUCTION_ENTRY selected={str(entry.selected).lower()} "
            f"confidence={entry.confidence:.3f} relation={entry.relation} "
            f"proposed={json.dumps(entry.proposed_path)} "
            f"source={json.dumps(entry.source_path, ensure_ascii=False)} "
            f"version={entry.version_id} xxh3_128={entry.xxh3_128} "
            f"conflict={entry.conflict_group or '-'}"
        )
    for conflict in manifest.conflicts:
        _print_console_line(f"CODE_RECONSTRUCTION_CONFLICT {conflict}")
    return 0


__all__ = [
    "run_code_projects",
    "run_code_reconstruct",
    "run_code_search",
    "run_code_status",
]
