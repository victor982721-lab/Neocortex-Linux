# Registro de cambios

Este archivo registra cambios observables del producto. Las cifras de pruebas,
cobertura y rendimiento pertenecen al informe técnico fechado de cada auditoría;
no se copian aquí para evitar que se conviertan en datos históricos sin contexto.

## [Unreleased]

### Añadido

- Acceptance experimental de contratos arquitectónicos declarados: cualquier
  cambio Python productivo queda ligado a
  `architecture.declared_import_contracts_are_evaluated`. Tres nodeids y cuatro
  gates comprueban el grafo vivo, una frontera permitida y controles negativos
  de aristas/ciclos; el receipt puede cerrar una disposición técnica acotada,
  pero conserva como riesgos el dispatch dinámico y la intención no declarada.
  La identidad portable excluye `baseline`/`passed`, IDs de tool-run y modo
  full/replay: esas coordenadas no invalidan un receipt si proveedor, schema y
  proyección de contratos son exactamente iguales; cualquier delta contractual
  sí genera una identidad nueva.
- Validación canónica `neocortex.code-change-validation/v3` ligada al diff:
  rutas y tests afectados se proyectan a
  preguntas/sujetos de aceptación versionados. Una pregunta relevante sin
  runner o sin disposición técnica exacta después del replay ahora abstiene;
  `not_required` exige evidencia de que el cambio es disjunto. La admisión
  `neocortex.code-validation-resources/v3` autentica contra el kernel la
  membresía al cgroup transient, consulta en systemd el `PrivateNetwork=yes` y
  prueba que la restricción `AF_UNIX` deniega realmente AF_INET/AF_INET6. El
  watchdog diferencia reclaim aislado con headroom de presión que amenaza al
  escritorio y detiene con SIGINT para permitir terminalización durable.
  Los cambios al ejecutor `tools/quality_gate.py` usan una matriz acotada de sus
  cinco contratos consumidores; los baselines de Coverage, packaging y policy
  continúan siendo fronteras de suite Linux completa.

- Review `neocortex.code-review/v18` con verificación técnica independiente y
  allow-listed. Sólo puede publicar
  `no_change_required_within_verified_scope` cuando requisitos,
  contraevidencia, receipt, gates y controles negativos específicos vuelven a
  comprobarse; no suplanta un actor humano, conserva riesgos residuales y nunca
  autoriza mutación. El proceso de staging Semantic ante muerte del proceso se
  incorpora como tercer template ejecutable, con tres gates medidos y binding a
  la proyección publicada Text→Semantic.
- Matriz experimental Code-owner para migraciones: cinco nodeids aislados
  verifican upgrade poblado desde v1, preservación de rows/relations/FTS,
  incorporación de receipts v6, rollback transaccional y rechazo read-only de
  schemas futuros. Sus cuatro gates satisfacen únicamente la pregunta exacta de
  evolución de schema y permiten una disposición técnica acotada, nunca una
  autorización para migrar estado vivo.
- Receipt loop acotado del autoanalizador: `--code-experiment-run` persiste el
  resultado terminal `neocortex.code-experiment-receipt/v3` en un envelope
  `neocortex.code-experiment-store/v1`; el review posterior enlaza únicamente
  terminales `passed` más nuevos del proposal/signature vigentes y gates
  registrados; un replay Code exacto puede reutilizar el receipt de un run
  completado anterior, mientras un terminal posterior fallido o abstenido lo
  invalida. Un envelope digest recalculable liga el contexto durable. Los
  bindings productivos cubren contratos de imports declarados, acceptance
  pública Text, workflow
  SQL/transaccional Text, recuperación Semantic acotada y la matriz de schema
  Code-owner; la evidencia exacta no crea una decisión humana, recomendación ni
  autoridad de mutación.
- Framework schema 21 con `ReviewTask` durable: batches y receipts acotados,
  memberships, tareas versionadas, eventos append-only con CAS, progreso keyset
  y heads fuente generacionales. Cada batch
  admite hasta 1,000 inputs y 100 tareas; la migración 20→21 es aditiva, deja el
  contrato vacío y no fabrica decisiones o hallazgos históricos.
- Primera productora ReviewTask para Value. `Neocortex review value --refresh
  --scope personal|framework` avanza exactamente una página de 100
  observaciones, puede crear/migrar Framework y publica su estado sólo dentro de
  ese owner. Rechaza `all`, vuelve a comprobar el snapshot antes del commit y no
  muta archivos, Inventory ni Catalog.
- Cola durable paginada para que Personal pueda preseleccionar más de 25,000
  observaciones sin retirar el límite. Los estados
  `ready`/`partial`/`stale`/`absent`/`unavailable` exponen la causa; decisiones
  humanas `RESOLVED`/`DISMISSED` sobreviven a refresh/reconstrucción.
- Journey CLI `review task show|history|claim|decide`: lectura acotada, CAS,
  actor explícito, retry idempotente y decisiones scoped `permanent`,
  `until-source-change` o `until-policy-change`. Sólo una expiración tipada
  permite successor receipt-backed; decisiones legacy no se reinterpretan.
- Progreso Value separa fin del cursor (`scan_complete`) de integridad de la
  evidencia (`evidence_complete`). Una página parcial conserva su razón durante
  toda la corrida y no permite superseder tareas abiertas por ausencia, aunque
  una página posterior complete el keyset.
- La página final con evidencia completa promueve un único head fuente
  append-only. La supersession de ausentes se deriva de ese receipt sin miles de
  eventos; `SUPERSEDED` queda reservado a receipts sistémicos exactos.
- Contratos públicos stdlib-only `CapabilityManifest`, `CapabilityRequest`,
  `CapabilityPolicy`, `CapabilityAvailability`, `CapabilitySelection` y
  `CapabilityBroker`, con manifests versionados/acotados, hard filters,
  preferencias explicables, abstención ante empate o ambigüedad y fingerprints
  canónicos de request, política, manifest y selección.
- Dos manifests estáticos de `text.extract/v2` para el extractor builtin y el
  worker Office heredado, con readiness por trabajo y política
  `neocortex-text-local-v1` local/offline. El builtin no depende de LibreOffice;
  conserva clase `environment_bound`, declara `incremental=true` y sigue siendo
  cacheable. DOC/XLS/PPT usa el worker v2
  `best_effort`/`non_replayable`/`incremental=false`: exige un backend compatible
  observado, fija su launcher mediante SHA-256/tamaño/ubicación hasheada y
  prohíbe fallback oculto después de iniciar un intento.
- Diagnóstico opt-in
  `Neocortex doctor capabilities --select text.extract --mime-type MIME
  --input-bytes BYTES [--json]`, con explicación por candidato y schema
  `neocortex.capability-selection/v1`, sin engines, modelos ni escritura de
  estado.
- Contratos públicos inmutables Reproducible Derivations schema 1:
  `StageDescriptor`, `InputBinding`, `OutputBinding`, `MaterializationRef`,
  `DerivationRef`, `CapabilityFailure`, `ReproducibilityClass`, `WorkOutcome`,
  `WorkExecutionMode` y `WorkReceipt`, con fingerprints,
  causalidad, configuración/runtime acotados y clases de reproducibilidad sin
  declarar `exact` cuando faltan los requisitos verificables.
- Text extraction/publication slice `text.extract/v2`: cada revisión fuente puede explicar sus
  outputs `text_representation` y `text_fts`; ejecución, cache hit, fallo,
  cancelación y abandono emiten receipts terminales sin atribuir outputs a un
  intento fallido.
- Outboxes append-only owner-local de Text y Semantic y proyección causal
  descartable en memoria, reconstruible e idempotente. Explica ancestros,
  producción/reutilización, causación e impacto por cambio de firma sin crear
  una autoridad SQLite transversal.
- `Neocortex inspect lineage IDENTIFICADOR [--scope
  personal|framework|all] [--json]` y `neocortex.read_api.lineage_payload()`
  para consultar linaje Text histórico o chunks Semantic con ventanas y códigos
  de abstención explícitos, sin crear ni migrar estado.

