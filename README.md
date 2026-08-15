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

`--all` inicia primero el autoanálisis protegido del checkout
canónico y guarda esa evidencia en el estado separado de autoanálisis. Después
continúa con el corpus documental. Si la raíz documental fue eliminada o no es
utilizable, el autoanálisis todavía se ejecuta y la etapa de corpus termina con
un error controlado `corpus_unavailable`, nunca con un traceback.

Después de esa validación, `Neocortex --all` también avanza Semantic sobre las
cachés durables disponibles. El canal textual incluye PDF, DOCX, XLSX, PPTX,
ODT, audio, miembros de ZIP y la ruta de texto/correo; si existe la caché de
imágenes, el mismo presupuesto publica además CLIP visión y el OCR retenido.
Usa límites reanudables de 100 000 items, 1 000 000 de jobs y 48 horas. Code no
entra implícitamente: es un corpus de análisis distinto que puede dominar el
presupuesto documental. Para incluirlo deliberadamente se usa
`--all --semantic-source code`. No se presupone ningún conteo histórico de
vectores o chunks: `--semantic-status` es la lectura del estado realmente
publicado.

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
Neocortex inspect code "dónde se valida SQLite" --scope framework
Neocortex inspect lineage IDENTIFICADOR --scope personal
Neocortex review value --scope personal --limit 50
```

`personal` consulta el corpus, `framework` el autoanálisis y `all` devuelve
ambos rankings por separado: nunca mezcla scores de snapshots distintos. La
página **Consulta** de `Neocortex --ui` ofrece las mismas acciones, evidencia,
citas, cobertura e incertidumbre sin controles de mutación. Para clientes
locales, `Neocortex agent serve` expone por MCP/stdio sólo `status`, `search`,
`context`, `evidence` e `inspect_code`, todos marcados read-only. Los flags
históricos siguen disponibles para automatización y producción de estado.
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
como legado sin soporte ni validación vigente. No instale el paquete, `pip`,
Node ni sus dependencias contra runtimes globales.

Los extras `agent` y `analysis` son superficies de distribución para la API MCP
y los analizadores, no elecciones operativas que Víctor deba administrar. Las
releases personales siguen instalando `full`, que reúne esas superficies con
las capacidades documentales, multimedia, Semantic y UI. Semgrep queda fuera
incluso de `analysis`/`full`: se provisiona y verifica en su tool-runtime
administrado, separado del runtime principal.

La instalación y los gates locales autentican `pip` antes de instalar otra
dependencia. El comando canónico no depende del `pip` ambiental: descarga el
wheel oficial `26.1.2`, exige su nombre y SHA-256 fijados, lo instala sin índice
ni dependencias y verifica la versión bajo Python aislado:

```bash
python -I tools/bootstrap_pip.py
```

Para una instalación offline, el mismo helper acepta `--wheel` con el wheel
canónico ya disponible y conserva la validación exacta.

### Kubuntu/Linux

La referencia local es Kubuntu/Ubuntu 26.04, Linux x86-64 y CPython 3.14.4. El
instalador mantenido construye el wheel, crea una release inmutable, instala el
extra `full` sólo desde wheels binarios, integra Node/Pyright, prepara modelos
de forma explícita y publica KDE al final:

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
| `analysis` | Complexipy, Cosmic Ray, Coverage, Deptry, Grimp, Mypy, pip-audit, Pytest, Radon, Ruff y Vulture |
| `documents` | PDF, fallback pdfminer y OCR documental |
| `audio` | transcripción local con faster-whisper |
| `image` | decodificación Pillow y clasificación NudeNet |
| `semantic` | embeddings texto/imagen con FastEmbed y NumPy |
| `ui` | interfaz PySide6 |
| `full` | unión compatible de `agent`, `analysis` y los cinco dominios de producto |

Algunas rutas conservan prerrequisitos externos que ningún extra de Python
puede instalar: `ffprobe` es obligatorio para `audio`; `tesseract` y `qpdf`
habilitan OCR y recuperación PDF degradables en `documents`; y `tesseract`
habilita el OCR documental degradable en `image`. El probe ligero sólo busca
estos ejecutables en `PATH`; no interpreta overrides de una ejecución concreta.

Los wheels nativos del perfil `full` requieren el Microsoft Visual C++ v14
Redistributable x64 vigente. Después de instalarlo, valida en el mismo runtime
los imports de PyMuPDF, ONNX Runtime, PySide6, PyAV, CTranslate2 y OpenCV antes
de promover el launcher; `pip check` por sí solo no detecta una DLL del sistema
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
`degraded`, que falta sólo una función opcional —por ejemplo OCR, fallback PDF
o clasificador adulto—; y `unavailable`, que falta un requisito obligatorio de
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
```

