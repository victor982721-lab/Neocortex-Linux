"""Lifecycle adapter for Fast Curation between routes/Catalog and Organization.

The adapter owns orchestration only.  It reads the current Catalog projection,
consumes route-derived text through the bounded source adapter, closes every
route snapshot before the Catalog writer is used, and delegates embedding and
policy decisions to ``neocortex.semantic.fast_curation_service``.  It never
opens Full Semantic, reads corpus files, creates a resource coordinator, or
moves a document.
"""

from __future__ import annotations

import os
import json
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TYPE_CHECKING

from neocortex.documents.curation_sources import (
    CurationSourceError,
    iter_catalog_route_source_pages,
)
from neocortex.documents.document_semantic_representation import (
    REPRESENTATION_VERSION,
    RepresentationBudgets,
    RepresentationInputError,
    build_document_semantic_representation,
)
from neocortex.persistence.sqlite_immutable import (
    SQLiteReadSession,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
)
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.runtime.control.global_resources import current_resource_coordinator

if TYPE_CHECKING:
    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.progress import ProgressCallback
    from neocortex.runtime.control.cancellation import CancellationToken
    from neocortex.runtime.models import FrameworkConfig


STAGE_SCHEMA = "neocortex.fast-curation-stage/v1"
STAGE_NAME = "fast_curation"
DEFAULT_PAGE_SIZE = 256
MAX_PAGE_SIZE = 4096
MAX_DECISION_SAMPLES = 8
ROUTE_DATABASE_NAMES = {
    "pdf": "pdf_database",
    "docx": "docx_database",
    "xlsx": "office_database",
    "pptx": "office_database",
    "odt": "office_database",
    "text": "text_database",
    "audio": "audio_database",
    "video": "video_database",
    "image": "image_database",
}


class FastCurationLifecycleError(RuntimeError):
    """Fast Curation could not establish its current bounded input."""


@dataclass(slots=True)
class _LifecycleCounters:
    catalog_candidates: int = 0
    representations: int = 0
    route_unavailable: int = 0
    representation_unavailable: int = 0
    source_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "catalog_candidates": self.catalog_candidates,
            "representations": self.representations,
            "route_unavailable": self.route_unavailable,
            "representation_unavailable": self.representation_unavailable,
            "source_errors": self.source_errors,
        }


