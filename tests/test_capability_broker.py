"""Capability manifests and deterministic, explainable provider selection."""

from __future__ import annotations

import hashlib
import math
import os
import sys
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import pytest

import neocortex.capabilities.runtime as capabilities_module
import neocortex.capabilities.broker as broker_module
from neocortex.capabilities.broker import (
    CAPABILITY_MANIFEST_SCHEMA,
    CAPABILITY_SELECTION_SCHEMA,
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
)
from neocortex.capabilities.runtime import (
    CAPABILITY_MANIFESTS,
    RUNTIME_CAPABILITY_SCHEMA_VERSION,
    CapabilityState,
    RequirementKind,
    RuntimeCapabilityStatus,
    RuntimeComponentStatus,
    RuntimeRequirement,
    TEXT_BUILTIN_IMPLEMENTATION_ID,
    TEXT_EXTRACT_CAPABILITY_ID,
    TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID,
    TEXT_RAW_INPUT_SCHEMA,
    TEXT_REPRESENTATION_OUTPUT_SCHEMA,
    build_runtime_capability_broker,
    inspect_capability_implementation_availability,
)


def _manifest(
    implementation_id: str,
    *,
    mime_types: tuple[str, ...] = ("text/plain",),
    languages: tuple[str, ...] = (),
    deterministic: bool | None = True,
    privacy: CapabilityPrivacy = CapabilityPrivacy.LOCAL_ONLY,
    network_required: bool = False,
    ram_bytes: int | None = 256,
    estimated_cost: float | None = 0.0,
    estimated_latency_ms: float | None = 20.0,
    quality_metrics: tuple[CapabilityQualityMetric, ...] = (),
) -> CapabilityManifest:
    return CapabilityManifest(
        capability_id="text.extract",
        capability_version="2",
        implementation_id=implementation_id,
        provider="fixture-provider",
        provider_version="1.2.3",
        lifecycle=CapabilityLifecycle.PRODUCTION,
        supported_platforms=("linux", "windows"),
        modalities=("document",),
        input_schemas=("neocortex.raw-bytes/v1",),
        output_schemas=("neocortex.text-representation/v1",),
        mime_types=mime_types,
        language_mode=CapabilityLanguageMode.AGNOSTIC,
        languages=languages,
        deterministic=deterministic,
        reproducibility_classes=("environment_bound",),
        incremental=True,
        cancellation=True,
        checkpointing=False,
        max_input_bytes=1024,
        default_timeout_seconds=10.0,
        cpu_threads=1,
        ram_bytes=ram_bytes,
        gpu_required=False,
        network_required=network_required,
        privacy=privacy,
        optional_extra="documents",
        required_components=("xxhash",),
        required_binaries=(),
        mime_binary_alternatives=(),
        required_models=(),
        compatibility=("text-route-v2",),
        quality_metrics=quality_metrics,
        estimated_cost=estimated_cost,
        estimated_latency_ms=estimated_latency_ms,
    )


def _request(**overrides: object) -> CapabilityRequest:
    values: dict[str, object] = {
        "capability_id": "text.extract",
        "modality": "document",
        "input_schema": "neocortex.raw-bytes/v1",
        "output_schema": "neocortex.text-representation/v1",
        "platform": "linux",
        "mime_type": "text/plain",
        "language": "es",
        "input_bytes": 512,
        "workspace_id": "workspace:fixture",
        "acceptable_reproducibility": ("environment_bound",),
        "require_deterministic": True,
        "require_incremental": True,
        "require_cancellation": True,
        "require_checkpointing": False,
    }
    values.update(overrides)
    return CapabilityRequest(**values)  # type: ignore[arg-type]


def _availability(
    *manifests: CapabilityManifest,
    request: CapabilityRequest | None = None,
) -> tuple[CapabilityAvailability, ...]:
    effective_request = _request() if request is None else request
    return tuple(
        CapabilityAvailability(
            manifest.implementation_id,
            manifest.contract_fingerprint,
            effective_request.execution_contract_fingerprint,
            available=True,
            observed_components=("xxhash",),
        )
        for manifest in manifests
    )


def test_manifest_is_versioned_rich_and_canonically_serializable() -> None:
    metric = CapabilityQualityMetric(
        metric_id="text_coverage",
        value=0.98,
        unit="ratio",
        evidence="fixture-evaluation-v1",
        higher_is_better=True,
    )
    manifest = _manifest("fixture.local", quality_metrics=(metric,))

    payload = manifest.to_dict()

    assert payload["schema"] == CAPABILITY_MANIFEST_SCHEMA
    assert payload["capability_id"] == "text.extract"
    assert payload["implementation_id"] == "fixture.local"
    assert payload["provider"] == "fixture-provider"
    assert payload["lifecycle"] == "production"
    assert payload["supported_platforms"] == ["linux", "windows"]
    assert payload["modalities"] == ["document"]
    assert payload["mime_types"] == ["text/plain"]
    assert payload["language_mode"] == "agnostic"
    assert payload["languages"] == []
    assert payload["privacy"] == "local_only"
    assert payload["network_required"] is False
    assert payload["models_required"] == []
    assert payload["quality_metrics"] == [metric.to_dict()]
    assert manifest.contract_fingerprint.startswith("sha256:")


