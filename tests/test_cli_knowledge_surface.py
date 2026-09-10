"""Compatibility contract for the flat read-only Knowledge CLI surface."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_knowledge_surface.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import argparse

import pytest

from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
# endregion [01]

# region [02] Implementación


KNOWLEDGE_GROUP_TITLE = "Read-only Knowledge Plane"

EXPECTED_KNOWLEDGE_ACTIONS = (
    (
        ("--knowledge-status",),
        "knowledge_status",
        "_StoreTrueAction",
        0,
        True,
        False,
        None,
        None,
        None,
        False,
        "show a bounded cross-owner logical snapshot without creating state",
    ),
    (
        ("--knowledge-health",),
        "knowledge_health",
        "_StoreAction",
        None,
        None,
        None,
        None,
        None,
        "RESOURCE_ID",
        False,
        "explain one stable asset identity across its published owner facts",
    ),
    (
        ("--knowledge-search",),
        "knowledge_search",
        "_StoreAction",
        None,
        None,
        None,
        None,
        None,
        "QUERY",
        False,
        "search available lexical, semantic, structural and catalog evidence",
    ),
    (
        ("--knowledge-context",),
        "knowledge_context",
        "_StoreAction",
        None,
        None,
        None,
        None,
        None,
        "QUERY",
        False,
        "build a bounded cited context from one read-only Knowledge search",
    ),
    (
        ("--knowledge-json",),
        "knowledge_json",
        "_StoreTrueAction",
        0,
        True,
        False,
        None,
        None,
        None,
        False,
        None,
    ),
    (
        ("--knowledge-response-version",),
        "knowledge_response_version",
        "_StoreAction",
        None,
        None,
        2,
        "int",
        (1, 2),
        None,
        False,
        "context response contract: compact agent v2 (default) or legacy v1",
    ),
    (
        ("--knowledge-limit",),
        "knowledge_limit",
        "_StoreAction",
        None,
        None,
        20,
        "int",
        None,
        "N",
        False,
        None,
    ),
    (
        ("--knowledge-context-characters",),
        "knowledge_context_characters",
        "_StoreAction",
        None,
        None,
        12_000,
        "int",
        None,
        "N",
        False,
        (
            "maximum ContextBundle characters; defaults to 12000 "
            "(allowed range: 1..1000000)"
        ),
    ),
    (
        ("--knowledge-history",),
        "knowledge_history",
        "_StoreTrueAction",
        0,
        True,
        False,
        None,
        None,
        None,
        False,
        None,
    ),
    (
        ("--knowledge-budget-rows",),
        "knowledge_budget_rows",
        "_StoreAction",
        None,
        None,
        None,
        "int",
        None,
        "N",
        False,
        None,
    ),
    (
        ("--knowledge-budget-vectors",),
        "knowledge_budget_vectors",
        "_StoreAction",
        None,
        None,
        None,
        "int",
        None,
        "N",
        False,
        None,
    ),
    (
        ("--knowledge-budget-temporary-bytes",),
        "knowledge_budget_temporary_bytes",
        "_StoreAction",
        None,
        None,
        None,
        "int",
        None,
        "N",
        False,
        None,
    ),
    (
        ("--knowledge-budget-seconds",),
        "knowledge_budget_seconds",
        "_StoreAction",
        None,
        None,
        None,
        "float",
        None,
        "S",
        False,
        None,
    ),
    (
        ("--knowledge-mode",),
        "knowledge_mode",
        "_StoreAction",
        None,
        None,
        "evidence",
        None,
        ("discovery", "evidence"),
        None,
        False,
        None,
    ),
)

EXPECTED_KNOWLEDGE_HELP = (
    "Read-only Knowledge Plane:\n"
    "  --knowledge-status    show a bounded cross-owner logical snapshot without\n"
    "                        creating state\n"
    "  --knowledge-health RESOURCE_ID\n"
    "                        explain one stable asset identity across its published\n"
    "                        owner facts\n"
    "  --knowledge-search QUERY\n"
    "                        search available lexical, semantic, structural and\n"
    "                        catalog evidence\n"
    "  --knowledge-context QUERY\n"
    "                        build a bounded cited context from one read-only\n"
    "                        Knowledge search\n"
    "  --knowledge-json\n"
    "  --knowledge-response-version {1,2}\n"
    "                        context response contract: compact agent v2 (default)\n"
    "                        or legacy v1\n"
    "  --knowledge-limit N\n"
    "  --knowledge-context-characters N\n"
    "                        maximum ContextBundle characters; defaults to 12000\n"
    "                        (allowed range: 1..1000000)\n"
    "  --knowledge-history\n"
    "  --knowledge-budget-rows N\n"
    "  --knowledge-budget-vectors N\n"
    "  --knowledge-budget-temporary-bytes N\n"
    "  --knowledge-budget-seconds S\n"
    "  --knowledge-mode {discovery,evidence}\n"
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


def test_knowledge_actions_and_help_preserve_the_normalized_flat_contract() -> None:
    parser = build_parser()
    group = next(
        item for item in parser._action_groups if item.title == KNOWLEDGE_GROUP_TITLE
    )

    assert group is parser._action_groups[-1]
    assert tuple(_normalized_action(action) for action in group._group_actions) == (
        EXPECTED_KNOWLEDGE_ACTIONS
    )
    help_text = parser.format_help()
    help_start = help_text.index(f"{KNOWLEDGE_GROUP_TITLE}:\n")
    assert help_text[help_start:] == EXPECTED_KNOWLEDGE_HELP


def test_knowledge_explicit_options_and_abbreviation_policy_remain_stable() -> None:
    parser = build_parser()
    args = parser.parse_args(
        (
            "--knowledge-search",
            "relay",
            "--knowledge-limit=37",
            "--knowledge-history",
        )
    )

    assert parser.allow_abbrev is False
    assert args._explicit_options == frozenset(
        {"knowledge_search", "knowledge_limit", "knowledge_history"}
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--knowledge-status", "--knowledge-search", " "),
            "Knowledge direct actions are mutually exclusive",
        ),
        (
            ("--knowledge-limit", "0"),
            "--knowledge-limit must be between 1 and 1000",
        ),
        (
            ("--knowledge-context", "relay", "--knowledge-limit", "101", "--apply"),
            "--knowledge-limit must be between 1 and 100 for --knowledge-context",
        ),
        (
            ("--knowledge-status", "--knowledge-history", "--apply"),
            "Knowledge limit, history and mode require search or context",
        ),
    ),
)
def test_knowledge_validation_error_precedence_remains_stable(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == message
# endregion [02]
