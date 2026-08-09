"""Identity-bound policy for user content that NeoCortex must never mutate.

The policy is deliberately independent from discovery.  ``exclude`` entries are
pruned when they are below a broader corpus root, while
``analyze_read_only`` entries may be selected explicitly and observed.  Every
entry, regardless of disposition, remains a symmetric mutation boundary.
"""

from __future__ import annotations

# region [01] Contracts and physical identities

import ctypes
import json
import os
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Literal

import xxhash
from neocortex.platform_policy import UNAVAILABLE_BIRTHTIME_NS

from .corpus_access import (
    CorpusAccessPolicy,
    ProtectedAnalysisRootError,
    _absolute_normalized,
    _birthtime_ns,
    _has_reparse_semantics,
    _is_same_or_descendant,
    _path_key,
    _physical_normalized,
    path_trees_intersect,
)


ProtectedDisposition = Literal["analyze_read_only", "exclude"]
ProtectedPathKind = Literal["tree", "file"]

PROTECTED_CONTENT_POLICY_VERSION = "protected-content-policy-v1"
PROTECTED_CONTENT_REASON = "protected_content_root"
MAX_PROTECTED_PATH_ENTRIES = 64
MAX_PROTECTED_PATH_MANIFEST_BYTES = 128 * 1024
MAX_PROTECTED_PATH_ROLE_BYTES = 256


class ProtectedContentError(ProtectedAnalysisRootError):
    """A corpus operation violates one identity-bound content reservation."""

    reason_code = PROTECTED_CONTENT_REASON


@dataclass(frozen=True, slots=True)
class ProtectedPathSpec:
    """One configured content reservation before physical capture."""

    role: str
    kind: ProtectedPathKind
    disposition: ProtectedDisposition
    path: Path


@dataclass(frozen=True, slots=True)
class ProtectedPathIdentity:
    """Lexical reservation and optional physical identity for protected content."""

    role: str
    kind: ProtectedPathKind
    disposition: ProtectedDisposition
    configured_path: Path
    canonical_path: Path
    exists: bool
    device_id: int | None
    file_id: int | None
    birthtime_ns: int | None

    def __post_init__(self) -> None:
        _validate_role(self.role)
        if self.kind not in {"tree", "file"}:
            raise ValueError(f"unsupported protected path kind: {self.kind!r}")
        if self.disposition not in {"analyze_read_only", "exclude"}:
            raise ValueError(f"unsupported protected path disposition: {self.disposition!r}")
        configured = _absolute_normalized(self.configured_path)
        canonical = _absolute_normalized(self.canonical_path)
        if _path_key(configured) != _path_key(self.configured_path):
            raise ValueError("configured protected path must be canonical and absolute")
        if _path_key(canonical) != _path_key(self.canonical_path):
            raise ValueError("physical protected path must be canonical and absolute")
        object.__setattr__(self, "configured_path", configured)
        object.__setattr__(self, "canonical_path", canonical)
        identity = (self.device_id, self.file_id)
        if any(value is not None and (type(value) is not int or value < 0) for value in identity):
            raise ValueError("protected path identity values must be non-negative")
        if self.birthtime_ns is not None and (
            type(self.birthtime_ns) is not int or self.birthtime_ns < UNAVAILABLE_BIRTHTIME_NS
        ):
            raise ValueError("protected path birthtime must be -1 or non-negative")
        complete_identity = (*identity, self.birthtime_ns)
        if self.exists and not all(value is not None for value in complete_identity):
            raise ValueError("protected path existence and identity are inconsistent")
        if not self.exists and any(value is not None for value in complete_identity):
            raise ValueError("protected path existence and identity are inconsistent")

    @classmethod
    def capture(
        cls,
        role: str,
        kind: ProtectedPathKind,
        disposition: ProtectedDisposition,
        path: str | os.PathLike[str],
        *,
        allow_missing: bool = True,
    ) -> ProtectedPathIdentity:
        """Capture an ordinary object or reserve an unambiguous missing path."""

        _validate_role(role)
        if kind not in {"tree", "file"}:
            raise ValueError(f"unsupported protected path kind: {kind!r}")
        if disposition not in {"analyze_read_only", "exclude"}:
            raise ValueError(f"unsupported protected path disposition: {disposition!r}")
        requested = Path(os.fspath(path))
        if not requested.is_absolute():
            raise ValueError(f"protected path must be absolute: {requested}")
        configured = _absolute_normalized(requested)
        path_exists = os.path.lexists(configured)
        physical = _physical_normalized(configured)
        if not path_exists:
            if not allow_missing:
                raise FileNotFoundError(os.fspath(configured))
            if _path_key(configured) != _path_key(physical):
                raise ValueError(
                    f"missing protected path traverses an alias or reparse: {configured}"
                )
            return cls(
                role,
                kind,
                disposition,
                configured,
                physical,
                False,
                None,
                None,
                None,
            )

        metadata = os.stat(configured, follow_symlinks=False)
        if _has_reparse_semantics(configured, metadata):
            raise ValueError(f"protected path cannot be a reparse point: {configured}")
        if kind == "tree" and not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(os.fspath(configured))
        if kind == "file" and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"protected file is not a regular file: {configured}")
        canonical = _absolute_normalized(os.path.realpath(configured))
        if _path_key(configured) != _path_key(canonical):
            raise ValueError(f"protected path traverses an alias or reparse: {configured}")
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
            raise ValueError(f"protected path resolution is ambiguous: {configured}")
        return cls(
            role,
            kind,
            disposition,
            configured,
            canonical,
            True,
            *canonical_identity,
        )

    @property
    def device_id_hex(self) -> str | None:
        return None if self.device_id is None else f"{self.device_id:x}"

    @property
    def file_id_hex(self) -> str | None:
        return None if self.file_id is None else f"{self.file_id:x}"

    def verify_identity(self) -> None:
        """Fail closed if a reservation appeared or an object was replaced."""

        if not self.exists:
            if os.path.lexists(self.configured_path):
                raise ProtectedContentError(
                    f"reserved protected path appeared: {self.configured_path}"
                )
            try:
                physical = _physical_normalized(self.configured_path)
            except (OSError, ValueError) as exc:
                raise ProtectedContentError(
                    f"reserved protected path cannot be verified: {self.configured_path}"
                ) from exc
            if _path_key(physical) != _path_key(self.canonical_path):
                raise ProtectedContentError(
                    f"reserved protected path resolution changed: {self.configured_path}"
                )
            return
        try:
            observed = type(self).capture(
                self.role,
                self.kind,
                self.disposition,
                self.configured_path,
                allow_missing=False,
            )
        except (OSError, ValueError) as exc:
            raise ProtectedContentError(
                f"protected path identity cannot be verified: {self.configured_path}"
            ) from exc
        if observed != self:
            raise ProtectedContentError(f"protected path identity changed: {self.configured_path}")

    def matches_file_identity(self, path: Path) -> bool:
        """Detect a hardlink alias of a protected regular file."""

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
            "disposition": self.disposition,
            "exists": self.exists,
            "file_id_hex": self.file_id_hex,
            "kind": self.kind,
            "role": self.role,
        }


