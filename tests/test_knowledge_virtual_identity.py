"""Virtual Knowledge resources must not masquerade as physical files."""

from __future__ import annotations

import pytest

from neocortex.knowledge.knowledge_contracts import (
    PhysicalIdentityRef,
    ResourceRef,
)


def test_archive_member_resource_has_no_physical_identity() -> None:
    resource = ResourceRef(
        "resource:archive:archive:member",
        "archive",
        "archive",
        current_path="/corpus/container.zip!/member.txt",
    )

    assert resource.physical_identity is None


def test_archive_member_resource_rejects_physical_identity() -> None:
    with pytest.raises(
        ValueError,
        match="virtual archive resource cannot expose physical identity",
    ):
        ResourceRef(
            "resource:archive:archive:member",
            "archive",
            "archive",
            PhysicalIdentityRef("owner_file_key", "archive:member", 1),
        )


def test_archive_physical_namespace_remains_separate_from_virtual_namespace() -> None:
    resource = ResourceRef(
        "resource:file:1:2:-1",
        "archive",
        "archive",
        PhysicalIdentityRef("posix_device_inode_birthtime", "1:2:-1", 1),
    )

    assert resource.resource_id.startswith("resource:file:")
    assert resource.physical_identity is not None
