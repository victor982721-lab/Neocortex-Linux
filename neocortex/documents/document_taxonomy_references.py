"""Authority and standards-reference evidence for document taxonomy."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/document_taxonomy_references.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import re
from typing import Iterable, Mapping

from .document_signals import (
    alias_count,
    classification_path_signal,
    compiled_regex,
    fold_signal,
)
from .document_taxonomy_entities import _clean_identifier
from .document_taxonomy_models import (
    AuthoritySpec,
    DocumentSignals,
    ScoredLabel,
    StandardReference,
)
from .document_taxonomy_vocabulary import _SHORT_EN_INDUSTRIAL_STANDARDS
# endregion [01]

# region [02] Implementación


def _document_authority_adjustment(
    scopes: Mapping[str, str],
    standards: tuple[StandardReference, ...],
    authorities: tuple[ScoredLabel, ...],
) -> tuple[ScoredLabel, ...]:
    """Prefer the cover issuer over organizations cited in normative references."""

    title = scopes.get("title", "")
    opening = _front_before_reference_section(scopes.get("opening", ""))[:2_000]
    front = f"{title} {opening}".strip()
    if not front:
        return authorities
    form_context = f"{title} {scopes.get('path', '')} {opening[:800]}"
    if compiled_regex(_EXPLICIT_NON_NORMATIVE_FORM_PATTERN).search(form_context):
        return authorities

    selected: str | None = None
    reason = ""
    formal_markers: tuple[tuple[str, str], ...] = (
        ("NOM", r"\bNORMA\s+OFICIAL\s+MEXICANA\b"),
        ("NMX", r"\bNORMA\s+MEXICANA\b"),
        (
            "CFE",
            r"\b(?:ESPECIFICACION|MANUAL|PROCEDIMIENTO|GUIA)\b.{0,120}"
            r"\bCFE(?:\s+[A-Z0-9_-]{4,16})?\b",
        ),
        (
            "IEEE",
            r"\bIEEE\s+(?:STD\.?|STANDARD|GUIDE|RECOMMENDED\s+PRACTICE)\b",
        ),
        (
            "ASTM",
            r"\bDESIGNATION\s*:\s*(?:ASTM\s*)?[A-Z]\s*\d{1,5}\b|"
            r"\bSTANDARD\s+TEST\s+METHOD\b.{0,180}\bASTM\b|"
            r"\bASTM\b.{0,180}\bSTANDARD\s+TEST\s+METHOD\b",
        ),
        ("NETA", r"\b(?:ANSI\s*/\s*)?NETA[-\s](?:ATS|MTS|ECS|ETT|EMW)\b"),
    )
    available = {reference.authority for reference in standards}
    available.update(label.label for label in authorities)
    for authority, pattern in formal_markers:
        if authority in available and compiled_regex(pattern).search(front):
            selected = authority
            reason = "estructura_portada"
            break

    if selected is None and compiled_regex(r"\bINTERNATIONAL\s+STANDARD\b").search(
        front
    ):
        selected = _earliest_reference_authority(
            front,
            standards,
            frozenset({"IEC", "ISO", "ISO/IEC", "IEC/IEEE"}),
            max_position=900,
        )
        if selected is not None:
            reason = "norma_internacional_portada"

    if selected is None and title:
        selected = _earliest_reference_authority(
            title,
            standards,
            frozenset(reference.authority for reference in standards),
            max_position=160,
        )
        if selected is not None:
            reason = "identificador_titulo"

    if selected is None:
        selected = _earliest_reference_authority(
            opening[:700],
            standards,
            frozenset(reference.authority for reference in standards),
            max_position=180,
        )
        if selected is not None:
            reason = "identificador_inicio"

    if selected is None:
        return authorities

    adjusted: list[ScoredLabel] = []
    found = False
    for label in authorities:
        if label.label != selected:
            adjusted.append(label)
            continue
        found = True
        adjusted.append(
            ScoredLabel(
                label.label,
                0.99,
                (*label.evidence, f"portada:autoridad_emisora={selected};{reason}"),
            )
        )
    if not found:
        adjusted.append(
            ScoredLabel(
                selected,
                0.99,
                (f"portada:autoridad_emisora={selected};{reason}",),
            )
        )
    return tuple(
        sorted(adjusted, key=lambda item: (-item.score, item.label.casefold()))
    )


def _earliest_reference_authority(
    text: str,
    standards: tuple[StandardReference, ...],
    allowed: frozenset[str],
    *,
    max_position: int,
) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for reference in standards:
        if reference.authority not in allowed:
            continue
        identifier = fold_signal(reference.identifier)
        position = text.find(identifier)
        if 0 <= position <= max_position:
            candidates.append((position, -len(identifier), reference.authority))
    return min(candidates)[2] if candidates else None


def _authority_evidence(
    scopes: Mapping[str, str],
    specs: tuple[AuthoritySpec, ...],
    *,
    raw_scopes: Mapping[str, str],
    managed_path: bool,
) -> tuple[tuple[StandardReference, ...], tuple[ScoredLabel, ...]]:
    references: dict[tuple[str, str], StandardReference] = {}
    labels: list[ScoredLabel] = []
    weights = {
        "path": 0.68,
        "title": 0.84,
        "author": 0.72,
        "metadata": 0.58,
        "opening": 0.72,
        "text": 0.50,
    }
    for spec in specs:
        score = 0.0
        evidence: list[str] = []
        for scope, text in scopes.items():
            for pattern in spec.identifier_patterns:
                for match in compiled_regex(pattern, re.IGNORECASE).finditer(text):
                    if not _plausible_authority_identifier(
                        spec.code,
                        scope=scope,
                        folded_text=text,
                        raw_text=raw_scopes.get(scope, ""),
                        match=match,
                        managed_path=managed_path,
                    ):
                        continue
                    identifier = _normalized_reference_identifier(
                        spec.code, match.group(0)
                    )
                    key = (spec.code, identifier.casefold())
                    references.setdefault(
                        key,
                        StandardReference(
                            spec.code, identifier, f"{scope}:{identifier}"
                        ),
                    )
                    score = max(score, weights[scope] + 0.12)
                    if len(evidence) < 8:
                        evidence.append(f"{scope}:identificador={identifier}")
            alias_hits = sum(alias_count(text, alias) for alias in spec.aliases)
            if alias_hits:
                score = max(
                    score, min(0.76, weights[scope] + 0.03 * min(alias_hits, 4))
                )
                if len(evidence) < 8:
                    evidence.append(f"{scope}:autoridad={spec.code};hits={alias_hits}")
        if score:
            labels.append(
                ScoredLabel(spec.code, round(min(score, 0.99), 6), tuple(evidence))
            )
    inferred_astm = _implicit_astm_cover_reference(scopes)
    if inferred_astm is not None:
        key = (inferred_astm.authority, inferred_astm.identifier.casefold())
        references.setdefault(key, inferred_astm)
        astm = next((label for label in labels if label.label == "ASTM"), None)
        inferred_evidence = (f"opening:identificador={inferred_astm.identifier}",)
        if astm is None:
            labels.append(ScoredLabel("ASTM", 0.99, inferred_evidence))
        elif astm.score < 0.99:
            labels[labels.index(astm)] = ScoredLabel(
                "ASTM", 0.99, (*astm.evidence, *inferred_evidence)
            )
    labels.sort(key=lambda item: (-item.score, item.label.casefold()))
    ordered_refs = _consolidate_references(references.values())
    return ordered_refs, tuple(labels)


def _plausible_authority_identifier(
    authority: str,
    *,
    scope: str,
    folded_text: str,
    raw_text: str,
    match: re.Match[str],
    managed_path: bool,
) -> bool:
    """Reject ambiguous identifiers without weakening explicit standards."""

    identifier = match.group(0)
    if authority == "NMX":
        compact = fold_signal(identifier).replace(" ", "-")
        segments = tuple(part for part in re.split(r"[-/]", compact) if part)
        if len(segments) < 3 or not segments[1][0].isalpha():
            return False
        if not any(
            any(character.isdigit() for character in part) for part in segments[2:]
        ):
            return False
        if any(len(part) > 10 for part in segments[1:]):
            return False
        return True
    if authority in {"IEC", "ISO", "ISO/IEC", "IEC/IEEE"}:
        number_match = re.search(r"\b(\d{3,5})\b", identifier)
        if number_match is None:
            return False
        number = int(number_match.group(1))
        local = folded_text[max(0, match.start() - 100) : match.end() + 100]
        formal_context = compiled_regex(
            r"\b(?:INTERNATIONAL\s+STANDARD|NORMA\s+INTERNACIONAL|"
            r"STANDARD\s+(?:NUMBER|NO\.?))\b"
        ).search(local)
        has_dated_edition = re.search(r"[-:](?:19|20)\d{2}\b", identifier) is not None
        if 1900 <= number <= 2099 and formal_context is None and not has_dated_edition:
            return False
        return True
    if authority != "EN":
        return True
    tail = identifier[2:].strip()
    exact_tail = re.escape(tail).replace(r"\ ", r"\s+")
    if compiled_regex(rf"(?<!\w)EN\s+{exact_tail}(?!\w)").search(raw_text) is None:
        return False
    number_match = re.match(r"\d{3,6}", tail)
    if number_match is None:
        return False
    number_text = number_match.group(0)
    if number_text.startswith("0"):
        return False
    number = int(number_text)
    local = folded_text[max(0, match.start() - 80) : match.end() + 80]
    formal_context = compiled_regex(
        r"\b(?:EUROPEAN\s+STANDARD|NORMA\s+EUROPEA|"
        r"(?:BS|DIN|UNE)\s+EN|STANDARD\s+EN)\b"
    ).search(local)
    if 1900 <= number <= 2099 and formal_context is None:
        return False
    has_dated_edition = re.search(r":(?:19|20)\d{2}\b", tail) is not None
    if (
        number < 1_000
        and number not in _SHORT_EN_INDUSTRIAL_STANDARDS
        and not has_dated_edition
        and formal_context is None
    ):
        return False
    if (
        scope == "path"
        and managed_path
        and formal_context is None
        and number < 5_000
        and not has_dated_edition
    ):
        return False
    return True


_EXPLICIT_NON_NORMATIVE_FORM_PATTERN = (
    r"\b(?:CONTRATO|COTIZACION|OFERTA\s+(?:COMERCIAL|TECNICA|REF)|"
    r"PROPUESTA\s+(?:COMERCIAL|TECNICA)|PROPOSAL\s+NO|"
    r"MANUAL\s+DEL\s+PARTICIPANTE|PROCEDIMIENTO\s+PARA|"
    r"INFORME\s+EGA|"
    r"(?:INFORME|REPORTE)\s+DE\s+(?:ACTIVIDADES|INSPECCION|ENSAYO|PRUEBAS|"
    r"RESULTADOS|[^A-Z0-9]{0,6}NO\s+CONFORMIDAD)|"
    r"(?:INFORME|REPORTE)\s+(?:DE\s+)?TRAZABILIDAD\s+NORMATIVA|"
    r"CERTIFICATE\s+OF\s+(?:COMPLIANCE|CONFORMITY|QUALITY)|"
    r"CONSTANCIA\s+DE\s+ACEPTACION\s+DE\s+PROTOTIPO|"
    r"FACTURACION\s+ELECTRONICA|"
    r"CUESTIONARIO\s+TECNICO|CARACTERISTICAS\s+PARTICULARES|"
    r"DESCRIPCION\s+TECNICA|"
    r"(?:ESTUDIO|RESUMEN|ANALISIS|COMENTARIOS?|INTERPRETACION)\s+"
    r"(?:DE|SOBRE)\s+(?:LA\s+)?NORMAS?|"
    r"PACKING\s+LIST|LISTA\s+DE\s+EMPAQUE)\b"
)


def _front_before_reference_section(opening: str) -> str:
    """Exclude a references block from issuer and primary-type decisions."""

    marker = compiled_regex(
        r"\b(?:REFERENCIAS\s+DOCUMENTALES|DOCUMENTARY\s+REFERENCES|"
        r"REFERENCED\s+DOCUMENTS|NORMAS\s+(?:Y\s+)?REFERENCIAS)\b"
    ).search(opening)
    return opening if marker is None else opening[: marker.start()]


def _reference_position(text: str, reference: StandardReference) -> int:
    identifier = fold_signal(reference.identifier)
    position = text.find(identifier)
    if reference.authority == "IEEE":
        candidates = [position] if position >= 0 else []
        dated = re.fullmatch(r"(.+?)([-:]\d{4})", identifier)
        if dated is not None:
            expression = (
                re.escape(dated.group(1)) + r"(?:\s*TM)?\s*" + re.escape(dated.group(2))
            )
            match = compiled_regex(expression).search(text)
            if match is not None:
                candidates.append(match.start())
        if candidates:
            return min(candidates)
    if reference.authority == "IEC":
        candidates = [position] if position >= 0 else []
        dated = re.fullmatch(r"(.+?):((?:19|20)\d{2})", identifier)
        if dated is not None:
            expression = (
                re.escape(dated.group(1))
                + r"\s+EDITION\s+\d+(?:\.\d+)?\s+"
                + re.escape(dated.group(2))
                + r"(?:-\d{2})?\b"
            )
            match = compiled_regex(expression).search(text)
            if match is not None:
                candidates.append(match.start())
        if candidates:
            return min(candidates)
    if position >= 0:
        return position
    if reference.authority == "CFE":
        common_ocr = re.fullmatch(r"CFE\s+([A-Z])([0O]{4})(-\d{2})", identifier)
        if common_ocr is not None:
            expression = (
                rf"\bCFE\s+{common_ocr.group(1)}[0O]{{4}}"
                rf"{re.escape(common_ocr.group(3))}\b"
            )
            match = compiled_regex(expression).search(text)
            if match is not None:
                return match.start()
        som = re.fullmatch(r"(SOM|M)-(\d{3,5})(?:-([A-Z0-9]{2,8}))?", identifier)
        if som is not None:
            suffix = rf"(?:[-\s]+{re.escape(som.group(3))})?" if som.group(3) else ""
            match = compiled_regex(
                rf"\b{som.group(1)}[-\s]+{som.group(2)}{suffix}\b"
            ).search(text)
            return -1 if match is None else match.start()
    if reference.authority == "ASTM" and identifier.startswith("ASTM "):
        designation = identifier.removeprefix("ASTM ")
        match = compiled_regex(
            rf"\bDESIGNATION\s*:\s*(?:ASTM\s*)?{re.escape(designation)}\b"
        ).search(text)
        return -1 if match is None else match.start()
    return -1


def _naming_reference_rank(
    reference: StandardReference,
    *,
    signals: DocumentSignals,
    primary_authority: str | None,
    managed_path: bool,
) -> tuple[int, bool, int, bool, int, str, str]:
    """Rank the cover identifier before standards cited later in the document."""

    title = fold_signal(signals.title)
    opening = fold_signal(_front_before_reference_section(signals.leading_text[:4_000]))
    path = fold_signal(classification_path_signal(signals.path))
    title_position = _reference_position(title, reference)
    opening_position = _reference_position(opening, reference)
    path_position = _reference_position(path, reference)
    formal_cover_position = _formal_cover_reference_position(opening, reference)
    superseded = _reference_is_superseded(opening, reference)
    positions = (
        (0, formal_cover_position),
        (1, title_position),
        (1, opening_position if 0 <= opening_position <= 240 else -1),
        (2, path_position),
        (3, opening_position if opening_position > 240 else -1),
    )
    direct = tuple((scope, position) for scope, position in positions if position >= 0)
    if direct:
        scope, position = min(direct)
    else:
        evidence_scope = reference.evidence.partition(":")[0]
        scope = {"title": 0, "opening": 1, "path": 3 if managed_path else 2}.get(
            evidence_scope,
            4,
        )
        position = 1_000_000
    return (
        reference.authority != primary_authority,
        superseded,
        scope,
        not _reference_has_explicit_edition(reference.identifier),
        position,
        reference.authority,
        reference.identifier,
    )


def _reference_is_superseded(opening: str, reference: StandardReference) -> bool:
    """Penalize an edition named only as the superseded or revised document."""

    position = _reference_position(opening, reference)
    if position < 0:
        return False
    context = opening[max(0, position - 100) : position]
    return (
        compiled_regex(
            r"(?:CANCELA\s+Y\s+REEMPLAZA\s+A|REVISION\s+OF|"
            r"REVISA\s+Y\s+SUSTITUYE\s+A|SUSTITUYE\s+A|SUPERSEDES?)"
        ).search(context)
        is not None
    )


def _formal_cover_reference_position(
    opening: str,
    reference: StandardReference,
) -> int:
    """Locate identifiers explicitly coupled to an issuer's cover marker."""

    position = _reference_position(opening, reference)
    if position < 0:
        return -1
    identifier = fold_signal(reference.identifier)
    if reference.authority == "CFE" and identifier.startswith("CFE "):
        code = identifier.removeprefix("CFE ")
        code_expression = re.escape(code).replace("0", "[0O]")
        match = compiled_regex(
            rf"\b(?:ESPECIFICACION|NORMA)\s+CFE\s+{code_expression}\b"
        ).search(opening)
        return -1 if match is None or position > 240 else position
    markers = {
        "ASTM": r"\b(?:DESIGNATION|STANDARD\s+TEST\s+METHOD)\b",
        "IEC": r"\b(?:INTERNATIONAL\s+STANDARD|�\s*IEC)\b",
        "ISO": r"\b(?:INTERNATIONAL\s+STANDARD|�\s*ISO)\b",
        "ISO/IEC": r"\bINTERNATIONAL\s+STANDARD\b",
        "IEEE": r"\bIEEE\s+(?:STD\.?|STANDARD|GUIDE)\b",
        "NETA": r"\b(?:ANSI\s*/?\s*)?NETA\s+(?:MTS|ATS|ECS|ETT)\b",
        "NMX": r"\bNORMA\s+MEXICANA\b",
        "NOM": r"\bNORMA\s+OFICIAL\s+MEXICANA\b",
    }
    marker = markers.get(reference.authority)
    if marker is None:
        return -1
    for match in compiled_regex(marker).finditer(opening):
        if match.start() > 1_200:
            break
        if abs(match.start() - position) <= 180:
            return position
    return -1


