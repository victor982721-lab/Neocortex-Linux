"""Cohesive owner mixin extracted from the Framework facade."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from itertools import islice
from pathlib import Path
from typing import Any

from neocortex.deduplication import FileSnapshot
from neocortex.platform.content_types import DetectedType
from neocortex.persistence.framework_state_common import CACHE_PRUNE_BATCH_SIZE
from neocortex.persistence.framework_state_types import (
    CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE as _CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE,
    CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE as _CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE,
    _FrameworkStateOwner,
    InventoryRunEvidence,
)
from neocortex.persistence.operational_freshness import operational_identity_floor

class FrameworkStateContentMixin(_FrameworkStateOwner):
    """Implementation for one FrameworkState/FrameworkActions responsibility."""

    def get_content_type_cache_batch(
        self,
        snapshots: Iterable[FileSnapshot],
        detector_version: str,
    ) -> dict[tuple[int, int, int, int, int], tuple[bool, DetectedType | None]]:
        """Return metadata-valid detections with bounded owner-side lookups.

        The caller owns the SQLite connection and supplies one inventory page
        (or another bounded chunk).  Identities are grouped into bounded SQL
        statements so this operation never creates one ``SELECT`` per file or
        exceeds SQLite's variable limit.  The complete physical identity is
        checked in Python because the durable cache key is identity plus
        detector version while size/mtime/birthtime are the freshness fence.

        Only cache hits are returned.  A present ``(True, None)`` value is a
        cached UNKNOWN decision; an absent key is a miss.
        """

        found: dict[tuple[int, int, int, int, int], tuple[bool, DetectedType | None]] = {}
        source = iter(snapshots)
        while True:
            batch = tuple(islice(source, _CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE))
            if not batch:
                break
            identity_to_snapshots: dict[tuple[str, str], list[FileSnapshot]] = {}
            for snapshot in batch:
                identity_to_snapshots.setdefault(
                    (f"{snapshot.volume_id:x}", f"{snapshot.file_id:x}"),
                    [],
                ).append(snapshot)
            predicates = " OR ".join(
                "(volume_id=? AND file_id=?)" for _ in identity_to_snapshots
            )
            parameters: list[object] = [detector_version]
            for volume_id, file_id in identity_to_snapshots:
                parameters.extend((volume_id, file_id))
            rows = self._connection.execute(
                f"""SELECT volume_id,file_id,size,mtime_ns,birthtime_ns,status,mime,
                canonical_extension,accepted_extensions_json,evidence
                FROM content_type_cache
                WHERE detector_version=? AND ({predicates})""",
                parameters,
            ).fetchall()
            for row in rows:
                candidates = identity_to_snapshots.get((str(row[0]), str(row[1])), ())
                for snapshot in candidates:
                    if (
                        int(row[2]) != snapshot.size
                        or int(row[3]) != snapshot.mtime_ns
                        or int(row[4]) != snapshot.birthtime_ns
                    ):
                        continue
                    found[self._content_type_cache_key(snapshot)] = (
                        True,
                        self._decode_content_type_cache_row(row),
                    )
        return found

    def get_content_type_cache(
        self, snapshot: FileSnapshot, detector_version: str
    ) -> tuple[bool, DetectedType | None]:
        """Return a metadata-valid detection, including cached unknown results."""

        return self.get_content_type_cache_batch((snapshot,), detector_version).get(
            self._content_type_cache_key(snapshot),
            (False, None),
        )

    def store_content_type_cache(
        self,
        snapshot: FileSnapshot,
        detector_version: str,
        detected: DetectedType | None,
        run_id: int,
    ) -> None:
        """Persist one reusable detector result without reading the file again."""

        self.store_content_type_cache_batch(((snapshot, detected),), detector_version, run_id)

    def store_content_type_cache_batch(
        self,
        rows: Iterable[tuple[FileSnapshot, DetectedType | None]],
        detector_version: str,
        run_id: int,
    ) -> None:
        """Upsert a bounded hit/miss batch in one transaction."""

        updated_ns = time.time_ns()
        statement = """INSERT OR REPLACE INTO content_type_cache(
                volume_id,file_id,size,mtime_ns,birthtime_ns,detector_version,status,mime,
                canonical_extension,accepted_extensions_json,evidence,last_seen_run_id,
                updated_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"""

        def parameters(batch: Iterable[tuple[FileSnapshot, DetectedType | None]]):
            return (
                (
                    f"{snapshot.volume_id:x}",
                    f"{snapshot.file_id:x}",
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    detector_version,
                    "unknown" if detected is None else "detected",
                    None if detected is None else detected.mime,
                    None if detected is None else detected.canonical_extension,
                    None
                    if detected is None
                    else json.dumps(
                        sorted(detected.accepted_extensions), separators=(",", ":")
                    ),
                    None if detected is None else detected.evidence,
                    run_id,
                    updated_ns,
                )
                for snapshot, detected in batch
            )

        with self._connection:
            source = iter(rows)
            while True:
                batch = tuple(islice(source, _CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE))
                if not batch:
                    return
                self._connection.executemany(statement, parameters(batch))

    def prune_route_candidates(
        self,
        keep_run_ids: Iterable[int] = (),
    ) -> int:
        """Remove old routing snapshots while preserving explicitly resumable runs."""

        keep = tuple(sorted({int(value) for value in keep_run_ids}))
        removed = 0
        while True:
            if keep:
                placeholders = ",".join("?" for _ in keep)
                rows = self._connection.execute(
                    f"""SELECT run_id,path FROM route_candidates
                    WHERE run_id NOT IN ({placeholders})
                    ORDER BY run_id,path LIMIT 1000""",
                    keep,
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """SELECT run_id,path FROM route_candidates
                    ORDER BY run_id,path LIMIT 1000"""
                ).fetchall()
            if not rows:
                return removed
            with self._connection:
                removed += int(
                    self._connection.executemany(
                        "DELETE FROM route_candidates WHERE run_id=? AND path=?", rows
                    ).rowcount
                )

    def latest_route_candidate_run(self) -> int | None:
        row = self._connection.execute("SELECT MAX(run_id) FROM route_candidates WHERE run_id>?", (operational_identity_floor(self._connection, "framework"),)).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def route_candidate_run_count(self, run_id: int) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM route_candidates WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )

    def route_candidate_workload(self, run_id: int) -> tuple[int, int]:
        """Return bounded item/byte counts for a retained route snapshot."""

        row = self._connection.execute(
            """SELECT COUNT(*),COALESCE(SUM(size),0)
            FROM route_candidates WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def route_run_count(self, run_id: int) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM route_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )

    def has_durable_routing_snapshot(self, run_id: int) -> bool:
        """Return whether a bound scan crossed a durable publication boundary."""

        row = self._connection.execute(
            "SELECT status,scan_id FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row[1] is None:
            return False
        if str(row[0]) == "completed":
            return True
        marker = self._connection.execute(
            """SELECT 1 FROM run_events WHERE run_id=? AND (
            (phase='routing-snapshot' AND message='Snapshot de rutas publicado') OR
            (phase='inventory-recovery' AND message='Vínculo de inventario recuperado'))
            LIMIT 1""",
            (run_id,),
        ).fetchone()
        if marker is not None:
            return True
        return self.route_run_count(run_id) > 0

    def source_run_scan_id(self, run_id: int) -> int:
        _, scan_id = self.source_run_inventory(run_id)
        if scan_id is None:
            raise ValueError(f"source run {run_id} has no reusable scan")
        return scan_id

    def source_run_inventory(self, run_id: int) -> tuple[Path, int | None]:
        row = self._connection.execute(
            "SELECT root,scan_id FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"source run {run_id} does not exist")
        return Path(str(row[0])), None if row[1] is None else int(row[1])

    def source_inventory_policy_signature(self, run_id: int) -> str | None:
        """Return the effective inventory boundary persisted by one source run."""

        row = self._connection.execute(
            "SELECT inventory_policy_signature FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"source run {run_id} does not exist")
        return None if row[0] is None else str(row[0])

    def content_admission_ledger(self):
        """Return the Semantic admission ledger on this Framework owner.

        The import is lazy so the long-lived Framework writer keeps its
        existing import boundary; the ledger never opens a second database.
        """

        from neocortex.persistence.framework_content_admission import ContentAdmissionLedger

        return ContentAdmissionLedger(self)

    def recorded_inventory_evidence(self, run_id: int) -> InventoryRunEvidence:
        """Recover one unambiguous inventory checkpoint from append-only events."""

        rows = self._connection.execute(
            """SELECT event_id,details_json FROM run_events
            WHERE run_id=? AND phase='inventory'
            AND message='Inventario preparado' AND details_json IS NOT NULL
            ORDER BY event_id DESC LIMIT 101""",
            (run_id,),
        ).fetchall()
        if len(rows) > 100:
            raise ValueError(f"source run {run_id} has too many inventory evidence events")

        def strict_integer(details: Mapping[str, Any], name: str) -> int:
            value = details[name]
            if type(value) is not int:
                raise ValueError(f"inventory evidence {name} is not an integer")
            return value

        evidence: list[InventoryRunEvidence] = []
        for event_id, details_json in rows:
            try:
                details = json.loads(str(details_json))
                if not isinstance(details, dict):
                    raise ValueError("inventory evidence is not an object")
                schema = details.get("schema")
                if schema not in {None, "neocortex.inventory-prepared/v1"}:
                    raise ValueError(f"unsupported inventory evidence schema: {schema}")
                scan_id = strict_integer(details, "scan_id")
                files = strict_integer(details, "files")
                reconciliation_records = strict_integer(details, "reconciliation_records")
                inventory_attempts = strict_integer(details, "attempts")
                inventory_mode_value = details["mode"]
                if not isinstance(inventory_mode_value, str):
                    raise ValueError("inventory evidence mode is not a string")
                inventory_mode = inventory_mode_value
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"source run {run_id} has malformed inventory event {event_id}"
                ) from exc
            if (
                scan_id <= 0
                or files < 0
                or reconciliation_records < 0
                or inventory_attempts < 0
                or inventory_mode not in {"full", "incremental"}
            ):
                raise ValueError(f"source run {run_id} has invalid inventory event {event_id}")
            evidence.append(
                InventoryRunEvidence(
                    int(event_id),
                    scan_id,
                    files,
                    reconciliation_records,
                    inventory_attempts,
                    inventory_mode,
                )
            )
        if not evidence:
            raise ValueError(f"source run {run_id} has no validated inventory event evidence")
        evidence_values = {
            (
                item.scan_id,
                item.files,
                item.reconciliation_records,
                item.inventory_attempts,
                item.inventory_mode,
            )
            for item in evidence
        }
        if len(evidence_values) != 1:
            raise ValueError(f"source run {run_id} has ambiguous inventory event evidence")
        return evidence[0]

    def resumable_route_names(self, run_id: int) -> tuple[str, ...]:
        """Return incomplete routes in their original stable order."""

        rows = self._connection.execute(
            """SELECT route_name FROM route_runs WHERE run_id=?
            AND status IN ('running','interrupted','failed','cancelled')
            ORDER BY started_ns,route_name""",
            (run_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def run_recovery_plan(self, run_id: int) -> dict[str, Any]:
        """Describe safe recovery inputs without starting another worker.

        Completed routes are explicitly ``skipped``.  Routes left in a
        non-terminal state are candidates for a new run only when their
        retained route inputs still exist; otherwise they are reported as
        ``non_replayable`` and the caller must abstain rather than repeat an
        uncertain effect.
        """

        row = self._connection.execute(
            """SELECT status,run_kind,source_run_id FROM initial_runs
            WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} does not exist")
        routes = self._connection.execute(
            """SELECT route_name,status FROM route_runs
            WHERE run_id=? ORDER BY started_ns,route_name""",
            (run_id,),
        ).fetchall()
        candidate_rows, candidate_bytes = self.route_candidate_workload(run_id)
        route_capabilities = self.read_run_route_capabilities(run_id)
        route_input_sources = self.read_route_input_sources(run_id)
        start_event = self._connection.execute(
            """SELECT details_json FROM run_events
            WHERE run_id=? AND phase='run'
            AND message='Ejecución aislada de rutas iniciada'
            ORDER BY event_id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if not route_input_sources and start_event is not None and start_event[0] is not None:
            try:
                details = json.loads(str(start_event[0]))
            except (TypeError, json.JSONDecodeError):
                details = None
            if isinstance(details, Mapping) and isinstance(
                details.get("route_input_sources"), Mapping
            ):
                route_input_sources = {
                    str(name): str(source)
                    for name, source in details["route_input_sources"].items()
                }
        skipped = tuple(str(name) for name, status in routes if str(status) == "completed")
        pending = tuple(
            str(name)
            for name, status in routes
            if str(status) in {"running", "interrupted", "failed", "cancelled"}
        )
        non_replayable = tuple(
            str(name)
            for name, status in routes
            if str(status) in {"failed", "cancelled", "interrupted"}
            and (
                (
                    candidate_rows == 0
                    and route_input_sources.get(str(name), "route_candidates")
                    != "inventory_snapshot"
                )
                or route_capabilities.get(str(name), "safe_replay") == "not_resumable"
            )
        )
        pending_stages = self._pending_organization_stages(run_id)
        return {
            "run_id": run_id,
            "status": str(row[0]),
            "run_kind": str(row[1]),
            "source_run_id": None if row[2] is None else int(row[2]),
            "resumed": str(row[1]) == "resume",
            "recoverable": str(row[0]) == "interrupted"
            or bool(pending or pending_stages),
            "replayed": bool(skipped),
            "skipped": list(skipped),
            "pending": list(pending),
            "pending_stages": list(pending_stages),
            "non_replayable": list(non_replayable),
            "route_input_sources": route_input_sources,
            "route_capabilities": route_capabilities,
            "candidate_rows": candidate_rows,
            "candidate_bytes": candidate_bytes,
            "candidates_retained": candidate_rows > 0,
        }

    def copy_route_candidates(self, source_run_id: int, target_run_id: int) -> int:
        """Copy one immutable routing snapshot without walking the filesystem."""

        with self._connection:
            result = self._connection.execute(
                """INSERT OR REPLACE INTO route_candidates(
                run_id,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                SELECT ?,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
                FROM route_candidates WHERE run_id=?""",
                (target_run_id, source_run_id),
            )
        return int(result.rowcount)

    def prune_content_type_cache(self, run_id: int, detector_version: str) -> int:
        """Remove stale detector rows in bounded transactions."""

        removed = 0
        while True:
            rows = self._connection.execute(
                """SELECT volume_id,file_id,detector_version
                FROM content_type_cache
                WHERE detector_version<>? OR last_seen_run_id<>?
                ORDER BY volume_id,file_id,detector_version LIMIT ?""",
                (detector_version, run_id, CACHE_PRUNE_BATCH_SIZE),
            ).fetchall()
            if not rows:
                return removed
            with self._connection:
                removed += int(
                    self._connection.executemany(
                        """DELETE FROM content_type_cache
                        WHERE volume_id=? AND file_id=? AND detector_version=?""",
                        rows,
                    ).rowcount
                )

    def store_route_candidates(
        self,
        run_id: int,
        candidates: Iterable[tuple[str, FileSnapshot]],
    ) -> None:
        """Persist already-detected route inputs in bounded caller batches."""

        with self._connection:
            self._connection.executemany(
                """INSERT OR REPLACE INTO route_candidates(
                    run_id,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
                    VALUES(?,?,?,?,?,?,?,?)""",
                (
                    (
                        run_id,
                        mime,
                        snapshot.path,
                        f"{snapshot.volume_id:x}",
                        f"{snapshot.file_id:x}",
                        snapshot.size,
                        snapshot.mtime_ns,
                        snapshot.birthtime_ns,
                    )
                    for mime, snapshot in candidates
                ),
            )

    def iter_route_candidates(self, run_id: int, mime: str):
        rows = self._connection.execute(
            """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
            FROM route_candidates WHERE run_id=? AND mime=? ORDER BY path""",
            (run_id, mime),
        )
        for path, volume_id, file_id, size, mtime_ns, birthtime_ns in rows:
            yield FileSnapshot(
                path,
                int(volume_id, 16),
                int(file_id, 16),
                int(size),
                int(mtime_ns),
                int(birthtime_ns),
            )

    def iter_route_candidates_by_prefix(self, run_id: int, mime_prefix: str):
        """Stream detected route inputs for a MIME family in stable path order."""

        rows = self._connection.execute(
            """SELECT mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            FROM route_candidates WHERE run_id=? AND mime LIKE ? ORDER BY path""",
            (run_id, f"{mime_prefix}%"),
        )
        for mime, path, volume_id, file_id, size, mtime_ns, birthtime_ns in rows:
            yield (
                str(mime),
                FileSnapshot(
                    path,
                    int(volume_id, 16),
                    int(file_id, 16),
                    int(size),
                    int(mtime_ns),
                    int(birthtime_ns),
                ),
            )

__all__ = ["FrameworkStateContentMixin"]
