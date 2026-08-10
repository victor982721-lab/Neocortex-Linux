"""Supply-chain contracts for the isolated Semgrep scan tool runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from neocortex.semgrep_tool_contract import (
    PIP_BOOTSTRAP_FILENAME,
    PIP_BOOTSTRAP_SHA256,
    SEMGREP_SCAN_WRAPPER,
    SEMGREP_SCAN_WRAPPER_NAME,
    SEMGREP_TOOL_CONSTRAINTS_SHA256,
    SEMGREP_TOOL_DENIED_ENTRYPOINTS,
    SEMGREP_TOOL_MCP_VERSION,
    SEMGREP_TOOL_RECEIPT_NAME,
    SEMGREP_TOOL_VERSION,
    canonical_json,
    resolve_semgrep_tool_runtime,
)
from tools import release_linux, semgrep_tool_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _synthetic_receipt(tool_root: Path) -> dict[str, object]:
    inventory = [
        {"name": "mcp", "version": SEMGREP_TOOL_MCP_VERSION},
        {"name": "pip", "version": semgrep_tool_runtime.SEMGREP_TOOL_PIP_VERSION},
        {"name": "semgrep", "version": SEMGREP_TOOL_VERSION},
    ]
    artifacts = [
        {
            "filename": "mcp.whl",
            "name": "mcp",
            "sha256": "1" * 64,
            "version": SEMGREP_TOOL_MCP_VERSION,
        },
        {
            "filename": "semgrep.whl",
            "name": "semgrep",
            "sha256": "2" * 64,
            "version": SEMGREP_TOOL_VERSION,
        },
    ]
    return semgrep_tool_runtime._receipt(
        tool_root=tool_root,
        inventory=inventory,
        artifacts=artifacts,
    )


def _write_synthetic_runtime(runtime_root: Path) -> tuple[Path, dict[str, object]]:
    tool_root = runtime_root / "tools" / "semgrep"
    scripts = tool_root / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    python.write_bytes(b"synthetic contained interpreter")
    (tool_root / SEMGREP_SCAN_WRAPPER_NAME).write_bytes(SEMGREP_SCAN_WRAPPER)
    receipt = _synthetic_receipt(tool_root)
    (tool_root / SEMGREP_TOOL_RECEIPT_NAME).write_bytes(canonical_json(receipt))
    return tool_root, receipt


def test_main_runtime_is_mcp_safe_and_semgrep_is_only_in_the_tool_lock() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    constraints = (PROJECT_ROOT / "constraints.txt").read_text(encoding="utf-8")
    tool_constraints = (PROJECT_ROOT / "tools" / "semgrep_tool_constraints.txt").read_text(
        encoding="utf-8"
    )

    assert '"mcp==1.29.0"' in pyproject
    assert "mcp==1.29.0" in constraints.splitlines()
    assert "semgrep" not in {
        line.split("==", 1)[0].casefold() for line in constraints.splitlines() if "==" in line
    }
    assert f"mcp=={SEMGREP_TOOL_MCP_VERSION}" in tool_constraints.splitlines()
    assert f"semgrep=={SEMGREP_TOOL_VERSION}" in tool_constraints.splitlines()
    assert (
        hashlib.sha256(tool_constraints.encode("utf-8")).hexdigest()
        == SEMGREP_TOOL_CONSTRAINTS_SHA256
    )


def test_release_and_tool_runtime_share_the_exact_pip_bootstrap_policy() -> None:
    assert release_linux.PIP_BOOTSTRAP_VERSION == semgrep_tool_runtime.SEMGREP_TOOL_PIP_VERSION
    assert release_linux.PIP_BOOTSTRAP_FILENAME == PIP_BOOTSTRAP_FILENAME
    assert release_linux.PIP_BOOTSTRAP_SHA256 == PIP_BOOTSTRAP_SHA256
    assert release_linux.PIP_BOOTSTRAP_URL == semgrep_tool_runtime.PIP_BOOTSTRAP_URL


def test_scan_wrapper_cannot_select_the_semgrep_mcp_subcommand() -> None:
    source = SEMGREP_SCAN_WRAPPER.decode("utf-8")

    assert 'sys.argv = [sys.argv[0], "scan", *arguments]' in source
    assert '"mcp"' not in source
    assert "console_scripts.pysemgrep" in source


def test_receipt_resolves_only_contained_regular_executables(tmp_path: Path) -> None:
    tool_root, receipt = _write_synthetic_runtime(tmp_path)

    runtime = resolve_semgrep_tool_runtime(tmp_path)

    assert runtime.tool_root == tool_root
    assert runtime.version == SEMGREP_TOOL_VERSION
    assert runtime.command_prefix == (
        str(tool_root / receipt["python_executable"]),
        "-I",
        str(tool_root / SEMGREP_SCAN_WRAPPER_NAME),
    )


def test_receipt_rejects_wrapper_digest_tampering(tmp_path: Path) -> None:
    tool_root, _receipt = _write_synthetic_runtime(tmp_path)
    (tool_root / SEMGREP_SCAN_WRAPPER_NAME).write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="wrapper digest"):
        resolve_semgrep_tool_runtime(tmp_path)


def test_denied_console_entrypoint_is_rejected(tmp_path: Path) -> None:
    tool_root, _receipt = _write_synthetic_runtime(tmp_path)
    scripts = tool_root / ("Scripts" if os.name == "nt" else "bin")
    denied = scripts / ("mcp.exe" if os.name == "nt" else "mcp")
    denied.write_bytes(b"not exposed")

    with pytest.raises(
        semgrep_tool_runtime.SemgrepToolRuntimeError,
        match="denied console entrypoint",
    ):
        semgrep_tool_runtime._require_no_denied_entrypoints(tool_root)

    assert set(SEMGREP_TOOL_DENIED_ENTRYPOINTS) == {"mcp", "pysemgrep", "semgrep"}


def test_pip_bootstrap_is_rejected_before_environment_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / PIP_BOOTSTRAP_FILENAME
    wheel.write_bytes(b"forged")
    created = False

    class RejectBuilder:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal created
            created = True

    monkeypatch.setattr(semgrep_tool_runtime.venv, "EnvBuilder", RejectBuilder)

    with pytest.raises(
        semgrep_tool_runtime.SemgrepToolRuntimeError,
        match="exact SHA-256",
    ):
        semgrep_tool_runtime._bootstrap_environment(
            tmp_path / "runtime",
            wheel,
            runner=semgrep_tool_runtime._run,
        )

    assert created is False


def test_receipt_json_is_deterministic_and_contains_explicit_exceptions(tmp_path: Path) -> None:
    tool_root, receipt = _write_synthetic_runtime(tmp_path)
    persisted = json.loads((tool_root / SEMGREP_TOOL_RECEIPT_NAME).read_text(encoding="utf-8"))

    assert persisted == receipt
    assert canonical_json(receipt) == canonical_json(persisted)
    assert {item["id"] for item in persisted["vulnerability_exceptions"]} == {
        "GHSA-hvrp-rf83-w775",
        "GHSA-jpw9-pfvf-9f58",
        "GHSA-vj7q-gjh5-988w",
    }
    assert all(item["reachable"] is False for item in persisted["vulnerability_exceptions"])
    assert all(item["expires"] == "2026-09-30" for item in persisted["vulnerability_exceptions"])
