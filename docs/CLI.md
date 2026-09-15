# Interfaz de línea de comandos

La interfaz canónica es el ejecutable instalado `Neocortex`. La definición
exacta vive en `neocortex/api/cli/cli_parser.py` y en los subparsers de
`neocortex.api.cli.human`; este documento organiza su uso, no sustituye
`Neocortex --help`.

## Inventario federado bounded de máquina

`machine-inventory` es una consulta local de metadata, siempre read-only. Sin
raíces explícitas observa sólo perfiles bounded de `HOME`, `/tmp`, cache y
configuración XDG, y estado, datos y corpus de NeoCortex; nunca recorre `/` por
omisión. `--machine-root` puede repetirse para seleccionar raíces absolutas,
incluida `/` cuando el caller la solicita conscientemente. La raíz ausente se
reporta, pero no se crea.

```bash
Neocortex machine-inventory --machine-json
Neocortex machine-inventory \
  --machine-root "$HOME" \
  --machine-root /tmp \
  --machine-max-entries 20000 \
  --machine-max-depth 8 \
  --machine-max-bytes 1073741824 \
  --machine-json
```

### Resumen y detalle

El JSON es compacto por defecto. Publica una fila/resumen para cada raíz
efectiva y los agregados globales, sin serializar todos los registros de las
entradas. Para pedir registros bounded de forma explícita usa
`--machine-json=records` (el modo predeterminado equivale a `compact`); esta
opción no aumenta `--machine-max-entries`, `--machine-max-depth` ni
`--machine-max-bytes`, y la serialización conserva su propio tope. `root_count`
y los resúmenes de raíz permanecen completos aunque
el recorrido haya agotado el presupuesto antes de visitar una raíz posterior;
si el tope excepcional de presentación recorta la lista, se informa
`serialization.root_summaries_omitted` y `presentation_truncated`.
Sin `--machine-json`, la vista humana conserva la misma cabecera y filas
compactas por raíz; no imprime el inventario completo por accidente.

### Cuota de entradas y contadores

El límite `--machine-max-entries` sigue siendo global, pero el owner lo reparte
entre las raíces seleccionadas con `root_quota_policy` igual a
`equal_fair_share_v1`. `root_entry_quotas` publica la cuota efectiva indexada por
`root_index`, y cada elemento de `root_summaries` publica el mismo valor como
`entry_quota`. Para la raíz actual, la cuota es
`ceil(entradas_globales_restantes / raíces_restantes)`; si termina antes, las
entradas no usadas regresan al fondo global y se asignan a las raíces
posteriores. Ninguna raíz puede exceder su cuota efectiva. Si el fondo se
agota, `entry_quota=0` aún produce el resumen de la raíz y el motivo de la
frontera global; no se interpreta como `absent`. La política sólo reparte
entradas: `--machine-max-depth` y `--machine-max-bytes` continúan siendo cotas
globales y pueden interrumpir una raíz antes de usar su cuota.

La salida separa contabilidad de registros y de raíces:

- `records_scanned` cuenta sólo observaciones de entradas, no filas sintéticas
  de resumen de una raíz.
- `record_status_counts`, `record_category_counts` y `record_reason_counts`
  cuentan únicamente esos registros observados.
- `root_status_counts` cuenta una vez el estado de cada raíz efectiva.
- `root_marker_status_counts`, `root_marker_category_counts` y
  `root_marker_reason_counts` desglosan los marcadores sintéticos que pueden
  alimentar los nombres históricos.
- `status_counts`, `category_counts` y `reason_counts` son nombres históricos
  compatibles y pueden incluir marcadores de raíz `absent`, `blocked` o
  `unknown`; no deben sumarse a los mapas `record_*`.

`root_quota_policy`, `root_entry_quotas`, `entry_quota` y estos grupos de
contadores forman parte de la evidencia de cobertura, no de una autorización
de lectura adicional ni de una acción de limpieza. La vista Python
`to_summary_dict()` conserva estos campos para que un consumidor no tenga que
inferirlos a partir de la cantidad de registros.

La salida distingue la observación de su presentación:

| Campo | Significado |
|---|---|
| `root_summaries` | Resumen sin registros completos para cada raíz efectiva; conserva también las raíces no visitadas por el presupuesto mientras quepa en la cota de presentación. |
| `scanner_truncated` / `truncated` | El owner no completó el recorrido. `truncation_reasons` identifica `entry_limit`, `depth_limit`, `byte_limit`, permisos, carrera o cancelación. |
| `serialization.mode` | `compact` por omisión; `records` sólo cuando se solicitan registros. |
| `serialization.records_included` | Si la proyección incluyó registros; no equivale a que no existan entradas. |
| `serialization.records_returned` / `serialization.records_omitted` | Registros serializados y registros disponibles que no se incluyeron en la respuesta. |
| `presentation_truncated` | El límite de la proyección de salida omitió registros; es independiente de `scanner_truncated`. |

En la superficie Python del owner, la misma separación se expresa en
`coverage_metadata` y `omissions`: cada uno tiene facetas `scanner` y
`presentation`, con `records_scanned`, `records_returned`,
`records_omitted`/`records_omitted_known` y sus razones. La CLI puede conservar
esas facetas dentro de `result` además del alias `serialization`; siempre usa
la proyección `root_summaries` para no reintroducir `records` por accidente.
El `records_omitted` de `scanner` puede ser `null` porque una valla impide
contar entradas que quedaron fuera; `serialization.records_omitted` es, en
cambio, el conteo conocido de registros que esta respuesta decidió no incluir.
El modo `compact` puede tener este último valor mayor que cero con
`presentation_truncated=false`: no es una falla del escáner.

