"""Entity, context, and generic pattern evidence for document taxonomy."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/document_taxonomy_entities.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import re
from typing import Mapping

from .document_signals import (
    alias_count,
    any_regex_match,
    compiled_regex,
    first_regex_matches,
    fold_signal,
)
from .document_taxonomy_models import (
    ClientSpec,
    OrganizationSpec,
    ProjectSpec,
    ScoredLabel,
)
from .document_taxonomy_vocabulary import (
    _DOCUMENT_SUBTYPE_PATTERNS,
    _EQUIPMENT_PATTERNS,
    _EQUIPMENT_SPECIFICITY,
)
# endregion [01]

# region [02] Implementación


def _managed_top_level_is(path: str, expected: str) -> bool:
    """Read only the managed top-level folder needed for safe migrations."""

    segments = tuple(
        fold_signal(segment) for segment in re.split(r"[\\/]", path) if segment.strip()
    )
    try:
        root_index = segments.index("CONSULTA TECNICA ORGANIZADA")
    except ValueError:
        return False
    category_index = root_index + 1
    return category_index < len(segments) and segments[category_index] == expected


def _technical_reference_evidence(
    topics: tuple[ScoredLabel, ...],
) -> tuple[ScoredLabel, ...]:
    """Classify otherwise unknown but strongly sector-specific consultation material."""

    strong_topic = bool(topics and topics[0].score >= 0.58)
    corroborated_topics = sum(topic.score >= 0.50 for topic in topics) >= 2
    broad_topic_support = sum(topic.score >= 0.42 for topic in topics) >= 3
    if not (strong_topic or corroborated_topics or broad_topic_support):
        return ()
    evidence = tuple(
        f"tema_tecnico:{topic.label};score={topic.score:.2f}" for topic in topics[:4]
    )
    return (ScoredLabel("referencia_tecnica", 0.74, evidence),)


def _document_topic_adjustment(
    topics: tuple[ScoredLabel, ...],
    primary_kind: str,
) -> tuple[ScoredLabel, ...]:
    """Prefer the measured system over incidental test-instrument terminology."""

    if primary_kind != "registro_mediciones":
        return topics
    adjusted = tuple(
        ScoredLabel(
            topic.label,
            round(min(0.97, topic.score + 0.05), 6),
            (*topic.evidence, "contexto:objeto_de_medicion"),
        )
        if topic.label == "puesta_tierra"
        else topic
        for topic in topics
    )
    return tuple(sorted(adjusted, key=lambda item: (-item.score, item.label)))


def _audio_kind_adjustment(
    kinds: tuple[ScoredLabel, ...],
    scopes: Mapping[str, str],
    *,
    managed_path: bool,
) -> tuple[ScoredLabel, ...]:
    """Keep conversations as audio unless their document form is explicit."""

    if not kinds:
        return (
            ScoredLabel(
                "audio_transcrito",
                0.62,
                ("fuente:audio_transcrito;tipo_no_determinado",),
            ),
        )
    explicit_audio_kinds = {
        "entrevista_grabada",
        "instruccion_verbal",
        "reunion_grabada",
    }
    explicit = tuple(item for item in kinds if item.label in explicit_audio_kinds)
    opening = scopes.get("opening", "")
    document_heading = compiled_regex(
        r"^(?:PROCEDIMIENTO|INSTRUCTIVO|MANUAL|CURSO|CAPACITACION)\b"
    ).search(opening)
    strong_document = tuple(
        item
        for item in kinds
        if item.label not in explicit_audio_kinds
        and (
            any("estructura_" in evidence for evidence in item.evidence)
            or document_heading is not None
            or (
                not managed_path
                and any(
                    evidence.startswith(("path:", "title:"))
                    for evidence in item.evidence
                )
            )
        )
    )
    selected = explicit or strong_document
    if selected:
        adjusted = tuple(
            ScoredLabel(
                item.label,
                round(min(0.97, item.score + 0.12), 6),
                (*item.evidence, "fuente:audio_transcrito"),
            )
            for item in selected
        )
        return tuple(sorted(adjusted, key=lambda item: (-item.score, item.label)))

    contextual = tuple(
        ScoredLabel(
            item.label,
            round(min(0.68, item.score), 6),
            (*item.evidence, "contexto_mencionado_en_audio"),
        )
        for item in kinds[:3]
    )
    primary = ScoredLabel(
        "audio_transcrito",
        0.74,
        (f"fuente:audio_transcrito;contexto={kinds[0].label}",),
    )
    return (primary, *contextual)


def _specific_equipment_evidence(
    scopes: Mapping[str, str],
) -> tuple[ScoredLabel, ...]:
    labels = _pattern_evidence(scopes, _EQUIPMENT_PATTERNS, base_score=0.42)
    present = {label.label for label in labels}
    adjusted: list[ScoredLabel] = []
    for label in labels:
        broader = _EQUIPMENT_SPECIFICITY.get(label.label, frozenset())
        matches = broader.intersection(present)
        if not matches:
            adjusted.append(label)
            continue
        adjusted.append(
            ScoredLabel(
                label.label,
                round(min(0.97, label.score + 0.08), 6),
                (
                    *label.evidence,
                    "equipo_especifico_sobre:" + ",".join(sorted(matches)),
                ),
            )
        )
    return tuple(sorted(adjusted, key=lambda item: (-item.score, item.label)))


def _document_subtype_evidence(
    scopes: Mapping[str, str],
    *,
    primary_kind: str,
    primary_authority: str | None,
    activities: tuple[ScoredLabel, ...],
) -> tuple[ScoredLabel, ...]:
    """Add a second document-form level without breaking stable primary kinds."""

    front_scopes = {
        name: value[:1_500] if name == "opening" else value
        for name, value in scopes.items()
        if name in {"path", "title", "opening"}
    }
    candidates = _pattern_evidence(
        front_scopes,
        _DOCUMENT_SUBTYPE_PATTERNS,
        base_score=0.48,
    )
    if primary_kind == "normativa":
        allowed = {
            "codigo",
            "especificacion",
            "guia",
            "metodo_prueba",
            "practica_recomendada",
            "regulacion_obligatoria",
        }
        filtered = [label for label in candidates if label.label in allowed]
        fallback = (
            "regulacion_obligatoria"
            if primary_authority == "NOM"
            else "especificacion"
            if primary_authority in {"CFE", "NETA"}
            else "norma"
        )
    elif primary_kind in {"manual_equipo", "manual_sistema_gestion"}:
        filtered = [label for label in candidates if label.label.startswith("manual_")]
        fallback = (
            "manual_sistema_gestion"
            if primary_kind == "manual_sistema_gestion"
            else "manual_general"
        )
    elif primary_kind == "procedimiento":
        filtered = [
            label for label in candidates if label.label.startswith("procedimiento_")
        ]
        fallback = "procedimiento_general"
        if not filtered and activities:
            activity_to_subtype = {
                "mantenimiento": "procedimiento_mantenimiento",
                "proteccion_ambiental": "procedimiento_ambiental",
                "pruebas_campo": "procedimiento_pruebas",
                "puesta_servicio": "procedimiento_puesta_servicio",
                "recepcion_aceptacion": "procedimiento_recepcion",
                "seguridad": "procedimiento_seguridad",
            }
            inferred = activity_to_subtype.get(activities[0].label)
            if inferred is not None:
                filtered.append(
                    ScoredLabel(
                        inferred,
                        max(0.72, activities[0].score),
                        (*activities[0].evidence, "subtipo:inferido_de_actividad"),
                    )
                )
    elif primary_kind == "especificacion_tecnica":
        filtered = [label for label in candidates if label.label == "especificacion"]
        fallback = "especificacion"
    else:
        return ()

    if not filtered:
        filtered.append(
            ScoredLabel(fallback, 0.64, (f"tipo_principal:{primary_kind}",))
        )
    return tuple(sorted(filtered, key=lambda item: (-item.score, item.label)))


def _organization_evidence(
    scopes: Mapping[str, str],
    specs: tuple[OrganizationSpec, ...],
) -> tuple[ScoredLabel, ...]:
    weights = {
        "path": 0.62,
        "title": 0.74,
        "author": 0.88,
        "metadata": 0.70,
        "opening": 0.62,
        "text": 0.38,
    }
    labels: list[ScoredLabel] = []
    for spec in specs:
        score = 0.0
        evidence: list[str] = []
        for scope, text in scopes.items():
            count = sum(alias_count(text, alias) for alias in spec.aliases)
            if not count:
                continue
            candidate = min(0.94, weights[scope] + 0.05 * min(count - 1, 4))
            if (
                spec.name == "LAPEM"
                and scope in {"title", "opening"}
                and alias_count(text, "LABORATORIO DE PRUEBAS DE EQUIPOS Y MATERIALES")
            ):
                candidate = 0.98
            score = max(score, candidate)
            if len(evidence) < 6:
                evidence.append(f"{scope}:organizacion={spec.name};hits={count}")
        if score:
            labels.append(ScoredLabel(spec.name, round(score, 6), tuple(evidence)))
    labels.sort(key=lambda item: (-item.score, item.label.casefold()))
    return tuple(labels)


def _project_evidence(
    scopes: Mapping[str, str],
    specs: tuple[ProjectSpec, ...],
) -> tuple[ScoredLabel, ...]:
    """Identify the operational project independently from document issuer."""

    weights = {
        "path": 0.86,
        "title": 0.90,
        "author": 0.58,
        "metadata": 0.76,
        "opening": 0.88,
        "text": 0.56,
    }
    labels: list[ScoredLabel] = []
    for spec in specs:
        score = 0.0
        evidence: list[str] = []
        for scope, text in scopes.items():
            count = _project_alias_count(text, spec)
            if not count:
                continue
            score = max(score, min(0.97, weights[scope] + 0.03 * min(count - 1, 3)))
            if len(evidence) < 6:
                evidence.append(
                    f"{scope}:proyecto={spec.name};cliente={spec.client};hits={count}"
                )
        if score:
            labels.append(ScoredLabel(spec.name, round(score, 6), tuple(evidence)))
    return tuple(sorted(labels, key=lambda item: (-item.score, item.label.casefold())))


def _project_alias_count(text: str, spec: ProjectSpec) -> int:
    """Gate ambiguous place names while retaining explicit project designations."""

    counts = {alias: alias_count(text, alias) for alias in spec.aliases}
    if spec.name.casefold() != "malpaso":
        return sum(counts.values())
    ambiguous = sum(
        count
        for alias, count in counts.items()
        if fold_signal(alias) in {"MALPASO", "MAL PASO"}
    )
    total = sum(counts.values())
    if not ambiguous:
        return total
    project_context = compiled_regex(
        r"\b(?:PROYECTO|PROJECT)\b.{0,50}\bMAL\s*PASO\b|"
        r"\b(?:CENTRAL\s+HIDROELECTRICA|HYDROELECTRIC\s+POWER\s+PLANT|"
        r"C\.?\s*H\.?|ANDRITZ|REPOSICION)\b.{0,50}\bMAL\s*PASO\b|"
        r"\bMAL\s*PASO\b.{0,50}\b(?:HCN|ANDRITZ|REPOTENCIACION|"
        r"MODERNIZACION|REPOWERING)\b|\bMALPASO[-\s]*HCN\b"
    ).search(text)
    return total if project_context is not None else total - ambiguous


def _client_evidence(
    scopes: Mapping[str, str],
    clients: tuple[ClientSpec, ...],
    project_specs: tuple[ProjectSpec, ...],
    projects: tuple[ScoredLabel, ...],
) -> tuple[ScoredLabel, ...]:
    """Resolve the account/client without replacing manufacturers or issuers."""

    weights = {
        "path": 0.84,
        "title": 0.90,
        "author": 0.94,
        "metadata": 0.82,
        "opening": 0.82,
        "text": 0.50,
    }
    best: dict[str, ScoredLabel] = {}
    for spec in clients:
        score = 0.0
        evidence: list[str] = []
        for scope, text in scopes.items():
            if scope == "author":
                continue
            count = _client_role_count(text, spec.aliases)
            if not count:
                continue
            score = max(score, min(0.97, weights[scope] + 0.03 * min(count - 1, 3)))
            if len(evidence) < 6:
                evidence.append(f"{scope}:cliente={spec.name};hits={count}")
        if score:
            best[spec.name.casefold()] = ScoredLabel(
                spec.name, round(score, 6), tuple(evidence)
            )

    project_clients = {spec.name.casefold(): spec.client for spec in project_specs}
    canonical_clients = {spec.name.casefold(): spec.name for spec in clients}
    for project in projects:
        client_name = project_clients.get(project.label.casefold())
        if client_name is None:
            continue
        canonical = canonical_clients.get(client_name.casefold(), client_name)
        derived = ScoredLabel(
            canonical,
            round(min(0.97, max(0.92, project.score + 0.08)), 6),
            (
                *project.evidence,
                f"relacion:proyecto={project.label};cliente={canonical}",
            ),
        )
        prior = best.get(canonical.casefold())
        if prior is None or derived.score > prior.score:
            best[canonical.casefold()] = derived
    return tuple(
        sorted(best.values(), key=lambda item: (-item.score, item.label.casefold()))
    )


def _client_role_count(text: str, aliases: tuple[str, ...]) -> int:
    """Count client names only when the surrounding text establishes that role."""

    count = 0
    for alias in aliases:
        if not alias_count(text, alias):
            continue
        expression = re.escape(fold_signal(alias)).replace(r"\ ", r"\s+")
        role_pattern = (
            rf"\b(?:CLIENTE|CUSTOMER|CONSIGNEE|CUENTA|PARA|FOR|"
            rf"ATENCION|DESTINATARIO|CONTRATANTE|SOLICITADO\s+POR|DIRIGIDO\s+A)\b"
            rf".{{0,120}}(?<!\w){expression}(?!\w)|"
            rf"(?<!\w){expression}(?!\w).{{0,100}}"
            rf"\b(?:CLIENTE|CUSTOMER|CONSIGNEE|DESTINATARIO|CONTRATANTE)\b|"
            rf"\b(?:COTIZACION|PROPUESTA|OFERTA|QUOTATION)\b.{{0,100}}"
            rf"(?<!\w){expression}(?!\w)"
        )
        if compiled_regex(role_pattern).search(text) is not None:
            count += 1
    return count


def _workstream_evidence(scopes: Mapping[str, str]) -> tuple[ScoredLabel, ...]:
    """Recognize bounded project work packages that need client-centric routing."""

    front = " ".join(
        scopes.get(scope, "")
        for scope in ("path", "title", "metadata", "opening")
        if scopes.get(scope)
    )
    full = f"{front} {scopes.get('text', '')}".strip()
    if not full:
        return ()
    hcn = compiled_regex(r"\b(?:MALPASO[-\s]*)?HCN[-\s/]?\d{1,3}\b").search(full)
    pressure = compiled_regex(
        r"\b(?:INTERNAL\s+PRESSURE\s+INSPECTION|PRESSURE\s+INSPECTION|"
        r"PRESSURE\s+READING|INSPECCION\s+DE\s+PRESION\s+INTERNA|"
        r"REVISION\s+DE\s+PRESION\s+INTERNA|PERIODO\s+DE\s+PRESURIZACION)\b"
    ).search(full)
    packing = compiled_regex(
        r"\b(?:PACKING\s+LIST|SHIPMENT\s+(?:NO|NUMBER|TYPE)|LISTA\s+DE\s+EMPAQUE|EMBARQUE)\b"
    ).search(full)
    malpaso = compiled_regex(r"\bMAL\s*PASO\b").search(full)
    modernization = compiled_regex(
        r"\b(?:REPOWERING\s+AND\s+MODERNIZATION|REPOTENCIACION|MODERNIZACION)\b"
    ).search(full)
    oil_sample_label = compiled_regex(
        r"\b(?:CEE\s*/\s*CROMATOGRAFIA|CLA\s*/\s*F\.?\s*Q\.?\s*E\.?|"
        r"EGA\s*/\s*PCB'?S?)\b.{0,60}\b(?:JERINGA|FRASCO)\b"
    ).search(full)
    labels: list[ScoredLabel] = []
    if pressure is not None and (hcn is not None or malpaso is not None):
        labels.append(
            ScoredLabel(
                "control_presion_unidades",
                0.98,
                (
                    f"contexto:presion={_clean_identifier(pressure.group(0))}",
                    "contexto:paquete_hcn" if hcn is not None else "contexto:malpaso",
                ),
            )
        )
    if packing is not None and (hcn is not None or malpaso is not None):
        labels.append(
            ScoredLabel(
                "embarques_hcn",
                0.96,
                (
                    f"contexto:embarque={_clean_identifier(packing.group(0))}",
                    "contexto:paquete_hcn" if hcn is not None else "contexto:malpaso",
                ),
            )
        )
    if modernization is not None and malpaso is not None:
        labels.append(
            ScoredLabel(
                "modernizacion_repotenciacion",
                0.88,
                (f"contexto:proyecto={_clean_identifier(modernization.group(0))}",),
            )
        )
    if oil_sample_label is not None and malpaso is not None:
        labels.append(
            ScoredLabel(
                "muestreo_aceite_transformadores",
                0.97,
                (
                    f"contexto:etiqueta_muestra={_clean_identifier(oil_sample_label.group(0))}",
                    "contexto:malpaso",
                ),
            )
        )
    return tuple(sorted(labels, key=lambda item: (-item.score, item.label)))


def _pattern_evidence(
    scopes: Mapping[str, str],
    patterns: Mapping[str, tuple[str, ...]],
    *,
    base_score: float,
) -> tuple[ScoredLabel, ...]:
    scope_bonus = {
        "path": 0.18,
        "title": 0.27,
        "author": 0.08,
        "metadata": 0.12,
        "opening": 0.24,
        "text": 0.0,
    }
    labels: list[ScoredLabel] = []
    for label, rules in patterns.items():
        score = 0.0
        evidence: list[str] = []
        total_signals = 0
        for scope, text in scopes.items():
            if not any_regex_match(rules, text, flags=re.IGNORECASE):
                continue
            scope_hits = 0
            for pattern in rules:
                matches = first_regex_matches(
                    pattern,
                    text,
                    flags=re.IGNORECASE,
                    limit=2,
                )
                if matches:
                    scope_hits += 1
                for match in matches[:2]:
                    if len(evidence) < 6:
                        evidence.append(
                            f"{scope}:regla={_clean_identifier(match.group(0))}"
                        )
            if scope_hits:
                total_signals += scope_hits
                score = max(score, base_score + scope_bonus[scope])
        if total_signals:
            score = min(0.92, score + 0.04 * min(total_signals - 1, 5))
            labels.append(ScoredLabel(label, round(score, 6), tuple(evidence)))
    labels.sort(key=lambda item: (-item.score, item.label))
    return tuple(labels)


def _clean_identifier(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).upper()
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.document_taxonomy_entities")
