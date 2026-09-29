"""Deterministic, content-aware policy for disposable corpus artifacts.

The hard redlist is intentionally small and metadata-only.  This owner sits
after Identify and adds only *demonstrable* software/cache evidence.  It is
not a semantic classifier and it never treats a filename alone as proof for a
personal database or a document.  Ambiguous candidates are retained as
``keep``; ``block`` is reserved for a bounded probe/identity verification
failure, never used as a destructive recommendation.

The policy performs bounded, synchronous probes only.  In particular, SQLite
schema inspection is deliberately not dispatched to workers: the corpus file
is opened immutable/read-only for a short, bounded query and no sidecar or
checkpoint is created.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat as stat_module
try:
    from defusedxml import ElementTree as _SecureElementTree
except ImportError:  # pragma: no cover - minimal runtime fallback
    from xml import etree as _xml_etree

    _SecureElementTree = _xml_etree.ElementTree
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from neocortex.platform.content_types import DetectedType, _detect_elf, _detect_pe
from neocortex.platform.logical_filename import LogicalFilename
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadMode,
    SQLiteReadSession,
)
from neocortex.safety.artifact_content_proof import (
    ArtifactContentProof,
    ArtifactContentProofError,
    capture_artifact_content_proof,
)

if TYPE_CHECKING:
    from neocortex.deduplication import FileSnapshot


ARTIFACT_POLICY_SCHEMA = "neocortex.corpus-artifact-policy/v1"
ARTIFACT_POLICY_VERSION = "artifact-policy-v1"
ARTIFACT_PROBE_BYTES = 128 * 1024
ARTIFACT_JSON_LINES = 32
ARTIFACT_SQLITE_TABLE_LIMIT = 64

Disposition = Literal["keep", "trash", "block"]


# These names are deliberately exact.  History/Cookies/Login Data/Bookmarks
# and messages are useful user data even when they happen to be SQLite or JSON.
_PERSONAL_BASENAMES = frozenset(
    {
        "history",
        "cookies",
        "login data",
        "login data for account",
        "bookmarks",
        "messages",
        "messages.json",
        "history.sqlite",
        "history.sqlite3",
        "cookies.sqlite",
        "cookies.sqlite3",
        "login data.sqlite",
        "login data.sqlite3",
        "bookmarks.sqlite",
        "bookmarks.sqlite3",
    }
)
_PACKAGE_METADATA_NAMES = frozenset(
    {
        "metadata",
        "record",
        "pkg-info",
        "sources.txt",
        "requires.txt",
        "dependency_links.txt",
        "entry_points.txt",
        "top_level.txt",
        "installer",
        "direct_url.json",
    }
)
_CHROMIUM_STATE_NAMES = frozenset(
    {
        "network persistent state",
        "transportsecurity",
        "quota_manager",
        "quota_manager-journal",
        "reporting and nel",
        "reporting and nel-journal",
        "origin bound certs",
        "trust tokens",
        "trust tokens-journal",
    }
)
_FIXTURE_COMPONENTS = frozenset(
    {"fixture", "fixtures", "test_data", "testdata", "binaryfixtures"}
)
_LICENSE_COMPONENTS = frozenset({"license", "licenses", "licence", "licences"})
_SQLITE_LIKE_EXTENSIONS = frozenset(
    {".db", ".sqlite", ".sqlite3", ".db3", ".sqlite3-wal", ".sqlite3-shm"}
)
_SQLITE_CACHE_TABLE = re.compile(
    r"(?:cache|entry|entries|blob|metadata|meta|resource|record|records|object|objects|data|quota|index)",
    re.IGNORECASE,
)
_SQLITE_PERSONAL_TABLE = re.compile(
    r"(?:history|cookie|login|bookmark|message)",
    re.IGNORECASE,
)
_ROLLOUT_NAME = re.compile(
    r"^rollout-"
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[tT][0-9]{2}[0-9:.+\-zZ]*)?"
    r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.jsonl$",
    re.IGNORECASE,
)
_ROLLOUT_TYPE_MARKERS = frozenset(
    {
        "session_meta",
        "turn_context",
        "response_item",
        "event_msg",
        "compacted",
        "token_count",
        "model_response",
    }
)
_ROLLOUT_CONTEXT_KEYS = frozenset(
    {
        "session_id",
        "thread_id",
        "turn_id",
        "rollout_id",
        "conversation_id",
        # Current Codex session_meta envelopes carry these fields in payload.
        # They are structural keys only; values are never copied to evidence.
        "id",
        "model_provider",
        "originator",
        "cli_version",
        "cwd",
    }
)
_CHROMIUM_CONTEXT_COMPONENTS = frozenset(
    {
        "chromium",
        "chrome",
        "google-chrome",
        "google-chrome-beta",
        "google-chrome-unstable",
        "chromium-browser",
        "user data",
        "profile",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactDecision:
    """One auditable policy result.

    ``preview`` is a bounded payload rather than a command.  It lets preview
    and apply report the same decision without granting this policy a physical
    effect.  ``decision`` and ``action`` are compatibility aliases used by
    callers that predate the ``disposition`` spelling.
    """

    path: str
    disposition: Disposition
    rule_id: str
    evidence: Mapping[str, object]
    preview: Mapping[str, object]
    mime: str | None = None
    content_proof: ArtifactContentProof | None = None

    @property
    def decision(self) -> Disposition:
        return self.disposition

    @property
    def action(self) -> Disposition:
        return self.disposition

    @property
    def status(self) -> Disposition:
        return self.disposition

    @property
    def previewed(self) -> bool:
        return True

    @property
    def evidence_json(self) -> str:
        return json.dumps(
            self.evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "decision": self.disposition,
            "disposition": self.disposition,
            "rule_id": self.rule_id,
            "evidence": dict(self.evidence),
            "preview": dict(self.preview),
            "mime": self.mime,
            "content_proof": (
                self.content_proof.as_dict() if self.content_proof is not None else None
            ),
        }


def _stable_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _path_parts(path: str | Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in Path(path).parts)


def _is_fixture_or_license_path(path: str | Path) -> str | None:
    parts = _path_parts(path)
    if any(part in _FIXTURE_COMPONENTS for part in parts):
        return "fixture_tree"
    if any(part in _LICENSE_COMPONENTS for part in parts):
        return "license_tree"
    name = LogicalFilename.parse(path).basename.casefold()
    if name.endswith((".whl", ".wheel")):
        return "package_archive"
    if name.endswith((".license", ".licence")) or name in {
        "license",
        "license.txt",
        "licence",
        "licence.txt",
        "copying",
        "notice",
        "notice.txt",
    }:
        return "legal_metadata"
    return None


def _personal_name(path: str | Path) -> str | None:
    name = LogicalFilename.parse(path).basename.casefold()
    return name if name in _PERSONAL_BASENAMES else None


def _generated_artifact_evidence(path: str | Path, logical: LogicalFilename) -> str | None:
    """Require a file-level relation, not merely a generated directory name."""

    parts = _path_parts(path)
    name = logical.basename.casefold()
    if "__pycache__" in parts and logical.logical_extension == ".pyc":
        return "python-bytecode-cache"
    if ".pytest_cache" in parts and name in {
        "cachedir.tag",
        "readme.md",
        "nodeids",
        "lastfailed",
        "stepwise",
    }:
        return "pytest-cache-state"
    if ".mypy_cache" in parts and logical.logical_extension in {".json", ".meta.json"}:
        return "mypy-cache-state"
    if ".ruff_cache" in parts and name in {"content", "cache", "cache.json"}:
        return "ruff-cache-state"
    if ".git" in parts:
        # Git object storage has an unambiguous two-hex directory plus a
        # 38-hex object name.  Keep arbitrary PDFs/docs under a Git tree.
        path_parts = tuple(part.casefold() for part in Path(path).parts)
        for index, component in enumerate(path_parts[:-1]):
            if component == "objects" and index + 2 < len(path_parts):
                shard, object_name = path_parts[index + 1 : index + 3]
                if re.fullmatch(r"[0-9a-f]{2}", shard) and re.fullmatch(
                    r"[0-9a-f]{38}", object_name
                ):
                    return "git-object-storage"
        if name in {"index", "packed-refs", "head", "config"}:
            return "git-control-metadata"
    if ".venv" in parts or ".virtualenv" in parts or "virtualenv" in parts:
        if name == "pyvenv.cfg":
            return "virtualenv-metadata"
    # ``node_modules`` is intentionally not matched here: a useful source
    # tree may contain a vendored package and directory context alone is not a
    # proof that every child is disposable.
    return None


def _read_prefix(
    path: str | Path,
    limit: int,
    *,
    cancellation_check: Callable[[], object] | None = None,
) -> bytes | None:
    """Read one bounded prefix without following non-regular inputs."""

    descriptor: int | None = None
    try:
        if cancellation_check is not None:
            cancellation_check()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        observed = os.fstat(descriptor)
        if not stat_module.S_ISREG(observed.st_mode) or observed.st_size < 0:
            return None
        before = (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        )
        payload = os.pread(descriptor, max(1, min(int(limit), ARTIFACT_PROBE_BYTES)), 0)
        if cancellation_check is not None:
            cancellation_check()
        after = os.fstat(descriptor)
        current = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before != current:
            return None
        return payload
    except (OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _strong_magic(path: str | Path, prefix: bytes) -> tuple[str, str] | None:
    """Reuse Identify's bounded PE/ELF validators for destructive evidence."""

    if prefix.startswith(b"MZ") and _detect_pe(path, prefix) is not None:
        return "pe", "magic:pe"
    if prefix.startswith(b"\x7fELF") and _detect_elf(path, prefix) is not None:
        return "elf", "magic:elf"
    return None


