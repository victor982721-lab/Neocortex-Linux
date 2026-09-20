# Kubuntu/Linux

NeoCortex `0.14.0` tiene como única plataforma activa Kubuntu/Ubuntu 26.04
x86-64, con CPython `>=3.13.5,<3.14` con GIL. Todas las versiones patch de
CPython 3.13 usan el mismo contrato ABI `cp313`.

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

Windows/NTFS no es una plataforma soportada ni una ruta de ejecución del
producto Linux.

## Prerrequisitos

No instales pip ni dependencias Python globalmente. El host puede requerir:

~~~bash
sudo apt install python3.13-venv qpdf tesseract-ocr \
  tesseract-ocr-spa tesseract-ocr-eng ffmpeg rsync
~~~

Instala únicamente los idiomas y binarios necesarios. Las rutas deben hacer
preflight y declarar cobertura cuando falta una herramienta.

## Instalación ordinaria desde una extracción

CPython 3.13 con GIL es el único runtime productivo soportado. GitHub
**Code → Download ZIP** entrega los archivos
necesarios, sin preparación adicional ni historial Git. Usa un venv nuevo, sin
`--system-site-packages`, para evitar paquetes y precargas del Python global.

`dev-resources/offline/artifacts/` contiene los wheels originales del cierre
transitivo para CPython 3.13/Linux x86_64, `locks/` fija versiones y SHA-256 por
capacidad, y `provenance.json` conserva origen y licencias. No requiere cachés
personales, Git LFS ni otra descarga. `constraints-linux-cp313-runtime.lock` es
el lock de la release productiva (base + documentos/imagen); el lock agregado de
suministro permanece en `constraints-linux-cp313.lock`. No existe un cierre
paralelo para CPython 3.14. Dedupe y las claves de idempotencia usan SHA-256
completo de la biblioteca estándar.

| Capacidad | Extra / recurso | Incluido offline |
|---|---|---|
| Runtime base, inventario y texto | `packaging`, `rich` y transitivos | Sí, lock `runtime-base-cp313-linux-x86_64.lock` |
| Construcción ordinaria | `build`, backend `setuptools` y transitivos | Sí, lock `build-cp313-linux-x86_64.lock` |
| Pruebas base | `test-base`: pytest, backend `setuptools==83.0.0` para auditorías de empaquetado y transitivos, sin plugins obligatorios | Sí, lock `test-base-cp313-linux-x86_64.lock` |
| Documentos e imagen | `documents`, `image`: Pillow, PyMuPDF, pdfminer.six, pytesseract y transitivos | Sí, lock `documents-image-cp313-linux-x86_64.lock` |
| Inferencia | `semantic`, `audio` y pesos originales locales | No |
| MCP / agente | `agent` | No |
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
  --route text --strict-exit-codes
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
requisito global para todas las rutas.

Las pruebas se seleccionan **antes de importar los módulos**:

~~~bash
cd "$Source"
"$Python" -m pytest --capabilities=base
"$Python" -m pytest --capabilities=base,documents,image
~~~

