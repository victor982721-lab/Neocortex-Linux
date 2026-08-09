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
| Consulta | `--help`, `--version`, `--status`, `--action-recovery-status`, `--retention-status`, `--knowledge-status`, `--knowledge-search`, `--knowledge-context`, búsquedas, previews y doctors | No debe recorrer ni modificar el corpus; puede fallar si falta estado. SQLite read-only puede participar en WAL/SHM. |
| Estado sin mutación del corpus | Corrida sin `--apply`, `--self-analysis`, `--semantic-index`, `--semantic-classify`, `--catalog-documents`, `--organization-plan`, `--review-record`, `--action-recovery-record` | Lee contenido o cachés y escribe bases, evidencia o planes. |
| Descarga/carga externa | `models prepare`; `--semantic-prepare-models`; primera transcripción Windows sin `--audio-local-models-only` | Puede adquirir modelos y ampliar cachés. `models status` es local y read-only. |
| Mutación de archivos | Corrida integrada con `--apply`; `--organization-apply` | En Windows puede renombrar extensiones o mover documentos sólo bajo el contrato NTFS ligado a handles; en Linux se rechaza antes de crear estado. |

“No destructivo” significa que una corrida sin autorización no debe mutar los
originales. No significa que sea de sólo lectura: el estado y las cachés sí se
actualizan.

Las operaciones Knowledge pertenecen específicamente al nivel **Consulta**:
abren sólo estado existente y no crean ni actualizan bases, cachés, planes o
archivos del corpus. Un propietario con esquema futuro, incompatible o corrupto
produce abstención explícita; no se migra, repara ni reconstruye durante la
consulta.

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
del corpus. Dedup v9 conserva la firma cruda de exclusión y Framework v20 liga
la firma efectiva que también incorpora las rutas internas.

En Linux, la política protege además estado/configuración/datos XDG, releases,
modelos, runtimes, el launcher `~/.local/share/Neocortex/bin/Neocortex`, el alias
`~/.local/bin/Neocortex` y los archivos `.desktop`; el inventario nunca sigue
symlinks.

### Autoanálisis de código

`--self-analysis` pertenece al nivel **Estado sin mutación del corpus**. Exige
una raíz y un estado explícitos cuyos árboles sean disjuntos, captura y vuelve
a verificar sus identidades, fuerza `analyze_only` y sólo admite la ruta
`code` desde el inventario. Rechaza `--apply`, route-only/resume, rutas MIME,
catálogo, organización y generated/vendored. El contenido se analiza como
evidencia no confiable y no se ejecuta en `protected` ni `trusted-static`. El
perfil predeterminado `protected`
integra sólo Ruff basic: recibe el manifest de archivos Python ya publicados,
abre copias temporales verificadas, ignora la configuración del proyecto y usa
entorno/cwd controlados, `--no-cache` y ninguna capacidad de fix.

`trusted-static` es una frontera explícita para una raíz que Victor declara
confiable. Conserva Ruff basic y permite leer el `pyproject.toml` versionado para
Ruff proyecto, Mypy, Pyright, Deptry y el inventario de dependencias. El
adaptador limita Ruff trusted a
`E4,E7,E9,F,B,C4,PIE,RUF` y omite `I,PT,SIM,UP`; además rechaza mecanismos que
amplían la confianza: Ruff `extend`, plugins o `mypy_path`, y rutas externas de
Pyright. Doce de los trece proveedores estáticos declaran `uses_network=false`;
pip-audit declara red porque consulta PyPI para crear un snapshot fechado y su
replay exacto vigente reutiliza ese snapshot sin otra consulta. Todos declaran
`imports_content=false`, `executes_content=false`, `authority=advisory` y
`mutation_authority=false`. Semgrep usa sólo tres reglas empaquetadas, desactiva
autofix y excluye sus fixtures propios del gate del proyecto. Deptry no instala
ni retira dependencias; el inventario sólo verifica metadata y `RECORD`. Esto
incluye `vulture-unused-static`: su confidence no prueba ausencia de uso y ni
siquiera el consenso alto tiene autoridad de borrado. Ningún gate de supply
chain autoriza actualizar, desinstalar o modificar paquetes.
`git-history-local` sólo lee objetos del repositorio local verificado bajo
límites de commits, archivos, relaciones, tiempo y salida; no usa red, hooks,
`textconv` ni drivers de diff externos, y sus señales son advisory. Si la
protección de ownership de Git requiere `safe.directory`, el proveedor autoriza
únicamente la raíz exacta ya resuelta mediante `-c` en cada invocación; nunca
usa comodines ni modifica configuración global, local o del sistema.

