"""Witness retention regressions for autonomous third-party cleanup."""

from __future__ import annotations

import io
import json
import os
import py_compile
import tarfile
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

from neocortex.curation.application import BackendOutcome
from neocortex.deduplication import (
    DedupIndex,
    DedupPlanner,
    InventoryExclusionPolicy,
    snapshot_path,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.config.third_party_policy import CodeThirdPartyPolicy
from neocortex.runtime.models import ActionSummary
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.workflow.actions.corpus_admission import CorpusAdmissionPolicy, assess_file
from tests.internal_paths_test_support import begin_signed_normal_run


class _ReversibleFixtureBackend:
    """Move effects into a private fixture trash area instead of unlinking."""

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = trash_root
        self.trash_root.mkdir(parents=True, exist_ok=True)
        self.calls: list[tuple[str, ...]] = []

    def apply_many_snapshots(self, items, *, root: Path):
        del root
        items = tuple(items)
        moved: list[str] = []
        outcomes: list[BackendOutcome] = []
        for index, (snapshot, _digest) in enumerate(items):
            source = Path(snapshot.path)
            target = self.trash_root / f"{index}-{source.name}"
            os.replace(source, target)
            moved.append(str(source))
            outcomes.append(
                BackendOutcome(
                    "applied",
                    "fixture_reversible_move",
                    receipt_json=json.dumps(
                        {"schema": "fixture-reversible/v1", "source": str(source), "target": str(target)},
                        sort_keys=True,
                    ),
                )
            )
        self.calls.append(tuple(moved))
        return tuple(outcomes)


def _write_npm_tgz(path: Path, *, javascript: bytes, python: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        package_json = b'{"name":"pkg","version":"1.0.0"}'
        metadata = tarfile.TarInfo("package/package.json")
        metadata.size = len(package_json)
        archive.addfile(metadata, io.BytesIO(package_json))
        for member_name, payload in (("package/index.js", javascript), ("package/module.py", python)):
            member = tarfile.TarInfo(member_name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


@contextmanager
def _runner_fixture(tmp_path: Path) -> Iterator[
    tuple[Path, Path, Path, Path, FrameworkActions, ActionSummary, _ReversibleFixtureBackend]
]:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    package_root = root / "node_modules" / "pkg"
    package_root.mkdir(parents=True)
    javascript = b"module.exports = 1;\n"
    python = b"VALUE = 1\n"
    candidate = package_root / "index.js"
    candidate.write_bytes(javascript)
    source = package_root / "module.py"
    source.write_bytes(python)
    pyc = package_root / "module.pyc"
    py_compile.compile(os.fspath(source), cfile=os.fspath(pyc), doraise=True)
    witness = root / "source.tgz"
    _write_npm_tgz(witness, javascript=javascript, python=python)

    backend = _ReversibleFixtureBackend(root / ".fixture-trash")
    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root, exclusion_policy=InventoryExclusionPolicy.compile(()))
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
                third_party_project_roots=(root / "interested",),
            )
            summary = runner.execute(plan, cleanup_empty_directories=False)
            yield root, witness, source, pyc, runner, summary, backend


def test_real_npm_witness_is_pinned_even_when_archive_is_process_admitted(tmp_path: Path) -> None:
    with _runner_fixture(tmp_path) as (root, witness, source, pyc, runner, summary, backend):
        decision = assess_file(
            snapshot_path(witness),
            root=root,
            policy=CorpusAdmissionPolicy((root / "interested",)),
        )

        assert decision.disposition == "process"
        assert decision.reason == "archive_processable_by_owner"
        assert summary.regeneration_proven >= 3
        assert summary.third_party_trashed >= 2
        assert witness.exists()
        assert witness.read_bytes().startswith(b"\x1f\x8b")
        assert source.exists()
        assert not pyc.exists()
        assert all(str(witness) not in batch for batch in backend.calls)
        assert str(witness) in runner._retained_regeneration_sources


def test_retained_witness_vetoes_later_duplicate_and_rename_effects(tmp_path: Path) -> None:
    with _runner_fixture(tmp_path) as (root, witness, _source, _pyc, runner, _summary, backend):
        witness_snapshot = snapshot_path(witness)
        before = witness.read_bytes()

        assert runner._effect_preservation_reason(witness_snapshot) == "retained_regeneration_witness"
        assert runner._rename_protected_reason(witness, root / "renamed.tgz") == "retained_regeneration_witness"
        for _ in range(2):
            applied, failed, protected = runner._apply_trash_batch(
                "trash_duplicate",
                ((str(witness), "fixture-witness"),),
                expected_snapshots=(witness_snapshot,),
                reference_snapshots=(None,),
            )
            assert (applied, failed) == (0, 0)
            assert protected == 1
            assert witness.exists()
            assert witness.read_bytes() == before
        assert all(str(witness) not in batch for batch in backend.calls)
