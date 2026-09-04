# Arquitectura de NeoCortex

> **Estado del documento.** Contrato derivado del árbol inspeccionado el
> 3 de septiembre de 2026. Describe el comportamiento observado y separa los
> cambios previstos de los ya implementados. No certifica por sí solo la suite
> completa ni la instalación empaquetada. El árbol auditado declara la versión
> `0.9.0`; la versión instalada debe comprobarse con
> `Neocortex --version`.

## Finalidad y principios

NeoCortex es un framework local para Linux/Kubuntu que permite descubrir, identificar, extraer,
indexar, relacionar, clasificar, revisar y buscar contenido personal de forma
incremental. Sus rutas actuales cubren PDF, DOCX, otros documentos Office,
ZIP anidados, texto físico/correo/Office heredado, audio, video, imágenes y
código.

“Incremental” no significa que todo provider sea replayable: una implementación
que no pueda cerrar su entorno declara `incremental=false`/`non_replayable` y se
reejecuta de forma visible en vez de aparentar que procesa sólo cambios.

La arquitectura persigue estos invariantes:

- los archivos originales permanecen intactos salvo autorización explícita;
- el estado derivado conserva identidad, evidencia, incertidumbre y
  procedencia;
- una interrupción no debe convertir trabajo parcial en verdad publicada;
- los lotes, colas, procesos y consumo de recursos deben permanecer acotados;
- las superficies CLI, GUI y Python deben converger en los mismos contratos;
- la evidencia probabilística o semántica nunca autoriza por sí sola una
  eliminación, movimiento o rename.

Los riesgos todavía abiertos se enumeran al final. La publicación generacional
de inventario, catálogo y semántica existe en la fuente `0.7.0`, pero sólo sus
lectores oficiales aplican el contrato; SQL externo sobre tablas legacy puede
eludirlo.

## Fuentes de verdad

| Tema | Fuente primaria |
|---|---|
| Entry point instalado | `[project.scripts]` de `pyproject.toml` y `neocortex.interface.entrypoint` |
| Parser y validación CLI | `neocortex/api/cli/cli_parser.py` y `neocortex/api/cli/cli_validation.py`, con superficies `cli_{audio,code,semantic,knowledge}_surface.py` |
| Configuración efectiva de una corrida | `neocortex/runtime/models.py`; `application_config.py`, proyecciones en `neocortex/runtime/config/application_config_projections.py` y construcción CLI en `neocortex/api/cli/cli_config.py` |
| Plataforma y rutas canónicas por usuario | `neocortex/platform/policy.py` y `neocortex/runtime/config/app_paths.py` |
| Protección de rutas propias | `neocortex/safety/internal_paths.py`, `neocortex/integrations/inventory/inventory_boundary.py` y `neocortex/runtime/control/incremental_gate.py` |
| Orden y adaptadores de rutas | `neocortex/runtime/orchestration/route_selection.py` y `route_registry.py` |
| Coordinación de corridas | `neocortex/runtime/orchestration/orchestrator.py` |
| Knowledge Plane read-only | `neocortex/knowledge/knowledge_contracts.py`, `knowledge_snapshot.py`, `knowledge_planner.py`, `knowledge_search.py`, `knowledge_context.py` y `knowledge_service.py` |
| Derivaciones reproducibles | `neocortex/semantic/derivation_contracts.py`, repositorios owner-local de Text/Semantic, `derivation_projection.py` y `derivation_lineage_service.py` |
| Manifests y selección de capacidades | contratos en `neocortex/capabilities/broker.py`, declaraciones/probes en `neocortex/capabilities/runtime.py` y consumidor Text en `neocortex/capabilities/formats/text/text_route.py` |
| Planner semántico read-only | `neocortex/semantic/semantic_planner.py` y contratos en `semantic_service_contracts.py` |
| SDK, consulta y capacidades públicas | `neocortex/sdk`, `neocortex/api/read_api.py`, `neocortex/api/cli/human.py`, `neocortex/api/agent_server.py`, `neocortex/capabilities/runtime.py` y markers `py.typed` |
| Apertura SQLite compartida | `neocortex/persistence/sqlite_immutable.py` y `sqlite_paths.py` |
| Esquemas persistentes | módulos `*_schema.py` y propietarios `*_state.py`/repositorios |
| Estado operacional | `${XDG_STATE_HOME:-~/.local/state}/Neocortex/state` |
| Comportamiento comprobable | código ejecutado y pruebas; la documentación no lo sustituye |

La topología Linux derivada de la política central separa los árboles propios:

```text
Fuente:       ~/Neocortex/Repository
Runtime:      ~/.local/share/Neocortex/releases/<runtime-id>
Launcher:     ~/.local/share/Neocortex/bin/Neocortex
Estado:       ${XDG_STATE_HOME:-~/.local/state}/Neocortex/state
```

Los runtimes son versionados y el launcher estable se valida contra el artefacto
exacto antes de promoverlo. Estado, configuración y datos respetan XDG. Las releases inmutables
viven en `~/.local/share/Neocortex/releases`, `current` selecciona la activa,
los modelos compartidos viven en `~/.local/share/Neocortex/models`, el launcher
estable es `~/.local/share/Neocortex/bin/Neocortex` y el alias público es
`~/.local/bin/Neocortex`.

La guía de bases, migraciones, backup y retención es
[PERSISTENCE.md](PERSISTENCE.md). La operación diaria se explica en
[OPERATIONS.md](OPERATIONS.md), la recuperación en
[RECOVERY.md](RECOVERY.md) y los límites de seguridad en
[SECURITY.md](SECURITY.md). La clasificación vigente de Code como contenido
se conserva en [CODE_SUBSYSTEM_CLASSIFICATION.md](CODE_SUBSYSTEM_CLASSIFICATION.md);
los contratos y límites del plano de conocimiento se documentan en
[KNOWLEDGE.md](KNOWLEDGE.md).

## Vista de alto nivel

```text
                   Neocortex / python -m neocortex
                                  │
                   neocortex.interface.entrypoint:entrypoint
                     ┌────────────┼────────────┐
                     │            │            │
                  CLI normal     --ui     --gui-worker
                     │            │            │
            cli_app / cli_direct  │       worker supervisado
                     │            └──── QProcess ─────┘
                     │
          FrameworkOrchestrator / operaciones directas
                     │
          framework.lock + estado común de corrida
                     │
       ┌─────────────┴─────────────────────────────┐
       │                                           │
 enumeración portable + USN opcional        rutas de contenido
       │                       ┌────┬────┬────┬────┬────┬────┬─────┬─────┬────┐
 inventario y deduplicación    PDF DOCX Office ZIP Text Audio Video Image Code
       │                       └────┴────┴────┴────┴────┴────┴─────┴─────┴────┘
 checkpoint durable                          │
       │                     catálogo documental / revisión / semántica
       └───────────────────────────────┬─────┘
                                      │
                              SQLite por propietario
```

La separación física entre bases reduce contención y permite que cada ruta sea
propietaria de su contrato. No constituye una transacción distribuida: la
coherencia entre bases depende del orden del orquestador, identificadores de
run, checkpoints y reglas de publicación de cada subsistema.

## Knowledge Plane de sólo lectura

La Fase 1 agrega una frontera de recuperación común sobre las bases que ya
poseen inventario, extractores, FTS, catálogo, semantic y código. No agrega una
base `knowledge.sqlite3`, DDL ni migraciones, y sus operaciones no indexan ni
modifican el corpus. Los contratos inmutables `ResourceRef`, `RevisionRef`,
`EvidenceRef`, `KnowledgeHit`, `KnowledgeSnapshot` y `ContextBundle` conservan
identidad, revisión, localizador, procedencia, score de origen y completitud.

`collect_knowledge_snapshot()` abre únicamente owners existentes en modo de
lectura, observa sus publicaciones o watermarks dos veces y reintenta el
conjunto una vez si detecta cambios. Ese snapshot es una vista lógica, no una
transacción distribuida. `KnowledgeSearchService` vuelve a capturar el estado
antes y después de cada consulta; ante un primer cambio repite la recuperación
completa una vez y, ante otro, devuelve evidencia parcial con el cambio
explícito en vez de fingir atomicidad.

El planner determinista selecciona rankings independientes de identidad/ruta
exacta, FTS por owner, semantic publicado, código estructural y metadatos de
catálogo. La fusión RRF mantiene scores y modelos en sus espacios originales y
opera por evidencia concreta, no sólo por archivo. El modo `evidence` permite
varias páginas, segmentos o chunks de un recurso; `discovery` conserva la
semántica compatible de un mejor hit semantic por recurso. El compilador de
contexto aplica un presupuesto duro, citas estables, contradicciones
estructuradas y estados de ausencia o abstención.

