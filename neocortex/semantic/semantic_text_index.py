"""Incremental text indexing from durable extraction caches."""

from __future__ import annotations
import itertools
import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from .semantic_chunking import TextChunkingConfig, TextTokenCounter
from .semantic_config import (
    SEMANTIC_PIPELINE_VERSION,
    multilingual_text_model,
    text_chunking_for_model,
)
from .semantic_generation_repository import (
    _enqueue_text_chunk_batch_bounded,
    find_exact_published_generation,
    has_building_embedding_generation,
    invalidate_embedding_generations_for_source_change,
    merge_source_head_ledger,
    published_source_head_ledger,
)
from .semantic_generation_worker import GenerationRunner
from .semantic_item_repository import (
    _finalize_semantic_item_refresh,
    _finalize_text_chunk_refresh,
    _stage_text_chunk_batch,
    _upsert_item,
)
from .semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    SemanticItem,
    TextSection,
    canonical_json,
)
from .semantic_preparation import (
    BackendFactory,
    initialize_models,
    model_cache,
    require_source_databases,
    resolve_text_token_guard,
    text_probe,
)
from .semantic_quality import SEMANTIC_TEXT_QUALITY_POLICY, iter_semantic_text_chunks
from .semantic_service_contracts import (
    SEMANTIC_PLAN_TEXT_SOURCE_KINDS,
    SEMANTIC_DATABASE_NAME,
    STAGING_BATCH_SIZE,
    GenerationWorkResult,
    SemanticIndexResult,
)
from .semantic_sources import (
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
    SemanticSourceHead,
    TextSourceRecord,
    iter_text_sections_with_metadata,
    semantic_source_heads,
    _source_head_query,
    semantic_text_processing_signature,
    require_readable_source_heads,
)
from .semantic_schema import SemanticStateError, semantic_database
from .semantic_state import (
    generation_summary,
    prepare_embedding_generation,
    start_embedding_generation,
    update_embedding_generation_cursor,
)
from .semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
    unlimited_semantic_work_budget,
)
from neocortex.persistence.sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)

TextRecordIterator = Callable[[Path, str], Iterator[TextSourceRecord]]
SEMANTIC_PROGRESS_ITEM_INTERVAL = 25


_TEXT_REPLAY_CONTRACT_KEYS = (
    "channel",
    "source_kinds",
    "pipeline",
    "base_chunking_signature",
    "title_policy",
    "text_quality_policy",
)
_SEMANTIC_OBSERVATION_KEYS = frozenset(
    {
        "last_seen_run_id",
        "first_observed_run_id",
        "last_observed_run_id",
    }
)


@dataclass(frozen=True, slots=True)
class _PublishedTextDelta:
    """Published text baseline used for source/item-level delta staging."""

    generation_id: int
    reusable_sources: frozenset[str]
    provenance: Mapping[str, object]


def _source_head_map(raw_heads: object) -> dict[str, dict[str, object]] | None:
    """Decode one source-head list without accepting duplicates or omissions."""

    if not isinstance(raw_heads, list):
        return None
    result: dict[str, dict[str, object]] = {}
    for raw_head in raw_heads:
        if not isinstance(raw_head, Mapping):
            return None
        source_kind = raw_head.get("source_kind")
        if not isinstance(source_kind, str) or not source_kind.strip():
            return None
        if source_kind in result:
            return None
        result[source_kind] = dict(raw_head)
    return result


def _text_replay_contract_matches(
    provenance: Mapping[str, object],
    *,
    replay_scope: str,
    current_entry: Mapping[str, object],
    selected_sources: Sequence[str],
) -> bool:
    """Require the full text projection contract before relaxing freshness."""

    ledger = provenance.get("source_head_ledger")
    if not isinstance(ledger, Mapping):
        return False
    raw_entry = ledger.get(replay_scope)
    if not isinstance(raw_entry, Mapping):
        return False
    for key in _TEXT_REPLAY_CONTRACT_KEYS:
        if raw_entry.get(key) != current_entry.get(key):
            return False
    if provenance.get("sources") != list(selected_sources):
        return False
    for key in _TEXT_REPLAY_CONTRACT_KEYS:
        if key == "channel" or key == "source_kinds":
            continue
        if provenance.get(key) != current_entry.get(key):
            return False
    if raw_entry.get("channel") != "text" or raw_entry.get("source_kinds") != list(
        selected_sources
    ):
        return False
    return True


