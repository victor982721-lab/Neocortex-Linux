"""Focused CLI integration tests for unified read-only Code queries."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from _04_Nucleo_Operativo import cli_code


@dataclass(frozen=True)
class _FakeQuery:
    surface: str
    providers: tuple[str, ...]
    categories: tuple[str, ...]
    modules: tuple[str, ...]
    statuses: tuple[str, ...]
    deltas: tuple[str, ...]
    work_packages: tuple[str, ...]
    limit: int


def _args(
    tmp_path: Path,
    surface: str,
    *,
    json_output: bool = True,
    limit: int = 50,
) -> argparse.Namespace:
    return argparse.Namespace(
        state_directory=tmp_path,
        code_query=surface,
        code_query_provider=["ruff", "mypy"],
        code_query_category=["diagnostic"],
        code_query_module=["core.api"],
        code_query_status=["ready"],
        code_query_delta=["added"],
        code_query_work_package=["wp-1"],
        code_query_limit=limit,
        code_query_baseline=None,
        code_json=json_output,
    )


def _install_engine(monkeypatch: pytest.MonkeyPatch, callback: Any) -> None:
    module = ModuleType("_04_Nucleo_Operativo.code_analysis_query")
    module.CodeAnalysisQuery = _FakeQuery  # type: ignore[attr-defined]
    module.query_code_analysis = callback  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)


def test_code_query_review_uses_public_payload_and_exact_query_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo import code_review

    calls: dict[str, Any] = {}

    class _ReviewResult:
        def as_payload(self) -> dict[str, object]:
            calls["serialized"] = True
            return {"kind": "code-review", "status": "ready"}

    def review_code_state(state: Path, *, limit: int):
        calls["review"] = (state, limit)
        return _ReviewResult()

    def query_code_analysis(payload: dict[str, object], query: _FakeQuery):
        calls["engine"] = (payload, query)
        return {
            "schema": "neocortex.code-analysis-query/v1",
            "kind": "code-analysis-query",
            "surface": "review",
            "status": "ready",
            "counts": {"matched": 1},
            "filters": {},
            "matches": [{"id": "finding-1"}],
            "limitations": [],
        }

    monkeypatch.setattr(code_review, "review_code_state", review_code_state)
    _install_engine(monkeypatch, query_code_analysis)
    args = _args(tmp_path, "review", limit=2)

    assert cli_code.run_code_query(args) == 0

    assert calls["review"] == (tmp_path, 2)
    assert calls["serialized"] is True
    payload, query = cast(tuple[dict[str, object], _FakeQuery], calls["engine"])
    assert payload == {"kind": "code-review", "status": "ready"}
    assert query == _FakeQuery(
        surface="review",
        providers=("ruff", "mypy"),
        categories=("diagnostic",),
        modules=("core.api",),
        statuses=("ready",),
        deltas=("added",),
        work_packages=("wp-1",),
        limit=2,
    )
    assert json.loads(capsys.readouterr().out) == query_code_analysis(payload, query)


def test_code_query_status_reuses_snapshot_and_self_analysis_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_analyzers, self_analysis_status

    database = tmp_path / "code.sqlite3"
    database.write_bytes(b"published-state")
    before = database.read_bytes()
    snapshot = cli_code._CodeStatusSnapshot(
        schema_version=4,
        counts={"files": 3},
        latest_run=None,
        external_evidence={"status": "ready"},
        external_evidence_suite={"profile": "full", "providers": []},
        architecture={"status": "ready"},
        test_coverage={"status": "ready"},
    )
    observed: dict[str, object] = {}

    class _Registry:
        def status(self):
            return {"python": "ready"}

    monkeypatch.setattr(code_analyzers, "builtin_analyzer_registry", _Registry)
    monkeypatch.setattr(self_analysis_status, "require_sqlite_sidecars_absent", lambda path: None)
    monkeypatch.setattr(cli_code, "_read_code_status_snapshot", lambda path: snapshot)

    def read_self_analysis(args, latest, *, enabled=None):
        observed["self_analysis"] = (args, latest, enabled)
        return {"status": "ready", "manifest": "current"}

    monkeypatch.setattr(cli_code, "_read_self_analysis_payload", read_self_analysis)

    def query_code_analysis(payload, query):
        observed["source"] = payload
        return {
            "surface": query.surface,
            "status": "ready",
            "counts": {"matched": 0},
            "matches": [],
        }

    _install_engine(monkeypatch, query_code_analysis)
    args = _args(tmp_path, "status")

    assert cli_code.run_code_query(args) == 0
    assert observed["self_analysis"] == (args, None, True)
    assert observed["source"] == cli_code._code_status_payload(
        database,
        {"python": "ready"},
        snapshot,
        {"status": "ready", "manifest": "current"},
    )
    assert database.read_bytes() == before
    assert tuple(tmp_path.iterdir()) == (database,)


def test_code_query_diff_uses_public_comparison_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _04_Nucleo_Operativo import code_publication_diff

    baseline = tmp_path / "baseline"
    current = tmp_path / "current"
    current.mkdir()
    calls: dict[str, Any] = {}

    class _DiffResult:
        def as_payload(self):
            calls["serialized"] = True
            return {"kind": "code-publication-diff", "status": "ready"}

    def compare_code_publications(baseline_state: Path, current_state: Path):
        calls["compare"] = (baseline_state, current_state)
        return _DiffResult()

    def query_code_analysis(payload, query):
        calls["source"] = payload
        return {"surface": query.surface, "status": "ready", "matches": []}

    monkeypatch.setattr(
        code_publication_diff,
        "compare_code_publications",
        compare_code_publications,
    )
    _install_engine(monkeypatch, query_code_analysis)
    args = _args(current, "diff")
    args.code_query_baseline = str(baseline)

    assert cli_code.run_code_query(args) == 0
    assert calls["compare"] == (baseline, current)
    assert calls["serialized"] is True
    assert calls["source"] == {"kind": "code-publication-diff", "status": "ready"}


def test_code_query_human_output_is_bounded_and_errors_are_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _args(tmp_path, "review", json_output=False, limit=2)
    monkeypatch.setattr(
        cli_code,
        "_read_code_query_source",
        lambda _args: {"kind": "code-review", "status": "ready"},
    )
    _install_engine(
        monkeypatch,
        lambda _payload, _query: {
            "surface": "review",
            "status": "ready",
            "counts": {"matched": 3},
            "filters": {"providers": ["ruff"]},
            "matches": [{"id": 1}, {"id": 2}, {"id": 3}],
            "limitations": ["bounded"],
        },
    )

    assert cli_code.run_code_query(args) == 0
    output = capsys.readouterr().out.splitlines()
    assert output[0] == "CODE_QUERY surface=review status=ready matched=3 returned=2 limit=2"
    assert sum(line.startswith("CODE_QUERY_MATCH ") for line in output) == 2
    assert "CODE_QUERY_LIMITATION bounded" in output

    args.code_query = "diff"
    args.code_query_baseline = None
    monkeypatch.undo()
    _install_engine(monkeypatch, lambda _payload, _query: {})
    assert cli_code.run_code_query(args) == 2
    assert "ERROR code-query ValueError" in capsys.readouterr().err
    assert not tuple(tmp_path.iterdir())
