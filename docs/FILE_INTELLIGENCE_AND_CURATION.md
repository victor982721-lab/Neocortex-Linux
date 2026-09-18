# File Intelligence & Curation

## Visión

NeoCortex debe permitir que Víctor pase de “tengo una carpeta caótica” a
“entiendo qué contiene, revisé un plan, autoricé efectos concretos y verifiqué el
resultado” sin crear un script nuevo para cada caso.

El producto sirve a dos consumidores con los mismos contratos:

- una persona que usa CLI o GUI;
- un agente local que necesita evidencia paginada, citable y accionable.

El agente no recibe más autoridad que la persona. El contenido del corpus, una
clasificación, una similitud o una recomendación nunca autorizan efectos.
La falta de autoridad para mutar no debe impedir que NeoCortex observe, relacione,
explique la incertidumbre y prepare una propuesta útil para revisión.

## Etiquetas de estado

- **CURRENT:** frontera operativa y de seguridad vigente; no acredita el SHA de
  una instalación.
- **IMPLEMENTED:** código y pruebas presentes en el checkout; la disponibilidad
  en el launcher se comprueba contra su manifest y su interfaz pública.
- **TARGET:** contrato futuro que todavía no existe como capacidad pública.

## Recorrido del producto

```text
descubrir → identificar → comprender → relacionar → proponer
          → revisar → autorizar → aplicar → verificar → conciliar
```

Cada etapa produce un artefacto durable o una abstención explicable:

1. **Descubrir:** enumerar con límites, progreso y checkpoint.
2. **Identificar:** conservar identidad física, revisión de contenido y linaje.
3. **Comprender:** detectar tipo real, extraer estructura y representar
   procedencia, confianza e incertidumbre.
4. **Relacionar:** separar duplicado exacto, versión, similitud y pertenencia.
5. **Proponer:** crear un plan inmutable y paginado con razones y cobertura.
6. **Revisar:** conservar decisiones humanas sin convertirlas en efectos.
7. **Autorizar:** ligar actor, alcance, caducidad, límites y digest del plan.
8. **Aplicar:** ejecutar sólo operaciones soportadas y revalidar junto al efecto.
9. **Verificar:** demostrar origen/destino/Papelera, identidad, bytes y conteos.
10. **Conciliar:** ante caída o ambigüedad, observar antes de reintentar.

**IMPLEMENTED** alcanza `scan → plan → verify → review → decide → authorize` y
el consumidor grant-bound de 0.11 sobre backends explícitamente inyectados:
scan y plan consultan publicaciones acotadas, verify comprueba evidencia física
actual sin mutarla, review publica tareas advisory, decide registra una decisión
humana y authorize emite un grant durable separado. El núcleo
`apply → verify → reconcile` ya puede ejecutar fixtures contenidos, sin que una
decisión ReviewTask ni la existencia del grant demuestren por sí solas un efecto
físico.

## Evidencia y autoridad

Una observación debe indicar productor, versión, fecha, identidad, cobertura y
localizador. Las afirmaciones se tipan como:

- hecho estructural o extraído;
- inferencia con modelo/regla y confianza;
- decisión humana;
- autorización explícita;
- efecto observado;
- incertidumbre o ausencia de cobertura.

Los scores de texto e imagen permanecen en sus espacios y no se convierten en
certeza. Un duplicado destructivo exige verificación byte a byte; una huella
rápida sólo reduce candidatos. “Regenerable”, “de terceros” o “personal” requiere
evidencia de procedencia, no nombres o extensiones aislados.

## Estado actual

La fuente y la release `0.13.0-c6d3985f7a45-cp314-linux-x86_64` aportan
inventario, extracción multimodal, catálogos, búsqueda, Knowledge, Semantic,
Code como contenido, planes de duplicados/organización, Review, receipts y
recuperación parcial. La tranche post-0.13 está instalada; su procedencia es
`source_sha=c6d3985f7a45fc3120bd03e9561195674f2b8ac2`.

- **CURRENT:** `curate scan`, `curate plan`, `curation_scan`, `curation_plan` y
  `--curation-preview` consultan el plan local paginado sin escribir estado o
  corpus.
- **IMPLEMENTED:** `curate verify` y `curation_verify` comprueban identidad,
  hash completo y bytes de los grupos duplicados del plan actual, con límites y
  abstenciones tipadas, sin escribir estado, `ReviewTask`, grants o `file_actions`.