class _CatalogEmbeddingCache:
    """Persistent cache seam for the service's opaque content key.

    The service deliberately exposes a single string cache key.  It is a
    SHA-256 key containing the content/representation/model identity, so it is
    safe to use as ``representation_sha256`` in the Catalog-owned cache.  The
    remaining key dimensions are fixed by the effective model and the actual
    encoder role.  The service's legacy seam supplies only the opaque key, so
    reads probe the two supported roles (QUERY/PASSAGE) and reject ambiguity;
    writes infer the role from the bounded metadata it already supplies.  No
    second SQLite owner is opened.
    """

    def __init__(self, connection: Any, *, model: Any, representation_version: str) -> None:
        from neocortex.documents.curation_state import CurationEmbeddingCacheKey

        self.connection = connection
        self.model = model
        self.representation_version = representation_version
        self._key_type = CurationEmbeddingCacheKey
        self._pending: list[object] = []
        self._flush_limit = 256

    def _key(self, opaque_key: str, role: str):
        return self._key_type(
            representation_sha256=opaque_key,
            representation_version=self.representation_version,
            model_signature=str(self.model.model_signature),
            role=role,
            vector_space=str(self.model.vector_space),
            dimensions=int(self.model.dimensions),
        )

    def get(self, opaque_key: str) -> tuple[float, ...] | None:
        from neocortex.documents.curation_state import read_embedding_cache

        if not isinstance(opaque_key, str) or len(opaque_key) != 64:
            return None
        matches = []
        for role in ("query", "passage"):
            record = read_embedding_cache(self.connection, self._key(opaque_key, role))
            if record is not None:
                matches.append(record)
        if len(matches) != 1:
            # Missing is a normal cache miss; two roles for one opaque key is
            # an invalid/ambiguous owner state and must not select arbitrarily.
            return None
        return tuple(matches[0].vector)

    def put(
        self,
        opaque_key: str,
        vector: Sequence[float],
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        from neocortex.documents.curation_state import CurationEmbeddingCacheRecord

        metadata_map = _bounded_json_mapping(metadata)
        if metadata_map.get("prototype_id") not in (None, ""):
            role = "query"
        elif metadata_map.get("representation_version") not in (None, ""):
            role = "passage"
        else:
            raise FastCurationLifecycleError(
                "embedding cache write lacks an explicit query/passage role"
            )
        record = CurationEmbeddingCacheRecord.from_values(
            self._key(opaque_key, role),
            tuple(float(value) for value in vector),
            metadata=metadata_map,
        )
        self._pending.append(record)
        if len(self._pending) >= self._flush_limit:
            self.flush()

    def flush(self) -> int:
        from neocortex.documents.curation_state import upsert_embedding_cache_batch

        if not self._pending:
            return 0
        selected = tuple(self._pending)
        self._pending.clear()
        return upsert_embedding_cache_batch(self.connection, selected)


class _CatalogDecisionSink:
    """Convert service evidence into Catalog-owned current decision rows."""

    def __init__(self, connection: Any, *, model: Any, root: Path) -> None:
        self.connection = connection
        self.model = model
        self.root = root

    def persist_fast_curation_decisions(
        self,
        decisions: Iterable[object],
        *,
        root: Path,
        state_directory: Path,
        framework_state: object | None = None,
        run_id: int | str | None = None,
    ) -> int:
        del state_directory, framework_state, run_id
        from neocortex.documents.curation_state import (
            CurationDecisionRecord,
            CurationTopCandidate,
            upsert_curation_decision_batch,
        )

        records: list[CurationDecisionRecord] = []
        for evidence in decisions:
            source_kind = _required_text(evidence, "source_kind", "unknown")
            file_key = _required_text(evidence, "file_key", _field(evidence, "document_id", ""))
            input_signature = _required_text(
                evidence,
                "input_signature",
                _field(evidence, "representation_fingerprint", "unavailable"),
            )
            decision = str(_field(evidence, "calibrated_decision", "abstain")).upper()
            if decision not in {"CLASSIFIED", "ABSTAIN"}:
                decision = "ABSTAIN"
            candidates = tuple(_field(evidence, "top_candidates", ()) or ())[:5]
            top = tuple(
                CurationTopCandidate(
                    _required_text(candidate, "concept_id", _field(candidate, "label", "unknown")),
                    float(_field(candidate, "score", 0.0)),
                )
                for candidate in candidates
            )
            top1 = top[0] if top else None
            top2 = top[1] if len(top) > 1 else None
            source_path = _field(evidence, "source_path", None)
            deterministic_raw = _field(evidence, "deterministic_evidence", {})
            binding = (
                deterministic_raw.get("source_binding")
                if isinstance(deterministic_raw, Mapping)
                else None
            )
            if not isinstance(binding, Mapping):
                raise FastCurationLifecycleError(
                    "Fast Curation decision has no canonical Catalog resource binding"
                )
            binding = dict(binding)
            if binding.get("source_kind") != source_kind or binding.get("file_key") != file_key:
                raise FastCurationLifecycleError("Catalog resource binding does not match decision identity")
            self._validate_current_binding(
                source_kind=source_kind,
                file_key=file_key,
                input_signature=input_signature,
                binding=binding,
                root=Path(root),
            )
            evidence_json = {
                "decision_reason": str(_field(evidence, "decision_reason", "unknown"))[:256],
                "confidence_kind": str(_field(evidence, "confidence_kind", "not_calibrated"))[:128],
                "deterministic": _bounded_json_mapping(_field(evidence, "deterministic_evidence", {})),
                "semantic": _bounded_json_mapping(_field(evidence, "semantic_evidence", {})),
                "metadata": _bounded_json_mapping(_field(evidence, "metadata_evidence", {})),
                "structural": _bounded_json_mapping(_field(evidence, "structural_evidence", {})),
                "selected_by_family": _bounded_json_mapping(
                    _field(evidence, "selected_by_family", {})
                ),
                "top_k": [
                    {
                        "concept_id": item.label,
                        "display_label": _required_text(
                            candidate,
                            "label",
                            item.label,
                        ),
                        "family": str(_field(candidate, "family", ""))[:128],
                        "prototype_id": str(_field(candidate, "prototype_id", ""))[:256],
                        "score": item.score,
                    }
                    for item, candidate in zip(top, candidates, strict=True)
                ],
            }
            selected_by_family = _field(evidence, "selected_by_family", {})
            if isinstance(selected_by_family, Mapping):
                selected_kind = selected_by_family.get("document_kind")
                if isinstance(selected_kind, str) and selected_kind:
                    evidence_json["top1_concept_id_document_kind"] = selected_kind[:256]
            prototype_set_fingerprint = _field(evidence, "prototype_set_fingerprint", None)
            if prototype_set_fingerprint not in (None, ""):
                evidence_json["prototype_set_fingerprint"] = str(prototype_set_fingerprint)[:128]
            ontology_id = _field(evidence, "ontology_id", None)
            if ontology_id not in (None, ""):
                evidence_json["ontology_id"] = str(ontology_id)[:256]
            records.append(
                CurationDecisionRecord(
                    source_kind=source_kind,
                    file_key=file_key,
                    source_binding=binding,
                    input_signature=input_signature,
                    semantic_representation_fingerprint=_required_text(
                        evidence,
                        "representation_fingerprint",
                        "0" * 64,
                    ),
                    representation_version=_required_text(evidence, "representation_version", "unknown"),
                    model_signature=_required_text(evidence, "model_signature", str(self.model.model_signature)),
                    role="document",
                    vector_space=_required_text(evidence, "vector_space", str(self.model.vector_space)),
                    dimensions=int(self.model.dimensions),
                    ontology_version=_required_text(evidence, "ontology_version", "unknown"),
                    prototype_version=_required_text(evidence, "prototype_version", "unknown"),
                    policy_version=_required_text(evidence, "policy_version", "unknown"),
                    calibration_version=_required_text(evidence, "calibration_version", "unavailable"),
                    decision=decision,
                    top1_label=None if top1 is None else top1.label,
                    top1_score=None if top1 is None else top1.score,
                    top2_label=None if top2 is None else top2.label,
                    top2_score=None if top2 is None else top2.score,
                    margin=_optional_float(_field(evidence, "margin", None)),
                    top_k=top,
                    evidence=evidence_json,
                    context_provenance={
                        "original_path": None if source_path is None else str(source_path)[:4096],
                        "source_kind": source_kind,
                        "file_key": file_key,
                    },
                )
            )
        if not records:
            return 0
        return upsert_curation_decision_batch(self.connection, records)

    def _validate_current_binding(
        self,
        *,
        source_kind: str,
        file_key: str,
        input_signature: str,
        binding: Mapping[str, object],
        root: Path,
    ) -> None:
        from neocortex.documents.document_resource_binding import parse_resource_binding

        signature_column = (
            "COALESCE(NULLIF(source_input_signature,''),text_fingerprint,processing_signature)"
            if _has_catalog_column(self.connection, "source_input_signature")
            else "COALESCE(text_fingerprint,processing_signature)"
        )
        row = self.connection.execute(
            f"""SELECT path,active,{signature_column} AS input_signature,
            resource_binding_json FROM documents
            WHERE source_kind=? AND file_key=?""",
            (source_kind, file_key),
        ).fetchone()
        if row is None or int(row[1]) != 1:
            raise FastCurationLifecycleError("Catalog current document disappeared")
        current_path = str(row[0])
        if not _path_in_root(current_path, root):
            raise FastCurationLifecycleError("Catalog current document escaped the run root")
        raw_binding = row[3]
        if not isinstance(raw_binding, str):
            raise FastCurationLifecycleError("Catalog current document has no resource binding")
        try:
            current_binding = parse_resource_binding(raw_binding)
        except ValueError as exc:
            raise FastCurationLifecycleError("Catalog current resource binding is invalid") from exc
        if current_binding != dict(binding) or current_binding.get("physical_anchor_path") != current_path:
            raise FastCurationLifecycleError("Catalog resource binding changed before persistence")
        observed_signature = row[2]
        if observed_signature is not None and str(observed_signature) != input_signature:
            raise FastCurationLifecycleError("Catalog source input signature changed before persistence")


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _required_text(value: object, name: str, default: object = None) -> str:
    selected = _field(value, name, default)
    if selected is None and default is not None:
        selected = default
    if not isinstance(selected, str) or not selected.strip():
        raise FastCurationLifecycleError(f"{name} is required for Catalog persistence")
    return selected.strip()[:4096]


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise FastCurationLifecycleError("decision numeric evidence is invalid") from exc


def _bounded_json_mapping(value: object, *, maximum_items: int = 32) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in sorted(value, key=lambda item: str(item))[:maximum_items]:
        selected = value[key]
        if isinstance(selected, (str, int, float, bool)) or selected is None:
            result[str(key)[:256]] = selected
        elif isinstance(selected, Mapping):
            result[str(key)[:256]] = _bounded_json_mapping(selected, maximum_items=32)
        elif isinstance(selected, (list, tuple)):
            result[str(key)[:256]] = [
                _bounded_json_value(item) for item in selected[:32]
            ]
        else:
            result[str(key)[:256]] = str(selected)[:512]
    return result


def _bounded_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _bounded_json_mapping(value, maximum_items=32)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:512]


