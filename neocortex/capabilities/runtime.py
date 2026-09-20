"""Lightweight runtime-capability declarations for optional NeoCortex routes.

These probes inspect import specs, declared version requirements, distribution
metadata and executable paths.  They never import an optional engine,
instantiate a model, inspect user content, download data or create runtime
state.  Metadata compatibility and a located executable are prerequisite facts,
not proof of a loadable native extension, local model or successful processing.
"""


# region [01] Versioned public contracts

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from itertools import islice
from pathlib import Path
from types import MappingProxyType

from .broker import (
    MAX_CAPABILITY_CANDIDATES,
    MAX_CAPABILITY_EVIDENCE_VALUES,
    MAX_CAPABILITY_MANIFESTS,
    CapabilityAvailability,
    CapabilityBinaryIdentity,
    CapabilityBroker,
    CapabilityLanguageMode,
    CapabilityLifecycle,
    CapabilityManifest,
    CapabilityMimeBinaryAlternatives,
    CapabilityPolicy,
    CapabilityPrivacy,
    CapabilityQualityMetric,
    CapabilityQualityRequirement,
    CapabilityRequest,
    CapabilitySelection,
)
from .requirement_metadata import inspect_requirement_compatibility

RUNTIME_CAPABILITY_SCHEMA_VERSION = 1
RUNTIME_CAPABILITY_PROBE_POLICY = "metadata-spec-path-only-v1"


