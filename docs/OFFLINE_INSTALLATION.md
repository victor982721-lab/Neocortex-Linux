# Instalación sin red

Esta guía distingue validación de artefactos, reutilización del entorno del sistema e instalación hermética. No existe actualmente un wheelhouse completo versionado dentro del repositorio.

## Estado verificado el 2026-07-25

- Fuente, wheel y sdist declaran NeoCortex `0.6.0` para Python `>=3.13,<3.14`.
- El wheel puede instalarse en un venv nuevo con `--no-deps`; `Neocortex --version`, `python -m neocortex --version` y `Neocortex --help` funcionan allí.
- Esa instalación **no está completa**: `pip check` informa las nueve dependencias base omitidas.
- Un venv con `--system-site-packages` pasa `pip check` en el equipo auditado, pero hereda paquetes globales y no demuestra una resolución aislada o reproducible.
- La instalación hermética desde el wheelhouse local incompleto se abstiene. El primer bloqueo exacto es `faster-whisper==1.2.1`, ausente de las ubicaciones locales proporcionadas.
- La instalación hermética desde sdist también necesita el requisito de build `setuptools==82.0.1` dentro del wheelhouse.
- `constraints.txt` fija las dependencias directas observadas; no es un lock transitivo con hashes.

No cambies constraints para sortear un paquete ausente. Completa y valida el wheelhouse para la versión de Python, arquitectura y extras que se desplegarán.

## Preparación vigente de la entrega `0.8.0`

El árbol fuente vigente declara `0.8.0`; eso no convierte en evidencia de esa
versión la validación fechada anterior. Los bloques posteriores que fijan
literalmente `0.6.0` conservan el ejercicio reproducido el 2026-07-25 y no deben
reescribirse al citarlo. Para promover `0.8.0`, construye sus artefactos y repite
las mismas barreras con nombres y requisitos exactos de la entrega actual:

La metadata vigente admite CPython `>=3.13,<3.15`. El wheelhouse y la
instalación deben validarse de forma independiente para 3.13 y 3.14; la
compatibilidad de un conjunto de wheels con una versión no demuestra la otra.
La sdist vigente declara `setuptools==83.0.0` como backend de build. Las
referencias posteriores a `setuptools==82.0.1` pertenecen exclusivamente a la
reproducción histórica de `0.6.0` y se conservan como evidencia fechada.

```powershell
$Version = '0.8.0'
$Wheel = "C:\Ruta\Dist\neocortex_framework-$Version-py3-none-any.whl"
$Sdist = "C:\Ruta\Dist\neocortex_framework-$Version.tar.gz"

py -3 -m venv C:\Ruta\VenvValidacion072
C:\Ruta\VenvValidacion072\Scripts\python.exe -m pip install --no-index --no-deps $Wheel
C:\Ruta\VenvValidacion072\Scripts\Neocortex.exe --version
C:\Ruta\VenvValidacion072\Scripts\python.exe -m neocortex --version
C:\Ruta\VenvValidacion072\Scripts\Neocortex.exe --help
```

La prueba `--no-deps` sigue siendo sólo una barrera de empaquetado ligero. Para
una instalación operativa hermética, el requisito debe ser
`neocortex-framework==0.8.0` para la base mínima, uno o más extras de dominio,
o `neocortex-framework[full]==0.8.0` para conservar el runtime integrado
completo. Todo su cierre transitivo debe resolverse exclusivamente desde el
wheelhouse autorizado. Después de `pip check`, comprueba además que Knowledge
pueda inspeccionar un directorio de estado deliberadamente ausente sin crearlo:

```powershell
$StateProbe = 'C:\Ruta\EstadoAusente072'
if (Test-Path -LiteralPath $StateProbe) { throw 'La ruta de prueba ya existe.' }
C:\Ruta\VenvValidacion072\Scripts\Neocortex.exe --state-directory $StateProbe --knowledge-status
if (Test-Path -LiteralPath $StateProbe) { throw 'Knowledge creó estado durante la consulta.' }
```

La METADATA de `0.8.0` separa el cierre de runtime así:

| Selección | Requisitos directos |
|---|---|
| base | `complexipy`, `cosmic-ray`, `coverage`, `deptry`, `grimp`, `mypy`, `packaging`, `pip-audit`, `pytest`, `radon`, `rich`, `ruff`, `semgrep`, `vulture`, `xxhash` |
| `documents` | `Pillow`, `PyMuPDF`, `pdfminer.six`, `pytesseract` |
| `audio` | `ctranslate2`, `faster-whisper` |
| `image` | `Pillow`, `nudenet` |
| `semantic` | `Pillow`, `fastembed`, `numpy` |
| `ui` | `PySide6` |
| `full` | unión de los cinco extras anteriores |

El wheelhouse sólo resuelve paquetes Python. Para `trusted-static`, prepare
además Node y Pyright `1.1.411` como paquete npm aislado dentro del venv del
runtime, sin añadir dependencias al proyecto analizado:

