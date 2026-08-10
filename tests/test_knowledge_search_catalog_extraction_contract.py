"""Tests-first extraction contracts for the Knowledge Search catalog channel."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import pickle
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from _04_Nucleo_Operativo import knowledge_search
from _04_Nucleo_Operativo.knowledge_contracts import (
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    PublicationHead,
    RevisionState,
)
from _04_Nucleo_Operativo.knowledge_snapshot import KnowledgeStatePaths


PUBLIC_MODULE = "_04_Nucleo_Operativo.knowledge_search"
CATALOG_MODULE = "_04_Nucleo_Operativo.knowledge_search_catalog"
DOCUMENT_CATALOG_MODULE = "_04_Nucleo_Operativo.document_catalog"
CATALOG_DELEGATES = (
    "_escape_like",
    "_catalog_identifiers",
    "_catalog_ranking",
)
CATALOG_IMPLEMENTATIONS = {
    "_escape_like": "_catalog_escape_like_impl",
    "_catalog_identifiers": "_catalog_identifiers_impl",
    "_catalog_ranking": "_catalog_ranking_impl",
}
EXPECTED_SIGNATURES = {
    "_escape_like": "(value: 'str') -> 'str'",
    "_catalog_identifiers": ("(value: 'object') -> 'tuple[tuple[str, str], ...]'"),
    "_catalog_ranking": (
        "(paths: 'KnowledgeStatePaths', plan: 'KnowledgePlan', "
        "snapshot: 'KnowledgeSnapshot', *, "
        "cancellation_check: 'Callable[[], None] | None' = None) -> "
        "'tuple[tuple[KnowledgeCandidate, ...], RankingExecution]'"
    ),
}
LATE_BOUND_GLOBALS = {
    "_escape_like": set(),
    "_catalog_identifiers": {"json"},
    "_catalog_ranking": {
        "_owner_available",
        "RankingExecution",
        "_LEXICAL_OWNER_FORMATS",
        "_escape_like",
        "_planned_candidate_limit",
        "MAX_KNOWLEDGE_CANDIDATES",
        "SQLiteCancellationBridge",
        "document_catalog_database",
        "sqlite_cancellation_scope",
        "sqlite3",
        "_reraise_captured_cancellation",
        "_cleanup_preserving_primary",
        "_decimal_identity_value",
        "FileIdentity",
        "FileIdentityEncoding",
        "FileIdentityError",
        "ValueError",
        "_direct_resource_ref",
        "canonical_json",
        "fingerprint_text",
        "RevisionRef",
        "RevisionState",
        "_catalog_identifiers",
        "json",
        "EvidenceRef",
        "EvidenceMethod",
        "KnowledgeCandidate",
        "RankingSignal",
    },
}

_FILE_KEY = "00000000000000000000000000000001:00000000000000000000000000000002"


def _snapshot(
    *heads: tuple[str, int],
    available: bool = True,
) -> KnowledgeSnapshot:
    publications = tuple(
        PublicationHead(scope, f"catalog:{generation}", generation)
        for scope, generation in heads
    )
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-30T12:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot("pdf", OwnerAvailability.ABSENT, 11),
            OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
            OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
            OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
            OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
            OwnerSnapshot("code", OwnerAvailability.ABSENT, 2),
            OwnerSnapshot(
                "catalog",
                (
                    OwnerAvailability.AVAILABLE
                    if available
                    else OwnerAvailability.ABSENT
                ),
                6,
                6 if available else None,
                publications=publications,
            ),
            OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
        ),
    )


def _plan(
    *,
    source_kinds: tuple[str, ...] = (),
    formats: tuple[str, ...] = (),
    project: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Any:
    return SimpleNamespace(
        source_kinds=source_kinds,
        formats=formats,
        project=project,
        date_from=date_from,
        date_to=date_to,
    )


def _row(
    index: int = 1,
    **overrides: object,
) -> dict[str, object]:
    volume_id = index * 2 - 1
    file_id = index * 2
    row: dict[str, object] = {
        "source_kind": "pdf",
        "file_key": f"{volume_id:032x}:{file_id:032x}",
        "path": f"C:/docs/catalog-{index}.pdf",
        "volume_id": str(volume_id),
        "file_id": str(file_id),
        "birthtime_ns": 9 + index,
        "size": 100,
        "mtime_ns": 20,
        "processing_signature": "catalog-fixture-v1",
        "classifier_signature": "classifier-v6:test",
        "primary_kind": "study",
        "primary_subtype": "coordination",
        "primary_project": "Alpha",
        "confidence": 0.9,
        "uncertainty": "baja",
        "standard_references_json": (
            '[" IEC 61850 ",{"identifier":"NFPA 70E"},"IEC 61850"]'
        ),
        "source_status": "done",
        "catalog_status": "classified",
        "updated_ns": 40,
        "last_seen_catalog_run_id": 7,
        "generation_id": 1,
    }
    row.update(overrides)
    return row


def _head_row(
    source_kind: str,
    generation: int,
    *,
    status: str | None = "published",
    actual_kind: str | None = None,
) -> dict[str, object]:
    return {
        "source_kind": source_kind,
        "generation_id": generation,
        "status": status,
        "actual_kind": source_kind if actual_kind is None and status else actual_kind,
    }


class _Cursor:
    def __init__(
        self,
        rows: tuple[dict[str, object], ...],
        events: list[str],
        label: str,
    ) -> None:
        self.rows = rows
        self.events = events
        self.label = label

    def fetchall(self) -> tuple[dict[str, object], ...]:
        self.events.append(f"FETCH:{self.label}")
        return self.rows


class _CatalogConnection:
    def __init__(
        self,
        *,
        preflight_rows: tuple[dict[str, object], ...] = (),
        candidate_rows: tuple[dict[str, object], ...] = (),
        interrupted: BaseException | None = None,
    ) -> None:
        self.preflight_rows = preflight_rows
        self.candidate_rows = candidate_rows
        self.interrupted = interrupted
        self.events: list[str] = []
        self.queries: list[tuple[str, str, tuple[object, ...]]] = []
        self.handler: object = None

    def set_progress_handler(self, callback: object, instructions: int) -> None:
        self.handler = callback
        self.events.append("CLEAR" if callback is None else f"SET:{instructions}")

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> _Cursor:
        is_preflight = "g.source_kind AS actual_kind" in statement
        label = "PREFLIGHT" if is_preflight else "QUERY"
        self.events.append(label)
        self.queries.append((label, statement, tuple(parameters)))
        if not is_preflight and self.interrupted is not None:
            assert callable(self.handler)
            result = self.handler()
            self.events.append(f"progress:{result}")
            raise self.interrupted
        rows = self.preflight_rows if is_preflight else self.candidate_rows
        return _Cursor(rows, self.events, label)


class _CatalogManager:
    def __init__(
        self,
        connection: _CatalogConnection,
        events: list[str],
        close_error: BaseException | None,
    ) -> None:
        self.connection = connection
        self.events = events
        self.close_error = close_error

    def __enter__(self) -> _CatalogConnection:
        self.events.append("ENTER")
        return self.connection

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> bool:
        self.events.append("CLOSE")
        if self.close_error is not None:
            raise self.close_error
        return False


def _install_catalog_database(
    monkeypatch: pytest.MonkeyPatch,
    connection: _CatalogConnection,
    *,
    target_limit: int = 2,
    close_error: BaseException | None = None,
) -> list[str]:
    manager_events: list[str] = []

    def database(path: Path, *, readonly: bool) -> _CatalogManager:
        manager_events.append(f"OPEN:{path}:{readonly}")
        return _CatalogManager(connection, manager_events, close_error)

    monkeypatch.setattr(knowledge_search, "document_catalog_database", database)
    monkeypatch.setattr(
        knowledge_search,
        "_planned_candidate_limit",
        lambda _plan_value, channel: (
            target_limit
            if channel == "catalog"
            else pytest.fail(f"unexpected channel: {channel}")
        ),
    )
    return manager_events


# region [01] Facade identity, late binding, DAG, and cold imports


@pytest.mark.parametrize("name", CATALOG_DELEGATES)
def test_catalog_facade_signatures_metadata_and_pickle_are_stable(name: str) -> None:
    seam = getattr(knowledge_search, name)
    assert str(inspect.signature(seam)) == EXPECTED_SIGNATURES[name]
    assert seam.__module__ == PUBLIC_MODULE
    assert seam.__qualname__ == name
    assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam


@pytest.mark.parametrize("name", CATALOG_DELEGATES)
def test_catalog_facade_seams_are_thin_late_bound_delegates(name: str) -> None:
    seam = getattr(knowledge_search, name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(seam)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    statements = function.body
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    assert len(statements) == 1
    statement = statements[0]
    assert isinstance(statement, ast.Return)
    assert isinstance(statement.value, ast.Call)
    assert isinstance(statement.value.func, ast.Name)
    assert statement.value.func.id == CATALOG_IMPLEMENTATIONS[name]
    referenced_names = {
        node.id for node in ast.walk(function) if isinstance(node, ast.Name)
    }
    assert LATE_BOUND_GLOBALS[name] <= referenced_names


@pytest.mark.parametrize("name", CATALOG_DELEGATES)
def test_catalog_facade_seams_forward_exact_runtime_objects(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if name == "_escape_like":
        call_args = (object(),)
        call_kwargs: dict[str, object] = {}
        expected_kwargs: dict[str, object] = {}
    elif name == "_catalog_identifiers":
        call_args = (object(),)
        call_kwargs = {}
        expected_kwargs = {"json_loads_fn": json.loads}
    elif name == "_catalog_ranking":
        cancellation = object()
        call_args = (object(), object(), object())
        call_kwargs = {"cancellation_check": cancellation}
        expected_kwargs = {
            "cancellation_check": cancellation,
            "owner_available_fn": knowledge_search._owner_available,
            "ranking_execution_type": knowledge_search.RankingExecution,
            "lexical_owner_formats": knowledge_search._LEXICAL_OWNER_FORMATS,
            "escape_like_fn": knowledge_search._escape_like,
            "planned_candidate_limit_fn": knowledge_search._planned_candidate_limit,
            "max_candidates": knowledge_search.MAX_KNOWLEDGE_CANDIDATES,
            "cancellation_bridge_type": knowledge_search.SQLiteCancellationBridge,
            "document_catalog_database_fn": (
                knowledge_search.document_catalog_database
            ),
            "sqlite_cancellation_scope_fn": (
                knowledge_search.sqlite_cancellation_scope
            ),
            "sqlite_error_type": sqlite3.Error,
            "reraise_captured_cancellation_fn": (
                knowledge_search._reraise_captured_cancellation
            ),
            "cleanup_preserving_primary_fn": (
                knowledge_search._cleanup_preserving_primary
            ),
            "decimal_identity_value_fn": knowledge_search._decimal_identity_value,
            "file_identity_type": knowledge_search.FileIdentity,
            "file_identity_encoding": knowledge_search.FileIdentityEncoding.AUTO,
            "file_identity_error_type": knowledge_search.FileIdentityError,
            "value_error_type": ValueError,
            "direct_resource_ref_fn": knowledge_search._direct_resource_ref,
            "canonical_json_fn": knowledge_search.canonical_json,
            "fingerprint_text_fn": knowledge_search.fingerprint_text,
            "revision_ref_type": knowledge_search.RevisionRef,
            "revision_state_type": knowledge_search.RevisionState,
            "catalog_identifiers_fn": knowledge_search._catalog_identifiers,
            "json_loads_fn": json.loads,
            "evidence_ref_type": knowledge_search.EvidenceRef,
            "evidence_method_type": knowledge_search.EvidenceMethod,
            "knowledge_candidate_type": knowledge_search.KnowledgeCandidate,
            "ranking_signal_type": knowledge_search.RankingSignal,
        }
    else:
        raise AssertionError(name)

    marker = object()
    captured: dict[str, object] = {}

    def implementation(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return marker

    monkeypatch.setattr(
        knowledge_search,
        CATALOG_IMPLEMENTATIONS[name],
        implementation,
        raising=False,
    )
    result = getattr(knowledge_search, name)(*call_args, **call_kwargs)

    assert result is marker
    actual_args = captured["args"]
    actual_kwargs = captured["kwargs"]
    assert isinstance(actual_args, tuple)
    assert isinstance(actual_kwargs, dict)
    assert len(actual_args) == len(call_args)
    assert all(
        actual is expected
        for actual, expected in zip(actual_args, call_args, strict=True)
    )
    assert actual_kwargs.keys() == expected_kwargs.keys()
    assert all(actual_kwargs[key] is value for key, value in expected_kwargs.items())


def test_catalog_extraction_module_exists_without_facade_or_owner_cycle() -> None:
    spec = importlib.util.find_spec(CATALOG_MODULE)
    assert spec is not None
    assert spec.origin is not None
    module_path = Path(spec.origin)
    source = module_path.read_text(encoding="utf-8")
    assert len(source.splitlines()) <= 900
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert PUBLIC_MODULE not in imported_modules
    assert DOCUMENT_CATALOG_MODULE not in imported_modules
    assert not any(module.endswith(".knowledge_search") for module in imported_modules)
    assert not any(module.endswith(".document_catalog") for module in imported_modules)
    helper_names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert {"escape_like", "catalog_identifiers", "catalog_ranking"} <= helper_names


def test_catalog_modules_form_expected_normalized_relative_import_dag() -> None:
    modules = (PUBLIC_MODULE, CATALOG_MODULE, DOCUMENT_CATALOG_MODULE)
    edges: set[tuple[str, str]] = set()
    for module_name in modules:
        spec = importlib.util.find_spec(module_name)
        assert spec is not None
        assert spec.origin is not None
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        package = module_name.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = f"{package}.{node.module}" if node.level else node.module
                if imported in modules:
                    edges.add((module_name, imported))
            elif isinstance(node, ast.Import):
                edges.update(
                    (module_name, alias.name)
                    for alias in node.names
                    if alias.name in modules
                )
    assert edges == {
        (PUBLIC_MODULE, CATALOG_MODULE),
        (PUBLIC_MODULE, DOCUMENT_CATALOG_MODULE),
    }


@pytest.mark.parametrize(
    "module_order",
    (
        (PUBLIC_MODULE, CATALOG_MODULE, DOCUMENT_CATALOG_MODULE),
        (PUBLIC_MODULE, DOCUMENT_CATALOG_MODULE, CATALOG_MODULE),
        (CATALOG_MODULE, PUBLIC_MODULE, DOCUMENT_CATALOG_MODULE),
        (CATALOG_MODULE, DOCUMENT_CATALOG_MODULE, PUBLIC_MODULE),
        (DOCUMENT_CATALOG_MODULE, PUBLIC_MODULE, CATALOG_MODULE),
        (DOCUMENT_CATALOG_MODULE, CATALOG_MODULE, PUBLIC_MODULE),
    ),
)
def test_catalog_modules_support_all_six_cold_import_orders(
    module_order: tuple[str, str, str],
) -> None:
    repository = Path(knowledge_search.__file__).resolve().parents[1]
    script = textwrap.dedent(
        f"""
        import importlib
        import inspect
        import pickle
        import sys

        sys.path.insert(0, {str(repository)!r})
        for module_name in {module_order!r}:
            importlib.import_module(module_name)
        facade = importlib.import_module({PUBLIC_MODULE!r})
        helper = importlib.import_module({CATALOG_MODULE!r})
        expected_signatures = {EXPECTED_SIGNATURES!r}
        for public_name in {CATALOG_DELEGATES!r}:
            seam = getattr(facade, public_name)
            assert str(inspect.signature(seam)) == expected_signatures[public_name]
            assert seam.__module__ == {PUBLIC_MODULE!r}
            assert seam.__qualname__ == public_name
            assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam
        for helper_name in ('escape_like', 'catalog_identifiers', 'catalog_ranking'):
            assert callable(getattr(helper, helper_name))
        print('ok')
        """
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


# endregion [01]

# region [02] Pure escaping and bounded identifier decoding


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("plain", "plain"),
        ("100%", "100\\%"),
        ("a_b", "a\\_b"),
        ("a_b%\\", "a\\_b\\%\\\\"),
    ),
)
def test_escape_like_preserves_literals_and_escapes_sql_wildcards(
    value: str,
    expected: str,
) -> None:
    assert knowledge_search._escape_like(value) == expected


def test_catalog_identifiers_preserve_order_trim_and_deduplicate() -> None:
    encoded = json.dumps(
        [
            " IEC 61850 ",
            {"identifier": "NFPA 70E"},
            {"identifier": "  "},
            7,
            "IEC 61850",
            {"other": "ignored"},
        ]
    )
    assert knowledge_search._catalog_identifiers(encoded) == (
        ("standard_identifier", "IEC 61850"),
        ("standard_identifier", "NFPA 70E"),
    )


@pytest.mark.parametrize("value", ("{", "{}", None))
def test_catalog_identifiers_invalid_json_or_non_list_fail_closed(
    value: object,
) -> None:
    assert knowledge_search._catalog_identifiers(value) == ()


def test_catalog_identifiers_bound_overlong_lists_without_raising() -> None:
    values = [f"STD-{index:03d}" for index in range(65)]
    assert knowledge_search._catalog_identifiers(json.dumps(values)) == tuple(
        ("standard_identifier", value) for value in values[:64]
    )


# endregion [02]

# region [03] Owner lifecycle, SQL, filters, limits, and identities


@pytest.mark.parametrize(
    ("snapshot", "reason", "available", "complete"),
    (
        (_snapshot(available=False), "catalog_owner_unavailable", False, True),
        (_snapshot(), "catalog_has_no_publication_heads", False, False),
    ),
)
def test_catalog_early_owner_states_do_not_open_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: KnowledgeSnapshot,
    reason: str,
    available: bool,
    complete: bool,
) -> None:
    monkeypatch.setattr(
        knowledge_search,
        "document_catalog_database",
        lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
    )
    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        snapshot,
    )
    assert candidates == ()
    assert not report.executed
    assert report.available is available
    assert report.complete is complete
    assert report.reason == reason


def test_catalog_sql_parameters_aliases_filters_and_lookahead_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = (("pdf", 7), ("xlsx", 9))
    connection = _CatalogConnection(
        preflight_rows=tuple(_head_row(*head) for head in heads),
    )
    _install_catalog_database(monkeypatch, connection, target_limit=3)
    plan = _plan(
        source_kinds=("office", "audio"),
        formats=(".PDF", "a_b%\\"),
        project="Alpha",
    )

    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(*heads),
    )

    assert candidates == ()
    assert report.complete
    assert [label for label, _sql, _parameters in connection.queries] == [
        "PREFLIGHT",
        "QUERY",
    ]
    preflight_sql = " ".join(connection.queries[0][1].split())
    preflight_parameters = connection.queries[0][2]
    assert "WITH expected(source_kind,generation_id) AS" in preflight_sql
    assert "LEFT JOIN catalog_generations g" in preflight_sql
    assert "g.source_kind AS actual_kind" in preflight_sql
    assert preflight_parameters[:4] == ("pdf", 7, "xlsx", 9)

    query_sql = " ".join(connection.queries[1][1].split())
    query_parameters = connection.queries[1][2]
    assert "catalog_publications" not in query_sql
    assert "JOIN catalog_generations g ON g.generation_id=e.generation_id" in query_sql
    assert "JOIN catalog_generation_documents d" in query_sql
    assert "d.source_kind COLLATE NOCASE IN" in query_sql
    assert query_sql.count("lower(d.path) LIKE ? ESCAPE '\\'") == 2
    assert "json_valid(d.projects_json)" in query_sql
    assert "ORDER BY d.confidence DESC,d.source_kind,d.path COLLATE NOCASE" in query_sql
    assert query_sql.endswith("LIMIT ?")
    expanded = tuple(
        sorted(
            knowledge_search._LEXICAL_OWNER_FORMATS["office"]
            | knowledge_search._LEXICAL_OWNER_FORMATS["audio"]
        )
    )
    assert query_parameters == (
        "pdf",
        7,
        "xlsx",
        9,
        *expanded,
        "%.pdf",
        "%.a\\_b\\%\\\\",
        "Alpha",
        "Alpha",
        4,
    )


def test_catalog_materialization_preserves_exact_identity_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _CatalogConnection(
        preflight_rows=(_head_row("pdf", 1),),
        candidate_rows=(_row(path="C:/docs/catalog-1.pdf"),),
    )
    manager_events = _install_catalog_database(monkeypatch, connection)

    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(("pdf", 1)),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.resource.resource_id == "resource:file:1:2:10"
    assert candidate.resource.current_path == "C:/docs/catalog-1.pdf"
    assert candidate.revision.revision_id == (
        "revision:catalog:203402563930d8e61b528d734c673848"
    )
    assert candidate.revision.producer == "document-catalog-v6"
    assert candidate.revision.processing_signature == "catalog-fixture-v1"
    assert candidate.revision.generation == 1
    assert candidate.revision.state is RevisionState.CURRENT
    assert candidate.evidence.evidence_id == f"evidence:catalog:1:pdf:{_FILE_KEY}"
    assert candidate.evidence.identifiers == (
        ("standard_identifier", "IEC 61850"),
        ("standard_identifier", "NFPA 70E"),
    )
    assert candidate.evidence.snippet == (
        "kind=study; subtype=coordination; project=Alpha; "
        "identifiers=IEC 61850, NFPA 70E; uncertainty=baja"
    )
    assert candidate.signal.source == "catalog_metadata"
    assert candidate.signal.score_kind == "catalog_confidence"
    assert candidate.signal.raw_score == 0.9
    assert candidate.signal.source_rank == 1
    assert candidate.signal.model_signature == "classifier-v6:test"
    assert candidate.signal.generation == 1
    assert candidate.warnings == ()
    assert report.complete
    assert report.returned == 1
    assert report.rows_scanned == 1
    assert report.reason is None
    assert connection.events == ["QUERY", "FETCH:QUERY"] or connection.events == [
        "PREFLIGHT",
        "FETCH:PREFLIGHT",
        "QUERY",
        "FETCH:QUERY",
    ]
    assert manager_events[-2:] == ["ENTER", "CLOSE"]


@pytest.mark.parametrize(
    ("max_candidates", "rows"),
    (
        (1_000, (_row(1), _row(2), _row(3))),
        (2, (_row(1), _row(2))),
    ),
)
def test_catalog_top_k_window_uses_one_row_lookahead_without_becoming_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_candidates: int,
    rows: tuple[dict[str, object], ...],
) -> None:
    monkeypatch.setattr(knowledge_search, "MAX_KNOWLEDGE_CANDIDATES", max_candidates)
    connection = _CatalogConnection(
        preflight_rows=(_head_row("pdf", 1),),
        candidate_rows=rows,
    )
    _install_catalog_database(monkeypatch, connection, target_limit=2)

    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(("pdf", 1)),
    )

    assert len(candidates) == 2
    assert report.returned == 2
    assert report.rows_scanned == len(rows)
    assert report.complete
    assert report.reason is None
    assert report.result_window_full
    query = next(item for item in connection.queries if item[0] == "QUERY")
    assert query[2][-1] == min(max_candidates, 3)


@pytest.mark.parametrize(
    ("preflight_rows", "candidate_rows", "expected_reason", "expected_count"),
    (
        (
            (_head_row("pdf", 1, status="superseded"),),
            (_row(generation_id=1),),
            None,
            1,
        ),
        (
            (_head_row("pdf", 1, status="building"),),
            (),
            "catalog_snapshot_heads_unavailable",
            0,
        ),
        (
            (
                _head_row("pdf", 1),
                _head_row("xlsx", 2, status=None, actual_kind=None),
            ),
            (_row(generation_id=1),),
            "catalog_snapshot_heads_partially_unavailable",
            1,
        ),
    ),
)
def test_catalog_snapshot_head_lifecycle_preserves_superseded_and_reports_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preflight_rows: tuple[dict[str, object], ...],
    candidate_rows: tuple[dict[str, object], ...],
    expected_reason: str | None,
    expected_count: int,
) -> None:
    heads = tuple(
        (str(row["source_kind"]), int(row["generation_id"])) for row in preflight_rows
    )
    connection = _CatalogConnection(
        preflight_rows=preflight_rows,
        candidate_rows=candidate_rows,
    )
    _install_catalog_database(monkeypatch, connection)

    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(*heads),
    )

    assert len(candidates) == expected_count
    assert report.returned == expected_count
    assert report.rows_scanned == len(candidate_rows)
    assert report.complete is (expected_reason is None)
    assert report.reason == expected_reason
    if candidate_rows:
        assert candidates[0].revision.generation == 1


# endregion [03]

# region [04] Invalid metadata, provenance, and exact reason precedence


@pytest.mark.parametrize(
    "identifier_json",
    (
        "{",
        "{}",
        json.dumps([f"STD-{index:03d}" for index in range(65)]),
    ),
)
def test_catalog_invalid_identifier_metadata_keeps_visible_row_but_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identifier_json: str,
) -> None:
    connection = _CatalogConnection(
        preflight_rows=(_head_row("pdf", 1),),
        candidate_rows=(_row(standard_references_json=identifier_json),),
    )
    _install_catalog_database(monkeypatch, connection)

    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(("pdf", 1)),
    )

    assert len(candidates) == 1
    assert not report.complete
    assert report.reason == "catalog_identifier_json_invalid"


def test_catalog_invalid_provenance_skips_only_bad_row_and_reports_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _CatalogConnection(
        preflight_rows=(_head_row("pdf", 1),),
        candidate_rows=(_row(1, size="not-an-int"), _row(2)),
    )
    _install_catalog_database(monkeypatch, connection, target_limit=2)

    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(("pdf", 1)),
    )

    assert len(candidates) == 1
    assert candidates[0].resource.current_path == "C:/docs/catalog-2.pdf"
    assert report.rows_scanned == 2
    assert not report.complete
    assert report.reason == "catalog_identity_or_provenance_invalid"


@pytest.mark.parametrize(
    ("case", "expected_reason", "expected_count"),
    (
        ("identity", "catalog_identity_or_provenance_invalid", 0),
        ("identifier", "catalog_identifier_json_invalid", 1),
        ("partial", "catalog_partial_or_review", 1),
        ("date", "catalog_content_date_filter_unsupported", 1),
        ("limit", None, 1),
    ),
)
def test_catalog_reason_precedence_is_identity_identifier_partial_date_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_reason: str | None,
    expected_count: int,
) -> None:
    first = _row(1)
    if case == "identity":
        first["file_key"] = "0" * 32 + ":" + "0" * 32
        first["standard_references_json"] = "{"
        first["source_status"] = "partial"
    elif case == "identifier":
        first["standard_references_json"] = "{"
        first["source_status"] = "partial"
    elif case == "partial":
        first["source_status"] = "partial"
    date_filter = "2026-01-01" if case != "limit" else None
    connection = _CatalogConnection(
        preflight_rows=(_head_row("pdf", 1),),
        candidate_rows=(first, _row(2)),
    )
    _install_catalog_database(monkeypatch, connection, target_limit=1)

    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(date_from=date_filter),
        _snapshot(("pdf", 1)),
    )

    assert len(candidates) == expected_count
    assert report.rows_scanned == 2
    assert report.reason == expected_reason
    assert report.complete is (expected_reason is None)
    assert report.result_window_full


# endregion [04]

# region [05] Cancellation identity and cleanup masking


@pytest.mark.parametrize(
    "error_type",
    (RuntimeError, sqlite3.OperationalError, sqlite3.DatabaseError),
)
def test_catalog_ranking_reraises_same_captured_callback_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    primary = error_type("cancel inside catalog callback")
    interrupted = sqlite3.OperationalError("interrupted")
    connection = _CatalogConnection(
        preflight_rows=(_head_row("pdf", 1),),
        interrupted=interrupted,
    )
    manager_events = _install_catalog_database(monkeypatch, connection)

    def cancellation() -> None:
        raise primary

    with pytest.raises(error_type) as raised:
        knowledge_search._catalog_ranking(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _plan(),
            _snapshot(("pdf", 1)),
            cancellation_check=cancellation,
        )

    assert raised.value is primary
    assert primary.__cause__ is interrupted
    assert connection.events[-2:] == ["progress:1", "CLEAR"]
    assert manager_events[-1] == "CLOSE"


def test_catalog_close_failure_does_not_mask_captured_primary_or_sqlite_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("cancel inside catalog callback")
    interrupted = sqlite3.OperationalError("interrupted")
    close_error = RuntimeError("catalog close failed")
    connection = _CatalogConnection(
        preflight_rows=(_head_row("pdf", 1),),
        interrupted=interrupted,
    )
    manager_events = _install_catalog_database(
        monkeypatch,
        connection,
        close_error=close_error,
    )

    with pytest.raises(RuntimeError) as raised:
        knowledge_search._catalog_ranking(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _plan(),
            _snapshot(("pdf", 1)),
            cancellation_check=lambda: (_ for _ in ()).throw(primary),
        )

    assert raised.value is primary
    assert primary.__cause__ is interrupted
    assert primary.__notes__ == [
        "catalog connection close cleanup failed: RuntimeError: catalog close failed"
    ]
    assert connection.events[-1] == "CLEAR"
    assert manager_events[-1] == "CLOSE"


def test_catalog_close_only_failure_is_controlled_as_owner_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_error = RuntimeError("catalog close failed")
    connection = _CatalogConnection(
        preflight_rows=(_head_row("pdf", 1),),
        candidate_rows=(_row(),),
    )
    manager_events = _install_catalog_database(
        monkeypatch,
        connection,
        close_error=close_error,
    )

    candidates, report = knowledge_search._catalog_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(("pdf", 1)),
    )

    assert candidates == ()
    assert report.executed
    assert report.available
    assert not report.complete
    assert report.reason == "owner_read_failed:RuntimeError"
    assert manager_events[-1] == "CLOSE"


# endregion [05]
