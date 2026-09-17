# Operación de NeoCortex

Esta guía contiene procedimientos. Los argumentos exactos están en
[CLI.md](CLI.md), los owners en [PERSISTENCE.md](PERSISTENCE.md) y la recuperación
en [RECOVERY.md](RECOVERY.md).

## Cierre operativo vigente

La release `0.14.1` integra dedupe/KIO receipt-bound, el lifecycle `--all`
(admisión, reutilización, reparación y ZIPs), reset/retención explícitos y el
coordinador global adaptativo. La publicación y la instalación se verifican por
separado con `tools/release_linux.py verify`; su receipt es la fuente viva del
SHA, launcher, `current` y rollback. Ningún procedimiento de esta guía aplica
el corpus personal por inferencia.

## Estado e instalación

Los focos locales no certifican que el comando instalado funcione sobre su
estado efectivo. Verifica manifest, launcher, resultados útiles y relanzamiento
desde el SHA final; no uses valores históricos de `current`, rollback o receipts
como estado vivo. Las generaciones experimentales no necesitan rescatarse para
iniciar una nueva `--all`; los originales del corpus permanecen protegidos.

## Preflight

Antes de procesar contenido:

1. confirma la raíz y que no sea un árbol interno de NeoCortex;
2. ejecuta `Neocortex --version` y compara también el `source_sha` del manifest,
   porque dos instalaciones pueden declarar la misma versión;
3. consulta estado y health sin crear cobertura nueva;
4. comprueba espacio, memoria, herramientas externas y modelos necesarios;
5. fija la selección de rutas y, para una corrida amplia, los límites globales
   de items, bytes y deadline;
6. confirma que no existe otro writer sobre los mismos owners desde el namespace
   del host; un `ps` dentro de un sandbox puede mostrar sólo sus procesos.

```bash
Neocortex --doctor-platform --doctor-platform-json
Neocortex status --scope all
Neocortex --state-health --state-health-json
```

No inspecciones SQLite viva con clientes ordinarios. Durante una corrida larga
observa el stream, transcript, proceso y cgroup; espera el estado terminal antes
de abrir owners salvo que una superficie pública garantice una lectura compatible.
La ausencia de WAL no demuestra quiescencia y un fallo de fence no se corrige
borrando sidecars ni sustituyendo el lector por `mode=ro`.

## Inventario federado de máquina

Para observar el control plane del host sin iniciar una corrida de contenido,
usa `machine-inventory`. La consulta predeterminada es metadata-only y bounded:
`HOME`, `/tmp`, cache/configuración XDG y las raíces de estado, datos y corpus de
NeoCortex. No descubre ni recorre `/` por omisión.

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

La consulta JSON entrega un **resumen compacto por defecto**. El resumen
conserva una entrada por raíz efectiva y sus `status`, `reason_code`, conteos,
bytes y razones de truncamiento, incluso si el presupuesto global impide
recorrer esa raíz. `root_count` es el número de raíces seleccionadas, no el de
raíces que alcanzaron a producir registros. Sólo solicita detalle con
`--machine-json=records` (el modo predeterminado equivale a `compact`); la
opción no elimina los límites del escáner ni el tope de serialización. Si la
lista de resúmenes rebasa esa cota, conserva `root_count` e informa
`serialization.root_summaries_omitted` junto con `presentation_truncated`.

El límite de entradas conserva una sola cota global, pero se reparte de forma
justa por raíz. `root_quota_policy` debe ser `equal_fair_share_v1` y
`root_entry_quotas` muestra la cuota efectiva de cada `root_index`; cada fila de
`root_summaries` la repite como `entry_quota`. La cuota dinámica es
`ceil(entradas_globales_restantes / raíces_restantes)` y el sobrante de una raíz
pequeña se devuelve para las siguientes. Una raíz con `entry_quota=0` conserva
su resumen y queda marcada por la frontera global; no se reporta como ausente.
El reparto sólo aplica a entradas. Profundidad y bytes siguen usando sus
límites globales y pueden producir una terminación parcial antes de agotar la
cuota.

Para no mezclar niveles de evidencia, `records_scanned` suma sólo registros de
entradas. `record_status_counts`, `record_category_counts` y
`record_reason_counts` describen exclusivamente esos registros;
`root_status_counts` cuenta una vez el estado de cada raíz. Los mapas históricos
`status_counts`, `category_counts` y `reason_counts` pueden incluir marcadores
de raíz para `absent`, `blocked` o `unknown`, por lo que no deben sumarse con
los mapas `record_*`.

Antes de ampliar una sonda:

1. usa `--machine-root` sólo con rutas absolutas y repítelo por cada raíz que
   quieras incluir; no sustituyas `--root`, que sigue siendo la raíz del corpus;
2. conserva `--machine-max-entries`, `--machine-max-depth` y
   `--machine-max-bytes` dentro de sus techos; el presupuesto es global y la
   salida explica `truncated` cuando se agota;
3. revisa `roots`, `root_count`, `root_quota_policy`, `root_entry_quotas`,
   `entry_quota`, resúmenes de raíz y agregados `record_*`/`root_status_counts`
   antes de interpretar cualquier tamaño; si se pidió
   detalle, revisa además `root_summaries`, `serialization.records_returned`
   y `serialization.records_omitted`;
4. trata `absent`, `blocked`, `unknown` y `out_of_profile` como resultados que
   requieren explicación, no como cero bytes ni como candidatos de limpieza.

