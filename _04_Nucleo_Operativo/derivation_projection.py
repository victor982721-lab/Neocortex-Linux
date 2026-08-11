"""Rebuildable in-memory projection over owner-local derivation outboxes.

The projection is deliberately not an authority and owns no SQLite database.
Owners commit their outputs, receipts and outbox rows atomically; this module
only folds those immutable facts into an explainable graph.  Discarding the
object and replaying the same events must produce the same graph.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from .derivation_contracts import MAX_WORK_RECEIPT_JSON_BYTES, WorkReceipt
from .semantic_models import canonical_json, fingerprint_text


DERIVATION_PROJECTION_SCHEMA = "neocortex.derivation-projection/v1"
MAX_DERIVATION_EVENTS = 100_000
MAX_DERIVATION_EVENT_BYTES = 1_048_576
MAX_DERIVATION_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DERIVATION_EVENT_IDENTIFIER_CHARS = 1_024
MAX_DERIVATION_GRAPH_NODES = 250_000
MAX_DERIVATION_GRAPH_EDGES = 500_000
MAX_LINEAGE_TRAVERSAL_NODES = 100_000


@dataclass(frozen=True, slots=True)
class DerivationProjectionEvent:
    """One immutable event read from an owner-local transactional outbox."""

    owner: str
    cursor: int
    event_id: str
    receipt: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.owner.strip() or not self.event_id.strip():
            raise ValueError("projection event owner and event_id cannot be blank")
        if (
            len(self.owner) > MAX_DERIVATION_EVENT_IDENTIFIER_CHARS
            or len(self.event_id) > MAX_DERIVATION_EVENT_IDENTIFIER_CHARS
        ):
            raise ValueError("projection event owner or event_id exceeds its bound")
        if isinstance(self.cursor, bool) or not isinstance(self.cursor, int) or self.cursor < 1:
            raise ValueError("projection event cursor must be a positive integer")
        if not isinstance(self.receipt, Mapping):
            raise TypeError("projection event receipt must be a mapping")


@dataclass(frozen=True, slots=True)
class DerivationGraphNode:
    node_id: str
    kind: str
    owner: str
    attributes: tuple[tuple[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "owner": self.owner,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class DerivationGraphEdge:
    source_id: str
    target_id: str
    relation: str
    receipt_id: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DerivationImpact:
    changed_stage_id: str
    expected_processing_signature: str
    stale_node_ids: tuple[str, ...]
    reusable_node_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "changed_stage_id": self.changed_stage_id,
            "expected_processing_signature": self.expected_processing_signature,
            "stale_node_ids": list(self.stale_node_ids),
            "reusable_node_ids": list(self.reusable_node_ids),
        }


@dataclass(frozen=True, slots=True)
class DerivationExplanation:
    target_id: str
    nodes: tuple[DerivationGraphNode, ...]
    edges: tuple[DerivationGraphEdge, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass(frozen=True, slots=True)
class DerivationProjection:
    """Deterministic snapshot reconstructed exclusively from outbox facts."""

    events_applied: int
    duplicate_events_ignored: int
    nodes: tuple[DerivationGraphNode, ...]
    edges: tuple[DerivationGraphEdge, ...]
    event_fingerprints: tuple[tuple[str, str, str], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": DERIVATION_PROJECTION_SCHEMA,
            "events_applied": self.events_applied,
            "duplicate_events_ignored": self.duplicate_events_ignored,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    def explain(self, target_id: str) -> DerivationExplanation:
        """Return the bounded causal ancestry of one known node."""

        nodes = {node.node_id: node for node in self.nodes}
        if target_id not in nodes:
            raise KeyError(f"unknown derivation node {target_id!r}")
        incoming: dict[str, list[DerivationGraphEdge]] = {}
        for edge in self.edges:
            incoming.setdefault(edge.target_id, []).append(edge)
        selected_nodes = {target_id}
        selected_edges: set[DerivationGraphEdge] = set()
        pending = deque((target_id,))
        while pending:
            current = pending.popleft()
            for edge in incoming.get(current, ()):
                selected_edges.add(edge)
                if edge.source_id not in selected_nodes:
                    if len(selected_nodes) >= MAX_LINEAGE_TRAVERSAL_NODES:
                        raise RuntimeError("lineage explanation exceeded its node bound")
                    selected_nodes.add(edge.source_id)
                    pending.append(edge.source_id)
        return DerivationExplanation(
            target_id,
            tuple(nodes[node_id] for node_id in sorted(selected_nodes)),
            tuple(
                sorted(
                    selected_edges,
                    key=lambda edge: (
                        edge.source_id,
                        edge.target_id,
                        edge.relation,
                        edge.receipt_id,
                    ),
                )
            ),
        )

    def impact(
        self,
        changed_stage_id: str,
        expected_processing_signature: str,
    ) -> DerivationImpact:
        """Propagate staleness from a changed stage through explicit edges."""

        if not changed_stage_id.strip() or not expected_processing_signature.strip():
            raise ValueError("changed stage and expected signature cannot be blank")
        nodes = {node.node_id: node for node in self.nodes}
        outgoing: dict[str, list[str]] = {}
        for edge in self.edges:
            outgoing.setdefault(edge.source_id, []).append(edge.target_id)
        changed_receipts = {
            node.node_id
            for node in self.nodes
            if node.kind == "work_receipt"
            and dict(node.attributes).get("stage_id") == changed_stage_id
            and dict(node.attributes).get("processing_signature") != expected_processing_signature
        }
        stale = set(changed_receipts)
        pending = deque(sorted(stale))
        while pending:
            current = pending.popleft()
            for dependent in outgoing.get(current, ()):
                if dependent in stale:
                    continue
                if len(stale) >= MAX_LINEAGE_TRAVERSAL_NODES:
                    raise RuntimeError("lineage impact exceeded its node bound")
                stale.add(dependent)
                pending.append(dependent)
        incoming: dict[str, list[str]] = {}
        for edge in self.edges:
            incoming.setdefault(edge.target_id, []).append(edge.source_id)
        ancestors: set[str] = set()
        pending = deque(sorted(changed_receipts))
        while pending:
            current = pending.popleft()
            for dependency in incoming.get(current, ()):
                if dependency in ancestors or dependency in stale:
                    continue
                if len(ancestors) >= MAX_LINEAGE_TRAVERSAL_NODES:
                    raise RuntimeError("lineage reuse analysis exceeded its node bound")
                ancestors.add(dependency)
                pending.append(dependency)
        reusable = tuple(
            sorted(
                node_id
                for node_id in ancestors
                if nodes[node_id].kind in {"revision", "materialization"}
            )
        )
        return DerivationImpact(
            changed_stage_id,
            expected_processing_signature,
            tuple(sorted(stale)),
            reusable,
        )


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"receipt {key} must be a non-blank string")
    return value


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"receipt {label} must be an object")
    return value


def _sequence(value: object, *, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f"receipt {label} must be a list")
    if len(value) > 4_096:
        raise ValueError(f"receipt {label} exceeds its binding bound")
    return tuple(_mapping(item, label=f"{label} item") for item in value)


def _node(
    node_id: str,
    kind: str,
    owner: str,
    attributes: Mapping[str, object],
) -> DerivationGraphNode:
    if not node_id.strip() or not kind.strip() or not owner.strip():
        raise ValueError("derivation node identity cannot be blank")
    return DerivationGraphNode(
        node_id,
        kind,
        owner,
        tuple(sorted(attributes.items())),
    )


def _merge_node(
    nodes: dict[str, DerivationGraphNode],
    candidate: DerivationGraphNode,
) -> None:
    existing = nodes.get(candidate.node_id)
    if existing is not None and existing != candidate:
        raise ValueError(f"derivation node {candidate.node_id!r} has conflicting facts")
    nodes[candidate.node_id] = candidate


def _event_fingerprint(event: DerivationProjectionEvent) -> tuple[str, int]:
    receipt_json = canonical_json(dict(event.receipt)).encode("utf-8")
    if len(receipt_json) > MAX_WORK_RECEIPT_JSON_BYTES:
        raise ValueError("derivation receipt exceeds its byte bound")
    encoded = canonical_json(
        {
            "owner": event.owner,
            "cursor": event.cursor,
            "event_id": event.event_id,
            "receipt": dict(event.receipt),
        }
    ).encode("utf-8")
    if len(encoded) > MAX_DERIVATION_EVENT_BYTES:
        raise ValueError("derivation event exceeds its byte bound")
    return fingerprint_text(encoded.decode("utf-8")).xxh3_128, len(encoded)


def _contract_fingerprint(payload: Mapping[str, object]) -> str:
    return fingerprint_text(canonical_json(dict(payload))).xxh3_128


def _apply_receipt(
    owner: str,
    receipt: Mapping[str, object],
    nodes: dict[str, DerivationGraphNode],
    edges: set[DerivationGraphEdge],
    causations: set[tuple[str, str]],
) -> None:
    receipt_contract = WorkReceipt.from_dict(receipt)
    receipt = receipt_contract.to_dict()
    receipt_id = _required_text(receipt, "receipt_id")
    receipt_owner = _required_text(receipt, "owner")
    if receipt_owner != owner:
        raise ValueError("outbox owner does not match WorkReceipt owner")
    stage = _mapping(receipt.get("stage"), label="stage")
    stage_id = _required_text(stage, "stage_id")
    stage_signature = _required_text(stage, "processing_signature")
    stage_node_id = f"stage:{stage_id}:{stage_signature}"
    execution_mode = _required_text(receipt, "execution_mode")
    if execution_mode not in {
        "attempted",
        "cache_hit",
        "executed",
        "replay",
        "unknown",
    }:
        raise ValueError("receipt execution_mode is not a supported WorkReceipt value")
    _merge_node(
        nodes,
        _node(
            stage_node_id,
            "stage",
            owner,
            {
                "stage_id": stage_id,
                "stage_version": _required_text(stage, "stage_version"),
                "processing_signature": stage_signature,
                "contract_fingerprint": _contract_fingerprint(stage),
            },
        ),
    )
    _merge_node(
        nodes,
        _node(
            receipt_id,
            "work_receipt",
            owner,
            {
                "stage_id": stage_id,
                "processing_signature": stage_signature,
                "outcome": _required_text(receipt, "outcome"),
                "execution_mode": execution_mode,
                "reproducibility": _required_text(receipt, "reproducibility"),
                "run_id": _required_text(receipt, "run_id"),
                "correlation_id": _required_text(receipt, "correlation_id"),
                "contract_fingerprint": receipt_contract.contract_fingerprint,
            },
        ),
    )
    edges.add(DerivationGraphEdge(stage_node_id, receipt_id, "stage_contract_for", receipt_id))
    causation_id = receipt.get("causation_id")
    if causation_id is not None:
        if not isinstance(causation_id, str) or not causation_id.strip():
            raise ValueError("receipt causation_id must be a non-blank string when present")
        if causation_id == receipt_id:
            raise ValueError("receipt cannot cause itself")
        causations.add((causation_id, receipt_id))

    for binding in _sequence(receipt.get("inputs"), label="inputs"):
        revision = _mapping(binding.get("revision"), label="input revision")
        revision_id = _required_text(revision, "revision_id")
        revision_owner = _required_text(revision, "producer")
        binding_name = _required_text(binding, "name")
        binding_id = f"binding:{receipt_id}:input:{binding_name}"
        _merge_node(
            nodes,
            _node(
                revision_id,
                "revision",
                revision_owner,
                {
                    "resource_id": _required_text(revision, "resource_id"),
                    "processing_signature": _required_text(revision, "processing_signature"),
                    "contract_fingerprint": _contract_fingerprint(revision),
                },
            ),
        )
        _merge_node(
            nodes,
            _node(
                binding_id,
                "input_binding",
                owner,
                {
                    "name": binding_name,
                    "fingerprint": _required_text(binding, "fingerprint"),
                    "fingerprint_algorithm": _required_text(binding, "fingerprint_algorithm"),
                },
            ),
        )
        edges.add(DerivationGraphEdge(revision_id, binding_id, "bound_as", receipt_id))
        edges.add(DerivationGraphEdge(binding_id, receipt_id, "input_for", receipt_id))
        materialization = binding.get("materialization")
        if materialization is not None:
            materialized = _mapping(materialization, label="input materialization")
            materialization_id = _required_text(materialized, "materialization_id")
            _merge_node(
                nodes,
                _node(
                    materialization_id,
                    "materialization",
                    _required_text(materialized, "owner"),
                    {
                        "materialization_kind": _required_text(
                            materialized, "materialization_kind"
                        ),
                        "owner_schema_version": materialized.get("owner_schema_version"),
                        "fingerprint": _required_text(binding, "fingerprint"),
                        "fingerprint_algorithm": _required_text(binding, "fingerprint_algorithm"),
                        "contract_fingerprint": _contract_fingerprint(materialized),
                    },
                ),
            )
            edges.add(
                DerivationGraphEdge(
                    materialization_id,
                    binding_id,
                    "materialization_bound_as",
                    receipt_id,
                )
            )

    for binding in _sequence(receipt.get("outputs"), label="outputs"):
        materialization = _mapping(binding.get("materialization"), label="output materialization")
        materialization_id = _required_text(materialization, "materialization_id")
        _merge_node(
            nodes,
            _node(
                materialization_id,
                "materialization",
                _required_text(materialization, "owner"),
                {
                    "materialization_kind": _required_text(materialization, "materialization_kind"),
                    "owner_schema_version": materialization.get("owner_schema_version"),
                    "fingerprint": _required_text(binding, "fingerprint"),
                    "fingerprint_algorithm": _required_text(binding, "fingerprint_algorithm"),
                    "contract_fingerprint": _contract_fingerprint(materialization),
                },
            ),
        )
        relation = "produced" if execution_mode == "executed" else "reused"
        edges.add(DerivationGraphEdge(receipt_id, materialization_id, relation, receipt_id))


def rebuild_derivation_projection(
    events: Iterable[DerivationProjectionEvent],
) -> DerivationProjection:
    """Fold bounded owner events idempotently into a deterministic graph."""

    nodes: dict[str, DerivationGraphNode] = {}
    edges: set[DerivationGraphEdge] = set()
    event_fingerprints: dict[tuple[str, str], str] = {}
    causations: set[tuple[str, str]] = set()
    applied = duplicates = 0
    total_event_bytes = 0
    owner_cursors: dict[str, int] = {}
    for event in events:
        if not isinstance(event, DerivationProjectionEvent):
            raise TypeError("events must contain DerivationProjectionEvent values")
        if applied + duplicates >= MAX_DERIVATION_EVENTS:
            raise ValueError("derivation projection exceeds its event bound")
        fingerprint, event_bytes = _event_fingerprint(event)
        total_event_bytes += event_bytes
        if total_event_bytes > MAX_DERIVATION_TOTAL_BYTES:
            raise ValueError("derivation projection exceeds its cumulative byte bound")
        event_key = (event.owner, event.event_id)
        existing = event_fingerprints.get(event_key)
        if existing is not None:
            if existing != fingerprint:
                raise ValueError(f"outbox event {event.event_id!r} changed after publication")
            duplicates += 1
            continue
        prior_cursor = owner_cursors.get(event.owner, 0)
        if event.cursor <= prior_cursor:
            raise ValueError("owner outbox cursors must be strictly increasing")
        owner_cursors[event.owner] = event.cursor
        _apply_receipt(event.owner, event.receipt, nodes, edges, causations)
        if len(nodes) > MAX_DERIVATION_GRAPH_NODES:
            raise ValueError("derivation projection exceeds its node bound")
        if len(edges) > MAX_DERIVATION_GRAPH_EDGES:
            raise ValueError("derivation projection exceeds its edge bound")
        event_fingerprints[event_key] = fingerprint
        applied += 1
    for cause_id, receipt_id in sorted(causations):
        cause = nodes.get(cause_id)
        if cause is None:
            _merge_node(
                nodes,
                _node(
                    cause_id,
                    "work_receipt_reference",
                    "external",
                    {"projection_status": "referenced_outside_event_window"},
                ),
            )
        elif cause.kind != "work_receipt":
            raise ValueError("receipt causation_id resolves to a non-receipt node")
        edges.add(DerivationGraphEdge(cause_id, receipt_id, "caused", receipt_id))
    if len(nodes) > MAX_DERIVATION_GRAPH_NODES:
        raise ValueError("derivation projection exceeds its node bound")
    if len(edges) > MAX_DERIVATION_GRAPH_EDGES:
        raise ValueError("derivation projection exceeds its edge bound")
    return DerivationProjection(
        events_applied=applied,
        duplicate_events_ignored=duplicates,
        nodes=tuple(nodes[node_id] for node_id in sorted(nodes)),
        edges=tuple(
            sorted(
                edges,
                key=lambda edge: (
                    edge.source_id,
                    edge.target_id,
                    edge.relation,
                    edge.receipt_id,
                ),
            )
        ),
        event_fingerprints=tuple(
            (owner, event_id, fingerprint)
            for (owner, event_id), fingerprint in sorted(event_fingerprints.items())
        ),
    )


class _TextOutboxEvent(Protocol):
    @property
    def sequence(self) -> int: ...

    @property
    def event_id(self) -> str: ...

    @property
    def payload_json(self) -> str: ...


class _SemanticOutboxEvent(Protocol):
    @property
    def event_id(self) -> int: ...

    @property
    def receipt(self) -> Mapping[str, object]: ...


def projection_event_from_text_outbox(
    event: _TextOutboxEvent,
) -> DerivationProjectionEvent:
    """Adapt the structural Text outbox record without importing its owner module."""

    try:
        payload = json.loads(str(event.payload_json))
        cursor = int(event.sequence)
        event_id = str(event.event_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Text derivation outbox event is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("Text derivation outbox payload is not an object")
    return DerivationProjectionEvent("text", cursor, event_id, payload)


def projection_event_from_semantic_outbox(
    event: _SemanticOutboxEvent,
) -> DerivationProjectionEvent:
    """Adapt Semantic's joined outbox receipt without importing its owner module."""

    try:
        cursor = int(event.event_id)
        receipt = event.receipt
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Semantic derivation outbox event is malformed") from exc
    if not isinstance(receipt, Mapping):
        raise ValueError("Semantic derivation outbox receipt is not an object")
    return DerivationProjectionEvent(
        "semantic",
        cursor,
        f"semantic:{cursor}",
        receipt,
    )


__all__ = (
    "DERIVATION_PROJECTION_SCHEMA",
    "DerivationExplanation",
    "DerivationGraphEdge",
    "DerivationGraphNode",
    "DerivationImpact",
    "DerivationProjection",
    "DerivationProjectionEvent",
    "projection_event_from_semantic_outbox",
    "projection_event_from_text_outbox",
    "rebuild_derivation_projection",
)