```powershell
$Runtime = 'C:\Ruta\VenvValidacion072'
npm install --prefix "$Runtime\tools\pyright" --ignore-scripts --no-audit --no-fund pyright@1.1.411
$env:PATH = "C:\Ruta\Node;$env:PATH"
& "$Runtime\Scripts\Neocortex.exe" --state-directory C:\Ruta\EstadoPrueba --code-doctor --code-json
```

El resultado esperado queda en
`$Runtime\tools\pyright\node_modules\pyright`; NeoCortex ejecuta su `index.js`
mediante Node y registra ambas resoluciones en la firma de entorno. La
instalación offline debe sustituir el comando npm por un cache/registry local
autorizado y conservar el tarball, hash y avisos; no se permite red durante el
autoanálisis.

El mismo cierre Python debe resolver exactamente Grimp `3.15` y Complexipy
`6.2.0` desde `constraints.txt`. No se instala Import Linter: la prueba focal de
`2.13` confirmó que es viable, pero Grimp ya entrega directamente el grafo
legible por máquina y evita duplicar el mismo análisis. Ruff Analyze pertenece
al Ruff `0.15.17` ya fijado; no es otra distribución.

También se debe preparar y validar por separado `ffprobe` en `PATH` para la
capacidad `audio`. La ausencia
de `tesseract` o `qpdf` deja degradadas las funciones OCR/recuperación PDF de
`documents`, incluido el OCR de PDF e imágenes dentro de ZIP; la ausencia de
`tesseract` también degrada el OCR documental de `image`. LibreOffice
(`soffice`) es opcional pero necesario para el backend preferido de DOC/XLS/PPT
heredados; `catdoc`, `xls2csv` y `catppt` pueden cubrir individualmente esos
formatos. El probe estático de capacidades busca estos ejecutables únicamente
en `PATH`; no interpreta overrides de una invocación operativa. Sus estados
declaran presencia, no validan los rangos de versiones: el resolver hermético y
`pip check` son las barreras de compatibilidad antes de promover el runtime.

En Windows, los wheels nativos del cierre `full` también requieren el Microsoft
Visual C++ v14 Redistributable x64. Conserva el instalador firmado y su hash en
la evidencia offline. Tras instalarlo, importa explícitamente PyMuPDF, ONNX
Runtime, PySide6, PyAV, CTranslate2 y OpenCV desde el venv candidato: la metadata
y `pip check` no detectan una DLL del sistema ausente.

`--no-deps` permite comprobar versión y ayuda porque esas rutas de arranque no
importan engines. No demuestra que `KnowledgeSearchService`, inventario o una
ruta operativa funcionen: para ello el venv aislado debe contener al menos el
cierre base de `complexipy`, `grimp`, `mypy`, `rich`, `ruff` y `xxhash`. Ruff,
Mypy, Grimp y Complexipy deben resolverse desde el mismo runtime Python.
Pyright pertenece a la preparación separada de
`trusted-static`; su ausencia no impide `protected`, pero deja su proveedor y el
consenso de tipos degradados. Ningún probe de instalación debe cargar o
descargar modelos; la disponibilidad de modelos
se valida después, de forma offline y contra cachés explícitas.

Conserva el wheel y sdist `0.8.0`, su `constraints.txt`, el inventario del
wheelhouse, hashes de procedencia y resultados de las barreras como evidencia
separada; no atribuyas a esos artefactos los resultados históricos de `0.6.0`.

## Qué demuestra cada modalidad

| Modalidad | Uso válido | Lo que no demuestra |
|---|---|---|
| `--no-deps` | Integridad instalable del wheel, versión y entrypoints ligeros | Runtime completo, compatibilidad de dependencias o `pip check` limpio |
| `--system-site-packages` | Compatibilidad puntual con el inventario global de ese equipo | Aislamiento, portabilidad, reproducibilidad o ausencia de dependencias accidentales |
| Venv hermético | Instalación aislada con resolución exclusiva desde un wheelhouse completo | Reproducibilidad futura si no se conservan wheels, constraints y hashes |
| Wheelhouse | Fuente local explícita de artefactos compatibles | Corrección por sí sola; debe verificarse su cierre transitivo y procedencia |

## Validación deliberada con `--no-deps`

Utiliza una ruta de venv dedicada. Este procedimiento no instala ni modifica NeoCortex globalmente:

```powershell
py -3 -m venv C:\Ruta\VenvValidacion
C:\Ruta\VenvValidacion\Scripts\python.exe -m pip install --no-index --no-deps C:\Ruta\Dist\neocortex_framework-0.6.0-py3-none-any.whl
C:\Ruta\VenvValidacion\Scripts\Neocortex.exe --version
C:\Ruta\VenvValidacion\Scripts\python.exe -m neocortex --version
C:\Ruta\VenvValidacion\Scripts\Neocortex.exe --help
C:\Ruta\VenvValidacion\Scripts\python.exe -m pip check
```

