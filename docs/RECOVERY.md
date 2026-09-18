# Recuperación, backup y rollback

## Principios

- detén writers antes de una operación multi-owner;
- conserva fuente, manifest y evidencia previa;
- valida en staging antes de publicar;
- nunca edites `schema_version` para aparentar compatibilidad;
- una operación incierta se concilia antes de reintentar;
- WAL y SHM pertenecen al estado de la base y no se borran aisladamente.

## Diagnóstico

```bash
Neocortex databases status --json
Neocortex --state-health --state-health-json
```

Registra runtime, root de estado, epoch, owners, schemas, sidecars y writers. Una
base ausente es distinta de una base corrupta o un sidecar huérfano.

## Backup

Usa un destino nuevo fuera de la raíz de estado:

```bash
Neocortex databases backup --backup-directory "$Backup" --json
```

El preview no crea el destino. Para escribir usa los argumentos exactos mostrados
por `--help`, incluidos `--apply`, `--confirm-database-backup BACKUP_DATABASES`,
integrity y, cuando se requiera, `--expected-epoch`.

Un backup válido:

- captura todos los owners registrados y ausencias explícitas;
- incluye `state-backup-manifest.json`;
- registra schema, tamaño, hash, sidecars, epoch y heads;
- valida integridad y foreign keys según el modo solicitado;
- recaptura el estado para detectar un writer concurrente;
- no sobrescribe otro backup.

No uses un script manual con conexiones `mode=ro` sobre el único estado vivo.

## Restore

Primero valida sin publicar:

```bash
Neocortex databases restore --backup-directory "$Backup" --json
```

Restore comprueba manifest, hashes, schemas y compatibilidad en staging para las
entradas que pueden prepararse. La publicación exige `--apply`, el SHA-256 del
manifest y `--confirm-database-restore RESTORE_DATABASES`. El CAS se compara con
el epoch actual esperado; el epoch histórico del backup no se reutiliza como
identidad del destino.

La compatibilidad se comprueba contra los contratos del owner, no sólo contra
hashes e integridad SQLite; schemas futuros, desconocidos o sin una ruta
compatible demostrada se rechazan antes de sustituir el destino. Las
migraciones admitidas se preparan en copias descartables, nunca en el único
backup.

El evento `complete` durable es el punto de commit, por lo que un fallo posterior
del puntero de epoch no autoriza revertir los archivos instalados. Antes de ese
punto, una reversión se cierra sólo después de comprobar los owners previos y
registrar el abort. Si la publicación o la reversión quedan inciertas, se
informa `recovery_required` y se conservan las rutas de staging, rollback y
backup previo necesarias para conciliación, sin reintentar efectos a ciegas.

Después verifica:

```bash
Neocortex databases status --json
Neocortex --state-health --state-health-json
```

Las entradas `absent` quedan registradas en el manifest, pero el consumidor
actual no las convierte automáticamente en eliminación de una base existente,
por lo que no debe presentarse todavía como restore completo de ausencias. La
resolución identity-bound de ese caso permanece como gate de persistencia antes
de autorizar una publicación que pueda retirar un owner.

## Purge

```bash
Neocortex databases purge --json
```

El preview enumera únicamente bases canónicas y sidecars. Aplicar requiere el
digest exacto del plan, confirmación literal y backup verificado. No toca corpus,
releases, modelos ni evidencia externa. Usa el mismo motor de backup que restore
puede consumir; un manifest de purge no se presenta como un backup general si su
contrato difiere.

## Reset selectivo de estado

`Neocortex state reset` coordina el borrado de estado derivado con tres alcances
mutuamente excluyentes. El comando siempre empieza en preview; no hay que
interpretar la aparición de un plan como un borrado:

| Alcance | Se retira | No se retira por este alcance |
|---|---|---|
| `runs` | Ledger de runs de Framework y lifecycle expresamente asociado | Owners de contenido, caches y metadata de publicación no ligada |
| `runs-and-caches` | `runs`, todos los owners SQLite registrados con sus sidecars y la metadata de publicación necesaria para mantener coherencia | Artefactos no-SQLite gestionados, archivos no reconocidos y backups externos |
| `all` | `runs-and-caches` y artefactos no-SQLite gestionados | Corpus, releases, modelos y backups externos |

Para `all`, los backups canónicos de migración del catálogo que ya existan bajo
`State` tampoco son targets: se conservan
`document_catalog.sqlite3.pre-vN-to-vN+1-<timestamp>.sqlite3` y su sidecar
asociado, incluido el receipt JSON homónimo (`...sqlite3.json`) y los sidecars
SQLite del mismo backup, si existen. Esta excepción no cubre otras SQLite: una
SQLite desconocida o una SQLite de `recovery`, `restore` o `staging` (con sus
sidecars) mantiene la abstención fail-closed.

