# Kubuntu/Linux

NeoCortex `0.12.0` tiene como única plataforma activa Kubuntu/Ubuntu 26.04
x86-64. CPython 3.14 es el runtime personal de referencia y 3.13 permanece como
piso sintáctico.

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
  tesseract-ocr-spa tesseract-ocr-eng ffmpeg libreoffice catdoc rsync \
  desktop-file-utils
~~~

Instala únicamente los idiomas y binarios necesarios. Las rutas deben hacer
preflight y declarar cobertura cuando falta una herramienta.

## Wheelhouse e instalación

La release canónica no resuelve paquetes desde Internet. Exige un wheelhouse
local con `wheelhouse-manifest.json`, wheels compatibles y hashes válidos.

~~~bash
python3.14 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --wheelhouse "$Wheelhouse" --prepare-models --desktop
python3.14 tools/release_linux.py verify
~~~

Si falta una rueda o no coincide su hash, la instalación falla cerrada. No
modifiques constraints ni uses paquetes globales para completar el entorno.

`--prepare-models` es explícito. Los modelos compartidos se conservan fuera de
las releases y se verifican antes de promover el launcher.

## Verificación

Una release válida demuestra:

1. `source_sha` del manifest igual al SHA construido;
2. árbol inmutable y dependencias compatibles;
3. launcher y alias resuelven a `current`;
4. comandos públicos funcionan sin `PYTHONPATH`;
5. desktop file y PySide6 offscreen pasan cuando se solicitó KDE;
6. smoke y replay usan fixtures, no el corpus real;
7. sólo quedan `current` y el rollback inmediato;
8. staging queda vacío.

## Estado actual de mutación

Las rutas genéricas `--apply` y `--organization-apply` se rechazan antes de
crear estado con `linux_mutation_backend_unavailable`. `curate apply` y
`curate restore` existen como consumidores grant-bound, pero la CLI instalada no
selecciona backend ni run firmado, por lo que también fallan cerrados hasta
recibir una integración explícita. `curate recovery status` y `curate restore
preview` sólo leen evidencia, mientras inventario, extracción, catálogo,
búsqueda, Review y conciliación advisory siguen disponibles.

No uses `Path.rename`, shell, KIO o scripts externos para eludir ese contrato.

## Backend objetivo de Papelera

`neocortex/safety/kio_trash.py` ya prepara el adaptador fail-closed: descubre el
primer cliente disponible entre `kioclient6`, `kioclient5` y `kioclient`, valida
configuración y snapshot, usa `move <origen> trash:/` con timeout acotado y
requiere un verificador del caller antes de emitir receipt. Todavía no está
conectado a `--apply`, promovido ni verificado con KIO real.

La implementación de `0.11.x` integra esa foundation con autorización, ledger,
preflight same-filesystem y recovery sobre fixtures, sin relajar su garantía
path-bound. KIO real, restore y sincronización posterior de owners siguen siendo
gates separados.

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
