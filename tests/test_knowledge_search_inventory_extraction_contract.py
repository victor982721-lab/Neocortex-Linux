"""Tests-first extraction contracts for the Knowledge Search inventory channel."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_search_inventory_extraction_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import ast
import importlib.util
import inspect
import pickle
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from neocortex.knowledge import knowledge_search
from neocortex.knowledge import knowledge_search_inventory
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    PhysicalIdentityRef,
    PublicationHead,
    RankingSignal,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.knowledge.knowledge_search import (
    KnowledgeCandidate,
)
from neocortex.foundation.file_identity import FileIdentity
from neocortex.deduplication import DedupIndex, DedupPlanner, snapshot_path
from neocortex.knowledge.knowledge_planner import (
    KnowledgeQuery,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
# endregion [01]

# region [02] Implementación


def _as_sqlite_row(value: Mapping[str, object]) -> sqlite3.Row:
    """Treat an injected mapping fixture as the row protocol under test."""

    return cast(sqlite3.Row, value)


PUBLIC_MODULE = "neocortex.knowledge.knowledge_search"
CONTRACT_MODULE = "neocortex.knowledge.knowledge_search_contracts"
INVENTORY_MODULE = "neocortex.knowledge.knowledge_search_inventory"
EXPECTED_SIGNATURES = {
    "_open_direct_readonly_sqlite": "(path: 'Path') -> 'sqlite3.Connection'",
    "_decimal_identity_value": "(value: 'object') -> 'int'",
    "_physical_identity_tuple": ("(resource: 'ResourceRef') -> 'tuple[int, int, int] | None'"),
    "_inventory_plan_heads": (
        "(snapshot: 'KnowledgeSnapshot') -> "
        "'tuple[tuple[InventoryHead, ...], tuple[InventoryPlanIssue, ...]]'"
    ),
    "_inventory_identity_blob": "(value: 'int') -> 'bytes'",
    "_validated_inventory_blob": "(value: 'object') -> 'int'",
    "_valid_full_fingerprint": "(value: 'object') -> 'bool'",
    "_inventory_relation_row": (
        "(row: 'sqlite3.Row') -> 'tuple[tuple[int, int, int], str, tuple[int, int, int]] | None'"
    ),
    "_apply_inventory_dispositions": (
        "(paths: 'KnowledgeStatePaths', snapshot: 'KnowledgeSnapshot', "
        "rankings: 'Mapping[str, Sequence[KnowledgeCandidate]]', *, "
        "cancellation_check: 'Callable[[], None] | None' = None) -> "
        "'tuple[dict[str, tuple[KnowledgeCandidate, ...]], RankingExecution]'"
    ),
}
INVENTORY_DELEGATES = tuple(
    name for name in EXPECTED_SIGNATURES if name != "_decimal_identity_value"
)
INVENTORY_IMPLEMENTATIONS = {
    "_open_direct_readonly_sqlite": "_inventory_open_direct_readonly_sqlite",
    "_physical_identity_tuple": "_inventory_physical_identity_tuple",
    "_inventory_plan_heads": "_inventory_plan_heads_impl",
    "_inventory_identity_blob": "_inventory_identity_blob_impl",
    "_validated_inventory_blob": "_inventory_validated_inventory_blob",
    "_valid_full_fingerprint": "_inventory_valid_full_fingerprint",
    "_inventory_relation_row": "_inventory_relation_row_impl",
    "_apply_inventory_dispositions": "_inventory_apply_inventory_dispositions",
}
LATE_BOUND_GLOBALS = {
    "_open_direct_readonly_sqlite": {
        "sqlite3",
        "readonly_sqlite_uri",
        "_cleanup_preserving_primary",
    },
    "_decimal_identity_value": set(),
    "_physical_identity_tuple": {"FileIdentity", "FileIdentityError"},
    "_inventory_plan_heads": {"OwnerAvailability"},
    "_inventory_identity_blob": set(),
    "_validated_inventory_blob": {"FileIdentity"},
    "_valid_full_fingerprint": set(),
    "_inventory_relation_row": {
        "_validated_inventory_blob",
        "FileIdentity",
        "_valid_full_fingerprint",
    },
    "_apply_inventory_dispositions": {
        "_owner_available",
        "_inventory_plan_heads",
        "_physical_identity_tuple",
        "_open_direct_readonly_sqlite",
        "_inventory_identity_blob",
        "_validated_inventory_blob",
        "_inventory_relation_row",
        "_cleanup_preserving_primary",
        "INVENTORY_IDENTITY_BATCH_SIZE",
        "INVENTORY_HEAD_BATCH_SIZE",
        "MAX_INVENTORY_RELATIONS",
        "sqlite3",
        "RankingExecution",
        "replace",
    },
}


def _publication(
    generation: int,
    signature: str | None,
    *,
    scope_suffix: str = "",
) -> PublicationHead:
    return PublicationHead(
        f"C:/fixture/{generation}{scope_suffix}",
        f"inventory-scan:{generation}",
        generation,
        signature,
    )


def _snapshot(
    *publications: PublicationHead,
    available: bool = True,
) -> KnowledgeSnapshot:
    return KnowledgeSnapshot.create(
        source_version="0.7.1",
        captured_at_utc="2026-07-30T12:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot(
                "inventory",
                (OwnerAvailability.AVAILABLE if available else OwnerAvailability.ABSENT),
                7,
                7 if available else None,
                publications=publications,
            ),
        ),
    )


def _candidate(
    identity: tuple[int, int, int],
    *,
    marker: str,
) -> KnowledgeCandidate:
    physical = ":".join(str(value) for value in identity)
    resource = ResourceRef(
        f"resource:file:{physical}",
        "pdf",
        "pdf",
        PhysicalIdentityRef("windows_file_id_birthtime", physical, 1),
        f"C:/fixture/{marker}.pdf",
    )
    revision = RevisionRef(
        resource.resource_id,
        f"revision:{marker}",
        "fixture-owner",
        "fixture-v1",
        1,
        RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        f"evidence:{marker}",
        resource.resource_id,
        revision.revision_id,
        EvidenceMethod.EXTRACTED,
        section_kind="pdf_page",
        section_id="1",
        identifiers=(("fixture", marker),),
    )
    return KnowledgeCandidate(
        resource,
        revision,
        evidence,
        RankingSignal("fts_pdf", "fixture", 1.0, 1),
        "inventory extraction fixture",
        warnings=("existing_warning",),
    )


def _blob(value: int) -> bytes:
    return value.to_bytes(16, "little")


def _coverage_row(identity: tuple[int, int, int]) -> dict[str, object]:
    return {
        "file_volume_id": _blob(identity[0]),
        "file_id": _blob(identity[1]),
        "file_birthtime_ns": identity[2],
        "member_present": 0,
        "member_role": None,
    }


def _relation_row(
    matched: tuple[int, int, int],
    *,
    role: str = "redundant",
    keeper: tuple[int, int, int] = (9, 10, 11),
) -> dict[str, object]:
    member_path = "C:/fixture/member.pdf"
    keeper_path = "C:/fixture/keeper.pdf"
    size = 100
    return {
        "file_volume_id": _blob(matched[0]),
        "file_id": _blob(matched[1]),
        "file_birthtime_ns": matched[2],
        "file_path": member_path,
        "file_size": size,
        "member_volume_id": _blob(matched[0]),
        "member_file_id": _blob(matched[1]),
        "member_birthtime_ns": matched[2],
        "member_present": 1,
        "member_order": 0 if role == "keep" else 1,
        "member_role": role,
        "member_path": member_path,
        "member_size": size,
        "group_size": size,
        "redundant_count": 1,
        "group_reclaimable_bytes": size,
        "full_fingerprint": "a" * 32,
        "keeper_volume_id": _blob(keeper[0]),
        "keeper_file_id": _blob(keeper[1]),
        "keeper_birthtime_ns": keeper[2],
        "keeper_member_order": 0,
        "keeper_role": "keep",
        "keeper_path": keeper_path,
        "keeper_size": size,
        "keeper_file_path": keeper_path,
        "keeper_file_size": size,
        "keep_path_matches": 1,
        "member_count": 2,
        "distinct_member_order_count": 2,
        "keep_count": 1,
        "redundant_role_count": 1,
        "invalid_role_order_count": 0,
    }


def test_inventory_facade_seam_signatures_metadata_and_pickle_are_stable() -> None:
    for name, expected_signature in EXPECTED_SIGNATURES.items():
        seam = getattr(knowledge_search, name)
        assert str(inspect.signature(seam)) == expected_signature
        assert seam.__module__ == PUBLIC_MODULE
        assert seam.__qualname__ == name
        assert pickle.loads(pickle.dumps(seam, protocol=5)) is seam


@pytest.mark.parametrize("name", INVENTORY_DELEGATES)
def test_inventory_facade_seams_are_thin_late_bound_delegates(name: str) -> None:
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
    assert statement.value.func.id.startswith("_inventory_")
    referenced_names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    assert LATE_BOUND_GLOBALS[name] <= referenced_names


def test_inventory_extraction_module_exists_without_a_facade_cycle() -> None:
    spec = importlib.util.find_spec(INVENTORY_MODULE)
    assert spec is not None
    assert spec.origin is not None
    module_path = Path(spec.origin)
    source = module_path.read_text(encoding="utf-8")
    # The canonical read path now includes an explicit injected-provider seam
    # so tests can remain hermetic while production uses the immutable kernel.
    # CA-04 keeps the owner-scope and proof validation in this extracted
    # module; the bound remains below the facade and leaves room for the
    # explicit per-head diagnostics.
    assert len(source.splitlines()) <= 1140
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "knowledge_search" not in imported_modules
    assert PUBLIC_MODULE not in imported_modules
    assert not any(module.endswith(".knowledge_search") for module in imported_modules)


@pytest.mark.parametrize(
    "module_order",
    ((PUBLIC_MODULE, INVENTORY_MODULE), (INVENTORY_MODULE, PUBLIC_MODULE)),
    ids=("facade-first", "inventory-helper-first"),
)
def test_inventory_helper_and_facade_support_both_cold_import_orders(
    module_order: tuple[str, str],
) -> None:
    repository = Path(knowledge_search.__file__).resolve().parents[2]
    helper_exports = tuple(name.removeprefix("_") for name in INVENTORY_DELEGATES)
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        sys.path.insert(0, {str(repository)!r})
        for module_name in {module_order!r}:
            importlib.import_module(module_name)
        facade = importlib.import_module({PUBLIC_MODULE!r})
        helper = importlib.import_module({INVENTORY_MODULE!r})
        for public_name in {INVENTORY_DELEGATES!r}:
            seam = getattr(facade, public_name)
            assert seam.__module__ == {PUBLIC_MODULE!r}
            assert seam.__qualname__ == public_name
        for helper_name in {helper_exports!r}:
            assert callable(getattr(helper, helper_name))
        print("ok")
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


def test_inventory_facades_inject_every_current_provider_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FileIdentityFailure(Exception):
        pass

    class SQLiteFailure(Exception):
        pass

    class SQLiteOperationalFailure(SQLiteFailure):
        pass

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    expected_result = object()

    def implementation(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return expected_result

    def contains_identity(value: object, expected: object) -> bool:
        if value is expected:
            return True
        if isinstance(value, Mapping):
            return any(contains_identity(item, expected) for pair in value.items() for item in pair)
        if isinstance(value, (tuple, list, set, frozenset)):
            return any(contains_identity(item, expected) for item in value)
        return False

    sqlite_connect = object()
    sqlite_row = object()
    sqlite_namespace = SimpleNamespace(
        connect=sqlite_connect,
        Row=sqlite_row,
        OperationalError=SQLiteOperationalFailure,
        Error=SQLiteFailure,
    )
    available_state = object()
    owner_availability = SimpleNamespace(AVAILABLE=available_state)
    readonly_sqlite_uri_provider = object()
    cleanup_provider = object()
    file_identity_provider = object()
    owner_available_provider = object()
    plan_heads_provider = object()
    physical_identity_provider = object()
    open_provider = object()
    identity_blob_provider = object()
    validated_blob_provider = object()
    relation_row_provider = object()
    fingerprint_provider = object()
    identity_batch_size_provider = object()
    head_batch_size_provider = object()
    relation_limit_provider = object()
    ranking_execution_provider = object()
    replace_provider = object()
    path = Path("C:/fixture/inventory.sqlite3")
    resource = object()
    snapshot = object()
    row = object()
    rankings = object()
    cancellation = object()

    cases = (
        (
            "_open_direct_readonly_sqlite",
            lambda seam: seam(path),
            {
                "sqlite3": (
                    sqlite_namespace,
                    (sqlite_connect, sqlite_row, SQLiteOperationalFailure),
                ),
                "readonly_sqlite_uri": (
                    readonly_sqlite_uri_provider,
                    (readonly_sqlite_uri_provider,),
                ),
                "_cleanup_preserving_primary": (
                    cleanup_provider,
                    (cleanup_provider,),
                ),
            },
        ),
        (
            "_physical_identity_tuple",
            lambda seam: seam(resource),
            {
                "FileIdentity": (
                    file_identity_provider,
                    (file_identity_provider,),
                ),
                "FileIdentityError": (
                    FileIdentityFailure,
                    (FileIdentityFailure,),
                ),
            },
        ),
        (
            "_inventory_plan_heads",
            lambda seam: seam(snapshot),
            {"OwnerAvailability": (owner_availability, (available_state,))},
        ),
        (
            "_validated_inventory_blob",
            lambda seam: seam(object()),
            {
                "FileIdentity": (
                    file_identity_provider,
                    (file_identity_provider,),
                )
            },
        ),
        (
            "_inventory_relation_row",
            lambda seam: seam(row),
            {
                "_validated_inventory_blob": (
                    validated_blob_provider,
                    (validated_blob_provider,),
                ),
                "FileIdentity": (
                    file_identity_provider,
                    (file_identity_provider,),
                ),
                "_valid_full_fingerprint": (
                    fingerprint_provider,
                    (fingerprint_provider,),
                ),
            },
        ),
        (
            "_apply_inventory_dispositions",
            lambda seam: seam(
                path,
                snapshot,
                rankings,
                cancellation_check=cancellation,
            ),
            {
                "_owner_available": (
                    owner_available_provider,
                    (owner_available_provider,),
                ),
                "_inventory_plan_heads": (
                    plan_heads_provider,
                    (plan_heads_provider,),
                ),
                "_physical_identity_tuple": (
                    physical_identity_provider,
                    (physical_identity_provider,),
                ),
                "_open_direct_readonly_sqlite": (
                    open_provider,
                    (open_provider,),
                ),
                "_inventory_identity_blob": (
                    identity_blob_provider,
                    (identity_blob_provider,),
                ),
                "_validated_inventory_blob": (
                    validated_blob_provider,
                    (validated_blob_provider,),
                ),
                "_inventory_relation_row": (
                    relation_row_provider,
                    (relation_row_provider,),
                ),
                "_cleanup_preserving_primary": (
                    cleanup_provider,
                    (cleanup_provider,),
                ),
                "INVENTORY_IDENTITY_BATCH_SIZE": (
                    identity_batch_size_provider,
                    (identity_batch_size_provider,),
                ),
                "INVENTORY_HEAD_BATCH_SIZE": (
                    head_batch_size_provider,
                    (head_batch_size_provider,),
                ),
                "MAX_INVENTORY_RELATIONS": (
                    relation_limit_provider,
                    (relation_limit_provider,),
                ),
                "sqlite3": (sqlite_namespace, (SQLiteFailure,)),
                "RankingExecution": (
                    ranking_execution_provider,
                    (ranking_execution_provider,),
                ),
                "replace": (replace_provider, (replace_provider,)),
            },
        ),
    )

    for seam_name, invoke, replacements in cases:
        calls.clear()
        with monkeypatch.context() as patcher:
            patcher.setattr(
                knowledge_search,
                INVENTORY_IMPLEMENTATIONS[seam_name],
                implementation,
                raising=False,
            )
            expected_providers: list[object] = []
            for global_name, (replacement, forwarded_values) in replacements.items():
                patcher.setattr(knowledge_search, global_name, replacement)
                expected_providers.extend(forwarded_values)
            seam = getattr(knowledge_search, seam_name)
            assert invoke(seam) is expected_result

        assert len(calls) == 1
        args, kwargs = calls[0]
        forwarded = (*args, *kwargs.values())
        for provider in expected_providers:
            assert any(contains_identity(value, provider) for value in forwarded), (
                seam_name,
                provider,
                forwarded,
            )


def test_direct_sqlite_open_resolves_current_dependencies_and_exact_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    row_factory = object()

    class Connection:
        row_factory: object = None
        last_statement = ""

        def execute(self, statement: str) -> Connection:
            self.last_statement = statement
            events.append(statement)
            return self

        def fetchone(self) -> tuple[int]:
            events.append(("fetchone", self.last_statement))
            return (1,)

        def close(self) -> None:
            events.append("CLOSE")

    connection = Connection()
    path = tmp_path / "owner.sqlite3"

    def uri(value: Path) -> str:
        assert value is path
        events.append(("uri", value))
        return "file:fixture-owner?mode=ro"

    def connect(database: str, **kwargs: object) -> Connection:
        events.append(("connect", database, kwargs))
        return connection

    monkeypatch.setattr(knowledge_search, "readonly_sqlite_uri", uri)
    monkeypatch.setattr(knowledge_search.sqlite3, "connect", connect)
    monkeypatch.setattr(knowledge_search.sqlite3, "Row", row_factory)

    opened = knowledge_search._open_direct_readonly_sqlite(path)

    assert cast(object, opened) is connection
    assert connection.row_factory is row_factory
    assert events == [
        ("uri", path),
        ("connect", "file:fixture-owner?mode=ro", {"uri": True, "timeout": 60}),
        "PRAGMA foreign_keys=ON",
        "PRAGMA query_only=ON",
        "PRAGMA busy_timeout=60000",
        "PRAGMA foreign_keys",
        ("fetchone", "PRAGMA foreign_keys"),
        "PRAGMA query_only",
        ("fetchone", "PRAGMA query_only"),
    ]


def test_inventory_open_sqlite_error_before_connection_returns_exact_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = sqlite3.OperationalError("inventory owner unavailable")
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    candidate = _candidate((1, 2, 3), marker="open-failure")
    rankings = {"fts_pdf": (candidate,)}

    def failed_open(path: Path) -> None:
        assert path == paths.inventory
        raise expected

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        failed_open,
    )

    updated, report = knowledge_search._apply_inventory_dispositions(
        paths,
        _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
        rankings,
    )

    assert updated == rankings
    assert report.to_dict() == {
        "name": "inventory_duplicate_plan",
        "channel": "relationship",
        "executed": True,
        "available": False,
        "complete": False,
        "returned": 0,
        "rows_scanned": 0,
        "row_count_semantics": "materialized_lower_bound",
        "vectors_scanned": 0,
        "reason": "owner_read_failed:OperationalError",
    }


def test_direct_sqlite_setup_preserves_primary_and_close_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ValueError("read-only setup failed")
    close_failure = RuntimeError("connection close failed")
    events: list[str] = []
    cleanup_labels: list[str] = []
    original_cleanup = knowledge_search._cleanup_preserving_primary

    class Connection:
        row_factory: object = None

        def execute(self, _statement: str) -> None:
            events.append("EXECUTE")
            raise primary

        def close(self) -> None:
            events.append("CLOSE")
            raise close_failure

    def cleanup(
        action: Callable[[], object],
        received_primary: BaseException,
        *,
        label: str,
    ) -> None:
        assert received_primary is primary
        cleanup_labels.append(label)
        original_cleanup(action, received_primary, label=label)

    monkeypatch.setattr(
        knowledge_search.sqlite3,
        "connect",
        lambda *_args, **_kwargs: Connection(),
    )
    monkeypatch.setattr(knowledge_search, "_cleanup_preserving_primary", cleanup)

    with pytest.raises(ValueError) as raised:
        knowledge_search._open_direct_readonly_sqlite(tmp_path / "owner.sqlite3")

    assert raised.value is primary
    assert events == ["EXECUTE", "CLOSE"]
    assert cleanup_labels == ["direct read-only SQLite close cleanup"]
    assert primary.__notes__ == [
        "direct read-only SQLite close cleanup failed: RuntimeError: connection close failed"
    ]


def test_inventory_identity_primitives_and_canonical_physical_identity() -> None:
    assert knowledge_search._decimal_identity_value(b"\x01\x02") == 513
    assert knowledge_search._decimal_identity_value(memoryview(b"\x03")) == 3
    assert knowledge_search._decimal_identity_value(7) == 7
    assert knowledge_search._decimal_identity_value("8") == 8
    with pytest.raises(ValueError, match="physical identity cannot be boolean"):
        knowledge_search._decimal_identity_value(True)
    with pytest.raises(ValueError, match="physical identity must be"):
        knowledge_search._decimal_identity_value(object())

    encoded = knowledge_search._inventory_identity_blob(17)
    assert encoded == _blob(17)
    assert knowledge_search._validated_inventory_blob(encoded) == 17
    with pytest.raises(ValueError, match="not a BLOB"):
        knowledge_search._validated_inventory_blob("17")
    with pytest.raises(ValueError, match="not 16 bytes"):
        knowledge_search._validated_inventory_blob(b"\x11")

    valid = _candidate((1, 2, 3), marker="valid").resource
    assert knowledge_search._physical_identity_tuple(valid) == (1, 2, 3)

    invalid_resources = (
        ResourceRef(
            "resource:file:01:2:3",
            "pdf",
            "pdf",
            PhysicalIdentityRef("windows_file_id_birthtime", "01:2:3", 1),
        ),
        ResourceRef(
            "resource:file:1:2:3",
            "pdf",
            "pdf",
            PhysicalIdentityRef("owner_file_key", "1:2:3", 1),
        ),
        ResourceRef(
            "resource:file:1:2:-1",
            "pdf",
            "pdf",
            PhysicalIdentityRef("windows_file_id_birthtime", "1:2:-1", 1),
        ),
        ResourceRef(
            "resource:file:1:2:3:4",
            "pdf",
            "pdf",
            PhysicalIdentityRef(
                "windows_file_id_birthtime",
                "1:2:3:4",
                1,
            ),
        ),
    )
    assert all(
        knowledge_search._physical_identity_tuple(resource) is None
        for resource in invalid_resources
    )


def test_inventory_plan_heads_are_sorted_deduplicated_and_strict() -> None:
    snapshot = _snapshot(
        _publication(2, "duplicate-plan-v1:20:2:1:100"),
        _publication(1, "duplicate-plan-v1:10:1:1:50"),
        _publication(
            2,
            "duplicate-plan-v1:20:2:1:100",
            scope_suffix="-duplicate",
        ),
        _publication(3, "duplicate-plan-v1:03:0:0:0"),
        _publication(4, None),
    )

    heads, malformed = knowledge_search._inventory_plan_heads(snapshot)

    assert tuple(head.sql_values for head in heads) == (
        (1, 10, 1, 1, 50),
        (2, 20, 2, 1, 100),
        (2, 20, 2, 1, 100),
    )
    assert all(head.scope for head in heads)
    assert len(malformed) == 1
    assert malformed[0].scan_id == 3
    assert malformed[0].reason == "invalid_inventory_plan_watermark"


def test_malformed_head_only_degrades_identities_in_its_scan(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "inventory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE files(scan_id,path,volume_id,file_id,birthtime_ns,size);
            CREATE TABLE duplicate_plan_summaries(scan_id,completed_ns,group_count,redundant_files,reclaimable_bytes);
            CREATE TABLE planned_duplicate_groups(group_id,scan_id,size,keep_path,redundant_count,reclaimable_bytes,full_fingerprint);
            CREATE TABLE planned_duplicate_members(group_id,member_order,role,path,volume_id,file_id,size,birthtime_ns);
            INSERT INTO duplicate_plan_summaries VALUES(2,20,1,1,100);
            INSERT INTO planned_duplicate_groups VALUES(1,2,100,'C:/keeper.pdf',1,100,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
            """
        )
        connection.executemany(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            (
                (1, "C:/bad.pdf", _blob(1), _blob(2), 3, 100),
                (2, "C:/good.pdf", _blob(9), _blob(10), 11, 100),
                (2, "C:/keeper.pdf", _blob(11), _blob(12), 12, 100),
            ),
        )
        connection.executemany(
            "INSERT INTO planned_duplicate_members VALUES(?,?,?,?,?,?,?,?)",
            (
                (1, 0, "keep", "C:/keeper.pdf", _blob(11), _blob(12), 100, 12),
                (1, 1, "redundant", "C:/good.pdf", _blob(9), _blob(10), 100, 11),
            ),
        )
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(knowledge_search, "_open_direct_readonly_sqlite", lambda _path: connection)
    snapshot = _snapshot(
        _publication(1, "duplicate-plan-v1:02:0:0:0"),
        _publication(2, "duplicate-plan-v1:20:1:1:100"),
    )
    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        snapshot,
        {"fts_pdf": (_candidate((1, 2, 3), marker="bad"), _candidate((9, 10, 11), marker="good"))},
    )
    connection.close()
    assert "inventory_duplicate_plan_ambiguous" in updated["fts_pdf"][0].warnings
    assert "inventory_duplicate_plan_ambiguous" not in updated["fts_pdf"][1].warnings
    assert report.reason == "invalid_or_conflicting_duplicate_plan"