def _detected_binary_kind(detected: DetectedType | None) -> tuple[str, str] | None:
    if detected is None:
        return None
    mime = detected.mime.casefold()
    evidence = detected.evidence.casefold()
    if "elf" in mime or "elf" in evidence or "sharedlib" in mime:
        return "elf", detected.evidence
    if "portable-executable" in mime or "dosexec" in mime or "pe" in evidence:
        return "pe", detected.evidence
    return None


def _sqlite_tables(
    path: str | Path,
    *,
    cancellation_check: Callable[[], object] | None = None,
) -> tuple[tuple[str, ...], str | None]:
    """Read a bounded schema from one immutable SQLite file synchronously."""

    try:
        if cancellation_check is not None:
            cancellation_check()
        prefix = _read_prefix(path, 100, cancellation_check=cancellation_check)
        if prefix is None:
            return (), "sqlite_owner_unavailable"
        if not prefix.startswith(b"SQLite format 3\0"):
            return (), "sqlite_magic_missing"
        session_cancel = None
        if cancellation_check is not None:
            def session_cancel() -> bool | None:
                result = cancellation_check()
                return result if result is None or type(result) is bool else None
        # Corpus SQLite files use the same fenced read kernel as state owners;
        # no bare ``mode=ro`` connection is permitted here.  Strict mode
        # rejects WAL/rollback/sidecar ambiguity and verifies the source fence
        # again during close.  No worker or checkpoint is created.
        with SQLiteReadSession(
            path,
            mode=SQLiteReadMode.IMMUTABLE_STRICT,
            timeout_seconds=1.0,
            max_attempts=1,
            cancellation_check=session_cancel,
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','index') AND name IS NOT NULL "
                "ORDER BY name LIMIT ?",
                (ARTIFACT_SQLITE_TABLE_LIMIT,),
            ).fetchall()
            if cancellation_check is not None:
                cancellation_check()
        names = tuple(str(row[0]) for row in rows if row and row[0] is not None)
        return names, None
    except ImmutableSQLiteUnavailable:
        return (), "sqlite_owner_unavailable"
    except (OSError, sqlite3.Error, ValueError):
        return (), "sqlite_schema_unreadable"


