# Operación de NeoCortex

Esta guía contiene procedimientos. Los argumentos exactos están en
[CLI.md](CLI.md), los owners en [PERSISTENCE.md](PERSISTENCE.md) y la recuperación
en [RECOVERY.md](RECOVERY.md).

## Preflight

Antes de procesar contenido:

1. confirma la raíz y que no sea un árbol interno de NeoCortex;
2. ejecuta `Neocortex --version` y compara también el `source_sha` del manifest,
   porque dos instalaciones pueden declarar la misma versión;
3. consulta estado y health sin crear cobertura nueva;
4. comprueba espacio, memoria, herramientas externas y modelos necesarios;
5. fija una ruta, máximo de elementos y límite de tiempo;
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

## Piloto

Empieza con una raíz que contenga sólo 20–50 elementos autorizados y cerca la
corrida completa a 10–15 minutos. `--max-count` limita PDFs, no el inventario
común; el timeout por documento tampoco es un deadline global. Para PDF:

```bash
Root="$HOME/Documentos/NeoCortex/Pilot"
test -d "$Root" || exit 2
Neocortex --root "$Root" --route pdf --max-count 25 --strict-exit-codes
```

Registra ruta, versión, exit, tiempo, elementos elegibles/procesados, errores,
cache hits y throughput. Corrige el primer bloqueo antes de ampliar rutas.

Ejecuta el mismo comando por segunda vez. El replay debe mostrar qué se reutilizó
y qué trabajo nuevo quedó, sin ocultar una reejecución como incremental.

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

Los límites de inventario y verificación no equivalen a un deadline durable
global de `--all`: el lifecycle entre workers y la uniformidad de reanudación
siguen siendo trabajo objetivo de 0.13.0.

## Modelos y herramientas externas

```bash
Neocortex models status --json
Neocortex models prepare
```

`status` es local. `prepare` puede usar red y requiere autorización. Tesseract,
FFmpeg/FFprobe y otros binarios se detectan antes de iniciar la ruta;
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

**IMPLEMENTED sobre fixtures:** `curate apply` consume un grant confirmado y
revalida su manifest antes de cada efecto, `curate reconcile` registra
observaciones sin reintentar y `curate restore preview/apply` ofrece una
reversión no-replace con un intent separado y confirmación exacta. La CLI
instalada no selecciona un backend ni un run firmado, por lo que apply/restore
devuelven `backend_unavailable` antes de crear efectos; las pruebas usan
backends inyectados y raíces temporales. La foundation KIO real y el restore de
escritorio siguen fuera de este gate.

```bash
Neocortex curate apply GRANT_ID --confirm-grant-id GRANT_ID --json
Neocortex curate reconcile --actor ACTOR --confirm-reconcile --json
```

## Mantenimiento de estado

Las comprobaciones tienen alcance explícito y no convierten lo omitido en sano:

```bash
Neocortex --state-health --state-health-scope compatibility --state-health-json
Neocortex --state-health --state-health-owner semantic --state-health-timeout 180 --state-health-json
Neocortex --archive-issues 20 --diagnostics-reason archive_member_count_limit --diagnostics-json
Neocortex --review-candidates 20 --review-json
Neocortex --action-recovery-status --action-recovery-json
```

`--state-health-max-owners` y `--state-health-after-owner` permiten continuar una
comprobación acotada; revisa también los owners que requieren reintento. Los
diagnósticos de contenido usan `--diagnostics-cursor` y filtros ligados al mismo
snapshot. Las páginas vacías explican disponibilidad y cobertura; JSONL legacy
se solicita explícitamente con `--review-json-lines` o
`--action-recovery-json-lines`, junto a su selector JSON.

Los presupuestos de snapshots se comprueban antes de copiar y pueden agotarse;
eso no demuestra corrupción. Retención sigue siendo diagnóstico/planificación,
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

## Instalación y release

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
Un benchmark compara la misma carga y entorno. Una release se valida desde el
artefacto instalado, no desde el checkout.

Los informes y salidas brutas viven fuera de la documentación canónica. El
repositorio conserva sólo contratos actuales, roadmap y changelog.
