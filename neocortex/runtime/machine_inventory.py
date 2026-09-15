"""Bounded, metadata-only inventory of explicitly selected machine roots.

The machine inventory is a diagnostic surface, not a cleanup or indexing
surface.  It accepts a repeatable set of roots, records only ``lstat``/no-
follow metadata, and keeps one global budget across the federated roots.  The
default roots are deliberately narrow: the current HOME, ``/tmp``, XDG
cache/config directories, and the canonical NeoCortex state/data/corpus
locations.  In particular, the default profile never turns ``/`` into a scan
root.

No file payload, SQLite database, network provider, KIO service, or mutation
operation is used by this module.  A root which is absent is reported as such;
the inventory never creates it merely to make an observation succeed.
"""

from __future__ import annotations

import errno
import os
import stat
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast

from neocortex.platform.policy import default_corpus_root, stat_birthtime_ns


MACHINE_INVENTORY_SCHEMA = "neocortex.machine-inventory/v1"

MachineInventoryStatus = Literal[
    "absent",
    "observed",
    "preserved",
    "blocked",
    "unknown",
    "out_of_profile",
]

MachineInventoryCoverage = Literal["complete", "partial", "blocked"]

MachineInventoryProfile = Literal[
    "observed",
    "preserved",
    "blocked",
    "unknown",
    "out_of_profile",
]

MachineInventoryCategory = Literal[
    "neocortex_state",
    "neocortex_data",
    "neocortex_corpus",
    "tmp",
    "cache",
    "config",
    "home",
    "external",
]

MACHINE_INVENTORY_STATUSES: tuple[MachineInventoryStatus, ...] = (
    "absent",
    "observed",
    "preserved",
    "blocked",
    "unknown",
    "out_of_profile",
)
MACHINE_INVENTORY_CATEGORIES: tuple[MachineInventoryCategory, ...] = (
    "neocortex_state",
    "neocortex_data",
    "neocortex_corpus",
    "tmp",
    "cache",
    "config",
    "home",
    "external",
)

DEFAULT_MACHINE_INVENTORY_MAX_ENTRIES = 10_000
DEFAULT_MACHINE_INVENTORY_MAX_DEPTH = 2
DEFAULT_MACHINE_INVENTORY_MAX_BYTES = 1 << 40
MAX_MACHINE_INVENTORY_ENTRIES = 1_000_000
MAX_MACHINE_INVENTORY_DEPTH = 64
MAX_MACHINE_INVENTORY_BYTES = 1 << 50
MAX_METADATA_LABEL_BYTES = 256
MAX_METADATA_REASON_BYTES = 2_048

# Entry admission is shared between the requested roots, not consumed by the
# first root in enumeration order.  The quota is recomputed before each root
# from the still-unused global entries and the roots still to visit.  Keeping
# the policy name in the payload makes the deterministic redistribution
# observable to callers without changing the meaning of the historical
# ``limits.max_entries`` field (which remains the invocation-wide bound).
MACHINE_INVENTORY_ENTRY_QUOTA_POLICY = "equal_fair_share_v1"

# The three counters below deliberately retain the existing numeric wire
# names.  This companion mapping makes their provenance explicit without
# changing the read-only scanner or pretending that ``observed`` is another
# kind of storage usage.
MACHINE_INVENTORY_BYTE_SEMANTICS: Mapping[str, str] = MappingProxyType(
    {
        "apparent": "metadata st_size: logical bytes reported for regular files and symlinks",
        "allocated": "metadata st_blocks * 512: filesystem-allocated bytes reported by the host",
        "observed": (
            "budget-credited apparent + allocated bytes; a reporting sum, not extra storage "
            "and not deduplicated disk usage"
        ),
        "budget": (
            "max_bytes applies globally to the observed sum; when bounded, counters may be "
            "lower than raw metadata sizes"
        ),
    }
)

# Stable, explanatory reason codes.  They describe evidence and boundaries;
# none of them authorizes a filesystem effect.
ROOT_OMITTED = "root_omitted"
ROOT_NOT_ABSOLUTE = "root_not_absolute"
ROOT_INVALID = "root_invalid"
ROOT_ABSENT = "root_absent"
ROOT_SYMLINK = "root_symlink"
ROOT_PATH_SYMLINK = "root_path_symlink"
ROOT_NOT_DIRECTORY = "root_not_directory"
ROOT_PERMISSION_DENIED = "permission_denied"
ROOT_IDENTITY_CHANGED = "root_identity_changed"
ROOT_UNAVAILABLE = "root_unavailable"
ENTRY_SYMLINK = "symlink"
ENTRY_HARDLINK = "hardlink"
ENTRY_NON_REGULAR = "non_regular"
ENTRY_MOUNT_BOUNDARY = "mount_boundary"
ENTRY_IDENTITY_UNAVAILABLE = "identity_unavailable"
ENTRY_IDENTITY_CHANGED = "identity_changed"
ENTRY_DISAPPEARED = "disappeared"
DEPTH_LIMIT = "depth_limit"
ENTRY_LIMIT = "entry_limit"
BYTE_LIMIT = "byte_limit"
CANCELLED = "cancelled"
SCAN_UNAVAILABLE = "scan_unavailable"
NO_ROOTS = "no_roots"
NO_OWNER = "no_owner"
PRESERVED_OWNER = "preserved_owner"
DIAGNOSTIC_ONLY = "diagnostic_only"
OUT_OF_PROFILE = "out_of_profile"
SCHEMA_UNVERIFIED = "schema_unverified"
MIXED_OBSERVATIONS = "mixed_observations"
EVIDENCE_INCOMPLETE = "evidence_incomplete"

MACHINE_INVENTORY_REASON_EXPLANATIONS: Mapping[str, str] = MappingProxyType(
    {
        ROOT_ABSENT: "La raíz no existe; no se creó para completar la consulta.",
        ROOT_SYMLINK: "La raíz es un enlace simbólico y no se siguió.",
        ROOT_PATH_SYMLINK: "Un componente de la ruta de la raíz es un enlace simbólico.",
        ROOT_NOT_DIRECTORY: "La raíz no es un directorio.",
        ROOT_PERMISSION_DENIED: "No se pudieron leer los metadatos por permisos.",
        ROOT_IDENTITY_CHANGED: "La identidad física de la raíz cambió durante la consulta.",
        ROOT_UNAVAILABLE: "La raíz no estuvo disponible para observación.",
        ENTRY_SYMLINK: "Se detectó un enlace simbólico; no se siguió.",
        ENTRY_HARDLINK: "El archivo tiene hardlinks compartidos; no se infiere autoridad.",
        ENTRY_NON_REGULAR: "El objeto no es un archivo/directorio regular.",
        ENTRY_MOUNT_BOUNDARY: "El objeto cruza un límite de filesystem/montaje.",
        ENTRY_IDENTITY_UNAVAILABLE: "La identidad física no pudo verificarse.",
        ENTRY_IDENTITY_CHANGED: "La identidad física cambió durante la observación.",
        ENTRY_DISAPPEARED: "La entrada desapareció durante la observación.",
        DEPTH_LIMIT: "La profundidad solicitada agotó el límite de observación.",
        ENTRY_LIMIT: "La cantidad de entradas agotó el límite global o la cuota fair-share de la raíz.",
        BYTE_LIMIT: "Los bytes observados agotaron el límite global.",
        CANCELLED: "La consulta fue cancelada antes de completar la cobertura.",
        SCAN_UNAVAILABLE: "La enumeración no estuvo disponible.",
        NO_ROOTS: "No se seleccionaron raíces para la consulta.",
        NO_OWNER: "No hay owner confiable registrado para la categoría.",
        PRESERVED_OWNER: "La categoría pertenece a un owner que se conserva.",
        DIAGNOSTIC_ONLY: "La categoría sólo se observa; no tiene autoridad de efecto aquí.",
        OUT_OF_PROFILE: "La categoría está fuera del perfil administrado por NeoCortex.",
        SCHEMA_UNVERIFIED: "La procedencia/schema de la categoría no está verificada.",
        MIXED_OBSERVATIONS: "La consulta contiene varias clasificaciones.",
        EVIDENCE_INCOMPLETE: "La observación no tiene evidencia completa.",
        "entry_blocked": "Una o más entradas cruzaron una frontera de seguridad.",
        "invalid_profile": "El perfil de la categoría no es válido.",
    }
)


class MachineInventoryError(RuntimeError):
    """Base error for invalid machine-inventory configuration."""


class MachineInventoryRootError(ValueError, MachineInventoryError):
    """The caller did not provide an absolute, bounded root specification."""


class MachineInventoryCategoryError(ValueError, MachineInventoryError):
    """The caller did not provide a valid category identifier."""


def _bounded_text(value: object, *, limit: int, label: str) -> str:
    text = str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > limit:
        encoded = encoded[:limit]
        text = encoded.decode("utf-8", errors="replace")
    if "\x00" in text:
        raise ValueError(f"{label} must not contain NUL")
    return text


def _required_text(value: object, *, label: str, limit: int = MAX_METADATA_LABEL_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MachineInventoryCategoryError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise MachineInventoryCategoryError(f"{label} must not contain NUL")
    if len(value.encode("utf-8")) > limit:
        raise MachineInventoryCategoryError(f"{label} exceeds its bounded size")
    return value


_CATEGORY_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "state": "neocortex_state",
        "data": "neocortex_data",
        "corpus": "neocortex_corpus",
        "neocortex-state": "neocortex_state",
        "neocortex-data": "neocortex_data",
        "neocortex-corpus": "neocortex_corpus",
        "temporary": "tmp",
        "temp": "tmp",
        "xdg-cache": "cache",
        "xdg_config": "config",
        "xdg-config": "config",
        "user-home": "home",
    }
)


def _canonical_category(value: object) -> str:
    text = _required_text(value, label="category")
    return _CATEGORY_ALIASES.get(text, text)


@dataclass(frozen=True, slots=True)
class MachineInventoryCategorySpec:
    """Static ownership and profile metadata for one inventory category."""

    name: str
    owner: str | None
    provenance: str
    profile: MachineInventoryProfile
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner": self.owner,
            "provenance": self.provenance,
            "profile": self.profile,
            "description": self.description,
        }


def _category(
    name: MachineInventoryCategory,
    *,
    owner: str | None,
    provenance: str,
    profile: MachineInventoryProfile,
    description: str,
) -> MachineInventoryCategorySpec:
    return MachineInventoryCategorySpec(
        name=name,
        owner=owner,
        provenance=provenance,
        profile=profile,
        description=description,
    )


