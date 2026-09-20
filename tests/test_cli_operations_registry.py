"""Parity tests for the declarative direct-operation CLI registry."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_operations_registry.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import pytest

from neocortex.api.cli.cli_app import dispatch_direct
from neocortex.api.cli.cli_operations import (
    DIRECT_OPERATIONS,
    selected_direct_operations,
)
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
# endregion [01]

# region [02] Implementación


DIRECT_ARGUMENT_CASES = (
    ("pdf_diagnostics", "run_pdf_diagnostics", ("--pdf-diagnostics", "20")),
    ("text_errors", "run_text_errors", ("--text-errors", "20")),
    ("content_diagnostics", "run_content_diagnostics", ("--content-diagnostics", "20")),
    (
        "content_diagnostics_v2",
        "run_content_diagnostics",
        ("--content-diagnostics-v2", "20"),
    ),
    (
        "doctor_capabilities",
        "run_doctor_capabilities",
        ("--doctor-capabilities",),
    ),
    ("doctor_platform", "run_doctor_platform", ("--doctor-platform",)),
    ("models_prepare", "run_models_prepare", ("--models-prepare",)),
    ("models_status", "run_models_status", ("--models-status",)),
    ("status", "run_operational_status", ("--status",)),
    ("state_health", "run_state_health", ("--state-health",)),
    ("retention_status", "run_retention_status", ("--retention-status",)),
    (
        "action_recovery_status",
        "run_file_action_recovery_status",
        ("--action-recovery-status",),
    ),
    (
        "action_recovery_record",
        "run_file_action_recovery_record",
        ("--action-recovery-record", "1"),
    ),
    ("watch", "run_incremental_watcher", ("--watch",)),
    ("semantic_status", "run_semantic_status", ("--semantic-status",)),
    ("semantic_plan", "run_semantic_plan", ("--semantic-plan", "text")),
    (
        "semantic_prepare_models",
        "run_semantic_prepare_models",
        ("--semantic-prepare-models",),
    ),
    ("semantic_index", "run_semantic_index", ("--semantic-index", "text")),
    (
        "semantic_exact_index_build",
        "run_semantic_exact_index_build",
        ("--semantic-exact-index-build", "/tmp/exact-index"),
    ),
    ("semantic_search", "run_semantic_search", ("--semantic-search", "query")),
    (
        "semantic_image_calibrate", "run_semantic_image_calibrate",
        ("--semantic-image-calibrate", "calibration.json"),
    ),
    (
        "semantic_classify",
        "run_semantic_classify",
        ("--semantic-classify", "text"),
    ),
    (
        "semantic_evidence",
        "run_semantic_evidence",
        ("--semantic-evidence", "item:pdf:1"),
    ),
    ("catalog_documents", "run_document_catalog", ("--catalog-documents",)),
    (
        "catalog_preview",
        "run_document_catalog_preview",
        ("--catalog-preview", "1"),
    ),
    ("organization_plan", "run_organization_plan", ("--organization-plan",)),
    (
        "organization_preview",
        "run_organization_preview",
        ("--organization-preview", "1"),
    ),
    (
        "curation_preview",
        "run_curation_preview",
        ("--curation-preview", "1"),
    ),
    (
        "organization_apply",
        "run_organization_apply",
        ("--organization-apply",),
    ),
    ("pdf_search", "run_pdf_search", ("--pdf-search", "query")),
    (
        "pdf_layout_groups",
        "run_pdf_layout_groups",
        ("--pdf-layout-groups", "1"),
    ),
    ("pdf_doctor", "run_pdf_doctor", ("--pdf-doctor",)),
    ("pdf_verify", "run_pdf_verify", ("--pdf-verify",)),
    ("docx_search", "run_docx_search", ("--docx-search", "query")),
    (
        "docx_layout_groups",
        "run_docx_layout_groups",
        ("--docx-layout-groups", "1"),
    ),
    ("docx_missing_pdf", "run_docx_missing_pdf", ("--docx-missing-pdf", "1")),
    ("office_search", "run_office_search", ("--office-search", "query")),
    ("audio_search", "run_audio_search", ("--audio-search", "query")),
    ("audio_doctor", "run_audio_doctor", ("--audio-doctor",)),
    ("video_status", "run_video_status", ("--video-status",)),
    ("video_search", "run_video_search", ("--video-search", "query")),
    ("video_doctor", "run_video_doctor", ("--video-doctor",)),
    ("knowledge_status", "run_knowledge_status", ("--knowledge-status",)),
    (
        "knowledge_health",
        "run_knowledge_health",
        ("--knowledge-health", "resource:file:1:2:-1"),
    ),
    (
        "knowledge_search",
        "run_knowledge_search",
        ("--knowledge-search", "query"),
    ),
    (
        "knowledge_context",
        "run_knowledge_context",
        ("--knowledge-context", "query"),
    ),
)


def test_importing_dispatch_keeps_direct_handler_module_lazy() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent(
                """
                import sys

                from neocortex.api.cli.cli_app import dispatch_direct
                from neocortex.api.cli.cli_parser import build_parser

                handler_modules = {
                    "neocortex.api.cli.cli_capabilities",
                    "neocortex.api.cli.cli_direct",
                    "neocortex.api.cli.cli_watcher",
                    "neocortex.api.cli.cli_knowledge",
                    "neocortex.api.cli.cli_video",
                    "neocortex.runtime.control.watcher",
                }
                loaded = handler_modules.intersection(sys.modules)
                if loaded:
                    raise SystemExit("handler imported with cli_app: " + ",".join(loaded))
                if dispatch_direct(build_parser().parse_args([])) is not None:
                    raise SystemExit("unexpected direct dispatch")
                loaded = handler_modules.intersection(sys.modules)
                if loaded:
                    raise SystemExit("empty dispatch imported handler: " + ",".join(loaded))
                """
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_registry_covers_every_stable_direct_destination_once() -> None:
    expected = tuple((destination, handler) for destination, handler, _ in DIRECT_ARGUMENT_CASES)
    registered = tuple(
        (operation.destination, operation.handler_name) for operation in DIRECT_OPERATIONS
    )

    assert registered == expected
    assert len({operation.destination for operation in DIRECT_OPERATIONS}) == len(DIRECT_OPERATIONS)
    parser_destinations = vars(build_parser().parse_args([]))
    assert {operation.destination for operation in DIRECT_OPERATIONS} <= parser_destinations.keys()


@pytest.mark.parametrize(
    ("destination", "handler_name", "arguments"),
    DIRECT_ARGUMENT_CASES,
)
def test_each_direct_flag_selects_and_lazily_dispatches_its_registered_handler(
    destination: str,
    handler_name: str,
    arguments: tuple[str, ...],
) -> None:
    args = build_parser().parse_args(arguments)
    selected = selected_direct_operations(args)
    assert tuple(operation.destination for operation in selected) == (destination,)
    operation = selected[0]

    handler = Mock(return_value=37)
    direct_module = ModuleType(operation.module_name)
    setattr(direct_module, handler_name, handler)
    with patch(
        "neocortex.api.cli.cli_operations.importlib.import_module",
        return_value=direct_module,
    ) as import_module:
        assert dispatch_direct(args) == 37

    import_module.assert_called_once_with(
        operation.module_name,
        package="neocortex.api.cli",
    )
    handler.assert_called_once_with(args)


def test_no_direct_selection_imports_no_handler_module() -> None:
    args = build_parser().parse_args([])
    with patch("neocortex.api.cli.cli_operations.importlib.import_module") as import_module:
        assert dispatch_direct(args) is None
    import_module.assert_not_called()


def test_direct_operations_remain_mutually_exclusive_across_domains() -> None:
    args = build_parser().parse_args(("--pdf-search", "transformer", "--docx-search", "breaker"))

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == (
        "direct status/recovery/semantic/curation/PDF/DOCX/Office/audio/video/"
        "Knowledge "
        "operations are mutually exclusive"
    )


def test_all_remains_incompatible_with_any_registered_direct_operation() -> None:
    args = build_parser().parse_args(("--all", "--audio-doctor"))

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == "--all cannot be combined with direct query/doctor options"


# endregion [02]
