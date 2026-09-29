"""Final-layout integration using real safe moves, only private fixture state."""

from types import SimpleNamespace

from neocortex.deduplication import DedupIndex
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.platform.content_types import DETECTOR_VERSION, detect_content_type
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.final_corpus_layout import run_residual_layout
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.platform.policy import stat_birthtime_ns
from tests.internal_paths_test_support import begin_signed_normal_run


def _run(state, root):
    run_id = begin_signed_normal_run(state, root)
    metadata = root.stat()
    state.publish_run_manifest(run_id, RunManifest(
        run_id=run_id, run_kind="initial", root=str(root),
        root_identity=(metadata.st_dev, metadata.st_ino, stat_birthtime_ns(metadata)),
        selected_routes=(), configuration={"route": "all", "apply_actions": True},
    ).event_payload())
    return run_id


def _fixture(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "useful.txt").write_text("potentially useful fixture body\n", encoding="utf-8")
    (root / "unknown.bin").write_bytes(b"\0\xff\x88opaque")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = FrameworkConfig(root=root, state_directory=state_dir, route="all", apply_actions=True)
    owner = SimpleNamespace(config=config, _cancellation=CancellationToken())
    return root, state_dir, owner


def test_layout_uses_current_identify_without_reread_and_replays_no_new_effects(tmp_path, monkeypatch):
    root, state_dir, owner = _fixture(tmp_path)
    originals = {path.name: path.read_bytes() for path in root.iterdir()}
    with FrameworkState(state_dir / "framework.sqlite3") as state:
        run_id = _run(state, root)
        with DedupIndex(state_dir / "dedup.sqlite3") as index:
            scan = index.scan(root)
            snapshots = tuple(index.snapshots(scan.scan_id))
            state.store_content_type_cache_batch(
                ((snapshot, detect_content_type(snapshot.path)) for snapshot in snapshots), DETECTOR_VERSION, run_id,
            )
        import neocortex.documents.residual_materialization as materializer
        def forbidden(*_args, **_kwargs):
            raise AssertionError("final layout must consume prior Identify, not reopen payload")
        monkeypatch.setattr(materializer, "identify", forbidden)
        first = run_residual_layout(owner, root=root, state=state, run_id=run_id, scan_id=scan.scan_id)
        assert first["status"] == "completed", first
        assert first["moved"] == 2
        expected = {
            root / "Sin_clasificar/_MIME/text/plain/useful.txt": originals["useful.txt"],
            root / "Sin_clasificar/_MIME/application/octet-stream/unknown.bin": originals["unknown.bin"],
        }
        assert all(path.read_bytes() == value for path, value in expected.items())
        assert {path.name for path in root.iterdir()} == {"Corpus_ordenado", "Sin_clasificar"}
        actions = state._connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0]
        second_id = _run(state, root)
        second = run_residual_layout(owner, root=root, state=state, run_id=second_id, scan_id=scan.scan_id)
        assert second["status"] == "completed", second
        assert second["moved"] == 0 and second["already_materialized"] == 2
        assert state._connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0] == actions


def test_final_layout_preview_preserves_paths_and_bytes(tmp_path):
    from dataclasses import replace
    root, state_dir, owner = _fixture(tmp_path)
    owner.config = replace(owner.config, apply_actions=False)
    before = {path.relative_to(root): path.read_bytes() for path in root.iterdir()}
    with FrameworkState(state_dir / "framework.sqlite3") as state:
        run_id = _run(state, root)
        with DedupIndex(state_dir / "dedup.sqlite3") as index:
            scan = index.scan(root)
            state.store_content_type_cache_batch(
                ((snapshot, detect_content_type(snapshot.path)) for snapshot in index.snapshots(scan.scan_id)),
                DETECTOR_VERSION, run_id,
            )
        result = run_residual_layout(owner, root=root, state=state, run_id=run_id, scan_id=scan.scan_id)
    assert result["planned"] == 2
    assert {path.relative_to(root): path.read_bytes() for path in root.iterdir()} == before


def test_layout_does_not_move_source_with_an_unresolved_prior_action(tmp_path):
    root, state_dir, owner = _fixture(tmp_path)
    source = root / "useful.txt"
    with FrameworkState(state_dir / "framework.sqlite3") as state:
        run_id = _run(state, root)
        with DedupIndex(state_dir / "dedup.sqlite3") as index:
            scan = index.scan(root)
            state.store_content_type_cache_batch(
                ((snapshot, detect_content_type(snapshot.path)) for snapshot in index.snapshots(scan.scan_id)),
                DETECTOR_VERSION, run_id,
            )
        action_id = state.begin_file_action(
            run_id, "correct_extension", str(source), str(root / "useful.json"),
            "application/json", "fixture", True,
        )
        state.finish_file_action(action_id, "failed", "fixture_pre_effect_failure")
        result = run_residual_layout(owner, root=root, state=state, run_id=run_id, scan_id=scan.scan_id)
    assert result["status"] == "partial"
    assert result["withheld_by_prior_action"] == 1
    assert result["moved"] == 1
    assert source.is_file()
    assert not (root / "Sin_clasificar/_MIME/text/plain/useful.txt").exists()