La selección predeterminada sigue siendo `all`; también existen `inference`,
`platform` y `agent`. Seleccionar una capacidad no instala dependencias
ni convierte su ausencia en procesamiento correcto. Véase
[ejecución y observabilidad](OPERATIONS.md) para rutas, estado y límites.
La instalación sigue los contratos de [venv](https://docs.python.org/3.13/library/venv.html),
[instalación repetible de pip](https://pip.pypa.io/en/stable/topics/repeatable-installs/)
y [tags de wheels](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/).

## Wheelhouse y release personal

La release canónica no resuelve paquetes desde Internet. Exige un wheelhouse
local con `wheelhouse-manifest.json`, wheels compatibles y hashes válidos.

~~~bash
python3.13 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --wheelhouse "$Wheelhouse" \
  --sqlite-policy "$SQLitePolicy" \
  --sqlite-policy-sha256 "$SQLitePolicySHA256" --require-models
python3.13 tools/release_linux.py verify
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

`SQLitePolicySHA256` es el SHA-256 canónico de una política de builds SQLite
revisada por separado. Debe recibirse de esa revisión, no aceptarse desde el
mismo archivo que valida. `--sqlite-policy` admite una política externa; si se
omite, usa `dev-resources/offline/sqlite-runtime-policy.json` en la fuente. No
se incluye una aprobación automática del SQLite del host. El hash acredita
integridad del archivo; la revisión del proveedor acredita el build concreto.

Antes de crear el corpus o activar una release se verifican todos los hashes,
tags Python/ABI/plataforma de los wheels, `Requires-Python`, dependencias
transitivas y markers del perfil productivo. Los extras `semantic`, `audio` y
`agent` siguen siendo perfiles optativos y requieren su propio wheelhouse y lock
autenticados; no se infieren ni se instalan desde la release base. No modifiques
constraints ni uses paquetes globales para completar el entorno.

`--require-models` exige modelos locales completos antes de promover el launcher.
El nombre anterior `--prepare-models` se conserva como alias de esta exigencia;
el instalador offline ya no invoca adquisición de pesos. La preparación se
realiza por separado, de forma explícita y con su autorización propia. Los
modelos compartidos se conservan fuera de las releases. La presencia de pesos
y `pip check` no sustituyen el smoke de inferencia de las capacidades solicitadas.

El paquete offline incluido para CPython 3.13 continúa limitado a los perfiles
de la tabla anterior. Una aceptación ampliada con Semantic/audio/MCP necesita
además inventariar el intérprete, bibliotecas nativas, ejecutables e idiomas de
OCR, y los modelos y tokenizers originales con hashes y licencias. Esa aceptación
se ejecuta desde el wheel instalado, con red denegada, HOME/XDG/TMP privados, sin
`PYTHONPATH` ni cachés del checkout. Incluye primer procesamiento y replay de
fixtures por capacidad, inventario instalado y SHA de fuente; un perfil base
correcto no acredita de forma implícita las extras optativas. La prueba
`test_semantic_numpy_binding.py` comprueba la ruta NumPy instalada concreta, sin
acreditar de forma implícita toda versión `<3`.

## Evidencia nativa SQLite

Los manifests, receipts y verificaciones nuevos usan schema 2. El bloque
`native_runtime` contiene la medición completa y sus hashes, identidad de
Python, SQLite `source_id` y opciones de compilación, `_sqlite3` y las bibliotecas
`libsqlite3` realmente mapeadas. Un `_sqlite3` builtin se identifica expresamente
mediante los bytes del ejecutable y sus contenedores `libpython` si existen;
la ausencia de biblioteca separada permanece
visible como `static_or_unobserved`. La identidad excluye rutas absolutas para
conservarse después del rename de staging.

Los ejecutables de Python del venv se materializan como archivos regulares
dentro de la release, también cuando el proveedor se encuentra fuera de
`/usr/bin`. Las copias conservan los bytes que acredita la política nativa.
El venv mantiene su referencia a la biblioteca estándar del proveedor mediante
`pyvenv.cfg`; ese runtime base debe permanecer disponible.

El probe ejecuta el `bin/python` de la release en un temporal propio y verifica
FTS5, JSON, claves foráneas, rollback y un commit WAL con `synchronous=FULL`.
Nunca abre bases de datos del corpus o del estado. WAL/FULL comprueba configuración
y un commit local; no demuestra durabilidad frente a un corte de energía.

La política fija identidades completas y evidencia revisada `upstream` o
`vendor_backport`. Una versión nominal o capacidades correctas sin ese registro
producen `unaccredited`; una capacidad ausente produce `incompatible`. Sólo
`approved` puede activar una nueva release o un rollback v2. El instalador
repite la medición tras el rename y junto a la activación; `verify` y
`doctor platform` vuelven a medir y distinguen evidencia almacenada de observación
actual. El entorno candidato y el launcher eliminan `LD_PRELOAD`,
`LD_LIBRARY_PATH` y `LD_AUDIT` para mantener el mismo contrato de carga nativa.

Schema 1 sigue siendo legible sin reescritura: su verificación informa
`verified=false` y `native_runtime.status=legacy_unaccredited`. Esto indica falta
de acreditación, no corrupción. Un v2 incompleto o alterado falla cerrado y no
permite retroceder silenciosamente a otro receipt. Una política distinta o la
migración desde v1 requieren una release nueva; no se modifica el manifest
inmutable existente ni se cambia `current` cuando esa preparación falla.

## Verificación

Una release válida demuestra:

1. `source_sha` del manifest igual al SHA construido;
2. árbol inmutable y dependencias compatibles;
3. receipt, manifest, launcher y alias concilian con `current`, y el contenido
   exacto y hash del launcher se comprueban antes de ejecutarlo;
4. comandos públicos funcionan sin `PYTHONPATH`;
5. la ayuda, el estado y el smoke CLI pasan fuera del checkout;
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
`--apply` y `curate restore` conservan sus fences físicas y receipts separadas;
no existe una frontera grant-bound ni una autorización humana intermedia.

## Backend objetivo de Papelera

`neocortex/safety/kio_trash.py` expone el adaptador: descubre el
primer cliente disponible entre `kioclient6`, `kioclient5` y `kioclient`, valida
configuración y snapshot, usa `move <origen> trash:/` con timeout acotado y
requiere un verificador antes de emitir receipt. La configuración de cuota se
lee sin modificar el `ktrashrc` global; se rechaza cualquier política que pueda
podar entradas existentes. La canaria instalada debe confirmar el efecto real.

La release `0.14.1` integra esa foundation con el ledger de acciones, preflight
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

## Rollback

El rollback selecciona la release anterior verificada. No edita el estado para
hacerlo compatible ni borra una release en uso. Después del cambio ejecuta
`tools/release_linux.py verify` y el smoke público.

La operación cotidiana se describe en [OPERATIONS.md](OPERATIONS.md) y la
recuperación en [RECOVERY.md](RECOVERY.md).
