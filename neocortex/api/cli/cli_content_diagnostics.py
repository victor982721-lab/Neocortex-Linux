"""Thin CLI adapters for root-scoped PDF, Text and Archive diagnostics."""

from __future__ import annotations

import argparse
import json
from typing import NoReturn
from neocortex.api.content_diagnostics_api import (
    content_diagnostics_error_payload,
    content_diagnostics_payload,
    validate_content_diagnostics_request,
)
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    normalize_route_selection,
)

_SELECTORS = (("pdf_diagnostics", "pdf"), ("text_errors", "text"), ("archive_issues", "archive"))


def register_content_diagnostics_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    for name, owner in _SELECTORS:
        group.add_argument(
            "--" + name.replace("_", "-"), type=int, metavar="N",
            help=f"Read at most N persisted {owner} diagnostic records under --root",
        )
    parser.add_argument("--diagnostics-cursor", help="Continue the same owner/root/filter snapshot")
    parser.add_argument("--diagnostics-file-key", help="Exact file key, or Archive physical container key")
    parser.add_argument("--diagnostics-path", help="Literal path fragment within the requested root")
    parser.add_argument("--diagnostics-reason", help="Exact PDF/Text error_type or Archive reason_code")
    parser.add_argument("--diagnostics-json", action="store_true", help="Print the structured diagnostic envelope")


def _options(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "cursor": getattr(args, "diagnostics_cursor", None),
        "file_key": getattr(args, "diagnostics_file_key", None),
        "path_fragment": getattr(args, "diagnostics_path", None),
        "reason": getattr(args, "diagnostics_reason", None),
    }


def _validation_error(args: argparse.Namespace, message: str) -> NoReturn:
    if getattr(args, "diagnostics_json", False):
        name, owner = next(
            ((name, owner) for name, owner in _SELECTORS if getattr(args, name, None) is not None),
            ("pdf_diagnostics", "pdf"),
        )
        options = _options(args)
        options.pop("cursor")
        payload = content_diagnostics_error_payload(
            owner, getattr(args, "root", None), kind="invalid_request", message=message,
            limit=getattr(args, name, None), **options,
        )
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))
        raise SystemExit(2)
    raise SystemExit(message)


def validate_content_diagnostics_arguments(args: argparse.Namespace) -> None:
    selected = [(name, owner) for name, owner in _SELECTORS if getattr(args, name, None) is not None]
    if len(selected) > 1:
        _validation_error(args, "format diagnostic selectors are mutually exclusive")
    if not selected:
        if any(value is not None for value in _options(args).values()) or getattr(args, "diagnostics_json", False):
            _validation_error(args, "--diagnostics-* requires --pdf-diagnostics, --text-errors or --archive-issues")
        return
    if getattr(args, "apply", False) or getattr(args, "organization_apply", False):
        _validation_error(args, "content diagnostics are read-only and cannot be combined with apply")
    if getattr(args, "all", False) or getattr(args, "route_only", False):
        _validation_error(args, "content diagnostics only read state and cannot be combined with --all or --route-only")
    try:
        selected_routes = normalize_route_selection(getattr(args, "route", "none") or "none", BUILTIN_ROUTE_ORDER)
    except ValueError as exc:
        _validation_error(args, str(exc))
    if selected_routes:
        _validation_error(args, "content diagnostics only read state and cannot be combined with --route")
    name, owner = selected[0]
    try:
        validate_content_diagnostics_request(
            owner, getattr(args, "root", None), getattr(args, name), **_options(args),
        )
    except (TypeError, ValueError) as exc:
        _validation_error(args, str(exc))


def _run(args: argparse.Namespace, owner: str, selector: str) -> int:
    payload = content_diagnostics_payload(
        owner, args.state_directory, getattr(args, "root", None),
        getattr(args, selector), **_options(args),
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
            print(f"CONTENT_ITEM id={json.dumps(record_id)} path={json.dumps(path)} reason={json.dumps(reason)}")
        if payload["next_cursor"] is not None:
            print(f"CONTENT_NEXT_CURSOR {payload['next_cursor']}")
        if payload["error"] is not None:
            print("CONTENT_ERROR " + json.dumps(payload["error"], ensure_ascii=False))
    return 0 if payload["status"] == "ok" else 2


def run_pdf_diagnostics(args: argparse.Namespace) -> int:
    return _run(args, "pdf", "pdf_diagnostics")


def run_text_errors(args: argparse.Namespace) -> int:
    return _run(args, "text", "text_errors")


def run_archive_issues(args: argparse.Namespace) -> int:
    return _run(args, "archive", "archive_issues")


__all__ = [
    "register_content_diagnostics_arguments",
    "run_archive_issues",
    "run_pdf_diagnostics",
    "run_text_errors",
    "validate_content_diagnostics_arguments",
]
