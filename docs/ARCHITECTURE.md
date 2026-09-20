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
     ┌───────┼───────┐
     │       │       │
    CLI    MCP/API  SDK
     └───────┼───────┘
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

El grafo productivo no admite ciclos de imports inmediatos. Los ciclos que
incluyen imports diferidos o de tipado se fijan con contratos exactos de módulos,
dirección y función en `tests/architecture/test_boundaries.py`. Archive comparte
primitivas y registro durable; factory reset delega la enumeración y retirada
acotada al owner de persistencia; HistoricalManager
delega adopción. Sus auxiliares reutilizan contratos del mismo owner sin volver a
entrar en la operación que los llamó. Estas cuatro delegaciones tienen 30 aristas
concretas revisadas; cualquier arista nueva exige revisar de nuevo el contrato.

El traslado de `TextSourceRecord` a `semantic_models` eliminó la dependencia
circular entre el agregador `semantic_sources` y el adaptador `video_source`,
manteniendo el reexport y la identidad pública histórica del DTO. El contrato
conceptual conserva las cinco relaciones diferidas previas y las cuatro
delegaciones descritas arriba: nueve componentes en total, sin ciclos inmediatos.
El analizador distingue imports de módulo, función y `TYPE_CHECKING`; diferir una
arista no equivale a eliminarla del grafo conceptual. No se extraen módulos por
tamaño ni se introducen capas sin consumidores.

### Foundation y plataforma

`neocortex.foundation` define identidad y procedencia compartidas.
`neocortex.platform` contiene políticas Linux, tipos de contenido, manifests de
capacidades y utilidades de seguridad de contenedores. La enumeración portable
observa el filesystem directamente y no depende de un journal de plataforma.

### Inventario federado read-only de máquina

`neocortex.runtime.machine_inventory` es el owner del inventario federado del
host. Su consulta acepta raíces absolutas repetibles o, si no se proporcionan,
selecciona perfiles bounded de `HOME`, `/tmp`, cache/configuración XDG y las
raíces de estado, datos y corpus de NeoCortex; nunca escanea `/` por defecto.
Cada raíz conserva su clasificación y procedencia: `neocortex_state`,
`neocortex_data` y `neocortex_corpus` se mantienen separados de `tmp`, `cache`,
`config`, `home` y `external`. El perfil es una observación de ownership, no
una autorización.

El recorrido es metadata-only mediante `lstat`/no-follow. Conserva identidad
física (`st_dev`, `st_ino`, birthtime o sentinel), tipo, owner, permisos,
montaje, symlink/hardlink y tamaños aparente/asignado sin leer cuerpos ni
payload del corpus. No abre SQLite, ni siquiera con `mode=ro`, no escribe
estado, no usa red, KIO, sudo o cleaners y no crea raíces ausentes. Esto lo
separa del inventario de contenido: no publica owners SQLite ni inicia el
lifecycle de rutas. Los cambios concurrentes del filesystem quedan como
`blocked`/`unknown`; la observación no promete un snapshot atómico.

El envelope `neocortex.machine-inventory/v1` es cerrado y declara
`read_only=true`, operación, raíces, `root_count`, límites globales de
`entries`/`depth`/`bytes`, `root_quota_policy`, `root_entry_quotas`,
`truncated`, registros bounded, conteos `record_status_counts`,
`record_category_counts`, `record_reason_counts` y `root_status_counts`, además
de los históricos por estado/categoría, `reason_summary`,
categorías/owners/procedencia y bytes
`observed`, aparentes y asignados. Sus estados son `absent`, `observed`,
`preserved`, `blocked`, `unknown` y `out_of_profile`. La ausencia de un root,
la truncación o un tamaño observado no se colapsan a éxito ni a permiso para
retirar archivos.

La interfaz separa el resumen de la lista de registros. El modo JSON
predeterminado serializa un resumen compacto por raíz y los agregados, sin
volcar la colección completa; `--machine-json=records` es un opt-in para
registros bounded (`compact` es el modo predeterminado) y conserva los mismos
presupuestos del escáner. Los resúmenes de todas las raíces seleccionadas se
preservan aunque el presupuesto global detenga el recorrido antes de
visitarlas, mientras quepan en el límite de presentación. Si esa lista también
se recorta, el resultado conserva `root_count` e informa
`root_summaries_omitted` y `presentation_truncated`; `root_count` no es un
alias de la cantidad de raíces con registros.

La cobertura tiene dos capas independientes. `scanner_truncated` (compatible
con `truncated`) y `truncation_reasons` describen el resultado del owner DFS y
sus límites de entradas, profundidad, bytes o seguridad; `presentation_truncated`
y `serialization` describen la proyección bounded que sale por la CLI. La
proyección informa `mode`, `records_included`, `records_returned` y
`records_omitted`; la omisión intencional del modo `compact` puede tener un
conteo positivo sin ser un truncamiento. Nunca debe sustituirse por el marcador
genérico de sanitización `[contenido omitido por límite]`. Así, la observación
puede ser parcial con un resumen íntegro, o completa con el detalle recortado.
El owner Python expone esta proyección mediante `to_summary_dict()`, con
`root_summaries`, `coverage_metadata` y `omissions`; `to_dict()` conserva la
representación completa para consumidores que la soliciten expresamente.

El reparto de entradas es global y fair-share, no una serie de escaneos
independientes. `root_quota_policy=equal_fair_share_v1` publica en
`root_entry_quotas` la cuota efectiva por `root_index`, y cada resumen de raíz
expone `entry_quota`. La implementación calcula para cada raíz
`ceil(remaining_global_entries / remaining_roots)`; al terminar una raíz, su
crédito no usado vuelve al fondo para las siguientes. `entry_quota=0` conserva
el resumen de la raíz como evidencia de que el fondo global se agotó, no como
evidencia de ausencia. La política limita sólo las entradas; depth y bytes
conservan sus vallas globales. Por diseño, la cuota es una asignación de
observación y no demuestra cobertura completa.

