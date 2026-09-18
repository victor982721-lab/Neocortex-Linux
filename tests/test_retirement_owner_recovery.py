"""Recovery serializes all manifests but may only reconcile the bound owner."""

from pathlib import Path

import pytest

from neocortex.runtime.artifact_registry import ArtifactRegistry, ArtifactSecurityError


@pytest.mark.parametrize("effect_happened", [False, True])
def test_retirement_recovery_preserves_another_owner_byte_for_byte(tmp_path: Path, effect_happened: bool):
    root = tmp_path / "artifacts"
    producer = ArtifactRegistry(root, owner="owner-b", create_root=True)
    workspace = tmp_path / "producer-b"
    workspace.mkdir(mode=0o700)
    producer.register("b:item", producer="producer-b", path=workspace, root=root,
                      purpose="test", state="completed", disposable=True)
    with pytest.raises(RuntimeError, match="crash boundary"):
        with producer.retirement_guard("b:item"):
            if effect_happened:
                workspace.rmdir()
            raise RuntimeError("crash boundary")
    before = producer.manifest_path("b:item").read_bytes()
    foreign = ArtifactRegistry(root, owner="owner-a")
    recovered = foreign.recover_retirements()
    assert producer.manifest_path("b:item").read_bytes() == before
    assert recovered["confirmed"] == 0
    assert recovered["recovery_required"] == 0
    # An observational parent and an explicitly bound child share the lock;
    # the producer can still finish its own recovery after the abstention.
    with ArtifactRegistry(root).observation_guard() as observation:
        own = observation.for_owner("owner-b").recover_retirements()
    assert own["confirmed"] == int(effect_happened)
    assert own["recovery_required"] == int(not effect_happened)


def test_federated_observation_cannot_start_retirement(tmp_path: Path):
    producer = ArtifactRegistry(tmp_path / "artifacts", owner="producer", create_root=True)
    target = tmp_path / "payload"
    target.write_bytes(b"keep")
    target.chmod(0o600)
    producer.register("item", producer="producer", path=target, root=producer.root,
                      purpose="test", state="completed", disposable=True)
    with pytest.raises(ArtifactSecurityError, match="read-only"):
        with ArtifactRegistry(producer.root, owner=None).retirement_guard("item"):
            pytest.fail("observational view entered the effect boundary")
    assert target.read_bytes() == b"keep"


def test_confirmed_tombstone_allows_only_verified_new_claim_at_same_path(tmp_path: Path):
    registry = ArtifactRegistry(tmp_path / "artifacts", owner="producer", create_root=True)
    target = tmp_path / "payload"
    target.write_bytes(b"old")
    target.chmod(0o600)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    replacement.chmod(0o600)
    old = registry.register("old", producer="producer", path=target, root=registry.root,
                           purpose="test", state="completed", disposable=True)
    with registry.retirement_guard("old"):
        target.unlink()
        registry.update("old", state="retired")
    replacement.rename(target)
    tombstone = registry.verify("old")
    assert tombstone.issue == "artifact_identity_drift"
    assert not registry.retired_claim_is_historical(tombstone, (tombstone,))
    new = registry.register("new", producer="producer", path=target, root=registry.root,
                           purpose="test", state="completed", disposable=True)
    assert new.path_identity != old.path_identity
    assert registry.retired_claim_is_historical(tombstone, (tombstone, new))
    with registry.retirement_guard("new"):
        target.unlink()
        registry.update("new", state="retired")
    assert not target.exists()
