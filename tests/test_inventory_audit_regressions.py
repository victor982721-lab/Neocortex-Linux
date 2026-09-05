"""Linux inventory edge cases reported by the 0.12 external audit."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.deduplication import (
    DedupIndex,
    InventoryError,
    InventoryScanBudgetExceeded,
    InventoryScanCancelled,
    InventoryWorkBudget,
)
from neocortex.deduplication.inventory import InventoryUnsupportedPathEncoding
from neocortex.deduplication.inventory.resume import (
    InventoryResumeCheckpointStore,
    InventoryResumeConflictError,
    empty_directory_digest,
)
from neocortex.deduplication.inventory.scanner import (
    InventoryBatch,
    InventoryScanDeadlineExceeded,
    InventoryScanner,
)
from neocortex.deduplication.inventory.traversal import FileObservation, InventoryTraversal


@pytest.mark.parametrize("shape", ("empty", "empty_directories", "trailing_directories"))
def test_terminal_checkpoint_captures_all_directories_and_replays(
    tmp_path: Path, shape: str
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    if shape != "empty":
        (root / "z" / "nested").mkdir(parents=True)
    if shape == "trailing_directories":
        (root / "a.bin").write_bytes(b"a")
    checkpoint = tmp_path / "state" / "inventory.json"
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        scan = index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, batch_size=1)
        checkpoint_bytes = checkpoint.read_bytes()
        for _ in range(2):
            assert index.scan(
                root, excluded_paths=(), checkpoint_path=checkpoint, resume=True
            ) == scan
            assert checkpoint.read_bytes() == checkpoint_bytes


def test_resume_cursor_after_directory_uses_the_same_dfs_order(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "a").mkdir(parents=True)
    for name in ("a/z", "a-foo", "b"):
        (root / name).write_bytes(name.encode())
    checkpoint = tmp_path / "state" / "inventory.json"
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(InventoryScanBudgetExceeded):
            index.scan(
                root,
                excluded_paths=(),
                checkpoint_path=checkpoint,
                batch_size=1,
                work_budget=InventoryWorkBudget(max_files=2),
            )
        assert InventoryResumeCheckpointStore(checkpoint, root=root).read().last_relative_cursor == "a-foo"
        scan = index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True)
        assert {Path(item.path).relative_to(root).as_posix() for item in index.snapshots(scan.scan_id)} == {
            "a/z", "a-foo", "b"
        }
        assert index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True) == scan


def test_directory_digest_extension_is_terminal_only_and_terminal_is_immutable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("a", "b"):
        (root / name).write_bytes(b"x")
    checkpoint = tmp_path / "state" / "inventory.json"
    store = InventoryResumeCheckpointStore(checkpoint, root=root)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(InventoryScanBudgetExceeded):
            index.scan(
                root,
                excluded_paths=(),
                checkpoint_path=checkpoint,
                batch_size=1,
                work_budget=InventoryWorkBudget(max_files=1),
            )
        partial = store.read()
        with pytest.raises(InventoryResumeConflictError, match="position changed concurrently"):
            store.write(replace(partial, directory_digest=empty_directory_digest()))
        assert store.read() == partial
        index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True)
        final = store.read()
        with pytest.raises(InventoryResumeConflictError, match="owner is terminal"):
            store.write(replace(final, directory_digest=empty_directory_digest()))
        assert store.read() == final


@pytest.mark.parametrize("stopping", ("cancel", "deadline"))
@pytest.mark.parametrize("checkpointed", (False, True))
def test_empty_inventory_checks_stop_before_publication(
    tmp_path: Path, stopping: str, checkpointed: bool
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    checkpoint = tmp_path / "state" / "inventory.json" if checkpointed else None
    budget = (
        InventoryWorkBudget(cancellation_check=lambda: True)
        if stopping == "cancel"
        else InventoryWorkBudget(deadline_monotonic=1, monotonic_clock=lambda: 1)
    )
    expected = InventoryScanCancelled if stopping == "cancel" else InventoryScanDeadlineExceeded
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(expected):
            index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, work_budget=budget)
        assert index._connection.execute("SELECT status FROM scans").fetchall() == [("partial",)]
        assert index.inventory_checkpoint(root) is None
        if checkpoint is not None:
            saved = InventoryResumeCheckpointStore(checkpoint, root=root).read()
            assert saved.status == "partial"
            assert saved.stop_reason == ("cancelled" if stopping == "cancel" else "deadline_exceeded")
            assert index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True).files_seen == 0


@pytest.mark.parametrize("boundary", ("traversal_done", "sqlite_done"))
def test_cancellation_at_terminal_boundary_never_leaves_a_complete_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    (root / "z").mkdir()
    checkpoint = tmp_path / "state" / "inventory.json"
    stop = False
    if boundary == "traversal_done":
        original_run = InventoryTraversal.run

        def run(self: InventoryTraversal):
            nonlocal stop
            result = original_run(self)
            stop = True
            return result

        monkeypatch.setattr(InventoryTraversal, "run", run)
    else:
        original_complete = InventoryScanner._complete_scan

        def complete(self: InventoryScanner, scan_id, counters):
            nonlocal stop
            original_complete(self, scan_id, counters)
            stop = True

        monkeypatch.setattr(InventoryScanner, "_complete_scan", complete)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(InventoryScanCancelled):
            index.scan(
                root,
                excluded_paths=(),
                batch_size=1,
                checkpoint_path=checkpoint,
                work_budget=InventoryWorkBudget(cancellation_check=lambda: stop),
            )
        assert index._connection.execute("SELECT status FROM scans").fetchall() == [("partial",)]
        assert InventoryResumeCheckpointStore(checkpoint, root=root).read().status == "partial"
        monkeypatch.undo()
        assert index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True).files_seen == 1


@pytest.mark.parametrize("checkpointed", (False, True))
@pytest.mark.parametrize("kind", ("file", "directory"))
def test_non_utf8_path_is_localized_and_supported_rows_survive(
    tmp_path: Path, checkpointed: bool, kind: str
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "normal.txt").write_bytes(b"normal")
    raw_path = os.fsencode(root) + b"/invalid-\xff"
    if kind == "directory":
        os.mkdir(raw_path)
        raw_path += b"/content"
    with open(raw_path, "wb") as stream:
        stream.write(b"keep exact bytes")
    checkpoint = tmp_path / "state" / "inventory.json" if checkpointed else None
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(InventoryUnsupportedPathEncoding) as raised:
            index.scan(root, excluded_paths=(), checkpoint_path=checkpoint)
        assert raised.value.reason_code == "unsupported_path_encoding"
        assert raised.value.coverage == "partial"
        assert raised.value.path_count == 1
        assert all(os.path.exists(os.fsencode(path)) for path in raised.value.paths)
        assert index._connection.execute("SELECT status,files_seen,errors FROM scans").fetchall() == [
            ("partial", 1, 1)
        ]
        assert index._connection.execute("SELECT path FROM files").fetchall() == [(str(root / "normal.txt"),)]
        assert index.inventory_checkpoint(root) is None
        assert index._connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        if checkpoint is not None:
            saved = InventoryResumeCheckpointStore(checkpoint, root=root).read()
            assert saved.status == "partial"
            assert saved.stop_reason == "unsupported_path_encoding"
    with open(raw_path, "rb") as stream:
        assert stream.read() == b"keep exact bytes"


def test_non_utf8_root_is_rejected_before_creating_scan_owner(tmp_path: Path) -> None:
    raw_root = os.fsencode(tmp_path) + b"/invalid-\xff"
    os.mkdir(raw_root)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(InventoryUnsupportedPathEncoding) as raised:
            index.scan(os.fsdecode(raw_root), excluded_paths=())
        assert raised.value.coverage == "unavailable"
        assert index._connection.execute("SELECT count(*) FROM scans").fetchone() == (0,)
    assert os.path.isdir(raw_root)


def test_non_utf8_path_cli_has_typed_partial_error_without_traceback(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "normal.txt").write_bytes(b"normal")
    with open(os.fsencode(root) + b"/invalid-\xff", "wb") as stream:
        stream.write(b"untouched")
    result = subprocess.run(
        [sys.executable, "-m", "neocortex", "--all", "--root", str(root), "--state-directory", str(tmp_path / "state")],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "NEOCORTEX_PROGRESS_STREAM": "1"},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "unsupported_path_encoding" in result.stderr
    assert "coverage=partial" in result.stderr
    assert "Traceback" not in result.stderr


def test_explicitly_excluded_root_is_not_scanned(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "secret").write_bytes(b"excluded")
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(InventoryError, match="root is excluded"):
            index.scan(root, excluded_paths=(root,))
        assert index._connection.execute("SELECT count(*) FROM scans").fetchone() == (0,)
        assert index._connection.execute("SELECT count(*) FROM files").fetchone() == (0,)


@pytest.mark.parametrize("deterministic", (False, True))
def test_directory_entry_added_after_enumeration_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deterministic: bool
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    original_append = InventoryBatch.append

    def append(self: InventoryBatch, observation: FileObservation) -> None:
        original_append(self, observation)
        (root / "late").write_bytes(b"late")

    monkeypatch.setattr(InventoryBatch, "append", append)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(InventoryError, match="directory ancestor changed"):
            index.scan(root, excluded_paths=(), deterministic=deterministic)
        assert index._connection.execute("SELECT status FROM scans").fetchall() == [("partial",)]
        assert index.inventory_checkpoint(root) is None


def test_observed_device_is_persisted_and_used_for_resume_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    original_capture = FileObservation.capture.__func__
    observed_dev = root.stat().st_dev + 1

    def capture(cls, entry, item_stat):
        # No mount privileges are needed: feed the real capture boundary a
        # stat result from another device and check both SQLite and replay.
        metadata = SimpleNamespace(
            st_dev=observed_dev,
            st_ino=item_stat.st_ino,
            st_size=item_stat.st_size,
            st_mtime_ns=item_stat.st_mtime_ns,
        )
        return original_capture(cls, entry, metadata)

    monkeypatch.setattr(FileObservation, "capture", classmethod(capture))
    checkpoint = tmp_path / "state" / "inventory.json"
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        scan = index.scan(root, excluded_paths=(), checkpoint_path=checkpoint)
        assert [snapshot.volume_id for snapshot in index.snapshots(scan.scan_id)] == [observed_dev]
        assert index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True) == scan
        monkeypatch.undo()
        with pytest.raises(InventoryResumeConflictError, match="prefix changed"):
            index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True)
