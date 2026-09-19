"""Safely apply exact-duplicate and extension-correction actions."""
# region [00] Contexto del módulo
# Módulo: neocortex/actions.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import json
import hashlib
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from neocortex.platform.policy import stat_birthtime_ns
from neocortex.foundation.hash_compat import HASH_ALGORITHM_128

from neocortex.deduplication import (
    DedupIndex,
    DedupPlan,
    FileChangedError,
    FileSnapshot,
    FULL_ALGORITHM,
    files_equal_exact,
    full_fingerprint,
    snapshot_path,
    stat_matches_snapshot,
)
from neocortex.deduplication.inventory.index import (
    DEFAULT_EXCLUDED_PATHS,
    InventoryExclusionPolicy,
    validate_inventory_root,
)
from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress
from neocortex.workflow.actions.action_policy import (
    corrected_path as _corrected_path,
    path_key as _path_key,
    postorder_directories as _postorder_directories,
    protected_path_reason as _protected_path_reason,
    same_snapshot as _same_snapshot,
    validate_mutation_path as _validate_mutation_path,
)
from neocortex.platform.content_types import DETECTOR_VERSION, DetectedType, detect_content_type
from neocortex.safety.corpus_access import CorpusMutationGuard, ProtectedAnalysisRootError
from neocortex.safety.internal_paths import InternalPathProtectionError
from neocortex.runtime.models import ActionSummary
from neocortex.safety.protected_content import ProtectedContentError
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.workflow.actions.file_action_recovery import expected_identity_json
from neocortex.curation.application import BackendOutcome, KioTrashBackend
from neocortex.code.code_contracts import ThirdPartyClassification
from neocortex.code.ingestion.code_detection import (
    classify_third_party_artifact,
    likely_code_candidate,
)
from neocortex.runtime.config.third_party_policy import CodeThirdPartyPolicy
from neocortex.workflow.actions.corpus_admission import (
    AdmissionDecision,
    CorpusAdmissionPolicy,
    MAX_PREFIX_BYTES,
    assess_file,
)

if TYPE_CHECKING:
    from neocortex.workflow.actions.regeneration import RegenerationProof
# endregion [01]

# region [02] Implementación


TRASH_BATCH_SIZE = 256
ReserveWork = Callable[[str, int, int], None]
MAX_PRESERVATION_EXAMPLES = 24
_THIRD_PARTY_METADATA_NAMES = frozenset(
    {
        "authors",
        "authors.txt",
        "changelog",
        "changelog.md",
        "copying",
        "copying.md",
        "license",
        "license.txt",
        "licenses",
        "licenses.txt",
        "licenses.md",
        "licence",
        "licence.txt",
        "notice",
        "notice.txt",
        "readme",
        "readme.md",
    }
)
_THIRD_PARTY_METADATA_PREFIXES = (
    "license",
    "licence",
    "copying",
    "notice",
    "authors",
    "third-party-notices",
    "third_party_notices",
    "thirdpartynotices",
)
TRASH_IDENTITY_ABSTENTION = (
    "Recycle Bin mutation abstained: the available Send2Trash backends resolve "
    "the source by path and cannot bind the observed file identity to the syscall"
)
# Compatibility probe for existing diagnostic/test consumers that monkeypatch
# the removed path backend to assert it is never invoked. Production code never
# reads or calls this sentinel.
send2trash: None = None


def _is_third_party_metadata_name(path: str | Path) -> bool:
    """Keep license/notice attribution files out of an origin cleanup plan."""

    name = Path(path).name.casefold()
    if name in _THIRD_PARTY_METADATA_NAMES:
        return True
    return any(
        name.startswith(prefix)
        and len(name) > len(prefix)
        and name[len(prefix)] in {"-", "_", "."}
        for prefix in _THIRD_PARTY_METADATA_PREFIXES
    )


