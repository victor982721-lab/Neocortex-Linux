# NeoCortex — handoff operativo actual

> Actualizado: 2026-08-14. El nombre del archivo es histórico y se conserva
> como ruta estable. Este documento es la fuente única de la frontera vigente;
> no guarda un SHA de cierre porque Git y la release instalada deben
> demostrarlo dinámicamente.

## Preferencia operativa de Víctor

- GitHub conserva únicamente `main`; no se usan PR ni ramas para la evolución
  ordinaria de este proyecto personal.
- Cada entrega se integra mediante commits atómicos directos en `main`, una
  release Linux del SHA final exacto, el launcher público y los gates locales
  verificados.
- GitHub Actions está prohibido y deshabilitado. Windows es legado fuera del
  alcance activo; toda validación vigente se ejecuta localmente en Linux.
- El estado vivo, el corpus y el launcher instalado son el SSOT operativo.
- En Linux no se usa `--apply` ni `--organization-apply`. Resultados de
  búsqueda, OCR, modelos o similitud nunca autorizan una mutación.
- Originales y estado publicado se preservan; fixtures, benchmarks y pilotos
  sólo autorizan la siguiente prueba acotada, no una promoción automática.

## Veredicto y frontera 0.9.0

La auditoría externa de 0.8.0 fue correcta en su diagnóstico central:
NeoCortex ya era una plataforma local de conocimiento madura y fail-closed,
pero los gates locales, supply chain, rendimiento del guard y ciclos de imports no estaban al
nivel de su amplitud. La campaña 0.9.0 cerró esos cuatro bloqueos y añadió un
piloto de producto medido, sin habilitar mutación Linux ni fabricar calidad de
modelos.

La versión fuente es `0.9.0`. Su cierre no se infiere de este documento: sólo
existe cuando se cumplen juntos los criterios dinámicos de la última sección.

## Cinco fases completadas

### Fase 0 — supply chain y fronteras de arquitectura

- El runtime principal usa `mcp==1.29.0`; el intercambio MCP stdio real cubre
  initialize, listado, llamada y cierre sin depender de transporte HTTP,
  WebSocket ni tareas experimentales.
- Semgrep `1.172.0` se retiró del runtime principal porque fija
  `mcp==1.23.3`. Vive en un tool-runtime administrado, scan-only, fuera de
  `PATH`, con wrapper, lock, inventario, artefactos y receipt verificados.
- Las tres vulnerabilidades MCP del tool-runtime son excepciones explícitas,
  no alcanzables por su superficie, y vencen el **2026-09-30**. El runtime
  principal no hereda ninguna excepción.
- Todo bootstrap mantenido parte de un venv sin pip y autentica el wheel oficial
  `pip 26.1.2` por nombre y SHA-256 antes de ejecutarlo. La release y los gates
  locales Linux usan `python -I tools/bootstrap_pip.py`.
- Pyright `1.1.411` se instala con Node `24.18.1` desde manifest y lock npm
  versionados. Se exige integridad exacta, `npm ci`, scripts deshabilitados,
  entorno hostil limpiado y verificación viva antes del gate estático.
- El empaquetado separa base, `agent` y `analysis`; la instalación personal
  canónica sigue usando `full`. Semgrep permanece aislado incluso de `full`.
- La distribución es privada (`LicenseRef-Proprietary`, clasificador
  `Private :: Do Not Upload`) y declara autor, mantenedor y URLs válidas.

### Fase 1 — barreras integrales locales históricas

- El inventario dinámico final contiene **265 archivos de prueba**. La corrida
  integral de cierre ejecutó **4,138 passed, 144 skipped y 98 subtests**.
- El baseline branch-aware mínimo aprobado sigue en **56,933/67,720 líneas** y
  **14,600/21,110 ramas**; la corrida de cierre quedó por encima, en
  **56,957/67,731 líneas** y **14,604/21,114 ramas**. También ratchetea las rutas
  aprobadas de tests y de
  las **326 fuentes de producción**: las adiciones pasan; un retiro exige una
  reescritura explícita y revisada.