def _checkpoint(cancellation: object | None) -> None:
    if cancellation is None:
        return
    callback = getattr(cancellation, "checkpoint", None)
    if callable(callback):
        callback()


def _path_in_root(path: str, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(root))) == os.path.abspath(root)
    except (OSError, ValueError):
        return False


def _route_path(config: FrameworkConfig, source_kind: str) -> Path:
    try:
        field_name = ROUTE_DATABASE_NAMES[source_kind]
    except KeyError as exc:
        raise FastCurationLifecycleError(f"unsupported Catalog source kind: {source_kind}") from exc
    value = getattr(config, field_name, None)
    if value is None:
        raise FastCurationLifecycleError(f"FrameworkConfig has no route database for {source_kind}")
    return Path(value)


def _has_catalog_column(connection: Any, column: str) -> bool:
    try:
        rows = connection.execute("PRAGMA table_info(documents)").fetchall()
    except Exception:
        return False
    return any(len(row) > 1 and str(row[1]) == column for row in rows)


def _catalog_page(
    connection: Any,
    *,
    root: Path,
    after: tuple[str, str] | None,
    limit: int,
) -> tuple[tuple[SimpleNamespace, ...], tuple[str, str] | None]:
    where = ["active=1", "last_seen_catalog_run_id IS NOT NULL"]
    parameters: list[object] = []
    if after is not None:
        where.append("(source_kind>? OR (source_kind=? AND file_key>?))")
        parameters.extend((after[0], after[0], after[1]))
    input_signature_column = (
        "source_input_signature"
        if _has_catalog_column(connection, "source_input_signature")
        else "NULL AS source_input_signature"
    )
    rows = connection.execute(
        f"""SELECT source_kind,file_key,path,volume_id,file_id,size,mtime_ns,birthtime_ns,
        source_status,processing_signature,text_fingerprint,primary_kind,primary_subtype,
        primary_authority,primary_organization,primary_client,primary_project,
        primary_workstream,topics_json,equipment_json,activities_json,
        last_seen_catalog_run_id,resource_binding_json,{input_signature_column}
        FROM documents WHERE {' AND '.join(where)}
        ORDER BY source_kind,file_key LIMIT ?""",
        (*parameters, limit),
    ).fetchall()
    selected: list[SimpleNamespace] = []
    for row in rows:
        path = str(row[2])
        if not _path_in_root(path, root):
            continue
        source_input_signature = (
            row[23]
            if len(row) > 23 and isinstance(row[23], str) and row[23].strip()
            else row[10]
        )
        selected.append(
            SimpleNamespace(
                source_kind=str(row[0]), file_key=str(row[1]), path=path,
                volume_id=str(row[3]), file_id=str(row[4]), size=int(row[5]),
                mtime_ns=int(row[6]), birthtime_ns=int(row[7]),
                source_status=str(row[8]), processing_signature=str(row[9]),
                text_fingerprint=(
                    None if source_input_signature is None else str(source_input_signature)
                ),
                source_input_signature=(
                    None
                    if source_input_signature is None
                    else str(source_input_signature)
                ),
                title="", author="",
                metadata={
                    key: value for key, value in {
                        "catalog_run_id": row[21], "primary_kind": row[11],
                        "primary_subtype": row[12], "primary_authority": row[13],
                        "primary_organization": row[14], "primary_client": row[15],
                        "primary_project": row[16], "primary_workstream": row[17],
                        "topics_json": row[18], "equipment_json": row[19],
                        "activities_json": row[20],
                        "resource_binding_json": row[22],
                    }.items() if value not in (None, "", "[]", "{}")
                },
                resource_binding_json=None if row[22] is None else str(row[22]),
            )
        )
    next_cursor = None if not rows else (str(rows[-1][0]), str(rows[-1][1]))
    return tuple(selected), next_cursor


