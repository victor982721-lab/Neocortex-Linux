# Interfaz de línea de comandos

La interfaz canónica es el ejecutable instalado `Neocortex`. La definición
exacta vive en `neocortex/api/cli/cli_parser.py` y en los subparsers de
`neocortex.api.cli.human`; este documento organiza su uso, no sustituye
`Neocortex --help`.

## Estado del lifecycle de curación

**CURRENT — consulta:** `curate plan` consulta la raíz de estado canónica, no
acepta rutas de estado o corpus y devuelve una página acotada con
`plan_digest`, `snapshot`, `cursor` y `next_cursor`. No escribe estado ni
autoriza acciones.

```bash
Neocortex curate plan --limit 50
Neocortex curate plan --limit 50 --cursor TOKEN
Neocortex curate plan --limit 50 --json
```

**IMPLEMENTED — scan y verificación exacta:** `curate scan` consulta la misma
publicación acotada e incluye sus cabezas de estado, mientras `curate verify`
relee los archivos regulares referenciados por el plan, comprueba identidad,
hash completo y comparación byte a byte de cada grupo duplicado. Ambas
operaciones son advisory, no crean estado, `ReviewTask`, grants ni
`file_actions`, y no modifican el corpus.

```bash
Neocortex curate scan --limit 50 --json
Neocortex curate scan --limit 50 --cursor TOKEN --json
Neocortex curate verify PLAN_ID --limit 100 --json
Neocortex curate verify PLAN_ID --limit 100 --cursor TOKEN --json
Neocortex curate verify PLAN_ID --item-id ITEM_ID --json
```

Un plan rápido conserva `persisted_mode=fast`; si la comprobación actual pasa,
la respuesta informa `observed_mode=full_hash`, sin convertir por sí sola el
plan persistido en autorización. Un cambio de identidad, bytes, symlink,
archivo especial, raíz o presupuesto devuelve una abstención tipada.

La respuesta conserva `source_heads` para inventario y catálogo, con su owner,
revisión, digest, cobertura y razón vigente; `--cursor` permite verificar una
página posterior sin confundirla con un cambio de snapshot.

El digest representa el stream completo de propuestas y no cambia al variar
`--limit`; si la publicación cambia, el cursor anterior se rechaza y debe
iniciarse una consulta nueva.

**IMPLEMENTED — ReviewTask advisory:** `curate review` publica una página del
plan completo como tareas de revisión, y `curate decide` registra por CAS una
decisión humana. La release instalada previa puede requerir una instalación
desde el SHA final para exponerlas.

```bash
Neocortex curate review PLAN_ID --limit 50 --json
Neocortex curate review PLAN_ID --limit 50 --cursor TOKEN --json
Neocortex curate decide PLAN_ID ITEM_ID \
  --expected-event-id EVENT_ID \
  --decision resolved \
  --decision-scope until-source-change \
  --actor ACTOR --note "evidencia revisada" --json
```

`PLAN_ID` es el `plan_digest` de `curate plan`. Review devuelve para cada item su
`task_id`, estado y `current_event_id`. Decide acepta `resolved` o `dismissed` y
los scopes `until-source-change`, `until-policy-change` o `permanent`. El mismo
evento se reproduce de forma idempotente; un digest o head distinto se rechaza.

Estas operaciones tienen `read_only=false` porque escriben únicamente
ReviewTask en Framework. No crean `file_actions`, no autorizan, no llaman KIO y
no cambian corpus ni sistemas externos. `--json` devuelve el envelope; no es una
interfaz de exportación ni crea un ZIP.

**IMPLEMENTED — AuthorizationGrant durable:** después de resolver los items,
`curate authorize` emite un grant explícito sin aplicar efectos:

```bash
Neocortex curate authorize PLAN_ID \
  --item-id ITEM_ID \
  --action move \
  --actor ACTOR \
  --expires-ns NS \
  --max-bytes BYTES \
  --json
```