- **IMPLEMENTED:** `curate review` publica páginas con cobertura completa como
  `ReviewTask` advisory; `curate decide` añade por CAS una decisión humana
  `resolved` o `dismissed`. Sólo escriben Framework, nunca `file_actions`, corpus
  o sistemas externos, y `actions_authorized` permanece `false`.
- **IMPLEMENTED:** `curate authorize` emite un `AuthorizationGrant` inmutable en
  la extensión `curation_authorization_grants` de Framework. El grant liga plan,
  snapshot, tareas resueltas y heads con versión, evento, fingerprints y digest
  agregado, además de actor, acción, límites y expiración; declara
  `actions_authorized=true` y `physical_effect_applied=false`.
- **IMPLEMENTED (fixtures/inyección explícita):**
  `neocortex.curation.application` consume sólo grants con manifests de heads,
  raíz y efectos físicos, vuelve a validar plan, ReviewTasks, identidad, hash,
  límites y contención, registra `file_actions` por efecto y conserva receipts
  o `recovery_required`. `PosixRenameBackend` usa no-replace same-filesystem y
  `KioTrashBackend` exige evidencia estructurada de Papelera; ningún backend se
  selecciona automáticamente desde la CLI instalada.
- **IMPLEMENTED (recovery):** `reconcile_curation_actions` clasifica y registra
  observaciones bounded, append-only e idempotentes, sin reintentar efectos.
- **IMPLEMENTED post-0.13:** inventario/deduplicación v13 y catálogo v9 fijan
  digests, heads, fences y publicaciones inmutables; Knowledge v2 conserva
  localizadores/hydration por owner, y la lectura fenced de curación proyecta
  grants, intentos, receipts y recovery sin crear estado.

Las brechas principales son:

- la promoción del backend físico real y el restore de escritorio siguen fuera
  de la cohorte; el restore no-replace ya está disponible para receipts de
  fixtures con confirmación separada;
- varios formatos aún carecen de localizadores estructurales publicados y se
  mantienen como `reference_only`;
- igualdad, versión, procedencia, valor y disposición no tienen una proyección
  pública unificada;
- MCP mantiene fuera `authorize`, `apply`, `restore` y conciliación escrita:
  falta enlazar el principal autenticado con una sesión confiable;
- la CLI ordinaria no selecciona un backend físico; movimientos y restore se
  validan sólo con inyección explícita sobre fixtures;
- progreso, cancelación y replay no son uniformes en todos los productores.

## Decisión Linux para Papelera

La fuente ya prepara la foundation `neocortex.safety.kio_trash`, que descubre el
primer cliente disponible entre `kioclient6`, `kioclient5` y `kioclient`, valida
configuración y snapshot, ejecuta mediante un runner inyectable
`move <origen> trash:/` y exige verificación del caller antes de emitir receipt.
`--all --apply` y `--dedupe --apply` la consumen en Linux con lotes bounded;
cualquier canaria física queda contenida en fixtures privados.

La integración grant-bound ya revalida raíz, autorización, límites e identidad
y conserva ledger y recovery con backends inyectados. La canaria KIO instalada
demuestra permisos y locks efectivos, ausencia del origen, entrada esperada en
`trash:/`, metadata de restauración y receipt en un árbol privado; la
restauración visual en el escritorio real permanece separada.
Un timeout, error ambiguo, configuración KDE no escribible, symlink,
hard link no admitido, mount inseguro o cambio concurrente deja
`recovery_required` y no se reintenta a ciegas.

No se usará `gio trash`, `unlink`, borrado permanente ni una carpeta de
cuarentena como fallback. Las pruebas de la foundation y la canaria instalada
usan runner, resolver y verificador sobre fixtures contenidos, sin reusar la
configuración global.

## Interfaces CURRENT, IMPLEMENTED y TARGET

**CURRENT — consulta:**

```text
Neocortex curate scan [--limit N] [--cursor TOKEN] [--json]
Neocortex curate plan [--limit N] [--cursor TOKEN] [--json]
MCP: curation_plan, curation_scan
```

El `PLAN_ID` consumido por las operaciones siguientes es el `plan_digest`
`sha256:<64 hex>` devuelto por plan.

**IMPLEMENTED — verificación exacta:**

```text
Neocortex curate verify PLAN_ID [--item-id ITEM_ID ...] [--limit N]
  [--cursor TOKEN] [--json]
MCP: curation_verify
```