def _validate_role(role: str) -> None:
    if (
        not isinstance(role, str)
        or not role
        or role.strip() != role
        or len(role.encode("utf-8")) > MAX_PROTECTED_PATH_ROLE_BYTES
        or any(ord(character) < 0x20 for character in role)
    ):
        raise ValueError("protected path role must be non-empty, trimmed and bounded")


# endregion [01]


# region [02] Policy, precedence and boundaries


@dataclass(frozen=True, slots=True)
class ProtectedContentPolicy:
    """A deterministic, identity-bound set of immutable content paths."""

    entries: tuple[ProtectedPathIdentity, ...]
    signature: str

    @classmethod
    def capture(
        cls,
        specs: Iterable[ProtectedPathSpec],
    ) -> ProtectedContentPolicy:
        bounded_specs = tuple(islice(specs, MAX_PROTECTED_PATH_ENTRIES + 1))
        if len(bounded_specs) > MAX_PROTECTED_PATH_ENTRIES:
            raise ValueError("protected content policy entry count is invalid")
        captured = tuple(
            sorted(
                (
                    ProtectedPathIdentity.capture(
                        spec.role,
                        spec.kind,
                        spec.disposition,
                        spec.path,
                    )
                    for spec in bounded_specs
                ),
                key=_entry_sort_key,
            )
        )
        _validate_policy_topology(captured)
        return cls(captured, _signature(captured))

    def __post_init__(self) -> None:
        if len(self.entries) > MAX_PROTECTED_PATH_ENTRIES:
            raise ValueError("protected content policy entry count is invalid")
        ordered = tuple(sorted(self.entries, key=_entry_sort_key))
        _validate_policy_topology(ordered)
        if ordered != self.entries or self.signature != _signature(ordered):
            raise ValueError("protected content policy is inconsistent with its signature")

    def manifest(self) -> dict[str, object]:
        return {
            "entries": [entry.manifest_entry() for entry in self.entries],
            "signature": self.signature,
            "version": PROTECTED_CONTENT_POLICY_VERSION,
        }

    def verify_identities(self) -> None:
        for entry in self.entries:
            entry.verify_identity()

    def validate_corpus_access(self, access: CorpusAccessPolicy) -> None:
        """Reject excluded descendants while allowing explicit restricted roots."""

        access.verify_root_identity()
        self.verify_identities()
        selected = self._containing_tree_entry(access.root)
        if selected is not None and selected.disposition == "exclude":
            exact_restricted_container = _path_key(access.root) == _path_key(
                selected.canonical_path
            ) and self._has_analyze_descendant(selected)
            if not exact_restricted_container:
                raise ProtectedContentError(
                    f"corpus root is excluded protected content: {access.root}"
                )
        access.verify_root_identity()
        self.verify_identities()

    def inventory_exclusion_roots(
        self,
        access: CorpusAccessPolicy,
    ) -> tuple[Path, ...]:
        """Return minimal excluded paths strictly below one allowed corpus root.

        Restricted containers with explicit analyze-only descendants are omitted:
        the inventory allowlist must traverse only those descendants and files.
        """

        self.validate_corpus_access(access)
        candidates = [
            entry.canonical_path
            for entry in self.entries
            if entry.disposition == "exclude"
            and not self._has_analyze_descendant(entry)
            and _is_same_or_descendant(entry.canonical_path, access.root)
            and _path_key(entry.canonical_path) != _path_key(access.root)
        ]
        retained: list[Path] = []
        for candidate in sorted(candidates, key=_path_depth_sort_key):
            if any(_is_same_or_descendant(candidate, root) for root in retained):
                continue
            retained.append(candidate)
        access.verify_root_identity()
        self.verify_identities()
        return tuple(retained)

    def require_mutation_paths_allowed(
        self,
        *paths: str | os.PathLike[str] | None,
    ) -> None:
        """Reject every lexical or physical intersection with protected content."""

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
                raise ProtectedContentError(
                    "protected mutation boundary cannot be verified: "
                    f"{os.fspath(raw_path)}: {type(exc).__name__}"
                ) from exc
            if blocked is not None:
                raise ProtectedContentError(
                    f"mutation path intersects protected content {blocked.role}: {candidate}"
                )
        self.verify_identities()

    def run_is_read_only(self, access: CorpusAccessPolicy) -> bool:
        """Return whether the selected root or access mode forbids the whole run."""

        self.validate_corpus_access(access)
        selected = self._containing_tree_entry(access.root)
        restricted_container = (
            selected is not None
            and selected.disposition == "exclude"
            and _path_key(access.root) == _path_key(selected.canonical_path)
            and self._has_analyze_descendant(selected)
        )
        return (
            access.mode == "analyze_only"
            or restricted_container
            or (selected is not None and selected.disposition == "analyze_read_only")
        )

    def _containing_tree_entry(
        self,
        path: Path,
    ) -> ProtectedPathIdentity | None:
        candidates = [
            entry
            for entry in self.entries
            if entry.kind == "tree" and _is_same_or_descendant(path, entry.canonical_path)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda entry: len(entry.canonical_path.parts))

    def _has_analyze_descendant(self, container: ProtectedPathIdentity) -> bool:
        if container.kind != "tree":
            return False
        return any(
            entry.disposition == "analyze_read_only"
            and _path_key(entry.canonical_path) != _path_key(container.canonical_path)
            and _is_same_or_descendant(
                entry.canonical_path,
                container.canonical_path,
            )
            for entry in self.entries
        )