- Los dos shards son una partición exacta: 132 archivos con 2,003 passed,
  97 skipped y 83 subtests; 133 archivos con 2,135 passed, 47 skipped y
  15 subtests. La suma coincide exactamente con la suite completa.
- Los shards locales son una partición reproducible del inventario completo;
  permiten distribuir la suite sin convertir una plataforma remota en gate.
- El gate local construye e instala el wheel `full`, ejecuta un smoke fuera del
  checkout sobre seis paquetes, `Orquestador`, package-data, metadata, versión
  y entrypoint, y después prueba el árbol fuente completo. Revalida SHA y
  worktree limpio al final y tras Coverage.
- El gate estático versionado conserva deuda sin permitir crecimiento:
  límites Ruff **76**, Mypy **94**, Pyright **142**; la corrida de cierre observó
  Ruff **69**, Mypy **94** y Pyright **142**. Pyright queda ligado explícitamente
  a los paquetes del intérprete canónico y eliminó 72 falsos missing-import. No
  significa “cero deuda”; significa
  cero diagnósticos nuevos por ruta/regla y versión exacta de cada herramienta.
- Grimp exige el baseline acíclico v2, seis contratos exactos y evidencia viva.
  Estado final: **325 módulos, 1,316 relaciones, 0 violaciones y 0 SCC**.
- Supply chain real se evalúa localmente en cada SHA con `pip-audit` del runtime
  principal y del tool-runtime Semgrep contra su receipt/policy; Coverage JSON
  se conserva como evidencia local ligada al SHA.

### Fase 2 — rendimiento sin perder seguridad TOCTOU

- El guard de mutación conserva revalidación de root, identidad, componente,
  contención y no-replace por candidato, pero reutiliza el snapshot inmutable
  de la frontera por lote.
- El caso grande pasó de aproximadamente **113 s** a **8.6 s** y conserva un
  máximo de seis reconstrucciones completas.
- Una primera optimización reveló dos regresiones de sustitución entre preflight
  y uso. Se corrigieron antes de aceptar el cambio y ambas pruebas TOCTOU pasan;
  el batching no convirtió una mejora de velocidad en autorización por ruta.
- La verificación sobre el estado vivo reveló además una poda cuadrática en la
  ruta Text: eliminar 30,248 filas antiguas hacía un scan completo del FTS por
  cada identidad y excedió dos límites de 15 minutos. La poda ahora usa dos
  `DELETE` set-based, conserva atomicidad y quedó protegida por una regresión
  que exige un número constante de sentencias. La corrida integral terminó en
  aproximadamente 3 min 56 s y el replay enteramente cacheado en 43 s.

### Fase 3 — arquitectura acíclica

- Se eliminaron, sin mover los owners públicos, los SCC de contratos
  semánticos, Knowledge, políticas de corpus y el ciclo central de 14 módulos.
- Protocols estructurales de sólo lectura y modelos hoja fijan la dirección de
  dependencias. Reexports conservan identidad y compatibilidad pública.
- El baseline histórico de cuatro ciclos quedó sustituido por
  `KNOWN_CYCLE_BASELINE = ()`; reintroducir cualquiera de ellos vuelve a fallar
  el contrato principal.

### Fase 4 — pilotos de producto y persistencia

- La página **Consulta** de la GUI ejecuta lecturas fuera del hilo Qt, permite
  cancelación y copia y conserva modo sólo lectura. El render offscreen
  1440×900 fue inspeccionado visualmente.
- Un piloto aislado de ocho videos reales produjo 7 completos y un error
  esperado de contenido sólo-audio, 4 `visual_only`, 29 frames, 13 keyframes,
  26 intervalos y OCR positivo en 27/29 intentos. El replay obtuvo 8 cache hits,
  0 OCR nuevo y terminó en aproximadamente 1.1 s.
- Audio enlazó los mismos ocho elementos: 1 transcrito, 3 `no_speech`, 4
  `no_audio`, 0 errores. La consulta `MALPASO transformer` recuperó evidencia
  OCR/frame, aunque con ruido; la muestra no demuestra todavía OCR DE/ZH fuerte.
