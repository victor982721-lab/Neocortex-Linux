# NeoCortex — handoff operativo actual

> Actualizado: 2026-08-15. El nombre del archivo es histórico y se conserva
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
La ola del autoanalizador v22 descrita abajo está integrada en un commit local
aún no aceptado ni publicado; no se considera terminada hasta aprobar el gate
canónico, release Linux instalada, replay, push y coincidencia de SHA.

## Pausa operativa exacta — 2026-08-15 17:36 CST

### Estado vivo que debe reconciliar la siguiente sesión

- Checkout canónico: `/home/winterboss/Neocortex/Repository`, rama `main`.
- Commit local al pausar: `bf4b07ad149194780b1a965e4f865bff932b6da3`;
  `main` está un commit por delante de `origin/main` y el árbol estaba limpio
  antes de actualizar este handoff documental. La siguiente sesión debe volver
  a consultar Git; nunca asumir este SHA por memoria.
- `origin/main` y la release instalada `current` siguen en
  `d1adefc4cdafbd16a97e0f40bd349827fc74f98e`. No hubo push, instalación ni
  promoción de v22.
- No existen workflows de GitHub Actions y está prohibido crearlos, ejecutarlos
  o consultarlos. Windows no forma parte del alcance.
- La configuración viva de Codex declara
  `features.multi_agent_v2.max_concurrent_threads_per_session = 8`: un
  coordinador más siete workers. La siguiente sesión debe verificar de nuevo
  ese valor antes de repartir ownership.
- El commit local contiene review v22, Knowledge Asset Health Text/PDF,
  runners/receipts/verificadores, las lecturas focales `code question` y
  `code storage`, superficie pública, documentación y las correcciones reales
  encontradas por el gate. La matriz focal previa cerró 369 pruebas, pero eso
  no sustituye la aceptación canónica.
- Las correcciones posteriores conservaron el baseline: tipado focal, frontera
  `neocortex.read_api`→`read_api_port` y snapshot CLI de
  `--knowledge-health`. Estática y arquitectura ya fueron observadas verdes.

### Último gate y único bloqueo comprobado

La última ejecución de `Neocortex code validate --baseline HEAD^` aprobó:

1. `static_no_regression`;
2. arquitectura declarada;
3. publicación `trusted-deep`;
4. review fresco;
5. Coverage afectada.

Se abstuvo antes de ejecutar experimentos:

```text
allowlisted_experiments = abstained
reason = affected_question_requires_unresolved_evidence
receipts = 0
```

La causa exacta ya fue reconstruida read-only, no debe diagnosticarse de nuevo:

- run Code vigente al corte: 149, review `neocortex.code-review/v22` ready;
- 12 scopes de aceptación quedaron afectados y existían 11 proposals
  potenciales;
- el único blocker era `security_supply_boundary`;
- `pip-audit` no pudo producir evidencia actual dentro del worker porque el gate
  prueba y exige `PrivateNetwork=yes`;
- la firma de entorno se cambió deliberadamente de v1 a v2 para dejar de ligar
  providers a la ruta física temporal/final de `sys.executable`; por ello el
  snapshot v1 no puede ser replay exacto bajo v2;
- el audit histórico run 139/tool run 1294 sigue fresco hasta
  `1786902337`, corresponde a las mismas 128 distribuciones instaladas y
  reporta cero vulnerabilidades. Esto permite al gate aceptar freshness, pero
  no crea el receipt tipado v2 que necesita la evaluación security;
- `_fresh_review_gate` conoce ese fallback; `_experiment_gate` ve correctamente
  la evaluación security cruda como `abstained` y cancela todos los proposals.

No se debe resolver rebajando seguridad, ignorando la pregunta, ampliando un
baseline o fabricando un receipt. El desbloqueo correcto es un único seed
networked bajo la firma v2; después el gate privado debe usar `cache_replay`
sin egress.

