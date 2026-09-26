"""Targeted pass-2 regressions for the effects admission projection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from neocortex.safety.corpus_access import CorpusMutationGuard
import neocortex.workflow.actions.action_effects as effects
from neocortex.workflow.actions.action_effects import EffectsActionsMixin


class _State:
    def __init__(self) -> None:
        self.started: tuple[tuple[object, ...], ...] = ()
        self.finished: list[tuple[tuple[int, ...], str, str | None]] = []

    def begin_file_actions(self, run_id: int, rows):
        assert run_id == 1
        self.started = tuple(tuple(row) for row in rows)
        return tuple(range(1, len(self.started) + 1))

    def finish_file_actions(self, action_ids, status: str, detail: str | None = None) -> None:
        self.finished.append((tuple(action_ids), status, detail))


class _Guard:
    def __init__(self, root: Path, denied: str) -> None:
        self.policy = SimpleNamespace(root=root)
        self.denied = denied
        self.paths: tuple[str, ...] = ()

    def mutation_path_protection_reasons(self, *paths: str) -> tuple[str | None, ...]:
        self.paths = paths
        return tuple("guard_denied" if path.endswith(self.denied) else None for path in paths)


class _Runner(EffectsActionsMixin):
    _apply = False
    _run_id = 1

    def __init__(self, state: _State) -> None:
        self._state = state

    def _record_redlist_batch_diagnostic(self, *_args: object) -> None:
        pass


def test_begin_candidates_observes_legacy_protection_once_and_preserves_guard_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    legacy = root / "ntuser.dat"
    guarded = root / "guarded.bin"
    safe = root / "safe.bin"
    for path in (legacy, guarded, safe):
        path.write_bytes(b"fixture")

    state = _State()
    runner = _Runner(state)
    guard = _Guard(root, guarded.name)
    batch = tuple((str(path), "fixture") for path in (legacy, guarded, safe))
    expected = (None,) * len(batch)
    references = (None,) * len(batch)
    original = effects._protected_path_reason
    calls = 0

    def counted(path: str | Path) -> str | None:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(effects, "_protected_path_reason", counted)
    eligible, protected = runner._begin_trash_candidates(
        "trash_redlist",
        batch,
        expected,
        references,
        mutation_guard=cast(CorpusMutationGuard, guard),
    )

    assert calls == len(batch)
    assert guard.paths == (str(guarded), str(safe))
    assert [item[1] for item in eligible] == [str(safe)]
    assert protected == 2
    assert [row[1] for row in state.started] == [str(legacy), str(safe)]
    assert state.finished == [((1,), "skipped", "protected Windows user-profile state")]
