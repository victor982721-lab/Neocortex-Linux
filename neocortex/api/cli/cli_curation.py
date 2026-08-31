"""Read-only curation preview command adapter."""

from __future__ import annotations

import argparse
import json
import sqlite3

from neocortex.curation import CurationStateError, build_curation_preview


def run_curation_preview(args: argparse.Namespace) -> int:
    """Show bounded duplicate, organization, and empty-file proposals."""

    try:
        preview = build_curation_preview(
            args.state_directory,
            limit=args.curation_preview,
        )
    except (CurationStateError, OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR curation-preview {type(exc).__name__}: {exc}")
        return 2

    if args.curation_json:
        print(
            json.dumps(
                {"kind": "curation-preview", **preview.to_dict()},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    print(
        f"CURATION_PREVIEW scan={preview.scan_id} root={preview.root} "
        f"inventory_files={preview.inventory_files} "
        f"duplicate_groups={preview.duplicate_groups} "
        f"duplicate_members={preview.duplicate_members} "
        f"reclaimable_bytes={preview.reclaimable_bytes} "
        f"organization_plans={preview.organization_plans} "
        f"empty_files={preview.empty_files} "
        f"items={len(preview.items)}/{preview.items_total} "
        f"truncated={int(preview.items_truncated)} "
        f"preview_fingerprint={preview.preview_fingerprint}"
    )
    for item in preview.items:
        evidence = json.dumps(
            item.evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        print(
            f"CURATION_ITEM id={item.item_id} kind={item.kind} status={item.status} "
            f"action={item.action} reason={item.reason} source={item.source_path} "
            f"destination={item.destination_path or '-'} evidence={evidence}"
        )
    return 0


__all__ = ["run_curation_preview"]
