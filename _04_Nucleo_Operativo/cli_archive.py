"""Read-only CLI status, search and listing for indexed ZIP members."""

from __future__ import annotations

import argparse
import json
import sqlite3

from .archive_state import (
    ArchiveSearchHit,
    list_archive_members,
    read_archive_status,
    search_archive_state,
)

__all__ = ("run_archive_list", "run_archive_search", "run_archive_status")


def _database(args: argparse.Namespace):
    return args.state_directory / "archive.sqlite3"


def _print_hit(hit: ArchiveSearchHit, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {"inside_zip": True, "location": "archive_member", **hit.to_dict()},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    print(
        "ARCHIVE_MEMBER location=archive_member inside_zip=1 "
        f"depth={hit.archive_depth} status={hit.status} kind={hit.content_kind} "
        f"container={json.dumps(hit.container_path, ensure_ascii=False)} "
        f"member={json.dumps(hit.member_path, ensure_ascii=False)} "
        f"chain={json.dumps(hit.member_chain, ensure_ascii=False)} "
        f"virtual_path={json.dumps(hit.virtual_path, ensure_ascii=False)} "
        f"snippet={json.dumps(hit.snippet, ensure_ascii=False)}"
    )


def run_archive_status(args: argparse.Namespace) -> int:
    try:
        status = read_archive_status(_database(args))
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR archive-status {type(exc).__name__}: {exc}")
        return 2
    if args.archive_json:
        print(
            json.dumps(
                {"kind": "archive-status", **status.to_dict()},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        print(
            f"ARCHIVE_STATUS state={'available' if status.available else 'missing'} "
            f"schema={status.schema_version or '-'} containers={status.containers} "
            f"complete={status.complete} partial={status.partial} errors={status.errors} "
            f"members={status.members} indexed={status.indexed} "
            f"metadata_only={status.metadata_only} nested={status.nested_archives} "
            f"issues={status.issues} text_chars={status.text_chars}"
        )
    return 0 if status.available else 2


def run_archive_search(args: argparse.Namespace) -> int:
    try:
        hits = search_archive_state(
            _database(args),
            args.archive_search,
            args.archive_search_limit,
            container_fragment=args.archive_container,
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR archive-search {type(exc).__name__}: {exc}")
        return 2
    for hit in hits:
        _print_hit(hit, json_output=args.archive_json)
    return 0 if hits else 3


def run_archive_list(args: argparse.Namespace) -> int:
    try:
        hits = list_archive_members(
            _database(args),
            args.archive_list,
            container_fragment=args.archive_container,
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR archive-list {type(exc).__name__}: {exc}")
        return 2
    for hit in hits:
        _print_hit(hit, json_output=args.archive_json)
    return 0 if hits else 3
