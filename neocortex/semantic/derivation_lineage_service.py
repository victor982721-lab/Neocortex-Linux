"""Bounded read-only explanations across owner-local derivation state."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from .derivation_projection import (
    DerivationProjection,
    DerivationProjectionEvent,
    projection_event_from_semantic_outbox,
    projection_event_from_text_outbox,
    rebuild_derivation_projection,
)
from .semantic_lineage_repository import (
    SemanticTextChunkLineage,
    explain_text_chunk_lineage,
    find_text_chunks_for_source_revision,
    read_semantic_derivation_outbox,
)
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths, KnowledgeStateRootError
from neocortex.capabilities.formats.text.text_derivation_repository import (
    TextDerivationIntegrityError,
    read_text_derivation_dependents_page,
    read_text_derivation_outbox,
    read_text_document_lineage,
    read_text_revision_lineage,
    read_text_work_receipts,
    resolve_text_lineage_identifier,
    resolve_text_lineage_revision_id,
)


DERIVATION_LINEAGE_VIEW_SCHEMA = "neocortex.derivation-lineage/v1"
MAX_LINEAGE_IDENTIFIER_CHARS = 4_096
MAX_LINEAGE_RECEIPTS = 1_000
MAX_LINEAGE_PROJECTION_EVENTS = 100_000
MAX_SEMANTIC_DEPENDENT_CHUNKS = 100


def _validate_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("lineage identifier cannot be blank")
    normalized = identifier.strip()
    if len(normalized) > MAX_LINEAGE_IDENTIFIER_CHARS:
        raise ValueError(
            f"lineage identifier cannot exceed {MAX_LINEAGE_IDENTIFIER_CHARS} characters"
        )
    return normalized


def _validate_owner_state_path(path: Path) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise KnowledgeStateRootError(
            path.parent,
            "contains an inaccessible owner state path",
            f"{path}: {exc.strerror or exc}",
        ) from exc
    try:
        metadata = os.stat(path)
    except OSError as exc:
        raise KnowledgeStateRootError(
            path.parent,
            "contains an inaccessible owner state path",
            f"{path}: {exc.strerror or exc}",
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise KnowledgeStateRootError(
            path.parent,
            "contains a non-file owner state path",
            str(path),
        )


def _empty_projection() -> DerivationProjection:
    return rebuild_derivation_projection(())


def _iter_text_projection_events(
    path: Path,
    *,
    max_events: int,
) -> Iterator[DerivationProjectionEvent]:
    cursor = 0
    yielded = 0
    while True:
        page = read_text_derivation_outbox(
            path,
            after_sequence=cursor,
            limit=min(1_000, max_events + 1 - yielded),
        )
        if not page:
            return
        cursor = page[-1].sequence
        for event in page:
            yielded += 1
            if yielded > max_events:
                raise RuntimeError("Text derivation projection exceeds its event bound")
            yield projection_event_from_text_outbox(event)


def _iter_owner_projection_events(
    state_directory: Path,
    *,
    max_events: int,
) -> Iterator[DerivationProjectionEvent]:
    yielded = 0
    text_path = state_directory / "text.sqlite3"
    if text_path.is_file():
        cursor = 0
        while True:
            text_page = read_text_derivation_outbox(
                text_path,
                after_sequence=cursor,
                limit=min(1_000, max_events + 1 - yielded),
            )
            if not text_page:
                break
            cursor = text_page[-1].sequence
            for text_event in text_page:
                yielded += 1
                if yielded > max_events:
                    raise RuntimeError("derivation projection exceeds its event bound")
                yield projection_event_from_text_outbox(text_event)

    semantic_path = state_directory / "semantic.sqlite3"
    if semantic_path.is_file():
        semantic_cursor = 0
        while True:
            semantic_page = read_semantic_derivation_outbox(
                semantic_path,
                after_event_id=semantic_cursor,
                limit=min(1_000, max_events + 1 - yielded),
            )
            if not semantic_page:
                break
            semantic_cursor = semantic_page[-1].event_id
            for semantic_event in semantic_page:
                yielded += 1
                if yielded > max_events:
                    raise RuntimeError("derivation projection exceeds its event bound")
                yield projection_event_from_semantic_outbox(semantic_event)


def rebuild_text_derivation_projection(
    path: Path,
    *,
    max_events: int = MAX_LINEAGE_PROJECTION_EVENTS,
) -> DerivationProjection:
    """Replay Text's outbox without creating a global authority or new state."""

    if not 1 <= max_events <= MAX_LINEAGE_PROJECTION_EVENTS:
        raise ValueError(f"max_events must be between 1 and {MAX_LINEAGE_PROJECTION_EVENTS}")
    _validate_owner_state_path(path)
    if not path.is_file():
        return _empty_projection()
    return rebuild_derivation_projection(_iter_text_projection_events(path, max_events=max_events))


