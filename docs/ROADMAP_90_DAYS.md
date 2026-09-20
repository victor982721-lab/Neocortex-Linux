# Roadmap de NeoCortex

> Actualizado el 15 de septiembre de 2026. Un estado aquí no sustituye código,
> pruebas ni una release instalada desde el SHA final; el `HEAD` documental y el
> `source_sha` instalado se registran por separado.

## Convención de estado

- **CURRENT:** frontera operativa y de seguridad vigente, no identidad de la
  instalación.
- **IMPLEMENTED:** presente en el checkout y cubierto por pruebas focales; no
  implica por sí solo aceptación integral ni disponibilidad instalada.
- **TARGET:** todavía no implementado.
- **SOURCE-ONLY:** existe en el checkout o está en integración, pero todavía no
  acredita aceptación integral ni una release instalada.

## Resultado buscado

Víctor debe poder convertir una raíz caótica en inventario, comprensión,
relaciones, plan revisable, efectos autorizados y verificación sin encargar un
script diferente por etapa.

La seguridad se mide por separación de efectos, identidad, revalidación,
Papelera reversible y recovery. Abstenerse es correcto ante una precondición
incierta, pero no cuenta como funcionalidad entregada para los casos soportados.

## Capacidades de la fuente actual

| Capacidad | Estado |
|---|---|
| Inventario e identidad Linux | Implementado; generaciones, snapshots y conciliación de scans abandonados |
| Extracción multimodal | Implementada con cobertura desigual por formato |
| Deduplicación | Planificación y `curate verify` implementados; la disposición pública sigue bloqueada hasta `apply` |
| Catálogo y organización | Planes disponibles; recorrido end-to-end parcial |
| Knowledge y contexto para agentes | Implementado read-only; cobertura/localizadores varían por owner |
| Review humano | **RETIRADO:** no hay ReviewTask, colas, eventos ni decisión humana obligatoria |
| Curación integrada | **CURRENT:** plan/scan/verify; `--apply` ejecuta sólo acciones automáticas seguras y conserva incertidumbre, receipts y recovery |
| Mutación Linux | `--apply` usa backends POSIX/KIO neutrales y ledger/recovery; incertidumbre queda en KEEP |
| Backup/restore/purge/factory reset | Implementados mediante `Neocortex databases` y `Neocortex --factory-reset`; sus contratos permanecen separados |
| MCP | **IMPLEMENTED:** plan/scan/verify y consultas read-only; no publica autorización ni review humano |
| Lifecycle durable de `--all` | **IMPLEMENTED INSTALADO:** `current` es la release Linux CPython 3.13 (`cp313`) validada; el `release_id` y `source_sha` vigentes constan en el receipt y en `tools/release_linux.py verify` |
| Preparación federada de `hygiene` | **CURRENT / PREPARACIÓN:** registry y manifests de owners/procedencia, categorías y cobertura en modo read-only/preview-only; zero deletion. La cadena de efectos es TARGET y permanece bloqueada |

## 0.10.0–0.11.0 — Evidencia y efectos (histórico consolidado)

El plan paginado, `curate scan` y `curate verify` conservan la evidencia,
identidad, cobertura y razones de abstención sin mutar el corpus. Las antiguas
superficies humanas de ReviewTask/decide/authorize fueron retiradas por la
simplificación estructural: no forman parte del runtime ni de CLI, API, SDK o
MCP. La clasificación automática mantiene `UNKNOWN` como KEEP.

Los backends POSIX/KIO y el ledger de `file_actions` se conservaron en el owner
neutral. La aplicación automática sólo cruza la frontera con `--apply` y una
raíz contenida; identidad, no-follow, no-replace, receipts y
`recovery_required` permanecen obligatorios. No existe un grant humano ni una
segunda implementación física.

## 0.12.0 — Inventario reanudable y verificación acotada

**Resultado demostrado:** inventario y verificación exacta acotados, con
checkpoint/replay sobre fixtures y un benchmark de 100,001 archivos sintéticos.
Ese benchmark no demuestra extracción multimodal, búsqueda ni `--all` sobre
100,000 documentos reales.