Una salida puede tener `scanner_truncated=true` y
`presentation_truncated=false` (el escáner quedó corto, pero el resumen cabe),
o viceversa (el escáner terminó y se limitó el detalle). No uses el marcador de
sanitización `[contenido omitido por límite]` como señal del escáner: pertenece
a otra frontera de presentación. Las raíces no visitadas conservan su estado y
razón en el resumen de raíz; `root_count` no se reduce a las raíces que sí
recibieron entradas.

Los límites `--machine-max-entries`, `--machine-max-depth` y
`--machine-max-bytes` son globales para toda la invocación y mantienen techos
válidos. Sus valores predeterminados son 10,000 entradas, profundidad 2 y 1 TiB
de bytes observados; los registros, resúmenes, muestras y agregados también son bounded. La salida
JSON usa el envelope cerrado `neocortex.machine-inventory/v1` y contiene
`operation`, `read_only=true`, `roots`, `root_count`, `limits`, `truncated`,
`root_summaries`, `root_quota_policy`, `root_entry_quotas`, conteos
`record_status_counts`/`record_category_counts`/`record_reason_counts`,
`root_status_counts` y los nombres históricos por estado/categoría, bytes observados/aparentes/asignados,
`reason_summary`, `reason_explanations` y el registro de categorías, owner y procedencia.
La colección `records` sólo aparece con `--machine-json=records` (en `result` y
como alias de compatibilidad en el envelope).

### Semántica de bytes

Los tres contadores no son sinónimos:

- `apparent`: tamaño lógico de la entrada (`st_size`), sin leer el payload;
- `allocated`: bloques reportados por el filesystem (`st_blocks * 512` en
  Linux);
- `observed`: crédito de presupuesto usado por el escáner, igual a
  `apparent + allocated` de los metadatos que pudo observar.

Directorios y objetos especiales normalmente aportan cero a esos tamaños. Un
`observed` grande no es espacio recuperable, no corrige hardlinks/symlinks y no
demuestra uso físico exclusivo. Cuando se agota `--machine-max-bytes`, el
crédito queda limitado, se marca `byte_limit` y la cobertura pasa a ser parcial;
el límite es global entre todas las raíces, no uno nuevo por raíz.

Los estados son `absent`, `observed`, `preserved`, `blocked`, `unknown` y
`out_of_profile`. Las categorías separan `neocortex_state`, `neocortex_data` y
`neocortex_corpus`, además de `tmp`, `cache`, `config`, `home` y `external`; esa
clasificación documenta ownership/procedencia y no autoriza ninguna acción.

| Perfil | Owner/procedencia observada | Tratamiento de esta consulta |
|---|---|---|
| `neocortex_state`, `neocortex_data`, `neocortex_corpus` | NeoCortex, sólo en las raíces canónicas identificadas | metadata observada; no se publica ni modifica el owner |
| `tmp` | `operating-system`, raíz fija `/tmp` | observación, preservación o bloqueo |
| `cache`, `config` | `xdg/application-cache` y `xdg/application-config` | no candidato implícito |
| `home` | usuario; el árbol amplio no prueba ownership por elemento | sólo observación bounded |
| `external` | raíz aportada por el caller, sin owner probado | `unknown`/`out_of_profile` o preservado |

El recorrido usa `lstat`/no-follow para identidad física, tipo, permisos,
montaje, symlink/hardlink y tamaños. No lee contenido, no abre SQLite ni
sidecars (tampoco con `mode=ro`), no escribe estado o corpus y no usa red, KIO,
sudo ni cleaners. Un hallazgo, una raíz ausente o cobertura truncada no es un
error de configuración: una consulta válida conserva salida de diagnóstico y
exit 0; los errores de argumentos, raíces o límites devuelven exit 2.

No mezcles `--machine-root` con `--root`: el primero define raíces de metadata
de la máquina y el segundo conserva su semántica de corpus. El comando rechaza
`--apply`, `--all`, `--dedupe`, rutas y operaciones directas. El siguiente gate
para acciones queda fuera de esta entrega y deberá aportar owner probado,
política, selección/preview, autorización explícita, revalidación fresca y un
backend reversible con receipt, verificación de postcondición y recovery.

## Diagnósticos federados v2

La vista aditiva `content-diagnostics/v2` consulta únicamente estado publicado,
sin escanear el corpus ni crear owners. Acepta un owner o `all`, filtros y un
cursor ligado a raíz, filtros y snapshots; distingue owner ausente, parcial,
futuro, corrupto, bloqueado y no disponible de cero incidencias.

```bash
Neocortex --root "$Root" --content-diagnostics 20 \
  --diagnostics-owner all --diagnostics-json
Neocortex --root "$Root" --content-diagnostics 20 \
  --diagnostics-owner text --diagnostics-budget-rows 500 \
  --diagnostics-deadline-seconds 5 --diagnostics-json
```

`--pdf-diagnostics`, `--text-errors` y `--archive-issues` conservan el
contrato v1. La API/SDK exponen `content_diagnostics_v2_payload`; MCP conserva
`content_diagnostics` v1 y añade `content_diagnostics_v2`. Todas las variantes
son read-only y no conceden autoridad.

Las consultas Knowledge admiten límites opcionales de lectura (`--knowledge-budget-rows`,
`--knowledge-budget-vectors`, `--knowledge-budget-temporary-bytes` y
`--knowledge-budget-seconds`); el agotamiento devuelve cobertura parcial y una
razón tipada, sin reintento ciego ni cache de resultados.

## Estado del lifecycle de curación

**CURRENT — consulta:** `curate plan` consulta la raíz de estado canónica, no
acepta rutas de estado o corpus y devuelve una página acotada con
`plan_digest`, `snapshot`, `cursor` y `next_cursor`. No escribe estado ni
autoriza acciones.

```bash
Neocortex curate plan --limit 50
Neocortex curate plan --limit 50 --cursor TOKEN
Neocortex curate plan --limit 50 --json
```

