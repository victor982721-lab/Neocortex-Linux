# Seguridad y operaciones sobre archivos

NeoCortex procesa archivos potencialmente dañados, usa herramientas externas y
puede recibir autorización para modificar el filesystem. Este documento define
los límites de confianza observados; no constituye una afirmación de sandbox o
seguridad perfecta.

## Modelo de confianza

Trate como no confiables:

- nombres, rutas, metadatos y contenido del corpus;
- PDF, ZIP/OOXML/ODT, texto/EML, Office heredado, imágenes, audio, vídeo y
  código analizado;
- modelos, pesos y cachés descargados;
- rutas de ejecutables externos;
- taxonomías TOML aportadas por el usuario;
- estado SQLite copiado de otra instalación;
- resultados semánticos, clasificadores y reglas heurísticas.

El estado persistido es evidencia operativa, no una autoridad sobre el estado
actual del filesystem. Una observación puede quedar obsoleta después de
registrarse.

Todo `ContextBundle` renderizado empieza, antes de la consulta y de cualquier
evidencia dinámica, con la frontera versionada
`untrusted-corpus-data-v1`. Declara el contenido como
`recovered_corpus_evidence` no confiable y fija
`instruction_authority=false`, `tools_authorized=false` y
`actions_authorized=false`. El marcador separa el contrato de NeoCortex del
texto recuperado; el consumidor agéntico debe respetarlo y nunca interpretar
PDF, OCR, Office, audio, código, metadatos o relaciones como autorización.

## Niveles de efecto

| Nivel | Ejemplos | Efecto |
|---|---|---|
| Consulta | `--help`, `--version`, `--status`, `--action-recovery-status`, `--retention-status`, `--knowledge-status`, `--knowledge-search`, `--knowledge-context`, `inspect lineage`, búsquedas, previews y doctors | No debe recorrer ni modificar el corpus; puede fallar si falta estado. SQLite read-only puede participar en WAL/SHM. |
| Estado sin mutación del corpus | Corrida sin `--apply`, `--semantic-index`, `--semantic-classify`, `--catalog-documents`, `--organization-plan`, `--review-record`, `--action-recovery-record` | Lee contenido o cachés y escribe bases, evidencia o planes. |
| Descarga/carga externa | `models prepare`; `--semantic-prepare-models`; primera transcripción Windows sin `--audio-local-models-only` | Puede adquirir modelos y ampliar cachés. `models status` es local y read-only. |
| Mutación de archivos | Corrida integrada con `--apply`; `--organization-apply` | En Windows puede renombrar extensiones o mover documentos sólo bajo el contrato NTFS ligado a handles; en Linux se rechaza antes de crear estado. |
| Purga de estado | `Neocortex databases purge --apply --confirm-database-purge DELETE_DATABASES` | Elimina sólo las bases SQLite canónicas y sus sidecars después de backup verificado y locks exclusivos; no toca corpus, releases, modelos ni recibos. |

“No destructivo” significa que una corrida sin autorización no debe mutar los
originales. No significa que sea de sólo lectura: el estado y las cachés sí se
actualizan.

Las operaciones Knowledge y la inspección de linaje pertenecen específicamente
al nivel **Consulta**:
abren sólo estado existente y no crean ni actualizan bases, cachés, planes o
archivos del corpus. Un propietario con esquema futuro, incompatible o corrupto
produce abstención explícita; no se migra, repara ni reconstruye durante la
consulta.

`Neocortex knowledge health` conserva esa misma frontera. Sólo acepta una
identidad física canónica `resource:file`, consulta los scopes fijos y captura
el snapshot lógico Knowledge más los facts Inventory/owner/Catalog mediante
readers immutable. Selecciona Text o PDF por identidad física y evidencia del
snapshot, nunca por path o extensión. No recibe paths, no lee el
archivo original y no cruza registros con identidad o processing signature
distintos. Dos observaciones y un único retry evitan publicar como estable una
vista cambiante. El lector PDF schema 13 es content-blind: observa estados,
páginas, staging, errores, warnings, FTS y recovery tipado sin devolver texto,
metadata ni mensajes de error. El resultado es advisory, no certifica
contenido/OCR, fidelidad visual ni verdad semántica y
mantiene `mutation_authorized=false` incluso cuando declara `healthy`.

