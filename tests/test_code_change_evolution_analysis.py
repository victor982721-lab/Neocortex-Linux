from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from _02_Deduplicacion import FileSnapshot
from _04_Nucleo_Operativo.code_change_evolution_analysis import (
    CODE_CHANGE_EVOLUTION_SCHEMA,
    analyze_code_change_evolution,
    expected_code_change_evolution_questions,
    parse_code_change_evolution_payload,
)
from _04_Nucleo_Operativo.code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
    ReferenceRecord,
    SourceRange,
    SymbolRecord,
)
from _04_Nucleo_Operativo.code_external_evidence import (
    ExternalEvidencePublication,
)
from _04_Nucleo_Operativo.code_schema import (
    checkpoint_code_wal,
    remove_checkpointed_code_sidecars,
)
from _04_Nucleo_Operativo.code_state import CodeState
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderMetric,
    ExternalProviderPublication,
    ExternalProviderRelation,
    ExternalRunInput,
    ProviderDescriptor,
    ProviderLimits,
    external_metric_identity,
    external_provider_result_digest,
    external_relation_identity,
    external_root_identity,
)
from _04_Nucleo_Operativo.external_git_history import (
    GIT_HISTORY_PROVIDER_ID,
    GIT_HISTORY_PROVIDER_SCHEMA,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes, fingerprint_text


PROCESSING_SIGNATURE = "code-change-evolution-fixture-v1"


def _function(qualified_name: str, *, line: int = 1) -> SymbolRecord:
    name = qualified_name.rsplit(".", 1)[-1]
    return SymbolRecord(
        "function",
        name,
        qualified_name,
        f"{name}()",
        SourceRange(line, 0, line + 1, 0, line * 20, line * 20 + 12),
        visibility="public",
        complexity=1,
    )


def _analysis(
    path: Path,
    identity: int,
    text: str,
    symbols: tuple[SymbolRecord, ...],
    *,
    references: tuple[ReferenceRecord, ...] = (),
    processing_signature: str = PROCESSING_SIGNATURE,
    mtime_ns: int = 1,
) -> CodeAnalysis:
    raw = text.encode("utf-8")
    raw_fingerprint = fingerprint_bytes(raw)
    text_fingerprint = fingerprint_text(text)
    return CodeAnalysis(
        input=CodeFileInput(
            FileSnapshot(str(path), 1, identity, len(raw), mtime_ns, -1),
            text,
            raw,
            "utf-8",
            ArtifactClassification(
                "python",
                ArtifactKind.SOURCE,
                1.0,
                ("change-evolution-fixture",),
            ),
            processing_signature,
        ),
        status=AnalysisStatus.COMPLETE,
        analyzer_id="fixture-python-ast",
        analyzer_version="1",
        parser_kind="python-ast",
        text_xxh3_128=text_fingerprint.xxh3_128,
        text_xxh3_64_guard=text_fingerprint.xxh3_64_guard,
        normalized_xxh3_128=text_fingerprint.xxh3_128,
        token_xxh3_128=text_fingerprint.xxh3_128,
        structure_xxh3_128=fingerprint_text(
            "\n".join(f"{item.kind}:{item.qualified_name}:{item.signature}" for item in symbols)
        ).xxh3_128,
        raw_xxh3_128=raw_fingerprint.xxh3_128,
        raw_xxh3_64_guard=raw_fingerprint.xxh3_64_guard,
        symbols=symbols,
        references=references,
        provenance={"fixture": True, "syntax_confirmed": True},
    )


def _descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        GIT_HISTORY_PROVIDER_ID,
        GIT_HISTORY_PROVIDER_SCHEMA,
        "git",
        "protected",
        "untrusted-safe",
        "project",
        f"external:{GIT_HISTORY_PROVIDER_ID}",
        "git-history-fixture-configuration",
        None,
        "git-history-fixture-environment",
        "git-history-fixture-comparability",
        "bounded-local-git-fixture",
        "project_wide",
        "exact-input",
        ProviderLimits(1.0, 1_000_000, 1_000_000, 1_000_000, 100),
    )