`trusted-deep` es la única frontera que ejecuta contenido. La CLI exige la
identidad física exacta de `C:\Users\Victor\Neocortex\Repository`, estado
disjunto y límites efectivos acotados; cualquier otra raíz se rechaza antes de
crear el run. Pytest carga plugins/código confiable y puede ejecutar comportamiento de
red porque el proveedor no impone un sandbox de red; el descriptor lo declara
en vez de prometer aislamiento inexistente. Coverage observa sólo el proceso
principal. Ningún resultado adquiere autoridad de mutación y el perfil nunca es
predeterminado. `cosmic-ray-focal-mutation/v1` se habilita sólo en esta frontera
con target y tests declarados: copia exactamente el snapshot a staging, muta
únicamente esa copia, verifica hashes de fuentes antes y después y aplica
límites por mutante y por corrida. El corte aceptado se limitó a `20` mutantes.
El proveedor es `authority=advisory`, conserva `mutation_authority=false` y
declara `uses_network=true` porque los tests podrían usar red; no es una
autorización para modificar la raíz canónica.

La finalización no confía únicamente en la CLI: Framework v20 impide enlazar
acciones a un run protegido, Dedup v9 exige el scan ligado a su firma y Code v4
conserva el run analítico. Los owners de mutación reciben
`CorpusMutationGuard` y el commit exige ceros durables en candidatos, acciones
y organización. Una identidad cambiada, un árbol intersectante o una frontera
indemostrable propaga `ProtectedAnalysisRootError`; no activa un fallback.

El diagnóstico asociado, `--code-status --code-json`, sí es consulta read-only
estricta. A diferencia de lectores que pueden participar en WAL, exige
instantáneas SQLite immutable sin `-wal`, `-shm` ni `-journal` y fences
estables antes/después. Cualquier sidecar —incluso vacío o desacoplado— o cerca
inestable en code, framework o Dedup provoca abstención total con código `2`.
No se emite evidencia parcial ni se crean sidecars, migraciones o reparaciones. Consulte
[SELF_ANALYSIS.md](SELF_ANALYSIS.md).

## Autorizaciones de mutación

Existen dos superficies explícitas:

1. `--apply` autoriza las acciones de una corrida integrada.
2. `--organization-apply` consume planes de organización ya persistidos sin
   requerir además `--apply`.

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
convierte DOC/XLS/PPT en un proceso aislado con perfil LibreOffice temporal;
nunca carga macros ni ejecuta el documento original.

Estos controles limitan impacto, pero no constituyen aislamiento de seguridad
completo. Mantenga actualizadas las dependencias compatibles y no desactive
límites para procesar un archivo sospechoso sobre el corpus vivo. Reproduzca en
un fixture no confidencial y aislado.

La ruta `code` analiza texto/AST/estructura y no ejecuta el código observado. El
analizador Rust vigente es léxico; Cargo y Clippy no se ejecutan como parte del
contrato actual. Los trece proveedores `trusted-static` sólo participan en
`--self-analysis`: no descubren archivos fuera del inventario, no importan
módulos del proyecto y no aplican fixes. El perfil protected no carga
configuración del corpus; trusted-static carga sólo las políticas y metadata
declaradas arriba. El flujo crea copias temporales verificadas
bajo el estado explícito y disjunto, nunca bajo la raíz observada aunque
`TEMP/TMP` apunten allí. NeoCortex cierra los handles y vuelve a comprobar los
inputs originales después de cada proceso.

