# Curación del corpus — checkpoint operativo WIP

**Estado:** PAUSADO / CURRENT_REVIEW, no apto todavía para promoción.
**Pendiente:** `NEO-CORPUS-20260928`.
**Rama:** `codex/curacion-corpus-wip-20260928`.
**Base de main:** `67524f92807ff2218fd86546a74d67f96a5e081a`.
**Fecha operativa:** 2026-09-28, America/Mexico_City.

Víctor solicitó guardar el trabajo existente en una rama y dejarlo pendiente.
El checkpoint conserva código, pruebas, fixtures sintéticas, configuración de
empaquetado y documentación en curso. No integra a main, no instala una release
y no acredita cierre del rediseño. No continuar implementación, pruebas largas,
publicación de main ni instalación sin una reanudación humana expresa.

## Contrato que debe conservarse

1. Linux/Kubuntu y CPython 3.13; sin GitHub Actions, modelos nuevos, servicios
   remotos ni mutación del corpus real. La evidencia de esta ronda es sintética.
2. Mantener `Inventory → ZIP Intake → successor Inventory → Identify`.
   Archive conserva su recursión ZIP interna. No reemplazarla con un loop global
   de ZIP ni mover el intake inicial después de Identify.
3. Todos los descendientes físicos ZIP/EML vuelven al gate barato por deltas.
   Identify/Normalize preceden a la Hard Redlist por extensión: un PDF llamado
   `.dll` debe sobrevivir. Un intento de rename no equivale a un efecto aplicado.
4. Antes de Dedupe/rutas/modelos debe cerrarse la admisión. Conservar la
   deduplicación existente `size → SHA-256 completo → comparación exacta`.
   No hashear basura primero ni usar muestras UI truncadas como cobertura.
5. Las rutas extraen una vez. Fast Curation consume derivados acotados y comparte
   infraestructura de embeddings, no el índice completo de Full Semantic.
   Contenido y contexto/path original permanecen separados.
6. Una sola política calibrada decide: `CLASSIFIED` vigente puede organizarse
   bajo `Corpus_ordenado`; abstenciones físicas van a
   `Sin_clasificar/_MIME/<major>/<subtype>`, con desconocidos en
   `application/octet-stream`. No tercera zona física de revisión/general.
7. Reutilizar no-replace, receipts, recovery, cache sync y COW por lotes.
   Preservar origen/contexto y las tres carpetas estructurales. Full Semantic
   sigue fuera del writer Framework y después del layout/rebinding; el verificador
   final no debe aprobar owners CURRENT obsoletos ni efectos pendientes.

## Superficies guardadas

| Frente | Owners principales |
|---|---|
| Nombres e Identify | `platform/logical_filename.py`, `platform/identification_probe.py`, `platform/content_types.py`, `workflow/actions/action_identify.py` |
| Política barata y efectos | `workflow/actions/artifact_policy.py`, `action_artifact_stage.py`, `capabilities/formats/archive/artifact_rules.py`, `safety/artifact_content_proof.py`, `safety/artifact_read_lease.py`, KIO/mutations |
| Admisión de productores | `deduplication/inventory/curation_admission.py`, `runtime/orchestration/curation_pipeline.py`, intake ZIP/EML y receipts Framework |
| Fast Curation | `documents/document_semantic_representation.py`, `curation_sources.py`, `curation_state.py`, `semantic/fast_curation_*.py`, `runtime/orchestration/fast_curation_lifecycle.py` |
| Decisión/layout | `documents/semantic_curation_gate.py`, `document_kind_destinations.py`, organización existente, `residual_materialization.py`, `document_cache_sync.py`, `document_retirement.py` |
| Cierre y observabilidad | `runtime/orchestration/final_corpus_layout.py`, `corpus_verification*.py`, `corpus_metrics.py`, orquestador/finalización y reporting CLI |