# This registry describes defaults only.  It never discovers a root and it
# never confers authority to modify an owner-managed path.
_CATEGORY_REGISTRY: dict[str, MachineInventoryCategorySpec] = {
    "neocortex_state": _category(
        "neocortex_state",
        owner="neocortex-runtime",
        provenance="platform-policy:state_directory",
        profile="preserved",
        description="NeoCortex durable runtime state is retained and observed read-only.",
    ),
    "neocortex_data": _category(
        "neocortex_data",
        owner="neocortex-runtime",
        provenance="platform-policy:data_directory",
        profile="preserved",
        description="NeoCortex data and release material is retained and observed read-only.",
    ),
    "neocortex_corpus": _category(
        "neocortex_corpus",
        owner="neocortex-content",
        provenance="platform-policy:corpus_root",
        profile="preserved",
        description="The NeoCortex corpus is preserved; this inventory never reads payloads.",
    ),
    "tmp": _category(
        "tmp",
        owner="operating-system",
        provenance="fixed:/tmp",
        profile="observed",
        description="The operating-system temporary tree is observed without cleanup authority.",
    ),
    "cache": _category(
        "cache",
        owner="xdg/application-cache",
        provenance="XDG_CACHE_HOME",
        profile="observed",
        description="XDG cache material is observed under its application owners.",
    ),
    "config": _category(
        "config",
        owner="xdg/application-config",
        provenance="XDG_CONFIG_HOME",
        profile="preserved",
        description="XDG configuration is retained and never treated as disposable.",
    ),
    "home": _category(
        "home",
        owner="user-profile",
        provenance="HOME",
        profile="preserved",
        description="The user profile is preserved; the inventory is metadata-only.",
    ),
    "external": _category(
        "external",
        owner=None,
        provenance="caller-explicit-root",
        profile="out_of_profile",
        description="An explicitly supplied external root has no inferred NeoCortex owner.",
    ),
}

MACHINE_INVENTORY_CATEGORY_SPECS: Mapping[str, MachineInventoryCategorySpec] = MappingProxyType(
    _CATEGORY_REGISTRY
)
MACHINE_INVENTORY_CATEGORIES_SPECS = MACHINE_INVENTORY_CATEGORY_SPECS
CATEGORY_REGISTRY = MACHINE_INVENTORY_CATEGORY_SPECS
SUPPORTED_MACHINE_INVENTORY_CATEGORIES = MACHINE_INVENTORY_CATEGORIES


def machine_inventory_category_spec(category: str) -> MachineInventoryCategorySpec:
    """Return category metadata without touching the filesystem."""

    canonical = _canonical_category(category)
    spec = _CATEGORY_REGISTRY.get(canonical)
    if spec is not None:
        return spec
    return MachineInventoryCategorySpec(
        name=canonical,
        owner=None,
        provenance="unregistered-category",
        profile="out_of_profile",
        description="No trusted owner is registered for this category.",
    )


# Short aliases make the typed registry convenient for embedding callers.
category_spec = machine_inventory_category_spec
inventory_category_spec = machine_inventory_category_spec


def _absolute_path(value: Path | str | os.PathLike[str], *, label: str) -> Path:
    try:
        candidate = Path(os.fspath(value))
    except (TypeError, ValueError) as exc:
        raise MachineInventoryRootError(f"{label} must be an absolute path") from exc
    if not candidate.is_absolute():
        raise MachineInventoryRootError(ROOT_NOT_ABSOLUTE)
    if "\x00" in os.fspath(candidate):
        raise MachineInventoryRootError(f"{label} must not contain NUL")
    return candidate


@dataclass(frozen=True, slots=True)
class MachineInventoryRoot:
    """One repeatable root and its logical category metadata.

    A bare path is intentionally not accepted by this dataclass without an
    absolute path.  Bare paths supplied to the public scanner are converted to
    this object as ``external`` roots.
    """

    path: Path
    category: str = "external"
    owner: str | None = None
    provenance: str | None = None
    profile: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        candidate = _absolute_path(self.path, label="root")
        category = _canonical_category(self.category)
        object.__setattr__(self, "path", candidate)
        object.__setattr__(self, "category", category)
        for field_name, value in (
            ("owner", self.owner),
            ("provenance", self.provenance),
            ("profile", self.profile),
            ("label", self.label),
        ):
            if value is not None:
                _bounded_text(value, limit=MAX_METADATA_LABEL_BYTES, label=field_name)

    @property
    def root(self) -> Path:
        """Compatibility name for callers that call the path a root."""

        return self.path

    @property
    def name(self) -> str:
        return self.label or self.category

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "root": str(self.path),
            "category": self.category,
            "owner": self.owner,
            "provenance": self.provenance,
            "profile": self.profile,
            "label": self.label,
        }


MachineInventoryRootSpec = MachineInventoryRoot
InventoryRoot = MachineInventoryRoot
RootInput: TypeAlias = (
    Path
    | str
    | os.PathLike[str]
    | MachineInventoryRoot
    | tuple[Path | str | os.PathLike[str], str]
)


def _coerce_root(value: RootInput) -> MachineInventoryRoot:
    if isinstance(value, MachineInventoryRoot):
        return value
    if isinstance(value, tuple):
        if len(value) != 2:
            raise MachineInventoryRootError("root tuple must contain (path, category)")
        return MachineInventoryRoot(Path(os.fspath(value[0])), category=value[1])
    return MachineInventoryRoot(Path(os.fspath(value)))


def _coerce_roots(
    roots: RootInput | Iterable[RootInput] | None,
) -> tuple[MachineInventoryRoot, ...]:
    if roots is None:
        return default_machine_inventory_roots()
    if (
        isinstance(roots, tuple)
        and len(roots) == 2
        and isinstance(roots[1], str)
        and isinstance(roots[0], (Path, str))
    ):
        return (_coerce_root(cast(RootInput, roots)),)
    if isinstance(roots, (MachineInventoryRoot, Path, str)) or hasattr(roots, "__fspath__"):
        return (_coerce_root(cast(RootInput, roots)),)
    if not isinstance(roots, Iterable):
        raise MachineInventoryRootError("roots must be a path or iterable of roots")
    return tuple(_coerce_root(root) for root in roots)


def _absolute_xdg(name: str, fallback: Path) -> Path:
    configured = os.environ.get(name)
    candidate = fallback if configured is None else Path(configured).expanduser()
    if not candidate.is_absolute():
        raise MachineInventoryRootError(f"{name} must name an absolute path")
    return candidate


def _corpus_default(home: Path) -> Path:
    configured = os.environ.get("NEOCORTEX_CORPUS_ROOT")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise MachineInventoryRootError("NEOCORTEX_CORPUS_ROOT must name an absolute path")
        return candidate
    # Use the platform policy for the real process so localized XDG documents
    # paths (for example ``~/Documentos``) remain canonical.  The injectable
    # fixture-home fallback avoids consulting the live profile in tests.
    if home == Path.home():
        return default_corpus_root()
    return home / "Documents" / "NeoCortex" / "Corpus"


def default_machine_inventory_roots(
    *,
    home: Path | str | os.PathLike[str] | None = None,
) -> tuple[MachineInventoryRoot, ...]:
    """Return safe default roots without creating or inspecting any path.

    ``home`` is injectable for deterministic callers and tests.  The returned
    tuple deliberately omits ``external`` and never contains filesystem root
    ``/`` merely as a broad discovery fallback.
    """

    profile = _absolute_path(Path.home() if home is None else home, label="home")
    cache_home = _absolute_xdg("XDG_CACHE_HOME", profile / ".cache")
    config_home = _absolute_xdg("XDG_CONFIG_HOME", profile / ".config")
    state_home = _absolute_xdg("XDG_STATE_HOME", profile / ".local" / "state")
    data_home = _absolute_xdg("XDG_DATA_HOME", profile / ".local" / "share")
    corpus = _corpus_default(profile)
    # Scan NeoCortex-owned roots before broad profile roots so a global
    # budget cannot be consumed by HOME or /tmp before the product's own
    # state/corpus categories receive any observation.
    return (
        MachineInventoryRoot(
            state_home / "Neocortex" / "state",
            category="neocortex_state",
            owner="neocortex-runtime",
            provenance="platform-policy:state_directory",
            profile="preserved",
        ),
        MachineInventoryRoot(
            data_home / "Neocortex",
            category="neocortex_data",
            owner="neocortex-runtime",
            provenance="platform-policy:data_directory",
            profile="preserved",
        ),
        MachineInventoryRoot(
            corpus,
            category="neocortex_corpus",
            owner="neocortex-content",
            provenance="platform-policy:corpus_root",
            profile="preserved",
        ),
        MachineInventoryRoot(
            Path("/tmp"),
            category="tmp",
            owner="operating-system",
            provenance="fixed:/tmp",
            profile="observed",
        ),
        MachineInventoryRoot(
            cache_home,
            category="cache",
            owner="xdg/application-cache",
            provenance="XDG_CACHE_HOME",
            profile="observed",
        ),
        MachineInventoryRoot(
            config_home,
            category="config",
            owner="xdg/application-config",
            provenance="XDG_CONFIG_HOME",
            profile="preserved",
        ),
        MachineInventoryRoot(profile, category="home", provenance="HOME", profile="preserved"),
    )


def default_machine_inventory_paths() -> tuple[Path, ...]:
    """Return only the paths represented by :func:`default_machine_inventory_roots`."""

    return tuple(root.path for root in default_machine_inventory_roots())
default_roots = default_machine_inventory_roots


def filesystem_identity(path: Path | str | os.PathLike[str]) -> tuple[int, int, int] | None:
    """Read one no-follow physical identity without opening a payload."""

    try:
        metadata = os.lstat(Path(path))
    except OSError:
        return None
    return _identity(metadata)


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return int(metadata.st_dev), int(metadata.st_ino), int(stat_birthtime_ns(metadata))


def _file_type(metadata: os.stat_result) -> str:
    mode = metadata.st_mode
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block_device"
    if stat.S_ISCHR(mode):
        return "char_device"
    return "other"


def _raw_sizes(metadata: os.stat_result) -> tuple[int, int]:
    """Return metadata-reported apparent/allocated size without reading data."""

    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        return 0, 0
    apparent = max(0, int(getattr(metadata, "st_size", 0)))
    allocated = max(0, int(getattr(metadata, "st_blocks", 0))) * 512
    return apparent, allocated


def _owner_class(uid: int | None) -> str:
    if uid is None:
        return "unknown"
    try:
        effective_uid = os.geteuid()
    except AttributeError:  # pragma: no cover - Linux has geteuid
        return "unknown"
    return "current-user" if int(uid) == int(effective_uid) else "foreign-user"


def _mode(metadata: os.stat_result | None) -> int | None:
    return None if metadata is None else stat.S_IMODE(metadata.st_mode)