La API Python, los comandos `--knowledge-*`, los aliases humanos
`status/search/ask` y MCP/stdio consumen esa misma frontera. `read_api` sólo
resuelve los scopes fijos `personal` y `framework`; `all` conserva rankings
independientes. `agent_server` registra únicamente tools read-only y no abre
red. Un grafo transversal entre owners permanece para una fase posterior. La
especificación completa está en
[KNOWLEDGE.md](KNOWLEDGE.md).

La telemetría Knowledge schema 1 ya se especifica allí: usa nanosegundos y
conserva intentos, snapshots, owners, rankings, fusión, broker y contexto sin
alterar los contratos sin telemetría. No constituye una publicación ni un owner
persistente adicional.

## Derivaciones reproducibles v1

**IMPLEMENTED — Text extraction/publication.** El contrato público schema 1
extiende, sin
duplicarlos, `ResourceRef` y `RevisionRef` con `StageDescriptor`,
`InputBinding`, `OutputBinding`, `MaterializationRef`, `DerivationRef`,
`CapabilityFailure`, `ReproducibilityClass`, `WorkOutcome`,
`WorkExecutionMode` y `WorkReceipt`. Un recibo terminal conserva stage y
versión, firma de procesamiento, configuración efectiva, runtime, inputs y
outputs con fingerprints, proveedor/modelo cuando aplica, tiempos, intento,
resultado, modo de ejecución, `run_id`, correlación, causación y una de las
clases `exact`, `environment_bound`, `seeded`, `equivalent`, `best_effort` o
`non_replayable`. El contrato rechaza una declaración `exact` sin digest de
implementación y runtime identificable; fallo/cancelación usan `attempted` y
un abandono usa `unknown`, por lo que no se presenta como ejecutado lo que no
puede demostrarse.

Text schema 2 hace durable la cadena `RevisionRef` → `text.extract/v2` →
`text_representation` + `text_fts`. Después de capturar y fingerprintar el input
exacto, el intento `running` se confirma antes del parser y de materializar
outputs. Sus tiempos cubren esa etapa transformativa, no la adquisición previa
del archivo. La publicación de documento/FTS, outputs, heads, recibo
terminal y evento de outbox ocurre en una sola transacción del owner Text; un
rollback no deja outputs declarados ni evento confirmado. Una reutilización compatible
verifica también los outputs físicos y publica un recibo `cache_hit` ligado por
`causation_id`; contenido, MIME efectivo, configuración, firma o versión de
stage incompatibles fuerzan ejecución. Esa reutilización aplica al provider
builtin `environment_bound`; el worker Office heredado `non_replayable` nunca
consulta ni publica cache hits de éxito o fallo. Cancelación, fallo y un intento
`running` encontrado después de una caída terminan con un recibo terminal
owner-local auditable, sin publicar materializaciones parciales.

La outbox es append-only y se confirma junto con el recibo owner-local. La
proyección transversal es un objeto descartable en memoria: consume de forma
idempotente outboxes Text y Semantic, distingue outputs producidos de
reutilizados, explica ancestros/causación y propaga staleness por stage y firma.
No es autoridad, no posee otra base SQLite y puede borrarse y reconstruirse;
por tanto no introduce una transacción distribuida ni registra un hecho antes
del commit del propietario.

`Neocortex inspect lineage IDENTIFICADOR` y `neocortex.api.read_api.lineage_payload`
exponen la vista con scopes fijos. La lectura acepta file key/path Text y
revisiones, materializaciones o recibos actuales/históricos, además de chunks
Semantic. Abre sólo bases existentes, valida el contrato de cada schema
soportado y limita recibos,
dependencias, chunks y eventos. No crea, migra, repara ni hace checkpoint del
estado.

**PARTIAL — extensión Semantic.** Semantic schema 7 conserva la publicación
generacional introducida en v6 y agrega receipts/outbox owner-local para
materialización/publicación de chunks, ejecución/reutilización de embeddings y
publicación de generaciones. El lector distingue chunks staged de los
publicados y puede enlazar un chunk textual con la `RevisionRef` owner-native
de Text cuando la fuente la aporta. Este corte no afirma todavía una cadena
canónica completa para PDF/DOCX/Office ni convierte todo el historial Semantic
legacy en hechos: la migración 6→7 crea las tablas vacías y no inventa
recibos retroactivos. Si un payload pre-v7 compatible se reutiliza, el owner lo
verifica y emite bajo demanda una attestación separada; esa materialización
declara la inspección del payload, no finge haber ejecutado el modelo original.

## CapabilityManifest y CapabilityBroker v1

**IMPLEMENTED — selección por trabajo Text.** `neocortex.capabilities.broker` es
una capa stdlib-only que define manifests, requests, políticas, observaciones de
readiness, evaluaciones y una selección o abstención explicable. No importa un
extractor, abre estado, descarga modelos ni ejecuta providers. Los contratos son
inmutables, versionados y acotados; manifiesto, request, política y selección se
serializan canónicamente y exponen fingerprints SHA-256.

Un `CapabilityManifest` declara identidad y versión de capacidad,
implementación/provider, lifecycle, plataformas, modalidad, schemas, MIME e
idiomas, determinismo y clases de reproducibilidad, incrementalidad,
cancelación/checkpointing, cotas de entrada/timeout/CPU/RAM/GPU, red,
privacidad, extra opcional, componentes/binarios/modelos, compatibilidad,
métricas de calidad, coste y latencia conocidos. Un dato ausente permanece
ausente; no se inventan versiones, recursos o calidad.

El broker aplica primero filtros duros de request y política: readiness,
plataforma, modalidad, schemas, MIME exacto, idioma, tamaño, reproducibilidad,
lifecycle, privacidad/red, hardware, recursos, provider/implementación y
umbrales de calidad. Después compara preferencias declaradas de forma estable.
No usa “primer provider disponible”, no fabrica un score global y, si dos
candidatos elegibles quedan exactamente empatados, se abstiene con
`ambiguous_capability_selection`. La explicación conserva motivos de rechazo y
preferencia de cada candidato.

La integración productiva inicial declara dos manifests estáticos de
`text.extract/v2`: `neocortex.text.builtin` para texto, CSV, Markdown, HTML,
XML, JSON y EML; y `neocortex.text.legacy-office-worker` para DOC/XLS/PPT
binarios. Cada candidato Text construye un request con MIME y bytes exactos,
plataforma y schemas; admite `environment_bound` o `non_replayable` según el
manifest elegido. La política `neocortex-text-local-v1` prohíbe red, exige
privacidad `local_only` y no ofrece GPU. El builtin conserva reproducibilidad
`environment_bound`, declara `incremental=true` y es cacheable bajo las
validaciones físicas/causales de Text. Sólo los MIME builtin exigen
incrementalidad en su `CapabilityRequest`. El worker Office heredado v2 declara
`best_effort`, `non_replayable` e `incremental=false`. Su readiness resuelve y
fija un backend exacto: DOC prueba `soffice`/`libreoffice` antes de `catdoc`;
XLS y PPT prueban primero `xls2csv` y `catppt`, respectivamente, y después
`soffice`/`libreoffice`. La ausencia de LibreOffice no degrada texto plano. El
orden de alternativas forma parte del manifest, readiness elige un único
launcher verificable y el worker no hace fallback oculto si éste falla.

Provider y versión, fingerprint del manifest, política y su fingerprint,
readiness, explicación y fingerprint de ejecución forman parte de la
configuración efectiva del `WorkReceipt` Text y, por tanto, de su firma de
procesamiento. En Office heredado también se conservan SHA-256/tamaño del
ejecutable y un digest de su ubicación resuelta; el worker vuelve a comprobar
esa identidad antes y después del proceso. Esa atestación cubre el launcher
seleccionado, no una clausura transitiva arbitraria de engines, librerías o
procesos descendientes. Por seguridad, todo intento legacy —incluidos éxitos y
fallos con firma invariable— se ejecuta otra vez y su receipt declara
`non_replayable`; nunca se consulta la caché legacy. El builtin sí puede publicar
`cache_hit` como `environment_bound`. Una abstención se registra como intento
fallido owner-local sin materializaciones ni heads. El fingerprint del manifest
identifica el contrato declarado y no se reutiliza falsamente como
`implementation_digest`; observar un launcher tampoco certifica toda su cadena
de suministro. El tradeoff es explícito: Office heredado continúa seleccionable
y seguro, pero no afirma incrementalidad ni “procesar sólo cambios”.

