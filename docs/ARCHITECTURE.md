# Arquitectura de NeoCortex

> Describe la arquitectura implementada en el checkout de esta oleada. El
> estado de `current`, el rollback, `HEAD == main == origin/main` y cualquier
> receipt se verifica por separado. Esta descripción no certifica una instalación
> ni que todos los resultados de un corpus sean correctos o útiles. Ninguna
> descripción aquí sustituye la evidencia de aceptación.

## Principios

NeoCortex es un framework local Linux-first para descubrir, identificar,
extraer, indexar, relacionar, revisar y buscar contenido. Sus invariantes son:

- los originales prevalecen sobre sus proyecciones;
- la identidad física no se reduce a una ruta;
- sólo una publicación completa puede ser vigente;
- evidencia, inferencia, decisión y autorización son clases distintas;
- una interrupción deja estado reanudable o `recovery_required`, nunca éxito;
- las interfaces comparten contratos y no reinterpretan texto de terminal;
- los recursos y lotes tienen límites observables.

## Topología

```text
Neocortex / python -m neocortex
             │
     interface.entrypoint
             │
     ┌───────┼────────┐
     │       │        │
    CLI     GUI      MCP/API
     └───────┼────────┘
             │
       runtime/orchestrator
             │
   inventory + content routes
             │
  owners SQLite y publicaciones
             │
 Catalog / Semantic / Knowledge / Review
```

La fuente vive en `~/Neocortex/Repository`; el estado, las releases, los modelos
y el launcher se resuelven mediante XDG. El corpus nunca debe contener los
árboles propios del producto.

## Capas

### Foundation y plataforma

`neocortex.foundation` define identidad y procedencia compartidas.
`neocortex.platform` contiene políticas Linux, tipos de contenido, manifests de
capacidades y utilidades de seguridad de contenedores. La enumeración portable
usa el filesystem; los módulos NTFS conservados son legado y no forman parte de
la ruta Linux normal.

### Inventario y deduplicación

La identidad física se valida con `FileIdentity` y el codec explícito del owner;
un recurso virtual conserva su `ResourceRef` y ancla, sin reinterpretarlo como
inode. Catálogo v8 añade bindings y ámbito mediante migración aditiva con copia
consistente previa, preservando claves e identidades históricas. Las lecturas
legacy ambiguas se abstienen de producir efectos y conservan el diagnóstico.

Catálogo v9 añade manifests de generación con source fence, raíz, política,
digest de entrada y digest de filas, además de triggers que impiden mutar una
generación publicada; el CAS compara identidad y digest del head.

Inventario v13 conserva evidencia por grupo/miembro y separa política solicitada,
verificación efectuada y cobertura del plan. La selección de keeper es explicable,
las preferencias explícitas prevalecen y mtime no representa versión documental;
aliases y bytes redundantes nominales no demuestran liberación física de espacio.

Los sucesores copy-on-write y los digests de contenido impiden reutilizar un plan
cuando cambia el contenido aunque `size` y `mtime` permanezcan iguales; los
`scan_id` anteriores quedan históricos y no vuelven a ser el head vigente.

`neocortex.deduplication` conserva snapshots, generaciones, fingerprints y
planes no destructivos. Reduce candidatos por tamaño y huella, pero la igualdad
destructiva exige comparación byte a byte. `mark_abandoned_scans()` concilia
scans `building` abandonados y el coordinador lo invoca antes de continuar; no
debe documentarse esa conciliación como ausente.

### Rutas de contenido

El registro de rutas compone PDF, DOCX, Office, Archive, Text, Audio, Video,
Image y Code. Cada ruta declara inputs, límites, progreso, owner y resultado.
Las implementaciones no tienen la misma riqueza: algunos formatos publican
localizadores estructurales y otros sólo texto o archivo completo. Esa brecha se
expone como cobertura, no se rellena con localizadores inventados.

### Lifecycle durable de `--all` (implementado; aceptación en curso)

`--all` selecciona exactamente las nueve rutas registradas y las coordina bajo
un único run Framework: `pdf`, `docx`, `office`, `archive`, `text`, `audio`,
`video`, `image` y `code`. Code sigue siendo contenido observado: detecta,
extrae y publica relaciones, pero nunca ejecuta el código del corpus ni lo
convierte en una herramienta de validación del repositorio.

Los artefactos 0.13 y post-0.13 anteriores conservan su evidencia histórica en
receipts separados; no se usan aquí para declarar aceptado o instalado el
checkout de esta oleada. La validación de este cambio debe repetir los gates
desde su SHA final.

