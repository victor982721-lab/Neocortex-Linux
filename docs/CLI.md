# Interfaz de línea de comandos

La interfaz canónica es el ejecutable instalado `Neocortex`. La definición
exacta vive en `neocortex/api/cli/cli_parser.py` y en los subparsers de
`neocortex.api.cli.human`; este documento organiza su uso, no sustituye
`Neocortex --help`.

## Efectos

### Plan local de curación

`curate plan` en el árbol actual consulta la raíz de estado canónica, no acepta
rutas de estado o corpus y devuelve una página acotada con `plan_digest`,
`snapshot`, `cursor` y `next_cursor`. La operación es advisory, no escribe
estado ni autoriza acciones; la release instalada previa puede requerir una
instalación desde el SHA actual para exponerla:

```bash
Neocortex curate plan --limit 50
Neocortex curate plan --limit 50 --cursor TOKEN
Neocortex curate plan --limit 50 --json
```

El digest representa el stream completo de propuestas y no cambia al variar
`--limit`; si la publicación cambia, el cursor anterior se rechaza y debe
iniciarse una consulta nueva.

| Clase | Ejemplos | Efecto |
|---|---|---|
| Consulta | `help`, `status`, `search`, `ask`, `inspect`, `models status`, `databases status` | Lee publicaciones existentes; no recorre corpus ni crea estado |
| Producción de estado | rutas, Semantic, catálogo, refresh de Review | Lee contenido y escribe estado, pero no modifica originales |
| Descarga | `models prepare` | Adquiere modelos de forma explícita |
| Estado destructivo | `databases restore`, `databases purge` con `--apply` | Requiere confirmación, manifest/plan y locks |
| Corpus | `--apply`, `--organization-apply` | Rechazado en Linux en la versión actual |

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

## Curación disponible

```bash
Neocortex --curation-preview 50 --curation-json
```

La vista es bounded y read-only. Reúne planes ya publicados de duplicados,
organización y archivos vacíos, junto con identidad, reasons y cobertura. No
existe todavía `Neocortex curate apply`; la jerarquía objetivo se describe en
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

El servidor stdio expone actualmente consultas read-only como status, search,
context, evidence, `curation_plan`, Code, lineage y salud de assets. `evidence`
puede recibir `evidence_id` y `expected_snapshot_id`; no acepta texto del corpus
como instrucción ni expone aplicación de acciones.

## Salida estructurada y códigos

Los modos JSON/JSONL conservan un `schema`, la operación, cobertura, errores y
warnings cuando el contrato los produce, mientras los campos de scope, epoch y
contadores dependen de la superficie consultada. `curation_plan` coloca cursor,
digest y conteos dentro de `snapshot` y `page`; no se presentan campos que el
contrato no entregue. Los códigos exactos pertenecen al comando y su ayuda; como
regla:

- `0`: operación solicitada completada dentro de la cobertura declarada;
- `2`: uso inválido, abstención operativa o cobertura incompleta bajo modo
  estricto;
- otros códigos no se normalizan a éxito y deben conservar su diagnóstico.

No uses la ausencia de traceback como prueba de completitud. Para procedimientos,
límites y replay consulta [OPERATIONS.md](OPERATIONS.md).