def test_set_like_contract_fields_have_order_independent_fingerprints() -> None:
    manifest = _manifest(
        "fixture.canonical",
        mime_types=("text/plain", "text/markdown"),
    )
    reordered_manifest = replace(
        manifest,
        supported_platforms=tuple(reversed(manifest.supported_platforms)),
        mime_types=tuple(reversed(manifest.mime_types)),
        compatibility=tuple(reversed(manifest.compatibility)),
    )
    request = _request(
        acceptable_reproducibility=("best_effort", "environment_bound"),
    )
    reordered_request = replace(
        request,
        acceptable_reproducibility=tuple(reversed(request.acceptable_reproducibility)),
    )
    policy = CapabilityPolicy(
        allowed_privacy=(
            CapabilityPrivacy.NETWORK_OPTIONAL,
            CapabilityPrivacy.LOCAL_ONLY,
        ),
        allowed_providers=("z", "a"),
    )
    reordered_policy = replace(
        policy,
        allowed_privacy=tuple(reversed(policy.allowed_privacy)),
        allowed_providers=tuple(reversed(policy.allowed_providers)),
    )

    assert reordered_manifest.contract_fingerprint == manifest.contract_fingerprint
    assert reordered_request.contract_fingerprint == request.contract_fingerprint
    assert reordered_policy.contract_fingerprint == policy.contract_fingerprint


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"implementation_id": " "}, "implementation_id cannot be blank"),
        ({"mime_types": ("text/plain", "text/plain")}, "mime_types cannot contain duplicates"),
        ({"network_required": True}, "local_only capability cannot require network"),
        ({"deterministic": False}, "exact reproducibility requires attested runtime"),
        ({"ram_bytes": 0}, "ram_bytes must be a positive integer"),
        ({"estimated_cost": math.nan}, "estimated_cost must be finite"),
        (
            {
                "mime_binary_alternatives": (
                    CapabilityMimeBinaryAlternatives(
                        "application/msword",
                        ("catdoc",),
                        "fixture_binary_unavailable",
                    ),
                )
            },
            "must reference declared MIME types",
        ),
    ),
)
def test_manifest_rejects_ambiguous_or_false_contracts(
    changes: dict[str, object],
    message: str,
) -> None:
    manifest = _manifest("fixture.invalid")
    if changes.get("deterministic") is False:
        changes = {**changes, "reproducibility_classes": ("exact",)}

    with pytest.raises(ValueError, match=message):
        replace(manifest, **changes)  # type: ignore[arg-type]


def test_broker_filters_hard_constraints_before_deterministic_preferences() -> None:
    local = _manifest(
        "z-local",
        quality_metrics=(CapabilityQualityMetric("text_coverage", 0.92, "ratio", "eval-v1", True),),
    )
    remote = _manifest(
        "a-remote",
        privacy=CapabilityPrivacy.NETWORK_REQUIRED,
        network_required=True,
        ram_bytes=64,
        estimated_latency_ms=1.0,
        quality_metrics=(CapabilityQualityMetric("text_coverage", 0.99, "ratio", "eval-v1", True),),
    )
    broker = CapabilityBroker(
        (remote, local),
        _availability(remote, local),
    )
    policy = CapabilityPolicy(
        policy_id="offline-private",
        allow_network=False,
        allowed_privacy=(CapabilityPrivacy.LOCAL_ONLY,),
        gpu_available=False,
        quality_requirements=(
            CapabilityQualityRequirement(
                "text_coverage",
                minimum=0.90,
                unit="ratio",
                evidence="eval-v1",
                prefer_higher=True,
            ),
        ),
    )

    selection = broker.select(_request(), policy)

    assert selection.selected is local
    evaluations = {item.implementation_id: item for item in selection.candidates}
    assert evaluations["a-remote"].eligible is False
    assert "network_forbidden" in evaluations["a-remote"].rejection_reasons
    assert "privacy_not_allowed:network_required" in (evaluations["a-remote"].rejection_reasons)
    assert evaluations["z-local"].eligible is True
    assert selection.to_dict()["schema"] == CAPABILITY_SELECTION_SCHEMA
    assert "selected:z-local" in selection.explanation


def test_broker_does_not_select_first_available_provider() -> None:
    slower = _manifest("a-slower", estimated_latency_ms=50.0)
    faster = _manifest("z-faster", estimated_latency_ms=5.0)
    broker = CapabilityBroker(
        (slower, faster),
        _availability(slower, faster),
    )

    selection = broker.select(_request(), CapabilityPolicy())

    assert selection.selected is faster
    assert selection.candidates[0].implementation_id == "a-slower"
    assert "estimated_latency_ms:5" in selection.explanation


