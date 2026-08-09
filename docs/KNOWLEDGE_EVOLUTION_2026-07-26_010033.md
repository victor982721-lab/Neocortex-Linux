# Evolución Knowledge Plane — Fase 1

> **DOCUMENTO HISTÓRICO.** Conserva evidencia del corte indicado y no describe
> el estado vigente. Consulte [README.md](../README.md), [KNOWLEDGE.md](KNOWLEDGE.md)
> y el comando `Neocortex --knowledge-status` para el producto actual.

**Corte:** 2026-07-26 01:00:33 -06:00  
**Fuente validada:** `neocortex-framework 0.7.0`  
**Estado operativo global observado:** `neocortex-framework 0.3.0`; su launcher
rechaza `--version` y no contiene Knowledge Plane. No se actualizó.

## Resultado ejecutivo

La Fase 1 quedó integrada en el árbol fuente como una capa Knowledge coherente,
read-only, incremental y acotada. Unifica snapshot lógico, planeación,
búsqueda exacta/léxica/semántica/código, fusión, contexto citado, evaluación y
CLI sin crear una base Knowledge paralela ni reprocesar el corpus.

La aceptación funcional focal, estática, evaluación dorada, empaquetado
preliminar e instalación aislada aprobaron. La cobertura combinada acumulada es
`81.69680839459849 %`, superior al umbral histórico comparable de
`81.07590899425898 %`.

Existe una salvedad ambiental explícita: la suite monolítica bajo cobertura
agotó el headroom de commit del host porque el propio proceso Codex retenía
aproximadamente 4.95 GiB privados. El guard PDF productivo, correctamente,
rechazó ejecutar por debajo de su piso de 2 GiB. Los tests se repitieron por
lotes; los 1,499 tests no-PDF pasaron con defaults productivos y los 44 tests
PDF pasaron con un plugin temporal que desactivó sólo ese piso durante la
prueba. No se modificó el código ni la configuración productiva. Por ello no se
presenta la corrida monolítica fallida como suite verde bajo defaults.

## Arquitectura entregada

| Componente | Responsabilidad |
|---|---|
| `knowledge_contracts.py` | Contratos canónicos, IDs estables, estados, evidencia, métricas y serialización JSON. |
| `knowledge_snapshot.py` | Snapshot read-only de owners, doble observación, reintento global único e integridad acotada. |
| `knowledge_planner.py` | Gramática estricta, filtros, exact terms tipados, límites y abstención explícita. |
| `knowledge_exact.py` | Lookup exacto parametrizado sobre inventario, código y catálogo antes del top-K. |
| `knowledge_search.py` | Adaptadores, completitud, filtros, RRF, fusión de evidencia y relaciones de código. |
| `knowledge_context.py` | ContextPlan, presupuesto renderizado, citas, grafo y contradicciones tipadas. |
| `knowledge_service.py` | Fachada `status/search/context`, snapshot/reintento y validación previa a I/O. |
| `knowledge_evaluation.py` | Golden suite ejecutable, gates y métricas de retrieval/integridad. |
| `sqlite_cancellation.py` | Cancelación cooperativa SQLite preservando la excepción original. |
| `cli_knowledge.py` | Superficie CLI lazy, JSON y códigos de salida Knowledge. |

La API lazy de `_04_Nucleo_Operativo` y el entry point
`Neocortex = neocortex.cli:entrypoint` consumen esos componentes. No se retuvo
un script aislado sin productor o consumidor.

## Capacidades verificadas

### Snapshot y estado

- Roots estrictos: ausente no se crea; archivo/no-directorio e inaccesible son
  errores, no owners falsamente ausentes.
- Conexiones SQLite read-only, validación de schema y observación de heads
  publicados.
- Doble vector por owner, vector global, un solo reintento y salida
  `snapshot_changed` cuando vuelve a cambiar.
- Integridad acotada y cancelación por progress handler dentro de consultas
  largas, conservando tipo e identidad de la excepción del callback.

### Planeación y exactitud

- Rutas Windows, UNC, POSIX y relativas; nombres completos con espacios y
  extensiones arbitrarias; hashes, IDs y símbolos con contexto de código.
- Máximo de 64 términos exactos, deduplicación determinista y años sólo con
  indicios temporales.
