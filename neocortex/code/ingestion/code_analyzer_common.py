"""Shared bounded utilities for language analyzers."""

from __future__ import annotations
import json
import re
import tomllib
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TypedDict

from ..code_contracts import (
    CodeChunk,
    DependencyRecord,
    DiagnosticRecord,
    DiagnosticSeverity,
    ProjectHint,
    SourceRange,
)
from .code_detection import normalized_tokens
from neocortex.semantic.semantic_models import fingerprint_bytes, fingerprint_text


# region [01] Source coordinate mapping


@dataclass(frozen=True, slots=True)
class SourceMap:
    """Translate line/column coordinates to UTF-8 byte offsets."""

    text: str
    lines: tuple[str, ...]
    line_char_offsets: tuple[int, ...]
    line_byte_offsets: tuple[int, ...]

    @classmethod
    def build(cls, text: str) -> "SourceMap":
        lines = tuple(text.splitlines(keepends=True))
        if not lines:
            lines = ("",)
        char_offsets: list[int] = []
        byte_offsets: list[int] = []
        char_cursor = 0
        byte_cursor = 0
        for line in lines:
            char_offsets.append(char_cursor)
            byte_offsets.append(byte_cursor)
            char_cursor += len(line)
            byte_cursor += len(line.encode("utf-8"))
        return cls(text, lines, tuple(char_offsets), tuple(byte_offsets))

    def line_text(self, line_number: int) -> str:
        index = min(max(line_number - 1, 0), len(self.lines) - 1)
        return self.lines[index]

    def byte_offset(self, line_number: int, column: int, *, utf8_column: bool) -> int:
        index = min(max(line_number - 1, 0), len(self.lines) - 1)
        line = self.lines[index]
        if utf8_column:
            return self.line_byte_offsets[index] + min(
                max(column, 0), len(line.encode("utf-8"))
            )
        prefix = line[: min(max(column, 0), len(line))]
        return self.line_byte_offsets[index] + len(prefix.encode("utf-8"))

    def source_range(
        self,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
        *,
        utf8_columns: bool = False,
    ) -> SourceRange:
        safe_start_line = min(max(start_line, 1), len(self.lines))
        safe_end_line = min(max(end_line, safe_start_line), len(self.lines))
        return SourceRange(
            safe_start_line,
            max(0, start_column),
            safe_end_line,
            max(0, end_column),
            self.byte_offset(safe_start_line, start_column, utf8_column=utf8_columns),
            self.byte_offset(safe_end_line, end_column, utf8_column=utf8_columns),
        )

    def offset_range(self, start: int, end: int) -> SourceRange:
        safe_start = min(max(start, 0), len(self.text))
        safe_end = min(max(end, safe_start), len(self.text))
        start_line_index = max(0, bisect_right(self.line_char_offsets, safe_start) - 1)
        end_line_index = max(0, bisect_right(self.line_char_offsets, safe_end) - 1)
        start_column = safe_start - self.line_char_offsets[start_line_index]
        end_column = safe_end - self.line_char_offsets[end_line_index]
        return self.source_range(
            start_line_index + 1,
            start_column,
            end_line_index + 1,
            end_column,
        )


# endregion [01]


# region [02] Search chunks and fingerprints


def searchable_chunks(text: str, max_chars: int) -> tuple[CodeChunk, ...]:
    """Split text at line boundaries with a strict per-chunk character bound."""

    if not text:
        return ()
    source_map = SourceMap.build(text)
    chunks: list[CodeChunk] = []
    start = 0
    cursor = 0
    for line in source_map.lines:
        next_cursor = cursor + len(line)
        if cursor > start and next_cursor - start > max_chars:
            chunks.append(
                CodeChunk(
                    index=len(chunks),
                    text=text[start:cursor],
                    source_range=source_map.offset_range(start, cursor),
                )
            )
            start = cursor
        if next_cursor - start > max_chars:
            while next_cursor - start > max_chars:
                end = start + max_chars
                chunks.append(
                    CodeChunk(
                        index=len(chunks),
                        text=text[start:end],
                        source_range=source_map.offset_range(start, end),
                    )
                )
                start = end
        cursor = next_cursor
    if start < len(text):
        chunks.append(
            CodeChunk(
                index=len(chunks),
                text=text[start:],
                source_range=source_map.offset_range(start, len(text)),
            )
        )
    return tuple(chunks)