class FrameworkActions:
    """Apply bounded action batches with durable before/after records."""

    def __init__(
        self,
        index: DedupIndex,
        state: FrameworkState,
        run_id: int,
        scan_id: int,
        *,
        apply: bool,
        verify_bytes_before_trash: bool = True,
        excluded_paths: Iterable[str | Path] = DEFAULT_EXCLUDED_PATHS,
        exclusion_policy: InventoryExclusionPolicy | None = None,
        progress: ProgressCallback | None = None,
        trash_backend: KioTrashBackend | None = None,
        third_party_policy: CodeThirdPartyPolicy | None = None,
        third_party_project_roots: Iterable[str | Path] = (),
        corpus_admission_policy: CorpusAdmissionPolicy | None = None,
        cancellation_check: Callable[[], None] | None = None,
        reserve_work: ReserveWork | None = None,
    ):
        self._index = index
        self._state = state
        self._run_id = run_id
        self._scan_id = scan_id
        self._apply = apply
        # Destructive mode never relies on a non-cryptographic fingerprint
        # alone, even when candidate reduction used the fast policy.
        self._verify_bytes_before_trash = apply or verify_bytes_before_trash
        self._exclusion_policy = exclusion_policy or InventoryExclusionPolicy.compile(
            excluded_paths
        )
        self._progress = progress
        self._trash_backend = trash_backend
        self._third_party_policy = third_party_policy or CodeThirdPartyPolicy()
        self._third_party_project_roots = tuple(
            Path(root).expanduser().absolute() for root in third_party_project_roots
        )
        self._deferred_reconciliation_paths: list[str] = []
        self._admission_policy = corpus_admission_policy
        from neocortex.runtime.config.app_paths import default_code_project_roots

        self._preservation_policy = corpus_admission_policy or CorpusAdmissionPolicy(
            interested_roots=self._third_party_project_roots or default_code_project_roots(),
        )
        self._cancellation_check = cancellation_check
        self._reserve_work = reserve_work
        self._admission_reasons: dict[str, int] = {}
        self._admission_examples: list[dict[str, object]] = []
        self._preservation_reasons: dict[str, int] = {}
        self._preservation_examples: list[dict[str, object]] = []
        self._regeneration_proofs: dict[str, RegenerationProof] = {}
        self._retained_regeneration_sources: dict[str, FileSnapshot] = {}
        self._regeneration_sources_truncated = False
        self._duplicate_work_reserved = False

    def execute(self, plan: DedupPlan, *, cleanup_empty_directories: bool = True) -> ActionSummary:
        self._validate_apply_root()
        self._deferred_reconciliation_paths.clear()
        self._admission_reasons.clear()
        self._admission_examples.clear()
        self._preservation_reasons.clear()
        self._preservation_examples.clear()
        self._regeneration_proofs.clear()
        self._retained_regeneration_sources.clear()
        self._regeneration_sources_truncated = False
        self._duplicate_work_reserved = False
        summary = ActionSummary(apply_actions=self._apply)
        started = time.perf_counter_ns()
        summary = self._trash_empty_files(plan, summary)
        self._record_phase("empty-files", started, summary)
        started = time.perf_counter_ns()
        summary = self._trash_duplicates(plan, summary)
        self._record_phase("duplicates", started, summary)
        if self._third_party_policy.mutation_requested:
            started = time.perf_counter_ns()
            summary = self._trash_third_party_code(plan, summary)
            self._record_phase("third-party-code", started, summary)
        # Third-party effects must precede content-type candidate publication;
        # otherwise route_candidates could retain a path already moved to
        # Trash and a later route would read a stale source identity.
        started = time.perf_counter_ns()
        summary = self._validate_extensions(plan, summary)
        self._record_phase("content-types", started, summary)
        self._publish_preservation_summary(
            "execute",
            admission_policy=(
                None if self._admission_policy is None else self._admission_policy.to_dict()
            ),
            summary=summary,
        )
        if cleanup_empty_directories:
            started = time.perf_counter_ns()
            summary = self._trash_empty_directories(plan, summary)
            self._record_phase("empty-directories", started, summary)
        self._state.store_action_summary(self._run_id, summary)
        return summary

    def _admission_checkpoint(self) -> None:
        if self._cancellation_check is not None:
            self._cancellation_check()

    @staticmethod
    def _preservation_reason_code(reason: str) -> str:
        value = str(reason).casefold()
        if "credential" in value or "secret" in value or "private" in value:
            return "credential"
        if "fixture" in value or "test_data" in value or "testdata" in value:
            return "fixture"
        if "license" in value or "licence" in value or "notice" in value or "legal" in value:
            return "legal_metadata"
        if "witness" in value or "regenerat" in value or "archive" in value:
            return "retained_witness"
        if "scope" in value or "outside" in value:
            return "out_of_scope"
        if "identity" in value or "changed" in value or "prefix" in value:
            return "identity_drift"
        if "protected" in value or "reparse" in value or "system" in value:
            return "protected_path"
        return "preservation_veto"

    def _record_preservation_veto(
        self,
        action_type: str,
        path: str | Path,
        reason: str,
        snapshot: FileSnapshot | None = None,
    ) -> None:
        """Keep bounded, non-payload evidence for a pre-ledger veto."""

        code = self._preservation_reason_code(reason)
        self._preservation_reasons[code] = self._preservation_reasons.get(code, 0) + 1
        if len(self._preservation_examples) >= MAX_PRESERVATION_EXAMPLES:
            return
        raw_path = os.fspath(path)
        self._preservation_examples.append(
            {
                "action_type": action_type,
                "reason": code,
                "path_digest": hashlib.sha256(
                    raw_path.encode("utf-8", "surrogatepass")
                ).hexdigest(),
                "size": None if snapshot is None else int(snapshot.size),
            }
        )

    def _preservation_summary_payload(
        self,
        operation: str,
        *,
        admission_policy: dict[str, object] | None,
        summary: ActionSummary | None,
    ) -> dict[str, object]:
        return {
            "schema": "neocortex.corpus-admission-summary/v1",
            "preservation_schema": "neocortex.corpus-preservation/v1",
            "operation": operation,
            "policy": admission_policy,
            "processed": 0 if summary is None else summary.admission_processed,
            "metadata_only": 0 if summary is None else summary.admission_metadata_only,
            "sensitive": 0 if summary is None else summary.admission_sensitive,
            "reasons": dict(sorted(self._admission_reasons.items())),
            "examples": list(self._admission_examples),
            "examples_limit": MAX_PRESERVATION_EXAMPLES,
            "admission_policy_present": admission_policy is not None,
            "veto_total": sum(self._preservation_reasons.values()),
            "veto_reasons": dict(sorted(self._preservation_reasons.items())),
            "preservation_examples": list(self._preservation_examples),
            "preservation_examples_limit": MAX_PRESERVATION_EXAMPLES,
            "file_actions_created_for_vetoes": 0,
            "excluded_files_deleted": False,
            "regeneration_proven": 0 if summary is None else summary.regeneration_proven,
            "regeneration_unproven": 0 if summary is None else summary.regeneration_unproven,
            "regeneration_action_limit_reached": (
                False if summary is None else summary.regeneration_action_limit_reached
            ),
            "regeneration_sources_truncated": (
                False if summary is None else summary.regeneration_sources_truncated
            ),
            "complete_coverage": summary is None or not (
                summary.regeneration_action_limit_reached
                or summary.regeneration_sources_truncated
            ),
        }

    def _publish_preservation_summary(
        self,
        operation: str,
        *,
        admission_policy: dict[str, object] | None = None,
        summary: ActionSummary | None = None,
    ) -> None:
        payload = self._preservation_summary_payload(
            operation,
            admission_policy=admission_policy,
            summary=summary,
        )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idempotency_key = "corpus-preservation:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        publish_stage = getattr(self._state, "publish_run_stage", None)
        read_manifest = getattr(self._state, "read_run_manifest", None)
        if callable(publish_stage) and callable(read_manifest) and read_manifest(self._run_id) is not None:
            publish_stage(
                self._run_id,
                "corpus-admission",
                "completed",
                details=payload,
                idempotency_key=idempotency_key,
            )
        else:
            self._state.record_event(
                self._run_id,
                "info",
                "corpus-admission",
                "Vetoes de preservación registrados",
                payload,
            )

    @staticmethod
    def _work_snapshot_payload(snapshot: FileSnapshot | None) -> tuple[object, ...] | None:
        if snapshot is None:
            return None
        return (
            snapshot.path,
            snapshot.volume_id,
            snapshot.file_id,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
        )

    def _reserve_snapshot_work(
        self,
        scope: str,
        snapshots: Iterable[FileSnapshot | None],
        *,
        references: Iterable[FileSnapshot | None] = (),
        items: int | None = None,
        bytes_multiplier: int = 1,
        extra_bytes: int = 0,
        bytes_override: int | None = None,
    ) -> None:
        """Reserve one bounded action batch before reading candidate payloads.

        The callback belongs to the orchestration owner.  It receives a
        conservative input/read bound, not a claim about exact physical I/O.
        Keeping the reservation at batch scope avoids a durable event per
        chunk or per file while still placing the budget gate before prefix,
        digest, and keeper comparisons.
        """

        if self._reserve_work is None:
            return
        selected = tuple(snapshots)
        retained = tuple(references)
        if items is None:
            items = len(selected)
        if type(items) is not int or items < 0:
            raise ValueError("reserved action items must be a non-negative integer")
        if type(bytes_multiplier) is not int or bytes_multiplier < 1:
            raise ValueError("reserved action multiplier must be a positive integer")
        if type(extra_bytes) is not int or extra_bytes < 0:
            raise ValueError("reserved action bytes must be non-negative")
        if bytes_override is not None:
            if type(bytes_override) is not int or bytes_override < 0:
                raise ValueError("reserved action bytes must be non-negative")
            byte_count = bytes_override
        else:
            byte_count = sum(
                max(0, int(snapshot.size))
                for snapshot in (*selected, *retained)
                if snapshot is not None
            )
            byte_count = byte_count * bytes_multiplier + extra_bytes
        payload = {
            "run_id": self._run_id,
            "scope": scope,
            "items": items,
            "snapshots": [self._work_snapshot_payload(item) for item in selected],
            "references": [self._work_snapshot_payload(item) for item in retained],
            "bytes_multiplier": bytes_multiplier,
            "extra_bytes": extra_bytes,
            "bytes_override": bytes_override,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        self._reserve_work(f"actions-work-v1:{scope}:{digest}", items, byte_count)

    def _effect_preservation_reason(self, snapshot: FileSnapshot) -> str | None:
        """Retention veto shared by dedupe, direct callers and regeneration.

        Metadata-only is not itself a veto: a proved regenerable artifact is
        deliberately metadata-only. Credentials, originals used as witnesses,
        licences and fixtures are separate protected categories.
        """
        path = Path(snapshot.path)
        if _path_key(path) in getattr(self, "_retained_regeneration_sources", {}):
            return "retained_regeneration_witness"
        if _is_third_party_metadata_name(snapshot.path):
            return "legal_metadata"
        if path.suffix.lower() in {".whl", ".nupkg"}:
            return "retained_package_archive"
        decision = assess_file(
            snapshot,
            root=Path(self._index.scan_root(self._scan_id)),
            policy=self._preservation_policy,
            cancellation_check=self._cancellation_check,
        )
        if decision.disposition == "sensitive" or decision.category in {
            "credential", "fixture", "preserved_artifact", "retained_archive",
        }:
            return f"{decision.category}:{decision.reason}"
        return None

    def _check_regeneration_at_effect(self, action_type: str, path: str) -> None:
        if action_type != "trash_third_party_code":
            return
        from neocortex.workflow.actions.regeneration import revalidate_regeneration_proof

        self._admission_checkpoint()
        proof = self._regeneration_proofs.get(path)
        if proof is None or not revalidate_regeneration_proof(
            proof,
            root=Path(self._index.scan_root(self._scan_id)),
            cancellation_check=self._cancellation_check,
        ):
            raise RuntimeError("regeneration evidence changed or is unavailable before trash")

    def _regeneration_source_paths(self) -> tuple[Path, ...]:
        """Select bounded local source archives through the already-open owner."""

        from neocortex.workflow.actions.regeneration import MAX_ARCHIVE_PATHS

        sources: list[Path] = []
        after_path = ""
        while True:
            self._admission_checkpoint()
            page = self._index.snapshots_page(
                self._scan_id, after_path=after_path, limit=TRASH_BATCH_SIZE,
            )
            if not page:
                break
            after_path = page[-1].path
            for snapshot in page:
                if Path(snapshot.path).suffix.lower() in {".whl", ".nupkg", ".tgz"}:
                    if len(sources) == MAX_ARCHIVE_PATHS:
                        self._regeneration_sources_truncated = True
                        return tuple(sources)
                    sources.append(Path(snapshot.path))
        return tuple(sources)

    @staticmethod
    def _local_source_snapshots(paths: Iterable[str | Path]) -> tuple[FileSnapshot, ...]:
        """Capture unique regular local inputs with lstat/no-follow only."""

        snapshots: list[FileSnapshot] = []
        identities: set[tuple[int, int]] = set()
        for raw_path in paths:
            path = Path(raw_path).absolute()
            try:
                metadata = os.lstat(path)
            except OSError:
                continue
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or int(getattr(metadata, "st_nlink", 1)) != 1
            ):
                continue
            identity = (int(metadata.st_dev), int(metadata.st_ino))
            if identity in identities:
                continue
            identities.add(identity)
            snapshots.append(
                FileSnapshot(
                    str(path),
                    identity[0],
                    identity[1],
                    int(metadata.st_size),
                    int(metadata.st_mtime_ns),
                    int(stat_birthtime_ns(metadata)),
                )
            )
        return tuple(snapshots)

    def recycle_verified_files(
        self,
        action_type: str,
        candidates: Iterable[tuple[FileSnapshot, str]],
    ) -> tuple[int, int, int]:
        """Recycle snapshot-verified files in bounded, durably recorded batches."""

        if not action_type.startswith("trash_"):
            raise ValueError("recycle action types must start with 'trash_'")
        self._preservation_reasons.clear()
        self._preservation_examples.clear()
        applied = failed = protected = 0
        batch: list[tuple[FileSnapshot, str]] = []

        def flush() -> None:
            nonlocal applied, failed, protected
            if not batch:
                return
            result = self._apply_trash_batch(
                action_type,
                tuple((snapshot.path, evidence) for snapshot, evidence in batch),
                expected_snapshots=tuple(snapshot for snapshot, _evidence in batch),
            )
            applied += result[0]
            failed += result[1]
            protected += result[2]
            batch.clear()

        for candidate in candidates:
            batch.append(candidate)
            if len(batch) >= TRASH_BATCH_SIZE:
                flush()
        flush()
        self._publish_preservation_summary(f"recycle:{action_type}")
        return applied, failed, protected

    def _record_phase(self, phase: str, started_ns: int, summary: ActionSummary) -> None:
        self._state.record_event(
            self._run_id,
            "info",
            phase,
            "Fase de acciones completada",
            {
                "elapsed_ns": time.perf_counter_ns() - started_ns,
                "files_checked": summary.files_checked,
                "type_cache_hits": summary.type_cache_hits,
                "type_cache_misses": summary.type_cache_misses,
                "stale_inventory": summary.stale_inventory,
                "errors": summary.errors,
                "third_party_candidates": summary.third_party_candidates,
                "third_party_trashed": summary.third_party_trashed,
                "third_party_skips": summary.third_party_skips,
            },
        )

    def cleanup_empty_directories(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        """Run final directory cleanup after an optional content route."""

        self._validate_apply_root()
        started = time.perf_counter_ns()
        result = self._trash_empty_directories(plan, summary)
        self._record_phase("empty-directories", started, result)
        self._state.store_action_summary(self._run_id, result)
        return result

    def _trash_empty_directories(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        root = self._index.scan_root(plan.scan_id)
        traversal_error_count = [0]
        pending: list[tuple[str, str, FileSnapshot]] = []
        logical_child_counts: dict[str, int] = {}
        candidates = applied_total = failed_total = protected_total = completed = 0
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "empty-directories",
                "Buscando directorios vacíos",
                0,
                unit="directorios",
            ),
        )

        def flush() -> None:
            nonlocal applied_total, failed_total, protected_total, completed
            if not pending:
                return
            batch = tuple((path, evidence) for path, evidence, _snapshot in pending)
            expected = tuple(snapshot for _path, _evidence, snapshot in pending)
            applied, failed, protected = self._apply_trash_batch(
                "trash_empty_directory", batch, expected_snapshots=expected
            )
            applied_total += applied
            failed_total += failed
            protected_total += protected
            completed += len(batch)
            if self._apply:
                for path, _evidence, _snapshot in pending:
                    if os.path.lexists(path):
                        continue
                    parent_key = _path_key(Path(path).parent)
                    remaining = logical_child_counts.get(parent_key, 0) - 1
                    if remaining > 0:
                        logical_child_counts[parent_key] = remaining
                    else:
                        logical_child_counts.pop(parent_key, None)
            pending.clear()
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "empty-directories",
                    "Enviando directorios vacíos",
                    completed,
                    unit="directorios",
                ),
            )

        for directory in _postorder_directories(
            root, self._exclusion_policy, traversal_error_count
        ):
            directory_snapshot = self._empty_directory_snapshot(
                directory,
                logical_child_counts,
                traversal_error_count,
                flush,
            )
            if directory_snapshot is None:
                continue
            path = str(directory)
            pending.append((path, "directory-empty;policy=trash", directory_snapshot))
            parent_key = _path_key(directory.parent)
            logical_child_counts[parent_key] = logical_child_counts.get(parent_key, 0) + 1
            candidates += 1
            if len(pending) >= TRASH_BATCH_SIZE:
                flush()
        flush()
        summary = replace(
            summary,
            empty_directory_candidates=candidates,
            empty_directories_trashed=applied_total,
            empty_directory_skips=(failed_total + protected_total + traversal_error_count[0]),
            errors=summary.errors + failed_total + traversal_error_count[0],
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "empty-directories",
                "Directorios vacíos procesados",
                candidates,
                candidates,
                "directorios",
                True,
            ),
        )
        return summary

    def _empty_directory_snapshot(
        self,
        directory: Path,
        logical_child_counts: dict[str, int],
        traversal_error_count: list[int],
        flush_pending: Callable[[], None],
    ) -> FileSnapshot | None:
        entry_count = self._directory_entry_count(
            directory,
            traversal_error_count,
        )
        if entry_count is None:
            return None
        scheduled_children = logical_child_counts.pop(_path_key(directory), 0)
        if entry_count != scheduled_children:
            return None
        if self._apply and scheduled_children:
            # A parent is admitted only after its planned children have been
            # applied and a new physical-empty observation succeeds.
            flush_pending()
            if (
                self._directory_entry_count(
                    directory,
                    traversal_error_count,
                    missing_is_error=True,
                )
                != 0
            ):
                return None
        try:
            return snapshot_path(directory)
        except OSError:
            traversal_error_count[0] += 1
            return None

    @staticmethod
    def _directory_entry_count(
        directory: Path,
        traversal_error_count: list[int],
        *,
        missing_is_error: bool = False,
    ) -> int | None:
        count = 0
        try:
            with os.scandir(directory) as entries:
                for _entry in entries:
                    count += 1
        except FileNotFoundError:
            if missing_is_error:
                traversal_error_count[0] += 1
            return None
        except OSError:
            traversal_error_count[0] += 1
            return None
        return count

    def _apply_trash_batch(
        self,
        action_type: str,
        batch: tuple[tuple[str, str], ...],
        *,
        expected_snapshots: tuple[FileSnapshot | None, ...] | None = None,
        reference_snapshots: tuple[FileSnapshot | None, ...] | None = None,
        defer_reconciliation: bool = False,
    ) -> tuple[int, int, int]:
        """Apply one bounded batch and isolate partial Recycle Bin failures."""

        # The empty-file phase is plan-independent but runs before the
        # duplicate-plan generator.  Always defer its successor publication so
        # that ``plan.scan_id`` still resolves to the generation containing the
        # persisted duplicate groups.  Keep the keyword optional for older
        # diagnostic wrappers that forward this private method.
        defer_reconciliation = defer_reconciliation or action_type == "trash_empty_file"
        mutation_guard = self._effective_mutation_guard()
        validated_root = self._validate_apply_root(mutation_guard=mutation_guard)
        expected, references = self._normalize_trash_snapshots(
            batch,
            expected_snapshots,
            reference_snapshots,
        )
        # Reserve before preservation-prefix reads and before any digest or
        # exact keeper comparison.  The bound covers the candidate and its
        # reference once; callers may use a stricter owner-level multiplier.
        if not (
            action_type == "trash_duplicate"
            and getattr(self, "_duplicate_work_reserved", False)
        ):
            self._reserve_snapshot_work(
                f"{action_type}:batch",
                expected,
                references=references,
            )
        eligible, protected = self._begin_trash_candidates(
            action_type,
            batch,
            expected,
            references,
            mutation_guard=mutation_guard,
        )
        if not self._apply:
            self._state.finish_file_actions(
                (candidate[0] for candidate in eligible),
                "planned",
            )
            return 0, 0, protected
        active, preflight_failures = self._preflight_trash_candidates(
            action_type,
            eligible,
            validated_root=validated_root,
        )
        if not active:
            return 0, preflight_failures, protected
        # Revalidate the immutable guard once for the whole batch at the
        # mutation frontier.  Candidate identity remains a per-path check: a
        # component may have been substituted after the admission pass even
        # when the corpus root and policy objects themselves are unchanged.
        mutation_guard.require_paths_allowed(*(candidate[1] for candidate in active))
        mutation_root = self._validate_apply_root(mutation_guard=mutation_guard)
        if mutation_root is None:
            raise RuntimeError("apply mutation root is unavailable")
        ready, revalidation_failures = self._revalidate_trash_candidates(
            action_type,
            active,
            validated_root=mutation_root,
        )
        preflight_failures += revalidation_failures
        if not ready:
            return 0, preflight_failures, protected
        if self._trash_backend is None or action_type == "trash_empty_directory":
            # Directory trash remains deliberately outside the KIO adapter.
            # The injected backend is an explicit opt-in; ordinary framework
            # runs preserve their historical fail-closed behavior.
            detail = (
                TRASH_IDENTITY_ABSTENTION
                if self._trash_backend is None
                else "KIO directory trash is unsupported; only regular files are supported"
            )
            self._state.finish_file_actions(
                (candidate[0] for candidate in ready),
                "skipped",
                detail,
            )
            return 0, preflight_failures, protected + len(ready)

        batch_apply = self._optional_trash_batch_backend()
        if batch_apply is not None:
            return self._apply_trash_backend_batch(
                action_type,
                ready,
                mutation_root=mutation_root,
                failed=preflight_failures,
                protected=protected,
                apply_batch=batch_apply,
                defer_reconciliation=defer_reconciliation,
            )

        applied = 0
        failed = preflight_failures
        applied_paths: list[str] = []
        for action_id, path, planned, reference, _current_stat in ready:
            if planned is None:
                self._state.finish_file_action(
                    action_id,
                    "failed",
                    "trash candidate has no expected snapshot",
                )
                failed += 1
                continue
            try:
                source_digest = f"{FULL_ALGORITHM}:" + full_fingerprint(planned).hex()
                if reference is not None:
                    if not files_equal_exact(planned, reference):
                        raise RuntimeError("keeper changed during exact duplicate comparison")
                expected_json = expected_identity_json(
                    planned,
                    source_path=path,
                    target_path=None,
                )
                self._state.mark_file_actions_applying(((action_id, expected_json),))
                self._check_regeneration_at_effect(action_type, path)
                apply_snapshot = getattr(self._trash_backend, "apply_snapshot", None)
                if callable(apply_snapshot):
                    outcome = apply_snapshot(
                        planned,
                        root=mutation_root,
                        source_digest=source_digest,
                    )
                else:
                    # Compatibility seam for older injected backends that
                    # implement the grant-style ``apply(candidate)`` only.
                    apply_effect = SimpleNamespace(
                        action="trash",
                        source=planned,
                        source_digest=source_digest,
                        keeper=None,
                        keeper_digest=None,
                        target_path=None,
                    )
                    apply_method = getattr(self._trash_backend, "apply", None)
                    if not callable(apply_method):
                        raise RuntimeError("trash backend lacks apply_snapshot(candidate)")
                    outcome = apply_method(
                        SimpleNamespace(effect=apply_effect, root=mutation_root)
                    )
                if not isinstance(outcome, BackendOutcome):
                    raise RuntimeError("trash backend returned an unsupported outcome")
                if outcome.status == "applied" and outcome.receipt_json is not None:
                    self._state.confirm_file_actions_applied(
                        ((action_id, outcome.receipt_json),)
                    )
                    applied += 1
                    applied_paths.append(path)
                    continue
                detail = outcome.detail or outcome.reason
                if outcome.status == "recovery_required":
                    self._state.require_file_action_recovery((action_id,), detail)
                    failed += 1
                else:
                    self._state.finish_file_action(action_id, "failed", detail)
                    failed += 1
            except (CancellationRequested, RunBudgetExceeded, KeyboardInterrupt) as exc:
                self._best_effort_require_recovery((action_id,), str(exc), exc)
                raise
            except (OSError, RuntimeError, FileChangedError, ValueError) as exc:
                # A failure for one member must not suppress independent
                # candidates in the same bounded batch.
                try:
                    row = self._state._connection.execute(
                        "SELECT status FROM file_actions WHERE action_id=?",
                        (action_id,),
                    ).fetchone()
                    if row is not None and str(row[0]) == "applying":
                        self._state.require_file_action_recovery((action_id,), str(exc))
                    elif row is not None and str(row[0]) == "started":
                        self._state.finish_file_action(action_id, "failed", str(exc))
                except BaseException as persistence_error:
                    exc.add_note(f"file action transition failed: {persistence_error}")
                failed += 1
        if applied_paths:
            if defer_reconciliation:
                self._deferred_reconciliation_paths.extend(applied_paths)
            else:
                self._index.apply_reconciliation(
                    self._scan_id,
                    remove_paths=tuple(applied_paths),
                )
        return applied, failed, protected

    def _optional_trash_batch_backend(self) -> Callable[..., object] | None:
        """Return the optional multi-snapshot seam without probing ``__getattr__``.

        ``KioTrashBackend`` exposes ``apply_many_snapshots`` (and a compatibility
        alias) only when the batch safety adapter is available.  Looking at
        ``dir`` first is intentional: an unconfigured ``MagicMock`` fabricates
        arbitrary attributes, and must continue through the individual fixture
        seam instead of being mistaken for a batch backend.
        """

        backend = self._trash_backend
        if backend is None:
            return None
        for name in ("apply_many_snapshots", "apply_snapshot_batch", "apply_batch"):
            if name not in dir(backend):
                continue
            candidate = getattr(backend, name, None)
            if callable(candidate):
                return candidate
        return None

    def _apply_trash_backend_batch(
        self,
        action_type: str,
        ready: list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ],
        *,
        mutation_root: Path,
        failed: int,
        protected: int,
        apply_batch: Callable[..., object],
        defer_reconciliation: bool,
    ) -> tuple[int, int, int]:
        """Run one optional backend batch while retaining per-item ledger rows."""

        # Compute each digest and exact-keeper check before crossing any
        # ``applying`` frontier.  One stale member is failed independently;
        # unrelated members can still use the same physical batch.
        prepared: list[tuple[int, str, FileSnapshot, str, str]] = []
        for action_id, path, planned, reference, _current_stat in ready:
            if planned is None:
                self._state.finish_file_action(
                    action_id,
                    "failed",
                    "trash candidate has no expected snapshot",
                )
                failed += 1
                continue
            try:
                source_digest = f"{FULL_ALGORITHM}:" + full_fingerprint(planned).hex()
                if reference is not None and not files_equal_exact(planned, reference):
                    raise RuntimeError("keeper changed during exact duplicate comparison")
                expected_json = expected_identity_json(
                    planned,
                    source_path=path,
                    target_path=None,
                )
            except (OSError, RuntimeError, FileChangedError, ValueError) as exc:
                self._state.finish_file_action(action_id, "failed", str(exc))
                failed += 1
                continue
            prepared.append((action_id, path, planned, source_digest, expected_json))

        if not prepared:
            return 0, failed, protected

        # The state writer owns this transition.  It is one transaction for the
        # batch, but every action receives its own expected identity and event.
        self._state.mark_file_actions_applying(
            (action_id, expected_json) for action_id, _path, _snapshot, _digest, expected_json in prepared
        )

        try:
            for _id, path, _snapshot, _digest, _expected in prepared:
                self._check_regeneration_at_effect(action_type, path)
            batch_result = apply_batch(
                tuple((snapshot, source_digest) for _id, _path, snapshot, source_digest, _expected in prepared),
                root=mutation_root,
            )
            outcomes_value = (
                batch_result
                if isinstance(batch_result, (tuple, list))
                else getattr(batch_result, "outcomes", None)
            )
            if outcomes_value is None:
                raise RuntimeError("trash backend returned no batch outcomes")
            outcomes = tuple(outcomes_value)
            if len(outcomes) != len(prepared):
                raise RuntimeError(
                    "trash backend returned an outcome count different from the batch"
                )
        except RunBudgetExceeded as exc:
            for action_id, _path, _snapshot, _digest, _expected in prepared:
                self._best_effort_require_recovery((action_id,), str(exc), exc)
            raise
        except (OSError, RuntimeError, FileChangedError, ValueError, TypeError) as exc:
            # A batch process may have crossed its physical frontier before an
            # exception reached this owner.  Never retry it as individual work;
            # preserve one recovery row for every member instead.
            detail = str(exc) or "trash backend batch outcome is unavailable"
            for action_id, _path, _snapshot, _digest, _expected in prepared:
                self._best_effort_require_recovery((action_id,), detail, exc)
            return 0, failed + len(prepared), protected
        except BaseException as exc:
            # KeyboardInterrupt/SystemExit or an unexpected backend failure
            # may arrive after the shared physical frontier.  Preserve every
            # applying row before re-raising the control-flow interruption;
            # never retry the batch as individual operations.
            detail = str(exc) or "trash backend batch operation was interrupted"
            for action_id, _path, _snapshot, _digest, _expected in prepared:
                self._best_effort_require_recovery((action_id,), detail, exc)
            raise

        applied_paths: list[str] = []
        applied = 0
        confirmations: list[tuple[int, str, str]] = []
        for (
            action_id,
            path,
            _snapshot,
            _source_digest,
            _expected,
        ), outcome in zip(prepared, outcomes, strict=True):
            if not isinstance(outcome, BackendOutcome):
                detail = "trash backend returned an unsupported batch outcome"
                self._best_effort_require_recovery((action_id,), detail, RuntimeError(detail))
                failed += 1
                continue
            if outcome.status == "applied":
                if outcome.receipt_json is None:
                    detail = "trash backend reported applied without a receipt"
                    self._best_effort_require_recovery(
                        (action_id,), detail, RuntimeError(detail)
                    )
                    failed += 1
                    continue
                try:
                    receipt_value = json.loads(outcome.receipt_json)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._best_effort_require_recovery((action_id,), str(exc), exc)
                    failed += 1
                    continue
                if not isinstance(receipt_value, dict):
                    detail = "trash backend returned a non-object effect receipt"
                    self._best_effort_require_recovery(
                        (action_id,), detail, RuntimeError(detail)
                    )
                    failed += 1
                    continue
                confirmations.append((action_id, outcome.receipt_json, path))
                continue

            detail = outcome.detail or outcome.reason
            # All members entered ``applying`` before the shared call.  The
            # state contract therefore cannot safely transition one member
            # back to ``failed`` after the frontier; retain recovery even when
            # the backend reports a per-item block, because the batch may have
            # crossed the physical frontier for another member.
            self._best_effort_require_recovery(
                (action_id,), detail, RuntimeError(detail)
            )
            failed += 1

        if confirmations:
            try:
                self._state.confirm_file_actions_applied(
                    (action_id, receipt) for action_id, receipt, _path in confirmations
                )
            except (OSError, RuntimeError, ValueError) as exc:
                # The physical results were classified as applied, but an
                # atomic ledger confirmation failed.  Preserve recovery for
                # every member and do not reconcile an unconfirmed path.
                for action_id, _receipt, _path in confirmations:
                    self._best_effort_require_recovery((action_id,), str(exc), exc)
                failed += len(confirmations)
            else:
                applied = len(confirmations)
                applied_paths.extend(path for _action_id, _receipt, path in confirmations)

        # Reconciliation is deliberately after all receipts have crossed the
        # durable confirmation frontier, and exactly once for this batch.
        if applied_paths:
            if defer_reconciliation:
                self._deferred_reconciliation_paths.extend(applied_paths)
            else:
                self._index.apply_reconciliation(
                    self._scan_id,
                    remove_paths=tuple(applied_paths),
                )
        return applied, failed, protected

    def _best_effort_require_recovery(
        self,
        action_ids: Iterable[int],
        detail: str,
        original_error: BaseException,
    ) -> None:
        try:
            self._state.require_file_action_recovery(action_ids, detail)
        except BaseException as persistence_error:
            original_error.add_note(
                "file action remains in applying state because recovery marking "
                f"failed: {type(persistence_error).__name__}: {persistence_error}"
            )

    @staticmethod
    def _normalize_trash_snapshots(
        batch: tuple[tuple[str, str], ...],
        expected_snapshots: tuple[FileSnapshot | None, ...] | None,
        reference_snapshots: tuple[FileSnapshot | None, ...] | None,
    ) -> tuple[
        tuple[FileSnapshot | None, ...],
        tuple[FileSnapshot | None, ...],
    ]:
        expected = (None,) * len(batch) if expected_snapshots is None else expected_snapshots
        if len(expected) != len(batch):
            raise ValueError("expected snapshot count does not match trash batch")
        references = (None,) * len(batch) if reference_snapshots is None else reference_snapshots
        if len(references) != len(batch):
            raise ValueError("reference snapshot count does not match trash batch")
        return expected, references

    def _begin_trash_candidates(
        self,
        action_type: str,
        batch: tuple[tuple[str, str], ...],
        expected: tuple[FileSnapshot | None, ...],
        references: tuple[FileSnapshot | None, ...],
        *,
        mutation_guard: CorpusMutationGuard | None = None,
    ) -> tuple[
        list[tuple[int, str, FileSnapshot | None, FileSnapshot | None]],
        int,
    ]:
        evaluated: list[
            tuple[
                tuple[str, str],
                FileSnapshot | None,
                FileSnapshot | None,
                str | None,
            ]
        ] = []
        filtered_protected = 0
        mutation_guard = mutation_guard or self._effective_mutation_guard()
        guard_paths = tuple(
            path for path, _evidence in batch if _protected_path_reason(path) is None
        )
        guard_reasons = iter(mutation_guard.mutation_path_protection_reasons(*guard_paths))
        for item, planned, reference in zip(batch, expected, references, strict=True):
            path, _evidence = item
            reason = _protected_path_reason(path)
            guard_reason = None if reason is not None else next(guard_reasons)
            retention_reason = (
                None
                if planned is None or action_type == "trash_empty_directory"
                else self._effect_preservation_reason(planned)
            )
            if reason is None:
                if guard_reason is not None:
                    self._record_preservation_veto(
                        action_type, path, str(guard_reason), planned
                    )
                    filtered_protected += 1
                    continue
            else:
                self._record_preservation_veto(action_type, path, reason, planned)
                # Legacy action-policy denials keep their existing skipped
                # ledger row for compatibility; corpus-preservation vetoes
                # and mutation-guard denials remain outside the action domain.
                evaluated.append((item, planned, reference, reason))
                continue
            if retention_reason is not None:
                self._record_preservation_veto(
                    action_type, path, retention_reason, planned
                )
                filtered_protected += 1
                continue
            evaluated.append((item, planned, reference, None))
        if not evaluated:
            return [], filtered_protected

        action_ids = self._state.begin_file_actions(
            self._run_id,
            (
                (
                    action_type,
                    path,
                    None,
                    "application/octet-stream",
                    evidence,
                    self._apply,
                )
                for (path, evidence), _planned, _reference, _reason in evaluated
            ),
        )
        eligible: list[tuple[int, str, FileSnapshot | None, FileSnapshot | None]] = []
        protected_by_reason: dict[str, list[int]] = {}
        for action_id, ((path, _evidence), planned, reference, reason) in zip(
            action_ids, evaluated, strict=True
        ):
            if reason is None:
                eligible.append((action_id, path, planned, reference))
            else:
                protected_by_reason.setdefault(reason, []).append(action_id)
        for reason, protected_ids in protected_by_reason.items():
            self._state.finish_file_actions(protected_ids, "skipped", reason)
        protected = filtered_protected + sum(
            len(action_ids) for action_ids in protected_by_reason.values()
        )
        return eligible, protected

    def _preflight_trash_candidates(
        self,
        action_type: str,
        eligible: list[tuple[int, str, FileSnapshot | None, FileSnapshot | None]],
        *,
        validated_root: Path | None = None,
    ) -> tuple[
        list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ],
        int,
    ]:
        active: list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ] = []
        failures = 0
        for action_id, path, planned, reference in eligible:
            try:
                current_stat = self._validate_trash_candidate(
                    action_type,
                    path,
                    planned,
                    reference,
                    validated_root=validated_root,
                )
            except (InternalPathProtectionError, ProtectedAnalysisRootError):
                raise
            except (OSError, RuntimeError) as exc:
                self._state.finish_file_action(action_id, "failed", str(exc))
                failures += 1
                continue
            active.append((action_id, path, planned, reference, current_stat))
        return active, failures

    def _revalidate_trash_candidates(
        self,
        action_type: str,
        active: list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ],
        *,
        validated_root: Path | None = None,
    ) -> tuple[
        list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ],
        int,
    ]:
        # The first pass admits candidates independently.  This second pass is
        # deliberately adjacent to the mutating call so a component replaced
        # after preflight cannot make an otherwise-safe batch cross its root.
        ready: list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ] = []
        failures = 0
        for action_id, path, planned, reference, original_stat in active:
            try:
                current_stat = self._validate_trash_candidate(
                    action_type,
                    path,
                    planned,
                    reference,
                    original_stat=original_stat,
                    validated_root=validated_root,
                )
            except (InternalPathProtectionError, ProtectedAnalysisRootError):
                raise
            except (OSError, RuntimeError) as exc:
                self._state.finish_file_action(action_id, "failed", str(exc))
                failures += 1
                continue
            ready.append((action_id, path, planned, reference, current_stat))
        return ready, failures

    def _validate_trash_candidate(
        self,
        action_type: str,
        path: str,
        planned: FileSnapshot | None,
        reference: FileSnapshot | None,
        *,
        original_stat: os.stat_result | None = None,
        validated_root: Path | None = None,
    ) -> os.stat_result:
        """Revalidate one source and its keeper without following reparses."""

        current_stat = (
            self._validate_action_path(path, role="trash source")
            if validated_root is None
            else _validate_mutation_path(validated_root, path, role="trash source")
        )
        if current_stat is None:
            self._record_preservation_veto(
                action_type, path, "identity_or_prefix_unverified", planned
            )
            raise RuntimeError("trash source disappeared before the operation")
        if planned is not None and not stat_matches_snapshot(planned, current_stat):
            self._record_preservation_veto(
                action_type, path, "identity_changed_before_effect", planned
            )
            raise RuntimeError("metadata changed after the trash candidate was planned")
        if original_stat is not None and not self._same_runtime_stat(original_stat, current_stat):
            self._record_preservation_veto(
                action_type, path, "identity_changed_after_preflight", planned
            )
            raise RuntimeError("trash source changed after mutation preflight")
        if planned is not None and action_type != "trash_empty_directory":
            retention_reason = self._effect_preservation_reason(planned)
            if retention_reason is not None:
                self._record_preservation_veto(
                    action_type, path, retention_reason, planned
                )
                raise RuntimeError(f"trash source is retained: {retention_reason}")
        if action_type == "trash_empty_directory":
            if planned is None:
                raise RuntimeError("empty-directory action has no expected snapshot")
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    raise RuntimeError("directory is no longer physically empty")
        if reference is not None:
            reference_stat = (
                self._validate_observation_path(
                    reference.path,
                    role="trash keeper/reference",
                )
                if validated_root is None
                else _validate_mutation_path(
                    validated_root,
                    reference.path,
                    role="trash keeper/reference",
                )
            )
            if reference_stat is None or not stat_matches_snapshot(reference, reference_stat):
                raise RuntimeError("keeper changed after exact duplicate comparison")
        return current_stat

    def _validate_action_path(
        self,
        path: str | Path,
        *,
        role: str,
        allow_missing_leaf: bool = False,
    ) -> os.stat_result | None:
        mutation_guard = self._effective_mutation_guard()
        mutation_guard.require_paths_allowed(path)
        root = self._validate_apply_root()
        if root is None:
            return None
        return _validate_mutation_path(
            root,
            path,
            role=role,
            allow_missing_leaf=allow_missing_leaf,
        )

    def _validate_observation_path(
        self,
        path: str | Path,
        *,
        role: str,
    ) -> os.stat_result | None:
        """Validate a corpus reference without treating it as a mutation target."""

        root = self._validate_apply_root()
        if root is None:
            return None
        return _validate_mutation_path(root, path, role=role)

    @staticmethod
    def _same_runtime_stat(
        original: os.stat_result,
        current: os.stat_result,
    ) -> bool:
        identity = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
        )
        if any(getattr(original, name) != getattr(current, name) for name in identity):
            return False
        original_birthtime = stat_birthtime_ns(original)
        current_birthtime = stat_birthtime_ns(current)
        return bool(original_birthtime == current_birthtime)

    def _trash_empty_files(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        candidates = self._index.file_count_by_size(plan.scan_id, 0)
        if not candidates:
            return summary
        skips_before_phase = summary.duplicate_skips
        summary = replace(
            summary,
            duplicate_candidates=summary.duplicate_candidates + candidates,
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "empty-files",
                "Enviando archivos vacíos",
                0,
                candidates,
                "archivos",
            ),
        )
        pending: list[tuple[str, str, FileSnapshot]] = []
        completed = 0
        after_path = ""
        while True:
            page = self._index.snapshots_by_size_page(
                plan.scan_id,
                0,
                after_path=after_path,
                limit=TRASH_BATCH_SIZE,
            )
            if not page:
                break
            protected = 0
            for snapshot in page:
                if _is_third_party_metadata_name(snapshot.path):
                    protected += 1
                    continue
                pending.append((snapshot.path, "size=0;policy=trash-all-empty", snapshot))
            after_path = page[-1].path
            applied = failed = batch_protected = 0
            if pending:
                applied, failed, batch_protected = self._apply_trash_batch(
                    "trash_empty_file",
                    tuple((path, evidence) for path, evidence, _snapshot in pending),
                    expected_snapshots=tuple(snapshot for _path, _evidence, snapshot in pending),
                )
            completed += len(pending) + protected
            pending.clear()
            summary = replace(
                summary,
                duplicates_trashed=summary.duplicates_trashed + applied,
                duplicate_skips=summary.duplicate_skips + failed + batch_protected + protected,
                errors=summary.errors + failed,
            )
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "empty-files",
                    "Enviando archivos vacíos",
                    completed,
                    candidates,
                    "archivos",
                ),
            )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "empty-files",
                "Archivos vacíos procesados",
                completed,
                candidates,
                "archivos",
                True,
                (
                    ProgressMetric(
                        "planned",
                        (
                            max(0, candidates - (summary.duplicate_skips - skips_before_phase))
                            if not self._apply
                            else 0
                        ),
                    ),
                    ProgressMetric("applied", summary.duplicates_trashed),
                ),
            ),
        )
        return summary

    def _trash_duplicates(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        candidates = plan.redundant_files
        # Cover the exact keeper/content comparisons performed below before
        # the first pre-ledger read.  A duplicate member and its keeper have
        # the same planned size, so twice the nominal reclaimable bytes is a
        # conservative input bound for this phase.
        self._reserve_snapshot_work(
            "duplicates-plan",
            (),
            items=candidates,
            bytes_override=max(0, int(plan.reclaimable_bytes)) * 2,
        )
        self._duplicate_work_reserved = True
        summary = replace(
            summary,
            duplicate_candidates=summary.duplicate_candidates + candidates,
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "duplicates",
                "Procesando duplicados",
                0,
                candidates,
                "archivos",
            ),
        )
        completed = 0
        applied_before_phase = summary.duplicates_trashed
        skips_before_phase = summary.duplicate_skips
        pending: list[tuple[str, str, FileSnapshot, FileSnapshot]] = []

        def progress_metrics() -> tuple[ProgressMetric, ...]:
            skipped = max(0, summary.duplicate_skips - skips_before_phase)
            return (
                ProgressMetric(
                    "planned",
                    max(0, candidates - skipped) if not self._apply else 0,
                ),
                ProgressMetric(
                    "applied",
                    max(0, summary.duplicates_trashed - applied_before_phase),
                ),
            )

        def report() -> None:
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "duplicates",
                    "Procesando duplicados",
                    completed,
                    candidates,
                    "archivos",
                    metrics=progress_metrics(),
                ),
            )

        def flush_pending() -> None:
            nonlocal completed, summary
            if not pending:
                return
            batch = tuple((path, evidence) for path, evidence, _snapshot, _reference in pending)
            expected = tuple(snapshot for _path, _evidence, snapshot, _reference in pending)
            references = tuple(reference for _path, _evidence, _snapshot, reference in pending)
            pending.clear()
            applied, failed, protected = self._apply_trash_batch(
                "trash_duplicate",
                batch,
                expected_snapshots=expected,
                reference_snapshots=references,
            )
            summary = replace(
                summary,
                duplicates_trashed=summary.duplicates_trashed + applied,
                duplicate_skips=summary.duplicate_skips + failed + protected,
                errors=summary.errors + failed,
            )
            completed += len(batch)
            report()

        def fail_candidate(path: str, evidence: str, detail: str) -> None:
            nonlocal completed, summary
            protected_reason = (
                "legal attribution metadata"
                if _is_third_party_metadata_name(path)
                else _protected_path_reason(path)
            )
            if protected_reason is None:
                protected_reason = self._protected_content_skip_reason(path)
            if protected_reason is not None:
                summary = replace(
                    summary,
                    duplicate_skips=summary.duplicate_skips + 1,
                )
                completed += 1
                report()
                return
            action_id = self._state.begin_file_action(
                self._run_id,
                "trash_duplicate",
                path,
                None,
                "application/octet-stream",
                evidence,
                self._apply,
            )
            self._state.finish_file_action(action_id, "failed", detail)
            summary = replace(
                summary,
                duplicate_skips=summary.duplicate_skips + 1,
                errors=summary.errors + 1,
            )
            completed += 1
            report()

        for group in self._index.iter_duplicate_groups(plan.scan_id):
            evidence = (
                f"{HASH_ALGORITHM_128}={group.full_fingerprint};"
                f"byte-for-byte={str(self._verify_bytes_before_trash).lower()};"
                f"keep={group.keep.path}"
            )
            _keep_now, keep_error = self._validated_duplicate_keeper(group.keep)
            for redundant in group.redundant:
                if _is_third_party_metadata_name(redundant.path):
                    fail_candidate(
                        redundant.path,
                        evidence,
                        "legal attribution metadata is preserved",
                    )
                    continue
                if keep_error is not None:
                    fail_candidate(redundant.path, evidence, keep_error)
                    continue
                if self._apply:
                    try:
                        redundant_now = snapshot_path(redundant.path)
                        if not _same_snapshot(redundant, redundant_now):
                            raise RuntimeError("metadata changed after exact duplicate planning")
                        assert _keep_now is not None
                        if self._verify_bytes_before_trash and not files_equal_exact(
                            _keep_now, redundant_now
                        ):
                            raise RuntimeError("content changed after exact duplicate planning")
                    except (OSError, RuntimeError, FileChangedError) as exc:
                        fail_candidate(redundant.path, evidence, str(exc))
                        continue
                pending.append((redundant.path, evidence, redundant, group.keep))
                if len(pending) >= TRASH_BATCH_SIZE:
                    flush_pending()
        flush_pending()
        # Empty-file effects intentionally defer reconciliation: publishing an
        # inventory successor before this generator is exhausted would make
        # ``plan.scan_id`` resolve to a generation without its duplicate plan.
        self._flush_deferred_reconciliation()
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "duplicates",
                "Duplicados procesados",
                candidates,
                candidates,
                "archivos",
                True,
                progress_metrics(),
            ),
        )
        return summary

    def _validated_duplicate_keeper(
        self,
        planned: FileSnapshot,
    ) -> tuple[FileSnapshot | None, str | None]:
        if not self._apply:
            return None, None
        try:
            current = snapshot_path(planned.path)
            if not _same_snapshot(planned, current):
                raise RuntimeError("keeper metadata changed after duplicate planning")
        except (OSError, RuntimeError) as exc:
            return None, str(exc)
        return current, None

    def _validate_extensions(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        # Keep direct phase callers safe as well as the normal ``execute``
        # route, whose duplicate phase normally flushes this queue first.
        self._flush_deferred_reconciliation()
        # The inventory is the physical source of truth for this pass.  A
        # dry-run only records proposed actions; it does not remove any
        # inventory member from the route input set.  In particular, a
        # planned duplicate is still a real file and must retain its identity
        # and content-type coverage until an effect is actually observed.
        # Applied runs may read the same immutable inventory snapshot because
        # the admission check below rejects sources that were really removed
        # (or changed) before route publication.
        # Empty files have their own explicit, planned ``trash_empty_file``
        # record and are not content-route inputs.  Keep them out of this
        # content-type denominator, but never subtract proposed duplicate
        # files: unlike an observed effect, a dry-run proposal leaves those
        # physical sources available for extraction.
        total = self._index.file_count(self._scan_id) - self._index.file_count_by_size(
            self._scan_id, 0
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "content-types",
                "Validando tipos de contenido",
                0,
                total,
                "archivos",
            ),
        )
        completed = 0
        route_candidates: list[tuple[str, FileSnapshot]] = []
        cache_updates: list[tuple[FileSnapshot, DetectedType | None]] = []

        def flush_route_candidates() -> None:
            if route_candidates:
                self._state.store_route_candidates(self._run_id, route_candidates)
                route_candidates.clear()

        def flush_cache_updates() -> None:
            if cache_updates:
                self._state.store_content_type_cache_batch(
                    cache_updates, DETECTOR_VERSION, self._run_id
                )
                cache_updates.clear()

        def report_progress() -> None:
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "content-types",
                    "Validando tipos de contenido",
                    completed,
                    total,
                    "archivos",
                ),
            )

        # Read bounded pages through the already-open inventory owner.  The
        # page tuple closes its SQLite cursor before this loop can persist
        # route/cache state or apply an extension rename; reopening the same
        # WAL-backed inventory here can exhaust the temporary snapshot budget.
        after_path = ""
        while True:
            page = self._index.snapshots_page(
                self._scan_id,
                after_path=after_path,
                limit=TRASH_BATCH_SIZE,
            )
            if not page:
                break
            after_path = page[-1].path
            if self._reserve_work is not None:
                self._reserve_snapshot_work(
                    "admission-prefix",
                    page,
                    items=0,
                    bytes_override=sum(
                        min(MAX_PREFIX_BYTES, max(0, int(snapshot.size)))
                        for snapshot in page
                    ),
                )
            for planned in page:
                self._admission_checkpoint()
                if planned.size == 0:
                    continue
                completed += 1
                summary, route_candidate, cache_update = self._inspect_content_type_candidate(
                    planned, summary
                )
                if cache_update is not None:
                    cache_updates.append(cache_update)
                    if len(cache_updates) >= 1000:
                        flush_cache_updates()
                if route_candidate is not None:
                    route_candidates.append(route_candidate)
                    if len(route_candidates) >= 1000:
                        flush_route_candidates()
                report_progress()
        flush_route_candidates()
        flush_cache_updates()
        summary = replace(
            summary,
            type_cache_pruned=self._state.prune_content_type_cache(self._run_id, DETECTOR_VERSION),
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "content-types",
                "Validación de tipos completada",
                completed,
                completed,
                "archivos",
                True,
            ),
        )
        return summary

    def _flush_deferred_reconciliation(self) -> None:
        """Publish deferred empty-file removals after the duplicate plan pass."""

        if not self._deferred_reconciliation_paths:
            return
        paths = tuple(self._deferred_reconciliation_paths)
        self._index.apply_reconciliation(
            self._scan_id,
            remove_paths=paths,
        )
        # Clear only after the owner has acknowledged the successor.  If the
        # reconciliation raises, the paths remain available to an explicit
        # retry by the caller rather than being silently discarded.
        self._deferred_reconciliation_paths.clear()

    def _trash_third_party_code(
        self,
        plan: DedupPlan,
        summary: ActionSummary,
    ) -> ActionSummary:
        """Select only artifacts reconstructible from a retained local witness.

        A directory name or origin score can exclude expensive processing, but
        can no longer authorize disposal. The historical action identifier is
        retained for ledger/read compatibility; each new action carries an
        exact regeneration proof and revalidates it at the physical frontier.
        """
        from neocortex.runtime.config.app_paths import default_code_project_roots
        from neocortex.workflow.actions.regeneration import (
            MAX_ARCHIVE_BYTES,
            MAX_MEMBER_BYTES,
            MAX_PYC_BYTES,
            MAX_SOURCE_BYTES,
            _pyc_source_path,
            find_regeneration_proof,
        )

        root = Path(self._index.scan_root(plan.scan_id))
        admission = self._admission_policy or CorpusAdmissionPolicy(
            interested_roots=default_code_project_roots(),
        )
        archive_paths = self._regeneration_source_paths()
        archive_snapshots = tuple(
            snapshot
            for snapshot in self._local_source_snapshots(archive_paths)
            if snapshot.size <= MAX_ARCHIVE_BYTES
        )
        candidates_total = self._index.file_count(self._scan_id)
        pending: list[tuple[str, str, FileSnapshot]] = []
        selected = applied_total = failed_total = protected_total = unproven = 0
        completed = 0
        capped = False
        after_path = ""

        def report(*, finished: bool = False) -> None:
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework", "third-party-code",
                    "Verificando utilidad y regenerabilidad",
                    completed, candidates_total, "archivos", finished,
                    (
                        ProgressMetric("proven", selected),
                        ProgressMetric("not_proven", unproven),
                        ProgressMetric("applied", applied_total),
                    ),
                ),
            )

        def flush() -> None:
            nonlocal applied_total, failed_total, protected_total
            if not pending:
                return
            applied, failed, protected = self._apply_trash_batch(
                "trash_third_party_code",
                tuple((path, evidence) for path, evidence, _snapshot in pending),
                expected_snapshots=tuple(snapshot for _path, _evidence, snapshot in pending),
            )
            applied_total += applied
            failed_total += failed
            protected_total += protected
            pending.clear()

        report()
        while True:
            self._admission_checkpoint()
            page = self._index.snapshots_page(
                plan.scan_id, after_path=after_path, limit=TRASH_BATCH_SIZE,
            )
            if not page:
                break
            page = tuple(page)
            self._reserve_snapshot_work(
                "admission-prefix",
                page,
                items=0,
                bytes_override=sum(
                    min(MAX_PREFIX_BYTES, max(0, int(snapshot.size))) for snapshot in page
                ),
            )
            proof_candidates: list[
                tuple[FileSnapshot, AdmissionDecision, ThirdPartyClassification]
            ] = []
            pyc_sources: dict[str, FileSnapshot] = {}
            for snapshot in page:
                self._admission_checkpoint()
                completed += 1
                path = Path(snapshot.path)
                if (
                    _is_third_party_metadata_name(snapshot.path)
                    or path.suffix.lower() in {".whl", ".nupkg", ".tgz", ".zip"}
                    or any(part.lower() in {"fixtures", "testdata", "test_data"} for part in path.parts)
                ):
                    continue
                decision = assess_file(
                    snapshot, root=root, policy=admission,
                    cancellation_check=self._cancellation_check,
                )
                if decision.disposition != "metadata_only":
                    continue
                classification = classify_third_party_artifact(
                    snapshot.path, project_roots=self._third_party_project_roots,
                )
                if not likely_code_candidate(snapshot.path) and not classification.is_binary:
                    continue
                if not self._third_party_policy.admits(
                    classification.kind.value, classification.confidence,
                ):
                    continue
                if selected + len(proof_candidates) >= self._third_party_policy.max_actions:
                    capped = True
                    continue
                if snapshot.path.casefold().endswith(".pyc"):
                    if snapshot.size > MAX_PYC_BYTES:
                        unproven += 1
                        continue
                    source_path = _pyc_source_path(Path(snapshot.path))
                    source_candidates = self._local_source_snapshots(
                        () if source_path is None else (source_path,)
                    )
                    if not source_candidates or source_candidates[0].size > MAX_SOURCE_BYTES:
                        unproven += 1
                        continue
                    pyc_sources[snapshot.path] = source_candidates[0]
                else:
                    if snapshot.size > MAX_MEMBER_BYTES or not archive_snapshots:
                        unproven += 1
                        continue
                proof_candidates.append((snapshot, decision, classification))
            if proof_candidates:
                proof_inputs: list[FileSnapshot] = []
                proof_input_identities: set[tuple[int, int]] = set()
                for snapshot in (
                    *(snapshot for snapshot, _decision, _classification in proof_candidates),
                    *archive_snapshots,
                    *(
                        pyc_sources[snapshot.path]
                        for snapshot, _decision, _classification in proof_candidates
                        if snapshot.path.casefold().endswith(".pyc")
                    ),
                ):
                    if snapshot.identity in proof_input_identities:
                        continue
                    proof_input_identities.add(snapshot.identity)
                    proof_inputs.append(snapshot)
                self._reserve_snapshot_work(
                    "third-party-proof",
                    tuple(proof_inputs),
                    items=len(proof_candidates),
                )
            for snapshot, decision, classification in proof_candidates:
                self._admission_checkpoint()
                proof = find_regeneration_proof(
                    snapshot, root=root, archive_paths=archive_paths,
                    cancellation_check=self._cancellation_check,
                )
                if proof is None:
                    unproven += 1
                    continue
                evidence = json.dumps(
                    {
                        "schema": "neocortex.regenerable-disposal/v1",
                        "origin_signals": list(classification.evidence),
                        "origin_confidence": classification.confidence,
                        "admission_reason": decision.reason,
                        "proof": proof.to_dict(),
                    },
                    ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                )
                if len(evidence.encode("utf-8")) > 8192:
                    unproven += 1
                    continue
                self._regeneration_proofs[snapshot.path] = proof
                for witness in proof.witnesses:
                    self._retained_regeneration_sources[_path_key(witness.path)] = witness
                selected += 1
                pending.append((snapshot.path, evidence, snapshot))
                if len(pending) >= TRASH_BATCH_SIZE:
                    flush()
            after_path = page[-1].path
            report()
        flush()
        report(finished=True)
        return replace(
            summary,
            third_party_candidates=selected,
            third_party_trashed=applied_total,
            third_party_skips=failed_total + protected_total + int(capped),
            regeneration_proven=selected,
            regeneration_unproven=unproven,
            regeneration_action_limit_reached=capped,
            regeneration_sources_truncated=self._regeneration_sources_truncated,
            errors=summary.errors + failed_total,
        )

    def _inspect_content_type_candidate(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
    ) -> tuple[
        ActionSummary,
        tuple[str, FileSnapshot] | None,
        tuple[FileSnapshot, DetectedType | None] | None,
    ]:
        summary, admitted = self._admit_content_type_candidate(planned, summary)
        if not admitted:
            return summary, None, None
        if self._admission_policy is not None:
            self._admission_checkpoint()
            decision = assess_file(
                planned,
                root=Path(self._index.scan_root(self._scan_id)),
                policy=self._admission_policy,
                cancellation_check=self._cancellation_check,
            )
            reason = decision.reason
            self._admission_reasons[reason] = self._admission_reasons.get(reason, 0) + 1
            if decision.disposition != "process":
                if len(self._admission_examples) < 24:
                    self._admission_examples.append({
                        "path_digest": hashlib.sha256(
                            planned.path.encode("utf-8", "surrogatepass")
                        ).hexdigest(),
                        "decision": decision.to_dict(),
                    })
                if decision.disposition == "sensitive":
                    summary = replace(summary, admission_sensitive=summary.admission_sensitive + 1)
                else:
                    summary = replace(
                        summary, admission_metadata_only=summary.admission_metadata_only + 1,
                    )
                return summary, None, None
            summary = replace(summary, admission_processed=summary.admission_processed + 1)
        summary, detected, usable = self._detect_planned_content_type(
            planned,
            summary,
        )
        if not usable:
            return summary, None, None
        summary, route_candidate = self._classify_detected_content_type(
            planned,
            detected,
            summary,
        )
        return summary, route_candidate, (planned, detected)

    def _admit_content_type_candidate(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
    ) -> tuple[ActionSummary, bool]:
        if _protected_path_reason(planned.path, check_attributes=True) is not None:
            return summary, False
        try:
            current = snapshot_path(planned.path)
        except FileNotFoundError:
            return replace(
                summary,
                stale_inventory=summary.stale_inventory + 1,
            ), False
        except OSError as exc:
            self._record_content_type_error(planned, exc)
            return replace(
                summary,
                files_checked=summary.files_checked + 1,
                errors=summary.errors + 1,
            ), False
        if not _same_snapshot(planned, current):
            return replace(
                summary,
                stale_inventory=summary.stale_inventory + 1,
            ), False
        return replace(
            summary,
            files_checked=summary.files_checked + 1,
        ), True

    def _detect_planned_content_type(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
    ) -> tuple[ActionSummary, DetectedType | None, bool]:
        cache_hit, detected = self._state.get_content_type_cache(
            planned,
            DETECTOR_VERSION,
        )
        if cache_hit:
            return (
                replace(
                    summary,
                    type_cache_hits=summary.type_cache_hits + 1,
                ),
                detected,
                True,
            )
        summary = replace(
            summary,
            type_cache_misses=summary.type_cache_misses + 1,
        )
        try:
            detected = detect_content_type(planned.path)
            refreshed = snapshot_path(planned.path)
        except FileNotFoundError:
            return (
                replace(
                    summary,
                    stale_inventory=summary.stale_inventory + 1,
                ),
                None,
                False,
            )
        except OSError as exc:
            self._record_content_type_error(planned, exc)
            return replace(summary, errors=summary.errors + 1), None, False
        if not _same_snapshot(planned, refreshed):
            return (
                replace(
                    summary,
                    stale_inventory=summary.stale_inventory + 1,
                ),
                None,
                False,
            )
        return summary, detected, True

    def _classify_detected_content_type(
        self,
        planned: FileSnapshot,
        detected: DetectedType | None,
        summary: ActionSummary,
    ) -> tuple[ActionSummary, tuple[str, FileSnapshot] | None]:
        if detected is None:
            return replace(
                summary,
                unknown_types=summary.unknown_types + 1,
            ), None
        summary = replace(summary, types_detected=summary.types_detected + 1)
        if detected.accepts(planned.path):
            return replace(
                summary,
                extensions_matching=summary.extensions_matching + 1,
            ), (detected.mime, planned)
        summary = self._rename_mismatch(planned, detected, summary)
        target = _corrected_path(Path(planned.path), detected.canonical_extension)
        actual_path = (
            target if target.is_file() and not Path(planned.path).exists() else Path(planned.path)
        )
        return summary, (detected.mime, replace(planned, path=str(actual_path)))

    def _record_content_type_error(
        self,
        planned: FileSnapshot,
        error: OSError,
    ) -> None:
        protected_reason = self._protected_content_skip_reason(planned.path)
        if protected_reason is not None:
            self._state.record_event(
                self._run_id,
                "error",
                "content-types",
                "Protected content inspection failed",
                {
                    "actionable": False,
                    "error": str(error),
                    "error_type": type(error).__name__,
                    "path": planned.path,
                    "protected_reason": protected_reason,
                },
            )
            return
        action_id = self._state.begin_file_action(
            self._run_id,
            "validate_content_type",
            planned.path,
            None,
            None,
            None,
            self._apply,
        )
        self._state.finish_file_action(action_id, "failed", str(error))

    def _rename_protected_reason(self, source: Path, target: Path) -> str | None:
        retained = getattr(self, "_retained_regeneration_sources", {})
        if _path_key(source) in retained or _path_key(target) in retained:
            return "retained_regeneration_witness"
        protected_reason = _protected_path_reason(source)
        if protected_reason is None:
            protected_reason = _protected_path_reason(
                target,
                check_attributes=False,
            )
        if protected_reason is None:
            protected_reason = self._protected_content_skip_reason(source, target)
        return protected_reason

    def _begin_rename_action(
        self,
        source: Path,
        target: Path,
        detected: DetectedType,
    ) -> int:
        return self._state.begin_file_action(
            self._run_id,
            "correct_extension",
            str(source),
            str(target),
            detected.mime,
            detected.evidence,
            self._apply,
        )

    def _rename_mismatch(self, planned, detected, summary: ActionSummary) -> ActionSummary:
        """Record an extension correction without a product mutation backend."""

        source = Path(planned.path)
        target = _corrected_path(source, detected.canonical_extension)
        summary = replace(summary, rename_candidates=summary.rename_candidates + 1)
        if self._rename_protected_reason(source, target) is not None:
            return replace(summary, rename_skips=summary.rename_skips + 1)
        action_id = self._begin_rename_action(source, target, detected)
        if not self._apply:
            self._state.finish_file_action(action_id, "planned")
            return summary
        try:
            self._validate_apply_root()
            self._validate_action_path(source, role="rename source")
            self._validate_action_path(
                target,
                role="rename target",
                allow_missing_leaf=True,
            )
        except (InternalPathProtectionError, ProtectedAnalysisRootError):
            raise
        except (OSError, RuntimeError) as exc:
            self._state.finish_file_action(action_id, "failed", str(exc))
            return replace(
                summary,
                rename_skips=summary.rename_skips + 1,
                errors=summary.errors + 1,
            )
        self._state.finish_file_action(
            action_id,
            "skipped",
            "linux_mutation_backend_unavailable",
        )
        return replace(summary, rename_skips=summary.rename_skips + 1)

    def _protected_content_skip_reason(
        self,
        *paths: str | Path,
    ) -> str | None:
        """Return only a content-policy denial; propagate systemic denials."""

        try:
            self._effective_mutation_guard().require_paths_allowed(*paths)
        except ProtectedContentError as exc:
            return str(exc)
        return None

    def _validate_apply_root(
        self,
        *,
        mutation_guard: CorpusMutationGuard | None = None,
    ) -> Path | None:
        """Revalidate the mutation boundary immediately before an action."""

        if not self._apply:
            return None
        mutation_guard = mutation_guard or self._effective_mutation_guard()
        mutation_guard.reject_run_mutation()
        recorded_root = self._index.scan_root(self._scan_id)
        recorded_volume, recorded_file, recorded_birthtime = self._index.scan_root_identity(
            self._scan_id
        )
        run_policy = mutation_guard.policy
        run_identity = (
            run_policy.root_device_id,
            run_policy.root_file_id,
            run_policy.root_birthtime_ns,
        )
        if (
            _path_key(run_policy.root) != _path_key(recorded_root)
            or None in run_identity
            or run_identity != (recorded_volume, recorded_file, recorded_birthtime)
        ):
            raise RuntimeError(
                "framework run root does not match the inventory scan root: "
                f"run={run_policy.root}; scan={recorded_root}"
            )
        current_root = validate_inventory_root(recorded_root)
        if _path_key(recorded_root) != _path_key(current_root):
            raise RuntimeError(
                "inventory root no longer resolves to its recorded canonical path: "
                f"{recorded_root} -> {current_root}"
            )
        current = snapshot_path(current_root)
        if (
            current.identity != (recorded_volume, recorded_file)
            or current.birthtime_ns != recorded_birthtime
        ):
            raise RuntimeError(
                f"inventory root identity changed after the scan was recorded: {recorded_root}"
            )
        return current_root

    def _effective_mutation_guard(self) -> CorpusMutationGuard:
        """Reload the current fail-closed guard at every mutation boundary."""

        return self._state.corpus_mutation_guard(self._run_id)