### Autorización externa pendiente

Antes de ejecutar el seed, la siguiente sesión debe pedir a Víctor una
autorización **explícita y concreta** después de informar que:

- `pip-audit` usará el servicio de vulnerabilidades `pypi`;
- transmitirá los nombres y versiones de las 128 distribuciones Python
  instaladas;
- no transmitirá fuentes, corpus, documentos, estado SQLite ni secretos;
- el productor completo se ejecutará localmente, acotado por cgroup, y sólo
  ese provider necesita red.

Una solicitud escalada ya fue rechazada antes de crear el unit porque la
autorización general de trabajo local no cubre ese egress concreto. No hubo
tráfico de red ni publicación parcial. No intentar rodear esa decisión.

### Comprobación esperada tras el seed

La publicación one-shot debe ejecutarse desde el source actual con el Python de
la release, `--analysis-profile trusted-static`, root y state directory
explícitos, dentro de un transient unit con memoria/CPU/tareas acotadas, **sin**
`PrivateNetwork=yes` y sin
`NEOCORTEX_PIP_AUDIT_NETWORK_POLICY=disabled-by-code-validation`.

Tras terminar, verificar por la superficie pública/source, no por inferencia:

1. run nuevo `completed`;
2. provider `pip-audit-known-vulnerabilities`: `status=ready`,
   `execution=full`, cero vulnerabilidades o fallo explícito;
3. evaluación security deja `abstained` y pasa a `experiment_required` con
   template `security.bounded_boundary_scenarios`;
4. el siguiente gate privado publica ese mismo provider como `cache_replay`,
   `process_invocations=0`, sin red;
5. los experimentos producen receipts y el replay final conserva las
   disposiciones técnicas reproducibles.

Si cualquiera difiere, detener el gate y corregir esa causa; no ejecutar una
segunda corrida integral como diagnóstico ciego.

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
- El snapshot Knowledge de Framework v22 valida y expone heads ReviewTask y
  watermarks de batches, eventos y publicaciones fuente; conserva lectura
  legacy validada para v19/v20/v21. Retention protege tareas y eventos humanos como
  holds y, por separado, el head vigente con toda la cadena alcanzable de
  batches, memberships y progreso exactos. La auditoría está acotada y falla
  cerrado ante receipts o vínculos históricos corruptos.
- Video OCR ya participa en la búsqueda Knowledge canónica con locator temporal
  y estado owner fail-closed; no se añadieron embeddings Video. `Knowledge
  Asset Health` incorpora trazas causales separadas para Text y PDF; no convierte
  el estado Value en un score ni cubre contenido/OCR, fidelidad visual o
  semántica, entities/claims, contradicciones, links o promociones shadow. Esos
  ReviewTask y una GUI para decidir tareas siguen pendientes.
- Este handoff documenta el árbol sin sustituir tests focales, gates, commit ni
  release Linux instalada del SHA final.

## Contrato operativo — Autoanalizador v22 (en integración)

- La siguiente frontera operativa es `Neocortex code validate`: una sola entrada
  Linux para validar implementaciones. Captura el diff, selecciona pruebas con
  evidencia publicada, ejecuta estática/arquitectura, publica `trusted-deep`,
  consume review v22, ejecuta experimentos allow-listed, instala el wheel
  candidato fuera del checkout y exige replay. Las herramientas individuales
  quedan como diagnóstico interno; no constituyen una aceptación paralela. La
  política `local-linux-diff-aware-validation-v8` liga cambios de CLI y de
  Knowledge Asset Health Text/PDF con sus preguntas, subjects, templates y
  disposiciones técnicas v6 exactas; si falta cualquiera, se abstiene.
  Una selección afectada conserva como máximo 5000 tests y shards de 50; al
  cruzar una frontera full, la policy fija 10000/250 para cubrir el inventario
  Linux sin truncarlo ni multiplicar el costo de arranque del worker.
