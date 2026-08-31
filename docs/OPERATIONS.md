# Guía operativa

Esta guía cubre ejecución normal, watcher, recursos, cancelación, diagnóstico y
mantenimiento. La estructura de componentes se describe en
[ARCHITECTURE.md](ARCHITECTURE.md) y los propietarios y versiones de las bases
en [PERSISTENCE.md](PERSISTENCE.md). La consulta cross-owner de solo lectura se
documenta en [KNOWLEDGE.md](KNOWLEDGE.md); no se duplican esos contratos aquí.

## Flujo personal recomendado

Éste es el flujo predeterminado; las secciones posteriores son referencia
cuando una frontera concreta lo requiera:

1. Preflight read-only: versión, capacidades, estado general y estado del
   subsistema implicado.
2. Una sola ruta y una muestra representativa de 20–50 elementos, con límite
   duro de 10–15 minutos.
3. Una salida que Victor pueda usar: búsqueda, evidencia, clasificación o
   preview; registre errores, tiempo y throughput.
4. La misma corrida una segunda vez para probar caché, reanudación e
   incrementalidad.
5. Una búsqueda o revisión real y una proyección antes de escalar.

Para el preflight y la consulta cotidiana prefiera la fachada corta:

```bash
Neocortex status --scope all
Neocortex search "consulta representativa" --scope personal
Neocortex ask "pregunta concreta" --scope personal
Neocortex review value --scope personal
```

Esos comandos no crean o migran estado. En cambio una corrida sin `--apply`
preserva el corpus, pero sí escribe inventario y cachés.

Si el piloto falla o excede el límite, deténgalo y corrija la causa. `--all`, un
watcher, una indexación Semantic completa, una migración, un rollback o una
auditoría integral no son el punto de partida.

## Bootstrap autenticado de pip

Antes de cualquier instalación Python, los flujos locales mantenidos ejecutan el
helper independiente del `pip` ambiental:

```bash
python -I tools/bootstrap_pip.py
```

El helper descarga únicamente el wheel oficial fijado de `pip 26.2.1`, valida
su nombre y SHA-256 antes de ejecutarlo, instala con aislamiento, sin índice ni
dependencias, y verifica la versión exacta. `--wheel` permite entregar ese mismo
artefacto ya descargado en un flujo offline; no relaja la autenticación.

## Condiciones previas

1. Confirme que no haya otra ejecución de NeoCortex usando el mismo directorio
   de estado.
2. Verifique el launcher y la ayuda:

   ```bash
   Neocortex --version
   Neocortex --help
   ```

   Esta guía corresponde a la fuente `0.9.0`. Si `--version` no existe o no
   informa `0.9.0`, el launcher operativo no coincide con esta entrega: no use
   sus contratos nuevos sobre estado real hasta validar el artefacto correcto.
   En Linux añada `Neocortex doctor platform --json` y confirme
   `compatible=true`, inventario portable y contención POSIX antes de abrir
   estado real.

3. Confirme la raíz exacta y que no sea un symlink, junction o punto de
   reanálisis.
4. El recorrido portable funciona sin USN. Para probar su acelerador opcional,
   use un volumen NTFS local y los permisos de lectura ya disponibles; no eleve
   la corrida cotidiana sólo para habilitarlo. Las rutas UNC y otros sistemas de
   archivos no ofrecen identidad/USN equivalentes, pero sí pueden usar el
   baseline portable si cumplen el resto de las protecciones de la raíz.
5. Antes de una actualización, migración o acción sobre archivos, siga
   [RECOVERY.md](RECOVERY.md).

En Linux, fuente, estado y release permanecen separados:

```text
Fuente:    ~/Neocortex/Repository
Corpus:    ~/Documentos/NeoCortex/Corpus
Release:   ~/.local/share/Neocortex/releases/<release-id>
Activa:    ~/.local/share/Neocortex/current
Launcher:  ~/.local/share/Neocortex/bin/Neocortex
Alias:     ~/.local/bin/Neocortex
Estado:    ~/.local/state/Neocortex/state
Modelos:   ~/.local/share/Neocortex/models
```

`XDG_CONFIG_HOME`, `XDG_STATE_HOME` y `XDG_DATA_HOME` sustituyen sus defaults
cuando están definidos. Consulte [LINUX_KUBUNTU.md](LINUX_KUBUNTU.md).

## Flujo normal no mutador del corpus

Una corrida sin `--apply` puede leer el corpus y escribir inventario, cachés,
eventos y planes; no es una consulta de sólo lectura. Empiece con un conjunto
acotado:

