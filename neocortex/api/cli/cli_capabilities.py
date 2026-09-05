"""Canonical read-only runtime-capabilities doctor handler."""


# region [01] Imports and exit contract

from __future__ import annotations
import argparse
import json
import sys
from enum import IntEnum

from neocortex.capabilities.broker import (
    CAPABILITY_SELECTION_SCHEMA,
    CapabilityPolicy,
    CapabilityPrivacy,
    CapabilityRequest,
    CapabilitySelection,
)
from neocortex.capabilities.runtime import (
    RUNTIME_CAPABILITY_PROBE_POLICY,
    RUNTIME_CAPABILITY_SCHEMA_VERSION,
    CapabilityState,
    RuntimeCapabilityStatus,
    TEXT_RAW_INPUT_SCHEMA,
    TEXT_REPRESENTATION_OUTPUT_SCHEMA,
    build_runtime_capability_broker,
    inspect_runtime_capabilities,
)

__all__ = ["CapabilitiesExitCode", "run_doctor_capabilities"]


class CapabilitiesExitCode(IntEnum):
    """Stable process outcomes for a valid or failed lightweight probe."""

    SUCCESS = 0
    FATAL = 1
    NOT_FULLY_AVAILABLE = 2


# endregion [01]


# region [02] Canonical report and presentation


def _all_available(statuses: tuple[RuntimeCapabilityStatus, ...]) -> bool:
    return all(
        status.state is CapabilityState.AVAILABLE
        and status.enabled
        and status.processing_error is None
        for status in statuses
    )


def _report_payload(
    statuses: tuple[RuntimeCapabilityStatus, ...],
) -> dict[str, object]:
    return {
        "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
        "kind": "runtime_capabilities_report",
        "probe_policy": RUNTIME_CAPABILITY_PROBE_POLICY,
        "all_available": _all_available(statuses),
        "capabilities": [status.to_dict() for status in statuses],
    }


def _quoted(value: str | None) -> str:
    if value is None:
        return "-"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _print_human(statuses: tuple[RuntimeCapabilityStatus, ...]) -> None:
    print(
        "CAPABILITIES "
        f"schema={RUNTIME_CAPABILITY_SCHEMA_VERSION} "
        f"probe_policy={RUNTIME_CAPABILITY_PROBE_POLICY} "
        f"count={len(statuses)} all_available={int(_all_available(statuses))}"
    )
    for status in statuses:
        reasons = ",".join(status.degradation_reasons) or "-"
        model_status = (
            "not_checked" if status.capability in {"audio", "semantic"} else "not_applicable"
        )
        print(
            f"CAPABILITY name={status.capability} state={status.state.value} "
            f"extra={status.extra or '-'} reasons={reasons} "
            f"operational_state={status.operational_state} "
            f"model_status={model_status} prerequisite_scope=metadata_and_paths"
        )
        for component in status.components:
            requirement = component.requirement
            reason = component.unavailable_reason or "-"
            print(
                f"CAPABILITY_COMPONENT capability={status.capability} "
                f"component={requirement.component} kind={requirement.kind.value} "
                f"required={int(requirement.required)} "
                f"available={int(component.available)} "
                f"version={_quoted(component.version)} path={_quoted(component.path)} "
                f"reason={reason} extra={requirement.extra or '-'} "
                f"status={component.observation_state.value} "
                f"distribution={_quoted(requirement.distribution)} "
                f"requirement={_quoted(component.applicable_requirement)} "
                "functional_status=not_checked"
            )


def _runtime_platform() -> str:
    return {"win32": "windows", "linux": "linux"}.get(sys.platform, sys.platform)


