"""Exercise the actual standard sdist backend and its extracted tool closure."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tools.release_artifacts import (
    SOURCE_DATE_EPOCH,
    canonicalize_sdist,
    validate_sdist,
    validate_wheel,
)


def _backend(source: Path, output: Path, kind: str) -> Path:
    output.mkdir()
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(
        PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0",
        PIP_NO_INDEX="1", PIP_CONFIG_FILE=os.devnull,
        SOURCE_DATE_EPOCH=str(SOURCE_DATE_EPOCH),
    )
    log_path = output / "build.log"
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            (
                sys.executable, "-I", "-B", "-c",
                f"import setuptools.build_meta as backend; backend.build_{kind}({str(output)!r})",
            ),
            cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=180,
        )
    assert result.returncode == 0, f"backend failed; see {log_path}"
    suffix = "*.tar.gz" if kind == "sdist" else "*.whl"
    artifacts = tuple(output.glob(suffix))
    assert len(artifacts) == 1
    return artifacts[0]


@pytest.fixture(scope="module")
def actual_sdists(tmp_path_factory: pytest.TempPathFactory):
    root = Path(__file__).resolve().parents[1]
    work = tmp_path_factory.mktemp("packaging-audit-sdist")
    source = work / "source-one"
    source.mkdir()
    for filename in (
        "MANIFEST.in", "pyproject.toml", "README.md", "constraints.txt",
        "constraints-linux-cp313.lock", "constraints-linux-cp313-runtime.lock",
        "constraints-linux-cp313-full.lock",
    ):
        shutil.copy2(root / filename, source / filename)
    for directory in ("neocortex", "tools", "docs"):
        shutil.copytree(
            root / directory, source / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    second = work / "source-two"
    shutil.copytree(source, second)
    first_sdist = _backend(source, work / "first-build", "sdist")
    second_sdist = _backend(second, work / "second-build", "sdist")
    return work, first_sdist, second_sdist


def test_actual_sdist_has_import_complete_release_tool_without_git(actual_sdists) -> None:
    work, first, _second = actual_sdists
    report = validate_sdist(first)
    assert report.kind == "sdist"
    extracted = work / "extracted"
    with tarfile.open(first) as archive:
        archive.extractall(extracted, filter="data")
    source = next(extracted.iterdir())
    assert not (source / ".git").exists()
    assert (source / "tools/pip_bootstrap.py").is_file()
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        (sys.executable, "-I", "-B", os.fspath(source / "tools/release_linux.py"), "--help"),
        cwd=work, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "install" in result.stdout and "verify" in result.stdout
    assert validate_wheel(_backend(source, work / "extracted-wheel", "wheel")).kind == "wheel"


def test_explicit_canonicalization_is_byte_stable_for_two_real_sdists(actual_sdists) -> None:
    work, first, second = actual_sdists
    left = work / "canonical-left" / first.name
    right = work / "canonical-right" / first.name
    left.parent.mkdir()
    right.parent.mkdir()

    canonicalize_sdist(first, second, left)
    canonicalize_sdist(second, first, right)

    assert left.read_bytes() == right.read_bytes()
    assert int.from_bytes(left.read_bytes()[4:8], "little") == SOURCE_DATE_EPOCH