```bash
$Root = 'C:\Datos'
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "La raíz no existe o no es un directorio: $Root"
}
Neocortex --root $Root --route pdf --MaxCount 25 --strict-exit-codes
```

Equivalente Linux, siempre sin flags de mutación:

```bash
Root="$HOME/Documentos/NeoCortex/Pilot"
test -d "$Root"
Neocortex --root "$Root" --route pdf --MaxCount 25 --strict-exit-codes
```

La frontera normal captura `InternalPathsPolicy`: una raíz situada dentro del
repositorio, runtime, datos de aplicación o laboratorio interno se rechaza; si
esos árboles son descendientes del corpus se excluyen del inventario. El estado
no puede ser igual ni ancestro del corpus, porque esa exclusión podaría la raíz
completa. Framework persiste la firma efectiva que combina la firma cruda de
exclusión con la identidad de esas rutas internas.

Después inspeccione la ejecución:

```powershell
Neocortex --status
Neocortex --status --status-json
```

Después de aprobar cada ruta por separado se puede probar una lista aún
acotada. `--all` selecciona PDF, DOCX, Office, ZIP, texto/correo, audio, video,
imagen y código, actualiza el catálogo técnico y se reserva para cuando exista una
proyección aceptada. Al final avanza Semantic sobre las cachés documentales y,
si existe la caché de imagen, también sobre CLIP visión/OCR; Code requiere
`--semantic-source code` explícito.
No existe una consulta de receipt de autoanálisis. `--all` ejecuta el flujo
seleccionado y, si el corpus no está disponible, informa `corpus_unavailable`
sin traceback ni creación parcial del estado documental.

```powershell
Neocortex --root $Root --route pdf,docx --MaxCount 25 --docx-max-count 25 --strict-exit-codes
```

No use una corrida amplia como prueba de instalación. Ayuda, versión y doctors
son la barrera inicial apropiada.

### Piloto ZIP y replay

Un piloto de ZIP debe fijar cantidad y conservar el estado fuera de la muestra:

```bash
Neocortex --root "$Root" --state-directory "$State" --route archive \
  --archive-max-count 25 --strict-exit-codes
Neocortex --state-directory "$State" --archive-status
Neocortex --state-directory "$State" --archive-search "término representativo"
```

Repita exactamente el primer comando. El segundo resumen debe informar los ZIP
seleccionados como `cache_hits`, sin volver a descomprimirlos. Compruebe una
ruta profunda como `contenedor.zip!/otro.zip!/documento.txt` y confirme
`inside_zip=1`. Una incidencia de traversal, cifrado, symlink, duplicado,
profundidad o expansión deja el contenedor `partial` y el miembro inseguro sin
leer; no se relajan límites para convertir ese resultado en éxito. La ruta no
extrae archivos, no usa `--apply` y no organiza físicamente el corpus.

Incluya en el fixture al menos un PDF con texto nativo, un PDF escaneado y una
imagen con texto dentro de un ZIP anidado. Con `--ocr auto`, las páginas con
menos de 40 caracteres nativos y las imágenes admitidas pasan por el worker
aislado. Compruebe que una dependencia/idioma OCR ausente se informe como
incidencia y que el miembro siga visible, en lugar de aceptar texto ficticio.

### Piloto de texto, EML y Office heredado

```bash
Neocortex --root "$Root" --state-directory "$State" --route text \
  --text-max-count 25 --strict-exit-codes
Neocortex --state-directory "$State" --knowledge-status
Neocortex --state-directory "$State" --knowledge-search "término representativo"
Neocortex --state-directory "$State" --catalog-preview 25
```

Para revisar propuestas de curación sin tocar el corpus ni los owners SQLite:

```bash
Neocortex --state-directory "$State" --curation-preview 25
Neocortex --state-directory "$State" --curation-preview 25 --curation-json
```

La salida reúne duplicados exactos, planes de organización y archivos vacíos
como elementos `review`, con identidad, evidencia, razón y un
`preview_fingerprint`. La operación es bounded/read-only, no crea ni migra
estado y rechaza `--apply` y `--route`.

La muestra debe combinar texto plano/Markdown, CSV o TSV, HTML/XML/JSON, un EML
multipart y DOC/XLS/PPT reales. Repita el productor: el segundo resumen debe
convertir los documentos sin cambios en `cache_hits`. El asunto del EML debe
aparecer como título/nombre sugerido cuando sea más útil que el basename. Un
Office heredado sin LibreOffice ni fallback local queda como error explícito;
no se intenta interpretar el CFB como texto plano.