La instalación personal de este corte y sus comprobaciones están registradas en
el handoff de 0.12.0. Los cambios posteriores de la fuente se distinguen en
[CHANGELOG.md](CHANGELOG.md); publicar una corrección en `main` no actualiza por
sí solo el launcher ni el artefacto instalado.

**Implementado en la fuente:**

- `CurationWorkBudget` opcional para verificación exacta, con límites de items,
  archivos, bytes, deadline monotónico y cancelación cooperativa;
- razones de truncamiento bounded y resultados parciales que conservan lo ya
  observado, sin efectos, `file_actions` ni cambios del corpus;
- `scan` que conserva cardinalidad y códigos de error tipados, además de mostrar
  por separado el modo persistido y el modo observado;
- planificación de duplicados que descarta un candidato mutado durante la
  comparación exacta;
- fixtures de replay, paginación, límites, cancelación y previews SQLite
  fenced, todos contenidos en temporales.
- contrato `neocortex.curation-checkpoint/v2` con manifests canónicos bounded,
  validación de root/source/plan/snapshot drift, batch digests, presupuesto
  acumulado y sucesor idempotente por página mediante API/SDK; las correcciones
  posteriores al corte 0.12.0 conservan lectura de v1 sin reescribir sus bytes;
- streaming de verificación con buffers fijos y benchmark opt-in de 100,001
  archivos sintéticos, con throughput, memoria, batches, commits y ETA.
- contrato `neocortex.inventory-resume/v1` para el inventario DFS, con orden
  determinista por bytes, cursor de recorrido real, identidad de raíz y de los
  directorios abiertos, digests de prefijo/directorio/lote, manifest canónico
  acotado, escritura atómica `0600`, owner lock y actualización monotónica;
  una interrupción deja un owner `partial` y una repetición terminal valida el
  corpus antes de devolver el mismo `scan_id`.
- `InventoryWorkBudget` global para archivos y bytes, deadline monotónico,
  cancelación cooperativa y comprobaciones antes y después de cada transacción
  bounded; los lotes de inventario mantienen el límite común de 10,000 filas.
- fixtures de reanudación con cursor vacío, nombres cuyo prefijo es directorio,
  drift de identidad/política, swap de ancestros y paridad contra una corrida
  limpia, sin filas duplicadas ni omisiones y sin tocar corpus real.

**Trabajo posterior, fuera del gate de 0.12.0:**

- procedencia y localizadores estructurales para más formatos;
- búsqueda temporal calibrada y ampliación de la búsqueda visual calibrada;
- cobertura generacional ampliada a owners que hoy son best-effort;
- políticas de canonicalización y versiones con evaluación representativa;
- acciones MCP opcionales sólo con concesión humana externa y el mismo ledger.

Criterios verificados del corte acotado:

- benchmark reproducible informa archivos/s, bytes/s, memoria, commits y ETA;
- cancelación deja checkpoint válido;
- reanudación y replay terminal concilian el inventario contra una corrida
  determinista limpia, rechazan drift y no duplican filas;
- no se reduce seguridad para ganar throughput.

La conciliación integral entre owners y las métricas de precisión/recall para
clasificación multimodal siguen siendo objetivos, no resultados de ese benchmark.

## 0.13.0 — IMPLEMENTED: lifecycle durable de `--all` (histórico CPython 3.14)

Los identificadores `cp314` de esta sección conservan evidencia histórica del
corte 0.13.0; no describen el runtime soportado ni el `current` vigente.

**Estado documental:** el artefacto instalado es
`0.13.0-1567fe46821b-cp314-linux-x86_64` y su `source_sha` es
`1567fe46821b923be5e90ba4223abdaf81a9924c`. C0–C7 están aceptados exactamente
sobre ese SHA con 6917 pasadas, 68 omitidas y 42 subtests; la calidad estática y
el piloto instalado de 37 fixtures concilian con el mismo artefacto.

