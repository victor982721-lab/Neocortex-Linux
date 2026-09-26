"""Fail-closed access contracts for corpus roots.
# region [00] Contexto del módulo
# Módulo: neocortex/corpus_access.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


State databases and other NeoCortex-managed artifacts are deliberately outside
this boundary.  The policy describes only the corpus tree observed by a run;
``analyze_only`` never authorizes a filesystem mutation.
"""

# region [01] Dependencias del módulo
from __future__ import annotations
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from neocortex.platform.policy import UNAVAILABLE_BIRTHTIME_NS, stat_birthtime_ns
# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:

    class InternalPathsPolicy(Protocol):
        """Structural mutation boundary supplied by the internal-path policy."""

        def validate_corpus_access(self, access: CorpusAccessPolicy) -> None: ...

        def require_mutation_paths_allowed(
            self,
            *paths: str | os.PathLike[str] | None,
        ) -> None: ...

    class _ProtectedPathIdentity(Protocol):
        @property
        def canonical_path(self) -> Path: ...

    class ProtectedContentPolicy(Protocol):
        """Structural boundary supplied by the protected-content policy."""

        @property
        def entries(self) -> tuple[_ProtectedPathIdentity, ...]: ...

        def run_mutation_reason(
            self,
            access: CorpusAccessPolicy,
        ) -> str | None: ...

        def require_run_mutation_allowed(
            self,
            access: CorpusAccessPolicy,
        ) -> None: ...

        def require_mutation_paths_allowed(
            self,
            *paths: str | os.PathLike[str] | None,
        ) -> None: ...

        def mutation_path_protection_reasons(
            self,
            *paths: str | os.PathLike[str] | None,
        ) -> tuple[str | None, ...]: ...


CorpusAccessMode = Literal["normal", "analyze_only"]
PROTECTED_ANALYSIS_REASON = "protected_analysis_root"
_REPARSE_POINT_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400))
_WIN32_EXTENDED_PREFIX = "\\\\?\\"
_WIN32_DEVICE_PREFIX = "\\\\.\\"
_NT_OBJECT_PREFIXES = ("\\??\\", "\\\\??\\")
_DOS_DEVICE_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


class ProtectedAnalysisRootError(PermissionError):
    """A requested mutation intersects an analyze-only corpus root."""

    reason_code = PROTECTED_ANALYSIS_REASON

    def __init__(self, detail: str = "corpus mutation is forbidden") -> None:
        self.detail = detail
        super().__init__(f"{self.reason_code}: {detail}")


def _birthtime_ns(metadata: os.stat_result) -> int:
    return stat_birthtime_ns(metadata)


def _has_reparse_semantics(path: Path, metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _reject_non_equivalent_extended_tail(tail: str) -> None:
    trimmed = tail[:-1] if tail.endswith("\\") else tail
    if not trimmed:
        return
    for component in trimmed.split("\\"):
        device_stem = component.split(".", 1)[0].casefold()
        if (
            not component
            or component in {".", ".."}
            or component.endswith((" ", "."))
            or ":" in component
            or device_stem in _DOS_DEVICE_NAMES
        ):
            raise ValueError("extended Windows path is not unambiguously Win32-equivalent")


def _equivalent_win32_path(path: str | os.PathLike[str]) -> str:
    """Map safe extended aliases and reject non-equivalent device namespaces."""

    raw = os.fspath(path)
    if "\x00" in raw:
        raise ValueError("path contains NUL")
    windows_path = raw.replace("/", "\\")
    folded = windows_path.casefold()
    if folded.startswith(_WIN32_EXTENDED_PREFIX.casefold()):
        remainder = windows_path[len(_WIN32_EXTENDED_PREFIX) :]
        if remainder.casefold().startswith("unc\\"):
            unc_tail = remainder[4:]
            components = unc_tail.split("\\")
            if len(components) < 2 or not components[0] or not components[1]:
                raise ValueError("extended UNC path has no server/share root")
            _reject_non_equivalent_extended_tail("\\".join(components[2:]))
            return "\\\\" + unc_tail
        if (
            len(remainder) >= 3
            and remainder[0].isascii()
            and remainder[0].isalpha()
            and remainder[1:3] == ":\\"
        ):
            _reject_non_equivalent_extended_tail(remainder[3:])
            return remainder
        raise ValueError("unsupported Windows namespace path")
    if folded.startswith(_WIN32_DEVICE_PREFIX.casefold()) or any(
        folded.startswith(prefix.casefold()) for prefix in _NT_OBJECT_PREFIXES
    ):
        raise ValueError("unsupported Windows namespace path")
    return raw


def _absolute_normalized(path: str | os.PathLike[str]) -> Path:
    equivalent = _equivalent_win32_path(path)
    return Path(os.path.abspath(os.path.normpath(equivalent)))


def _path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.fspath(_absolute_normalized(path)))


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _physical_normalized(path: str | os.PathLike[str]) -> Path:
    """Resolve the existing prefix and preserve a normalized missing suffix."""

    current = _absolute_normalized(path)
    missing_tail: list[str] = []
    while True:
        try:
            os.lstat(current)
            break
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                raise
            missing_tail.append(current.name)
            current = parent
    physical = _absolute_normalized(os.path.realpath(current))
    os.stat(physical, follow_symlinks=False)
    for component in reversed(missing_tail):
        physical /= component
    return _absolute_normalized(physical)


