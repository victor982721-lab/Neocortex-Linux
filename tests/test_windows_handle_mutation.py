"""Regression tests for identity-bound Windows rename boundaries."""
# region [00] Contexto del módulo
# Módulo: tests/test_windows_handle_mutation.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations


import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.deduplication import snapshot_path
from neocortex.safety import windows_handle_mutation
from neocortex.safety.windows_handle_mutation import (
    IdentityBoundMutationError,
    MutationEffectUncertainError,
    UnsupportedIdentityBoundMutation,
    rename_no_replace_by_identity,
)
from tests.mutation_containment import ContainedMutationRoot


TEST_CAPABILITIES = ("platform",)
TEST_PLATFORMS = ("win32",)
# endregion [01]

# region [02] Implementación


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handle contract")


@pytest.fixture
def mutation_containment(tmp_path: Path) -> Iterator[ContainedMutationRoot]:
    base = tmp_path / "native-mutation-roots"
    base.mkdir()
    containment = ContainedMutationRoot.create(base, watch_directories=(base,))
    yield containment
    containment.assert_no_leaks()


def _noop_native_boundary() -> None:
    """Provide an explicit inert policy boundary for primitive-only tests."""


def test_public_before_native_callback_is_required(
    mutation_containment: ContainedMutationRoot,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")

    with pytest.raises(TypeError, match="before_native_call"):
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            snapshot_path(source),
        )

    assert source.read_bytes() == b"authorized-object"
    assert not destination.exists()


def test_identity_bound_rename_moves_only_the_retained_file(
    mutation_containment: ContainedMutationRoot,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "renamed.txt"
    source.write_bytes(b"authorized-object")
    expected = snapshot_path(source)

    receipt = mutation_containment.call_rename(
        rename_no_replace_by_identity,
        source,
        destination,
        expected,
        before_native_call=_noop_native_boundary,
    )

    assert not source.exists()
    assert destination.read_bytes() == b"authorized-object"
    assert (receipt.volume_id, receipt.file_id) == expected.identity
    assert receipt.file_system == "NTFS"
    assert receipt.link_count == 1


def test_source_swap_at_native_boundary_is_blocked(
    mutation_containment: ContainedMutationRoot,
) -> None:
    root = mutation_containment.root
    source = root / "authorized.txt"
    authorized_elsewhere = root / "authorized-away.txt"
    replacement = root / "replacement.txt"
    destination = root / "renamed.txt"
    source.write_bytes(b"authorized-object")
    replacement.write_bytes(b"replacement-object")
    expected = snapshot_path(source)
    blocked: list[int] = []

    def attempt_swap() -> None:
        try:
            mutation_containment.rename(source, authorized_elsewhere)
        except PermissionError as exc:
            blocked.append(int(exc.winerror or 0))
        else:  # pragma: no cover - this is the regression being prevented
            mutation_containment.rename(replacement, source)

    mutation_containment.call_rename(
        rename_no_replace_by_identity,
        source,
        destination,
        expected,
        before_native_call=_noop_native_boundary,
        _before_native_call=attempt_swap,
    )

    assert blocked
    assert destination.read_bytes() == b"authorized-object"
    assert replacement.read_bytes() == b"replacement-object"
    assert not authorized_elsewhere.exists()


def test_source_write_open_is_blocked_while_rename_handles_are_retained(
    mutation_containment: ContainedMutationRoot,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")
    blocked: list[int] = []

    def attempt_write() -> None:
        try:
            source.write_bytes(b"concurrent-write")
        except OSError as exc:
            blocked.append(int(exc.winerror or 0))

    mutation_containment.call_rename(
        rename_no_replace_by_identity,
        source,
        destination,
        snapshot_path(source),
        before_native_call=_noop_native_boundary,
        _before_native_call=attempt_write,
    )

    assert blocked
    assert not source.exists()
    assert destination.read_bytes() == b"authorized-object"


def test_destination_parent_swap_at_native_boundary_is_blocked(
    mutation_containment: ContainedMutationRoot,
) -> None:
    root = mutation_containment.root
    source = root / "authorized.txt"
    destination_parent = root / "destination"
    replacement_parent = root / "replacement-parent"
    parent_elsewhere = root / "destination-away"
    destination_parent.mkdir()
    replacement_parent.mkdir()
    source.write_bytes(b"authorized-object")
    expected = snapshot_path(source)
    blocked: list[int] = []

    def attempt_swap() -> None:
        try:
            mutation_containment.rename(destination_parent, parent_elsewhere)
        except PermissionError as exc:
            blocked.append(int(exc.winerror or 0))
        else:  # pragma: no cover - this is the regression being prevented
            mutation_containment.rename(replacement_parent, destination_parent)

    destination = destination_parent / "renamed.txt"
    mutation_containment.call_rename(
        rename_no_replace_by_identity,
        source,
        destination,
        expected,
        before_native_call=_noop_native_boundary,
        _before_native_call=attempt_swap,
    )

    assert blocked
    assert destination.read_bytes() == b"authorized-object"
    assert not parent_elsewhere.exists()


@pytest.mark.parametrize("changed_role", ("source", "destination parent"))
def test_retained_binding_change_after_public_callback_abstains_before_native_call(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
    changed_role: str,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")
    expected = snapshot_path(source)
    fault_injection_seen = False
    policy_boundary_seen = False
    original = windows_handle_mutation._validate_retained_path_binding

    def validate_binding(
        path: Path,
        handle_identity: object,
        *,
        role: str,
    ) -> os.stat_result:
        if policy_boundary_seen and role == changed_role:
            raise IdentityBoundMutationError(f"simulated {role} path binding change")
        return original(path, handle_identity, role=role)  # type: ignore[arg-type]

    def inject_fault() -> None:
        nonlocal fault_injection_seen
        fault_injection_seen = True

    def revalidate_policy() -> None:
        nonlocal policy_boundary_seen
        assert fault_injection_seen
        policy_boundary_seen = True

    monkeypatch.setattr(
        windows_handle_mutation,
        "_validate_retained_path_binding",
        validate_binding,
    )
    with pytest.raises(IdentityBoundMutationError, match="path binding change"):
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            expected,
            before_native_call=revalidate_policy,
            _before_native_call=inject_fault,
        )

    assert fault_injection_seen
    assert policy_boundary_seen
    assert source.read_bytes() == b"authorized-object"
    assert not destination.exists()


def test_destination_created_at_native_boundary_is_never_replaced(
    mutation_containment: ContainedMutationRoot,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")
    expected = snapshot_path(source)

    with pytest.raises(FileExistsError):
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            expected,
            before_native_call=_noop_native_boundary,
            _before_native_call=lambda: destination.write_bytes(b"concurrent-object"),
        )

    assert source.read_bytes() == b"authorized-object"
    assert destination.read_bytes() == b"concurrent-object"


def test_recreated_source_with_same_bytes_is_not_authorized(
    mutation_containment: ContainedMutationRoot,
) -> None:
    root = mutation_containment.root
    source = root / "authorized.txt"
    original = root / "original.txt"
    destination = root / "destination.txt"
    source.write_bytes(b"same-bytes")
    expected = snapshot_path(source)
    mutation_containment.rename(source, original)
    source.write_bytes(b"same-bytes")
    os.utime(source, ns=(expected.mtime_ns, expected.mtime_ns))

    with pytest.raises(IdentityBoundMutationError, match="identity changed"):
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            expected,
            before_native_call=_noop_native_boundary,
        )

    assert source.exists()
    assert original.exists()
    assert not destination.exists()


def test_hard_link_source_abstains_before_mutation(
    mutation_containment: ContainedMutationRoot,
) -> None:
    root = mutation_containment.root
    source = root / "authorized.txt"
    linked = root / "linked.txt"
    destination = root / "destination.txt"
    source.write_bytes(b"authorized-object")
    os.link(source, linked)

    with pytest.raises(UnsupportedIdentityBoundMutation, match="hard links"):
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            snapshot_path(source),
            before_native_call=_noop_native_boundary,
        )

    assert source.exists()
    assert linked.exists()
    assert not destination.exists()


