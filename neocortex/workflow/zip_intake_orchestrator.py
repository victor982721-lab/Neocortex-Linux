"""Framework-owned adapter for the physical ZIP intake stage.

The archive intake implementation owns ZIP parsing, staging, publication, and
KIO Trash.  Framework owns *when* that service is called and which inventory
rows are admitted to it.  Keeping this boundary here prevents the old Archive
route from becoming an implicit second intake path.

This module deliberately imports the intake engine lazily.  A normal import of
the runtime/orchestrator must not import ZIP parsers or create a scratch
workspace; the stage is loaded only for an initial ``--all`` run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.admission import size_is_admitted, validate_max_file_bytes
from neocortex.progress import ProgressEvent, ProgressMetric, emit_progress

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.intake import ZipIntakeLimits
    from neocortex.platform.content_types import DetectedType
    from neocortex.progress import ProgressCallback
    from neocortex.runtime.control.cancellation import CancellationToken


INTAKE_ENGINE_MODULE = "neocortex.capabilities.formats.archive.intake"
INTAKE_ENGINE_CALLABLE = "run_zip_intake"
ZIP_INTAKE_SCHEMA = "neocortex.zip-intake/v1"


class _KioTrashBackend(Protocol):
    def apply_snapshot(
        self,
        snapshot: FileSnapshot,
        *,
        root: Path,
        source_digest: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ZipIntakeAdmission:
    """Metadata-only admission view passed from Inventory to ZIP Intake."""

    snapshots: tuple[FileSnapshot, ...]
    total_files: int
    eligible_files: int
    size_skipped_files: int
    size_skipped_bytes: int
    max_file_bytes: int | None
    # These counters are derived only from the already-completed Inventory
    # snapshots.  They are intentionally not ZIP candidates: a ``.docx`` or
    # ``.odt`` is a ZIP-backed *container* without being a file whose suffix is
    # ``.zip``.  Keep both totals so consumers can explain the distinction
    # without a second directory walk or a header read.
    zip_files_inventoried: int = 0
    zip_files_admitted: int = 0

    def payload(self) -> dict[str, object]:
        return {
            "total_files": self.total_files,
            "eligible_files": self.eligible_files,
            "size_skipped_files": self.size_skipped_files,
            "size_skipped_bytes": self.size_skipped_bytes,
            "max_file_bytes": self.max_file_bytes,
            "zip_files_inventoried": self.zip_files_inventoried,
            "zip_files_admitted": self.zip_files_admitted,
        }


@dataclass(frozen=True, slots=True)
class ZipIntakeStageResult:
    """Bounded orchestration result for one ZIP Intake stage."""

    details: Mapping[str, object]
    filesystem_changed: bool = False
    reconciliation_required: bool = False
    # Caller-owned, identity-bound seeds for Framework Identify.  These stay
    # out of the serialized stage payload and are consumed on the SQLite owner
    # thread before Identify starts.
    atomic_decisions: tuple[tuple[FileSnapshot, "DetectedType"], ...] = ()

    @property
    def status(self) -> str:
        value = self.details.get("status")
        return value if isinstance(value, str) else "completed"

    def as_dict(self) -> dict[str, object]:
        return dict(self.details)


def build_zip_intake_admission(
    snapshots: Iterable[FileSnapshot],
    max_file_bytes: int | None,
) -> ZipIntakeAdmission:
    """Filter Inventory metadata without opening or stat'ing corpus paths.

    The caller supplies snapshots from the completed Inventory generation.
    This function only reads snapshot metadata (size and the basename). A source
    ZIP above the global ceiling never reaches the engine and cannot be
    classified, hashed, staged, or opened by this stage.
    """

    limit = validate_max_file_bytes(max_file_bytes)
    admitted: list[FileSnapshot] = []
    total_files = eligible_files = size_skipped_files = size_skipped_bytes = 0
    zip_files_inventoried = zip_files_admitted = 0
    for snapshot in snapshots:
        if not isinstance(snapshot, FileSnapshot):
            raise TypeError("ZIP Intake admission requires FileSnapshot values")
        total_files += 1
        # ``path`` is an Inventory value, not a path to inspect here.  Do not
        # use exists/stat/open (or a fresh walk) to derive this presentation
        # counter: it must describe exactly the snapshot generation supplied
        # by the caller.
        is_zip_suffix = Path(snapshot.path).name.casefold().endswith(".zip")
        if is_zip_suffix:
            zip_files_inventoried += 1
        if size_is_admitted(snapshot.size, limit):
            eligible_files += 1
            admitted.append(snapshot)
            if is_zip_suffix:
                zip_files_admitted += 1
        else:
            size_skipped_files += 1
            size_skipped_bytes += int(snapshot.size)
    return ZipIntakeAdmission(
        tuple(admitted),
        total_files,
        eligible_files,
        size_skipped_files,
        size_skipped_bytes,
        limit,
        zip_files_inventoried,
        zip_files_admitted,
    )


def _engine_result_payload(value: object) -> dict[str, object]:
    """Convert the engine's public result contract to a bounded mapping."""

    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        to_dict = getattr(value, "to_dict", None)
        if not callable(to_dict):
            raise TypeError(
                "ZIP Intake engine must return a mapping or an object with to_dict()"
            )
        converted = to_dict()
        if not isinstance(converted, Mapping):
            raise TypeError("ZIP Intake engine to_dict() must return a mapping")
        payload = dict(converted)
    # Engine-owned details may add counters, but these fields are Framework
    # facts and are always overwritten at the boundary.  This also ensures a
    # stale per-file decision cannot masquerade as a global admission result.
    return payload


