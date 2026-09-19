# Runtime y persistencia

## Ownership

`neocortex/runtime` coordina configuración, recursos, procesos y lifecycle;
`neocortex/persistence` define el kernel de lectura/escritura, publicación y
recuperación de owners. Los schemas específicos siguen con su owner, no se
centralizan por esta organización documental. Consulta [Architecture](../ARCHITECTURE.md#persistencia),
[Persistence](../PERSISTENCE.md) y [Operations](../OPERATIONS.md#recursos-y-progreso).

## Fronteras

- En Linux la identidad usa `st_dev`/`st_ino`; cuando no existe birthtime real
  se conserva `birthtime_ns=-1`, nunca se convierte `ctime` en nacimiento.
- Una generación parcial no es vigente. Schema futuro, corrupción, identidad
  incierta o cambio concurrente abortan la frontera que no puede demostrar seguridad.
- `mode=ro` ordinario puede alterar WAL/SHM. Durante una corrida cercada observa
  sólo stream, transcript, proceso/cgroup del host y archivos de progreso previstos;
  una API sólo procede si garantiza compatibilidad con esos owners activos.
- Después del estado terminal, selecciona `SQLiteReadSession` o snapshot según
  su contrato y presupuesto. No borres sidecars ni relajes fences para validar
  una corrida interferida; conserva evidencia y repite sólo la corrida afectada.
- Un `ps` en sandbox no demuestra quiescencia del host. Mientras unidad, wrapper
  o cgroup estén activos no modifiques SHA, checkout ni entradas cercadas.
- Concurrencia de agentes, procesos y writers son límites distintos. Un único
  escritor por owner/archivo; CPU, memoria y temporales se dimensionan con
  cgroups, afinidad y presión observables, no sólo `os.cpu_count()`.
- Procesar contenido real requiere alcance, preflight y límites. Un piloto usa
  fixtures o 20–50 elementos autorizados y 10–15 minutos como máximo; un límite
  de PDF no limita el inventario ni garantiza un deadline global de `--all`.
  No escales sin un límite duro demostrado; si falta, impleméntalo primero.

## Validación proporcional

Selecciona pruebas existentes de `test_cgroup_resources`, `test_global_resources`,
`test_run_budget`, `test_run_budget_terminal_accounting`, `test_sqlite_immutable`
o `test_sqlite_*` según el cambio. Usa HOME/XDG y owners de fixtures aislados,
nunca SQLite productivas para comprobar una edición de desarrollo. Si inicias
una unidad con logs append, crea y verifica primero su directorio de logs.

## Higiene, actividades y mantenimiento

`HygieneManager` conserva su preview sin efectos. El coordinador
`runtime.orchestration.maintenance` ejecuta los planes completos de los owners
seleccionados; `MaintenanceRequest` transporta presupuestos, selección y
cancelación, mientras `ScopeAuthority` identifica la autorización por ámbito.
El digest identifica el plan y nunca sustituye esa autorización. Los adapters
de scratch, retención terminal y adopción histórica mantienen sus propios
guards, intenciones y recibos. Un owner ausente, una dependencia activa, un
ciclo o un fallo al publicar evidencia obligatoria queda en el resultado
global como mantenimiento parcial. El estado del trabajo principal se informa
por separado. La composición normal sólo incluye ámbitos configurados bajo
state; no añade Corpus, HOME, cachés ajenas ni una operación de factory reset.

`AgentActivity.prepare(..., workspace_root=...)` permite una raíz privada
exacta, por ejemplo `Auditorias/una-auditoria/work`. La reanudación localiza ese
workspace por el registro del productor. Un hermano con el mismo basename
permanece ajeno. `run` fija `TMPDIR`, `TMP` y `TEMP` para el hijo, y publica el
mismo contexto como `NEOCORTEX_ACTIVITY_WORKSPACE` para adaptadores con `dir`.
`temporary_directory_contract` distingue herramientas declaradas compatibles
de cobertura incompleta; esas variables no contienen herramientas que las
ignoren. La publicación verifica y sincroniza la entrega final antes de
retirar su dependencia del scratch. Las entregas siguen siendo canonical y
no descartables.

La captura limita cada stream en bytes durante la lectura. El grupo POSIX
ofrece cierre acotado y declara cobertura incompleta para descendientes
separados con `setsid`. Un caller con un subtree cgroup v2 ya delegado puede
pasar `delegated_cgroup_root`: el launcher se detiene antes de ejecutar el argv,
el supervisor registra PID/start ticks, asigna y verifica el cgroup y libera
la barrera. El recibo conserva boot ID, identidad física, montaje y población;
la cancelación usa `cgroup.kill` y exige `populated=0`. No se crea ni configura
una delegación global. Una reclamación de proceso sin quiescencia verificada
queda `cleanup_unverified`: `close`, `retire` y `reconcile("release")` conservan
el workspace y exigen la prueba del scope completo, incluso al reanudar y
frente a un manifiesto antiguo que sólo declaraba el líder `exited`. No se
libera por TTL ni por un booleano de aprobación.

El perfil de payload ordinario es `strict`. El perfil `fixture_posix_v1`
requiere `issue_fixture_grant` del owner, vinculado a actividad y creación;
metadata libre no lo habilita. `AgentActivity.prepare` expone ese circuito con
`payload_profile`, `fixture_creation_grant_id` y `fixture_authorized`. Observación,
sello, registro y retirada consumen la misma política verificada. Los enlaces
y FIFO se representan sin abrir sus destinos; nunca se cambia el modo de un
fichero regular compartido. La preparación explícita de permisos usa FDs y
registra/restaura los modos de los directorios sobrevivientes. La lectura
ordinaria no repara permisos. Sockets, dispositivos, montajes, dueño ajeno y
sustitución permanecen bloqueados.

Los nombres POSIX se transportan mediante `PathIdentity`: bytes base64 y
presentación escapada separadas. Los campos `posix_path_identity` complementan
las identidades físicas `st_dev/st_ino/birthtime`; la presentación no se usa
como ruta. Los manifests anidados, temporales `.manifest.json.*` y cualquier
payload distinto del manifest raíz participan en el sello. La conciliación
de un sello antiguo requiere una acción explícita del owner y conserva la
evidencia anterior.

`ScratchManager.observe_workspace_batch` persiste generaciones, límites,
frontera de directorios, cookies, identidades y hashes por lote. Cancelar y
reanudar no convierte una observación parcial en completa. Estos checkpoints
son evidencia de observación y no autorizan efectos. `apply(plan=...)` y
`verify(plan)` conservan la selección original; un workspace completado después
del plan no entra en el efecto. La primitiva mantiene su cota de 100001
miembros por árbol; los lotes de workspaces tienen recibos independientes.
Un árbol monolítico que supera esa cota se conserva con explicación.

La adopción histórica tiene un circuito público separado dentro del mismo
owner: `plan_selected` → `prepare_adoption` → `approve_adoption` →
`apply_selected`. `HistoricalSelection` contiene la entrada exacta y las
referencias a evidencia del productor. Un manifest de scratch actual se
contrasta con el registro privado y su sello. Un histórico sin ese manifest
necesita procedencia registrada y una copia durable protegida cuyo contenido
se verifica. La aprobación se guarda autenticada bajo state y se liga a
identidad, ámbito y selección. Un campo legacy `approved: true`, el UID, la
edad, el nombre o un hash autoconsistente no autorizan la retirada. La entrada
legacy conserva descubrimiento y devuelve `private_adoption_required` al
solicitar efectos. Una raíz sticky sólo aporta contexto para la entrada
seleccionada; no concede autoridad sobre sus vecinos. El replay concilia un
recibo preparado cuando el efecto ya ocurrió sin repetir unlink.

Los productores existentes usan el mismo owner de scratch y registro:

| Productor y call site | Contexto de trabajo | Evidencia focal |
| --- | --- | --- |
| Archive `materialization._registered_scratch_workspace` | Staging registrado antes de publicar materializaciones | `test_archive_registered_scratch.py` |
| PDF `pdf_isolation._registered_pdf_recovery_workspace` | Recuperación aislada del extractor | `test_pdf_registered_scratch.py` |
| Video `frames._registered_video_scratch_workspace` | Extracción de frames | `test_video_registered_scratch.py` |
| Semantic `semantic_plan_scratch._registered_scratch_workspace` | Plan SQLite temporal del owner | `test_semantic_registered_scratch.py` |
| Agentes `AgentActivity.prepare/run/publish/close` | Workspace privado y entrega canonical independiente | `test_agent_activity_api.py`, `test_activity_fixture_policy.py` |

Las variantes autónomas que no reciben state mantienen su contrato temporal
local; este inventario no afirma que todo `tempfile` del sistema haya migrado.
Modelos, herramientas, fuentes, instalaciones y cachés externas se registran
como external/no disposable cuando se incluyen en una observación autorizada;
sólo su owner puede aplicar su lifecycle. Los releases mantienen su transacción
propia y current/rollback. `TerminalRetentionOwner` adapta
`TerminalRetentionPolicy` por propósito y sólo retira tombstones confirmados,
sin obligaciones de recuperación, replay, pins o grants. Las cuotas no otorgan
por sí mismas permiso de retirada.

La contabilidad distingue bytes aparentes, bloques asignados observados,
bytes de entradas retiradas y variación observada de espacio libre. Los
hardlinks se deduplican por inode dentro de la observación. Los bloques
compartidos y reflinks impiden afirmar exclusividad: el campo
`exclusive_reclaimable_bytes` queda nulo. Un error de lectura conserva cobertura
parcial y bytes desconocidos. La variación de espacio libre se informa como
observación concurrente, sin prometer que coincida con los bytes retirados.

El factory reset es una operación del owner de persistencia, no una variante
superpuesta de scratch, adopción histórica o retención. Sólo puede retirar el
estado operacional que pertenece a la raíz de estado cercada; no consume claims
de retirada ni crea compensaciones o receipts adicionales. Una ruta externa o
un montaje ajeno permanece fuera de alcance y una cobertura incompleta produce
un error con conteos parciales.