def _jsonl_codex_markers(path: str | Path, prefix: bytes) -> tuple[bool, dict[str, object]]:
    try:
        text = prefix.decode("utf-8-sig", "strict")
    except UnicodeDecodeError:
        return False, {"structure": "not_utf8_jsonl"}
    markers: list[str] = []
    context_keys: list[str] = []
    records = 0
    for raw_line in text.splitlines()[:ARTIFACT_JSON_LINES]:
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(value, dict):
            continue
        records += 1
        raw_type = value.get("type")
        if isinstance(raw_type, str) and raw_type.casefold() in _ROLLOUT_TYPE_MARKERS:
            markers.append(raw_type.casefold())
        context_keys.extend(
            key for key in _ROLLOUT_CONTEXT_KEYS if key in value and key not in context_keys
        )
        # Some rollout envelopes nest the event under payload; inspect only
        # that bounded object, never recursively walk arbitrary content.
        payload = value.get("payload")
        if isinstance(payload, dict):
            nested_type = payload.get("type")
            if isinstance(nested_type, str) and nested_type.casefold() in _ROLLOUT_TYPE_MARKERS:
                markers.append(nested_type.casefold())
            context_keys.extend(
                key for key in _ROLLOUT_CONTEXT_KEYS if key in payload and key not in context_keys
            )
    codex = bool(markers) and bool(context_keys or records >= 2)
    return codex, {
        "records_observed": records,
        "type_markers": tuple(sorted(set(markers)))[:8],
        "context_keys": tuple(sorted(set(context_keys)))[:8],
        "source": "bounded_jsonl_prefix",
    }


def _appx_structure(name: str, prefix: bytes) -> tuple[bool, str]:
    try:
        root = _SecureElementTree.fromstring(prefix)
    except Exception:
        return False, "xml_structure_unreadable"

    def split_tag(tag: object) -> tuple[str, str] | None:
        if not isinstance(tag, str) or not tag.startswith("{") or "}" not in tag:
            return None
        namespace, local = tag[1:].split("}", 1)
        return namespace, local

    root_tag = split_tag(root.tag)
    if root_tag is None:
        return False, "xml_namespace_missing"
    namespace, local = root_tag
    if name == "appxmanifest.xml":
        expected = "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
        if local != "Package":
            return False, "package_root_missing"
        if namespace != expected:
            return False, "appx_manifest_namespace_missing"
        child_locals = {
            item[1]
            for child in root
            if (item := split_tag(child.tag)) is not None
        }
        if not {"Identity", "Applications"}.intersection(child_locals):
            return False, "appx_manifest_structure_missing"
        return True, "appx_manifest_namespace_and_structure"
    if name == "appxblockmap.xml":
        expected = "http://schemas.microsoft.com/appx/2010/blockmap"
        if local != "BlockMap":
            return False, "blockmap_root_missing"
        if namespace != expected:
            return False, "appx_blockmap_namespace_missing"
        descendants = {
            item[1]
            for child in root.iter()
            if (item := split_tag(child.tag)) is not None
        }
        if not {"File", "Block"}.issubset(descendants):
            return False, "appx_blockmap_structure_missing"
        return True, "appx_blockmap_namespace_and_structure"
    return False, "not_appx_metadata_name"


