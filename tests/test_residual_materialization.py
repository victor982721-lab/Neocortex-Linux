"""Residual MIME materialization acceptance tests."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.deduplication import snapshot_path
from neocortex.deduplication import DedupIndex
from neocortex.documents.document_cache_sync import (
    DocumentMoveTransition,
    synchronize_moved_documents,
)
from neocortex.platform.content_types import DETECTOR_VERSION
from neocortex.documents.residual_materialization import (
    UNKNOWN_MIME,
    materialize_residual_documents,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run


@contextmanager
def _apply_environment(root: Path):
    state_directory = root.parent / "state"
    state_directory.mkdir(exist_ok=True)
    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id = begin_signed_normal_run(state, root)
        yield {
            "framework_state": state,
            "run_id": run_id,
            "mutation_guard": state.corpus_mutation_guard(run_id),
            "framework_lock_held": True,
        }


def _decision(path: Path, mime: str, snapshot=None) -> dict[str, object]:
    observed = snapshot or snapshot_path(path)
    return {
        "path": str(path),
        "mime": mime,
        "volume_id": observed.volume_id,
        "file_id": observed.file_id,
        "size": observed.size,
        "mtime_ns": observed.mtime_ns,
        "birthtime_ns": observed.birthtime_ns,
        "detector_version": DETECTOR_VERSION,
        "evidence": "fixture:identify",
    }


def test_preview_has_no_filesystem_effect_and_unknown_does_not_use_suffix(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "looks-like.pdf"
    source.write_bytes(b"not a PDF and not a recognized document\x00\x01")

    result = materialize_residual_documents(root, [source])

    assert result.preview
    assert result.planned == 1
    assert result.moves[0].mime == UNKNOWN_MIME
    assert source.is_file()
    assert not (root / "Sin_clasificar").exists()
    assert result.moves[0].target_path is not None
    assert result.moves[0].target_path.endswith("application/octet-stream/looks-like.pdf")


def test_incomplete_identify_fence_abstains_without_claiming_mime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "ambiguous.pdf"
    source.write_bytes(b"payload")
    observed = snapshot_path(source)

    import neocortex.documents.residual_materialization as residual

    monkeypatch.setattr(
        residual,
        "identify",
        lambda _path: (_ for _ in ()).throw(AssertionError("invalid cache decision must abstain")),
    )
    result = materialize_residual_documents(
        root,
        [{"path": str(source), "snapshot": observed}],
        [{"path": str(source), "mime": "application/pdf"}],
    )

    assert result.moves[0].mime == UNKNOWN_MIME
    assert result.moves[0].status == "planned"


def test_apply_materializes_validated_identify_without_rereading_payload(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "large.bin"
    source.write_bytes(b"fixture payload")
    observed = snapshot_path(source)

    import neocortex.documents.residual_materialization as residual

    def fail_identify(_path: Path):
        raise AssertionError("validated Identify decision must be reused")

    monkeypatch.setattr(residual, "identify", fail_identify)
    with _apply_environment(root) as execution:
        result = materialize_residual_documents(
            root,
            [
                {
                    "path": str(source),
                    "snapshot": observed,
                }
            ],
            [_decision(source, "application/pdf", observed)],
            apply=True,
            **execution,
        )

    target = root / "Sin_clasificar/_MIME/application/pdf/large.bin"
    assert result.moved == 1
    assert target.read_bytes() == b"fixture payload"
    assert not source.exists()
    assert result.moves[0].receipt_json is not None
    assert (
        json.loads(result.moves[0].receipt_json)["residual_receipt_schema"]
        == "neocortex.residual-move-receipt/v1"
    )


def test_stale_identity_abstains_before_move(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "stale.bin"
    source.write_bytes(b"old")
    old = snapshot_path(source)
    source.write_bytes(b"new content")

    with _apply_environment(root) as execution:
        result = materialize_residual_documents(
            root,
            [{"path": str(source), "snapshot": old}],
            [_decision(source, "application/pdf", old)],
            apply=True,
            **execution,
        )

    assert result.stale == 1
    assert source.read_bytes() == b"new content"
    assert not (root / "Sin_clasificar").exists()


def test_collision_uses_metadata_token_not_gnu_suffix_or_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    first_dir = root / "a"
    second_dir = root / "b"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    first = first_dir / "report.pdf"
    second = second_dir / "report.pdf"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_snapshot = snapshot_path(first)
    second_snapshot = snapshot_path(second)

    with _apply_environment(root) as execution:
        result = materialize_residual_documents(
            root,
            [
                {"path": str(first), "snapshot": first_snapshot},
                {"path": str(second), "snapshot": second_snapshot},
            ],
            [
                _decision(first, "application/pdf", first_snapshot),
                _decision(second, "application/pdf", second_snapshot),
            ],
            apply=True,
            **execution,
        )

    bucket = root / "Sin_clasificar/_MIME/application/pdf"
    names = sorted(path.name for path in bucket.iterdir())
    assert names[0] == "report.pdf"
    assert names[1].startswith("report__")
    assert ".~" not in names[1]
    assert {path.read_bytes() for path in bucket.iterdir()} == {b"first", b"second"}
    assert result.moved == 2

    with _apply_environment(root) as execution:
        replay = materialize_residual_documents(
            root,
            [
                {"path": str(first), "snapshot": first_snapshot},
                {"path": str(second), "snapshot": second_snapshot},
            ],
            [
                _decision(first, "application/pdf", first_snapshot),
                _decision(second, "application/pdf", second_snapshot),
            ],
            apply=True,
            **execution,
        )
    assert replay.recovered == 2


def test_unbound_survivor_keeps_owner_absent_and_second_apply_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "unknown.bin"
    source.write_bytes(b"payload")
    observed = snapshot_path(source)

    survivor = {"path": str(source), "snapshot": observed}
    with _apply_environment(root) as execution:
        first = materialize_residual_documents(
            root,
            [survivor],
            [_decision(source, UNKNOWN_MIME, observed)],
            apply=True,
            **execution,
        )
        second = materialize_residual_documents(
            root,
            [survivor],
            [_decision(source, UNKNOWN_MIME, observed)],
            apply=True,
            **execution,
        )

    assert first.moved == 1
    assert second.recovered == 1
    assert second.complete


def test_already_materialized_scan_does_not_create_a_second_framework_action(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "once.bin"
    source.write_bytes(b"payload")
    with _apply_environment(root) as execution:
        first = materialize_residual_documents(root, [source], apply=True, **execution)
        state = execution["framework_state"]
        before = state._connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0]
        second = materialize_residual_documents(root, apply=True, **execution)
        after = state._connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0]
        temp_tables = state._connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE '_neocortex_residual_mime_reservations_%'"
        ).fetchall()

    assert first.moved == 1
    assert second.already_materialized == 1
    assert before == after
    assert temp_tables == []


def test_reservation_temp_table_is_dropped_when_decision_stream_is_cancelled(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "cancel.bin"
    source.write_bytes(b"payload")

    def cancelled(_survivor):
        raise KeyboardInterrupt("cancelled decision stream")

    with _apply_environment(root) as execution:
        state = execution["framework_state"]
        with pytest.raises(KeyboardInterrupt):
            materialize_residual_documents(
                root,
                [source],
                cancelled,
                apply=True,
                **execution,
            )
        temp_tables = state._connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE '_neocortex_residual_mime_reservations_%'"
        ).fetchall()

    assert temp_tables == []


def test_rebind_failure_leaves_durable_physical_receipt_before_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "receipt.bin"
    source.write_bytes(b"payload")
    observed = snapshot_path(source)
    import neocortex.documents.residual_materialization as residual

    monkeypatch.setattr(
        residual,
        "_rebind_catalog_document",
        lambda *_args, **_kwargs: (False, "injected catalog rebind failure"),
    )
    with _apply_environment(root) as execution:
        result = materialize_residual_documents(
            root,
            [{"path": str(source), "snapshot": observed}],
            [_decision(source, UNKNOWN_MIME, observed)],
            apply=True,
            **execution,
        )
        row = execution["framework_state"]._connection.execute(
            "SELECT status,effect_receipt_json FROM file_actions"
        ).fetchone()

    assert result.recovery_required == 1
    assert row[0] == "applied"
    assert row[1] is not None
    assert not source.exists()


def test_unbound_batch_reconciliation_publishes_all_final_paths_in_real_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    sources = tuple(root / f"source-{index}.bin" for index in range(2))
    for index, source in enumerate(sources):
        source.write_bytes(f"payload-{index}".encode())
    snapshots = tuple(snapshot_path(source) for source in sources)
    dedup_path = state_directory / "dedup.sqlite3"
    with DedupIndex(dedup_path) as index:
        index.scan(root)
    destinations = tuple(root / "Sin_clasificar" / "_MIME" / "application" / "octet-stream" / source.name for source in sources)
    for source, destination in zip(sources, destinations, strict=True):
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        transitions = tuple(
            DocumentMoveTransition(
                None,
                None,
                str(source),
                str(destination),
                str(snapshot.volume_id),
                str(snapshot.file_id),
            )
            for source, destination, snapshot in zip(sources, destinations, snapshots, strict=True)
        )
        result = synchronize_moved_documents(
            state_directory,
            transitions,
            framework_lock_held=True,
            framework_connection=state._connection,
            existing_only=True,
        )
        assert result.complete

    with DedupIndex(dedup_path) as index:
        current_scan = index.current_scan_for_path(str(destinations[0]))
        rows = index._connection.execute(
            "SELECT path FROM files WHERE scan_id=? ORDER BY path",
            (current_scan,),
        ).fetchall()
    resolved_paths = {str(row[0]) for row in rows}
    assert {str(destination) for destination in destinations} <= resolved_paths
    assert not ({str(source) for source in sources} & resolved_paths)
