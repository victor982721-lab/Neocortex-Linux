"""Deterministic contracts for residue-free tracked source staging."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from neocortex.runtime.source_staging import (
    SourceStagingError,
    parse_git_tracked_paths,
    stage_tracked_source,
)


def test_tracked_source_staging_copies_only_manifest_owners(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    package = source / "neocortex"
    package.mkdir()
    module = package / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    module.chmod(0o744)
    link = source / "README.link"
    link.symlink_to("neocortex/module.py")
    ignored = source / "build/lib/removed_package.py"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("STALE = True\n", encoding="utf-8")

    paths = parse_git_tracked_paths("neocortex/module.py\0README.link\0")
    destination = tmp_path / "staged"
    summary = stage_tracked_source(source, destination, paths)

    assert summary.file_count == 2
    assert summary.regular_bytes == module.stat().st_size
    assert summary.symlink_count == 1
    assert (destination / "neocortex/module.py").read_bytes() == module.read_bytes()
    assert (destination / "neocortex/module.py").stat().st_mode & 0o777 == 0o744
    assert (destination / "README.link").is_symlink()
    assert os.readlink(destination / "README.link") == "neocortex/module.py"
    assert not (destination / "build").exists()


@pytest.mark.parametrize("value", ("", "/absolute.py", "../escape.py", "a\\b.py"))
def test_tracked_source_manifest_rejects_unsafe_paths(value: str) -> None:
    with pytest.raises(SourceStagingError):
        parse_git_tracked_paths(value + "\0")


def test_tracked_source_staging_rejects_existing_destination_and_missing_owner(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(SourceStagingError, match="must not already exist"):
        stage_tracked_source(source, destination, ("missing.py",))
    with pytest.raises(SourceStagingError, match="owner is unavailable"):
        stage_tracked_source(source, tmp_path / "new-destination", ("missing.py",))
