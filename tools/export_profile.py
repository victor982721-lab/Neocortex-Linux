"""Versioned Git-derived checkout, sanitized export and wheel contracts.

Profiles are explicit, never inferred from missing instructions or documents.
The commit object and complete Git tree bind the source catalog; every emitted
file is checked against its blob, with reversible, recorded link rewrites for
excluded source-only documents. This module never changes the source checkout.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import subprocess
from typing import Any, Literal
from urllib.parse import quote, unquote, urlsplit, urlunsplit

SCHEMA = "neocortex.export-profile/v1"
MANIFEST_NAME = "neocortex-export-profile.json"
MAX_SOURCE_FILES = 20_000
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_FILE_BYTES = 512 * 1024**2
MAX_MANIFEST_BYTES = 32 * 1024**2

# Canonical document inventory is shared with the documentation contract tests.
ACTIVE_DOCUMENTS = frozenset(
    {
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/CHANGELOG.md",
        "docs/CLI.md",
        "docs/AGENT_ACTIVITY.md",
        "docs/FILE_INTELLIGENCE_AND_CURATION.md",
        "docs/KNOWLEDGE.md",
        "docs/KNOWLEDGE_OPERATIONAL_QUERY.md",
        "docs/LINUX_KUBUNTU.md",
        "docs/OPERATIONS.md",
        "docs/PERSISTENCE.md",
        "docs/RECOVERY.md",
        "docs/ROADMAP_90_DAYS.md",
        "docs/SECURITY.md",
        "docs/SUBPROJECTS.md",
        "docs/subprojects/code-content.md",
        "docs/subprojects/curation-effects.md",
        "docs/subprojects/development-release.md",
        "docs/subprojects/formats.md",
        "docs/subprojects/interfaces.md",
        "docs/subprojects/inventory-catalog.md",
        "docs/subprojects/platform-state.md",
        "docs/subprojects/retrieval-context.md",
    }
)
SOURCE_ONLY_DOCUMENTS = frozenset(
    {
        ".codex/handoffs/NEOCORTEX_0.12.0_BUGFIX_2026-09-05.md",
        ".codex/handoffs/NEOCORTEX_0.12.0_SCALE_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.12.1_LIFECYCLE_2026-09-05.md",
        ".codex/handoffs/NEOCORTEX_0.13.0_BUDGETS_2026-09-05.md",
        ".codex/handoffs/NEOCORTEX_FUNCTIONAL_2026-09-06.md",
        ".codex/handoffs/CURRENT.md",
        ".codex/handoffs/NEOCORTEX_0.11.1_RECOVERY_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.11.0_APPLY_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.10.0_CURATION_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.9.0_CURATION_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.7.2_PAUSE_2026-07-30.md",
        "AGENTS.md",
        "neocortex/api/AGENTS.md",
        "neocortex/capabilities/AGENTS.md",
        "neocortex/code/AGENTS.md",
        "neocortex/curation/AGENTS.md",
        "neocortex/deduplication/AGENTS.md",
        "neocortex/documents/AGENTS.md",
        "neocortex/interface/AGENTS.md",
        "neocortex/knowledge/AGENTS.md",
        "neocortex/persistence/AGENTS.md",
        "neocortex/runtime/AGENTS.md",
        "neocortex/semantic/AGENTS.md",
        "neocortex/workflow/AGENTS.md",
    }
)

_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)")
_REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?P<target><[^>]+>|\S+)")
_HTML_LINK = re.compile(r"\b(?:href|src)=[\"'](?P<target>[^\"']+)[\"']", re.IGNORECASE)
_OID = re.compile(r"[0-9a-f]{40}\Z")


class ExportProfileError(ValueError):
    """The declared profile or artifact cannot be verified."""


@dataclass(frozen=True, slots=True)
class ExportProfile:
    name: Literal["checkout", "sanitized", "release"]
    source_commit: str
    tree_hash: str
    allowed_paths: tuple[str, ...]
    required_paths: tuple[str, ...]
    excluded_paths: dict[str, str]
    schema: str = SCHEMA


def documentation_paths(profile: str) -> frozenset[str]:
    if profile == "checkout":
        return ACTIVE_DOCUMENTS | SOURCE_ONLY_DOCUMENTS
    if profile == "sanitized":
        return ACTIVE_DOCUMENTS
    raise ExportProfileError("documentation tests require checkout or sanitized profile")


def _safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or not path.parts or path.is_absolute() or any(part in {".", "..", ".git"} for part in path.parts) or path.as_posix() != value or "\\" in value or "\x00" in value:
        raise ExportProfileError(f"invalid source path: {value!r}")
    return value


def _repository_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or not url.netloc or not url.path.strip("/") or url.query or url.fragment or url.username or url.password:
        raise ExportProfileError("repository_url must be an explicit public HTTPS repository URL")
    return value.rstrip("/")


def _git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments], check=False, capture_output=True, timeout=60,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"},
    )
    if result.returncode:
        raise ExportProfileError("Git source lookup failed: " + result.stderr.decode("utf-8", "replace")[:500])
    return result.stdout


def _git_hash(kind: str, data: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(data)}\0".encode("ascii") + data).hexdigest()


def _tree_hash(entries: dict[str, dict[str, Any]]) -> str:
    tree: dict[str, Any] = {}
    for path, entry in entries.items():
        parts = PurePosixPath(_safe_path(path)).parts
        current = tree
        for component in parts[:-1]:
            child = current.setdefault(component, {})
            if not isinstance(child, dict):
                raise ExportProfileError("file/directory collision in Git tree")
            current = child
        if parts[-1] in current:
            raise ExportProfileError("duplicate Git tree entry")
        mode, blob = entry.get("mode"), entry.get("git_blob")
        if mode not in {"100644", "100755"} or not isinstance(blob, str) or not _OID.fullmatch(blob):
            raise ExportProfileError("only regular Git source blobs are supported")
        current[parts[-1]] = (mode, blob)
    def digest(node: dict[str, Any]) -> str:
        payload = bytearray()
        for name in sorted(node, key=lambda value: (value + "/" if isinstance(node[value], dict) else value).encode("utf-8")):
            value = node[name]
            mode, oid = ("40000", digest(value)) if isinstance(value, dict) else value
            payload.extend(f"{mode} {name}\0".encode("utf-8"))
            payload.extend(bytes.fromhex(oid))
        return _git_hash("tree", bytes(payload))
    return digest(tree)


def _source_catalog(repo: Path, source_ref: str) -> tuple[str, bytes, str, dict[str, dict[str, Any]]]:
    commit = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{source_ref}^{{commit}}").decode().strip()
    if not _OID.fullmatch(commit):
        raise ExportProfileError("source must resolve to a SHA-1 Git commit")
    commit_object = _git(repo, "cat-file", "commit", commit)
    tree = commit_object.splitlines()[0].removeprefix(b"tree ").decode("ascii")
    entries: dict[str, dict[str, Any]] = {}
    total = 0
    for row in _git(repo, "ls-tree", "-rlz", "--full-tree", commit).split(b"\0"):
        if not row:
            continue
        header, raw_path = row.split(b"\t", 1)
        mode, kind, blob, size = header.decode("ascii").split()
        path = _safe_path(raw_path.decode("utf-8"))
        if path == MANIFEST_NAME:
            raise ExportProfileError("source uses the reserved export manifest path")
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ExportProfileError("source tree contains an unsupported non-regular entry")
        byte_count = int(size)
        total += byte_count
        if byte_count > MAX_FILE_BYTES or total > MAX_SOURCE_BYTES or len(entries) >= MAX_SOURCE_FILES:
            raise ExportProfileError("source export exceeds bounded profile limits")
        entries[path] = {"mode": mode, "git_blob": blob, "size": byte_count}
    if _tree_hash(entries) != tree or _git_hash("commit", commit_object) != commit:
        raise ExportProfileError("Git source identity did not verify")
    missing = (ACTIVE_DOCUMENTS | SOURCE_ONLY_DOCUMENTS) - entries.keys()
    if missing:
        raise ExportProfileError(f"source commit lacks required canonical documents: {sorted(missing)}")
    return commit, commit_object, tree, entries


def _exclusions(profile: str, entries: dict[str, Any]) -> dict[str, str]:
    if profile == "checkout":
        return {}
    if profile != "sanitized":
        raise ExportProfileError("Git directory export requires checkout or sanitized profile")
    return {
        path: "source_only_document" if path in SOURCE_ONLY_DOCUMENTS else "source_only_agent_control"
        for path in sorted(entries)
        if path in SOURCE_ONLY_DOCUMENTS or path.startswith(".codex/") or PurePosixPath(path).name in {"AGENTS.md", "AGENTS.override.md"}
    }


def _rewrite_links(text: str, source: str, excluded: dict[str, str], repository_url: str, commit: str) -> tuple[str, list[dict[str, Any]]]:
    edits: list[dict[str, Any]] = []
    offset = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        marker = line.lstrip()[:3]
        if marker in {"```", "~~~"}:
            fence = marker if fence is None else None if fence == marker else fence
        elif fence is None:
            matches = [match for pattern in (_INLINE_LINK, _REFERENCE_LINK, _HTML_LINK) for match in pattern.finditer(line)]
            for match in sorted(matches, key=lambda item: item.start("target")):
                target = match.group("target")
                parts = urlsplit(target.strip("<>"))
                if parts.scheme or parts.netloc or not parts.path:
                    continue
                destination = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(parts.path)))
                if destination not in excluded:
                    continue
                permalink = urlunsplit(("https", urlsplit(repository_url).netloc,
                    urlsplit(repository_url).path.rstrip("/") + "/blob/" + commit + "/" + quote(destination, safe="/"), parts.query, parts.fragment))
                replacement = f"<{permalink}>" if target.startswith("<") else permalink
                edits.append({"start": offset + match.start("target"), "end": offset + match.end("target"), "before": target, "after": replacement, "excluded_target": destination})
        offset += len(line)
    result = text
    for edit in reversed(edits):
        result = result[:edit["start"]] + edit["after"] + result[edit["end"]:]
    return result, edits


def _restore_links(text: str, edits: list[dict[str, Any]]) -> str:
    delta = 0
    adjusted = []
    previous_end = 0
    for edit in edits:
        start, end = edit["start"], edit["end"]
        if type(start) is not int or type(end) is not int or start < previous_end or end - start != len(edit["before"]):
            raise ExportProfileError("invalid link transformation range")
        output_start = start + delta
        output_end = output_start + len(edit["after"])
        if text[output_start:output_end] != edit["after"]:
            raise ExportProfileError("rewritten link does not match its transformation")
        adjusted.append((output_start, output_end, edit["before"]))
        delta += len(edit["after"]) - (end - start)
        previous_end = end
    for start, end, before in reversed(adjusted):
        text = text[:start] + before + text[end:]
    return text


def export_from_git(
    repository: Path, destination: Path, *, profile: Literal["checkout", "sanitized"],
    repository_url: str, source_ref: str = "HEAD",
) -> Path:
    """Create a new export from immutable Git blobs, never from dirty files."""
    repo = repository.resolve(strict=True)
    target = destination.resolve(strict=False)
    repository_url = _repository_url(repository_url)
    if target.exists() or target.is_symlink() or target.is_relative_to(repo) or repo.is_relative_to(target):
        raise ExportProfileError("export destination must be new and outside the source checkout")
    commit, commit_object, tree, entries = _source_catalog(repo, source_ref)
    excluded = _exclusions(profile, entries)
    allowed = tuple(sorted(entries.keys() - excluded.keys()))
    selected = ExportProfile(profile, commit, tree, allowed, allowed, excluded)
    files = {}
    transformations = {}
    target.mkdir(mode=0o700)
    for path in allowed:
        source = _git(repo, "cat-file", "blob", entries[path]["git_blob"])
        if len(source) != entries[path]["size"] or _git_hash("blob", source) != entries[path]["git_blob"]:
            raise ExportProfileError("source blob changed or failed its Git digest")
        emitted = source
        edits: list[dict[str, Any]] = []
        if path.endswith(".md") and profile == "sanitized":
            text, edits = _rewrite_links(source.decode("utf-8"), path, excluded, repository_url, commit)
            emitted = text.encode("utf-8")
        if edits:
            transformations[path] = edits
        output = target / path
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with output.open("xb") as stream:
            stream.write(emitted)
        output.chmod(0o755 if entries[path]["mode"] == "100755" else 0o644)
        files[path] = {"source_sha256": hashlib.sha256(source).hexdigest(), "sha256": hashlib.sha256(emitted).hexdigest(), "size": len(emitted)}
    manifest = {
        **asdict(selected), "repository_url": repository_url.rstrip("/"),
        "source_commit_object_b64": base64.b64encode(commit_object).decode("ascii"),
        "source_tree_entries": entries, "files": files, "transformations": transformations,
    }
    manifest_path = target / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    validate_export_directory(target, manifest_path, expected_profile=profile, expected_commit=commit)
    return manifest_path


def validate_export_directory(
    root: Path, manifest_path: Path, *, expected_profile: str, expected_commit: str | None = None,
) -> ExportProfile:
    """Verify source tree, exact inventory, transforms and every emitted hash."""
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ExportProfileError("export manifest exceeds its size bound")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if _repository_url(value["repository_url"]) != value["repository_url"]:
        raise ExportProfileError("repository URL is not canonical")
    if value.get("schema") != SCHEMA or value.get("name") != expected_profile:
        raise ExportProfileError("export profile must match the explicit declaration")
    commit = value["source_commit"]
    if not isinstance(commit, str) or not _OID.fullmatch(commit) or (expected_commit is not None and commit != expected_commit):
        raise ExportProfileError("source commit does not match the requested export")
    commit_object = base64.b64decode(value["source_commit_object_b64"], validate=True)
    entries = value["source_tree_entries"]
    if len(entries) > MAX_SOURCE_FILES or any(
        type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES
        for entry in entries.values()
    ) or sum(entry["size"] for entry in entries.values()) > MAX_SOURCE_BYTES:
        raise ExportProfileError("export source catalog exceeds bounded limits")
    if _git_hash("commit", commit_object) != commit or commit_object.splitlines()[0] != b"tree " + value["tree_hash"].encode("ascii") or _tree_hash(entries) != value["tree_hash"]:
        raise ExportProfileError("export source commit/tree binding is invalid")
    if not (ACTIVE_DOCUMENTS | SOURCE_ONLY_DOCUMENTS) <= entries.keys():
        raise ExportProfileError("source tree omits required canonical documents")
    excluded = _exclusions(expected_profile, entries)
    allowed = tuple(sorted(entries.keys() - excluded.keys()))
    if value["excluded_paths"] != excluded or value["allowed_paths"] != list(allowed) or value["required_paths"] != list(allowed) or set(value["files"]) != set(allowed):
        raise ExportProfileError("manifest changes the profile's exact allowed/required/excluded inventory")
    if set(value["transformations"]) - set(allowed):
        raise ExportProfileError("transformation is outside the allowed inventory")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ExportProfileError("export contains a symlink")
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir() and path != manifest_path:
            raise ExportProfileError("export contains a non-regular member")
        if len(actual) > len(allowed):
            raise ExportProfileError("export contains additional files")
    if actual != set(allowed):
        raise ExportProfileError(f"export inventory mismatch; missing={sorted(set(allowed)-actual)}; unexpected={sorted(actual-set(allowed))}")
    for path in allowed:
        record = value["files"][path]
        info = (root / path).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES + 1024**2 or bool(info.st_mode & 0o111) != (entries[path]["mode"] == "100755"):
            raise ExportProfileError(f"export member mode or size mismatch: {path}")
        emitted = (root / path).read_bytes()
        if len(emitted) != record["size"] or hashlib.sha256(emitted).hexdigest() != record["sha256"]:
            raise ExportProfileError(f"exported file hash mismatch: {path}")
        edits = value["transformations"].get(path, [])
        source = _restore_links(emitted.decode("utf-8"), edits).encode("utf-8") if edits else emitted
        if len(source) != entries[path]["size"] or hashlib.sha256(source).hexdigest() != record["source_sha256"] or _git_hash("blob", source) != entries[path]["git_blob"]:
            raise ExportProfileError(f"source blob proof failed: {path}")
        if path.endswith(".md") and expected_profile == "sanitized":
            rewritten, expected_edits = _rewrite_links(source.decode("utf-8"), path, excluded, value["repository_url"], commit)
            if rewritten.encode("utf-8") != emitted or edits != expected_edits:
                raise ExportProfileError(f"noncanonical export link transformation: {path}")
        elif edits:
            raise ExportProfileError("only sanitized Markdown links may be rewritten")
    if expected_profile not in {"checkout", "sanitized"}:
        raise ExportProfileError("directory profile must be checkout or sanitized")
    name: Literal["checkout", "sanitized"] = "checkout" if expected_profile == "checkout" else "sanitized"
    return ExportProfile(name, commit, value["tree_hash"], allowed, allowed, excluded)


def release_profile(wheel: Path, *, source_commit: str, tree_hash: str) -> dict[str, Any]:
    """Use the release owner's RECORD validator instead of document inference."""
    from tools.release_artifacts import validate_wheel
    if not _OID.fullmatch(source_commit) or not _OID.fullmatch(tree_hash):
        raise ExportProfileError("release provenance requires explicit source commit and tree")
    inspected = validate_wheel(wheel)
    if not inspected.record_verified:
        raise ExportProfileError("wheel RECORD did not verify")
    paths = tuple(sorted(member.path for member in inspected.members))
    return {
        **asdict(ExportProfile("release", source_commit, tree_hash, paths, paths, {})),
        "wheel_sha256": inspected.archive_sha256, "record_verified": True,
        "version": inspected.version, "files": {member.path: {"sha256": member.sha256, "size": member.size} for member in inspected.members},
        "source_binding": "caller-supplied release provenance; RECORD proves wheel contents, not their source commit",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--profile", choices=("checkout", "sanitized"), required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--source-ref", default="HEAD")
    args = parser.parse_args(argv)
    path = export_from_git(args.repository, args.destination, profile=args.profile, repository_url=args.repository_url, source_ref=args.source_ref)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
