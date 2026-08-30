"""Regression contracts for isolated generations and policy-bound schema v8.
# region [00] Contexto del módulo
# Módulo: tests/test_inventory_generation_v7.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


Every fixture is synthetic and bounded to ``tmp_path``.  No test opens an
operational NeoCortex database or traverses the live corpus.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.enumeration import JournalCursor, NtfsEntry, UsnChangeBatch
from neocortex.deduplication import (
    DedupIndex,
    InventoryCheckpoint,
    InventoryExclusionPolicy,
    snapshot_path,
)
from neocortex.deduplication.inventory import scan as inventory_scan_module
from neocortex.deduplication import persistence as inventory_schema_module
from neocortex.deduplication.domain.errors import InventoryError
from neocortex.deduplication.persistence.ddl import (
    LEGACY_SHARED_DDL,
    V6_GENERATIONAL_DDL,
)
from neocortex.integrations.inventory import reconcile as reconcile_module
from neocortex.integrations.inventory.reconcile import reconcile_usn_window
# endregion [01]

# region [02] Implementación


_EMPTY_POLICY = InventoryExclusionPolicy.compile(())


def _checkpoint(root: Path, scan_id: int, next_usn: int) -> InventoryCheckpoint:
    return InventoryCheckpoint(
        str(root),
        scan_id,
        "fixture:",
        7,
        next_usn,
        True,
        _EMPTY_POLICY.signature,
    )


def _create_populated_v6(database: Path, root: Path) -> None:
    """Create the exact historical v6 schema without faking a downgrade."""

    identity = bytes(16)
    with sqlite3.connect(database) as connection:
        for statement in V6_GENERATIONAL_DDL:
            connection.execute(statement)
        for statement in LEGACY_SHARED_DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','6')")
        connection.execute(
            """INSERT INTO scans(
            scan_id,root,root_volume_id,root_file_id,root_birthtime_ns,
            started_ns,completed_ns,files_seen,directories_seen,bytes_seen,
            skipped_links,excluded_directories,errors)
            VALUES(7,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(root), identity, identity, 1, 10, 20, 2, 1, 7, 0, 0, 0),
        )
        connection.executemany(
            """INSERT INTO files(
            path,volume_id,file_id,size,mtime_ns,birthtime_ns,scan_id)
            VALUES(?,?,?,?,?,?,7)""",
            (
                (
                    str(root / "a.bin"),
                    identity,
                    (1).to_bytes(16, "little"),
                    3,
                    4,
                    5,
                ),
                (
                    str(root / "b.bin"),
                    identity,
                    (2).to_bytes(16, "little"),
                    4,
                    5,
                    6,
                ),
            ),
        )
        connection.execute(
            """INSERT INTO inventory_checkpoints(
            root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
            VALUES(?,7,'fixture:','9',123,1,777)""",
            (str(root),),
        )


def test_populated_v6_migration_preserves_rows_checkpoint_and_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v6.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v6(database, root)

    inventory_schema_module.initialize_inventory_schema(database)
    inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("10",)
        assert connection.execute(
            """SELECT scan_id,status,files_seen,bytes_seen,
            inventory_policy_signature FROM scans"""
        ).fetchone() == (7, "complete", 2, 7, None)
        assert connection.execute(
            "SELECT path,size,scan_id FROM files ORDER BY path"
        ).fetchall() == [
            (str(root / "a.bin"), 3, 7),
            (str(root / "b.bin"), 4, 7),
        ]
        assert connection.execute(
            """SELECT root,scan_id,volume,journal_id,next_usn,valid,updated_ns
            FROM inventory_checkpoints"""
        ).fetchone() == (str(root), 7, "fixture:", "9", 123, 0, 777)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        inventory_schema_module.validate_inventory_schema(connection)