def _has_current_catalog_rows(connection: Any, root: Path) -> bool:
    after: tuple[str, str] | None = None
    while True:
        where = ["active=1", "last_seen_catalog_run_id IS NOT NULL"]
        parameters: list[object] = []
        if after is not None:
            where.append("(source_kind>? OR (source_kind=? AND file_key>?))")
            parameters.extend((after[0], after[0], after[1]))
        rows = connection.execute(
            f"""SELECT source_kind,file_key,path FROM documents
            WHERE {' AND '.join(where)} ORDER BY source_kind,file_key LIMIT ?""",
            (*parameters, DEFAULT_PAGE_SIZE),
        ).fetchall()
        if not rows:
            return False
        if any(_path_in_root(str(item[2]), root) for item in rows):
            return True
        if len(rows) < DEFAULT_PAGE_SIZE:
            return False
        after = (str(rows[-1][0]), str(rows[-1][1]))


def _representation_inputs(
    config: FrameworkConfig,
    *,
    root: Path,
    catalog_connection: Any,
    cancellation: object | None,
    counters: _LifecycleCounters,
    page_size: int,
) -> Iterator[object]:
    after: tuple[str, str] | None = None
    budgets = RepresentationBudgets(
        max_chars=24_000,
        max_tokens=4_096,
        max_metadata_chars=4_096,
        max_headings=32,
        max_fragments=4,
        max_fragment_chars=3_000,
        max_views=1,
    )
    while True:
        _checkpoint(cancellation)
        catalog_fence = capture_sqlite_read_fence(Path(config.document_catalog_database))
        documents, next_cursor = _catalog_page(
            catalog_connection, root=root, after=after, limit=page_size,
        )
        if not next_cursor:
            return
        counters.catalog_candidates += len(documents)
        by_kind: dict[str, list[SimpleNamespace]] = {}
        for document in documents:
            by_kind.setdefault(document.source_kind, []).append(document)
        representations: dict[tuple[str, str], object] = {}
        for source_kind, group in sorted(by_kind.items()):
            _checkpoint(cancellation)
            route_database = _route_path(config, source_kind)
            if not route_database.is_file():
                counters.route_unavailable += len(group)
                continue
            try:
                route_fence = capture_sqlite_read_fence(route_database)
                mode = preferred_sqlite_read_mode(route_database)
                with SQLiteReadSession(
                    route_database,
                    mode=mode,
                    timeout_seconds=60.0,
                ) as route_connection:
                    pages = tuple(
                        iter_catalog_route_source_pages(
                            group,
                            route_connection,
                            page_size=page_size,
                            route_fence=route_fence,
                            catalog_fence=catalog_fence,
                            max_text_chars=budgets.max_source_chars,
                            cancellation=cancellation,
                        )
                    )
                if capture_sqlite_read_fence(route_database) != route_fence:
                    raise FastCurationLifecycleError(
                        f"route source changed during Fast Curation read: {source_kind}"
                    )
                for page in pages:
                    for source in page.items:
                        try:
                            representation = build_document_semantic_representation(
                                source, budgets=budgets,
                            )
                            from neocortex.documents.document_resource_binding import (
                                parse_resource_binding,
                            )

                            raw_binding = source.metadata.get("resource_binding_json")
                            binding = parse_resource_binding(raw_binding)
                            if binding["physical_anchor_path"] != source.path:
                                raise CurationSourceError(
                                    "Catalog resource binding path is not current"
                                )
                            representation = replace(
                                representation,
                                deterministic_evidence={"source_binding": binding},
                            )
                        except (RepresentationInputError, CurationSourceError, ValueError):
                            counters.representation_unavailable += 1
                            continue
                        representations[(source.source_kind, source.file_key)] = representation
            except FastCurationLifecycleError:
                raise
            except (KeyboardInterrupt, CancellationRequested):
                raise
            except BaseException as exc:
                counters.source_errors += 1
                raise FastCurationLifecycleError(
                    f"route source read failed for {source_kind}: {type(exc).__name__}"
                ) from exc
        # Preserve the Catalog keyset order rather than route-kind grouping.
        for document in documents:
            representation = representations.get((document.source_kind, document.file_key))
            if representation is not None:
                counters.representations += 1
                yield representation
        after = next_cursor


