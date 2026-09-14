"""Bounded language, artifact and encoding detection for textual files."""

from __future__ import annotations
import codecs
import io
import re
import token
import tokenize
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from ..code_contracts import (
    ArtifactClassification,
    ArtifactKind,
    ThirdPartyClassification,
    ThirdPartyKind,
)


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

# These are deliberately exact path components.  A filename containing one of
# these words is not enough to classify it as foreign; a project may quite
# legitimately have ``dependencies.py`` or ``build_notes.md``.
_DEPENDENCY_PATH_PARTS = frozenset(
    {
        ".cargo",
        ".gradle",
        ".venv",
        "bower_components",
        "carthage",
        "dist-packages",
        "go.mod.cache",
        "jspm_packages",
        "node_modules",
        "packagecache",
        "pods",
        "site-packages",
        "thirdparty",
        "third-party",
        "third_party",
        "vendor",
        "vendors",
        "venv",
    }
)
_VENDORED_DIRECTORY_PARTS = frozenset(
    {"thirdparty", "third-party", "third_party", "vendor", "vendors"}
)
_BUILD_PATH_PARTS = frozenset(
    {
        ".next",
        ".nuxt",
        ".parcel-cache",
        ".turbo",
        "bin",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "obj",
        "out",
        "target",
        "wheelhouse",
    }
)
_GENERATED_PATH_PARTS_EXTENDED = frozenset(
    {"__generated__", "codegen", "generated", "gen"}
)
_CACHE_PATH_PARTS = frozenset(
    {
        ".cache",
        ".gradle",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
    }
)
_BINARY_SUFFIXES = frozenset(
    {
        ".a",
        ".aar",
        ".appimage",
        ".class",
        ".dll",
        ".dylib",
        ".ear",
        ".exe",
        ".jar",
        ".lib",
        ".msi",
        ".o",
        ".obj",
        ".pdb",
        ".pyc",
        ".pyo",
        ".so",
        ".war",
        ".wasm",
    }
)
_GENERATED_SUFFIXES = frozenset({".map"})
_BINARY_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\x7fELF", "elf"),
    (b"MZ", "pe"),
    (b"\xca\xfe\xba\xbe", "mach-o-or-class"),
    (b"\xfe\xed\xfa\xce", "mach-o"),
    (b"\xce\xfa\xed\xfe", "mach-o"),
    (b"\xcf\xfa\xed\xfe", "mach-o"),
    (b"\xfe\xed\xfa\xcf", "mach-o"),
    (b"\x00asm", "wasm"),
)
THIRD_PARTY_DETECTOR_VERSION = "code-third-party-detector-v1"

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


# region [04] Third-party/build/binary origin signals


def _path_parts_for_origin(path: str | Path) -> tuple[str, ...]:
    """Return normalized path components without resolving or reading a path."""

    return tuple(part.casefold() for part in Path(path).parts if part not in {"", "."})


def _project_root_for_path(
    path: str | Path,
    roots: Iterable[str | Path],
) -> Path | None:
    """Return the nearest explicit project root using lexical paths only."""

    candidate = Path(path).absolute()
    selected: Path | None = None
    for root in roots:
        root_path = Path(root).absolute()
        try:
            candidate.relative_to(root_path)
        except ValueError:
            continue
        if selected is None or len(root_path.parts) > len(selected.parts):
            selected = root_path
    return selected


def _binary_magic_evidence(raw: bytes) -> tuple[str, ...]:
    for magic, label in _BINARY_MAGIC:
        if raw.startswith(magic):
            return (f"bytes:binary-magic:{label}",)
    return ()


def _origin_text_probe(
    candidate: Path,
    raw: bytes | None,
    text: str | None,
) -> str | None:
    """Obtain only the bounded header used for generated-marker detection."""

    if text is not None:
        return text[:_HEADER_SCAN_CHARS]
    if raw is None or not raw or looks_binary(raw):
        return None
    try:
        decoded, _encoding, _evidence = decode_text(
            raw[:PROBE_BYTES],
            candidate,
        )
    except (UnicodeError, ValueError):
        return None
    return decoded[:_HEADER_SCAN_CHARS]