_SUCCESS_STATUSES = frozenset({"planned", "atomic", "applied"})


def _classification_kind(decision: object, payload: Mapping[str, object]) -> str | None:
    """Return the bounded classification kind without inspecting the source.

    The engine normally echoes the identity-bound decision in its result.  A
    compatibility adapter/test double may omit that echo, so fall back to the
    decision that this orchestrator already holds.  This is deliberately a
    DTO-only operation: it must not reopen the source or infer a kind from its
    suffix.
    """

    classification = payload.get("classification")
    if isinstance(classification, Mapping):
        value = classification.get("kind")
        if isinstance(value, str):
            return value
    selected = getattr(decision, "classification", None)
    value = getattr(selected, "kind", None)
    return value if isinstance(value, str) else None


def _trusted_extracted_files(
    payload: Mapping[str, object],
    *,
    classification_kind: str | None,
    status: str,
) -> int | None:
    """Read an explicit physical-file extraction count, if the owner supplied one.

    ``members`` is not such a count: it can include directories and nested
    members, and it is populated for a dry-run plan.  Likewise ``published``
    is a boolean/container-level fact.  Only the explicit file counters are
    safe to expose as ``extracted_files``.  Returning ``None`` keeps the UI at
    an honest unknown state instead of manufacturing a number from containers.
    """

    if classification_kind != "generic_zip" or status not in {
        "applied",
        "recovery_required",
    }:
        return 0
    for name in ("extracted_files", "published_files"):
        value = payload.get(name)
        if type(value) is int and value >= 0:
            return value
    return None


