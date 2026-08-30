from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.code.code_analysis_epistemics import (
    validate_analysis_question_set,
)
from neocortex.code.code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
    CodeRouteConfig,
)
from neocortex.code.code_experiment_planner import plan_code_experiments
from neocortex.code.code_interface_surface_analysis import (
    CLI_SURFACE_QUESTION,
    CONFIGURATION_SURFACE_QUESTION,
    MODULE_SURFACE_QUESTION,
    abstained_code_interface_surface,
    interface_surface_questions,
    parse_code_interface_surface_payload,
    read_code_interface_surface_analysis,
)
from neocortex.code.code_python import PythonAnalyzer
from neocortex.code.code_schema import (
    checkpoint_code_wal,
    readonly_code_database,
    remove_checkpointed_code_sidecars,
)
from neocortex.code.code_state import CodeState
from neocortex.semantic.semantic_models import fingerprint_bytes, fingerprint_text

_SIGNATURE = "interface-surface-fixture-v1"


def _input(
    path: Path,
    text: str,
    *,
    identity: int,
    language: str,
    artifact_kind: ArtifactKind,
) -> CodeFileInput:
    raw = text.encode("utf-8")
    return CodeFileInput(
        FileSnapshot(str(path), 1, identity, len(raw), 100 + identity, 50),
        text,
        raw,
        "utf-8",
        ArtifactClassification(language, artifact_kind, 1.0, ("fixture",)),
        _SIGNATURE,
    )


def _generic_analysis(source: CodeFileInput, *, status: AnalysisStatus) -> CodeAnalysis:
    raw = fingerprint_bytes(source.raw_bytes)
    text = fingerprint_text(source.text)
    return CodeAnalysis(
        input=source,
        status=status,
        analyzer_id="fixture-config-analyzer",
        analyzer_version="1",
        parser_kind="fixture-config",
        text_xxh3_128=text.xxh3_128,
        text_xxh3_64_guard=text.xxh3_64_guard,
        normalized_xxh3_128=text.xxh3_128,
        token_xxh3_128=None,
        structure_xxh3_128=None,
        raw_xxh3_128=raw.xxh3_128,
        raw_xxh3_64_guard=raw.xxh3_64_guard,
        provenance={"fixture": True},
    )


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "code.sqlite3"
    route = CodeRouteConfig(tmp_path / "state", tmp_path / "dedup")
    large_source = "\n".join(f"value_{index} = {index}" for index in range(1_050)) + "\n"
    cli_source = """\
import argparse

def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alpha", required=True)
    parser.add_argument("input")
    dynamic_name = "--dynamic"
    parser.add_argument(dynamic_name)
    commands = parser.add_subparsers()
    commands.add_parser("run")
    return parser
"""
    json_config = json.dumps(
        {"root": {"enabled": True, "limits": [1, 2]}, "mode": "fixture"},
        sort_keys=True,
    )
    yaml_config = "root:\n  enabled: true\n"
    analyses = (
        PythonAnalyzer().analyze(
            _input(
                tmp_path / "large_module.py",
                large_source,
                identity=1,
                language="python",
                artifact_kind=ArtifactKind.SOURCE,
            ),
            route,
        ),
        PythonAnalyzer().analyze(
            _input(
                tmp_path / "cli_surface.py",
                cli_source,
                identity=2,
                language="python",
                artifact_kind=ArtifactKind.SOURCE,
            ),
            route,
        ),
        _generic_analysis(
            _input(
                tmp_path / "settings.json",
                json_config,
                identity=3,
                language="json",
                artifact_kind=ArtifactKind.CONFIG,
            ),
            status=AnalysisStatus.COMPLETE,
        ),
        _generic_analysis(
            _input(
                tmp_path / "workflow.yml",
                yaml_config,
                identity=4,
                language="yaml",
                artifact_kind=ArtifactKind.CONFIG,
            ),
            status=AnalysisStatus.TEXT_ONLY,
        ),
    )
    with CodeState(database) as state:
        run_id = state.begin_run(1, 1, _SIGNATURE)
        for analysis in analyses:
            state.store_analysis(analysis, 1)
        state.finalize_graph(1)
        state.complete_run(
            run_id,
            {"candidates": 4, "processed": 4, "cache_hits": 0, "errors": 0},
            partial=False,
            graph_current=True,
        )
        checkpoint_code_wal(state.connection)
    remove_checkpointed_code_sidecars(database)
    return database


