"""Bounded, advisory supply-chain evidence for the installed runtime.

The networked producer is intentionally limited to ``pip-audit``'s PyPI
service.  The local producer uses only installed distribution metadata and a
verified staged ``pyproject.toml``.  Neither producer fixes packages, evaluates
legal compatibility, imports project content, or grants mutation authority.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import base64
import binascii
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from neocortex.runtime.control.bounded_subprocess import run_bounded_capture
from .external_evidence_models import (
    ExternalProviderMetric,
    ExternalProviderRelation,
    external_metric_identity,
    external_relation_identity,
    external_signature,
)

PIP_AUDIT_PROVIDER_ID = "pip-audit-known-vulnerabilities"
PIP_AUDIT_PROVIDER_SCHEMA = "neocortex.pip-audit-known-vulnerabilities/v2"
INSTALLED_PACKAGE_PROVIDER_ID = "installed-package-inventory"
INSTALLED_PACKAGE_PROVIDER_SCHEMA = "neocortex.installed-package-inventory/v1"

PIP_AUDIT_USES_NETWORK = True
INSTALLED_PACKAGE_USES_NETWORK = False
PIP_AUDIT_SERVICE = "pypi"
_PIP_AUDIT_SOURCE = "PyPI JSON API via pip-audit pypi service"

_PIP_AUDIT_TIMEOUT_SECONDS = 180.0
_PIP_AUDIT_SOCKET_TIMEOUT_SECONDS = 15
_PIP_AUDIT_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
_PIP_AUDIT_STDERR_LIMIT_BYTES = 128 * 1024
_PIP_AUDIT_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
_PIP_AUDIT_FRESHNESS_SECONDS = 24 * 60 * 60
_MAX_AUDIT_PACKAGES = 2_000
_MAX_VULNERABILITIES = 10_000
_MAX_ALIASES_PER_VULNERABILITY = 128
_MAX_FIX_VERSIONS_PER_VULNERABILITY = 128

_MAX_PYPROJECT_BYTES = 1024 * 1024
_MAX_DISTRIBUTIONS = 2_000
_MAX_REQUIREMENTS = 20_000
_MAX_REQUIREMENTS_PER_EDGE = 128
_MAX_LICENSE_DECLARATIONS = 10_000
_MAX_LICENSE_DECLARATIONS_PER_PACKAGE = 64
_MAX_RECORD_BYTES = 8 * 1024 * 1024
_MAX_RECORD_ENTRIES = 20_000
_MAX_RECORD_HASH_BYTES = 1024 * 1024 * 1024
_MAX_TEXT_BYTES = 4_096
_MAX_LICENSE_VALUE_BYTES = 64 * 1024
_LICENSE_EXCERPT_BYTES = 1_024
_HASH_READ_BYTES = 1024 * 1024

_PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_REQUIREMENT_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_PIP_AUDIT_VERSION_PATTERN = re.compile(r"^2\.10\.\d+(?:[A-Za-z0-9.+-]*)?$")

PIP_AUDIT_LIMITATIONS = (
    "known_vulnerability_feed_is_a_point_in_time_snapshot",
    "absence_of_a_report_is_not_proof_of_security",
    "package_reachability_and_runtime_exposure_are_not_assessed",
    "duplicate_advisory_rows_are_merged_by_casefolded_id_with_alias_and_fix_union",
    "advisory_only_no_fix_or_mutation_authority",
)
_INVENTORY_LIMITATIONS = (
    "optional_extra_and_transitive_requirement_constraints_are_recorded_not_gated",
    "base_direct_url_origin_is_recorded_not_verified",
    "license_metadata_is_inventory_not_legal_compatibility_analysis",
    "multiple_license_declarations_remain_explicitly_ambiguous",
    "record_verification_cannot_detect_files_omitted_from_record_without_enumeration",
    "inventory_is_current_only_at_its_observation_time",
    "advisory_only_no_mutation_authority",
)


def installed_environment_distributions() -> tuple[importlib.metadata.Distribution, ...]:
    """Enumerate installed metadata without treating the source cwd as a wheel.

    ``importlib.metadata.distributions()`` implicitly searches ``sys.path``.
    For a source-checkout invocation, its empty first entry therefore discovers
    a local ``*.egg-info`` in addition to the installed wheel.  Supply-chain
    evidence owns the interpreter environment, not project metadata, so search
    every explicit, existing import root while excluding the resolved working
    directory.  Genuine duplicate installed identities remain an error in the
    downstream normalizer.
    """

    working_directory = Path.cwd().resolve(strict=True)
    search_paths: list[str] = []
    seen: set[str] = set()
    for raw in sys.path:
        if not raw:
            continue
        try:
            path = Path(raw).resolve(strict=True)
        except OSError:
            continue
        if path == working_directory or not path.is_dir():
            continue
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        search_paths.append(str(path))
    if not search_paths:
        raise ValueError("installed distribution search path is unavailable")
    return tuple(importlib.metadata.distributions(path=search_paths))


@dataclass(frozen=True, slots=True)
class PipAuditCounters:
    packages_observed: int
    packages_audited: int
    packages_skipped: int
    vulnerable_packages: int
    vulnerabilities: int
    aliases: int


@dataclass(frozen=True, slots=True)
class _PipAuditVulnerability:
    vulnerability_id: str
    aliases: tuple[str, ...]
    fix_versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PipAuditExecution:
    metrics: tuple[ExternalProviderMetric, ...]
    relations: tuple[ExternalProviderRelation, ...]
    counters: PipAuditCounters
    tool_version: str
    source: str
    observed_at_utc: str
    observed_date_utc: str
    snapshot_id: str
    freshness_status: str
    fresh_until_utc: str
    stdout_bytes: int
    stderr_bytes: int
    process_invocations: int
    uses_network: bool
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PipAuditProcess:
    tool_version: str
    completed: subprocess.CompletedProcess[bytes]


@dataclass(frozen=True, slots=True)
class _PipAuditContext:
    process: _PipAuditProcess
    payload: tuple[object, ...]
    observed: datetime
    fresh_until: datetime
    observed_text: str
    fresh_until_text: str
    snapshot_id: str
    common_metadata: Mapping[str, object]


@dataclass(slots=True)
class _PipAuditProjection:
    metrics: list[ExternalProviderMetric] = field(default_factory=list)
    relations: list[ExternalProviderRelation] = field(default_factory=list)
    seen_packages: set[str] = field(default_factory=set)
    raw_vulnerability_rows: int = 0
    packages_audited: int = 0
    packages_skipped: int = 0
    vulnerable_packages: int = 0
    vulnerabilities: int = 0
    aliases: int = 0


@dataclass(frozen=True, slots=True)
class InstalledPackageCounters:
    distributions: int
    requirement_relations: int
    pyproject_required_dependencies: int
    pyproject_required_dependencies_applicable: int
    pyproject_required_dependencies_installed: int
    pyproject_required_dependencies_missing: int
    pyproject_required_dependencies_version_compatible: int
    pyproject_required_dependencies_version_mismatch: int
    pyproject_optional_dependencies: int
    packages_with_license_metadata: int
    packages_with_ambiguous_license_metadata: int
    packages_without_license_metadata: int
    record_entries: int
    record_hash_verified: int
    record_size_verified: int
    record_missing_files: int
    record_hash_mismatches: int
    record_size_mismatches: int
    record_unverifiable_entries: int
    record_unsafe_entries: int


@dataclass(frozen=True, slots=True)
class InstalledPackageInventoryExecution:
    metrics: tuple[ExternalProviderMetric, ...]
    relations: tuple[ExternalProviderRelation, ...]
    counters: InstalledPackageCounters
    source: str
    observed_at_utc: str
    observed_date_utc: str
    snapshot_id: str
    freshness_status: str
    pyproject_sha256: str
    installed_project_version: str
    files_hashed: int
    bytes_hashed: int
    process_invocations: int
    uses_network: bool
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LicenseDeclaration:
    field: str
    value_sha256: str
    excerpt: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class _ProjectRequirement:
    group: str
    raw: str
    target: str
    specifier: str
    marker: str | None
    requested_extras: tuple[str, ...]
    direct_url: str | None
    marker_evaluated: bool
    marker_applies: bool | None


@dataclass(frozen=True, slots=True)
class _BaseDependencyEvaluation:
    declaration: _ProjectRequirement
    target_installed: bool
    installed_version: str | None
    presence_gate_evaluated: bool
    version_constraint_evaluated: bool
    version_compatible: bool | None


@dataclass(frozen=True, slots=True)
class _DistributionRow:
    distribution: importlib.metadata.Distribution
    name: str
    normalized_name: str
    version: str
    requirements: tuple[str, ...]
    licenses: tuple[_LicenseDeclaration, ...]
    license_expression_count: int
    license_legacy_count: int
    license_classifier_count: int


@dataclass(frozen=True, slots=True)
class _RecordVerification:
    present: bool
    digest: str | None
    entries: int
    hash_verified: int
    size_verified: int
    missing_files: int
    hash_mismatches: int
    size_mismatches: int
    unverifiable_entries: int
    unsafe_entries: int
    malformed_entries: int
    files_hashed: int
    bytes_hashed: int

    @property
    def current(self) -> bool:
        return bool(
            self.present
            and self.entries > 1
            and self.hash_verified > 0
            and self.size_verified > 0
            and not (
                self.missing_files
                or self.hash_mismatches
                or self.size_mismatches
                or self.unverifiable_entries
                or self.unsafe_entries
                or self.malformed_entries
            )
        )


@dataclass(frozen=True, slots=True)
class _InventoryContext:
    project_name: str
    pyproject_digest: str
    declarations: tuple[_ProjectRequirement, ...]
    marker_environment: Mapping[str, str]
    rows: tuple[_DistributionRow, ...]
    rows_by_name: Mapping[str, _DistributionRow]
    base_evaluations: tuple[_BaseDependencyEvaluation, ...]
    base_evaluations_by_target: Mapping[str, tuple[_BaseDependencyEvaluation, ...]]
    project_row: _DistributionRow
    record: _RecordVerification
    observed: datetime
    observed_text: str
    snapshot_id: str
    common_metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _PackageInventoryOutputs:
    metrics: tuple[ExternalProviderMetric, ...]
    relations: tuple[ExternalProviderRelation, ...]
    requirement_edges: Mapping[tuple[str, str], tuple[str, ...]]
    license_available: int
    license_ambiguous: int
    license_missing: int


@dataclass(frozen=True, slots=True)
class _InventorySummary:
    applicable_evaluations: tuple[_BaseDependencyEvaluation, ...]
    required_installed: int
    required_missing: int
    required_compatible: int
    required_mismatch: int
    optional_declarations: tuple[_ProjectRequirement, ...]


def _normalized_package_name(value: object, *, label: str) -> str:
    name = _required_text(value, label=label, maximum=256)
    if _PACKAGE_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"{label} is not a valid distribution name")
    return re.sub(r"[-_.]+", "-", name).lower()


def _required_text(value: object, *, label: str, maximum: int = _MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _required_list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} is not a list")
    return value


def _required_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not an object")
    return value


def _observation_time(value: datetime | None) -> datetime:
    observed = datetime.now(timezone.utc) if value is None else value
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("supply-chain observation time must be timezone-aware")
    return observed.astimezone(timezone.utc).replace(microsecond=0)


def _iso_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _metric(
    provider_id: str,
    *,
    subject_key: str,
    category: str,
    name: str,
    value: int | float,
    unit: str = "count",
    metadata: Mapping[str, object] | None = None,
) -> ExternalProviderMetric:
    return ExternalProviderMetric(
        external_metric_identity(
            provider_id,
            subject_kind="project",
            subject_key=subject_key,
            category=category,
            metric_name=name,
            unit=unit,
        ),
        "project",
        subject_key,
        category,
        name,
        value,
        unit,
        metadata={} if metadata is None else metadata,
    )


def _relation(
    provider_id: str,
    *,
    relation_kind: str,
    source_key: str,
    target_kind: Literal["project", "contract"],
    target_key: str,
    metadata: Mapping[str, object],
) -> ExternalProviderRelation:
    return ExternalProviderRelation(
        external_relation_identity(
            provider_id,
            relation_kind=relation_kind,
            source_kind="project",
            source_key=source_key,
            target_kind=target_kind,
            target_key=target_key,
        ),
        relation_kind,
        "project",
        source_key,
        target_kind,
        target_key,
        confidence=1.0,
        metadata=metadata,
    )


def _pip_audit_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in environment.items()
        if not str(key).upper().startswith("PIP_AUDIT_")
    }


def _pip_audit_version() -> str:
    try:
        version = importlib.metadata.version("pip-audit")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError("pip-audit runtime dependency is unavailable") from exc
    if _PIP_AUDIT_VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("pip-audit runtime must use the supported 2.10.x line")
    return version


def _pip_audit_payload(raw: bytes) -> list[object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pip-audit JSON output is malformed") from exc
    envelope = _required_mapping(payload, label="pip-audit output")
    if set(envelope) != {"dependencies", "fixes"}:
        raise ValueError("pip-audit JSON output has an unsupported schema")
    fixes = _required_list(envelope.get("fixes"), label="pip-audit fixes")
    if fixes:
        raise ValueError("pip-audit output reports applied fixes")
    return _required_list(envelope.get("dependencies"), label="pip-audit dependencies")


def _pip_audit_cache_parent(environment: Mapping[str, str]) -> Path:
    normalized = {str(key).casefold(): str(value) for key, value in environment.items()}
    configured = next(
        (normalized[name] for name in ("temp", "tmp", "tmpdir") if normalized.get(name)),
        None,
    )
    selected = Path(tempfile.gettempdir()) if configured is None else Path(configured)
    try:
        parent = selected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("pip-audit cache parent cannot be resolved") from exc
    if not parent.is_dir():
        raise ValueError("pip-audit cache parent is not a directory")
    return parent


def _pip_audit_exit_error(completed: subprocess.CompletedProcess[bytes]) -> ValueError:
    raw_detail = completed.stderr or completed.stdout
    normalized = " ".join(raw_detail.decode("utf-8", errors="replace").split())
    folded = normalized.casefold()
    if any(
        marker in folded
        for marker in (
            "no address associated with hostname",
            "temporary failure in name resolution",
            "name or service not known",
            "nameresolutionerror",
            "connection refused",
            "max retries exceeded",
        )
    ):
        digest = hashlib.sha256(raw_detail).hexdigest()
        return ValueError(
            f"pip_audit_network_unavailable:{completed.returncode}:stderr_sha256:{digest}"
        )
    detail = normalized[:512]
    message = f"pip_audit_unexpected_exit:{completed.returncode}"
    return ValueError(message if not detail else f"{message}:{detail}")


def _bounded_text_list(value: object, *, label: str, maximum_items: int) -> tuple[str, ...]:
    raw_items = _required_list(value, label=label)
    if len(raw_items) > maximum_items:
        raise ValueError(f"{label} exceeds its bound")
    items = {_required_text(item, label=label, maximum=512) for item in raw_items}
    return tuple(sorted(items, key=str.casefold))


def _deduplicated_pip_audit_vulnerabilities(
    raw_vulnerabilities: list[object],
) -> tuple[_PipAuditVulnerability, ...]:
    ids_by_key: defaultdict[str, set[str]] = defaultdict(set)
    aliases_by_key: defaultdict[str, set[str]] = defaultdict(set)
    fixes_by_key: defaultdict[str, set[str]] = defaultdict(set)
    for raw_vulnerability in raw_vulnerabilities:
        vulnerability = _required_mapping(raw_vulnerability, label="pip-audit vulnerability")
        vulnerability_id = _required_text(
            vulnerability.get("id"), label="pip-audit vulnerability id", maximum=256
        )
        vulnerability_key = vulnerability_id.casefold()
        ids_by_key[vulnerability_key].add(vulnerability_id)
        aliases_by_key[vulnerability_key].update(
            _bounded_text_list(
                vulnerability.get("aliases", []),
                label="pip-audit aliases",
                maximum_items=_MAX_ALIASES_PER_VULNERABILITY,
            )
        )
        fixes_by_key[vulnerability_key].update(
            _bounded_text_list(
                vulnerability.get("fix_versions", []),
                label="pip-audit fix versions",
                maximum_items=_MAX_FIX_VERSIONS_PER_VULNERABILITY,
            )
        )

    merged: list[_PipAuditVulnerability] = []
    for vulnerability_key in sorted(ids_by_key):
        aliases = aliases_by_key[vulnerability_key]
        fixes = fixes_by_key[vulnerability_key]
        if len(aliases) > _MAX_ALIASES_PER_VULNERABILITY:
            raise ValueError("pip-audit aliases exceeds its merged bound")
        if len(fixes) > _MAX_FIX_VERSIONS_PER_VULNERABILITY:
            raise ValueError("pip-audit fix versions exceeds its merged bound")
        merged.append(
            _PipAuditVulnerability(
                vulnerability_id=min(
                    ids_by_key[vulnerability_key], key=lambda value: (value.casefold(), value)
                ),
                aliases=tuple(sorted(aliases, key=lambda value: (value.casefold(), value))),
                fix_versions=tuple(sorted(fixes, key=lambda value: (value.casefold(), value))),
            )
        )
    return tuple(merged)


def _validate_pip_audit_freshness(freshness_seconds: int) -> None:
    if not 60 <= freshness_seconds <= 7 * 24 * 60 * 60:
        raise ValueError("pip-audit freshness window is outside its bound")


def _pip_audit_command(cache_sentinel: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "pip_audit",
        "--format",
        "json",
        "--vulnerability-service",
        PIP_AUDIT_SERVICE,
        "--aliases",
        "on",
        "--desc",
        "off",
        "--progress-spinner",
        "off",
        "--timeout",
        str(_PIP_AUDIT_SOCKET_TIMEOUT_SECONDS),
        "--cache-dir",
        str(cache_sentinel),
    )


def _execute_pip_audit_process(environment: Mapping[str, str]) -> _PipAuditProcess:
    tool_version = _pip_audit_version()
    cache_parent = _pip_audit_cache_parent(environment)
    with tempfile.TemporaryDirectory(
        prefix="neocortex-pip-audit-",
        dir=cache_parent,
    ) as temporary:
        # pip-audit otherwise writes its implicit cache below the user's
        # LOCALAPPDATA. A file path deliberately disables that HTTP cache: the
        # first bounded run remains a fresh network snapshot, while NeoCortex's
        # own publication replay is the durable cache and performs no network
        # call. Using a file also avoids leaving child-created cache entries
        # that a constrained Windows runner cannot remove.
        cache_sentinel = Path(temporary) / "http-cache-disabled"
        cache_sentinel.write_bytes(b"")
        command = _pip_audit_command(cache_sentinel)
        completed = run_bounded_capture(
            command,
            timeout_seconds=_PIP_AUDIT_TIMEOUT_SECONDS,
            stdout_limit_bytes=_PIP_AUDIT_STDOUT_LIMIT_BYTES,
            stderr_limit_bytes=_PIP_AUDIT_STDERR_LIMIT_BYTES,
            environment={
                **_pip_audit_environment(environment),
                "HOME": temporary,
                **({"USERPROFILE": temporary} if os.name == "nt" else {}),
            },
            memory_limit_bytes=_PIP_AUDIT_MEMORY_LIMIT_BYTES if os.name == "nt" else None,
        )
    return _PipAuditProcess(tool_version, completed)


def _validated_pip_audit_payload(
    completed: subprocess.CompletedProcess[bytes],
) -> tuple[object, ...]:
    if completed.returncode not in {0, 1}:
        raise _pip_audit_exit_error(completed)
    try:
        payload = _pip_audit_payload(completed.stdout)
    except ValueError as exc:
        if completed.returncode == 1:
            raise _pip_audit_exit_error(completed) from exc
        raise
    if len(payload) > _MAX_AUDIT_PACKAGES:
        raise ValueError("pip-audit package count exceeds its bound")
    return tuple(payload)


def _prepare_pip_audit_context(
    process: _PipAuditProcess,
    payload: tuple[object, ...],
    *,
    observed_at: datetime | None,
    freshness_seconds: int,
) -> _PipAuditContext:
    observed = _observation_time(observed_at)
    fresh_until = observed + timedelta(seconds=freshness_seconds)
    observed_text = _iso_utc(observed)
    fresh_until_text = _iso_utc(fresh_until)
    snapshot_id = external_signature(
        "pip-audit-snapshot-v1",
        {
            "tool_version": process.tool_version,
            "service": PIP_AUDIT_SERVICE,
            "observed_at_utc": observed_text,
            "payload_sha256": hashlib.sha256(process.completed.stdout).hexdigest(),
        },
    )
    common_metadata = {
        "provider_schema": PIP_AUDIT_PROVIDER_SCHEMA,
        "tool_version": process.tool_version,
        "source": _PIP_AUDIT_SOURCE,
        "service": PIP_AUDIT_SERVICE,
        "observed_at_utc": observed_text,
        "observed_date_utc": observed.date().isoformat(),
        "snapshot_id": snapshot_id,
        "freshness_status": "fresh_at_observation",
        "fresh_until_utc": fresh_until_text,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return _PipAuditContext(
        process,
        payload,
        observed,
        fresh_until,
        observed_text,
        fresh_until_text,
        snapshot_id,
        common_metadata,
    )


def _append_pip_audit_vulnerability(
    projection: _PipAuditProjection,
    vulnerability: _PipAuditVulnerability,
    *,
    subject_key: str,
    package_metadata: Mapping[str, object],
) -> None:
    vulnerability_id = vulnerability.vulnerability_id
    aliases = vulnerability.aliases
    fix_versions = vulnerability.fix_versions
    projection.vulnerabilities += 1
    projection.aliases += len(aliases)
    evidence_metadata = {
        **package_metadata,
        "vulnerability_id": vulnerability_id,
        "aliases": list(aliases),
        "fix_versions": list(fix_versions),
        "fix_available": bool(fix_versions),
        "descriptions_collected": False,
    }
    projection.metrics.append(
        _metric(
            PIP_AUDIT_PROVIDER_ID,
            subject_key=subject_key,
            category="known_vulnerability",
            name=f"known_vulnerability:{vulnerability_id}",
            value=1,
            metadata=evidence_metadata,
        )
    )
    projection.relations.append(
        _relation(
            PIP_AUDIT_PROVIDER_ID,
            relation_kind="package_has_known_vulnerability",
            source_key=subject_key,
            target_kind="contract",
            target_key=f"advisory:{vulnerability_id}",
            metadata={**evidence_metadata, "category": "known_vulnerability"},
        )
    )


def _append_pip_audit_package(
    context: _PipAuditContext,
    projection: _PipAuditProjection,
    raw_package: object,
) -> None:
    package = _required_mapping(raw_package, label="pip-audit package")
    raw_name = _required_text(
        package.get("name"),
        label="pip-audit package name",
        maximum=256,
    )
    normalized = _normalized_package_name(raw_name, label="pip-audit package name")
    if normalized in projection.seen_packages:
        raise ValueError("pip-audit output contains a duplicate package")
    projection.seen_packages.add(normalized)
    subject_key = f"package:{normalized}"
    skip_reason = package.get("skip_reason")
    if skip_reason is not None:
        reason = _required_text(skip_reason, label="pip-audit skip reason")
        projection.packages_skipped += 1
        projection.metrics.append(
            _metric(
                PIP_AUDIT_PROVIDER_ID,
                subject_key=subject_key,
                category="known_vulnerability",
                name="package_audit_skipped",
                value=1,
                metadata={
                    **context.common_metadata,
                    "package_name": raw_name,
                    "skip_reason": reason,
                },
            )
        )
        return

    version = _required_text(
        package.get("version"),
        label="pip-audit installed version",
        maximum=512,
    )
    raw_vulnerabilities = _required_list(
        package.get("vulns"),
        label="pip-audit vulnerabilities",
    )
    projection.raw_vulnerability_rows += len(raw_vulnerabilities)
    if projection.raw_vulnerability_rows > _MAX_VULNERABILITIES:
        raise ValueError("pip-audit vulnerability count exceeds its bound")
    vulnerabilities = _deduplicated_pip_audit_vulnerabilities(raw_vulnerabilities)
    projection.packages_audited += 1
    projection.vulnerable_packages += int(bool(vulnerabilities))
    package_metadata = {
        **context.common_metadata,
        "package_name": raw_name,
        "normalized_name": normalized,
        "installed_version": version,
    }
    projection.metrics.extend(
        (
            _metric(
                PIP_AUDIT_PROVIDER_ID,
                subject_key=subject_key,
                category="known_vulnerability",
                name="package_audited",
                value=1,
                metadata=package_metadata,
            ),
            _metric(
                PIP_AUDIT_PROVIDER_ID,
                subject_key=subject_key,
                category="known_vulnerability",
                name="known_vulnerability_count",
                value=len(vulnerabilities),
                metadata=package_metadata,
            ),
        )
    )
    for vulnerability in vulnerabilities:
        _append_pip_audit_vulnerability(
            projection,
            vulnerability,
            subject_key=subject_key,
            package_metadata=package_metadata,
        )


def _build_pip_audit_outputs(context: _PipAuditContext) -> _PipAuditProjection:
    projection = _PipAuditProjection()
    for raw_package in context.payload:
        _append_pip_audit_package(context, projection, raw_package)
    return projection


def _validate_pip_audit_exit_status(
    completed: subprocess.CompletedProcess[bytes],
    vulnerability_count: int,
) -> None:
    if (completed.returncode == 1) != (vulnerability_count > 0):
        raise ValueError("pip-audit exit status disagrees with vulnerability payload")


def _pip_audit_counters(
    context: _PipAuditContext,
    projection: _PipAuditProjection,
) -> PipAuditCounters:
    return PipAuditCounters(
        len(context.payload),
        projection.packages_audited,
        projection.packages_skipped,
        projection.vulnerable_packages,
        projection.vulnerabilities,
        projection.aliases,
    )


def _pip_audit_summary_metrics(
    context: _PipAuditContext,
    counters: PipAuditCounters,
    *,
    freshness_seconds: int,
) -> tuple[ExternalProviderMetric, ...]:
    summary_key = "project:installed-environment"
    summary_values = (
        ("audit_observed_at_unix_seconds", int(context.observed.timestamp()), "unix_seconds"),
        (
            "audit_fresh_until_unix_seconds",
            int(context.fresh_until.timestamp()),
            "unix_seconds",
        ),
        ("audit_freshness_window_seconds", freshness_seconds, "seconds"),
        ("audit_current_at_observation", 1, "boolean"),
        ("audited_package_count", counters.packages_audited, "count"),
        ("skipped_package_count", counters.packages_skipped, "count"),
        ("vulnerable_package_count", counters.vulnerable_packages, "count"),
        ("known_vulnerability_count", counters.vulnerabilities, "count"),
    )
    return tuple(
        _metric(
            PIP_AUDIT_PROVIDER_ID,
            subject_key=summary_key,
            category="known_vulnerability",
            name=name,
            value=value,
            unit=unit,
            metadata=context.common_metadata,
        )
        for name, value, unit in summary_values
    )


def _build_pip_audit_execution(
    context: _PipAuditContext,
    projection: _PipAuditProjection,
    *,
    freshness_seconds: int,
) -> PipAuditExecution:
    completed = context.process.completed
    counters = _pip_audit_counters(context, projection)
    metrics = (
        *projection.metrics,
        *_pip_audit_summary_metrics(
            context,
            counters,
            freshness_seconds=freshness_seconds,
        ),
    )
    return PipAuditExecution(
        tuple(sorted(metrics, key=lambda item: item.portable_metric_id)),
        tuple(sorted(projection.relations, key=lambda item: item.portable_relation_id)),
        counters,
        context.process.tool_version,
        _PIP_AUDIT_SOURCE,
        context.observed_text,
        context.observed.date().isoformat(),
        context.snapshot_id,
        "fresh_at_observation",
        context.fresh_until_text,
        len(completed.stdout),
        len(completed.stderr),
        1,
        PIP_AUDIT_USES_NETWORK,
        PIP_AUDIT_LIMITATIONS,
    )


def execute_pip_audit_known_vulnerabilities(
    environment: Mapping[str, str],
    *,
    observed_at: datetime | None = None,
    freshness_seconds: int = _PIP_AUDIT_FRESHNESS_SECONDS,
) -> PipAuditExecution:
    """Audit the installed environment through the bounded PyPI service only."""

    _validate_pip_audit_freshness(freshness_seconds)
    process = _execute_pip_audit_process(environment)
    payload = _validated_pip_audit_payload(process.completed)
    context = _prepare_pip_audit_context(
        process,
        payload,
        observed_at=observed_at,
        freshness_seconds=freshness_seconds,
    )
    outputs = _build_pip_audit_outputs(context)
    _validate_pip_audit_exit_status(
        process.completed,
        outputs.vulnerabilities,
    )
    return _build_pip_audit_execution(
        context,
        outputs,
        freshness_seconds=freshness_seconds,
    )


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & reparse)


def _read_verified_file(path: Path, *, maximum: int, label: str) -> bytes:
    before = os.lstat(path)
    if _is_reparse_point(path) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} is not a regular file")
    if before.st_size > maximum:
        raise ValueError(f"{label} exceeds its byte bound")
    raw = path.read_bytes()
    after = os.lstat(path)
    if (
        len(raw) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError(f"{label} changed while it was read")
    return raw


def _requirement_name(requirement: str) -> str:
    _required_text(requirement, label="requirement")
    match = _REQUIREMENT_NAME_PATTERN.match(requirement)
    if match is None:
        raise ValueError("requirement does not start with a valid distribution name")
    return _normalized_package_name(match.group(1), label="requirement name")


def _parse_project_requirement(
    raw: object,
    *,
    group: str,
    marker_environment: Mapping[str, str],
) -> _ProjectRequirement:
    text = _required_text(raw, label="pyproject dependency")
    try:
        parsed = Requirement(text)
    except InvalidRequirement as exc:
        raise ValueError("pyproject dependency is not a valid PEP 508 requirement") from exc
    marker = None if parsed.marker is None else str(parsed.marker)
    marker_evaluated = group == "required"
    marker_applies = (
        None
        if not marker_evaluated
        else parsed.marker is None or parsed.marker.evaluate(environment=dict(marker_environment))
    )
    return _ProjectRequirement(
        group,
        text,
        _normalized_package_name(parsed.name, label="pyproject dependency name"),
        str(parsed.specifier),
        marker,
        tuple(sorted(parsed.extras)),
        parsed.url,
        marker_evaluated,
        marker_applies,
    )


def _project_metadata(
    pyproject_path: Path,
) -> tuple[str, str, tuple[_ProjectRequirement, ...], Mapping[str, str]]:
    raw = _read_verified_file(
        pyproject_path.absolute(), maximum=_MAX_PYPROJECT_BYTES, label="staged pyproject"
    )
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("staged pyproject is malformed") from exc
    project = _required_mapping(document.get("project"), label="pyproject project")
    raw_name = _required_text(project.get("name"), label="pyproject project name", maximum=256)
    normalized_name = _normalized_package_name(raw_name, label="pyproject project name")
    if normalized_name != "neocortex-framework":
        raise ValueError("staged pyproject does not describe neocortex-framework")
    marker_environment: dict[str, str] = {}
    for raw_key, raw_value in default_environment().items():
        key = _required_text(raw_key, label="marker environment key", maximum=128)
        marker_environment[key] = _required_text(
            raw_value,
            label=f"marker environment value {key}",
            maximum=4096,
        )
    marker_environment["extra"] = ""
    declarations: list[_ProjectRequirement] = []
    for raw_requirement in _required_list(
        project.get("dependencies", []), label="pyproject dependencies"
    ):
        declarations.append(
            _parse_project_requirement(
                raw_requirement,
                group="required",
                marker_environment=marker_environment,
            )
        )
    optional = _required_mapping(
        project.get("optional-dependencies", {}), label="pyproject optional dependencies"
    )
    for raw_group, raw_requirements in sorted(optional.items(), key=lambda item: str(item[0])):
        group = _required_text(raw_group, label="pyproject optional group", maximum=128)
        for raw_requirement in _required_list(
            raw_requirements, label="pyproject optional dependency group"
        ):
            declarations.append(
                _parse_project_requirement(
                    raw_requirement,
                    group=group,
                    marker_environment=marker_environment,
                )
            )
    if len(declarations) > _MAX_REQUIREMENTS:
        raise ValueError("pyproject dependency declarations exceed their bound")
    return (
        normalized_name,
        hashlib.sha256(raw).hexdigest(),
        tuple(declarations),
        marker_environment,
    )


def _metadata_values(distribution: importlib.metadata.Distribution, field: str) -> tuple[str, ...]:
    metadata = distribution.metadata
    getter = getattr(metadata, "get_all", None)
    raw_values: object
    if callable(getter):
        raw_values = getter(field, [])
    else:
        raw_values = metadata.get(field)
    if raw_values is None:
        return ()
    if isinstance(raw_values, str):
        values: Sequence[object] = (raw_values,)
    elif isinstance(raw_values, Sequence):
        values = raw_values
    else:
        raise ValueError(f"installed metadata field {field} is invalid")
    result = []
    for value in values:
        text = _required_text(
            value, label=f"installed metadata field {field}", maximum=_MAX_LICENSE_VALUE_BYTES
        ).strip()
        if text:
            result.append(text)
    return tuple(result)


def _license_declaration(field: str, value: str) -> _LicenseDeclaration:
    raw = value.encode("utf-8")
    excerpt_bytes = raw[:_LICENSE_EXCERPT_BYTES]
    while True:
        try:
            excerpt = excerpt_bytes.decode("utf-8")
            break
        except UnicodeDecodeError:
            excerpt_bytes = excerpt_bytes[:-1]
    return _LicenseDeclaration(
        field,
        hashlib.sha256(raw).hexdigest(),
        excerpt,
        len(raw) > len(excerpt_bytes),
    )


def _distribution_rows(
    distributions: Iterable[importlib.metadata.Distribution],
) -> tuple[_DistributionRow, ...]:
    rows: list[_DistributionRow] = []
    seen: set[str] = set()
    total_requirements = total_licenses = 0
    for distribution in distributions:
        if len(rows) >= _MAX_DISTRIBUTIONS:
            raise ValueError("installed distribution count exceeds its bound")
        raw_name = distribution.metadata.get("Name")
        normalized = _normalized_package_name(raw_name, label="installed distribution name")
        if normalized in seen:
            raise ValueError("installed distribution names are ambiguous after normalization")
        seen.add(normalized)
        version = _required_text(
            distribution.version, label="installed distribution version", maximum=512
        )
        raw_requirements = distribution.requires or []
        requirements = tuple(
            _required_text(value, label="installed distribution requirement")
            for value in raw_requirements
        )
        total_requirements += len(requirements)
        if total_requirements > _MAX_REQUIREMENTS:
            raise ValueError("installed requirement declarations exceed their bound")
        expressions = _metadata_values(distribution, "License-Expression")
        legacy = _metadata_values(distribution, "License")
        classifiers = tuple(
            value
            for value in _metadata_values(distribution, "Classifier")
            if value.startswith("License ::")
        )
        declarations = tuple(
            _license_declaration(field, value)
            for field, values in (
                ("License-Expression", expressions),
                ("License", legacy),
                ("Classifier", classifiers),
            )
            for value in values
        )
        if len(declarations) > _MAX_LICENSE_DECLARATIONS_PER_PACKAGE:
            raise ValueError("installed package license declarations exceed their bound")
        total_licenses += len(declarations)
        if total_licenses > _MAX_LICENSE_DECLARATIONS:
            raise ValueError("installed license declarations exceed their bound")
        rows.append(
            _DistributionRow(
                distribution,
                _required_text(raw_name, label="installed distribution name", maximum=256),
                normalized,
                version,
                requirements,
                declarations,
                len(expressions),
                len(legacy),
                len(classifiers),
            )
        )
    return tuple(sorted(rows, key=lambda item: item.normalized_name))


def _evaluate_base_dependencies(
    declarations: Sequence[_ProjectRequirement],
    rows_by_name: Mapping[str, _DistributionRow],
) -> tuple[_BaseDependencyEvaluation, ...]:
    evaluations: list[_BaseDependencyEvaluation] = []
    for declaration in declarations:
        if declaration.group != "required":
            continue
        if declaration.marker_applies is None:
            raise ValueError("base dependency marker was not evaluated")
        installed = rows_by_name.get(declaration.target)
        applicable = declaration.marker_applies
        version_evaluated = bool(
            applicable and installed is not None and not declaration.direct_url
        )
        version_compatible: bool | None = None
        if version_evaluated:
            assert installed is not None
            try:
                installed_version = Version(installed.version)
            except InvalidVersion as exc:
                raise ValueError("installed base dependency version is not valid PEP 440") from exc
            version_compatible = declaration.specifier == "" or Requirement(
                declaration.raw
            ).specifier.contains(installed_version, prereleases=True)
        evaluations.append(
            _BaseDependencyEvaluation(
                declaration,
                installed is not None,
                None if installed is None else installed.version,
                applicable,
                version_evaluated,
                version_compatible,
            )
        )
    return tuple(evaluations)


def _base_evaluation_payload(
    evaluation: _BaseDependencyEvaluation,
    *,
    marker_environment: Mapping[str, str],
) -> dict[str, object]:
    declaration = evaluation.declaration
    return {
        "group": declaration.group,
        "requirement": declaration.raw,
        "target": declaration.target,
        "specifier": declaration.specifier,
        "marker": declaration.marker,
        "requested_extras": list(declaration.requested_extras),
        "direct_url": declaration.direct_url,
        "marker_evaluated": declaration.marker_evaluated,
        "marker_applies": declaration.marker_applies,
        "marker_environment": dict(marker_environment),
        "target_installed": evaluation.target_installed,
        "installed_version": evaluation.installed_version,
        "presence_gate_evaluated": evaluation.presence_gate_evaluated,
        "version_constraint_evaluated": evaluation.version_constraint_evaluated,
        "version_compatible": evaluation.version_compatible,
        "gate_scope": "base_project_dependency",
    }


def _optional_declaration_payload(declaration: _ProjectRequirement) -> dict[str, object]:
    return {
        "group": declaration.group,
        "requirement": declaration.raw,
        "target": declaration.target,
        "specifier": declaration.specifier,
        "marker": declaration.marker,
        "requested_extras": list(declaration.requested_extras),
        "direct_url": declaration.direct_url,
        "marker_evaluated": False,
        "marker_applies": None,
        "presence_gate_evaluated": False,
        "version_constraint_evaluated": False,
        "version_compatible": None,
        "extra_group_selected": False,
        "gate_scope": "optional_extra_inventory_only",
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(path), os.fspath(root)))
        return os.path.normcase(common) == os.path.normcase(os.fspath(root))
    except ValueError:
        return False


def _record_hash(path: Path, algorithm: str) -> tuple[bytes, int]:
    digest = hashlib.new(algorithm)
    observed = 0
    before = os.lstat(path)
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_READ_BYTES):
            observed += len(chunk)
            digest.update(chunk)
    after = os.lstat(path)
    if (
        observed != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError("installed package file changed during RECORD verification")
    return digest.digest(), observed


@dataclass(slots=True)
class _RecordVerificationProgress:
    hash_verified: int = 0
    size_verified: int = 0
    missing_files: int = 0
    hash_mismatches: int = 0
    size_mismatches: int = 0
    unverifiable_entries: int = 0
    unsafe_entries: int = 0
    malformed_entries: int = 0
    files_hashed: int = 0
    bytes_hashed: int = 0

    def result(self, raw_record: bytes, entry_count: int) -> _RecordVerification:
        return _RecordVerification(
            present=True,
            digest=hashlib.sha256(raw_record).hexdigest(),
            entries=entry_count,
            hash_verified=self.hash_verified,
            size_verified=self.size_verified,
            missing_files=self.missing_files,
            hash_mismatches=self.hash_mismatches,
            size_mismatches=self.size_mismatches,
            unverifiable_entries=self.unverifiable_entries,
            unsafe_entries=self.unsafe_entries,
            malformed_entries=self.malformed_entries,
            files_hashed=self.files_hashed,
            bytes_hashed=self.bytes_hashed,
        )


class _RecordVerifier:
    __slots__ = ("_distribution", "_progress", "_root")

    def __init__(
        self,
        distribution: importlib.metadata.Distribution,
        root: Path,
    ) -> None:
        self._distribution = distribution
        self._root = root
        self._progress = _RecordVerificationProgress()

    def verify(
        self,
        rows: Sequence[Sequence[str]],
        raw_record: bytes,
    ) -> _RecordVerification:
        for row in rows:
            self._verify_row(row)
        return self._progress.result(raw_record, len(rows))

    def _verify_row(self, row: Sequence[str]) -> None:
        entry = self._parse_entry(row)
        if entry is None:
            return
        record_path, hash_field, size_field = entry
        candidate = self._admit_candidate(record_path)
        if candidate is None:
            return
        path, file_metadata = candidate
        self._verify_size(size_field, file_metadata.st_size)
        expected_hash = self._expected_hash(hash_field)
        if expected_hash is None:
            if not self._blank_hash_is_exempt(record_path):
                self._progress.unverifiable_entries += 1
            return
        algorithm, expected_digest = expected_hash
        self._verify_hash(path, file_metadata.st_size, algorithm, expected_digest)

    def _parse_entry(self, row: Sequence[str]) -> tuple[str, str, str] | None:
        if len(row) != 3 or not row[0] or len(row[0].encode("utf-8")) > _MAX_TEXT_BYTES:
            self._progress.malformed_entries += 1
            return None
        return row[0], row[1], row[2]

    def _admit_candidate(self, record_path: str) -> tuple[Path, os.stat_result] | None:
        candidate = Path(str(self._distribution.locate_file(record_path))).resolve(strict=False)
        if not _is_within(candidate, self._root):
            self._progress.unsafe_entries += 1
            return None
        try:
            file_metadata = os.lstat(candidate)
        except FileNotFoundError:
            self._progress.missing_files += 1
            return None
        if _is_reparse_point(candidate) or not stat.S_ISREG(file_metadata.st_mode):
            self._progress.unsafe_entries += 1
            return None
        return candidate, file_metadata

    def _verify_size(self, size_field: str, observed_size: int) -> None:
        if not size_field:
            return
        if not size_field.isdecimal():
            self._progress.malformed_entries += 1
        elif int(size_field) == observed_size:
            self._progress.size_verified += 1
        else:
            self._progress.size_mismatches += 1

    def _expected_hash(self, hash_field: str) -> tuple[str, bytes] | None:
        if not hash_field:
            return None
        algorithm, separator, encoded_digest = hash_field.partition("=")
        try:
            if (
                not separator
                or algorithm not in hashlib.algorithms_guaranteed
                or not encoded_digest
            ):
                raise ValueError
            padding = "=" * (-len(encoded_digest) % 4)
            expected_digest = base64.urlsafe_b64decode(encoded_digest + padding)
        except (ValueError, binascii.Error):
            self._progress.malformed_entries += 1
            return None
        return algorithm, expected_digest

    @staticmethod
    def _blank_hash_is_exempt(record_path: str) -> bool:
        normalized = record_path.replace("\\", "/").casefold()
        return normalized.endswith((".pyc", ".dist-info/record"))

    def _verify_hash(
        self,
        path: Path,
        expected_bytes: int,
        algorithm: str,
        expected_digest: bytes,
    ) -> None:
        if self._progress.bytes_hashed + expected_bytes > _MAX_RECORD_HASH_BYTES:
            raise ValueError("installed RECORD hash bytes exceed their bound")
        observed_digest, observed_bytes = _record_hash(path, algorithm)
        self._progress.files_hashed += 1
        self._progress.bytes_hashed += observed_bytes
        if observed_digest == expected_digest:
            self._progress.hash_verified += 1
        else:
            self._progress.hash_mismatches += 1


def _record_verification(
    distribution: importlib.metadata.Distribution,
    *,
    installation_root: Path,
) -> _RecordVerification:
    record = distribution.read_text("RECORD")
    if record is None:
        return _RecordVerification(
            present=False,
            digest=None,
            entries=0,
            hash_verified=0,
            size_verified=0,
            missing_files=0,
            hash_mismatches=0,
            size_mismatches=0,
            unverifiable_entries=0,
            unsafe_entries=0,
            malformed_entries=0,
            files_hashed=0,
            bytes_hashed=0,
        )
    raw_record = record.encode("utf-8")
    if len(raw_record) > _MAX_RECORD_BYTES:
        raise ValueError("installed RECORD exceeds its byte bound")
    rows = list(csv.reader(io.StringIO(record)))
    if len(rows) > _MAX_RECORD_ENTRIES:
        raise ValueError("installed RECORD entry count exceeds its bound")
    root = installation_root.resolve(strict=True)
    return _RecordVerifier(distribution, root).verify(rows, raw_record)


def _license_ambiguity(row: _DistributionRow) -> tuple[bool, tuple[str, ...]]:
    reasons = []
    populated_fields = sum(
        count > 0
        for count in (
            row.license_expression_count,
            row.license_legacy_count,
            row.license_classifier_count,
        )
    )
    if populated_fields > 1:
        reasons.append("multiple_metadata_fields")
    if any(
        count > 1
        for count in (
            row.license_expression_count,
            row.license_legacy_count,
            row.license_classifier_count,
        )
    ):
        reasons.append("multiple_values_in_field")
    return bool(reasons), tuple(reasons)


def _prepare_inventory_context(
    pyproject_path: Path,
    *,
    distributions: Iterable[importlib.metadata.Distribution] | None,
    installation_root: Path | None,
    observed_at: datetime | None,
) -> _InventoryContext:
    project_name, pyproject_digest, declarations, marker_environment = _project_metadata(
        pyproject_path
    )
    rows = _distribution_rows(
        installed_environment_distributions() if distributions is None else distributions
    )
    rows_by_name = {row.normalized_name: row for row in rows}
    project_row = rows_by_name.get(project_name)
    if project_row is None:
        raise ValueError("installed neocortex-framework distribution is unavailable")
    base_evaluations = _evaluate_base_dependencies(declarations, rows_by_name)
    evaluations_by_target: dict[str, list[_BaseDependencyEvaluation]] = defaultdict(list)
    for evaluation in base_evaluations:
        evaluations_by_target[evaluation.declaration.target].append(evaluation)
    root = Path(sys.prefix) if installation_root is None else installation_root
    record = _record_verification(project_row.distribution, installation_root=root)
    observed = _observation_time(observed_at)
    observed_text = _iso_utc(observed)
    snapshot_payload = {
        "pyproject_sha256": pyproject_digest,
        "distributions": [
            {
                "name": row.normalized_name,
                "version": row.version,
                "requirements": list(row.requirements),
                "licenses": [
                    {"field": item.field, "value_sha256": item.value_sha256}
                    for item in row.licenses
                ],
            }
            for row in rows
        ],
        "record": {
            "sha256": record.digest,
            "current": record.current,
            "entries": record.entries,
            "hash_verified": record.hash_verified,
            "size_verified": record.size_verified,
            "missing_files": record.missing_files,
            "hash_mismatches": record.hash_mismatches,
            "size_mismatches": record.size_mismatches,
            "unverifiable_entries": record.unverifiable_entries,
            "unsafe_entries": record.unsafe_entries,
            "malformed_entries": record.malformed_entries,
        },
        "marker_environment": dict(marker_environment),
        "base_dependency_evaluations": [
            _base_evaluation_payload(
                evaluation,
                marker_environment=marker_environment,
            )
            for evaluation in base_evaluations
        ],
        "observed_at_utc": observed_text,
    }
    snapshot_id = (
        "installed-package-snapshot-v1:sha256:"
        + hashlib.sha256(
            json.dumps(
                snapshot_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    )
    common_metadata = {
        "provider_schema": INSTALLED_PACKAGE_PROVIDER_SCHEMA,
        "source": "python importlib.metadata and installed wheel RECORD",
        "observed_at_utc": observed_text,
        "observed_date_utc": observed.date().isoformat(),
        "snapshot_id": snapshot_id,
        "freshness_status": "current_at_observation_only",
        "pyproject_sha256": pyproject_digest,
        "marker_environment_source": "packaging.markers.default_environment",
        "marker_environment_sha256": hashlib.sha256(
            json.dumps(
                marker_environment,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "authority": "advisory",
        "mutation_authority": False,
    }
    return _InventoryContext(
        project_name,
        pyproject_digest,
        declarations,
        marker_environment,
        rows,
        rows_by_name,
        base_evaluations,
        {key: tuple(value) for key, value in evaluations_by_target.items()},
        project_row,
        record,
        observed,
        observed_text,
        snapshot_id,
        common_metadata,
    )


def _build_package_inventory_outputs(context: _InventoryContext) -> _PackageInventoryOutputs:
    metrics: list[ExternalProviderMetric] = []
    relations: list[ExternalProviderRelation] = []
    requirement_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    license_available = license_ambiguous = license_missing = 0
    for row in context.rows:
        subject_key = f"package:{row.normalized_name}"
        package_metadata = {
            **context.common_metadata,
            "package_name": row.name,
            "normalized_name": row.normalized_name,
            "installed_version": row.version,
        }
        for requirement in row.requirements:
            target = _requirement_name(requirement)
            edge = requirement_edges[(subject_key, f"package:{target}")]
            if len(edge) >= _MAX_REQUIREMENTS_PER_EDGE:
                raise ValueError("installed requirements per package edge exceed their bound")
            edge.append(requirement)
        ambiguous, ambiguity_reasons = _license_ambiguity(row)
        if row.licenses:
            license_available += 1
            license_ambiguous += int(ambiguous)
        else:
            license_missing += 1
        metrics.extend(
            (
                _metric(
                    INSTALLED_PACKAGE_PROVIDER_ID,
                    subject_key=subject_key,
                    category="package_integrity",
                    name="distribution_present",
                    value=1,
                    metadata=package_metadata,
                ),
                _metric(
                    INSTALLED_PACKAGE_PROVIDER_ID,
                    subject_key=subject_key,
                    category="package_integrity",
                    name="declared_requirement_count",
                    value=len(row.requirements),
                    metadata={
                        **package_metadata,
                        "requirement_markers_evaluated": False,
                        "version_constraints_evaluated": False,
                    },
                ),
                _metric(
                    INSTALLED_PACKAGE_PROVIDER_ID,
                    subject_key=subject_key,
                    category="license_inventory",
                    name="license_metadata_available",
                    value=int(bool(row.licenses)),
                    unit="boolean",
                    metadata={
                        **package_metadata,
                        "metadata_status": "present" if row.licenses else "missing",
                        "legal_compatibility_assessed": False,
                    },
                ),
                _metric(
                    INSTALLED_PACKAGE_PROVIDER_ID,
                    subject_key=subject_key,
                    category="license_inventory",
                    name="license_metadata_ambiguous",
                    value=int(ambiguous),
                    unit="boolean",
                    metadata={
                        **package_metadata,
                        "ambiguity_reasons": list(ambiguity_reasons),
                        "legal_compatibility_assessed": False,
                    },
                ),
            )
        )
        for name, value in (
            ("license_expression_count", row.license_expression_count),
            ("license_legacy_field_count", row.license_legacy_count),
            ("license_classifier_count", row.license_classifier_count),
        ):
            metrics.append(
                _metric(
                    INSTALLED_PACKAGE_PROVIDER_ID,
                    subject_key=subject_key,
                    category="license_inventory",
                    name=name,
                    value=value,
                    metadata={**package_metadata, "legal_compatibility_assessed": False},
                )
            )
        for declaration in row.licenses:
            field_key = re.sub(r"[^a-z0-9]+", "-", declaration.field.casefold()).strip("-")
            relations.append(
                _relation(
                    INSTALLED_PACKAGE_PROVIDER_ID,
                    relation_kind="package_declares_license",
                    source_key=subject_key,
                    target_kind="contract",
                    target_key=(
                        f"license-declaration:{field_key}:sha256:{declaration.value_sha256}"
                    ),
                    metadata={
                        **package_metadata,
                        "category": "license_inventory",
                        "source_field": declaration.field,
                        "declaration_excerpt": declaration.excerpt,
                        "declaration_sha256": declaration.value_sha256,
                        "declaration_truncated": declaration.truncated,
                        "metadata_ambiguous": ambiguous,
                        "ambiguity_reasons": list(ambiguity_reasons),
                        "legal_compatibility_assessed": False,
                    },
                )
            )
    return _PackageInventoryOutputs(
        tuple(metrics),
        tuple(relations),
        {key: tuple(value) for key, value in requirement_edges.items()},
        license_available,
        license_ambiguous,
        license_missing,
    )


def _package_requirement_relations(
    requirement_edges: Mapping[tuple[str, str], tuple[str, ...]],
    *,
    rows_by_name: Mapping[str, _DistributionRow],
    common_metadata: Mapping[str, object],
) -> tuple[ExternalProviderRelation, ...]:
    return tuple(
        _relation(
            INSTALLED_PACKAGE_PROVIDER_ID,
            relation_kind="package_requires_distribution",
            source_key=source_key,
            target_kind="project",
            target_key=target_key,
            metadata={
                **common_metadata,
                "category": "package_integrity",
                "requirements": sorted(set(requirements)),
                "target_installed": target_key.removeprefix("package:") in rows_by_name,
                "requirement_markers_evaluated": False,
                "version_constraints_evaluated": False,
            },
        )
        for (source_key, target_key), requirements in sorted(requirement_edges.items())
    )


def _project_dependency_relations(
    context: _InventoryContext,
) -> tuple[ExternalProviderRelation, ...]:
    direct_edges: dict[str, list[_ProjectRequirement]] = defaultdict(list)
    for declaration in context.declarations:
        direct_edges[declaration.target].append(declaration)
    relations = []
    for target, edge_declarations in sorted(direct_edges.items()):
        target_base_evaluations = context.base_evaluations_by_target.get(target, ())
        target_optional_declarations = [
            declaration for declaration in edge_declarations if declaration.group != "required"
        ]
        relations.append(
            _relation(
                INSTALLED_PACKAGE_PROVIDER_ID,
                relation_kind="project_declares_dependency",
                source_key=f"package:{context.project_name}",
                target_kind="project",
                target_key=f"package:{target}",
                metadata={
                    **context.common_metadata,
                    "category": "package_integrity",
                    "groups": sorted({declaration.group for declaration in edge_declarations}),
                    "requirements": sorted({declaration.raw for declaration in edge_declarations}),
                    "target_installed": target in context.rows_by_name,
                    "base_dependency_evaluations": [
                        _base_evaluation_payload(
                            evaluation,
                            marker_environment=context.marker_environment,
                        )
                        for evaluation in target_base_evaluations
                    ],
                    "optional_declarations": [
                        _optional_declaration_payload(declaration)
                        for declaration in target_optional_declarations
                    ],
                    "base_gate_evaluated": bool(target_base_evaluations),
                    "optional_extras_gate_evaluated": False,
                },
            )
        )
    return tuple(relations)


def _inventory_summary(context: _InventoryContext) -> _InventorySummary:
    applicable_evaluations = tuple(
        evaluation
        for evaluation in context.base_evaluations
        if evaluation.declaration.marker_applies is True
    )
    return _InventorySummary(
        applicable_evaluations,
        sum(evaluation.target_installed for evaluation in applicable_evaluations),
        sum(not evaluation.target_installed for evaluation in applicable_evaluations),
        sum(evaluation.version_compatible is True for evaluation in applicable_evaluations),
        sum(evaluation.version_compatible is False for evaluation in applicable_evaluations),
        tuple(
            declaration for declaration in context.declarations if declaration.group != "required"
        ),
    )


def _record_metrics(context: _InventoryContext) -> tuple[ExternalProviderMetric, ...]:
    record = context.record
    record_values = (
        ("record_present", int(record.present), "boolean"),
        ("record_entry_count", record.entries, "count"),
        ("record_hash_verified_count", record.hash_verified, "count"),
        ("record_size_verified_count", record.size_verified, "count"),
        ("record_missing_file_count", record.missing_files, "count"),
        ("record_hash_mismatch_count", record.hash_mismatches, "count"),
        ("record_size_mismatch_count", record.size_mismatches, "count"),
        ("record_unverifiable_entry_count", record.unverifiable_entries, "count"),
        ("record_unsafe_entry_count", record.unsafe_entries, "count"),
        ("record_malformed_entry_count", record.malformed_entries, "count"),
        ("wheel_record_integrity_current", int(record.current), "boolean"),
    )
    project_key = f"package:{context.project_name}"
    return tuple(
        _metric(
            INSTALLED_PACKAGE_PROVIDER_ID,
            subject_key=project_key,
            category="package_integrity",
            name=name,
            value=value,
            unit=unit,
            metadata={
                **context.common_metadata,
                "installed_version": context.project_row.version,
                "record_sha256": record.digest,
            },
        )
        for name, value, unit in record_values
    )


def _summary_metrics(
    context: _InventoryContext,
    outputs: _PackageInventoryOutputs,
    summary: _InventorySummary,
) -> tuple[ExternalProviderMetric, ...]:
    summary_key = "project:installed-environment"
    summary_values = (
        ("inventory_observed_at_unix_seconds", int(context.observed.timestamp()), "unix_seconds"),
        ("inventory_current_at_observation", 1, "boolean"),
        ("installed_distribution_count", len(context.rows), "count"),
        ("declared_requirement_relation_count", len(outputs.requirement_edges), "count"),
        ("pyproject_direct_requirement_count", len(context.base_evaluations), "count"),
        ("pyproject_direct_requirement_installed_count", summary.required_installed, "count"),
        (
            "pyproject_required_applicable_dependency_count",
            len(summary.applicable_evaluations),
            "count",
        ),
        ("pyproject_required_missing_dependency_count", summary.required_missing, "count"),
        (
            "pyproject_required_version_compatible_count",
            summary.required_compatible,
            "count",
        ),
        ("pyproject_required_version_mismatch_count", summary.required_mismatch, "count"),
        ("pyproject_optional_dependency_count", len(summary.optional_declarations), "count"),
    )
    metrics = [
        _metric(
            INSTALLED_PACKAGE_PROVIDER_ID,
            subject_key=summary_key,
            category="package_integrity",
            name=name,
            value=value,
            unit=unit,
            metadata=context.common_metadata,
        )
        for name, value, unit in summary_values
    ]
    metrics.extend(
        _metric(
            INSTALLED_PACKAGE_PROVIDER_ID,
            subject_key=summary_key,
            category="license_inventory",
            name=name,
            value=value,
            metadata={**context.common_metadata, "legal_compatibility_assessed": False},
        )
        for name, value in (
            ("packages_with_license_metadata", outputs.license_available),
            ("packages_with_ambiguous_license_metadata", outputs.license_ambiguous),
            ("packages_without_license_metadata", outputs.license_missing),
        )
    )
    return tuple(metrics)


def _build_installed_inventory_execution(
    context: _InventoryContext,
    outputs: _PackageInventoryOutputs,
    summary: _InventorySummary,
) -> InstalledPackageInventoryExecution:
    metrics = list(outputs.metrics)
    metrics.extend(_record_metrics(context))
    metrics.extend(_summary_metrics(context, outputs, summary))
    relations = list(outputs.relations)
    relations.extend(
        _package_requirement_relations(
            outputs.requirement_edges,
            rows_by_name=context.rows_by_name,
            common_metadata=context.common_metadata,
        )
    )
    relations.extend(_project_dependency_relations(context))
    record = context.record
    counters = InstalledPackageCounters(
        len(context.rows),
        len(outputs.requirement_edges),
        len(context.base_evaluations),
        len(summary.applicable_evaluations),
        summary.required_installed,
        summary.required_missing,
        summary.required_compatible,
        summary.required_mismatch,
        len(summary.optional_declarations),
        outputs.license_available,
        outputs.license_ambiguous,
        outputs.license_missing,
        record.entries,
        record.hash_verified,
        record.size_verified,
        record.missing_files,
        record.hash_mismatches,
        record.size_mismatches,
        record.unverifiable_entries,
        record.unsafe_entries,
    )
    return InstalledPackageInventoryExecution(
        tuple(sorted(metrics, key=lambda item: item.portable_metric_id)),
        tuple(sorted(relations, key=lambda item: item.portable_relation_id)),
        counters,
        "python importlib.metadata and installed wheel RECORD",
        context.observed_text,
        context.observed.date().isoformat(),
        context.snapshot_id,
        "current_at_observation_only",
        context.pyproject_digest,
        context.project_row.version,
        record.files_hashed,
        record.bytes_hashed,
        0,
        INSTALLED_PACKAGE_USES_NETWORK,
        _INVENTORY_LIMITATIONS,
    )


def execute_installed_package_inventory(
    pyproject_path: Path,
    *,
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
    installation_root: Path | None = None,
    observed_at: datetime | None = None,
) -> InstalledPackageInventoryExecution:
    """Inventory installed metadata and verify the framework wheel's RECORD."""

    context = _prepare_inventory_context(
        pyproject_path,
        distributions=distributions,
        installation_root=installation_root,
        observed_at=observed_at,
    )
    outputs = _build_package_inventory_outputs(context)
    return _build_installed_inventory_execution(context, outputs, _inventory_summary(context))


__all__ = [
    "INSTALLED_PACKAGE_PROVIDER_ID",
    "INSTALLED_PACKAGE_PROVIDER_SCHEMA",
    "INSTALLED_PACKAGE_USES_NETWORK",
    "PIP_AUDIT_LIMITATIONS",
    "PIP_AUDIT_PROVIDER_ID",
    "PIP_AUDIT_PROVIDER_SCHEMA",
    "PIP_AUDIT_SERVICE",
    "PIP_AUDIT_USES_NETWORK",
    "InstalledPackageCounters",
    "InstalledPackageInventoryExecution",
    "PipAuditCounters",
    "PipAuditExecution",
    "execute_installed_package_inventory",
    "execute_pip_audit_known_vulnerabilities",
    "installed_environment_distributions",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.external_supply_chain_audit")