def _entry_sort_key(entry: ProtectedPathIdentity) -> tuple[str, str, str, str]:
    return (
        entry.role,
        entry.kind,
        entry.disposition,
        _path_key(entry.configured_path),
    )


def _path_depth_sort_key(path: Path) -> tuple[int, str]:
    return len(path.parts), _path_key(path)


def _validate_policy_topology(
    entries: tuple[ProtectedPathIdentity, ...],
) -> None:
    roles = [entry.role for entry in entries]
    if len(set(roles)) != len(roles):
        raise ValueError("protected content policy roles must be unique")
    path_keys = [_path_key(entry.configured_path) for entry in entries]
    if len(set(path_keys)) != len(path_keys):
        raise ValueError("protected content policy paths must be unique")
    physical_files: set[tuple[int, int, int]] = set()
    for entry in entries:
        if entry.kind != "file" or not entry.exists:
            continue
        device_id = entry.device_id
        file_id = entry.file_id
        birthtime_ns = entry.birthtime_ns
        if device_id is None or file_id is None or birthtime_ns is None:
            raise ValueError("protected content file identity is incomplete")
        identity = (
            device_id,
            file_id,
            birthtime_ns,
        )
        if identity in physical_files:
            raise ValueError("protected content file aliases are ambiguous")
        physical_files.add(identity)


