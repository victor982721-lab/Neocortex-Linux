"""Focused resource-cleanup regressions for the exact-index format layer."""

from __future__ import annotations

import importlib
import os
import stat
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.semantic_exact_index_fixtures import published_text_fixture


TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


def _format_module() -> Any:
    return importlib.import_module("neocortex.semantic.semantic_exact_index_format")


def _index_module() -> Any:
    return importlib.import_module("neocortex.semantic.semantic_exact_index")


def _read_context_module() -> Any:
    return importlib.import_module("neocortex.semantic.semantic_schema")


def _recursive_probe(connection: sqlite3.Connection) -> None:
    connection.execute(
        "WITH RECURSIVE counter(value) AS ("
        "SELECT 1 UNION ALL SELECT value+1 FROM counter WHERE value<256) "
        "SELECT sum(value) FROM counter"
    ).fetchone()


@pytest.mark.parametrize("phase", ("prepare", "open"))
def test_cold_rejection_inside_borrowed_read_context_preserves_progress_and_owner(
    tmp_path: Path,
    phase: str,
) -> None:
    fixture = published_text_fixture(
        tmp_path / "owner",
        rows=24,
        dtype="float16",
        with_titles=False,
    )
    index = _index_module()
    destination = tmp_path / "index"
    if phase == "open":
        index.prepare_exact_index(
            fixture.database,
            destination,
            model_signature=fixture.model.model_signature,
            text_scope="content",
            max_rows=fixture.row_count,
            max_total_bytes=4_000_000_000,
        ).close()
        other = published_text_fixture(
            tmp_path / "other-owner",
            rows=24,
            dtype="float16",
            with_titles=False,
        )
        database = other.database
        def operation() -> Any:
            return index.open_exact_index(
                database,
                destination,
                max_rows=other.row_count,
                max_total_bytes=4_000_000_000,
            )
    else:
        database = fixture.database
        def operation() -> Any:
            return index.prepare_exact_index(
                database,
                destination,
                model_signature=fixture.model.model_signature,
                text_scope="title",
                max_rows=fixture.row_count,
                max_total_bytes=4_000_000_000,
            )

    before = database.read_bytes()
    schema = _read_context_module()
    seen: list[int] = []

    def progress() -> int:
        seen.append(1)
        return 0

    with schema.semantic_read_context() as context:
        with schema.semantic_database(
            database,
            readonly=True,
            read_context=context,
        ) as connection:
            connection.set_progress_handler(progress, 1)
            with pytest.raises(index.ExactIndexUnavailable) as caught:
                operation()
            assert caught.value.reason == "cold_open_requires_independent_scope"
            _recursive_probe(connection)
            assert seen
            connection.set_progress_handler(None, 0)
    assert database.read_bytes() == before


def test_open_directory_path_closes_next_fd_when_previous_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    format_module = _format_module()
    opened = iter((100, 101))
    close_calls: list[int] = []

    def fake_open(*_args: Any, **_kwargs: Any) -> int:
        return next(opened)

    def fake_close(fd: int) -> None:
        close_calls.append(fd)
        if fd == 100:
            raise OSError("already closed")

    monkeypatch.setattr(format_module.os, "open", fake_open)
    monkeypatch.setattr(format_module.os, "close", fake_close)
    with pytest.raises(OSError, match="already closed"):
        format_module._open_directory_path(Path("/component"))
    assert close_calls.count(100) == 1
    assert close_calls.count(101) == 1


def test_validated_view_close_attempts_both_fds_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    format_module = _format_module()
    view = format_module.ValidatedView(
        Path("/tmp/exact-index"),
        {},
        {},
        {},
        100,
        101,
        "exact-index",
        {},
    )
    close_calls: list[int] = []

    def fake_close(fd: int) -> None:
        close_calls.append(fd)
        if fd == 100:
            raise OSError("root close failure")

    monkeypatch.setattr(format_module.os, "close", fake_close)
    with pytest.raises(OSError, match="root close failure"):
        view.close()
    assert close_calls == [100, 101]
    assert view.root_fd == -1
    assert view.parent_fd == -1
    view.close()
    assert close_calls == [100, 101]


def test_mapped_cleanup_closes_every_mapping_and_fd_preserving_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    format_module = _format_module()
    names = (
        "rows.bin",
        "identity.bin",
        "metadata.bin",
        "vectors-f16.bin",
        "vectors-f32.bin",
        "numeric-codes.bin",
        "numeric-norms-f64.bin",
    )
    view = format_module.ValidatedView(
        Path("/tmp/exact-index"),
        {"files": {name: {"bytes": 1, "fence": {}} for name in names}},
        {},
        {},
        100,
        101,
        "exact-index",
        {},
    )
    opened = iter(range(200, 207))
    stream_close_calls: list[int] = []
    mapping_close_calls: list[int] = []
    stable_calls: list[int] = []

    class FakeMapping:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def close(self) -> None:
            mapping_close_calls.append(self.fd)
            if self.fd == 200:
                raise OSError("mapping close failure")

    def fake_open_regular(*_args: Any, **_kwargs: Any) -> tuple[int, Any]:
        return next(opened), SimpleNamespace(st_size=1)

    def fake_mmap(fd: int, *_args: Any, **_kwargs: Any) -> FakeMapping:
        return FakeMapping(fd)

    def fake_close(fd: int) -> None:
        stream_close_calls.append(fd)

    monkeypatch.setattr(format_module, "_open_regular_at", fake_open_regular)
    monkeypatch.setattr(format_module.mmap, "mmap", fake_mmap)
    monkeypatch.setattr(format_module.os, "close", fake_close)
    monkeypatch.setattr(
        format_module.ValidatedView,
        "assert_files_stable",
        lambda _view: stable_calls.append(1),
    )

    with pytest.raises(RuntimeError, match="body failure"):
        with format_module._mapped(view, include_numeric=True, include_norms=True):
            raise RuntimeError("body failure")
    assert mapping_close_calls == list(range(200, 207))
    assert stream_close_calls == list(range(200, 207))
    assert len(stable_calls) == 2