### Cambiado

- Code pasa de schema 5 a 6 mediante una migración aditiva que crea
  `code_experiment_receipts`, tabla append-only con triggers que impiden update y
  delete. El wire de receipts se introdujo en `neocortex.code-review/v17` y el
  vigente es `neocortex.code-review/v18`, sin
  declarar compatibilidad estructural con versiones anteriores, e incluye la
  proyección acotada de receipts exactos. El ejecutor verifica la firma de
  inputs Python/soporte Git antes y después, y un fence Linux de identidad,
  sidecars y anclas acotadas cubre Code durante la fase de tests sin releer el
  store completo; la CLI escribe el receipt después y por ello no es read-only.
- Framework pasa de schema 21 a 22 y Code de 4 a 5 para aplicar una única
  política de identidad de rutas: `BINARY` en Linux y `NOCASE` en Windows. La
  migración Framework 21→22 reconstruye sólo `route_candidates` cuando hace
  falta, conserva las filas ReviewTask y versiona sus dos triggers de lifecycle;
  los productores y
  Retention exigen v22 exacto. Knowledge valida v22 como current y admite
  v19/v20/v21 sólo mediante sus validadores legacy exactos.
- `review value` sin `--refresh` permanece read-only, pero prefiere una cola
  ReviewTask vigente; si Framework v22 o la cola no existen conserva el preview
  legacy sin crear ni migrar estado. Un scan incompleto reanuda la época fijada
  aunque cruce medianoche; al cambiar fuente o política, el último head completo
  permanece visible como `stale` y conserva decisiones humanas actuales.
- El snapshot Knowledge de Framework v22 valida y expone heads ReviewTask y
  agrega watermarks de batches, eventos y publicaciones fuente; mantiene
  compatibilidad read-only validada con v19/v20/v21. Retention protege tareas y
  eventos humanos como holds y, por separado, cada head vigente con toda su
  cadena alcanzable de batches, memberships y progreso exactos. La auditoría
  falla cerrado bajo cotas explícitas y usa búsquedas indexadas por batch.
- Cada candidato Text selecciona provider por request/política/readiness en vez
  de usar el primer provider disponible. Sus receipts y firmas de procesamiento
  conservan provider/versión, fingerprints de manifest/política/selección,
  readiness y explicación; una abstención publica sólo el fallo durable, nunca
  outputs o heads parciales.
- Text reutiliza éxitos/fallos compatibles del provider builtin
  `environment_bound`, pero nunca consulta la caché para el worker Office
  heredado. Su receipt es `non_replayable` y cada corrida vuelve a ejecutar
  incluso con firma invariable, porque sólo se atesta el launcher seleccionado,
  no una clausura transitiva arbitraria de engines o dependencias. Las requests
  exigen incrementalidad sólo para MIME builtin; Office permanece seleccionable
  con el tradeoff explícito de no procesar únicamente cambios.
- `Neocortex doctor capabilities [--json]` sin `--select` conserva el reporte
  agregado `RuntimeCapabilitySpec` schema 1, su orden y su comportamiento
  ligero previo; el broker sólo se construye en la modalidad opt-in.
- Text pasa de schema 1 a 2 con revisiones inmutables, intentos, bindings,
  materializaciones, heads, receipts y outbox. La migración exacta conserva
  documentos/FTS legacy, deja su `revision_id` nulo y no fabrica receipts ni
  procedencia retroactiva.
- La publicación Text confirma documento/FTS, materializaciones, heads, receipt
  y outbox en una sola transacción del owner. Para el builtin, el cache hit exige
  outputs físicos y fingerprints vigentes; cambios de contenido, MIME,
  configuración o stage invalidan la reutilización. El worker Office no produce
  cache hits de éxito ni de fallo.
- Semantic pasa de schema 6 a 7 mediante una migración aditiva que preserva los
  heads generacionales de v6 y agrega tracking de intentos, receipts,
  derivaciones de chunks y outbox. No se inventa linaje para trabajo legacy.
- Las fuentes Semantic procedentes de Text conservan el `RevisionRef`
  owner-native cuando está disponible, en vez de depender sólo de una revisión
  sintética DB-local.

### Corregido

- XLS y PPT heredados priorizan sus extractores locales `xls2csv` y `catppt`
  antes de LibreOffice. Cada intento conserva un único backend fijado y no cae
  silenciosamente a otro si éste falla; los receipts siguen declarándose
  `non_replayable`.
- Semantic ya no clona una materialización base que tiene trabajo objetivo en
  la generación sucesora. Los estados históricos que conservan un receipt de
  clone y otro posterior para el mismo ID se reanudan usando únicamente el
  productor cuyo fingerprint coincide con la materialización física vigente.
- Video OCR participa en Knowledge lexical mediante el owner publicado, con
  locator temporal y cobertura fail-closed; Audio enlazado no se duplica y no se
  añadieron embeddings Video.

### Límites conocidos

- `Knowledge Asset Health` general permanece parcial/planificado: los estados
  causales implementados sólo cubren la cola Value. ReviewTask para OCR,
  entities/claims, contradicciones, links, recovery y promoción shadow, además
  de una GUI para refrescar/decidir tareas, todavía no está implementado; la CLI
  sí cubre ya el lifecycle humano.
- CapabilityBroker v1 sólo está conectado a la ruta Text. PDF, DOCX, la ruta
  Office, Semantic y plugins/providers externos permanecen planificados; no hay
  autodescubrimiento de plugins ni providers pesados obligatorios.
- Reproducible Derivations v1 implementa extracción y publicación Text; la
  certificación del SHA se determina dinámicamente por las barreras y la release,
  no por esta entrada.
  La extensión Semantic es parcial y no equivale a linaje completo de PDF,
  DOCX, Office ni de materializaciones creadas antes de schema 7.
- `normalize` y `chunk` todavía no son stages Text independientes; se
  incorporarán únicamente donde existan fronteras ejecutables reales.
- La proyección transversal es una vista reconstruible, no una transacción
  distribuida ni un grafo epistémico de entities/claims.

## [0.9.0] - 2026-08-10

### Añadido

- Gate único `tools/quality_gate.py` para inventariar dinámicamente toda la
  suite, ejecutar contratos Grimp contra el grafo vivo, impedir deuda nueva de
  Ruff/Mypy/Pyright, auditar supply chain y certificar Coverage completo con
  ramas y baseline de no-regresión. El CI publica el reporte Coverage y ya no
  mantiene listas manuales de pruebas.
- Runtime administrado y aislado de Semgrep, expuesto sólo mediante un wrapper
  `scan`, con lock exacto, inventario y hashes en un recibo verificable. Sus
  tres excepciones MCP quedan confinadas, declaradas como no alcanzables y con
  vencimiento; el entorno de aplicación no las hereda.
- Bootstrap compartido y autenticado de `pip 26.1.2` para CI, release Linux y
  entornos Windows/offline: verifica el wheel fijado antes de ejecutarlo,
  elimina configuración `PIP_*` ambiental e instala sin índice ni dependencias.
- Dedup schema 10 con índices de identidad para las relaciones de Knowledge y
  migración 9→10 aditiva, exacta, transaccional e idempotente.

### Cambiado

- MCP sube a `1.29.0`; el bridge Linux que acepta streams ya abiertos queda
  encapsulado en una única frontera privada, ligada a versiones probadas y
  fail-closed. El runtime base se reduce a `packaging`, `rich` y `xxhash`;
  `agent` y `analysis` son extras de distribución y la release personal sigue
  instalando su unión `full`.
- La página GUI **Consulta** ejecuta lecturas publicadas en una tarea cancelable,
  mantiene la ventana responsiva y permite copiar el resultado sin añadir
  ninguna superficie de escritura.
- Los contratos de Archive/Video, Semantic, Knowledge y políticas de corpus
  invierten dependencias hacia modelos o Protocols neutrales; las fachadas y
  reexports públicos conservan su identidad.
