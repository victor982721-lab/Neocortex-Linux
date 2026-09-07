"""Bounded role evidence: what a document is, not everything it mentions.

Signals are untrusted content. Labels are proposals and explicit issuer strings
are declarations, not authentication of their author or provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .document_signals import fold_signal
from .document_taxonomy_models import (
    DocumentSignals,
    EntityRoleEvidence,
    ScoredLabel,
    StandardReference,
    TechnicalTaxonomy,
)


@dataclass(frozen=True, slots=True)
class RoleAssessment:
    kinds: tuple[ScoredLabel, ...]
    contradictions: tuple[str, ...] = ()
    outside_role: str | None = None
    outside_evidence: tuple[str, ...] = ()


def _textual_log_source(signals: DocumentSignals) -> bool:
    """Use the Archive producer's MIME, not a member suffix or OCR text.

    Catalog metadata is flattened key/value evidence. Ambiguous duplicate MIME
    keys abstain rather than choosing a declaration embedded in a member name.
    """

    if signals.source_kind == "text":
        return True
    if signals.source_kind != "archive":
        return False
    media_types = re.findall(r"(?:^|\s)media_type=([^\s]+)", signals.metadata)
    content_kinds = re.findall(r"(?:^|\s)content_kind=([^\s]+)", signals.metadata)
    return (
        len(media_types) == 1
        and media_types[0] in {"text/plain", "application/json"}
        and not any(kind in {"image", "pdf"} for kind in content_kinds)
    )


def document_role_assessment(
    signals: DocumentSignals,
    scopes: Mapping[str, str],
    kinds: tuple[ScoredLabel, ...],
) -> RoleAssessment:
    """Require a heading or structural log evidence before overriding a role."""

    opening = signals.leading_text[:4_000]
    path = scopes.get("path", "")
    header = f"{scopes.get('title', '')} {scopes.get('opening', '')[:400]}"
    headings = (scopes.get("title", "").strip(), scopes.get("opening", "")[:400].strip())
    if signals.source_kind == "code":
        metadata = signals.metadata
        evidence = ["source_kind:code"]
        for field in ("language", "artifact_kind"):
            match = re.search(rf"(?:^|\s){field}=([^\s]+)", metadata)
            if match is not None:
                evidence.append(f"metadata:{field}={match.group(1)}")
        role = ScoredLabel("codigo", 1.0, tuple(evidence))
        return RoleAssessment(
            kinds=(role,),
            contradictions=tuple(
                f"role_vs_mention:{item.label}"
                for item in kinds
                if item.label != role.label
            ),
            outside_role=role.label,
            outside_evidence=role.evidence,
        )
    # A mentioned report, standard, or incident does not change the role of a
    # timestamped command transcript. Filename alone is deliberately insufficient.
    timestamp_lines = re.findall(
        r"(?m)^\s*\[?\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+[^\n]{0,180}",
        opening,
    )
    log_fields = re.findall(
        r"(?im)(?:^|\s)(?:INFO|DEBUG|WARN(?:ING)?|ERROR)\b|"
        r"\b(?:exit_code|returncode|stdout|stderr|tool_call|command)\s*[:=]",
        opening,
    )
    log_hint = bool(re.search(r"\b(?:LOG|TRANSCRIPT|CODEX|SYNAPTA)\b", path + " " + header))
    strong_log_structure = len(timestamp_lines) >= 2 and len(log_fields) >= 2
    # A named record may quote diagnostic output. Preserve its existing kind
    # when a real heading identifies a bitacora, never from its filename alone.
    basename = signals.path.replace("\\", "/").rsplit("/", 1)[-1]
    filename_titles = {fold_signal(basename), fold_signal(basename.rsplit(".", 1)[0])}
    declared_title = "" if scopes.get("title", "") in filename_titles else headings[0]
    explicit_role_heading = next(
        (
            heading
            for heading in (declared_title, headings[1])
            if re.match(r"(?:REPORTE|INFORME|CERTIFICADO|BITACORA|REPORT)\b", heading)
        ),
        "",
    )
    if (
        _textual_log_source(signals)
        and strong_log_structure
        and re.match(r"BITACORA\b", explicit_role_heading)
    ):
        bitacora = next((item for item in kinds if item.label == "registro_bitacora"), None)
        if bitacora is not None:
            return RoleAssessment(
                kinds=(
                    ScoredLabel(
                        bitacora.label,
                        bitacora.score,
                        (*bitacora.evidence, "heading:explicit_bitacora_over_quoted_log"),
                    ),
                ),
                contradictions=tuple(
                    f"role_vs_mention:{item.label}"
                    for item in kinds
                    if item.label != bitacora.label
                ),
            )
    if (
        _textual_log_source(signals)
        and not explicit_role_heading
        and (strong_log_structure or (log_hint and len(log_fields) >= 3))
    ):
        role = ScoredLabel(
            "registro_log",
            0.94,
            (
                f"opening:timestamped_records={len(timestamp_lines)}",
                f"opening:log_fields={len(log_fields)}",
            ),
        )
        return RoleAssessment(
            kinds=(role,),
            contradictions=tuple(f"role_vs_mention:{item.label}" for item in kinds[:8]),
            outside_role="registro_log",
            outside_evidence=role.evidence,
        )

    # In particular, an analysis *of* standards is not the source standard.
    # A body mention is not enough: the report phrase must begin its heading.
    report_heading = next(
        (
            match
            for heading in headings
            if (
                match := re.match(
                    r"(?:REPORTE|INFORME|ESTUDIO|ANALISIS)\s+(?:DE\s+|SOBRE\s+)?"
                    r"(?:NORMATIVIDAD|NORMATIVA|CUMPLIMIENTO\s+NORMATIVO|NORMAS)\b",
                    heading,
                )
            )
        ),
        None,
    )
    if report_heading is not None:
        role = ScoredLabel(
            "informe_tecnico",
            0.94,
            (f"heading:document_role={report_heading.group(0).strip()}",),
        )
        return RoleAssessment(
            kinds=(
                role,
                *(item for item in kinds if item.label not in {"normativa", "informe_tecnico"}),
            ),
            contradictions=("cited_standard_not_document_role",),
        )

    incident_heading = next(
        (
            heading
            for heading in headings
            if re.match(
                r"(?:REPORTE|INFORME)\s+(?:DE\s+)?INCIDENTES?\b|INCIDENT\s+REPORT\b",
                fold_signal(heading),
            )
        ),
        None,
    )
    if incident_heading is not None:
        role = ScoredLabel(
            "reporte_incidente",
            0.96,
            (f"heading:document_role={fold_signal(incident_heading).splitlines()[0]}",),
        )
        return RoleAssessment(
            kinds=(role,),
            contradictions=tuple(
                f"role_vs_mention:{item.label}"
                for item in kinds
                if item.label != role.label
            ),
            outside_role=role.label,
            outside_evidence=role.evidence,
        )

    template_heading = next(
        (
            heading
            for heading in headings
            if re.match(r"(?:PLANTILLA|TEMPLATE|FORMULARIO|FORM)\b", fold_signal(heading))
        ),
        None,
    )
    if template_heading is not None and kinds and kinds[0].label in {
        "reporte_fat_sat",
        "reporte_anomalias",
        "informe_tecnico",
        "formato_empresa",
        "formato_inspeccion",
        "lista_verificacion",
    }:
        template = next(
            (
                item
                for item in kinds
                if item.label in {"formato_empresa", "formato_inspeccion", "lista_verificacion"}
            ),
            None,
        )
        role = ScoredLabel(
            template.label if template is not None else "formato_empresa",
            max(0.90, template.score if template is not None else 0.90),
            (
                *(template.evidence if template is not None else ()),
                f"heading:template={fold_signal(template_heading).splitlines()[0]}",
            ),
        )
        return RoleAssessment(
            kinds=(role,),
            contradictions=tuple(
                f"role_vs_mention:{item.label}"
                for item in kinds
                if item.label != role.label
            ),
        )

    personal_role = re.match(
        r"\s*(RECETA\s+DE\s+COCINA|DIARIO\s+PERSONAL|LISTA\s+DE\s+COMPRAS)\b",
        header.strip(),
    )
    if personal_role is not None and (not kinds or kinds[0].label == "otro"):
        return RoleAssessment(
            kinds=kinds,
            outside_role="personal_document",
            outside_evidence=(
                f"heading:outside_technical_taxonomy={personal_role.group(0).strip()}",
            ),
        )
    return RoleAssessment(kinds=kinds)


def entity_role_evidence(
    signals: DocumentSignals,
    taxonomy: TechnicalTaxonomy,
    primary: ScoredLabel,
    authorities: tuple[ScoredLabel, ...],
    organizations: tuple[ScoredLabel, ...],
    standards: tuple[StandardReference, ...],
) -> tuple[EntityRoleEvidence, ...]:
    """Separate cited authorities, mentions and attributed issuer declarations."""

    author = fold_signal(signals.author).strip()
    front = fold_signal(signals.leading_text[:1_000])
    aliases = {item.code: (*item.aliases, item.code) for item in taxonomy.authorities}
    aliases.update({item.name: (*item.aliases, item.name) for item in taxonomy.organizations})
    formal_normative = primary.label == "normativa" and any(
        item.startswith("opening:estructura_normativa=") for item in primary.evidence
    )
    roles: list[EntityRoleEvidence] = []
    seen: set[str] = set()
    for label in (*authorities, *organizations):
        if label.label in seen:
            continue
        seen.add(label.label)
        declared: str | None = None
        for alias in aliases.get(label.label, (label.label,)):
            token = fold_signal(alias).strip()
            if token and author == token:
                declared = f"author:issuer_declaration={label.label}"
                break
            if token and re.search(
                r"\b(?:EMITIDO\s+POR|PUBLICADO\s+POR|ISSUED\s+BY|PUBLISHED\s+BY)\s*[:=-]?\s*"
                + re.escape(token)
                + r"\b",
                front,
            ):
                declared = f"opening:issuer_declaration={label.label}"
                break
        if declared is not None:
            roles.append(EntityRoleEvidence(label.label, "issuer", (declared,), "declaration"))
        elif formal_normative and authorities and label is authorities[0]:
            roles.append(EntityRoleEvidence(label.label, "issuer", primary.evidence, "inference"))
        references = tuple(item.evidence for item in standards if item.authority == label.label)
        if references and not (formal_normative and authorities and label is authorities[0]):
            roles.append(EntityRoleEvidence(label.label, "cited", references, "observation"))
        elif declared is None and not (
            formal_normative and authorities and label is authorities[0]
        ):
            roles.append(
                EntityRoleEvidence(label.label, "mentioned", label.evidence, "observation")
            )
    return tuple(roles)