**IMPLEMENTED — scan y verificación exacta:** `curate scan` consulta la misma
publicación acotada e incluye sus cabezas de estado, mientras `curate verify`
relee los archivos regulares referenciados por el plan, comprueba identidad,
hash completo y comparación byte a byte de cada grupo duplicado. Ambas
operaciones son advisory, no crean estado, `ReviewTask`, grants ni
`file_actions`, y no modifican el corpus.

```bash
Neocortex curate scan --limit 50 --json
Neocortex curate scan --limit 50 --cursor TOKEN --json
Neocortex curate verify PLAN_ID --limit 100 --json
Neocortex curate verify PLAN_ID --limit 100 --cursor TOKEN --json
Neocortex curate verify PLAN_ID --item-id ITEM_ID --json
```

Un plan rápido conserva `persisted_mode=fast`; si la comprobación actual pasa,
la respuesta informa `observed_mode=full_hash`, sin convertir por sí sola el
plan persistido en autorización. Un cambio de identidad, bytes, symlink,
archivo especial, raíz o presupuesto devuelve una abstención tipada.

La respuesta conserva `source_heads` para inventario y catálogo, con su owner,
revisión, digest, cobertura y razón vigente; `--cursor` permite verificar una
página posterior sin confundirla con un cambio de snapshot.

El digest representa el stream completo de propuestas y no cambia al variar
`--limit`; si la publicación cambia, el cursor anterior se rechaza y debe
iniciarse una consulta nueva.

**IMPLEMENTED — ReviewTask advisory:** `curate review` publica una página del
plan completo como tareas de revisión, y `curate decide` registra por CAS una
decisión humana. Son interfaces heredadas de 0.12.0; su presencia no demuestra que la
instalación incluya las correcciones posteriores del checkout.

```bash
Neocortex curate review PLAN_ID --limit 50 --json
Neocortex curate review PLAN_ID --limit 50 --cursor TOKEN --json
Neocortex curate decide PLAN_ID ITEM_ID \
  --expected-event-id EVENT_ID \
  --decision resolved \
  --decision-scope until-source-change \
  --actor ACTOR --note "evidencia revisada" --json
```

`PLAN_ID` es el `plan_digest` de `curate plan`. Review devuelve para cada item su
`task_id`, estado y `current_event_id`. Decide acepta `resolved` o `dismissed` y
los scopes `until-source-change`, `until-policy-change` o `permanent`. El mismo
evento se reproduce de forma idempotente; un digest o head distinto se rechaza.

Estas operaciones tienen `read_only=false` porque escriben únicamente
ReviewTask en Framework. No crean `file_actions`, no autorizan, no llaman KIO y
no cambian corpus ni sistemas externos. `--json` devuelve el envelope; no es una
interfaz de exportación ni crea un ZIP.

**IMPLEMENTED — AuthorizationGrant durable:** después de resolver los items,
`curate authorize` emite un grant explícito sin aplicar efectos:

```bash
Neocortex curate authorize PLAN_ID \
  --item-id ITEM_ID \
  --action move \
  --actor ACTOR \
  --expires-ns NS \
  --max-bytes BYTES \
  --json
```

`--item-id` puede repetirse hasta 100 veces; `--action` acepta `trash`, `move` o
`rename`, y `--authorization-key` permite una key de idempotencia explícita. El
grant valida el plan, el snapshot y los heads actuales de ReviewTask al emitirse,
y persiste un manifiesto inmutable con task IDs, versiones, eventos, fingerprints
y digest agregado. Trash de duplicados exige `verification_mode=full_hash`;
move/rename exige destino absoluto. La respuesta declara `actions_authorized=true` y
`physical_effect_applied=false`: no crea `file_actions`, no invoca KIO y no toca
corpus ni sistemas externos.

**IMPLEMENTED sobre fixtures y backends inyectados:** `curate apply` consume sólo
un grant confirmado y `curate reconcile` registra observaciones bounded, sin
reintentar efectos. La CLI instalada no selecciona backend ni run firmado por sí
sola, así que `curate apply` devuelve `backend_unavailable` antes de crear
`file_actions`; la ejecución física de 0.11 se prueba desde API/SDK con un
backend POSIX o KIO falso y una raíz temporal.

```bash
Neocortex curate apply GRANT_ID --confirm-grant-id GRANT_ID --json
Neocortex curate reconcile --actor ACTOR --confirm-reconcile --limit 100 --json
```

El consumidor rechaza grants legacy sin manifest de efectos, vuelve a comprobar
plan, source heads, ReviewTask heads, expiración, identidad, tamaño, mtime, hash,
contención y presupuesto, y procesa un efecto por vez con
`started → applying → applied|recovery_required`. Un timeout, receipt inválido,
interrupción o ambigüedad queda en `recovery_required`, sin fallback a `gio`,
`unlink`, sobrescritura ni reintento automático. `reconcile` sólo añade evidencia
append-only e idempotente; no convierte la observación en permiso.

**IMPLEMENTED — estado y restore de fixtures:**

```bash
Neocortex curate recovery status --limit 100 --json
Neocortex curate restore preview ACTION_ID --json
Neocortex curate restore apply ACTION_ID \
  --confirm-action-id ACTION_ID --confirmation TOKEN --actor ACTOR --json
```

`recovery status` y `restore preview` son read-only y muestran sólo evidencia
bounded. `restore apply` exige el token exacto derivado del receipt original,
crea un intento separado antes de mover, utiliza no-replace same-filesystem y
verifica bytes e identidad, mientras la CLI ordinaria falla cerrada sin backend
inyectado. El restore de owners SQLite mediante `databases restore` mantiene su
flujo y autoridad independientes.

