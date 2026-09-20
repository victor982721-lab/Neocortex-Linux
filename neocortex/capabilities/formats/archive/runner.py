"""Archive route selection, cache replay and publication orchestration.

The facade owns locking and per-container publication; this module keeps the
route-wide candidate/cache loop independent from the format extraction code.
Helper calls resolve through the route module at call time so established
monkeypatch and injection boundaries remain intact.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace

from neocortex.capabilities.formats.archive.models import ArchiveRouteSummary
from neocortex.capabilities.formats.archive.state import archive_database, initialize_archive_state
from neocortex.capabilities.formats.fts_lookup import (
    initialize_format_fts_lookup,
)
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.progress import ProgressEvent, ProgressMetric, emit_progress

from .container_execution import (
    _ArchiveContainerTask,
    _PreparedArchiveContainer,
)
from .contracts import (
    ARCHIVE_MIME,
    ArchiveExtractionError,
    ArchiveCacheInvalid as _ArchiveCacheInvalid,
)
from .traversal import (
    _ArchiveContainerGroup,
    _ContainerCounters,
)


def _route():
    from . import route

    return route


def _cached_container(*args, **kwargs):
    return _route()._cached_container(*args, **kwargs)


def _refresh_cached_container(*args, **kwargs):
    return _route()._refresh_cached_container(*args, **kwargs)


def _extract_archive_container(*args, **kwargs):
    return _route()._extract_archive_container(*args, **kwargs)


def _archive_container_capacity(*args, **kwargs):
    return _route()._archive_container_capacity(*args, **kwargs)


def _archive_container_memory(*args, **kwargs):
    return _route()._archive_container_memory(*args, **kwargs)


def _prune_stale_containers(*args, **kwargs):
    return _route()._prune_stale_containers(*args, **kwargs)


def _store_container_error(*args, **kwargs):
    return _route()._store_container_error(*args, **kwargs)


def run_locked(owner) -> ArchiveRouteSummary:
    owner.cancellation.checkpoint()
    initialize_archive_state(owner.config.state_path)
    candidate_pool, eligible, selected_count = owner._selected_counts()
    processed = cache_hits = cached_errors = complete = partial = errors = 0
    members = indexed = metadata_only = nested = text_chars = issues = 0
    fts_rows_repaired = 0
    materialization_applied = materialization_reused = 0
    materialization_pending = materialization_collisions = 0
    materialization_units_preserved = 0
    materialization_manifest_digest: str | None = None

    def report(*, finished: bool = False) -> None:
        emit_progress(
            owner.progress,
            ProgressEvent(
                "archive",
                "extract",
                "ZIP indexados" if finished else "Indexando ZIP",
                processed,
                selected_count,
                "archivos ZIP",
                finished,
                (
                    ProgressMetric("cache_hits", cache_hits),
                    ProgressMetric("members", members),
                    ProgressMetric("nested_archives", nested),
                    ProgressMetric(
                        "materialized",
                        materialization_applied + materialization_reused,
                    ),
                    ProgressMetric("materialization_pending", materialization_pending),
                    ProgressMetric("issues", issues + errors),
                ),
            ),
        )

    with archive_database(owner.config.state_path, create=False) as connection:
        initialize_format_fts_lookup(
            connection, "document_fts", checkpoint=owner.cancellation.checkpoint
        )
        iterator = owner.framework_state.iter_selected_route_candidates(
            owner.run_id,
            ARCHIVE_MIME,
            "archive",
            owner.config.selection,
        )
        def selected_candidates():
            selected = 0
            for snapshot in iterator:
                if selected >= selected_count:
                    break
                owner.cancellation.checkpoint()
                if (
                    owner.config.max_file_bytes is not None
                    and snapshot.size > owner.config.max_file_bytes
                ):
                    continue
                selected += 1
                yield snapshot

        def reuse_cached(snapshot):
            nonlocal processed, cache_hits, cached_errors, complete, partial, errors
            nonlocal members, indexed, metadata_only, nested, text_chars, issues
            nonlocal fts_rows_repaired, materialization_applied, materialization_reused
            nonlocal materialization_pending, materialization_collisions
            nonlocal materialization_units_preserved, materialization_manifest_digest
            cached = _cached_container(
                connection,
                snapshot,
                owner.config.processing_signature,
            )
            retryable_error = (
                cached is not None
                and str(cached["status"]) == "error"
                and type(cached["retryable"]) is int
                and cached["retryable"] == 1
            )
            if cached is not None and not (
                str(cached["status"]) == "error"
                and (
                    owner.config.retry_errors
                    or (owner.config.retry_recoverable_errors and retryable_error)
                )
            ):
                connection.execute("BEGIN IMMEDIATE")
                repaired: int | None = None
                cached_materialization = _ContainerCounters()
                try:
                    repaired_count = _refresh_cached_container(
                        connection,
                        snapshot,
                        owner.run_id,
                        max_text_chars=owner.config.max_text_chars,
                    )
                    if owner.config.materialize_on_apply and str(cached["status"]) != "error":
                        owner._materialize_container(
                            connection,
                            snapshot,
                            file_key_from_snapshot(snapshot),
                            cached_materialization,
                        )
                except _ArchiveCacheInvalid:
                    # A missing/corrupt derived projection is repairable,
                    # but a corrupt durable representation must go through
                    # the normal bounded extraction path.
                    connection.rollback()
                    cached = None
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                    repaired = repaired_count
                if repaired is not None:
                    if cached is None:
                        raise RuntimeError("Archive cache row disappeared after refresh")
                    fts_rows_repaired += repaired
                    cache_hits += 1
                    status_value = str(cached["status"])
                    cached_errors += int(status_value == "error")
                    complete += int(status_value == "complete")
                    partial += int(status_value == "partial")
                    members += int(cached["member_count"])
                    indexed += int(cached["indexed_count"])
                    metadata_only += int(cached["metadata_only_count"])
                    nested += int(cached["nested_archive_count"])
                    issues += int(cached["issue_count"])
                    text_chars += int(cached["text_chars"])
                    materialization_applied += cached_materialization.materialization_applied
                    materialization_reused += cached_materialization.materialization_reused
                    materialization_pending += cached_materialization.materialization_pending
                    materialization_collisions += cached_materialization.materialization_collisions
                    materialization_units_preserved += (
                        cached_materialization.materialization_units_preserved
                    )
                    materialization_manifest_digest = (
                        cached_materialization.materialization_manifest_digest
                        or materialization_manifest_digest
                    )
                    issues += cached_materialization.issues
                    processed += 1
                    report()
                    return True

            return False

        def uncached_candidates():
            for snapshot in selected_candidates():
                if not reuse_cached(snapshot):
                    yield snapshot

        def prepared_containers():
            if not callable(getattr(owner.memory_gate, "worker_capacity", None)):
                yield from uncached_candidates()
                return
            from neocortex.runtime.control.elastic_workers import ImmediateResult, elastic_map

            group = _ArchiveContainerGroup()
            selected = iter(selected_candidates())
            exhausted = False
            retries: deque[_ArchiveContainerTask] = deque()

            def tasks():
                nonlocal exhausted
                while retries or not exhausted:
                    if retries:
                        yield retries.popleft()
                        continue
                    try:
                        snapshot = next(selected)
                    except StopIteration:
                        exhausted = True
                        return
                    # This indexed metadata lookup does not decode a durable
                    # representation. Cache verification/FTS repair is prepare.
                    cached = _cached_container(connection, snapshot, owner.config.processing_signature)
                    if cached is not None and str(cached["status"]) == "error" and (
                        owner.config.retry_errors or (
                            owner.config.retry_recoverable_errors and cached["retryable"] == 1
                        )
                    ):
                        cached = None
                    yield _ArchiveContainerTask(snapshot, owner.config, group, cached)

            def prepare(task: _ArchiveContainerTask):
                if task.cached is None:
                    return task
                try:
                    if reuse_cached(task.snapshot):
                        return ImmediateResult(None)
                    # Damaged durable cache needs a fresh, larger extraction
                    # admission after releasing its representation lease.
                    return ImmediateResult(replace(task, cached=None))
                finally:
                    # Cached apply may renew CPU around materialization.
                    # Close those owner contexts here: this preparation
                    # only returns ImmediateResult, so no parser follows.
                    from neocortex.runtime.control.global_resources import current_resource_grant

                    grant = current_resource_grant()
                    if grant is not None:
                        grant.release_cpu()

            def estimate(task: _ArchiveContainerTask):
                if task.cached is None:
                    return _archive_container_memory(owner.config)
                try:
                    chars = max(0, min(int(task.cached["text_chars"]), owner.config.max_total_text_chars))
                    count = max(0, min(int(task.cached["member_count"]), owner.config.max_members))
                except (ValueError, TypeError, OverflowError):
                    return _archive_container_memory(owner.config)
                # Canonical and actual FTS projections coexist; names and
                # ancestor paths are bounded by the archive naming contract.
                return 4 * 1024 * 1024 + chars * 16 + count * 96 * 1024

            worker: Callable[
                [_ArchiveContainerTask], _PreparedArchiveContainer | _ArchiveContainerTask | None
            ] = _extract_archive_container
            while not exhausted or retries:
                with elastic_map(
                    worker, tasks(), gate=owner.memory_gate,
                    capacity=lambda: _archive_container_capacity(owner.memory_gate, owner.config),
                    estimated_bytes=estimate, prepare=prepare,
                    phase="archive-container", native_threads=0, io_slots=1,
                    cancellation=owner.cancellation,
                ) as results:
                    for result in results:
                        if isinstance(result, _ArchiveContainerTask):
                            retries.append(result)
                        elif result is not None:
                            yield result

        with closing(prepared_containers()) as prepared_results:
            for prepared in prepared_results:
                snapshot = (
                    prepared.snapshot if isinstance(prepared, _PreparedArchiveContainer)
                    else prepared
                )
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        outcome = (
                            owner._publish_prepared_container(connection, prepared)
                            if isinstance(prepared, _PreparedArchiveContainer)
                            else owner._process_container(connection, snapshot)
                        )
                    except ArchiveExtractionError as failure:
                        connection.rollback()
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            _store_container_error(
                                connection,
                                snapshot,
                                owner.config.processing_signature,
                                owner.run_id,
                                failure,
                            )
                        except BaseException:
                            connection.rollback()
                            raise
                        else:
                            connection.commit()
                        errors += 1
                        issues += 1
                    except BaseException:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                        complete += int(outcome.status == "complete")
                        partial += int(outcome.status == "partial")
                        members += outcome.counters.members
                        indexed += outcome.counters.indexed
                        metadata_only += outcome.counters.metadata_only
                        nested += outcome.counters.nested_archives
                        issues += outcome.counters.issues
                        text_chars += outcome.counters.text_chars
                        materialization_applied += outcome.counters.materialization_applied
                        materialization_reused += outcome.counters.materialization_reused
                        materialization_pending += outcome.counters.materialization_pending
                        materialization_collisions += outcome.counters.materialization_collisions
                        materialization_units_preserved += (
                            outcome.counters.materialization_units_preserved
                        )
                        materialization_manifest_digest = (
                            outcome.counters.materialization_manifest_digest
                            or materialization_manifest_digest
                        )
                    processed += 1
                    report()
                finally:
                    if isinstance(prepared, _PreparedArchiveContainer) and prepared.spool is not None:
                        prepared.spool.close()

        pruned_containers = pruned_members = 0
        if (
            not owner.config.selection.active
            and owner.config.max_documents is None
            and owner.config.max_file_bytes is None
        ):
            connection.execute("BEGIN IMMEDIATE")
            try:
                pruned_containers, pruned_members = _prune_stale_containers(
                    connection,
                    owner.run_id,
                )
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
    report(finished=True)
    provenance = owner.config.processing_provenance
    return ArchiveRouteSummary(
        candidate_pool=candidate_pool,
        candidates=selected_count,
        skipped_by_size=candidate_pool - eligible,
        skipped_by_count=eligible - selected_count,
        processed=processed,
        cache_hits=cache_hits,
        fts_rows_repaired=fts_rows_repaired,
        cached_errors=cached_errors,
        containers_complete=complete,
        containers_partial=partial,
        errors=errors,
        members_seen=members,
        members_indexed=indexed,
        metadata_only=metadata_only,
        nested_archives=nested,
        text_chars=text_chars,
        safety_issues=issues,
        cache_containers_pruned=pruned_containers,
        cache_members_pruned=pruned_members,
        peak_reserved_bytes=(
            0 if owner.memory_gate is None else int(owner.memory_gate.peak_reserved_bytes)
        ),
        memory_waits=(0 if owner.memory_gate is None else int(owner.memory_gate.wait_count)),
        processing_signature=provenance.signature,
        processing_provenance=provenance.manifest,
        materialization_applied=materialization_applied,
        materialization_reused=materialization_reused,
        materialization_pending=materialization_pending,
        materialization_collisions=materialization_collisions,
        materialization_units_preserved=materialization_units_preserved,
        materialization_manifest_digest=materialization_manifest_digest,
    )