def test_quality_evidence_must_be_comparable_before_it_can_rank() -> None:
    manifest = _manifest(
        "fixture.quality",
        quality_metrics=(CapabilityQualityMetric("text_coverage", 0.98, "ratio", "eval-a", True),),
    )
    policy = CapabilityPolicy(
        quality_requirements=(
            CapabilityQualityRequirement(
                "text_coverage",
                minimum=0.9,
                unit="ratio",
                evidence="eval-b",
                prefer_higher=True,
            ),
        )
    )

    selection = CapabilityBroker(
        (manifest,),
        _availability(manifest),
    ).select(_request(), policy)

    assert selection.selected is None
    assert selection.candidates[0].rejection_reasons == ("quality_evidence_mismatch:text_coverage",)


def test_quality_numeric_semantics_match_canonical_fingerprints() -> None:
    lower = CapabilityQualityMetric("q", 2**53, "units", "eval-v1", True)
    rounded = CapabilityQualityMetric("q", 2**53 + 1, "units", "eval-v1", True)
    lower_manifest = _manifest("fixture.numeric", quality_metrics=(lower,))
    rounded_manifest = _manifest("fixture.numeric", quality_metrics=(rounded,))
    lower_policy = CapabilityPolicy(
        quality_requirements=(CapabilityQualityRequirement("q", minimum=2**53, evidence="eval-v1"),)
    )
    rounded_policy = CapabilityPolicy(
        quality_requirements=(
            CapabilityQualityRequirement("q", minimum=2**53 + 1, evidence="eval-v1"),
        )
    )
    request = _request()

    assert lower.value == rounded.value
    assert lower_manifest.contract_fingerprint == rounded_manifest.contract_fingerprint
    assert lower_policy.contract_fingerprint == rounded_policy.contract_fingerprint
    availability = _availability(lower_manifest, request=request)
    lower_selection = CapabilityBroker((lower_manifest,), availability).select(
        request,
        lower_policy,
    )
    rounded_selection = CapabilityBroker((rounded_manifest,), availability).select(
        request,
        rounded_policy,
    )
    assert lower_selection.status == rounded_selection.status == "selected"


def test_exact_preference_tie_abstains_instead_of_using_registry_order() -> None:
    alpha = _manifest("alpha")
    beta = _manifest("beta")
    availability = _availability(alpha, beta)

    forward = CapabilityBroker((alpha, beta), availability).select(_request())
    reverse = CapabilityBroker((beta, alpha), tuple(reversed(availability))).select(_request())

    assert forward.selected is None
    assert reverse.selected is None
    assert (
        forward.explanation
        == reverse.explanation
        == (
            "unavailable:text.extract",
            "ambiguous_capability_selection:alpha,beta",
        )
    )
    assert forward.to_dict() == reverse.to_dict()


def test_long_valid_identifiers_produce_bounded_explanations_and_reasons() -> None:
    alpha = _manifest("a" * 300)
    beta = _manifest("b" * 300)
    request = _request()

    tie = CapabilityBroker(
        (alpha, beta),
        _availability(alpha, beta, request=request),
    ).select(request)

    assert tie.selected is None
    assert tie.explanation[1].startswith("ambiguous_capability_selection:")
    assert tie.explanation[1].endswith(
        hashlib.sha256(f"{alpha.implementation_id},{beta.implementation_id}".encode()).hexdigest()
    )
    assert all(len(item) <= broker_module.MAX_CAPABILITY_TEXT for item in tie.explanation)

    model = "m" * 500
    manifest = replace(_manifest("fixture.long-model"), required_models=(model,))
    selection = build_runtime_capability_broker(
        request,
        manifests=(manifest,),
        module_finder=lambda name: object() if name == "xxhash" else None,
        distribution_version=_runtime_version,
        executable_finder=lambda _name: None,
    ).select(request)

    assert selection.selected is None
    reason = selection.candidates[0].rejection_reasons[0]
    assert reason.startswith("required_model_unobserved:")
    assert reason.endswith(hashlib.sha256(model.encode()).hexdigest())
    assert len(reason) <= broker_module.MAX_CAPABILITY_TEXT

    with pytest.raises(ValueError, match="ram_bytes must be a positive integer"):
        replace(_manifest("fixture.huge-ram"), ram_bytes=10**600)

    long_missing_reason = "x" * 600
    requirement = RuntimeRequirement(
        component="xxhash",
        kind=RequirementKind.PYTHON_DISTRIBUTION,
        required=True,
        missing_reason=long_missing_reason,
        distribution="xxhash",
        module="xxhash",
    )
    unavailable = RuntimeCapabilityStatus(
        capability="text",
        state=CapabilityState.UNAVAILABLE,
        components=(RuntimeComponentStatus(requirement, available=False),),
        degradation_reasons=(long_missing_reason,),
    )
    missing_selection = build_runtime_capability_broker(
        request,
        statuses=(unavailable,),
        manifests=(_manifest("fixture.long-missing"),),
    ).select(request)
    observed_reason = missing_selection.candidates[0].rejection_reasons[0]
    assert len(observed_reason) <= broker_module.MAX_CAPABILITY_TEXT
    assert observed_reason.endswith(hashlib.sha256(long_missing_reason.encode()).hexdigest())