- Dedup schema 10 añade dos índices identity-bound para los joins productivos
  de Knowledge. En una copia poblada v9→v10 preservó 414,421 archivos,
  347,239,389,995 bytes, 3,518 miembros planeados y FK/integridad; migró en
  5.40 s y la consulta medida bajó aproximadamente de 1.33 s a 0.106 s.
- Fresh v10, v9→v10, cadena 1→10, rollback, DDL adversarial y `EXPLAIN QUERY
  PLAN` productivo tienen regresiones específicas. La migración del estado vivo
  sólo ocurre dentro del cierre protegido de release descrito abajo.

## Corte evolutivo actual — Reproducible Derivations v1

- Text schema 2 registra la cadena owner-local `RevisionRef -> text.extract/v2
  -> text_representation/text_fts`: intento durable, bindings exactos,
  materializaciones, heads, `WorkReceipt` terminal y outbox se confirman sin
  falsa atomicidad cross-owner. En el provider builtin, cache/replay exigen
  outputs físicos vigentes y causación exacta; Office heredado se mantiene
  `non_replayable` y nunca reutiliza éxitos ni fallos. Fallo, cancelación, crash
  y rollback no publican outputs parciales.
- La migración Text 1→2 conserva documentos/FTS y deja el legado explícitamente
  `legacy_unattributed`; no inventa receipts. Los readers validan facts
  normalizados, receipts canónicos, publicación física e identidad histórica y
  permanecen read-only.
- Semantic schema 7 agrega receipts/outbox, revisiones inmutables y manifests
  causales para chunks, embeddings y generaciones. El enlace Text nuevo
  conserva su revisión y materialización owner-native. Staging/restage no
  sustituye la verdad publicada antes de `finalize`.
- Los payloads vectoriales pre-v7 no reciben receipts durante la migración. Una
  reutilización compatible exige attestación owner-local bajo demanda que
  valida sus hechos físicos y no finge haber ejecutado el modelo histórico.
- `Neocortex inspect lineage IDENTIFICADOR` y
  `neocortex.read_api.lineage_payload()` explican ejecución/reutilización,
  dependencias, publicación y staleness mediante una proyección acotada,
  descartable y reconstruible. Ayuda, versión y superficies ligeras conservan
  carga lazy.
- Certificación local del árbol final previo al commit: 271 archivos de prueba;
  4,271 passed, 144 skipped y 109 subtests; Coverage 59,779/71,019 líneas y
  15,410/22,270 ramas; 331 fuentes de producción y 271 tests ratcheteados.
  Los shards exactos sumaron lo mismo: 2,171/56/96 y 2,100/88/13. Grimp observó
  330 módulos, 1,347 relaciones, 0 violaciones y 0 SCC. Static quedó en Ruff
  67, Mypy 94 y Pyright 142, sin aceptar diagnósticos nuevos.
- El alcance cerrado es la extracción/publicación Text y su cadena Semantic
  nueva. `normalize` no es todavía un stage durable independiente; PDF, DOCX,
  Office y el historial Semantic pre-v7 no se presentan como linaje completo.
  El SHA comprometido, los gates locales y la release Linux instalada siguen
  siendo la evidencia dinámica de entrega, no este texto.

## Corte evolutivo actual — CapabilityManifest + CapabilityBroker v1

- El árbol fuente incorpora contratos stdlib-only, inmutables y acotados para
  manifests, requests, política, readiness, evaluación y selección/abstención.
  Sus serializaciones canónicas exponen fingerprints de request, política,
  manifest y selección; el broker no importa engines, abre estado ni descarga
  modelos.
- La única integración productiva de este corte es `text.extract/v2`, con
  manifests estáticos para `neocortex.text.builtin` y
  `neocortex.text.legacy-office-worker`. La selección ocurre por candidato y
  aplica plataforma, schemas, MIME/tamaño, reproducibilidad, readiness y la
  política `neocortex-text-local-v1`: local-only, sin red y sin GPU. El builtin
  permanece `environment_bound`, `incremental=true` y cacheable; el worker v2
  declara `best_effort`/`non_replayable`/`incremental=false`. Las solicitudes
  exigen incrementalidad sólo para MIME builtin.
