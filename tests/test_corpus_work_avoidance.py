"""Synthetic cost/admission checks for the Corpus curation boundary.

These tests deliberately keep Catalog/Semantic out of the fixture.  Their
metrics are marked as not observed rather than inferred from route counts.
The expensive work under test is the real SHA-256 ``DedupPlanner`` and the
real Framework Identify route-candidate projection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from neocortex.deduplication import DedupPlanner, DedupIndex
from neocortex.deduplication.inventory.curation_admission import CurationAdmission
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.workflow.mutations import BackendOutcome
from tests.internal_paths_test_support import begin_signed_normal_run
from tests.test_framework_actions import _fixture_trash_receipt


_JUNK_COUNT = 24
_JUNK_PREFIX = "junk-"


def _pdf_payload(marker: bytes, size: int) -> bytes:
    prefix = b"%PDF-1.7\n"
    suffix = b"\n%%EOF\n"
    body = (marker * ((size - len(prefix) - len(suffix)) // len(marker) + 1))[
        : size - len(prefix) - len(suffix)
    ]
    return prefix + body + suffix


def _write_fixture(root: Path) -> tuple[Path, ...]:
    root.mkdir(parents=True)
    # Same-size, same-content junk buckets force the baseline planner to do
    # real SHA-256 work.  The gate excludes them before the planner sees them.
    junk = _pdf_payload(b"JUNK", 2_048)
    junk_paths = tuple(root / f"{_JUNK_PREFIX}{index:03d}.dll" for index in range(_JUNK_COUNT))
    for path in junk_paths:
        path.write_bytes(junk)

    useful = _pdf_payload(b"USEFUL", 2_304)
    useful_paths = (root / "useful-a.pdf", root / "useful-b.pdf")
    for path in useful_paths:
        path.write_bytes(useful)

    unknown = root / "unknown.bin"
    unknown.write_bytes(b"\x00\x01\x02unknown-payload\x00")
    text = root / "notes.txt"
    text.write_text("Bitácora sintética de supervivencia.\n", encoding="utf-8")
    return (*junk_paths, *useful_paths, unknown, text)


def _snapshot_tree(root: Path) -> dict[str, tuple[int, str]]:
    return {
        str(path.relative_to(root)): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _is_junk(snapshot) -> bool:
    return Path(snapshot.path).name.startswith(_JUNK_PREFIX)


class _CountingPlanner(DedupPlanner):
    """Use the real planner while exposing which snapshots reached hashing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fingerprinted: list[str] = []

    def _fingerprint(self, snapshot):  # type: ignore[override]
        self.fingerprinted.append(snapshot.path)
        return super()._fingerprint(snapshot)


def _plan_cost(root: Path, state_root: Path, *, gated: bool) -> dict[str, object]:
    state_root.mkdir(parents=True, exist_ok=True)
    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        if not gated:
            planner = _CountingPlanner(index)
            plan = planner.plan(scan.scan_id, exact_compare=True)
            return {
                "inventory_files": scan.files_seen,
                "sha_files": len(planner.fingerprinted),
                "sha_paths": tuple(sorted(Path(path).name for path in planner.fingerprinted)),
                "sha_bytes": plan.statistics.hash_read_bytes,
                "full_hash_files": plan.statistics.full_hash_files,
                "duplicate_groups": plan.group_count,
                "duplicate_files": plan.redundant_files,
                "duplicate_bytes": plan.reclaimable_bytes,
                "admitted": scan.files_seen,
                "excluded": 0,
            }

        with CurationAdmission(index, checkpoint=lambda: None) as admission:
            admission.capture_delta(scan.scan_id)
            admission.settle(
                lambda snapshot: (
                    not _is_junk(snapshot),
                    "synthetic_artifact_policy" if _is_junk(snapshot) else "survivor",
                )
            )
            planner = _CountingPlanner(index, admission_check=admission.permits)
            plan = planner.plan(scan.scan_id, exact_compare=True)
            summary = admission.summary()
            return {
                "inventory_files": scan.files_seen,
                "sha_files": len(planner.fingerprinted),
                "sha_paths": tuple(sorted(Path(path).name for path in planner.fingerprinted)),
                "sha_bytes": plan.statistics.hash_read_bytes,
                "full_hash_files": plan.statistics.full_hash_files,
                "duplicate_groups": plan.group_count,
                "duplicate_files": plan.redundant_files,
                "duplicate_bytes": plan.reclaimable_bytes,
                "admitted": summary["admitted"],
                "excluded": summary["excluded"],
                "exclusion_reasons": summary["exclusion_reasons"],
            }


