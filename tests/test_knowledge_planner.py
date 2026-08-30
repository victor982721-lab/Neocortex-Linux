"""Deterministic query planning without an LLM dependency."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_planner.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from dataclasses import replace

import pytest

from neocortex.knowledge.knowledge_planner import (
    KnowledgePlan,
    KnowledgeQuery,
    RetrievalMode,
    RetrievalStep,
    _knowledge_plan_identifier,
    plan_knowledge_query,
)
# endregion [01]

# region [02] Implementación


def _semantic_steps(plan: KnowledgePlan) -> tuple[tuple[str, bool], ...]:
    return tuple(
        (step.ranking_name, step.required)
        for step in plan.steps
        if step.channel == "semantic"
    )


def _identifier_for(
    plan: KnowledgePlan,
    *,
    steps: tuple[RetrievalStep, ...] | None = None,
) -> str:
    return _knowledge_plan_identifier(
        normalized_query=plan.normalized_query,
        retrieval_mode=plan.retrieval_mode,
        intents=plan.intents,
        exact_terms=plan.exact_terms,
        source_kinds=plan.source_kinds,
        formats=plan.formats,
        project=plan.project,
        date_from=plan.date_from,
        date_to=plan.date_to,
        include_history=plan.include_history,
        limit=plan.limit,
        max_per_resource=plan.max_per_resource,
        min_section_distance=plan.min_section_distance,
        max_vectors=plan.max_vectors,
        steps=plan.steps if steps is None else steps,
        notices=plan.notices,
    )


def test_planner_combines_exact_lexical_semantic_and_structural_signals() -> None:
    query = KnowledgeQuery(
        text=r"C:\Corpus\Área #1\control.py serial SN-2048 control.validate",
        retrieval_mode=RetrievalMode.EVIDENCE,
        source_kinds=("code", "pdf", "code"),
        project="Subestación Norte",
        limit=12,
        max_per_resource=3,
    )

    first = plan_knowledge_query(query)
    second = plan_knowledge_query(query)

    assert first == second
    assert first.plan_id == second.plan_id
    assert first.plan_id.startswith("knowledge-plan-v2:")
    assert first.source_kinds == ("code", "pdf")
    assert _semantic_steps(first) == (("semantic_text", True),)
    assert {step.channel for step in first.steps} >= {
        "exact",
        "lexical",
        "semantic",
        "structural_code",
        "catalog",
    }
    assert "path" in first.intents
    assert "identifier" in first.intents
    assert "symbol" in first.intents
    assert r"C:\Corpus\Área #1\control.py" in first.exact_terms
    assert any("SN-2048" in value for value in first.exact_terms)
    assert first.to_dict()["schema_version"] == 1


def test_discovery_v3_declares_optional_title_without_changing_evidence_v2() -> None:
    discovery = plan_knowledge_query(
        KnowledgeQuery("transformador", retrieval_mode=RetrievalMode.DISCOVERY)
    )
    evidence = plan_knowledge_query(
        KnowledgeQuery("transformador", retrieval_mode=RetrievalMode.EVIDENCE)
    )

    assert discovery.plan_id.startswith("knowledge-plan-v3:")
    assert [
        (step.channel, step.ranking_name, step.required)
        for step in discovery.steps
        if step.ranking_name in {"semantic_text", "semantic_title"}
    ] == [
        ("semantic", "semantic_text", True),
        ("semantic_discovery", "semantic_title", False),
    ]
    assert evidence.plan_id.startswith("knowledge-plan-v2:")
    assert all(step.ranking_name != "semantic_title" for step in evidence.steps)


def test_v2_rejects_title_and_v3_requires_its_canonical_optional_step() -> None:
    evidence = plan_knowledge_query(KnowledgeQuery("transformador"))
    title = RetrievalStep(
        "semantic_discovery",
        "semantic_title",
        "fixture title prior",
        20,
        False,
    )
    with pytest.raises(
        ValueError,
        match="Knowledge plan v2 contains an unsupported retrieval step",
    ):
        replace(evidence, steps=(*evidence.steps, title))

    discovery = plan_knowledge_query(
        KnowledgeQuery("transformador", retrieval_mode=RetrievalMode.DISCOVERY)
    )
    without_title = tuple(
        step for step in discovery.steps if step.ranking_name != "semantic_title"
    )
    with pytest.raises(
        ValueError,
        match="Knowledge plan v3 discovery must contain one semantic title step",
    ):
        replace(discovery, steps=without_title)


def test_standalone_exact_identifier_routes_to_published_catalog() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("IEC-61850"))
    steps = {step.channel: step for step in plan.steps}

    assert "IEC-61850" in plan.exact_terms
    assert steps.keys() >= {"exact", "catalog"}
    assert steps["exact"].required
    assert not steps["catalog"].required


@pytest.mark.parametrize(
    "path",
    (
        r"C:\Corpus\Subestaciones\control.pdf",
        "C:/docs/a.pdf",
        "/docs/a.pdf",
        "./docs/a.pdf",
        "../docs/a.pdf",
        "docs/a.pdf",
        r"C:\Corpus\Área #1\module\control.validate.pdf",
        r"\\server\share\a.pdf",
    ),
)
def test_path_spans_are_exact_inventory_cues_not_code_cues(path: str) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(f"Busca {path}, compáralo"))
    channels = {step.channel for step in plan.steps}

    assert plan.exact_terms == (path,)
    assert {"exact", "catalog"} <= channels
    assert "structural_code" not in channels
    assert "symbol" not in plan.intents


@pytest.mark.parametrize(
    "text",
    (
        "protection/control equipment",
        "input/output signal",
        "and/or condition",
        "voltage/current transformer",
    ),
)
def test_natural_slash_phrases_are_not_relative_paths(text: str) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(text))

    assert plan.exact_terms == ()
    assert "path" not in plan.intents
    assert "exact" not in {step.channel for step in plan.steps}


def test_extensionless_relative_path_uses_conservative_root_grammar() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("src/module"))

    assert plan.exact_terms == ("src/module",)
    assert "path" in plan.intents


@pytest.mark.parametrize("terminal", (".", ":", "]"))
def test_terminal_prose_punctuation_is_not_part_of_an_unquoted_path(
    terminal: str,
) -> None:
    path = r"C:\Corpus\control.pdf"
    plan = plan_knowledge_query(KnowledgeQuery(f"Busca {path}{terminal}"))

    assert plan.exact_terms == (path,)


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ('"A%_# report.pdf"', "A%_# report.pdf"),
        ("A%_# report.pdf", "A%_# report.pdf"),
        ("weekly inspection report.pdf", "weekly inspection report.pdf"),
    ),
)
def test_full_file_names_preserve_safe_spaces_and_punctuation(
    text: str,
    expected: str,
) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(text))

    assert plan.exact_terms == (expected,)
    assert "name" in plan.intents
    assert "symbol" not in plan.intents
    assert "structural_code" not in {step.channel for step in plan.steps}


def test_file_name_leader_does_not_swallow_serial_or_symbol_cues() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery("find serial SN-2048 symbol control.validate")
    )

    assert plan.exact_terms == ("serial SN-2048", "control.validate")
    assert "symbol" in plan.intents
    assert "structural_code" in {step.channel for step in plan.steps}


def test_arbitrary_file_extensions_are_names_without_code_evidence() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("manual.dwg archive.zip"))

    assert plan.exact_terms == ("manual.dwg", "archive.zip")
    assert "name" in plan.intents
    assert "symbol" not in plan.intents
    assert "structural_code" not in {step.channel for step in plan.steps}


def test_exact_terms_preserve_surface_order_across_grammars() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "C:/docs/a.py SN-2048 control.validate",
            source_kinds=("code",),
        )
    )

    assert plan.exact_terms == ("C:/docs/a.py", "SN-2048", "control.validate")
    assert "symbol" in plan.intents


@pytest.mark.parametrize("serial", ("SN-2048", "S/N 2048", "serial:ABC-9"))
def test_strict_serial_grammar_does_not_conflict_with_paths(serial: str) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(serial))

    assert plan.exact_terms == (serial,)
    assert "path" not in plan.intents


@pytest.mark.parametrize(
    "text", ("snapshot validation", "snmp.client", "serial_port.open")
)
def test_serial_prefix_substrings_are_not_serial_cues(text: str) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(text))

    assert "path" not in plan.intents
    assert "symbol" not in plan.intents
    assert "structural_code" not in {step.channel for step in plan.steps}


def test_exact_term_limit_is_enforced_before_retrieval() -> None:
    sixty_four = " ".join(f"SN-{index:04d}" for index in range(64))
    sixty_five = " ".join(f"SN-{index:04d}" for index in range(65))

    assert len(plan_knowledge_query(KnowledgeQuery(sixty_four)).exact_terms) == 64
    with pytest.raises(ValueError, match="more than 64 exact terms"):
        plan_knowledge_query(KnowledgeQuery(sixty_five))


def test_planner_records_temporal_relational_and_explicit_history() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery(
            text="¿Qué función importa y depende del módulo protección en 2025?",
            include_history=True,
            source_kinds=("code",),
            retrieval_mode=RetrievalMode.DISCOVERY,
        )
    )
    steps = {step.channel: step for step in plan.steps}

    assert "relational" in plan.intents
    assert "temporal" in plan.intents
    assert steps.keys() >= {
        "relational",
        "temporal",
        "structural_code",
    }
    assert plan.include_history
    assert not steps["lexical"].required
    assert steps["structural_code"].required
    assert steps["relational"].required
    assert steps["temporal"].required


def test_code_local_calls_and_imports_do_not_require_cross_owner_relations() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "¿Qué función llama e importa control.validate?",
            source_kinds=("code",),
        )
    )
    steps = {step.channel: step for step in plan.steps}

    assert "structural_code" in steps
    assert steps["structural_code"].required
    assert not steps["lexical"].required
    assert "relational" not in steps


def test_code_only_bare_identifier_does_not_plan_catalog() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "definition calculate_breaker",
            source_kinds=("code",),
        )
    )
    steps = {step.channel: step for step in plan.steps}

    assert steps["structural_code"].required
    assert not steps["lexical"].required
    assert _semantic_steps(plan) == (("semantic_text", True),)
    assert "catalog" not in steps


@pytest.mark.parametrize("code_format", ("py", "rs", "rust", "typescript"))
def test_code_format_routes_bare_identifier_to_structural_search(
    code_format: str,
) -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery("calculate_breaker", formats=(code_format,))
    )
    steps = {step.channel: step for step in plan.steps}

    assert steps["structural_code"].required
    assert not steps["lexical"].required
    assert _semantic_steps(plan) == (("semantic_text", True),)
    assert "catalog" not in steps


def test_reference_word_routes_bare_symbol_to_structural_search() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("KnowledgeSnapshot create references"))
    steps = {step.channel: step for step in plan.steps}

    assert steps["structural_code"].required


def test_project_membership_word_is_a_cross_owner_relation_cue() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("a qué proyecto pertenece este archivo"))
    steps = {step.channel: step for step in plan.steps}

    assert steps["relational"].required


def test_explicit_catalog_filters_make_metadata_retrieval_required() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery("protección diferencial", formats=("pdf",))
    )
    steps = {step.channel: step for step in plan.steps}

    assert steps["catalog"].required


@pytest.mark.parametrize(
    "office_format",
    ("docx", "xlsx", "pptx", "odt", "ods", "odp"),
)
def test_office_formats_require_catalog_and_owner_fts(office_format: str) -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery("protección diferencial", formats=(office_format,))
    )
    steps = {step.channel: step for step in plan.steps}

    assert steps["catalog"].required
    assert steps["lexical"].required
    assert _semantic_steps(plan) == (("semantic_text", True),)


def test_broad_multimodal_query_requires_lexical_and_semantic_coverage() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado de la subestación"))
    steps = {step.channel: step for step in plan.steps}

    assert steps["lexical"].required
    assert _semantic_steps(plan) == (
        ("semantic_text", True),
        ("semantic_image", True),
    )


def test_image_query_requires_image_semantics_not_document_fts() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery("estado del interruptor", source_kinds=("image",))
    )
    steps = {step.channel: step for step in plan.steps}

    assert not steps["lexical"].required
    assert _semantic_steps(plan) == (("semantic_image", True),)


def test_image_ocr_query_maps_to_text_semantics() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery("folio de validación", source_kinds=("image_ocr",))
    )
    steps = {step.channel: step for step in plan.steps}

    assert not steps["lexical"].required
    assert _semantic_steps(plan) == (("semantic_text", True),)


@pytest.mark.parametrize("source_kind", ("pdf", "audio"))
def test_text_owner_queries_require_text_semantics(source_kind: str) -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery("protección diferencial", source_kinds=(source_kind,))
    )
    steps = {step.channel: step for step in plan.steps}

    assert steps["lexical"].required
    assert _semantic_steps(plan) == (("semantic_text", True),)


def test_mixed_document_and_image_query_requires_each_semantic_modality() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "protección diferencial",
            source_kinds=("pdf", "image"),
        )
    )
    steps = {step.channel: step for step in plan.steps}

    assert steps["lexical"].required
    assert _semantic_steps(plan) == (
        ("semantic_text", True),
        ("semantic_image", True),
    )


@pytest.mark.parametrize(
    ("formats", "expected"),
    (
        (("pdf",), (("semantic_text", True),)),
        (("jpg",), (("semantic_image", True),)),
        (
            ("pdf", "jpg"),
            (("semantic_text", True), ("semantic_image", True)),
        ),
    ),
)
def test_format_scope_materializes_each_semantic_modality(
    formats: tuple[str, ...],
    expected: tuple[tuple[str, bool], ...],
) -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo", formats=formats))

    assert _semantic_steps(plan) == expected


@pytest.mark.parametrize(
    "text",
    (
        "corriente nominal 2025 A",
        "corriente nominal 2025A",
        "tensión nominal 2025 kV",
        "tensión nominal 2025V",
        "frecuencia nominal 2025 Hz",
        "longitud nominal 2025mm",
        "identificador técnico 6180",
        "referencia técnica 1899",
        "referencia técnica 2101",
        "modelo 2025",
    ),
)
def test_technical_values_and_implausible_years_are_not_temporal(text: str) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(text))

    assert "temporal" not in plan.intents
    assert "temporal" not in {step.channel for step in plan.steps}


@pytest.mark.parametrize(
    "identifier",
    (
        "SN-2048",
        "IEC-2025",
        "deadbeef2025cafe",
    ),
)
def test_year_shaped_substrings_in_exact_identifiers_are_not_temporal(
    identifier: str,
) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(identifier))

    assert identifier in plan.exact_terms
    assert "temporal" not in plan.intents
    assert "temporal" not in {step.channel for step in plan.steps}


@pytest.mark.parametrize("year", (1900, 2025, 2100))
def test_plausible_unqualified_years_are_temporal(year: int) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(f"inspección realizada en {year}"))

    assert "temporal" in plan.intents
    assert "temporal" in {step.channel for step in plan.steps}


@pytest.mark.parametrize(
    "text",
    (
        "procedimiento vigente de pruebas",
        "current protection procedure",
    ),
)
def test_current_vigency_uses_default_current_view_not_history(text: str) -> None:
    plan = plan_knowledge_query(KnowledgeQuery(text))

    assert "temporal" not in plan.intents
    assert "temporal" not in {step.channel for step in plan.steps}


@pytest.mark.parametrize(
    "kwargs",
    (
        {"text": ""},
        {"text": " "},
        {"text": "x" * 4097},
        {"text": "ok", "limit": 0},
        {"text": "ok", "limit": 1001},
        {"text": "ok", "max_per_resource": 0},
        {"text": "ok", "min_section_distance": -1},
        {"text": "ok", "max_vectors": 0},
        {"text": 123},
        {"text": "ok", "source_kinds": ("x" * 257,)},
        {"text": "ok", "source_kinds": tuple(f"s{index}" for index in range(65))},
        {"text": "ok", "formats": tuple("x" * 256 for _ in range(17))},
        {"text": "ok", "project": "x" * 1025},
        {"text": "ok", "source_kinds": (1,)},
    ),
)
def test_query_rejects_unbounded_or_invalid_requests(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        KnowledgeQuery(**kwargs)  # type: ignore[arg-type]


def test_query_rejects_untyped_enum_and_boolean_values_at_the_boundary() -> None:
    with pytest.raises(ValueError, match="RetrievalMode"):
        KnowledgeQuery("ok", retrieval_mode="evidence")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="include_history must be a bool"):
        KnowledgeQuery("ok", include_history=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"channel": 1}, "channel must be a string"),
        ({"ranking_name": None}, "ranking_name must be a string"),
        ({"reason": " "}, "reason cannot be blank"),
        ({"candidate_limit": True}, "candidate limit"),
        ({"candidate_limit": "3"}, "candidate limit"),
        ({"candidate_limit": 0}, "candidate limit"),
        ({"candidate_limit": 1_001}, "candidate limit"),
        ({"required": 1}, "required must be a bool"),
    ),
)
def test_retrieval_step_rejects_invalid_boundary_values(
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "channel": "semantic",
        "ranking_name": "semantic_text",
        "reason": "bounded fixture",
        "candidate_limit": 3,
        "required": True,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        RetrievalStep(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"plan_id": 1}, "plan_id must be a string"),
        ({"normalized_query": " "}, "normalized_query cannot be blank"),
        ({"retrieval_mode": "evidence"}, "RetrievalMode"),
        ({"intents": ["semantic"]}, "intents must be a tuple"),
        ({"exact_terms": (1,)}, "exact_terms must contain only strings"),
        ({"source_kinds": ("PDF",)}, "canonical lowercase"),
        ({"project": " "}, "project cannot be blank"),
        ({"date_from": "2026-01-02", "date_to": "2026-01-01"}, "after"),
        ({"include_history": 1}, "include_history must be a bool"),
        ({"limit": True}, "limit must be between"),
        ({"max_vectors": 0}, "max_vectors must be between"),
        ({"steps": []}, "steps must be a tuple"),
        ({"steps": (object(),)}, "only RetrievalStep"),
    ),
)
def test_knowledge_plan_rejects_invalid_boundary_values(
    changes: dict[str, object],
    message: str,
) -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery("protección diferencial", source_kinds=("pdf",))
    )

    with pytest.raises(ValueError, match=message):
        replace(plan, **changes)  # type: ignore[arg-type]


def test_v2_plan_rejects_missing_or_duplicate_lexical_step() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo"))
    lexical = next(step for step in plan.steps if step.channel == "lexical")
    without_lexical = tuple(step for step in plan.steps if step.channel != "lexical")

    with pytest.raises(ValueError) as missing:
        replace(plan, steps=without_lexical)
    assert str(missing.value) == (
        "Knowledge plan v2 must contain exactly one lexical retrieval step"
    )

    with pytest.raises(ValueError) as duplicate:
        replace(plan, steps=(*plan.steps, lexical))
    assert str(duplicate.value) == (
        "Knowledge plan v2 must contain exactly one lexical retrieval step"
    )


def test_v2_plan_rejects_unknown_or_missing_semantic_rankings() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo"))
    text_step = next(
        step for step in plan.steps if step.ranking_name == "semantic_text"
    )
    image_step = next(
        step for step in plan.steps if step.ranking_name == "semantic_image"
    )
    unknown = replace(text_step, ranking_name="semantic_unknown")
    unknown_steps = tuple(unknown if step is text_step else step for step in plan.steps)

    with pytest.raises(ValueError) as unsupported:
        replace(plan, steps=unknown_steps)
    assert str(unsupported.value) == (
        "Knowledge plan v2 contains an unsupported retrieval step"
    )

    without_image = tuple(step for step in plan.steps if step is not image_step)
    with pytest.raises(ValueError) as missing:
        replace(plan, steps=without_image)
    assert str(missing.value) == (
        "Knowledge plan v2 semantic rankings do not match its retrieval scope"
    )

    with pytest.raises(ValueError) as duplicate:
        replace(plan, steps=(*plan.steps, text_step))
    assert str(duplicate.value) == (
        "Knowledge plan v2 semantic rankings cannot contain duplicates"
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"limit": 21},
        {"max_per_resource": 4},
        {"max_vectors": 400_000},
        {"notices": ("fixture notice",)},
    ),
)
def test_v2_plan_rejects_stale_identifier_after_payload_change(
    changes: dict[str, object],
) -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo"))

    with pytest.raises(ValueError) as captured:
        replace(plan, **changes)  # type: ignore[arg-type]
    assert str(captured.value) == (
        "Knowledge plan v2 plan_id does not match its canonical payload"
    )


def test_v2_plan_rejects_stale_identifier_after_step_change() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo"))
    lexical = next(step for step in plan.steps if step.channel == "lexical")
    changed = replace(lexical, reason="changed lexical reason")
    changed_steps = tuple(changed if step is lexical else step for step in plan.steps)

    with pytest.raises(ValueError) as captured:
        replace(plan, steps=changed_steps)
    assert str(captured.value) == (
        "Knowledge plan v2 plan_id does not match its canonical payload"
    )


def test_v2_plan_rejects_hash_coherent_noncanonical_step_payload() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo"))
    text_step = next(
        step for step in plan.steps if step.ranking_name == "semantic_text"
    )
    changed = replace(
        text_step,
        candidate_limit=text_step.candidate_limit + 1,
    )
    changed_steps = tuple(changed if step is text_step else step for step in plan.steps)
    changed_identifier = _identifier_for(plan, steps=changed_steps)

    with pytest.raises(ValueError) as captured:
        replace(plan, steps=changed_steps, plan_id=changed_identifier)
    assert str(captured.value) == (
        "Knowledge plan v2 steps do not match canonical executable topology"
    )


@pytest.mark.parametrize(
    "plan_id",
    (
        "knowledge-plan-v2:fixture",
        "knowledge-plan-v2:" + ("A" * 32),
        "knowledge-plan-v2:" + ("0" * 31),
    ),
)
def test_v2_plan_rejects_malformed_identifier_with_controlled_error(
    plan_id: str,
) -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo"))

    with pytest.raises(ValueError) as captured:
        replace(plan, plan_id=plan_id)
    assert str(captured.value) == (
        "Knowledge plan v2 plan_id must contain a lowercase XXH3-128 digest"
    )


def test_legacy_plan_identifier_keeps_fixture_construction_compatibility() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo"))

    legacy = replace(
        plan,
        plan_id="knowledge-plan-v1:fixture",
        steps=(),
        limit=999,
    )

    assert legacy.plan_id == "knowledge-plan-v1:fixture"
    assert legacy.steps == ()
    assert legacy.limit == 999


def test_generated_v2_identifier_uses_the_shared_identity_contract() -> None:
    plan = plan_knowledge_query(KnowledgeQuery("estado del equipo"))

    assert plan.plan_id == _identifier_for(plan)


def test_plan_json_is_stable_for_unicode_filters() -> None:
    plan = plan_knowledge_query(
        KnowledgeQuery(
            text="transformador código IEC-61850",
            formats=("PDF", "pdf", "docx"),
            project="Área eléctrica",
            date_from="2025-01-01",
            date_to="2026-01-01",
        )
    )
    expected_json = (
        '{"date_from":"2025-01-01","date_to":"2026-01-01",'
        '"exact_terms":["IEC-61850"],"formats":["pdf","docx"],'
        '"include_history":false,"intents":["identifier","lexical",'
        '"semantic","filtered","temporal"],"kind":"knowledge_query_plan",'
        '"limit":20,"max_per_resource":3,"max_vectors":500000,'
        '"min_section_distance":128,"normalized_query":"transformador código '
        'IEC-61850","plan_id":"knowledge-plan-v2:'
        '2d1f8e4ab95ce9e8c93af8a83f03de3d","project":"Área eléctrica",'
        '"retrieval_mode":"evidence","schema_version":1,"source_kinds":[],'
        '"steps":[{"candidate_limit":60,"channel":"exact",'
        '"ranking_name":"exact_identifiers","reason":"query contains exact path, '
        'identifier, hash, serial or symbol syntax","required":true},'
        '{"candidate_limit":60,"channel":"lexical","ranking_name":"owner_fts",'
        '"reason":"exact lexical evidence is available from owner FTS indexes",'
        '"required":true},{"candidate_limit":60,"channel":"semantic",'
        '"ranking_name":"semantic_text","reason":"semantic text retrieval covers '
        'compatible text and OCR evidence","required":true},'
        '{"candidate_limit":60,"channel":"catalog",'
        '"ranking_name":"catalog_metadata","reason":"exact identifiers or explicit '
        'filters require owner metadata","required":true},'
        '{"candidate_limit":60,"channel":"temporal",'
        '"ranking_name":"published_history","reason":"query requests history, '
        'vigency or a temporal boundary","required":true}]}'
    )

    assert plan.formats == ("pdf", "docx")
    assert plan.plan_id == ("knowledge-plan-v2:2d1f8e4ab95ce9e8c93af8a83f03de3d")
    assert plan.to_json().encode("utf-8") == expected_json.encode("utf-8")
    assert (
        plan_knowledge_query(
            KnowledgeQuery(
                text="transformador código IEC-61850",
                formats=("PDF", "pdf", "docx"),
                project="Área eléctrica",
                date_from="2025-01-01",
                date_to="2026-01-01",
            )
        ).to_json()
        == expected_json
    )


# endregion [02]