- Texto/EML sigue funcionando sin LibreOffice. DOC/XLS/PPT heredado sólo elige
  el worker si observa y fija `soffice`/`libreoffice` o el backend exacto del
  MIME; el worker verifica su SHA-256/tamaño antes y después y no cambia de
  backend silenciosamente. Ante ausencia se abstiene, confirma el receipt de
  fallo y no publica materializaciones ni heads.
- La identidad Office atesta únicamente el launcher seleccionado, no una
  clausura transitiva arbitraria de engines, librerías o procesos descendientes.
  Por ello Text vuelve a ejecutar siempre Office heredado y no consulta su caché
  de éxito ni de fallo, incluso cuando la firma no cambió; el receipt conserva
  `non_replayable`. Office sigue seleccionable, pero el coste explícito es que no
  procesa únicamente candidatos legacy cambiados.
- Los receipts Text ligan provider y versión, fingerprint de manifest,
  política, readiness y fingerprint de selección con la firma de
  procesamiento. El fingerprint del manifest no se presenta como digest de
  implementación; la identidad del ejecutable Office se registra por separado
  sin sobreafirmar certificación supply-chain.
- `Neocortex doctor capabilities --select text.extract --mime-type MIME
  --input-bytes BYTES [--json]` explica la decisión con schema
  `neocortex.capability-selection/v1`. El reporte agregado sin `--select`
  conserva schema 1 y no construye el broker; ambos son ligeros y no crean
  estado.
- **PLANNED:** PDF, DOCX, la ruta Office, Semantic y plugins/providers externos
  todavía no consumen este broker. No existe autodescubrimiento de plugins ni
  se añadieron dependencias pesadas obligatorias.
- Este handoff describe el comportamiento del árbol; no sustituye tests, gates,
  commit ni release Linux instalada del SHA final.

## Corte evolutivo actual — ReviewTask durable + Value

- Framework schema 21 incorpora `review_task_batches`, `review_tasks`,
  `review_task_batch_memberships`, `review_task_events`,
  `review_task_scan_progress` y `review_task_source_publications` dentro del owner existente;
  no crea un datastore transversal. Batches/receipts están acotados a 1,000
  inputs y 100 tareas, las tareas son versiones inmutables y los eventos son
  append-only con CAS. La migración 20→21 crea el contrato vacío sin interpretar
  revisión histórica como tareas nuevas.
- La primera vertical productiva es Value. `Neocortex review value --refresh
  --scope personal|framework` avanza exactamente una página keyset de 100
  observaciones y puede crear/migrar Framework. Rechaza `all`, vuelve a leer el
  fence de Inventory/Catalog antes de publicar y sólo escribe batches, tareas,
  eventos y progreso owner-local; nunca muta corpus ni owners fuente.
- `review value` sin `--refresh` sigue estrictamente read-only. Usa la cola si
  coincide con el fingerprint fuente y, si Framework v22 o la cola todavía no
  existen, conserva el preview legacy sin DDL. El epoch de un scan incompleto
  persiste al cruzar medianoche; al cambiar fuente o política, el último head
  completo permanece visible como `stale` mientras se construye el siguiente.
- El cursor durable permite recorrer más de 25,000 observaciones sin retirar el
  límite que antes producía `scope_too_broad`. Cada invocación explícita avanza
  sólo una página; replay de un batch idéntico es idempotente.
- Fin de cursor y completitud de evidencia se persisten por separado. Si una
  página observa owner ausente, plan inválido o mismatch, la corrida completa
  permanece `partial` y no supersede pendientes por ausencia aunque una página
  posterior llegue al final del keyset.
- `review task show/history/claim/decide` cierra el journey CLI con actor y CAS.
  `RESOLVED` y `DISMISSED` guardan scope `permanent`,
  `until-source-change` o `until-policy-change`; sólo una expiración comprobada
  permite successor receipt-backed. Retry exacto es idempotente y las
  decisiones legacy permanecen terminales. `SUPERSEDED` sigue reservado a
  receipts sistémicos.