### Rutas internas protegidas

La topología reservada es
`%USERPROFILE%\Neocortex\Repository`,
`%LOCALAPPDATA%\Programs\Neocortex`,
`%LOCALAPPDATA%\Neocortex` y el launcher estable
`%LOCALAPPDATA%\Programs\Neocortex\bin\Neocortex.exe`.
`InternalPathsPolicy` captura rutas e identidades físicas, rechaza
aliases/reparses y protege también un hardlink del launcher. Una raíz normal
situada dentro de un árbol propio se rechaza; los árboles propios descendientes
de un corpus permitido se excluyen. El estado no puede ser igual ni ancestro
del corpus. Dedup v10 conserva la firma cruda de exclusión y Framework v22
conserva la firma efectiva incorporada en v20, que también incluye las rutas
internas.

En Linux, la política protege además estado/configuración/datos XDG, releases,
modelos, runtimes, el launcher `~/.local/share/Neocortex/bin/Neocortex`, el alias
`~/.local/bin/Neocortex` y los archivos `.desktop`; el inventario nunca sigue
symlinks.

### Código como contenido

La ruta Code recibe archivos del usuario como datos no confiables. Puede leer su
texto, extraer estructura, guardar identidad y relaciones, y ofrecer búsquedas,
pero nunca ejecuta el código observado ni autoriza cambios sobre él.

La ruta productiva no mantiene review interno, experimentos, proveedores de QA,
receipts de validación ni un agregador de herramientas. `pytest`, Ruff,
Pyright/Mypy y Semgrep pertenecen al desarrollo y se ejecutan directamente,
fuera del runtime, sólo cuando una modificación lo requiera.

La CLI pública de Code se limita a estado, búsqueda, proyectos y reconstrucción.
No acepta `--self-analysis`, `--code-review`, `--code-experiment-run` ni otros
comandos de autoevaluación. El contenido, los nombres y los resultados
semánticos siguen siendo datos; ninguna similitud, diagnóstico o clasificación
puede mover, renombrar o eliminar archivos.

## Autorizaciones de mutación

Existen dos superficies explícitas:

1. `--apply` autoriza las acciones de una corrida integrada.
2. `--organization-apply` consume planes de organización ya persistidos sin
   requerir además `--apply`.
3. `databases purge --apply --confirm-database-purge DELETE_DATABASES` autoriza
   exclusivamente la eliminación de estado SQLite propio, con backup y
   revalidación de identidad; no autoriza mutaciones del corpus.

Según las rutas y planes, `--apply` puede corregir extensiones incompatibles con
una firma reconocida y aplicar movimientos documentales con clasificación
suficiente, siempre que la identidad y plataforma satisfagan la sección
siguiente. Los duplicados binarios, vacíos, directorios vacíos y PDF
irrecuperables siguen apareciendo como candidatos en dry-run, pero la aplicación
los marca `skipped`: la única API de Papelera evaluada era path-bound y no se
invoca. `Send2Trash` fue retirado como dependencia y no existe un bypass
permisivo.

Antes de autorizar:

1. confirme versión, raíz y estado efectivos;
2. detenga watcher y otras corridas;
3. cree un backup SQLite consistente conforme a
   [RECOVERY.md](RECOVERY.md);
4. revise planes, candidatos y destinos;
5. limite el lote cuando la operación directa lo permita;
6. confirme que existe espacio y que el destino no contiene datos que pudieran
   colisionar;
7. preserve la salida y el `run_id`.

No use operaciones reales para probar una instalación. Use fixtures temporales.

### Contención de pruebas nativas