def rebuild_derivation_projection_from_owners(
    state_directory: Path,
    *,
    max_events: int = MAX_LINEAGE_PROJECTION_EVENTS,
) -> DerivationProjection:
    """Rebuild a disposable cross-owner view from committed owner outboxes."""

    if not 1 <= max_events <= MAX_LINEAGE_PROJECTION_EVENTS:
        raise ValueError(f"max_events must be between 1 and {MAX_LINEAGE_PROJECTION_EVENTS}")
    state_paths = KnowledgeStatePaths.from_directory(state_directory)
    state_paths.validate_roots()
    assert state_paths.text is not None
    _validate_owner_state_path(state_paths.text)
    _validate_owner_state_path(state_paths.semantic)
    return rebuild_derivation_projection(
        _iter_owner_projection_events(state_directory, max_events=max_events)
    )


def _projection_for_receipts(payloads: tuple[dict[str, object], ...]) -> dict[str, object]:
    events = tuple(
        DerivationProjectionEvent(
            "text",
            index,
            f"text-inspection:{payload['receipt_id']}",
            payload,
        )
        for index, payload in enumerate(payloads, start=1)
    )
    return rebuild_derivation_projection(events).to_dict()


def _receipt_payload(record_json: str) -> dict[str, object]:
    payload = json.loads(record_json)
    if not isinstance(payload, dict):
        raise RuntimeError("Text WorkReceipt payload is not a JSON object")
    return payload


def _read_receipt_only_lineage(path: Path, identifier: str) -> dict[str, object] | None:
    records = read_text_work_receipts(path, (identifier,))
    if not records:
        return None
    payload = _receipt_payload(records[0].payload_json)
    inputs = payload.get("inputs")
    first_input = inputs[0] if isinstance(inputs, list) and inputs else None
    revision = first_input.get("revision") if isinstance(first_input, dict) else None
    outcome = payload.get("outcome")
    return {
        "owner": "text",
        "file_key": None,
        "lineage": {
            "file_key": None,
            "path": None,
            "document_status": outcome,
            "attribution": "receipt_only",
            "revision": revision if isinstance(revision, dict) else None,
            "receipts": [identifier],
            "materializations": [],
        },
        "receipts": [payload],
        "receipt_count": 1,
        "receipt_window_truncated": False,
        "dependencies": [],
        "dependency_count": 0,
        "dependency_window_truncated": False,
        "current_materialization_heads": 0,
        "causal_graph": _projection_for_receipts((payload,)),
    }


def _read_text_lineage(path: Path, identifier: str) -> dict[str, object] | None:
    if not path.is_file():
        return None
    file_key = resolve_text_lineage_identifier(path, identifier)
    if file_key is not None:
        lineage = read_text_document_lineage(
            path,
            file_key,
            limit=MAX_LINEAGE_RECEIPTS,
        )
    else:
        revision_id = resolve_text_lineage_revision_id(path, identifier)
        lineage = (
            None
            if revision_id is None
            else read_text_revision_lineage(
                path,
                revision_id,
                limit=MAX_LINEAGE_RECEIPTS,
            )
        )
        if lineage is None:
            return _read_receipt_only_lineage(path, identifier)
    if lineage is None:  # pragma: no cover - protected by same read-only owner state
        return None
    records = read_text_work_receipts(path, lineage.receipts)
    payloads: list[dict[str, object]] = []
    for record in records:
        payloads.append(_receipt_payload(record.payload_json))
    revision_id = None if lineage.revision is None else lineage.revision.revision_id
    dependency_page = (
        None
        if revision_id is None
        else read_text_derivation_dependents_page(
            path,
            revision_id,
            limit=MAX_LINEAGE_RECEIPTS,
        )
    )
    dependencies = () if dependency_page is None else dependency_page.items
    materializations = [item.to_dict() for item in lineage.materializations]
    current_heads = sum(bool(item["current_head"]) for item in materializations)
    return {
        "owner": "text",
        "file_key": lineage.file_key,
        "lineage": lineage.to_dict(),
        "receipts": payloads,
        "receipt_count": lineage.receipt_count,
        "receipt_window_truncated": lineage.receipt_count > len(lineage.receipts),
        "dependencies": [item.to_dict() for item in dependencies],
        "dependency_count": 0 if dependency_page is None else dependency_page.total_count,
        "dependency_window_truncated": (
            False if dependency_page is None else dependency_page.truncated
        ),
        "current_materialization_heads": current_heads,
        "causal_graph": _projection_for_receipts(tuple(payloads)),
    }