- El grafo de imports de producción pasa de cuatro SCC conocidos a cero. El
  baseline arquitectónico v2 es vacío, de modo que reintroducir cualquiera de
  los ciclos resueltos vuelve a fallar el gate principal.
- Los guards de mutación se reconstruyen por lote, no por candidato, sin quitar
  la revalidación de raíz, componentes e identidad inmediatamente antes de la
  frontera de acción.

### Corregido

- Se restauró la segunda pasada TOCTOU por candidato después de que el nuevo
  batching revelara sustituciones de raíz y componentes entre preflight y
  frontera; ambas regresiones fallan de forma cerrada y el presupuesto de
  reconstrucciones permanece acotado.
- La consulta de relaciones de inventario evita escaneos costosos por identidad
  sin alterar evidencia, generaciones ni decisiones de deduplicación.

## [0.8.0] - 2026-08-09

### Añadido

- Soporte de producto para Kubuntu/Ubuntu 26.04: política XDG central,
  inventario portable case-sensitive sin symlinks, identidad POSIX
  `st_dev`/`st_ino` con `birthtime_ns=-1` cuando no existe nacimiento real,
  contención por grupos de proceso y límites de memoria, y doctor de plataforma
  versionado.
- `Neocortex models prepare/status` para preparar secuencialmente y revisar de
  forma local el conjunto Jina, MiniLM, CLIP, Whisper y NudeNet compartido entre
  releases.
- `tools/release_linux.py` para instalar, verificar y revertir releases
  inmutables CPython 3.14 con wheel `full`, Node/Pyright aislados, activación
  atómica, recibos y publicación KDE.
- Normalización reproducible de la dependencia source-only `yattag 1.16.1` a
  un wheel local después de validar el hash de su sdist; la instalación final
  conserva `--only-binary=:all:`.
- Modo portátil Linux en la interfaz: sin elevación falsa, con inventario,
  búsqueda y procesamiento disponibles y controles de mutación deshabilitados.
- Ruta `archive` incremental para ZIP y ZIP anidados, con cadenas visibles
  `contenedor.zip!/otro.zip!/miembro`, límites contra expansión maliciosa,
  extracción textual acotada, FTS5, consultas directas y owner Knowledge
  aditivo. Los miembros nunca se materializan en el filesystem.
- OCR aislado y acotado dentro de Archive para imágenes BMP/GIF/JPEG/PNG/
  TIFF/WebP y para páginas PDF con texto nativo insuficiente, incluidas cadenas
  de ZIP anidados. Conserva miembro, contenedor, profundidad, modo de extracción
  e incidencias cuando el runtime OCR no está disponible.
- Ruta `text` incremental para texto imprimible, Markdown, CSV/TSV, HTML, XML,
  JSON, EML y Office heredado DOC/XLS/PPT. Publica `text.sqlite3` schema 1 con
  FTS, título/autor, metadata y errores; alimenta catálogo, Knowledge y
  Semantic. LibreOffice corre en worker aislado y existen fallbacks locales
  `catdoc`/`xls2csv`/`catppt`.
- Política Semantic `semantic-text-quality-v1` que omite ruido de alta confianza
  —binario codificado, volcados de fórmulas, mojibake, tokens desmedidos y
  repetición mecánica— y colapsa chunks exactos repetidos sin alterar las
  cachés fuente.
- Huella completa XXH3-128 de cada imagen elegible o reutilizada, persistida en
  Dedup para que la planificación y publicación CLIP no pierdan cobertura por
  fingerprints ausentes.
- Fachada humana read-only `status`, `search`, `ask`, `inspect code` y
  `review value`, con scopes fijos Personal/Framework, salida trazable y JSON;
  los flags históricos permanecen compatibles.
- API `neocortex.read_api` y servidor MCP local exclusivamente por stdio con
  tools read-only `status`, `search`, `context`, `evidence` e `inspect_code`.
- Revisión `value-preview` advisory, explicable y acotada sobre SQLite
  publicado; nunca autoriza mover, archivar o borrar.
- Ruta `video` v1 con FFprobe/FFmpeg, escenas, keyframes, muestreo periódico,
  OCR por frame, timestamps, FTS, métricas, límites y vínculos a Audio.
- Perfiles OCR `latin`, `han-simplified`, `han-traditional` y
  `auto-multilingual` para PDF, imagen y video, con alemán, chino simplificado/
  tradicional, OSD, fallback acotado y procedencia por página/frame.
- Persistencia Office v2 de cada celda XLSX no vacía: libro, hoja, A1, tipo,
  valor lógico/raw, fórmula, cache, estilo/formato y proyección FTS.
- Fallback lexical CJK de substring exacto, posterior a FTS y limitado a
  50 000 filas por fuente, más fixtures positivos/negativos ES/EN/DE/ZH.
- Página GUI **Consulta** para estado, búsqueda, contexto y revisión de valor,
  con evidencia seleccionable, límites y abstención visible.

### Cambiado

- La reanudación ilimitada de la ruta Image reproduce primero las
  clasificaciones vigentes desde SQLite, de modo que el progreso acredita la
  caché antes de iniciar trabajo nuevo. Un `Ctrl+C` conserva además el último
  lote ya completado; los límites explícitos siguen priorizando imágenes aún
  no procesadas.
- El paquete admite CPython 3.13 y 3.14 en Windows y Linux
  (`>=3.13,<3.15`); el carril `standard` instala el perfil `full`, comprueba sus
  imports nativos y prueba el wheel con ambas versiones y sistemas.
- `--apply` y `--organization-apply` se abstienen en Linux antes de crear
  estado con razón estable `linux_mutation_backend_unavailable`; Windows
  conserva USN, Job Objects, `ReplaceFileW` y mutaciones ligadas a handles.
- La instalación documenta y valida el Microsoft Visual C++ v14 Redistributable
  x64 requerido por los wheels nativos; `pip check` permanece como una barrera
  distinta y no sustituye el probe de carga de DLL.
- El proveedor de historia Git admite repositorios con ownership de otro SID
  mediante `safe.directory` limitado a la raíz exacta validada y a cada comando;
  no usa comodines ni modifica configuración persistente de Git.
- Los extras `audio` y `full` declaran directamente `ctranslate2`, y el runtime
  base declara `radon` para la suite NTFS que ejecuta el perfil profundo, de
  modo que cada import pertenece a una dependencia explícita sin excepciones
  de Deptry.
- Los pisos accionables de seguridad suben a `Pillow 12.3.0` y
  `setuptools 83.0.0`; constraints, CI y la guía de build vigente comparten las
  mismas versiones.
- El entrypoint del runtime versionado incorpora su directorio Node propiedad
  de la instalación junto al shim de Pyright, sin depender del `PATH` heredado.
- `--all` incorpora las cachés Archive/texto al canal textual y publica también
  imagen CLIP/OCR cuando existe `image.sqlite3`; Code permanece como inclusión
  explícita. La documentación deja de congelar conteos históricos de millones
  de chunks o vectores y remite a `--semantic-status`.
- La abstención Jina usa el contrato mixto v2 y piso `0.42` para todos los
  owners textuales admitidos, incluido el canal de título. La diversidad limita
  resultados repetidos a un documento en discovery y dos evidencias en
  evidence.
- El título Semantic v3 prefiere metadata propia —como el asunto EML—, usa un
  encabezado acotado cuando el basename es genérico y conserva el basename como
  fallback, siempre como señal advisory sin autoridad de organización.
- Knowledge distingue una ventana top-k normal del truncamiento duro:
  `result_window_full`/`window_omitted_candidates` no degradan completitud;
  los cortes reales propagan `next_cursor` y `cutoff_score`.
- La búsqueda lexical usa strict-first, stopwords ES/EN/DE y fallback acotado;
  el escaneo exacto de vectores se agrupa con NumPy conservando scores,
  empates, identidad y provenance.
- MiniLM queda como candidato shadow tras un bakeoff medido, no como reemplazo
  automático de Jina; cada espacio y calibración permanecen separados.