Una prueba que invoque una mutación nativa debe definir una única raíz de
laboratorio canónica antes de crear el fixture. El helper, no sólo el test, debe
rechazar rutas relativas ambiguas, `..`, UNC, enlaces/reparses y todo
origen/destino/padre fuera de esa raíz. Debe verificar raíz, volumen e identidad
antes de cada llamada, registrar cada objeto por identidad y fallar cerrado si
no puede demostrar la contención.

`TEMP`/`TMP`, `PYTHONPYCACHEPREFIX`, caché y `--basetemp` de pytest, Coverage,
build, distribuciones y venv deben redirigirse antes de importar el proyecto. Al
cerrar, inspeccione fugas y retire sólo artefactos cuya identidad y procedencia
sean las del fixture. No ejecute el helper contra el perfil, repositorio,
escritorio, documentos o una raíz compartida.

Las regresiones de coordinación USN que no necesitan probar el driver deben
usar el journal sintético contenido del proyecto. Ese doble comprueba creación,
modificación, rename y borrado por identidad dentro de la raíz temporal y
prohíbe abrir el volumen raw; no sustituye las pruebas específicas de la
primitiva Windows ligada a handles.

## Evidencia probabilística y revisión humana

Embeddings, similitud, clasificación semántica, OCR, categorías de imagen y
taxonomía documental son señales complementarias. La evidencia semántica actual
se declara advisory y no calibrada.

Por sí solos, estos resultados nunca deben autorizar:

- eliminación;
- envío a la Papelera;
- rename;
- movimiento;
- elección de una versión canónica.

Los `KnowledgeHit`, sus evidencias y el `ContextBundle` compilado también son
resultados de consulta. Una cita, un score alto, la fusión entre propietarios o
el texto de contexto no constituyen una autorización de mutación ni pueden
activar `--apply` o `--organization-apply`.

Los `WorkReceipt`, las materializaciones y la proyección de derivación tienen
la misma frontera: explican causalidad técnica, no autorizan una acción física.
Un output derivado, un cache hit o una clase de reproducibilidad nunca sustituye
policy, autorización, revalidación de identidad ni recibo de efecto.

### ReviewTask no es autorización

Framework v22 conserva `ReviewTask` como una cola advisory: input/revisión,
snapshot, evidencia, motivo, incertidumbre, impacto, irreversibilidad,
sugerencias y eventos de estado. La prioridad
`impacto × incertidumbre × irreversibilidad` es una regla de ordenamiento, no
una probabilidad ni una certeza calibrada. Ni una tarea `OPEN`, ni su score, ni
una sugerencia permiten ejecutar syscalls o saltar el policy engine.

`Neocortex review value` permanece read-only y consulta una cola sólo si su
fingerprint coincide con Inventory/Catalog; de lo contrario se abstiene como
`stale`. Si la cola no existe, usa el preview legacy sin DDL. La operación
separada `review value --refresh --scope personal|framework` escribe únicamente
estado Framework y puede crear/migrar ese owner; rechaza `all`, avanza una sola
página keyset de 100 y nunca modifica archivos, Inventory o Catalog. La segunda
lectura del fence antes de publicar reduce mezcla TOCTOU, pero no afirma una
transacción distribuida.

Completar el cursor tampoco autoriza una conclusión por ausencia. La cola
mantiene por separado `scan_complete` y `evidence_complete`; cualquier owner
faltante, plan inválido o mismatch observado en una página conserva el estado
`partial` y bloquea la supersession por ausencia de tareas abiertas previas.
Así, perder cobertura no puede retirar silenciosamente trabajo humano pendiente.

Los eventos son append-only y las transiciones usan CAS. `RESOLVED` y
`DISMISSED` requieren actor humano, decisión y scope tipado. `permanent` no se
reabre; `until-source-change` y `until-policy-change` sólo expiran al cambiar el
hecho declarado y con successor receipt-backed. Decisiones legacy se conservan
terminales. `SUPERSEDED` no es una transición pública: sólo se acepta con actor
sistémico y receipt exacto de successor o head fuente. Knowledge falla cerrado
si ese head no reconcilia su source receipt,
progreso o cualquier receipt, batch o membership de la cadena publicada.
Retention trata tareas y eventos humanos como holds y protege por separado el
head vigente con toda esa cadena; el resto de la coordinación derivada puede
reconstruirse. Recovery de acciones, OCR dudoso, entities/claims y otros
dominios todavía no producen ReviewTask general: esa ausencia no debe ocultarse
creando tareas nominales o habilitando decisiones automáticas.

