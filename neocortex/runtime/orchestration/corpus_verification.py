"""Bounded, read-only terminal verification of a materialised Corpus.

The Framework supplies projections from the existing owners.  This module owns
only the final filesystem checks and does not open SQLite, query an owner, or
apply an effect.  In particular, the two lifecycle entry points deliberately
receive owner-owned DTOs and lookup callbacks instead of manufacturing a second
catalogue or provenance store.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


SCHEMA = "neocortex.corpus-final-verification/v1"
STRUCTURAL_ROOTS = (
    "Corpus_ordenado",
    "Sin_clasificar",
    "Sin_clasificar/_MIME",
)
_TOP_LEVEL_ROOTS = frozenset({"Corpus_ordenado", "Sin_clasificar"})
_POST_SEMANTIC_OWNERS = ("source_cache", "catalog", "framework", "dedup")
_FINAL_OWNERS = (*_POST_SEMANTIC_OWNERS, "semantic")
_ALLOWED_RETAINED = frozenset({"blocked", "stale", "protected", "recovery_required"})
_RESOLVED = frozenset({"applied", "complete", "removed", "resolved", "trashed", "verified"})
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+\-]*\/[a-z0-9][a-z0-9!#$&^_.+\-]*$")

VerificationPhase = Literal["before_semantic", "after_semantic"]
VerificationStatus = Literal["complete", "partial", "blocked"]


@dataclass(frozen=True, slots=True)
class OrganizationMoveReceipt:
    """Validated projection of an existing organization move receipt.

    ``from_json`` accepts the canonical receipt emitted by the organization
    owner.  It does not create a receipt or infer an identity: source digest,
    target identity, source/target paths, schema and source absence must all be
    present in the owner receipt.
    """

    schema: str
    owner: str
    status: str
    source_path: str | Path
    target_path: str | Path
    source_identity: object
    target_identity: Mapping[str, object]
    source_absent: bool

    @classmethod
    def from_json(
        cls,
        raw: str | Mapping[str, object],
        *,
        owner: str,
        status: str,
    ) -> OrganizationMoveReceipt:
        if isinstance(raw, Mapping):
            payload = raw
        elif isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("organization receipt is not valid JSON") from exc
            if not isinstance(decoded, Mapping):
                raise ValueError("organization receipt is not an object")
            payload = decoded
        else:
            raise TypeError("organization receipt must be JSON text or a mapping")
        schema = payload.get("organization_receipt_schema")
        source_path = payload.get("source_path")
        target_path = payload.get("target_path")
        source_identity = payload.get("source_identity", payload.get("source_digest"))
        target_identity = payload.get("target_identity")
        source_absent = payload.get("source_absent")
        if (
            schema != "neocortex.organization-move-receipt/v1"
            or not isinstance(owner, str)
            or not owner.strip()
            or status != "applied"
            or not isinstance(source_path, str)
            or not source_path.strip()
            or not isinstance(target_path, str)
            or not target_path.strip()
            or not _nonempty(source_identity)
            or not isinstance(target_identity, Mapping)
            or not target_identity
            or not {"path", "size", "mtime_ns", "birthtime_ns", "volume_id", "file_id"}.issubset(target_identity)
            or source_absent is not True
        ):
            raise ValueError("organization receipt lacks schema, owner, applied status, identity, or source absence")
        return cls(
            schema=str(schema),
            owner=owner,
            status=status,
            source_path=source_path,
            target_path=target_path,
            source_identity=source_identity,
            target_identity=dict(target_identity),
            source_absent=True,
        )


@dataclass(frozen=True, slots=True)
class ClassifiedSurvivor:
    """Owner projection required for one file under ``Corpus_ordenado``."""

    path: str | Path
    classification: str | Mapping[str, object]
    confidence: float
    evidence: object
    organization_root: str | Path
    receipt: OrganizationMoveReceipt
    status: str = "applied"
    taxonomy_status: str = "verified"


@dataclass(frozen=True, slots=True)
class ResidualSurvivor:
    """Owner projection required for one file under the residual MIME tree."""

    path: str | Path
    mime: str
    evidence: object
    status: str = "unclassified"
    receipt: object | None = None


@dataclass(frozen=True, slots=True)
class PendingFinding:
    """Explicit outcome for junk, duplicate, policy, or producer findings."""

    kind: Literal["junk", "duplicate", "producer", "policy"]
    status: str
    path: str | Path | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class OwnerCurrentPath:
    """Current path observation supplied by a closed/coordinated owner."""

    owner: Literal["source_cache", "catalog", "framework", "dedup", "semantic"]
    path: str | Path | None
    status: str = "current"
    observed_exists: bool = True
    physical_identity: Mapping[str, object] | None = None


ClassificationLookup = Callable[[Path], ClassifiedSurvivor | None]
ResidualLookup = Callable[[Path], ResidualSurvivor | None]


@dataclass(frozen=True, slots=True)
class CorpusVerificationInputs:
    """Bounded hand-off from Framework and the closed owner projections.

    Lookups are called once per physical survivor, so the verifier never builds
    a path-indexed copy of Catalog, Framework, Dedup, or Semantic.  Streams are
    consumed once and bounded by ``max_owner_records``.
    """

    classified_lookup: ClassificationLookup | None
    residual_lookup: ResidualLookup | None
    current_paths: Iterable[OwnerCurrentPath]
    actionable_junk: Iterable[PendingFinding] = ()
    actionable_duplicates: Iterable[PendingFinding] = ()
    producers_pending: Iterable[PendingFinding] = ()
    policy_findings: Iterable[PendingFinding] = ()
    report_metrics: Mapping[str, object] = field(default_factory=dict)
    checkpoint: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class CorpusVerificationIssue:
    code: str
    detail: str
    path: str | None = None
    owner: str | None = None
    severity: Literal["error", "warning"] = "error"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "detail": self.detail,
            "path": self.path,
            "owner": self.owner,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class CorpusVerificationResult:
    phase: VerificationPhase
    root: str
    status: VerificationStatus
    coverage: Literal["complete", "partial"]
    passed: bool
    issues: tuple[CorpusVerificationIssue, ...]
    metrics: Mapping[str, int | None]
    accounted_paths: tuple[str, ...] = ()
    unaccounted_paths: tuple[str, ...] = ()
    owner_paths_checked: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.status == "complete" and self.coverage == "complete"

    @property
    def failures(self) -> tuple[CorpusVerificationIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "error")

    @property
    def warnings(self) -> tuple[CorpusVerificationIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "warning")

    @property
    def ok(self) -> bool:
        return self.passed

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "phase": self.phase,
            "root": self.root,
            "status": self.status,
            "coverage": self.coverage,
            "passed": self.passed,
            "issues": [item.to_dict() for item in self.issues],
            "metrics": dict(self.metrics),
            # These are bounded samples, not a serialized corpus inventory.
            "accounted_paths": list(self.accounted_paths),
            "unaccounted_paths": list(self.unaccounted_paths),
            "owner_paths_checked": list(self.owner_paths_checked),
        }

    as_dict = to_dict


class _Issues:
    def __init__(self, *, max_items: int) -> None:
        self.items: list[CorpusVerificationIssue] = []
        self.max_items = max_items
        self.coverage_partial = False
        self._overflowed = False

    def add(
        self,
        code: str,
        detail: str,
        *,
        path: Path | str | None = None,
        owner: str | None = None,
        severity: Literal["error", "warning"] = "error",
    ) -> None:
        if len(self.items) >= self.max_items:
            if not self._overflowed:
                self.items.append(
                    CorpusVerificationIssue(
                        "issue_bound_exceeded",
                        "verification issue sample reached its bound",
                        severity="error",
                    )
                )
                self._overflowed = True
            self.coverage_partial = True
            return
        self.items.append(
            CorpusVerificationIssue(
                str(code)[:128],
                str(detail)[:1_024],
                None if path is None else str(path),
                None if owner is None else str(owner)[:128],
                severity,
            )
        )

    def partial(self, code: str, detail: str, *, path: Path | str | None = None) -> None:
        self.coverage_partial = True
        self.add(code, detail, path=path)


@dataclass(slots=True)
class _Scan:
    files: int = 0
    bytes: int = 0
    ordered: int = 0
    residual: int = 0
    accounted: int = 0
    unaccounted: int = 0
    mime_buckets: set[tuple[str, str]] = field(default_factory=set)
    empty_directories: int = 0
    accounted_sample: list[str] = field(default_factory=list)
    unaccounted_sample: list[str] = field(default_factory=list)


class CorpusVerifier:
    """Reusable verifier with only filesystem/report bounds as configuration."""

    def __init__(self, *, max_files: int = 100_000, max_owner_records: int = 100_000, max_issues: int = 256) -> None:
        for value, label in (
            (max_files, "max_files"),
            (max_owner_records, "max_owner_records"),
            (max_issues, "max_issues"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        self.max_files = max_files
        self.max_owner_records = max_owner_records
        self.max_issues = max_issues

    def verify(
        self,
        root: str | Path,
        inputs: CorpusVerificationInputs,
        *,
        phase: VerificationPhase,
        min_confidence: float,
    ) -> CorpusVerificationResult:
        return verify_corpus(
            root,
            inputs,
            phase=phase,
            min_confidence=min_confidence,
            max_files=self.max_files,
            max_owner_records=self.max_owner_records,
            max_issues=self.max_issues,
        )

    def verify_before_semantic(
        self,
        root: str | Path,
        inputs: CorpusVerificationInputs,
        *,
        min_confidence: float,
    ) -> CorpusVerificationResult:
        return self.verify(root, inputs, phase="before_semantic", min_confidence=min_confidence)

    def verify_after_semantic(
        self,
        root: str | Path,
        inputs: CorpusVerificationInputs,
        *,
        min_confidence: float,
    ) -> CorpusVerificationResult:
        return self.verify(root, inputs, phase="after_semantic", min_confidence=min_confidence)


def _nonempty(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    try:
        return len(value) > 0  # type: ignore[arg-type]
    except TypeError:
        return True


def _evidence_object(value: object) -> bool:
    """Accept owner evidence, not an unbound hash/string supplied by a caller."""

    if isinstance(value, Mapping) and bool(value):
        return True
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return isinstance(decoded, Mapping) and bool(decoded)
    return False


def _root_path(root: str | Path, issues: _Issues) -> Path:
    try:
        path = Path(root)
    except (TypeError, ValueError) as exc:
        issues.add("invalid_root", f"invalid corpus root: {type(exc).__name__}")
        return Path("/")
    if not path.is_absolute():
        issues.add("root_not_absolute", "corpus root must be absolute", path=path)
        path = Path(os.path.abspath(os.fspath(path)))
    return path


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path(root: Path, raw: str | Path, *, label: str, issues: _Issues) -> Path | None:
    try:
        candidate = Path(raw)
    except (TypeError, ValueError):
        issues.add("invalid_path", f"{label} is invalid")
        return None
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    if not _inside(root, candidate):
        issues.add("path_outside_root", f"{label} is outside the corpus root", path=candidate)
        return None
    return candidate


def _validate_structural_roots(root: Path, issues: _Issues) -> None:
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        issues.add("root_unavailable", f"cannot stat corpus root: {type(exc).__name__}", path=root)
        return
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        issues.add("root_not_directory", "corpus root is not a real directory", path=root)
        return
    for relative in STRUCTURAL_ROOTS:
        candidate = root / relative
        try:
            metadata = os.lstat(candidate)
        except OSError:
            issues.add("structural_root_missing", "mandatory structural root is missing", path=candidate)
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            issues.add("structural_root_invalid", "mandatory structural root is not a real directory", path=candidate)


def _mime(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    return candidate if _MIME_RE.fullmatch(candidate) else None


def _sample_append(sample: list[str], path: Path) -> None:
    if len(sample) < 32:
        sample.append(str(path))


def _validate_current_identity(path: Path, identity: Mapping[str, object] | None) -> bool:
    if not isinstance(identity, Mapping):
        return False
    required = {"volume_id", "file_id", "size", "mtime_ns", "birthtime_ns"}
    if not required.issubset(identity):
        return False
    try:
        metadata = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            return False
        raw_volume = identity["volume_id"]
        raw_file = identity["file_id"]
        volume = _identity_number(raw_volume)
        file_id = _identity_number(raw_file)
        return (
            volume == metadata.st_dev
            and file_id == metadata.st_ino
            and int(identity["size"]) == metadata.st_size
            and int(identity["mtime_ns"]) == metadata.st_mtime_ns
            and int(identity["birthtime_ns"]) == getattr(metadata, "st_birthtime_ns", -1)
        )
    except (OSError, TypeError, ValueError):
        return False


def _identity_number(value: object) -> int:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return int.from_bytes(bytes(value), "little", signed=False)
    if isinstance(value, str):
        return int(value, 16)
    return int(value)


def _lookup(
    callback: Callable[[Path], object | None] | None,
    path: Path,
    *,
    label: str,
    issues: _Issues,
) -> object | None:
    if callback is None:
        issues.add(f"{label}_lookup_missing", f"no owner lookup was supplied for {label} survivors", path=path)
        return None
    try:
        return callback(path)
    except Exception as exc:
        issues.add(f"{label}_lookup_failed", f"{label} owner lookup failed: {type(exc).__name__}", path=path)
        return None


def _validate_receipt(root: Path, survivor: ClassifiedSurvivor, *, issues: _Issues) -> bool:
    receipt = survivor.receipt
    if not isinstance(receipt, OrganizationMoveReceipt):
        issues.add("organization_receipt_invalid", "classified survivor does not carry an owner move receipt", path=root / survivor.path)
        return False
    actual = _path(root, survivor.path, label="classified survivor", issues=issues)
    if actual is None:
        return False
    receipt_target = _path(root, receipt.target_path, label="organization receipt target", issues=issues)
    receipt_source = _path(root, receipt.source_path, label="organization receipt source", issues=issues)
    owner_root = _path(root, survivor.organization_root, label="classification owner root", issues=issues)
    valid = True
    if owner_root != root:
        issues.add("classification_owner_root_mismatch", "classification owner root differs from the corpus root", path=actual)
        valid = False
    if survivor.status != "applied" or receipt.status != "applied":
        issues.add("organization_receipt_not_applied", "classified survivor lacks applied organization status", path=actual)
        valid = False
    if receipt.schema != "neocortex.organization-move-receipt/v1" or not receipt.owner.strip():
        issues.add("organization_receipt_schema_invalid", "organization receipt schema/owner is invalid", path=actual)
        valid = False
    if receipt_target != actual:
        issues.add("organization_receipt_target_mismatch", "organization receipt target differs from current path", path=actual)
        valid = False
    if receipt_source is None or not receipt.source_absent:
        issues.add("organization_receipt_source_invalid", "organization receipt lacks source path/absence evidence", path=actual)
        valid = False
    if not _nonempty(receipt.source_identity) or not receipt.target_identity:
        issues.add("organization_receipt_identity_missing", "organization receipt lacks source/target identity evidence", path=actual)
        valid = False
    else:
        target_identity = receipt.target_identity
        if target_identity.get("path") != str(actual):
            issues.add("organization_receipt_identity_mismatch", "receipt target identity path differs from current path", path=actual)
            valid = False
        try:
            metadata = os.stat(actual, follow_symlinks=False)
            if int(target_identity["size"]) != metadata.st_size or int(target_identity["mtime_ns"]) != metadata.st_mtime_ns:
                raise ValueError("target metadata differs")
            observed_birthtime = getattr(metadata, "st_birthtime_ns", -1)
            if int(target_identity["birthtime_ns"]) != observed_birthtime:
                raise ValueError("target birthtime differs")
            raw_volume = target_identity["volume_id"]
            raw_file = target_identity["file_id"]
            volume = int(str(raw_volume), 16) if isinstance(raw_volume, str) else int(raw_volume)
            file_id = int(str(raw_file), 16) if isinstance(raw_file, str) else int(raw_file)
            if volume != metadata.st_dev or file_id != metadata.st_ino:
                raise ValueError("target physical identity differs")
        except (OSError, TypeError, ValueError):
            issues.add("organization_receipt_identity_mismatch", "receipt target identity differs from current file", path=actual)
            valid = False
    return valid


def _check_classified(
    root: Path,
    path: Path,
    lookup: ClassificationLookup | None,
    *,
    min_confidence: float,
    issues: _Issues,
) -> bool:
    record = _lookup(lookup, path, label="classified", issues=issues)
    if record is None:
        issues.add("classified_record_missing", "no current classification decision covers the physical survivor", path=path)
        return False
    if not isinstance(record, ClassifiedSurvivor):
        issues.add("classified_record_invalid", "classified owner lookup returned the wrong DTO", path=path)
        return False
    record_path = _path(root, record.path, label="classified record", issues=issues)
    valid = record_path == path
    if record_path != path:
        issues.add("classified_record_path_mismatch", "classified owner record does not identify the current file", path=path)
        valid = False
    if not _nonempty(record.classification):
        issues.add("classification_missing", "classified survivor has no classification", path=path)
        valid = False
    if isinstance(record.confidence, bool) or not isinstance(record.confidence, (int, float)) or not math.isfinite(float(record.confidence)) or record.confidence < min_confidence:
        issues.add("classification_confidence_insufficient", f"classification confidence is below caller policy {min_confidence:g}", path=path)
        valid = False
    if record.taxonomy_status.casefold() in {"unverified", "unknown", "review", "outside_taxonomy"}:
        issues.add("classification_taxonomy_unverified", "classified survivor taxonomy is not verified", path=path)
        valid = False
    if not _evidence_object(record.evidence):
        issues.add("classification_evidence_missing", "classified survivor has no classification evidence", path=path)
        valid = False
    if not _validate_receipt(root, record, issues=issues):
        valid = False
    return valid


def _check_residual(
    root: Path,
    path: Path,
    lookup: ResidualLookup | None,
    *,
    issues: _Issues,
    scan: _Scan,
) -> bool:
    record = _lookup(lookup, path, label="residual", issues=issues)
    if record is None:
        issues.add("residual_record_missing", "no current MIME decision covers the physical survivor", path=path)
        return False
    if not isinstance(record, ResidualSurvivor):
        issues.add("residual_record_invalid", "residual owner lookup returned the wrong DTO", path=path)
        return False
    record_path = _path(root, record.path, label="residual record", issues=issues)
    valid = record_path == path
    if record_path != path:
        issues.add("residual_record_path_mismatch", "residual owner record does not identify the current file", path=path)
        valid = False
    mime = _mime(record.mime)
    try:
        relative = path.relative_to(root / "Sin_clasificar" / "_MIME").parts
    except ValueError:
        relative = ()
    if mime is None:
        issues.add("residual_mime_invalid", "residual survivor has no valid MIME", path=path)
        valid = False
    elif len(relative) != 3 or relative[0] != mime.split("/", 1)[0] or relative[1] != mime.split("/", 1)[1]:
        issues.add("residual_mime_bucket_mismatch", "residual path does not match MIME major/subtype", path=path)
        valid = False
    else:
        if len(scan.mime_buckets) < 4_096:
            scan.mime_buckets.add((relative[0], relative[1]))
        else:
            issues.partial("mime_bucket_bound_exceeded", "MIME bucket sample reached its bound")
    if not _evidence_object(record.evidence):
        issues.add("residual_evidence_missing", "residual survivor has no MIME decision evidence", path=path)
        valid = False
    evidence = record.evidence if isinstance(record.evidence, Mapping) else {}
    if record.receipt is None:
        inventory_current = evidence.get("inventory_current")
        mime_identity = evidence.get("mime_identity")
        if not isinstance(inventory_current, Mapping) or not isinstance(mime_identity, Mapping):
            issues.add("residual_move_receipt_missing", "residual survivor lacks a move receipt or equivalent current inventory/MIME identity", path=path)
            valid = False
        elif inventory_current.get("path") != str(path) or mime_identity.get("path") != str(path):
            issues.add("residual_identity_path_mismatch", "residual current identity does not bind the final MIME path", path=path)
            valid = False
        elif not _validate_current_identity(path, inventory_current):
            issues.add("residual_inventory_identity_stale", "residual inventory identity does not match the final file", path=path)
            valid = False
        elif not _validate_current_identity(path, mime_identity.get("physical_identity")):
            issues.add("residual_mime_identity_stale", "residual MIME cache identity does not match the final file", path=path)
            valid = False
    elif not _nonempty(record.receipt):
        issues.add("residual_receipt_invalid", "residual move receipt is empty", path=path)
        valid = False
    elif isinstance(record.receipt, Mapping):
        required_receipt = {"source_path", "target_path", "source_absent"}
        if (
            not required_receipt.issubset(record.receipt)
            or record.receipt.get("target_path") != str(path)
            or record.receipt.get("source_absent") is not True
            or not _nonempty(record.receipt.get("source_path"))
            or not _nonempty(record.receipt.get("expected_identity", record.receipt.get("source_digest")))
        ):
            issues.add("residual_receipt_invalid", "residual move receipt lacks expected source/target identity", path=path)
            valid = False
    else:
        issues.add("residual_receipt_invalid", "residual move receipt has an unsupported shape", path=path)
        valid = False
    if record.status.casefold() not in {"unclassified", "residual"}:
        issues.add("residual_status_invalid", "residual owner status is not unclassified/residual", path=path)
        valid = False
    return valid


def _check_finding_stream(
    stream: Iterable[PendingFinding],
    *,
    expected_kind: Literal["junk", "duplicate", "producer", "policy"],
    issues: _Issues,
    max_records: int,
    root: Path,
) -> tuple[int, int]:
    observed = 0
    retained = 0
    try:
        iterator = iter(stream)
    except TypeError:
        issues.add("finding_stream_invalid", f"{expected_kind} finding stream is not iterable")
        return 0, 0
    try:
        for index, finding in enumerate(iterator):
            if index >= max_records:
                issues.partial("finding_stream_bound_exceeded", f"{expected_kind} findings exceed {max_records}")
                break
            observed += 1
            if not isinstance(finding, PendingFinding) or finding.kind != expected_kind:
                issues.add("finding_invalid", f"{expected_kind} stream returned the wrong DTO")
                continue
            status = finding.status.casefold()
            path = None
            if finding.path is not None:
                path = _path(root, finding.path, label=f"{expected_kind} finding", issues=issues)
            if status in _ALLOWED_RETAINED:
                retained += 1
                issues.add("allowed_exception", f"{expected_kind} finding retained as {status}", path=path, severity="warning")
            elif status in _RESOLVED:
                if path is not None and os.path.lexists(path):
                    issues.add("resolved_finding_still_present", f"{expected_kind} finding says {status} but path exists", path=path)
                continue
            else:
                detail = finding.detail.strip()
                suffix = f": {detail[:512]}" if detail else ""
                issues.add("actionable_finding_unresolved", f"{expected_kind} finding has unresolved status {status or 'missing'}{suffix}", path=path)
    except Exception as exc:
        issues.partial("finding_stream_failed", f"{expected_kind} finding stream failed: {type(exc).__name__}")
    return observed, retained


def _check_owner_paths(
    root: Path,
    stream: Iterable[OwnerCurrentPath],
    *,
    expected: tuple[str, ...],
    issues: _Issues,
    max_records: int,
) -> tuple[str, ...]:
    seen: set[str] = set()
    samples: list[str] = []
    try:
        iterator = iter(stream)
    except TypeError:
        issues.add("owner_path_stream_invalid", "owner current-path stream is not iterable")
        return ()
    try:
        for index, item in enumerate(iterator):
            if index >= max_records:
                issues.partial("owner_path_bound_exceeded", f"owner current paths exceed {max_records}")
                break
            if not isinstance(item, OwnerCurrentPath) or item.owner not in _FINAL_OWNERS:
                issues.add("owner_path_record_invalid", "owner current-path stream returned an invalid DTO")
                continue
            owner = item.owner
            seen.add(owner)
            if owner not in expected:
                # A full final snapshot is allowed at the pre-Semantic gate;
                # Semantic is simply not required until the second gate.
                continue
            if len(samples) < 32:
                samples.append(f"{owner}:{item.path}")
            if item.path is None and item.status == "empty":
                continue
            if item.status != "current":
                issues.add("owner_current_path_stale", f"owner {owner} path status is {item.status!r}", owner=owner)
                continue
            if item.path is None:
                if item.status == "empty":
                    continue
                issues.add("owner_current_path_missing", f"owner {owner} has no current path", owner=owner)
                continue
            path = Path(item.path)
            if not path.is_absolute():
                issues.add("owner_current_path_not_absolute", f"owner {owner} current path is not absolute", path=path, owner=owner)
                path = Path(os.path.abspath(os.fspath(path)))
            if not item.observed_exists or not os.path.lexists(path):
                issues.add("owner_current_path_stale", f"owner {owner} current path is absent", path=path, owner=owner)
                continue
            if not _validate_current_identity(path, item.physical_identity):
                issues.add("owner_current_identity_stale", f"owner {owner} current path identity does not match the live file", path=path, owner=owner)
    except Exception as exc:
        issues.partial("owner_path_stream_failed", f"owner current-path stream failed: {type(exc).__name__}")
    for owner in expected:
        if owner not in seen:
            issues.add("owner_current_paths_missing", f"owner {owner} supplied no current path", owner=owner)
    return tuple(samples)


def _scan_tree(
    start: Path,
    *,
    branch: Literal["ordered", "residual", "unknown"],
    root: Path,
    inputs: CorpusVerificationInputs,
    scan: _Scan,
    issues: _Issues,
    max_files: int,
    max_entries: int,
    empty_allowed: frozenset[Path],
    min_confidence: float,
) -> None:
    stack = [start]
    entries = 0
    while stack:
        if inputs.checkpoint is not None:
            inputs.checkpoint()
        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            issues.partial("filesystem_scan_failed", f"cannot scan {directory}: {type(exc).__name__}", path=directory)
            continue
        had_entry = False
        try:
            for entry in iterator:
                if inputs.checkpoint is not None:
                    inputs.checkpoint()
                had_entry = True
                entries += 1
                if entries > max_entries:
                    issues.partial("filesystem_scan_bound_exceeded", f"filesystem scan exceeds {max_entries} entries")
                    return
                entry_path = Path(entry.path)
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                except OSError as exc:
                    issues.partial("filesystem_stat_failed", f"cannot stat {entry_path}: {type(exc).__name__}", path=entry_path)
                    continue
                if stat.S_ISLNK(mode):
                    issues.add("symlink_survivor", "symbolic links are not accounted corpus survivors", path=entry_path)
                elif stat.S_ISDIR(mode):
                    stack.append(entry_path)
                elif stat.S_ISREG(mode):
                    if scan.files >= max_files:
                        issues.partial("filesystem_file_bound_exceeded", f"filesystem scan exceeds {max_files} files")
                        return
                    scan.files += 1
                    try:
                        scan.bytes += int(entry.stat(follow_symlinks=False).st_size)
                    except OSError as exc:
                        issues.partial("filesystem_stat_failed", f"cannot read size for {entry_path}: {type(exc).__name__}", path=entry_path)
                    if branch == "ordered":
                        scan.ordered += 1
                        if _check_classified(root, entry_path, inputs.classified_lookup, min_confidence=min_confidence, issues=issues):
                            scan.accounted += 1
                            _sample_append(scan.accounted_sample, entry_path)
                        else:
                            scan.unaccounted += 1
                            _sample_append(scan.unaccounted_sample, entry_path)
                    elif branch == "residual":
                        scan.residual += 1
                        if _check_residual(root, entry_path, inputs.residual_lookup, issues=issues, scan=scan):
                            scan.accounted += 1
                            _sample_append(scan.accounted_sample, entry_path)
                        else:
                            scan.unaccounted += 1
                            _sample_append(scan.unaccounted_sample, entry_path)
                    else:
                        scan.unaccounted += 1
                        _sample_append(scan.unaccounted_sample, entry_path)
                        issues.add("survivor_unaccounted", "physical survivor is outside the two allowed content branches", path=entry_path)
                else:
                    issues.add("non_regular_survivor", "special filesystem entry is not a valid corpus survivor", path=entry_path)
        except OSError as exc:
            issues.partial("filesystem_scan_failed", f"cannot enumerate {directory}: {type(exc).__name__}", path=directory)
        finally:
            iterator.close()
        if not had_entry and directory not in empty_allowed:
            scan.empty_directories += 1
            issues.add("unexpected_empty_directory", "empty directory is not one of the three structural roots", path=directory)


def _scan(
    root: Path,
    inputs: CorpusVerificationInputs,
    *,
    issues: _Issues,
    max_files: int,
    min_confidence: float,
) -> _Scan:
    scan = _Scan()
    _validate_structural_roots(root, issues)
    structural = frozenset({root / item for item in STRUCTURAL_ROOTS})
    try:
        iterator = os.scandir(root)
    except OSError as exc:
        issues.partial("filesystem_scan_failed", f"cannot enumerate corpus root: {type(exc).__name__}", path=root)
        return scan
    top_entries = 0
    try:
        for entry in iterator:
            top_entries += 1
            if top_entries > 100_000:
                issues.partial("top_level_bound_exceeded", "corpus root has more than 100000 entries")
                break
            entry_path = Path(entry.path)
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                issues.partial("filesystem_stat_failed", f"cannot stat {entry_path}: {type(exc).__name__}", path=entry_path)
                continue
            if entry.name not in _TOP_LEVEL_ROOTS:
                issues.add("unexpected_top_level", "only Corpus_ordenado and Sin_clasificar may be top-level", path=entry_path)
            if stat.S_ISLNK(mode):
                issues.add("structural_root_invalid", "top-level entry is a symbolic link", path=entry_path)
                continue
            if not stat.S_ISDIR(mode):
                issues.add("unexpected_top_level_file", "top-level corpus content must be a directory", path=entry_path)
                continue
            if entry.name == "Corpus_ordenado":
                _scan_tree(entry_path, branch="ordered", root=root, inputs=inputs, scan=scan, issues=issues, max_files=max_files, max_entries=max(100_000, max_files * 4), empty_allowed=structural, min_confidence=min_confidence)
            elif entry.name == "Sin_clasificar":
                _scan_tree(entry_path, branch="residual", root=root, inputs=inputs, scan=scan, issues=issues, max_files=max_files, max_entries=max(100_000, max_files * 4), empty_allowed=structural, min_confidence=min_confidence)
            else:
                _scan_tree(entry_path, branch="unknown", root=root, inputs=inputs, scan=scan, issues=issues, max_files=max_files, max_entries=max(100_000, max_files * 4), empty_allowed=structural, min_confidence=min_confidence)
    except OSError as exc:
        issues.partial("filesystem_scan_failed", f"cannot enumerate corpus root: {type(exc).__name__}", path=root)
    finally:
        iterator.close()
    return scan


def _report_metric(report: Mapping[str, object], key: str) -> int | None:
    value = report.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _metrics(inputs: CorpusVerificationInputs, scan: _Scan, *, empty_removed: int | None, blocked: int, recovery: int, current_paths: int) -> dict[str, int | None]:
    names = (
        "inventory_files",
        "preclean_matched",
        "preclean_trashed",
        "identify_checked",
        "identify_cache_hits",
        "identify_unknown",
        "renamed",
        "content_cleanup_matched",
        "content_cleanup_trashed",
        "archives_examined",
        "archives_extracted",
        "archives_trash_after_extract",
        "duplicates_removed",
        "duplicate_bytes_reclaimed",
        "route_survivors",
        "expensive_work_avoided",
    )
    result = {name: _report_metric(inputs.report_metrics, name) for name in names}
    result.update(
        {
            "classified": scan.ordered,
            "organized": scan.ordered,
            "unclassified": scan.residual,
            "mime_buckets": len(scan.mime_buckets),
            "empty_directories_removed": empty_removed,
            "blocked": blocked,
            "recovery_required": recovery,
            "final_files": scan.files,
            "final_bytes": scan.bytes,
            "accounted_files": scan.accounted,
            "unaccounted_files": scan.unaccounted,
            "current_paths_checked": current_paths,
        }
    )
    return result


def verify_corpus(
    root: str | Path,
    inputs: CorpusVerificationInputs,
    *,
    phase: VerificationPhase,
    min_confidence: float,
    max_files: int = 100_000,
    max_owner_records: int = 100_000,
    max_issues: int = 256,
) -> CorpusVerificationResult:
    """Verify a corpus without opening an active owner or applying an effect."""

    issues = _Issues(max_items=max_issues if isinstance(max_issues, int) and max_issues > 0 else 1)
    if phase not in {"before_semantic", "after_semantic"}:
        issues.add("invalid_phase", f"unsupported phase {phase!r}")
        phase = "after_semantic"
    if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)) or not math.isfinite(float(min_confidence)) or not 0.0 <= float(min_confidence) <= 1.0:
        issues.add("invalid_policy", "caller confidence policy is invalid")
        min_confidence = 0.0
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
        issues.add("invalid_bound", "max_files must be positive")
        max_files = 1
    if isinstance(max_owner_records, bool) or not isinstance(max_owner_records, int) or max_owner_records < 1:
        issues.add("invalid_bound", "max_owner_records must be positive")
        max_owner_records = 1
    root_path = _root_path(root, issues)
    if not isinstance(inputs, CorpusVerificationInputs):
        issues.add("inputs_invalid", "verification requires CorpusVerificationInputs DTO")
        inputs = CorpusVerificationInputs(None, None, ())
    scan = _scan(root_path, inputs, issues=issues, max_files=max_files, min_confidence=float(min_confidence))
    expected = _POST_SEMANTIC_OWNERS if phase == "before_semantic" else _FINAL_OWNERS
    owner_samples = _check_owner_paths(root_path, inputs.current_paths, expected=expected, issues=issues, max_records=max_owner_records)
    finding_counts = []
    for stream, kind in (
        (inputs.actionable_junk, "junk"),
        (inputs.actionable_duplicates, "duplicate"),
        (inputs.producers_pending, "producer"),
        (inputs.policy_findings, "policy"),
    ):
        finding_counts.append(_check_finding_stream(stream, expected_kind=kind, issues=issues, max_records=max_owner_records, root=root_path))
    retained = sum(item[1] for item in finding_counts)
    recovery = sum(
        1
        for item in issues.items
        if item.code == "allowed_exception" and "recovery_required" in item.detail
    )
    empty_removed_value = inputs.report_metrics.get("empty_directories_removed")
    empty_removed = empty_removed_value if isinstance(empty_removed_value, int) and not isinstance(empty_removed_value, bool) and empty_removed_value >= 0 else None
    metrics = _metrics(inputs, scan, empty_removed=empty_removed, blocked=retained, recovery=recovery, current_paths=len(owner_samples))
    errors = tuple(item for item in issues.items if item.severity == "error")
    if errors:
        status: VerificationStatus = "partial" if issues.coverage_partial else "blocked"
        coverage: Literal["complete", "partial"] = "partial" if issues.coverage_partial else "complete"
    elif issues.coverage_partial:
        status = "partial"
        coverage = "partial"
    else:
        status = "complete"
        coverage = "complete"
    return CorpusVerificationResult(
        phase=phase,
        root=str(root_path),
        status=status,
        coverage=coverage,
        passed=status == "complete" and coverage == "complete",
        issues=tuple(issues.items),
        metrics=metrics,
        accounted_paths=tuple(scan.accounted_sample),
        unaccounted_paths=tuple(scan.unaccounted_sample),
        owner_paths_checked=owner_samples,
    )


def verify_before_semantic(
    root: str | Path,
    inputs: CorpusVerificationInputs,
    *,
    min_confidence: float,
    max_files: int = 100_000,
    max_owner_records: int = 100_000,
    max_issues: int = 256,
) -> CorpusVerificationResult:
    """Verify layout and post-move non-Semantic owners before Semantic."""

    return verify_corpus(
        root,
        inputs,
        phase="before_semantic",
        min_confidence=min_confidence,
        max_files=max_files,
        max_owner_records=max_owner_records,
        max_issues=max_issues,
    )


def verify_after_semantic(
    root: str | Path,
    inputs: CorpusVerificationInputs,
    *,
    min_confidence: float,
    max_files: int = 100_000,
    max_owner_records: int = 100_000,
    max_issues: int = 256,
) -> CorpusVerificationResult:
    """Verify the terminal layout and all five owner current-path streams."""

    return verify_corpus(
        root,
        inputs,
        phase="after_semantic",
        min_confidence=min_confidence,
        max_files=max_files,
        max_owner_records=max_owner_records,
        max_issues=max_issues,
    )


verify_final_corpus = verify_after_semantic


__all__ = [
    "SCHEMA",
    "STRUCTURAL_ROOTS",
    "ClassifiedSurvivor",
    "CorpusVerificationInputs",
    "CorpusVerificationIssue",
    "CorpusVerificationResult",
    "CorpusVerifier",
    "OrganizationMoveReceipt",
    "OwnerCurrentPath",
    "PendingFinding",
    "ResidualSurvivor",
    "verify_after_semantic",
    "verify_before_semantic",
    "verify_corpus",
    "verify_final_corpus",
]
