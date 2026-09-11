"""Incremental extraction for physical text, email, and Office documents."""

from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

import xxhash

from neocortex import __version__ as _NEOCORTEX_DISTRIBUTION_VERSION
from neocortex.capabilities.broker import (
    CapabilityBroker,
    CapabilityPolicy,
    CapabilityPrivacy,
    CapabilityRequest,
    CapabilitySelection,
)
from neocortex.capabilities.runtime import (
    TEXT_BUILTIN_IMPLEMENTATION_ID,
    TEXT_EXTRACT_CAPABILITY_ID,
    TEXT_RAW_INPUT_SCHEMA,
    TEXT_REPRESENTATION_OUTPUT_SCHEMA,
    build_runtime_capability_broker,
    inspect_runtime_capability,
)
from neocortex.platform.policy import physical_identity_scheme_for_birthtime

from neocortex.deduplication import FileChangedError, FileSnapshot
from neocortex.deduplication.fingerprinting import snapshot_path, stat_matches_snapshot
from neocortex.deduplication.io import native_io_path
from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from neocortex.runtime.control.bounded_subprocess import SubprocessOutputLimitError
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.semantic.derivation_contracts import (
    CapabilityFailure,
    InputBinding,
    MaterializationRef,
    OutputBinding,
    ReproducibilityClass,
    StageDescriptor,
    WorkExecutionMode,
    WorkOutcome,
    WorkReceipt,
)
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.knowledge.knowledge_contracts import (
    PhysicalIdentityRef,
    ResourceDisposition,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.foundation.processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    ProcessingProvenance,
    build_processing_provenance,
    python_runtime_component,
)
from neocortex.safety.route_filters import CandidateSelection
from neocortex.capabilities.formats.xml_safety import safe_xml_fromstring
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text
from .text_derivation_repository import (
    TextDerivationAttemptStart,
    TextDerivationIntegrityError,
    TextReusableDerivation,
    abandon_running_text_derivations,
    begin_text_derivation_attempt_from_connection,
    cancel_text_derivation_attempt,
    compute_text_fts_fingerprint,
    compute_text_representation_fingerprint,
    fail_text_derivation_attempt,
    read_reusable_text_derivation_from_connection,
    read_reusable_text_failure_from_connection,
    succeed_text_derivation_attempt,
)
from .text_state import TEXT_SCHEMA_VERSION, initialize_text_state, text_database


TEXT_ROUTE_VERSION = "text-route-v3"
_TEXT_EXTRACT_STAGE_ID = "text.extract"
_TEXT_EXTRACT_STAGE_VERSION = "2"
# Checked-in digest of the normalized Text-owned extractor contract.  It is
# neither a transitive digest of binaries/environment nor a runtime checkout
# hash; the source characterization requires updating it when those symbols
# change, which in turn changes every affected processing signature.
_TEXT_EXTRACTOR_CONTRACT_SHA256 = (
    "sha256:982a71b97ca4f1e5efed9a228874ddc461adda96df7798b7de178739cbf35e37"
)
_TEXT_IMPLEMENTATION_SCHEMA = "neocortex.text-implementation-contract/v1"
_TEXT_DISTRIBUTION_NAME = "neocortex-framework"
_TEXT_IMPLEMENTATION_CONFIGURATION_KEYS = (
    "implementation_digest",
    "implementation_distribution",
    "implementation_distribution_version",
    "implementation_extractor_contract_sha256",
    "implementation_schema",
)
_TEXT_SOURCE_REVISION_PRODUCER = "text.source"
_TEXT_SOURCE_PROCESSING_SIGNATURE = "text-source-revision-v1:xxh3-128"
_TEXT_REPRESENTATION_KIND = "text_representation"
_TEXT_FTS_KIND = "text_fts"
_MAX_DERIVATION_VALUE_CHARS = 4_096
MAX_EMAIL_PARTS = 4_096
MAX_EMAIL_DEPTH = 64
MAX_EMAIL_PART_BYTES = 8 * 1024 * 1024
TEXT_ROUTE_MIMES = (
    "text/plain",
    "text/csv",
    "text/tab-separated-values",
    "text/markdown",
    "text/html",
    "application/xml",
    "application/json",
    "message/rfc822",
)
_TEXT_CAPABILITY_POLICY = CapabilityPolicy(
    policy_id="neocortex-text-local-v1",
    allow_network=False,
    allowed_privacy=(CapabilityPrivacy.LOCAL_ONLY,),
    gpu_available=False,
)


def _text_implementation_configuration(
    *,
    distribution_version: str | None = None,
    extractor_contract_sha256: str | None = None,
) -> dict[str, str]:
    """Return a packaged contractual identity, not a transitive artifact hash."""

    contract = {
        "implementation_distribution": _TEXT_DISTRIBUTION_NAME,
        "implementation_distribution_version": (
            _NEOCORTEX_DISTRIBUTION_VERSION
            if distribution_version is None
            else distribution_version
        ),
        "implementation_extractor_contract_sha256": (
            _TEXT_EXTRACTOR_CONTRACT_SHA256
            if extractor_contract_sha256 is None
            else extractor_contract_sha256
        ),
        "implementation_schema": _TEXT_IMPLEMENTATION_SCHEMA,
    }
    encoded = canonical_json(contract).encode("utf-8")
    return {
        "implementation_digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        **contract,
    }


class TextFrameworkState(Protocol):
    def selected_route_candidate_counts(
        self,
        run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        selection: CandidateSelection,
    ) -> tuple[int, int]: ...

    def iter_selected_route_candidates(
        self,
        run_id: int,
        mime: str,
        route_name: str,
        selection: CandidateSelection,
    ) -> Iterable[FileSnapshot]: ...


@dataclass(frozen=True, slots=True)
class TextRouteConfig:
    state_path: Path
    max_file_bytes: int | None = 64 * 1024 * 1024
    max_documents: int | None = None
    max_text_chars: int = 4_000_000
    worker_timeout_seconds: float = 60.0
    worker_memory_bytes: int = 1024 * 1024 * 1024
    retry_errors: bool = False
    retry_recoverable_errors: bool = field(default=False, kw_only=True)
    selection: CandidateSelection = field(default_factory=CandidateSelection)

    @property
    def processing_provenance(self) -> ProcessingProvenance:
        return build_processing_provenance(
            "text-route",
            f"{TEXT_ROUTE_VERSION}:text.extract/{_TEXT_EXTRACT_STAGE_VERSION}",
            {
                **_text_implementation_configuration(),
                "max_text_chars": self.max_text_chars,
                "worker_timeout_seconds": self.worker_timeout_seconds,
                "worker_memory_bytes": self.worker_memory_bytes,
                "email_policy": "stdlib-default-visible-text-v1",
                "plain_text_decoder": "strict-bom-utf8-cp1252-v1",
            },
            (python_runtime_component(),),
            compatibility_tag=(f"{TEXT_ROUTE_VERSION}-text-extract-v{_TEXT_EXTRACT_STAGE_VERSION}"),
        )

    @property
    def processing_signature(self) -> str:
        return self.processing_provenance.signature