def _manifest_payload(entries: tuple[ProtectedPathIdentity, ...]) -> bytes:
    payload = json.dumps(
        {
            "entries": [entry.manifest_entry() for entry in entries],
            "version": PROTECTED_CONTENT_POLICY_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > MAX_PROTECTED_PATH_MANIFEST_BYTES:
        raise ValueError("protected content policy manifest is too large")
    return payload


def _signature(entries: tuple[ProtectedPathIdentity, ...]) -> str:
    return (
        f"{PROTECTED_CONTENT_POLICY_VERSION}:xxh3_128:"
        f"{xxhash.xxh3_128_hexdigest(_manifest_payload(entries))}"
    )


# endregion [02]


# region [03] Canonical per-user factory


_DOCUMENTS_FOLDER_ID = uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    )


def _windows_documents_directory() -> Path:
    """Resolve FOLDERID_Documents without assuming the visible folder name."""

    guid = _Guid.from_buffer_copy(_DOCUMENTS_FOLDER_ID.bytes_le)
    allocated = ctypes.c_wchar_p()
    win_dll = ctypes.WinDLL
    shell32 = win_dll("shell32", use_last_error=True)
    ole32 = win_dll("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = (
        ctypes.POINTER(_Guid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole32.CoTaskMemFree.restype = None
    result = int(
        shell32.SHGetKnownFolderPath(
            ctypes.byref(guid),
            0,
            None,
            ctypes.byref(allocated),
        )
    )
    if result != 0:
        code = result & 0xFFFFFFFF
        raise OSError(f"SHGetKnownFolderPath failed with HRESULT 0x{code:08x}")
    try:
        value = allocated.value
        if not value:
            raise OSError("SHGetKnownFolderPath returned an empty Documents path")
        return Path(value)
    finally:
        ole32.CoTaskMemFree(ctypes.cast(allocated, ctypes.c_void_p))


def canonical_protected_content_policy(
    *,
    home: str | os.PathLike[str] | None = None,
    documents: str | os.PathLike[str] | None = None,
) -> ProtectedContentPolicy:
    """Capture the canonical protected content layout for one user profile."""

    profile = Path.home() if home is None else Path(home)
    if documents is None:
        documents_root = (
            _windows_documents_directory() if os.name == "nt" else profile / "Documents"
        )
    else:
        documents_root = Path(documents)
    codex_root = profile / ".codex"
    return ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "documents_codex",
                "tree",
                "analyze_read_only",
                documents_root / "Codex",
            ),
            ProtectedPathSpec("codex_home", "tree", "exclude", codex_root),
            ProtectedPathSpec(
                "codex_sessions",
                "tree",
                "analyze_read_only",
                codex_root / "sessions",
            ),
            ProtectedPathSpec(
                "codex_archived_sessions",
                "tree",
                "analyze_read_only",
                codex_root / "archived_sessions",
            ),
            ProtectedPathSpec(
                "codex_memories",
                "tree",
                "analyze_read_only",
                codex_root / "memories",
            ),
            ProtectedPathSpec(
                "codex_skills",
                "tree",
                "analyze_read_only",
                codex_root / "skills",
            ),
            ProtectedPathSpec(
                "codex_scripts",
                "tree",
                "analyze_read_only",
                codex_root / "scripts",
            ),
            ProtectedPathSpec(
                "codex_hooks",
                "tree",
                "analyze_read_only",
                codex_root / "hooks",
            ),
            ProtectedPathSpec(
                "codex_visualizations",
                "tree",
                "analyze_read_only",
                codex_root / "visualizations",
            ),
            ProtectedPathSpec(
                "codex_agents",
                "file",
                "analyze_read_only",
                codex_root / "AGENTS.md",
            ),
            ProtectedPathSpec(
                "codex_agents_override",
                "file",
                "analyze_read_only",
                codex_root / "AGENTS.override.md",
            ),
            ProtectedPathSpec(
                "application_data",
                "tree",
                "exclude",
                profile / "AppData",
            ),
            ProtectedPathSpec(
                "codex_runtime_cache",
                "tree",
                "exclude",
                profile / ".cache",
            ),
            ProtectedPathSpec(
                "codex_sandbox_denybin",
                "tree",
                "exclude",
                profile / ".sbx-denybin",
            ),
        )
    )


__all__ = [
    "MAX_PROTECTED_PATH_ENTRIES",
    "PROTECTED_CONTENT_POLICY_VERSION",
    "PROTECTED_CONTENT_REASON",
    "ProtectedContentError",
    "ProtectedContentPolicy",
    "ProtectedDisposition",
    "ProtectedPathIdentity",
    "ProtectedPathKind",
    "ProtectedPathSpec",
    "canonical_protected_content_policy",
]

# endregion [03]