def _route_candidates(root: Path, state_root: Path, *, gated: bool) -> dict[str, object]:
    """Observe actual Framework route-candidate rows, not a projected count."""

    state_root.mkdir(parents=True, exist_ok=True)
    with DedupIndex(state_root / "dedup.sqlite3") as index, FrameworkState(
        state_root / "framework.sqlite3"
    ) as state:
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        admission = None
        try:
            if gated:
                admission = CurationAdmission(index, checkpoint=lambda: None)
                admission.capture_delta(scan.scan_id)
                admission.settle(
                    lambda snapshot: (
                        not _is_junk(snapshot),
                        "synthetic_artifact_policy" if _is_junk(snapshot) else "survivor",
                    )
                )
            runner = FrameworkActions(index, state, run_id, scan.scan_id, apply=False)
            if admission is not None:
                runner._action_snapshot_selector = admission.current_page
                runner._action_snapshot_count = admission.pending_count
                runner._expensive_admission = admission.permits
            summary = runner.identify_and_normalize()
            runner._validate_extensions(
                None,
                summary,
                publish_routes=True,
                prune_cache=False,
                reuse_identified=True,
            )
            rows = state._connection.execute(
                "SELECT mime,path FROM route_candidates WHERE run_id=? ORDER BY path",
                (run_id,),
            ).fetchall()
            paths = tuple(str(row[1]) for row in rows)
            return {
                "observed": True,
                "run_id": run_id,
                "count": len(rows),
                "paths": paths,
                "junk_paths": tuple(path for path in paths if _JUNK_PREFIX in Path(path).name),
                "catalog": {
                    "observed": False,
                    "value": None,
                    "mode": "not_run",
                    "reason": "isolated Identify/Dedupe benchmark does not invoke Catalog",
                },
                "semantic": {
                    "observed": False,
                    "value": None,
                    "mode": "not_run",
                    "reason": "isolated Identify/Dedupe benchmark does not invoke Semantic",
                },
            }
        finally:
            if admission is not None:
                admission.close()


def test_real_planner_hashes_only_survivors(tmp_path: Path) -> None:
    baseline_root = tmp_path / "baseline" / "corpus"
    gated_root = tmp_path / "gated" / "corpus"
    _write_fixture(baseline_root)
    _write_fixture(gated_root)

    baseline = _plan_cost(baseline_root, tmp_path / "baseline" / "state", gated=False)
    gated = _plan_cost(gated_root, tmp_path / "gated" / "state", gated=True)

    assert baseline["sha_files"] >= _JUNK_COUNT + 2
    assert gated["sha_files"] == 2
    assert all(_JUNK_PREFIX not in name for name in gated["sha_paths"])
    assert gated["sha_bytes"] < baseline["sha_bytes"]
    assert gated["duplicate_groups"] == 1
    assert gated["duplicate_files"] == 1
    assert gated["excluded"] == _JUNK_COUNT


def test_route_candidates_are_observed_and_admitted_only(tmp_path: Path) -> None:
    baseline_root = tmp_path / "baseline" / "corpus"
    gated_root = tmp_path / "gated" / "corpus"
    _write_fixture(baseline_root)
    _write_fixture(gated_root)

    baseline = _route_candidates(baseline_root, tmp_path / "baseline" / "state", gated=False)
    gated = _route_candidates(gated_root, tmp_path / "gated" / "state", gated=True)

    assert baseline["observed"] is True
    assert gated["observed"] is True
    assert baseline["catalog"]["observed"] is False
    assert baseline["semantic"]["observed"] is False
    assert baseline["junk_paths"]
    assert not gated["junk_paths"]
    assert gated["count"] < baseline["count"]


