"""Compatibility contract for the flat structured-code CLI surface."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_code_surface.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import argparse

import pytest

from _04_Nucleo_Operativo.cli_parser import build_parser, decimal_megabytes
from _04_Nucleo_Operativo.cli_validation import validate_arguments
# endregion [01]

# region [02] Implementación


CODE_GROUP_TITLE = "Structured source-code intelligence route"
SEMANTIC_GROUP_TITLE = "Multimodal semantic index"
KNOWLEDGE_GROUP_TITLE = "Read-only Knowledge Plane"


def _expected_store(
    option: str,
    destination: str,
    *,
    default: object = None,
    type_name: str | None = None,
    choices: tuple[str, ...] | None = None,
    metavar: str | None = None,
    help_text: str | None = None,
    action_name: str = "_StoreAction",
) -> tuple[object, ...]:
    return (
        (option,),
        destination,
        action_name,
        None,
        None,
        default,
        type_name,
        choices,
        metavar,
        False,
        help_text,
    )


def _expected_flag(
    option: str,
    destination: str,
    help_text: str | None = None,
) -> tuple[object, ...]:
    return (
        (option,),
        destination,
        "_StoreTrueAction",
        0,
        True,
        False,
        None,
        None,
        None,
        False,
        help_text,
    )


def _expected_boolean_optional(
    option: str,
    negative_option: str,
    destination: str,
    help_text: str,
    *,
    default: bool = True,
) -> tuple[object, ...]:
    return (
        (option, negative_option),
        destination,
        "BooleanOptionalAction",
        0,
        None,
        default,
        None,
        None,
        None,
        False,
        help_text,
    )


EXPECTED_CODE_ACTIONS = (
    _expected_store(
        "--code-max-mb",
        "code_max_file_bytes",
        default=8 * 1024 * 1024,
        type_name="decimal_megabytes",
        metavar="MB",
        help_text="analyze only textual artifacts at or below this bounded size",
    ),
    _expected_store(
        "--code-max-count",
        "code_max_documents",
        type_name="int",
        metavar="N",
    ),
    _expected_store(
        "--code-max-text-chars",
        "code_max_text_chars",
        default=4_000_000,
        type_name="int",
    ),
    _expected_store(
        "--code-chunk-chars",
        "code_chunk_chars",
        default=12_000,
        type_name="int",
    ),
    _expected_store(
        "--code-complexity-warning",
        "code_complexity_warning",
        default=15,
        type_name="int",
    ),
    _expected_store(
        "--code-function-lines-warning",
        "code_function_lines_warning",
        default=200,
        type_name="int",
    ),
    _expected_store(
        "--code-cache-validation",
        "code_cache_validation",
        default="metadata",
        choices=("metadata", "full"),
        help_text=("metadata is incremental-fast; full rechecks exact bytes before reuse"),
    ),
    _expected_store(
        "--code-project-root",
        "code_project_root",
        type_name="Path",
        metavar="PATH",
        help_text="replace default Code project allowlist; repeat for each owned project",
        action_name="_AppendAction",
    ),
    _expected_store(
        "--code-scope",
        "code_candidate_scope",
        default="projects",
        choices=("projects", "broad"),
        help_text=(
            "projects admits only explicit project roots; broad restores the "
            "legacy profile-wide textual selection"
        ),
    ),
    _expected_boolean_optional(
        "--code-generated",
        "--no-code-generated",
        "code_include_generated",
        "retain generated artifacts in structural analysis",
        default=False,
    ),
    _expected_boolean_optional(
        "--code-vendored",
        "--no-code-vendored",
        "code_include_vendored",
        "retain vendored artifacts in structural analysis",
        default=False,
    ),
    _expected_flag(
        "--retry-code-errors",
        "retry_code_errors",
        "retry unchanged partial or failed code observations",
    ),
    _expected_flag(
        "--code-status",
        "code_status",
        "show bounded code database, analyzer and index status",
    ),
    _expected_flag(
        "--code-review",
        "code_review",
        (
            "show deterministic read-only structural observations, open questions "
            "and the underlying hotspot ranking"
        ),
    ),
    _expected_store(
        "--code-review-limit",
        "code_review_limit",
        default=10,
        type_name="int",
        metavar="N",
        help_text=(
            "bound each review surface to 1 to 50 observations; values above 10 require --code-json"
        ),
    ),
    _expected_store(
        "--code-publication-diff",
        "code_publication_diff",
        metavar="BASELINE_STATE",
        help_text=(
            "compare the current completed Code publication with a baseline "
            "state without writing either owner"
        ),
    ),
    _expected_store(
        "--code-search",
        "code_search",
        metavar="QUERY",
    ),
    _expected_store(
        "--code-search-mode",
        "code_search_mode",
        choices=(
            "literal",
            "fts",
            "path",
            "language",
            "symbol",
            "definition",
            "reference",
            "import",
            "dependency",
            "call",
            "signature",
            "diagnostic",
            "complexity",
            "semantic",
            "hybrid",
        ),
        help_text="repeat to fuse complementary exact and structural search modes",
        action_name="_AppendAction",
    ),
    _expected_store(
        "--code-search-limit",
        "code_search_limit",
        default=20,
        type_name="int",
        metavar="N",
    ),
    _expected_store("--code-path", "code_path", metavar="FRAGMENT"),
    _expected_store("--code-language", "code_language"),
    _expected_store("--code-project", "code_project"),
    _expected_store("--code-symbol", "code_symbol"),
    _expected_store("--code-diagnostic", "code_diagnostic"),
    _expected_store(
        "--code-min-complexity",
        "code_min_complexity",
        type_name="float",
    ),
    _expected_flag(
        "--code-projects",
        "code_projects",
        "list inferred project instances without touching source files",
    ),
    _expected_store(
        "--code-reconstruct",
        "code_reconstruct",
        metavar="PROJECT_OR_ID",
    ),
    _expected_store(
        "--code-reconstruct-strategy",
        "code_reconstruct_strategy",
        default="coherent",
        choices=("latest", "coherent", "branches"),
    ),
    _expected_flag(
        "--code-doctor",
        "code_doctor",
        "verify code schema, FTS and analyzer availability without analysis",
    ),
    _expected_store(
        "--code-query",
        "code_query",
        choices=("status", "review", "diff"),
        help_text="query published Code analysis through one bounded read-only surface",
    ),
    *(
        _expected_store(
            f"--code-query-{option}",
            f"code_query_{option.replace('-', '_')}",
            metavar="VALUE",
            help_text="repeat to add an exact Code analysis query filter",
            action_name="_AppendAction",
        )
        for option in (
            "provider",
            "category",
            "module",
            "status",
            "delta",
            "work-package",
        )
    ),
    _expected_store(
        "--code-query-limit",
        "code_query_limit",
        default=50,
        type_name="int",
        metavar="N",
        help_text="return between 1 and 500 bounded query rows",
    ),
    _expected_store(
        "--code-query-baseline",
        "code_query_baseline",
        metavar="BASELINE_STATE",
        help_text="baseline state required only by --code-query diff",
    ),
    _expected_flag("--code-json", "code_json"),
)

EXPECTED_CODE_HELP = (
    "Structured source-code intelligence route:\n"
    "  --code-max-mb MB      analyze only textual artifacts at or below this\n"
    "                        bounded size\n"
    "  --code-max-count N\n"
    "  --code-max-text-chars CODE_MAX_TEXT_CHARS\n"
    "  --code-chunk-chars CODE_CHUNK_CHARS\n"
    "  --code-complexity-warning CODE_COMPLEXITY_WARNING\n"
    "  --code-function-lines-warning CODE_FUNCTION_LINES_WARNING\n"
    "  --code-cache-validation {metadata,full}\n"
    "                        metadata is incremental-fast; full rechecks exact\n"
    "                        bytes before reuse\n"
    "  --code-project-root PATH\n"
    "                        replace default Code project allowlist; repeat for\n"
    "                        each owned project\n"
    "  --code-scope {projects,broad}\n"
    "                        projects admits only explicit project roots; broad\n"
    "                        restores the legacy profile-wide textual selection\n"
    "  --code-generated, --no-code-generated\n"
    "                        retain generated artifacts in structural analysis\n"
    "  --code-vendored, --no-code-vendored\n"
    "                        retain vendored artifacts in structural analysis\n"
    "  --retry-code-errors   retry unchanged partial or failed code observations\n"
    "  --code-status         show bounded code database, analyzer and index status\n"
    "  --code-review         show deterministic read-only structural observations,\n"
    "                        open questions and the underlying hotspot ranking\n"
    "  --code-review-limit N\n"
    "                        bound each review surface to 1 to 50 observations;\n"
    "                        values above 10 require --code-json\n"
    "  --code-publication-diff BASELINE_STATE\n"
    "                        compare the current completed Code publication with a\n"
    "                        baseline state without writing either owner\n"
    "  --code-search QUERY\n"
    "  --code-search-mode "
    "{literal,fts,path,language,symbol,definition,reference,import,dependency,call,"
    "signature,diagnostic,complexity,semantic,hybrid}\n"
    "                        repeat to fuse complementary exact and structural\n"
    "                        search modes\n"
    "  --code-search-limit N\n"
    "  --code-path FRAGMENT\n"
    "  --code-language CODE_LANGUAGE\n"
    "  --code-project CODE_PROJECT\n"
    "  --code-symbol CODE_SYMBOL\n"
    "  --code-diagnostic CODE_DIAGNOSTIC\n"
    "  --code-min-complexity CODE_MIN_COMPLEXITY\n"
    "  --code-projects       list inferred project instances without touching\n"
    "                        source files\n"
    "  --code-reconstruct PROJECT_OR_ID\n"
    "  --code-reconstruct-strategy {latest,coherent,branches}\n"
    "  --code-doctor         verify code schema, FTS and analyzer availability\n"
    "                        without analysis\n"
    "  --code-query {status,review,diff}\n"
    "                        query published Code analysis through one bounded\n"
    "                        read-only surface\n"
    "  --code-query-provider VALUE\n"
    "                        repeat to add an exact Code analysis query filter\n"
    "  --code-query-category VALUE\n"
    "                        repeat to add an exact Code analysis query filter\n"
    "  --code-query-module VALUE\n"
    "                        repeat to add an exact Code analysis query filter\n"
    "  --code-query-status VALUE\n"
    "                        repeat to add an exact Code analysis query filter\n"
    "  --code-query-delta VALUE\n"
    "                        repeat to add an exact Code analysis query filter\n"
    "  --code-query-work-package VALUE\n"
    "                        repeat to add an exact Code analysis query filter\n"
    "  --code-query-limit N  return between 1 and 500 bounded query rows\n"
    "  --code-query-baseline BASELINE_STATE\n"
    "                        baseline state required only by --code-query diff\n"
    "  --code-json\n"
    "\n"
)


def _normalized_action(action: argparse.Action) -> tuple[object, ...]:
    choices = None if action.choices is None else tuple(action.choices)
    type_name = None if action.type is None else action.type.__name__
    return (
        tuple(action.option_strings),
        action.dest,
        type(action).__name__,
        action.nargs,
        action.const,
        action.default,
        type_name,
        choices,
        action.metavar,
        action.required,
        action.help,
    )


def test_code_actions_aliases_and_help_preserve_the_normalized_contract() -> None:
    parser = build_parser()
    group = next(item for item in parser._action_groups if item.title == CODE_GROUP_TITLE)

    assert group is parser._action_groups[-3]
    assert parser._action_groups[-2].title == SEMANTIC_GROUP_TITLE
    assert parser._action_groups[-1].title == KNOWLEDGE_GROUP_TITLE
    assert tuple(_normalized_action(action) for action in group._group_actions) == (
        EXPECTED_CODE_ACTIONS
    )
    max_file_action = next(
        action for action in group._group_actions if action.dest == "code_max_file_bytes"
    )
    assert max_file_action.type is decimal_megabytes
    help_text = parser.format_help()
    help_start = help_text.index(f"{CODE_GROUP_TITLE}:\n")
    help_end = help_text.index(f"{SEMANTIC_GROUP_TITLE}:\n", help_start)
    assert help_text[help_start:help_end] == EXPECTED_CODE_HELP


def test_code_explicit_aliases_and_abbreviation_policy_remain_stable() -> None:
    parser = build_parser()
    args = parser.parse_args(
        (
            "--code-status",
            "--code-generated",
            "--no-code-vendored",
            "--code-search-mode=fts",
        )
    )

    assert parser.allow_abbrev is False
    assert args.code_include_generated is True
    assert args.code_include_vendored is False
    assert args.code_search_mode == ["fts"]
    assert args._explicit_options == frozenset(
        {
            "code_status",
            "code_include_generated",
            "code_include_vendored",
            "code_search_mode",
        }
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--code-max-mb", "0.001", "--code-search", " "),
            "--code-max-mb must be at least 0.004096",
        ),
        (
            ("--code-search", " ", "--code-search-limit", "0"),
            "--code-search must be non-empty",
        ),
        (
            (
                "--code-search-mode",
                "fts",
                "--code-search-mode",
                "fts",
            ),
            "--code-search-mode values cannot be duplicated",
        ),
        (
            ("--code-reconstruct-strategy", "branches", "--code-json"),
            "--code-reconstruct-strategy requires --code-reconstruct",
        ),
        (
            ("--code-review-limit", "11"),
            "--code-review-limit requires --code-review",
        ),
        (
            ("--code-review", "--code-review-limit", "11"),
            "--code-review-limit above 10 requires --code-json",
        ),
        (
            ("--code-status", "--code-json", "--route", "code", "--apply"),
            "direct code operations are read-only and reject --apply",
        ),
    ),
)
def test_code_validation_error_precedence_remains_stable(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == message


# endregion [02]
