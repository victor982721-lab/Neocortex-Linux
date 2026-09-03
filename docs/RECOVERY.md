# Recuperación, backup y rollback

> **Contrato vigente:** NeoCortex se opera en Linux/Kubuntu. Las funciones
> `Neocortex databases status|backup|restore|purge` son la superficie canónica,
> con `SQLiteReadSession`, manifests, digest y confirmaciones explícitas. Los
> ejemplos Windows y los scripts `mode=ro` más abajo son evidencia histórica,
> no instrucciones para una operación nueva.

NeoCortex conserva estado relacionado en varias bases SQLite. Esta guía no
describe sus esquemas; consulte [PERSISTENCE.md](PERSISTENCE.md). Su objetivo es preservar
evidencia y evitar que una recuperación repita una acción incierta o pierda
frames confirmados del WAL.

## Principios

1. Detenga writers antes de respaldar o restaurar el conjunto completo.
2. Una copia aislada de `archivo.sqlite3` **no es un backup válido** cuando
   puede existir `archivo.sqlite3-wal`.
3. Use `sqlite3.Connection.backup`, que incorpora las páginas confirmadas
   visibles para SQLite.
4. Conserve originales, WAL, SHM y backups. Para retirar deliberadamente el
   estado use `Neocortex databases purge`; no elimine archivos manualmente.
5. Valide `integrity_check`, `foreign_key_check` y versión de esquema después de
   cada copia o restauración.
6. Nunca simule un downgrade cambiando `PRAGMA user_version` o una tabla de
   metadatos.
7. Una acción cuyo efecto real no puede probarse queda incierta: no se repite
   automáticamente.

## Directorio de estado

La ubicación normal en Linux es:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/Neocortex/state
```

Confírmela sin modificarla:

```bash
State="${XDG_STATE_HOME:-$HOME/.local/state}/Neocortex/state"
test -d "$State" && find "$State" -maxdepth 1 -type f \\
  \( -name '*.sqlite3' -o -name '*.sqlite3-wal' -o -name '*.sqlite3-shm' \) \\
  -printf '%f %s bytes\\n'
```

No use el tamaño de WAL o SHM como razón para borrarlos.

## Preparación

1. Finalice de forma cooperativa la terminal, watcher o GUI propios.
2. Confirme por PID, proceso padre y línea de comandos que no queda un proceso
   de NeoCortex perteneciente a esta operación. No termine procesos por nombre
   genérico.
3. Registre, si todavía responde:

   ```text
   Neocortex --version
   Neocortex --status --status-json
   ```

4. Elija un destino nuevo fuera del directorio de estado. No reutilice una
   carpeta de backup existente.

Aunque la API de backup admite writers concurrentes por base, NeoCortex usa
varias bases relacionadas. Detener writers evita obtener instantes lógicos
diferentes entre ellas.

## Backup consistente mediante la API SQLite (histórico)

El procedimiento manual siguiente conserva el razonamiento de la incidencia
original, pero no debe ejecutarse sobre estado vigente. Use `Neocortex databases
backup --backup-directory ...` para obtener el manifest y las validaciones del
kernel, y `Neocortex databases restore` para validar o publicar un conjunto con
digest y confirmación.

El siguiente script usa sólo la biblioteca estándar, rechaza sobrescrituras,
respalda todas las bases `*.sqlite3` del directorio indicado y valida cada
destino. Guárdelo temporalmente como `backup_neocortex_state.py`; no lo presente
como un comando incorporado al paquete.

```python
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def readonly_uri(path: Path) -> str:
    return f"{path.resolve(strict=True).as_uri()}?mode=ro"


