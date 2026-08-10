"""Compatibility seams for the incremental Knowledge Search extraction."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_search_modularization_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

import _04_Nucleo_Operativo as operational
import neocortex.sdk as sdk
from _04_Nucleo_Operativo import knowledge_search
from _04_Nucleo_Operativo.knowledge_contracts import (
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
from _04_Nucleo_Operativo.knowledge_planner import (
    KnowledgePlan,
    KnowledgeQuery,
    plan_knowledge_query,
)
from _04_Nucleo_Operativo.knowledge_search import (
    KnowledgeCandidate,
    RankingExecution,
    execute_knowledge_search,
)
from _04_Nucleo_Operativo.knowledge_snapshot import KnowledgeStatePaths
# endregion [01]

# region [02] Implementación


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MODULE = "_04_Nucleo_Operativo.knowledge_search"
PUBLIC_EXPORTS = (
    "KnowledgeCandidate",
    "KnowledgeSearchResult",
    "RankingExecution",
    "execute_knowledge_search",
    "fuse_evidence_rankings",
)
PUBLIC_SIGNATURES = {
    "KnowledgeCandidate": (
        "(resource: 'ResourceRef', revision: 'RevisionRef', "
        "evidence: 'EvidenceRef', signal: 'RankingSignal', reason: 'str', "
        "confidence: 'float | None' = None, warnings: 'tuple[str, ...]' = ()) "
        "-> None"
    ),
    "KnowledgeSearchResult": (
        "(plan: 'KnowledgePlan', snapshot: 'KnowledgeSnapshot', "
        "hits: 'tuple[KnowledgeHit, ...]', rankings: 'tuple[RankingExecution, ...]', "
        "complete: 'bool', truncated: 'bool', omitted_candidates: 'int', "
        "rows_scanned: 'int', vectors_scanned: 'int', "
        "elapsed_milliseconds: 'int', warnings: 'tuple[str, ...]' = (), "
        "telemetry: 'KnowledgeQueryTelemetry | None' = None, "
        "blocking_owners: 'tuple[str, ...]' = (), "
        "result_window_full: 'bool' = False, "
        "window_omitted_candidates: 'int' = 0) -> None"
    ),
    "RankingExecution": (
        "(name: 'str', channel: 'str', executed: 'bool', available: 'bool', "
        "complete: 'bool', returned: 'int', rows_scanned: 'int' = 0, "
        "vectors_scanned: 'int' = 0, reason: 'str | None' = None, "
        "owner: 'str | None' = None, elapsed_ns: 'int | None' = None, "
        "result_window_full: 'bool' = False, next_cursor: 'int | None' = None, "
        "cutoff_score: 'float | None' = None) -> None"
    ),
    "execute_knowledge_search": (
        "(paths: 'KnowledgeStatePaths', plan: 'KnowledgePlan', "
        "snapshot: 'KnowledgeSnapshot', *, "
        "cancellation_check: 'Callable[[], None] | None' = None, "
        "clock_ns: 'Callable[[], int] | None' = None, "
        "telemetry_clock: 'KnowledgeTelemetryClock | None' = None) "
        "-> 'KnowledgeSearchResult'"
    ),
    "fuse_evidence_rankings": (
        "(rankings: 'Mapping[str, Sequence[KnowledgeCandidate]]', *, "
        "discovery_signals: 'Sequence[ResourceDiscoverySignal]' = (), "
        "limit: 'int', max_per_resource: 'int', min_section_distance: 'int', "
        "include_history: 'bool' = False, "
        "cancellation_check: 'Callable[[], None] | None' = None) "
        "-> 'tuple[tuple[KnowledgeHit, ...], int]'"
    ),
}
PRIVATE_MONKEYPATCH_SEAMS = (
    "_lexical_rankings",
    "_semantic_rankings",
    "_exact_rankings",
    "_code_ranking",
    "_catalog_ranking",
    "_apply_plan_filters",
    "_apply_inventory_dispositions",
    "fuse_evidence_rankings",
)


def _available_snapshot() -> KnowledgeSnapshot:
    versions = {
        "pdf": 11,
        "docx": 5,
        "office": 1,
        "audio": 1,
        "semantic": 6,
        "code": 2,
        "catalog": 6,
        "inventory": 7,
    }
    return KnowledgeSnapshot.create(
        source_version="0.7.1",
        captured_at_utc="2026-07-30T12:00:00Z",
        captured_monotonic_ns=1,
        owners=tuple(
            OwnerSnapshot(owner, OwnerAvailability.AVAILABLE, version, version)
            for owner, version in versions.items()
        ),
    )


def _complete_report(
    name: str,
    channel: str,
    *,
    owner: str | None = None,
) -> RankingExecution:
    return RankingExecution(
        name,
        channel,
        True,
        True,
        True,
        0,
        owner=owner,
    )


def _physical_candidate() -> KnowledgeCandidate:
    physical_identity = "1:2:3"
    resource = ResourceRef(
        f"resource:file:{physical_identity}",
        "pdf",
        "pdf",
        PhysicalIdentityRef("windows_file_id_birthtime", physical_identity, 1),
        "C:/fixture/report.pdf",
    )
    revision = RevisionRef(
        resource.resource_id,
        "revision:fixture",
        "fixture-owner",
        "fixture-v1",
        1,
        RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        "evidence:fixture",
        resource.resource_id,
        revision.revision_id,
        EvidenceMethod.EXTRACTED,
        section_kind="pdf_page",
        section_id="1",
    )
    return KnowledgeCandidate(
        resource,
        revision,
        evidence,
        RankingSignal("fts_pdf", "fixture", 1.0, 1),
        "modularization fixture",
    )


def test_public_facade_surface_signatures_and_identity_are_stable() -> None:
    assert knowledge_search.__all__ == PUBLIC_EXPORTS
    for name, expected_signature in PUBLIC_SIGNATURES.items():
        value = getattr(knowledge_search, name)
        assert value.__module__ == PUBLIC_MODULE
        assert str(inspect.signature(value)) == expected_signature

    assert operational.KnowledgeSearchResult is knowledge_search.KnowledgeSearchResult
    assert sdk.KnowledgeSearchResult is knowledge_search.KnowledgeSearchResult
    assert KnowledgeCandidate.__match_args__ == KnowledgeCandidate.__slots__
    assert RankingExecution.__match_args__ == RankingExecution.__slots__
    assert all(
        callable(getattr(knowledge_search, name)) for name in PRIVATE_MONKEYPATCH_SEAMS
    )


@pytest.mark.parametrize(
    "module_order",
    (
        (PUBLIC_MODULE, "_04_Nucleo_Operativo", "neocortex.sdk"),
        ("_04_Nucleo_Operativo", "neocortex.sdk", PUBLIC_MODULE),
    ),
)
def test_knowledge_search_cold_import_orders_preserve_facade_identity(
    module_order: tuple[str, ...],
) -> None:
    script = textwrap.dedent(
        f"""
        import importlib

        for module_name in {module_order!r}:
            importlib.import_module(module_name)
        package = importlib.import_module("_04_Nucleo_Operativo")
        sdk = importlib.import_module("neocortex.sdk")
        search = importlib.import_module("{PUBLIC_MODULE}")
        assert search.__all__ == {PUBLIC_EXPORTS!r}
        assert package.KnowledgeSearchResult is search.KnowledgeSearchResult
        assert sdk.KnowledgeSearchResult is search.KnowledgeSearchResult
        assert search.KnowledgeSearchResult.__module__ == "{PUBLIC_MODULE}"
        """
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_full_plan_topology_dispatches_through_facade_seams_in_stable_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "which module uses controller.breaker before version",
            project="alpha",
            include_history=True,
            limit=3,
            max_vectors=9,
        )
    )
    expected_topology = (
        ("exact", "exact_identifiers", 9, True),
        ("lexical", "owner_fts", 9, True),
        ("semantic", "semantic_text", 9, True),
        ("semantic", "semantic_image", 9, True),
        ("structural_code", "code_structural", 9, True),
        ("catalog", "catalog_metadata", 9, True),
        ("relational", "verified_relations", 9, True),
        ("temporal", "published_history", 9, True),
    )
    assert (
        tuple(
            (step.channel, step.ranking_name, step.candidate_limit, step.required)
            for step in plan.steps
        )
        == expected_topology
    )

    base_paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    paths = replace(
        base_paths,
        semantic=tmp_path / "custom" / "vectors.sqlite3",
    )
    snapshot = _available_snapshot()
    calls: list[str] = []

    def lexical(
        received_paths: KnowledgeStatePaths,
        received_plan: KnowledgePlan,
        received_snapshot: KnowledgeSnapshot,
        **_kwargs: object,
    ) -> tuple[dict[str, tuple[KnowledgeCandidate, ...]], list[RankingExecution]]:
        assert (received_paths, received_plan, received_snapshot) == (
            paths,
            plan,
            snapshot,
        )
        calls.append("lexical")
        return {}, [
            _complete_report(f"fts_{owner}", "lexical", owner=owner)
            for owner in ("pdf", "docx", "office", "audio")
        ]

    def semantic(
        received_paths: KnowledgeStatePaths,
        received_plan: KnowledgePlan,
        received_snapshot: KnowledgeSnapshot,
        *_args: object,
        **_kwargs: object,
    ) -> tuple[dict[str, tuple[KnowledgeCandidate, ...]], list[RankingExecution]]:
        assert received_paths.semantic == paths.semantic
        assert (received_plan, received_snapshot) == (plan, snapshot)
        calls.append("semantic")
        return {}, [
            _complete_report(step.ranking_name, "semantic", owner="semantic")
            for step in plan.steps
            if step.channel == "semantic"
        ]

    def exact(
        received_paths: KnowledgeStatePaths,
        received_plan: KnowledgePlan,
        received_snapshot: KnowledgeSnapshot,
        **_kwargs: object,
    ) -> tuple[
        dict[str, tuple[KnowledgeCandidate, ...]],
        list[RankingExecution],
        int,
        bool,
        tuple[()],
    ]:
        assert (received_paths, received_plan, received_snapshot) == (
            paths,
            plan,
            snapshot,
        )
        calls.append("exact")
        return {}, [_complete_report("exact_coverage", "exact")], 0, False, ()

    def code(
        received_paths: KnowledgeStatePaths,
        received_plan: KnowledgePlan,
        received_snapshot: KnowledgeSnapshot,
        **_kwargs: object,
    ) -> tuple[tuple[KnowledgeCandidate, ...], RankingExecution]:
        assert (received_paths, received_plan, received_snapshot) == (
            paths,
            plan,
            snapshot,
        )
        calls.append("code")
        return (), _complete_report("code_structural", "structural_code")

    def catalog(
        received_paths: KnowledgeStatePaths,
        received_plan: KnowledgePlan,
        received_snapshot: KnowledgeSnapshot,
        **_kwargs: object,
    ) -> tuple[tuple[KnowledgeCandidate, ...], RankingExecution]:
        assert received_paths.catalog == paths.catalog
        assert (received_plan, received_snapshot) == (plan, snapshot)
        calls.append("catalog")
        return (), _complete_report("catalog_metadata", "catalog")

    def apply_filters(
        rankings: dict[str, tuple[KnowledgeCandidate, ...]],
        received_plan: KnowledgePlan,
    ) -> dict[str, tuple[KnowledgeCandidate, ...]]:
        assert received_plan is plan
        calls.append("filters")
        return dict(rankings)

    def inventory(
        received_paths: KnowledgeStatePaths,
        received_snapshot: KnowledgeSnapshot,
        rankings: dict[str, tuple[KnowledgeCandidate, ...]],
        **_kwargs: object,
    ) -> tuple[dict[str, tuple[KnowledgeCandidate, ...]], RankingExecution]:
        assert (received_paths, received_snapshot) == (paths, snapshot)
        calls.append("inventory")
        return dict(rankings), _complete_report(
            "inventory_duplicate_plan",
            "relationship",
            owner="inventory",
        )

    def fuse(
        rankings: dict[str, tuple[KnowledgeCandidate, ...]],
        **kwargs: object,
    ) -> tuple[tuple[()], int]:
        assert rankings == {}
        assert kwargs == {
            "limit": plan.limit,
            "max_per_resource": plan.max_per_resource,
            "min_section_distance": plan.min_section_distance,
            "include_history": plan.include_history,
            "cancellation_check": None,
        }
        calls.append("fusion")
        return (), 0

    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical)
    monkeypatch.setattr(knowledge_search, "_semantic_rankings", semantic)
    monkeypatch.setattr(knowledge_search, "_exact_rankings", exact)
    monkeypatch.setattr(knowledge_search, "_code_ranking", code)
    monkeypatch.setattr(knowledge_search, "_catalog_ranking", catalog)
    monkeypatch.setattr(knowledge_search, "_apply_plan_filters", apply_filters)
    monkeypatch.setattr(knowledge_search, "_apply_inventory_dispositions", inventory)
    monkeypatch.setattr(knowledge_search, "fuse_evidence_rankings", fuse)
    ticks = iter(range(1, 100))

    result = execute_knowledge_search(
        paths,
        plan,
        snapshot,
        clock_ns=lambda: next(ticks),
    )

    assert calls == [
        "lexical",
        "semantic",
        "exact",
        "code",
        "catalog",
        "filters",
        "inventory",
        "fusion",
    ]
    assert tuple(report.name for report in result.rankings) == (
        "fts_pdf",
        "fts_docx",
        "fts_office",
        "fts_audio",
        "semantic_text",
        "semantic_image",
        "exact_coverage",
        "code_structural",
        "catalog_metadata",
        "verified_relations",
        "published_history",
        "inventory_duplicate_plan",
    )
    assert result.plan is plan
    assert result.snapshot is snapshot
    assert not result.complete
    assert not result.truncated
    assert result.warnings == (
        "ranking_unavailable:published_history",
        "ranking_unavailable:verified_relations",
    )
    assert tuple(result.to_dict()) == (
        "schema_version",
        "kind",
        "query",
        "plan",
        "snapshot",
        "hits",
        "rankings",
        "complete",
        "truncated",
        "omitted_candidates",
        "rows_scanned",
        "row_count_semantics",
        "vectors_scanned",
        "elapsed_milliseconds",
        "warnings",
        "telemetry",
    )


def _install_empty_successful_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    def semantic(
        _paths: KnowledgeStatePaths,
        plan: KnowledgePlan,
        _snapshot: KnowledgeSnapshot,
        *_args: object,
        **_kwargs: object,
    ) -> tuple[dict[str, tuple[KnowledgeCandidate, ...]], list[RankingExecution]]:
        return {}, [
            _complete_report(step.ranking_name, "semantic", owner="semantic")
            for step in plan.steps
            if step.channel == "semantic"
        ]

    def inventory(
        _paths: KnowledgeStatePaths,
        _snapshot: KnowledgeSnapshot,
        rankings: dict[str, tuple[KnowledgeCandidate, ...]],
        **_kwargs: object,
    ) -> tuple[dict[str, tuple[KnowledgeCandidate, ...]], RankingExecution]:
        return dict(rankings), _complete_report(
            "inventory_duplicate_plan",
            "relationship",
            owner="inventory",
        )

    monkeypatch.setattr(knowledge_search, "_semantic_rankings", semantic)
    monkeypatch.setattr(knowledge_search, "_apply_inventory_dispositions", inventory)
    monkeypatch.setattr(
        knowledge_search,
        "fuse_evidence_rankings",
        lambda _rankings, **_kwargs: ((), 0),
    )


def test_plain_lexical_value_error_remains_an_observable_fts_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported(*_args: object, **_kwargs: object) -> None:
        raise ValueError("ordinary unsupported FTS query")

    monkeypatch.setattr(knowledge_search, "_lexical_rankings", unsupported)
    _install_empty_successful_pipeline(monkeypatch)
    result = execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan_knowledge_query(KnowledgeQuery("breaker protection")),
        _available_snapshot(),
    )

    lexical_reports = tuple(
        report for report in result.rankings if report.channel == "lexical"
    )
    assert tuple(report.name for report in lexical_reports) == (
        "fts_pdf",
        "fts_docx",
        "fts_office",
        "fts_audio",
    )
    assert all(
        report.reason == "query_unsupported_by_fts:ValueError"
        for report in lexical_reports
    )
    assert not result.complete


def test_lexical_callback_value_error_is_repropagated_by_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ValueError("lexical cancellation fixture")
    inside_lexical = False

    def cancel() -> None:
        if inside_lexical:
            raise expected

    def lexical(
        _paths: KnowledgeStatePaths,
        _plan: KnowledgePlan,
        _snapshot: KnowledgeSnapshot,
        *,
        cancellation_check: object = None,
        **_kwargs: object,
    ) -> None:
        nonlocal inside_lexical
        assert callable(cancellation_check)
        inside_lexical = True
        try:
            cancellation_check()
        finally:
            inside_lexical = False

    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical)
    _install_empty_successful_pipeline(monkeypatch)

    with pytest.raises(ValueError) as raised:
        execute_knowledge_search(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            plan_knowledge_query(KnowledgeQuery("breaker protection")),
            _available_snapshot(),
            cancellation_check=cancel,
        )

    assert raised.value is expected


def test_direct_sqlite_setup_preserves_primary_error_when_close_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ValueError("read-only setup failed")
    secondary = RuntimeError("connection close failed")
    events: list[str] = []

    class Connection:
        row_factory: object = None

        def execute(self, _statement: str) -> None:
            events.append("execute")
            raise primary

        def close(self) -> None:
            events.append("close")
            raise secondary

    monkeypatch.setattr(
        knowledge_search.sqlite3, "connect", lambda *_a, **_k: Connection()
    )

    with pytest.raises(BaseException) as raised:
        knowledge_search._open_direct_readonly_sqlite(tmp_path / "owner.sqlite3")

    assert events == ["execute", "close"]
    assert raised.value is primary


def test_inventory_cleanup_preserves_cancellation_when_close_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("inventory cancellation fixture")
    secondary = RuntimeError("inventory close failed")
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
            raise secondary

    connection = Connection()
    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: connection,
    )
    snapshot = KnowledgeSnapshot.create(
        source_version="0.7.1",
        captured_at_utc="2026-07-30T12:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot(
                "inventory",
                OwnerAvailability.AVAILABLE,
                7,
                7,
                publications=(
                    PublicationHead(
                        "C:/fixture",
                        "inventory-scan:1",
                        1,
                        "duplicate-plan-v1:2:0:0:0",
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(BaseException) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            snapshot,
            {"fts_pdf": (_physical_candidate(),)},
            cancellation_check=lambda: (_ for _ in ()).throw(primary),
        )

    assert events == ["BEGIN", "ROLLBACK", "CLOSE"]
    assert raised.value is primary


def test_inventory_cleanup_preserves_primary_through_rollback_and_close_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("inventory cancellation fixture")
    rollback_failure = RuntimeError("inventory rollback failed")
    close_failure = RuntimeError("inventory close failed")
    events: list[str] = []

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

    connection = Connection()
    monkeypatch.setattr(
        knowledge_search,
        "_open_direct_readonly_sqlite",
        lambda _path: connection,
    )
    snapshot = KnowledgeSnapshot.create(
        source_version="0.7.1",
        captured_at_utc="2026-07-30T12:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot(
                "inventory",
                OwnerAvailability.AVAILABLE,
                7,
                7,
                publications=(
                    PublicationHead(
                        "C:/fixture",
                        "inventory-scan:1",
                        1,
                        "duplicate-plan-v1:2:0:0:0",
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(BaseException) as raised:
        knowledge_search._apply_inventory_dispositions(
            KnowledgeStatePaths.from_directory(tmp_path / "state"),
            snapshot,
            {"fts_pdf": (_physical_candidate(),)},
            cancellation_check=lambda: (_ for _ in ()).throw(primary),
        )

    assert events == ["BEGIN", "ROLLBACK", "CLOSE"]
    assert raised.value is primary
    assert primary.__notes__ == [
        "inventory read rollback cleanup failed: RuntimeError: inventory rollback failed",
        "inventory read connection close cleanup failed: RuntimeError: inventory close failed",
    ]


def test_code_metadata_cleanup_preserves_primary_error_when_close_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("code metadata read failed")
    secondary = RuntimeError("code connection close failed")
    events: list[str] = []

    class Connection:
        def execute(self, _statement: str, _parameters: object) -> None:
            events.append("execute")
            raise primary

        def close(self) -> None:
            events.append("close")
            raise secondary

    monkeypatch.setattr(
        knowledge_search,
        "connect_code_state",
        lambda *_args, **_kwargs: Connection(),
    )

    with pytest.raises(BaseException) as raised:
        knowledge_search._code_version_metadata(
            tmp_path / "code.sqlite3",
            (1,),
        )

    assert events == ["execute", "close"]
    assert raised.value is primary


# endregion [02]