- Recuperación visual CLIP pasa a fail-closed sin calibración positiva/negativa
  compatible. El fixture humano mostró solapamiento y no justificó un umbral
  escalar productivo.
- PDF schema 12 y Office schema 2 agregan migraciones aditivas; Video inicia en
  schema 1. La evidencia nueva se materializa sólo al reprocesar.

### Corregido

- `--image-max-count` limita ahora toda la selección de imágenes, incluidos
  aciertos de caché y huellas completas pendientes; el orden estable prioriza
  trabajo nuevo, incompleto o desactualizado antes de reutilizar caché vigente.
- El probe de FFprobe usa su argumento real `-version`, evitando un falso
  `unavailable` cuando el ejecutable está instalado.
- Las comprobaciones de snapshot aceptan `birthtime_ns=-1` como sentinel Linux
  y conservan el `ctime` positivo exacto sólo para verificar estado legacy;
  Windows mantiene birthtime positivo y cualquier deriva sigue fallando.
- La validación SQLite informa primero tablas durables ausentes y no deja que
  un FTS ajeno dañado oculte el contrato requerido; los módulos de work package
  interpretan rutas Windows correctamente aun cuando el análisis corre en
  POSIX.
- La aplicación autorizada de organización en Windows sincroniza también la
  ruta física y el FTS de `text.sqlite3`; el replay es idempotente y conserva
  el mismo documento identificado.
- Code search ordena valores opcionales `None`/texto de forma determinista;
  ContextBundle reserva una cita útil antes de consumir el presupuesto en
  diagnósticos.
- Video visual-only deja de producir un error Audio global: publica `no_audio`
  benigno y no carga Whisper; MIME de audio conserva el fallo.
- Los readers read-only pueden abrir bases SQLite inactivas sin crear sidecars,
  pero se abstienen ante actividad o contratos incompatibles.
- El transporte MCP usa pipes asyncio nativos en CPython 3.14 y completa
  initialize, listado y llamada real; el transporte upstream basado en workers
  AnyIO podía quedar
  bloqueado tras recibir stdin.

## [0.7.2] - 2026-07-31

### Añadido

- Fachada Python lazy `neocortex.sdk` para los contratos y el servicio
  Knowledge read-only existentes, conservando identidad con imports legacy y
  declarada como paquete tipado PEP 561 mediante markers `py.typed` tanto en
  el facade como en su shim de implementación durante la transición.
- Contrato público `neocortex.capabilities` schema 1 con declaraciones
  estáticas por ruta, estados `available/degraded/unavailable` y probes de
  spec, metadata y paths que no importan engines ni cargan o descargan modelos.
- Telemetría Knowledge schema 1 en nanosegundos para planner, snapshots before
  y after por intento, owners/rankings, fusión, broker y compilación de
  contexto. Los retries conservan las fases de ambos intentos; Exact mide una
  vez por owner batch sin duplicar el tiempo por término.
- Preset `--self-analysis` para analizar una raíz de código explícita como
  `analyze_only`, con raíz/estado disjuntos, entrada directa desde inventario y
  exclusión obligatoria de generated/vendored, catálogo, organización y rutas
  MIME.
- Manifest `neocortex.self-analysis-manifest/v2` ligado transaccionalmente a la
  finalización, con lectura legacy v1. Conserva policy/firma, identidad,
  scan/journal, evidencia code, cuatro conteos cero y argv canónicos
  `analyze`/`status` como arrays acotados; si USN no está disponible registra el
  fallback completo sin checkpoint ni cursor ficticio.
- Plataforma External Evidence v1 con perfiles `protected`, `trusted-static` y
  `trusted-deep`. Protected conserva Ruff basic aislado `E4,E7,E9,F`;
  trusted-static suma Ruff proyecto, Mypy, Pyright, Ruff Analyze, Grimp,
  Complexipy, Vulture, Semgrep, Deptry, pip-audit, inventario instalado e
  historia Git local como productores independientes. Trusted-deep añade
  Pytest/Coverage y mutación focal Cosmic Ray sólo para la identidad física
  exacta del repositorio. Todos publican evidencia advisory y carecen de
  autoridad de mutación; sólo el perfil profundo ejecuta contenido.
- Code schema 3 añade contratos, inputs, findings, replays y counters externos;
  schema 4 añade métricas y relaciones genéricas con migraciones compatibles y
  consumidores reales. El replay exacto verifica fingerprints y no duplica
  evidencia; el inventario instalado conserva como costo real la reverificación
  de `RECORD`.
- Ruff Analyze/Grimp publican grafo, contratos, SCC y ciclos; Complexipy,
  complejidad cognitiva por símbolo y módulo. Vulture se correlaciona con
  Pyright, grafo, contratos dinámicos y Coverage para explicar o abstenerse ante
  código potencialmente no usado, sin autoridad de borrado.
- Semgrep `1.172.0` ejecuta tres invariantes locales con autofix deshabilitado;
  Deptry `0.25.1` analiza higiene de declaraciones; pip-audit `2.10.1` captura
  snapshots fechados de PyPI; y el inventario instalado verifica constraints,
  metadata de licencia e integridad `RECORD`. Seis gates mantienen separadas
  las categorías de dependencias, vulnerabilidades, paquete y licencias, sin
  score mágico ni fixes.
- `--code-review` v10 —compatible con v2-v9— y
  `--code-publication-diff` v8 —compatible con v1-v7— exponen proveedores,
  arquitectura, Coverage, consenso de uso, supply chain y analítica de
  ingeniería mediante deltas y gates explicables. Status, work packages y code
  doctor consumen la misma evidencia sin cambiar ranking ni autorizar
  mutaciones.
- `--code-query {status,review,diff}` unifica consultas acotadas sobre esas
  publicaciones. Admite filtros repetibles por proveedor, categoría, módulo,
  estado, delta y work package, salida humana/JSON y hasta 500 resultados;
  nunca reanaliza ni escribe estado y no reduce dimensiones a un score mágico.
- Historia Git local v1 publica commits, churn, recencia y relaciones de cambio
  sin consultar remotos. `neocortex.code-engineering-analytics/v1` correlaciona
  esa historia con arquitectura v2, Coverage y mutación focal mediante
  dimensiones y gates explícitos, con `aggregate_score=null` y
  `defect_probability=null` en vez de una puntuación mágica.
- Cosmic Ray `8.4.6` genera y ejecuta mutantes sólo sobre una copia staged
  verificada del objetivo y su selección de pruebas. Los argumentos nuevos
  `--deep-mutation-target`, `--deep-mutation-symbol`,
  `--deep-mutation-max-mutants`, `--deep-mutation-timeout-seconds` y
  `--deep-mutation-time-budget-seconds` acotan target, símbolo, cantidad,
  timeout por mutante y presupuesto total. El proveedor declara posible red por
  el contenido ejecutado, pero no puede mutar repositorio, corpus ni estado
  durable.
- Workflow único `Neocortex CI` para Windows/Python 3.13: carriles `fast` y
  `standard` en pull requests/pushes, wheel instalado en el segundo y carril
  `deep` sólo semanal o manual sobre fixtures/contratos. Usa las Actions
  oficiales v7 y no relaja la identidad física local exigida por trusted-deep.
- Inventario Dedup v9: la publicación generacional ya no depende de USN. Un
  checkpoint conserva cursor completo o tres nulos; la corrida normal usa full
  scan portable cuando USN falta y mantiene Code/rutas incrementales por caché.
- Watcher portable en primer plano: USN queda como señal opcional; un checkpoint
  sin cursor o un lector USN indisponible dispara corridas normales programadas
  sobre Dedup v9, con intervalo explícito y sin otro índice.
- Clon Semantic reanudable: una generación con cambios fija la base publicada y
  persiste high-watermark, conteo y cursor por páginas bajo el deadline común.
- Pisos de abstención para recuperación Jina/body, separados por PDF y Code y
  aplicados sólo cuando modelo, pipeline, backend y owner coinciden exactamente.
