# Arquitectura de NeoCortex

> Describe la arquitectura implementada en el checkout vigente y en la release
> post-0.13 activa. La release `current` proviene de
> `source_sha=c6d3985f7a45fc3120bd03e9561195674f2b8ac2`; el rollback inmediato
> conserva el artefacto 0.13 anterior. C0–C7 y la tranche post-0.13 están
> aceptados sólo sobre sus respectivos artefactos y receipts. Ninguna
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

### Lifecycle durable de `--all` (0.13 instalado y aceptado)

`--all` selecciona exactamente las nueve rutas registradas y las coordina bajo
un único run Framework: `pdf`, `docx`, `office`, `archive`, `text`, `audio`,
`video`, `image` y `code`. Code sigue siendo contenido observado: detecta,
extrae y publica relaciones, pero nunca ejecuta el código del corpus ni lo
convierte en una herramienta de validación del repositorio.

El artefacto base `0.13.0-1567fe46821b-cp314-linux-x86_64` contiene este
contrato y fue aceptado con C0–C7, suite integral, calidad estática, smoke,
replay y piloto aislado sobre el mismo `source_sha`; la release activa añade la
tranche post-0.13 descrita en las capas de Knowledge, diagnóstico y curación.

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

Cada ruta declara una capacidad de lifecycle: `phase_resume` conserva progreso
por fase, `safe_replay` reejecuta únicamente con entradas/publicaciones
durables e idempotencia, y `not_resumable` se rechaza explícitamente. La ruta
PDF conserva `phase_resume`; las demás sólo se reanudan cuando su manifest
declara `safe_replay`. Un adapter debe poder estimar su workload de forma
bounded y emitir checkpoints cooperativos, sin reservar de antemano todo un
snapshot que luego filtre candidatos.

Semantic y Code quedan ligados al mismo run, no como una operación posterior
sin identidad. `--all` coordina las rutas de contenido; el Semantic pesado
continúa siendo opt-in. Archive, Code y Video son fuentes Semantic explícitas,
por lo que no se infieren por el solo hecho de seleccionar `--all`; si una
fuente, modelo o herramienta falta, el stage conserva `unavailable` o `blocked`
y la corrida queda `incomplete`, sin éxito vacío ni skip silencioso.

### Progreso y cancelación

`neocortex.progress` define `ProgressEvent` y métricas estructuradas. Terminal,
GUI y grabadores consumen el mismo evento. En Linux los procesos externos usan
sesión/grupo propios; la cancelación alcanza el árbol y registra el estado final.

### Catálogo, Semantic y Knowledge

Catálogo y Semantic son proyecciones reconstruibles con heads publicados.
Knowledge crea un snapshot lógico sobre owners compatibles y fusiona rankings
sin convertir scores heterogéneos en una sola certeza. Puede entregar evidencia
y contexto citado, pero no genera autoridad de mutación.

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

## Interfaces públicas

- **CLI instalada:** `Neocortex`; el parser es la fuente exacta de argumentos.
- **API Python:** contratos tipados en `neocortex.api` y `neocortex.sdk`.
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

Las rutas genéricas `--apply` y `--organization-apply` continúan rechazándose
con `linux_mutation_backend_unavailable`. La ruta nueva `curate apply` sólo
consume un grant confirmado y un backend inyectado en una raíz contenida, por lo
que no habilita mutación implícita del corpus instalado.

La fuente ya contiene `neocortex.safety.kio_trash`: una foundation preparada que
descubre `kioclient6`, `kioclient5` o `kioclient`, valida configuración y snapshot,
ejecuta `move <origen> trash:/` mediante un runner inyectable y clasifica
`blocked`, `recovery_required` o `applied` sólo después de un verificador del
caller. Es reversible pero path-bound y está intencionalmente desconectada de
Linux `--apply`; no fue promovida ni probada contra KIO real en esta cohorte.

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
cooperativa; backup, restore y purge requieren exclusión más fuerte. Los
subprocesos tardíos no pueden publicar sobre un head nuevo. Un fallo alrededor
de la frontera de efecto produce un estado conciliable, no un reintento ciego.

La terminación de procesos aislados identifica el wrapper propio por PID y
starttime y limpia su grupo original aunque el líder ya haya terminado. Este
límite de PGID no contiene descendientes que creen otra sesión o grupo.

Antes de iniciar workers de contenido, `FrameworkState.route_candidate_snapshot()`
publica una copia temporal desde la conexión writer que ya posee el owner, con
lectura fijada, copia por páginas y comprobación acotada. `FrameworkRouteState`
usa esa vista inmutable sólo para candidatos; eventos, ReviewTasks, acciones y
lifecycle conservan el owner original. La copia vive hasta que terminan todos
los workers, incluso ante error o cancelación, sin abrir un lector ordinario
en el origen ni relajar los fences de `SQLiteReadSession`.

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
