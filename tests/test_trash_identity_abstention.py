# region [00] Contexto del módulo
# Módulo: tests/test_trash_identity_abstention.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from neocortex.deduplication import DedupIndex, snapshot_path
from _04_Nucleo_Operativo.actions import (
    TRASH_IDENTITY_ABSTENTION,
    FrameworkActions,
)
from _04_Nucleo_Operativo.state import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run
# endregion [01]

# region [02] Implementación


def test_apply_abstains_when_recycle_backend_is_path_bound(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    candidate = corpus / "candidate.bin"
    candidate.write_bytes(b"authorized-object")
    expected = snapshot_path(candidate)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"

    with (
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = begin_signed_normal_run(state, corpus)
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            excluded_paths=(),
        )
        with patch("_04_Nucleo_Operativo.actions.send2trash") as recycle:
            result = actions._apply_trash_batch(
                "trash_duplicate",
                ((str(candidate), "fixture"),),
                expected_snapshots=(expected,),
            )

    assert result == (0, 0, 1)
    assert candidate.read_bytes() == b"authorized-object"
    recycle.assert_not_called()
    with sqlite3.connect(database) as connection:
        status, detail = connection.execute(
            "SELECT status,detail FROM file_actions WHERE action_type='trash_duplicate'"
        ).fetchone()
    assert status == "skipped"
    assert detail == TRASH_IDENTITY_ABSTENTION


def test_dry_run_retains_planned_recycle_action(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    candidate = corpus / "candidate.bin"
    candidate.write_bytes(b"authorized-object")
    expected = snapshot_path(candidate)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"

    with (
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(corpus, excluded_paths=())
        run_id = begin_signed_normal_run(state, corpus)
        result = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=False,
            excluded_paths=(),
        )._apply_trash_batch(
            "trash_duplicate",
            ((str(candidate), "fixture"),),
            expected_snapshots=(expected,),
        )

    assert result == (0, 0, 0)
    assert candidate.exists()
    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM file_actions WHERE action_type='trash_duplicate'"
        ).fetchone()[0]
    assert status == "planned"


# endregion [02]
