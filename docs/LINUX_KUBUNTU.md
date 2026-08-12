# Kubuntu/Linux

NeoCortex `0.9.0` admite Kubuntu/Ubuntu 26.04 sobre Linux x86-64 con CPython
3.14 como entorno personal de referencia. Python 3.13 permanece cubierto por
la matriz de CI. El modo Linux conserva inventario, procesamiento, catálogo,
búsqueda, Semantic y la interfaz KDE; las mutaciones del corpus continúan
reservadas al backend seguro de Windows.

## Contrato de seguridad

- El inventario Linux es un recorrido completo portable. No presupone USN.
- La identidad física usa `st_dev` + `st_ino`; cuando no existe nacimiento real
  persiste `birthtime_ns=-1`. `ctime` nunca se presenta como nacimiento.
- Los enlaces simbólicos no se siguen. Estado, configuración, modelos,
  runtimes, launchers y archivos `.desktop` se excluyen del corpus.
- Cada subproceso POSIX obtiene sesión/grupo propios. La cancelación alcanza el
  árbol con `SIGTERM` y, si es necesario, `SIGKILL`.
- Un límite de memoria solicitado debe imponerse mediante `RLIMIT_AS` o
  `/usr/bin/prlimit`; si no se puede, NeoCortex se abstiene.
- `--apply` y `--organization-apply` se rechazan antes de crear estado, con
  código `2` y razón estable `linux_mutation_backend_unavailable`. No existe un
  reemplazo inseguro basado en `Path.rename`.

## Rutas XDG

La política central resuelve las rutas sin ejecutar el contenido de
`user-dirs.dirs`:

| Recurso | Ruta predeterminada |
|---|---|
| Corpus | `${XDG_DOCUMENTS_DIR}/NeoCortex/Corpus` |
| Estado | `${XDG_STATE_HOME:-~/.local/state}/Neocortex/state` |
| Configuración | `${XDG_CONFIG_HOME:-~/.config}/Neocortex` |
| Datos, releases y modelos | `${XDG_DATA_HOME:-~/.local/share}/Neocortex` |
| Release activa | `~/.local/share/Neocortex/current` |
| Launcher estable | `~/.local/share/Neocortex/bin/Neocortex` |
| Alias de usuario | `~/.local/bin/Neocortex` |
| Entrada KDE | `~/.local/share/applications/neocortex.desktop` |

`XDG_DOCUMENTS_DIR` se toma de
`${XDG_CONFIG_HOME:-~/.config}/user-dirs.dirs`. Sólo se aceptan una ruta
absoluta o los prefijos literales `$HOME`/`${HOME}`; texto con sustituciones de
comandos, variables adicionales o `..` se ignora. Si no hay una entrada
válida, se usa `~/Documents`.

## Prerrequisitos

No instale dependencias Python, `pip` ni Node globalmente. El instalador crea un
runtime aislado y obtiene Node dentro de la release. En el host de referencia:

```bash
sudo apt install python3.14-venv qpdf tesseract-ocr tesseract-ocr-spa \
  tesseract-ocr-eng tesseract-ocr-deu tesseract-ocr-chi-sim \
  tesseract-ocr-chi-tra tesseract-ocr-osd ffmpeg libreoffice catdoc rsync \
  desktop-file-utils
```

En Ubuntu, el paquete `catdoc` aporta `catdoc`, `xls2csv` y `catppt`.
NeoCortex prioriza estos dos últimos para XLS y PPT heredados, respectivamente,
y conserva LibreOffice como alternativa cuando el extractor específico no está
disponible.

La instalación necesita red para resolver wheels binarios, Node y, cuando se
solicita, modelos. Requiere al menos 4 GiB libres para modelos y cachés. No
promueve una release que falle imports nativos, `pip check`, Node/Pyright,
doctor de plataforma o el arranque PySide6 offscreen.

## Instalación versionada

Desde un checkout limpio y situado en el commit que se desea instalar:

```bash
cd "$HOME/Neocortex/Repository"
python3.14 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --prepare-models \
  --desktop
```

El identificador inmutable tiene la forma
`<version>-<sha12>-cp314-linux-x86_64`. El wheel se construye con
`setuptools==83.0.0`, se instala con el extra `full`, constraints y únicamente
wheels binarios. La dependencia transitiva `yattag`, publicada sólo como sdist,
se descarga con versión y SHA-256 fijados, se normaliza primero mediante
`pip wheel` y sólo entonces entra al instalador como wheel local. Node `24.18.1`
y Pyright `1.1.411` quedan dentro de la release.
El instalador crea `--corpus-root` y sus padres cuando faltan, exige que el
resultado sea un directorio real y registra en el recibo si tuvo que crearlo.
No añade archivos al corpus ni inicia una corrida.
La activación de `current` se realiza mediante reemplazo atómico bajo `flock`;
el venv se crea en su ruta final no activa —los venv no son movibles—, se vuelve
de sólo lectura tras las verificaciones y sólo entonces puede recibir el enlace
`current`. Las releases anteriores se conservan.

