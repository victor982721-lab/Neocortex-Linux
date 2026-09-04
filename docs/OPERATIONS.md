# Operación de NeoCortex

Esta guía contiene procedimientos. Los argumentos exactos están en
[CLI.md](CLI.md), los owners en [PERSISTENCE.md](PERSISTENCE.md) y la recuperación
en [RECOVERY.md](RECOVERY.md).

## Preflight

Antes de procesar contenido:

1. confirma la raíz y que no sea un árbol interno de NeoCortex;
2. ejecuta `Neocortex --version` y compara el runtime esperado;
3. consulta estado y health sin crear cobertura nueva;
4. comprueba espacio, memoria, herramientas externas y modelos necesarios;
5. fija una ruta, máximo de elementos y límite de tiempo;
6. confirma que no existe otro writer sobre los mismos owners.

```bash
Neocortex status --scope all
Neocortex --state-health --state-health-json
```

No inspecciones SQLite viva con clientes ordinarios. Durante una corrida larga
observa el stream, transcript, proceso y cgroup; espera el estado terminal antes
de abrir owners salvo que una superficie pública garantice una lectura compatible.

## Piloto

Empieza con 20–50 elementos y 10–15 minutos. Para PDF:

```bash
Root="$HOME/Documentos/NeoCortex/Pilot"
test -d "$Root" || exit 2
Neocortex --root "$Root" --route pdf --max-count 25 --strict-exit-codes
```

Registra ruta, versión, exit, tiempo, elementos elegibles/procesados, errores,
cache hits y throughput. Corrige el primer bloqueo antes de ampliar rutas.

Ejecuta el mismo comando por segunda vez. El replay debe mostrar qué se reutilizó
y qué provider es `non_replayable`; no llames incremental a una reejecución
oculta.

## Ampliación controlada

Después de aprobar una ruta, añade otra explícitamente. `--all` es una operación
amplia, no el primer smoke; selecciona todas las rutas registradas, incluida Code
como contenido.

Una corrida sin `--apply` no modifica originales, pero sí escribe inventario,
cachés, planes y publicaciones. Distingue siempre consulta read-only, producción
de estado y efecto sobre corpus.

## Reanudación

Usa el identificador durable de la corrida:

```bash
Neocortex --status --status-run RUN_ID --status-json
Neocortex --route pdf --resume-run RUN_ID --strict-exit-codes
```

La reanudación debe usar los inputs durables del run, no volver a descubrir una
raíz cambiante como si fuera la misma ejecución. Si cambió una precondición,
crea una corrida nueva o registra la abstención.

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

Las rutas emiten `ProgressEvent` con fase, completado, total y métricas. La salida
operativa debe mostrar al menos fase, elementos, bytes, errores, velocidad y
tiempo. Configura presupuestos globales sólo cuando una medición los justifique.

No ejecutes un recorrido largo sin máximo o deadline. Evita un proceso por
archivo y commits SQLite por elemento; usa streaming y batches acotados.

## Modelos y herramientas externas

```bash
Neocortex models status --json
Neocortex models prepare
```

`status` es local. `prepare` puede usar red y requiere autorización. Tesseract,
FFmpeg/FFprobe, LibreOffice y otros binarios se detectan antes de iniciar la ruta;
una ausencia se reporta como cobertura o bloqueo, no como éxito vacío.

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

**TARGET:** `apply → verify → reconcile` consumirá y revalidará el grant. La
foundation KIO preparada no habilita Linux `--apply`; esta auditoría tampoco
autoriza una prueba contra KIO real.

## Mantenimiento de estado

```bash
Neocortex databases status --json
Neocortex databases backup --backup-directory "$Backup" --json
Neocortex databases restore --backup-directory "$Backup" --json
Neocortex databases purge --json
```

Todos muestran preview cuando corresponde. Antes de `--apply`, conserva el
manifest/digest presentado, detén writers y sigue [RECOVERY.md](RECOVERY.md).

## Instalación y release

La instalación personal es offline y reproducible desde un wheelhouse local
autenticado:

```bash
python3.14 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --wheelhouse "$Wheelhouse" --prepare-models --desktop
python3.14 tools/release_linux.py verify
```

No existe fallback de red. El wheelhouse contiene `wheelhouse-manifest.json`,
wheels compatibles y hashes. Si falta una dependencia, la instalación se
abstiene; no cambies constraints para sortearla.

Una release termina cuando artefacto, manifest, launcher y `source_sha`
coinciden, el smoke público pasa sin `PYTHONPATH`, el replay es verificable,
staging queda vacío y sólo permanecen `current` y el rollback inmediato.

## Auditorías técnicas

Una auditoría integral es excepcional. Registra estado vivo, HEAD, alcance,
comando, exit, duración y evidencia; separa hechos, inferencias y no verificado.
Un benchmark compara la misma carga y entorno. Una release se valida desde el
artefacto instalado, no desde el checkout.

Los informes y salidas brutas viven fuera de la documentación canónica. El
repositorio conserva sólo contratos actuales, roadmap y changelog.
