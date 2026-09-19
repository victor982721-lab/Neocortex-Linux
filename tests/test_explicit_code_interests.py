"""Bounded admission tests for NeoCortex's explicit Code project interests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Mapping

from neocortex.code.code_contracts import CodeRouteConfig
from neocortex.code.code_route import CodeRoute
from neocortex.code.ingestion.code_candidate_scope import (
    ProjectCandidateScope,
    normalize_code_path,
)
from neocortex.deduplication import FileSnapshot
from neocortex.runtime.config.app_paths import default_code_project_roots
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.route_registry import (
    RouteExecutionContext,
    _code_route_workload,
)


def _snapshot(path: Path) -> FileSnapshot:
    observed = path.stat()
    return FileSnapshot(
        str(path),
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        getattr(observed, "st_birthtime_ns", observed.st_ctime_ns),
    )


class _Inventory:
    def __init__(self, paths: Iterable[Path]) -> None:
        self._paths = tuple(paths)

    def snapshots(self, _scan_id: int) -> Iterable[FileSnapshot]:
        return iter(_snapshot(path) for path in self._paths)


class _FrameworkState:
    def begin_route_phase(
        self,
        _run_id: int,
        _route: str,
        _phase: str,
        *,
        source_run_id: int | None = None,
    ) -> None:
        del source_run_id

    def complete_route_phase(
        self,
        _run_id: int,
        _route: str,
        _phase: str,
        summary: Mapping[str, object] | None = None,
    ) -> None:
        assert summary is not None

    def fail_route_phase(
        self,
        _run_id: int,
        _route: str,
        _phase: str,
        _exc: BaseException,
    ) -> None:
        raise AssertionError("Code scope fixture route must not fail")


def _code_config(
    tmp_path: Path,
    *,
    roots: tuple[Path, ...] = (),
    scope: str = "projects",
) -> CodeRouteConfig:
    return CodeRouteConfig(
        state_path=tmp_path / "state" / "code.sqlite3",
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        candidate_scope=scope,  # type: ignore[arg-type]
        explicit_project_roots=roots,
        include_generated=False,
        include_vendored=False,
    )


def _write(path: Path, text: str = "VALUE = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_the_three_exact_owned_code_projects() -> None:
    home = Path.home()
    assert default_code_project_roots() == (
        home / "Neocortex" / "Repository",
        home / "MTF",
        home / "Documentos" / "ANDRITZ" / "Bitacoras-EPS",
    )


def test_explicit_owned_root_admits_code_and_unrelated_markers_stay_outside(
    tmp_path: Path,
) -> None:
    owned = tmp_path / "owned-project"
    unrelated = tmp_path / "unrelated-project"
    files = (
        _write(owned / "pyproject.toml", "[project]\nname='owned'\n"),
        _write(owned / "src" / "owned.py"),
        _write(unrelated / "package.json", '{"name":"unrelated"}\n'),
        _write(unrelated / "broken.js", "broken syntax {{\n"),
        _write(unrelated / "solution.sln", "Microsoft Visual Studio Solution File\n"),
    )

    summary = CodeRoute(
        _code_config(tmp_path, roots=(owned,)),
        _Inventory(files),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.project_scope_enabled == 1
    assert summary.project_roots == 1
    assert summary.candidates == 2
    assert summary.processed == 2
    assert summary.outside_project_skips == 3


def test_empty_allowlist_does_not_discover_marker_projects(tmp_path: Path) -> None:
    project = tmp_path / "looks-like-a-project"
    files = (
        _write(project / "pyproject.toml", "[project]\nname='unowned'\n"),
        _write(project / "module.py"),
        _write(project / "package.json", '{"name":"unowned"}\n'),
    )

    summary = CodeRoute(
        _code_config(tmp_path),
        _Inventory(files),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.project_scope_enabled == 1
    assert summary.project_roots == 0
    assert summary.candidates == 0
    assert summary.processed == 0
    assert summary.outside_project_skips == len(files)


def test_disjoint_allowlist_does_not_broaden_from_markers(tmp_path: Path) -> None:
    configured = tmp_path / "configured-project"
    observed = tmp_path / "other-project"
    marker = _write(observed / "pyproject.toml", "[project]\nname='other'\n")
    source = _write(observed / "module.py")

    scope = ProjectCandidateScope.discover(
        (str(marker), str(source)),
        include_generated=False,
        include_vendored=False,
        explicit_roots=(configured,),
    )

    assert scope.roots == (str(configured),)
    assert scope.decision(marker) == "outside_project"
    assert scope.decision(source) == "outside_project"


def test_spoofed_canonical_basename_does_not_match_default_root(tmp_path: Path) -> None:
    spoofed = tmp_path / "Neocortex" / "Repository"
    marker = _write(spoofed / "pyproject.toml", "[project]\nname='spoof'\n")
    source = _write(spoofed / "module.py")

    scope = ProjectCandidateScope.discover(
        (str(marker), str(source)),
        include_generated=False,
        include_vendored=False,
        explicit_roots=default_code_project_roots(),
    )

    assert normalize_code_path(spoofed) not in {
        normalize_code_path(root) for root in default_code_project_roots()
    }
    assert scope.decision(marker) == "outside_project"
    assert scope.decision(source) == "outside_project"


def test_parent_and_symlinked_root_normalization_keeps_prefix_siblings_outside(
    tmp_path: Path,
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    alias = tmp_path / "owned-alias"
    alias.symlink_to(owned, target_is_directory=True)
    escaped = _write(tmp_path / "ownedish" / "module.py")
    configured_root = alias / ".." / "owned"

    scope = ProjectCandidateScope.discover(
        (escaped,),
        include_generated=False,
        include_vendored=False,
        explicit_roots=(configured_root,),
    )

    assert scope.roots == (str(owned),)
    assert scope.decision(escaped) == "outside_project"


def test_broad_scope_remains_an_explicit_opt_in(tmp_path: Path) -> None:
    project = tmp_path / "unowned-project"
    files = (
        _write(project / "package.json", '{"name":"unowned"}\n'),
        _write(project / "module.js", "export const value = 1;\n"),
    )

    summary = CodeRoute(
        _code_config(tmp_path, scope="broad"),
        _Inventory(files),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.project_scope_enabled == 0
    assert summary.candidates == 2
    assert summary.processed == 2


def test_code_workload_matches_project_scope_route_selection(tmp_path: Path) -> None:
    owned = tmp_path / "owned-project"
    unrelated = tmp_path / "unrelated-project"
    files = (
        _write(owned / "pyproject.toml", "[project]\nname='owned'\n"),
        _write(owned / "module.py"),
        _write(unrelated / "package.json", '{"name":"unrelated"}\n'),
        _write(unrelated / "module.js", "export const value = 1;\n"),
    )
    inventory = _Inventory(files)
    config = FrameworkConfig(
        root=tmp_path,
        state_directory=tmp_path / "state",
        route="code",
        code_candidate_scope="projects",
        code_project_roots=(owned,),
    )
    context = RouteExecutionContext(
        config=config,
        root=tmp_path,
        framework_state=SimpleNamespace(),
        run_id=1,
        scan_id=1,
        progress=None,
        resource_coordinator=None,
        cancellation=CancellationToken(),
        inventory_view=inventory,
    )

    workload = _code_route_workload(context)
    summary = CodeRoute(
        _code_config(tmp_path, roots=(owned,)),
        inventory,
        _FrameworkState(),
        1,
        1,
    ).run()

    assert workload == (
        summary.candidates,
        sum(_snapshot(path).size for path in files[:2]),
    )


def test_strict_zero_candidate_run_retires_previous_broad_current_rows(
    tmp_path: Path,
) -> None:
    project = tmp_path / "previously-indexed-project"
    files = (
        _write(project / "package.json", '{"name":"previous"}\n'),
        _write(project / "module.js", "export const value = 1;\n"),
    )
    inventory = _Inventory(files)
    state_path = tmp_path / "state" / "code.sqlite3"

    broad = CodeRouteConfig(
        state_path=state_path,
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        candidate_scope="broad",
        include_generated=False,
        include_vendored=False,
    )
    broad_summary = CodeRoute(
        broad,
        inventory,
        _FrameworkState(),
        1,
        1,
    ).run()

    strict = _code_config(tmp_path, roots=())
    strict_summary = CodeRoute(
        strict,
        inventory,
        _FrameworkState(),
        2,
        2,
    ).run()

    assert broad_summary.candidates == 2
    assert strict_summary.candidates == 0
    assert broad_summary.processing_signature != strict_summary.processing_signature
    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM files WHERE status='current'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM file_versions WHERE invalidated_ns IS NOT NULL"
        ).fetchone() == (2,)
