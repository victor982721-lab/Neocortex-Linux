# _04_Nucleo_Operativo

Coordinador durable de los componentes compartidos, no destructivo salvo
cuando el usuario solicita explícitamente `--apply`. Actualmente:

1. intenta capturar un cursor USN anterior cuando Windows/NTFS lo ofrece;
2. inventaría de forma portable el perfil seleccionado, excluyendo AppData,
   estado y árboles internos de Neocortex, metadatos VCS, entornos virtuales,
   dependencias instaladas, laboratorios, `.CDX`, temporales reconocibles de
   pruebas, caches/bytecode generados y directorios ocultos;
3. reduce candidatos de duplicado con `neocortex.deduplication` y exige comparación
   exacta antes de cualquier acción destructiva;
4. si USN está disponible, consume su ventana; si no, publica el recorrido
   completo con cursor nulo;
5. repite el inventario, hasta tres veces, si detecta un movimiento estructural
   de directorio que no puede representarse con seguridad mediante eventos
   individuales;
6. valida por firma de contenido las extensiones de formatos conocidos;
7. conserva el checkpoint reconciliado, el cursor opcional, cada acción y el
   estado en SQLite.

La búsqueda final de directorios vacíos reutiliza la misma política compilada
del inventario —rutas exactas, nombres, prefijos, fragmentos, atributos y
restricciones—. No vuelve a entrar en dependencias, temporales o árboles
protegidos que el inventario ya excluyó. Los árboles reconstruibles `build`,
`dist` y `wheelhouse` se excluyen únicamente bajo Neocortex, el framework EPS
canónico y su referencia histórica en OneDrive; esos nombres permanecen
elegibles en cualquier otro lugar del corpus.

El recorrido completo se usa para crear la primera generación válida, cuando
USN no existe o no está accesible, y para recuperarse de una discontinuidad o
de movimientos estructurales que no puedan representarse con seguridad. Con
un cursor compatible, las ejecuciones posteriores aplican únicamente la
ventana USN pendiente. Sin él, vuelven a enumerar la raíz y las rutas comparan
el snapshot publicado contra sus caches por identidad y metadatos; USN acelera
la enumeración, pero no determina la corrección. Cada lote seguro, sus
agregados y su nuevo checkpoint se confirman en la misma transacción SQLite.
Si una identidad o ruta USN no puede resolverse, el lote ambiguo no se aplica y
el cursor durable permanece en el último límite seguro; la ejecución cae a un
recorrido completo. Un proceso interrumpido puede continuar desde el último
lote confirmado.

Las exploraciones completas se publican por generación. Una misma ruta puede
coexistir en una generación anterior y otra en construcción; el checkpoint de
la raíz cambia sólo después de comprobar estado `complete`, cero errores y
agregados consistentes. Una exploración con errores queda `partial`, conserva
la generación publicada anterior y no emite un evento de finalización exitosa.
Una cancelación conserva como `partial` el prefijo ya confirmado. Al iniciar el
siguiente flujo integrado, cualquier `building` heredado de una terminación
abrupta se cierra idempotentemente como `partial`, se invalida si aparecía ligado
a un checkpoint y nunca sustituye la publicación completa anterior.

La salida indica `inventory_mode=full` o `inventory_mode=incremental`. Los
eventos operativos y tiempos de fase se guardan estructuradamente en la tabla
`run_events` de `framework.sqlite3`, junto con las ejecuciones y acciones. No
se crean archivos de log o reportes auxiliares.

La detección de tipos conserva resultados detectados y desconocidos en
`content_type_cache`, identificados por volumen, archivo, tamaño, `mtime`,
`birthtime` y versión del detector. Una corrida caliente no vuelve a abrir
archivos sin cambios. La salida y `run_actions` distinguen `type_cache_hits` y
`type_cache_misses`. Al terminar satisfactoriamente la validación, una
reconciliación generacional elimina en lotes las entradas no vistas y las de
versiones anteriores; se informa como `type_cache_pruned`. Los archivos de
colmena del perfil (`NTUSER.DAT*`, `UsrClass.dat*` y `ntuser.ini`), los árboles
de sistema configurados y los archivos con atributos `SYSTEM` o `HIDDEN` se
protegen explícitamente frente a mutaciones.

La ejecución predeterminada es una simulación: calcula y registra las acciones
sin modificar archivos. Para aplicar el plan explícitamente:

```powershell
Neocortex --root C:\Corpus\Entrada --apply
```

La política predeterminada `--dedup-policy fast` reduce candidatos mediante el
mismo tamaño y XXH3-128 completo. El plan vuelve a validar identidad, tamaño,
`mtime`, `birthtime` y, para duplicados, contenido byte por byte antes de llegar
a una frontera de mutación. `--dedup-policy exact` realiza además la comparación
durante la planeación.

En `0.8.0` la aplicación de Papelera se abstiene siempre: el backend disponible
opera por ruta y no puede ligar el efecto a la identidad autorizada. El dry-run
conserva candidatos de duplicados, vacíos, directorios vacíos y PDF
irrecuperables, pero `--apply` los termina como `skipped` con evidencia; no se
invoca `Send2Trash` y la dependencia fue retirada.

Los rename de extensión y movimientos documentales sólo se ejecutan sobre un
archivo regular con un único hard link, en NTFS local y el mismo volumen. La
implementación mantiene handles del archivo y del directorio destino, verifica
FileId/volumen y usa rename relativo *no-replace*. UNC, otros filesystems,
reparses, directorios, hard links múltiples y cross-volume provocan abstención;
no existe fallback permisivo por ruta.

Framework v19 registra `started`, cruza a `applying` con identidad esperada justo
antes de la llamada nativa y sólo confirma `applied` con recibo posterior. Un
fallo después de esa frontera queda `recovery_required`; cada transición añade
un evento append-only y nunca se reintenta automáticamente. Al reiniciar, una
acción abandonada en `started` termina `failed` porque no alcanzó la frontera ni
intentó el efecto; una abandonada en `applying` conserva
`recovery_required`. El status de conciliación es de sólo lectura; un registro
durable requiere actor y confirmación separados:

```powershell
Neocortex --action-recovery-status --action-recovery-limit 100
Neocortex --action-recovery-status --action-recovery-json
Neocortex --action-recovery-record 42 --action-recovery-actor "Victor" --confirm-reconciliation-record --action-recovery-json
```

Clasifica `confirmed`, `not_performed`, `ambiguous` o `impossible_to_check` sin
repetir el efecto. `record` agrega una observación inmutable con CAS, actor,
procedencia y firma; nunca autoriza una mutación. Para trash legacy, un recibo
sólo confirma si sus rutas coinciden con la misma acción y el origen está
ausente; `--apply` de Papelera continúa absteniéndose. El lector rechaza un
schema framework posterior al soportado. Sólo existen `status` y `record`:
`decide`, `authorize`, `recover` y `verify` siguen deliberadamente pendientes.
`--status` muestra además el conteo incierto.

La retención disponible es sólo un plan diagnóstico, acotado y paginado por
keyset. Protege las publicaciones vigente y anterior, builders/leases vivos,
cadenas base y evidencia humana o de acciones inciertas; no elimina ni aplica
cuotas:

```powershell
Neocortex --retention-status
Neocortex --retention-status --retention-store catalog --retention-min-age-days 30 --retention-json
```

Las guías canónicas de Knowledge, operación, persistencia, recuperación y
seguridad están en [`docs/KNOWLEDGE.md`](../docs/KNOWLEDGE.md),
[`docs/OPERATIONS.md`](../docs/OPERATIONS.md),
[`docs/PERSISTENCE.md`](../docs/PERSISTENCE.md),
[`docs/RECOVERY.md`](../docs/RECOVERY.md) y
[`docs/SECURITY.md`](../docs/SECURITY.md).

La detección de directorios y archivos vacíos sigue siendo acotada y conserva
sus candidatos, pero no muta esas rutas en `0.8.0`. El plan de archivos no
vacíos se persiste en SQLite y se consume como flujo; en memoria sólo se
conservan los grupos solicitados mediante `--show-groups` y el grupo en proceso.

La ruta PDF ya está integrada como alternativa explícita después de la etapa
común:

```powershell
Neocortex --root C:\Corpus\Entrada --route pdf
```

La contraparte DOCX usa el mismo inventario vivo y se ejecuta como una ruta
explícita, incremental y no destructiva:

```powershell
Neocortex --root C:\Corpus\Entrada --route docx
```

Extrae el cuerpo y las partes útiles del paquete OOXML (encabezados, pies,
notas, comentarios y metadatos), conserva el texto comprimido, construye un
índice FTS5 y reutiliza la caché por identidad NTFS, tamaño, `mtime`,
`birthtime` y firma de procesamiento. La lectura impone límites al número de
miembros ZIP, a cada XML,
al tamaño expandido total y al texto resultante; no renderiza páginas ni crea
imágenes auxiliares.

El mismo extractor acepta los tipos principales OOXML de documento, plantilla
y variantes con macros (`DOCX`, `DOTX`, `DOCM`, `DOTM`), incluso cuando la
extensión no refleja el tipo interno. Las filas legacy que rechazaron una
plantilla se reintentan una sola vez sin invalidar toda la caché DOCX.

La integridad OOXML se diagnostica por parte. Un encabezado, pie o nota
opcional dañados no descartan un cuerpo legible: el documento queda `partial`
y conserva el texto recuperado. Para miembros DEFLATE dañados, el fallback raw
solo se acepta dentro de los límites del header y directorio central, sin
cifrado ni data descriptor, y únicamente si el XML resultante es válido. La
base registra integridad, modo de recuperación, retryability y diagnósticos por
parte; una corrupción obligatoria no recuperable se marca como
`deletion_candidate`, pero la ruta nunca borra ni mueve el DOCX.

Al actualizar una ruta, la caché elimina un dueño anterior solo si esa identidad
ya no está vigente en el inventario de la ejecución. Si todavía es una identidad
viva —incluidas filas legacy omitidas por límites— se conserva y el conflicto
falla de forma explícita en vez de sobrescribir evidencia.