Los agregados mantienen una frontera explícita. `records_scanned` y los mapas
`record_status_counts`, `record_category_counts` y `record_reason_counts`
describen únicamente registros de entradas. `root_status_counts` cuenta el
estado de cada raíz una vez. Los campos históricos `status_counts`,
`category_counts` y `reason_counts` pueden incorporar marcadores de raíz
ausente/bloqueada/desconocida para no perder evidencia cuando una raíz no
produce registros; son compatibles, pero no deben sumarse con `record_*`.

La contabilidad de bytes también es explícita: `apparent` suma `st_size`,
`allocated` suma `st_blocks * 512` y `observed` es `apparent + allocated`, la
métrica de crédito para el presupuesto global. Son metadatos, no una lectura
de payload ni una medida de espacio libre/recuperable; hardlinks, symlinks,
directorios y objetos especiales mantienen sus fronteras de identidad y no se
convierten en bytes exclusivos por sumar esos campos.

El comando de consulta rechaza `--apply` y los selectores de corpus (`--root`,
`--all`, `--dedupe` y rutas). El siguiente plano de acción permanece separado:
requiere registro de owner y política, selección y preview explícitos,
autorización humana, revalidación fresca de identidad/topología/actividad y un
backend reversible con receipt, postcondición y recuperación. No se habilitan
acciones de sistema o privilegiadas desde esta capa.

### Preparación federada de `hygiene`

`hygiene` es una capa de composición para preparar una vista bounded de higiene;
no es un owner universal ni una segunda base de datos. En la cohorte actual su
arquitectura es **read-only/preview-only**:

```text
registry de fuentes/owners/categorías
              │
  manifests + claims de procedencia e identidad
       ┌──────┼─────────┬──────────────┐
    scratch  retención  machine-      external
    registrado por owner inventory     diagnostic
       └──────┴─────────┴──────────────┘
              │
       preview bounded, zero deletion
```

#### Presente: preparación y ownership

El registry de higiene es un catálogo versionado de adaptadores y contratos,
no una autorización. Cada registro identifica fuente, owner lógico, raíz o
ámbito, categoría, procedencia, capacidad, límites y versión de schema. El
manifest de una petición liga esa selección con identidad física, snapshot o
owner-head, digest, cobertura, bytes, retención declarada, estado y razones.
Ambos son evidencia para comparar y revalidar; no son instrucciones de workers,
no sustituyen los manifests de los productores y no convierten texto del
filesystem en configuración.

El registry de artefactos de la fuente usa
`neocortex.artifact-registry/v1`. Sus claims bounded ligan `artifact_id`,
owner/producer, propósito, `run_id`, root/path, identidad de raíz y artefacto,
`kind`, estado, `source_ref`, digest, dependencias, retención, `disposable`,
metadata y `manifest_digest`. Sólo un manifest íntegro y una revalidación
no-follow de root, permisos, tipo, montaje e identidad pueden clasificar una
entrada; `canonical`, `operational`, `rebuildable`, `temporary`, `cache` y
`external` son roles de lifecycle, no instrucciones de disposición. La
registración o actualización del registry es una operación del owner aparte:
la preparación `hygiene` consume sus planes y no escribe esos manifests.

La autoridad permanece disjunta:

- `neocortex.runtime.scratch` conserva la propiedad de los workspaces registrados
  y sus manifests `neocortex.scratch/v1`; un nombre bajo `/tmp` no crea esa
  relación;
- cada owner mantiene su propio cálculo de retención, reachability, referencias,
  heads y estados de recuperación; `hygiene` consume sólo su proyección
  read-only y no ejecuta SQL de limpieza;
- `neocortex.runtime.machine_inventory` conserva la observación de máquina y
  su envelope `neocortex.machine-inventory/v1`, sin abrir SQLite ni leer
  payloads;
- `neocortex.runtime.external_maintenance` conserva el diagnóstico de una raíz
  y categoría externas explícitas mediante `neocortex.external-maintenance/v1`;
  la categoría externa no prueba ownership de NeoCortex.

El agregado no eleva el nivel de confianza de una fuente. Si falta un owner o
manifest, hay schema futuro, carrera, drift o límite agotado, conserva la
incertidumbre y el registro queda `blocked`, `unknown`, `out_of_profile` o
parcial según la evidencia. Una respuesta compacta no implica cobertura total.

Las categorías de clasificación de higiene son ortogonales a estados y razones:

| Categoría | Regla arquitectónica |
|---|---|
| `canonical` | Fuente de verdad, evidencia o estado no sustituible; el owner y la procedencia deben preservarse. |
| `operational` | Estado necesario para operar, incluidos locks, ledgers, heads y metadatos vivos; no se retira por parecer derivado. |
| `rebuildable` | Derivado potencialmente reconstruible desde inputs y receta versionada; la reconstruibilidad no prueba recuperabilidad ni autoriza retiro. |
| `temporary` | Workspace bounded, privado, registrado y terminal según su manifest; no es sinónimo de `/tmp`. |
| `cache` | Caché de aplicación, modelo o índice con política propia de invalidez, costo y retención; no tiene cleaner genérico. |

Esta clasificación no limita lo conservable a código y documentación. Corpus,
fotografías, correo, configuración, modelos, backups, sesiones, releases y
otros datos personales pueden ser canónicos u operativos. La ausencia de una
categoría segura, una procedencia externa o una referencia incompleta conduce a
preservación/abstención, no a una selección por nombre, tamaño o antigüedad.

