"""Compatibility and isolation contracts for extracted CLI families."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_family_boundaries.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from neocortex.api.cli.cli_operations import DIRECT_OPERATIONS
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
# endregion [01]

# region [02] Implementación


SEMANTIC_DIRECT_ARGUMENTS = (
    ("--semantic-status",),
    ("--semantic-plan", "text"),
    ("--semantic-prepare-models",),
    ("--semantic-index", "text"),
    ("--semantic-search", "relay"),
    ("--semantic-classify", "text"),
    ("--semantic-evidence", "item:pdf:1"),
)
AUDIO_DIRECT_ARGUMENTS = (
    ("--audio-search", "relay"),
    ("--audio-doctor",),
)
FAMILY_DIRECT_ARGUMENTS = SEMANTIC_DIRECT_ARGUMENTS + AUDIO_DIRECT_ARGUMENTS


def _operation(destination: str):
    return next(
        operation
        for operation in DIRECT_OPERATIONS
        if operation.destination == destination
    )


def test_registry_routes_extracted_handlers_to_family_modules() -> None:
    semantic = tuple(
        operation
        for operation in DIRECT_OPERATIONS
        if operation.destination.startswith("semantic_")
    )
    audio = tuple(
        operation
        for operation in DIRECT_OPERATIONS
        if operation.destination in {"audio_search", "audio_doctor"}
    )

    assert len(semantic) == 9
    assert "semantic_image_calibrate" in {operation.destination for operation in semantic}
    assert {operation.module_name for operation in semantic} == {".cli_semantic"}
    assert len(audio) == 2
    assert {operation.module_name for operation in audio} == {".cli_audio"}
    diagnostics = tuple(
        operation for operation in DIRECT_OPERATIONS
        if operation.destination in {"pdf_diagnostics", "text_errors", "archive_issues"}
    )
    assert len(diagnostics) == 3
    assert {operation.module_name for operation in diagnostics} == {".cli_content_diagnostics"}


def test_family_handlers_remain_lazy_and_isolated_in_a_fresh_process() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent(
                """
                import sys

                from neocortex.api.cli.cli_operations import DIRECT_OPERATIONS
                from neocortex.api.cli.cli_parser import build_parser

                names = {
                    "neocortex.api.cli.cli_audio",
                    "neocortex.api.cli.cli_direct",
                    "neocortex.api.cli.cli_semantic",
                }
                if names.intersection(sys.modules):
                    raise SystemExit("family handler imported during parser setup")
                build_parser().parse_args([])
                if names.intersection(sys.modules):
                    raise SystemExit("family handler imported by empty parsing")
                audio = next(
                    item for item in DIRECT_OPERATIONS
                    if item.destination == "audio_search"
                )
                audio.load_handler()
                if "neocortex.api.cli.cli_audio" not in sys.modules:
                    raise SystemExit("audio handler was not loaded")
                if {
                    "neocortex.api.cli.cli_direct",
                    "neocortex.api.cli.cli_semantic",
                }.intersection(sys.modules):
                    raise SystemExit("audio loading crossed a family boundary")
                """
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_direct_cli_has_no_cross_family_handler_delegates() -> None:
    for operation in DIRECT_OPERATIONS:
        if operation.module_name not in {".cli_audio", ".cli_semantic"}:
            continue
        direct = __import__("neocortex.api.cli.cli_direct", fromlist=["cli_direct"])
        assert not hasattr(direct, operation.handler_name)


@pytest.mark.parametrize("direct_arguments", FAMILY_DIRECT_ARGUMENTS)
@pytest.mark.parametrize(
    ("conflict", "message"),
    (
        (("--apply",), "cannot be combined with file-action --apply"),
        (("--route", "pdf"), "cannot be combined with --route"),
    ),
)
def test_family_directs_report_usage_two_for_ignored_framework_intent(
    direct_arguments: tuple[str, ...],
    conflict: tuple[str, ...],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main((*direct_arguments, *conflict))

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert message in captured.err


@pytest.mark.parametrize("direct_arguments", FAMILY_DIRECT_ARGUMENTS)
def test_explicit_neutral_route_remains_compatible(
    direct_arguments: tuple[str, ...],
) -> None:
    args = build_parser().parse_args((*direct_arguments, "--route", "none"))

    validate_arguments(args)


@pytest.mark.parametrize(
    ("direct_arguments", "message"),
    (
        (
            ("--semantic-status",),
            "semantic direct actions cannot be combined with file-action --apply",
        ),
        (
            ("--audio-doctor",),
            "audio direct actions cannot be combined with file-action --apply",
        ),
    ),
)
def test_apply_error_precedes_route_error_for_family_directs(
    direct_arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args((*direct_arguments, "--apply", "--route", "pdf"))

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == message


def test_docx_office_group_order_defaults_and_explicit_tracking_are_stable() -> None:
    parser = build_parser()
    docx = next(group for group in parser._action_groups if group.title == "DOCX route")
    office = next(
        group for group in parser._action_groups if group.title == "XLSX/PPTX/ODT route"
    )

    assert parser._action_groups.index(docx) + 1 == parser._action_groups.index(office)
    assert tuple(action.dest for action in docx._group_actions) == (
        "docx_max_file_bytes",
        "docx_max_documents",
        "docx_max_text_chars",
        "retry_docx_errors",
        "docx_memory_budget_mb",
        "docx_min_free_memory_mb",
        "docx_min_free_commit_mb",
        "docx_memory_wait_timeout",
        "docx_search",
        "docx_search_limit",
        "docx_layout_groups",
        "docx_missing_pdf",
    )
    assert tuple(action.dest for action in office._group_actions) == (
        "office_max_file_bytes",
        "office_max_documents",
        "office_max_text_chars",
        "retry_office_errors",
        "office_memory_budget_mb",
        "office_min_free_memory_mb",
        "office_min_free_commit_mb",
        "office_memory_wait_timeout",
        "office_search",
        "office_search_limit",
    )

    args = parser.parse_args(
        (
            "--all",
            "--docx-max-count=3",
            "--office-max-count",
            "4",
            "--office-memory-budget-mb=600",
        )
    )
    validate_arguments(args)
    assert args.docx_max_documents == 3
    assert args.office_max_documents == 4
    assert args.office_memory_budget_mb == 600
    assert {
        "docx_max_documents",
        "office_max_documents",
        "office_memory_budget_mb",
    } <= args._explicit_options


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--docx-max-count", "0", "--docx-max-text-chars", "0"),
            "--docx-max-count must be positive",
        ),
        (
            ("--office-max-count", "0", "--office-max-text-chars", "0"),
            "--office-max-count must be positive",
        ),
        (
            ("--office-search", " ", "--office-search-limit", "0"),
            "--office-search-limit must be between 1 and 1000",
        ),
    ),
)
def test_docx_office_validation_precedence_remains_stable(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == message
# endregion [02]
