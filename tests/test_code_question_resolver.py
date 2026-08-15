from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.code_question_resolver as resolver_module
import _04_Nucleo_Operativo.code_review as review_module
from _04_Nucleo_Operativo.code_interface_surface_analysis import (
    CLI_SURFACE_QUESTION,
    interface_surface_questions,
    read_code_interface_surface_analysis,
)
from _04_Nucleo_Operativo.code_question_resolver import (
    code_question_reader_registry,
    compare_code_question_parity,
    resolve_code_question,
)
from _04_Nucleo_Operativo.self_analysis_status import quiescent_sqlite_database
from tests.test_code_review import PROCESSING_SIGNATURE, _build_state, _status


def _state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    state_directory = tmp_path / "state"
    database = _build_state(state_directory, hotspots=False)
    monkeypatch.setattr(
        resolver_module,
        "read_self_analysis_status",
        lambda _state_directory, _latest_run: _status(tmp_path),
    )
    return state_directory, database


def _canonical_cli_evaluations(
    database: Path,
    *,
    limit: int,
    rank_offset: int,
):
    with quiescent_sqlite_database(database) as connection:
        analysis = read_code_interface_surface_analysis(
            connection,
            analysis_run_id=1,
            processing_signature=PROCESSING_SIGNATURE,
            database=str(database),
            limit=limit,
        )
    _specs, evaluations = interface_surface_questions(
        analysis,
        snapshot_freshness="current",
        rank_offset=rank_offset,
    )
    return tuple(
        item for item in evaluations if item.question_id == CLI_SURFACE_QUESTION.question_id
    )


def test_static_cli_reader_is_focal_and_never_calls_global_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, _database = _state(tmp_path, monkeypatch)

    def forbidden_global_review(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("global review must not be materialized by a focal reader")

    monkeypatch.setattr(review_module, "review_code_state", forbidden_global_review)

    result = resolve_code_question(
        state_directory,
        CLI_SURFACE_QUESTION.question_id,
        limit=10,
    )

    assert result.status == "ready"
    assert result.question_version == CLI_SURFACE_QUESTION.version
    assert result.reader_id == "code-interface-static-cli-question-reader"
    assert result.source_surface == "interface_surface"
    assert result.source_digest is not None
    assert result.total_matches == len(result.evaluations) == 1
    assert result.truncated is False
    assert result.fallback is None
    assert result.evaluations[0].question_id == CLI_SURFACE_QUESTION.question_id
    assert "focal_resolution_reads_interface_projection_without_global_review" in (
        result.limitations
    )


def test_static_cli_reader_has_exact_normalized_parity_with_canonical_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, database = _state(tmp_path, monkeypatch)
    result = resolve_code_question(
        state_directory,
        CLI_SURFACE_QUESTION.question_id,
        limit=10,
    )
    canonical = _canonical_cli_evaluations(
        database,
        limit=10,
        rank_offset=41,
    )

    parity = compare_code_question_parity(result, canonical)

    assert parity.status == "matched"
    assert parity.reason is None
    assert parity.resolver_evaluation_ids == parity.canonical_evaluation_ids
    assert parity.resolver_evaluation_digests == parity.canonical_evaluation_digests


def test_parity_detects_payload_tampering_even_when_evaluation_id_is_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, database = _state(tmp_path, monkeypatch)
    result = resolve_code_question(
        state_directory,
        CLI_SURFACE_QUESTION.question_id,
        limit=10,
    )
    canonical = _canonical_cli_evaluations(database, limit=10, rank_offset=0)
    tampered = replace(
        canonical[0],
        decision_reason="tampered_without_changing_the_evaluation_id",
    )

    parity = compare_code_question_parity(result, (tampered,))

    assert parity.status == "mismatched"
    assert parity.missing_evaluation_ids == ()
    assert parity.unexpected_evaluation_ids == ()
    assert parity.resolver_evaluation_digests != parity.canonical_evaluation_digests


def test_parity_binds_each_payload_digest_to_its_ordered_evaluation_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, _database = _state(tmp_path, monkeypatch)
    original = resolve_code_question(
        state_directory,
        CLI_SURFACE_QUESTION.question_id,
        limit=10,
    )
    base = original.evaluations[0]
    first = replace(
        base,
        evaluation_id="evaluation-a",
        decision_reason="payload-a",
    )
    second = replace(
        base,
        evaluation_id="evaluation-b",
        decision_reason="payload-b",
    )
    focal = replace(
        original,
        total_matches=2,
        evaluations=(first, second),
    )
    canonical_with_swapped_payloads = (
        replace(first, decision_reason="payload-b"),
        replace(second, decision_reason="payload-a"),
    )

    parity = compare_code_question_parity(
        focal,
        canonical_with_swapped_payloads,
    )

    assert parity.resolver_evaluation_ids == parity.canonical_evaluation_ids
    assert sorted(parity.resolver_evaluation_digests) == sorted(parity.canonical_evaluation_digests)
    assert parity.resolver_evaluation_digests != parity.canonical_evaluation_digests
    assert parity.status == "mismatched"


def test_unsupported_question_returns_nonautomatic_fallback_without_reading_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resolver_module,
        "read_self_analysis_status",
        lambda *_args: pytest.fail("unsupported questions must not read state"),
    )

    result = resolve_code_question(
        tmp_path / "missing",
        "architecture.unregistered_question",
    )

    assert result.status == "unsupported"
    assert result.source_surface == "review"
    assert result.evaluations == ()
    assert result.fallback is not None
    assert result.fallback.automatic is False
    assert result.fallback.fallback_kind == "code_analysis_query"
    assert result.fallback.question_id == "architecture.unregistered_question"


def test_registry_is_exact_bounded_and_shared_by_cli_and_knowledge() -> None:
    registry = code_question_reader_registry()

    assert len(registry) == 1
    assert registry[0].question_id == CLI_SURFACE_QUESTION.question_id
    assert registry[0].question_version == CLI_SURFACE_QUESTION.version
    assert registry[0].consumer_interfaces == ("cli", "knowledge")
    assert registry[0].max_results == 50


@pytest.mark.parametrize("limit", [0, 51, True])
def test_question_resolution_rejects_unbounded_limits(
    tmp_path: Path,
    limit: int,
) -> None:
    with pytest.raises(ValueError, match="between 1 and 50"):
        resolve_code_question(
            tmp_path,
            CLI_SURFACE_QUESTION.question_id,
            limit=limit,
        )