El lifecycle ordena las fronteras `preflight → inventory → catalog/dedup →
routes → semantic → publication → finalize`. El manifest inmutable
`neocortex.run-manifest/v1` se publica antes de iniciar trabajo y liga la raíz,
identidad física, snapshot de entradas, configuración efectiva, rutas,
capacidad de replay y presupuesto. Cada transición de stage usa
`neocortex.lifecycle-stage/v1` y conserva el digest del manifest; un stage
interrumpido no se presenta como completado.

El ledger `neocortex.run-budget/v1` es global para toda la corrida, no sólo para
un worker: cubre inventario, catalogación/deduplicación, las nueve rutas, la
etapa Semantic y la publicación lógica. Las reservas por stage/ruta/unidad son
bounded e idempotentes, con items, bytes, deadline absoluto y cancelación
durable. El gate se consulta antes de admitir trabajo y antes de cada transición
terminal; el replay consume sólo el remanente del run origen y nunca abre una
ventana de presupuesto nueva.

### Scratch registrado (tranche A+B)

El control-plane `maintenance --scope owned-temp|audit-work` administra sólo
workspaces privados creados por NeoCortex bajo
`<state_directory>/scratch/<scope>`. Cada workspace tiene `manifest.json` con
digest, owner, run, identidad física y estado `active`, `committing`,
`completed`, `failed-retained` o `recovery_required`. La consulta no crea la
raíz ausente; `--apply` revalida el root, la identidad, los permisos y el
contenido antes de retirar únicamente un `completed` elegible. Vecinos sin
manifest, enlaces, drift o estados no terminales quedan intactos.

El servicio vive en `neocortex.runtime.scratch` y no sustituye los owners de
SQLite, KIO, staging de releases ni corpus. Semantic puede optar por este
servicio al recibir un `scratch_directory`; si no lo recibe conserva su
compatibilidad temporal aislada. El `--all` inicial registra la observación
bounded de `owned-temp` en el lifecycle y no escanea `/tmp`.

Cada ruta declara una capacidad de lifecycle: `phase_resume` conserva progreso
por fase, `safe_replay` reejecuta únicamente con entradas/publicaciones
durables e idempotencia, y `not_resumable` se rechaza explícitamente. La ruta
PDF conserva `phase_resume`; las demás sólo se reanudan cuando su manifest
declara `safe_replay`. Un adapter debe poder estimar su workload de forma
bounded y emitir checkpoints cooperativos, sin reservar de antemano todo un
snapshot que luego filtre candidatos.

Semantic y Code quedan ligados al mismo run, no como una operación posterior
sin identidad. `--all` coordina las nueve rutas y, en su selección integrada,
considera Archive, Code y Video junto con los demás owners Semantic cuando sus
fuentes, heads y dependencias están disponibles. Una selección explícita puede
acotar fuentes; no se introducen techos globales implícitos y los límites
expresados por el usuario siguen siendo acumulativos. Si una fuente, modelo o
herramienta falta, el stage conserva `unavailable` o `blocked` y la corrida
queda `partial`/`incomplete`, sin éxito vacío ni skip silencioso.

La integración de catálogo se ejecuta después de cada productor y serializa
únicamente la generación/CAS del owner compartido; la extracción permanece
paralela. Las observaciones `protected`, `no_speech`, `no_audio` y
`metadata_only` siguen consultables con cobertura parcial. FTS y derivados se
reparan desde representaciones durables válidas; un reintento exige evidencia
estructurada `retryable` y sólo se intenta una vez por archivo y corrida. Un
fallo o parcialidad queda en la fase `catalog` y en su evento tipado. Los nueve
summaries exponen contadores `catalog_*` y `catalog_complete`; `None` conserva
el estado no observado y no se interpreta como cero trabajo publicado.

Los títulos de Video son metadata para descubrimiento, no evidencia de un
fotograma. La lectura compatible de títulos legacy conserva esa separación sin
crear timestamps, modificar el cache ni repetir OCR o embeddings. Un localizador
malformado deja su ranking parcial, sin anular rankings independientes.

Las propuestas de organización son advisory y no requieren `--apply`; no
conceden permiso para mover, renombrar o borrar originales. La GUI proyecta la
misma selección, presupuesto, estados y stage Semantic que la CLI: el perfil
completo usa `--all`, el piloto mantiene límites acotados y una selección guardada
no se amplía por inferencia.