def _zip_limits(config: object) -> ZipIntakeLimits:
    """Build engine limits from existing archive knobs without eager imports."""

    from neocortex.capabilities.formats.archive.intake import ZipIntakeLimits
    defaults = ZipIntakeLimits()

    def integer(name: str, default: int) -> int:
        value = getattr(config, name, default)
        return default if value is None else int(value)

    return ZipIntakeLimits(
        max_members=integer("archive_max_members", defaults.max_members),
        max_member_bytes=integer(
            "archive_max_member_bytes", defaults.max_member_bytes
        ),
        max_total_uncompressed_bytes=integer(
            "archive_max_total_uncompressed_bytes",
            defaults.max_total_uncompressed_bytes,
        ),
        max_total_temp_bytes=integer(
            "archive_max_total_temp_bytes",
            defaults.max_total_temp_bytes,
        ),
        max_nested_depth=integer("archive_max_depth", defaults.max_nested_depth),
        max_compression_ratio=float(
            getattr(config, "archive_max_compression_ratio", defaults.max_compression_ratio)
        ),
        max_central_directory_bytes=integer(
            "archive_max_central_directory_bytes",
            defaults.max_central_directory_bytes,
        ),
        auto_measured_budget=bool(
            getattr(config, "archive_auto_measured_budget", defaults.auto_measured_budget)
        ),
        auto_max_member_bytes=integer(
            "archive_auto_max_member_bytes", defaults.auto_max_member_bytes
        ),
        auto_max_total_uncompressed_bytes=integer(
            "archive_auto_max_total_uncompressed_bytes",
            defaults.auto_max_total_uncompressed_bytes,
        ),
        auto_max_expansion_ratio=float(
            getattr(config, "archive_auto_max_expansion_ratio", defaults.auto_max_expansion_ratio)
        ),
    )


def _decide_zip_candidate(
    engine: object,
    path: Path,
    *,
    max_file_bytes: int | None,
    limits: object,
    cancellation: object,
    progress: object | None,
) -> object | None:
    """Delegate candidate discovery and ZIP classification to the engine.

    The engine owns the one bounded signature/content decision.  Framework
    must not probe a header and then ask the engine to inspect the same path
    again.  The owner is mandatory so a missing decision seam fails closed
    instead of silently skipping wrong-extension ZIPs.
    """

    decider = getattr(engine, "decide_zip_candidate", None)
    if not callable(decider):
        raise RuntimeError(
            f"ZIP Intake engine {INTAKE_ENGINE_MODULE!r} has no "
            "decide_zip_candidate() callable"
        )
    return decider(
        path,
        max_file_bytes=max_file_bytes,
        limits=limits,
        cancellation=cancellation,
        progress=progress,
    )


def _atomic_detected_type(decision: object) -> "DetectedType | None":
    """Translate a validated atomic decision into a cache DTO."""

    classification = getattr(decision, "classification", None)
    if classification is None or getattr(classification, "kind", None) != "atomic_package":
        return None
    values: dict[str, tuple[str, str, tuple[str, ...], str]] = {
        "docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".docx",
            (".docx", ".dotx", ".docm", ".dotm"),
            "zip:intake-atomic-docx",
        ),
        "xlsx": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
            (".xlsx", ".xltx", ".xlsm", ".xltm"),
            "zip:intake-atomic-xlsx",
        ),
        "pptx": (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".pptx",
            (".pptx", ".potx", ".ppsx", ".pptm", ".potm", ".ppsm"),
            "zip:intake-atomic-pptx",
        ),
        "epub": ("application/epub+zip", ".epub", (".epub",), "zip:intake-atomic-epub"),
        "apk": (
            "application/vnd.android.package-archive",
            ".apk",
            (".apk",),
            "zip:intake-atomic-apk",
        ),
        "jar": ("application/java-archive", ".jar", (".jar",), "zip:intake-atomic-jar"),
    }
    unit_kind = str(
        getattr(classification, "unit_kind", getattr(classification, "codec", ""))
    ).casefold()
    value = values.get(unit_kind)
    if unit_kind == "odf":
        odf = {
            "application/vnd.oasis.opendocument.text": (".odt", (".odt", ".ott")),
            "application/vnd.oasis.opendocument.spreadsheet": (".ods", (".ods", ".ots")),
            "application/vnd.oasis.opendocument.presentation": (".odp", (".odp", ".otp")),
            "application/vnd.oasis.opendocument.graphics": (".odg", (".odg", ".otg")),
        }.get(str(getattr(classification, "mime", "")).casefold())
        if odf is not None:
            odf_extension, odf_accepted = odf
            value = (
                str(classification.mime),
                odf_extension,
                odf_accepted,
                "zip:intake-atomic-odf",
            )
    if value is None:
        # ODF currently does not retain its exact MIME in the bounded
        # classification DTO; avoid seeding an incorrect route decision.
        return None
    from neocortex.platform.content_types import DetectedType

    mime, extension, accepted_extensions, evidence = value
    return DetectedType(mime, extension, frozenset(accepted_extensions), evidence)