def test_current_plan_without_member_proof_is_not_a_valid_relation(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    first, second = root / "first.bin", root / "second.bin"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=True, preview_limit=10)
        summary = index._connection.execute(
            "SELECT completed_ns,group_count,redundant_files,reclaimable_bytes FROM duplicate_plan_summaries WHERE scan_id=?",
            (scan.scan_id,),
        ).fetchone()
        assert summary is not None and plan.group_count == 1
        identity = snapshot_path(Path(plan.groups[0].redundant[0].path))
        index._connection.row_factory = sqlite3.Row
        rows_valid = knowledge_search_inventory._inventory_rows(
            index._connection,
            [(*identity.identity, identity.birthtime_ns)],
            [(scan.scan_id, *map(int, summary))],
            10,
            knowledge_search._inventory_identity_blob,
        )
        assert (
            rows_valid
            and knowledge_search_inventory.inventory_relation_row(
                rows_valid[0],
                validated_inventory_blob=knowledge_search._validated_inventory_blob,
                file_identity_type=FileIdentity,
                valid_full_fingerprint=knowledge_search._valid_full_fingerprint,
            )
            is not None
        )
        index._connection.execute(
            "UPDATE planned_duplicate_members SET proof_json='{}' WHERE group_id=(SELECT group_id FROM planned_duplicate_groups WHERE scan_id=?) AND member_order=1",
            (scan.scan_id,),
        )
        index._connection.commit()
        rows = knowledge_search_inventory._inventory_rows(
            index._connection,
            [(*identity.identity, identity.birthtime_ns)],
            [(scan.scan_id, *map(int, summary))],
            10,
            knowledge_search._inventory_identity_blob,
        )
        assert rows
        assert (
            knowledge_search_inventory.inventory_relation_row(
                rows[0],
                validated_inventory_blob=knowledge_search._validated_inventory_blob,
                file_identity_type=FileIdentity,
                valid_full_fingerprint=knowledge_search._valid_full_fingerprint,
            )
            is None
        )