En `all`, las filas `recovery_required` del Framework se conservan dentro del
owner staged y se reportan como evidencia preservada; no se reintentan ni se
descartan. Sólo runs/fases activas o acciones `started`/`applying` mantienen el
bloqueo del apply.

Antes de cualquier aplicación:

```bash
State="$HOME/.local/state/Neocortex/state"
Neocortex databases status --state-directory "$State" --json
Neocortex state reset --state-directory "$State" --scope all --json
```

El segundo comando devuelve `neocortex.state-reset/v1`, un `plan_digest`, targets,
conteo/bytes, referencias, epoch, locks/fences y límites efectivos. Lee estado
sin crear backup, migrar SQLite, eliminar sidecars ni escribir epoch/journal. El
plan debe revisarse para confirmar que el alcance es el deseado y que no hay
referencias cruzadas, runs activos, publicaciones pendientes, schemas no
compatibles o writers en curso. Un bloqueo se conserva como abstención; no se
resuelve borrando el lock o ignorando el fence.

La aplicación usa rollback efímero y requiere el mismo alcance y digest del
preview, el límite no excedido y el token literal. No se crea un backup durable
si no se solicita `--backup-directory` de forma explícita. Para el uso ordinario
no interactivo, `--yes` enlaza un preview fresco con su digest:

```bash
Neocortex state reset --state-directory "$State" --scope all --apply --yes --json
```

Cuando se necesita conservar una copia externa, el backup sí se pide de forma
expresa:

```bash
Neocortex state reset --state-directory "$State" --scope all \
  --backup-directory "$HOME/.local/state/Neocortex/state-reset-backups/all-20260911" \
  --plan-digest PLAN_SHA256 \
  --confirm-state-reset RESET_STATE --apply --json
```

El preview reserva de antemano la capacidad de metadata necesaria para las
identidades de todas las posibles restauraciones y su temporal. Expone
`recovery_metadata.required_bytes` y `limit_bytes` (65.536 bytes). Si la reserva
no cabe, `blocked_by` contiene `reset-recovery-metadata-budget-exceeded` y no
se inicia ningún efecto. La cantidad depende de los paths y sus identidades,
no de un número fijo de archivos. Para un estado mayor se pueden usar los scopes
permitidos `runs` o `runs-and-caches` cuando ése sea el objetivo, y la retención
propietaria de artefactos para su limpieza; no se omite el límite para hacer
pasar `all`. La recuperación de 400 archivos en un único reset no está soportada
por esta política de metadata.

El destino del backup explícito debe ser absoluto, nuevo y externo a `State`; el
reset no reutiliza ni limpia backups existentes. El motor vuelve a comprobar fingerprints,
epoch, locks, referencias, límites de archivos/bytes e integridad antes de
publicar el cambio. Si el estado cambió desde el preview, falta confirmación o
el backup no puede verificarse, aborta sin retirar targets.

El manifest del backup conserva procedencia, hashes, sidecars, heads y, según el
alcance, los artefactos no-SQLite gestionados. Un fallo antes del commit intenta
rollback desde el staging/backup verificado; si el rollback o la frontera de
publicación quedan inciertos, el resultado es `recovery_required` y se conservan
las rutas necesarias para conciliación. No se reintenta a ciegas ni se presenta
un manifest extendido de `all` como si fuera un backup general de
`databases restore`.

Cada aplicación publica un `operation_id` antes de crear su staging. Si termina
con `recovery_required`, conserva ese identificador y el recibo verificable.
Desde una nueva sesión se puede inspeccionar la acción de conciliación:

```bash
Neocortex state reset --state-directory "$State" --scope all \
  --reconcile-operation OPERATION_ID --json
```

El preview devuelve `receipt_digest`, `phase`, `action` y la ubicación exacta
registrada. La conciliación requiere ese digest y la confirmación explícita:

```bash
Neocortex state reset --state-directory "$State" --scope all \
  --reconcile-operation OPERATION_ID --receipt-digest RECEIPT_SHA256 \
  --confirm-state-reset RESET_STATE --apply --json
```

Un intento sin efectos sólo permite limpiar su área transitoria verificada. Un
reset aplicado y verificado sólo requiere cerrar su limpieza pendiente. Si el
proceso murió entre efectos, se restaura exclusivamente desde el raw íntegro a
rutas ausentes, archivos idénticos al original o promociones propias registradas.
Una ruta nueva o modificada por otra persona conserva sus bytes y mantiene
`recovery_required`. La restauración usa publicación sin reemplazo y vuelve a
comprobar la identidad antes de actuar. Repetir una conciliación cerrada devuelve
`no_changes`; el comando no busca staging por nombre ni recorre `/tmp`.

