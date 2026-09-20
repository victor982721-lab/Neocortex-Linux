# Persistencia

## Alcance

NeoCortex conserva proyecciones y evidencia autoritativa en owners SQLite
independientes. Los archivos originales son la fuente primaria del contenido,
pero no reconstruyen decisiones, autorizaciones, acciones, correcciones ni toda
la evidencia de recuperación. La reconstruibilidad se determina por tabla,
referencias y procedencia; no por la extensión SQLite o por llamar caché al owner.

El factory reset sólo informa el efecto local que pudo verificar. Si una frontera
impide completarlo, conserva conteos parciales y un error concreto; la CLI no lo
presenta como completo ni acredita una nueva corrida o la disponibilidad de una
release o modelo.

`STATE_STORE_REGISTRY` expone `lifecycle_policy_version` y `lifecycle_rules`.
Cada tabla admitida declara su rol, fuente de reconstrucción, retención,
selector de dependencias y frontera de durabilidad. El mapa versionado es único;
el factory reset no convierte una tabla desconocida en descartable ni cambia la
autoridad del owner. Los doce SQLite conservan su distribución física y
Knowledge sigue componiendo snapshots de esos owners.

El factory reset no crea backup, snapshot SQL, plan, digest ni receipt durable
adicional. La recuperación de backup/restore y purge mantiene sus propios
manifests y contratos, sin ser invocada por el factory reset. Un bloqueo o una
interrupción se informa con el alcance realmente retirado y requiere una nueva
invocación después de resolver la frontera.

La raíz predeterminada es:

```text
${XDG_STATE_HOME:-~/.local/state}/Neocortex/state
```

El owner de inventario sólo publica observaciones portables Linux; no crea ni
consume cursores de journal de plataforma.

## Registro de owners

`neocortex.safety.state_topology_contracts.STATE_STORE_REGISTRY` define orden,
nombre de base, versión esperada y modo de captura Knowledge:

| Owner | Base |
|---|---|
| inventory | `dedup.sqlite3` |
| framework | `framework.sqlite3` |
| catalog | `document_catalog.sqlite3` |
| pdf | `pdf.sqlite3` |
| docx | `docx.sqlite3` |
| office | `office.sqlite3` |
| audio | `audio.sqlite3` |
| video | `video.sqlite3` |
| image | `image.sqlite3` |
| semantic | `semantic.sqlite3` |
| archive | `archive.sqlite3` |
| text | `text.sqlite3` |

Las constantes de cada schema son la fuente de versión. No dupliques números en
otra tabla ni añadas un owner sin registrarlo y conectar backup, health,
retención y Knowledge.

## Propiedad

Cada owner controla:

- creación y migración de su schema;
- factories de conexión y PRAGMAs;
- transacciones y locks;
- staging/head si es generacional;
- validación e integridad;
- retención de su historia.

El validador estructural compartido reutiliza únicamente tokens SQL inmutables
por texto observado exacto. Su caché limita el almacenamiento contabilizado a
2 MiB y 256 entradas, con un máximo de 4.096 caracteres por entrada. No conserva
aceptaciones de bases ni sustituye lecturas de `sqlite_master`, PRAGMAs o
comparaciones de contratos: un cambio de DDL sigue siendo observable aunque
conserve `schema_version`. Los textos mayores usan el parser acotado habitual.

Framework posee workflow: runs, acciones, Review, decisiones y recovery. Catalog
posee clasificaciones y planes documentales. Inventory posee observaciones,
generaciones y evidencia de duplicados. Knowledge es una vista en memoria y no
tiene `knowledge.sqlite3`.

La tranche post-0.13 usa Inventory schema v13 para heads de generación, sucesores
copy-on-write, digests de contenido y heads de plan. Catalog schema v9 añade
manifests de generación con source fence, raíz, política y digests, con triggers
que bloquean UPDATE/DELETE sobre generaciones publicadas. Las migraciones son
aditivas y conservan lectura de v12/v8.

La publicación portable de Inventory materializa una vez la observación TEMP
completa y validada cuando hay cambios. Calcula su digest antes de tomar el lock
de escritura y revalida checkpoint, generación y revisión al adquirirlo. Catalog
valida replay, digests y recuentos en una transacción de lectura; antes de publicar
comprueba de nuevo la conexión, identidad física, fuente y raíz bajo transacción
de escritura. La adquisición de ambos writers consulta cancelación entre intentos
acotados, sin ampliar el busy timeout configurado. Inventory conserva el coste
de materializar la generación actual; Catalog conserva el cambio atómico de su
proyección y su lock de coordinación dentro del proceso.

Catalog persiste el binding de recursos en el mismo INSERT/UPSERT que la fila
de staging, incluidos los errores de clasificación. Los aciertos reutilizados
copian el binding validado; no requieren una segunda escritura por documento.
La proyección publicada desactiva únicamente rutas retiradas o trasladadas y
conserva el refresco de las observaciones dentro de la transacción. La igualdad
de replay observa todas las columnas por claves únicas y comprueba la cobertura
en ambos sentidos; no guarda aceptaciones entre observaciones.

