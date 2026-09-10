# Roadmap de NeoCortex

> Actualizado el 9 de septiembre de 2026. Un estado aquí no sustituye código,
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
| Review | **IMPLEMENTED:** `curate review` publica ReviewTasks y `curate decide` añade decisiones humanas por CAS |
| Curación integrada | **CURRENT:** scan/plan/verify; **IMPLEMENTED:** review/decide, AuthorizationGrant durable y apply/reconcile grant-bound sobre fixtures |
| Mutación Linux | `curate apply` usa backends POSIX/KIO inyectados y ledger/recovery; `--apply`/`--organization-apply` genéricos siguen absteniéndose |
| Backup/restore/purge | Implementados mediante `Neocortex databases` |
| MCP | **IMPLEMENTED:** plan/scan/verify/review/decide; authorize se omite hasta resolver un principal autenticado |
| Lifecycle durable de `--all` | **IMPLEMENTED INSTALADO:** `current` es `0.13.0-c6d3985f7a45-cp314-linux-x86_64`; C0–C7 y la tranche post-0.13 están aceptados sobre sus `source_sha` |

## 0.10.0 — Evidencia y plan de curación

**Resultado:** una persona o agente puede inspeccionar, paginar y revisar un plan
completo sin mutar el corpus.

**IMPLEMENTED en el checkout:** `curate scan` y `curate plan` consultan el digest
paginado; `curate verify` revalida identidad, hash completo y bytes de grupos
duplicados sin escribir estado; `curate review` publica páginas idempotentes como
ReviewTask y `curate decide` registra `resolved`/`dismissed` mediante digest y
event-head CAS. API, SDK y MCP proyectan estas operaciones. Scan/plan/verify son
read-only; review/decide escriben sólo Framework, mantienen
`actions_authorized=false` y crean cero `file_actions`.

Las superficies de scan, plan y verify comparten `source_heads` para inventario y
catálogo, con revisión, digest, cobertura, modo de verificación y razón de
abstención; verify admite `--cursor` para recorrer páginas posteriores sin
confundirlas con un cambio del snapshot.

`curate authorize` y `curation_authorize_payload` validan plan completo,
ReviewTasks resueltas, acción, actor, expiración y presupuestos, y persisten un
grant append-only con manifiesto de heads, versiones, eventos, fingerprints y
digest agregado en la extensión Framework. Está expuesto por CLI/API/SDK, no por
MCP; declara autoridad acotada, pero `physical_effect_applied=false` y crea cero
`file_actions`.

Entregas restantes:

1. proyección común de tipo real, procedencia, valor, duplicado, versión,
   similitud, disposición y evidencia;
2. ampliar el plan inmutable ya paginado con reason codes y localizadores
   públicos comprobables;
3. resolver autenticación antes de considerar un tool MCP de autorización;
4. límites uniformes de elementos, tiempo, RAM y disco, con progreso y
   cancelación;
5. corregir la paridad de `--all`, `resume` y las fachadas públicas;
6. cerrar las regresiones de fences SQLite y restore que afectan la siguiente
   cohorte física.

Criterios de aceptación:

- fixture heterogéneo de 20–50 elementos recorre scan, plan, verify, review y
  decide con paginación/replay;
- segunda corrida no rehace trabajo compatible;
- cada propuesta enlaza evidencia y explica incertidumbre;
- igualdad exacta exige comparación byte a byte;
- CLI, SDK, GUI y MCP proyectan el mismo schema;
- cero cambios en bytes/rutas del corpus, cero `file_actions` y cero autoridad
  derivada de una decisión; el grant sólo aparece tras `curate authorize` y no
  demuestra aplicación física.

No se añadirá exportación ni ZIP de curación en este corte. JSON/JSONL son
respuestas de interfaz, no artefactos de entrega.

## 0.11.0 — IMPLEMENTED: efectos Linux reversibles sobre fixtures

**Resultado verificado:** un grant aprobado puede mover, renombrar o enviar a
Papelera un lote pequeño sobre una raíz de fixture mediante un backend inyectado,
y después demostrar o conservar para recovery el efecto.

Decisión de backend:

- reutilizar `neocortex.safety.kio_trash`, ya preparado pero no promovido ni
  validado contra KIO real;
- Papelera KDE mediante el primer cliente disponible entre `kioclient6`,
  `kioclient5` y `kioclient`, con `move <origen> trash:/`;
- preflight de identidad y revalidación para compensar la resolución path-bound;
- rename POSIX no-replace separado del backend de Papelera;
- ningún fallback a `gio trash`, `unlink`, borrado directo o copia+delete;
- timeout o efecto ambiguo dejan recovery pendiente, sin reintento automático.

Entregas implementadas:

1. `apply` consume el AuthorizationGrant vigente y crea un intento `file_actions`
   por efecto, con replay idempotente;
2. revalidación de grant, expiración, digest, ReviewTask heads, identidad,
   tamaño, mtime y hash junto a la frontera;
3. aplicación KIO/rename dentro del scope, acción y presupuestos concedidos;
4. verificación física con receipt y evidencia de Papelera/destino;
5. `reconcile` resuelve cada punto de caída y conserva `recovery_required`;
6. lotes pequeños con límite de acciones/bytes y cancelación entre efectos;
7. API, SDK y CLI proyectan el resultado, mientras MCP no expone autoridad de
   aplicación ni conciliación escrita;
