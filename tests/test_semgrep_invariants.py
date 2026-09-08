"""Focused executable check for the repository's local Semgrep invariants."""

from __future__ import annotations

import os
import re
import selectors
import signal
import shutil
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "semgrep_invariants"
RULESET = ROOT / "semgrep" / "neo-invariants.yml"

EXPECTED_FINDINGS = 7


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


def _semgrep_summary(path: Path, *, home: Path, config: Path) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "SEMGREP_SEND_METRICS": "off",
        }
    )
    process = subprocess.Popen(
        [
            _semgrep(),
            "scan",
            "--config",
            str(RULESET),
            "--metrics=off",
            "--x-ignore-semgrepignore-files",
            "--no-git-ignore",
            "--jobs",
            "1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    chunks: list[bytes] = []
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            events = selector.select(timeout=1)
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 16_384)
            if not chunk:
                break
            chunks.append(chunk)
            if b"Ran 7 rules on" in b"".join(chunks):
                break
        output = b"".join(chunks).decode("utf-8", errors="replace")
        assert "Scan completed successfully" in output, output
        assert "Ran 7 rules on" in output, output
        return output
    finally:
        selector.close()
        _stop_process(process)


def test_local_ruleset_distinguishes_positive_and_negative_fixtures(tmp_path: Path) -> None:
    isolated_home = tmp_path / "home"
    isolated_config = tmp_path / "config"
    isolated_home.mkdir()
    isolated_config.mkdir()
    positive = _semgrep_summary(
        FIXTURES / "positive.py", home=isolated_home, config=isolated_config
    )
    negative = _semgrep_summary(
        FIXTURES / "negative.py", home=isolated_home, config=isolated_config
    )
    positive_match = re.search(r"Findings: (\d+)", positive)
    negative_match = re.search(r"Findings: (\d+)", negative)
    assert positive_match is not None
    assert negative_match is not None
    assert int(positive_match.group(1)) == EXPECTED_FINDINGS
    assert int(negative_match.group(1)) == 0
