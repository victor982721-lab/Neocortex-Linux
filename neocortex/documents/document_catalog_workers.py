"""Independent catalog classification jobs and bounded retained results."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
import sys
from typing import TYPE_CHECKING
import zlib

if TYPE_CHECKING:
    from .document_catalog_models import SourceDocument
    from .document_taxonomy import DocumentClassification, TechnicalTaxonomy


@dataclass(frozen=True, slots=True)
class CatalogClassificationTask:
    source_path: Path
    document: SourceDocument
    taxonomy: TechnicalTaxonomy
    max_text_chars: int
    classifier_signature: str


@dataclass(frozen=True, slots=True)
class CatalogClassificationResult:
    document: SourceDocument
    classification: DocumentClassification | None = None
    error: UnicodeError | ValueError | zlib.error | None = None
    cache_hit: bool = False
    source_stale: bool = False
    outside_scope: bool = False


def retained_catalog_bytes(value: object) -> int:
    """Conservatively include nested value storage, without copying payloads."""

    size = sys.getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        size += sum(retained_catalog_bytes(getattr(value, field.name)) for field in fields(value))
    elif isinstance(value, (tuple, list)):
        size += sum(retained_catalog_bytes(item) for item in value)
    elif isinstance(value, dict):
        size += sum(retained_catalog_bytes(key) + retained_catalog_bytes(item) for key, item in value.items())
    elif isinstance(value, BaseException):
        size += retained_catalog_bytes(value.args)
    return size


def classify_catalog_task(task: CatalogClassificationTask) -> CatalogClassificationResult:
    """Read only the caller's stable SQLite view; no writer crosses a process."""

    from neocortex.persistence.sqlite_immutable import open_immutable_sqlite_connection
    from neocortex.runtime.control.elastic_workers import current_worker_cancellation
    from .document_catalog_replay import catalog_sql_cancellation
    from .document_catalog_text import _load_leading_text
    from .document_taxonomy import DocumentSignals, classify_document, document_classifier_signature

    cancellation = current_worker_cancellation()
    if cancellation is not None:
        cancellation.checkpoint()
    if document_classifier_signature(task.taxonomy) != task.classifier_signature:
        raise RuntimeError("catalog worker classifier identity differs from its job")
    try:
        connection = open_immutable_sqlite_connection(task.source_path, timeout_seconds=60.0)
        try:
            with catalog_sql_cancellation(connection, cancellation):
                text = _load_leading_text(
                    connection, task.document, max_text_chars=task.max_text_chars,
                    cancellation=cancellation,
                )
        finally:
            connection.close()
        if cancellation is not None:
            cancellation.checkpoint()
        document = task.document
        classification = classify_document(
            DocumentSignals(
                source_kind=document.source_kind, path=document.path,
                source_status=document.source_status, title=document.title,
                author=document.author, metadata=document.metadata,
                leading_text=text, page_count=document.page_count,
            ), task.taxonomy,
        )
        if cancellation is not None:
            cancellation.checkpoint()
        if classification.classifier_signature != task.classifier_signature:
            raise RuntimeError("catalog worker classifier result identity differs from its job")
        return CatalogClassificationResult(document, classification=classification)
    except (UnicodeError, ValueError, zlib.error) as exc:
        return CatalogClassificationResult(task.document, error=exc.with_traceback(None))
