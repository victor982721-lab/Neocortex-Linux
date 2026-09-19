# Persistencia

## Alcance

NeoCortex conserva proyecciones y evidencia autoritativa en owners SQLite
independientes. Los archivos originales son la fuente primaria del contenido,
pero no reconstruyen decisiones, autorizaciones, acciones, correcciones ni toda
la evidencia de recuperación. La reconstruibilidad se determina por tabla,
referencias y procedencia; no por la extensión SQLite o por llamar caché al owner.

El resultado de reset distingue owners seleccionados, transformados y bases
retiradas o preservadas. `verified` conserva su alcance histórico de targets,
indicado por `verification_scope=selected_targets`. El contrato adicional
`owner_verifications` demuestra por owner la conservación de autoridad,
referencias válidas y ausencia de selección operacional anterior. Sólo un
`all` con observación completa, sin bloqueos y todas esas postcondiciones
satisfechas anuncia `operational_freshness=fresh`; los scopes parciales usan
`not_assessed`. `database_count` y `cache_count` siguen contando selección.

`STATE_STORE_REGISTRY` expone `lifecycle_policy_version` y `lifecycle_rules`.
Cada tabla admitida declara su rol, fuente de reconstrucción, retención,
selector de dependencias, frontera de durabilidad y acción de reset. El mapa
versionado es único; reset deriva de él sus políticas protectoras. Una tabla
sin regla bloquea `all`, aunque esté vacía. Los trece SQLite conservan su
distribución física y Knowledge sigue componiendo snapshots de esos owners.

Un reset sin `backup_directory` utiliza una copia transitoria de rollback. Antes
de adquirir esa área escribe un intento inmutable en `state-reset-operations`
y lo registra en `artifacts` con owner `state-reset`. Su identidad, fases,
digest del recibo y promociones se enlazan de forma durable. La CLI puede
conciliar ese intento desde otro proceso, sin explorar directorios temporales.
Un fallo de limpieza posterior a efectos verificados conserva
`applied-cleanup-pending`; un cambio ajeno durante rollback conserva sus bytes,
el raw y `recovery_required`. Antes de retirar un claim, reset registra su
compensación con el productor. Tras un rollback, esa API valida el intento,
el sello original y las promociones exactas antes de restituir estado, metadata,
dependencias y binding del claim. Un claim legado sin enrolamiento, una copia
incompleta o una identidad ajena mantiene `recovery_required`.

La raíz predeterminada es:

```text
${XDG_STATE_HOME:-~/.local/state}/Neocortex/state
```

No se copian bases de Windows ni se reinterpretan identidades NTFS en Linux.

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
| code | `code.sqlite3` |
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

Code v9 añade bloques derivados compartidos y manifiestos completos para sus
generaciones v2. La migración preserva el ledger v1 y los lectores de Knowledge
aceptan las formas exactas v7/v8 sin migrarlas. Reset clasifica las seis tablas
nuevas como derivadas; los heads y localizadores históricos conservan su política
operacional y de retención.
La evidencia de validación de bloques dentro de una publicación es transitoria:
su presupuesto contabilizado es de 16 MiB y la reutilización exige una nueva
comparación exacta de filas y tipos. No cambia el esquema, la autoridad de las
tablas originales, los digests ni la validación de lectores de generaciones.

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

## Reset selectivo

El motor `neocortex.persistence.state_reset` publica el contrato
`neocortex.state-reset/v1` y coordina un reset explícito con scopes
`runs`, `runs-and-caches` o `all`. El scope no es una etiqueta informativa: forma
parte del digest del plan y de la lista de targets, por lo que no puede cambiarse
entre preview y apply.

| Scope | Owners/artefactos afectados | Invariante de propiedad |
|---|---|---|
| `runs` | Ledger de ejecución Framework y lifecycle asociado, sin borrar por inferencia Review, recovery o curación no ligados | Los owners de contenido y sus heads permanecen intactos |
| `runs-and-caches` | Todos los owners SQLite del registro, sus sidecars y metadata de publicación necesaria (`epoch`/journal/manifests administrados) | La frontera cross-owner se retira como conjunto lógico, no como purga aislada de un archivo |
| `all` | `runs-and-caches` más artefactos no-SQLite administrados por el estado | Sólo se alcanzan rutas registradas; corpus, releases, modelos y backups externos quedan fuera |

`runs` debe mantener la continuidad de IDs y procedencia: no reutiliza un ID que
pueda seguir referenciado. Si se compacta el ledger, el plan conserva un high
water mark/tombstone o un mecanismo equivalente de asignación futura y reporta
las referencias cruzadas que impidan retirar una fila. Ningún reset convierte
una referencia histórica en autoridad nueva.

`runs-and-caches` transforma o retira coordinadamente owners y metadata de publicación;
no abre una transacción SQLite distribuida ni simula que varios archivos son una
sola base. El motor toma los locks de writers/publicación, registra baseline y
postcondición, y sólo declara `complete` después de verificar el conjunto. WAL,
SHM y journals siempre se tratan como parte del owner correspondiente. Para
inspeccionar un owner grande durante preview, el layout residual exacto
`-wal=0`/`-shm=32768` evita copiar el main completo sólo cuando la prueba de
locks de sólo lectura y el fence demuestran quiescencia; un owner activo o
ambiguo sigue la ruta de snapshot acotado o se bloquea si rebasa el presupuesto
canónico.

