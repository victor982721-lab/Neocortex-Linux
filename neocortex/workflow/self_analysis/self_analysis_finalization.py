"""Pure evidence contracts for protected self-analysis finalization."""

from __future__ import annotations
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import InventoryExclusionPolicy

from neocortex.safety.corpus_access import CorpusAccessPolicy
from neocortex.workflow.self_analysis.self_analysis import (
    build_self_analysis_completion_manifest,
)


# region [01] Persisted run evidence


@dataclass(frozen=True, slots=True)
class SelfAnalysisRunEvidence:
    """Validated owner row required to publish a protected completion."""

    run_id: int
    root: str
    access_mode: str
    root_device_id_hex: str
    root_file_id_hex: str
    root_birthtime_ns: int
    state_directory: str
    inventory_policy_signature: str
    scan_id: int
    journal_volume: str | None
    journal_id: str | None
    start_usn: int | None
    reconciliation_records: int
    inventory_attempts: int
    inventory_mode: str

    @classmethod
    def decode(
        cls,
        run_id: int,
        row: Sequence[object] | None,
    ) -> SelfAnalysisRunEvidence:
        """Decode and validate the fixed initial-runs projection."""

        if row is None:
            raise ValueError(f"self-analysis run {run_id} does not exist")
        if len(row) != 16:
            raise ValueError(f"self-analysis run {run_id} has malformed evidence")
        (
            root,
            status,
            run_kind,
            access_mode,
            root_device_id_hex,
            root_file_id_hex,
            root_birthtime_ns,
            state_directory,
            policy_signature,
            scan_id,
            journal_volume,
            journal_id,
            start_usn,
            reconciliation_records,
            inventory_attempts,
            inventory_mode,
        ) = row
        if status != "running" or run_kind != "self_analysis" or access_mode != "analyze_only":
            raise ValueError(f"run {run_id} is not a running protected self-analysis")
        required = (
            root_device_id_hex,
            root_file_id_hex,
            root_birthtime_ns,
            state_directory,
            policy_signature,
            scan_id,
            reconciliation_records,
            inventory_attempts,
            inventory_mode,
        )
        if any(value is None for value in required):
            raise ValueError(f"self-analysis run {run_id} has incomplete evidence")
        journal_values = (journal_volume, journal_id, start_usn)
        if any(value is None for value in journal_values) and not all(
            value is None for value in journal_values
        ):
            raise ValueError(f"self-analysis run {run_id} has partial journal evidence")
        if all(value is None for value in journal_values) and (
            inventory_mode != "full"
            or int(cast(int, reconciliation_records)) != 0
            or int(cast(int, inventory_attempts)) != 1
        ):
            raise ValueError(f"self-analysis run {run_id} has invalid journal-free inventory")
        return cls(
            run_id=run_id,
            root=str(root),
            access_mode=str(access_mode),
            root_device_id_hex=str(root_device_id_hex),
            root_file_id_hex=str(root_file_id_hex),
            root_birthtime_ns=int(cast(int, root_birthtime_ns)),
            state_directory=str(state_directory),
            inventory_policy_signature=str(policy_signature),
            scan_id=int(cast(int, scan_id)),
            journal_volume=(None if journal_volume is None else str(journal_volume)),
            journal_id=None if journal_id is None else str(journal_id),
            start_usn=None if start_usn is None else int(cast(int, start_usn)),
            reconciliation_records=int(cast(int, reconciliation_records)),
            inventory_attempts=int(cast(int, inventory_attempts)),
            inventory_mode=str(inventory_mode),
        )

    def validate_inventory_boundary(
        self,
        cursor: JournalCursor | None,
        inventory_policy: InventoryExclusionPolicy,
    ) -> None:
        """Validate policy and journal continuity at the completion boundary."""

        if self.inventory_policy_signature != inventory_policy.signature:
            raise ValueError("self-analysis inventory policy signature changed")
        if self.journal_volume is None:
            if cursor is not None:
                raise ValueError("journal-free self-analysis received a cursor")
            return
        if cursor is None or self.journal_id is None or self.start_usn is None:
            raise ValueError("self-analysis completion cursor is missing")
        if (
            self.journal_volume != cursor.volume
            or self.journal_id != str(cursor.journal_id)
            or cursor.next_usn < self.start_usn
        ):
            raise ValueError("self-analysis completion cursor is inconsistent")

    def access_policy(self) -> CorpusAccessPolicy:
        """Rehydrate the identity-bound analyze-only policy."""

        return CorpusAccessPolicy.from_storage(
            self.access_mode,
            self.root,
            self.root_device_id_hex,
            self.root_file_id_hex,
            self.root_birthtime_ns,
        )

    def inventory_binding(self) -> tuple[int, int, int, str, int]:
        """Return the arguments for the owner's inventory binding validator."""

        return (
            self.scan_id,
            self.reconciliation_records,
            self.inventory_attempts,
            self.inventory_mode,
            0,
        )

    def manifest_run(self) -> dict[str, object]:
        """Build the run section without touching durable state."""

        return {
            "run_id": self.run_id,
            "run_kind": "self_analysis",
            "status": "completed",
            "corpus_access_mode": "analyze_only",
            "root": self.root,
            "root_identity": {
                "device_id_hex": self.root_device_id_hex,
                "file_id_hex": self.root_file_id_hex,
                "birthtime_ns": self.root_birthtime_ns,
            },
            "state_directory": self.state_directory,
        }

    def manifest_inventory(self, cursor: JournalCursor | None) -> dict[str, object]:
        """Build the bounded inventory section at the supplied end cursor."""

        journal: dict[str, object]
        if cursor is None:
            journal = {"status": "unavailable"}
        else:
            journal = {
                "status": "available",
                "volume": self.journal_volume,
                "journal_id": self.journal_id,
                "start_usn": self.start_usn,
                "end_usn": cursor.next_usn,
            }
        return {
            "scan_id": self.scan_id,
            "mode": self.inventory_mode,
            "attempts": self.inventory_attempts,
            "reconciliation_records": self.reconciliation_records,
            "journal": journal,
        }


