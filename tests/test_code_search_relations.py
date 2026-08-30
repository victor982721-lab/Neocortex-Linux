"""Typed relation provenance exposed by the public code search boundary."""
# region [00] Contexto del módulo
# Módulo: tests/test_code_search_relations.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.code import code_search as code_search_module
from neocortex.code.code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
    CodeSearchHit,
    CodeSearchQuery,
    DependencyRecord,
    ReferenceRecord,
    SourceRange,
    SymbolRecord,
)
from neocortex.code.code_search import search_code
from neocortex.code.code_state import CodeState
from neocortex.knowledge.knowledge_context import build_context_bundle
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
)
from neocortex.knowledge.knowledge_planner import (
    KnowledgeQuery,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_search import (
    KnowledgeSearchResult,
    execute_knowledge_search,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from neocortex.semantic.semantic_models import fingerprint_bytes, fingerprint_text
# endregion [01]

# region [02] Implementación


def _range(line: int) -> SourceRange:
    return SourceRange(line, 0, line, 8, line * 10, line * 10 + 8)


def test_tied_file_and_symbol_hits_have_a_total_deterministic_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(symbol: str | None) -> code_search_module._SearchRow:
        return code_search_module._SearchRow(
            version_id=1,
            path=str(tmp_path / "knowledge.py"),
            project=None,
            language="python",
            artifact_kind="source",
            symbol=symbol,
            signature=None,
            start_line=1,
            end_line=2,
            snippet="def knowledge_search(): ...",
            size=32,
            mtime_ns=1,
            status="complete",
            evidence="fixture",
        )

    rankings = (
        ("literal", (row(None),)),
        ("symbol", (row("knowledge.knowledge_search"),)),
    )
    monkeypatch.setattr(
        code_search_module,
        "_search_rankings",
        lambda *_args, **_kwargs: rankings,
    )
    query = CodeSearchQuery(
        text="function knowledge_search",
        modes=("literal", "symbol"),
        limit=5,
    )

    first = search_code(tmp_path / "isolated-code.sqlite3", query)
    second = search_code(tmp_path / "isolated-code.sqlite3", query)

    assert tuple(hit.symbol for hit in first) == (
        None,
        "knowledge.knowledge_search",
    )
    assert second == first


def _analysis(
    path: Path,
    file_id: int,
    text: str,
    *,
    symbols: tuple[SymbolRecord, ...] = (),
    references: tuple[ReferenceRecord, ...] = (),
    dependencies: tuple[DependencyRecord, ...] = (),
) -> CodeAnalysis:
    raw = text.encode("utf-8")
    text_fingerprint = fingerprint_text(text)
    raw_fingerprint = fingerprint_bytes(raw)
    snapshot = FileSnapshot(str(path), 1, file_id, len(raw), 100 + file_id, 50)
    source = CodeFileInput(
        snapshot=snapshot,
        text=text,
        raw_bytes=raw,
        encoding="utf-8",
        classification=ArtifactClassification(
            "python", ArtifactKind.SOURCE, 1.0, ("relation-fixture",)
        ),
        processing_signature="code-relations-test-v1",
    )
    return CodeAnalysis(
        input=source,
        status=AnalysisStatus.COMPLETE,
        analyzer_id="relation-fixture",
        analyzer_version="1",
        parser_kind="fixture",
        text_xxh3_128=text_fingerprint.xxh3_128,
        text_xxh3_64_guard=text_fingerprint.xxh3_64_guard,
        normalized_xxh3_128=text_fingerprint.xxh3_128,
        token_xxh3_128=None,
        structure_xxh3_128=None,
        raw_xxh3_128=raw_fingerprint.xxh3_128,
        raw_xxh3_64_guard=raw_fingerprint.xxh3_64_guard,
        symbols=symbols,
        references=references,
        dependencies=dependencies,
        provenance={"fixture": "code-relations"},
    )


def _relation_state(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    database = tmp_path / "code.sqlite3"
    target_path = tmp_path / "target.py"
    dependency_path = tmp_path / "depmod.py"
    caller_path = tmp_path / "caller.py"
    target = SymbolRecord("function", "target", "pkg.target", "target()", _range(1))
    dependency_module = SymbolRecord("module", "depmod", "depmod", None, _range(1))
    caller = SymbolRecord("function", "caller", "pkg.caller", "caller()", _range(1))
    relations = (
        ReferenceRecord(
            "call",
            "target",
            _range(2),
            source_qualified_name="pkg.caller",
            target_hint="pkg.target",
            confirmed=True,
            confidence=0.95,
            evidence="fixture-call",
        ),
        ReferenceRecord(
            "call",
            "target_missing",
            _range(2),
            source_qualified_name="pkg.caller",
            target_hint="pkg.target_missing",
            confirmed=False,
            confidence=0.4,
            evidence="fixture-unresolved-call",
        ),
        ReferenceRecord(
            "import",
            "external",
            _range(3),
            source_qualified_name="pkg.caller",
            target_hint="external.lib",
            confirmed=False,
            confidence=0.6,
            evidence="fixture-import",
        ),
    )
    dependencies = (
        DependencyRecord(
            "depmod",
            "python_import",
            scope="runtime",
            version_spec=">=1",
            source_range=_range(4),
            confirmed=True,
            confidence=0.9,
            evidence="fixture-dependency",
        ),
        DependencyRecord(
            "missing_dep",
            "manifest",
            scope="development",
            version_spec="2.*",
            source_range=_range(5),
            confirmed=True,
            confidence=0.8,
            evidence="fixture-missing-dependency",
        ),
    )

    with CodeState(database) as state:
        target_version, _ = state.store_analysis(
            _analysis(target_path, 10, "def target(): pass", symbols=(target,)), 1
        )
        dependency_version, _ = state.store_analysis(
            _analysis(
                dependency_path,
                11,
                "# depmod",
                symbols=(dependency_module,),
            ),
            1,
        )
        caller_version, _ = state.store_analysis(
            _analysis(
                caller_path,
                12,
                "def caller(): pass",
                symbols=(caller,),
                references=relations,
                dependencies=dependencies,
            ),
            1,
        )
        state.finalize_graph(1)
    return database, {
        "target": target_version,
        "dependency": dependency_version,
        "caller": caller_version,
    }


def _knowledge_snapshot() -> KnowledgeSnapshot:
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T05:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot("code", OwnerAvailability.AVAILABLE, 2, 2),
            OwnerSnapshot("pdf", OwnerAvailability.ABSENT, 11),
            OwnerSnapshot("docx", OwnerAvailability.ABSENT, 5),
            OwnerSnapshot("office", OwnerAvailability.ABSENT, 1),
            OwnerSnapshot("audio", OwnerAvailability.ABSENT, 1),
            OwnerSnapshot("semantic", OwnerAvailability.ABSENT, 6),
            OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 6),
            OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 7),
        ),
    )