El perfil de layout registra tamaño/orientación del papel, márgenes, columnas,
estilos y alineaciones dominantes, tablas, imágenes, secciones y firmas
separadas de encabezados y pies. Los perfiles equivalentes se consolidan en
familias consultables. En cada ejecución también cruza los DOCX vivos con los
PDF del mismo inventario: prefiere mismo directorio y nombre base, acepta un
nombre base global solo si es único y registra por separado coincidencias,
ambigüedades y ausencias. Nunca crea ni elimina el PDF.

```powershell
Neocortex --docx-search "transformador AND mantenimiento"
Neocortex --docx-layout-groups 20
Neocortex --docx-missing-pdf 100
```

Los controles opcionales `--docx-max-mb`, `--docx-max-count` y
`--docx-max-text-chars` acotan una corrida. Los documentos vivos omitidos por
esos límites conservan una caché válida; los desaparecidos o modificados se
reconcilian al completar la ruta. El estado reside en `docx.sqlite3` junto al
resto de índices persistentes.

La ruta `office` cubre hojas de cálculo XLSX, presentaciones PPTX y documentos
ODT que el inventario ya identifica por contenido:

```powershell
Neocortex --root C:\Corpus\Entrada --route office
Neocortex --office-search 'transformador AND mantenimiento'
```

Extrae de forma incremental propiedades, nombres de hojas, cadenas compartidas,
celdas XLSX con libro, hoja, referencia A1, tipo y valor —más fórmula y valor
cacheado en campos separados—, diapositivas, notas y contenido ODT. La lectura
XML es streaming, con límites por miembro, expansión ZIP, texto y memoria
coordinada; no abre Excel o PowerPoint ni crea representaciones visibles. El
estado `office.sqlite3` conserva esa evidencia XLSX tipada en `xlsx_cells`,
además de texto comprimido, FTS5, firma XXH3, formato y evidencia de error. Una
estructura corrupta se registra como `deletion_candidate` para revisión, nunca
como una orden automática de borrado. Los límites se controlan
con `--office-max-mb`, `--office-max-count`, `--office-max-text-chars` y los
parámetros `--office-*-memory-*`; un XLSX con más de 250,000 celdas no vacías o
cadenas compartidas se abstiene como revisión manual en vez de publicar una
representación parcial.

La ruta `archive` consume los ZIP detectados por el inventario y publica sus
miembros en `archive.sqlite3`, con FTS5 y procedencia completa. Recorre ZIP
anidados de forma recursiva y representa la ubicación sin ambigüedad:

```text
contenedor.zip!/subcarpeta/otro.zip!/documento.txt
```

```powershell
Neocortex --root C:\Corpus\Entrada --route archive --archive-max-count 25
Neocortex --archive-status
Neocortex --archive-search 'protección AND transformador'
Neocortex --archive-list 50 --archive-container 'contenedor.zip'
```

El productor nunca extrae miembros al filesystem. Valida el directorio central,
nombres portables, duplicados, cifrado, tipo de miembro, compresión, tamaños y
presupuestos acumulados antes de leer. Los límites predeterminados son cinco
niveles, 20 000 miembros visibles, 64 MiB por miembro, 512 MiB expandidos por
contenedor y ratio 200. Texto/código, HTML/XML, DOCX/XLSX/PPTX, ODT/ODS/ODP y
EPUB son consultables. Los PDF conservan texto nativo y, en modo OCR `auto`, las
páginas con poco texto se rasterizan de forma acotada; las imágenes
BMP/GIF/JPEG/PNG/TIFF/WebP también pueden pasar por OCR. Así, un PDF escaneado o
una imagen dentro de un ZIP anidado puede ser buscable sin materializarse. Si
falta Pillow, PyMuPDF, Tesseract o el idioma requerido, el miembro y la
incidencia siguen visibles y no se inventa texto.
`location=archive_member inside_zip=1`, `container`, `member`, `chain` y
`virtual_path` distinguen siempre estos resultados de un archivo físico.

La ruta `text` cubre archivos físicos imprimibles que antes sólo aparecían en
el inventario: TXT, Markdown, CSV/TSV, HTML, XML, JSON y formatos equivalentes.
También interpreta EML y conserva asunto, remitente y encabezados acotados. Los
DOC/XLS/PPT binarios heredados se extraen en un proceso aislado mediante
LibreOffice o los fallbacks disponibles `catdoc`/`xls2csv`/`catppt`; una
extensión por sí sola no convierte bytes arbitrarios en texto:

```powershell
Neocortex --root C:\Corpus\Entrada --route text --text-max-count 25
Neocortex --knowledge-search 'protección AND transformador'
```

`text.sqlite3` conserva texto, tipo, título, autor, firma, errores y FTS5. El
catálogo puede proponer clasificación y nombres, por ejemplo a partir del
asunto de un EML, pero no ejecuta mutaciones. Semantic consume esta caché con
`--semantic-source text`.

La ruta `audio` transcribe de forma incremental el audio detectado por contenido
en PTT/Opus/Ogg, MP3, WAV, FLAC, M4A/MP4 y las pistas de audio de los vídeos
MP4, MOV y AVI. Usa `faster-whisper` dentro de un proceso persistente aislado,
con VAD, límites de duración/texto/segmentos, timeout por archivo, cancelación y
admisión coordinada de memoria. La reserva permanece activa mientras el modelo
reside en el proceso y se libera después de cerrar ese proceso, en vez de
liberarse y volver a solicitarse entre cada audio:

```powershell
Neocortex --root C:\Corpus\Entrada --route audio
Neocortex --audio-search 'transformador AND mantenimiento'
Neocortex --audio-doctor
```

El modelo predeterminado es `small`; en un equipo sin CUDA se resuelve a
CPU/int8. La primera transcripción puede descargar sus pesos al caché del modelo;
`--audio-local-models-only` prohíbe esa descarga y exige que el modelo ya exista.
`--audio-doctor` sólo comprueba `faster-whisper`, CTranslate2 y FFprobe: no carga
ni descarga pesos. Se puede elegir modelo, dispositivo, tipo de cómputo e idioma
con `--whisper-model`, `--whisper-device`, `--whisper-compute-type` y
`--audio-language`.

`audio.sqlite3` conserva el texto comprimido, FTS5, segmentos con tiempos,
idioma, duración, modelo/backend, firma de procesamiento y XXH3. Un contenedor
estructuralmente inválido se registra como candidato de revisión para eliminación;
el framework no lo borra. Un audio válido sin voz se conserva como `no_speech`.
Los límites se controlan con `--audio-max-mb`, `--audio-max-count`,
`--audio-max-duration-seconds`, `--audio-max-transcript-chars`,
`--audio-max-segments`, `--audio-file-timeout` y `--audio-*-memory-*`.

La ruta `video` es un productor visual separado. FFprobe valida contenedor y
streams; FFmpeg selecciona escenas, keyframes y muestras periódicas sin
materializar un video derivado. `video.sqlite3` conserva cada frame con
timestamp, ordinal, razones de selección, dimensiones, XXH3, OCR y procedencia,
además del enlace exacto a la transcripción Audio publicada cuando existe:

```powershell
Neocortex --root C:\Corpus\Entrada --route video --video-max-count 25
Neocortex --video-status
Neocortex --video-search "placa del transformador" --video-search-limit 20
Neocortex --video-doctor --video-ocr-profile auto-multilingual
```

Un video visual-only termina correctamente y Audio publica `no_audio` benigno
sin cargar Whisper. Los límites predeterminados incluyen 48 frames, 40 MP de
OCR total, 16 KiB de OCR por frame, 512 MiB de scratch y 2 GiB de memoria
virtual del worker. Los perfiles OCR compartidos conservan `configured`
(`spa+eng`) como default y ofrecen `latin`, `han-simplified`,
`han-traditional` y `auto-multilingual`; perfil, idiomas efectivos, OSD,
confianza, fallback y huellas de traineddata forman parte de la procedencia.

## Inteligencia estructurada de código fuente

La ruta `code` consume los `FileSnapshot` del inventario/deduplicador común; no
recorre otro árbol ni modifica los archivos. Revalida tipo regular, ausencia de
enlaces o puntos de reparse, identidad física y metadatos antes y después de
cada lectura acotada. Una versión y todos sus símbolos, referencias,
dependencias, diagnósticos, métricas y chunks FTS se publican en una sola
transacción en `code.sqlite3`:

```powershell
Neocortex --root C:\Codigo --route code
Neocortex --root C:\Codigo --route code --code-cache-validation full
Neocortex --code-status
Neocortex --code-review
Neocortex --state-directory C:\Estado\Actual --code-publication-diff C:\Estado\Baseline
Neocortex --code-doctor
```

Sobre un autoanálisis publicado, `--code-review` devuelve un top 10
determinista de hotspots Python confirmados (`high_complexity` y/o
`long_function`), como máximo dos por archivo. El score entero en basis points
combina severidad relativa a los umbrales e impacto por callers estáticos
resueltos; es prioridad de revisión, no riesgo ni confianza calibrada. La
consulta conserva archivos y SQLite byte por byte y suprime deliberadamente
`probable_dead_symbol` mientras la resolución de llamadas siga incompleta.

`--code-publication-diff` compara dos publicaciones completadas sin crear,
migrar ni modificar sus bases. La identidad portable de una call común usa
ruta relativa, rango de bytes y nombre; la de un hotspot usa ruta relativa y
qualified name. El resultado separa altas, correcciones, pérdidas y cambios de
evidencia, conserva límites duros y trata `probable_dead` sólo como conteo no
calibrado.

La transacción global se conserva deliberadamente en el esquema 4. Pruebas con
lectores concurrentes y fault injection confirmaron snapshot precommit y
rollback completo; no debe fragmentarse hasta definir generación, membresía,
head/CAS, reanudación, migración y poda como un único contrato.

La clasificación separa código, scripts, configuración, manifests, locks,
datos, plantillas, documentación, fixtures, ejemplos, generado, vendorizado y
texto plano. Combina extensión, nombre, ubicación, shebang y contenido y
conserva evidencia y confianza. Python usa el AST estándar y registra rangos,
imports, clases, funciones, métodos, decoradores, parámetros, tipos, docstrings,
llamadas y excepciones; un error sintáctico conserva texto y diagnóstico. Rust
usa por ahora un analizador léxico estructural extensible; sus inferencias no se
marcan como confirmadas por compilador. El registro perezoso permite añadir
analizadores nativos, Tree-sitter, LSP o herramientas externas sin volverlos
dependencias obligatorias. Si un analizador no está disponible, la degradación
mantiene texto y FTS.