# endregion [01]


# region [02] Route and safety evidence


def _single_code_summary_json(
    route_rows: Sequence[Sequence[object]],
) -> str:
    if (
        len(route_rows) != 1
        or route_rows[0][0] != "code"
        or route_rows[0][1] != "completed"
        or route_rows[0][2] is None
        or route_rows[0][3] is not None
    ):
        raise ValueError("self-analysis requires exactly one completed code route")
    return str(route_rows[0][2])


def _decode_code_summary(summary_json: str) -> dict[str, object]:
    if len(summary_json.encode("utf-8")) > 65_536:
        raise ValueError("self-analysis code summary exceeds its bound")
    try:
        decoded = json.loads(summary_json)
    except json.JSONDecodeError as exc:
        raise ValueError("self-analysis code summary is malformed") from exc
    if not isinstance(decoded, dict):
        raise ValueError("self-analysis code summary must be an object")
    return cast(dict[str, object], decoded)


def _validate_processing_signature(
    processing_signature: str,
    summary: Mapping[str, object],
) -> None:
    if (
        not isinstance(processing_signature, str)
        or not processing_signature
        or len(processing_signature.encode("utf-8")) > 8_192
    ):
        raise ValueError("self-analysis code signature is invalid")
    if summary.get("processing_signature") != processing_signature:
        raise ValueError("self-analysis code signature does not match its route summary")


@dataclass(frozen=True, slots=True)
class CompletedCodeRoute:
    """The single completed code route admitted by protected analysis."""

    processing_signature: str
    summary: dict[str, object]

    @classmethod
    def decode(
        cls,
        snapshot_markers: Sequence[Sequence[object]],
        route_rows: Sequence[Sequence[object]],
        processing_signature: str,
    ) -> CompletedCodeRoute:
        """Validate the unique snapshot marker and completed code route."""

        if len(snapshot_markers) != 1:
            raise ValueError("self-analysis requires one published snapshot")
        summary = _decode_code_summary(_single_code_summary_json(route_rows))
        _validate_processing_signature(processing_signature, summary)
        return cls(processing_signature, summary)


@dataclass(frozen=True, slots=True)
class SelfAnalysisSafetyCounts:
    """Counts proving protected analysis emitted no corpus mutation work."""

    route_candidates: int
    file_actions: int
    run_actions: int
    organization_events: int

    def validate(self) -> None:
        """Reject any candidate, action, or organization evidence."""

        if any(self.as_manifest().values()):
            raise ValueError("self-analysis produced forbidden corpus work")

    def as_manifest(self) -> dict[str, int]:
        """Return the exact safety section consumed by the manifest contract."""

        return {
            "route_candidates": self.route_candidates,
            "file_actions": self.file_actions,
            "run_actions": self.run_actions,
            "organization_events": self.organization_events,
        }


# endregion [02]


# region [03] Completion manifest evidence


@dataclass(frozen=True, slots=True)
class SelfAnalysisCompletionEvidence:
    """Pure aggregate used to construct one canonical completion manifest."""

    run: SelfAnalysisRunEvidence
    cursor: JournalCursor | None
    code: CompletedCodeRoute
    safety: SelfAnalysisSafetyCounts

    def build_manifest(
        self,
        *,
        inventory_policy: InventoryExclusionPolicy,
        commands: Mapping[str, Sequence[str]],
    ) -> tuple[dict[str, object], str]:
        """Build the canonical manifest without owning a SQLite transaction."""

        return build_self_analysis_completion_manifest(
            run=self.run.manifest_run(),
            inventory=self.run.manifest_inventory(self.cursor),
            inventory_policy=inventory_policy,
            code_processing_signature=self.code.processing_signature,
            code_summary=self.code.summary,
            safety_counts=self.safety.as_manifest(),
            commands=commands,
        )


# endregion [03]


__all__ = [
    "CompletedCodeRoute",
    "SelfAnalysisCompletionEvidence",
    "SelfAnalysisRunEvidence",
    "SelfAnalysisSafetyCounts",
]