`curate verify` lee el plan publicado y los archivos regulares dentro de su
raíz, devuelve `source_heads`, `persisted_mode`, `observed_mode`, conteos y
razones de abstención, y mantiene `actions_authorized=false`.

**IMPLEMENTED — ReviewTask advisory:**

```text
Neocortex curate review PLAN_ID [--limit N] [--cursor TOKEN] [--json]
Neocortex curate decide PLAN_ID ITEM_ID --expected-event-id EVENT_ID
  --decision resolved|dismissed
  --decision-scope until-source-change|until-policy-change|permanent
  --actor ACTOR [--note NOTA] [--json]
MCP: curation_review, curation_decide
```

Review exige un plan completo y digest vigente, publica páginas reanudables e
idempotentes y devuelve `task_id`, estado y `current_event_id`. Decide vuelve a
probar el digest, usa `expected_event_id` como CAS y conserva replay idempotente
del mismo evento. Ambas superficies declaran `read_only=false` porque escriben
estado ReviewTask, pero `effects.corpus=none`, `effects.external=none` y
`actions_authorized=false`.

**IMPLEMENTED — grant durable, sin efecto físico:**

```text
Neocortex curate authorize PLAN_ID --item-id ITEM_ID [--item-id ITEM_ID ...]
  --action trash|move|rename --actor ACTOR --expires-ns NS
  --max-bytes BYTES [--authorization-key KEY] [--json]
API/SDK: curation_authorize_payload
MCP: no disponible
```

Authorize exige plan completo y vigente, entre 1 y 100 items con ReviewTask
`resolved`, snapshots concordantes, expiración futura y un presupuesto que cubra
los bytes conocidos. Trash de duplicados exige `verification_mode=full_hash`;
move/rename exige destino absoluto distinto del origen. El grant es append-only
e idempotente por su key, no crea `file_actions`, no llama KIO y no toca corpus o
sistemas externos.

No hay tool MCP de autorización: aceptar un `actor` aportado por un agente no
resuelve autenticación del principal humano.

**IMPLEMENTED — aplicación acotada, recovery y restore de fixtures:**

```text
Neocortex curate apply GRANT_ID --confirm-grant-id GRANT_ID [--json]
Neocortex curate reconcile --actor ACTOR --confirm-reconcile [--limit N]
Neocortex curate recovery status [--action-id ID] [--limit N]
Neocortex curate restore preview ACTION_ID [--json]
Neocortex curate restore apply ACTION_ID --confirm-action-id ACTION_ID \
  --confirmation TOKEN --actor ACTOR [--json]
API/SDK: curation_apply_payload, curation_reconcile_payload
        curation_recovery_status_payload, curation_restore_preview_payload,
        curation_restore_payload
MCP: no disponible para estas superficies; no apply, restore, authorize ni conciliación escrita
```

`curate apply` exige repetir exactamente el `GRANT_ID`. La CLI estándar no
inyecta un backend ni un run firmado, por lo que devuelve `backend_unavailable`
sin crear `file_actions`; los tests de producto suministran un backend POSIX o
KIO falso sobre una raíz temporal. El coordinador consume el manifest inmutable
del grant, vuelve a leer el plan y los ReviewTask heads, exige expiración y
presupuestos vigentes, cruza `started → applying` por efecto y sólo acepta
`applied` con un receipt ligado al grant, efecto, identidad y hash. Una
interrupción, timeout, receipt incompleto o resultado ambiguo queda en
`recovery_required` y no se reintenta automáticamente. `reconcile` sólo observa
y registra la clasificación; no convierte una inferencia en autorización.

`recovery status` y `restore preview` sólo leen la evidencia y no crean
sidecars. `restore apply` exige el token exacto derivado del `action_id` y del
receipt original, crea un intento `restore_curation` antes del movimiento y usa
un backend inyectado con `renameat2(RENAME_NOREPLACE)` para fixtures. La fuente,
la raíz y la entrada de Papelera se revalidan por identidad, hash y contención;
si el movimiento o la limpieza de `.trashinfo` queda ambiguo, el intento
permanece `recovery_required` y no se reintenta automáticamente. El restore de
owners SQLite (`databases restore`) es un flujo distinto y no comparte esta
autoridad.

