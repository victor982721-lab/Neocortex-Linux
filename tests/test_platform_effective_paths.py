"""The platform doctor must not hide a launcher or invocation path override."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.api.cli.cli_platform import platform_report
from neocortex.interface.entrypoint import entrypoint
from neocortex.platform.policy import current_platform_policy


def test_report_distinguishes_launcher_override_from_canonical_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "configured-corpus"
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", str(corpus))
    report = platform_report()
    assert report["paths"]["corpus"] == str(current_platform_policy().corpus_root)
    assert report["effective_paths"]["corpus"] == str(corpus)
    assert not corpus.exists()


def test_report_without_override_has_matching_canonical_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEOCORTEX_CORPUS_ROOT", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    report = platform_report()
    for key in ("corpus", "state"):
        assert report["effective_paths"][key] == report["paths"][key]


@pytest.mark.parametrize("as_json", [True, False])
def test_doctor_exposes_explicit_paths_without_creating_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", str(tmp_path / "launcher-default"))
    corpus, state = tmp_path / "selected-corpus", tmp_path / "selected-state"
    argv = ["--doctor-platform", "--root", str(corpus), "--state-directory", str(state)]
    if as_json:
        argv.append("--doctor-platform-json")
    assert entrypoint(argv) == 0
    captured = capsys.readouterr()
    assert not captured.err
    if as_json:
        report = json.loads(captured.out)
        assert report["effective_paths"] == {"corpus": str(corpus), "state": str(state)}
    else:
        assert f'PLATFORM_EFFECTIVE_PATH name=corpus value="{corpus}"' in captured.out
        assert f'PLATFORM_EFFECTIVE_PATH name=state value="{state}"' in captured.out
    assert not corpus.exists()
    assert not state.exists()
