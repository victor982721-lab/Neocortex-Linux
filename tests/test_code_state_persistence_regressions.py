# region [00] Contexto del módulo
# Módulo: tests/test_code_state_persistence_regressions.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.code_state as code_state_module
from _02_Deduplicacion import FileSnapshot
from _04_Nucleo_Operativo.cancellation import CancellationRequested
from _04_Nucleo_Operativo.code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    ArtifactKind,
    CodeAnalysis,
    CodeChunk,
    CodeFileInput,
    DependencyRecord,
    DiagnosticRecord,
    DiagnosticSeverity,
    ProjectHint,
    ReferenceRecord,
    SourceRange,
    SymbolRecord,
)
from _04_Nucleo_Operativo.code_state import CodeState, SkippedCodeObservation
from _04_Nucleo_Operativo.semantic_models import fingerprint_bytes, fingerprint_text
from _04_Nucleo_Operativo.sqlite_cancellation import SQLiteCancellationBridge
from _04_Nucleo_Operativo.sqlite_paths import readonly_sqlite_uri
# endregion [01]

# region [02] Implementación


PROCESSING_SIGNATURE = "code-state-regression-v1"


def _snapshot(
    path: Path,
    *,
    file_id: int,
    text: str,
    mtime_ns: int = 100,
) -> FileSnapshot:
    return FileSnapshot(
        str(path),
        1,
        file_id,
        len(text.encode("utf-8")),
        mtime_ns,
        50,
    )


def _classification(kind: ArtifactKind = ArtifactKind.SOURCE) -> ArtifactClassification:
    return ArtifactClassification("python", kind, 1.0, ("test-fixture",))


def _source_range(text: str) -> SourceRange:
    return SourceRange(1, 0, 1, len(text), 0, len(text.encode("utf-8")))


def _analysis(
    snapshot: FileSnapshot,
    text: str,
    *,
    kind: ArtifactKind = ArtifactKind.SOURCE,
    symbols: tuple[SymbolRecord, ...] = (),
    references: tuple[ReferenceRecord, ...] = (),
    dependencies: tuple[DependencyRecord, ...] = (),
    chunks: tuple[CodeChunk, ...] = (),
    project_hints: tuple[ProjectHint, ...] = (),
) -> CodeAnalysis:
    text_fingerprint = fingerprint_text(text)
    raw_fingerprint = fingerprint_bytes(text.encode("utf-8"))
    source = CodeFileInput(
        snapshot,
        text,
        text.encode("utf-8"),
        "utf-8",
        _classification(kind),
        PROCESSING_SIGNATURE,
    )
    return CodeAnalysis(
        input=source,
        status=AnalysisStatus.COMPLETE,
        analyzer_id="test-analyzer",
        analyzer_version="1",
        parser_kind="test-parser",
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
        chunks=chunks,
        project_hints=project_hints,
        provenance={"fixture": True},
    )


def _skipped(
    snapshot: FileSnapshot,
    text: str,
    *,
    status: AnalysisStatus,
) -> SkippedCodeObservation:
    fingerprint = fingerprint_text(text)
    return SkippedCodeObservation(
        snapshot=snapshot,
        classification=_classification(),
        processing_signature=PROCESSING_SIGNATURE,
        status=status,
        analyzer_id="test-skipped",
        analyzer_version="1",
        parser_kind="text-fallback",
        diagnostic=DiagnosticRecord(
            "test-skipped",
            "fixture",
            DiagnosticSeverity.ERROR
            if status is AnalysisStatus.ERROR
            else DiagnosticSeverity.INFO,
            "fixture observation",
            tool_name="test-skipped",
            tool_version="1",
        ),
        encoding="utf-8",
        text_excerpt=text,
        raw_xxh3_128=fingerprint.xxh3_128,
        raw_xxh3_64_guard=fingerprint.xxh3_64_guard,
    )


def test_error_retry_preserves_attempt_history(tmp_path: Path) -> None:
    database = tmp_path / "code.sqlite3"
    snapshot = _snapshot(tmp_path / "broken.py", file_id=1, text="broken")
    observation = _skipped(snapshot, "broken", status=AnalysisStatus.ERROR)

    with CodeState(database) as state:
        first, replaced = state.store_skipped(observation, 1)
        assert replaced is False
        assert (
            state.reuse_cached(
                snapshot,
                PROCESSING_SIGNATURE,
                2,
                retry_errors=True,
            )
            is None
        )

        second, replaced = state.store_skipped(observation, 2)
        assert replaced is True
        assert second != first
        rows = state.connection.execute(
            """SELECT version_id,invalidated_ns,invalidation_reason
            FROM file_versions ORDER BY version_id"""
        ).fetchall()
        history = state.connection.execute(
            """SELECT version_id,replacement_version_id,reason
            FROM invalidation_history ORDER BY invalidation_id"""
        ).fetchall()
        current = state.connection.execute(
            "SELECT current_version_id FROM files"
        ).fetchone()[0]

    assert [int(row[0]) for row in rows] == [first, second]
    assert rows[0][1] is not None
    assert str(rows[0][2]) == "superseded_observation"
    assert rows[1][1] is None
    assert [tuple(row) for row in history] == [
        (first, second, "superseded_observation")
    ]
    assert int(current) == second


