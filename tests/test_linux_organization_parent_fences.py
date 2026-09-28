"""Independent C01 regression: no creation through swapped directory ancestors."""

import os
from pathlib import Path

from neocortex.documents import document_organization_application as application
from neocortex.documents.document_organization import apply_document_organization
from tests.test_linux_organization_application import organization_fixture as organization_fixture


def test_parent_swap_at_mkdir_never_creates_in_foreign_tree(organization_fixture, monkeypatch) -> None:
    fixture = organization_fixture
    fixture.destination_root.mkdir()
    foreign = fixture.base / "foreign"
    foreign.mkdir()
    detached = fixture.base / "detached"
    real_mkdir = os.mkdir
    root_identity = fixture.destination_root.stat()
    injected = False

    def swap(path, mode=0o777, *, dir_fd=None):
        nonlocal injected
        at_frontier = (
            dir_fd is not None
            and (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino)
            == (root_identity.st_dev, root_identity.st_ino)
        ) or (dir_fd is None and Path(path).is_relative_to(fixture.destination_root)
              and Path(path) != fixture.destination_root)
        if not injected and at_frontier:
            injected = True
            fixture.destination_root.rename(detached)
            fixture.destination_root.symlink_to(foreign, target_is_directory=True)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(application.os, "mkdir", swap)
    result = apply_document_organization(
        fixture.catalog, fixture.destination_root, mutation_guard=fixture.guard, max_actions=1,
    )
    assert injected
    assert result.applied == 0
    assert fixture.source.read_bytes() == b"synthetic Linux organization fixture"
    assert list(foreign.iterdir()) == []


def test_source_directory_symlink_is_not_followed_during_organization(organization_fixture) -> None:
    fixture = organization_fixture
    real_parent = fixture.base / "real-parent"
    fixture.source.parent.rename(real_parent)
    fixture.source.parent.symlink_to(real_parent, target_is_directory=True)
    result = apply_document_organization(
        fixture.catalog, fixture.destination_root, mutation_guard=fixture.guard, max_actions=1,
    )
    assert result.applied == 0
    assert (real_parent / fixture.source.name).read_bytes() == b"synthetic Linux organization fixture"


def test_hardlinked_source_is_kept(organization_fixture) -> None:
    fixture = organization_fixture
    alias = fixture.base / "alias"
    os.link(fixture.source, alias)
    result = apply_document_organization(
        fixture.catalog, fixture.destination_root, mutation_guard=fixture.guard, max_actions=1,
    )
    assert result.applied == 0
    assert fixture.source.exists() and alias.exists()