Para explicar causalmente un resultado Text ya publicado, use la identidad
exacta que devolvió Knowledge o search; no derive un ID desde la ruta:

```bash
Neocortex knowledge health 'resource:file:1:2:-1' \
  --scope personal --json
```

La consulta selecciona Text o PDF por identidad y evidencia publicada, nunca
por ruta/extensión; lee Inventory, owner fuente, Catalog y Knowledge dos veces y
reintenta una sola vez si cambia el snapshot. `healthy` exige los facts
aplicables completos, publicados y causalmente alineados. Para PDF schema 13
verifica además estado tipado, páginas, staging, errores, FTS y recovery
estructural reconocido. Un mismatch, owner ausente, schema futuro/corrupto,
publicación incompleta, WAL activo o segundo cambio produce un estado acotado o
abstención; nunca repara el estado. Esta vertical cubre sólo Text/PDF y no
demuestra contenido/OCR, verdad semántica, calidad visual, otros owners ni
pérdida de energía.

## Código como contenido

Code es una ruta de producto, no una herramienta para revisar el desarrollo de
NeoCortex. Descubre proyectos, reconoce lenguajes, guarda símbolos,
dependencias, versiones y diagnósticos, e incorpora búsquedas y relaciones al
índice publicado.

Las consultas principales son:

```bash
Neocortex --code-status --code-json
Neocortex --code-search "dónde se valida SQLite" --code-search-mode hybrid
Neocortex --code-projects --code-json
Neocortex --code-reconstruct PROJECT_OR_ID --code-json
```

Estas operaciones trabajan sólo con contenido Code y respetan los límites de
la ruta. No ejecutan review interno, experimentos, proveedores externos,
autoanálisis ni receipts de calidad. Para revisar el desarrollo, Codex usa
directamente `pytest`, Ruff, Pyright/Mypy, Semgrep u otra herramienta que
corresponda, sin un comando agregador dentro de NeoCortex.

## Reanudación

`--status` muestra runs, rutas y fases con un límite predeterminado de cinco. Se
puede ampliar hasta 1000:

```powershell
Neocortex --status --status-limit 20
Neocortex --status --status-run 40 --status-json
```

Para continuar fases incompletas de un run cuyo snapshot siga retenido:

```powershell
Neocortex --resume-run 40
```

La reanudación implica `--route-only`. No ejecuta el inventario común ni
acciones de archivos. Si el snapshot falta, es incompatible o quedó obsoleto,
la operación debe abstenerse; no reconstruya filas SQLite manualmente.

Code puede reutilizar directamente un inventario durable aunque el snapshot
conserve cero candidatos MIME:

```powershell
$State = 'C:\Estado\Neocortex'
Neocortex --root $Root --state-directory $State --route code --route-only
Neocortex --root $Root --state-directory $State --route code --route-only --candidate-run 40
Neocortex --root $Root --state-directory $State --resume-run 40
```

La selección pública predeterminada es `--code-scope projects`: el inventario
sigue cubriendo el corpus compartido, pero Code sólo consume árboles con un
manifiesto de proyecto y omite dependencias, caches y outputs. Use
`--code-scope broad` únicamente cuando se quiera analizar deliberadamente
código suelto fuera de proyectos.

Sin `--candidate-run`, se examina el owner durable más reciente de la raíz
exacta y se exige modo `normal`; una discrepancia falla sin retroceder a un run
histórico por tener candidatos. Cero candidatos sólo se
admite cuando **todas** las rutas seleccionadas declaran
`input_source=inventory_snapshot`; una ruta MIME o selección mixta falla antes
de crear o ejecutar el nuevo run.

Un run actual se vuelve reanudable sólo después de que terminó de generar todos
los candidatos y publicó atómicamente su `scan_id`, conteos y evento de
enrutamiento. Al abrirlo de nuevo se validan raíz normalizada, identidad física
de la raíz, scan completo sin errores y conteo de archivos. Para un run legacy
interrumpido sin vínculo se exige además evidencia de inventario única y al
menos un `route_run` durable; si una comprobación falla, ejecute una corrida
nueva en vez de forzar la reanudación.

## Watcher incremental en primer plano

El watcher vive exclusivamente en el proceso y terminal actuales. No instala
servicios, tareas programadas ni procesos desprendidos.

Actívelo sólo después de aprobar, para una ruta, el piloto y su segunda corrida
incremental. El watcher actual dispara corridas de contenido y catálogo; no
ejecuta `--semantic-index` ni `--semantic-classify` y todavía no demuestra el
daemon multimodal completo.

```powershell
Neocortex --root $Root --watch --route pdf
```

