"""Identity-bound filesystem boundaries shared by inventory consumers.
# region [00] Contexto del módulo
# Módulo: neocortex/inventory_boundary.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


This module is deliberately independent from orchestration and state
repositories.  It captures the exact corpus, state, exclusion, and internal
path policy that must agree before inventory reuse or filesystem mutation.
"""

# region [01] Dependencias del módulo
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

from neocortex.deduplication import InventoryExclusionPolicy
from neocortex.deduplication.inventory.index import (
    DEFAULT_EXCLUDED_PATHS,
    DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
    DEFAULT_GENERATED_DIRECTORY_NAMES,
    DEFAULT_GENERATED_DIRECTORY_PREFIXES,
    DEFAULT_GENERATED_FILE_SUFFIXES,
)

from neocortex.runtime.config.app_paths import default_generated_artifact_directories

from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    _absolute_normalized,
    _path_key,
    _physical_normalized,
    path_trees_intersect,
)
from neocortex.safety.internal_paths import (
    InternalPathRole,
    InternalPathsPolicy,
    canonical_internal_paths_policy,
    effective_inventory_policy_signature,
)
from neocortex.safety.protected_content import (
    ProtectedContentError,
    ProtectedContentPolicy,
    canonical_protected_content_policy,
)
# endregion [01]

# region [02] Implementación


_RESTRICTED_CODEX_DIRECTORY_NAMES = (
    ".sandbox",
    ".sandbox-bin",
    ".sandbox-secrets",
    ".tmp",
    "cache",
    "computer-use",
    "node_repl",
    "pets",
    "plugins",
    "process_manager",
    "sqlite",
    "tmp",
    "vendor_imports",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "logs",
)
_RESTRICTED_CODEX_FILE_NAMES = (
    "auth.json",
    "config.toml",
    "cap_sid",
    "installation_id",
    "models_cache.json",
    "session_index.jsonl",
    ".codex-global-state.json",
    ".codex-global-state.json.bak",
    ".sandbox_migration",
    ".env",
)
_RESTRICTED_CODEX_FILE_SUFFIXES = (
    ".sqlite",
    ".db",
    ".wal",
    ".shm",
    ".key",
    ".pem",
)
_AUTHORIZED_INTERNAL_STATE_ROLES: tuple[InternalPathRole, ...] = (
    "application_data",
    "self_analysis",
)
_FORBIDDEN_INTERNAL_STATE_ROLES: tuple[InternalPathRole, ...] = (
    "repository",
    "runtime",
    "launcher",
)
_CANONICAL_STATE_DATABASE_NAMES = (
    "framework.sqlite3",
    "dedup.sqlite3",
    "document_catalog.sqlite3",
    "pdf.sqlite3",
    "docx.sqlite3",
    "office.sqlite3",
    "audio.sqlite3",
    "image.sqlite3",
    "code.sqlite3",
    "semantic.sqlite3",
)


@dataclass(frozen=True, slots=True)
class NormalInventoryBoundary:
    """One identity-bound inventory boundary shared by normal consumers."""

    access_policy: CorpusAccessPolicy
    state_policy: CorpusAccessPolicy
    internal_paths_policy: InternalPathsPolicy
    protected_content_policy: ProtectedContentPolicy
    exclusion_policy: InventoryExclusionPolicy
    effective_signature: str

    def verify(self) -> None:
        self.access_policy.verify_root_identity()
        if self.state_policy.mode != "normal" or None in (
            self.state_policy.root_device_id,
            self.state_policy.root_file_id,
            self.state_policy.root_birthtime_ns,
        ):
            raise ValueError("state access policy requires a complete normal identity")
        self.state_policy.verify_root_identity()
        self.internal_paths_policy.validate_corpus_access(self.access_policy)
        self.protected_content_policy.validate_corpus_access(self.access_policy)
        validate_authorized_state_path(
            self.state_policy.root,
            internal_paths_policy=self.internal_paths_policy,
            protected_content_policy=self.protected_content_policy,
            mutation_paths=canonical_state_mutation_paths(
                self.state_policy.root,
            ),
        )
        expected_signature = effective_inventory_policy_signature(
            self.exclusion_policy.signature,
            self.internal_paths_policy.signature,
            self.protected_content_policy.signature,
        )
        if self.effective_signature != expected_signature:
            raise ValueError("normal inventory boundary signature is inconsistent")


