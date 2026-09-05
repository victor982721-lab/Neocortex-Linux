# Roadmap de NeoCortex

> Actualizado el 5 de septiembre de 2026. Un estado aquí no sustituye código,
> pruebas ni una release instalada desde el SHA final.

## Convención de estado

- **CURRENT:** frontera operativa y de seguridad vigente, no identidad de la
  instalación.
- **IMPLEMENTED:** presente en el checkout y cubierto por pruebas focales; su
  disponibilidad instalada depende del SHA del manifest y del comando público.
- **TARGET:** todavía no implementado.

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
- búsqueda visual y temporal calibrada;
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

## 0.13.0 — TARGET: lifecycle durable de --all

**Vertical inicial implementado en la fuente:** las corridas Framework ya
publican un manifest versionado y con digest, conservan candidatos de rutas
necesarios para recuperación, mantienen un ledger durable de presupuesto y
status/API/SDK/MCP exponen envelopes read-only; este vertical no sustituye
todavía la reanudación multimodal completa ni la etapa Semantic integrada.

**Resultado objetivo:** reanudar una corrida multimodal interrumpida desde sus
inputs y publicaciones durables, conservando cobertura, errores y presupuesto
entre workers, sin repetir trabajo ya comprometido.

- Integrar inventario, rutas y publicaciones en un lifecycle comprobable; los
  checkpoints DFS y page-level existentes no equivalen todavía a ese contrato.
- Compartir un presupuesto global de trabajo, deadline y cancelación entre
  workers, con observabilidad de avance y reanudación.
- El ledger `neocortex.run-budget/v1` ya reserva trabajo por ruta con
  idempotencia, cancelación durable y consumo bounded de items/bytes; falta
  ampliar la cobertura a todas las fases y a la recuperación posterior a una
  terminación abrupta.
- `lifecycle_status` ya está disponible como lectura MCP bounded y read-only;
  no inicia corridas ni concede autoridad.
- Probar primero 20–50 fixtures heterogéneos: interrupción, replay terminal,
  drift y paridad de envelopes CLI/API/SDK frente a una corrida limpia.
- Conservar los fences de lectores y la separación entre consulta, producción
  de estado y efectos físicos; ni KIO real ni corpus personal forman parte del
  piloto de desarrollo.

## Orden inmediato

1. Corregir y comprobar los defectos observados del recorrido actual, con
   publicación de código e instalación como barreras separadas.
2. Diseñar los fixtures y contratos de 0.13.0 antes de ampliar escala.
3. Mantener KIO/restore de escritorio, sincronización de caches y cualquier
   autoridad MCP como gates independientes, no como requisitos de ese piloto.

## Límites

- No usar GitHub Actions ni proveedores remotos implícitos.
- No abrir el corpus real durante desarrollo o validación sin autorización.
- No reintroducir el antiguo subsistema de autoanálisis.
- Windows/NTFS no forma parte de estas entregas.
- Los informes de auditoría y evidencia bruta viven fuera de `docs/`.

La visión estable está en
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).