Opciones y valores predeterminados:

| Opción | Predeterminado | Contrato |
|---|---:|---|
| `--watch-bootstrap` | `if-needed` | Bootstrap siempre, cuando sea necesario o nunca. |
| `--watch-poll-timeout-seconds` | `1` | De 1 a 300 segundos. |
| `--watch-debounce-seconds` | `2` | Puede ser cero. |
| `--watch-max-debounce-seconds` | `30` | Positivo y no menor que debounce. |
| `--watch-error-backoff-initial-seconds` | `1` | Puede ser cero. |
| `--watch-error-backoff-max-seconds` | `60` | No menor que el inicial. |
| `--watch-error-backoff-multiplier` | `2` | Mínimo 1. |
| `--watch-portable-interval-seconds` | `300` | De 1 a 86 400; sólo gobierna el recorrido normal cuando no hay USN. |

Ejemplo con política explícita para una ruta ya aprobada:

```powershell
Neocortex --root $Root --watch --route pdf `
  --watch-bootstrap if-needed `
  --watch-poll-timeout-seconds 2 `
  --watch-debounce-seconds 1 `
  --watch-max-debounce-seconds 15
```

El watcher rechaza `--apply`, `--route-only`, `--resume-run` y
`--candidate-run`. Los cambios USN actúan como señales para nuevas corridas;
no convierten el journal en un backup ni prueban por sí solos que una
exploración parcial sea completa. Sin cursor USN compatible, espera el intervalo
portable y ejecuta la corrida integrada normal: el inventario vuelve a recorrer
la raíz, mientras las rutas reutilizan sus caches por identidad y versión. No se
crea un cursor sintético, un datastore ni un indexador paralelo.
La recarga del owner durable entre ciclos usa una instantánea immutable cercada:
no crea `framework.sqlite3-wal/-shm` sobre una publicación quiescente y se
abstiene con el backoff normal si detecta un writer o sidecars activos.

Durante toda su vida adquiere un lease del sistema operativo por la combinación
canónica de raíz y directorio de estado. El archivo
`watcher-life-xxh3-128-<digest>.lock` conserva metadatos acotados de PID, tiempo
de creación, host, versión, argv, raíz/estado e inicio. Un segundo watcher con
la misma identidad se abstiene y devuelve `2`; otra raíz puede operar sin
colisión. El byte lock, no el JSON, determina ownership y se libera al cerrar o
caer el proceso. No borre el archivo: un owner stale se reemplaza sólo después
de que el nuevo proceso adquiere el lock. Esta exclusión no mata procesos ni
reemplaza `framework.lock`, que sigue protegiendo cada corrida integrada.
En el fixture sintético comparable, adquirir y persistir el lease costó
aproximadamente 11.86 ms una sola vez al iniciar el watcher; no es una medición
del corpus vivo.

### Cancelación del watcher

- El primer `Ctrl+C` solicita cancelación cooperativa y despierta las esperas de
  recursos.
- Un segundo `Ctrl+C` vuelve a interrumpir el hilo principal si el cierre no
  concluye.
- La cancelación interactiva termina con código `130`.
- Errores de fuente o corridas fallidas retenidas producen código `2`.

No cierre procesos por coincidencia amplia de nombre. Si fuera indispensable
intervenir, confirme PID, proceso padre y línea de comandos y actúe sólo sobre
el proceso propio.

## Límites y recursos

Los valores siguientes son contratos predeterminados del parser/configuración,
no promesas de RSS real. Los presupuestos son admisión estimada; bibliotecas
nativas y procesos hijos también consumen memoria.