Si se restauraron bytes después de retirar un claim de `ArtifactRegistry`,
la conciliación invoca la compensación de su productor. Sólo los claims
inscritos antes del efecto pueden restituirse mediante el sello original, la
identidad de la operación y las promociones exactas de archivos y directorios.
Los claims legados sin ese enrolamiento mantienen `recovery_required`; volver
a crear una ruta no reactiva un tombstone. Una segunda interrupción durante la
restauración conserva los nombres e identidades de sus temporales; al reanudar
se retiran exclusivamente esos temporales registrados.

Los owners Semantic, Text y Code con evidencia autoritativa también se abstienen
antes del reset por falta de un transformador de frescura. Ninguno de estos
bloqueos se resuelve borrando receipts o quitando su protección.

Después de un resultado `applied`, verifica el resultado local:

```bash
Neocortex databases status --state-directory "$State" --json
Neocortex --state-health --state-health-json
```

Para un `all` verificado, `operational_freshness=fresh` incluye comprobaciones
por owner y cobertura del inventario; los contadores distinguen transformación,
retiro físico y preservación. La barrera conserva historia y padres de evidencia,
pero impide reanudar sus identidades. La siguiente corrida debe ser nueva y
producir sus propios manifests/heads por encima del suelo de IDs retirados. Un
reset no reconstruye el corpus, no instala releases ni modelos y no demuestra
que una ejecución futura haya terminado. Los originales y los backups externos
permanecen fuera de estos tres alcances.

## Corridas interrumpidas

Consulta el run y sus fases. Reanuda sólo cuando inputs, firma de procesamiento y
owner son compatibles. Un worker tardío no puede publicar sobre un head nuevo.

Inventory marca scans `building` abandonados antes de una nueva coordinación.
Las generaciones incompletas permanecen invisibles y pueden podarse sólo cuando
ningún head, lease o referencia las protege.

## Acciones inciertas

El lifecycle mínimo distingue:

```text
planned → authorized → prepared → applying
        → applied_unverified → verified
        ↘ not_performed / failed / recovery_required / ambiguous
```

Ante `applying` o `applied_unverified`:

1. no repitas el efecto;
2. verifica origen, destino o Papelera y receipt;
3. compara identidad, tamaño, mtime y hash exigido;
4. registra la observación de forma append-only;
5. decide recuperación o confirmación mediante una autorización separada.

La ausencia de la ruta original no demuestra que el archivo esté en Papelera.

## Foundation KIO y recovery 0.11

La foundation KIO preparada ya clasifica `blocked`, `recovery_required` y
`applied`, y sólo produce `KioTrashReceipt` cuando el caller confirma ausencia
del origen y evidencia de Papelera. `KioTrashBackend` la conecta al lifecycle
grant-bound mediante un runner y un verificador inyectados; la foundation por
sí sola no aporta restauración, autorización ni prueba same-filesystem.

La integración de `0.11.x` localiza el efecto por receipt y metadata de KIO, no
asume que el nombre original quedó intacto y exige un backend inyectado para
fixtures. El preview de restore es read-only; la aplicación exige confirmación
exacta del action/receipt, crea su propio `file_actions` intent, restaura con
no-replace, verifica bytes/hash y elimina `.trashinfo` sólo después de verificar
el archivo restaurado. Un fallo posterior al movimiento conserva
`recovery_required` y se concilia sin retry. La ejecución integrada de KIO se
verifica en lotes bounded sobre fixtures privados; el restore visual de
escritorio sigue siendo un gate posterior e independiente.

Si falta metadata, aparece una colisión, el filesystem cambió o el efecto cruzó
dispositivo, el estado permanece `recovery_required`; no se degrada a borrado
directo.

## Corrupción o schema futuro

Trabaja sobre una copia verificada. No migres la única base, no hagas `VACUUM`,
no borres sidecars y no edites números manualmente. Si la versión instalada no
admite el schema, instala una release compatible o reconstruye la proyección
desde originales después de preservar evidencia.

## Rollback de release

Rollback selecciona la release anterior verificada; no muta las bases para hacer
coincidir un binario antiguo. Antes de cambiar `current`, comprueba que el schema
sea legible por el destino. Después valida manifest, launcher y smoke público.

Los contratos de owners y migración están en [PERSISTENCE.md](PERSISTENCE.md).