@dataclass(frozen=True, slots=True)
class TextRouteSummary:
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    extracted: int = 0
    plain_text: int = 0
    emails: int = 0
    text_chars: int = 0
    truncated: int = 0
    errors: int = 0
    retryable_errors: int = 0
    cache_documents_pruned: int = 0
    catalog_candidates: int = 0
    catalog_classified: int = 0
    catalog_cache_hits: int = 0
    catalog_review_required: int = 0
    catalog_errors: int = 0
    catalog_source_stale: int = 0
    catalog_stale_marked: int = 0
    peak_reserved_bytes: int = 0
    memory_waits: int = 0
    processing_signature: str | None = None
    effective_processing_signatures: tuple[str, ...] = ()
    processing_provenance: dict[str, Any] | None = None
    summary_schema: str = ROUTE_SUMMARY_SCHEMA
    catalog_source_missing: int = field(default=0, kw_only=True)
    catalog_complete: bool | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class _ExtractedText:
    text: str
    content_kind: str
    media_type: str
    title: str | None = None
    author: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    truncated: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _TextDerivationWork:
    attempt_id: str
    resource: ResourceRef
    revision: RevisionRef
    input_binding: InputBinding
    stage: StageDescriptor
    capability_selection: CapabilitySelection
    started_monotonic_ns: int

    def receipt_id(self, outcome: WorkOutcome) -> str:
        return _stable_identifier(
            f"receipt:text:{outcome.value}",
            {"attempt_id": self.attempt_id, "outcome": outcome.value},
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _stable_identifier(prefix: str, payload: dict[str, object]) -> str:
    return f"{prefix}:{fingerprint_text(canonical_json(payload)).xxh3_128}"


def _resource_ref(snapshot: FileSnapshot) -> ResourceRef:
    physical_value = f"{snapshot.volume_id}:{snapshot.file_id}:{snapshot.birthtime_ns}"
    return ResourceRef(
        resource_id=f"resource:file:{physical_value}",
        source_kind="text",
        owner="text",
        physical_identity=PhysicalIdentityRef(
            physical_identity_scheme_for_birthtime(snapshot.birthtime_ns),
            physical_value,
            1,
        ),
        current_path=snapshot.path,
        disposition=ResourceDisposition.CANONICAL,
    )


def _input_binding(
    snapshot: FileSnapshot,
    payload: bytes,
) -> tuple[ResourceRef, RevisionRef, InputBinding]:
    resource = _resource_ref(snapshot)
    raw_xxh3_128 = xxhash.xxh3_128_hexdigest(payload)
    revision_id = _stable_identifier(
        "revision:text",
        {
            "byte_count": len(payload),
            "fingerprint_algorithm": "xxh3-128",
            "raw_xxh3_128": raw_xxh3_128,
            "resource_id": resource.resource_id,
        },
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id=revision_id,
        producer=_TEXT_SOURCE_REVISION_PRODUCER,
        processing_signature=_TEXT_SOURCE_PROCESSING_SIGNATURE,
        generation=None,
        state=RevisionState.CURRENT,
    )
    return (
        resource,
        revision,
        InputBinding(
            name="source",
            revision=revision,
            fingerprint=raw_xxh3_128,
            fingerprint_algorithm="xxh3-128",
        ),
    )


def _partial_input_binding(
    snapshot: FileSnapshot,
) -> tuple[ResourceRef, RevisionRef, InputBinding]:
    """Identify an unreadable observation without inventing a raw content hash."""

    resource = _resource_ref(snapshot)
    snapshot_fingerprint = fingerprint_text(
        canonical_json(
            {
                "birthtime_ns": snapshot.birthtime_ns,
                "file_id": snapshot.file_id,
                "mtime_ns": snapshot.mtime_ns,
                "size": snapshot.size,
                "volume_id": snapshot.volume_id,
            }
        )
    ).xxh3_128
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id=_stable_identifier(
            "revision:text:partial",
            {
                "resource_id": resource.resource_id,
                "snapshot_fingerprint": snapshot_fingerprint,
            },
        ),
        producer=_TEXT_SOURCE_REVISION_PRODUCER,
        processing_signature="text-source-revision-partial-v1",
        generation=None,
        state=RevisionState.PARTIAL,
    )
    return (
        resource,
        revision,
        InputBinding(
            name="source_observation",
            revision=revision,
            fingerprint=snapshot_fingerprint,
            fingerprint_algorithm="text-snapshot-v1",
        ),
    )


class TextCapabilityUnavailableError(RuntimeError):
    """No declared Text provider satisfies the exact workload and local policy."""

    capability_unavailable = True

    def __init__(self, selection: CapabilitySelection) -> None:
        self.selection = selection
        super().__init__("Text extraction capability is unavailable for this workload")


def _stage_descriptor(provenance: ProcessingProvenance) -> StageDescriptor:
    configuration = provenance.manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("Text processing provenance has no canonical configuration")
    provider = configuration.get("capability_provider")
    provider_version = configuration.get("capability_provider_version")
    manifest_fingerprint = configuration.get("capability_manifest_fingerprint")
    implementation_digest = configuration.get("implementation_digest")
    for name, value in (
        ("capability_provider", provider),
        ("capability_provider_version", provider_version),
        ("capability_manifest_fingerprint", manifest_fingerprint),
        ("implementation_digest", implementation_digest),
    ):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Text {name} must be a string or null")
    if implementation_digest is None:
        raise ValueError("Text processing provenance has no implementation digest")
    return StageDescriptor(
        stage_id=_TEXT_EXTRACT_STAGE_ID,
        stage_version=_TEXT_EXTRACT_STAGE_VERSION,
        processing_signature=provenance.signature,
        implementation_digest=implementation_digest,
        provider=provider,
        provider_version=provider_version,
    )


def _derivation_configuration(
    provenance: ProcessingProvenance,
) -> tuple[tuple[str, str | int | float | bool | None], ...]:
    configuration = provenance.manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("Text processing provenance has no canonical configuration")
    values: list[tuple[str, str | int | float | bool | None]] = []
    for key, value in sorted(configuration.items()):
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError("Text effective configuration must contain JSON scalars")
        values.append((key, value))
    return tuple(values)


def _derivation_runtime(provenance: ProcessingProvenance) -> tuple[tuple[str, str], ...]:
    components = provenance.manifest.get("components")
    if not isinstance(components, list):
        raise ValueError("Text processing provenance has no canonical runtime components")
    values: list[tuple[str, str]] = []
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("Text runtime component must be an object")
        name = component.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Text runtime component has no name")
        values.append((name, canonical_json(component)))
    return tuple(sorted(values))