class ComparisonFingerprints(TypedDict):
    """Named comparison fingerprints accepted by :class:`CodeAnalysis`."""

    raw_xxh3_128: str
    raw_xxh3_64_guard: str
    text_xxh3_128: str
    text_xxh3_64_guard: str
    normalized_xxh3_128: str | None
    token_xxh3_128: str | None
    structure_xxh3_128: str | None


def comparison_fingerprints(
    raw: bytes,
    text: str,
    language: str | None,
    *,
    structure: str | None,
) -> ComparisonFingerprints:
    """Return exact and comparison-only XXH3 fingerprints with explicit roles."""

    raw_fingerprint = fingerprint_bytes(raw)
    text_fingerprint = fingerprint_text(text)
    tokens = normalized_tokens(text, language)
    token_payload = "\0".join(tokens)
    normalized_payload = " ".join(tokens)
    return {
        "raw_xxh3_128": raw_fingerprint.xxh3_128,
        "raw_xxh3_64_guard": raw_fingerprint.xxh3_64_guard,
        "text_xxh3_128": text_fingerprint.xxh3_128,
        "text_xxh3_64_guard": text_fingerprint.xxh3_64_guard,
        "normalized_xxh3_128": (
            None if not normalized_payload else fingerprint_text(normalized_payload).xxh3_128
        ),
        "token_xxh3_128": (
            None if not token_payload else fingerprint_text(token_payload).xxh3_128
        ),
        "structure_xxh3_128": (
            None if structure is None else fingerprint_text(structure).xxh3_128
        ),
    }


# endregion [02]


# region [03] Manifest and project evidence


def _manifest_range(text: str) -> SourceRange:
    source_map = SourceMap.build(text)
    return source_map.offset_range(0, len(text))


def _dependency_records(
    values: Iterable[tuple[str, str | None]],
    *,
    kind: str,
    text: str,
) -> tuple[DependencyRecord, ...]:
    source_range = _manifest_range(text)
    return tuple(
        DependencyRecord(
            name=name,
            kind=kind,
            version_spec=version,
            source_range=source_range,
            evidence=f"manifest:{kind}",
        )
        for name, version in values
        if name
    )


type ManifestEvidence = tuple[
    tuple[ProjectHint, ...],
    tuple[DependencyRecord, ...],
    tuple[DiagnosticRecord, ...],
]


def _manifest_parse_error(tool: str, exc: Exception) -> DiagnosticRecord:
    return DiagnosticRecord(
        source="manifest-parser",
        code="manifest_parse_error",
        severity=DiagnosticSeverity.ERROR,
        message=str(exc)[:4096],
        tool_name=tool,
        tool_version="stdlib",
    )


def _cargo_dependencies(
    payload: dict[str, object], text: str
) -> tuple[DependencyRecord, ...]:
    dependencies: list[DependencyRecord] = []
    for section_name, scope in (
        ("dependencies", "runtime"),
        ("dev-dependencies", "development"),
        ("build-dependencies", "build"),
    ):
        section = payload.get(section_name)
        if not isinstance(section, dict):
            continue
        values: list[tuple[str, str | None]] = []
        for dependency_name, spec in section.items():
            version = spec if isinstance(spec, str) else None
            if isinstance(spec, dict) and isinstance(spec.get("version"), str):
                version = str(spec["version"])
            values.append((str(dependency_name), version))
        dependencies.extend(
            DependencyRecord(
                item.name,
                "cargo",
                scope,
                item.version_spec,
                item.source_range,
                item.confirmed,
                item.confidence,
                item.evidence,
            )
            for item in _dependency_records(values, kind="cargo", text=text)
        )
    return tuple(dependencies)


def _cargo_manifest_evidence(
    path: Path, text: str, payload: dict[str, object]
) -> ManifestEvidence:
    package = payload.get("package")
    workspace = payload.get("workspace")
    project_name = None
    if isinstance(package, dict) and isinstance(package.get("name"), str):
        project_name = str(package["name"])
    elif isinstance(workspace, dict):
        project_name = path.parent.name
    hints: tuple[ProjectHint, ...] = ()
    if project_name:
        hints = (
            ProjectHint(
                "rust",
                project_name,
                str(path.parent),
                1.0,
                ("manifest:Cargo.toml",),
                "cargo",
                {"workspace": isinstance(workspace, dict)},
            ),
        )
    return hints, _cargo_dependencies(payload, text), ()


