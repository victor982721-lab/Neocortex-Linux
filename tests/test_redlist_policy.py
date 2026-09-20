from __future__ import annotations

from neocortex.workflow.actions.redlist import (
    REDLIST_ENTRIES,
    redlist_match,
    redlist_policy_digest,
)
from neocortex.safety.kio_trash import metadata_binding
from neocortex.deduplication import FileSnapshot
from neocortex.curation.application import BackendOutcome
from neocortex.deduplication import DedupIndex
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.api.lifecycle_read_api import lifecycle_status_payload
from neocortex.runtime.orchestration.run_manifest import RunBudget, RunManifest
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from tests.internal_paths_test_support import begin_signed_normal_run
import json
from pathlib import Path

import pytest


class _MetadataOnlyBackend:
    def __init__(self) -> None:
        self.calls = 0

    def apply_many_snapshots(self, items, *, root: Path):
        del root
        outcomes = []
        for snapshot, _binding in items:
            self.calls += 1
            Path(snapshot.path).unlink()
            outcomes.append(
                BackendOutcome(
                    "applied",
                    "fixture_verified",
                    receipt_json=json.dumps({"schema": "fixture-redlist/v1"}),
                )
            )
        return tuple(outcomes)


def _snapshot(path: str) -> FileSnapshot:
    return FileSnapshot(path, 1, 2, 3, 4, -1)


def test_redlist_matches_names_and_suffixes_case_insensitively() -> None:
    assert redlist_match("/root/cache.BAK-2") == ".bak-2"
    assert redlist_match("/root/.GITIGNORE") == ".gitignore"
    assert redlist_match("/root/report.sqlite3-wal") == ".sqlite3-wal"
    assert redlist_match("/root/service.TIMER") == ".timer"
    assert redlist_match("/root/report.keep") is None


def test_redlist_does_not_treat_intermediate_version_dots_as_extensions() -> None:
    assert redlist_match("/root/0.1.- Reporte.xlsx") is None
    assert redlist_match("/root/Informe_Rev.A.pdf") is None
    assert redlist_match("/root/WhatsApp_Image_2026-04-09_at_3.04.43_PM.jpeg") is None
    assert redlist_match("/root/rsc.io panicnil v1.1.0 - 522dacd0.txt") is None


def test_redlist_policy_is_stable_and_metadata_binding_does_not_read_payload() -> None:
    assert len(REDLIST_ENTRIES) == 289
    assert redlist_policy_digest().startswith("sha256:")
    first = metadata_binding(_snapshot("/root/a.bak"))
    second = metadata_binding(_snapshot("/root/a.bak"))
    assert first == second
    assert first.startswith("metadata-v1:")


def test_redlist_uses_only_the_current_explicit_list() -> None:
    assert ".0_ cu" in REDLIST_ENTRIES
    assert ".bak legado (v1)" in REDLIST_ENTRIES
    assert ".astro" in REDLIST_ENTRIES
    assert ".py" in REDLIST_ENTRIES
    assert ".timer" in REDLIST_ENTRIES
    assert ".pdbxml" not in REDLIST_ENTRIES
    assert ".bak" not in REDLIST_ENTRIES
    assert ".back" not in REDLIST_ENTRIES
    assert ".backup" not in REDLIST_ENTRIES


def test_redlist_exports_only_policy_symbols() -> None:
    import neocortex.workflow.actions.redlist as redlist

    assert "metadata_binding" not in redlist.__all__
    assert set(redlist.__all__) == {
        "REDLIST_ENTRIES",
        "REDLIST_POLICY_SCHEMA",
        "redlist_match",
        "redlist_policy_digest",
        "redlist_policy_payload",
    }


def test_redlist_prepass_removes_matches_before_content_hashing(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    redlisted = root / "generated.BAK-2"
    redlisted.write_bytes(b"must never be hashed")
    retained = root / "keep.txt"
    retained.write_text("keep", encoding="utf-8")

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("redlist prepass must not hash content")

    monkeypatch.setattr("neocortex.workflow.actions.actions.full_fingerprint", forbidden_hash)
    backend = _MetadataOnlyBackend()
    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=True,
                trash_backend=backend,  # type: ignore[arg-type]
            )
            result = runner.apply_redlist_prepass(policy_digest=redlist_policy_digest())
            successor = index.current_scan_id(scan.scan_id)
            paths = {item.path for item in index.snapshots_page(successor, limit=100)}

    assert result["matched"] == 1
    assert result["applied"] == 1
    assert backend.calls == 1
    assert not redlisted.exists()
    assert str(redlisted) not in paths
    assert str(retained) in paths