La superficie visible es opt-in:

```text
Neocortex doctor capabilities --select text.extract \
  --mime-type text/plain --input-bytes 4096 [--json]
```

Devuelve schema `neocortex.capability-selection/v1`, status `selected` o
`unavailable`, request/policy, candidatos, explicación y fingerprints. El
diagnóstico agregado `Neocortex doctor capabilities [--json]` conserva schema
1, orden, salida y códigos previos; no construye el broker salvo que exista
`--select`. Ambas superficies siguen sin cargar modelos ni crear estado.

**PLANNED — no implementado en este corte.** Las rutas `pdf`, `docx` y
`office`, Semantic y los plugins/providers externos todavía no consumen el
broker. No existe registro automático de plugins, sandbox de providers externos
ni protocolo fuera de proceso. La extensión debe hacerse por una ruta vertical
a la vez después de estabilizar estos contratos, sin dependencias pesadas base.

## ReviewTask durable y salud del conocimiento

**IMPLEMENTED — contrato transversal y primera vertical Value.** Framework
schema 21 añadió, dentro del owner que ya coordina revisión, seis familias:
`review_task_batches`, `review_tasks`, `review_task_batch_memberships`,
`review_task_events`, `review_task_scan_progress` y
`review_task_source_publications`. No aparece una base global nueva. Cada batch fija
scope, tipo, selector y fingerprint/snapshot fuente, conserva un receipt
canónico y admite como máximo 1,000 inputs examinados y 100 tareas. Las tareas
son versiones inmutables; sus eventos son append-only y las transiciones usan
CAS sobre estado/evento anterior. La migración 20→21 sólo crea este contrato
vacío: no convierte candidatos o decisiones históricos en tareas sintéticas.
Framework schema 22 conserva esas seis familias y migra la identidad de rutas a
`BINARY` en Linux y `NOCASE` en Windows. También versiona el contrato de
lifecycle: v21 permanece validable como predecessor exacto y v22 permite sólo
la reapertura receipt-backed de decisiones cuyo scope tipado expiró.

`ReviewTask` mantiene separados el hallazgo derivado y la decisión humana. Sus
estados son `OPEN`, `IN_REVIEW`, `RESOLVED`, `DISMISSED` y `SUPERSEDED`; una
resolución o descarte exige actor humano y decisión durable. El scope puede ser
permanente, hasta cambio de fuente o hasta cambio de selector; el repositorio
revalida recurso, fingerprint, selector y successor dentro de la misma
transacción. Decisiones legacy no se reabren. `SUPERSEDED` está reservado a
receipts sistémicos exactos. El
batch, memberships, tareas, eventos iniciales, progreso y, al terminar con
evidencia completa, el head fuente se publican en transacciones Framework
owner-local; no se
declara atomicidad con Inventory o Catalog. El fence fuente se vuelve a leer
antes de publicar y cualquier cambio causa abstención.

La primera productora real es `review value`. El comando predeterminado sigue
read-only: consulta una cola vigente cuando existe en Framework v22 y, ante un
schema anterior o sin cola, conserva el preview legacy sin DDL. La variante explícita
`review value --refresh --scope personal|framework` puede crear/migrar Framework
y avanza exactamente una página keyset de 100 observaciones. Es advisory,
rechaza `all`, nunca escribe Inventory/Catalog ni toca el corpus y permite
recorrer más de 25,000 observaciones sin quitar la cota. El epoch de evaluación
de un scan incompleto es durable a través de límites diarios; mientras se
construye un epoch nuevo o cambia el owner fuente, el último head completo se
mantiene visible como `stale` y conserva eventos humanos posteriores.

El progreso distingue dos hechos independientes: `scan_complete` sólo confirma
que el cursor keyset llegó al final; `evidence_complete` acumula la salud de
todas las páginas. Una página parcial vuelve parcial a la corrida completa. En
ese estado no se infiere que un finding desapareció y las tareas abiertas del
snapshot anterior no se superseden por ausencia. Falta de un head publicado,
plan de duplicados inválido, owner ausente o mismatch de Catalog se conserva
como causa durable, aunque la última página sí haya terminado el recorrido.

Knowledge expone heads ReviewTask sólo después de reconciliar source receipt,
progreso y la cadena completa alcanzable de batches y memberships; añade
watermarks de batches, eventos y publicaciones fuente en Framework v22.
Retention trata tareas y eventos humanos como holds separados y protege además
el head vigente, toda esa cadena publicada y su progreso exacto. La auditoría
owner-local está acotada a 1,024 heads, 10,000 batches, 1,000,000 memberships y
128 MiB de payload. El resto de la coordinación sistémica no se eleva a
conocimiento humano. Esto permite detectar cambios y proteger decisiones sin
convertir ReviewTask en autoridad sobre otros owners.

**IMPLEMENTED / PARTIAL — verticales Text y PDF.** `Knowledge Asset Health`
acepta una identidad estricta `resource:file:<volume>:<file>:<birthtime>` y
reconstruye facts acotados desde Inventory, el owner fuente, Catalog y la
selección de Knowledge search. El dispatch elige Text o PDF únicamente mediante
la identidad física, probes packed/legacy, el snapshot y, como desempate,
evidencia Catalog publicada; nunca usa el path o la extensión.
Cada owner SQLite se abre de forma immutable, con schema exacto y sin
checkpoint. La captura completa de Knowledge y la captura de facts se repiten;
ante cambio se reintenta una sola vez y después se abstiene. Sólo cuatro facts
completos, publicados, estables y causalmente alineados producen `healthy`. El
reporte es advisory, read-only y no autoriza mutación.

La proyección PDF lee schema 13 y conserva los estados
`done|partial|protected|error|processing`, rango y conteos de páginas,
page-staging, page-errors, warnings, FTS y las relaciones Catalog/Search. La
recuperación sólo cuenta como reconocida cuando metadata contiene
`neocortex_recovery.engine` (`pdfminer` o `qpdf+pymupdf`) y
`recovery_version=pdf-structural-recovery-v2`; campos top-level o versiones
desconocidas no se reinterpretan. PDF vacío coherente es válido;
`protected`/`error` pueden cerrar sin Catalog/Search, `processing` queda
degradado/parcial y únicamente un `done` completo y coherente puede ser
`healthy`. El reader es content-blind: no descomprime páginas ni devuelve texto,
metadata o mensajes de error.

Esto no constituye todavía un árbol general para Semantic, DOCX/Office,
entidades, claims, links o shadow promotion. Tampoco mide contenido/OCR,
fidelidad visual, verdad semántica o calidad del texto ni demuestra power loss.
Framework `route_runs` es evidencia opcional y no autoridad de salud. La cola Value conserva
su propio estado `ready`/`partial`/`stale`/`absent`/`unavailable`; no se fusiona
en un score de salud. Las tareas ReviewTask para esos dominios y una GUI
consumidora siguen planificadas.

## Planificador semántico read-only

`plan_semantic_index()` y `--semantic-plan {text,image,all}` calculan un
preflight determinista sobre estado durable existente. No adquieren modelos,
no crean jobs y no modifican las bases propietarias. Usan un SQLite scratch
privado, con cuota dura predeterminada de 512 MiB, para deduplicar y proyectar
contenido con memoria acotada; exceso, cancelación o bloqueo persistente fallan
cerrado.

Cada base física se abre en su propia transacción de lectura y se protege con
versión de esquema, fingerprint XXH3 del snapshot y un fence `data_version`
antes/después. Los logical owners Office que comparten archivo se leen dentro
de una sola transacción física. Esto evita mezclar vistas de una misma base,
pero no crea atomicidad cross-database; el contrato lo declara explícitamente.

Cada workload conserva modalidad, rol, modelo, versión, proveedor, espacio
vectorial, dimensiones, dtype, normalización, distancia, procedencia y firma de
procesamiento. El planner calcula reutilización preexistente y entre workloads,
bytes vectoriales como cota inferior y solicitudes al modelo como rango entre
contenido único nuevo y entidades aún no reutilizadas. El rango de segundos de
modelo sólo aparece con una calibración exacta de ejecución/procesamiento/
workload/modelo/rol; sin ella queda desconocido en vez de extrapolar una tasa.

Imagen y `all` se planifican sólo desde caché. No se abren originales, por lo
que `originals_verified=false`, `execution_ready=null` y `complete=false` son
resultados deliberados, no una inferencia de disponibilidad. El JSON estable y
la salida humana exponen esos límites junto con la cuota y los hashes de
snapshot.