def _published_text_source_delta(
    database: Path,
    *,
    model_signature: str,
    replay_scope: str,
    replay_entry: Mapping[str, object],
    selected_sources: Sequence[str],
    current_heads: Sequence[SemanticSourceHead],
) -> _PublishedTextDelta | None:
    """Return unchanged sources from the ready base, or abstain fail-closed.

    The source head is the cheap owner-level guard.  Only after the complete
    projection contract matches do we use it to avoid enumerating a source;
    changed sources are still enumerated and compared item by item below.
    """

    if not database.is_file():
        return None
    try:
        with semantic_database(database, readonly=True) as connection:
            row = connection.execute(
                """SELECT g.generation_id,g.status,g.provenance_json
                FROM published_embedding_heads head
                JOIN embedding_generations g ON g.generation_id=head.generation_id
                WHERE head.model_signature=? AND g.model_signature=?""",
                (model_signature, model_signature),
            ).fetchone()
            if row is None or str(row["status"]) != "ready":
                return None
            provenance = json.loads(str(row["provenance_json"]))
            if not isinstance(provenance, dict):
                return None
            ledger = provenance.get("source_head_ledger")
            if not isinstance(ledger, Mapping):
                return None
            selected = set(selected_sources)
            candidates: list[tuple[bool, int, str, Mapping[str, object]]] = []
            for scope, raw_entry in ledger.items():
                if not isinstance(scope, str) or not isinstance(raw_entry, Mapping):
                    continue
                if raw_entry.get("channel") != "text":
                    continue
                raw_sources = raw_entry.get("source_kinds")
                if not isinstance(raw_sources, list) or not all(
                    isinstance(source, str) for source in raw_sources
                ):
                    continue
                if not selected.issubset(set(raw_sources)):
                    continue
                if any(
                    raw_entry.get(key) != replay_entry.get(key)
                    for key in _TEXT_REPLAY_CONTRACT_KEYS
                    if key not in {"source_kinds"}
                ):
                    continue
                provenance_sources = provenance.get("sources")
                if not isinstance(provenance_sources, list) or not selected.issubset(
                    {value for value in provenance_sources if isinstance(value, str)}
                ):
                    continue
                candidates.append(
                    (
                        scope == replay_scope,
                        len(raw_sources),
                        scope,
                        raw_entry,
                    )
                )
            if not candidates:
                return None
            _exact_scope, _scope_size, _scope_name, entry = sorted(
                candidates,
                key=lambda value: (not value[0], value[1], value[2]),
            )[0]
            old_raw_heads = entry.get("source_heads")
            old_by_kind = _source_head_map(old_raw_heads)
            current_by_kind = _source_head_map(
                [head.as_payload() for head in current_heads]
            )
            if old_by_kind is None or current_by_kind is None:
                return None
            if (
                not selected.issubset(set(old_by_kind))
                or set(current_by_kind) != selected
                or provenance.get("source_heads") != old_raw_heads
            ):
                return None
            if any(
                not isinstance(head, Mapping) or head.get("complete") is not True
                for head in current_by_kind.values()
            ):
                return None
            reusable_sources = frozenset(
                source_kind
                for source_kind in selected_sources
                if old_by_kind[source_kind] == current_by_kind[source_kind]
            )
            return _PublishedTextDelta(
                int(row["generation_id"]),
                reusable_sources,
                provenance,
            )
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None