La primera tranche 0.12 mantiene `curate scan` y `curate verify` sin efectos y
con límites bounded. La verificación exacta contabiliza items, archivos y bytes
reales, admite deadline/cancelación en la API Python mediante
`CurationWorkBudget` y devuelve las razones `budget_exhausted`, `cancelled` o
`deadline_exceeded`. Los checkpoints y su reanudación por página se consumen
mediante `neocortex.api.public` o `neocortex.sdk`, exigen un directorio de estado
explícito para no seleccionar el corpus por accidente y no se exponen en MCP;
la CLI conserva sus límites seguros por defecto.

El inventario DFS checkpointado se consume en la API Python, no mediante un
flag genérico de la CLI: `DedupIndex.scan` acepta `checkpoint_path`, `resume`,
`deterministic` y un `InventoryWorkBudget`. El primer proceso crea un owner
externo y, si se interrumpe, deja `partial`; la siguiente corrida usa
`resume=True`, valida raíz, política, prefijo y ancestros, elimina sólo el tail
no confirmado y continúa. Un owner `complete` se puede repetir para validar
drift sin crear otro `scan_id`; el contrato no toca el corpus ni se publica como
operación MCP.

Si un nombre POSIX contiene bytes no representables por SQLite TEXT, el
inventario conserva los archivos independientes y devuelve salida 2 con
`unsupported_path_encoding`, sin traceback ni publicación completa. No
renombra los originales ni sustituye caracteres para inventar otra ruta.

## Mantenimiento registrado de scratch

**IMPLEMENTADO Y VERIFICADO EN LA RELEASE `0.14.0-a02f6761ece2`:** `maintenance` es una
hoja de control local para el scratch registrado de NeoCortex. El alcance se
resuelve exclusivamente bajo `<state_directory>/scratch/`:
`owned-temp` y `audit-work`; no usa `--root` para redirigirlo ni escanea
`/tmp`. La consulta predeterminada sólo genera un plan bounded, no crea la
raíz ausente ni produce un efecto físico.

```bash
Neocortex maintenance --scope owned-temp --maintenance-json
Neocortex maintenance --scope audit-work --maintenance-json
Neocortex maintenance --scope owned-temp --apply --maintenance-json
```

`--apply` sólo puede retirar scratch propio, registrado y en estado
`completed`; el scratch interno no pasa por KIO. El estado derivado fuera de
`scratch/`, `host/.cache`, `.codex`, el corpus, las releases y las SQLite
productivas quedan fuera de este comando. `--all --apply` puede conciliar
también esta área privada únicamente después de una integración verificada;
no amplía esos límites. `--all` sin `--apply` no produce efecto físico,
aunque el lifecycle puede escribir estado derivado.

Las rutas integradas de Archive, PDF y video crean sus workspaces bajo
`state/scratch/archive-materialization`, `state/scratch/pdf-recovery` y
`state/scratch/video-frames`; una excepción conserva `failed-retained` y el
cierre exitoso retira sólo el workspace registrado. Las APIs directas que no
reciben una raíz de scratch mantienen su aislamiento de compatibilidad y no
habilitan mantenimiento sobre el corpus.

### Auditoría histórica explícita

**IMPLEMENTADO Y VERIFICADO EN LA RELEASE `0.14.0-a02f6761ece2`:**
`historical-temp` no es un alias del scratch registrado ni un limpiador global.
Requiere una raíz de auditoría absoluta y explícita mediante
`--maintenance-audit-root`; el selector `--root` sigue siendo el corpus y se
rechaza en esta operación. No hay valor predeterminado para la raíz y nunca se
escanea `/tmp` por omisión.

```bash
Neocortex maintenance --scope historical-temp \
  --maintenance-audit-root "/ruta/raiz-historica" --maintenance-json
Neocortex maintenance --scope historical-temp \
  --maintenance-audit-root /tmp \
  --maintenance-max-entries 100000 --maintenance-max-depth 8 \
  --maintenance-json
Neocortex maintenance --scope historical-temp \
  --maintenance-audit-root "/ruta/raiz-historica" \
  --apply --maintenance-json
```

La primera forma es un plan bounded y read-only: no crea la raíz ausente y
observa sólo hijos directos con prefijo `neocortex-` y manifests de nombres
permitidos. Los vecinos sin evidencia quedan fuera del alcance. El plan
publica el envelope `neocortex.maintenance/v1` con `audit=historical-audit`,
`historical_counts`, `historical_bytes` y registros limitados; los bytes son
observaciones aparentes/asignadas del owner, no espacio de disco que se prometa
liberar.

`--apply` no convierte el plan anterior en autorización: el owner vuelve a
escanear y revalida la raíz, el punto de montaje, la identidad de la entrada y
del manifest, permisos, ausencia de enlaces/hardlinks peligrosos y la
atestación de adopción. Una entrada sólo es elegible cuando el manifest de una
aplicación NeoCortex contiene claims exactos de raíz/ruta/identidad, actividad
explícitamente no incierta, estado `completed`, `disposable=true` y una
adopción aprobada con `adoption_id` y digest ligados. La retirada es
descriptor-relative y no-follow; lo desconocido, activo, conservado, ambiguo o
con drift queda intacto y el resultado es `blocked` o `recovery_required`.
Cada efecto escribe primero un receipt bounded fuera de la entrada y sólo lo
marca `applied` después de verificar la ausencia del target; un cierre incierto
queda en recuperación y se reporta en `receipts`.

Para raíces históricas grandes, `--maintenance-max-entries`,
`--maintenance-max-depth` y `--maintenance-max-bytes` permiten una observación
acotada más amplia sin quitar el límite. `status_counts` y `reason_summary`
explican cada bloqueo (`no_manifest`, `permissions_unsafe`,
`activity_uncertain`, `recovery_required`, `bounds_exceeded`, etc.) y
`largest_records` muestra sólo los mayores registros de forma limitada. Si la
cobertura sigue truncada, el resultado no autoriza ningún efecto.