- Puente Code↔Semantic integrado sobre `code.embedding_links`: una publicación
  textual completa sincroniza cobertura exacta por item, modelo, espacio,
  generación y versión vigente; el replay es idempotente y conserva enlaces
  históricos inactivos.
- Publicación Code quiescente: al completar un run hace checkpoint y retira
  sidecars reconstruibles con WAL vacío; un lector externo puede diferir la
  limpieza sin revertir el estado ya publicado ni forzar el cierre de handles.
- Búsqueda Code `semantic`/`hybrid` con canal de disponibilidad explícito,
  procedencia `retrieval_evidence_only`, similitud no calibrada y abstención si
  faltan head, cobertura o modelo local; el modo híbrido conserva las señales
  léxicas y estructurales disponibles.
- Proyección `self_analysis` en `--code-status --code-json`, con estados de
  manifest y frescura separada para raíz, framework/code, checkpoint y journal.
- `--code-review` v2 como consumidor read-only del autoanálisis publicado:
  conserva el ranking bruto, separa callers de producción/pruebas, clasifica la
  construcción y antepone hasta tres recomendaciones `act_now` con riesgo,
  contratos y validación. `--code-review-limit` expone de 1 a 50 resultados en
  JSON; builders, validators, reglas y evidencia desconocida no se promueven
  por score. Los avisos `probable_dead_symbol` permanecen suprimidos.
- `--code-review` v3 conserva los campos v2 y añade un `work_package`
  determinista sobre un top 50 fijo: una sola recomendación raíz y guards
  alcanzables exclusivamente por llamadas confirmadas a uno o dos saltos, con
  riesgo, contratos, orden, validación, gates, provenance y abstención explícita.
  La calibración rc13–rc19 fija el rechazo de hotspots sustitutos y replays no
  completamente incrementales.
- Caracterización v14 de Document Taxonomy con 30 payloads sintéticos completos
  y dos seams de ambigüedad. La partición de `classify_document` y de la regla
  normativa conserva firma, evidencia, orden, confianza, incertidumbre,
  abstención y naming; el autoanálisis rc20 retira ambos hotspots sin añadir otro
  ni perder o corregir resoluciones comunes.
- Partición transaccional de `_queue_job_rows_bounded`: el orquestador conserva
  orden, presupuesto de jobs, lazy base-member reuse y reanudación, pero delega
  carga, selección, rebind y escrituras a fases acotadas sin commits internos.
  La regresión post-upsert demuestra rollback del slice y el autoanálisis rc17
  reduce el símbolo de 302 líneas/complejidad 44 a 47/3 sin hotspot sustituto.
- Partición determinista de `knowledge_context._derive_context_graph`: un
  coordinador de nueve líneas delega validación de relaciones Code,
  acumulación y materialización sin cambiar orden, IDs estables ni rechazo
  atómico de evidencia inconsistente. El autoanálisis rc18 retira sólo ese
  hotspot, añade cero y conserva cero resoluciones corregidas o perdidas.
- Partición contractual de `knowledge_exact._lookup_catalog`: el wrapper baja
  de 225 líneas/complejidad 44 a 58/5 y delega preflight generacional,
  decodificación, cobertura y reportes sin cambiar firma, ranking, límites,
  provenance ni lectura estricta. El autoanálisis rc19 retira sólo el hotspot
  objetivo, no añade otro y conserva cero resoluciones corregidas o perdidas.
- Fixture reproducible de actionability para el primer top 10, con labels
  provisionales `actionable`/`defer`, score recalculado y `Precision@10`
  explícitamente no humana antes de modificar pesos o añadir detectores.
- Calibración ampliada sobre la unión de los top 40 de ranking v1/v2: 41
  símbolos etiquetados por construcción. El peso v2 prioriza complejidad sobre
  longitud y elevó `Precision@10` provisional de 0.60 a 0.70 sin degradar los
  cortes 20/30/40.
- `--code-publication-diff BASELINE_STATE` como comparación canónica, bounded y
  estrictamente read-only de dos publicaciones Code completadas, con cambios
  de resolución, hotspots, ejemplos acotados, limitaciones y digest portable.
- Fixture portable de 40 `probable_dead_symbol`: 36 usos demostrables, un
  contrato externo y tres candidatos de revisión. La señal falló el gate de
  precisión y permanece suprimida para recomendaciones de borrado.
- [Guía operativa del autoanálisis protegido](SELF_ANALYSIS.md), incluido un
  mini-root sintético reproducible.
- Topología canónica por usuario: fuente en
  `%USERPROFILE%\Neocortex\Repository`, runtimes versionados bajo
  `%LOCALAPPDATA%\Programs\Neocortex\versions`, launcher estable en `bin` y
  estado/autoanálisis bajo `%LOCALAPPDATA%\Neocortex`.
- `InternalPathsPolicy` con identidades físicas y firma versionada para
  repositorio, runtime, datos de aplicación, autoanálisis y launcher.
- Política de contenido protegido integrada en inventario, lectura y fronteras
  de mutación para que rutas internas o reservadas permanezcan fuera de las
  decisiones operativas sobre el corpus.

### Corregido

- La instalación base incorpora las herramientas del autoanálisis integrado:
  Complexipy, Cosmic Ray, Coverage, Deptry, Grimp, Mypy, Packaging, pip-audit,
  Pytest, Rich, Ruff, Semgrep, Vulture y xxHash. `documents`, `audio`, `image`,
  `semantic` y `ui` conservan extras explícitos, y `full` mantiene la unión
  compatible del runtime integrado. Pillow permanece declarado en cada dominio
  que lo importa.
- Knowledge status/search/context y los facades Semantic ya no cargan Pillow ni
  schemas de owners por imports eager al inspeccionar estado ausente ni al leer
  un owner `image` existente.
- Full scan y USN comparten una `InventoryExclusionPolicy` con firma cruda
  `inventory-exclusion-policy-v2:xxh3_128:...`; Framework y watcher ligan la
  firma efectiva que también incorpora `InternalPathsPolicy`. La puerta
  incremental exige último run durable, checkpoint/scan y raíz/cursor vivos.
  Un fallo fuerza full scan sin fallback histórico.
- La puerta incremental normal conserva USN como acelerador cuando la frontera
  durable coincide; ante plataforma no soportada o fallo de acceso publica un
  snapshot portable en vez de abortar, sin fingir journal ni ampliar permisos.
- Las lecturas operativas de Code ya no recrean sidecars en una base quiescente;
  usan immutable con cercas, y ante un writer activo conservan read-only sin
  borrar ni hacer checkpoint de auxiliares ajenos.
- La recarga del dueño durable entre ciclos del watcher usa también una
  instantánea immutable cercada; ya no recrea `framework.sqlite3-wal/-shm` al
  consultar una publicación quiescente y se abstiene si existe actividad WAL.
- La política de abstención Semantic permanece fuera del contrato durable del
  encoder; añadir o ajustar pisos de recuperación no invalida modelos ni heads
  ya publicados cuya firma vectorial sigue siendo idéntica. Los vectores
  reutilizados recuperan backend/pipeline desde su `payload_provenance` exacto;
  cualquier conflicto con el nivel superior queda sin calibrar.
- La decisión de imagen v10 deja de promover iconos transparentes y composiciones
  casi cuadradas a página documental sólo por píxeles claros; exige geometría
  plausible o evidencia textual firme e invalida la decisión cacheada anterior.
- Route-only/resume code acepta cero candidatos únicamente cuando todas las
  rutas seleccionadas consumen `inventory_snapshot`. Sin run explícito exige
  que el owner durable más reciente de la raíz exacta sea `normal`, sin fallback
  histórico; rutas MIME o mixtas fallan antes de crear el run.
- `CodeState.finalize_graph()` consulta cancelación mediante un progress handler
  limitado a su transacción, hace rollback antes de propagar y retira el handler
  al salir. La publicación generacional del grafo sigue fuera de este cambio.
