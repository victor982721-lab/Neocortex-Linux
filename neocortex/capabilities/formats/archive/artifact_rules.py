"""Pure, bounded archive-member artifact rules.

This module owns only central-directory/member evidence.  It never opens a
source, extracts bytes, hashes a file, or performs a physical effect.  Both
archive intake and the workflow ArtifactPolicy use this single decision owner.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from neocortex.platform.logical_filename import LogicalFilename


ArchiveDisposition = Literal["keep", "block"]
_FIXTURE_COMPONENTS = frozenset({"fixture", "fixtures", "test_data", "testdata", "binaryfixtures"})
_LICENSE_COMPONENTS = frozenset({"license", "licenses", "licence", "licences"})
_ARCHIVE_RUNTIME_ROOTS = frozenset(
    {"bin", "lib", "include", "runtime", "sdk", "jre", "node_modules", "site-packages", "toolchains", "platforms"}
)
_ARCHIVE_NATIVE_SUFFIXES = (".so", ".dll", ".dylib", ".exe", ".class", ".wasm", ".a")


@dataclass(frozen=True, slots=True)
class ArchiveRuleDecision:
    path: str
    disposition: ArchiveDisposition
    rule_id: str
    evidence: Mapping[str, object]


def _decision(
    path: str,
    disposition: ArchiveDisposition,
    rule_id: str,
    evidence: Mapping[str, object],
) -> ArchiveRuleDecision:
    return ArchiveRuleDecision(path, disposition, rule_id, dict(evidence))


def classify_archive_members(
    path: str | Path,
    members: Iterable[str],
    *,
    member_signatures: Mapping[str, str] | None = None,
    container_kind: str | None = None,
    max_members: int = 4096,
) -> ArchiveRuleDecision:
    """Classify bounded archive evidence without granting effect authority."""

    source = str(path)
    if type(max_members) is not int or not 1 <= max_members <= 20_000:
        raise ValueError("max_members must be between 1 and 20000")
    if container_kind not in {None, "apk", "jar"}:
        return _decision(
            source,
            "keep",
            "keep.archive-container-schema-unconfirmed",
            {"container_kind": container_kind},
        )
    logical = LogicalFilename.parse(source)
    parts = tuple(part.casefold() for part in Path(source).parts)
    if logical.logical_extension in {".whl", ".wheel"} or any(
        part in _FIXTURE_COMPONENTS | _LICENSE_COMPONENTS for part in parts
    ):
        return _decision(
            source,
            "keep",
            "protect.archive-preserved-material",
            {"protection": "source_tree"},
        )

    selected: list[str] = []
    for raw in members:
        if len(selected) >= max_members:
            return _decision(
                source, "keep", "keep.archive-member-limit", {"max_members": max_members}
            )
        if not isinstance(raw, str):
            return _decision(
                source,
                "keep",
                "keep.archive-member-set-unconfirmed",
                {"verification": "member_not_text"},
            )
        name = raw.replace("\\", "/").strip("/")
        if (
            not name
            or len(name) > 512
            or "\x00" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
        ):
            return _decision(
                source,
                "keep",
                "keep.archive-member-set-unconfirmed",
                {"verification": "member_name_invalid"},
            )
        selected.append(name.casefold())
    names = tuple(selected)
    name_set = set(names)
    if len(name_set) != len(names):
        return _decision(
            source,
            "keep",
            "keep.archive-member-set-unconfirmed",
            {"verification": "duplicate_member_names"},
        )

    if member_signatures is not None and not isinstance(member_signatures, Mapping):
        return _decision(
            source,
            "keep",
            "keep.archive-signatures-unconfirmed",
            {"verification": "signature_schema_invalid"},
        )
    signatures: dict[str, str] = {}
    for index, (raw_name, raw_value) in enumerate((member_signatures or {}).items()):
        if index >= max_members:
            return _decision(
                source, "keep", "keep.archive-signatures-unconfirmed", {"verification": "signature_limit"}
            )
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            return _decision(
                source,
                "keep",
                "keep.archive-signatures-unconfirmed",
                {"verification": "signature_schema_invalid"},
            )
        signature_name = raw_name.replace("\\", "/").strip("/").casefold()
        signature_value = raw_value.casefold()
        if signature_name not in name_set:
            return _decision(
                source,
                "keep",
                "keep.archive-signatures-unbound",
                {"verification": "signature_member_not_observed"},
            )
        if signature_value not in {"elf", "pe"} or signature_name in signatures:
            return _decision(
                source,
                "keep",
                "keep.archive-signatures-unconfirmed",
                {"verification": "signature_kind_or_key_invalid"},
            )
        signatures[signature_name] = signature_value

    preserved_members = tuple(
        name for name in names
        if name.endswith((".whl", ".wheel", ".license", ".licence"))
        or any(part in _FIXTURE_COMPONENTS | _LICENSE_COMPONENTS for part in name.split("/"))
    )
    if preserved_members:
        return _decision(
            source,
            "keep",
            "protect.archive-preserved-material",
            {"preserved_members": len(preserved_members)},
        )

    document_members = tuple(
        name for name in names
        if name.endswith((".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"))
        or (
            name.endswith((".txt", ".md", ".csv"))
            and any(token in name.rsplit("/", 1)[-1] for token in ("report", "informe", "manual", "document"))
        )
    )
    if document_members:
        return _decision(
            source,
            "keep",
            "keep.archive-documentary-content",
            {"document_members": len(document_members)},
        )

    package_markers = tuple(
        marker for marker in ("androidmanifest.xml", "meta-inf/manifest.mf", "classes.dex") if marker in name_set
    )
    if (
        container_kind in {"apk", "jar"} and len(package_markers) >= 2
    ) or (
        "androidmanifest.xml" in name_set and "classes.dex" in name_set
    ) or (
        "meta-inf/manifest.mf" in name_set and any(name.endswith(".class") for name in names)
    ):
        return _decision(
            source,
            "keep",
            "artifact.archive.software-package.v1",
            {
                "package_markers": package_markers,
                "physical_effect": "deferred_to_source_proof_stage",
            },
        )

    native_members = tuple(name for name in names if name.endswith(_ARCHIVE_NATIVE_SUFFIXES))
    if native_members and not set(native_members).issubset(signatures):
        return _decision(
            source,
            "keep",
            "keep.archive-signatures-incomplete",
            {"native_members": len(native_members), "strong_members": len(signatures)},
        )
    runtime_roots = tuple(
        root for root in sorted(_ARCHIVE_RUNTIME_ROOTS)
        if any(name == root or name.startswith(root + "/") for name in names)
    )
    strong_members = tuple(name for name, signature in signatures.items() if signature in {"elf", "pe"})
    if strong_members and (
        (len(runtime_roots) >= 2 and len(native_members) >= 2)
        or any(root in runtime_roots for root in ("runtime", "sdk", "jre", "toolchains"))
        or bool(runtime_roots)
    ):
        return _decision(
            source,
            "keep",
            "artifact.archive.runtime-structure.v1",
            {
                "runtime_roots": runtime_roots[:12],
                "native_members": len(native_members),
                "strong_members": len(strong_members),
                "physical_effect": "deferred_to_source_proof_stage",
            },
        )
    return _decision(
        source,
        "keep",
        "keep.archive-not-demonstrable-runtime",
        {"runtime_roots": runtime_roots[:12], "strong_members": len(strong_members)},
    )


__all__ = ["ArchiveRuleDecision", "classify_archive_members"]