- La primera ejecución full del corte proporcional confirmó 5330 tests y 22
  shards, pero falló focalmente cuando un test parcheó el singleton público
  `sqlite3.connect` y Coverage heredó ese doble durante teardown. También reveló
  que un shard aprobado con tests `skipped` no producía checkpoint. El worker
  conserva ahora su conector SQLite real sin alterar el módulo visto por los
  tests, y sólo reutiliza shards con suite aprobada, cero fallos y resultados
  terminales `passed|skipped`. Ruff, Mypy focal, la reproducción real y la
  matriz consolidada de 399 tests más 2 subtests quedaron verdes; no existe aún
  receipt canónico aprobado para este corte.
- La aceptación más reciente sobre `b1979f1ab8f04a798846591e7e6654dfa2034581`
  aprobó static, arquitectura, `trusted-deep` y review v22, pero Coverage cerró
  con 126 fallos: 124 nacían de colocar `TMPDIR` dentro del owner durable de
  self-analysis y 2 de expectativas Semantic anteriores al replay medible. No
  publicó receipt. El lote material posterior corrige ambas causas: runtime
  pytest atómico fuera del owner durable, checkpoints v2 independientes sólo
  de la ruta efímera, y contratos `execution_mode`/contadores Semantic.
- El mismo lote inicia la reorganización capability-first sin crear nuevos
  roots: mueve 27 implementaciones Archive/DOCX/Image/shared a
  `capabilities.formats` y `platform.shared`, mantiene aliases históricos
  exactos y conserva schemas/stores. Un registry versionado declara 25 pares
  canonical/legacy, owner, estado, superficie y tests; la proyección
  arquitectónica distingue SCC de módulo realizables de SCC agregados sólo
  diagnósticos y aplica el DAG `compat.formats → capabilities.formats`.
  Antes de la aceptación aprobaron Ruff en 111 archivos y cuatro matrices
  focales disjuntas: 811 tests, 21 skips y 102 subtests. El recibo canónico,
  release instalada, E2E/replay y push siguen pendientes.
- La aceptación de `b1e777e6dd3041a0e4f9dc4475ac5d464f451556` se detuvo
  correctamente en static no-regression, antes de providers, autoanálisis,
  Coverage o experimentos, por dos diagnósticos Pyright. El helper Image quedó
  expresado como API interna usada por sus 15 módulos y la proyección SCC evita
  indexar una tupla que el analizador no podía demostrar no vacía. Los dos
  diagnósticos focales desaparecieron, 94 pruebas de compatibilidad/proyección
  aprobaron y la etapa static no-regression completa volvió a quedar verde. No
  se relanzó el gate sobre ese SHA; corresponde congelar el candidato corregido
  antes de una nueva aceptación.
- La corrida full medida necesita reservar el costo de providers no-Coverage y
  finalización además del presupuesto 2x de shards. El timeout interno full es
  ahora 60 minutos (1800 s de Coverage más 1800 s de overhead), todavía dentro
  de la cota global de 75; la ruta afectada conserva 15 minutos de overhead.
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
- La promoción de cualquier corte requiere pruebas focales, commit directo a
  `main`, una sola validación verde con `--baseline HEAD^`, release Linux del
  SHA exacto, launcher público verificado, push único y coincidencia
  `HEAD=main=origin/main=current`. Ninguno se infiere de este handoff.

- `Neocortex --state-directory ESTADO --code-review` ya no es una vista que
  convierte nombres, rutas o tamaño en recomendaciones. El envelope
  `neocortex.code-review/v22` publica un registro general de preguntas y
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
- La pregunta CLI dispone de un scenario v3 de veintiún nodeids/cinco gates:
  ayuda y traducción, precedencia de dispatch, rechazos sin estado y lecturas
  focales de pregunta/storage. No ejecuta cada handler, GUI, MCP, worker o
  efecto externo. Su template v2 y disposición técnica sólo aceptan esa matriz.
