"""Application control flow for the NeoCortex command-line interface."""


# region [01] Lightweight imports and public contract
# Route engines are imported only after parsing and direct-command dispatch.

from __future__ import annotations
import argparse
import importlib
import inspect
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .cli_operations import dispatch_direct_operation

__all__ = ["dispatch_direct", "main", "run_framework"]


# Optional service adapters are deliberately kept here instead of growing a
# second duplicate engine in the CLI.  Domain owners may publish one of these
# stable lazy modules; until then ``--dedupe`` fails closed with a bounded,
# actionable result.
_DEDUPE_SERVICE_MODULES = (
    "neocortex.api.dedupe",
    "neocortex.api.dedupe_service",
    "neocortex.deduplication.service",
)
_DEDUPE_SERVICE_HANDLERS = ("run_dedupe", "dedupe", "execute_dedupe")
_SERVICE_UNAVAILABLE_EXIT_CODE = 2


def _json_summary_payload(result: object, *, semantic_results: Sequence[tuple[str, object]], semantic_exit_code: int) -> object:
    """Convert one completed run to a bounded JSON-safe summary."""

    if is_dataclass(result) and not isinstance(result, type):
        payload: dict[str, object] = asdict(result)
    elif isinstance(result, Mapping):
        payload = {str(key): value for key, value in result.items()}
    else:
        payload = {"result": str(result)}
    if isinstance(payload, dict):
        payload.setdefault("semantic_exit_code", semantic_exit_code)
        if semantic_results:
            payload["semantic_results"] = [
                {
                    "scope": scope,
                    "result": (
                        asdict(value)
                        if is_dataclass(value) and not isinstance(value, type)
                        else value
                    ),
                }
                for scope, value in semantic_results
            ]
    from neocortex.api.read_contract import sanitize_untrusted_payload

    return sanitize_untrusted_payload(payload)


def _service_json_requested(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "dedupe_json", False)
        or getattr(args, "json_output", False)
        or getattr(args, "json", False)
    )


def _service_payload(
    value: object,
    *,
    operation: str,
    code: str | None = None,
) -> dict[str, object]:
    """Normalize one optional service result without interpreting text output."""

    if isinstance(value, Mapping):
        payload = {str(key): item for key, item in value.items()}
    else:
        to_mapping = getattr(value, "to_dict", None)
        if not callable(to_mapping):
            to_mapping = getattr(value, "as_dict", None)
        converted = to_mapping() if callable(to_mapping) else None
        payload = (
            {str(key): item for key, item in converted.items()}
            if isinstance(converted, Mapping)
            else {"result": value}
        )
    payload.setdefault("operation", operation)
    if code is not None:
        payload.setdefault("code", code)
    return payload