El analizador Python `neocortex-python-ast` versión 3 conserva módulo y nivel de
imports relativos. También interpreta asignaciones como enlaces, no como
nombres arbitrarios de expresiones: sólo emite símbolos para `Name`,
destructuring `Tuple`/`List` y `Starred`, omite atributos y subscripts y
deduplica un mismo nombre dentro de la asignación.

Un cache hit con la misma ruta actualiza `last_seen_run_id` y
`last_observed_run_id` con cero DML sobre `code_fts`. Si la misma identidad
física aparece bajo otra ruta, la caché se rechaza sin escribir: el productor
normal publica una versión sucesora, conserva el `path_observed` histórico y
reconstruye raíces, membresías de proyecto y FTS desde la ruta nueva. La
reutilización también comprueba el analizador que seleccionaría el runtime
actual; si un analizador opcional pasa a estar disponible, reemplaza de forma
incremental el fallback.

Una corrida completa, sin selección ni límite, ejecuta `mark_missing` antes del
grafo. Cuando hay cambios, `finalize_graph` borra y reconstruye membresías
derivadas, resoluciones y diagnósticos reconstruibles sólo para versiones
vigentes; al final sincroniza las etiquetas FTS distintas con un mapa temporal
indexado y un único scan, sin tocar labels históricos. Manifests e historia no
se convierten en staging mutable.

El resolver v4 materializa símbolos y dependencias vigentes en conjuntos
temporales indexados. Prioriza llamadas demostrables dentro del mismo módulo o
clase y módulos relativos por ruta léxica exacta; después resuelve por nombre
cualificado o simple sólo cuando la coincidencia global es única. Los empates y
ausencias permanecen ambiguos o no resueltos; no se fabrican relaciones para
forzar conectividad.

El grafo sólo se omite cuando no hubo cambios ni invalidaciones, todos los
candidatos fueron cache hits compatibles con el runtime y el run inmediatamente
anterior está `completed`, conserva la misma firma/summary y coincide con el
fence tipado `code-graph-resolver-v4`. Ese fence avanza atómicamente con la
finalización del `analysis_run`. Un fence ausente, malformado o stale, un run
intermedio o la primera corrida sobre una base existente sin fence fuerzan
reconstrucción completa. Los cache hits retienen los contadores `partial`,
`text_only`, `binary`, `skipped_limit` y `error` del resultado vigente.

El esquema continúa no generacional en versión 4: la cancelación sólo tiene
checkpoints alrededor de la transacción global, no dentro de una sentencia
SQLite; los empates se conservan ambiguos y la firma global del registro puede
invalidar lenguajes cuyo analizador no cambió.

Después de publicar el run, la ruta hace checkpoint del WAL y retira `-wal` y
`-shm` únicamente cuando el WAL está vacío y los auxiliares son reconstruibles.
Un lector externo puede impedir esa limpieza sin revertir el run ya completado;
en tal caso el diagnóstico quiescente se abstiene hasta que los handles se
liberen y una corrida posterior complete la limpieza.
Las búsquedas y listados operativos no dependen de esa limpieza: ante una base
sin sidecars abren una instantánea immutable con cercas de archivo antes y
después, y ante un writer activo usan SQLite read-only sin borrar, checkpoint o
crear auxiliares de forma oportunista.

La ruta Code integrada de `Neocortex --all` no autodetecta proyectos por
`pyproject.toml`, `package.json` u otros marcadores distribuidos en el perfil.
Su allowlist predeterminada contiene exclusivamente
`$HOME\Neocortex\Repository` y
`$HOME\Frameworks\Generador de bitácoras EPS`; laboratorios,
dependencias, generados y caches dentro de esas raíces también se excluyen antes
de leer contenido. `--code-project-root RUTA` reemplaza la allowlist y puede
repetirse. `--code-scope broad` conserva la selección histórica amplia sólo como
override deliberado.

Las consultas exactas, textuales y estructurales se pueden ejecutar por separado
o fusionar mediante reciprocal rank fusion. Cada resultado conserva archivo,
proyecto probable, símbolo, líneas, tipo de coincidencia, evidencia, versión y
estado observado:

```powershell
Neocortex --code-search "validate_sqlite_access" --code-search-mode definition
Neocortex --code-search "sqlite3" --code-search-mode import --code-language python
Neocortex --code-search "dónde se valida el acceso a SQLite" --code-search-mode hybrid --code-json
Neocortex --code-search "python_parse_error" --code-search-mode diagnostic
```

La agrupación de proyectos usa manifests, raíces, paquetes, imports y relaciones
para conservar instancias y familias probables. La reconstrucción es únicamente
conceptual: devuelve un manifiesto con origen, versión, huellas y criterio, pero
no crea, mueve, combina ni sobrescribe archivos:

```powershell
Neocortex --code-projects --code-json
Neocortex --code-reconstruct PROYECTO_O_ID --code-reconstruct-strategy latest --code-json
Neocortex --code-reconstruct PROYECTO_O_ID --code-reconstruct-strategy coherent --code-json
Neocortex --code-reconstruct PROYECTO_O_ID --code-reconstruct-strategy branches --code-json
```

Los embeddings de código son opcionales y complementarios. Primero se indexan
los chunks durables en el espacio textual existente; una consulta `semantic` o
`hybrid` sólo aporta esa señal si hay una generación semántica disponible:

```powershell
Neocortex --semantic-index text --semantic-source code
Neocortex --code-search "dónde se valida el acceso a SQLite" --code-search-mode semantic
```

## Capa semántica multimodal

La capa semántica complementa los índices y clasificadores deterministas; no
los sustituye ni mezcla espacios vectoriales incompatibles. Consume de forma
incremental las bases durables que ya producen las rutas PDF, DOCX, Office
(XLSX/PPTX/ODT), audio, Archive, texto físico y código, sin volver a abrir esos
archivos. Para imágenes consume `image.sqlite3`, verifica el snapshot del
archivo original y reutiliza la huella completa que la ruta de imagen garantiza
en el deduplicador; también puede
incorporar como texto independiente el OCR acotado y verificado que conserva la
ruta de imagen. El resultado vive en `semantic.sqlite3` y no crea
representaciones visibles ni modifica los archivos de origen.

Cada elemento conserva una identidad durable independiente de la ruta mutable,
la revisión exacta de su fuente, firma de procesamiento y huellas XXH3 no
criptográficas con longitud y guardas de colisión. El texto se divide en
ventanas naturales acotadas, con solapamiento y configuración propia por
modelo. El guard `exact-token-guard-v2` ajusta cada ventana con el tokenizador
real antes de persistir jobs; el backend vuelve a contar y rechaza cualquier
entrada que todavía implicara truncamiento silencioso. Límite y revisión del
tokenizador forman parte de la identidad durable del perfil.
Los vectores normalizados L2 se almacenan en `float16` junto con modelo,
versión, rol, dimensiones, procedencia y firma completa.

Los espacios vigentes son deliberadamente separados:

- `jinaai/jina-embeddings-v2-base-es`, 768 dimensiones, es el perfil de texto
  `quality` predeterminado para español e inglés;