def test_bcp47_language_matching_is_case_insensitive_and_canonical() -> None:
    manifest = replace(
        _manifest("fixture.language"),
        language_mode=CapabilityLanguageMode.DECLARED,
        languages=("en-US",),
    )
    request = _request(language="en-us")

    selection = CapabilityBroker(
        (manifest,),
        _availability(manifest, request=request),
    ).select(request)

    assert selection.selected is manifest
    assert manifest.languages == ("en-us",)
    assert request.language == "en-us"
    with pytest.raises(ValueError, match="case-insensitive duplicates"):
        replace(manifest, languages=("en-US", "en-us"))


@pytest.mark.parametrize(
    ("capability_request", "policy", "reason"),
    (
        (_request(mime_type="application/pdf"), CapabilityPolicy(), "mime_type_unsupported"),
        (_request(input_bytes=2048), CapabilityPolicy(), "input_limit_exceeded"),
        (
            _request(acceptable_reproducibility=("exact",)),
            CapabilityPolicy(),
            "reproducibility_not_supported",
        ),
        (_request(platform=None), CapabilityPolicy(), "platform_required"),
        (_request(platform="darwin"), CapabilityPolicy(), "platform_unsupported"),
        (
            _request(),
            CapabilityPolicy(max_ram_bytes=128),
            "ram_budget_exceeded",
        ),
        (
            _request(),
            CapabilityPolicy(
                quality_requirements=(CapabilityQualityRequirement("unknown_metric", minimum=0.5),)
            ),
            "quality_metric_unavailable:unknown_metric",
        ),
    ),
)
def test_broker_abstains_with_exact_reasons(
    capability_request: CapabilityRequest,
    policy: CapabilityPolicy,
    reason: str,
) -> None:
    manifest = _manifest("fixture.local")
    selection = CapabilityBroker(
        (manifest,),
        _availability(manifest),
    ).select(capability_request, policy)

    assert selection.selected is None
    assert selection.status == "unavailable"
    assert reason in selection.candidates[0].rejection_reasons


def test_missing_runtime_observation_is_fail_closed() -> None:
    manifest = _manifest("fixture.unknown")

    selection = CapabilityBroker((manifest,), ()).select(_request())

    assert selection.selected is None
    assert selection.candidates[0].rejection_reasons == ("runtime_availability_unknown",)


def test_runtime_readiness_cannot_be_reused_for_a_different_request() -> None:
    manifest = _manifest(
        "fixture.bound-readiness",
        mime_types=("application/msword", "application/vnd.ms-excel"),
    )
    doc_request = _request(mime_type="application/msword")
    excel_request = _request(mime_type="application/vnd.ms-excel")
    broker = CapabilityBroker(
        (manifest,),
        _availability(manifest, request=doc_request),
    )

    selection = broker.select(excel_request)

    assert selection.selected is None
    assert selection.candidates[0].rejection_reasons == ("runtime_observation_request_mismatch",)


def test_runtime_readiness_cannot_be_reused_for_a_changed_manifest() -> None:
    original = _manifest("fixture.manifest-bound")
    changed = replace(original, required_models=("new-required-model",))
    request = _request()
    broker = CapabilityBroker(
        (changed,),
        _availability(original, request=request),
    )

    selection = broker.select(request)

    assert selection.selected is None
    assert selection.candidates[0].rejection_reasons == ("runtime_observation_manifest_mismatch",)


def test_shadow_provider_requires_explicit_policy_and_fingerprints_the_decision() -> None:
    manifest = replace(_manifest("fixture.shadow"), lifecycle=CapabilityLifecycle.SHADOW)
    broker = CapabilityBroker((manifest,), _availability(manifest))

    rejected = broker.select(_request())
    accepted = broker.select(_request(), CapabilityPolicy(allow_shadow=True))

    assert rejected.selected is None
    assert rejected.candidates[0].rejection_reasons == ("shadow_not_allowed",)
    assert accepted.selected is manifest
    assert rejected.causal_fingerprint != accepted.causal_fingerprint
    payload = accepted.to_dict()
    assert isinstance(payload["request_fingerprint"], str)
    assert isinstance(payload["policy_fingerprint"], str)
    assert payload["request_fingerprint"].startswith("sha256:")
    assert payload["policy_fingerprint"].startswith("sha256:")
    assert payload["selected_manifest_fingerprint"] == manifest.contract_fingerprint