def path_trees_intersect(
    left: str | os.PathLike[str],
    right: str | os.PathLike[str],
) -> bool:
    """Return whether two lexical or physical path trees contain one another."""

    left_path = _absolute_normalized(left)
    right_path = _absolute_normalized(right)
    if _is_same_or_descendant(left_path, right_path) or _is_same_or_descendant(
        right_path, left_path
    ):
        return True
    physical_left = _physical_normalized(left_path)
    physical_right = _physical_normalized(right_path)
    return _is_same_or_descendant(physical_left, physical_right) or _is_same_or_descendant(
        physical_right, physical_left
    )


@dataclass(frozen=True, slots=True)
class CorpusAccessPolicy:
    """Canonical root identity and the permitted corpus access mode."""

    mode: CorpusAccessMode
    root: Path
    root_device_id: int | None
    root_file_id: int | None
    root_birthtime_ns: int | None

    def __post_init__(self) -> None:
        if self.mode not in {"normal", "analyze_only"}:
            raise ValueError(f"unsupported corpus access mode: {self.mode!r}")
        normalized = _absolute_normalized(self.root)
        if _path_key(normalized) != _path_key(self.root):
            raise ValueError("corpus access root must be canonical and absolute")
        object.__setattr__(self, "root", normalized)
        identity = (self.root_device_id, self.root_file_id)
        if any(value is not None and (type(value) is not int or value < 0) for value in identity):
            raise ValueError("corpus root identity values must be non-negative integers")
        if self.root_birthtime_ns is not None and (
            type(self.root_birthtime_ns) is not int
            or self.root_birthtime_ns < UNAVAILABLE_BIRTHTIME_NS
        ):
            raise ValueError("corpus root birthtime must be -1 or a non-negative integer")
        if self.mode == "analyze_only" and any(value is None for value in identity):
            raise ValueError("analyze-only corpus policy requires a complete root identity")

    @classmethod
    def capture(
        cls,
        mode: CorpusAccessMode,
        root: str | os.PathLike[str],
    ) -> CorpusAccessPolicy:
        """Capture a real directory without following a reparse root."""

        requested = _absolute_normalized(root)
        metadata = os.stat(requested, follow_symlinks=False)
        if _has_reparse_semantics(requested, metadata):
            raise ValueError("corpus access root cannot be a symlink or reparse point")
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(os.fspath(requested))
        canonical = _absolute_normalized(os.path.realpath(requested))
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
            raise ValueError("corpus access root resolution is ambiguous")
        return cls(mode, canonical, *canonical_identity)

    @classmethod
    def from_storage(
        cls,
        mode: str,
        root: str | os.PathLike[str],
        root_device_id_hex: str | None,
        root_file_id_hex: str | None,
        root_birthtime_ns: int | None,
    ) -> CorpusAccessPolicy:
        """Rehydrate one policy without trusting the current filesystem."""

        if mode not in {"normal", "analyze_only"}:
            raise ValueError(f"unsupported persisted corpus access mode: {mode!r}")
        try:
            device_id = None if root_device_id_hex is None else int(root_device_id_hex, 16)
            file_id = None if root_file_id_hex is None else int(root_file_id_hex, 16)
        except ValueError as exc:
            raise ValueError("persisted corpus root identity is malformed") from exc
        return cls(
            cast(CorpusAccessMode, mode),
            _absolute_normalized(root),
            device_id,
            file_id,
            None if root_birthtime_ns is None else int(root_birthtime_ns),
        )

    @property
    def root_device_id_hex(self) -> str | None:
        return None if self.root_device_id is None else f"{self.root_device_id:x}"

    @property
    def root_file_id_hex(self) -> str | None:
        return None if self.root_file_id is None else f"{self.root_file_id:x}"

    def verify_root_identity(self) -> None:
        """Fail closed if the root disappeared, was replaced, or became reparse."""

        if None in (
            self.root_device_id,
            self.root_file_id,
            self.root_birthtime_ns,
        ):
            # A normal run is not allowed to fall back to path-only mutation
            # when a legacy or tampered record lacks its physical identity.
            # ``-1`` is the explicit Linux birth-time sentinel; ``None`` is
            # uncertainty and therefore must abstain in every access mode.
            raise ProtectedAnalysisRootError("corpus root identity is incomplete")
        try:
            metadata = os.stat(self.root, follow_symlinks=False)
        except OSError as exc:
            raise ProtectedAnalysisRootError(
                f"protected root identity cannot be verified: {type(exc).__name__}"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode) or _has_reparse_semantics(self.root, metadata):
            raise ProtectedAnalysisRootError(
                "protected root is no longer a real non-reparse directory"
            )
        observed = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            _birthtime_ns(metadata),
        )
        expected = (
            self.root_device_id,
            self.root_file_id,
            self.root_birthtime_ns,
        )
        if observed != expected:
            raise ProtectedAnalysisRootError("protected root identity changed")


