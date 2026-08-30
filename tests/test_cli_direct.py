from __future__ import annotations

import argparse
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from neocortex.api.cli.cli_direct import (
    run_organization_apply,
    run_organization_plan,
    run_pdf_layout_groups,
    run_pdf_search,
)
from neocortex.safety.corpus_access import CorpusMutationGuard
from neocortex.capabilities.formats.pdf.pdf_derived_queries import (
    MAX_LAYOUT_MEMBERS_PER_GROUP,
    list_layout_groups,
    search_pdf_state,
)
from neocortex.safety.protected_content import (
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from tests.internal_paths_test_support import disjoint_internal_paths_policy


# region [01] Query-state fixtures


def _create_query_state(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE VIRTUAL TABLE page_fts USING fts5(
                file_key UNINDEXED,
                path UNINDEXED,
                page_number UNINDEXED,
                text
            );
            CREATE TABLE similarity_state(
                signature_kind TEXT PRIMARY KEY,
                relation_run_id INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE layout_groups(
                relation_run_id INTEGER NOT NULL,
                group_key TEXT NOT NULL,
                representative_file_key TEXT NOT NULL,
                member_count INTEGER NOT NULL,
                minimum_edge_score REAL NOT NULL,
                evidence_json TEXT NOT NULL,
                PRIMARY KEY(relation_run_id,group_key)
            ) WITHOUT ROWID;
            CREATE TABLE layout_group_members(
                relation_run_id INTEGER NOT NULL,
                group_key TEXT NOT NULL,
                file_key TEXT NOT NULL,
                PRIMARY KEY(relation_run_id,group_key,file_key)
            ) WITHOUT ROWID;
            INSERT INTO similarity_state VALUES('layout',7);
            INSERT INTO layout_groups VALUES(7,'group-a','member-00',25,0.875,
                '{"algorithm":"test"}');
            INSERT INTO layout_groups VALUES(7,'group-b','group-b-00',2,0.950,
                '{"algorithm":"test"}');
            INSERT INTO documents VALUES('search-key','search.pdf','done');
            INSERT INTO page_fts VALUES('search-key','search.pdf',0,
                'transformador de potencia');
            """
        )
        documents = [
            (f"member-{index:02d}", f"member-{index:02d}.pdf", "done")
            for index in range(24, -1, -1)
        ]
        connection.executemany("INSERT INTO documents VALUES(?,?,?)", documents)
        connection.executemany(
            "INSERT INTO layout_group_members VALUES(7,'group-a',?)",
            ((file_key,) for file_key, _path, _status in documents),
        )
        connection.executemany(
            "INSERT INTO documents VALUES(?,?,?)",
            (
                ("group-b-00", "group-b-00.pdf", "done"),
                ("group-b-01", "group-b-01.pdf", "done"),
            ),
        )
        connection.executemany(
            "INSERT INTO layout_group_members VALUES(7,'group-b',?)",
            (("group-b-00",), ("group-b-01",)),
        )
        connection.commit()


# endregion [01]


# region [02] Read-only PDF query behavior


class PdfReadOnlyQueryTests(unittest.TestCase):
    def test_existing_queries_are_bounded_and_leave_database_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "pdf.sqlite3"
            _create_query_state(database)
            before = database.read_bytes()

            search_results = search_pdf_state(database, "transformador", 5)
            groups = list_layout_groups(database, 2)

            self.assertEqual(len(search_results), 1)
            self.assertEqual(search_results[0]["path"], "search.pdf")
            self.assertEqual(len(groups), 2)
            self.assertEqual(
                groups[0]["members"],
                [
                    f"member-{index:02d}.pdf"
                    for index in range(MAX_LAYOUT_MEMBERS_PER_GROUP)
                ],
            )
            self.assertEqual(groups[0]["representative_path"], "member-00.pdf")
            self.assertTrue(groups[0]["members_truncated"])
            self.assertEqual(groups[0]["evidence"], {"algorithm": "test"})
            self.assertEqual(groups[1]["members"], ["group-b-00.pdf", "group-b-01.pdf"])
            self.assertFalse(groups[1]["members_truncated"])
            self.assertEqual(database.read_bytes(), before)

    def test_missing_database_returns_two_without_creating_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary) / "state"
            database = state_directory / "pdf.sqlite3"
            search_args = argparse.Namespace(
                state_directory=state_directory,
                pdf_search="transformador",
                pdf_search_limit=5,
            )
            layout_args = argparse.Namespace(
                state_directory=state_directory,
                pdf_layout_groups=5,
            )

            for operation, args, label in (
                (run_pdf_search, search_args, "ERROR pdf-search"),
                (
                    run_pdf_layout_groups,
                    layout_args,
                    "ERROR pdf-layout-groups",
                ),
            ):
                with self.subTest(operation=operation.__name__):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = operation(args)
                    self.assertEqual(exit_code, 2)
                    self.assertIn(label, output.getvalue())
                    self.assertNotIn("Traceback", output.getvalue())
                    self.assertFalse(database.exists())
                    self.assertFalse(state_directory.exists())

    def test_incomplete_schema_is_not_initialized_or_migrated_by_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary)
            database = state_directory / "pdf.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE documents(file_key TEXT PRIMARY KEY,path TEXT);
                    CREATE TABLE pages(file_key TEXT,page_number INTEGER);
                    """
                )
                connection.commit()
            before = database.read_bytes()
            args = argparse.Namespace(
                state_directory=state_directory,
                pdf_search="transformador",
                pdf_search_limit=5,
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_pdf_search(args)

            self.assertEqual(exit_code, 2)
            self.assertIn("ERROR pdf-search", output.getvalue())
            self.assertEqual(database.read_bytes(), before)
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table'"
                    )
                }
                page_columns = [
                    row[1] for row in connection.execute("PRAGMA table_info(pages)")
                ]
            self.assertEqual(tables, {"documents", "pages"})
            self.assertEqual(page_columns, ["file_key", "page_number"])

    def test_corrupt_layout_evidence_returns_two_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary)
            database = state_directory / "pdf.sqlite3"
            _create_query_state(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE layout_groups SET evidence_json='not-json' "
                    "WHERE group_key='group-a'"
                )
                connection.commit()
            before = database.read_bytes()
            args = argparse.Namespace(
                state_directory=state_directory,
                pdf_layout_groups=5,
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_pdf_layout_groups(args)

            self.assertEqual(exit_code, 2)
            self.assertIn("ERROR pdf-layout-groups", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
            self.assertEqual(database.read_bytes(), before)

    def test_operating_system_errors_return_two_without_traceback(self):
        args = argparse.Namespace(
            state_directory=Path("unused"),
            pdf_search="transformador",
            pdf_search_limit=5,
        )
        output = io.StringIO()
        with (
            patch(
                "neocortex.capabilities.formats.pdf.pdf_derived_queries.search_pdf_state",
                side_effect=OSError("access denied"),
            ),
            redirect_stdout(output),
        ):
            exit_code = run_pdf_search(args)

        self.assertEqual(exit_code, 2)
        self.assertEqual(output.getvalue().strip(), "ERROR pdf-search access denied")


# endregion [02]


# region [03] Organization mutation boundary


class OrganizationApplyBoundaryTests(unittest.TestCase):
    def test_direct_plan_passes_identity_bound_protected_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            state_directory = base / "state"
            root.mkdir()
            state_directory.mkdir()
            organization_root = base / "organized"
            args = argparse.Namespace(
                root=root,
                state_directory=state_directory,
                organization_root=organization_root,
                organization_min_confidence=0.81,
                document_taxonomy=None,
            )
            summary = SimpleNamespace(
                considered=0,
                planned=0,
                review_required=0,
                blocked=0,
                already_organized=0,
            )
            internal_policy = disjoint_internal_paths_policy(base)
            protected_policy = ProtectedContentPolicy.capture(())

            with (
                patch(
                    "neocortex.documents.document_catalog.update_document_catalog",
                    return_value=(),
                ),
                patch(
                    "neocortex.documents.document_organization."
                    "plan_document_organization",
                    return_value=summary,
                ) as plan_mock,
                patch(
                    "neocortex.safety.internal_paths."
                    "canonical_internal_paths_policy",
                    return_value=internal_policy,
                ) as internal_factory,
                patch(
                    "neocortex.safety.protected_content."
                    "canonical_protected_content_policy",
                    return_value=protected_policy,
                ) as protected_factory,
                patch("neocortex.runtime.control.locking.FrameworkRunLock"),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run_organization_plan(args)

            self.assertEqual(exit_code, 0)
            plan_mock.assert_called_once()
            call = plan_mock.call_args
            guard = call.kwargs["mutation_guard"]
            self.assertIsInstance(guard, CorpusMutationGuard)
            self.assertIs(guard.internal_paths_policy, internal_policy)
            self.assertIs(guard.protected_content_policy, protected_policy)
            internal_factory.assert_called_once_with()
            protected_factory.assert_called_once_with()
            self.assertEqual(guard.policy.mode, "normal")
            self.assertEqual(guard.policy.root, root)
            self.assertEqual(
                call.args,
                (state_directory / "document_catalog.sqlite3", organization_root),
            )
            self.assertEqual(call.kwargs["min_confidence"], 0.81)

    def test_direct_apply_passes_identity_bound_normal_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            state_directory = base / "state"
            root.mkdir()
            state_directory.mkdir()
            organization_root = base / "organized"
            args = argparse.Namespace(
                root=root,
                state_directory=state_directory,
                organization_root=organization_root,
                organization_max_actions=7,
            )
            summary = SimpleNamespace(
                selected=0,
                applied=0,
                stale=0,
                blocked=0,
                failed=0,
                cache_synced=0,
                cache_pending=0,
                remaining=0,
            )
            internal_policy = disjoint_internal_paths_policy(base)
            protected_policy = ProtectedContentPolicy.capture(())

            with (
                patch(
                    "neocortex.documents.document_organization."
                    "apply_document_organization",
                    return_value=summary,
                ) as apply_mock,
                patch(
                    "neocortex.safety.internal_paths."
                    "canonical_internal_paths_policy",
                    return_value=internal_policy,
                ) as policy_factory,
                patch(
                    "neocortex.safety.protected_content."
                    "canonical_protected_content_policy",
                    return_value=protected_policy,
                ) as protected_factory,
                patch("neocortex.runtime.control.locking.FrameworkRunLock"),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run_organization_apply(args)

            self.assertEqual(exit_code, 0)
            apply_mock.assert_called_once()
            call = apply_mock.call_args
            guard = call.kwargs["mutation_guard"]
            self.assertIsInstance(guard, CorpusMutationGuard)
            self.assertIs(guard.internal_paths_policy, internal_policy)
            self.assertIs(guard.protected_content_policy, protected_policy)
            policy_factory.assert_called_once_with()
            protected_factory.assert_called_once_with()
            self.assertEqual(guard.policy.mode, "normal")
            self.assertEqual(guard.policy.root, root)
            self.assertIsNotNone(guard.policy.root_device_id)
            self.assertIsNotNone(guard.policy.root_file_id)
            self.assertIsNotNone(guard.policy.root_birthtime_ns)
            self.assertEqual(
                call.args,
                (state_directory / "document_catalog.sqlite3", organization_root),
            )
            self.assertEqual(call.kwargs["max_actions"], 7)

    def test_direct_plan_rejects_protected_state_before_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            state_directory = base / "protected-state"
            root.mkdir()
            state_directory.mkdir()
            args = argparse.Namespace(
                root=root,
                state_directory=state_directory,
                organization_root=base / "organized",
                organization_min_confidence=0.81,
                document_taxonomy=None,
            )
            internal_policy = disjoint_internal_paths_policy(base)
            protected_policy = ProtectedContentPolicy.capture(
                (
                    ProtectedPathSpec(
                        "protected-state",
                        "tree",
                        "exclude",
                        state_directory,
                    ),
                )
            )

            with (
                patch(
                    "neocortex.documents.document_catalog.update_document_catalog"
                ) as catalog_mock,
                patch(
                    "neocortex.documents.document_organization."
                    "plan_document_organization"
                ) as plan_mock,
                patch(
                    "neocortex.safety.internal_paths."
                    "canonical_internal_paths_policy",
                    return_value=internal_policy,
                ),
                patch(
                    "neocortex.safety.protected_content."
                    "canonical_protected_content_policy",
                    return_value=protected_policy,
                ),
                patch("neocortex.runtime.control.locking.FrameworkRunLock") as lock_mock,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run_organization_plan(args)

            self.assertEqual(exit_code, 2)
            lock_mock.assert_not_called()
            catalog_mock.assert_not_called()
            plan_mock.assert_not_called()
            self.assertFalse((state_directory / "framework.lock").exists())

    def test_direct_apply_rejects_protected_state_before_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            state_directory = base / "protected-state"
            root.mkdir()
            state_directory.mkdir()
            args = argparse.Namespace(
                root=root,
                state_directory=state_directory,
                organization_root=base / "organized",
                organization_max_actions=7,
            )
            internal_policy = disjoint_internal_paths_policy(base)
            protected_policy = ProtectedContentPolicy.capture(
                (
                    ProtectedPathSpec(
                        "protected-state",
                        "tree",
                        "exclude",
                        state_directory,
                    ),
                )
            )

            with (
                patch(
                    "neocortex.documents.document_organization."
                    "apply_document_organization"
                ) as apply_mock,
                patch(
                    "neocortex.safety.internal_paths."
                    "canonical_internal_paths_policy",
                    return_value=internal_policy,
                ),
                patch(
                    "neocortex.safety.protected_content."
                    "canonical_protected_content_policy",
                    return_value=protected_policy,
                ),
                patch("neocortex.runtime.control.locking.FrameworkRunLock") as lock_mock,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run_organization_apply(args)

            self.assertEqual(exit_code, 2)
            lock_mock.assert_not_called()
            apply_mock.assert_not_called()
            self.assertFalse((state_directory / "framework.lock").exists())

    def test_direct_plan_rejects_protected_sqlite_hardlink_before_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "corpus"
            state_directory = base / "state"
            protected_file = base / "protected.sqlite3"
            target = state_directory / "document_catalog.sqlite3"
            root.mkdir()
            state_directory.mkdir()
            protected_file.write_bytes(b"protected-bytes")
            try:
                os.link(protected_file, target)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            args = argparse.Namespace(
                root=root,
                state_directory=state_directory,
                organization_root=base / "organized",
                organization_min_confidence=0.81,
                document_taxonomy=None,
            )
            internal_policy = disjoint_internal_paths_policy(base)
            protected_policy = ProtectedContentPolicy.capture(
                (
                    ProtectedPathSpec(
                        "protected-sqlite",
                        "file",
                        "exclude",
                        protected_file,
                    ),
                )
            )
            before = protected_file.read_bytes()

            with (
                patch(
                    "neocortex.documents.document_catalog.update_document_catalog"
                ) as catalog_mock,
                patch(
                    "neocortex.documents.document_organization."
                    "plan_document_organization"
                ) as plan_mock,
                patch(
                    "neocortex.safety.internal_paths."
                    "canonical_internal_paths_policy",
                    return_value=internal_policy,
                ),
                patch(
                    "neocortex.safety.protected_content."
                    "canonical_protected_content_policy",
                    return_value=protected_policy,
                ),
                patch("neocortex.runtime.control.locking.FrameworkRunLock") as lock_mock,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run_organization_plan(args)

            self.assertEqual(exit_code, 2)
            lock_mock.assert_not_called()
            catalog_mock.assert_not_called()
            plan_mock.assert_not_called()
            self.assertEqual(protected_file.read_bytes(), before)


# endregion [03]


if __name__ == "__main__":
    unittest.main()