def _analysis(tmp_path: Path):
    database = _database(tmp_path)
    with readonly_code_database(database) as connection:
        return read_code_interface_surface_analysis(
            connection,
            analysis_run_id=1,
            processing_signature=_SIGNATURE,
            database=str(database),
        )


def test_interface_surface_observes_modules_configuration_and_static_cli(
    tmp_path: Path,
) -> None:
    analysis = _analysis(tmp_path)

    assert analysis.status == "ready"
    assert analysis.total_modules == 2
    assert analysis.selected_modules == analysis.returned_modules == 1
    module = analysis.modules[0]
    assert module.path.endswith("large_module.py")
    assert module.line_count == 1_050
    assert "line_threshold" in module.selection_reasons
    assert "direct_symbol_threshold" in module.selection_reasons
    assert analysis.configuration_artifacts == 2
    assert analysis.exact_configuration_artifacts == 1
    assert analysis.incomplete_configuration_artifacts == 1
    exact = next(item for item in analysis.configurations if item.parse_status == "exact")
    assert exact.top_level_keys == 2
    assert exact.total_keys == 4
    assert exact.top_level_key_examples == ("mode", "root")
    cli = analysis.cli_files[0]
    assert cli.parse_status == "exact"
    assert cli.add_argument_calls == 3
    assert cli.literal_argument_calls == 2
    assert cli.dynamic_argument_calls == 1
    assert cli.literal_option_strings == 1
    assert cli.positional_argument_calls == 1
    assert cli.literal_subcommands == 1
    assert cli.option_examples == ("--alpha",)
    assert cli.subcommand_examples == ("run",)


def test_module_surface_aggregates_each_module_without_cross_file_count_leakage(
    tmp_path: Path,
) -> None:
    analysis = _analysis(tmp_path)

    large = analysis.modules[0]
    assert large.direct_symbols == 1_050
    assert large.public_direct_symbols == 1_050
    assert large.direct_variables == 1_050
    assert large.direct_functions == 0
    assert large.direct_classes == 0
    assert large.confirmed_dependencies == 0
    assert large.confirmed_references == 0


def test_interface_questions_never_convert_width_into_a_change_decision(tmp_path: Path) -> None:
    analysis = _analysis(tmp_path)

    specs, evaluations = interface_surface_questions(
        analysis,
        snapshot_freshness="current",
        rank_offset=7,
    )

    assert specs == (
        MODULE_SURFACE_QUESTION,
        CONFIGURATION_SURFACE_QUESTION,
        CLI_SURFACE_QUESTION,
    )
    assert tuple(item.rank for item in evaluations) == (8, 9, 10)
    assert all(item.observation_status == "confirmed" for item in evaluations)
    assert all(item.inference_status == "abstained" for item in evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in evaluations)
    assert all(item.decision is None for item in evaluations)
    validate_analysis_question_set(
        specs,
        tuple(replace(item, rank=index) for index, item in enumerate(evaluations, start=1)),
    )