La primera tranche 0.12 añade límites opcionales de trabajo a la verificación
exacta mediante `CurationWorkBudget`, con contabilidad de items, archivos y
bytes, deadline monotónico y cancelación cooperativa. Las observaciones previas
se conservan cuando el presupuesto se agota y el resto queda `not_verified`, sin
crear efectos ni modificar el corpus. El contrato durable
`neocortex.curation-checkpoint/v2` permite crear, leer, validar y reanudar el
siguiente lote paginado desde API/SDK, enlazando root identity, source heads,
plan/snapshot digest, cursor, batch digest y contadores acumulados, con sucesores
deterministas y escritura atómica no-replace. Su alcance es la página publicada:
no afirma checkpoint DFS de inventario ni habilita MCP, KIO o mutación.

La secuencia controlada desde Python es:

```python
created = curation_checkpoint_create_payload(
    "verify", plan_id=PLAN_ID, limit=100, state_directory=FIXTURE_STATE
)
status = curation_checkpoint_status_payload(
    created["result"]["checkpoint_id"], state_directory=FIXTURE_STATE
)
next_page = curation_checkpoint_resume_payload(
    created["result"]["checkpoint_id"], state_directory=FIXTURE_STATE
)
```

Crear y reanudar escriben únicamente manifests de estado con efectos de corpus
nulos, exigen un directorio explícito de fixtures y no aceptan overrides de
presupuesto al reanudar; el estado y la evidencia de la página se revalidan
antes de publicar el sucesor. El presupuesto restante se transmite al
verificador antes del I/O y los sucesores conservan el tamaño de página. Un
recorrido agotado con fuentes parciales sigue informando cobertura parcial,
sin repetir indefinidamente la última página, y el replay terminal comprueba
el snapshot actual. Los checkpoints v1 se leen sin alterar su representación;
el contrato v2 conserva explícitamente cobertura, razones y fin del recorrido.

El inventario completo dispone además del contrato `neocortex.inventory-resume/v1`
para corridas Linux sobre una raíz contenida. El checkpoint conserva el cursor
DFS determinista, la identidad de raíz y de los directorios recorridos, los
contadores y los digests canónicos del prefijo y del lote comprometido. Las
escrituras son atómicas, privadas y monotónicas, y una cancelación deja un
owner `partial`; al reanudar se revalida el prefijo, se descarta sólo el tail
posterior al cursor y se continúa con lotes acotados. Si el owner ya es
terminal, el replay vuelve a comprobar el inventario y no crea otra generación;
un cambio de contenido, política, identidad o ancestro produce abstención.

La superficie MCP no crea ni reanuda estos owners, porque el contrato requiere
un directorio de estado explícito y una autoridad local de proceso; la capacidad
se consume mediante `DedupIndex.scan`/`InventoryScanner` en la API Python y se
mantiene separada de `curate verify`, que conserva su checkpoint page-level.

No existe una interfaz de exportación ni un paquete ZIP de curación. `--json`
serializa la respuesta de una operación; no crea un artefacto durable.

**Lectura y diagnóstico post-0.13:**

```text
API/SDK: content_diagnostics_v2_payload, KnowledgeReadBudget
MCP: content_diagnostics_v2
GUI: vista read-only de grants, intentos, receipts y recovery
```

Estas superficies conservan snapshots y cursores bounded, no escanean el corpus
ni conceden autoridad. El contrato `neocortex.authenticated-principal/v1`
rechaza actores textuales o principals no atestados; no habilita autorización
MCP. La promoción física de KIO, restore de escritorio y sincronización de
`trash` permanecen fuera del alcance.

## Separación de Code y desarrollo

Un repositorio, incluido NeoCortex, puede procesarse como corpus Code: lenguajes,
estructura, símbolos, relaciones y búsqueda. Las pruebas, lint, tipos y análisis
de seguridad pertenecen al desarrollo y se ejecutan con herramientas externas.

Dogfooding significa consultar NeoCortex como contenido y entregar evidencia a
Codex; no significa reintroducir una plataforma productiva que coordine sus
propios validadores.

## Criterio de completitud

La curación estará entregada cuando una muestra representativa pueda recorrer el
lifecycle completo, repetir sin rehacer trabajo, recuperarse de una interrupción
y dejar el plan, las decisiones, los efectos y el estado final consultables desde
los owners locales. Abstenerse de forma segura es necesario, pero no sustituye
resolver los casos soportados.

