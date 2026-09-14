"""Typed identification evidence for a ZIP-backed logical document.

Identification is deliberately weaker than package validation or application
opening.  Neither a declared MIME nor a proposed extension authorizes an effect.
"""

from __future__ import annotations

from dataclasses import dataclass


MAX_DECLARED_MIME_BYTES = 512
ODF_MIME_KINDS = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.text-template": "ott",
    "application/vnd.oasis.opendocument.text-master": "odm",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.spreadsheet-template": "ots",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.presentation-template": "otp",
    "application/vnd.oasis.opendocument.graphics": "odg",
    "application/vnd.oasis.opendocument.graphics-template": "otg",
    "application/vnd.oasis.opendocument.formula": "odf",
    "application/vnd.oasis.opendocument.chart": "odc",
    "application/vnd.oasis.opendocument.database": "odb",
}
LOGICAL_MEDIA_TYPES = {
    **{kind: mime for mime, kind in ODF_MIME_KINDS.items()},
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "epub": "application/epub+zip",
}
ODF_KINDS = frozenset(ODF_MIME_KINDS.values())


@dataclass(frozen=True, slots=True)
class LogicalDocumentEvidence:
    declared_mime: str | None
    logical_kind: str | None
    proposed_extension: str | None
    evidence: tuple[str, ...]
    identification_status: str
    integrity_status: str = "not_verified"
    opening_status: str = "not_verified"

    @property
    def identified(self) -> bool:
        return self.identification_status == "identified"


def identify_logical_document(
    member_names: tuple[str, ...], declared_mime: str | None
) -> LogicalDocumentEvidence | None:
    """Infer only from an observed MIME and exact minimum package member names.

    The caller has already bounded and safely read ``mimetype``.  These are
    structural *names*, not a claim that XML, references or all CRCs are valid.
    Duplicate marker names abstain instead of choosing an arbitrary entry.
    """

    names = set(member_names)
    if declared_mime is not None:
        kind = ODF_MIME_KINDS.get(declared_mime)
        required: tuple[str, ...] = ("mimetype", "content.xml", "META-INF/manifest.xml")
        if declared_mime == "application/epub+zip":
            kind = "epub"
            required = ("mimetype", "META-INF/container.xml")
        if kind is None:
            return LogicalDocumentEvidence(
                declared_mime, None, None, ("mimetype",), "unsupported_declared_mime"
            )
        present = tuple(name for name in required if name in names)
        status = "identified" if len(present) == len(required) else "insufficient_structure"
        if any(member_names.count(name) != 1 for name in present):
            status = "ambiguous_structure"
        if "[Content_Types].xml" in names and names.intersection(
            {
                "word/document.xml",
                "xl/workbook.xml",
                "ppt/presentation.xml",
            }
        ):
            # Competing package signatures are a reviewable contradiction, not
            # permission to pick whichever declaration was encountered first.
            status = "ambiguous_structure"
        return LogicalDocumentEvidence(
            declared_mime,
            kind if status == "identified" else None,
            f".{kind}" if status == "identified" else None,
            present,
            status,
        )

    # OOXML compatibility: names provide an inference, not a MIME declaration.
    if "[Content_Types].xml" in names:
        markers = {
            "word/document.xml": "docx",
            "xl/workbook.xml": "xlsx",
            "ppt/presentation.xml": "pptx",
        }
        present = tuple(name for name in markers if name in names)
        if len(present) == 1:
            marker = present[0]
            if member_names.count(marker) == member_names.count("[Content_Types].xml") == 1:
                kind = markers[marker]
                return LogicalDocumentEvidence(
                    None, kind, f".{kind}", ("[Content_Types].xml", marker), "identified"
                )
    return None


def issue_diagnosis(reason_code: str) -> tuple[str, str]:
    """Return coverage impact and a recovery *possibility*, never permission."""

    if reason_code in {
        "archive_materialization_complete",
        "archive_materialization_unit_preserved",
    }:
        # A successful apply-only manifest is evidence of a separate local
        # stage, not a coverage defect.  Functional packages are intentionally
        # preserved as one unit rather than marked removable.
        return "identification_only", "not_verified"
    if reason_code.startswith("archive_materialization_"):
        return "coverage_limited", "bounded_retry_review"
    if reason_code == "archive_logical_extension_mismatch":
        return "identification_only", "review_identification"
    if reason_code.startswith("archive_logical_"):
        return "identification_incomplete", "review_identification"
    if reason_code.startswith("archive_source_") or reason_code == "archive_corrupt_container":
        return "container_unavailable", "not_verified"
    if reason_code == "archive_encrypted_member":
        return "member_text_unavailable", "credentials_required"
    if reason_code in {
        "archive_member_size_limit",
        "archive_total_uncompressed_limit",
        "archive_member_count_limit",
        "archive_compression_ratio_limit",
        "archive_depth_limit",
        "archive_total_text_limit",
        "archive_text_limit",
    }:
        return "coverage_limited", "bounded_retry_review"
    if reason_code in {
        "archive_unsafe_member_name",
        "archive_special_member",
        "archive_duplicate_member",
        "archive_virtual_path_collision",
        "archive_embedded_duplicate_member",
        "archive_embedded_unsafe_member",
    }:
        return "member_excluded", "manual_review_required"
    if reason_code == "archive_unsupported_compression":
        return "member_text_unavailable", "supported_decoder_required"
    return "member_text_unavailable", "not_verified"


__all__ = (
    "LOGICAL_MEDIA_TYPES",
    "MAX_DECLARED_MIME_BYTES",
    "ODF_KINDS",
    "LogicalDocumentEvidence",
    "identify_logical_document",
    "issue_diagnosis",
)