def test_open_regular_close_failure_preserves_primary_error_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    format_module = _format_module()
    close_calls: list[int] = []

    def fake_open(*_args: Any, **_kwargs: Any) -> int:
        return 300

    def fake_fstat(_fd: int) -> Any:
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    def fake_close(fd: int) -> None:
        close_calls.append(fd)
        raise OSError("close raced")

    monkeypatch.setattr(format_module.os, "open", fake_open)
    monkeypatch.setattr(format_module.os, "fstat", fake_fstat)
    monkeypatch.setattr(format_module.os, "close", fake_close)
    with pytest.raises(format_module.FallbackExactRequired, match="regular"):
        format_module._open_regular_at(1, "artifact.bin")
    assert close_calls == [300]


@pytest.mark.parametrize("failure_phase", ("validate", "post_marker", "replaced_marker"))
def test_failed_prepare_does_not_publish_or_remove_a_foreign_completion_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_phase: str,
) -> None:
    fixture = published_text_fixture(tmp_path / "owner", rows=24)
    index = _index_module()
    directory = tmp_path / "index"
    ready = directory / index.READY_FILE
    cancellation = RuntimeError("cancelled preparation")

    if failure_phase == "validate":
        def fail_validation(*_args: Any, **_kwargs: Any) -> Any:
            assert not ready.exists()
            raise cancellation
        monkeypatch.setattr(index._format, "validate_exact_view", fail_validation)

    def checkpoint() -> None:
        if ready.exists():
            if failure_phase == "replaced_marker":
                foreign = directory / "foreign-marker"
                foreign.write_bytes(b"foreign replacement must survive")
                foreign.replace(ready)
            raise cancellation

    with pytest.raises(RuntimeError) as caught:
        index.prepare_exact_index(
            fixture.database, directory,
            model_signature=fixture.model.model_signature,
            cancellation_check=checkpoint,
        )
    assert caught.value is cancellation
    if failure_phase == "replaced_marker":
        assert ready.read_bytes() == b"foreign replacement must survive"
        assert any("completion_marker_replaced" in note for note in cancellation.__notes__)
    else:
        assert not ready.exists()


def test_prepare_total_byte_bound_includes_completion_marker(tmp_path: Path) -> None:
    fixture = published_text_fixture(tmp_path / "owner", rows=24)
    index = _index_module()
    first = tmp_path / "first-index"
    with index.prepare_exact_index(
        fixture.database, first, model_signature=fixture.model.model_signature,
    ):
        total = sum(path.stat().st_size for path in first.iterdir())
    with pytest.raises(index.ExactIndexUnavailable):
        index.prepare_exact_index(
            fixture.database, tmp_path / "bounded-index",
            model_signature=fixture.model.model_signature,
            max_total_bytes=total - (first / index.READY_FILE).stat().st_size,
        )
    assert not (tmp_path / "bounded-index" / index.READY_FILE).exists()


def test_in_place_corruption_restoring_mtime_invalidates_the_warm_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = published_text_fixture(tmp_path / "owner", rows=24)
    index = _index_module()
    repository = importlib.import_module("neocortex.semantic.semantic_search_repository")
    directory = tmp_path / "index"
    with index.prepare_exact_index(
        fixture.database, directory, model_signature=fixture.model.model_signature,
    ) as handle:
        vector_file = directory / "vectors-f16.bin"
        before = vector_file.stat()
        vector_file.chmod(0o600)
        with vector_file.open("r+b") as stream:
            stream.write(b"\x00\x3e")  # finite fp16 1.5, not a malformed-vector shortcut
        os.utime(vector_file, ns=(before.st_atime_ns, before.st_mtime_ns))
        vector_file.chmod(stat.S_IMODE(before.st_mode))
        after = vector_file.stat()
        assert (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode) == (
            before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode,
        )
        assert after.st_ctime_ns != before.st_ctime_ns

        def forbid_numeric(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("corrupt derived bytes must not be scored")

        monkeypatch.setattr(index._format, "_query_numeric", forbid_numeric)
        native = repository.search_exact_page(
            fixture.database, fixture.query, text_scope="content", batch_size=8,
        )
        indexed = repository.search_exact_page(
            fixture.database, fixture.query, text_scope="content", batch_size=8,
            exact_index=handle,
        )
        assert indexed == native
        assert handle.usage_summary()["used_queries"] == 0
        assert handle.usage_summary()["fallback_queries"] == 1
