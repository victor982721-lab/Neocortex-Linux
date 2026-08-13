from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.code_review as code_review_module
from _04_Nucleo_Operativo.code_analysis_query import (
    CodeAnalysisQuery,
    query_code_analysis,
)
from _04_Nucleo_Operativo.code_class_surface_analysis import (
    CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD,
    CLASS_SPAN_LINES_ATTENTION_THRESHOLD,
    CodeClassSurfaceEvidenceResolutionError,
    parse_code_class_surface_payload,
    read_code_class_surface_analysis,
    validate_code_class_surface_observation,
)
from _04_Nucleo_Operativo.code_contracts import SourceRange, SymbolRecord
from _04_Nucleo_Operativo.code_review import review_code_state
from _04_Nucleo_Operativo.code_schema import (
    checkpoint_code_wal,
    readonly_code_database,
    remove_checkpointed_code_sidecars,
)
from _04_Nucleo_Operativo.code_state import CodeState
from tests.test_code_review import PROCESSING_SIGNATURE, _analysis, _status


def _class_symbols(
    qualified_name: str,
    *,
    span_lines: int,
    methods: int,
    bases: tuple[str, ...] = (),
) -> tuple[SymbolRecord, ...]:
    name = qualified_name.rsplit(".", 1)[-1]
    class_range = SourceRange(1, 0, span_lines, 0, 0, span_lines * 10)
    symbols: list[SymbolRecord] = [
        SymbolRecord(
            "class",
            name,
            qualified_name,
            f"class {name}",
            class_range,
            visibility="public",
            metadata={"bases": bases, "decorators": ()},
        )
    ]
    visibilities = ("public", "private", "special")
    for index in range(methods):
        method_name = f"method_{index}"
        line = min(2 + index, span_lines)
        symbols.append(
            SymbolRecord(
                "method",
                method_name,
                f"{qualified_name}.{method_name}",
                f"{method_name}()",
                SourceRange(line, 4, line, 20, 40 + index * 20, 55 + index * 20),
                parent_qualified_name=qualified_name,
                visibility=visibilities[index % len(visibilities)],
                complexity=1,
                metadata={"async": False, "decorators": (), "return_annotation": None},
            )
        )
    return tuple(symbols)


def _build_class_state(state_directory: Path) -> Path:
    state_directory.mkdir(parents=True)
    database = state_directory / "code.sqlite3"
    fixtures = (
        (
            Path("service.py"),
            810,
            "pkg.NeutralSurface",
            CLASS_SPAN_LINES_ATTENTION_THRESHOLD,
            1,
            (),
        ),
        (
            Path("result_repository.py"),
            811,
            "pkg.BuildCommitRepository",
            100,
            CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD,
            (),
        ),
        (
            Path("tests") / "test_protocol.py",
            812,
            "tests.DeclarativeProtocol",
            CLASS_SPAN_LINES_ATTENTION_THRESHOLD,
            1,
            ("Protocol",),
        ),
        (
            Path("small.py"),
            813,
            "pkg.SmallClass",
            CLASS_SPAN_LINES_ATTENTION_THRESHOLD - 1,
            CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD - 1,
            (),
        ),
    )
    with CodeState(database) as state:
        run_id = state.begin_run(1, 1, PROCESSING_SIGNATURE)
        for path, identity, qualified_name, span, method_count, bases in fixtures:
            state.store_analysis(
                _analysis(
                    state_directory / path,
                    identity,
                    symbols=_class_symbols(
                        qualified_name,
                        span_lines=span,
                        methods=method_count,
                        bases=bases,
                    ),
                ),
                run_id,
            )
        state.finalize_graph(run_id)
        state.complete_run(
            run_id,
            {
                "candidates": len(fixtures),
                "processed": len(fixtures),
                "cache_hits": 0,
                "errors": 0,
            },
            partial=False,
            graph_current=True,
        )
        checkpoint_code_wal(state.connection)
    remove_checkpointed_code_sidecars(database)
    return database