El último paso publica el launcher, el alias, el icono y la entrada KDE. Si la
preparación de modelos queda incompleta, conserva cachés reanudables y la
release candidata, pero no cambia `current` ni publica el acceso de escritorio.
Los recibos JSON de construcción, artefactos, hashes, activación y resultado se
guardan en:

```text
${XDG_STATE_HOME:-~/.local/state}/Neocortex/state/installation-receipts
```

Después de los pilotos por ruta, `Neocortex --all` ejecuta primero el
autoanálisis protegido del checkout canónico y luego el flujo documental. Si el
directorio del corpus se elimina después de instalar, el autoanálisis todavía
se publica y la etapa documental se abstiene con `corpus_unavailable` y código
`2`, sin traceback.

## Modelos

La descarga siempre es explícita y secuencial para no mantener varios modelos
en memoria:

```bash
Neocortex models prepare
Neocortex models prepare --json
```

Prepara Jina, MiniLM compacto, CLIP texto, CLIP visión y Whisper `small`, y
valida además el modelo NudeNet incluido en su distribución. Los modelos se
comparten entre releases bajo `~/.local/share/Neocortex/models`; Whisper usa
CPU/int8.

La inspección es estrictamente local, de sólo lectura y nunca inicia una
descarga:

```bash
Neocortex models status
Neocortex models status --json
```

`all_prepared=true` es la barrera para publicar KDE cuando se pidió preparar
modelos.

## Verificación y rollback

```bash
python3.14 tools/release_linux.py verify
Neocortex doctor capabilities --json
Neocortex doctor platform --json
Neocortex models status --json
Neocortex status --scope all
Neocortex search "consulta representativa" --scope personal --limit 5
Neocortex review value --scope personal --limit 10
desktop-file-validate "$HOME/.local/share/applications/neocortex.desktop"
```

La barrera del agente local inicia `Neocortex agent serve` desde un cliente MCP
por stdio y exige initialize, `tools/list` y una llamada read-only real. No debe
abrir puertos ni crear rutas. Para la GUI, capture la ventana activa y verifique
visualmente la página Consulta; un test offscreen por sí solo no sustituye la
comprobación KDE pública.

`doctor platform` tiene un esquema versionado e informa sistema, rutas,
inventario, identidad, contención, elevación y mutación. En Linux debe declarar
la plataforma compatible, inventario `portable-full-scan`, contención POSIX,
elevación no requerida y mutación intencionalmente no disponible.

Para volver a la release anterior registrada:

```bash
python3.14 tools/release_linux.py rollback
```

También puede elegir una release conservada:

```bash
python3.14 tools/release_linux.py rollback \
  --release 0.9.0-0123456789ab-cp314-linux-x86_64
```

Rollback sólo cambia atómicamente el enlace activo y deja un recibo; no elimina
artefactos.

## Uso KDE y piloto

La entrada de aplicaciones se llama **NeoCortex**. La ventana muestra
“modo portátil Linux”, no solicita elevación y mantiene desactivados los
controles de mutación. Inventario, PDF, DOCX, Office, ZIP anidados con OCR,
texto/correo/Office heredado, audio, video, imagen, Code, catálogo, Semantic y
búsqueda siguen disponibles. La página **Consulta** ofrece status, search, ask
y review value sobre scopes fijos, sin modificar estado. DOC heredado prioriza
LibreOffice; XLS y PPT priorizan `xls2csv` y `catppt`. Todos extraen texto sin
modificar el original. Tesseract conserva `spa+eng` como default y permite
perfiles latín/Han/auto con alemán, chino simplificado, tradicional y OSD.
FFmpeg/FFprobe sostienen la ruta Video acotada.

La primera ejecución debe usar una raíz de laboratorio con 20–50 fixtures, una
sola ruta por vez y un máximo de 10–15 minutos. Compare hashes antes y después;
nunca use `--apply`, `--organization-apply` ni `--all` en el piloto.

## Copia futura desde Windows

Cuando exista un volumen o backup Windows montado, copie sólo originales a
ext4, nunca SQLite ni identidades NTFS. No use `--delete`:

```bash
rsync -a --info=progress2 /medio/windows/Corpus/ \
  "$HOME/Documentos/NeoCortex/Corpus/"
rsync -a --checksum --dry-run /medio/windows/Corpus/ \
  "$HOME/Documentos/NeoCortex/Corpus/"
```

El segundo pase debe quedar vacío. Después construya estado Linux nuevo; no
migre las bases de estado de Windows.