def _print_service_unavailable(args: argparse.Namespace, *, reason: str) -> int:
    """Report an absent optional service on JSON stdout or diagnostic stderr."""

    payload = _service_payload(
        {
            "schema": "neocortex.cli-service/v1",
            "operation": "dedupe",
            "status": "unavailable",
            "code": "dedupe_service_unavailable",
            "exit_code": _SERVICE_UNAVAILABLE_EXIT_CODE,
            "read_only": not bool(getattr(args, "apply", False)),
            "mutation_authorized": bool(getattr(args, "apply", False)),
            "reason": reason,
        },
        operation="dedupe",
    )
    if _service_json_requested(args):
        from neocortex.api.read_contract import sanitize_untrusted_payload

        safe_payload = sanitize_untrusted_payload(payload)
        print(
            json.dumps(
                safe_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        from neocortex.api.read_contract import sanitize_untrusted_text

        print(
            "ERROR dedupe_service_unavailable: "
            + sanitize_untrusted_text(reason, limit=800),
            file=sys.stderr,
        )
    return _SERVICE_UNAVAILABLE_EXIT_CODE


def _load_dedupe_service() -> tuple[Callable[[argparse.Namespace], object] | None, str | None]:
    """Find a domain-owned duplicate service without importing route engines."""

    reasons: list[str] = []
    for module_name in _DEDUPE_SERVICE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # A missing dependency inside an existing service is still an
            # unavailable capability, not permission to fall back to a local
            # implementation or to start the framework engine here.
            if exc.name and exc.name != module_name:
                reasons.append(f"{module_name}: missing dependency {exc.name}")
            continue
        except Exception as exc:
            reasons.append(f"{module_name}: {type(exc).__name__}")
            continue
        for handler_name in _DEDUPE_SERVICE_HANDLERS:
            handler = getattr(module, handler_name, None)
            if callable(handler):
                return handler, None
        reasons.append(f"{module_name}: no supported handler")
    return None, "; ".join(reasons) or "no duplicate-detection service is registered"


def _dispatch_dedupe_service(args: argparse.Namespace) -> int:
    """Dispatch ``--dedupe`` to the owner service, never to a second engine."""

    handler, unavailable_reason = _load_dedupe_service()
    if handler is None:
        return _print_service_unavailable(
            args,
            reason=unavailable_reason or "service unavailable",
        )
    try:
        result = handler(args)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if _service_json_requested(args):
            from neocortex.api.read_contract import sanitize_untrusted_payload

            payload = _service_payload(
                {
                    "schema": "neocortex.cli-service/v1",
                    "operation": "dedupe",
                    "status": "failed",
                    "code": "dedupe_service_failed",
                    "exit_code": _SERVICE_UNAVAILABLE_EXIT_CODE,
                    "reason": reason,
                },
                operation="dedupe",
            )
            print(
                json.dumps(
                    sanitize_untrusted_payload(payload),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            from neocortex.api.read_contract import sanitize_untrusted_text

            print(
                "ERROR dedupe_service_failed: "
                + sanitize_untrusted_text(reason, limit=800),
                file=sys.stderr,
            )
        return _SERVICE_UNAVAILABLE_EXIT_CODE

    if isinstance(result, int) and not isinstance(result, bool):
        return result
    payload = _service_payload(result, operation="dedupe")
    raw_exit_code = payload.get("exit_code", 0)
    exit_code = (
        raw_exit_code
        if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool)
        else 0
    )
    if _service_json_requested(args):
        from neocortex.api.read_contract import sanitize_untrusted_payload

        print(
            json.dumps(
                sanitize_untrusted_payload(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        from neocortex.api.read_contract import sanitize_untrusted_text

        status = sanitize_untrusted_text(payload.get("status", "complete"), limit=128)
        code = sanitize_untrusted_text(payload.get("code", "ok"), limit=128)
        print(f"DEDUPE status={status} code={code} exit_code={exit_code}")
    return exit_code


# endregion [01]


# region [02] Direct operation dispatch


def dispatch_direct(args: argparse.Namespace) -> int | None:
    """Run a selected direct operation, or return ``None`` for a full run."""

    if getattr(args, "factory_reset", False):
        from .factory_reset import run_factory_reset

        return run_factory_reset(args)
    # Hygiene is a plan/preview/verification leaf.  Its owner import stays
    # behind parsing and validation and can never fall through to Framework or
    # a physical file-action path.
    if getattr(args, "command", None) == "hygiene":
        from .cli_hygiene import run_hygiene

        return run_hygiene(args)
    # Machine inventory is an explicit read-only control-plane leaf.  Keep its
    # owner import behind parsing and validation; it must never fall through
    # to the framework inventory or route graph.
    if getattr(args, "command", None) == "machine-inventory":
        from .cli_machine_inventory import run_machine_inventory

        return run_machine_inventory(args)
    # Maintenance is a control-plane leaf, not a Framework route.  Keep its
    # owner import lazy and dispatch it before any integrated-run decision.
    if getattr(args, "command", None) == "external-maintenance":
        from .cli_external_maintenance import run_external_maintenance

        return run_external_maintenance(args)
    if getattr(args, "command", None) == "maintenance":
        from .cli_maintenance import run_maintenance

        return run_maintenance(args)
    if getattr(args, "command", None) == "agent-activity":
        from .cli_agent_activity import run_agent_activity

        return run_agent_activity(args)
    if getattr(args, "dedupe", False):
        return _dispatch_dedupe_service(args)
    # Configuration doctor is a parser-owned leaf rather than a product
    # direct-operation registry entry, preserving the established registry
    # contract for content and state operations.
    if getattr(args, "doctor_config", False):
        from .cli_config_doctor import run_doctor_config

        return run_doctor_config(args)
    return dispatch_direct_operation(args)


# endregion [02]


# region [03] Framework configuration and execution


def _run_framework_with_progress(
    args: argparse.Namespace,
    progress,
    *,
    lifecycle_stage_runner: Callable[[int], object] | None = None,
    lifecycle_stage_details: Mapping[str, object] | None = None,
):
    """Execute the framework with one caller-owned progress reporter."""

    from .cli_config import framework_config_from_args
    from neocortex.runtime.control.console_cancellation import ConsoleCancellationBridge
    from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator

    config = framework_config_from_args(args)
    orchestrator = FrameworkOrchestrator(
        config,
        progress=progress,
        lifecycle_stage_runner=lifecycle_stage_runner,
        lifecycle_stage_details=lifecycle_stage_details,
    )
    args._semantic_cancellation_check = lambda: orchestrator._cancellation.is_cancelled
    with ConsoleCancellationBridge(orchestrator.request_cancellation):
        return orchestrator.run()


def run_framework(
    args: argparse.Namespace,
    *,
    progress=None,
    lifecycle_stage_runner: Callable[[int], object] | None = None,
    lifecycle_stage_details: Mapping[str, object] | None = None,
):
    """Build the validated configuration and run the integrated framework."""

    from neocortex.progress import LineProgress, RichProgress

    if progress is not None:
        if lifecycle_stage_runner is None and lifecycle_stage_details is None:
            return _run_framework_with_progress(args, progress)
        return _run_framework_with_progress(
            args,
            progress,
            lifecycle_stage_runner=lifecycle_stage_runner,
            lifecycle_stage_details=lifecycle_stage_details,
        )
    reporter = (
        LineProgress() if os.environ.get("NEOCORTEX_PROGRESS_STREAM") == "1" else RichProgress()
    )
    with reporter as progress:
        if lifecycle_stage_runner is None and lifecycle_stage_details is None:
            return _run_framework_with_progress(args, progress)
        return _run_framework_with_progress(
            args,
            progress,
            lifecycle_stage_runner=lifecycle_stage_runner,
            lifecycle_stage_details=lifecycle_stage_details,
        )


def _semantic_stage_details(args: argparse.Namespace) -> dict[str, object]:
    """Capture Semantic configuration before Framework workers start."""

    selected_sources = tuple(getattr(args, "semantic_source", None) or ())
    details: dict[str, object] = {
        "selected_sources": list(selected_sources),
        "selection_pending": getattr(args, "semantic_source", None) is None,
        "complete_all": bool(getattr(args, "_semantic_complete_all", False)),
        "semantic_budget_version": 2,
        "image_available": False,
        "semantic_budget": {
            "max_items": getattr(args, "semantic_max_items", None),
            "max_new_jobs": getattr(args, "semantic_max_new_jobs", None),
            "time_budget_seconds": getattr(args, "semantic_time_budget_seconds", None),
        },
        "semantic_text_profile": getattr(args, "semantic_text_profile", "quality"),
        "semantic_model_cache": (
            None
            if getattr(args, "semantic_model_cache", None) is None
            else str(args.semantic_model_cache)
        ),
        "semantic_threads": getattr(args, "semantic_threads", None),
        "semantic_no_ocr": bool(getattr(args, "semantic_no_ocr", False)),
    }
    publication_owners = getattr(args, "_semantic_publication_owners", None)
    if publication_owners is not None:
        details["publication_owners"] = list(publication_owners)
    return details


def _emit_unsuccessful_execution(
    progress,
    failure: BaseException,
    *,
    error_code: str,
    errors: int = 1,
    cancelled: bool = False,
    failed_routes: Sequence[str] = (),
) -> None:
    """Terminate the CLI run, not a successful subphase, before its reporter closes."""

    from neocortex.api.read_contract import sanitize_untrusted_text
    from neocortex.progress import ProgressEvent, ProgressMetric

    progress(
        ProgressEvent(
            "framework",
            "result",
            "Ejecución cancelada" if cancelled else "Ejecución fallida — resultado incompleto",
            0,
            None,
            "ejecución",
            True,
            (
                ProgressMetric("status", "cancelled" if cancelled else "failed"),
                ProgressMetric("completion", "incomplete"),
                ProgressMetric("exit_code", 130 if cancelled else 2),
                ProgressMetric("error_code", error_code),
                ProgressMetric(
                    "error_type", sanitize_untrusted_text(type(failure).__name__, limit=128)
                ),
                ProgressMetric("cause", sanitize_untrusted_text(failure, limit=512)),
                ProgressMetric("errors", errors),
                ProgressMetric(
                    "failed_routes",
                    sanitize_untrusted_text(",".join(sorted(failed_routes)), limit=512),
                ),
            ),
        )
    )


def _emit_json_execution_error(
    *,
    code: str,
    failure: BaseException,
    failed_routes: Sequence[str] = (),
) -> None:
    """Keep ``--json`` parseable when execution fails before a result object."""

    from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text

    payload = {
        "schema": "neocortex.lifecycle-envelope/v1",
        "status": "partial",
        "completion": "incomplete",
        "exit_code": 2,
        "error": {
            "code": code,
            "type": type(failure).__name__,
            "message": sanitize_untrusted_text(failure, limit=1_000, single_line=False),
        },
        "failed_routes": [sanitize_untrusted_text(item, limit=128) for item in failed_routes],
    }
    print(
        json.dumps(
            sanitize_untrusted_payload(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


_ROUTE_FAILURE_NEXT_STEP = (
    "Siguiente paso: consulte --status --status-json con el mismo --state-directory "
    "y resuelva las causas indicadas antes de reanudar la ejecución."
)
_SQLITE_FAILURE_NEXT_STEP = (
    "Siguiente paso: cuando no haya escritores activos, consulte --status --status-json "
    "con el mismo --state-directory y revise la lectura estable de SQLite antes de reintentar; "
    "no borre WAL/SHM."
)


# endregion [03]


# region [04] Application control flow


_LEAF_VALUE_OPTIONS = frozenset(
    {
        "--doctor-capabilities-select",
        "--doctor-capabilities-mime-type",
        "--doctor-capabilities-input-bytes",
        "--root",
        "--state-directory",
    }
)
_LEAF_FLAG_OPTIONS = frozenset(
    {
        "--doctor-capabilities",
        "--doctor-capabilities-json",
        "--doctor-platform",
        "--doctor-platform-json",
        "--doctor-config",
        "--doctor-config-json",
    }
)
_CAPABILITIES_LEAF_OPTIONS = frozenset(
    {
        "--doctor-capabilities",
        "--doctor-capabilities-json",
        "--doctor-capabilities-select",
        "--doctor-capabilities-mime-type",
        "--doctor-capabilities-input-bytes",
    }
)
_PLATFORM_LEAF_OPTIONS = frozenset({"--doctor-platform", "--doctor-platform-json"})
_CONFIG_LEAF_OPTIONS = frozenset({"--doctor-config", "--doctor-config-json"})


def _leaf_arguments_supported(arguments: Sequence[str]) -> bool:
    """Recognize one complete doctor leaf without accepting partial argv."""

    position = 0
    while position < len(arguments):
        token = arguments[position]
        option, separator, value = token.partition("=")
        if option in _LEAF_FLAG_OPTIONS:
            if separator:
                return False
            position += 1
            continue
        if option not in _LEAF_VALUE_OPTIONS:
            return False
        if separator:
            if not value:
                return False
            position += 1
            continue
        if position + 1 >= len(arguments):
            return False
        position += 2
    return True


def _build_doctor_leaf_parser() -> argparse.ArgumentParser:
    """Build only the hidden compatibility options consumed by doctor leaves."""

    parser = argparse.ArgumentParser(
        prog="Neocortex",
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
    )
    parser.add_argument("--doctor-capabilities", action="store_true")
    parser.add_argument("--doctor-capabilities-json", action="store_true")
    parser.add_argument("--doctor-capabilities-select")
    parser.add_argument("--doctor-capabilities-mime-type")
    parser.add_argument("--doctor-capabilities-input-bytes", type=int)
    parser.add_argument("--doctor-platform", action="store_true")
    parser.add_argument("--doctor-platform-json", action="store_true")
    parser.add_argument("--doctor-config", action="store_true")
    parser.add_argument("--doctor-config-json", action="store_true")
    # Doctors deliberately ignore these full-parser options and must not
    # resolve or create state merely to accept them.
    parser.add_argument("--root", type=Path)
    parser.add_argument("--state-directory", type=Path)
    return parser


def _explicit_leaf_options(arguments: Sequence[str]) -> frozenset[str]:
    destinations: set[str] = set()
    for token in arguments:
        if token.startswith("--"):
            destinations.add(token.partition("=")[0][2:].replace("-", "_"))
    return frozenset(destinations)


def _run_doctor_leaf(arguments: Sequence[str]) -> int | None:
    """Dispatch a valid doctor leaf without importing the integrated parser DAG."""

    if not _leaf_arguments_supported(arguments):
        return None
    capabilities = "--doctor-capabilities" in arguments
    platform = "--doctor-platform" in arguments
    config = "--doctor-config" in arguments
    if sum((capabilities, platform, config)) != 1:
        return None
    supplied_options = {token.partition("=")[0] for token in arguments if token.startswith("--")}
    if capabilities:
        unrelated_options = _PLATFORM_LEAF_OPTIONS | _CONFIG_LEAF_OPTIONS
    elif platform:
        unrelated_options = _CAPABILITIES_LEAF_OPTIONS | _CONFIG_LEAF_OPTIONS
    else:
        unrelated_options = _CAPABILITIES_LEAF_OPTIONS | _PLATFORM_LEAF_OPTIONS
    if supplied_options.intersection(unrelated_options):
        return None

    parser = _build_doctor_leaf_parser()
    try:
        args = parser.parse_args(arguments)
    except (argparse.ArgumentError, SystemExit):
        # Preserve full-parser diagnostics for malformed values.  The fast path
        # only owns an argv that is both complete and valid.
        return None
    args.apply = False
    args.route = "none"
    args._explicit_options = _explicit_leaf_options(arguments)
    if capabilities:
        from .cli_capabilities import run_doctor_capabilities
        from .cli_capabilities_surface import validate_capabilities_arguments

        try:
            validate_capabilities_arguments(args)
        except SystemExit:
            return None
        return run_doctor_capabilities(args)

    if config:
        from .cli_config_doctor import run_doctor_config
        from .cli_config_doctor_surface import validate_config_doctor_arguments

        try:
            validate_config_doctor_arguments(args)
        except SystemExit:
            return None
        return run_doctor_config(args)

    from .cli_platform import run_doctor_platform
    from .cli_platform_surface import validate_platform_arguments

    try:
        validate_platform_arguments(args)
    except SystemExit:
        return None
    return run_doctor_platform(args)


def _run_exact_version(arguments: Sequence[str]) -> None:
    """Preserve argparse's exact version bytes and ``SystemExit(0)`` contract."""

    from neocortex import __version__

    parser = argparse.ArgumentParser(prog="Neocortex", add_help=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """Parse, validate and dispatch one NeoCortex command invocation."""

    forwarded = list(sys.argv[1:] if arguments is None else arguments)
    if not forwarded:
        # An empty invocation is an ambiguous no-op, not permission to start
        # an inventory run.  Keep this fast path before parser construction so
        # it cannot create state or import route engines.
        print(
            "Uso: Neocortex --all, --dedupe, --route ROUTES, machine-inventory, "
            "maintenance, hygiene, agent-activity o una operación directa"
        )
        print("Use `Neocortex --help` para ver las opciones disponibles.")
        return 0
    if forwarded == ["--version"]:
        _run_exact_version(forwarded)
        raise AssertionError("argparse version action must exit")
    leaf_exit_code = _run_doctor_leaf(forwarded)
    if leaf_exit_code is not None:
        return leaf_exit_code

    from .cli_parser import build_parser

    parser = build_parser()
    args = parser.parse_args(forwarded)

    from .cli_validation import validate_arguments

    try:
        validate_arguments(args)
    except SystemExit as exc:
        # Domain validators retain a lightweight direct-call contract, but the
        # public CLI reports every argument error through argparse with code 2.
        if isinstance(exc.code, str):
            parser.error(exc.code)
        raise
    direct_exit_code = dispatch_direct(args)
    if direct_exit_code is not None:
        return direct_exit_code

    if not (
        args.all
        or args.route != "none"
        or args.route_only
        or args.resume_run is not None
        or args.candidate_run is not None
        or bool(getattr(args, "dedupe", False))
        or bool(getattr(args, "dedup_keep", ()))
        or bool(getattr(args, "dedup_prefer_root", ()))
    ):
        # Options such as --root or --state-directory alone do not select an
        # operation.  Never turn them into an implicit inventory write.
        print(
            "Uso: Neocortex --all, --dedupe, --route ROUTES, machine-inventory, "
            "maintenance, hygiene, agent-activity o una operación directa"
        )
        print("Use `Neocortex --help` para ver las opciones disponibles.")
        return 0

    from neocortex.progress import LineProgress, RichProgress
    from rich.console import Console
    from neocortex.api.read_contract import sanitize_untrusted_text
    from neocortex.deduplication import InventoryError
    from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable
    from neocortex.persistence.state_publication import StatePublicationError
    from neocortex.persistence.framework_state_writer import RunBudgetExceeded
    from neocortex.runtime.orchestration.orchestrator import RouteExecutionError
    from neocortex.runtime.config.runtime_cache import RuntimeCacheConfigurationError
    from neocortex.safety.protected_content import ProtectedContentError

    from .cli_reporting import (
        has_organization_errors,
        has_strict_route_errors,
        print_professional_summary,
        print_reports,
    )

    professional_output = Console().is_terminal
    semantic_results: list[tuple[str, object]] = []
    semantic_exit_code = 0
    semantic_attempted = False
    semantic_resume_source_run_id: int | None = None
    semantic_stage_runner: Callable[[int], object] | None = None
    semantic_stage_details: Mapping[str, object] | None = None
    semantic_callback_lock_held = False
    if args.resume_run is not None:
        from .cli_semantic import semantic_resume_available

        if semantic_resume_available(args, args.resume_run):
            semantic_resume_source_run_id = args.resume_run
    if args.all or semantic_resume_source_run_id is not None:
        from .cli_semantic import run_integrated_all_semantic_index

        semantic_attempted = True

        def run_semantic_stage(run_id: int) -> object:
            nonlocal semantic_exit_code
            semantic_exit_code = run_integrated_all_semantic_index(
                args,
                progress=progress,
                result_sink=lambda scope, value: semantic_results.append((scope, value)),
                print_output=not professional_output and not bool(getattr(args, "json_output", False)),
                run_id=run_id,
                resume_source_run_id=semantic_resume_source_run_id,
                framework_lock_held=semantic_callback_lock_held,
            )
            return semantic_exit_code

        semantic_stage_runner = run_semantic_stage
    try:
        reporter = (
            LineProgress() if os.environ.get("NEOCORTEX_PROGRESS_STREAM") == "1" else RichProgress()
        )
        with reporter as progress:
            try:
                if args.all or args.resume_run is not None:
                    from neocortex.runtime.config.runtime_cache import configure_runtime_cache
                    from .cli_semantic import prepare_integrated_semantic_start

                    configure_runtime_cache(args.state_directory)
                    prepare_integrated_semantic_start(args, progress=progress)
                if semantic_stage_runner is not None:
                    # The fresh-start preflight may consume part of an explicit
                    # time cap; persist the effective remainder, not the
                    # preflight-free value captured before the dispatcher.
                    semantic_stage_details = _semantic_stage_details(args)
                supports_lifecycle_hook = (
                    "lifecycle_stage_runner" in inspect.signature(run_framework).parameters
                )
                semantic_callback_lock_held = supports_lifecycle_hook
                if supports_lifecycle_hook:
                    result = run_framework(
                        args,
                        progress=progress,
                        lifecycle_stage_runner=semantic_stage_runner,
                        lifecycle_stage_details=semantic_stage_details,
                    )
                else:
                    # Keep lightweight test doubles and downstream callers
                    # that implement the pre-0.13 two-argument seam working;
                    # they cannot host the integrated Semantic callback.
                    result = run_framework(args, progress=progress)
                    if semantic_stage_runner is not None:
                        semantic_callback_lock_held = False
                        # Compatibility path for callers/tests that replace
                        # ``run_framework`` with the pre-0.13 seam.  The real
                        # implementation receives the callback above; this
                        # fallback keeps the historical post-run hook without
                        # weakening the production lifecycle.
                        fallback_run_id = getattr(result, "run_id", None)
                        if type(fallback_run_id) is int:
                            semantic_stage_runner(fallback_run_id)
            except KeyboardInterrupt as exc:
                _emit_unsuccessful_execution(
                    progress, exc, error_code="execution_cancelled", errors=0, cancelled=True
                )
                # The public entrypoint owns exit 130; direct callers retain
                # KeyboardInterrupt and the orchestrator's cancellation contract.
                raise
            except (
                InventoryError,
                RouteExecutionError,
                ImmutableSQLiteUnavailable,
                StatePublicationError,
                RunBudgetExceeded,
                RuntimeCacheConfigurationError,
                ProtectedContentError,
            ) as exc:
                error_code = (
                    "budget_exhausted"
                    if isinstance(exc, RunBudgetExceeded)
                    else "recovery_required"
                    if isinstance(exc, StatePublicationError)
                    else "protected_content_root"
                    if isinstance(exc, ProtectedContentError)
                    else "runtime_cache_configuration"
                    if isinstance(exc, RuntimeCacheConfigurationError)
                    else "route_execution_failed"
                    if isinstance(exc, RouteExecutionError)
                    else "sqlite_snapshot_unavailable"
                    if isinstance(exc, ImmutableSQLiteUnavailable)
                    else "corpus_unavailable"
                )
                _emit_unsuccessful_execution(
                    progress,
                    exc,
                    error_code=error_code,
                    errors=len(exc.failures) if isinstance(exc, RouteExecutionError) else 1,
                    failed_routes=tuple(exc.failures)
                    if isinstance(exc, RouteExecutionError)
                    else (),
                )
                raise
    except RunBudgetExceeded as exc:
        if bool(getattr(args, "json_output", False)):
            _emit_json_execution_error(code="budget_exhausted", failure=exc)
            return 2
        print(
            "ERROR budget_exhausted completion=incomplete: "
            + sanitize_untrusted_text(exc, limit=800),
            file=sys.stderr,
        )
        return 2
    except StatePublicationError as exc:
        if bool(getattr(args, "json_output", False)):
            _emit_json_execution_error(code="recovery_required", failure=exc)
            return 2
        print(
            "ERROR recovery_required status=failed completion=incomplete: "
            + sanitize_untrusted_text(exc, limit=800),
            file=sys.stderr,
        )
        print("El avance se conserva; la publicación pendiente requiere recuperación compatible.", file=sys.stderr)
        return 2
    except InventoryError as exc:
        if bool(getattr(args, "json_output", False)):
            _emit_json_execution_error(code="corpus_unavailable", failure=exc)
            return 2
        print(
            f"ERROR corpus_unavailable: {sanitize_untrusted_text(exc, limit=800)}", file=sys.stderr
        )
        return 2
    except ImmutableSQLiteUnavailable as exc:
        if bool(getattr(args, "json_output", False)):
            _emit_json_execution_error(code="sqlite_snapshot_unavailable", failure=exc)
            return 2
        print(
            "ERROR sqlite_snapshot_unavailable status=failed completion=incomplete: "
            + sanitize_untrusted_text(exc, limit=800),
            file=sys.stderr,
        )
        print(_SQLITE_FAILURE_NEXT_STEP, file=sys.stderr)
        return 2
    except RuntimeCacheConfigurationError as exc:
        if bool(getattr(args, "json_output", False)):
            _emit_json_execution_error(code="runtime_cache_configuration", failure=exc)
            return 2
        print(
            "ERROR runtime_cache_configuration status=failed completion=incomplete: "
            + sanitize_untrusted_text(exc, limit=800),
            file=sys.stderr,
        )
        return 2
    except ProtectedContentError as exc:
        if bool(getattr(args, "json_output", False)):
            _emit_json_execution_error(code="protected_content_root", failure=exc)
            return 2
        print(
            "ERROR protected_content_root status=failed completion=incomplete: "
            + sanitize_untrusted_text(exc, limit=800),
            file=sys.stderr,
        )
        return 2
    except RouteExecutionError as exc:
        # The owners have already recorded the failed routes.  Present that
        # failure without inventing a completed run or continuing --all's
        # dependent semantic stage; partial results remain with their owners.
        if bool(getattr(args, "json_output", False)):
            _emit_json_execution_error(
                code="route_execution_failed",
                failure=exc,
                failed_routes=tuple(exc.failures),
            )
            return 2
        print(
            "ERROR route_execution_failed status=failed completion=incomplete",
            file=sys.stderr,
        )
        for route_name, failure in sorted(exc.failures.items()):
            route = sanitize_untrusted_text(route_name, limit=128)
            reason = sanitize_untrusted_text(failure, limit=800)
            error_type = sanitize_untrusted_text(type(failure).__name__, limit=128)
            print(
                f"ERROR route_failed route={route} error_type={error_type}: {reason}",
                file=sys.stderr,
            )
        print(
            _SQLITE_FAILURE_NEXT_STEP
            if any(
                isinstance(failure, ImmutableSQLiteUnavailable) for failure in exc.failures.values()
            )
            else _ROUTE_FAILURE_NEXT_STEP,
            file=sys.stderr,
        )
        return 2

    if bool(getattr(args, "json_output", False)):
        print(
            json.dumps(
                _json_summary_payload(
                    result,
                    semantic_results=tuple(semantic_results),
                    semantic_exit_code=semantic_exit_code,
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif professional_output:
        print_professional_summary(
            result,
            args,
            semantic_results=tuple(semantic_results),
            semantic_exit_code=semantic_exit_code,
            semantic_attempted=semantic_attempted,
        )
    else:
        print_reports(result, args)
    actions = getattr(result, "actions", None)
    if getattr(result, "route_failures", None):
        return 2
    maintenance = getattr(result, "maintenance", {})
    if maintenance and maintenance.get("operation_status") not in {"complete", "planned"}:
        return 2
    if (actions is not None and actions.errors) or has_organization_errors(result):
        return 2
    if semantic_exit_code != 0:
        return 2
    if (args.all or args.strict_exit_codes) and has_strict_route_errors(result):
        return 2
    return 0


# endregion [04]