@dataclass(frozen=True, slots=True)
class AuthorizedStateDirectory:
    """Exact state path and the post-initialization internal identity fence."""

    state_policy: CorpusAccessPolicy
    internal_paths_policy: InternalPathsPolicy

    @property
    def path(self) -> Path:
        return self.state_policy.root


def _same_or_descendant(path: Path, root: Path) -> bool:
    path_key = os.path.normcase(os.fspath(path))
    root_key = os.path.normcase(os.fspath(root))
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _strict_descendant(path: Path, root: Path) -> bool:
    return os.path.normcase(os.fspath(path)) != os.path.normcase(
        os.fspath(root)
    ) and _same_or_descendant(path, root)


def _normalized_trees_intersect(
    candidate: Path,
    candidate_physical: Path,
    reserved: Path,
) -> bool:
    return (
        _same_or_descendant(candidate, reserved)
        or _same_or_descendant(reserved, candidate)
        or _same_or_descendant(candidate_physical, reserved)
        or _same_or_descendant(reserved, candidate_physical)
    )


def state_sqlite_mutation_paths(database: Path) -> tuple[Path, ...]:
    """Return one SQLite target and the sidecars it may create or rewrite."""

    return (
        database,
        Path(f"{database}-journal"),
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    )


def canonical_state_mutation_paths(
    state_directory: Path,
) -> tuple[Path, ...]:
    """Return the bounded canonical lock and SQLite mutation surface."""

    return (
        state_directory / "framework.lock",
        *(
            target
            for database_name in _CANONICAL_STATE_DATABASE_NAMES
            for target in state_sqlite_mutation_paths(state_directory / database_name)
        ),
    )


def validate_authorized_state_path(
    state_directory: Path,
    *,
    internal_paths_policy: InternalPathsPolicy,
    protected_content_policy: ProtectedContentPolicy,
    mutation_paths: tuple[Path, ...] = (),
) -> Path:
    """Fail closed unless state and its explicit write targets are authorized.

    Canonical application-data and self-analysis trees may sit below a broader
    protected container (the user profile AppData tree in the default policy).
    That exception never covers a protected child, a protected file, or a
    matching protected file identity inside the authorized internal tree.
    """

    internal_paths_policy.verify_identities()
    protected_content_policy.verify_identities()
    try:
        requested = _absolute_normalized(state_directory)
        physical = _physical_normalized(requested)
    except (OSError, ValueError) as exc:
        raise ValueError("framework state boundary cannot be verified") from exc
    if _path_key(requested) != _path_key(physical):
        raise ValueError("framework state_directory cannot use an alias or reparse path")

    entries_by_role = {entry.role: entry for entry in internal_paths_policy.entries}
    authorized_roots = tuple(
        entries_by_role[role].canonical_path
        for role in _AUTHORIZED_INTERNAL_STATE_ROLES
        if _same_or_descendant(
            requested,
            entries_by_role[role].canonical_path,
        )
    )

    for raw_candidate in (requested, *mutation_paths):
        try:
            candidate = _absolute_normalized(raw_candidate)
            candidate_physical = _physical_normalized(candidate)
        except (OSError, ValueError) as exc:
            raise ValueError("framework state mutation boundary cannot be verified") from exc
        if _path_key(candidate) != _path_key(candidate_physical):
            raise ValueError("framework state mutation path cannot use an alias or reparse path")

        for internal_entry in internal_paths_policy.entries:
            try:
                intersects = _normalized_trees_intersect(
                    candidate,
                    candidate_physical,
                    internal_entry.canonical_path,
                )
                identity_match = bool(
                    os.path.lexists(candidate) and internal_entry.matches_file_identity(candidate)
                )
            except (OSError, ValueError) as exc:
                raise ValueError("framework state/internal boundary cannot be verified") from exc
            if not intersects and not identity_match:
                continue
            allowed_internal = internal_entry.role in _AUTHORIZED_INTERNAL_STATE_ROLES and any(
                _same_or_descendant(candidate, root) for root in authorized_roots
            )
            if internal_entry.role in _FORBIDDEN_INTERNAL_STATE_ROLES:
                raise ValueError("framework state_directory intersects protected code/runtime")
            if not allowed_internal:
                raise ValueError("framework state_directory is not in an authorized state tree")

        for protected_entry in protected_content_policy.entries:
            try:
                intersects = _normalized_trees_intersect(
                    candidate,
                    candidate_physical,
                    protected_entry.canonical_path,
                )
                identity_match = bool(
                    os.path.lexists(candidate) and protected_entry.matches_file_identity(candidate)
                )
            except (OSError, ValueError) as exc:
                raise ProtectedContentError(
                    "protected state mutation boundary cannot be verified: "
                    f"{candidate}: {type(exc).__name__}"
                ) from exc
            if not intersects and not identity_match:
                continue
            allowed_container = (
                protected_entry.kind == "tree"
                and any(
                    _same_or_descendant(root, protected_entry.canonical_path)
                    for root in authorized_roots
                )
                and _same_or_descendant(
                    candidate,
                    protected_entry.canonical_path,
                )
            )
            if not allowed_container:
                raise ProtectedContentError(
                    "framework state mutation path intersects protected content "
                    f"{protected_entry.role}: {candidate}"
                )

    internal_paths_policy.verify_identities()
    protected_content_policy.verify_identities()
    return requested