class CapabilityState(StrEnum):
    """Availability of one capability under its declared prerequisites."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class RuntimeComponentState(StrEnum):
    """Evidence observed by a prerequisite probe, not processing success."""

    ABSENT = "absent"
    PRESENT_COMPATIBLE = "present_compatible"
    PRESENT_INCOMPATIBLE = "present_incompatible"
    PRESENT_UNVERIFIED = "present_unverified"
    EXECUTABLE_LOCATED = "executable_located"


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
    status: RuntimeComponentState | None = None
    applicable_requirement: str | None = None
    requirement_source: str | None = None
    reason: str | None = None

    @property
    def observation_state(self) -> RuntimeComponentState:
        if self.status is not None:
            return self.status
        if not self.available:
            return RuntimeComponentState.ABSENT
        if self.requirement.kind is RequirementKind.EXECUTABLE:
            return RuntimeComponentState.EXECUTABLE_LOCATED
        return RuntimeComponentState.PRESENT_UNVERIFIED

    @property
    def unavailable_reason(self) -> str | None:
        return self.reason or (None if self.available else self.requirement.missing_reason)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "component": self.requirement.component,
            "kind": self.requirement.kind.value,
            "required": self.requirement.required,
            "available": self.available,
            "status": self.observation_state.value,
            "functional_status": "not_checked",
        }
        if self.requirement.distribution is not None:
            payload["distribution"] = self.requirement.distribution
            payload["observed_version"] = self.version
            payload["requirement"] = self.applicable_requirement
            payload["requirement_source"] = self.requirement_source
        if self.requirement.extra is not None:
            payload["extra"] = self.requirement.extra
        if self.version is not None:
            payload["version"] = self.version
        if self.path is not None:
            payload["path"] = self.path
        if self.unavailable_reason is not None:
            payload["reason"] = self.unavailable_reason
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
    enabled: bool = True
    processing_error: str | None = None

    @property
    def operational_state(self) -> str:
        """Keep configuration, prerequisite failure and observed failure separate."""

        if not self.enabled:
            return "disabled"
        if self.processing_error is not None:
            return "failed"
        if self.state is CapabilityState.UNAVAILABLE:
            return "blocked_by_requirements"
        return "not_checked"

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
            "kind": "runtime_capability_status",
            "probe_policy": RUNTIME_CAPABILITY_PROBE_POLICY,
            "capability": self.capability,
            "state": self.state.value,
            "enabled": self.enabled,
            "operational_state": self.operational_state,
            "processing_status": "failed" if self.processing_error is not None else "not_checked",
            "prerequisite_scope": "metadata_and_paths",
            "models_loaded": False,
            "models_downloaded": False,
            "components": [component.to_dict() for component in self.components],
            "degradation_reasons": list(self.degradation_reasons),
        }
        if self.extra is not None:
            payload["extra"] = self.extra
        if self.capability in {"semantic", "audio"}:
            payload["model_status"] = "not_checked"
        if self.processing_error is not None:
            payload["processing_error"] = self.processing_error
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
        "packaging",
        "packaging",
        "packaging",
        required=True,
        missing_reason="base_packaging_unavailable",
        extra=None,
    ),
    _distribution(
        "rich",
        "rich",
        "rich",
        required=True,
        missing_reason="base_rich_unavailable",
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
    "text",
    "audio",
    "video",
    "image",
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
                _distribution(
                    "pillow",
                    "Pillow",
                    "PIL",
                    required=False,
                    missing_reason="archive_image_ocr_decoder_unavailable",
                    extra="documents",
                ),
                _distribution(
                    "pytesseract",
                    "pytesseract",
                    "pytesseract",
                    required=False,
                    missing_reason="archive_ocr_adapter_unavailable",
                    extra="documents",
                ),
                _executable(
                    "tesseract",
                    "tesseract",
                    required=False,
                    missing_reason="archive_ocr_executable_unavailable",
                    extra="documents",
                ),
            ),
            extra="documents",
        ),
        "text": RuntimeCapabilitySpec(
            "text",
            _with_base(),
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
        "video": RuntimeCapabilitySpec(
            "video",
            _with_base(
                _executable(
                    "ffmpeg",
                    "ffmpeg",
                    required=True,
                    missing_reason="video_frame_extractor_unavailable",
                    extra=None,
                ),
                _executable(
                    "ffprobe",
                    "ffprobe",
                    required=True,
                    missing_reason="video_probe_unavailable",
                    extra=None,
                ),
                _distribution(
                    "pillow",
                    "Pillow",
                    "PIL",
                    required=False,
                    missing_reason="video_frame_ocr_decoder_unavailable",
                    extra="documents",
                ),
                _executable(
                    "tesseract",
                    "tesseract",
                    required=False,
                    missing_reason="video_frame_ocr_unavailable",
                    extra="documents",
                ),
            ),
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

TEXT_EXTRACT_CAPABILITY_ID = "text.extract"
TEXT_RAW_INPUT_SCHEMA = "neocortex.raw-bytes/v1"
TEXT_REPRESENTATION_OUTPUT_SCHEMA = "neocortex.text-representation/v1"
TEXT_BUILTIN_IMPLEMENTATION_ID = "neocortex.text.builtin"

_TEXT_BUILTIN_MIMES = (
    "text/plain",
    "text/csv",
    "text/tab-separated-values",
    "text/markdown",
    "text/html",
    "application/xml",
    "application/json",
    "message/rfc822",
)
CAPABILITY_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        capability_id=TEXT_EXTRACT_CAPABILITY_ID,
        capability_version="2",
        implementation_id=TEXT_BUILTIN_IMPLEMENTATION_ID,
        provider="neocortex-builtin",
        provider_version="text-route-v2",
        lifecycle=CapabilityLifecycle.PRODUCTION,
        supported_platforms=("linux", "windows"),
        modalities=("document",),
        input_schemas=(TEXT_RAW_INPUT_SCHEMA,),
        output_schemas=(TEXT_REPRESENTATION_OUTPUT_SCHEMA,),
        mime_types=_TEXT_BUILTIN_MIMES,
        language_mode=CapabilityLanguageMode.AGNOSTIC,
        languages=(),
        deterministic=True,
        reproducibility_classes=("environment_bound",),
        incremental=True,
        cancellation=False,
        checkpointing=False,
        max_input_bytes=None,
        default_timeout_seconds=None,
        cpu_threads=1,
        ram_bytes=None,
        gpu_required=False,
        network_required=False,
        privacy=CapabilityPrivacy.LOCAL_ONLY,
        optional_extra="documents",
        # XXH3 is an optional accelerator.  The product's hash compatibility
        # layer supplies a deterministic stdlib fallback, so its absence must
        # never make the builtin text implementation unavailable.
        required_components=(),
        required_binaries=(),
        mime_binary_alternatives=(),
        required_models=(),
        compatibility=("text-route-v2", "text-state-v2"),
        quality_metrics=(),
        estimated_cost=None,
        estimated_latency_ms=None,
    ),
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
    owner_distribution: str = "neocortex-framework",
) -> RuntimeComponentStatus:
    assert requirement.module is not None
    assert requirement.distribution is not None
    try:
        # find_spec on a dotted name imports its parent.  A metadata probe must
        # not initialize a native engine merely to look for a child module.
        module_available = module_finder(requirement.module.partition(".")[0]) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        module_available = False
    try:
        version = distribution_version(requirement.distribution)
    except (metadata.PackageNotFoundError, OSError, ValueError):
        version = None
    compatibility = inspect_requirement_compatibility(
        requirement.distribution,
        version,
        extra=requirement.extra,
        owner_distribution=owner_distribution,
    )
    reason: str | None
    if not module_available and version is None:
        state = RuntimeComponentState.ABSENT
        reason = requirement.missing_reason
    elif not module_available:
        state = RuntimeComponentState.PRESENT_UNVERIFIED
        reason = f"{requirement.missing_reason}:module_spec_unavailable"
    elif version is None:
        state = RuntimeComponentState.PRESENT_UNVERIFIED
        reason = f"{requirement.missing_reason}:distribution_metadata_unavailable"
    elif compatibility.compatible is False:
        state = RuntimeComponentState.PRESENT_INCOMPATIBLE
        reason = f"{requirement.missing_reason}:{compatibility.reason}"
    elif compatibility.compatible is None:
        state = RuntimeComponentState.PRESENT_UNVERIFIED
        reason = f"{requirement.missing_reason}:{compatibility.reason}"
    else:
        state = RuntimeComponentState.PRESENT_COMPATIBLE
        reason = None
    return RuntimeComponentStatus(
        requirement,
        available=state is RuntimeComponentState.PRESENT_COMPATIBLE,
        version=version,
        status=state,
        applicable_requirement=compatibility.requirement,
        requirement_source=compatibility.source,
        reason=reason,
    )


def inspect_python_component(
    distribution: str,
    module: str,
    *,
    extra: str | None = None,
    owner_distribution: str = "neocortex-framework",
    module_finder: ModuleFinder | None = None,
    distribution_version: DistributionVersion | None = None,
) -> RuntimeComponentStatus:
    """Expose the same metadata-only requirement check for local model probes.

    An installed backend is not proof that its extension loads or that model
    files exist.  Callers retain their own model, provenance and processing
    contracts; this probe never imports that backend or downloads anything.
    """

    requirement = _distribution(
        distribution,
        distribution,
        module,
        required=True,
        missing_reason=f"{distribution}_unavailable",
        extra=extra,
    )
    return _python_status(
        requirement,
        module_finder=importlib.util.find_spec if module_finder is None else module_finder,
        distribution_version=(
            metadata.version if distribution_version is None else distribution_version
        ),
        owner_distribution=owner_distribution,
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
    return RuntimeComponentStatus(
        requirement,
        available=path is not None,
        path=path,
        status=(
            RuntimeComponentState.EXECUTABLE_LOCATED
            if path is not None
            else RuntimeComponentState.ABSENT
        ),
    )


def inspect_runtime_capability(
    capability: str,
    *,
    module_finder: ModuleFinder | None = None,
    distribution_version: DistributionVersion | None = None,
    executable_finder: ExecutableFinder | None = None,
    enabled: bool = True,
    processing_error: str | None = None,
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
        component.unavailable_reason or component.requirement.missing_reason
        for component in components
        if component.requirement.required and not component.available
    )
    missing_optional = tuple(
        component.unavailable_reason or component.requirement.missing_reason
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
        enabled=enabled,
        processing_error=processing_error,
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


# region [04] Per-work implementation readiness

_MAX_CAPABILITY_BINARY_BYTES = 16 * 1024 * 1024
_MAX_ATTESTED_REQUIRED_BINARIES = 2
_BINARY_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_CAPABILITY_DIAGNOSTIC_CHARS = 512


def _bounded_capability_diagnostic(prefix: str, detail: str) -> str:
    value = f"{prefix}:{detail}"
    if (
        value.isascii()
        and all(32 <= ord(character) < 127 for character in value)
        and len(value) <= _MAX_CAPABILITY_DIAGNOSTIC_CHARS
    ):
        return value
    digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()
    if not detail.isascii():
        return f"{prefix}:sha256:{digest}"
    suffix = f":sha256:{digest}"
    available = _MAX_CAPABILITY_DIAGNOSTIC_CHARS - len(prefix) - len(suffix) - 2
    return f"{prefix}:{detail[: max(0, available)]}:{suffix.removeprefix(':')}"


def _bounded_capability_value(value: str) -> str:
    if (
        value.isascii()
        and all(32 <= ord(character) < 127 for character in value)
        and len(value) <= _MAX_CAPABILITY_DIAGNOSTIC_CHARS
    ):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if not value.isascii():
        return f"sha256:{digest}"
    suffix = f":sha256:{digest}"
    return value[: _MAX_CAPABILITY_DIAGNOSTIC_CHARS - len(suffix)] + suffix


def _bounded_capability_evidence(
    values: Iterable[str],
    *,
    category: str,
) -> tuple[str, ...]:
    canonical = tuple(sorted(set(values)))
    if len(canonical) <= MAX_CAPABILITY_EVIDENCE_VALUES:
        return canonical
    digest = hashlib.sha256()
    for value in canonical:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    marker = _bounded_capability_diagnostic(
        f"additional_{category}_truncated",
        digest.hexdigest(),
    )
    return (*canonical[: MAX_CAPABILITY_EVIDENCE_VALUES - 1], marker)


def _bounded_capability_manifests(
    manifests: Iterable[CapabilityManifest],
) -> tuple[CapabilityManifest, ...]:
    selected = tuple(islice(manifests, MAX_CAPABILITY_MANIFESTS + 1))
    if len(selected) > MAX_CAPABILITY_MANIFESTS:
        raise ValueError(f"capability manifests cannot exceed {MAX_CAPABILITY_MANIFESTS}")
    if any(not isinstance(item, CapabilityManifest) for item in selected):
        raise ValueError("capability manifests must contain CapabilityManifest values")
    return selected


def _bounded_capability_statuses(
    statuses: Iterable[RuntimeCapabilityStatus],
) -> tuple[RuntimeCapabilityStatus, ...]:
    selected = tuple(islice(statuses, MAX_CAPABILITY_MANIFESTS + 1))
    if len(selected) > MAX_CAPABILITY_MANIFESTS:
        raise ValueError(f"runtime capability statuses cannot exceed {MAX_CAPABILITY_MANIFESTS}")
    if any(not isinstance(item, RuntimeCapabilityStatus) for item in selected):
        raise ValueError("runtime capability statuses must contain RuntimeCapabilityStatus values")
    return selected


def _find_executable_safely(
    executable: str,
    finder: ExecutableFinder,
) -> str | None:
    try:
        return finder(executable)
    except OSError:
        return None


def _binary_identity(
    executable: str,
    finder: ExecutableFinder,
) -> CapabilityBinaryIdentity | None:
    discovered = _find_executable_safely(executable, finder)
    if discovered is None:
        return None
    try:
        resolved = Path(discovered).expanduser().resolve(strict=True)
        before = resolved.stat()
        if (
            not resolved.is_file()
            or not os.access(resolved, os.X_OK)
            or before.st_size > _MAX_CAPABILITY_BINARY_BYTES
        ):
            return None
        digest = hashlib.sha256()
        hashed_bytes = 0
        with resolved.open("rb") as stream:
            while chunk := stream.read(
                min(
                    _BINARY_HASH_CHUNK_BYTES,
                    _MAX_CAPABILITY_BINARY_BYTES + 1 - hashed_bytes,
                )
            ):
                hashed_bytes += len(chunk)
                if hashed_bytes > _MAX_CAPABILITY_BINARY_BYTES:
                    return None
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            return None
        return CapabilityBinaryIdentity(
            name=executable,
            command=str(resolved),
            command_sha256=hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
            artifact_sha256=digest.hexdigest(),
            size_bytes=before.st_size,
        )
    except (OSError, RuntimeError, ValueError):
        return None


def _available_binary_alternatives(
    manifest: CapabilityManifest,
    request: CapabilityRequest,
    finder: ExecutableFinder,
) -> tuple[tuple[CapabilityBinaryIdentity, ...], str | None]:
    requirement = next(
        (item for item in manifest.mime_binary_alternatives if item.mime_type == request.mime_type),
        None,
    )
    if requirement is None:
        return (), None
    for executable in requirement.alternatives:
        identity = _binary_identity(executable, finder)
        if identity is not None:
            # The declared order is the implementation's explicit execution
            # strategy.  Only this verified artifact may be invoked; the
            # worker must not fall through invisibly to another backend.
            return (identity,), requirement.unavailable_reason
    return (), requirement.unavailable_reason


def inspect_capability_implementation_availability(
    request: CapabilityRequest,
    statuses: Iterable[RuntimeCapabilityStatus],
    *,
    manifests: Iterable[CapabilityManifest] = CAPABILITY_MANIFESTS,
    executable_finder: ExecutableFinder | None = None,
) -> tuple[CapabilityAvailability, ...]:
    """Project aggregate route probes into exact per-work readiness facts."""

    selected_statuses = _bounded_capability_statuses(statuses)
    selected_manifests = _bounded_capability_manifests(manifests)
    matching_manifests = tuple(
        item for item in selected_manifests if item.capability_id == request.capability_id
    )
    if len(matching_manifests) > MAX_CAPABILITY_CANDIDATES:
        return tuple(
            CapabilityAvailability(
                manifest.implementation_id,
                manifest.contract_fingerprint,
                request.execution_contract_fingerprint,
                available=False,
                reasons=("candidate_limit_exceeded_before_readiness",),
            )
            for manifest in matching_manifests
        )
    status_names = tuple(item.capability for item in selected_statuses)
    if len(status_names) != len(set(status_names)):
        raise ValueError("runtime capability statuses cannot be duplicated")
    status_by_name = {item.capability: item for item in selected_statuses}
    find_executable = shutil.which if executable_finder is None else executable_finder
    observations: list[CapabilityAvailability] = []
    for manifest in matching_manifests:
        oversized_evidence = tuple(
            name
            for name, count, maximum in (
                (
                    "required_components",
                    len(manifest.required_components),
                    MAX_CAPABILITY_EVIDENCE_VALUES,
                ),
                (
                    "required_models",
                    len(manifest.required_models),
                    MAX_CAPABILITY_EVIDENCE_VALUES,
                ),
                (
                    "required_binaries",
                    len(manifest.required_binaries),
                    _MAX_ATTESTED_REQUIRED_BINARIES,
                ),
            )
            if count > maximum
        )
        if oversized_evidence:
            observations.append(
                CapabilityAvailability(
                    manifest.implementation_id,
                    manifest.contract_fingerprint,
                    request.execution_contract_fingerprint,
                    available=False,
                    reasons=(
                        _bounded_capability_diagnostic(
                            "manifest_readiness_evidence_limit_exceeded",
                            ",".join(oversized_evidence),
                        ),
                    ),
                )
            )
            continue
        route_name = manifest.capability_id.partition(".")[0]
        status = status_by_name.get(route_name)
        if status is None:
            observations.append(
                CapabilityAvailability(
                    manifest.implementation_id,
                    manifest.contract_fingerprint,
                    request.execution_contract_fingerprint,
                    available=False,
                    reasons=("runtime_capability_status_unknown",),
                )
            )
            continue
        if not status.enabled:
            observations.append(
                CapabilityAvailability(
                    manifest.implementation_id,
                    manifest.contract_fingerprint,
                    request.execution_contract_fingerprint,
                    available=False,
                    reasons=("capability_disabled",),
                )
            )
            continue
        if len(status.components) > MAX_CAPABILITY_EVIDENCE_VALUES:
            observations.append(
                CapabilityAvailability(
                    manifest.implementation_id,
                    manifest.contract_fingerprint,
                    request.execution_contract_fingerprint,
                    available=False,
                    reasons=("runtime_component_evidence_limit_exceeded",),
                )
            )
            continue
        component_names = tuple(item.requirement.component for item in status.components)
        if len(component_names) != len(set(component_names)):
            observations.append(
                CapabilityAvailability(
                    manifest.implementation_id,
                    manifest.contract_fingerprint,
                    request.execution_contract_fingerprint,
                    available=False,
                    reasons=("runtime_component_observations_duplicated",),
                )
            )
            continue
        components = {item.requirement.component: item for item in status.components}
        missing: list[str] = []
        observed: list[str] = []
        binary_identities: list[CapabilityBinaryIdentity] = []
        for component_name in manifest.required_components:
            component = components.get(component_name)
            if component is None:
                missing.append(
                    _bounded_capability_diagnostic(
                        "runtime_component_unknown",
                        component_name,
                    )
                )
            elif component.available:
                observed.append(
                    _bounded_capability_value(
                        component_name
                        if component.version is None
                        else f"{component_name}@{component.version}"
                    )
                )
            else:
                missing.append(
                    _bounded_capability_value(
                        component.unavailable_reason or component.requirement.missing_reason
                    )
                )
        for binary in manifest.required_binaries:
            identity = _binary_identity(binary, find_executable)
            if identity is None:
                missing.append(
                    _bounded_capability_diagnostic(
                        "required_binary_unavailable",
                        binary,
                    )
                )
            else:
                binary_identities.append(identity)
        missing.extend(
            _bounded_capability_diagnostic("required_model_unobserved", model)
            for model in manifest.required_models
        )
        binary_alternatives, unavailable_reason = _available_binary_alternatives(
            manifest,
            request,
            find_executable,
        )
        if unavailable_reason is not None:
            if not binary_alternatives:
                missing.append(_bounded_capability_value(unavailable_reason))
            else:
                binary_identities.extend(binary_alternatives)
        if len(observed) > MAX_CAPABILITY_EVIDENCE_VALUES:
            missing.append("component_evidence_limit_exceeded")
            observed = []
        if len(binary_identities) > MAX_CAPABILITY_EVIDENCE_VALUES:
            missing.append("binary_evidence_limit_exceeded")
            binary_identities = []
        observations.append(
            CapabilityAvailability(
                manifest.implementation_id,
                manifest.contract_fingerprint,
                request.execution_contract_fingerprint,
                available=not missing,
                reasons=_bounded_capability_evidence(
                    missing,
                    category="readiness_reasons",
                ),
                observed_components=_bounded_capability_evidence(
                    observed,
                    category="observed_components",
                ),
                binary_identities=tuple(binary_identities),
            )
        )
    return tuple(sorted(observations, key=lambda item: item.implementation_id))


def build_runtime_capability_broker(
    request: CapabilityRequest,
    *,
    statuses: Iterable[RuntimeCapabilityStatus] | None = None,
    manifests: Iterable[CapabilityManifest] = CAPABILITY_MANIFESTS,
    module_finder: ModuleFinder | None = None,
    distribution_version: DistributionVersion | None = None,
    executable_finder: ExecutableFinder | None = None,
) -> CapabilityBroker:
    """Build a frozen broker snapshot without importing or loading providers."""

    selected_manifests = tuple(
        item
        for item in _bounded_capability_manifests(manifests)
        if item.capability_id == request.capability_id
    )
    if len(selected_manifests) > MAX_CAPABILITY_CANDIDATES:
        return CapabilityBroker(selected_manifests, ())
    if statuses is None:
        routes = tuple(
            dict.fromkeys(item.capability_id.partition(".")[0] for item in selected_manifests)
        )
        selected_statuses = inspect_runtime_capabilities(
            routes,
            module_finder=module_finder,
            distribution_version=distribution_version,
            executable_finder=executable_finder,
        )
    else:
        selected_statuses = _bounded_capability_statuses(statuses)
    availability = inspect_capability_implementation_availability(
        request,
        selected_statuses,
        manifests=selected_manifests,
        executable_finder=executable_finder,
    )
    return CapabilityBroker(selected_manifests, availability)


# endregion [04]


__all__ = (
    "CAPABILITY_MANIFESTS",
    "CAPABILITY_SPECS",
    "ROUTE_CAPABILITY_NAMES",
    "RUNTIME_CAPABILITY_PROBE_POLICY",
    "RUNTIME_CAPABILITY_SCHEMA_VERSION",
    "TEXT_BUILTIN_IMPLEMENTATION_ID",
    "TEXT_EXTRACT_CAPABILITY_ID",
    "TEXT_RAW_INPUT_SCHEMA",
    "TEXT_REPRESENTATION_OUTPUT_SCHEMA",
    "CapabilityAvailability",
    "CapabilityBinaryIdentity",
    "CapabilityBroker",
    "CapabilityLanguageMode",
    "CapabilityLifecycle",
    "CapabilityManifest",
    "CapabilityMimeBinaryAlternatives",
    "CapabilityPolicy",
    "CapabilityPrivacy",
    "CapabilityQualityMetric",
    "CapabilityQualityRequirement",
    "CapabilityRequest",
    "CapabilitySelection",
    "CapabilityState",
    "RequirementKind",
    "RuntimeCapabilitySpec",
    "RuntimeCapabilityStatus",
    "RuntimeComponentState",
    "RuntimeComponentStatus",
    "RuntimeRequirement",
    "build_runtime_capability_broker",
    "inspect_capability_implementation_availability",
    "inspect_python_component",
    "inspect_runtime_capabilities",
    "inspect_runtime_capability",
)