Una nueva ejecución `--all` no es una reanudación obligatoria del intento
anterior. Si queda un pendiente Semantic, valida su manifest/raíz y los heads
publicados de todos los modelos y Code, registra el intento anterior como
fallido y publica un checkpoint nuevo mediante una sustitución atómica que
preserva el prefijo del journal. Ese checkpoint no promueve generaciones
`building`, no declara éxito del intento anterior y no copia SQLite. El nuevo
trabajo usa la petición y los presupuestos actuales. Los enlaces Code obsoletos
por cambio de versión o avance del head Semantic se desactivan acotadamente
antes de capturar el baseline; la sincronización ordinaria reconstruye sus
derivados. La verificación junto al commit es sólo observación, nunca reparación.

`--resume-run` explícito conserva productor, manifest, selección y presupuesto
originales. Raíz ajena, schemas futuros, evidencia alterada o cambios concurrentes
siguen causando abstención en su frontera; no se sustituyen contratos de una
reanudación explícita por defaults ni se ocultan parciales.

### Progreso y cancelación

`neocortex.progress` define `ProgressEvent` y métricas estructuradas. Terminal,
GUI y grabadores consumen el mismo evento. En Linux los procesos externos usan
sesión/grupo propios; la cancelación alcanza el árbol y registra el estado final.

Durante el stage Semantic, Framework mantiene su heartbeat escritor. Su consulta
interna de cancelación participa en el lifecycle de ese owner: abre sólo la base
existente, verifica `query_only`, lee en una transacción SQLite y revierte/cierra.
Revalida la identidad física antes y después, limita espera por deadline y
conserva la cancelación externa sin recursión. No copia el owner por cada pulso
ni presume que `FrameworkRunLock` serialice el hilo de heartbeat.

Las lecturas públicas mantienen sus fences byte-neutral. En un snapshot temporal,
si un sidecar capturado desaparece antes de copiarlo, se descarta ese candidato y
se recaptura la fence completa dentro de los mismos intentos y presupuesto. No
se omite WAL ni se confunde esa carrera con un main ausente; tampoco se promete
obtener un snapshot de un owner que cambia continuamente durante la copia.

### Catálogo, Semantic y Knowledge

Catálogo y Semantic son proyecciones reconstruibles con heads publicados.
Knowledge crea un snapshot lógico sobre owners compatibles y fusiona rankings
sin convertir scores heterogéneos en una sola certeza. Puede entregar evidencia
y contexto citado, pero no genera autoridad de mutación.

Semantic conserva desde v8 el control de generación en el mismo owner SQLite.
Los cinco contadores de jobs de una generación `building` se actualizan por
triggers en la misma transacción que cada transición; los contadores terminales
siguen siendo snapshots históricos. La migración inicializa sólo los contadores
vivos, sin reinterpretar generaciones legacy publicadas con members y sin jobs.
Dos campos derivados por job acotan la revisión ordinaria: `source_dirty` recibe
las invalidaciones de item/chunk y `cached_payload_id` identifica posibles hits
de la caché de contenido. Sus índices permiten consultar cambios y hits, no
recorrer todos los pending antes de cada batch. Son pistas, no evidencia: la
reutilización y completion revalidan la fuente y conservan receipts/outbox,
leases, revisiones y causalidad. La finalización conserva la reconciliación
completa de stale y conteos después de limpiar el candidato, las verificaciones
de members/publicación de fuente y el CAS del head. Los repositorios de control
retienen el cálculo completo para una base v7 todavía no migrada; una lectura
no instala esta proyección ni reanuda una generación productiva.
Las invalidaciones por item parten de sus chunks antes de buscar jobs; los
índices de derivaciones por revisión/refresh y receipt de publicación evitan
recorrer toda la cohorte al publicar cada item durante el staging. No cambian
el conjunto publicado ni eliminan la validación de ordinals duplicados.
Knowledge, observación de heads, availability de búsqueda Code y preflight de
reuse leen v7/v8/v9/v10 sólo con el contrato canónico de la versión observada. Conservan
esa versión en avisos, planes, locators y digests; no la actualizan por lectura ni amplían los
writers. Salud de estado sigue exigiendo el schema vigente para declarar healthy.