- Predicados `source`/`format` dentro de cada owner antes de `ORDER BY/LIMIT`,
  con OR dentro de una dimensión y AND entre dimensiones.
- Aliases productivos para PDF, Office, audio, código/lenguaje e `image_ocr`.
- `omitted_matches` es una cota inferior de matches válidos observados; Search
  lo transporta una sola vez y conserva truncamiento aun cuando la omisión
  conocida sea cero.

### Retrieval y fusión

- Evidencia exacta, FTS, semántica de texto/imagen, catálogo y código permanece
  separada hasta la fusión medida.
- El catálogo regular actúa como membership/filtro y no como falsa relevancia
  RRF; estados partial/review/error se propagan.
- Presupuesto `max_vectors` global compartido por texto e imagen, cutoff
  observable y modalidad requerida sin enmascaramiento por otra completa.
- Clustering antes de diversidad/límite, reranking después de sumar señales y
  locators PDF normalizados sin colapsar secciones distintas.
- Relaciones de código son evidencias separadas con endpoints reales,
  provenance y confianza; una relación unresolved nunca fabrica una arista.

### Contexto y evaluación

- `ContextPlan` autocontenido, snippets y grafo incluidos dentro del presupuesto
  renderizado, con omisiones visibles.
- `ContextContradictionRef` sólo para claims estructurados incompatibles, ID
  XXH3 estable y dos o más citas existentes.
- Golden suite de 17 categorías que atraviesa planner, fusión, contexto y el
  seam de servicio. El fixture contiene inputs/expectativas, no outputs
  precalculados.

## Persistencia y seguridad

- `0.7.0` no eleva schemas ni crea una base Knowledge.
- Los owners conservan sus tablas, generaciones, fingerprints y migraciones.
- No se ejecutó reproceso, watcher, clasificación ni recorrido sobre el corpus
  vivo.
- No se movió, renombró ni eliminó contenido; embeddings o clasificaciones no
  autorizan acciones destructivas.
- Las pruebas usaron fixtures y estados bajo `%TEMP%`. Un temporal de una
  auditoría SQLite permanece retenido por un handle del probe; no se forzó su
  eliminación.

## Validación

### Focal y estática

- Integración Knowledge/código/CLI: **292 passed** en 14.09 s.
- Exact + Search final: **73 passed**.
- Ruff completo: limpio.
- mypy: 202 archivos sin errores; tres notas informativas por cuerpos de
  funciones no tipadas.
- `pip check`: sin requisitos rotos.
- CLI fuente sobre state inexistente: version/status/search/context
  `0/0/4/4`, stderr vacío y sin crear state.

### Suite y cobertura branch

Primera corrida monolítica con `coverage --branch`:

- **1,526 passed + 78 subtests**.
- **17 failed**, todos en `test_pdf_route.py` por `PdfResourceError` al caer el
  commit disponible por debajo del piso de 2 GiB.
- Duración: 1,388.71 s.
- El traceback quedó truncado; no se usa como evidencia completa.

Repetición particionada y acumulada:

- **1,499 tests no-PDF + 78 subtests**, todos con defaults productivos.
- **44 tests PDF**, todos con el único override temporal de headroom descrito
  arriba.
- Total lógico cubierto: **1,543 tests + 78 subtests**.

Cobertura acumulada:

| Métrica | Valor |
|---|---:|
| statements | 34,835 |
| covered lines | 29,740 |
| branches | 11,004 |
| covered branches | 7,709 |
| cobertura combinada | **81.69680839459849 %** |
| baseline histórica | 81.07590899425898 % |

La diferencia de `+0.62089940033951` puntos porcentuales satisface el umbral;
no se presenta como mejora de rendimiento ni como prueba de calidad del corpus.

### Golden suite final

- Gate de ejecución: **17/17**, aprobado.
- Gate de evaluación: aprobado.
- Recall@10: `0.9102564102564104`.
- MRR: `0.9615384615384616`.
- nDCG@10: `0.9356412992914329`.
- Cobertura de evidencia: `18/21 = 0.8571428571428571`.
- Outcome accuracy: `1.0`; abstention accuracy: `1.0`.
- Citation precision: `18/19 = 0.9473684210526315`.
- Locator precision: `18/18 = 1.0`.
- Stale y duplicate retrieved: `0/1` y `0/1`.
- El state aislado permaneció ausente.

