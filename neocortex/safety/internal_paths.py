"""Canonical internal roots excluded from corpus discovery and mutation.

The policy is independent from :class:`CorpusAccessPolicy`: one describes the
framework's own trees, while the other describes the corpus requested by a
specific run.  Both reuse the same Windows path and physical-identity rules.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

# region [01] Contracts and immutable schema

import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Literal

import xxhash
from neocortex.platform_policy import UNAVAILABLE_BIRTHTIME_NS

from neocortex.runtime.config.app_paths import (
    local_application_data_directory,
    program_installation_directory,
    self_analysis_data_directory,
    source_repository_directory,
    stable_launcher_path,
)
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    _absolute_normalized,
    _birthtime_ns,
    _has_reparse_semantics,
    _is_same_or_descendant,
    _path_key,
    _physical_normalized,
    path_trees_intersect,
)


InternalPathRole = Literal[
    "repository",
    "runtime",
    "application_data",
    "self_analysis",
    "launcher",
]
InternalPathKind = Literal["tree", "file"]

INTERNAL_PATHS_POLICY_VERSION = "internal-paths-policy-v1"
EFFECTIVE_INVENTORY_POLICY_VERSION = "effective-inventory-policy-v1"
EFFECTIVE_INVENTORY_POLICY_VERSION_V2 = "effective-inventory-policy-v2"
INTERNAL_PATH_PROTECTION_REASON = "internal_framework_root"
MAX_INTERNAL_PATH_ENTRIES = 16
MAX_INTERNAL_PATH_MANIFEST_BYTES = 64 * 1024
_VALID_ROLES = frozenset({"repository", "runtime", "application_data", "self_analysis", "launcher"})
_REQUIRED_ROLE_KINDS: dict[InternalPathRole, InternalPathKind] = {
    "repository": "tree",
    "runtime": "tree",
    "application_data": "tree",
    "self_analysis": "tree",
    "launcher": "file",
}


class InternalPathProtectionError(PermissionError):
    """A corpus operation intersects a NeoCortex-owned path."""

    reason_code = INTERNAL_PATH_PROTECTION_REASON

    def __init__(self, detail: str = "internal framework path is protected") -> None:
        self.detail = detail
        super().__init__(f"{self.reason_code}: {detail}")


@dataclass(frozen=True, slots=True)
class InternalPathSpec:
    """One configured internal tree or file before physical capture."""

    role: InternalPathRole
    kind: InternalPathKind
    path: Path


@dataclass(frozen=True, slots=True)
class InternalPathIdentity:
    """Lexical reservation and optional physical identity of one internal path."""

    role: InternalPathRole
    kind: InternalPathKind
    configured_path: Path
    canonical_path: Path
    exists: bool
    device_id: int | None
    file_id: int | None
    birthtime_ns: int | None

    def __post_init__(self) -> None:
        if self.role not in _VALID_ROLES:
            raise ValueError(f"unsupported internal path role: {self.role!r}")
        if self.kind not in {"tree", "file"}:
            raise ValueError(f"unsupported internal path kind: {self.kind!r}")
        configured = _absolute_normalized(self.configured_path)
        canonical = _absolute_normalized(self.canonical_path)
        if _path_key(configured) != _path_key(self.configured_path):
            raise ValueError("configured internal path must be canonical and absolute")
        if _path_key(canonical) != _path_key(self.canonical_path):
            raise ValueError("physical internal path must be canonical and absolute")
        object.__setattr__(self, "configured_path", configured)
        object.__setattr__(self, "canonical_path", canonical)
        identity = (self.device_id, self.file_id)
        if any(value is not None and (type(value) is not int or value < 0) for value in identity):
            raise ValueError("internal path identity values must be non-negative")
        if self.birthtime_ns is not None and (
            type(self.birthtime_ns) is not int or self.birthtime_ns < UNAVAILABLE_BIRTHTIME_NS
        ):
            raise ValueError("internal path birthtime must be -1 or non-negative")
        complete_identity = (*identity, self.birthtime_ns)
        if self.exists and not all(value is not None for value in complete_identity):
            raise ValueError("internal path existence and identity are inconsistent")
        if not self.exists and any(value is not None for value in complete_identity):
            raise ValueError("internal path existence and identity are inconsistent")

    @classmethod
    def capture(
        cls,
        role: InternalPathRole,
        kind: InternalPathKind,
        path: str | os.PathLike[str],
        *,
        allow_missing: bool = True,
    ) -> InternalPathIdentity:
        """Capture one ordinary path or reserve one unambiguous missing path."""

        requested = Path(os.fspath(path))
        if not requested.is_absolute():
            raise ValueError(f"internal path must be absolute: {requested}")
        configured = _absolute_normalized(path)
        path_exists = os.path.lexists(configured)
        physical = _physical_normalized(configured)
        if not path_exists:
            if not allow_missing:
                raise FileNotFoundError(os.fspath(configured))
            if _path_key(configured) != _path_key(physical):
                raise ValueError(
                    f"missing internal path traverses an alias or reparse: {configured}"
                )
            return cls(role, kind, configured, physical, False, None, None, None)

        metadata = os.stat(configured, follow_symlinks=False)
        if _has_reparse_semantics(configured, metadata):
            raise ValueError(f"internal path cannot be a reparse point: {configured}")
        if kind == "tree" and not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(os.fspath(configured))
        if kind == "file" and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"internal launcher is not a regular file: {configured}")
        canonical = _absolute_normalized(os.path.realpath(configured))
        if _path_key(configured) != _path_key(canonical):
            raise ValueError(f"internal path traverses an alias or reparse: {configured}")
        canonical_metadata = os.stat(canonical, follow_symlinks=False)
        requested_identity = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            _birthtime_ns(metadata),
        )
        canonical_identity = (
            int(canonical_metadata.st_dev),
            int(canonical_metadata.st_ino),
            _birthtime_ns(canonical_metadata),
        )
        if requested_identity != canonical_identity:
            raise ValueError(f"internal path resolution is ambiguous: {configured}")
        return cls(role, kind, configured, canonical, True, *canonical_identity)

    @property
    def device_id_hex(self) -> str | None:
        return None if self.device_id is None else f"{self.device_id:x}"

    @property
    def file_id_hex(self) -> str | None:
        return None if self.file_id is None else f"{self.file_id:x}"

    def verify_identity(self) -> None:
        """Fail if a reserved path appeared or a captured object changed."""

        if not self.exists:
            if os.path.lexists(self.configured_path):
                raise InternalPathProtectionError(
                    f"reserved internal path appeared: {self.configured_path}"
                )
            physical = _physical_normalized(self.configured_path)
            if _path_key(physical) != _path_key(self.canonical_path):
                raise InternalPathProtectionError(
                    f"reserved internal path resolution changed: {self.configured_path}"
                )
            return
        try:
            observed = type(self).capture(
                self.role,
                self.kind,
                self.configured_path,
                allow_missing=False,
            )
        except (OSError, ValueError) as exc:
            raise InternalPathProtectionError(
                f"internal path identity cannot be verified: {self.configured_path}"
            ) from exc
        if observed != self:
            raise InternalPathProtectionError(
                f"internal path identity changed: {self.configured_path}"
            )

    def matches_file_identity(self, path: Path) -> bool:
        """Detect an external hardlink alias of a protected internal file."""

        if self.kind != "file" or not self.exists:
            return False
        metadata = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or _has_reparse_semantics(path, metadata):
            return False
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            _birthtime_ns(metadata),
        ) == (self.device_id, self.file_id, self.birthtime_ns)

    def manifest_entry(self) -> dict[str, object]:
        return {
            "birthtime_ns": self.birthtime_ns,
            "canonical_path": str(self.canonical_path),
            "configured_path": str(self.configured_path),
            "device_id_hex": self.device_id_hex,
            "exists": self.exists,
            "file_id_hex": self.file_id_hex,
            "kind": self.kind,
            "role": self.role,
        }


# endregion [01]


# region [02] Policy, signature and boundaries


@dataclass(frozen=True, slots=True)
class InternalPathsPolicy:
    """One bounded, deterministic set of NeoCortex-owned paths."""

    entries: tuple[InternalPathIdentity, ...]
    signature: str

    @classmethod
    def capture(
        cls,
        specs: Iterable[InternalPathSpec],
    ) -> InternalPathsPolicy:
        required_count = len(_REQUIRED_ROLE_KINDS)
        bounded_specs = tuple(islice(specs, required_count + 1))
        if len(bounded_specs) < required_count:
            raise ValueError("internal path policy must contain every role exactly once")
        if len(bounded_specs) > required_count:
            raise ValueError("internal path policy entry count is invalid")
        captured = tuple(
            sorted(
                (
                    InternalPathIdentity.capture(spec.role, spec.kind, spec.path)
                    for spec in bounded_specs
                ),
                key=lambda item: (
                    item.role,
                    item.kind,
                    _path_key(item.configured_path),
                ),
            )
        )
        _validate_policy_topology(captured)
        payload = _manifest_payload(captured)
        signature = f"{INTERNAL_PATHS_POLICY_VERSION}:xxh3_128:{xxhash.xxh3_128_hexdigest(payload)}"
        return cls(captured, signature)

    def __post_init__(self) -> None:
        if not self.entries or len(self.entries) > MAX_INTERNAL_PATH_ENTRIES:
            raise ValueError("internal path policy entry count is invalid")
        ordered = tuple(
            sorted(
                self.entries,
                key=lambda item: (
                    item.role,
                    item.kind,
                    _path_key(item.configured_path),
                ),
            )
        )
        _validate_policy_topology(ordered)
        expected_signature = (
            f"{INTERNAL_PATHS_POLICY_VERSION}:xxh3_128:"
            f"{xxhash.xxh3_128_hexdigest(_manifest_payload(ordered))}"
        )
        if ordered != self.entries or expected_signature != self.signature:
            raise ValueError("internal path policy is inconsistent with its signature")

    def manifest(self) -> dict[str, object]:
        return {
            "entries": [entry.manifest_entry() for entry in self.entries],
            "signature": self.signature,
            "version": INTERNAL_PATHS_POLICY_VERSION,
        }

    def verify_identities(self) -> None:
        for entry in self.entries:
            entry.verify_identity()

    def validate_corpus_access(self, access: CorpusAccessPolicy) -> None:
        """Reject normal internal roots and ambiguous analyze-only intersections."""

        access.verify_root_identity()
        self.verify_identities()
        intersections = tuple(
            entry
            for entry in self.entries
            if path_trees_intersect(access.root, entry.canonical_path)
        )
        if access.mode == "normal":
            nested = tuple(
                entry
                for entry in intersections
                if _is_same_or_descendant(access.root, entry.canonical_path)
            )
            if nested:
                raise InternalPathProtectionError(f"normal corpus root is internal: {access.root}")
            access.verify_root_identity()
            self.verify_identities()
            return
        if not intersections:
            access.verify_root_identity()
            self.verify_identities()
            return
        repository = self._entry("repository")
        access_identity = (
            access.root_device_id,
            access.root_file_id,
            access.root_birthtime_ns,
        )
        repository_identity = (
            repository.device_id,
            repository.file_id,
            repository.birthtime_ns,
        )
        if (
            repository.exists
            and _path_key(access.root) == _path_key(repository.canonical_path)
            and access_identity == repository_identity
        ):
            access.verify_root_identity()
            self.verify_identities()
            return
        raise InternalPathProtectionError(
            f"analyze-only root intersects a non-repository internal path: {access.root}"
        )

    def inventory_exclusion_roots(
        self,
        access: CorpusAccessPolicy,
    ) -> tuple[Path, ...]:
        """Return minimal internal subtrees beneath one allowed corpus root."""

        self.validate_corpus_access(access)
        candidates = [
            entry.canonical_path
            for entry in self.entries
            if entry.kind == "tree"
            and _is_same_or_descendant(entry.canonical_path, access.root)
            and _path_key(entry.canonical_path) != _path_key(access.root)
        ]
        retained: list[Path] = []
        for candidate in sorted(candidates, key=lambda path: len(path.parts)):
            if any(_is_same_or_descendant(candidate, root) for root in retained):
                continue
            retained.append(candidate)
        self.verify_identities()
        return tuple(retained)

    def require_mutation_paths_allowed(
        self,
        *paths: str | os.PathLike[str] | None,
    ) -> None:
        """Reject any lexical or physical intersection with an internal path."""

        self.verify_identities()
        for raw_path in paths:
            if raw_path is None:
                continue
            try:
                candidate = _absolute_normalized(raw_path)
                blocked = next(
                    (
                        entry
                        for entry in self.entries
                        if path_trees_intersect(candidate, entry.canonical_path)
                    ),
                    None,
                )
                if blocked is None and os.path.lexists(candidate):
                    blocked = next(
                        (entry for entry in self.entries if entry.matches_file_identity(candidate)),
                        None,
                    )
            except (OSError, ValueError) as exc:
                raise InternalPathProtectionError(
                    "internal mutation boundary cannot be verified: "
                    f"{os.fspath(raw_path)}: {type(exc).__name__}"
                ) from exc
            if blocked is not None:
                raise InternalPathProtectionError(
                    f"mutation path intersects internal {blocked.role}: {candidate}"
                )
        self.verify_identities()

    def _entry(self, role: InternalPathRole) -> InternalPathIdentity:
        for entry in self.entries:
            if entry.role == role:
                return entry
        raise ValueError(f"internal path policy has no {role!r} entry")


def _validate_policy_topology(
    entries: tuple[InternalPathIdentity, ...],
) -> None:
    by_role = {entry.role: entry for entry in entries}
    if set(by_role) != set(_REQUIRED_ROLE_KINDS) or len(by_role) != len(entries):
        raise ValueError("internal path policy must contain every role exactly once")
    if any(
        by_role[role].kind != expected_kind for role, expected_kind in _REQUIRED_ROLE_KINDS.items()
    ):
        raise ValueError("internal path policy role kind is invalid")

    repository = by_role["repository"].canonical_path
    runtime = by_role["runtime"].canonical_path
    application_data = by_role["application_data"].canonical_path
    self_analysis = by_role["self_analysis"].canonical_path
    launcher = by_role["launcher"].canonical_path
    if any(
        path_trees_intersect(repository, other)
        for other in (runtime, application_data, self_analysis, launcher)
    ):
        raise ValueError("repository must be disjoint from every other internal path")
    if path_trees_intersect(runtime, application_data):
        raise ValueError("runtime and application data must be disjoint")
    if not _is_strict_descendant(self_analysis, application_data):
        raise ValueError("self-analysis must be below application data")
    if not _is_strict_descendant(launcher, runtime):
        raise ValueError("launcher must be below runtime")


def _is_strict_descendant(path: Path, root: Path) -> bool:
    return _path_key(path) != _path_key(root) and _is_same_or_descendant(path, root)


def _manifest_payload(entries: tuple[InternalPathIdentity, ...]) -> bytes:
    payload = json.dumps(
        {
            "entries": [entry.manifest_entry() for entry in entries],
            "version": INTERNAL_PATHS_POLICY_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > MAX_INTERNAL_PATH_MANIFEST_BYTES:
        raise ValueError("internal path policy manifest is too large")
    return payload


def effective_inventory_policy_signature(
    inventory_exclusion_signature: str,
    internal_paths_signature: str,
    protected_content_signature: str | None = None,
) -> str:
    """Bind discovery rules to internal and, when supplied, protected paths.

    Two-argument callers retain the exact v1 payload and digest.  Supplying the
    protected-content signature opts into the three-layer v2 durable contract.
    """

    signatures = [
        ("inventory exclusion signature", inventory_exclusion_signature),
        ("internal paths signature", internal_paths_signature),
    ]
    if protected_content_signature is not None:
        signatures.append(("protected content signature", protected_content_signature))
    for label, value in signatures:
        if not value or value.strip() != value or len(value.encode("utf-8")) > 4096:
            raise ValueError(f"{label} must be trimmed and bounded")
    version = (
        EFFECTIVE_INVENTORY_POLICY_VERSION
        if protected_content_signature is None
        else EFFECTIVE_INVENTORY_POLICY_VERSION_V2
    )
    manifest = {
        "internal_paths_signature": internal_paths_signature,
        "inventory_exclusion_signature": inventory_exclusion_signature,
        "version": version,
    }
    if protected_content_signature is not None:
        manifest["protected_content_signature"] = protected_content_signature
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{version}:xxh3_128:{xxhash.xxh3_128_hexdigest(payload)}"


# endregion [02]


# region [03] Canonical per-user factory


def canonical_internal_paths_policy() -> InternalPathsPolicy:
    """Capture the canonical per-user layout, including reserved missing paths."""

    return InternalPathsPolicy.capture(
        (
            InternalPathSpec("repository", "tree", source_repository_directory()),
            InternalPathSpec("runtime", "tree", program_installation_directory()),
            InternalPathSpec(
                "application_data",
                "tree",
                local_application_data_directory(),
            ),
            InternalPathSpec(
                "self_analysis",
                "tree",
                self_analysis_data_directory(),
            ),
            InternalPathSpec("launcher", "file", stable_launcher_path()),
        )
    )


__all__ = [
    "EFFECTIVE_INVENTORY_POLICY_VERSION",
    "EFFECTIVE_INVENTORY_POLICY_VERSION_V2",
    "INTERNAL_PATHS_POLICY_VERSION",
    "INTERNAL_PATH_PROTECTION_REASON",
    "InternalPathIdentity",
    "InternalPathKind",
    "InternalPathProtectionError",
    "InternalPathRole",
    "InternalPathSpec",
    "InternalPathsPolicy",
    "canonical_internal_paths_policy",
    "effective_inventory_policy_signature",
]

# endregion [03]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.internal_paths")
