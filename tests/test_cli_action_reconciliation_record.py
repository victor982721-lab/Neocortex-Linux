"""CLI contracts for durable, non-mutating reconciliation observations."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_action_reconciliation_record.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import snapshot_path
from neocortex.api.cli.cli_app import main as cli_main
from neocortex.workflow.actions.file_action_recovery import expected_identity_json
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run
from tests.mutation_containment import ContainedMutationRoot
# endregion [01]

# region [02] Implementación


def _create_uncertain_action(state_directory: Path, root: Path) -> tuple[int, Path, Path]:
    database = state_directory / "framework.sqlite3"
    source = root / "source-that-must-not-be-created.bin"
    target = root / "target-that-must-not-be-created.bin"
    with FrameworkState(database) as state:
        run_id = begin_signed_normal_run(state, root)
        action_id = state.begin_file_action(
            run_id,
            "move_document",
            str(source),
            str(target),
            None,
            "controlled CLI fixture",
            True,
        )
        state.require_file_action_recovery((action_id,), "legacy receipt unavailable")
    return action_id, source, target


def _action_sandbox(base: Path) -> tuple[Path, Path]:
    state_directory = base / "state"
    root = base / "corpus"
    state_directory.mkdir()
    root.mkdir()
    return state_directory, root


def _record_args(
    state_directory: Path,
    action_id: int,
    *,
    actor: str = "fixture-operator",
    expected_event: int | None = None,
) -> list[str]:
    arguments = [
        "--state-directory",
        str(state_directory),
        "--action-recovery-record",
        str(action_id),
        "--action-recovery-actor",
        actor,
        "--confirm-reconciliation-record",
        "--action-recovery-json",
    ]
    if expected_event is not None:
        arguments.extend(("--action-recovery-expected-event", str(expected_event)))
    return arguments


def test_reconciliation_record_cli_is_explicit_idempotent_and_non_mutating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory, root = _action_sandbox(tmp_path)
    action_id, source, target = _create_uncertain_action(state_directory, root)

    first_exit = cli_main(_record_args(state_directory, action_id, actor="  operator-a  "))
    first = json.loads(capsys.readouterr().out)
    second_exit = cli_main(_record_args(state_directory, action_id, actor="operator-a"))
    second = json.loads(capsys.readouterr().out)

    assert first_exit == second_exit == 2
    assert first == second
    assert first["classification"] == "impossible_to_check"
    assert first["filesystem_mutation_authorized"] is False
    assert first["actor"] == "operator-a"
    assert not source.exists()
    assert not target.exists()
    with sqlite3.connect(state_directory / "framework.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM file_action_reconciliation_events"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM file_actions WHERE action_id=?", (action_id,)
        ).fetchone() == ("recovery_required",)


def test_reconciliation_record_cli_rejects_a_stale_predecessor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory, root = _action_sandbox(tmp_path)
    action_id, source, target = _create_uncertain_action(state_directory, root)
    assert cli_main(_record_args(state_directory, action_id)) == 2
    event_id = int(json.loads(capsys.readouterr().out)["event_id"])

    assert (
        cli_main(
            _record_args(
                state_directory,
                action_id,
                actor="second-operator",
                expected_event=event_id + 100,
            )
        )
        == 2
    )
    assert "latest event changed" in capsys.readouterr().out
    assert not source.exists()
    assert not target.exists()
    with sqlite3.connect(state_directory / "framework.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM file_action_reconciliation_events"
        ).fetchone() == (1,)


def test_reconciliation_record_cli_does_not_create_missing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / "missing"

    assert cli_main(_record_args(state_directory, 1)) == 2

    assert "ERROR action-recovery-record" in capsys.readouterr().out
    assert not state_directory.exists()


def test_reconciliation_record_cli_human_output_confirms_effect_without_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trusted_parent = tmp_path / "native-roots"
    trusted_parent.mkdir()
    with ContainedMutationRoot.create(
        trusted_parent,
        watch_directories=(trusted_parent,),
    ) as containment:
        state_directory = containment.root / "state"
        root = containment.root / "corpus"
        state_directory.mkdir()
        root.mkdir()
        source = root / "source.bin"
        target = root / "target.bin"
        source.write_bytes(b"confirmed fixture")
        with FrameworkState(state_directory / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            action_id = state.begin_file_action(
                run_id,
                "move_document",
                str(source),
                str(target),
                None,
                "controlled confirmed fixture",
                True,
            )
            identity = expected_identity_json(
                snapshot_path(source),
                source_path=str(source),
                target_path=str(target),
            )
            state.mark_file_actions_applying(((action_id, identity),))
            containment.rename(source, target)
            state.require_file_action_recovery((action_id,), "commit result lost")

        exit_code = cli_main(
            [
                "--state-directory",
                str(state_directory),
                "--action-recovery-record",
                str(action_id),
                "--action-recovery-actor",
                "operator-confirmed",
                "--confirm-reconciliation-record",
            ]
        )

        output = capsys.readouterr().out
        assert exit_code == 0
        assert "ACTION_RECOVERY_RECORDED" in output
        assert "classification=confirmed" in output
        assert source.exists() is False
        assert target.read_bytes() == b"confirmed fixture"
        with sqlite3.connect(state_directory / "framework.sqlite3") as connection:
            assert connection.execute(
                "SELECT status FROM file_actions WHERE action_id=?", (action_id,)
            ).fetchone() == ("recovery_required",)
            assert connection.execute(
                "SELECT COUNT(*) FROM file_action_reconciliation_events"
            ).fetchone() == (1,)


def test_reconciliation_record_cli_rejects_action_that_is_not_uncertain(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory, root = _action_sandbox(tmp_path)
    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = begin_signed_normal_run(state, root)
        action_id = state.begin_file_action(
            run_id,
            "move_document",
            str(root / "source.bin"),
            str(root / "target.bin"),
            None,
            "not uncertain",
            False,
        )
        state.finish_file_action(action_id, "planned", "dry-run")

    assert cli_main(_record_args(state_directory, action_id)) == 2

    assert "is not recoverable" in capsys.readouterr().out
    with sqlite3.connect(state_directory / "framework.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM file_action_reconciliation_events"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "arguments",
    (
        ("--action-recovery-record", "1", "--action-recovery-actor", "operator"),
        ("--action-recovery-record", "1", "--confirm-reconciliation-record"),
        ("--action-recovery-actor", "operator"),
        ("--confirm-reconciliation-record",),
    ),
)
def test_reconciliation_record_cli_requires_scoped_explicit_authorization(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit):
        cli_main(list(arguments))


# endregion [02]
