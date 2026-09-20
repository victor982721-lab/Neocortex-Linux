"""Safely apply exact-duplicate and extension-correction actions."""
# region [00] Contexto del módulo
# Módulo: neocortex/actions.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path


from neocortex.deduplication import (
    DedupIndex,
    DedupPlan,
    FileSnapshot,
    files_equal_exact,  # noqa: F401 - historical test/injection seam
    full_fingerprint,
    snapshot_path,
)
from neocortex.deduplication.inventory.index import (
    DEFAULT_EXCLUDED_PATHS,
    InventoryExclusionPolicy,
)
from neocortex.deduplication.admission import (
    size_is_admitted,
    validate_max_file_bytes,
)
from neocortex.progress import ProgressCallback
from neocortex.platform.content_types import DETECTOR_VERSION, DetectedType, detect_content_type
from neocortex.runtime.models import ActionSummary
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.mutations import (
    KioTrashBackend,
    PosixRenameBackend,
)
# endregion [01]

# region [02] Implementación


TRASH_BATCH_SIZE = 256
ReserveWork = Callable[[str, int, int], None]
CONTENT_PREFIX_BYTES = 64 * 1024
REDLIST_REASON_EXAMPLE_LIMIT = 24
REDLIST_REASON_CODE_LIMIT = 64
DetectionKey = tuple[int, int, int, int, int]
IDENTIFY_PROGRESS_ITEM_STEP = 100
IDENTIFY_PROGRESS_INTERVAL_NS = 100_000_000


def _redlist_reason_code(value: object) -> str:
    """Map an untrusted action diagnostic to one bounded reason code.

    Redlist diagnostics are persisted in the Framework owner.  Do not copy
    backend messages (which may contain paths, helper output, or arbitrary
    bytes) into the durable counters.  The detailed message remains owned by
    the file-action row when that row crossed a real frontier.
    """

    text = str(value or "").casefold()
    known = (
        "destination_exists",
        "outside_root",
        "protected_content",
        "internal_path",
        "symbolic_link",
        "reparse",
        "source_disappeared",
        "metadata_changed",
        "identity_drift",
        "backend_unavailable",
        "preflight_failed",
        "effect_ambiguous",
        "receipt_invalid",
        "receipt_missing",
        "source_changed",
        "cancelled",
        "budget_exhausted",
    )
    aliases = {
        "destination exists": "destination_exists",
        "outside": "outside_root",
        "escapes root": "outside_root",
        "protected content": "protected_content",
        "internal framework path": "internal_path",
        "symbolic link": "symbolic_link",
        "symlink": "symbolic_link",
        "reparse": "reparse",
        "source disappeared": "source_disappeared",
        "metadata changed": "metadata_changed",
        "source changed": "source_changed",
        "identity": "identity_drift",
        "backend": "backend_unavailable",
        "preflight": "preflight_failed",
        "ambiguous": "effect_ambiguous",
        "recovery": "effect_ambiguous",
        "receipt": "receipt_invalid",
        "cancel": "cancelled",
        "budget": "budget_exhausted",
    }
    for token in known:
        if token in text:
            return token
    for token, code in aliases.items():
        if token in text:
            return code
    return "unspecified"