El envelope `neocortex.machine-inventory/v1` conserva `read_only=true`,
`reason_summary`, `reason_explanations`, límites efectivos, identidad física, owner/procedencia,
symlink/hardlink, montaje, permisos, `root_quota_policy`, `root_entry_quotas`,
`entry_quota`, contadores `record_*`/`root_status_counts` y bytes observados,
aparentes y asignados.
Los perfiles/categorías separan `neocortex_state`, `neocortex_data` y
`neocortex_corpus` de NeoCortex de `tmp`, `cache`, `config`, `home` y `external`;
ningún perfil prueba por sí mismo que NeoCortex pueda retirarlo. Los estados son
`absent`, `observed`, `preserved`, `blocked`, `unknown` y `out_of_profile`.

En la API Python, `to_summary_dict()` es la proyección equivalente y expone
`root_summaries`, `coverage_metadata` y `omissions` sin `records`; `to_dict()`
queda reservado para el detalle completo solicitado por un consumidor.

No confundas las capas de truncamiento: `scanner_truncated`/`truncated` y
`truncation_reasons` pertenecen al recorrido (por ejemplo `entry_limit`,
`depth_limit` o `byte_limit`); `presentation_truncated` y el objeto
`serialization` pertenecen a la proyección de salida. La omisión deliberada
del modo `compact` se reporta en la faceta de presentación sin implicar por sí
sola `presentation_truncated=true`. Es válido que el escáner quede parcial
mientras el resumen se entregue completo, o que el escáner termine y sólo se
omitan registros por el tope de presentación. El
texto `[contenido omitido por límite]` no sustituye esos campos ni debe usarse
para declarar cobertura.

Interpreta los bytes así: `apparent` es `st_size` (tamaño lógico), `allocated`
es `st_blocks * 512` (bloques asignados reportados por Linux) y `observed` es
la suma usada como crédito del presupuesto bounded. `observed` no equivale a
espacio libre, no deduplica hardlinks y no prueba recuperabilidad. El límite de
bytes es global y puede dejar créditos parciales en la última entrada; conserva
la razón `byte_limit` junto con el resultado.

La sonda usa `lstat`/no-follow, no lee cuerpos ni el payload del corpus, no abre
SQLite (tampoco en modo `ro`), no escribe estado, no usa red, KIO, sudo ni
cleaners y no crea raíces ausentes. Una carrera de filesystem puede producir
`blocked`/`unknown`; no se presenta como snapshot atómico. Un resultado válido
con hallazgos o cobertura truncada conserva exit 0; sólo errores de
configuración/argumentos producen exit 2.

`machine-inventory` no admite `--apply`, `--all`, `--dedupe`, rutas ni
operaciones directas. El próximo gate de acciones es independiente: debe
resolver owner/procedencia, política y selección explícitas, generar preview,
recibir autorización humana, revalidar identidad/topología/actividad junto al
efecto y usar una vía reversible con receipt, postcondición y recovery. No
aplicar manualmente por nombre, antigüedad o tamaño observado.

## Preparación federada de `hygiene`

Esta superficie prepara una vista end-to-end de higiene sin cruzar una frontera
de efecto. En la cohorte actual es **read-only/preview-only**: la operación
construye una respuesta bounded con registry y manifest, pero no escribe owners,
no publica heads, no crea `file_actions`, no cambia el corpus o el estado y
declara **zero deletion**. La ayuda instalada (`Neocortex hygiene --help`) es la
fuente exacta de los selectores y límites; no agregues `--apply` ni un root por
inferencia.

Una consulta mínima y un preview con root explícito son:

```bash
Neocortex hygiene --hygiene-json
Neocortex hygiene --hygiene-preview --hygiene-json
Neocortex hygiene --hygiene-root "/ruta/explicita" \
  --hygiene-max-entries 20000 --hygiene-max-depth 8 \
  --hygiene-max-bytes 1073741824 --hygiene-preview --hygiene-json
```

Los defaults del adapter son 10,000 entradas, profundidad 2 y 1 TiB de bytes.
Los límites del owner y los límites de serialización continúan siendo
independientes; una cota agotada conserva `partial`/`blocked` y su razón. Un
preview sin root explícito mantiene componentes `deferred` cuando la fuente no
puede seleccionarse de forma segura. El envelope es
`neocortex.hygiene/v1`; su `mode` es `plan` o `preview` y siempre declara
`read_only=true`, `effects_enabled=false`, `preview_only=true`,
`deletion_performed=0`, `actions_ready=false`,
`physical_effect_applied=false`, `mutation_authorized=false` y `applied=0`.

### Procedimiento de preview

1. Antes de consultar, confirma la release/SHA y las rutas efectivas. Si se va a
   leer retención o un owner con writer activo, espera su estado terminal; no
   abras SQLite vigilada ni la sustituyas por `mode=ro`.
2. Selecciona únicamente raíces y fuentes permitidas por `hygiene`. Mantén
   separados scratch registrado, retención, inventario de máquina y diagnóstico
   externo. No conviertas `/tmp`, HOME, la Papelera, caches o un backup externo
   en una raíz administrada sólo por aparecer en la federación.
3. Ejecuta el preview con límites explícitos o revisa los defaults efectivos que
   devuelve la ayuda. Conserva en la evidencia de la consulta el registry, el
   manifest, el snapshot/digest, la procedencia por entrada, el owner, el estado,
   la cobertura, los bytes y los motivos de truncamiento/bloqueo.
4. Revisa cada entrada por owner y categoría. `canonical` y `operational` se
   preservan; `rebuildable` sólo expresa una posible receta de reconstrucción;
   `temporary` exige scratch registrado y lifecycle terminal; `cache` sigue
   sujeto al owner y al costo/receta de reconstrucción. Ninguna categoría es un
   permiso de retiro.
5. Separa `observed`, `preserved`, `blocked`, `unknown`, `out_of_profile` y
   `partial`. Una raíz ausente, un manifest faltante, un owner no disponible o
   una cota agotada requiere explicación; no se transforma en cero bytes,
   completitud ni espacio recuperable.