`text.sqlite3` conserva texto, tipo, título, autor, firma, errores y FTS. El
catálogo puede proponer clasificación y nombres —por ejemplo, el asunto de un
EML—, pero en Linux sigue sin existir autoridad de movimiento. Semantic acepta
esta caché mediante `--semantic-source text`.

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

### Autoanálisis de código

`--self-analysis` ejecuta sólo la ruta `code` sobre una raíz explícita en modo
`analyze_only`. Exige un estado externo cuyo árbol sea completamente disjunto,
omite candidatos MIME, acciones, catálogo y organización, y finaliza sólo si
los conteos de trabajo sobre el corpus permanecen en cero:

```powershell
$Lab = Join-Path $env:LOCALAPPDATA 'Neocortex\self-analysis\fixtures'
$MiniRoot = Join-Path $Lab 'mini-root'
$MiniState = Join-Path $Lab 'mini-state'
Neocortex --self-analysis --root $MiniRoot --state-directory $MiniState
Neocortex --state-directory $MiniState --code-status --code-json
Neocortex --state-directory $MiniState --code-review
```

El primer comando escribe inventario y estado de código; no es una consulta de
sólo lectura. El perfil predeterminado `protected` ejecuta Ruff con una política
fija `E4,E7,E9,F`, aislada de la configuración del proyecto. Para una raíz que
Victor haya declarado confiable, `--analysis-profile trusted-static` suma 13
proveedores independientes: Ruff basic, Ruff con la política versionada del
proyecto acotada a `E4,E7,E9,F,B,C4,PIE,RUF`, Mypy, Pyright, Ruff Analyze,
Grimp, Complexipy, Vulture, Semgrep, Deptry, pip-audit, inventario del entorno
instalado e historial Git local. Ruff trusted omite
deliberadamente `I,PT,SIM,UP`: esas familias de estilo, tests y modernización no
deben ahogar la señal de mantenimiento en esta etapa. Los dos type checkers
conservan hallazgos separados y publican
un resumen explícito de coincidencias y discrepancias; la ausencia de uno queda
`not_comparable`, nunca se disfraza de consenso.

Vulture 2.16 aporta candidatos estáticos `unused_code` mediante una ejecución
aislada, acotada y sin cargar configuración del proyecto. Su confianza es una
señal heurística, no una conclusión. El consumidor
`neocortex.code-unused-analysis/v1` la correlaciona con Pyright, grafo interno,
imports, reexports, `__all__`, callbacks, registries, fixtures, entry points,
Protocols y, cuando existe, Coverage. Cada candidato queda en uno de cuatro
estados explicables: `explained_usage`, `dynamic_usage_possible`,
`insufficient_evidence` o `probable_unused_high_consensus`. Fixture de
calibración y holdout publican precision, recall y abstención por separado; una
señal ausente o no comparable obliga a abstenerse. Incluso el consenso alto es
advisory, exige confirmación humana y tiene cero autoridad de borrado o mutación.

Los tres proveedores de arquitectura tienen contratos distintos: Ruff Analyze
(`ruff-analyze-imports`) actúa como oráculo diferencial del grafo; Grimp
(`grimp-architecture`) produce relaciones de import, fan-in/fan-out, SCC,
ciclos y evaluaciones de contratos; Complexipy (`complexipy-cognitive`) publica
complejidad cognitiva por símbolo y sus agregados por módulo. Los contratos v1
se derivan de los seis paquetes de producción y conservan explícitamente las
fronteras permitidas y los ciclos ya existentes como baseline `no-new`; no
presentan la arquitectura actual como acíclica.

