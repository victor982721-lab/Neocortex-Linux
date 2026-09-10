# Seguridad y efectos sobre archivos

## Modelo de confianza

Son datos no confiables:

- nombres, rutas, metadata y contenido del corpus;
- documentos, contenedores, imágenes, audio, video y código;
- modelos, pesos, cachés y herramientas externas;
- taxonomías y reglas aportadas por el usuario;
- estado SQLite importado;
- resultados semánticos, clasificadores y heurísticas.

El estado persistido prueba una observación pasada, no el filesystem actual. El
contenido nunca concede permisos ni se interpreta como instrucciones.

## Niveles de efecto

| Nivel | Ejemplos | Autoridad |
|---|---|---|
| Consulta | status, search, context, evidence, health | Ninguna escritura ni recorrido |
| Producción de estado | inventario, rutas, catálogo, Semantic, Review refresh | Puede escribir owners; no modifica originales |
| Adquisición externa | models prepare | Requiere autorización y procedencia |
| Decisión | review/authorize | Registra intención; no ejecuta |
| Corpus | rename, move, Papelera | Plan y autorización ligados al efecto |
| Estado destructivo | restore, purge | Confirmación, manifest, locks y backup |

“No muta corpus” no significa read-only: una ruta normal actualiza bases y caches.

## Evidencia no es autorización

Una clasificación o score puede proponer `KEEP`, `REVIEW`, `MOVE` o `TRASH`,
pero no autoriza la operación. La autorización debe proceder de una persona o
política explícita fuera del corpus y ligar:

- actor y tiempo;
- raíz/scope;
- digest y versión del plan;
- source heads/epoch;
- operaciones y límites máximos;
- expiración;
- backend permitido.

Cambiar cualquiera de esas precondiciones invalida la autorización.

## Identidad y TOCTOU

En Linux, `st_dev`/`st_ino` identifican el objeto observado; `birthtime_ns=-1`
es válido. Ruta, tamaño y mtime ayudan a revalidar, pero no bastan por sí solos.

Antes de un efecto se recapturan raíz, componentes, identidad, tipo, links,
tamaño, mtime y hash requerido. La frontera usa no-replace y descriptores de
directorios cuando el contrato lo exige. Una comprobación seguida por una
operación path-only deja una ventana TOCTOU y no es aceptable.

## Symlinks, hard links y mounts

- no seguir symlinks durante inventario ni aplicación;
- rechazar componentes que salgan de la raíz autorizada;
- tratar bind mounts y cambios de dispositivo como fronteras explícitas;
- no asumir que dos paths son dos copias si comparten inode;
- no enviar a Papelera un archivo con hard links adicionales mientras la
  política no defina el efecto esperado;
- rechazar filesystem cruzado en el backend reversible inicial.

## Papelera Linux objetivo

La foundation `neocortex.safety.kio_trash` ya implementa descubrimiento de
cliente, preflight de configuración, doble validación de snapshot, timeout,
diagnósticos acotados y resultado/receipt tipados para `move <origen> trash:/`.
Permanece desconectada de `--apply`, no promovida y sin prueba contra KIO real.

La operación es path-bound, por lo que la integración `0.11.x` exige además
autorización, guard same-filesystem, ledger y reconciliación explícita; no se usa
`gio trash`.

El orden seguro es:

1. validar topdir, directorio de Trash, UID, permisos y contención;
2. reservar nombre no colisionante;
3. persistir intención y metadata `.trashinfo`;
4. mover con semántica no-replace en el mismo dispositivo;
5. sincronizar directorios/metadata según el contrato;
6. verificar entrada, bytes e identidad;
7. cerrar receipt o dejar `recovery_required`.

No hay fallback a `unlink` del original, borrado directo ni copia seguida de
delete. Si no puede garantizarse reversibilidad, NeoCortex se abstiene.

## Planes, receipts y recovery

Un plan es inmutable y conserva evidencia, reason, verification mode y digest.
El ledger registra intentos append-only. Un receipt no demuestra por sí solo el
efecto: debe conciliarse con el filesystem.