- El snapshot Knowledge de Framework v21 valida y expone heads ReviewTask y
  watermarks de batches, eventos y publicaciones fuente; conserva lectura
  legacy validada para v19/v20. Retention protege tareas y eventos humanos como
  holds y, por separado, el head vigente con toda la cadena alcanzable de
  batches, memberships y progreso exactos. La auditoría está acotada y falla
  cerrado ante receipts o vínculos históricos corruptos.
- Video OCR ya participa en la búsqueda Knowledge canónica con locator temporal
  y estado owner fail-closed; no se añadieron embeddings Video. **PARTIAL /
  PLANNED:** esto no completa `Knowledge Asset Health`; ReviewTask para OCR,
  entities/claims, contradicciones, links, recovery y promociones shadow, así
  como una GUI para decidir tareas, siguen pendientes.
- Este handoff documenta el árbol sin sustituir tests focales, gates, commit ni
  release Linux instalada del SHA final.

## Contrato operativo — Autoanalizador v19

- La siguiente frontera operativa es `Neocortex code validate`: una sola entrada
  Linux para validar implementaciones. Captura el diff, selecciona pruebas con
  evidencia publicada, ejecuta estática/arquitectura, publica `trusted-deep`,
  consume review v19, ejecuta experimentos allow-listed, instala el wheel
  candidato fuera del checkout y exige replay. Las herramientas individuales
  quedan como diagnóstico interno; no constituyen una aceptación paralela.
- Los deltas portables `added/resolved` siguen siendo evidencia histórica
  advisory: pueden usar una publicación comparable anterior a `HEAD^` y variar
  al mover coordenadas. La barrera estática bloqueante es el baseline
  versionado por path/regla/conteo que `code validate` ejecuta antes del review;
  un provider `ready` no falla sólo por conservar un delta global distinto de
  cero.
- La entrada completa se reejecuta dentro de un único cgroup v2 de usuario. Un
  preflight adaptativo reserva memoria para KDE/Chrome, sólo permite una corrida,
  limita memoria/swap/CPU/tareas/tiempo y detiene todos los descendientes si el
  watchdog observa pérdida de headroom o PSI crítico con memoria física
  amenazada. Su admisión queda en el recibo
  `neocortex.code-validation-resources/v3`; el worker comprueba su unit exacto
  en `/proc/self/cgroup`, consulta en systemd el `PrivateNetwork=yes` y prueba
  que la restricción `AF_UNIX` deniega AF_INET/AF_INET6, por lo que ni un
  receipt de entorno ni una propiedad declarativa pueden fingir contención. No
  existe fallback sin contención. La denegación comprobada elimina el egress IP;
  `code validate` resuelve sólo evidencia supply ya publicada y fresca.
- La promoción de cualquier corte requiere el comando verde sobre el diff,
  commit directo a `main`, repetición con `--baseline HEAD^`, release Linux del
  SHA exacto, launcher público verificado, push único y coincidencia
  `HEAD=main=origin/main=current`. Ninguno se infiere de este handoff.

- `Neocortex --state-directory ESTADO --code-review` ya no es una vista que
  convierte nombres, rutas o tamaño en recomendaciones. El envelope
  `neocortex.code-review/v19` publica un registro general de preguntas y
  evaluaciones enlazadas a registros fuente; separa observación, inferencia,
  hipótesis, contraevidencia, evidencia faltante, experimento, decisión y
  autoridad. Toda evaluación es advisory y `mutation_authority=false`.
- El camino estructural sólo confirma umbrales de funciones y clases. No existe
  `act_now`, no hay recomendación semántica ni package hotspot; los únicos
  packages posibles son caracterizaciones de código probablemente no usado,
  calibradas y sin pasos de cambio.
- La cobertura estructural ahora incluye módulos, configuración y superficie
  CLI estática; los artefactos no parseados y la ausencia de contratos runtime
  quedan explícitos. Los nombres de las preguntas describen la evidencia
  pendiente, no afirman que la superficie ya sea completa.
