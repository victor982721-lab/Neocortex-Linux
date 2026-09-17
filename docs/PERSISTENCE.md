# Persistencia

## Alcance

NeoCortex conserva estado derivado en owners SQLite independientes. Los archivos
originales son la verdad primaria; las bases son proyecciones reconstruibles con
identidad, revisiones y publicaciones.

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

Framework posee workflow: runs, acciones, Review, decisiones y recovery. Catalog
posee clasificaciones y planes documentales. Inventory posee observaciones,
generaciones y evidencia de duplicados. Knowledge es una vista en memoria y no
tiene `knowledge.sqlite3`.

La tranche post-0.13 usa Inventory schema v13 para heads de generación, sucesores
copy-on-write, digests de contenido y heads de plan. Catalog schema v9 añade
manifests de generación con source fence, raíz, política y digests, con triggers
que bloquean UPDATE/DELETE sobre generaciones publicadas. Las migraciones son
aditivas y conservan lectura de v12/v8.

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

`runs-and-caches` elimina de forma coordinada owners y metadata de publicación;
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

Si Inventory conserva resúmenes/grupos/miembros de planes de duplicados o
evidencia de huellas, y si Catalog conserva generaciones publicadas o historial
de clasificación, el reset transforma esos owners en staging, conserva esas
filas y sus padres verificables y compacta el resultado antes de promoverlo.
Una referencia de evidencia huérfana, una generación con ancestry no conciliable
o un sidecar nuevo durante la promoción bloquea el efecto; no se convierte en
un target implícito ni se borra para hacer pasar el reset.

`all` usa un inventario explícito de artefactos no-SQLite gestionados (por ejemplo
manifests, checkpoints o journals administrados) y conserva archivos desconocidos
o externos salvo que una política futura los registre expresamente. Los backups
se escriben fuera de la raíz y nunca forman parte del target del reset.

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