def _history_metric(
    *,
    subject_key: str,
    version_id: int,
    name: str,
    value: float,
    unit: str,
    metadata: dict[str, object],
) -> ExternalProviderMetric:
    return ExternalProviderMetric(
        external_metric_identity(
            GIT_HISTORY_PROVIDER_ID,
            subject_kind="file",
            subject_key=subject_key,
            category="history",
            metric_name=name,
            unit=unit,
        ),
        "file",
        subject_key,
        "history",
        name,
        value,
        unit,
        version_id=version_id,
        metadata=metadata,
    )


def _history_publication(state: CodeState, root: Path) -> ExternalProviderPublication:
    files = state.external_evidence_files(root)
    input_signature = "git-history-input-fixture-v1"
    metadata: dict[str, object] = {
        "provider_schema": GIT_HISTORY_PROVIDER_SCHEMA,
        "history_input_signature": input_signature,
        "requested_ref": "HEAD",
        "head_commit": "a" * 40,
        "window_commits": 3,
        "window_start_timestamp": 1,
        "window_end_timestamp": 3,
        "history_truncated": False,
        "repository_shallow": False,
        "interpretation": "observed_history_not_defect_probability",
    }
    metrics: list[ExternalProviderMetric] = []
    by_path = {item.relative_path: item for item in files}
    for item in files:
        definitions = (
            ("history_observed", 1.0, "flag"),
            ("observed_commit_count", 2.0, "count"),
            ("observed_touch_count", 2.0, "count"),
            ("observed_additions", 7.0, "lines"),
            ("observed_deletions", 2.0, "lines"),
            ("observed_churn_lines", 9.0, "lines"),
            ("binary_or_unmeasured_touch_count", 0.0, "count"),
            ("observed_change_frequency_per_100_commits", 66.666, "changes_per_100_commits"),
            ("observed_age_seconds", 200.0, "seconds"),
            ("observed_recency_seconds", 10.0, "seconds"),
        )
        metrics.extend(
            _history_metric(
                subject_key=item.relative_path,
                version_id=item.version_id,
                name=name,
                value=value,
                unit=unit,
                metadata=metadata,
            )
            for name, value, unit in definitions
        )
    service = by_path["pkg/service.py"]
    moved = by_path["moved/stable.py"]
    relation = ExternalProviderRelation(
        external_relation_identity(
            GIT_HISTORY_PROVIDER_ID,
            relation_kind="file_cochange",
            source_kind="file",
            source_key="moved/stable.py",
            target_kind="file",
            target_key="pkg/service.py",
            directed=False,
        ),
        "file_cochange",
        "file",
        "moved/stable.py",
        "file",
        "pkg/service.py",
        directed=False,
        confidence=None,
        source_version_id=moved.version_id,
        target_version_id=service.version_id,
        metadata={
            **metadata,
            "observed_commits_together": 2,
            "observed_frequency_per_100_commits": 66.666,
            "relations_truncated": False,
            "interpretation": "cochange_observation_not_defect_probability",
        },
    )
    metric_tuple = tuple(metrics)
    relation_tuple = (relation,)
    return ExternalProviderPublication(
        _descriptor(),
        ExternalEvidencePublication(
            "git",
            "2.0",
            "git-history-fixture-configuration",
            "completed",
            1,
            2,
            {"execution": "full", "uses_network": False, "executes_content": False},
        ),
        str(root),
        external_root_identity(root),
        input_signature,
        tuple(ExternalRunInput.from_file(item, covered=True) for item in files),
        (),
        {
            "eligible_files": len(files),
            "covered_files": len(files),
            "findings": 0,
            "metrics": len(metric_tuple),
            "relations": len(relation_tuple),
            "comparable": 0,
        },
        True,
        external_provider_result_digest((), metric_tuple, relation_tuple),
        "git-history-publication-fixture-v1",
        metrics=metric_tuple,
        relations=relation_tuple,
    )