def _normalized_reference_identifier(authority: str, value: str) -> str:
    identifier = _clean_identifier(value)
    if authority == "IEC":
        cover_edition = re.fullmatch(
            r"(IEC(?:\s+(?:TR|TS|PAS))?\s+\d{3,5}(?:[.\-]\d+)*)\s+"
            r"EDITION\s+\d+(?:\.\d+)?\s+((?:19|20)\d{2})(?:-\d{2})?",
            identifier,
        )
        if cover_edition is not None:
            return f"{cover_edition.group(1)}:{cover_edition.group(2)}"
    if authority == "IEEE":
        identifier = re.sub(r"(?i)\s*TM\s*(?=[-:]\d{4}\b)", "", identifier)
    if authority == "CFE":
        som = re.fullmatch(
            r"(SOM|M)[-\s]+(\d{3,5})(?:[-\s]+([A-Z0-9]{2,8}))?",
            identifier,
        )
        if som is not None:
            suffix = f"-{som.group(3)}" if som.group(3) else ""
            return f"{som.group(1)}-{som.group(2)}{suffix}"
        common_ocr = re.fullmatch(r"CFE\s+([A-Z])([0O]{4})(-\d{2})", identifier)
        if common_ocr is not None:
            return (
                f"CFE {common_ocr.group(1)}"
                f"{common_ocr.group(2).replace('O', '0')}"
                f"{common_ocr.group(3)}"
            )
    return identifier