def _package_metadata_structure(name: str, prefix: bytes) -> tuple[bool, str]:
    """Require bounded package metadata syntax in addition to its relation."""

    if prefix.startswith((b"%PDF", b"MZ", b"\x7fELF", b"PK\x03\x04")):
        return False, "package_metadata_binary_magic"
    try:
        text = prefix.decode("utf-8-sig", "strict")
    except UnicodeDecodeError:
        return False, "package_metadata_not_utf8"
    if not text or any(ord(character) < 9 for character in text):
        return False, "package_metadata_not_text"
    lines = tuple(line.strip() for line in text.splitlines()[:128] if line.strip())
    if name in {"metadata", "pkg-info"}:
        headers = {line.split(":", 1)[0].casefold() for line in lines if ":" in line}
        if "metadata-version" in headers and ({"name", "version"} & headers):
            return True, "core_metadata_headers"
        return False, "core_metadata_headers_missing"
    if name == "direct_url.json":
        try:
            value = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            return False, "direct_url_json_invalid"
        if isinstance(value, dict) and any(key in value for key in ("url", "vcs_info", "archive_info")):
            return True, "direct_url_structure"
        return False, "direct_url_structure_missing"
    if name == "entry_points.txt":
        if any(line.startswith("[") and line.endswith("]") for line in lines) and any(
            "=" in line for line in lines
        ):
            return True, "entry_points_structure"
        return False, "entry_points_structure_missing"
    if name == "record":
        records = tuple(line.split(",") for line in lines)
        if any(
            len(fields) >= 3
            and fields[0]
            and not fields[0].startswith("%PDF")
            and (not fields[2] or fields[2].isdigit())
            for fields in records
        ):
            return True, "record_csv_structure"
        return False, "record_csv_structure_missing"
    if name in {"installer", "top_level.txt", "sources.txt", "requires.txt", "dependency_links.txt"}:
        if lines and all(len(line) <= 4096 for line in lines):
            return True, "bounded_package_text"
        return False, "bounded_package_text_missing"
    return False, "package_metadata_rule_missing"


def _chromium_context(path: str | Path) -> bool:
    return bool(_CHROMIUM_CONTEXT_COMPONENTS.intersection(_path_parts(path)))