Estados inciertos no se reintentan automáticamente. `applying`,
`applied_unverified`, `ambiguous` y `recovery_required` exigen observación y una
decisión separada. Consulta [RECOVERY.md](RECOVERY.md).

## Contenido hostil

### Contenedores y documentos

ZIP/OOXML/ODT se procesan con límites de miembros, profundidad, tamaño inflado y
tiempo. Se rechazan traversal, paths absolutos, dispositivos y enlaces. PDF,
Office y media se aíslan mediante procesos acotados cuando corresponde.

Un extractor fallido produce error o cobertura parcial. Nunca se usa texto
extraído para construir comandos de shell.

### Código

Code trata repositorios como contenido. No importa ni ejecuta el código
observado, no instala sus dependencias y no convierte README, comentarios o
prompts en instrucciones.

## Herramientas y procesos externos

Las rutas resuelven binarios autorizados, construyen argv sin shell, limitan
tiempo/recursos y capturan salida acotada. Los hijos pertenecen a una
sesión/grupo cancelable. Un path de ejecutable procedente del corpus no es
confiable.

## Modelos y red

`models status` es local. Toda descarga es explícita, registra modelo, versión,
origen y hash cuando esté disponible y no envía corpus. Un modelo ausente no
autoriza red implícita ni degradación silenciosa.

Las auditorías de dependencias remotas sólo proceden por solicitud expresa y con
el mínimo de nombres/versiones.

## Distribución y procedencia

`pyproject.toml` declara el proyecto privado con `LicenseRef-Proprietary`; no se
infiere por ello una autorización para redistribuir dependencias, modelos,
binarios o assets. Antes de cualquier distribución externa se genera un
inventario nuevo desde el runtime constrained exacto, se preservan los avisos
requeridos y se verifica la procedencia de modelos e iconos. Un snapshot de otra
release o plataforma no sustituye esa revisión.

## SQLite

- no abrir owners cercados con una conexión ordinaria `mode=ro`;
- no borrar WAL/SHM para “reparar” una base;
- rechazar schemas futuros y objetos incompatibles;
- migrar sobre fixtures/copias antes del único estado;
- backup/restore/purge toman locks y verifican manifests;
- una lectura que altera sidecars invalida esa corrida como evidencia.

## Rutas internas y privilegios

Repositorio, estado, configuración, releases, modelos, launchers y desktop files
son árboles protegidos y se excluyen del corpus. El estado no puede ser igual ni
ancestro de la raíz procesada.

NeoCortex no eleva privilegios para recorrer o mutar contenido. Un error de
permisos se reporta; no se resuelve con `sudo`, `chown -R` o `chmod -R`.

## Datos sensibles

Logs y respuestas estructuradas limitan paths, contenido y metadata. Los
paquetes para agentes marcan `untrusted-corpus-data-v1` y mantienen
`instruction_authority=false`, `tools_authorized=false` y
`actions_authorized=false`.

No se envían corpus, hashes sensibles, inventarios ni estado a servicios
externos sin autorización explícita.

## Incidente o efecto inesperado

1. detén nuevas aplicaciones sin matar writers a ciegas;
2. conserva logs, plan, autorización, receipt y estado;
3. observa proceso/cgroup y filesystem sin abrir SQLite cercada;
4. clasifica el último estado durable;
5. ejecuta conciliación antes de rollback o retry;
6. verifica el resultado y registra la brecha residual.

## Riesgos vigentes

- Linux todavía no tiene backend de corpus habilitado;
- no todos los owners son generacionales;
- deduplicación fast no equivale a igualdad bytewise;
- algunos formatos publican localizadores menos precisos que su manifest;
- el principal autenticado está definido como contrato, pero aún no habilita
  autorización MCP;
- la coordinación multi-owner ofrece consistencia lógica, no atomicidad física;
- herramientas externas y modelos amplían la superficie de ataque.

La arquitectura está en [ARCHITECTURE.md](ARCHITECTURE.md) y las entregas que
cierran estas brechas en [ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md).