- El analizador Python v5 conserva módulo/nivel relativo, binding léxico y
  aliases; el resolver de grafo v7 enlaza qualified names únicos, un salto de
  reexport confirmado y submódulos físicos únicos, absteniéndose ante shadowing,
  imports externos o ambigüedad. Mantiene además la resolución privada por
  módulo/clase sin confundir un alias con un homónimo del archivo llamador.
- Knowledge Plan conserva como requeridas las modalidades semánticas solicitadas,
  liga cada ranking a su propietario y path SQLite exactos, respeta
  `candidate_limit` y no permite que otro ranking del mismo canal sustituya una
  evidencia requerida. Un ranking exacto truncado o requerido ausente fuerza
  completitud parcial y warnings deterministas.
- `execute_knowledge_search` separa su orquestación en fases privadas acotadas
  sin cambiar firma, seams, orden de owners, reloj, cancelación, warnings ni
  resultado. El método público baja de 416 a 26 líneas y el autoanálisis retira
  su hotspot sin introducir otro.
- Los hotspots de Semantic Plan, Knowledge Planner, Knowledge Search y contratos
  Knowledge se dividieron en módulos con DAG unidireccional; las dataclasses,
  firmas públicas, `__module__`, pickle, JSON, IDs y excepciones permanecen en
  las fachadas compatibles.

### Persistencia y seguridad

- Framework pasa de schema 19 a 20 mediante una migración aditiva que preserva
  filas y añade modo, identidad, estado/firma de policy y snapshots protegidos
  de acción. Checks, triggers y `CorpusMutationGuard` impiden que un run
  `analyze_only` alcance owners de mutación. No existe downgrade; rollback
  requiere backup consistente y paquete compatible.
- Dedup pasa de schema 7 a 8 y después a 9: añade
  `scans.inventory_policy_signature`, conserva scans, archivos y bytes, e
  invalida checkpoints legacy sin firma en vez de inventar evidencia; v9 vuelve
  opcional únicamente la terna USN completa y rechaza estados parciales.
- La frontera normal rechaza un estado igual o ancestro del corpus y excluye
  los árboles internos descendientes; el autoanálisis mantiene disjunción
  completa entre raíz y estado.
- La finalización se abstiene salvo una única ruta code completada y ceros
  exactos en candidatos, acciones y organización. Run completado y manifest se
  publican en una sola transacción.
- El status de autoanálisis no crea ni migra: usa SQLite
  `mode=ro&immutable=1`, `query_only` y fences pre/post. Cualquier sidecar,
  incluso vacío o desacoplado, o una cerca inestable en code, framework o Dedup
  causa abstención total con código `2`, sin vista parcial ni cambios de estado.
- La promoción Windows retiene por handle los cuatro directorios autorizados
  —launcher, receipts, backups y lock— sin `FILE_SHARE_DELETE`, valida identidad
  y volumen en esos mismos handles y los libera en orden inverso después del
  lock, preservando la primera `BaseException` y anotando fallos de cleanup.

### Compatibilidad y límites

- Instalar `neocortex-framework[full]` mantiene las dependencias del runtime
  completo; la base mínima no habilita por sí sola rutas opcionales. Los probes
  ligeros declaran prerrequisitos, no certifican cachés ni modelos ejecutables.
- `elapsed_milliseconds` conserva su semántica legacy del broker retornado. El
  nuevo envelope es aditivo y observacional: no cambia IDs, JSON sin
  telemetría, texto renderizado, presupuestos ni schemas SQLite. Las fases
  anidadas no son aditivas y una campaña comparable aún debe fijar fixture,
  snapshot y condiciones de cache.
- La entrega eleva la versión pública de `0.7.1` a `0.7.2` sin cambiar los
  schemas SQLite vigentes. El launcher estable sólo debe promoverse desde un
  runtime versionado después de validar artefacto, dependencias, versión y
  ayuda; el rollback operativo conserva el runtime 0.7.1 como destino explícito.
- Los estados de autoanálisis pertenecen a árboles nuevos bajo
  `%LOCALAPPDATA%\Neocortex\self-analysis`, nunca dentro del repositorio; un
  mini-root no demuestra cobertura de la raíz canónica completa.

## [0.7.1] - 2026-07-26

### Añadido

- Frontera renderizada `untrusted-corpus-data-v1` que precede a todo
  `ContextBundle` y niega autoridad de instrucciones, herramientas y acciones a
  la evidencia recuperada.

### Corregido

- El analizador Python versión 2 emite símbolos de asignación sólo para nombres
  realmente enlazados, admite destructuring/`Starred`, omite atributos y
  subscripts y deduplica nombres repetidos.
- El vínculo entre run, inventario y candidatos de ruta se publica atómicamente
  después de completar la generación de candidatos. La reanudación valida scan,
  conteos e identidad de raíz; la recuperación legacy exige evidencia
  inequívoca y trabajo de ruta durable.
- Los subprocess acotados y workers aislados en Windows contienen el árbol
  propio mediante Job Objects kill-on-close asociados antes de reanudar el
  hijo, y cierran procesos, pipes y handles ante timeout, overflow o excepción.
- PDF e imagen cierran sus streams de candidatos en el mismo thread que creó la
  conexión SQLite, también al desenrollar error o cancelación; se elimina el
  cierre cross-thread observado durante la admisión de recursos.
- El staging semántico de texto reutiliza una sesión SQLite por fuente y limita
  cada transacción a 128 items o chunks, en lugar de abrir una conexión por
  operación. Fallo, cancelación y `BaseException` revierten el lote activo; el
  prefijo confirmado es reanudable y permanece fuera del head publicado.
- El worker semántico alcanza el punto fijo de reutilización exacta antes de
  cada claim, por lo que un payload producido por un batch satisface duplicados
  pendientes posteriores. También limpia leases propios ante
  `RuntimeError`, `KeyboardInterrupt` y `BaseException` sin enmascarar la causa.
  Duplicados ya reclamados dentro del mismo batch y escrituras por job siguen
  siendo límites conocidos.
- Un cache hit de código con ruta invariable realiza cero DML sobre `code_fts` y
  sólo actualiza presencia y observación. Cualquier cambio de ruta rechaza la
  caché; el procesamiento normal publica una versión sucesora y conserva
  inmutables la ruta y evidencia históricas.
- El resolver de símbolos y dependencias del grafo usa conjuntos TEMP indexados
  y joins set-oriented en lugar de consultas correlacionadas por relación;
  mantiene resultados ambiguos o no resueltos cuando no existe coincidencia
  única.
- Una reconciliación completa —sin límite ni selección— ejecuta `mark_missing`
  antes del grafo. La finalización reinicia membresías derivadas y, una vez
  resueltos sus conflictos, sincroniza sólo etiquetas FTS vigentes distintas
  mediante un mapa TEMP indexado y una única sentencia; moves y cambios de
  manifest no dejan proyectos obsoletos ni reescriben labels históricos.
- El fastpath estable del grafo exige cero cambios, todos los candidatos como
  cache hits compatibles con los analizadores del runtime y un fence tipado
  `code-graph-resolver-v3` ligado al run completo inmediatamente anterior, con
  la misma firma de procesamiento. Fence ausente, malformado o stale —incluida
  la primera corrida sobre una base existente— fuerza finalización completa.
- La caché comprueba el analizador realmente disponible en el runtime, por lo
  que la aparición de un parser opcional reemplaza el fallback sin cambiar la
  firma declarativa ni reprocesar archivos ajenos; los cache hits preservan los
  contadores `partial`, `text_only`, `binary`, `skipped_limit` y `error`.
- La finalización de runs de código usa compare-and-swap sobre el owner
  `running`; cancelaciones quedan como `cancelled` y un run fallido, duplicado o
  inexistente no puede avanzar el fence del grafo.

### Cambiado

- La firma del analizador Python se elevó a versión 2 para que los resultados
  previos con semántica de asignación distinta no se reutilicen como caché.

### Compatibilidad y migración

- La entrega eleva la versión pública de `0.7.0` a `0.7.1` sin crear bases ni
  cambiar schemas persistentes. El fence versionado se guarda en `metadata` y
  avanza en la misma transacción que completa su `analysis_run`.
