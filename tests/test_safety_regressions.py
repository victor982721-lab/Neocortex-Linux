"""Regression tests for filesystem mutation-boundary invariants."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import (
    DedupIndex,
    DedupPlanner,
    FileChangedError,
    FileSnapshot,
    full_fingerprint,
    snapshot_path,
)
from neocortex.deduplication.domain.errors import InventoryError
from neocortex.workflow.actions import action_policy
from neocortex.workflow.actions.action_policy import same_snapshot
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.platform.content_types import DetectedType
from neocortex.safety.corpus_access import ProtectedAnalysisRootError
from neocortex.capabilities.formats.image.route import _same_snapshot as image_same_snapshot
from neocortex.runtime.models import ActionSummary
from neocortex.capabilities.formats.pdf.pdf_isolation import _source_matches
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run


# region [01] Test support


class _DirectoryLinkTestCase(unittest.TestCase):
    def create_directory_link(self, target: Path, link: Path) -> None:
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")


def _framework_database(base: Path) -> Path:
    state_directory = base / "state"
    state_directory.mkdir(exist_ok=True)
    return state_directory / "framework.sqlite3"


# endregion [01]


# region [02] Root boundary regressions


class InventoryRootSafetyTests(_DirectoryLinkTestCase):
    def test_normal_temporary_directory_remains_a_valid_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            (root / "item.bin").write_bytes(b"content")

            with DedupIndex(base / "state.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                root_snapshot = snapshot_path(root)
                root_identity = index.scan_root_identity(scan.scan_id)

            self.assertEqual(scan.files_seen, 1)
            self.assertEqual(Path(scan.root), Path(os.path.realpath(root)))
            self.assertEqual(
                root_identity,
                (
                    root_snapshot.volume_id,
                    root_snapshot.file_id,
                    root_snapshot.birthtime_ns,
                ),
            )

    def test_reparse_root_is_rejected_before_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir()
            (target / "sensitive.bin").write_bytes(b"content")
            link = base / "linked-root"
            self.create_directory_link(target, link)

            with DedupIndex(base / "state.sqlite3") as index:
                with self.assertRaisesRegex(InventoryError, "symlink, junction, or reparse point"):
                    index.scan(link, excluded_paths=())

    def test_exclusion_alias_is_canonicalized_with_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            root = target / "corpus"
            excluded = root / "excluded"
            excluded.mkdir(parents=True)
            (excluded / "private.bin").write_bytes(b"private")
            (root / "visible.bin").write_bytes(b"visible")
            alias = base / "alias"
            self.create_directory_link(target, alias)

            with DedupIndex(base / "state.sqlite3") as index:
                scan = index.scan(
                    alias / "corpus",
                    excluded_paths=(alias / "corpus" / "excluded",),
                )

            self.assertEqual(scan.files_seen, 1)
            self.assertEqual(scan.excluded_directories, 1)
            self.assertEqual(Path(scan.root), Path(os.path.realpath(root)))

    def test_run_rejects_a_root_replaced_by_a_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            (root / "item.bin").write_bytes(b"content")

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                index.scan(root, excluded_paths=())
                moved_root = base / "moved-corpus"
                root.rename(moved_root)
                self.create_directory_link(moved_root, root)
                with self.assertRaisesRegex(ValueError, "cannot be a symlink or reparse point"):
                    state.begin_initial_run(
                        root,
                        JournalCursor(root.drive, 1, 0),
                    )

    def test_apply_rejects_root_replaced_by_an_ordinary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            (root / "original.bin").write_bytes(b"content")

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                plan = DedupPlanner(index).plan(scan.scan_id)
                root.rename(base / "original-corpus")
                root.mkdir()
                (root / "replacement.bin").write_bytes(b"replacement")
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )

                with (
                    patch("neocortex.workflow.actions.actions.send2trash") as trash,
                    self.assertRaisesRegex(
                        RuntimeError,
                        "framework run root does not match the inventory scan root",
                    ),
                ):
                    actions.execute(plan)

                trash.assert_not_called()

    def test_mutated_empty_file_is_rejected_before_first_trash_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            empty = root / "empty.bin"
            empty.write_bytes(b"")

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                plan = DedupPlanner(index).plan(scan.scan_id)
                empty.write_bytes(b"new content")
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )

                with patch("neocortex.workflow.actions.actions.send2trash") as trash:
                    summary = actions.execute(
                        plan,
                        cleanup_empty_directories=False,
                    )

                trash.assert_not_called()
                self.assertEqual(summary.duplicates_trashed, 0)
                self.assertEqual(summary.duplicate_skips, 1)
                self.assertEqual(summary.errors, 1)
                self.assertEqual(empty.read_bytes(), b"new content")

    def test_late_file_blocks_empty_directory_before_first_trash_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            candidate = root / "empty-directory"
            candidate.mkdir(parents=True)

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                original_apply_batch = FrameworkActions._apply_trash_batch
                injected = False

                def inject_late_file(
                    instance: FrameworkActions,
                    action_type: str,
                    batch: tuple[tuple[str, str], ...],
                    *,
                    expected_snapshots: tuple[FileSnapshot | None, ...] | None = None,
                ) -> tuple[int, int, int]:
                    nonlocal injected
                    if action_type == "trash_empty_directory" and not injected:
                        Path(batch[0][0], "late.bin").write_bytes(b"late")
                        injected = True
                    return original_apply_batch(
                        instance,
                        action_type,
                        batch,
                        expected_snapshots=expected_snapshots,
                    )

                with (
                    patch.object(
                        FrameworkActions,
                        "_apply_trash_batch",
                        new=inject_late_file,
                    ),
                    patch("neocortex.workflow.actions.actions.send2trash") as trash,
                ):
                    summary = actions.execute(plan)

                trash.assert_not_called()
                self.assertTrue((candidate / "late.bin").is_file())
                self.assertEqual(summary.empty_directories_trashed, 0)
                self.assertEqual(summary.empty_directory_skips, 1)
                self.assertEqual(summary.errors, 1)

    def test_mutated_keeper_blocks_duplicate_before_first_trash_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            payload = b"duplicate-content" * 1024
            (root / "first.bin").write_bytes(payload)
            (root / "second.bin").write_bytes(payload)

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                original_apply_batch = FrameworkActions._apply_trash_batch
                mutated = False

                def mutate_keeper(
                    instance: FrameworkActions,
                    action_type: str,
                    batch: tuple[tuple[str, str], ...],
                    *,
                    expected_snapshots: tuple[FileSnapshot | None, ...] | None = None,
                    reference_snapshots: tuple[FileSnapshot | None, ...] | None = None,
                ) -> tuple[int, int, int]:
                    nonlocal mutated
                    if action_type == "trash_duplicate" and not mutated:
                        assert reference_snapshots is not None
                        keeper = reference_snapshots[0]
                        assert keeper is not None
                        Path(keeper.path).write_bytes(b"mutated-keeper")
                        mutated = True
                    return original_apply_batch(
                        instance,
                        action_type,
                        batch,
                        expected_snapshots=expected_snapshots,
                        reference_snapshots=reference_snapshots,
                    )

                with (
                    patch.object(
                        FrameworkActions,
                        "_apply_trash_batch",
                        new=mutate_keeper,
                    ),
                    patch("neocortex.workflow.actions.actions.send2trash") as trash,
                ):
                    summary = actions.execute(
                        plan,
                        cleanup_empty_directories=False,
                    )

                trash.assert_not_called()
                self.assertEqual(summary.duplicates_trashed, 0)
                self.assertEqual(summary.duplicate_skips, 1)
                self.assertEqual(summary.errors, 1)
                self.assertTrue((root / "first.bin").exists())
                self.assertTrue((root / "second.bin").exists())

    def test_valid_duplicate_abstains_before_path_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            payload = b"duplicate-content" * 1024
            first = root / "first.bin"
            second = root / "second.bin"
            first.write_bytes(payload)
            second.write_bytes(payload)

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )

                with patch("neocortex.workflow.actions.actions.send2trash") as trash:
                    summary = actions.execute(
                        plan,
                        cleanup_empty_directories=False,
                    )

                trash.assert_not_called()
                self.assertEqual(summary.duplicates_trashed, 0)
                self.assertEqual(summary.duplicate_skips, 1)
                self.assertEqual(summary.errors, 0)
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())

    def test_root_substitution_between_trash_preflight_passes_fails_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            displaced = base / "displaced-corpus"
            root.mkdir()
            payload = b"duplicate-content" * 1024
            first = root / "first.bin"
            second = root / "second.bin"
            first.write_bytes(payload)
            second.write_bytes(payload)

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )

                original_validate = actions._validate_trash_candidate
                validation_count = 0

                def replace_root_after_preflight(*args, **kwargs):
                    nonlocal validation_count
                    result = original_validate(*args, **kwargs)
                    validation_count += 1
                    if validation_count != 1:
                        return result
                    os.replace(root, displaced)
                    root.mkdir()
                    for source in displaced.iterdir():
                        os.replace(source, root / source.name)
                    return result

                with (
                    patch.object(
                        actions,
                        "_validate_trash_candidate",
                        side_effect=replace_root_after_preflight,
                    ),
                    patch("neocortex.workflow.actions.actions.send2trash") as trash,
                    self.assertRaisesRegex(
                        ProtectedAnalysisRootError,
                        "root identity changed",
                    ),
                ):
                    actions.execute(
                        plan,
                        cleanup_empty_directories=False,
                    )

                trash.assert_not_called()
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())

    def test_file_changed_during_exact_compare_fails_only_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            payload = b"duplicate-content" * 1024
            first = root / "first.bin"
            second = root / "second.bin"
            first.write_bytes(payload)
            second.write_bytes(payload)

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )

                with (
                    patch(
                        "neocortex.workflow.actions.actions.files_equal_exact",
                        side_effect=FileChangedError("changed while reading"),
                    ),
                    patch("neocortex.workflow.actions.actions.send2trash") as trash,
                ):
                    summary = actions.execute(
                        plan,
                        cleanup_empty_directories=False,
                    )

                trash.assert_not_called()
                self.assertEqual(summary.duplicates_trashed, 0)
                self.assertEqual(summary.duplicate_skips, 1)
                self.assertEqual(summary.errors, 1)
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())

    def test_outside_candidate_fails_without_contaminating_safe_batch_item(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            safe = root / "safe.bin"
            outside = base / "outside.bin"
            safe.write_bytes(b"safe")
            outside.write_bytes(b"outside")

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )

                with patch("neocortex.workflow.actions.actions.send2trash") as trash:
                    result = actions._apply_trash_batch(
                        "trash_test_candidate",
                        (
                            (str(safe), "safe fixture"),
                            (str(outside), "outside fixture"),
                        ),
                        expected_snapshots=(
                            snapshot_path(safe),
                            snapshot_path(outside),
                        ),
                    )

                self.assertEqual(result, (0, 1, 1))
                trash.assert_not_called()
                self.assertTrue(safe.exists())
                self.assertTrue(outside.exists())
                detail = state._connection.execute(
                    "SELECT detail FROM file_actions WHERE source_path=?",
                    (str(outside),),
                ).fetchone()[0]
                self.assertIn("escapes the inventory root lexically", detail)

    def test_simulated_intermediate_reparse_is_rejected_before_trash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            parent = root / "incoming"
            parent.mkdir(parents=True)
            candidate = parent / "candidate.bin"
            candidate.write_bytes(b"content")
            parent_key = os.path.normcase(os.path.abspath(parent))
            real_check = action_policy._is_reparse_entry

            def simulated_reparse(path: Path, entry_stat: os.stat_result) -> bool:
                return os.path.normcase(os.path.abspath(path)) == parent_key or real_check(
                    path, entry_stat
                )

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                snapshot = snapshot_path(candidate)
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                with (
                    patch.object(
                        action_policy,
                        "_is_reparse_entry",
                        side_effect=simulated_reparse,
                    ),
                    patch("neocortex.workflow.actions.actions.send2trash") as trash,
                ):
                    result = actions.recycle_verified_files(
                        "trash_test_reparse",
                        ((snapshot, "reparse fixture"),),
                    )

            self.assertEqual(result, (0, 1, 0))
            trash.assert_not_called()
            self.assertTrue(candidate.exists())

    def test_component_substitution_after_preflight_isolated_from_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            safe_parent = root / "safe"
            replaced_parent = root / "replaced"
            safe_parent.mkdir(parents=True)
            replaced_parent.mkdir()
            safe = safe_parent / "safe.bin"
            replaced = replaced_parent / "replaced.bin"
            safe.write_bytes(b"safe")
            replaced.write_bytes(b"replaced")
            replaced_parent_key = os.path.normcase(os.path.abspath(replaced_parent))
            real_check = action_policy._is_reparse_entry
            replacement_active = False

            def simulated_reparse(path: Path, entry_stat: os.stat_result) -> bool:
                return (
                    replacement_active
                    and os.path.normcase(os.path.abspath(path)) == replaced_parent_key
                ) or real_check(path, entry_stat)

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                original_validate = actions._validate_trash_candidate
                validation_count = 0

                def substitute_after_first_pass(*args, **kwargs):
                    nonlocal replacement_active, validation_count
                    result = original_validate(*args, **kwargs)
                    validation_count += 1
                    if validation_count == 2:
                        replacement_active = True
                    return result

                with (
                    patch.object(
                        action_policy,
                        "_is_reparse_entry",
                        side_effect=simulated_reparse,
                    ),
                    patch.object(
                        actions,
                        "_validate_trash_candidate",
                        side_effect=substitute_after_first_pass,
                    ),
                    patch("neocortex.workflow.actions.actions.send2trash") as trash,
                ):
                    result = actions._apply_trash_batch(
                        "trash_test_substitution",
                        (
                            (str(safe), "safe fixture"),
                            (str(replaced), "replaced fixture"),
                        ),
                        expected_snapshots=(
                            snapshot_path(safe),
                            snapshot_path(replaced),
                        ),
                    )

            self.assertEqual(result, (0, 1, 1))
            trash.assert_not_called()
            self.assertTrue(safe.exists())
            self.assertTrue(replaced.exists())

    def test_outside_keeper_blocks_duplicate_recycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            candidate = root / "candidate.bin"
            keeper = base / "outside-keeper.bin"
            candidate.write_bytes(b"duplicate")
            keeper.write_bytes(b"duplicate")

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                with patch("neocortex.workflow.actions.actions.send2trash") as trash:
                    result = actions._apply_trash_batch(
                        "trash_duplicate",
                        ((str(candidate), "outside keeper fixture"),),
                        expected_snapshots=(snapshot_path(candidate),),
                        reference_snapshots=(snapshot_path(keeper),),
                    )

            self.assertEqual(result, (0, 1, 0))
            trash.assert_not_called()
            self.assertTrue(candidate.exists())
            self.assertTrue(keeper.exists())

    def test_reparse_keeper_component_blocks_duplicate_recycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            keeper_parent = root / "keepers"
            keeper_parent.mkdir(parents=True)
            candidate = root / "candidate.bin"
            keeper = keeper_parent / "keeper.bin"
            candidate.write_bytes(b"duplicate")
            keeper.write_bytes(b"duplicate")
            keeper_parent_key = os.path.normcase(os.path.abspath(keeper_parent))
            real_check = action_policy._is_reparse_entry

            def simulated_reparse(path: Path, entry_stat: os.stat_result) -> bool:
                return os.path.normcase(os.path.abspath(path)) == keeper_parent_key or real_check(
                    path, entry_stat
                )

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                with (
                    patch.object(
                        action_policy,
                        "_is_reparse_entry",
                        side_effect=simulated_reparse,
                    ),
                    patch("neocortex.workflow.actions.actions.send2trash") as trash,
                ):
                    result = actions._apply_trash_batch(
                        "trash_duplicate",
                        ((str(candidate), "reparse keeper fixture"),),
                        expected_snapshots=(snapshot_path(candidate),),
                        reference_snapshots=(snapshot_path(keeper),),
                    )

            self.assertEqual(result, (0, 1, 0))
            trash.assert_not_called()
            self.assertTrue(candidate.exists())

    def test_unsafe_rename_target_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            parent = root / "incoming"
            parent.mkdir(parents=True)
            source = parent / "image.txt"
            source.write_bytes(b"image payload")
            planned = snapshot_path(source)
            parent_key = os.path.normcase(os.path.abspath(parent))
            real_check = action_policy._is_reparse_entry
            detected = DetectedType(
                "image/png",
                ".png",
                frozenset({".png"}),
                "fixture",
            )

            def simulated_reparse(path: Path, entry_stat: os.stat_result) -> bool:
                return os.path.normcase(os.path.abspath(path)) == parent_key or real_check(
                    path, entry_stat
                )

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                with patch.object(
                    action_policy,
                    "_is_reparse_entry",
                    side_effect=simulated_reparse,
                ):
                    summary = actions._rename_mismatch(
                        planned,
                        detected,
                        ActionSummary(apply_actions=True),
                    )

            self.assertEqual(summary.files_renamed, 0)
            self.assertEqual(summary.rename_skips, 1)
            self.assertEqual(summary.errors, 1)
            self.assertTrue(source.exists())
            self.assertFalse(source.with_suffix(".png").exists())

    @unittest.skipUnless(os.name == "nt", "native mutation backend is Windows-only")
    def test_normal_in_root_rename_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            source = root / "image.txt"
            source.write_bytes(b"image payload")
            planned = snapshot_path(source)
            detected = DetectedType(
                "image/png",
                ".png",
                frozenset({".png"}),
                "fixture",
            )

            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                summary = actions._rename_mismatch(
                    planned,
                    detected,
                    ActionSummary(apply_actions=True),
                )

            self.assertEqual(summary.files_renamed, 1)
            self.assertEqual(summary.errors, 0)
            self.assertFalse(source.exists())
            self.assertTrue(source.with_suffix(".png").exists())


# endregion [02]


# region [03] Planner generation regressions


class PlannerSnapshotSafetyTests(unittest.TestCase):
    def test_replacement_with_preserved_size_and_mtime_is_not_planned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            left = root / "left.bin"
            right = root / "right.bin"
            payload = b"same-content" * 1024
            left.write_bytes(payload)
            right.write_bytes(payload)

            with DedupIndex(base / "state.sqlite3") as index:
                scan = index.scan(root, excluded_paths=())
                recorded = {
                    Path(snapshot.path).name: snapshot for snapshot in index.snapshots(scan.scan_id)
                }[left.name]

                replacement = root / "replacement.bin"
                replacement.write_bytes(payload)
                os.utime(
                    replacement,
                    ns=(recorded.mtime_ns, recorded.mtime_ns),
                )
                os.replace(replacement, left)
                current = snapshot_path(left)
                if current.identity == recorded.identity:
                    self.skipTest("filesystem reused the replaced file identity")
                self.assertEqual(current.size, recorded.size)
                self.assertEqual(current.mtime_ns, recorded.mtime_ns)
                self.assertEqual(
                    tuple(index.size_collision_groups(scan.scan_id)),
                    (),
                )

                plan = DedupPlanner(index).plan(
                    scan.scan_id,
                    preview_limit=None,
                    exact_compare=True,
                )

            self.assertEqual(plan.group_count, 0)
            self.assertEqual(plan.statistics.changed_or_unreadable_files, 1)


# endregion [03]


# region [04] Birth-time mutation regressions


class BirthTimeSnapshotSafetyTests(unittest.TestCase):
    def test_action_snapshot_comparison_rejects_changed_birth_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "item.bin"
            path.write_bytes(b"content")
            snapshot = snapshot_path(path)

            changed = replace(snapshot, birthtime_ns=snapshot.birthtime_ns - 1)

            self.assertFalse(same_snapshot(snapshot, changed))
            self.assertFalse(image_same_snapshot(snapshot, changed))

    def test_hashing_rejects_changed_birth_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "item.bin"
            path.write_bytes(b"content")
            snapshot = snapshot_path(path)
            stale = replace(snapshot, birthtime_ns=snapshot.birthtime_ns - 1)

            self.assertFalse(_source_matches(stale))
            with self.assertRaises(FileChangedError):
                full_fingerprint(stale)

    def test_runtime_stat_rejects_changed_birth_time(self) -> None:
        attributes = {
            "st_dev": 1,
            "st_ino": 2,
            "st_mode": 3,
            "st_size": 4,
            "st_mtime_ns": 5,
            "st_ctime_ns": 6,
        }
        original = cast(
            os.stat_result,
            SimpleNamespace(**attributes, st_birthtime_ns=10),
        )
        current = cast(
            os.stat_result,
            SimpleNamespace(**attributes, st_birthtime_ns=11),
        )

        self.assertFalse(FrameworkActions._same_runtime_stat(original, current))


# endregion [04]


# region [05] Fingerprint cache migration regressions


class FingerprintCacheSafetyTests(unittest.TestCase):
    def test_legacy_fingerprint_never_matches_a_new_birth_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    ) WITHOUT ROWID;
                    INSERT INTO metadata VALUES('schema_version', '4');
                    CREATE TABLE fingerprints(
                        volume_id BLOB NOT NULL,
                        file_id BLOB NOT NULL,
                        size INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        algorithm TEXT NOT NULL,
                        digest BLOB NOT NULL,
                        PRIMARY KEY(volume_id,file_id,size,mtime_ns,algorithm)
                    ) WITHOUT ROWID;
                    """
                )
                connection.execute(
                    "INSERT INTO fingerprints VALUES(?,?,?,?,?,?)",
                    (
                        (7).to_bytes(16, "little"),
                        (11).to_bytes(16, "little"),
                        100,
                        200,
                        "test-v1",
                        b"legacy",
                    ),
                )
                connection.commit()

            snapshot = FileSnapshot("unused.bin", 7, 11, 100, 200, 300)
            with DedupIndex(database) as index:
                self.assertIsNone(index.cached_fingerprint(snapshot, "test-v1"))
                with closing(sqlite3.connect(database)) as connection:
                    version = connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()[0]
                    legacy_birthtime = connection.execute(
                        "SELECT birthtime_ns FROM fingerprints"
                    ).fetchone()[0]
                self.assertEqual(version, "10")
                self.assertEqual(legacy_birthtime, -1)

                index.store_fingerprint(snapshot, "test-v1", b"refreshed")
                self.assertEqual(
                    index.cached_fingerprint(snapshot, "test-v1"),
                    b"refreshed",
                )

            with closing(sqlite3.connect(database)) as connection:
                refreshed_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM fingerprints"
                ).fetchone()[0]
            self.assertEqual(refreshed_birthtime, snapshot.birthtime_ns)


