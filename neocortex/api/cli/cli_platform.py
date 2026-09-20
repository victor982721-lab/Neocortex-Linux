"""Canonical, read-only platform policy report."""

from __future__ import annotations
import argparse
import json
import platform
import sys
from pathlib import Path

from neocortex.platform.policy import (
    LINUX_MUTATION_REASON,
    current_platform_policy,
    default_corpus_root,
)
from neocortex.runtime.config.app_paths import default_state_directory

PLATFORM_REPORT_SCHEMA_VERSION = 2


def platform_report(
    *, root: Path | None = None, state_directory: Path | None = None
) -> dict[str, object]:
    """Distinguish canonical locations from the paths this invocation will use."""
    policy = current_platform_policy()
    from neocortex.platform.sqlite_runtime_attestation import observe_platform_native_runtime

    native_runtime = observe_platform_native_runtime(
        receipts_directory=policy.state_directory / "installation-receipts",
    )
    return {
        "schema_version": PLATFORM_REPORT_SCHEMA_VERSION,
        "kind": "platform_report",
        "native_runtime": native_runtime,
        "compatible": policy.compatible,
        "system": {
            "family": policy.system,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "paths": {
            "corpus": str(policy.corpus_root),
            "state": str(policy.state_directory),
            "config": str(policy.config_directory),
            "data": str(policy.data_directory),
            "releases": str(policy.releases_directory),
            "current": str(policy.current_release),
            "models": str(policy.models_directory),
            "runtimes": str(policy.runtimes_directory),
            "launcher": str(policy.stable_launcher),
            "alias": str(policy.user_alias),
        },
        "effective_paths": {
            "corpus": str(default_corpus_root() if root is None else root),
            "state": str(default_state_directory() if state_directory is None else state_directory),
        },
        "inventory": {"backend": policy.inventory_backend},
        "identity": {
            "backend": policy.identity_backend,
            "birthtime_unavailable_sentinel": -1,
        },
        "containment": {"backend": policy.containment_backend},
        "elevation": {"mode": policy.elevation},
        "mutation": {
            "available": policy.mutation_available,
            "backend": policy.mutation_backend,
            "reason": None if policy.mutation_available else LINUX_MUTATION_REASON,
        },
    }


def _print_human(report: dict[str, object]) -> None:
    system = report["system"]
    inventory = report["inventory"]
    containment = report["containment"]
    mutation = report["mutation"]
    assert isinstance(system, dict)
    assert isinstance(inventory, dict)
    assert isinstance(containment, dict)
    assert isinstance(mutation, dict)
    print(
        "PLATFORM "
        f"schema={PLATFORM_REPORT_SCHEMA_VERSION} family={system['family']} "
        f"compatible={int(bool(report['compatible']))}"
    )
    print(f"PLATFORM_INVENTORY backend={inventory['backend']}")
    print(f"PLATFORM_CONTAINMENT backend={containment['backend']}")
    native_runtime = report["native_runtime"]
    assert isinstance(native_runtime, dict)
    print(f"PLATFORM_SQLITE status={native_runtime.get('status', 'unaccredited')}")
    print(
        f"PLATFORM_MUTATION available={int(bool(mutation['available']))} "
        f"backend={mutation['backend']} reason={mutation['reason'] or '-'}"
    )
    paths = report["paths"]
    assert isinstance(paths, dict)
    for name, value in paths.items():
        print(f"PLATFORM_PATH name={name} value={json.dumps(value, ensure_ascii=False)}")
    effective_paths = report["effective_paths"]
    assert isinstance(effective_paths, dict)
    for name, value in effective_paths.items():
        print(f"PLATFORM_EFFECTIVE_PATH name={name} value={json.dumps(value, ensure_ascii=False)}")


def run_doctor_platform(args: argparse.Namespace) -> int:
    try:
        report = platform_report(
            root=getattr(args, "root", None),
            state_directory=getattr(args, "state_directory", None),
        )
        if args.doctor_platform_json:
            print(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        else:
            _print_human(report)
    except Exception as exc:
        print(f"ERROR doctor-platform {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


__all__ = ["PLATFORM_REPORT_SCHEMA_VERSION", "platform_report", "run_doctor_platform"]