Dedup lee los metadatos de las pruebas en lotes acotados sobre su misma conexión
y observaciones TEMP actuales. Los recuentos completos y la procedencia del hash
se separan de la muestra limitada de alias, sin ordenar todas sus rutas
ni conservar autoridad después de cambiar el tamaño de candidatos.

## Frontera durable de acciones físicas

Framework configura WAL con `synchronous=FULL`: conserva intentos y recibos de
acciones sobre archivos. Antes de `BEGIN IMMEDIATE`,
`mark_file_actions_applying` comprueba y, si hace falta, eleva `main.synchronous`
a FULL; verifica el valor efectivo y lo conserva para los recibos posteriores.
Un valor inferior, una transacción ajena o un fallo de COMMIT no autorizan el
siguiente efecto físico. La política es por conexión y no cambia el schema.

La escritura de `applying`, su identidad esperada y su evento se confirman en
una única transacción. COMMIT queda dentro del manejo de errores: si falla y
la transacción sigue abierta, se revierte; si falla la reversión, se cierra la
conexión y se conserva la excepción inicial con detalles secundarios. Una
confirmación incierta exige conciliación del estado persistido y del archivo;
no se convierte en un reintento automático de la mutación. FULL solicita la
sincronización al sistema: la resistencia final a pérdida de energía también
depende de que filesystem y dispositivo respeten esa solicitud.

Los owner heads de una publicación se comparan después de
`canonical_owner_heads`: valida tipos, límite y unicidad de owners, y ordena por
owner. El orden de observación no constituye drift. Cualquier cambio de owner,
revision, digest o versión de schema conserva el bloqueo de recuperación.

## Lectura segura

No abras una base viva con `sqlite3.connect(...mode=ro...)` como si fuera
byte-neutral. SQLite puede crear o tocar `-wal`/`-shm`.

`SQLiteReadSession` ofrece:

- `immutable_strict` para un owner quiescente, con identidad y fence
  verificables hasta el cierre. El preflight central considera quiescente
  únicamente uno de estos conjuntos: sin sidecars, o exactamente `-wal` y
  `-shm` regulares, con `-wal` de 0 bytes y `-shm` residual de exactamente
  32768 bytes. En el segundo caso los tamaños no bastan: una prueba de locks
  de sólo lectura y la recaptura del fence deben demostrar que no hay un owner
  activo y que el conjunto no cambió; durante cualquier sesión estricta el
  kernel conserva una guardia OFD compartida sobre los locks de control (también
  para el layout sin sidecars) para cerrar la carrera entre la sonda y la
  lectura;
- `snapshot_temp` para copiar main y sidecars con fence antes/después de la
  copia y reintentos acotados ante drift; si no obtiene un conjunto estable,
  se abstiene con `ImmutableSQLiteUnavailable`.

`writer_coordinated` identifica una frontera que debe aportar el owner writer,
no un modo genérico que esta clase pueda abrir. Un snapshot temporal ya validado
queda desligado de escrituras posteriores del origen; no promete capturar un
owner que cambia continuamente mientras se copian sus bytes.