| Ruta | Límites predeterminados relevantes |
|---|---|
| PDF | 4 workers y 2 permisos OCR; render máximo 40 000 000 píxeles por página; texto máximo 5 000 000 caracteres por página; timeout base 600 s en modo adaptativo, máximo 1200 s; reserva mínima 512 MiB por worker; máximo 2 documentos sobre 128 MiB. No hay límite predeterminado de cantidad, tamaño ni páginas. |
| DOCX | Texto máximo 20 000 000 caracteres; presupuesto 512 MiB; margen físico y de commit de 1024 MiB; espera 60 s. Sin límite predeterminado de tamaño o cantidad. |
| Office | Texto máximo 20 000 000 caracteres; presupuesto 512 MiB; margen físico y de commit de 1024 MiB; espera 60 s. Sin límite predeterminado de tamaño o cantidad. |
| ZIP | Profundidad 5; 20 000 miembros visibles; directorio central 32 MiB; 64 MiB por miembro; 512 MiB expandidos y 20 000 000 caracteres por contenedor; ratio 200; PDF interno de hasta 500 páginas con worker de 768 MiB/60 s; OCR hasta 50 páginas, 200 dpi, 40 000 000 píxeles y 30 s por llamada. Sin límite predeterminado de ZIP físicos. |
| Texto | 64 MB decimales por archivo; 4 000 000 caracteres; conversor Office heredado aislado con 1024 MiB y 60 s. Sin límite predeterminado de cantidad. |
| Imagen | 4 workers; presupuesto 512 MiB; margen físico y de commit de 1024 MiB; espera 60 s; timeout de worker 120 s y OCR documental 12 s; cada imagen seleccionada obtiene/reutiliza huella completa XXH3-128 en Dedup. `--image-max-count` limita candidatos completos, incluidos cache hits. Sin límite predeterminado de tamaño o cantidad. |
| Audio | Duración máxima 6 h; transcripción máxima 5 000 000 caracteres y 100 000 segmentos; timeout por archivo 3600 s; arranque de worker 1800 s; reserva declarada de worker 4096 MiB, presupuesto de ruta 2048 MiB, márgenes físico/commit de 2048 MiB y espera 300 s. Sin límite predeterminado de tamaño o cantidad. |
| Video | Duración máxima 6 h; 48 frames; intervalos de 30 s más escenas/keyframes; 2 073 600 píxeles por frame y lado 1920; 40 MP OCR totales, 16 KiB OCR por frame, scratch máximo 512 MiB; probe 30 s, discovery 60 s, frame 20 s, archivo 300 s y worker 2 GiB. |
| Código | Archivo máximo 8 MiB; texto máximo 4 000 000 caracteres; chunks de 12 000 caracteres; sin límite predeterminado de cantidad; scope `projects` excluye dependencias, generado y vendorizado salvo inclusión explícita. |

Un piloto Video debe incluir al menos un clip audio+visual y uno visual-only,
comprobar timestamps contra FFprobe y repetir la corrida. En visual-only, Audio
debe publicar `no_audio` sin cargar Whisper y Video debe terminar completo; un
archivo cuyo MIME sea realmente audio conserva el error si carece de stream.
`--video-doctor` verifica FFmpeg/FFprobe y los idiomas OCR sin crear estado.

El coordinador global usa por defecto un máximo de carga CPU del 90 % y una
espera de recursos de 300 s; los presupuestos globales de memoria, commit y
slots CPU se calculan cuando no se fijan explícitamente.

Para una primera ejecución use límites de tamaño/cantidad compatibles con la
ruta. Los valores `--*-max-mb` usan megabytes decimales; en PDF `1000` equivale
a 1 GB:

```powershell
Neocortex --root $Root --route pdf --MaxMB 1000 --MaxCount 25
Neocortex --root $Root --route archive --archive-max-mb 1000 --archive-max-count 25
Neocortex --root $Root --route text --text-max-mb 64 --text-max-count 25
Neocortex --root $Root --route image --image-max-mb 100 --image-max-count 100
Neocortex --root $Root --route video --video-max-count 25
Neocortex --root $Root --route code --code-max-count 500
```

No reduzca OCR, límites de texto o validación de caché para declarar éxito sin
registrar que cambió la carga y el contrato de resultados.

En PDF e imagen, el productor que abre el stream de candidatos debe consumirlo
y cerrarlo en su propio thread. Un fallo de admisión, una excepción o una
cancelación se desenrollan mediante el `finally` de ese productor; el
coordinador no debe cerrar el generator desde otro thread.

### Ruta code: cache y grafo estable

Un hit con la misma ruta actualiza presencia y observación, pero ejecuta cero
DML sobre `code_fts`. Si cambia la ruta, no es un hit: se procesa una versión
sucesora y la anterior queda como historia. Los hits de resultados `partial` o
`error` conservan esos contadores; `--retry-code-errors` solicita reprocesarlos.

El fastpath del grafo sólo aplica a una corrida completa de `code`, sin
`--code-max-count` ni filtros de selección. Primero se ejecuta `mark_missing`;
si no hubo invalidaciones ni trabajo nuevo, todos los candidatos fueron hits
compatibles con el runtime y el run completo inmediatamente anterior publicó el
fence tipado exacto con `resolver_signature=code-graph-resolver-v4`, se reutiliza
el conteo de proyectos. Esa versión resuelve símbolos y dependencias mediante
conjuntos temporales indexados, prioriza ámbito local y rutas relativas exactas,
y sincroniza los labels FTS distintos en una pasada, no con una consulta o
actualización por relación o versión. Una
base existente sin fence, un run intermedio, un manifest/move o cualquier
evidencia malformada fuerzan `finalize_graph` y reconstruyen membresías y FTS.

