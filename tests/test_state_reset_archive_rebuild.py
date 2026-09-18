"""Public reset integration must consume durable Archive reconstruction proofs."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive.materialization import materialize_archive
from neocortex.capabilities.formats.archive.state import initialize_archive_state
from neocortex.persistence.state_reset import (
    STATE_RESET_CONFIRMATION, StateResetError, StateResetResult,
    execute_state_reset, plan_state_reset,
)


def _fixture(tmp_path: Path, *, archive_db: bool = False, nested: bool = False):
    source = tmp_path / "original.zip"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("content.sqlite", b"document bytes, not an owner database")
    if nested:
        inner = output.getvalue()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("inner.zip", inner)
    source.write_bytes(output.getvalue())
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    destination = state / "archive-materialized" / "fixture"
    manifest = materialize_archive(source, destination, apply=True,
        manifest_directory=state / "archive-manifests", artifact_registry_root=state / "artifacts",
        scratch_directory=tmp_path / "apply-scratch")
    if archive_db:
        initialize_archive_state(state / "archive.sqlite3")
    cache = state / "runtime-cache"
    cache.mkdir()
    (cache / "cache.bin").write_bytes(b"regenerable cache")
    return state, source, destination, manifest, cache


@pytest.mark.parametrize("archive_db,nested", [(False, False), (True, False), (False, True)])
def test_total_reset_retires_proven_output_and_preserves_original_and_manifest(
    tmp_path: Path, archive_db: bool, nested: bool,
) -> None:
    state, source, destination, manifest, cache = _fixture(tmp_path, archive_db=archive_db, nested=nested)
    original = source.read_bytes()
    plan = plan_state_reset(state, scope="all")
    assert plan.inventory is not None and not plan.inventory.blockers, plan.as_payload()
    result = execute_state_reset(state, scope="all", apply=True,
        plan_digest=plan.plan_digest, confirmation=STATE_RESET_CONFIRMATION,
        backup_directory=tmp_path / "backup")
    assert isinstance(result, StateResetResult)
    assert source.read_bytes() == original
    assert Path(manifest.manifest_path).exists()
    assert not list(destination.rglob("content.sqlite"))
    assert not cache.exists()


@pytest.mark.parametrize("mutation", ["unknown", "source_changed"])
def test_total_reset_abstains_before_any_other_target_when_archive_proof_fails(
    tmp_path: Path, mutation: str,
) -> None:
    state, source, destination, _manifest, cache = _fixture(tmp_path)
    if mutation == "unknown":
        (destination / "personal.sqlite").write_bytes(b"preserve this added file")
    else:
        source.write_bytes(b"X" * source.stat().st_size)
    plan = plan_state_reset(state, scope="all")
    assert plan.inventory is not None and plan.inventory.blockers
    with pytest.raises(StateResetError):
        execute_state_reset(state, scope="all", apply=True,
            plan_digest=plan.plan_digest, confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "backup")
    assert (cache / "cache.bin").read_bytes() == b"regenerable cache"
    assert (destination / "content.sqlite").exists()


def test_total_reset_revalidates_source_after_preview_before_cache_effect(tmp_path: Path) -> None:
    state, source, destination, _manifest, cache = _fixture(tmp_path)
    plan = plan_state_reset(state, scope="all")
    assert plan.inventory is not None and not plan.inventory.blockers
    source.write_bytes(b"X" * source.stat().st_size)
    with pytest.raises(StateResetError):
        execute_state_reset(state, scope="all", apply=True,
            plan_digest=plan.plan_digest, confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "backup")
    assert (cache / "cache.bin").exists()
    assert (destination / "content.sqlite").exists()


def test_reset_does_not_reuse_retired_archive_claim_when_output_is_rematerialized(tmp_path: Path) -> None:
    state, source, destination, _manifest, _cache = _fixture(tmp_path)
    for attempt in range(2):
        if attempt:
            materialize_archive(source, destination, apply=True,
                manifest_directory=state / "archive-manifests", artifact_registry_root=state / "artifacts",
                scratch_directory=tmp_path / "apply-scratch")
        plan = plan_state_reset(state, scope="all")
        assert plan.inventory is not None and not plan.inventory.blockers
        result = execute_state_reset(state, scope="all", apply=True,
            plan_digest=plan.plan_digest, confirmation=STATE_RESET_CONFIRMATION)
        assert result.as_payload()["operational_freshness"] == "fresh"
        assert source.exists() and not (destination / "content.sqlite").exists()