El último comando debe fallar si el venv contiene únicamente NeoCortex. En la validación fechada faltaron: `faster-whisper`, `nudenet`, `pdfminer-six`, `pillow`, `pymupdf`, `pyside6`, `pytesseract`, `rich` y `xxhash`. No presentes esta modalidad como una instalación operativa.

## Instalación hermética desde un wheelhouse completo

Un wheelhouse válido debe incluir:

1. el wheel exacto de NeoCortex;
2. todas las dependencias base y sus transitivas para CPython 3.13/Windows y la arquitectura destino;
3. `fastembed==0.8.0`, `numpy==2.4.6` y sus transitivas si se instalará el extra `semantic`;
4. `setuptools==82.0.1` si se permitirá construir desde sdist;
5. un inventario de nombres, versiones, origen y hashes calculado durante su aprovisionamiento autorizado.

Desde un checkout que contenga `constraints.txt`, la instalación base es:

```powershell
py -3 -m venv C:\Ruta\VenvNeoCortex
C:\Ruta\VenvNeoCortex\Scripts\python.exe -m pip install --no-index --find-links C:\Ruta\Wheelhouse --constraint C:\Ruta\Checkout\constraints.txt neocortex-framework==0.6.0
C:\Ruta\VenvNeoCortex\Scripts\python.exe -m pip check
C:\Ruta\VenvNeoCortex\Scripts\Neocortex.exe --version
C:\Ruta\VenvNeoCortex\Scripts\Neocortex.exe --help
```

Para el extra semántico cambia el requisito por:

```powershell
C:\Ruta\VenvNeoCortex\Scripts\python.exe -m pip install --no-index --find-links C:\Ruta\Wheelhouse --constraint C:\Ruta\Checkout\constraints.txt "neocortex-framework[semantic]==0.6.0"
```

Una instalación es hermética sólo si el venv se creó sin `--system-site-packages`, las búsquedas de índice están deshabilitadas y todos los artefactos proceden del wheelhouse autorizado. Ejecuta `pip check` antes de promoverla.

### Bloqueo local reproducido

El wheelhouse disponible durante la auditoría contenía únicamente el wheel de NeoCortex. Este comando seguro y sin red:

```powershell
C:\Ruta\VenvNeoCortex\Scripts\python.exe -m pip install --no-index --find-links C:\Ruta\Wheelhouse --constraint C:\Ruta\Checkout\constraints.txt neocortex-framework==0.6.0
```

terminó con `ResolutionImpossible`: no había una distribución compatible para `faster-whisper`, mientras `constraints.txt` exigía `faster-whisper==1.2.1`. Esto describe el inventario local, no una incompatibilidad demostrada de `faster-whisper` con NeoCortex.

## Instalación desde sdist

Prefiere el wheel para una instalación offline. El sdist activa el backend declarado en `pyproject.toml` y requiere `setuptools==82.0.1` durante el build aislado:

```powershell
C:\Ruta\VenvNeoCortex\Scripts\python.exe -m pip install --no-index --find-links C:\Ruta\Wheelhouse --constraint C:\Ruta\Checkout\constraints.txt C:\Ruta\Dist\neocortex_framework-0.6.0.tar.gz
```

Si `setuptools==82.0.1` no está en el wheelhouse, pip se abstiene antes de construir. `--no-build-isolation` reutiliza el build backend ya instalado en el venv y sólo debe emplearse cuando esa versión se haya instalado y verificado explícitamente; no convierte el proceso en hermético por sí mismo.

## `--system-site-packages` sólo para diagnóstico

La siguiente modalidad fue comprobada, pero no es el procedimiento recomendado de despliegue:

```powershell
py -3 -m venv --system-site-packages C:\Ruta\VenvDiagnostico
C:\Ruta\VenvDiagnostico\Scripts\python.exe -m pip install --no-index --no-deps C:\Ruta\Dist\neocortex_framework-0.6.0-py3-none-any.whl
C:\Ruta\VenvDiagnostico\Scripts\python.exe -m pip check
```

Un resultado limpio depende del entorno global existente. Registra por separado las versiones globales y no copies ese venv como si fuera autocontenido.

## Actualización, verificación y rollback

1. Conserva el venv vigente sin modificar.
2. Crea otro venv dedicado y realiza allí la instalación offline.
3. Verifica `pip check`, versión, ayuda y los comandos operativos autorizados sobre fixtures.
4. Cambia el launcher o procedimiento operativo únicamente después de aprobar la validación.
5. Para rollback, vuelve a seleccionar el venv anterior; no edites manualmente metadata ni números de versión y no sustituyas la instalación global.

Conserva wheel, sdist, `constraints.txt`, inventario del wheelhouse, hashes y logs de validación asociados a cada promoción. La procedencia y las decisiones sobre licencias de terceros se documentan en `THIRD_PARTY_LICENSE_INVENTORY.md`.