def test_class_surface_selection_uses_structure_not_names_or_paths(tmp_path: Path) -> None:
    database = _build_class_state(tmp_path / "state")

    with readonly_code_database(database) as connection:
        analysis = read_code_class_surface_analysis(
            connection,
            snapshot_id=PROCESSING_SIGNATURE,
            snapshot_freshness="current",
            limit=10,
        )

    assert analysis.eligible_classes == 4
    assert analysis.selected_classes == analysis.returned_classes == 3
    assert analysis.selection_truncated is False
    by_name = {item.qualified_name: item for item in analysis.observations}
    assert "pkg.SmallClass" not in by_name
    assert by_name["pkg.NeutralSurface"].selection_signals == (
        "class_span_attention_threshold_met",
    )
    assert by_name["pkg.BuildCommitRepository"].selection_signals == (
        "direct_method_attention_threshold_met",
    )
    assert by_name["tests.DeclarativeProtocol"].selection_signals == (
        "class_span_attention_threshold_met",
    )
    assert (
        by_name["tests.DeclarativeProtocol"].span_lines == by_name["pkg.NeutralSurface"].span_lines
    )
    assert (
        by_name["tests.DeclarativeProtocol"].direct_methods
        == by_name["pkg.NeutralSurface"].direct_methods
    )
    assert all(item.authority == "advisory" for item in analysis.observations)
    assert not any(item.mutation_authority for item in analysis.observations)


def test_class_surface_question_preserves_protocol_counterexample_and_abstains_from_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _build_class_state(state_directory)
    monkeypatch.setattr(
        code_review_module,
        "read_self_analysis_status",
        lambda _state, _run: _status(tmp_path),
    )

    result = review_code_state(state_directory, limit=10)
    payload = result.as_payload()

    assert payload["schema"] == "neocortex.code-review/v16"
    assert result.findings == ()
    assert result.structural_analysis is not None
    assert result.structural_analysis.selected_classes == 3
    class_evaluations = tuple(
        item for item in result.question_evaluations if item.subject.subject_kind == "class"
    )
    assert len(class_evaluations) == 3
    assert all(item.inference_status == "abstained" for item in class_evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in class_evaluations)
    assert all(item.decision is None for item in class_evaluations)
    assert result.recommendations == ()
    assert result.work_packages == ()
    query = query_code_analysis(
        json.loads(json.dumps(payload)),
        CodeAnalysisQuery(
            surface="review",
            categories=("maintenance.class_surface_requires_change",),
        ),
    )
    assert query["counts"]["matched"] == 3


def test_class_surface_source_resolver_rejects_changed_direct_members(tmp_path: Path) -> None:
    database = _build_class_state(tmp_path / "state")
    with readonly_code_database(database) as connection:
        analysis = read_code_class_surface_analysis(
            connection,
            snapshot_id=PROCESSING_SIGNATURE,
            snapshot_freshness="current",
            limit=10,
        )
    target = next(
        item for item in analysis.observations if item.qualified_name == "pkg.BuildCommitRepository"
    )
    with sqlite3.connect(database) as connection:
        child = connection.execute(
            "SELECT symbol_id FROM symbols WHERE parent_symbol_id=? ORDER BY symbol_id LIMIT 1",
            (target.symbol_id,),
        ).fetchone()
        assert child is not None
        connection.execute("UPDATE symbols SET confirmed=0 WHERE symbol_id=?", (child[0],))
        connection.commit()
        checkpoint_code_wal(connection)
    remove_checkpointed_code_sidecars(database)

    with readonly_code_database(database) as connection:
        with pytest.raises(
            CodeClassSurfaceEvidenceResolutionError,
            match="disagrees with its source symbols",
        ):
            validate_code_class_surface_observation(connection, target)


def test_class_surface_wire_rejects_smuggled_semantics(tmp_path: Path) -> None:
    database = _build_class_state(tmp_path / "state")
    with readonly_code_database(database) as connection:
        analysis = read_code_class_surface_analysis(
            connection,
            snapshot_id=PROCESSING_SIGNATURE,
            snapshot_freshness="current",
            limit=10,
        )
    payload = json.loads(json.dumps(analysis.as_payload()))
    payload["observations"][0]["limitations"].append("delete_this_class_now")

    with pytest.raises(ValueError, match="limitations are not canonical"):
        parse_code_class_surface_payload(payload)