Las entregas y fechas se controlan en [ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md);
la arquitectura implementada se documenta en [ARCHITECTURE.md](ARCHITECTURE.md).


## Contrato público de KIO

**IMPLEMENTED:** `neocortex.safety.kio_trash.KioTrashService` es responsable del
ciclo físico común a la curación individual y los lotes de deduplicación exacta.
La curación conserva validación del root y keeper, autorización, intents,
transiciones del ledger y vinculación de recibos al grant. El servicio no abre
SQLite, selecciona políticas de autorización ni añade mutaciones MCP.

- `move(snapshot, *, source_digest)` devuelve un `KioTrashResult`.
- `move_many(items: Sequence[KioTrashBatchItem])` devuelve un resultado por
  entrada y en el mismo orden. Ambos usan `move_many_to_trash`, también para
  un lote de un elemento, y comparten claims, verificación y durabilidad.
- `trash_receipt_paths(evidence, expected, source_digest)` valida estructura,
  identidad, digest y layout declarado sin leer el filesystem.
- `verify_trash_receipt_evidence(evidence, expected, source_digest)` reobserva
  identidad completa, objeto regular único, digest, ausencia del original y
  metadatos de restauración vinculados al source. Es read-only y rechaza cambios.
- `read_claim_recovery_detail(detail, *, source_path)` resuelve localizadores
  v1/v2 contra el source exacto del resultado o acción, sin I/O ni restauración.

El constructor mantiene los parámetros explícitos de verifier, runner, which,
environment, home_directory, timeout y los tres controles privados. Se conservan
`move_to_trash`, `move_many_to_trash`, `restore_trash_receipt`, los tipos públicos,
los imports y firmas de `KioTrashBackend`, sus aliases de aplicación y el nombre
`kio-trash-path-bound-v1`. Los recibos de éxito y el esquema SQLite siguen en v1.

El modo nativo conserva configuración KDE privada, D-Bus privado y claim vecino
mediante `renameat2(RENAME_NOREPLACE)` en el mismo filesystem. Fuente y ejecutable
se revalidan junto a sus fronteras físicas. No hay fallback de copia, reemplazo,
GIO ni unlink del original. Un runner inyectado conserva el seam de fixtures:
recibe las rutas originales y no activa claims ni configuración de escritorio.

`applied` exige retorno satisfactorio, ausencia del original, evidencia exacta
Trash, flush de directorios y eliminación del claim seguida de fsync de su
padre. `blocked` sólo representa rechazo previo al efecto, con claims creados
restaurados. Timeout, interrupción, verificación incierta o fallo de restauración
requieren recovery; nunca un reintento automático de una operación ambigua.

El JSON de recuperación no se recorta como texto. Si el envelope v1 excedería
los 4096 bytes admitidos por `BackendOutcome`, v2 conserva basename del directorio de
claim, identidad física completa y SHA-256 de la ruta source. Esa ruta absoluta
ya está en el resultado o acción. El lector público reconstruye el claim exacto
y rechaza un source distinto; sólo el diagnóstico opcional puede reducirse.
Los envelopes históricos v1 íntegros de hasta 65.536 bytes continúan siendo
legibles. Esta preservación de metadatos no declara soporte KIO completo para
todos los nombres POSIX; esa compatibilidad conserva su validación específica.

Los lotes se separan por cantidad antes de invocar KIO. Se permite dividir por
argv sólo cuando todos los miembros devolvieron `blocked` con
`kio_batch_arguments_too_large`, después de restaurar todos sus claims. Los
resultados ya verificados se conservan si falla un lote posterior. Si todos los
resultados físicos ya están resueltos y falla retirar configuración temporal,
se conserva cada recibo y se registra el diagnóstico correspondiente.
Una interrupción del operador conserva recovery para el lote que ya cruzó la
frontera y marca `kio_cancelled_before_effect` en los sublotes aún no invocados;
no inicia efectos nuevos después de Ctrl+C.

`curation.application` traduce evidencia del servicio a `BackendOutcome`; su
replay, `curation.recovery` y `workflow.actions.file_action_recovery` consumen
validadores públicos, sin helpers privados de safety. `FrameworkActions`
conserva una fila y un recibo por source. Consultar un recibo o claim no autoriza
restaurar, reintentar ni ampliar la ejecución nativa. No se requieren migración,
reset del estado ni reclasificación de acciones históricas.