def _knowledge_relation_search(state: Path) -> KnowledgeSearchResult:
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "call target",
            source_kinds=("code",),
            limit=20,
            max_per_resource=10,
        )
    )
    return execute_knowledge_search(
        KnowledgeStatePaths.from_directory(state),
        plan,
        _knowledge_snapshot(),
    )


def test_search_exposes_resolved_call_endpoints_and_owner_provenance(
    tmp_path: Path,
) -> None:
    database, versions = _relation_state(tmp_path)

    hit = search_code(database, CodeSearchQuery(text="target", modes=("call",), limit=5))[0]
    relation = hit.relations[0]

    assert relation.family == "reference"
    assert relation.kind == "call"
    assert relation.name == "target"
    assert relation.source.version_id == versions["caller"]
    assert relation.source.path.endswith("caller.py")
    assert relation.source.symbol_id is not None
    assert relation.source.symbol == "pkg.caller"
    assert relation.target is not None
    assert relation.target.version_id == versions["target"]
    assert relation.target.path.endswith("target.py")
    assert relation.target.symbol_id is not None
    assert relation.target.symbol == "pkg.target"
    assert relation.target_hint == "pkg.target"
    assert relation.resolved is True
    assert relation.confirmed is True
    assert relation.confidence == 0.95
    assert relation.provenance == "fixture-call"
    assert relation.source_table == "code_references"
    assert relation.source_row_id > 0
    assert [item.name for item in hit.relations] == ["target", "target_missing"]
    assert hit.relations[1].target is None
    assert hit.relations[1].resolved is False
    assert hit.relations[1].target_hint == "pkg.target_missing"


