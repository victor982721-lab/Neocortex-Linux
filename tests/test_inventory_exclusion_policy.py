"""Shared full-scan and USN exclusion-policy regressions."""
# region [00] Contexto del módulo
# Módulo: tests/test_inventory_exclusion_policy.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from neocortex.platform_policy import current_platform_policy

from _01_Enumeracion import JournalCursor, NtfsEntry, UsnChangeBatch
from _02_Deduplicacion import (
    DEFAULT_INVENTORY_EXCLUSION_POLICY,
    DedupIndex,
    InventoryCheckpoint,
    InventoryError,
    InventoryExclusionPolicy,
)
from _02_Deduplicacion.inventory_scan import DEFAULT_EXCLUDED_PATHS
from _04_Nucleo_Operativo import (
    inventory_coordinator as inventory_coordinator_module,
)
from _04_Nucleo_Operativo import reconcile as reconcile_module
from _04_Nucleo_Operativo.reconcile import reconcile_usn_window
from _04_Nucleo_Operativo.state import FrameworkState
# endregion [01]

# region [02] Implementación


def _write(path: Path, payload: bytes = b"fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _relative_paths(index: DedupIndex, scan_id: int, root: Path) -> set[str]:
    return {
        Path(snapshot.path).relative_to(root).as_posix() for snapshot in index.snapshots(scan_id)
    }


def _entry(file_id: int, parent_id: int, name: str, usn: int) -> NtfsEntry:
    return NtfsEntry(file_id, parent_id, name, usn, None, 0, 0, 0, 0, 3, 0)


class _FakeReader:
    def __init__(
        self,
        batch: UsnChangeBatch,
        resolved_parents: dict[int, Path],
    ) -> None:
        self._batch = batch
        self._resolved_parents = resolved_parents

    def iter_until(self, _target_usn: int):
        yield self._batch

    def resolve_path(self, file_id: int) -> str:
        return str(self._resolved_parents[file_id])


def test_policy_signature_is_versioned_canonical_and_non_cryptographic(
    tmp_path: Path,
) -> None:
    explicit_roots = (tmp_path / "state", tmp_path / "audit")
    restricted_root = tmp_path / "profile" / ".codex"
    allowed_trees = (
        restricted_root / "sessions",
        restricted_root / "scripts",
    )
    allowed_files = (restricted_root / "AGENTS.md",)
    first = InventoryExclusionPolicy.compile(
        explicit_roots,
        directory_names=("Build", "NODE_MODULES"),
        directory_prefixes=("TMP", "BaseTemp"),
        directory_fragments=("PyTest",),
        file_names=("Coverage.XML", ".Coverage"),
        file_suffixes=(".PSTATS", ".prof"),
        restricted_roots=(restricted_root,),
        restricted_allowed_trees=allowed_trees,
        restricted_allowed_files=allowed_files,
        restricted_directory_names=("CACHE", "__pycache__"),
        restricted_file_names=("AUTH.JSON",),
        restricted_file_suffixes=(".SQLITE", ".DB"),
    )
    second = InventoryExclusionPolicy.compile(
        reversed(explicit_roots),
        directory_names=("node_modules", "build", "BUILD"),
        directory_prefixes=("basetemp", "tmp", "TMP"),
        directory_fragments=("pytest", "PYTEST"),
        file_names=(".coverage", "coverage.xml"),
        file_suffixes=(".prof", ".pstats"),
        restricted_roots=(restricted_root,),
        restricted_allowed_trees=reversed(allowed_trees),
        restricted_allowed_files=allowed_files,
        restricted_directory_names=("__PYCACHE__", "cache"),
        restricted_file_names=("auth.json",),
        restricted_file_suffixes=(".db", ".sqlite"),
    )

    assert first.signature == second.signature
    assert first.signature.startswith("inventory-exclusion-policy-v4:xxh3_128:")
    assert len(first.signature.rsplit(":", 1)[1]) == 32
    assert first.directory_names == frozenset({"build", "node_modules"})
    assert first.directory_prefixes == ("basetemp", "tmp")
    assert first.directory_fragments == ("pytest",)
    assert first.file_names == frozenset({".coverage", "coverage.xml"})
    assert first.file_suffixes == (".prof", ".pstats")
    assert first.restricted_roots == (str(restricted_root.resolve()),)
    assert set(first.restricted_allowed_trees) == {str(path.resolve()) for path in allowed_trees}
    assert first.restricted_allowed_files == (str(allowed_files[0].resolve()),)
    assert first.restricted_directory_names == frozenset({"cache", "__pycache__"})
    assert first.restricted_file_names == frozenset({"auth.json"})
    assert first.restricted_file_suffixes == (".db", ".sqlite")

    with pytest.raises(ValueError, match="invalid directory-name exclusion"):
        InventoryExclusionPolicy.compile(directory_names=("build/*",))
    with pytest.raises(ValueError, match="invalid directory-prefix exclusion"):
        InventoryExclusionPolicy.compile(directory_prefixes=("tmp/*",))


def test_default_paths_exclude_codex_cache_and_sandbox_infrastructure() -> None:
    home = Path.home()
    assert DEFAULT_EXCLUDED_PATHS == (
        home / "AppData",
        home / ".codex",
        home / ".cache",
        home / ".sbx-denybin",
        home / "Neocortex" / "Laboratory",
        home / "Neocortex" / "Lab",
        home / "Neocortex" / "Checkpoints",
        home / "Neocortex" / "Backups",
        home / "Neocortex" / "external_backups",
        home / "Neocortex" / "Repository" / "Laboratory",
        home / "Neocortex" / "Laboratories",
        home / "Neocortex" / "TestTemp",
        current_platform_policy().state_directory,
        current_platform_policy().config_directory,
        current_platform_policy().data_directory,
        home / ".local" / "bin",
        home / ".local" / "share" / "applications",
    )


def test_default_policy_excludes_generated_dependencies_without_broad_names(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"

    for directory_name in (
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "site-packages",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".CDX",
        "pytest",
    ):
        assert DEFAULT_INVENTORY_EXCLUSION_POLICY.excludes_directory(
            project / directory_name,
            file_attributes=0,
        )

    for directory_name in (
        ".tmp-handoff",
        "basetemp-second",
        "inline-snapshot-abcd",
        "tmp1mfujc__",
        "title-review-pytest",
    ):
        assert DEFAULT_INVENTORY_EXCLUSION_POLICY.excludes_directory(
            project / directory_name,
            file_attributes=0,
        )

    assert DEFAULT_INVENTORY_EXCLUSION_POLICY.excludes_file(project / "cached.PYC")
    assert DEFAULT_INVENTORY_EXCLUSION_POLICY.excludes_file(project / "optimized.pyo")
    assert not DEFAULT_INVENTORY_EXCLUSION_POLICY.excludes_directory(
        project / "build",
        file_attributes=0,
    )
    assert not DEFAULT_INVENTORY_EXCLUSION_POLICY.excludes_directory(
        project / "dist",
        file_attributes=0,
    )
    assert not DEFAULT_INVENTORY_EXCLUSION_POLICY.excludes_directory(
        project / "templates",
        file_attributes=0,
    )
    assert not DEFAULT_INVENTORY_EXCLUSION_POLICY.excludes_file(project / "document.pdf")


def test_restricted_policy_is_fail_closed_and_denials_take_priority(
    tmp_path: Path,
) -> None:
    restricted_root = tmp_path / "profile" / ".codex"
    allowed_tree = restricted_root / "sessions" / "authored"
    allowed_file = restricted_root / "AGENTS.md"
    policy = InventoryExclusionPolicy.compile(
        (restricted_root,),
        restricted_roots=(restricted_root,),
        restricted_allowed_trees=(allowed_tree,),
        restricted_allowed_files=(allowed_file,),
        restricted_directory_names=("cache",),
        restricted_file_names=("auth.json",),
        restricted_file_suffixes=(".sqlite",),
    )

    assert policy.excludes_directory(restricted_root, file_attributes=0) is False
    assert policy.excludes_directory(restricted_root / "sessions", file_attributes=0) is False
    assert policy.excludes_directory(allowed_tree, file_attributes=0) is False
    assert policy.excludes_directory(allowed_tree / "nested", file_attributes=0) is False
    assert policy.excludes_directory(restricted_root / "unknown", file_attributes=0) is True
    assert policy.excludes_directory(allowed_tree / "cache", file_attributes=0)
    assert policy.excludes_file(allowed_file) is False
    assert policy.excludes_file(allowed_tree / "thread.jsonl") is False
    assert policy.excludes_file(restricted_root / "auth.json")
    assert policy.excludes_file(allowed_tree / "AUTH.JSON")
    assert policy.excludes_file(allowed_tree / "state.SQLITE")
    assert policy.excludes_file(restricted_root / "sessions" / "unknown.jsonl")
    assert (
        policy.excludes_directory(
            tmp_path / "outside" / "cache",
            file_attributes=0,
        )
        is False
    )
    assert policy.excludes_file(tmp_path / "outside" / "auth.json") is False
    assert policy.excludes_file(tmp_path / "outside" / "state.sqlite") is False


def test_restricted_policy_rejects_ambiguous_or_escaping_topology(
    tmp_path: Path,
) -> None:
    restricted_root = tmp_path / "profile" / ".codex"

    with pytest.raises(ValueError, match="must be within a restricted root"):
        InventoryExclusionPolicy.compile(
            restricted_roots=(restricted_root,),
            restricted_allowed_trees=(tmp_path / "outside",),
        )
    with pytest.raises(ValueError, match="must be below a restricted root"):
        InventoryExclusionPolicy.compile(
            restricted_roots=(restricted_root,),
            restricted_allowed_files=(restricted_root,),
        )
    with pytest.raises(ValueError, match="must not contain one another"):
        InventoryExclusionPolicy.compile(
            restricted_roots=(
                restricted_root,
                restricted_root / "nested",
            ),
        )


def test_full_scan_applies_compiled_roots_directory_names_and_file_rules(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = root / "state"
    expected = {
        "AGENTS.md",
        "docs/README.md",
        "pyproject.toml",
        "tests/test_sample.py",
    }
    for relative in expected:
        _write(root / relative)
    for relative in (
        "BUILD/generated.py",
        "pkg/NoDe_MoDuLeS/index.js",
        "state/framework.sqlite3",
        "COVERAGE.XML",
        "profile.PSTATS",
    ):
        _write(root / relative)

    policy = InventoryExclusionPolicy.compile(
        (state,),
        directory_names=("build", "node_modules"),
        file_names=("coverage.xml",),
        file_suffixes=(".pstats",),
    )
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=policy)
        observed = _relative_paths(index, scan.scan_id, root)
        persisted_signature = index.scan_inventory_policy_signature(scan.scan_id)

    assert observed == expected
    assert persisted_signature == policy.signature
    assert scan.files_seen == len(expected)
    assert scan.excluded_directories == 3


def test_full_scan_traverses_only_restricted_allowlisted_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    restricted_root = root / ".codex"
    allowed_tree = restricted_root / "sessions"
    allowed_file = restricted_root / "AGENTS.md"
    expected = {
        ".codex/AGENTS.md",
        ".codex/sessions/thread.jsonl",
        "outside/auth.json",
        "outside/state.sqlite",
    }
    for relative in expected:
        _write(root / relative)
    for relative in (
        ".codex/auth.json",
        ".codex/plugins/plugin.json",
        ".codex/sessions/AUTH.JSON",
        ".codex/sessions/cache/secret.jsonl",
        ".codex/sessions/state.SQLITE",
    ):
        _write(root / relative)

    policy = InventoryExclusionPolicy.compile(
        (restricted_root,),
        restricted_roots=(restricted_root,),
        restricted_allowed_trees=(allowed_tree,),
        restricted_allowed_files=(allowed_file,),
        restricted_directory_names=("cache",),
        restricted_file_names=("auth.json",),
        restricted_file_suffixes=(".sqlite",),
    )
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=policy)
        observed = _relative_paths(index, scan.scan_id, root)

    assert observed == expected
    assert scan.files_seen == len(expected)
    assert scan.excluded_directories == 2


def test_usn_reconciliation_reuses_file_and_directory_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state = root / "state"
    build = root / "BUILD"
    docs = root / "docs"
    for directory in (state, build, docs):
        directory.mkdir(parents=True)
    _write(root / "pyproject.toml")
    _write(docs / "README.md")
    _write(build / "initial.py")
    _write(state / "framework.sqlite3")
    _write(root / "COVERAGE.XML")

    policy = InventoryExclusionPolicy.compile(
        (state,),
        directory_names=("build",),
        file_names=("coverage.xml",),
        file_suffixes=(".pstats",),
    )
    start = JournalCursor("fixture:", 7, 100)
    target = JournalCursor("fixture:", 7, 104)

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=policy)
        _write(docs / "guide.md")
        _write(build / "generated.py")
        _write(state / "state.log")
        _write(root / "trace.PSTATS")
        batch = UsnChangeBatch(
            start,
            target,
            (
                _entry(1, 10, "guide.md", 100),
                _entry(2, 20, "generated.py", 101),
                _entry(3, 30, "state.log", 102),
                _entry(4, 40, "trace.PSTATS", 103),
            ),
        )
        reader = _FakeReader(
            batch,
            {10: docs, 20: build, 30: state, 40: root},
        )

        @contextmanager
        def fake_consume_changes(*_args: object, **_kwargs: object):
            yield reader

        monkeypatch.setattr(
            reconcile_module,
            "consume_changes",
            fake_consume_changes,
        )
        result = reconcile_usn_window(
            index,
            scan.scan_id,
            root,
            start,
            target,
            exclusion_policy=policy,
        )
        observed = _relative_paths(index, scan.scan_id, root)

    assert observed == {"docs/README.md", "docs/guide.md", "pyproject.toml"}
    assert result.records_seen == 4
    assert result.files_upserted == 1
    assert result.files_removed == 0
    assert result.requires_rescan is False


