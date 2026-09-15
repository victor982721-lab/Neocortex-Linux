"""CLI contracts for the explicit historical-audit maintenance scope."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


def _install_fake_manager(monkeypatch, *, plan_result, apply_result=None):
    calls: list[tuple[str, object]] = []

    class FakeHistoricalAuditManager:
        def __init__(self, root, **kwargs):
            calls.append(("init", (Path(root), kwargs)))
            self.root = Path(root)

        def plan(self):
            calls.append(("plan", self.root))
            return plan_result

        def apply(self, plan):
            calls.append(("apply", plan))
            return plan if apply_result is None else apply_result

    module = types.ModuleType("neocortex.runtime.historical_audit")
    module.__dict__["HistoricalAuditManager"] = FakeHistoricalAuditManager
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls


def _invoke(args: list[str], capsys) -> tuple[int, dict[str, object]]:
    exit_code = main(args)
    captured = capsys.readouterr()
    assert captured.err == ""
    return exit_code, json.loads(captured.out)


def test_historical_scope_requires_explicit_absolute_root() -> None:
    parser = build_parser()
    missing = parser.parse_args(["maintenance", "--scope", "historical-temp"])
    with pytest.raises(SystemExit, match="maintenance --scope historical-temp"):
        validate_arguments(missing)

    relative = parser.parse_args(
        [
            "maintenance",
            "--scope",
            "historical-temp",
            "--maintenance-audit-root",
            "relative/audit",
        ]
    )
    with pytest.raises(SystemExit, match="must be absolute"):
        validate_arguments(relative)


def test_historical_scope_does_not_default_to_tmp_or_create_root(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    audit_root = tmp_path / "historical"
    calls = _install_fake_manager(
        monkeypatch,
        plan_result={"status": "planned", "records": [], "reason": "root is absent"},
    )
    exit_code, payload = _invoke(
        [
            "maintenance",
            "--scope",
            "historical-temp",
            "--maintenance-audit-root",
            str(audit_root),
            "--maintenance-json",
        ],
        capsys,
    )
    assert exit_code == 0
    assert payload["scope"] == "historical-temp"
    assert payload["audit"] == "historical-audit"
    assert payload["historical_audit"] is True
    assert payload["root"] == str(audit_root)
    assert payload["root_exists"] is False
    assert not audit_root.exists()
    assert calls[0][0] == "init"
    assert calls[1][0] == "plan"
    assert all(name != "apply" for name, _value in calls)


def test_historical_scope_exposes_explicit_bounds_and_explanations(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "historical"
    root.mkdir(mode=0o700)
    entry = root / "neocortex-unregistered"
    entry.mkdir(mode=0o700)
    entry.chmod(0o700)
    payload_file = entry / "payload"
    payload_file.write_bytes(b"payload")
    payload_file.chmod(0o600)
    code = main(
        [
            "maintenance",
            "--scope",
            "historical-temp",
            "--maintenance-audit-root",
            str(root),
            "--maintenance-max-entries",
            "123",
            "--maintenance-max-depth",
            "4",
            "--maintenance-max-bytes",
            "9876",
            "--maintenance-json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["limits"] == {
        "max_entries": 123,
        "max_depth": 4,
        "max_bytes": 9876,
    }
    reason_summary = payload["reason_summary"]
    assert isinstance(reason_summary, list)
    assert isinstance(reason_summary[0], dict)
    assert reason_summary[0]["key"] == "no_manifest"
    assert payload["records_returned"] == 1
    assert payload["records_truncated"] is False


def test_historical_apply_delegates_and_preserves_unverified_as_blocked(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    audit_root = tmp_path / "historical"
    audit_root.mkdir()
    plan = {"status": "unverified", "records": [{"path": "/tmp/old"}]}
    calls = _install_fake_manager(monkeypatch, plan_result=plan)
    exit_code, payload = _invoke(
        [
            "maintenance",
            "--scope",
            "historical-temp",
            "--maintenance-audit-root",
            str(audit_root),
            "--apply",
            "--maintenance-json",
        ],
        capsys,
    )
    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["read_only"] is False
    assert payload["blocked"] == 1
    assert [name for name, _value in calls] == ["init", "plan", "apply"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--all"],
        ["--route", "pdf"],
        ["--route-only", "--route", "pdf"],
        ["--root", "/tmp/corpus"],
    ],
)
def test_historical_scope_rejects_integrated_or_ambiguous_roots(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "maintenance",
                "--scope",
                "historical-temp",
                "--maintenance-audit-root",
                "/tmp/neocortex-historical-test",
                *extra,
            ]
        )


def test_historical_audit_root_rejected_without_historical_scope() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "maintenance",
                "--scope",
                "owned-temp",
                "--maintenance-audit-root",
                "/tmp/neocortex-historical-test",
            ]
        )
    with pytest.raises(SystemExit):
        main(
            [
                "--maintenance-audit-root",
                "/tmp/neocortex-historical-test",
            ]
        )


def test_historical_audit_root_cannot_equal_effective_corpus_or_state(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"
    parser = build_parser()
    for forbidden in (corpus, state):
        args = parser.parse_args(
            [
                "maintenance",
                "--scope",
                "historical-temp",
                "--maintenance-audit-root",
                str(forbidden),
            ]
        )
        # Simulate effective configured roots without passing --root: the
        # historical selector may not reuse either effective owner root.
        args.root = corpus
        args.state_directory = state
        with pytest.raises(SystemExit):
            validate_arguments(args)
