"""Canonical model lifecycle command handlers."""

from __future__ import annotations
import argparse
import json
import sys

from neocortex.runtime.config.model_management import inspect_models, prepare_models


def _print(report: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return
    print(
        f"MODELS schema={report['schema_version']} "
        f"all_prepared={int(bool(report['all_prepared']))} root={report['models_root']}"
    )
    models = report["models"]
    assert isinstance(models, list)
    for item in models:
        assert isinstance(item, dict)
        print(
            f"MODEL id={item['model_id']} kind={item['kind']} "
            f"prepared={int(bool(item['prepared']))} reason={item['reason']} "
            f"location={json.dumps(item['location'], ensure_ascii=False)}"
        )


def run_models_status(args: argparse.Namespace) -> int:
    try:
        report = inspect_models(**_model_options(args))
        _print(report, as_json=args.models_json)
    except Exception as exc:
        print(f"ERROR models-status {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0 if report["all_prepared"] else 2


def run_models_prepare(args: argparse.Namespace) -> int:
    try:
        report = prepare_models(**_model_options(args))
        _print(report, as_json=args.models_json)
    except Exception as exc:
        print(f"ERROR models-prepare {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    # Preparation is successful only when every requested model is actually
    # available.  ``prepare_models`` deliberately returns a report for
    # missing/offline resources instead of raising, so treating every report
    # as exit 0 used to publish a false completion to scripts and --all
    # callers.
    status = str(report.get("status", "")).casefold()
    if status in {"blocked", "cancelled", "error", "failed", "partial", "unavailable"}:
        return 130 if status == "cancelled" else 2
    return 0 if report.get("all_prepared") is True else 2


def _model_options(args: argparse.Namespace) -> dict:
    options = {}
    if getattr(args, "models_root", None) is not None:
        options["models_root"] = args.models_root
    if getattr(args, "models_model_id", None) is not None:
        options["model_ids"] = args.models_model_id
    return options


__all__ = ["run_models_prepare", "run_models_status"]