def _finish_run(
    state: CodeState,
    run_id: int,
    candidates: int,
    *,
    external: ExternalProviderPublication | None = None,
) -> None:
    state.finalize_graph(run_id)
    state.complete_run(
        run_id,
        {
            "candidates": candidates,
            "processed": candidates,
            "cache_hits": 0,
            "errors": 0,
        },
        partial=False,
        graph_current=True,
        external_evidence=None if external is None else (external,),
    )


def _build_transition(
    tmp_path: Path,
    *,
    history: bool,
    current_signature: str = PROCESSING_SIGNATURE,
) -> Path:
    root = tmp_path / "repository"
    state_directory = tmp_path / "state"
    root.mkdir(parents=True)
    state_directory.mkdir()
    database = state_directory / "code.sqlite3"
    baseline: tuple[tuple[Path, int, str, tuple[SymbolRecord, ...]], ...] = (
        (root / "pkg/service.py", 10, "def old():\n    return 1\n", (_function("pkg.old"),)),
        (root / "pkg/stable.py", 11, "def stable():\n    return 1\n", (_function("pkg.stable"),)),
        (root / "pkg/wrapper.py", 12, "def core():\n    return 1\n", (_function("pkg.core"),)),
        (
            root / "pkg/target_a.py",
            13,
            "def target_a():\n    return 1\n",
            (_function("pkg.target_a"),),
        ),
        (
            root / "pkg/target_b.py",
            14,
            "def target_b():\n    return 1\n",
            (_function("pkg.target_b"),),
        ),
        (
            root / "pkg/caller.py",
            15,
            "def caller():\n    return target_a()\n",
            (_function("pkg.caller"),),
        ),
    )
    current: tuple[tuple[Path, int, str, tuple[SymbolRecord, ...]], ...] = (
        (root / "pkg/service.py", 10, "def new():\n    return 2\n", (_function("pkg.new"),)),
        (root / "moved/stable.py", 11, "def stable():\n    return 1\n", (_function("pkg.stable"),)),
        (
            root / "pkg/wrapper.py",
            12,
            "def core():\n    return 1\n\ndef wrapper():\n    return core()\n",
            (_function("pkg.core"), _function("pkg.wrapper", line=4)),
        ),
        (
            root / "pkg/target_a.py",
            13,
            "def target_a():\n    return 1\n",
            (_function("pkg.target_a"),),
        ),
        (
            root / "pkg/target_b.py",
            14,
            "def target_b():\n    return 1\n",
            (_function("pkg.target_b"),),
        ),
        (
            root / "pkg/caller.py",
            15,
            "def caller():\n    return target_a()\n",
            (_function("pkg.caller"),),
        ),
    )
    reference = ReferenceRecord(
        "call",
        "target_a",
        SourceRange(2, 11, 2, 19, 18, 26),
        source_qualified_name="pkg.caller",
        target_hint="pkg.target_a",
        confirmed=True,
        confidence=1.0,
        evidence="fixture-call",
    )
    with CodeState(database) as state:
        first = state.begin_run(1, 1, PROCESSING_SIGNATURE)
        for path, identity, text, symbols in baseline:
            path.parent.mkdir(parents=True, exist_ok=True)
            state.store_analysis(
                _analysis(
                    path,
                    identity,
                    text,
                    symbols,
                    references=(reference,) if path.name == "caller.py" else (),
                    mtime_ns=1,
                ),
                first,
            )
        _finish_run(state, first, len(baseline))

        second = state.begin_run(2, 2, current_signature)
        for path, identity, text, symbols in current:
            path.parent.mkdir(parents=True, exist_ok=True)
            state.store_analysis(
                _analysis(
                    path,
                    identity,
                    text,
                    symbols,
                    references=(reference,) if path.name == "caller.py" else (),
                    processing_signature=current_signature,
                    mtime_ns=2,
                ),
                second,
            )
        publication = _history_publication(state, root) if history else None
        _finish_run(state, second, len(current), external=publication)
        checkpoint_code_wal(state.connection)
    remove_checkpointed_code_sidecars(database)
    return database


