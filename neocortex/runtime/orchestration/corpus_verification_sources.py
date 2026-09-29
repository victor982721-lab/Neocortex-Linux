"""Owner-bound projections for the terminal Corpus verifier.

This adapter is intentionally below the orchestration coordinator.  It lends
an already-open Framework writer connection, reads quiescent non-Framework
owners through :class:`SQLiteReadSession`, and exposes only bounded current
rows and existing receipts.  It never opens a second Framework connection,
never uses SQLite ``mode=ro``, and never performs an Identify/Semantic
operation or a filesystem effect.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from neocortex.deduplication import snapshot_path
from neocortex.deduplication.inventory.scanner import MAX_SCAN_FILES
from neocortex.deduplication.inventory.repository_scans import resolve_scan_id
from neocortex.foundation.file_identity import decode_file_identity
from neocortex.persistence.sqlite_immutable import (
    SQLiteReadSession,
    ImmutableSQLiteUnavailable,
    preferred_sqlite_read_mode,
)
from neocortex.platform.content_types import DETECTOR_VERSION

from .corpus_verification import (
    ClassifiedSurvivor,
    CorpusVerificationInputs,
    CorpusVerificationResult,
    OrganizationMoveReceipt,
    OwnerCurrentPath,
    PendingFinding,
    ResidualSurvivor,
    verify_after_semantic,
    verify_before_semantic,
)


_PAGE = 256
_SOURCE_CACHE_OWNERS = (
    ("pdf_database", "documents"),
    ("docx_database", "documents"),
    ("office_database", "documents"),
    ("text_database", "documents"),
    ("audio_database", "documents"),
    ("image_database", "images"),
    ("video_database", "documents"),
)


class _AdapterState:
    def __init__(self, *, root: Path, checkpoint, max_rows: int) -> None:
        self.root = root
        self.checkpoint = checkpoint
        self.max_rows = max_rows
        self.errors: list[str] = []
        self.bound_exceeded = False

    def tick(self) -> None:
        if self.checkpoint is not None:
            self.checkpoint()

    def error(self, detail: str) -> None:
        if len(self.errors) < 64:
            self.errors.append(str(detail)[:512])

    def probe_limit(
        self,
        connection: Any,
        sql: str,
        parameters: tuple[object, ...],
        *,
        label: str,
    ) -> None:
        """Probe one extra bounded row without materialising an owner stream."""

        try:
            self.tick()
            probe_parameters = (
                (*parameters[:-1], self.max_rows + 1)
                if parameters
                else (self.max_rows + 1,)
            )
            count = connection.execute(
                f"SELECT COUNT(*) FROM ({sql}) AS bounded_owner_rows",
                probe_parameters,
            ).fetchone()[0]
            if isinstance(count, int) and count > self.max_rows:
                self.bound_exceeded = True
                self.error(f"{label}: bounded owner projection exceeds {self.max_rows} rows")
            self.tick()
        except Exception as exc:
            self.error(f"{label}: bounded row probe failed: {type(exc).__name__}")


def _absolute_root(root: str | Path) -> Path:
    path = Path(root)
    if not path.is_absolute():
        raise ValueError("verification root must be absolute")
    return Path(os.path.abspath(os.fspath(path)))


def _validate_framework_scope(
    framework_state: Any,
    *,
    root: Path,
    run_id: int,
    scan_id: int,
    state: _AdapterState,
) -> None:
    """Validate the live Framework binding without requiring terminal status."""

    inventory_reader = getattr(framework_state, "source_run_inventory", None)
    manifest_reader = getattr(framework_state, "read_run_manifest", None)
    if not callable(inventory_reader) or not callable(manifest_reader):
        state.error("Framework live owner lacks public run-scope readers")
        return
    try:
        connection = getattr(framework_state, "_connection", None)
        if connection is None:
            state.error("Framework live writer connection is unavailable")
        else:
            row = connection.execute(
                "SELECT status FROM initial_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None or str(row[0]) != "running":
                state.error("Framework run is not live/running at verifier boundary")
        observed_root, observed_scan = inventory_reader(run_id)
        if Path(os.path.abspath(os.fspath(observed_root))) != root:
            state.error("Framework run root does not match verifier root")
        if observed_scan is None or int(observed_scan) != scan_id:
            state.error("Framework run scan binding does not match verifier scan")
        manifest = manifest_reader(run_id)
        if not isinstance(manifest, Mapping):
            state.error("Framework lifecycle manifest is missing")
            return
        if (
            int(manifest.get("run_id", -1)) != run_id
            or str(manifest.get("run_kind", "initial")) != "initial"
            or str(manifest.get("root", "")) != str(root)
        ):
            state.error("Framework lifecycle manifest is not bound to this run/root")
        if not isinstance(manifest.get("input_snapshot"), Mapping):
            state.error("Framework lifecycle manifest input snapshot is missing")
    except (OSError, RuntimeError, TypeError, ValueError):
        state.error("Framework run scope/manifest could not be validated")


def _semantic_skip_is_authorized(framework_state: Any, run_id: int, *, state: _AdapterState) -> bool:
    reader = getattr(framework_state, "read_run_stages", None)
    if not callable(reader):
        return False
    try:
        stages = tuple(item for item in reader(run_id) if isinstance(item, Mapping))
    except Exception as exc:
        state.error(f"Semantic lifecycle stage read failed: {type(exc).__name__}")
        return False
    semantic = [item for item in stages if item.get("stage") == "semantic"]
    if not semantic:
        return False
    latest = semantic[-1]
    details = latest.get("details")
    return (
        latest.get("status") == "skipped"
        and isinstance(details, Mapping)
        and details.get("selected_sources") == []
        and details.get("image_available") is False
        and details.get("source_unavailable") == {}
    )


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_prefix(root: Path) -> str:
    # LIKE wildcards are escaped so a corpus name cannot widen an owner query.
    prefix = str(root).rstrip(os.sep) + os.sep
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _table_columns(connection: Any, table: str) -> set[str]:
    if table not in {
        "documents", "images", "files", "route_candidates", "semantic_items",
        "organization_plans", "planned_duplicate_groups", "planned_duplicate_members",
        "file_actions", "published_embedding_heads", "embedding_generations",
        "embedding_generation_members", "semantic_item_revisions",
    }:
        raise ValueError("unknown adapter table")
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _require_columns(connection: Any, table: str, required: set[str], state: _AdapterState) -> bool:
    try:
        columns = _table_columns(connection, table)
    except Exception as exc:
        state.error(f"{table}: schema inspection failed: {type(exc).__name__}")
        return False
    missing = sorted(required - columns)
    if missing:
        state.error(f"{table}: missing required columns {','.join(missing)}")
        return False
    return True


def _open_owner(
    stack: ExitStack,
    path: Path,
    *,
    label: str,
    state: _AdapterState,
):
    if not path.is_file():
        return None
    try:
        mode = preferred_sqlite_read_mode(path)
        return stack.enter_context(
            SQLiteReadSession(
                path,
                mode=mode,
                timeout_seconds=30.0,
                max_attempts=2,
                cancellation_check=lambda: (state.tick() or False),
            )
        )
    except (OSError, RuntimeError, ValueError, ImmutableSQLiteUnavailable) as exc:
        state.error(f"{label}: fenced owner read unavailable: {type(exc).__name__}")
        return None


def _iter_path_rows(
    connection: Any,
    *,
    owner: str,
    sql: str,
    parameters: tuple[object, ...],
    root: Path,
    state: _AdapterState,
    required_columns: set[str],
    row_identity: bool = False,
    provenance_index: int | None = None,
    empty_is_error: bool = False,
) -> Iterator[OwnerCurrentPath]:
    table = {
        "catalog": "documents",
        "source_cache": "documents",
        "framework": "route_candidates",
        "dedup": "files",
        "semantic": "semantic_items",
    }[owner]
    if not _require_columns(connection, table, required_columns, state):
        yield OwnerCurrentPath(owner, None, status="empty", observed_exists=False)  # type: ignore[arg-type]
        return
    found = False
    try:
        state.probe_limit(connection, sql, parameters, label=owner)
        cursor = connection.execute(sql, parameters)
        while True:
            state.tick()
            rows = cursor.fetchmany(_PAGE)
            if not rows:
                break
            for row in rows:
                raw = row[0]
                if not isinstance(raw, str) or not raw:
                    state.error(f"{owner}: current path row is malformed")
                    continue
                path = Path(raw)
                if not path.is_absolute():
                    state.error(f"{owner}: current path is not absolute")
                    continue
                if not _inside(root, path):
                    continue
                identity = None
                if row_identity:
                    try:
                        identity = {
                            "volume_id": row[1],
                            "file_id": row[2],
                            "size": row[3],
                            "mtime_ns": row[4],
                            "birthtime_ns": row[5],
                        }
                    except (IndexError, TypeError):
                        state.error(f"{owner}: current identity row is malformed")
                        continue
                elif provenance_index is not None:
                    try:
                        provenance = json.loads(str(row[provenance_index]))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(provenance, Mapping):
                        continue
                    identity = provenance.get("physical_identity", provenance.get("identity"))
                    if identity is None and isinstance(provenance.get("source_revision"), Mapping):
                        revision = provenance["source_revision"]
                        identity = revision.get("physical_identity")
                        if identity is None and {"volume_id", "file_id", "size", "mtime_ns", "birthtime_ns"}.issubset(revision):
                            identity = revision
                    if not isinstance(identity, Mapping):
                        continue
                    if not {"volume_id", "file_id", "size", "mtime_ns", "birthtime_ns"}.issubset(identity):
                        # Semantic virtual resources are not asserted as
                        # physical corpus paths without owner metadata.
                        continue
                found = True
                yield OwnerCurrentPath(owner, path, physical_identity=identity)
    except Exception as exc:
        state.error(f"{owner}: current path read failed: {type(exc).__name__}")
    if not found:
        if empty_is_error:
            state.error(f"{owner}: terminal owner published no current source evidence")
        yield OwnerCurrentPath(owner, None, status="empty", observed_exists=False)  # type: ignore[arg-type]


def _iter_source_cache_paths(
    connections: tuple[tuple[str, Any], ...],
    *,
    root: Path,
    state: _AdapterState,
) -> Iterator[OwnerCurrentPath]:
    found = False
    prefix = _path_prefix(root)
    for _label, connection in connections:
        if connection is None:
            continue
        table = "images" if _label == "image_database" else "documents"
        identity_columns = {"volume_id", "file_id", "size", "mtime_ns", "birthtime_ns"}
        if not _require_columns(connection, table, {"path"}, state):
            continue
        try:
            columns = _table_columns(connection, table)
            direct_identity = identity_columns.issubset(columns)
            encoded_identity = {"file_key", "size", "mtime_ns", "birthtime_ns"}.issubset(columns)
            if not direct_identity and not encoded_identity:
                state.error(f"{_label}: current physical identity columns are missing")
                continue
            selected_columns = (
                "path,volume_id,file_id,size,mtime_ns,birthtime_ns"
                if direct_identity
                else "path,file_key,size,mtime_ns,birthtime_ns"
            )
            # Do not use a global MAX(last_seen_run_id): a later partial route
            # run can have a lower-coverage frontier.  The owner table is the
            # current keyed projection; stale rows remain visible and fail
            # identity/path verification instead of being silently dropped.
            query = f"SELECT {selected_columns} FROM {table} WHERE path LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?"
            query_parameters = (prefix, state.max_rows)
            state.probe_limit(connection, query, query_parameters, label=_label)
            cursor = connection.execute(query, query_parameters)
            while True:
                state.tick()
                rows = cursor.fetchmany(_PAGE)
                if not rows:
                    break
                for row in rows:
                    raw = row[0]
                    if not isinstance(raw, str) or not raw:
                        state.error(f"{_label}: malformed current path")
                        continue
                    path = Path(raw)
                    if not path.is_absolute():
                        state.error(f"{_label}: current path is not absolute")
                        continue
                    if not _inside(root, path):
                        continue
                    found = True
                    if direct_identity:
                        identity = {
                            "volume_id": row[1],
                            "file_id": row[2],
                            "size": row[3],
                            "mtime_ns": row[4],
                            "birthtime_ns": row[5],
                        }
                    else:
                        try:
                            decoded = decode_file_identity(str(row[1]))
                        except (TypeError, ValueError):
                            state.error(f"{_label}: encoded file identity is malformed")
                            continue
                        identity = {
                            "volume_id": decoded.volume_id,
                            "file_id": decoded.file_id,
                            "size": row[2],
                            "mtime_ns": row[3],
                            "birthtime_ns": row[4],
                        }
                    yield OwnerCurrentPath("source_cache", path, physical_identity=identity)
        except Exception as exc:
            state.error(f"{_label}: current path read failed: {type(exc).__name__}")
    if not found:
        yield OwnerCurrentPath("source_cache", None, status="empty", observed_exists=False)


def _iter_semantic_published_paths(
    connection: Any,
    *,
    root: Path,
    state: _AdapterState,
    empty_is_error: bool,
) -> Iterator[OwnerCurrentPath]:
    """Read only paths reachable from a published Semantic generation head."""

    required_tables = {
        "published_embedding_heads": {"model_signature", "generation_id"},
        "embedding_generations": {"generation_id", "status"},
        "embedding_generation_members": {"generation_id", "item_revision_id"},
        "semantic_item_revisions": {"item_revision_id", "path", "provenance_json"},
    }
    for table, columns in required_tables.items():
        if not _require_columns(connection, table, columns, state):
            yield OwnerCurrentPath("semantic", None, status="empty", observed_exists=False)
            return
    prefix = _path_prefix(root)
    query = """SELECT DISTINCT r.path,r.provenance_json
        FROM published_embedding_heads h
        JOIN embedding_generations g ON g.generation_id=h.generation_id
        JOIN embedding_generation_members m ON m.generation_id=g.generation_id
        JOIN semantic_item_revisions r ON r.item_revision_id=m.item_revision_id
        WHERE g.status IN ('ready','ready_partial')
          AND r.path IS NOT NULL AND r.path LIKE ? ESCAPE '\\'
        ORDER BY r.path LIMIT ?"""
    state.probe_limit(connection, query, (prefix, state.max_rows), label="semantic published paths")
    found = False
    try:
        cursor = connection.execute(query, (prefix, state.max_rows))
        while True:
            state.tick()
            rows = cursor.fetchmany(_PAGE)
            if not rows:
                break
            for raw_path, raw_provenance in rows:
                if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                    state.error("semantic published path is malformed")
                    continue
                path = Path(raw_path)
                if not _inside(root, path):
                    continue
                try:
                    provenance = json.loads(str(raw_provenance))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(provenance, Mapping):
                    continue
                identity = provenance.get("physical_identity")
                if identity is None and isinstance(provenance.get("source_revision"), Mapping):
                    identity = provenance["source_revision"].get("physical_identity")
                if not isinstance(identity, Mapping) or not {"volume_id", "file_id", "size", "mtime_ns", "birthtime_ns"}.issubset(identity):
                    continue
                found = True
                yield OwnerCurrentPath("semantic", path, physical_identity=identity)
    except Exception as exc:
        state.error(f"semantic published path read failed: {type(exc).__name__}")
    if not found:
        if empty_is_error:
            state.error("Semantic has no current paths reachable from a published generation head")
        yield OwnerCurrentPath("semantic", None, status="empty", observed_exists=False)



def _catalog_lookup(
    connection: Any,
    *,
    root: Path,
    state: _AdapterState,
    policy_bundle: object,
):
    """Read the current Catalog row and delegate disposition to the pure gate."""

    from neocortex.documents.semantic_curation_gate import (
        FastOrganizationCurationGate,
        validate_current_fast_curation_decision,
    )
    from neocortex.platform.policy import sqlite_path_collation

    path_collation = sqlite_path_collation()
    required_plan = {
        "plan_id",
        "destination_path",
        "organization_root",
        "source_path",
        "source_kind",
        "file_key",
        "status",
        "cache_sync_status",
        "cache_sync_json",
    }
    required_doc = {
        "source_kind",
        "file_key",
        "path",
        "active",
        "resource_binding_json",
        "volume_id",
        "file_id",
        "size",
        "mtime_ns",
        "birthtime_ns",
        "catalog_status",
    }
    if not _require_columns(connection, "organization_plans", required_plan, state):
        return lambda _path: None
    if not _require_columns(connection, "documents", required_doc, state):
        return lambda _path: None
    try:
        document_columns = _table_columns(connection, "documents")
    except Exception as exc:
        state.error(f"documents: schema inspection failed: {type(exc).__name__}")
        return lambda _path: None
    signature_candidates = tuple(
        name
        for name in ("source_input_signature", "text_fingerprint", "processing_signature")
        if name in document_columns
    )
    if not signature_candidates:
        state.error("documents: current source-input signature column is missing")
        return lambda _path: None
    source_signature_expression = "COALESCE(" + ",".join(
        f"d.{name}" for name in signature_candidates
    ) + ")"

    def lookup(path: Path) -> ClassifiedSurvivor | None:
        state.tick()
        try:
            row = connection.execute(
                """SELECT p.destination_path,p.organization_root,p.source_path,p.status,
                p.cache_sync_status,p.cache_sync_json,p.source_kind,p.file_key,
                d.path,d.active,d.resource_binding_json,
                d.volume_id,d.file_id,d.size,d.mtime_ns,d.birthtime_ns,
                """ + source_signature_expression + """
                FROM organization_plans p JOIN documents d
                ON d.source_kind=p.source_kind AND d.file_key=p.file_key
                WHERE p.destination_path=? COLLATE """ + path_collation + """
                AND p.organization_root=? COLLATE """ + path_collation + """
                AND p.status='applied' AND p.cache_sync_status='synced'
                AND d.active=1 ORDER BY p.plan_id DESC LIMIT 1""",
                (str(path), str(root)),
            ).fetchone()
        except Exception as exc:
            state.error(f"catalog classification lookup failed: {type(exc).__name__}")
            return None
        if row is None:
            return None
        try:
            expected_identity = (
                row[11],
                row[12],
                row[13],
                row[14],
                row[15],
            )
            gate: FastOrganizationCurationGate = validate_current_fast_curation_decision(
                connection,
                source_kind=str(row[6]),
                file_key=str(row[7]),
                policy_bundle=policy_bundle,
                expected_path=str(row[8]),
                expected_identity=expected_identity,
            )
            if not gate.eligible or gate.decision is None:
                return None
            sync_payload = json.loads(str(row[5]))
            if not isinstance(sync_payload, Mapping):
                return None
            receipt = OrganizationMoveReceipt.from_json(
                sync_payload.get("physical_receipt"),
                owner="document_organization",
                status="applied",
            )
            decision = gate.decision
            evidence = {
                "fast_curation_gate": gate.reason,
                "decision": decision.decision,
                "top1_label": decision.top1_label,
                "top1_score": decision.top1_score,
                "top2_score": decision.top2_score,
                "margin": decision.margin,
                "model_signature": decision.model_signature,
                "representation_version": decision.representation_version,
                "ontology_version": decision.ontology_version,
                "prototype_version": decision.prototype_version,
                "policy_version": decision.policy_version,
                "calibration_version": decision.calibration_version,
                "source_binding": dict(decision.source_binding),
                "evidence": dict(decision.evidence),
                "context_provenance": dict(decision.context_provenance),
                "document_kind": gate.document_kind,
            }
            confidence = (
                0.0
                if decision.confidence is None
                else float(decision.confidence)
            )
            return ClassifiedSurvivor(
                path=path,
                classification=evidence,
                confidence=confidence,
                evidence=evidence,
                organization_root=root,
                receipt=receipt,
                status="applied",
                taxonomy_status="verified",
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None

    return lookup



def _residual_lookup(framework_state: Any, dedup_connection: Any, *, scan_id: int, state: _AdapterState):
    def lookup(path: Path) -> ResidualSurvivor | None:
        state.tick()
        try:
            snapshot = snapshot_path(path)
            hit, detected = framework_state.get_content_type_cache(snapshot, DETECTOR_VERSION)
        except Exception as exc:
            state.error(f"Framework content cache lookup failed: {type(exc).__name__}")
            return None
        if not hit:
            return None
        if dedup_connection is None:
            state.error("residual current inventory owner is unavailable")
            return None
        try:
            inventory_row = dedup_connection.execute(
                """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
                FROM files WHERE scan_id=? AND path=? LIMIT 1""",
                (scan_id, str(path)),
            ).fetchone()
        except Exception as exc:
            state.error(f"residual current inventory lookup failed: {type(exc).__name__}")
            return None
        if inventory_row is None:
            return None
        inventory_identity = {
            "path": str(path),
            "volume_id": inventory_row[1],
            "file_id": inventory_row[2],
            "size": inventory_row[3],
            "mtime_ns": inventory_row[4],
            "birthtime_ns": inventory_row[5],
        }
        if detected is None:
            return ResidualSurvivor(
                path=path,
                mime="application/octet-stream",
                evidence={
                    "owner": "framework.content_type_cache",
                    "detector_version": DETECTOR_VERSION,
                    "status": "unknown",
                    "inventory_current": inventory_identity,
                    "mime_identity": {"path": str(path), "mime": "application/octet-stream", "detector_version": DETECTOR_VERSION, "physical_identity": inventory_identity},
                },
            )
        return ResidualSurvivor(
            path=path,
            mime=detected.mime,
            evidence={
                "owner": "framework.content_type_cache",
                "detector_version": DETECTOR_VERSION,
                "status": "detected",
                "detector_evidence": detected.evidence,
                "inventory_current": inventory_identity,
                "mime_identity": {"path": str(path), "mime": detected.mime, "detector_version": DETECTOR_VERSION, "physical_identity": inventory_identity},
            },
        )

    return lookup


def _action_kind(action_type: str, status: str) -> str | None:
    if action_type == "trash_duplicate":
        return "duplicate"
    if action_type in {"trash_artifact", "trash_redlist", "trash_empty_directory", "trash_empty_file"}:
        return "junk"
    if action_type == "residual_mime_move":
        return "producer" if status != "applied" else None
    if "archive" in action_type or "email" in action_type or "material" in action_type:
        return "producer"
    return None


def _valid_trash_receipt(receipt_raw: object, expected_raw: object, path: Path) -> bool:
    try:
        receipt = json.loads(str(receipt_raw))
        expected = json.loads(str(expected_raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    source = expected.get("source") if isinstance(expected, Mapping) else None
    return (
        isinstance(receipt, Mapping)
        and receipt.get("operation") == "trash"
        and receipt.get("source_path") == str(path)
        and receipt.get("source_absent") is True
        and receipt.get("target_path") is None
        and isinstance(receipt.get("source_digest"), str)
        and isinstance(source, Mapping)
        and source.get("path") == str(path)
        and source.get("file_id") is not None
        and source.get("volume_id") is not None
        and source.get("size") is not None
        and source.get("mtime_ns") is not None
    )


def _iter_framework_findings(
    connection: Any,
    *,
    root: Path,
    run_id: int,
    kind: str,
    state: _AdapterState,
) -> Iterator[PendingFinding]:
    required = {
        "run_id", "action_type", "source_path", "target_path", "status",
        "effect_receipt_json", "expected_identity_json",
    }
    if not _require_columns(connection, "file_actions", required, state):
        yield PendingFinding(kind, "pending", detail="Framework file_actions schema unavailable")  # type: ignore[arg-type]
        return
    try:
        query = """SELECT action_type,source_path,target_path,status,detail,
            effect_receipt_json,expected_identity_json
            FROM file_actions WHERE run_id=? ORDER BY action_id LIMIT ?"""
        state.probe_limit(connection, query, (run_id, state.max_rows), label="file_actions")
        cursor = connection.execute(query, (run_id, state.max_rows))
        while True:
            state.tick()
            rows = cursor.fetchmany(_PAGE)
            if not rows:
                break
            for action_type, source, target, status, detail, receipt_raw, expected_raw in rows:
                actual_kind = _action_kind(str(action_type), str(status))
                if actual_kind != kind:
                    continue
                raw_path = target if str(status) == "applied" and target else source
                path = None
                if isinstance(raw_path, str):
                    candidate = Path(raw_path)
                    if candidate.is_absolute() and _inside(root, candidate):
                        path = candidate
                if str(status) == "applied" and actual_kind in {"junk", "duplicate"}:
                    if path is None or not _valid_trash_receipt(receipt_raw, expected_raw, path):
                        status = "pending"
                yield PendingFinding(kind, str(status), path, str(detail or ""))  # type: ignore[arg-type]
    except Exception as exc:
        state.error(f"Framework file_actions read failed: {type(exc).__name__}")
        yield PendingFinding(kind, "pending", detail="Framework file_actions read failed")  # type: ignore[arg-type]


def _iter_admission_findings(admission_result: object, *, state: _AdapterState) -> Iterator[PendingFinding]:
    if not isinstance(admission_result, Mapping):
        state.error("curation admission result is missing or malformed")
        yield PendingFinding("producer", "pending", detail="curation admission result unavailable")
        return
    if "status" not in admission_result or "exclusion_reasons" not in admission_result:
        state.error("curation admission result lacks status/exclusion_reasons")
        yield PendingFinding("producer", "pending", detail="curation admission result incomplete")
        return
    status = str(admission_result.get("status", "")).casefold()
    if status in {"partial", "failed", "blocked", "recovery_required"}:
        yield PendingFinding("producer", "pending", detail=f"curation admission terminal status: {status}")
    pending = admission_result.get("pending_physical_admission", 0)
    if isinstance(pending, int) and pending > 0:
        yield PendingFinding("producer", "pending", detail=f"pending physical admission: {pending}")
    elif not isinstance(pending, int):
        state.error("curation admission pending_physical_admission is malformed")
    reasons = admission_result.get("exclusion_reasons")
    if not isinstance(reasons, Mapping):
        state.error("curation admission exclusion_reasons is malformed")
        yield PendingFinding("policy", "pending", detail="curation admission reason projection unavailable")
        return
    for reason, count in reasons.items():
        if not isinstance(count, int) or count < 0:
            state.error("curation admission exclusion count is malformed")
            yield PendingFinding("policy", "pending", detail="curation admission count malformed")
            continue
        if count == 0:
            continue
        if str(reason) == "producer_incomplete":
            yield PendingFinding("producer", "pending", detail=f"producer_incomplete:{count}")
        elif str(reason) in {"identify_incomplete", "archive_not_consumed"}:
            yield PendingFinding("policy", "pending", detail=f"{reason}:{count}")


def _iter_duplicate_findings(
    connection: Any,
    *,
    framework_connection: Any,
    root: Path,
    scan_id: int,
    kind: str,
    state: _AdapterState,
) -> Iterator[PendingFinding]:
    required_groups = {"group_id", "scan_id"}
    required_members = {"group_id", "path", "role"}
    if not _require_columns(connection, "planned_duplicate_groups", required_groups, state) or not _require_columns(connection, "planned_duplicate_members", required_members, state):
        yield PendingFinding("duplicate", "pending", detail="Dedup duplicate-plan schema unavailable")
        return
    try:
        query = """SELECT m.path FROM planned_duplicate_members m
            JOIN planned_duplicate_groups g ON g.group_id=m.group_id
            WHERE g.scan_id=? AND m.role='redundant'
              AND m.path LIKE ? ESCAPE '\\'
            ORDER BY m.path LIMIT ?"""
        state.probe_limit(
            connection,
            query,
            (scan_id, _path_prefix(root), state.max_rows),
            label="planned_duplicate_members",
        )
        cursor = connection.execute(query, (scan_id, _path_prefix(root), state.max_rows))
        while True:
            state.tick()
            rows = cursor.fetchmany(_PAGE)
            if not rows:
                break
            for (raw_path,) in rows:
                path = Path(str(raw_path))
                if os.path.lexists(path):
                    status = "pending"
                else:
                    status = "removed" if _duplicate_trash_receipt(framework_connection, path, state=state) else "pending"
                yield PendingFinding(kind, status, path, "planned duplicate member")
    except Exception as exc:
        state.error(f"Dedup duplicate-plan read failed: {type(exc).__name__}")
        yield PendingFinding("duplicate", "pending", detail="Dedup duplicate-plan read failed")


def _duplicate_trash_receipt(connection: Any, path: Path, *, state: _AdapterState) -> bool:
    """Require Framework's applied Trash receipt before treating absence as resolved."""

    if connection is None:
        state.error("duplicate member is absent but Framework receipt owner is unavailable")
        return False
    required = {"action_type", "source_path", "status", "effect_receipt_json", "expected_identity_json"}
    if not _require_columns(connection, "file_actions", required, state):
        return False
    try:
        row = connection.execute(
            """SELECT effect_receipt_json,expected_identity_json FROM file_actions
            WHERE action_type='trash_duplicate' AND source_path=? AND status='applied'
            ORDER BY action_id DESC LIMIT 1""",
            (str(path),),
        ).fetchone()
        if row is None:
            return False
        receipt = json.loads(str(row[0]))
        expected = json.loads(str(row[1]))
        source = expected.get("source") if isinstance(expected, Mapping) else None
        return (
            isinstance(receipt, Mapping)
            and receipt.get("operation") == "trash"
            and receipt.get("source_path") == str(path)
            and receipt.get("source_absent") is True
            and receipt.get("target_path") is None
            and isinstance(receipt.get("source_digest"), str)
            and isinstance(source, Mapping)
            and source.get("path") == str(path)
            and source.get("size") is not None
            and source.get("mtime_ns") is not None
            and source.get("file_id") is not None
            and source.get("volume_id") is not None
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        state.error("duplicate Trash receipt is malformed")
        return False


def _owner_current_paths(
    *,
    root: Path,
    config: Any,
    framework_state: Any,
    connections: dict[str, Any],
    run_id: int,
    scan_id: int,
    state: _AdapterState,
    require_semantic_rows: bool,
) -> Iterator[OwnerCurrentPath]:
    # Source route caches are physical owner rows, not model/cache directories.
    yield from _iter_source_cache_paths(
        tuple((label, connections.get(label)) for label, _table in _SOURCE_CACHE_OWNERS),
        root=root,
        state=state,
    )
    catalog = connections.get("catalog")
    if catalog is None:
        yield OwnerCurrentPath("catalog", None, status="empty", observed_exists=False)
    else:
        yield from _iter_path_rows(
            catalog,
            owner="catalog",
            sql="SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns FROM documents WHERE active=1 AND path LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?",
            parameters=(_path_prefix(root), state.max_rows),
            root=root,
            state=state,
            required_columns={"path", "active", "volume_id", "file_id", "size", "mtime_ns", "birthtime_ns"},
            row_identity=True,
        )
    framework_connection = getattr(framework_state, "_connection", None)
    if framework_connection is None:
        state.error("FrameworkState does not expose its live writer connection")
        yield OwnerCurrentPath("framework", None, status="empty", observed_exists=False)
    else:
        yield from _iter_path_rows(
            framework_connection,
            owner="framework",
            sql="SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns FROM route_candidates WHERE run_id=? AND path LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?",
            parameters=(run_id, _path_prefix(root), state.max_rows),
            root=root,
            state=state,
            required_columns={"run_id", "path", "volume_id", "file_id", "size", "mtime_ns", "birthtime_ns"},
            row_identity=True,
        )
    dedup = connections.get("dedup")
    if dedup is None:
        yield OwnerCurrentPath("dedup", None, status="empty", observed_exists=False)
    else:
        yield from _iter_path_rows(
            dedup,
            owner="dedup",
            sql="SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns FROM files WHERE scan_id=? AND path LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?",
            parameters=(scan_id, _path_prefix(root), state.max_rows),
            root=root,
            state=state,
            required_columns={"scan_id", "path", "volume_id", "file_id", "size", "mtime_ns", "birthtime_ns"},
            row_identity=True,
        )
    semantic = connections.get("semantic")
    if semantic is None:
        yield OwnerCurrentPath("semantic", None, status="empty", observed_exists=False)
    else:
        yield from _iter_semantic_published_paths(
            semantic,
            root=root,
            state=state,
            empty_is_error=require_semantic_rows,
        )


def verify_current_corpus(
    config: Any,
    *,
    root: str | Path,
    framework_state: Any,
    run_id: int,
    scan_id: int,
    admission_result: object,
    phase: str = "before_semantic",
    checkpoint=None,
    max_files: int | None = None,
    max_owner_records: int | None = None,
) -> CorpusVerificationResult:
    """Build bounded owner projections and run the terminal verifier.

    ``framework_state`` is borrowed for the duration of this call.  The
    adapter never constructs ``FrameworkState`` and never opens a second
    connection to ``framework.sqlite3``.
    """

    corpus_root = _absolute_root(root)
    configured_limit = getattr(config, "run_max_items", None)
    effective_files = max_files if max_files is not None else configured_limit
    if effective_files is None:
        effective_files = MAX_SCAN_FILES
    effective_owner_records = (
        max_owner_records if max_owner_records is not None else effective_files
    )
    if (
        isinstance(effective_files, bool)
        or not isinstance(effective_files, int)
        or effective_files < 1
        or effective_files > MAX_SCAN_FILES
    ):
        effective_files = MAX_SCAN_FILES
    if (
        isinstance(effective_owner_records, bool)
        or not isinstance(effective_owner_records, int)
        or effective_owner_records < 1
        or effective_owner_records > MAX_SCAN_FILES
    ):
        effective_owner_records = effective_files
    state = _AdapterState(
        root=corpus_root,
        checkpoint=checkpoint,
        max_rows=effective_owner_records,
    )
    if type(run_id) is not int or run_id < 1:
        state.error("run_id is invalid")
    if type(scan_id) is not int or scan_id < 1:
        state.error("scan_id is invalid")
    _validate_framework_scope(
        framework_state,
        root=corpus_root,
        run_id=run_id,
        scan_id=scan_id,
        state=state,
    )
    state_directory = Path(getattr(config, "state_directory", corpus_root / ".state"))
    catalog_path = Path(getattr(config, "document_catalog_database", state_directory / "document_catalog.sqlite3"))
    dedup_path = Path(getattr(config, "dedup_database", state_directory / "dedup.sqlite3"))
    semantic_path = state_directory / "semantic.sqlite3"
    with ExitStack() as stack:
        connections: dict[str, Any] = {}
        catalog = _open_owner(stack, catalog_path, label="catalog", state=state)
        if not dedup_path.is_file():
            state.error("dedup owner is missing during an initial run")
        dedup = _open_owner(stack, dedup_path, label="dedup", state=state)
        current_scan_id = scan_id
        if dedup is not None:
            try:
                current_scan_id = resolve_scan_id(dedup, scan_id)
            except Exception as exc:
                state.error(f"Dedup current scan resolution failed: {type(exc).__name__}")
        semantic_skip = (
            phase == "after_semantic"
            and _semantic_skip_is_authorized(framework_state, run_id, state=state)
        )
        semantic_expected = phase == "after_semantic" and not semantic_skip
        if semantic_expected and not semantic_path.is_file():
            state.error("Semantic owner is missing at the terminal verification phase")
        semantic = (
            _open_owner(stack, semantic_path, label="semantic", state=state)
            if semantic_expected
            else None
        )
        connections.update(catalog=catalog, dedup=dedup, semantic=semantic)
        for label, attribute in _SOURCE_CACHE_OWNERS:
            path = Path(getattr(config, attribute, state_directory / f"{attribute}.sqlite3"))
            connections[label] = _open_owner(stack, path, label=label, state=state)
        policy_bundle = getattr(config, "fast_curation_policy_bundle", None)
        if policy_bundle is None:
            policy_bundle = getattr(config, "curation_policy_bundle", None)
        catalog_lookup = _catalog_lookup(
            catalog,
            root=corpus_root,
            state=state,
            policy_bundle=policy_bundle,
        ) if catalog is not None else None
        inputs = CorpusVerificationInputs(
            classified_lookup=catalog_lookup,
            residual_lookup=_residual_lookup(
                framework_state,
                dedup,
                scan_id=current_scan_id,
                state=state,
            ),
            current_paths=_owner_current_paths(
                root=corpus_root,
                config=config,
                framework_state=framework_state,
                connections=connections,
                run_id=run_id,
                scan_id=current_scan_id,
                state=state,
                require_semantic_rows=semantic_expected,
            ),
            actionable_junk=_iter_framework_findings(
                getattr(framework_state, "_connection", None),
                root=corpus_root,
                run_id=run_id,
                kind="junk",
                state=state,
            ) if getattr(framework_state, "_connection", None) is not None else (),
            actionable_duplicates=_iter_duplicate_findings(
                dedup,
                framework_connection=getattr(framework_state, "_connection", None),
                root=corpus_root,
                scan_id=current_scan_id,
                kind="duplicate",
                state=state,
            ) if dedup is not None else (),
            producers_pending=_iter_admission_findings(admission_result, state=state),
            policy_findings=_policy_findings(
                framework_state=getattr(framework_state, "_connection", None),
                root=corpus_root,
                run_id=run_id,
                admission_result=admission_result,
                state=state,
            ),
            report_metrics=(dict(admission_result) if isinstance(admission_result, Mapping) else {}),
            checkpoint=checkpoint,
        )
        # Fast Curation's ``calibrated_decision=accepted`` is already the
        # caller-owned policy gate.  Do not apply the legacy taxonomy floor a
        # second time or create a second authority.
        confidence = 0.0
        if phase == "before_semantic":
            result = verify_before_semantic(
                corpus_root,
                inputs,
                min_confidence=confidence,
                max_files=effective_files,
                max_owner_records=effective_owner_records,
            )
        elif phase == "after_semantic":
            result = verify_after_semantic(
                corpus_root,
                inputs,
                min_confidence=confidence,
                max_files=effective_files,
                max_owner_records=effective_owner_records,
            )
        else:
            state.error(f"unsupported verification phase: {phase!r}")
            result = verify_before_semantic(
                corpus_root,
                inputs,
                min_confidence=confidence,
                max_files=effective_files,
                max_owner_records=effective_owner_records,
            )
        if state.bound_exceeded:
            result = replace(
                result,
                status="partial",
                coverage="partial",
                passed=False,
            )
        if not state.errors:
            return result
        # Adapter errors are delivered as policy findings during verification;
        # this branch is defensive for errors raised after the policy stream.
        return result


def _policy_findings(
    *,
    framework_state: Any,
    root: Path,
    run_id: int,
    admission_result: object,
    state: _AdapterState,
) -> Iterator[PendingFinding]:
    for finding in _iter_admission_findings(admission_result, state=state):
        if finding.kind == "policy":
            yield finding
    if framework_state is not None:
        yield from _iter_framework_findings(
            framework_state,
            root=root,
            run_id=run_id,
            kind="policy",
            state=state,
        )
    # Setup/read failures are not allowed to disappear merely because an
    # owner happened to contain no current physical rows.
    for detail in tuple(state.errors):
        yield PendingFinding("policy", "pending", detail=detail)


__all__ = ["verify_current_corpus"]
