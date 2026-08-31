# NeoCortex

NeoCortex es un framework incremental para descubrir, identificar, extraer,
clasificar, revisar y buscar documentos, imágenes, audio y código mediante un
inventario y estado compartidos. Conserva evidencia, incertidumbre y versiones
de procesamiento. El modo predeterminado no modifica el corpus.

## Empieza aquí

El flujo personal normal es deliberadamente corto:

1. Comprueba el runtime y el estado de la capacidad que necesitas.
2. Busca primero en el estado ya publicado.
3. Si falta cobertura, procesa una sola ruta sobre 20–50 archivos con un límite
   duro de 10–15 minutos.
4. Verifica una salida útil y repite la misma corrida para comprobar que es
   incremental.
5. Escala sólo después de revisar resultados, errores, velocidad y proyección.

La incrementalidad no se presume para providers declarados
`non_replayable`: esos candidatos deben volver a ejecutarse y mostrar ese coste
explícitamente, aunque el resto de la corrida reutilice estado compatible.

`--all`, el watcher, una indexación Semantic completa, una auditoría integral y
un ciclo de release no son pruebas iniciales. Si el piloto no produce algo útil,
se detiene y se corrige. La continuación técnica vigente, con el estado
observado y la siguiente acción única, está en el
[handoff operativo vigente](https://github.com/victor982721-lab/Neocortex/blob/main/.codex/handoffs/NEOCORTEX_0.7.2_PAUSE_2026-07-30.md).

Esta precaución aplica al arranque y diagnóstico, no elimina la experiencia
simple buscada. El flujo cotidiano vigente es `Neocortex --all` en Linux:
inventario, procesamiento y búsqueda están disponibles, pero `--apply` y
`--organization-apply` se abstienen deliberadamente hasta que exista un backend
ext4 con garantías equivalentes.

`--all` ejecuta el flujo documental y no consulta ni produce evidencia de
autoanálisis del repositorio. Si la raíz documental no es utilizable, la etapa
de corpus termina con `corpus_unavailable`, nunca con un traceback.

Después de esa consulta, `Neocortex --all` también avanza Semantic sobre las
cachés durables disponibles. El canal textual incluye PDF, DOCX, XLSX, PPTX,
ODT, audio, miembros de ZIP y la ruta de texto/correo; si existe la caché de
imágenes, el mismo presupuesto publica además CLIP visión y el OCR retenido.
Usa límites reanudables de 100 000 items, 1 000 000 de jobs y 48 horas. Code no
entra implícitamente: es un corpus de análisis distinto que puede dominar el
presupuesto documental. Para incluirlo deliberadamente se usa
`--all --semantic-source code`. No se presupone ningún conteo histórico de
vectores o chunks: `--semantic-status` es la lectura del estado realmente
publicado.

Cuando las fuentes durables no cambiaron, Semantic reutiliza el head publicado
mediante una proyección compacta y no vuelve a enumerar, descomprimir, fragmentar
ni cargar modelos. La salida distingue `mode=exact_replay` y exige
`sources_enumerated=0`, `items=0`, `chunks=0` y `new_jobs=0` para demostrar ese
replay. Code conserva su caché entre perfiles de análisis y agrupa en lotes las
actualizaciones de observación.

La ruta Code usa `--code-scope projects` de forma predeterminada: descubre
raíces por manifiestos de proyecto (`pyproject.toml`, `package.json`,
`Cargo.toml`, soluciones de Visual Studio y equivalentes) y sólo admite
artefactos dentro de esas raíces. Dependencias instaladas, caches y salidas de
build quedan fuera; `--code-generated` y `--code-vendored` son inclusiones
deliberadas. `--code-scope broad` conserva únicamente como override explícito
la selección textual histórica de todo el perfil. La salida informa raíces y
descartes por causa para que la cobertura no quede implícita.

### Consulta cotidiana de solo lectura

La experiencia normal ya no exige recordar los flags internos. Estos comandos
consultan únicamente publicaciones existentes, usan scopes fijos y no crean,
migran ni reprocesan estado:

```bash
Neocortex help
Neocortex status --scope all
Neocortex search "pruebas eléctricas del transformador U5" --scope personal
Neocortex ask "¿qué evidencia existe sobre el tratamiento de aceite?" --scope personal
Neocortex inspect code "dónde se valida SQLite" --scope personal
Neocortex inspect lineage IDENTIFICADOR --scope personal
Neocortex review value --scope personal --limit 50
```

`personal` consulta el estado publicado y `all` conserva la forma de consulta
federada sin mezclar scores de snapshots distintos. La
página **Consulta** de `Neocortex --ui` ofrece las mismas acciones, evidencia,
citas, cobertura e incertidumbre sin controles de mutación. Para clientes
locales, `Neocortex agent serve` expone por MCP/stdio sólo `status`, `search`,
`context`, `evidence` e `inspect_code`, todos marcados read-only. Las interfaces
retiradas sólo se conservan en documentación histórica y no forman parte del
contrato de automatización vigente.
`inspect lineage` explica receipts, revisiones, materializaciones y dependencias
Text/Semantic ya publicadas; tampoco ejecuta extractores ni migra owners.

`review value` conserva esa misma frontera read-only: si existe una cola
`ReviewTask` vigente en Framework la consulta; si todavía no existe, usa el
preview legacy sobre publicaciones sin crear ni migrar estado. La construcción
durable es una operación distinta y explícita:

```bash
Neocortex review value --refresh --scope personal --limit 50
```

`--refresh` sólo admite `personal` o `framework`, avanza exactamente una página
keyset de 100 observaciones y puede crear o migrar `framework.sqlite3` a schema
21. No abre una autorización de mutación: escribe únicamente batches,
memberships, tareas, eventos, progreso y el head generacional de revisión en el
owner Framework; nunca mueve, renombra, archiva ni elimina archivos. Un corpus
de más de 25,000 observaciones se recorre
por páginas sucesivas, y un cambio del snapshot fuente deja la cola `stale` en
vez de mezclar evidencia. Las decisiones humanas `RESOLVED`/`DISMISSED` se
preservan y no se reabren automáticamente al refrescar. Terminar el cursor no
equivale por sí solo a tener evidencia completa: si falta un owner, una
publicación o una fila no concuerda con su snapshot, la cola conserva
`scan_complete=true` pero `evidence_complete=false`, permanece `partial` y no
retira hallazgos abiertos por ausencia de evidencia.

La selección del extractor Text también puede inspeccionarse antes de procesar
un archivo. Es una consulta local, sin modelos ni escritura de estado:

```bash
Neocortex doctor capabilities --select text.extract \
  --mime-type text/plain --input-bytes 4096
```

La salida explica la implementación elegida o cada causa de abstención. El
diagnóstico agregado `Neocortex doctor capabilities [--json]` conserva su
contrato schema 1; la selección por trabajo sólo se activa con `--select`.

## Topología canónica por usuario

La fuente, el runtime y el estado ocupan árboles separados en Kubuntu/Linux:

```text
Fuente:       ~/Neocortex/Repository
Corpus:       ~/Documentos/NeoCortex/Corpus
Release:      ~/.local/share/Neocortex/releases/<version>-<sha12>-cp314-linux-x86_64
Activa:       ~/.local/share/Neocortex/current
Launcher:     ~/.local/share/Neocortex/bin/Neocortex
Alias:        ~/.local/bin/Neocortex
Estado:       ~/.local/state/Neocortex/state
Modelos:      ~/.local/share/Neocortex/models
```

Las rutas Linux respetan `XDG_CONFIG_HOME`, `XDG_STATE_HOME` y
`XDG_DATA_HOME`; el directorio Documentos se resuelve de forma segura desde
`user-dirs.dirs`. Cada runtime es versionado e inmutable. El launcher estable
sólo se promueve después de validar el artefacto y su entorno aislado. La
invocación pública es `Neocortex`.

## Instalación compatible

La plataforma activa es Kubuntu/Linux con CPython `>=3.13,<3.15`. Windows queda
como legado sin soporte ni validación vigente. No instale el paquete ni `pip`
contra runtimes globales.

Los extras `agent` y `analysis` son superficies de distribución para la API MCP
y los analizadores de desarrollo, no elecciones operativas que Víctor deba
administrar. Las releases personales siguen instalando `full`, que reúne la
API MCP con las capacidades documentales, multimedia, Semantic y UI; las
herramientas de calidad permanecen fuera del runtime.

La instalación y los gates locales autentican `pip` antes de instalar otra
dependencia. El comando canónico no depende del `pip` ambiental: descarga el
wheel oficial `26.2.1`, exige su nombre y SHA-256 fijados, lo instala sin índice
ni dependencias y verifica la versión bajo Python aislado:

```bash
python -I tools/bootstrap_pip.py
```

Para una instalación offline, el mismo helper acepta `--wheel` con el wheel
canónico ya disponible y conserva la validación exacta.

### Kubuntu/Linux

La referencia local es Kubuntu/Ubuntu 26.04, Linux x86-64 y CPython 3.14.4. El
instalador mantenido construye el wheel, crea una release inmutable, instala el
extra `full` sólo desde wheels binarios, prepara modelos de forma explícita y
publica KDE al final:

```bash
cd "$HOME/Neocortex/Repository"
python3.14 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --prepare-models \
  --desktop
python3.14 tools/release_linux.py verify
```

La instalación prepara `--corpus-root` como directorio real si todavía no
existe. No copia documentos ni inicia procesamiento; sólo garantiza que el
comando cotidiano tenga una raíz válida desde su primera ejecución.

`constraints.txt` conserva los pins directos compartidos, mientras
`constraints-linux-cp314.lock` fija el inventario transitivo completo de la
release Linux. El instalador aplica ambos constraints, incorpora el lock y su
SHA-256 al manifest y rechaza la promoción si una distribución instalada difiere;
un cambio del lock exige volver a comprobar el artefacto, sin convertirlo en un
gate de calidad del producto.

Los modelos se comparten entre releases. `Neocortex models status --json` es
local y de sólo lectura; `Neocortex models prepare --json` es la única fachada
que descarga el conjunto de producción deliberadamente. La entrada KDE muestra
“modo portátil Linux”, no solicita elevación y mantiene desactivadas las
mutaciones. Consulte [Kubuntu/Linux](docs/LINUX_KUBUNTU.md) para requisitos,
rutas XDG, recibos y rollback.

Para el uso personal de Víctor, `full` es la instalación canónica: el comando
`Neocortex` debe exponer documentos, audio, imagen, Semantic y UI sin exigirle
elegir perfiles. Si una capacidad central aparece `unavailable`, se repara la
instalación o su declaración antes de operar; no se trata como una decisión
cotidiana del usuario.

La base y los extras se conservan como superficies de empaquetado y desarrollo,
no como decisiones cotidianas:

| Superficie | Runtime añadido |
|---|---|
| base (sin extra) | `packaging`, `rich` y `xxhash` para el comando mínimo |
| `agent` | servidor MCP/stdio con MCP `1.29.0` |
| `analysis` (sólo desarrollo) | Complexipy, Cosmic Ray, Coverage, Deptry, Grimp, Mypy, pip-audit, Pytest, Radon, Ruff y Vulture |
| `documents` | PDF, fallback pdfminer y OCR documental |
| `audio` | transcripción local con faster-whisper |
| `image` | decodificación Pillow, OCR y semántica visual |
| `semantic` | embeddings texto/imagen con FastEmbed y NumPy |
| `ui` | interfaz PySide6 |
| `full` | unión compatible de `agent` y los cinco dominios de producto |

Algunas rutas conservan prerrequisitos externos que ningún extra de Python
puede instalar: `ffprobe` es obligatorio para `audio`; `tesseract` y `qpdf`
habilitan OCR y recuperación PDF degradables en `documents`; y `tesseract`
habilita el OCR documental degradable en `image`. El probe ligero sólo busca
estos ejecutables en `PATH`; no interpreta overrides de una ejecución concreta.

Los wheels nativos del perfil `full` requieren el Microsoft Visual C++ v14
Redistributable x64 vigente. Después de instalarlo, valida en el mismo runtime
los imports de PyMuPDF, ONNX Runtime, PySide6, PyAV y CTranslate2 antes de
promover el launcher; `pip check` por sí solo no detecta una DLL del sistema
ausente.

Para desarrollo sobre el runtime completo:

```powershell
& "$Venv\Scripts\python.exe" -m pip install -c constraints.txt ".[full,dev]"
```

Los mantenedores pueden combinar dominios para probar el empaquetado, por
ejemplo `.[documents,audio]`. Esa modularidad no cambia el producto personal
canónico. Pillow se declara directamente en `documents`, `image` y `semantic`
porque los tres dominios lo importan en sus rutas propias.

La API pública ligera permite inspeccionar prerrequisitos sin importar engines
ni cargar o descargar modelos:

```python
from neocortex.capabilities import inspect_runtime_capabilities

for capability in inspect_runtime_capabilities():
    print(capability.capability, capability.state.value)
```

La misma inspección está disponible como doctor canónico de sólo lectura:

```powershell
Neocortex doctor capabilities
Neocortex doctor capabilities --json
Neocortex doctor platform --json
```

El doctor usa únicamente declaraciones, metadata de distribuciones, specs de
import y resolución de ejecutables. No importa engines opcionales, no carga o
descarga modelos y no crea estado.
`doctor platform` añade un contrato versionado de sistema, rutas, inventario,
identidad, contención, elevación y backend de mutación. Una plataforma Linux es
compatible aunque informe la mutación como intencionalmente no disponible.

`available` significa que todos los componentes declarados están presentes;
`degraded`, que falta sólo una función opcional —por ejemplo OCR o fallback
PDF—; y `unavailable`, que falta un requisito obligatorio de
la capacidad. Este probe no certifica cachés de modelos, idiomas OCR ni que un
backend pueda ejecutar inferencia; esas comprobaciones profundas permanecen
separadas y offline. Sus estados representan presencia, no conformidad con los
rangos de versiones de `pyproject.toml`: esa compatibilidad la deben cerrar el
resolver del entorno y `pip check` antes de promover el runtime.

Una instalación offline sólo es hermética si el wheelhouse contiene el cierre
completo de artefactos y del backend de build; consulte
[Instalación offline](docs/OFFLINE_INSTALLATION.md). Una instalación
`--no-deps` o con `--system-site-packages` sirve para pruebas acotadas, pero no
demuestra ese cierre.

Compruebe siempre el entrypoint de ese entorno antes de operar. Tras activarlo,
la invocación canónica es `Neocortex`; sin activación puede validar el ejecutable
por su ruta exacta:

```powershell
& "$Venv\Scripts\Neocortex.exe" --version
& "$Venv\Scripts\Neocortex.exe" --help
```

`Neocortex --status` es un diagnóstico del estado persistente, no una prueba de
instalación: en un entorno nuevo sin `framework.sqlite3` devuelve `2` de forma
esperada y no crea la base.

La versión fuente de esta entrega es `0.9.0`. Si el ejecutable exacto del
runtime no informa `0.9.0` o no reconoce las opciones de esta guía, deténgase y
valide el artefacto en un entorno aislado antes de promover el launcher estable.

## Primer uso y rutas

Una corrida sin `--apply` actualiza inventarios y cachés, pero preserva los
archivos originales. El primer uso debe cubrir una sola ruta y como máximo
20–50 archivos:

```powershell
Neocortex --root C:\Datos --route pdf --MaxCount 25
```

Las rutas vigentes son `pdf`, `docx`, `office`, `archive`, `text`, `audio`,
`video`, `image` y `code`.
Las listas y `--all` se reservan para después de aprobar cada ruta y su
proyección. Las búsquedas operan sobre estado ya construido, por ejemplo:

```powershell
Neocortex --code-search "dónde se valida el acceso a SQLite" --code-search-mode hybrid
Neocortex --pdf-search "transformador AND mantenimiento"
```

### ZIP, incluidos ZIP anidados

La ruta `archive` indexa los miembros de cada ZIP y recorre ZIP anidados sin
extraerlos al filesystem. Conserva nombres, tamaños, profundidad y texto
consultable de archivos de texto, HTML/XML, documentos OOXML/ODF y EPUB. Los
PDF conservan el texto nativo y, en modo OCR `auto`, las páginas con menos de
40 caracteres se renderizan de forma acotada; las imágenes BMP/GIF/JPEG/PNG/
TIFF/WebP también se someten a OCR. Así un PDF escaneado o una imagen dentro de
un ZIP anidado puede ser buscable sin materializarse. Si Pillow, PyMuPDF,
Tesseract o los idiomas solicitados no están disponibles, el miembro y la
incidencia siguen visibles y no se inventa texto. El primer piloto debe seguir
acotado:

```bash
Neocortex --root "$Root" --route archive --archive-max-count 25 --strict-exit-codes
Neocortex --archive-status
Neocortex --archive-search "protección diferencial"
Neocortex --archive-list 50
```

Una ruta virtual como
`contenedor.zip!/subcarpeta/otro.zip!/documento.txt` indica exactamente la
cadena de contenedores. Las salidas de Archive y Knowledge muestran
`location=archive_member inside_zip=1`; un resultado normal de Knowledge usa
`location=physical inside_zip=0`. La lectura rechaza nombres absolutos o con
traversal, miembros cifrados o especiales y expansiones fuera de los límites de
profundidad, cantidad, tamaño, ratio y texto. Nunca mueve, borra ni materializa
los miembros del ZIP.

### Texto físico, correo y Office heredado

La ruta `text` cubre archivos imprimibles que antes sólo aparecían en el
inventario: TXT, Markdown, CSV/TSV, HTML, XML, JSON y formatos de texto
equivalentes. También interpreta la estructura visible de correo EML y conserva
asunto, remitente y encabezados acotados. Los DOC/XLS/PPT binarios heredados se
extraen en un proceso aislado con un único backend fijado por formato. DOC
prioriza LibreOffice y usa `catdoc` si no está disponible; XLS y PPT priorizan
respectivamente `xls2csv` y `catppt`, con LibreOffice como fallback. La
detección exige evidencia de bytes imprimibles, RFC 5322 o contenedor CFB; una
extensión por sí sola no convierte binarios arbitrarios en texto.

```bash
Neocortex --root "$Root" --route text --text-max-count 25 --strict-exit-codes
Neocortex --knowledge-status
Neocortex --knowledge-search "término representativo" --knowledge-limit 20
Neocortex --catalog-preview 25
Neocortex --curation-preview 25 --curation-json
```

`text.sqlite3` conserva texto, tipo, título, autor, firma, errores y FTS. El
catálogo puede proponer clasificación y nombres —por ejemplo, el asunto de un
EML—, pero en Linux sigue sin existir autoridad de movimiento. Semantic acepta
esta caché mediante `--semantic-source text`.

`--curation-preview` reúne en una vista acotada los planes durables de
duplicados, organización y archivos vacíos. Devuelve propuestas advisory con
identidad, evidencia, razón y fingerprint reproducible; no inicializa ni migra
SQLite, no modifica sus bytes y nunca mueve, renombra o elimina contenido.

**IMPLEMENTED — broker para Text.** Antes de extraer cada candidato, la ruta
evalúa manifests estáticos y versionados para `neocortex.text.builtin` y
`neocortex.text.legacy-office-worker`. La política vigente exige ejecución
local/offline, MIME exacto, plataforma compatible y runtime disponible; no elige
simplemente el primer provider registrado. El builtin de texto/EML sigue siendo
`environment_bound` y puede reutilizar un resultado o fallo compatible bajo las
validaciones Text existentes. DOC/XLS/PPT heredado usa el provider worker v2,
declarado `best_effort` y `non_replayable`: requiere demostrar y fijar
`soffice`/`libreoffice` o el backend específico del formato, pero sólo puede
atestar ese launcher, no la clausura transitiva arbitraria de engines,
dependencias o procesos que éste invoque. Por ello cada candidato Office
heredado vuelve a ejecutarse y nunca reutiliza un éxito ni un fallo previo.
Su manifest declara además `incremental=false`: no se presenta como una ruta que
procese únicamente cambios. Es el coste deliberado de no fingir reproducibilidad
ni cobertura transitiva. Ante ausencia de backend se abstiene, persiste un
receipt de fallo explicable y no publica outputs parciales; después de iniciar
un intento tampoco cambia silenciosamente a otra alternativa.

Los receipts Text ligan la firma de procesamiento con provider y versión,
fingerprint del manifest, política, readiness observado y fingerprint de la
selección. Para Office heredado, readiness incorpora SHA-256, tamaño e identidad
hasheada de la ubicación del ejecutable fijado; el worker los revalida antes y
después del uso. Esto liga la corrida al artefacto observado, pero no constituye
por sí solo una certificación supply-chain del proveedor ni vuelve reproducible
su ejecución transitiva. El receipt legacy conserva clase `non_replayable`; los
manifests y el broker son stdlib-only y no cargan providers, engines ni modelos
al inspeccionarlos.

**PLANNED — no implementado en este corte.** Las rutas nativas `pdf`, `docx` y
`office`, Semantic y plugins/providers externos todavía no consumen este
broker. Se incorporarán una ruta a la vez, sin reescribir extractores ni añadir
dependencias base obligatorias.

### Imágenes completas y búsqueda multimodal

La ruta `image` garantiza un XXH3-128 completo de cada imagen elegible, incluso
cuando reutiliza el análisis visual, y lo guarda en el índice Dedup compartido.
Eso permite que el plan Semantic resuelva identidad exacta en lugar de omitir
imágenes por falta de huella. Tras una ruta de imagen, `--all` incluye CLIP
visión y el OCR documental retenido cuando la caché y los modelos locales están
disponibles; texto e imagen permanecen en espacios vectoriales separados.

La recuperación visual es deliberadamente *fail-closed*: sin una calibración
positiva y negativa compatible con modelo, pipeline y backend no carga CLIP ni
devuelve vecinos. La evaluación humana actual encontró solapamiento entre
positivos y negativos; un piso suficientemente conservador sobre el estado vivo
retendría sólo 32% de los positivos. Por ello `0.9.0` no inventa un umbral ni
presenta similitud visual como confianza.

Los OCR de PDF, imagen y video conservan `spa+eng` como contrato predeterminado.
Los perfiles explícitos `latin`, `han-simplified`, `han-traditional` y
`auto-multilingual` añaden alemán, chino simplificado/tradicional y OSD con una
ruta primaria y como máximo un fallback medido; nunca envían todos los idiomas
a cada reconocimiento.

### Video acotado y trazable

La ruta `video` usa FFprobe/FFmpeg en workers contenidos para conservar streams,
duración, escenas, keyframes, frames muestreados, OCR y timestamps. Acepta tanto
video con audio como visual-only; en este último caso Audio publica `no_audio`
benigno sin cargar Whisper. Sus límites predeterminados incluyen 48 frames,
40 megapíxeles totales de OCR, 16 KiB por OCR de frame, 512 MiB de scratch y
2 GiB de memoria virtual del worker:

```bash
Neocortex --root "$Root" --route video --video-max-count 25 --strict-exit-codes
Neocortex --video-status
Neocortex --video-search "placa del transformador" --video-search-limit 20
Neocortex --video-doctor --video-ocr-profile auto-multilingual
```

El OCR de frames publicado también participa en `Neocortex search` y `ask` con
localizador temporal; no se presenta como embedding Video ni duplica la pista
Audio enlazada.

### Código con recuperación semántica integrada

Después de construir la ruta `code`, Semantic puede publicar embeddings de sus
chunks y sincronizar el puente existente de Code en la misma operación:

```powershell
Neocortex --semantic-index text --semantic-source code
Neocortex --code-search "dónde se valida el acceso a SQLite" --code-search-mode hybrid
```

La sincronización sólo acepta el head Semantic `ready` que acaba de publicarse y
liga cada chunk vigente de Code con item, modelo, espacio vectorial y generación
exactos. Un replay sin cambios no crea otra generación ni reescribe enlaces; si
cambia una versión, los enlaces anteriores quedan inactivos como historial.
`--code-status` informa enlaces activos, vigentes y obsoletos.

Al terminar una publicación Code, el productor hace checkpoint y retira los
sidecars reconstruibles sólo si el WAL quedó vacío. Si otro lector mantiene los
handles abiertos, la corrida no invalida el estado publicado, pero el status
quiescente se abstiene hasta que ese lector cierre y una corrida posterior pueda
retirar los auxiliares.
Los lectores operativos de una base quiescente usan una instantánea immutable
con cercas antes/después, por lo que búsqueda y listado ya no crean `-wal` o
`-shm`; si ya existe un writer activo, leen con SQLite read-only sin borrar ni
cerrar auxiliares ajenos.

La búsqueda `semantic` consume únicamente esos enlaces exactos. Si falta el head,
la cobertura o el modelo local, declara `CODE_SEARCH_CHANNEL available=0` con la
causa y el modo exclusivamente semántico devuelve `2`; `hybrid` conserva las
señales léxicas y estructurales disponibles. El score es similitud no calibrada y
sólo evidencia de recuperación: no autoriza clasificación ni mutación. El cache
canónico es el del estado; `--semantic-model-cache DIRECTORIO` se reserva para
un cache local explícito, por ejemplo en un laboratorio aislado.

### Código como contenido

NeoCortex trabaja con código como una fuente más de conocimiento. La ruta Code
puede descubrir proyectos, reconocer lenguajes, guardar estructura y relaciones,
y ofrecer búsquedas textuales, estructurales y semánticas sobre el índice
publicado:

```bash
Neocortex --code-status --code-json
Neocortex --code-search "dónde se valida SQLite" --code-search-mode hybrid
Neocortex --code-projects --code-json
Neocortex --code-reconstruct PROJECT_OR_ID --code-json
```

Estas operaciones consultan o actualizan únicamente el índice de contenido. La
ruta Code no audita el repositorio de NeoCortex, no ejecuta review interno,
experimentos, proveedores externos ni genera receipts de calidad. `pytest`,
Ruff, Pyright/Mypy, Semgrep u otras herramientas se usan directamente durante
el desarrollo cuando una modificación lo requiera; no existe un comando
agregador de validación dentro del producto.

### Knowledge Plane de sólo lectura

La Fase 1 implementa el contrato de recuperación unificada sobre el estado ya
producido por inventario, FTS de documentos, catálogo, Semantic y código.
Su disponibilidad operativa no se presupone: primero se ejecuta
`--knowledge-status`. `status` conserva la vista global y devuelve `6` o `7`
ante cualquier owner incompatible o corrupto; `search` y `context` sólo se
abstienen cuando ese owner aparece en `blocking_owners`. Un owner severo ajeno
a los rankings requeridos permanece visible sin ocultar evidencia sana.

Framework schema 19 es la única compatibilidad legacy explícita: se admite
sólo en lectura cuando satisface exactamente el contrato estructural esperado,
se marca `legacy_schema_read_compatible:19->20` y nunca se migra.

Cuando los owners requeridos por la consulta son utilizables, Knowledge captura un
`KnowledgeSnapshot` lógico —no una transacción distribuida—, construye un plan
determinista, fusiona rankings sin confundir sus scores y compila contexto con
citas y presupuesto explícitos. El modo `evidence`, predeterminado, puede
conservar varias evidencias concretas del mismo recurso; `discovery` prioriza un
resultado semantic por recurso.

```powershell
Neocortex --knowledge-status
Neocortex --knowledge-search "protección diferencial de transformador" --knowledge-mode evidence
Neocortex --knowledge-context "protección diferencial de transformador" --knowledge-limit 12
```

Estas operaciones no crean `knowledge.sqlite3`, no migran bases, no reprocesan
el corpus y no autorizan mutaciones. Cada `ContextBundle` marca primero la
frontera `untrusted-corpus-data-v1`, antes de la consulta y de la evidencia
dinámica: el contenido recuperado es dato no confiable y no tiene autoridad
para emitir instrucciones, seleccionar herramientas ni autorizar acciones. El
payload se preserva y cita para mantener su trazabilidad; no se promociona a
instrucciones del consumidor.

La API Python canónica y tipada PEP 561 `neocortex.sdk` expone los mismos
contratos, planner, snapshot y `KnowledgeSearchService` sin retirar los imports
legacy. El golden actual usa candidatos de owner scripted: valida contratos y
orquestación, pero no sustituye una evaluación humana ni demuestra calidad
sobre el corpus real. El grafo transversal entre owners continúa como
evolución futura; la CLI humana y MCP/stdio read-only ya consumen esta
publicación mediante scopes fijos. Consulte
[Knowledge Plane](docs/KNOWLEDGE.md) para contratos, completitud, códigos de
salida y límites verificables.

### Plan semántico de sólo lectura

El preflight semántico hace un inventario exacto de las cachés durables sin
cargar modelos, crear jobs ni mutar estado durable:

```powershell
Neocortex --semantic-plan text --semantic-plan-json
Neocortex --semantic-plan image --semantic-plan-max-scratch-bytes 536870912
```

El plan informa recursos, contenido único, reutilización, bytes vectoriales
como cota inferior y solicitudes al modelo como rango inferior/superior. El
canal textual se marca
`model_only_request_range_from_pre_tokenizer_content_projection`: el productor
liga después el tokenizador exacto y puede dividir más chunks. Por eso ese rango
no sustituye el piloto acotado. Imagen sin OCR conserva proyección exacta. El
tiempo de modelo sólo tiene rango cuando la API de servicio recibe una
calibración exacta compatible; la CLI no inventa esa calibración. Cada base
física se observa en su propia transacción con fences de cambio y el SQLite
scratch privado tiene una cuota dura predeterminada de 512 MiB.

La planificación de imagen es deliberadamente *cache-only*: no reabre
originales. Por ello informa `originals_verified=false`,
`execution_ready=null` y `complete=false`; un plan calculado no certifica que
la ejecución posterior esté lista.

Antes de cualquier indexación compruebe `--semantic-status`. Cero modelos
publicados o cero embeddings significa que la señal semántica aún no está
entregada. `--semantic-index` usa por defecto un único presupuesto compartido
de 50 items nuevos o cambiados, 1 500 jobs durables nuevos o reactivados y
900 segundos. Los replays exactos no consumen los dos primeros límites.

La indexación textual publica también un título durable y explica su base:
prefiere un título propio de la fuente —por ejemplo el asunto EML—; si el
basename es genérico o de recuperación, usa un encabezado humano acotado del
contenido; en último término usa el basename sin directorios ni extensión. La
búsqueda lo mantiene como señal semántica separada y advisory: fusiona cuerpo
(peso `1.0`) y título (peso `0.5`) por RRF, aplica la misma abstención calibrada,
conserva la procedencia y devuelve el snippet corporal cuando existe.
Clasificación, evidencia materializada y Knowledge `evidence` continúan
consumiendo sólo contenido. Knowledge `discovery` puede usar el título sólo
como prior de un recurso ya sustentado por evidencia corporal. Un título nunca
autoriza mover, renombrar o borrar.
Un head legado sin ese canal informa `title_channel_not_indexed` hasta una
publicación acotada compatible.

La recuperación léxica intenta primero la intersección estricta. Sólo si no hay
hits elimina stopwords ES/EN/DE y aplica un fallback acotado; para consultas Han
de al menos dos caracteres, y únicamente después de fallar FTS, recorre como
máximo 50 000 filas por fuente mediante substring exacto con procedencia
separada. Los escaneos vectoriales exactos se agrupan con NumPy sin cambiar
scores, empates ni provenance. Un bakeoff offline ES/EN/DE/ZH dejó a MiniLM
como candidato *shadow* —mejoró calidad agregada y latencia caliente, pero
retrocedió en inglés y el fixture fue pequeño—; Jina continúa como modelo
publicado. No se mezclan espacios ni se transfiere el piso de un modelo a otro.

Si se agota un límite, la salida marca `truncated=1`, devuelve `2` y conserva la
generación sin publicar; el head anterior no cambia. Una generación
`bounded-v1` sólo puede publicarse después de confirmar la enumeración completa.
Valide primero sobre 20–50 elementos: embeddings, publicación, búsquedas reales
y segunda corrida incremental.

## Uso seguro

`--apply` y `--organization-apply` son autorizaciones explícitas para mutar
archivos; no son necesarias para indexar o buscar. En `0.9.0`, rename y
organización sólo operan sobre un archivo regular con un único hard link, en
NTFS local y en el mismo volumen, mediante handles retenidos y semántica
*no-replace*. Rutas UNC, otros filesystems, reparses, directorios y movimientos
entre volúmenes provocan abstención. La planeación en seco conserva candidatos
de Papelera, pero la aplicación por ruta está deshabilitada y se registra como
`skipped`; `Send2Trash` ya no es una dependencia.

En Linux ambas autorizaciones se rechazan antes de crear estado, con código `2`
y razón `linux_mutation_backend_unavailable`. No se degrada el contrato NTFS a
una operación basada sólo en rutas.

Una acción que cruzó la frontera de mutación sin poder confirmar el registro
queda `recovery_required` y nunca se repite automáticamente. `status` sólo
clasifica; `record` persiste explícitamente esa observación append-only, sin
autorizar ni ejecutar una recuperación:

```powershell
Neocortex --action-recovery-status --action-recovery-limit 100
Neocortex --action-recovery-status --action-recovery-json
Neocortex --action-recovery-record 42 --action-recovery-actor "Victor" --confirm-reconciliation-record --action-recovery-json
```

No existen todavía fases productivas `decide`, `authorize`, `recover` o
`verify`. `confirmed` y `not_performed` son clasificaciones de evidencia,
no permisos para repetir una syscall.

El planificador de retención es también diagnóstico y no destructivo. No poda,
no aplica cuotas ni ejecuta `VACUUM` o checkpoints. Conserva las publicaciones
vigente/anterior, evidencia semántica, el último run válido y holds cross-store;
no existen `prepare/apply/verify` productivos:

```powershell
Neocortex --retention-status
Neocortex --retention-status --retention-store semantic --retention-min-age-days 30 --retention-json
```

Los lectores oficiales de catálogo y semántica seleccionan únicamente la
generación publicada; el staging incompleto permanece invisible y la
publicación cambia su puntero mediante una transacción CAS. Los embeddings y
clasificaciones probabilísticas nunca autorizan por sí solos una mutación.
Antes de actualizar una instalación con bases existentes, realice un backup
consistente mediante la API SQLite; no copie sólo el `.sqlite3` si puede existir
WAL.

### Borrado explícito de bases

Cuando se necesite empezar de nuevo con el estado derivado, `databases purge`
borra sólo las bases SQLite canónicas y sus sidecars (`-wal`, `-shm` y
`-journal`). Sin `--apply` siempre muestra una vista previa; la ejecución exige
el token exacto `DELETE_DATABASES`, toma un backup verificable fuera del
directorio `state`, comprueba los locks de framework/release/routes/watcher y
vuelve a validar cada archivo antes de quitarlo. Releases, modelos, launchers,
recibos y archivos SQLite desconocidos se conservan:

```bash
Neocortex databases purge --json
Neocortex databases purge --store image --store semantic --json
Neocortex databases purge --apply --confirm-database-purge DELETE_DATABASES
```

El backup queda en un directorio nuevo `database-backups/` junto al estado y su
manifest conserva tamaño, hash e integridad. Si hay un writer activo, cambia un
archivo o falla el backup, el comando se abstiene sin borrar la fuente.

## Documentación

- [Kubuntu/Linux](docs/LINUX_KUBUNTU.md): instalación versionada, XDG, modelos,
  KDE, verificación y límite de mutación.

- [Guía de CLI](docs/CLI.md)
- [Operación y watcher](docs/OPERATIONS.md)
- [Instalación offline y wheelhouse](docs/OFFLINE_INSTALLATION.md)
- [Arquitectura](docs/ARCHITECTURE.md)
- [Código como contenido](docs/CODE_SUBSYSTEM_CLASSIFICATION.md)
- [Knowledge Plane](docs/KNOWLEDGE.md)
- [Handoff operativo vigente](https://github.com/victor982721-lab/Neocortex/blob/main/.codex/handoffs/NEOCORTEX_0.7.2_PAUSE_2026-07-30.md)
- [Persistencia y migraciones](docs/PERSISTENCE.md)
- [Recuperación y rollback](docs/RECOVERY.md)
- [Seguridad y operaciones sobre archivos](docs/SECURITY.md)
- [Registro de cambios](docs/CHANGELOG.md)
- [Inventario técnico de licencias de terceros](docs/THIRD_PARTY_LICENSE_INVENTORY.md)
- [Estándar de cierre de auditorías](docs/AUDIT_REPORTING_STANDARD.md)
- [Estructura canónica](docs/ARCHITECTURE.md)

### Referencia histórica; no es flujo de trabajo

Las auditorías y handoffs anteriores se conservan como evidencia, pero no deben
ejecutarse como instrucciones vigentes:

- [Cierre histórico de Fase 1 Knowledge](docs/KNOWLEDGE_EVOLUTION_2026-07-26_010033.md)
- [Informe integral histórico 0.7.1](docs/TECHNICAL_EVOLUTION_2026-07-26_173000.md)
- [Handoff técnico histórico 0.7.1](docs/TECHNICAL_EVOLUTION_HANDOFF_2026-07-29_082142.md)
