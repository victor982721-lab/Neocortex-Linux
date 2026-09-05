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

## Lectura segura

No abras una base viva con `sqlite3.connect(...mode=ro...)` como si fuera
byte-neutral. SQLite puede crear o tocar `-wal`/`-shm`.

`SQLiteReadSession` ofrece:

- `immutable_strict` para un owner quiescente, sin sidecars activos y con fence
  verificable hasta el cierre;
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

Un WAL vacío con SHM presente no demuestra que no exista un writer. La
selección automática utiliza un snapshot en ese caso, sin retirar sidecars del
origen. Las sesiones `immutable_strict` y las conexiones bare estrictas verifican
el fence al cerrar. Health incluye sidecars huérfanos de owners desconocidos y
aplica un presupuesto cooperativo a SQL y a las etapas de comprobación; no
promete interrumpir de forma forzosa una llamada de filesystem o Python
bloqueada.

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

## Retención

La retención protege heads actuales/anteriores necesarios, builders, leases,
Review, acciones inciertas y referencias cross-owner. Un plan es read-only hasta
que exista journal y autorización explícita. No usa el tamaño de WAL como señal
de borrado ni ejecuta `VACUUM` implícito.

## Checklist de cambio

- actualizar schema, migración y registro de owner;
- cubrir fuente vacía y poblada de cada versión admitida;
- comprobar rollback, foreign keys, integridad y objetos desconocidos;
- actualizar backup/restore/purge, health, Knowledge y retención;
- probar writer concurrente, WAL/SHM y publicación interrumpida;
- documentar sólo el contrato final, no el transcript de la migración.

Los límites de confianza están en [SECURITY.md](SECURITY.md) y la arquitectura
de publicación en [ARCHITECTURE.md](ARCHITECTURE.md).