6. Comprueba el invariante de salida: zero deletion, cero `file_actions`, cero
   movimientos/renombres/retiradas y ningún `DELETE`, `VACUUM` o cleaner. Si la
   respuesta no puede demostrar esa frontera, el resultado se conserva como no
   verificable y no se avanza.

La federación no sustituye a los owners. Scratch se consulta desde sus manifests
`neocortex.scratch/v1` bajo `state/scratch`; la retención se toma de sus planes
read-only sin compactar ni podar; `machine-inventory` aporta
`neocortex.machine-inventory/v1` metadata-only; y `external-maintenance` aporta
`neocortex.external-maintenance/v1` sólo cuando el root y la categoría externa
fueron seleccionados explícitamente. Un owner ausente, una categoría sin
procedencia o una entrada con drift se conserva o se bloquea; no se rellena por
nombre, antigüedad, tamaño o recomendación.

Para artefactos registrados, el manifest de fuente usa
`neocortex.artifact-registry/v1` y su claim incluye owner/producer, propósito,
root/path, identidad física, `kind`, `state`, `source_ref`, digest,
dependencias, retención, `disposable`, metadata acotada y `manifest_digest`.
`kind` acepta `canonical`, `operational`, `rebuildable`, `temporary`, `cache` o
`external`; un `state=completed` o `disposable=true` sólo hace visible una
propuesta del owner y no autoriza retirarla. El registry de fuente puede tener
operaciones de registro propias, pero el preview de `hygiene` sólo consume
`plan`/`verify` y nunca registra, actualiza o retira una entrada.

El manifest es una captura de claims y no congela el filesystem. Si una etapa
posterior fuera autorizada, debe revalidar junto al efecto raíz, identidad física,
montaje, permisos, symlink/hardlink, actividad, manifest/digest, owner-head,
política/categoría, límites y bytes. Drift, crecimiento fuera de cota, writer
activo, cambio de schema o resultado incierto producen abstención y dejan la
evidencia para recovery. El preview no es reutilizable como autorización.

### Gates posteriores (no disponibles en esta etapa)

La preparación deja definida, pero no ejecuta, la cadena completa:

| Gate | Qué se valida | Efecto permitido |
|---|---|---|
| `preview` | registry/manifest, owners, procedencia, cobertura e identidad bounded | Lectura únicamente; zero deletion. |
| `review` | decisión humana sobre entradas, evidencia y razones | Ninguno; no autoriza. |
| `authorize` | selección, política, actor, expiración y presupuesto en un grant | Sólo autoridad durable separada; cero efecto físico. |
| `apply` | backend explícito, locks, revalidación fresca y receipt | Futuro; no lo expone `hygiene`. |
| `verify` | postcondición, identidad y receipt | Futuro; no se infiere del retorno del backend. |
| `recovery` | cualquier timeout, drift, ambigüedad o efecto parcial | Preservar evidencia y resolver; no retry ciego. |

No mezcles este flujo con los `--apply` ya existentes de `maintenance`,
`state reset`, curación o `--all`. Esos comandos mantienen su ownership y sus
gates independientes; la federación de `hygiene` sólo prepara información.

## Piloto y regresión acotada

Para una nueva regresión acotada, usa una raíz que contenga sólo 20–50 elementos
autorizados y cerca la corrida completa a 10–15 minutos. `--max-count` limita
PDFs, no el inventario común; el timeout por documento tampoco es un deadline
global. Para PDF:

```bash
Root="$HOME/Documentos/NeoCortex/Pilot"
test -d "$Root" || exit 2
Neocortex --root "$Root" --route pdf --max-count 25 --strict-exit-codes
```

Registra ruta, versión, exit, tiempo, elementos elegibles/procesados, errores,
cache hits y throughput. Corrige el primer bloqueo antes de ampliar rutas.

Ejecuta el mismo comando por segunda vez. El replay debe mostrar qué se reutilizó
y qué trabajo nuevo quedó, sin ocultar una reejecución como incremental.

### Piloto del lifecycle 0.13 (procedimiento reutilizable; aceptación pendiente)

Este procedimiento sirve para validar una integración desde una raíz temporal,
no para transferir resultados históricos al checkout actual. Usa 20–50 fixtures
heterogéneas, sin abrir el corpus personal ni una SQLite cercada de producción.
Fija límites explícitos y conserva los recibos fuera de `docs/`:

```bash
Pilot="$HOME/Documentos/NeoCortex/Pilot-013"
Neocortex --root "$Pilot" --state-directory "$Pilot-state" --all \
  --run-max-items 1000 \
  --run-max-bytes 1073741824 \
  --run-time-budget-seconds 900 \
  --strict-exit-codes
```

La corrida debe publicar el manifest antes de workers y mostrar los stages
`preflight`, `inventory`, `catalog/dedup`, `routes`, `semantic`, `publication` y
`finalize`. La ausencia de Audio/Whisper, FFmpeg, un modelo u otra herramienta
se registra como `unavailable`/`blocked` y deja `incomplete`; no se corrige
relajando fences ni se presenta como cobertura completa. El stage Semantic se
coordina dentro del run y considera Archive, Code y Video cuando sus owners,
heads y dependencias están disponibles; `--semantic-source` sigue permitiendo
acotar explícitamente el conjunto. Una ausencia afecta la ruta dependiente sin
ocultar las rutas independientes.

## Ampliación controlada

Después de validar un foco, amplía sólo sobre la raíz temporal. `--all` es una
operación amplia, no el primer smoke: selecciona todas las rutas registradas,
incluida Code como contenido, pero en Code conserva el alcance seguro `projects`.
Registra las copias de proyectos que quieras procesar con
`--code-project-root PATH`; sólo usa `--code-scope broad` cuando quieras asumir
explícitamente una exploración amplia dentro de la raíz elegida.
El flujo normal `--all` prepara automáticamente la limpieza de
dependencias/vendor/binarios con señales fuertes; sin `--apply` sólo prepara el
plan y con `--apply` cruza la frontera física. Los artefactos ambiguos permanecen
intactos y los efectos usan la misma frontera KIO receipt-bound que dedupe.

