"""Versioned architectural contracts for NeoCortex production packages.

The contract layer consumes a normalized import graph.  It does not know how
that graph was produced, which keeps the policy reusable by both Grimp and the
Ruff differential oracle.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

ARCHITECTURE_CONTRACT_SCHEMA = "neocortex.code-architecture-contracts/v1"
ARCHITECTURE_BASELINE_ID = "neocortex-production-imports-2026-08-23/v5"

PRODUCTION_ROOT_PACKAGES = (
    "neocortex",
    "_04_Nucleo_Operativo",
)
EXCLUDED_ARCHITECTURE_NAMESPACES = ("tests", "tools", "benchmarks")
EXCLUDED_STANDALONE_MODULES: tuple[str, ...] = ()

_CORE = "_04_Nucleo_Operativo"
_UI = "neocortex.interface"
_ENUMERATION = "neocortex.enumeration"
_DEDUPLICATION = "neocortex.deduplication"
_PROGRESS = "neocortex.progress"
_CANONICAL_FAMILY_TREES = (_DEDUPLICATION, _ENUMERATION, _PROGRESS, _UI)

_DEDUP_CORE_ALLOWLIST = (
    ("neocortex.deduplication.__main__", "_04_Nucleo_Operativo.app_paths"),
    ("neocortex.deduplication.__main__", "_04_Nucleo_Operativo.cli_app"),
)
_ENUMERATION_PRODUCT_ALLOWLIST = (
    (
        "neocortex.enumeration.path_index.schema",
        "neocortex.sqlite_schema_contract",
    ),
    (
        "neocortex.enumeration.path_index.schema",
        "neocortex.sqlite_schema_lifecycle",
    ),
)
_DEDUP_PRODUCT_ALLOWLIST = (
    ("neocortex.deduplication.__main__", "neocortex.platform_policy"),
    ("neocortex.deduplication.fingerprinting", "neocortex.platform_policy"),
    ("neocortex.deduplication.inventory.index", "neocortex.progress"),
    ("neocortex.deduplication.inventory.policy", "neocortex.platform_policy"),
    (
        "neocortex.deduplication.inventory.repository_plans",
        "neocortex.platform_policy",
    ),
    ("neocortex.deduplication.inventory.repository_scans", "neocortex.progress"),
    ("neocortex.deduplication.inventory.scanner", "neocortex.progress"),
    ("neocortex.deduplication.inventory.traversal", "neocortex.platform_policy"),
    ("neocortex.deduplication.inventory.traversal", "neocortex.progress"),
    (
        "neocortex.deduplication.persistence.connections",
        "neocortex.sqlite_schema_lifecycle",
    ),
    (
        "neocortex.deduplication.persistence.contracts",
        "neocortex.sqlite_schema_contract",
    ),
    ("neocortex.deduplication.persistence.ddl", "neocortex.platform_policy"),
    (
        "neocortex.deduplication.persistence.lifecycle",
        "neocortex.sqlite_schema_lifecycle",
    ),
    (
        "neocortex.deduplication.persistence.migrations.v6_to_v7",
        "neocortex.sqlite_schema_contract",
    ),
    (
        "neocortex.deduplication.persistence.migrations.v7_to_v8",
        "neocortex.sqlite_schema_contract",
    ),
    (
        "neocortex.deduplication.persistence.migrations.v8_to_v9",
        "neocortex.sqlite_schema_contract",
    ),
    (
        "neocortex.deduplication.persistence.migrations.v9_to_v10",
        "neocortex.sqlite_schema_contract",
    ),
    (
        "neocortex.deduplication.persistence.validation",
        "neocortex.sqlite_schema_contract",
    ),
    ("neocortex.deduplication.planning.pipeline", "neocortex.progress"),
    ("neocortex.deduplication.planning.planner", "neocortex.progress"),
)
_INTERFACE_CORE_ALLOWLIST = (
    ("neocortex.interface.application.app", "_04_Nucleo_Operativo.app_paths"),
    (
        "neocortex.interface.presentation.windows.main",
        "_04_Nucleo_Operativo.app_paths",
    ),
    ("neocortex.interface.protocol.worker", "_04_Nucleo_Operativo.cli_config"),
    ("neocortex.interface.protocol.worker", "_04_Nucleo_Operativo.cli_parser"),
    ("neocortex.interface.protocol.worker", "_04_Nucleo_Operativo.cli_reporting"),
    ("neocortex.interface.protocol.worker", "_04_Nucleo_Operativo.cli_validation"),
    ("neocortex.interface.protocol.worker", "_04_Nucleo_Operativo.orchestrator"),
    ("neocortex.interface.read.status", "_04_Nucleo_Operativo.framework_connection"),
    ("neocortex.interface.read.status", "_04_Nucleo_Operativo.run_status"),
)
_INTERFACE_PRODUCT_ALLOWLIST = (
    ("neocortex.interface.application.app", "neocortex"),
    ("neocortex.interface.application.app", "neocortex.platform_policy"),
    (
        "neocortex.interface.presentation.windows.main",
        "neocortex.platform_policy",
    ),
    ("neocortex.interface.protocol.messages", "neocortex.progress"),
    ("neocortex.interface.protocol.worker", "neocortex.progress"),
)
_NEOCORTEX_CORE_UI_ALLOWLIST = (
    # Canonical code-analysis facades need the protected owner default without
    # importing the operational CLI graph merely to render help or translate
    # an omitted --state-directory.
    ("neocortex.cli", "_04_Nucleo_Operativo.app_paths"),
    ("neocortex.cli", "_04_Nucleo_Operativo.cli_app"),
    ("neocortex.cli", "neocortex.interface.application.app"),
    ("neocortex.cli", "neocortex.interface.protocol.worker"),
    # Fixed-scope read operations cross through one declared core port instead
    # of coupling the public adapter to each operational owner.
    ("neocortex.read_api", "_04_Nucleo_Operativo.read_api_port"),
    ("neocortex.sdk", "_04_Nucleo_Operativo"),
    # Value review is a separate advisory-only public contract.
    ("neocortex.value_cli_adapter", "_04_Nucleo_Operativo.value_review_port"),
    # Durable ReviewTask commands share the same bounded advisory-only port.
    ("neocortex.review_task_cli_adapter", "_04_Nucleo_Operativo.value_review_port"),
    # Archive is the first format capability moved into the canonical package.
    # Its bounded route still consumes these legacy foundation leaves until
    # the platform/foundation cohorts migrate, keeping the transition explicit
    # instead of hiding a reverse dependency in a compatibility wrapper.
    ("neocortex.capabilities.formats.archive.models", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.archive.route", "_04_Nucleo_Operativo.action_policy"),
    ("neocortex.capabilities.formats.archive.route", "_04_Nucleo_Operativo.bounded_subprocess"),
    ("neocortex.capabilities.formats.archive.route", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.archive.route", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.archive.route", "_04_Nucleo_Operativo.platform.shared.zip_safety"),
    ("neocortex.capabilities.formats.archive.route", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.archive.route", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.archive.route", "_04_Nucleo_Operativo.state"),
    ("neocortex.capabilities.formats.archive.state", "_04_Nucleo_Operativo.semantic_lexical"),
    ("neocortex.capabilities.formats.archive.state", "_04_Nucleo_Operativo.sqlite_schema_contract"),
    # DOCX is the next format cohort; these legacy foundation leaves remain
    # explicit until the shared platform and workflow families move.
    ("neocortex.capabilities.formats.docx.integrity", "_04_Nucleo_Operativo.memory_runtime"),
    ("neocortex.capabilities.formats.docx.integrity", "_04_Nucleo_Operativo.platform.shared.zip_safety"),
    ("neocortex.capabilities.formats.docx.layout", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.docx.models", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.docx.models", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.docx.route", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.docx.route", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.docx.route", "_04_Nucleo_Operativo.memory_runtime"),
    ("neocortex.capabilities.formats.docx.route", "_04_Nucleo_Operativo.platform.shared.zip_safety"),
    ("neocortex.capabilities.formats.docx.route", "_04_Nucleo_Operativo.review"),
    ("neocortex.capabilities.formats.docx.route", "_04_Nucleo_Operativo.state"),
    ("neocortex.capabilities.formats.docx.schema", "_04_Nucleo_Operativo.sqlite_schema_contract"),
    ("neocortex.capabilities.formats.docx.state", "_04_Nucleo_Operativo.sqlite_schema_lifecycle"),
    # Audio completes the currently migrated media cohort; transcription and
    # probing still use the legacy process/safety leaves until those families
    # receive their own canonical homes.
    ("neocortex.capabilities.formats.audio.models", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.audio.models", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.audio.probe", "_04_Nucleo_Operativo.bounded_subprocess"),
    ("neocortex.capabilities.formats.audio.route", "_04_Nucleo_Operativo.action_policy"),
    ("neocortex.capabilities.formats.audio.route", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.audio.route", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.audio.route", "_04_Nucleo_Operativo.memory_runtime"),
    ("neocortex.capabilities.formats.audio.route", "_04_Nucleo_Operativo.review"),
    ("neocortex.capabilities.formats.audio.route", "_04_Nucleo_Operativo.state"),
    ("neocortex.capabilities.formats.audio.state", "_04_Nucleo_Operativo.sqlite_schema_contract"),
    ("neocortex.capabilities.formats.audio.whisper", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.audio.whisper", "_04_Nucleo_Operativo.isolated_process"),
    # Image completes the current OCR/visual format cohort; decoding, OCR and
    # worker safety still consume these legacy foundation leaves until their
    # shared platform owners receive canonical homes.
    ("neocortex.capabilities.formats.image.adult", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.image.document", "_04_Nucleo_Operativo.bounded_subprocess"),
    ("neocortex.capabilities.formats.image.document", "_04_Nucleo_Operativo.ocr_image_preprocess"),
    ("neocortex.capabilities.formats.image.document", "_04_Nucleo_Operativo.ocr_profiles"),
    ("neocortex.capabilities.formats.image.document", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.image.features", "_04_Nucleo_Operativo.memory_runtime"),
    ("neocortex.capabilities.formats.image.isolation", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.image.isolation", "_04_Nucleo_Operativo.isolated_process"),
    ("neocortex.capabilities.formats.image.policy", "_04_Nucleo_Operativo.semantic_ontology"),
    ("neocortex.capabilities.formats.image.route", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.image.route", "_04_Nucleo_Operativo.memory_runtime"),
    ("neocortex.capabilities.formats.image.route", "_04_Nucleo_Operativo.ocr_profiles"),
    ("neocortex.capabilities.formats.image.route", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.image.route", "_04_Nucleo_Operativo.review"),
    ("neocortex.capabilities.formats.image.route", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.image.route", "_04_Nucleo_Operativo.state"),
    ("neocortex.capabilities.formats.image.state", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.image.state", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.image.state", "_04_Nucleo_Operativo.sqlite_paths"),
    ("neocortex.capabilities.formats.image.state", "_04_Nucleo_Operativo.sqlite_schema_contract"),
    # Office follows the same explicit transition pattern; legacy ZIP safety,
    # extraction workers and route/state foundations remain declared seams.
    ("neocortex.capabilities.formats.office.extraction", "_04_Nucleo_Operativo.platform.shared.zip_safety"),
    ("neocortex.capabilities.formats.office.legacy_worker", "_04_Nucleo_Operativo.bounded_subprocess"),
    ("neocortex.capabilities.formats.office.models", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.office.models", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.office.route", "_04_Nucleo_Operativo.action_policy"),
    ("neocortex.capabilities.formats.office.route", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.office.route", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.office.route", "_04_Nucleo_Operativo.memory_runtime"),
    ("neocortex.capabilities.formats.office.route", "_04_Nucleo_Operativo.review"),
    ("neocortex.capabilities.formats.office.route", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.office.route", "_04_Nucleo_Operativo.state"),
    ("neocortex.capabilities.formats.office.state", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.office.state", "_04_Nucleo_Operativo.sqlite_schema_contract"),
    # PDF now has a package-owned implementation while its shared safety,
    # OCR, retry and state foundations remain explicit legacy seams.
    ("neocortex.capabilities.formats.pdf.pdf_admin", "_04_Nucleo_Operativo.ocr_profiles"),
    ("neocortex.capabilities.formats.pdf.pdf_admin", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.pdf.pdf_derived", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.pdf.pdf_isolation", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.pdf.pdf_isolation", "_04_Nucleo_Operativo.isolated_process"),
    ("neocortex.capabilities.formats.pdf.pdf_isolation", "_04_Nucleo_Operativo.ocr_image_preprocess"),
    ("neocortex.capabilities.formats.pdf.pdf_isolation", "_04_Nucleo_Operativo.ocr_profiles"),
    ("neocortex.capabilities.formats.pdf.pdf_isolation", "_04_Nucleo_Operativo.retry_policy"),
    ("neocortex.capabilities.formats.pdf.pdf_isolation", "_04_Nucleo_Operativo.sqlite_paths"),
    ("neocortex.capabilities.formats.pdf.pdf_route", "_04_Nucleo_Operativo.actions"),
    ("neocortex.capabilities.formats.pdf.pdf_route", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.pdf.pdf_route", "_04_Nucleo_Operativo.ocr_profiles"),
    ("neocortex.capabilities.formats.pdf.pdf_route", "_04_Nucleo_Operativo.retry_policy"),
    ("neocortex.capabilities.formats.pdf.pdf_route", "_04_Nucleo_Operativo.review"),
    ("neocortex.capabilities.formats.pdf.pdf_route", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.pdf.pdf_route", "_04_Nucleo_Operativo.state"),
    ("neocortex.capabilities.formats.pdf.pdf_route_cache", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.pdf.pdf_route_cache", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.pdf.pdf_route_cache", "_04_Nucleo_Operativo.retry_policy"),
    ("neocortex.capabilities.formats.pdf.pdf_route_models", "_04_Nucleo_Operativo.ocr_profiles"),
    ("neocortex.capabilities.formats.pdf.pdf_route_models", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.pdf.pdf_route_models", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.pdf.pdf_route_storage", "_04_Nucleo_Operativo.retry_policy"),
    ("neocortex.capabilities.formats.pdf.pdf_runtime", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.pdf.pdf_runtime", "_04_Nucleo_Operativo.memory_runtime"),
    ("neocortex.capabilities.formats.pdf.pdf_schema", "_04_Nucleo_Operativo.sqlite_schema_contract"),
    ("neocortex.capabilities.formats.pdf.pdf_state", "_04_Nucleo_Operativo.sqlite_schema_lifecycle"),
    # Video completes the media route cohort; frame extraction and probing keep
    # explicit seams to the shared process, OCR, retry and state foundations.
    ("neocortex.capabilities.formats.video.frames", "_04_Nucleo_Operativo.bounded_subprocess"),
    ("neocortex.capabilities.formats.video.frames", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.video.models", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.video.probe", "_04_Nucleo_Operativo.bounded_subprocess"),
    ("neocortex.capabilities.formats.video.route", "_04_Nucleo_Operativo.action_policy"),
    ("neocortex.capabilities.formats.video.route", "_04_Nucleo_Operativo.cancellation"),
    ("neocortex.capabilities.formats.video.route", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.video.route", "_04_Nucleo_Operativo.ocr_profiles"),
    ("neocortex.capabilities.formats.video.route", "_04_Nucleo_Operativo.processing_provenance"),
    ("neocortex.capabilities.formats.video.route", "_04_Nucleo_Operativo.review"),
    ("neocortex.capabilities.formats.video.route", "_04_Nucleo_Operativo.route_filters"),
    ("neocortex.capabilities.formats.video.route", "_04_Nucleo_Operativo.state"),
    ("neocortex.capabilities.formats.video.state", "_04_Nucleo_Operativo.file_identity"),
    ("neocortex.capabilities.formats.video.state", "_04_Nucleo_Operativo.semantic_lexical"),
    ("neocortex.capabilities.formats.video.state", "_04_Nucleo_Operativo.sqlite_schema_contract"),
)

# The v5 graph is acyclic.  Keep the baseline empty so that reintroducing even
# a previously resolved component fails the primary architecture contract.
KNOWN_CYCLE_BASELINE: tuple[tuple[str, ...], ...] = ()

ContractKind = Literal["forbidden_dependency", "allowlisted_boundary", "baseline_no_new_cycles"]
ContractStatus = Literal["passed", "failed", "baseline"]


def stable_architecture_id(namespace: str, *parts: object) -> str:
    """Return a portable identifier for already-normalized values."""

    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return f"{namespace}:sha256:{digest}"


def module_root(module: str) -> str:
    return module.partition(".")[0]


def _in_module_tree(module: str, module_tree: str) -> bool:
    return module == module_tree or module.startswith(module_tree + ".")


def is_production_module(module: str) -> bool:
    return module_root(module) in PRODUCTION_ROOT_PACKAGES


@dataclass(frozen=True, slots=True, order=True)
class ImportLineDetail:
    line_number: int
    line_contents: str

    def __post_init__(self) -> None:
        if self.line_number < 1:
            raise ValueError("import detail line must be positive")
        if "\n" in self.line_contents or "\r" in self.line_contents:
            raise ValueError("import detail contents must be one line")

    def as_payload(self) -> dict[str, object]:
        return {"line_number": self.line_number, "line_contents": self.line_contents}


@dataclass(frozen=True, slots=True)
class ModuleImport:
    importer: str
    imported: str
    details: tuple[ImportLineDetail, ...] = ()

    def __post_init__(self) -> None:
        if not self.importer or not self.imported:
            raise ValueError("import modules cannot be empty")

    @property
    def relation_id(self) -> str:
        return stable_architecture_id("module-import-v1", self.importer, self.imported)

    def as_payload(self) -> dict[str, object]:
        return {
            "relation_id": self.relation_id,
            "relation": "module_import",
            "importer": self.importer,
            "imported": self.imported,
            "details": [item.as_payload() for item in self.details],
        }


@dataclass(frozen=True, slots=True)
class ArchitectureContractDefinition:
    contract_id: str
    title: str
    kind: ContractKind
    scope: str
    authority: Literal["gate", "baseline-no-new"]
    source_roots: tuple[str, ...] = ()
    target_roots: tuple[str, ...] = ()
    allowlist: tuple[tuple[str, str], ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "title": self.title,
            "kind": self.kind,
            "scope": self.scope,
            "authority": self.authority,
            "source_roots": list(self.source_roots),
            "target_roots": list(self.target_roots),
            "allowlist": [list(item) for item in self.allowlist],
        }


ARCHITECTURE_CONTRACTS = (
    ArchitectureContractDefinition(
        "core-does-not-depend-on-ui-v1",
        "Core does not depend on the UI",
        "forbidden_dependency",
        "transitive production import chains entering the UI",
        "gate",
        (_CORE,),
        (_UI,),
    ),
    ArchitectureContractDefinition(
        "foundation-does-not-depend-on-core-or-ui-v1",
        "Foundation packages do not depend on core or UI",
        "forbidden_dependency",
        "transitive production import chains entering core or UI",
        "gate",
        (_ENUMERATION,),
        (_CORE, _UI),
    ),
    ArchitectureContractDefinition(
        "progress-does-not-depend-on-other-production-v1",
        "Progress contracts do not depend on another product family",
        "forbidden_dependency",
        "transitive production imports leaving neocortex.progress",
        "gate",
        (_PROGRESS,),
        PRODUCTION_ROOT_PACKAGES,
    ),
    ArchitectureContractDefinition(
        "production-does-not-import-nonproduction-namespaces-v1",
        "Production code does not import tests, tools, or benchmarks",
        "forbidden_dependency",
        "direct imports from exact production packages",
        "gate",
        PRODUCTION_ROOT_PACKAGES,
        EXCLUDED_ARCHITECTURE_NAMESPACES,
    ),
    ArchitectureContractDefinition(
        "dedup-core-boundary-v1",
        "Deduplication uses only its declared core entry points",
        "allowlisted_boundary",
        "direct package-boundary imports",
        "gate",
        (_DEDUPLICATION,),
        (_CORE,),
        _DEDUP_CORE_ALLOWLIST,
    ),
    ArchitectureContractDefinition(
        "dedup-product-boundary-v1",
        "Deduplication uses only declared shared product foundations",
        "allowlisted_boundary",
        "direct imports leaving neocortex.deduplication",
        "gate",
        (_DEDUPLICATION,),
        ("neocortex",),
        _DEDUP_PRODUCT_ALLOWLIST,
    ),
    ArchitectureContractDefinition(
        "enumeration-product-boundary-v1",
        "Enumeration uses only declared shared product foundations",
        "allowlisted_boundary",
        "direct imports leaving neocortex.enumeration",
        "gate",
        (_ENUMERATION,),
        ("neocortex",),
        _ENUMERATION_PRODUCT_ALLOWLIST,
    ),
    ArchitectureContractDefinition(
        "interface-core-boundary-v1",
        "Interface uses only declared application-core entry points",
        "allowlisted_boundary",
        "direct imports from neocortex.interface into Core",
        "gate",
        (_UI,),
        (_CORE,),
        _INTERFACE_CORE_ALLOWLIST,
    ),
    ArchitectureContractDefinition(
        "interface-product-boundary-v1",
        "Interface uses only declared shared product foundations",
        "allowlisted_boundary",
        "direct imports leaving neocortex.interface",
        "gate",
        (_UI,),
        ("neocortex",),
        _INTERFACE_PRODUCT_ALLOWLIST,
    ),
    ArchitectureContractDefinition(
        "neocortex-core-ui-boundary-v1",
        "Public package uses only declared core and UI entry points",
        "allowlisted_boundary",
        "direct package-boundary imports",
        "gate",
        ("neocortex",),
        (_CORE, _UI),
        _NEOCORTEX_CORE_UI_ALLOWLIST,
    ),
    ArchitectureContractDefinition(
        "no-new-production-import-cycles-v1",
        "No new production import cycles",
        "baseline_no_new_cycles",
        "exact strongly connected component membership",
        "baseline-no-new",
        PRODUCTION_ROOT_PACKAGES,
        PRODUCTION_ROOT_PACKAGES,
    ),
)


@dataclass(frozen=True, slots=True)
class ArchitectureViolation:
    contract_id: str
    importer: str
    imported: str
    import_chain: tuple[str, ...]
    message: str
    details: tuple[ImportLineDetail, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def violation_id(self) -> str:
        return stable_architecture_id(
            "architecture-violation-v1",
            self.contract_id,
            *self.import_chain,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "violation_id": self.violation_id,
            "contract_id": self.contract_id,
            "importer": self.importer,
            "imported": self.imported,
            "import_chain": list(self.import_chain),
            "message": self.message,
            "details": [item.as_payload() for item in self.details],
            "metadata": dict(self.metadata),
            "authority": "advisory",
            "mutation_authority": False,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureContractEvaluation:
    definition: ArchitectureContractDefinition
    status: ContractStatus
    violations: tuple[ArchitectureViolation, ...]
    observed_count: int
    baseline_count: int = 0
    resolved_baseline_count: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "contract": self.definition.as_payload(),
            "status": self.status,
            "observed_count": self.observed_count,
            "baseline_count": self.baseline_count,
            "resolved_baseline_count": self.resolved_baseline_count,
            "violations": [item.as_payload() for item in self.violations],
        }


def _canonical_imports(imports: Iterable[ModuleImport]) -> tuple[ModuleImport, ...]:
    combined: dict[tuple[str, str], set[ImportLineDetail]] = {}
    for item in imports:
        combined.setdefault((item.importer, item.imported), set()).update(item.details)
    return tuple(
        ModuleImport(importer, imported, tuple(sorted(details)))
        for (importer, imported), details in sorted(combined.items())
    )


def _adjacency(
    modules: Iterable[str], imports: Sequence[ModuleImport]
) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {module: set() for module in modules}
    for item in imports:
        result.setdefault(item.importer, set()).add(item.imported)
        result.setdefault(item.imported, set())
    return {module: tuple(sorted(imported)) for module, imported in sorted(result.items())}


def cyclic_strongly_connected_components(
    modules: Iterable[str], imports: Sequence[ModuleImport]
) -> tuple[tuple[str, ...], ...]:
    """Return deterministic cyclic SCCs without recursion depth risk."""

    selected = {module for module in modules if is_production_module(module)}
    edges = _canonical_imports(imports)
    adjacency = {
        module: tuple(
            item.imported for item in edges if item.importer == module and item.imported in selected
        )
        for module in sorted(selected)
    }
    reverse: dict[str, list[str]] = {module: [] for module in selected}
    for importer, imported_modules in adjacency.items():
        for imported in imported_modules:
            reverse[imported].append(importer)
    for importers in reverse.values():
        importers.sort()

    visited: set[str] = set()
    finish_order: list[str] = []
    for start in sorted(selected):
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            module, expanded = stack.pop()
            if expanded:
                finish_order.append(module)
                continue
            if module in visited:
                continue
            visited.add(module)
            stack.append((module, True))
            for imported in reversed(adjacency[module]):
                if imported not in visited:
                    stack.append((imported, False))

    assigned: set[str] = set()
    cyclic: list[tuple[str, ...]] = []
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component: list[str] = []
        component_stack = [start]
        assigned.add(start)
        while component_stack:
            module = component_stack.pop()
            component.append(module)
            for importer in reversed(reverse[module]):
                if importer not in assigned:
                    assigned.add(importer)
                    component_stack.append(importer)
        normalized = tuple(sorted(component))
        if len(normalized) > 1 or normalized[0] in adjacency[normalized[0]]:
            cyclic.append(normalized)
    return tuple(sorted(cyclic))


def _shortest_path(
    adjacency: Mapping[str, Sequence[str]], starts: Iterable[str], target: str
) -> tuple[str, ...] | None:
    parents: dict[str, str | None] = {}
    queue: deque[str] = deque()
    for start in sorted(set(starts)):
        parents[start] = None
        queue.append(start)
    while queue:
        module = queue.popleft()
        if module == target:
            chain: list[str] = []
            current: str | None = module
            while current is not None:
                chain.append(current)
                current = parents[current]
            return tuple(reversed(chain))
        for imported in adjacency.get(module, ()):
            if imported not in parents:
                parents[imported] = module
                queue.append(imported)
    return None


def shortest_cycle_chain(
    component: Sequence[str], imports: Sequence[ModuleImport]
) -> tuple[str, ...]:
    """Return one shortest, lexicographically stable closed chain for an SCC."""

    selected = set(component)
    adjacency = _adjacency(selected, imports)
    candidates: list[tuple[str, ...]] = []
    for start in sorted(selected):
        if start in adjacency.get(start, ()):
            candidates.append((start, start))
        for imported in adjacency.get(start, ()):
            if imported == start or imported not in selected:
                continue
            path = _shortest_path(adjacency, (imported,), start)
            if path is not None:
                candidates.append((start, *path))
    if not candidates:
        raise ValueError("component does not contain a cycle")
    return min(candidates, key=lambda chain: (len(chain), chain))


def _detail_index(imports: Sequence[ModuleImport]) -> dict[tuple[str, str], ModuleImport]:
    return {(item.importer, item.imported): item for item in imports}


def _boundary_entries(
    modules: Sequence[str],
    imports: Sequence[ModuleImport],
    *,
    source: Callable[[str], bool],
    target: Callable[[str], bool],
) -> tuple[tuple[str, ...], ...]:
    adjacency = _adjacency(modules, imports)
    starts = tuple(module for module in sorted(modules) if source(module))
    permitted_adjacency = {
        module: tuple(item for item in imported if not target(item))
        for module, imported in adjacency.items()
        if not target(module)
    }
    results: set[tuple[str, ...]] = set()
    for item in imports:
        if not target(item.imported) or target(item.importer):
            continue
        prefix = _shortest_path(permitted_adjacency, starts, item.importer)
        if prefix is not None:
            results.add((*prefix, item.imported))
    return tuple(sorted(results, key=lambda chain: (len(chain), chain)))


def _violation_for_chain(
    contract_id: str,
    chain: Sequence[str],
    message: str,
    index: Mapping[tuple[str, str], ModuleImport],
    *,
    metadata: Mapping[str, object] | None = None,
) -> ArchitectureViolation:
    edge = index.get((chain[-2], chain[-1]))
    return ArchitectureViolation(
        contract_id,
        chain[0],
        chain[-1],
        tuple(chain),
        message,
        () if edge is None else edge.details,
        {} if metadata is None else metadata,
    )


def evaluate_architecture_contracts(
    modules: Iterable[str], imports: Iterable[ModuleImport]
) -> tuple[ArchitectureContractEvaluation, ...]:
    """Evaluate the v1 contract set against one normalized graph."""

    normalized_modules = tuple(sorted(set(modules)))
    normalized_imports = _canonical_imports(imports)
    index = _detail_index(normalized_imports)
    definitions = {item.contract_id: item for item in ARCHITECTURE_CONTRACTS}
    evaluations: list[ArchitectureContractEvaluation] = []

    forbidden_groups = (
        (
            "core-does-not-depend-on-ui-v1",
            lambda module: module_root(module) == _CORE,
            lambda module: _in_module_tree(module, _UI),
        ),
        (
            "foundation-does-not-depend-on-core-or-ui-v1",
            lambda module: _in_module_tree(module, _ENUMERATION),
            lambda module: module_root(module) == _CORE or _in_module_tree(module, _UI),
        ),
        (
            "progress-does-not-depend-on-other-production-v1",
            lambda module: _in_module_tree(module, _PROGRESS),
            lambda module: is_production_module(module) and not _in_module_tree(module, _PROGRESS),
        ),
    )
    for contract_id, source, target in forbidden_groups:
        chains = _boundary_entries(
            normalized_modules,
            normalized_imports,
            source=source,
            target=target,
        )
        violations = tuple(
            _violation_for_chain(
                contract_id,
                chain,
                "forbidden architectural dependency",
                index,
            )
            for chain in chains
        )
        evaluations.append(
            ArchitectureContractEvaluation(
                definitions[contract_id],
                "failed" if violations else "passed",
                violations,
                len(chains),
            )
        )

    namespace_contract = "production-does-not-import-nonproduction-namespaces-v1"
    namespace_imports = tuple(
        item
        for item in normalized_imports
        if is_production_module(item.importer)
        and module_root(item.imported) in EXCLUDED_ARCHITECTURE_NAMESPACES
    )
    namespace_violations = tuple(
        _violation_for_chain(
            namespace_contract,
            (item.importer, item.imported),
            "production module imports an excluded non-production namespace",
            index,
        )
        for item in namespace_imports
    )
    evaluations.append(
        ArchitectureContractEvaluation(
            definitions[namespace_contract],
            "failed" if namespace_violations else "passed",
            namespace_violations,
            len(namespace_imports),
        )
    )

    allowlisted_contracts = (
        (
            "dedup-core-boundary-v1",
            lambda module: _in_module_tree(module, _DEDUPLICATION),
            lambda module: module_root(module) == _CORE,
        ),
        (
            "dedup-product-boundary-v1",
            lambda module: _in_module_tree(module, _DEDUPLICATION),
            lambda module: (
                module_root(module) == "neocortex" and not _in_module_tree(module, _DEDUPLICATION)
            ),
        ),
        (
            "enumeration-product-boundary-v1",
            lambda module: _in_module_tree(module, _ENUMERATION),
            lambda module: (
                module_root(module) == "neocortex" and not _in_module_tree(module, _ENUMERATION)
            ),
        ),
        (
            "interface-core-boundary-v1",
            lambda module: _in_module_tree(module, _UI),
            lambda module: module_root(module) == _CORE,
        ),
        (
            "interface-product-boundary-v1",
            lambda module: _in_module_tree(module, _UI),
            lambda module: module_root(module) == "neocortex" and not _in_module_tree(module, _UI),
        ),
        (
            "neocortex-core-ui-boundary-v1",
            lambda module: (
                module_root(module) == "neocortex"
                and not any(_in_module_tree(module, tree) for tree in _CANONICAL_FAMILY_TREES)
            ),
            lambda module: module_root(module) == _CORE or _in_module_tree(module, _UI),
        ),
    )
    for contract_id, source, target in allowlisted_contracts:
        definition = definitions[contract_id]
        allowlist = set(definition.allowlist)
        crossings = tuple(
            item for item in normalized_imports if source(item.importer) and target(item.imported)
        )
        violations = tuple(
            _violation_for_chain(
                contract_id,
                (item.importer, item.imported),
                "package-boundary import is not in the explicit allowlist",
                index,
                metadata={"allowed_imports": [list(value) for value in definition.allowlist]},
            )
            for item in crossings
            if (item.importer, item.imported) not in allowlist
        )
        evaluations.append(
            ArchitectureContractEvaluation(
                definition,
                "failed" if violations else "passed",
                violations,
                len(crossings),
            )
        )

    cycle_contract = "no-new-production-import-cycles-v1"
    components = cyclic_strongly_connected_components(normalized_modules, normalized_imports)
    baseline = {tuple(component) for component in KNOWN_CYCLE_BASELINE}
    observed = set(components)
    new_components = tuple(sorted(observed - baseline))
    cycle_violations: list[ArchitectureViolation] = []
    for component in new_components:
        chain = shortest_cycle_chain(component, normalized_imports)
        cycle_violations.append(
            _violation_for_chain(
                cycle_contract,
                chain,
                "production import cycle is not in the published acyclic v5 baseline",
                index,
                metadata={"component": list(component)},
            )
        )
    if cycle_violations:
        cycle_status: ContractStatus = "failed"
    elif observed:
        cycle_status = "baseline"
    else:
        cycle_status = "passed"
    evaluations.append(
        ArchitectureContractEvaluation(
            definitions[cycle_contract],
            cycle_status,
            tuple(cycle_violations),
            len(components),
            len(baseline),
            len(baseline - observed),
        )
    )

    return tuple(sorted(evaluations, key=lambda item: item.definition.contract_id))


def architecture_contract_manifest() -> dict[str, object]:
    return {
        "schema": ARCHITECTURE_CONTRACT_SCHEMA,
        "baseline_id": ARCHITECTURE_BASELINE_ID,
        "domain": {
            "included_root_packages": list(PRODUCTION_ROOT_PACKAGES),
            "excluded_namespaces": list(EXCLUDED_ARCHITECTURE_NAMESPACES),
            "excluded_standalone_modules": list(EXCLUDED_STANDALONE_MODULES),
        },
        "contracts": [item.as_payload() for item in ARCHITECTURE_CONTRACTS],
        "known_cycle_components": [list(item) for item in KNOWN_CYCLE_BASELINE],
    }
