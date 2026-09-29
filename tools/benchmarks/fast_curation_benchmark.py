"""Offline benchmark for document-level Fast Curation Semantic.

This module is deliberately a benchmark owner, not a production classifier.  It
uses the repository's shared ``FastEmbedBackend`` in local-files-only mode and
keeps prototype/calibration/held-out evaluation separate.  No SQLite state,
corpus files, or model cache files are written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import numpy as np
except ImportError:  # pragma: no cover - runtime dependency of the benchmark
    np = None  # type: ignore[assignment]

from neocortex.semantic.semantic_backends import (
    EmbeddingBackend,
    FastEmbedBackend,
    iter_embedding_batches,
)
from neocortex.semantic.semantic_config import (
    SemanticModelUnavailableError,
    compact_multilingual_text_model,
    local_fastembed_snapshot,
    multilingual_text_model,
    text_chunking_for_model,
)
from neocortex.documents.curation_sources import CurationSource, DerivedContent, PhysicalIdentity
from neocortex.documents.document_semantic_representation import (
    REPRESENTATION_VERSION,
    RepresentationBudgets,
    build_document_representation,
)
from neocortex.semantic.fast_curation_prototypes import prototype_set_from_records
from neocortex.semantic.fast_curation_embeddings import (
    CurationRepresentation,
    fit_text_to_model,
    coerce_representation,
    _rank_candidates,
)
from neocortex.semantic.fast_curation_policy import (
    CalibrationParameters,
    FastCurationPolicy,
    RankedCandidate,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    fingerprint_text,
)
from neocortex.semantic.semantic_chunking import TextSection, chunk_text_sections


DEFAULT_FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/curation_semantic_holdout.json"
DEFAULT_CACHE = Path.home() / ".local/share/Neocortex/models/fastembed"
DEFAULT_BATCHES = (16, 32, 64)
DEFAULT_MAX_REPRESENTATION_CHARS = 4_000
DEFAULT_TARGET_PRECISION = 0.99
PRODUCTION_MAX_TOKENS = 2_048


@dataclass(frozen=True, slots=True)
class CalibrationPolicy:
    """Per-predicted-family score and margin gate calibrated on validation only."""

    thresholds: Mapping[str, tuple[float, float]]
    target_precision: float
    calibration_count: int


@dataclass(frozen=True, slots=True)
class ScoreRecord:
    record_id: str
    truth: str | None
    expected_disposition: str
    predicted: str | None
    family: str | None
    score: float
    margin: float


@dataclass(frozen=True, slots=True)
class EmbeddingRun:
    vectors: Mapping[str, tuple[float, ...]]
    batch_seconds: tuple[float, ...]
    total_seconds: float
    first_batch_seconds: float
    embedding_count: int

    @property
    def throughput(self) -> float:
        return self.embedding_count / self.total_seconds if self.total_seconds else 0.0

    @property
    def p50_batch_seconds(self) -> float:
        return _percentile(self.batch_seconds, 50.0)

    @property
    def p95_batch_seconds(self) -> float:
        return _percentile(self.batch_seconds, 95.0)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    if np is not None:
        return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil((percentile / 100) * len(ordered)) - 1))
    return float(ordered[index])


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    """Load and validate the synthetic fixture without touching production state."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "neocortex.fast-curation-semantic-fixture/v1":
        raise ValueError("fixture schema is unsupported")
    labels = payload.get("labels")
    records = payload.get("records")
    if not isinstance(labels, list) or not isinstance(records, list):
        raise ValueError("fixture labels and records must be lists")
    label_ids = {str(item["id"]) for item in labels if isinstance(item, dict) and "id" in item}
    if len(label_ids) != len(labels):
        raise ValueError("fixture labels are not unique")
    ids: set[str] = set()
    split_ids: dict[str, set[str]] = {
        "prototype": set(),
        "validation": set(),
        "heldout": set(),
        "fresh_heldout": set(),
    }
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("fixture record is not an object")
        record_id = record.get("id")
        split = record.get("split")
        if not isinstance(record_id, str) or record_id in ids:
            raise ValueError("fixture record ids are missing or duplicated")
        if split not in split_ids:
            raise ValueError(f"unsupported fixture split: {split!r}")
        ids.add(record_id)
        split_ids[split].add(record_id)
        label = record.get("label")
        if label is not None and label not in label_ids:
            raise ValueError(f"fixture record uses unknown label: {label!r}")
        if not isinstance(record.get("text"), str) or not record["text"].strip():
            raise ValueError("fixture record text is empty")
    if any(not split_ids[name] for name in split_ids):
        raise ValueError("fixture must contain all three disjoint splits")
    if len(records) < 100:
        raise ValueError("fixture must contain more than 100 examples")
    manifest = payload.get("prototype_manifest", ())
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("fixture must provide an explicit prototype_manifest")
    if any(
        not isinstance(item, dict)
        or item.get("family") not in {"document_kind", "topic", "activity"}
        or not isinstance(item.get("description"), str)
        or not item["description"].strip()
        for item in manifest
    ):
        raise ValueError("prototype_manifest contains an invalid or label-only entry")
    prototype_set_from_records(manifest)
    result = dict(payload)
    result["labels"] = labels
    result["records"] = records
    result["prototype_manifest"] = manifest
    return result


def label_index(fixture: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): dict(item) for item in fixture["labels"]}