def test_inventory_relation_validation_preserves_roles_and_rejects_conflicts() -> None:
    redundant = _relation_row((1, 2, 3))
    assert knowledge_search._inventory_relation_row(_as_sqlite_row(redundant)) == (
        (1, 2, 3),
        "redundant",
        (9, 10, 11),
    )

    keep = _relation_row((9, 10, 11), role="keep")
    assert knowledge_search._inventory_relation_row(_as_sqlite_row(keep)) == (
        (9, 10, 11),
        "keep",
        (9, 10, 11),
    )

    linux = _relation_row((1, 2, -1), keeper=(9, 10, -1))
    linux["file_birthtime_ns"] = -1
    linux["member_birthtime_ns"] = -1
    linux["keeper_birthtime_ns"] = -1
    assert knowledge_search._inventory_relation_row(_as_sqlite_row(linux)) == (
        (1, 2, -1),
        "redundant",
        (9, 10, -1),
    )

    invalid_rows: list[Mapping[str, object]] = []
    for key, value in (
        ("full_fingerprint", "A" * 32),
        ("member_count", 3),
        ("keep_count", 2),
        ("keep_path_matches", 0),
        ("member_path", "C:/other.pdf"),
        ("member_role", "unknown"),
    ):
        row = dict(redundant)
        row[key] = value
        invalid_rows.append(row)
    conflicting_keep = dict(keep)
    conflicting_keep["member_order"] = 1
    invalid_rows.append(conflicting_keep)

    assert all(
        knowledge_search._inventory_relation_row(_as_sqlite_row(row)) is None
        for row in invalid_rows
    )