El protocolo owner v9 mantiene la DDL v8 y sólo añade su marcador transaccional
de migración: no reescribe receipts ni eventos históricos. Los nuevos eventos
Semantic usan un envelope `semantic-derivation-event/v2` que referencia el receipt
canónico completo mediante owner, ID, key y SHA-256 de sus bytes UTF-8 exactos.
La fila, su FK y ambos registros append-only conservan la atomicidad anterior.
El reader valida forma, digest y hechos normalizados en el mismo snapshot, y
entrega el mismo DTO lógico v1 hidratado para lineage/proyección; una referencia
sin su receipt no es un replay autocontenido. El presupuesto de página sigue
contando el envelope lógico completo y el receipt, no sólo el wire reducido.
Los eventos v1 existentes permanecen legibles y bytewise intactos; los writers
compatibles que aún operen sobre v7/v8 siguen emitiendo v1. Un binario anterior
rechaza el owner v9 por versión en lugar de interpretar el wire nuevo como v8.
No hay compactación, GC, migración de estado productivo ni instalación implícita.

El schema v10 cambia sólo el layout físico de `text_chunks` a rowid, con
`chunk_id TEXT PRIMARY KEY NOT NULL` explícito y los mismos dos índices de
consulta. El layout anterior almacenaba filas amplias en un B-tree de claves;
rowid evita su umbral de overflow sin cambiar chunks, compresión ni vectores.
La migración copia y compara todas las columnas y clases de almacenamiento
antes de sustituir la tabla, preserva la FK de `text_embeddings` y recrea los
seis triggers canónicos de invalidación de fuente. Una copia equivalente de
`text_embeddings` apunta al nuevo parent antes de retirar el par anterior;
preserva ref_ids y el high-water del allocator. Las referencias se actualizan
al renombrar las copias ya verificadas. FK permanece habilitada e inmediata
durante toda la transacción, sin activar deferral ni resetear su tracking.
Se ejecutan también `foreign_key_check` e `integrity_check` completos. Cualquier
error revierte datos, DDL y marcadores juntos. No se ejecuta
VACUUM ni se promete reducir el archivo existente: las páginas liberadas quedan
disponibles para reutilización. Receipts históricos y protocolo wire v2 se
conservan; leer un owner antiguo no dispara esta migración.

La búsqueda vectorial exacta conserva el scan exhaustivo y acotado de miembros
publicados. Para un único par modelo/generación, el miembro dirige los joins y
el prefijo fijo del índice existente permite emitir `member_id` en orden sin
ordenar filas anchas con vectores y provenance. No se fuerza un índice por
nombre ni se repara un owner durante la lectura. La selección de varios pares,
incluidas firmas repetidas, conserva su consulta y multiplicidad anteriores.
La optimización no cambia scoring, precisión, desempates, filtros ni resolución:
todos los vectores y JSON de los candidatos efectivamente escaneados se validan,
incluso si no entran al top-K; la fila adicional que detecta un corte por
`max_vectors` no se puntúa. Cada página mantiene su top-K local, cursor y
cobertura, no una promesa de top-K global al concatenar páginas. La hidratación
de evidencia mantiene sus revisiones, localizadores y fences. El costo continúa
siendo exhaustivo: eliminar el sort temporal no garantiza una latencia constante
ni un p95 objetivo, y no incorpora ANN ni nuevos modelos.

El índice exacto derivado es una opción explícita, apagada por defecto. Su
formato separado conserva los bytes f16/f32, códigos de agrupación y normas
float64 calculadas con la misma aritmética del scan nativo. La preparación
desde un único head textual crea un directorio nuevo; no cambia el owner ni
se ejecuta con `--all`. Los hashes y el marker de finalización no conceden
autoridad: `open_exact_index` coteja todas las filas con la relación nativa
publicada, además de modelos, scope, runtime matemático y fences. Preparar y
abrir son operaciones frías O(ND), fuera de un contexto de lectura prestado.

Las consultas aceptan un `ExactIndexHandle` ya verificado, nunca una ruta para
reconstruir implícitamente. El handle serializa su uso y requiere cierre
explícito/context manager. La ruta elegible puntúa exhaustivamente en lotes
de hasta512 vectores, mantiene arrays numéricos O(U) por grupos y sólo hidrata
losK ganadores; no es ANN. Cada página conserva scores, desempates y cursor.
Un índice obsoleto o una combinación no soportada vuelve al scan nativo antes
del scoring y con los mismos límites; un cambio detectado durante la consulta
produce abstención sin retry oculto. No se prometen tiempos constantes ni una
latencia CLI equivalente a la consulta cálida: la CLI abre y verifica el
artefacto por invocación. La ausencia del argumento conserva la ruta anterior
sin descubrir, abrir ni construir cachés.