def _safe_reason(value: object) -> str:
    text = str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_METADATA_REASON_BYTES:
        return text
    return encoded[:MAX_METADATA_REASON_BYTES].decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class MachineInventoryRecord:
    """One bounded no-follow metadata observation."""

    path: Path
    root_path: Path
    root_index: int
    relative_path: str
    name: str
    category: str
    owner: str | None
    provenance: str
    profile: str
    status: MachineInventoryStatus
    reason_code: str
    reason: str | None
    owner_class: str
    path_identity: tuple[int, int, int] | None
    root_identity: tuple[int, int, int] | None
    observed_uid: int | None
    observed_gid: int | None
    mode: int | None
    nlink: int | None
    apparent_bytes: int
    allocated_bytes: int
    observed_bytes: int
    depth: int
    file_type: str
    is_directory: bool
    truncated: bool = False

    @property
    def root(self) -> Path:
        return self.root_path

    @property
    def identity(self) -> tuple[int, int, int] | None:
        return self.path_identity

    @property
    def device_id(self) -> int | None:
        return None if self.path_identity is None else self.path_identity[0]

    @property
    def inode(self) -> int | None:
        return None if self.path_identity is None else self.path_identity[1]

    @property
    def birthtime_ns(self) -> int | None:
        return None if self.path_identity is None else self.path_identity[2]

    @property
    def uid(self) -> int | None:
        return self.observed_uid

    @property
    def gid(self) -> int | None:
        return self.observed_gid

    @property
    def owner_uid(self) -> int | None:
        return self.observed_uid

    @property
    def owner_gid(self) -> int | None:
        return self.observed_gid

    @property
    def permissions(self) -> int | None:
        return self.mode

    @property
    def permission_bits(self) -> int | None:
        return self.mode

    @property
    def kind(self) -> str:
        return self.file_type

    @property
    def is_symlink(self) -> bool:
        return self.file_type == "symlink"

    @property
    def is_hardlink(self) -> bool:
        return self.reason_code == ENTRY_HARDLINK

    @property
    def crosses_mount(self) -> bool:
        return self.reason_code == ENTRY_MOUNT_BOUNDARY

    @property
    def size_bytes(self) -> int:
        return self.observed_bytes

    @property
    def bytes(self) -> int:
        return self.observed_bytes

    def to_dict(self) -> dict[str, object]:
        identity = None if self.path_identity is None else list(self.path_identity)
        root_identity = None if self.root_identity is None else list(self.root_identity)
        return {
            "path": str(self.path),
            "root": str(self.root_path),
            "root_path": str(self.root_path),
            "root_index": self.root_index,
            "relative_path": self.relative_path,
            "name": self.name,
            "category": self.category,
            "owner": self.owner,
            "provenance": self.provenance,
            "profile": self.profile,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "owner_class": self.owner_class,
            "path_identity": identity,
            "identity": identity,
            "root_identity": root_identity,
            "observed_uid": self.observed_uid,
            "observed_gid": self.observed_gid,
            "uid": self.observed_uid,
            "gid": self.observed_gid,
            "owner_uid": self.observed_uid,
            "owner_gid": self.observed_gid,
            "mode": self.mode,
            "permissions": self.mode,
            "nlink": self.nlink,
            "apparent_bytes": self.apparent_bytes,
            "allocated_bytes": self.allocated_bytes,
            "observed_bytes": self.observed_bytes,
            "size_bytes": self.observed_bytes,
            "depth": self.depth,
            "file_type": self.file_type,
            "kind": self.file_type,
            "is_directory": self.is_directory,
            "is_symlink": self.is_symlink,
            "is_hardlink": self.is_hardlink,
            "crosses_mount": self.crosses_mount,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class MachineInventoryRootResult:
    """Result for one root in the federated inventory."""

    path: Path
    root_index: int
    category: str
    owner: str | None
    provenance: str
    profile: str
    status: MachineInventoryStatus
    reason_code: str
    reason: str | None
    records: tuple[MachineInventoryRecord, ...] = ()
    scanned: int = 0
    observed: int = 0
    preserved: int = 0
    blocked: int = 0
    unknown: int = 0
    out_of_profile: int = 0
    absent: int = 0
    apparent_bytes: int = 0
    allocated_bytes: int = 0
    observed_bytes: int = 0
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()
    root_identity: tuple[int, int, int] | None = None
    root_uid: int | None = None
    root_gid: int | None = None
    root_mode: int | None = None
    root_nlink: int | None = None
    root_exists: bool = False
    root_type: str = "unknown"
    root_is_directory: bool = False
    max_entries: int = 0
    max_depth: int = 0
    max_bytes: int = 0
    entry_quota: int = 0

    @property
    def root(self) -> Path:
        return self.path

    @property
    def coverage(self) -> MachineInventoryCoverage:
        """Return scanner coverage for this root, not renderer coverage."""

        if self.status == "blocked":
            return "blocked"
        if self.truncated or self.status in {"unknown", "absent"}:
            return "partial"
        return "complete"

    @property
    def identity(self) -> tuple[int, int, int] | None:
        return self.root_identity

    @property
    def entries(self) -> tuple[MachineInventoryRecord, ...]:
        return self.records

    @property
    def items(self) -> tuple[MachineInventoryRecord, ...]:
        return self.records

    @property
    def records_scanned(self) -> int:
        """Number of record observations produced by the bounded scanner."""

        return self.scanned

    @property
    def records_returned(self) -> int:
        """Number of records present in this owner result."""

        return len(self.records)

    @property
    def records_omitted(self) -> int | None:
        """Exact omitted-record count when known, otherwise ``None``.

        A bounded scan cannot count entries beyond a depth/entry/byte fence
        without defeating that fence.  ``None`` therefore means *unknown*,
        not zero.  A complete root (and an explicitly absent root) has an
        exact omitted count of zero.
        """

        if self.truncated or self.status in {"blocked", "unknown"}:
            return None
        return 0

    @property
    def records_omitted_known(self) -> bool:
        return self.records_omitted is not None

    @property
    def record_status_counts(self) -> dict[str, int]:
        """Return status counts for records only.

        A root result has no synthetic root-marker records, so this is the
        unambiguous record-only counterpart to the federated report's
        compatibility ``status_counts`` aggregate.
        """

        return {
            status_name: int(self.counts.get(status_name, 0))
            for status_name in MACHINE_INVENTORY_STATUSES
        }

    @property
    def record_category_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter(record.category for record in self.records)
        return dict(counts)

    @property
    def record_reason_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter(record.reason_code for record in self.records)
        return dict(counts)

    @property
    def scanner_truncated(self) -> bool:
        """Whether the owner scanner, rather than presentation, truncated."""

        return self.truncated

    @property
    def scanner_truncation_reasons(self) -> tuple[str, ...]:
        return self.truncation_reasons

    @property
    def presentation_truncated(self) -> bool:
        """The runtime returns all owner records; renderers may trim later."""

        return False

    @property
    def byte_semantics(self) -> dict[str, str]:
        return dict(MACHINE_INVENTORY_BYTE_SEMANTICS)

    @property
    def limits(self) -> dict[str, int]:
        return {
            "max_entries": self.max_entries,
            "max_depth": self.max_depth,
            "max_bytes": self.max_bytes,
        }

    @property
    def root_quota_policy(self) -> str:
        return MACHINE_INVENTORY_ENTRY_QUOTA_POLICY

    @property
    def entry_budget(self) -> dict[str, object]:
        """Describe this root's effective share of the global entry bound."""

        return {
            "policy": MACHINE_INVENTORY_ENTRY_QUOTA_POLICY,
            "global_max_entries": self.max_entries,
            "quota": self.entry_quota,
            "entry_quota": self.entry_quota,
            "records_scanned": self.records_scanned,
            "records_remaining": max(0, self.entry_quota - self.records_scanned),
        }

    @property
    def omissions(self) -> dict[str, dict[str, object]]:
        """Separate scanner omissions from presentation omissions.

        This owner result has no renderer, so presentation omissions are
        explicitly zero.  A consumer that clips the result must update its
        own presentation facet instead of relabelling scanner truncation.
        """

        return {
            "scanner": {
                "truncated": self.scanner_truncated,
                "reasons": list(self.scanner_truncation_reasons),
                "records_omitted": self.records_omitted,
                "records_omitted_known": self.records_omitted_known,
            },
            "presentation": {
                "truncated": False,
                "reasons": [],
                "records_omitted": 0,
                "records_omitted_known": True,
            },
        }

    @property
    def coverage_metadata(self) -> dict[str, object]:
        """Return explicit scanner/presentation coverage facets."""

        return {
            "scanner": {
                "status": self.coverage,
                "truncated": self.scanner_truncated,
                "truncation_reasons": list(self.scanner_truncation_reasons),
                "records_scanned": self.records_scanned,
                "records_returned": self.records_returned,
                "records_omitted": self.records_omitted,
                "records_omitted_known": self.records_omitted_known,
            },
            "presentation": {
                "status": "complete",
                "truncated": self.presentation_truncated,
                "records_scanned": self.records_scanned,
                "records_returned": self.records_returned,
                "records_omitted": 0,
                "records_omitted_known": True,
            },
        }

    def to_summary_dict(self) -> dict[str, object]:
        """Return a compact root summary without the full record list."""

        return {
            "root": str(self.path),
            "path": str(self.path),
            "root_index": self.root_index,
            "category": self.category,
            "owner": self.owner,
            "provenance": self.provenance,
            "profile": self.profile,
            "status": self.status,
            "coverage": self.coverage,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "root_identity": None if self.root_identity is None else list(self.root_identity),
            "identity": None if self.root_identity is None else list(self.root_identity),
            "root_uid": self.root_uid,
            "root_gid": self.root_gid,
            "root_mode": self.root_mode,
            "root_nlink": self.root_nlink,
            "root_exists": self.root_exists,
            "root_type": self.root_type,
            "root_is_directory": self.root_is_directory,
            "counts": self.counts,
            "records_scanned": self.records_scanned,
            "records_returned": self.records_returned,
            "records_omitted": self.records_omitted,
            "records_omitted_known": self.records_omitted_known,
            "scanner_truncated": self.scanner_truncated,
            "scanner_truncation_reasons": list(self.scanner_truncation_reasons),
            "presentation_truncated": self.presentation_truncated,
            "root_quota_policy": MACHINE_INVENTORY_ENTRY_QUOTA_POLICY,
            "entry_quota": self.entry_quota,
            "entry_budget": self.entry_budget,
            "bytes": self.bytes,
            "byte_semantics": self.byte_semantics,
            "record_status_counts": self.record_status_counts,
            "record_category_counts": self.record_category_counts,
            "record_reason_counts": self.record_reason_counts,
            "limits": self.limits,
            "coverage_metadata": self.coverage_metadata,
            "omissions": self.omissions,
        }

    @property
    def root_owner_class(self) -> str:
        return _owner_class(self.root_uid)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "observed": self.observed,
            "preserved": self.preserved,
            "blocked": self.blocked,
            "unknown": self.unknown,
            "out_of_profile": self.out_of_profile,
            "absent": self.absent,
        }

    @property
    def bytes(self) -> dict[str, int]:
        return {
            "apparent": self.apparent_bytes,
            "allocated": self.allocated_bytes,
            "observed": self.observed_bytes,
        }

    @property
    def size_bytes(self) -> int:
        return self.observed_bytes

    def to_dict(self) -> dict[str, object]:
        identity = None if self.root_identity is None else list(self.root_identity)
        return {
            "root": str(self.path),
            "path": str(self.path),
            "root_index": self.root_index,
            "category": self.category,
            "owner": self.owner,
            "provenance": self.provenance,
            "profile": self.profile,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "root_identity": identity,
            "identity": identity,
            "root_uid": self.root_uid,
            "root_gid": self.root_gid,
            "root_mode": self.root_mode,
            "root_nlink": self.root_nlink,
            "root_exists": self.root_exists,
            "root_type": self.root_type,
            "root_is_directory": self.root_is_directory,
            # Additive summary/coverage fields.  ``records`` below remains
            # the full owner result for compatibility; callers that need a
            # compact projection should use ``to_summary_dict``.
            "coverage": self.coverage,
            "records_scanned": self.records_scanned,
            "records_returned": self.records_returned,
            "records_omitted": self.records_omitted,
            "records_omitted_known": self.records_omitted_known,
            "scanner_truncated": self.scanner_truncated,
            "scanner_truncation_reasons": list(self.scanner_truncation_reasons),
            "presentation_truncated": self.presentation_truncated,
            "byte_semantics": self.byte_semantics,
            "limits": self.limits,
            "root_quota_policy": MACHINE_INVENTORY_ENTRY_QUOTA_POLICY,
            "entry_quota": self.entry_quota,
            "entry_budget": self.entry_budget,
            "coverage_metadata": self.coverage_metadata,
            "omissions": self.omissions,
            "summary": self.to_summary_dict(),
            "counts": self.counts,
            "record_status_counts": self.record_status_counts,
            "record_category_counts": self.record_category_counts,
            "record_reason_counts": self.record_reason_counts,
            "bytes": self.bytes,
            "scanned": self.scanned,
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "records": [record.to_dict() for record in self.records],
        }