def fixture_prototype_records(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project the shared controlled prototype manifest into benchmark views.

    The service owns three controlled axes.  This benchmark keeps a bounded
    combined-label comparison for precision reporting, but its prototype text
    is assembled only from the same explicit axis manifest consumed by the
    service; no second prototype vocabulary is invented here.
    """

    labels = label_index(fixture)
    manifest = fixture.get("prototype_manifest", ())
    prototype_set = prototype_set_from_records(manifest)
    result: list[dict[str, Any]] = []
    for label_id, label in labels.items():
        fragments: list[str] = []
        prototype_texts: list[str] = []
        for prototype, prototype_dto in zip(manifest, prototype_set.prototypes, strict=True):
            provenance = prototype.get("provenance", {})
            combined = provenance.get("combined_labels", ()) if isinstance(provenance, Mapping) else ()
            if label_id not in combined:
                continue
            prototype_texts.append(prototype_dto.text)
            fragments.append(str(prototype.get("description", "")))
            fragments.extend(str(value) for value in prototype.get("aliases", ())[:8])
            fragments.extend(str(value) for value in prototype.get("positive_examples", ())[:4])
        if not fragments:
            raise ValueError(f"label {label_id!r} has no shared prototype manifest evidence")
        result.append(
            {
                "id": f"prototype:{label_id}",
                "split": "prototype",
                "label": label_id,
                "document_kind": label.get("nature", ""),
                "topic": label.get("topic", ""),
                "activity": label.get("activity", ""),
                "title": label_id,
                "path_context": "synthetic prototype manifest",
                "text": " ".join(fragments),
                "prototype_text": " ".join(prototype_texts),
            }
        )
    return result


def _record_source(record: Mapping[str, Any]) -> CurationSource:
    """Adapt one synthetic route record to the production source DTO."""

    document_id = str(record["id"])
    return CurationSource(
        source_kind="text",
        file_key=document_id,
        path=str(record.get("path_context", f"/synthetic/{document_id}")),
        physical_identity=PhysicalIdentity("synthetic-volume", document_id, -1),
        content_signature=f"synthetic-content:{document_id}:v1",
        sections=(
            DerivedContent(
                "synthetic-derived-text",
                "body",
                str(record.get("text", "")),
                {"source": "synthetic-fixture"},
            ),
        ),
        metadata={
            "title": str(record.get("title", "")),
        },
    )


def _record_representation(
    record: Mapping[str, Any],
    *,
    max_chars: int,
) -> Any:
    if type(max_chars) is not int or max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    return build_document_representation(
        _record_source(record),
        budgets=RepresentationBudgets(
            max_chars=max_chars,
            max_tokens=PRODUCTION_MAX_TOKENS,
            max_views=1,
        ),
        version=REPRESENTATION_VERSION,
    )


def document_representation(
    record: Mapping[str, Any],
    *,
    max_chars: int = DEFAULT_MAX_REPRESENTATION_CHARS,
    include_path_context: bool = False,
) -> str:
    """Return the production bounded content view; path remains separate."""

    del include_path_context  # retained as a compatibility spelling for tests
    return str(_record_representation(record, max_chars=max_chars).embedding_text)


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _backend_bounded_text(
    backend: EmbeddingBackend,
    record: Mapping[str, Any],
    *,
    max_chars: int,
) -> str:
    """Fit one representation to the exact local tokenizer without truncation."""

    if record.get("prototype_text"):
        text = str(record["prototype_text"])
    else:
        representation = coerce_representation(
            _record_representation(record, max_chars=max_chars),
            max_chars=max_chars,
        )
        text = representation.embedding_views(max_views=1)[0]
    # This is the shared production fit contract; it preserves bounded head and
    # tail context and refuses backend truncation.
    return fit_text_to_model(backend, text, max_chars=max_chars)


def _requests(
    backend: EmbeddingBackend,
    records: Sequence[Mapping[str, Any]],
    *,
    max_chars: int,
    role: EmbeddingRole,
) -> tuple[EmbeddingRequest, ...]:
    return tuple(
        EmbeddingRequest(
            request_id=str(record["id"]),
            role=role,
            fingerprint=fingerprint_text(text := _backend_bounded_text(backend, record, max_chars=max_chars)),
            text=text,
        )
        for record in records
    )


def embed_records(
    backend: EmbeddingBackend,
    records: Sequence[Mapping[str, Any]],
    *,
    batch_size: int,
    max_chars: int = DEFAULT_MAX_REPRESENTATION_CHARS,
    role: EmbeddingRole = EmbeddingRole.PASSAGE,
) -> EmbeddingRun:
    """Embed records with actual bounded backend batches and retain no payload cache."""

    if not records:
        return EmbeddingRun({}, (), 0.0, 0.0, 0)
    if batch_size < 1 or batch_size > backend.max_batch_size:
        raise ValueError("batch_size exceeds backend bound")
    requests = _requests(backend, records, max_chars=max_chars, role=role)
    vectors: dict[str, tuple[float, ...]] = {}
    durations: list[float] = []
    started = time.perf_counter()
    for batch in _chunks(requests, batch_size):
        batch_started = time.perf_counter()
        outputs = tuple(iter_embedding_batches(backend, batch, batch_size=len(batch)))
        durations.append(time.perf_counter() - batch_started)
        for output in outputs:
            vectors[output.request_id] = tuple(float(value) for value in output.vector)
    total = time.perf_counter() - started
    if set(vectors) != {str(record["id"]) for record in records}:
        raise RuntimeError("embedding backend omitted or duplicated records")
    return EmbeddingRun(
        vectors=vectors,
        batch_seconds=tuple(durations),
        total_seconds=total,
        first_batch_seconds=durations[0] if durations else 0.0,
        embedding_count=len(vectors),
    )


def _normalized_matrix(vectors: Sequence[Sequence[float]]) -> Any:
    if np is None:
        raise RuntimeError("the benchmark requires NumPy")
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


def prototype_matrix(
    fixture: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    vectors: Mapping[str, Sequence[float]],
) -> tuple[tuple[str, ...], Any, dict[str, str]]:
    """Derive one centroid per label from prototype records only."""

    labels = label_index(fixture)
    prototype_records = [record for record in records if record["split"] == "prototype"]
    if any(record["split"] != "prototype" for record in prototype_records):
        raise RuntimeError("prototype leakage detected")
    grouped: dict[str, list[Sequence[float]]] = {}
    for record in prototype_records:
        label = record.get("label")
        if label is not None:
            grouped.setdefault(str(label), []).append(vectors[str(record["id"])])
    ordered = tuple(label_id for label_id in labels if label_id in grouped)
    centroids = []
    families: dict[str, str] = {}
    for label_id in ordered:
        centroids.append(_normalized_matrix(grouped[label_id]).mean(axis=0))
        families[label_id] = str(labels[label_id].get("family", label_id))
    return ordered, _normalized_matrix(centroids), families


def semantic_scores(
    records: Sequence[Mapping[str, Any]],
    vectors: Mapping[str, Sequence[float]],
    ordered_labels: Sequence[str],
    prototypes: Any,
) -> list[ScoreRecord]:
    if np is None:
        raise RuntimeError("the benchmark requires NumPy")
    matrix = _normalized_matrix([vectors[str(record["id"])] for record in records])
    scores = matrix @ prototypes.T
    results: list[ScoreRecord] = []
    for record, row in zip(records, scores, strict=True):
        order = np.argsort(-row, kind="stable")
        first = int(order[0])
        second = int(order[1]) if len(order) > 1 else first
        results.append(
            ScoreRecord(
                record_id=str(record["id"]),
                truth=None if record.get("label") is None else str(record["label"]),
                expected_disposition=str(record.get("expected_disposition", "classify")),
                predicted=ordered_labels[first],
                family=None,
                score=float(row[first]),
                margin=float(row[first] - row[second]),
            )
        )
    return results


def _heuristic_score(record: Mapping[str, Any], label: Mapping[str, Any]) -> float:
    text = " ".join(
        str(record.get(key, "")) for key in ("title", "text", "path_context", "document_kind", "topic", "activity")
    ).casefold()
    terms = tuple(str(term).casefold() for term in label.get("terms", ()))
    hits = sum(term in text for term in terms)
    score = hits / max(1, len(terms))
    if str(record.get("document_kind", "")) == str(label.get("nature", "")):
        score += 0.30
    if str(record.get("topic", "")) == str(label.get("topic", "")):
        score += 0.20
    if str(record.get("activity", "")) == str(label.get("activity", "")):
        score += 0.20
    return min(1.0, score)


def heuristic_scores(
    fixture: Mapping[str, Any], records: Sequence[Mapping[str, Any]], ordered_labels: Sequence[str]
) -> list[ScoreRecord]:
    labels = label_index(fixture)
    rows: list[ScoreRecord] = []
    for record in records:
        scores = [_heuristic_score(record, labels[label_id]) for label_id in ordered_labels]
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], ordered_labels[i]))
        first, second = order[0], order[1] if len(order) > 1 else order[0]
        rows.append(
            ScoreRecord(
                record_id=str(record["id"]),
                truth=None if record.get("label") is None else str(record["label"]),
                expected_disposition=str(record.get("expected_disposition", "classify")),
                predicted=ordered_labels[first] if scores[first] > 0 else None,
                family=None,
                score=float(scores[first]),
                margin=float(scores[first] - scores[second]),
            )
        )
    return rows


def hybrid_scores(
    semantic: Sequence[ScoreRecord],
    heuristic: Sequence[ScoreRecord],
) -> list[ScoreRecord]:
    by_id = {item.record_id: item for item in heuristic}
    rows: list[ScoreRecord] = []
    for item in semantic:
        baseline = by_id[item.record_id]
        score = 0.8 * ((item.score + 1.0) / 2.0) + 0.2 * baseline.score
        margin = 0.8 * item.margin + 0.2 * baseline.margin
        rows.append(
            ScoreRecord(
                item.record_id,
                item.truth,
                item.expected_disposition,
                item.predicted,
                None,
                score,
                margin,
            )
        )
    return rows


def calibrate_policy(
    validation: Sequence[ScoreRecord],
    families: Mapping[str, str],
    *,
    target_precision: float = DEFAULT_TARGET_PRECISION,
) -> CalibrationPolicy:
    """Choose score/margin gates using validation rows only."""

    if not 0.0 < target_precision <= 1.0:
        raise ValueError("target_precision must be in (0,1]")
    grouped: dict[str, list[ScoreRecord]] = {}
    for row in validation:
        family = families.get(str(row.predicted), "unknown") if row.predicted else "unknown"
        grouped.setdefault(family, []).append(row)
    thresholds: dict[str, tuple[float, float]] = {}
    for family, rows in grouped.items():
        score_values = sorted({0.0, *(max(0.0, min(1.0, row.score)) for row in rows)})
        margin_values = sorted({0.0, *(max(0.0, min(1.0, row.margin)) for row in rows)})
        candidates: list[tuple[float, float, float, float, float]] = []
        for score_floor in score_values:
            for margin_floor in margin_values:
                selected = [row for row in rows if row.score >= score_floor and row.margin >= margin_floor]
                if not selected:
                    continue
                correct = sum(row.truth is not None and row.predicted == row.truth for row in selected)
                precision = correct / len(selected)
                coverage = len(selected) / len(rows)
                if precision >= target_precision:
                    candidates.append((coverage, precision, -score_floor, -margin_floor, score_floor + margin_floor))
        if candidates:
            selected = max(candidates)
            thresholds[family] = (-selected[2], -selected[3])
        else:
            # Keep a fail-closed policy if synthetic validation cannot reach the
            # target: maximize precision, then coverage, with strict gates.
            fallback: list[tuple[float, float, float, float]] = []
            for score_floor in score_values:
                for margin_floor in margin_values:
                    selected_rows = [row for row in rows if row.score >= score_floor and row.margin >= margin_floor]
                    if not selected_rows:
                        continue
                    precision = sum(row.truth is not None and row.predicted == row.truth for row in selected_rows) / len(selected_rows)
                    fallback.append((precision, len(selected_rows) / len(rows), score_floor, margin_floor))
            if fallback:
                precision, _coverage, score_floor, margin_floor = max(fallback)
                thresholds[family] = (score_floor, margin_floor)
    return CalibrationPolicy(thresholds, target_precision, len(validation))


def _structural_contradiction(
    record: Mapping[str, Any],
    predicted: str | None,
    label_metadata: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Veto a semantic move when bounded route metadata contradicts its nature."""

    if predicted is None:
        return False
    label = label_metadata.get(predicted, {})
    route_structure = record.get("route_structure", {})
    if not isinstance(route_structure, Mapping):
        route_structure = {}
    if route_structure.get("ambiguous") is True:
        return True
    document_kind = route_structure.get("kind")
    predicted_kind = label.get("nature")
    if isinstance(document_kind, str) and document_kind and isinstance(predicted_kind, str):
        return document_kind != predicted_kind
    return False


def apply_policy(
    rows: Sequence[ScoreRecord],
    policy: CalibrationPolicy,
    families: Mapping[str, str],
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
    label_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    structural_veto: bool = False,
) -> tuple[ScoreRecord, ...]:
    selected: list[ScoreRecord] = []
    by_id = {str(record["id"]): record for record in records or ()}
    metadata = label_metadata or {}
    for row in rows:
        family = families.get(str(row.predicted), "unknown") if row.predicted else "unknown"
        floor = policy.thresholds.get(family)
        accepted = floor is not None and row.predicted is not None and row.score >= floor[0] and row.margin >= floor[1]
        vetoed = accepted and structural_veto and _structural_contradiction(
            by_id.get(row.record_id, {}), row.predicted, metadata
        )
        selected.append(
            row
            if accepted and not vetoed
            else ScoreRecord(
                row.record_id,
                row.truth,
                row.expected_disposition,
                None,
                "structural_veto" if vetoed else family,
                row.score,
                row.margin,
            )
        )
    return tuple(selected)


def metrics(rows: Sequence[ScoreRecord]) -> dict[str, Any]:
    total = len(rows)
    accepted = [row for row in rows if row.predicted is not None]
    correct = [row for row in accepted if row.truth is not None and row.predicted == row.truth]
    false_moves = [row for row in accepted if row.truth is None or row.predicted != row.truth]
    known = [row for row in rows if row.truth is not None]
    confusion = Counter(f"{row.truth or 'abstain'}->{row.predicted or 'abstain'}" for row in false_moves)
    return {
        "documents": total,
        "accepted": len(accepted),
        "abstentions": total - len(accepted),
        "precision": len(correct) / len(accepted) if accepted else 0.0,
        "coverage": len(accepted) / total if total else 0.0,
        "known_coverage": sum(row in accepted for row in known) / len(known) if known else 0.0,
        "false_automatic_moves": len(false_moves),
        "false_move_rate": len(false_moves) / total if total else 0.0,
        "ambiguous_abstentions": sum(row.expected_disposition == "abstain" and row.predicted is None for row in rows),
        "confusion_pairs": dict(confusion.most_common(20)),
    }


def _rss_mb() -> float:
    # Linux ru_maxrss is KiB; retain a portable fallback for test doubles.
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def model_spec(name: str) -> EmbeddingModelSpec:
    if name == "minilm":
        return compact_multilingual_text_model()
    if name == "jina":
        return multilingual_text_model()
    raise ValueError(f"unknown benchmark model: {name}")


def _prototype_query_records(fixture: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    prototype_set = prototype_set_from_records(fixture["prototype_manifest"])
    return tuple(
        {
            "id": prototype.prototype_id,
            "split": "prototype",
            "label": prototype.concept_id,
            "title": prototype.label,
            "text": prototype.text,
            "prototype_text": prototype.text,
            "path_context": "",
        }
        for prototype in prototype_set.prototypes
    )


def _curation_representation(record: Mapping[str, Any], *, max_chars: int) -> CurationRepresentation:
    document = _record_representation(record, max_chars=max_chars)
    route_structure = record.get("route_structure", {})
    structural: dict[str, object] = {}
    if isinstance(route_structure, Mapping):
        if route_structure.get("ambiguous") is True:
            structural["document_kind_conflict"] = True
        elif isinstance(route_structure.get("kind"), str):
            structural.update(
                {
                    "document_kind_status": "catalog",
                    "document_kind_concept_id": route_structure["kind"],
                }
            )
    # This is the service DTO projection of the production representation; the
    # benchmark never sends the fixture's labeled axes as content metadata.
    return CurationRepresentation(
        document_id=str(record["id"]),
        source_kind="text",
        file_key=str(record["id"]),
        input_signature=document.content_signature,
        content_fingerprint=document.content_fingerprint,
        representation_identity=document.fingerprint,
        representation_version=document.version,
        content_text=document.embedding_text,
        path_context=document.context_text,
        title=document.title,
        metadata=document.provenance.get("selected", {}),
        headings=document.headings,
        opening=document.opening,
        representative_body=document.representative_body,
        conclusion=document.conclusion,
        views=document.view_texts,
        derived_sources=(str(document.provenance.get("source_kind", "text")),),
        structural_evidence=structural,
    )


def _calibrate_axis_thresholds(
    rows: Sequence[dict[str, object]],
    *,
    target_precision: float = DEFAULT_TARGET_PRECISION,
) -> tuple[float, float]:
    values = sorted(
        {
            (0.0, 0.0),
            *(
                (max(-1.0, min(1.0, float(row["score"]))), max(0.0, min(2.0, float(row["margin"]))))
                for row in rows
            ),
        }
    )
    candidates: list[tuple[float, float, float, float]] = []
    for score_floor, margin_floor in values:
        selected = [
            row for row in rows
            if float(row["score"]) >= score_floor and float(row["margin"]) >= margin_floor
        ]
        if not selected:
            continue
        precision = sum(row["truth"] is not None and row["predicted"] == row["truth"] for row in selected) / len(selected)
        coverage = len(selected) / len(rows)
        if precision >= target_precision:
            candidates.append((coverage, precision, -score_floor, -margin_floor))
    if candidates:
        selected = max(candidates)
        return -selected[2], -selected[3]
    fallback: list[tuple[float, float, float, float]] = []
    for score_floor, margin_floor in values:
        selected = [
            row for row in rows
            if float(row["score"]) >= score_floor and float(row["margin"]) >= margin_floor
        ]
        if selected:
            precision = sum(row["truth"] is not None and row["predicted"] == row["truth"] for row in selected) / len(selected)
            fallback.append((precision, len(selected) / len(rows), score_floor, margin_floor))
    if not fallback:
        return 1.0, 2.0
    _precision, _coverage, score_floor, margin_floor = max(fallback)
    return score_floor, margin_floor


def _actual_decisions(
    records: Sequence[Mapping[str, Any]],
    representations: Mapping[str, CurationRepresentation],
    vectors: Mapping[str, Sequence[float]],
    prototype_set: Any,
    prototype_vectors: Mapping[str, Sequence[float]],
    policy: FastCurationPolicy,
) -> tuple[tuple[ScoreRecord, ...], dict[str, dict[str, object]], dict[str, tuple[RankedCandidate, ...]]]:
    if np is None:
        raise RuntimeError("the benchmark requires NumPy")
    prototype_order = tuple(prototype.prototype_id for prototype in prototype_set.prototypes)
    prototype_matrix = _normalized_matrix([prototype_vectors[prototype_id] for prototype_id in prototype_order])
    document_matrix = _normalized_matrix([vectors[str(record["id"])] for record in records])
    scores = document_matrix @ prototype_matrix.T
    primary: list[ScoreRecord] = []
    evidence_by_id: dict[str, dict[str, object]] = {}
    candidates_by_id: dict[str, tuple[RankedCandidate, ...]] = {}
    for record, row in zip(records, scores, strict=True):
        candidates = _rank_candidates(row.tolist(), prototype_set.prototypes, top_k=5)
        candidates_by_id[str(record["id"])] = candidates
        decision = policy.evaluate(
            candidates,
            text_chars=representations[str(record["id"])].text_chars,
            model_available=True,
            model_signature=policy.model_signature,
            representation_quality="adequate",
            structural_evidence=representations[str(record["id"])].structural_evidence,
            semantic_evidence={"score_kind": "cosine_similarity_not_probability"},
        )
        selected = decision.selected_by_family.get("document_kind")
        top1 = decision.top1_scores.get("document_kind")
        margin = decision.margins.get("document_kind")
        primary.append(
            ScoreRecord(
                record_id=str(record["id"]),
                truth=str(record["document_kind"]),
                expected_disposition=str(record.get("expected_disposition", "classify")),
                predicted=None if selected is None or not decision.classified else selected.concept_id,
                family="document_kind",
                score=0.0 if top1 is None else float(top1),
                margin=0.0 if margin is None else float(margin),
            )
        )
        evidence_by_id[str(record["id"])] = {
            "decision": decision,
            "representation_fingerprint": representations[str(record["id"])].representation_fingerprint,
            "scores_by_family": {
                family: {
                    "top1": decision.top1_scores.get(family),
                    "top2": decision.top2_scores.get(family),
                    "margin": decision.margins.get(family),
                }
                for family in ("document_kind", "topic", "activity")
            },
            "selected_by_family": {
                family: candidate.concept_id
                for family, candidate in decision.selected_by_family.items()
            },
        }
    return tuple(primary), evidence_by_id, candidates_by_id


def benchmark_model(
    fixture: Mapping[str, Any],
    *,
    model_name: str,
    cache_dir: Path,
    batch_size: int,
    threads: int = 1,
    max_chars: int = DEFAULT_MAX_REPRESENTATION_CHARS,
    evaluation_split: str = "heldout",
    required_families: tuple[str, ...] = ("document_kind", "topic", "activity"),
) -> dict[str, Any]:
    """Run the actual Fast Curation DTO/prototype/policy path."""

    model = model_spec(model_name)
    snapshot = local_fastembed_snapshot(model, cache_dir)
    load_started = time.perf_counter()
    backend = FastEmbedBackend(
        model,
        cache_dir=cache_dir,
        local_files_only=True,
        threads=threads,
        batch_size=batch_size,
        providers=("CPUExecutionProvider",),
    )
    construct_seconds = time.perf_counter() - load_started
    prototype_set = prototype_set_from_records(fixture["prototype_manifest"])
    prototype_records = list(_prototype_query_records(fixture))
    evaluation = [
        dict(record)
        for record in fixture["records"]
        if record["split"] in {"validation", evaluation_split}
    ]
    representations = {
        str(record["id"]): _curation_representation(record, max_chars=max_chars)
        for record in evaluation
    }
    prototype_embedding = embed_records(
        backend, prototype_records, batch_size=batch_size, max_chars=max_chars, role=EmbeddingRole.QUERY
    )
    evaluation_embedding = embed_records(
        backend, evaluation, batch_size=batch_size, max_chars=max_chars, role=EmbeddingRole.PASSAGE
    )
    vectors = {**prototype_embedding.vectors, **evaluation_embedding.vectors}
    embedding = EmbeddingRun(
        vectors=vectors,
        batch_seconds=prototype_embedding.batch_seconds + evaluation_embedding.batch_seconds,
        total_seconds=prototype_embedding.total_seconds + evaluation_embedding.total_seconds,
        first_batch_seconds=prototype_embedding.first_batch_seconds,
        embedding_count=prototype_embedding.embedding_count + evaluation_embedding.embedding_count,
    )
    prototype_vectors = {record["id"]: prototype_embedding.vectors[record["id"]] for record in prototype_records}
    # First obtain uncalibrated validation candidates with thresholds at the
    # permissive boundary, then choose score/margin gates from validation only.
    permissive = FastCurationPolicy(
        calibration=CalibrationParameters(
            calibration_version=f"benchmark-precalibration-{model_name}-v2",
            model_signature=model.model_signature,
            measured=True,
            min_score_by_family=dict.fromkeys(("document_kind", "topic", "activity"), -1.0),
            min_margin_by_family=dict.fromkeys(("document_kind", "topic", "activity"), 0.0),
            min_evidence_count=2,
            allow_single_candidate=False,
        ),
        required_families=("document_kind", "topic", "activity"),
    )
    validation_records = [record for record in evaluation if record["split"] == "validation"]
    _, validation_evidence, _ = _actual_decisions(
        validation_records, representations, vectors, prototype_set, prototype_vectors, permissive
    )
    thresholds: dict[str, tuple[float, float]] = {}
    for family in ("document_kind", "topic", "activity"):
        rows = []
        for record in validation_records:
            evidence = validation_evidence[str(record["id"])]
            score = evidence["scores_by_family"][family]["top1"]
            margin = evidence["scores_by_family"][family]["margin"]
            # Permissive policy selected the top candidate for every family.
            candidates = evidence["decision"].candidates
            predicted = next((candidate.concept_id for candidate in candidates if candidate.family == family), None)
            rows.append({"score": score or -1.0, "margin": margin or 0.0, "truth": record[family], "predicted": predicted})
        thresholds[family] = _calibrate_axis_thresholds(rows)
    calibration_version = f"benchmark-validation-{model_name}-v2"
    parameters = CalibrationParameters(
        calibration_version=calibration_version,
        model_signature=model.model_signature,
        measured=True,
        min_score_by_family={family: values[0] for family, values in thresholds.items()},
        min_margin_by_family={family: values[1] for family, values in thresholds.items()},
        min_text_chars=1,
        min_evidence_count=2,
        min_views_agreement=1.0,
        confidence_floor=0.0,
        allow_single_candidate=False,
        provenance={"source": "validation_only", "records": len(validation_records)},
    )
    policy = FastCurationPolicy.from_calibration(
        parameters,
        required_families=required_families,
    )
    evaluation_records = [record for record in evaluation if record["split"] == evaluation_split]
    primary, evidence_by_id, _ = _actual_decisions(
        evaluation_records, representations, vectors, prototype_set, prototype_vectors, policy
    )
    axis_rows: dict[str, list[ScoreRecord]] = {family: [] for family in ("document_kind", "topic", "activity")}
    for record in evaluation_records:
        evidence = evidence_by_id[str(record["id"])]
        decision = evidence["decision"]
        for family in axis_rows:
            selected = decision.selected_by_family.get(family)
            axis_rows[family].append(
                ScoreRecord(
                    str(record["id"]),
                    str(record[family]),
                    str(record.get("expected_disposition", "classify")),
                    None if selected is None or not decision.classified else selected.concept_id,
                    family,
                    float(decision.top1_scores.get(family) or 0.0),
                    float(decision.margins.get(family) or 0.0),
                )
            )
    per_document = []
    for row in primary:
        evidence = evidence_by_id[row.record_id]
        decision = evidence["decision"]
        per_document.append(
            {
                "record_id": row.record_id,
                "truth_by_family": {family: next(item.truth for item in axis_rows[family] if item.record_id == row.record_id) for family in axis_rows},
                "calibrated_decision": decision.decision.value,
                "decision_reason": decision.reason,
                "representation_fingerprint": evidence["representation_fingerprint"],
                "scores_by_family": evidence["scores_by_family"],
                "selected_by_family": evidence["selected_by_family"],
            }
        )
    from neocortex.semantic.fast_curation_policy_bundle import FastCurationPolicyBundle
    bundle = FastCurationPolicyBundle(
        policy=policy,
        model_signature=model.model_signature,
        representation_version=REPRESENTATION_VERSION,
        ontology_version=prototype_set.ontology_version,
        prototype_version=prototype_set.prototype_version,
        policy_version=policy.policy_version,
        calibration_version=calibration_version,
        prototype_set_fingerprint=prototype_set.fingerprint,
        prototype_scope=prototype_set.ontology_id,
    )
    backend.close()
    return {
        "model": model_name,
        "model_id": model.model_id,
        "model_signature": model.model_signature,
        "dimensions": model.dimensions,
        "cache_snapshot": str(snapshot),
        "batch_size": batch_size,
        "representation_version": REPRESENTATION_VERSION,
        "prototype_count": len(prototype_set.prototypes),
        "prototype_set_fingerprint": prototype_set.fingerprint,
        "prototype_version": prototype_set.prototype_version,
        "prototype_scope": prototype_set.ontology_id,
        "validation_count": len(validation_records),
        "heldout_count": len(evaluation_records),
        "evaluation_split": evaluation_split,
        "load_time_sec": construct_seconds + embedding.first_batch_seconds,
        "construct_time_sec": construct_seconds,
        "embedding_time_sec": embedding.total_seconds,
        "throughput_documents_sec": embedding.throughput,
        "batch_latency_p50_sec": embedding.p50_batch_seconds,
        "batch_latency_p95_sec": embedding.p95_batch_seconds,
        "vectors_per_document": 1.0,
        "cache_hit_ratio": 0.0,
        "cache": {
            "mode": "benchmark_ephemeral_no_persistence",
            "key_schema": "fast-curation-content-embedding-v1|content_fingerprint|representation_fingerprint|representation_version|model_signature|vector_space|dimensions",
            "hits": 0,
            "misses": embedding.embedding_count,
        },
        "peak_rss_mb": _rss_mb(),
        "semantic": metrics(primary),
        "axis_metrics": {
            family: metrics(rows) if family in required_families else {"status": "not_evaluated"}
            for family, rows in axis_rows.items()
        },
        "required_families": list(required_families),
        "policy_choice_fingerprint": hashlib.sha256(
            json.dumps(list(required_families), separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "calibration": {
            "target_precision": DEFAULT_TARGET_PRECISION,
            "validation_only": True,
            "thresholds_by_family": {key: list(value) for key, value in thresholds.items()},
            "bundle": bundle.as_dict(),
        },
        "per_document_scores": per_document,
        "_heldout_rows": [_score_to_payload(row) for row in primary],
        "status": "complete",
    }


def _long_synthetic_records(*, count: int = 20, size_bytes: int = 100 * 1024) -> tuple[dict[str, Any], ...]:
    """Create bounded, deterministic long derived-text inputs without PII."""

    if count < 1 or size_bytes < 1:
        raise ValueError("long benchmark dimensions must be positive")
    records: list[dict[str, Any]] = []
    technical = (
        "Synthetic technical maintenance record. Transformer U5 insulation, winding resistance, "
        "protection relay checks, measured values and acceptance criteria are described for a "
        "fictional engineering package. "
    )
    administrative = (
        "Synthetic administrative record. Invoice control, purchase order, delivery milestone, "
        "supplier review and payment reconciliation are described for a fictional project. "
    )
    for index in range(count):
        base = technical if index % 2 == 0 else administrative
        repeated = (base * ((size_bytes // len(base)) + 2)).encode("utf-8")[:size_bytes]
        text = repeated.decode("utf-8", errors="ignore")
        records.append(
            {
                "id": f"long-cost-{index:03d}",
                "split": "long_cost",
                "label": "technical" if index % 2 == 0 else "administrative",
                "expected_disposition": "classify",
                "language": "en",
                "document_kind": "report" if index % 2 == 0 else "invoice",
                "topic": "synthetic_long_document",
                "activity": "measurement" if index % 2 == 0 else "administration",
                "title": f"Synthetic long document {index:03d}",
                "path_context": f"/synthetic/long/{index:03d}",
                "text": text,
            }
        )
    return tuple(records)


def _embed_request_sequence(
    backend: EmbeddingBackend,
    requests: Sequence[EmbeddingRequest],
    *,
    batch_size: int,
) -> EmbeddingRun:
    started = time.perf_counter()
    durations: list[float] = []
    vectors: dict[str, tuple[float, ...]] = {}
    for batch in _chunks(requests, batch_size):
        batch_started = time.perf_counter()
        outputs = tuple(iter_embedding_batches(backend, batch, batch_size=len(batch)))
        durations.append(time.perf_counter() - batch_started)
        for output in outputs:
            vectors[output.request_id] = tuple(float(value) for value in output.vector)
    elapsed = time.perf_counter() - started
    return EmbeddingRun(
        vectors=vectors,
        batch_seconds=tuple(durations),
        total_seconds=elapsed,
        first_batch_seconds=durations[0] if durations else 0.0,
        embedding_count=len(vectors),
    )


def benchmark_long_document_cost(
    *,
    model_name: str,
    cache_dir: Path,
    batch_size: int = 32,
    document_count: int = 20,
    document_size_bytes: int = 100 * 1024,
) -> dict[str, Any]:
    """Compare one Fast Curation vector with actual Full Semantic chunk vectors.

    This measures shared encoder work only.  It deliberately does not open or
    publish a Full Semantic owner/index and reports that overhead separately.
    """

    model = model_spec(model_name)
    snapshot = local_fastembed_snapshot(model, cache_dir)
    backend = FastEmbedBackend(
        model,
        cache_dir=cache_dir,
        local_files_only=True,
        threads=1,
        batch_size=batch_size,
        providers=("CPUExecutionProvider",),
    )
    records = _long_synthetic_records(count=document_count, size_bytes=document_size_bytes)
    prototype_records = _prototype_query_records(load_fixture(DEFAULT_FIXTURE))
    setup_started = time.perf_counter()
    _prototype_run = embed_records(
        backend, prototype_records, batch_size=batch_size, max_chars=DEFAULT_MAX_REPRESENTATION_CHARS,
        role=EmbeddingRole.QUERY,
    )
    setup_seconds = time.perf_counter() - setup_started

    fast_started = time.perf_counter()
    fast_run = embed_records(
        backend, records, batch_size=batch_size, max_chars=DEFAULT_MAX_REPRESENTATION_CHARS,
        role=EmbeddingRole.PASSAGE,
    )
    fast_seconds = time.perf_counter() - fast_started
    fast_texts = [
        _backend_bounded_text(backend, record, max_chars=DEFAULT_MAX_REPRESENTATION_CHARS)
        for record in records
    ]
    fast_token_counts, fast_limit = backend.text_token_counts(fast_texts)

    full_started = time.perf_counter()
    _tokenizer_signature, token_limit = backend.text_tokenizer_contract()
    # Use the actual Full Semantic natural-window chunker, then apply the
    # shared exact-fit helper per chunk.  This avoids repeatedly retokenizing
    # speculative 100-KB windows while preserving the no-silent-truncation
    # contract before the encoder call.
    chunk_config = text_chunking_for_model(model)
    chunks = []
    for record in records:
        # Full Semantic receives the same synthetic derived bytes, not the
        # bounded curation representation.
        sections = (TextSection("synthetic-derived-text", "body", str(record["text"]), {}),)
        chunks.extend(chunk_text_sections(str(record["id"]), sections, chunk_config))
    fitted_chunk_texts = [fit_text_to_model(backend, chunk.text) for chunk in chunks]
    chunk_texts = fitted_chunk_texts
    chunk_token_counts, _ = backend.text_token_counts(chunk_texts) if chunk_texts else ((), token_limit)
    full_requests = tuple(
        EmbeddingRequest(
            request_id=chunk.chunk_id,
            role=EmbeddingRole.PASSAGE,
            fingerprint=fingerprint_text(chunk.text),
            text=fitted_chunk_texts[index],
        )
        for index, chunk in enumerate(chunks)
    )
    full_run = _embed_request_sequence(backend, full_requests, batch_size=batch_size)
    full_seconds = time.perf_counter() - full_started
    backend.close()
    return {
        "model": model_name,
        "model_id": model.model_id,
        "model_signature": model.model_signature,
        "dimensions": model.dimensions,
        "cache_snapshot": str(snapshot),
        "batch_size": batch_size,
        "documents": document_count,
        "bytes_per_document": document_size_bytes,
        "source_bytes_total": document_count * document_size_bytes,
        "prototype_count": len(prototype_records),
        "prototype_setup_seconds_excluded_from_paths": setup_seconds,
        "fast_curation": {
            "vectors": fast_run.embedding_count,
            "vectors_per_document": fast_run.embedding_count / document_count,
            "tokens": sum(fast_token_counts),
            "token_limit": fast_limit,
            "wall_seconds": fast_seconds,
            "documents_per_second": document_count / fast_seconds if fast_seconds else 0.0,
            "batch_p50_seconds": fast_run.p50_batch_seconds,
            "batch_p95_seconds": fast_run.p95_batch_seconds,
            "rss_peak_mb": _rss_mb(),
        },
        "full_semantic_chunk_embedding_only": {
            "chunk_config": chunk_config.signature,
            "chunks": len(chunks),
            "chunks_per_document": len(chunks) / document_count,
            "tokens": sum(chunk_token_counts),
            "token_limit": token_limit,
            "vectors": full_run.embedding_count,
            "wall_seconds": full_seconds,
            "chunks_per_second": len(chunks) / full_seconds if full_seconds else 0.0,
            "batch_p50_seconds": full_run.p50_batch_seconds,
            "batch_p95_seconds": full_run.p95_batch_seconds,
            "rss_peak_mb": _rss_mb(),
            "owner_index": "not run",
        },
        "overhead_not_observed": [
            "Full Semantic SQLite owner/index writes",
            "generation/publication/lineage persistence",
            "route extraction and corpus I/O",
            "persistent embedding cache writes",
        ],
    }

def benchmark_cascade(minilm: Mapping[str, Any], jina: Mapping[str, Any]) -> dict[str, Any]:
    """Apply MiniLM first and Jina only to MiniLM abstentions."""

    if minilm.get("status") != "complete" or jina.get("status") != "complete":
        return {
            "stage_1": "minilm",
            "stage_2": "jina_on_minilm_abstentions",
            "status": "missing_or_failed_model",
        }
    first = {row["record_id"]: row for row in minilm.get("_heldout_rows", ())}
    second = {row["record_id"]: row for row in jina.get("_heldout_rows", ())}
    combined: list[ScoreRecord] = []
    escalated = stage_one_accepts = stage_two_accepts = 0
    for record_id in sorted(first):
        left = _score_from_payload(first[record_id])
        right = _score_from_payload(second[record_id])
        if left.predicted is not None:
            stage_one_accepts += 1
            combined.append(left)
        elif right.predicted is not None:
            escalated += 1
            stage_two_accepts += 1
            combined.append(right)
        else:
            combined.append(left)
    return {
        "stage_1": "minilm",
        "stage_2": "jina_on_minilm_abstentions",
        "status": "complete",
        "stage_one_accepts": stage_one_accepts,
        "escalated_to_stage_two": escalated,
        "stage_two_accepts": stage_two_accepts,
        "metrics": metrics(combined),
    }


def _score_to_payload(row: ScoreRecord) -> dict[str, Any]:
    return {
        "record_id": row.record_id,
        "truth": row.truth,
        "expected_disposition": row.expected_disposition,
        "predicted": row.predicted,
        "family": row.family,
        "score": row.score,
        "margin": row.margin,
    }


def _score_from_payload(value: Mapping[str, Any]) -> ScoreRecord:
    return ScoreRecord(
        record_id=str(value["record_id"]),
        truth=None if value.get("truth") is None else str(value["truth"]),
        expected_disposition=str(value.get("expected_disposition", "classify")),
        predicted=None if value.get("predicted") is None else str(value["predicted"]),
        family=None if value.get("family") is None else str(value["family"]),
        score=float(value["score"]),
        margin=float(value["margin"]),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--model", choices=("minilm", "jina", "both"), default="both")
    parser.add_argument("--batch-size", type=int, choices=DEFAULT_BATCHES, action="append")
    parser.add_argument(
        "--evaluation-split",
        choices=("heldout", "fresh_heldout"),
        default="heldout",
        help="evaluate only after validation-only calibration; fresh_heldout is blind to prior results",
    )
    parser.add_argument(
        "--required-families",
        choices=("all", "document_kind"),
        default="all",
        help="policy scoring scope; calibration still uses validation only",
    )
    parser.add_argument("--longdoc-cost", action="store_true")
    parser.add_argument("--longdoc-only", action="store_true")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--max-representation-chars", type=int, default=DEFAULT_MAX_REPRESENTATION_CHARS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    fixture = load_fixture(args.fixture)
    models = ("minilm", "jina") if args.model == "both" else (args.model,)
    batches = tuple(args.batch_size or DEFAULT_BATCHES)
    required_families = (
        ("document_kind", "topic", "activity")
        if args.required_families == "all"
        else ("document_kind",)
    )
    output: dict[str, Any] = {
        "schema": "neocortex.fast-curation-benchmark/v1",
        "fixture": str(args.fixture),
        "cache_dir": str(args.cache_dir),
        "models": {},
        "cascade": {},
        "fixture_counts": fixture.get("counts"),
        "local_only": True,
        "network": "disabled by caller sandbox",
        "required_families": list(required_families),
        "long_document_cost": {},
    }
    if not args.longdoc_only:
        for model_name in models:
            rows: list[dict[str, Any]] = []
            for batch_size in batches:
                try:
                    rows.append(
                        benchmark_model(
                            fixture,
                            model_name=model_name,
                            cache_dir=args.cache_dir,
                            batch_size=batch_size,
                            threads=args.threads,
                            max_chars=args.max_representation_chars,
                            evaluation_split=args.evaluation_split,
                            required_families=required_families,
                        )
                    )
                except SemanticModelUnavailableError as exc:
                    rows.append({"model": model_name, "batch_size": batch_size, "status": "missing", "reason": exc.reason, "detail": exc.detail})
                except (OSError, RuntimeError, ValueError) as exc:
                    rows.append({"model": model_name, "batch_size": batch_size, "status": "failed", "reason": type(exc).__name__, "detail": str(exc)})
            output["models"][model_name] = rows
    if "minilm" in output["models"] and "jina" in output["models"]:
        output["cascade"] = benchmark_cascade(output["models"]["minilm"][-1], output["models"]["jina"][-1])
        for rows in output["models"].values():
            for row in rows:
                row.pop("_heldout_rows", None)
    if args.longdoc_cost:
        cost_batch = batches[-1] if batches else 32
        for model_name in models:
            try:
                output["long_document_cost"][model_name] = benchmark_long_document_cost(
                    model_name=model_name,
                    cache_dir=args.cache_dir,
                    batch_size=cost_batch,
                )
            except SemanticModelUnavailableError as exc:
                output["long_document_cost"][model_name] = {
                    "status": "missing",
                    "reason": exc.reason,
                    "detail": exc.detail,
                }
            except (OSError, RuntimeError, ValueError) as exc:
                output["long_document_cost"][model_name] = {
                    "status": "failed",
                    "reason": type(exc).__name__,
                    "detail": str(exc),
                }
    else:
        for rows in output["models"].values():
            for row in rows:
                row.pop("_heldout_rows", None)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    has_model = any(row.get("status") == "complete" for rows in output["models"].values() for row in rows)
    has_cost = any(value.get("fast_curation") for value in output["long_document_cost"].values())
    return 0 if has_model or has_cost or args.longdoc_only else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