def test_search_marks_unresolved_reference_without_fabricating_target(
    tmp_path: Path,
) -> None:
    database, _ = _relation_state(tmp_path)

    hit = search_code(
        database,
        CodeSearchQuery(text="external", modes=("reference", "import"), limit=5),
    )[0]
    relation = hit.relations[0]

    assert hit.match_types == ("reference", "import")
    assert len(hit.relations) == 1
    assert relation.kind == "import"
    assert relation.name == "external"
    assert relation.target is None
    assert relation.target_hint == "external.lib"
    assert relation.resolved is False
    assert relation.confirmed is False
    assert relation.provenance == "fixture-import"


def test_search_exposes_resolved_and_unresolved_dependency_edges(
    tmp_path: Path,
) -> None:
    database, versions = _relation_state(tmp_path)

    resolved = search_code(
        database, CodeSearchQuery(text="depmod", modes=("dependency",), limit=5)
    )[0].relations[0]
    unresolved = search_code(
        database,
        CodeSearchQuery(text="missing_dep", modes=("dependency",), limit=5),
    )[0].relations[0]

    assert resolved.family == "dependency"
    assert resolved.kind == "python_import"
    assert resolved.source.version_id == versions["caller"]
    assert resolved.source.symbol is None
    assert resolved.target is not None
    assert resolved.target.version_id == versions["dependency"]
    assert resolved.target.path.endswith("depmod.py")
    assert resolved.target.symbol is None
    assert resolved.resolved is True
    assert resolved.target_hint is None
    assert resolved.scope == "runtime"
    assert resolved.version_spec == ">=1"
    assert resolved.provenance == "fixture-dependency"
    assert resolved.source_table == "dependencies"

    assert unresolved.name == "missing_dep"
    assert unresolved.target is None
    assert unresolved.resolved is False
    assert unresolved.target_hint is None
    assert unresolved.scope == "development"
    assert unresolved.version_spec == "2.*"
    assert unresolved.provenance == "fixture-missing-dependency"


def test_code_search_hit_constructor_remains_compatible_without_relations() -> None:
    hit = CodeSearchHit(
        "source.py",
        None,
        "python",
        "source",
        None,
        None,
        1,
        1,
        "source",
        1.0,
        ("literal",),
        ("literal:source",),
        1,
        6,
        1,
        "complete",
    )

    assert hit.relations == ()