MachineInventoryRootReport = MachineInventoryRootResult


@dataclass(frozen=True, slots=True)
class MachineInventoryReport:
    """Read-only federated machine-inventory result."""

    roots: tuple[MachineInventoryRootResult, ...]
    records: tuple[MachineInventoryRecord, ...]
    status: MachineInventoryStatus
    reason_code: str
    reason: str | None
    scanned: int
    root_count: int
    observed: int
    preserved: int
    blocked: int
    unknown: int
    out_of_profile: int
    absent: int
    apparent_bytes: int
    allocated_bytes: int
    observed_bytes: int
    truncated: bool
    truncation_reasons: tuple[str, ...]
    max_entries: int
    max_depth: int
    max_bytes: int
    status_counts: Mapping[str, int]
    category_counts: Mapping[str, int]
    reason_counts: Mapping[str, int]
    root_status_counts: Mapping[str, int]
    read_only: bool = True
    metadata_only: bool = True
    content_read: bool = False
    sqlite_read: bool = False
    network_used: bool = False
    kio_used: bool = False
    mutated: bool = False
    # Additive counters are appended after the historical defaulted fields so
    # positional construction of older report objects keeps its meaning.
    record_status_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    record_category_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    record_reason_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    root_marker_status_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    root_marker_category_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    root_marker_reason_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def schema(self) -> str:
        return MACHINE_INVENTORY_SCHEMA

    @property
    def operation(self) -> str:
        return "machine-inventory"

    @property
    def diagnostic_only(self) -> bool:
        return True

    @property
    def coverage(self) -> str:
        if self.status == "blocked":
            return "blocked"
        return (
            "partial"
            if self.truncated or self.status in {"unknown", "absent"}
            else "complete"
        )

    @property
    def scan_coverage(self) -> MachineInventoryCoverage:
        """Coverage attributable to the filesystem scanner."""

        return cast(MachineInventoryCoverage, self.coverage)

    @property
    def presentation_coverage(self) -> MachineInventoryCoverage:
        """Coverage before any downstream renderer/paginator clips output."""

        return "complete"

    @property
    def root_results(self) -> tuple[MachineInventoryRootResult, ...]:
        return self.roots

    @property
    def entries(self) -> tuple[MachineInventoryRecord, ...]:
        return self.records

    @property
    def items(self) -> tuple[MachineInventoryRecord, ...]:
        return self.records

    @property
    def records_scanned(self) -> int:
        """Number of bounded record observations made by the scanner."""

        return self.scanned

    @property
    def records_returned(self) -> int:
        """Number of records retained in the owner result payload."""

        return len(self.records)

    @property
    def records_omitted(self) -> int | None:
        """Exact omitted-record count, or ``None`` when a bound hides it."""

        if self.truncated or self.status in {"blocked", "unknown"}:
            return None
        return 0

    @property
    def records_omitted_known(self) -> bool:
        return self.records_omitted is not None

    @property
    def records_truncated(self) -> bool:
        """Compatibility alias for scanner-side record truncation."""

        return self.scanner_truncated

    @property
    def scanner_truncated(self) -> bool:
        return self.truncated

    @property
    def scanner_truncation_reasons(self) -> tuple[str, ...]:
        return self.truncation_reasons

    @property
    def byte_semantics(self) -> dict[str, str]:
        return dict(MACHINE_INVENTORY_BYTE_SEMANTICS)

    @property
    def root_summaries(self) -> tuple[dict[str, object], ...]:
        """Compact, record-free summaries for every requested root."""

        return tuple(root.to_summary_dict() for root in self.roots)

    @property
    def root_quota_policy(self) -> str:
        """Name the deterministic per-root entry-budget policy."""

        return MACHINE_INVENTORY_ENTRY_QUOTA_POLICY

    @property
    def root_entry_quotas(self) -> tuple[int, ...]:
        """Effective entry quotas in stable root-index order."""

        return tuple(root.entry_quota for root in self.roots)

    @property
    def entry_budget(self) -> dict[str, object]:
        """Describe the invocation-wide bound and its root allocations."""

        return {
            "policy": self.root_quota_policy,
            "global_max_entries": self.max_entries,
            "root_count": self.root_count,
            "root_entry_quotas": list(self.root_entry_quotas),
            # These are sequential per-root caps, not reservations.  When an
            # earlier root finishes below its cap, later caps grow and their
            # sum can therefore exceed the global bound without permitting
            # the scanner to exceed it.
            "quota_cap_sum": sum(self.root_entry_quotas),
            "records_scanned": self.records_scanned,
            "records_remaining": max(0, self.max_entries - self.records_scanned),
        }

    @property
    def summaries(self) -> tuple[dict[str, object], ...]:
        """Short alias for :attr:`root_summaries`."""

        return self.root_summaries

    @property
    def omissions(self) -> dict[str, dict[str, object]]:
        """Expose scanner and presentation omission facets separately."""

        scanner_roots = sum(root.coverage != "complete" for root in self.roots)
        return {
            "scanner": {
                "truncated": self.scanner_truncated,
                "reasons": list(self.scanner_truncation_reasons),
                "records_omitted": self.records_omitted,
                "records_omitted_known": self.records_omitted_known,
                # Every requested root gets a bounded result object.  A root
                # may still have partial child coverage; that distinction is
                # represented by its own summary rather than hidden here.
                "roots_requested": self.root_count,
                "root_summaries_omitted": 0,
                "roots_with_incomplete_coverage": scanner_roots,
            },
            "presentation": {
                "truncated": False,
                "reasons": [],
                "records_omitted": 0,
                "records_omitted_known": True,
                "root_summaries_omitted": 0,
            },
        }

    @property
    def coverage_metadata(self) -> dict[str, object]:
        """Return explicit scanner and downstream-presentation coverage."""

        return {
            "scanner": {
                "status": self.scan_coverage,
                "truncated": self.scanner_truncated,
                "truncation_reasons": list(self.scanner_truncation_reasons),
                "reason_code": self.reason_code,
                "records_scanned": self.records_scanned,
                "records_returned": self.records_returned,
                "records_omitted": self.records_omitted,
                "records_omitted_known": self.records_omitted_known,
                "roots_requested": self.root_count,
                "root_summaries_returned": len(self.root_summaries),
                "root_summaries_omitted": 0,
            },
            "presentation": {
                "status": self.presentation_coverage,
                "truncated": False,
                "records_scanned": self.records_scanned,
                "records_returned": self.records_returned,
                "records_omitted": 0,
                "records_omitted_known": True,
                "root_summaries_returned": len(self.root_summaries),
                "root_summaries_omitted": 0,
            },
        }

    def to_summary_dict(self) -> dict[str, object]:
        """Return a bounded federated summary without full record payloads.

        ``to_dict`` remains the compatibility/full representation.  This
        projection is the owner-provided view for human/JSON renderers that
        should not print every record by default.
        """

        return {
            "schema": MACHINE_INVENTORY_SCHEMA,
            "operation": self.operation,
            "status": self.status,
            "coverage": self.coverage,
            "scan_coverage": self.scan_coverage,
            "presentation_coverage": self.presentation_coverage,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "read_only": self.read_only,
            "diagnostic_only": self.diagnostic_only,
            "metadata_only": self.metadata_only,
            "content_read": self.content_read,
            "sqlite_read": self.sqlite_read,
            "network_used": self.network_used,
            "kio_used": self.kio_used,
            "mutated": self.mutated,
            "scanner_truncated": self.scanner_truncated,
            "scanner_truncation_reasons": list(self.scanner_truncation_reasons),
            "presentation_truncated": False,
            "truncated": self.truncated,
            "records_scanned": self.records_scanned,
            "records_returned": self.records_returned,
            "records_omitted": self.records_omitted,
            "records_omitted_known": self.records_omitted_known,
            "limits": {
                "max_entries": self.max_entries,
                "max_depth": self.max_depth,
                "max_bytes": self.max_bytes,
            },
            "counts": self.counts,
            "aggregates": self.aggregates,
            "status_counts": dict(self.status_counts),
            "category_counts": dict(self.category_counts),
            "reason_counts": dict(self.reason_counts),
            "root_status_counts": dict(self.root_status_counts),
            "record_status_counts": dict(self.record_status_counts),
            "record_category_counts": dict(self.record_category_counts),
            "record_reason_counts": dict(self.record_reason_counts),
            "root_marker_status_counts": dict(self.root_marker_status_counts),
            "root_marker_category_counts": dict(self.root_marker_category_counts),
            "root_marker_reason_counts": dict(self.root_marker_reason_counts),
            "reason_summary": self.reason_summary,
            "reason_explanations": self.reason_explanations,
            "bytes": self.bytes,
            "byte_semantics": self.byte_semantics,
            "observed_bytes": self.observed_bytes,
            "observed_apparent_bytes": self.observed_apparent_bytes,
            "observed_allocated_bytes": self.observed_allocated_bytes,
            "root_count": self.root_count,
            "root_quota_policy": self.root_quota_policy,
            "root_entry_quotas": list(self.root_entry_quotas),
            "entry_budget": self.entry_budget,
            "root_summaries": list(self.root_summaries),
            "coverage_metadata": self.coverage_metadata,
            "omissions": self.omissions,
        }

    @property
    def summary(self) -> dict[str, object]:
        """Convenience alias for the compact owner projection."""

        return self.to_summary_dict()

    @property
    def counts(self) -> dict[str, int]:
        result = dict(self.status_counts)
        result.update({"scanned": self.scanned, "roots": self.root_count})
        return result

    @property
    def aggregates(self) -> dict[str, dict[str, int]]:
        return {
            "status": dict(self.status_counts),
            "category": dict(self.category_counts),
            "reason": dict(self.reason_counts),
            "root_status": dict(self.root_status_counts),
            "record_status": dict(self.record_status_counts),
            "record_category": dict(self.record_category_counts),
            "record_reason": dict(self.record_reason_counts),
            "root_marker_status": dict(self.root_marker_status_counts),
            "root_marker_category": dict(self.root_marker_category_counts),
            "root_marker_reason": dict(self.root_marker_reason_counts),
        }

    @property
    def reason_summary(self) -> dict[str, int]:
        """Return bounded reason counts under the stable summary name."""

        return dict(self.reason_counts)

    @property
    def reason_explanations(self) -> dict[str, str]:
        """Return deterministic human explanations for observed reason codes."""

        return {
            code: MACHINE_INVENTORY_REASON_EXPLANATIONS.get(
                code,
                "La razón requiere revisión del owner; no autoriza un efecto.",
            )
            for code in self.reason_counts
        }

    @property
    def category_registry(self) -> dict[str, dict[str, object]]:
        return {
            name: spec.to_dict()
            for name, spec in MACHINE_INVENTORY_CATEGORY_SPECS.items()
        }

    @property
    def owner_registry(self) -> dict[str, str | None]:
        return {
            name: spec.owner for name, spec in MACHINE_INVENTORY_CATEGORY_SPECS.items()
        }

    @property
    def provenance_registry(self) -> dict[str, str]:
        return {
            name: spec.provenance
            for name, spec in MACHINE_INVENTORY_CATEGORY_SPECS.items()
        }

    @property
    def categories(self) -> dict[str, int]:
        return dict(self.category_counts)

    @property
    def owners(self) -> dict[str, str | None]:
        return dict(self.owner_registry)

    @property
    def provenance(self) -> dict[str, str]:
        return dict(self.provenance_registry)

    @property
    def observed_apparent_bytes(self) -> int:
        return self.apparent_bytes

    @property
    def observed_allocated_bytes(self) -> int:
        return self.allocated_bytes

    @property
    def returned(self) -> int:
        return len(self.records)

    @property
    def entries_returned(self) -> int:
        return len(self.records)

    @property
    def bytes(self) -> dict[str, int]:
        return {
            "apparent": self.apparent_bytes,
            "allocated": self.allocated_bytes,
            "observed": self.observed_bytes,
        }

    @property
    def size_bytes(self) -> int:
        return self.observed_bytes

    @property
    def truncated_reason(self) -> str | None:
        return self.truncation_reasons[0] if self.truncation_reasons else None

    @property
    def zero_candidates(self) -> bool:
        """Compatibility aid: this diagnostic never produces effect candidates."""

        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": MACHINE_INVENTORY_SCHEMA,
            "operation": self.operation,
            "status": self.status,
            "coverage": self.coverage,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "read_only": self.read_only,
            "diagnostic_only": self.diagnostic_only,
            "metadata_only": self.metadata_only,
            "content_read": self.content_read,
            "sqlite_read": self.sqlite_read,
            "network_used": self.network_used,
            "kio_used": self.kio_used,
            "mutated": self.mutated,
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            # Keep the historical full representation intact while exposing
            # additive scanner/presentation provenance and a compact root
            # projection for renderers that do not need every record.
            "scan_coverage": self.scan_coverage,
            "presentation_coverage": self.presentation_coverage,
            "scanner_truncated": self.scanner_truncated,
            "scanner_truncation_reasons": list(self.scanner_truncation_reasons),
            "presentation_truncated": False,
            "limits": {
                "max_entries": self.max_entries,
                "max_depth": self.max_depth,
                "max_bytes": self.max_bytes,
                "entries": self.max_entries,
                "depth": self.max_depth,
                "bytes": self.max_bytes,
            },
            "counts": self.counts,
            "aggregates": self.aggregates,
            "status_counts": dict(self.status_counts),
            "category_counts": dict(self.category_counts),
            "reason_counts": dict(self.reason_counts),
            "root_status_counts": dict(self.root_status_counts),
            "record_status_counts": dict(self.record_status_counts),
            "record_category_counts": dict(self.record_category_counts),
            "record_reason_counts": dict(self.record_reason_counts),
            "root_marker_status_counts": dict(self.root_marker_status_counts),
            "root_marker_category_counts": dict(self.root_marker_category_counts),
            "root_marker_reason_counts": dict(self.root_marker_reason_counts),
            "reason_summary": self.reason_summary,
            "reason_explanations": self.reason_explanations,
            "categories": self.categories,
            "category_registry": self.category_registry,
            "owners": self.owners,
            "owner_registry": self.owner_registry,
            "provenance": self.provenance,
            "provenance_registry": self.provenance_registry,
            "bytes": self.bytes,
            "byte_semantics": self.byte_semantics,
            "observed_bytes": self.observed_bytes,
            "observed_apparent_bytes": self.observed_apparent_bytes,
            "observed_allocated_bytes": self.observed_allocated_bytes,
            "scanned": self.scanned,
            "returned": self.returned,
            "entries": self.entries_returned,
            "entries_returned": self.entries_returned,
            "records_returned": self.records_returned,
            "records_scanned": self.records_scanned,
            "records_omitted": self.records_omitted,
            "records_omitted_known": self.records_omitted_known,
            "root_count": self.root_count,
            "root_quota_policy": self.root_quota_policy,
            "root_entry_quotas": list(self.root_entry_quotas),
            "entry_budget": self.entry_budget,
            "root_summaries": list(self.root_summaries),
            "coverage_metadata": self.coverage_metadata,
            "omissions": self.omissions,
            "roots": [root.to_dict() for root in self.roots],
            "records": [record.to_dict() for record in self.records],
        }


