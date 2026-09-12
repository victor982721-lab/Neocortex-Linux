"""Allocator regression after a run-history reset."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from neocortex.persistence.framework_state_writer import FrameworkState


def test_framework_writer_honors_reset_run_id_floor(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "framework.sqlite3"
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('framework_run_id_floor','50')"
        )

    with FrameworkState(database) as framework:
        run_id = framework.begin_initial_run(corpus, None)

    assert run_id == 51