def test_usn_reconciliation_reuses_restricted_fail_closed_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    restricted_root = root / ".codex"
    sessions = restricted_root / "sessions"
    sessions.mkdir(parents=True)
    _write(sessions / "initial.jsonl")
    _write(restricted_root / "AGENTS.md")
    _write(root / "outside.txt")
    policy = InventoryExclusionPolicy.compile(
        (restricted_root,),
        restricted_roots=(restricted_root,),
        restricted_allowed_trees=(sessions,),
        restricted_allowed_files=(restricted_root / "AGENTS.md",),
        restricted_file_names=("auth.json",),
    )
    start = JournalCursor("fixture:", 7, 100)
    target = JournalCursor("fixture:", 7, 104)

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=policy)
        _write(sessions / "allowed.jsonl")
        _write(restricted_root / "unknown.txt")
        _write(sessions / "AUTH.JSON")
        _write(root / "new-outside.txt")
        batch = UsnChangeBatch(
            start,
            target,
            (
                _entry(1, 10, "allowed.jsonl", 100),
                _entry(2, 20, "unknown.txt", 101),
                _entry(3, 10, "AUTH.JSON", 102),
                _entry(4, 30, "new-outside.txt", 103),
            ),
        )
        reader = _FakeReader(
            batch,
            {10: sessions, 20: restricted_root, 30: root},
        )

        @contextmanager
        def fake_consume_changes(*_args: object, **_kwargs: object):
            yield reader

        monkeypatch.setattr(
            reconcile_module,
            "consume_changes",
            fake_consume_changes,
        )
        result = reconcile_usn_window(
            index,
            scan.scan_id,
            root,
            start,
            target,
            exclusion_policy=policy,
        )
        observed = _relative_paths(index, scan.scan_id, root)

    assert observed == {
        ".codex/AGENTS.md",
        ".codex/sessions/allowed.jsonl",
        ".codex/sessions/initial.jsonl",
        "new-outside.txt",
        "outside.txt",
    }
    assert result.records_seen == 4
    assert result.files_upserted == 2
    assert result.files_removed == 0
    assert result.requires_rescan is False


