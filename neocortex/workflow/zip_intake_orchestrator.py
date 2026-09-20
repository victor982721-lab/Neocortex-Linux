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

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.intake import ZipIntakeLimits
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

    def payload(self) -> dict[str, object]:
        return {
            "total_files": self.total_files,
            "eligible_files": self.eligible_files,
            "size_skipped_files": self.size_skipped_files,
            "size_skipped_bytes": self.size_skipped_bytes,
            "max_file_bytes": self.max_file_bytes,
        }


@dataclass(frozen=True, slots=True)
class ZipIntakeStageResult:
    """Bounded orchestration result for one ZIP Intake stage."""

    details: Mapping[str, object]
    filesystem_changed: bool = False
    reconciliation_required: bool = False

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
    This function only reads ``FileSnapshot.size``.  In particular, a source
    ZIP above the global ceiling never reaches the engine and cannot be
    classified, hashed, staged, or opened by this stage.
    """

    limit = validate_max_file_bytes(max_file_bytes)
    admitted: list[FileSnapshot] = []
    total_files = eligible_files = size_skipped_files = size_skipped_bytes = 0
    for snapshot in snapshots:
        if not isinstance(snapshot, FileSnapshot):
            raise TypeError("ZIP Intake admission requires FileSnapshot values")
        total_files += 1
        if size_is_admitted(snapshot.size, limit):
            eligible_files += 1
            admitted.append(snapshot)
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


def _zip_candidate(path: Path) -> bool:
    """Read only the ZIP signature needed to find mislabeled containers."""

    if path.suffix.casefold() == ".zip":
        return True
    try:
        with path.open("rb") as source:
            return source.read(4) in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x06\x06"}
    except OSError:
        # The intake engine owns the typed blocked/corrupt outcome for a
        # candidate path.  A path that cannot even provide a bounded signature
        # is not a candidate discovered by this metadata/header pass.
        return False


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
            "archive_max_total_uncompressed_bytes",
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
    )


class _KioTrashHook:
    """Bind the existing KIO backend to the intake identity contract."""

    def __init__(self, backend: object, root: Path, snapshot: FileSnapshot) -> None:
        self._backend = backend
        self._root = root
        self._snapshot = snapshot

    def trash(self, source: Path, identity: object) -> object:
        from neocortex.capabilities.formats.archive.intake import TrashDisposition
        from neocortex.deduplication.fingerprinting import snapshot_path
        from neocortex.safety.kio_trash import metadata_binding

        if Path(source) != Path(self._snapshot.path):
            return TrashDisposition("blocked", "intake_source_snapshot_mismatch")
        try:
            current = snapshot_path(source)
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
        "generic_candidates": 0,
        "atomic_packages": 0,
        "planned": 0,
        "applied": 0,
        "published": 0,
        "trashed": 0,
        "members": 0,
        "uncompressed_bytes": 0,
    }
    statuses: dict[str, int] = {}
    samples: list[dict[str, object]] = []
    filesystem_changed = False
    for snapshot in admission.snapshots:
        checkpoint = getattr(cancellation, "checkpoint", None)
        if callable(checkpoint):
            checkpoint()
        if not _zip_candidate(Path(snapshot.path)):
            continue
        counters["candidates"] += 1
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
        )
        source_payload = _engine_result_payload(outcome)
        source_status = str(source_payload.get("status", "unknown"))
        statuses[source_status] = statuses.get(source_status, 0) + 1
        classification = source_payload.get("classification")
        if isinstance(classification, Mapping):
            kind = classification.get("kind")
            if kind == "generic_zip":
                counters["generic_candidates"] += 1
            elif kind == "atomic_package":
                counters["atomic_packages"] += 1
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
        "filesystem_changed": filesystem_changed,
        "reconciliation_required": bool(apply and filesystem_changed),
        **counters,
        "statuses": statuses,
        "failures": failure_statuses,
        "failure_samples": samples,
    }
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