def _extractor_selector(mime: str, path: str) -> tuple[str, str]:
    suffix = Path(path).suffix.casefold().removeprefix(".")
    if mime == "message/rfc822":
        return "stdlib_email_visible_text", "email"
    if mime == "text/html":
        return "strict_text_decode+html_visible_text", "html"
    if mime == "application/xml":
        return "strict_text_decode+xml_itertext", "xml"
    content_kind = {
        "text/csv": "csv",
        "text/tab-separated-values": "tsv",
        "text/markdown": "markdown",
        "application/json": "json",
    }.get(mime, suffix or "text")
    return "strict_text_decode", content_kind


def _runtime_platform() -> str:
    return {"win32": "windows", "linux": "linux"}.get(sys.platform, sys.platform)


def _text_capability_request(mime: str, input_bytes: int) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=TEXT_EXTRACT_CAPABILITY_ID,
        modality="document",
        input_schema=TEXT_RAW_INPUT_SCHEMA,
        output_schema=TEXT_REPRESENTATION_OUTPUT_SCHEMA,
        platform=_runtime_platform(),
        mime_type=mime,
        language="unknown",
        input_bytes=input_bytes,
        workspace_id="text-owner",
        acceptable_reproducibility=(ReproducibilityClass.ENVIRONMENT_BOUND.value,),
        require_incremental=True,
    )


def _bounded_capability_text(value: str) -> str:
    if len(value) <= _MAX_DERIVATION_VALUE_CHARS:
        return value
    digest = fingerprint_text(value).xxh3_128
    suffix = f"...[truncated;xxh3-128={digest}]"
    return value[: _MAX_DERIVATION_VALUE_CHARS - len(suffix)] + suffix


def _selected_availability(selection: CapabilitySelection) -> str:
    selected = selection.selected
    if selected is None:
        return "|".join(selection.explanation)
    evaluation = next(
        item
        for item in selection.candidates
        if item.implementation_id == selected.implementation_id
    )
    availability = evaluation.availability
    if availability is None:
        return "runtime_availability_unknown"
    evidence = [*availability.observed_components]
    evidence.extend(
        f"binary:{item.name}@sha256:{item.artifact_sha256}"
        for item in availability.binary_identities
    )
    return _bounded_capability_text(",".join(evidence) or "available")


def _selection_allows_reuse(selection: CapabilitySelection) -> bool:
    selected = selection.selected
    return selected is not None and (
        ReproducibilityClass.NON_REPLAYABLE.value not in selected.reproducibility_classes
    )


def _record_extracted_counters(
    counters: dict[str, int],
    extracted: _ExtractedText,
) -> None:
    counters["text_chars"] += len(extracted.text)
    counters["truncated"] += int(extracted.truncated)
    if extracted.content_kind == "email":
        counters["emails"] += 1
    else:
        counters["plain_text"] += 1


def _work_reproducibility(selection: CapabilitySelection) -> ReproducibilityClass:
    return (
        ReproducibilityClass.NON_REPLAYABLE
        if selection.selected is not None
        and ReproducibilityClass.NON_REPLAYABLE.value in selection.selected.reproducibility_classes
        else ReproducibilityClass.ENVIRONMENT_BOUND
    )


def _capability_rejections(selection: CapabilitySelection) -> str:
    return _bounded_capability_text(
        ";".join(
            f"{item.implementation_id}={','.join(item.rejection_reasons) or 'eligible'}"
            for item in selection.candidates
        )
    )


def _candidate_processing_provenance(
    base: ProcessingProvenance,
    mime: str,
    path: str,
    selection: CapabilitySelection,
) -> ProcessingProvenance:
    manifest = base.manifest
    configuration = manifest.get("configuration")
    components = manifest.get("components")
    if not isinstance(configuration, dict) or not isinstance(components, list):
        raise ValueError("Text base provenance is malformed")
    adapter, content_kind = _extractor_selector(mime, path)
    selected = selection.selected
    if selected is not None:
        if selected.implementation_id != TEXT_BUILTIN_IMPLEMENTATION_ID:
            raise ValueError("Text capability selection conflicts with extractor adapter")
    effective_configuration: dict[str, object] = {
        "capability_id": TEXT_EXTRACT_CAPABILITY_ID,
        "capability_implementation": (None if selected is None else selected.implementation_id),
        "capability_manifest_fingerprint": (
            None if selected is None else selected.contract_fingerprint
        ),
        "capability_policy": selection.policy.policy_id,
        "capability_policy_fingerprint": selection.policy.contract_fingerprint,
        "capability_provider": None if selected is None else selected.provider,
        "capability_provider_version": (None if selected is None else selected.provider_version),
        "capability_readiness": _selected_availability(selection),
        "capability_selection": selection.explanation[0],
        "capability_selection_fingerprint": selection.execution_fingerprint,
        "max_text_chars": configuration["max_text_chars"],
        "declared_mime": mime,
        "extractor_adapter": adapter,
        "output_content_kind": content_kind,
    }
    effective_configuration.update(
        {key: configuration[key] for key in _TEXT_IMPLEMENTATION_CONFIGURATION_KEYS}
    )
    effective_configuration["plain_text_decoder"] = configuration["plain_text_decoder"]
    if mime == "message/rfc822":
        effective_configuration["email_policy"] = configuration["email_policy"]
    effective_components: list[dict[str, object]] = [
        component
        for component in components
        if not isinstance(component, dict) or component.get("name") != "soffice"
    ]
    return build_processing_provenance(
        "text-route",
        f"{TEXT_ROUTE_VERSION}:text.extract/{_TEXT_EXTRACT_STAGE_VERSION}",
        effective_configuration,
        effective_components,
        compatibility_tag=(f"{TEXT_ROUTE_VERSION}-text-extract-v{_TEXT_EXTRACT_STAGE_VERSION}"),
    )


def _representation_fingerprint(extracted: _ExtractedText) -> str:
    return compute_text_representation_fingerprint(
        text=extracted.text,
        content_kind=extracted.content_kind,
        media_type=extracted.media_type,
        title=extracted.title,
        author=extracted.author,
        metadata=extracted.metadata,
        truncated=extracted.truncated,
        detail=extracted.detail,
    )


def _fts_fingerprint(file_key: str, extracted: _ExtractedText) -> str:
    return compute_text_fts_fingerprint(
        file_key,
        text=extracted.text,
        content_kind=extracted.content_kind,
        title=extracted.title,
        author=extracted.author,
    )


def _redacted_failure_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: diagnostic detail redacted by Text owner policy."