**Resultado objetivo:** una corrida `--all` coordina `pdf`, `docx`, `office`,
`archive`, `text`, `audio`, `video` e `image`, integra el stage Semantic
con la misma identidad durable y puede reanudar sólo el trabajo incompleto,
conservando cobertura, errores, checkpoints, owner heads y presupuesto entre
workers.

### Contrato y secuencia

- Publicar antes de trabajar un `neocortex.run-manifest/v1` con root/identidad,
  snapshot, configuración efectiva, rutas, owners, capacidades y digest.
- Ejecutar `preflight → inventory → catalog/dedup → routes → semantic →
  publication → finalize`; cada stage conserva transición idempotente y
  checkpoint bounded ligado al digest del manifest.
- Mantener `neocortex.run-budget/v1` como presupuesto de toda la corrida, con
  reservas/consumo por stage, ruta y unidad, items, bytes, deadline absoluto y
  cancelación durable, incluidos inventario y publicación Semantic.
- Exponer límites opcionales `--run-max-items`, `--run-max-bytes` y
  `--run-time-budget-seconds` en FrameworkConfig y CLI, y proyectarlos sin
  divergencias en API/SDK.
- Reutilizar `GlobalResourceCoordinator`; una consulta de estado no crea runs,
  no reserva trabajo y no concede autoridad.

### Rutas, Semantic y recuperación

- Cada adapter declara `phase_resume`, `safe_replay` o `not_resumable`; PDF
  conserva reanudación por fase y una capacidad `not_resumable` siempre se
  rechaza, nunca se interpreta por inferencia.
- El adapter estima workload de forma bounded y emite checkpoints cooperativos;
  no reserva todo un snapshot antes de aplicar sus filtros.
- `--all` coordina las ocho rutas de contenido. Semantic se integra en el mismo
  lifecycle, pero el Semantic pesado permanece opt-in; Archive y Video sólo
  entran como fuentes Semantic cuando se seleccionan explícitamente.
- Una fuente, modelo o herramienta ausente se registra como `unavailable` o
  `blocked`, conserva la causa y produce `incomplete`; nunca hay skip silencioso
  ni éxito por ausencia.
- Resume hereda el presupuesto/deadline restante del run origen y valida root,
  política, snapshot, modelo, herramienta, manifest y owner heads. Drift,
  publicación parcial o ambigüedad queda `blocked`/`recovery_required`.
- Semantic publica por staging/CAS lógico: el epoch sólo avanza cuando todos
  los heads requeridos están completos. No se simula una transacción SQLite
  distribuida.

### Superficies y compatibilidad

- `read_run_status`, `lifecycle_status`, CLI, API, SDK y MCP comparten el
  envelope read-only `neocortex.lifecycle-envelope/v1`, con stages, presupuesto,
  checkpoints, capacidades, recuperación y owner heads bounded.
- Los contratos v1 existentes siguen siendo legibles; los campos y extensiones
  de 0.13 son aditivos. MCP no añade ejecución, autorización, aplicación ni
  mutación.

### Criterios de aceptación

- **C0 — Contratos:** registro cerrado de rutas, owners, dependencias,
  capacidades y estimadores de workload.
- **C1 — Positiva:** 20–50 fixtures temporales, con las 28 heterogéneas como
  base, ocho rutas, stages terminales y manifest completo.
- **C2 — Replay:** segunda pasada con `new_work=0` para trabajo comprometido,
  sin duplicados ni efectos repetidos.
- **C3 — Dependencias:** ausencia de Audio/Whisper u otra herramienta devuelve
  causa tipada `unavailable`/`incomplete`.
- **C4 — Presupuesto:** items, bytes, deadline y cancelación se respetan en
  inventario, workers, Semantic y publicación; no se completa después de expirar.
- **C5 — Recuperación:** interrupciones en preparación, snapshot, worker, PDF,
  Semantic, publicación y finalización permiten dos resumes idempotentes.
- **C6 — Drift:** root, política, snapshot, capability `not_resumable` y
  owner-head drift se rechazan fail-closed.
- **C7 — Paridad:** CLI/API/SDK/MCP devuelven los mismos estados, errores,
  stages, capacidades y recuperación; consultar no crea estado.