def test_redlist_preview_records_plan_without_mutating_or_hashing(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "service.timer"
    candidate.write_bytes(b"must remain in preview")

    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            runner = FrameworkActions(index, state, run_id, scan.scan_id, apply=False)
            result = runner.apply_redlist_prepass(policy_digest=redlist_policy_digest())
            row = state._connection.execute(
                "SELECT action_type,status,evidence FROM file_actions"
            ).fetchone()

    assert result["matched"] == 1
    assert result["planned"] == 1
    assert result["applied"] == 0
    assert result["skipped"] == 0
    assert candidate.exists()
    assert row is not None and row[0:2] == ("trash_redlist", "planned")
    assert '"redlist_entry":".timer"' in row[2]


def test_integrated_all_does_not_select_code_route(tmp_path: Path) -> None:
    orchestrator = FrameworkOrchestrator(
        FrameworkConfig(root=tmp_path, state_directory=tmp_path / "state", route="all")
    )
    assert "code" not in orchestrator.selected_routes


def test_explicit_code_route_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown routes: code"):
        FrameworkOrchestrator(
            FrameworkConfig(root=tmp_path, state_directory=tmp_path / "state", route="code")
        )


def test_all_apply_prefilters_the_corpus_before_planning_and_never_leaves_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    outside = tmp_path / "outside.BAK"
    root.mkdir()
    outside.write_bytes(b"outside")
    redlisted = root / "inside.BAK-2"
    redlisted.write_bytes(b"redlist content")
    retained = root / "keep.txt"
    retained.write_text("retained", encoding="utf-8")

    class Backend(_MetadataOnlyBackend):
        pass

    monkeypatch.setattr("neocortex.curation.application.KioTrashBackend", Backend)
    monkeypatch.setattr(
        "neocortex.workflow.actions.actions.full_fingerprint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected hash")),
    )
    monkeypatch.setattr(FrameworkOrchestrator, "_prepare_run_contract", lambda *_args: None)
    monkeypatch.setattr(
        "neocortex.runtime.orchestration.orchestrator.builtin_route_registry",
        lambda: {},
    )
    result = FrameworkOrchestrator(
        FrameworkConfig(root=root, state_directory=state, route="all", apply_actions=True),
    ).run()

    assert not redlisted.exists()
    assert retained.exists()
    assert outside.exists()
    assert result.scan.files_seen == 1
    with FrameworkState(state / "framework.sqlite3") as framework:
        row = framework._connection.execute(
            "SELECT action_type,evidence FROM file_actions ORDER BY action_id LIMIT 1"
        ).fetchone()
    assert row is not None and row[0] == "trash_redlist"
    assert "\"redlist_entry\":\".bak-2\"" in row[1]


def test_all_apply_route_input_excludes_redlisted_source(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    (root / "discard.BAK-2").write_bytes(b"discard")
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    seen: list[str] = []

    def execute(context):
        for _mime, snapshot in context.framework_state.iter_route_candidates_by_prefix(
            context.run_id, ""
        ):
            seen.append(snapshot.path)
        return {"candidates": len(seen), "processed": len(seen)}

    class Backend(_MetadataOnlyBackend):
        pass

    monkeypatch.setattr("neocortex.curation.application.KioTrashBackend", Backend)
    monkeypatch.setattr(FrameworkOrchestrator, "_prepare_run_contract", lambda *_args: None)
    monkeypatch.setattr(
        "neocortex.workflow.actions.actions.full_fingerprint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected hash")),
    )
    adapter = RouteAdapter("text", execute)
    result = FrameworkOrchestrator(
        FrameworkConfig(root=root, state_directory=state, route="all", apply_actions=True),
        route_registry={"text": adapter},
    ).run()

    assert result.route_failures == {}
    assert seen == [str(root / "keep.txt")]


def test_redlist_reserves_durable_budget_and_publishes_counters(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    redlisted = root / "discard.BAK-2"
    redlisted.write_bytes(b"discard")
    retained = root / "keep.txt"
    retained.write_text("keep", encoding="utf-8")
    backend = _MetadataOnlyBackend()

    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            state.publish_run_manifest(
                run_id,
                RunManifest(
                    run_id=run_id,
                    run_kind="initial",
                    root=str(root),
                    root_identity=(1, 2, -1),
                    selected_routes=("text",),
                    route_capabilities={"text": "safe_replay"},
                    budget={
                        "durable": RunBudget(max_items=10, max_bytes=0).payload(),
                    },
                ).event_payload(),
            )
            reservations: list[tuple[str, int, int]] = []

            def reserve(key: str, items: int, bytes_count: int) -> None:
                reservations.append((key, items, bytes_count))
                state.reserve_run_stage(
                    run_id,
                    "redlist",
                    key,
                    items=items,
                    bytes=bytes_count,
                    worker="redlist",
                )

            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=True,
                trash_backend=backend,  # type: ignore[arg-type]
                reserve_work=reserve,
                cancellation_check=lambda: state.check_run_budget(run_id),
            )
            result = runner.apply_redlist_prepass(policy_digest=redlist_policy_digest())
            budget_by_stage = state.read_run_stage_budget(run_id)
            state.cancel_initial_run(run_id)

    assert result["matched"] == 1
    assert result["applied"] == 1
    assert reservations == [(reservations[0][0], 2, 0)]
    assert budget_by_stage["redlist"] == {"items": 2, "bytes": 0, "reservations": 1}
    assert not redlisted.exists()
    assert retained.exists()

    payload = lifecycle_status_payload(limit=5, state_directory=state_root)
    assert payload["status"] == "ok"
    stages = payload["runs"][0]["stages"]
    completed = [stage for stage in stages if stage["stage"] == "redlist"][-1]
    assert completed["status"] == "completed"
    assert completed["details"]["matched"] == 1
    assert completed["details"]["applied"] == 1
    assert completed["details"]["failed"] == 0