def test_execution_fingerprint_tracks_provider_policy_and_readiness_not_input_size() -> None:
    manifest = _manifest("fixture.fingerprint")
    request_10 = _request(input_bytes=10)
    request_11 = _request(input_bytes=11)
    availability_10 = CapabilityAvailability(
        "fixture.fingerprint",
        manifest.contract_fingerprint,
        request_10.execution_contract_fingerprint,
        available=True,
        observed_components=("xxhash@3.6.0",),
    )
    availability_11 = replace(
        availability_10,
        execution_request_fingerprint=request_11.execution_contract_fingerprint,
    )
    broker_10 = CapabilityBroker((manifest,), (availability_10,))
    first = broker_10.select(request_10)
    other_size = CapabilityBroker((manifest,), (availability_11,)).select(request_11)
    changed_provider = replace(manifest, provider_version="2.0.0")
    provider_change = CapabilityBroker(
        (changed_provider,),
        (
            replace(
                availability_10,
                manifest_fingerprint=changed_provider.contract_fingerprint,
            ),
        ),
    ).select(request_10)
    readiness_change = CapabilityBroker(
        (manifest,),
        (
            replace(
                availability_10,
                observed_components=("xxhash@3.7.0",),
            ),
        ),
    ).select(request_10)
    policy_change = broker_10.select(
        request_10,
        CapabilityPolicy(policy_id="fixture-policy-v2"),
    )
    windows_request = _request(input_bytes=10, platform="windows")
    windows = CapabilityBroker(
        (manifest,),
        _availability(manifest, request=windows_request),
    ).select(windows_request)

    assert first.causal_fingerprint != other_size.causal_fingerprint
    assert first.execution_fingerprint == other_size.execution_fingerprint
    assert first.execution_fingerprint != provider_change.execution_fingerprint
    assert first.execution_fingerprint != readiness_change.execution_fingerprint
    assert first.execution_fingerprint != policy_change.execution_fingerprint
    assert first.execution_fingerprint != windows.execution_fingerprint


def test_runtime_observations_are_canonical_and_duplicate_rejections_do_not_crash() -> None:
    manifest = _manifest("fixture.observed")
    request = _request(mime_type="application/pdf", platform="darwin")
    observed_reasons = (
        "mime_type_unsupported",
        "additional_rejections_truncated",
        *(f"observed_reason_{index:02d}" for index in range(14)),
    )
    availability = CapabilityAvailability(
        manifest.implementation_id,
        manifest.contract_fingerprint,
        request.execution_contract_fingerprint,
        available=False,
        reasons=tuple(reversed(observed_reasons)),
        observed_components=("z-runtime", "a-runtime"),
    )

    selection = CapabilityBroker((manifest,), (availability,)).select(request)

    reasons = selection.candidates[0].rejection_reasons
    assert selection.selected is None
    assert len(reasons) == 16
    assert len(reasons) == len(set(reasons))
    assert reasons[-1] == "additional_rejections_truncated"
    assert "platform_unsupported" in reasons
    assert "mime_type_unsupported" in reasons
    assert availability.observed_components == ("a-runtime", "z-runtime")


def test_quality_collections_are_immutable_tuples() -> None:
    manifest = _manifest("fixture.immutable")
    metric = CapabilityQualityMetric("coverage", 1.0, "ratio", "eval-v1", True)
    requirement = CapabilityQualityRequirement("coverage", evidence="eval-v1")

    with pytest.raises(ValueError, match="quality_metrics must be a tuple"):
        replace(manifest, quality_metrics=[metric])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="quality_requirements must be a tuple"):
        CapabilityPolicy(quality_requirements=[requirement])  # type: ignore[arg-type]


@pytest.mark.parametrize("mime_type", ("a/", "/b", "a/b/c", "text/*", "Text/plain"))
def test_mime_contracts_require_one_canonical_exact_value(mime_type: str) -> None:
    with pytest.raises(ValueError, match="canonical exact MIME"):
        _request(mime_type=mime_type)


@pytest.mark.parametrize("language", ("e", "es_419", "unknown-extra", "es-"))
def test_language_contracts_reject_ambiguous_tags(language: str) -> None:
    with pytest.raises(ValueError, match="BCP-47 tag or unknown"):
        _request(language=language)


def test_unknown_language_must_be_explicit() -> None:
    manifest = _manifest("fixture.language")
    request = _request(language=None)
    selection = CapabilityBroker(
        (manifest,),
        _availability(manifest, request=request),
    ).select(request)

    assert selection.selected is None
    assert selection.candidates[0].rejection_reasons == ("language_required",)


