"""Lightweight runtime-capability declarations for optional NeoCortex routes.

These probes inspect import specs, distribution metadata and executable paths.
They never import an optional engine, instantiate a model, inspect user content,
download data or create runtime state.  Deeper model and binary-version checks
belong to the later doctor facade and can consume this public contract.
"""


# region [01] Versioned public contracts

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from itertools import islice
from types import MappingProxyType

RUNTIME_CAPABILITY_SCHEMA_VERSION = 1
RUNTIME_CAPABILITY_PROBE_POLICY = "metadata-spec-path-only-v1"


class CapabilityState(StrEnum):
    """Availability of one capability under its declared prerequisites."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class RequirementKind(StrEnum):
    """Safe prerequisite kinds understood by the lightweight probe."""

    PYTHON_DISTRIBUTION = "python_distribution"
    EXECUTABLE = "executable"


@dataclass(frozen=True, slots=True)
class RuntimeRequirement:
    """One required or degradable component of a public capability."""

    component: str
    kind: RequirementKind
    required: bool
    missing_reason: str
    extra: str | None = None
    distribution: str | None = None
    module: str | None = None
    executable: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("component", self.component),
            ("missing_reason", self.missing_reason),
        ):
            if not value.strip():
                raise ValueError(f"{name} cannot be blank")
        if self.extra is not None and not self.extra.strip():
            raise ValueError("extra cannot be blank when present")
        if self.kind is RequirementKind.PYTHON_DISTRIBUTION and (
            not self.distribution or not self.module or self.executable is not None
        ):
            raise ValueError("Python requirements need distribution and module only")
        elif self.kind is RequirementKind.EXECUTABLE and (
            not self.executable or self.distribution is not None or self.module is not None
        ):
            raise ValueError("executable requirements need executable only")


@dataclass(frozen=True, slots=True)
class RuntimeComponentStatus:
    """Observed lightweight status for one declared component."""

    requirement: RuntimeRequirement
    available: bool
    version: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "component": self.requirement.component,
            "kind": self.requirement.kind.value,
            "required": self.requirement.required,
            "available": self.available,
        }
        if self.requirement.extra is not None:
            payload["extra"] = self.requirement.extra
        if self.version is not None:
            payload["version"] = self.version
        if self.path is not None:
            payload["path"] = self.path
        if not self.available:
            payload["reason"] = self.requirement.missing_reason
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeCapabilitySpec:
    """Static, ordered prerequisite declaration for one route or surface."""

    name: str
    requirements: tuple[RuntimeRequirement, ...]
    extra: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("capability name cannot be blank")
        if self.extra is not None and not self.extra.strip():
            raise ValueError("capability extra cannot be blank when present")
        components = tuple(item.component for item in self.requirements)
        if len(components) != len(set(components)):
            raise ValueError(f"duplicate component in capability {self.name}")


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityStatus:
    """Versioned result that explicitly separates absence from degradation."""

    capability: str
    state: CapabilityState
    components: tuple[RuntimeComponentStatus, ...]
    degradation_reasons: tuple[str, ...]
    extra: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
            "kind": "runtime_capability_status",
            "probe_policy": RUNTIME_CAPABILITY_PROBE_POLICY,
            "capability": self.capability,
            "state": self.state.value,
            "models_loaded": False,
            "models_downloaded": False,
            "components": [component.to_dict() for component in self.components],
            "degradation_reasons": list(self.degradation_reasons),
        }
        if self.extra is not None:
            payload["extra"] = self.extra
        return payload


# endregion [01]


# region [02] Static route and surface declarations


def _distribution(
    component: str,
    distribution: str,
    module: str,
    *,
    required: bool,
    missing_reason: str,
    extra: str | None,
) -> RuntimeRequirement:
    return RuntimeRequirement(
        component=component,
        kind=RequirementKind.PYTHON_DISTRIBUTION,
        required=required,
        missing_reason=missing_reason,
        extra=extra,
        distribution=distribution,
        module=module,
    )


def _executable(
    component: str,
    executable: str,
    *,
    required: bool,
    missing_reason: str,
    extra: str | None,
) -> RuntimeRequirement:
    return RuntimeRequirement(
        component=component,
        kind=RequirementKind.EXECUTABLE,
        required=required,
        missing_reason=missing_reason,
        extra=extra,
        executable=executable,
    )


_BASE_REQUIREMENTS = (
    _distribution(
        "rich",
        "rich",
        "rich",
        required=True,
        missing_reason="base_rich_unavailable",
        extra=None,
    ),
    _distribution(
        "xxhash",
        "xxhash",
        "xxhash",
        required=True,
        missing_reason="base_xxhash_unavailable",
        extra=None,
    ),
)


def _with_base(*requirements: RuntimeRequirement) -> tuple[RuntimeRequirement, ...]:
    return (*_BASE_REQUIREMENTS, *requirements)


ROUTE_CAPABILITY_NAMES = (
    "pdf",
    "docx",
    "office",
    "archive",
    "audio",
    "image",
    "code",
)

CAPABILITY_SPECS: Mapping[str, RuntimeCapabilitySpec] = MappingProxyType(
    {
        "pdf": RuntimeCapabilitySpec(
            "pdf",
            _with_base(
                _distribution(
                    "pymupdf",
                    "PyMuPDF",
                    "fitz",
                    required=True,
                    missing_reason="pdf_extractor_unavailable",
                    extra="documents",
                ),
                _distribution(
                    "pdfminer",
                    "pdfminer.six",
                    "pdfminer",
                    required=False,
                    missing_reason="pdf_fallback_unavailable",
                    extra="documents",
                ),
                _distribution(
                    "pillow",
                    "Pillow",
                    "PIL",
                    required=False,
                    missing_reason="pdf_ocr_image_runtime_unavailable",
                    extra="documents",
                ),
                _distribution(
                    "pytesseract",
                    "pytesseract",
                    "pytesseract",
                    required=False,
                    missing_reason="pdf_ocr_adapter_unavailable",
                    extra="documents",
                ),
                _executable(
                    "tesseract",
                    "tesseract",
                    required=False,
                    missing_reason="pdf_ocr_executable_unavailable",
                    extra="documents",
                ),
                _executable(
                    "qpdf",
                    "qpdf",
                    required=False,
                    missing_reason="pdf_recovery_unavailable",
                    extra="documents",
                ),
            ),
            extra="documents",
        ),
        "docx": RuntimeCapabilitySpec("docx", _with_base()),
        "office": RuntimeCapabilitySpec("office", _with_base()),
        "archive": RuntimeCapabilitySpec(
            "archive",
            _with_base(
                _distribution(
                    "pymupdf",
                    "PyMuPDF",
                    "fitz",
                    required=False,
                    missing_reason="archive_pdf_text_extractor_unavailable",
                    extra="documents",
                ),
            ),
            extra="documents",
        ),
        "audio": RuntimeCapabilitySpec(
            "audio",
            _with_base(
                _distribution(
                    "faster-whisper",
                    "faster-whisper",
                    "faster_whisper",
                    required=True,
                    missing_reason="audio_backend_unavailable",
                    extra="audio",
                ),
                _distribution(
                    "ctranslate2",
                    "ctranslate2",
                    "ctranslate2",
                    required=True,
                    missing_reason="audio_inference_runtime_unavailable",
                    extra="audio",
                ),
                _executable(
                    "ffprobe",
                    "ffprobe",
                    required=True,
                    missing_reason="audio_probe_unavailable",
                    extra="audio",
                ),
            ),
            extra="audio",
        ),
        "image": RuntimeCapabilitySpec(
            "image",
            _with_base(
                _distribution(
                    "pillow",
                    "Pillow",
                    "PIL",
                    required=True,
                    missing_reason="image_decode_unavailable",
                    extra="image",
                ),
                _distribution(
                    "nudenet",
                    "nudenet",
                    "nudenet",
                    required=False,
                    missing_reason="image_adult_classifier_unavailable",
                    extra="image",
                ),
                _executable(
                    "tesseract",
                    "tesseract",
                    required=False,
                    missing_reason="image_document_ocr_unavailable",
                    extra="image",
                ),
            ),
            extra="image",
        ),
        "code": RuntimeCapabilitySpec(
            "code",
            _with_base(
                _distribution(
                    "ruff",
                    "ruff",
                    "ruff",
                    required=False,
                    missing_reason="code_ruff_provider_unavailable",
                    extra=None,
                ),
                _distribution(
                    "mypy",
                    "mypy",
                    "mypy",
                    required=False,
                    missing_reason="code_mypy_provider_unavailable",
                    extra=None,
                ),
                _distribution(
                    "vulture",
                    "vulture",
                    "vulture",
                    required=False,
                    missing_reason="code_vulture_provider_unavailable",
                    extra=None,
                ),
                _distribution(
                    "grimp",
                    "grimp",
                    "grimp",
                    required=False,
                    missing_reason="code_grimp_provider_unavailable",
                    extra=None,
                ),
                _distribution(
                    "complexipy",
                    "complexipy",
                    "complexipy",
                    required=False,
                    missing_reason="code_complexipy_provider_unavailable",
                    extra=None,
                ),
                _executable(
                    "node",
                    "node",
                    required=False,
                    missing_reason="code_pyright_node_unavailable",
                    extra=None,
                ),
                _executable(
                    "pyright",
                    "pyright",
                    required=False,
                    missing_reason="code_pyright_provider_unavailable",
                    extra=None,
                ),
            ),
        ),
        "semantic": RuntimeCapabilitySpec(
            "semantic",
            _with_base(
                _distribution(
                    "fastembed",
                    "fastembed",
                    "fastembed",
                    required=True,
                    missing_reason="semantic_backend_unavailable",
                    extra="semantic",
                ),
                _distribution(
                    "numpy",
                    "numpy",
                    "numpy",
                    required=True,
                    missing_reason="semantic_numeric_runtime_unavailable",
                    extra="semantic",
                ),
                _distribution(
                    "pillow",
                    "Pillow",
                    "PIL",
                    required=True,
                    missing_reason="semantic_image_probe_unavailable",
                    extra="semantic",
                ),
            ),
            extra="semantic",
        ),
        "ui": RuntimeCapabilitySpec(
            "ui",
            _with_base(
                _distribution(
                    "pyside6",
                    "PySide6",
                    "PySide6",
                    required=True,
                    missing_reason="ui_runtime_unavailable",
                    extra="ui",
                ),
            ),
            extra="ui",
        ),
    }
)

# endregion [02]


# region [03] Safe prerequisite probes

ModuleFinder = Callable[[str], object | None]
DistributionVersion = Callable[[str], str]
ExecutableFinder = Callable[[str], str | None]


def _python_status(
    requirement: RuntimeRequirement,
    *,
    module_finder: ModuleFinder,
    distribution_version: DistributionVersion,
) -> RuntimeComponentStatus:
    assert requirement.module is not None
    assert requirement.distribution is not None
    try:
        module_available = module_finder(requirement.module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        module_available = False
    try:
        version = distribution_version(requirement.distribution)
    except (metadata.PackageNotFoundError, OSError, ValueError):
        version = None
    return RuntimeComponentStatus(
        requirement,
        available=module_available and version is not None,
        version=version,
    )


def _executable_status(
    requirement: RuntimeRequirement,
    *,
    executable_finder: ExecutableFinder,
) -> RuntimeComponentStatus:
    assert requirement.executable is not None
    try:
        path = executable_finder(requirement.executable)
    except OSError:
        path = None
    return RuntimeComponentStatus(requirement, available=path is not None, path=path)


def inspect_runtime_capability(
    capability: str,
    *,
    module_finder: ModuleFinder | None = None,
    distribution_version: DistributionVersion | None = None,
    executable_finder: ExecutableFinder | None = None,
) -> RuntimeCapabilityStatus:
    """Inspect one declaration without importing an engine or touching models."""

    try:
        spec = CAPABILITY_SPECS[capability]
    except KeyError as exc:
        raise ValueError(f"unknown runtime capability: {capability}") from exc
    find_module = importlib.util.find_spec if module_finder is None else module_finder
    read_version = metadata.version if distribution_version is None else distribution_version
    find_executable = shutil.which if executable_finder is None else executable_finder

    components: list[RuntimeComponentStatus] = []
    for requirement in spec.requirements:
        if requirement.kind is RequirementKind.PYTHON_DISTRIBUTION:
            component = _python_status(
                requirement,
                module_finder=find_module,
                distribution_version=read_version,
            )
        else:
            component = _executable_status(
                requirement,
                executable_finder=find_executable,
            )
        components.append(component)

    missing_required = tuple(
        component.requirement.missing_reason
        for component in components
        if component.requirement.required and not component.available
    )
    missing_optional = tuple(
        component.requirement.missing_reason
        for component in components
        if not component.requirement.required and not component.available
    )
    state = (
        CapabilityState.UNAVAILABLE
        if missing_required
        else CapabilityState.DEGRADED
        if missing_optional
        else CapabilityState.AVAILABLE
    )
    return RuntimeCapabilityStatus(
        capability=spec.name,
        state=state,
        components=tuple(components),
        degradation_reasons=(*missing_required, *missing_optional),
        extra=spec.extra,
    )


def inspect_runtime_capabilities(
    capabilities: Iterable[str] | None = None,
    *,
    module_finder: ModuleFinder | None = None,
    distribution_version: DistributionVersion | None = None,
    executable_finder: ExecutableFinder | None = None,
) -> tuple[RuntimeCapabilityStatus, ...]:
    """Inspect an ordered bounded set, defaulting to every static declaration."""

    if capabilities is None:
        selected = tuple(CAPABILITY_SPECS)
    else:
        maximum_selection_size = len(CAPABILITY_SPECS)
        selected = tuple(islice(capabilities, maximum_selection_size + 1))
    if len(selected) != len(set(selected)):
        raise ValueError("runtime capability names cannot be duplicated")
    if len(selected) > len(CAPABILITY_SPECS):
        raise ValueError(
            "runtime capability selection cannot exceed the declared capability "
            f"count ({len(CAPABILITY_SPECS)})"
        )
    return tuple(
        inspect_runtime_capability(
            name,
            module_finder=module_finder,
            distribution_version=distribution_version,
            executable_finder=executable_finder,
        )
        for name in selected
    )


# endregion [03]


__all__ = (
    "CAPABILITY_SPECS",
    "ROUTE_CAPABILITY_NAMES",
    "RUNTIME_CAPABILITY_PROBE_POLICY",
    "RUNTIME_CAPABILITY_SCHEMA_VERSION",
    "CapabilityState",
    "RequirementKind",
    "RuntimeCapabilitySpec",
    "RuntimeCapabilityStatus",
    "RuntimeComponentStatus",
    "RuntimeRequirement",
    "inspect_runtime_capabilities",
    "inspect_runtime_capability",
)
