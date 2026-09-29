"""Materialize surviving, not-yet-organized files into MIME buckets.

This owner is deliberately smaller than the document-organization planner.  A
residual move has no semantic destination: it preserves the physical basename
and places the file below ``Sin_clasificar/_MIME/<major>/<subtype>``.  MIME is
accepted only from a current, identity-bound Identify observation; a suffix is
never used as a type guess.

The physical boundary is shared with document organization.  In particular,
there is one POSIX no-replace backend, one Framework action-intent/receipt
contract, and one cache-rebinding path.  A residual without a catalog owner is
valid input: its owner fields remain ``None`` and only existing owner rows are
reconciled.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.platform.content_types import DETECTOR_VERSION, FileTypeDecision, identify
from neocortex.platform.logical_filename import LogicalFilename, collision_path, identity_token
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.safety.corpus_access import CorpusMutationGuard, path_trees_intersect
from neocortex.workflow.actions.file_action_recovery import (
    expected_identity_json,
)
from neocortex.persistence.state_publication import (
    StatePublicationError,
    read_state_publication_state,
)

from .document_cache_sync import (
    DocumentMoveTransition,
    DocumentCacheSyncResult,
    synchronize_moved_documents,
)
from .document_catalog import document_catalog_database
from .document_resource_binding import (
    ResourceBindingError,
    parse_resource_binding,
    rebind_resource_binding_path,
)

if TYPE_CHECKING:  # pragma: no cover - imports are for static consumers only
    pass


RESIDUAL_MATERIALIZATION_SCHEMA = "neocortex.residual-materialization/v1"
RESIDUAL_MOVE_RECEIPT_SCHEMA = "neocortex.residual-move-receipt/v1"
UNKNOWN_MIME = "application/octet-stream"
CORPUS_ORDERED_DIRECTORY = "Corpus_ordenado"
UNCLASSIFIED_DIRECTORY = "Sin_clasificar"
MIME_DIRECTORY = "_MIME"
_SUPPORTED_SOURCE_KINDS = frozenset(
    {"pdf", "docx", "xlsx", "pptx", "odt", "text", "audio", "image", "video"}
)
_MIME_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+\-]*$")
_MOVE_BACKEND_NAME = "posix-link-unlink-no-replace-v1"
_STATUS = Literal[
    "planned",
    "moved",
    "recovered",
    "already_materialized",
    "moved_cache_pending",
    "stale",
    "blocked",
    "recovery_required",
    "failed",
    "skipped_classified",
]


@dataclass(frozen=True, slots=True)
class ResidualSurvivor:
    """One physical survivor and optional, non-authoritative owner metadata."""

    path: Path
    snapshot: FileSnapshot | None = None
    source_kind: str | None = None
    file_key: str | None = None

    def __post_init__(self) -> None:
        path = Path(os.path.abspath(os.fspath(self.path)))
        object.__setattr__(self, "path", path)
        if self.source_kind is not None and self.source_kind not in _SUPPORTED_SOURCE_KINDS:
            raise ValueError("source_kind must be a supported owner kind or None")
        if self.file_key is not None and (
            not isinstance(self.file_key, str) or not self.file_key.strip()
        ):
            raise ValueError("file_key must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class ResidualMimeDecision:
    """An optional Identify result supplied by an upstream pipeline stage.

    Identity fields are a fence, not a replacement for a current detector
    observation.  The materializer re-identifies an existing source and uses
    this record only when its physical fence still matches.
    """

    path: Path
    mime: str | None = None
    volume_id: int | str | None = None
    file_id: int | str | None = None
    size: int | None = None
    mtime_ns: int | None = None
    birthtime_ns: int | None = None
    detector_version: str | None = None
    evidence: str = ""
    confidence: str = "none"
    source_kind: str | None = None
    file_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(os.path.abspath(os.fspath(self.path))))


@dataclass(frozen=True, slots=True)
class ResidualMaterializationConfig:
    """Execution boundary for one materialization pass."""

    corpus_root: Path
    apply: bool = False
    preview: bool = False
    state_directory: Path | None = None
    catalog_path: Path | None = None
    framework_state: Any | None = None
    run_id: int | None = None
    mutation_guard: CorpusMutationGuard | None = None
    framework_lock_held: bool = False
    max_actions: int | None = None
    checkpoint: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        root = Path(os.path.abspath(os.fspath(self.corpus_root)))
        object.__setattr__(self, "corpus_root", root)
        if self.state_directory is not None:
            object.__setattr__(
                self,
                "state_directory",
                Path(os.path.abspath(os.fspath(self.state_directory))),
            )
        if self.catalog_path is not None:
            object.__setattr__(
                self,
                "catalog_path",
                Path(os.path.abspath(os.fspath(self.catalog_path))),
            )
        if type(self.apply) is not bool or type(self.preview) is not bool:
            raise TypeError("apply and preview must be boolean")
        if self.apply and self.preview:
            raise ValueError("preview cannot be combined with apply")
        if type(self.framework_lock_held) is not bool:
            raise TypeError("framework_lock_held must be boolean")
        if self.max_actions is not None and (
            type(self.max_actions) is not int or self.max_actions < 1
        ):
            raise ValueError("max_actions must be positive when supplied")
        if self.run_id is not None and (type(self.run_id) is not int or self.run_id < 1):
            raise ValueError("run_id must be a positive integer when supplied")


@dataclass(frozen=True, slots=True)
class ResidualMoveResult:
    source_path: str
    target_path: str | None
    mime: str
    status: _STATUS
    detail: str | None = None
    source_kind: str | None = None
    file_key: str | None = None
    receipt_json: str | None = None
    cache_sync_json: str | None = None
    action_id: int | None = None

    @property
    def effect_applied(self) -> bool:
        return self.status in {
            "moved",
            "recovered",
            "already_materialized",
            "moved_cache_pending",
        }


@dataclass(frozen=True, slots=True)
class ResidualMaterializationResult:
    """Bounded, serializable result for preview, apply, and recovery."""

    schema: str = RESIDUAL_MATERIALIZATION_SCHEMA
    preview: bool = True
    selected: int = 0
    planned: int = 0
    moved: int = 0
    recovered: int = 0
    already_materialized: int = 0
    stale: int = 0
    blocked: int = 0
    failed: int = 0
    recovery_required: int = 0
    cache_synced: int = 0
    cache_pending: int = 0
    unclassified: int = 0
    error: str | None = None
    mime_buckets: tuple[tuple[str, int], ...] = ()
    moves: tuple[ResidualMoveResult, ...] = ()

    @property
    def complete(self) -> bool:
        return not (
            self.stale
            or self.blocked
            or self.failed
            or self.recovery_required
            or self.cache_pending
        )

    @property
    def has_unresolved(self) -> bool:
        return not self.complete

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["mime_buckets"] = dict(self.mime_buckets)
        payload["moves"] = [asdict(item) for item in self.moves]
        payload["complete"] = self.complete
        payload["effects"] = {
            "corpus": "filesystem_moves" if not self.preview else "none",
            "state": "intents_receipts_cache_rebind" if not self.preview else "none",
            "external": "none",
        }
        return payload

    def as_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


@dataclass(frozen=True, slots=True)
class _PreparedResidual:
    survivor: ResidualSurvivor
    expected: FileSnapshot | None
    target: Path | None
    mime: str
    evidence: str
    status: _STATUS
    detail: str | None = None


class _ResidualStale(RuntimeError):
    pass


def _reject_symlink_prefix(path: Path, *, root: Path, allow_missing_tail: bool) -> None:
    """Reject symlink/reparse ancestors without resolving an input alias."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes corpus root: {path}") from exc
    current = root
    for index, component in enumerate(relative.parts):
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_tail:
                return
            raise
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"residual path traverses a symbolic link: {current}")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(current)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _same_snapshot(left: FileSnapshot, right: FileSnapshot) -> bool:
    return (
        left.identity == right.identity
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
        and left.birthtime_ns == right.birthtime_ns
    )