## Paquetes y responsabilidades

### `neocortex`

Paquete de instalación mínimo:

- declara la versión pública en `neocortex.__version__`;
- expone `neocortex.interface.entrypoint:entrypoint`;
- soporta `python -m neocortex`;
- ofrece la fachada read-only de scopes, CLI humana y MCP/stdio;
- contiene utilidades compartidas de ciclo de vida y contrato SQLite;
- declara los contratos puros y manifests estáticos de capacidad, además de los
  probes ligeros que alimentan al broker sin cargar providers.

No implementa el pipeline completo. Su función es ofrecer una frontera estable
y evitar imports pesados durante ayuda, versión o selección de modo.

### `neocortex.platform`

Frontera canónica de contratos y primitivas compartidas de plataforma:

- `content_types.py` detecta tipos únicamente con evidencia acotada de
  contenido, sin convertir la extensión en una afirmación;
- `zip_safety.py` inspecciona estructuras ZIP y recupera miembros DEFLATE bajo
  límites explícitos antes de materializar metadatos;
- `architecture_projection.py` proyecta grafos y evalúa el DAG de familias;
- `capability_registry.py` y `capability_registry_specs.py` declaran el
  registro de capacidades y sus relaciones, sin ejecutar providers ni abrir
  estado.

La plataforma expone únicamente los módulos canónicos de este namespace; no
existe una segunda implementación ni un alias físico paralelo.

### `neocortex.enumeration`

Frontera de enumeración por plataforma:

- Windows conserva enumeración MFT y lectura/resolución de registros USN;
- Linux ejecuta un recorrido completo portable, case-sensitive y sin seguir
  enlaces simbólicos;
- los snapshots conservan identidad durable y metadatos: FileId/volumen en
  Windows, `st_dev`/`st_ino` en Linux;
- Linux persiste `birthtime_ns=-1` si el filesystem no expone nacimiento real;
  nunca usa `ctime` como sustituto;
- el índice SQLite auxiliar de rutas usa la collation de cada plataforma.

Produce observaciones; no decide eliminación ni clasificación. El
`SqlitePathIndex` es una API auxiliar soportada y probada, pero no se confirmó
un consumidor dentro de la corrida integrada actual.

### `neocortex.deduplication`

Propietario del inventario común:

- scans e inventarios;
- publicaciones por raíz con cursor USN opcional;
- fingerprints no criptográficos de contenido propio;
- grupos y planes de duplicados;
- comparación exacta inmediatamente antes de una mutación autorizada.

El esquema fuente actual es v10. Conserva la clave `(scan_id, path)` y los scans
`building`, `complete` y `partial` introducidos por v7, liga cada scan nuevo a
su `inventory_policy_signature` y publica un checkpoint que referencia una
generación completa. El cursor USN del checkpoint es opcional y sólo acelera
la siguiente enumeración; la publicación portable sigue siendo consumible por
Knowledge y Semantic. Las migraciones históricas v6→v7, v7→v8 y v8→v9 tienen
regresiones específicas; v9→v10 agrega índices de identidad sin cambiar la
semántica generacional. Los diagnósticos no migran una base existente y la
actualización debe seguir el procedimiento respaldado de
[PERSISTENCE.md](PERSISTENCE.md).

### `neocortex.progress`

Contratos de eventos y reporteros. Separa el progreso del motor de la
representación Rich, texto o protocolo de GUI. Las rutas no deben depender de
widgets ni escribir directamente a una terminal para informar avance.

### Composición canónica del producto

La composición canónica agrupa cada responsabilidad bajo su propio paquete y
mantiene una sola implementación física. Contiene:

- configuración, parser, validación y reporte CLI;
- fachada plana `ApplicationConfig` compatible con `FrameworkConfig`, nueve
  proyecciones de ruta y una proyección de límites globales calculadas desde el
  valor vigente;
- superficies de registro/validación CLI separadas para Audio, Video, Code,
  Semantic y Knowledge, sin cambiar sus flags planos;
- orquestador, locking, cancelación y heartbeat;
- selección y registro de rutas;
- coordinador global de recursos;
- extractores, clasificadores, cachés y repositorios por formato;
- catálogo documental, organización, revisión y evidencia;
- búsqueda PDF/DOCX/audio/video/código y servicio semántico;
- contratos de estado, relaciones y proyecciones necesarias para las
  capacidades de contenido, sin una plataforma de evidencia de QA del propio
  repositorio;
- contratos, snapshot lógico, planner, recuperación, fusión y contexto de la
  Knowledge Plane read-only;
- watcher incremental foreground;
- acciones autorizadas sobre archivos, recibos de efecto, eventos append-only y
  conciliación de sólo lectura. Una operación `record` separada puede conservar
  la observación como evento append-only; decisión, autorización, recuperación
  y verificación productivas todavía no existen.

Es el paquete más grande y concentra integración, pero las rutas mantienen
bases y modelos propios para limitar transacciones cruzadas.

### `neocortex.interface`

Frontend PySide6:

- transforma el formulario en una solicitud canónica;
- inicia un único worker hijo mediante `QProcess`;
- intercambia eventos estructurados y acotados;
- permite cancelación supervisada;
- consulta estado mediante conexiones cortas de sólo lectura;
- añade una página Consulta sobre `read_api` y `value_cli_adapter`, con scopes
  fijos, presentación acotada y sin controles de mutación.

La GUI ofrece PDF, DOCX, Office, ZIP, texto/correo, audio, video, imagen y Code. En
Linux presenta modo portátil, no solicita elevación y desactiva los controles
de mutación, sin retirar inventario, procesamiento o búsqueda.

### Topología de raíz

Las raíces numeradas y el shim independiente anterior fueron retirados del
checkout y del wheel; no existe una segunda implementación ni una fachada
paralela. Las invocaciones soportadas son `Neocortex`, `python -m neocortex` y,
para el planner dedicado, la ruta integrada `Neocortex` con sus opciones de inventario.

## Superficies públicas

### CLI instalada

La invocación canónica es:

```bash
Neocortex --help
```

`neocortex.interface.entrypoint` selecciona perezosamente cuatro modos:

1. subcomandos humanos `help/status/search/ask/inspect/review/knowledge/agent`;
2. CLI normal: delega en `neocortex.api.cli.cli_app`;
3. `--ui`: inicia la aplicación de escritorio;
4. `--gui-worker`: protocolo interno del frontend, no comando de usuario.

Las operaciones directas se registran declarativamente y cargan su handler de
forma lazy. Archive, texto, audio, Code, Semantic y Knowledge separan registro
y validación en sus módulos `cli_*_surface.py`; los handlers conservan sus
módulos de dominio.
Las operaciones que escriben estado adquieren el lock común cuando su contrato
lo requiere. La lista de comandos y códigos de salida está en
[CLI.md](CLI.md).

`Neocortex doctor capabilities [--json]` es un alias canónico estrecho que
`neocortex.interface.entrypoint` traduce a flags planos internos ocultos. El handler inspecciona
specs, metadata y ejecutables sin cargar engines/modelos ni crear estado; no
introduce un `--doctor` o `--json` global.
La variante opt-in `--select text.extract --mime-type MIME --input-bytes BYTES`
ejecuta la selección explicable por trabajo bajo la política local Text y usa
schema `neocortex.capability-selection/v1`; sin `--select`, el reporte agregado
conserva schema 1 y no construye el broker.

La Knowledge Plane se expone mediante operaciones directas mutuamente
excluyentes y no destructivas:

```bash
Neocortex --knowledge-status
Neocortex --knowledge-health "resource:file:1:2:-1" --knowledge-json
Neocortex --knowledge-search "protección diferencial" --knowledge-mode evidence
Neocortex --knowledge-context "protección diferencial" --knowledge-limit 12
```

Estas operaciones sólo abren estado existente y pueden informar owners
ausentes, incompatibles, futuros o corruptos sin crearlos ni migrarlos. Sus
formatos, opciones auxiliares y códigos de salida se detallan en
[KNOWLEDGE.md](KNOWLEDGE.md).

Las operaciones de Code se limitan a contenido del corpus y se exponen mediante
`Neocortex --code-status`, `Neocortex --code-search`, `Neocortex --code-projects`
y `Neocortex --code-reconstruct`. Son lecturas acotadas del índice publicado;
no ejecutan auditorías del repositorio, review interno, experimentos ni
retención de evidencias de QA.

