"""Fixture-only E2E checks for the global ``-S`` admission ceiling.

The heavier PDF/image/archive cold-cache canary is kept in the bounded
``/tmp/run_global_size_limit_e2e.py`` receipt harness.  This test stays small
enough for the normal suite while still driving the public ``--all`` command,
fresh state, duplicate planning, and an apply run against a temporary root.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


TEN_MB = 10_000_000


def _write_sparse(path: Path, size: int, prefix: bytes = b"\0") -> None:
    with path.open("wb") as stream:
        stream.write(prefix[:size])
        stream.truncate(size)


def _make_fixture(root: Path) -> None:
    root.mkdir()
    _write_sparse(root / "tiny.txt", 1_000, b"global size E2E\n")
    _write_sparse(root / "boundary.bin", TEN_MB, b"boundary\n")
    _write_sparse(root / "over.bin", TEN_MB + 1, b"oversize\n")
    (root / "duplicate-a.txt").write_bytes(b"duplicate small\n")
    (root / "duplicate-b.txt").write_bytes(b"duplicate small\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(root: Path, state: Path, *size_arguments: str, apply: bool = False) -> dict[str, Any]:
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "DISPLAY", "WAYLAND_DISPLAY"):
        environment.pop(key, None)
    home = state.parent / f"home-{state.name}"
    for key, suffix in (
        ("HOME", "home"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("TMPDIR", "tmp"),
    ):
        destination = home / suffix
        destination.mkdir(parents=True, exist_ok=True)
        environment[key] = str(destination)
    environment.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        NEOCORTEX_PROGRESS_STREAM="1",
        QT_QPA_PLATFORM="offscreen",
    )
    launcher = os.environ.get("NEOCORTEX_SIZE_LIMIT_LAUNCHER")
    canonical_state_home = os.environ.get("NEOCORTEX_SIZE_LIMIT_CANONICAL_STATE")
    if launcher and canonical_state_home:
        # The installed effects-preparation probe reads the attested release
        # receipt from the canonical XDG state tree.  The explicit
        # ``--state-directory`` below remains temporary and isolated.
        environment["XDG_STATE_HOME"] = canonical_state_home
    command = ([launcher] if launcher else [sys.executable, "-m", "neocortex"]) + [
        "--root",
        str(root),
        "--state-directory",
        str(state),
        "--all",
        "--json",
        "--no-document-catalog",
        "--ocr",
        "never",
        "--image-document-ocr",
        "never",
        "--video-ocr",
        "never",
        # No audio fixture is present, so the integrated semantic stage has
        # an empty source scope and does not load/download a model.
        "--semantic-source",
        "audio",
        "--semantic-max-items",
        "1",
        "--semantic-max-new-jobs",
        "1",
        "--semantic-time-budget-seconds",
        "10",
        *size_arguments,
    ]
    if apply:
        command.insert(command.index("--all") + 1, "--apply")
    result = subprocess.run(
        command,
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout tail:\n{result.stdout[-2500:]}\n"
        f"stderr tail:\n{result.stderr[-3500:]}"
    )
    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"no JSON payload in stdout:\n{result.stdout[-2500:]}")


def _find_admission(payload: object) -> dict[str, object]:
    required = {
        "total_files",
        "eligible_files",
        "size_skipped_files",
        "size_skipped_bytes",
        "max_file_bytes",
    }

    def visit(value: object) -> dict[str, object] | None:
        if isinstance(value, dict):
            if required <= value.keys():
                return {key: value[key] for key in required}
            for child in value.values():
                found = visit(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found is not None:
                    return found
        return None

    found = visit(payload)
    assert found is not None, f"size admission metrics absent from payload: {json.dumps(payload)[:8000]}"
    return found


@pytest.mark.parametrize(
    "size_arguments",
    (("-S10",), ("-S", "10"), ("--max-size-mb", "10")),
    ids=("attached", "separated", "long"),
)
def test_all_size_limit_spellings_have_identical_fixture_metrics(
    tmp_path: Path, size_arguments: tuple[str, ...]
) -> None:
    root = tmp_path / "corpus"
    _make_fixture(root)
    metrics = _find_admission(_run(root, tmp_path / "state", *size_arguments))
    assert metrics == {
        "total_files": 5,
        "eligible_files": 4,
        "size_skipped_files": 1,
        "size_skipped_bytes": TEN_MB + 1,
        "max_file_bytes": TEN_MB,
    }


def test_all_size_limit_apply_preserves_oversize_and_larger_limit_readmits(
    tmp_path: Path,
) -> None:
    if not os.environ.get("NEOCORTEX_SIZE_LIMIT_LAUNCHER"):
        pytest.skip(
            "--apply E2E requires the installed, attested launcher; source venvs are read-only"
        )
    root = tmp_path / "corpus"
    _make_fixture(root)
    before = _sha256(root / "over.bin")
    limited = _run(root, tmp_path / "state-limited", "-S10", apply=True)
    assert _find_admission(limited)["size_skipped_files"] == 1
    assert (root / "over.bin").exists()
    assert _sha256(root / "over.bin") == before

    expanded = _run(root, tmp_path / "state-expanded", "-S100")
    assert _find_admission(expanded) == {
        # The limited apply may remove one eligible duplicate; the previously
        # oversize file remains and is now admitted by the larger ceiling.
        "total_files": 3,
        "eligible_files": 3,
        "size_skipped_files": 0,
        "size_skipped_bytes": 0,
        "max_file_bytes": 100_000_000,
    }