def classify_third_party_artifact(
    path: str | Path,
    raw: bytes | None = None,
    *,
    text: str | None = None,
    artifact_classification: ArtifactClassification | None = None,
    analysis_status: str | None = None,
    project_roots: Iterable[str | Path] = (),
) -> ThirdPartyClassification:
    """Classify foreign/build/binary signals without asserting ownership.

    The function is deliberately path-first and bounded.  It consumes only
    the observed path, optional bytes already read by Code, optional decoded
    text, and optional route metadata.  It never opens a file, resolves a
    symlink, consults a package index, contacts a provider, or mutates the
    corpus.

    Exact dependency/install path components are stronger than a filename
    substring.  Generated/build/cache paths and binary signatures are retained
    as separate signals because a generated or binary file can still be
    project-owned.  A clean source file is called ``PROJECT_CODE`` only when
    it lies under an explicit project root; otherwise the result is
    ``UNKNOWN``.  Neither result is an authorship or license determination.
    """

    candidate = Path(path)
    candidate_absolute = candidate.absolute()
    project_root = _project_root_for_path(candidate_absolute, project_roots)
    parts = _path_parts_for_origin(
        candidate_absolute.relative_to(project_root)
        if project_root is not None
        else candidate_absolute
    )
    suffixes = tuple(suffix.casefold() for suffix in candidate.suffixes)
    evidence: list[str] = [f"detector:{THIRD_PARTY_DETECTOR_VERSION}"]
    observed: set[ThirdPartyKind] = set()
    path_dependency = next(
        (part for part in parts if part in _DEPENDENCY_PATH_PARTS),
        None,
    )
    if path_dependency is not None:
        kind = (
            ThirdPartyKind.VENDORED
            if path_dependency in _VENDORED_DIRECTORY_PARTS
            else ThirdPartyKind.DEPENDENCY
        )
        observed.add(kind)
        evidence.append(f"path:{kind.value}:{path_dependency}")

    path_build = next((part for part in parts if part in _BUILD_PATH_PARTS), None)
    if path_build is not None:
        observed.add(ThirdPartyKind.BUILD_ARTIFACT)
        evidence.append(f"path:build-artifact:{path_build}")

    path_generated = next(
        (part for part in parts if part in _GENERATED_PATH_PARTS_EXTENDED),
        None,
    )
    if path_generated is not None:
        observed.add(ThirdPartyKind.GENERATED)
        evidence.append(f"path:generated:{path_generated}")

    path_cache = next((part for part in parts if part in _CACHE_PATH_PARTS), None)
    if path_cache is not None:
        observed.add(ThirdPartyKind.CACHE)
        evidence.append(f"path:cache:{path_cache}")

    if suffixes and suffixes[-1] in _GENERATED_SUFFIXES:
        observed.add(ThirdPartyKind.GENERATED)
        evidence.append(f"suffix:generated:{suffixes[-1]}")
    stem = candidate.stem.casefold()
    if stem.endswith((".generated", ".gen", "_generated", "-generated")):
        observed.add(ThirdPartyKind.GENERATED)
        evidence.append("filename:generated-suffix")

    if artifact_classification is None and text is not None:
        artifact_classification = classify_artifact(candidate, text)
    if artifact_classification is not None:
        if artifact_classification.vendored or (
            artifact_classification.artifact_kind is ArtifactKind.VENDORED
        ):
            observed.add(ThirdPartyKind.VENDORED)
            evidence.append("metadata:artifact-kind:vendored")
        if artifact_classification.generated or (
            artifact_classification.artifact_kind is ArtifactKind.GENERATED
        ):
            observed.add(ThirdPartyKind.GENERATED)
            evidence.append("metadata:artifact-kind:generated")

    probe_text = _origin_text_probe(candidate, raw, text)
    if probe_text:
        generated_evidence = _generated_header_evidence(
            probe_text,
            artifact_classification.language if artifact_classification is not None else None,
        )
        if generated_evidence:
            observed.add(ThirdPartyKind.GENERATED)
            evidence.extend(f"metadata:{item}" for item in generated_evidence)

    binary_evidence: tuple[str, ...] = ()
    binary_strong = False
    if raw is not None:
        binary_evidence = _binary_magic_evidence(raw)
        binary_strong = bool(binary_evidence)
        if looks_binary(raw):
            binary_evidence = (*binary_evidence, "bytes:binary-control-probe")
    if binary_evidence:
        observed.add(ThirdPartyKind.BINARY)
        evidence.extend(binary_evidence)
    if suffixes and any(suffix in _BINARY_SUFFIXES for suffix in suffixes):
        observed.add(ThirdPartyKind.BINARY)
        binary_strong = True
        matched_suffix = next(suffix for suffix in suffixes if suffix in _BINARY_SUFFIXES)
        if f"suffix:binary:{matched_suffix}" not in evidence:
            evidence.append(f"suffix:binary:{matched_suffix}")
    if analysis_status is not None and analysis_status.casefold() == "binary":
        observed.add(ThirdPartyKind.BINARY)
        evidence.append("metadata:analysis-status:binary")

    # A binary inside an explicitly owned project is not evidence that it is
    # third-party. Keep the binary fact as a secondary signal so the Code route
    # can avoid parsing it, but do not admit it to the default cleanup policy.
    only_binary_in_owned_scope = bool(
        project_root is not None and observed and observed <= {ThirdPartyKind.BINARY}
    )
    if only_binary_in_owned_scope:
        primary = ThirdPartyKind.PROJECT_CODE
        confidence = 0.60
        evidence.append("scope:explicit-project-root")
    elif observed:
        precedence = (
            ThirdPartyKind.VENDORED,
            ThirdPartyKind.DEPENDENCY,
            ThirdPartyKind.CACHE,
            ThirdPartyKind.BUILD_ARTIFACT,
            ThirdPartyKind.GENERATED,
            ThirdPartyKind.BINARY,
        )
        primary = next(item for item in precedence if item in observed)
        if primary in {ThirdPartyKind.VENDORED, ThirdPartyKind.DEPENDENCY}:
            confidence = 0.98
        elif primary is ThirdPartyKind.BINARY and not binary_strong:
            # A generic control-byte probe or a cached ``binary`` status is
            # useful evidence but can also describe a PDF/image payload.  It
            # must stay below the default trash-policy threshold.
            confidence = 0.70
        else:
            confidence = 0.95
    elif project_root is not None:
        primary = ThirdPartyKind.PROJECT_CODE
        confidence = 0.60
        evidence.append("scope:explicit-project-root")
    else:
        primary = ThirdPartyKind.UNKNOWN
        confidence = 0.0
        evidence.append("scope:ownership-unverified")

    ordered_signals = tuple(
        item
        for item in (
            ThirdPartyKind.VENDORED,
            ThirdPartyKind.DEPENDENCY,
            ThirdPartyKind.GENERATED,
            ThirdPartyKind.BUILD_ARTIFACT,
            ThirdPartyKind.CACHE,
            ThirdPartyKind.BINARY,
        )
        if item in observed and item is not primary
    )
    return ThirdPartyClassification(
        kind=primary,
        confidence=confidence,
        evidence=tuple(dict.fromkeys(evidence)),
        signals=ordered_signals,
    )


def classify_code_provenance(
    path: str | Path,
    raw: bytes | None = None,
    *,
    text: str | None = None,
    artifact_classification: ArtifactClassification | None = None,
    analysis_status: str | None = None,
    project_roots: Iterable[str | Path] = (),
) -> ThirdPartyClassification:
    """Readable alias for :func:`classify_third_party_artifact`."""

    return classify_third_party_artifact(
        path,
        raw,
        text=text,
        artifact_classification=artifact_classification,
        analysis_status=analysis_status,
        project_roots=project_roots,
    )


# endregion [04]


# region [05] Non-destructive normalized/token fingerprints


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
    "THIRD_PARTY_DETECTOR_VERSION",
    "classify_artifact",
    "classify_code_provenance",
    "classify_third_party_artifact",
    "decode_text",
    "likely_code_candidate",
    "looks_binary",
    "normalized_tokens",
]


# endregion [05]
