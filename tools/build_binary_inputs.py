"""Build pinned source-only transitive inputs into locally verified wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

SOURCE_ONLY_PROJECTS = ("yattag",)
SOURCE_ONLY_REQUIREMENTS = Path(__file__).with_name("source_only_requirements.txt")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    arguments: Sequence[os.PathLike[str] | str], **kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(map(os.fspath, arguments)),
        check=True,
        text=True,
        **kwargs,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_source_only_wheels(
    python: Path,
    wheel_directory: Path,
    constraints: Path,
    *,
    requirements: Path = SOURCE_ONLY_REQUIREMENTS,
    runner: CommandRunner = _run,
) -> tuple[Path, ...]:
    """Use pip's wheel builder, never its installer, for pinned source-only inputs."""

    wheel_directory.mkdir(parents=True, exist_ok=True)
    runner(
        (
            python,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            wheel_directory,
            "--constraint",
            constraints,
            "--require-hashes",
            "--requirement",
            requirements,
        ),
        timeout=900,
    )
    wheels: list[Path] = []
    for project in SOURCE_ONLY_PROJECTS:
        candidates = tuple(wheel_directory.glob(f"{project}-*.whl"))
        if len(candidates) != 1 or not candidates[0].is_file() or candidates[0].stat().st_size <= 0:
            raise RuntimeError(f"source-only wheel build failed for {project}")
        wheels.append(candidates[0])
    return tuple(wheels)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build_binary_inputs.py")
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        python = args.python.expanduser().absolute()
        if not python.is_file():
            raise OSError(f"Python executable is unavailable: {python}")
        wheels = build_source_only_wheels(
            python,
            args.wheel_dir.resolve(strict=False),
            args.constraints.resolve(strict=True),
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR binary-inputs {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = {
        "schema_version": 1,
        "kind": "binary_input_wheels",
        "wheels": [{"filename": wheel.name, "sha256": _sha256(wheel)} for wheel in wheels],
    }
    if not all(_SHA256.fullmatch(str(item["sha256"])) for item in payload["wheels"]):
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SOURCE_ONLY_PROJECTS",
    "SOURCE_ONLY_REQUIREMENTS",
    "build_source_only_wheels",
    "main",
]