- `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, 384
  dimensiones, es el perfil opcional `compact` y nunca se compara directamente
  con Jina;
- `Qdrant/clip-ViT-B-32-text` y `Qdrant/clip-ViT-B-32-vision`, 512
  dimensiones, comparten exclusivamente el espacio CLIP para consultas de
  texto contra imágenes.

El esquema versiona espacios, contratos de modelos, elementos, chunks,
generaciones, trabajos, leases, cargas vectoriales, prototipos y evidencia. Una
generación interrumpida es reanudable; los lotes y leases son acotados, el
worker renueva en una transacción sus leases mientras una inferencia síncrona
sigue activa y detiene ese heartbeat al terminar, y una
huella XXH3 idéntica bajo la misma firma de modelo permite reutilizar inferencia
ya confirmada. Cada nueva ejecución vuelve a enumerar las cachés seleccionadas
para reconciliar su estado, pero no recalcula embeddings sin cambios. Un replay
exacto no crea jobs ni consume `max_items` o `max_new_jobs`; la reexploración y
la reconciliación de la fuente siguen siendo O(n), pero el replay sin cambios
reutiliza directamente el head publicado y no vuelve a clonarlo. Una generación
con altas, bajas o cambios todavía materializa su base en O(n), ahora por páginas
con cursor durable y el mismo deadline de la invocación: una interrupción
reanuda el prefijo fijado en vez de repetir el clon completo.

El staging textual confirma como máximo 128 items o chunks por transacción.
Error, cancelación o deadline revierten el lote activo; el prefijo confirmado
queda idempotente y reanudable en la generación `building`. Sólo una enumeración
`bounded-v1` completa permite desactivar miembros no observados y publicar. Al
cambiar el perfil de chunking, el nuevo head sustituye ese perfil únicamente en
las fuentes seleccionadas y conserva las demás; las revisiones históricas siguen
reconstruibles fuera del head.

Antes de cada claim, el worker agota payloads reutilizables por coincidencia
exacta de modelo y huella reforzada. Un payload creado al completar el batch N
puede cerrar duplicados todavía pendientes antes del batch N+1, sin invocar de
nuevo el backend. `RuntimeError`, `KeyboardInterrupt` y cualquier
`BaseException` liberan los leases aún propios a retry/error durable y preservan
la causa original; no publican una generación incompleta. Los duplicados que ya
entraron juntos en un batch todavía pueden inferirse más de una vez y las
transiciones de resultado permanecen N+1 por job.

Semantic v6 separa construcción y publicación. Cada modelo tiene un
`published_embedding_heads`; una generación `building` clona por lotes los
miembros de una base fijada —ID, high-watermark y conteo— y persiste el cursor
del último miembro confirmado. Los nuevos resultados se ligan a revisiones
inmutables. Sólo una finalización completa y una base todavía idéntica cambia el head por CAS en la misma
transacción. `ready_partial`, fallos y cancelación conservan la generación
anterior, y las búsquedas oficiales fijan los heads del snapshot antes de
resolver hits. El contenido sigue congelado, mientras el localizador de ruta se
toma del elemento actual sólo si `item_id`, tipo e identidad de fuente coinciden;
una reasignación se rechaza. Espacio y modalidad se validan contra el modelo
persistido, no contra la afirmación del hit. SQL externo sobre tablas legacy no recibe esta garantía. No hay
todavía poda global para builds fallidos, parciales o abandonados.

La búsqueda mantiene rankings independientes para contenido textual Jina o
MiniLM, título textual, CLIP y los índices FTS5 de PDF/DOCX/Office/audio/
Archive/texto. Antes de indexar, un filtro conservador rechaza Base64 o binario
de alta confianza, volcados de fórmulas, mojibake, tokens desmesurados y texto
de máquina muy repetitivo; además colapsa chunks exactamente duplicados dentro
de un elemento sin alterar la caché fuente. El título usa primero un título de
origen fiable —por ejemplo el asunto EML—, después un encabezado acotado cuando
el basename es genérico y, como fallback, el basename sin directorios ni
extensión final. Se publica como sección durable `semantic_metadata_title` bajo
la política `semantic-content-aware-title-v3`. Es una señal mutable, advisory y
separada del cuerpo: no se usa para clasificación ni como evidencia
materializada. Un head legado sin títulos declara `title_channel_not_indexed`,
sin ocultar el ranking corporal disponible.

La fusión usa reciprocal rank fusion (RRF): combina posiciones y conserva la
contribución de cada ranking, sin tratar sus puntuaciones crudas como si
estuvieran calibradas entre sí. En texto, el cuerpo aporta peso `1.0` y el
título `0.5`; ambos reutilizan una sola vectorización de la consulta y el hit
fusionado prefiere el snippet corporal. El backend vectorial actual es una
búsqueda exacta con un límite explícito de vectores; informa cuando el recorrido
queda incompleto. El recorrido agrupa páginas float16/float32 con NumPy,
conserva scores, empates e identidad exactos y vuelve al camino escalar si
NumPy no está disponible. No existe todavía un índice ANN.

El contrato mixto vigente de Jina aplica un piso uniforme `0.42` al cuerpo y al
título de Archive, audio, Code, DOCX, imagen/OCR, ODT, PDF, PPTX, texto y XLSX.
Un vecino inferior se informa como `abstained`, no como hit. El piso sólo
filtra evidencia débil dentro de esa firma exacta: no es una probabilidad, no
se extrapola a otros modelos o backends y no autoriza clasificación ni
mutación. La reutilización exacta conserva el contrato dentro de
`payload_provenance`; un conflicto entre ese payload y el miembro evita
aplicarlo.

La recuperación lexical conserva AND estricto como primera estrategia. Sólo
ante cero hits elimina stopwords ES/EN/DE y aplica un fallback acotado; para
consultas Han de al menos dos caracteres puede recorrer, después de fallar FTS,
un máximo de 50 000 filas por fuente mediante substring exacto y procedencia
`sqlite_bounded_cjk_substring`.

CLIP no hereda el piso textual. Sin una calibración positiva/negativa ligada a
modelo, pipeline y backend, la búsqueda visual no carga el backend, devuelve
cero vecinos y explica `image_retrieval_not_calibrated`. La medición humana de
esta entrega mostró solapamiento: el corte conservador contra el estado vivo
retendría sólo 32 % de positivos, por lo que no se inventó un umbral. MiniLM
permanece como candidato shadow después de mejorar calidad agregada y latencia
en un fixture pequeño ES/EN/DE/ZH, pero Jina sigue siendo el head productivo y
los espacios nunca se mezclan.

La clasificación compara embeddings activos con prototipos versionados de la
ontología industrial compartida y materializa evidencia trazable por elemento,
concepto, familia, modelo, generación y puntuación. Se abstiene cuando no hay una
similitud finita positiva. Esta evidencia es **advisory** y **uncalibrated**: no
demuestra exactitud, no reemplaza las reglas especializadas y nunca autoriza
borrados, movimientos ni renombres. Las decisiones humanas de revisión quedan
registradas como evidencia append-only, pero aún no calibran automáticamente
estas puntuaciones.

La dependencia de inferencia se empaqueta en el extra `semantic`, pero el
runtime personal canónico se instala con `full`. Los extras individuales son
una herramienta de desarrollo, no perfiles que Victor deba administrar.
Ningún comando de indexación, búsqueda semántica o clasificación descarga
pesos: los modelos sólo pueden adquirirse mediante preparación explícita.

Este entorno editable sirve únicamente para desarrollo:

```powershell
$Venv = Join-Path $PWD '.venv'
py -3 -m venv $Venv
& "$Venv\Scripts\python.exe" -m pip install -c constraints.txt -e ".[full,dev]"
& "$Venv\Scripts\Neocortex.exe" --semantic-status
& "$Venv\Scripts\Neocortex.exe" --semantic-prepare-models
& "$Venv\Scripts\Neocortex.exe" --semantic-prepare-models --semantic-include-compact
```

`--semantic-status` es de solo lectura y no crea ni migra una base ausente. La
preparación carga Jina y ambos encoders CLIP; el último ejemplo incluye además
MiniLM.

`--semantic-index` usa límites seguros predeterminados:

- `--semantic-max-items 50`;
- `--semantic-max-new-jobs 1500`;
- `--semantic-time-budget-seconds 900`.

El presupuesto es único para texto, imagen y OCR en una ejecución `all`. Un
límite agotado deja la generación sin publicar, informa la causa y devuelve
código `2`. Empiece con un estado aislado de 20–50 elementos; un plan o staging
correcto sin embeddings publicados y consultables no es una entrega Semantic.

Sin `--semantic-source`, la ruta de texto selecciona solo las bases durables que
ya existen. La opción se repite para acotar las fuentes y acepta `pdf`, `docx`,
`xlsx`, `pptx`, `odt`, `audio`, `archive`, `text` y `code`. La ruta de imagen
siempre genera evidencia visual CLIP; por defecto añade el OCR retenido en su
espacio textual y `--semantic-no-ocr` lo excluye reconciliando también el
estado previo.

Las consultas admiten los modos independientes `all`, `text`, `image` y
`lexical`; `all` fusiona los rankings disponibles. El límite de resultados es
de 1 a 1 000. `--semantic-max-vectors` acota el recorrido exacto (500 000 por
defecto, hasta 10 000 000) y la salida indica si fue completo:

```powershell
Neocortex --semantic-search "mantenimiento de transformadores de potencia"
Neocortex --semantic-search "protección diferencial" --semantic-search-mode text --semantic-search-limit 50
Neocortex --semantic-search "tablero de control" --semantic-search-mode image --semantic-max-vectors 200000
Neocortex --semantic-search "transformador mantenimiento" --semantic-search-mode lexical
Neocortex --semantic-search "puesta en servicio" --semantic-search-mode text --semantic-text-profile compact
```

La materialización y consulta de evidencia también son explícitas. La
clasificación se ejecuta sólo después del piloto acotado y sobre una generación
publicada; no use `--semantic-classify all` como smoke. El
`ITEM_ID` exacto se obtiene de `SEMANTIC_HIT item=...`; el límite evita una
salida no acotada y el comando informa si la lista fue truncada:

```powershell
Neocortex --semantic-evidence "item:pdf:IDENTIDAD" --semantic-evidence-limit 100
```

Las rutas individuales no disparan esos comandos y no hay watcher semántico
automático. `Neocortex --all` sí ejecuta al final su indexación semántica
integrada sobre las cachés publicadas. Después de incorporar contenido mediante
una ruta aislada se debe volver a ejecutar `--semantic-index` con límites
explícitos y, si se desea ontología actualizada, la clasificación
correspondiente.

## Knowledge Plane de solo lectura

La versión `0.8.0` conserva una fachada coherente de consulta sobre los
propietarios durables ya existentes: inventario, FTS de PDF/DOCX/Office/audio,
Archive y texto físico cuando sus bases existen, catálogo técnico, índice
estructural de código y, cuando está publicado, evidencia semántica. Knowledge
no descubre ni reprocesa el corpus, no crea o migra bases y no autoriza
borrados, movimientos ni renombres.

Cada operación fija un snapshot lógico cross-owner, incluidos los heads
generacionales disponibles, observa los propietarios antes y después de la
consulta y reintenta una vez si cambian. Como SQLite no ofrece una transacción
distribuida entre esas bases, una segunda deriva, un propietario ausente o una
capacidad no soportada se declara de forma visible como resultado parcial; no
se rellena con evidencia obsoleta ni se fuerza una respuesta.

```powershell
Neocortex --knowledge-status
Neocortex --knowledge-search "IEC 61850 protección diferencial"
Neocortex --knowledge-context "mantenimiento de transformadores" --knowledge-limit 12
Neocortex --knowledge-search "validate_sqlite_access" --knowledge-mode discovery --knowledge-json
```

La fachada cotidiana ofrece los mismos lectores mediante scopes fijos, sin
aceptar rutas de estado arbitrarias:

```powershell
Neocortex status --scope all
Neocortex search "mantenimiento de transformadores" --scope personal
Neocortex ask "¿qué evidencia existe de la prueba FAT?" --scope personal
Neocortex inspect code "validación de SQLite" --scope framework
Neocortex review value --scope personal
Neocortex agent serve
```

El último comando expone por MCP/stdio únicamente tools read-only. No abre red,
no registra productores ni mutaciones y trata el corpus como datos no
confiables. Una ventana top-k llena no implica truncamiento:
`result_window_full`/`window_omitted_candidates` informan omisiones normales;
sólo un corte duro marca `truncated` y propaga `next_cursor`/`cutoff_score`. El
contexto reserva primero una cita K1 utilizable antes de diagnósticos.

El modo predeterminado `evidence` conserva evidencias distintas de una misma
fuente; `discovery` reduce esas repeticiones cuando el propietario lo permite.
`--knowledge-history` incluye revisiones históricas o supersedidas que se
excluyen por defecto. La fusión conserva contribuciones y localizadores reales,
y `--knowledge-context` produce citas deterministas y acotadas sin inventar
páginas, tiempos, líneas o regiones que el propietario no haya persistido.

El contexto renderizado antepone la frontera versionada
`untrusted-corpus-data-v1`, que clasifica toda evidencia recuperada como no
confiable y declara que no posee autoridad de instrucciones, herramientas ni
acciones. Ese marcador precede a la consulta, metadatos, citas y relaciones.

Los códigos estables distinguen éxito (`0`), fallo fatal (`1`), uso inválido
(`2`), ausencia de resultados (`3`), resultado parcial (`4`), cambio repetido
del snapshot (`5`), esquema futuro/incompatible (`6`), corrupción (`7`) y
cancelación (`130`). El contrato, las garantías y los límites se detallan en
[`docs/KNOWLEDGE.md`](../docs/KNOWLEDGE.md).

## Catálogo técnico y organización documental

Después de una ruta PDF, DOCX, Office, texto o audio, el framework clasifica
incrementalmente el texto y los metadatos ya extraídos; no vuelve a abrir los
documentos originales.
`document_catalog.sqlite3` conserva la clasificación vigente y su historial,
con versión, puntuación, incertidumbre y evidencia. El muestreo textual está
acotado a un prefijo de 64 000 caracteres por documento y una clasificación sin cambios se
reutiliza por identidad, metadatos, firma de extracción y XXH3 del texto.
Las expresiones regulares se compilan en una caché acotada y cada familia de
reglas usa un prefiltro combinado antes de evaluar evidencia individual. La
ruta de un documento ya organizado aporta únicamente su nombre de archivo: las
carpetas creadas por una clasificación anterior no pueden reforzarse a sí
mismas ni impedir una corrección posterior.

Catálogo v6 construye una generación aislada por `source_kind`. Los UPSERT por
lote permanecen en `catalog_generation_documents`; al completar, una transacción
CAS reemplaza la proyección `documents`, agrega historial, reconcilia planes y
cambia `catalog_publications`. Un fallo, cancelación o publicación competidora
mantiene visible el head anterior. Generaciones fallidas, `superseded` o
abandonadas se preservan y aún carecen de retención global.

La taxonomía incluida reconoce referencias IEEE, IEC, ISO, ISO/IEC, IEC/IEEE,
NMX, NOM, NRF, CFE, ANSI, NFPA, NEMA, ASTM, UL, LAPEM, CIGRE, CSA, EN, BS, DIN,
ASME, API, OSHA, NETA, NERC, EPRI, ISA, ACI, AISC, AWS, ASCE, SSPC, NEC y
especificaciones PEMEX. Distingue, entre otros, normativa, procedimientos,
cursos, manuales, formatos, planos/diagramas, memorias de cálculo,
especificaciones, protocolos y bitácoras, laboratorio/DGA, FAT/SAT,
inspecciones, auditorías, dossiers de calidad, registros fotográficos,
reportes de actividades, anomalías y resultados de pruebas, mediciones de
campo, análisis, no conformidades, acciones correctivas/preventivas,
referencias técnicas, certificados e informes de calibración, hojas de datos
de seguridad, controles metrológicos, minutas y reportes FAT/SAT,
compras, contratos, facturas, licitaciones y correspondencia. También separa
los registros controlados de auditores, entrega de EPP, incidencias y
visitantes, los programas de seguridad/salud y gestión ambiental, manuales del
sistema de gestión, descripciones técnicas de sistemas, hojas de asignación de
proyecto, comprobantes de viaje e informes diarios de campo. Los reportes
generados de inventario de archivos y las instrucciones con datos bancarios se
identifican, pero permanecen en revisión en vez de moverse automáticamente. Un
certificado de calibración exige
estructura corroborada —encabezado, instrumento y señales de trazabilidad,
identidad o resultados—; una mención aislada, un anexo extenso o una bitácora de
equipos no bastan. Del mismo modo, citar una norma no convierte un
procedimiento, reporte, catálogo o plan en la norma misma; los campos de
contrato o licitación usados como encabezado de proyecto tampoco reemplazan el
tipo real del informe. Las organizaciones incluyen CFE, PEMEX, INEEL, ANDRITZ,
SERINTRA, Ingeniería Analítica, ARBEIT, CYMI, Saavi Energía, CHINT, Vitro, CLAM
y fabricantes habituales del sector. Se preservan alternativas, evidencia y
puntuaciones; los expedientes personales siempre requieren revisión.

La autoridad emisora se determina con la portada, el título y la estructura
formal del documento; las normas citadas se conservan como referencias, pero
no desplazan al emisor. Así, una especificación CFE que referencia NMX e IEC
sigue siendo CFE, y una IEC ubicada previamente bajo `Normativa\NMX` puede
reclasificarse sin que esa carpeta refuerce el error. Las designaciones de
portada ASTM D877/D1816 prevalecen sobre guías IEEE citadas y `SOM 3531`,
`SOM-3531` o variantes equivalentes se normalizan como `SOM-3531` sin convertir
el procedimiento CFE en una norma. En nombres sugeridos de documentos
normativos se antepone el identificador de la autoridad primaria: una adopción
NMX que cita IEC no recibe un nombre que comience por IEC. La palabra española
`en` seguida de una cantidad o año tampoco se interpreta como norma europea.
Las palabras CFE o LAPEM por sí solas no se inventan como identificadores.
Además del tipo principal,
el catálogo conserva subtipos —norma, guía, método de prueba, práctica
recomendada, código, especificación, regulación obligatoria y clases de manual
o procedimiento—, equipos concretos y actividades operativas. El vocabulario
de equipos incluye transformadores de potencia y distribución,
autotransformadores, reactores, boquillas, cambiadores de derivaciones,
interruptores, cuchillas, TC, TP, apartarrayos, bancos de capacitores, GIS,
switchgear, cables, sistemas de tierra, barras, aisladores, aceite, SF6,
baterías y protección/control. Las actividades distinguen diseño,
construcción, FAT, pruebas de campo, recepción, puesta a punto, puesta en
servicio, mantenimiento, diagnóstico, monitoreo, laboratorio, reparación,
seguridad y protección ambiental.

El catálogo separa empresa emisora/fabricante de cliente, proyecto y frente de
trabajo. La taxonomía incorporada relaciona Malpaso con el cliente ANDRITZ y
reconoce análisis/inspecciones de presión de unidades, paquetes y embarques HCN,
listas de empaque, reportes de entrega, etiquetas de recipientes para muestreo
de aceite, exportaciones de resultados de pruebas, control de tiempo y recursos,
y el contexto de modernización/repotenciación. También separa informes de
laboratorio, constancias LAPEM, certificados SAT, anexos técnicos contestados y
reportes de trazabilidad de las normas que únicamente citan. Conserva estos
campos, sus puntuaciones, evidencia y alternativas para que puedan consultarse
y organizarse sin depender solo del nombre del archivo.

Cuando ANDRITZ es la empresa emisora pero no existe evidencia suficiente para
afirmar que también es el cliente, el catálogo conserva separados ambos roles
y organiza el documento en `Clientes\ANDRITZ\General`. Las normas auténticas
siguen bajo `Normativa`, incluso cuando pertenecen a un expediente de ANDRITZ.

Una taxonomía local puede añadir autoridades, expresiones de identificador,
empresas, clientes y proyectos sin sustituir los valores sectoriales
incorporados:

```toml
[[authorities]]
code = "AUTORIDAD"
aliases = ["Nombre completo"]
identifier_patterns = ["\\bAUT-[0-9]{3,5}(?:-[0-9]{4})?\\b"]