`Neocortex agent serve` adapta la misma API a MCP por stdio. El transporte
CPython 3.14 usa pipes asyncio nativos para evitar delegar stdin/stdout a
workers AnyIO; limita cada línea a 1 MiB y termina limpiamente al cerrar stdin.
No registra HTTP, rutas de estado ni productores.

`--action-recovery-status` es una excepción deliberada: abre
`framework.sqlite3` sin crearla ni migrarla y clasifica acciones inciertas sin
repetir una syscall. Su salida JSON pertenece sólo a esa familia.

### API Python

`neocortex.api.public` expone perezosamente configuraciones, summaries,
rutas, orquestador, búsquedas, doctors y coordinador de recursos. Las clases de
ruta y `PdfDerivedIndexer` son superficies de bajo nivel: un consumidor que las
invoque fuera del orquestador debe respetar inicialización de esquema,
cancelación, recursos y exclusión de writers. La ejecución canónica mediante
`FrameworkOrchestrator` es la frontera que aplica el contrato integrado.

`neocortex.sdk` es la fachada pública lazy y tipada PEP 561 para Knowledge. Sus
símbolos resuelven únicamente contratos canónicos y el paquete distribuye
`py.typed`.

La superficie diferida también exporta `ResourceRef`, `RevisionRef`,
`EvidenceRef`, `KnowledgeHit`, `KnowledgeSnapshot`, `ContextBundle`,
`KnowledgeQuery`, `KnowledgePlan`, `RetrievalMode`, `KnowledgeStatePaths`,
`KnowledgeSearchResult`, `KnowledgeSearchService` y
`plan_knowledge_query`. Estas APIs consultan estado persistente; no sustituyen
la corrida que lo produce.

`neocortex.api.read_api.asset_health_payload()` expone las mismas verticales Text/PDF
sobre el estado publicado, sin aceptar rutas arbitrarias. La consulta Code usa
`code_search_payload()` y conserva la misma frontera read-only.

`route_registry` sólo contiene adaptadores y contratos de ejecución; los
consumidores importan cada ruta desde su módulo canónico.

## Corrida integrada

Una corrida normal sigue este orden lógico:

1. validar argumentos, raíz, estado y compatibilidad;
2. adquirir `${XDG_STATE_HOME:-$HOME/.local/state}/Neocortex/state/framework.lock`
   mediante un lock del sistema operativo;
3. inicializar esquemas y marcar runs/acciones abandonados según la política
   vigente;
4. abrir el run común y su heartbeat;
5. intentar capturar el cursor USN inicial, sin exigirlo;
6. preparar inventario completo o incremental y su checkpoint;
7. construir el plan de deduplicación;
8. ejecutar sólo las acciones expresamente autorizadas;
9. publicar atómicamente el vínculo al inventario y el conjunto completo de
   candidatos de ruta;
10. iniciar las rutas de contenido seleccionadas;
11. actualizar catálogo y organización cuando corresponda;
12. podar estado transitorio permitido;
13. completar el run y detener el heartbeat;
14. liberar el lock.

Errores y cancelación toman ramas distintas. `KeyboardInterrupt` solicita
cancelación cooperativa y el launcher devuelve `130`; una ruta fallida no debe
presentarse como completada.

La enumeración, el inventario y las rutas escriben distintas bases. Por ello la
finalización del run común no reemplaza los invariantes locales de publicación
de cada propietario.

La publicación del snapshot de enrutamiento ocurre después de que
`FrameworkActions.execute()` termina de persistir todos los candidatos. El
`scan_id`, los contadores de inventario y el evento versionado
`neocortex.routing-snapshot/v1` se confirman en la misma transacción de
`framework.sqlite3`; una ruta no puede iniciarse mientras ese vínculo no exista.

Para corridas normales, `NormalInventoryBoundary` captura raíz, estado,
`InternalPathsPolicy` y exclusiones. La policy reserva por ruta e identidad
física el repositorio, runtime, datos de aplicación y launcher;
detecta aliases/reparses y el hardlink del launcher. Los árboles internos que
quedan bajo un corpus permitido se excluyen, pero una raíz situada dentro de
ellos se rechaza. El estado tampoco puede ser igual ni ancestro del corpus. La
firma cruda de `InventoryExclusionPolicy` se guarda desde Dedup v9; Framework y
watcher usan la firma efectiva versionada que combina esa firma con la de
`InternalPathsPolicy`.

## Code como contenido

La capacidad Code pertenece al producto porque permite procesar código del
usuario como contenido: descubre proyectos, detecta lenguajes, representa
símbolos y dependencias, guarda versiones, indexa texto y ofrece búsquedas y
relaciones semánticas. Sus módulos productivos se concentran en
`neocortex/code/ingestion`, `neocortex/code/search`, `code_contracts`,
`code_route`, `code_schema`, `code_state` y `code_retention`.

Code no audita el repositorio de NeoCortex ni mantiene review interno,
experimentos, proveedores externos, epistemología, work packages o receipts de
validación. Las herramientas de desarrollo se ejecutan fuera del runtime y de
forma individual: `pytest`, Ruff, Pyright/Mypy, Semgrep u otra herramienta que
corresponda a la propiedad modificada. No existe un agregador automático de
calidad como capacidad del producto.

El esquema Code nuevo contiene sólo las tablas necesarias para observaciones,
proyectos, símbolos, referencias, dependencias, diagnósticos, chunks y enlaces
semánticos. Las tablas de proveedores o experimentos que puedan aparecer en una
base histórica se leen únicamente para compatibilidad y no son producidas por
la ruta actual ni autorizan retención o mutación.

La CLI pública expone únicamente operaciones de contenido:
`--code-status`, `--code-search`, `--code-projects` y `--code-reconstruct`.
`Neocortex --all` ejecuta el flujo documental y no consulta ni produce
autoanálisis del repositorio.

## Mutación ligada a identidad y recuperación

Las mutaciones soportadas de rename y organización usan
`windows_handle_mutation.rename_no_replace_by_identity`. La primitiva mantiene
abiertos el archivo fuente y el directorio destino, verifica volumen/FileId y
opera de forma relativa al handle del padre con semántica *no-replace*. El
contrato es deliberadamente estrecho: Windows, NTFS local, archivo regular, un
solo hard link y mismo volumen. UNC, otros filesystems, reparses, directorios,
hard links múltiples y movimientos entre volúmenes provocan abstención; no hay
fallback permisivo por ruta.

Ese backend es exclusivamente Windows. En Linux, `--apply` y
`--organization-apply` se rechazan antes de crear estado con código `2` y razón
`linux_mutation_backend_unavailable`; no existe un fallback con `Path.rename`.

`file_actions` conserva en framework v22 la frontera incorporada en v18 y
endurecida en v20:

```text
started -> applying -> applied
                    \-> recovery_required
```

`applying` se persiste con identidad esperada justo antes de la llamada nativa;
`applied` exige un recibo posterior. Si el proceso o el registro fallan después
de cruzar la frontera, la acción queda `recovery_required`. Cada transición
agrega una fila a `file_action_events`; triggers impiden actualizar o borrar
esos eventos. Al reiniciar, una acción `started` abandonada antes de la
frontera queda `failed` con evidencia de que no se intentó el efecto; una
`applying` abandonada queda `recovery_required`. Ninguna se repite
automáticamente.

El conciliador observa origen y destino y devuelve `confirmed`,
`not_performed`, `ambiguous` o `impossible_to_check`. `status` es idempotente y
de sólo lectura. `record` agrega a `file_action_reconciliation_events` una
observación append-only con CAS, key idempotente, actor, procedencia, firma y
evidencia, pero declara que no autoriza una mutación. No hay todavía contratos
`decide`, `authorize`, `recover` o `verify`. Un recibo de Papelera sólo
confirma la acción si liga las rutas origen/destino de esa misma acción, aunque
la aplicación de Papelera sigue deshabilitada. Los planes de organización
conservan su propio
`recovery_required`, excluido del selector automático y del reintento; además
reserva el destino para evitar que otro plan lo reutilice.

La API de Papelera disponible era path-bound. Por ello `0.7.0` conserva la
planeación y validación en dry-run, pero `--apply` se abstiene y registra esas
acciones como `skipped`. `Send2Trash` fue retirado y no se ofrece un override
inseguro.

## Registro y ejecución de rutas

El orden estable es:

| Ruta | Entrada principal | Salida persistente | Consumidor adicional |
|---|---|---|---|
| `pdf` | snapshots identificados como PDF | texto, páginas, OCR, warnings, FTS, similitud y layout | catálogo documental |
| `docx` | OOXML Word validado | partes, texto, diagnósticos, FTS, layout y vínculos PDF | catálogo documental |
| `office` | OOXML/ODF de otros documentos | texto, XLSX por celda tipada, estado y FTS | catálogo documental |
| `archive` | ZIP y ZIP anidados validados | miembros virtuales, cadena de contenedores, texto nativo/OCR, incidencias y FTS | Knowledge y Semantic; no organización física |
| `text` | texto imprimible, EML y CFB DOC/XLS/PPT | texto visible, título/autor, metadata, errores y FTS | catálogo documental, Knowledge y Semantic |
| `audio` | audio/vídeo sondeado | transcripción, segmentos y FTS | catálogo documental |
| `video` | streams visuales sondeados | escenas/keyframes, frames, OCR, timestamps, métricas y FTS | búsqueda directa y revisión; visual-only admitido |
| `image` | imágenes no documentales o candidatas de documento | clasificación, OCR/evidencia, estado y huella completa Dedup | catálogo multimodal, revisión y Semantic cuando la cobertura está publicada |
| `code` | archivos de texto/código acotados | proyectos, versiones, AST/símbolos, referencias, grafo, chunks y FTS | búsqueda y puente semántico |

El lector legacy del grafo de código conserva schema 7 y el mismo owner ahora
incluye un ledger generacional aditivo con snapshot de entradas, lotes,
membresías, checkpoints y head/CAS. Una generación `building` no es visible
como head, los fallos por fase revierten el lote y la reanudación usa el último
checkpoint; el grafo transversal entre owners todavía requiere un contrato de
publicación y rollback posterior.

La reutilización exige la misma ruta observada, metadatos, firma y analizador
efectivo. Un hit de ruta invariable actualiza sólo presencia/observación y hace
cero DML en `code_fts`; una ruta distinta rechaza la caché y el productor
publica una versión sucesora, en vez de mutar la evidencia histórica.

En una corrida completa sin límite ni selección, `mark_missing` precede al
grafo. Una finalización real elimina y reconstruye las membresías derivadas y
sincroniza en una sola sentencia las etiquetas FTS vigentes realmente distintas
mediante un mapa temporal indexado; las etiquetas históricas permanecen
inmutables. El resolver vigente materializa conjuntos temporales indexados de
símbolos y dependencias vigentes. Primero enlaza llamadas dentro del mismo
módulo o clase y módulos relativos por su ruta léxica exacta; después aplica el
fallback global por nombre cualificado o simple sólo cuando la coincidencia es
única. Los empates y ausencias permanecen ambiguos o no resueltos; no se
fabrican aristas. Sólo se omite si no cambió
ninguna entrada,
todos los candidatos fueron cache hits compatibles con el runtime y un fence
tipado `code-graph-resolver-v7` identifica exactamente el run completo
inmediatamente anterior con la misma firma. El fence avanza atómicamente con la
finalización de ese `analysis_run`; ausencia, corrupción, run intermedio o la
primera corrida sobre una base existente sin fence fallan cerrados hacia
`finalize_graph`.

El analizador Python `neocortex-python-ast` versión 3 conserva además el nivel y
módulo de imports relativos —incluido `from . import módulo`— para que paquetes
con basenames repetidos no se mezclen. Sólo publica símbolos de
asignación para nombres realmente enlazados por objetivos `Name`,
`Tuple`/`List` y `Starred`. Atributos y subscripts no crean símbolos globales o
de clase espurios, y un nombre repetido en la misma asignación se conserva una
sola vez.

El puente Code↔Semantic conserva los propietarios separados. Una publicación
textual completa de `source_kind=code` termina primero el head Semantic v7
—que preserva el contrato generacional introducido en v6— y,
bajo el lock común del framework, proyecta en `code.embedding_links` un enlace
por chunk vigente. Cada enlace fija item Semantic, modelo, espacio vectorial,
generación y procedencia; no existe FK ni transacción distribuida entre las dos
bases. La proyección se construye completa en TEMP y falla antes de mutar si un
miembro no resuelve exactamente por identidad, versión, índice y clase de chunk.
La búsqueda Code acepta un hit semántico sólo si ese enlace sigue activo y la
versión continúa vigente. Los enlaces de generaciones anteriores se conservan
inactivos como historia reconstruible; no son evidencia publicada actual.

`RouteAdapter` recibe un `RouteExecutionContext` con configuración, raíz,
`run_id`, `scan_id`, estado corto de framework, cancelación y coordinador global.
La summary debe ser dataclass o mapping serializable.

PDF e imagen tratan el stream de candidatos como un recurso con afinidad de
thread: creación, iteración y cierre ocurren en el thread propietario de la
conexión SQLite. Un `finally` del productor lo cierra también ante error o
cancelación; el coordinador no desenrolla ese generator desde otro thread.

Las rutas PDF, DOCX, Office, texto y audio alimentan el catálogo por lotes.
Archive, imagen y código conservan repositorios especializados; no deben
presentarse como documentos catalogados si no existe ese consumidor. Knowledge
consume FTS de Archive y texto directamente; Archive conserva la cadena
`ZIP!/miembro` como procedencia y no entra a organización física.

## Concurrencia y cancelación

### Exclusión global

`FrameworkRunLock` bloquea un byte de `framework.lock`. El orquestador y varias
operaciones directas de escritura lo adquieren para impedir dos corridas
integradas simultáneas sobre el mismo estado. El archivo no registra PID ni
línea de comandos; un error de contención sólo prueba que otro handle mantiene
el lock.

No se debe borrar el lock para “desbloquear” una ejecución. El sistema operativo
libera el bloqueo al cerrar el handle; primero debe identificarse el proceso
propietario.

### Paralelismo de rutas

Las rutas seleccionadas se envían a un `ThreadPoolExecutor` acotado por su
cantidad. Cada una recibe:

- un estado de framework de vida corta;
- token de cancelación común;
- coordinador global de memoria, commit, CPU y carga;
- su propia base de ruta.

El orquestador espera las rutas, conserva errores por nombre y cancela de forma
cooperativa si una ruta falla o el usuario interrumpe. Algunas rutas usan
procesos `spawn` supervisados para aislar bibliotecas nativas y timeouts.

El lock PDF adicional es un `RLock` **local al proceso**. Serializa writers del
proceso padre, pero no sustituye `framework.lock` ni protege consumidores Python
externos en otro proceso.

### Watcher

El watcher:

- corre en primer plano;
- usa lotes USN como señal de que debe reconciliarse;
- no publica un cursor independiente;
- vuelve a cargar el checkpoint durable después de cada corrida;
- aplica debounce y backoff acotados.

Además del `threading.Lock` por instancia, `WatcherLifeLease` mantiene un byte
lock del sistema operativo durante toda la vida del proceso para la identidad
canónica `(root,state_directory)`. Su nombre usa XXH3-128 y sus metadatos
acotados registran PID, creación del proceso, host, versión, argv, raíz, estado e
inicio. El lock del SO es la autoridad: un owner vivo provoca abstención; JSON
stale sólo se reemplaza después de adquirirlo y nunca se mata un proceso. El
handle se libera en cierre normal o caída. Raíces distintas no colisionan y las
corridas directas conservan `framework.lock` por corrida.

## GUI y worker

El proceso de UI no ejecuta el pipeline dentro del event loop. `WorkerController`
crea un proceso hijo con el mismo intérprete y el módulo
`neocortex.interface.protocol.worker`.
El worker:

- reconstruye parser y configuración canónicos;
- emite eventos estructurados de progreso y terminales;
- mantiene heartbeat supervisado;
- escucha cancelación;
- captura `KeyboardInterrupt` y `BaseException` para emitir un cierre
  observable;
- no se desprende ni se instala como servicio.

Las líneas y buffers están limitados. La ventana conserva un historial visual
acotado; ese historial no sustituye las tablas persistentes de eventos.

La página Consulta no usa el worker productor: llama de forma diferida a la API
read-only compartida, valida schema/kind/scope/exit code y limita la
presentación a 200 filas y 128 KiB. Status y revisión no requieren texto;
search/ask aceptan hasta 4096 caracteres. Una incompatibilidad provoca
abstención visible y no intenta migrar ni reparar.

## Persistencia y flujo de datos

Las ubicaciones persistentes por usuario en Linux son:

```text
Estado normal: ${XDG_STATE_HOME:-~/.local/state}/Neocortex/state
Configuración: ${XDG_CONFIG_HOME:-~/.config}/Neocortex
Releases:      ${XDG_DATA_HOME:-~/.local/share}/Neocortex/releases
Modelos:       ${XDG_DATA_HOME:-~/.local/share}/Neocortex/models
```

