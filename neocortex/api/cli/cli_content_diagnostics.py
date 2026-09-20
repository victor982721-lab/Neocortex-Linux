"""Thin CLI adapters for root-scoped PDF and Text diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import NoReturn
from neocortex.api.content_diagnostics_api import (
    CONTENT_DIAGNOSTIC_V2_OWNERS,
    content_diagnostics_error_payload,
    content_diagnostics_payload,
    content_diagnostics_v2_error_payload,
    content_diagnostics_v2_payload,
    validate_content_diagnostics_request,
    validate_content_diagnostics_v2_request,
)
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    normalize_route_selection,
)

_SELECTORS = (("pdf_diagnostics", "pdf"), ("text_errors", "text"))
_V2_SELECTORS = (("content_diagnostics", "all"), ("content_diagnostics_v2", "all"))


def register_content_diagnostics_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    for name, owner in _SELECTORS:
        group.add_argument(
            "--" + name.replace("_", "-"),
            type=int,
            metavar="N",
            help=f"Read at most N persisted {owner} diagnostic records under --root",
        )
    group.add_argument(
        "--content-diagnostics",
        dest="content_diagnostics",
        type=int,
        metavar="N",
        help="Read at most N diagnostic records per selected v2 owner under --root",
    )
    group.add_argument(
        "--content-diagnostics-v2",
        dest="content_diagnostics_v2",
        type=int,
        metavar="N",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--diagnostics-owner",
        choices=("all", *CONTENT_DIAGNOSTIC_V2_OWNERS),
        default="all",
        help="Owner or all for the content-diagnostics/v2 read",
    )
    parser.add_argument("--diagnostics-cursor", help="Continue the same owner/root/filter snapshot")
    parser.add_argument(
        "--diagnostics-file-key", help="Exact file key"
    )
    parser.add_argument(
        "--diagnostics-path", help="Literal path fragment within the requested root"
    )
    parser.add_argument(
        "--diagnostics-reason", help="Exact PDF/Text error_type"
    )
    parser.add_argument(
        "--diagnostics-json", action="store_true", help="Print the structured diagnostic envelope"
    )
    parser.add_argument(
        "--diagnostics-budget-rows",
        type=int,
        help="Optional KnowledgeReadBudget row limit for content-diagnostics/v2",
    )
    parser.add_argument(
        "--diagnostics-budget-vectors",
        type=int,
        help="Optional KnowledgeReadBudget vector limit for content-diagnostics/v2",
    )
    parser.add_argument(
        "--diagnostics-budget-temporary-bytes",
        type=int,
        help="Optional KnowledgeReadBudget detached-snapshot byte limit",
    )
    parser.add_argument(
        "--diagnostics-deadline-seconds",
        type=float,
        help="Optional monotonic deadline for content-diagnostics/v2",
    )


def _options(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "cursor": getattr(args, "diagnostics_cursor", None),
        "file_key": getattr(args, "diagnostics_file_key", None),
        "path_fragment": getattr(args, "diagnostics_path", None),
        "reason": getattr(args, "diagnostics_reason", None),
    }


def _validation_error(args: argparse.Namespace, message: str) -> NoReturn:
    if getattr(args, "diagnostics_json", False):
        v2_name = next(
            (name for name, _ in _V2_SELECTORS if getattr(args, name, None) is not None),
            None,
        )
        if v2_name is not None:
            payload = content_diagnostics_v2_error_payload(
                getattr(args, "diagnostics_owner", "all"),
                getattr(args, "root", None),
                state_directory=getattr(args, "state_directory", None),
                kind="invalid_request",
                message=message,
                limit=getattr(args, v2_name, 20),
            )
            print(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))
            raise SystemExit(2)
        name, owner = next(
            ((name, owner) for name, owner in _SELECTORS if getattr(args, name, None) is not None),
            ("pdf_diagnostics", "pdf"),
        )
        options = _options(args)
        options.pop("cursor")
        selected_limit = getattr(args, name, None)
        if not isinstance(selected_limit, int) or isinstance(selected_limit, bool):
            selected_limit = 20
        payload = content_diagnostics_error_payload(
            owner,
            getattr(args, "root", None),
            kind="invalid_request",
            message=message,
            limit=selected_limit,
            file_key=options["file_key"],
            path_fragment=options["path_fragment"],
            reason=options["reason"],
        )
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))
        raise SystemExit(2)
    raise SystemExit(message)


def validate_content_diagnostics_arguments(args: argparse.Namespace) -> None:
    selected = [
        (name, owner) for name, owner in _SELECTORS if getattr(args, name, None) is not None
    ]
    selected_v2 = [
        name for name, _ in _V2_SELECTORS if getattr(args, name, None) is not None
    ]
    if selected_v2:
        if any(getattr(args, name, None) is not None for name, _ in _SELECTORS):
            _validation_error(args, "content diagnostic selectors are mutually exclusive")
        if getattr(args, "apply", False) or getattr(args, "organization_apply", False):
            _validation_error(args, "content diagnostics are read-only and cannot be combined with apply")
        if getattr(args, "all", False) or getattr(args, "route_only", False):
            _validation_error(args, "content diagnostics only read state and cannot be combined with --all or --route-only")
        try:
            selected_routes = normalize_route_selection(
                getattr(args, "route", "none") or "none", BUILTIN_ROUTE_ORDER
            )
        except ValueError as exc:
            _validation_error(args, str(exc))
        if selected_routes:
            _validation_error(args, "content diagnostics only read state and cannot be combined with --route")
        source_root = getattr(args, "root", None)
        selected_limit = getattr(args, selected_v2[0])
        if not isinstance(source_root, (Path, str)):
            _validation_error(args, "--root is required for content diagnostics")
        if not isinstance(selected_limit, int) or isinstance(selected_limit, bool):
            _validation_error(args, "diagnostic limit must be an integer")
        budget_values = {
            "max_rows": getattr(args, "diagnostics_budget_rows", None),
            "max_vectors": getattr(args, "diagnostics_budget_vectors", None),
            "max_temporary_bytes": getattr(args, "diagnostics_budget_temporary_bytes", None),
            "deadline_seconds": getattr(args, "diagnostics_deadline_seconds", None),
        }
        try:
            validate_content_diagnostics_v2_request(
                getattr(args, "diagnostics_owner", "all"),
                source_root,
                selected_limit,
                state_directory=getattr(args, "state_directory", None),
                cursor=getattr(args, "diagnostics_cursor", None),
                file_key=getattr(args, "diagnostics_file_key", None),
                path_fragment=getattr(args, "diagnostics_path", None),
                reason=getattr(args, "diagnostics_reason", None),
            )
            from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudget

            KnowledgeReadBudget(
                max_rows=budget_values["max_rows"],
                max_vectors=budget_values["max_vectors"],
                max_temporary_bytes=budget_values["max_temporary_bytes"],
                deadline_seconds=budget_values["deadline_seconds"],
            )
        except (TypeError, ValueError) as exc:
            _validation_error(args, str(exc))
        return
    if len(selected) > 1:
        _validation_error(args, "format diagnostic selectors are mutually exclusive")
    if not selected:
        if (
            any(value is not None for value in _options(args).values())
            or getattr(args, "diagnostics_json", False)
            or getattr(args, "diagnostics_owner", "all") != "all"
            or any(
                getattr(args, name, None) is not None
                for name in (
                    "diagnostics_budget_rows",
                    "diagnostics_budget_vectors",
                    "diagnostics_budget_temporary_bytes",
                    "diagnostics_deadline_seconds",
                )
            )
        ):
            _validation_error(
                args,
                "--diagnostics-* requires a content diagnostic selector",
            )
        return
    if getattr(args, "apply", False) or getattr(args, "organization_apply", False):
        _validation_error(
            args, "content diagnostics are read-only and cannot be combined with apply"
        )
    if getattr(args, "all", False) or getattr(args, "route_only", False):
        _validation_error(
            args,
            "content diagnostics only read state and cannot be combined with --all or --route-only",
        )
    try:
        selected_routes = normalize_route_selection(
            getattr(args, "route", "none") or "none", BUILTIN_ROUTE_ORDER
        )
    except ValueError as exc:
        _validation_error(args, str(exc))
    if selected_routes:
        _validation_error(
            args, "content diagnostics only read state and cannot be combined with --route"
        )
    name, owner = selected[0]
    source_root = getattr(args, "root", None)
    selected_limit = getattr(args, name, None)
    if not isinstance(source_root, (Path, str)):
        _validation_error(args, "--root is required for content diagnostics")
    if not isinstance(selected_limit, int) or isinstance(selected_limit, bool):
        _validation_error(args, "diagnostic limit must be an integer")
    try:
        validate_content_diagnostics_request(
            owner,
            source_root,
            selected_limit,
            **_options(args),
        )
    except (TypeError, ValueError) as exc:
        _validation_error(args, str(exc))


def _run(args: argparse.Namespace, owner: str, selector: str) -> int:
    source_root = getattr(args, "root", None)
    if not isinstance(source_root, (Path, str)):
        raise ValueError("--root is required for content diagnostics")
    payload = content_diagnostics_payload(
        owner,
        args.state_directory,
        source_root,
        getattr(args, selector),
        cursor=getattr(args, "diagnostics_cursor", None),
        file_key=getattr(args, "diagnostics_file_key", None),
        path_fragment=getattr(args, "diagnostics_path", None),
        reason=getattr(args, "diagnostics_reason", None),
    )
    if getattr(args, "diagnostics_json", False):
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))
    else:
        print(
            f"CONTENT_DIAGNOSTICS owner={owner} status={payload['status']} "
            f"returned={payload['count']} matched={payload['matched_count']} "
            f"truncated={payload['truncated']} root={json.dumps(payload['requested_root'])}"
        )
        items = payload["items"]
        assert isinstance(items, list)
        for item in items:
            assert isinstance(item, dict)
            record_id = item.get("record_id", item.get("file_key", item.get("issue_id")))
            path = item.get("path", item.get("container_path"))
            reason = item.get("error_type", item.get("reason_code")) or item.get("status")
            print(
                f"CONTENT_ITEM id={json.dumps(record_id)} path={json.dumps(path)} reason={json.dumps(reason)}"
            )
        if payload["next_cursor"] is not None:
            print(f"CONTENT_NEXT_CURSOR {payload['next_cursor']}")
        if payload["error"] is not None:
            print("CONTENT_ERROR " + json.dumps(payload["error"], ensure_ascii=False))
    return 0 if payload["status"] == "ok" else 2


def run_pdf_diagnostics(args: argparse.Namespace) -> int:
    return _run(args, "pdf", "pdf_diagnostics")


def run_text_errors(args: argparse.Namespace) -> int:
    return _run(args, "text", "text_errors")


def _read_budget(args: argparse.Namespace):
    max_rows = getattr(args, "diagnostics_budget_rows", None)
    max_vectors = getattr(args, "diagnostics_budget_vectors", None)
    max_temporary_bytes = getattr(args, "diagnostics_budget_temporary_bytes", None)
    deadline_seconds = getattr(args, "diagnostics_deadline_seconds", None)
    values = (max_rows, max_vectors, max_temporary_bytes, deadline_seconds)
    if all(value is None for value in values):
        return None
    from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudget

    return KnowledgeReadBudget(
        max_rows=max_rows,
        max_vectors=max_vectors,
        max_temporary_bytes=max_temporary_bytes,
        deadline_seconds=deadline_seconds,
    )


def run_content_diagnostics(args: argparse.Namespace) -> int:
    """Run the additive all-owner content-diagnostics/v2 CLI operation."""

    selector = next(
        (name for name, _ in _V2_SELECTORS if getattr(args, name, None) is not None),
        "content_diagnostics",
    )
    source_root = getattr(args, "root", None)
    if not isinstance(source_root, (Path, str)):
        raise ValueError("--root is required for content diagnostics")
    payload = content_diagnostics_v2_payload(
        getattr(args, "diagnostics_owner", "all"),
        getattr(args, "state_directory", None),
        source_root,
        getattr(args, selector),
        budget=_read_budget(args),
        cursor=getattr(args, "diagnostics_cursor", None),
        file_key=getattr(args, "diagnostics_file_key", None),
        path_fragment=getattr(args, "diagnostics_path", None),
        reason=getattr(args, "diagnostics_reason", None),
    )
    if getattr(args, "diagnostics_json", False):
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))
    else:
        print(
            f"CONTENT_DIAGNOSTICS_V2 status={payload['status']} "
            f"returned={payload['count']} matched={payload['matched_count']} "
            f"truncated={payload['truncated']} root={json.dumps(payload['requested_root'])}"
        )
        owner_states = payload.get("owner_states", {})
        if isinstance(owner_states, dict):
            for owner, state in owner_states.items():
                if isinstance(state, dict):
                    print(
                        f"CONTENT_OWNER owner={json.dumps(owner)} "
                        f"state={json.dumps(state.get('state'))} "
                        f"status={json.dumps(state.get('status'))}"
                    )
        items = payload.get("items", [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    print(
                        f"CONTENT_ITEM owner={json.dumps(item.get('owner'))} "
                        f"id={json.dumps(item.get('record_id'))} "
                        f"path={json.dumps(item.get('path'))} "
                        f"reason={json.dumps(item.get('error_type'))}"
                    )
        if payload.get("next_cursor") is not None:
            print(f"CONTENT_NEXT_CURSOR {payload['next_cursor']}")
        if payload.get("error") is not None:
            print("CONTENT_ERROR " + json.dumps(payload["error"], ensure_ascii=False))
    return 0 if payload["status"] in {"ok", "empty"} else 2


__all__ = [
    "register_content_diagnostics_arguments",
    "run_content_diagnostics",
    "run_pdf_diagnostics",
    "run_text_errors",
    "validate_content_diagnostics_arguments",
]
