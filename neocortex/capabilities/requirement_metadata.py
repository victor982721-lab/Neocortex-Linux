"""Read dependency constraints from the package being inspected, without engines.

Installed wheels use their own ``Requires-Dist`` metadata.  Source extractions
use the adjacent pyproject, not an unrelated installed copy of NeoCortex.  No
dependency versions are duplicated here and no package is imported to inspect
its version.  ``packaging`` is imported only when parsing a requirement so even
an incomplete base installation can explain why compatibility is unverified.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RequirementCompatibility:
    requirement: str | None
    compatible: bool | None
    reason: str | None = None
    source: str | None = None


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declarations(*, extra: str | None, owner_distribution: str) -> tuple[tuple[str, ...], str]:
    if owner_distribution == "neocortex-framework":
        source = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if source.is_file():
            with source.open("rb") as stream:
                project = tomllib.load(stream).get("project", {})
            if project.get("name") == owner_distribution:
                requirements = tuple(project.get("dependencies", ()))
                if extra is not None:
                    requirements += tuple(project.get("optional-dependencies", {}).get(extra, ()))
                return requirements, "source_pyproject"
    return tuple(metadata.requires(owner_distribution) or ()), "installed_metadata"


def inspect_requirement_compatibility(
    distribution: str,
    observed_version: str | None,
    *,
    extra: str | None = None,
    owner_distribution: str = "neocortex-framework",
) -> RequirementCompatibility:
    """Compare metadata to active PEP 508 requirements, never import a backend.

    ``owner_distribution`` permits checking a declared transitive requirement
    (for example ONNX Runtime from FastEmbed metadata) without copying its
    constraints into this module.  Unknown or malformed metadata fails closed,
    but remains distinct from an incompatible installed version.
    """

    try:
        declarations, source = _declarations(extra=extra, owner_distribution=owner_distribution)
    except (metadata.PackageNotFoundError, OSError, ValueError, TypeError):
        return RequirementCompatibility(None, None, "requirement_metadata_unavailable")
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.specifiers import SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return RequirementCompatibility(
            None, None, "requirement_parser_unavailable:packaging", source
        )

    matches = []
    try:
        for declaration in declarations:
            requirement = Requirement(declaration)
            if _canonical_name(requirement.name) != _canonical_name(distribution):
                continue
            if requirement.marker is not None and not requirement.marker.evaluate(
                {"extra": extra or ""}
            ):
                continue
            matches.append(requirement)
    except (InvalidRequirement, ValueError, TypeError):
        return RequirementCompatibility(None, None, "requirement_metadata_invalid", source)

    if not matches:
        return RequirementCompatibility(None, None, "requirement_not_declared", source)
    specifier = SpecifierSet(",".join(str(requirement.specifier) for requirement in matches))
    requirement_text = f"{matches[0].name}{specifier}"
    if any(requirement.url is not None for requirement in matches):
        return RequirementCompatibility(
            requirement_text, None, "direct_url_compatibility_not_checked", source
        )
    if observed_version is None:
        return RequirementCompatibility(requirement_text, None, source=source)
    try:
        version = Version(observed_version)
    except InvalidVersion:
        return RequirementCompatibility(
            requirement_text, None, "distribution_version_invalid", source
        )
    compatible = version in specifier
    return RequirementCompatibility(
        requirement_text,
        compatible,
        None if compatible else "distribution_version_incompatible",
        source,
    )