def _build_single_publication(tmp_path: Path) -> Path:
    database = tmp_path / "state" / "code.sqlite3"
    database.parent.mkdir(parents=True)
    with CodeState(database) as state:
        run = state.begin_run(1, 1, PROCESSING_SIGNATURE)
        state.store_analysis(
            _analysis(
                tmp_path / "repository" / "only.py",
                90,
                "def only():\n    return 1\n",
                (_function("pkg.only"),),
            ),
            run,
        )
        _finish_run(state, run, 1)
        checkpoint_code_wal(state.connection)
    remove_checkpointed_code_sidecars(database)
    return database


def test_change_history_schema_vertical_preserves_epistemic_boundaries(tmp_path: Path) -> None:
    database = _build_transition(tmp_path, history=True)

    result = analyze_code_change_evolution(database, limit=20)
    specs, evaluations = expected_code_change_evolution_questions(result)

    assert result.status == "ready"
    assert result.snapshot_freshness == "publication_only"
    assert result.aggregate_score is None
    assert result.defect_probability is None
    assert result.decision is None
    assert result.mutation_authority is False
    surface = result.change_surface
    assert surface.status == "ready"
    assert surface.baseline_analysis_run_id == 1
    assert surface.current_analysis_run_id == 2
    assert surface.baseline_publication_id is not None
    assert surface.current_publication_id is not None
    assert surface.baseline_publication_id.startswith("code-publication-v1:xxh3_128:")
    assert surface.current_publication_id.startswith("code-publication-v1:xxh3_128:")
    assert surface.baseline_publication_id != surface.current_publication_id
    assert surface.files_added == surface.files_removed == 0
    assert surface.files_modified == 2
    assert surface.files_relocated == 1
    assert surface.public_symbols_added == 2
    assert surface.public_symbols_removed == 1
    by_current = {item.current_path: item for item in surface.observations}
    moved = by_current[str(tmp_path / "repository" / "moved" / "stable.py")]
    assert moved.content_change == "unchanged"
    assert moved.path_change == "relocated"
    assert moved.public_symbols_added == moved.public_symbols_removed == ()
    renamed = by_current[str(tmp_path / "repository" / "pkg" / "service.py")]
    assert renamed.content_change == "modified"
    assert any('"qualified_name":"pkg.new"' in item for item in renamed.public_symbols_added)
    assert any('"qualified_name":"pkg.old"' in item for item in renamed.public_symbols_removed)
    wrapper = by_current[str(tmp_path / "repository" / "pkg" / "wrapper.py")]
    assert any('"qualified_name":"pkg.wrapper"' in item for item in wrapper.public_symbols_added)

    assert result.history.status == "ready"
    assert result.history.head_commit == "a" * 40
    assert len(result.history.file_contexts) == 3
    assert len(result.history.companions) == 1
    assert result.history.companions[0].source_changed is True
    assert result.history.companions[0].target_changed is True
    assert result.code_schema.status == "ready"
    assert result.code_schema.schema_version == 5
    assert result.code_schema.migration_count == 5
    assert result.code_schema.ddl_digest is not None
    assert result.code_schema.migration_digest is not None

    assert [item.question_id for item in specs] == [
        "evolution.change_surface_requires_review",
        "evolution.change_history_requires_companion_review",
        "evolution.code_owner_schema_requires_migration_review",
    ]
    assert all(item.observation_status == "confirmed" for item in evaluations)
    assert all(item.inference_status == "abstained" for item in evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in evaluations)
    assert all(item.decision is None for item in evaluations)
    assert not any(item.mutation_authority for item in evaluations)

    repeated = analyze_code_change_evolution(database, limit=20)
    assert repeated == result