Para reproducir o regresionar el lifecycle 0.14, ejecuta la ampliación sólo
sobre el piloto temporal y prueba las nueve rutas (`pdf`, `docx`, `office`,
`archive`, `text`, `audio`, `video`, `image`, `code`) bajo el mismo presupuesto.
Code no ejecuta el contenido observado. El stage Semantic integrado se ejecuta
con `--all`; sus fuentes pueden acotarse con `--semantic-source` y la preparación
de modelos continúa siendo explícita. `--all` no añade techos globales implícitos;
sus límites globales y los límites por formato son acumulativos cuando se
expresan. La validación C0–C7 y la instalación deben repetirse desde el SHA final
de esta oleada antes de declararse cerradas.

Una corrida sin `--apply` no modifica originales, pero sí escribe inventario,
cachés, planes y publicaciones. Distingue siempre consulta read-only, producción
de estado y efecto sobre corpus.

Las rutas reutilizan extracción válida para reparar FTS y derivados sin repetir
OCR, transcripción o análisis íntegros. Los reintentos sólo proceden con
evidencia estructurada `retryable` y una vez por archivo y corrida; el texto de
un mensaje no es autorización. `--dedupe` usa la misma planificación exacta sin
cargar modelos; `--apply` organiza de forma reversible dentro de la raíz
autorizada.

## Reanudación

Usa el identificador durable de la corrida:

```bash
Neocortex --status --status-run RUN_ID --status-json
Neocortex --resume-run RUN_ID --root "$Pilot" --state-directory "$Pilot-state" \
  --strict-exit-codes
```

Resume usa los inputs y publicaciones durables del run origen, omite stages y
rutas ya completados y reanuda sólo lo incompleto. Hereda el presupuesto y
deadline restantes; no abre una ventana nueva. PDF conserva `phase_resume`,
`safe_replay` exige entradas y publicaciones estables, y `not_resumable` se
rechaza explícitamente. También puede reanudarse sólo el stage Semantic, pero
debe recuperar sus fuentes, selección, modelo, presupuesto y publicación desde
el run origen.

Antes de publicar se revalidan root/identidad, política, snapshot, manifest,
modelo, herramienta y owner heads. Cualquier drift, publicación parcial,
capacidad no reanudable o ambigüedad queda `blocked`/`recovery_required`; no se
reinicia por inferencia ni se marca `complete` por haber terminado otras rutas.
Si se solicitó `--resume-run` y el pendiente corresponde a Semantic, se conserva
el mismo productor, manifest, heads de todos los modelos y presupuesto restante;
no se inventa un presupuesto legacy ausente.
Dos reanudaciones consecutivas deben ser idempotentes y conservar candidatos,
errores y presupuesto restante.

En cambio, repetir **`Neocortex --all`** inicia una petición nueva: no requiere
rescatar la corrida incompleta. El preflight valida raíz/manifest y heads
publicados; abandona el intento pendiente sin fingir rollback y establece un
checkpoint coherente antes del procesamiento. Los budgets explícitos pertenecen
a la nueva petición y se mantienen acumulados dentro de ella, incluyendo el
tiempo del preflight. Se reutiliza lo válido y se reconstruyen los derivados
necesarios, sin mover originales, exigir copias de recuperación de las bases ni
promover generaciones parciales.
Un enlace Code obsoleto tras una interrupción o cambio de archivo se desactiva
una sola vez y se reconstruye en el flujo normal. Schema futuro, manifest ajeno
o corrupto y drift real no se convierten en éxito.

## Watcher

El watcher es foreground y se promueve sólo después de aprobar piloto y replay.
Debe procesar únicamente cambios observables, informar el mecanismo portable y
responder a cancelación cooperativa.

```bash
Neocortex --watch --root "$Root"
```

Cancela una vez y espera el evento terminal. Si el proceso no termina, identifica
su grupo antes de escalar; no mates procesos por nombre genérico.

## Recursos y progreso

`ROUTE_REPLAY` separa trabajo nuevo de observaciones reutilizadas; por ejemplo,
`transcribed` conserva su significado histórico de audios con transcripción y
no implica llamadas nuevas al motor. `ROUTE_COVERAGE` identifica parciales y
errores con el siguiente paso de diagnóstico. Code puede conservar contenido
HTML mediante `generic-lexical-fallback` y declarar estructura parcial: no se
eleva a análisis completo ni se añade un parser para ocultar esa limitación.

Video acota el muestreo por la duración del stream y sus intervalos; un título
no es un fotograma ni recibe un timestamp inventado. Las marcas de muestreo no
afirman cobertura de cada fotograma del video. Los límites por formato y las
fences de lectura/snapshot permanecen independientes de los límites globales
de procesamiento. Una copia de estado grande o con WAL necesita el procedimiento
consistente autorizado; no se amplían sus presupuestos ni se abren owners activos
para sortear una abstención.

Las rutas emiten `ProgressEvent` con fase, completado, total y métricas. La salida
operativa debe mostrar al menos stage/ruta, elementos, bytes, errores, velocidad,
tiempo, presupuesto restante, checkpoint y causa de recuperación. En 0.13 los
límites globales explícitos (`--run-max-items`, `--run-max-bytes` y
`--run-time-budget-seconds`) cubren todo el lifecycle, incluidos inventario,
workers, Semantic y publicación; no se añade un techo global implícito y los
límites específicos de una ruta no se sustituyen ni reinician.