Los 13 proveedores estáticos tienen límites y no importan ni ejecutan el código
observado; los analizadores de contenido trabajan sobre copias verificadas. El
historial se limita al repositorio Git local, pip-audit declara la red usada por
su snapshot y el inventario instalado se recalcula en cada corrida. Todos
publican versión, firmas, cobertura, contadores de proceso/bytes/tiempo/caché y
evidencia únicamente advisory; ninguno aplica fixes ni posee autoridad de
mutación. Un
replay exacto vuelve a verificar los inputs y reutiliza la publicación sin
reejecutar el workload del analizador, tests o mutantes; puede usar probes
acotados, explicados y costeados. La suite aparece en status, review,
publication diff y code doctor; una indisponibilidad o límite alcanzado obliga
a abstener el gate
afectado, no borra la evidencia de los demás proveedores.

`trusted-deep` es un perfil adicional, nunca predeterminado, que conserva los
13 proveedores estáticos y añade `pytest-coverage-trusted-deep` y
`cosmic-ray-focal-mutation`, para un total de 15. Sólo acepta
la identidad física exacta de `$HOME\Neocortex\Repository`: ejecuta
el código del proyecto, sus pruebas y `conftest.py`, mide líneas y ramas con
contextos dinámicos por test, y por ello no se admite sobre una raíz arbitraria.
El estado debe permanecer aislado en Laboratory:

```powershell
$Root = Join-Path $HOME 'Neocortex\Repository'
$State = Join-Path $HOME 'Neocortex\Laboratory\self-analysis\trusted-deep'
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State

# Mutación focal del símbolo elegido por el work package.
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State `
  --deep-test-selector tests/test_external_deep_coverage.py `
  --deep-mutation-target _04_Nucleo_Operativo/external_deep_coverage.py `
  --deep-mutation-symbol external_deep_coverage._normalize `
  --deep-mutation-max-mutants 20 --deep-mutation-timeout-seconds 30 `
  --deep-mutation-time-budget-seconds 600
