"""Declarative registry for lightweight direct CLI operations."""

from __future__ import annotations
import argparse
import importlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from types import ModuleType
from typing import cast

__all__ = [
    "DIRECT_OPERATIONS",
    "DirectOperation",
    "DirectOperationFamily",
    "dispatch_direct_operation",
    "selected_direct_operations",
]


# region [01] Typed operation contract

DirectHandler = Callable[[argparse.Namespace], int]


class SelectionMode(Enum):
    """How an argparse destination represents an enabled operation."""

    TRUTHY = auto()
    NOT_NONE = auto()


class DirectOperationFamily(Enum):
    """Validation domain for related direct operations."""

    CAPABILITIES = auto()
    PLATFORM = auto()
    MODELS = auto()
    STATUS = auto()
    RECOVERY = auto()
    WATCH = auto()
    REVIEW = auto()
    SEMANTIC = auto()
    ORGANIZATION = auto()
    PDF = auto()
    DOCX = auto()
    OFFICE = auto()
    AUDIO = auto()
    VIDEO = auto()
    ARCHIVE = auto()
    CODE = auto()
    KNOWLEDGE = auto()
    CURATION = auto()


@dataclass(frozen=True, slots=True)
class DirectOperation:
    """One direct CLI destination and its lazily imported handler."""

    destination: str
    handler_name: str
    family: DirectOperationFamily
    selection_mode: SelectionMode = SelectionMode.TRUTHY
    module_name: str = ".cli_direct"

    def is_selected(self, args: argparse.Namespace) -> bool:
        default = None if self.selection_mode is SelectionMode.NOT_NONE else False
        value = getattr(args, self.destination, default)
        if self.selection_mode is SelectionMode.NOT_NONE:
            return value is not None
        return bool(value)

    def load_handler(self) -> DirectHandler:
        module: ModuleType = importlib.import_module(
            self.module_name,
            package=__package__,
        )
        return cast(DirectHandler, getattr(module, self.handler_name))

    def dispatch(self, args: argparse.Namespace) -> int:
        return self.load_handler()(args)


# endregion [01]


# region [02] Stable direct-operation registry

_VALUE = SelectionMode.NOT_NONE
_CAPABILITIES = DirectOperationFamily.CAPABILITIES
_PLATFORM = DirectOperationFamily.PLATFORM
_MODELS = DirectOperationFamily.MODELS
_STATUS = DirectOperationFamily.STATUS
_RECOVERY = DirectOperationFamily.RECOVERY
_WATCH = DirectOperationFamily.WATCH
_REVIEW = DirectOperationFamily.REVIEW
_SEMANTIC = DirectOperationFamily.SEMANTIC
_ORGANIZATION = DirectOperationFamily.ORGANIZATION
_PDF = DirectOperationFamily.PDF
_DOCX = DirectOperationFamily.DOCX
_OFFICE = DirectOperationFamily.OFFICE
_AUDIO = DirectOperationFamily.AUDIO
_VIDEO = DirectOperationFamily.VIDEO
_ARCHIVE = DirectOperationFamily.ARCHIVE
_CODE = DirectOperationFamily.CODE
_KNOWLEDGE = DirectOperationFamily.KNOWLEDGE
_CURATION = DirectOperationFamily.CURATION