def test_inventory_batches_in_sorted_order_and_preserves_safe_dispositions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    query_calls: list[tuple[tuple[int, int, int], tuple[int, int, int, int, int], int]] = []

    class Result:
        def __init__(self, rows: list[Mapping[str, object]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[Mapping[str, object]]:
            return self._rows

    class Connection:
        in_transaction = False

        def execute(
            self,
            statement: str,
            parameters: tuple[bytes | int, ...] = (),
        ) -> object:
            if statement == "BEGIN":
                events.append("BEGIN")
                self.in_transaction = True
                return self
            if statement == "COMMIT":
                events.append("COMMIT")
                self.in_transaction = False
                return self
            if statement == "ROLLBACK":
                events.append("ROLLBACK")
                self.in_transaction = False
                return self
            assert statement.startswith("WITH wanted")
            normalized_sql = " ".join(statement.split())
            assert (
                "ORDER BY f.volume_id,f.file_id,f.birthtime_ns, "
                "g.scan_id,g.group_id LIMIT ?" in normalized_sql
            )
            identity = (
                int.from_bytes(bytes(parameters[0]), "little"),
                int.from_bytes(bytes(parameters[1]), "little"),
                int(parameters[2]),
            )
            head = tuple(int(value) for value in parameters[3:8])
            assert len(head) == 5
            limit = int(parameters[-1])
            query_calls.append((identity, head, limit))
            events.append(("SELECT", identity, head, limit))
            if identity == (1, 2, 3):
                return Result([_relation_row(identity)])
            return Result(
                [
                    {
                        "file_volume_id": _blob(identity[0]),
                        "file_id": _blob(identity[1]),
                        "file_birthtime_ns": identity[2],
                        "member_role": None,
                    }
                ]
            )

        def close(self) -> None:
            events.append("CLOSE")

    connection = Connection()
    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: connection,
    )
    monkeypatch.setattr(knowledge_search, "INVENTORY_IDENTITY_BATCH_SIZE", 1)
    monkeypatch.setattr(knowledge_search, "INVENTORY_HEAD_BATCH_SIZE", 1)
    monkeypatch.setattr(knowledge_search, "MAX_INVENTORY_RELATIONS", 10)

    candidate_three = _candidate((3, 4, 5), marker="three")
    candidate_one = _candidate((1, 2, 3), marker="one")
    rankings = {"fts_pdf": (candidate_three, candidate_one)}
    snapshot = _snapshot(
        _publication(2, "duplicate-plan-v1:20:2:1:100"),
        _publication(1, "duplicate-plan-v1:10:1:1:50"),
    )

    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        snapshot,
        rankings,
    )

    assert query_calls == [
        ((1, 2, 3), (1, 10, 1, 1, 50), 11),
        ((1, 2, 3), (2, 20, 2, 1, 100), 10),
        ((3, 4, 5), (1, 10, 1, 1, 50), 9),
        ((3, 4, 5), (2, 20, 2, 1, 100), 8),
    ]
    assert events[0] == "BEGIN"
    assert events[-2:] == ["COMMIT", "CLOSE"]
    assert updated["fts_pdf"][0] is candidate_three
    planned = updated["fts_pdf"][1]
    assert planned.resource.disposition is None
    assert planned.evidence.identifiers == (
        ("fixture", "one"),
        ("planned_duplicate_of", "resource:file:9:10:11"),
    )
    assert planned.warnings == (
        "existing_warning",
        "inventory_planned_duplicate_unverified",
    )
    assert report.to_dict() == {
        "name": "inventory_duplicate_plan",
        "channel": "relationship",
        "executed": True,
        "available": True,
        "complete": False,
        "returned": 1,
        "rows_scanned": 4,
        "row_count_semantics": "materialized_lower_bound",
        "vectors_scanned": 0,
        "reason": "inventory_exact_verification_unavailable",
    }


def test_inventory_cancellation_preserves_primary_through_rollback_and_close_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("inventory cancellation fixture")
    rollback_failure = RuntimeError("inventory rollback failed")
    close_failure = RuntimeError("inventory close failed")
    events: list[str] = []
    cleanup_labels: list[str] = []
    original_cleanup = knowledge_search._cleanup_preserving_primary

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> Connection:
            events.append(statement)
            if statement == "BEGIN":
                self.in_transaction = True
            elif statement == "ROLLBACK":
                raise rollback_failure
            return self

        def close(self) -> None:
            events.append("CLOSE")
            raise close_failure

    def cleanup(
        action: Callable[[], object],
        received_primary: BaseException,
        *,
        label: str,
    ) -> None:
        assert received_primary is primary
        cleanup_labels.append(label)
        original_cleanup(action, received_primary, label=label)

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    monkeypatch.setattr(knowledge_search, "_cleanup_preserving_primary", cleanup)

    def cancel() -> None:
        raise primary

    with pytest.raises(RuntimeError) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            {"fts_pdf": (_candidate((1, 2, 3), marker="cancel"),)},
            cancellation_check=cancel,
        )

    assert raised.value is primary
    assert events == ["BEGIN", "ROLLBACK", "CLOSE"]
    assert cleanup_labels == [
        "inventory read rollback cleanup",
        "inventory read connection close cleanup",
    ]
    assert primary.__notes__ == [
        "inventory read rollback cleanup failed: RuntimeError: inventory rollback failed",
        "inventory read connection close cleanup failed: RuntimeError: inventory close failed",
    ]