MachineInventoryResult = MachineInventoryReport


@dataclass(slots=True)
class _Budget:
    max_entries: int
    max_bytes: int
    entries: int = 0
    root_entries: int = 0
    entry_quota: int | None = None
    entry_limit_scope: Literal["global", "root"] | None = None
    apparent_bytes: int = 0
    allocated_bytes: int = 0
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)

    @property
    def observed_bytes(self) -> int:
        return self.apparent_bytes + self.allocated_bytes

    def note(self, code: str) -> None:
        self.truncated = True
        if code not in self.truncation_reasons:
            self.truncation_reasons.append(code)

    def begin_root(self, entry_quota: int) -> None:
        """Set the local entry share for the next root observation."""

        self.root_entries = 0
        self.entry_quota = entry_quota
        self.entry_limit_scope = None

    @property
    def root_quota_exhausted(self) -> bool:
        return self.entry_quota is not None and self.root_entries >= self.entry_quota

    def reserve(self) -> bool:
        self.entry_limit_scope = None
        if self.entries >= self.max_entries:
            self.entry_limit_scope = "global"
            self.note(ENTRY_LIMIT)
            return False
        if self.entry_quota is not None and self.root_entries >= self.entry_quota:
            self.entry_limit_scope = "root"
            return False
        self.entries += 1
        self.root_entries += 1
        return True

    def account(self, metadata: os.stat_result) -> tuple[int, int, int, bool]:
        apparent, allocated = _raw_sizes(metadata)
        remaining = max(0, self.max_bytes - self.observed_bytes)
        apparent_credit = min(apparent, remaining)
        allocated_credit = min(allocated, max(0, remaining - apparent_credit))
        credited = apparent_credit + allocated_credit
        bounded = credited < apparent + allocated
        if bounded:
            self.note(BYTE_LIMIT)
        self.apparent_bytes += apparent_credit
        self.allocated_bytes += allocated_credit
        return apparent_credit, allocated_credit, credited, bounded


@dataclass(slots=True)
class _ScanContext:
    root: MachineInventoryRoot
    category: MachineInventoryCategorySpec
    root_identity: tuple[int, int, int]
    root_device: int
    root_index: int
    budget: _Budget
    max_depth: int
    cancelled: Callable[[], bool] | None
    records: list[MachineInventoryRecord]
    issue_code: str | None = None
    issue: str | None = None
    local_truncation_reasons: list[str] = field(default_factory=list)

    def stop(
        self,
        code: str,
        reason: str,
        *,
        truncation: bool = False,
        global_stop: bool = True,
    ) -> None:
        if truncation:
            if global_stop:
                self.budget.note(code)
            elif code not in self.local_truncation_reasons:
                self.local_truncation_reasons.append(code)
        if self.issue_code is None:
            self.issue_code = code
            self.issue = _safe_reason(reason)


def _resolved_category(root: MachineInventoryRoot) -> MachineInventoryCategorySpec:
    spec = machine_inventory_category_spec(root.category)
    profile = spec.profile if root.profile is None else root.profile
    if profile not in MACHINE_INVENTORY_STATUSES:
        # Custom profiles are accepted as descriptive input, but they cannot
        # silently acquire a trusted status.  They remain outside the profile.
        profile = "out_of_profile"
    return MachineInventoryCategorySpec(
        name=spec.name,
        owner=spec.owner if root.owner is None else root.owner,
        provenance=spec.provenance if root.provenance is None else root.provenance,
        profile=profile,  # type: ignore[arg-type]
        description=spec.description,
    )


def _base_classification(spec: MachineInventoryCategorySpec) -> tuple[MachineInventoryStatus, str, str]:
    profile = spec.profile
    if profile == "preserved":
        return "preserved", PRESERVED_OWNER, "category is retained by its owning subsystem"
    if profile == "observed":
        return "observed", DIAGNOSTIC_ONLY, "category is observed only; its owner is external"
    if profile == "unknown":
        return "unknown", SCHEMA_UNVERIFIED, "category profile is not sufficiently verified"
    if profile == "out_of_profile":
        return "out_of_profile", OUT_OF_PROFILE, "category is outside the NeoCortex profile"
    return "blocked", "invalid_profile", "category profile is invalid"


def _relative(components: tuple[str, ...]) -> str:
    return "/".join(components)


def _path_for(root: Path, components: tuple[str, ...]) -> Path:
    path = root
    for component in components:
        path /= component
    return path


def _record(
    context: _ScanContext,
    *,
    path: Path,
    components: tuple[str, ...],
    metadata: os.stat_result | None,
    depth: int,
    credits: tuple[int, int, int, bool] | None,
    status: MachineInventoryStatus | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
) -> MachineInventoryRecord:
    if metadata is None:
        return MachineInventoryRecord(
            path=path,
            root_path=context.root.path,
            root_index=context.root_index,
            relative_path=_relative(components),
            name=components[-1] if components else path.name,
            category=context.category.name,
            owner=context.category.owner,
            provenance=context.category.provenance,
            profile=context.category.profile,
            status=status or "unknown",
            reason_code=reason_code or ENTRY_IDENTITY_UNAVAILABLE,
            reason=reason or "entry identity could not be observed",
            owner_class="unknown",
            path_identity=None,
            root_identity=context.root_identity,
            observed_uid=None,
            observed_gid=None,
            mode=None,
            nlink=None,
            apparent_bytes=0,
            allocated_bytes=0,
            observed_bytes=0,
            depth=depth,
            file_type="unknown",
            is_directory=False,
            truncated=False,
        )
    apparent, allocated, observed, bounded = credits or (0, 0, 0, False)
    base_status, base_code, base_reason = _base_classification(context.category)
    final_status = status or base_status
    final_code = reason_code or base_code
    final_reason = base_reason if reason is None else reason
    uid = int(metadata.st_uid)
    gid = int(metadata.st_gid)
    return MachineInventoryRecord(
        path=path,
        root_path=context.root.path,
        root_index=context.root_index,
        relative_path=_relative(components),
        name=components[-1] if components else path.name,
        category=context.category.name,
        owner=context.category.owner,
        provenance=context.category.provenance,
        profile=context.category.profile,
        status=final_status,
        reason_code=final_code,
        reason=_safe_reason(final_reason),
        owner_class=_owner_class(uid),
        path_identity=_identity(metadata),
        root_identity=context.root_identity,
        observed_uid=uid,
        observed_gid=gid,
        mode=_mode(metadata),
        nlink=int(getattr(metadata, "st_nlink", 0)),
        apparent_bytes=apparent,
        allocated_bytes=allocated,
        observed_bytes=observed,
        depth=depth,
        file_type=_file_type(metadata),
        is_directory=stat.S_ISDIR(metadata.st_mode),
        truncated=bounded,
    )


