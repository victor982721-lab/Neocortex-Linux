"""Focused regressions for the bounded, metadata-only machine inventory."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from neocortex.runtime.machine_inventory import (
    MACHINE_INVENTORY_CATEGORIES,
    MACHINE_INVENTORY_SCHEMA,
    MACHINE_INVENTORY_STATUSES,
    MachineInventoryRoot,
    category_spec,
    collect_machine_inventory,
    default_machine_inventory_roots,
)


def _root(tmp_path: Path, name: str = "root") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return root


def test_categories_and_defaults_are_bounded_without_creating_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "profile"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("NEOCORTEX_CORPUS_ROOT", raising=False)

    roots = default_machine_inventory_roots()

    assert {root.category for root in roots} == {
        "home",
        "tmp",
        "cache",
        "config",
        "neocortex_state",
        "neocortex_data",
        "neocortex_corpus",
    }
    assert all(root.path != Path("/") for root in roots)
    assert not home.exists()
    assert not (tmp_path / "cache").exists()
    assert set(MACHINE_INVENTORY_CATEGORIES) >= {
        "neocortex_state",
        "neocortex_data",
        "neocortex_corpus",
        "tmp",
        "cache",
        "config",
        "home",
        "external",
    }
    assert set(MACHINE_INVENTORY_STATUSES) == {
        "absent",
        "observed",
        "preserved",
        "blocked",
        "unknown",
        "out_of_profile",
    }


def test_explicit_absent_root_is_reported_without_creation(tmp_path: Path) -> None:
    absent = tmp_path / "not-created"

    report = collect_machine_inventory([MachineInventoryRoot(absent, category="external")])

    assert report.status == "absent"
    assert report.reason_code == "root_absent"
    assert report.absent == 1
    assert report.coverage == "partial"
    assert "root_absent" in report.reason_explanations
    assert report.roots[0].status == "absent"
    assert report.records == ()
    assert not absent.exists()


def test_metadata_identity_types_links_permissions_and_payload_preservation(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    payload = root / "payload.bin"
    payload.write_bytes(b"payload remains untouched")
    symlink = root / "link.bin"
    symlink.symlink_to(payload)
    shared = tmp_path / "shared.bin"
    shared.write_bytes(b"shared")
    os.link(shared, root / "hardlink.bin")
    fifo = root / "pipe"
    os.mkfifo(fifo)
    nested = root / "nested"
    nested.mkdir()
    (nested / "child.txt").write_bytes(b"child")
    before = payload.read_bytes()
    metadata = payload.stat()

    report = collect_machine_inventory(
        [MachineInventoryRoot(root, category="tmp")],
        max_entries=50,
        max_depth=3,
        max_bytes=100_000,
    )
    records = {record.relative_path: record for record in report.records}

    assert report.read_only and report.metadata_only
    assert not report.content_read and not report.sqlite_read
    assert report.to_dict()["schema"] == MACHINE_INVENTORY_SCHEMA
    assert records["payload.bin"].file_type == "regular"
    payload_record = records["payload.bin"]
    assert payload_record.path_identity is not None
    assert payload_record.path_identity[:2] == (metadata.st_dev, metadata.st_ino)
    assert payload_record.birthtime_ns == -1
    assert payload_record.observed_uid == metadata.st_uid
    assert payload_record.permissions == stat.S_IMODE(metadata.st_mode)
    assert records["link.bin"].status == "blocked"
    assert records["link.bin"].reason_code == "symlink"
    assert records["hardlink.bin"].status == "blocked"
    assert records["hardlink.bin"].reason_code == "hardlink"
    assert records["pipe"].status == "blocked"
    assert records["pipe"].reason_code == "non_regular"
    assert records["nested"].file_type == "directory"
    assert payload.read_bytes() == before


def test_global_limits_cover_repeated_roots_and_explain_truncation(tmp_path: Path) -> None:
    first = _root(tmp_path, "first")
    second = _root(tmp_path, "second")
    for directory in (first, second):
        for name in ("a", "b", "c"):
            (directory / name).write_bytes(name.encode() * 16)

    report = collect_machine_inventory(
        [
            MachineInventoryRoot(first, category="tmp"),
            MachineInventoryRoot(second, category="cache"),
        ],
        max_entries=4,
        max_depth=2,
        max_bytes=1_000_000,
    )

    assert report.scanned == len(report.records) == 4
    assert report.truncated
    assert report.reason_code == "entry_limit"
    assert "entry_limit" in report.truncation_reasons
    assert report.roots[1].status == "unknown"
    assert report.roots[1].reason_code == "entry_limit"
    assert sum(report.category_counts.values()) == 4


def test_depth_and_byte_limits_are_global_and_hard(tmp_path: Path) -> None:
    root = _root(tmp_path)
    nested = root / "one"
    nested.mkdir()
    (nested / "two").mkdir()
    (nested / "two" / "payload").write_bytes(b"payload")

    depth = collect_machine_inventory(
        [MachineInventoryRoot(root, category="neocortex_state")],
        max_entries=20,
        max_depth=1,
        max_bytes=100_000,
    )
    assert depth.truncated
    assert depth.reason_code == "depth_limit"
    assert {record.relative_path for record in depth.records} == {"one"}

    byte_root = _root(tmp_path, "byte-root")
    (byte_root / "large").write_bytes(b"x" * 10_000)
    byte = collect_machine_inventory(
        [MachineInventoryRoot(byte_root, category="external")],
        max_entries=20,
        max_depth=2,
        max_bytes=10,
    )
    assert byte.truncated
    assert byte.reason_code == "byte_limit"
    assert byte.observed_bytes <= 10


def test_root_and_component_symlinks_are_blocked_without_following(tmp_path: Path) -> None:
    target = _root(tmp_path, "target")
    (target / "outside").write_bytes(b"outside")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)

    root_report = collect_machine_inventory([root_link])
    assert root_report.status == "blocked"
    assert root_report.reason_code == "root_symlink"
    assert root_report.records == ()

    component = tmp_path / "component"
    component.symlink_to(tmp_path, target_is_directory=True)
    nested_root = component / target.name
    component_report = collect_machine_inventory([nested_root])
    assert component_report.status == "blocked"
    assert component_report.reason_code == "root_path_symlink"
    assert component_report.records == ()
    assert (target / "outside").read_bytes() == b"outside"


def test_category_specs_expose_owner_provenance_and_profile() -> None:
    state = category_spec("state")
    external = category_spec("external")

    assert state.name == "neocortex_state"
    assert state.owner == "neocortex-runtime"
    assert state.provenance
    assert state.profile == "preserved"
    assert external.owner is None
    assert external.profile == "out_of_profile"


def test_single_tuple_root_preserves_its_category(tmp_path: Path) -> None:
    root = _root(tmp_path, "tuple-root")

    report = collect_machine_inventory((root, "cache"), max_entries=4, max_depth=0)

    assert report.root_count == 1
    assert report.roots[0].category == "cache"