def test_missing_history_provider_abstains_without_weakening_other_dimensions(
    tmp_path: Path,
) -> None:
    database = _build_transition(tmp_path, history=False)

    result = analyze_code_change_evolution(database, limit=20)
    _specs, evaluations = expected_code_change_evolution_questions(result)

    assert result.status == "partial"
    assert result.change_surface.status == "ready"
    assert result.code_schema.status == "ready"
    assert result.history.status == "abstained"
    assert result.history.reason == "history_provider_missing"
    assert evaluations[0].decision_readiness == "experiment_required"
    assert evaluations[1].observation_status == "abstained"
    assert evaluations[1].question_readiness == "abstained"
    assert evaluations[1].decision_readiness == "abstained"
    assert evaluations[1].decision is None
    assert evaluations[2].decision_readiness == "experiment_required"


def test_missing_or_incomparable_baseline_fails_closed(tmp_path: Path) -> None:
    missing = analyze_code_change_evolution(_build_single_publication(tmp_path / "missing"))
    mismatch = analyze_code_change_evolution(
        _build_transition(
            tmp_path / "mismatch",
            history=False,
            current_signature="different-processing-signature",
        )
    )

    assert missing.change_surface.status == "abstained"
    assert missing.change_surface.reason == "comparable_baseline_missing"
    assert mismatch.change_surface.status == "abstained"
    assert mismatch.change_surface.reason == "baseline_processing_signature_mismatch"
    for result in (missing, mismatch):
        _specs, evaluations = expected_code_change_evolution_questions(result)
        assert evaluations[0].observation_status == "abstained"
        assert evaluations[0].decision_readiness == "abstained"
        assert evaluations[0].decision is None


def test_newer_unpublished_run_invalidates_transition_freshness(tmp_path: Path) -> None:
    database = _build_transition(tmp_path, history=True)
    with CodeState(database) as state:
        state.begin_run(3, 3, PROCESSING_SIGNATURE)
        checkpoint_code_wal(state.connection)
    remove_checkpointed_code_sidecars(database)

    result = analyze_code_change_evolution(database, limit=20)
    _specs, evaluations = expected_code_change_evolution_questions(result)

    assert result.status == "partial"
    assert result.change_surface.status == "abstained"
    assert result.change_surface.reason == "newer_analysis_run_not_published"
    assert result.history.status == "abstained"
    assert result.code_schema.status == "ready"
    assert evaluations[0].observation_status == "abstained"
    assert evaluations[1].observation_status == "abstained"
    assert all(item.decision is None for item in evaluations)


def test_history_digest_mismatch_abstains_without_using_stale_metrics(tmp_path: Path) -> None:
    database = _build_transition(tmp_path, history=True)
    with sqlite3.connect(database) as connection:
        updated = connection.execute(
            """UPDATE external_run_contracts SET result_digest='tampered-result-digest'
            WHERE provider_id=?""",
            (GIT_HISTORY_PROVIDER_ID,),
        )
        assert updated.rowcount == 1
        connection.commit()
        checkpoint_code_wal(connection)
    remove_checkpointed_code_sidecars(database)

    result = analyze_code_change_evolution(database, limit=20)
    _specs, evaluations = expected_code_change_evolution_questions(result)

    assert result.change_surface.status == "ready"
    assert result.history.status == "abstained"
    assert result.history.reason == "external_provider_projection_invalid"
    assert result.history.file_contexts == ()
    assert result.history.companions == ()
    assert evaluations[1].observation_status == "abstained"
    assert evaluations[1].decision_readiness == "abstained"
    assert evaluations[1].counterevidence_status == "not_evaluated"