- Arquitectura conserva el consenso Ruff Analyze/Grimp, seis contratos y el
  grafo físico. Un registro versionado y explícitamente parcial declara seis
  logical owners sin owner predeterminado; módulos sin mapping y cruces entre
  owners permanecen observaciones, no ownership inferido por path.
- Estado incorpora un registro público de 13 stores, una pregunta exacta sobre
  cierre relacional de la publicación terminal Text y otra sobre la proyección
  publicada Text→Semantic. Un test aislado mata el proceso después de un
  prefijo durable y verifica aislamiento del head y convergencia al reanudar;
  no se presenta como prueba de pérdida de energía.
- Evolución separa cambio de contenido, relocation e interfaz pública entre
  publicaciones comparables; Git/co-change abstiene si su ventana excluye
  commits grandes. Schema v1 observa el DDL y ledger del owner Code, no finge
  snapshots históricos de los demás stores.
- Assurance distingue tests que ejecutan líneas de tests que demuestran un
  invariante; Coverage/mutación faltante produce abstención. Supply chain
  conserva Semgrep, Deptry, pip-audit e inventario instalado por separado;
  seguridad se abstiene si pip-audit no resuelve, aunque Semgrep haya pasado.
  Una falla exclusivamente de red sólo puede resolverse con un audit publicado
  aún vigente, cero vulnerabilidades, inventario instalado exactamente idéntico
  y ningún cambio de packaging/política supply; el receipt conserva el run y
  digest reutilizados.
  El inventario local se reobserva en ambos runs y el replay exige igualdad
  semántica completa, normalizando sólo reloj/ID efímero de captura.
- La primera ruta de capability reachability liga `text.extract` con intentos,
  receipts y heads publicados. El autoanálisis compara su publicación con el
  checkout Git por contenido y expone coste/cobertura, pero no publica
  precision, recall ni utilidad humana sin outcomes independientes.
- v19 conserva ownership lógico explícito, interacciones SQL/SQLite ligadas a
  los 13 stores declarados, fronteras transaccionales/workflow, reachability de
  las nueve rutas built-in, cuatro invariantes, calibración anti-Goodhart y un
  planificador de experimentos sin comandos libres. SQL dinámico, parser no
  disponible, provider stale o evidencia truncada producen abstención. Los
  placeholders numerados válidos `?NNN` se adaptan token a token para SQLGlot
  sin alterar strings ni el digest observado; dejaron de contarse como error
  los dos sitios productivos Text/Archive que usaban esa sintaxis.
- `--code-experiment-run PROPOSAL_ID` reconstruye el plan vigente y sólo admite
  seis templates ejecutables: contratos de imports declarados (tres nodeids y
  cuatro gates), acceptance pública Text (un nodeid), trace/fault
  boundaries del workflow Text (cuatro nodeids), recuperación Semantic ante
  muerte del proceso durante staging (un nodeid con tres gates), una matriz
  Code-owner de migración poblada/rollback/schema futuro (cinco nodeids y cuatro
  gates) y Retention durable en dry-run (nueve nodeids y cuatro gates). Retention
  comprueba holds declarados, fallos cerrados y lectura concurrente sobre
  fixtures; no autoriza ni valida borrado. La aceptación arquitectónica preserva
  como límites el dispatch dinámico y la intención no declarada. Los escenarios
  restantes del assurance de invariantes y los controles de calibración
  permanecen en el registry, pero no se convierten automáticamente en runners. Pytest corre
  directamente sobre el checkout canónico confiable; el temporal externo aloja
  runtime/checkpoints y no constituye copia ni sandbox. Trusted-deep puede usar
  red y conserva `HOME`. El provider verifica antes/después la firma exacta de
  los inputs Python publicados y del soporte Git observado; un fence Linux de
  identidad, sidecars y anclas acotadas cerca `code.sqlite3` durante la fase
  ejecutora sin releer todo el store. No hay lock continuo
  del checkout y corpus/otros stores quedan fuera.
