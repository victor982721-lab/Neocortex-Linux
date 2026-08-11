"""Public contract tests for Reproducible Derivations v1."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.derivation_contracts import (
    DERIVATION_CONTRACT_SCHEMA_VERSION,
    CapabilityFailure,
    DerivationRef,
    InputBinding,
    MaterializationRef,
    OutputBinding,
    ReproducibilityClass,
    StageDescriptor,
    WorkExecutionMode,
    WorkOutcome,
    WorkReceipt,
)
from _04_Nucleo_Operativo.knowledge_contracts import (
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _references() -> tuple[ResourceRef, RevisionRef, MaterializationRef]:
    resource = ResourceRef(
        resource_id="resource:text:1",
        source_kind="text",
        owner="text",
        current_path="informe.txt",
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id="revision:text:1",
        producer="text.extract",
        processing_signature="text-extract-v1:fixture",
        generation=1,
        state=RevisionState.CURRENT,
        observed_at_utc="2026-08-11T12:00:00Z",
    )
    materialization = MaterializationRef(
        owner="text",
        kind="canonical_text",
        materialization_id="text-document:1",
        schema_version=2,
        resource=resource,
        revision=revision,
        generation=1,
    )
    return resource, revision, materialization


def _successful_receipt(
    *,
    execution_mode: WorkExecutionMode = WorkExecutionMode.EXECUTED,
    reproducibility: ReproducibilityClass = ReproducibilityClass.EXACT,
    stage: StageDescriptor | None = None,
) -> WorkReceipt:
    _, revision, materialization = _references()
    input_fingerprint = fingerprint_text("fuente").xxh3_128
    output_fingerprint = fingerprint_text("salida").xxh3_128
    return WorkReceipt(
        receipt_id="receipt:text:1",
        owner="text",
        stage=stage
        or StageDescriptor(
            stage_id="text.extract",
            stage_version="1",
            processing_signature="text-extract-v1:fixture",
            implementation_digest="git:fixture",
            provider="builtin",
            provider_version="0.9.0",
        ),
        inputs=(
            InputBinding(
                name="source",
                revision=revision,
                fingerprint=input_fingerprint,
            ),
        ),
        outputs=(
            OutputBinding(
                name="canonical_text",
                materialization=materialization,
                fingerprint=output_fingerprint,
            ),
        ),
        effective_configuration=(("normalize_newlines", True), ("encoding", "utf-8")),
        runtime=(("python", "3.14.1"), ("platform", "linux-x86_64")),
        started_at_utc="2026-08-11T12:00:00Z",
        finished_at_utc="2026-08-11T12:00:00.002Z",
        duration_ns=2_000_000,
        attempt=1,
        outcome=WorkOutcome.SUCCEEDED,
        execution_mode=execution_mode,
        reproducibility=reproducibility,
        run_id="run:1",
        correlation_id="correlation:1",
    )


def test_public_vocabulary_is_versioned_and_does_not_duplicate_evidence_ref() -> None:
    import _04_Nucleo_Operativo.derivation_contracts as contracts

    assert DERIVATION_CONTRACT_SCHEMA_VERSION == 1
    assert tuple(member.value for member in ReproducibilityClass) == (
        "exact",
        "environment_bound",
        "seeded",
        "equivalent",
        "best_effort",
        "non_replayable",
    )
    assert tuple(member.value for member in WorkOutcome) == (
        "succeeded",
        "failed",
        "cancelled",
        "abandoned",
    )
    assert tuple(member.value for member in WorkExecutionMode) == (
        "executed",
        "cache_hit",
        "replay",
        "attempted",
        "unknown",
    )
    assert "EvidenceRef" not in contracts.__all__
    assert not hasattr(contracts, "EvidenceRef")


def test_receipt_serialization_is_canonical_bounded_and_identity_stable() -> None:
    receipt = _successful_receipt()
    payload = receipt.to_dict()

    assert receipt.effective_configuration == (
        ("encoding", "utf-8"),
        ("normalize_newlines", True),
    )
    assert receipt.runtime == (
        ("platform", "linux-x86_64"),
        ("python", "3.14.1"),
    )
    assert payload["schema_version"] == 1
    assert payload["kind"] == "work_receipt"
    assert payload["outcome"] == "succeeded"
    assert payload["execution_mode"] == "executed"
    assert payload["reproducibility"] == "exact"
    assert json.loads(receipt.to_json()) == payload
    assert receipt.to_json() == _successful_receipt().to_json()
    assert receipt.contract_fingerprint == _successful_receipt().contract_fingerprint
    assert receipt.contract_fingerprint.startswith("derivation-contract-v1:xxh3-128:")
    assert WorkReceipt.from_dict(payload) == receipt
    assert WorkReceipt.from_json(receipt.to_json()) == receipt
    with pytest.raises(FrozenInstanceError):
        receipt.owner = "other"  # type: ignore[misc]

    noncanonical = dict(payload)
    noncanonical["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        WorkReceipt.from_dict(noncanonical)

    false_success = dict(payload)
    false_success["execution_mode"] = "unknown"
    with pytest.raises(ValueError, match="succeeded receipt must use"):
        WorkReceipt.from_dict(false_success)


def test_exact_claim_requires_identified_stage_runtime_and_model() -> None:
    with pytest.raises(ValueError, match="implementation_digest"):
        _successful_receipt(
            stage=StageDescriptor(
                stage_id="text.extract",
                stage_version="1",
                processing_signature="text-extract-v1:fixture",
            )
        )

    model_stage = StageDescriptor(
        stage_id="semantic.embed",
        stage_version="1",
        processing_signature="semantic-v1:fixture",
        implementation_digest="git:fixture",
        provider="local",
        model="fixture-model",
    )
    with pytest.raises(ValueError, match="model_version and model_digest"):
        _successful_receipt(stage=model_stage)


@pytest.mark.parametrize(
    "execution_mode",
    (WorkExecutionMode.ATTEMPTED, WorkExecutionMode.UNKNOWN),
)
def test_success_never_claims_attempted_or_unknown_execution(
    execution_mode: WorkExecutionMode,
) -> None:
    with pytest.raises(ValueError, match="must use executed, cache_hit or replay"):
        _successful_receipt(execution_mode=execution_mode)


@pytest.mark.parametrize("execution_mode", (WorkExecutionMode.CACHE_HIT, WorkExecutionMode.REPLAY))
def test_reuse_receipts_require_explicit_causation(
    execution_mode: WorkExecutionMode,
) -> None:
    with pytest.raises(ValueError, match="require a causation_id"):
        _successful_receipt(execution_mode=execution_mode)


@pytest.mark.parametrize(
    ("outcome", "failure", "execution_mode"),
    [
        (WorkOutcome.FAILED, "provider_timeout", WorkExecutionMode.ATTEMPTED),
        (WorkOutcome.CANCELLED, "user_cancelled", WorkExecutionMode.ATTEMPTED),
        (WorkOutcome.ABANDONED, "worker_disappeared", WorkExecutionMode.UNKNOWN),
    ],
)
def test_unsuccessful_receipts_never_claim_outputs(
    outcome: WorkOutcome,
    failure: str,
    execution_mode: WorkExecutionMode,
) -> None:
    successful = _successful_receipt()
    terminal = WorkReceipt(
        receipt_id=f"receipt:{failure}",
        owner=successful.owner,
        stage=successful.stage,
        inputs=successful.inputs,
        outputs=(),
        effective_configuration=successful.effective_configuration,
        runtime=successful.runtime,
        started_at_utc=successful.started_at_utc,
        finished_at_utc=successful.finished_at_utc,
        duration_ns=successful.duration_ns,
        attempt=successful.attempt,
        outcome=outcome,
        execution_mode=execution_mode,
        reproducibility=ReproducibilityClass.BEST_EFFORT,
        run_id=successful.run_id,
        correlation_id=successful.correlation_id,
        failure=CapabilityFailure(
            capability_id="text.extract",
            reason_code=failure,
            message="El intento no publicó una salida.",
            retryable=outcome is not WorkOutcome.ABANDONED,
        ),
    )

    assert terminal.outputs == ()
    assert terminal.to_dict()["failure"] is not None
    with pytest.raises(ValueError, match="cannot claim committed outputs"):
        WorkReceipt(
            receipt_id=terminal.receipt_id,
            owner=terminal.owner,
            stage=terminal.stage,
            inputs=terminal.inputs,
            outputs=successful.outputs,
            effective_configuration=terminal.effective_configuration,
            runtime=terminal.runtime,
            started_at_utc=terminal.started_at_utc,
            finished_at_utc=terminal.finished_at_utc,
            duration_ns=terminal.duration_ns,
            attempt=terminal.attempt,
            outcome=terminal.outcome,
            execution_mode=terminal.execution_mode,
            reproducibility=terminal.reproducibility,
            run_id=terminal.run_id,
            correlation_id=terminal.correlation_id,
            failure=terminal.failure,
        )


def test_receipt_size_is_bounded_by_the_projection_contract() -> None:
    successful = _successful_receipt()
    _, _, materialization = _references()
    oversized_outputs = tuple(
        OutputBinding(
            name=f"output-{index:04d}",
            materialization=replace(
                materialization,
                materialization_id=f"materialization:text:{index:04d}",
            ),
            fingerprint=f"fingerprint-{index:04d}",
        )
        for index in range(1_500)
    )

    with pytest.raises(ValueError, match="WorkReceipt JSON cannot exceed"):
        replace(successful, outputs=oversized_outputs)


def test_bindings_are_logical_and_reject_mismatched_revisions() -> None:
    resource, revision, materialization = _references()
    derivation = DerivationRef(
        derivation_id="derivation:text:1",
        receipt_id="receipt:text:1",
        materialization=materialization,
    )

    materialization_payload = derivation.to_dict()["materialization"]
    assert isinstance(materialization_payload, dict)
    assert materialization_payload["resource"] == resource.to_dict()
    assert not {"path", "locator", "uri"}.intersection(materialization.to_dict())

    different_revision = RevisionRef(
        resource_id=revision.resource_id,
        revision_id="revision:text:other",
        producer=revision.producer,
        processing_signature=revision.processing_signature,
        generation=revision.generation,
        state=revision.state,
    )
    with pytest.raises(ValueError, match="match exactly"):
        InputBinding(
            name="source",
            revision=different_revision,
            fingerprint="fixture",
            materialization=materialization,
        )

    conflicting_facts = replace(
        revision,
        processing_signature="text-extract-v2:conflict",
    )
    with pytest.raises(ValueError, match="match exactly"):
        InputBinding(
            name="source",
            revision=revision,
            fingerprint="fixture",
            materialization=replace(materialization, revision=conflicting_facts),
        )


def test_reference_contracts_reject_wrong_types_and_cross_resource_facts() -> None:
    resource, revision, materialization = _references()
    other_resource = replace(resource, resource_id="resource:text:other")

    with pytest.raises(ValueError, match="provider_version requires provider"):
        StageDescriptor("text.extract", "1", "signature", provider_version="1")
    with pytest.raises(ValueError, match="require model"):
        StageDescriptor("semantic.embed", "1", "signature", model_version="1")
    with pytest.raises(ValueError, match="resource must be a ResourceRef"):
        replace(materialization, resource="not-a-resource")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="revision must be a RevisionRef"):
        replace(materialization, revision="not-a-revision")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identify the same resource"):
        replace(materialization, resource=other_resource)
    with pytest.raises(ValueError, match="revision must be a RevisionRef"):
        InputBinding("source", "not-a-revision", "fingerprint")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="materialization must be a MaterializationRef"):
        InputBinding(
            "source",
            revision,
            "fingerprint",
            materialization="not-a-materialization",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="materialization must be a MaterializationRef"):
        OutputBinding(
            "output",
            "not-a-materialization",  # type: ignore[arg-type]
            "fingerprint",
        )
    with pytest.raises(ValueError, match="retryable must be a bool"):
        CapabilityFailure("text.extract", "failed", "failure", retryable=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="materialization must be a MaterializationRef"):
        DerivationRef(
            "derivation:text:bad",
            "receipt:text:bad",
            "not-a-materialization",  # type: ignore[arg-type]
        )


def test_receipt_contract_rejects_impossible_terminal_state_combinations() -> None:
    receipt = _successful_receipt(reproducibility=ReproducibilityClass.ENVIRONMENT_BOUND)
    failure = CapabilityFailure("text.extract", "failed", "failure", retryable=True)

    with pytest.raises(ValueError, match="stage must be a StageDescriptor"):
        replace(receipt, stage="not-a-stage")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="inputs must contain at least one"):
        replace(receipt, inputs=())
    with pytest.raises(ValueError, match="cannot precede"):
        replace(receipt, finished_at_utc="2026-08-11T11:59:59Z")
    with pytest.raises(ValueError, match="duration_ns must be between"):
        replace(receipt, duration_ns=-1)
    with pytest.raises(ValueError, match="failure must be a CapabilityFailure"):
        replace(receipt, failure="not-a-failure")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must bind at least one output"):
        replace(receipt, outputs=())
    with pytest.raises(ValueError, match="cannot include a failure"):
        replace(receipt, failure=failure)
    with pytest.raises(ValueError, match="must explain its failure"):
        replace(
            receipt,
            outputs=(),
            outcome=WorkOutcome.FAILED,
            execution_mode=WorkExecutionMode.ATTEMPTED,
        )
    with pytest.raises(ValueError, match="must use attempted mode"):
        replace(
            receipt,
            outputs=(),
            outcome=WorkOutcome.CANCELLED,
            execution_mode=WorkExecutionMode.UNKNOWN,
            failure=failure,
        )
    with pytest.raises(ValueError, match="must explain its unknown state"):
        replace(
            receipt,
            outputs=(),
            outcome=WorkOutcome.ABANDONED,
            execution_mode=WorkExecutionMode.UNKNOWN,
        )
    with pytest.raises(ValueError, match="must use unknown mode"):
        replace(
            receipt,
            outputs=(),
            outcome=WorkOutcome.ABANDONED,
            execution_mode=WorkExecutionMode.ATTEMPTED,
            failure=failure,
        )
    with pytest.raises(ValueError, match="non_replayable work cannot"):
        replace(
            receipt,
            execution_mode=WorkExecutionMode.CACHE_HIT,
            reproducibility=ReproducibilityClass.NON_REPLAYABLE,
            causation_id="receipt:text:producer",
        )


def test_exact_and_json_receipts_fail_closed_on_ambiguous_replay_facts() -> None:
    receipt = _successful_receipt()

    with pytest.raises(ValueError, match="identified runtime"):
        replace(receipt, runtime=())
    with pytest.raises(ValueError, match="JSON must be a string"):
        WorkReceipt.from_json(receipt.to_dict())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="JSON is malformed"):
        WorkReceipt.from_json("{")
    with pytest.raises(ValueError, match="JSON is not canonical"):
        WorkReceipt.from_json(json.dumps(receipt.to_dict(), indent=2))


def test_configuration_is_scalar_immutable_and_fail_closed() -> None:
    successful = _successful_receipt()
    base = successful.to_dict()
    assert base["effective_configuration"] == {
        "encoding": "utf-8",
        "normalize_newlines": True,
    }

    with pytest.raises(ValueError, match="duplicate key"):
        replace(
            successful,
            effective_configuration=(
                ("encoding", "utf-8"),
                ("encoding", "latin-1"),
            ),
        )
    with pytest.raises(ValueError, match="must be redacted"):
        replace(
            successful,
            effective_configuration=(("provider_api_key", "not-a-secret-copy"),),
        )

    redacted = replace(
        successful,
        effective_configuration=(("provider_api_key", "[redacted]"),),
    )
    assert redacted.to_dict()["effective_configuration"] == {"provider_api_key": "[redacted]"}


@pytest.mark.parametrize(
    "key",
    ("apiKey", "clientSecret", "accessToken", "secretKey", "provider.api-key"),
)
def test_secret_shaped_keys_are_redacted_across_all_receipt_maps(key: str) -> None:
    successful = _successful_receipt()
    with pytest.raises(ValueError, match="must be redacted"):
        replace(successful, effective_configuration=((key, "SECRET-SENTINEL"),))
    with pytest.raises(ValueError, match="must be redacted"):
        replace(successful, runtime=((key, "SECRET-SENTINEL"),))
    with pytest.raises(ValueError, match="must be redacted"):
        CapabilityFailure(
            "text.extract",
            "provider_error",
            "Provider error.",
            True,
            details=((key, "SECRET-SENTINEL"),),
        )


def test_sdk_resolves_contracts_lazily_and_preserves_object_identity() -> None:
    script = textwrap.dedent(
        """
        import sys
        import neocortex.sdk as sdk

        module = "_04_Nucleo_Operativo.derivation_contracts"
        if module in sys.modules:
            raise SystemExit("derivation contracts loaded eagerly")
        resolved = sdk.WorkReceipt
        if module not in sys.modules:
            raise SystemExit("derivation contracts were not resolved")
        from _04_Nucleo_Operativo.derivation_contracts import WorkReceipt
        if resolved is not WorkReceipt:
            raise SystemExit("SDK wrapped the public contract")
        print("DERIVATION_SDK_LAZY_OK")
        """
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "DERIVATION_SDK_LAZY_OK" in completed.stdout