`--item-id` puede repetirse hasta 100 veces; `--action` acepta `trash`, `move` o
`rename`, y `--authorization-key` permite una key de idempotencia explícita. El
grant valida el plan, el snapshot y los heads actuales de ReviewTask al emitirse,
y persiste un manifiesto inmutable con task IDs, versiones, eventos, fingerprints
y digest agregado. Trash de duplicados exige `verification_mode=full_hash`;
move/rename exige destino absoluto. La respuesta declara `actions_authorized=true` y
`physical_effect_applied=false`: no crea `file_actions`, no invoca KIO y no toca
corpus ni sistemas externos.

**IMPLEMENTED sobre fixtures y backends inyectados:** `curate apply` consume sólo
un grant confirmado y `curate reconcile` registra observaciones bounded, sin
reintentar efectos. La CLI instalada no selecciona backend ni run firmado por sí
sola, así que `curate apply` devuelve `backend_unavailable` antes de crear
`file_actions`; la ejecución física de 0.11 se prueba desde API/SDK con un
backend POSIX o KIO falso y una raíz temporal.

```bash
Neocortex curate apply GRANT_ID --confirm-grant-id GRANT_ID --json
Neocortex curate reconcile --actor ACTOR --confirm-reconcile --limit 100 --json
```

El consumidor rechaza grants legacy sin manifest de efectos, vuelve a comprobar
plan, source heads, ReviewTask heads, expiración, identidad, tamaño, mtime, hash,
contención y presupuesto, y procesa un efecto por vez con
`started → applying → applied|recovery_required`. Un timeout, receipt inválido,
interrupción o ambigüedad queda en `recovery_required`, sin fallback a `gio`,
`unlink`, sobrescritura ni reintento automático. `reconcile` sólo añade evidencia
append-only e idempotente; no convierte la observación en permiso.

## Efectos

| Clase | Ejemplos | Efecto |
|---|---|---|
| Consulta | `help`, `status`, `search`, `ask`, `inspect`, `models status`, `databases status`, `curate scan` | Lee publicaciones existentes; no recorre corpus ni crea estado |
| Verificación advisory | `curate verify` | Lee archivos regulares y estado publicado; no crea `file_actions` ni modifica el corpus |
| Producción de estado | rutas, Semantic, catálogo, Review refresh, `curate review/decide` | Escribe owners; no modifica originales ni autoriza efectos |
| Grant de autorización | `curate authorize` | Escribe un grant acotado; no aplica ni verifica un efecto físico |
| Descarga | `models prepare` | Adquiere modelos de forma explícita |
| Estado destructivo | `databases restore`, `databases purge` con `--apply` | Requiere confirmación, manifest/plan y locks |
| Aplicación grant-bound | `curate apply` | Requiere confirmación exacta y backend/run inyectados; la CLI ordinaria falla cerrada sin ellos |
| Conciliación | `curate reconcile` | Registra evidencia bounded; no reintenta ni modifica corpus |
| Corpus genérico | `--apply`, `--organization-apply` | Rechazado en Linux en la versión actual |

## Consultas cotidianas

```bash
Neocortex help
Neocortex status --scope all
Neocortex search "consulta" --scope personal --limit 20
Neocortex ask "consulta" --scope personal --limit 12
Neocortex inspect code "consulta" --scope personal
Neocortex inspect lineage IDENTIFICADOR --scope personal
Neocortex review value --scope personal --limit 50
Neocortex models status --json
Neocortex databases status --json
```

`personal` consulta las publicaciones del usuario. `all` mantiene owners y
scores separados y reporta cobertura. Ninguna consulta corrige, migra o crea una
base ausente.

`review value --refresh` es diferente: avanza una página durable de Review en
Framework. No modifica corpus ni concede autorización.

## Procesamiento de contenido

Las rutas registradas son `pdf`, `docx`, `office`, `archive`, `text`, `audio`,
`video`, `image` y `code`.

```bash
Neocortex --root "$Root" --route pdf --max-count 25 --strict-exit-codes
Neocortex --root "$Root" --route pdf,docx --max-count 25 \
  --docx-max-count 25 --strict-exit-codes
```

