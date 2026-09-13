"""DDL for the v8 owner-local generation-control projection.

The projection is deliberately derived from the existing Semantic tables.  It
does not add an event log, a watermark, a worker cache, or a second source of
truth for receipts and publication.  The migration bootstraps the live job
counters for ``building`` generations and the triggers keep those counters,
source-dirty hints, and cache-payload hints in the same SQLite transaction as
the write that caused them.

This module contains SQL only.  The schema owner applies the tuple through the
normal sequential migration runner in :mod:`semantic_schema`.
"""

from __future__ import annotations


SEMANTIC_GENERATION_CONTROL_SCHEMA_VERSION = 8


# v8 adds columns, one bootstrap pass, canonical lookup indexes, and triggers.
# There is intentionally no new durable table: ``source_dirty`` is a bounded
# per-job projection and ``cached_payload_id`` is only a lookup hint.
SEMANTIC_GENERATION_CONTROL_MIGRATION = (
    """ALTER TABLE embedding_jobs
        ADD COLUMN source_dirty INTEGER NOT NULL DEFAULT 1
        CHECK(source_dirty IN (0,1))""",
    """ALTER TABLE embedding_jobs
        ADD COLUMN cached_payload_id INTEGER
        REFERENCES vector_payloads(payload_id)""",
    """UPDATE embedding_jobs
        SET source_dirty=CASE
            WHEN status IN ('pending','leased') THEN 1 ELSE 0 END""",
    """UPDATE embedding_jobs
        SET cached_payload_id=(
            SELECT p.payload_id
            FROM vector_payloads p
            WHERE p.model_signature=embedding_jobs.model_signature
              AND p.content_xxh3_128=embedding_jobs.content_xxh3_128
              AND p.content_bytes=embedding_jobs.content_bytes
              AND p.content_xxh3_64_guard=embedding_jobs.content_xxh3_64_guard)""",
    # Only building generations receive a live bootstrap.  Stored counters on
    # ready/ready_partial/failed rows are immutable historical snapshots, in
    # particular for the v6 ready-with-members/no-jobs import shape.
    """UPDATE embedding_generations
        SET pending_count=(
                SELECT COUNT(*) FROM embedding_jobs j
                WHERE j.generation_id=embedding_generations.generation_id
                  AND j.status='pending'),
            leased_count=(
                SELECT COUNT(*) FROM embedding_jobs j
                WHERE j.generation_id=embedding_generations.generation_id
                  AND j.status='leased'),
            done_count=(
                SELECT COUNT(*) FROM embedding_jobs j
                WHERE j.generation_id=embedding_generations.generation_id
                  AND j.status='done'),
            error_count=(
                SELECT COUNT(*) FROM embedding_jobs j
                WHERE j.generation_id=embedding_generations.generation_id
                  AND j.status='error'),
            stale_count=(
                SELECT COUNT(*) FROM embedding_jobs j
                WHERE j.generation_id=embedding_generations.generation_id
                  AND j.status='stale')
        WHERE status='building'""",
    """CREATE INDEX embedding_jobs_source_dirty_idx
        ON embedding_jobs(generation_id,status,job_id)
        WHERE source_dirty=1""",
    """CREATE INDEX embedding_jobs_cached_pending_idx
        ON embedding_jobs(generation_id,job_id)
        WHERE status='pending' AND cached_payload_id IS NOT NULL""",
    """CREATE INDEX embedding_jobs_source_idx
        ON embedding_jobs(entity_kind,entity_id,status)""",
    """CREATE INDEX embedding_jobs_lease_expiry_idx
        ON embedding_jobs(generation_id,status,lease_until_ns,job_id)""",
    # A refresh token spans the source cohort, not one item.  Publication is
    # per item/receipt, so these predicates must not rescan that whole cohort
    # for every item while staging the generation.
    """CREATE INDEX semantic_chunk_derivations_item_refresh_idx
        ON semantic_chunk_derivations(
            item_revision_id,refresh_token,chunk_revision_id)""",
    """CREATE INDEX semantic_chunk_derivations_publication_idx
        ON semantic_chunk_derivations(publication_receipt_id,chunk_revision_id)""",
    # Live counters: an INSERT contributes its status only while its target
    # generation is building.  Terminal snapshots are intentionally untouched.
    """CREATE TRIGGER embedding_jobs_generation_counts_insert
        AFTER INSERT ON embedding_jobs
        WHEN EXISTS(
            SELECT 1 FROM embedding_generations g
            WHERE g.generation_id=NEW.generation_id AND g.status='building')
        BEGIN
            UPDATE embedding_generations
            SET pending_count=pending_count+
                    CASE WHEN NEW.status='pending' THEN 1 ELSE 0 END,
                leased_count=leased_count+
                    CASE WHEN NEW.status='leased' THEN 1 ELSE 0 END,
                done_count=done_count+
                    CASE WHEN NEW.status='done' THEN 1 ELSE 0 END,
                error_count=error_count+
                    CASE WHEN NEW.status='error' THEN 1 ELSE 0 END,
                stale_count=stale_count+
                    CASE WHEN NEW.status='stale' THEN 1 ELSE 0 END
            WHERE generation_id=NEW.generation_id AND status='building';
        END""",
    """CREATE TRIGGER embedding_jobs_generation_counts_update
        AFTER UPDATE OF status,generation_id ON embedding_jobs
        WHEN OLD.status IS NOT NEW.status
          OR OLD.generation_id IS NOT NEW.generation_id
        BEGIN
            UPDATE embedding_generations
            SET pending_count=pending_count-
                    CASE WHEN OLD.status='pending' THEN 1 ELSE 0 END,
                leased_count=leased_count-
                    CASE WHEN OLD.status='leased' THEN 1 ELSE 0 END,
                done_count=done_count-
                    CASE WHEN OLD.status='done' THEN 1 ELSE 0 END,
                error_count=error_count-
                    CASE WHEN OLD.status='error' THEN 1 ELSE 0 END,
                stale_count=stale_count-
                    CASE WHEN OLD.status='stale' THEN 1 ELSE 0 END
            WHERE generation_id=OLD.generation_id AND status='building';
            UPDATE embedding_generations
            SET pending_count=pending_count+
                    CASE WHEN NEW.status='pending' THEN 1 ELSE 0 END,
                leased_count=leased_count+
                    CASE WHEN NEW.status='leased' THEN 1 ELSE 0 END,
                done_count=done_count+
                    CASE WHEN NEW.status='done' THEN 1 ELSE 0 END,
                error_count=error_count+
                    CASE WHEN NEW.status='error' THEN 1 ELSE 0 END,
                stale_count=stale_count+
                    CASE WHEN NEW.status='stale' THEN 1 ELSE 0 END
            WHERE generation_id=NEW.generation_id AND status='building';
        END""",
    """CREATE TRIGGER embedding_jobs_generation_counts_delete
        AFTER DELETE ON embedding_jobs
        WHEN EXISTS(
            SELECT 1 FROM embedding_generations g
            WHERE g.generation_id=OLD.generation_id AND g.status='building')
        BEGIN
            UPDATE embedding_generations
            SET pending_count=pending_count-
                    CASE WHEN OLD.status='pending' THEN 1 ELSE 0 END,
                leased_count=leased_count-
                    CASE WHEN OLD.status='leased' THEN 1 ELSE 0 END,
                done_count=done_count-
                    CASE WHEN OLD.status='done' THEN 1 ELSE 0 END,
                error_count=error_count-
                    CASE WHEN OLD.status='error' THEN 1 ELSE 0 END,
                stale_count=stale_count-
                    CASE WHEN OLD.status='stale' THEN 1 ELSE 0 END
            WHERE generation_id=OLD.generation_id AND status='building';
        END""",
    # Job writes provide both hints.  The explicit UPDATE makes the insert
    # contract hold even if a caller supplies a terminal status or a nondefault
    # source_dirty value: only live jobs are dirty.  It does not touch updated_ns
    # or any worker fields.
    """CREATE TRIGGER embedding_jobs_control_insert
        AFTER INSERT ON embedding_jobs
        BEGIN
            UPDATE embedding_jobs
            SET source_dirty=CASE
                    WHEN NEW.status IN ('pending','leased') THEN 1 ELSE 0 END,
                cached_payload_id=(
                    SELECT p.payload_id
                    FROM vector_payloads p
                    WHERE p.model_signature=NEW.model_signature
                      AND p.content_xxh3_128=NEW.content_xxh3_128
                      AND p.content_bytes=NEW.content_bytes
                      AND p.content_xxh3_64_guard=NEW.content_xxh3_64_guard)
            WHERE job_id=NEW.job_id;
        END""",
    # The source columns below are the fields whose change can alter the
    # current-job identity.  model_signature is included so a model change
    # refreshes the cache hint in the same write without affecting operational
    # timestamps.  IS NOT gives a real value comparison, including NULL-safe
    # behavior if a legacy caller ever supplies one to a nullable field.
    """CREATE TRIGGER embedding_jobs_control_update
        AFTER UPDATE OF status,entity_kind,entity_id,item_id,model_signature,
            content_xxh3_128,content_bytes,content_xxh3_64_guard ON embedding_jobs
        WHEN OLD.status IS NOT NEW.status
          OR OLD.entity_kind IS NOT NEW.entity_kind
          OR OLD.entity_id IS NOT NEW.entity_id
          OR OLD.item_id IS NOT NEW.item_id
          OR OLD.model_signature IS NOT NEW.model_signature
          OR OLD.content_xxh3_128 IS NOT NEW.content_xxh3_128
          OR OLD.content_bytes IS NOT NEW.content_bytes
          OR OLD.content_xxh3_64_guard IS NOT NEW.content_xxh3_64_guard
        BEGIN
            UPDATE embedding_jobs
            SET source_dirty=CASE
                    WHEN NEW.status IN ('pending','leased') THEN 1 ELSE 0 END,
                cached_payload_id=(
                    SELECT p.payload_id
                    FROM vector_payloads p
                    WHERE p.model_signature=NEW.model_signature
                      AND p.content_xxh3_128=NEW.content_xxh3_128
                      AND p.content_bytes=NEW.content_bytes
                      AND p.content_xxh3_64_guard=NEW.content_xxh3_64_guard)
            WHERE job_id=NEW.job_id;
        END""",
    """CREATE TRIGGER vector_payloads_embedding_jobs_cache_hint
        AFTER INSERT ON vector_payloads
        BEGIN
            UPDATE embedding_jobs
            SET cached_payload_id=NEW.payload_id
            WHERE model_signature=NEW.model_signature
              AND content_xxh3_128=NEW.content_xxh3_128
              AND content_bytes=NEW.content_bytes
              AND content_xxh3_64_guard=NEW.content_xxh3_64_guard;
        END""",
    # Source fanout is intentionally a boolean hint.  It never changes job
    # status, creates receipts, or appends an outbox event.  The text fanout is
    # joined by text_chunks.item_id -> embedding_jobs.entity_id rather than by
    # embedding_jobs.item_id, because the latter may be an old/rebound value.
    # CROSS JOIN keeps the item-local chunks outermost: INDEXED BY alone can
    # still let SQLite scan every text job before looking up each chunk.
    """CREATE TRIGGER semantic_items_embedding_jobs_source_dirty_insert
        AFTER INSERT ON semantic_items
        BEGIN
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE status IN ('pending','leased')
              AND entity_kind='image_item'
              AND entity_id=NEW.item_id;
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE job_id IN (
                SELECT j.job_id
                FROM text_chunks c
                CROSS JOIN embedding_jobs j INDEXED BY embedding_jobs_source_idx
                  ON j.entity_kind='text_chunk' AND j.entity_id=c.chunk_id
                WHERE c.item_id=NEW.item_id
                  AND j.status IN ('pending','leased'));
        END""",
    """CREATE TRIGGER semantic_items_embedding_jobs_source_dirty_update
        AFTER UPDATE OF item_id,source_kind,source_identity,identity_version,
            path,content_xxh3_128,content_bytes,content_xxh3_64_guard,active
            ON semantic_items
        WHEN OLD.item_id IS NOT NEW.item_id
          OR OLD.source_kind IS NOT NEW.source_kind
          OR OLD.source_identity IS NOT NEW.source_identity
          OR OLD.identity_version IS NOT NEW.identity_version
          OR OLD.content_xxh3_128 IS NOT NEW.content_xxh3_128
          OR OLD.content_bytes IS NOT NEW.content_bytes
          OR OLD.content_xxh3_64_guard IS NOT NEW.content_xxh3_64_guard
          OR OLD.active IS NOT NEW.active
          OR ((OLD.path IS NULL) <> (NEW.path IS NULL))
        BEGIN
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE status IN ('pending','leased')
              AND entity_kind='image_item'
              AND entity_id IN (OLD.item_id,NEW.item_id);
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE job_id IN (
                SELECT j.job_id
                FROM text_chunks c
                CROSS JOIN embedding_jobs j INDEXED BY embedding_jobs_source_idx
                  ON j.entity_kind='text_chunk' AND j.entity_id=c.chunk_id
                WHERE c.item_id IN (OLD.item_id,NEW.item_id)
                  AND j.status IN ('pending','leased'));
        END""",
    """CREATE TRIGGER semantic_items_embedding_jobs_source_dirty_delete
        AFTER DELETE ON semantic_items
        BEGIN
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE status IN ('pending','leased')
              AND entity_kind='image_item'
              AND entity_id=OLD.item_id;
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE job_id IN (
                SELECT j.job_id
                FROM text_chunks c
                CROSS JOIN embedding_jobs j INDEXED BY embedding_jobs_source_idx
                  ON j.entity_kind='text_chunk' AND j.entity_id=c.chunk_id
                WHERE c.item_id=OLD.item_id
                  AND j.status IN ('pending','leased'));
        END""",
    """CREATE TRIGGER text_chunks_embedding_jobs_source_dirty_insert
        AFTER INSERT ON text_chunks
        BEGIN
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE status IN ('pending','leased')
              AND entity_kind='text_chunk'
              AND entity_id=NEW.chunk_id;
        END""",
    """CREATE TRIGGER text_chunks_embedding_jobs_source_dirty_update
        AFTER UPDATE OF chunk_id,item_id,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,active ON text_chunks
        WHEN OLD.chunk_id IS NOT NEW.chunk_id
          OR OLD.item_id IS NOT NEW.item_id
          OR OLD.content_xxh3_128 IS NOT NEW.content_xxh3_128
          OR OLD.content_bytes IS NOT NEW.content_bytes
          OR OLD.content_xxh3_64_guard IS NOT NEW.content_xxh3_64_guard
          OR OLD.active IS NOT NEW.active
        BEGIN
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE status IN ('pending','leased')
              AND entity_kind='text_chunk'
              AND entity_id IN (OLD.chunk_id,NEW.chunk_id);
        END""",
    """CREATE TRIGGER text_chunks_embedding_jobs_source_dirty_delete
        AFTER DELETE ON text_chunks
        BEGIN
            UPDATE embedding_jobs
            SET source_dirty=1
            WHERE status IN ('pending','leased')
              AND entity_kind='text_chunk'
              AND entity_id=OLD.chunk_id;
        END""",
)


__all__ = (
    "SEMANTIC_GENERATION_CONTROL_MIGRATION",
    "SEMANTIC_GENERATION_CONTROL_SCHEMA_VERSION",
)