def test_link_count_is_revalidated_after_public_boundary(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")
    boundary_seen = False
    original = windows_handle_mutation._legacy_handle_info

    def legacy_info(handle: int) -> object:
        information = original(handle)
        if (
            boundary_seen
            and not information.attributes & windows_handle_mutation.FILE_ATTRIBUTE_DIRECTORY
        ):
            return replace(information, link_count=2)
        return information

    def cross_policy_boundary() -> None:
        nonlocal boundary_seen
        boundary_seen = True

    monkeypatch.setattr(
        windows_handle_mutation,
        "_legacy_handle_info",
        legacy_info,
    )
    with pytest.raises(UnsupportedIdentityBoundMutation, match="hard links"):
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            snapshot_path(source),
            before_native_call=cross_policy_boundary,
        )

    assert boundary_seen
    assert source.read_bytes() == b"authorized-object"
    assert not destination.exists()


def test_cancellation_before_native_call_has_no_effect(
    mutation_containment: ContainedMutationRoot,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")

    with pytest.raises(KeyboardInterrupt):
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            snapshot_path(source),
            before_native_call=_noop_native_boundary,
            cancellation_checkpoint=lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    assert source.exists()
    assert not destination.exists()


def test_interruption_during_native_return_is_explicitly_uncertain(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")
    original = windows_handle_mutation._nt_rename_relative_no_replace

    def interrupted_native_return(
        source_handle: int,
        parent_handle: int,
        target_name: str,
        destination_path: Path,
    ) -> None:
        original(
            source_handle,
            parent_handle,
            target_name,
            destination_path,
        )
        raise KeyboardInterrupt("interrupted after native effect")

    monkeypatch.setattr(
        windows_handle_mutation,
        "_nt_rename_relative_no_replace",
        interrupted_native_return,
    )
    with pytest.raises(MutationEffectUncertainError) as captured:
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            snapshot_path(source),
            before_native_call=_noop_native_boundary,
        )

    assert isinstance(captured.value.cause, KeyboardInterrupt)
    assert not source.exists()
    assert destination.read_bytes() == b"authorized-object"


def test_failure_after_native_success_is_explicitly_uncertain(
    mutation_containment: ContainedMutationRoot,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")

    with pytest.raises(MutationEffectUncertainError) as captured:
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            snapshot_path(source),
            before_native_call=_noop_native_boundary,
            _after_native_call=lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    assert isinstance(captured.value.cause, KeyboardInterrupt)
    assert not source.exists()
    assert destination.read_bytes() == b"authorized-object"


def test_unsupported_volume_abstains_before_native_call(
    mutation_containment: ContainedMutationRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = mutation_containment.root / "authorized.txt"
    destination = mutation_containment.root / "destination.txt"
    source.write_bytes(b"authorized-object")
    monkeypatch.setattr(windows_handle_mutation, "_volume_file_system", lambda _handle: "REFS")

    with pytest.raises(UnsupportedIdentityBoundMutation, match="requires local NTFS"):
        mutation_containment.call_rename(
            rename_no_replace_by_identity,
            source,
            destination,
            snapshot_path(source),
            before_native_call=_noop_native_boundary,
        )

    assert source.exists()
    assert not destination.exists()


# endregion [02]