class _KioTrashHook:
    """Bind the existing KIO backend to the intake identity contract."""

    def __init__(self, backend: object, root: Path, snapshot: FileSnapshot) -> None:
        self._backend = backend
        self._root = root
        self._snapshot = snapshot

    def trash(self, source: Path, identity: object) -> object:
        from neocortex.capabilities.formats.archive.intake import (
            SourceIdentity,
            TrashDisposition,
        )
        from neocortex.deduplication.fingerprinting import snapshot_path
        from neocortex.safety.kio_trash import metadata_binding

        source_path = Path(source)
        if source_path != Path(self._snapshot.path):
            return TrashDisposition("blocked", "intake_source_snapshot_mismatch")
        # The intake engine's SourceIdentity is the proof captured before
        # classification/staging.  Never replace it with a fresh path
        # observation: a replacement at the same pathname must not become the
        # object that KIO receives.  Keep this validation here, immediately at
        # the framework-to-KIO seam, in addition to the backend's own checks.
        if not isinstance(identity, SourceIdentity):
            return TrashDisposition("blocked", "source_identity_invalid")
        try:
            identity_path = Path(identity.path)
        except (TypeError, ValueError, OSError):
            return TrashDisposition("blocked", "source_identity_invalid")
        if identity_path != source_path:
            return TrashDisposition("blocked", "source_identity_invalid")
        try:
            current_identity = SourceIdentity.capture(source_path)
            if current_identity != identity:
                return TrashDisposition("blocked", "source_changed")
            # KIO's path-bound effect is only safe for a unique regular file.
            # A changed link count is also a source drift even when all other
            # visible metadata happens to remain equal.
            if current_identity.nlink != 1:
                return TrashDisposition("blocked", "source_changed")
            current = snapshot_path(source_path)
            if current != self._snapshot:
                return TrashDisposition("blocked", "source_changed")
        except (OSError, RuntimeError, ValueError, TypeError):
            # Validation failures are pre-frontier source drift.  Do not pass
            # a newly observed path/snapshot to KIO and do not label this a
            # backend recovery case.
            return TrashDisposition("blocked", "source_changed")
        try:
            binding = metadata_binding(current)
            backend = cast(_KioTrashBackend, self._backend)
            outcome = backend.apply_snapshot(
                current,
                root=self._root,
                source_digest=binding,
            )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            return TrashDisposition("blocked", f"kio_trash_adapter:{type(exc).__name__}:{exc}")
        status = getattr(outcome, "status", None)
        detail = getattr(outcome, "detail", None)
        receipt = getattr(outcome, "receipt_json", None)
        if status == "applied":
            return TrashDisposition("applied", detail, receipt)
        if status == "recovery_required":
            return TrashDisposition("recovery_required", detail, receipt)
        return TrashDisposition("blocked", detail, receipt)