- `Knowledge Asset Health` aporta verticales causales Text/PDF bajo la misma
  superficie. El resource ID `resource:file` liga Inventory, owner fuente,
  Catalog y la selección Knowledge; el dispatch usa identidad física, probes
  packed/legacy, snapshot y hint Catalog, nunca path/extensión. Dos observaciones
  completas y un retry acotado impiden publicar como estable una vista
  cambiante. Text usa doce nodeids/cuatro gates. PDF schema 13 usa otros doce y
  cuatro gates 5/3/3/1; conserva estados
  `done|partial|protected|error|processing`, páginas/staging/errores/FTS,
  Catalog/Search y recovery tipado. El binding counter exige nueve relaciones y
  el resultado completo doce. `healthy` no significa contenido/OCR, verdad
  semántica, calidad visual, salud de otros owners ni resistencia a power loss;
  la lectura es content-blind, advisory y jamás autoriza mutación.
- `Neocortex code question QUESTION_ID --limit N --json` evita el review global
  sólo para la pregunta CLI registrada. Una pregunta distinta devuelve
  `unsupported` con fallback `automatic=false`. `Neocortex code storage
  --run-limit N --row-scan-limit N --retain-runs N --json` observa forma,
  crecimiento y ventana temporal mediante SQLite immutable; `retain-runs` es
  `preview_only` y no ejecuta delete, prune, vacuum, checkpoint o sidecars.
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
- v22 conserva ownership lógico explícito, interacciones SQL/SQLite ligadas a
  los 13 stores declarados, fronteras transaccionales/workflow, reachability de
  las nueve rutas built-in, cuatro invariantes, calibración anti-Goodhart y un
  planificador de experimentos sin comandos libres. SQL dinámico, parser no
  disponible, provider stale o evidencia truncada producen abstención. Los
  placeholders numerados válidos `?NNN` se adaptan token a token para SQLGlot
  sin alterar strings ni el digest observado; dejaron de contarse como error
  los dos sitios productivos Text/Archive que usaban esa sintaxis.
- `--code-experiment-run PROPOSAL_ID` reconstruye el plan vigente y sólo admite
  once templates ejecutables en los registries runtime/template v10: contratos
  de imports declarados (tres nodeids y
  cuatro gates), acceptance pública Text (un nodeid), trace/fault
  boundaries del workflow Text (cuatro nodeids), recuperación Semantic ante
  muerte del proceso durante staging (un nodeid con tres gates), una matriz
  Code-owner de migración poblada/rollback/schema futuro (cinco nodeids y cuatro
  gates), Retention durable en dry-run (catorce nodeids y cuatro gates),
  supply-chain local (diez nodeids y siete gates), Framework ReviewTask (ocho
  nodeids y cinco gates), CLI pública (veintiún nodeids y cinco gates),
  Knowledge Asset Health Text (doce nodeids y cuatro gates) y PDF (doce/cuatro,
  5/3/3/1). ReviewTask vuelve
  a resolver owner/store/schema y
  adapter/port, ejecuta publicación/CAS/replay/rollback y el journey CLI sobre
  SQLite/XDG temporal; el actor es sintético/no autenticado y los fallos
  inyectados no prueban power loss. Retention
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
  no vuelve read-only a la invocación completa. El review v22 evalúa el terminal
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
- Las cifras de 38 preguntas `experiment_required`, 11 huecos y cero proposals
  pendientes pertenecen al corte v17 y no describen por sí solas v22. El conteo
  vigente se obtiene únicamente de la publicación final v22. Supply,
  coverage/mutation pueden abstenerse por frescura, red deshabilitada o backend
  no disponible; esas ausencias no se reinterpretan como resultado verde.

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

### A. Reanudación y cierre de v22 — antes de abrir otra vertical