```

Sin `--deep-test-selector`, ejecuta la suite declarada completa. El selector se
puede repetir con una ruta relativa bajo `tests/` o un node id de Pytest. Los
límites predeterminados son 3000 tests, 600 segundos y shards de 20; sus rangos
son 1–5000, 30–900 y 1–50, respectivamente. Sólo los shards que terminaron con
todas sus pruebas aprobadas producen checkpoints reanudables ligados a las
firmas exactas de inputs, herramientas y configuración. Coverage mide sólo el
proceso principal: la cobertura de subprocesses no se atribuye y se declara
como limitación.
La mutación focal exige al menos un selector explícito. Admite 1–100 mutantes
(20), timeout individual de 1–120 segundos (30) y presupuesto total de 10–900
segundos (600). Cosmic Ray sólo modifica la copia staged, pero ejecuta las
pruebas seleccionadas y éstas pueden usar red; no tiene autoridad alguna sobre
el repositorio original.
El status sí es estrictamente read-only:
cualquier `-wal`, `-shm`
o `-journal` junto a `code.sqlite3`, `framework.sqlite3` o `dedup.sqlite3`,
incluso vacío o desacoplado, causa abstención total con código `2` sin tocar el
estado. Consulte [Autoanálisis de código](docs/SELF_ANALYSIS.md) antes de usar
el preset.

Si el proceso no puede abrir el journal USN, el autoanálisis degrada a un
recorrido completo portable. No publica checkpoint ni inventa cursor: el
manifest registra `journal.status=unavailable`, el status nunca afirma
`current=true` y la ruta code todavía reutiliza por caché los archivos sin
cambios.

`--code-review` convierte la publicación en observaciones y preguntas
explicables. El envelope `neocortex.code-review/v19` no declara schemas
compatibles: conserva el corte fail-closed y usa la proyección general
`neocortex.code-analysis-epistemics/v1`. Cada finding separa observación,
hipótesis, readiness de pregunta, evidencia faltante, contraevidencia por
buscar, siguiente acción y readiness de decisión. Un hotspot queda
`experiment_required`; no infiere construcción ni riesgo por nombres y nunca
autoriza mutación.

v19 conserva el resolver trazable introducido en v17, deliberadamente acotado:
sólo enlaza
receipts `passed` del run, processing signature, evaluación, proposal, template y
gates registrados exactos. Hoy puede satisfacer requisitos de seis familias:
ruta pública Text, workflow SQL/transaccional Text, recuperación Semantic,
migración Code-owner, contratos de imports y Retention durable; el resto conserva
su evidencia faltante. Aun cuando una evaluación avanza a
`human_review_required`, no crea una decisión humana, recomendación o package de
cambio. v19 conserva el verificador técnico determinista y allow-listed que puede
publicar únicamente `no_change_required_within_verified_scope` cuando vuelve a
comprobar la partición de evidencia, el receipt, sus gates y los controles
negativos específicos. La disposición es advisory, conserva riesgos residuales,
no suplanta a una persona y nunca autoriza mutación; una pregunta sin política
exacta queda explícitamente `unresolved`.
`python-maintenance-work-packages-v5` sólo puede entregar hasta
tres paquetes `unused_characterization` calibrados: todos sus pasos son de
caracterización, requieren confirmación humana y declaran
`mutation_authority=false`. Coverage v2 aporta evidencia de ejecución por una
suite passing, no prueba que un test afirme o proteja un invariante; por eso la
superficie usa `executing_tests` y
`work_package_target_executed_by_passing_suite`. `--code-review-limit N
--code-json` amplía de 1 a 50 la vista
auditable. La consulta es estrictamente read-only; un snapshot full sin USN se
etiqueta `publication_only` y un journal avanzado/discontinuo causa abstención.
Cada evaluación v19 fija el fingerprint de la pregunta, el snapshot y la
revisión. Los hotspots enlazan los IDs de diagnóstico exactos; la nueva familia
`class_surface` vuelve a resolver el símbolo de clase y todos sus miembros AST
directos confirmados. Sus umbrales provisionales de 500 líneas o 20 métodos son
filtros de atención, no riesgo calibrado ni evidencia de una *god class*.
`--code-review-limit N` acota cada familia a `N` observaciones. `resolved` prueba
concordancia con los registros; no convierte tamaño, nombres o rutas en daño ni
en una decisión humana.

Además de funciones y clases, v19 publica una proyección acotada de módulos,
configuraciones y construcción CLI. Los módulos se seleccionan por conteos
estructurales explícitos; JSON/TOML se parsean desde el snapshot sin exponer
valores; YAML/text-only permanece `unsupported` o `incomplete`; y las llamadas
`argparse` literales/dinámicas se observan sin ejecutar código. Ninguna de estas
señales prueba cohesión, reachability o necesidad de refactor.

El grafo Ruff/Grimp y los contratos de imports existentes también se proyectan
como preguntas generales. Consenso estático y contratos evaluados son evidencia
consultable, no ownership ni una orden de cambio. Como NeoCortex todavía no
tenía un ownership explícito, v19 conserva un registry versionado y
deliberadamente parcial para `text`, `semantic`, `knowledge`, `review`,
`retention` y `framework`. La pregunta publica cobertura, módulos sin mapear,
solapamientos y edges cross-owner; nunca rellena un owner por defecto ni a partir
del primer segmento del módulo.

En la ubicación canónica de autoanálisis, v19 publica observaciones cross-owner:
`neocortex.code-state-projection/v1`: abre Text y Semantic mediante lecturas
SQLite `immutable=1` con fences de archivo y sidecars inactivos, y compara el
conjunto exacto de revisiones Text elegibles con cada head Semantic publicado de
modalidad texto. Conserva por separado filas completas vacías y no las convierte
en faltantes. Alineación o delta siguen siendo observaciones: no prueban una
transacción distribuida, recuperación ante muerte de proceso ni un defecto.
`neocortex.code-state-topology/v1` verifica por separado el cierre relacional de
la publicación terminal Text contra receipts y outbox owner-local.

v19 añade `neocortex.code-retention-analysis/v1`: vuelve a ejecutar el planner
dry-run sobre Semantic, Catalog, Inventory y Framework, publica schemas, holds,
conteos y cursores acotados, y conserva como contraevidencia cualquier owner
bloqueado o hold declarado ausente. Una segunda lectura debe reproducir la misma
proyección. El resultado nunca autoriza borrar; la disposición técnica requiere
además los nueve controles aislados del template Retention exacto.

El mismo envelope integra change/schema evolution, assurance, supply-chain,
capability reachability y autoeficacia. La ausencia o caducidad de Git history,
Coverage, Semgrep, Deptry, pip-audit o inventario instalado queda visible como
evidencia faltante y nunca como gate aprobado. Autoeficacia compara por contenido
la última publicación Code con el inventario Git visible y deja precision,
recall y finding→decision sin calcular mientras no existan outcomes humanos o
defectos escapados enlazados independientemente.

v19 conserva varias verticales productivas sobre ese mismo registro. La primera
analiza SQL literal Python con SQLGlot en dialecto SQLite y publica por separado
READ, WRITE, DDL, SQL dinámico/no parseable y eventos transaccionales; un store o
workflow sólo se asigna mediante el registry explícito, nunca por el nombre de la
función. Los bind parameters numerados válidos de SQLite (`?1`, `?2`, ...)
se normalizan token a token para el parser sin reescribir strings ni el digest
de la fuente observada. La segunda enlaza cuatro invariantes versionados con sus escenarios
pytest exactos, distingue ejecución, assertion y prueba formal, y conserva como
faltante cualquier resultado trusted-deep ausente o stale. También proyecta las
nueve rutas built-in desde el contrato público hasta estado owner-local; sólo
Text tiene hoy un enlace causal completo, las demás quedan observadas pero no
atribuidas.

Cada evaluación `experiment_required` recibe una propuesta determinista del
experimento registrado más barato. El plan no ejecuta comandos libres ni
autoriza cambios. Las propuestas allow-listed con runner real aparecen como
`CODE_EXPERIMENT_PROPOSAL`; una de ellas puede ejecutarse explícitamente con
`--code-experiment-run PROPOSAL_ID [--code-json]`. El ejecutor vuelve a construir
el review vigente, exige el mismo proposal ID, raíz física y manifest Code, y
sólo admite seis templates ejecutables registrados: acceptance de la ruta
pública Text (un nodeid), trace/fault boundaries del workflow Text (cuatro
nodeids), recuperación Semantic ante muerte del proceso durante staging (un
nodeid con tres gates medidos), una matriz Code-owner de migración poblada,
rollback y rechazo de schema futuro (cinco nodeids con cuatro gates), y la
aceptación acotada de los contratos de imports declarados (tres nodeids y cuatro
gates), más la seguridad durable de Retention en dry-run sobre los cuatro owners
(catorce nodeids exactos y cuatro gates). Retention comprueba heads, builders, leases,
evidencia humana, fallos de schema/receipts y lectura concurrente; nunca autoriza
ni valida un futuro ejecutor de borrado. La aceptación arquitectónica comprueba
el grafo vivo, una frontera permitida y controles negativos de aristas/ciclos;
no demuestra dispatch dinámico ni toda la intención arquitectónica.
Pytest se ejecuta sobre el checkout canónico confiable; el temporal aloja
runtime/checkpoints, no una copia ni un sandbox de seguridad. El provider puede
usar red y conserva `HOME`. El provider vuelve a verificar antes y después la
firma exacta de los inputs Python publicados y del soporte Git observado; si
cambia, el receipt se rechaza. Un fence Linux de identidad, sidecars y anclas
acotadas de `code.sqlite3` se compara antes/después sin volver a leer todo su
historial durante cada experimento. No existe un lock continuo del checkout y ni el
corpus ni otros stores quedan dentro de esa barrera.

El resultado medido es `neocortex.code-experiment-receipt/v3`. Después de
terminar, la CLI lo agrega como `neocortex.code-experiment-store/v1` a la tabla
inmutable y append-only `code_experiment_receipts` de Code schema v6. Por ello
`code_database_unchanged=true` describe esa barrera acotada durante la fase
ejecutora antes de persistir, no una equivalencia byte a byte del store ni que el
comando completo no escriba su receipt. El siguiente review v19
sólo proyecta el receipt terminal `passed` más nuevo ligado al proposal y a
la processing signature actuales. Puede provenir de una publicación Code
completada anterior cuando la publicación vigente es un replay exacto con la
misma firma; su envelope digest vuelve verificable todo ese contexto. Un receipt
posterior `failed` o `abstained`, o uno stale, corrupto o sin binding, invalida
la satisfacción.
Cada gate prueba exclusivamente el contrato de sus tests exactos, no una verdad
formal ni una decisión humana; `mutation_authority=false` se conserva.

`--code-publication-diff` publica el envelope
`neocortex.code-publication-diff/v10`, sin declarar compatibilidad estructural
con wires anteriores, y compara dos
publicaciones Code completadas sin escribirlas. Informa calls comunes,
resoluciones nuevas/corregidas/perdidas,
hotspots añadidos o retirados, el delta no calibrado de `probable_dead` y los
hallazgos añadidos/resueltos por proveedor cuando sus firmas de comparabilidad
coinciden. Para Mypy y Pyright separa además los findings `relocated` cuando
ruta, categoría, código, severidad, mensaje y metadata permanecen idénticos y
sólo cambia el rango; conserva multiplicidad y posiciones exactas sin fallar el
gate. Añade deltas de métricas por módulo, contratos, ciclos y complejidad
desplazada cuando ambas publicaciones son comparables. Para `trusted-deep`
compara cobertura de líneas y ramas sólo si coinciden la suite, alcance,
configuración y herramientas; de otro modo la dimensión queda
`not_evaluated`. También compara identidades y cambios entre los cuatro estados
de código potencialmente no usado sólo si coinciden proveedor, policy,
calibración y holdout, e informa candidatos de consenso alto añadidos o
resueltos; cualquier incompatibilidad queda `not_evaluated`. También proyecta
por separado deltas comparables de `engineering_analytics`, incluido el score
de mutación sólo cuando coincide su alcance. El veredicto
agregado siempre conserva las limitaciones parciales y nunca transforma el
delta en autorización de borrado.
Exige bases quiescentes, limita la enumeración y conserva ejemplos en `--code-json`.
Un cambio semántico real continúa apareciendo como añadido/resuelto y conserva
la autoridad del gate.

#### Consulta unificada de la publicación

`--code-query` ofrece una sola superficie read-only sobre `status`, `review` y
`diff`; consume únicamente publicaciones existentes y no vuelve a analizar la
raíz, migra bases ni escribe estado:

```powershell
Neocortex --state-directory $State --code-query status
Neocortex --state-directory $State --code-query review --code-query-provider $Provider --code-query-module $Module --code-json
Neocortex --state-directory $State --code-query diff --code-query-baseline $BaselineState --code-query-delta added --code-json
```

Los filtros repetibles son `provider`, `category`, `module`, `status`, `delta`
y `work-package`; se combinan con AND entre dimensiones y OR dentro de una
misma dimensión. Un filtro de módulo incluye el módulo exacto y sus
descendientes. `--code-query-limit` acepta 1–500 (50 por defecto) y
`--code-query-baseline` sólo es válido con `diff`. La salida humana y JSON
conservan dimensiones, evidencia y limitaciones por separado: no calculan un
score agregado ni una probabilidad de defecto, y nunca autorizan una mutación.

NeoCortex no usa GitHub Actions. La entrada cotidiana única para validar una
implementación nueva es local y Linux-only:

```bash
Neocortex code validate
```

Antes de ejecutar herramientas, el comando toma un lock exclusivo, observa
`MemAvailable`, swap y PSI, y reserva dinámicamente memoria para KDE/Chrome.
Toda la validación y sus descendientes se reejecutan en un único cgroup de
usuario con `MemoryHigh`, `MemoryMax`, `MemorySwapMax`, cuota de CPU y límite
global de 45 minutos. Un watchdog detiene cooperativamente el grupo con SIGINT
si se pierde la reserva del escritorio; el reclaim aislado por `MemoryHigh` no
se confunde con riesgo para KDE/Chrome mientras exista headroom físico. El
runner crea además un grupo de proceso por comando y, ante su timeout, envía
SIGINT y espera una terminalización durable antes de escalar a SIGKILL. El
recibo enlaza la admisión `neocortex.code-validation-resources/v3`; el worker
la autentica contra su membresía exacta en `/proc/self/cgroup`, consulta en
systemd el `PrivateNetwork=yes` del unit y prueba ante el kernel que AF_INET y
AF_INET6 están realmente denegados. La salida
humana muestra el pico de memoria observado por systemd. El mismo servicio
declara `PrivateNetwork=yes`, por lo que el worker y todos sus providers carecen
de ruta hacia la red externa.

El comando captura el diff y su digest, selecciona pruebas afectadas por cambio
directo, convención y grafo de imports publicado, añade las fronteras públicas y
escenarios registrados si el grafo queda incompleto, y sólo escala a la suite
declarada cuando cambia packaging, schema o la política de gates.
La suite declarada omite únicamente las pruebas del runtime Windows/NTFS
retirado; conserva fixtures portables aunque modelen metadatos históricos.
Después ejecuta los gates estáticos y arquitectónicos existentes, publica el
perfil `trusted-deep`, consume `neocortex.code-review/v19`, ejecuta una vez cada
plantilla de experimento allow-listed relevante, construye e instala el wheel
candidato en un entorno efímero fuera del checkout, y repite la publicación
idéntica para exigir replay de los proveedores. Devuelve un único recibo
`neocortex.code-change-validation/v3` con salida `0` sólo si no hubo fallo ni
abstención y si fuente/estado canónico permanecieron intactos. No autoriza
patches, push, release ni mutación del corpus.

La relevancia experimental no se deduce de que exista o no un runner. Un
registro versionado liga rutas y tests del diff con sujetos/preguntas concretos:
si una pregunta relevante sigue en `experiment_required` y carece de runner o
disposición técnica verificada, el gate se abstiene. Sólo usa `not_required`
cuando el binding publicado demuestra que esa pregunta no fue afectada. Tras el
replay, cada pregunta relevante debe aparecer como `human_review_required` con
una disposición técnica independiente y acotada; de otro modo no existe
`passed` canónico.

El review conserva los deltas históricos `added/resolved` de los providers,
pero `code validate` no los confunde con el diff Git: un ID puede cambiar al
mover líneas y su publicación comparable puede ser anterior a `HEAD^`. La
barrera que bloquea una regresión estática es el baseline versionado por
path/regla/conteo ejecutado antes del review; el delta portable continúa siendo
evidencia advisory para investigar.

`code validate` no inicia tráfico de red. Resuelve el último snapshot publicado
de `pip-audit` todavía dentro de su ventana de 24 horas únicamente cuando el
inventario instalado actual coincide paquete por paquete y versión por versión,
el snapshot reportó cero vulnerabilidades y el diff no toca packaging ni la
política supply. El recibo expone esa resolución; si falta un audit fresco o
existe cualquier discrepancia, el gate se abstiene. La actualización deliberada
del feed permanece fuera de esta validación local.
El inventario instalado se vuelve a observar deliberadamente en el segundo run;
el replay sólo pasa si métricas, relaciones, findings y versiones normalizados
son idénticos, excluyendo únicamente el reloj y el ID efímero del snapshot.

`tools/quality_gate.py` sigue siendo infraestructura interna reutilizada por el
orquestador y sirve para diagnóstico especializado, no como flujo alternativo
que cada sesión deba reconstruir manualmente. Para validar un commit ya creado
contra su padre use `Neocortex code validate --baseline HEAD^`.

La validación H6 sobre la raíz canónica produjo el work package
`_04_Nucleo_Operativo.external_deep_coverage` /
`external_deep_coverage._normalize`. Run 9 terminó en 343.168 s con 585
candidatos (2 procesados, 583 por caché), 15 proveedores y 0 errores; Cosmic Ray
completó 20/20 mutantes seleccionados (5 killed, 5 survived, 10 incompetent, 0
timeout; score 0.50) de 524 generados. Run 10 repitió los mismos bytes en 23.996
s: 585/585 candidatos por caché, cero bytes/analyze/persist/graph y 14 replays;
el inventario instalado se recalculó. Status, review y diff tardaron 38.982,
47.675 y 57.856 s, respectivamente.

La aceptación H7 desde wheel ejecutó run 11 en 356.807 s sobre 591 candidatos
(64 procesados, 527 por caché), 15 proveedores y cero errores. Run 12 demostró
replay exacto en 25.076 s con 591/591 hits, cero bytes y cero milisegundos de
read/analyze/persist/graph. Status, review y diff públicos tardaron 33.029,
41.311 y 66.279 s; las consultas instaladas combinaron proveedor, categoría,
módulo, estado, delta y work package con resultados acotados y sin score mágico.
El cierre factual completo está en
[Programa de autoanálisis multianalizador](docs/SELF_ANALYSIS_PROGRAM_REPORT_2026-08-03.md).

La corrida normal usa el baseline portable: publica el snapshot completo con
cursor nulo y las rutas comparan ese inventario contra sus caches.
`journal_usn_span=unavailable` distingue esa ejecución; las acciones continúan
sujetas a sus revalidaciones de identidad, contenido y destino. USN permanece
únicamente como implementación histórica fuera del alcance Linux vigente.
El watcher aplica la misma política: USN despierta corridas cuando está
disponible y, sin cursor compatible, programa inventarios normales portables a
intervalos explícitos sin crear otro índice. Entre ciclos recarga el dueño
durable mediante una instantánea immutable cercada, por lo que no recrea
sidecars de Framework sobre una publicación quiescente.

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

## Documentación

- [Kubuntu/Linux](docs/LINUX_KUBUNTU.md): instalación versionada, XDG, modelos,
  KDE, verificación y límite de mutación.

- [Guía de CLI](docs/CLI.md)
- [Operación y watcher](docs/OPERATIONS.md)
- [Instalación offline y wheelhouse](docs/OFFLINE_INSTALLATION.md)
- [Arquitectura](docs/ARCHITECTURE.md)
- [Autoanálisis de código y evidencia externa](docs/SELF_ANALYSIS.md)
- [Knowledge Plane](docs/KNOWLEDGE.md)
- [Handoff operativo vigente](https://github.com/victor982721-lab/Neocortex/blob/main/.codex/handoffs/NEOCORTEX_0.7.2_PAUSE_2026-07-30.md)
- [Persistencia y migraciones](docs/PERSISTENCE.md)
- [Recuperación y rollback](docs/RECOVERY.md)
- [Seguridad y operaciones sobre archivos](docs/SECURITY.md)
- [Registro de cambios](docs/CHANGELOG.md)
- [Inventario técnico de licencias de terceros](docs/THIRD_PARTY_LICENSE_INVENTORY.md)
- [Estándar de cierre de auditorías](docs/AUDIT_REPORTING_STANDARD.md)
- [Núcleo operativo](_04_Nucleo_Operativo/README.md)

### Referencia histórica; no es flujo de trabajo

Las auditorías y handoffs anteriores se conservan como evidencia, pero no deben
ejecutarse como instrucciones vigentes:

- [Cierre histórico de Fase 1 Knowledge](docs/KNOWLEDGE_EVOLUTION_2026-07-26_010033.md)
- [Informe integral histórico 0.7.1](docs/TECHNICAL_EVOLUTION_2026-07-26_173000.md)
- [Handoff técnico histórico 0.7.1](docs/TECHNICAL_EVOLUTION_HANDOFF_2026-07-29_082142.md)