La planificación organizativa selecciona una raíz de entrada con identidad y
heads publicados antes de calcular destinos. Clasificación, elegibilidad,
operación y ejecutabilidad son dimensiones distintas; los miembros/componentes
virtuales no reciben movimientos físicos ni un flag SQLite concede autoridad.
Los planes legacy sin ámbito probado permanecen advisory y no ejecutables.

Knowledge v2 proyecta fuentes únicas y citas con localizadores, manteniendo
recuperación, relaciones, evidencia y presentación como coberturas separadas.
Cada cita distingue referencia verificada, relación recuperada y suficiencia
de respuesta: `answer_sufficiency=not_assessed` deja esta última al LLM.
Ni `owner_verified`, ni `evidence_candidate`, ni cobertura completa afirman
que el fragmento responda la pregunta; las comprobaciones heurísticas son
orientativas y conservan sus límites junto al pasaje original.
El lookup por referencia valida owner, revisión y publicación sin repetir la
búsqueda; un rango o propietario no soportado se declara explícitamente. Salud
del pipeline, condición del archivo, contenido documentado y preferencia
organizativa no son equivalentes. El diagnóstico consulta owners especializados,
sin copiar sus datos a otro almacén monolítico.

El envelope `neocortex.context-response/v2` proyecta, de forma bounded, entidades,
relaciones, contradicciones, grafo y telemetría. `SharedReadClient`, GUI y
conveniencias SDK solicitan v2; la fachada Python v1 permanece disponible sólo
por compatibilidad explícita. `KnowledgeReadBudget` limita filas, vectores,
temporales, deadline y cancelación sin escribir estado ni introducir caches sin
invalidación por heads/fences.

`neocortex.content-diagnostics/v2` federa los nueve owners de contenido mediante
cursores ligados a raíz, filtros y snapshots, y conserva estados de ausencia,
parcialidad, schema futuro, corrupción y bloqueo sin confundirlos con cero
incidencias. La versión v1 sigue intacta.

Archive distingue ZIP físico, documento lógico y componentes, incluido OTT
exterior/anidado; MIME declarado, estructura e integridad pendiente se conservan
separados. PDF informa el resultado publicado sin sumar como omisiones actuales
las páginas fallidas de intentos históricos. Una imagen candidata a documento
es una observación, no una decisión humana ni un candidato automático a borrar.

La búsqueda visual mantiene un contrato adicional de calibración local: el piso
de similitud se mide sobre consultas positivas y negativas, se liga al modelo,
pipeline y `processing_signature` del head de imágenes publicado y se guarda en
el owner Semantic. Si el contrato deriva o no existe, la búsqueda visual falla
cerrada con una abstención explicable en vez de presentar vecinos no calibrados.

### Review y curación

Framework conserva batches, tareas, decisiones y eventos. **CURRENT:**
`curate plan` y `--curation-preview` componen propuestas existentes sin crear
estado ni tocar archivos.

**IMPLEMENTED:** `neocortex.curation.lifecycle` enlaza un `plan_digest` completo
con el owner ReviewTask existente. `curate review` publica una página como tareas
advisory, conserva el item y snapshot, pagina con cursor, usa una source fence y
reproduce el mismo batch de forma idempotente. `curate decide` vuelve a comprobar
digest y snapshot y añade por CAS un evento humano `resolved` o `dismissed` con
scope y actor. Ninguna de las dos operaciones crea `file_actions`, invoca KIO,
autoriza efectos o toca corpus/sistemas externos.

`neocortex.curation.authorization` implementa `curate authorize`. Exige
un plan completo, ReviewTasks `resolved`, digest/snapshot/fence vigentes y un
efecto permitido; persiste un `AuthorizationGrant` inmutable y acotado en la
extensión opcional `curation_authorization_grants` del owner Framework. La
extensión se crea sólo por la operación explícita, no añade otro owner y bloquea
UPDATE/DELETE. El grant liga actor, acción, backend Linux, items/tareas,
`max_actions`, `max_bytes`, emisión y expiración.

Emitirlo escribe sólo estado: no crea `file_actions`, no invoca KIO y mantiene
`physical_effect_applied=false`. **IMPLEMENTED sobre fixtures y backends
inyectados:** `neocortex.curation.application` consume el manifest físico del
grant, revalida plan, heads, expiración, raíz, identidad, hash y presupuestos,
registra un `file_action` por efecto, verifica el receipt y conserva
`recovery_required` ante ambigüedad. `PosixRenameBackend` usa no-replace
same-filesystem y `KioTrashBackend` exige evidencia estructurada de destino.
`neocortex.curation.recovery` añade preview read-only y restore con un intento
`restore_curation`, confirmación exacta, `renameat2(RENAME_NOREPLACE)`, hash,
identidad de raíz/Trash y receipt durable; una caída posterior al movimiento se
concilia sin reintento. La CLI no selecciona backend automáticamente; la
promoción KIO real y restore de escritorio siguen siendo gates posteriores. La
conciliación append-only se expone mediante `reconcile_curation_actions` y no
reintenta efectos. El contrato se describe en
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).