Los candidatos del pipeline integrado usan una publicación específica del writer
Framework, no ese modo genérico; véase [concurrencia](ARCHITECTURE.md#concurrencia-y-recuperación).

Una consulta pública no crea bases ausentes, no migra y no hace checkpoint. Los
schemas `future`, incompatibles o corruptos producen abstención tipada.

Un WAL vacío con SHM presente no demuestra quiescencia por sus tamaños solos.
La selección automática sólo permite `immutable_strict` para el layout residual
exacto descrito arriba después de la prueba de locks de sólo lectura, la
recaptura del fence y la comprobación de cierre; no retira ni normaliza
sidecars del origen. Un `-wal` no vacío, un rollback journal no vacío, un SHM
aislado o de tamaño inesperado, sidecars adicionales, entradas no regulares o
un lock/owner activo o ambiguo no son evidencia de inactividad. Esos casos
pueden usar `snapshot_temp` únicamente en una ruta que admita una copia estable
y dentro del presupuesto; si no, la operación se abstiene fail-closed. Nunca
se hace una copia temporal ilimitada para superar esa frontera.

Las sesiones `immutable_strict` y las conexiones bare estrictas verifican el
fence al cerrar. Health y retención reutilizan este mismo contrato, incluidos
los sidecars huérfanos de owners desconocidos, y aplican un presupuesto
cooperativo a SQL y a las etapas de comprobación; no prometen interrumpir de
forma forzosa una llamada de filesystem o Python bloqueada.

Las consultas Knowledge pueden recibir un `KnowledgeReadBudget` en memoria para
limitar filas, vectores, bytes temporales, deadline monotónico y cancelación.
Su agotamiento sólo produce cobertura parcial; no crea owners, checkpoints,
cache de resultados ni efectos sobre el corpus.

## Transacciones y publicación

Los writers usan transacciones explícitas y rollback también ante
`BaseException`. WAL no es evidencia de corrupción ni un archivo que pueda
borrarse aisladamente.

Inventory, Catalog, Semantic y otras rutas generacionales construyen staging y
cambian el head sólo al completar. Inventory concilia scans `building`
abandonados mediante `mark_abandoned_scans()` antes de continuar; una exploración
parcial no se publica como vigente.

La publicación cross-owner registra baseline, owners afectados, heads finales y
estado. Los lectores bloquean sólo cuando la transición pendiente intersecta sus
owners. El protocolo ofrece consistencia lógica y recovery, no una transacción
física distribuida entre archivos.

## Identidad y revisiones

En Linux la identidad física usa `st_dev` y `st_ino`; `birthtime_ns=-1` es válido
cuando el filesystem no expone nacimiento. Ruta, tamaño y mtime son observaciones
revalidables, no identidad suficiente.

Las generaciones son append-only respecto de hechos históricos. Un movimiento
futuro publicará una nueva ubicación o overlay; no reescribirá generaciones
pasadas. Las derivaciones ligan recurso, revisión, productor y firma de
procesamiento.

## Migraciones

Una migración:

1. acepta sólo versiones declaradas;
2. rechaza objetos desconocidos o schema futuro;
3. se ejecuta en una transacción;
4. preserva filas legacy sin inventar autoridad ni precisión;
5. valida versión, integridad, foreign keys e índices al terminar;
6. tiene pruebas desde cada versión soportada y rollback ante fallo.

Un campo nuevo no debe convertir evidencia legacy en `verified`. Las filas sin
prueba suficiente se marcan `unverified`, `legacy` o equivalente.

## Backup, restore y purge

NeoCortex sí dispone de operaciones generales `databases backup`, `restore` y
`purge`. Sus manifests conservan owner, schema, bytes, hashes, sidecars, epoch y
heads. El procedimiento y las confirmaciones están en
[RECOVERY.md](RECOVERY.md).

Backup de varios owners puede demostrar un conjunto coherente sólo si mantiene
la coordinación y recaptura los mismos heads. De otro modo debe declararse
`independent_owner_snapshots`.

## Factory reset operativo

La frontera pública para eliminar todo el estado operativo administrado es
`Neocortex --factory-reset`. Trabaja únicamente sobre la raíz de estado
seleccionada y retira las bases SQLite operativas con sus sidecars, las
materializaciones de ZIP administradas bajo ella (incluido
`state/archive-materialized`), las cachés y los metadatos de procesamiento. No
toca destinos externos producidos por APIs standalone. Los ZIP originales y el
resto del corpus, la instalación, los modelos y los `installation-receipts` no
son targets.

```bash
Neocortex --factory-reset
```

`--state-directory` queda disponible como override para fixtures sin seleccionar
el estado productivo por accidente. La operación no lee ni procesa el corpus, no
reconstruye contenido y no cambia schemas, owners ni datos fuera de su raíz.
No existen scopes ni contratos de preview/apply: no crea backup, snapshot SQL,
plan, digest o receipt durable adicional, y no acepta `--apply` ni `--yes`.

La operación toma sus locks y verifica writers, procesos y rutas/montajes antes
de retirar. Los symlinks dentro de la raíz se desvinculan sin tocar sus targets;
no se siguen ni se borran targets externos. Rutas o montajes ajenos, permisos
insuficientes, corrupción, cambios concurrentes y objetos cuya propiedad no
pueda verificarse producen un error con conteos parciales. No se borra un
sidecar aislado ni se relaja la valla para continuar, y la operación tampoco
adopta rutas externas como parte del estado.

Si no se puede retirar todo el estado operacional alcanzable, el error conserva
los conteos parciales y la CLI termina con código distinto de cero; no declara
un factory reset completo. La ausencia posterior de una base no prueba que el
corpus se haya procesado, que una instalación esté vigente o que los modelos
estén disponibles.

## Retención

La retención protege heads actuales/anteriores necesarios, builders, leases,
Review, acciones inciertas y referencias cross-owner. Un plan es read-only hasta
que exista journal y autorización explícita. La inspección de owners SQLite
reutiliza el contrato de lectura segura: un owner grande quiescente con ningún
sidecar, o con el layout residual exacto `-wal=0` y `-shm=32768` probado por
locks de sólo lectura y fence, usa `immutable_strict` sin snapshot temporal
completo. Un owner activo, ambiguo, con WAL/journal no vacío o con sidecars
inesperados sólo puede usar un snapshot estable dentro del límite de
`DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES` (256 MiB); de lo contrario se
abstiene fail-closed. No usa el tamaño de WAL como señal de borrado ni ejecuta
`VACUUM` implícito.

## Checklist de cambio

- actualizar schema, migración y registro de owner;
- cubrir fuente vacía y poblada de cada versión admitida;
- comprobar rollback, foreign keys, integridad y objetos desconocidos;
- actualizar backup/restore/purge/factory reset, health, Knowledge y retención;
- probar writer concurrente, WAL/SHM y publicación interrumpida;
- documentar sólo el contrato final, no el transcript de la migración.

Los límites de confianza están en [SECURITY.md](SECURITY.md) y la arquitectura
de publicación en [ARCHITECTURE.md](ARCHITECTURE.md).