def _candidate_base_generation_id(
    database: Path,
    generation_id: int,
) -> int | None:
    """Read the candidate's pinned base, abstaining on any state error."""

    try:
        with semantic_database(database, readonly=True) as connection:
            row = connection.execute(
                "SELECT base_generation_id FROM embedding_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if row is None or row["base_generation_id"] is None:
                return None
            return int(row["base_generation_id"])
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None


def _semantic_source_revision_json(value: object) -> str:
    """Ignore owner observation clocks that are absent from source heads."""

    if isinstance(value, Mapping):
        value = {
            key: selected
            for key, selected in value.items()
            if key not in _SEMANTIC_OBSERVATION_KEYS
        }
    return canonical_json(value)


def _content_compatible_text_replay(
    state_directory: Path,
    connection: sqlite3.Connection,
    generation_id: int,
    provenance: dict[str, object],
    *,
    selected_sources: tuple[str, ...],
    current_heads: Sequence[SemanticSourceHead],
    replay_scope: str,
    current_entry: Mapping[str, object],
) -> bool:
    """Allow replay after route-signature churn only when text content is stable.

    Text route cache hits can refresh an extractor signature without changing
    the durable text representation.  Published Semantic item revisions retain
    the exact materialization fingerprint and physical revision, so compare
    those facts directly before accepting the existing vector head.  Other
    source heads still require exact equality.
    """

    if "text" not in selected_sources:
        return False
    if not _text_replay_contract_matches(
        provenance,
        replay_scope=replay_scope,
        current_entry=current_entry,
        selected_sources=selected_sources,
    ):
        return False
    ledger = provenance.get("source_head_ledger")
    if not isinstance(ledger, Mapping):
        return False
    entry = ledger.get(replay_scope)
    if not isinstance(entry, Mapping):
        return False
    old_heads = entry.get("source_heads")
    if not isinstance(old_heads, list):
        return False
    current_payloads = [head.as_payload() for head in current_heads]
    old_by_kind = _source_head_map(old_heads)
    current_by_kind = _source_head_map(current_payloads)
    if old_by_kind is None or current_by_kind is None:
        return False
    if set(old_by_kind) != set(current_by_kind) or set(old_by_kind) != set(selected_sources):
        return False
    for source_kind, old_head in old_by_kind.items():
        if source_kind != "text" and old_head != current_by_kind[source_kind]:
            return False

    from neocortex.persistence.sqlite_immutable import immutable_sqlite_database
    from .semantic_sources import semantic_source_database

    owner = semantic_source_database(state_directory, "text")
    try:
        with immutable_sqlite_database(owner, timeout_seconds=30) as owner_connection:
            query, parameters = _source_head_query(owner_connection, "text")
            current_rows = owner_connection.execute(query, parameters).fetchall()
    except (OSError, sqlite3.DatabaseError):
        return False
    current_by_identity = {str(row["file_key"]): row for row in current_rows}
    if not current_by_identity:
        return False
    published_rows = connection.execute(
        """SELECT DISTINCT revision.source_identity,revision.path,
            revision.source_revision_json,revision.provenance_json
        FROM embedding_generation_members member
        JOIN semantic_item_revisions revision
          ON revision.item_revision_id=member.item_revision_id
        WHERE member.generation_id=? AND member.entity_kind='text_chunk'
          AND revision.source_kind='text'
        ORDER BY revision.source_identity""",
        (generation_id,),
    ).fetchall()
    if len(published_rows) != len(current_by_identity):
        return False
    for revision in published_rows:
        identity = str(revision["source_identity"])
        current = current_by_identity.get(identity)
        if current is None or str(revision["path"]) != str(current["path"]):
            return False
        try:
            source_revision = json.loads(str(revision["source_revision_json"]))
            source_provenance = json.loads(str(revision["provenance_json"]))
            owner_revision = source_revision["owner_revision"]
            owner_fingerprint = str(owner_revision["fingerprint"])
            if (
                int(source_revision["size"]) != int(current["size"])
                or int(source_revision["mtime_ns"]) != int(current["mtime_ns"])
                or int(source_revision["birthtime_ns"]) != int(current["birthtime_ns"])
                or owner_fingerprint != str(current["text_xxh3_128"])
            ):
                return False
            for provenance_key, current_key in (("source_title", "title"), ("source_author", "author")):
                if provenance_key in source_provenance and source_provenance[provenance_key] != current[current_key]:
                    return False
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
    return True


# region [01] Source grouping and staging


def grouped_text_records(
    state_directory: Path,
    source_kind: str,
    *,
    source_record_iterator: TextRecordIterator,
) -> Iterator[tuple[str, Iterator[TextSourceRecord]]]:
    records = source_record_iterator(state_directory, source_kind)
    return itertools.groupby(records, key=lambda record: record.item.item_id)


class _SemanticTextStagingSession:
    """Stage one source through one connection and bounded transactions."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        generation_id: int,
        source_kind: str,
        refresh_token: str,
        chunking: TextChunkingConfig,
        token_counter: TextTokenCounter | None = None,
        cancellation: SQLiteCancellationBridge,
        work_budget: SemanticWorkBudget,
    ) -> None:
        self._connection = connection
        self._generation_id = generation_id
        self._source_kind = source_kind
        self._refresh_token = refresh_token
        self._chunking = chunking
        self._token_counter = token_counter
        self._cancellation = cancellation
        self._work_budget = work_budget
        self._transaction_chunks = 0
        self._transaction_items = 0

    def _begin(self) -> None:
        self._cancellation.checkpoint()
        if not self._connection.in_transaction:
            self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        if not self._connection.in_transaction:
            return
        self._cancellation.checkpoint()
        self._connection.commit()
        self._transaction_chunks = 0
        self._transaction_items = 0

    def stage_item(
        self,
        item: SemanticItem,
        sections: Iterable[TextSection],
    ) -> tuple[int, int, int, bool]:
        """Stage one item while committing oversized work in bounded slices."""

        self._begin()
        _upsert_item(
            self._connection,
            item,
            refresh_token=self._refresh_token,
            updated_ns=time.time_ns(),
            invalidate_text_on_fingerprint_change=True,
        )
        chunks_staged = queued = new_jobs = 0
        chunks = iter_semantic_text_chunks(
            item.item_id,
            iter_text_sections_with_metadata(item, sections),
            self._chunking,
            token_counter=self._token_counter,
        )
        while True:
            capacity = STAGING_BATCH_SIZE - self._transaction_chunks
            batch = tuple(itertools.islice(chunks, capacity))
            if not batch:
                break
            self._begin()
            selected_ns = time.time_ns()
            chunks_staged += _stage_text_chunk_batch(
                self._connection,
                batch,
                refresh_token=self._refresh_token,
                updated_ns=selected_ns,
            )
            allowance = self._work_budget.new_job_allowance(STAGING_BATCH_SIZE)
            enqueue_result = _enqueue_text_chunk_batch_bounded(
                self._connection,
                self._generation_id,
                tuple(chunk.chunk_id for chunk in batch),
                max_new_jobs=allowance,
                now_ns=selected_ns,
            )
            queued += enqueue_result.touched
            new_jobs += enqueue_result.new_jobs
            self._work_budget.record_new_jobs(enqueue_result.new_jobs)
            self._work_budget.record_rebound_members(enqueue_result.rebound_members)
            self._transaction_chunks += len(batch)
            self._cancellation.checkpoint()
            if not enqueue_result.complete:
                self._work_budget.mark_job_limit()
                self._commit()
                return chunks_staged, queued, new_jobs, False
            if self._transaction_chunks >= STAGING_BATCH_SIZE:
                self._commit()

        self._begin()
        _finalize_text_chunk_refresh(
            self._connection,
            item_id=item.item_id,
            chunking_signature=self._chunking.signature,
            refresh_token=self._refresh_token,
            updated_ns=time.time_ns(),
        )
        self._transaction_items += 1
        self._cancellation.checkpoint()
        if self._transaction_items >= STAGING_BATCH_SIZE:
            self._commit()
        return chunks_staged, queued, new_jobs, True

    def finalize_source(self) -> None:
        """Deactivate unseen source members only after the refresh completed."""

        self._begin()
        _finalize_semantic_item_refresh(
            self._connection,
            source_kind=self._source_kind,
            refresh_token=self._refresh_token,
            updated_ns=time.time_ns(),
        )
        self._commit()


def _semantic_item_revision_key(item: SemanticItem) -> tuple[object, ...]:
    """Return the immutable semantic materialization identity for one item."""

    return (
        item.item_id,
        item.source_kind,
        item.source_identity,
        item.identity_version,
        item.path,
        item.fingerprint.xxh3_128,
        item.fingerprint.byte_count,
        item.fingerprint.xxh3_64_guard,
        canonical_json(item.provenance),
        _semantic_source_revision_json(item.source_revision),
    )


def _decode_json_object(raw: object) -> object:
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return raw


def _semantic_item_revision_row_key(row: sqlite3.Row) -> tuple[object, ...]:
    return (
        str(row["item_id"]),
        str(row["source_kind"]),
        str(row["source_identity"]),
        str(row["identity_version"]),
        None if row["path"] is None else str(row["path"]),
        str(row["content_xxh3_128"]),
        int(row["content_bytes"]),
        str(row["content_xxh3_64_guard"]),
        str(row["provenance_json"]),
        _semantic_source_revision_json(_decode_json_object(row["source_revision_json"])),
    )


def _published_item_revision_keys(
    connection: sqlite3.Connection,
    generation_id: int,
    source_kind: str,
) -> dict[str, tuple[object, ...] | None]:
    """Load base item revisions, marking conflicting historical bindings unsafe."""

    rows = connection.execute(
        """SELECT member.item_id AS member_item_id,
            revision.item_id,revision.source_kind,revision.source_identity,
            revision.identity_version,revision.path,revision.content_xxh3_128,
            revision.content_bytes,revision.content_xxh3_64_guard,
            revision.provenance_json,revision.source_revision_json
        FROM embedding_generation_members member
        JOIN semantic_item_revisions revision
          ON revision.item_revision_id=member.item_revision_id
        WHERE member.generation_id=? AND member.entity_kind='text_chunk'
          AND revision.source_kind=?
        ORDER BY member.item_id,revision.item_revision_id""",
        (generation_id, source_kind),
    ).fetchall()
    revisions: dict[str, tuple[object, ...] | None] = {}
    for row in rows:
        item_id = str(row["member_item_id"])
        key = _semantic_item_revision_row_key(row)
        prior = revisions.get(item_id)
        if prior is None and item_id in revisions:
            continue
        if prior is not None and prior != key:
            revisions[item_id] = None
        else:
            revisions[item_id] = key
    return revisions


def _mark_unchanged_item_seen(
    connection: sqlite3.Connection,
    item: SemanticItem,
    *,
    base_revision: tuple[object, ...] | None,
    refresh_token: str,
) -> bool:
    """Keep an unchanged item active without rebuilding its chunks."""

    if base_revision is None or not refresh_token.strip():
        return False
    current_key = _semantic_item_revision_key(item)
    if base_revision != current_key:
        return False
    current = connection.execute(
        """SELECT item_id,source_kind,source_identity,identity_version,path,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,source_revision_json
        FROM semantic_items
        WHERE item_id=? AND source_kind=? AND active=1""",
        (item.item_id, item.source_kind),
    ).fetchone()
    if current is None or _semantic_item_revision_row_key(current) != current_key:
        return False
    updated = connection.execute(
        """UPDATE semantic_items SET refresh_token=?,updated_ns=?
        WHERE item_id=? AND source_kind=? AND active=1""",
        (refresh_token, time.time_ns(), item.item_id, item.source_kind),
    )
    return updated.rowcount == 1


def _stage_source(
    database: Path,
    state_directory: Path,
    source_kind: str,
    *,
    generation_id: int,
    refresh_token: str,
    chunking: TextChunkingConfig,
    token_counter: TextTokenCounter | None = None,
    source_record_iterator: TextRecordIterator,
    base_generation_id: int | None = None,
    work_budget: SemanticWorkBudget | None = None,
    cancellation_check: CancellationCheck | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[int, int, int, bool]:
    if not source_kind.strip() or not refresh_token.strip():
        raise ValueError("source_kind and refresh_token cannot be blank")
    budget = work_budget or unlimited_semantic_work_budget()
    source_items = chunks_staged = queued = 0
    source_new_jobs_before = budget.new_jobs_admitted
    groups = grouped_text_records(
        state_directory,
        source_kind,
        source_record_iterator=source_record_iterator,
    )

    def staging_checkpoint() -> None:
        if cancellation_check is not None:
            cancellation_check()
        budget.checkpoint()

    bridge = SQLiteCancellationBridge(staging_checkpoint)
    emit_progress(
        progress,
        ProgressEvent(
            "semantic",
            f"stage:{source_kind}",
            f"Preparando texto {source_kind.upper()}",
            0,
            None,
            "documentos",
            metrics=(
                ProgressMetric("chunks", 0),
                ProgressMetric("queued_work", 0),
                ProgressMetric("new_jobs", 0),
            ),
        ),
    )
    with semantic_database(database) as connection:
        with sqlite_cancellation_scope(connection, bridge):
            base_revisions = (
                _published_item_revision_keys(connection, base_generation_id, source_kind)
                if base_generation_id is not None
                else {}
            )
            session = _SemanticTextStagingSession(
                connection,
                generation_id=generation_id,
                source_kind=source_kind,
                refresh_token=refresh_token,
                chunking=chunking,
                token_counter=token_counter,
                cancellation=bridge,
                work_budget=budget,
            )
            source_complete = True
            for _item_id, grouped in groups:
                bridge.checkpoint()
                iterator = iter(grouped)
                first = next(iterator)
                item = first.item
                unchanged = _mark_unchanged_item_seen(
                    connection,
                    item,
                    base_revision=base_revisions.get(item.item_id),
                    refresh_token=refresh_token,
                )
                if unchanged:
                    for _record in iterator:
                        bridge.checkpoint()
                    continue
                if not budget.try_admit_item():
                    source_complete = False
                    break
                item_rebounds_before = budget.rebound_members
                sections = itertools.chain(
                    (first.section,),
                    (record.section for record in iterator),
                )
                item_chunks, item_jobs, item_new_jobs, item_complete = session.stage_item(
                    item, sections
                )
                source_items += 1
                chunks_staged += item_chunks
                queued += item_jobs
                if source_items % SEMANTIC_PROGRESS_ITEM_INTERVAL == 0:
                    emit_progress(
                        progress,
                        ProgressEvent(
                            "semantic",
                            f"stage:{source_kind}",
                            f"Preparando texto {source_kind.upper()}",
                            source_items,
                            None,
                            "documentos",
                            metrics=(
                                ProgressMetric("chunks", chunks_staged),
                                ProgressMetric("queued_work", queued),
                                ProgressMetric(
                                    "new_jobs",
                                    budget.new_jobs_admitted - source_new_jobs_before,
                                ),
                            ),
                        ),
                    )
                if not item_complete:
                    source_complete = False
                    break
                if item_new_jobs == 0 and budget.rebound_members == item_rebounds_before:
                    budget.refund_replayed_item()
            if source_complete:
                session.finalize_source()
    emit_progress(
        progress,
        ProgressEvent(
            "semantic",
            f"stage:{source_kind}",
            (
                f"Texto {source_kind.upper()} preparado"
                if source_complete
                else f"Texto {source_kind.upper()} pausado"
            ),
            source_items,
            source_items,
            "documentos",
            True,
            (
                ProgressMetric("chunks", chunks_staged),
                ProgressMetric("queued_work", queued),
                ProgressMetric(
                    "new_jobs",
                    budget.new_jobs_admitted - source_new_jobs_before,
                ),
            ),
        ),
    )
    return source_items, chunks_staged, queued, source_complete


# endregion [01]


# region [02] Text indexing orchestration


def index_text_embeddings(
    state_directory: Path,
    *,
    source_kinds: Sequence[str],
    model: EmbeddingModelSpec | None,
    model_cache_override: Path | None,
    local_files_only: bool,
    threads: int | None,
    chunking: TextChunkingConfig | None,
    backend_factory: BackendFactory,
    source_record_iterator: TextRecordIterator,
    generation_runner: GenerationRunner,
    work_budget: SemanticWorkBudget | None = None,
    progress: ProgressCallback | None = None,
) -> SemanticIndexResult:
    """Incrementally embed extracted text; source files are never rescanned."""

    budget = work_budget or unlimited_semantic_work_budget()
    new_jobs_before = budget.new_jobs_admitted
    selected_sources = tuple(dict.fromkeys(source_kinds))
    if not selected_sources or any(
        kind not in SEMANTIC_PLAN_TEXT_SOURCE_KINDS for kind in selected_sources
    ):
        raise ValueError("semantic text sources must name supported durable caches")
    selected_model = model or multilingual_text_model()
    if selected_model.modality is not EmbeddingModality.TEXT:
        raise ValueError("text indexing requires a text model")
    require_source_databases(state_directory, selected_sources)
    base_chunking = chunking or text_chunking_for_model(selected_model)
    database = state_directory / SEMANTIC_DATABASE_NAME
    source_heads = semantic_source_heads(state_directory, selected_sources)
    source_head_payload = [head.as_payload() for head in source_heads]
    replay_entry: dict[str, object] = {
        "channel": "text",
        "source_kinds": list(selected_sources),
        "pipeline": SEMANTIC_PIPELINE_VERSION,
        "base_chunking_signature": base_chunking.signature,
        "title_policy": SEMANTIC_TITLE_POLICY,
        "text_quality_policy": SEMANTIC_TEXT_QUALITY_POLICY,
        "source_heads": source_head_payload,
    }
    replay_scope = "text:" + ",".join(selected_sources)
    content_compatible_replay = False
    if all(head.complete for head in source_heads) and not (
        budget.preserve_existing_generations
        and has_building_embedding_generation(database, model_signature=selected_model.model_signature)
    ):
        def source_head_compatibility(
            connection: sqlite3.Connection,
            generation_id: int,
            provenance: Mapping[str, object],
        ) -> bool:
            nonlocal content_compatible_replay
            accepted = _content_compatible_text_replay(
                state_directory,
                connection,
                generation_id,
                dict(provenance),
                selected_sources=selected_sources,
                current_heads=source_heads,
                replay_scope=replay_scope,
                current_entry=replay_entry,
            )
            content_compatible_replay = accepted
            return accepted

        published = find_exact_published_generation(
            database,
            model_signature=selected_model.model_signature,
            required_source_head_ledger={replay_scope: replay_entry},
            writer_coordinated=True,
            source_head_compatibility=(
                source_head_compatibility
                if "text" in selected_sources
                else None
            ),
        )
        if published is not None:
            confirmed_heads = semantic_source_heads(state_directory, selected_sources)
            if confirmed_heads == source_heads:
                emit_progress(
                    progress,
                    ProgressEvent(
                        "semantic",
                        "exact-replay:text",
                        "Semantic texto reutilizado sin enumeración",
                        len(selected_sources),
                        len(selected_sources),
                        "fuentes",
                        True,
                        (
                            ProgressMetric("sources", len(selected_sources)),
                            ProgressMetric("reused", len(selected_sources)),
                            ProgressMetric("new_jobs", 0),
                        ),
                    ),
                )
                return SemanticIndexResult(
                    database,
                    selected_sources,
                    0,
                    0,
                    (GenerationWorkResult(published, 0, 0, 0, 0),),
                    new_jobs_staged=0,
                    execution_mode=(
                        "content_compatible_replay"
                        if content_compatible_replay
                        else "exact_replay"
                    ),
                    sources_reused=len(selected_sources),
                    sources_enumerated=0,
                )
            source_heads = confirmed_heads
            source_head_payload = [head.as_payload() for head in source_heads]
            replay_entry["source_heads"] = source_head_payload
    require_readable_source_heads(source_heads)
    published_delta = _published_text_source_delta(
        database,
        model_signature=selected_model.model_signature,
        replay_scope=replay_scope,
        replay_entry=replay_entry,
        selected_sources=selected_sources,
        current_heads=source_heads,
    )
    source_head_ledger = merge_source_head_ledger(
        published_source_head_ledger(
            database,
            model_signature=selected_model.model_signature,
            writer_coordinated=True,
        ),
        scope_key=replay_scope,
        entry=replay_entry,
    )
    cache = model_cache(state_directory, model_cache_override)
    embedding_backend = backend_factory(
        selected_model,
        cache_dir=cache,
        local_files_only=local_files_only,
        threads=threads,
    )
    text_probe(embedding_backend)
    token_guard = resolve_text_token_guard(embedding_backend, selected_model)
    active_chunking = replace(
        base_chunking,
        model_token_limit=token_guard.token_limit,
        tokenizer_signature=token_guard.tokenizer_signature,
    )

    initialize_models(database, (selected_model,))
    processing_signature = semantic_text_processing_signature(
        pipeline_version=SEMANTIC_PIPELINE_VERSION,
        chunking_signature=active_chunking.signature,
        source_kinds=selected_sources,
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=selected_model.model_signature,
        processing_signature=processing_signature,
        provenance={
            "pipeline": SEMANTIC_PIPELINE_VERSION,
            "sources": list(selected_sources),
            "base_chunking_signature": base_chunking.signature,
            "title_policy": SEMANTIC_TITLE_POLICY,
            "text_quality_policy": SEMANTIC_TEXT_QUALITY_POLICY,
            "source_heads": source_head_payload,
            "source_head_ledger": source_head_ledger,
            "chunking_signature": active_chunking.signature,
            "tokenizer_signature": token_guard.tokenizer_signature,
            "model_token_limit": token_guard.token_limit,
        },
        cursor={
            "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
            "enumeration_complete": False,
            "selected_sources": list(selected_sources),
            "completed_sources": [],
        },
        materialize_base=False,
        work_budget=budget,
    )
    if published_delta is not None and _candidate_base_generation_id(
        database,
        generation_id,
    ) != published_delta.generation_id:
        # Do not compare current items with a stale base if another writer
        # advanced the published head between delta planning and candidate
        # creation.
        published_delta = None
    resume_cursor = generation_summary(
        database,
        generation_id,
        writer_coordinated=True,
    ).cursor
    raw_completed_sources = resume_cursor.get("completed_sources", [])
    completed_sources = list(
        dict.fromkeys(
            source
            for source in raw_completed_sources
            if isinstance(source, str) and source in selected_sources
        )
    ) if isinstance(raw_completed_sources, list) else []
    raw_reused_sources = resume_cursor.get("reused_sources", [])
    reused_sources = list(
        dict.fromkeys(
            source
            for source in raw_reused_sources
            if isinstance(source, str) and source in completed_sources
        )
    ) if isinstance(raw_reused_sources, list) else []
    items_staged = chunks_staged = queued = 0
    enumeration_complete = resume_cursor.get("enumeration_complete") is True
    if published_delta is not None and any(
        published_delta.provenance.get(name) != value
        for name, value in (
            ("chunking_signature", active_chunking.signature),
            ("tokenizer_signature", token_guard.tokenizer_signature),
            ("model_token_limit", token_guard.token_limit),
        )
    ):
        # A source item is reusable only when the published base used the
        # exact same physical chunking/tokenizer contract.
        published_delta = None
    for source_kind in selected_sources:
        if source_kind in completed_sources:
            continue
        if (
            published_delta is not None
            and source_kind in published_delta.reusable_sources
        ):
            completed_sources.append(source_kind)
            if source_kind not in reused_sources:
                reused_sources.append(source_kind)
            update_embedding_generation_cursor(
                database,
                generation_id,
                cursor={
                    "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
                    "enumeration_complete": False,
                    "selected_sources": list(selected_sources),
                    "completed_sources": completed_sources,
                    "reused_sources": reused_sources,
                    "completed_source": source_kind,
                    "items": 0,
                },
            )
            continue
        refresh_token = f"generation:{generation_id}:source:{source_kind}"
        source_items, source_chunks, source_jobs, source_complete = _stage_source(
            database,
            state_directory,
            source_kind,
            generation_id=generation_id,
            refresh_token=refresh_token,
            chunking=active_chunking,
            token_counter=token_guard.counter,
            source_record_iterator=source_record_iterator,
            base_generation_id=(
                None if published_delta is None else published_delta.generation_id
            ),
            work_budget=budget,
            progress=progress,
        )
        items_staged += source_items
        chunks_staged += source_chunks
        queued += source_jobs
        if not source_complete:
            enumeration_complete = False
            update_embedding_generation_cursor(
                database,
                generation_id,
                cursor={
                    "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
                    "enumeration_complete": False,
                    "selected_sources": list(selected_sources),
                    "completed_sources": completed_sources,
                    "reused_sources": reused_sources,
                    "current_source": source_kind,
                    "truncation_reason": budget.truncation_reason,
                },
            )
            break
        completed_sources.append(source_kind)
        update_embedding_generation_cursor(
            database,
            generation_id,
            cursor={
                "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
                "enumeration_complete": False,
                "selected_sources": list(selected_sources),
                "completed_sources": completed_sources,
                "reused_sources": reused_sources,
                "completed_source": source_kind,
                "items": source_items,
            },
        )

    if not enumeration_complete and set(completed_sources) == set(selected_sources):
        enumeration_complete = True

    if enumeration_complete:
        confirmed_heads = semantic_source_heads(state_directory, selected_sources)
        if confirmed_heads != source_heads:
            if budget.preserve_existing_generations:
                raise SemanticStateError("source changed during recovery; generation was kept unpublished")
            invalidate_embedding_generations_for_source_change(
                database,
                (generation_id,),
                expected_source_heads=source_head_payload,
                observed_source_heads=[head.as_payload() for head in confirmed_heads],
            )
            raise RuntimeError("semantic source heads changed during text enumeration")
        update_embedding_generation_cursor(
            database,
            generation_id,
            cursor={
                "protocol": SEMANTIC_TEXT_ENUMERATION_PROTOCOL,
                "enumeration_complete": True,
                "selected_sources": list(selected_sources),
                "completed_sources": completed_sources,
                "reused_sources": reused_sources,
            },
        )

    try:
        reused_summary = prepare_embedding_generation(
            database,
            generation_id,
            enumeration_complete=enumeration_complete,
            work_budget=budget,
        )
    except SemanticIndexDeadlineExceeded:
        result = GenerationWorkResult(
            generation_summary(database, generation_id, writer_coordinated=True),
            queued,
            0,
            0,
            0,
        )
    else:
        if reused_summary is None:
            result = generation_runner(
                database,
                generation_id,
                embedding_backend,
                queued=queued,
                work_budget=budget,
                publish_if_complete=enumeration_complete,
            )
        else:
            result = GenerationWorkResult(reused_summary, 0, 0, 0, 0)
    return SemanticIndexResult(
        database,
        selected_sources,
        items_staged,
        chunks_staged,
        (result,),
        new_jobs_staged=budget.new_jobs_admitted - new_jobs_before,
        execution_mode="enumerated",
        sources_reused=len(reused_sources),
        sources_enumerated=len(set(completed_sources).difference(reused_sources)),
        truncated=budget.truncated,
        truncation_reason=budget.truncation_reason,
    )


# endregion [02]