def test_preview_has_no_physical_mutation(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _write_fixture(root)
    before = _snapshot_tree(root)
    _plan_cost(root, tmp_path / "state", gated=True)
    _route_candidates(root, tmp_path / "route-state", gated=True)
    assert _snapshot_tree(root) == before


def test_incomplete_identify_fallback_is_not_admitted(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    incomplete = root / "incomplete.pdf"
    survivor = root / "survivor.pdf"
    incomplete.write_bytes(_pdf_payload(b"I", 2_048))
    survivor.write_bytes(_pdf_payload(b"S", 2_304))
    with DedupIndex(tmp_path / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with CurationAdmission(index, checkpoint=lambda: None) as admission:
            admission.capture_delta(scan.scan_id)
            admission.settle(
                lambda snapshot: (
                    False if Path(snapshot.path).name == incomplete.name else True,
                    (
                        "identify_incomplete"
                        if Path(snapshot.path).name == incomplete.name
                        else "survivor"
                    ),
                )
            )
            planner = _CountingPlanner(index, admission_check=admission.permits)
            plan = planner.plan(scan.scan_id, exact_compare=True)
            assert incomplete.as_posix() not in planner.fingerprinted
            assert plan.group_count == 0
            assert admission.summary()["exclusion_reasons"] == {"identify_incomplete": 1}


def test_apply_fixture_backend_is_root_and_identity_bound(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    payload = _pdf_payload(b"D", 2_304)
    first = root / "first.pdf"
    second = root / "second.pdf"
    first.write_bytes(payload)
    second.write_bytes(payload)
    backend_calls: list[str] = []

    class FixtureTrash:
        supports_empty_directories = True

        def apply_snapshot(self, snapshot, *, root, source_digest, **_kwargs):
            source = Path(snapshot.path)
            assert source.is_relative_to(root)
            assert snapshot.file_id == source.stat().st_ino
            backend_calls.append(str(source))
            return BackendOutcome(
                "applied",
                "fixture_verified",
                receipt_json=_fixture_trash_receipt(snapshot, source_digest, tmp_path / "trash"),
            )

    state_root = tmp_path / "state"
    state_root.mkdir()
    with DedupIndex(state_root / "dedup.sqlite3") as index, FrameworkState(
        state_root / "framework.sqlite3"
    ) as state:
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        with CurationAdmission(index, checkpoint=lambda: None) as admission:
            admission.capture_delta(scan.scan_id)
            admission.settle(lambda _snapshot: (True, "survivor"))
            plan = DedupPlanner(index, admission_check=admission.permits).plan(scan.scan_id)
            summary = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=True,
                trash_backend=FixtureTrash(),
            ).execute(plan)

    assert summary.duplicates_trashed == 1
    assert len(backend_calls) == 1
    assert first.exists() ^ second.exists()


def test_successful_apply_reports_complete_curation_status(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    payload = _pdf_payload(b"D", 2_304)
    (root / "first.pdf").write_bytes(payload)
    (root / "second.pdf").write_bytes(payload)
    effects: list[str] = []

    class FixtureTrash:
        supports_empty_directories = True

        def apply_snapshot(self, snapshot, *, root, source_digest, **_kwargs):
            assert Path(snapshot.path).is_relative_to(root)
            effects.append(snapshot.path)
            return BackendOutcome(
                "applied",
                "fixture_verified",
                receipt_json=_fixture_trash_receipt(snapshot, source_digest, tmp_path / "trash"),
            )

    monkeypatch.setattr("neocortex.workflow.mutations.KioTrashBackend", FixtureTrash)
    monkeypatch.setattr(
        "neocortex.safety.kio_trash.preflight_kio_trash",
        lambda: SimpleNamespace(client="contained-fixture"),
    )
    monkeypatch.setattr(
        "neocortex.platform.sqlite_runtime_attestation.observe_platform_native_runtime",
        lambda **_kwargs: {"status": "approved", "observed": {}},
    )
    config = FrameworkConfig(
        root=root,
        state_directory=tmp_path / "state",
        route="all",
        apply_actions=True,
        document_catalog_enabled=False,
        global_cpu_slots=2,
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
    )
    result = FrameworkOrchestrator(
        config,
        route_registry={"text": RouteAdapter("text", lambda _context: {"fixture": "text"})},
    ).run()

    assert result.route_results["curation_admission"]["status"] == "completed"
    assert result.route_failures == {}
    assert result.actions.duplicates_trashed == 1
    assert len(effects) == 1
