# ruff: noqa: F401
"""Private, manifest-backed registry for local runtime artifacts.

The registry is deliberately smaller than a general filesystem inventory.  A
path is managed only when an authenticated manifest in the configured registry
root claims it; names that merely happen to be nearby are reported as
unmanaged and are never adopted.  ``plan`` and ``verify`` are read-only.  This
module does not remove, move, copy, or otherwise modify an artifact path.

Manifests contain bounded metadata and physical identity observations, not
artifact contents.  Every write uses a temporary file followed by an atomic
publication and a directory fsync.  The final manifest, registry root, and
claimed artifact are revalidated without following symlinks before a record is
considered eligible.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any

from .artifact_models import (
    ARTIFACT_KINDS, ARTIFACT_REGISTRY_SCHEMA, ARTIFACT_STATES, MANIFEST_SUFFIX,
    MAX_ARTIFACT_ID_BYTES, MAX_DEPENDENCIES, MAX_DEPENDENCY_BYTES, MAX_MANIFEST_BYTES,
    MAX_METADATA_BYTES, MAX_REASON_BYTES, MAX_RECORDS, MAX_RUN_ID_BYTES, MAX_SCAN_BYTES,
    MAX_TEXT_BYTES, ArtifactConflictError, ArtifactKind, ArtifactManifestError,
    ArtifactRecord, ArtifactPlan, ArtifactRegistryError, ArtifactRootError,
    ArtifactSecurityError, ArtifactState, _BLOCKING_ISSUES, _DEFAULT_OWNER,
    _DEFAULT_PRODUCER, _DEFAULT_PURPOSE, _DISPOSABLE_KINDS, _LIVE_DEPENDENT_STATES,
    _RETIREMENT_CONFIRMED_PHASE, _RETIREMENT_KEY, _RETIREMENT_PENDING_PHASES,
    _SAFE_ID, _TOMBSTONE_RETENTION_DIR, _TOMBSTONE_RETENTION_SCHEMA, _UNSET,
    _artifact_observation_issue, _bounded_identity, _bounded_json, _bounded_mapping,
    _bounded_run_id, _bounded_text, _canonical_json, _directory_size_no_follow,
    _identity, _lstat, _manifest_digest, _path_is_within, _path_size_no_follow,
    _private_artifact_issue, _private_directory_issue, _safe_manifest_name,
    _same_identity, _validate_absolute_path, _validate_limit,
)
from .artifact_registration import (
    register as _register_impl,
    update as _update_impl,
)
from .artifact_manifest import (
    invalid_record as _invalid_record_impl,
    load_record as _load_record_impl,
    payload_from_record as _payload_from_record_impl,
    read_manifest_payload as _read_manifest_payload_impl,
    record_from_payload as _record_from_payload_impl,
    revalidate_record as _revalidate_record_impl,
    registration_equal as _registration_equal_impl,
)
from .artifact_storage import (
    _manifest_file_issue, _retention_receipt_digest, _retention_receipt_name,
    _write_json_atomic,
)



class _RetirementBatch:
    """Registry-lock-scoped batch used by ScratchManager.apply().

    Keeping the directory lock for one bounded batch lets every target reuse
    the same dependency observation.  Each target is still reloaded and
    revalidated immediately before its own effect; the batch is an
    optimization of discovery, not an authorization cache.
    """

    def __init__(
        self,
        registry: "ArtifactRegistry",
        records: tuple[ArtifactRecord, ...],
        unmanaged: tuple[Path, ...],
        truncated: bool,
    ) -> None:
        self.registry = registry
        self.records = records
        self.unmanaged = unmanaged
        self.truncated = truncated

    @contextmanager
    def guard(self, artifact: str | ArtifactRecord) -> Iterator[ArtifactRecord]:
        yield from self.registry._retirement_guard_locked(
            artifact,
            records=self.records,
            unmanaged=self.unmanaged,
            truncated=self.truncated,
        )


# endregion [02]


# region [03] Atomic storage and registry


_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "artifact_id",
        "owner",
        "producer",
        "run_id",
        "purpose",
        "path",
        "root",
        "path_identity",
        "root_identity",
        "kind",
        "state",
        "created_ns",
        "updated_ns",
        "source_ref",
        "digest",
        "dependencies",
        "retain_until_ns",
        "ttl_ns",
        "disposable",
        "metadata",
        "path_size_bytes",
        "path_mtime_ns",
        "manifest_digest",
    }
)


def _registry_write_locked(method: Any) -> Any:
    """Serialize manifest reads+writes across registry processes."""

    @wraps(method)
    def wrapped(self: "ArtifactRegistry", *args: Any, **kwargs: Any) -> Any:
        # Registration historically creates its configured root on demand;
        # updates must remain read-only with respect to an absent root.
        self._ensure_root(create=method.__name__ == "register")
        with self._registry_lock():
            return method(self, *args, **kwargs)

    return wrapped


class ArtifactRegistry:
    """Own a private manifest root and revalidate its registered artifacts."""

    def __init__(
        self,
        root: Path | str,
        *,
        owner: str | None = _DEFAULT_OWNER,
        create_root: bool = False,
        max_records: int = MAX_RECORDS,
        max_entries: int | None = None,
        max_bytes: int = MAX_SCAN_BYTES,
        max_manifest_bytes: int = MAX_MANIFEST_BYTES,
        max_metadata_bytes: int = MAX_METADATA_BYTES,
    ) -> None:
        self.root = _validate_absolute_path(root, label="artifact registry root")
        # ``owner=None`` is an explicitly read-only federated view used by
        # hygiene orchestration to inspect manifests written by multiple
        # producer owners in one registry root.  Registration/update always
        # require an exact owner and therefore cannot accidentally use this
        # view as an authority.
        self.owner = (
            None
            if owner is None
            else _bounded_text(owner, label="artifact registry owner", limit=MAX_TEXT_BYTES)
        )
        self.create_root = create_root
        if type(create_root) is not bool:
            raise TypeError("artifact create_root must be a boolean")
        if max_entries is not None:
            max_records = max_entries
        self.max_records = _validate_limit(max_records, label="artifact max_records")
        self.max_bytes = _validate_limit(max_bytes, label="artifact max_bytes")
        self.max_manifest_bytes = _validate_limit(
            max_manifest_bytes,
            label="artifact max_manifest_bytes",
        )
        self.max_metadata_bytes = _validate_limit(
            max_metadata_bytes,
            label="artifact max_metadata_bytes",
        )
        self._lock_local = threading.local()
        if create_root:
            self._ensure_root(create=True)

    # -- root and manifest primitives ---------------------------------

    def _ensure_root(self, *, create: bool) -> bool:
        try:
            metadata = self.root.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
            except FileExistsError:
                pass
            try:
                metadata = self.root.lstat()
            except OSError as exc:
                raise ArtifactRootError("artifact registry root could not be inspected") from exc
        except OSError as exc:
            raise ArtifactRootError("artifact registry root could not be inspected") from exc
        issue = _private_directory_issue(metadata, root=True)
        if issue is not None:
            raise ArtifactRootError(f"artifact registry root failed safety check: {issue}")
        return True

    @contextmanager
    def _registry_lock(self) -> Iterator[None]:
        """Hold an OS lock on the registry directory without creating files."""

        active_fd = getattr(self._lock_local, "fd", None)
        if active_fd is not None:
            # Nested calls from ScratchManager's retirement guard use the same
            # registry object.  Reusing the descriptor avoids a second flock
            # while preserving the outer process-wide critical section.
            yield
            return
        try:
            import fcntl

            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ArtifactRootError("artifact registry root could not be locked") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._lock_local.fd = fd
            yield
        finally:
            self._lock_local.fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _manifest_path(self, artifact_id: str) -> Path:
        return self.root / _safe_manifest_name(artifact_id)

    def manifest_path(self, artifact_id: str) -> Path:
        """Return the deterministic manifest path for an artifact id."""

        artifact_id = _bounded_text(
            artifact_id,
            label="artifact_id",
            limit=MAX_ARTIFACT_ID_BYTES,
        )
        return self._manifest_path(artifact_id)

    def tombstone_retention_receipt_path(self, operation_id: str) -> Path:
        """Locate an owner's existing terminal-retention receipt without IO."""
        operation = _bounded_text(operation_id, label="tombstone retention operation", limit=128)
        return self.root / _TOMBSTONE_RETENTION_DIR / _retention_receipt_name(operation)

    @staticmethod
    def _normalize_dependencies(value: Iterable[str] | None) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise TypeError("artifact dependencies must be an iterable of strings")
        values: list[str] = []
        for dependency in value:
            normalized = _bounded_text(
                dependency,
                label="artifact dependency",
                limit=MAX_DEPENDENCY_BYTES,
            )
            values.append(normalized)
            if len(values) > MAX_DEPENDENCIES:
                raise ValueError("artifact dependencies exceed the durable limit")
        return tuple(sorted(set(values)))

    @staticmethod
    def _retirement_claim(record: ArtifactRecord) -> dict[str, Any] | None:
        """Return the reserved retirement claim, if one is well formed.

        The claim lives in the owner-controlled manifest metadata.  A
        malformed claim is not treated as absent: callers that need a safety
        decision must abstain instead of silently reverting to the ordinary
        completed/disposable policy.
        """

        value = record.metadata.get(_RETIREMENT_KEY)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ArtifactSecurityError("retirement claim is not an object")
        phase = value.get("phase")
        operation_id = value.get("operation_id")
        if phase not in _RETIREMENT_PENDING_PHASES | {_RETIREMENT_CONFIRMED_PHASE}:
            raise ArtifactSecurityError("retirement claim has an unsupported phase")
        if not isinstance(operation_id, str) or not operation_id:
            raise ArtifactSecurityError("retirement claim has no operation id")
        return dict(value)

    def _metadata_with_retirement(
        self,
        record: ArtifactRecord,
        *,
        phase: str,
        operation_id: str,
        **observations: Any,
    ) -> dict[str, Any]:
        """Build bounded metadata for one durable retirement transition."""

        if phase not in _RETIREMENT_PENDING_PHASES | {_RETIREMENT_CONFIRMED_PHASE}:
            raise ValueError("unsupported retirement phase")
        metadata = dict(record.metadata)
        existing = metadata.get(_RETIREMENT_KEY)
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise ArtifactSecurityError("retirement claim is not an object")
            previous_id = existing.get("operation_id")
            if previous_id != operation_id:
                raise ArtifactSecurityError("a different retirement operation is pending")
            claim = dict(existing)
        else:
            claim = {
                "operation_id": operation_id,
                "expected_path_identity": list(record.path_identity or ()),
                "expected_size_bytes": record.path_size_bytes,
                "expected_mtime_ns": record.path_mtime_ns,
                "prepared_ns": time.time_ns(),
            }
        claim["phase"] = phase
        claim.update(observations)
        metadata[_RETIREMENT_KEY] = claim
        # Use the same bounded metadata validator as ordinary registration;
        # it prevents a producer from turning the recovery receipt into an
        # unbounded side channel.
        return _bounded_mapping(
            metadata,
            label="artifact metadata",
            limit=self.max_metadata_bytes,
        )

    def _dependency_observation_complete(
        self,
        records: Sequence[ArtifactRecord],
        unmanaged: Sequence[Path] = (),
        *,
        truncated: bool = False,
    ) -> bool:
        """Whether the registry view can prove absence of live consumers.

        An invalid manifest, an unmanaged registry entry, or a bounded scan is
        an incomplete view of the owner-controlled dependency universe.  It is
        safer to preserve a candidate than to interpret a lost dependency list
        as an empty one.  This is intentionally scoped to this registry root,
        not to every filesystem path on the machine.
        """

        return not truncated and not unmanaged and all(
            record.valid or self.retired_claim_is_historical(record, records)
            for record in records
        )

    @staticmethod
    def retired_claim_is_historical(record: ArtifactRecord,
                                    records: Sequence[ArtifactRecord]) -> bool:
        """Recognize a confirmed old tombstone superseded by a verified claim.

        This only completes dependency observation. It cannot authorize an
        effect against the old identity or the new occupant of the path.
        """
        if record.state != ArtifactState.RETIRED.value or record.issue != "artifact_identity_drift":
            return False
        try:
            claim = ArtifactRegistry._retirement_claim(record)
        except ArtifactSecurityError:
            return False
        if (claim is None or claim.get("phase") != _RETIREMENT_CONFIRMED_PHASE
                or claim.get("observed_path_exists") is not False
                or claim.get("expected_path_identity") != list(record.path_identity or ())):
            return False
        return any(other.artifact_id != record.artifact_id and other.verified
                   and other.state != ArtifactState.RETIRED.value and other.path == record.path
                   and other.path_identity is not None and other.path_identity != record.path_identity
                   for other in records)

    def _validate_dependencies_locked(
        self,
        dependencies: Sequence[str],
        *,
        artifact_id: str,
    ) -> None:
        """Validate live dependency acquisitions while holding the registry lock.

        Dependency names in this owner registry are live artifact claims, not
        free-form provenance.  Requiring a verified, non-retired target closes
        the race where a consumer is registered after its input has already
        been physically retired.  Producers that need historical provenance
        should put it in ``source_ref``/``metadata`` instead.
        """

        for dependency in dependencies:
            if dependency == artifact_id:
                raise ArtifactSecurityError("artifact cannot depend on itself")
            try:
                target = self._load_record(self._manifest_path(dependency))
            except (ArtifactRegistryError, OSError, TypeError, ValueError) as exc:
                raise ArtifactSecurityError(
                    f"dependency target is unavailable: {dependency}"
                ) from exc
            if not target.verified:
                raise ArtifactSecurityError(
                    f"dependency target is not verified: {dependency}"
                )
            claim = self._retirement_claim(target)
            if target.state == ArtifactState.RETIRED.value or (
                claim is not None and claim.get("phase") in _RETIREMENT_PENDING_PHASES
            ):
                raise ArtifactSecurityError(
                    f"dependency target is no longer usable: {dependency}"
                )

    def _payload_from_record(self, record: ArtifactRecord) -> dict[str, Any]:
        return _payload_from_record_impl(self, record)

    def _read_manifest_payload(self, manifest_path: Path) -> dict[str, Any]:
        return _read_manifest_payload_impl(self, manifest_path)

    def _record_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        manifest_path: Path,
        revalidate: bool = True,
    ) -> ArtifactRecord:
        return _record_from_payload_impl(
            self, payload, manifest_path=manifest_path, revalidate=revalidate
        )

    def _revalidate_record(self, record: ArtifactRecord) -> ArtifactRecord:
        return _revalidate_record_impl(self, record)

    def _invalid_record(self, manifest_path: Path, issue: str) -> ArtifactRecord:
        return _invalid_record_impl(self, manifest_path, issue)

    def _load_record(self, manifest_path: Path) -> ArtifactRecord:
        return _load_record_impl(self, manifest_path)

    # -- registration/update ------------------------------------------

    @_registry_write_locked
    def register(
        self,
        artifact_id: str | ArtifactRecord,
        producer: str | None = None,
        path: Path | str | None = None,
        *,
        owner: str | None = None,
        run_id: int | str | None = None,
        purpose: str | None = None,
        root: Path | str | None = None,
        kind: str = ArtifactKind.TEMPORARY.value,
        state: str = ArtifactState.ACTIVE.value,
        source_ref: Any = None,
        digest: str | None = None,
        dependencies: Iterable[str] | None = None,
        retain_until_ns: int | None = None,
        ttl_ns: int | None = None,
        ttl: int | None = None,
        disposable: bool = True,
        metadata: Mapping[str, Any] | None = None,
        created_ns: int | None = None,
        updated_ns: int | None = None,
        path_identity: Sequence[int] | None = None,
        identity: Sequence[int] | None = None,
        root_identity: Sequence[int] | None = None,
        lifecycle_state: str | None = None,
        retain_on_success: bool | None = None,
        retention: Mapping[str, Any] | None = None,
        manifest_digest: str | None = None,
    ) -> ArtifactRecord:
        return _register_impl(self, artifact_id=artifact_id, producer=producer, path=path, owner=owner, run_id=run_id, purpose=purpose, root=root, kind=kind, state=state, source_ref=source_ref, digest=digest, dependencies=dependencies, retain_until_ns=retain_until_ns, ttl_ns=ttl_ns, ttl=ttl, disposable=disposable, metadata=metadata, created_ns=created_ns, updated_ns=updated_ns, path_identity=path_identity, identity=identity, root_identity=root_identity, lifecycle_state=lifecycle_state, retain_on_success=retain_on_success, retention=retention, manifest_digest=manifest_digest)

    def _write_registration(self, manifest_path: Path, payload: Mapping[str, Any]) -> None:
        if len(_canonical_json(payload).encode("utf-8")) > self.max_manifest_bytes:
            raise ValueError("artifact manifest exceeds the configured size limit")
        _write_json_atomic(manifest_path, payload, exclusive=True)
        metadata = manifest_path.lstat()
        issue = _manifest_file_issue(metadata)
        if issue is not None:
            raise ArtifactManifestError(f"published manifest failed safety check: {issue}")

    def _write_existing_registration(
        self,
        manifest_path: Path,
        payload: Mapping[str, Any],
    ) -> None:
        """Atomically replace an already-owned manifest."""

        if len(_canonical_json(payload).encode("utf-8")) > self.max_manifest_bytes:
            raise ValueError("artifact manifest exceeds the configured size limit")
        _write_json_atomic(manifest_path, payload, exclusive=False)
        metadata = manifest_path.lstat()
        issue = _manifest_file_issue(metadata)
        if issue is not None:
            raise ArtifactManifestError(f"published manifest failed safety check: {issue}")

    @staticmethod
    def _registration_equal(existing: ArtifactRecord, candidate: ArtifactRecord) -> bool:
        return _registration_equal_impl(existing, candidate)

    @_registry_write_locked
    def update(
        self,
        artifact: str | ArtifactRecord,
        *,
        producer: str | object = _UNSET,
        run_id: int | str | object | None = _UNSET,
        purpose: str | object = _UNSET,
        state: str | object = _UNSET,
        source_ref: Any = _UNSET,
        digest: str | object | None = _UNSET,
        dependencies: Iterable[str] | object | None = _UNSET,
        retain_until_ns: int | object | None = _UNSET,
        ttl_ns: int | object | None = _UNSET,
        ttl: int | object | None = _UNSET,
        disposable: bool | object = _UNSET,
        metadata: Mapping[str, Any] | object | None = _UNSET,
        updated_ns: int | None = None,
    ) -> ArtifactRecord:
        return _update_impl(self, artifact=artifact, producer=producer, run_id=run_id, purpose=purpose, state=state, source_ref=source_ref, digest=digest, dependencies=dependencies, retain_until_ns=retain_until_ns, ttl_ns=ttl_ns, ttl=ttl, disposable=disposable, metadata=metadata, updated_ns=updated_ns)

    # -- read-only verification and planning --------------------------

    def _scan_entries(
        self,
        *,
        max_records: int,
    ) -> tuple[tuple[os.DirEntry[str], ...], bool, tuple[str, ...]]:
        try:
            iterator = os.scandir(self.root)
        except OSError as exc:
            raise ArtifactRootError("artifact registry root could not be scanned") from exc
        entries: list[os.DirEntry[str]] = []
        truncated = False
        try:
            for entry in iterator:
                if entry.name == _TOMBSTONE_RETENTION_DIR:
                    try:
                        internal = entry.stat(follow_symlinks=False)
                    except OSError:
                        # Keep the entry visible as unmanaged so a concurrent
                        # or corrupt retention store fails closed.
                        entries.append(entry)
                        continue
                    if (
                        stat.S_ISDIR(internal.st_mode)
                        and not stat.S_ISLNK(internal.st_mode)
                        and internal.st_uid == os.geteuid()
                        and not internal.st_mode & 0o077
                    ):
                        continue
                if len(entries) >= max_records:
                    truncated = True
                    break
                entries.append(entry)
        finally:
            iterator.close()
        return tuple(sorted(entries, key=lambda item: item.name)), truncated, (
            "record_limit",
        ) if truncated else ()

    def _scan_records(
        self,
        *,
        max_records: int,
    ) -> tuple[tuple[ArtifactRecord, ...], tuple[Path, ...], bool, tuple[str, ...]]:
        entries, truncated, truncation_reasons = self._scan_entries(max_records=max_records)
        records: list[ArtifactRecord] = []
        unmanaged: list[Path] = []
        for entry in entries:
            path = self.root / entry.name
            if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                # A crashed writer's temporary is not a managed artifact.  It
                # is still surfaced as unmanaged below if the caller wants the
                # full neighbor list; never parse it as a manifest.
                unmanaged.append(path)
                continue
            if not entry.name.endswith(MANIFEST_SUFFIX):
                unmanaged.append(path)
                continue
            try:
                records.append(self._load_record(path))
            except (ArtifactRegistryError, OSError, TypeError, ValueError) as exc:
                records.append(self._invalid_record(path, f"manifest_invalid:{type(exc).__name__}"))
        if truncated:
            truncation_reasons = tuple(dict.fromkeys((*truncation_reasons, "scan_bounded")))
        return tuple(records), tuple(unmanaged), truncated, truncation_reasons

    @staticmethod
    def _classify(record: ArtifactRecord, *, now_ns: int) -> tuple[str, str]:
        if not record.valid and record.issue in _BLOCKING_ISSUES:
            return "blocked", record.issue
        if not record.verified:
            return "unknown", record.issue or "manifest_invalid"
        # A durable retirement intent is a recovery boundary.  Until its
        # receipt is confirmed the artifact is never eligible, even if the
        # ordinary state/kind/retention fields would otherwise qualify it.
        try:
            retirement = ArtifactRegistry._retirement_claim(record)
        except ArtifactSecurityError:
            return "blocked", "retirement_claim_invalid"
        if retirement is not None and retirement.get("phase") in _RETIREMENT_PENDING_PHASES:
            return "blocked", "retirement_recovery_required"
        if record.updated_ns > now_ns:
            return "protected", "future_timestamp"
        if record.state == ArtifactState.ACTIVE.value:
            return "protected", "state_active"
        if record.state == ArtifactState.FAILED.value:
            return "protected", "state_failed"
        if record.state == ArtifactState.RECOVERY_REQUIRED.value:
            return "protected", "state_recovery_required"
        if record.state == ArtifactState.RETIRED.value:
            return "protected", "state_retired"
        if record.state != ArtifactState.COMPLETED.value:
            return "unknown", "state_unknown"
        if not record.disposable:
            return "protected", "not_disposable"
        if record.kind not in _DISPOSABLE_KINDS:
            return "protected", "kind_protected"
        deadline = record.effective_retain_until_ns()
        if deadline is not None and now_ns < deadline:
            return "protected", "retention_active"
        if deadline is not None and now_ns < 0:  # pragma: no cover - defensive
            return "protected", "retention_active"
        return "eligible", "retention_expired" if deadline is not None else "disposable_completed"

    @staticmethod
    def _live_dependents(
        target: ArtifactRecord,
        records: Sequence[ArtifactRecord],
    ) -> tuple[str, ...]:
        """Return active/recoverable claims that still reference ``target``."""

        dependents: list[str] = []
        for record in records:
            if record.artifact_id == target.artifact_id:
                continue
            if target.artifact_id not in record.dependencies:
                continue
            # An invalid or incomplete observation is conservative: its
            # dependency claim remains live until an owner explicitly repairs
            # or releases it.
            if not record.valid or record.state in _LIVE_DEPENDENT_STATES:
                dependents.append(record.artifact_id)
        return tuple(sorted(set(dependents)))

    def verify(
        self,
        target: str | Path | ArtifactRecord | None = None,
        *,
        now_ns: int | None = None,
        raise_on_error: bool = False,
    ) -> ArtifactRecord | tuple[ArtifactRecord, ...]:
        """Re-read and revalidate one record, or all records when target is null.

        The single-record return is truthy only when both the manifest and the
        no-follow root/artifact identity are valid.  No call creates a root or
        changes any filesystem entry.
        """

        del now_ns  # verification is identity-only; retention belongs to plan
        if target is None:
            self._ensure_root(create=False)
            records, _, _, _ = self._scan_records(max_records=self.max_records)
            return records
        if isinstance(target, ArtifactRecord):
            artifact_id = target.artifact_id
        elif isinstance(target, Path):
            candidate = _validate_absolute_path(target, label="artifact verify target")
            artifact_id = None
            if candidate.suffix == MANIFEST_SUFFIX and candidate.parent == self.root:
                try:
                    payload = self._read_manifest_payload(candidate)
                    artifact_id_value = payload.get("artifact_id")
                    if isinstance(artifact_id_value, str):
                        artifact_id = artifact_id_value
                except ArtifactRegistryError:
                    result = self._invalid_record(candidate, "manifest_invalid")
                    if raise_on_error:
                        raise ArtifactManifestError(result.reason or "artifact manifest invalid") from None
                    return result
            if artifact_id is None:
                self._ensure_root(create=False)
                records, _, _, _ = self._scan_records(max_records=self.max_records)
                matches = tuple(record for record in records if record.path == candidate)
                result = matches[0] if matches else self._invalid_record(candidate, "artifact_unmanaged")
                if raise_on_error and not result:
                    raise ArtifactSecurityError(result.reason or "artifact is not verified")
                return result
        else:
            artifact_id = _bounded_text(
                str(target),
                label="artifact_id",
                limit=MAX_ARTIFACT_ID_BYTES,
            )
        self._ensure_root(create=False)
        assert artifact_id is not None
        manifest_path = self._manifest_path(artifact_id)
        try:
            result = self._load_record(manifest_path)
        except (ArtifactRegistryError, OSError, TypeError, ValueError) as exc:
            result = self._invalid_record(manifest_path, f"manifest_invalid:{type(exc).__name__}")
        if raise_on_error and not result:
            raise ArtifactSecurityError(result.reason or "artifact is not verified")
        return result

    def verify_bool(self, target: str | Path | ArtifactRecord) -> bool:
        """Boolean convenience wrapper around detailed :meth:`verify`."""

        result = self.verify(target)
        return isinstance(result, ArtifactRecord) and result.verified

    @contextmanager
    def observation_guard(self) -> Iterator["ArtifactRegistry"]:
        """Hold the existing registry guard for a coordinated owner snapshot.

        This read-only coordination surface grants no retirement or lifecycle
        authority. A missing registry remains absent; existing consumers can
        use the same guarded instance to verify their exact claims.
        """
        if not self._ensure_root(create=False):
            yield self
            return
        with self._registry_lock():
            yield self

    def for_owner(self, owner: str) -> "ArtifactRegistry":
        """Bind an explicit owner while sharing this instance's nested guard.

        The owning subsystem must supply its own logical owner. This does not
        rewrite claims or bypass the existing owner and dependency checks.
        """
        bound = ArtifactRegistry(self.root, owner=owner, create_root=False,
                                 max_records=self.max_records, max_bytes=self.max_bytes,
                                 max_manifest_bytes=self.max_manifest_bytes,
                                 max_metadata_bytes=self.max_metadata_bytes)
        bound._lock_local = self._lock_local
        return bound

    @contextmanager
    def retirement_guard(self, artifact: str | ArtifactRecord) -> Iterator[ArtifactRecord]:
        from .artifact_retirement import retirement_guard
        with retirement_guard(self, artifact) as record:
            yield record

    @contextmanager
    def retirement_batch_guard(self) -> Iterator[_RetirementBatch]:
        """Hold one registry lock for a bounded retirement batch.

        The batch shares one dependency observation, eliminating the old
        ``N`` full-registry rescans while preserving per-target reloads,
        policy checks, and identity checks immediately before each effect.
        """

        if self.owner is None:
            raise ArtifactSecurityError(
                "federated artifact registry view is read-only for retirement"
            )
        self._ensure_root(create=False)
        with self._registry_lock():
            records, unmanaged, truncated, _reasons = self._scan_records(
                max_records=self.max_records,
            )
            yield _RetirementBatch(self, records, unmanaged, truncated)

    def _retirement_guard_locked(
        self,
        artifact: str | ArtifactRecord,
        *,
        records: tuple[ArtifactRecord, ...] | None = None,
        unmanaged: tuple[Path, ...] = (),
        truncated: bool = False,
    ) -> Iterator[ArtifactRecord]:
        from .artifact_retirement import retirement_guard_locked
        yield from retirement_guard_locked(
            self, artifact, records=records, unmanaged=unmanaged, truncated=truncated
        )

    def _prepare_retirement_locked(self, record: ArtifactRecord) -> ArtifactRecord:
        from .artifact_retirement import prepare_retirement
        return prepare_retirement(self, record)

    def _record_retirement_failure_locked(self, record: ArtifactRecord) -> None:
        from .artifact_retirement import record_retirement_failure
        return record_retirement_failure(self, record)

    def _clear_retirement_intent_locked(self, record: ArtifactRecord) -> None:
        from .artifact_retirement import clear_retirement_intent
        return clear_retirement_intent(self, record)

    @_registry_write_locked
    def recover_retirements(self) -> dict[str, object]:
        from .artifact_retirement import recover_retirements
        return recover_retirements(self)

    def _classify_for_owner(self, record: ArtifactRecord, *, now_ns: int) -> tuple[str, str]:
        if not record.valid:
            if record.issue in _BLOCKING_ISSUES:
                return "blocked", record.issue
            return "unknown", record.issue or "manifest_invalid"
        if self.owner is not None and record.owner != self.owner:
            return "blocked", "owner_mismatch"
        return self._classify(record, now_ns=now_ns)

    def plan(
        self,
        *,
        now_ns: int | None = None,
        max_records: int | None = None,
        max_entries: int | None = None,
        max_bytes: int | None = None,
    ) -> ArtifactPlan:
        """Return a bounded classification without deleting or moving anything."""

        now = time.time_ns() if now_ns is None else now_ns
        if type(now) is not int or now < 0:
            raise ValueError("artifact plan now_ns must be a non-negative integer")
        if max_entries is not None:
            max_records = max_entries
        effective_records = self.max_records if max_records is None else _validate_limit(
            max_records,
            label="artifact plan max_records",
        )
        effective_bytes = self.max_bytes if max_bytes is None else _validate_limit(
            max_bytes,
            label="artifact plan max_bytes",
        )
        if not self._ensure_root(create=False):
            return ArtifactPlan(
                root=self.root,
                reason="artifact registry root is absent",
                root_blocked="artifact registry root is absent",
                max_records=effective_records,
                max_bytes=effective_bytes,
            )
        root_metadata = self.root.lstat()
        root_identity = _identity(root_metadata)
        records, unmanaged, truncated, truncation_reasons = self._scan_records(
            max_records=effective_records,
        )
        categories: dict[str, int] = dict.fromkeys(
            ("protected", "eligible", "blocked", "unknown"), 0
        )
        byte_categories: dict[str, int] = dict.fromkeys(categories, 0)
        reasons: dict[str, int] = {}
        observed_records: list[ArtifactRecord] = []
        total_bytes = 0
        byte_truncated = False
        dependency_complete = self._dependency_observation_complete(
            records,
            unmanaged,
            truncated=truncated,
        )
        for record in records:
            category, reason = self._classify_for_owner(record, now_ns=now)
            eligible = category == "eligible"
            if record.issue == "size_truncated":
                byte_truncated = True
                category = "blocked"
                reason = "size_truncated"
                eligible = False
            if eligible:
                if not dependency_complete:
                    # A malformed/foreign/omitted registry entry may be the
                    # consumer that protects this artifact.  Do not turn the
                    # missing information into an empty dependency set.
                    category = "blocked"
                    reason = "dependency_observation_incomplete"
                    eligible = False
                else:
                    dependents = self._live_dependents(record, records)
                if eligible and dependents:
                    category = "protected"
                    reason = "dependency_live"
                    eligible = False
                elif eligible and truncated:
                    # A bounded registry scan cannot prove that no unseen
                    # consumer references this artifact.  Fail closed rather
                    # than presenting a partial selection as disposable.
                    category = "blocked"
                    reason = "dependency_observation_incomplete"
                    eligible = False
            observed = record
            if total_bytes + record.size_bytes > effective_bytes:
                byte_truncated = True
                credited = max(0, effective_bytes - total_bytes)
                observed = replace(record, size_bytes=credited, valid=False, issue="size_truncated", reason="size_truncated")
                # A byte fence means the complete artifact observation was not
                # obtained.  No category may remain eligible on a partial
                # observation; preserve it as blocked instead.
                category = "blocked"
                reason = "size_truncated"
                eligible = False
            total_bytes += min(record.size_bytes, max(0, effective_bytes - total_bytes))
            observed = replace(observed, classification=category, eligible=eligible, reason=reason)
            observed_records.append(observed)
            categories[category] += 1
            byte_categories[category] += observed.size_bytes
            reasons[reason] = reasons.get(reason, 0) + 1
        if byte_truncated:
            truncated = True
            truncation_reasons = tuple(dict.fromkeys((*truncation_reasons, "byte_limit")))
        unmanaged_bytes = 0
        for path in unmanaged:
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                unmanaged_bytes += min(max(0, int(metadata.st_size)), max(0, effective_bytes - unmanaged_bytes))
        status = "unknown" if categories["unknown"] else "blocked" if categories["blocked"] else "planned"
        return ArtifactPlan(
            root=self.root,
            records=tuple(observed_records),
            unmanaged=unmanaged,
            protected=categories["protected"],
            eligible=categories["eligible"],
            blocked=categories["blocked"],
            unknown=categories["unknown"],
            protected_bytes=byte_categories["protected"],
            eligible_bytes=byte_categories["eligible"],
            blocked_bytes=byte_categories["blocked"],
            unknown_bytes=byte_categories["unknown"],
            unmanaged_bytes=unmanaged_bytes,
            scanned=len(records) + len(unmanaged),
            returned=len(records) + len(unmanaged),
            truncated=truncated,
            truncation_reasons=tuple(truncation_reasons),
            reasons=reasons,
            status=status,
            reason=truncation_reasons[0] if truncation_reasons else None,
            read_only=True,
            root_identity=root_identity,
            max_records=effective_records,
            max_bytes=effective_bytes,
        )

    def records(self) -> tuple[ArtifactRecord, ...]:
        """Return the bounded manifest view without creating the root."""

        if not self._ensure_root(create=False):
            return ()
        records, _, _, _ = self._scan_records(max_records=self.max_records)
        return records

    def to_dict(self, *, include_records: bool = True) -> dict[str, object]:
        """Return a bounded registry description with no artifact contents."""

        result: dict[str, object] = {
            "schema": ARTIFACT_REGISTRY_SCHEMA,
            "root": str(self.root),
            "owner": self.owner,
            "limits": {
                "max_records": self.max_records,
                "max_bytes": self.max_bytes,
                "max_manifest_bytes": self.max_manifest_bytes,
                "max_metadata_bytes": self.max_metadata_bytes,
            },
        }
        if include_records:
            result["records"] = [record.to_dict() for record in self.records()]
        return result

    @_registry_write_locked
    def apply_tombstone_retention(
        self,
        artifact_ids: Iterable[str],
        *,
        release_authorized: bool,
        evidence: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        from .artifact_retirement import apply_tombstone_retention
        return apply_tombstone_retention(
            self, artifact_ids, release_authorized=release_authorized,
            evidence=evidence, operation_id=operation_id,
        )

    @_registry_write_locked
    def recover_tombstone_retention(self) -> dict[str, object]:
        from .artifact_retirement import recover_tombstone_retention
        return recover_tombstone_retention(self)


# endregion [03]


__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_REGISTRY_SCHEMA",
    "ARTIFACT_STATES",
    "MANIFEST_SUFFIX",
    "MAX_DEPENDENCIES",
    "MAX_MANIFEST_BYTES",
    "MAX_METADATA_BYTES",
    "MAX_RECORDS",
    "ArtifactConflictError",
    "ArtifactKind",
    "ArtifactManifestError",
    "ArtifactPlan",
    "ArtifactRecord",
    "ArtifactRegistry",
    "ArtifactRegistryError",
    "ArtifactRootError",
    "ArtifactSecurityError",
    "ArtifactState",
]
