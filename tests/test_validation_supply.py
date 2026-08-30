"""Focused supply replay and dependency-contract regressions."""

from __future__ import annotations

import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from neocortex.code import validation_supply as supply


def _dependency_files(pyproject: str) -> dict[str, str]:
    return {
        "constraints.txt": "packaging==26.0\n",
        "constraints-linux-cp314.lock": "packaging==26.0\n",
        "tools/quality_gate_supply_policy.json": '{"schema":"fixture/v1"}\n',
        "pyproject.toml": pyproject,
    }


VALID_PYPROJECT = """[build-system]
requires = ["setuptools==83.0.0"]
build-backend = "setuptools.build_meta"

[project]
name = "fixture"
requires-python = ">=3.13"
dependencies = ["packaging>=26,<27"]

[project.optional-dependencies]
full = ["rich>=15,<16"]
"""


def test_dependency_contract_is_canonical_and_detects_only_dependency_semantics() -> None:
    baseline = _dependency_files(VALID_PYPROJECT)
    topology_only = dict(baseline)
    topology_only["pyproject.toml"] = VALID_PYPROJECT + "\n[tool.setuptools]\npy-modules=[]\n"

    equal = supply.dependency_contract_comparison(topology_only, baseline)
    assert equal["semantics_identical"] is True

    changed = dict(baseline)
    changed["constraints-linux-cp314.lock"] = "packaging==26.1\n"
    different = supply.dependency_contract_comparison(changed, baseline)
    assert different["semantics_identical"] is False


@pytest.mark.parametrize(
    "files,reason",
    (
        ({}, "files_incomplete"),
        (
            {
                **_dependency_files(VALID_PYPROJECT),
                "tools/quality_gate_supply_policy.json": "[]",
            },
            "policy_not_object",
        ),
        (
            _dependency_files("[project]\nname='missing-build-system'\n"),
            "sections_missing",
        ),
    ),
)
def test_dependency_contract_rejects_incomplete_or_malformed_inputs(
    files: dict[str, str],
    reason: str,
) -> None:
    with pytest.raises(supply.SupplyValidationError, match=reason):
        supply.dependency_contract_payload(files)


class _Result:
    def __init__(self, *, one=None, all_rows=None) -> None:
        self._one = one
        self._all = [] if all_rows is None else all_rows

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _SupplyConnection:
    versions = (
        {
            "subject_key": "package:packaging",
            "value": 1,
            "metadata_json": '{"normalized_name":"packaging","installed_version":"26.0"}',
        },
        {
            "subject_key": "package:rich",
            "value": 1,
            "metadata_json": '{"normalized_name":"rich","installed_version":"15.0"}',
        },
    )

    def execute(self, query: str, parameters=()):
        normalized = " ".join(query.split())
        if "SELECT r.status,c.execution" in normalized:
            return _Result(one={"status": "completed", "execution": "full"})
        if "category='package_integrity'" in normalized:
            return _Result(all_rows=list(self.versions))
        if "ORDER BY r.analysis_run_id DESC" in normalized:
            return _Result(one={"tool_run_id": 300})
        if "WHERE r.analysis_run_id=? AND c.provider_id=?" in normalized:
            return _Result(one={"tool_run_id": 200})
        if "audit_fresh_until_unix_seconds" in normalized:
            return _Result(
                one={
                    "tool_run_id": 2278,
                    "analysis_run_id": 208,
                    "result_digest": "audit-digest",
                    "fresh_until": time.time() + 7200,
                }
            )
        if "SUM(CASE WHEN metric_name='known_vulnerability_count'" in normalized:
            return _Result(one={"vulnerabilities": 0, "current_markers": 1})
        if "SELECT COUNT(*) FROM external_findings" in normalized:
            return _Result(one=(0,))
        raise AssertionError(f"unexpected SQL: {normalized}")


def test_historical_audit_requires_matching_inventory_and_zero_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "code.sqlite3"
    database.touch()
    connection = _SupplyConnection()
    monkeypatch.setattr(supply, "readonly_code_database", lambda _path: nullcontext(connection))
    monkeypatch.setattr(supply, "validate_code_schema", lambda _connection: None)
    comparison = {
        "semantics_identical": True,
        "current_signature": "sha256:dependency",
        "baseline_signature": "sha256:dependency",
    }

    receipt = supply.historical_pip_audit_fallback(
        tmp_path,
        analysis_run_id=None,
        supply_paths=("pyproject.toml",),
        dependency_comparison=comparison,
    )

    assert receipt is not None
    assert receipt["tool_run_id"] == 2278
    assert receipt["installed_distributions"] == 2
    assert receipt["known_vulnerabilities"] == 0
    assert receipt["dependency_contract"] == comparison


def test_installed_versions_rejects_duplicate_or_invalid_rows() -> None:
    connection = _SupplyConnection()
    assert supply.installed_versions(connection, 300) == {
        "packaging": "26.0",
        "rich": "15.0",
    }
    duplicate = _SupplyConnection()
    duplicate.versions = (connection.versions[0], connection.versions[0])
    assert supply.installed_versions(duplicate, 300) is None
