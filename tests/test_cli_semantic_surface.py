"""Compatibility contract for the flat multimodal Semantic CLI surface."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_semantic_surface.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import argparse
import os

import pytest

from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from neocortex.platform_policy import LINUX_MUTATION_REASON
# endregion [01]

# region [02] Implementación


SEMANTIC_GROUP_TITLE = "Multimodal semantic index"
KNOWLEDGE_GROUP_TITLE = "Read-only Knowledge Plane"
_APPLY_PRECEDENCE_MESSAGE = (
    "--semantic-plan-json requires --semantic-plan"
    if os.name == "nt"
    else f"{LINUX_MUTATION_REASON}: corpus mutation is intentionally unavailable on Linux"
)


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
    help_text: str,
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


EXPECTED_SEMANTIC_ACTIONS = (
    _expected_flag(
        "--semantic-status",
        "semantic_status",
        "show bounded read-only semantic state and generation status",
    ),
    _expected_store(
        "--semantic-plan",
        "semantic_plan",
        choices=("text", "image", "all"),
        help_text=(
            "plan exact semantic resources, chunks, reuse and bytes; model-time "
            "ranges remain unavailable unless an exact calibration is supplied "
            "through the service API"
        ),
    ),
    _expected_flag(
        "--semantic-plan-json",
        "semantic_plan_json",
        "emit the read-only semantic plan as one stable JSON document",
    ),
    _expected_store(
        "--semantic-plan-max-scratch-bytes",
        "semantic_plan_max_scratch_bytes",
        default=512 * 1024 * 1024,
        type_name="int",
        metavar="BYTES",
        help_text=("hard private SQLite scratch-storage ceiling for semantic planning"),
    ),
    _expected_flag(
        "--semantic-prepare-models",
        "semantic_prepare_models",
        "explicitly download or load the configured text and CLIP models",
    ),
    _expected_store(
        "--semantic-index",
        "semantic_index",
        choices=("text", "image", "all"),
        help_text="incrementally index existing durable text caches, images, or both",
    ),
    _expected_store(
        "--semantic-max-items",
        "semantic_max_items",
        default=50,
        type_name="int",
        metavar="N",
        help_text=(
            "maximum complete new or changed source items admitted by one "
            "semantic index run; exact replay is free"
        ),
    ),
    _expected_store(
        "--semantic-max-new-jobs",
        "semantic_max_new_jobs",
        default=1_500,
        type_name="int",
        metavar="N",
        help_text=(
            "maximum durable embedding jobs newly created or reactivated by one "
            "semantic index run; exact replay is free"
        ),
    ),
    _expected_store(
        "--semantic-time-budget-seconds",
        "semantic_time_budget_seconds",
        default=900.0,
        type_name="float",
        metavar="SECONDS",
        help_text="shared monotonic time budget for one semantic index run",
    ),
    _expected_store(
        "--semantic-search",
        "semantic_search",
        metavar="QUERY",
        help_text="search lexical and/or separate semantic vector spaces",
    ),
    _expected_store(
        "--semantic-classify",
        "semantic_classify",
        choices=("text", "image", "all"),
        help_text="materialize advisory ontology evidence for active embeddings",
    ),
    _expected_store(
        "--semantic-evidence",
        "semantic_evidence",
        metavar="ITEM_ID",
        help_text="list current advisory ontology evidence for one exact item",
    ),
    _expected_store(
        "--semantic-evidence-limit",
        "semantic_evidence_limit",
        default=100,
        type_name="int",
        metavar="N",
        help_text="maximum advisory evidence rows to display; truncation is reported",
    ),
    _expected_store(
        "--semantic-source",
        "semantic_source",
        choices=("pdf", "docx", "xlsx", "pptx", "odt", "audio", "code"),
        help_text=("repeat to select durable text caches for text/all planning or indexing"),
        action_name="_AppendAction",
    ),
    _expected_store(
        "--semantic-text-profile",
        "semantic_text_profile",
        default="quality",
        choices=("quality", "compact"),
        help_text="quality uses the Spanish Jina model; compact uses MiniLM",
    ),
    _expected_store(
        "--semantic-search-mode",
        "semantic_search_mode",
        default="all",
        choices=("all", "text", "image", "lexical"),
        help_text="rank all spaces by default, or select one independent mode",
    ),
    _expected_store(
        "--semantic-search-limit",
        "semantic_search_limit",
        default=20,
        type_name="int",
        metavar="N",
    ),
    _expected_store(
        "--semantic-max-vectors",
        "semantic_max_vectors",
        default=500_000,
        type_name="int",
        metavar="N",
        help_text="hard exact-search scan bound; incomplete results are reported",
    ),
    _expected_store(
        "--semantic-model-cache",
        "semantic_model_cache",
        type_name="Path",
        metavar="DIRECTORY",
    ),
    _expected_store(
        "--semantic-threads",
        "semantic_threads",
        type_name="int",
        metavar="N",
    ),
    _expected_flag(
        "--semantic-no-ocr",
        "semantic_no_ocr",
        "plan or index visual embeddings without retained image OCR text",
    ),
    _expected_flag(
        "--semantic-include-compact",
        "semantic_include_compact",
        "also acquire the optional compact text model during preparation",
    ),
)

EXPECTED_SEMANTIC_HELP = (
    "Multimodal semantic index:\n"
    "  --semantic-status     show bounded read-only semantic state and generation\n"
    "                        status\n"
    "  --semantic-plan {text,image,all}\n"
    "                        plan exact semantic resources, chunks, reuse and\n"
    "                        bytes; model-time ranges remain unavailable unless an\n"
    "                        exact calibration is supplied through the service API\n"
    "  --semantic-plan-json  emit the read-only semantic plan as one stable JSON\n"
    "                        document\n"
    "  --semantic-plan-max-scratch-bytes BYTES\n"
    "                        hard private SQLite scratch-storage ceiling for\n"
    "                        semantic planning\n"
    "  --semantic-prepare-models\n"
    "                        explicitly download or load the configured text and\n"
    "                        CLIP models\n"
    "  --semantic-index {text,image,all}\n"
    "                        incrementally index existing durable text caches,\n"
    "                        images, or both\n"
    "  --semantic-max-items N\n"
    "                        maximum complete new or changed source items admitted\n"
    "                        by one semantic index run; exact replay is free\n"
    "  --semantic-max-new-jobs N\n"
    "                        maximum durable embedding jobs newly created or\n"
    "                        reactivated by one semantic index run; exact replay is\n"
    "                        free\n"
    "  --semantic-time-budget-seconds SECONDS\n"
    "                        shared monotonic time budget for one semantic index\n"
    "                        run\n"
    "  --semantic-search QUERY\n"
    "                        search lexical and/or separate semantic vector spaces\n"
    "  --semantic-classify {text,image,all}\n"
    "                        materialize advisory ontology evidence for active\n"
    "                        embeddings\n"
    "  --semantic-evidence ITEM_ID\n"
    "                        list current advisory ontology evidence for one exact\n"
    "                        item\n"
    "  --semantic-evidence-limit N\n"
    "                        maximum advisory evidence rows to display; truncation\n"
    "                        is reported\n"
    "  --semantic-source {pdf,docx,xlsx,pptx,odt,audio,code}\n"
    "                        repeat to select durable text caches for text/all\n"
    "                        planning or indexing\n"
    "  --semantic-text-profile {quality,compact}\n"
    "                        quality uses the Spanish Jina model; compact uses\n"
    "                        MiniLM\n"
    "  --semantic-search-mode {all,text,image,lexical}\n"
    "                        rank all spaces by default, or select one independent\n"
    "                        mode\n"
    "  --semantic-search-limit N\n"
    "  --semantic-max-vectors N\n"
    "                        hard exact-search scan bound; incomplete results are\n"
    "                        reported\n"
    "  --semantic-model-cache DIRECTORY\n"
    "  --semantic-threads N\n"
    "  --semantic-no-ocr     plan or index visual embeddings without retained image\n"
    "                        OCR text\n"
    "  --semantic-include-compact\n"
    "                        also acquire the optional compact text model during\n"
    "                        preparation\n"
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


def test_semantic_actions_and_help_preserve_the_normalized_flat_contract() -> None:
    parser = build_parser()
    group = next(item for item in parser._action_groups if item.title == SEMANTIC_GROUP_TITLE)

    assert group is parser._action_groups[-2]
    assert parser._action_groups[-1].title == KNOWLEDGE_GROUP_TITLE
    assert tuple(_normalized_action(action) for action in group._group_actions) == (
        EXPECTED_SEMANTIC_ACTIONS
    )
    help_text = parser.format_help()
    help_start = help_text.index(f"{SEMANTIC_GROUP_TITLE}:\n")
    help_end = help_text.index(f"{KNOWLEDGE_GROUP_TITLE}:\n", help_start)
    assert help_text[help_start:help_end] == EXPECTED_SEMANTIC_HELP


def test_semantic_explicit_options_and_abbreviation_policy_remain_stable() -> None:
    parser = build_parser()
    args = parser.parse_args(
        (
            "--semantic-plan=all",
            "--semantic-plan-json",
            "--semantic-plan-max-scratch-bytes=131072",
            "--semantic-source=pdf",
            "--semantic-no-ocr",
        )
    )

    assert parser.allow_abbrev is False
    assert args.semantic_plan == "all"
    assert args.semantic_plan_max_scratch_bytes == 131_072
    assert args.semantic_source == ["pdf"]
    assert args._explicit_options == frozenset(
        {
            "semantic_plan",
            "semantic_plan_json",
            "semantic_plan_max_scratch_bytes",
            "semantic_source",
            "semantic_no_ocr",
        }
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--semantic-status", "--semantic-plan", "text"),
            "semantic direct actions are mutually exclusive",
        ),
        (
            ("--semantic-search", "", "--semantic-search-limit", "0"),
            "--semantic-search must be non-empty",
        ),
        (
            (
                "--semantic-plan",
                "image",
                "--semantic-source",
                "pdf",
                "--semantic-plan-json",
            ),
            "--semantic-source requires semantic text/all planning or indexing",
        ),
        (
            ("--semantic-status", "--semantic-plan-json", "--apply"),
            _APPLY_PRECEDENCE_MESSAGE,
        ),
        (
            ("--semantic-status", "--semantic-plan-max-scratch-bytes", "131072"),
            "--semantic-plan-max-scratch-bytes requires --semantic-plan",
        ),
        (
            ("--semantic-plan", "text", "--semantic-model-cache", "models"),
            ("semantic model cache/thread options require prepare, index, search, or classify"),
        ),
    ),
)
def test_semantic_validation_error_precedence_remains_stable(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == message


# endregion [02]