def _protected_inventory_restrictions(
    policy: ProtectedContentPolicy,
) -> tuple[tuple[Path, ...], tuple[Path, ...], tuple[Path, ...]]:
    restricted_roots = tuple(
        entry.canonical_path
        for entry in policy.entries
        if entry.kind == "tree"
        and entry.disposition == "exclude"
        and any(
            candidate.disposition == "analyze_read_only"
            and _strict_descendant(
                candidate.canonical_path,
                entry.canonical_path,
            )
            for candidate in policy.entries
        )
    )
    allowed_trees = tuple(
        entry.canonical_path
        for entry in policy.entries
        if entry.kind == "tree"
        and entry.disposition == "analyze_read_only"
        and any(_strict_descendant(entry.canonical_path, root) for root in restricted_roots)
    )
    allowed_files = tuple(
        entry.canonical_path
        for entry in policy.entries
        if entry.kind == "file"
        and entry.disposition == "analyze_read_only"
        and any(_strict_descendant(entry.canonical_path, root) for root in restricted_roots)
    )
    return restricted_roots, allowed_trees, allowed_files


def build_normal_inventory_boundary(
    root: Path,
    state_directory: Path,
    *,
    access_policy: CorpusAccessPolicy | None = None,
    state_policy: CorpusAccessPolicy | None = None,
    internal_paths_policy: InternalPathsPolicy | None = None,
    protected_content_policy: ProtectedContentPolicy | None = None,
) -> NormalInventoryBoundary:
    """Capture the canonical normal boundary after authorized layout setup."""

    if access_policy is None:
        access_policy = CorpusAccessPolicy.capture("normal", root)
    elif access_policy.mode != "normal" or os.path.normcase(
        os.fspath(access_policy.root)
    ) != os.path.normcase(os.path.abspath(os.fspath(root))):
        raise ValueError("normal inventory access policy does not match its root")
    access_policy.verify_root_identity()
    if internal_paths_policy is None:
        internal_paths_policy = canonical_internal_paths_policy()
    internal_paths_policy.validate_corpus_access(access_policy)
    if protected_content_policy is None:
        protected_content_policy = canonical_protected_content_policy()
    protected_content_policy.validate_corpus_access(access_policy)
    state_path = validate_authorized_state_path(
        state_directory,
        internal_paths_policy=internal_paths_policy,
        protected_content_policy=protected_content_policy,
        mutation_paths=canonical_state_mutation_paths(state_directory),
    )
    if state_policy is None:
        state_policy = CorpusAccessPolicy.capture("normal", state_path)
    elif state_policy.mode != "normal" or os.path.normcase(
        os.fspath(state_policy.root)
    ) != os.path.normcase(os.fspath(state_path)):
        raise ValueError("state access policy does not match state_directory")
    state_policy.verify_root_identity()
    if _same_or_descendant(access_policy.root, state_path):
        raise ValueError("framework state_directory cannot equal or contain the inventory root")
    restricted_roots, allowed_trees, allowed_files = _protected_inventory_restrictions(
        protected_content_policy
    )
    exclusion_policy = InventoryExclusionPolicy.compile(
        (
            *DEFAULT_EXCLUDED_PATHS,
            *default_generated_artifact_directories(),
            state_path,
            *internal_paths_policy.inventory_exclusion_roots(access_policy),
            *protected_content_policy.inventory_exclusion_roots(access_policy),
        ),
        directory_names=DEFAULT_GENERATED_DIRECTORY_NAMES,
        directory_prefixes=DEFAULT_GENERATED_DIRECTORY_PREFIXES,
        directory_fragments=DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
        file_suffixes=DEFAULT_GENERATED_FILE_SUFFIXES,
        restricted_roots=restricted_roots,
        restricted_allowed_trees=allowed_trees,
        restricted_allowed_files=allowed_files,
        restricted_directory_names=_RESTRICTED_CODEX_DIRECTORY_NAMES,
        restricted_file_names=_RESTRICTED_CODEX_FILE_NAMES,
        restricted_file_suffixes=_RESTRICTED_CODEX_FILE_SUFFIXES,
    )
    effective_signature = effective_inventory_policy_signature(
        exclusion_policy.signature,
        internal_paths_policy.signature,
        protected_content_policy.signature,
    )
    boundary = NormalInventoryBoundary(
        access_policy=access_policy,
        state_policy=state_policy,
        internal_paths_policy=internal_paths_policy,
        protected_content_policy=protected_content_policy,
        exclusion_policy=exclusion_policy,
        effective_signature=effective_signature,
    )
    boundary.verify()
    return boundary