def _replace_record(record: MachineInventoryRecord, **changes: object) -> MachineInventoryRecord:
    return replace(record, **cast(Any, changes))


def _bounded_names(descriptor: int, limit: int) -> tuple[tuple[str, ...], bool]:
    names: list[str] = []
    overflow = False
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            if len(names) >= limit:
                overflow = True
                break
            names.append(entry.name)
    names.sort(key=os.fsencode)
    return tuple(names), overflow


def _has_child(descriptor: int) -> bool:
    names, overflow = _bounded_names(descriptor, 1)
    return bool(names) or overflow


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _inspect_entry(
    context: _ScanContext,
    descriptor: int,
    components: tuple[str, ...],
    depth: int,
) -> None:
    if context.cancelled is not None and context.cancelled():
        context.stop(CANCELLED, "machine inventory was cancelled", truncation=True)
        return
    if not context.budget.reserve():
        context.stop(
            ENTRY_LIMIT,
            "machine inventory entry limit exceeded",
            truncation=True,
            global_stop=context.budget.entry_limit_scope != "root",
        )
        return
    name = components[-1]
    path = _path_for(context.root.path, components)
    try:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        context.records.append(
            _record(
                context,
                path=path,
                components=components,
                metadata=None,
                depth=depth,
                credits=None,
                status="unknown",
                reason_code=ENTRY_DISAPPEARED,
                reason="entry disappeared during the bounded observation",
            )
        )
        return
    except OSError as exc:
        permission = exc.errno in {errno.EACCES, errno.EPERM}
        context.records.append(
            _record(
                context,
                path=path,
                components=components,
                metadata=None,
                depth=depth,
                credits=None,
                status="blocked" if permission else "unknown",
                reason_code=ROOT_PERMISSION_DENIED if permission else ENTRY_IDENTITY_UNAVAILABLE,
                reason=_safe_reason(f"entry metadata unavailable: {exc}"),
            )
        )
        return

    credits = context.budget.account(metadata)
    is_directory = stat.S_ISDIR(metadata.st_mode)
    status, code, reason = _base_classification(context.category)
    if stat.S_ISLNK(metadata.st_mode):
        status, code, reason = "blocked", ENTRY_SYMLINK, "symlink payload is never followed"
    elif int(metadata.st_dev) != context.root_device:
        status, code, reason = (
            "blocked",
            ENTRY_MOUNT_BOUNDARY,
            "entry crosses the root filesystem boundary",
        )
    elif stat.S_ISREG(metadata.st_mode) and int(metadata.st_nlink) > 1:
        status, code, reason = (
            "blocked",
            ENTRY_HARDLINK,
            "hardlinked payload is shared with another name",
        )
    elif not is_directory and not stat.S_ISREG(metadata.st_mode):
        status, code, reason = (
            "blocked",
            ENTRY_NON_REGULAR,
            "non-regular payload is outside the safe metadata profile",
        )
    record = _record(
        context,
        path=path,
        components=components,
        metadata=metadata,
        depth=depth,
        credits=credits,
        status=status,
        reason_code=code,
        reason=reason,
    )
    context.records.append(record)
    if credits[3]:
        # Keep the record that consumed the final byte credit visible, while
        # stopping before any later entry can exceed the global byte budget.
        context.stop(BYTE_LIMIT, "machine inventory byte limit exceeded", truncation=True)
        return
    if context.budget.truncated or not is_directory:
        return
    if depth >= context.max_depth:
        child_fd: int | None = None
        try:
            child_fd = os.open(name, _directory_flags(), dir_fd=descriptor)
            has_child = _has_child(child_fd)
            if has_child and context.budget.root_quota_exhausted:
                context.records[-1] = _replace_record(record, truncated=True)
                context.stop(
                    ENTRY_LIMIT,
                    "machine inventory root entry quota exceeded",
                    truncation=True,
                    global_stop=False,
                )
            elif has_child:
                context.records[-1] = _replace_record(record, truncated=True)
                context.stop(
                    DEPTH_LIMIT,
                    "machine inventory depth limit exceeded",
                    truncation=True,
                    global_stop=False,
                )
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                context.records[-1] = _replace_record(
                    record,
                    status="blocked",
                    reason_code=ROOT_PERMISSION_DENIED,
                    reason="directory depth could not be checked due to permissions",
                )
                context.stop(
                    ROOT_PERMISSION_DENIED,
                    "directory depth could not be checked due to permissions",
                )
            else:
                context.stop(
                    SCAN_UNAVAILABLE,
                    _safe_reason(f"directory depth could not be checked: {exc}"),
                )
        finally:
            if child_fd is not None:
                os.close(child_fd)
        return
    child_fd = None
    try:
        child_fd = os.open(name, _directory_flags(), dir_fd=descriptor)
    except OSError as exc:
        context.records[-1] = _replace_record(
            record,
            status="blocked",
            reason_code=ROOT_PERMISSION_DENIED
            if exc.errno in {errno.EACCES, errno.EPERM}
            else ENTRY_IDENTITY_UNAVAILABLE,
            reason=_safe_reason(f"directory could not be inspected: {exc}"),
        )
        return
    try:
        opened = os.fstat(child_fd)
        if _identity(opened) != _identity(metadata):
            context.records[-1] = _replace_record(
                record,
                status="unknown",
                reason_code=ENTRY_IDENTITY_CHANGED,
                reason="directory identity changed before descent",
            )
            return
        _scan_directory(context, child_fd, components, depth + 1)
    except OSError as exc:
        context.stop(
            ROOT_PERMISSION_DENIED if exc.errno in {errno.EACCES, errno.EPERM} else SCAN_UNAVAILABLE,
            _safe_reason(f"directory observation failed: {exc}"),
        )
    finally:
        os.close(child_fd)


def _scan_directory(
    context: _ScanContext,
    descriptor: int,
    components: tuple[str, ...],
    depth: int,
) -> None:
    if context.cancelled is not None and context.cancelled():
        context.stop(CANCELLED, "machine inventory was cancelled", truncation=True)
        return
    global_remaining = context.budget.max_entries - context.budget.entries
    if global_remaining <= 0:
        context.stop(ENTRY_LIMIT, "machine inventory entry limit exceeded", truncation=True)
        return
    local_remaining = global_remaining
    if context.budget.entry_quota is not None:
        local_remaining = context.budget.entry_quota - context.budget.root_entries
        if local_remaining <= 0:
            # Checking for a child does not reserve an entry and lets an
            # exactly-sized root remain complete when it has no more names.
            # If a name exists, the local quota is the boundary; do not mark
            # the invocation-wide budget as exhausted.
            try:
                if _has_child(descriptor):
                    context.stop(
                        ENTRY_LIMIT,
                        "machine inventory root entry quota exceeded",
                        truncation=True,
                        global_stop=False,
                    )
            except OSError as exc:
                context.stop(
                    ROOT_PERMISSION_DENIED
                    if exc.errno in {errno.EACCES, errno.EPERM}
                    else SCAN_UNAVAILABLE,
                    _safe_reason(f"directory could not be inspected: {exc}"),
                )
            return
    remaining = min(global_remaining, local_remaining)
    try:
        names, overflow = _bounded_names(descriptor, remaining)
    except OSError as exc:
        context.stop(
            ROOT_PERMISSION_DENIED if exc.errno in {errno.EACCES, errno.EPERM} else SCAN_UNAVAILABLE,
            _safe_reason(f"directory could not be inspected: {exc}"),
        )
        return
    for name in names:
        _inspect_entry(context, descriptor, (*components, name), depth)
        # A depth or availability issue in one child is local to that child;
        # continue with its siblings.  Entry-limit issues, in contrast, end
        # this root's share, while a global budget truncation ends the scan.
        if context.budget.truncated or context.issue_code == ENTRY_LIMIT:
            return
    if overflow:
        local_boundary = (
            context.budget.entry_quota is not None
            and local_remaining < global_remaining
        )
        context.stop(
            ENTRY_LIMIT,
            "machine inventory root entry quota exceeded"
            if local_boundary
            else "machine inventory entry limit exceeded",
            truncation=True,
            global_stop=not local_boundary,
        )


