"""Bounded, read-only admission classification for mixed corpus files.

This module is deliberately smaller than any content route.  It answers only
whether one already-inventoried file may proceed to normal route processing or
must remain metadata-only.  It never walks a directory, parses source code,
mutates the corpus, or grants a physical effect.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from neocortex.code.code_contracts import ArtifactKind
from neocortex.code.ingestion.code_candidate_scope import ProjectCandidateScope
from neocortex.code.ingestion.code_detection import classify_artifact
from neocortex.deduplication import FileSnapshot, stat_matches_snapshot
from neocortex.deduplication.domain.errors import FileChangedError
from neocortex.deduplication.fingerprinting import (
    _open_regular_descriptor as _open_regular_descriptor_no_follow,
)


CORPUS_ADMISSION_SCHEMA: Final = "neocortex.corpus-admission-policy/v1"
MAX_PREFIX_BYTES: Final = 8192
AdmissionDisposition = Literal["process", "metadata_only", "sensitive"]

_CODE_SCOPES: Final[frozenset[str]] = frozenset({"projects", "broad"})

# These are source/script suffixes only.  In particular, document, data and
# configuration suffixes are intentionally absent: the mixed corpus may hold
# valuable Markdown, text, CSV, JSON and XML that must remain processable.
_SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".asm",
        ".bas",
        ".bat",
        ".bash",
        ".c",
        ".cc",
        ".clj",
        ".cljs",
        ".cpp",
        ".cs",
        ".css",
        ".cxx",
        ".dart",
        ".ex",
        ".exs",
        ".fs",
        ".fsx",
        ".go",
        ".groovy",
        ".h",
        ".h++",
        ".haskell",
        ".hh",
        ".hpp",
        ".hs",
        ".java",
        ".jl",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".m",
        ".mm",
        ".php",
        ".pl",
        ".pm",
        ".ps1",
        ".py",
        ".pyi",
        ".pyw",
        ".r",
        ".rb",
        ".rs",
        ".s",
        ".scala",
        ".sh",
        ".sql",
        ".swift",
        ".tcl",
        ".ts",
        ".tsx",
        ".vb",
        ".vbs",
        ".zig",
    }
)
_SCRIPT_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        "awk",
        "dockerfile",
        "gnumakefile",
        "jenkinsfile",
        "justfile",
        "makefile",
        "rakefile",
    }
)

_PACKAGE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".cargo",
        ".gradle",
        ".nuget",
        ".venv",
        "bower_components",
        "carthage",
        "dist-packages",
        "env",
        "gems",
        "node_modules",
        "packages",
        "pods",
        "site-packages",
        "third-party",
        "third_party",
        "vendor",
        "vendors",
        "venv",
    }
)
_RUNTIME_CONTEXT_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".appdata",
        ".cache",
        ".dotnet",
        "appdata",
        "bin",
        "cache",
        "caches",
        "local",
        "programs",
        "runtime",
        "runtimes",
    }
)
_NPM_ARCHIVE_CONTEXT_NAMES: Final[frozenset[str]] = frozenset(
    {".npm", "_cacache", "npm", "npm-cache", "npm_cache", "registry"}
)
_FIXTURE_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"fixture", "fixtures", "test_data", "testdata"}
)
_LICENSE_PREFIXES: Final[tuple[str, ...]] = (
    "license",
    "licence",
    "copying",
    "notice",
    "authors",
    "third-party-notices",
    "third_party_notices",
)
_SENSITIVE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".asc", ".gpg", ".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}
)
_SENSITIVE_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".env",
        "authorized_keys",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "passwd",
        "password",
        "secrets",
        "secrets.json",
        "token",
        "token.json",
    }
)
_PACKAGE_JSON_NAMES: Final[frozenset[str]] = frozenset(
    {
        "composer.json",
        "manifest.json",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "package.json",
        "pipfile.lock",
        "project.assets.json",
    }
)
_BINARY_SIGNATURES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\x7fELF", "elf"),
    (b"MZ", "pe"),
    (b"\xca\xfe\xba\xbe", "mach-o-or-class"),
    (b"\xcf\xfa\xed\xfe", "mach-o"),
    (b"\xfe\xed\xfa\xcf", "mach-o"),
    (b"\x00asm", "wasm"),
)
_DOCUMENT_SIGNATURES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
_ARCHIVE_SIGNATURES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"\x1f\x8b", "gzip"),
)
_JSON_LOCK_NAMES: Final[frozenset[str]] = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json"}
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _absolute_path(value: str | os.PathLike[str] | Path) -> Path:
    """Normalize lexically without resolving a symlink or touching the disk."""

    return Path(os.path.abspath(os.fspath(value)))


def _path_key(value: str | os.PathLike[str] | Path) -> str:
    return os.path.normcase(os.fspath(_absolute_path(value)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _relative_parts(path: Path, root: Path | None = None) -> tuple[str, ...]:
    candidate = path if root is None else Path(os.path.relpath(path, root))
    return tuple(part.casefold() for part in candidate.parts if part not in {"", "."})


def _parts(path: Path) -> tuple[str, ...]:
    return _relative_parts(path)


def _component_in(parts: tuple[str, ...], values: frozenset[str]) -> bool:
    return bool(set(parts).intersection(values))


def _evidence(*values: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class CorpusAdmissionPolicy:
    """Immutable, serializable policy for one mixed-corpus admission pass."""

    interested_roots: tuple[Path, ...]
    code_scope: str = "projects"
    include_generated: bool = False
    include_vendored: bool = False

    def __post_init__(self) -> None:
        if self.code_scope not in _CODE_SCOPES:
            raise ValueError("code_scope must be projects or broad")
        if not isinstance(self.include_generated, bool):
            raise ValueError("include_generated must be a boolean")
        if not isinstance(self.include_vendored, bool):
            raise ValueError("include_vendored must be a boolean")
        normalized: dict[str, Path] = {}
        for root in self.interested_roots:
            candidate = _absolute_path(root)
            normalized.setdefault(_path_key(candidate), candidate)
        ordered = tuple(normalized[key] for key in sorted(normalized))
        object.__setattr__(self, "interested_roots", ordered)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": CORPUS_ADMISSION_SCHEMA,
            "interested_roots": [os.fspath(root) for root in self.interested_roots],
            "code_scope": self.code_scope,
            "include_generated": self.include_generated,
            "include_vendored": self.include_vendored,
        }

    @property
    def signature(self) -> str:
        digest = hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()
        return f"{CORPUS_ADMISSION_SCHEMA}:sha256:{digest}"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Read-only classification; no disposition authorizes a physical effect."""

    disposition: AdmissionDisposition
    category: str
    reason: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition not in {"process", "metadata_only", "sensitive"}:
            raise ValueError("unsupported corpus admission disposition")
        if not self.category or not self.reason:
            raise ValueError("corpus admission category and reason are required")
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError("corpus admission evidence must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "category": self.category,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


def _decision(
    disposition: AdmissionDisposition,
    category: str,
    reason: str,
    *evidence: str,
) -> AdmissionDecision:
    return AdmissionDecision(disposition, category, reason, _evidence(*evidence))


def _checkpoint(cancellation_check: Callable[[], None] | None) -> None:
    if cancellation_check is not None:
        cancellation_check()


def _has_reparse_semantics(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400))
    )