La preparación mantiene una sola frontera de observación. No escribe owners ni
heads, no crea `file_actions`, no mueve, renombra o retira archivos, no ejecuta
`DELETE`, `VACUUM`, compactación o cleaners y no promete bytes recuperables.
Este **zero deletion** es una propiedad del plano actual, no una conclusión de
que los candidatos sean desechables.

Los presupuestos son explícitos y acumulados por petición: raíces, entradas,
profundidad, bytes, tamaño de manifests/registros, tiempo y cancelación cuando
la fuente los soporte. El agregador no relaja una valla para completar una raíz;
publica cobertura y remanente. Conserva las fences existentes de no-follow,
identidad física, montaje, permisos y no apertura de SQLite cercada (tampoco en
`mode=ro`), además de la prohibición de red, KIO, sudo y cleaners. Un manifest
no es un snapshot atómico del filesystem.

#### Target: efectos sólo mediante gates explícitos

La arquitectura reserva una secuencia, actualmente fuera de `hygiene`, para
cualquier capacidad que llegue a actuar:

```text
preview → review → authorize → apply → verify → recovery
```

`preview` publica el registry/manifest y su cobertura; `review` registra la
decisión humana sin autorizar; `authorize` emite un grant acotado a owner,
selección, política, actor, expiración y presupuesto; `apply` requeriría un
backend reversible y locks; `verify` demostraría la postcondición; y
`recovery` conservaría receipts y resolvería cualquier timeout, drift o efecto
ambiguo. Cada transición debe revalidar junto al efecto raíz, identidad física,
montaje, permisos, enlaces, actividad, manifest/digest, owner-head,
categoría/política y límites. El preview nunca se convierte por sí mismo en
autorización y no hay retry automático ante una frontera incierta.

Así, `hygiene` federará evidencia sin absorber los efectos ya existentes de
`maintenance`, `--factory-reset`, curación, `machine-inventory` o
`external-maintenance`. Esas superficies conservan sus owners y gates; agregar
una fuente al registry no amplía su ámbito ni habilita su `--apply`.

### Inventario y deduplicación

La ruta integrada observa metadatos antes de decidir por archivo y ejecuta
`inventory → identify → normalize → policy/redlist → dedupe → routes → organize
→ semantic`. La extensión observada no es fuente de verdad: Identify usa firmas,
contenedores y parsers estructurales bounded; Normalize corrige sólo evidencia
demostrable antes de cualquier hash completo. La redlist explícita se evalúa
sobre la ruta normalizada y no clasifica autoría, procedencia ni utilidad mediante
heurísticas. Archive verifica CRC/tamaño antes
de parsear, extraer texto/FTS o visitar ZIP anidados. Un miembro virtual nunca
se convierte en objetivo físico.

Los archivos sin extensión pasan por Identify. Sólo una evidencia fuerte permite
restaurar una extensión canónica; la incertidumbre, un
destino existente o un drift conserva el original. Redlist y rename cruzan sus
fronteras físicas únicamente con root, identidad, no-reemplazo, ledger y
receipt/recovery válidos.

Identify procesa el inventario por páginas: consulta `content_type_cache` en
lotes acotados, envía sólo misses a observadores bounded y conserva SQLite,
Normalize, política y publicación en el owner principal. La capacidad de los
workers se deriva de los recursos vivos (con una ventana en vuelo acotada) y
se vuelve a muestrear sin crear otro coordinador; los workers sólo observan
bytes/metadatos y devuelven DTOs identity-bound. La clave de cache incluye
volumen, identidad física, tamaño, mtime, birthtime y `DETECTOR_VERSION`, por
lo que un cambio o stale nunca reutiliza evidencia. El progreso se agrupa
por tiempo/cantidad y la publicación conserva el orden del inventario.

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

La construcción de pruebas obtiene metadatos en lotes de hasta 128 solicitudes,
con recuentos completos de alias y enlaces y muestras de rutas acotadas. Los
datos se consumen mientras siguen vigentes las observaciones del grupo; no se
difieren hasta vaciar el acumulador ni se conservan entre planes. La cadencia
temporal del progreso evita emitir un evento por cada fila de metadatos.

Los sucesores copy-on-write y los digests de contenido impiden reutilizar un plan
cuando cambia el contenido aunque `size` y `mtime` permanezcan iguales; los
`scan_id` anteriores quedan históricos y no vuelven a ser el head vigente.

`neocortex.deduplication` conserva snapshots, generaciones, fingerprints y
planes no destructivos. Reduce candidatos por tamaño y huella, pero la igualdad
destructiva exige comparación byte a byte. `mark_abandoned_scans()` concilia
scans `building` abandonados y el coordinador lo invoca antes de continuar; no
debe documentarse esa conciliación como ausente.

### Rutas de contenido

El registro de rutas compone PDF, DOCX, Office, Archive, Text, Audio, Video e
Image. Cada ruta declara inputs, límites, progreso, owner y resultado.
Las implementaciones no tienen la misma riqueza: algunos formatos publican
localizadores estructurales y otros sólo texto o archivo completo. Esa brecha se
expone como cobertura, no se rellena con localizadores inventados.

Office comprueba cancelación antes y después de las lecturas XML y antes de
devolver la extracción, incluidos los componentes XLSX. DOCX clasifica primero
el posible acierto de caché y valida su representación completa una sola vez al
consumirla. La escritura comprueba identidad, firma de procesamiento y estado
vigentes, y mantiene la observación hasta la actualización de FTS.

### Lifecycle durable de `--all` (implementado; aceptación en curso)