El gate contractual no implica métricas perfectas ni calidad demostrada sobre
el corpus productivo. Los candidatos/rankings del fixture son scripted y el
caso multihop usa relaciones inyectadas.

### Benchmark

`hyperfine 1.20.0`, 3 warmups y 10 corridas, mismo host:

| Comando | Media ± sigma | Mediana | Rango |
|---|---:|---:|---:|
| launcher global 0.3 `--help` | 170.3 ± 4.9 ms | 170.6 ms | 163.4–179.4 ms |
| wheel aislado 0.7 `--help` | 204.8 ± 13.4 ms | 202.6 ms | 186.1–228.4 ms |
| wheel 0.7 Knowledge status, state ausente | 414.3 ± 10.8 ms | 413.4 ms | 395.9–428.0 ms |

El launcher global fue `1.20 ± 0.09` veces más rápido en `--help`. Es una
comparación de startup entre una instalación global 0.3 y un venv 0.7, no un
benchmark de retrieval. `0.3.0` no posee el endpoint Knowledge, por lo que no
existe una línea base comparable para status/search/context. El benchmark de
status es absoluto y confirmó que no crea state.

### Build final e instalación aislada

- sdist final: 379 miembros; tar sin traversal, paths absolutos, links ni
  miembros especiales.
- El sdist incluye este informe, el enlace exacto desde README,
  `docs/KNOWLEDGE.md`, fixture golden y módulos productivos.
- Wheel construido desde ese sdist: 212 miembros; ZIP/RECORD íntegro, tag
  `py3-none-any` y entry point
  `Neocortex = neocortex.cli:entrypoint`.
- El wheel contiene producción y tres activos UI, no tests ni docs.
- Venv `--system-site-packages`, instalación `--no-index --no-deps` y
  `pip check` aprobados.
- Desde cwd vacío: version/help/status/search/context `0/0/0/4/4`, JSON válido,
  stderr vacío y state no creado.

## Limitaciones explícitas de Fase 1

1. `SERIAL` es `UNSUPPORTED`: ningún owner Fase 1 publica un campo serial
   contractual.
2. Inventario y código no ofrecen publicación generacional uniforme; sus
   exact matches se declaran `PARTIAL`. Catálogo sí puede fijarse por head.
3. `include_history` sólo permite historias ya entregadas por owners; no existe
   un lector histórico transversal.
4. Filtros de fecha se abstienen como partial; no hay filtrado temporal
   productivo uniforme.
5. No existe grafo cross-owner general. Las relaciones owner-local de código sí
   llegan a Context.
6. El plan de duplicados de inventario es evidencia no verificada y no produce
   por sí solo una disposición destructiva o canónica global.
7. DOCX/Office/audio no se fusionan con chunks semánticos cuando no existe un
   locator neutral seguro equivalente.
8. Ventanas filtradas acotadas a 1,000 candidatos pueden producir partial
   honesto en colecciones mayores.
9. `rows_scanned`/`omitted` son cotas inferiores materializadas y
   `sqlite_steps` es cargo conservador de presupuesto; contadores per-owner de
   preflight no son aditivos, aunque el agregado global permanece acotado.
10. Un símbolo punteado como `control.validate` requiere contexto de código;
    sin él puede tratarse como nombre.
11. La cancelación SQLite durante una espera de lock puede demorarse hasta el
    `busy_timeout` de 60 s. La apertura read-only de una base en WAL puede
    requerir visibilidad `-shm/-wal`; no se usa `immutable=1` para ocultarla.
12. La instalación aislada hereda paquetes globales por
    `--system-site-packages`; demuestra compatibilidad en este host, no un
    wheelhouse hermético.
13. La instalación global 0.3 no fue promovida. Validar el wheel no equivale a
    desplegarlo.

## Criterio de cierre

La implementación Fase 1 del árbol `0.7.0` está integrada y técnicamente
validada dentro de las condiciones declaradas. El artefacto final incluye este
informe y sus sondas aisladas aprobaron. Antes de promoción operativa sólo resta
ejecutar la suite monolítica bajo defaults desde un host con al menos 2 GiB
reales de commit libre —por ejemplo con Codex cerrado— para eliminar la única
salvedad ambiental restante.
