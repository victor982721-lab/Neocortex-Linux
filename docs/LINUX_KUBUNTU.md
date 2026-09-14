# Kubuntu/Linux

NeoCortex `0.14.0` tiene como única plataforma activa Kubuntu/Ubuntu 26.04
x86-64, con CPython 3.13–3.14 con GIL. CPython 3.14 es el runtime personal de
referencia y 3.13 dispone de la instalación ordinaria offline descrita aquí.

## Rutas XDG

| Recurso | Ruta predeterminada |
|---|---|
| Corpus | `${XDG_DOCUMENTS_DIR}/NeoCortex/Corpus` |
| Estado | `${XDG_STATE_HOME:-~/.local/state}/Neocortex/state` |
| Configuración | `${XDG_CONFIG_HOME:-~/.config}/Neocortex` |
| Datos y modelos | `${XDG_DATA_HOME:-~/.local/share}/Neocortex` |
| Releases | `~/.local/share/Neocortex/releases` |
| Release activa | `~/.local/share/Neocortex/current` |
| Launcher | `~/.local/share/Neocortex/bin/Neocortex` |
| Alias | `~/.local/bin/Neocortex` |
| Entrada KDE | `~/.local/share/applications/neocortex.desktop` |

`XDG_DOCUMENTS_DIR` se lee como datos, sin ejecutar `user-dirs.dirs`. Sólo se
aceptan rutas absolutas o expansiones literales seguras de HOME.

## Contrato Linux

- inventario portable sin depender de USN;
- identidad física con `st_dev`/`st_ino`;
- `birthtime_ns=-1` cuando no existe birthtime real;
- enlaces simbólicos no seguidos;
- árboles internos XDG, releases, modelos y launchers excluidos;
- procesos externos en sesión/grupo propios;
- límites de memoria impuestos o abstención;
- cancelación mediante SIGTERM y escalamiento acotado a SIGKILL.

Windows/NTFS se conserva sólo como legado de lectura o compatibilidad interna y
no se prueba ni expone como plataforma soportada.

## Prerrequisitos

No instales pip ni dependencias Python globalmente. El host puede requerir:

~~~bash
sudo apt install python3.14-venv qpdf tesseract-ocr \
  tesseract-ocr-spa tesseract-ocr-eng ffmpeg rsync \
  desktop-file-utils
~~~

Instala únicamente los idiomas y binarios necesarios. Las rutas deben hacer
preflight y declarar cobertura cuando falta una herramienta.

## Instalación ordinaria desde una extracción

Kubuntu/CPython 3.14 sigue siendo la referencia productiva; CPython 3.13 con GIL
también está soportado. GitHub **Code → Download ZIP** entrega los archivos
necesarios, sin preparación adicional ni historial Git. Usa un venv nuevo, sin
`--system-site-packages`, para evitar paquetes y precargas del Python global.

`dev-resources/offline/artifacts/` contiene los wheels originales del cierre
transitivo para CPython 3.13/Linux x86_64, `locks/` fija versiones y SHA-256 por
capacidad, y `provenance.json` conserva origen y licencias. No requiere cachés
personales, Git LFS ni otra descarga. `constraints-linux-cp313.lock` reúne ese
cierre, mientras el lock productivo CPython 3.14 permanece independiente.

| Capacidad | Extra / recurso | Incluido offline |
|---|---|---|
| Runtime base, inventario, texto y Code | `packaging`, `rich`, `xxhash` y transitivos | Sí, lock `runtime-base-cp313-linux-x86_64.lock` |
| Construcción ordinaria | `build`, backend `setuptools` y transitivos | Sí, lock `build-cp313-linux-x86_64.lock` |
| Pruebas base | `test-base`: pytest, backend `setuptools==83.0.0` para auditorías de empaquetado y transitivos, sin plugins obligatorios | Sí, lock `test-base-cp313-linux-x86_64.lock` |
| Documentos e imagen | `documents`, `image`: Pillow, PyMuPDF, pdfminer.six, pytesseract y transitivos | Sí, lock `documents-image-cp313-linux-x86_64.lock` |
| Inferencia | `semantic`, `audio` y pesos originales locales | No |
| UI / MCP | `ui` / `agent` | No |
| Herramientas de desarrollo adicionales | `analysis` | No; no es requisito de las pruebas base |

Desde la extracción, con CPython 3.13 disponible:

~~~bash
Source="$PWD"
Offline="$Source/dev-resources/offline"
Lab="$(mktemp -d)"
python3.13 -I -m venv "$Lab/venv"
Python="$Lab/venv/bin/python"
export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_FIND_LINKS="$Offline/artifacts"
"$Python" -m pip install --require-hashes \
  -r "$Offline/locks/runtime-base-cp313-linux-x86_64.lock" \
  -r "$Offline/locks/build-cp313-linux-x86_64.lock" \
  -r "$Offline/locks/test-base-cp313-linux-x86_64.lock"
"$Python" -m build --wheel --no-isolation --outdir "$Lab/wheels" "$Source"
"$Python" -m pip install --no-deps "$Lab"/wheels/neocortex_framework-*.whl
"$Python" -m pip check
cd "$Lab"
"$Lab/venv/bin/Neocortex" --help
"$Python" -m neocortex --version
~~~

No se usa el launcher personal, `current`, integración de escritorio ni
`tools/release_linux.py`. El wheel contiene los recursos de runtime y excluye
el almacén de dependencias de desarrollo; el paquete instalado no necesita la
extracción ni su directorio de trabajo. Para ampliar a documentos e imagen:

~~~bash
"$Python" -m pip install --require-hashes \
  -r "$Offline/locks/documents-image-cp313-linux-x86_64.lock"
"$Python" -m pip check
~~~

Un recorrido pequeño del producto real, repetible sobre los fixtures incluidos:

~~~bash
mkdir -p "$Lab/corpus"
cp -R "$Source/tests/fixtures/headless_product/base/." "$Lab/corpus/"
NEOCORTEX_PROGRESS_STREAM=1 "$Lab/venv/bin/Neocortex" \
  --root "$Lab/corpus" --state-directory "$Lab/state" \
  --code-project-root "$Lab/corpus/code" --route text,code --strict-exit-codes
"$Lab/venv/bin/Neocortex" --root "$Lab/corpus" \
  --state-directory "$Lab/state" --status --status-json
~~~

Repite la misma orden de procesamiento para comprobar replay/caché y consulta
los owners de estado publicados; las pruebas instaladas de
`tests/test_headless_product_workflows.py` verifican también cambios de un
archivo, reanudación acotada, documentos, OCR, video y ausencia de modelos.

Los wheels nativos son cp313 o `abi3` aplicable, no cp314 ni free-threaded, y
declaran su mínimo manylinux/glibc. El intérprete y los ejecutables de sistema
se provisionan por separado: FFmpeg/ffprobe para multimedia y Tesseract con el
idioma solicitado para OCR. Los formatos Office indexables son DOCX/XLSX/PPTX/ODT
y se procesan con lectores nativos, sin convertidores externos. `qpdf` no es
requisito global, ni la ausencia de Qt impide CLI.
La UI necesita PySide6 y sus bibliotecas; `QT_QPA_PLATFORM=offscreen` o un
display Xvfb permiten ejecución headless, pero no acreditan KDE/Wayland ni KIO.

Las pruebas se seleccionan **antes de importar los módulos**:

~~~bash
cd "$Source"
"$Python" -m pytest --capabilities=base
"$Python" -m pytest --capabilities=base,documents,image
~~~