[[organizations]]
name = "EMPRESA"
aliases = ["EMPRESA", "MARCA COMERCIAL"]

[[clients]]
name = "CLIENTE"
aliases = ["CLIENTE", "RAZON SOCIAL"]

[[projects]]
name = "Proyecto Delta"
client = "CLIENTE"
aliases = ["PRJ-DELTA-77", "CENTRAL DELTA"]
```

```powershell
Neocortex --catalog-documents
Neocortex --catalog-preview 100 --catalog-authority IEEE
Neocortex --catalog-preview 100 --catalog-organization ANDRITZ
Neocortex --catalog-preview 100 --catalog-client ANDRITZ --catalog-project Malpaso
Neocortex --catalog-preview 100 --catalog-workstream embarques_hcn
Neocortex --catalog-documents --document-taxonomy C:\Configuracion\taxonomia.toml
```

La clasificación es automática tras PDF/DOCX/Office/texto/audio;
`--no-document-catalog` la desactiva explícitamente. Cada formato expone una
barra propia con documentos clasificados, reutilizados desde caché, enviados a
revisión, errores y pendientes. Los filtros de catálogo son consultas de solo
lectura.

El flujo normal de organización es no destructivo y revisable:

```powershell
Neocortex --catalog-preview 20
Neocortex --organization-plan
Neocortex --organization-preview 20 --organization-preview-status planned
```

Revise evidencia, clasificaciones y destinos. Sólo una autorización explícita
posterior permite aplicar un lote pequeño:

```powershell
Neocortex --organization-apply --organization-max-actions 20
```

`Neocortex --all --apply` no se usa como smoke ni como primer piloto. Una vez
validado el entorno es la interfaz cotidiana simplificada prevista: procesa las
rutas integradas y aplica únicamente movimientos que superen las protecciones
del framework. Después de aplicar, verifique el resultado; las capacidades aún
no integradas deben añadirse a este flujo, no trasladarse a una secuencia manual
para Victor.

Sin `--organization-root`, ambas operaciones usan
`<raíz_analizada>\Consulta_Tecnica_Organizada`: `--root` explícito tiene
precedencia y, si se omite, se recupera la raíz de la última ejecución inicial
completada. `--organization-root D:\ConsultaTecnica` permite reemplazar ese
destino.

El plan actualiza primero el catálogo y propone jerarquías como
`Normativa\IEC\Normas\transformadores_potencia`,
`Normativa\NMX\Metodos_de_prueba\transformadores_potencia`,
`Normativa\CFE\Especificaciones\interruptores_potencia`,
`CFE\Procedimientos\Puesta_en_servicio\subestaciones`,
`CFE\Manuales\Mantenimiento\transformadores_potencia`,
`CFE\Cursos_y_capacitacion`,
`Clientes\ANDRITZ\General\Formatos_de_inspeccion`,
`Clientes\ANDRITZ\Malpaso\Inspecciones_y_analisis\Presion_de_unidades\Informes`,
`Clientes\ANDRITZ\Malpaso\Logistica_y_embarques\HCN\Listas_de_empaque`,
`Clientes\ANDRITZ\Malpaso\Logistica_y_embarques\HCN\Reportes_de_entrega`,
`Clientes\ANDRITZ\Malpaso\Analisis_de_aceite\Etiquetas_de_muestras`,
`Seguridad_y_salud\Hojas_de_datos_de_seguridad\Fabricante`,
`Manuales_de_equipos\OMICRON\Usuario\instrumentos_prueba`,
`Certificados\Calibracion\GRUPO DE METROLOGÍA CLAM`,
`Metrologia\Control_de_equipos\General`,
`Mediciones_y_pruebas\puesta_tierra\VITRO`,
`Mediciones_y_pruebas\Reportes_de_resultados\transformadores`,
`Calidad\Acciones_correctivas_y_preventivas\SERINTRA`,
`Calidad\Manuales_del_sistema_de_gestion\SERINTRA`,
`Seguridad_y_salud\Entrega_de_EPP\SERINTRA`,
`Gestion_ambiental\Programas\SERINTRA`,
`Gestion_de_proyectos\Asignaciones\SERINTRA`,
`Descripciones_tecnicas_de_sistemas\transformadores\ANDRITZ`,
`Referencias_tecnicas\transformadores\General` e
`Informes_de_inspeccion\SERINTRA`. Nunca crea directorios ni mueve archivos.
Los resultados ambiguos, de confianza insuficiente o sin empresa obligatoria
quedan en `review` sin destino automático. Si una clasificación nueva invalida
la ubicación de un documento movido previamente por el framework, se propone
su traslado a la categoría correcta o a `Revision_pendiente`; los documentos
externos ambiguos no se mueven. Los nombres descriptivos se conservan. Solo los
nombres técnicos de baja calidad —por ejemplo `x_...` o un nombre dominado por
una identidad interna— reciben una propuesta semántica basada en tipo,
instrumento, modelo, serie, identificador y fecha disponibles. Los formatos
controlados priorizan el tipo documental, su código y el mes de revisión en
propuestas semánticas; los correos usan el asunto, los reportes diarios el
código/proyecto/fecha y los comprobantes de viaje la fecha y el total, en lugar
de campos vacíos como `Nombre del Auditor` o `Nombre del empleado`. Si dos
documentos distintos terminan con el mismo nombre y clasificación, el segundo
conserva ambos archivos mediante un sufijo determinista y compacto de tipo e
identidad de volumen/archivo. Una colisión que aparezca durante la aplicación
—incluidos alias cortos 8.3 de Windows— se desambigua otra vez antes del
movimiento; no se reemplaza ni se descarta ningún archivo.

`--organization-apply` consume como máximo el lote solicitado de planes
persistidos; el flujo integral consume todos los planes seguros mediante varios
lotes. Antes
de cada movimiento revalida identidad, tamaño, `mtime` y `birthtime`; exige el
origen y destino en NTFS local y el mismo volumen, un archivo regular con un
único hard link y handles retenidos; no reemplaza destinos y rechaza árboles del
sistema, el directorio de estado, UNC, otros filesystems, symlinks y junctions.
Registra `applied`, `stale`, `blocked`, `recovery_required` o `failed`. La carpeta predeterminada no se crea al planear ni consultar: se crea
únicamente cuando `--organization-apply` o el `--apply` integral encuentra planes vigentes;
su directorio padre debe existir. El plan solo llega a `applied` después de
actualizar de forma idempotente el catálogo, PDF/DOCX/Office/texto/audio, FTS, inventario
actual, revisiones abiertas, planes pendientes de deduplicación y, cuando ya
existe, la ruta durable del elemento en `semantic.sqlite3`. Si el proceso
se interrumpe después del movimiento, queda `moved_cache_pending` y la siguiente
aplicación reanuda únicamente la sincronización sin volver a mover el archivo.
Si el efecto nativo no puede confirmarse, queda `recovery_required`, reserva el
destino y se excluye de todo reintento automático; se consulta con
`--organization-preview 100 --organization-preview-status recovery_required`.
La planificación muestra documentos evaluados y la aplicación actualiza en vivo
movimientos, sincronizaciones de caché, bloqueos, errores y pendientes; una barra
anterior completada ya no oculta estas fases posteriores.

Sin `--root`, el framework trabaja sobre el perfil del usuario actual. El
tamaño y la cantidad de PDF son ilimitados salvo que se indiquen controles
explícitos. `--MaxMB` usa megabytes decimales, por lo que `1000` equivale a
1 GB:

```powershell
Neocortex --route pdf --MaxMB 1000 --MaxCount 250
```

También se aceptan los alias `--max-mb` y `--max-count`. El límite de cantidad
se aplica después del límite de tamaño y antes de despachar trabajos PDF.

Una ruta puede reutilizar el snapshot durable de candidatos sin repetir
inventario, deduplicación ni acciones:

```powershell
Neocortex --route pdf --route-only
Neocortex --route pdf --route-only --candidate-run 40
Neocortex --route pdf --route-only --select-status error --select-error-type PdfDocumentTimeout --MaxCount 25
Neocortex --route pdf --route-only --failed-pages-only
Neocortex --route image --route-only --select-recommendation manual_review
Neocortex --route docx --route-only --select-path C:\Datos\documento.docx
```

`--route-only` exige una ruta, rechaza `--apply` y toma por defecto el snapshot
más reciente. Las selecciones son explícitas y acotadas; las entradas no
seleccionadas conservan intacto su estado de caché.

Los productores PDF e imagen mantienen afinidad de thread para su stream SQLite
de candidatos: el mismo thread crea, itera y cierra el generator mediante
`finally`, incluso si falla la admisión de recursos o se cancela la ruta. El
coordinador no lo desenrolla desde otro thread.

El snapshot se publica sólo después de terminar la generación completa de
candidatos: vínculo a `scan_id`, contadores y evento versionado comparten una
transacción. Al reutilizarlo se validan scan completo, conteo de archivos, ruta
e identidad física de la raíz. Un run legacy interrumpido sin vínculo exige
evidencia de inventario inequívoca, conteos coincidentes y al menos un
`route_run` durable; de lo contrario la operación se abstiene.

La clasificación de imágenes es una ruta primaria y consume los MIME `image/*`
ya detectados por el inventario común; no vuelve a recorrer el perfil:

```powershell
Neocortex --route image
```

Los resultados, evidencia, incertidumbre, atributos y errores quedan en
`image.sqlite3`. La ruta es incremental y reanuda por identidad y metadatos del
archivo. `--image-max-mb`, `--image-max-count` y `--retry-image-errors` controlan
la selección. `--image-max-count` es un límite duro de candidatos, incluidos
los aciertos de caché y el cálculo/reuso de huellas completas. El presupuesto
agregado y los márgenes se ajustan mediante
`--image-memory-budget-mb`, `--image-min-free-memory-mb`,
`--image-min-free-commit-mb` y `--image-memory-wait-timeout`. Esta ruta clasifica
e indexa; no mueve ni elimina imágenes.

En modo predeterminado, `--image-document-ocr auto` aplica un verificador
ligero únicamente a candidatos con estructura de página, diagrama o captura.
Tesseract recibe una muestra acotada por `stdin` y devuelve TSV por `stdout`:
no se crean rasterizados ni archivos OCR temporales. El texto reconocido con
confianza suficiente se conserva comprimido hasta un máximo de 16 KiB UTF-8,
con longitud, XXH3 y marca explícita de truncamiento. En `evidence_json` quedan
además conteos de palabras, líneas y caracteres, cobertura, confianza,
etiquetas semánticas limitadas, versión y errores. `--image-ocr-lang` selecciona
idiomas y `--image-ocr-timeout` limita cada candidato;
`--image-document-ocr never` conserva el clasificador de píxeles sin el
verificador.

La candidatura documental es independiente de la categoría principal y guarda
score, evidencia e incertidumbre. Las menciones eléctricas detectadas por OCR
se registran separadas de las inferidas por nombre/ruta. Un clasificador visual
modular añade candidatos multietiqueta con puntuación, evidencia, versión y
procedencia. El backend incluido es deliberadamente conservador y no calibrado:
solo aporta señales visuales amplias; el protocolo permite sustituirlo por un
modelo local calibrado sin atribuir entidades no demostradas. Pillow decodifica
primero en modo estricto y solo tolera streams truncados ante errores conocidos.
Una recuperación útil queda versionada y limita la confianza a 0,72; píxeles
uniformes sin contenido o una corrupción PNG demostrada mediante estructura y
CRC32 quedan como candidatos de eliminación para revisión, nunca como acciones
automáticas.
La decisión v10 no trata el fondo compuesto de un recurso transparente ni una
composición casi cuadrada como página por sí solos: exige geometría plausible o
texto/terminología documental firme. El cambio de firma fuerza reclasificación
sin perder la caché compatible de características.

Los hallazgos PDF, DOCX, Office, audio e imagen convergen en
`review_candidates` dentro de `framework.sqlite3`. Cada registro conserva
identidad, snapshot, causa, evidencia, detector, confianza, retryability y
recomendación. Consultarlos es una operación directa de solo lectura:

```powershell
Neocortex --review-candidates 100
Neocortex --review-candidates 100 --review-recommendation deletion_candidate
```

Los filtros de revisión no se aceptan sin `--review-candidates`, y el comando
rechaza `--apply`. Marcar `deletion_candidate` significa exclusivamente que el
archivo debe revisarse; no lo envía a la Papelera ni autoriza su eliminación.

La salida de candidatos expone `route`, `reason`, `volume`, `file` y
`generation`. Esos cinco valores identifican exactamente el hallazgo abierto
que recibe una decisión humana. `--review-record` exige también el actor,
revalida snapshot y generación bajo el bloqueo del framework y agrega una fila
inmutable; nunca ejecuta una acción sobre el archivo. Los estados admitidos son
`confirmed`, `dismissed` y `deferred`:

```powershell
Neocortex --review-record confirmed --review-route image --review-reason image_uniform_content --review-volume-id HEX --review-file-id HEX --review-generation RUN_ID --review-actor Victor --review-note "Revisado contra el original"
Neocortex --review-decisions 100
Neocortex --review-decisions 100 --review-decision-status dismissed --review-route image
Neocortex --review-decisions 100 --review-reason image_uniform_content --review-volume-id HEX --review-file-id HEX --review-generation RUN_ID
```

El historial `review_decisions` es append-only e idempotente: una decisión
nueva no sobrescribe otra anterior y congela la evidencia, detector,
recomendación, retryability y confianza exactos que vio la persona. Listarlo es de solo lectura y puede
filtrarse por estado, ruta, causa, identidad durable y generación. Ni registrar
ni consultar decisiones se combina con `--apply`.

Las decisiones pueden materializarse incrementalmente como ejemplos trazables
de evaluación. Cada invocación de sincronización procesa un único lote entre 1
y 256 decisiones; el cursor durable permite continuar sin reexaminar rangos
anteriores. Las métricas son descriptivas e informan explícitamente que la
calibración no está establecida:

```powershell
Neocortex --review-evidence-sync --review-evidence-batch-size 128
Neocortex --review-evidence-metrics --review-evidence-route image
Neocortex --review-evidence-list 100 --review-evidence-status dismissed --review-evidence-completeness complete
Neocortex --review-evidence-list 100 --review-json
```

El listado admite filtros propios por ruta, causa, recomendación objetivo,
versión del detector, actor, estado de la decisión y disponibilidad del snapshot
de evidencia. `--review-json` produce JSON Lines determinista para candidatos,
decisiones, registros y evidencia. Ninguna de estas etiquetas autoriza
movimientos, renombres o eliminaciones.

Los decodificadores y Tesseract viven en workers persistentes contenidos por
Job Objects. La reserva incluye todo el árbol del proceso y el coordinador
mantiene márgenes de memoria física y commit. Una actualización que sólo
cambia reglas de decisión reutiliza `features_json` compatible sin volver a
decodificar las imágenes; la salida distingue `feature_cache_hits` de hits
completos e informa intentos, positivos y fallos OCR. La firma de procesamiento
incluye versiones separadas de características, decisiones, Tesseract,
idiomas y tamaño de muestra.

Los subprocess acotados y los workers aislados usan en Windows hijos creados
suspendidos y Job Objects kill-on-close asociados por handle exacto antes de
reanudar. Timeout, overflow, cancelación o excepción terminan el árbol propio,
esperan al hijo directo y cierran pipes y handles.

La definición de argumentos, la traducción a `FrameworkConfig` y el flujo de
aplicación viven respectivamente en `cli_parser`, `cli_config` y `cli_app`;
`route_selection` mantiene el contrato ligero usado por la validación. Las
operaciones directas importan sólo su backend y el registro carga cada motor
únicamente cuando se ejecuta su adaptador. Por ello consultar
`Neocortex --help`, validar opciones o seleccionar rutas no carga por adelantado
los motores PDF, DOCX o de imagen. El shim independiente anterior fue retirado.

La implementación está separada por responsabilidad: `pdf_route` coordina la
extracción, `pdf_state` conserva y migra el esquema, `pdf_cache` comparte la
evidencia XXH3 con el deduplicador, `pdf_derived` construye FTS/perfiles y
similitud, y `pdf_admin` realiza diagnósticos sin inventariar el filesystem.
`route_filters`, `run_lifecycle` y `run_status` aíslan respectivamente la
selección explícita, el heartbeat/detección de huérfanos y la consulta operativa
de solo lectura.
La caché usa metadatos por defecto. La validación estricta vuelve a leer el PDF
y compara su XXH3 completo con el asociado a la extracción:

```powershell
Neocortex --root C:\Corpus\Entrada --route pdf --pdf-cache-validation full
```

Las operaciones administrativas son directas y no inician un inventario:

```powershell
Neocortex --pdf-doctor
Neocortex --pdf-verify
```

La ejecución integrada aísla extracción y perfilado en procesos supervisados.
Un timeout termina el árbol del proceso, incluido Tesseract en Windows, sin
dejar el trabajador bloqueado:

```powershell
Neocortex --route pdf --pdf-document-timeout 600
```

El modo predeterminado `--pdf-timeout-mode adaptive` parte de ese valor y
ajusta el plazo por tamaño, páginas conocidas y páginas pendientes, limitado
por `--pdf-max-document-timeout`; `fixed` conserva un plazo uniforme. El timeout
OCR por página continúa siendo independiente.

Los cierres del hijo conservan fase, código y política de reintento. Los lotes
de promoción son pequeños para que un timeout pueda continuar desde páginas ya
confirmadas. Un timeout que avanzó páginas conserva una reanudación inmediata y
reinicia su presupuesto de reintentos; uno que no avanzó mantiene backoff y
límite durable. Las filas legacy incompletas reciben una sola migración a esta
política. Si la estructura no abre, un hijo intenta una reescritura acotada
con qpdf en el directorio temporal y después los fallbacks de extracción; el
temporal siempre se retira y el original permanece intacto. Una recuperación
completa queda `done`; sólo una recuperación incompleta con páginas fallidas
queda `partial` y `manual_review`. Un PDF cifrado queda `keep_protected`, nunca
como candidato de eliminación. Si qpdf y los extractores de fallback confirman
que la estructura no es recuperable, se registra `deletion_candidate`; en modo
normal el original se conserva. En `0.8.0`, incluso con `--apply`, la acción de
Papelera se registra `skipped` porque no existe una primitiva ligada a identidad.

Una secuencia de 32 páginas consecutivas que no existen o no pueden cargarse
detiene ese recorrido y promueve el contenido ya comprobado como `partial`, con
el número de páginas omitidas en la evidencia. Así, un árbol de páginas roto no
consume el timeout completo intentando miles de páginas inexistentes.

Los permisos de concurrencia OCR pertenecen al proceso supervisor, no a los
hijos. Cada hijo solicita y devuelve una concesión por protocolo; si expira o
es terminado mientras usa Tesseract, el supervisor recupera el permiso después
de cerrar su Job Object. Por ello un PDF atascado no puede bloquear el OCR de
todos los documentos posteriores.

La extracción sigue siendo paralela, pero la promoción a `pdf.sqlite3` pasa
por un único escritor con lotes acotados. Los avisos nativos de MuPDF se
suprimen de la consola, se drenan después de cada página para no acumularlos
sin límite, se consolidan por documento y etapa, y se conservan en
`document_warnings` con conteo y una muestra limitada. Esto cubre extracción y
perfilado. La salida resume `warning_documents` y `mupdf_warnings`.

El perfilado PDF conserva cada lote de páginas ya publicado y reanuda sólo las
páginas cuyo perfil o layout vigente falta; después reconstruye el perfil de
documento mediante un recorrido SQLite acotado. Los documentos cuya extracción
sigue en `PdfDocumentTimeout` se difieren hasta que esa etapa termine, en vez de
competir durante otros diez minutos con trabajo incompleto. Cada fallo de
perfilado conserva tipo, mensaje acotado y número de intentos en
`document_warnings` con etapa `profile-error`, y se retira al publicar el perfil.

Antes de aplicar `MaxMB` o `MaxCount`, la ruta marca todos los PDF del
inventario vivo. Por eso un documento omitido por esos límites conserva su
caché reutilizable. Después de completar correctamente la ruta se eliminan en
lotes los documentos de caché cuyo archivo desapareció o cambió, junto con sus
páginas, FTS, perfiles, advertencias y relaciones derivadas. Las métricas son
`pdf_cache_documents_pruned` y `pdf_cache_rows_pruned`; nunca se elimina el PDF
de origen durante esta reconciliación.

Un cambio de ruta puede desalojar una fila que aún ocupe el destino solo cuando
su identidad ya no corresponda al `pdf_inventory` vivo; se eliminan también sus
capas derivadas. Un dueño todavía vigente se preserva y detiene el touch antes de
mutar la caché.

Un cache hit PDF exige identidad NTFS, tamaño, `mtime`, `birthtime` y firma de
procesamiento coincidentes, y comprueba también la cantidad de páginas y
errores persistidos. Las filas anteriores a la incorporación de `birthtime`
solo se promueven después de confirmar su XXH3-128 completo, reutilizando la
huella del índice común cuando ya existe y leyendo el archivo una vez en caso
contrario. La escritura de esa promoción se difiere al touch por lotes: la
verificación no conserva una transacción abierta ni bloquea a los extractores
concurrentes. Sin esa evidencia se procesa como miss normal. Si faltan páginas, se
repite la extracción; si solo faltan filas FTS, perfiles, firmas o relaciones
de similitud, se reconstruye únicamente esa capa. Los registros transitorios
de enrutamiento y las generaciones históricas de similitud que ya no respaldan
un estado activo también se podan después de una ejecución satisfactoria. El
fallo histórico de control OCR con `BoundedSemaphore` se migra de forma dirigida
a un error reintentable; no se invalida el resto de la caché.

El perfilado construye además un mapa de layout por página. Para PDF nativos
normaliza a una cuadrícula independiente del tamaño las cajas de texto e
imagen, fuentes, tamaños, estilos, líneas, dibujos, márgenes y rotación. Para
páginas escaneadas añade una cuadrícula visual en escala de grises de muy baja
resolución, calculada exclusivamente en memoria; no conserva renderizados ni
crea archivos de imagen. Encabezado y pie tienen firmas separadas para detectar
membretes aunque cambie el cuerpo del documento.

Los mapas comprimidos se guardan en `page_layouts`, sus agregados documentales
en `document_layouts`, las relaciones explicables en `similarity_relations`
como `layout_similar`, y las familias conexas en `layout_groups` y
`layout_group_members`. La evidencia separa geometría, apariencia, encabezado,
pie y secuencia de páginas. La firma incluye una versión de algoritmo; si falta
un mapa o cambia la versión, solo se repite el perfilado, no la extracción ni el
OCR. Las métricas son `layout_pages_mapped`, `layout_similarity_pairs` y
`layout_groups`.

Las familias activas pueden consultarse sin inventariar el perfil:

```powershell
Neocortex --pdf-layout-groups 20
```

Las consultas directas abren `pdf.sqlite3` en modo de solo lectura: no crean la
base, no inicializan tablas y no ejecutan migraciones. Un estado ausente,
incompatible o dañado produce código de salida `2` sin alterar sus bytes.

Cada familia muestra un representante, cantidad de miembros, puntuación mínima
de sus relaciones y hasta veinte rutas de ejemplo.

`run_events` conserva la configuración efectiva y duraciones separadas para
inventario, planificación de duplicados, archivos vacíos, duplicados,
validación de tipos, extracción PDF, deduplicación textual, FTS, firmas,
perfiles y similitudes.

La coincidencia de texto normalizado entre PDF se registra únicamente como la
acción consultable `review_pdf_text_duplicate`. Nunca elimina archivos, incluso
con `--apply`, porque el mismo texto no demuestra equivalencia visual, de
firmas, geometría ni contenido no textual.

Cada página se confirma por separado. Un fallo se conserva en `page_errors`,
las demás páginas siguen disponibles y el documento queda como `partial`;
solo fallos transitorios clasificados (memoria de OCR, timeout, colisión
temporal o un fallo interno ya corregido) pueden reintentarse automáticamente.
Cada archivo tiene un máximo durable de tres intentos fallidos y espera
exponencial; un límite de render sin cambios o un PDF estructuralmente dañado
se reutiliza como error cacheado. `--retry-pdf-errors` fuerza manualmente un
nuevo intento y, en documentos parciales, procesa solo las páginas fallidas.
Para abortar al primer fallo se puede usar `--pdf-fail-fast-pages`.

Una secuencia de 32 páginas estructuralmente inaccesibles activa una sola
recuperación completa en la corrida siguiente: primero reescritura temporal con
QPDF y después PDFMiner si el árbol de páginas reparado continúa fallando. Una
recuperación completa queda `done`, no `partial`. Si los tres motores fallan,
el archivo se conserva como candidato de revisión; `--apply` no lo envía a la
Papelera y conserva la abstención registrada:

```powershell
Neocortex --all --apply
```

`--apply` es la autorización única para las acciones soportadas de las rutas
seleccionadas; las acciones de Papelera permanecen deshabilitadas.

El render OCR usa escala de grises y reduce automáticamente los DPI efectivos
cuando la página superaría `--pdf-max-render-pixels`, sin bajar de 72 DPI. Si
Tesseract reporta falta de memoria, vuelve a renderizar a una resolución menor
dentro del mismo intento. Si ni a 72 DPI cabe, la página queda registrada como
error en vez de agotar memoria. Las colisiones temporales de archivo de
Tesseract se reintentan una vez. Los cierres transitorios del proceso hijo y
bloqueos breves de SQLite se agendan con espera creciente en la caché. Un
timeout que dejó páginas durables se reanuda automáticamente desde las páginas
faltantes; uno sin avance durable queda cacheado y requiere
`--retry-pdf-errors` para evitar repetir indefinidamente el mismo trabajo.

Los rangos usan numeración de páginas desde uno y quedan marcados como
extracciones parciales, por lo que no participan en deduplicación ni similitud
textual de documentos completos:

```powershell
Neocortex --route pdf --pdf-page-start 20 --pdf-page-end 40
```

Antes de despachar trabajo se comprueban espacio libre, memoria física y
capacidad de commit. Extracción y perfilado comparten un presupuesto ponderado:
cada proceso activo reserva el árbol completo de MuPDF, Pillow y Tesseract. En
modo OCR la reserva efectiva mínima es 1 GiB y puede crecer según el límite
de render; el total no puede superar `--pdf-memory-budget-bytes`. Si no se
especifican, el presupuesto y los márgenes físico/commit se calculan de forma
adaptativa. Los PDF que superan
`--pdf-large-document-bytes` usan además un cupo independiente limitado por
`--pdf-large-document-workers`, cuyo valor predeterminado es dos; el coordinador
global mantiene los límites de memoria y commit aunque exista ese paralelismo.
Durante extracciones largas la barra muestra el agregado `páginas X/Y` leído de
los lotes ya confirmados en SQLite, por lo que refleja avance durable entre un
documento completado y el siguiente. Los demás controles son
`--pdf-min-free-bytes`, `--pdf-memory-backpressure-bytes`,
`--pdf-commit-backpressure-bytes` y `--pdf-memory-wait-timeout`; el valor `0`
desactiva únicamente el margen físico o de commit indicado.

Las rutas PDF, DOCX, Office, ZIP, texto/correo, audio, video, imágenes y código
pueden ejecutarse juntas. Comparten el mismo inventario, bloqueo operativo,
registro de ejecución y coordinador global de memoria, commit y CPU.
`Neocortex --all` selecciona las nueve y deja que el
coordinador dimensione dinámicamente memoria, margen libre y CPU según el equipo;
opciones compatibles indicadas explícitamente por el usuario tienen precedencia.
Una combinación contradictoria como
`--all --route pdf` se rechaza. `--all` no fuerza errores permanentes ya
cacheados; los flags `--retry-pdf-errors`, `--retry-docx-errors`,
`--retry-office-errors`, `--retry-archive-errors`, `--retry-text-errors`,
`--retry-audio-errors`, `--retry-video-errors`, `--retry-image-errors` y
`--retry-code-errors` siguen disponibles como overrides manuales.

Durante las rutas de contenido, Rich muestra contadores vivos junto a cada
barra: hits de caché, errores cacheados, elementos nuevos, actualizaciones de
caché por cambio de firma o estado, reintentos reales,
reclasificaciones, páginas que realmente serán reintentadas, resultados
completados, errores, timeouts, OCR, trabajo en curso, elementos restantes y
esperas del coordinador. Los mismos campos de
trabajo nuevo, caché actualizada y reintentos quedan en el resumen durable de la ruta, por lo que
una barra lenta puede distinguir trabajo real de espera y de reutilización.

La admisión prioriza una ruta todavía inactiva sobre otra que ya conserva
trabajo, siempre que la nueva solicitud quepa en los recursos libres. El
timeout global sólo mide una falta sostenida de memoria física o commit cuando
no queda trabajo activo que pueda liberar reservas; no convierte en error la
espera normal detrás de un PDF supervisado. Así, un documento que alcance su
propio timeout puede fallar de forma aislada sin hacer caer DOCX, Office o imágenes.

Los errores de archivos individuales quedan en los resúmenes y bases de cada
ruta sin convertir por defecto una corrida completa en fallo del proceso. Para
automatización que requiera una salida distinta de cero ante cualquier error,
documento parcial o error cacheado se usa `Neocortex --all --strict-exit-codes`.

El directorio de estado tiene un bloqueo exclusivo: dos ejecuciones no pueden
aplicar acciones simultáneamente. Una cancelación de teclado se registra como
`cancelled`, distinta de un fallo. PID, heartbeat y fases se conservan de forma
durable; al adquirir el bloqueo, una ejecución previa sin proceso vivo se marca
como interrumpida. El primer `Ctrl+C` solicita cierre cooperativo y despierta las
esperas de recursos; un segundo `Ctrl+C` vuelve a interrumpir el hilo principal
en vez de quedar ignorado durante el cierre. Los snapshots recientes de enrutamiento se conservan para
reanudar trabajo sin volver a inventariar:

```powershell
Neocortex --status
Neocortex --status --status-run 35 --status-json
Neocortex --resume-run 35
```

`--status` abre el estado en modo de solo lectura. La reanudación reutiliza las
fases PDF ya completadas y continúa las pendientes; no repite extracción ni
deduplicación textual confirmadas. Sólo acepta un snapshot que haya cruzado la
frontera durable de publicación descrita arriba. Si un archivo desaparece o cambia entre el
inventario y la validación, se cuenta como `stale_inventory` y no como error de
acción; la siguiente reconciliación actualiza el inventario persistente.

El watcher incremental se ejecuta exclusivamente en primer plano dentro del
proceso de `Neocortex`; no instala servicios, tareas ni procesos en segundo
plano. Usa USN sólo como señal opcional en Windows. Cuando el checkpoint no
incluye cursor —o el lector USN deja de estar disponible— programa una corrida
normal portable sobre Dedup v9 y vuelve a cargar la publicación durable:

```powershell
Neocortex --root C:\Corpus\Entrada --watch --all
Neocortex --root C:\Datos --watch --route pdf,image --watch-bootstrap if-needed
Neocortex --watch --watch-poll-timeout-seconds 2 --watch-debounce-seconds 1 --watch-max-debounce-seconds 15
Neocortex --watch --watch-portable-interval-seconds 300
```

`--watch-bootstrap` admite `always`, `if-needed` y `never`. También se pueden
acotar el backoff inicial/máximo y su multiplicador con las opciones
`--watch-error-backoff-*`. El intervalo portable predeterminado es 300 segundos
y admite de 1 a 86 400. Los eventos, cada corrida y el resumen final se
imprimen con contadores explícitos. `Ctrl+C` solicita cancelación cooperativa.
La recarga del owner durable usa una instantánea immutable con cercas y no
recrea `framework.sqlite3-wal/-shm`; si la publicación está activa, falla
cerrada y entra al backoff normal del watcher.
Por seguridad, `--watch` rechaza `--apply`, `--route-only`, `--resume-run` y
`--candidate-run`.

Un watcher mantiene además un byte lock cross-process durante toda su vida,
identificado por XXH3-128 de raíz+estado canónicos. El archivo
`watcher-life-xxh3-128-<digest>.lock` registra PID, creación, host, versión,
argv, raíz/estado e inicio para diagnóstico. Otro owner vivo hace que la CLI se
abstenga con código `2`; metadata stale sólo se reemplaza tras adquirir el lock.
No mata procesos, se libera en cierre/caída y no sustituye el `framework.lock`
de cada corrida.

## Dependencias

`pyproject.toml` declara las dependencias Python y el conjunto de herramientas
de desarrollo probado. Tesseract y sus datos `spa`/`eng` son componentes
externos al entorno Python y se validan mediante `Neocortex --pdf-doctor`.
FastEmbed/ONNX es opcional y está fijado en el extra `.[semantic]`; solo es
necesario para preparar modelos, generar embeddings, buscar en espacios
vectoriales o materializar evidencia semántica. La búsqueda `lexical` y las
consultas de estado/evidencia persistida no descargan ni requieren cargar esos
modelos.

El paquete expone el entry point instalable `Neocortex` y también admite
`python -m neocortex`. La versión se define una sola vez en
`neocortex.__version__`; tanto los metadatos de distribución como la aplicación
Qt consumen esa fuente; no existe un módulo lanzador paralelo.
