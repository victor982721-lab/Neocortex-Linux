"""Run a bounded two-pass replay benchmark against an installed NeoCortex.

The benchmark copies only the authored synthetic fixtures into a temporary
corpus, gives the installed launcher a private HOME/XDG state, and reports the
canonical per-route replay counters from ``--status-json``.  It deliberately
does not import the product from the checkout, inspect a user SQLite owner, or
write a receipt inside the repository.

The default profile contains 28 heterogeneous files (text, code, PDF, DOCX,
ODT, image, video, audio, and ZIP).  Audio is present in the
fixture but is not selected by default because it needs an existing local
Whisper model; add ``audio`` explicitly only when that model is provisioned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import resource
import shlex
import shutil
import subprocess
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "headless_product"
BENCHMARK_SCHEMA = "neocortex.route-replay-benchmark/v1"
DEFAULT_ROUTES = (
    "text",
    "code",
    "pdf",
    "docx",
    "office",
    "image",
    "video",
    "archive",
)
FIXTURE_GROUPS = (
    "base",
    "documents",
    "image",
    "video",
    "video_low_fps",
    "audio",
)
MIN_FIXTURES = 20
MAX_FIXTURES = 50
PROCESS_TIMEOUT_SECONDS = 180.0
SYSTEM_TEMP_ROOT = Path("/tmp").resolve()


class BenchmarkConfigurationError(ValueError):
    """The requested benchmark is outside its bounded, isolated contract."""


class BenchmarkExecutionError(RuntimeError):
    """An installed-product or status subprocess did not complete safely."""


@dataclass(frozen=True, slots=True)
class FixtureManifest:
    files: int
    bytes: int
    digest_sha256: str
    groups: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Execution:
    command: tuple[str, ...]
    completed: subprocess.CompletedProcess[str]
    wall_seconds: float
    cpu_seconds: float
    user_seconds: float
    system_seconds: float


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkConfigurationError("timeout must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > PROCESS_TIMEOUT_SECONDS:
        raise BenchmarkConfigurationError(
            f"timeout must be greater than 0 and at most {PROCESS_TIMEOUT_SECONDS:g} seconds"
        )
    return result


def _validate_routes(routes: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(route.strip().casefold() for route in routes if route.strip())
    if not selected:
        raise BenchmarkConfigurationError("at least one route is required")
    if len(set(selected)) != len(selected):
        raise BenchmarkConfigurationError("routes must not be repeated")
    available = {
        "pdf",
        "docx",
        "office",
        "archive",
        "text",
        "audio",
        "video",
        "image",
        "code",
    }
    unknown = sorted(set(selected) - available)
    if unknown:
        raise BenchmarkConfigurationError(f"unknown routes: {', '.join(unknown)}")
    return selected


def _zip_fixture_bytes() -> bytes:
    """Return one deterministic ZIP so the archive route is represented."""

    from io import BytesIO

    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            ("notes/fixture.txt", b"NeoCortex synthetic archive fixture\n"),
            ("code/fixture.py", b"def transformador():\n    return 'fixture'\n"),
        ):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
    return stream.getvalue()


def _hash_tree(root: Path) -> tuple[int, int, str]:
    digest = hashlib.sha256()
    files = bytes_total = 0
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        files += 1
        bytes_total += len(payload)
    return files, bytes_total, digest.hexdigest()


def build_fixture(root: Path) -> FixtureManifest:
    """Copy the fixed synthetic fixture profile below ``root`` only."""

    root = Path(root)
    if not root.is_absolute():
        raise BenchmarkConfigurationError("fixture root must be absolute")
    if root.exists() or root.is_symlink():
        raise BenchmarkConfigurationError("fixture root must not already exist")
    try:
        Path(os.path.realpath(root.parent)).relative_to(SYSTEM_TEMP_ROOT)
    except ValueError as exc:
        raise BenchmarkConfigurationError(
            "fixture root must be below the system temporary directory"
        ) from exc
    if not FIXTURE_ROOT.is_dir():
        raise BenchmarkConfigurationError(f"fixture source is missing: {FIXTURE_ROOT}")

    root.mkdir(mode=0o700)
    for group in FIXTURE_GROUPS:
        source = FIXTURE_ROOT / group
        if not source.is_dir():
            raise BenchmarkConfigurationError(f"fixture group is missing: {group}")
        shutil.copytree(
            source,
            root / group,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    archive_path = root / "archive" / "fixture.zip"
    archive_path.parent.mkdir(mode=0o700)
    archive_path.write_bytes(_zip_fixture_bytes())

    counts = {
        group: sum(1 for path in (root / group).rglob("*") if path.is_file())
        for group in (*FIXTURE_GROUPS, "archive")
    }
    files, bytes_total, digest = _hash_tree(root)
    if not MIN_FIXTURES <= files <= MAX_FIXTURES:
        raise BenchmarkConfigurationError(
            f"fixture profile has {files} files; expected {MIN_FIXTURES}..{MAX_FIXTURES}"
        )
    return FixtureManifest(files, bytes_total, digest, counts)


def _resolve_launcher(value: str | Path | None) -> Path:
    candidate = Path(value).expanduser() if value is not None else None
    if candidate is None:
        environment_value = os.environ.get("NEOCORTEX_BENCHMARK_LAUNCHER")
        candidate = Path(environment_value).expanduser() if environment_value else None
    if candidate is None:
        discovered = shutil.which("Neocortex")
        if discovered is None:
            raise BenchmarkConfigurationError(
                "no Neocortex launcher found; pass --launcher with the installed executable"
            )
        candidate = Path(discovered)
    if not candidate.is_absolute():
        raise BenchmarkConfigurationError("--launcher must be an absolute path")
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise BenchmarkConfigurationError(f"launcher is not executable: {candidate}")

    # The convenience ~/.local/bin/Neocortex wrapper resets XDG_* to the
    # user's real directories. Resolve its literal exec target so this
    # benchmark cannot accidentally open a global SQLite owner.
    try:
        with candidate.open("r", encoding="utf-8") as stream:
            first_line = stream.readline()
    except UnicodeDecodeError:
        first_line = ""
    shebang_words = shlex.split(first_line[2:].strip()) if first_line.startswith("#!") else []
    shell_interpreter = (
        Path(shebang_words[-1]).name
        if shebang_words and shebang_words[0] != "/usr/bin/env"
        else (shebang_words[1] if len(shebang_words) > 1 else "")
    )
    if shell_interpreter in {"sh", "bash", "dash", "zsh", "ksh"}:
        lines = candidate.read_text(encoding="utf-8").splitlines()
        exec_lines = [line.strip() for line in lines if line.strip().startswith("exec ")]
        if len(exec_lines) != 1:
            raise BenchmarkConfigurationError(
                "shell launcher cannot be isolated; pass the direct installed executable"
            )
        target_words = shlex.split(exec_lines[0])
        if len(target_words) < 2 or target_words[1] in {"$@", '"$@"'}:
            raise BenchmarkConfigurationError(
                "shell launcher has no literal isolated target; pass the direct executable"
            )
        candidate = Path(target_words[1]).expanduser()
        if (
            not candidate.is_absolute()
            or not candidate.is_file()
            or not os.access(candidate, os.X_OK)
        ):
            raise BenchmarkConfigurationError("shell launcher target is not executable")
    return candidate


def _private_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PIP_CONFIG_FILE",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_NO_INDEX",
        "NEOCORTEX_CORPUS_ROOT",
    ):
        environment.pop(name, None)
    for variable, directory in (
        ("HOME", root / "home"),
        ("XDG_CONFIG_HOME", root / "config"),
        ("XDG_CACHE_HOME", root / "cache"),
        ("XDG_DATA_HOME", root / "data"),
        ("XDG_STATE_HOME", root / "state-home"),
        ("TMPDIR", root / "tmp"),
    ):
        directory.mkdir(mode=0o700)
        environment[variable] = str(directory)
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NEOCORTEX_PROGRESS_STREAM": "0",
            "QT_QPA_PLATFORM": "offscreen",
        }
    )
    return environment


def _command(
    launcher: Path,
    route: str,
    corpus: Path,
    state: Path,
) -> tuple[str, ...]:
    return (
        str(launcher),
        "--root",
        str(corpus),
        "--state-directory",
        str(state),
        "--route",
        route,
        "--no-document-catalog",
        "--strict-exit-codes",
        "--ocr",
        "never",
        "--pdf-workers",
        "1",
        "--ocr-workers",
        "1",
        "--text-max-count",
        "50",
        "--docx-max-count",
        "50",
        "--office-max-count",
        "50",
        "--archive-max-count",
        "50",
        "--image-max-count",
        "50",
        "--image-workers",
        "1",
        "--image-document-ocr",
        "never",
        "--video-max-count",
        "50",
        "--video-max-frames",
        "2",
        "--video-interval-seconds",
        "1",
        "--video-ocr",
        "never",
        "--code-max-count",
        "50",
        "--code-project-root",
        str(corpus / "base" / "code"),
    )


def _status_command(launcher: Path, state: Path) -> tuple[str, ...]:
    return (str(launcher), "--state-directory", str(state), "--status", "--status-json")


def _run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
) -> _Execution:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    wall_seconds = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    user_seconds = max(0.0, after.ru_utime - before.ru_utime)
    system_seconds = max(0.0, after.ru_stime - before.ru_stime)
    return _Execution(
        command,
        completed,
        wall_seconds,
        user_seconds + system_seconds,
        user_seconds,
        system_seconds,
    )


def _status_payload(execution: _Execution) -> dict[str, Any]:
    if execution.completed.returncode != 0:
        raise BenchmarkExecutionError(
            f"status command failed with exit={execution.completed.returncode}: "
            f"{execution.completed.stderr[-1800:]}"
        )
    candidates: list[dict[str, Any]] = []
    for line in execution.completed.stdout.splitlines():
        if line.lstrip().startswith("{"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                candidates.append(value)
    if not candidates:
        raise BenchmarkExecutionError("status command did not emit JSON")
    # ``--status-json`` emits newest-to-oldest rows, one JSON object per line.
    # The first row is the run just measured; selecting the last row would
    # silently report the first pass again after a replay.
    payload = candidates[0]
    if payload.get("status") != "completed":
        raise BenchmarkExecutionError(f"status is not terminal: {payload.get('status')!r}")
    return payload


def _route_metrics(payload: Mapping[str, Any], route: str) -> dict[str, Any]:
    rows = payload.get("routes")
    if not isinstance(rows, list):
        raise BenchmarkExecutionError("status JSON has no route list")
    row = next(
        (value for value in rows if isinstance(value, dict) and value.get("route_name") == route),
        None,
    )
    if row is None:
        raise BenchmarkExecutionError(f"status JSON omitted selected route {route!r}")

    def counter(name: str) -> int:
        value = row.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BenchmarkExecutionError(f"route {route!r} has invalid {name}: {value!r}")
        return value

    candidates = counter("candidates")
    cache_hits = counter("cache_hits")
    cached_errors = counter("cached_errors")
    new_work = counter("new_work")
    reused = cache_hits + cached_errors
    started_ns = row.get("started_ns")
    completed_ns = row.get("completed_ns")
    route_wall_seconds = None
    if isinstance(started_ns, int) and isinstance(completed_ns, int) and completed_ns >= started_ns:
        route_wall_seconds = (completed_ns - started_ns) / 1_000_000_000
    return {
        "status": row.get("status"),
        "candidates": candidates,
        "processed": counter("processed"),
        "cache_hits": cache_hits,
        "cached_errors": cached_errors,
        "reuse": reused,
        "new_work": new_work,
        "replay_status": row.get("replay_status"),
        "replayability": row.get("replayability", row.get("resume_capability")),
        "route_wall_seconds": route_wall_seconds,
    }


def _failure(execution: _Execution) -> BenchmarkExecutionError:
    completed = execution.completed
    return BenchmarkExecutionError(
        f"product command failed with exit={completed.returncode}; "
        f"stderr={completed.stderr[-2400:]}"
    )


def run_benchmark(
    *,
    launcher: str | Path | None = None,
    routes: Sequence[str] = DEFAULT_ROUTES,
    timeout_seconds: float = PROCESS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run every selected route twice over one isolated synthetic corpus."""

    selected_routes = _validate_routes(routes)
    timeout = _positive_timeout(timeout_seconds)
    direct_launcher = _resolve_launcher(launcher)
    started = time.perf_counter()
    with TemporaryDirectory(prefix="neocortex-route-replay-", dir=SYSTEM_TEMP_ROOT) as temporary:
        root = Path(temporary)
        corpus = root / "corpus"
        state = root / "state"
        work = root / "work"
        state.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        manifest = build_fixture(corpus)
        environment = _private_environment(root)
        executions: list[dict[str, Any]] = []
        for run_index in (1, 2):
            for route in selected_routes:
                product = _run(
                    _command(direct_launcher, route, corpus, state),
                    cwd=work,
                    environment=environment,
                    timeout=timeout,
                )
                if product.completed.returncode != 0:
                    raise _failure(product)
                status_execution = _run(
                    _status_command(direct_launcher, state),
                    cwd=work,
                    environment=environment,
                    timeout=timeout,
                )
                payload = _status_payload(status_execution)
                metrics = _route_metrics(payload, route)
                metrics.update(
                    {
                        "run": run_index,
                        "wall_seconds": product.wall_seconds,
                        "cpu_seconds": product.cpu_seconds,
                        "cpu_user_seconds": product.user_seconds,
                        "cpu_system_seconds": product.system_seconds,
                    }
                )
                executions.append({"run": run_index, "route": route, "metrics": metrics})

        by_route = {
            route: {
                f"run_{run_index}": next(
                    item["metrics"]
                    for item in executions
                    if item["route"] == route and item["run"] == run_index
                )
                for run_index in (1, 2)
            }
            for route in selected_routes
        }
        replay_checks = {
            route: {
                "first_new_work": by_route[route]["run_1"]["new_work"],
                "second_new_work": by_route[route]["run_2"]["new_work"],
                "second_reuse": by_route[route]["run_2"]["reuse"],
                "second_full_replay": (
                    by_route[route]["run_2"]["candidates"] == by_route[route]["run_2"]["reuse"]
                    and by_route[route]["run_2"]["new_work"] == 0
                ),
            }
            for route in selected_routes
        }
        return {
            "schema": BENCHMARK_SCHEMA,
            "launcher": str(direct_launcher),
            "routes": list(selected_routes),
            "fixture": {
                "files": manifest.files,
                "bytes": manifest.bytes,
                "digest_sha256": manifest.digest_sha256,
                "groups": manifest.groups,
                "temporary": True,
            },
            "executions": executions,
            "by_route": by_route,
            "replay_checks": replay_checks,
            "elapsed_seconds": time.perf_counter() - started,
            "state_is_temporary": True,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--launcher",
        type=Path,
        help=(
            "absolute path to the direct installed Neocortex executable; the convenience "
            "shell wrapper is resolved only when its target is literal"
        ),
    )
    parser.add_argument(
        "--routes",
        default=",".join(DEFAULT_ROUTES),
        help="comma-separated route names; audio is opt-in because it needs a local model",
    )
    parser.add_argument("--timeout-seconds", type=float, default=PROCESS_TIMEOUT_SECONDS)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    parsed = parser.parse_args(arguments)
    try:
        report = run_benchmark(
            launcher=parsed.launcher,
            routes=tuple(parsed.routes.split(",")),
            timeout_seconds=parsed.timeout_seconds,
        )
    except (
        BenchmarkConfigurationError,
        BenchmarkExecutionError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "BENCHMARK_SCHEMA",
    "DEFAULT_ROUTES",
    "FIXTURE_GROUPS",
    "FixtureManifest",
    "build_fixture",
    "main",
    "run_benchmark",
)