Las bases principales son `dedup`, `framework`, `pdf`, `docx`, `office`,
`archive`, `text`, `audio`, `video`, `image`, `document_catalog`, `code` y
`semantic`.
No todas existen antes de usar su ruta. La UI persiste configuración en el árbol
XDG y FastEmbed usa el cache compartido `models/fastembed`.

La Knowledge Plane no es otro owner persistente: conserva once owners base
históricos y agrega Archive y texto sólo cuando existen sus bases. Su snapshot
y resultados viven en memoria y no introducen una migración propia.

En Dedup v10, `DedupIndex.published_snapshots(root)` conserva el lector público
introducido en v9. Para recorrer la generación vigente, checkpoint y filas se
seleccionan en una sola sentencia SQL y conservan el snapshot del lector ante
una publicación y poda concurrentes. Cada scan nuevo conserva su firma cruda de
exclusión. La migración
7→8 preserva scans, archivos y bytes, pero invalida checkpoints legacy sin firma
en vez de inventar evidencia; 8→9 conserva publicaciones y vuelve opcional el
cursor USN como una terna indivisible. La migración 9→10 sólo agrega índices
para joins ligados a identidad. No combine por cuenta propia
`inventory_checkpoint(root)` con `snapshots(scan_id)`; entre ambas llamadas otro
writer puede publicar y podar la generación elegida.

En semantic v7, cada `model_signature` tiene un único
`published_embedding_heads`. Una generación `building` clona de forma acotada
los miembros de una base fijada. El clon confirma por páginas un cursor durable
con high-watermark y conteo; comparte el deadline del productor y reanuda el
prefijo confirmado. Adjunta resultados a revisiones inmutables y sólo un cierre
completo, después de revalidar la base, cambia el head dentro de la transacción
de finalización. Un
cierre parcial queda `ready_partial` y no publica; un CAS perdido obliga a
rebase. Las búsquedas oficiales fijan los heads al inicio y resuelven hits desde
sus miembros/revisiones congelados. El contenido y la identidad publicados
permanecen inmutables, pero el localizador `path` se toma de `semantic_items`
sólo cuando coinciden `item_id`, `source_kind` y `source_identity`; así un move
confirmado no deja resultados apuntando al origen ni una identidad reasignada
puede redirigir evidencia histórica. El resolver contrasta además
`vector_space` y modalidad del hit con el modelo persistido; no confía en esos
campos suministrados por el llamador.

El staging textual mantiene una única sesión SQLite por `source_kind` y agrupa
cada transacción en un máximo de 128 items o chunks. Un item mayor se divide en
lotes de hasta 128 chunks. Error, cancelación o cualquier `BaseException`
revierte sólo la transacción en curso; el prefijo ya confirmado permanece
idempotente y reanudable dentro de la generación `building`. La desactivación de
miembros no observados ocurre al finalizar la fuente, y ningún prefijo parcial
cambia el head publicado. Esa mecánica de batching, introducida sin cambio de
schema en v6, se conserva en v7; el cambio de schema actual corresponde a
receipts y outbox de derivación.

El worker alcanza un punto fijo de reutilización exacta antes de cada claim:
agota jobs pendientes cuyo modelo, XXH3, longitud y guarda coinciden con un
payload durable. Por ello el payload creado por el batch N satisface duplicados
que sigan pendientes antes del claim N+1. Todo lease aún propio se libera ante
`RuntimeError`, `KeyboardInterrupt` u otra `BaseException` sin ocultar la
excepción original. Permanecen dos límites explícitos: duplicados ya incluidos
en el mismo batch pueden llegar juntos al backend y los commits/fallos por job
todavía realizan persistencia N+1.

Text v2 conserva `documents` y `document_fts` como salidas físicas compatibles,
pero agrega revisiones fuente inmutables, intentos, bindings de entrada/salida,
materializaciones, heads, `WorkReceipt` y outbox. La migración exacta v1→v2
preserva documentos y FTS, deja `documents.revision_id=NULL` y no fabrica
revisiones, materializaciones ni recibos para trabajo legacy que no puede
atribuirse. Las consultas de linaje lo presentan como
`legacy_unattributed` hasta que el productor lo reprocese.

En catálogo v7, cada `source_kind` construye filas en
`catalog_generation_documents`. Los lectores siguen viendo la proyección
`documents` anterior hasta que una transacción reemplaza esa fuente, agrega el
historial, reconcilia planes y cambia `catalog_publications` mediante CAS. Un
fallo o cancelación conserva el puntero previo; dos publicaciones competidoras
marcan la atrasada `superseded`.

Ambos contratos preservan generaciones fallidas o abandonadas para diagnóstico.
Un planificador dry-run puede inventariarlas y proteger publicaciones, bases y
leases. También bloquea generaciones semánticas referenciadas por evidencia y
protege el último run completado del framework. Todavía no existen
`prepare/apply/verify`, poda ni enforcement de cuotas. Consumidores externos
que consulten directamente las tablas legacy mutables no reciben estas
garantías.

La poda owner-local del inventario sólo puede ejecutarse cuando el coordinador
entrega explícitamente todos los `scan_id` retenidos por framework. Sin esos
holds falla cerrado; con ellos conserva la publicación vigente, la anterior y
cualquier referencia cross-store. No es un motor de retención genérico ni
elimina evidencia humana o acciones inciertas.

La propiedad de un esquema implica:

- un solo módulo decide DDL y migraciones;
- los writers deben usar su factory canónica;
- los lectores deben pasar por `SQLiteReadSession` y declarar
  `immutable_strict` o `snapshot_temp` cuando no modifican;
- las relaciones entre bases se expresan mediante identificadores y evidencia,
  no mediante foreign keys cruzadas;
- un run global no vuelve atómica una publicación local incompleta.

`neocortex.persistence.sqlite_connection` centraliza modos explícitos y salvaguardas
connection-local, pero su adopción productiva actual se limita a las factories
de PDF, DOCX y catálogo. `FrameworkRouteState` conserva una apertura separada
de estado existente mediante URI `mode=rw`; no se forzó una factory universal.
El inventario de esta fase registra 42 connects en 25 módulos y 132
adquisiciones mediante 20 factories de propietario. Consulte
[PERSISTENCE.md](PERSISTENCE.md) para la matriz exacta y los límites de SQL
externo/WAL.

La conexión URI `mode=ro` con `query_only=ON` quedó como compatibilidad histórica
y no debe describirse como byte-neutra. La barrera actual usa el kernel
`SQLiteReadSession`: immutable para owners quiescentes y snapshot temporal para
WAL activo, sin tocar bytes publicados. La validación de esta continuación
incluye fixtures de ambas topologías; no
abrió ni migró bases operativas vivas.

## Recursos y procesos externos

Controles observados:

- futuros de trabajo PDF e imagen acotados aproximadamente a `workers * 2`;
- batches del catálogo de 100 filas y de escritura semántica de hasta 500;
- staging semántico textual de hasta 128 items o chunks por transacción y una
  sesión SQLite por fuente;
- colas multiprocessing pequeñas para PDF, imagen y Whisper;
- límites de miembros, expansión y central directory antes de abrir ZIP/OOXML;
- OCR de PDF e imágenes dentro de ZIP y conversión de Office heredado en
  procesos aislados con tiempo, memoria y salida acotados;
- límites de píxeles, texto, páginas, duración y segmentos por ruta;
- subprocess con argumentos, timeout, drenaje concurrente y límite de salida;
- limpieza de temporales después de cerrar procesos y handles;
- admisión global según memoria física, commit, carga y slots CPU.