La vista `neocortex.curation.read` permite consultar grants, intentos, receipts y
recovery sin abrir el corpus ni crear estado; la GUI sólo presenta y copia esa
información. El contrato `neocortex.authenticated-principal/v1` rechaza actores
textuales y principals no atestados, pero no habilita todavía autorización MCP.
La sincronización de caches para move/rename usa lock ordering explícito sólo en
fixtures; `trash` conserva una política de invalidación separada.

### Code como contenido

Code detecta proyectos y lenguajes, extrae símbolos/relaciones, conserva
versiones y permite búsqueda/reconstrucción. No ejecuta el código observado ni
importa herramientas de desarrollo. Pytest, Ruff, Mypy/Pyright y Semgrep se
ejecutan fuera del runtime.

## Persistencia

`STATE_STORE_REGISTRY` es el inventario contractual de owners:

```text
inventory, framework, catalog, pdf, docx, office, audio,
video, image, semantic, code, archive, text
```

Cada owner controla su schema y migraciones. Los lectores eligen una estrategia
de `SQLiteReadSession` compatible con la actividad del owner; abrir una base
viva con `mode=ro` ordinario no es una observación garantizada porque SQLite
puede tocar sidecars.

Las publicaciones owner-local usan staging y cambio atómico de head. Las vistas
multi-owner se validan contra el protocolo de publicación cross-owner y se
abstienen cuando una transición relevante queda pendiente o inconsistente. No
se promete una transacción física distribuida entre archivos SQLite.

El lifecycle agrega al owner Framework los manifests, stages, reservas, eventos
y checkpoints acotados, con digest de manifest, raíz/identidad, snapshot,
configuración efectiva, owner heads y último límite durable. El inicio del run,
la publicación del manifest y la creación de stages deben ser atómicos e
idempotentes, para no dejar filas `running` huérfanas. El orquestador reutiliza
`GlobalResourceCoordinator`; no existe un segundo coordinador para el
presupuesto de `--all`.

La publicación Semantic/Code usa staging y CAS lógico: sólo avanza el epoch
cuando todos los heads requeridos por el manifest están completos y no hay
drift. Parcialidad, owner-head drift o una preparación ambigua producen
`blocked`/`recovery_required`; no se simula una transacción SQLite distribuida.
Una consulta de estado observa estos datos mediante el owner/publication
canónicos y no abre una SQLite cercada durante writers.

El producto sí expone backup y restore generales mediante `Neocortex databases`.
Persistencia define el contrato; el procedimiento está en
[RECOVERY.md](RECOVERY.md).

### Reset seleccionable de estado

`Neocortex state reset` es una frontera de mantenimiento separada de las rutas
de contenido y de `databases purge`. Su contrato `neocortex.state-reset/v1`
construye un plan inmutable y exige uno de tres scopes:

- `runs`: limpia el ledger de ejecución de Framework y lifecycle expresamente
  asociado, sin borrar owners de contenido ni datos de Review/recovery/curación
  que no estén ligados por un contrato verificable;
- `runs-and-caches`: extiende el alcance a todos los owners SQLite registrados,
  sus sidecars y la metadata de publicación administrada, retirando la frontera
  cross-owner como conjunto lógico;
- `all`: agrega los artefactos no-SQLite administrados por el estado. No adopta
  archivos desconocidos, corpus, releases, modelos ni backups externos como
  targets.

El plan incluye scope, raíz, targets, referencias cruzadas, fingerprints,
conteos/bytes, epoch y conflictos de locks/fences. El digest cubre esos datos y
los límites efectivos, por lo que modificar scope, raíz, estado observado o
límite entre preview y apply invalida la operación. El preview no crea estado ni
backup. Apply adquiere exclusión fuerte, vuelve a comprobar writers, publicación,
schemas y heads, y usa staging/rollback efímero; sólo `--backup-directory`
explícito crea una copia durable externa. Las reservas de bytes/archivos son
bounded; no existe un fallback que quite el límite para terminar.