def _scan_residual_files(root: Path) -> Iterable[Path]:
    """Stream a deterministic scan without following symlink directories."""

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            candidate = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if candidate.name == CORPUS_ORDERED_DIRECTORY:
                        continue
                    stack.append(candidate)
                elif entry.is_file(follow_symlinks=False):
                    yield candidate
            except OSError:
                continue


def _mapping_value(value: object, key: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _snapshot_from_value(value: object, *, fallback_path: Path | None = None) -> FileSnapshot | None:
    if isinstance(value, FileSnapshot):
        return value
    raw = value
    if raw is None:
        return None
    if isinstance(value, Mapping) and "snapshot" in value:
        raw = value.get("snapshot")
    elif not isinstance(value, Mapping) and hasattr(value, "snapshot"):
        raw = value.snapshot
    if isinstance(raw, FileSnapshot):
        return raw
    path_value = _mapping_value(raw, "path", fallback_path)
    fields = ("volume_id", "file_id", "size", "mtime_ns", "birthtime_ns")
    if path_value is None or any(_mapping_value(raw, name) is None for name in fields):
        return None
    try:
        return FileSnapshot(
            str(Path(os.path.abspath(os.fspath(path_value)))),
            int(_mapping_value(raw, "volume_id")),
            int(_mapping_value(raw, "file_id")),
            int(_mapping_value(raw, "size")),
            int(_mapping_value(raw, "mtime_ns")),
            int(_mapping_value(raw, "birthtime_ns")),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_survivor(value: object) -> ResidualSurvivor:
    if isinstance(value, ResidualSurvivor):
        return value
    if isinstance(value, (str, os.PathLike, Path)):
        return ResidualSurvivor(Path(os.fspath(value)))
    path_value = _mapping_value(value, "path")
    if path_value is None:
        path_value = _mapping_value(value, "source_path")
    if path_value is None:
        raise ValueError("residual survivor has no path")
    snapshot = _snapshot_from_value(value, fallback_path=Path(os.fspath(path_value)))
    return ResidualSurvivor(
        Path(os.fspath(path_value)),
        snapshot=snapshot,
        source_kind=_optional_text(_mapping_value(value, "source_kind")),
        file_key=_optional_text(_mapping_value(value, "file_key")),
    )


def _coerce_decision(value: object, *, path: Path) -> ResidualMimeDecision:
    if isinstance(value, ResidualMimeDecision):
        return value
    if isinstance(value, FileTypeDecision):
        return ResidualMimeDecision(
            path=path,
            mime=value.mime,
            evidence=value.evidence or ("unknown" if value.status == "unknown" else ""),
            confidence=value.confidence,
        )
    mime = _mapping_value(value, "mime")
    if mime is None:
        detected = _mapping_value(value, "detected_type")
        mime = _mapping_value(detected, "mime")
    return ResidualMimeDecision(
        path=Path(os.fspath(_mapping_value(value, "path", path))),
        mime=None if mime is None else str(mime),
        volume_id=_mapping_value(value, "volume_id"),
        file_id=_mapping_value(value, "file_id"),
        size=_as_int(_mapping_value(value, "size")),
        mtime_ns=_as_int(_mapping_value(value, "mtime_ns")),
        birthtime_ns=_as_int(_mapping_value(value, "birthtime_ns")),
        detector_version=_optional_text(_mapping_value(value, "detector_version")),
        evidence=str(_mapping_value(value, "evidence", "") or ""),
        confidence=str(_mapping_value(value, "confidence", "none") or "none"),
        source_kind=_optional_text(_mapping_value(value, "source_kind")),
        file_key=_optional_text(_mapping_value(value, "file_key")),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _as_int(value: object) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _decision_index(decisions: object) -> dict[str, object]:
    if decisions is None:
        return {}
    if callable(decisions):
        return {}
    if isinstance(decisions, Mapping):
        if "path" in decisions or "source_path" in decisions:
            path = _mapping_value(decisions, "path") or _mapping_value(decisions, "source_path")
            return {_path_index_key(Path(os.fspath(path))): decisions}
        return {
            _path_index_key(Path(os.fspath(path))): (
                ResidualMimeDecision(path=Path(os.fspath(path)), evidence="unknown")
                if decision is None
                else decision
            )
            for path, decision in decisions.items()
        }
    result: dict[str, object] = {}
    for decision in decisions:  # type: ignore[union-attr]
        path = _mapping_value(decision, "path")
        if path is None:
            path = _mapping_value(decision, "source_path")
        if path is not None:
            result[_path_index_key(Path(os.fspath(path)))] = (
                ResidualMimeDecision(path=Path(os.fspath(path)), evidence="unknown")
                if decision is None
                else decision
            )
    return result


def _decision_for(
    decisions: object,
    index: Mapping[str, object],
    survivor: ResidualSurvivor,
) -> object | None:
    if callable(decisions):
        return decisions(survivor)
    return index.get(_path_index_key(survivor.path))


def _path_index_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _valid_mime(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if candidate.count("/") != 1 or any(
        not _MIME_COMPONENT.fullmatch(component) for component in candidate.split("/")
    ):
        return None
    return candidate


def _identity_matches_decision(snapshot: FileSnapshot, decision: ResidualMimeDecision) -> bool:
    supplied = (
        decision.volume_id,
        decision.file_id,
        decision.size,
        decision.mtime_ns,
        decision.birthtime_ns,
    )
    if any(value is None for value in supplied):
        return False
    try:
        return (
            snapshot.volume_id == int(decision.volume_id)
            and snapshot.file_id == int(decision.file_id)
            and snapshot.size == int(decision.size)
            and snapshot.mtime_ns == int(decision.mtime_ns)
            and snapshot.birthtime_ns == int(decision.birthtime_ns)
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _resolve_mime(
    path: Path,
    *,
    expected: FileSnapshot | None,
    decision_value: object | None,
) -> tuple[FileSnapshot, str, str]:
    """Return a current snapshot and identity-bound Identify MIME decision."""

    current = snapshot_path(path)
    if expected is not None and not _same_snapshot(expected, current):
        raise _ResidualStale("survivor identity or metadata changed")
    decision = None if decision_value is None else _coerce_decision(decision_value, path=path)
    if decision is not None:
        if not _same_path(decision.path, path):
            raise _ResidualStale("Identify decision path differs from survivor")
        decision_identity_valid = (
            _identity_matches_decision(current, decision)
            and decision.detector_version == DETECTOR_VERSION
        )
        if not decision_identity_valid:
            return current, UNKNOWN_MIME, "identify:abstain:invalid-decision-fence"
        # Identify already ran upstream for this identity.  Reusing that
        # bounded decision avoids reopening/re-reading a large survivor merely
        # to choose its residual bucket.  A detector-version mismatch is the
        # one explicit reason to take the local fallback below.
        supplied_mime = _valid_mime(decision.mime)
        if supplied_mime is not None:
            evidence = (
                f"identify:validated:{decision.detector_version}:"
                f"{decision.evidence or 'decision'}"
            )
            return current, supplied_mime, evidence
        if (
            supplied_mime is None
            and decision.evidence in {"unknown", "identify:unknown"}
        ):
            return (
                current,
                UNKNOWN_MIME,
                f"identify:validated:{decision.detector_version}:unknown",
            )
    try:
        observed = identify(path)
    except (OSError, ValueError, RecursionError) as exc:
        observed = None
        observe_error = f"identify:{type(exc).__name__}"
    else:
        observe_error = ""
    refreshed = snapshot_path(path)
    if not _same_snapshot(current, refreshed):
        raise _ResidualStale("file changed during Identify")
    if expected is not None and not _same_snapshot(expected, refreshed):
        raise _ResidualStale("survivor identity changed during Identify")
    mime = _valid_mime(None if observed is None else observed.mime)
    if mime is None:
        mime = UNKNOWN_MIME
    evidence = (
        f"identify:{DETECTOR_VERSION}:{observed.evidence}"
        if observed is not None and observed.evidence
        else observe_error or "identify:unknown"
    )
    return refreshed, mime, evidence


def _mime_target(root: Path, mime: str, basename: str) -> Path:
    major, subtype = mime.split("/", 1)
    return root / UNCLASSIFIED_DIRECTORY / MIME_DIRECTORY / major / subtype / basename


def _is_in_ordered(root: Path, path: Path) -> bool:
    return path.is_relative_to(root / CORPUS_ORDERED_DIRECTORY)


def _is_in_mime_root(root: Path, path: Path) -> bool:
    return path.is_relative_to(root / UNCLASSIFIED_DIRECTORY / MIME_DIRECTORY)


def _target_matches_expected(target: Path, expected: FileSnapshot | None) -> bool:
    if expected is None or not target.is_file() or target.is_symlink():
        return False
    try:
        return _same_snapshot(expected, snapshot_path(target))
    except OSError:
        return False


class _DestinationReservation:
    """Cross-page collision reservation backed by Framework TEMP state."""

    _FALLBACK_LIMIT = 4096

    def __init__(self, connection: object | None, *, run_id: int | None):
        self._connection = connection
        self._run_id = 0 if run_id is None else int(run_id)
        self._table = f"_neocortex_residual_mime_reservations_{id(self):x}"
        self._savepoint = 0
        self._created = False
        self._fallback: set[str] | None = None
        if connection is None:
            self._fallback = set()
            return
        try:
            self._temp_write(
                f"CREATE TEMP TABLE {self._table}("
                "run_id INTEGER NOT NULL,target_path TEXT NOT NULL,"
                "PRIMARY KEY(run_id,target_path))"
            )
            self._created = True
        except BaseException:
            self._created = False
            raise

    def _temp_write(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        self._savepoint += 1
        name = f"residual_reservation_{self._savepoint}"
        self._connection.execute(f"SAVEPOINT {name}")
        try:
            self._connection.execute(sql, parameters)
            self._connection.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            try:
                self._connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self._connection.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                pass
            raise

    def contains(self, path: Path) -> bool:
        key = _path_index_key(path)
        if self._fallback is not None:
            return key in self._fallback
        row = self._connection.execute(
            f"SELECT 1 FROM {self._table} WHERE run_id=? AND target_path=?",
            (self._run_id, key),
        ).fetchone()
        return row is not None

    def add(self, path: Path) -> None:
        key = _path_index_key(path)
        if self._fallback is not None:
            if len(self._fallback) >= self._FALLBACK_LIMIT and key not in self._fallback:
                raise RuntimeError("residual preview collision reservation limit reached")
            self._fallback.add(key)
            return
        self._temp_write(
            f"INSERT OR IGNORE INTO {self._table}(run_id,target_path) VALUES(?,?)",
            (self._run_id, key),
        )

    def close(self) -> None:
        if self._fallback is not None or not self._created:
            return
        try:
            self._temp_write(f"DROP TABLE {self._table}")
        finally:
            self._created = False


@contextmanager
def _reservation_scope(reservation: _DestinationReservation, lock: object):
    try:
        with lock:
            yield
    finally:
        reservation.close()


def _collision_target(
    requested: Path,
    *,
    expected: FileSnapshot,
    reserved: _DestinationReservation,
) -> Path:
    token = identity_token(
        expected.volume_id,
        expected.file_id,
        expected.birthtime_ns,
        expected.size,
        expected.mtime_ns,
    )
    for attempt in range(1, 1001):
        candidate = collision_path(requested, token, attempt=attempt)
        if (
            not reserved.contains(candidate)
            and (
                not os.path.lexists(candidate)
                or _target_matches_expected(candidate, expected)
            )
        ):
            return candidate
    raise RuntimeError("residual collision disambiguation exhausted")


def _residual_receipt(source: Path, target: Path, snapshot: FileSnapshot) -> str:
    # Keep receipt construction in the organization owner.  The lazy import
    # avoids the application -> residual-materializer import cycle while both
    # paths still emit the same physical receipt contract.
    from .document_organization_application import _organization_move_receipt

    payload = json.loads(
        _organization_move_receipt(
            source,
            target,
            snapshot,
            detail="recovered a completed residual MIME move",
        )
    )
    payload.update(
        {
            "backend": _MOVE_BACKEND_NAME,
            "residual_receipt_schema": RESIDUAL_MOVE_RECEIPT_SCHEMA,
        }
    )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _residual_receipt_from_backend(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("residual backend receipt is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("residual backend receipt is not an object")
    payload["residual_receipt_schema"] = RESIDUAL_MOVE_RECEIPT_SCHEMA
    payload.setdefault("backend", _MOVE_BACKEND_NAME)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_residual_receipt(
    receipt: str,
    *,
    source: Path,
    target: Path,
    expected: FileSnapshot,
) -> None:
    from .document_organization_application import _validate_organization_move_receipt

    _validate_organization_move_receipt(
        receipt,
        source=source,
        destination=target,
        expected=expected,
    )


def _framework_connection(framework_state: object | None):
    connection = getattr(framework_state, "_connection", None)
    if connection is None or not hasattr(connection, "execute"):
        return None
    return connection


def _framework_action_status(framework_state: object, action_id: int) -> str | None:
    connection = _framework_connection(framework_state)
    if connection is None:
        return None
    row = connection.execute(
        "SELECT status FROM file_actions WHERE action_id=?", (action_id,)
    ).fetchone()
    return None if row is None else str(row[0])


def _find_pending_framework_action(
    framework_state: object | None,
    *,
    run_id: int | None,
    source: Path,
    target: Path,
    mime: str,
) -> int | None:
    connection = _framework_connection(framework_state)
    if connection is None or run_id is None:
        return None
    row = connection.execute(
        """SELECT action_id FROM file_actions
        WHERE run_id=? AND action_type='residual_mime_move'
        AND source_path=? AND target_path=? AND detected_mime=?
        AND status IN ('started','applying')
        ORDER BY action_id DESC LIMIT 1""",
        (run_id, str(source), str(target), mime),
    ).fetchone()
    return None if row is None else int(row[0])


def _begin_framework_action(
    framework_state: object | None,
    *,
    run_id: int | None,
    source: Path,
    target: Path,
    mime: str,
    evidence: str,
) -> tuple[int | None, str | None]:
    if framework_state is None or run_id is None:
        return None, None
    begin = getattr(framework_state, "begin_file_action", None)
    if not callable(begin):
        return None, "framework state does not expose file-action intents"
    detail = json.dumps(
        {
            "schema": RESIDUAL_MATERIALIZATION_SCHEMA,
            "detector_evidence": evidence,
            "mime": mime,
            "logical_filename": LogicalFilename.parse(source).normalized_basename,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        action_id = int(
            begin(
                run_id,
                "residual_mime_move",
                str(source),
                str(target),
                mime,
                detail,
                True,
            )
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return None, f"residual intent could not be recorded: {type(exc).__name__}: {exc}"
    return action_id, None


def _mark_framework_frontier(
    framework_state: object | None,
    action_id: int | None,
    expected: FileSnapshot,
    *,
    source: Path,
    target: Path,
) -> str | None:
    if framework_state is None or action_id is None:
        return None
    status = _framework_action_status(framework_state, action_id)
    if status != "started":
        return None
    try:
        framework_state.mark_file_actions_applying(
            (
                (
                    action_id,
                    expected_identity_json(
                        expected,
                        source_path=str(source),
                        target_path=str(target),
                    ),
                ),
            )
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return f"residual mutation frontier could not be recorded: {type(exc).__name__}: {exc}"
    return None


def _finish_framework(
    framework_state: object | None,
    action_id: int | None,
    *,
    status: Literal["applied", "skipped", "recovery_required"],
    detail: str | None = None,
    receipt: str | None = None,
) -> str | None:
    if framework_state is None or action_id is None:
        return None
    try:
        current = _framework_action_status(framework_state, action_id)
        if status == "applied":
            if current == "applied":
                return None
            if current != "applying" or receipt is None:
                return f"residual action cannot be confirmed from {current!r}"
            framework_state.confirm_file_actions_applied(((action_id, receipt),))
        elif status == "skipped":
            if current in {"started", "applying"}:
                framework_state.finish_file_action(action_id, "skipped", detail)
        else:
            if current in {"started", "applying"}:
                framework_state.require_file_action_recovery((action_id,), detail or "recovery required")
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return f"residual action ledger update failed: {type(exc).__name__}: {exc}"
    return None


def _persist_physical_receipt(
    framework_state: object | None,
    action_id: int | None,
    receipt: str | None,
) -> str | None:
    """Persist the physical receipt before any Catalog/cache owner write."""

    if framework_state is None or action_id is None or receipt is None:
        return None
    current = _framework_action_status(framework_state, action_id)
    if current == "applied":
        return None
    if current != "applying":
        return f"physical receipt cannot be persisted from framework action {current!r}"
    try:
        framework_state.confirm_file_actions_applied(((action_id, receipt),))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return f"physical receipt persistence failed: {type(exc).__name__}: {exc}"
    return None


def _identity_integer(value: object) -> int | None:
    try:
        text = str(value)
        return int(text, 0) if text.lower().startswith("0x") else int(text, 10)
    except (TypeError, ValueError, OverflowError):
        return None


def _rebind_catalog_document(
    catalog_path: Path | None,
    *,
    source: Path,
    target: Path,
    expected: FileSnapshot,
    source_kind: str | None,
    file_key: str | None,
    receipt: str | None,
) -> tuple[bool, str | None]:
    """Rebind only a proven current catalog owner; history stays immutable."""

    if catalog_path is None or not catalog_path.is_file():
        return True, None
    try:
        with document_catalog_database(catalog_path) as connection:
            if source_kind is not None and file_key is not None:
                row = connection.execute(
                    """SELECT source_kind,file_key,path,volume_id,file_id,
                    birthtime_ns,size,mtime_ns,resource_binding_json
                    FROM documents WHERE source_kind=? AND file_key=? AND active=1""",
                    (source_kind, file_key),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT source_kind,file_key,path,volume_id,file_id,
                    birthtime_ns,size,mtime_ns,resource_binding_json
                    FROM documents WHERE path IN (?,?) AND active=1
                    ORDER BY CASE path WHEN ? THEN 0 ELSE 1 END LIMIT 1""",
                    (str(source), str(target), str(source)),
                ).fetchone()
            if row is None:
                return True, None
            current_path = Path(str(row["path"]))
            if not (_same_path(current_path, source) or _same_path(current_path, target)):
                return False, "catalog current path is neither residual source nor destination"
            raw_binding = row["resource_binding_json"]
            if raw_binding is None:
                return False, "catalog owner has no physical resource binding"
            rebound = rebind_resource_binding_path(
                raw_binding,
                expected_path=str(source),
                new_path=str(target),
            )
            if (
                rebound.get("source_kind") != str(row["source_kind"])
                or rebound.get("file_key") != str(row["file_key"])
            ):
                return False, "catalog resource binding owner differs from current row"
            binding = parse_resource_binding(
                json.dumps(rebound, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            physical = binding["physical_identity"]
            packed = physical["packed_key"]
            # Parsing above validates the shape; compare only the identity
            # fields that the current filesystem observation can prove.
            from neocortex.foundation.file_identity import FileIdentity, FileIdentityEncoding

            identity = FileIdentity.decode(packed, encoding=FileIdentityEncoding.PACKED_HEX_V1)
            if (identity.volume_id, identity.file_id) != expected.identity:
                return False, "catalog binding physical identity differs from residual"
            if int(physical["birthtime_ns"]) != expected.birthtime_ns:
                return False, "catalog binding birthtime differs from residual"
            revision = binding["physical_anchor_revision"]
            if int(revision["size"]) != expected.size or int(revision["mtime_ns"]) != expected.mtime_ns:
                return False, "catalog binding revision differs from residual"
            connection.execute(
                """UPDATE documents SET path=?,resource_binding_json=?,updated_ns=?
                WHERE source_kind=? AND file_key=? AND active=1""",
                (
                    str(target),
                    json.dumps(rebound, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    time.time_ns(),
                    row["source_kind"],
                    row["file_key"],
                ),
            )
            if receipt is not None:
                curation_table = connection.execute(
                    """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='curator_decisions' LIMIT 1"""
                ).fetchone()
                if curation_table is not None:
                    decision_row = connection.execute(
                        """SELECT 1 FROM curator_decisions
                        WHERE source_kind=? AND file_key=? LIMIT 1""",
                        (row["source_kind"], row["file_key"]),
                    ).fetchone()
                    if decision_row is not None:
                        from .curation_state import rebind_curation_decision

                        rebind_curation_decision(
                            connection,
                            source_kind=str(row["source_kind"]),
                            file_key=str(row["file_key"]),
                            source_path=str(source),
                            target_path=str(target),
                            receipt=receipt,
                            current_source_binding=rebound,
                            updated_ns=time.time_ns(),
                        )
        return True, None
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, ResourceBindingError) as exc:
        return False, f"catalog rebind requires recovery: {type(exc).__name__}: {exc}"


def _cache_sync(
    state_directory: Path | None,
    prepared: _PreparedResidual,
    *,
    expected: FileSnapshot,
    target: Path,
    framework_lock_held: bool,
    checkpoint: Callable[[], None] | None,
    framework_connection: Any | None = None,
) -> DocumentCacheSyncResult | None:
    if state_directory is None:
        return None
    transition = DocumentMoveTransition(
        source_kind=prepared.survivor.source_kind,
        file_key=prepared.survivor.file_key,
        old_path=str(prepared.survivor.path),
        new_path=str(target),
        volume_id=str(expected.volume_id),
        file_id=str(expected.file_id),
    )
    return synchronize_moved_documents(
        state_directory,
        (transition,),
        framework_lock_held=framework_lock_held,
        work_check=checkpoint,
        existing_only=True,
        framework_connection=framework_connection,
    )


def _cache_sync_batch(
    state_directory: Path,
    prepared: Sequence[_PreparedResidual],
    *,
    framework_lock_held: bool,
    checkpoint: Callable[[], None] | None,
    framework_connection: Any | None,
) -> DocumentCacheSyncResult:
    transitions = tuple(
        DocumentMoveTransition(
            source_kind=item.survivor.source_kind,
            file_key=item.survivor.file_key,
            old_path=str(item.survivor.path),
            new_path=str(item.target),
            volume_id=str(item.expected.volume_id),
            file_id=str(item.expected.file_id),
        )
        for item in prepared
        if item.target is not None and item.expected is not None
    )
    if not transitions:
        return DocumentCacheSyncResult(True, 0, ())
    return synchronize_moved_documents(
        state_directory,
        transitions,
        framework_lock_held=framework_lock_held,
        work_check=checkpoint,
        existing_only=True,
        framework_connection=framework_connection,
    )


def _prepare_one(
    root: Path,
    survivor: ResidualSurvivor,
    decision_value: object | None,
    *,
    reserved: _DestinationReservation,
) -> _PreparedResidual:
    source = survivor.path
    if not source.is_relative_to(root):
        return _PreparedResidual(survivor, None, None, UNKNOWN_MIME, "path:outside-root", "blocked", "survivor escapes corpus root")
    if _is_in_ordered(root, source):
        return _PreparedResidual(survivor, None, None, UNKNOWN_MIME, "path:Corpus_ordenado", "skipped_classified", "already in Corpus_ordenado")
    _reject_symlink_prefix(source, root=root, allow_missing_tail=True)
    supplied = survivor.snapshot
    try:
        exists = source.is_file() and not source.is_symlink()
    except OSError:
        exists = False
    if exists:
        try:
            expected, mime, evidence = _resolve_mime(
                source,
                expected=supplied,
                decision_value=decision_value,
            )
        except _ResidualStale as exc:
            return _PreparedResidual(survivor, None, None, UNKNOWN_MIME, "identify:stale", "stale", str(exc))
        except (OSError, ValueError, RuntimeError) as exc:
            return _PreparedResidual(survivor, None, None, UNKNOWN_MIME, "identify:error", "blocked", str(exc))
    else:
        expected = supplied
        if expected is None:
            return _PreparedResidual(survivor, None, None, UNKNOWN_MIME, "source:missing", "stale", "source is missing")
        raw_mime = _valid_mime(_mapping_value(decision_value, "mime"))
        mime = raw_mime or UNKNOWN_MIME
        evidence = "identify:recovery-input" if raw_mime else "identify:unknown-recovery"
    target = _mime_target(root, mime, source.name)
    _reject_symlink_prefix(target, root=root, allow_missing_tail=True)
    if _same_path(source, target):
        return _PreparedResidual(survivor, expected, target, mime, evidence, "already_materialized", None)
    if not exists and _target_matches_expected(target, expected):
        return _PreparedResidual(survivor, expected, target, mime, evidence, "recovered", "physical move already present")
    if os.path.lexists(target) or reserved.contains(target):
        if expected is None:
            return _PreparedResidual(survivor, expected, None, mime, evidence, "blocked", "collision identity is unavailable")
        target = _collision_target(target, expected=expected, reserved=reserved)
        if not exists and _target_matches_expected(target, expected):
            return _PreparedResidual(
                survivor,
                expected,
                target,
                mime,
                evidence,
                "recovered",
                "physical collision-disambiguated move already present",
            )
    reserved.add(target)
    return _PreparedResidual(survivor, expected, target, mime, evidence, "planned", None)


def _apply_one(
    prepared: _PreparedResidual,
    config: ResidualMaterializationConfig,
    *,
    framework_state: object | None,
    synchronize_cache: bool = True,
) -> ResidualMoveResult:
    survivor = prepared.survivor
    source = survivor.path
    target = prepared.target
    if prepared.status == "skipped_classified":
        return ResidualMoveResult(str(source), None, prepared.mime, prepared.status, prepared.detail, survivor.source_kind, survivor.file_key)
    if prepared.status in {"stale", "blocked"} or target is None or prepared.expected is None:
        return ResidualMoveResult(str(source), None if target is None else str(target), prepared.mime, prepared.status, prepared.detail, survivor.source_kind, survivor.file_key)
    if prepared.status == "already_materialized":
        return ResidualMoveResult(
            str(source),
            str(target),
            prepared.mime,
            "already_materialized",
            prepared.detail,
            survivor.source_kind,
            survivor.file_key,
        )
    expected = prepared.expected
    if prepared.status == "recovered":
        action_id = _find_pending_framework_action(
            framework_state,
            run_id=config.run_id,
            source=source,
            target=target,
            mime=prepared.mime,
        )
        intent_error = None
    else:
        action_id, intent_error = _begin_framework_action(
            framework_state,
            run_id=config.run_id,
            source=source,
            target=target,
            mime=prepared.mime,
            evidence=prepared.evidence,
        )
    if intent_error is not None:
        return ResidualMoveResult(str(source), str(target), prepared.mime, "blocked", intent_error, survivor.source_kind, survivor.file_key, action_id=action_id)
    current_status = None if action_id is None else _framework_action_status(framework_state, action_id)
    receipt: str | None = None
    physical_status: _STATUS = prepared.status
    if prepared.status == "recovered":
        from .document_organization_application import _recover_organization_destination

        recovered = _recover_organization_destination(
            source,
            target,
            expected,
            receipt_json=None,
            effect_pending=True,
        )
        if recovered.status != "moved" or recovered.receipt_json is None:
            detail = recovered.detail or "residual destination recovery requires review"
            _finish_framework(framework_state, action_id, status="recovery_required", detail=detail)
            return ResidualMoveResult(
                str(source),
                str(target),
                prepared.mime,
                "recovery_required",
                detail,
                survivor.source_kind,
                survivor.file_key,
                action_id=action_id,
            )
        receipt = _residual_receipt_from_backend(recovered.receipt_json)
    elif prepared.status == "already_materialized":
        receipt = None
        physical_status = "already_materialized"
    else:
        if current_status == "recovery_required":
            return ResidualMoveResult(str(source), str(target), prepared.mime, "recovery_required", "framework action is already in recovery_required", survivor.source_kind, survivor.file_key, action_id=action_id)
        if current_status == "applied" and _target_matches_expected(target, expected) and not source.exists():
            from .document_organization_application import _recover_organization_destination

            recovered = _recover_organization_destination(
                source,
                target,
                expected,
                receipt_json=None,
                effect_pending=True,
            )
            if recovered.status != "moved" or recovered.receipt_json is None:
                detail = recovered.detail or "residual destination recovery requires review"
                return ResidualMoveResult(
                    str(source),
                    str(target),
                    prepared.mime,
                    "recovery_required",
                    detail,
                    survivor.source_kind,
                    survivor.file_key,
                    action_id=action_id,
                )
            physical_status = "recovered"
            receipt = _residual_receipt_from_backend(recovered.receipt_json)
        else:
            if config.mutation_guard is None or config.state_directory is None:
                return ResidualMoveResult(str(source), str(target), prepared.mime, "blocked", "residual apply boundary is unavailable", survivor.source_kind, survivor.file_key, action_id=action_id)
            try:
                config.mutation_guard.require_paths_allowed(source, target)
                root_stat = os.stat(config.corpus_root, follow_symlinks=False)
            except (OSError, RuntimeError, ValueError) as exc:
                _finish_framework(framework_state, action_id, status="skipped", detail=str(exc))
                return ResidualMoveResult(str(source), str(target), prepared.mime, "blocked", str(exc), survivor.source_kind, survivor.file_key, action_id=action_id)
            frontier_error = _mark_framework_frontier(
                framework_state,
                action_id,
                expected,
                source=source,
                target=target,
            )
            if frontier_error is not None:
                _finish_framework(framework_state, action_id, status="skipped", detail=frontier_error)
                return ResidualMoveResult(str(source), str(target), prepared.mime, "blocked", frontier_error, survivor.source_kind, survivor.file_key, action_id=action_id)
            try:
                from .document_organization_application import _move_organization_source

                outcome = _move_organization_source(
                    source,
                    target,
                    expected,
                    config.state_directory,
                    config.corpus_root,
                    root_stat,
                    config.mutation_guard,
                    backend_root=config.corpus_root,
                    owner_id=f"residual:{config.run_id}:{source}",
                )
            except BaseException as exc:
                _finish_framework(framework_state, action_id, status="recovery_required", detail=str(exc))
                raise
            if outcome.status == "blocked":
                detail = outcome.detail
                _finish_framework(framework_state, action_id, status="skipped", detail=detail)
                return ResidualMoveResult(str(source), str(target), prepared.mime, "blocked", detail, survivor.source_kind, survivor.file_key, action_id=action_id)
            if outcome.status == "recovery_required" or outcome.receipt_json is None:
                detail = outcome.detail or "residual move requires recovery"
                _finish_framework(framework_state, action_id, status="recovery_required", detail=detail)
                return ResidualMoveResult(str(source), str(target), prepared.mime, "recovery_required", detail, survivor.source_kind, survivor.file_key, action_id=action_id)
            try:
                receipt = _residual_receipt_from_backend(outcome.receipt_json)
            except ValueError as exc:
                _finish_framework(framework_state, action_id, status="recovery_required", detail=str(exc))
                return ResidualMoveResult(str(source), str(target), prepared.mime, "recovery_required", str(exc), survivor.source_kind, survivor.file_key, action_id=action_id)
            physical_status = "moved"
    receipt_error = _persist_physical_receipt(framework_state, action_id, receipt)
    if receipt_error is not None:
        _finish_framework(framework_state, action_id, status="recovery_required", detail=receipt_error)
        return ResidualMoveResult(
            str(source),
            str(target),
            prepared.mime,
            "recovery_required",
            receipt_error,
            survivor.source_kind,
            survivor.file_key,
            receipt_json=receipt,
            action_id=action_id,
        )
    if physical_status != "already_materialized" and (
        not _target_matches_expected(target, expected) or source.exists()
    ):
        detail = "residual destination identity changed before owner rebinding"
        _finish_framework(framework_state, action_id, status="recovery_required", detail=detail)
        return ResidualMoveResult(
            str(source),
            str(target),
            prepared.mime,
            "recovery_required",
            detail,
            survivor.source_kind,
            survivor.file_key,
            receipt_json=receipt,
            action_id=action_id,
        )
    if receipt is not None:
        try:
            _validate_residual_receipt(
                receipt,
                source=source,
                target=target,
                expected=expected,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            detail = f"residual receipt validation requires recovery: {exc}"
            _finish_framework(framework_state, action_id, status="recovery_required", detail=detail)
            return ResidualMoveResult(
                str(source),
                str(target),
                prepared.mime,
                "recovery_required",
                detail,
                survivor.source_kind,
                survivor.file_key,
                receipt_json=receipt,
                action_id=action_id,
            )
    catalog_ok, catalog_detail = _rebind_catalog_document(
        config.catalog_path,
        source=source,
        target=target,
        expected=expected,
        source_kind=survivor.source_kind,
        file_key=survivor.file_key,
        receipt=receipt,
    )
    if not catalog_ok:
        _finish_framework(framework_state, action_id, status="recovery_required", detail=catalog_detail)
        return ResidualMoveResult(str(source), str(target), prepared.mime, "recovery_required", catalog_detail, survivor.source_kind, survivor.file_key, receipt_json=receipt, action_id=action_id)
    if physical_status != "already_materialized" and (
        not _target_matches_expected(target, expected) or source.exists()
    ):
        detail = "residual destination identity changed before cache synchronization"
        _finish_framework(framework_state, action_id, status="recovery_required", detail=detail)
        return ResidualMoveResult(
            str(source),
            str(target),
            prepared.mime,
            "recovery_required",
            detail,
            survivor.source_kind,
            survivor.file_key,
            receipt_json=receipt,
            action_id=action_id,
            )
    if not synchronize_cache:
        return ResidualMoveResult(
            str(source),
            str(target),
            prepared.mime,
            physical_status,
            catalog_detail,
            survivor.source_kind,
            survivor.file_key,
            receipt_json=receipt,
            action_id=action_id,
        )
    cache_result = (
        None
        if physical_status == "already_materialized"
        else _cache_sync(
            config.state_directory,
            prepared,
            expected=expected,
            target=target,
            framework_lock_held=config.framework_lock_held,
            checkpoint=config.checkpoint,
            framework_connection=_framework_connection(framework_state),
        )
    )
    cache_json = None if cache_result is None else cache_result.as_json()
    if cache_result is not None and not cache_result.complete:
        detail = cache_result.error_message or "owner cache synchronization remains pending"
        _finish_framework(framework_state, action_id, status="recovery_required", detail=detail)
        return ResidualMoveResult(str(source), str(target), prepared.mime, "moved_cache_pending", detail, survivor.source_kind, survivor.file_key, receipt_json=receipt, cache_sync_json=cache_json, action_id=action_id)
    action_error = _finish_framework(
        framework_state,
        action_id,
        status=(
            "skipped"
            if current_status == "started"
            and physical_status in {"recovered", "already_materialized"}
            else "applied"
        ),
        detail=(
            "physical residual move was already present; no new filesystem effect"
            if current_status == "started"
            and physical_status in {"recovered", "already_materialized"}
            else None
        ),
        receipt=(
            None
            if current_status == "started"
            and physical_status in {"recovered", "already_materialized"}
            else receipt or _residual_receipt(source, target, expected)
        ),
    )
    if action_error is not None:
        return ResidualMoveResult(str(source), str(target), prepared.mime, "recovery_required", action_error, survivor.source_kind, survivor.file_key, receipt_json=receipt, cache_sync_json=cache_json, action_id=action_id)
    final_status: _STATUS = physical_status
    return ResidualMoveResult(str(source), str(target), prepared.mime, final_status, catalog_detail, survivor.source_kind, survivor.file_key, receipt_json=receipt, cache_sync_json=cache_json, action_id=action_id)


class ResidualMaterializer:
    """Plan/apply one residual MIME materialization boundary."""

    def __init__(self, config: ResidualMaterializationConfig):
        self.config = config

    def materialize(
        self,
        survivors: Iterable[object] | None = None,
        decisions: object | None = None,
    ) -> ResidualMaterializationResult:
        return _materialize(self.config, survivors=survivors, decisions=decisions)

    def plan(
        self,
        survivors: Iterable[object] | None = None,
        decisions: object | None = None,
    ) -> ResidualMaterializationResult:
        config = replace(self.config, apply=False, preview=True)
        return _materialize(config, survivors=survivors, decisions=decisions)

    def apply(
        self,
        survivors: Iterable[object] | None = None,
        decisions: object | None = None,
    ) -> ResidualMaterializationResult:
        config = replace(self.config, apply=True, preview=False)
        return _materialize(config, survivors=survivors, decisions=decisions)


def _materialize(
    config: ResidualMaterializationConfig,
    *,
    survivors: Iterable[object] | None,
    decisions: object | None,
) -> ResidualMaterializationResult:
    root = config.corpus_root
    if not root.is_dir() or root.is_symlink():
        raise ValueError("corpus_root must be an existing real directory")
    _reject_symlink_prefix(root, root=root, allow_missing_tail=False)
    state_directory = config.state_directory
    framework_path = getattr(config.framework_state, "path", None)
    if state_directory is None and framework_path is not None:
        state_directory = Path(os.path.abspath(os.fspath(framework_path))).parent
    if framework_path is not None and state_directory is not None:
        expected_state = Path(os.path.abspath(os.fspath(framework_path))).parent
        if not _same_path(expected_state, state_directory):
            raise ValueError("framework_state and state_directory refer to different owners")
    if state_directory is not None and path_trees_intersect(root, state_directory):
        raise ValueError("residual corpus and state directory must be disjoint")
    if state_directory is not config.state_directory:
        config = replace(config, state_directory=state_directory)
    apply = config.apply and not config.preview
    mutation_guard = config.mutation_guard
    if apply:
        missing_boundary = []
        if mutation_guard is None:
            missing_boundary.append("mutation_guard")
        if config.framework_state is None:
            missing_boundary.append("framework_state")
        if config.run_id is None:
            missing_boundary.append("run_id")
        if state_directory is None or not state_directory.is_dir():
            missing_boundary.append("state_directory")
        if missing_boundary:
            return ResidualMaterializationResult(
                preview=False,
                blocked=1,
                error="residual apply boundary is unavailable: " + ", ".join(missing_boundary),
            )
        try:
            publication_view = read_state_publication_state(state_directory)
        except StatePublicationError as exc:
            return ResidualMaterializationResult(
                preview=False,
                recovery_required=1,
                error=f"residual apply blocked by state publication gate: {exc}",
            )
        if publication_view.status not in {"absent", "complete"}:
            return ResidualMaterializationResult(
                preview=False,
                recovery_required=1,
                error=(
                    "residual apply blocked by unresolved state publication: "
                    f"{publication_view.status}: {publication_view.reason or 'unknown'}"
                ),
            )
        try:
            root.relative_to(mutation_guard.policy.root)
        except ValueError as exc:
            raise ValueError("corpus_root is outside the mutation guard root") from exc
        mutation_guard.reject_run_mutation()
        config = replace(config, mutation_guard=mutation_guard, state_directory=state_directory)
    if survivors is None:
        source_values: Iterable[object] = _scan_residual_files(root)
    elif isinstance(survivors, Mapping) and not (
        "path" in survivors or "source_path" in survivors
    ):
        # Accept the common inventory shape ``{path: FileSnapshot}`` without
        # treating a mapping key as an owner or inventing a catalog identity.
        source_values = (
            {
                "path": path,
                "snapshot": value,
            }
            for path, value in survivors.items()
        )
    else:
        source_values = survivors
    decision_index = _decision_index(decisions)
    reservation = _DestinationReservation(
        _framework_connection(config.framework_state) if apply else None,
        run_id=config.run_id,
    )
    lock = (
        nullcontext()
        if not apply or config.framework_lock_held or config.state_directory is None
        else FrameworkRunLock(config.state_directory / "framework.lock")
    )
    cache_config = (
        replace(config, framework_lock_held=True)
        if apply and config.state_directory is not None
        else config
    )
    counts: Counter[str] = Counter()
    buckets: Counter[str] = Counter()
    sample: list[ResidualMoveResult] = []
    selected = 0
    cache_synced = 0

    def record(result: ResidualMoveResult) -> None:
        nonlocal selected, cache_synced
        selected += 1
        counts[result.status] += 1
        if result.status in {
            "planned",
            "moved",
            "recovered",
            "already_materialized",
            "moved_cache_pending",
        }:
            buckets[result.mime] += 1
        if result.cache_sync_json is not None and '"complete":true' in result.cache_sync_json:
            cache_synced += 1
        if len(sample) < 32:
            sample.append(result)

    pending_batch: list[tuple[_PreparedResidual, ResidualMoveResult]] = []

    def finalize_batch() -> None:
        if not pending_batch:
            return
        prepared_batch = [item for item, _result in pending_batch]
        sync = _cache_sync_batch(
            cache_config.state_directory,
            prepared_batch,
            framework_lock_held=cache_config.framework_lock_held,
            checkpoint=cache_config.checkpoint,
            framework_connection=_framework_connection(cache_config.framework_state),
        )
        sync_json = sync.as_json()
        for _prepared_item, result in pending_batch:
            if sync.complete:
                current = (
                    _framework_action_status(cache_config.framework_state, result.action_id)
                    if result.action_id is not None
                    else None
                )
                skipped = current == "started" and result.status == "recovered"
                action_error = _finish_framework(
                    cache_config.framework_state,
                    result.action_id,
                    status="skipped" if skipped else "applied",
                    detail=(
                        "physical residual move was already present; no new filesystem effect"
                        if skipped
                        else None
                    ),
                    receipt=None if skipped else result.receipt_json,
                )
                final = replace(
                    result,
                    status="recovery_required" if action_error is not None else result.status,
                    detail=action_error or result.detail,
                    cache_sync_json=sync_json,
                )
            else:
                detail = sync.error_message or "owner cache synchronization remains pending"
                _finish_framework(
                    cache_config.framework_state,
                    result.action_id,
                    status="recovery_required",
                    detail=detail,
                )
                final = replace(
                    result,
                    status="moved_cache_pending",
                    detail=detail,
                    cache_sync_json=sync_json,
                )
            record(final)
        pending_batch.clear()

    with _reservation_scope(reservation, lock):
        processed = 0
        for value in source_values:
            if config.max_actions is not None and processed >= config.max_actions:
                break
            survivor = _coerce_survivor(value)
            processed += 1
            item = _prepare_one(
                root,
                survivor,
                _decision_for(decisions, decision_index, survivor),
                reserved=reservation,
            )
            if apply:
                result = _apply_one(
                    item,
                    cache_config,
                    framework_state=config.framework_state,
                    synchronize_cache=False,
                )
                if result.status in {"moved", "recovered"} and item.expected is not None:
                    pending_batch.append((item, result))
                    if len(pending_batch) >= 256:
                        finalize_batch()
                else:
                    if result.status == "already_materialized":
                        _finish_framework(
                            cache_config.framework_state,
                            result.action_id,
                            status="skipped",
                            detail="physical residual move was already present; no new filesystem effect",
                        )
                    record(result)
            else:
                record(
                    ResidualMoveResult(
                        str(item.survivor.path),
                        None if item.target is None else str(item.target),
                        item.mime,
                        "planned" if item.status == "planned" else item.status,
                        item.detail,
                        item.survivor.source_kind,
                        item.survivor.file_key,
                    )
                )
        finalize_batch()
    cache_pending = counts["moved_cache_pending"]
    return ResidualMaterializationResult(
        preview=not apply,
        selected=selected,
        planned=counts["planned"],
        moved=counts["moved"],
        recovered=counts["recovered"],
        already_materialized=counts["already_materialized"],
        stale=counts["stale"],
        blocked=counts["blocked"],
        failed=counts["failed"],
        recovery_required=counts["recovery_required"],
        cache_synced=cache_synced,
        cache_pending=cache_pending,
        unclassified=sum(
            counts[status]
            for status in ("planned", "moved", "recovered", "already_materialized", "moved_cache_pending")
        ),
        mime_buckets=tuple(sorted(buckets.items())),
        moves=tuple(sample),
    )


def materialize_residual_documents(
    corpus_root: Path,
    survivors: Iterable[object] | None = None,
    decisions: object | None = None,
    *,
    config: ResidualMaterializationConfig | None = None,
    mutation_guard: CorpusMutationGuard | None = None,
    state_directory: Path | None = None,
    catalog_path: Path | None = None,
    framework_state: object | None = None,
    run_id: int | None = None,
    apply: bool = False,
    preview: bool | None = None,
    framework_lock_held: bool = False,
    max_actions: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> ResidualMaterializationResult:
    """Plan or apply residual MIME moves.

    ``preview`` defaults to the inverse of ``apply``.  The function accepts
    plain paths, mappings, ``FileSnapshot``-bearing records, or the typed
    dataclasses above so an upstream pipeline can pass its existing survivor
    and Identify iterables without fabricating a catalog owner.
    """

    if config is not None:
        if any(
            value is not None
            for value in (
                mutation_guard,
                state_directory,
                catalog_path,
                framework_state,
                run_id,
                max_actions,
                checkpoint,
            )
        ) or apply or preview is not None or framework_lock_held:
            raise ValueError("config cannot be combined with execution overrides")
        selected = config
    else:
        selected_preview = (not apply) if preview is None else preview
        if type(selected_preview) is not bool:
            raise TypeError("preview must be boolean")
        selected = ResidualMaterializationConfig(
            corpus_root=Path(corpus_root),
            apply=apply,
            preview=selected_preview,
            state_directory=state_directory,
            catalog_path=catalog_path,
            framework_state=framework_state,
            run_id=run_id,
            mutation_guard=mutation_guard,
            framework_lock_held=framework_lock_held,
            max_actions=max_actions,
            checkpoint=checkpoint,
        )
    return ResidualMaterializer(selected).materialize(survivors, decisions)


__all__ = (
    "CORPUS_ORDERED_DIRECTORY",
    "MIME_DIRECTORY",
    "RESIDUAL_MATERIALIZATION_SCHEMA",
    "UNCLASSIFIED_DIRECTORY",
    "UNKNOWN_MIME",
    "ResidualMaterializationConfig",
    "ResidualMaterializationResult",
    "ResidualMaterializer",
    "ResidualMimeDecision",
    "ResidualMoveResult",
    "ResidualSurvivor",
    "materialize_residual_documents",
)
