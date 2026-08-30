"""Regression coverage for code-route candidate selection semantics."""
# region [00] Contexto del módulo
# Módulo: tests/test_code_route_selection.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

from neocortex.deduplication import FileSnapshot
from neocortex.code.code_contracts import CodeRouteConfig
from neocortex.code.code_route import CodeRoute
from neocortex.safety.route_filters import CandidateSelection
# endregion [01]

# region [02] Implementación


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
    def __init__(self, paths: Iterable[Path]):
        self.paths = tuple(paths)

    def snapshots(self, _scan_id: int):
        return iter(_snapshot(path) for path in self.paths)


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
        raise AssertionError("selection fixture route must not fail")


def _config(tmp_path: Path, selection: CandidateSelection) -> CodeRouteConfig:
    return CodeRouteConfig(
        state_path=tmp_path / "state" / "code.sqlite3",
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        selection=selection,
    )


def test_path_only_selection_can_publish_a_first_observation(tmp_path: Path) -> None:
    source = tmp_path / "selected.py"
    source.write_text("def selected():\n    return True\n", encoding="utf-8")
    selection = CandidateSelection.from_values(paths=(source,))

    summary = CodeRoute(
        _config(tmp_path, selection),
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.candidates == 1
    assert summary.processed == 1


def test_status_and_diagnostic_selection_uses_current_code_state(tmp_path: Path) -> None:
    valid = tmp_path / "valid.py"
    invalid = tmp_path / "invalid.py"
    valid.write_text("def valid():\n    return True\n", encoding="utf-8")
    invalid.write_text("def invalid(:\n    pass\n", encoding="utf-8")
    inventory = _Inventory((valid, invalid))
    CodeRoute(
        _config(tmp_path, CandidateSelection()),
        inventory,
        _FrameworkState(),
        1,
        1,
    ).run()

    selected = CandidateSelection.from_values(
        statuses=("partial",),
        error_types=("python_parse_error",),
    )
    summary = CodeRoute(
        _config(tmp_path, selected),
        inventory,
        _FrameworkState(),
        2,
        2,
    ).run()

    assert summary.candidates == 1
    assert summary.cache_hits == 1
    assert summary.processed == 0


def test_project_scope_keeps_owned_files_and_rejects_profile_noise(
    tmp_path: Path,
) -> None:
    project = tmp_path / "Projects" / "owned"
    outside = tmp_path / "Texto"
    files = {
        project / "pyproject.toml": "[project]\nname='owned'\n",
        project / "src" / "owned.py": "def owned():\n    return True\n",
        project / "docs" / "README.md": "# Owned project\n",
        project / "settings.json": '{"tool": "owned"}\n',
        project / "node_modules" / "dep" / "package.json": '{"name":"dep"}\n',
        project / "node_modules" / "dep" / "index.js": "export const dep = 1;\n",
        project / ".venv" / "Lib" / "site-packages" / "installed.py": "VALUE = 1\n",
        project / "build" / "generated.py": "VALUE = 2\n",
        project / ".pytest_cache" / "cached.py": "VALUE = 3\n",
        outside / "export.jsonl": '{"not":"code"}\n',
        outside / "loose.py": "VALUE = 4\n",
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    config = CodeRouteConfig(
        state_path=tmp_path / "state" / "code.sqlite3",
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        candidate_scope="projects",
        include_generated=False,
        include_vendored=False,
    )
    summary = CodeRoute(
        config,
        _Inventory(files),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.project_scope_enabled == 1
    assert summary.project_roots == 1
    assert summary.candidates == 4
    assert summary.processed == 4
    assert summary.outside_project_skips == 2
    assert summary.dependency_skips == 3
    assert summary.generated_scope_skips == 1
    assert summary.cache_skips == 1


def test_explicit_project_allowlist_rejects_other_marked_projects_and_labs(
    tmp_path: Path,
) -> None:
    neocortex = tmp_path / "Neocortex" / "Repository"
    bitacoras = tmp_path / "Frameworks" / "Bitacoras-EPS"
    unwanted = tmp_path / "Other" / "broken-project"
    files = {
        neocortex / "pyproject.toml": "[project]\nname='neocortex'\n",
        neocortex / "src" / "owned.py": "OWNED = True\n",
        bitacoras / "pyproject.toml": "[project]\nname='bitacoras'\n",
        bitacoras / "src" / "owned.py": "OWNED = True\n",
        unwanted / "package.json": '{"name":"unwanted"}\n',
        unwanted / "broken.js": "broken syntax {{\n",
        neocortex / "Laboratory" / "pyproject.toml": "not valid toml\n",
        neocortex / "Laboratory" / "broken.py": "def broken(:\n",
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    config = CodeRouteConfig(
        state_path=tmp_path / "state" / "code.sqlite3",
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        candidate_scope="projects",
        include_generated=False,
        include_vendored=False,
        explicit_project_roots=(neocortex, bitacoras),
    )
    summary = CodeRoute(
        config,
        _Inventory(files),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.project_roots == 2
    assert summary.candidates == 4
    assert summary.outside_project_skips == 2
    assert summary.generated_scope_skips == 2
    assert summary.errors == 0


def test_explicit_path_overrides_project_discovery(tmp_path: Path) -> None:
    source = tmp_path / "loose.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    selection = CandidateSelection.from_values(paths=(source,))
    config = CodeRouteConfig(
        state_path=tmp_path / "state" / "code.sqlite3",
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        candidate_scope="projects",
        include_generated=False,
        include_vendored=False,
        selection=selection,
    )

    summary = CodeRoute(
        config,
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.project_scope_enabled == 0
    assert summary.project_roots == 0
    assert summary.candidates == 1
    assert summary.processed == 1


def test_project_scope_reconciles_broad_state_without_discarding_history(
    tmp_path: Path,
) -> None:
    project = tmp_path / "Projects" / "owned"
    manifest = project / "pyproject.toml"
    owned = project / "owned.py"
    loose = tmp_path / "Texto" / "loose.py"
    for path, content in (
        (manifest, "[project]\nname='owned'\n"),
        (owned, "OWNED = True\n"),
        (loose, "LOOSE = True\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    state_path = tmp_path / "state" / "code.sqlite3"
    broad = CodeRouteConfig(
        state_path=state_path,
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        candidate_scope="broad",
        include_generated=False,
        include_vendored=False,
    )
    inventory = _Inventory((manifest, owned, loose))
    first = CodeRoute(broad, inventory, _FrameworkState(), 1, 1).run()

    narrowed = CodeRouteConfig(
        state_path=state_path,
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        candidate_scope="projects",
        include_generated=False,
        include_vendored=False,
    )
    second = CodeRoute(narrowed, inventory, _FrameworkState(), 2, 2).run()

    assert first.candidates == 3
    assert second.candidates == 2
    assert second.cache_hits == 2
    assert second.invalidated_versions == 1
    with sqlite3.connect(state_path) as connection:
        current_paths = {
            str(row[0])
            for row in connection.execute("SELECT current_path FROM files WHERE status='current'")
        }
        loose_history = connection.execute(
            """SELECT COUNT(*) FROM file_versions
            WHERE path_observed=? AND invalidated_ns IS NOT NULL""",
            (str(loose),),
        ).fetchone()

    assert current_paths == {str(manifest), str(owned)}
    assert loose_history == (1,)


# endregion [02]
