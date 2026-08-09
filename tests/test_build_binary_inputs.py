"""Deterministic source-only dependency wheel normalization."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.build_binary_inputs import (
    SOURCE_ONLY_REQUIREMENTS,
    build_source_only_wheels,
)


def test_source_only_dependency_is_hash_pinned_and_built_without_installing(
    tmp_path: Path,
) -> None:
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("yattag==1.16.1\n", encoding="utf-8")
    observed: tuple[object, ...] = ()

    def runner(arguments, **kwargs):
        nonlocal observed
        observed = tuple(arguments)
        wheel_directory = Path(observed[observed.index("--wheel-dir") + 1])
        (wheel_directory / "yattag-1.16.1-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(observed, 0, "", "")

    wheels = build_source_only_wheels(
        Path("/fixture/python"),
        tmp_path / "wheelhouse",
        constraints,
        runner=runner,
    )

    assert [wheel.name for wheel in wheels] == ["yattag-1.16.1-py3-none-any.whl"]
    assert observed[:4] == (Path("/fixture/python"), "-m", "pip", "wheel")
    assert "--no-deps" in observed
    assert "--no-build-isolation" in observed
    assert "--require-hashes" in observed
    requirement = SOURCE_ONLY_REQUIREMENTS.read_text(encoding="utf-8")
    assert "yattag==1.16.1" in requirement
    assert "sha256:baa8f254e7ea5d3e0618281ad2ff5610e0e5360b3608e695c29bfb3b29d051f4" in requirement
