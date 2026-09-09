# Registro de cambios

Este archivo conserva cambios observables del producto. Métricas, receipts,
comandos de auditoría y estado de una instalación pertenecen a evidencia fechada
fuera de `docs/`.

## 0.13.0 — instalada y verificada (fuente `42115cd`)

Estas entradas describen el contrato y la integración del lifecycle durable de
`--all`. La release `0.13.0-42115cd060f3-cp314-linux-x86_64` quedó instalada y
verificada; el receipt E2E fechado está fuera de `docs/` en
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-09-lifecycle-013/`.

### Cierre instalado

- `current` y rollback inmediato concilian con el SHA final; `.staging` quedó
  vacío y el launcher reporta `Neocortex 0.13.0`.
- Dos corridas `--all` sobre 29 fixtures aisladas terminaron con `RC1=0` y
  `RC2=0`; las nueve rutas, Semantic y replay quedaron completos, sin cambios
  en bytes de fixtures.
- La corrección del lock integrado de Semantic evita reacquirir `framework.lock`
  durante el callback lifecycle, mientras las invocaciones Semantic directas
  conservan su exclusividad.

### Lifecycle durable

- `--all` coordina las nueve rutas de contenido (`pdf`, `docx`, `office`,
  `archive`, `text`, `audio`, `video`, `image` y `code`) bajo un mismo run
  Framework; Code permanece contenido no ejecutable.
- El run publica `neocortex.run-manifest/v1` antes de trabajar y enlaza root,
  identidad, snapshot, configuración, owners, capacidades y digest. Los stages
  `preflight`, `inventory`, `catalog/dedup`, `routes`, `semantic`, `publication`
  y `finalize` dejan transiciones y checkpoints bounded, idempotentes y ligados
  al manifest.
- `neocortex.run-budget/v1` cubre inventario, catalogación/deduplicación, rutas,
  Semantic y publicación con reservas por stage/ruta/unidad, consumo de items y
  bytes, deadline absoluto y cancelación durable. El replay usa sólo el
  remanente del run origen; no abre un presupuesto nuevo.
- Las capacidades `phase_resume`, `safe_replay` y `not_resumable` forman parte
  del manifest. PDF conserva `phase_resume`; una capacidad no reanudable se
  rechaza explícitamente. Drift de root, política, snapshot, modelo,
  herramienta, manifest u owner heads queda fail-closed.

### Semantic, publicación y superficies

- Semantic se registra dentro del mismo lifecycle y Code participa como ruta de
  contenido. El Semantic pesado continúa opt-in; Archive, Code y Video son
  fuentes Semantic explícitas y no se infieren por `--all`.
- Semantic/Code publican por staging/CAS lógico y sólo avanzan el epoch cuando
  todos los heads requeridos están completos; parcialidad o ambigüedad queda
  `blocked`/`recovery_required`, sin simular una transacción SQLite distribuida.
- CLI, `read_run_status`, API, SDK y MCP comparten el envelope read-only
  `neocortex.lifecycle-envelope/v1`, con presupuesto, stages, checkpoints,
  capacidades, recuperación y owner heads bounded. MCP no recibe herramientas
  de ejecución, autorización, aplicación ni mutación.
- Ausencias de modelos, herramientas o rutas producen `unavailable`/`blocked` y
  `incomplete`, nunca éxito vacío ni skip silencioso. Los contratos v1 y sus
  manifests/checkpoints históricos siguen siendo legibles; las extensiones 0.13
  son aditivas.

## Cambios posteriores a 0.12.0

- Identidad por codec/owner y bindings de recursos físicos/lógicos, planes
  organizativos delimitados por raíz y errores de curación localizables en JSON.
- Evidencia dedup por miembro, keeper explicable y distinción entre redundancia
  nominal y espacio liberable; detalle de grupos consistente en terminal/pipe.
- Contexto v2 como flujo CLI/MCP, con presupuesto global, fuentes únicas,
  referencias directas y compatibilidad explícita con v1; diagnósticos de
  contenido y páginas vacías con cobertura y causas visibles.
- Las citas v2 separan verificación de referencia y suficiencia de respuesta,
  que queda explícitamente a cargo del LLM; una negación de consulta cuyo
  alcance no puede interpretar el helper no se etiqueta como contradicción.
- Documentos compuestos OTT/ZIP, papel documental separado de menciones,
  cobertura final PDF separada de intentos históricos y revisión informativa
  sin convertir scores en decisiones o autorizaciones.
- Duración terminal estable, scopes de salud, snapshots con presupuesto/reuso
  y eliminación de agregaciones globales repetidas durante clonación semántica.
- Las referencias lexicales ahora exigen localizadores de sección y rangos
  respaldados por el owner, el estado Semantic rechaza schemas futuros y sus
  timings se leen en el mismo snapshot; la retención aplica un límite SQL
  cooperativo sin convertir un timeout en elegibilidad.
- `ask` y el MCP incorporan `operational_query` para consultar diagnósticos
  persistidos de PDF, Text, Archive y Review sin confundir documentos que
  mencionan un error con el recurso afectado; las recomendaciones siguen siendo
  advisory y no autorizan efectos físicos.

- La extracción activa de texto queda limitada a formatos actuales, y se retiran
  del runtime los lectores CFB de DOC/XLS/PPT, sus convertidores externos y sus
  contadores de ejecución no reutilizable; DOCX/XLSX/PPTX/ODT conservan sus rutas
  nativas y caché verificable, mientras las migraciones históricas de estado se
  mantienen sólo para lectura.
- Snapshots SQLite con WAL o rollback journal se materializan sólo en temporales
  autocontenidos antes de una lectura inmutable, y el cierre conserva la causa
  primaria con diagnósticos secundarios acotados.
- `GlobalResourceCoordinator` y el orquestador retiran admisiones interrumpidas,
  liberan reservas una sola vez y persisten fallos de workers derivados de
  `BaseException` sin dejar rutas en estado `running`.
- Cada corrida Framework publica un manifest `neocortex.run-manifest/v1` con
  digest, raíz, identidad, rutas, configuración, presupuesto y snapshot de
  entradas; `--status --status-json`, API y SDK exponen un envelope read-only
  `neocortex.lifecycle-envelope/v1` sin conceder autoridad de mutación.
- `SubprocessOutputLimitError` conserva `args`, traceback y `add_note`, y las
  pruebas de publicación de imagen ya no se seleccionan como `base` sin Pillow.
- El lifecycle Framework persiste un ledger `neocortex.run-budget/v1` con
  reservas globales idempotentes, cancelación durable, deadline y consumo de
  items/bytes; status distingue reanudación, replay, recuperación y rutas no
  replayables.
- La preparación de snapshots SQLite acepta límites bounded de bytes temporales,
  tiempo y cancelación, emite métricas de intentos/preparación y aplica el mismo
  presupuesto a snapshots coordinados por writer.
- MCP incorpora `lifecycle_status` como consulta read-only bounded del estado
  Framework, manifest, presupuesto y recuperación, sin iniciar ni autorizar runs.
- El ledger de presupuesto se conecta al loop de rutas: cada worker reserva su
  workload una sola vez, los reintentos son idempotentes y cancelaciones o
  deadlines impiden cruzar la frontera de trabajo sin efecto parcial.
- La búsqueda visual CLIP admite calibración local reproducible con una muestra
  bounded de 20–50 imágenes, persiste el umbral en el owner Semantic ligado al
  modelo y al `processing_signature`, y se abstiene ante ausencia o deriva del
  contrato; la CLI expone medición, búsqueda y razones de candidatos rechazados.

- Fingerprinting POSIX rechaza FIFO, symlinks y cambios de identidad sin bloquear;
  la captura de subprocesses usa descriptores no bloqueantes con un presupuesto
  total de cleanup, y el preflight ZIP cuenta y valida el directorio central real
  antes de materializar miembros, incluidos documentos Office anidados.
- La paginación de curación publica el digest completo una sola vez por generación
  y lee páginas mediante keyset, mientras la verificación y autorización recuperan
  la membresía completa de grupos grandes desde el owner SQLite, sin usar la muestra
  visual truncada como permiso físico.
- La fachada humana convierte entradas inválidas en envelopes tipados sin traceback,
  ofrece ayuda contextual para subcomandos y rechaza el alcance `projects` de Code
  cuando una raíz explícita no coincide con ningún proyecto configurado.
- La ruta PDF coordina lecturas y escrituras del owner en un hilo dedicado durante
  la extracción, `test-base` declara explícitamente `setuptools` y la reanudación
  de inventario cuenta con una regresión de interrupción, reapertura y replay.
- El progreso Rich conserva terminales fallidos o cancelados como indeterminados y
  la API de verificación propaga sus métricas acotadas.

- Restore separa commit durable de fallos del puntero, retiene material de
  recuperación incompleta, valida schemas y usa el CAS del destino para backups
  históricos; cache-sync prepara la publicación antes de escribir owners.
- Checkpoints de curación v2 aplican presupuestos antes de verificar, conservan
  tamaño de página, revalidan replay terminal y distinguen cobertura parcial
  de paginación terminada, manteniendo lectura de manifests v1.
- Inventario corrige digest terminal y orden DFS, respeta interrupciones vacías,
  aísla nombres POSIX no representables y conserva el dispositivo observado;
  el cursor de un lote parcial termina en el último archivo admitido, y el
  replay rechaza filas adicionales o conteos/bytes incongruentes.
- Curación verifica outcomes, identidad y evidencia de Papelera también durante
  preview, reconciliación y replay; audio/video revalidan el archivo antes de
  reutilizar caché basada en metadatos.
- Lecturas SQLite estrictas verifican el fence al cerrar; health distingue WAL
  vacío de inactividad e incluye sidecars desconocidos y presupuesto cooperativo.
- Las rutas de contenido comparten candidatos de un snapshot publicado desde
  la conexión writer Framework, mientras progreso y lifecycle escriben en el
  owner original; la vista permanece válida hasta que terminan los workers.
- La búsqueda lexical conecta también el owner canónico de video, sin declarar
  cobertura completa cuando una base realmente falta.
- CLI/MCP conservan errores tipados de entrada/dependencias, rechazan valores
  booleanos inválidos y envelopes contradictorios o con colisiones de claves.
- Los fallos tipados de inventario, rutas y snapshots SQLite cierran el progreso
  con resultado incompleto y salida 2, sin ejecutar Semantic ni presentar éxito;
  una interrupción conserva estado cancelado y salida 130.
- El sdist incluye el cierre local de la herramienta de release y el staging
  verifica los archivos contra el commit identificado, sin depender de una
  segunda lectura del checkout mutable.
- Release separa el corpus operativo de los smokes temporales, respeta overrides
  por proceso y valida el launcher antes de ejecutarlo; rollback conserva la
  raíz operativa y un manifest ausente o corrupto nunca autoriza a retirar la
  release activa.
- Instalación ordinaria CPython 3.13 desde archivos sin Git, con cierre offline
  versionado de runtime, construcción, pruebas base y documentos/imagen,
  separado del wheel instalado y de la promoción personal CPython 3.14.
- Ayuda, estado publicado y contratos ligeros independientes de inferencia y
  Qt; diagnóstico distingue requisitos compatibles, presencia y comprobación.
- El doctor de plataforma separa rutas canónicas de rutas efectivas por
  argumentos o entorno, sin crear directorios ni estado.
- Memoria y CPU consideran límites/consumo de cgroups v2, presión y afinidad,
  conservando los controles y presupuestos del producto.
- Timeout, cancelación y cierre normal limpian el grupo original de workers aislados aunque
  su líder ya haya terminado, con identidad del wrapper y limpieza acotada;
  no se amplía esa garantía a procesos que abandonen el PGID.
- Modelos locales inspeccionables por selección, sin descarga durante el
  procesamiento offline ni sustitución de backend, pesos o identidades.
- Semantic rechaza fuentes bloqueadas antes de cargar modelos o crear
  generaciones, comprueba la deriva de los heads antes de publicar e invalida
  sólo sus candidatos `building` para permitir un nuevo intento, conservando
  jobs, historia y el head anterior. Imagen vacía concilia como `done`, y
  `embed_ocr_text` participa en el ledger para impedir replay de una política
  obsoleta.
- Suite seleccionable por capacidades antes de colección y fixtures pequeños
  que recorren CLI, owners SQLite y replay del producto instalado.
- Muestreo de video acotado por frecuencia al final del clip, con una política
  identificada en la procedencia para no reutilizar resultados anteriores.

## 0.12.0 — 2026-09-04

### Verificación acotada

- Se añadió `CurationWorkBudget` para limitar de forma explícita items, archivos,
  bytes, deadline monotónico y cancelación cooperativa durante la verificación
  exacta, conservando resultados parciales y razones tipadas sin efectos.
- `curate scan` conserva errores tipados y falla cerrado ante una combinación
  inconsistente de error y cobertura completa, mientras la salida humana mantiene
  separados `persisted_mode` y `observed_mode`.
- La planificación de duplicados descarta un candidato mutado durante la
  comparación exacta, evitando que se convierta en representante o redundante.
- Se añadió el contrato durable `neocortex.curation-checkpoint/v1` con JSON
  canónico, escritura atómica no-replace, root/source/plan/snapshot digests,
  batch digest, presupuesto acumulado, validación de drift y sucesores
  deterministas para reanudación por página en API/SDK.
- La verificación usa buffers fijos y almacenamiento temporal para el keeper, y
  el benchmark opt-in reproduce 100,001 archivos sintéticos con throughput,
  memoria, batches, commits y ETA; no se ejecuta sobre el corpus ni se registra
  como herramienta MCP.

## 0.11.1 — 2026-09-04

### Recovery y restore de curation

- Se añadieron `curate recovery status` y `curate restore preview` como
  superficies fenced de lectura, sin migrar owners ni crear sidecars.
- El restore grant-bound exige confirmación exacta del action/receipt, registra
  un `restore_curation` intent antes del efecto y verifica root, Trash,
  `.trashinfo`, bytes, hash, identidad y destino no existente con
  `renameat2(RENAME_NOREPLACE)`.
- Los fallos posteriores al movimiento quedan `recovery_required`, el replay
  devuelve `already_restored` cuando la evidencia coincide y MCP no recibe
  autoridad de restore.

## 0.11.0 — 2026-09-04

### Curation grant-bound

- Se añadió `neocortex.curation.application` para consumir exclusivamente grants
  con manifests de ReviewTask heads, raíz y efectos físicos, revalidando plan,
  identidad, hash, contención y presupuestos antes de cada frontera.
- Los grants nuevos conservan un efecto físico expandido por item, con digest
  completo de fuente y keeper, y calculan `max_actions`/`max_bytes` sobre esos
  efectos; los grants legacy siguen siendo legibles pero no consumibles.
- `PosixRenameBackend` implementa no-replace same-filesystem sobre fixtures y
  `KioTrashBackend` exige evidencia estructurada de Papelera; no se selecciona
  backend real automáticamente ni se usa `gio`, borrado directo o fallback de
  copia.
- `curate apply`, `curate reconcile` y sus adaptadores API/SDK proyectan estados
  bounded, receipts y `recovery_required`; MCP no recibe autoridad de aplicación
  ni de conciliación escrita.

## 0.10.0 — 2026-09-04

### Documentación

- Se definió File Intelligence & Curation como visión estable del producto y se
  separaron visión, arquitectura, CLI, operación, persistencia, seguridad,
  Knowledge, recovery, roadmap e historia.
- Se retiraron del árbol activo auditorías fechadas, handoffs sustituidos,
  snapshots de licencias, instrucciones Windows y la documentación operativa del
  antiguo autoanálisis.
- Se documentó para `0.11.0` la promoción de la foundation KIO ya preparada a
  una Papelera KDE same-filesystem y reversible, con plan, autorización y
  recovery, sin `gio trash` ni fallback destructivo.
- Se reconciliaron los punteros de arquitectura, Knowledge, roadmap y handoff
  con la línea vigente de `main`; el handoff de pausa anterior permanece como
  referencia histórica.

### Producto en el árbol posterior a 0.9.0

- Code permanece limitado a contenido: ingesta, detección, estructura,
  persistencia, búsqueda y relaciones.
- Las herramientas de desarrollo dejaron de formar parte del runtime productivo.
- Image ya no incorpora NudeNet ni su cadena de clasificación.
- `Neocortex databases` expone status, backup, restore y purge con preview,
  manifests, locks y confirmaciones.
- `--state-health` usa snapshots compatibles y reporta owners/sidecars.
- `--curation-preview` compone planes read-only con cobertura.
- `neocortex.safety.kio_trash` prepara descubrimiento, preflight, validación de
  snapshot, ejecución inyectable y receipts KIO, pero permanece desconectado de
  Linux `--apply` y no fue probado contra KIO real.
- `curate plan` y la herramienta MCP `curation_plan` consultan páginas de
  propuestas con cursor y digest ligado al snapshot; la API de evidencia acepta
  `evidence_id` y snapshot esperado para evitar reasignar alias.
- `curate review`/`curation_review` publican páginas con cobertura completa como
  ReviewTasks advisory, ligadas a `plan_digest` y snapshot; el replay de la misma
  página es idempotente.
- `curate decide`/`curation_decide` registran por CAS una decisión humana
  `resolved` o `dismissed`, con scope, actor y event head esperado. Sólo escriben
  eventos ReviewTask: no crean `file_actions`, no autorizan, no invocan KIO y no
  cambian corpus ni sistemas externos.
- `curate authorize` y la API/SDK `curation_authorize_payload` emiten un
  AuthorizationGrant append-only dentro de la extensión Framework
  `curation_authorization_grants`. Exigen plan vigente, ReviewTasks resueltas,
  actor, acción, expiración y presupuestos; el replay equivalente es idempotente.
- `curate scan`/`curation_scan` consultan el plan publicado con un envelope
  acotado, y `curate verify`/`curation_verify` comprueban identidad, hash completo
  y bytes de grupos duplicados sin crear efectos ni `file_actions`.
- Scan, plan y verify exponen el mismo manifiesto `source_heads` de inventario y
  catálogo, ligado al digest del plan con revisión, cobertura, modo y razón
  de cada owner.
- Los grants nuevos conservan un manifiesto inmutable de heads de ReviewTask,
  con versiones, eventos, fingerprints y digest agregado; los grants históricos
  sin ese manifiesto permanecen legacy y no son consumibles por un futuro `apply`.
- Emitir el grant no crea `file_actions`, no llama KIO y conserva
  `physical_effect_applied=false`. MCP no expone authorize hasta resolver un
  principal autenticado; la brecha siguiente es `apply → verify → reconcile`.
- El lifecycle de curación no incorpora exportación ni ZIP; `--json` devuelve el
  envelope de la operación.
- La publicación cross-owner, el ledger generacional Code, Review y el manifest
  multimodal avanzaron en el árbol fuente, pero requieren validación conjunta y
  una release instalada desde el SHA final.

## 0.9.0 — 2026-08-10

- Se estableció Linux/Kubuntu y CPython 3.13–3.14 como plataforma activa.
- Se introdujo la fachada humana de status, search, ask, inspect, Review y MCP
  read-only.
- Knowledge, Semantic y Code ampliaron evidencia, linaje y consultas.
- Se separaron dependencias productivas de herramientas de desarrollo.
- Se añadió instalación Linux versionada con manifest, launcher estable y
  política `current + rollback inmediato`.
- Linux mantuvo mutaciones de corpus deshabilitadas con
  `linux_mutation_backend_unavailable`.
- Cambios posteriores retiraron la plataforma de validación interna y el soporte
  activo Windows sin reinterpretar datos históricos.

## 0.8.0 — 2026-08-09

- Se amplió el procesamiento Linux de PDF, DOCX, Office, ZIP, Text, Audio, Video,
  Image y Code.
- Se añadieron contratos de capacidades, selección de providers y cobertura.
- Se fortalecieron publicación generacional, replay y búsquedas multimodales.
- Se incorporó la interfaz PySide6 Linux y el aislamiento de workers.
- Se preservó Windows como compatibilidad histórica mientras Linux permanecía
  fail-closed para efectos.

## 0.7.2 — 2026-07-31

- Se introdujeron perfiles internos de análisis del repositorio, providers
  externos, receipts y Review de código.
- Se añadieron inventarios de dependencias, validación de artefactos y guards de
  rutas internas.
- Se amplió recovery de acciones, retención y estado read-only.
- Esa plataforma se retiró posteriormente: sus datos son historia y no forman
  parte de la interfaz productiva actual.

## 0.7.1 — 2026-07-26

- Se integró Knowledge sobre owners existentes sin crear una base paralela.
- Se añadieron contexto citado, rankings separados y evaluación reproducible.
- Se mejoraron límites de recursos, cancelación y reanudación.
- Se publicaron mejoras de Semantic y Code con evidencia de cobertura.

## 0.7.0 — 2026-07-25

- Se introdujeron owners y publicaciones para Knowledge, catálogo y Semantic.
- Se añadieron snapshots lógicos y lectores read-only.
- Se fortalecieron contratos de identidad, errores y salida estructurada.
- Las migraciones permanecieron aditivas y fail-closed.

## 0.6.0 — 2026-07-25

- Se añadió observación durable de acciones inciertas y un planificador de
  retención read-only.
- Se ampliaron factories SQLite, diagnósticos y contratos de conexión.
- Se separó observación de recovery respecto de decisión y autorización.

## 0.5.0 — 2026-07-24

- Se introdujeron publicación generacional de inventario, catálogo y Semantic.
- Se añadieron estados de acción y conciliación después de interrupciones.
- La implementación original de efectos estaba ligada a NTFS y quedó fuera del
  alcance Linux posterior.

## 0.4.1 — 2026-07-24

- Se corrigieron publicación parcial, cursor de inventario y transacciones.
- Se reforzaron migraciones, foreign keys y rollback.
- Se añadió validación de artefactos e instalación aislada.

## 0.4.0 — fecha no verificada

- Se consolidaron inventario, extracción, búsqueda y organización iniciales bajo
  el comando `Neocortex`.
- La ausencia de una fecha verificada se conserva explícita; no se infiere una
  fecha desde commits posteriores.