def test_legacy_paths_and_compiled_policy_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    policy = InventoryExclusionPolicy.compile()

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        with pytest.raises(ValueError, match="cannot be supplied together"):
            index.scan(root, excluded_paths=(), exclusion_policy=policy)


def test_interrupted_inventory_is_partial_and_keeps_its_committed_prefix(
    tmp_path: Path,
) -> None:
    class InventoryCancelled(BaseException):
        pass

    root = tmp_path / "corpus"
    root.mkdir()
    for number in range(520):
        _write(root / f"{number:04d}.txt")

    def cancel_after_progress(event) -> None:
        if event.completed >= 512:
            raise InventoryCancelled

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        with pytest.raises(InventoryCancelled):
            index.scan(
                root,
                batch_size=100,
                exclusion_policy=InventoryExclusionPolicy.compile(),
                progress=cancel_after_progress,
            )
        row = index._connection.execute(
            """SELECT status,completed_ns,files_seen,directories_seen,bytes_seen,
            skipped_links,excluded_directories,errors,
            (SELECT COUNT(*) FROM files f WHERE f.scan_id=scans.scan_id),
            (SELECT COALESCE(SUM(size),0) FROM files f
             WHERE f.scan_id=scans.scan_id)
            FROM scans"""
        ).fetchone()

        assert row is not None
        assert row[0] == "partial"
        assert row[1] is not None
        assert row[2:] == (512, 1, 512 * len(b"fixture"), 0, 0, 0, 512, 3584)
        assert index.mark_abandoned_scans() == ()