_TEXT_LOCAL_POLICY = CapabilityPolicy(
    policy_id="neocortex-text-local-v1",
    allow_network=False,
    allowed_privacy=(CapabilityPrivacy.LOCAL_ONLY,),
    gpu_available=False,
)
_NON_REPLAYABLE_TEXT_MIMES = frozenset(
    {
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)


def _text_selection(args: argparse.Namespace) -> CapabilitySelection:
    request = CapabilityRequest(
        capability_id=args.doctor_capabilities_select,
        modality="document",
        input_schema=TEXT_RAW_INPUT_SCHEMA,
        output_schema=TEXT_REPRESENTATION_OUTPUT_SCHEMA,
        mime_type=args.doctor_capabilities_mime_type,
        language="unknown",
        input_bytes=args.doctor_capabilities_input_bytes,
        platform=_runtime_platform(),
        acceptable_reproducibility=("environment_bound", "non_replayable"),
        require_incremental=(args.doctor_capabilities_mime_type not in _NON_REPLAYABLE_TEXT_MIMES),
    )
    broker = build_runtime_capability_broker(request)
    return broker.select(request, _TEXT_LOCAL_POLICY)


def _print_selection_human(selection: CapabilitySelection) -> None:
    print(
        f"CAPABILITY_SELECTION schema={CAPABILITY_SELECTION_SCHEMA} "
        f"status={selection.status} "
        f"capability={selection.request.capability_id} "
        f"policy={selection.policy.policy_id} "
        f"mime_type={selection.request.mime_type} "
        f"input_bytes={selection.request.input_bytes}"
    )
    if selection.selected is not None:
        print(
            "CAPABILITY_SELECTED "
            f"implementation={selection.selected.implementation_id} "
            f"provider={selection.selected.provider} "
            f"provider_version={selection.selected.provider_version or '-'}"
        )
    for reason in selection.explanation:
        print(f"CAPABILITY_SELECTION_REASON value={reason}")
    for candidate in selection.candidates:
        rejected = ",".join(candidate.rejection_reasons) or "-"
        preferred = ",".join(candidate.preference_reasons) or "-"
        readiness = (
            "unknown"
            if candidate.availability is None
            else "available"
            if candidate.availability.available
            else "unavailable"
        )
        binaries = (
            "-"
            if candidate.availability is None
            else ",".join(
                f"{item.name}@sha256:{item.artifact_sha256}"
                for item in candidate.availability.binary_identities
            )
            or "-"
        )
        print(
            "CAPABILITY_CANDIDATE "
            f"implementation={candidate.implementation_id} "
            f"eligible={int(candidate.eligible)} readiness={readiness} "
            f"binaries={binaries} rejected={rejected} preferences={preferred}"
        )


def _run_text_selection(args: argparse.Namespace) -> int:
    try:
        selection = _text_selection(args)
        serialized = (
            json.dumps(
                selection.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if args.doctor_capabilities_json
            else None
        )
    except Exception as exc:
        print(
            f"ERROR doctor-capabilities {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return int(CapabilitiesExitCode.FATAL)

    if serialized is not None:
        print(serialized)
    else:
        _print_selection_human(selection)
    return int(
        CapabilitiesExitCode.SUCCESS
        if selection.selected is not None
        else CapabilitiesExitCode.NOT_FULLY_AVAILABLE
    )


# endregion [02]


# region [03] Direct handler


def run_doctor_capabilities(args: argparse.Namespace) -> int:
    """Inspect declared runtime prerequisites without loading optional engines."""

    if getattr(args, "doctor_capabilities_select", None) is not None:
        return _run_text_selection(args)

    try:
        statuses = inspect_runtime_capabilities()
        serialized = (
            json.dumps(
                _report_payload(statuses),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if args.doctor_capabilities_json
            else None
        )
    except Exception as exc:
        print(
            f"ERROR doctor-capabilities {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return int(CapabilitiesExitCode.FATAL)

    if serialized is not None:
        print(serialized)
    else:
        _print_human(statuses)
    return int(
        CapabilitiesExitCode.SUCCESS
        if _all_available(statuses)
        else CapabilitiesExitCode.NOT_FULLY_AVAILABLE
    )


# endregion [03]