def test_inventory_sqlite_cancellation_preserves_exact_callback_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = sqlite3.OperationalError("inventory sqlite cancellation fixture")
    events: list[str] = []

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> Connection:
            events.append(statement)
            if statement == "BEGIN":
                self.in_transaction = True
            elif statement == "ROLLBACK":
                self.in_transaction = False
            return self

        def close(self) -> None:
            events.append("CLOSE")

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )

    def cancel() -> None:
        raise expected

    with pytest.raises(sqlite3.OperationalError) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            {"fts_pdf": (_candidate((1, 2, 3), marker="sqlite-cancel"),)},
            cancellation_check=cancel,
        )

    assert raised.value is expected
    assert events == ["BEGIN", "ROLLBACK", "CLOSE"]


def test_inventory_sqlite_callback_preserves_identity_through_both_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = sqlite3.OperationalError("inventory sqlite cancellation fixture")
    rollback_failure = RuntimeError("inventory rollback failed")
    close_failure = RuntimeError("inventory close failed")
    events: list[str] = []

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> Connection:
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "ROLLBACK":
                events.append("ROLLBACK")
                raise rollback_failure
            raise AssertionError(f"unexpected statement after cancellation: {statement}")

        def close(self) -> None:
            events.append("CLOSE")
            raise close_failure

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )

    def cancel() -> None:
        raise expected

    with pytest.raises(sqlite3.OperationalError) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            {"fts_pdf": (_candidate((1, 2, 3), marker="sqlite-cleanup"),)},
            cancellation_check=cancel,
        )

    assert raised.value is expected
    assert events == ["BEGIN", "ROLLBACK", "CLOSE"]
    assert expected.__notes__ == [
        "inventory read rollback cleanup failed: RuntimeError: inventory rollback failed",
        "inventory read connection close cleanup failed: RuntimeError: inventory close failed",
    ]


def test_execute_knowledge_search_preserves_inventory_sqlite_callback_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = sqlite3.OperationalError("outer inventory cancellation fixture")
    identity = (1, 2, 3)
    candidate = _candidate(identity, marker="outer-sqlite-cancel")
    events: list[str] = []
    inside_inventory = False
    raised_once = False

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> Connection:
            nonlocal inside_inventory
            if statement == "BEGIN":
                self.in_transaction = True
                inside_inventory = True
                events.append("BEGIN")
                return self
            if statement == "ROLLBACK":
                self.in_transaction = False
                inside_inventory = False
                events.append("ROLLBACK")
                return self
            raise AssertionError(f"unexpected statement after cancellation: {statement}")

        def close(self) -> None:
            events.append("CLOSE")

    def cancellation() -> None:
        nonlocal raised_once
        if inside_inventory and not raised_once:
            raised_once = True
            raise expected

    def lexical(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[dict[str, tuple[KnowledgeCandidate, ...]], list[object]]:
        return {"fts_pdf": (candidate,)}, []

    def forbidden_fusion(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("inventory callback cancellation reached fusion")

    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical)
    monkeypatch.setattr(knowledge_search, "_planned", lambda *_args: False)
    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    monkeypatch.setattr(
        knowledge_search,
        "fuse_evidence_rankings",
        forbidden_fusion,
    )

    with pytest.raises(sqlite3.OperationalError) as raised:
        knowledge_search.execute_knowledge_search(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            plan_knowledge_query(KnowledgeQuery("breaker protection")),
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            cancellation_check=cancellation,
        )

    assert raised.value is expected
    assert raised_once
    assert events == ["BEGIN", "ROLLBACK", "CLOSE"]


def test_inventory_sqlite_failure_reports_rollback_and_closes_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ReadFailure(sqlite3.OperationalError):
        pass

    class RollbackFailure(sqlite3.OperationalError):
        pass

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> Connection:
            if statement == "BEGIN":
                events.append("BEGIN")
                self.in_transaction = True
                return self
            if statement == "ROLLBACK":
                events.append("ROLLBACK")
                raise RollbackFailure("rollback failed")
            events.append("SELECT")
            raise ReadFailure("read failed")

        def close(self) -> None:
            events.append("CLOSE")

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    rankings = {"fts_pdf": (_candidate((1, 2, 3), marker="failure"),)}

    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
        rankings,
    )

    assert updated == rankings
    assert events == ["BEGIN", "SELECT", "ROLLBACK", "CLOSE"]
    assert report.executed
    assert report.available
    assert not report.complete
    assert report.returned == 0
    assert report.rows_scanned == 0
    assert report.reason == ("owner_read_failed:ReadFailure:rollback_failed:RollbackFailure")


def test_inventory_early_returns_are_complete_and_never_open_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate((1, 2, 3), marker="early")
    rankings = {"fts_pdf": [candidate]}
    open_calls: list[Path] = []

    def forbidden_open(path: Path) -> None:
        open_calls.append(path)
        raise AssertionError("early inventory return opened SQLite")

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        forbidden_open,
    )
    monkeypatch.setattr(
        knowledge_search,
        "_physical_identity_tuple",
        lambda _resource: None,
    )
    cases = (
        (
            _snapshot(available=False),
            {
                "name": "inventory_duplicate_plan",
                "channel": "relationship",
                "executed": False,
                "available": False,
                "complete": True,
                "returned": 0,
                "rows_scanned": 0,
                "row_count_semantics": "materialized_lower_bound",
                "vectors_scanned": 0,
                "reason": "inventory_owner_unavailable",
            },
        ),
        (
            _snapshot(_publication(1, "duplicate-plan-v1:02:0:0:0")),
            {
                "name": "inventory_duplicate_plan",
                "channel": "relationship",
                "executed": False,
                "available": True,
                "complete": True,
                "returned": 0,
                "rows_scanned": 0,
                "row_count_semantics": "materialized_lower_bound",
                "vectors_scanned": 0,
                "reason": "no_physical_candidates",
            },
        ),
        (
            _snapshot(),
            {
                "name": "inventory_duplicate_plan",
                "channel": "relationship",
                "executed": False,
                "available": True,
                "complete": True,
                "returned": 0,
                "rows_scanned": 0,
                "row_count_semantics": "materialized_lower_bound",
                "vectors_scanned": 0,
                "reason": "no_completed_inventory_plans",
            },
        ),
        (
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            {
                "name": "inventory_duplicate_plan",
                "channel": "relationship",
                "executed": False,
                "available": True,
                "complete": True,
                "returned": 0,
                "rows_scanned": 0,
                "row_count_semantics": "materialized_lower_bound",
                "vectors_scanned": 0,
                "reason": "no_physical_candidates",
            },
        ),
    )

    for snapshot, expected_report in cases:
        updated, report = knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            snapshot,
            rankings,
        )
        assert updated == {"fts_pdf": (candidate,)}
        assert report.to_dict() == expected_report
    assert open_calls == []


def test_inventory_global_relation_lookahead_rolls_back_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Result:
        def fetchall(self) -> list[object]:
            return [object(), object(), object()]

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "ROLLBACK":
                self.in_transaction = False
                events.append("ROLLBACK")
                return self
            if statement == "COMMIT":
                raise AssertionError("lookahead overflow committed")
            events.append("SELECT")
            return Result()

        def close(self) -> None:
            events.append("CLOSE")

    connection = Connection()
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda path: connection if path == paths.inventory else None,
    )
    monkeypatch.setattr(knowledge_search, "MAX_INVENTORY_RELATIONS", 2)
    candidate = _candidate((1, 2, 3), marker="lookahead")
    rankings = {"fts_pdf": (candidate,)}

    updated, report = knowledge_search._apply_inventory_dispositions(
        paths,
        _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
        rankings,
    )

    assert updated == rankings
    assert events == ["BEGIN", "SELECT", "ROLLBACK", "CLOSE"]
    assert report.to_dict() == {
        "name": "inventory_duplicate_plan",
        "channel": "relationship",
        "executed": True,
        "available": True,
        "complete": False,
        "returned": 0,
        "rows_scanned": 2,
        "row_count_semantics": "materialized_lower_bound",
        "vectors_scanned": 0,
        "reason": "inventory_relation_limit_exceeded",
    }


def test_inventory_close_failure_after_successful_commit_is_repropagated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_failure = RuntimeError("inventory post-commit close failed")
    events: list[str] = []

    class Result:
        def fetchall(self) -> list[object]:
            return []

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                events.append("COMMIT")
                return self
            if statement == "ROLLBACK":
                raise AssertionError("successful inventory read rolled back")
            events.append("SELECT")
            return Result()

        def close(self) -> None:
            events.append("CLOSE")
            raise close_failure

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )

    with pytest.raises(RuntimeError) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            {"fts_pdf": (_candidate((1, 2, 3), marker="commit-close"),)},
        )

    assert raised.value is close_failure
    assert events == ["BEGIN", "SELECT", "COMMIT", "CLOSE"]


