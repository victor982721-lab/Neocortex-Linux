"""Final physical layout after routes/classification, before Full Semantic.

Inventory is opened once to detach a paged metadata selection into the existing
Framework writer's TEMP workspace, then closed before any move/cache COW. The
document move owner, not this adapter, owns effects, receipts and recovery.
"""

from __future__ import annotations

from pathlib import Path

from neocortex.deduplication import DedupIndex, FileSnapshot
from neocortex.documents.residual_materialization import (
    ResidualMaterializationConfig, ResidualMaterializer, ResidualMimeDecision,
)
from neocortex.platform.content_types import DETECTOR_VERSION


_DDL = "(path TEXT PRIMARY KEY,volume_id TEXT,file_id TEXT,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER) WITHOUT ROWID"


def _values(snapshot: FileSnapshot):
    return (snapshot.path, f"{snapshot.volume_id:x}", f"{snapshot.file_id:x}",
            snapshot.size, snapshot.mtime_ns, snapshot.birthtime_ns)


def publish_layout_exclusions(framework_state, admission) -> None:
    """Keep blocked/recovery/preview exclusions alive through the route DAG."""
    connection = framework_state._connection
    connection.execute("CREATE TEMP TABLE curation_layout_excluded " + _DDL)
    for page in admission.excluded_pages():
        with connection:
            connection.executemany("INSERT INTO temp.curation_layout_excluded VALUES(?,?,?,?,?,?)",
                                   (_values(snapshot) for snapshot, _reason in page))


def _ensure_roots(root, state_directory, guard):
    from neocortex.documents.document_organization_application import _create_validated_apply_root

    guard.reject_run_mutation()
    guard.policy.verify_root_identity()
    for destination in (root / "Corpus_ordenado", root / "Sin_clasificar", root / "Sin_clasificar" / "_MIME"):
        guard.require_paths_allowed(destination)
        _create_validated_apply_root(state_directory, destination, destination.parent,
                                     destination.parent.lstat(), guard)
    guard.policy.verify_root_identity()


def run_residual_layout(owner, *, root, state, run_id, scan_id):
    """Select physical survivors without keeping Inventory open during moves."""
    connection = state._connection
    connection.execute("CREATE TEMP TABLE curation_layout_sources " + _DDL)
    cache = {}
    withheld = 0
    withheld_actions = 0
    try:
        with DedupIndex(owner.config.dedup_database) as index:
            scan = index.current_scan_id(scan_id)
            after = ""
            while page := index.snapshots_page(scan, after_path=after, limit=256):
                owner._cancellation.checkpoint()
                state.check_run_budget(run_id)
                after = page[-1].path
                with connection:
                    connection.executemany("INSERT INTO temp.curation_layout_sources VALUES(?,?,?,?,?,?)",
                                           (_values(snapshot) for snapshot in page))
        # The same physical observation denied to expensive work cannot cross
        # another effect just because it survived a blocked Trash/recovery.
        denied = connection.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name='curation_layout_excluded'"
        ).fetchone()
        if denied is not None:
            with connection:
                withheld = connection.execute(
                    "DELETE FROM temp.curation_layout_sources AS s WHERE EXISTS("
                    "SELECT 1 FROM temp.curation_layout_excluded e WHERE e.path=s.path "
                    "AND e.volume_id=s.volume_id AND e.file_id=s.file_id AND e.size=s.size "
                    "AND e.mtime_ns=s.mtime_ns AND e.birthtime_ns=s.birthtime_ns)"
                ).rowcount
        if owner.config.apply_actions:
            # Denial is conservative, not an effect authority: a source with
            # an unresolved prior effect in this run cannot be moved again to
            # make the final topology appear complete. The action receipt
            # remains the recovery source; no path-only permission is granted.
            with connection:
                withheld_actions = connection.execute(
                    "DELETE FROM temp.curation_layout_sources WHERE path IN("
                    "SELECT a.source_path FROM file_actions a WHERE a.run_id=? AND a.apply_requested=1 "
                    "AND a.status<>'applied' "
                    "AND a.action_type IN ('correct_extension','trash_redlist','trash_artifact',"
                    "'trash_duplicate','trash_empty_file'))", (run_id,),
                ).rowcount

        def snapshots():
            nonlocal cache
            after = ""
            while True:
                owner._cancellation.checkpoint()
                state.check_run_budget(run_id)
                rows = connection.execute(
                    "SELECT * FROM temp.curation_layout_sources WHERE path>? ORDER BY path LIMIT 256", (after,)
                ).fetchall()
                if not rows:
                    return
                after = rows[-1][0]
                page = tuple(FileSnapshot(row[0], int(row[1], 16), int(row[2], 16), *row[3:]) for row in rows)
                cache = state.get_content_type_cache_batch(page, DETECTOR_VERSION)
                yield from page

        def decision(survivor):
            snapshot = survivor.snapshot
            if snapshot is None:
                raise RuntimeError("residual selection lost its inventory identity")
            key = (snapshot.volume_id, snapshot.file_id, snapshot.size,
                   snapshot.mtime_ns, snapshot.birthtime_ns)
            available, detected = cache.get(key, (False, None))
            # An unobserved nonempty file is not a licence for a fresh detector
            # after admission. Fail closed and leave it for explicit recovery.
            if not available and snapshot.size:
                raise RuntimeError("residual survivor has no current Identify decision")
            return ResidualMimeDecision(
                path=Path(snapshot.path), mime=None if detected is None else detected.mime,
                volume_id=snapshot.volume_id, file_id=snapshot.file_id, size=snapshot.size,
                mtime_ns=snapshot.mtime_ns, birthtime_ns=snapshot.birthtime_ns,
                detector_version=DETECTOR_VERSION, evidence="unknown" if detected is None else detected.evidence,
            )

        guard = state.corpus_mutation_guard(run_id)
        if owner.config.apply_actions:
            _ensure_roots(root, owner.config.state_directory, guard)
        state.set_run_phase(run_id, "residual_mime")
        result = ResidualMaterializer(ResidualMaterializationConfig(
            corpus_root=root, apply=bool(owner.config.apply_actions), preview=not owner.config.apply_actions,
            state_directory=owner.config.state_directory, catalog_path=owner.config.document_catalog_database,
            framework_state=state, run_id=run_id, mutation_guard=guard, framework_lock_held=True,
            checkpoint=owner._cancellation.checkpoint,
        )).materialize(snapshots(), decisions=decision)
        payload = result.to_dict()
        # Large receipts remain in their durable owners, not in a summary.
        payload.pop("moves", None)
        payload["withheld_by_admission"] = withheld
        payload["withheld_by_prior_action"] = withheld_actions
        payload["status"] = "completed" if result.complete and not (withheld or withheld_actions) else "partial"
        state.publish_run_stage(run_id, "residual-mime", payload["status"], details=payload,
                                idempotency_key="residual-mime:final-layout")
        return payload
    finally:
        connection.execute("DROP TABLE temp.curation_layout_sources")
        if connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='curation_layout_excluded'").fetchone():
            connection.execute("DROP TABLE temp.curation_layout_excluded")