@dataclass(frozen=True, slots=True)
class CorpusMutationGuard:
    """Apply one persisted corpus policy at planning and syscall boundaries."""

    policy: CorpusAccessPolicy
    internal_paths_policy: InternalPathsPolicy
    protected_content_policy: ProtectedContentPolicy | None = None

    @property
    def reason_code(self) -> str | None:
        if self.policy.mode == "analyze_only":
            return PROTECTED_ANALYSIS_REASON
        if self.protected_content_policy is None:
            return None
        return self.protected_content_policy.run_mutation_reason(self.policy)

    def reject_run_mutation(self) -> None:
        """Reject any action originating in a globally read-only run."""

        # Revalidate the physical root even for normal runs.  Previously a
        # normal policy with an incomplete identity returned here without any
        # root check, allowing callers to cross the mutation boundary using
        # path-only evidence.
        self.policy.verify_root_identity()
        if self.policy.mode == "analyze_only":
            raise ProtectedAnalysisRootError()
        if self.protected_content_policy is not None:
            self.protected_content_policy.require_run_mutation_allowed(self.policy)

    def require_paths_allowed(
        self,
        *paths: str | os.PathLike[str] | None,
    ) -> None:
        """Reject internal, protected-content and analyze-only intersections."""

        self.policy.verify_root_identity()
        self.internal_paths_policy.require_mutation_paths_allowed(*paths)
        if self.protected_content_policy is not None:
            self.protected_content_policy.require_mutation_paths_allowed(*paths)
        if self.policy.mode == "analyze_only":
            self._require_analyze_only_paths_allowed(paths)
        self.policy.verify_root_identity()

    def mutation_path_protection_reasons(
        self,
        *paths: str | os.PathLike[str] | None,
    ) -> tuple[str | None, ...]:
        """Classify one path batch while revalidating each policy only once."""

        self.policy.verify_root_identity()
        self.internal_paths_policy.require_mutation_paths_allowed(*paths)
        protected_reasons: tuple[str | None, ...]
        if self.protected_content_policy is None:
            protected_reasons = (None,) * len(paths)
        elif self.policy.mode == "analyze_only":
            self.protected_content_policy.require_mutation_paths_allowed(*paths)
            protected_reasons = (None,) * len(paths)
        else:
            protected_reasons = self.protected_content_policy.mutation_path_protection_reasons(
                *paths
            )

        if self.policy.mode != "analyze_only":
            self.policy.verify_root_identity()
            return protected_reasons
        self._require_analyze_only_paths_allowed(paths)
        self.policy.verify_root_identity()
        return protected_reasons

    def _require_analyze_only_paths_allowed(
        self,
        paths: tuple[str | os.PathLike[str] | None, ...],
    ) -> None:
        """Fail closed for every mutation intersecting an analyze-only root."""

        root = self.policy.root
        self.policy.verify_root_identity()
        for raw_path in paths:
            if raw_path is None:
                continue
            try:
                candidate = _absolute_normalized(raw_path)
                intersects = path_trees_intersect(candidate, root)
            except (OSError, ValueError) as exc:
                raise ProtectedAnalysisRootError(
                    "mutation path boundary cannot be verified: "
                    f"{os.fspath(raw_path)}: {type(exc).__name__}"
                ) from exc
            if intersects:
                raise ProtectedAnalysisRootError(
                    f"mutation path intersects protected root: {candidate}"
                )
        self.policy.verify_root_identity()


__all__ = [
    "PROTECTED_ANALYSIS_REASON",
    "CorpusAccessMode",
    "CorpusAccessPolicy",
    "CorpusMutationGuard",
    "ProtectedAnalysisRootError",
    "path_trees_intersect",
]
# endregion [02]