Mypy se invoca con el intérprete aislado y una caché efímera. Pyright `1.1.411`
se ejecuta mediante Node desde un paquete npm owned junto al runtime; no usa
`npx`, no descarga herramientas durante la corrida y no ejecuta scripts del
proyecto. Ningún otro linter, compilador, plugin o script puede incorporarse sin
una política explícita de confianza, timeout, recursos, publicación y
proveniencia.

## Herramientas externas

NeoCortex puede localizar o usar Tesseract, FFprobe/FFmpeg, qpdf, LibreOffice y
los fallbacks `catdoc`/`xls2csv`/`catppt`.

- Use rutas absolutas verificadas cuando se proporcionen overrides.
- No sustituya un binario mientras exista una corrida activa.
- Registre versión y origen del ejecutable.
- qpdf es un fallback opcional; su ausencia no debe provocar la ejecución de un
  binario alternativo no confiable.
- LibreOffice y los extractores Office heredados sólo reciben una copia
  temporal acotada; no se habilitan macros ni se reutiliza el perfil personal.
- Ruff y Mypy son dependencias base del runtime y se invocan con el mismo
  intérprete (`python -I -m ...`), nunca mediante ejecutables encontrados en
  `PATH`.
- La instalación canónica de Pyright requiere Node y conserva el paquete npm
  aislado junto al runtime; cualquier resolución alternativa queda incorporada
  a la firma de entorno y no concede autoridad adicional.
- Semgrep, Deptry y pip-audit se resuelven desde el mismo runtime Python. Las
  reglas Semgrep están empaquetadas y autofix permanece deshabilitado; Deptry es
  read-only; pip-audit crea un snapshot sin `--fix` y declara su red de forma
  explícita.
- No interpole rutas o consultas de usuario dentro de comandos de shell propios.

Los temporales de recuperación deben permanecer en el directorio temporal del
sistema, tener límites y retirarse sólo después de cerrar procesos y handles.
Nunca modifique el original para “repararlo” durante extracción.

En Windows, la frontera `run_bounded_capture()` y los workers aislados crean el
hijo suspendido y lo asignan por su handle exacto a un Job Object con
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` antes de reanudarlo. Timeout, desborde de
stdout/stderr, cancelación o excepción terminan el Job, esperan al proceso
directo y cierran pipes y handles. Esta contención cubre los descendientes de
esas fronteras; los proveedores estáticos añaden límites de memoria, cwd y entorno
explícitos. Esto no autoriza matar procesos por nombre ni debe atribuirse a un
callsite que no use esos supervisores.

En POSIX, esos supervisores crean una sesión/grupo propio, terminan el árbol con
`SIGTERM`/`SIGKILL` y aplican memoria con `RLIMIT_AS` o `/usr/bin/prlimit`. Si se
solicita un límite que no puede imponerse, fallan cerrado antes de ejecutar.

## Modelos y red

`models prepare` es la frontera integrada explícita de adquisición secuencial;
`models status` sólo inspecciona archivos locales y metadata instalada.
`--semantic-prepare-models` conserva la frontera específica de Semantic. En
Linux, audio usa Whisper CPU/int8 y local-only por defecto; en Windows, la
primera carga puede descargar pesos salvo `--audio-local-models-only`.

El perfil `trusted-static` también puede acceder a la red únicamente mediante
pip-audit para capturar el snapshot de vulnerabilidades. Esa consulta no
descarga ni instala paquetes; el replay de un snapshot vigente no usa red. La
fecha de captura y la frescura deben permanecer visibles y un snapshot vencido
obliga a abstener su gate.

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