En `all`, un `recovery_required` del Framework se conserva dentro del owner
staged y se informa como `preserved_recovery_action_ids`; no autoriza reintentar
ni descartar la evidencia. Runs, fases y acciones `started`/`applying` continúan
siendo una frontera activa que bloquea la aplicación.

Framework, Inventory y Catalog se transforman en staging aun cuando sólo sea
necesario conservar el suelo de identidades. Inventory preserva planes,
evidencia y scans padres, y retira checkpoints y heads vigentes. Catalog
preserva generaciones publicadas, documentos, manifests, ancestros,
correcciones e historia, y vacía `catalog_publications`. Framework conserva
Review, autorizaciones y acciones, incluidos sus padres de recuperación.

La barrera `neocortex.operational-reset-barrier/v1`, ligada al `plan_digest`,
impide seleccionar o reanudar identidades anteriores. Los nuevos scans, runs y
generaciones se asignan por encima del mayor ID retirado; una consulta histórica
explícita conserva su significado. Las versiones Framework 23, Inventory 14 y
Catalog 11 introducen el contrato de lector mediante migraciones de metadata
que validan el schema previo y conservan su DDL. Un lector anterior rechaza el
nuevo fence. Un segundo reset sin trabajo nuevo verifica `no_changes` sin
volver a promover estos archivos.

Los owners Semantic, Text y Code que contengan evidencia, outbox o receipts
autoritativos bloquean antes del primer efecto: no hay transformador de frescura
para esos casos. Los owners exclusivamente derivados pueden retirarse una vez
validados. No se descarta autoridad para desbloquear un reset.
Una referencia de evidencia huérfana, una generación con ancestry no conciliable
o un sidecar nuevo durante la promoción bloquea el efecto; no se convierte en
un target implícito ni se borra para hacer pasar el reset.

`all` recorre de forma acotada la raíz de estado y compone los contratos SQLite,
las rutas canónicas y los claims reales de `ArtifactRegistry`. El digest incluye
observaciones de archivos, identidades, reglas de owner, manifests, pruebas de
reconstrucción, dependencias y la reserva calculada para metadata de recuperación.
Si las promociones de todas las rutas y un temporal no caben en los 65.536 bytes
del registro, el preview muestra la cantidad, el límite y
`reset-recovery-metadata-budget-exceeded` antes de iniciar efectos. La cobertura parcial, los objetos desconocidos,
los claims inválidos o solapados, un ciclo y un consumidor retenido bloquean el
plan antes del primer efecto. Apply vuelve a observar el mismo grafo bajo los
locks de writers y registro y verifica después el inventario resultante.

Archive persiste manifests de procedencia fuera de su SQLite derivado. Cada
salida enlaza el contenedor original, la identidad del miembro, su destino y sus
hashes. Reset sólo retira una materialización cuando reproduce y compara todos
los outputs desde originales supervivientes autorizados; ni la extensión ni el
nombre de carpeta aportan permiso. Los límites de profundidad, miembros, bytes,
ratio y tiempo se aplican a esa prueba. Los contenedores anidados pequeños usan
un spool de memoria de hasta 8 MiB; si se necesita un spool mayor y no hay scratch
registrado autorizado, la prueba se abstiene. Preview no crea scratch en disco.

Los backups se escriben fuera de la raíz y nunca forman parte del target.

La única excepción interna documentada son los backups canónicos de migración del
catálogo: `document_catalog.sqlite3.pre-vN-to-vN+1-<timestamp>.sqlite3` y su
sidecar asociado, incluido el receipt JSON homónimo (`...sqlite3.json`) y los
sidecars SQLite del mismo backup, si existen, se conservan íntegros aun cuando
estén dentro de la raíz de `State`. El patrón y la relación deben ser exactos;
una SQLite desconocida o una SQLite de `recovery`, `restore` o `staging` (con sus
sidecars) bloquea el reset fail-closed.

El preview es read-only y calcula digest, fingerprints, conteos y bytes dentro de
límites bounded. Apply requiere el digest exacto, `RESET_STATE` y una segunda
validación de locks, epoch, heads, schemas, referencias y límites. Sin
`--backup-directory` el motor usa sólo staging/rollback efímero; el backup
durable es opcional y debe ser nuevo, absoluto y externo cuando se solicita de
forma explícita. Ante drift, schema futuro, writer activo o referencia no
conciliable, el motor se abstiene fail-closed. Si una reversión o publicación
quedan inciertas, conserva lo necesario y expone `recovery_required` en vez de
reintentar.

Durante `apply` se conserva además una guardia SQLite de control para cada
owner target desde la revalidación hasta el efecto y la promoción. Un WAL o
journal cerrado puede retirarse como parte del reset, pero un writer que ya
exista o aparezca después del preview no puede competir con el reemplazo: la
guardia aborta antes de borrar o promover. Las tablas no reconocidas, incluso vacías, bloquean la aplicación hasta que
su owner declare una política válida.

La operación no migra ni abre el corpus, no modifica bytes originales y no toca
los directorios de releases/modelos. Una nueva corrida debe volver a crear sólo
las proyecciones que sus writers publiquen; la ausencia temporal de un owner no
se presenta como cobertura completa.

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
- actualizar backup/restore/purge/reset, health, Knowledge y retención;
- probar writer concurrente, WAL/SHM y publicación interrumpida;
- documentar sólo el contrato final, no el transcript de la migración.

Los límites de confianza están en [SECURITY.md](SECURITY.md) y la arquitectura
de publicación en [ARCHITECTURE.md](ARCHITECTURE.md).