def test_integer_resource_contracts_reject_floats() -> None:
    manifest = _manifest("fixture.integer")

    with pytest.raises(ValueError, match="ram_bytes must be a positive integer"):
        replace(manifest, ram_bytes=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_ram_bytes must be a positive integer"):
        CapabilityPolicy(max_ram_bytes=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="input_bytes must be a non-negative integer"):
        _request(input_bytes=broker_module.MAX_CAPABILITY_INTEGER + 1)


def test_policy_payload_and_readiness_work_are_bounded_before_selection() -> None:
    oversized_values = tuple(f"provider-{index:02d}-" + "ñ" * 490 for index in range(64))
    with pytest.raises(ValueError, match="capability policy cannot exceed"):
        CapabilityPolicy(allowed_providers=oversized_values)

    request = _request()
    manifest = replace(
        _manifest("fixture.too-many-binaries"),
        required_components=(),
        required_binaries=tuple(f"binary-{index}" for index in range(17)),
    )
    status = RuntimeCapabilityStatus(
        capability="text",
        state=CapabilityState.AVAILABLE,
        components=(),
        degradation_reasons=(),
    )

    selection = build_runtime_capability_broker(
        request,
        statuses=(status,),
        manifests=(manifest,),
        executable_finder=lambda _name: pytest.fail("oversized readiness must not hash binaries"),
    ).select(request)

    assert selection.selected is None
    assert selection.candidates[0].rejection_reasons == (
        "manifest_readiness_evidence_limit_exceeded:required_binaries",
    )

    candidates = tuple(
        replace(
            _manifest(f"fixture.candidate-{index}"),
            required_components=(),
            required_binaries=("fixture-binary",),
        )
        for index in range(5)
    )
    broker = build_runtime_capability_broker(
        request,
        manifests=candidates,
        module_finder=lambda _name: pytest.fail("candidate overflow must skip runtime probes"),
        executable_finder=lambda _name: pytest.fail("candidate overflow must skip binary probes"),
    )
    assert broker.select(request).explanation[1].startswith("candidate_limit_exceeded:5:")

    with pytest.raises(ValueError, match="alternatives cannot contain more than 4"):
        CapabilityMimeBinaryAlternatives(
            "application/msword",
            tuple(f"backend-{index}" for index in range(5)),
            "fixture_backend_unavailable",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_binary_hashing_stops_at_the_attested_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = tmp_path / "backend"
    backend.write_bytes(b"x")
    backend.chmod(0o755)
    real_access = capabilities_module.os.access

    def grow_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
    ) -> bool:
        backend.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
        backend.chmod(0o755)
        return real_access(path, mode)

    class CountingDigest:
        def __init__(self) -> None:
            self.bytes_hashed = 0

        def update(self, payload: bytes) -> None:
            self.bytes_hashed += len(payload)

        def hexdigest(self) -> str:
            return "0" * 64

    digests: list[CountingDigest] = []

    def digest_factory(_payload: bytes = b"") -> CountingDigest:
        digest = CountingDigest()
        digest.update(_payload)
        digests.append(digest)
        return digest

    monkeypatch.setattr(capabilities_module.os, "access", grow_before_open)
    monkeypatch.setattr(capabilities_module.hashlib, "sha256", digest_factory)

    identity = capabilities_module._binary_identity("backend", lambda _name: str(backend))

    assert identity is None
    assert digests[0].bytes_hashed == 16 * 1024 * 1024


def test_cpu_budget_is_a_hard_explainable_filter() -> None:
    manifest = _manifest("fixture.cpu")
    request = _request()
    selection = CapabilityBroker(
        (manifest,),
        _availability(manifest, request=request),
    ).select(request, CapabilityPolicy(max_cpu_threads=1))

    assert selection.selected is manifest
    high_cpu = replace(manifest, cpu_threads=2)
    rejected = CapabilityBroker(
        (high_cpu,),
        _availability(high_cpu, request=request),
    ).select(request, CapabilityPolicy(max_cpu_threads=1))
    assert rejected.selected is None
    assert rejected.candidates[0].rejection_reasons == ("cpu_budget_exceeded",)


def test_network_optional_provider_is_not_selected_by_offline_policy() -> None:
    manifest = _manifest(
        "fixture.network-optional",
        privacy=CapabilityPrivacy.NETWORK_OPTIONAL,
    )
    request = _request()
    selection = CapabilityBroker(
        (manifest,),
        _availability(manifest, request=request),
    ).select(
        request,
        CapabilityPolicy(
            allow_network=False,
            allowed_privacy=(
                CapabilityPrivacy.LOCAL_ONLY,
                CapabilityPrivacy.NETWORK_OPTIONAL,
            ),
        ),
    )

    assert selection.selected is None
    assert "network_forbidden" in selection.candidates[0].rejection_reasons


def test_required_models_are_fail_closed_until_readiness_is_attested() -> None:
    request = _request()
    manifest = replace(
        _manifest("fixture.model"),
        required_models=("fixture-model-v1",),
    )
    status = capabilities_module.inspect_runtime_capability(
        "text",
        module_finder=lambda name: object() if name == "xxhash" else None,
        distribution_version=_runtime_version,
        executable_finder=lambda _name: None,
    )

    availability = inspect_capability_implementation_availability(
        request,
        (status,),
        manifests=(manifest,),
        executable_finder=lambda _name: None,
    )
    selection = CapabilityBroker((manifest,), availability).select(request)

    assert selection.selected is None
    assert selection.candidates[0].rejection_reasons == (
        "required_model_unobserved:fixture-model-v1",
    )


def test_policy_names_estimated_limits_as_estimates() -> None:
    policy = CapabilityPolicy(
        max_estimated_cost=2.0,
        max_estimated_latency_ms=50.0,
    )

    assert policy.to_dict()["max_estimated_cost"] == 2.0
    assert policy.to_dict()["max_estimated_latency_ms"] == 50.0


def test_manifest_and_request_iterables_are_bounded() -> None:
    def manifests():
        for index in range(130):
            yield _manifest(f"fixture-{index}")

    with pytest.raises(ValueError, match="cannot exceed 128"):
        CapabilityBroker(manifests(), ())
    with pytest.raises(ValueError, match="cannot exceed 128"):
        build_runtime_capability_broker(_request(), manifests=manifests())


def test_excess_candidates_abstain_with_bounded_registry_fingerprint() -> None:
    request = _request()
    manifests = tuple(_manifest(f"fixture-{index}") for index in range(20))

    selection = CapabilityBroker(
        manifests,
        _availability(*manifests, request=request),
    ).select(request)

    assert selection.selected is None
    assert selection.candidates == ()
    assert selection.explanation[1].startswith("candidate_limit_exceeded:20:sha256:")
    assert selection.contract_fingerprint.startswith("sha256:")


def test_selected_explanation_reserves_space_for_preference_evidence() -> None:
    metrics = tuple(
        CapabilityQualityMetric(f"metric-{index}", 1.0, "ratio", "eval-v1", True)
        for index in range(12)
    )
    requirements = tuple(
        CapabilityQualityRequirement(
            metric.metric_id,
            evidence="eval-v1",
            prefer_higher=True,
        )
        for metric in metrics
    )
    manifest = _manifest("fixture.preferences", quality_metrics=metrics)
    request = _request()

    selection = CapabilityBroker(
        (manifest,),
        _availability(manifest, request=request),
    ).select(request, CapabilityPolicy(quality_requirements=requirements))

    assert selection.selected is manifest
    assert len(selection.explanation) == 18
    assert selection.explanation[0] == "selected:fixture.preferences"
    assert selection.explanation[-1] == "additional_preferences_truncated"


def _runtime_version(distribution: str) -> str:
    if distribution == "xxhash":
        return "3.8.0"
    raise metadata.PackageNotFoundError(distribution)


def _text_request(mime_type: str) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=TEXT_EXTRACT_CAPABILITY_ID,
        modality="document",
        input_schema=TEXT_RAW_INPUT_SCHEMA,
        output_schema=TEXT_REPRESENTATION_OUTPUT_SCHEMA,
        platform="linux",
        mime_type=mime_type,
        language="unknown",
        input_bytes=64,
        workspace_id="workspace:fixture",
        acceptable_reproducibility=("environment_bound", "non_replayable"),
        require_incremental=mime_type
        not in {
            "application/msword",
            "application/vnd.ms-excel",
            "application/vnd.ms-powerpoint",
        },
    )


