# NeoCortex — clasificación vigente del subsistema `code`

> Inventario comparativo levantado el 30 de agosto de 2026. Los módulos de la
> plataforma anterior se clasifican aquí por su función, no por la cantidad de
> líneas que tenían. Los documentos y receipts históricos no describen el diseño
> vigente.

## Regla

- **A — producto:** código que sigue siendo útil para el usuario de NeoCortex.
- **B — desarrollo:** controles o utilidades que viven fuera del runtime.
- **C — eliminado:** infraestructura redundante de autoanálisis, review,
  experimentos o agregación de validadores.
- **D — transición:** compatibilidad o datos históricos que se desacoplan sin
  borrar filas a ciegas.

Cada módulo del árbol anterior aparece una sola vez en las tablas siguientes.

## Línea base y resultado

| Métrica | Antes | Ahora |
|---|---:|---:|
| Archivos bajo `neocortex/code` | 85 | 18 |
| Archivos Python bajo `neocortex/code` | 84 | 18 |
| Líneas | 88,558 | 10,745 |
| Bytes bajo `neocortex/code` | 3,466,933 | 392,743 |

La reducción física observada es de **67 archivos**, **77,813 líneas** y
**3,074,190 bytes**, sin retirar la ingestión ni la búsqueda de código.

## A — Capacidad productiva conservada

| Módulo original | Ubicación vigente | Responsabilidad |
|---|---|---|
| `__init__.py` | `neocortex/code/__init__.py` | paquete import-light |
| `code_analyzer_common.py` | `code/ingestion/code_analyzer_common.py` | tipos y utilidades de análisis de contenido |
| `code_analyzers.py` | `code/ingestion/code_analyzers.py` | registro lazy de analizadores de lenguajes |
| `code_candidate_scope.py` | `code/ingestion/code_candidate_scope.py` | límites de proyectos y candidatos |
| `code_contracts.py` | `code/code_contracts.py` | contratos de artefactos, símbolos, consultas y resultados |
| `code_detection.py` | `code/ingestion/code_detection.py` | detección de lenguaje y decodificación |
| `code_generic.py` | `code/ingestion/code_generic.py` | representación textual genérica |
| `code_projects.py` | `code/ingestion/code_projects.py` | proyectos, manifiestos y reconstrucción |
| `code_python.py` | `code/ingestion/code_python.py` | símbolos, referencias y dependencias Python |
| `code_retention.py` | `code/code_retention.py` | retención acotada de runs Code; legacy sólo lectura |
| `code_route.py` | `code/code_route.py` | ingesta incremental y publicación |
| `code_rust.py` | `code/ingestion/code_rust.py` | representación léxica Rust |
| `code_schema.py` | `code/code_schema.py` | esquema, migraciones y conexión del owner Code |
| `code_search.py` | `code/search/code_search.py` | búsqueda textual y estructural |
| `code_semantic_links.py` | `code/search/code_semantic_links.py` | enlaces Code–Semantic |
| `code_state.py` | `code/code_state.py` | persistencia de versiones, símbolos y relaciones |
| *(nuevo)* | `code/ingestion/__init__.py` | frontera física de ingestión |
| *(nuevo)* | `code/search/__init__.py` | frontera física de búsqueda |

A no importa `tools`, `tests` ni proveedores de calidad, y no ejecuta el código
que recibe como corpus.

## B — Desarrollo fuera del runtime

| Elemento | Destino | Motivo |
|---|---|---|
| `pip_bootstrap.py` | `tools/pip_bootstrap.py` | preparación explícita de entornos de desarrollo |
| invariantes de imports y ciclos | `tests/architecture/test_boundaries.py` | pruebas directas, reproducibles y sin infraestructura productiva |

No se conserva un archivo que coordine estos controles. Ruff, Pyright/Mypy,
Semgrep, pytest y cualquier otra herramienta se ejecutan directamente cuando el
cambio lo necesita.

## C — Infraestructura eliminada

Los siguientes módulos sólo duplicaban validadores especializados, review,
experimentos o analítica del propio repositorio y fueron retirados junto con sus
pruebas y consumidores:

```text
code_analysis_epistemics.py       code_analysis_query.py
code_analyzer_calibration.py     code_analyzer_effectiveness.py
code_architecture_analysis.py    code_architecture_contracts.py
code_architecture_questions.py   code_assurance_analysis.py
code_capability_reachability_analysis.py
code_change_evolution_analysis.py
code_change_validation.py        code_class_surface_analysis.py
code_coverage_analysis.py        code_engineering_analytics.py
code_experiment_executor.py      code_experiment_planner.py
code_experiment_store.py         code_interface_surface_analysis.py
code_invariant_assurance_analysis.py
code_invariant_contracts.py      code_knowledge_asset_health_analysis.py
code_knowledge_pdf_asset_health_analysis.py
code_publication_diff.py         code_question_resolver.py
code_retention_analysis.py       code_review.py
code_review_actionability.py     code_review_eligibility.py
code_review_epistemics.py        code_review_models.py
code_review_serialization.py     code_review_task_analysis.py
code_review_work_packages.py     code_route_capability_analysis.py
code_security_dependency_questions.py
code_state_interaction_analysis.py
code_state_projection_analysis.py
code_state_topology_analysis.py  code_storage_analysis.py
code_supply_chain_analysis.py    code_technical_verification.py
code_unused_analysis.py          external_architecture_providers.py
external_architecture_worker.py  external_deep_coverage.py
external_deep_coverage_worker.py external_dependency_hygiene.py
external_git_history.py          external_mutation_cosmic_ray.py
external_mutation_cosmic_ray_worker.py
external_semgrep_invariants.py   external_supply_chain_audit.py
external_unused_vulture.py       external_unused_vulture_worker.py
validation_supply.py
```

## D — Compatibilidad y datos históricos

| Elemento original | Tratamiento vigente |
|---|---|
| `code_external_evidence.py` | productor eliminado; filas antiguas no reciben nuevos escritores |
| `external_evidence_models.py` | lector eliminado del runtime; datos antiguos quedan fuera de las consultas productivas |
| `external_evidence_providers.py` | proveedores retirados; no se contactan servicios externos |
| `external_evidence_store.py` | store retirado; no se publican evidencias nuevas |
| `code_validation_public_review.py` | contrato retirado, sin sustituto productivo |
| `code_validation_receipts.py` | receipts antiguos sólo se conservan como evidencia externa/histórica |
| `code_validation_resources.py` | frontera de recursos retirada del producto |
| `contracts/__init__.py` | paquete de targets retirado |
| `contracts/target_projection.py` | proyección reemplazada por pruebas directas |
| `contracts/target_registry.py` | registro obsoleto retirado |
| `logical_owner_contracts.py` | registro de ownership del autoanálisis retirado |

`code_schema.py` mantiene definiciones de tablas antiguas únicamente para poder
reconocer y validar bases existentes de forma conservadora. Las bases nuevas no
crean esas tablas y `code_retention.py` nunca las borra.

## Criterio final

Si NeoCortex estuviera terminado y no se volviera a modificar su propio código,
A seguiría siendo útil para consultar el código del usuario. B sólo ayudaría a
mantener el proyecto, C no aportaba valor diferencial frente a herramientas
especializadas y D sólo protege la lectura conservadora de historia existente.
