"""Flat CLI, exit-code and non-creation contracts for Knowledge commands."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_knowledge.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.cli_knowledge as cli_knowledge
import _04_Nucleo_Operativo.knowledge_snapshot as knowledge_snapshot
from _04_Nucleo_Operativo.cli_app import main
from _04_Nucleo_Operativo.cli_operations import selected_direct_operations
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from _04_Nucleo_Operativo.knowledge_context import build_context_bundle
from _04_Nucleo_Operativo.knowledge_contracts import (
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    SnapshotConsistency,
)
from _04_Nucleo_Operativo.knowledge_planner import KnowledgeQuery, plan_knowledge_query
from _04_Nucleo_Operativo.knowledge_search import KnowledgeSearchResult
# endregion [01]

# region [02] Implementación


def _snapshot(
    state: OwnerAvailability = OwnerAvailability.ABSENT,
    *,
    consistency: SnapshotConsistency = SnapshotConsistency.STABLE,
) -> KnowledgeSnapshot:
    snapshot_changed = consistency is SnapshotConsistency.SNAPSHOT_CHANGED
    effective_state = OwnerAvailability.AVAILABLE if snapshot_changed else state
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T03:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot(
                "pdf",
                effective_state,
                11,
                11 if effective_state is OwnerAvailability.AVAILABLE else None,
                data_version_before=1 if snapshot_changed else None,
                data_version_after=2 if snapshot_changed else None,
            ),
        ),
        consistency=consistency,
        attempts=2 if snapshot_changed else 1,
    )


def _empty_result(
    snapshot: KnowledgeSnapshot,
    *,
    complete: bool,
    blocking_owners: tuple[str, ...] = (),
) -> KnowledgeSearchResult:
    plan = plan_knowledge_query(KnowledgeQuery("relay protection"))
    return KnowledgeSearchResult(
        plan,
        snapshot,
        (),
        (),
        complete,
        False,
        0,
        0,
        0,
        1,
        blocking_owners=blocking_owners,
    )


def test_knowledge_json_escapes_unencodable_corpus_text_on_windows_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StrictCp1252Console:
        encoding = "cp1252"

        def __init__(self) -> None:
            self.parts: list[str] = []

        def write(self, value: str) -> int:
            value.encode(self.encoding)
            self.parts.append(value)
            return len(value)

        def flush(self) -> None:
            return None

        def getvalue(self) -> str:
            return "".join(self.parts)

    result = replace(
        _empty_result(_snapshot(), complete=True),
        plan=plan_knowledge_query(KnowledgeQuery("relay \uf0b7 protection")),
    )

    class Service:
        def search(self, *_args: object, **_kwargs: object) -> KnowledgeSearchResult:
            return result

    monkeypatch.setattr(cli_knowledge, "_service", lambda _args: Service())
    monkeypatch.setattr(
        cli_knowledge,
        "_with_cancellation",
        lambda operation: operation(lambda: None),
    )
    console = StrictCp1252Console()
    monkeypatch.setattr(sys, "stdout", console)
    args = build_parser().parse_args(("--knowledge-search", "relay", "--knowledge-json"))

    code = cli_knowledge.run_knowledge_search(args)

    assert code == int(cli_knowledge.KnowledgeExitCode.NO_RESULTS)
    assert "\\uf0b7" in console.getvalue()
    assert json.loads(console.getvalue())["plan"]["normalized_query"] == ("relay \uf0b7 protection")


def test_parser_selects_three_lazy_flat_operations_with_bounded_defaults() -> None:
    parser = build_parser()
    status = parser.parse_args(("--knowledge-status",))
    search = parser.parse_args(("--knowledge-search", "relay"))
    context = parser.parse_args(("--knowledge-context", "relay"))

    assert status.knowledge_limit == 20
    assert status.knowledge_context_characters == 12_000
    assert status.knowledge_mode == "evidence"
    assert "--knowledge-context-characters N" in parser.format_help()
    assert tuple(item.destination for item in selected_direct_operations(status)) == (
        "knowledge_status",
    )
    assert tuple(item.destination for item in selected_direct_operations(search)) == (
        "knowledge_search",
    )
    assert tuple(item.destination for item in selected_direct_operations(context)) == (
        "knowledge_context",
    )


@pytest.mark.parametrize(
    "arguments",
    (
        ("--knowledge-limit", "3"),
        ("--knowledge-status", "--knowledge-search", "relay"),
        ("--knowledge-search", " "),
        ("--knowledge-search", "relay", "--knowledge-limit", "0"),
        ("--knowledge-search", "relay", "--knowledge-limit", "1001"),
        ("--knowledge-status", "--knowledge-history"),
        ("--knowledge-status", "--apply"),
        ("--knowledge-search", "relay", "--route", "pdf"),
    ),
)
def test_validation_rejects_ambiguous_or_mutating_combinations(
    arguments: tuple[str, ...],
) -> None:
    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit):
        validate_arguments(args)


@pytest.mark.parametrize(
    ("operation", "limit"),
    (
        ("--knowledge-search", "1000"),
        ("--knowledge-context", "100"),
    ),
)
def test_query_limits_preserve_the_search_and_context_bounds(
    operation: str,
    limit: str,
) -> None:
    args = build_parser().parse_args((operation, "relay protection", "--knowledge-limit", limit))

    validate_arguments(args)

    assert args.knowledge_limit == int(limit)


def test_context_limit_above_builder_bound_is_usage_error_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatched: list[object] = []

    def handler(args: object) -> int:
        dispatched.append(args)
        return 0

    monkeypatch.setattr(cli_knowledge, "run_knowledge_context", handler)

    with pytest.raises(SystemExit) as raised:
        main(
            (
                "--knowledge-context",
                "relay protection",
                "--knowledge-limit",
                "101",
            )
        )

    assert raised.value.code == int(cli_knowledge.KnowledgeExitCode.USAGE)
    assert "between 1 and 100 for --knowledge-context" in capsys.readouterr().err
    assert dispatched == []


@pytest.mark.parametrize("characters", (1, 1_000_000))
def test_context_character_budget_accepts_its_documented_bounds(
    characters: int,
) -> None:
    args = build_parser().parse_args(
        (
            "--knowledge-context",
            "relay protection",
            "--knowledge-context-characters",
            str(characters),
        )
    )

    validate_arguments(args)

    assert args.knowledge_context_characters == characters


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            (
                "--knowledge-context",
                "relay protection",
                "--knowledge-context-characters",
                "0",
            ),
            "must be between 1 and 1000000",
        ),
        (
            (
                "--knowledge-context",
                "relay protection",
                "--knowledge-context-characters",
                "1000001",
            ),
            "must be between 1 and 1000000",
        ),
        (
            (
                "--knowledge-search",
                "relay protection",
                "--knowledge-context-characters",
                "12000",
            ),
            "requires --knowledge-context",
        ),
    ),
)
def test_context_character_budget_errors_before_dispatch(
    arguments: tuple[str, ...],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatched: list[object] = []

    def handler(args: object) -> int:
        dispatched.append(args)
        return 0

    monkeypatch.setattr(cli_knowledge, "run_knowledge_search", handler)
    monkeypatch.setattr(cli_knowledge, "run_knowledge_context", handler)

    with pytest.raises(SystemExit) as raised:
        main(arguments)

    assert raised.value.code == int(cli_knowledge.KnowledgeExitCode.USAGE)
    assert message in capsys.readouterr().err
    assert dispatched == []


def test_context_handler_forwards_the_character_budget(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}

    class Bundle:
        def to_json(self) -> str:
            return '{"schema_version":1}'

    class Service:
        def context(self, query: KnowledgeQuery, **kwargs: object) -> Bundle:
            observed["query"] = query
            observed.update(kwargs)
            return Bundle()

    def run_immediately(operation):
        return operation(lambda: None)

    monkeypatch.setattr(cli_knowledge, "_service", lambda _args: Service())
    monkeypatch.setattr(cli_knowledge, "_with_cancellation", run_immediately)
    monkeypatch.setattr(
        cli_knowledge,
        "knowledge_context_exit_code",
        lambda _bundle: cli_knowledge.KnowledgeExitCode.SUCCESS,
    )
    args = build_parser().parse_args(
        (
            "--knowledge-context",
            "relay protection",
            "--knowledge-context-characters",
            "34567",
            "--knowledge-json",
        )
    )
    validate_arguments(args)

    code = cli_knowledge.run_knowledge_context(args)

    assert code == int(cli_knowledge.KnowledgeExitCode.SUCCESS)
    assert observed["max_characters"] == 34_567
    assert observed["max_hits"] == 20
    assert callable(observed["cancellation_check"])
    assert json.loads(capsys.readouterr().out) == {"schema_version": 1}


def test_absent_status_json_is_successful_and_creates_no_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "absent"

    code = main(
        (
            "--state-directory",
            str(state),
            "--knowledge-status",
            "--knowledge-json",
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == int(cli_knowledge.KnowledgeExitCode.SUCCESS)
    assert payload["kind"] == "knowledge_snapshot"
    assert not state.exists()


@pytest.mark.parametrize(
    "operation",
    (
        ("--knowledge-status",),
        ("--knowledge-search", "relay protection"),
        ("--knowledge-context", "relay protection"),
    ),
)
def test_existing_non_directory_state_root_is_fatal_not_absent_success(
    tmp_path: Path,
    operation: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state-file"
    original = b"not a state directory"
    state.write_bytes(original)

    code = main(
        (
            "--state-directory",
            str(state),
            *operation,
            "--knowledge-json",
        )
    )

    captured = capsys.readouterr()
    assert code == int(cli_knowledge.KnowledgeExitCode.FATAL)
    assert captured.out == ""
    assert "KnowledgeStateRootError" in captured.err
    assert "is not a directory" in captured.err
    assert state.read_bytes() == original


@pytest.mark.parametrize(
    "operation",
    (
        ("--knowledge-status",),
        ("--knowledge-search", "relay protection"),
        ("--knowledge-context", "relay protection"),
    ),
)
def test_inaccessible_state_root_is_fatal_for_every_knowledge_action(
    tmp_path: Path,
    operation: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = (tmp_path / "state").absolute()
    state.mkdir()
    real_scandir = knowledge_snapshot.os.scandir

    def deny_state_directory(path: object):
        if Path(path) == state:
            raise PermissionError(13, "access denied", str(state))
        return real_scandir(path)

    monkeypatch.setattr(knowledge_snapshot.os, "scandir", deny_state_directory)

    code = main(
        (
            "--state-directory",
            str(state),
            *operation,
            "--knowledge-json",
        )
    )

    captured = capsys.readouterr()
    assert code == int(cli_knowledge.KnowledgeExitCode.FATAL)
    assert captured.out == ""
    assert "KnowledgeStateRootError" in captured.err
    assert "is inaccessible" in captured.err


@pytest.mark.parametrize(
    "operation",
    ("--knowledge-search", "--knowledge-context"),
)
def test_absent_query_is_partial_not_false_no_results_and_creates_no_state(
    tmp_path: Path,
    operation: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / operation.removeprefix("--")

    code = main(
        (
            "--state-directory",
            str(state),
            operation,
            "relay protection",
            "--knowledge-json",
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == int(cli_knowledge.KnowledgeExitCode.PARTIAL)
    assert payload["schema_version"] == 1
    assert not state.exists()


def test_search_exit_code_precedence_is_stable() -> None:
    stable = _empty_result(_snapshot(), complete=True)
    partial = replace(stable, complete=False)
    changed = replace(
        stable,
        snapshot=_snapshot(consistency=SnapshotConsistency.SNAPSHOT_CHANGED),
    )
    future = replace(
        stable,
        snapshot=_snapshot(OwnerAvailability.FUTURE),
        blocking_owners=("pdf",),
    )
    corrupt = replace(
        stable,
        snapshot=_snapshot(OwnerAvailability.CORRUPT),
        blocking_owners=("pdf",),
    )

    assert cli_knowledge.knowledge_search_exit_code(stable) == 3
    assert cli_knowledge.knowledge_search_exit_code(partial) == 4
    assert cli_knowledge.knowledge_search_exit_code(changed) == 5
    assert cli_knowledge.knowledge_search_exit_code(future) == 6
    assert cli_knowledge.knowledge_search_exit_code(corrupt) == 7


def test_exact_read_compatible_legacy_owner_is_not_schema_incompatible() -> None:
    snapshot = KnowledgeSnapshot.create(
        source_version="0.7.2",
        captured_at_utc="2026-08-01T00:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot(
                "framework",
                OwnerAvailability.AVAILABLE,
                21,
                19,
                warning="legacy_schema_read_compatible:19->21",
            ),
        ),
    )
    result = _empty_result(snapshot, complete=True)

    assert cli_knowledge.knowledge_search_exit_code(result) == 3


def test_optional_incompatible_owner_does_not_override_query_outcome() -> None:
    snapshot = _snapshot(OwnerAvailability.INCOMPATIBLE)
    partial = _empty_result(snapshot, complete=False)
    no_evidence = _empty_result(snapshot, complete=True)
    required = _empty_result(
        snapshot,
        complete=False,
        blocking_owners=("pdf",),
    )

    assert cli_knowledge.knowledge_search_exit_code(partial) == 4
    assert cli_knowledge.knowledge_search_exit_code(no_evidence) == 3
    assert cli_knowledge.knowledge_search_exit_code(required) == 6

    optional_context = build_context_bundle(partial, character_limit=2_000)
    required_context = build_context_bundle(
        required,
        character_limit=2_000,
    )
    corrupt_context = build_context_bundle(
        _empty_result(
            _snapshot(OwnerAvailability.CORRUPT),
            complete=False,
            blocking_owners=("pdf",),
        ),
        character_limit=2_000,
    )
    assert cli_knowledge.knowledge_context_exit_code(optional_context) == 4
    assert cli_knowledge.knowledge_context_exit_code(required_context) == 6
    assert cli_knowledge.knowledge_context_exit_code(corrupt_context) == 7


def test_handler_does_not_swallow_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = build_parser().parse_args(("--knowledge-status",))

    def interrupt(_operation):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_knowledge, "_with_cancellation", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli_knowledge.run_knowledge_status(args)


# endregion [02]
