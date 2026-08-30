"""Bounded language, artifact and encoding detection for textual files."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import codecs
import io
import re
import token
import tokenize
from pathlib import Path
from typing import Final

from .code_contracts import ArtifactClassification, ArtifactKind


# region [01] Extensible detection tables


DETECTOR_VERSION = "code-artifact-detector-v2"
PROBE_BYTES = 64 * 1024

LANGUAGE_EXTENSIONS: Final[dict[str, str]] = {
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".rs": "rust",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sc": "scala",
    ".lua": "lua",
    ".r": "r",
    ".dart": "dart",
    ".hs": "haskell",
    ".lhs": "haskell",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".fs": "fsharp",
    ".fsx": "fsharp",
    ".vb": "visual_basic",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "fish",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".vue": "vue",
    ".svelte": "svelte",
    ".asm": "assembly",
    ".s": "assembly",
    ".wat": "webassembly_text",
}

CONFIG_EXTENSIONS: Final[dict[str, str]] = {
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".json5": "json5",
    ".xml": "xml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "config",
    ".properties": "properties",
    ".env": "dotenv",
    ".editorconfig": "editorconfig",
}

DATA_EXTENSIONS: Final[dict[str, str]] = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".ndjson": "jsonl",
    ".jsonl": "jsonl",
    ".graphql": "graphql",
    ".proto": "protobuf",
}

DOCUMENT_EXTENSIONS: Final[dict[str, str]] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".rst": "restructuredtext",
    ".adoc": "asciidoc",
    ".txt": "text",
}

TEMPLATE_EXTENSIONS: Final[dict[str, str]] = {
    ".j2": "jinja2",
    ".jinja": "jinja2",
    ".jinja2": "jinja2",
    ".tmpl": "template",
    ".tpl": "template",
    ".mustache": "mustache",
    ".hbs": "handlebars",
    ".ejs": "ejs",
}

EXACT_FILENAMES: Final[dict[str, tuple[str | None, ArtifactKind]]] = {
    "cargo.toml": ("toml", ArtifactKind.MANIFEST),
    "cargo.lock": ("toml", ArtifactKind.LOCK),
    "pyproject.toml": ("toml", ArtifactKind.MANIFEST),
    "setup.py": ("python", ArtifactKind.MANIFEST),
    "setup.cfg": ("ini", ArtifactKind.MANIFEST),
    "requirements.txt": ("requirements", ArtifactKind.MANIFEST),
    "pipfile": ("toml", ArtifactKind.MANIFEST),
    "pipfile.lock": ("json", ArtifactKind.LOCK),
    "poetry.lock": ("toml", ArtifactKind.LOCK),
    "package.json": ("json", ArtifactKind.MANIFEST),
    "package-lock.json": ("json", ArtifactKind.LOCK),
    "pnpm-lock.yaml": ("yaml", ArtifactKind.LOCK),
    "yarn.lock": ("yarn_lock", ArtifactKind.LOCK),
    "go.mod": ("go_mod", ArtifactKind.MANIFEST),
    "go.sum": ("go_sum", ArtifactKind.LOCK),
    "composer.json": ("json", ArtifactKind.MANIFEST),
    "composer.lock": ("json", ArtifactKind.LOCK),
    "gemfile": ("ruby", ArtifactKind.MANIFEST),
    "gemfile.lock": ("bundler_lock", ArtifactKind.LOCK),
    "pom.xml": ("xml", ArtifactKind.MANIFEST),
    "build.gradle": ("groovy", ArtifactKind.MANIFEST),
    "build.gradle.kts": ("kotlin", ArtifactKind.MANIFEST),
    "makefile": ("make", ArtifactKind.SCRIPT),
    "gnumakefile": ("make", ArtifactKind.SCRIPT),
    "cmakelists.txt": ("cmake", ArtifactKind.MANIFEST),
    "dockerfile": ("dockerfile", ArtifactKind.SCRIPT),
    "jenkinsfile": ("groovy", ArtifactKind.SCRIPT),
    "justfile": ("just", ArtifactKind.SCRIPT),
    ".gitignore": ("gitignore", ArtifactKind.CONFIG),
    ".gitattributes": ("gitattributes", ArtifactKind.CONFIG),
    ".dockerignore": ("dockerignore", ArtifactKind.CONFIG),
}

_GENERATED_PATH_PARTS = frozenset(
    {"dist", "build", "target", "out", "generated", "gen", "__generated__"}
)
_VENDORED_PATH_PARTS = frozenset(
    {"vendor", "vendors", "third_party", "third-party", "node_modules", "site-packages"}
)
_FIXTURE_PATH_PARTS = frozenset({"fixture", "fixtures", "testdata", "test_data"})
_EXAMPLE_PATH_PARTS = frozenset(
    {"example", "examples", "demo", "demos", "sample", "samples"}
)
_DOC_PATH_PARTS = frozenset({"doc", "docs", "documentation"})

_GENERATED_DECLARATION = re.compile(
    r"(?ix)^\s*(?:(?:warning|notice|important|caution)\s*[:!-]\s*)?(?:"
    r"<\s*auto[- ]?generated\s*/?>|"
    r"@generated\b|"
    r"(?:auto[- ]?generated|automatically\s+generated|machine[- ]generated)\b|"
    r"generated\s+(?:file|code|by)\b|"
    r"this\s+(?:file|code)\s+(?:is|was)\s+"
    r"(?:auto[- ]?generated|automatically\s+generated|machine[- ]generated|generated)\b|"
    r"this\s+is\s+an?\s+(?:auto[- ]?|automatically\s+|machine[- ])?"
    r"generated\s+(?:file|code)\b|"
    r"(?:file|code)\s+generated\b|"
    r"do\s+not\s+edit\b"
    r")"
)
_HEADER_PROLOGUE = re.compile(r"^<\?xml\b[^>]*\?>$", re.I)
_HEADER_SCAN_CHARS = 8192
_HEADER_SCAN_LINES = 80

_HASH_COMMENT_LANGUAGES = frozenset(
    {
        "asciidoc",
        "config",
        "dockerfile",
        "dotenv",
        "editorconfig",
        "fish",
        "gitattributes",
        "gitignore",
        "make",
        "perl",
        "powershell",
        "properties",
        "python",
        "r",
        "requirements",
        "ruby",
        "shell",
        "toml",
        "yaml",
    }
)
_SLASH_COMMENT_LANGUAGES = frozenset(
    {
        "c",
        "cpp",
        "csharp",
        "dart",
        "go",
        "groovy",
        "java",
        "javascript",
        "json5",
        "kotlin",
        "php",
        "protobuf",
        "rust",
        "scala",
        "swift",
        "typescript",
        "webassembly_text",
    }
)
_C_BLOCK_COMMENT_LANGUAGES = _SLASH_COMMENT_LANGUAGES | frozenset(
    {"css", "ejs", "less", "sass", "scss"}
)
_DASH_COMMENT_LANGUAGES = frozenset({"haskell", "lua", "sql"})
_SEMICOLON_COMMENT_LANGUAGES = frozenset({"assembly", "ini"})
_XML_COMMENT_LANGUAGES = frozenset(
    {
        "asciidoc",
        "html",
        "markdown",
        "restructuredtext",
        "svelte",
        "vue",
        "xml",
    }
)
_LINE_COMMENT_STYLE_GROUPS: Final[tuple[tuple[str, str, frozenset[str]], ...]] = (
    (
        "hash",
        "#",
        _HASH_COMMENT_LANGUAGES | frozenset({"cmake", "elixir", "just", "yarn_lock"}),
    ),
    ("slash", "//", _SLASH_COMMENT_LANGUAGES),
    ("dash", "--", _DASH_COMMENT_LANGUAGES),
    ("semicolon", ";", _SEMICOLON_COMMENT_LANGUAGES),
    ("percent", "%", frozenset({"erlang"})),
    ("apostrophe", "'", frozenset({"visual_basic"})),
    ("rem", "REM", frozenset({"batch", "visual_basic"})),
    ("batch-label", "::", frozenset({"batch"})),
)
_BLOCK_COMMENT_STYLE_GROUPS: Final[tuple[tuple[str, str, str, frozenset[str]], ...]] = (
    ("c-block", "/*", "*/", _C_BLOCK_COMMENT_LANGUAGES),
    ("xml-block", "<!--", "-->", _XML_COMMENT_LANGUAGES),
    ("powershell-block", "<#", "#>", frozenset({"powershell"})),
    ("haskell-block", "{-", "-}", frozenset({"haskell"})),
    ("fsharp-block", "(*", "*)", frozenset({"fsharp"})),
    ("jinja-block", "{#", "#}", frozenset({"jinja2", "template"})),
)
_SHEBANG_LANGUAGE = (
    (re.compile(r"\bpython(?:\d+(?:\.\d+)*)?\b", re.I), "python"),
    (re.compile(r"\b(?:bash|sh|zsh|dash)\b", re.I), "shell"),
    (re.compile(r"\bpwsh\b|powershell", re.I), "powershell"),
    (re.compile(r"\bruby\b", re.I), "ruby"),
    (re.compile(r"\bnode\b|deno", re.I), "javascript"),
    (re.compile(r"\bperl\b", re.I), "perl"),
    (re.compile(r"\bphp\b", re.I), "php"),
)
_PYTHON_CODING = re.compile(rb"coding[=:]\s*([-\w.]+)")
_EXTENSION_ROLES: Final[
    tuple[tuple[dict[str, str], ArtifactKind, float, str | None], ...]
] = (
    (LANGUAGE_EXTENSIONS, ArtifactKind.SOURCE, 0.9, None),
    (CONFIG_EXTENSIONS, ArtifactKind.CONFIG, 0.88, "config"),
    (DATA_EXTENSIONS, ArtifactKind.DATA, 0.85, "data"),
    (DOCUMENT_EXTENSIONS, ArtifactKind.DOCUMENTATION, 0.82, "documentation"),
    (TEMPLATE_EXTENSIONS, ArtifactKind.TEMPLATE, 0.82, "template"),
)


# endregion [01]


# region [02] Bounded binary and encoding handling


def likely_code_candidate(path: str | Path) -> bool:
    """Select plausible textual artifacts without opening obvious binaries."""

    candidate = Path(path)
    name = candidate.name.casefold()
    if name in EXACT_FILENAMES:
        return True
    suffixes = tuple(value.casefold() for value in candidate.suffixes)
    if any(
        suffix in LANGUAGE_EXTENSIONS
        or suffix in CONFIG_EXTENSIONS
        or suffix in DATA_EXTENSIONS
        or suffix in DOCUMENT_EXTENSIONS
        or suffix in TEMPLATE_EXTENSIONS
        for suffix in suffixes
    ):
        return True
    if name.endswith((".lock", ".in", ".example", ".sample")):
        return True
    return not candidate.suffix


def looks_binary(raw: bytes) -> bool:
    """Reject binary payloads while allowing BOM-marked UTF-16/UTF-32 text."""

    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return False
    sample = raw[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        even_nuls = sample[0::2].count(0)
        odd_nuls = sample[1::2].count(0)
        paired_slots = max(1, len(sample) // 2)
        if max(even_nuls, odd_nuls) / paired_slots < 0.3:
            return True
    controls = sum(
        value < 32 and value not in {8, 9, 10, 12, 13, 27} for value in sample
    )
    return controls / len(sample) > 0.05


def _declared_python_encoding(raw: bytes) -> str | None:
    for line in raw.splitlines()[:2]:
        match = _PYTHON_CODING.search(line)
        if match is None:
            continue
        try:
            return codecs.lookup(match.group(1).decode("ascii")).name
        except (LookupError, UnicodeDecodeError):
            return None
    return None


def decode_text(raw: bytes, path: str | Path) -> tuple[str, str, tuple[str, ...]]:
    """Decode one bounded payload deterministically and retain the decision evidence."""

    if looks_binary(raw):
        raise UnicodeError("payload contains binary control bytes")
    evidence: list[str] = []
    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    )
    for bom, encoding in bom_encodings:
        if raw.startswith(bom):
            evidence.append(f"encoding:bom:{encoding}")
            return raw.decode(encoding, "strict"), encoding, tuple(evidence)

    if Path(path).suffix.casefold() in {".py", ".pyi", ".pyw"}:
        declared = _declared_python_encoding(raw)
        if declared is not None:
            evidence.append(f"encoding:pep263:{declared}")
            return raw.decode(declared, "strict"), declared, tuple(evidence)

    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        if raw and len(raw) % 2 == 0:
            even_nuls = raw[:8192:2].count(0)
            odd_nuls = raw[1:8192:2].count(0)
            slots = max(1, len(raw[:8192]) // 2)
            if odd_nuls / slots > 0.3 or even_nuls / slots > 0.3:
                encoding = "utf-16-le" if odd_nuls > even_nuls else "utf-16-be"
                evidence.append(f"encoding:structure:{encoding}")
                return raw.decode(encoding, "strict"), encoding, tuple(evidence)
        evidence.append("encoding:fallback:cp1252")
        return raw.decode("cp1252", "strict"), "cp1252", tuple(evidence)
    evidence.append("encoding:utf-8-strict")
    return text, "utf-8", tuple(evidence)


# endregion [02]


# region [03] Language and artifact classification


def _path_role(path: Path) -> tuple[ArtifactKind | None, tuple[str, ...]]:
    parts = tuple(part.casefold() for part in path.parts)
    part_set = frozenset(parts)
    if part_set & _VENDORED_PATH_PARTS:
        matched = sorted(part_set & _VENDORED_PATH_PARTS)[0]
        return ArtifactKind.VENDORED, (f"path:vendored:{matched}",)
    if part_set & _GENERATED_PATH_PARTS:
        matched = sorted(part_set & _GENERATED_PATH_PARTS)[0]
        return ArtifactKind.GENERATED, (f"path:generated:{matched}",)
    if part_set & _FIXTURE_PATH_PARTS:
        return ArtifactKind.FIXTURE, ("path:fixture",)
    if part_set & _EXAMPLE_PATH_PARTS:
        return ArtifactKind.EXAMPLE, ("path:example",)
    if part_set & _DOC_PATH_PARTS:
        return ArtifactKind.DOCUMENTATION, ("path:documentation",)
    return None, ()


def _comment_styles(
    language: str | None,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str, str], ...]]:
    line_styles = tuple(
        (style, prefix)
        for style, prefix, languages in _LINE_COMMENT_STYLE_GROUPS
        if language in languages
    )
    block_styles = tuple(
        (style, opener, closer)
        for style, opener, closer, languages in _BLOCK_COMMENT_STYLE_GROUPS
        if language in languages
    )
    return line_styles, block_styles


def _line_comment_body(
    line: str, styles: tuple[tuple[str, str], ...]
) -> tuple[str, str] | None:
    stripped = line.lstrip()
    for style, prefix in styles:
        if prefix == "REM":
            body = stripped[3:] if stripped[:3].casefold() == "rem" else ""
            if stripped[:3].casefold() != "rem" or (body and not body[0].isspace()):
                continue
            return style, body
        if stripped.startswith(prefix):
            return style, stripped[len(prefix) :]
    return None


def _block_comment_style(
    line: str, styles: tuple[tuple[str, str, str], ...]
) -> tuple[str, str, str] | None:
    stripped = line.lstrip()
    for style, opener, closer in styles:
        if stripped.startswith(opener):
            return style, opener, closer
    return None


def _generated_marker_evidence(
    body: str, *, style: str, line_number: int
) -> tuple[str, ...]:
    normalized = body.lstrip()
    if style in {"c-block", "fsharp-block"}:
        normalized = normalized.removeprefix("*").lstrip()
    if _GENERATED_DECLARATION.match(normalized) is None:
        return ()
    return (
        "header:generated-marker",
        f"header:generated-marker:{style}:line-{line_number}",
        f"detector:{DETECTOR_VERSION}",
    )


def _scan_block_comment(
    lines: list[str],
    start_index: int,
    style: str,
    opener: str,
    closer: str,
) -> tuple[tuple[str, ...], int, bool]:
    index = start_index
    pending_evidence: tuple[str, ...] = ()
    while index < len(lines):
        body = lines[index].lstrip()
        if index == start_index:
            body = body[len(opener) :]
        comment_body, separator, tail = body.partition(closer)
        if not pending_evidence:
            pending_evidence = _generated_marker_evidence(
                comment_body,
                style=style,
                line_number=index + 1,
            )
        index += 1
        if separator:
            clean_close = not tail.strip()
            return pending_evidence if clean_close else (), index, clean_close
    return (), index, False


def _generated_header_evidence(text: str, language: str | None) -> tuple[str, ...]:
    line_styles, block_styles = _comment_styles(language)
    if not line_styles and not block_styles:
        return ()
    bounded = text[:_HEADER_SCAN_CHARS].removeprefix("\ufeff")
    lines = bounded.splitlines()[:_HEADER_SCAN_LINES]
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or (index == 0 and stripped.startswith("#!")):
            index += 1
            continue
        if _HEADER_PROLOGUE.fullmatch(stripped) is not None:
            index += 1
            continue
        line_comment = _line_comment_body(lines[index], line_styles)
        if line_comment is not None:
            style, body = line_comment
            evidence = _generated_marker_evidence(
                body,
                style=style,
                line_number=index + 1,
            )
            if evidence:
                return evidence
            index += 1
            continue
        block_comment = _block_comment_style(lines[index], block_styles)
        if block_comment is None:
            return ()
        style, opener, closer = block_comment
        evidence, index, clean_close = _scan_block_comment(
            lines,
            index,
            style,
            opener,
            closer,
        )
        if evidence:
            return evidence
        if not clean_close:
            return ()
    return ()


def _base_classification(
    candidate: Path,
) -> tuple[str | None, ArtifactKind, float, tuple[str, ...]]:
    name = candidate.name.casefold()
    exact = EXACT_FILENAMES.get(name)
    if exact is not None:
        return exact[0], exact[1], 0.99, (f"filename:{name}",)

    suffix = candidate.suffix.casefold()
    for extension_map, kind, confidence, evidence_role in _EXTENSION_ROLES:
        language = extension_map.get(suffix)
        if language is not None:
            role = language if evidence_role is None else evidence_role
            return language, kind, confidence, (f"extension:{suffix}:{role}",)
    if name.endswith((".in", ".tmpl")):
        return (
            "template",
            ArtifactKind.TEMPLATE,
            0.82,
            (f"extension:{suffix or '<none>'}:template",),
        )
    if name.endswith(".lock"):
        return "text", ArtifactKind.LOCK, 0.8, ("filename-suffix:lock",)
    return None, ArtifactKind.PLAIN_TEXT, 0.45, ()


def _content_language(text: str) -> tuple[str | None, str | None]:
    prefix = text[:16_384]
    first_line = prefix.splitlines()[0] if prefix.splitlines() else ""
    if first_line.startswith("#!"):
        for pattern, language in _SHEBANG_LANGUAGE:
            if pattern.search(first_line):
                return language, f"shebang:{language}"
    if re.search(r"(?m)^\s*(?:async\s+)?def\s+\w+\s*\(|^\s*class\s+\w+", prefix):
        return "python", "content:python-definitions"
    if re.search(
        r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:fn|struct|enum|trait|impl)\b", prefix
    ):
        return "rust", "content:rust-items"
    if re.search(
        r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+\w+|\bconst\s+\w+\s*=\s*\([^)]*\)\s*=>",
        prefix,
    ):
        return "javascript", "content:javascript-functions"
    if re.search(
        r"(?m)^\s*package\s+\w+\s*$|^\s*func\s+(?:\([^)]*\)\s*)?\w+\s*\(", prefix
    ):
        return "go", "content:go-declarations"
    if re.search(r"(?m)^\s*(?:public\s+)?(?:class|interface|enum)\s+\w+", prefix):
        return "java", "content:jvm-declarations"
    if re.search(r"(?m)^\s*param\s*\(|^\s*function\s+[\w-]+", prefix, re.I):
        return "powershell", "content:powershell-functions"
    return None, None


def _refine_from_content(
    text: str,
    language: str | None,
    kind: ArtifactKind,
    confidence: float,
) -> tuple[str | None, ArtifactKind, float, tuple[str, ...]]:
    content_language, content_evidence = _content_language(text)
    if content_language is None:
        return language, kind, confidence, ()
    if language is None or language in {"text", "config"}:
        language = content_language
        kind = ArtifactKind.SCRIPT if text.startswith("#!") else ArtifactKind.SOURCE
        confidence = max(confidence, 0.86)
    return (
        language,
        kind,
        confidence,
        () if content_evidence is None else (content_evidence,),
    )


def classify_artifact(path: str | Path, text: str) -> ArtifactClassification:
    """Combine filename, extension, shebang, path and bounded content evidence."""

    candidate = Path(path)
    language, kind, confidence, base_evidence = _base_classification(candidate)
    evidence = list(base_evidence)
    language, kind, confidence, content_evidence = _refine_from_content(
        text,
        language,
        kind,
        confidence,
    )
    evidence.extend(content_evidence)

    role, role_evidence = _path_role(candidate)
    generated_header_evidence = _generated_header_evidence(text, language)
    if generated_header_evidence:
        role = ArtifactKind.GENERATED
        role_evidence = (*role_evidence, *generated_header_evidence)
    if role is not None:
        kind = role
        confidence = max(confidence, 0.9)
        evidence.extend(role_evidence)

    if not evidence:
        evidence.append("content:textual-unclassified")
    if kind is ArtifactKind.DOCUMENTATION and "```" in text[:65_536]:
        evidence.append("content:fenced-code")

    return ArtifactClassification(
        language=language,
        artifact_kind=kind,
        confidence=confidence,
        evidence=tuple(dict.fromkeys(evidence)),
        generated=kind is ArtifactKind.GENERATED,
        vendored=kind is ArtifactKind.VENDORED,
    )


# endregion [03]


# region [04] Non-destructive normalized/token fingerprints


_GENERIC_TOKEN = re.compile(
    r"(?:[A-Za-z_$][\w$]*|0[xX][0-9A-Fa-f]+|\d+(?:\.\d+)?|"
    r"==|!=|<=|>=|::|->|=>|&&|\|\||\S)"
)


def normalized_tokens(text: str, language: str | None) -> tuple[str, ...]:
    """Return an explicit comparison-only token stream; never an identity hash."""

    if language == "python":
        try:
            stream = io.StringIO(text).readline
            ignored = {
                token.ENCODING,
                token.ENDMARKER,
                token.INDENT,
                token.DEDENT,
                token.NEWLINE,
                tokenize.NL,
                token.COMMENT,
            }
            return tuple(
                f"{item.type}:{item.string}"
                for item in tokenize.generate_tokens(stream)
                if item.type not in ignored
            )
        except (IndentationError, SyntaxError, tokenize.TokenError):
            pass
    without_line_comments = re.sub(r"(?m)^\s*(?://|#).*$", "", text)
    without_block_comments = re.sub(r"/\*.*?\*/", "", without_line_comments, flags=re.S)
    return tuple(_GENERIC_TOKEN.findall(without_block_comments))


__all__ = [
    "DETECTOR_VERSION",
    "PROBE_BYTES",
    "classify_artifact",
    "decode_text",
    "likely_code_candidate",
    "looks_binary",
    "normalized_tokens",
]


# endregion [04]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_detection")