8. `reconcile` clasifica y registra eventos append-only de forma idempotente.

Criterios de aceptación verificados en fixtures:

- mismo filesystem aprobado; `EXDEV` se abstiene;
- symlink, hard link no soportado, destino existente o fuente mutada se abstienen;
- crash antes/después de metadata y rename produce estado conciliable;
- restore usa no-replace y verifica bytes;
- una segunda aplicación del mismo plan no repite efectos;
- el piloto no toca contenido fuera de su raíz y límites.

Pendiente de promoción: verificador/runner KIO real, restore no-replace contra
la Papelera del escritorio, sincronización de caches y una GUI que sólo presente
el grant y el intento. El restore no-replace de fixtures ya está implementado en
el corte 0.11.1, pero esos gates reales no se ejecutaron para evitar tocar el
escritorio o el corpus real.

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

## 0.13.0 — IMPLEMENTED: lifecycle durable de `--all`

**Estado documental:** el artefacto instalado es
`0.13.0-1567fe46821b-cp314-linux-x86_64` y su `source_sha` es
`1567fe46821b923be5e90ba4223abdaf81a9924c`. C0–C7 están aceptados exactamente
sobre ese SHA con 6917 pasadas, 68 omitidas y 42 subtests; la calidad estática y
el piloto instalado de 37 fixtures concilian con el mismo artefacto.

**Resultado objetivo:** una corrida `--all` coordina `pdf`, `docx`, `office`,
`archive`, `text`, `audio`, `video`, `image` y `code`, integra el stage Semantic
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
  cancelación durable, incluidos inventario, publicación Semantic y Code.
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
- `--all` coordina las nueve rutas de contenido, incluido Code como contenido
  no ejecutable. Semantic se integra en el mismo lifecycle, pero el Semantic
  pesado permanece opt-in; Archive, Code y Video sólo entran como fuentes
  Semantic cuando se seleccionan explícitamente.
- Una fuente, modelo o herramienta ausente se registra como `unavailable` o
  `blocked`, conserva la causa y produce `incomplete`; nunca hay skip silencioso
  ni éxito por ausencia.
- Resume hereda el presupuesto/deadline restante del run origen y valida root,
  política, snapshot, modelo, herramienta, manifest y owner heads. Drift,
  publicación parcial o ambigüedad queda `blocked`/`recovery_required`.
- Semantic/Code publican por staging/CAS lógico: el epoch sólo avanza cuando
  todos los heads requeridos están completos. No se simula una transacción
  SQLite distribuida.

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
  base, nueve rutas, stages terminales y manifest completo.
- **C2 — Replay:** segunda pasada con `new_work=0` para trabajo comprometido,
  sin duplicados ni efectos repetidos.
- **C3 — Dependencias:** ausencia de Audio/Whisper u otra herramienta devuelve
  causa tipada `unavailable`/`incomplete`.
- **C4 — Presupuesto:** items, bytes, deadline y cancelación se respetan en
  inventario, workers, Semantic y publicación; no se completa después de expirar.
- **C5 — Recuperación:** interrupciones en preparación, snapshot, worker, PDF,
  Code, Semantic, publicación y finalización permiten dos resumes idempotentes.
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

## Post-0.13 — IMPLEMENTED: candidata instalada y verificada

La tranche quedó implementada en `main` y promovida a `current` desde
`c6d3985f7a45fc3120bd03e9561195674f2b8ac2`. El artefacto activo es
`0.13.0-c6d3985f7a45-cp314-linux-x86_64`; el rollback inmediato es
`0.13.0-1567fe46821b-cp314-linux-x86_64` y `.staging` está vacío. Incluye:

- inventario/deduplicación v13 con sucesores copy-on-write, digest de contenido,
  heads de plan y rechazo de cache stat-only ante reescrituras ambiguas;
- catálogo v9 con manifest de generación, source fence, digest, CAS y triggers
  de inmutabilidad, además de materialización binding-aware para Archive/Code;
- localizadores y hydration bounded para Audio, Video, Image y Code, Context v2
  con entidades, relaciones, contradicciones y telemetría, y v1 explícito;
- `content-diagnostics/v2` para los nueve owners, cursores ligados a snapshot y
  `KnowledgeReadBudget` con deadline, filas, vectores, temporales y cancelación;
- contrato `neocortex.authenticated-principal/v1`, lectura fenced de grants y
  recovery, sincronización de caches move/rename sólo sobre fixtures y panel GUI
  read-only; MCP no recibe autorización ni aplicación.

La integración y release quedaron aceptadas con **6959 pasadas, 67 omitidas y
42 subtests**, calidad estática individual sin errores bloqueantes, build
reproducible, verificación de manifest/árbol/launcher y smoke/replay instalado
`RC1=0`/`RC2=0` sobre 23 fixtures temporales y nueve rutas. Semantic 17, R1–R4,
KIO real, corpus personal y poda permanecen fuera de esta tranche.

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

## Límites

- No usar GitHub Actions ni proveedores remotos implícitos.
- No abrir el corpus real durante desarrollo o validación sin autorización.
- No reintroducir el antiguo subsistema de autoanálisis.
- Windows/NTFS no forma parte de estas entregas.
- Semantic pesado no se activa por `--all`; modelos y herramientas se preparan
  sólo mediante una operación explícita y autorizada.
- Los informes de auditoría y evidencia bruta viven fuera de `docs/`.

La visión estable está en
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).