La implementación no simula una transacción física distribuida. Cada owner se
respalda/retira con su contrato y el journal de reset registra baseline,
postcondición y manifest. `runs` conserva continuidad de identificadores y
procedencia mediante high-water mark/tombstone o un allocator equivalente; las
referencias que no puedan conciliarse producen abstención. Si falla la
preparación, el backup o la reversión, el estado queda `recovery_required` con
staging/backup conservados. Un resultado `complete` sólo significa que el
alcance local fue verificado; no inicia una corrida ni afirma efectos sobre el
corpus, releases o modelos.

## Interfaces públicas

- **CLI instalada:** `Neocortex`; el parser es la fuente exacta de argumentos.
- **API/SDK Python:** `state_reset_payload` ofrece el mismo preview/apply
  explícito y envelope bounded; exige raíz, scope, digest y confirmación cuando
  aplica, sin seleccionar el estado productivo por omisión.
- **GUI:** presentación PySide6 que delega trabajo a workers; no redefine reglas.
- **MCP:** servidor stdio local con consultas read-only y las escrituras de
  estado advisory `curation_review`/`curation_decide`; estas últimas declaran
  `readOnlyHint=false`, `destructiveHint=false` y no conceden autoridad. No
  expone `authorize`, `apply`, `restore` ni conciliación escrita; el principal
  autenticado sólo está definido como contrato de preparación.

Las cuatro superficies deben conservar operación, scope, cobertura, epoch,
errores y evidencia equivalentes. La salida estructurada es contrato; el texto
humano no debe convertirse de nuevo en datos mediante parsing.

El envelope read-only `neocortex.lifecycle-envelope/v1` es común para
`read_run_status`, `lifecycle_status`, API, SDK y MCP. Expone de forma bounded
manifest/digest, status, stages, rutas, presupuesto, checkpoints, capacidad de
replay, recuperación y owner heads. Sus consultas no inician runs, no reservan
trabajo y no conceden autorización; MCP no añade herramientas de ejecución,
aplicación ni mutación. Los contratos v1 y manifests/checkpoints históricos
siguen siendo legibles, y las extensiones 0.13 son aditivas.

La tranche 0.12 incorpora `CurationWorkBudget` como límite opcional de la
verificación exacta, con contabilidad de items, archivos y bytes, deadline
monotónico y cancelación cooperativa. Un corte por presupuesto conserva los
resultados ya observados y materializa el resto como `not_verified`. El contrato
`neocortex.curation-checkpoint/v2` publica manifests bounded con root/source/plan
digests, cursor, batch digest, presupuesto acumulado y tamaño de página. La
API/SDK admite sólo el trabajo que cabe en el presupuesto restante y revalida
el snapshot incluso en replay terminal. El fin del recorrido y la cobertura de
las fuentes son independientes: una página final no completa evidencia parcial.
Los manifests `neocortex.curation-checkpoint/v1` permanecen legibles sin
reescribir sus bytes; los sucesores incorporan el contrato vigente. V1 no
demuestra cobertura ni conserva el tamaño de página: una continuación parcial
sin cursor se rechaza por ambigüedad, en vez de reiniciar trabajo por inferencia.

El inventario Linux añade `neocortex.inventory-resume/v1`: el productor
checkpointado recorre cada directorio en orden determinista por bytes, conserva
un cursor DFS, identidad de la raíz y digest de los directorios abiertos, además
de los digests del prefijo y del último lote. El owner se escribe fuera del
corpus con bytes canónicos, permisos `0600`, lock y avance monotónico; la
reanudación elimina sólo el tail posterior al cursor, revalida el prefijo y
rechaza drift de identidad, política, ancestros o lote antes de publicar. Un
replay terminal valida el inventario vigente y devuelve el mismo `scan_id`; no
expone mutación ni se anuncia como herramienta MCP.

El checkpoint terminal incorpora también directorios observados después del
último lote, y cancelación/deadline se consultan aunque no haya entradas. Los
nombres POSIX no representables en SQLite TEXT se aíslan con
`unsupported_path_encoding`: se conservan las observaciones independientes y
el scan queda parcial, sin renombrar el corpus ni sustituir bytes de la ruta.
Se revalidan cambios en directorios activos, sin prometer un snapshot atómico
global del filesystem.

No existe una superficie de exportación o ZIP para el lifecycle de curación;
Archive/ZIP sigue siendo únicamente una ruta de contenido.

## Efectos sobre archivos