Los experimentos históricos del autoanalizador no forman parte de este
protocolo ni conceden autoridad sobre el estado actual. Cualquier decisión de
mutación sigue requiriendo el actor humano y las barreras explícitas de la
operación correspondiente; una evidencia histórica no equivale a aprobación
ni a un permiso vigente.

### Receipts y linaje como datos sensibles

El contrato de derivación limita bindings, configuración, runtime y tamaño JSON.
Las claves de configuración con forma de password, token, cookie, credencial,
clave privada o secreto sólo se aceptan con valor nulo o explícitamente
redactado. El adapter Semantic aplica redacción recursiva y cotas antes de
incorporar provenance a un receipt. Esta protección reduce filtraciones
accidentales; no autoriza persistir un secreto bajo una clave deliberadamente
engañosa. Los producers deben pasar únicamente configuración efectiva necesaria
para explicar/reproducir el stage.
Text tampoco persiste `str(exc)` en el documento de error, `CapabilityFailure`
ni outbox: conserva el tipo/reason code y una indicación explícita de diagnóstico
redactado. El detalle crudo de un provider o subproceso no forma parte del
contrato durable.

`inspect lineage` acepta un identificador de hasta 4096 caracteres y scopes
predefinidos; no acepta una ruta arbitraria de base. Sus readers validan el
schema owner-local, aplican ventanas SQL y distinguen truncamiento. La
proyección de outboxes tiene cotas acumuladas de eventos, bytes, nodos y aristas,
y vive sólo en memoria. Un
evento malformado, una base futura o un receipt contradictorio falla cerrado;
no se normaliza para aparentar un grafo coherente.

Text confirma intento `running` después de capturar el input exacto y antes del
parser; sólo registra receipt, outbox, outputs y heads terminales en la
transacción owner-local que publica el resultado. Semantic hace lo mismo para
las etapas conectadas en schema 7. No
hay commit coordinado entre bases: una proyección transversal se reconstruye
después de los commits y nunca debe elevarse a autoridad monolítica.

### Broker de capacidades y providers

`CapabilityManifest` describe hechos declarados; no concede autoridad al
provider ni certifica que un ejecutable sea confiable. El broker es stdlib-only
y puro respecto de engines: recibe observaciones explícitas, aplica filtros y
devuelve una selección o abstención. Inspeccionarlo no importa extractores, no
abre workspace/SQLite, no carga o descarga modelos y no inicia red o procesos.

La integración implementada se limita a `text.extract`. La política
`neocortex-text-local-v1` exige privacidad `local_only`, prohíbe red y no declara
GPU. El request por archivo exige plataforma, schemas, MIME exacto,
una clase aceptable `environment_bound` o `non_replayable` y, sólo para MIME
builtin, incrementalidad. DOC/XLS/PPT no exigen esa propiedad porque el manifest
legacy declara `incremental=false`. Readiness ambiguo, provider ausente,
incompatibilidad o empate exacto causan abstención. Nunca se elige por orden de
registro. La salida conserva causas por candidato para que la abstención no se
degrade a un fallback silencioso.

Texto/EML usa el provider builtin sin requerir LibreOffice. Para DOC/XLS/PPT
heredado, readiness exige un backend exacto observado: `soffice`/`libreoffice`
o `catdoc`/`xls2csv`/`catppt` según MIME. La extracción externa conserva la
frontera de worker acotado descrita abajo. Readiness fija un único comando
resuelto, su SHA-256, tamaño y digest de ubicación; el worker los revalida y no
prueba otro backend después de un fallo. Si no existe implementación elegible,
Text registra un `CapabilityFailure` redacted dentro del receipt owner-local y
no publica outputs ni heads parciales.