def test_v6_extra_column_preflight_abstains_and_preserves_v6_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory-v6-drift.sqlite3"
    root = tmp_path / "historical-root"
    _create_populated_v6(database, root)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE files ADD COLUMN unknown_payload TEXT")
        connection.execute(
            "UPDATE files SET unknown_payload='preserve-me' WHERE path=?",
            (str(root / "a.bin"),),
        )

    with pytest.raises(InventoryError):
        inventory_schema_module.initialize_inventory_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("6",)
        assert connection.execute(
            "SELECT unknown_payload FROM files WHERE path=?",
            (str(root / "a.bin"),),
        ).fetchone() == ("preserve-me",)
        assert "status" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(scans)")
        }
        assert connection.execute(
            """SELECT COUNT(*) FROM sqlite_master
            WHERE name IN ('files_v6','inventory_checkpoints_v6')"""
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone() == (2,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_same_path_is_isolated_until_atomic_checkpoint_switch(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "same.bin"
    source.write_bytes(b"old")

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        first = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, first.scan_id, 10))
        source.write_bytes(b"new-payload")
        second = index.scan(root, excluded_paths=())

        assert [item.size for item in index.snapshots(first.scan_id)] == [3]
        assert [item.size for item in index.snapshots(second.scan_id)] == [11]
        assert index.inventory_checkpoint(root) == _checkpoint(root, first.scan_id, 10)
        assert [item.size for item in index.published_snapshots(root)] == [3]

        index.bind_inventory_checkpoint(_checkpoint(root, second.scan_id, 20))

        assert index.inventory_checkpoint(root) == _checkpoint(root, second.scan_id, 20)
        assert [item.size for item in index.published_snapshots(root)] == [11]


def test_partial_scan_persists_partial_state_and_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    (root / "visible.bin").write_bytes(b"visible")
    (blocked / "hidden.bin").write_bytes(b"hidden")
    real_scandir = os.scandir

    def fail_blocked(path: str | os.PathLike[str]):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(blocked)):
            raise PermissionError("synthetic denied directory")
        return real_scandir(path)

    monkeypatch.setattr(inventory_scan_module.os, "scandir", fail_blocked)
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        with pytest.raises(InventoryError, match="partial"):
            index.scan(root, excluded_paths=())

        row = index._connection.execute(
            """SELECT status,errors,completed_ns IS NOT NULL
            FROM scans ORDER BY scan_id DESC LIMIT 1"""
        ).fetchone()
        assert row == ("partial", 1, 1)
        assert index.inventory_checkpoint(root) is None


def test_bind_rejects_internally_inconsistent_complete_scan(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "one.bin").write_bytes(b"one")

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        with index._connection:
            index._connection.execute(
                "UPDATE scans SET files_seen=files_seen+1,bytes_seen=bytes_seen+1 WHERE scan_id=?",
                (scan.scan_id,),
            )

        with pytest.raises(InventoryError, match="internally consistent"):
            index.bind_inventory_checkpoint(_checkpoint(root, scan.scan_id, 10))
        assert index.inventory_checkpoint(root) is None