`--organization-apply` y las operaciones directas de cada formato conservan
sus rechazos read-only. En Linux, `--all --apply` y `--dedupe --apply` sí cruzan
la frontera explícita de archivos regulares mediante KIO receipt-bound; no hay
mutación implícita en las corridas sin `--apply`. `curate apply` sigue siendo
grant-bound y requiere un backend inyectado en una raíz contenida.

La fuente ya contiene `neocortex.safety.kio_trash`: una foundation preparada que
descubre `kioclient6`, `kioclient5` o `kioclient`, valida configuración y snapshot,
ejecuta `move <origen> trash:/` mediante un runner inyectable y clasifica
`blocked`, `recovery_required` o `applied` sólo después de un verificador del
caller. La frontera integrada agrupa hasta 256 archivos por invocación no
interactiva, conserva claim/receipt por elemento y publica reconciliación por
lote. Es reversible pero path-bound; la canaria instalada se ejecutó sólo en
fixtures privados y la restauración visual de Dolphin conserva su gate humano.

La integración actual hace que `apply` lea y revalide el grant, además de
identidades, guard same-filesystem, ledger y expiración, y que `reconcile`
registre la observación sin reintentar. Un timeout o resultado ambiguo permanece
`recovery_required`; KIO real y la sincronización posterior de caches no se
consideran promovidos.

## Concurrencia y recuperación

Los probes de control Linux incorporan cgroups v2 y afinidad a los datos del
host. La memoria distingue límite duro y margen antes de presión por
`memory.high`, y CPU conserva cuota fraccional para el diagnóstico y un número
conservador de workers para los consumidores existentes. No introduce una
política fija de recursos ni un segundo coordinador.

Las superficies de ayuda y los contratos de configuración no importan motores
ni Qt. El diagnóstico de paquetes usa requisitos de `pyproject.toml` en fuente
o `Requires-Dist` del paquete instalado, sin una lista paralela de versiones;
inspección de metadata, localización de ejecutable, archivos de modelo y éxito
de procesamiento no son equivalentes.

El coordinador limita CPU/memoria y registra fases. Writers toman exclusión
cooperativa; backup, restore, purge y state reset requieren exclusión más fuerte. Los
subprocesos tardíos no pueden publicar sobre un head nuevo. Un fallo alrededor
de la frontera de efecto produce un estado conciliable, no un reintento ciego.

La terminación de procesos aislados identifica el wrapper propio por PID y
starttime y limpia su grupo original aunque el líder ya haya terminado. Este
límite de PGID no contiene descendientes que creen otra sesión o grupo.

Antes de iniciar workers de contenido, `FrameworkState.route_candidate_snapshot()`
publica una proyección temporal bounded desde la conexión writer que ya posee el
owner, con lectura fijada, copia por páginas y comprobación acotada. Conserva
únicamente la generación de candidatos, las recomendaciones abiertas ligadas a
ella y la evidencia mínima de fases de replay; el backup completo queda para
llamadas legacy sin una generación explícita. `FrameworkRouteState` usa esa
vista inmutable sólo para candidatos; eventos, ReviewTasks, acciones y lifecycle
conservan el owner original. La copia vive hasta que terminan todos los
workers, incluso ante error o cancelación, sin abrir un lector ordinario en el
origen ni relajar los fences de `SQLiteReadSession`. Code recibe además una
proyección efímera de sus filas admisibles producida por el owner de inventario,
evitando reabrir un WAL grande desde el worker.

En `--all`, la misma frontera se conserva entre workers de ruta y stages
posteriores. El progreso, transcript y estado público distinguen `complete`,
`partial`, `unavailable`, `blocked`, `cancelled` y `recovery_required`; una
ruta no disponible no se convierte en cobertura completa por terminar las
demás. La reanudación valida root, política, snapshot, modelo, herramienta,
manifest y owner heads antes de publicar, y se abstiene fail-closed ante drift.

## Brechas vigentes

- varias fuentes todavía tienen publicación no generacional;
- la cobertura de localizadores sigue variando cuando un productor no publica la
  estructura requerida, y esos casos se mantienen como reference-only;
- falta resolver y conectar un principal autenticado con una sesión MCP confiable;
- la ruta física, restore de escritorio y KIO real siguen habilitados sólo mediante
  backends inyectados y fixtures;
- la sincronización de caches para `trash` y la promoción de efectos reales siguen
  fuera de esta cohorte;

La prioridad y los criterios de aceptación están en
[ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md); seguridad y owners se detallan en
[SECURITY.md](SECURITY.md) y [PERSISTENCE.md](PERSISTENCE.md).