Esta frontera no usa `rm`, `shutil`, KIO ni otro cleaner externo, no abre
SQLite y no recorre ni modifica el corpus. Un plan puede reportar una raíz
explícita no disponible sin crearla; aplicar sobre una raíz compartida como
`/tmp` conserva el gate de propiedad/permisos y no se presenta como éxito.
En modo plan un bloqueo es una observación segura; un apply con elementos
bloqueados, fallidos o en recuperación devuelve salida 2.

### Diagnóstico externo explícito

`external-maintenance` es la entrada D del plan. No es un limpiador: exige una
raíz y una categoría explícitas, sólo lee metadatos bounded y no ofrece
`--apply`.

```bash
Neocortex external-maintenance \
  --external-root "/ruta/externa" \
  --external-category desktop_thumbnail_cache --external-json
```

El envelope `neocortex.external-maintenance/v1` distingue `observed`,
`preserved`, `blocked`, `unknown`, `absent` y `out_of_profile`, e informa
identidad física, owner/procedencia, límites y bytes observados. Categorías sin
owner NeoCortex (caches de aplicaciones, miniaturas KDE, paquetes, journal,
coredumps, sesiones Codex, backups externos, Papelera KIO e históricos no
adoptados) nunca son candidatas ni se modifican.

## Efectos

| Clase | Ejemplos | Efecto |
|---|---|---|
| Consulta | `help`, `status`, `search`, `ask`, `inspect`, `--models-status`, `databases status`, `curate scan` | Lee publicaciones existentes; no recorre corpus ni crea estado |
| Verificación advisory | `curate verify` | Lee archivos regulares y estado publicado; no crea `file_actions` ni modifica el corpus |
| Producción de estado | rutas, Semantic, catálogo, Review refresh, `curate review/decide` | Escribe owners; no modifica originales ni autoriza efectos |
| Grant de autorización | `curate authorize` | Escribe un grant acotado; no aplica ni verifica un efecto físico |
| Descarga | `--models-prepare` | Adquiere modelos de forma explícita |
| Estado destructivo | `state reset`, `databases restore`, `databases purge` con `--apply` | Requiere confirmación, manifest/plan y locks |
| Aplicación grant-bound | `curate apply` | Requiere confirmación exacta y conserva su autoridad independiente |
| Conciliación | `curate reconcile` | Registra evidencia bounded; no reintenta ni modifica corpus |
| Mantenimiento de scratch registrado | `maintenance --scope owned-temp|audit-work` | Plan limitado a `state_directory/scratch`; `--apply` sólo retira scratch propio `completed`, sin KIO |
| Auditoría histórica | `maintenance --scope historical-temp --maintenance-audit-root PATH` | Plan read-only sobre una raíz absoluta explícita; `--apply` sólo retira adopciones verificadas, sin `/tmp` por defecto, cleaner externo, corpus ni SQLite |
| Diagnóstico externo | `external-maintenance --external-root PATH --external-category CATEGORY` | Observación metadata-only bounded; siempre read-only, sin owner implícito, `--apply`, red, SQLite, KIO o sudo |
| Dedupe/corpus Linux | `--dedupe`, `--all --apply` | Backend KIO receipt-bound, igualdad exacta, no-replace y raíz delimitada |

## Consultas cotidianas

```bash
Neocortex help
Neocortex status --scope all
Neocortex search "consulta" --scope personal --limit 20
Neocortex ask "consulta" --scope personal --limit 12
Neocortex ask "¿Qué PDFs están protegidos?" --scope all --json
Neocortex ask "¿Qué errores tienen mis archivos?" --scope personal --cursor TOKEN --json
Neocortex inspect code "consulta" --scope personal
Neocortex inspect lineage IDENTIFICADOR --scope personal
Neocortex review value --scope personal --limit 50
Neocortex --models-status --models-json
Neocortex databases status --json
```

La ayuda contextual reutiliza el contrato de cada subcomando, por ejemplo
`Neocortex help status`, `Neocortex help curate plan` y
`Neocortex help inspect code`; las opciones se consultan en el propio comando
con `--help`.

El contrato de la consulta operativa está descrito en
[KNOWLEDGE_OPERATIONAL_QUERY.md](KNOWLEDGE_OPERATIONAL_QUERY.md).

`personal` consulta las publicaciones del usuario. `all` mantiene owners y
scores separados y reporta cobertura. Ninguna consulta corrige, migra o crea una
base ausente.

Las preguntas explícitas sobre errores, protección, incidencias ZIP o posibles
duplicados se enrutan a los owners diagnósticos mediante `ask`; el resultado
conserva el snapshot, el cursor y la distinción entre archivo, procesamiento,
índice, política y condición documental, sin autorizar acciones.

`review value --refresh` es diferente: avanza una página durable de Review en
Framework. No modifica corpus ni concede autorización.

## Procesamiento de contenido

Las rutas registradas son `pdf`, `docx`, `office`, `archive`, `text`, `audio`,
`video`, `image` y `code`.

### Dedupe físico Linux

```bash
Neocortex --root "$Root" --dedupe --dedupe-json
Neocortex --root "$Root" --dedupe --apply --dedupe-json
```

`dedupe` fuerza comparación exacta de bytes y no carga OCR, Semantic ni
modelos. `--apply` reutiliza el mismo planner y ledger de `--all`, reclama cada
redundante mediante KIO receipt-bound y conserva una restauración no-replace;
no usa `gio`, `unlink` ni vacía la Papelera. El alias `Neocortex dedupe` traduce
al mismo servicio. Un replay sin cambios no repite efectos.

