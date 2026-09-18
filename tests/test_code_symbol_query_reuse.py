"""One symbol selection preserves both public signals and their work budgets."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.code.code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
    CodeSearchQuery,
    SourceRange,
    SymbolRecord,
)
from neocortex.code.code_state import CodeState
from neocortex.code.search import code_search
from neocortex.deduplication import FileSnapshot
from neocortex.persistence.sqlite_cancellation import sqlite_cancellation_scope
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.semantic.semantic_models import fingerprint_bytes, fingerprint_text


def _database(tmp_path: Path, symbols_per_file: int = 3) -> Path:
    database = tmp_path / "code.sqlite3"
    with CodeState(database) as state:
        for file_id, (module, language, project) in enumerate(
            (("alpha", "python", "first-project"), ("beta", "rust", "second-project")), 1,
        ):
            path = tmp_path / f"{module}.code"
            lines = tuple(f"function_{index:03}()\n" for index in range(symbols_per_file))
            text = "".join(lines)
            raw = text.encode("utf-8")
            fingerprint = fingerprint_text(text)
            raw_fingerprint = fingerprint_bytes(raw)
            symbols = tuple(
                SymbolRecord(
                    "function", f"function_{index:03}", f"{module}.function_{index:03}",
                    f"function_{index:03}()", SourceRange(
                        index + 1, 0, index + 1, len(line) - 1,
                        index * len(line), (index + 1) * len(line) - 1,
                    ), complexity=index % 2,
                )
                for index, line in enumerate(lines)
            )
            analysis = CodeAnalysis(
                input=CodeFileInput(
                    snapshot=FileSnapshot(str(path), 1, file_id, len(raw), 100, 50),
                    text=text, raw_bytes=raw, encoding="utf-8",
                    classification=ArtifactClassification(
                        language, ArtifactKind.SOURCE, 1.0, ("symbol-query-fixture",),
                    ), processing_signature="symbol-query-fixture",
                ),
                status=AnalysisStatus.COMPLETE,
                analyzer_id="symbol-query-fixture", analyzer_version="1", parser_kind="fixture",
                text_xxh3_128=fingerprint.xxh3_128,
                text_xxh3_64_guard=fingerprint.xxh3_64_guard,
                normalized_xxh3_128=fingerprint.xxh3_128,
                token_xxh3_128=None, structure_xxh3_128=None,
                raw_xxh3_128=raw_fingerprint.xxh3_128,
                raw_xxh3_64_guard=raw_fingerprint.xxh3_64_guard,
                symbols=symbols,
            )
            version, _ = state.store_analysis(analysis, 1)
            state.connection.execute(
                "INSERT INTO projects(project_id,project_key,name,ecosystem,confidence,"
                "evidence_json,first_seen_run_id,last_seen_run_id,status) "
                "VALUES(?,?,?,'fixture',1,'[]',1,1,'current')",
                (file_id, project, project),
            )
            state.connection.execute(
                "INSERT INTO project_memberships(project_id,version_id,proposed_path,relation,"
                "confidence,selected,evidence_json) VALUES(?,?,?,'manifest',1,1,'[]')",
                (file_id, version, str(path)),
            )
        state.connection.commit()
    return database


def _trace_reads(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[sqlite3.Connection]]:
    statements: list[str] = []
    connections: list[sqlite3.Connection] = []
    original = code_search.readonly_code_database

    @contextmanager
    def traced(*args, **kwargs) -> Iterator[sqlite3.Connection]:
        with original(*args, **kwargs) as connection:
            connections.append(connection)
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(code_search, "readonly_code_database", traced)
    return statements, connections


def _symbol_selections(statements: list[str]) -> list[str]:
    return [statement for statement in statements if "FROM symbols s" in statement]


def _separate_mode_rankings(
    path, query, modes, fetch_limit, cancellation, *,
    semantic_model_cache, semantic_threads, row_admission=None,
):
    """Prior query flow: select every requested mode independently."""

    with code_search.readonly_code_database(path) as connection:
        connection.execute("BEGIN")
        with sqlite_cancellation_scope(connection, cancellation):
            rankings = []
            for mode in modes:
                rankings.append((mode, code_search._search_rows_for_mode(
                    path, connection, query, mode, fetch_limit, cancellation,
                    semantic_model_cache=semantic_model_cache,
                    semantic_threads=semantic_threads, row_admission=row_admission,
                )))
                cancellation.checkpoint()
    return tuple(rankings)


@pytest.mark.parametrize("query", (
    CodeSearchQuery(text="function", modes=("symbol", "definition")),
    CodeSearchQuery(text="function", modes=("definition", "symbol")),
    CodeSearchQuery(text="function", modes=("symbol", "literal", "definition")),
    CodeSearchQuery(text="function", modes=("hybrid",)),
    CodeSearchQuery(text="absent", symbol="function_001", modes=("definition", "symbol")),
    CodeSearchQuery(text="function", path="alpha", modes=("symbol", "definition")),
    CodeSearchQuery(text="function", language="PYTHON", modes=("definition", "symbol")),
    CodeSearchQuery(text="function", project="first-project", modes=("symbol", "definition")),
    CodeSearchQuery(text="function", project="missing", modes=("symbol", "definition")),
    CodeSearchQuery(text="function", modes=("definition", "symbol"), limit=2),
    CodeSearchQuery(text="", modes=("symbol", "definition")),
))
def test_symbol_reuse_preserves_complete_public_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: CodeSearchQuery,
) -> None:
    database = _database(tmp_path)
    actual_charges: list[int] = []
    actual = code_search.search_code(database, query, row_admission=actual_charges.append)
    monkeypatch.setattr(code_search, "_search_rankings", _separate_mode_rankings)
    expected_charges: list[int] = []
    expected = code_search.search_code(database, query, row_admission=expected_charges.append)
    assert actual == expected
    assert actual_charges == expected_charges


@pytest.mark.parametrize("modes", (("symbol", "definition"), ("definition", "symbol")))
def test_symbol_pair_selects_once_and_retains_both_rrf_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, modes: tuple[str, ...],
) -> None:
    database = _database(tmp_path)
    statements, _ = _trace_reads(monkeypatch)
    charges: list[int] = []
    hits = code_search.search_code(
        database, CodeSearchQuery(text="function", modes=modes), row_admission=charges.append,
    )
    assert len(_symbol_selections(statements)) == 1
    assert len(hits) == 6
    assert charges == [1] * 12
    weights = {"symbol": 5.0, "definition": 6.0}
    for rank, hit in enumerate(hits, 1):
        assert hit.match_types == modes
        assert hit.evidence == tuple(f"{mode}:function" for mode in modes)
        assert hit.score == sum(weights[mode] / (60.0 + rank) for mode in modes)


def test_symbol_reuse_does_not_survive_another_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    query = CodeSearchQuery(text="function", modes=("symbol", "definition"))
    statements, _ = _trace_reads(monkeypatch)
    original = code_search.search_code(database, query)
    with CodeState(database) as state:
        state.connection.execute("UPDATE symbols SET signature='changed signature'")
        state.connection.commit()
    updated = code_search.search_code(database, query)
    assert len(_symbol_selections(statements)) == 2
    assert len(updated) == len(original) == 6
    assert all(hit.signature == "changed signature" for hit in updated)
    assert updated != original


def test_row_budget_failure_in_second_symbol_signal_closes_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    statements, connections = _trace_reads(monkeypatch)
    remaining = 7
    error = RuntimeError("second symbol signal exceeds row budget")

    def admit(count: int) -> None:
        nonlocal remaining
        if count > remaining:
            raise error
        remaining -= count

    query = CodeSearchQuery(text="function", modes=("symbol", "definition"))
    with pytest.raises(RuntimeError) as caught:
        code_search.search_code(database, query, row_admission=admit)
    assert caught.value is error and remaining == 0
    assert len(_symbol_selections(statements)) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[-1].execute("SELECT 1")
    assert len(code_search.search_code(database, query)) == 6


def test_cancellation_during_second_symbol_signal_closes_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path, symbols_per_file=160)
    statements, connections = _trace_reads(monkeypatch)
    admitted = 0
    error = CancellationRequested("cancel during second symbol signal")

    def admit(count: int) -> None:
        nonlocal admitted
        admitted += count

    def cancel() -> None:
        if admitted > 320:
            raise error

    query = CodeSearchQuery(text="function", modes=("definition", "symbol"), limit=100)
    with pytest.raises(CancellationRequested) as caught:
        code_search.search_code(database, query, row_admission=admit, cancellation_check=cancel)
    assert caught.value is error
    assert 320 < admitted < 640
    assert len(_symbol_selections(statements)) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[-1].execute("SELECT 1")
    assert len(code_search.search_code(database, query)) == 100