def test_abandoned_inventory_recovery_is_idempotent_and_invalidates_publication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    database = tmp_path / "inventory.sqlite3"
    policy = InventoryExclusionPolicy.compile()

    with DedupIndex(database) as index:
        cursor = index._connection.execute(
            """INSERT INTO scans(
            root,root_volume_id,root_file_id,root_birthtime_ns,started_ns,
            inventory_policy_signature)
            VALUES(?,?,?,?,?,?)""",
            (str(root), bytes(16), bytes(16), 1, 2, policy.signature),
        )
        assert cursor.lastrowid is not None
        scan_id = int(cursor.lastrowid)
        index._connection.execute(
            """INSERT INTO files(
            scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
            VALUES(?,?,?,?,?,?,?)""",
            (scan_id, str(root / "prefix.bin"), bytes(16), bytes(16), 11, 3, 4),
        )
        index._connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES(?,?,?,?,?,?,?)""",
            (str(root), scan_id, None, None, None, 1, 5),
        )
        index._connection.commit()

        assert index.mark_abandoned_scans() == (scan_id,)
        assert index.mark_abandoned_scans() == ()
        row = index._connection.execute(
            """SELECT status,completed_ns,files_seen,directories_seen,bytes_seen,
            skipped_links,excluded_directories,errors FROM scans WHERE scan_id=?""",
            (scan_id,),
        ).fetchone()
        checkpoint = index._connection.execute(
            "SELECT valid FROM inventory_checkpoints WHERE scan_id=?",
            (scan_id,),
        ).fetchone()

        assert row is not None
        assert row[0] == "partial"
        assert row[1] is not None
        assert row[2:] == (1, 0, 11, 0, 0, 0)
        assert checkpoint == (0,)
        assert index.file_count(scan_id) == 1


def test_prepare_inventory_recovers_abandoned_scan_before_new_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    policy = InventoryExclusionPolicy.compile()

    def stop_before_new_scan(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("stop after recovery")

    monkeypatch.setattr(
        inventory_coordinator_module,
        "_full_inventory_without_journal",
        stop_before_new_scan,
    )
    with DedupIndex(state_directory / "dedup.sqlite3") as index:
        cursor = index._connection.execute(
            """INSERT INTO scans(
            root,root_volume_id,root_file_id,root_birthtime_ns,started_ns,
            inventory_policy_signature)
            VALUES(?,?,?,?,?,?)""",
            (str(root), bytes(16), bytes(16), 1, 2, policy.signature),
        )
        assert cursor.lastrowid is not None
        abandoned_scan_id = int(cursor.lastrowid)
        index._connection.commit()

        with FrameworkState(state_directory / "framework.sqlite3") as state:
            run_id = state.begin_initial_run(root, None)
            with pytest.raises(RuntimeError, match="stop after recovery"):
                inventory_coordinator_module.prepare_inventory(
                    index,
                    state,
                    run_id,
                    root,
                    None,
                    progress=lambda _event: None,
                    exclusion_policy=policy,
                )
            state.fail_initial_run(run_id)

        assert index._connection.execute(
            "SELECT status FROM scans WHERE scan_id=?",
            (abandoned_scan_id,),
        ).fetchone() == ("partial",)


def test_prepare_inventory_can_bypass_incremental_without_invalidating_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _write(root / "source.py")
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    cursor = JournalCursor("fixture:", 7, 100)
    policy = InventoryExclusionPolicy.compile()

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=policy)
        checkpoint = InventoryCheckpoint(
            str(root),
            scan.scan_id,
            cursor.volume,
            cursor.journal_id,
            cursor.next_usn,
            True,
            policy.signature,
        )
        index.bind_inventory_checkpoint(checkpoint)

        def forbidden_incremental(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("incremental inventory must be bypassed")

        def fail_full_inventory(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced full inventory failure")

        monkeypatch.setattr(
            inventory_coordinator_module,
            "_try_incremental_inventory",
            forbidden_incremental,
        )
        monkeypatch.setattr(
            inventory_coordinator_module,
            "_full_inventory",
            fail_full_inventory,
        )
        with FrameworkState(state_directory / "framework.sqlite3") as state:
            run_id = state.begin_initial_run(root, cursor)
            with pytest.raises(RuntimeError, match="forced full inventory failure"):
                inventory_coordinator_module.prepare_inventory(
                    index,
                    state,
                    run_id,
                    root,
                    cursor,
                    progress=lambda _event: None,
                    exclusion_policy=policy,
                    allow_incremental=False,
                )
            state.fail_initial_run(run_id)

        assert index.inventory_checkpoint(root) == checkpoint


def test_full_inventory_retry_persists_effective_attempt_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _write(root / "source.py")
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    initial_cursor = JournalCursor("fixture:", 7, 10)
    first_start = JournalCursor("fixture:", 7, 20)
    first_target = JournalCursor("fixture:", 7, 21)
    effective_start = JournalCursor("fixture:", 7, 30)
    effective_target = JournalCursor("fixture:", 7, 31)
    cursor_sequence = iter((first_start, first_target, effective_start, effective_target))
    observed_starts: list[JournalCursor] = []
    policy = InventoryExclusionPolicy.compile()

    monkeypatch.setattr(
        inventory_coordinator_module,
        "query_journal_cursor",
        lambda _volume: next(cursor_sequence),
    )

    def fake_reconcile(
        _index: DedupIndex,
        _scan_id: int,
        _root: Path,
        start: JournalCursor,
        target: JournalCursor,
        **_kwargs: object,
    ) -> reconcile_module.ReconcileResult:
        observed_starts.append(start)
        return reconcile_module.ReconcileResult(
            target,
            records_seen=0,
            files_upserted=0,
            files_removed=0,
            requires_rescan=len(observed_starts) == 1,
        )

    monkeypatch.setattr(
        inventory_coordinator_module,
        "reconcile_usn_window",
        fake_reconcile,
    )

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        with FrameworkState(state_directory / "framework.sqlite3") as state:
            run_id = state.begin_initial_run(root, initial_cursor)
            prepared = inventory_coordinator_module.prepare_inventory(
                index,
                state,
                run_id,
                root,
                initial_cursor,
                progress=lambda _event: None,
                exclusion_policy=policy,
                allow_incremental=False,
            )
            persisted_cursor = state._connection.execute(
                """SELECT journal_volume,journal_id,start_usn
                FROM initial_runs WHERE run_id=?""",
                (run_id,),
            ).fetchone()

    assert prepared.inventory_mode == "full"
    assert prepared.inventory_attempts == 2
    assert prepared.journal_before == effective_start
    assert observed_starts == [first_start, effective_start]
    assert persisted_cursor == ("fixture:", "7", effective_start.next_usn)


def test_policy_change_forces_a_new_full_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    internal = root / "internal"
    root.mkdir()
    _write(root / "visible.py")
    _write(internal / "generated.py")
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    cursor = JournalCursor("fixture:", 7, 100)
    old_policy = InventoryExclusionPolicy.compile()
    new_policy = InventoryExclusionPolicy.compile(directory_names=("internal",))

    monkeypatch.setattr(
        inventory_coordinator_module,
        "query_journal_cursor",
        lambda _volume: cursor,
    )

    def unchanged_reconciliation(
        _index: DedupIndex,
        _scan_id: int,
        _root: Path,
        _start: JournalCursor,
        target: JournalCursor,
        **_kwargs: object,
    ) -> reconcile_module.ReconcileResult:
        return reconcile_module.ReconcileResult(target, 0, 0, 0, False)

    monkeypatch.setattr(
        inventory_coordinator_module,
        "reconcile_usn_window",
        unchanged_reconciliation,
    )

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        old_scan = index.scan(root, exclusion_policy=old_policy)
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(
                str(root),
                old_scan.scan_id,
                cursor.volume,
                cursor.journal_id,
                cursor.next_usn,
                True,
                old_policy.signature,
            )
        )
        with FrameworkState(state_directory / "framework.sqlite3") as state:
            run_id = state.begin_initial_run(root, cursor)
            prepared = inventory_coordinator_module.prepare_inventory(
                index,
                state,
                run_id,
                root,
                cursor,
                progress=lambda _event: None,
                exclusion_policy=new_policy,
            )
            state.fail_initial_run(run_id)

        checkpoint = index.inventory_checkpoint(root)
        observed = _relative_paths(index, prepared.scan.scan_id, root)

    assert prepared.inventory_mode == "full"
    assert prepared.scan.scan_id != old_scan.scan_id
    assert prepared.inventory_policy_signature == new_policy.signature
    assert checkpoint is not None
    assert checkpoint.inventory_policy_signature == new_policy.signature
    assert observed == {"visible.py"}


def test_usn_rejects_a_policy_mismatch_before_consuming_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _write(root / "source.py")
    old_policy = InventoryExclusionPolicy.compile()
    changed_policy = InventoryExclusionPolicy.compile(directory_names=("internal",))
    cursor = JournalCursor("fixture:", 7, 100)
    consumed = False

    @contextmanager
    def forbidden_consume(*_args: object, **_kwargs: object):
        nonlocal consumed
        consumed = True
        yield

    monkeypatch.setattr(reconcile_module, "consume_changes", forbidden_consume)
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=old_policy)
        with pytest.raises(InventoryError, match="policy signature does not match"):
            reconcile_usn_window(
                index,
                scan.scan_id,
                root,
                cursor,
                cursor,
                exclusion_policy=changed_policy,
            )

    assert consumed is False


# endregion [02]