- Code schema v6 persiste después cada terminal
  `neocortex.code-experiment-receipt/v3` en una tabla append-only y devuelve
  `neocortex.code-experiment-store/v1`. Por eso `code_database_unchanged=true`
  no vuelve read-only a la invocación completa. El review v19 evalúa el terminal
  más nuevo del proposal/signature actual y bindings de gates explícitos; puede
  reutilizar un `passed` de un run completado anterior ante replay exacto con la
  misma firma, mientras un terminal posterior `failed`/`abstained` lo invalida.
  El envelope digest liga todo el contexto durable; evidencia stale, corrupta o
  no registrada tampoco satisface. El receipt mantiene la readiness epistémica
  `human_review_required`, pero un verificador técnico separado y allow-listed
  puede publicar `no_change_required_within_verified_scope` tras recomprobar
  requisitos, contraevidencia, gates y controles negativos de la pregunta. La
  disposición es advisory, conserva riesgos residuales y no crea decisión
  humana, recomendación, patch ni autoridad de mutación. Preguntas completas sin
  política exacta quedan `unresolved`.
- El reader rechaza o abstiene ante publicaciones Code mezcladas, vuelve a
  resolver evidencias, verifica digests y conserva queries dimensionadas
  (`observation:*`, `question:*`, `decision:*`). La salida humana resume
  preguntas confirmadas/abstenidas, decisiones y autoridad de mutación.
- El corte v17 aceptado por `Neocortex code validate --baseline HEAD^` aprobó
  los once gates locales, incluida publicación `trusted-deep`, review,
  Coverage afectada, wheel candidato, replay y snapshot de fuente inmutable.
  El replay reconstruido enlazó los dos receipts `passed` por identidad
  semántica portable —no por IDs de captura— y dejó ambas preguntas en
  `human_review_required`, sin volver a proponer experimentos ya ejecutados.
  La vista pública conserva cero recomendaciones, cero work packages y
  `mutation_authority=false`.
- Esta mejora no equivale a cobertura experimental total: permanecen 38
  preguntas `experiment_required`, 11 huecos explícitos del registry y cero
  propuestas ejecutables pendientes. Supply/coverage/mutation pueden abstenerse
  por frescura, red deshabilitada o backend no disponible; esas ausencias no se
  reinterpretan como resultado verde.

## Capacidades que permanecen fail-closed

1. **Linux mutation:** sigue intencionalmente deshabilitada. Un backend POSIX
   sólo puede abrirse como proyecto separado con `openat2`/dirfd, identidad por
   descriptor, `renameat2`, journal durable y pruebas adversariales equivalentes
   a NTFS.
2. **CLIP textual:** las distribuciones positivas y negativas reales siguen
   solapadas; no existe un umbral publicado y la búsqueda se abstiene.
3. **MiniLM:** el resultado offline favorece al modelo compacto, pero no hay
   todavía 20–50 consultas humanas ES/EN/DE/ZH etiquetadas. Permanece shadow y
   separado del espacio Jina.
4. **Archive Semantic:** no se publican miles de chunks por inercia; sólo procede
   por selector si supera FTS en preguntas reales.
5. **OCR DE/ZH en video:** runtime y perfiles están listos, pero el piloto no
   aportó evidencia representativa suficiente para declararlo validado.
6. **Recuperación incierta:** una acción `recovery_required` continúa exigiendo
   revisión manual; nunca se reintenta automáticamente.

## Deuda residual explícita

- El baseline estático permite como máximo 67/94/142 hallazgos y agrupa por
  ruta/regla, no por fingerprint de cada mensaje. El corte Derivations observó
  exactamente 67/94/142;
  debe reducirse gradualmente y nunca usarse para intercambiar deuda nueva por
  vieja.
- `text_derivation_repository.py` y `semantic_lineage_repository.py` son nuevos
  hotspots grandes. Sus readers críticos están paginados/set-based y tienen
  regresiones de cotas, pero deben dividirse sólo por owners y contratos reales,
  no mediante un refactor cosmético.
- Permanecen hotspots grandes en evidencia externa, validaciones Knowledge y el
  parser legado. La campaña eliminó ciclos, no fingió haber reducido toda la
  complejidad ciclomática.