- La firma Python v2 invalida deliberadamente la caché producida con la
  semántica anterior; la reconstrucción derivada no requiere una migración.
- La evolución del staging semántico no cambia schema, API Python, CLI ni JSON;
  conserva la publicación generacional v6 y sus datos son compatibles con el
  rollback.
- El rollback a `0.7.0` no exige downgrade SQLite; puede reprocesar código por
  firma y recalcular el grafo, pero las versiones sucesoras siguen siendo
  legibles por el esquema 2.
- Permanecen las limitaciones del esquema 2: publicación no generacional,
  transacción global sin cancelación dentro de una sentencia SQL, empates de
  resolución conservados como ambiguos y firma global del registro que puede
  invalidar lenguajes no afectados.

## [0.7.0] - 2026-07-25

### Añadido

- Contratos inmutables y serialización estable para recursos, revisiones,
  evidencias, hits, snapshots y bundles de contexto del plano Knowledge.
- Snapshot lógico cross-owner de inventario, framework, extractores, catálogo,
  semántica y código, con observación antes/después, un único reintento y
  estados explícitos para propietarios ausentes, futuros, incompatibles o
  corruptos.
- Planificador determinista y búsqueda unificada read-only con rankings por
  propietario, fusión RRF por evidencia, filtros, diversidad por recurso,
  historial opcional y abstención/completitud explícitas.
- Compilador acotado de `ContextBundle` con citas estables, locators exactos,
  presupuesto de caracteres, truncación visible y contradicciones estructuradas.
- Operaciones CLI `--knowledge-status`, `--knowledge-search` y
  `--knowledge-context`, con salida humana/JSON y códigos de salida
  diferenciados para ausencia de resultados, parcialidad, cambio de snapshot,
  esquema futuro/incompatible, corrupción y cancelación.
- Evaluación reproducible de 17 escenarios golden para recuperación, ranking,
  evidencia, citas, staleness, duplicados y abstención.

### Corregido

- La resolución semántica de código interpreta `section_id` como el
  `chunk_index` persistido y exige clase, versión e identidad exactas; ya no
  cae al primer chunk ni devuelve una revisión stale ante evidencia inválida.

### Cambiado

- El modo discovery conserva el colapso compatible por elemento; el modo
  evidence preserva entidades y evidencias distintas sin alterar el contrato
  previo de discovery.
- Las representaciones físicas de inventario, extractores, catálogo, semántica
  y código se normalizan a una identidad estable común antes de fusionar
  evidencia entre propietarios.
- Las relaciones de deduplicación del inventario v7 sólo influyen cuando el
  plan conserva verificación byte a byte; Knowledge se abstiene ante planes no
  verificados y ninguna relación autoriza una acción sobre archivos.

### Compatibilidad y migración

- La entrega es aditiva y eleva la versión pública de `0.6.0` a `0.7.0`; las
  búsquedas existentes y el modo discovery permanecen compatibles.
- No se crea una base nueva, no cambia ningún esquema y no existe migración de
  estado. Knowledge abre únicamente bases existentes en modo de consulta.
- El rollback consiste en instalar el paquete anterior: esta capa read-only no
  deja estado que revertir ni autoriza mutaciones mediante hits, evidencias o
  contexto.

## [0.6.0] - 2026-07-25

### Añadido

- Registro durable de observaciones de conciliación en
  `file_action_reconciliation_events`, append-only, con CAS de predecesor,
  clave idempotente, actor, procedencia, firma del conciliador y evidencia
  autocontenida. El registro nunca autoriza ni repite una mutación.
- Operación CLI explícita `--action-recovery-record`, separada del
  `--action-recovery-status` de sólo lectura, con confirmación de escritura,
  salida humana/JSON y códigos de salida conservadores.
- Planificador de retención `--retention-status` estrictamente dry-run para
  inventario, framework, catálogo y semántica, paginado por keyset y con
  publicaciones, builders, leases y evidencia protegidos.
- Inventario técnico reproducible de metadatos de licencias y artefactos de
  terceros; no se eligió una licencia para NeoCortex ni se creó `NOTICE`.
- Barrera de contención para fixtures nativos: raíz canónica única, rechazo de
  rutas fuera de ella, registro por identidad e inspección posterior de fugas.
- Guía de instalación offline que distingue validación `--no-deps`, entornos
  heredados y resolución hermética mediante un wheelhouse completo.
- Journal USN sintético contenido para probar coordinación, cancelación y
  reanudación sin abrir el volumen raw.

### Corregido

- Las observaciones sobre acciones `recovery_required` ya no desaparecen al
  terminar el proceso y dos writers no pueden avanzar el mismo frontier de
  conciliación de manera incompatible.
- Lectores y writers de SQLite inventariados aplican contratos explícitos de
  existencia, FK, `query_only`, timeout, rollback y cierre según el propietario;
  los diagnósticos endurecidos no crean una base ausente.
- La cobertura de ramas de riesgo incluye fallos, cancelación, concurrencia,
  migración y rollback de conciliación, contención, retención y conexiones.
- El reinicio distingue una intención `started` abandonada antes de la frontera
  —fallo sin efecto intentado— de una acción `applying` incierta; las
  transiciones repetidas con evidencia contradictoria ya no se aceptan como
  idempotentes.
- Un recibo de Papelera sólo puede confirmar la acción cuyas rutas liga, y el
  lector de conciliación se abstiene ante un esquema framework futuro o
  metadata de versión no canónica.
- El planner de retención bloquea generaciones semánticas referenciadas por
  evidencia y conserva el último run completado de framework. La poda
  owner-local de inventario falla cerrado sin holds cross-store explícitos y,
  cuando se proporcionan, conserva publicación vigente, anterior y scans
  referenciados.
- La cancelación durante una consulta SQL del planner se normaliza como
  `RetentionPlanningCancelled` en lugar de escapar como error SQLite genérico.

### Cambiado

- Framework pasa de esquema 18 a 19 mediante una migración aditiva y
  transaccional que crea el log de conciliación sin reinterpretar acciones
  legacy.
- La versión pública pasa de `0.5.0` a `0.6.0` por el nuevo contrato CLI y el
  esquema framework 19.
- `finalize_graph` conserva deliberadamente su transacción global: los
  benchmarks y pruebas de snapshot/rollback no justificaron fragmentarla sin
  diseñar antes un esquema generacional completo.
- Esta continuación no cambia la CLI pública, los esquemas ni la versión
  `0.6.0`; endurece contratos internos y sus regresiones.

### Compatibilidad y límites

- `--action-recovery-record` puede migrar una base framework existente desde
  un esquema soportado después de la confirmación explícita; no crea una base
  ausente. El rollback exige restaurar un backup SQLite consistente y el
  paquete anterior; nunca editar números de versión.
- Sólo se persisten observaciones de conciliación. No existen todavía comandos
  `decide`, `authorize`, `recover` o `verify`, ni una autorización durable
  para ejecutar una nueva mutación.
- Retención no elimina, no aplica cuotas, no ejecuta `VACUUM` ni hace
  checkpoints. Los bytes informados son una cota inferior del payload SQLite,
  no espacio físico garantizado como recuperable.
- No existen todavía `retention prepare/apply/verify`; el planner no autoriza
  un `DELETE` manual ni sustituye un journal reanudable por propietario.
- El grafo de código permanece en esquema 2 y `NC-AUD-015` sigue abierto como
  trabajo de diseño generacional.
- La instalación global observada no fue actualizada por esta auditoría.

## [0.5.0] - 2026-07-24

### Añadido

- Primitiva Windows de rename/move ligada a handles retenidos, FileId y volumen,
  relativa al directorio destino y sin reemplazo.
- Evidencia de frontera y recibo en `file_actions`, clave idempotente y bitácora
  `file_action_events` protegida contra update/delete.
- Conciliador CLI read-only `--action-recovery-status`, con paginación/filtro de
  run, salida humana/JSON Lines y clasificaciones explícitas.