def test_inventory_close_failure_replaces_handled_sqlite_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_failure = sqlite3.OperationalError("inventory read failed")
    close_failure = RuntimeError("inventory close replaced fallback")
    events: list[str] = []

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> Connection:
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "ROLLBACK":
                self.in_transaction = False
                events.append("ROLLBACK")
                return self
            events.append("SELECT")
            raise read_failure

        def close(self) -> None:
            events.append("CLOSE")
            raise close_failure

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )

    with pytest.raises(RuntimeError) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            {"fts_pdf": (_candidate((1, 2, 3), marker="close"),)},
        )

    assert raised.value is close_failure
    assert events == ["BEGIN", "SELECT", "ROLLBACK", "CLOSE"]


def test_inventory_row_checkpoint_preserves_exact_cancellation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = RuntimeError("inventory row cancellation fixture")
    events: list[str] = []
    cancellation_calls = 0
    validated_blob_calls = 0
    identity = (1, 2, 3)
    row = _coverage_row(identity)
    original_validated_blob = knowledge_search._validated_inventory_blob

    class Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [dict(row) for _ in range(128)]

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "ROLLBACK":
                self.in_transaction = False
                events.append("ROLLBACK")
                return self
            if statement == "COMMIT":
                raise AssertionError("row cancellation committed")
            events.append("SELECT")
            return Result()

        def close(self) -> None:
            events.append("CLOSE")

    def validated_blob(value: object) -> int:
        nonlocal validated_blob_calls
        validated_blob_calls += 1
        return original_validated_blob(value)

    def cancel() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        if cancellation_calls == 3:
            raise expected

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_validated_inventory_blob",
        validated_blob,
    )

    with pytest.raises(RuntimeError) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            {"fts_pdf": (_candidate(identity, marker="row-cancel"),)},
            cancellation_check=cancel,
        )

    assert raised.value is expected
    assert cancellation_calls == 3
    assert validated_blob_calls == 254
    assert events == ["BEGIN", "SELECT", "ROLLBACK", "CLOSE"]


def test_inventory_row_checkpoint_occurs_before_global_row_128(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = (1, 2, 3)
    row = _coverage_row(identity)
    validated_blob_calls = 0
    checkpoint_positions: list[int] = []
    original_validated_blob = knowledge_search._validated_inventory_blob

    class Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [dict(row) for _ in range(128)]

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                return self
            assert statement.startswith("WITH wanted")
            return Result()

        def close(self) -> None:
            return None

    def validated_blob(value: object) -> int:
        nonlocal validated_blob_calls
        validated_blob_calls += 1
        return original_validated_blob(value)

    def checkpoint() -> None:
        checkpoint_positions.append(validated_blob_calls // 2)

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_validated_inventory_blob",
        validated_blob,
    )
    candidate = _candidate(identity, marker="row-cadence")

    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
        {"fts_pdf": (candidate,)},
        cancellation_check=checkpoint,
    )

    assert checkpoint_positions == [0, 0, 127]
    assert validated_blob_calls == 256
    assert updated == {"fts_pdf": (candidate,)}
    assert report.complete
    assert report.rows_scanned == 128
    assert report.reason is None


def test_inventory_row_checkpoint_counter_crosses_64_plus_64_fetches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = (1, 2, 3)
    row = _coverage_row(identity)
    events: list[str] = []
    validated_blob_calls = 0
    checkpoint_positions: list[int] = []
    select_calls = 0
    original_validated_blob = knowledge_search._validated_inventory_blob

    class Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [dict(row) for _ in range(64)]

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            nonlocal select_calls
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                events.append("COMMIT")
                return self
            select_calls += 1
            events.append("SELECT")
            return Result()

        def close(self) -> None:
            events.append("CLOSE")

    def validated_blob(value: object) -> int:
        nonlocal validated_blob_calls
        validated_blob_calls += 1
        return original_validated_blob(value)

    def checkpoint() -> None:
        checkpoint_positions.append(validated_blob_calls // 2)

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_validated_inventory_blob",
        validated_blob,
    )
    monkeypatch.setattr(knowledge_search, "INVENTORY_HEAD_BATCH_SIZE", 1)
    candidate = _candidate(identity, marker="row-cross-fetch")

    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(
            _publication(1, "duplicate-plan-v1:2:0:0:0"),
            _publication(2, "duplicate-plan-v1:3:0:0:0"),
        ),
        {"fts_pdf": (candidate,)},
        cancellation_check=checkpoint,
    )

    assert checkpoint_positions == [0, 0, 64, 127]
    assert validated_blob_calls == 256
    assert select_calls == 2
    assert events == ["BEGIN", "SELECT", "SELECT", "COMMIT", "CLOSE"]
    assert updated == {"fts_pdf": (candidate,)}
    assert report.complete
    assert report.rows_scanned == 128
    assert report.reason is None


@pytest.mark.parametrize(
    ("row_count", "expected_positions"),
    ((129, [0, 0, 127]), (257, [0, 0, 127, 255])),
    ids=("129-rows", "257-rows"),
)
def test_inventory_row_checkpoint_counter_is_global_for_non_multiple_fetches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row_count: int,
    expected_positions: list[int],
) -> None:
    identity = (1, 2, 3)
    row = _coverage_row(identity)
    validated_blob_calls = 0
    checkpoint_positions: list[int] = []
    original_validated_blob = knowledge_search._validated_inventory_blob

    class Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [dict(row) for _ in range(row_count)]

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                return self
            assert statement.startswith("WITH wanted")
            return Result()

        def close(self) -> None:
            return None

    def validated_blob(value: object) -> int:
        nonlocal validated_blob_calls
        validated_blob_calls += 1
        return original_validated_blob(value)

    def checkpoint() -> None:
        checkpoint_positions.append(validated_blob_calls // 2)

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    monkeypatch.setattr(
        knowledge_search,
        "_validated_inventory_blob",
        validated_blob,
    )
    candidate = _candidate(identity, marker=f"row-count-{row_count}")

    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
        {"fts_pdf": (candidate,)},
        cancellation_check=checkpoint,
    )

    assert checkpoint_positions == expected_positions
    assert validated_blob_calls == row_count * 2
    assert updated == {"fts_pdf": (candidate,)}
    assert report.complete
    assert report.rows_scanned == row_count
    assert report.reason is None


def test_inventory_uses_explicit_checkpoints_without_sqlite_progress_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = (1, 2, 3)
    progress_handler_calls: list[tuple[object, int]] = []
    checkpoint_calls = 0

    class Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [_coverage_row(identity)]

    class Connection:
        in_transaction = False

        def set_progress_handler(self, callback: object, instructions: int) -> None:
            progress_handler_calls.append((callback, instructions))

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                return self
            return Result()

        def close(self) -> None:
            return None

    def checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    candidate = _candidate(identity, marker="no-progress-handler")

    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
        {"fts_pdf": (candidate,)},
        cancellation_check=checkpoint,
    )

    assert progress_handler_calls == []
    assert checkpoint_calls == 2
    assert updated == {"fts_pdf": (candidate,)}
    assert report.complete
    assert report.rows_scanned == 1