La validación local requerida ejecuta Pytest, Ruff, Mypy, Pyright y Semgrep como
herramientas individuales, además de una suite integral posterior a la
integración transversal. El receipt final acredita build reproducible,
manifest/wheel/launcher, smoke/replay desde el artefacto instalado, piloto
aislado y corpus intacto. Los gates físicos, Semantic 17 y reservas R1–R4
siguen separados.

## Post-0.13 — IMPLEMENTED: candidata instalada y verificada (histórico CPython 3.14)

La tranche quedó implementada en `main` y promovida a `current` desde
`c6d3985f7a45fc3120bd03e9561195674f2b8ac2`. El artefacto activo es
`0.13.0-c6d3985f7a45-cp314-linux-x86_64`; el rollback inmediato es
`0.13.0-1567fe46821b-cp314-linux-x86_64` y `.staging` está vacío. Incluye:

- inventario/deduplicación v13 con sucesores copy-on-write, digest de contenido,
  heads de plan y rechazo de cache stat-only ante reescrituras ambiguas;
- catálogo v9 con manifest de generación, source fence, digest, CAS y triggers
  de inmutabilidad, además de materialización binding-aware para Archive;
- localizadores y hydration bounded para Audio, Video e Image, Context v2
  con entidades, relaciones, contradicciones y telemetría, y v1 explícito;
- `content-diagnostics/v2` para los ocho owners, cursores ligados a snapshot y
  `KnowledgeReadBudget` con deadline, filas, vectores, temporales y cancelación;
- contrato `neocortex.authenticated-principal/v1`, lectura fenced de grants y
  recovery, sincronización de caches move/rename sólo sobre fixtures; MCP no
  recibe autorización ni aplicación.

La integración y release quedaron aceptadas con **6959 pasadas, 67 omitidas y
42 subtests**, calidad estática individual sin errores bloqueantes, build
reproducible, verificación de manifest/árbol/launcher y smoke/replay instalado
`RC1=0`/`RC2=0` sobre 23 fixtures temporales y ocho rutas. Semantic 17, R1–R4,
KIO real, corpus personal y poda permanecen fuera de esta tranche.

## Preparación federada de `hygiene`

**CURRENT / PREPARACIÓN:** `hygiene` establece una superficie local bounded para
reunir, de extremo a extremo, el registry y los manifests de fuentes de
higiene. El resultado de esta etapa es sólo read-only/preview-only: conserva
owners, procedencia, identidad, cobertura, límites, retención declarada y
razones; no publica heads, no crea `file_actions`, no modifica corpus/estado o
sistemas externos y exige **zero deletion**. Su presencia documental o en la
fuente no acredita una instalación ni una limpieza ejecutada.

El registry versionado identifica por entrada el adaptador, owner lógico, root o
ámbito, schema/manifest, categoría y capacidad. El manifest de la petición liga
esas fuentes con snapshot/owner-head, digest, identidad física, montaje,
permisos, actividad, bytes observados, cobertura y presupuesto. La federación
consume sólo las proyecciones existentes y sus límites:

- scratch registrado y sus manifests `neocortex.scratch/v1` bajo
  `state/scratch`;
- estado/planes de retención read-only de cada owner, sin `DELETE`, `VACUUM`,
  compactación ni poda;
- `neocortex.machine-inventory/v1` como observación metadata-only del host;
- `neocortex.external-maintenance/v1` como diagnóstico de un root y categoría
  externos explícitos, sin ownership implícito.

Para artefactos locales, el registry de fuente es
`neocortex.artifact-registry/v1` y cada manifest liga owner/producer,
`artifact_id`, propósito, root/path, identidades físicas, `kind`, `state`,
`source_ref`, digest, dependencias, retención, `disposable`, metadata acotada y
`manifest_digest`. La preparación sólo consume sus operaciones read-only
`plan`/`verify`; registrar o actualizar un artefacto sigue siendo responsabilidad
separada del owner.