No ejecutes un recorrido largo sin máximo o deadline. Evita un proceso por
archivo y commits SQLite por elemento; usa streaming y batches acotados.

En contenedores Linux los controladores consideran los límites aplicables de
cgroups v2: memoria disponible del host y margen `memory.max - memory.current`
en la jerarquía, además de cuota CPU y afinidad. No se presupone que
`os.cpu_count()` ni `/proc/meminfo` representen los recursos utilizables.

Para subprocesses sin TTY, `NEOCORTEX_PROGRESS_STREAM=1` reutiliza `LineProgress`
en stderr con flush; stdout queda reservado a la salida de la operación.
Indica `--root`, `--state-directory` y, para Code, `--code-project-root`; HOME/XDG
pueden apuntar a un directorio temporal. Los owners crean estado nuevo sin bases
productivas. Consulta las SQLite sólo después del estado terminal, mediante
`SQLiteReadSession` y los contratos públicos de publicación.

Compara cobertura, contenido, errores, procedencia y replay, no bytes idénticos
de bases entre entornos: rutas, tiempos e identidades físicas pueden variar,
mientras backend, versión y fingerprint deben permanecer explícitos. Escoger
`--route text,code` limita expresamente una ejecución, no redefine `--all` ni
convierte una generación parcial en una publicación completa.

El ledger `neocortex.run-budget/v1` reserva por stage/ruta/unidad de forma
idempotente, comprueba el deadline antes de admitir trabajo y antes de cada
transición terminal, y persiste cancelación y consumo. Una ruta filtrada no
reserva todo el snapshot: su adapter estima workload de forma bounded y actualiza
checkpoints cooperativos. `GlobalResourceCoordinator` es el único coordinador
de recursos; no se crea un ledger paralelo por worker.

El estado público usa el envelope bounded
`neocortex.lifecycle-envelope/v1`, compartido por CLI, API, SDK y MCP. Las
consultas no crean runs ni estado. MCP permanece read-only para este lifecycle y
no recibe herramientas de ejecución, autorización, aplicación o mutación.

## Modelos y herramientas externas

```bash
Neocortex models status --json
Neocortex models prepare
```

`status` es local. `prepare` puede usar red y requiere autorización. Tesseract,
FFmpeg/FFprobe y otros binarios se detectan antes de iniciar la ruta;
una ausencia se reporta como cobertura o bloqueo, no como éxito vacío.

Semantic pesado no descarga modelos automáticamente durante `--all`. El selector
integrado considera Archive, Code y Video cuando sus fuentes y heads están
disponibles; `--semantic-source` puede acotar la selección. Un modelo o herramienta
ausente produce `unavailable`/`blocked` y cobertura `partial`/`incomplete`, no
éxito vacío. La preparación de modelos sigue siendo una operación separada,
explícita y autorizada.

## Curación

**CURRENT — consulta:**

```bash
Neocortex --curation-preview 50 --curation-json
Neocortex curate plan --limit 50 --json
```

**IMPLEMENTED — revisión advisory:** toma `plan_digest` como `PLAN_ID`, publica
cada página y decide usando el event head devuelto:

```bash
Neocortex curate review PLAN_ID --limit 50 --json
Neocortex curate decide PLAN_ID ITEM_ID --expected-event-id EVENT_ID \
  --decision resolved --decision-scope until-source-change --actor ACTOR --json
Neocortex curate authorize PLAN_ID --item-id ITEM_ID --action move \
  --actor ACTOR --expires-ns NS --max-bytes BYTES --json
```

Revisa coverage, digest, snapshot, `current_event_id` y efecto declarado. Review
y decide escriben únicamente ReviewTask en Framework; no crean `file_actions`,
no autorizan ni modifican corpus o sistemas externos. Authorize exige items
resueltos, action, actor, expiración futura y presupuesto; persiste un grant
inmutable en Framework. Conserva el `grant_id`, pero no lo interpretes como
receipt: no creó `file_actions` ni aplicó nada. Un digest/event head cambiado
requiere volver a consultar, no reintentar a ciegas. `--json` no exporta ni crea
ZIP, y MCP no ofrece authorize sin actor autenticado.

**IMPLEMENTED sobre fixtures y canaria local:** `curate apply` consume un grant
confirmado y revalida su manifest antes de cada efecto, `curate reconcile`
registra observaciones sin reintentar y `curate restore preview/apply` ofrece
una reversión no-replace con un intent separado. `dedupe --apply` y
`--all --apply` usan KIO receipt-bound con claim same-filesystem; la canaria
privada verifica que las cuotas no borren testigos y que la restauración nativa
sea observable. La interacción visual única de Dolphin permanece como gate
humano independiente.

```bash
Neocortex curate apply GRANT_ID --confirm-grant-id GRANT_ID --json
Neocortex curate reconcile --actor ACTOR --confirm-reconcile --json
```

## Índice exacto derivado de Semantic

Es opt-in y no se activa con `--all`, `--semantic-index` ni al preparar modelos.
Requiere un head textual ya publicado y un directorio destino **nuevo**, de
ruta absoluta y con padre existente, fuera del corpus y de los owners. No
descarga modelos, cambia SQLite, sustituye índices previos ni limpia archivos.
Selecciona la firma registrada del head; el scope debe coincidir con la ruta
de búsqueda que se quiere acelerar (`content` para el cuerpo por defecto).

```bash
Neocortex --state-directory /ruta/estado \
  --semantic-exact-index-build /ruta/derivados/indice-nuevo \
  --semantic-exact-index-model FIRMA_REGISTRADA \
  --semantic-exact-index-scope content --semantic-max-vectors 500000
Neocortex --state-directory /ruta/estado --semantic-search "consulta" \
  --semantic-exact-index /ruta/derivados/indice-nuevo
```

