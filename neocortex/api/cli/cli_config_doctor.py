"""Read-only configuration doctor for the canonical CLI.

The configuration doctor deliberately lives at the CLI boundary. It reports
the values that affect a normal invocation without loading the framework,
opening an owner database, creating an XDG directory, or echoing the process
environment. Paths are useful diagnostics, while secret-shaped environment
variables are never copied into the report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Final

from neocortex.platform.policy import (
    LINUX_MUTATION_REASON,
    current_platform_policy,
    default_corpus_root,
)
from neocortex.runtime.config.app_paths import default_state_directory
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER

__all__ = [
    "CONFIG_REPORT_SCHEMA_VERSION",
    "config_report",
    "configuration_report",
    "run_doctor_config",
]


CONFIG_REPORT_SCHEMA_VERSION: Final = 1

# Only these names can affect the path report. Values are represented as a
# presence marker rather than copying arbitrary environment content; this
# keeps the doctor useful without making it an environment/secret dumper.
_PATH_OVERRIDE_ENVIRONMENT: Final = (
    "NEOCORTEX_CORPUS_ROOT",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "LOCALAPPDATA",
)


def _path_text(value: object) -> str:
    """Render one path without resolving it through the filesystem."""

    if not isinstance(value, (str, bytes, os.PathLike)):
        raise TypeError("path value must implement the filesystem path protocol")
    return os.fsdecode(os.fspath(value))


def _environment_sources() -> dict[str, str]:
    """Report path override presence without exposing arbitrary env values."""

    return {
        name: "configured" if os.environ.get(name) else "default"
        for name in _PATH_OVERRIDE_ENVIRONMENT
    }


def _canonical_paths(policy) -> dict[str, str]:
    return {
        "corpus": _path_text(policy.corpus_root),
        "state": _path_text(policy.state_directory),
        "config": _path_text(policy.config_directory),
        "data": _path_text(policy.data_directory),
        "releases": _path_text(policy.releases_directory),
        "current": _path_text(policy.current_release),
        "models": _path_text(policy.models_directory),
        "runtimes": _path_text(policy.runtimes_directory),
        "launcher": _path_text(policy.stable_launcher),
        "alias": _path_text(policy.user_alias),
    }


def _effective_paths(
    policy,
    *,
    root: object | None,
    state_directory: object | None,
) -> dict[str, str]:
    canonical = _canonical_paths(policy)
    return {
        **canonical,
        "corpus": _path_text(default_corpus_root() if root is None else root),
        "state": _path_text(
            default_state_directory() if state_directory is None else state_directory
        ),
    }


def configuration_report(
    *,
    root: object | None = None,
    state_directory: object | None = None,
) -> dict[str, object]:
    """Return effective CLI configuration without touching durable state.

    ``root`` and ``state_directory`` are invocation overrides, not write
    targets. The function intentionally performs no existence checks because
    a doctor must also be safe for not-yet-created paths.
    """

    policy = current_platform_policy()
    canonical = _canonical_paths(policy)
    effective = _effective_paths(
        policy,
        root=root,
        state_directory=state_directory,
    )
    return {
        "schema_version": CONFIG_REPORT_SCHEMA_VERSION,
        "kind": "configuration_report",
        "read_only": True,
        "state_access": "none",
        "defaults": {
            "route": "none",
            "apply": False,
            "dedup_policy": "fast",
            "knowledge_scope": "personal",
            "knowledge_mode": "evidence",
            "knowledge_limit": 20,
            "knowledge_context_characters": 12_000,
            "knowledge_response_version": 2,
            "knowledge": {
                "scope": "personal",
                "mode": "evidence",
                "limit": 20,
                "context_characters": 12_000,
                "response_version": 2,
            },
        },
        "limits": {
            "knowledge_limit": {"minimum": 1, "maximum": 1_000},
            "knowledge_context_limit": {"minimum": 1, "maximum": 100},
            "knowledge_context_characters": {"minimum": 1, "maximum": 1_000_000},
        },
        "paths": {
            "canonical": canonical,
            "effective": effective,
        },
        # Keep the platform doctor shape available to scripts that only need
        # effective locations while retaining canonical paths above.
        "effective_paths": {
            "corpus": effective["corpus"],
            "state": effective["state"],
        },
        "capabilities": {
            "system": policy.system,
            "compatible": bool(policy.compatible),
            "routes": list(BUILTIN_ROUTE_ORDER),
            "inventory_backend": policy.inventory_backend,
            "identity_backend": policy.identity_backend,
            "containment_backend": policy.containment_backend,
            "mutation": {
                "available": bool(policy.mutation_available),
                "backend": policy.mutation_backend,
                "reason": (None if policy.mutation_available else LINUX_MUTATION_REASON),
            },
        },
        "environment": {
            "path_overrides": _environment_sources(),
            "secrets": "not inspected",
        },
    }


def _print_human(report: dict[str, object]) -> None:
    defaults = report["defaults"]
    limits = report["limits"]
    capabilities = report["capabilities"]
    paths = report["paths"]
    assert isinstance(defaults, dict)
    assert isinstance(limits, dict)
    assert isinstance(capabilities, dict)
    assert isinstance(paths, dict)
    knowledge = defaults["knowledge"]
    mutation = capabilities["mutation"]
    assert isinstance(knowledge, dict)
    assert isinstance(mutation, dict)
    print(
        "CONFIG "
        f"schema={CONFIG_REPORT_SCHEMA_VERSION} read_only=1 "
        f"system={capabilities['system']} compatible={int(bool(capabilities['compatible']))}"
    )
    print(
        "CONFIG_DEFAULTS "
        f"route={defaults['route']} apply={int(bool(defaults['apply']))} "
        f"dedup_policy={defaults['dedup_policy']} "
        f"knowledge_scope={knowledge['scope']} knowledge_mode={knowledge['mode']}"
    )
    for name, value in limits.items():
        if isinstance(value, dict):
            print(
                f"CONFIG_LIMIT name={name} minimum={value.get('minimum')} "
                f"maximum={value.get('maximum')}"
            )
    for name, value in paths["effective"].items():
        print(f"CONFIG_PATH name={name} value={json.dumps(value, ensure_ascii=False)}")
    print(
        f"CONFIG_MUTATION available={int(bool(mutation['available']))} "
        f"backend={mutation['backend']} reason={mutation['reason'] or '-'}"
    )
    print("CONFIG_STATE_ACCESS value=none")


def run_doctor_config(args: argparse.Namespace) -> int:
    """Print the safe configuration report and never create state."""

    try:
        report = configuration_report(
            root=getattr(args, "root", None),
            state_directory=getattr(args, "state_directory", None),
        )
        if getattr(args, "doctor_config_json", False):
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
        print(f"ERROR doctor-config {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


# Keep the concise report name aligned with the existing ``platform_report``
# helper while retaining the explicit function name for callers that prefer
# the operation's full spelling.
config_report = configuration_report
