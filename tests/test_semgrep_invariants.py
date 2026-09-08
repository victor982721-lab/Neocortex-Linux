"""Focused executable check for the repository's local Semgrep invariants."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "semgrep_invariants"
RULESET = ROOT / "semgrep" / "neo-invariants.yml"

EXPECTED_FINDINGS = {
    "neo-no-shell-execution": 1,
    "neo-no-eval-execution": 1,
    "neo-no-os-system": 1,
    "neo-no-corpus-execution": 1,
    "neo-sqlite-unsafe-read": 1,
    "neo-subprocess-without-limits": 1,
    "neo-mutation-without-grant-fence": 1,
}


def _semgrep() -> str:
    configured = os.environ.get("NEOCORTEX_SEMGREP")
    if configured:
        return configured
    discovered = shutil.which("semgrep")
    if discovered:
        return discovered
    candidates = sorted(
        Path.home().glob(".local/share/Neocortex/tooling/static-*/bin/semgrep"),
        reverse=True,
    )
    if candidates:
        return str(candidates[0])
    pytest.skip("Semgrep is not installed in this development environment")


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _run_semgrep(*, home: Path, config: Path) -> dict[str, object]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "SEMGREP_SEND_METRICS": "off",
            "SEMGREP_ENABLE_VERSION_CHECK": "0",
            "SEMGREP_DISABLE_VERSION_CHECK": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    process = subprocess.Popen(
        [
            _semgrep(),
            "scan",
            "--config",
            str(RULESET),
            "--json",
            "--metrics=off",
            "--x-ignore-semgrepignore-files",
            "--no-git-ignore",
            "--no-rewrite-rule-ids",
            "--jobs",
            "1",
            str(ROOT / "neocortex"),
            str(FIXTURES / "positive.py"),
            str(FIXTURES / "negative.py"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=90)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            pytest.fail(f"Semgrep timed out after 90 seconds: {exc}")
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")[-4000:]
        try:
            return json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            pytest.fail(
                "Semgrep returned invalid JSON: "
                f"{exc}; stdout={stdout[-1000:]!r}; stderr={stderr[-1000:]!r}"
            )
    finally:
        _stop_process(process)


def test_local_ruleset_distinguishes_product_and_fixture_boundaries(tmp_path: Path) -> None:
    isolated_home = tmp_path / "home"
    isolated_config = tmp_path / "config"
    isolated_home.mkdir()
    isolated_config.mkdir()
    report = _run_semgrep(home=isolated_home, config=isolated_config)
    assert isinstance(report, dict)
    assert report.get("errors") == []
    paths = report.get("paths")
    assert isinstance(paths, dict)
    scanned = paths.get("scanned", [])
    assert isinstance(scanned, list)
    assert any(path.endswith("neocortex/__init__.py") for path in scanned)
    assert str(FIXTURES / "positive.py") in scanned
    assert str(FIXTURES / "negative.py") in scanned

    results = report.get("results")
    assert isinstance(results, list)
    assert len(results) == sum(EXPECTED_FINDINGS.values())
    positive_results = [
        result for result in results if result.get("path") == str(FIXTURES / "positive.py")
    ]
    assert Counter(result.get("check_id") for result in positive_results) == Counter(
        EXPECTED_FINDINGS
    )
    assert all(result.get("path") != str(FIXTURES / "negative.py") for result in results)
    assert all(not result.get("path", "").startswith(str(ROOT / "neocortex")) for result in results)