def test_bounded_change_selection_cannot_be_promoted_to_question_evidence(
    tmp_path: Path,
) -> None:
    result = analyze_code_change_evolution(
        _build_transition(tmp_path, history=True),
        limit=1,
    )
    _specs, evaluations = expected_code_change_evolution_questions(result)

    assert result.change_surface.status == "ready"
    assert result.change_surface.total_observations == 3
    assert result.change_surface.returned_observations == 1
    assert result.change_surface.truncated is True
    assert result.history.status == "abstained"
    assert result.history.reason == "change_surface_selection_truncated"
    assert evaluations[0].observation_status == "abstained"
    assert evaluations[0].decision_readiness == "abstained"
    assert evaluations[1].observation_status == "abstained"


def test_corrected_call_resolution_cannot_claim_change_success(tmp_path: Path) -> None:
    database = _build_transition(tmp_path, history=False)
    before = analyze_code_change_evolution(database, limit=20)

    with sqlite3.connect(database) as connection:
        target = connection.execute(
            """SELECT s.symbol_id,s.version_id FROM symbols s
            JOIN file_versions v ON v.version_id=s.version_id
            JOIN files f ON f.current_version_id=v.version_id
            WHERE f.status='current' AND s.qualified_name='pkg.target_b'"""
        ).fetchone()
        assert target is not None
        updated = connection.execute(
            """UPDATE code_references SET target_symbol_id=?,target_version_id=?
            WHERE reference_id=(SELECT MAX(reference_id) FROM code_references
                                WHERE name='target_a')""",
            target,
        )
        assert updated.rowcount == 1
        connection.commit()
        checkpoint_code_wal(connection)
    remove_checkpointed_code_sidecars(database)

    after = analyze_code_change_evolution(database, limit=20)
    _specs, evaluations = expected_code_change_evolution_questions(after)

    assert after.change_surface == before.change_surface
    assert after.history == before.history
    assert after.code_schema == before.code_schema
    assert after.analysis_id == before.analysis_id
    assert all(item.decision is None for item in evaluations)
    assert evaluations[0].decision_readiness == "experiment_required"
    assert "corrected_graph_resolution_is_not_change_success_evidence" in (
        evaluations[0].limitations
    )


def test_strict_wire_roundtrip_rejects_tampering_and_unknown_fields(tmp_path: Path) -> None:
    result = analyze_code_change_evolution(
        _build_transition(tmp_path, history=True),
        limit=20,
    )
    payload = json.loads(json.dumps(result.as_payload()))

    assert payload["schema"] == CODE_CHANGE_EVOLUTION_SCHEMA
    assert parse_code_change_evolution_payload(payload) == result

    tampered = json.loads(json.dumps(payload))
    tampered["change_surface"]["observations"][0]["content_change"] = "unchanged"
    with pytest.raises(ValueError):
        parse_code_change_evolution_payload(tampered)

    unknown = json.loads(json.dumps(payload))
    unknown["quality_score"] = 100
    with pytest.raises(ValueError, match="fields are invalid"):
        parse_code_change_evolution_payload(unknown)

    forged_decision = json.loads(json.dumps(payload))
    forged_decision["decision"] = "accept"
    with pytest.raises(ValueError, match="cannot infer a defect or own a decision"):
        parse_code_change_evolution_payload(forged_decision)


def test_immutable_reader_leaves_no_sqlite_sidecars(tmp_path: Path) -> None:
    database = _build_transition(tmp_path, history=False)
    sidecars = tuple(Path(f"{database}{suffix}") for suffix in ("-journal", "-wal", "-shm"))
    before = tuple(path.exists() for path in sidecars)

    result = analyze_code_change_evolution(database, limit=20)

    assert result.change_surface.status == "ready"
    assert tuple(path.exists() for path in sidecars) == before == (False, False, False)


@pytest.mark.parametrize("limit", (0, 201, True))
def test_public_reader_rejects_invalid_bounds(tmp_path: Path, limit: object) -> None:
    database = _build_transition(tmp_path, history=False)

    with pytest.raises(ValueError, match="limit must be between"):
        analyze_code_change_evolution(database, limit=limit)  # type: ignore[arg-type]