def test_inventory_reason_precedence_is_ambiguous_then_planned_then_uncovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambiguous = _candidate((1, 2, 3), marker="ambiguous")
    planned = _candidate((4, 5, 6), marker="planned")
    uncovered = _candidate((7, 8, 9), marker="uncovered")
    ambiguous_row = _relation_row((1, 2, 3))
    ambiguous_row["full_fingerprint"] = "A" * 32
    planned_row = _relation_row((4, 5, 6))
    snapshot = _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0"))
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")

    class Result:
        def __init__(self, rows: list[Mapping[str, object]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[Mapping[str, object]]:
            return self._rows

    class Connection:
        in_transaction = False

        def __init__(self, rows: list[Mapping[str, object]]) -> None:
            self._rows = rows

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                return self
            assert statement.startswith("WITH wanted")
            return Result(self._rows)

        def close(self) -> None:
            return None

    def run(
        candidates: tuple[KnowledgeCandidate, ...],
        rows: list[Mapping[str, object]],
    ) -> tuple[
        tuple[KnowledgeCandidate, ...],
        knowledge_search.RankingExecution,
    ]:
        monkeypatch.setattr(
            knowledge_search,
            "_open_direct_readonly_sqlite",
            lambda _path: Connection(rows),
        )
        updated, report = knowledge_search._apply_inventory_dispositions(
            paths,
            snapshot,
            {"fts_pdf": candidates},
        )
        return updated["fts_pdf"], report

    all_updated, all_report = run(
        (uncovered, planned, ambiguous),
        [ambiguous_row, planned_row],
    )
    assert tuple(candidate.resource.resource_id for candidate in all_updated) == tuple(
        candidate.resource.resource_id for candidate in (uncovered, planned, ambiguous)
    )
    assert all_updated[0].warnings == (
        "existing_warning",
        "inventory_duplicate_plan_coverage_unknown",
    )
    assert all_updated[1].warnings == (
        "existing_warning",
        "inventory_planned_duplicate_unverified",
    )
    assert all_updated[2].warnings == (
        "existing_warning",
        "inventory_duplicate_plan_ambiguous",
    )
    assert all_report.returned == 1
    assert all_report.rows_scanned == 2
    assert all_report.reason == "invalid_or_conflicting_duplicate_plan"

    planned_updated, planned_report = run(
        (uncovered, planned),
        [planned_row],
    )
    assert planned_updated[1].evidence.identifiers[-1] == (
        "planned_duplicate_of",
        "resource:file:9:10:11",
    )
    assert planned_report.returned == 1
    assert planned_report.reason == "inventory_exact_verification_unavailable"

    uncovered_updated, uncovered_report = run((uncovered,), [])
    assert uncovered_updated[0].warnings[-1] == ("inventory_duplicate_plan_coverage_unknown")
    assert uncovered_report.returned == 0
    assert uncovered_report.reason == "inventory_plan_coverage_unknown"


def test_inventory_multielement_batches_keep_identity_then_head_parameter_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_layouts = ((2, 2), (2, 1), (1, 2), (1, 1))
    query_calls: list[
        tuple[
            tuple[tuple[int, int, int], ...],
            tuple[tuple[int, int, int, int, int], ...],
            int,
        ]
    ] = []
    events: list[str] = []
    cancellation_calls = 0

    class Result:
        def fetchall(self) -> list[object]:
            return []

    class Connection:
        in_transaction = False
        query_index = 0

        def execute(
            self,
            statement: str,
            parameters: tuple[bytes | int, ...] = (),
        ) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                events.append("COMMIT")
                return self
            identity_count, head_count = call_layouts[self.query_index]
            self.query_index += 1
            identity_end = identity_count * 3
            head_end = identity_end + head_count * 5
            assert len(parameters) == head_end + 1
            identities = tuple(
                (
                    int.from_bytes(
                        bytes(parameters[offset]),
                        "little",
                    ),
                    int.from_bytes(
                        bytes(parameters[offset + 1]),
                        "little",
                    ),
                    int(parameters[offset + 2]),
                )
                for offset in range(0, identity_end, 3)
            )
            heads = cast(
                tuple[tuple[int, int, int, int, int], ...],
                tuple(
                    tuple(int(value) for value in parameters[offset : offset + 5])
                    for offset in range(identity_end, head_end, 5)
                ),
            )
            assert all(len(head) == 5 for head in heads)
            query_calls.append((identities, heads, int(parameters[-1])))
            events.append("SELECT")
            return Result()

        def close(self) -> None:
            events.append("CLOSE")

    def checkpoint() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1

    connection = Connection()
    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: connection,
    )
    monkeypatch.setattr(knowledge_search, "INVENTORY_IDENTITY_BATCH_SIZE", 2)
    monkeypatch.setattr(knowledge_search, "INVENTORY_HEAD_BATCH_SIZE", 2)
    monkeypatch.setattr(knowledge_search, "MAX_INVENTORY_RELATIONS", 99)
    candidates = (
        _candidate((3, 33, 303), marker="batch-three"),
        _candidate((1, 11, 101), marker="batch-one"),
        _candidate((2, 22, 202), marker="batch-two"),
    )
    snapshot = _snapshot(
        _publication(3, "duplicate-plan-v1:30:3:1:300"),
        _publication(1, "duplicate-plan-v1:10:1:1:100"),
        _publication(2, "duplicate-plan-v1:20:2:1:200"),
    )

    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        snapshot,
        {"fts_pdf": candidates},
        cancellation_check=checkpoint,
    )

    first_identities = ((1, 11, 101), (2, 22, 202))
    second_identities = ((3, 33, 303),)
    first_heads = (
        (1, 10, 1, 1, 100),
        (2, 20, 2, 1, 200),
    )
    second_heads = ((3, 30, 3, 1, 300),)
    assert query_calls == [
        (first_identities, first_heads, 100),
        (first_identities, second_heads, 100),
        (second_identities, first_heads, 100),
        (second_identities, second_heads, 100),
    ]
    assert cancellation_calls == 6
    assert events == [
        "BEGIN",
        "SELECT",
        "SELECT",
        "SELECT",
        "SELECT",
        "COMMIT",
        "CLOSE",
    ]
    assert tuple(candidate.resource.resource_id for candidate in updated["fts_pdf"]) == tuple(
        candidate.resource.resource_id for candidate in candidates
    )
    assert all(
        "inventory_duplicate_plan_coverage_unknown" in candidate.warnings
        for candidate in updated["fts_pdf"]
    )
    assert report.rows_scanned == 0
    assert report.reason == "inventory_plan_coverage_unknown"


def test_inventory_open_provider_is_resolved_again_on_every_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path]] = []

    class Result:
        def fetchall(self) -> list[object]:
            return []

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                return self
            return Result()

        def close(self) -> None:
            return None

    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    snapshot = _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0"))
    rankings = {"fts_pdf": (_candidate((1, 2, 3), marker="provider"),)}

    def open_one(path: Path) -> Connection:
        calls.append(("one", path))
        return Connection()

    def open_two(path: Path) -> Connection:
        calls.append(("two", path))
        return Connection()

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        open_one,
    )
    knowledge_search._apply_inventory_dispositions(paths, snapshot, rankings)
    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        open_two,
    )
    knowledge_search._apply_inventory_dispositions(paths, snapshot, rankings)

    assert calls == [
        ("one", paths.inventory),
        ("two", paths.inventory),
    ]


def test_inventory_relation_malformed_keep_path_match_degrades_to_none() -> None:
    row = _relation_row((1, 2, 3))
    row["keep_path_matches"] = "not-an-integer"

    assert knowledge_search._inventory_relation_row(_as_sqlite_row(row)) is None


@pytest.mark.parametrize("member_order", (-1, 2, 999))
def test_inventory_relation_member_order_must_fit_declared_count(
    member_order: int,
) -> None:
    row = _relation_row((1, 2, 3))
    row["member_order"] = member_order

    assert knowledge_search._inventory_relation_row(_as_sqlite_row(row)) is None


@pytest.mark.parametrize(
    ("keeper_role", "keeper_member_order"),
    (("redundant", 0), ("keep", 1)),
)
def test_inventory_relation_selected_keeper_requires_keep_provenance(
    keeper_role: str,
    keeper_member_order: int,
) -> None:
    row = _relation_row((1, 2, 3))
    row["keeper_role"] = keeper_role
    row["keeper_member_order"] = keeper_member_order

    assert knowledge_search._inventory_relation_row(_as_sqlite_row(row)) is None


@pytest.mark.parametrize(
    ("redundant_role_count", "invalid_role_order_count"),
    ((0, 0), (1, 1)),
)
def test_inventory_relation_all_other_members_require_redundant_roles_and_orders(
    redundant_role_count: int,
    invalid_role_order_count: int,
) -> None:
    row = _relation_row((1, 2, 3))
    row["redundant_role_count"] = redundant_role_count
    row["invalid_role_order_count"] = invalid_role_order_count

    assert knowledge_search._inventory_relation_row(_as_sqlite_row(row)) is None


def test_inventory_relation_orders_must_be_unique_and_contiguous() -> None:
    row = _relation_row((1, 2, 3))
    row["member_count"] = 3
    row["redundant_count"] = 2
    row["redundant_role_count"] = 2
    row["group_reclaimable_bytes"] = 200
    row["distinct_member_order_count"] = 2

    assert knowledge_search._inventory_relation_row(_as_sqlite_row(row)) is None


def test_inventory_query_exposes_keeper_and_group_role_order_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_statements: list[str] = []

    class Result:
        def fetchall(self) -> list[object]:
            return []

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                return self
            select_statements.append(statement)
            return Result()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
        {"fts_pdf": (_candidate((1, 2, 3), marker="provenance"),)},
    )

    assert len(select_statements) == 1
    statement = select_statements[0]
    assert "keeper.member_order AS keeper_member_order" in statement
    assert "keeper.role AS keeper_role" in statement
    assert "AS redundant_role_count" in statement
    assert "AS invalid_role_order_count" in statement
    assert "AS distinct_member_order_count" in statement
    assert "AS member_present" in statement