def _failure_retry_evidence(exc: BaseException) -> tuple[bool, str]:
    """Classify only typed source failures for the bounded retry policy."""

    retryable = isinstance(exc, (FileChangedError, OSError))
    return retryable, "retry" if retryable else "manual_review"


def _output_bindings(
    work: _TextDerivationWork,
    file_key: str,
    extracted: _ExtractedText,
    generation: int | None,
) -> tuple[OutputBinding, OutputBinding]:
    def output(name: str, kind: str, fingerprint: str) -> OutputBinding:
        materialization_id = _stable_identifier(
            "materialization:text",
            {"attempt_id": work.attempt_id, "output_kind": kind},
        )
        return OutputBinding(
            name=name,
            materialization=MaterializationRef(
                owner="text",
                kind=kind,
                materialization_id=materialization_id,
                schema_version=TEXT_SCHEMA_VERSION,
                resource=work.resource,
                revision=work.revision,
                generation=generation,
            ),
            fingerprint=fingerprint,
            fingerprint_algorithm="xxh3-128",
        )

    return (
        output(
            "text_representation",
            _TEXT_REPRESENTATION_KIND,
            _representation_fingerprint(extracted),
        ),
        output(
            "text_fts",
            _TEXT_FTS_KIND,
            _fts_fingerprint(file_key, extracted),
        ),
    )


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _decode_text(payload: bytes) -> tuple[str, str]:
    encodings = (
        ("utf-32",)
        if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"))
        else ("utf-16",)
        if payload.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ("utf-8-sig", "cp1252")
    )
    for encoding in encodings:
        try:
            return payload.decode(encoding, "strict"), encoding
        except UnicodeError:
            continue
    raise UnicodeError("text payload cannot be decoded safely")


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized[:limit], len(normalized) > limit


def _visible_html(value: str) -> str:
    parser = _VisibleHTML()
    parser.feed(value)
    parser.close()
    return "\n".join(parser.parts)


def _email_text(payload: bytes, limit: int) -> _ExtractedText:
    message = BytesParser(policy=policy.default).parsebytes(payload)
    parts: list[str] = []
    text_chars = 0
    truncated = False
    pending: list[tuple[Any, int]] = [(message, 0)]
    visited = 0
    while pending:
        part, depth = pending.pop()
        visited += 1
        if visited > MAX_EMAIL_PARTS:
            truncated = True
            break
        if depth > MAX_EMAIL_DEPTH:
            raise ValueError(f"email MIME depth exceeds {MAX_EMAIL_DEPTH}")
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            if part.is_multipart():
                children = part.get_payload()
                if isinstance(children, list):
                    pending.extend((child, depth + 1) for child in reversed(children))
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in {"text/plain", "text/html"}:
            continue
        encoded_payload = part.get_payload()
        if isinstance(encoded_payload, str) and len(encoded_payload) > MAX_EMAIL_PART_BYTES * 2:
            truncated = True
            break
        raw_payload = part.get_payload(decode=True)
        if isinstance(raw_payload, bytes) and len(raw_payload) > MAX_EMAIL_PART_BYTES:
            truncated = True
            break
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            raw = part.get_payload(decode=True)
            if not isinstance(raw, bytes):
                continue
            content, _encoding = _decode_text(raw)
        if not isinstance(content, str):
            continue
        visible = _visible_html(content) if content_type == "text/html" else content
        remaining = limit - text_chars - (1 if parts else 0)
        if remaining <= 0:
            truncated = True
            break
        if len(visible) > remaining:
            visible = visible[:remaining]
            truncated = True
        if parts:
            text_chars += 1
        parts.append(visible)
        text_chars += len(visible)
        if truncated:
            break
    text = "\n".join(parts)
    metadata: dict[str, object] = {
        key: str(message.get(key, ""))[:4096]
        for key in ("date", "from", "to", "cc", "message-id")
        if message.get(key)
    }
    return _ExtractedText(
        text=text,
        content_kind="email",
        media_type="message/rfc822",
        title=(str(message.get("subject"))[:1024] if message.get("subject") else None),
        author=(str(message.get("from"))[:1024] if message.get("from") else None),
        metadata=metadata,
        truncated=truncated,
        detail="stdlib_email_visible_text",
    )


def _extract(
    payload: bytes,
    mime: str,
    path: str,
    config: TextRouteConfig,
    selection: CapabilitySelection,
) -> _ExtractedText:
    selected = selection.selected
    if selected is None:
        raise TextCapabilityUnavailableError(selection)
    if selected.implementation_id != TEXT_BUILTIN_IMPLEMENTATION_ID:
        raise RuntimeError("Text capability selection changed before execution")
    suffix = Path(path).suffix.casefold()
    if mime == "message/rfc822":
        return _email_text(payload, config.max_text_chars)
    value, encoding = _decode_text(payload)
    if mime == "text/html":
        value = _visible_html(value)
    elif mime == "application/xml":
        root = safe_xml_fromstring(value)
        value = "\n".join(part.strip() for part in root.itertext() if part.strip())
    text, truncated = _bounded(value, config.max_text_chars)
    kind = {
        "text/csv": "csv",
        "text/tab-separated-values": "tsv",
        "text/markdown": "markdown",
        "text/html": "html",
        "application/xml": "xml",
        "application/json": "json",
    }.get(mime, suffix.removeprefix(".") or "text")
    return _ExtractedText(
        text=text,
        content_kind=kind,
        media_type=mime,
        metadata={"encoding": encoding},
        truncated=truncated,
        detail=f"encoding={encoding}",
    )


