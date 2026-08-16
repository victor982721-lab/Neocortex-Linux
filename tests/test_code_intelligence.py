"""Integrated, deterministic coverage for structured source-code intelligence."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import AbstractContextManager
from dataclasses import replace
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable, Literal, Mapping, ParamSpec, TypeVar

import pytest

import _04_Nucleo_Operativo.code_route as code_route_module
import _04_Nucleo_Operativo.code_state as code_state_module
from _02_Deduplicacion import FileSnapshot
from _04_Nucleo_Operativo.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from _04_Nucleo_Operativo.code_analyzers import AnalyzerRegistry, AnalyzerSpec
from _04_Nucleo_Operativo.code_contracts import (
    AnalysisStatus,
    ArtifactKind,
    CodeAnalysis,
    CodeFileInput,
    CodeRouteConfig,
    CodeSearchQuery,
)
from _04_Nucleo_Operativo.code_detection import classify_artifact, decode_text
from _04_Nucleo_Operativo.code_generic import GenericAnalyzer
from _04_Nucleo_Operativo.code_projects import list_projects, reconstruct_project
from _04_Nucleo_Operativo.code_route import CodeRoute
from _04_Nucleo_Operativo.code_schema import (
    CODE_SCHEMA_VERSION,
    checkpoint_code_wal,
    code_database,
    connect_code_state,
    initialize_code_state,
    remove_checkpointed_code_sidecars,
)
from _04_Nucleo_Operativo.code_search import search_code
from _04_Nucleo_Operativo.code_state import (
    CODE_GRAPH_RESOLVER_SIGNATURE,
    CodeState,
)
from _04_Nucleo_Operativo.semantic_sources import iter_text_source_records
from _04_Nucleo_Operativo.sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
)


def test_quiescent_code_reads_do_not_recreate_sqlite_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    initialize_code_state(database)
    writer = connect_code_state(database, create=False)
    try:
        checkpoint_code_wal(writer)
    finally:
        writer.close()
    remove_checkpointed_code_sidecars(database)
    sidecars = tuple(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm"))
    assert not any(path.exists() for path in sidecars)

    with code_database(database, readonly=True) as connection:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == (
            CODE_SCHEMA_VERSION
        )
    assert (
        search_code(
            database,
            CodeSearchQuery(text="absent", modes=("literal",), limit=5),
        )
        == ()
    )
    assert list_projects(database) == ()

    assert not any(path.exists() for path in sidecars)


def test_readonly_code_path_never_removes_active_writer_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "code.sqlite3"
    initialize_code_state(database)
    writer = connect_code_state(database, create=False)
    try:
        writer.execute(
            """INSERT INTO metadata(key,value) VALUES('reader-fixture','current')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value"""
        )
        writer.commit()
        sidecars = tuple(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm"))
        assert all(path.exists() for path in sidecars)

        with code_database(database, readonly=True) as connection:
            assert (
                connection.execute(
                    "SELECT value FROM metadata WHERE key='reader-fixture'"
                ).fetchone()[0]
                == "current"
            )

        assert all(path.exists() for path in sidecars)
    finally:
        writer.close()


# region [01] Deterministic collaborators


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

    def snapshots(self, scan_id: int):
        del scan_id
        return iter(_snapshot(path) for path in self.paths)


class _FrameworkState:
    def __init__(self):
        self.phases: list[tuple[str, str]] = []

    def begin_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        *,
        source_run_id: int | None = None,
    ) -> None:
        del run_id, route_name, source_run_id
        self.phases.append((phase_name, "running"))

    def complete_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        summary: Mapping[str, object] | None = None,
    ) -> None:
        del run_id, route_name
        assert summary is not None
        self.phases.append((phase_name, "completed"))

    def fail_route_phase(
        self,
        run_id: int,
        route_name: str,
        phase_name: str,
        exc: BaseException,
    ) -> None:
        del run_id, route_name, exc
        self.phases.append((phase_name, "failed"))


def _config(
    tmp_path: Path,
    *,
    max_file_bytes: int = 1024 * 1024,
    max_text_chars: int = 100_000,
    chunk_chars: int = 1024,
    cache_validation: Literal["metadata", "full"] = "metadata",
    include_generated: bool = True,
    include_vendored: bool = True,
) -> CodeRouteConfig:
    return CodeRouteConfig(
        state_path=tmp_path / "state" / "code.sqlite3",
        dedup_path=tmp_path / "state" / "dedup.sqlite3",
        max_file_bytes=max_file_bytes,
        max_text_chars=max_text_chars,
        chunk_chars=chunk_chars,
        cache_validation=cache_validation,
        include_generated=include_generated,
        include_vendored=include_vendored,
    )


def _track_graph_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    calls: list[int] = []
    original = CodeState.finalize_graph

    def tracked(
        state: CodeState,
        framework_run_id: int,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> int:
        calls.append(framework_run_id)
        return original(
            state,
            framework_run_id,
            cancellation_check=cancellation_check,
        )

    monkeypatch.setattr(CodeState, "finalize_graph", tracked)
    return calls


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _observed_call(
    events: list[str],
    label: str,
    callback: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(callback)
    def observed(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        events.append(label)
        return callback(*args, **kwargs)

    return observed


# endregion [01]


# region [02] Detection and decoding


@pytest.mark.parametrize(
    ("name", "text", "language", "kind"),
    (
        ("worker.py", "def run():\n    pass\n", "python", ArtifactKind.SOURCE),
        ("lib.rs", "pub fn run() {}\n", "rust", ArtifactKind.SOURCE),
        ("Cargo.toml", "[package]\nname='x'\n", "toml", ArtifactKind.MANIFEST),
        ("settings.yaml", "enabled: true\n", "yaml", ArtifactKind.CONFIG),
        ("README.md", "```python\npass\n```\n", "markdown", ArtifactKind.DOCUMENTATION),
        (
            "generated/client.ts",
            "// generated file\n",
            "typescript",
            ArtifactKind.GENERATED,
        ),
        ("vendor/lib.go", "package lib\n", "go", ArtifactKind.VENDORED),
    ),
)
def test_artifact_detection_separates_textual_roles(
    name: str,
    text: str,
    language: str,
    kind: ArtifactKind,
) -> None:
    classification = classify_artifact(name, text)

    assert classification.language == language
    assert classification.artifact_kind is kind
    assert classification.evidence


def test_decoding_preserves_declared_and_non_utf8_text() -> None:
    python_text, python_encoding, evidence = decode_text(
        b"# coding: cp1252\nlabel = 'Espa\xf1a'\n", "legacy.py"
    )
    fallback_text, fallback_encoding, _ = decode_text(b"caf\xe9\n", "notes.txt")

    assert "España" in python_text
    assert python_encoding == "cp1252"
    assert evidence == ("encoding:pep263:cp1252",)
    assert fallback_text == "café\n"
    assert fallback_encoding == "cp1252"


# endregion [02]


# region [03] Incremental route, diagnostics and search


def test_route_is_incremental_searchable_and_does_not_modify_sources(
    tmp_path: Path,
) -> None:
    project = tmp_path / "scattered" / "alpha"
    project.mkdir(parents=True)
    sources = {
        project / "pyproject.toml": "[project]\nname='alpha'\nversion='1'\n",
        project / "db.py": (
            "import sqlite3\n\n"
            "def validate_sqlite_access(path: str) -> bool:\n"
            '    """Validate access to SQLite."""\n'
            "    with sqlite3.connect(path) as connection:\n"
            "        return connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'\n"
        ),
        project / "invalid.py": "def broken(:\n    pass\n",
    }
    for path, text in sources.items():
        path.write_text(text, encoding="utf-8")
    original = {path: path.read_bytes() for path in sources}
    config = _config(tmp_path)
    inventory = _Inventory(sources)
    framework_state = _FrameworkState()

    first = CodeRoute(config, inventory, framework_state, 1, 7).run()
    second = CodeRoute(config, inventory, framework_state, 2, 7).run()

    assert first.candidates == first.processed == 3
    assert first.symbols >= 2
    assert first.diagnostics >= 1
    assert first.projects == 1
    assert second.cache_hits == 3
    assert second.processed == 0
    assert second.diagnostics == first.diagnostics
    assert original == {path: path.read_bytes() for path in sources}
    assert not Path(f"{config.state_path}-wal").exists()
    assert not Path(f"{config.state_path}-shm").exists()
    assert framework_state.phases[-4:] == [
        ("analysis", "running"),
        ("analysis", "completed"),
        ("graph", "running"),
        ("graph", "completed"),
    ]

    hybrid = search_code(
        config.state_path,
        CodeSearchQuery(text="SQLite", modes=("hybrid",), limit=20),
    )
    definitions = search_code(
        config.state_path,
        CodeSearchQuery(text="validate_sqlite_access", modes=("definition",), limit=5),
    )
    diagnostics = search_code(
        config.state_path,
        CodeSearchQuery(
            diagnostic="python_parse_error", modes=("diagnostic",), limit=5
        ),
    )

    assert any(
        "fts" in hit.match_types and hit.path.endswith("db.py") for hit in hybrid
    )
    assert definitions[0].symbol == "db.validate_sqlite_access"
    assert diagnostics[0].path.endswith("invalid.py")
    assert diagnostics[0].analysis_status == "partial"
    semantic_records = tuple(iter_text_source_records(config.state_path.parent, "code"))
    assert semantic_records
    assert all(record.item.source_kind == "code" for record in semantic_records)
    assert any(
        record.section.provenance.get("language") == "python"
        and "validate_sqlite_access" in record.section.text
        for record in semantic_records
    )


def test_route_preserves_analysis_graph_publication_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source = tmp_path / "project" / "worker.py"
    source.parent.mkdir()
    source.write_text("def run():\n    return True\n", encoding="utf-8")

    class ObservedFrameworkState(_FrameworkState):
        def begin_route_phase(
            self,
            run_id: int,
            route_name: str,
            phase_name: str,
            *,
            source_run_id: int | None = None,
        ) -> None:
            events.append(f"framework:begin:{phase_name}")
            super().begin_route_phase(
                run_id,
                route_name,
                phase_name,
                source_run_id=source_run_id,
            )

        def complete_route_phase(
            self,
            run_id: int,
            route_name: str,
            phase_name: str,
            summary: Mapping[str, object] | None = None,
        ) -> None:
            events.append(f"framework:complete:{phase_name}")
            super().complete_route_phase(run_id, route_name, phase_name, summary)

    monkeypatch.setattr(
        CodeState,
        "begin_run",
        _observed_call(events, "state:begin_run", CodeState.begin_run),
    )
    monkeypatch.setattr(
        CodeRoute,
        "_process_candidate",
        _observed_call(events, "candidate", CodeRoute._process_candidate),
    )
    monkeypatch.setattr(
        CodeState,
        "mark_missing",
        _observed_call(events, "state:mark_missing", CodeState.mark_missing),
    )
    monkeypatch.setattr(
        CodeState,
        "finalize_graph",
        _observed_call(events, "state:finalize_graph", CodeState.finalize_graph),
    )
    monkeypatch.setattr(
        CodeRoute,
        "_external_evidence",
        _observed_call(events, "external_evidence", CodeRoute._external_evidence),
    )
    monkeypatch.setattr(
        CodeState,
        "complete_run",
        _observed_call(events, "state:complete_run", CodeState.complete_run),
    )
    monkeypatch.setattr(
        code_route_module,
        "checkpoint_code_wal",
        _observed_call(
            events,
            "checkpoint_wal",
            code_route_module.checkpoint_code_wal,
        ),
    )
    monkeypatch.setattr(
        code_route_module,
        "remove_checkpointed_code_sidecars",
        _observed_call(
            events,
            "remove_sidecars",
            code_route_module.remove_checkpointed_code_sidecars,
        ),
    )

    summary = CodeRoute(
        _config(tmp_path),
        _Inventory((source,)),
        ObservedFrameworkState(),
        1,
        7,
    ).run()

    assert summary.processed == 1
    assert events == [
        "framework:begin:analysis",
        "state:begin_run",
        "candidate",
        "framework:complete:analysis",
        "framework:begin:graph",
        "state:mark_missing",
        "state:finalize_graph",
        "external_evidence",
        "state:complete_run",
        "framework:complete:graph",
        "checkpoint_wal",
        "remove_sidecars",
    ]


def test_relative_imports_resolve_by_package_path_without_basename_guessing(
    tmp_path: Path,
) -> None:
    sources = {
        tmp_path / "alpha" / "__init__.py": "",
        tmp_path / "alpha" / "models.py": "VALUE = 'alpha'\n",
        tmp_path / "alpha" / "helper.py": "def assist():\n    return True\n",
        tmp_path / "alpha" / "consumer.py": (
            "from .models import VALUE\n"
            "from . import helper\n"
            "\n"
            "def consume():\n"
            "    return VALUE, helper.assist()\n"
        ),
        tmp_path / "beta" / "__init__.py": "",
        tmp_path / "beta" / "models.py": "VALUE = 'beta'\n",
        tmp_path / "beta" / "consumer.py": "from .models import VALUE\n",
        tmp_path / "nested" / "__init__.py": "",
        tmp_path / "nested" / "shared.py": "VALUE = 'shared'\n",
        tmp_path / "nested" / "pkg" / "__init__.py": "",
        tmp_path / "nested" / "pkg" / "consumer.py": (
            "from ..shared import VALUE\nfrom .absent import missing\n"
        ),
    }
    for path, text in sources.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config = _config(tmp_path)

    summary = CodeRoute(
        config,
        _Inventory(sources),
        _FrameworkState(),
        1,
        1,
    ).run()

    with sqlite3.connect(config.state_path) as connection:
        rows = connection.execute(
            """SELECT source.current_path,d.name,target.current_path
            FROM dependencies d
            JOIN file_versions v ON v.version_id=d.version_id
            JOIN files source ON source.file_id=v.file_id
            LEFT JOIN file_versions resolved
                ON resolved.version_id=d.resolved_version_id
            LEFT JOIN files target ON target.file_id=resolved.file_id
            WHERE d.kind='python_relative_import'
            ORDER BY source.current_path,d.name"""
        ).fetchall()
        unresolved = connection.execute(
            """SELECT source.current_path FROM diagnostics diagnostic
            JOIN file_versions v ON v.version_id=diagnostic.version_id
            JOIN files source ON source.file_id=v.file_id
            WHERE diagnostic.code='unresolved_relative_import'
            ORDER BY source.current_path"""
        ).fetchall()

    by_source_and_name = {
        (Path(str(source)).relative_to(tmp_path).as_posix(), str(name)): (
            None
            if target is None
            else Path(str(target)).relative_to(tmp_path).as_posix()
        )
        for source, name, target in rows
    }
    assert summary.processed == summary.candidates == len(sources)
    assert by_source_and_name == {
        ("alpha/consumer.py", ".helper"): "alpha/helper.py",
        ("alpha/consumer.py", ".models"): "alpha/models.py",
        ("beta/consumer.py", ".models"): "beta/models.py",
        ("nested/pkg/consumer.py", "..shared"): "nested/shared.py",
        ("nested/pkg/consumer.py", ".absent"): None,
    }
    assert [
        Path(str(row[0])).relative_to(tmp_path).as_posix() for row in unresolved
    ] == ["nested/pkg/consumer.py"]


def test_scoped_private_calls_resolve_before_global_name_union(tmp_path: Path) -> None:
    source_text = (
        "def _helper():\n"
        "    return True\n"
        "\n"
        "class Worker:\n"
        "    def _step(self):\n"
        "        return _helper()\n"
        "\n"
        "    def run(self):\n"
        "        return self._step()\n"
    )
    sources = (tmp_path / "alpha.py", tmp_path / "beta.py")
    for path in sources:
        path.write_text(source_text, encoding="utf-8")
    config = _config(tmp_path)

    CodeRoute(
        config,
        _Inventory(sources),
        _FrameworkState(),
        1,
        1,
    ).run()

    with sqlite3.connect(config.state_path) as connection:
        resolved = connection.execute(
            """SELECT source.current_path,r.name,target.qualified_name
            FROM code_references r
            JOIN file_versions source_version
                ON source_version.version_id=r.version_id
            JOIN files source ON source.file_id=source_version.file_id
            LEFT JOIN symbols target ON target.symbol_id=r.target_symbol_id
            WHERE r.name IN ('_helper','self._step')
            ORDER BY source.current_path,r.name"""
        ).fetchall()
        probable_dead = int(
            connection.execute(
                """SELECT COUNT(*) FROM diagnostics
                WHERE code='probable_dead_symbol'"""
            ).fetchone()[0]
        )

    assert [
        (
            Path(str(source)).relative_to(tmp_path).as_posix(),
            str(name),
            str(target),
        )
        for source, name, target in resolved
    ] == [
        ("alpha.py", "_helper", "alpha._helper"),
        ("alpha.py", "self._step", "alpha.Worker._step"),
        ("beta.py", "_helper", "beta._helper"),
        ("beta.py", "self._step", "beta.Worker._step"),
    ]
    assert probable_dead == 0


def test_python_import_alias_calls_resolve_without_crossing_shadowed_names(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text(
        "def direct():\n"
        "    return True\n"
        "\n"
        "class Worker:\n"
        "    @staticmethod\n"
        "    def build():\n"
        "        return True\n",
        encoding="utf-8",
    )
    package = tmp_path / "pkg"
    package.mkdir()
    package_init = package / "__init__.py"
    package_init.write_text(
        "from helper import direct as exported\n",
        encoding="utf-8",
    )
    package_helper = package / "helper.py"
    package_helper.write_text(
        "def package_direct():\n    return True\n",
        encoding="utf-8",
    )
    facade = tmp_path / "facade.py"
    facade.write_text(
        "from helper import Worker as ExportedWorker\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "from helper import direct as aliased_direct\n"
        "import helper as helper_alias\n"
        "from helper import Worker as AliasWorker\n"
        "from helper import direct as shadowed\n"
        "from helper import direct as rebound\n"
        "from pkg import exported as package_alias\n"
        "from pkg import helper as package_module\n"
        "from facade import ExportedWorker as FacadeWorker\n"
        "from external_pkg import direct as external_direct\n"
        "rebound = lambda: False\n"
        "\n"
        "def direct():\n"
        "    return False\n"
        "\n"
        "def via_from():\n"
        "    return aliased_direct()\n"
        "\n"
        "def via_module():\n"
        "    return helper_alias.direct()\n"
        "\n"
        "def via_class():\n"
        "    return AliasWorker.build()\n"
        "\n"
        "def via_local_import():\n"
        "    from helper import direct as inner\n"
        "    return inner()\n"
        "\n"
        "def via_package_reexport():\n"
        "    return package_alias()\n"
        "\n"
        "def via_module_reexport():\n"
        "    return FacadeWorker.build()\n"
        "\n"
        "def via_package_submodule():\n"
        "    return package_module.package_direct()\n"
        "\n"
        "def external_collision():\n"
        "    return external_direct()\n"
        "\n"
        "def parameter_shadow(shadowed):\n"
        "    return shadowed()\n"
        "\n"
        "def module_rebound():\n"
        "    return rebound()\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)

    CodeRoute(
        config,
        _Inventory((helper, package_init, package_helper, facade, consumer)),
        _FrameworkState(),
        1,
        1,
    ).run()

    with sqlite3.connect(config.state_path) as connection:
        rows = connection.execute(
            """SELECT r.name,r.target_hint,r.evidence,target.qualified_name
            FROM code_references r
            JOIN file_versions source_version ON source_version.version_id=r.version_id
            JOIN files source ON source.file_id=source_version.file_id
            LEFT JOIN symbols target ON target.symbol_id=r.target_symbol_id
            WHERE source.current_path=? AND r.kind='call'
            ORDER BY r.start_line""",
            (str(consumer),),
        ).fetchall()

    by_name = {
        str(name): (
            str(target_hint),
            str(evidence),
            None if target is None else str(target),
        )
        for name, target_hint, evidence, target in rows
    }
    assert by_name["aliased_direct"] == (
        "helper.direct",
        "python-ast:call-expression-import-bound",
        "helper.direct",
    )
    assert by_name["helper_alias.direct"] == (
        "helper.direct",
        "python-ast:call-expression-import-bound",
        "helper.direct",
    )
    assert by_name["AliasWorker.build"] == (
        "helper.Worker.build",
        "python-ast:call-expression-import-bound",
        "helper.Worker.build",
    )
    assert by_name["inner"] == (
        "helper.direct",
        "python-ast:call-expression-import-bound",
        "helper.direct",
    )
    assert by_name["package_alias"] == (
        "pkg.exported",
        "python-ast:call-expression-import-bound",
        "helper.direct",
    )
    assert by_name["FacadeWorker.build"] == (
        "facade.ExportedWorker.build",
        "python-ast:call-expression-import-bound",
        "helper.Worker.build",
    )
    assert by_name["package_module.package_direct"] == (
        "pkg.helper.package_direct",
        "python-ast:call-expression-import-bound",
        "helper.package_direct",
    )
    assert by_name["external_direct"] == (
        "external_pkg.direct",
        "python-ast:call-expression-import-bound",
        None,
    )
    assert by_name["shadowed"] == (
        "shadowed",
        "python-ast:call-expression",
        None,
    )
    assert by_name["rebound"] != (
        "helper.direct",
        "python-ast:call-expression-import-bound",
        "helper.direct",
    )


def test_stable_full_run_reuses_exact_completed_graph_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "stable.py"
    source.write_text("def stable():\n    return True\n", encoding="utf-8")
    config = _config(tmp_path)
    inventory = _Inventory((source,))

    first = CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()
    calls = _track_graph_finalization(monkeypatch)
    second = CodeRoute(config, inventory, _FrameworkState(), 2, 2).run()

    assert calls == []
    assert second.cache_hits == second.candidates == 1
    assert second.processed == second.invalidated_versions == 0
    assert second.projects == first.projects
    with sqlite3.connect(config.state_path) as connection:
        marker = connection.execute(
            """SELECT value FROM metadata
            WHERE key='code_graph_completion_v3'"""
        ).fetchone()[0]
        statuses = connection.execute(
            "SELECT status FROM analysis_runs ORDER BY analysis_run_id"
        ).fetchall()
    assert json.loads(str(marker)) == {
        "analysis_run_id": 2,
        "resolver_signature": CODE_GRAPH_RESOLVER_SIGNATURE,
        "schema_version": 1,
    }
    assert statuses == [("completed",), ("completed",)]


@pytest.mark.parametrize("change_kind", ("new", "replaced", "renamed", "manifest"))
def test_graph_fast_path_rejects_changed_file_and_project_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change_kind: str,
) -> None:
    source = tmp_path / "base.py"
    source.write_text("def base():\n    return 1\n", encoding="utf-8")
    config = _config(tmp_path)
    CodeRoute(
        config,
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
    ).run()

    paths = [source]
    if change_kind == "new":
        added = tmp_path / "added.py"
        added.write_text("def added():\n    return 2\n", encoding="utf-8")
        paths.append(added)
    elif change_kind == "replaced":
        source.write_text("def base():\n    return 200\n", encoding="utf-8")
    elif change_kind == "renamed":
        moved = tmp_path / "moved.py"
        source.rename(moved)
        paths = [moved]
    else:
        manifest = tmp_path / "pyproject.toml"
        manifest.write_text("[project]\nname='fixture'\n", encoding="utf-8")
        paths.append(manifest)

    calls = _track_graph_finalization(monkeypatch)
    summary = CodeRoute(
        config,
        _Inventory(paths),
        _FrameworkState(),
        2,
        2,
    ).run()

    assert calls == [2]
    if change_kind == "renamed":
        assert summary.cache_hits == 0
        assert summary.processed == 1
    else:
        assert summary.processed >= 1


def test_graph_fast_path_rejects_missing_current_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = tmp_path / "retained.py"
    removed = tmp_path / "removed.py"
    retained.write_text("def retained():\n    return 1\n", encoding="utf-8")
    removed.write_text("def removed():\n    return 2\n", encoding="utf-8")
    config = _config(tmp_path)
    CodeRoute(
        config,
        _Inventory((retained, removed)),
        _FrameworkState(),
        1,
        1,
    ).run()

    calls = _track_graph_finalization(monkeypatch)
    summary = CodeRoute(
        config,
        _Inventory((retained,)),
        _FrameworkState(),
        2,
        2,
    ).run()

    assert calls == [2]
    assert summary.invalidated_versions == 1
    with sqlite3.connect(config.state_path) as connection:
        status = connection.execute(
            "SELECT status FROM files WHERE current_path=?",
            (str(removed),),
        ).fetchone()[0]
    assert status == "missing"


def test_graph_fast_path_rejects_prior_incomplete_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "stable.py"
    source.write_text("def stable():\n    return True\n", encoding="utf-8")
    config = _config(tmp_path)
    inventory = _Inventory((source,))
    CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()
    with CodeState(config.state_path) as state:
        signature = str(
            state.connection.execute(
                "SELECT processing_signature FROM analysis_runs"
            ).fetchone()[0]
        )
        incomplete_run = state.begin_run(2, 2, signature)
        state.fail_run(incomplete_run, RuntimeError("injected incomplete graph"))

    calls = _track_graph_finalization(monkeypatch)
    summary = CodeRoute(config, inventory, _FrameworkState(), 3, 3).run()

    assert calls == [3]
    assert summary.cache_hits == 1
    assert summary.processed == 0


def test_cancelled_fast_path_does_not_advance_graph_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "stable.py"
    source.write_text("def stable():\n    return True\n", encoding="utf-8")
    config = _config(tmp_path)
    inventory = _Inventory((source,))
    CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()
    token = CancellationToken()
    original_reuse = CodeState.reusable_graph_project_count

    def cancel_after_proof(
        state: CodeState,
        analysis_run_id: int,
        processing_signature: str,
    ) -> int | None:
        result = original_reuse(state, analysis_run_id, processing_signature)
        token.cancel()
        return result

    monkeypatch.setattr(
        CodeState,
        "reusable_graph_project_count",
        cancel_after_proof,
    )
    with pytest.raises(CancellationRequested):
        CodeRoute(
            config,
            inventory,
            _FrameworkState(),
            2,
            2,
            cancellation=token,
        ).run()

    monkeypatch.setattr(CodeState, "reusable_graph_project_count", original_reuse)
    calls = _track_graph_finalization(monkeypatch)
    CodeRoute(config, inventory, _FrameworkState(), 3, 3).run()
    assert calls == [3]
    with sqlite3.connect(config.state_path) as connection:
        statuses = connection.execute(
            "SELECT status FROM analysis_runs ORDER BY analysis_run_id"
        ).fetchall()
    assert statuses == [("completed",), ("cancelled",), ("completed",)]


def test_systemd_sigint_terminalizes_the_current_code_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "interrupted.py"
    source.write_text("def interrupted():\n    return True\n", encoding="utf-8")
    config = _config(tmp_path)
    inventory = _Inventory((source,))

    def interrupt_graph(
        _state: CodeState,
        _framework_run_id: int,
        *,
        cancellation_check=None,
    ) -> int:
        del cancellation_check
        raise KeyboardInterrupt("systemd SIGINT")

    monkeypatch.setattr(CodeState, "finalize_graph", interrupt_graph)

    with pytest.raises(KeyboardInterrupt, match="systemd SIGINT"):
        CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()

    with sqlite3.connect(config.state_path) as connection:
        observed = connection.execute(
            "SELECT status,error_type,error_message FROM analysis_runs"
        ).fetchone()
    assert observed == ("cancelled", "KeyboardInterrupt", "systemd SIGINT")


def test_cancelled_sqlite_graph_preserves_fence_and_rebuilds_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "cancelled_graph.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    config = _config(tmp_path)
    inventory = _Inventory((source,))
    CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()
    with sqlite3.connect(config.state_path) as connection:
        marker_before = connection.execute(
            "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
        ).fetchone()[0]

    source.write_text("def value():\n    return 200\n", encoding="utf-8")
    token = CancellationToken()
    original_scope = code_state_module.sqlite_cancellation_scope
    original_infer = CodeState._infer_incomplete_projects

    def one_instruction_scope(
        connection: sqlite3.Connection,
        bridge: SQLiteCancellationBridge,
    ) -> AbstractContextManager[SQLiteCancellationBridge]:
        return original_scope(connection, bridge, instructions=1)

    def cancel_after_inference(state: CodeState, framework_run_id: int) -> None:
        original_infer(state, framework_run_id)
        token.cancel()

    monkeypatch.setattr(
        code_state_module,
        "sqlite_cancellation_scope",
        one_instruction_scope,
    )
    monkeypatch.setattr(
        CodeState,
        "_infer_incomplete_projects",
        cancel_after_inference,
    )
    failed_framework = _FrameworkState()
    with pytest.raises(CancellationRequested) as raised:
        CodeRoute(
            config,
            inventory,
            failed_framework,
            2,
            2,
            cancellation=token,
        ).run()

    assert token.is_cancelled
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert failed_framework.phases[-1] == ("graph", "failed")
    with sqlite3.connect(config.state_path) as connection:
        marker_after_cancel = connection.execute(
            "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
        ).fetchone()[0]
        statuses_after_cancel = connection.execute(
            "SELECT status FROM analysis_runs ORDER BY analysis_run_id"
        ).fetchall()
    assert marker_after_cancel == marker_before
    assert statuses_after_cancel == [("completed",), ("cancelled",)]

    monkeypatch.setattr(
        CodeState,
        "_infer_incomplete_projects",
        original_infer,
    )
    monkeypatch.setattr(
        code_state_module,
        "sqlite_cancellation_scope",
        original_scope,
    )
    recovered = CodeRoute(config, inventory, _FrameworkState(), 3, 3).run()

    assert recovered.cache_hits == recovered.candidates == 1
    assert recovered.processed == 0
    with sqlite3.connect(config.state_path) as connection:
        marker_after_recovery = json.loads(
            str(
                connection.execute(
                    "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
                ).fetchone()[0]
            )
        )
        statuses_after_recovery = connection.execute(
            "SELECT status FROM analysis_runs ORDER BY analysis_run_id"
        ).fetchall()
    assert marker_after_recovery["analysis_run_id"] == 3
    assert statuses_after_recovery == [
        ("completed",),
        ("cancelled",),
        ("completed",),
    ]


def test_graph_fence_publication_is_atomic_with_run_completion(tmp_path: Path) -> None:
    database = tmp_path / "state" / "code.sqlite3"
    with CodeState(database) as state:
        analysis_run_id = state.begin_run(1, 1, "fixture-signature")

        def deny_graph_fence(
            action: int,
            argument_one: str | None,
            _argument_two: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_INSERT and argument_one == "metadata":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        state.connection.set_authorizer(deny_graph_fence)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                state.complete_run(
                    analysis_run_id,
                    {
                        "candidates": 0,
                        "processed": 0,
                        "cache_hits": 0,
                        "errors": 0,
                        "graph_milliseconds": 0,
                    },
                    partial=False,
                    graph_current=True,
                )
        finally:
            state.connection.set_authorizer(None)
        status = state.connection.execute(
            "SELECT status FROM analysis_runs WHERE analysis_run_id=?",
            (analysis_run_id,),
        ).fetchone()[0]
        marker = state.connection.execute(
            """SELECT value FROM metadata
            WHERE key='code_graph_completion_v3'"""
        ).fetchone()

    assert status == "running"
    assert marker is None


def test_graph_fast_path_requires_current_resolver_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "stable.py"
    source.write_text("def stable():\n    return True\n", encoding="utf-8")
    config = _config(tmp_path)
    inventory = _Inventory((source,))
    CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()
    with sqlite3.connect(config.state_path) as connection:
        fence = json.loads(
            str(
                connection.execute(
                    "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
                ).fetchone()[0]
            )
        )
        fence["resolver_signature"] = "code-graph-resolver-legacy"
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='code_graph_completion_v3'",
            (json.dumps(fence, sort_keys=True, separators=(",", ":")),),
        )

    calls = _track_graph_finalization(monkeypatch)
    summary = CodeRoute(config, inventory, _FrameworkState(), 2, 2).run()

    assert calls == [2]
    assert summary.cache_hits == 1
    assert summary.processed == 0
    with sqlite3.connect(config.state_path) as connection:
        renewed = json.loads(
            str(
                connection.execute(
                    "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
                ).fetchone()[0]
            )
        )
    assert renewed["analysis_run_id"] == 2
    assert renewed["resolver_signature"] == CODE_GRAPH_RESOLVER_SIGNATURE


def test_run_completion_compare_and_swap_rejects_non_running_owners(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "code.sqlite3"
    summary = {
        "candidates": 0,
        "processed": 0,
        "cache_hits": 0,
        "errors": 0,
        "graph_milliseconds": 0,
    }
    with CodeState(database) as state:
        failed = state.begin_run(1, 1, "fixture-signature")
        state.fail_run(failed, RuntimeError("injected failure"))
        with pytest.raises(RuntimeError, match="one running owner row"):
            state.complete_run(
                failed,
                summary,
                partial=False,
                graph_current=True,
            )
        assert (
            state.connection.execute(
                "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
            ).fetchone()
            is None
        )

        completed = state.begin_run(2, 2, "fixture-signature")
        state.complete_run(
            completed,
            summary,
            partial=False,
            graph_current=True,
        )
        marker_before = state.connection.execute(
            "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
        ).fetchone()[0]
        with pytest.raises(RuntimeError, match="one running owner row"):
            state.complete_run(
                completed,
                summary,
                partial=False,
                graph_current=True,
            )
        with pytest.raises(RuntimeError, match="one running owner row"):
            state.complete_run(
                completed + 10_000,
                summary,
                partial=False,
                graph_current=True,
            )
        marker_after = state.connection.execute(
            "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
        ).fetchone()[0]
        statuses = state.connection.execute(
            "SELECT status FROM analysis_runs ORDER BY analysis_run_id"
        ).fetchall()

    assert marker_after == marker_before
    assert [tuple(row) for row in statuses] == [("failed",), ("completed",)]


def test_python_assignments_emit_only_names_bound_by_the_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bindings.py"
    source.write_text(
        "mapping[keyword[keyword.find('_')]] = 1\n"
        "head, *tail = values\n"
        "left, left = values\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)

    summary = CodeRoute(
        config,
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
    ).run()

    uri = f"file:{config.state_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            """SELECT name, qualified_name
            FROM symbols
            WHERE kind='module_variable'
            ORDER BY qualified_name"""
        ).fetchall()

    assert summary.processed == 1
    assert rows == [
        ("head", "bindings.head"),
        ("left", "bindings.left"),
        ("tail", "bindings.tail"),
    ]


def test_full_cache_validation_reanalyzes_changed_bytes(tmp_path: Path) -> None:
    source = tmp_path / "value.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    config = _config(tmp_path, cache_validation="full")
    inventory = _Inventory((source,))

    first = CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()
    original_mtime = source.stat().st_mtime_ns
    source.write_text("def value():\n    return 2\n", encoding="utf-8")
    source.touch()
    source_mtime_seconds = original_mtime / 1_000_000_000
    os.utime(source, (source_mtime_seconds, source_mtime_seconds))
    second = CodeRoute(config, inventory, _FrameworkState(), 2, 2).run()

    assert first.processed == 1
    assert second.processed == 1
    assert second.cache_hits == 0
    assert second.bytes_read == source.stat().st_size
    hits = search_code(
        config.state_path,
        CodeSearchQuery(text="return 2", modes=("literal",), limit=5),
    )
    assert hits and hits[0].path == str(source)


def test_stronger_cache_validation_reuses_compatible_analysis(tmp_path: Path) -> None:
    source = tmp_path / "stable.py"
    source.write_text("def stable():\n    return True\n", encoding="utf-8")
    inventory = _Inventory((source,))

    first = CodeRoute(
        _config(tmp_path, cache_validation="metadata"),
        inventory,
        _FrameworkState(),
        1,
        1,
    ).run()
    second = CodeRoute(
        _config(tmp_path, cache_validation="full"),
        inventory,
        _FrameworkState(),
        2,
        2,
    ).run()

    assert first.processed == 1
    assert second.processed == 0
    assert second.cache_hits == 1
    assert second.bytes_read == source.stat().st_size


def test_route_timings_accumulate_submillisecond_file_operations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sources = (tmp_path / "first.py", tmp_path / "second.py")
    for source in sources:
        source.write_text("def value():\n    return 1\n", encoding="utf-8")
    ticks = iter(range(0, 60_000_000, 600_000))
    monkeypatch.setattr(
        code_route_module.time,
        "perf_counter_ns",
        lambda: next(ticks),
    )

    summary = CodeRoute(
        _config(tmp_path),
        _Inventory(sources),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.read_milliseconds == 1
    assert summary.analyze_milliseconds == 1
    assert summary.persist_milliseconds == 1


def test_missing_optional_analyzer_degrades_to_searchable_generic_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "worker.py"
    source.write_text("def fallback_symbol():\n    return 1\n", encoding="utf-8")
    registry = AnalyzerRegistry(
        (
            AnalyzerSpec(
                "missing-python-parser",
                frozenset({"python"}),
                ".module_that_does_not_exist",
                "MissingAnalyzer",
                "fixture-v1",
                priority=1,
            ),
            AnalyzerSpec(
                "neocortex-generic-text",
                frozenset({"*"}),
                ".code_generic",
                "GenericAnalyzer",
                "1",
                priority=100,
            ),
        )
    )
    config = _config(tmp_path)

    summary = CodeRoute(
        config,
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
        analyzers=registry,
    ).run()
    hits = search_code(
        config.state_path,
        CodeSearchQuery(text="fallback_symbol", modes=("literal",), limit=5),
    )

    assert summary.partial == 1
    assert hits and hits[0].path == str(source)
    assert registry.status()[0]["load_error"] is not None


def test_newly_available_analyzer_invalidates_runtime_fallback_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "worker.py"
    source.write_text("def runtime_upgrade():\n    return 1\n", encoding="utf-8")
    module_name = "_04_Nucleo_Operativo._fixture_optional_code_analyzer"
    sys.modules.pop(module_name, None)
    specs = (
        AnalyzerSpec(
            "fixture-optional-python",
            frozenset({"python"}),
            "._fixture_optional_code_analyzer",
            "FixtureOptionalAnalyzer",
            "fixture-v1",
            priority=1,
        ),
        AnalyzerSpec(
            "neocortex-generic-text",
            frozenset({"*"}),
            ".code_generic",
            "GenericAnalyzer",
            "1",
            priority=100,
        ),
    )
    config = _config(tmp_path)
    first = CodeRoute(
        config,
        _Inventory((source,)),
        _FrameworkState(),
        1,
        1,
        analyzers=AnalyzerRegistry(specs),
    ).run()

    class FixtureOptionalAnalyzer:
        analyzer_id = "fixture-optional-python"
        analyzer_version = "fixture-v1"
        languages = frozenset({"python"})

        def analyze(
            self,
            source_input: CodeFileInput,
            route_config: CodeRouteConfig,
        ) -> CodeAnalysis:
            fallback = GenericAnalyzer().analyze(source_input, route_config)
            return replace(
                fallback,
                status=AnalysisStatus.COMPLETE,
                analyzer_id=self.analyzer_id,
                analyzer_version=self.analyzer_version,
                parser_kind="fixture-optional-parser",
            )

    module = ModuleType(module_name)
    module.__dict__["FixtureOptionalAnalyzer"] = FixtureOptionalAnalyzer
    sys.modules[module_name] = module
    try:
        second = CodeRoute(
            config,
            _Inventory((source,)),
            _FrameworkState(),
            2,
            2,
            analyzers=AnalyzerRegistry(specs),
        ).run()
    finally:
        sys.modules.pop(module_name, None)

    with sqlite3.connect(config.state_path) as connection:
        versions = connection.execute(
            """SELECT analyzer_id,analysis_status,invalidated_ns
            FROM file_versions ORDER BY version_id"""
        ).fetchall()

    assert first.partial == 1
    assert second.cache_hits == 0
    assert second.processed == second.invalidated_versions == 1
    assert versions[0][0] == "neocortex-generic-text"
    assert versions[0][2] is not None
    assert versions[1] == ("fixture-optional-python", "complete", None)


def test_cached_incomplete_status_counters_match_first_publication(
    tmp_path: Path,
) -> None:
    truncated = tmp_path / "truncated.py"
    failing = tmp_path / "failing.rs"
    truncated.write_text(
        "def truncated():\n" + "    # bounded text\n" * 100,
        encoding="utf-8",
    )
    failing.write_text("pub fn failing() {}\n", encoding="utf-8")
    module_name = "_04_Nucleo_Operativo._fixture_failing_code_analyzer"

    class FixtureFailingAnalyzer:
        analyzer_id = "fixture-failing-rust"
        analyzer_version = "fixture-v1"
        languages = frozenset({"rust"})

        def analyze(
            self,
            source_input: CodeFileInput,
            route_config: CodeRouteConfig,
        ) -> CodeAnalysis:
            del source_input, route_config
            raise RuntimeError("injected analyzer failure")

    module = ModuleType(module_name)
    module.__dict__["FixtureFailingAnalyzer"] = FixtureFailingAnalyzer
    sys.modules[module_name] = module
    specs = (
        AnalyzerSpec(
            "fixture-failing-rust",
            frozenset({"rust"}),
            "._fixture_failing_code_analyzer",
            "FixtureFailingAnalyzer",
            "fixture-v1",
            priority=1,
        ),
        AnalyzerSpec(
            "neocortex-generic-text",
            frozenset({"*"}),
            ".code_generic",
            "GenericAnalyzer",
            "1",
            priority=100,
        ),
    )
    config = _config(tmp_path, max_text_chars=1024)
    inventory = _Inventory((truncated, failing))
    try:
        first = CodeRoute(
            config,
            inventory,
            _FrameworkState(),
            1,
            1,
            analyzers=AnalyzerRegistry(specs),
        ).run()
        second = CodeRoute(
            config,
            inventory,
            _FrameworkState(),
            2,
            2,
            analyzers=AnalyzerRegistry(specs),
        ).run()
    finally:
        sys.modules.pop(module_name, None)

    assert first.partial == second.partial == 1
    assert first.errors == second.errors == 1
    assert second.cache_hits == second.candidates == 2
    assert second.processed == 0


def test_excluded_generated_and_vendored_files_remain_full_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = tmp_path / "generated" / "client.py"
    vendored = tmp_path / "vendor" / "library.py"
    generated.parent.mkdir()
    vendored.parent.mkdir()
    generated.write_text("# generated\ndef client():\n    pass\n", encoding="utf-8")
    vendored.write_text("def library():\n    pass\n", encoding="utf-8")
    config = _config(
        tmp_path,
        include_generated=False,
        include_vendored=False,
    )
    inventory = _Inventory((generated, vendored))

    first = CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()
    calls = _track_graph_finalization(monkeypatch)
    second = CodeRoute(config, inventory, _FrameworkState(), 2, 2).run()
    third = CodeRoute(config, _Inventory(()), _FrameworkState(), 3, 3).run()

    assert first.processed == 2
    assert first.text_only == 2
    assert first.generated == first.vendored == 1
    assert second.cache_hits == 2
    assert second.cache_batches == 1
    assert second.cache_lookup_milliseconds >= 0
    assert second.cache_update_milliseconds >= 0
    assert second.cache_commit_milliseconds >= 0
    assert second.text_only == 2
    assert second.generated == second.vendored == 1
    assert calls == [3]
    assert third.invalidated_versions == 2
    with sqlite3.connect(config.state_path) as connection:
        statuses = connection.execute(
            "SELECT status,COUNT(*) FROM files GROUP BY status"
        ).fetchall()
    assert statuses == [("missing", 2)]


def test_cache_observation_updates_commit_in_bounded_batches(tmp_path: Path) -> None:
    sources = []
    for index in range(257):
        source = tmp_path / f"cached_{index:03d}.py"
        source.write_text(f"VALUE = {index}\n", encoding="utf-8")
        sources.append(source)
    config = _config(tmp_path)
    inventory = _Inventory(tuple(sources))

    first = CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()
    replay = CodeRoute(config, inventory, _FrameworkState(), 2, 2).run()

    assert first.processed == 257
    assert replay.processed == 0
    assert replay.cache_hits == 257
    assert replay.cache_batches == 3


def test_binary_and_oversized_candidates_are_bounded_and_versioned(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "binary.py"
    oversized = tmp_path / "huge.rs"
    binary.write_bytes(b"\x00\x01\x02not-source")
    oversized.write_bytes(b"pub fn large() {}\n" + b"x" * 5000)
    config = _config(
        tmp_path,
        max_file_bytes=4096,
        max_text_chars=1024,
    )

    summary = CodeRoute(
        config,
        _Inventory((binary, oversized)),
        _FrameworkState(),
        1,
        1,
    ).run()

    assert summary.candidates == summary.processed == 2
    assert summary.binary_skips == 1
    assert summary.skipped_limit == 1
    assert summary.bytes_read == binary.stat().st_size
    with sqlite3.connect(config.state_path) as connection:
        rows = connection.execute(
            "SELECT analysis_status,raw_xxh3_128 FROM file_versions ORDER BY path_observed"
        ).fetchall()
    assert rows[0][0] == "binary" and rows[0][1]
    assert rows[1] == ("skipped_limit", None)


# endregion [03]


# region [04] Projects, variants and schema


def test_dispersed_projects_are_reconstructed_conceptually(tmp_path: Path) -> None:
    root = tmp_path / "backup" / "motor"
    (root / "src").mkdir(parents=True)
    manifest = root / "Cargo.toml"
    implementation = root / "src" / "lib.rs"
    manifest.write_text(
        "[package]\nname='motor'\nversion='0.1.0'\n[dependencies]\nserde='1'\n",
        encoding="utf-8",
    )
    implementation.write_text(
        "pub trait Start { fn start(&self); }\n"
        "pub struct Motor;\n"
        "impl Start for Motor { fn start(&self) {} }\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    CodeRoute(
        config,
        _Inventory((manifest, implementation)),
        _FrameworkState(),
        1,
        1,
    ).run()

    projects = list_projects(config.state_path)
    reconstruction = reconstruct_project(
        config.state_path, projects[0].project_id, strategy="branches"
    )

    assert projects[0].name == "motor"
    assert projects[0].current_files == 2
    assert reconstruction.strategy == "branches"
    assert {entry.proposed_path for entry in reconstruction.entries} == {
        "Cargo.toml",
        "src/lib.rs",
    }
    assert not any(entry.selected for entry in reconstruction.entries)
    assert "conceptual-only:no-filesystem-mutation" in reconstruction.evidence


def test_source_move_publishes_successor_and_rebuilds_inferred_project(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    (root_a / "src").mkdir(parents=True)
    (root_b / "src").mkdir(parents=True)
    source = root_a / "src" / "unit.py"
    source.write_text("def unit():\n    return 1\n", encoding="utf-8")
    config = _config(tmp_path)
    CodeRoute(config, _Inventory((source,)), _FrameworkState(), 1, 1).run()

    moved = root_b / "src" / "unit.py"
    source.rename(moved)
    second = CodeRoute(
        config,
        _Inventory((moved,)),
        _FrameworkState(),
        2,
        2,
    ).run()

    with sqlite3.connect(config.state_path) as connection:
        versions = connection.execute(
            """SELECT path_observed,invalidated_ns
            FROM file_versions ORDER BY version_id"""
        ).fetchall()
        projects = connection.execute(
            "SELECT probable_root,status FROM projects ORDER BY probable_root"
        ).fetchall()
        current_membership = connection.execute(
            """SELECT p.probable_root,m.relation
            FROM project_memberships m
            JOIN projects p ON p.project_id=m.project_id
            JOIN file_versions v ON v.version_id=m.version_id
            WHERE v.invalidated_ns IS NULL AND m.selected=1"""
        ).fetchall()
        current_fts = connection.execute(
            """SELECT DISTINCT x.path,x.project FROM code_fts x
            JOIN file_versions v ON v.version_id=x.version_id
            WHERE v.invalidated_ns IS NULL"""
        ).fetchall()

    assert second.cache_hits == 0
    assert second.processed == second.invalidated_versions == 1
    assert versions[0][0] == str(source)
    assert versions[0][1] is not None
    assert versions[1] == (str(moved), None)
    assert projects == [(str(root_a), "historical"), (str(root_b), "ambiguous")]
    assert current_membership == [(str(root_b), "inferred_root")]
    assert current_fts == [(str(moved), root_b.name)]


def test_manifest_move_retires_old_rooted_project(tmp_path: Path) -> None:
    root_a = tmp_path / "manifest-a"
    root_b = tmp_path / "manifest-b"
    root_a.mkdir()
    root_b.mkdir()
    manifest = root_a / "pyproject.toml"
    manifest.write_text("[project]\nname='fixture'\n", encoding="utf-8")
    config = _config(tmp_path)
    CodeRoute(config, _Inventory((manifest,)), _FrameworkState(), 1, 1).run()

    moved = root_b / "pyproject.toml"
    manifest.rename(moved)
    second = CodeRoute(
        config,
        _Inventory((moved,)),
        _FrameworkState(),
        2,
        2,
    ).run()

    with sqlite3.connect(config.state_path) as connection:
        projects = connection.execute(
            "SELECT probable_root,status FROM projects ORDER BY probable_root"
        ).fetchall()
        memberships = connection.execute(
            """SELECT p.probable_root,v.path_observed,v.invalidated_ns,m.relation
            FROM project_memberships m
            JOIN projects p ON p.project_id=m.project_id
            JOIN file_versions v ON v.version_id=m.version_id
            ORDER BY v.version_id"""
        ).fetchall()
        current_fts = connection.execute(
            """SELECT DISTINCT x.path,x.project FROM code_fts x
            JOIN file_versions v ON v.version_id=x.version_id
            WHERE v.invalidated_ns IS NULL"""
        ).fetchall()

    assert second.cache_hits == 0
    assert second.processed == second.invalidated_versions == 1
    assert projects == [(str(root_a), "historical"), (str(root_b), "current")]
    assert memberships[0][0] == str(root_a)
    assert memberships[0][1] == str(manifest)
    assert memberships[0][2] is not None
    assert memberships[0][3] == "manifest"
    assert memberships[1] == (str(root_b), str(moved), None, "manifest")
    assert current_fts == [(str(moved), "fixture")]


def test_removed_manifest_rebuilds_retained_source_membership(tmp_path: Path) -> None:
    root = tmp_path / "removed-manifest"
    (root / "src").mkdir(parents=True)
    manifest = root / "pyproject.toml"
    source = root / "src" / "unit.py"
    manifest.write_text("[project]\nname='declared-name'\n", encoding="utf-8")
    source.write_text("def unit():\n    return 1\n", encoding="utf-8")
    config = _config(tmp_path)
    CodeRoute(
        config,
        _Inventory((manifest, source)),
        _FrameworkState(),
        1,
        1,
    ).run()

    second = CodeRoute(
        config,
        _Inventory((source,)),
        _FrameworkState(),
        2,
        2,
    ).run()

    with sqlite3.connect(config.state_path) as connection:
        projects = connection.execute(
            "SELECT name,status FROM projects ORDER BY name"
        ).fetchall()
        membership = connection.execute(
            """SELECT p.name,p.status,m.relation
            FROM project_memberships m
            JOIN projects p ON p.project_id=m.project_id
            JOIN file_versions v ON v.version_id=m.version_id
            WHERE v.invalidated_ns IS NULL AND m.selected=1"""
        ).fetchall()
        current_fts = connection.execute(
            """SELECT DISTINCT x.project FROM code_fts x
            JOIN file_versions v ON v.version_id=x.version_id
            WHERE v.invalidated_ns IS NULL"""
        ).fetchall()
        manifest_status = connection.execute(
            "SELECT status FROM files WHERE current_path=?",
            (str(manifest),),
        ).fetchone()[0]

    assert second.cache_hits == 1
    assert second.invalidated_versions == 1
    assert projects == [("declared-name", "historical"), (root.name, "ambiguous")]
    assert membership == [(root.name, "ambiguous", "inferred_root")]
    assert current_fts == [(root.name,)]
    assert manifest_status == "missing"


def test_manifest_name_change_relabels_cached_sources(tmp_path: Path) -> None:
    root = tmp_path / "renamed-project"
    (root / "src").mkdir(parents=True)
    manifest = root / "pyproject.toml"
    source = root / "src" / "unit.py"
    manifest.write_text("[project]\nname='old-name'\n", encoding="utf-8")
    source.write_text("def unit():\n    return 1\n", encoding="utf-8")
    config = _config(tmp_path)
    inventory = _Inventory((manifest, source))
    CodeRoute(config, inventory, _FrameworkState(), 1, 1).run()

    manifest.write_text("[project]\nname='new-long-name'\n", encoding="utf-8")
    second = CodeRoute(config, inventory, _FrameworkState(), 2, 2).run()

    with sqlite3.connect(config.state_path) as connection:
        projects = connection.execute(
            "SELECT name,status FROM projects ORDER BY name"
        ).fetchall()
        source_membership = connection.execute(
            """SELECT p.name,p.status,m.relation
            FROM project_memberships m
            JOIN projects p ON p.project_id=m.project_id
            JOIN file_versions v ON v.version_id=m.version_id
            WHERE v.invalidated_ns IS NULL AND v.path_observed=? AND m.selected=1""",
            (str(source),),
        ).fetchall()
        source_fts = connection.execute(
            """SELECT DISTINCT x.project FROM code_fts x
            JOIN file_versions v ON v.version_id=x.version_id
            WHERE v.invalidated_ns IS NULL AND v.path_observed=?""",
            (str(source),),
        ).fetchall()

    assert second.cache_hits == 1
    assert second.processed == second.invalidated_versions == 1
    assert projects == [("new-long-name", "current"), ("old-name", "historical")]
    assert source_membership == [("new-long-name", "current", "under_manifest_root")]
    assert source_fts == [("new-long-name",)]


def test_code_schema_is_versioned_and_exactly_initialized(tmp_path: Path) -> None:
    state_path = tmp_path / "code.sqlite3"

    initialize_code_state(state_path)
    initialize_code_state(state_path)

    with sqlite3.connect(state_path) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == CODE_SCHEMA_VERSION
        )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == str(CODE_SCHEMA_VERSION)
        assert tuple(
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ) == tuple(range(1, CODE_SCHEMA_VERSION + 1))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# endregion [04]