La frontera física agrupa archivos regulares en lotes bounded (máximo 256 o el
límite de argumentos efectivo) y ejecuta una invocación KIO no interactiva por
lote. Cada miembro conserva su claim, receipt y transición de recovery; un
resultado parcial nunca activa un retry individual ciego. La reconciliación de
inventario se publica por lote y los vacíos se concilian después de consumir el
plan de duplicados, para que un sucesor no oculte sus grupos persistidos.

```bash
Neocortex --root "$Root" --route pdf --max-count 25 --strict-exit-codes
Neocortex --root "$Root" --route pdf,docx --max-count 25 \
  --docx-max-count 25 --strict-exit-codes
```

Estas corridas actualizan inventario y owners de contenido. `--route-only` usa
inputs durables y omite inventario, deduplicación, detección y acciones;
`--candidate-run RUN_ID` elige el inventario y `--resume-run RUN_ID` reanuda
fases incompletas.

Cuando se proporciona una raíz explícita para Code, el alcance predeterminado
`projects` exige que esa raíz coincida con un proyecto configurado; si no,
NeoCortex se abstiene antes de crear estado y muestra cómo usar
`--code-project-root PATH` o `--code-scope broad`.

`--all` selecciona todas las rutas registradas, incluida Code, pero conserva el
alcance seguro `projects`: no convierte automáticamente toda la raíz del corpus
en código candidato. Para analizar una copia de un proyecto, registra su raíz
con `--code-project-root PATH`; `--code-scope broad` queda como opt-in explícito
para una exploración amplia y no ejecuta el código observado ni produce
evidencia de validación del repositorio.

La procedencia de Code es sólo una señal explicable, no una prueba legal de
autoría. En el flujo normal `--all`, NeoCortex prepara automáticamente la
limpieza de dependencias, árboles `vendor` y binarios con evidencia fuerte; sin
`--apply` sólo genera el plan y con `--apply` lo ejecuta. La política integrada
es `trash` para esas clases; overrides internos pueden usar `keep`. Artefactos
ambiguos, licencias, contenedores ZIP y miembros virtuales no se mueven. La
firma binaria por sí sola no prueba procedencia: un binario fuera de una ruta
de dependencia/vendor queda intacto. La acción conserva la misma identidad,
revalidación, Papelera KIO y receipt por
archivo que el dedupe.
Las carpetas que el inventario excluye por política (por ejemplo `.venv`,
`node_modules`, `.git` y cachés) no forman parte de esta acción y no se borran;
para una limpieza posterior de una copia concreta habrá que incluirla mediante
una política de inventario independiente.

Ejemplo de una copia controlada:

```bash
Neocortex --root "$Root" --state-directory "$State" --all --apply
```

### Lifecycle durable de `--all` (0.14 instalado)

Una corrida amplia puede fijar un presupuesto global opcional para todo el
lifecycle, no sólo para una ruta o un documento:

```bash
Neocortex --root "$Root" --all \
  --run-max-items 1000 \
  --run-max-bytes 1073741824 \
  --run-time-budget-seconds 900 \
  --strict-exit-codes
```

`--run-max-items`, `--run-max-bytes` y `--run-time-budget-seconds` se persisten
en `neocortex.run-budget/v1` junto con el deadline efectivo. El límite cubre
preflight/inventario, catalogación y deduplicación, las nueve rutas, Semantic y
la publicación lógica. Cada reserva por stage/ruta/unidad es bounded e
idempotente; cancelación, deadline o falta de presupuesto detienen la admisión
antes de cruzar otra frontera de trabajo.

El run publica primero `neocortex.run-manifest/v1` y avanza los stages
`preflight`, `inventory`, `catalog/dedup`, `routes`, `semantic`, `publication` y
`finalize`. Los checkpoints conservan el digest del manifest, root/identidad,
snapshot, configuración, owner heads, presupuesto y último límite durable. El
estado terminal diferencia `complete`, `partial`, `unavailable`, `blocked`,
`cancelled` y `recovery_required`; terminar una ruta no acredita completar el
run.

Consulta y reanudación usan el mismo identificador durable:

```bash
Neocortex --status --status-run RUN_ID --status-json
Neocortex --resume-run RUN_ID --root "$Root" --strict-exit-codes
```

Resume hereda el presupuesto y deadline restantes del run origen, omite rutas y
stages ya completados y reejecuta sólo los incompletos. `pdf` puede declarar
`phase_resume`; una ruta `safe_replay` sólo reusa entradas/publicaciones
durables, y `not_resumable` se rechaza con causa explícita. Root, política,
snapshot, modelo, herramienta, manifest o owner-head drift producen abstención
fail-closed y no una corrida nueva por inferencia. El replay terminal expone
`replayed`/`new_work` sin ocultar trabajo reejecutado.

Semantic pertenece al mismo lifecycle cuando se solicita `--all` o se reanuda
un stage Semantic, pero el Semantic pesado continúa siendo opt-in. Archive,
Code y Video son fuentes Semantic explícitas; `--all` coordina sus rutas de
contenido sin indexarlas automáticamente como fuentes Semantic pesadas. Code
permanece contenido no ejecutable. Si falta Audio/Whisper, FFmpeg, un modelo u
otra herramienta, la ruta o el stage conserva `unavailable`/`blocked` y el run
queda `incomplete`, nunca éxito vacío ni skip silencioso.

`read_run_status`, `lifecycle_status`, API, SDK y MCP deben devolver el envelope
bounded `neocortex.lifecycle-envelope/v1`, con manifest/digest, stages, rutas,
presupuesto, checkpoints, capacidades, recuperación y owner heads equivalentes.
Las consultas son read-only: no inician runs, no reservan trabajo y no crean
estado. MCP no expone ejecución, autorización, aplicación ni mutación. Los
manifests/checkpoints históricos siguen siendo legibles y 0.14 añade campos de
forma compatible.

## Estado y salud