def _python_manifest_evidence(
    path: Path, text: str, payload: dict[str, object]
) -> ManifestEvidence:
    project = payload.get("project")
    tool = payload.get("tool", {})
    project_name = None
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        project_name = str(project["name"])
    elif isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict) and isinstance(poetry.get("name"), str):
            project_name = str(poetry["name"])
    hints: tuple[ProjectHint, ...] = ()
    if project_name:
        hints = (
            ProjectHint(
                "python",
                project_name,
                str(path.parent),
                1.0,
                ("manifest:pyproject.toml",),
                "pyproject",
            ),
        )
    values: list[tuple[str, str | None]] = []
    if isinstance(project, dict) and isinstance(project.get("dependencies"), list):
        for value in project["dependencies"]:
            if not isinstance(value, str):
                continue
            dependency_name = re.split(r"[<>=!~\s\[]", value, maxsplit=1)[0]
            values.append((dependency_name, value[len(dependency_name) :] or None))
    return hints, _dependency_records(values, kind="python", text=text), ()


def _toml_manifest_evidence(path: Path, text: str) -> ManifestEvidence:
    try:
        payload = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        return (), (), (_manifest_parse_error("tomllib", exc),)
    name = path.name.casefold()
    if name == "cargo.toml":
        return _cargo_manifest_evidence(path, text, payload)
    if name == "pyproject.toml":
        return _python_manifest_evidence(path, text, payload)
    return (), (), ()


def _json_manifest_evidence(path: Path, text: str) -> ManifestEvidence:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return (), (), (_manifest_parse_error("json", exc),)
    name = path.name.casefold()
    hints: tuple[ProjectHint, ...] = ()
    if isinstance(payload, dict) and isinstance(payload.get("name"), str):
        ecosystem = "javascript" if name == "package.json" else "php"
        hints = (
            ProjectHint(
                ecosystem,
                str(payload["name"]),
                str(path.parent),
                1.0,
                (f"manifest:{name}",),
                name.removesuffix(".json"),
            ),
        )
    values: list[tuple[str, str | None]] = []
    if isinstance(payload, dict):
        for section_name in (
            "dependencies",
            "devDependencies",
            "require",
            "require-dev",
        ):
            section = payload.get(section_name)
            if isinstance(section, dict):
                values.extend((str(key), str(value)) for key, value in section.items())
    return hints, _dependency_records(values, kind=name, text=text), ()


def _go_manifest_evidence(path: Path, text: str) -> ManifestEvidence:
    match = re.search(r"(?m)^\s*module\s+(\S+)", text)
    hints: tuple[ProjectHint, ...] = ()
    if match:
        hints = (
            ProjectHint(
                "go",
                match.group(1),
                str(path.parent),
                1.0,
                ("manifest:go.mod",),
                "go_mod",
            ),
        )
    values = [
        (item.group(1), item.group(2))
        for item in re.finditer(r"(?m)^\s*([\w./-]+)\s+(v\S+)", text)
        if item.group(1) != "module"
    ]
    return hints, _dependency_records(values, kind="go", text=text), ()


def _maven_manifest_evidence(path: Path, text: str) -> ManifestEvidence:
    artifact = re.search(r"<artifactId>\s*([^<]+)\s*</artifactId>", text)
    hints: tuple[ProjectHint, ...] = ()
    if artifact:
        hints = (
            ProjectHint(
                "java",
                artifact.group(1).strip(),
                str(path.parent),
                0.95,
                ("manifest:pom.xml:artifactId",),
                "maven",
            ),
        )
    return hints, (), ()


def manifest_evidence(
    path: Path,
    text: str,
) -> ManifestEvidence:
    """Parse common manifests with standard-library parsers and bounded output."""

    name = path.name.casefold()
    if name in {"cargo.toml", "pyproject.toml", "pipfile"}:
        return _toml_manifest_evidence(path, text)
    if name in {"package.json", "composer.json"}:
        return _json_manifest_evidence(path, text)
    if name == "go.mod":
        return _go_manifest_evidence(path, text)
    if name == "pom.xml":
        return _maven_manifest_evidence(path, text)
    return (), (), ()


# endregion [03]


__all__ = [
    "SourceMap",
    "comparison_fingerprints",
    "manifest_evidence",
    "searchable_chunks",
]