En Windows, los procesos aislados y `run_bounded_capture()` crean el hijo
suspendido, lo asocian por su handle exacto a un Job Object con
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` y sólo entonces lo reanudan. Timeout,
overflow, cancelación y excepciones terminan el Job, esperan al hijo directo,
cierran pipes y liberan el handle; así los descendientes propios no sobreviven
a la frontera supervisada.

En POSIX, el hijo usa sesión/grupo de proceso propio. Timeout, overflow,
cancelación y excepciones alcanzan todo el árbol con `SIGTERM` y después
`SIGKILL`. Un límite de memoria solicitado se impone mediante `RLIMIT_AS` o
`/usr/bin/prlimit`; si ninguna vía está disponible, la operación se abstiene en
vez de ejecutar sin contención.

Herramientas externas posibles:

- Tesseract para OCR;
- FFprobe/FFmpeg para audio y vídeo;
- qpdf opcional para recuperación estructural PDF;
- FastEmbed y Faster-Whisper para inferencia local.

Ruff, Pyright/Mypy, pytest, Semgrep y otras herramientas de desarrollo no son
dependencias del runtime ni productores de estado Code. Codex las ejecuta de
forma individual cuando una modificación lo requiere; sus resultados no se
convierten en receipts ni en una proyección productiva.

No se observó `shell=True` en el motor auditado. La presencia de límites no
equivale a sandbox completo; véase [SECURITY.md](SECURITY.md).

## Empaquetado y dependencias opcionales

El paquete se construye con setuptools y exige Python `>=3.13,<3.15`; la
plataforma objetivo vigente es Linux con CPython 3.14. Incluye `neocortex` y
los assets de la GUI. La base exacta incluye Packaging, Rich y
xxHash; `agent` declara MCP y `analysis` agrupa únicamente herramientas de
desarrollo como Complexipy, Cosmic Ray,
Coverage, Deptry, Grimp, Mypy, pip-audit, Pytest, Radon, Ruff y Vulture.
`documents`, `audio`, `image`, `semantic` y `ui` declaran runtimes de dominio;
`full` es la unión canónica de las capacidades de producto y `agent`; las
herramientas de calidad no se incluyen en el runtime.
`neocortex.capabilities` conserva el reporte agregado schema 1 y proyecta
readiness por implementación Text para el broker. Los contratos de selección
son stdlib-only; no certifican inferencia, caché de modelos, digest de binario
externo ni compatibilidad no observada.

La ayuda y versión deben arrancar sin cargar rutas pesadas. La instalación, el
wheel y el sdist deben validarse en un entorno limpio antes de publicar; este
documento no afirma que esa barrera final ya haya ocurrido.

En Linux, `tools/release_linux.py` instala el wheel `full` desde artefactos
binarios, activa `current` bajo `flock`, conserva sólo `current` y el rollback
inmediato, poda releases anteriores y publica launcher, alias y KDE sólo después
de validar modelos y runtime. Los recibos viven en el estado XDG.

El inventario técnico de metadata/licencias y archivos redistribuidos está en
[THIRD_PARTY_LICENSE_INVENTORY.md](THIRD_PARTY_LICENSE_INVENTORY.md). La metadata
declara `LicenseRef-Proprietary` y `Private :: Do Not Upload`; no concede
permisos. Las decisiones de licencia/NOTICE
pertenecen al propietario.

## Extensibilidad

Una ruta nueva debe definir antes de integrarse:

1. nombre estable y posición o política de orden;
2. tipos de entrada y detección;
3. configuración y límites;
4. base propietaria o contrato explícito de reutilización;
5. firma de procesamiento y política de caché;
6. summary serializable;
7. eventos de progreso y cancelación;
8. interacción con catálogo, revisión y semántica;
9. pruebas de error, reanudación, recursos y empaquetado;
10. documentación de dependencias y herramientas externas.

No debe añadirse una base, repositorio o clasificación sin productor y
consumidor confirmados.

**PLANNED.** Un provider externo no se descubre ni carga automáticamente en el
corte actual. Antes de plugins se deben conservar manifests estables, registro
explícito, permisos/red, timeout, recursos, cancelación, compatibilidad y salida
estructurada; un plugin nunca adquiere autoridad implícita sobre corpus u
owners.

## Compatibilidad y retirada de legacy

Clasificación actual:

| Elemento | Estado | Criterio de retirada |
|---|---|---|
| shim independiente anterior | retirado | no recrear; usar `Neocortex` o `python -m neocortex` |
| antiguas raíces numeradas de enumeración y deduplicación | retiradas | no recrear; las superficies canónicas viven bajo `neocortex` |
| exports de motores en `route_registry` | retirados | importar cada ruta desde su módulo canónico |
| agregadores `state`/`semantic_state`/`semantic_service` | superficies públicas | no duplican la implementación física ni crean una raíz paralela |
| `SqlitePathIndex` | auxiliar soportado, integración no verificada | decidir explícitamente si se integra o se depreca; no eliminar por análisis automático |

Una métrica de complejidad, vulture o ausencia de import interno no basta para
eliminar una API empaquetada.

## Riesgos arquitectónicos pendientes

Los siguientes límites deben permanecer visibles:

- `NC-AUD-001`, `NC-AUD-002` y `NC-AUD-003` quedaron corregidos en el código
  v7 y se conservan en v8 con regresiones de migración
  poblada/abstencionista, aislamiento, publicación, scan parcial, lectura
  concurrente, poda y cursor USN ambiguo; la barrera integral se registra
  aparte;
- la poda vigente de v10 conserva generaciones `building` y candidatos `complete` aún no
  publicados para evitar carreras; el planner dry-run diagnostica candidatos,
  pero todavía no ejecuta expiración/conciliación de un build abandonado;
- semántica v7 preserva el staging y puntero/CAS de v6, y catálogo v7 publica
  por puntero/CAS para sus lectores oficiales (`NC-AUD-012` y `NC-AUD-013`);
  los receipts Semantic nuevos no atribuyen trabajo legacy y SQL externo sobre
  tablas mutables no hereda el contrato;
- el owner Code vigente es schema 7 y conserva las tablas legacy para lectores
  compatibles, mientras el ledger generacional aditivo (`graph_input_snapshots`,
  generaciones, batches, memberships, checkpoints y `graph_heads`) valida
  membresías y prepara una publicación progresiva; el productor principal aún
  usa la ruta legacy, por lo que cancelación, recovery, poda e integración CAS
  completa siguen pendientes (`NC-AUD-015`);
- la Knowledge Plane no implementa un grafo transversal entre owners;
  relaciones verificadas e historial transversal se reportan como capacidades
  incompletas en vez de inferirse. MCP es sólo una fachada read-only y no añade
  ese grafo;
- el golden Knowledge vigente ejecuta candidatos de owner scripted; comprueba
  contratos y fórmulas, no una evaluación humana ni calidad representativa del
  corpus;
- la calibración visual medida no separa positivos y negativos con un umbral
  escalar robusto, de modo que CLIP permanece fail-closed; MiniLM sigue como
  shadow hasta un A/B real etiquetado y una generación propia;
- el planner semántico valida tipo y longitud de payloads reutilizados, pero el
  writer `semantic_generation_repository.reuse_cached_jobs` aún no replica esa
  guarda; esa convergencia pertenece a Fase 2;
- el máximo configurable de scratch (16 TiB) es un límite de validación, no una
  promesa de que toda build de SQLite acepte ese `max_page_count`; el default
  operativo permanece en 512 MiB y el planner falla cerrado;
- los propietarios SQLite oficiales quedaron clasificados y sus familias
  verifican existencia/FK/query-only/timeout/rollback/cierre (`NC-AUD-017`);
  SQL externo puede evadirlas y no se comprobaron bases operativas vivas;
- rename y organización sólo operan con identidad ligada por handles dentro del
  subconjunto NTFS soportado; Papelera se abstiene y la conciliación de
  `file_actions` es idempotente y su observación puede persistirse append-only,
  pero decisión/autorización/recuperación no están implementadas y los planes
  de organización continúan en diagnóstico manual;
- `semantic_status` eliminó N+1 de conexiones y summaries, y conserva una sola
  conexión/snapshot; sus nueve conteos completos todavía pueden ser costosos
  (`NC-AUD-019`);
- el watcher tiene exclusión cross-process de por vida por raíz+estado y se
  abstiene ante owner vivo (`NC-AUD-020`); el archivo de diagnóstico persiste y
  no debe borrarse mientras un proceso pueda poseerlo;
- no hay comando general independiente de backup/restauración; `Neocortex
  databases purge` sí crea un backup online verificado como parte de una
  purga explícita de bases, mientras retención continúa ofreciendo sólo
  dry-run, sin delete/cuotas/compactación (`NC-AUD-014`);
- este corte no promovió el launcher estable; la validación del artefacto,
  dependencias, versión y ayuda sigue siendo una barrera posterior explícita;
- el proyecto no declara licencia propia ni NOTICE jurídico; el inventario
  técnico de terceros no sustituye la decisión del propietario (`NC-AUD-021`).

Los detalles, estados y procedimientos seguros pertenecen al informe técnico y
a [PERSISTENCE.md](PERSISTENCE.md), [RECOVERY.md](RECOVERY.md) y
[SECURITY.md](SECURITY.md). Una suite aprobada no convertiría automáticamente
estos riesgos de diseño en resueltos.
