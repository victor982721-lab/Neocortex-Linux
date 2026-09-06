"""Small read-only pagination contract shared by format diagnostics.

The token identifies a state-file generation and exact selector, not permission
to read another owner. No source file or SQLite sidecar is opened for writing.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


MAX_DIAGNOSTIC_RESULTS = 1_000


@dataclass(frozen=True, slots=True)
class DiagnosticPage:
    items: tuple[dict[str, object], ...]
    next_cursor: str | None
    scope: dict[str, object]
    snapshot_id: str
    matched_count: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_limit(limit: int) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_DIAGNOSTIC_RESULTS
    ):
        raise ValueError(f"limit must be between 1 and {MAX_DIAGNOSTIC_RESULTS}")


def selector(
    *,
    file_key: str | None = None,
    path_scope: str | None = None,
    path_fragment: str | None = None,
) -> tuple[list[str], list[object], dict[str, object]]:
    clauses: list[str] = []
    params: list[object] = []
    scope: dict[str, object] = {}
    for name, value in (
        ("file_key", file_key),
        ("path_scope", path_scope),
        ("path_fragment", path_fragment),
    ):
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 4_096 or "\x00" in value
        ):
            raise ValueError(f"invalid {name}")
    if file_key is not None:
        clauses.append("d.file_key=?")
        params.append(file_key)
        scope["file_key"] = file_key
    if path_scope is not None:
        if not path_scope.startswith("/"):
            raise ValueError("path_scope must be an absolute Linux path")
        normalized = path_scope.rstrip("/") or "/"
        prefix = normalized.rstrip("/") + "/"
        clauses.append("(d.path=? COLLATE BINARY OR substr(d.path,1,?)=? COLLATE BINARY)")
        params.extend((normalized, len(prefix), prefix))
        scope["path_scope"] = normalized
    if path_fragment is not None:
        clauses.append("instr(d.path,?)>0")
        params.append(path_fragment)
        scope["path_fragment"] = path_fragment
    return clauses, params, scope


def state_snapshot_id(path: Path) -> str:
    """Observe original-owner identity; never hash/decompress the corpus."""

    records: list[object] = [str(path.absolute())]
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            info = candidate.stat()
        except FileNotFoundError:
            records.append(None)
        else:
            records.append(
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            )
    return hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()


def verify_snapshot(path: Path, expected: str) -> None:
    if state_snapshot_id(path) != expected:
        raise ValueError("format state changed during diagnostic read; retry from first page")


def decode_cursor(cursor: str | None, *, snapshot_id: str, scope: Mapping[str, object]) -> str:
    if cursor is None:
        return ""
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 8_192:
            raise ValueError("invalid cursor size")
        decoded = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(decoded)
        if not isinstance(payload, dict) or set(payload) != {"v", "snapshot", "scope", "after"}:
            raise ValueError("invalid cursor shape")
        if (
            payload["v"] != 1
            or payload["snapshot"] != snapshot_id
            or payload["scope"] != dict(scope)
        ):
            raise ValueError("cursor scope or state generation changed")
        after = payload["after"]
        if not isinstance(after, str) or not after or len(after) > 4_096 or "\x00" in after:
            raise ValueError("invalid cursor key")
        return after
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError(f"invalid diagnostic cursor: {exc}") from exc


def encode_cursor(after: str, *, snapshot_id: str, scope: Mapping[str, object]) -> str:
    value = {"v": 1, "snapshot": snapshot_id, "scope": dict(scope), "after": after}
    return base64.urlsafe_b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).decode("ascii")