def _stage_payload(
    *,
    root: Path,
    run_id: int,
    status: str,
    counters: _LifecycleCounters,
    result: object | None = None,
    reason: str | None = None,
    model_signature: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": STAGE_SCHEMA,
        "root": str(root),
        "run_id": run_id,
        "status": status,
        "candidates": counters.catalog_candidates,
        "representations": counters.representations,
        "route_unavailable": counters.route_unavailable,
        "representation_unavailable": counters.representation_unavailable,
        "source_errors": counters.source_errors,
        "representation_version": REPRESENTATION_VERSION,
    }
    if model_signature is not None:
        payload["model_signature"] = model_signature
    if reason is not None:
        payload["reason"] = reason
    if result is not None:
        metrics = getattr(result, "metrics", None)
        if metrics is not None and callable(getattr(metrics, "as_dict", None)):
            payload.update(metrics.as_dict())
        payload["model_available"] = bool(getattr(metrics, "model_available", False)) if metrics else False
        payload["calibration_loaded"] = bool(getattr(metrics, "calibration_loaded", False)) if metrics else False
        errors = tuple(getattr(result, "errors", ()) or ())
        payload["errors"] = tuple(str(item)[:256] for item in errors[:32])
        samples = tuple(getattr(result, "decision_samples", ()) or ())
        if samples:
            first_sample = samples[0]
            for field_name in (
                "prototype_set_fingerprint",
                "prototype_version",
                "policy_version",
                "calibration_version",
            ):
                value = _field(first_sample, field_name, None)
                if value not in (None, ""):
                    payload[field_name] = str(value)[:256]
        payload["decision_samples"] = tuple(
            item.as_dict() if callable(getattr(item, "as_dict", None)) else str(item)[:512]
            for item in samples[:MAX_DECISION_SAMPLES]
        )
    return payload