La primera corrida completa posterior a esta actualización puede por ello
realizar una finalización larga; las siguientes sólo prueban estado estable si
usan el mismo corpus, configuración y firma. El esquema vigente es 4. Durante
`finalize_graph`, un progress handler SQLite acotado consulta cancelación dentro
de la transacción, revierte antes de propagar la excepción original y se retira
al salir; esto no convierte el grafo en una publicación generacional.

## Modelos y herramientas externas

Las dependencias de producto se consultan mediante sus doctores y rutas propias:

```bash
Neocortex --pdf-doctor
Neocortex --audio-doctor
Neocortex --video-doctor --video-ocr-profile auto-multilingual
Neocortex models status --json
```

Tesseract, FFprobe/FFmpeg, LibreOffice y qpdf se ejecutan sólo cuando la ruta de
contenido correspondiente los necesita, con límites de tiempo, memoria y salida.
Whisper, Jina, MiniLM y CLIP se preparan de forma explícita mediante el dominio
Semantic; `models status` es sólo lectura y nunca descarga modelos. Una
herramienta ausente deja una razón visible y no se sustituye por resultados
inventados.

## Verificación del desarrollo fuera del runtime

NeoCortex no mantiene una plataforma productiva de autoanálisis ni un quality
gate agregador. La comprobación se hace directamente y de forma proporcional:

- `pytest` para regresiones de comportamiento e integración;
- Ruff y Pyright/Mypy para estática y tipos;
- Semgrep sólo para invariantes que no estén expresadas por una prueba directa;
- imports/ciclos acotados cuando cambie la organización;
- release y launcher únicamente si el alcance incluye empaquetado o instalación.

Una prueba focal sirve para diagnosticar el bloque que se está editando, no
para declarar todo el repositorio aceptado. Los resultados no se escriben en el
estado Code ni se convierten en receipts productivos. `pip-audit` no se ejecuta
implícitamente; una auditoría advisory remota requiere una solicitud expresa y
un alcance mínimo.

## Cancelación de una corrida normal

El primer `Ctrl+C` solicita cierre cooperativo. La corrida se registra como
`cancelled`, distinta de `failed`, y el launcher devuelve `130`. Espere la
liberación de workers y procesos hijos antes de iniciar otra corrida con el
mismo estado.

La implementación histórica Windows usa Job Objects, pero permanece fuera del
alcance activo y no constituye una barrera vigente.

En Linux, cada subproceso usa una sesión/grupo propio; timeout y cancelación
terminan hijos y nietos con `SIGTERM` y después `SIGKILL`. Los límites de memoria
se imponen con `RLIMIT_AS` o `/usr/bin/prlimit`; si se solicita uno y no puede
imponerse, la operación se abstiene en vez de ejecutar sin contención.

Si se interrumpió una operación autorizada sobre archivos, **no la repita
automáticamente**. Siga la sección de acciones inciertas de
[RECOVERY.md](RECOVERY.md).

En `0.9.0`, los rename y movimientos admitidos son únicamente de archivos
regulares con un hard link en NTFS local y mismo volumen, mediante handles
retenidos y sin reemplazo. Los demás casos se abstienen. La aplicación de
candidatos de Papelera está deshabilitada; el dry-run continúa registrando el
plan y un `--apply` los marca `skipped` sin llamar a `Send2Trash`.

Linux no expone aún ese backend de mutación. `--apply` y
`--organization-apply` se rechazan antes de crear estado con salida `2` y razón
`linux_mutation_backend_unavailable`.

## Diagnóstico operativo

Diagnóstico cotidiano mínimo, sin modificar el corpus:

```powershell
Neocortex --version
Neocortex doctor capabilities
Neocortex doctor platform --json
Neocortex models status --json
Neocortex --status --status-limit 20
```

Añada únicamente el status o doctor de la capacidad que está usando, por
ejemplo `--knowledge-status` o `--semantic-status`. `pip check`, recovery,
retención y todos los doctors se reservan para fallos de dependencias,
operaciones inciertas o validación de una instalación.

Preserve la salida exacta, código de salida, hora, versión y `run_id`. No adjunte
contenido confidencial del corpus a diagnósticos sin autorización.