Catalog schema 13 añade decisiones y caché de curación en el mismo owner, con
roles query/passage y bindings/versiones. El mapping físico controlado se separó
del planner para evitar un ciclo de imports. La taxonomía heurística previa se
conserva como evidencia auxiliar; se retiró el gate heurístico paralelo escrito
al inicio de esta ronda.

## Evidencia disponible y límites

Expediente local canónico:
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-28-rediseno-curacion-01a0e956/`.
No se adjuntan al repositorio bases de QA, caches de modelos ni logs extensos.

- `regression3.log`: 76 passed, 2 skips y 11 subtests, sobre un snapshot anterior.
- `layout6.log`: 55 passed. Incluye ZIP→EML→ZIP, 263 adjuntos, preview sin efectos,
  replay ZIP, nombres GNU, protección ante Normalize bloqueado y retirement.
- `regression-full-discovery1.log`: **ejecución incompleta**, 665 passed,
  3 failed, 5 skips y 2 subtests antes de interrumpir una espera de recursos.
  Los tres fallos eran contratos de ciclos/fields/stages; hay correcciones
  posteriores, pero no se repitió la suite general sobre este checkpoint.
- La espera se reprodujo en `catalog-hang.log`: admisión del GRC ante presión
  I/O del host, no un error atribuido sin evidencia a CPU o SQLite.
  `catalog-idle-fixture.log` aprobó un caso con entrada I/O sintética explícita.
  Esa simulación no prueba capacidad ni rendimiento del host y no sustituye
  pruebas de presión/cancelación ni autoriza relajar límites productivos.
- `c03-effect-boundary-review.md`: revisión independiente de la frontera de
  efectos. Sus resultados no quedan cerrados por la mera presencia de un fix.
- Los focos reportados por subfrentes no son un gate integral del snapshot final.
  En particular, parte de QA de organización se ejecutó desde el checkout;
  debe repetirse con fuente, HOME/XDG y owners privados.

La observación de guardado comprobó sintaxis AST de 109 Python modificados sin
errores y `git diff --check`. **No ejecutó de nuevo pruebas ni aceptó conducta.**

## Bloqueadores y siguiente orden de trabajo

### 1. Seguridad de ArtifactPolicy/claim — prioridad alta

La revisión reprodujo modificación same-inode fuera de segmentos muestreados,
con mtime restaurado, entre el precheck y el rename privado. El rebind que
ignoraba ctime podía aceptar esa carrera.

Existe una corrección en curso basada en read leases Linux en
`safety/artifact_read_lease.py`, además de cambios de proof/KIO/tests. **Falta
aceptación independiente final** de adquisición, ruptura, release, serialización,
claim y restore, incluida cancelación. Una prueba serializada no debe fabricar
una capacidad viva. Mantener fail-closed ante lease ausente/conflicto, sin leer
SHA completo de cada artefacto como sustituto ni cambiar permisos del host.

Revisar `tests/test_curation_effect_boundary_review.py` y
`tests/test_artifact_content_proof.py`. La otra observación, firmas de miembros
ZIP ajenos/no observados, recibió un fix y un owner puro compartido; también
debe revalidarse junto con la integración source-bound.

### 2. E2E con Catalog/Fast Curation/Full Semantic

`tests/test_fast_curation_end_to_end.py` contiene 108+ entradas TXT/PDF/DOCX/XLSX,
clases conocidas, ambiguas/OOD, duplicado, ZIP y Redlist. Usa productores/owners
reales con encoder, Trash y observación de recursos controlados.

Se observaron 37 clasificados y 72 abstenciones, pero el recorrido completo
todavía **no está aceptado**. Se corrigió una comparación errónea entre
`FileIdentity` y la tupla de `FileSnapshot.identity`, y se adelantó la persistencia
del receipt físico antes de rebind/cache sync. El último foco E2E aún informó
incompatibilidad hexadecimal/decimal de la identidad del receipt en el rebind
de curación. Comprobar el estado final de esa corrección antes de repetir.

No suavizar el verificador para admitir rutas antiguas. Exigir CURRENT coherente
en Framework/Inventory/Catalog/source caches, receipt durable ante fallo después
del move, callback Full Semantic ejecutado una vez a paths finales y replay sin
nuevos movimientos, Trash ni embeddings equivalentes.
Comprobar además el resultado/receipt terminal del callback y la publicación
semántica exitosa: observar llamadas al encoder o al callback no acredita éxito.
Las referencias a focos E2E intermedios son reportes de trabajo, no un receipt
de aceptación del snapshot guardado.

### 3. Calibración y costo

Se guardaron `semantic/data/curation_policy_bundle.json` y
`curation_prototypes.json`; `pyproject.toml` incluye ambos como package data.
La configuración candidata incluida apunta a Jina, batch 32 y 23 prototipos con fingerprint
`91fe4911163b0ce0c66437259109bdf3879638d2be9bfb682d931f095e379de2`.
La medición por ejes sobre 120 ejemplos de evaluación, de 256 sintéticos totales,
dio 100% de precisión observada
con 15% de cobertura; no equivale a 99% de precisión poblacional ni a validación
del corpus real. La calibración usó validation separado.

`fast-curation-benchmark-report-v2.json` fue actualizado durante la revisión y
declara schema interno v3; comprobar schema/representación/prototipos,
no inferir versión por el nombre. Comparaciones anteriores con prototipos
combinados no son transferibles al scoring productivo por eje. Los documentos
cortos produjeron un chunk por documento y no demostraron ahorro material frente
a Full Semantic. El reporte no acredita ejecutar Full Semantic: la proyección
de chunks no equivale a indexación ni a un ahorro medido. Quedan revisión del
benchmark largo, baseline heurístico real,
cascada, exactitud de los cache keys/roles y medición del costo/throughput.
El reporte conserva selección pendiente aunque los JSON candidatos estén
empaquetados en esta rama; no interpretar ese empaquetado como promoción aceptada.

### 4. Regresión, empaquetado y entrega futura

Congelar una fuente coherente después de cerrar escritores. Repetir focos de
seguridad/recovery, owners y E2E, luego cerrar la regresión amplia pendiente.
Comprobar ciclos sin agregar una excepción genérica, Ruff, métricas observadas
y la inclusión/lectura de los dos JSON desde la wheel instalada.

Sólo cuando se retome y se cumplan estos gates procede conciliar con main vivo,
publicar la entrega autorizada, construir/instalar y verificar launcher,
`source_sha`, rollback y árbol limpio. Este checkpoint no adelanta esos pasos.

## Entorno para retomar

- Repository: `/home/winterboss/Neocortex/Repository`.
- Python de QA verificado:
  `/home/winterboss/.local/share/Neocortex/tooling/quality-cp313-20260928/bin/python`.
- Ruff disponible como binario ELF standalone:
  `/home/winterboss/.local/share/Neocortex/tooling/quality-cp314-20260907/bin/ruff`.
  El nombre del directorio no autoriza ejecutar pruebas con Python 3.14.
- Mypy/Pyright/Semgrep cp313 no quedaron acreditados como disponibles offline;
  no introducir descargas implícitas ni declarar esos gates aprobados.
- Receta privada ya utilizada: `run_checks.sh` en el expediente, con copia
  completa de fuente, metadatos generados desde ella, HOME/XDG/TMP/model-cache
  privados y `bwrap --unshare-net`. Verificar rutas efectivas y no copiar sobre
  fuentes/owners de una prueba activa. Los scratch de `/tmp` son prescindibles.
- Rerun operacional: leer AGENTS/skills vigentes, comprobar rama/HEAD/remoto y
  procesos antes de actuar. Los compromisos siguen en PENDIENTES/HISTORIAL,
  separados del estado del código y de este handoff.