1. Leer CTBI/AGENTS, `PENDIENTES.md` y este handoff; verificar en vivo rama,
   `HEAD`, `origin/main`, release `current`, worktree, procesos y ausencia de
   workflows. Usar shells no-login.
2. Mantener el lote material como un único candidato limpio: `--all` consumidor
   del receipt, caché Code por lotes, replay Semantic exacto, runtime Coverage
   efímero, checkpoints v2 y primera cohorte capability-first. Registrar su SHA
   exacto en `PENDIENTES.md`; no abrir v23 ni otra cohorte antes del cierre.
3. Ejecutar una sola vez
   `Neocortex code validate --baseline HEAD^`. El resultado aceptable es
   `passed`; `failed` o `abstained` abren diagnóstico focal desde la evidencia
   del gate, nunca una batería manual paralela. Comprobar especialmente:
   - receipt exacto v1 ligado al commit limpio y policy v8;
   - suite Linux y Coverage sin regresión dentro del mismo receipt;
   - pip-audit `cache_replay`, cero procesos/red dentro del worker;
   - experiments con receipts terminales;
   - wheel candidato instalado fuera del checkout;
   - replay y disposiciones técnicas diff-bound;
   - fuente sin cambios y cgroup/headroom conservados.
4. Con gate verde, instalar la release Linux desde ese SHA exacto mediante
   `python3.14 tools/release_linux.py install --corpus-root
   "$HOME/Documentos/NeoCortex/Corpus" --prepare-models --desktop` y ejecutar
   `python3.14 tools/release_linux.py verify`. El manifest, receipt, launcher y
   `current/neocortex-release.json` deben declarar el mismo SHA.
5. Probar desde `~/.local/bin/Neocortex`, sin `PYTHONPATH` ni dependencia del
   checkout: status, review v22, `code question`, `code storage` y `knowledge
   health` con un `resource:file` real obtenido de estado publicado. Repetir
   una lectura sólo cuando demuestre estabilidad/replay y conservar exits,
   schemas, digests, cotas y `mutation_authority=false`.
6. Sin writers vivos, crear un backup online nuevo de las 13 SQLite con la API
   instalada y verificar quick-check/FK del manifest antes del E2E.
7. Ejecutar una corrida E2E instalada con el receipt estricto. Como este diff
   cruza caché y pipeline, ejecutar una segunda; debe demostrar
   `code_processed=0`, todos los candidatos Code reutilizados y Semantic con
   cero fuentes enumeradas, elementos/fragmentos preparados y jobs nuevos. No
   usar `--apply`, no borrar sidecars y no abrir corridas duplicadas.
8. Hacer un solo `git push origin main`, verificar
   `HEAD=origin/main=current.source_sha`, release verify y worktree limpio.
   GitHub Actions permanece ausente y no se consulta.

### B. Campaña multiagente v23 — siete workers más coordinador

Comenzar sólo después del cierre observable de v22. La siguiente sesión debe
verificar la capacidad viva de concurrencia; si confirma siete workers, usarlos
con ownership disjunto. El coordinador no compite por archivos: congela APIs,
integra, resuelve conflictos, ejecuta el gate y actualiza el estado durable.

1. **Worker DOCX core (writer):** crear
   `knowledge_asset_health_docx.py`; integrar exclusivamente
   `knowledge_asset_health_repository.py` y `knowledge_asset_health.py`; añadir
   `tests/test_knowledge_asset_health_docx.py` y regresiones Text/PDF mínimas.
2. **Worker QuestionSpec (writer):** crear
   `code_knowledge_docx_asset_health_analysis.py` y su test. No tocar los
   registries compartidos hasta congelar IDs, facts y requirements.
3. **Worker runtime/receipt (writer exclusivo):** después de congelar la API,
   poseer `code_invariant_contracts.py`, `code_experiment_planner.py`,
   `code_experiment_store.py` y sus tests. Nadie más edita esos registries.