def _publish_stage(state: object, run_id: int, status: str, payload: Mapping[str, object]) -> None:
    publish = getattr(state, "publish_run_stage", None)
    if callable(publish):
        publish(
            run_id,
            STAGE_NAME,
            status,
            details=dict(payload),
            idempotency_key=f"{STAGE_NAME}:{status}",
        )


def run_fast_curation_stage(
    config: FrameworkConfig,
    *,
    root: Path,
    state: FrameworkState,
    run_id: int,
    cancellation: CancellationToken,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    """Run Fast Curation after routes/Catalog and before Organization.

    The Catalog connection is the only writer opened by this adapter.  Route
    snapshots are opened page-by-page and closed before the service can call
    the decision sink, so a live route owner is never held across a Catalog
    write.  A missing Catalog is a safe empty stage and never loads a model.
    """

    del progress
    root = Path(root)
    state_directory = Path(config.state_directory)
    catalog_path = Path(config.document_catalog_database)
    counters = _LifecycleCounters()
    started = time.monotonic()
    read_stages = getattr(state, "read_run_stages", None)
    if callable(read_stages):
        prior = [
            event for event in read_stages(run_id)
            if isinstance(event, Mapping) and event.get("stage") == STAGE_NAME
        ]
        if prior and prior[-1].get("status") in {"completed", "skipped"}:
            details = prior[-1].get("details")
            if isinstance(details, Mapping):
                return dict(details)
    if not catalog_path.is_file():
        payload = _stage_payload(
            root=root, run_id=run_id, status="skipped", counters=counters,
            reason="catalog_unavailable",
        )
        _publish_stage(state, run_id, "skipped", payload)
        return payload

    from neocortex.semantic.fast_curation_service import (
        FastCurationConfig,
        run_fast_curation,
    )
    from neocortex.semantic.fast_curation_policy_bundle import default_calibrated_policy
    from neocortex.semantic.fast_curation_prototypes import default_prototypes
    from neocortex.documents.document_catalog import document_catalog_database

    bundle_error: str | None = None
    try:
        policy_bundle = default_calibrated_policy(
            required_versions={"representation_version": REPRESENTATION_VERSION}
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        # A missing/corrupt measured artifact is a capability gap, never a
        # reason to invent thresholds or silently choose another model.
        policy_bundle = None
        bundle_error = type(exc).__name__
    prototype_set = default_prototypes()
    if policy_bundle is not None:
        service_config = FastCurationConfig.from_policy_bundle(
            policy_bundle,
            model_cache_override=(
                None if config.curation_model_cache is None else Path(config.curation_model_cache)
            ),
            threads=config.curation_threads,
            batch_size=(config.curation_batch_size or 32),
            max_views=1,
            max_documents=500_000,
            representation_max_chars=24_000,
            route_name="fast-curation",
        )
        model = service_config.model
        bundle_for_run = policy_bundle
    else:
        # FastCurationConfig supplies the service's benchmark-backed local
        # default model, but without a measured bundle the service must remain
        # fail-safe and abstain without loading it.
        service_config = FastCurationConfig(
            local_files_only=True,
            model_cache_override=(
                None if config.curation_model_cache is None else Path(config.curation_model_cache)
            ),
            threads=config.curation_threads,
            batch_size=(config.curation_batch_size or 32),
            max_views=1,
            max_documents=500_000,
            representation_max_chars=24_000,
            route_name="fast-curation",
        )
        model = service_config.model
        bundle_for_run = None
    page_size = config.curation_batch_size or DEFAULT_PAGE_SIZE
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        raise ValueError("curation_batch_size must be an integer")
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    details = {
        "schema": STAGE_SCHEMA,
        "root": str(root),
        "run_id": run_id,
        "catalog": str(catalog_path),
        "model_signature": model.model_signature,
        "representation_version": REPRESENTATION_VERSION,
        "local_files_only": True,
        "policy_bundle": "loaded" if policy_bundle is not None else "unavailable",
    }
    if bundle_error is not None:
        details["policy_bundle_error"] = bundle_error
    _publish_stage(state, run_id, "running", details)
    try:
        with document_catalog_database(catalog_path) as catalog_connection:
            if not _has_current_catalog_rows(catalog_connection, root):
                payload = _stage_payload(
                root=root, run_id=run_id, status="skipped", counters=counters,
                    reason="no_current_catalog_candidates", model_signature=model.model_signature,
                )
                _publish_stage(state, run_id, "skipped", payload)
                return payload
            cache = _CatalogEmbeddingCache(
                catalog_connection,
                model=model,
                representation_version=REPRESENTATION_VERSION,
            )
            sink = _CatalogDecisionSink(catalog_connection, model=model, root=root)
            try:
                result = run_fast_curation(
                    service_config,
                    root,
                    state_directory,
                    framework_state=state,
                    run_id=run_id,
                    inputs=_representation_inputs(
                        config,
                        root=root,
                        catalog_connection=catalog_connection,
                        cancellation=cancellation,
                        counters=counters,
                        page_size=page_size,
                    ),
                    embedding_cache=cache,
                    decision_sink=sink,
                    policy_bundle=bundle_for_run,
                    prototypes=prototype_set,
                    cancellation=cancellation,
                    resource_coordinator=current_resource_coordinator(),
                )
            finally:
                cache.flush()
            status = "completed" if str(getattr(result, "status", "completed")) == "completed" and counters.source_errors == 0 else "partial"
            payload = _stage_payload(
                root=root, run_id=run_id, status=status, counters=counters, result=result,
                model_signature=model.model_signature,
            )
            payload["elapsed_seconds"] = max(0.0, time.monotonic() - started)
            _publish_stage(state, run_id, status, payload)
            return payload
    except (KeyboardInterrupt, CancellationRequested) as exc:
        payload = _stage_payload(
            root=root, run_id=run_id, status="interrupted", counters=counters,
            reason=type(exc).__name__,
        )
        _publish_stage(state, run_id, "interrupted", payload)
        raise
    except BaseException as exc:
        payload = _stage_payload(
            root=root, run_id=run_id, status="failed", counters=counters,
            reason=type(exc).__name__,
        )
        _publish_stage(state, run_id, "failed", payload)
        raise


__all__ = [
    "ROUTE_DATABASE_NAMES",
    "STAGE_NAME",
    "STAGE_SCHEMA",
    "FastCurationLifecycleError",
    "run_fast_curation_stage",
]
