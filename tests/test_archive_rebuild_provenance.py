from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive.materialization import (
    ArchiveMaterializationLimits, materialize_archive,
)
from neocortex.capabilities.formats.archive.rebuild import (
    ARCHIVE_PROVENANCE_OWNER,
    ArchiveManifestReference, ArchiveRebuildBudget, ArchiveRebuildError,
    archive_manifest_surviving_inputs, archive_output_retirement_guard,
    assess_archive_materialization_rebuildability, assess_archive_output_rebuildability,
    revalidate_archive_rebuild_proof,
)
from neocortex.runtime.artifact_registry import ArtifactRegistry


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return data.getvalue()


def _materialize(tmp_path: Path, entries: list[tuple[str, bytes]] | None = None):
    source = tmp_path / "source.zip"
    source.write_bytes(_zip(entries or [("file.sqlite", b"extracted document")] ))
    destination = tmp_path / "out"
    registry_root = tmp_path / "artifacts"
    manifest = materialize_archive(source, destination, apply=True,
        manifest_directory=tmp_path / "manifests", artifact_registry_root=registry_root,
        scratch_directory=tmp_path / "scratch")
    registry = ArtifactRegistry(registry_root, owner=ARCHIVE_PROVENANCE_OWNER)
    claims = [record for record in registry.records() if record.purpose == "archive-materialized-output"]
    refs = {record.metadata["manifest_ref"]["digest"]:
            ArchiveManifestReference.from_dict(record.metadata["manifest_ref"]) for record in claims}
    return source, destination, manifest, registry, claims, tuple(refs.values())