`--all` coordina las ocho rutas de contenido bajo un único run Framework:
`pdf`, `docx`, `office`, `archive`, `text`, `audio`, `video` e `image`. Con
`--all --apply`, la ingestión integrada ejecuta
`inventory → identify → normalize → policy/redlist → dedupe → routes → organize
→ semantic → finalize`; cada efecto de Papelera ocurre sobre la ruta normalizada.
La restauración de extensión usa evidencia bounded y rename seguro no-replace.

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
un worker: cubre inventario, redlist, catalogación/deduplicación, las rutas de
contenido, la etapa Semantic y la publicación lógica. Las reservas por
stage/ruta/unidad son
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

Archive materialization, PDF structural recovery y video frame sampling usan
el mismo owner registrado desde sus rutas integradas: `state/scratch/archive-
materialization`, `state/scratch/pdf-recovery` y `state/scratch/video-frames`.
Cada productor vincula `run_id`, conserva `failed-retained` ante error y cierra
sólo después de publicar el resultado; las llamadas directas sin root mantienen
su compatibilidad aislada sin recibir autoridad sobre el estado productivo.

### Auditoría histórica y adopción explícita

`maintenance --scope historical-temp` delega exclusivamente en
`neocortex.runtime.historical_audit.HistoricalAuditManager`. Recibe una raíz
absoluta explícita y literal; no la deriva de `--root`, del estado o de un
default en `/tmp`, y no arranca el grafo de rutas. El plan es read-only, no crea
la raíz y limita la observación a hijos directos con prefijo `neocortex-`, una
lista cerrada de nombres de manifest y un presupuesto de entradas/profundidad/
bytes. Los vecinos no prefijados son unmanaged, no candidatos implícitos.

La clasificación distingue evidencia suficiente de incertidumbre. Una entrada
sólo pasa a `adoptable` si su manifest identificado como NeoCortex contiene
claims exactos de raíz, ruta e identidad física, digest válido, actividad
explícitamente no incierta y una atestación `historical_adoption` aprobada con
`adoption_id` y digest ligados; además exige `state=completed` y
`disposable=true`. Identidad, owner/permisos, enlaces, hardlinks y límites de
montaje se comprueban bounded; nombres, antigüedad, tamaño o un JSON de otra
aplicación no conceden autoridad.

`apply` no confía en el plan read-only: toma una observación nueva y revalida
raíz, manifest, identidad y topología en la frontera de efecto. Sólo entonces
retira la entrada mediante descriptor-relative/no-follow; drift, actividad
incierta, manifest ausente/ambiguo, root no verificable o cualquier estado no
adoptable queda preservado como `blocked` o `recovery_required`. Este owner no
abre SQLite, no usa KIO ni un cleaner externo y no toca corpus, releases,
modelos ni otros estados. Antes de retirar se escribe un receipt durable de
intención fuera de la entrada y, sólo tras confirmar que la entrada desapareció,
se cierra como `applied`; si ese cierre falla, la entrada queda en
`recovery_required`.

Cada ruta declara una capacidad de lifecycle: `phase_resume` conserva progreso
por fase, `safe_replay` reejecuta únicamente con entradas/publicaciones
durables e idempotencia, y `not_resumable` se rechaza explícitamente. La ruta
PDF conserva `phase_resume`; las demás sólo se reanudan cuando su manifest
declara `safe_replay`. Un adapter debe poder estimar su workload de forma
bounded y emitir checkpoints cooperativos, sin reservar de antemano todo un
snapshot que luego filtre candidatos.

La retención por owner no comparte un writer: Inventory conserva el payload de
checkpoints, planes y el componente conectado por sucesores; el planner común
valida reachability bounded, FK/schema y estados incompletos. Es una superficie
de diagnóstico y no una compactación general.

Semantic queda ligado al mismo run, no como una operación posterior sin
identidad. `--all` coordina las rutas de contenido y una selección explícita
puede acotar fuentes; no se introducen techos globales implícitos y los límites
expresados por el usuario siguen siendo acumulativos. Si una fuente, modelo o
herramienta falta, el stage conserva `unavailable` o `blocked` y la corrida
queda `partial`/`incomplete`, sin éxito vacío ni skip silencioso.

La integración de catálogo se ejecuta después de cada productor. Cada fuente
conserva exclusión propia; fuentes distintas preparan y clasifican en paralelo,
con resultados acotados y publicación mediante el writer/CAS del catálogo.
Video puede consumir Audio cuando su caché fuente está publicada y cerrada,
aunque la clasificación de Audio continúe. El estado de Audio sigue pendiente
hasta completar también esa clasificación. Las observaciones `protected`, `no_speech`, `no_audio` y
`metadata_only` siguen consultables con cobertura parcial. FTS y derivados se
reparan desde representaciones durables válidas; un reintento exige evidencia
estructurada `retryable` y sólo se intenta una vez por archivo y corrida. Un
fallo o parcialidad queda en la fase `catalog` y en su evento tipado. Los ocho
summaries exponen contadores `catalog_*` y `catalog_complete`; `None` conserva
el estado no observado y no se interpreta como cero trabajo publicado.

Los títulos de Video son metadata para descubrimiento, no evidencia de un
fotograma. La lectura compatible de títulos legacy conserva esa separación sin
crear timestamps, modificar el cache ni repetir OCR o embeddings. Un localizador
malformado deja su ranking parcial, sin anular rankings independientes.

Las propuestas de organización son advisory y no requieren `--apply`; no
conceden permiso para mover, renombrar o borrar originales. La CLI conserva la
misma selección, presupuesto, estados y stage Semantic en sus modos de perfil
completo y selección acotada; una selección guardada no se amplía por inferencia.