`--action-recovery-status` abre sólo la base existente y clasifica
`applying`/`recovery_required` sin escribir ni repetir operaciones. Use
`--action-recovery-after` para paginar, `--action-recovery-run` para acotar y
`--action-recovery-json` para JSON Lines. Devuelve `2` si una fila es ambigua o
imposible de comprobar; `confirmed` y `not_performed` siguen requiriendo una
decisión humana antes de cualquier cambio persistente.

Para conservar la observación, no la mutación, use después un `record`
explícito con actor y confirmación:

```powershell
Neocortex --action-recovery-record 42 --action-recovery-actor "Victor" --confirm-reconciliation-record --action-recovery-json
```

El evento es append-only e idempotente; `--action-recovery-expected-event`
protege una observación posterior mediante CAS. Un código `2` puede acompañar
un registro correcto si la clasificación sigue ambigua o imposible. Verifique
la salida y el `event_id`. No existe todavía un comando de recuperación o
verificación y ningún evento autoriza por sí mismo una mutación.

Los planes documentales `recovery_required` tampoco se reintentan y conservan
reservado su destino:

```powershell
Neocortex --organization-preview 100 --organization-preview-status recovery_required
```

## Crecimiento y mantenimiento

Obtenga primero un plan de sólo lectura. La edad es deliberadamente explícita;
si se omite, no se declara elegibilidad por antigüedad:

```powershell
Neocortex --retention-status
Neocortex --retention-status --retention-store semantic --retention-store catalog --retention-min-age-days 30 --retention-batch-size 100
```

El resultado protege como mínimo las publicaciones vigente y anterior,
builders y leases vivos, bases de generaciones, checkpoints, el último run
`completed` de framework aunque haya runs fallidos o cancelados posteriores, y
evidencia humana o incierta. Una referencia desde `semantic_evidence` es un
hold y bloquea la elegibilidad de esa generación. Se pagina con cursores
`--retention-<store>-after`. Los bytes son una cota inferior del payload SQLite
y el snapshot no es atómico entre bases. Un store con deriva queda `blocked` y
el comando devuelve `2`.

`--retention-status` es exclusivamente read-only/dry-run. No existen comandos
productivos `--retention-prepare`, `--retention-apply` ni
`--retention-verify`; el plan tampoco autoriza `DELETE` manuales. La ejecución
genérica permanece bloqueada hasta que las referencias cross-DB tengan holds
write-ahead durables y cada propietario disponga de journal reanudable e
idempotente. SQLite no proporciona una transacción atómica entre esas bases.

- Las rutas podan determinadas cachés obsoletas sólo después de una corrida
  satisfactoria; no todas las tablas históricas tienen una política global de
  retención demostrada.
- La poda legacy del inventario es una operación específica del propietario,
  separada de `--retention-status`. El coordinador debe entregarle todos los
  holds cross-store explícitos; si no puede hacerlo, falla cerrado sin borrar.
  Conserva siempre la publicación actual y la anterior de cada raíz, además de
  builders, candidatos y scans referenciados.
- Catálogo v6 y semántica v6 preservan la generación publicada durante staging,
  fallo o cancelación. Existe un planificador dry-run, pero no una poda ni
  enforcement de cuotas para generaciones fallidas, canceladas, superseded,
  `ready_partial` o builds abandonados.

### Borrado explícito de bases

Para retirar todo el estado derivado sin tocar el corpus, use primero la vista
previa:

```bash
Neocortex databases purge --json
Neocortex databases purge --store semantic --store image --json
```

La ejecución destructiva sólo se habilita con el token literal y crea un backup
SQLite verificado antes de eliminar:

```bash
Neocortex databases purge --apply \
  --confirm-database-purge DELETE_DATABASES
```

El comando cubre únicamente las bases canónicas registradas y sus sidecars,
adquiere locks de framework/release y de routes/watcher, revalida identidad y
preserva releases, modelos, recibos, locks y archivos SQLite no reconocidos. Un
writer activo, un backup fallido, un symlink o un cambio de snapshot detienen la
operación sin continuar con el resto de los archivos; el manifest del backup
queda junto a la copia para recuperación posterior.

- Supervise tamaño de `.sqlite3`, `-wal`, cachés de modelos y espacio libre.
- No elimine generaciones, runs, modelos, WAL o SHM por antigüedad aparente.
- No ejecute `VACUUM`, checkpoints, cambios de `journal_mode` ni manipulación
  de `PRAGMA user_version` como mantenimiento rutinario.
- Antes de cualquier intervención, detenga writers y cree un backup mediante la
  API SQLite según [RECOVERY.md](RECOVERY.md).
- Un WAL que crece requiere identificar primero el writer/lector que impide el
  checkpoint; no se corrige borrando el archivo.