# endregion [05]


# region [06] Content-type cache migration regressions


class ContentTypeCacheSafetyTests(unittest.TestCase):
    def test_legacy_detection_never_matches_a_new_birth_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "framework.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    ) WITHOUT ROWID;
                    INSERT INTO metadata VALUES('schema_version', '10');
                    CREATE TABLE content_type_cache(
                        volume_id TEXT NOT NULL,
                        file_id TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        detector_version TEXT NOT NULL,
                        status TEXT NOT NULL,
                        mime TEXT,
                        canonical_extension TEXT,
                        accepted_extensions_json TEXT,
                        evidence TEXT,
                        last_seen_run_id INTEGER NOT NULL DEFAULT 0,
                        updated_ns INTEGER NOT NULL,
                        PRIMARY KEY(volume_id,file_id,detector_version)
                    ) WITHOUT ROWID;
                    INSERT INTO content_type_cache VALUES(
                        '7','b',100,200,'detector-v1','unknown',
                        NULL,NULL,NULL,NULL,1,1
                    );
                    """
                )
                connection.commit()

            snapshot = FileSnapshot("unused.bin", 7, 11, 100, 200, 300)
            detected = DetectedType(
                "application/pdf",
                ".pdf",
                frozenset({".pdf"}),
                "test-evidence",
            )
            with FrameworkState(database) as state:
                self.assertEqual(
                    state.get_content_type_cache(snapshot, "detector-v1"),
                    (False, None),
                )
                with closing(sqlite3.connect(database)) as connection:
                    version = connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()[0]
                    legacy_birthtime = connection.execute(
                        "SELECT birthtime_ns FROM content_type_cache"
                    ).fetchone()[0]
                self.assertEqual(version, str(SCHEMA_VERSION))
                self.assertEqual(legacy_birthtime, -1)

                state.store_content_type_cache(
                    snapshot,
                    "detector-v1",
                    detected,
                    run_id=2,
                )
                self.assertEqual(
                    state.get_content_type_cache(snapshot, "detector-v1"),
                    (True, detected),
                )

            with closing(sqlite3.connect(database)) as connection:
                refreshed_birthtime = connection.execute(
                    "SELECT birthtime_ns FROM content_type_cache"
                ).fetchone()[0]
            self.assertEqual(refreshed_birthtime, snapshot.birthtime_ns)


# endregion [06]


# region [07] Explicit review-candidate recycling


class VerifiedRecycleSafetyTests(unittest.TestCase):
    def test_changed_candidate_is_preserved_before_recycle_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            candidate = root / "broken.pdf"
            candidate.write_bytes(b"%PDF-1.4\noriginal")
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                snapshot = next(index.snapshots(scan.scan_id))
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )
                candidate.write_bytes(b"%PDF-1.4\nchanged")
                with patch("neocortex.workflow.actions.actions.send2trash") as trash:
                    applied, failed, protected = actions.recycle_verified_files(
                        "trash_unrecoverable_pdf",
                        ((snapshot, "all PDF engines failed"),),
                    )
            trash.assert_not_called()
            self.assertEqual((applied, failed, protected), (0, 1, 0))
            self.assertTrue(candidate.is_file())

    def test_verified_recycle_abstains_and_preserves_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            root.mkdir()
            candidate = root / "broken.pdf"
            candidate.write_bytes(b"%PDF-1.4\nunrecoverable")
            framework_database = _framework_database(base)
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(framework_database) as state,
            ):
                scan = index.scan(root, excluded_paths=())
                snapshot = next(index.snapshots(scan.scan_id))
                run_id = begin_signed_normal_run(state, root)
                actions = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=True,
                    excluded_paths=(),
                )

                with patch("neocortex.workflow.actions.actions.send2trash") as trash:
                    result = actions.recycle_verified_files(
                        "trash_unrecoverable_pdf",
                        ((snapshot, "all PDF engines failed"),),
                    )
                remaining = tuple(index.snapshots(scan.scan_id))
            trash.assert_not_called()
            self.assertEqual(result, (0, 0, 1))
            self.assertTrue(candidate.exists())
            self.assertEqual(tuple(snapshot.path for snapshot in remaining), (str(candidate),))
            with closing(sqlite3.connect(framework_database)) as connection:
                action = connection.execute(
                    "SELECT action_type,status,detail FROM file_actions"
                ).fetchone()
            self.assertEqual(action[:2], ("trash_unrecoverable_pdf", "skipped"))
            self.assertIn("cannot bind the observed file identity", action[2])


# endregion [07]


if __name__ == "__main__":
    unittest.main()
