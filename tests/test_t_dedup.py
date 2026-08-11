# region [00] Contexto del módulo
# Módulo: tests/test_t_dedup.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
import ctypes
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from _02_Deduplicacion import (
    DedupIndex,
    DedupPlanner,
    InventoryCheckpoint,
    files_equal_exact,
    full_fingerprint,
    partial_fingerprint,
    snapshot_path,
)
# endregion [01]

# region [02] Implementación


class HashingTests(unittest.TestCase):
    def test_hashes_and_exact_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.bin"
            right = root / "right.bin"
            other = root / "other.bin"
            left.write_bytes(b"abc" * 10_000)
            right.write_bytes(left.read_bytes())
            other.write_bytes(b"abd" * 10_000)
            left_snapshot = snapshot_path(left)
            right_snapshot = snapshot_path(right)
            other_snapshot = snapshot_path(other)
            self.assertEqual(full_fingerprint(left_snapshot), full_fingerprint(right_snapshot))
            self.assertEqual(
                partial_fingerprint(left_snapshot), partial_fingerprint(right_snapshot)
            )
            self.assertTrue(files_equal_exact(left_snapshot, right_snapshot))
            self.assertFalse(files_equal_exact(left_snapshot, other_snapshot))


class PlannerTests(unittest.TestCase):
    def test_migrates_schema_one_to_streamed_plan_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;"
                "INSERT INTO metadata VALUES('schema_version','1');"
            )
            connection.commit()
            connection.close()
            with DedupIndex(database):
                pass
            connection = sqlite3.connect(database)
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            connection.close()
            self.assertEqual(version, "10")
            self.assertIn("planned_duplicate_groups", tables)
            self.assertIn("planned_duplicate_members", tables)
            self.assertIn("inventory_checkpoints", tables)

    @unittest.skipUnless(os.name == "nt", "legacy drive-letter checkpoint is Windows-only")
    def test_schema_three_fixture_invalidates_the_old_inventory_checkpoint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(
                        key TEXT PRIMARY KEY,value TEXT NOT NULL
                    ) WITHOUT ROWID;
                    INSERT INTO metadata VALUES('schema_version','3');
                    CREATE TABLE scans(
                        scan_id INTEGER PRIMARY KEY,root TEXT NOT NULL,
                        started_ns INTEGER NOT NULL,completed_ns INTEGER,
                        files_seen INTEGER,directories_seen INTEGER,
                        bytes_seen INTEGER,skipped_links INTEGER,
                        excluded_directories INTEGER,errors INTEGER
                    );
                    INSERT INTO scans VALUES(
                        7,'C:\\corpus',1,2,0,1,0,0,0,0
                    );
                    CREATE TABLE inventory_checkpoints(
                        root TEXT PRIMARY KEY COLLATE NOCASE,
                        scan_id INTEGER NOT NULL,volume TEXT NOT NULL,
                        journal_id TEXT NOT NULL,next_usn INTEGER NOT NULL,
                        valid INTEGER NOT NULL,updated_ns INTEGER NOT NULL
                    ) WITHOUT ROWID;
                    INSERT INTO inventory_checkpoints VALUES(
                        'C:\\corpus',7,'C:','1',100,1,1
                    );
                    """
                )
                connection.commit()
            with DedupIndex(database) as index:
                checkpoint = index.inventory_checkpoint("C:\\corpus")
            assert checkpoint is not None
            self.assertFalse(checkpoint.valid)
            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, "10")

    def test_reuses_inventory_checkpoint_and_advances_it_with_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "corpus"
            root.mkdir()
            original = root / "original.bin"
            original.write_bytes(b"original")
            with DedupIndex(base / "state.db") as index:
                scan = index.scan(root, excluded_paths=())
                index.bind_inventory_checkpoint(
                    InventoryCheckpoint(str(root), scan.scan_id, "C:", 7, 100)
                )
                created = root / "created.bin"
                created.write_bytes(b"created")
                index.apply_reconciliation(
                    scan.scan_id,
                    upserts=(snapshot_path(created),),
                    checkpoint=InventoryCheckpoint(str(root), scan.scan_id, "C:", 7, 120),
                )
                index.refresh_scan_aggregates(scan.scan_id)
                checkpoint = index.inventory_checkpoint(root)
                summary = index.scan_summary(scan.scan_id)
                names = {Path(item.path).name for item in index.snapshots(scan.scan_id)}
            assert checkpoint is not None
            self.assertEqual(checkpoint.next_usn, 120)
            self.assertTrue(checkpoint.valid)
            self.assertEqual(summary.files_seen, 2)
            self.assertEqual(names, {"original.bin", "created.bin"})

    def test_reconciliation_removes_the_exact_inventory_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "corpus"
            root.mkdir()
            source = root / "removed.bin"
            source.write_bytes(b"removed")
            with DedupIndex(base / "state.db") as index:
                scan = index.scan(root, excluded_paths=())
                snapshot = next(index.snapshots(scan.scan_id))
                index.apply_reconciliation(
                    scan.scan_id,
                    remove_identities=((snapshot.volume_id, snapshot.file_id),),
                )
                remaining = tuple(index.snapshots(scan.scan_id))

            self.assertEqual(remaining, ())

    def test_scan_excludes_only_exact_configured_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            (root / "AppData" / "Local").mkdir(parents=True)
            (root / ".CoDeX" / "state").mkdir(parents=True)
            (root / "Workspace" / "AppData").mkdir(parents=True)
            (root / "Workspace" / ".codex").mkdir(parents=True)
            (root / "AppData" / "Local" / "direct_appdata.bin").write_bytes(b"ignored")
            (root / ".CoDeX" / "state" / "direct_codex.bin").write_bytes(b"ignored")
            (root / "Workspace" / "AppData" / "nested_appdata.bin").write_bytes(b"visible")
            (root / "Workspace" / ".codex" / "nested_codex.bin").write_bytes(b"visible")
            with DedupIndex(Path(directory) / "state.db") as index:
                # Defaults point at the actual user profile, not this corpus.
                default_scan = index.scan(root)
                self.assertEqual(default_scan.files_seen, 4)

                scan = index.scan(
                    root,
                    excluded_paths=(root / "AppData", root / ".codex"),
                )
                names = {Path(item.path).name for item in index.snapshots(scan.scan_id)}
                expected_names = {"nested_appdata.bin", "nested_codex.bin"}
                expected_exclusions = 2
                if os.name != "nt":
                    expected_names.add("direct_codex.bin")
                    expected_exclusions = 1
                self.assertEqual(
                    names,
                    expected_names,
                )
                self.assertEqual(scan.excluded_directories, expected_exclusions)

    def test_scan_always_excludes_internal_quarantine_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            quarantine = root / ".dedupe-quarantine-20260720-010203"
            quarantine.mkdir(parents=True)
            (quarantine / "ignored.bin").write_bytes(b"internal")
            (root / "visible.bin").write_bytes(b"visible")

            with DedupIndex(Path(directory) / "state.db") as index:
                scan = index.scan(root, excluded_paths=())
                names = {Path(item.path).name for item in index.snapshots(scan.scan_id)}

            self.assertEqual(names, {"visible.bin"})
            self.assertEqual(scan.excluded_directories, 1)

    @unittest.skipUnless(os.name == "nt", "Windows hidden attributes are required")
    def test_scan_skips_directories_with_hidden_attribute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            hidden = root / "Private"
            visible = root / "Visible"
            hidden.mkdir(parents=True)
            visible.mkdir()
            (hidden / "hidden.bin").write_bytes(b"hidden")
            (visible / "visible.bin").write_bytes(b"visible")
            kernel32 = ctypes.windll.kernel32
            attributes = kernel32.GetFileAttributesW(str(hidden))
            self.assertNotEqual(attributes, 0xFFFFFFFF)
            self.assertTrue(kernel32.SetFileAttributesW(str(hidden), attributes | 0x2))
            try:
                with DedupIndex(Path(directory) / "state.db") as index:
                    scan = index.scan(root, excluded_paths=())
                    names = {Path(item.path).name for item in index.snapshots(scan.scan_id)}
                self.assertEqual(names, {"visible.bin"})
                self.assertEqual(scan.excluded_directories, 1)
            finally:
                kernel32.SetFileAttributesW(str(hidden), attributes)

    def test_plans_only_exact_duplicates_and_keeps_newest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            root.mkdir()
            older = root / "older.dat"
            newer = root / "newer.dat"
            collision = root / "same_size_not_duplicate.dat"
            unique = root / "unique.dat"
            older.write_bytes(b"duplicate-content")
            newer.write_bytes(b"duplicate-content")
            collision.write_bytes(b"different-content")
            unique.write_bytes(b"unique")
            os.utime(older, ns=(1_700_000_000_000_000_000,) * 2)
            os.utime(newer, ns=(1_710_000_000_000_000_000,) * 2)

            database = Path(directory) / "state.db"
            with DedupIndex(database) as index:
                scan = index.scan(root, batch_size=2)
                plan = DedupPlanner(index, partial_threshold=0).plan(scan.scan_id, preview_limit=1)
                self.assertEqual(scan.files_seen, 4)
                self.assertEqual(len(plan.groups), 1)
                group = plan.groups[0]
                self.assertEqual(Path(group.keep.path).name, "newer.dat")
                self.assertEqual([Path(item.path).name for item in group.redundant], ["older.dat"])
                self.assertEqual(group.reclaimable_bytes, len(b"duplicate-content"))
                self.assertNotIn(
                    "same_size_not_duplicate.dat",
                    {Path(item.path).name for item in group.redundant},
                )

    def test_second_plan_reuses_cached_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            root.mkdir()
            (root / "a").write_bytes(b"x" * 20_000)
            (root / "b").write_bytes(b"x" * 20_000)
            with DedupIndex(Path(directory) / "state.db") as index:
                scan = index.scan(root)
                planner = DedupPlanner(index, partial_threshold=0)
                first = planner.plan(scan.scan_id)
                second = planner.plan(scan.scan_id)
                self.assertEqual(first.statistics.partial_hash_files, 2)
                self.assertEqual(first.statistics.full_hash_files, 2)
                self.assertEqual(second.statistics.partial_hash_files, 0)
                self.assertEqual(second.statistics.full_hash_files, 0)

    def test_persists_full_plan_and_only_materializes_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            root.mkdir()
            for name, content in (
                ("a1", b"first-group"),
                ("a2", b"first-group"),
                ("b1", b"second-group-longer"),
                ("b2", b"second-group-longer"),
            ):
                (root / name).write_bytes(content)
            with DedupIndex(Path(directory) / "state.db") as index:
                scan = index.scan(root)
                plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=1)
                streamed = list(index.iter_duplicate_groups(scan.scan_id))
            self.assertEqual(plan.group_count, 2)
            self.assertEqual(plan.redundant_files, 2)
            self.assertEqual(len(plan.groups), 1)
            self.assertEqual(len(streamed), 2)
            self.assertGreaterEqual(streamed[0].reclaimable_bytes, streamed[1].reclaimable_bytes)

    def test_large_duplicate_group_uses_bounded_member_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b"bounded-duplicate-group"
            for item_number in range(1050):
                (root / f"copy-{item_number:04d}.bin").write_bytes(payload)
            with DedupIndex(root / "state.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                plan = DedupPlanner(index).plan(
                    scan.scan_id,
                    exact_compare=False,
                    preview_limit=0,
                )
                groups = list(index.iter_duplicate_groups(scan.scan_id))
            self.assertEqual(plan.redundant_files, 1049)
            self.assertEqual(sum(len(group.redundant) for group in groups), 1049)
            self.assertLessEqual(max(len(group.redundant) for group in groups), 1024)
            self.assertEqual(len({group.keep.path for group in groups}), 1)

    def test_zero_byte_files_bypass_hash_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            root.mkdir()
            (root / "placeholder-a").touch()
            (root / "placeholder-b").touch()
            with DedupIndex(Path(directory) / "state.db") as index:
                scan = index.scan(root)
                plan = DedupPlanner(index).plan(scan.scan_id)
            self.assertEqual(plan.group_count, 0)
            self.assertEqual(plan.redundant_files, 0)
            self.assertEqual(plan.statistics.size_candidate_files, 0)
            self.assertEqual(plan.reclaimable_bytes, 0)

    def test_fast_policy_does_not_compare_files_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            root.mkdir()
            (root / "a.bin").write_bytes(b"same-content")
            (root / "b.bin").write_bytes(b"same-content")
            with DedupIndex(Path(directory) / "state.db") as index:
                scan = index.scan(root)
                with patch(
                    "_02_Deduplicacion.planner.files_equal_exact",
                    side_effect=AssertionError("byte comparison must not run"),
                ):
                    plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False)
            self.assertEqual(plan.group_count, 1)
            self.assertEqual(plan.statistics.exact_compare_files, 0)


if __name__ == "__main__":
    unittest.main()
# endregion [02]