def test_knowledge_search_and_context_preserve_real_code_relation_evidence(
    tmp_path: Path,
) -> None:
    _, versions = _relation_state(tmp_path)

    result = _knowledge_relation_search(tmp_path)
    relation_hits = tuple(
        hit for hit in result.hits if hit.evidence.section_kind == "code_relation"
    )

    assert len(relation_hits) == 2
    assert len({hit.evidence.evidence_id for hit in relation_hits}) == 2
    assert all(
        hit.evidence.evidence_id.startswith("evidence:code-relation:") for hit in relation_hits
    )
    identifiers_by_name = {
        dict(hit.evidence.identifiers)["code_relation_name"]: (
            hit,
            dict(hit.evidence.identifiers),
        )
        for hit in relation_hits
    }
    assert set(identifiers_by_name) == {"target", "target_missing"}

    resolved_hit, resolved = identifiers_by_name["target"]
    unresolved_hit, unresolved = identifiers_by_name["target_missing"]
    assert resolved_hit.evidence.section_id is not None
    assert resolved_hit.evidence.section_id.startswith("code_references:")
    assert resolved["code_relation_id"]
    assert resolved["code_relation_family"] == "reference"
    assert resolved["code_relation_kind"] == "call"
    assert resolved["code_relation_source_version_id"] == str(versions["caller"])
    assert resolved["code_relation_source_symbol"] == "pkg.caller"
    assert resolved["code_relation_source_resource"] == "resource:file:1:12:50"
    assert resolved["code_relation_target_version_id"] == str(versions["target"])
    assert resolved["code_relation_target_symbol"] == "pkg.target"
    assert resolved["code_relation_target_resource"] == "resource:file:1:10:50"
    assert resolved["code_relation_resolved"].casefold() == "true"
    assert resolved["code_relation_confirmed"].casefold() == "true"
    assert resolved["code_relation_provenance"] == "fixture-call"
    assert resolved_hit.resource.resource_id == resolved["code_relation_source_resource"]

    assert unresolved_hit.evidence.section_id is not None
    assert unresolved_hit.evidence.section_id.startswith("code_references:")
    assert unresolved["code_relation_source_resource"] == "resource:file:1:12:50"
    assert unresolved["code_relation_target_hint"] == "pkg.target_missing"
    assert unresolved["code_relation_resolved"].casefold() == "false"
    assert "code_relation_target_resource" not in unresolved
    assert any("unresolved" in warning.casefold() for warning in unresolved_hit.warnings)

    bundle = build_context_bundle(
        result,
        character_limit=100_000,
        max_hits=20,
    )
    resolved_relations = tuple(
        relation
        for relation in bundle.relations
        if resolved_hit.evidence.evidence_id in relation.evidence_ids
    )
    assert len(resolved_relations) == 1
    relation = resolved_relations[0]
    assert relation.relation_kind == "code_reference:call"
    assert relation.method is EvidenceMethod.STRUCTURAL
    assert relation.confidence == 0.95
    assert "analyzer:fixture-call" in relation.provenance
    assert any(item.startswith("code:code_references:") for item in relation.provenance)
    entities = {entity.entity_id: entity for entity in bundle.entities}
    assert (
        resolved["code_relation_source_resource"]
        in entities[relation.source_entity_id].resource_ids
    )
    assert (
        resolved["code_relation_target_resource"]
        in entities[relation.target_entity_id].resource_ids
    )
    assert all(
        unresolved_hit.evidence.evidence_id not in relation.evidence_ids
        for relation in bundle.relations
    )
    notices = (*bundle.missing_information, *bundle.warnings)
    assert any("unresolved" in notice.casefold() for notice in notices)


def test_stale_code_relation_target_never_fabricates_a_context_endpoint(
    tmp_path: Path,
) -> None:
    database, versions = _relation_state(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE file_versions SET invalidated_ns=? WHERE version_id=?",
            (999, versions["target"]),
        )

    result = _knowledge_relation_search(tmp_path)
    target_hit = next(
        hit
        for hit in result.hits
        if hit.evidence.section_kind == "code_relation"
        and dict(hit.evidence.identifiers).get("code_relation_name") == "target"
    )
    identifiers = dict(target_hit.evidence.identifiers)

    assert identifiers["code_relation_confirmed"].casefold() == "true"
    assert identifiers["code_relation_resolved"].casefold() == "false"
    assert identifiers["code_relation_target_hint"] == "pkg.target"
    assert "code_relation_target_resource" not in identifiers
    assert "code_relation_target_version_id" not in identifiers
    assert "code_relation_target_symbol" not in identifiers
    assert any("unresolved" in warning.casefold() for warning in target_hit.warnings)

    bundle = build_context_bundle(
        result,
        character_limit=100_000,
        max_hits=20,
    )
    assert bundle.relations == ()
    assert all("resource:file:1:10:50" not in entity.resource_ids for entity in bundle.entities)
    notices = (*bundle.missing_information, *bundle.warnings)
    assert any("unresolved" in notice.casefold() for notice in notices)


# endregion [02]
