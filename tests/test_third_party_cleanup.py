"""Explicit third-party Code cleanup remains bounded and receipt-bound."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from neocortex.curation.application import BackendOutcome
from neocortex.deduplication import DedupIndex
from neocortex.deduplication import DedupPlanner
from neocortex.runtime.config.third_party_policy import CodeThirdPartyPolicy
from neocortex.runtime.models import ActionSummary
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


class _FixtureBatchBackend:
    """Tiny injected backend that models verified reversible effects."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def apply_many_snapshots(self, items, *, root: Path):
        del root
        items = tuple(items)
        self.calls.append(len(items))
        outcomes = []
        for snapshot, _digest in items:
            Path(snapshot.path).unlink()
            outcomes.append(
                BackendOutcome(
                    "applied",
                    "fixture_verified",
                    receipt_json=json.dumps({"schema": "fixture-receipt/v1", "path": snapshot.path}),
                )
            )
        return tuple(outcomes)


def _write_fixture_wheel(path: Path, members: dict[str, bytes]) -> None:
    """Create the smallest standards-shaped wheel used as a local witness."""

    dist_info = "fixture_pkg-1.0.dist-info"
    all_members = {
        **members,
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ).encode(),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: fixture-pkg\nVersion: 1.0\n"
        ).encode(),
    }
    record_name = f"{dist_info}/RECORD"
    record = "".join(f"{name},,\n" for name in sorted((*all_members, record_name)))
    all_members[record_name] = record.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_STORED) as archive:
        for name, payload in all_members.items():
            archive.writestr(name, payload)


def test_explicit_third_party_cleanup_moves_only_strong_signals(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    owned = root / "owned"
    owned.mkdir()
    (owned / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (owned / "tool.so").write_bytes(b"\x7fELF\x02\x01\x01\x00owned")
    (root / "vendor" / "library.py").parent.mkdir()
    (root / "vendor" / "library.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "vendor" / "libfoo.so").write_bytes(b"\x7fELF\x02\x01\x01\x00fixture")
    _write_fixture_wheel(
        root / "vendor" / "fixture_pkg-1.0-py3-none-any.whl",
        {"library.py": b"VALUE = 2\n", "libfoo.so": b"\x7fELF\x02\x01\x01\x00fixture"},
    )
    (root / "standalone.so").write_bytes(b"\x7fELF\x02\x01\x01\x00unscoped")
    (root / "vendor" / "archive.zip").write_bytes(b"PK\x03\x04not-a-code-action")
    generated = root / "generated" / "client.py"
    generated.parent.mkdir()
    generated.write_text("VALUE = 4\n", encoding="utf-8")
    (root / "loose.py").write_text("VALUE = 3\n", encoding="utf-8")
    (root / "LICENSE").write_text("keep attribution\n", encoding="utf-8")
    metadata_files = (
        root / "vendor" / "LICENSE-MIT",
        root / "vendor" / "NOTICE.md",
        root / "vendor" / "THIRD-PARTY-NOTICES.txt",
    )
    for metadata in metadata_files:
        metadata.write_text("keep attribution\n", encoding="utf-8")

    backend = _FixtureBatchBackend()
    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=True,
                trash_backend=backend,  # type: ignore[arg-type]
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
                third_party_project_roots=(owned,),
            )
            summary = runner._trash_third_party_code(
                plan,
                ActionSummary(apply_actions=True),
            )

    assert backend.calls == [2]
    assert summary.third_party_candidates == 2
    assert summary.third_party_trashed == 2
    assert summary.third_party_skips == 0
    assert (owned / "main.py").exists()
    assert (owned / "tool.so").exists()
    assert (root / "loose.py").exists()
    assert (root / "LICENSE").exists()
    assert all(path.exists() for path in metadata_files)
    assert (root / "vendor" / "archive.zip").exists()
    assert generated.exists()
    assert not (root / "vendor" / "library.py").exists()
    assert not (root / "vendor" / "libfoo.so").exists()
    assert (root / "standalone.so").exists()


def test_third_party_cleanup_is_preview_only_without_apply(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "vendor" / "library.py"
    candidate.parent.mkdir()
    candidate.write_text("VALUE = 1\n", encoding="utf-8")

    backend = _FixtureBatchBackend()
    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=False,
                trash_backend=backend,  # type: ignore[arg-type]
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
            )
            summary = runner._trash_third_party_code(
                plan,
                ActionSummary(apply_actions=False),
            )

    assert summary.third_party_candidates == 0
    assert summary.third_party_trashed == 0
    assert summary.regeneration_unproven == 1
    assert backend.calls == []
    assert candidate.exists()


def test_applied_third_party_cleanup_precedes_route_candidate_publication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    owned = root / "owned"
    owned.mkdir()
    own_source = owned / "main.py"
    own_source.write_text("VALUE = 1\n", encoding="utf-8")
    vendor_source = root / "vendor" / "library.py"
    vendor_source.parent.mkdir()
    vendor_source.write_text("VALUE = 2\n", encoding="utf-8")
    _write_fixture_wheel(
        root / "vendor" / "fixture_pkg-1.0-py3-none-any.whl",
        {"library.py": b"VALUE = 2\n"},
    )

    backend = _FixtureBatchBackend()
    route_candidate_paths: set[str] = set()
    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=True,
                trash_backend=backend,  # type: ignore[arg-type]
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
                third_party_project_roots=(owned,),
            )
            summary = runner.execute(plan, cleanup_empty_directories=False)
            route_candidate_paths = {
                snapshot.path
                for _mime, snapshot in state.iter_route_candidates_by_prefix(run_id, "")
            }

    assert summary.third_party_trashed == 1
    assert not vendor_source.exists()
    assert own_source.exists()
    assert str(vendor_source) not in route_candidate_paths


def test_legal_metadata_survives_dedupe_and_empty_file_actions(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    keeper = root / "keeper.txt"
    keeper.write_text("same attribution\n", encoding="utf-8")
    duplicate_notice = root / "vendor" / "NOTICE.md"
    duplicate_notice.parent.mkdir()
    duplicate_notice.write_text("same attribution\n", encoding="utf-8")
    empty_license = root / "vendor" / "LICENSE-APACHE"
    empty_license.touch()

    backend = _FixtureBatchBackend()
    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=True,
                trash_backend=backend,  # type: ignore[arg-type]
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
            )
            summary = runner.execute(plan, cleanup_empty_directories=False)

    assert summary.errors == 0
    assert duplicate_notice.exists()
    assert empty_license.exists()