- Publicación generacional semántica v6 mediante revisiones congeladas, miembros
  de generación y un head CAS por modelo.
- Publicación generacional de catálogo v6 mediante staging por fuente, puntero
  CAS y proyección `documents` compatible.
- Lease de vida cross-process del watcher por raíz+estado, con lock del sistema
  operativo, metadatos acotados y recuperación conservadora de metadata stale.

### Corregido

- Rename de extensión y organización ya no caen a una syscall final por ruta:
  operan sólo sobre archivos regulares de un hard link en NTFS local y mismo
  volumen; UNC, otros filesystems, reparses, directorios y cross-volume se
  abstienen.
- Un fallo después de la frontera nativa conserva `recovery_required` en lugar
  de permitir que la acción se clasifique como fallo sin efecto o se repita.
- Los planes documentales inciertos reservan su destino y no vuelven a entrar al
  selector automático.
- Las búsquedas semánticas oficiales no observan miembros parciales y los
  lectores del catálogo mantienen la generación anterior durante fallo,
  cancelación o conflicto de publicación.
- La resolución semántica conserva evidencia publicada inmutable, pero usa la
  ruta viva sólo cuando coincide la identidad completa; después de organizar un
  archivo no devuelve el origen obsoleto ni acepta un `SearchHit` con espacio o
  modalidad distintos del modelo persistido.
- `semantic_status` usa una sola conexión y snapshot WAL para conteos,
  generaciones y summaries; los summaries se agregan en una consulta acotada,
  eliminando el patrón N+1 de conexiones y statements.
- Ocho factories de subsistemas y las conexiones principales de framework
  verifican FK, `query_only` en lectura y cierre ante aborto de configuración,
  sin reconstruir tablas ni abrir bases operativas.

### Cambiado

- La aplicación de candidatos de Papelera está deshabilitada: el dry-run sigue
  planificando, pero `--apply` registra abstención `skipped` porque la API
  disponible es path-bound. Se retiró `Send2Trash` de las dependencias.
- La versión pública pasa de `0.4.1` a `0.5.0` por el cambio deliberadamente
  restrictivo de mutaciones y por los esquemas framework 18, catálogo 6 y
  semántica 6.

### Migración y compatibilidad

- Framework 17→18 valida el layout conocido, preserva conteos y agrega columnas
  de recuperación; no inventa recibos para acciones legacy.
- Catálogo 5→6 exige un contrato v5 exacto, importa una generación publicada por
  fuente, conserva historial/planes y valida conteos/FK.
- Semántica 5→6 exige el contrato e historial exactos, importa únicamente la
  vista legacy activa y consistente por hash, valida conteos/FK/integridad y
  conserva tablas legacy.
- Las tres migraciones son transaccionales y se abstienen ante objetos no
  comprendidos. No existe downgrade por edición de versión: restaure el backup
  completo y el paquete compatible.
- La distribución global observada permaneció en `0.3.0`; esta auditoría no la
  actualizó ni promovió `0.5.0` fuera de entornos de validación.

### Limitaciones conocidas

- No existe todavía política global de retención/poda para generaciones
  fallidas, parciales o abandonadas (`NC-AUD-014`).
- `finalize_graph` conserva su transacción global (`NC-AUD-015`) y algunas
  conexiones/FK mantienen políticas desiguales (`NC-AUD-017`).
- El conciliador no modifica acciones, no persiste todavía una transición o
  evento `reconciled` y no autoriza reintentos; los planes de organización
  inciertos requieren revisión manual.
- SQL externo sobre tablas legacy no recibe la garantía generacional de los
  repositorios oficiales.
- El proyecto no declara licencia propia ni se añadió una durante esta entrega
  (`NC-AUD-021`).

## [0.4.1] - 2026-07-24

### Añadido

- Guía de la CLI canónica con rutas, operaciones directas, ámbitos JSON, códigos
  de salida y ejemplos comprobables.
- Guía operativa para ejecución normal, watcher en primer plano, cancelación,
  reanudación, límites de recursos, diagnóstico y mantenimiento.
- Guía de recuperación y rollback con copias consistentes mediante la API de
  backup de SQLite, validación de integridad y tratamiento conservador de
  acciones cuyo resultado sea incierto.
- Guía de seguridad para simulación, autorización de mutaciones, límites de
  confianza de evidencia semántica y riesgos residuales de TOCTOU.
- Estándar permanente para el cierre de auditorías, con informe técnico fechado,
  resumen visible, manifiesto, evidencia y barrera de validación.
- Publicación generacional del inventario deduplicador mediante el esquema 7,
  con lector público `published_snapshots(root)` y migración conservadora desde
  el esquema 6.

### Corregido

- Las generaciones de inventario ya no se sobrescriben por una ruta global; el
  checkpoint sólo selecciona una exploración completa con conteos y bytes
  consistentes.
- Las exploraciones parciales no se publican ni se anuncian como completas, y
  un lote USN ambiguo no se aplica ni adelanta el último cursor durable seguro.
- La migración v1→v2 del historial del catálogo preserva filas conocidas y se
  abstiene ante columnas, triggers u objetos reservados desconocidos.
- Las acciones interrumpidas después de registrar intención quedan como
  `recovery_required`; el estado terminal ya no interpreta un PID reutilizado
  como propietario vivo y muestra el conteo incierto.
- Las conexiones de sincronización de cachés y derivados PDF activan claves
  foráneas; el lector aislado de perfiles PDF abre SQLite en modo de sólo
  lectura.
- La fuente semántica de imágenes selecciona sólo la generación de inventario
  válida más reciente y no mezcla filas de generaciones no publicadas.
- Las validaciones de argumentos posteriores al parseo usan el mismo código de
  salida `2` que `argparse`.

- La documentación de deduplicación distingue ahora el modo `fast`, que aplaza
  la comparación byte a byte hasta la aplicación, del modo `exact`, que la
  realiza al construir el plan. La aplicación sigue requiriendo verificación
  exacta antes de eliminar un duplicado.
- La misma documentación describe la exclusión de forma precisa: se protege el
  subárbol `AppData` del perfil efectivo; no se excluye por nombre cualquier
  directorio arbitrario llamado `AppData`.
- Los procedimientos de copia y restauración dejan de sugerir que copiar sólo
  un archivo `.sqlite3` abierto sea suficiente cuando existen WAL y SHM.
- La documentación operativa hace explícito que el watcher actual permanece en
  primer plano y que la primera preparación semántica o de audio puede adquirir
  modelos si no se activa el modo exclusivamente local.

### Compatibilidad

- La versión fuente y de paquete pasa de `0.4.0` a `0.4.1` por correcciones de
  integridad y por la migración de inventario 6→7; no se elevó por la auditoría
  documental por sí sola.
- Al primer uso escritor con `0.4.1`, `dedup.sqlite3` esquema 6 migra de forma
  transaccional a 7. Realice antes un backup SQLite consistente. Un esquema 6
  con estructura desconocida se conserva y la actualización se abstiene.
- Antes de una actualización debe comprobarse que el ejecutable `Neocortex`
  resuelve a la distribución esperada. La instalación pública `0.3.0`
  observada al iniciar la auditoría no representaba este árbol.
- No se documenta ni se admite un downgrade basado en alterar manualmente
  `PRAGMA user_version`.

### Limitaciones conocidas

- Permanecen riesgos abiertos de TOCTOU en syscalls por ruta, conciliación
  automática de efectos inciertos, visibilidad parcial semántica y de catálogo,
  transacción global del grafo de código, aplicación desigual de claves
  foráneas y retención incompleta.
- La evidencia semántica y probabilística es asesora: no autoriza por sí sola
  movimientos, renombres, reemplazos ni eliminaciones.
- No se ejecutó ninguna mutación sobre el corpus vivo para preparar estas guías.

## [0.4.0] - sin fecha de publicación verificada

La versión está declarada en el paquete fuente y en `pyproject.toml`. No se
reconstruye aquí un historial de cambios sin evidencia documental verificable.