def test_interface_question_revision_and_proposal_ignore_only_capture_run_identity(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with readonly_code_database(database) as connection:
        primary = read_code_interface_surface_analysis(
            connection,
            analysis_run_id=1,
            processing_signature=_SIGNATURE,
            database=str(database),
        )
        replay = read_code_interface_surface_analysis(
            connection,
            analysis_run_id=2,
            processing_signature=_SIGNATURE,
            database=str(database),
        )
        changed = read_code_interface_surface_analysis(
            connection,
            analysis_run_id=2,
            processing_signature=f"{_SIGNATURE}-changed",
            database=str(database),
        )

    assert primary.analysis_id != replay.analysis_id
    primary_specs, primary_evaluations = interface_surface_questions(
        primary,
        snapshot_freshness="current",
        rank_offset=0,
    )
    replay_specs, replay_evaluations = interface_surface_questions(
        replay,
        snapshot_freshness="current",
        rank_offset=0,
    )
    changed_specs, changed_evaluations = interface_surface_questions(
        changed,
        snapshot_freshness="current",
        rank_offset=0,
    )
    primary_cli = primary_evaluations[-1]
    replay_cli = replay_evaluations[-1]
    changed_cli = changed_evaluations[-1]
    assert primary_cli.evaluation_id != replay_cli.evaluation_id
    assert primary_cli.subject.revision_id == replay_cli.subject.revision_id
    assert primary_cli.evidence[0].evidence_id == replay_cli.evidence[0].evidence_id
    primary_proposal = next(
        item
        for item in plan_code_experiments(primary_specs, primary_evaluations).proposals
        if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    replay_proposal = next(
        item
        for item in plan_code_experiments(replay_specs, replay_evaluations).proposals
        if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    changed_proposal = next(
        item
        for item in plan_code_experiments(changed_specs, changed_evaluations).proposals
        if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    assert primary_proposal.proposal_id == replay_proposal.proposal_id
    assert primary_proposal.evaluation_binding_fingerprint == (
        replay_proposal.evaluation_binding_fingerprint
    )
    assert changed_cli.subject.revision_id != replay_cli.subject.revision_id
    assert changed_proposal.proposal_id != replay_proposal.proposal_id


def test_interface_surface_wire_is_strict_and_identity_bound(tmp_path: Path) -> None:
    analysis = _analysis(tmp_path)
    payload = json.loads(json.dumps(analysis.as_payload()))

    assert parse_code_interface_surface_payload(payload) == analysis
    payload["modules"][0]["line_count"] += 1
    with pytest.raises(ValueError, match="identity"):
        parse_code_interface_surface_payload(payload)


def test_cli_static_view_does_not_claim_effective_parser_behavior(tmp_path: Path) -> None:
    analysis = _analysis(tmp_path)
    _, evaluations = interface_surface_questions(
        analysis,
        snapshot_freshness="publication_only",
        rank_offset=0,
    )
    cli = evaluations[-1]

    requirements = {item.requirement_id: item.status for item in cli.requirements}
    assert requirements["published_static_argparse_call_projection"] == "satisfied"
    assert requirements["effective_runtime_parser_contract_observed"] == "missing"
    assert requirements["dynamic_cli_construction_counterevidence_evaluated"] == ("not_evaluated")
    assert cli.authority == "advisory"
    assert cli.mutation_authority is False


def test_unresolved_interface_provider_is_an_explicit_abstained_question() -> None:
    analysis = abstained_code_interface_surface(
        "interface_surface_unresolvable:fixture",
        database="fixture.sqlite3",
    )

    specs, evaluations = interface_surface_questions(
        analysis,
        snapshot_freshness="unknown",
        rank_offset=3,
    )

    assert tuple(item.question_id for item in specs) == (
        "structure.interface_evidence_provider_is_resolved",
    )
    assert len(evaluations) == 1
    availability = evaluations[0]
    assert availability.rank == 4
    assert availability.observation_status == "abstained"
    assert availability.question_readiness == "abstained"
    assert availability.decision_readiness == "abstained"
    assert availability.decision is None
    assert availability.evidence == ()
    assert tuple(item.status for item in availability.requirements) == (
        "missing",
        "missing",
        "not_evaluated",
        "not_evaluated",
    )
