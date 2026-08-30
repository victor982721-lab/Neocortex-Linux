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
        report = inspect_models()
        _print(report, as_json=args.models_json)
    except Exception as exc:
        print(f"ERROR models-status {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0 if report["all_prepared"] else 2


def run_models_prepare(args: argparse.Namespace) -> int:
    try:
        report = prepare_models()
        _print(report, as_json=args.models_json)
    except Exception as exc:
        print(f"ERROR models-prepare {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


__all__ = ["run_models_prepare", "run_models_status"]
