# region [00] Contexto del módulo
# Módulo: tests/test_framework_actions.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryExclusionPolicy
from neocortex.deduplication.io import native_io_path
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.platform.content_types import detect_content_type
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run
# endregion [01]

# region [02] Implementación


def _framework_database(base: Path) -> Path:
    state_directory = base / "state"
    state_directory.mkdir(exist_ok=True)
    return state_directory / "framework.sqlite3"


class ContentTypeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows extended paths are required")
    def test_detects_signature_through_extended_length_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent = base
            while len(os.fspath(parent / "image.png")) < 280:
                parent /= "long-path-segment-0123456789"
            path = parent / "image.png"
            extended_parent = Path("\\\\?\\" + os.path.abspath(parent))
            extended_path = Path("\\\\?\\" + os.path.abspath(path))
            extended_base = Path("\\\\?\\" + os.path.abspath(base))
            extended_parent.mkdir(parents=True)
            extended_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
            try:
                self.assertTrue(native_io_path(path).startswith("\\\\?\\"))
                detected = detect_content_type(path)

                self.assertIsNotNone(detected)
                assert detected is not None
                self.assertEqual(detected.mime, "image/png")
            finally:
                extended_path.unlink(missing_ok=True)
                current = extended_parent
                while current != extended_base:
                    current.rmdir()
                    current = current.parent

    def test_detects_signature_instead_of_claimed_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.txt"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
            detected = detect_content_type(path)
            self.assertIsNotNone(detected)
            assert detected is not None
            self.assertEqual(detected.mime, "image/png")
            self.assertEqual(detected.canonical_extension, ".png")
            self.assertFalse(detected.accepts(path))

    def test_distinguishes_ooxml_zip_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types />")
                archive.writestr("word/document.xml", "<document />")
            detected = detect_content_type(path)
            self.assertIsNotNone(detected)
            assert detected is not None
            self.assertEqual(detected.canonical_extension, ".docx")

    def test_detects_bounded_printable_text_without_trusting_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = {
                "notes.md": (b"# Ajustes\nProteccion diferencial", "text/markdown"),
                "table.tsv": (b"equipo\tvalor\ninterruptor\t42", "text/tab-separated-values"),
                "settings.local": (b"modo=seguro\numbral=proteccion", "text/plain"),
                "sin-extension": (b"bitacora del transformador", "text/plain"),
            }
            for name, (payload, expected_mime) in paths.items():
                with self.subTest(name=name):
                    path = Path(directory) / name
                    path.write_bytes(payload)
                    detected = detect_content_type(path)
                    self.assertIsNotNone(detected)
                    assert detected is not None
                    self.assertEqual(detected.mime, expected_mime)
                    self.assertTrue(detected.accepts(path))

    def test_detects_email_only_with_structural_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mensaje.eml"
            path.write_bytes(
                b"From: operation@example.test\r\n"
                b"To: victor@example.test\r\n"
                b"Subject: Proteccion de alimentador\r\n\r\n"
                b"Resultado satisfactorio"
            )
            detected = detect_content_type(path)
            self.assertIsNotNone(detected)
            assert detected is not None
            self.assertEqual(detected.mime, "message/rfc822")

    def test_detects_legacy_office_only_with_cfb_signature_and_known_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            signature = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504
            expectations = {
                "legacy.doc": "application/msword",
                "legacy.xls": "application/vnd.ms-excel",
                "legacy.ppt": "application/vnd.ms-powerpoint",
            }
            for name, expected_mime in expectations.items():
                with self.subTest(name=name):
                    path = Path(directory) / name
                    path.write_bytes(signature)
                    detected = detect_content_type(path)
                    self.assertIsNotNone(detected)
                    assert detected is not None
                    self.assertEqual(detected.mime, expected_mime)

            unknown = Path(directory) / "legacy.bin"
            unknown.write_bytes(signature)
            self.assertIsNone(detect_content_type(unknown))

    def test_rejects_binary_bytes_even_when_named_as_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.txt"
            path.write_bytes(bytes(range(256)) * 10)
            self.assertIsNone(detect_content_type(path))


class ActionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows file attributes are required")
    def test_hidden_system_file_is_protected_from_content_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            marker = corpus / ".onedrive-internal-marker"
            marker.write_bytes(b"internal")
            os.system(f'attrib +H +S "{marker}"')
            try:
                with (
                    DedupIndex(base / "dedup.sqlite3") as index,
                    FrameworkState(_framework_database(base)) as state,
                ):
                    scan = index.scan(corpus)
                    plan = DedupPlanner(index).plan(scan.scan_id)
                    run_id = begin_signed_normal_run(state, corpus)
                    summary = FrameworkActions(
                        index, state, run_id, scan.scan_id, apply=False
                    ).execute(plan)
                    failures = state._connection.execute(
                        "SELECT COUNT(*) FROM file_actions WHERE run_id=? AND status='failed'",
                        (run_id,),
                    ).fetchone()[0]
                self.assertEqual(summary.errors, 0)
                self.assertEqual(failures, 0)
            finally:
                os.system(f'attrib -H -S "{marker}"')

    def test_valid_trash_candidates_abstain_before_path_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            paths = [corpus / f"duplicate-{number}.bin" for number in range(3)]
            for path in paths:
                path.write_bytes(b"same")
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, corpus)
                with patch("neocortex.workflow.actions.actions.send2trash") as trash:
                    summary = FrameworkActions(
                        index,
                        state,
                        run_id,
                        scan.scan_id,
                        apply=True,
                        verify_bytes_before_trash=False,
                    ).execute(plan)
                statuses = state._connection.execute(
                    """SELECT source_path,status,detail FROM file_actions
                    WHERE run_id=? AND action_type='trash_duplicate'
                    ORDER BY source_path""",
                    (run_id,),
                ).fetchall()
            trash.assert_not_called()
            self.assertEqual(summary.duplicates_trashed, 0)
            self.assertEqual(summary.duplicate_skips, 2)
            self.assertEqual(summary.errors, 0)
            self.assertEqual(
                {row[1] for row in statuses},
                {"skipped"},
            )
            details = " ".join(str(row[2]).lower() for row in statuses)
            self.assertIn("cannot bind the observed file identity", details)
            self.assertTrue(all(path.exists() for path in paths))

    def test_disappearing_inventory_is_stale_not_an_action_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            disappearing = corpus / "disappearing.png"
            disappearing.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, corpus)
                disappearing.unlink()
                summary = FrameworkActions(index, state, run_id, scan.scan_id, apply=False).execute(
                    plan
                )
                failed = state._connection.execute(
                    "SELECT COUNT(*) FROM file_actions WHERE run_id=? AND status='failed'",
                    (run_id,),
                ).fetchone()[0]
            self.assertEqual(summary.stale_inventory, 1)
            self.assertEqual(summary.errors, 0)
            self.assertEqual(failed, 0)

    def test_reuses_content_type_cache_for_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            (corpus / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
            (corpus / "unknown.bin").write_bytes(b"no recognized signature")
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index).plan(scan.scan_id)
                first_run = begin_signed_normal_run(state, corpus)
                first = FrameworkActions(
                    index, state, first_run, scan.scan_id, apply=False
                ).execute(plan)
                second_run = begin_signed_normal_run(state, corpus)
                second = FrameworkActions(
                    index, state, second_run, scan.scan_id, apply=False
                ).execute(plan)
            self.assertEqual(first.type_cache_hits, 0)
            self.assertEqual(first.type_cache_misses, 2)
            self.assertEqual(second.type_cache_hits, 2)
            self.assertEqual(second.type_cache_misses, 0)

            (corpus / "unknown.bin").unlink()
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index).plan(scan.scan_id)
                third_run = begin_signed_normal_run(state, corpus)
                third = FrameworkActions(
                    index, state, third_run, scan.scan_id, apply=False
                ).execute(plan)
                cache_rows = state._connection.execute(
                    "SELECT COUNT(*) FROM content_type_cache"
                ).fetchone()[0]
            self.assertEqual(third.type_cache_hits, 1)
            self.assertEqual(third.type_cache_pruned, 1)
            self.assertEqual(cache_rows, 1)

    def test_empty_directory_tree_abstains_and_preserves_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            leaf = corpus / "empty-a" / "empty-b" / "empty-c"
            leaf.mkdir(parents=True)
            keep_directory = corpus / "keep"
            keep_directory.mkdir()
            (keep_directory / "file.bin").write_bytes(b"content")
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False, preview_limit=0)
                run_id = begin_signed_normal_run(state, corpus)

                with patch("neocortex.workflow.actions.actions.send2trash") as recycle:
                    summary = FrameworkActions(
                        index,
                        state,
                        run_id,
                        scan.scan_id,
                        apply=True,
                        verify_bytes_before_trash=False,
                    ).execute(plan)
            self.assertEqual(summary.empty_directory_candidates, 1)
            self.assertEqual(summary.empty_directories_trashed, 0)
            self.assertEqual(summary.empty_directory_skips, 1)
            recycle.assert_not_called()
            self.assertTrue(corpus.exists())
            self.assertTrue(leaf.exists())
            self.assertTrue((keep_directory / "file.bin").exists())

    def test_empty_directory_cleanup_honors_the_inventory_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            excluded = corpus / "Workspace" / "AppData"
            excluded_leaf = excluded / "empty-child"
            eligible = corpus / "eligible-empty"
            excluded_leaf.mkdir(parents=True)
            eligible.mkdir(parents=True)
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus, excluded_paths=(excluded,))
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, corpus)

                with patch("neocortex.workflow.actions.actions.send2trash") as recycle:
                    summary = FrameworkActions(
                        index,
                        state,
                        run_id,
                        scan.scan_id,
                        apply=True,
                        excluded_paths=(excluded,),
                    ).execute(plan)
            recycle.assert_not_called()
            self.assertEqual(summary.empty_directory_candidates, 1)
            self.assertEqual(summary.empty_directories_trashed, 0)
            self.assertEqual(summary.empty_directory_skips, 1)
            self.assertTrue(eligible.exists())
            self.assertTrue(excluded_leaf.exists())

    def test_empty_directory_cleanup_reuses_named_inventory_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            excluded_leaf = corpus / "node_modules" / "empty-child"
            eligible = corpus / "eligible-empty"
            excluded_leaf.mkdir(parents=True)
            eligible.mkdir(parents=True)
            policy = InventoryExclusionPolicy.compile(directory_names=("node_modules",))
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus, exclusion_policy=policy)
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, corpus)
                summary = FrameworkActions(
                    index,
                    state,
                    run_id,
                    scan.scan_id,
                    apply=False,
                    exclusion_policy=policy,
                ).execute(plan)

            self.assertEqual(summary.errors, 0)
            self.assertEqual(summary.empty_directory_candidates, 1)
            self.assertTrue(eligible.exists())
            self.assertTrue(excluded_leaf.exists())

    def test_empty_files_without_hash_group_abstain_from_path_trash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            empty_paths = [corpus / f"empty-{index}" for index in range(3)]
            for path in empty_paths:
                path.touch()
            survivor = corpus / "nonempty.bin"
            survivor.write_bytes(b"content")
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False, preview_limit=0)
                run_id = begin_signed_normal_run(state, corpus)

                with patch("neocortex.workflow.actions.actions.send2trash") as recycle:
                    summary = FrameworkActions(
                        index,
                        state,
                        run_id,
                        scan.scan_id,
                        apply=True,
                        verify_bytes_before_trash=False,
                    ).execute(plan)
            self.assertEqual(summary.duplicate_candidates, 3)
            self.assertEqual(summary.duplicates_trashed, 0)
            self.assertEqual(summary.duplicate_skips, 3)
            self.assertEqual(summary.errors, 0)
            recycle.assert_not_called()
            self.assertTrue(survivor.exists())
            self.assertTrue(all(path.exists() for path in empty_paths))

    def test_large_group_abstains_in_bounded_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            for number in range(258):
                (corpus / f"duplicate-{number:03d}.bin").write_bytes(b"same")
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False, preview_limit=0)
                run_id = begin_signed_normal_run(state, corpus)
                load_guard = state.corpus_mutation_guard
                guard_rebuilds = 0

                def counted_guard(current_run_id: int):
                    nonlocal guard_rebuilds
                    guard_rebuilds += 1
                    return load_guard(current_run_id)

                with (
                    patch("neocortex.workflow.actions.actions.send2trash") as recycle,
                    patch.object(
                        state,
                        "corpus_mutation_guard",
                        side_effect=counted_guard,
                    ),
                ):
                    summary = FrameworkActions(
                        index,
                        state,
                        run_id,
                        scan.scan_id,
                        apply=True,
                        verify_bytes_before_trash=False,
                    ).execute(plan)
            self.assertEqual(summary.duplicates_trashed, 0)
            self.assertEqual(summary.duplicate_skips, 257)
            self.assertEqual(summary.errors, 0)
            recycle.assert_not_called()
            self.assertLessEqual(guard_rebuilds, 6)
            self.assertEqual(len(list(corpus.iterdir())), 258)

    @unittest.skipUnless(os.name == "nt", "identity-bound rename is Windows-only")
    def test_abstains_exact_trash_and_applies_safe_extension_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            older = corpus / "older.bin"
            newer = corpus / "newer.bin"
            disguised = corpus / "photo.txt"
            older.write_bytes(b"duplicate")
            newer.write_bytes(b"duplicate")
            disguised.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
            os.utime(older, ns=(1_700_000_000_000_000_000,) * 2)
            os.utime(newer, ns=(1_710_000_000_000_000_000,) * 2)

            framework_database = _framework_database(base)
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(framework_database) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index, partial_threshold=0).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, corpus)

                with patch("neocortex.workflow.actions.actions.send2trash") as recycle:
                    summary = FrameworkActions(
                        index,
                        state,
                        run_id,
                        scan.scan_id,
                        apply=True,
                        verify_bytes_before_trash=False,
                    ).execute(plan)

                recycle.assert_not_called()
                self.assertEqual(summary.duplicates_trashed, 0)
                self.assertEqual(summary.duplicate_skips, 1)
                self.assertEqual(summary.errors, 0)
                self.assertEqual(summary.files_renamed, 1)
                self.assertTrue(older.exists())
                self.assertTrue(newer.exists())
                self.assertFalse(disguised.exists())
                self.assertTrue((corpus / "photo.png").exists())

            connection = sqlite3.connect(framework_database)
            statuses = connection.execute(
                "SELECT action_type, status FROM file_actions ORDER BY action_id"
            ).fetchall()
            connection.close()
            self.assertEqual(
                statuses,
                [("trash_duplicate", "skipped"), ("correct_extension", "applied")],
            )

    def test_never_plans_or_applies_windows_profile_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            first = corpus / "NTUSER.DAT{fixture}.TxR.1.regtrans-ms"
            second = corpus / "NTUSER.DAT{fixture}.TxR.2.regtrans-ms"
            first.write_bytes(b"profile-state")
            second.write_bytes(b"profile-state")
            framework_database = _framework_database(base)
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(framework_database) as state,
            ):
                scan = index.scan(corpus, excluded_paths=())
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, corpus)
                with patch("neocortex.workflow.actions.actions.send2trash") as recycle:
                    summary = FrameworkActions(
                        index,
                        state,
                        run_id,
                        scan.scan_id,
                        apply=True,
                    ).execute(plan)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(summary.duplicate_candidates, 1)
            self.assertEqual(summary.duplicate_skips, 1)
            self.assertEqual(summary.errors, 0)
            recycle.assert_not_called()
            with closing(sqlite3.connect(framework_database)) as connection:
                status, detail = connection.execute(
                    "SELECT status,detail FROM file_actions WHERE action_type='trash_duplicate'"
                ).fetchone()
            self.assertEqual(status, "skipped")
            self.assertIn("protected Windows user-profile state", detail)

    def test_does_not_overwrite_existing_extension_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            corpus.mkdir()
            source = corpus / "photo.txt"
            target = corpus / "photo.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\nsource")
            target.write_bytes(b"\x89PNG\r\n\x1a\ntarget")
            with (
                DedupIndex(base / "dedup.sqlite3") as index,
                FrameworkState(_framework_database(base)) as state,
            ):
                scan = index.scan(corpus)
                plan = DedupPlanner(index).plan(scan.scan_id)
                run_id = begin_signed_normal_run(state, corpus)
                summary = FrameworkActions(index, state, run_id, scan.scan_id, apply=True).execute(
                    plan
                )
            self.assertEqual(summary.files_renamed, 0)
            self.assertEqual(summary.rename_skips, 1)
            self.assertTrue(source.exists())
            self.assertEqual(target.read_bytes(), b"\x89PNG\r\n\x1a\ntarget")


if __name__ == "__main__":
    unittest.main()
# endregion [02]
