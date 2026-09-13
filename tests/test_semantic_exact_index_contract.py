"""Authority and lifecycle regressions for the opt-in exact-index adapter.

This inference-only test owns temporary fixture state only.  It never treats a
cache path, identifier, or self-declared digest as authority for an owner.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.semantic_exact_index_fixtures import published_text_fixture


TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


def _index_module() -> Any:
    return importlib.import_module("neocortex.semantic.semantic_exact_index")


def _search_module() -> Any:
    return importlib.import_module("neocortex.semantic.semantic_search_repository")


def _schema_module() -> Any:
    return importlib.import_module("neocortex.semantic.semantic_schema")


def _prepare(
    fixture: Any,
    destination: Path,
    *,
    model_signature: str | None = None,
    text_scope: str = "content",
    max_rows: int | None = None,
    max_total_bytes: int = 4_000_000_000,
    cancellation_check: Callable[[], None] | None = None,
) -> Any:
    api = _index_module()
    return api.prepare_exact_index(
        fixture.database,
        destination,
        model_signature=(
            fixture.model.model_signature
            if model_signature is None
            else model_signature
        ),
        text_scope=text_scope,
        max_rows=fixture.row_count if max_rows is None else max_rows,
        max_total_bytes=max_total_bytes,
        cancellation_check=cancellation_check,
    )


def _open(fixture: Any, directory: Path, *, max_rows: int | None = None) -> Any:
    return _index_module().open_exact_index(
        fixture.database,
        directory,
        max_rows=fixture.row_count if max_rows is None else max_rows,
        max_total_bytes=4_000_000_000,
    )


def _close(handle: Any) -> None:
    close = getattr(handle, "close", None)
    if close is not None:
        close()


def _query(
    fixture: Any,
    *,
    handle: Any | None = None,
    evidence: bool = False,
    cancellation_check: Callable[[], None] | None = None,
) -> Any:
    search = _search_module()
    function = search.search_exact_evidence_page if evidence else search.search_exact_page
    return function(
        fixture.database,
        fixture.query,
        limit=4,
        max_vectors=fixture.row_count,
        after_ref_id=0,
        batch_size=8,
        text_scope="content",
        exact_index=handle,
        cancellation_check=cancellation_check,
    )


def _tree_bytes(directory: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (str(path.relative_to(directory)), path.read_bytes())
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )


def _fence(path: Path) -> dict[str, int]:
    info = path.stat()
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mode": int(info.st_mode),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
    }


def _rewrite_manifest_file_descriptor(directory: Path, name: str) -> None:
    manifest_path = directory / "manifest.json"
    manifest_mode = manifest_path.stat().st_mode & 0o777
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    descriptor = manifest["files"][name]
    artifact = directory / name
    descriptor["bytes"] = artifact.stat().st_size
    descriptor["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    descriptor["fence"] = _fence(artifact)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(manifest_mode)


def _rewrite_ready_marker(directory: Path) -> None:
    api = _index_module()
    manifest_path = directory / "manifest.json"
    ready_path = directory / api.READY_FILE
    ready_mode = ready_path.stat().st_mode & 0o777
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ready_path.chmod(0o600)
    ready_path.write_text(
        api.canonical_json(api._ready_payload(manifest)) + "\n",
        encoding="utf-8",
    )
    ready_path.chmod(ready_mode)


def _mutate_provenance_and_refresh_self_descriptors(directory: Path) -> None:
    artifact = directory / "metadata.bin"
    mode = artifact.stat().st_mode & 0o777
    raw = bytearray(artifact.read_bytes())
    original = b"semantic-exact-index"
    replacement = b"semantic-exact-INDEX"
    assert original in raw
    raw[raw.index(original) : raw.index(original) + len(original)] = replacement
    artifact.chmod(0o600)
    artifact.write_bytes(bytes(raw))
    artifact.chmod(mode)
    _rewrite_manifest_file_descriptor(directory, artifact.name)
    _rewrite_ready_marker(directory)


def _mutate_unannounced_artifact(directory: Path) -> None:
    artifact = directory / "rows.bin"
    mode = artifact.stat().st_mode & 0o777
    artifact.chmod(0o600)
    with artifact.open("ab") as stream:
        stream.write(b"\x00")
    artifact.chmod(mode)


def _mutate_owner_path(database: Path, item_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE semantic_items SET path=? WHERE item_id=?",
            ("/tmp/exact-index-owner-drift.pdf", item_id),
        )


@pytest.fixture
def fixture(tmp_path: Path) -> Any:
    return published_text_fixture(
        tmp_path,
        rows=24,
        dtype="float16",
        with_titles=False,
    )


def test_prepare_open_summary_and_source_bytes_are_verified_and_stable(
    fixture: Any,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "exact-index"
    before = fixture.database.read_bytes()

    prepared = _prepare(fixture, destination)
    summary = prepared.summary()
    usage = prepared.usage_summary()

    assert isinstance(summary, dict)
    assert summary["model_signature"] == fixture.model.model_signature
    assert summary["text_scope"] == "content"
    assert summary["row_count"] == fixture.row_count
    assert isinstance(usage, dict)
    assert {
        "used_queries",
        "fallback_queries",
        "rows_scanned",
        "last_fallback_reason",
    }.issubset(usage)
    assert usage["used_queries"] == 0
    assert usage["fallback_queries"] == 0

    opened = _open(fixture, destination)
    assert opened.summary() == summary
    assert fixture.database.read_bytes() == before
    _close(opened)
    _close(prepared)


@pytest.mark.parametrize("kind", ("relative", "existing", "collision", "symlink"))
def test_output_path_guards_reject_relative_existing_collision_and_symlink(
    fixture: Any,
    tmp_path: Path,
    kind: str,
) -> None:
    if kind == "relative":
        destination = Path("relative-exact-index-contract")
    elif kind == "existing":
        destination = tmp_path / "existing-index"
        destination.mkdir()
        (destination / "sentinel").write_bytes(b"owner")
    elif kind == "collision":
        destination = tmp_path / "collision-index"
        _close(_prepare(fixture, destination))
    else:
        target = tmp_path / "symlink-target"
        target.mkdir()
        destination = tmp_path / "symlink-index"
        destination.symlink_to(target, target_is_directory=True)

    before = _tree_bytes(destination) if destination.is_dir() else ()
    expected_error = ValueError if kind == "relative" else FileExistsError
    with pytest.raises(expected_error):
        _prepare(fixture, destination)
    if kind in {"existing", "collision"}:
        assert _tree_bytes(destination) == before
    if kind == "relative":
        assert not destination.exists()


@pytest.mark.parametrize(
    ("max_rows", "max_total_bytes"),
    (
        (23, 4_000_000_000),
        (24, 1),
    ),
)
def test_bounds_reject_before_owned_output_and_preserve_sentinel(
    fixture: Any,
    tmp_path: Path,
    max_rows: int,
    max_total_bytes: int,
) -> None:
    destination = tmp_path / "bounded-index"
    sentinel = tmp_path / "unowned-sentinel"
    sentinel.write_bytes(b"must-survive")
    with pytest.raises(_index_module().ExactIndexUnavailable):
        _prepare(
            fixture,
            destination,
            max_rows=max_rows,
            max_total_bytes=max_total_bytes,
        )
    assert not destination.exists()
    assert sentinel.read_bytes() == b"must-survive"


@pytest.mark.parametrize("mutation", ("no_head", "future_owner"))
def test_missing_head_and_future_owner_reject_without_migration_or_output(
    fixture: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    if mutation == "no_head":
        with sqlite3.connect(fixture.database) as connection:
            connection.execute("DELETE FROM published_embedding_heads")
    else:
        with sqlite3.connect(fixture.database) as connection:
            connection.execute("PRAGMA user_version=11")
            connection.execute(
                "UPDATE metadata SET value='11' WHERE key='schema_version'"
            )
    before = fixture.database.read_bytes()

    def forbidden_migration(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("exact-index preparation must not migrate an owner")

    schema = _schema_module()
    monkeypatch.setattr(schema, "initialize_semantic_state", forbidden_migration)
    monkeypatch.setattr(schema, "_migrate_from", forbidden_migration)
    destination = tmp_path / "invalid-owner-index"
    with pytest.raises((_index_module().ExactIndexUnavailable, _schema_module().SemanticStateError)):
        _prepare(fixture, destination)
    assert not destination.exists()
    assert fixture.database.read_bytes() == before


@pytest.mark.parametrize("binding", ("model", "scope", "database"))
def test_wrong_model_scope_or_source_binding_is_not_accepted(
    fixture: Any,
    tmp_path: Path,
    binding: str,
) -> None:
    api = _index_module()
    if binding == "model":
        with pytest.raises(api.ExactIndexUnavailable):
            _prepare(fixture, tmp_path / "wrong-model", model_signature="not-the-owner")
        return
    if binding == "scope":
        with pytest.raises(api.ExactIndexUnavailable):
            _prepare(fixture, tmp_path / "wrong-scope", text_scope="title")
        return

    destination = tmp_path / "bound-index"
    _close(_prepare(fixture, destination))
    other = published_text_fixture(
        tmp_path / "other-owner",
        rows=24,
        dtype="float16",
        with_titles=False,
    )
    with pytest.raises(api.ExactIndexUnavailable):
        _open(other, destination)


def test_forged_cache_with_provenance_and_refreshed_self_descriptors_is_rejected(
    fixture: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "forged-index"
    _close(_prepare(fixture, destination))
    before_owner = fixture.database.read_bytes()

    _mutate_provenance_and_refresh_self_descriptors(destination)
    reached_owner_verification = False
    format_module = _index_module()._format
    real_verify = format_module.verify_exact_records

    def spy_verify(*args: Any, **kwargs: Any) -> Any:
        nonlocal reached_owner_verification
        reached_owner_verification = True
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(format_module, "verify_exact_records", spy_verify)
    with pytest.raises(_index_module().ExactIndexUnavailable):
        _open(fixture, destination)
    assert reached_owner_verification
    assert fixture.database.read_bytes() == before_owner


@pytest.mark.parametrize("phase", ("prepare", "open"))
def test_cancellation_propagates_and_does_not_publish_or_modify_cache(
    fixture: Any,
    tmp_path: Path,
    phase: str,
) -> None:
    class Cancelled(Exception):
        pass

    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        raise Cancelled("contract cancellation")

    before_owner = fixture.database.read_bytes()
    if phase == "prepare":
        destination = tmp_path / "cancelled-index"
        with pytest.raises(Cancelled, match="contract cancellation"):
            _prepare(fixture, destination, cancellation_check=cancel)
        assert not destination.exists()
        assert fixture.database.read_bytes() == before_owner
        return

    destination = tmp_path / "open-cancelled-index"
    handle = _prepare(fixture, destination)
    before = _tree_bytes(destination)
    with pytest.raises(Cancelled, match="contract cancellation"):
        _index_module().open_exact_index(
            fixture.database,
            destination,
            max_rows=fixture.row_count,
            max_total_bytes=4_000_000_000,
            cancellation_check=cancel,
        )
    assert calls >= 1
    assert _tree_bytes(destination) == before
    assert fixture.database.read_bytes() == before_owner
    _close(handle)


def test_stale_owner_before_query_falls_back_without_index_work(
    fixture: Any,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "stale-owner-index"
    handle = _prepare(fixture, destination)
    before = handle.usage_summary()
    _mutate_owner_path(fixture.database, fixture.item_ids[0])

    page = _query(fixture, handle=handle)
    after = handle.usage_summary()
    assert page.complete is True or page.next_cursor is not None
    assert after["used_queries"] == before["used_queries"]
    assert after["fallback_queries"] == before["fallback_queries"] + 1
    assert after["rows_scanned"] == before["rows_scanned"]
    assert after["last_fallback_reason"]
    _close(handle)


def test_unverified_handle_is_rejected_and_default_search_never_autobuilds(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _index_module()

    def forbidden_factory(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("search must not build or open an index implicitly")

    monkeypatch.setattr(api, "prepare_exact_index", forbidden_factory)
    monkeypatch.setattr(api, "open_exact_index", forbidden_factory)
    with pytest.raises(TypeError):
        _query(fixture, handle=object())
    _query(fixture, evidence=False)
    _query(fixture, evidence=True)


def test_handle_file_fence_after_effect_abstains_without_retry(
    fixture: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "fence-index"
    handle = _prepare(fixture, destination)
    first_usage = handle.usage_summary()
    _query(fixture, handle=handle)
    used = handle.usage_summary()
    assert used["used_queries"] == first_usage["used_queries"] + 1

    format_module = _index_module()._format
    real_query = format_module.query_exact_view

    def drift_then_query(*args: Any, **kwargs: Any) -> Any:
        _mutate_unannounced_artifact(destination)
        return real_query(*args, **kwargs)

    monkeypatch.setattr(format_module, "query_exact_view", drift_then_query)

    with pytest.raises(
        (
            _index_module().ExactIndexUnavailable,
            _schema_module().SemanticStateError,
        )
    ):
        _query(fixture, handle=handle)
    after = handle.usage_summary()
    assert after["used_queries"] == used["used_queries"]
    _close(handle)