Una nueva ejecución `--all` no es una reanudación obligatoria del intento
anterior. Si queda un pendiente Semantic, valida su manifest/raíz y los heads
publicados de todos los modelos, registra el intento anterior como
fallido y publica un checkpoint nuevo mediante una sustitución atómica que
preserva el prefijo del journal. Ese checkpoint no promueve generaciones
`building`, no declara éxito del intento anterior y no copia SQLite. El nuevo
trabajo usa la petición y los presupuestos actuales. La verificación junto al
commit es sólo observación, nunca reparación.

`--resume-run` explícito conserva productor, manifest, selección y presupuesto
originales. Raíz ajena, schemas futuros, evidencia alterada o cambios concurrentes
siguen causando abstención en su frontera; no se sustituyen contratos de una
reanudación explícita por defaults ni se ocultan parciales.

### Progreso y cancelación

`neocortex.progress` define `ProgressEvent` y métricas estructuradas. Terminal y
grabadores consumen el mismo evento. En Linux los procesos externos usan
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

El recorrido del catálogo usa paginación por clave con predicados indexables y
comparte su validación de esquema durante la iteración.
La política de texto v17 cuenta todos los caracteres, incluidos separadores,
y consulta cancelación entre fragmentos vacíos. Su versión participa tanto en
el marker de caché como en la identidad de replay y clasificación.
Los prefijos de Video se transfieren como BLOB acotado y se decodifican según el
encoding real de SQLite, preservando NUL y Unicode. La planificación
de organización conserva claves y ordinales en una selección TEMP; carga como
máximo una página de 128 payloads junto con la fila en curso y mantiene la
transacción, el orden de decisiones, el progreso y el rollback originales.

Las revisiones incrementales de Semantic usan ventanas acotadas ascendentes o
descendentes y consultas de membresía por generación, tipo e identidad. La
consulta numérica reserva el espacio de agrupación y selección además de los
lotes de puntuación y la residencia ya concedida; no inicia una asignación que
deba esperar indefinidamente contra sus propias reservas.

Catálogo y Semantic son proyecciones reconstruibles con heads publicados.
Knowledge crea un snapshot lógico sobre owners compatibles y fusiona rankings
sin convertir scores heterogéneos en una sola certeza. Puede entregar evidencia
y contexto citado, pero no genera autoridad de mutación.

Al publicar Catalog, sólo las filas retiradas o que cambian de ruta liberan su
ruta activa antes del UPSERT. Las observaciones y sus fechas se actualizan como
parte de la misma publicación atómica. Replay compara todos los campos de la
proyección mediante sus claves únicas y comprueba también filas ausentes o de
otro tipo de fuente, conservando la igualdad de NULL y valores almacenados.

La doble observación mutable de Knowledge cierra el handle cercado de la
primera lectura y abre otro para la segunda. Así no retiene páginas inmutables
de una publicación anterior entre observaciones. El kernel sigue eligiendo
zero-copy para owners quiescentes y conserva sus fences y presupuesto; no se
fuerza una copia temporal por el tamaño del owner ni se amplían sus límites.

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
Knowledge, observación de heads y preflight de reuse leen v7/v8/v9/v10 sólo con
el contrato canónico de la versión observada. Conservan
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
relaciones, contradicciones, grafo y telemetría. CLI, MCP y conveniencias SDK
solicitan v2; la fachada Python v1 permanece disponible sólo
por compatibilidad explícita. `KnowledgeReadBudget` limita filas, vectores,
temporales, deadline y cancelación sin escribir estado ni introducir caches sin
invalidación por heads/fences.

La resolución de hits Semantic prepara la consulta una vez y comparte el
recorrido literal del chunk entre su soporte y su fragmento. Procesa bloques de
4.096 caracteres, sin almacenar todas las coincidencias ni todos los tokens;
el estado auxiliar depende de la consulta y de la ventana solicitada. Conserva
normalización Unicode, frases, cobertura, negaciones, posiciones y desempates.
Los checkpoints de lectura se consultan dentro del recorrido, incluso al cruzar
una palabra larga, y alrededor de descompresión y materialización. La búsqueda
léxica independiente transmite también su callback explícito. Cancelación o
deadline mantienen su error y los cargos de filas observadas. Las comprobaciones
de testigos conservan su cota y su interpretación, sin convertir coincidencia
literal en suficiencia de respuesta.

`neocortex.content-diagnostics/v2` federa los ocho owners de contenido mediante
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

### Curación automática y evidencia

`curate plan`, `curate scan` y `curate verify` consultan publicaciones acotadas,
revalidan identidad y bytes y devuelven cobertura, razones e incertidumbre. No
crean tareas humanas, colas, eventos ni grants. El lector paginado de
`workflow.findings_query` conserva observaciones no autoritativas del owner
`findings`; no las convierte en planes de ejecución.

`--apply` es la única autorización de usuario para propuestas automáticas de
alta confianza dentro de una raíz contenida. FrameworkActions revalida la raíz,
identidad, política y no-follow junto al efecto, registra `file_actions` y
receipts, y deja `recovery_required` ante una frontera ambigua. POSIX rename y
KIO Trash viven en el owner neutral de mutaciones; no existe un backend físico
paralelo de curation ni una capa de autorización humana.

La vista `neocortex.curation.read` proyecta intentos, receipts y recovery sin
abrir el corpus ni crear estado. Restore y su confirmación exacta siguen siendo
un flujo separado de recovery; consultar evidencia no autoriza reintentar,
restaurar o ampliar la ejecución.

## Persistencia

`STATE_STORE_REGISTRY` es el inventario contractual de owners:

```text
inventory, framework, catalog, pdf, docx, office, audio,
video, image, semantic, archive, text
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

La publicación Semantic usa staging y CAS lógico: sólo avanza el epoch cuando
todos los heads requeridos por el manifest están completos y no hay drift.
Parcialidad, owner-head drift o una preparación ambigua producen
`blocked`/`recovery_required`; no se simula una transacción SQLite distribuida.
Una consulta de estado observa estos datos mediante el owner/publication
canónicos y no abre una SQLite cercada durante writers.

El producto sí expone backup y restore generales mediante `Neocortex databases`.
Persistencia define el contrato; el procedimiento está en
[RECOVERY.md](RECOVERY.md).

### Factory reset operativo

`Neocortex --factory-reset` es una frontera de mantenimiento separada de las
rutas de contenido y de `databases purge`. Su propósito es retirar todo el
estado operativo administrado de la raíz seleccionada: bases SQLite y sidecars,
materializaciones de ZIP administradas bajo ella (incluido
`state/archive-materialized`), cachés y metadatos de procesamiento. No toca
destinos externos producidos por APIs standalone. Los originales del corpus, los
ZIP que los contienen, la instalación, los modelos y los
`installation-receipts` permanecen protegidos.

La invocación admite `--state-directory` como override para cercar fixtures y
no acepta `--root`, rutas de contenido, scopes, preview/apply, backup, snapshot
SQL, plan,
digest, receipt durable adicional, `--apply` ni `--yes`. No procesa el corpus ni
reconstruye contenido. La operación toma sus locks y verifica writers, procesos y
rutas/montajes antes de retirar. Los symlinks dentro de la raíz se desvinculan
sin tocar sus targets; no se siguen ni se borran targets externos. Rutas o
montajes ajenos, permisos insuficientes, cambios concurrentes y targets no
verificables producen un error con conteos parciales.

El borrado no es una transacción física distribuida ni una autorización para
adoptar rutas externas. Si alguna frontera impide completar el alcance
operativo, el error conserva los conteos parciales y la CLI termina con código
distinto de cero; no declara éxito completo por conteos esperados ni por
ausencia posterior de una base.

## Interfaces públicas

- **CLI instalada:** `Neocortex`; el parser es la fuente exacta de argumentos.
- **Mantenimiento histórico:** la CLI expone `historical-temp` sólo con
  `--maintenance-audit-root` absoluto; su plan y su aplicación delegan al
  owner histórico y no comparten autoridad con scratch, corpus o SQLite.
- **Diagnóstico externo:** `external-maintenance` exige `--external-root` y
  `--external-category`, devuelve `neocortex.external-maintenance/v1` y es
  siempre metadata-only; categorías sin owner no se vuelven candidatas.
- **Inventario de máquina:** `machine-inventory` devuelve
  `neocortex.machine-inventory/v1` con raíces, categorías, owners/procedencia,
  estados, límites y métricas bounded. Es siempre `read_only`; no abre SQLite,
  no lee payload, no escribe estado/corpus y no ofrece acciones.
- **Preparación de higiene:** `hygiene` compone registry y manifest versionados
  de esas fuentes, con owners, categorías canónicas/operativas/rebuildables/
  temporales/cache, procedencia, drift y cobertura. En esta cohorte es
  read-only/preview-only, con zero deletion y sin `file_actions`; no es un nuevo
  owner ni expone `apply`.
- **API/SDK Python:** no exponen una variante paralela del factory reset; la
  operación destructiva completa permanece en la CLI `--factory-reset` y
  conserva el `--state-directory` explícito para fixtures.
- **MCP:** servidor stdio local con consultas read-only de plan, scan y verify;
  no publica tareas humanas, autorización, aplicación ni conciliación escrita.

Las tres superficies deben conservar operación, scope, cobertura, epoch,
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
mutación implícita en las corridas sin `--apply`. No existe una ruta de
autorización humana paralela.

La frontera `maintenance --scope historical-temp` es independiente de esas
acciones: su plan nunca muta, y `--apply` sólo puede retirar una entrada con
manifest/adopción verificables después de una revalidación fresca. No usa KIO,
SQLite, `rm` ni otro cleaner externo, no selecciona `/tmp` por defecto y no
modifica el corpus.

El diagnóstico externo comparte los límites de la plataforma sin convertirse
en owner: el registry sólo describe procedencia y clasificación. Miniaturas,
caches de aplicaciones, paquetes, journal, coredumps, sesiones Codex, backups,
Papelera y otros árboles no administrados se reportan como `out_of_profile`,
`preserved` o `unknown`; no hay fallback de borrado.

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

La planificación de procesos contempla simultáneamente RAM y commit libres,
residencia del intérprete y espacio para la tarea. La nueva concesión de un
presupuesto nativo se ajusta durante la admisión, antes de configurar el backend.
Después, un backend de ancho fijo conserva esa demanda al renovar su permiso;
una reducción temporal de capacidad produce espera cancelable, no una reserva
menor que los hilos que el modelo seguirá usando. El propietario del mapa
elástico observa el deadline durante la espera de resultados y mantiene la
excepción principal si también falla la limpieza.

`GlobalResourceCoordinator` vive desde inventario y Dedup hasta las rutas,
Catalog, Semantic y Knowledge del mismo run. Los workers heredan ese contexto;
una entrada independiente posee y cierra su propio scope cuando lo necesita.
La admisión comparte CPU, threads nativos, memoria transitoria, residencia,
espacio temporal e I/O por dispositivo. Mantener un modelo o intérprete cargado
reserva memoria sin ocupar CPU; los resultados conservan su reserva hasta que
el owner los consume. Sólo un proceso propio identificado por PID y starttime
puede aportar crédito de memoria privada ya materializada.
El registro de un hijo verificado alimenta también al observador cuando Linux
no expone los listados de hijos. Cada muestra vuelve a comprobar identidad y
cgroup; el crédito exige memoria privada legible y una concesión todavía viva.
Esto no convierte una observación incompleta del árbol o de CPU en completa.
Los módulos de orquestación, acciones, persistencia, Archive, recursos, CLI
Semantic, Scratch y Artifact Registry son fachadas/coordinadores delgados sobre
owners cohesivos; la separación no crea conexiones SQLite adicionales ni mueve
las fronteras de seguridad.
Las renovaciones conservan su contexto de apertura y cierre al pasar entre
preparador, trabajador y consumidor. Cada ruta mantiene sus reservas hasta
cerrar, dentro de un contexto propio que las demás rutas no heredan.
Los techos de memoria explícitos por formato limitan su agregado residente y
transitorio en ese mismo ledger; se exigen junto al techo global. La configuración
Framework distingue ausencia (`None`, automático) de un número explícito, también
cuando coincide con un antiguo default. Los manifests de initial, route-only y
resume conservan los valores de la invocación; no se infiere intención a partir
de un valor histórico. Los constructores autónomos mantienen sus defaults locales.

El observador Linux publica una muestra compartida cada 250 ms por defecto,
con carga propia y externa, memoria, PSI, cgroups v2 y afinidad. Los gates
reutilizan esa muestra y la topología se actualiza durante el run. La capacidad
CPU incluye cuotas fraccionales, y la memoria distingue `memory.high`, límite
duro y margen disponible. La carga propia no provoca una reducción circular de
workers. Una observación incompleta conserva su estado desconocido.

Sin techos explícitos, la capacidad crece hasta los recursos útiles disponibles
para las unidades pendientes. No hay un máximo fijo de dos/cuatro workers por
formato ni de ocho threads Semantic. Los pools crecen y retiran cohortes ociosas
según disponibilidad; no cambian atributos privados de executors. Una reserva
pequeña para el sistema y las estimaciones del trabajo evitan que ocupar RAM sin
utilidad sustituya al procesamiento. La competencia externa reduce nuevas
admisiones y los checkpoints renuevan la ejecución; la recuperación vuelve a
abrir capacidad. El drenaje de un resultado ya terminado puede guardar sus
datos y liberar RAM bajo presión, sin admitir nuevos buffers o trabajo pesado.
La espera automática por capacidad dura hasta recuperación, cancelación o
deadline del run; un timeout de espera es opcional. El deadline publicado se
copia una vez desde el owner y se comprueba durante admisión sin reabrir SQLite.
Los hijos reducen su propia prioridad y, sólo en una sesión privada comprobada,
la de su autogroup. La política deja intactos al caller y a procesos ajenos.

Text, DOCX y Office separan preparación/publicación en el owner del análisis en
procesos. Text aplica el timeout y límite de memoria dentro del
parser aislado. Archive conserva identidad virtual y presupuesto por
contenedor al paralelizar extracción; usa supervisores por contenedor y procesos
para miembros mayores, evitando crear procesos para cada ZIP diminuto. PDF
automático ejecuta MuPDF en procesos aislados; el modo local requiere un único
worker explícito sin timeout de documento. Los procesos y modelos reutilizados
mantienen su residencia contabilizada hasta cerrarse; cancelación espera su
salida antes de retirar snapshots temporales.

Dedup lee primero las muestras que pueden excluir candidatos grandes y calcula
el hash completo de los supervivientes. Una muestra nunca prueba igualdad ni
valida por sí sola una caché durable. Hashing usa workers de I/O acotados y
publicación desde la conexión del owner; revalida identidad y versión de cambio.
Semantic agrupa la tokenización exacta y prepara los chunks antes de abrir la
transacción de escritura. Los límites nativos de BLAS se aplican y restauran
bajo una exclusión compartida mediante `threadpoolctl`; su coste forma parte
del grant de ejecución.

Audio admite réplicas Whisper según CPU, RAM y, cuando el backend resuelto es
CUDA, VRAM del dispositivo identificado. La reutilización de VRAM requiere
atribución a procesos propios comprobados; una observación desconocida no
autoriza capacidad supuesta. Los cambios de threads o workers no cambian la
identidad del contenido procesado; cambiar CPU por CUDA sí cambia la firma
efectiva del backend. Semantic conserva su proveedor CPU verificado.

Video decodifica el plan seleccionado en un lote FFmpeg, con límites de salida,
memoria para ambas representaciones de la captura y reserva temporal antes del
productor. El análisis de escenas y keyframes conserva sus consultas propias.
El OCR reutiliza sólo frames PNG exactamente iguales dentro del mismo archivo y
conserva tiempo y motivo de cada selección. La captura comprueba cancelación
durante lectura y espera, y recoge el proceso propio antes de devolver el error.

Las superficies de ayuda y los contratos de configuración no importan motores
ni backends de presentación. El diagnóstico de paquetes usa requisitos de `pyproject.toml` en fuente
o `Requires-Dist` del paquete instalado, sin una lista paralela de versiones;
inspección de metadata, localización de ejecutable, archivos de modelo y éxito
de procesamiento no son equivalentes.

El coordinador registra consumo, espera, capacidad y fases. Writers toman exclusión
cooperativa; backup, restore, purge y factory reset requieren exclusión más fuerte. Los
subprocesos tardíos no pueden publicar sobre un head nuevo. Un fallo alrededor
de la frontera de efecto produce un estado conciliable, no un reintento ciego.

PDF transmite su token local a la admisión global. Los gates vuelven a consultar
cancelación después del sondeo de memoria y antes de conceder recursos. Las
señales explícitas de CPU conservan presión e histéresis; el observador
predeterminado controla capacidad con carga externa atribuible. La estimación
de candidatos consulta cancelación durante el recorrido y el inventario consume
su presupuesto mientras enumera. Los límites de workers son techos opcionales;
no sustituyen la disponibilidad observada.

La terminación de procesos aislados identifica el wrapper propio por PID y
starttime y limpia su grupo original aunque el líder ya haya terminado. Este
límite de PGID no contiene descendientes que creen otra sesión o grupo.

Antes de iniciar workers de contenido, `FrameworkState.route_candidate_snapshot()`
publica una proyección temporal bounded desde la conexión writer que ya posee el
owner, con lectura fijada, copia por páginas y comprobación acotada. Conserva
únicamente la generación de candidatos, las recomendaciones abiertas ligadas a
ella y la evidencia mínima de fases de replay; el backup completo queda para
llamadas legacy sin una generación explícita. `FrameworkRouteState` usa esa
vista inmutable sólo para candidatos; acciones y lifecycle
conservan sus owners. La copia vive hasta que terminan todos los
workers, incluso ante error o cancelación, sin abrir un lector ordinario en el
origen ni relajar los fences de `SQLiteReadSession`. La estimación multimodal conserva
el orden de los selectores MIME y omite sus solapamientos por precedencia,
aprovechando la unicidad de path/MIME del owner sin acumular todos los paths.

En `--all`, la misma frontera se conserva entre workers de ruta y stages
posteriores. El progreso, transcript y estado público distinguen `complete`,
`partial`, `unavailable`, `blocked`, `cancelled` y `recovery_required`; una
ruta no disponible no se convierte en cobertura completa por terminar las
demás. La reanudación valida root, política, snapshot, modelo, herramienta,
manifest y owner heads antes de publicar, y se abstiene fail-closed ante drift.

## Brechas vigentes

- Knowledge conserva observaciones `best_effort_non_generational` para Framework
  y los agregados de documentos/imágenes; no equivalen a una publicación
  generacional de todos esos owners;
- la proyección de evidencia mantiene `reference_only` si faltan snippet o
  localizador verificado; no inventa estructura ni suficiencia de respuesta;
- MCP conserva sólo consultas de evidencia; no expone autorización, aplicación
  ni conciliación escrita;
- Framework construye `KioTrashBackend` en Linux al solicitar efectos. La ruta
  exige política de plataforma, revalidación física y recibo. Las fixtures del
  adapter no certifican disponibilidad KDE/KIO ni restore de escritorio en el
  equipo instalado;
- `document_cache_sync` distingue `trash` de move/rename y exige una política de
  invalidación del owner; ese contrato no certifica por sí solo la sincronización
  de todos los consumidores después de un efecto real;

La prioridad y los criterios de aceptación están en
[ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md); seguridad y owners se detallan en
[SECURITY.md](SECURITY.md) y [PERSISTENCE.md](PERSISTENCE.md).

## Contratos consolidados de estado, mantenimiento y preparación

El registro de owners expone una política versionada de tablas. Cada regla declara
rol, fuente de reconstrucción, retención, dependencias y frontera durable. Factory
reset, backup y restore respetan ese mismo mapa; las tablas desconocidas, incluso
vacías, no se convierten en proyecciones descartables. Framework, Inventory y
Catalog conservan sus barreras operacionales para distinguir historial protegido
de trabajo reutilizable. El factory reset sólo declara el alcance físico que
pudo retirar y verificar.

El inventario de estado relaciona SQLite, artefactos, referencias y pruebas de
reconstrucción Archive. El factory reset retira únicamente los objetivos
operativos dentro de la raíz cercada; no procesa el corpus ni adopta rutas
externas. Las copias canónicas, los ZIP originales, la instalación, los modelos
y los `installation-receipts` siguen protegidos.

El coordinador de mantenimiento usa planes y verificadores de los owners
existentes. La autoridad de cada scope viaja separada de su fingerprint. Framework
registra las fases prepared y confirmed y conserva por separado el resultado del
trabajo principal y el mantenimiento. Un fallo de publicación del recibo impide
presentar el efecto como completado. La selección histórica añade aprobación
privada autenticada; ni el JSON del payload ni su prefijo pueden concederla.

Los recorridos POSIX de scratch comparten observación relativa a descriptores,
identidad y mounts, con iteración en profundidad, presupuesto de miembros,
lectura limitada de manifests y cobertura explícita. Las rutas en bytes se
conservan independientemente de sus representaciones de presentación. El cierre
de procesos distingue grupo observado de cgroup delegado; un grupo aislado no
demuestra ausencia de descendientes que se hayan separado de él.

Cada ejecución inicial o de rutas compone una preparación acotada sobre la misma
frontera del inventario. Las comprobaciones previas declaran estado, alcance,
evidencia y momento; las validaciones de contenido, CRC e inferencia permanecen
marcadas como no ejecutadas hasta el owner correspondiente. La selección, las
opciones y los presupuestos se conservan en el evento de preparación sin alterar
el fingerprint estable de procesamiento.

La distribución Linux v2 acredita SQLite en el intérprete que ejecutará el
producto. La identidad incluye ejecutable, módulo, bibliotecas cargadas, source_id,
opciones y pruebas funcionales. Una política revisada fijada por digest y un
recibo externo ligan la evidencia a la release; verify y rollback vuelven a medirla.
Los manifests v1 permanecen legibles con acreditación pendiente. La instalación
consume wheels y modelos locales; la adquisición es una operación separada.