def _semantic_change_analysis(
    lineage: SemanticTextChunkLineage,
) -> tuple[dict[str, object], ...]:
    chunk_id = lineage.chunk_id
    origins = lineage.origins
    embeddings = lineage.embeddings
    origin_revisions = sorted(
        {
            str(origin.source_revision["revision_id"])
            for origin in origins
            if isinstance(origin.source_revision.get("revision_id"), str)
        }
    )
    embedding_nodes = [
        f"semantic:embedding-generation:{embedding.generation_id}:member:{embedding.member_id}"
        for embedding in embeddings
    ]
    return (
        {
            "change": "chunking_signature",
            "stale": [f"semantic:chunk:{chunk_id}", *embedding_nodes],
            "reusable": origin_revisions,
            "reason": "chunks and their embeddings depend on the recorded chunking signature",
        },
        {
            "change": "embedding_model_signature",
            "stale": embedding_nodes,
            "reusable": [*origin_revisions, f"semantic:chunk:{chunk_id}"],
            "reason": "the chunk is independent from a replacement embedding space",
        },
    )


def _read_semantic_lineage(path: Path, identifier: str) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        lineage = explain_text_chunk_lineage(path, chunk_id=identifier)
    except KeyError:
        return None
    return {
        "owner": "semantic",
        "lineage": asdict(lineage),
        "change_analysis": list(_semantic_change_analysis(lineage)),
    }


def _read_semantic_dependents(
    path: Path,
    revision_id: str | None,
) -> dict[str, object] | None:
    if revision_id is None or not path.is_file():
        return None
    page = find_text_chunks_for_source_revision(
        path,
        revision_id=revision_id,
        limit=MAX_SEMANTIC_DEPENDENT_CHUNKS,
    )
    return {
        "revision_id": page.revision_id,
        "chunk_ids": list(page.chunk_ids),
        "chunk_count_in_window": len(page.chunk_ids),
        "truncated": page.truncated,
    }


