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
del origen y evidencia de Papelera. No está conectada al lifecycle de acciones ni
incluye por sí sola restauración, autorización o prueba same-filesystem.

La integración de `0.11.x` localiza el efecto por receipt y metadata de KIO, no
asume que el nombre original quedó intacto y exige un backend inyectado para
fixtures. El preview de restore es read-only; la aplicación exige confirmación
exacta del action/receipt, crea su propio `file_actions` intent, restaura con
no-replace, verifica bytes/hash y elimina `.trashinfo` sólo después de verificar
el archivo restaurado. Un fallo posterior al movimiento conserva
`recovery_required` y se concilia sin retry. La ejecución real de KIO y el
restore de escritorio siguen siendo gates posteriores.

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