DIRECT_OPERATIONS: tuple[DirectOperation, ...] = (
    DirectOperation(
        "doctor_capabilities",
        "run_doctor_capabilities",
        _CAPABILITIES,
        module_name=".cli_capabilities",
    ),
    DirectOperation(
        "doctor_platform",
        "run_doctor_platform",
        _PLATFORM,
        module_name=".cli_platform",
    ),
    DirectOperation(
        "models_prepare",
        "run_models_prepare",
        _MODELS,
        module_name=".cli_models",
    ),
    DirectOperation(
        "models_status",
        "run_models_status",
        _MODELS,
        module_name=".cli_models",
    ),
    DirectOperation("status", "run_operational_status", _STATUS),
    DirectOperation(
        "state_health",
        "run_state_health",
        _STATUS,
        module_name=".cli_state_health",
    ),
    DirectOperation(
        "retention_status",
        "run_retention_status",
        _STATUS,
        module_name=".cli_retention",
    ),
    DirectOperation(
        "action_recovery_status",
        "run_file_action_recovery_status",
        _RECOVERY,
    ),
    DirectOperation(
        "action_recovery_record",
        "run_file_action_recovery_record",
        _RECOVERY,
        _VALUE,
    ),
    DirectOperation(
        "watch",
        "run_incremental_watcher",
        _WATCH,
        module_name=".cli_watcher",
    ),
    DirectOperation("review_candidates", "run_review_candidates", _REVIEW, _VALUE),
    DirectOperation("review_decisions", "run_review_decisions", _REVIEW, _VALUE),
    DirectOperation("review_record", "run_review_record", _REVIEW, _VALUE),
    DirectOperation(
        "review_evidence_sync",
        "run_review_evidence_sync",
        _REVIEW,
        module_name=".cli_review_evidence",
    ),
    DirectOperation(
        "review_evidence_metrics",
        "run_review_evidence_metrics",
        _REVIEW,
        module_name=".cli_review_evidence",
    ),
    DirectOperation(
        "review_evidence_list",
        "run_review_evidence_list",
        _REVIEW,
        _VALUE,
        module_name=".cli_review_evidence",
    ),
    DirectOperation(
        "semantic_status",
        "run_semantic_status",
        _SEMANTIC,
        module_name=".cli_semantic",
    ),
    DirectOperation(
        "semantic_plan",
        "run_semantic_plan",
        _SEMANTIC,
        _VALUE,
        module_name=".cli_semantic",
    ),
    DirectOperation(
        "semantic_prepare_models",
        "run_semantic_prepare_models",
        _SEMANTIC,
        module_name=".cli_semantic",
    ),
    DirectOperation(
        "semantic_index",
        "run_semantic_index",
        _SEMANTIC,
        _VALUE,
        module_name=".cli_semantic",
    ),
    DirectOperation(
        "semantic_search",
        "run_semantic_search",
        _SEMANTIC,
        _VALUE,
        module_name=".cli_semantic",
    ),
    DirectOperation(
        "semantic_image_calibrate",
        "run_semantic_image_calibrate",
        _SEMANTIC,
        _VALUE,
        module_name=".cli_semantic",
    ),
    DirectOperation(
        "semantic_classify",
        "run_semantic_classify",
        _SEMANTIC,
        _VALUE,
        module_name=".cli_semantic",
    ),
    DirectOperation(
        "semantic_evidence",
        "run_semantic_evidence",
        _SEMANTIC,
        _VALUE,
        module_name=".cli_semantic",
    ),
    DirectOperation("catalog_documents", "run_document_catalog", _ORGANIZATION),
    DirectOperation("catalog_preview", "run_document_catalog_preview", _ORGANIZATION, _VALUE),
    DirectOperation("organization_plan", "run_organization_plan", _ORGANIZATION),
    DirectOperation("organization_preview", "run_organization_preview", _ORGANIZATION, _VALUE),
    DirectOperation(
        "curation_preview",
        "run_curation_preview",
        _CURATION,
        _VALUE,
        module_name=".cli_curation",
    ),
    DirectOperation("organization_apply", "run_organization_apply", _ORGANIZATION),
    DirectOperation("pdf_search", "run_pdf_search", _PDF, _VALUE),
    DirectOperation("pdf_layout_groups", "run_pdf_layout_groups", _PDF, _VALUE),
    DirectOperation("pdf_doctor", "run_pdf_doctor", _PDF),
    DirectOperation("pdf_verify", "run_pdf_verify", _PDF),
    DirectOperation("docx_search", "run_docx_search", _DOCX, _VALUE),
    DirectOperation("docx_layout_groups", "run_docx_layout_groups", _DOCX, _VALUE),
    DirectOperation("docx_missing_pdf", "run_docx_missing_pdf", _DOCX, _VALUE),
    DirectOperation("office_search", "run_office_search", _OFFICE, _VALUE),
    DirectOperation(
        "archive_status",
        "run_archive_status",
        _ARCHIVE,
        module_name=".cli_archive",
    ),
    DirectOperation(
        "archive_search",
        "run_archive_search",
        _ARCHIVE,
        _VALUE,
        module_name=".cli_archive",
    ),
    DirectOperation(
        "archive_list",
        "run_archive_list",
        _ARCHIVE,
        _VALUE,
        module_name=".cli_archive",
    ),
    DirectOperation(
        "audio_search",
        "run_audio_search",
        _AUDIO,
        _VALUE,
        module_name=".cli_audio",
    ),
    DirectOperation("audio_doctor", "run_audio_doctor", _AUDIO, module_name=".cli_audio"),
    DirectOperation(
        "video_status",
        "run_video_status",
        _VIDEO,
        module_name=".cli_video",
    ),
    DirectOperation(
        "video_search",
        "run_video_search",
        _VIDEO,
        _VALUE,
        module_name=".cli_video",
    ),
    DirectOperation(
        "video_doctor",
        "run_video_doctor",
        _VIDEO,
        module_name=".cli_video",
    ),
    DirectOperation("code_status", "run_code_status", _CODE, module_name=".cli_code"),
    DirectOperation("code_search", "run_code_search", _CODE, _VALUE, module_name=".cli_code"),
    DirectOperation("code_projects", "run_code_projects", _CODE, module_name=".cli_code"),
    DirectOperation(
        "code_reconstruct",
        "run_code_reconstruct",
        _CODE,
        _VALUE,
        module_name=".cli_code",
    ),
    DirectOperation(
        "knowledge_status",
        "run_knowledge_status",
        _KNOWLEDGE,
        module_name=".cli_knowledge",
    ),
    DirectOperation(
        "knowledge_health",
        "run_knowledge_health",
        _KNOWLEDGE,
        _VALUE,
        module_name=".cli_knowledge",
    ),
    DirectOperation(
        "knowledge_search",
        "run_knowledge_search",
        _KNOWLEDGE,
        _VALUE,
        module_name=".cli_knowledge",
    ),
    DirectOperation(
        "knowledge_context",
        "run_knowledge_context",
        _KNOWLEDGE,
        _VALUE,
        module_name=".cli_knowledge",
    ),
)


def selected_direct_operations(
    args: argparse.Namespace,
    *,
    family: DirectOperationFamily | None = None,
) -> tuple[DirectOperation, ...]:
    """Return selected operations in their stable dispatch order."""

    return tuple(
        operation
        for operation in DIRECT_OPERATIONS
        if (family is None or operation.family is family) and operation.is_selected(args)
    )


def dispatch_direct_operation(args: argparse.Namespace) -> int | None:
    """Dispatch the first selected operation without eagerly importing handlers."""

    selected = selected_direct_operations(args)
    if not selected:
        return None
    return selected[0].dispatch(args)


# endregion [02]