def inspect_derivation_lineage(
    state_directory: Path,
    identifier: str,
) -> dict[str, object]:
    """Explain one Text resource/revision/output or one Semantic chunk.

    The caller selects a trusted state root.  This function never initializes,
    migrates or repairs either owner database.
    """

    normalized = _validate_identifier(identifier)
    state_paths = KnowledgeStatePaths.from_directory(state_directory)
    state_paths.validate_roots()
    assert state_paths.text is not None
    text_path = state_paths.text
    semantic_path = state_paths.semantic
    _validate_owner_state_path(text_path)
    _validate_owner_state_path(semantic_path)
    warnings: list[str] = []
    text_payload: dict[str, object] | None = None
    semantic_payload: dict[str, object] | None = None
    semantic_dependents: dict[str, object] | None = None
    schema_incompatible = False
    try:
        text_payload = _read_text_lineage(text_path, normalized)
    except (TextDerivationIntegrityError, ValueError) as exc:
        return {
            "schema": DERIVATION_LINEAGE_VIEW_SCHEMA,
            "kind": "derivation_lineage",
            "read_only": True,
            "identifier": normalized,
            "status": "corrupt",
            "complete": False,
            "exit_code": 7,
            "text": None,
            "semantic": None,
            "semantic_dependents": None,
            "warnings": [f"text:{type(exc).__name__}:{exc}"],
        }
    except RuntimeError as exc:
        schema_incompatible = True
        warnings.append(f"text:{type(exc).__name__}:{exc}")
    except sqlite3.DatabaseError as exc:
        return {
            "schema": DERIVATION_LINEAGE_VIEW_SCHEMA,
            "kind": "derivation_lineage",
            "read_only": True,
            "identifier": normalized,
            "status": "corrupt",
            "complete": False,
            "exit_code": 7,
            "text": None,
            "semantic": None,
            "semantic_dependents": None,
            "warnings": [f"text:{type(exc).__name__}:{exc}"],
        }
    try:
        semantic_payload = _read_semantic_lineage(semantic_path, normalized)
    except ValueError as exc:
        return {
            "schema": DERIVATION_LINEAGE_VIEW_SCHEMA,
            "kind": "derivation_lineage",
            "read_only": True,
            "identifier": normalized,
            "status": "corrupt",
            "complete": False,
            "exit_code": 7,
            "text": text_payload,
            "semantic": None,
            "semantic_dependents": None,
            "warnings": [f"semantic:{type(exc).__name__}:{exc}"],
        }
    except RuntimeError as exc:
        schema_incompatible = True
        warnings.append(f"semantic:{type(exc).__name__}:{exc}")
    except sqlite3.DatabaseError as exc:
        return {
            "schema": DERIVATION_LINEAGE_VIEW_SCHEMA,
            "kind": "derivation_lineage",
            "read_only": True,
            "identifier": normalized,
            "status": "corrupt",
            "complete": False,
            "exit_code": 7,
            "text": text_payload,
            "semantic": None,
            "semantic_dependents": None,
            "warnings": [f"semantic:{type(exc).__name__}:{exc}"],
        }
    text_lineage = None if text_payload is None else text_payload.get("lineage")
    revision = text_lineage.get("revision") if isinstance(text_lineage, dict) else None
    revision_id = revision.get("revision_id") if isinstance(revision, dict) else None
    try:
        semantic_dependents = _read_semantic_dependents(
            semantic_path,
            revision_id if isinstance(revision_id, str) else None,
        )
    except ValueError as exc:
        return {
            "schema": DERIVATION_LINEAGE_VIEW_SCHEMA,
            "kind": "derivation_lineage",
            "read_only": True,
            "identifier": normalized,
            "status": "corrupt",
            "complete": False,
            "exit_code": 7,
            "text": text_payload,
            "semantic": semantic_payload,
            "semantic_dependents": None,
            "warnings": [f"semantic:{type(exc).__name__}:{exc}"],
        }
    except RuntimeError as exc:
        schema_incompatible = True
        warnings.append(f"semantic:{type(exc).__name__}:{exc}")
    except sqlite3.DatabaseError as exc:
        return {
            "schema": DERIVATION_LINEAGE_VIEW_SCHEMA,
            "kind": "derivation_lineage",
            "read_only": True,
            "identifier": normalized,
            "status": "corrupt",
            "complete": False,
            "exit_code": 7,
            "text": text_payload,
            "semantic": semantic_payload,
            "semantic_dependents": None,
            "warnings": [f"semantic:{type(exc).__name__}:{exc}"],
        }
    if text_payload is not None:
        text_lineage_payload = text_payload.get("lineage")
        truncation_flags = {
            "receipt_window": text_payload.get("receipt_window_truncated"),
            "dependency_window": text_payload.get("dependency_window_truncated"),
            "materialization_window": (
                text_lineage_payload.get("materialization_window_truncated")
                if isinstance(text_lineage_payload, dict)
                else False
            ),
        }
        warnings.extend(
            f"text:{name}_truncated"
            for name, truncated in truncation_flags.items()
            if truncated is True
        )
    if semantic_payload is not None:
        semantic_lineage_payload = semantic_payload.get("lineage")
        if isinstance(semantic_lineage_payload, dict):
            warnings.extend(
                f"semantic:{name}_truncated"
                for name in ("origins", "embeddings")
                if semantic_lineage_payload.get(f"{name}_truncated") is True
            )
    if semantic_dependents is not None and semantic_dependents.get("truncated") is True:
        warnings.append("semantic:dependency_window_truncated")
    if text_payload is None and semantic_payload is None:
        return {
            "schema": DERIVATION_LINEAGE_VIEW_SCHEMA,
            "kind": "derivation_lineage",
            "read_only": True,
            "identifier": normalized,
            "status": "schema_incompatible" if schema_incompatible else "not_found",
            "complete": False,
            "exit_code": 6 if schema_incompatible else 3,
            "text": None,
            "semantic": None,
            "semantic_dependents": None,
            "warnings": warnings,
        }
    text_attribution = text_lineage.get("attribution") if isinstance(text_lineage, dict) else None
    text_document_status = (
        text_lineage.get("document_status") if isinstance(text_lineage, dict) else None
    )
    semantic_lineage = None if semantic_payload is None else semantic_payload.get("lineage")
    current_heads = (
        None if text_payload is None else text_payload.get("current_materialization_heads")
    )
    complete = (
        not warnings
        and text_attribution
        not in {
            "legacy_unattributed",
            "receipt_only",
            "revision_missing",
            "receipt_missing",
        }
        and (
            text_payload is None
            or (
                text_document_status == "complete"
                and isinstance(current_heads, int)
                and not isinstance(current_heads, bool)
                and current_heads > 0
            )
        )
        and (
            semantic_payload is None
            or (
                isinstance(semantic_lineage, dict)
                and semantic_lineage.get("lineage_status") == "recorded"
            )
        )
    )
    return {
        "schema": DERIVATION_LINEAGE_VIEW_SCHEMA,
        "kind": "derivation_lineage",
        "read_only": True,
        "identifier": normalized,
        "status": "ready" if complete else "partial",
        "complete": complete,
        "exit_code": 0 if complete else 4,
        "text": text_payload,
        "semantic": semantic_payload,
        "semantic_dependents": semantic_dependents,
        "warnings": warnings,
    }


__all__ = (
    "DERIVATION_LINEAGE_VIEW_SCHEMA",
    "inspect_derivation_lineage",
    "rebuild_derivation_projection_from_owners",
    "rebuild_text_derivation_projection",
)


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.derivation_lineage_service")