def _path_components_have_no_symlinks(path: Path) -> None:
    """Reject a root whose existing parent component is a symlink."""

    parts = path.parts
    if len(parts) <= 1:
        return
    cursor = Path(parts[0])
    for component in parts[1:-1]:
        cursor /= component
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise MachineInventoryRootError(
                _safe_reason(f"root path is unavailable: {exc}")
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise MachineInventoryRootError(ROOT_PATH_SYMLINK)


def _root_counts(records: tuple[MachineInventoryRecord, ...]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(MACHINE_INVENTORY_STATUSES, 0)
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    return counts


def _aggregate_status(
    statuses: Iterable[MachineInventoryStatus],
    *,
    fallback: MachineInventoryStatus,
) -> tuple[MachineInventoryStatus, str, str]:
    values = set(statuses)
    if not values:
        if fallback == "absent":
            return "absent", ROOT_ABSENT, "explicit root is absent"
        return fallback, _base_reason_code(fallback), _base_reason(fallback)
    if "blocked" in values:
        return "blocked", "entry_blocked", "one or more entries failed a safe metadata boundary"
    if "unknown" in values:
        return "unknown", EVIDENCE_INCOMPLETE, "one or more entries lack complete evidence"
    if "absent" in values and len(values) == 1:
        return "absent", ROOT_ABSENT, "explicit root is absent"
    if len(values) == 1:
        value = next(iter(values))
        return value, _base_reason_code(value), _base_reason(value)
    return "observed", MIXED_OBSERVATIONS, "bounded observation contains multiple classifications"


def _base_reason_code(status: MachineInventoryStatus) -> str:
    return {
        "observed": DIAGNOSTIC_ONLY,
        "preserved": PRESERVED_OWNER,
        "out_of_profile": OUT_OF_PROFILE,
        "unknown": EVIDENCE_INCOMPLETE,
        "blocked": "entry_blocked",
        "absent": ROOT_ABSENT,
    }[status]


def _base_reason(status: MachineInventoryStatus) -> str:
    return {
        "observed": "category is observed only; its owner is external",
        "preserved": "category is retained by its owning subsystem",
        "out_of_profile": "category is outside the NeoCortex profile",
        "unknown": "bounded observation lacks complete evidence",
        "blocked": "bounded observation encountered a blocked boundary",
        "absent": "explicit root is absent",
    }[status]


def _make_root_result(
    *,
    root: MachineInventoryRoot,
    root_index: int,
    spec: MachineInventoryCategorySpec,
    records: Iterable[MachineInventoryRecord],
    issue_code: str | None,
    issue: str | None,
    root_metadata: os.stat_result | None,
    root_identity: tuple[int, int, int] | None,
    root_exists: bool,
    truncation_reasons: tuple[str, ...],
    truncated: bool,
    max_entries: int = 0,
    max_depth: int = 0,
    max_bytes: int = 0,
    entry_quota: int = 0,
) -> MachineInventoryRootResult:
    frozen_records = tuple(records)
    base_status, _, _ = _base_classification(spec)
    status, reason_code, reason_text = _aggregate_status(
        (record.status for record in frozen_records),
        fallback=base_status,
    )
    reason: str | None = reason_text
    if issue_code is not None:
        if issue_code == ROOT_ABSENT:
            status = "absent"
            reason_code = ROOT_ABSENT
            reason = issue or "explicit root is absent"
        elif issue_code in {
            ROOT_PERMISSION_DENIED,
            ROOT_SYMLINK,
            ROOT_PATH_SYMLINK,
            ROOT_NOT_DIRECTORY,
            ROOT_UNAVAILABLE,
        }:
            status = "blocked"
            reason_code = issue_code
            reason = issue
        elif issue_code in {ENTRY_LIMIT, DEPTH_LIMIT, BYTE_LIMIT, CANCELLED}:
            status = "unknown"
            reason_code = issue_code
            reason = issue
        else:
            status = "unknown"
            reason_code = issue_code
            reason = issue
    counts = _root_counts(frozen_records)
    root_type = "unknown" if root_metadata is None else _file_type(root_metadata)
    return MachineInventoryRootResult(
        path=root.path,
        root_index=root_index,
        category=spec.name,
        owner=spec.owner,
        provenance=spec.provenance,
        profile=spec.profile,
        status=status,
        reason_code=reason_code,
        reason=reason,
        records=frozen_records,
        scanned=len(frozen_records),
        observed=counts["observed"],
        preserved=counts["preserved"],
        blocked=counts["blocked"],
        unknown=counts["unknown"],
        out_of_profile=counts["out_of_profile"],
        absent=1 if status == "absent" else counts["absent"],
        apparent_bytes=sum(record.apparent_bytes for record in frozen_records),
        allocated_bytes=sum(record.allocated_bytes for record in frozen_records),
        observed_bytes=sum(record.observed_bytes for record in frozen_records),
        truncated=truncated,
        truncation_reasons=truncation_reasons,
        root_identity=root_identity,
        root_uid=None if root_metadata is None else int(root_metadata.st_uid),
        root_gid=None if root_metadata is None else int(root_metadata.st_gid),
        root_mode=_mode(root_metadata),
        root_nlink=None if root_metadata is None else int(root_metadata.st_nlink),
        root_exists=root_exists,
        root_type=root_type,
        root_is_directory=False if root_metadata is None else stat.S_ISDIR(root_metadata.st_mode),
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
        entry_quota=entry_quota,
    )


def _root_empty_issue(
    root: MachineInventoryRoot,
    index: int,
    spec: MachineInventoryCategorySpec,
    *,
    code: str,
    reason: str,
    metadata: os.stat_result | None = None,
    identity: tuple[int, int, int] | None = None,
    exists: bool = False,
    truncation_reasons: tuple[str, ...] = (),
    truncated: bool = False,
    max_entries: int = 0,
    max_depth: int = 0,
    max_bytes: int = 0,
    entry_quota: int = 0,
) -> MachineInventoryRootResult:
    return _make_root_result(
        root=root,
        root_index=index,
        spec=spec,
        records=(),
        issue_code=code,
        issue=reason,
        root_metadata=metadata,
        root_identity=identity,
        root_exists=exists,
        truncation_reasons=truncation_reasons,
        truncated=truncated,
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
        entry_quota=entry_quota,
    )


def _equal_fair_share_quota(remaining_entries: int, roots_remaining: int) -> int:
    """Return the next root's deterministic share of remaining entries."""

    if remaining_entries <= 0 or roots_remaining <= 0:
        return 0
    return (remaining_entries + roots_remaining - 1) // roots_remaining


def _scan_root(
    root: MachineInventoryRoot,
    index: int,
    *,
    budget: _Budget,
    entry_quota: int,
    max_depth: int,
    cancelled: Callable[[], bool] | None,
) -> MachineInventoryRootResult:
    budget.begin_root(entry_quota)
    spec = _resolved_category(root)
    try:
        _path_components_have_no_symlinks(root.path)
    except MachineInventoryRootError as exc:
        return _root_empty_issue(
            root,
            index,
            spec,
            code=ROOT_PATH_SYMLINK,
            reason=_safe_reason(exc),
            max_entries=budget.max_entries,
            max_depth=max_depth,
            max_bytes=budget.max_bytes,
            entry_quota=entry_quota,
        )
    try:
        metadata = os.lstat(root.path)
    except FileNotFoundError:
        return _root_empty_issue(
            root,
            index,
            spec,
            code=ROOT_ABSENT,
            reason="explicit root is absent",
            max_entries=budget.max_entries,
            max_depth=max_depth,
            max_bytes=budget.max_bytes,
            entry_quota=entry_quota,
        )
    except OSError as exc:
        permission = exc.errno in {errno.EACCES, errno.EPERM}
        return _root_empty_issue(
            root,
            index,
            spec,
            code=ROOT_PERMISSION_DENIED if permission else ROOT_UNAVAILABLE,
            reason=_safe_reason(f"explicit root is unavailable: {exc}"),
            max_entries=budget.max_entries,
            max_depth=max_depth,
            max_bytes=budget.max_bytes,
            entry_quota=entry_quota,
        )
    identity = _identity(metadata)
    if stat.S_ISLNK(metadata.st_mode):
        return _root_empty_issue(
            root,
            index,
            spec,
            code=ROOT_SYMLINK,
            reason="explicit root must not be a symlink",
            metadata=metadata,
            identity=identity,
            exists=True,
            max_entries=budget.max_entries,
            max_depth=max_depth,
            max_bytes=budget.max_bytes,
            entry_quota=entry_quota,
        )
    if not stat.S_ISDIR(metadata.st_mode):
        return _root_empty_issue(
            root,
            index,
            spec,
            code=ROOT_NOT_DIRECTORY,
            reason="explicit root must be a directory",
            metadata=metadata,
            identity=identity,
            exists=True,
            max_entries=budget.max_entries,
            max_depth=max_depth,
            max_bytes=budget.max_bytes,
            entry_quota=entry_quota,
        )
    global_truncation = next(
        (
            code
            for code in budget.truncation_reasons
            if code in {ENTRY_LIMIT, BYTE_LIMIT, CANCELLED}
        ),
        None,
    )
    if global_truncation is not None:
        # Preserve a summary for every requested root without attempting a
        # metadata entry after a global byte/entry/cancellation fence fired.
        return _root_empty_issue(
            root,
            index,
            spec,
            code=global_truncation,
            reason=f"machine inventory global budget already bounded by {global_truncation}",
            metadata=metadata,
            identity=identity,
            exists=True,
            truncation_reasons=(global_truncation,),
            truncated=True,
            max_entries=budget.max_entries,
            max_depth=max_depth,
            max_bytes=budget.max_bytes,
            entry_quota=entry_quota,
        )
    if entry_quota <= 0:
        # Still return the root's no-follow identity and classification, but
        # do not enumerate any entry when the global budget has no share left.
        return _root_empty_issue(
            root,
            index,
            spec,
            code=ENTRY_LIMIT,
            reason="machine inventory root entry quota is zero",
            metadata=metadata,
            identity=identity,
            exists=True,
            truncation_reasons=(ENTRY_LIMIT,),
            truncated=True,
            max_entries=budget.max_entries,
            max_depth=max_depth,
            max_bytes=budget.max_bytes,
            entry_quota=entry_quota,
        )
    try:
        descriptor = os.open(root.path, _directory_flags())
    except OSError as exc:
        permission = exc.errno in {errno.EACCES, errno.EPERM}
        return _root_empty_issue(
            root,
            index,
            spec,
            code=ROOT_PERMISSION_DENIED if permission else ROOT_UNAVAILABLE,
            reason=_safe_reason(f"explicit root cannot be opened: {exc}"),
            metadata=metadata,
            identity=identity,
            exists=True,
            max_entries=budget.max_entries,
            max_depth=max_depth,
            max_bytes=budget.max_bytes,
            entry_quota=entry_quota,
        )

    start_reasons = len(budget.truncation_reasons)
    records: list[MachineInventoryRecord] = []
    context = _ScanContext(
        root=root,
        category=spec,
        root_identity=identity,
        root_device=int(metadata.st_dev),
        root_index=index,
        budget=budget,
        max_depth=max_depth,
        cancelled=cancelled,
        records=records,
    )
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != identity:
            context.stop(ROOT_IDENTITY_CHANGED, "root identity changed before observation")
        elif cancelled is not None and cancelled():
            context.stop(CANCELLED, "machine inventory was cancelled", truncation=True)
        elif budget.entries >= budget.max_entries:
            context.stop(ENTRY_LIMIT, "machine inventory entry limit exceeded", truncation=True)
        else:
            _scan_directory(context, descriptor, (), 1)
        final = os.fstat(descriptor)
        if _identity(final) != identity:
            context.stop(ROOT_IDENTITY_CHANGED, "root identity changed during observation")
    except OSError as exc:
        context.stop(
            ROOT_PERMISSION_DENIED if exc.errno in {errno.EACCES, errno.EPERM} else SCAN_UNAVAILABLE,
            _safe_reason(f"root observation failed: {exc}"),
        )
    finally:
        os.close(descriptor)
    local_reasons = tuple(
        dict.fromkeys(
            (
                *budget.truncation_reasons[start_reasons:],
                *context.local_truncation_reasons,
            )
        )
    )
    if context.issue_code in {ENTRY_LIMIT, DEPTH_LIMIT, BYTE_LIMIT, CANCELLED} and not local_reasons:
        local_reasons = (context.issue_code,)
    return _make_root_result(
        root=root,
        root_index=index,
        spec=spec,
        records=records,
        issue_code=context.issue_code,
        issue=context.issue,
        root_metadata=metadata,
        root_identity=identity,
        root_exists=True,
        truncation_reasons=local_reasons,
        truncated=bool(local_reasons),
        max_entries=budget.max_entries,
        max_depth=max_depth,
        max_bytes=budget.max_bytes,
        entry_quota=entry_quota,
    )


def _validate_limits(*, max_entries: int, max_depth: int, max_bytes: int) -> tuple[int, int, int]:
    for label, value in (
        ("max_entries", max_entries),
        ("max_depth", max_depth),
        ("max_bytes", max_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a non-negative integer")
    if not 1 <= max_entries <= MAX_MACHINE_INVENTORY_ENTRIES:
        raise ValueError(f"max_entries must be between 1 and {MAX_MACHINE_INVENTORY_ENTRIES}")
    if max_depth > MAX_MACHINE_INVENTORY_DEPTH:
        raise ValueError(f"max_depth must be at most {MAX_MACHINE_INVENTORY_DEPTH}")
    if max_bytes > MAX_MACHINE_INVENTORY_BYTES:
        raise ValueError(f"max_bytes must be at most {MAX_MACHINE_INVENTORY_BYTES}")
    return max_entries, max_depth, max_bytes


def _build_report(
    roots: tuple[MachineInventoryRootResult, ...],
    *,
    budget: _Budget,
    max_entries: int,
    max_depth: int,
    max_bytes: int,
) -> MachineInventoryReport:
    records = tuple(record for root in roots for record in root.records)
    root_status_counts = Counter(root.status for root in roots)
    statuses_for_aggregate: list[MachineInventoryStatus] = [root.status for root in roots]
    status: MachineInventoryStatus
    reason_code: str
    reason: str | None
    if not roots:
        status, reason_code, reason = "unknown", NO_ROOTS, "no inventory roots were supplied"
    else:
        status, reason_code, reason = _aggregate_status(
            statuses_for_aggregate,
            fallback="unknown",
        )
        if len(roots) == 1 and not roots[0].records:
            # Preserve a precise root boundary for the common single-root
            # diagnostic instead of collapsing it into ``entry_blocked`` or
            # ``evidence_incomplete``.
            status = roots[0].status
            reason_code = roots[0].reason_code
            reason = roots[0].reason
        root_truncation = next(
            (
                code
                for root in roots
                for code in root.truncation_reasons
            ),
            None,
        )
        if status == "unknown" and (budget.truncated or root_truncation is not None):
            # The first budget/root reason is the most actionable coverage
            # cause; depth is local to a root while entry/byte limits are
            # global to the invocation.
            code = (
                budget.truncation_reasons[0]
                if budget.truncation_reasons
                else root_truncation or reason_code
            )
            reason_code = code
            reason = f"machine inventory coverage is bounded by {code}"
    # Keep record-only counters immutable while building the historical
    # aggregate counters below.  The latter may include synthetic root
    # markers for an absent/blocked/bounded root and therefore must not be
    # mistaken for entry observations.
    record_status_counts_counter: Counter[str] = Counter(record.status for record in records)
    record_category_counts_counter: Counter[str] = Counter(record.category for record in records)
    record_reason_counts_counter: Counter[str] = Counter(record.reason_code for record in records)
    status_counts_counter: Counter[str] = record_status_counts_counter.copy()
    category_counts_counter: Counter[str] = record_category_counts_counter.copy()
    reason_counts_counter: Counter[str] = record_reason_counts_counter.copy()
    root_marker_status_counts_counter: Counter[str] = Counter()
    root_marker_category_counts_counter: Counter[str] = Counter()
    root_marker_reason_counts_counter: Counter[str] = Counter()
    # Root-level absence/blocking is evidence even though a root itself is not
    # emitted as an entry record.  Empty successful roots remain zero-record
    # observations and are not double-counted here.
    for root in roots:
        if not root.records and root.status in {"absent", "blocked", "unknown"}:
            root_marker_status_counts_counter[root.status] += 1
            root_marker_category_counts_counter[root.category] += 1
            root_marker_reason_counts_counter[root.reason_code] += 1
            status_counts_counter[root.status] += 1
            category_counts_counter[root.category] += 1
            reason_counts_counter[root.reason_code] += 1
        elif root.truncated and root.reason_code not in {
            "diagnostic_only",
            "preserved_owner",
            "out_of_profile",
        }:
            # A root-level limit is distinct from its entry classifications;
            # preserve every reported truncation reason so a partial scan
            # cannot look complete in the aggregate reason summary.  The
            # primary ``reason_code`` is not necessarily the only boundary
            # (for example, a root can hit both depth and entry quota).
            reasons = root.truncation_reasons or (root.reason_code,)
            for code in dict.fromkeys(reasons):
                root_marker_reason_counts_counter[code] += 1
                reason_counts_counter[code] += 1
    status_counts: dict[str, int] = {
        status_name: int(status_counts_counter.get(status_name, 0))
        for status_name in MACHINE_INVENTORY_STATUSES
    }
    category_counts = dict(category_counts_counter)
    reason_counts = dict(reason_counts_counter)
    root_status_counts_payload: dict[str, int] = {
        status_name: int(root_status_counts.get(status_name, 0))
        for status_name in MACHINE_INVENTORY_STATUSES
    }
    record_status_counts: dict[str, int] = {
        status_name: int(record_status_counts_counter.get(status_name, 0))
        for status_name in MACHINE_INVENTORY_STATUSES
    }
    record_category_counts = dict(record_category_counts_counter)
    record_reason_counts = dict(record_reason_counts_counter)
    root_marker_status_counts: dict[str, int] = {
        status_name: int(root_marker_status_counts_counter.get(status_name, 0))
        for status_name in MACHINE_INVENTORY_STATUSES
    }
    root_marker_category_counts = dict(root_marker_category_counts_counter)
    root_marker_reason_counts = dict(root_marker_reason_counts_counter)
    truncation_reasons: list[str] = list(budget.truncation_reasons)
    for root in roots:
        for code in root.truncation_reasons:
            if code not in truncation_reasons:
                truncation_reasons.append(code)
    truncated = budget.truncated or any(root.truncated for root in roots)
    return MachineInventoryReport(
        roots=roots,
        records=records,
        status=status,
        reason_code=reason_code,
        reason=reason,
        scanned=budget.entries,
        root_count=len(roots),
        observed=status_counts["observed"],
        preserved=status_counts["preserved"],
        blocked=status_counts["blocked"],
        unknown=status_counts["unknown"],
        out_of_profile=status_counts["out_of_profile"],
        absent=status_counts["absent"],
        apparent_bytes=budget.apparent_bytes,
        allocated_bytes=budget.allocated_bytes,
        observed_bytes=budget.observed_bytes,
        truncated=truncated,
        truncation_reasons=tuple(truncation_reasons),
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
        status_counts=MappingProxyType(status_counts),
        category_counts=MappingProxyType(category_counts),
        reason_counts=MappingProxyType(reason_counts),
        root_status_counts=MappingProxyType(root_status_counts_payload),
        record_status_counts=MappingProxyType(record_status_counts),
        record_category_counts=MappingProxyType(record_category_counts),
        record_reason_counts=MappingProxyType(record_reason_counts),
        root_marker_status_counts=MappingProxyType(root_marker_status_counts),
        root_marker_category_counts=MappingProxyType(root_marker_category_counts),
        root_marker_reason_counts=MappingProxyType(root_marker_reason_counts),
    )


class MachineInventory:
    """Collect one bounded, read-only inventory across repeatable roots."""

    def __init__(
        self,
        roots: RootInput | Iterable[RootInput] | None = None,
        *,
        include_defaults: bool = False,
        max_entries: int = DEFAULT_MACHINE_INVENTORY_MAX_ENTRIES,
        max_depth: int = DEFAULT_MACHINE_INVENTORY_MAX_DEPTH,
        max_bytes: int = DEFAULT_MACHINE_INVENTORY_MAX_BYTES,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.max_entries, self.max_depth, self.max_bytes = _validate_limits(
            max_entries=max_entries,
            max_depth=max_depth,
            max_bytes=max_bytes,
        )
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable or None")
        if roots is None:
            selected = default_machine_inventory_roots()
        else:
            selected = _coerce_roots(roots)
            if include_defaults:
                selected = (*default_machine_inventory_roots(), *selected)
        self.roots = tuple(selected)
        self.cancelled = cancelled

    @property
    def root_specs(self) -> tuple[MachineInventoryRoot, ...]:
        return self.roots

    def scan(self) -> MachineInventoryReport:
        """Return a bounded result without creating state or reading payloads."""

        budget = _Budget(self.max_entries, self.max_bytes)
        results: list[MachineInventoryRootResult] = []
        for index, root in enumerate(self.roots):
            remaining_entries = self.max_entries - budget.entries
            roots_remaining = len(self.roots) - index
            entry_quota = _equal_fair_share_quota(remaining_entries, roots_remaining)
            results.append(
                _scan_root(
                    root,
                    index,
                    budget=budget,
                    entry_quota=entry_quota,
                    max_depth=self.max_depth,
                    cancelled=self.cancelled,
                )
            )
        return _build_report(
            tuple(results),
            budget=budget,
            max_entries=self.max_entries,
            max_depth=self.max_depth,
            max_bytes=self.max_bytes,
        )

    # Descriptive aliases retain one implementation path.
    collect = scan
    inventory = scan
    plan = scan
    audit = scan


MachineInventoryManager = MachineInventory


def collect_machine_inventory(
    roots: RootInput | Iterable[RootInput] | None = None,
    *,
    include_defaults: bool = False,
    max_entries: int = DEFAULT_MACHINE_INVENTORY_MAX_ENTRIES,
    max_depth: int = DEFAULT_MACHINE_INVENTORY_MAX_DEPTH,
    max_bytes: int = DEFAULT_MACHINE_INVENTORY_MAX_BYTES,
    cancelled: Callable[[], bool] | None = None,
) -> MachineInventoryReport:
    """Collect a federated inventory for explicit roots or safe defaults."""

    # The CLI represents an omitted repeatable option as ``[]``.  Treat that
    # representation exactly like ``None`` so the public leaf selects the
    # conservative default profile instead of silently returning an empty
    # inventory.  A direct ``MachineInventory(roots=[])`` remains available
    # for callers that explicitly need an empty result.
    if isinstance(roots, (list, tuple)) and not roots:
        roots = None
    return MachineInventory(
        roots,
        include_defaults=include_defaults,
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
        cancelled=cancelled,
    ).scan()


scan_machine_inventory = collect_machine_inventory
inventory_machine = collect_machine_inventory
plan_machine_inventory = collect_machine_inventory
diagnose_machine_inventory = collect_machine_inventory


__all__ = [
    "BYTE_LIMIT",
    "CANCELLED",
    "CATEGORY_REGISTRY",
    "DEFAULT_MACHINE_INVENTORY_MAX_BYTES",
    "DEFAULT_MACHINE_INVENTORY_MAX_DEPTH",
    "DEFAULT_MACHINE_INVENTORY_MAX_ENTRIES",
    "DEPTH_LIMIT",
    "ENTRY_HARDLINK",
    "ENTRY_IDENTITY_CHANGED",
    "ENTRY_IDENTITY_UNAVAILABLE",
    "ENTRY_LIMIT",
    "ENTRY_MOUNT_BOUNDARY",
    "ENTRY_NON_REGULAR",
    "ENTRY_SYMLINK",
    "MACHINE_INVENTORY_BYTE_SEMANTICS",
    "MACHINE_INVENTORY_CATEGORIES",
    "MACHINE_INVENTORY_CATEGORIES_SPECS",
    "MACHINE_INVENTORY_CATEGORY_SPECS",
    "MACHINE_INVENTORY_ENTRY_QUOTA_POLICY",
    "MACHINE_INVENTORY_REASON_EXPLANATIONS",
    "MACHINE_INVENTORY_SCHEMA",
    "MACHINE_INVENTORY_STATUSES",
    "NO_ROOTS",
    "SUPPORTED_MACHINE_INVENTORY_CATEGORIES",
    "InventoryRoot",
    "MachineInventory",
    "MachineInventoryCategory",
    "MachineInventoryCategoryError",
    "MachineInventoryCategorySpec",
    "MachineInventoryCoverage",
    "MachineInventoryError",
    "MachineInventoryManager",
    "MachineInventoryProfile",
    "MachineInventoryRecord",
    "MachineInventoryReport",
    "MachineInventoryResult",
    "MachineInventoryRoot",
    "MachineInventoryRootError",
    "MachineInventoryRootReport",
    "MachineInventoryRootResult",
    "MachineInventoryRootSpec",
    "MachineInventorySchema",
    "MachineInventoryStatus",
    "category_spec",
    "collect_machine_inventory",
    "default_machine_inventory_paths",
    "default_machine_inventory_roots",
    "default_roots",
    "diagnose_machine_inventory",
    "filesystem_identity",
    "inventory_category_spec",
    "inventory_machine",
    "machine_inventory_category_spec",
    "plan_machine_inventory",
    "scan_machine_inventory",
]

# Backwards-friendly constant spelling; the canonical value remains the
# schema constant above.
MachineInventorySchema = MACHINE_INVENTORY_SCHEMA