def test_cache_diagnostic_count_excludes_external_and_graph_projections(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    snapshot = _snapshot(tmp_path / "cached.py", file_id=2, text="cached")
    observation = _skipped(snapshot, "cached", status=AnalysisStatus.TEXT_ONLY)

    with CodeState(database) as state:
        version_id, _ = state.store_skipped(observation, 1)
        state.connection.executemany(
            """INSERT INTO diagnostics(
            version_id,source,code,severity,message,tool_name,tool_version,
            confirmed,confidence,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    version_id,
                    source,
                    "derived-fixture",
                    "info",
                    "derived fixture",
                    "fixture",
                    "1",
                    0,
                    1.0,
                    "{}",
                )
                for source in (
                    "external:vulture-unused-static",
                    "external:future-provider",
                    "neocortex-reference-graph",
                )
            ),
        )
        all_diagnostics = state.connection.execute(
            "SELECT COUNT(*) FROM diagnostics WHERE version_id=?",
            (version_id,),
        ).fetchone()[0]
        cached = state.reuse_cached(
            snapshot,
            PROCESSING_SIGNATURE,
            2,
            retry_errors=False,
        )

    assert int(all_diagnostics) == 4
    assert cached is not None
    assert cached.diagnostics == 1


def test_full_cache_validation_rejects_metadata_preserving_change(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    snapshot = _snapshot(tmp_path / "same.py", file_id=2, text="alpha")
    first_observation = _skipped(
        snapshot,
        "alpha",
        status=AnalysisStatus.TEXT_ONLY,
    )
    changed_observation = _skipped(
        snapshot,
        "bravo",
        status=AnalysisStatus.TEXT_ONLY,
    )
    first_fingerprint = fingerprint_text("alpha")
    changed_fingerprint = fingerprint_text("bravo")

    with CodeState(database) as state:
        first, _ = state.store_skipped(first_observation, 1)
        assert (
            state.reuse_cached(
                snapshot,
                PROCESSING_SIGNATURE,
                2,
                retry_errors=False,
            )
            is not None
        )
        assert (
            state.reuse_cached(
                snapshot,
                PROCESSING_SIGNATURE,
                2,
                retry_errors=False,
                raw_xxh3_128=first_fingerprint.xxh3_128,
                raw_xxh3_64_guard=first_fingerprint.xxh3_64_guard,
            )
            is not None
        )
        assert (
            state.reuse_cached(
                snapshot,
                PROCESSING_SIGNATURE,
                2,
                retry_errors=False,
                raw_xxh3_128=changed_fingerprint.xxh3_128,
                raw_xxh3_64_guard=changed_fingerprint.xxh3_64_guard,
            )
            is None
        )
        with pytest.raises(ValueError, match="supplied together"):
            state.reuse_cached(
                snapshot,
                PROCESSING_SIGNATURE,
                2,
                retry_errors=False,
                raw_xxh3_128=changed_fingerprint.xxh3_128,
            )

        second, replaced = state.store_skipped(changed_observation, 2)
        rows = state.connection.execute(
            """SELECT raw_xxh3_128,invalidated_ns
            FROM file_versions ORDER BY version_id"""
        ).fetchall()

    assert replaced is True
    assert second != first
    assert str(rows[0][0]) == first_fingerprint.xxh3_128
    assert rows[0][1] is not None
    assert str(rows[1][0]) == changed_fingerprint.xxh3_128
    assert rows[1][1] is None


def test_path_change_rejects_cache_and_publishes_successor_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    original = _snapshot(tmp_path / "original.py", file_id=3, text="alpha")
    moved = _snapshot(tmp_path / "moved.py", file_id=3, text="alpha")

    with CodeState(database) as state:
        state.store_skipped(
            _skipped(original, "alpha", status=AnalysisStatus.TEXT_ONLY),
            1,
        )

        unchanged_trace: list[str] = []
        state.connection.set_trace_callback(unchanged_trace.append)
        unchanged = state.reuse_cached(
            original,
            PROCESSING_SIGNATURE,
            2,
            retry_errors=False,
        )
        state.connection.set_trace_callback(None)

        moved_trace: list[str] = []
        state.connection.set_trace_callback(moved_trace.append)
        renamed = state.reuse_cached(
            moved,
            PROCESSING_SIGNATURE,
            3,
            retry_errors=False,
        )
        state.connection.set_trace_callback(None)

        path_after_rejected_reuse = state.connection.execute(
            "SELECT current_path FROM files"
        ).fetchone()[0]
        observed_after_rejected_reuse = state.connection.execute(
            "SELECT last_observed_run_id FROM file_versions"
        ).fetchone()[0]

        successor, replaced = state.store_skipped(
            _skipped(moved, "alpha", status=AnalysisStatus.TEXT_ONLY),
            3,
        )
        current_path = state.connection.execute(
            "SELECT current_path FROM files"
        ).fetchone()[0]
        versions = state.connection.execute(
            """SELECT version_id,path_observed,last_observed_run_id,invalidated_ns
            FROM file_versions ORDER BY version_id"""
        ).fetchall()
        fts_paths = state.connection.execute(
            """SELECT DISTINCT v.version_id,x.path,v.invalidated_ns
            FROM code_fts x JOIN file_versions v ON v.version_id=x.version_id
            ORDER BY v.version_id"""
        ).fetchall()

    def _fts_path_updates(statements: list[str]) -> list[str]:
        return [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("UPDATE CODE_FTS SET PATH=")
        ]

    assert unchanged is not None
    assert _fts_path_updates(unchanged_trace) == []
    assert renamed is None
    assert _fts_path_updates(moved_trace) == []
    assert not any(
        statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))
        for statement in moved_trace
    )
    assert str(path_after_rejected_reuse) == original.path
    assert int(observed_after_rejected_reuse) == 2
    assert replaced is True
    assert str(current_path) == moved.path
    assert int(versions[0][0]) != successor
    assert str(versions[0][1]) == original.path
    assert int(versions[0][2]) == 2
    assert versions[0][3] is not None
    assert tuple(versions[1]) == (successor, moved.path, 3, None)
    assert [tuple(row) for row in fts_paths] == [
        (int(versions[0][0]), original.path, versions[0][3]),
        (successor, moved.path, None),
    ]


def test_graph_rebinds_current_reference_after_target_version_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    target_range = _source_range("target")
    target_symbol = SymbolRecord(
        "function",
        "target",
        "pkg.target",
        "target()",
        target_range,
        visibility="public",
    )
    reference = ReferenceRecord(
        "call",
        "target",
        _source_range("target()"),
        target_hint="pkg.target",
        confirmed=True,
        confidence=1.0,
        evidence="fixture-call",
    )
    target_v1 = _analysis(
        _snapshot(tmp_path / "target.py", file_id=10, text="target", mtime_ns=1),
        "target",
        symbols=(target_symbol,),
    )
    caller = _analysis(
        _snapshot(tmp_path / "caller.py", file_id=11, text="target()"),
        "target()",
        references=(reference,),
    )
    target_v2 = _analysis(
        _snapshot(tmp_path / "target.py", file_id=10, text="target2", mtime_ns=2),
        "target2",
        symbols=(target_symbol,),
    )

    with CodeState(database) as state:
        first_target, _ = state.store_analysis(target_v1, 1)
        caller_version, _ = state.store_analysis(caller, 1)
        state.finalize_graph(1)
        initial_target = state.connection.execute(
            """SELECT target_version_id FROM code_references
            WHERE version_id=?""",
            (caller_version,),
        ).fetchone()[0]
        assert int(initial_target) == first_target

        second_target, _ = state.store_analysis(target_v2, 2)
        state.finalize_graph(2)
        rebound = state.connection.execute(
            """SELECT r.target_version_id,v.invalidated_ns
            FROM code_references r JOIN file_versions v
                ON v.version_id=r.target_version_id
            WHERE r.version_id=?""",
            (caller_version,),
        ).fetchone()

    assert int(rebound[0]) == second_target
    assert rebound[1] is None


def test_graph_retracts_obsolete_current_diagnostics(tmp_path: Path) -> None:
    database = tmp_path / "code.sqlite3"
    unused = SymbolRecord(
        "function",
        "unused",
        "pkg.unused",
        "unused()",
        _source_range("unused"),
        visibility="private",
    )
    dependency = DependencyRecord(
        "missing",
        "python_relative_import",
        confirmed=True,
        confidence=1.0,
        evidence="fixture-import",
    )
    module = SymbolRecord(
        "module",
        "missing",
        "missing",
        None,
        _source_range("missing"),
        visibility="public",
    )
    reference = ReferenceRecord(
        "call",
        "unused",
        _source_range("unused()"),
        target_hint="pkg.unused",
        confirmed=True,
        confidence=1.0,
        evidence="fixture-call",
    )

    with CodeState(database) as state:
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "owner.py", file_id=20, text="unused"),
                "unused",
                symbols=(unused,),
            ),
            1,
        )
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "consumer.py", file_id=21, text="import"),
                "import",
                dependencies=(dependency,),
            ),
            1,
        )
        state.finalize_graph(1)
        before = {
            str(row[0])
            for row in state.connection.execute(
                """SELECT d.code FROM diagnostics d JOIN file_versions v
                ON v.version_id=d.version_id WHERE v.invalidated_ns IS NULL
                AND d.source IN ('neocortex-project-resolver',
                    'neocortex-reference-graph')"""
            )
        }
        assert before == {"unresolved_relative_import", "probable_dead_symbol"}

        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "missing.py", file_id=22, text="missing"),
                "missing",
                symbols=(module,),
            ),
            2,
        )
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "use.py", file_id=23, text="unused()"),
                "unused()",
                references=(reference,),
            ),
            2,
        )
        state.finalize_graph(2)
        after = state.connection.execute(
            """SELECT d.code FROM diagnostics d JOIN file_versions v
            ON v.version_id=d.version_id WHERE v.invalidated_ns IS NULL
            AND d.source IN ('neocortex-project-resolver',
                'neocortex-reference-graph')"""
        ).fetchall()

    assert after == []


def test_project_instances_keep_homonymous_roots_and_conflicts_ambiguous(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    root_a = tmp_path / "copy-a"
    root_b = tmp_path / "copy-b"
    hint_a = ProjectHint("rust", "same-name", str(root_a), 1.0, ("manifest",), "cargo")
    hint_b = ProjectHint("rust", "same-name", str(root_b), 1.0, ("manifest",), "cargo")
    manifest_a = _analysis(
        _snapshot(root_a / "Cargo.toml", file_id=30, text="manifest-a"),
        "manifest-a",
        kind=ArtifactKind.MANIFEST,
        project_hints=(hint_a,),
    )
    manifest_b = _analysis(
        _snapshot(root_b / "Cargo.toml", file_id=31, text="manifest-b"),
        "manifest-b",
        kind=ArtifactKind.MANIFEST,
        project_hints=(hint_b,),
    )

    with CodeState(database) as state:
        version_a, _ = state.store_analysis(manifest_a, 1)
        version_b, _ = state.store_analysis(manifest_b, 1)
        projects = state.connection.execute(
            """SELECT project_id,project_key,probable_root,evidence_json
            FROM projects ORDER BY probable_root"""
        ).fetchall()
        assert len(projects) == 2
        assert str(projects[0][1]) != str(projects[1][1])
        evidence = [json.loads(str(row[3])) for row in projects]
        assert evidence[0]["family_key"] == evidence[1]["family_key"]
        assert evidence[0]["instance_root_key"] != evidence[1]["instance_root_key"]

        project_a = next(int(row[0]) for row in projects if Path(str(row[2])) == root_a)
        state.connection.execute(
            """INSERT INTO project_memberships(
            project_id,version_id,proposed_path,relation,confidence,selected,
            evidence_json) VALUES(?,?,'Cargo.toml','ambiguous-copy',0.5,1,'{}')""",
            (project_a, version_b),
        )
        state.connection.commit()
        state.finalize_graph(1)
        memberships = state.connection.execute(
            """SELECT version_id,selected,conflict_group
            FROM project_memberships WHERE project_id=?
            AND proposed_path='Cargo.toml' COLLATE NOCASE ORDER BY version_id""",
            (project_a,),
        ).fetchall()

    assert [int(row[0]) for row in memberships] == [version_a, version_b]
    assert [int(row[1]) for row in memberships] == [0, 0]
    assert memberships[0][2] is not None
    assert str(memberships[0][2]) == str(memberships[1][2])


def _chunks(*texts: str) -> tuple[CodeChunk, ...]:
    return tuple(
        CodeChunk(index, text, _source_range(text))
        for index, text in enumerate(texts)
    )


def test_graph_reconciles_fts_projects_once_and_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "code.sqlite3"
    root = tmp_path / "alpha"
    hint = ProjectHint("rust", "manifest-label", str(root), 1.0, ("manifest",), "cargo")
    manifest = _analysis(
        _snapshot(root / "Cargo.toml", file_id=40, text="manifest"),
        "manifest",
        kind=ArtifactKind.MANIFEST,
        chunks=_chunks("manifest"),
        project_hints=(hint,),
    )
    upper = _analysis(
        _snapshot(root / "src" / "Thing.py", file_id=41, text="upper"),
        "upper",
        chunks=_chunks("upper-a", "upper-b"),
    )
    lower = _analysis(
        _snapshot(root / "src" / "Other.py", file_id=42, text="lower"),
        "lower",
        chunks=_chunks("lower"),
    )

    with CodeState(database) as state:
        state.store_analysis(manifest, 1)
        upper_version, _ = state.store_analysis(upper, 1)
        lower_version, _ = state.store_analysis(lower, 1)
        changed_rows: list[int] = []
        original = state._synchronize_current_fts_projects

        def tracked_sync() -> None:
            original()
            changed_rows.append(int(state.connection.execute("SELECT changes()").fetchone()[0]))

        monkeypatch.setattr(state, "_synchronize_current_fts_projects", tracked_sync)
        first_trace: list[str] = []
        state.connection.set_trace_callback(first_trace.append)
        state.finalize_graph(1)
        state.connection.set_trace_callback(None)
        labels = state.connection.execute(
            "SELECT version_id,project FROM code_fts ORDER BY rowid"
        ).fetchall()
        state.connection.execute(
            """UPDATE project_memberships SET proposed_path='collision.py'
            WHERE relation='under_manifest_root' AND version_id IN (?,?)""",
            (upper_version, lower_version),
        )
        state._resolve_membership_conflicts()
        state._synchronize_current_fts_projects()
        selections = state.connection.execute(
            """SELECT version_id,selected FROM project_memberships
            WHERE relation='under_manifest_root' AND version_id IN (?,?)
            ORDER BY version_id""",
            (upper_version, lower_version),
        ).fetchall()

        second_trace: list[str] = []
        state.connection.set_trace_callback(second_trace.append)
        state.finalize_graph(2)
        state.connection.set_trace_callback(None)
        temporary = state.connection.execute(
            """SELECT name FROM temp.sqlite_temp_master
            WHERE name='_nc_fts_project_map'"""
        ).fetchall()

    def fts_project_updates(statements: list[str]) -> list[str]:
        return [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("UPDATE CODE_FTS")
        ]

    assert [str(row[1]) for row in labels] == [
        "manifest-label",
        "manifest-label",
        "manifest-label",
        "manifest-label",
    ]
    assert [tuple(row) for row in selections] == [
        (upper_version, 0),
        (lower_version, 0),
    ]
    assert changed_rows[0] > 0
    assert changed_rows[1:] == [0, 0]
    assert len(fts_project_updates(first_trace)) == 1
    assert len(fts_project_updates(second_trace)) == 1
    assert temporary == []


def test_fts_project_sync_preserves_history_across_rename_root_and_removal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    first_hint = ProjectHint(
        "rust", "first-manifest", str(first_root), 1.0, ("manifest",), "cargo"
    )
    second_hint = ProjectHint(
        "rust", "second-manifest", str(second_root), 1.0, ("manifest",), "cargo"
    )
    manifest_v1 = _analysis(
        _snapshot(first_root / "Cargo.toml", file_id=50, text="m1", mtime_ns=1),
        "m1",
        kind=ArtifactKind.MANIFEST,
        chunks=_chunks("m1"),
        project_hints=(first_hint,),
    )
    source_v1 = _analysis(
        _snapshot(first_root / "src" / "main.py", file_id=51, text="s1", mtime_ns=1),
        "s1",
        chunks=_chunks("s1-a", "s1-b"),
    )
    manifest_v2 = _analysis(
        _snapshot(second_root / "Cargo.toml", file_id=50, text="m2", mtime_ns=2),
        "m2",
        kind=ArtifactKind.MANIFEST,
        chunks=_chunks("m2"),
        project_hints=(second_hint,),
    )
    source_v2 = _analysis(
        _snapshot(second_root / "src" / "main.py", file_id=51, text="s2", mtime_ns=2),
        "s2",
        chunks=_chunks("s2-a", "s2-b"),
    )

    with CodeState(database) as state:
        old_manifest, _ = state.store_analysis(manifest_v1, 1)
        old_source, _ = state.store_analysis(source_v1, 1)
        state.finalize_graph(1)
        new_manifest, _ = state.store_analysis(manifest_v2, 2)
        new_source, _ = state.store_analysis(source_v2, 2)
        state.finalize_graph(2)
        labels_after_move = state.connection.execute(
            """SELECT version_id,MIN(project),MAX(project),COUNT(*)
            FROM code_fts GROUP BY version_id ORDER BY version_id"""
        ).fetchall()

        assert state.reuse_cached(
            source_v2.input.snapshot, PROCESSING_SIGNATURE, 3, retry_errors=False
        ) is not None
        assert state.mark_missing(3) == 1
        state.finalize_graph(3)
        labels_after_removal = state.connection.execute(
            """SELECT version_id,MIN(project),MAX(project),COUNT(*)
            FROM code_fts GROUP BY version_id ORDER BY version_id"""
        ).fetchall()

        # Parser-owned manifest evidence is the lowest-priority FTS fallback.
        state.connection.execute(
            """DELETE FROM project_memberships
            WHERE version_id=?
            AND relation IN ('under_manifest_root','inferred_root')""",
            (new_source,),
        )
        state.connection.execute(
            """INSERT OR IGNORE INTO project_memberships(
            project_id,version_id,proposed_path,relation,confidence,selected,evidence_json)
            SELECT project_id,?,'main.py','manifest',1.0,1,'{}'
            FROM projects WHERE name='second-manifest'""",
            (new_source,),
        )
        state._synchronize_current_fts_projects()
        manifest_fallback = state.connection.execute(
            "SELECT DISTINCT project FROM code_fts WHERE version_id=?",
            (new_source,),
        ).fetchall()

    assert [tuple(row) for row in labels_after_move] == [
        (old_manifest, "first-manifest", "first-manifest", 1),
        (old_source, "first-manifest", "first-manifest", 2),
        (new_manifest, "second-manifest", "second-manifest", 1),
        (new_source, "second-manifest", "second-manifest", 2),
    ]
    assert [tuple(row) for row in labels_after_removal] == [
        (old_manifest, "first-manifest", "first-manifest", 1),
        (old_source, "first-manifest", "first-manifest", 2),
        (new_manifest, "second-manifest", "second-manifest", 1),
        (new_source, "second-root", "second-root", 2),
    ]
    assert [tuple(row) for row in manifest_fallback] == [("second-manifest",)]


def test_fts_project_sync_matches_legacy_fixture_with_bounded_statements(
    tmp_path: Path,
    record_property: object,
) -> None:
    database = tmp_path / "code.sqlite3"
    root = tmp_path / "benchmark-root"
    hint = ProjectHint("python", "benchmark", str(root), 1.0, ("manifest",), "pyproject")
    manifest = _analysis(
        _snapshot(root / "pyproject.toml", file_id=60, text="manifest"),
        "manifest",
        kind=ArtifactKind.MANIFEST,
        chunks=_chunks("manifest"),
        project_hints=(hint,),
    )

    with CodeState(database) as state:
        state.store_analysis(manifest, 1)
        for index in range(80):
            text = f"source-{index}"
            state.store_analysis(
                _analysis(
                    _snapshot(root / "src" / f"file-{index}.py", file_id=1000 + index, text=text),
                    text,
                    chunks=_chunks(f"{text}-a", f"{text}-b", f"{text}-c"),
                ),
                1,
            )
        state.finalize_graph(1)
        mapping = state.connection.execute(
            """SELECT m.version_id,p.name FROM project_memberships m
            JOIN projects p ON p.project_id=m.project_id
            WHERE m.relation IN(
                'under_manifest_root','inferred_root','manifest')
            ORDER BY m.version_id,
                CASE m.relation WHEN 'under_manifest_root' THEN 0
                    WHEN 'inferred_root' THEN 1 ELSE 2 END,
                m.project_id"""
        ).fetchall()
        preferred = dict(reversed([(int(row[0]), str(row[1])) for row in reversed(mapping)]))

        state.connection.execute("UPDATE code_fts SET project='' WHERE project<>''")
        legacy_trace: list[str] = []
        state.connection.set_trace_callback(legacy_trace.append)
        legacy_start = time.perf_counter()
        for version_id, project in preferred.items():
            state.connection.execute(
                "UPDATE code_fts SET project=? WHERE version_id=?",
                (project, version_id),
            )
        legacy_seconds = time.perf_counter() - legacy_start
        state.connection.set_trace_callback(None)
        legacy = state.connection.execute(
            "SELECT rowid,project FROM code_fts ORDER BY rowid"
        ).fetchall()

        state.connection.execute("UPDATE code_fts SET project='' WHERE project<>''")
        sync_trace: list[str] = []
        state.connection.set_trace_callback(sync_trace.append)
        sync_start = time.perf_counter()
        state._synchronize_current_fts_projects()
        sync_seconds = time.perf_counter() - sync_start
        state.connection.set_trace_callback(None)
        synchronized = state.connection.execute(
            "SELECT rowid,project FROM code_fts ORDER BY rowid"
        ).fetchall()
        row_count = len(synchronized)
        temporary = state.connection.execute(
            """SELECT name FROM temp.sqlite_temp_master
            WHERE name='_nc_fts_project_map'"""
        ).fetchall()

    legacy_updates = [
        statement for statement in legacy_trace
        if statement.lstrip().upper().startswith("UPDATE CODE_FTS")
    ]
    sync_updates = [
        statement for statement in sync_trace
        if statement.lstrip().upper().startswith("UPDATE CODE_FTS")
    ]
    assert [tuple(row) for row in synchronized] == [tuple(row) for row in legacy]
    assert len(legacy_updates) == 81
    assert len(sync_updates) == 1
    assert temporary == []
    assert callable(record_property)
    record_property("fts_rows", row_count)  # type: ignore[operator]
    record_property("legacy_update_statements", len(legacy_updates))  # type: ignore[operator]
    record_property("set_oriented_update_statements", len(sync_updates))  # type: ignore[operator]
    record_property("legacy_seconds", legacy_seconds)  # type: ignore[operator]
    record_property("set_oriented_seconds", sync_seconds)  # type: ignore[operator]


def test_mark_missing_is_keyset_batched_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "code.sqlite3"
    with CodeState(database) as state:
        observations: list[tuple[FileSnapshot, SkippedCodeObservation]] = []
        for index in range(5):
            text = f"file-{index}"
            snapshot = _snapshot(
                tmp_path / f"file-{index}.py",
                file_id=100 + index,
                text=text,
            )
            observation = _skipped(
                snapshot,
                text,
                status=AnalysisStatus.TEXT_ONLY,
            )
            state.store_skipped(observation, 1)
            observations.append((snapshot, observation))

        assert (
            state.reuse_cached(
                observations[0][0],
                PROCESSING_SIGNATURE,
                2,
                retry_errors=False,
            )
            is not None
        )
        assert state.mark_missing(2, batch_size=2) == 4
        assert state.mark_missing(2, batch_size=2) == 0
        statuses = state.connection.execute(
            "SELECT status,COUNT(*) FROM files GROUP BY status ORDER BY status"
        ).fetchall()
        invalidations = state.connection.execute(
            """SELECT COUNT(*) FROM invalidation_history
            WHERE reason='not_seen_in_complete_inventory'"""
        ).fetchone()[0]

    assert [tuple(row) for row in statuses] == [("current", 1), ("missing", 4)]
    assert int(invalidations) == 4


def _graph_derived_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, list[tuple[object, ...]]]:
    queries = {
        "projects": "SELECT * FROM projects ORDER BY project_id",
        "memberships": (
            "SELECT * FROM project_memberships ORDER BY project_id,version_id"
        ),
        "edges": (
            "SELECT * FROM project_edges "
            "ORDER BY source_project_id,dependency_name,edge_kind"
        ),
        "references": (
            "SELECT reference_id,target_symbol_id,target_version_id "
            "FROM code_references ORDER BY reference_id"
        ),
        "dependencies": (
            "SELECT dependency_id,resolved_version_id "
            "FROM dependencies ORDER BY dependency_id"
        ),
        "diagnostics": "SELECT * FROM diagnostics ORDER BY diagnostic_id",
        "relations": (
            "SELECT * FROM version_relations "
            "ORDER BY left_version_id,right_version_id,relation_kind"
        ),
        "fts": (
            "SELECT rowid,chunk_id,version_id,path,project,language,symbol,signature,body "
            "FROM code_fts ORDER BY rowid"
        ),
    }
    return {
        name: [tuple(row) for row in connection.execute(query).fetchall()]
        for name, query in queries.items()
    }


@pytest.mark.parametrize(
    "phase",
    [
        "_assign_manifest_roots",
        "_infer_incomplete_projects",
        "_resolve_symbols_and_dependencies",
        "_record_duplicate_relations",
        "_resolve_membership_conflicts",
        "_synchronize_current_fts_projects",
        "_record_project_edges",
        "_record_probable_dead_symbols",
    ],
)
def test_finalize_graph_rolls_back_each_phase_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    database = tmp_path / f"{phase}.sqlite3"
    symbol = SymbolRecord(
        "function",
        "unused",
        "pkg.unused",
        "unused()",
        _source_range("unused"),
        visibility="private",
    )
    dependency = DependencyRecord(
        "missing",
        "python_relative_import",
        confirmed=True,
        confidence=1.0,
        evidence="fixture-import",
    )
    with CodeState(database) as state:
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "src" / "owner.py", file_id=500, text="unused"),
                "unused",
                symbols=(symbol,),
                dependencies=(dependency,),
            ),
            1,
        )
        before = _graph_derived_snapshot(state.connection)
        original = getattr(state, phase)

        def fail_after_phase(*args: object) -> None:
            original(*args)
            raise RuntimeError(f"injected after {phase}")

        monkeypatch.setattr(state, phase, fail_after_phase)
        with pytest.raises(RuntimeError, match=f"injected after {phase}"):
            state.finalize_graph(2)
        after = _graph_derived_snapshot(state.connection)
        temporary = state.connection.execute(
            """SELECT name FROM temp.sqlite_temp_master
            WHERE name='_nc_fts_project_map'"""
        ).fetchall()

        assert state.connection.in_transaction is False
        assert after == before
        assert temporary == []


def test_finalize_graph_rolls_back_keyboard_interrupt(tmp_path: Path) -> None:
    database = tmp_path / "interrupt.sqlite3"
    with CodeState(database) as state:
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "src" / "owner.py", file_id=501, text="owner"),
                "owner",
            ),
            1,
        )
        before = _graph_derived_snapshot(state.connection)
        original = state._resolve_symbols_and_dependencies

        def interrupt_after_phase() -> None:
            original()
            raise KeyboardInterrupt

        state._resolve_symbols_and_dependencies = interrupt_after_phase  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt):
            state.finalize_graph(2)

        assert state.connection.in_transaction is False
        assert _graph_derived_snapshot(state.connection) == before


def test_finalize_graph_sqlite_cancellation_rolls_back_and_clears_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "sqlite-cancellation.sqlite3"
    symbol = SymbolRecord(
        "function",
        "unused",
        "pkg.unused",
        "unused()",
        _source_range("unused"),
        visibility="private",
    )
    dependency = DependencyRecord(
        "missing",
        "python_relative_import",
        confirmed=True,
        confidence=1.0,
        evidence="fixture-import",
    )
    cancellation = CancellationRequested("cancel inside graph SQLite")
    armed = False
    checkpoints = 0

    def cancel_when_armed() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if armed:
            raise cancellation

    original_scope = code_state_module.sqlite_cancellation_scope

    def one_instruction_scope(
        connection: sqlite3.Connection,
        bridge: SQLiteCancellationBridge,
    ) -> AbstractContextManager[SQLiteCancellationBridge]:
        return original_scope(connection, bridge, instructions=1)

    monkeypatch.setattr(
        code_state_module,
        "sqlite_cancellation_scope",
        one_instruction_scope,
    )
    with CodeState(database) as state:
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "src" / "owner.py", file_id=503, text="unused"),
                "unused",
                symbols=(symbol,),
                dependencies=(dependency,),
            ),
            1,
        )
        # Inferred projects intentionally remain ``ambiguous``; the return
        # value counts only durable ``current`` manifest-backed projects.
        assert state.finalize_graph(1) == 0
        before = _graph_derived_snapshot(state.connection)
        original_infer = state._infer_incomplete_projects

        def arm_after_inference(framework_run_id: int) -> None:
            nonlocal armed
            original_infer(framework_run_id)
            armed = True

        state._infer_incomplete_projects = arm_after_inference  # type: ignore[method-assign]
        with pytest.raises(CancellationRequested) as raised:
            state.finalize_graph(2, cancellation_check=cancel_when_armed)

        calls_after_cancel = checkpoints
        probe_count = state.connection.execute(
            """WITH RECURSIVE probe(n) AS (
            VALUES(1) UNION ALL SELECT n+1 FROM probe WHERE n<2000)
            SELECT COUNT(*) FROM probe"""
        ).fetchone()[0]
        temporary = state.connection.execute(
            """SELECT name FROM temp.sqlite_temp_master
            WHERE name LIKE '_nc_%' ORDER BY name"""
        ).fetchall()

        assert raised.value is cancellation
        assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
        assert state.connection.in_transaction is False
        assert _graph_derived_snapshot(state.connection) == before
        assert probe_count == 2000
        assert checkpoints == calls_after_cancel
        assert temporary == []

        state._infer_incomplete_projects = original_infer  # type: ignore[method-assign]
        monkeypatch.setattr(
            code_state_module,
            "sqlite_cancellation_scope",
            original_scope,
        )
        assert state.finalize_graph(3) == 0
        recovered = _graph_derived_snapshot(state.connection)
        expected = {
            **before,
            # A successful rebuild advances only project freshness provenance.
            "projects": [
                (*row[:9], 3, *row[10:]) for row in before["projects"]
            ],
        }
        assert recovered == expected


def test_finalize_graph_keeps_reader_snapshot_atomic(tmp_path: Path) -> None:
    database = tmp_path / "reader.sqlite3"
    with CodeState(database) as state:
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "src" / "owner.py", file_id=502, text="owner"),
                "owner",
            ),
            1,
        )
        reader = sqlite3.connect(readonly_sqlite_uri(database), uri=True)
        try:
            reader.execute("PRAGMA query_only=ON")
            reader.execute("BEGIN")
            assert reader.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0

            state.finalize_graph(2)

            assert reader.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
            reader.commit()
            assert reader.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        finally:
            reader.close()


def test_set_oriented_resolver_preserves_union_and_module_semantics(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resolver.sqlite3"
    symbol_specs = (
        ("alpha", "pkg.alpha", "function"),
        ("short", "pkg.short", "function"),
        ("other", "wanted.q", "function"),
        ("wanted", "other.wanted", "function"),
        ("dup-q-a", "dup.q", "function"),
        ("dup-q-b", "dup.q", "function"),
        ("common", "common.one", "function"),
        ("common", "common.two", "function"),
        ("unique_mod", "unique_mod", "module"),
        ("dup_mod", "dup_mod.one", "module"),
        ("dup_mod", "dup_mod.two", "module"),
        ("not_module", "not_module", "function"),
    )
    reference_specs = (
        ("same-union", "call", "alpha", "pkg.alpha"),
        ("qualified-only", "inherits", "absent", "pkg.short"),
        ("name-only", "implements_trait", "short", "absent.q"),
        ("same-union-decorator", "decorator", "alpha", "pkg.alpha"),
        ("distinct-union", "call", "wanted", "wanted.q"),
        ("duplicate-qualified", "call", "absent", "dup.q"),
        ("duplicate-qualified-with-name-hit", "call", "dup-q-a", "dup.q"),
        ("duplicate-name", "call", "common", "absent.q"),
        ("duplicate-name-with-qualified-hit", "call", "common", "common.one"),
        ("missing", "call", "absent", "absent.q"),
        ("unsupported-kind", "read", "alpha", "pkg.alpha"),
    )
    references = tuple(
        ReferenceRecord(
            kind,
            name,
            SourceRange(1, offset, 1, offset + 1, offset, offset + 1),
            target_hint=target_hint,
            confirmed=True,
            confidence=1.0,
            evidence=label,
        )
        for offset, (label, kind, name, target_hint) in enumerate(reference_specs)
    )
    dependencies = (
        DependencyRecord("unique_mod", "python_import", evidence="unique"),
        DependencyRecord("unique_mod", "python_relative_import", evidence="relative"),
        DependencyRecord("dup_mod", "python_import", evidence="ambiguous"),
        DependencyRecord(
            "same_version_mod", "python_import", evidence="same-version-ambiguous"
        ),
        DependencyRecord("not_module", "python_import", evidence="non-module"),
        DependencyRecord("missing", "python_relative_import", evidence="missing-a"),
        DependencyRecord("missing", "python_relative_import", evidence="missing-b"),
    )

    with CodeState(database) as state:
        for file_id, (name, qualified_name, kind) in enumerate(symbol_specs, 100):
            symbol = SymbolRecord(
                kind,
                name,
                qualified_name,
                None,
                _source_range(name),
                visibility="public",
            )
            state.store_analysis(
                _analysis(
                    _snapshot(
                        tmp_path / f"symbol-{file_id}.py",
                        file_id=file_id,
                        text=qualified_name,
                    ),
                    qualified_name,
                    symbols=(symbol,),
                ),
                1,
            )
        state.store_analysis(
            _analysis(
                _snapshot(
                    tmp_path / "same-version-modules.py",
                    file_id=150,
                    text="same-version-modules",
                ),
                "same-version-modules",
                symbols=(
                    SymbolRecord(
                        "module",
                        "same_version_mod",
                        "same_version_mod.one",
                        None,
                        SourceRange(1, 0, 1, 1, 0, 1),
                    ),
                    SymbolRecord(
                        "module",
                        "same_version_mod",
                        "same_version_mod.two",
                        None,
                        SourceRange(1, 2, 1, 3, 2, 3),
                    ),
                ),
            ),
            1,
        )
        caller_version, _ = state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "caller.py", file_id=200, text="caller"),
                "caller",
                references=references,
                dependencies=dependencies,
            ),
            1,
        )
        suppressor_version, _ = state.store_analysis(
            _analysis(
                _snapshot(
                    tmp_path / "preexisting.py", file_id=201, text="preexisting"
                ),
                "preexisting",
                dependencies=(
                    DependencyRecord(
                        "preexisting",
                        "python_relative_import",
                        evidence="preexisting",
                    ),
                ),
            ),
            1,
        )
        state.connection.execute(
            """INSERT INTO diagnostics(
            version_id,source,code,severity,message,tool_name,tool_version,
            confirmed,confidence,metadata_json)
            VALUES(?,'external-fixture','unresolved_relative_import','warning',
            'preexisting','fixture','1',1,1.0,'{}')""",
            (suppressor_version,),
        )

        state._resolve_symbols_and_dependencies()
        first = tuple(
            tuple(row)
            for row in state.connection.execute(
                """SELECT r.evidence,s.qualified_name,r.target_version_id
                FROM code_references r LEFT JOIN symbols s
                    ON s.symbol_id=r.target_symbol_id
                WHERE r.version_id=? ORDER BY r.reference_id""",
                (caller_version,),
            )
        )
        dependency_rows = {
            str(row[0]): row[1]
            for row in state.connection.execute(
                """SELECT evidence,resolved_version_id FROM dependencies
                WHERE version_id=?""",
                (caller_version,),
            )
        }
        diagnostic_rows = state.connection.execute(
            """SELECT code,COUNT(*) FROM diagnostics WHERE version_id=?
            AND source='neocortex-project-resolver' GROUP BY code""",
            (caller_version,),
        ).fetchall()
        suppressed_diagnostics = int(
            state.connection.execute(
                """SELECT COUNT(*) FROM diagnostics WHERE version_id=?
                AND source='neocortex-project-resolver'
                AND code='unresolved_relative_import'""",
                (suppressor_version,),
            ).fetchone()[0]
        )

        state._resolve_symbols_and_dependencies()
        second = tuple(
            tuple(row)
            for row in state.connection.execute(
                """SELECT r.evidence,s.qualified_name,r.target_version_id
                FROM code_references r LEFT JOIN symbols s
                    ON s.symbol_id=r.target_symbol_id
                WHERE r.version_id=? ORDER BY r.reference_id""",
                (caller_version,),
            )
        )
        temporary_tables = state.connection.execute(
            """SELECT name FROM sqlite_temp_master
            WHERE name LIKE '_nc_%' ORDER BY name"""
        ).fetchall()

    by_evidence = {str(row[0]): row[1] for row in first}
    assert by_evidence == {
        "same-union": "pkg.alpha",
        "qualified-only": "pkg.short",
        "name-only": "pkg.short",
        "same-union-decorator": "pkg.alpha",
        "distinct-union": None,
        "duplicate-qualified": None,
        "duplicate-qualified-with-name-hit": None,
        "duplicate-name": None,
        "duplicate-name-with-qualified-hit": None,
        "missing": None,
        "unsupported-kind": None,
    }
    assert all(row[2] is not None for row in first[:4])
    assert all(row[2] is None for row in first[4:])
    assert dependency_rows["unique"] is not None
    assert dependency_rows["relative"] == dependency_rows["unique"]
    assert dependency_rows["ambiguous"] is None
    assert dependency_rows["same-version-ambiguous"] is None
    assert dependency_rows["non-module"] is None
    assert dependency_rows["missing-a"] is None
    assert dependency_rows["missing-b"] is None
    assert [tuple(row) for row in diagnostic_rows] == [
        ("unresolved_relative_import", 1)
    ]
    assert suppressed_diagnostics == 0
    assert second == first
    assert temporary_tables == []


def test_set_oriented_resolver_preserves_historical_binding_rules(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resolver-history.sqlite3"
    keep_symbol = SymbolRecord(
        "function", "keep", "pkg.keep", None, _source_range("keep")
    )
    drop_symbol = SymbolRecord(
        "function", "drop", "pkg.drop", None, _source_range("drop")
    )
    module_symbol = SymbolRecord(
        "module", "oldmod", "oldmod", None, _source_range("oldmod")
    )
    references = (
        ReferenceRecord(
            "call", "keep", _source_range("keep"), target_hint="pkg.keep",
            evidence="keep",
        ),
        ReferenceRecord(
            "call", "drop", _source_range("drop"), target_hint="pkg.drop",
            evidence="drop",
        ),
    )
    dependency = DependencyRecord("oldmod", "python_import", evidence="oldmod")

    with CodeState(database) as state:
        keep_version, _ = state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "keep.py", file_id=500, text="keep"),
                "keep",
                symbols=(keep_symbol,),
            ),
            1,
        )
        drop_v1, _ = state.store_analysis(
            _analysis(
                _snapshot(
                    tmp_path / "drop.py", file_id=501, text="drop", mtime_ns=1
                ),
                "drop",
                symbols=(drop_symbol,),
            ),
            1,
        )
        module_v1, _ = state.store_analysis(
            _analysis(
                _snapshot(
                    tmp_path / "oldmod.py", file_id=502, text="oldmod", mtime_ns=1
                ),
                "oldmod",
                symbols=(module_symbol,),
            ),
            1,
        )
        caller_v1, _ = state.store_analysis(
            _analysis(
                _snapshot(
                    tmp_path / "caller-history.py",
                    file_id=503,
                    text="caller-v1",
                    mtime_ns=1,
                ),
                "caller-v1",
                references=references,
                dependencies=(dependency,),
            ),
            1,
        )
        state._resolve_symbols_and_dependencies()
        drop_v2, _ = state.store_analysis(
            _analysis(
                _snapshot(
                    tmp_path / "caller-history.py",
                    file_id=503,
                    text="caller-v2",
                    mtime_ns=2,
                ),
                "caller-v2",
            ),
            2,
        )
        state.store_analysis(
            _analysis(
                _snapshot(
                    tmp_path / "drop.py", file_id=501, text="drop-v2", mtime_ns=2
                ),
                "drop-v2",
                symbols=(drop_symbol,),
            ),
            2,
        )
        state.store_analysis(
            _analysis(
                _snapshot(
                    tmp_path / "oldmod.py",
                    file_id=502,
                    text="oldmod-v2",
                    mtime_ns=2,
                ),
                "oldmod-v2",
                symbols=(module_symbol,),
            ),
            2,
        )
        state.connection.execute(
            """UPDATE code_references SET target_version_id=?
            WHERE version_id=? AND evidence='keep'""",
            (drop_v2, caller_v1),
        )
        state._resolve_symbols_and_dependencies()
        historical_references = {
            str(row[0]): (row[1], row[2])
            for row in state.connection.execute(
                """SELECT evidence,target_symbol_id,target_version_id
                FROM code_references WHERE version_id=?""",
                (caller_v1,),
            )
        }
        historical_dependency = state.connection.execute(
            """SELECT resolved_version_id FROM dependencies
            WHERE version_id=? AND evidence='oldmod'""",
            (caller_v1,),
        ).fetchone()[0]

    assert historical_references["keep"][0] is not None
    assert int(historical_references["keep"][1]) == keep_version
    assert historical_references["drop"] == (None, None)
    assert historical_dependency is None
    assert drop_v1 != keep_version
    assert module_v1 != keep_version


def test_set_oriented_resolver_rolls_back_and_drops_temporary_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resolver-rollback.sqlite3"
    reference = ReferenceRecord(
        "call",
        "missing",
        _source_range("missing"),
        target_hint="pkg.missing",
        evidence="rollback",
    )
    with CodeState(database) as state:
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "rollback.py", file_id=300, text="missing"),
                "missing",
                references=(reference,),
            ),
            1,
        )
        state.connection.execute(
            """CREATE TRIGGER inject_resolver_failure
            BEFORE UPDATE ON code_references
            BEGIN SELECT RAISE(ABORT,'injected resolver failure'); END"""
        )
        before = state.connection.execute(
            """SELECT target_symbol_id,target_version_id
            FROM code_references"""
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="injected resolver failure"):
            with state.connection:
                state._resolve_symbols_and_dependencies()
        after = state.connection.execute(
            """SELECT target_symbol_id,target_version_id
            FROM code_references"""
        ).fetchall()
        temporary_tables = state.connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE '_nc_%'"
        ).fetchall()

    assert after == before
    assert temporary_tables == []


def test_set_oriented_resolver_update_plans_are_not_correlated(
    tmp_path: Path,
) -> None:
    class PlanningConnection:
        def __init__(self, connection: sqlite3.Connection):
            self.connection = connection
            self.plans: list[tuple[str, ...]] = []

        def execute(
            self,
            statement: str,
            parameters: tuple[object, ...] = (),
        ) -> sqlite3.Cursor:
            if (
                "FROM _nc_reference_targets AS t" in statement
                or "FROM _nc_current_versions AS v, _nc_module_lookup AS m"
                in statement
            ):
                plan = self.connection.execute(
                    "EXPLAIN QUERY PLAN " + statement, parameters
                ).fetchall()
                self.plans.append(tuple(str(row[3]) for row in plan))
            return self.connection.execute(statement, parameters)

        def close(self) -> None:
            self.connection.close()

    database = tmp_path / "resolver-plan.sqlite3"
    symbol = SymbolRecord(
        "module", "target", "pkg.target", None, _source_range("target")
    )
    reference = ReferenceRecord(
        "call", "target", _source_range("target"), target_hint="pkg.target"
    )
    dependency = DependencyRecord("target", "python_import")
    with CodeState(database) as state:
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "target.py", file_id=400, text="target"),
                "target",
                symbols=(symbol,),
            ),
            1,
        )
        state.store_analysis(
            _analysis(
                _snapshot(tmp_path / "plan.py", file_id=401, text="plan"),
                "plan",
                references=(reference,),
                dependencies=(dependency,),
            ),
            1,
        )
        wrapped = PlanningConnection(state.connection)
        state.connection = wrapped  # type: ignore[assignment]
        state._resolve_symbols_and_dependencies()

        assert len(wrapped.plans) == 2
        assert not any(
            "CORRELATED" in detail.upper()
            for plan in wrapped.plans
            for detail in plan
        )
# endregion [02]