```bash
Neocortex --doctor-platform --doctor-platform-json
Neocortex --status --status-json
Neocortex --state-health --state-health-json
Neocortex --knowledge-status --knowledge-json
Neocortex --semantic-status
Neocortex --code-status --code-json
```

Los comandos distinguen `complete`, `partial`, `unavailable`, `blocked`, schemas
futuros y corrupción. Ausencia de resultados no se presenta como éxito.

`--semantic-status` no tiene un flag JSON paralelo; `--semantic-plan-json` sólo
acompaña a `--semantic-plan`.

El doctor de plataforma separa `paths` canónicos de `effective_paths`, que
incluyen `--root`, `--state-directory` y overrides de entorno. No crea los
directorios que informa, por lo que permite detectar una raíz temporal heredada
del launcher sin abrir el corpus ni producir estado.

## Búsquedas especializadas

```bash
Neocortex --knowledge-search "consulta" --knowledge-json
Neocortex --code-search "consulta" --code-search-mode hybrid --code-json
Neocortex --code-projects --code-json
Neocortex --code-reconstruct PROJECT_OR_ID --code-json
Neocortex --semantic-index image --semantic-max-items 50
Neocortex --semantic-image-calibrate /ruta/calibration.json \
  --semantic-model-cache /ruta/models/fastembed
Neocortex --semantic-search "pink flower" --semantic-search-mode image
```

Los localizadores dependen del productor. Si una ruta no conserva página, celda,
segmento o región, la salida no inventa esa precisión.

La búsqueda visual usa CLIP local sólo cuando existe una calibración durable
compatible con el modelo, el pipeline y el `processing_signature` de la
generación de imágenes publicada; sin ella, o ante deriva de cualquiera de
esos contratos, la consulta se abstiene y declara la razón. El archivo de
calibración local conserva una muestra de 20–50 `sample_item_ids`, al menos
tres `positive_queries` con `expected_item_ids` y tres `negative_queries`, por
ejemplo:

```json
{
  "schema": "neocortex-image-retrieval-calibration/v1",
  "sample_item_ids": ["item:image:..."],
  "positive_queries": [
    {"query": "pink flower", "expected_item_ids": ["item:image:..."]}
  ],
  "negative_queries": ["a mountain landscape"]
}
```

La calibración se escribe en el owner `semantic.sqlite3`, no en el corpus ni
en un proveedor remoto, y la salida de búsqueda muestra el umbral, la muestra,
la firma y los candidatos rechazados por debajo del piso medido.

## Compatibilidad plana de curación

```bash
Neocortex --curation-preview 50 --curation-json
```

La vista es bounded y read-only. Reúne planes ya publicados de duplicados,
organización y archivos vacíos, junto con identidad, reasons y cobertura.
`curate scan/plan/verify/review/decide/authorize/apply/reconcile` es la interfaz
humana estructurada; apply sólo puede ejecutar efectos mediante un backend
inyectado y contenido de fixture. Consulta
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).

## Bases de datos

```bash
Neocortex databases status --json
Neocortex databases backup --backup-directory "$Backup" --json
Neocortex databases restore --backup-directory "$Backup" --json
Neocortex databases purge --json
```

`backup`, `restore` y `purge` muestran preview por defecto. Escribir exige
`--apply`, la confirmación literal que muestra `--help`, epoch/manifest o digest
del plan según la operación. Restore publica desde staging; purge crea primero
su backup verificable. Consulta [RECOVERY.md](RECOVERY.md).

## Reset selectivo del estado

El comando canónico para limpiar estado local es `Neocortex state reset`. Es una
operación distinta de `databases purge`: permite escoger cuánto estado derivado
se retira sin tocar el corpus. El alcance es obligatorio y sólo acepta uno de
estos valores:

| `--scope` | Alcance | Conserva fuera del alcance |
|---|---|---|
| `runs` | Ledger de ejecución de Framework y sus datos de lifecycle expresamente ligados a ese ledger | Owners SQLite de contenido, caches y metadata de publicación no ligada |
| `runs-and-caches` | `runs` más todos los owners SQLite derivados registrados, incluidos sus WAL/SHM/journal, y la metadata de publicación necesaria para que el estado quede coherente | Artefactos no-SQLite gestionados por `all`, archivos desconocidos y backups externos |
| `all` | `runs-and-caches` más los artefactos no-SQLite gestionados del directorio de estado | Corpus, releases, modelos, backups externos y backups canónicos de migración del catálogo |

El plan enumera targets, conteos, bytes, referencias cruzadas, locks/fences y la
estrategia de continuidad de identificadores. Si una referencia, writer,
publicación pendiente, schema o cambio concurrente impide garantizar el alcance,
el reset se abstiene; no borra filas de Review, recovery o curación por
inferencia. Las tres variantes preservan los originales del corpus.

Aunque estén dentro de `State`, los backups canónicos de migración del catálogo
con nombre `document_catalog.sqlite3.pre-vN-to-vN+1-<timestamp>.sqlite3` se
conservan con su sidecar asociado, incluido el receipt JSON homónimo
(`...sqlite3.json`) y cualquier sidecar SQLite que pertenezca al mismo backup.
Esta excepción sólo aplica a ese patrón canónico: una SQLite desconocida o una
SQLite de `recovery`, `restore` o `staging` (con sus sidecars) sigue bloqueando
el reset con abstención fail-closed.

El modo predeterminado es read-only y sólo produce un plan con digest. No crea
el backup ni modifica SQLite, sidecars, epoch, journals o artefactos gestionados:

```bash
State="$HOME/.local/state/Neocortex/state"
Neocortex state reset --state-directory "$State" \
  --scope runs --json
Neocortex state reset --state-directory "$State" \
  --scope runs-and-caches --json
Neocortex state reset --state-directory "$State" \
  --scope all --json
```