def test_inventory_sql_distinguishes_absent_from_present_malformed_member(
    tmp_path: Path,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    paths.inventory.parent.mkdir(parents=True)
    with sqlite3.connect(paths.inventory) as connection:
        connection.executescript(
            """
            CREATE TABLE files(
                scan_id INTEGER,path TEXT,volume_id BLOB,file_id BLOB,
                birthtime_ns INTEGER,size INTEGER
            );
            CREATE TABLE duplicate_plan_summaries(
                scan_id INTEGER,completed_ns INTEGER,group_count INTEGER,
                redundant_files INTEGER,reclaimable_bytes INTEGER
            );
            CREATE TABLE planned_duplicate_groups(
                group_id INTEGER,scan_id INTEGER,size INTEGER,keep_path TEXT,
                redundant_count INTEGER,reclaimable_bytes INTEGER,
                full_fingerprint TEXT
            );
            CREATE TABLE planned_duplicate_members(
                group_id,member_order,role,path,volume_id,file_id,size,birthtime_ns
            );
            INSERT INTO duplicate_plan_summaries VALUES(1,2,4,4,400);
            """
        )
        file_rows = (
            (1, "C:/fixture/null-role.pdf", _blob(1), _blob(2), 3, 100),
            (1, "C:/fixture/keeper-1.pdf", _blob(9), _blob(10), 11, 100),
            (1, "C:/fixture/null-keeper-role.pdf", _blob(21), _blob(22), 23, 100),
            (1, "C:/fixture/keeper-2.pdf", _blob(29), _blob(30), 31, 100),
            (1, "C:/fixture/null-keeper-fields.pdf", _blob(41), _blob(42), 43, 100),
            (1, "C:/fixture/malformed-order.pdf", _blob(51), _blob(52), 53, 100),
            (1, "C:/fixture/keeper-4.pdf", _blob(59), _blob(60), 61, 100),
            (1, "C:/fixture/absent-member.pdf", _blob(71), _blob(72), 73, 100),
        )
        connection.executemany("INSERT INTO files VALUES(?,?,?,?,?,?)", file_rows)
        group_rows = (
            (1, 1, 100, "C:/fixture/keeper-1.pdf", 1, 100, "a" * 32),
            (2, 1, 100, "C:/fixture/keeper-2.pdf", 1, 100, "b" * 32),
            (3, 1, 100, "C:/fixture/keeper-3.pdf", 1, 100, "c" * 32),
            (4, 1, 100, "C:/fixture/keeper-4.pdf", 1, 100, "d" * 32),
        )
        connection.executemany(
            "INSERT INTO planned_duplicate_groups VALUES(?,?,?,?,?,?,?)",
            group_rows,
        )
        member_rows = (
            (1, 0, "keep", "C:/fixture/keeper-1.pdf", _blob(9), _blob(10), 100, 11),
            (1, 1, None, "C:/fixture/null-role.pdf", _blob(1), _blob(2), 100, 3),
            (2, 0, None, "C:/fixture/keeper-2.pdf", _blob(29), _blob(30), 100, 31),
            (
                2,
                1,
                "redundant",
                "C:/fixture/null-keeper-role.pdf",
                _blob(21),
                _blob(22),
                100,
                23,
            ),
            (3, 0, "keep", "C:/fixture/keeper-3.pdf", None, None, 100, None),
            (
                3,
                1,
                "redundant",
                "C:/fixture/null-keeper-fields.pdf",
                _blob(41),
                _blob(42),
                100,
                43,
            ),
            (4, 0, "keep", "C:/fixture/keeper-4.pdf", _blob(59), _blob(60), 100, 61),
            (
                4,
                "bad",
                "redundant",
                "C:/fixture/malformed-order.pdf",
                _blob(51),
                _blob(52),
                100,
                53,
            ),
        )
        connection.executemany(
            "INSERT INTO planned_duplicate_members VALUES(?,?,?,?,?,?,?,?)",
            member_rows,
        )

    malformed = (
        _candidate((1, 2, 3), marker="null-role"),
        _candidate((21, 22, 23), marker="null-keeper-role"),
        _candidate((41, 42, 43), marker="null-keeper-fields"),
        _candidate((51, 52, 53), marker="malformed-order"),
    )
    absent = _candidate((71, 72, 73), marker="absent-member")
    updated, report = knowledge_search._apply_inventory_dispositions(
        paths,
        _snapshot(_publication(1, "duplicate-plan-v1:2:4:4:400")),
        {"fts_pdf": (*malformed, absent)},
    )

    for original, candidate in zip(malformed, updated["fts_pdf"][:4], strict=True):
        assert candidate.warnings == (
            "existing_warning",
            "inventory_duplicate_plan_ambiguous",
        )
        assert candidate.evidence == original.evidence
    assert updated["fts_pdf"][4] == absent
    assert report.complete is False
    assert report.returned == 0
    assert report.rows_scanned == 5
    assert report.reason == "invalid_or_conflicting_duplicate_plan"


@pytest.mark.parametrize(
    "case",
    (
        "missing_with_evidence",
        "malformed",
        "zero_with_evidence",
        "one_without_evidence",
    ),
)
def test_inventory_legacy_presence_inference_is_strictly_fail_closed(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = (1, 2, 3)
    row = _coverage_row(identity)
    if case == "missing_with_evidence":
        del row["member_present"]
        row["member_order"] = 1
    elif case == "malformed":
        row["member_present"] = "0"
    elif case == "zero_with_evidence":
        row["member_order"] = 1
    else:
        row["member_present"] = 1

    class Result:
        def fetchall(self) -> list[Mapping[str, object]]:
            return [row]

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            if statement == "BEGIN":
                self.in_transaction = True
                return self
            if statement == "COMMIT":
                self.in_transaction = False
                return self
            return Result()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(_publication(1, "duplicate-plan-v1:2:1:1:100")),
        {"fts_pdf": (_candidate(identity, marker=case),)},
    )

    assert updated["fts_pdf"][0].warnings == (
        "existing_warning",
        "inventory_duplicate_plan_ambiguous",
    )
    assert report.complete is False
    assert report.rows_scanned == 1
    assert report.reason == "invalid_or_conflicting_duplicate_plan"


def test_inventory_non_sqlite_rollback_primary_survives_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_failure = sqlite3.OperationalError("inventory read failed")
    rollback_failure = RuntimeError("inventory non-sqlite rollback failed")
    close_failure = RuntimeError("inventory close failed")
    events: list[str] = []

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> Connection:
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "ROLLBACK":
                events.append("ROLLBACK")
                raise rollback_failure
            events.append("SELECT")
            raise read_failure

        def close(self) -> None:
            events.append("CLOSE")
            raise close_failure

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )

    with pytest.raises(RuntimeError) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
            {"fts_pdf": (_candidate((1, 2, 3), marker="rollback-primary"),)},
        )

    assert raised.value is rollback_failure
    assert events == ["BEGIN", "SELECT", "ROLLBACK", "CLOSE"]
    assert rollback_failure.__notes__ == [
        "inventory read connection close cleanup failed: RuntimeError: inventory close failed"
    ]


def test_inventory_limit_rollback_failure_attempts_one_fallback_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_rollback = sqlite3.OperationalError("limit rollback failed")
    events: list[str] = []
    rollback_calls = 0

    class Result:
        def fetchall(self) -> list[object]:
            return [object(), object()]

    class Connection:
        in_transaction = False

        def execute(self, statement: str, *_args: object) -> object:
            nonlocal rollback_calls
            if statement == "BEGIN":
                self.in_transaction = True
                events.append("BEGIN")
                return self
            if statement == "ROLLBACK":
                rollback_calls += 1
                events.append(f"ROLLBACK:{rollback_calls}")
                if rollback_calls == 1:
                    raise first_rollback
                self.in_transaction = False
                return self
            events.append("SELECT")
            return Result()

        def close(self) -> None:
            events.append("CLOSE")

    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: Connection(),
    )
    monkeypatch.setattr(knowledge_search, "MAX_INVENTORY_RELATIONS", 1)
    candidate = _candidate((1, 2, 3), marker="limit-rollback")
    rankings = {"fts_pdf": (candidate,)}

    updated, report = knowledge_search._apply_inventory_dispositions(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        _snapshot(_publication(1, "duplicate-plan-v1:2:0:0:0")),
        rankings,
    )

    assert updated == rankings
    assert events == [
        "BEGIN",
        "SELECT",
        "ROLLBACK:1",
        "ROLLBACK:2",
        "CLOSE",
    ]
    assert report.rows_scanned == 0
    assert report.reason == "owner_read_failed:OperationalError"


# endregion [02]