def test_published_snapshots_holds_reader_generation_across_publish_and_prune(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.bin").write_bytes(b"a")
    (root / "b.bin").write_bytes(b"bb")
    database = tmp_path / "inventory.sqlite3"

    with DedupIndex(database) as publisher:
        first = publisher.scan(root, excluded_paths=())
        publisher.bind_inventory_checkpoint(_checkpoint(root, first.scan_id, 10))

        with DedupIndex(database) as reader:
            old_rows = reader.published_snapshots(root)
            first_old_row = next(old_rows)

            (root / "c.bin").write_bytes(b"ccc")
            second = publisher.scan(root, excluded_paths=())
            publisher.bind_inventory_checkpoint(_checkpoint(root, second.scan_id, 20))
            publisher.prune_obsolete_state(protected_scan_ids=())

            old_names = [Path(first_old_row.path).name]
            old_names.extend(Path(item.path).name for item in old_rows)
            assert old_names == ["a.bin", "b.bin"]
            assert publisher.file_count(first.scan_id) == 2
            assert [Path(item.path).name for item in reader.published_snapshots(root)] == [
                "a.bin",
                "b.bin",
                "c.bin",
            ]


def test_prune_retains_building_and_complete_publish_candidate(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "same.bin"
    source.write_bytes(b"data")

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        published = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, published.scan_id, 10))
        candidate = index.scan(root, excluded_paths=())
        identity = bytes(16)
        with index._connection:
            cursor = index._connection.execute(
                "INSERT INTO scans(root,started_ns) VALUES(?,?)",
                (str(root), 1),
            )
            assert cursor.lastrowid is not None
            building_scan_id = int(cursor.lastrowid)
            index._connection.execute(
                """INSERT INTO files(
                scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    building_scan_id,
                    str(root / "building-only.bin"),
                    identity,
                    (99).to_bytes(16, "little"),
                    1,
                    1,
                    1,
                ),
            )

        removed = index.prune_obsolete_state()

        assert removed["files"] == 0
        assert index.file_count(published.scan_id) == 1
        assert index.file_count(candidate.scan_id) == 1
        assert index.file_count(building_scan_id) == 1


class _FakeIndex:
    def __init__(self) -> None:
        self.applied: list[dict[str, object]] = []

    def contains_identity(self, _scan_id: int, _volume_id: int, _file_id: int) -> bool:
        return False

    def require_scan_inventory_policy_signature(
        self,
        _scan_id: int,
        expected_signature: str,
    ) -> None:
        assert expected_signature == _EMPTY_POLICY.signature

    def apply_reconciliation(
        self,
        scan_id: int,
        *,
        upserts,
        remove_paths,
        remove_identities,
        checkpoint,
    ) -> None:
        self.applied.append(
            {
                "scan_id": scan_id,
                "upserts": tuple(upserts),
                "remove_paths": frozenset(remove_paths),
                "remove_identities": frozenset(remove_identities),
                "checkpoint": checkpoint,
            }
        )


class _FakeReader:
    def __init__(
        self,
        batches: tuple[UsnChangeBatch, ...],
        resolved_parents: dict[int, Path],
    ) -> None:
        self._batches = batches
        self._resolved_parents = resolved_parents

    def iter_until(self, _target_usn: int):
        yield from self._batches

    def resolve_path(self, file_id: int) -> str:
        try:
            return str(self._resolved_parents[file_id])
        except KeyError:
            raise OSError("synthetic unresolved FileId") from None


def _entry(file_id: int, parent_id: int, name: str, usn: int) -> NtfsEntry:
    return NtfsEntry(file_id, parent_id, name, usn, None, 0, 0, 0, 0, 3, 0)


def test_ambiguous_reconciliation_does_not_apply_or_advance_unsafe_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = tmp_path / "created.bin"
    created.write_bytes(b"created")
    start = JournalCursor("C:", 7, 100)
    middle = JournalCursor("C:", 7, 101)
    target = JournalCursor("C:", 7, 102)
    batches = (
        UsnChangeBatch(start, middle, (_entry(1, 10, created.name, 100),)),
        UsnChangeBatch(middle, target, (_entry(2, 99, "unknown.bin", 101),)),
    )
    reader = _FakeReader(batches, {10: tmp_path})

    @contextmanager
    def fake_consume_changes(*_args: object, **_kwargs: object):
        yield reader

    monkeypatch.setattr(reconcile_module, "consume_changes", fake_consume_changes)
    index = _FakeIndex()

    result = reconcile_usn_window(
        index,
        5,
        tmp_path,
        start,
        target,
        persist_checkpoint=True,
        excluded_paths=(),
    )

    assert result.requires_rescan is True
    assert result.cursor == middle
    assert result.records_seen == 2
    assert result.files_upserted == 1
    assert len(index.applied) == 1
    checkpoint = index.applied[0]["checkpoint"]
    assert isinstance(checkpoint, InventoryCheckpoint)
    assert checkpoint.valid is True
    assert checkpoint.next_usn == middle.next_usn


def test_incremental_checkpoint_and_aggregates_commit_or_rollback_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    first_path = root / "first.bin"
    first_path.write_bytes(b"first")
    second_path = root / "second.bin"

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        index.bind_inventory_checkpoint(_checkpoint(root, scan.scan_id, 10))
        second_path.write_bytes(b"second-file")
        second_snapshot = snapshot_path(second_path)
        advanced = _checkpoint(root, scan.scan_id, 20)

        def fail_checkpoint(_checkpoint_value: InventoryCheckpoint) -> None:
            raise RuntimeError("synthetic checkpoint failure")

        with monkeypatch.context() as patcher:
            patcher.setattr(index, "_write_inventory_checkpoint", fail_checkpoint)
            with pytest.raises(RuntimeError, match="synthetic checkpoint failure"):
                index.apply_reconciliation(
                    scan.scan_id,
                    upserts=(second_snapshot,),
                    checkpoint=advanced,
                )

        rolled_back = index.scan_summary(scan.scan_id)
        assert rolled_back.files_seen == 1
        assert rolled_back.bytes_seen == len(b"first")
        assert index.file_count(scan.scan_id) == 1
        assert index.inventory_checkpoint(root) == _checkpoint(root, scan.scan_id, 10)

        index.apply_reconciliation(
            scan.scan_id,
            upserts=(second_snapshot,),
            checkpoint=advanced,
        )

        committed = index.scan_summary(scan.scan_id)
        assert committed.files_seen == 2
        assert committed.bytes_seen == len(b"first") + len(b"second-file")
        assert index.file_count(scan.scan_id) == 2
        assert index.inventory_checkpoint(root) == advanced


# endregion [02]