El provider builtin conserva clase `environment_bound`, declara
`incremental=true` y puede reutilizar éxitos o fallos compatibles bajo las
validaciones Text. El worker Office v2 se declara `best_effort`,
`non_replayable` e `incremental=false`: el digest verifica el launcher
seleccionado, pero no puede atestar la clausura transitiva arbitraria de engines,
librerías, configuración o procesos descendientes que éste invoque. Por ello
Text omite por completo la lectura de caché para Office heredado; una segunda
corrida vuelve a ejecutar tanto un éxito como un fallo previo, aunque la firma
permanezca igual, y registra `non_replayable` en su receipt. La pérdida de
incrementalidad legacy es un tradeoff de seguridad visible, no una optimización
pendiente ocultada como “procesar sólo cambios”.

Los receipts incorporan provider/versión, fingerprints de manifest, política y
selección, y readiness observado. Esos valores forman parte de la firma de
procesamiento para impedir reutilización bajo un contrato distinto. Un
fingerprint SHA-256 del JSON canónico demuestra igualdad del contrato. Para el
worker Office se añade por separado el digest verificable del ejecutable
observado; no demuestra por sí solo procedencia, firma del proveedor,
dependencias transitivas ni toda la cadena de suministro. La selección tampoco
autoriza syscalls, mutación del corpus ni escritura en un owner ajeno.

**PLANNED — no implementado.** PDF, DOCX, la ruta Office, Semantic y
plugins/providers externos aún no usan el broker. No hay autodescubrimiento de
plugins, aislamiento externo versionado ni permisos implícitos; cualquier
extensión futura exige registro explícito, política de red/privacidad, timeout,
recursos, compatibilidad y salida estructurada antes de poder ser elegible.

`deletion_candidate` significa “requiere revisión”, no “eliminar”. Las
decisiones `confirmed`, `dismissed` y `deferred` conservan evidencia humana, pero
registrarlas no ejecuta una acción sobre el archivo.

## Identidad, rutas y TOCTOU

Los rename de extensión y movimientos de organización soportados mantienen
abierto el archivo fuente y el directorio destino, verifican volumen/FileId y
ejecutan un rename relativo al handle del padre con semántica *no-replace*. La
identidad esperada se persiste en `applying` inmediatamente antes de la llamada
nativa y un recibo posterior confirma `applied`.

El contrato se limita a Windows, volumen NTFS local, mismo volumen, archivo
regular, un único hard link, fuente sin reparse y destino ausente. El framework
se abstiene ante UNC, filesystem distinto de NTFS, symlink/junction/reparse,
directorio, hard links múltiples, movimiento cross-volume o garantía nativa no
disponible. No cae a `Path.rename`, `MoveFileW` por ruta ni reemplazo.

Linux no intenta ejecutar ese contrato NTFS: cualquier `--apply` o
`--organization-apply` se rechaza antes de crear estado con código `2` y razón
`linux_mutation_backend_unavailable`. Para observación, la identidad portable
usa `st_dev`/`st_ino`; cuando no existe nacimiento real persiste
`birthtime_ns=-1` y nunca disfraza `ctime` como nacimiento.

Esto reduce la sustitución entre validación y syscall dentro del subconjunto
soportado; no vuelve atómica la posterior escritura SQLite. Un fallo después de
la llamada queda `recovery_required`, con evidencia append-only, y se concilia
de sólo lectura. Al reiniciar, `started` abandonado antes de la frontera se
clasifica como fallo sin efecto intentado; sólo `applying` conserva
incertidumbre. Los recibos de Papelera no confirman una acción distinta: deben
ligar origen y destino registrados, aunque Papelera no dispone del enlace por
handle requerido y por ello se abstiene siempre en modo apply.

Consecuencias operativas:

- evite raíces que otros programas estén reescribiendo activamente;
- no aplique durante sincronizaciones, descargas o despliegues sobre el mismo
  árbol;
- una discrepancia o plataforma no soportada causa abstención, nunca fallback;
- después de una caída, no repita una acción incierta; siga
  [RECOVERY.md](RECOVERY.md).