4. **Worker verificador/gate (writer exclusivo):** poseer
   `code_technical_verification.py`, `code_change_validation.py` y sus tests;
   mantener diff→pregunta→template exacto y abstención ante runner/evidencia
   ausente.
5. **Worker review/query (writer exclusivo):** bump v23 y compatibilidad
   explícita en `code_review_models.py`, `code_review_serialization.py`,
   `code_review.py`, `code_analysis_query.py` y tests; v16–v22 no deben adquirir
   vocabulario v23 retroactivamente.
6. **Worker superficie/adversarial (writer acotado):** demostrar que la misma
   API/CLI Health despacha DOCX por identidad/snapshot, nunca extensión o path;
   editar sólo ports/superficie y sus tests si aparece una brecha real. Debe
   probar recursos, WAL, ambigüedad, future/corrupt y cero lectura de contenido.
7. **Worker documentación y auditoría independiente:** actualizar README/docs
   sólo tras congelar contratos y, en paralelo, auditar read-only la siguiente
   candidata Office v24. No implementar v24 ni tocar registries v23.

El coordinador debe lanzar primero los workers 1, 2, 6 y 7; los workers 3–5
pueden auditar en paralelo, pero sólo editan después de recibir el contrato
congelado. Un archivo compartido tiene un único writer. Cada entrega incluye
diff, pruebas focales con timeout, Ruff/format focal, límites y riesgos; ningún
worker hace commit, push, release, providers ni gate integral.

### C. Contrato DOCX v23 ya auditado; no volver a inventarlo

- Question: `knowledge.docx_asset_health_preserves_terminal_partial_protected_failure_and_recovery_causality`
  v1.
- Subject: `capability:knowledge-asset-health:docx`.
- Action/template/scenario:
  `run_knowledge_docx_asset_health_causal_experiment` /
  `knowledge.docx_asset_health_causal_acceptance` v1.
- Bumps: review v23, runtime/template/planning v11, technical v7/validation v8; no
  schema SQLite nuevo.
- DOCX owner/store/schema: `docx` / `sqlite:docx.sqlite3` / 6; estados reales
  `complete|partial|error`. `protected` Health sólo se deriva de la tupla
  cifrada exacta persistida, sin inventar un status DOCX.
- `document_fts.file_key/path` no está indexado: la lectura se limita mediante
  `sqlite3.set_progress_handler` con presupuesto fijo y abstiene
  `docx_owner_read_budget_exhausted`; `LIMIT` no se presenta como cota de
  trabajo. Nunca leer body/blobs, mensajes de error, texto de partes,
  diagnósticos crudos ni metadatos privados.
- Dispatch por identidad física/snapshot y candidatos packed/legacy; dos owners
  actuales producen `source_owner_identity_ambiguous`. Nunca usar extensión,
  MIME o path para elegir modalidad.
- Complete/partial coherentes pueden llegar a Catalog/Search; error/encrypted
  no. El fact conserva partes, diagnósticos, FTS, integrity, retry,
  disposition/recovery y digests tipados, no contenido.