La apertura verifica el artefacto completo contra el owner y tiene costo frío
O(ND), aunque la consulta use menos vectores. La CLI informa esa apertura y los
contadores de uso/fallback; no oculta ese costo dentro de una promesa de consulta
cálida. El API permite amortizarlo con un handle reutilizable:

```python
from pathlib import Path
from neocortex.semantic.semantic_exact_index import open_exact_index
from neocortex.semantic.semantic_search_repository import search_exact_page

database = Path("/ruta/estado/semantic.sqlite3")
with open_exact_index(database, Path("/ruta/derivados/indice-nuevo")) as index:
    page = search_exact_page(database, query, text_scope="content", exact_index=index)
```

`query` es un `ExactSearchQuery` compatible, no texto sin vector. Construcción
y apertura se realizan fuera de `semantic_read_context`; un callback de
cancelación del API debe elevar su excepción, no devolver un booleano.
El artefacto admite como máximo500000 filas y4GB totales. El índice se invalida
si cambia el owner, head, archivo o runtime matemático. Se debe construir otro
destino explícitamente para datos nuevos; no hay rebuild, retención ni poda
automáticos. La ruta no soportada (otros scopes, imagen, varios pares,
diagnósticos dirigidos o batches escalares) usa el scan nativo; un cambio durante
el scoring cancela la consulta sin volver a escanearla. Los límites y cursores
siguen siendo por página; concatenar páginas no equivale a top-K global.

## Mantenimiento de estado

### Scratch registrado (tranche A+B)

**IMPLEMENTADO Y VERIFICADO EN LA RELEASE `0.14.0-a02f6761ece2`:** este flujo sólo revisa
los workspaces privados registrados bajo
`<state_directory>/scratch/<scope>`. Los scopes admitidos son `owned-temp` y
`audit-work`; no se debe proporcionar una raíz de corpus para cambiar el
alcance y no se inspecciona `/tmp`.

La consulta no crea la raíz ausente y no tiene efecto físico. Ejecuta primero
el plan bounded:

```bash
Neocortex maintenance --scope owned-temp --maintenance-json
Neocortex maintenance --scope audit-work --maintenance-json
```

Sólo aplica después de revisar la salida y confirmar que los candidatos son
scratch propio, registrado y marcado `completed`:

```bash
Neocortex maintenance --scope owned-temp --apply --maintenance-json
```

El `--apply` de este comando retira únicamente esos registros y no usa KIO
para scratch interno. No incluye estado derivado fuera de `scratch/`,
`host/.cache`, `.codex`, corpus, releases ni SQLite productiva. La integración
de `--all --apply` ya concilia esta área privada en la fuente verificada; la
release instalada requiere su propio gate de promoción y verificación. Un
`--all` sin `--apply` no cruza ninguna frontera física, aunque puede escribir
estado derivado del lifecycle.

Las rutas integradas de Archive, PDF y video ya apuntan sus temporales de
materialización, recuperación estructural y frames a raíces registradas bajo
`state/scratch`. En éxito el workspace se cierra y retira; en error queda
`failed-retained` para diagnóstico posterior. No se deben sustituir esas raíces
por el corpus, `/tmp` completo ni un directorio compartido.

### Actividad externa determinista sobre el mismo lifecycle

Una actividad local (incluido un proceso de prueba de un agente) usa la API
existente, no escribe manifests a mano ni adopta `.codex`/`HOME` como scratch:

```python
registry = ArtifactRegistry(state / "artifacts", owner="actividad", create_root=True)
scratch = ScratchManager(
    state / "scratch" / "owned-temp",
    owner="actividad",
    create_root=True,
    artifact_registry=registry,
)
workspace = scratch.create(run_id="run-local", retain_on_success=True)
# ejecutar el proceso externo con workspace.path como su directorio exclusivo
# publicar el entregable en otra raíz owner-owned y registrarlo como canonical
workspace.complete((workspace.path / "resultado.bin",), retain=True)
scratch.apply()  # revalida dependencias, identidad y cambios tardíos
```

Una interrupción conserva el workspace `active` o `failed-retained` para
reanudación. El entregable publicado fuera de scratch conserva su propio
registro y no se vuelve desechable por haber sido producido desde allí. El
cierre comprueba el tamaño observado al completar; contenido añadido después
queda bloqueado para recuperación explícita. La CLI `maintenance --scope ...
--apply` compone el registry canónico `<state_directory>/artifacts`; su replay
es un no-op y una consulta no crea raíces ausentes.

### Auditoría histórica explícita

`historical-temp` es una auditoría separada del scratch registrado. La raíz no
se descubre desde configuración: debe pasarse como un `PATH` absoluto con
`--maintenance-audit-root`. No uses `--root` para este flujo y no asumas
`/tmp`; esa ruta sólo puede observarse si se selecciona explícitamente. El
owner no crea una raíz ausente.

Primero captura un plan bounded y revisa su salida JSON:

```bash
HistoricalRoot="/ruta/raiz-historica"
Neocortex maintenance --scope historical-temp \
--maintenance-audit-root "$HistoricalRoot" --maintenance-json
```

Si la raíz es grande, amplía sólo de forma consciente los límites del mismo
comando, por ejemplo `--maintenance-max-entries 100000
--maintenance-max-depth 8`. La respuesta incluye `limits`, `status_counts`,
`reason_summary` y `largest_records`: permite ver cuánto quedó en
`no_manifest`, permisos inseguros, actividad incierta, recovery o cobertura
truncada, sin leer cuerpos ni convertir el tamaño observado en permiso.

El plan es read-only. Sólo enumera hijos directos cuyo nombre empieza por
`neocortex-`, intenta manifests de nombres permitidos y conserva vecinos no
gestionados fuera del alcance. Revisa `status`, `root_exists`,
`historical_counts`, `historical_bytes` y `records`; `unknown`, `blocked`,
actividad incierta, drift, enlaces, hardlinks, permisos inseguros o un manifest
ambiguo no son candidatos. Los contadores de bytes son observaciones bounded,
no una medición de espacio libre del filesystem.