- La suite integral prueba el árbol fuente después de instalar el wheel; el
  artefacto instalado tiene un smoke aislado fuerte, no una segunda ejecución
  artificial de los 4,138 casos que dependen también de tools/docs del checkout.
- Windows queda como legado no mantenido ni validado; no es deuda activa.
- Las excepciones Semgrep dejan de ser válidas el 2026-09-30. Antes de esa fecha
  se actualiza o sustituye Semgrep; no se prolonga el vencimiento por rutina.
- La poda Text deja páginas libres reutilizables dentro de `text.sqlite3`; no se
  ejecutó `VACUUM` automático sobre el estado vivo. Cualquier compactación debe
  ser una ventana de mantenimiento explícita, con backup y verificación.
- La preselección durable ya evita retirar el límite de Personal, pero requiere
  invocaciones explícitas de `--refresh` hasta completar el cursor. No existe
  todavía UI de decisión ni productores ReviewTask para dominios distintos de
  Value.

## Próximos pasos, en orden

1. Ampliar el registry de experimentos una familia verificable por vez. Las
   verticales de recuperación Semantic, migración Code-owner, contratos de
   imports declarados y Retention durable ya tienen runner, receipt enlazable,
   controles negativos y verificador técnico acotado. La siguiente prioridad es
   Framework ReviewTask, manteniendo una política técnica exacta. Todo cambio
   semántico debe invalidar el receipt y
   `mutation_authority` permanece falso.
2. Mantener junto con cada nueva familia el binding de aceptación
   ruta/test→pregunta/sujeto. Una pregunta relevante sin runner o disposición
   técnica exacta debe abstener; sólo una relación disjunta demostrada puede
   quedar `not_required`.
3. Etiquetar con Víctor 20–50 consultas reales ES/EN/DE/ZH. La infraestructura
   golden ya existe, pero no debe inventar juicios humanos ni promover modelos
   por una métrica sintética.
4. Completar `Knowledge Asset Health` mediante una sola vertical causal sobre
   facts/receipts/snapshots reales —sin score agregado— y exponerla primero en
   API/CLI/doctor/status; no fingir salud de dominios aún no instrumentados.
5. Extender manifests y contrato causal a PDF, luego DOCX y finalmente Office,
   una ruta por vez; conservar legado no atribuible y no reescribir extractores.
6. Mantener `normalize`/`chunk` como deuda explícita hasta identificar fronteras
   ejecutables reales. Diseñar mutación Linux identity-bound sólo si la
   organización física en Kubuntu se vuelve prioridad.

## Criterio dinámico de cierre de 0.9.0

La release queda cerrada únicamente cuando se comprueba todo lo siguiente sobre
el mismo SHA comprometido y un worktree limpio:

1. `git rev-parse HEAD` = `git rev-parse origin/main`;
2. `current/neocortex-release.json:source_sha` = ese mismo SHA;
3. `python3.14 tools/release_linux.py verify` y `Neocortex --version` informan
   una release válida `0.9.0`;
4. el pre-push canónico aprueba arquitectura, estática, supply, Coverage y SHA;
5. antes de la primera corrida 0.9 se respalda cada SQLite con la API de backup
   online y se verifica integridad/FK de las copias;
6. dos corridas `Neocortex --all` desde el launcher final terminan en dry-run,
   migran Dedup a v10, conservan originales y demuestran replay incremental en
   providers replayables; Office heredado debe mostrar su reejecución
   `non_replayable` en vez de fingir cache hit;
7. las trece bases, corpus before/after, status/search/ask/review, MCP stdio y GUI
   instalados aprueban;
8. `origin/main` apunta al mismo SHA y GitHub Actions permanece deshabilitado;
   GitHub sólo expone `main`.

La evidencia mínima de cierre se conserva bajo
`$HOME/.codex/vault/evidence/neocortex-0.9-release-2026-08-10/`; el backup
pre-0.9 se conserva separadamente bajo `$HOME/.codex/vault/backups/neocortex/`.
Si cualquiera de los ocho puntos difiere, 0.9.0 sigue abierta y se corrige antes
de declarar éxito.