La selección predeterminada sigue siendo `all`; también existen `inference`,
`ui`, `platform` y `agent`. Seleccionar una capacidad no instala dependencias
ni convierte su ausencia en procesamiento correcto. Véase
[ejecución y observabilidad](OPERATIONS.md) para rutas, estado y límites.
La instalación sigue los contratos de [venv](https://docs.python.org/3.13/library/venv.html),
[instalación repetible de pip](https://pip.pypa.io/en/stable/topics/repeatable-installs/)
y [tags de wheels](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/).

## Wheelhouse y release personal

La release canónica no resuelve paquetes desde Internet. Exige un wheelhouse
local con `wheelhouse-manifest.json`, wheels compatibles y hashes válidos.

~~~bash
python3.14 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --wheelhouse "$Wheelhouse" --prepare-models --desktop
python3.14 tools/release_linux.py verify
~~~

`install --corpus-root` define la raíz operativa persistente; es opcional y, si
se omite, usa la política de plataforma, no `NEOCORTEX_CORPUS_ROOT` del proceso.
La instalación y `verify` crean automáticamente otra raíz vacía temporal para
sus smokes, la eliminan al terminar y nunca la graban como corpus operativo.
No existe un argumento `--smoke-corpus-root`.

El launcher conserva el default instalado y admite `NEOCORTEX_CORPUS_ROOT` como
override por proceso, sin reconfigurar la instalación. `verify --corpus-root`
no cambia ese default: comprueba que coincida con el receipt. Rollback conserva
la raíz operativa del último receipt, no la de un smoke histórico.

Si falta una rueda o no coincide su hash, la instalación falla cerrada. No
modifiques constraints ni uses paquetes globales para completar el entorno.

`--prepare-models` es una adquisición explícita que puede descargar pesos;
no está cubierta por el cierre offline de paquetes Python. Omítela si no está
autorizada esa adquisición. Los modelos compartidos se conservan fuera de las
releases y se verifican antes de promover el launcher.

## Verificación

Una release válida demuestra:

1. `source_sha` del manifest igual al SHA construido;
2. árbol inmutable y dependencias compatibles;
3. receipt, manifest, launcher y alias concilian con `current`, y el contenido
   exacto y hash del launcher se comprueban antes de ejecutarlo;
4. comandos públicos funcionan sin `PYTHONPATH`;
5. desktop file y PySide6 offscreen pasan cuando se solicitó KDE;
6. los smokes automáticos usan una raíz vacía temporal y el replay del producto
   usa fixtures contenidos, no el corpus real;
7. sólo quedan `current` y el rollback inmediato;
8. staging queda vacío.

Cuando el runtime expone `effective_paths.corpus`, `verify` comprueba la raíz
efectiva. Un runtime anterior que no entregue ese campo informa
`verification_effective_corpus_checked=false`, no una comprobación inventada.
Un launcher legacy que descarta el override requiere reparación o promoción
autorizada; actualizar el helper en fuente no modifica el launcher instalado.

## Estado actual de mutación

`--dedupe --apply` y `--all --apply` seleccionan el backend Linux
`posix-renameat2+kio-trash` sólo después de validar la raíz, identidad, keeper y
configuración de Papelera. El backend usa claim same-filesystem, KIO nativo,
receipt y restauración no-replace; no usa `gio`, shell ni borrado permanente.
Una carrera, cuota con autolimpieza o evidencia insuficiente deja el efecto en
`recovery_required`/`blocked` individualmente, sin detener recursos válidos.
`curate apply` y `curate restore` conservan su frontera grant-bound separada.

## Backend objetivo de Papelera

`neocortex/safety/kio_trash.py` expone el adaptador: descubre el
primer cliente disponible entre `kioclient6`, `kioclient5` y `kioclient`, valida
configuración y snapshot, usa `move <origen> trash:/` con timeout acotado y
requiere un verificador antes de emitir receipt. La configuración de cuota se
lee sin modificar el `ktrashrc` global; se rechaza cualquier política que pueda
podar entradas existentes. La canaria instalada debe confirmar el efecto real.

La release `0.14.0` integra esa foundation con el ledger de acciones, preflight
same-filesystem y recovery sobre fixtures. La restauración automática nativa se
verifica por bytes/identidad; la única restauración visual de Dolphin permanece
como comprobación humana independiente.

Preflight:

- identificar mount/topdir, UID y directorio de Trash permitido;
- verificar permisos, sticky bit cuando aplique y contención;
- capturar identidad, tamaño, mtime, número de links y hash requerido;
- reservar nombre no colisionante;
- rechazar filesystem cruzado, symlink y hard link no soportado.

Aplicación:

- crear y sincronizar el `.trashinfo`;
- mover con semántica no-replace dentro del mismo dispositivo;
- registrar receipt antes/después de la frontera;
- verificar bytes y entrada de recuperación;
- mantener `recovery_required` ante resultado ambiguo.

Nunca habrá fallback a `gio trash`, borrado directo o una cuarentena propia. Si
KIO no existe, no puede escribir su configuración, devuelve un error ambiguo o
no permite verificar el efecto, la operación queda abstendida y conciliable.

## KDE

`Neocortex --ui` usa PySide6 y los mismos contratos que CLI. La interfaz no
eleva permisos ni habilita controles que el backend Linux rechaza. La apertura
de una ventana no es evidencia de procesamiento ni autorización.

## Rollback

El rollback selecciona la release anterior verificada. No edita el estado para
hacerlo compatible ni borra una release en uso. Después del cambio ejecuta
`tools/release_linux.py verify` y el smoke público.

La operación cotidiana se describe en [OPERATIONS.md](OPERATIONS.md) y la
recuperación en [RECOVERY.md](RECOVERY.md).
