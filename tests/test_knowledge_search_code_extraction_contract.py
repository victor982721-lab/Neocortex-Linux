"""Tests-first extraction contracts for the Knowledge Search code channel."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import pickle
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from neocortex.foundation.hash_compat import HASH_ALGORITHM_128, HASH_ALGORITHM_64
from neocortex.knowledge import knowledge_search
from neocortex.code.code_contracts import (
    CodeRelationEndpoint,
    CodeSearchHit,
    CodeSearchQuery,
    CodeSearchRelation,
)
from neocortex.knowledge.knowledge_contracts import (
    MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS,
    EvidenceMethod,
    EvidenceRef,
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    RankingSignal,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.knowledge.knowledge_planner import (
    KnowledgeQuery,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_search import (
    KnowledgeCandidate,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text


PUBLIC_MODULE = "neocortex.knowledge.knowledge_search"
CODE_MODULE = "neocortex.knowledge.knowledge_search_code"
CODE_SEARCH_MODULE = "neocortex.code.search.code_search"
LEGACY_PUBLIC_MODULE = "neocortex.knowledge.knowledge_search"
EXPECTED_SIGNATURES = {
    "_code_version_metadata": (
        "(path: 'Path', version_ids: 'Sequence[int]', *, "
        "cancellation_check: 'Callable[[], None] | None' = None) -> "
        "'dict[int, sqlite3.Row]'"
    ),
    "_code_resource_revision": (
        "(row: 'sqlite3.Row', *, path: 'str') -> 'tuple[ResourceRef, RevisionRef, tuple[str, ...]]'"
    ),
    "_bounded_code_relation_value": (
        "(namespace: 'str', value: 'str', warnings: 'set[str]') -> 'str'"
    ),
    "_code_relation_candidate": (
        "(metadata: 'Mapping[int, sqlite3.Row]', *, source_rank: 'int', "
        "hit: 'CodeSearchHit', relation: 'CodeSearchRelation') -> "
        "'tuple[KnowledgeCandidate | None, bool]'"
    ),
    "_code_ranking": (
        "(paths: 'KnowledgeStatePaths', plan: 'KnowledgePlan', "
        "snapshot: 'KnowledgeSnapshot', *, "
        "cancellation_check: 'Callable[[], None] | None' = None) -> "
        "'tuple[tuple[KnowledgeCandidate, ...], RankingExecution]'"
    ),
}
CODE_DELEGATES = tuple(EXPECTED_SIGNATURES)
CODE_IMPLEMENTATIONS = {
    "_code_version_metadata": "_code_version_metadata_impl",
    "_code_resource_revision": "_code_resource_revision_impl",
    "_bounded_code_relation_value": "_code_bounded_relation_value_impl",
    "_code_relation_candidate": "_code_relation_candidate_impl",
    "_code_ranking": "_code_ranking_impl",
}
LATE_BOUND_GLOBALS = {
    "_code_version_metadata": {
        "connect_code_state",
        "SQLiteCancellationBridge",
        "sqlite_cancellation_scope",
        "_cleanup_preserving_primary",
        "SQLITE_BATCH_SIZE",
    },
    "_code_resource_revision": {
        "FileIdentity",
        "_direct_resource_ref",
        "canonical_json",
        "fingerprint_text",
        "RevisionRef",
        "RevisionState",
    },
    "_bounded_code_relation_value": {
        "MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS",
        "fingerprint_text",
    },
    "_code_relation_candidate": {
        "_code_resource_revision",
        "_bounded_code_relation_value",
        "FileIdentityError",
        "canonical_json",
        "fingerprint_text",
        "EvidenceMethod",
        "EvidenceRef",
        "KnowledgeCandidate",
        "RankingSignal",
        "RevisionState",
    },
    "_code_ranking": {
        "_owner_available",
        "_planned_candidate_limit",
        "MAX_KNOWLEDGE_CANDIDATES",
        "MAX_CODE_RELATION_CANDIDATES",
        "_CODE_QUERY_CUES",
        "SQLiteCancellationBridge",
        "search_code",
        "CodeSearchQuery",
        "_code_version_metadata",
        "_code_resource_revision",
        "_code_relation_candidate",
        "sqlite3",
        "_reraise_captured_cancellation",
        "FileIdentityError",
        "EvidenceMethod",
        "EvidenceRef",
        "fingerprint_text",
        "KnowledgeCandidate",
        "RankingSignal",
        "RankingExecution",
    },
}
CODE_MODES = (
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
)


def _snapshot(*, available: bool = True) -> KnowledgeSnapshot:
    return KnowledgeSnapshot.create(
        source_version="0.7.1",
        captured_at_utc="2026-07-30T12:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot(
                "code",
                (OwnerAvailability.AVAILABLE if available else OwnerAvailability.ABSENT),
                2,
                2 if available else None,
            ),
        ),
    )


def _plan(
    text: str = "definition breaker",
    *,
    project: str | None = None,
    source_kinds: tuple[str, ...] = (),
    limit: int = 5,
):
    return plan_knowledge_query(
        KnowledgeQuery(
            text,
            source_kinds=source_kinds,
            formats=("py",),
            project=project,
            limit=limit,
        )
    )


def _metadata_row(
    version_id: int = 1,
    *,
    status: str = "complete",
    volume_id: str | None = None,
    physical_file_id: str | None = None,
) -> dict[str, object]:
    return {
        "version_id": version_id,
        "volume_id": volume_id or f"{version_id:032x}",
        "physical_file_id": physical_file_id or f"{version_id + 100:032x}",
        "size": 1000 + version_id,
        "mtime_ns": 2000 + version_id,
        "birthtime_ns": 3000 + version_id,
        "raw_xxh3_128": f"{version_id:032x}",
        "processing_signature": "code-processing-v1",
        "analyzer_id": "python-ast",
        "analyzer_version": "1",
        "analysis_status": status,
        "first_observed_run_id": 1,
        "last_observed_run_id": 2,
    }


def _relation(
    *,
    source_version: int = 1,
    target_version: int | None = 2,
    confirmed: bool = True,
    name: str = "trip_breaker",
    source_row_id: int = 1,
) -> CodeSearchRelation:
    source = CodeRelationEndpoint(
        source_version,
        "C:/src/source.py",
        11,
        "source.trip",
    )
    target = (
        None
        if target_version is None
        else CodeRelationEndpoint(
            target_version,
            "C:/src/target.py",
            22,
            "target.trip",
        )
    )
    return CodeSearchRelation(
        "reference",
        "call",
        name,
        source,
        target,
        "target.trip",
        target is not None,
        confirmed,
        0.875,
        "python-ast",
        "code_references",
        source_row_id,
    )


def _hit(
    version_id: int = 1,
    *,
    project: str | None = "Alpha",
    relations: tuple[CodeSearchRelation, ...] = (),
) -> CodeSearchHit:
    return CodeSearchHit(
        f"C:/src/file-{version_id}.py",
        project,
        "python",
        "source",
        f"symbol_{version_id}",
        f"symbol_{version_id}()",
        version_id,
        version_id + 1,
        f"breaker fixture {version_id}",
        1.0 / version_id,
        ("literal",),
        (f"literal:{version_id}",),
        version_id,
        100 + version_id,
        200 + version_id,
        "complete",
        relations,
    )


def _candidate(marker: str, *, source_rank: int = 1) -> KnowledgeCandidate:
    resource = ResourceRef(
        f"resource:code-fixture:{marker}",
        "code",
        "code",
        current_path=f"C:/src/{marker}.py",
    )
    revision = RevisionRef(
        resource.resource_id,
        f"revision:code-fixture:{marker}",
        "fixture:1",
        "fixture-signature",
        None,
        RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        f"evidence:code-fixture:{marker}",
        resource.resource_id,
        revision.revision_id,
        EvidenceMethod.STRUCTURAL,
        start_line=1,
        end_line=2,
        section_kind="fixture",
        section_id=marker,
    )
    return KnowledgeCandidate(
        resource,
        revision,
        evidence,
        RankingSignal("code_structural", "fixture", 1.0, source_rank),
        "code extraction fixture",
    )


# region [01] Facade identity and extraction architecture


@pytest.mark.parametrize("name", CODE_DELEGATES)
def test_code_facade_seam_signatures_metadata_and_pickle_are_stable(
    name: str,
) -> None:
    seam = getattr(knowledge_search, name)
    assert str(inspect.signature(seam)) == EXPECTED_SIGNATURES[name]
    assert seam.__module__ == LEGACY_PUBLIC_MODULE
    assert seam.__qualname__ == name
    assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam


@pytest.mark.parametrize("name", CODE_DELEGATES)
def test_code_facade_seams_are_thin_late_bound_delegates(name: str) -> None:
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
    assert statement.value.func.id == CODE_IMPLEMENTATIONS[name]
    referenced_names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    assert LATE_BOUND_GLOBALS[name] <= referenced_names


@pytest.mark.parametrize("name", CODE_DELEGATES)
def test_code_facade_seams_forward_exact_runtime_objects(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if name == "_code_version_metadata":
        cancellation = object()
        call_args = (object(), object())
        call_kwargs = {"cancellation_check": cancellation}
        expected_kwargs = {
            "cancellation_check": cancellation,
            "connect_code_state_fn": knowledge_search.connect_code_state,
            "cancellation_bridge_type": knowledge_search.SQLiteCancellationBridge,
            "sqlite_cancellation_scope_fn": knowledge_search.sqlite_cancellation_scope,
            "cleanup_preserving_primary_fn": knowledge_search._cleanup_preserving_primary,
            "sqlite_batch_size": knowledge_search.SQLITE_BATCH_SIZE,
        }
    elif name == "_code_resource_revision":
        path = object()
        call_args = (object(),)
        call_kwargs = {"path": path}
        expected_kwargs = {
            "path": path,
            "file_identity_type": knowledge_search.FileIdentity,
            "direct_resource_ref_fn": knowledge_search._direct_resource_ref,
            "canonical_json_fn": knowledge_search.canonical_json,
            "fingerprint_text_fn": knowledge_search.fingerprint_text,
            "revision_ref_type": knowledge_search.RevisionRef,
            "revision_state_type": knowledge_search.RevisionState,
        }
    elif name == "_bounded_code_relation_value":
        call_args = (object(), object(), object())
        call_kwargs = {}
        expected_kwargs = {
            "max_identifier_chars": (knowledge_search.MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS),
            "fingerprint_text_fn": knowledge_search.fingerprint_text,
        }
    elif name == "_code_relation_candidate":
        source_rank = object()
        hit = object()
        relation = object()
        call_args = (object(),)
        call_kwargs = {
            "source_rank": source_rank,
            "hit": hit,
            "relation": relation,
        }
        expected_kwargs = {
            **call_kwargs,
            "code_resource_revision_fn": knowledge_search._code_resource_revision,
            "bounded_relation_value_fn": knowledge_search._bounded_code_relation_value,
            "file_identity_error_type": knowledge_search.FileIdentityError,
            "canonical_json_fn": knowledge_search.canonical_json,
            "fingerprint_text_fn": knowledge_search.fingerprint_text,
            "evidence_method_type": knowledge_search.EvidenceMethod,
            "evidence_ref_type": knowledge_search.EvidenceRef,
            "knowledge_candidate_type": knowledge_search.KnowledgeCandidate,
            "ranking_signal_type": knowledge_search.RankingSignal,
            "revision_state_type": knowledge_search.RevisionState,
        }
    elif name == "_code_ranking":
        cancellation = object()
        call_args = (object(), object(), object())
        call_kwargs = {"cancellation_check": cancellation}
        expected_kwargs = {
            "cancellation_check": cancellation,
            "owner_available_fn": knowledge_search._owner_available,
            "planned_candidate_limit_fn": knowledge_search._planned_candidate_limit,
            "max_candidates": knowledge_search.MAX_KNOWLEDGE_CANDIDATES,
            "max_relation_candidates": (knowledge_search.MAX_CODE_RELATION_CANDIDATES),
            "code_query_cues": knowledge_search._CODE_QUERY_CUES,
            "cancellation_bridge_type": knowledge_search.SQLiteCancellationBridge,
            "search_code_fn": knowledge_search.search_code,
            "code_search_query_type": knowledge_search.CodeSearchQuery,
            "code_version_metadata_fn": knowledge_search._code_version_metadata,
            "code_resource_revision_fn": knowledge_search._code_resource_revision,
            "code_relation_candidate_fn": knowledge_search._code_relation_candidate,
            "sqlite_error_type": sqlite3.Error,
            "reraise_captured_cancellation_fn": (knowledge_search._reraise_captured_cancellation),
            "file_identity_error_type": knowledge_search.FileIdentityError,
            "evidence_method_type": knowledge_search.EvidenceMethod,
            "evidence_ref_type": knowledge_search.EvidenceRef,
            "fingerprint_text_fn": knowledge_search.fingerprint_text,
            "knowledge_candidate_type": knowledge_search.KnowledgeCandidate,
            "ranking_signal_type": knowledge_search.RankingSignal,
            "ranking_execution_type": knowledge_search.RankingExecution,
        }
    else:
        raise AssertionError(name)

    marker = object()
    captured: dict[str, object] = {}

    def implementation(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return marker

    monkeypatch.setattr(knowledge_search, CODE_IMPLEMENTATIONS[name], implementation)
    result = getattr(knowledge_search, name)(*call_args, **call_kwargs)

    assert result is marker
    actual_args = captured["args"]
    actual_kwargs = captured["kwargs"]
    assert isinstance(actual_args, tuple)
    assert isinstance(actual_kwargs, dict)
    assert len(actual_args) == len(call_args)
    assert all(actual is expected for actual, expected in zip(actual_args, call_args, strict=True))
    assert actual_kwargs.keys() == expected_kwargs.keys()
    assert all(actual_kwargs[key] is value for key, value in expected_kwargs.items())


def test_code_extraction_module_exists_without_a_facade_cycle() -> None:
    spec = importlib.util.find_spec(CODE_MODULE)
    assert spec is not None
    assert spec.origin is not None
    module_path = Path(spec.origin)
    source = module_path.read_text(encoding="utf-8")
    # The safe reader path adds a small bounded session-selection seam while
    # preserving the helper/facade split.
    assert len(source.splitlines()) <= 930
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
    assert not any(module.endswith(".knowledge_search") for module in imported_modules)
    helper_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {
        "code_version_metadata",
        "code_resource_revision",
        "bounded_code_relation_value",
        "code_relation_candidate",
        "code_ranking",
    } <= helper_names


def test_code_extraction_modules_form_expected_relative_import_dag() -> None:
    modules = (PUBLIC_MODULE, CODE_MODULE, CODE_SEARCH_MODULE)
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
                    (module_name, alias.name) for alias in node.names if alias.name in modules
                )

    assert edges == {
        (PUBLIC_MODULE, CODE_MODULE),
        (PUBLIC_MODULE, CODE_SEARCH_MODULE),
    }


@pytest.mark.parametrize(
    "module_order",
    (
        (PUBLIC_MODULE, CODE_MODULE, CODE_SEARCH_MODULE),
        (PUBLIC_MODULE, CODE_SEARCH_MODULE, CODE_MODULE),
        (CODE_MODULE, PUBLIC_MODULE, CODE_SEARCH_MODULE),
        (CODE_MODULE, CODE_SEARCH_MODULE, PUBLIC_MODULE),
        (CODE_SEARCH_MODULE, PUBLIC_MODULE, CODE_MODULE),
        (CODE_SEARCH_MODULE, CODE_MODULE, PUBLIC_MODULE),
    ),
    ids=(
        "facade-helper-search",
        "facade-search-helper",
        "helper-facade-search",
        "helper-search-facade",
        "search-facade-helper",
        "search-helper-facade",
    ),
)
def test_code_modules_support_all_six_cold_import_orders(
    module_order: tuple[str, str, str],
) -> None:
    repository = Path(__file__).resolve().parents[1]
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
        helper = importlib.import_module({CODE_MODULE!r})
        expected_signatures = {EXPECTED_SIGNATURES!r}
        for public_name in {CODE_DELEGATES!r}:
            seam = getattr(facade, public_name)
            assert str(inspect.signature(seam)) == expected_signatures[public_name]
            assert seam.__module__ == {LEGACY_PUBLIC_MODULE!r}
            assert seam.__qualname__ == public_name
            assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam
        for helper_name in (
            'code_version_metadata',
            'code_resource_revision',
            'bounded_code_relation_value',
            'code_relation_candidate',
            'code_ranking',
        ):
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


# region [02] Metadata reads, identities, and bounded relation evidence


def test_code_metadata_batches_in_order_deduplicates_locally_and_reads_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    sql_statements: list[str] = []
    batches: list[tuple[int, ...]] = []

    class Cursor:
        def __init__(self, rows: tuple[dict[str, int], ...]) -> None:
            self.rows = rows

        def fetchall(self) -> tuple[dict[str, int], ...]:
            events.append("fetchall")
            return self.rows

    class Connection:
        def set_progress_handler(self, callback: object, instructions: int) -> None:
            events.append(("progress", callback is not None, instructions))

        def execute(self, statement: str, parameters: tuple[int, ...]) -> Cursor:
            events.append("execute")
            sql_statements.append(statement)
            batches.append(parameters)
            return Cursor(tuple({"version_id": value} for value in parameters))

        def close(self) -> None:
            events.append("close")

    connection = Connection()
    path = tmp_path / "code.sqlite3"
    opens: list[tuple[Path, bool]] = []

    def connect(path_value: Path, *, readonly: bool) -> Connection:
        opens.append((path_value, readonly))
        return connection

    checkpoints = 0

    def cancellation() -> None:
        nonlocal checkpoints
        checkpoints += 1
        events.append("checkpoint")

    monkeypatch.setattr(knowledge_search, "connect_code_state", connect)
    version_ids = (*range(1, 501), 500, 501, 501, 502)

    result = knowledge_search._code_version_metadata(
        path,
        version_ids,
        cancellation_check=cancellation,
    )

    assert opens == [(path, True)]
    assert batches == [tuple(range(1, 501)), (500, 501, 502)]
    assert tuple(result) == tuple(range(1, 503))
    assert checkpoints == 2
    assert events[0] == ("progress", True, 1000)
    assert events[-2:] == [("progress", False, 0), "close"]
    assert all(statement.lstrip().upper().startswith("SELECT ") for statement in sql_statements)
    combined_sql = " ".join(sql_statements).upper()
    assert "F.CURRENT_VERSION_ID=V.VERSION_ID" in combined_sql
    assert "F.STATUS='CURRENT'" in combined_sql
    assert "V.INVALIDATED_NS IS NULL" in combined_sql
    assert not any(
        token in combined_sql
        for token in (
            " INSERT ",
            " UPDATE ",
            " DELETE ",
            " CREATE ",
            " DROP ",
            " ALTER ",
        )
    )


def test_code_metadata_progress_cleanup_preserves_primary_and_close_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("metadata query failed")
    clear_error = RuntimeError("metadata clear failed")
    close_error = RuntimeError("metadata close failed")
    events: list[object] = []

    class Connection:
        def set_progress_handler(self, callback: object, instructions: int) -> None:
            events.append(("progress", callback is not None, instructions))
            if callback is None:
                raise clear_error

        def execute(self, _statement: str, _parameters: object) -> None:
            events.append("execute")
            raise primary

        def close(self) -> None:
            events.append("close")
            raise close_error

    monkeypatch.setattr(
        knowledge_search,
        "connect_code_state",
        lambda *_args, **_kwargs: Connection(),
    )

    with pytest.raises(RuntimeError) as raised:
        knowledge_search._code_version_metadata(
            tmp_path / "code.sqlite3",
            (1,),
            cancellation_check=lambda: events.append("checkpoint"),
        )

    assert raised.value is primary
    assert events == [
        ("progress", True, 1000),
        "checkpoint",
        "execute",
        ("progress", False, 0),
        "close",
    ]
    assert primary.__notes__ == [
        "SQLite progress handler cleanup failed: RuntimeError: metadata clear failed",
        "code metadata connection close cleanup failed: RuntimeError: metadata close failed",
    ]


@pytest.mark.parametrize(
    ("status", "expected_state"),
    (
        ("complete", RevisionState.CURRENT),
        ("text_only", RevisionState.CURRENT),
        ("failed", RevisionState.PARTIAL),
    ),
)
def test_code_resource_revision_has_stable_identity_and_status_mapping(
    status: str,
    expected_state: RevisionState,
) -> None:
    row = _metadata_row(7, status=status)

    resource, revision, warnings = knowledge_search._code_resource_revision(
        row,
        path="C:/src/original.py",
    )
    moved_resource, moved_revision, moved_warnings = knowledge_search._code_resource_revision(
        row,
        path="D:/moved/original.py",
    )

    source_identity = f"{row['volume_id']}:{row['physical_file_id']}"
    source_revision = {
        "version_id": 7,
        "size": row["size"],
        "mtime_ns": row["mtime_ns"],
        "birthtime_ns": row["birthtime_ns"],
        "raw_content_xxh3_128": row["raw_xxh3_128"],
    }
    expected_fingerprint = fingerprint_text(
        canonical_json(
            {
                "source_kind": "code",
                "source_identity": source_identity,
                "source_revision": source_revision,
            }
        )
    )
    assert warnings == moved_warnings == ()
    assert resource.resource_id == moved_resource.resource_id
    assert resource.current_path == "C:/src/original.py"
    assert moved_resource.current_path == "D:/moved/original.py"
    assert revision == moved_revision
    assert revision.revision_id == f"revision:code:{expected_fingerprint.xxh3_128}"
    assert revision.producer == "python-ast:1"
    assert revision.processing_signature == "code-processing-v1"
    assert revision.state is expected_state


def test_bounded_code_relation_value_uses_character_limit_and_exact_fingerprint() -> None:
    warnings: set[str] = set()
    at_limit = "á" * MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS
    over_limit = "á" * (MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS + 1)

    unchanged = knowledge_search._bounded_code_relation_value(
        "code_relation_name",
        at_limit,
        warnings,
    )
    bounded = knowledge_search._bounded_code_relation_value(
        "code_relation_name",
        over_limit,
        warnings,
    )

    fingerprint = fingerprint_text(over_limit)
    assert unchanged == at_limit
    expected = (
        f"xxh3-v1:{fingerprint.xxh3_128}:{fingerprint.byte_count}:{fingerprint.xxh3_64_guard}"
        if HASH_ALGORITHM_128 == "xxh3-128"
        else (
            f"{HASH_ALGORITHM_128}-v1:{fingerprint.xxh3_128}:{fingerprint.byte_count}:"
            f"{HASH_ALGORITHM_64}-guard={fingerprint.xxh3_64_guard}"
        )
    )
    assert bounded == expected
    assert fingerprint.byte_count == 2 * len(over_limit)
    assert warnings == {"code_relation_name_fingerprinted_due_to_contract_limit"}


@pytest.mark.parametrize(
    ("target_present", "confirmed", "source_status", "method", "incomplete"),
    (
        (True, True, "complete", EvidenceMethod.STRUCTURAL, False),
        (True, False, "complete", EvidenceMethod.INFERRED, True),
        (False, True, "complete", EvidenceMethod.AMBIGUOUS, True),
        (True, True, "failed", EvidenceMethod.STRUCTURAL, True),
    ),
)
def test_code_relation_candidate_preserves_resolution_and_evidence_semantics(
    target_present: bool,
    confirmed: bool,
    source_status: str,
    method: EvidenceMethod,
    incomplete: bool,
) -> None:
    relation = _relation(confirmed=confirmed)
    metadata = {1: _metadata_row(1, status=source_status)}
    if target_present:
        metadata[2] = _metadata_row(2)

    candidate, observed_incomplete = knowledge_search._code_relation_candidate(
        metadata,
        source_rank=3,
        hit=_hit(relations=(relation,)),
        relation=relation,
    )

    assert candidate is not None
    assert observed_incomplete is incomplete
    assert candidate.evidence.method is method
    assert candidate.signal.source_rank == 3
    assert candidate.evidence.evidence_id.startswith("evidence:code-relation:v1:")
    warning_set = set(candidate.warnings)
    if not target_present:
        assert {
            "code_relation_target_changed_after_owner_read",
            "code_relation_unresolved",
        } <= warning_set
        assert "code_relation_target_resource" not in dict(candidate.evidence.identifiers)
    if not confirmed:
        assert "code_relation_unconfirmed" in warning_set
    identifier_names = tuple(name for name, _ in candidate.evidence.identifiers)
    expected_prefix = (
        "code_relation_id",
        "code_relation_family",
        "code_relation_kind",
        "code_relation_name",
        "code_relation_source_resource",
        "code_relation_source_version_id",
        "code_relation_source_symbol",
    )
    assert identifier_names[: len(expected_prefix)] == expected_prefix


def test_code_relation_candidate_fails_closed_for_missing_or_invalid_source() -> None:
    relation = _relation(target_version=None)
    hit = _hit(relations=(relation,))

    missing = knowledge_search._code_relation_candidate(
        {},
        source_rank=1,
        hit=hit,
        relation=relation,
    )
    malformed = knowledge_search._code_relation_candidate(
        {1: _metadata_row(1, volume_id="not-hex")},
        source_rank=1,
        hit=hit,
        relation=relation,
    )

    assert missing == (None, True)
    assert malformed == (None, True)


def test_code_relation_candidate_bounds_long_identifiers_without_losing_source() -> None:
    long_name = "Ω" * (MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS + 1)
    relation = _relation(target_version=None, name=long_name)

    candidate, incomplete = knowledge_search._code_relation_candidate(
        {1: _metadata_row(1)},
        source_rank=1,
        hit=_hit(relations=(relation,)),
        relation=relation,
    )

    assert candidate is not None
    assert incomplete
    identifiers = dict(candidate.evidence.identifiers)
    fingerprint = fingerprint_text(long_name)
    expected = (
        f"xxh3-v1:{fingerprint.xxh3_128}:{fingerprint.byte_count}:{fingerprint.xxh3_64_guard}"
        if HASH_ALGORITHM_128 == "xxh3-128"
        else (
            f"{HASH_ALGORITHM_128}-v1:{fingerprint.xxh3_128}:{fingerprint.byte_count}:"
            f"{HASH_ALGORITHM_64}-guard={fingerprint.xxh3_64_guard}"
        )
    )
    assert identifiers["code_relation_name"] == expected
    assert max(map(len, identifiers.values())) <= MAX_EVIDENCE_IDENTIFIER_COMPONENT_CHARS
    assert "code_relation_name_fingerprinted_due_to_contract_limit" in (candidate.warnings)


# endregion [02]


# region [03] Ranking filters, bounds, ordering, and failure semantics


@pytest.mark.parametrize(
    ("available", "source_kinds", "expected_reason", "expected_executed"),
    (
        (False, (), "code_owner_unavailable", False),
        (True, ("pdf",), "source_filter_excludes_code", True),
    ),
)
def test_code_ranking_early_filters_do_not_open_the_owner(
    available: bool,
    source_kinds: tuple[str, ...],
    expected_reason: str,
    expected_executed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_search,
        "search_code",
        lambda *_args, **_kwargs: pytest.fail("filtered code owner was opened"),
    )

    candidates, report = knowledge_search._code_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(source_kinds=source_kinds),
        _snapshot(available=available),
    )

    assert candidates == ()
    assert report.reason == expected_reason
    assert report.executed is expected_executed
    assert report.complete


@pytest.mark.parametrize(
    ("query_text", "expected_owner_text"),
    (
        ("Definition: breaker CALL symbol!", "breaker"),
        ("definition CALL symbol", "definition CALL symbol"),
    ),
)
def test_code_ranking_forwards_exact_path_modes_lookahead_and_cue_policy(
    query_text: str,
    expected_owner_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    observed: list[tuple[Path, CodeSearchQuery, object]] = []
    metadata_calls: list[tuple[Path, tuple[int, ...], object]] = []

    def search_fixture(
        path: Path,
        query: CodeSearchQuery,
        *,
        cancellation_check: object,
    ) -> tuple[()]:
        observed.append((path, query, cancellation_check))
        return ()

    def metadata_fixture(
        path: Path,
        version_ids: tuple[int, ...],
        *,
        cancellation_check: object,
    ) -> dict[int, object]:
        metadata_calls.append((path, version_ids, cancellation_check))
        return {}

    monkeypatch.setattr(knowledge_search, "search_code", search_fixture)
    monkeypatch.setattr(knowledge_search, "_code_version_metadata", metadata_fixture)
    monkeypatch.setattr(
        knowledge_search,
        "_planned_candidate_limit",
        lambda *_args, **_kwargs: 4,
    )

    candidates, report = knowledge_search._code_ranking(
        paths,
        _plan(query_text, project="Alpha"),
        _snapshot(),
    )

    assert candidates == ()
    assert report.complete
    assert len(observed) == 1
    owner_path, owner_query, owner_cancellation = observed[0]
    assert owner_path == paths.code
    assert owner_query.text == expected_owner_text
    assert owner_query.modes == CODE_MODES
    assert owner_query.project is None
    assert owner_query.limit == 5
    assert owner_cancellation is None
    assert metadata_calls == [(paths.code, (), None)]


def test_code_ranking_filters_project_casefold_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    hits = (
        _hit(1, project="Beta"),
        _hit(2, project="ALPHA"),
        _hit(3, project=None),
    )
    metadata_ids: list[tuple[int, ...]] = []
    base = _candidate("project")

    monkeypatch.setattr(
        knowledge_search,
        "search_code",
        lambda *_args, **_kwargs: hits,
    )

    def metadata_fixture(
        _path: Path,
        version_ids: tuple[int, ...],
        **_kwargs: object,
    ) -> dict[int, dict[str, str]]:
        metadata_ids.append(version_ids)
        return {
            2: {
                "analyzer_id": "fixture",
                "analyzer_version": "1",
                "processing_signature": "fixture-v1",
            }
        }

    monkeypatch.setattr(knowledge_search, "_code_version_metadata", metadata_fixture)
    monkeypatch.setattr(
        knowledge_search,
        "_code_resource_revision",
        lambda *_args, **_kwargs: (base.resource, base.revision, ()),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_planned_candidate_limit",
        lambda *_args, **_kwargs: 4,
    )

    candidates, report = knowledge_search._code_ranking(
        paths,
        _plan(project="Alpha"),
        _snapshot(),
    )

    assert metadata_ids == [(2,)]
    assert len(candidates) == 1
    assert candidates[0].signal.source_rank == 2
    assert candidates[0].resource is base.resource
    assert report.rows_scanned == 3
    assert report.complete


def test_code_ranking_malformed_metadata_row_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_search,
        "search_code",
        lambda *_args, **_kwargs: (_hit(1),),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_code_version_metadata",
        lambda *_args, **_kwargs: {1: _metadata_row(1, volume_id="not-hex")},
    )

    candidates, report = knowledge_search._code_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(),
    )

    assert candidates == ()
    assert not report.complete
    assert report.rows_scanned == 1
    assert report.reason == "code_identity_invalid_or_stale"


@pytest.mark.parametrize(
    ("error", "reason"),
    (
        (RuntimeError("owner runtime failure"), "owner_read_failed:RuntimeError"),
        (
            sqlite3.OperationalError("ordinary sqlite failure"),
            "owner_read_failed:OperationalError",
        ),
    ),
)
def test_code_ranking_ordinary_owner_errors_still_report_incomplete(
    error: BaseException,
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> tuple[CodeSearchHit, ...]:
        raise error

    monkeypatch.setattr(knowledge_search, "search_code", fail)

    candidates, report = knowledge_search._code_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(),
    )

    assert candidates == ()
    assert not report.complete
    assert report.reason == reason


@pytest.mark.parametrize(
    (
        "invalid_hit",
        "relation_limit",
        "relation_incomplete",
        "target_limit",
        "relation_count",
        "expected_reason",
    ),
    (
        (True, 1, True, 4, 2, "code_identity_invalid_or_stale"),
        (False, 1, True, 4, 2, "code_relation_limit_reached"),
        (False, 4000, True, 1, 1, "code_relation_unresolved_or_unconfirmed"),
        (False, 4000, False, 1, 1, None),
    ),
)
def test_code_ranking_reason_precedence_and_direct_before_relation_order(
    invalid_hit: bool,
    relation_limit: int,
    relation_incomplete: bool,
    target_limit: int,
    relation_count: int,
    expected_reason: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relations = tuple(
        _relation(target_version=None, source_row_id=index)
        for index in range(1, relation_count + 1)
    )
    hit = _hit(1, relations=relations)
    direct = _candidate("direct")

    monkeypatch.setattr(
        knowledge_search,
        "search_code",
        lambda *_args, **_kwargs: (hit,),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_planned_candidate_limit",
        lambda *_args, **_kwargs: target_limit,
    )
    monkeypatch.setattr(
        knowledge_search,
        "MAX_CODE_RELATION_CANDIDATES",
        relation_limit,
    )
    monkeypatch.setattr(
        knowledge_search,
        "_code_version_metadata",
        lambda *_args, **_kwargs: (
            {}
            if invalid_hit
            else {
                1: {
                    "analyzer_id": "fixture",
                    "analyzer_version": "1",
                    "processing_signature": "fixture-v1",
                }
            }
        ),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_code_resource_revision",
        lambda *_args, **_kwargs: (direct.resource, direct.revision, ()),
    )

    def relation_fixture(
        _metadata: object,
        *,
        source_rank: int,
        hit: CodeSearchHit,
        relation: CodeSearchRelation,
    ) -> tuple[KnowledgeCandidate, bool]:
        del hit
        return (
            _candidate(
                f"relation-{relation.source_row_id}",
                source_rank=source_rank,
            ),
            relation_incomplete,
        )

    monkeypatch.setattr(
        knowledge_search,
        "_code_relation_candidate",
        relation_fixture,
    )

    candidates, report = knowledge_search._code_ranking(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _plan(),
        _snapshot(),
    )

    assert report.reason == expected_reason
    assert report.complete is (expected_reason is None)
    if expected_reason is None:
        assert report.result_window_full
    assert report.rows_scanned == 2
    if not invalid_hit:
        assert candidates[0].evidence.section_kind == "code_search_hit"


# endregion [03]


# region [04] Exact same-object SQLite cancellation regressions


def test_code_ranking_reraises_exact_sqlite_callback_error_from_search_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = sqlite3.OperationalError("cancel inside search callback")
    interrupted = sqlite3.OperationalError("interrupted")
    events: list[str] = []
    calls = 0

    def cancellation() -> None:
        nonlocal calls
        calls += 1
        events.append(f"cancel:{calls}")
        if calls == 2:
            raise primary

    class Connection:
        def __init__(self) -> None:
            self.in_transaction = False
            self.handler: object = None

        def execute(self, statement: str, _parameters: object = ()) -> object:
            if statement.strip().upper() == "BEGIN":
                events.append("BEGIN")
                self.in_transaction = True
                return self
            events.append("QUERY")
            assert callable(self.handler)
            progress_result = self.handler()
            events.append(f"progress:{progress_result}")
            raise interrupted

        def set_progress_handler(self, callback: object, instructions: int) -> None:
            self.handler = callback
            events.append("CLEAR" if callback is None else f"SET:{instructions}")

        def rollback(self) -> None:
            events.append("ROLLBACK")
            self.in_transaction = False

        def close(self) -> None:
            events.append("CLOSE")

    code_search_module = importlib.import_module(CODE_SEARCH_MODULE)

    def connect(*_args: object, **_kwargs: object) -> Connection:
        events.append("CONNECT")
        return Connection()

    monkeypatch.setattr(code_search_module, "connect_code_state", connect)
    monkeypatch.setattr(
        knowledge_search,
        "_code_version_metadata",
        lambda *_args, **_kwargs: pytest.fail("metadata must not be reached"),
    )

    with pytest.raises(sqlite3.OperationalError) as raised:
        knowledge_search._code_ranking(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _plan(),
            _snapshot(),
            cancellation_check=cancellation,
        )

    assert raised.value is primary
    assert raised.value.__cause__ is interrupted
    assert events == [
        "cancel:1",
        "CONNECT",
        "BEGIN",
        "SET:1000",
        "QUERY",
        "cancel:2",
        "progress:1",
        "CLEAR",
        "ROLLBACK",
        "CLOSE",
    ]


def test_code_ranking_reraises_exact_sqlite_callback_error_from_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = sqlite3.DatabaseError("cancel inside metadata callback")
    interrupted = sqlite3.OperationalError("interrupted")
    clear_error = RuntimeError("metadata clear failed")
    close_error = RuntimeError("metadata close failed")
    events: list[str] = []
    calls = 0

    def cancellation() -> None:
        nonlocal calls
        calls += 1
        events.append(f"cancel:{calls}")
        if calls == 2:
            raise primary

    class Connection:
        def __init__(self) -> None:
            self.handler: object = None

        def set_progress_handler(self, callback: object, instructions: int) -> None:
            self.handler = callback
            events.append("CLEAR" if callback is None else f"SET:{instructions}")
            if callback is None:
                raise clear_error

        def execute(self, _statement: str, _parameters: object) -> object:
            events.append("QUERY")
            assert callable(self.handler)
            progress_result = self.handler()
            events.append(f"progress:{progress_result}")
            raise interrupted

        def close(self) -> None:
            events.append("CLOSE")
            raise close_error

    def connect(*_args: object, **_kwargs: object) -> Connection:
        events.append("CONNECT")
        return Connection()

    monkeypatch.setattr(
        knowledge_search,
        "search_code",
        lambda *_args, **_kwargs: (_hit(1),),
    )
    monkeypatch.setattr(knowledge_search, "connect_code_state", connect)

    with pytest.raises(sqlite3.DatabaseError) as raised:
        knowledge_search._code_ranking(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _plan(),
            _snapshot(),
            cancellation_check=cancellation,
        )

    assert raised.value is primary
    assert raised.value.__cause__ is interrupted
    assert primary.__notes__ == [
        "SQLite progress handler cleanup failed: RuntimeError: metadata clear failed",
        "code metadata connection close cleanup failed: RuntimeError: metadata close failed",
    ]
    assert events == [
        "CONNECT",
        "SET:1000",
        "cancel:1",
        "QUERY",
        "cancel:2",
        "progress:1",
        "CLEAR",
        "CLOSE",
    ]


# endregion [04]

# region [05] Direct code owner cleanup preservation


class _DirectSearchCleanupConnection:
    def __init__(
        self,
        events: list[str],
        *,
        interrupted: sqlite3.Error | None = None,
        clear_error: BaseException | None = None,
        transaction_state_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.interrupted = interrupted
        self.clear_error = clear_error
        self.transaction_state_error = transaction_state_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self._in_transaction = False
        self.handler: object = None

    @property
    def in_transaction(self) -> bool:
        self.events.append("STATE")
        if self.transaction_state_error is not None:
            raise self.transaction_state_error
        return self._in_transaction

    def execute(self, statement: str, _parameters: object = ()) -> object:
        if statement.strip().upper() == "BEGIN":
            self.events.append("BEGIN")
            self._in_transaction = True
            return self
        self.events.append("QUERY")
        if self.handler is not None:
            assert callable(self.handler)
            progress_result = self.handler()
            self.events.append(f"progress:{progress_result}")
            if progress_result:
                assert self.interrupted is not None
                raise self.interrupted
        return ()

    def set_progress_handler(self, callback: object, instructions: int) -> None:
        self.handler = callback
        self.events.append("CLEAR" if callback is None else f"SET:{instructions}")
        if callback is None and self.clear_error is not None:
            raise self.clear_error

    def rollback(self) -> None:
        self.events.append("ROLLBACK")
        if self.rollback_error is not None:
            raise self.rollback_error
        self._in_transaction = False

    def close(self) -> None:
        self.events.append("CLOSE")
        if self.close_error is not None:
            raise self.close_error


def test_direct_code_search_callback_primary_survives_clear_rollback_and_close_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = sqlite3.OperationalError("cancel inside direct code callback")
    interrupted = sqlite3.OperationalError("interrupted")
    clear_error = RuntimeError("code clear failed")
    rollback_error = RuntimeError("code rollback failed")
    close_error = RuntimeError("code close failed")
    events: list[str] = []
    calls = 0

    def cancellation() -> None:
        nonlocal calls
        calls += 1
        events.append(f"cancel:{calls}")
        if calls == 2:
            raise primary

    connection = _DirectSearchCleanupConnection(
        events,
        interrupted=interrupted,
        clear_error=clear_error,
        rollback_error=rollback_error,
        close_error=close_error,
    )
    code_search_module = importlib.import_module(CODE_SEARCH_MODULE)

    def connect(*_args: object, **_kwargs: object) -> _DirectSearchCleanupConnection:
        events.append("CONNECT")
        return connection

    monkeypatch.setattr(code_search_module, "connect_code_state", connect)

    with pytest.raises(sqlite3.OperationalError) as raised:
        code_search_module.search_code(
            tmp_path / "code.sqlite3",
            CodeSearchQuery(text="breaker", modes=("literal",), limit=1),
            cancellation_check=cancellation,
        )

    assert raised.value is primary
    assert primary.__cause__ is interrupted
    assert primary.__notes__ == [
        "SQLite progress handler cleanup failed: RuntimeError: code clear failed",
        "code search rollback cleanup failed: RuntimeError: code rollback failed",
        "code search connection close cleanup failed: RuntimeError: code close failed",
    ]
    assert events == [
        "cancel:1",
        "CONNECT",
        "BEGIN",
        "SET:1000",
        "QUERY",
        "cancel:2",
        "progress:1",
        "CLEAR",
        "STATE",
        "ROLLBACK",
        "CLOSE",
    ]


def test_direct_code_search_rollback_failure_still_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollback_error = RuntimeError("code rollback failed")
    events: list[str] = []
    connection = _DirectSearchCleanupConnection(
        events,
        rollback_error=rollback_error,
    )
    code_search_module = importlib.import_module(CODE_SEARCH_MODULE)
    monkeypatch.setattr(
        code_search_module,
        "connect_code_state",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(RuntimeError) as raised:
        code_search_module.search_code(
            tmp_path / "code.sqlite3",
            CodeSearchQuery(text="breaker", modes=("literal",), limit=1),
        )

    assert raised.value is rollback_error
    assert events == ["BEGIN", "QUERY", "STATE", "ROLLBACK", "CLOSE"]


def test_direct_code_search_close_failure_without_primary_is_propagated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_error = RuntimeError("code close failed")
    events: list[str] = []
    connection = _DirectSearchCleanupConnection(events, close_error=close_error)
    code_search_module = importlib.import_module(CODE_SEARCH_MODULE)
    monkeypatch.setattr(
        code_search_module,
        "connect_code_state",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(RuntimeError) as raised:
        code_search_module.search_code(
            tmp_path / "code.sqlite3",
            CodeSearchQuery(text="breaker", modes=("literal",), limit=1),
        )

    assert raised.value is close_error
    assert events == ["BEGIN", "QUERY", "STATE", "ROLLBACK", "CLOSE"]


def test_code_search_transaction_state_failure_preserves_primary_and_closes() -> None:
    primary = RuntimeError("code query failed")
    state_error = RuntimeError("transaction state failed")
    events: list[str] = []
    connection = _DirectSearchCleanupConnection(
        events,
        transaction_state_error=state_error,
    )
    code_search_module = importlib.import_module(CODE_SEARCH_MODULE)

    code_search_module._cleanup_search_connection(connection, primary)

    assert primary.__notes__ == [
        "code search transaction-state cleanup failed: RuntimeError: transaction state failed"
    ]
    assert events == ["STATE", "CLOSE"]


def test_code_search_transaction_state_failure_is_primary_and_close_is_noted() -> None:
    state_error = RuntimeError("transaction state failed")
    close_error = RuntimeError("code close failed")
    events: list[str] = []
    connection = _DirectSearchCleanupConnection(
        events,
        transaction_state_error=state_error,
        close_error=close_error,
    )
    code_search_module = importlib.import_module(CODE_SEARCH_MODULE)

    with pytest.raises(RuntimeError) as raised:
        code_search_module._cleanup_search_connection(connection, None)

    assert raised.value is state_error
    assert state_error.__notes__ == [
        "code search connection close cleanup failed: RuntimeError: code close failed"
    ]
    assert events == ["STATE", "CLOSE"]


# endregion [05]