## Actualización y rollback (procedimiento condicional)

Esta sección se usa sólo al instalar, promover, migrar o restaurar una versión.
No forma parte del flujo cotidiano ni de una corrección focal.

En Kubuntu/Linux, la herramienta mantenida construye y verifica una release
inmutable, activa `current` bajo `flock`, conserva las anteriores y escribe un
recibo en el estado:

```bash
python3.14 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --prepare-models --desktop
python3.14 tools/release_linux.py verify
python3.14 tools/release_linux.py rollback
```

Una preparación incompleta de modelos no promueve el runtime ni publica KDE.
Rollback cambia sólo el enlace activo; no elimina releases ni sustituye la
recuperación de bases.

La release CPython 3.14 consume conjuntamente `constraints.txt` y
`constraints-linux-cp314.lock`. El segundo archivo fija el inventario transitivo
Linux completo, se conserva dentro de la release y debe coincidir por nombre,
versión, cantidad y SHA-256 antes de promover o verificar el artefacto.

1. Termine sólo los procesos propios de NeoCortex y confirme que no quede un
   watcher activo.
2. Capture `Neocortex --version` y `Neocortex --status --status-json`.
3. Cree un backup consistente de todas las bases.
4. Instale el artefacto ya validado conforme al README de la entrega.
5. Compruebe versión, ayuda, dependencias y doctors antes de abrir estado real.
6. Permita migraciones únicamente con la versión compatible y conserve el
   backup previo.
7. Si la actualización falla, no reduzca números de esquema. Restaure el paquete
   compatible y las bases completas siguiendo [RECOVERY.md](RECOVERY.md).

Una instalación limpia en un entorno temporal valida el paquete, pero no
actualiza por sí sola el launcher operativo del sistema. Compruebe ambos de
forma independiente.

La actualización `0.5.0` eleva `framework.sqlite3` 17→18,
`semantic.sqlite3` 5→6 y `document_catalog.sqlite3` 5→6. Las migraciones
preservan datos y se abstienen ante contratos v5/v17 desconocidos, pero no
ofrecen downgrade. El rollback exige restaurar el conjunto respaldado y el
paquete compatible; nunca edite los números de esquema.

La actualización `0.6.0` eleva únicamente `framework.sqlite3` 18→19 y agrega
el log de conciliación vacío. La operación `--action-recovery-record` puede
aplicar esta migración aditiva a una base existente después de la confirmación;
`status` y retención nunca migran. El rollback sigue requiriendo restaurar la
copia consistente y el paquete 0.5.0, no editar `schema_version`.

La actualización `0.7.0` no eleva ningún esquema ni crea una base Knowledge.
Sus comandos `--knowledge-status`, `--knowledge-search` y
`--knowledge-context` abren únicamente los propietarios ya existentes en modo
de solo lectura. Por tanto, un rollback del paquete a `0.6.0` no requiere un
downgrade de base atribuible a Knowledge; cualquier otra migración o cambio de
estado realizado por comandos distintos conserva su propio contrato de
recuperación.

La fuente `0.9.0` declara framework v22, Dedup v10, PDF v13, DOCX v6, Office
v3, Audio v2, Video v2 y catálogo v7. Framework 19→20 preserva
filas legacy como `normal`; Dedup 7→8 agrega la firma cruda de inventario a los
scans, conserva scans/archivos/bytes e invalida checkpoints sin firma en vez de
inventar evidencia. Dedup 8→9 conserva esas publicaciones y permite que
`volume`, `journal_id` y `next_usn` sean todos `NULL` o todos presentes, para
separar publicación de aceleración USN. Dedup 9→10 añade únicamente los índices
para joins Knowledge ligados a identidad y preserva conteos y bytes de archivos
y miembros planeados. PDF 11→12 y Office 1→2 fueron migraciones aditivas; las
migraciones posteriores de paths reconstruyen cada owner con la collation de
la plataforma, sin fusionar filas Linux case-distinct. Video se crea sólo al
ejecutar su ruta. Ninguna migración ofrece downgrade. Abra bases vivas sólo
con el runtime versionado validado; el rollback exige paquete compatible y
backup completo, nunca editar `schema_version`.

## Auditorías técnicas

Cuando Victor solicite explícitamente una auditoría integral o un cierre de
release, debe conservar los informes anteriores y seguir
[AUDIT_REPORTING_STANDARD.md](AUDIT_REPORTING_STANDARD.md) para evidencia,
manifiesto, barrera y cierre visible. Ese estándar no aplica a documentación,
configuración, correcciones focales ni slices verticales ordinarios.