- Scenario: 12 nodeids disjuntos; gates 4/3/4/1. Counterevidence usa gates
  mismatch + recovery + WAL/ambiguity = 8 relaciones; el resultado completo
  usa 12. Nodeids contractuales exactos:
  1. `tests/test_code_knowledge_docx_asset_health_analysis.py::test_docx_asset_health_question_requires_terminal_partial_protected_failure_and_recovery_experiment`;
  2. `tests/test_knowledge_asset_health_docx.py::test_docx_aligned_projection_is_healthy_read_only_and_content_blind`;
  3. `tests/test_knowledge_asset_health_docx.py::test_docx_complete_partial_encrypted_and_other_error_states_are_typed_without_inventing_catalog`;
  4. `tests/test_knowledge_asset_health_docx.py::test_docx_projection_recovery_and_terminal_inconsistencies_fail_closed`;
  5. `tests/test_knowledge_asset_health_docx.py::test_docx_dispatch_ambiguity_and_owner_fences_abstain_without_mutation`;
  6. `tests/test_docx_route.py::DocxRouteTests::test_extracts_searches_classifies_pairs_and_reuses_cache`;
  7. `tests/test_docx_route.py::DocxRouteTests::test_records_corrupt_compressed_member_without_aborting_route`;
  8. `tests/test_docx_route.py::DocxRouteTests::test_indexes_body_when_an_optional_header_is_corrupt`;
  9. `tests/test_docx_route.py::DocxRouteTests::test_recovers_well_formed_required_xml_with_bad_central_crc`;
  10. `tests/test_docx_route.py::DocxRouteTests::test_marks_required_deflate_corruption_as_deletion_candidate`;
  11. `tests/test_docx_route.py::DocxRouteTests::test_retries_transient_errors_without_the_manual_retry_flag`;
  12. `tests/test_docx_route.py::DocxRouteTests::test_commits_bounded_batches_before_an_interruption`.
- Gates exactos: `diagnostic_parts_fts_and_catalog_mismatch_fail_closed`
  = nodeids 4/7/8/10; `recovery_retry_and_interruption_contracts_remain_typed_and_bounded`
  = 9/11/12; `typed_docx_states_preserve_complete_partial_protected_and_error_semantics`
  = 1/2/3/6; `wal_snapshot_and_owner_ambiguity_remain_read_only` = 5.
- Fuera de alcance: power loss, verdad semántica, fidelidad visual y recuperar
  una historia que el estado actual no conserva.

### D. Horizontales posteriores al cierre v23

1. Corregir el diagnóstico del seed supply sin debilitar el gate: transportar
   evidencia histórica tipada entre `_fresh_review_gate` y la epistemología, o
   emitir explícitamente `fresh_pip_audit_seed_required`. Nunca convertir una
   evaluación abstained en passed ni reutilizar un comparable como exact replay.
2. Añadir otro lector focal sólo si una medición viva supera claramente el
   review global y conserva paridad exacta de IDs/digests. No crear un segundo
   motor de review.
3. Mantener cada binding ruta/test→pregunta/sujeto. Relevante sin runner o
   disposición exacta abstiene; sólo irrelevancia demostrada es
   `not_required`.
4. Implementar Office v24 sólo después de que DOCX v23 pase su gate, release y
   replay. La auditoría puede adelantarse read-only, la edición no.
5. La calibración humana 20–50 consultas ES/EN/DE/ZH sigue separada: no inventar
   outcomes ni pedir a Víctor que juzgue código. Los verificadores técnicos
   deterministas cargan esa parte; Víctor sólo aporta valor/uso cuando pueda.

## Criterio dinámico de cierre de 0.9.0

La release queda cerrada únicamente cuando se comprueba todo lo siguiente sobre
el mismo SHA comprometido y un worktree limpio:

1. `git rev-parse HEAD` = `git rev-parse origin/main`;
2. `current/neocortex-release.json:source_sha` = ese mismo SHA;
3. `python3.14 tools/release_linux.py verify` y `Neocortex --version` informan
   una release válida `0.9.0`;
4. un único receipt `Neocortex code validate --baseline HEAD^` aprueba
   arquitectura, estática, supply, Coverage sin regresión, wheel/replay y SHA
   limpio; no se ejecuta además el `pre-push` histórico cuando esas mismas
   barreras constan en el receipt;
5. antes de la primera corrida 0.9 se respalda cada SQLite con la API de backup
   online y se verifica integridad/FK de las copias;
6. una corrida `Neocortex --all` desde el launcher final termina en dry-run,
   conserva originales y completa el producto; como este cambio cruza caché y
   pipeline, una segunda corrida demuestra replay real mediante contadores de
   trabajo nulo en Code y Semantic. Office heredado debe mostrar su reejecución
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