def _implicit_astm_cover_reference(
    scopes: Mapping[str, str],
) -> StandardReference | None:
    """Recover ASTM designations whose cover omits the ASTM prefix."""

    opening = scopes.get("opening", "")[:2_000]
    designation = compiled_regex(
        r"\bDESIGNATION\s*:\s*(?:ASTM\s*)?([A-Z]\s*\d{1,5})\b"
    ).search(opening)
    if designation is None:
        return None
    if not compiled_regex(
        r"\b(?:STANDARD\s+TEST\s+METHOD\s+FOR|"
        r"THIS\s+STANDARD\s+IS\s+ISSUED\s+UNDER\s+THE\s+FIXED\s+DESIGNATION|"
        r"ASTM\s+(?:INTERNATIONAL|COMMITTEE|STANDARDS?))\b"
    ).search(opening):
        return None
    compact = re.sub(r"\s+", "", designation.group(1))
    identifier = f"ASTM {compact}"
    return StandardReference("ASTM", identifier, f"opening:DESIGNATION {compact}")


def _consolidate_references(
    references: Iterable[StandardReference],
) -> tuple[StandardReference, ...]:
    """Prefer the most specific version of one identifier seen in many scopes."""

    selected: list[StandardReference] = []
    ordered = sorted(
        references,
        key=lambda item: (
            item.authority,
            not _reference_has_explicit_edition(item.identifier),
            item.identifier,
        ),
    )
    for reference in ordered:
        family = _reference_family_key(reference.identifier)
        if not _reference_has_explicit_edition(reference.identifier) and any(
            prior.authority == reference.authority
            and _reference_has_explicit_edition(prior.identifier)
            and _reference_family_key(prior.identifier) == family
            for prior in selected
        ):
            continue
        selected.append(reference)
    return tuple(sorted(selected, key=lambda item: (item.authority, item.identifier)))


def _reference_has_explicit_edition(value: str) -> bool:
    return re.search(r"[-:](?:19|20)\d{2}\b", value) is not None


def _reference_family_key(value: str) -> str:
    without_edition = re.sub(r"[-:](?:19|20)\d{2}\b", "", value)
    without_std = re.sub(r"\bSTD\.?\b", "", without_edition, flags=re.IGNORECASE)
    return re.sub(r"[^A-Z0-9.]", "", without_std.upper())
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.document_taxonomy_references")