Para aplicar, la nueva invocación debe volver a satisfacer todos los claims;
el plan mostrado no es una autorización reutilizable:

```bash
Neocortex maintenance --scope historical-temp \
  --maintenance-audit-root "$HistoricalRoot" \
  --apply --maintenance-json
```

El owner sólo retira una entrada cuando puede verificar en ese momento la
identidad de raíz/ruta/manifest, la topología de montaje, permisos y una
adopción explícita (`adoption_id` y digest ligados, aprobada, actividad no
incierta, `state=completed` y `disposable=true`). La retirada es relativa a un
descriptor y no sigue enlaces. No se usa `rm`, `shutil`, KIO ni un cleaner
externo; tampoco se abre SQLite ni se modifica el corpus. Una raíz compartida
como `/tmp` puede servir para una observación explícita, pero conserva el gate
más estricto de propiedad/permisos para aplicar y no permite presentar el
resultado como limpieza exitosa.

La aplicación deja un receipt bounded fuera de cada entrada: primero queda
`prepared`, y sólo tras comprobar que el target está ausente pasa a `applied`.
Si hay drift, crecimiento fuera de límites o una falla después de retirar algún
hijo, se conserva el receipt y el resultado exige `recovery_required`.

El plan puede terminar con observaciones bloqueadas sin cruzar una frontera;
un apply con `blocked`, `failed` o `recovery_required` devuelve salida 2 y deja
los elementos para recuperación/decisión posterior. No borres manualmente los
vecinos no gestionados ni conviertas una entrada desconocida en adopción por
su nombre, edad, tamaño o contenido.

### Retención por owner y contabilidad física

La retención C mantiene una sola autoridad por owner: Semantic, Catalog,
Inventory, Framework y Code calculan reachability y protegen heads, builders,
lineage, outbox, referencias cross-owner, checkpoints, planes y decisiones
humanas. El planner común valida schema/FK, bloquea estados parciales o de
recuperación y usa límites de profundidad/nodos; Code e Inventory conservan sus
writers específicos y no se sustituyen por SQL genérico.

`--retention-status` y `plan_retention(...)` siguen siendo **read-only**. Su
salida separa `observed`, `eligible/proposed`, `retired=0` y
`physically_recoverable=unknown`; no ejecuta `DELETE`, `VACUUM`, compactación ni
borra WAL/SHM. Una tabla desconocida, un schema futuro, una referencia huérfana,
un outbox sin floor o un checkpoint/recovery ambiguo bloquea la elegibilidad.
La compactación física sólo puede ser una operación posterior, explícita y
medida con espacio temporal, equivalencia de IDs/FTS y owner quiescente.

### Diagnóstico externo explícito

El frente D se ejecuta sin efectos y con root/categoría suministrados por el
caller:

```bash
Neocortex external-maintenance \
  --external-root "/ruta/externa" \
  --external-category application_cache --external-json
```

La salida `neocortex.external-maintenance/v1` es metadata-only, bounded y
`diagnostic_only=true`. Distingue `observed`, `preserved`, `blocked`, `unknown`,
`absent` y `out_of_profile`; no ofrece `--apply` y no descubre HOME, caches,
Papelera, sesiones Codex, journal, coredumps, backups o miniaturas KDE como
propiedad de NeoCortex. No usa red, SQLite, KIO, sudo ni cleaners externos.

### Preferencias para conservar duplicados

Sobre una muestra autorizada, las preferencias se aplican a las identidades
físicas del inventario seleccionado, no a nombres sin revalidación:

```bash
Neocortex --root /ruta/muestra --dedup-keep /ruta/muestra/original.pdf --show-groups
Neocortex --root /ruta/muestra --dedup-prefer-root /ruta/muestra/preferidos --show-groups
```

Ambas opciones son repetibles y activan inventario y planificación, incluso sin
una ruta de extracción. Escriben estado interno, no modifican el corpus ni
autorizan efectos; no son consultas read-only. Una decisión explícita prevalece
sobre las ubicaciones preferidas, cuyo orden expresa prioridad. Una selección
fuera de la raíz, una identidad cambiada o dos conservaciones incompatibles en
un grupo producen un error, no una elección silenciosa. Las referencias sólo
influyen cuando están verificadas contra su propietario y la publicación
vigente; su ausencia o una comprobación incompleta no prueban prescindibilidad.

### Salud y cobertura

Las comprobaciones tienen alcance explícito y no convierten lo omitido en sano:

```bash
Neocortex --state-health --state-health-scope compatibility --state-health-json
Neocortex --state-health --state-health-owner semantic --state-health-timeout 180 --state-health-json
Neocortex --archive-issues 20 --diagnostics-reason archive_member_count_limit --diagnostics-json
Neocortex --root /ruta/muestra --content-diagnostics 20 --diagnostics-owner all --diagnostics-json
Neocortex --root /ruta/muestra --content-diagnostics 20 --diagnostics-owner text \
  --diagnostics-budget-rows 500 --diagnostics-deadline-seconds 5 --diagnostics-json
Neocortex --review-candidates 20 --review-json
Neocortex --action-recovery-status --action-recovery-json
```

`--state-health-max-owners` y `--state-health-after-owner` permiten continuar una
comprobación acotada; revisa también los owners que requieren reintento. Los
diagnósticos de contenido usan `--diagnostics-cursor` y filtros ligados al mismo
snapshot. Las páginas vacías explican disponibilidad y cobertura; JSONL legacy
se solicita explícitamente con `--review-json-lines` o
`--action-recovery-json-lines`, junto a su selector JSON.

