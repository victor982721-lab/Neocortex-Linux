"""Read-only, bounded diagnostics for external maintenance categories."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from neocortex.runtime.external_maintenance import (
    EXTERNAL_MAINTENANCE_SCHEMA,
    EXTERNAL_CATEGORY_SPECS,
    ExternalCategoryError,
    ExternalMaintenanceManager,
    ExternalRootError,
    diagnose_external,
    external_category_spec,
    plan_external_maintenance,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "external-root"
    root.mkdir(mode=0o700)
    return root


def test_root_and_category_are_explicit_and_absent_root_is_not_created(
    tmp_path: Path,
) -> None:
    with pytest.raises(ExternalRootError):
        ExternalMaintenanceManager(Path("relative"), "application_cache")

    with pytest.raises(ExternalRootError):
        ExternalMaintenanceManager(None, "application_cache")

    with pytest.raises(ExternalCategoryError):
        ExternalMaintenanceManager(tmp_path, "")

    absent = tmp_path / "not-created"
    plan = plan_external_maintenance(absent, "application_cache")
    assert plan.status == "absent"
    assert plan.reason_code == "root_absent"
    assert plan.absent == 1
    assert not absent.exists()


def test_category_registry_keeps_unowned_categories_out_of_profile() -> None:
    assert "desktop_thumbnail_cache" in EXTERNAL_CATEGORY_SPECS
    spec = external_category_spec("desktop_thumbnail_cache")
    assert spec.owner is None
    assert spec.profile == "out_of_profile"

    unknown = external_category_spec("future-category-without-owner")
    assert unknown.owner is None
    assert unknown.profile == "out_of_profile"


def test_no_owner_category_is_observed_without_candidates_or_effects(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    (root / "thumbnail.png").write_bytes(b"thumbnail")

    plan = plan_external_maintenance(root, "desktop_thumbnail_cache")

    assert plan.status == "out_of_profile"
    assert plan.reason_code == "no_owner"
    assert plan.out_of_profile == 1
    assert plan.candidates == plan.applied == 0
    assert plan.read_only and plan.diagnostic_only
    assert plan.to_dict()["schema"] == EXTERNAL_MAINTENANCE_SCHEMA
    assert plan.to_dict()["zero_candidates"] is True
    assert plan.records[0].status == "out_of_profile"
    assert plan.records[0].reason_code == "no_owner"
    assert (root / "thumbnail.png").read_bytes() == b"thumbnail"


def test_known_owner_categories_are_preserved_or_observed_only(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "artifact").write_bytes(b"artifact")

    observed = diagnose_external(root, "project_generated_artifact")
    assert observed.status == "observed"
    assert observed.records[0].status == "observed"
    assert observed.records[0].owner == "project-producer"
    assert observed.records[0].reason_code == "diagnostic_only"
    assert observed.candidates == observed.applied == 0

    preserved = diagnose_external(root, "model_cache")
    assert preserved.status == "preserved"
    assert preserved.records[0].status == "preserved"
    assert preserved.records[0].owner == "neocortex-model-management"
    assert preserved.records[0].reason_code == "preserved_owner"
    assert preserved.candidates == preserved.applied == 0

    historical = diagnose_external(root, "historical_temp_unadopted")
    assert historical.status == "unknown"
    assert historical.records[0].status == "unknown"
    assert historical.records[0].reason_code == "schema_unverified"


def test_root_symlinks_and_symlink_components_are_blocked_without_following(
    tmp_path: Path,
) -> None:
    target = _root(tmp_path)
    (target / "outside").write_bytes(b"outside")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)

    root_plan = plan_external_maintenance(root_link, "application_cache")
    assert root_plan.status == "blocked"
    assert root_plan.reason_code == "root_symlink"
    assert root_plan.records == ()

    component_link = tmp_path / "component-link"
    component_link.symlink_to(tmp_path, target_is_directory=True)
    nested_root = component_link / target.name
    component_plan = plan_external_maintenance(nested_root, "application_cache")
    assert component_plan.status == "blocked"
    assert component_plan.reason_code == "root_path_symlink"
    assert component_plan.records == ()
    assert (target / "outside").read_bytes() == b"outside"


def test_nested_links_hardlinks_and_fifo_are_never_followed_or_candidates(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    os.symlink(outside, root / "link.bin")
    os.link(outside, root / "shared.bin")
    fifo = root / "pipe"
    os.mkfifo(fifo)
    nested = root / "nested"
    nested.mkdir()
    (nested / "payload.bin").write_bytes(b"payload")
    os.symlink(outside, nested / "nested-link.bin")

    plan = plan_external_maintenance(root, "project_generated_artifact")
    by_name = {record.relative_path: record for record in plan.records}

    assert by_name["link.bin"].status == "blocked"
    assert by_name["link.bin"].reason_code == "symlink"
    assert by_name["shared.bin"].status == "blocked"
    assert by_name["shared.bin"].reason_code == "hardlink"
    assert by_name["pipe"].status == "blocked"
    assert by_name["pipe"].reason_code == "non_regular"
    assert by_name["nested/nested-link.bin"].status == "blocked"
    assert by_name["nested/nested-link.bin"].reason_code == "symlink"
    assert "outside.bin" not in by_name
    assert outside.read_bytes() == b"outside"
    assert (nested / "payload.bin").read_bytes() == b"payload"


def test_filesystem_identity_is_recorded_without_reading_payload(tmp_path: Path) -> None:
    root = _root(tmp_path)
    payload = root / "datos\nñ.bin"
    payload.write_bytes(b"not decoded or opened by the diagnostic")
    before = payload.stat()
    before_bytes = payload.read_bytes()

    plan = plan_external_maintenance(root, "project_generated_artifact")
    record = plan.records[0]

    assert record.path_identity is not None
    assert record.path_identity[:2] == (before.st_dev, before.st_ino)
    assert record.path_identity[2] == -1
    assert record.file_type == "regular"
    assert record.observed_uid == before.st_uid
    assert record.nlink == 1
    assert record.apparent_bytes == len(before_bytes)
    assert payload.read_bytes() == before_bytes


def test_entry_and_byte_limits_are_hard_and_report_incomplete_evidence(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    for name in ("a", "b", "c", "d"):
        (root / name).write_bytes(name.encode("ascii") * 100)

    entry_limited = plan_external_maintenance(
        root,
        "project_generated_artifact",
        max_entries=2,
        max_bytes=10_000,
    )
    assert entry_limited.scanned == 2
    assert len(entry_limited.records) == 2
    assert entry_limited.truncated
    assert entry_limited.status == "unknown"
    assert entry_limited.reason_code == "entry_limit"
    assert entry_limited.candidates == 0

    byte_limited = plan_external_maintenance(
        root,
        "project_generated_artifact",
        max_entries=20,
        max_bytes=10,
    )
    assert byte_limited.truncated
    assert byte_limited.status == "unknown"
    assert byte_limited.reason_code == "byte_limit"
    assert byte_limited.observed_bytes <= 10
    assert byte_limited.candidates == byte_limited.applied == 0


def test_directory_depth_limit_does_not_descend_unbounded(tmp_path: Path) -> None:
    root = _root(tmp_path)
    level = root / "level-1"
    level.mkdir()
    (level / "level-2").mkdir()
    (level / "level-2" / "payload").write_bytes(b"payload")

    plan = plan_external_maintenance(
        root,
        "project_generated_artifact",
        max_depth=1,
    )

    assert plan.truncated
    assert plan.reason_code == "depth_limit"
    assert {record.relative_path for record in plan.records} == {"level-1"}
    assert plan.candidates == plan.applied == 0


def test_diagnostic_has_no_mutation_path_and_preserves_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    payload = root / "keep.txt"
    payload.write_bytes(b"keep")
    os.chmod(payload, stat.S_IRUSR | stat.S_IWUSR)
    before = payload.stat()
    before_mode = stat.S_IMODE(before.st_mode)
    before_bytes = payload.read_bytes()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("external diagnostic attempted a mutation")

    monkeypatch.setattr(os, "unlink", forbidden)
    monkeypatch.setattr(os, "remove", forbidden)
    monkeypatch.setattr(os, "rmdir", forbidden)

    manager = ExternalMaintenanceManager(root, "application_cache")
    assert not hasattr(manager, "apply")
    plan = manager.plan()
    after = payload.stat()

    assert plan.read_only and plan.diagnostic_only
    assert plan.candidates == plan.applied == 0
    assert after.st_ino == before.st_ino
    assert stat.S_IMODE(after.st_mode) == before_mode
    assert payload.read_bytes() == before_bytes


def test_cancelled_observation_is_unknown_and_does_not_create_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "payload").write_bytes(b"payload")
    plan = plan_external_maintenance(root, "project_generated_artifact", cancelled=lambda: True)

    assert plan.status == "unknown"
    assert plan.reason_code == "cancelled"
    assert plan.truncated
    assert plan.records == ()
    assert plan.candidates == plan.applied == 0
    assert sorted(path.name for path in root.iterdir()) == ["payload"]