Para aplicar, el uso normal es `--yes`: el adaptador obtiene un preview nuevo,
enlaza su `plan_digest` internamente y confirma sólo ese plan. No crea un backup
persistentemente salvo que se indique `--backup-directory`; el staging temporal
se elimina tras éxito o rollback verificado. El motor verifica de nuevo el plan,
toma locks exclusivos y deja un estado conciliable ante fallo, sin retry ciego:

```bash
Neocortex state reset --state-directory "$State" --scope runs \
  --backup-directory "$HOME/.local/state/Neocortex/state-reset-backups/runs-20260911" \
  --plan-digest PLAN_SHA256 --confirm-state-reset RESET_STATE \
  --apply --json
```

`--backup-directory` debe ser absoluto, nuevo y estar fuera de `State`; nunca se
usa una ruta dentro del estado que se va a limpiar. La forma legacy con
`--confirm-state-reset RESET_STATE` y `--plan-digest` se mantiene para integradores.
En una tubería o sesión no-TTY, `--apply` sin `--yes` se rechaza con una
instrucción concreta. El digest se liga a la raíz, alcance, fingerprints, epoch,
referencias y límites efectivos; si cualquier dato cambia desde el preview hay
que generar otro plan.

La salida JSON usa `neocortex.state-reset/v1` y distingue `preview` de
`applied`, `read_only`, `scope`, `plan_digest`, backup/manifest, conteos y errores.
Un resultado `applied` sólo acredita el reset local; no acredita una nueva
corrida, release instalada, reconstrucción del corpus ni promoción de modelos.
Para restaurar/conciliar usa el manifest del backup cuando se haya solicitado y
[RECOVERY.md](RECOVERY.md).

## Modelos y GUI

```bash
Neocortex --models-status --models-json
Neocortex --models-prepare --models-json
Neocortex --ui
```

`--models-status` es local; `--models-prepare` puede descargar. La GUI consume los mismos
contratos y mantiene deshabilitados los efectos de corpus en Linux.

La inspección o preparación puede limitarse al modelo solicitado, sin exigir
todos los modelos productivos:

```bash
Neocortex --models-status --models-json --models-root /tmp/models \
  --models-model-id jinaai/jina-embeddings-v2-base-es
Neocortex --root /tmp/corpus --state-directory /tmp/state --route audio \
  --whisper-model small --audio-model-cache /tmp/models/whisper
```

`--models-model-id` es repetible; omitirlo conserva la selección completa. La
raíz explícita contiene `fastembed/` y `whisper/`, y los procesadores usan
`--semantic-model-cache` y `--audio-model-cache` respectivamente. Semantic
requiere el layout de caché Hugging Face con `refs/main`, revisión y
`snapshots/<revisión>/`: pesos, tokenizer y configuración originales se validan
juntos y participan en la procedencia. Whisper también admite una carpeta
directa con `model.bin`, `config.json` y `tokenizer.json`; omitir el tokenizer
no autoriza una descarga de fallback. Los IDs Jina, MiniLM, CLIP y Whisper no
se sustituyen por modelos de prueba.

Procesar o consultar offline no prepara modelos: sin backend o archivos locales
la capacidad declara el requisito ausente. Backend presente, archivos presentes
y procesamiento comprobado son estados distintos. `--ui --help` no requiere
Qt; iniciar la UI sí requiere el extra `ui` y bibliotecas de plataforma.

## MCP local

```bash
Neocortex agent serve
```

El servidor stdio expone 15 herramientas: consultas read-only como status, search, context,
`lifecycle_status`, `content_diagnostics`, `operational_query`, `evidence`,
`inspect_code`, `lineage`, `asset_health`, `curation_plan`, `curation_scan` y
`curation_verify`. También expone `curation_review` y `curation_decide`: pueden
escribir únicamente eventos advisory de ReviewTask, están marcadas como no
destructivas y mantienen `actions_authorized=false`. `evidence` puede recibir
`evidence_id` y `expected_snapshot_id`; ningún tool aplica acciones de corpus.
MCP no expone `curation_authorize`, `curation_apply`, `curation_reconcile` ni
`curation_restore`: el actor autenticado que podría emitir un grant no está
resuelto y no se acepta un nombre aportado por el agente como sustituto.

## Salida estructurada y códigos

Los modos JSON/JSONL conservan un `schema`, la operación, cobertura, errores y
warnings cuando el contrato los produce, mientras los campos de scope, epoch y
contadores dependen de la superficie consultada. Curation coloca cursor e items
en `page`; review añade `publication`, y decide devuelve el evento e
`idempotent`; authorize devuelve `grant`, sus efectos state-only y
`physical_effect_applied=false`. No se presentan campos que el contrato no entregue. Los códigos
exactos pertenecen al comando y su ayuda; como regla:

- `0`: operación solicitada completada dentro de la cobertura declarada;
- `2`: uso inválido, abstención operativa, fallo tipado de inventario/ruta/SQLite
  o cobertura incompleta bajo modo estricto;
- `5`: cambió el snapshot, digest o event head esperado;
- `7`: estado corrupto;
- `130`: cancelación mediante interrupción;
- otros códigos no se normalizan a éxito y deben conservar su diagnóstico.

Los fallos tipados durante procesamiento no continúan la etapa Semantic de
`--all` ni imprimen un resumen de éxito. Con `NEOCORTEX_PROGRESS_STREAM=1`, el
evento terminal `NEOCORTEX_PROGRESS` de `framework/result` declara `finished=true`
pero `completion=incomplete`, con `status=failed` o `cancelled`, código de salida,
causa acotada y rutas fallidas. Que termine una fase no acredita el éxito global;
los errores inesperados conservan su propagación y diagnóstico.

No uses la ausencia de traceback como prueba de completitud. Para procedimientos,
límites y replay consulta [OPERATIONS.md](OPERATIONS.md).