def test_full_provenance_survives_new_process_and_sqlite_owner_removal(tmp_path: Path) -> None:
    source, destination, manifest, _registry, claims, refs = _materialize(tmp_path)
    archive_db = tmp_path / "archive.sqlite3"
    archive_db.write_bytes(b"owner removed independently")
    archive_db.unlink()
    assert manifest.manifest_path and Path(manifest.manifest_path).exists()
    payload = json.loads(Path(manifest.manifest_path).read_text())
    assert payload["schema"] == "neocortex.archive-manifest/v2"
    assert payload["outputs"][0]["entry_identity"] == payload["entries"][0]["identity"]
    assert claims[0].kind == "rebuildable" and not claims[0].disposable
    script = """
import json, sys
from pathlib import Path
from neocortex.capabilities.formats.archive.rebuild import *
ref = ArchiveManifestReference.from_dict(json.loads(sys.argv[3]))
proof = assess_archive_materialization_rebuildability(Path(sys.argv[1]), (ref,), (Path(sys.argv[2]),))
print(json.dumps(proof.to_dict()))
raise SystemExit(0 if proof.rebuildable else 1)
"""
    result = subprocess.run([sys.executable, "-c", script, str(destination), str(source),
                             json.dumps(refs[0].to_dict())], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["outputs"][0]["verified_size"] == len(b"extracted document")


@pytest.mark.parametrize("mutation,blocker", [
    ("missing", "source_unavailable"), ("corrupt", "source_changed"),
    ("output_same_size", "output_changed"), ("manifest", "manifest_changed"),
    ("retired_source", "source_selected_for_retirement"),
])
def test_rebuild_abstains_when_evidence_or_surviving_input_changes(tmp_path: Path, mutation: str, blocker: str) -> None:
    source, _destination, _manifest, _registry, claims, refs = _materialize(tmp_path)
    output = claims[0].path
    retirement_set = ()
    if mutation == "missing":
        source.unlink()
    elif mutation == "corrupt":
        source.write_bytes(b"X" * source.stat().st_size)
    elif mutation == "output_same_size":
        output.write_bytes(b"X" * output.stat().st_size)
    elif mutation == "manifest":
        path = Path(refs[0].path)
        path.write_bytes(path.read_bytes().replace(b'extracted', b'EXTRACTED'))
        # Rewriting even unchanged JSON changes the physical observation.
        os.utime(path, None)
    elif mutation == "retired_source":
        retirement_set = (source,)
    proof = assess_archive_output_rebuildability(output, refs[0], (source,), retirement_set=retirement_set)
    assert not proof.rebuildable and proof.blocker == blocker
    if mutation != "output_same_size":
        assert output.read_bytes() == b"extracted document"


def test_moved_source_requires_explicit_authorized_location(tmp_path: Path) -> None:
    source, _destination, _manifest, _registry, claims, refs = _materialize(tmp_path)
    moved = tmp_path / "authorized-move.zip"
    source.rename(moved)
    assert not assess_archive_output_rebuildability(claims[0], refs[0], (source,)).rebuildable
    assert archive_manifest_surviving_inputs(refs) == ()
    inputs = archive_manifest_surviving_inputs(refs, authorized_locations=(moved,))
    assert assess_archive_output_rebuildability(claims[0], refs[0], inputs).rebuildable


def test_nested_duplicate_names_and_delimiter_names_keep_structural_ancestry(tmp_path: Path) -> None:
    nested = _zip([("deep.txt", b"nested")])
    source, destination, manifest, _registry, _claims, refs = _materialize(tmp_path, [
        ("same.zip", nested), ("same.zip", nested), ("same.zip!/deep.txt", b"literal"),
    ])
    assert len({entry.identity for entry in manifest.entries}) == len(manifest.entries)
    proof = assess_archive_materialization_rebuildability(destination, refs, (source,),
        ArchiveRebuildBudget(scratch_directory=tmp_path / "proof-scratch"))
    assert proof.rebuildable, proof.to_dict()
    assert sorted(len(item.member_identities) for item in proof.outputs) == [1, 2, 2]
    assert source.exists()


@pytest.mark.parametrize("unknown", ["personal.txt", "unknown.sqlite", "extra-folder"])
def test_unknown_additions_block_complete_tree_retirement(tmp_path: Path, unknown: str) -> None:
    source, destination, _manifest, _registry, _claims, refs = _materialize(tmp_path)
    path = destination / unknown
    path.mkdir() if unknown == "extra-folder" else path.write_bytes(b"personal")
    proof = assess_archive_materialization_rebuildability(destination, refs, (source,))
    assert not proof.rebuildable
    assert proof.unknown_paths == (str(path),)


def test_revalidation_rejects_source_changed_after_initial_proof(tmp_path: Path) -> None:
    source, _destination, _manifest, _registry, claims, refs = _materialize(tmp_path)
    proof = assess_archive_output_rebuildability(claims[0], refs[0], (source,))
    assert proof.rebuildable
    source.write_bytes(b"X" * source.stat().st_size)
    current = revalidate_archive_rebuild_proof(proof, (source,))
    assert not current.rebuildable
    assert current.blocker == "source_changed"


def test_rebuild_enforces_expansion_budget_and_cancellation(tmp_path: Path) -> None:
    source, _destination, _manifest, _registry, claims, refs = _materialize(tmp_path, [("file", os.urandom(200_000))])
    budget = ArchiveRebuildBudget(limits=ArchiveMaterializationLimits(max_member_bytes=100))
    assert assess_archive_output_rebuildability(claims[0], refs[0], (source,), budget).blocker == "budget"
    calls = 0
    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls > 3:
            raise RuntimeError("cancelled by caller")
    proof = assess_archive_output_rebuildability(claims[0], refs[0], (source,), ArchiveRebuildBudget(checkpoint=cancel))
    assert proof.blocker == "cancelled"
    assert claims[0].path.exists()


def test_symlink_output_or_parent_never_grants_rebuild_proof(tmp_path: Path) -> None:
    source, destination, _manifest, _registry, claims, refs = _materialize(tmp_path)
    output = claims[0].path
    outside = tmp_path / "personal"
    outside.write_bytes(output.read_bytes())
    output.unlink()
    output.symlink_to(outside)
    assert not assess_archive_output_rebuildability(output, refs[0], (source,)).rebuildable
    assert not assess_archive_materialization_rebuildability(destination, refs, (source,)).rebuildable
    assert outside.read_bytes() == b"extracted document"


def test_crash_after_first_output_keeps_write_ahead_mapping_and_replay(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    source.write_bytes(_zip([("one", b"first"), ("two", b"second")]))
    manifests = tmp_path / "manifests"
    def fail(event):
        if event["event"] == "materialization":
            raise RuntimeError("lost return")
    with pytest.raises(RuntimeError, match="lost return"):
        materialize_archive(source, tmp_path / "out", apply=True, manifest_directory=manifests,
                            journal_hook=fail)
    journals = list(manifests.glob("operation-*.jsonl"))
    records = [json.loads(line) for line in journals[0].read_text().splitlines()]
    assert [item["event"] for item in records] == ["manifest", "materialization_intent", "materialization"]
    initial = Path(records[0]["manifest_ref"]["path"])
    assert json.loads(initial.read_text())["entries"][0]["sha256"]
    assert (tmp_path / "out" / "one").read_bytes() == b"first"
    result = materialize_archive(source, tmp_path / "out", apply=True, manifest_directory=manifests)
    assert result.complete
    assert {output.status for output in result.outputs} == {"reused", "applied"}


def test_retirement_guard_restores_protection_without_effect_and_preserves_source(tmp_path: Path) -> None:
    source, _destination, _manifest, registry, claims, refs = _materialize(tmp_path)
    proof = assess_archive_output_rebuildability(claims[0], refs[0], (source,))
    assert proof.rebuildable
    with archive_output_retirement_guard(proof, registry, (source,), retirement_set=(claims[0].path,)) as guarded:
        assert not guarded.disposable and guarded.state == "active"
    record = registry.verify(claims[0].artifact_id)
    assert record.state == "active" and not record.disposable
    with archive_output_retirement_guard(proof, registry, (source,), retirement_set=(claims[0].path,)):
        claims[0].path.unlink()
    assert source.exists() and Path(refs[0].path).exists()
    assert not claims[0].path.exists()


def test_retirement_guard_detects_source_change_and_does_not_authorize_output(tmp_path: Path) -> None:
    source, _destination, _manifest, registry, claims, refs = _materialize(tmp_path)
    proof = assess_archive_output_rebuildability(claims[0], refs[0], (source,))
    source.write_bytes(b"X" * source.stat().st_size)
    with pytest.raises(ArchiveRebuildError):
        with archive_output_retirement_guard(proof, registry, (source,)):
            pytest.fail("an invalid proof reached its effect")
    assert claims[0].path.exists()
    assert not registry.verify(claims[0].artifact_id).disposable


def test_functional_unit_is_proved_as_exact_original_bytes(tmp_path: Path) -> None:
    source, destination, manifest, _registry, claims, refs = _materialize(tmp_path, [
        ("[Content_Types].xml", b"<Types/>"), ("word/document.xml", b"<document/>"),
    ])
    assert manifest.classification.preserve_as_unit
    proof = assess_archive_materialization_rebuildability(destination, refs, (source,))
    assert proof.rebuildable, proof.to_dict()
    assert proof.outputs[0].member_identities == ()
    assert claims[0].path.read_bytes() == source.read_bytes()


def test_partial_manifests_never_authorize_retirement(tmp_path: Path) -> None:
    source, _destination, manifest, _registry, claims, refs = _materialize(tmp_path, [
        ("../escape", b"bad"), ("valid", b"good"),
    ])
    assert manifest.status == "partial"
    proof = assess_archive_output_rebuildability(claims[0], refs[0], (source,))
    assert not proof.rebuildable and proof.blocker == "coverage_partial"


def test_cancellation_during_nested_spool_preserves_failure_and_new_attempt_replays(tmp_path: Path) -> None:
    nested = _zip([("deep", os.urandom(300_000))])
    source, _destination, _manifest, _registry, claims, refs = _materialize(tmp_path, [("nested.zip", nested)])
    scratch = tmp_path / "proof-scratch"
    def cancel_in_spool() -> None:
        if scratch.exists() and any(path.stat().st_size for path in scratch.glob("*/member-0")):
            raise RuntimeError("cancel during spool")
    proof = assess_archive_output_rebuildability(claims[0], refs[0], (source,),
        ArchiveRebuildBudget(checkpoint=cancel_in_spool, scratch_directory=scratch))
    assert proof.blocker == "cancelled", proof.to_dict()
    retained = list(scratch.glob("*/member-0"))
    assert retained and 0 < retained[0].stat().st_size < len(nested)
    retry = assess_archive_output_rebuildability(claims[0], refs[0], (source,),
        ArchiveRebuildBudget(scratch_directory=scratch))
    assert retry.rebuildable, retry.to_dict()
    assert retained[0].exists()


def test_retirement_guard_process_exit_leaves_no_disposable_permission(tmp_path: Path) -> None:
    source, _destination, _manifest, registry, claims, refs = _materialize(tmp_path)
    script = """
import json, os, sys
from pathlib import Path
from neocortex.capabilities.formats.archive.rebuild import *
from neocortex.runtime.artifact_registry import ArtifactRegistry
ref = ArchiveManifestReference.from_dict(json.loads(sys.argv[3]))
source = Path(sys.argv[2])
proof = assess_archive_output_rebuildability(Path(sys.argv[1]), ref, (source,))
registry = ArtifactRegistry(Path(sys.argv[4]), owner=ARCHIVE_PROVENANCE_OWNER)
with archive_output_retirement_guard(proof, registry, (source,)):
    os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", script, str(claims[0].path), str(source),
                             json.dumps(refs[0].to_dict()), str(registry.root)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 23, result.stdout + result.stderr
    current = registry.verify(claims[0].artifact_id)
    assert current.state == "active" and not current.disposable
    assert current.metadata["neocortex_retirement"]["phase"] == "applying"
    assert not any(item.artifact_id == current.artifact_id for item in registry.plan().eligible_records)
    assert claims[0].path.exists() and source.exists()


def test_retirement_guard_failure_keeps_output_protected(tmp_path: Path) -> None:
    source, _destination, _manifest, registry, claims, refs = _materialize(tmp_path)
    proof = assess_archive_output_rebuildability(claims[0], refs[0], (source,))
    with pytest.raises(RuntimeError, match="failed effect"):
        with archive_output_retirement_guard(proof, registry, (source,)):
            raise RuntimeError("failed effect")
    current = registry.verify(claims[0].artifact_id)
    assert not current.disposable
    assert not any(item.artifact_id == current.artifact_id for item in registry.plan().eligible_records)
    assert current.path.exists()


def test_output_can_be_reconstructed_after_guarded_retirement(tmp_path: Path) -> None:
    source, destination, _manifest, registry, claims, refs = _materialize(tmp_path)
    proof = assess_archive_output_rebuildability(claims[0], refs[0], (source,))
    with archive_output_retirement_guard(proof, registry, (source,)):
        claims[0].path.unlink()
    registry.recover_retirements()
    result = materialize_archive(source, destination, apply=True,
        manifest_directory=tmp_path / "manifests", artifact_registry_root=registry.root)
    assert result.complete
    assert claims[0].path.read_bytes() == b"extracted document"
    new_claims = [record for record in registry.records()
                  if record.purpose == "archive-materialized-output" and record.state == "active"]
    assert len(new_claims) == 1
    assert new_claims[0].artifact_id != claims[0].artifact_id
    ref = ArchiveManifestReference.from_dict(new_claims[0].metadata["manifest_ref"])
    assert assess_archive_output_rebuildability(new_claims[0], ref, (source,)).rebuildable


@pytest.mark.skipif(os.name != "posix", reason="POSIX byte filenames")
def test_source_and_destination_paths_preserve_non_utf8_bytes(tmp_path: Path) -> None:
    source = tmp_path / os.fsdecode(b"source-\xff.zip")
    source.write_bytes(_zip([("file", b"content")]))
    destination = tmp_path / os.fsdecode(b"out-\xfe")
    registry = ArtifactRegistry(tmp_path / "artifacts", owner=ARCHIVE_PROVENANCE_OWNER)
    result = materialize_archive(source, destination, apply=True,
        manifest_directory=tmp_path / "manifests", artifact_registry_root=registry.root)
    claim = next(record for record in registry.records() if record.purpose == "archive-materialized-output")
    reference = ArchiveManifestReference.from_dict(claim.metadata["manifest_ref"])
    proof = assess_archive_output_rebuildability(claim, reference, (source,))
    assert result.complete and proof.rebuildable
    assert os.fsencode(proof.source_path) == os.fsencode(source)


def test_nested_default_proof_uses_bounded_memory_without_creating_scratch(tmp_path: Path) -> None:
    source, destination, _manifest, _registry, claims, refs = _materialize(tmp_path, [
        ("inner.zip", _zip([("nested", b"content")])),
    ])
    before = set(tmp_path.rglob("*"))
    proof = assess_archive_materialization_rebuildability(destination, refs + refs, (source,))
    assert proof.rebuildable, proof.to_dict()
    assert set(tmp_path.rglob("*")) == before
    bounded = assess_archive_output_rebuildability(claims[0], refs[0], (source,),
        ArchiveRebuildBudget(max_memory_spool_bytes=1))
    assert not bounded.rebuildable and bounded.blocker == "scratch_required"