_LEGAL_METADATA_NAMES = frozenset(
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
_LEGAL_METADATA_PREFIXES = (
    "license",
    "licence",
    "copying",
    "notice",
    "authors",
)
_PROTECTED_EFFECT_NAMES = frozenset(
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
        "password",
        "passwords",
        "secret",
        "secrets",
        "secrets.json",
        "token",
        "token.json",
    }
)
_PROTECTED_EFFECT_SUFFIXES = frozenset(
    {".asc", ".gpg", ".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}
)
_FIXTURE_COMPONENTS = frozenset({"fixture", "fixtures", "test_data", "testdata"})
TRASH_IDENTITY_ABSTENTION = (
    "Recycle Bin mutation abstained: the available Send2Trash backends resolve "
    "the source by path and cannot bind the observed file identity to the syscall"
)
# Compatibility probe for existing diagnostic/test consumers that monkeypatch
# the removed path backend to assert it is never invoked. Production code never
# reads or calls this sentinel.
send2trash: None = None


def _is_legal_metadata_name(path: str | Path) -> bool:
    """Keep license/notice attribution files out of an origin cleanup plan."""

    name = Path(path).name.casefold()
    if name in _LEGAL_METADATA_NAMES:
        return True
    if name.endswith("notices") and ("-" in name or "_" in name):
        return True
    return any(
        name.startswith(prefix)
        and len(name) > len(prefix)
        and name[len(prefix)] in {"-", "_", "."}
        for prefix in _LEGAL_METADATA_PREFIXES
    )


from neocortex.workflow.actions.action_identify import IdentifyActionsMixin  # noqa: E402
from neocortex.workflow.actions.action_redlist_stage import RedlistActionsMixin  # noqa: E402
from neocortex.workflow.actions.action_effects import EffectsActionsMixin  # noqa: E402
from neocortex.workflow.actions.action_contracts import (  # noqa: E402
    RedlistPrepassError as _RedlistPrepassError,
)

RedlistPrepassError = _RedlistPrepassError

class FrameworkActions(IdentifyActionsMixin, RedlistActionsMixin, EffectsActionsMixin):
    """Apply bounded action batches with durable before/after records."""

    @staticmethod
    def _detector_version() -> str:
        """Resolve the detector version through the historical module seam."""

        return DETECTOR_VERSION

    @staticmethod
    def _detector_function():
        """Resolve the detector through the historical module seam."""

        return detect_content_type

    @staticmethod
    def _snapshot_path(path: str | Path) -> FileSnapshot:
        """Resolve snapshotting through the historical module seam."""

        return snapshot_path(path)

    @staticmethod
    def _full_fingerprint(snapshot: FileSnapshot) -> bytes:
        """Resolve full hashing through the historical module seam."""

        return full_fingerprint(snapshot)

    def __init__(
        self,
        index: DedupIndex,
        state: FrameworkState,
        run_id: int,
        scan_id: int,
        *,
        apply: bool,
        max_file_bytes: int | None = None,
        verify_bytes_before_trash: bool = True,
        excluded_paths: Iterable[str | Path] = DEFAULT_EXCLUDED_PATHS,
        exclusion_policy: InventoryExclusionPolicy | None = None,
        progress: ProgressCallback | None = None,
        trash_backend: KioTrashBackend | None = None,
        rename_backend: PosixRenameBackend | None = None,
        cancellation_check: Callable[[], None] | None = None,
        reserve_work: ReserveWork | None = None,
    ):
        self._index = index
        self._state = state
        self._run_id = run_id
        self._scan_id = scan_id
        self._apply = apply
        # This ceiling is a run-scoped admission decision.  Inventory remains
        # complete; every content-aware action owner consults this same value
        # before opening a source or consulting any content cache.
        self._max_file_bytes = validate_max_file_bytes(max_file_bytes)
        self._size_skipped_files = 0
        self._size_skipped_bytes = 0
        # Destructive mode never relies on a non-cryptographic fingerprint
        # alone, even when candidate reduction used the fast policy.
        self._verify_bytes_before_trash = apply or verify_bytes_before_trash
        self._exclusion_policy = exclusion_policy or InventoryExclusionPolicy.compile(
            excluded_paths
        )
        self._progress = progress
        self._trash_backend = trash_backend
        self._rename_backend = rename_backend or PosixRenameBackend()
        self._deferred_reconciliation_paths: list[str] = []
        self._deferred_reconciliation_upserts: list[FileSnapshot] = []
        self._cancellation_check = cancellation_check
        self._reserve_work = reserve_work
        self._duplicate_work_reserved = False
        # A redlist page reserves the bounded lifecycle work before any of its
        # candidates can cross the action frontier.  Keep that reservation
        # visible while the corresponding trash batch is applied so the
        # generic batch helper does not charge the same page a second time.
        self._redlist_page_reserved = False
        # Paths admitted by the explicit redlist must not become route inputs
        # merely because a hard boundary prevented the physical effect.  Keep
        # this as a bounded in-memory set; the policy matcher is also checked
        # lazily so a second FrameworkActions instance (the integrated runner
        # creates one after the prepass) observes the same decision.
        self._redlist_excluded_paths: set[str] = set()
        self._normalized_paths: dict[str, str] = {}
        self._redlist_prepass_active = False
        self._redlist_policy_active = False
        # Integrated --all sets this while running the explicit prepass after
        # Identify/Normalize.  Direct FrameworkActions callers retain the
        # legacy late-redlist compatibility path until they opt into the
        # ordered pipeline through ``identify_and_normalize``.
        self._redlist_suppress_late_mutation: bool = False
        self._normalize_without_full_hash: bool = False
        # Identify is deliberately a single content-aware pass.  Keep the
        # decisions observed by that pass bound to the complete physical
        # identity, rather than to a path that Normalize may replace.  The
        # durable content_type_cache remains the cross-run source; this map
        # prevents the same FrameworkActions instance from reopening the
        # payload after Dedupe merely to publish route candidates.
        self._identified_types: dict[DetectionKey, DetectedType | None] = {}
        self._identified_detector_version: str | None = None
        self._identify_summary: ActionSummary | None = None
        self._redlist_diagnostics: dict[str, object] = {
            "blocked": 0,
            "protected": 0,
            "failed_pre_effect": 0,
            "recovery_required": 0,
            "reason_codes": {},
            "examples": [],
        }
        self._redlist_batch_diagnostics: dict[str, object] = {}

    def _size_is_admitted(self, snapshot: FileSnapshot) -> bool:
        """Return the single global admission decision for one snapshot."""

        return size_is_admitted(snapshot.size, self._max_file_bytes)

    def _record_size_skip(
        self,
        summary: ActionSummary,
        snapshot: FileSnapshot,
    ) -> ActionSummary:
        """Record bounded run metrics without making a file-level decision durable.

        Older focused action fixtures may construct an ``ActionSummary`` from
        before the global-size fields existed.  The conditional update keeps
        that compatibility seam while allowing the runtime model to expose
        ``size_skipped_files``/``size_skipped_bytes`` when present.
        """

        self._size_skipped_files += 1
        self._size_skipped_bytes += max(0, int(snapshot.size))
        return replace(
            summary,
            size_skipped_files=summary.size_skipped_files + 1,
            size_skipped_bytes=summary.size_skipped_bytes + max(0, int(snapshot.size)),
            max_file_bytes=self._max_file_bytes,
        )

    def _with_size_limit(self, summary: ActionSummary) -> ActionSummary:
        """Attach the run-scoped ceiling to summaries that expose the field."""

        return replace(summary, max_file_bytes=self._max_file_bytes)










    @staticmethod
    def _detection_key(snapshot: FileSnapshot) -> DetectionKey:
        """Bind one Identify result to the observed file metadata.

        A path is intentionally absent from this key.  Normalize can replace
        only the name while retaining the same inode and metadata; the bounded
        content evidence remains valid for that successor.  Any identity,
        size, mtime, or birth-time change therefore misses the key and forces
        a fresh Identify decision.
        """

        return (
            int(snapshot.volume_id),
            int(snapshot.file_id),
            int(snapshot.size),
            int(snapshot.mtime_ns),
            int(snapshot.birthtime_ns),
        )

    def _remember_identified_type(
        self,
        snapshot: FileSnapshot,
        detected: DetectedType | None,
    ) -> None:
        self._identified_types[self._detection_key(snapshot)] = detected


    def execute(self, plan: DedupPlan, *, cleanup_empty_directories: bool = True) -> ActionSummary:
        self._validate_apply_root()
        self._deferred_reconciliation_paths.clear()
        self._deferred_reconciliation_upserts.clear()
        self._duplicate_work_reserved = False
        self._redlist_page_reserved = False
        # The integrated runner has already completed Identify/Normalize.  In
        # that canonical path, route publication consumes the decisions from
        # that pass instead of invoking the detector a second time.  Direct
        # FrameworkActions callers that skip Identify retain the historical
        # one-shot fallback below for compatibility and focused action tests.
        summary = self._with_size_limit(
            self._identify_summary or ActionSummary(apply_actions=self._apply)
        )
        started = time.perf_counter_ns()
        summary = self._trash_empty_files(plan, summary)
        self._record_phase("empty-files", started, summary)
        started = time.perf_counter_ns()
        summary = self._trash_duplicates(plan, summary)
        self._record_phase("duplicates", started, summary)
        # All physical effects must precede content-type candidate publication;
        # otherwise route_candidates could retain a path already moved to
        # Trash and a later route would read a stale source identity.
        started = time.perf_counter_ns()
        if self._identify_summary is None:
            summary = self._validate_extensions(plan, summary)
        else:
            summary = self._validate_extensions(
                plan,
                summary,
                reuse_identified=True,
            )
        self._record_phase("content-types", started, summary)
        if cleanup_empty_directories:
            started = time.perf_counter_ns()
            summary = self._trash_empty_directories(plan, summary)
            self._record_phase("empty-directories", started, summary)
        self._state.store_action_summary(self._run_id, summary)
        return summary



    def _checkpoint(self) -> None:
        if self._cancellation_check is not None:
            self._cancellation_check()

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



































def apply_exact_dedupe_plan(
    index: DedupIndex,
    state: FrameworkState,
    run_id: int,
    plan: DedupPlan,
    *,
    max_file_bytes: int | None = None,
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
        max_file_bytes=max_file_bytes,
        verify_bytes_before_trash=True,
        excluded_paths=excluded_paths,
        exclusion_policy=exclusion_policy,
        progress=progress,
        trash_backend=trash_backend or KioTrashBackend(),
    )
    summary = runner._trash_duplicates(plan, ActionSummary(apply_actions=True))
    state.store_action_summary(run_id, summary)
    return summary


# A short alias is useful to importers that already use the noun "dedupe";
# both names intentionally point at the same no-planner service.
apply_exact_dedupe = apply_exact_dedupe_plan


__all__ = ["FrameworkActions", "apply_exact_dedupe", "apply_exact_dedupe_plan"]
# endregion [02]