Los presupuestos de snapshots y `KnowledgeReadBudget` se comprueban antes de
copiar o continuar y pueden agotarse; eso no demuestra corrupción. Retención sigue siendo diagnóstico/planificación,
no compactación ni garantía de liberar espacio. Una nueva ejecución y sus
recibos son compatibles con replay, pero no justifican rehacer derivados sin
cambio de entrada. Un piloto detenido a los 15 minutos queda pendiente con su
continuación y no se considera aprobado.

```bash
Neocortex databases status --json
Neocortex databases backup --backup-directory "$Backup" --json
Neocortex databases restore --backup-directory "$Backup" --json
Neocortex databases purge --json
```

Todos muestran preview cuando corresponde. Antes de `--apply`, conserva el
manifest/digest presentado, detén writers y sigue [RECOVERY.md](RECOVERY.md).

### Reset seleccionable de runs y estado derivado

Para limpiar el estado de forma controlada usa `state reset`, no un `rm` manual
ni `databases purge` como sustituto. El alcance debe elegirse expresamente:

```bash
State="$HOME/.local/state/Neocortex/state"
Neocortex state reset --state-directory "$State" --scope runs --json
Neocortex state reset --state-directory "$State" --scope runs-and-caches --json
Neocortex state reset --state-directory "$State" --scope all --json
```

El preview es read-only. Conserva su `plan_digest` y revisa targets, referencias,
epoch, locks/fences, conteos, bytes y límites efectivos antes de aplicar. En
particular:

1. `runs` sólo retira el ledger de ejecución y no debe eliminar Review, recovery,
   curación ni owners de contenido que no estén ligados de forma demostrable.
2. `runs-and-caches` limpia derivaciones de los owners administrados; conserva
   tablas de política, correcciones, Review, autorización y recovery mediante
   reconstrucción staged; WAL/SHM/journal son parte del owner.
3. `all` agrega los artefactos no-SQLite administrados; no convierte archivos
   desconocidos, corpus, releases, modelos o backups externos en targets.

Aunque estén dentro de `State`, `all` conserva los backups canónicos de migración
del catálogo `document_catalog.sqlite3.pre-vN-to-vN+1-<timestamp>.sqlite3` y su
sidecar asociado, incluido el receipt JSON homónimo (`...sqlite3.json`) y los
sidecars SQLite del mismo backup, si existen. La excepción es exacta: una
SQLite desconocida o una SQLite de `recovery`, `restore` o `staging` (con sus
sidecars) mantiene el bloqueo fail-closed y no se elimina ni se adopta como
backup.

Para aplicar el alcance revisado sin crear backup persistente:

```bash
Neocortex state reset --state-directory "$State" --scope runs-and-caches \
  --apply --yes --json
```

Si se desea respaldo, añade un `--backup-directory` nuevo, absoluto y externo al
estado. El motor revalida el preview, snapshot/fingerprints, continuidad de IDs,
referencias, schemas, epoch, locks y límites bounded antes de cambiar archivos.
Si algo deriva, hay un writer activo o una publicación pendiente, se abstiene sin
forzar la operación. Un reset aplicado sin backup sólo conserva staging efímero;
no se reintenta un efecto incierto ni se borra un backup solicitado para liberar
espacio automáticamente.

Después de cualquier aplicación, comprueba que el estado quedó terminal y que
la siguiente ejecución sea nueva:

```bash
Neocortex databases status --state-directory "$State" --json
Neocortex --state-health --state-health-json
```

El resultado sólo prueba la limpieza local declarada. No prueba extracción nueva,
integridad del corpus, instalación de una release o disponibilidad de modelos.

## Instalación y release

Esta oleada no declara una release publicada ni instalada. Antes de usar un
launcher o `current`, comprueba en vivo el SHA de la fuente, manifest, artefacto,
rollback, staging, launcher y árbol limpio; una versión histórica no acredita
el checkout actual.

La [instalación ordinaria offline](LINUX_KUBUNTU.md#instalación-ordinaria-desde-una-extracción)
en venv CPython 3.13 no promueve una release ni requiere Git. El procedimiento
siguiente conserva el contrato de instalación personal CPython 3.14.

La construcción e instalación de paquetes Python es offline y reproducible
desde un wheelhouse local autenticado:

```bash
python3.14 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --wheelhouse "$Wheelhouse" --prepare-models --desktop
python3.14 tools/release_linux.py verify
```

No existe fallback de red para esos paquetes. El wheelhouse contiene
`wheelhouse-manifest.json`, wheels compatibles y hashes. Si falta una dependencia,
la instalación se abstiene; no cambies constraints para sortearla.

`--prepare-models` es una operación adicional explícita que puede adquirir pesos
y requiere su autorización; omítela cuando sólo corresponda usar modelos locales.

Una release termina cuando artefacto, manifest, launcher y `source_sha`
coinciden, el smoke público pasa sin `PYTHONPATH`, el replay es verificable,
staging queda vacío y sólo permanecen `current` y el rollback inmediato.
Conserva además la distinción entre corpus operativo y raíz temporal de smoke;
la semántica de instalación, overrides y verificación está en
[LINUX_KUBUNTU.md](LINUX_KUBUNTU.md#verificación).

## Auditorías técnicas

Una auditoría integral es excepcional. Registra estado vivo, HEAD, alcance,
comando, exit, duración y evidencia; separa hechos, inferencias y no verificado.
Un benchmark compara la misma carga y entorno. Las aceptaciones y releases
anteriores son antecedentes históricos independientes; no certifican la oleada
actual ni una recuperación real de la generación 17 hasta repetir sus gates.

Los informes y salidas brutas viven fuera de la documentación canónica. El
repositorio conserva sólo contratos actuales, roadmap y changelog.