def _read_exact(snapshot: FileSnapshot, limit: int, cancellation: CancellationToken) -> bytes:
    path = native_io_path(snapshot.path)
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise FileChangedError("refusing non-regular or linked text source")
    if snapshot.size > limit:
        raise ValueError("text source exceeds configured size limit")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            descriptor = -1
            before = os.fstat(stream.fileno())
            if not stat_matches_snapshot(snapshot, before):
                raise FileChangedError("text source changed before reading")
            chunks: list[bytes] = []
            remaining = snapshot.size
            while remaining:
                cancellation.checkpoint()
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise FileChangedError("unexpected end of text source")
                chunks.append(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise FileChangedError("text source grew while reading")
            if not stat_matches_snapshot(snapshot, os.fstat(stream.fileno())):
                raise FileChangedError("text source changed while reading")
            return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class TextRoute:
    route_name = "text"

    def __init__(
        self,
        config: TextRouteConfig,
        framework_state: TextFrameworkState,
        run_id: int,
        *,
        progress: ProgressCallback | None = None,
        memory_gate=None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self.config = config
        self.framework_state = framework_state
        self.run_id = run_id
        self.progress = progress
        self.memory_gate = memory_gate
        self.cancellation = cancellation or CancellationToken()
        self._recoverable_retry_keys: set[str] = set()

    def _validate(self) -> None:
        if self.config.max_file_bytes is not None and self.config.max_file_bytes < 1:
            raise ValueError("text max_file_bytes must be positive")
        if self.config.max_documents is not None and self.config.max_documents < 1:
            raise ValueError("text max_documents must be positive")
        if self.config.max_text_chars < 1:
            raise ValueError("text max_text_chars must be positive")
        if self.config.worker_timeout_seconds <= 0 or self.config.worker_memory_bytes < 1:
            raise ValueError("text worker limits must be positive")
        if not isinstance(self.config.retry_recoverable_errors, bool):
            raise ValueError("text retry_recoverable_errors must be a boolean")

    def _counts(self) -> tuple[int, int, int]:
        pool = eligible = 0
        for mime in TEXT_ROUTE_MIMES:
            mime_pool, mime_eligible = self.framework_state.selected_route_candidate_counts(
                self.run_id,
                mime,
                self.config.max_file_bytes,
                self.route_name,
                self.config.selection,
            )
            pool += mime_pool
            eligible += mime_eligible
        selected = (
            eligible
            if self.config.max_documents is None
            else min(eligible, self.config.max_documents)
        )
        return pool, eligible, selected

    def _candidates(self):
        yielded = 0
        for mime in TEXT_ROUTE_MIMES:
            for snapshot in self.framework_state.iter_selected_route_candidates(
                self.run_id,
                mime,
                self.route_name,
                self.config.selection,
            ):
                if (
                    self.config.max_file_bytes is not None
                    and snapshot.size > self.config.max_file_bytes
                ):
                    continue
                if self.config.max_documents is not None and yielded >= self.config.max_documents:
                    return
                yielded += 1
                yield mime, snapshot

    def _admission(self, snapshot: FileSnapshot):
        if self.memory_gate is None:
            return nullcontext()
        return self.memory_gate.admit(
            max(4 * 1024 * 1024, snapshot.size * 3 + self.config.max_text_chars * 4)
        )

    def _cached_extracted(
        self,
        connection: sqlite3.Connection,
        file_key: str,
        revision: RevisionRef,
        signature: str,
    ) -> _ExtractedText | None:
        row = connection.execute(
            "SELECT * FROM documents WHERE file_key=?",
            (file_key,),
        ).fetchone()
        if (
            row is None
            or str(row["status"]) != "complete"
            or str(row["processing_signature"]) != signature
            or row["revision_id"] is None
            or str(row["revision_id"]) != revision.revision_id
            or row["text_zlib"] is None
            or row["text_xxh3_128"] is None
        ):
            return None
        fts_rows = connection.execute(
            "SELECT file_key,path,content_kind,title,author,body "
            "FROM document_fts WHERE file_key=?",
            (file_key,),
        ).fetchall()
        if len(fts_rows) != 1:
            return None
        try:
            text = zlib.decompress(bytes(row["text_zlib"])).decode("utf-8", "strict")
            metadata = json.loads(str(row["metadata_json"]))
        except (TypeError, UnicodeError, ValueError, zlib.error):
            return None
        if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
            return None
        extracted = _ExtractedText(
            text=text,
            content_kind=str(row["content_kind"]),
            media_type=str(row["media_type"]),
            title=None if row["title"] is None else str(row["title"]),
            author=None if row["author"] is None else str(row["author"]),
            metadata=metadata,
            truncated=bool(row["text_truncated"]),
            detail=None if row["detail"] is None else str(row["detail"]),
        )
        encoded = text.encode("utf-8")
        fts = fts_rows[0]
        physical_output_matches = (
            int(row["text_chars"]) == len(text)
            and str(row["text_xxh3_128"]) == xxhash.xxh3_128_hexdigest(encoded)
            and str(fts["file_key"]) == file_key
            and str(fts["content_kind"]) == extracted.content_kind
            and str(fts["title"]) == (extracted.title or "")
            and str(fts["author"]) == (extracted.author or "")
            and str(fts["body"]) == extracted.text
        )
        return extracted if physical_output_matches else None

    def _reusable_derivation(
        self,
        connection: sqlite3.Connection,
        file_key: str,
        resource: ResourceRef,
        revision: RevisionRef,
        signature: str,
    ) -> tuple[TextReusableDerivation, _ExtractedText] | None:
        try:
            reusable = read_reusable_text_derivation_from_connection(
                connection,
                file_key,
                stage_id=_TEXT_EXTRACT_STAGE_ID,
                processing_signature=signature,
            )
        except TextDerivationIntegrityError:
            return None
        if reusable is None or reusable.revision.revision_id != revision.revision_id:
            return None
        extracted = self._cached_extracted(connection, file_key, revision, signature)
        if extracted is None:
            return None
        expected = {
            "text_representation": (
                _TEXT_REPRESENTATION_KIND,
                _representation_fingerprint(extracted),
            ),
            "text_fts": (_TEXT_FTS_KIND, _fts_fingerprint(file_key, extracted)),
        }
        if len(reusable.outputs) != len(expected):
            return None
        for output in reusable.outputs:
            contract = expected.get(output.name)
            materialization = output.materialization
            if (
                contract is None
                or output.fingerprint_algorithm != "xxh3-128"
                or output.fingerprint != contract[1]
                or materialization.owner != "text"
                or materialization.kind != contract[0]
                or materialization.schema_version != TEXT_SCHEMA_VERSION
                or materialization.resource is None
                or materialization.resource.resource_id != resource.resource_id
                or materialization.revision is None
                or materialization.revision.revision_id != revision.revision_id
            ):
                return None
        return reusable, extracted

    def _refresh_cached_document(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        file_key: str,
        signature: str,
    ) -> None:
        conflict = connection.execute(
            "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
            (snapshot.path, file_key),
        ).fetchone()
        if conflict is not None:
            self._delete_document(connection, str(conflict["file_key"]))
        updated = connection.execute(
            "UPDATE documents SET path=?,size=?,mtime_ns=?,birthtime_ns=?,"
            "processing_signature=?,last_seen_run_id=?,updated_ns=? WHERE file_key=?",
            (
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                signature,
                self.run_id,
                time.time_ns(),
                file_key,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("cached Text document disappeared before publication")
        fts = connection.execute(
            "UPDATE document_fts SET path=? WHERE file_key=?",
            (snapshot.path, file_key),
        )
        if fts.rowcount != 1:
            raise RuntimeError("cached Text FTS output disappeared before publication")

    def _refresh_cached_error(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        file_key: str,
        signature: str,
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            conflict = connection.execute(
                "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
                (snapshot.path, file_key),
            ).fetchone()
            if conflict is not None:
                self._delete_document(connection, str(conflict["file_key"]))
            updated = connection.execute(
                """UPDATE documents SET path=?,size=?,mtime_ns=?,birthtime_ns=?,
                processing_signature=?,last_seen_run_id=?,updated_ns=?
                WHERE file_key=? AND status='error'""",
                (
                    snapshot.path,
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    signature,
                    self.run_id,
                    time.time_ns(),
                    file_key,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("cached Text error disappeared before refresh")
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def _cached_failure_has_retry_evidence(
        connection: sqlite3.Connection,
        file_key: str,
        receipt_id: str,
    ) -> bool:
        """Require both durable retryability and an explicit retry recommendation."""

        document = connection.execute(
            "SELECT retryable FROM documents WHERE file_key=? AND status='error'",
            (file_key,),
        ).fetchone()
        if document is None or type(document["retryable"]) is not int:
            return False
        if document["retryable"] != 1:
            return False
        receipt_row = connection.execute(
            "SELECT receipt_json FROM text_work_receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if receipt_row is None:
            return False
        try:
            receipt = WorkReceipt.from_json(str(receipt_row["receipt_json"]))
        except (TypeError, ValueError):
            return False
        failure = receipt.failure
        return (
            receipt.outcome is WorkOutcome.FAILED
            and failure is not None
            and failure.retryable is True
            and dict(failure.details).get("recommendation") == "retry"
        )

    def _claim_recoverable_retry(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        receipt_id: str,
    ) -> bool:
        """Claim at most one typed automatic retry for one file in this run."""

        if not self.config.retry_recoverable_errors:
            return False
        file_key = file_key_from_snapshot(snapshot)
        if file_key in self._recoverable_retry_keys:
            return False
        if not self._cached_failure_has_retry_evidence(connection, file_key, receipt_id):
            return False
        self._recoverable_retry_keys.add(file_key)
        return True

    @staticmethod
    def _delete_document(connection: sqlite3.Connection, key: str) -> None:
        connection.execute(
            "DELETE FROM text_materialization_heads WHERE materialization_owner='text' "
            "AND materialization_kind IN (?,?) AND resource_id IN ("
            "SELECT r.resource_id FROM documents d JOIN text_input_revisions r "
            "ON r.revision_id=d.revision_id WHERE d.file_key=?)",
            (_TEXT_REPRESENTATION_KIND, _TEXT_FTS_KIND, key),
        )
        connection.execute("DELETE FROM document_fts WHERE file_key=?", (key,))
        connection.execute("DELETE FROM documents WHERE file_key=?", (key,))

    def _prune_stale_documents(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COUNT(*) FROM documents WHERE last_seen_run_id<>?",
            (self.run_id,),
        ).fetchone()
        stale = int(row[0])
        if stale == 0:
            return 0
        connection.execute(
            "DELETE FROM text_materialization_heads WHERE materialization_owner='text' "
            "AND materialization_kind IN (?,?) AND resource_id IN ("
            "SELECT r.resource_id FROM documents d JOIN text_input_revisions r "
            "ON r.revision_id=d.revision_id WHERE d.last_seen_run_id<>?)",
            (_TEXT_REPRESENTATION_KIND, _TEXT_FTS_KIND, self.run_id),
        )
        connection.execute(
            "DELETE FROM document_fts WHERE file_key IN "
            "(SELECT file_key FROM documents WHERE last_seen_run_id<>?)",
            (self.run_id,),
        )
        connection.execute(
            "DELETE FROM documents WHERE last_seen_run_id<>?",
            (self.run_id,),
        )
        return stale

    def _store_success(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        extracted: _ExtractedText,
        signature: str,
    ) -> None:
        key = file_key_from_snapshot(snapshot)
        conflict = connection.execute(
            "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
            (snapshot.path, key),
        ).fetchone()
        if conflict is not None:
            self._delete_document(connection, str(conflict["file_key"]))
        self._delete_document(connection, key)
        encoded = extracted.text.encode("utf-8")
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,title,author,metadata_json,text_zlib,text_chars,
            text_xxh3_128,text_truncated,detail,error_type,error_message,retryable,
            last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,?,?)""",
            (
                key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                signature,
                extracted.content_kind,
                extracted.media_type,
                extracted.title,
                extracted.author,
                json.dumps(extracted.metadata, ensure_ascii=False, sort_keys=True),
                zlib.compress(encoded, 6),
                len(extracted.text),
                xxhash.xxh3_128_hexdigest(encoded),
                int(extracted.truncated),
                extracted.detail,
                self.run_id,
                time.time_ns(),
            ),
        )
        connection.execute(
            "INSERT INTO document_fts(file_key,path,content_kind,title,author,body) "
            "VALUES(?,?,?,?,?,?)",
            (
                key,
                snapshot.path,
                extracted.content_kind,
                extracted.title or "",
                extracted.author or "",
                extracted.text,
            ),
        )

    def _store_error(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        mime: str,
        signature: str,
        exc: BaseException,
    ) -> bool:
        key = file_key_from_snapshot(snapshot)
        conflict = connection.execute(
            "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
            (snapshot.path, key),
        ).fetchone()
        if conflict is not None:
            self._delete_document(connection, str(conflict["file_key"]))
        self._delete_document(connection, key)
        retryable, _ = _failure_retry_evidence(exc)
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,metadata_json,text_chars,text_truncated,
            error_type,error_message,retryable,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,'error',?,?,'{}',0,0,?,?,?,?,?)""",
            (
                key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                signature,
                Path(snapshot.path).suffix.casefold().removeprefix(".") or "text",
                mime,
                type(exc).__name__,
                _redacted_failure_message(exc),
                int(retryable),
                self.run_id,
                time.time_ns(),
            ),
        )
        return retryable

    def _begin_derivation(
        self,
        connection: sqlite3.Connection,
        provenance: ProcessingProvenance,
        resource: ResourceRef,
        revision: RevisionRef,
        input_binding: InputBinding,
        capability_selection: CapabilitySelection,
        *,
        causation_id: str | None,
    ) -> _TextDerivationWork:
        stage = _stage_descriptor(provenance)
        recorded_ns = time.time_ns()
        started_monotonic_ns = time.monotonic_ns()
        correlation_id = _stable_identifier(
            "correlation:text",
            {
                "resource_id": resource.resource_id,
                "stage_id": stage.stage_id,
            },
        )
        attempt_number = int(
            connection.execute(
                "SELECT COALESCE(MAX(attempt_number),0)+1 "
                "FROM text_derivation_attempts WHERE correlation_id=?",
                (correlation_id,),
            ).fetchone()[0]
        )
        attempt_id = _stable_identifier(
            "attempt:text",
            {
                "correlation_id": correlation_id,
                "recorded_ns": recorded_ns,
                "run_id": self.run_id,
                "started_monotonic_ns": started_monotonic_ns,
                "stage_version": stage.stage_version,
            },
        )
        begin_text_derivation_attempt_from_connection(
            connection,
            TextDerivationAttemptStart(
                attempt_id=attempt_id,
                stage=stage,
                inputs=(input_binding,),
                effective_configuration=_derivation_configuration(provenance),
                runtime=_derivation_runtime(provenance),
                started_at_utc=_utc_now(),
                started_monotonic_ns=started_monotonic_ns,
                attempt=attempt_number,
                run_id=f"framework:{self.run_id}",
                correlation_id=correlation_id,
                recorded_ns=recorded_ns,
                causation_id=causation_id,
            ),
        )
        return _TextDerivationWork(
            attempt_id=attempt_id,
            resource=resource,
            revision=revision,
            input_binding=input_binding,
            stage=stage,
            capability_selection=capability_selection,
            started_monotonic_ns=started_monotonic_ns,
        )

    @staticmethod
    def _duration_ns(work: _TextDerivationWork) -> int:
        return max(0, time.monotonic_ns() - work.started_monotonic_ns)

    def _publish_cache_hit(
        self,
        connection: sqlite3.Connection,
        work: _TextDerivationWork,
        snapshot: FileSnapshot,
        reusable: TextReusableDerivation,
    ) -> None:
        file_key = file_key_from_snapshot(snapshot)
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._refresh_cached_document(
                connection,
                snapshot,
                file_key,
                work.stage.processing_signature,
            )
            succeed_text_derivation_attempt(
                connection,
                work.attempt_id,
                receipt_id=work.receipt_id(WorkOutcome.SUCCEEDED),
                outputs=reusable.outputs,
                finished_at_utc=_utc_now(),
                duration_ns=self._duration_ns(work),
                execution_mode=WorkExecutionMode.CACHE_HIT,
                reproducibility=_work_reproducibility(work.capability_selection),
                document_file_key=file_key,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _publish_success(
        self,
        connection: sqlite3.Connection,
        work: _TextDerivationWork,
        snapshot: FileSnapshot,
        extracted: _ExtractedText,
    ) -> None:
        file_key = file_key_from_snapshot(snapshot)
        generation = self.run_id if self.run_id >= 0 else None
        outputs = _output_bindings(work, file_key, extracted, generation)
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._store_success(
                connection,
                snapshot,
                extracted,
                work.stage.processing_signature,
            )
            succeed_text_derivation_attempt(
                connection,
                work.attempt_id,
                receipt_id=work.receipt_id(WorkOutcome.SUCCEEDED),
                outputs=outputs,
                finished_at_utc=_utc_now(),
                duration_ns=self._duration_ns(work),
                execution_mode=WorkExecutionMode.EXECUTED,
                reproducibility=_work_reproducibility(work.capability_selection),
                document_file_key=file_key,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _publish_error(
        self,
        connection: sqlite3.Connection,
        work: _TextDerivationWork,
        snapshot: FileSnapshot,
        mime: str,
        exc: BaseException,
    ) -> bool:
        file_key = file_key_from_snapshot(snapshot)
        connection.execute("BEGIN IMMEDIATE")
        try:
            retryable = self._store_error(
                connection,
                snapshot,
                mime,
                work.stage.processing_signature,
                exc,
            )
            recommendation = "retry" if retryable else "manual_review"
            fail_text_derivation_attempt(
                connection,
                work.attempt_id,
                receipt_id=work.receipt_id(WorkOutcome.FAILED),
                finished_at_utc=_utc_now(),
                duration_ns=self._duration_ns(work),
                reproducibility=_work_reproducibility(work.capability_selection),
                failure=CapabilityFailure(
                    capability_id=_TEXT_EXTRACT_STAGE_ID,
                    reason_code=type(exc).__name__,
                    message=_redacted_failure_message(exc),
                    retryable=retryable,
                    provider=work.stage.provider,
                    details=(
                        ("diagnostic_detail", "[redacted]"),
                        (
                            "rejections",
                            _capability_rejections(work.capability_selection),
                        ),
                        ("selection", "|".join(work.capability_selection.explanation)),
                        ("recommendation", recommendation),
                    ),
                ),
                document_file_key=file_key,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        return retryable

    def _publish_cancellation(
        self,
        connection: sqlite3.Connection,
        work: _TextDerivationWork,
        _exc: CancellationRequested,
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cancel_text_derivation_attempt(
                connection,
                work.attempt_id,
                receipt_id=work.receipt_id(WorkOutcome.CANCELLED),
                finished_at_utc=_utc_now(),
                duration_ns=self._duration_ns(work),
                reproducibility=_work_reproducibility(work.capability_selection),
                failure=CapabilityFailure(
                    capability_id=_TEXT_EXTRACT_STAGE_ID,
                    reason_code="framework_cancellation_requested",
                    message="Framework cancellation requested; diagnostic detail redacted.",
                    retryable=True,
                    provider=work.stage.provider,
                    details=(
                        ("diagnostic_detail", "[redacted]"),
                        (
                            "rejections",
                            _capability_rejections(work.capability_selection),
                        ),
                        ("selection", "|".join(work.capability_selection.explanation)),
                    ),
                ),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _emit(self, completed: int, total: int, summary: dict[str, int], *, finished=False) -> None:
        emit_progress(
            self.progress,
            ProgressEvent(
                "text",
                "extract",
                "Extracción incremental de texto genérico",
                completed,
                total,
                "documentos",
                finished,
                (
                    ProgressMetric("cache_hits", summary["cache_hits"]),
                    ProgressMetric("errors", summary["errors"]),
                ),
            ),
        )

    def run(self) -> TextRouteSummary:
        self._validate()
        self.cancellation.checkpoint()
        lock_path = self.config.state_path.with_suffix(
            self.config.state_path.suffix + ".route.lock"
        )
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        with FrameworkRunLock(lock_path):
            return self._run_locked()

    def _run_locked(self) -> TextRouteSummary:
        self._recoverable_retry_keys.clear()
        initialize_text_state(self.config.state_path)
        abandoned_ns = time.time_ns()
        while abandon_running_text_derivations(
            self.config.state_path,
            finished_at_utc=_utc_now(),
            terminal_ns=abandoned_ns,
        ):
            pass
        text_runtime_status = inspect_runtime_capability("text")
        provenance = self.config.processing_provenance
        signature = provenance.signature
        pool, eligible, selected = self._counts()
        counters = {
            "processed": 0,
            "cache_hits": 0,
            "cached_errors": 0,
            "extracted": 0,
            "plain_text": 0,
            "emails": 0,
            "text_chars": 0,
            "truncated": 0,
            "errors": 0,
            "retryable_errors": 0,
        }
        handled_errors = (
            FileChangedError,
            OSError,
            RuntimeError,
            SubprocessOutputLimitError,
            UnicodeError,
            ValueError,
            ET.ParseError,
        )
        effective_signatures: set[str] = set()
        capability_brokers: dict[str, CapabilityBroker] = {}
        self._emit(0, selected, counters)
        with text_database(self.config.state_path, create=False) as connection:
            for mime, snapshot in self._candidates():
                capability_request = _text_capability_request(mime, snapshot.size)
                broker_key = capability_request.execution_contract_fingerprint
                capability_broker = capability_brokers.get(broker_key)
                if capability_broker is None:
                    capability_broker = build_runtime_capability_broker(
                        capability_request,
                        statuses=(text_runtime_status,),
                    )
                    capability_brokers[broker_key] = capability_broker
                capability_selection = capability_broker.select(
                    capability_request,
                    _TEXT_CAPABILITY_POLICY,
                )
                candidate_provenance = _candidate_processing_provenance(
                    provenance,
                    mime,
                    snapshot.path,
                    capability_selection,
                )
                candidate_signature = candidate_provenance.signature
                effective_signatures.add(candidate_signature)
                self.cancellation.checkpoint()
                file_key = file_key_from_snapshot(snapshot)
                with self._admission(snapshot):
                    try:
                        payload = _read_exact(
                            snapshot,
                            self.config.max_file_bytes or snapshot.size,
                            self.cancellation,
                        )
                    except CancellationRequested as exc:
                        resource, revision, input_binding = _partial_input_binding(snapshot)
                        work = self._begin_derivation(
                            connection,
                            candidate_provenance,
                            resource,
                            revision,
                            input_binding,
                            capability_selection,
                            causation_id=None,
                        )
                        self._publish_cancellation(connection, work, exc)
                        raise
                    except handled_errors as exc:
                        resource, revision, input_binding = _partial_input_binding(snapshot)
                        work = self._begin_derivation(
                            connection,
                            candidate_provenance,
                            resource,
                            revision,
                            input_binding,
                            capability_selection,
                            causation_id=None,
                        )
                        retryable = self._publish_error(
                            connection,
                            work,
                            snapshot,
                            mime,
                            exc,
                        )
                        counters["processed"] += 1
                        counters["errors"] += 1
                        counters["retryable_errors"] += int(retryable)
                    else:
                        resource, revision, input_binding = _input_binding(snapshot, payload)
                        reuse_allowed = _selection_allows_reuse(capability_selection)
                        cached_failure = (
                            None
                            if self.config.retry_errors or not reuse_allowed
                            else read_reusable_text_failure_from_connection(
                                connection,
                                file_key,
                                stage_id=_TEXT_EXTRACT_STAGE_ID,
                                processing_signature=candidate_signature,
                                revision_id=revision.revision_id,
                                size=snapshot.size,
                                mtime_ns=snapshot.mtime_ns,
                                birthtime_ns=snapshot.birthtime_ns,
                            )
                        )
                        automatic_retry = (
                            cached_failure is not None
                            and self._claim_recoverable_retry(
                                connection,
                                snapshot,
                                cached_failure,
                            )
                        )
                        if cached_failure is not None and not automatic_retry:
                            self._refresh_cached_error(
                                connection,
                                snapshot,
                                file_key,
                                candidate_signature,
                            )
                            counters["cache_hits"] += 1
                            counters["cached_errors"] += 1
                            completed = counters["processed"] + counters["cache_hits"]
                            self._emit(completed, selected, counters)
                            continue
                        reusable_state = (
                            self._reusable_derivation(
                                connection,
                                file_key,
                                resource,
                                revision,
                                candidate_signature,
                            )
                            if reuse_allowed
                            else None
                        )
                        reusable = None if reusable_state is None else reusable_state[0]
                        work = self._begin_derivation(
                            connection,
                            candidate_provenance,
                            resource,
                            revision,
                            input_binding,
                            capability_selection,
                            causation_id=(
                                None if reusable is None else reusable.producer_receipt_id
                            ),
                        )
                        try:
                            self.cancellation.checkpoint()
                        except CancellationRequested as exc:
                            self._publish_cancellation(connection, work, exc)
                            raise
                        if reusable_state is not None:
                            self._publish_cache_hit(
                                connection,
                                work,
                                snapshot,
                                reusable_state[0],
                            )
                            counters["cache_hits"] += 1
                            _record_extracted_counters(counters, reusable_state[1])
                        else:
                            try:
                                extracted = _extract(
                                    payload,
                                    mime,
                                    snapshot.path,
                                    self.config,
                                    capability_selection,
                                )
                                self.cancellation.checkpoint()
                                refreshed = snapshot_path(snapshot.path)
                                if refreshed != snapshot:
                                    raise FileChangedError("text source changed after extraction")
                            except CancellationRequested as exc:
                                self._publish_cancellation(connection, work, exc)
                                raise
                            except handled_errors as exc:
                                retryable = self._publish_error(
                                    connection,
                                    work,
                                    snapshot,
                                    mime,
                                    exc,
                                )
                                counters["processed"] += 1
                                counters["errors"] += 1
                                counters["retryable_errors"] += int(retryable)
                            else:
                                self._publish_success(
                                    connection,
                                    work,
                                    snapshot,
                                    extracted,
                                )
                                counters["processed"] += 1
                                counters["extracted"] += 1
                                _record_extracted_counters(counters, extracted)
                completed = counters["processed"] + counters["cache_hits"]
                self._emit(completed, selected, counters)
            pruned = 0
            if self.config.max_documents is None and not self.config.selection.active:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    pruned = self._prune_stale_documents(connection)
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
        self._emit(selected, selected, counters, finished=True)
        peak = int(getattr(self.memory_gate, "peak_reserved_bytes", 0))
        waits = int(getattr(self.memory_gate, "wait_count", 0))
        return TextRouteSummary(
            candidate_pool=pool,
            candidates=selected,
            skipped_by_size=max(0, pool - eligible),
            skipped_by_count=max(0, eligible - selected),
            cache_documents_pruned=pruned,
            peak_reserved_bytes=peak,
            memory_waits=waits,
            processing_signature=signature,
            effective_processing_signatures=tuple(sorted(effective_signatures)),
            processing_provenance=provenance.manifest,
            processed=counters["processed"],
            cache_hits=counters["cache_hits"],
            cached_errors=counters["cached_errors"],
            extracted=counters["extracted"],
            plain_text=counters["plain_text"],
            emails=counters["emails"],
            text_chars=counters["text_chars"],
            truncated=counters["truncated"],
            errors=counters["errors"],
            retryable_errors=counters["retryable_errors"],
        )


__all__ = (
    "TEXT_ROUTE_MIMES",
    "TEXT_ROUTE_VERSION",
    "TextRoute",
    "TextRouteConfig",
    "TextRouteSummary",
)