Las categorías son `canonical`, `operational`, `rebuildable`, `temporary` y
`cache`. Son clasificación, no disposición: lo canónico y operativo se
preserva; lo rebuildable sólo es potencialmente reconstruible con inputs y
receta demostrables; lo temporal exige registro/lifecycle; y una cache requiere
política del owner, costo de reconstrucción y procedencia. Corpus, fotos, correo,
configuración, modelos, backups, sesiones, releases y otros datos personales no
se reducen a la dicotomía código/documentación: cualquiera puede ser fuente
canónica u operativa, y lo externo, ambiguo o sin owner se conserva o se bloquea.

Los límites de roots, entradas, profundidad, bytes, manifests/registros,
tiempo y cancelación son explícitos y se propagan al manifest. Cotas agotadas,
raíces ausentes, cobertura parcial, owners no disponibles, schemas futuros o
drift permanecen visibles y nunca se reinterpretan como cero bytes o permiso de
retiro. El filesystem no se trata como snapshot atómico; se conservan
no-follow, identidad física, fences de montaje/permisos y la prohibición de
abrir SQLite cercada, usar red/KIO/sudo o llamar cleaners.

**TARGET — efectos separados:** cualquier evolución que actúe deberá cruzar, en
orden y con evidencia independiente, `preview → review → authorize → apply →
verify → recovery`. Review no autoriza; authorize sólo emite un grant acotado;
apply requerirá backend reversible, locks y revalidación fresca; verify tendrá
que demostrar la postcondición; recovery conservará receipt y resolverá toda
ambigüedad, drift, timeout o efecto parcial. La preparación actual no expone
`apply` y no convierte su manifest en autorización.

### Criterios para el siguiente gate

- registry y manifest reproducibles y acotados, con owner/procedencia por entrada
  y categorías sin colapsar estados de cobertura;
- federación con scratch, retención, machine-inventory y diagnóstico externo sin
  abrir owners cercados ni duplicar sus writers;
- replay o reconsulta que detecte cambios de identidad, manifest, owner-head,
  política, actividad, montaje, permisos o límites y se abstenga fail-closed;
- fixture con categorías canónicas, operativas, rebuildables, temporales y
  cache, incluyendo entradas sin owner, donde el conteo de eliminaciones y
  `file_actions` permanezca en cero;
- documentación y ayuda que mantengan `hygiene` separado de los `--apply`
  existentes y no presenten una lista de candidatos como limpieza universal.

## Orden inmediato

El plan funcional autorizado prioriza identidad/ámbito y evidencia verificable,
contexto v2 realmente utilizado por CLI/MCP, diagnóstico por owners y costos de
lectura/incrementalidad. Su cierre requiere resolver los incidentes originales
de curación y recuperación, no sólo reproducciones, y evaluar el candidato con
familias reservadas que no se hayan utilizado para ajustar el sistema. El
handoff funcional conserva ese gate separado de la publicación y la instalación;
no habilita limpieza, KIO real, reindexación global ni modelos nuevos.

1. Mantener la release instalada y el rollback inmediato bajo verificación de
   procedencia, sin abrir el corpus personal ni la SQLite cercada durante writers.
2. Mantener KIO/restore de escritorio, sincronización de caches y autoridad MCP
   como gates independientes, no como requisitos del piloto.
3. Tratar Semantic 17, R1–R4 y cualquier poda como decisiones separadas.
4. Cualquier cambio posterior de código/configuración/build exige resolver de
   nuevo el SHA final y ejecutar el procedimiento de release desde el artefacto.
5. Mantener `hygiene` en preview-only/zero deletion hasta que cada gate
   `preview → review → authorize → apply → verify → recovery` tenga contrato,
   revalidación, receipt y recuperación independientes.

## Límites

- No usar GitHub Actions ni proveedores remotos implícitos.
- No abrir el corpus real durante desarrollo o validación sin autorización.
- No reintroducir el antiguo subsistema de autoanálisis.
- La plataforma activa es Linux/Kubuntu; Windows/NTFS no se publica ni valida.
- Semantic pesado no se activa por `--all`; modelos y herramientas se preparan
  sólo mediante una operación explícita y autorizada.
- Los informes de auditoría y evidencia bruta viven fuera de `docs/`.

La visión estable está en
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).
