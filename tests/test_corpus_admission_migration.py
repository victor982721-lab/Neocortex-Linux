"""Functional policy-migration fixtures for C03.

These tests use only temporary corpus/state trees. They exercise the real
Framework, Code/Text owners, and Semantic source adapters without loading a
model or contacting a provider.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

from neocortex.capabilities.formats.archive.route import ARCHIVE_MIME, ArchiveRoute
from neocortex.capabilities.formats.archive.state import archive_database
from neocortex.deduplication import snapshot_path
from neocortex.runtime.config.application_config_projections import (
    archive_route_config_from_application,
)
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.semantic.semantic_sources import iter_text_source_records


def _write(path: Path, value: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_bytes(value)
    return path


def _framework_config(
    root: Path,
    state: Path,
    *,
    scope: str,
    project_roots: tuple[Path, ...],
) -> FrameworkConfig:
    return FrameworkConfig(
        root=root,
        state_directory=state,
        route="text,code",
        code_candidate_scope=scope,
        code_project_roots=project_roots,
        document_catalog_enabled=False,
        global_memory_budget_bytes=512 * 1024 * 1024,
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
        global_cpu_slots=2,
        text_worker_memory_bytes=128 * 1024 * 1024,
        text_worker_timeout_seconds=30.0,
        heartbeat_interval_seconds=0.01,
    )


def _current_paths(database: Path, table: str, column: str) -> set[str]:
    with sqlite3.connect(database) as connection:
        where = " WHERE status='current'" if table == "files" else ""
        return {
            str(row[0])
            for row in connection.execute(f"SELECT {column} FROM {table}{where}")
        }


def test_broad_to_projects_retires_foreign_code_but_keeps_useful_text_and_semantic_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    owned = _write(root / "owned" / "pyproject.toml", "[project]\nname='owned'\n").parent
    owned_source = _write(owned / "src" / "owned.py", "OWNED = 1\n")
    foreign = root / "foreign"
    foreign_source = _write(foreign / "module.py", "FOREIGN = 1\n")
    foreign_note = _write(foreign / "note.txt", "Documento útil ajeno al interés de Code.\n")

    broad = FrameworkOrchestrator(
        _framework_config(root, state, scope="broad", project_roots=(owned,))
    ).run_initial()
    assert broad.code is not None
    assert broad.text is not None

    broad_code = _current_paths(state / "code.sqlite3", "files", "current_path")
    broad_text = _current_paths(state / "text.sqlite3", "documents", "path")
    assert str(foreign_source) in broad_code
    assert str(owned_source) in broad_code
    assert str(foreign_note) in broad_text
    assert str(foreign_source) in broad_text

    narrow = FrameworkOrchestrator(
        _framework_config(root, state, scope="projects", project_roots=(owned,))
    ).run_initial()
    assert narrow.code is not None
    assert narrow.text is not None

    narrow_code = _current_paths(state / "code.sqlite3", "files", "current_path")
    narrow_text = _current_paths(state / "text.sqlite3", "documents", "path")
    assert str(foreign_source) not in narrow_code
    assert str(foreign_source) not in narrow_text
    assert str(owned_source) in narrow_code
    assert str(foreign_note) in narrow_text

    # The incremental Semantic source adapter consumes only current Code rows.
    semantic_code_paths = {
        str(record.item.path)
        for record in iter_text_source_records(state, "code")
    }
    assert str(foreign_source) not in semantic_code_paths
    assert str(owned_source) in semantic_code_paths
    semantic_text_paths = {
        str(record.item.path)
        for record in iter_text_source_records(state, "text")
    }
    assert str(foreign_source) not in semantic_text_paths
    assert str(foreign_note) in semantic_text_paths


class _ArchiveFramework:
    def __init__(self, source: Path) -> None:
        self.snapshot = snapshot_path(source)

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        _selection,
    ) -> tuple[int, int]:
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        return 1, int(max_file_bytes is None or self.snapshot.size <= max_file_bytes)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection,
    ):
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        yield self.snapshot


def _zip(entries: dict[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


def test_archive_excluded_member_never_enters_semantic_archive_source(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    container = root / "mixed.zip"
    container.write_bytes(
        _zip(
            {
                "vendor/foreign.py": "FOREIGN_CODE = 1\n",
                "docs/report.txt": "Documento archive útil.\n",
                ".codex/auth.json": '{"fixture_secret":"must not enter text"}',
                "nested.zip": _zip(
                    {
                        "vendor/nested.js": "NESTED_CODE = 1\n",
                        "docs/nested.txt": "Documento nested útil.\n",
                    }
                ),
            }
        )
    )
    config = FrameworkConfig(
        root=root,
        state_directory=state,
        route="archive",
        code_project_roots=(root / "owned",),
        archive_ocr_mode="never",
        document_catalog_enabled=False,
    )
    archive_config = archive_route_config_from_application(config)
    summary = ArchiveRoute(
        archive_config,
        _ArchiveFramework(container),  # type: ignore[arg-type]
        1,
    ).run()
    assert summary.errors == 0

    records = tuple(iter_text_source_records(state, "archive"))
    paths = {str(record.item.path) for record in records}
    assert any(path.endswith("!/docs/report.txt") for path in paths)
    assert any(path.endswith("!/docs/nested.txt") for path in paths)
    assert not any(path.endswith("!/vendor/foreign.py") for path in paths)
    assert not any(path.endswith("!/vendor/nested.js") for path in paths)
    assert not any(path.endswith("!/.codex/auth.json") for path in paths)

    with archive_database(config.archive_database, readonly=True) as connection:
        denied = {
            str(row["member_chain"]): (str(row["status"]), row["text_zlib"])
            for row in connection.execute(
                "SELECT member_chain,status,text_zlib FROM documents WHERE member_chain<>''"
            )
            if "vendor/" in str(row["member_chain"]) or ".codex/" in str(row["member_chain"])
        }
    assert denied
    assert all(status == "metadata_only" and text is None for status, text in denied.values())