def validate_database(path: Path) -> tuple[int, str]:
    with closing(
        sqlite3.connect(readonly_uri(path), uri=True, timeout=5.0)
    ) as connection:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError(f"integrity_check falló para {path}: {integrity[:20]}")
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign_keys:
            raise RuntimeError(
                f"foreign_key_check falló para {path}: {foreign_keys[:20]}"
            )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        return user_version, integrity[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()

    source = args.source.resolve(strict=True)
    if not source.is_dir():
        raise NotADirectoryError(source)
    destination = args.destination.resolve()
    if destination.exists():
        raise FileExistsError(f"El destino ya existe: {destination}")
    if source == destination or source in destination.parents:
        raise ValueError("El backup debe quedar fuera del directorio de estado")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"El padre del destino no existe: {destination.parent}"
        )

    databases = sorted(path for path in source.glob("*.sqlite3") if path.is_file())
    if not databases:
        raise FileNotFoundError(f"No hay bases SQLite en {source}")
    destination.mkdir()

    for database in databases:
        output = destination / database.name
        if output.exists():
            raise FileExistsError(output)
        with (
            closing(
                sqlite3.connect(readonly_uri(database), uri=True, timeout=5.0)
            ) as origin,
            closing(sqlite3.connect(output, timeout=5.0)) as target,
        ):
            origin.backup(target, pages=1024, sleep=0.05)
        user_version, result = validate_database(output)
        print(f"OK {output.name} user_version={user_version} integrity={result}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Ejecución explícita:

```text
$State = Join-Path $env:LOCALAPPDATA 'Neocortex\state'
$Backup = Join-Path $env:USERPROFILE 'NeoCortex_Backups\2026-07-24_190000'
py -3 .\backup_neocortex_state.py --source $State --destination $Backup
if ($LASTEXITCODE -ne 0) { throw "El backup no terminó correctamente" }
```

Sustituya la marca temporal por la hora real. Si el script falla después de
crear parte del conjunto, no borre el destino parcial: márquelo como incompleto,
presérvelo para diagnóstico y cree otro destino nuevo después de corregir la
causa.

El backup no requiere copiar `-wal` ni `-shm`; copiar esos archivos por separado
no mejora el snapshot creado por la API.

## Purga explícita del estado

`Neocortex databases purge` elimina bases completas y sus sidecars únicamente
después de una vista previa, confirmación literal, backup online verificado y
adquisición de los locks de NeoCortex. La operación conserva el corpus,
releases, modelos, recibos y archivos no reconocidos; el manifest del backup
permite restaurar cada base con el procedimiento de esta guía.

El script respalda sólo las bases `*.sqlite3`. `ui.ini`, cachés de modelos y
otros archivos auxiliares no forman parte de ese conjunto transaccional. Si una
recuperación de instalación necesita conservarlos, invéntaríelos y cópielos por
separado con sus procesos detenidos; no presente esa copia auxiliar como
evidencia de coherencia entre bases.

## Validación del backup

La salida debe contener una línea `OK` por cada base descubierta. Además:

- compare la lista de nombres con `PERSISTENCE.md` y con el directorio fuente;
- conserve los `user_version` informados;
- compruebe que el destino no contiene archivos de tamaño cero;
- no abra el backup con una versión que pueda migrarlo automáticamente;
- pruebe restauración primero sobre una copia o entorno temporal.

`integrity_check` y `foreign_key_check` verifican estructuras SQLite y
restricciones declaradas; no demuestran coherencia semántica entre bases ni
completitud del corpus.

## Restauración

`Neocortex databases restore` valida el manifest en modo preview y sólo publica
con `--apply`, `--manifest-sha256 SHA256` y
`--confirm-database-restore RESTORE_DATABASES`. La restauración es una operación
explícita y potencialmente destructiva sobre el estado actual.

1. Instale primero una versión de NeoCortex compatible con el backup.
2. Detenga todos los writers.
3. Cree y valide un backup **pre-restauración** del estado actual.
4. Valide el backup que se restaurará sin migrarlo.
5. Restaure una base del mismo nombre mediante la API SQLite; no mediante
   `Copy-Item` sobre una base con WAL.
6. Valide inmediatamente la base restaurada.
7. Repita para el conjunto compatible y registre exactamente cuáles terminaron.
8. Ejecute doctors/status antes de cualquier reproceso.

La primitiva para una sola base es:

```python
import sqlite3
from contextlib import closing
from pathlib import Path


def restore_one(backup_file: Path, live_file: Path) -> None:
    backup_file = backup_file.resolve(strict=True)
    live_file = live_file.resolve(strict=True)
    if backup_file.name.casefold() != live_file.name.casefold():
        raise ValueError("El backup y el destino deben tener el mismo nombre")
    if backup_file == live_file:
        raise ValueError("El origen y el destino no pueden ser el mismo archivo")
    source_uri = f"{backup_file.as_uri()}?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as source,
        closing(sqlite3.connect(live_file, timeout=5.0)) as destination,
    ):
        source.backup(destination, pages=1024, sleep=0.05)
```

Esta función sobrescribe lógicamente el contenido del destino; úsela sólo tras
el backup pre-restauración y dentro de un procedimiento que ejecute las mismas
validaciones del script anterior. No elimine manualmente WAL/SHM antes o después.

Si falla una restauración intermedia, deténgase. No mezcle deliberadamente el
resto de bases de dos generaciones sin una matriz de compatibilidad verificada.

## Migraciones y downgrade

- Las migraciones se aplican al abrir estado mediante los propietarios de cada
  esquema.
- `0.5.0` eleva framework 17→18, catálogo 5→6 y semántica 5→6; no abrió ni
  migró las bases vivas durante la auditoría.
- `0.6.0` eleva framework 18→19 de forma aditiva para conservar observaciones
  de conciliación. `status` no migra; un `record` confirmado puede migrar una
  base existente.
- El lector de recuperación comprueba la versión declarada del framework y
  rechaza un schema posterior al soportado. No intenta reinterpretarlo,
  migrarlo ni crear estado desde `status`.
- Haga backup antes de permitir la primera apertura con una versión nueva.
- Una migración poblada debe probarse antes sobre copias, con conteos,
  idempotencia, interrupción y rollback.
- No borre una base que la aplicación declare futura, desconocida o derivada.
- No edite `PRAGMA user_version`, `metadata.schema_version` ni el historial para
  forzar compatibilidad.
- El downgrade admitido consiste en restaurar un backup completo compatible y
  el paquete que lo produjo. No existe downgrade destructivo automático.

## Generaciones incompletas

- Semántica v6 mantiene el head anterior mientras una generación está
  `building`; `ready_partial` tampoco publica. Un reinicio puede reusar sólo un
  build compatible en modelo, firma y proveniencia. No edite
  `published_embedding_heads` ni fuerce un rebase por SQL.
- Catálogo v6 conserva `catalog_publications` durante fallo o cancelación; un
  competidor atrasado queda `superseded`. No copie filas de staging a
  `documents` ni cambie el puntero manualmente.
- Las generaciones fallidas, parciales y abandonadas se preservan porque aún no
  existe una política global de poda. Primero diagnostique; no las borre para
  liberar espacio.
- El rollback durable sigue siendo el backup de todas las bases. Que la
  generación publicada anterior continúe visible durante el build no sustituye
  restaurar un conjunto compatible.
- Sólo los repositorios oficiales garantizan esta visibilidad. SQL externo sobre
  tablas legacy puede eludir el contrato y no debe usarse para recuperación.

## Corridas interrumpidas

Primero use la consulta de sólo lectura:

```text
Neocortex --status --status-limit 20
Neocortex --status --status-run 40 --status-json
```

Si el run conserva un snapshot válido y sólo tiene fases de ruta incompletas:

```text
Neocortex --resume-run 40
```

Los runs nuevos cruzan la frontera reanudable únicamente después de materializar
todos los candidatos: el vínculo a `scan_id`, los contadores y el evento
`neocortex.routing-snapshot/v1` se publican juntos. La apertura vuelve a exigir
un scan `complete` sin errores, `files_seen == COUNT(files)` y coincidencia de
ruta e identidad física de la raíz.

La recuperación automática de un run legacy sin `scan_id` no reconstruye datos
por conjetura. Sólo admite un run inicial `interrupted` con evidencia
`Inventario preparado` válida y no contradictoria, conteo de candidatos
estable y al menos un `route_run` durable. El CAS del vínculo y el evento
`neocortex.inventory-recovery/v1` comparten transacción; evidencia malformada,
excesiva, ambigua o con metadatos incompatibles deja el run sin modificar.

No reanude si hubo una acción sobre archivos cuyo efecto sea incierto. Resuelva
primero la sección siguiente.

Una discontinuidad USN, una raíz sustituida o errores de acceso requieren
abstención o una nueva reconciliación completa; no adelante cursores ni marque
manualmente el run como completado.

## Acciones inciertas después de una caída

### Máquina de estados v19

`file_actions` registra intención en `started`. Inmediatamente antes de la
llamada nativa, persiste `applying`, la identidad física esperada y una clave
idempotente. Un retorno observado se confirma como `applied` con recibo. Una
excepción o caída después de cruzar esa frontera queda `recovery_required`. Al
reiniciar, una acción abandonada en `started` termina `failed` con evidencia de
que no alcanzó la frontera y no intentó un efecto; sólo una acción abandonada en
`applying` queda `recovery_required`. Ninguna acción incierta se reintenta
automáticamente.

Cada transición agrega `file_action_events`; triggers impiden editar o borrar
esa bitácora. Las filas legacy no reciben identidad o recibos inventados durante
la migración 17→18. V19 añade `file_action_reconciliation_events` sin modificar
las acciones existentes.

### Conciliador de sólo lectura

Después de detener writers y crear el backup, ejecute con la versión `0.6.0`
validada:

```text
Neocortex --action-recovery-status --action-recovery-limit 100
Neocortex --action-recovery-status --action-recovery-after 100 --action-recovery-run 40
Neocortex --action-recovery-status --action-recovery-json
```

El comando abre la base existente en modo de sólo lectura, inspecciona sólo
`applying`/`recovery_required`, pagina por `action_id` y no cambia SQLite ni el
filesystem. Repetirlo sobre el mismo estado devuelve la misma clasificación:

| Clasificación | Evidencia observada | Acción operativa |
|---|---|---|
| `confirmed` | Rename/move: origen ausente y destino conserva la identidad; trash legacy: origen ausente y recibo durable cuyas rutas de origen/destino coinciden con la misma acción. | Preserve evidencia; no repita. El comando no modifica el registro. |
| `not_performed` | Origen conserva la identidad esperada y el destino no existe. | No reintente automáticamente; revise y cree una nueva autorización sólo si todavía corresponde. |
| `ambiguous` | Combinación física incompatible o ruta sustituida. | Preserve todo, no mute y haga revisión manual. |
| `impossible_to_check` | Evidencia legacy/malformada, estado incompatible o acceso imposible. | Preserve todo y no infiera el efecto. |

Una página vacía o compuesta sólo por `confirmed`/`not_performed` devuelve `0`.
La presencia de `ambiguous`/`impossible_to_check`, una base ausente/incompatible
o un error de lectura devuelve `2`. JSON es JSON Lines determinista y sólo se
activa con una operación status o record de esta familia.

### Registro durable de la observación

Después de revisar una fila concreta puede conservar la observación sin
recuperar ni autorizar el archivo:

```text
Neocortex --action-recovery-record 42 --action-recovery-actor "Victor" --confirm-reconciliation-record --action-recovery-json
```

La operación vuelve a clasificar, abre únicamente una base existente y agrega
un evento append-only. `--action-recovery-expected-event EVENT_ID` aplica CAS a
una observación posterior; repetir exactamente la misma solicitud devuelve el
mismo evento. Actor, procedencia, firma, identidades, recibo, clasificación,
recomendación y timestamps quedan en la evidencia. Un evento registrado declara
`authorizes_filesystem_mutation=false` y no cambia el status de `file_actions`.

El código es `2` si la observación quedó `ambiguous` o `impossible_to_check`,
incluso cuando el commit fue correcto. Confirme `event_id` en la salida y vuelva
a consultar. Sólo existen `status` y `record`: siguen deliberadamente pendientes
las operaciones separadas de decisión humana (`decide`), autorización
(`authorize`), recuperación (`recover`) y verificación (`verify`). Registrar una
observación no concede ninguna de ellas.

### Procedimiento

1. No vuelva a ejecutar `--apply` ni `--organization-apply`.
2. Cree un backup consistente del estado y preserve salida, `run_id`, rutas,
   tiempos, identidad, recibos y eventos.
3. Ejecute el conciliador por páginas hasta no recibir filas y, cuando necesite
   evidencia durable, registre cada observación con actor/confirmación.
4. No cambie manualmente estados a `applied`, `failed` o `recovery_required`.
5. No interprete la ausencia de una ruta como prueba de Papelera. En `0.6.0` la
   aplicación de Papelera está deshabilitada; filas antiguas sin recibo o con
   un recibo cuyas rutas no coincidan con la acción siguen siendo ambiguas.
6. Reanude únicamente cuando cada acción potencialmente repetible tenga una
   resolución humana documentada. Ni status ni record autorizan otra syscall.

La organización documental conserva en `organization_plans` un
`recovery_required` separado. Ese plan reserva el destino y queda excluido de
la aplicación automática:

```text
Neocortex --organization-preview 100 --organization-preview-status recovery_required
```

No existe todavía un conciliador automático que modifique o complete ese plan;
observe origen/destino por identidad y manténgalo bloqueado si hay duda. Los
estados `moved_cache_pending` sólo reanudan sincronización durable ya confirmada,
no vuelven a mover el archivo.

## Corrupción o incompatibilidad

1. Detenga writers y preserve todos los archivos, incluidos WAL/SHM.
2. No ejecute `VACUUM`, `.recover`, DDL, `REINDEX` ni migraciones sobre el único
   original.
3. Trabaje sobre un backup o copia forense consistente.
4. Ejecute el doctor específico cuando exista:

   ```text
   Neocortex --pdf-verify
   Neocortex --code-status --code-json
   Neocortex --semantic-status
   ```

5. Registre el error exacto. Una salida `2` es diagnóstico, no reparación.
6. Restaure sólo desde un backup validado y compatible.

## Qué no debe hacerse

- No copiar sólo `.sqlite3` ignorando WAL/SHM.
- No borrar WAL/SHM, bases, índices o generaciones manualmente para liberar
  espacio; use la purga explícita y su backup verificable.
- No modificar números de versión para fingir un downgrade.
- No repetir automáticamente trash, rename o movimientos inciertos.
- No restaurar con watcher o GUI activos.
- No mezclar bases de backups distintos sin compatibilidad demostrada.
- No reprocesar el corpus vivo para probar que el rollback “funcionó”.
- No considerar la Papelera de reciclaje como mecanismo de recuperación.