## Enlaces, junctions, reparses y hard links

- La raíz y los elementos se validan para rechazar symlinks, junctions y puntos
  de reanálisis en los recorridos protegidos.
- No confíe únicamente en una ruta canónica calculada mucho antes de la syscall.
- La enumeración MFT representa registros de archivo y no necesariamente todos
  los nombres de un archivo con múltiples hard links. No interprete el
  inventario auxiliar como catálogo completo de enlaces duros.
- Por esa razón, una mutación autorizada exige exactamente un hard link; un
  contador mayor provoca abstención.
- No use una raíz UNC o un filesystem no NTFS como si ofreciera identidad/USN
  equivalentes a un volumen NTFS local.

## Archivos protegidos y alcance

El flujo integrado excluye o protege el directorio de estado, árboles de sistema
configurados, el subárbol `AppData` del perfil efectivo, `.codex`, atributos
`SYSTEM`/`HIDDEN` y colmenas del perfil descritas por la implementación. No se
excluye por nombre cualquier directorio arbitrario llamado `AppData`. Estas
protecciones reducen exposición, pero no sustituyen la validación exacta de la
raíz.

No seleccione como raíz una carpeta de sistema, el propio estado, una caché de
modelos o una ubicación cuya propiedad no esté clara.

## Formatos maliciosos y límites

Los extractores aplican límites de tamaño, texto, miembros ZIP, expansión,
píxeles, páginas, duración, segmentos, timeouts y memoria según la ruta. PDF,
imagen y audio usan procesos supervisados en partes críticas; DOCX/Office
aplican lectura acotada de contenedores. Archive valida cada nivel antes de
descomprimir, conserva la cadena virtual y envía PDF/imágenes internas a un
worker OCR aislado. La ruta `text` exige evidencia imprimible/RFC 5322/CFB y
extrae DOC/XLS/PPT en un proceso aislado con un único backend fijado; cuando el
backend es LibreOffice usa un perfil temporal. Nunca carga macros ni ejecuta el
documento original.

Estos controles limitan impacto, pero no constituyen aislamiento de seguridad
completo. Mantenga actualizadas las dependencias compatibles y no desactive
límites para procesar un archivo sospechoso sobre el corpus vivo. Reproduzca en
un fixture no confidencial y aislado.

La ruta `code` analiza texto, AST y estructura sin ejecutar el código observado.
El analizador Rust vigente es léxico; Cargo y Clippy no se ejecutan como parte
del contrato. Los resultados estructurales, diagnósticos y relaciones conservan
su procedencia y límites, pero son evidencia de contenido, no conclusiones sobre
la calidad del repositorio ni permisos de mutación.

## Herramientas externas

NeoCortex puede usar Tesseract, FFprobe/FFmpeg, qpdf, LibreOffice y extractores
Office heredados cuando una ruta de contenido los necesita. Cada proceso recibe
rutas absolutas verificadas, límites de entrada/salida, memoria y tiempo; una
herramienta ausente se informa sin ejecutar un sustituto no confiable.

Los validadores del desarrollo no son dependencias del runtime: Ruff, Pyright,
Mypy, Semgrep, Coverage, Vulture, Grimp, Complexipy, pip-audit y Cosmic Ray se
invocan sólo de forma individual y explícita durante el mantenimiento, nunca a
través de una capacidad productiva de NeoCortex.

## Modelos y red

`models prepare` es la frontera integrada explícita de adquisición secuencial;
`models status` sólo inspecciona archivos locales y metadata instalada.
`--semantic-prepare-models` conserva la frontera específica de Semantic. En
Linux, audio usa Whisper CPU/int8 y local-only por defecto; en Windows, la
primera carga puede descargar pesos salvo `--audio-local-models-only`.

NeoCortex no accede a la red para validar su propio código. Una auditoría de
dependencias sólo procede por solicitud expresa y con el mínimo de nombres y
versiones necesario; no instala paquetes ni usa `--fix`.

Antes de permitir red:

- confirme modelo, backend, caché y espacio requerido;
- use una fuente y licencia aceptadas;
- conserve versión/proveniencia;
- no asuma que un nombre de modelo garantiza bytes inmutables;
- no incluya documentos, OCR o consultas confidenciales en servicios remotos.

El pipeline descrito usa inferencia local; incorporar un backend remoto requiere
otra revisión de privacidad y seguridad.

## SQLite y estado importado

- Abra consultas administrativas en modo de sólo lectura.
- No concatene filtros no confiables en SQL propio.
- No abra una base desconocida con una versión que vaya a migrarla antes de
  respaldarla y validarla.
- No elimine WAL/SHM ni altere `user_version`.
- Para retirar una base completa use `Neocortex databases purge`; nunca elimine
  manualmente sus archivos ni sus sidecars.
- `integrity_check` y `foreign_key_check` no prueban que la evidencia pertenezca
  al mismo corpus o generación.
- Una base incompatible debe preservarse y provocar abstención.

## Privilegios

La lectura del volumen NTFS/USN puede requerir elevación, pero es un acelerador
opcional: la corrida cotidiana debe degradar al recorrido portable. No eleve el
framework sólo para obtener USN. Si una prueba diagnóstica expresamente necesita
esa frontera, limite la raíz y confirme los argumentos antes de aceptar UAC; un
proceso elevado amplía el impacto de cualquier parser o ruta mal seleccionada.

No ejecute de forma elevada doctors, ayuda, versión o búsquedas que no lo
requieran. No instale un servicio privilegiado para operar el watcher: el
watcher soportado es foreground.

## Evidencia y datos confidenciales

Los estados pueden contener rutas, fragmentos, OCR, diagnósticos, nombres de
proyecto y evidencia derivada. Proteja el directorio de estado y los backups con
los mismos controles que el corpus.

La salida humana y JSON de Knowledge puede reproducir rutas, locators, snippets
y relaciones entre fuentes. Trátela como material sensible: redáctela antes de
compartirla, no la publique como telemetría y no asuma que `--knowledge-json`
anonimiza el contenido.

Al reportar un defecto:

- incluya versión, comando, código de salida, `run_id` y error;
- minimice rutas y snippets;
- use fixtures sintéticos cuando sean suficientes;
- no adjunte bases, modelos o documentos reales sin autorización;
- preserve la evidencia original sin publicarla automáticamente.

## Respuesta ante incidente o efecto inesperado

1. Solicite cancelación cooperativa.
2. No ejecute otra mutación ni un “cleanup”.
3. Identifique exactamente los procesos propios aún activos.
4. Preserve estado, WAL/SHM, salida y filesystem observado.
5. Cree un backup consistente si las bases todavía abren.
6. Trate acciones `applying`/`recovery_required` como inciertas y use
   `--action-recovery-status`; una fila legacy sin identidad puede resultar
   `impossible_to_check`.
7. Si necesita evidencia durable, use `--action-recovery-record` con actor y
   confirmación. El evento nunca autoriza la recuperación.
8. Una base framework con versión futura o metadata no canónica se rechaza; no
   fuerce el lector ni edite `schema_version`.
9. Siga [RECOVERY.md](RECOVERY.md) antes de restaurar o reintentar.

## Riesgos residuales que deben permanecer visibles

- Las garantías de identidad sólo cubren el subconjunto NTFS descrito; fuera de
  él la operación se abstiene y la Papelera permanece deshabilitada.
- El status de conciliación no modifica estado; record conserva una observación
  pero no persiste decisión/autorización, no existen todavía
  `decide/authorize/recover/verify` productivos y ninguna clasificación autoriza
  una nueva mutación. La conciliación de planes de organización sigue siendo
  manual.
- Diferencias entre evidencia persistida y estado físico actual.
- Crecimiento de ciertos historiales/generaciones sin una política global
  completa de retención.
- Resultados probabilísticos no calibrados.
- Riesgo inherente de procesar formatos y herramientas nativas no confiables.

Que un doctor, test o `pip check` termine correctamente no elimina estos límites.
