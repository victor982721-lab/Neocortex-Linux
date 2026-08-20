"""Public, versioned ownership and durable-boundary contracts.

Knowledge used to keep its physical owner registry private.  This module is the
single public declaration of those state owners and SQLite stores.  It records
declared ownership and transaction control only; it does not infer either from
module names, paths, SQL text, or a final database state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Literal, Mapping, Sequence

from _02_Deduplicacion.inventory_schema import SCHEMA_VERSION as INVENTORY_SCHEMA_VERSION

from .capabilities.formats.archive.state import ARCHIVE_SCHEMA_VERSION
from .capabilities.formats.audio.state import AUDIO_SCHEMA_VERSION
from .code_schema import CODE_SCHEMA_VERSION
from .document_catalog_schema import CATALOG_SCHEMA_VERSION
from .capabilities.formats.docx.schema import DOCX_SCHEMA_VERSION
from .framework_schema import SCHEMA_VERSION as FRAMEWORK_SCHEMA_VERSION
from .office_state import OFFICE_SCHEMA_VERSION
from .pdf_schema import PDF_SCHEMA_VERSION
from .semantic_schema import SEMANTIC_SCHEMA_VERSION
from .text_state import TEXT_SCHEMA_VERSION
from .video_state import VIDEO_SCHEMA_VERSION

STATE_STORE_REGISTRY_SCHEMA: Literal["neocortex.state-store-registry/v1"] = (
    "neocortex.state-store-registry/v1"
)
DURABLE_WORKFLOW_CONTRACT_SCHEMA: Literal["neocortex.durable-workflow-contract/v1"] = (
    "neocortex.durable-workflow-contract/v1"
)
DURABLE_WORKFLOW_BINDING_SCHEMA: Literal["neocortex.durable-workflow-implementation-binding/v1"] = (
    "neocortex.durable-workflow-implementation-binding/v1"
)

# Knowledge intentionally avoids importing image_state until the database is
# present because that module loads the image-processing runtime.  The public
# reader contract therefore pins the same schema version without importing it.
KNOWLEDGE_IMAGE_SCHEMA_VERSION = 5

KnowledgeCaptureMode = Literal["configured", "if_present"]
TransactionAuthority = Literal["caller", "callee"]
TransactionScopeKind = Literal["single_state_store"]


def _required_text(label: str, value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _unique_texts(label: str, values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{label} must be an immutable tuple")
    for value in values:
        _required_text(label, value)
    if len(set(values)) != len(values):
        raise ValueError(f"{label} cannot repeat")


@dataclass(frozen=True, slots=True)
class StateStoreContract:
    """One exact physical SQLite store and its declared state owner."""

    state_store_id: str
    state_owner_id: str
    database_name: str
    knowledge_path_attribute: str
    expected_schema_version: int
    knowledge_read_kind: str
    knowledge_capture_mode: KnowledgeCaptureMode
    storage_engine: Literal["sqlite"] = "sqlite"

    def __post_init__(self) -> None:
        for label, value in (
            ("state store id", self.state_store_id),
            ("state owner id", self.state_owner_id),
            ("database name", self.database_name),
            ("Knowledge path attribute", self.knowledge_path_attribute),
            ("Knowledge read kind", self.knowledge_read_kind),
        ):
            _required_text(label, value)
        if not self.database_name.endswith(".sqlite3"):
            raise ValueError("state store database name must identify a SQLite file")
        if (
            isinstance(self.expected_schema_version, bool)
            or not isinstance(self.expected_schema_version, int)
            or self.expected_schema_version < 1
        ):
            raise ValueError("state store schema version must be positive")
        if self.knowledge_capture_mode not in {"configured", "if_present"}:
            raise ValueError("state store Knowledge capture mode is invalid")
        if self.storage_engine != "sqlite":
            raise ValueError("state store contract currently supports SQLite only")

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StateStoreRegistry:
    schema: Literal["neocortex.state-store-registry/v1"]
    stores: tuple[StateStoreContract, ...]

    def __post_init__(self) -> None:
        if self.schema != STATE_STORE_REGISTRY_SCHEMA:
            raise ValueError("state store registry schema is invalid")
        if not self.stores:
            raise ValueError("state store registry cannot be empty")
        for store in self.stores:
            if not isinstance(store, StateStoreContract):
                raise ValueError("state store registry entries are invalid")
        for label, values in (
            ("state store id", tuple(item.state_store_id for item in self.stores)),
            ("state owner id", tuple(item.state_owner_id for item in self.stores)),
            ("database name", tuple(item.database_name for item in self.stores)),
            (
                "Knowledge path attribute",
                tuple(item.knowledge_path_attribute for item in self.stores),
            ),
        ):
            _unique_texts(label, values)

    def by_owner(self, state_owner_id: str) -> StateStoreContract:
        selected = _required_text("state owner id", state_owner_id)
        match = next((item for item in self.stores if item.state_owner_id == selected), None)
        if match is None:
            raise ValueError(f"unknown state owner: {selected}")
        return match

    def by_store(self, state_store_id: str) -> StateStoreContract:
        selected = _required_text("state store id", state_store_id)
        match = next((item for item in self.stores if item.state_store_id == selected), None)
        if match is None:
            raise ValueError(f"unknown state store: {selected}")
        return match

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "stores": [item.as_payload() for item in self.stores],
        }


def _store(
    owner: str,
    database_name: str,
    expected_schema: int,
    read_kind: str,
    *,
    capture_mode: KnowledgeCaptureMode = "configured",
) -> StateStoreContract:
    return StateStoreContract(
        state_store_id=f"sqlite:{database_name}",
        state_owner_id=owner,
        database_name=database_name,
        knowledge_path_attribute=owner,
        expected_schema_version=expected_schema,
        knowledge_read_kind=read_kind,
        knowledge_capture_mode=capture_mode,
    )


# Order is contractual: Knowledge uses it for owner vectors and bounded search.
STATE_STORE_REGISTRY = StateStoreRegistry(
    schema=STATE_STORE_REGISTRY_SCHEMA,
    stores=(
        _store("inventory", "dedup.sqlite3", INVENTORY_SCHEMA_VERSION, "inventory"),
        _store("framework", "framework.sqlite3", FRAMEWORK_SCHEMA_VERSION, "framework"),
        _store("catalog", "document_catalog.sqlite3", CATALOG_SCHEMA_VERSION, "catalog"),
        _store("pdf", "pdf.sqlite3", PDF_SCHEMA_VERSION, "documents"),
        _store("docx", "docx.sqlite3", DOCX_SCHEMA_VERSION, "documents"),
        _store("office", "office.sqlite3", OFFICE_SCHEMA_VERSION, "documents"),
        _store("audio", "audio.sqlite3", AUDIO_SCHEMA_VERSION, "documents"),
        _store("video", "video.sqlite3", VIDEO_SCHEMA_VERSION, "documents"),
        _store("image", "image.sqlite3", KNOWLEDGE_IMAGE_SCHEMA_VERSION, "images"),
        _store("semantic", "semantic.sqlite3", SEMANTIC_SCHEMA_VERSION, "semantic"),
        _store("code", "code.sqlite3", CODE_SCHEMA_VERSION, "code"),
        _store(
            "archive",
            "archive.sqlite3",
            ARCHIVE_SCHEMA_VERSION,
            "documents",
            capture_mode="if_present",
        ),
        _store(
            "text",
            "text.sqlite3",
            TEXT_SCHEMA_VERSION,
            "documents",
            capture_mode="if_present",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class DurableTransactionBoundaryContract:
    """Declared control and minimum relations for one durable boundary."""

    boundary_id: str
    state_store_id: str
    state_owner_id: str
    begin_authority: TransactionAuthority
    commit_authority: TransactionAuthority
    transaction_scope: TransactionScopeKind
    required_read_tables: tuple[str, ...]
    required_write_tables: tuple[str, ...]
    conditional_write_tables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("transaction boundary id", self.boundary_id),
            ("transaction state store id", self.state_store_id),
            ("transaction state owner id", self.state_owner_id),
        ):
            _required_text(label, value)
        if self.begin_authority not in {"caller", "callee"}:
            raise ValueError("transaction begin authority is invalid")
        if self.commit_authority not in {"caller", "callee"}:
            raise ValueError("transaction commit authority is invalid")
        if self.transaction_scope != "single_state_store":
            raise ValueError("transaction scope must remain owner-local")
        for label, values in (
            ("required read table", self.required_read_tables),
            ("required write table", self.required_write_tables),
            ("conditional write table", self.conditional_write_tables),
        ):
            _unique_texts(label, values)
        if not self.required_write_tables:
            raise ValueError("durable transaction boundary requires declared writes")
        if set(self.required_write_tables) & set(self.conditional_write_tables):
            raise ValueError("required and conditional transaction writes cannot overlap")
        store = STATE_STORE_REGISTRY.by_store(self.state_store_id)
        if store.state_owner_id != self.state_owner_id:
            raise ValueError("transaction boundary store and state owner disagree")

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DurableWorkflowContract:
    schema: Literal["neocortex.durable-workflow-contract/v1"]
    workflow_id: str
    version: str
    boundaries: tuple[DurableTransactionBoundaryContract, ...]

    def __post_init__(self) -> None:
        if self.schema != DURABLE_WORKFLOW_CONTRACT_SCHEMA:
            raise ValueError("durable workflow schema is invalid")
        _required_text("durable workflow id", self.workflow_id)
        _required_text("durable workflow version", self.version, maximum=64)
        if not self.boundaries or any(
            not isinstance(item, DurableTransactionBoundaryContract) for item in self.boundaries
        ):
            raise ValueError("durable workflow requires typed boundaries")
        _unique_texts(
            "durable transaction boundary id",
            tuple(item.boundary_id for item in self.boundaries),
        )

    def boundary(self, boundary_id: str) -> DurableTransactionBoundaryContract:
        selected = _required_text("transaction boundary id", boundary_id)
        match = next((item for item in self.boundaries if item.boundary_id == selected), None)
        if match is None:
            raise ValueError(f"unknown transaction boundary: {selected}")
        return match

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workflow_id": self.workflow_id,
            "version": self.version,
            "boundaries": [item.as_payload() for item in self.boundaries],
        }


@dataclass(frozen=True, slots=True)
class DurableBoundaryImplementationBinding:
    """Exact Code symbols declared to implement one durable boundary.

    These are source contracts.  The SQL analyzer still has to resolve the
    symbols and their statements from a particular Code publication before the
    binding counts as evidence.
    """

    boundary_id: str
    qualified_symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        _required_text("transaction boundary id", self.boundary_id)
        _unique_texts("transaction boundary implementation symbol", self.qualified_symbols)
        if not self.qualified_symbols:
            raise ValueError("transaction boundary implementation binding cannot be empty")
        TEXT_DERIVATION_WORKFLOW.boundary(self.boundary_id)

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DurableWorkflowImplementationBinding:
    schema: Literal["neocortex.durable-workflow-implementation-binding/v1"]
    workflow_id: str
    workflow_version: str
    boundaries: tuple[DurableBoundaryImplementationBinding, ...]

    def __post_init__(self) -> None:
        if self.schema != DURABLE_WORKFLOW_BINDING_SCHEMA:
            raise ValueError("durable workflow implementation binding schema is invalid")
        if (
            self.workflow_id != TEXT_DERIVATION_WORKFLOW.workflow_id
            or self.workflow_version != TEXT_DERIVATION_WORKFLOW.version
        ):
            raise ValueError("durable workflow implementation binding target is invalid")
        if not self.boundaries or any(
            not isinstance(item, DurableBoundaryImplementationBinding) for item in self.boundaries
        ):
            raise ValueError("durable workflow implementation binding requires typed boundaries")
        _unique_texts(
            "bound implementation boundary id",
            tuple(item.boundary_id for item in self.boundaries),
        )

    def boundary(self, boundary_id: str) -> DurableBoundaryImplementationBinding:
        selected = _required_text("transaction boundary id", boundary_id)
        match = next((item for item in self.boundaries if item.boundary_id == selected), None)
        if match is None:
            raise ValueError(f"unbound transaction boundary: {selected}")
        return match

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "boundaries": [item.as_payload() for item in self.boundaries],
        }


TEXT_DERIVATION_WORKFLOW = DurableWorkflowContract(
    schema=DURABLE_WORKFLOW_CONTRACT_SCHEMA,
    workflow_id="text.derivation-publication",
    version="v1",
    boundaries=(
        DurableTransactionBoundaryContract(
            boundary_id="text.derivation-attempt-begin",
            state_store_id="sqlite:text.sqlite3",
            state_owner_id="text",
            begin_authority="callee",
            commit_authority="callee",
            transaction_scope="single_state_store",
            required_read_tables=("text_input_revisions",),
            required_write_tables=(
                "text_input_revisions",
                "text_derivation_attempts",
                "text_derivation_input_bindings",
            ),
        ),
        DurableTransactionBoundaryContract(
            boundary_id="text.terminal-publication",
            state_store_id="sqlite:text.sqlite3",
            state_owner_id="text",
            begin_authority="caller",
            commit_authority="caller",
            transaction_scope="single_state_store",
            required_read_tables=(
                "text_derivation_attempts",
                "text_derivation_input_bindings",
                "text_input_revisions",
            ),
            required_write_tables=(
                "text_work_receipts",
                "text_derivation_attempts",
                "text_derivation_outbox",
            ),
            conditional_write_tables=(
                "documents",
                "document_fts",
                "text_materializations",
                "text_derivation_output_bindings",
                "text_materialization_heads",
            ),
        ),
    ),
)


TEXT_DERIVATION_IMPLEMENTATION_BINDING = DurableWorkflowImplementationBinding(
    schema=DURABLE_WORKFLOW_BINDING_SCHEMA,
    workflow_id=TEXT_DERIVATION_WORKFLOW.workflow_id,
    workflow_version=TEXT_DERIVATION_WORKFLOW.version,
    boundaries=(
        DurableBoundaryImplementationBinding(
            boundary_id="text.derivation-attempt-begin",
            qualified_symbols=(
                "text_derivation_repository.begin_text_derivation_attempt_from_connection",
            ),
        ),
        DurableBoundaryImplementationBinding(
            boundary_id="text.terminal-publication",
            qualified_symbols=(
                "text_derivation_repository._persist_terminal_receipt",
                "text_derivation_repository.cancel_text_derivation_attempt",
                "text_derivation_repository.fail_text_derivation_attempt",
                "text_derivation_repository.succeed_text_derivation_attempt",
            ),
        ),
    ),
)


def parse_state_store_registry_payload(payload: Mapping[str, object]) -> StateStoreRegistry:
    if not isinstance(payload, Mapping) or payload.get("schema") != STATE_STORE_REGISTRY_SCHEMA:
        raise ValueError("state store registry payload schema is invalid")
    if set(payload) != {"schema", "stores"}:
        raise ValueError("state store registry payload fields are invalid")
    raw_stores = payload.get("stores")
    if not isinstance(raw_stores, list):
        raise ValueError("state store registry stores are invalid")
    expected_fields = {field.name for field in fields(StateStoreContract)}
    stores: list[StateStoreContract] = []
    for raw in raw_stores:
        if not isinstance(raw, Mapping) or set(raw) != expected_fields:
            raise ValueError("state store registry entry fields are invalid")
        stores.append(StateStoreContract(**dict(raw)))
    return StateStoreRegistry(STATE_STORE_REGISTRY_SCHEMA, tuple(stores))


def parse_durable_workflow_contract_payload(
    payload: Mapping[str, object],
) -> DurableWorkflowContract:
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != DURABLE_WORKFLOW_CONTRACT_SCHEMA
    ):
        raise ValueError("durable workflow payload schema is invalid")
    if set(payload) != {"schema", "workflow_id", "version", "boundaries"}:
        raise ValueError("durable workflow payload fields are invalid")
    raw_boundaries = payload.get("boundaries")
    if not isinstance(raw_boundaries, list):
        raise ValueError("durable workflow boundaries are invalid")
    boundary_fields = {field.name for field in fields(DurableTransactionBoundaryContract)}
    boundaries: list[DurableTransactionBoundaryContract] = []
    for raw in raw_boundaries:
        if not isinstance(raw, Mapping) or set(raw) != boundary_fields:
            raise ValueError("durable workflow boundary fields are invalid")
        values = dict(raw)
        for name in (
            "required_read_tables",
            "required_write_tables",
            "conditional_write_tables",
        ):
            raw_tables = values[name]
            if not isinstance(raw_tables, Sequence) or isinstance(
                raw_tables, (str, bytes, bytearray)
            ):
                raise ValueError("durable workflow boundary tables are invalid")
            values[name] = tuple(raw_tables)
        boundaries.append(DurableTransactionBoundaryContract(**values))
    return DurableWorkflowContract(
        schema=DURABLE_WORKFLOW_CONTRACT_SCHEMA,
        workflow_id=_required_text("durable workflow id", payload["workflow_id"]),
        version=_required_text("durable workflow version", payload["version"], maximum=64),
        boundaries=tuple(boundaries),
    )


def parse_durable_workflow_implementation_binding_payload(
    payload: Mapping[str, object],
) -> DurableWorkflowImplementationBinding:
    if not isinstance(payload, Mapping) or payload.get("schema") != DURABLE_WORKFLOW_BINDING_SCHEMA:
        raise ValueError("durable workflow implementation binding payload schema is invalid")
    if set(payload) != {"schema", "workflow_id", "workflow_version", "boundaries"}:
        raise ValueError("durable workflow implementation binding fields are invalid")
    raw_boundaries = payload.get("boundaries")
    if not isinstance(raw_boundaries, list):
        raise ValueError("durable workflow implementation boundaries are invalid")
    boundaries: list[DurableBoundaryImplementationBinding] = []
    expected_fields = {field.name for field in fields(DurableBoundaryImplementationBinding)}
    for raw in raw_boundaries:
        if not isinstance(raw, Mapping) or set(raw) != expected_fields:
            raise ValueError("durable workflow implementation boundary fields are invalid")
        raw_symbols = raw.get("qualified_symbols")
        if not isinstance(raw_symbols, Sequence) or isinstance(
            raw_symbols, (str, bytes, bytearray)
        ):
            raise ValueError("durable workflow implementation symbols are invalid")
        boundaries.append(
            DurableBoundaryImplementationBinding(
                boundary_id=_required_text("transaction boundary id", raw["boundary_id"]),
                qualified_symbols=tuple(raw_symbols),
            )
        )
    return DurableWorkflowImplementationBinding(
        schema=DURABLE_WORKFLOW_BINDING_SCHEMA,
        workflow_id=_required_text("durable workflow id", payload["workflow_id"]),
        workflow_version=_required_text(
            "durable workflow version", payload["workflow_version"], maximum=64
        ),
        boundaries=tuple(boundaries),
    )


__all__ = [
    "DURABLE_WORKFLOW_BINDING_SCHEMA",
    "DURABLE_WORKFLOW_CONTRACT_SCHEMA",
    "KNOWLEDGE_IMAGE_SCHEMA_VERSION",
    "STATE_STORE_REGISTRY",
    "STATE_STORE_REGISTRY_SCHEMA",
    "TEXT_DERIVATION_IMPLEMENTATION_BINDING",
    "TEXT_DERIVATION_WORKFLOW",
    "DurableBoundaryImplementationBinding",
    "DurableTransactionBoundaryContract",
    "DurableWorkflowContract",
    "DurableWorkflowImplementationBinding",
    "StateStoreContract",
    "StateStoreRegistry",
    "parse_durable_workflow_contract_payload",
    "parse_durable_workflow_implementation_binding_payload",
    "parse_state_store_registry_payload",
]