def test_builtin_text_manifests_are_additive_to_runtime_schema_v1() -> None:
    assert RUNTIME_CAPABILITY_SCHEMA_VERSION == 1
    assert tuple(item.implementation_id for item in CAPABILITY_MANIFESTS) == (
        TEXT_BUILTIN_IMPLEMENTATION_ID,
        TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID,
    )
    assert {item.capability_id for item in CAPABILITY_MANIFESTS} == {TEXT_EXTRACT_CAPABILITY_ID}
    by_id = {item.implementation_id: item for item in CAPABILITY_MANIFESTS}
    assert by_id[TEXT_BUILTIN_IMPLEMENTATION_ID].incremental is True
    assert by_id[TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID].incremental is False
    assert by_id[TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID].reproducibility_classes == (
        "best_effort",
        "non_replayable",
    )
    assert {
        item.mime_type: item.alternatives
        for item in by_id[TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID].mime_binary_alternatives
    } == {
        "application/msword": ("soffice", "libreoffice", "catdoc"),
        "application/vnd.ms-excel": ("xls2csv", "soffice", "libreoffice"),
        "application/vnd.ms-powerpoint": ("catppt", "soffice", "libreoffice"),
    }
    for name in (
        "CapabilityBroker",
        "CapabilityManifest",
        "CapabilityPolicy",
        "CapabilityRequest",
        "CapabilitySelection",
    ):
        assert name in capabilities_module.__all__
        assert getattr(capabilities_module, name) is getattr(broker_module, name)


def test_plain_text_selection_ignores_missing_optional_soffice() -> None:
    request = _text_request("text/plain")
    broker = build_runtime_capability_broker(
        request,
        module_finder=lambda name: object() if name == "xxhash" else None,
        distribution_version=_runtime_version,
        executable_finder=lambda _name: None,
    )

    selection = broker.select(request)

    assert selection.selected is not None
    assert selection.selected.implementation_id == TEXT_BUILTIN_IMPLEMENTATION_ID