def initialize_authorized_state_directory(
    access_policy: CorpusAccessPolicy,
    state_directory: Path,
    *,
    require_disjoint: bool,
    protected_content_policy: ProtectedContentPolicy | None = None,
) -> AuthorizedStateDirectory:
    """Create only the exact configured state tree behind identity fences."""

    access_policy.verify_root_identity()
    if protected_content_policy is None:
        protected_content_policy = canonical_protected_content_policy()
    protected_content_policy.validate_corpus_access(access_policy)
    before = canonical_internal_paths_policy()
    before.validate_corpus_access(access_policy)
    requested = validate_authorized_state_path(
        state_directory,
        internal_paths_policy=before,
        protected_content_policy=protected_content_policy,
        mutation_paths=canonical_state_mutation_paths(state_directory),
    )
    entries_by_role = {entry.role: entry for entry in before.entries}
    allowed_transition_roles: set[InternalPathRole] = {
        role
        for role in _AUTHORIZED_INTERNAL_STATE_ROLES
        if _same_or_descendant(
            requested,
            entries_by_role[role].canonical_path,
        )
    }
    try:
        intersects = path_trees_intersect(access_policy.root, requested)
    except (OSError, ValueError) as exc:
        access_policy.verify_root_identity()
        raise ValueError("framework root/state boundary cannot be verified") from exc
    if require_disjoint and intersects:
        raise ValueError("self-analysis root and state directory must be disjoint")
    if not requested.exists():
        if intersects and not allowed_transition_roles:
            raise ValueError("framework cannot create a state directory inside the corpus root")
        validate_authorized_state_path(
            requested,
            internal_paths_policy=before,
            protected_content_policy=protected_content_policy,
            mutation_paths=canonical_state_mutation_paths(requested),
        )
        requested.mkdir(parents=True, exist_ok=False)
    observed = CorpusAccessPolicy.capture("normal", requested)
    if _path_key(observed.root) != _path_key(requested):
        raise ValueError("framework state_directory is not canonical")
    after = canonical_internal_paths_policy()
    after_by_role = {entry.role: entry for entry in after.entries}
    for role, prior in entries_by_role.items():
        current = after_by_role[role]
        if prior.exists:
            if current != prior:
                raise ValueError(f"internal path identity changed during state setup: {role}")
        elif current.exists and role not in allowed_transition_roles:
            raise ValueError(f"unauthorized internal path appeared during state setup: {role}")
        elif not current.exists and current != prior:
            raise ValueError(f"internal path reservation changed during state setup: {role}")
    after.verify_identities()
    access_policy.verify_root_identity()
    protected_content_policy.validate_corpus_access(access_policy)
    validate_authorized_state_path(
        observed.root,
        internal_paths_policy=after,
        protected_content_policy=protected_content_policy,
        mutation_paths=canonical_state_mutation_paths(observed.root),
    )
    return AuthorizedStateDirectory(observed, after)


__all__ = [
    "AuthorizedStateDirectory",
    "NormalInventoryBoundary",
    "_same_or_descendant",
    "build_normal_inventory_boundary",
    "canonical_state_mutation_paths",
    "initialize_authorized_state_directory",
    "state_sqlite_mutation_paths",
    "validate_authorized_state_path",
]
# endregion [02]