def run_zip_intake_stage(
    *,
    root: Path,
    admission: ZipIntakeAdmission,
    config: object,
    apply: bool,
    state_directory: Path,
    run_id: int,
    state: object,
    progress: ProgressCallback | None,
    cancellation: CancellationToken,
) -> ZipIntakeStageResult:
    """Run the explicit archive intake engine over admitted snapshots.

    The callable and module name are intentionally fixed.  If the engine is
    absent or returns an invalid contract, Framework fails closed instead of
    silently falling back to the legacy virtual Archive route.
    """

    if not isinstance(root, Path) or not isinstance(state_directory, Path):
        raise TypeError("ZIP Intake root and state_directory must be Path values")
    if type(run_id) is not int or run_id < 1:
        raise ValueError("ZIP Intake run_id must be a positive integer")
    limit = admission.max_file_bytes
    engine = import_module(INTAKE_ENGINE_MODULE)
    runner = getattr(engine, INTAKE_ENGINE_CALLABLE, None)
    if not callable(runner):
        raise RuntimeError(
            f"ZIP Intake engine {INTAKE_ENGINE_MODULE!r} has no "
            f"{INTAKE_ENGINE_CALLABLE}() callable"
        )
    scratch_root = state_directory / "scratch" / "zip-intake"
    limits = _zip_limits(config)
    staging_factory: object | None = None
    trash_backend: object | None = None

    counters: dict[str, int] = {
        "candidates": 0,
        # ``candidates`` remains the source-compatible spelling.  The typed
        # name is what consumers should use for the container progress row.
        "containers_examined": 0,
        "generic_candidates": 0,
        "generic_identified": 0,
        "atomic_packages": 0,
        "unclassified": 0,
        "blocked": 0,
        "planned": 0,
        "applied": 0,
        "published": 0,
        "trashed": 0,
        "members": 0,
        "uncompressed_bytes": 0,
    }
    extracted_files = 0
    extracted_files_known = True
    statuses: dict[str, int] = {}
    samples: list[dict[str, object]] = []
    atomic_decisions: list[tuple[FileSnapshot, "DetectedType"]] = []
    filesystem_changed = False
    for snapshot in admission.snapshots:
        checkpoint = getattr(cancellation, "checkpoint", None)
        if callable(checkpoint):
            checkpoint()
        source_path = Path(snapshot.path)
        decision = _decide_zip_candidate(
            engine,
            source_path,
            max_file_bytes=limit,
            limits=limits,
            cancellation=cancellation,
            progress=progress,
        )
        if decision is None:
            continue
        counters["candidates"] += 1
        counters["containers_examined"] += 1
        if apply and staging_factory is None:
            from neocortex.capabilities.formats.archive.intake import ScratchStageFactory
            from neocortex.workflow.mutations import KioTrashBackend

            staging_factory = ScratchStageFactory(scratch_root)
            trash_backend = KioTrashBackend()
        trash = None if not apply else _KioTrashHook(trash_backend, root, snapshot)
        outcome = runner(
            snapshot.path,
            apply=bool(apply),
            max_file_bytes=limit,
            limits=limits,
            staging=staging_factory,
            trash=trash,
            cancellation=cancellation,
            cancellation_token=cancellation,
            progress=progress,
            decision=decision,
        )
        source_payload = _engine_result_payload(outcome)
        source_status = str(source_payload.get("status", "unknown"))
        statuses[source_status] = statuses.get(source_status, 0) + 1
        kind = _classification_kind(decision, source_payload)
        if kind == "generic_zip":
            counters["generic_candidates"] += 1
            counters["generic_identified"] += 1
        elif kind == "atomic_package":
            counters["atomic_packages"] += 1
            # Presentation can describe the held decision without an echo.
            # Cache publication keeps its original engine-result gate.
            classification = source_payload.get("classification")
            if isinstance(classification, Mapping) and classification.get("kind") == "atomic_package":
                detected = _atomic_detected_type(decision)
                if detected is not None:
                    atomic_decisions.append((snapshot, detected))
        else:
            # ``invalid``/unknown is a classification outcome, not evidence
            # that the source was a generic ZIP.  Keep it separate from the
            # status-based blocked count below; a generic collision, for
            # example, is generic *and* blocked.
            counters["unclassified"] += 1
        if source_status == "planned":
            counters["planned"] += 1
        if source_status == "applied":
            counters["applied"] += 1
        if bool(source_payload.get("published")):
            counters["published"] += 1
            filesystem_changed = True
        if bool(source_payload.get("trashed")):
            counters["trashed"] += 1
            filesystem_changed = True
        if bool(source_payload.get("filesystem_changed")):
            filesystem_changed = True
        if source_status not in _SUCCESS_STATUSES:
            counters["blocked"] += 1
        extracted = _trusted_extracted_files(
            source_payload,
            classification_kind=kind,
            status=source_status,
        )
        if extracted is None:
            # One applied generic without an explicit regular-file count makes
            # the aggregate unknown.  Do not fall back to ``members`` or the
            # boolean ``published`` field.
            extracted_files_known = False
        elif extracted_files_known:
            extracted_files += extracted
        for name in ("members", "uncompressed_bytes"):
            value = source_payload.get(name)
            if type(value) is int and value >= 0:
                counters[name] += value
        if len(samples) < 32 and source_status not in {"planned", "atomic", "applied"}:
            samples.append(
                {
                    "path": snapshot.path,
                    "status": source_status,
                    "reason": source_payload.get("reason"),
                    "detail": source_payload.get("detail"),
                }
            )
        # Engine callbacks describe phases within one source; only this
        # caller knows how many containers have settled across the batch.
        # Preserve the real callback for cancellation/member observation,
        # then publish cumulative container counts without a guessed total.
        emit_progress(
            progress,
            ProgressEvent(
                "zip-intake",
                "process",
                "Procesando ZIPs",
                counters["candidates"],
                None,
                "ZIPs",
                metrics=(
                    *(ProgressMetric(name, value) for name, value in counters.items()),
                    ProgressMetric("zip_files_inventoried", admission.zip_files_inventoried),
                    ProgressMetric("zip_files_admitted", admission.zip_files_admitted),
                    *(() if not extracted_files_known else (
                        ProgressMetric("extracted_files", extracted_files),
                    )),
                    ProgressMetric("status", "running"),
                ),
            ),
        )

    failure_statuses = {
        status: count
        for status, count in statuses.items()
        if status not in {"planned", "atomic", "applied"}
    }
    if not counters["candidates"]:
        status = "planned"
    elif failure_statuses:
        successful = sum(
            count
            for name, count in statuses.items()
            if name in {"planned", "atomic", "applied"}
        )
        status = "partial" if successful else "blocked"
    elif apply and counters["applied"]:
        status = "applied"
    else:
        status = "planned"
    payload = {
        "schema": ZIP_INTAKE_SCHEMA,
        "status": status,
        "mode": "apply" if apply else "plan",
        "apply": bool(apply),
        "max_file_bytes": limit,
        "total_files": admission.total_files,
        "eligible_files": admission.eligible_files,
        "size_skipped_files": admission.size_skipped_files,
        "size_skipped_bytes": admission.size_skipped_bytes,
        "zip_files_inventoried": admission.zip_files_inventoried,
        "zip_files_admitted": admission.zip_files_admitted,
        "containers_examined": counters["containers_examined"],
        "containers_total": counters["containers_examined"],
        "filesystem_changed": filesystem_changed,
        "reconciliation_required": bool(apply and filesystem_changed),
        **counters,
        "atomic_decisions_reused": len(atomic_decisions),
        "statuses": statuses,
        "failures": failure_statuses,
        "failure_samples": samples,
    }
    if extracted_files_known:
        payload["extracted_files"] = extracted_files
    else:
        # Keep the distinction explicit in durable stage evidence while
        # omitting the typed count from progress so the UI can render ``—``.
        payload["extracted_files_known"] = False
    reconciliation_required = bool(apply and filesystem_changed)
    if not apply:
        # A plan is never allowed to trigger a rescan merely because the
        # engine estimated a publication.
        filesystem_changed = False
        reconciliation_required = False
    return ZipIntakeStageResult(
        details=payload,
        filesystem_changed=filesystem_changed,
        reconciliation_required=reconciliation_required,
        atomic_decisions=tuple(atomic_decisions),
    )


__all__ = [
    "INTAKE_ENGINE_CALLABLE",
    "INTAKE_ENGINE_MODULE",
    "ZIP_INTAKE_SCHEMA",
    "ZipIntakeAdmission",
    "ZipIntakeStageResult",
    "build_zip_intake_admission",
    "run_zip_intake_stage",
]