def test_legacy_text_abstains_without_a_matching_backend_and_pins_exact_fallback(
    tmp_path: Path,
) -> None:
    request = _text_request("application/msword")
    backend = tmp_path / "catdoc"
    backend.write_bytes(b"#!/bin/sh\nprintf 'fixture text\\n'\n")
    backend.chmod(0o755)
    missing = build_runtime_capability_broker(
        request,
        module_finder=lambda name: object() if name == "xxhash" else None,
        distribution_version=_runtime_version,
        executable_finder=lambda _name: None,
    ).select(request)
    fallback = build_runtime_capability_broker(
        request,
        module_finder=lambda name: object() if name == "xxhash" else None,
        distribution_version=_runtime_version,
        executable_finder=(lambda name: str(backend) if name == "catdoc" else None),
    ).select(request)
    primary = tmp_path / "soffice"
    primary.write_bytes(b"#!/bin/sh\nprintf 'primary text\\n'\n")
    primary.chmod(0o755)
    both = build_runtime_capability_broker(
        request,
        module_finder=lambda name: object() if name == "xxhash" else None,
        distribution_version=_runtime_version,
        executable_finder=(
            lambda name: (
                str(primary) if name == "soffice" else str(backend) if name == "catdoc" else None
            )
        ),
    ).select(request)

    assert missing.selected is None
    missing_legacy = next(
        item
        for item in missing.candidates
        if item.implementation_id == TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID
    )
    assert missing_legacy.rejection_reasons == ("legacy_office_extractor_unavailable",)
    assert fallback.selected is not None
    assert fallback.selected.implementation_id == TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID
    evaluation = next(item for item in fallback.candidates if item.eligible)
    assert evaluation.availability is not None
    assert len(evaluation.availability.binary_identities) == 1
    identity = evaluation.availability.binary_identities[0]
    assert identity.name == "catdoc"
    assert identity.command == str(backend.resolve())
    assert identity.to_dict()["command_sha256"] != identity.to_dict()["artifact_sha256"]
    assert str(backend) not in str(fallback.to_dict())
    both_evaluation = next(item for item in both.candidates if item.eligible)
    assert both_evaluation.availability is not None
    assert tuple(item.name for item in both_evaluation.availability.binary_identities) == (
        "soffice",
    )


@pytest.mark.parametrize(
    ("mime_type", "specific_backend"),
    (
        ("application/vnd.ms-excel", "xls2csv"),
        ("application/vnd.ms-powerpoint", "catppt"),
    ),
)
def test_legacy_text_prefers_format_specific_backend_when_soffice_is_also_present(
    mime_type: str,
    specific_backend: str,
) -> None:
    request = _text_request(mime_type)
    command = os.fspath(Path(sys.executable).resolve())
    selection = build_runtime_capability_broker(
        request,
        module_finder=lambda name: object() if name == "xxhash" else None,
        distribution_version=_runtime_version,
        executable_finder=(lambda name: command if name in {specific_backend, "soffice"} else None),
    ).select(request)

    assert selection.selected is not None
    assert selection.selected.implementation_id == TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID
    evaluation = next(item for item in selection.candidates if item.eligible)
    assert evaluation.availability is not None
    assert tuple(item.name for item in evaluation.availability.binary_identities) == (
        specific_backend,
    )


@pytest.mark.parametrize(
    "mime_type",
    ("application/vnd.ms-excel", "application/vnd.ms-powerpoint"),
)
def test_legacy_text_uses_soffice_when_format_specific_backend_is_absent(
    mime_type: str,
) -> None:
    request = _text_request(mime_type)
    command = os.fspath(Path(sys.executable).resolve())
    selection = build_runtime_capability_broker(
        request,
        module_finder=lambda name: object() if name == "xxhash" else None,
        distribution_version=_runtime_version,
        executable_finder=lambda name: command if name == "soffice" else None,
    ).select(request)

    evaluation = next(item for item in selection.candidates if item.eligible)
    assert evaluation.availability is not None
    assert tuple(item.name for item in evaluation.availability.binary_identities) == ("soffice",)


def test_binary_artifact_and_location_change_execution_fingerprint(
    tmp_path: Path,
) -> None:
    request = _text_request("application/msword")
    first_backend = tmp_path / "first" / "catdoc"
    second_backend = tmp_path / "second" / "catdoc"
    first_backend.parent.mkdir()
    second_backend.parent.mkdir()
    first_backend.write_bytes(b"#!/bin/sh\nprintf A\n")
    second_backend.write_bytes(b"#!/bin/sh\nprintf A\n")
    first_backend.chmod(0o755)
    second_backend.chmod(0o755)

    def selection(path: Path):
        return build_runtime_capability_broker(
            request,
            module_finder=lambda name: object() if name == "xxhash" else None,
            distribution_version=_runtime_version,
            executable_finder=(lambda name: str(path) if name == "catdoc" else None),
        ).select(request)

    first = selection(first_backend)
    second = selection(second_backend)
    second_backend.write_bytes(b"#!/bin/sh\nprintf B\n")
    second_backend.chmod(0o755)
    changed_artifact = selection(second_backend)

    assert first.selected is not None
    assert second.selected is not None
    assert first.execution_fingerprint != second.execution_fingerprint
    assert second.execution_fingerprint != changed_artifact.execution_fingerprint


def test_binary_identity_contract_rejects_invalid_hashes() -> None:
    command = os.fspath(Path(sys.executable).resolve())
    with pytest.raises(ValueError, match="command_sha256"):
        CapabilityBinaryIdentity(
            name="fixture",
            command=command,
            command_sha256="invalid",
            artifact_sha256="0" * 64,
            size_bytes=0,
        )
    with pytest.raises(ValueError, match="must fingerprint command exactly"):
        CapabilityBinaryIdentity(
            name="fixture",
            command=command,
            command_sha256="0" * 64,
            artifact_sha256="0" * 64,
            size_bytes=0,
        )