Estas corridas actualizan inventario y owners de contenido. `--route-only` usa
inputs durables y omite inventario, deduplicación, detección y acciones;
`--candidate-run RUN_ID` elige el inventario y `--resume-run RUN_ID` reanuda
fases incompletas.

`--all` selecciona todas las rutas registradas, incluida Code. No ejecuta código
del corpus ni produce evidencia de validación del repositorio.

## Estado y salud

```bash
Neocortex --status --status-json
Neocortex --state-health --state-health-json
Neocortex --knowledge-status --knowledge-json
Neocortex --semantic-status --semantic-json
Neocortex --code-status --code-json
```

Los comandos distinguen `complete`, `partial`, `unavailable`, `blocked`, schemas
futuros y corrupción. Ausencia de resultados no se presenta como éxito.

## Búsquedas especializadas

```bash
Neocortex --knowledge-search "consulta" --knowledge-json
Neocortex --code-search "consulta" --code-search-mode hybrid --code-json
Neocortex --code-projects --code-json
Neocortex --code-reconstruct PROJECT_OR_ID --code-json
```

Los localizadores dependen del productor. Si una ruta no conserva página, celda,
segmento o región, la salida no inventa esa precisión.

## Compatibilidad plana de curación

```bash
Neocortex --curation-preview 50 --curation-json
```

La vista es bounded y read-only. Reúne planes ya publicados de duplicados,
organización y archivos vacíos, junto con identidad, reasons y cobertura.
`curate scan/plan/verify/review/decide/authorize/apply/reconcile` es la interfaz
humana estructurada; apply sólo puede ejecutar efectos mediante un backend
inyectado y contenido de fixture. Consulta
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).

## Bases de datos

```bash
Neocortex databases status --json
Neocortex databases backup --backup-directory "$Backup" --json
Neocortex databases restore --backup-directory "$Backup" --json
Neocortex databases purge --json
```

`backup`, `restore` y `purge` muestran preview por defecto. Escribir exige
`--apply`, la confirmación literal que muestra `--help`, epoch/manifest o digest
del plan según la operación. Restore publica desde staging; purge crea primero
su backup verificable. Consulta [RECOVERY.md](RECOVERY.md).

## Modelos y GUI

```bash
Neocortex models status --json
Neocortex models prepare
Neocortex --ui
```

`models status` es local; `prepare` puede descargar. La GUI consume los mismos
contratos y mantiene deshabilitados los efectos de corpus en Linux.

## MCP local

```bash
Neocortex agent serve
```

El servidor stdio expone consultas read-only como status, search, context,
evidence, `curation_plan`, `curation_scan`, `curation_verify`, Code, lineage y
salud de assets. También expone
`curation_review` y `curation_decide`: escriben sólo ReviewTask advisory, están
marcadas no destructivas y mantienen `actions_authorized=false`. `evidence`
puede recibir `evidence_id` y `expected_snapshot_id`; ningún tool aplica acciones
de corpus. MCP no expone `curation_authorize`: el actor autenticado que podría
emitir un grant no está resuelto y no se acepta un nombre aportado por el agente
como sustituto.

## Salida estructurada y códigos

Los modos JSON/JSONL conservan un `schema`, la operación, cobertura, errores y
warnings cuando el contrato los produce, mientras los campos de scope, epoch y
contadores dependen de la superficie consultada. Curation coloca cursor e items
en `page`; review añade `publication`, y decide devuelve el evento e
`idempotent`; authorize devuelve `grant`, sus efectos state-only y
`physical_effect_applied=false`. No se presentan campos que el contrato no entregue. Los códigos
exactos pertenecen al comando y su ayuda; como regla:

- `0`: operación solicitada completada dentro de la cobertura declarada;
- `2`: uso inválido, abstención operativa o cobertura incompleta bajo modo
  estricto;
- `5`: cambió el snapshot, digest o event head esperado;
- `7`: estado corrupto;
- otros códigos no se normalizan a éxito y deben conservar su diagnóstico.

No uses la ausencia de traceback como prueba de completitud. Para procedimientos,
límites y replay consulta [OPERATIONS.md](OPERATIONS.md).