class ArtifactPolicy:
    """Evaluate deterministic artifact families without semantic authority."""

    def __init__(
        self,
        *,
        max_probe_bytes: int = ARTIFACT_PROBE_BYTES,
        cancellation_check: Callable[[], object] | None = None,
    ) -> None:
        if type(max_probe_bytes) is not int or not 1024 <= max_probe_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_probe_bytes must be between 1024 and 4 MiB")
        self.max_probe_bytes = max_probe_bytes
        self.cancellation_check = cancellation_check
        self._proof_required = False

    def _checkpoint(self) -> None:
        if self.cancellation_check is not None:
            self.cancellation_check()

    @staticmethod
    def _binary_probe_extensions() -> frozenset[str]:
        return frozenset(
            {"", ".bin", ".dat", ".dll", ".elf", ".exe", ".out", ".run", ".so", ".sys"}
        )

    def may_probe(
        self,
        path_or_snapshot: str | Path | FileSnapshot,
        detected: DetectedType | None = None,
        *,
        context: Mapping[str, object] | None = None,
    ) -> bool:
        """Metadata-only admission to the bounded artifact probe."""

        context = {} if context is None else context
        path = str(path_or_snapshot.path if hasattr(path_or_snapshot, "path") else path_or_snapshot)
        logical = LogicalFilename.parse(path)
        name = logical.basename.casefold()
        if _is_fixture_or_license_path(path) is not None or _personal_name(path) is not None:
            return False
        if logical.logical_extension in {".whl", ".wheel"}:
            return False
        package_parent = Path(path).parent.name.casefold()
        package_relation = (
            package_parent.endswith((".dist-info", ".egg-info"))
            and name in _PACKAGE_METADATA_NAMES
        )
        if bool(context.get("preidentify")):
            return package_relation
        if package_relation or _generated_artifact_evidence(path, logical) is not None:
            return True
        if (
            _ROLLOUT_NAME.fullmatch(name)
            or name in {"appxmanifest.xml", "appxblockmap.xml"}
            or name in _CHROMIUM_STATE_NAMES
            or re.fullmatch(r"cache\.[0-9]{1,6}\.db", name) is not None
        ):
            return True
        if logical.logical_extension in {".zip", ".apk", ".jar", ".xapk"}:
            return True
        if detected is not None and _detected_binary_kind(detected) is not None:
            return True
        return logical.logical_extension in self._binary_probe_extensions()

    def _capture_active_content_proof(
        self,
        source: str | Path | FileSnapshot,
    ) -> None:
        self._active_content_proof = None
        try:
            self._checkpoint()
            self._active_content_proof = capture_artifact_content_proof(
                source,
                family="artifact.policy",
            )
            self._checkpoint()
        except (ArtifactContentProofError, OSError):
            # A keep/block decision may remain useful without a proof, but a
            # later Trash rule is fail-closed in ``_decision``.
            self._active_content_proof = None

    @property
    def policy_digest(self) -> str:
        return _stable_digest(self.policy_payload())

    def digest(self) -> str:
        """Method-form compatibility alias for callers that avoid properties."""

        return self.policy_digest

    @staticmethod
    def policy_payload() -> dict[str, object]:
        return {
            "schema": ARTIFACT_POLICY_SCHEMA,
            "version": ARTIFACT_POLICY_VERSION,
            "families": [
                "generated-file-relation",
                "codex-rollout",
                "sqlite-cache",
                "appx-metadata",
                "chromium-generated-state",
                "package-metadata",
                "strong-runtime-or-compiled-magic",
                "archive-runtime-structure",
                "archive-software-package",
            ],
            "protected": [
                "personal-database-names",
                "wheel-and-license-material",
                "fixture-trees",
            ],
        }

    def _decision(
        self,
        path: str,
        disposition: Disposition,
        rule_id: str,
        evidence: Mapping[str, object],
        *,
        mime: str | None = None,
        content_proof: ArtifactContentProof | None = None,
    ) -> ArtifactDecision:
        if content_proof is None:
            content_proof = getattr(self, "_active_content_proof", None)
        if disposition == "trash" and self._proof_required and content_proof is None:
            evidence = {
                **dict(evidence),
                "intended_rule_id": rule_id,
                "content_proof": "unavailable",
            }
            disposition = "block"
            rule_id = "artifact.content-proof-unavailable"
        bounded_evidence = {
            "schema": ARTIFACT_POLICY_SCHEMA,
            "policy_version": ARTIFACT_POLICY_VERSION,
            "rule_id": rule_id,
            **dict(evidence),
        }
        preview = {
            "schema": ARTIFACT_POLICY_SCHEMA,
            "action": disposition,
            "rule_id": rule_id,
            "path": path,
            "mime": mime,
        }
        return ArtifactDecision(
            path,
            disposition,
            rule_id,
            bounded_evidence,
            preview,
            mime,
            content_proof,
        )

    def evaluate_archive_members(
        self,
        path: str | Path,
        members: Iterable[str],
        *,
        detected: DetectedType | None = None,
        member_signatures: Mapping[str, str] | None = None,
        container_kind: str | None = None,
        max_members: int = 4096,
    ) -> ArtifactDecision:
        """Adapt the pure archive rule result into this policy DTO."""

        from neocortex.capabilities.formats.archive.artifact_rules import (
            classify_archive_members,
        )

        source = str(path)
        self._proof_required = False
        self._active_content_proof = None
        result = classify_archive_members(
            source,
            members,
            member_signatures=member_signatures,
            container_kind=container_kind,
            max_members=max_members,
        )
        mime = detected.mime if detected is not None else None
        return self._decision(
            source,
            result.disposition,
            result.rule_id,
            result.evidence,
            mime=mime,
        )

    # Short alias for archive intake callers that prefer the action verb.
    evaluate_archive = evaluate_archive_members

    def evaluate(
        self,
        path_or_snapshot: str | Path | FileSnapshot,
        detected: DetectedType | None = None,
        *,
        detected_type: DetectedType | None = None,
        metadata: Mapping[str, object] | None = None,
        context: Mapping[str, object] | None = None,
    ) -> ArtifactDecision:
        """Evaluate one path using typed detection and bounded family probes.

        ``metadata`` is inventory metadata (size, identity, mtime, etc.) and
        ``context`` may contain already-observed bounded facts such as
        ``sqlite_tables``.  Neither is treated as semantic classification.
        """

        if detected is not None and detected_type is not None and detected != detected_type:
            raise ValueError("detected and detected_type disagree")
        if detected is None:
            detected = detected_type
        snapshot = path_or_snapshot
        self._proof_required = True
        self._active_content_proof = None
        self._checkpoint()
        path = str(snapshot.path if hasattr(snapshot, "path") else snapshot)
        metadata = {} if metadata is None else dict(metadata)
        context = {} if context is None else dict(context)
        logical = LogicalFilename.parse(path)
        name = logical.basename.casefold()
        mime = detected.mime if detected is not None else None
        common = {
            "logical_basename": logical.normalized_basename,
            "logical_extension": logical.logical_extension,
            "gnu_suffixes": logical.gnu_suffixes,
            "detected_mime": mime,
            "detected_evidence": detected.evidence if detected is not None else None,
            "metadata": {
                key: metadata[key]
                for key in ("volume_id", "file_id", "size", "mtime_ns", "birthtime_ns")
                if key in metadata
            },
        }

        protected = _is_fixture_or_license_path(path)
        if protected is not None:
            return self._decision(
                path,
                "keep",
                "protect." + protected,
                {**common, "protection": protected},
                mime=mime,
            )
        personal = _personal_name(path)
        if personal is not None:
            return self._decision(
                path,
                "keep",
                "protect.personal-data-name",
                {**common, "protected_basename": personal},
                mime=mime,
            )

        # A wheel/license/fixture must win over every generated-tree hint.
        if logical.logical_extension in {".whl", ".wheel"}:
            return self._decision(
                path,
                "keep",
                "protect.package-archive",
                {**common, "extension": logical.logical_extension},
                mime=mime,
            )

        # A generated directory name alone is not proof: a useful PDF placed
        # below ``.pytest_cache`` remains a survivor.  Require a file-level
        # relation such as a .pyc in __pycache__ or a Git object shape.
        generated_evidence = _generated_artifact_evidence(path, logical)

        # A caller may run this same owner before Identify.  That pre-clean is
        # intentionally stricter than the post-Identify pass: it accepts only
        # exact package-directory relation and never uses a suffix or a weak
        # content hint as a destructive signal.  Root/orchestrator callers can
        # invoke the post-Identify pass later for the bounded
        # strong-magic and structure rules below.
        if bool(context.get("preidentify")):
            package_parent_name = Path(path).parent.name.casefold()
            package_parent = (
                package_parent_name
                if package_parent_name.endswith((".dist-info", ".egg-info"))
                else None
            )
            if package_parent is not None and name in _PACKAGE_METADATA_NAMES:
                self._capture_active_content_proof(
                    snapshot if hasattr(snapshot, "path") else path
                )
                prefix = _read_prefix(
                    path,
                    self.max_probe_bytes,
                    cancellation_check=self.cancellation_check,
                )
                if prefix is not None:
                    valid, structure = _package_metadata_structure(name, prefix)
                    if valid:
                        return self._decision(
                            path,
                            "trash",
                            "artifact.package-metadata.relation-v1",
                            {
                                **common,
                                "package_directory": package_parent,
                                "relation": "distribution-metadata",
                                "structure": structure,
                                "phase": "preidentify",
                            },
                            mime=mime,
                        )
            return self._decision(
                path,
                "keep",
                "keep.preidentify-no-context-proof",
                {**common, "phase": "preidentify"},
                mime=mime,
            )

        if generated_evidence is not None:
            self._capture_active_content_proof(
                snapshot if hasattr(snapshot, "path") else path
            )
            return self._decision(
                path,
                "trash",
                "artifact.generated-file.v1",
                {**common, "relation": generated_evidence},
                mime=mime,
            )

        if not self.may_probe(path, detected, context=context):
            return self._decision(
                path,
                "keep",
                "keep.no-artifact-probe-candidate",
                common,
                mime=mime,
            )

        # Capture the bounded proof before any family-specific prefix/schema
        # probe.  The physical effect revalidates these same segments and ctime.
        self._capture_active_content_proof(
            snapshot if hasattr(snapshot, "path") else path
        )

        is_archive_candidate = (
            detected is not None
            and detected.mime.casefold() in {"application/zip", "application/x-zip-compressed"}
        ) or logical.logical_extension in {".zip", ".apk", ".jar", ".xapk"}
        if is_archive_candidate:
            try:
                from neocortex.capabilities.formats.archive.intake import (
                    observe_archive_artifact_structure,
                )

                observation = observe_archive_artifact_structure(
                    path,
                    max_member_probe_bytes=min(64 * 1024, self.max_probe_bytes),
                    cancellation=self.cancellation_check,
                )
                archive_policy = ArtifactPolicy(cancellation_check=self.cancellation_check)
                archive_decision = archive_policy.evaluate_archive_members(
                    path,
                    observation.members,
                    member_signatures=observation.member_signatures,
                    container_kind=observation.container_kind,
                )
            except Exception as exc:
                return self._decision(
                    path,
                    "block",
                    "artifact.archive-observation-failed",
                    {**common, "error_type": type(exc).__name__},
                    mime=mime,
                )
            if archive_decision.rule_id in {
                "artifact.archive.runtime-structure.v1",
                "artifact.archive.software-package.v1",
            }:
                return self._decision(
                    path,
                    "trash",
                    archive_decision.rule_id,
                    {
                        **common,
                        "archive_evidence": dict(archive_decision.evidence),
                        "physical_effect": "artifact_policy_proof_stage",
                    },
                    mime=mime,
                )

        # Codex rollouts need both the UUID/timestamp basename and the bounded
        # event structure.  A documentary JSONL containing the word rollout is
        # therefore retained.
        if _ROLLOUT_NAME.fullmatch(name):
            prefix = _read_prefix(
                path,
                self.max_probe_bytes,
                cancellation_check=self.cancellation_check,
            )
            if prefix is None:
                return self._decision(
                    path,
                    "block",
                    "artifact.codex-rollout.unreadable",
                    {**common, "verification": "bounded_prefix_unavailable"},
                    mime=mime,
                )
            codex, structure = _jsonl_codex_markers(path, prefix)
            if codex:
                return self._decision(
                    path,
                    "trash",
                    "artifact.codex-rollout.v1",
                    {**common, **structure, "filename": "timestamp_uuid_jsonl"},
                    mime=mime,
                )
            return self._decision(
                path,
                "keep",
                "keep.codex-rollout.structure-unconfirmed",
                {**common, **structure, "filename": "timestamp_uuid_jsonl"},
                mime=mime,
            )

        # AppX files are disposable package metadata only when their XML
        # namespace and required structure are present.
        if name in {"appxmanifest.xml", "appxblockmap.xml"}:
            prefix = _read_prefix(
                path,
                self.max_probe_bytes,
                cancellation_check=self.cancellation_check,
            )
            if prefix is None:
                return self._decision(
                    path,
                    "block",
                    "artifact.appx.unreadable",
                    {**common, "verification": "bounded_prefix_unavailable"},
                    mime=mime,
                )
            valid, reason = _appx_structure(name, prefix)
            if valid:
                return self._decision(
                    path,
                    "trash",
                    "artifact.appx-metadata.v1",
                    {**common, "structure": reason},
                    mime=mime,
                )
            return self._decision(
                path,
                "keep",
                "keep.appx.structure-unconfirmed",
                {**common, "structure": reason},
                mime=mime,
            )

        # Chromium state is name-specific *and* profile-specific.  Generic
        # names such as History and Cookies were stopped above and are never
        # members of this family.
        if name in _CHROMIUM_STATE_NAMES:
            if not _chromium_context(path):
                return self._decision(
                    path,
                    "keep",
                    "keep.chromium-state.context-missing",
                    {**common, "verification": "chromium_profile_context_required"},
                    mime=mime,
                )
            if name in {"quota_manager", "quota_manager-journal"}:
                tables = context.get("sqlite_tables")
                if isinstance(tables, (tuple, list)):
                    header = _read_prefix(
                        path,
                        100,
                        cancellation_check=self.cancellation_check,
                    )
                    if header is None or not header.startswith(b"SQLite format 3\0"):
                        return self._decision(
                            path,
                            "keep",
                            "keep.chromium-state.sqlite-magic-unconfirmed",
                            {**common, "sqlite_tables": tuple(str(item) for item in tables)[:16]},
                            mime=mime,
                        )
                else:
                    tables, sqlite_error = _sqlite_tables(
                        path,
                        cancellation_check=self.cancellation_check,
                    )
                    if sqlite_error == "sqlite_owner_unavailable":
                        return self._decision(
                            path,
                            "keep",
                            "keep.chromium-state.sqlite-owner-unavailable",
                            {**common, "sqlite_error": sqlite_error},
                            mime=mime,
                        )
                    if sqlite_error is not None:
                        return self._decision(
                            path,
                            "block",
                            "artifact.chromium-state.sqlite-unconfirmed",
                            {**common, "sqlite_error": sqlite_error},
                            mime=mime,
                        )
                normalized_tables = tuple(str(item) for item in tables)
                if not any(
                    re.search(r"(?:quota|origin|meta|database|bucket)", table, re.IGNORECASE)
                    for table in normalized_tables
                ):
                    return self._decision(
                        path,
                        "keep",
                        "keep.chromium-state.schema-unconfirmed",
                        {**common, "sqlite_tables": normalized_tables[:16]},
                        mime=mime,
                    )
                return self._decision(
                    path,
                    "trash",
                    "artifact.chromium-generated-state.v1",
                    {**common, "context": "chromium_profile", "sqlite_tables": normalized_tables[:16]},
                    mime=mime,
                )
            prefix = _read_prefix(
                path,
                self.max_probe_bytes,
                cancellation_check=self.cancellation_check,
            )
            if prefix is None:
                return self._decision(
                    path,
                    "block",
                    "artifact.chromium-state.unreadable",
                    {**common, "verification": "bounded_prefix_unavailable"},
                    mime=mime,
                )
            try:
                text = prefix.decode("utf-8-sig", "strict").casefold()
            except UnicodeDecodeError:
                text = ""
            markers = tuple(
                marker
                for marker in (
                    "chromium",
                    "chrome",
                    "transport_security",
                    "transportsecurity",
                    "version",
                    "servers",
                    "origins",
                )
                if marker in text
            )
            if not markers:
                return self._decision(
                    path,
                    "keep",
                    "keep.chromium-state.structure-unconfirmed",
                    {**common, "context": "chromium_profile", "markers": ()},
                    mime=mime,
                )
            return self._decision(
                path,
                "trash",
                "artifact.chromium-generated-state.v1",
                {**common, "context": "chromium_profile", "markers": markers[:8]},
                mime=mime,
            )

        # Package metadata is disposable only in a recognized distribution
        # directory.  This prevents a useful standalone ``METADATA`` document
        # from being removed by name.
        package_parent_name = Path(path).parent.name.casefold()
        package_parent = (
            package_parent_name
            if package_parent_name.endswith((".dist-info", ".egg-info"))
            else None
        )
        if package_parent is not None and name in _PACKAGE_METADATA_NAMES:
            prefix = _read_prefix(
                path,
                self.max_probe_bytes,
                cancellation_check=self.cancellation_check,
            )
            if prefix is None:
                return self._decision(
                    path,
                    "block",
                    "artifact.package-metadata.unreadable",
                    {**common, "package_directory": package_parent},
                    mime=mime,
                )
            valid, structure = _package_metadata_structure(name, prefix)
            if valid:
                return self._decision(
                    path,
                    "trash",
                    "artifact.package-metadata.relation-v1",
                    {
                        **common,
                        "package_directory": package_parent,
                        "relation": "distribution-metadata",
                        "structure": structure,
                    },
                    mime=mime,
                )
            return self._decision(
                path,
                "keep",
                "keep.package-metadata.structure-unconfirmed",
                {
                    **common,
                    "package_directory": package_parent,
                    "structure": structure,
                },
                mime=mime,
            )

        # Cache.N.db requires SQLite magic *and* a cache-like schema.  A
        # filename whose bytes/schema do not prove that family remains useful
        # data; only a real read/verification failure is a block.
        if re.fullmatch(r"cache\.[0-9]{1,6}\.db", name):
            tables = context.get("sqlite_tables")
            sqlite_error: str | None = None
            if isinstance(tables, (tuple, list)):
                header = _read_prefix(
                    path,
                    100,
                    cancellation_check=self.cancellation_check,
                )
                if header is None:
                    sqlite_error = "sqlite_owner_unavailable"
                elif not header.startswith(b"SQLite format 3\0"):
                    sqlite_error = "sqlite_magic_missing"
            else:
                tables, sqlite_error = _sqlite_tables(
                    path,
                    cancellation_check=self.cancellation_check,
                )
            if sqlite_error in {"sqlite_magic_missing", "sqlite_owner_unavailable"}:
                return self._decision(
                    path,
                    "keep",
                    "keep.sqlite-cache.structure-unavailable",
                    {**common, "sqlite_error": sqlite_error},
                    mime=mime,
                )
            if sqlite_error is not None:
                return self._decision(
                    path,
                    "block",
                    "artifact.sqlite-cache.unconfirmed",
                    {**common, "sqlite_error": sqlite_error},
                    mime=mime,
                )
            normalized_tables = tuple(str(item) for item in tables)
            personal_tables = tuple(
                table for table in normalized_tables if _SQLITE_PERSONAL_TABLE.search(table)
            )
            if personal_tables:
                return self._decision(
                    path,
                    "keep",
                    "protect.sqlite-personal-schema",
                    {**common, "sqlite_tables": personal_tables[:16]},
                    mime=mime,
                )
            cache_tables = tuple(
                table for table in normalized_tables if _SQLITE_CACHE_TABLE.search(table)
            )
            if not cache_tables:
                return self._decision(
                    path,
                    "keep",
                    "keep.sqlite-cache.schema-unconfirmed",
                    {**common, "sqlite_tables": normalized_tables[:16]},
                    mime=mime,
                )
            return self._decision(
                path,
                "trash",
                "artifact.sqlite-cache.v1",
                {**common, "sqlite_tables": cache_tables[:16], "context": "cache.N.db"},
                mime=mime,
            )

        # Identify's typed hint is advisory; the destructive decision requires
        # a fresh bounded proof through the same PE/ELF validators.  This keeps
        # a stale detector cache from turning a replaced source into Trash.
        typed_binary_hint = _detected_binary_kind(detected)
        binary_probe_extensions = {
            "",
            ".bin",
            ".dat",
            ".dll",
            ".elf",
            ".exe",
            ".out",
            ".run",
            ".so",
            ".sys",
        }
        prefix = (
            _read_prefix(
                path,
                min(self.max_probe_bytes, 64 * 1024),
                cancellation_check=self.cancellation_check,
            )
            if typed_binary_hint is not None
            or logical.logical_extension in binary_probe_extensions
            else None
        )
        binary = None if prefix is None else _strong_magic(path, prefix)
        if binary is not None:
            kind, binary_evidence = binary
            return self._decision(
                path,
                "trash",
                f"artifact.strong-magic.{kind}",
                {
                    **common,
                    "binary_kind": kind,
                    "binary_evidence": binary_evidence,
                    "typed_hint": typed_binary_hint[0] if typed_binary_hint else None,
                },
                mime=mime,
            )

        if logical.logical_extension in _SQLITE_LIKE_EXTENSIONS or (
            detected is not None and detected.mime.casefold() == "application/vnd.sqlite3"
        ):
            # SQLite-looking sources not covered by a verified cache family
            # remain useful/unknown data.  This is an explicit keep rule, not
            # a fallback to name-based deletion.
            return self._decision(
                path,
                "keep",
                "protect.sqlite-unknown",
                {**common, "reason": "unknown_sqlite_owner"},
                mime=mime,
            )

        return self._decision(
            path,
            "keep",
            "keep.no-demonstrable-artifact",
            common,
            mime=mime,
        )

    classify = evaluate


def artifact_policy_payload() -> dict[str, object]:
    return ArtifactPolicy.policy_payload()


def artifact_policy_digest() -> str:
    return _stable_digest(ArtifactPolicy.policy_payload())


__all__ = [
    "ARTIFACT_POLICY_SCHEMA",
    "ARTIFACT_POLICY_VERSION",
    "ArtifactDecision",
    "ArtifactPolicy",
    "Disposition",
    "artifact_policy_digest",
    "artifact_policy_payload",
]