def apply_exact_dedupe_plan(
    index: DedupIndex,
    state: FrameworkState,
    run_id: int,
    plan: DedupPlan,
    *,
    trash_backend: KioTrashBackend | None = None,
    excluded_paths: Iterable[str | Path] = DEFAULT_EXCLUDED_PATHS,
    exclusion_policy: InventoryExclusionPolicy | None = None,
    progress: ProgressCallback | None = None,
) -> ActionSummary:
    """Apply an existing, complete exact-dedup plan through native KIO.

    This is intentionally not a planner: callers provide the already
    published ``DedupPlan`` and its inventory owner.  Only duplicate members
    are processed; extension corrections, empty files/directories, and any
    non-exact or partial plan remain outside this E1 service.
    """

    if plan.verification_mode != "full_hash" or plan.requested_policy != "exact":
        raise ValueError("exact dedupe application requires a complete full-hash plan")
    if plan.coverage != "complete":
        raise ValueError("exact dedupe application requires complete plan coverage")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("run_id must be positive")
    runner = FrameworkActions(
        index,
        state,
        run_id,
        plan.scan_id,
        apply=True,
        verify_bytes_before_trash=True,
        excluded_paths=excluded_paths,
        exclusion_policy=exclusion_policy,
        progress=progress,
        trash_backend=trash_backend or KioTrashBackend(),
    )
    summary = runner._trash_duplicates(plan, ActionSummary(apply_actions=True))
    runner._publish_preservation_summary("apply_exact_dedupe_plan", summary=summary)
    state.store_action_summary(run_id, summary)
    return summary


# A short alias is useful to importers that already use the noun "dedupe";
# both names intentionally point at the same no-planner service.
apply_exact_dedupe = apply_exact_dedupe_plan


__all__ = ["FrameworkActions", "apply_exact_dedupe", "apply_exact_dedupe_plan"]
# endregion [02]