def _validate_path_identity(snapshot: FileSnapshot, root: Path) -> str | None:
    """Validate root, ancestors and leaf with no-following metadata only."""

    candidate = _absolute_path(snapshot.path)
    corpus_root = _absolute_path(root)
    if not _is_within(candidate, corpus_root):
        return "path_outside_corpus_root"
    current = Path(os.sep)
    try:
        for component in candidate.parts[1:]:
            current /= component
            metadata = os.lstat(current)
            is_leaf = _path_key(current) == _path_key(candidate)
            if _has_reparse_semantics(metadata):
                return "path_reparse_or_symlink"
            if is_leaf:
                if not stat.S_ISREG(metadata.st_mode):
                    return "identity_not_regular"
                if not stat_matches_snapshot(snapshot, metadata):
                    return "identity_changed_before_read"
            elif not stat.S_ISDIR(metadata.st_mode):
                return "ancestor_not_directory"
    except OSError:
        return "identity_unavailable"
    return None


def _read_prefix(
    snapshot: FileSnapshot,
    *,
    cancellation_check: Callable[[], None] | None,
) -> tuple[bytes | None, str | None]:
    """Read at most one bounded prefix while binding I/O to the snapshot."""

    _checkpoint(cancellation_check)
    descriptor: int | None = None
    try:
        descriptor = _open_regular_descriptor_no_follow(snapshot)
        observed_before = os.fstat(descriptor)
        if not stat.S_ISREG(observed_before.st_mode):
            return None, "identity_not_regular"
        if not stat_matches_snapshot(snapshot, observed_before):
            return None, "identity_changed_before_read"
        wanted = min(MAX_PREFIX_BYTES, max(0, int(snapshot.size)))
        payload = bytearray()
        while len(payload) < wanted:
            _checkpoint(cancellation_check)
            chunk = os.read(descriptor, wanted - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        observed_after = os.fstat(descriptor)
        if not stat_matches_snapshot(snapshot, observed_after):
            return None, "identity_changed_after_read"
        _checkpoint(cancellation_check)
        return bytes(payload), None
    except (FileChangedError, OSError, ValueError, TypeError):
        return None, "prefix_unavailable"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _sensitive_path_reason(path: Path) -> str | None:
    name = path.name.casefold()
    parts = _parts(path)
    suffix = path.suffix.casefold()
    if ".codex" in parts and name == "auth.json":
        return "profile_credential_copy"
    if name == ".env" or name.startswith(".env."):
        return "environment_credential_file"
    if name in _SENSITIVE_NAMES or suffix in _SENSITIVE_SUFFIXES:
        return "private_or_credential_name"
    if name in {"private_key", "private-key", "secret_key", "secret-key"}:
        return "private_key_name"
    return None


def _preserved_name_reason(path: Path) -> str | None:
    name = path.name.casefold()
    if any(
        name == prefix or any(name.startswith(prefix + suffix) for suffix in (".", "-", "_"))
        for prefix in _LICENSE_PREFIXES
    ):
        return "license_or_notice"
    if _component_in(_parts(path), _FIXTURE_DIRECTORY_NAMES):
        return "fixture_tree"
    return None


def _package_context(path: Path) -> bool:
    return _component_in(_parts(path.parent), _PACKAGE_DIRECTORY_NAMES)


def _runtime_context(path: Path) -> bool:
    parts = _parts(path.parent)
    # AppData and .dotnet are context only when paired with a runtime/cache
    # component.  Their names alone never exclude documents or data.
    return _component_in(parts, _RUNTIME_CONTEXT_NAMES) or (
        "appdata" in parts and bool(set(parts).intersection({"local", "programs", "runtime", "cache"}))
    )


def _npm_archive_context(path: Path) -> bool:
    return _component_in(_parts(path.parent), _NPM_ARCHIVE_CONTEXT_NAMES)


def _binary_signature(prefix: bytes) -> str | None:
    for marker, label in _BINARY_SIGNATURES:
        if prefix.startswith(marker):
            return label
    return None


def _document_signature(prefix: bytes) -> str | None:
    for marker, label in _DOCUMENT_SIGNATURES:
        if prefix.startswith(marker):
            return label
    return None


def _archive_signature(prefix: bytes) -> str | None:
    for marker, label in _ARCHIVE_SIGNATURES:
        if prefix.startswith(marker):
            return label
    return None


def _package_archive_kind(path: Path, prefix: bytes) -> str | None:
    archive = _archive_signature(prefix)
    suffix = path.suffix.casefold()
    if suffix == ".whl" and archive == "zip":
        return "wheel"
    if suffix == ".nupkg" and archive == "zip":
        return "nuget"
    if suffix == ".tgz" and archive == "gzip" and _npm_archive_context(path):
        return "npm_tgz"
    return None


def _current_runtime_bytecode(prefix: bytes, path: Path) -> bool:
    """Recognize a current-runtime pyc header without loading or executing it."""

    if path.suffix.casefold() != ".pyc" or len(prefix) < 16:
        return False
    if prefix[:4] != importlib.util.MAGIC_NUMBER:
        return False
    flags = struct.unpack_from("<I", prefix, 4)[0]
    # PEP 552 reserves only the hash-based and checked-hash bits here.
    return flags & ~0x03 == 0


def _looks_sqlite(prefix: bytes) -> bool:
    return prefix.startswith(b"SQLite format 3\x00")


def _looks_shebang(prefix: bytes) -> bool:
    if not prefix.startswith(b"#!"):
        return False
    first_line = prefix.splitlines()[0].lower()
    return any(
        token in first_line
        for token in (b"python", b"bash", b"sh", b"zsh", b"ruby", b"perl", b"node", b"pwsh", b"php")
    )


def _json_package_metadata(path: Path, prefix: bytes) -> str | None:
    if path.suffix.casefold() != ".json" or path.name.casefold() not in _PACKAGE_JSON_NAMES:
        return None
    name = path.name.casefold()
    if name in _JSON_LOCK_NAMES:
        bounded_lock = _bounded_lock_structure(prefix)
        if bounded_lock is not None:
            return bounded_lock
    if not _package_context(path):
        return None
    try:
        decoded = prefix.decode("utf-8", "strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    keys = set(value)
    if name == "package.json" and isinstance(value.get("name"), str) and (
        isinstance(value.get("version"), str)
        or bool(keys.intersection({"dependencies", "devDependencies", "peerDependencies", "scripts", "main", "exports"}))
    ):
        return "json_manifest_structure"
    if name == "composer.json" and isinstance(value.get("require"), dict):
        return "json_manifest_structure"
    if name == "pipfile.lock" and isinstance(value.get("default"), dict):
        return "json_lock_structure"
    if name == "project.assets.json" and (
        isinstance(value.get("targets"), dict) and isinstance(value.get("libraries"), dict)
    ):
        return "json_lock_structure"
    if name == "manifest.json" and isinstance(value.get("name"), str) and (
        isinstance(value.get("version"), str) or isinstance(value.get("files"), list)
    ):
        return "json_manifest_structure"
    return None


def _json_string_end(payload: bytes, start: int) -> int | None:
    if start >= len(payload) or payload[start] != 0x22:
        return None
    index = start + 1
    escaped = False
    while index < len(payload):
        value = payload[index]
        if escaped:
            escaped = False
        elif value == 0x5C:
            escaped = True
        elif value == 0x22:
            return index + 1
        index += 1
    return None


def _top_level_json_keys(prefix: bytes) -> tuple[dict[str, int], bytes]:
    """Return bounded top-level key/value offsets without parsing nested values."""

    data = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not data.startswith(b"{"):
        return {}, data
    depth = 0
    index = 0
    keys: dict[str, int] = {}
    while index < len(data):
        value = data[index]
        if value == 0x22:
            end = _json_string_end(data, index)
            if end is None:
                break
            cursor = end
            while cursor < len(data) and data[cursor] in b" \t\r\n":
                cursor += 1
            if depth == 1 and cursor < len(data) and data[cursor] == 0x3A:
                try:
                    key = json.loads(data[index:end].decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError):
                    key = None
                if isinstance(key, str):
                    cursor += 1
                    while cursor < len(data) and data[cursor] in b" \t\r\n":
                        cursor += 1
                    keys.setdefault(key, cursor)
            index = end
            continue
        if value in (0x7B, 0x5B):
            depth += 1
        elif value in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                break
        index += 1
    return keys, data


def _json_value_is_object(data: bytes, offset: int | None) -> bool:
    return offset is not None and offset < len(data) and data[offset] == 0x7B


def _json_value_is_integer(data: bytes, offset: int | None) -> bool:
    if offset is None or offset >= len(data):
        return False
    return re.match(rb"(?:0|[1-9][0-9]*)(?=\s*[,}])", data[offset:]) is not None


def _bounded_lock_structure(prefix: bytes) -> str | None:
    keys, data = _top_level_json_keys(prefix)
    if not keys:
        return None
    lockfile_version = keys.get("lockfileVersion")
    package_map = keys.get("packages")
    dependency_map = keys.get("dependencies")
    if _json_value_is_integer(data, lockfile_version) and (
        _json_value_is_object(data, package_map)
        or _json_value_is_object(data, dependency_map)
    ):
        return "json_lock_structure_prefix"
    # Older npm lockfiles may omit lockfileVersion. Requiring package identity
    # keys keeps this from becoming a generic JSON word search.
    if (
        _json_value_is_object(data, dependency_map)
        and "name" in keys
        and "version" in keys
    ):
        return "json_lock_structure_prefix"
    return None


def _is_source_or_script(path: Path, prefix: bytes | None) -> bool:
    name = path.name.casefold()
    if path.suffix.casefold() in _SOURCE_SUFFIXES or name in _SCRIPT_FILENAMES:
        return True
    return not path.suffix and prefix is not None and _looks_shebang(prefix)


def _source_scope_decision(path: Path, policy: CorpusAdmissionPolicy) -> str:
    """Reuse Code's bounded project/dependency/cache/generated vocabulary."""

    scope = ProjectCandidateScope(
        roots=tuple(os.fspath(root) for root in policy.interested_roots),
        include_generated=policy.include_generated,
        include_vendored=policy.include_vendored,
    )
    return scope.decision(path)


def _classify_source_scope(path: Path, policy: CorpusAdmissionPolicy) -> AdmissionDecision:
    if policy.code_scope == "broad":
        return _decision("process", "source", "broad_code_scope", "code:scope:broad")
    scope_decision = _source_scope_decision(path, policy)
    if scope_decision == "admit":
        return _decision("process", "source", "interested_source_root", "code:scope:interested")
    reason = (
        "source_outside_interested_root"
        if scope_decision == "outside_project"
        else scope_decision
    )
    return _decision("metadata_only", "source", reason, "code:scope:projects")


def _classify_observed_prefix(
    candidate: Path,
    prefix: bytes,
    *,
    policy: CorpusAdmissionPolicy,
) -> AdmissionDecision:
    """Classify already-observed bytes without any filesystem access."""

    package_context = _package_context(candidate)
    runtime_context = _runtime_context(candidate)
    document = _document_signature(prefix)
    if document is not None:
        return _decision(
            "process",
            "document",
            "content_magic_precedes_path_suffix",
            f"magic:{document}",
        )
    archive = _archive_signature(prefix)
    if archive is not None:
        package_archive = _package_archive_kind(candidate, prefix)
        if package_archive is not None:
            return _decision(
                "metadata_only",
                "retained_archive",
                "retained_archive_no_regeneration_proof",
                f"package:{package_archive}",
            )
        return _decision(
            "process",
            "archive",
            "archive_processable_by_owner",
            f"archive:{archive}",
        )
    if _current_runtime_bytecode(prefix, candidate):
        return _decision(
            "metadata_only",
            "bytecode_cache",
            "current_runtime_bytecode_cache",
            "pyc:current-runtime",
            "pyc:header",
        )
    binary = _binary_signature(prefix)
    if binary is not None:
        if package_context or runtime_context:
            return _decision(
                "metadata_only",
                "runtime_binary",
                "runtime_or_package_binary_metadata_only",
                f"binary:{binary}",
                "binary:contextual",
            )
        return _decision(
            "process",
            "binary",
            "binary_without_runtime_context",
            f"binary:{binary}",
        )
    if _looks_sqlite(prefix):
        return _decision("process", "sqlite", "sqlite_preserved_no_trash", "magic:sqlite")
    json_reason = _json_package_metadata(candidate, prefix)
    if json_reason is not None:
        return _decision(
            "metadata_only",
            "package_metadata",
            "package_json_structure_metadata_only",
            f"json:{json_reason}",
        )
    source_or_script = _is_source_or_script(candidate, prefix)
    if source_or_script:
        return _classify_source_scope(candidate, policy)
    artifact = classify_artifact(candidate, "")
    if artifact.artifact_kind is ArtifactKind.PLAIN_TEXT and _looks_shebang(prefix):
        decision = _classify_source_scope(candidate, policy)
        return AdmissionDecision(
            decision.disposition,
            decision.category,
            decision.reason if decision.reason != "interested_source_root" else "script_content_detected",
            (*decision.evidence, "code:shebang"),
        )
    if artifact.artifact_kind is ArtifactKind.DOCUMENTATION:
        return _decision("process", "document", "document_preserved", "content:document")
    if artifact.artifact_kind is ArtifactKind.DATA:
        return _decision("process", "data", "data_preserved", "content:data")
    return _decision("process", "unknown", "unknown_preserved_no_trash", "content:unclassified")


def _classify_without_prefix(candidate: Path, *, policy: CorpusAdmissionPolicy) -> AdmissionDecision:
    """Classify a path known not to require content inspection."""

    source_or_script = _is_source_or_script(candidate, None)
    if source_or_script:
        return _classify_source_scope(candidate, policy)
    artifact = classify_artifact(candidate, "")
    if artifact.artifact_kind is ArtifactKind.DOCUMENTATION:
        return _decision("process", "document", "document_preserved", "content:document")
    if artifact.artifact_kind is ArtifactKind.DATA:
        return _decision("process", "data", "data_preserved", "content:data")
    return _decision("process", "unknown", "unknown_preserved_no_trash", "content:unclassified")


def _virtual_member_path(member_name: str, container_path: Path) -> Path:
    if not isinstance(member_name, str):
        raise TypeError("virtual member name must be text")
    if not member_name or "\x00" in member_name:
        raise ValueError("virtual member name is empty or contains NUL")
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError("virtual member name must be relative")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if not parts or any(part == ".." or ":" in part for part in parts):
        raise ValueError("virtual member name contains an unsafe component")
    return _absolute_path(container_path).joinpath(*parts)


def assess_virtual_member(
    member_name: str,
    prefix: bytes,
    *,
    container_path: Path,
    policy: CorpusAdmissionPolicy,
) -> AdmissionDecision:
    """Classify an Archive member from bounded verified bytes without filesystem I/O."""

    if not isinstance(prefix, bytes):
        raise TypeError("virtual member prefix must be bytes")
    if len(prefix) > MAX_PREFIX_BYTES:
        raise ValueError("virtual member prefix exceeds the bounded admission limit")
    candidate = _virtual_member_path(member_name, container_path)
    sensitive_reason = _sensitive_path_reason(candidate)
    if sensitive_reason is not None:
        return _decision(
            "sensitive",
            "credential",
            "credential_path_sensitive",
            f"credential:{sensitive_reason}",
        )
    preserved_reason = _preserved_name_reason(candidate)
    if preserved_reason is not None:
        category = "fixture" if preserved_reason == "fixture_tree" else "preserved_artifact"
        return _decision("metadata_only", category, "preserved_artifact_metadata_only", f"preserve:{preserved_reason}")
    return _classify_observed_prefix(candidate, prefix, policy=policy)


def assess_file(
    snapshot: FileSnapshot,
    *,
    root: Path,
    policy: CorpusAdmissionPolicy,
    cancellation_check: Callable[[], None] | None = None,
) -> AdmissionDecision:
    """Classify one inventory snapshot without granting or applying an effect.

    Only a bounded prefix is read for document/archive/binary signatures,
    bytecode, bounded JSON metadata, SQLite detection, or an extensionless
    shebang.  All read failures and identity drift fail closed as ``sensitive``;
    no exception text or payload is included in the returned evidence.
    """

    if not isinstance(snapshot, FileSnapshot):
        raise TypeError("snapshot must be a FileSnapshot")
    if not isinstance(policy, CorpusAdmissionPolicy):
        raise TypeError("policy must be a CorpusAdmissionPolicy")
    _checkpoint(cancellation_check)
    corpus_root = _absolute_path(root)
    candidate = _absolute_path(snapshot.path)
    if not _is_within(candidate, corpus_root):
        return _decision(
            "sensitive",
            "out_of_scope",
            "path_outside_corpus_root",
            "scope:corpus-root",
        )
    identity_failure = _validate_path_identity(snapshot, corpus_root)
    if identity_failure is not None:
        return _decision(
            "sensitive",
            "unverified",
            "identity_or_prefix_unverified",
            f"identity:{identity_failure}",
        )

    sensitive_reason = _sensitive_path_reason(candidate)
    if sensitive_reason is not None:
        return _decision(
            "sensitive",
            "credential",
            "credential_path_sensitive",
            f"credential:{sensitive_reason}",
        )

    preserved_reason = _preserved_name_reason(candidate)
    if preserved_reason is not None:
        category = (
            "fixture"
            if preserved_reason == "fixture_tree"
            else "retained_archive"
            if preserved_reason == "retained_archive"
            else "preserved_artifact"
        )
        return _decision(
            "metadata_only",
            category,
            (
                "retained_archive_no_regeneration_proof"
                if preserved_reason == "retained_archive"
                else "preserved_artifact_metadata_only"
            ),
            f"preserve:{preserved_reason}",
        )

    package_context = _package_context(candidate)
    runtime_context = _runtime_context(candidate)
    suffix = candidate.suffix.casefold()
    needs_prefix = (
        suffix == ".json"
        or not suffix
        or suffix
        in {
            ".bin",
            ".dat",
            ".db",
            ".elf",
            ".exe",
            ".nupkg",
            ".pyc",
            ".so",
            ".sqlite",
            ".sqlite3",
            ".tgz",
            ".whl",
        }
        or suffix in _SOURCE_SUFFIXES
        or candidate.name.casefold() in _SCRIPT_FILENAMES
        or package_context
        or runtime_context
    )
    prefix: bytes | None = None
    if needs_prefix:
        prefix, failure = _read_prefix(snapshot, cancellation_check=cancellation_check)
        if failure is not None:
            return _decision(
                "sensitive",
                "unverified",
                "identity_or_prefix_unverified",
                f"read:{failure}",
            )
    assert prefix is None or len(prefix) <= MAX_PREFIX_BYTES
    if prefix is not None:
        return _classify_observed_prefix(candidate, prefix, policy=policy)
    return _classify_without_prefix(candidate, policy=policy)


__all__ = [
    "CORPUS_ADMISSION_SCHEMA",
    "MAX_PREFIX_BYTES",
    "AdmissionDecision",
    "AdmissionDisposition",
    "CorpusAdmissionPolicy",
    "assess_file",
    "assess_virtual_member",
]
