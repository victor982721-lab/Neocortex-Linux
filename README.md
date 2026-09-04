# NeoCortex

NeoCortex es un framework local, incremental y multimodal para comprender y
organizar archivos personales en Linux. Su objetivo es sustituir inventarios,
auditorías y scripts improvisados por un flujo reproducible que conserve
identidad, evidencia, incertidumbre y trazabilidad.

La fuente vigente declara `0.11.1`. El comportamiento efectivo siempre se
comprueba con el ejecutable instalado y con su estado publicado; una versión en
el árbol fuente no demuestra qué release está activa.

## Qué resuelve hoy

NeoCortex puede:

- inventariar archivos sin seguir enlaces simbólicos;
- detectar tipos y extraer contenido de documentos, archivos comprimidos,
  imágenes, audio, video, texto y código;
- conservar resultados en owners SQLite separados y publicar proyecciones
  incrementales;
- planear duplicados, clasificación y organización sin modificar originales;
- buscar evidencia mediante CLI, API Python, GUI y MCP local; las únicas
  escrituras MCP actuales publican o deciden ReviewTasks advisory;
- exponer cobertura, errores, procedencia y localizadores cuando el productor
  puede demostrarlos;
- respaldar, restaurar, inspeccionar y purgar el estado mediante comandos
  explícitos.

El recorrido físico sólo está habilitado para fixtures mediante un backend
explícitamente inyectado: `curate apply` consume un grant confirmado y cruza el
ledger por efecto, `curate reconcile` registra recovery sin reintentar y
`curate restore preview/apply` permite una reversión no-replace con confirmación
separada sobre receipts de fixtures.
`--apply` y `--organization-apply` siguen absteniéndose con
`linux_mutation_backend_unavailable`, y la CLI no selecciona KIO ni otro backend
automáticamente. La promoción contra KIO real, el restore y la sincronización de
caches permanecen como gates posteriores, sin `gio trash`, borrado directo ni
fallback destructivo.

## Empieza por una consulta

Estas operaciones no recorren el corpus ni crean estado:

```bash
Neocortex --version
Neocortex --help
Neocortex help
Neocortex status --scope all
Neocortex search "consulta" --scope personal --limit 20
Neocortex ask "consulta" --scope personal --limit 12
Neocortex inspect code "consulta" --scope personal
Neocortex inspect lineage IDENTIFICADOR --scope personal
Neocortex curate plan --limit 20
```

Si el estado no tiene cobertura, prueba una sola ruta sobre una muestra acotada.
Esta corrida sí lee contenido y actualiza estado, aunque no modifica los
originales:

```bash
Root="$HOME/Documentos/NeoCortex/Pilot"
test -d "$Root" || exit 2
Neocortex --root "$Root" --route pdf --max-count 25 --strict-exit-codes
```

Repite el mismo comando y revisa cache, errores y tiempo antes de ampliar el
alcance. `--all` ejecuta todas las rutas de contenido registradas, incluida Code
como contenido; no ejecuta el código observado ni reintroduce el antiguo
autoanálisis del propio repositorio.

## Ruta de curación actual

**CURRENT:** `curate plan` consulta la página estable y paginada del plan local
sin abrir una ruta nueva ni escribir estado. **IMPLEMENTED en el checkout:**
`curate review` publica ReviewTasks advisory, `curate decide` registra por CAS
una decisión humana y `curate authorize` emite un grant durable separado; la
release instalada puede requerir promoción desde el SHA final para exponerlos.

```bash
Neocortex curate plan --limit 20
Neocortex curate plan --limit 20 --cursor TOKEN
Neocortex curate review PLAN_ID --limit 20 --json
Neocortex curate decide PLAN_ID ITEM_ID --expected-event-id EVENT_ID \
  --decision resolved --decision-scope until-source-change --actor ACTOR --json
Neocortex curate authorize PLAN_ID --item-id ITEM_ID --action move \
  --actor ACTOR --expires-ns NS --max-bytes BYTES --json
```

`PLAN_ID` es el `plan_digest` devuelto por plan. Review/decide escriben únicamente
estado ReviewTask y no autorizan. Authorize persiste el grant append-only, pero
no crea `file_actions`, invoca KIO ni aplica un efecto físico. MCP no expone
authorize mientras el actor autenticado no esté resuelto. No existe exportación
o ZIP de curación; `--json` sólo devuelve la respuesta.
`--curation-preview 50 --curation-json` permanece como compatibilidad plana. El contrato completo está en
[File Intelligence & Curation](docs/FILE_INTELLIGENCE_AND_CURATION.md).

El tramo físico controlado se consulta así:

```bash
Neocortex curate apply GRANT_ID --confirm-grant-id GRANT_ID --json
Neocortex curate reconcile --actor ACTOR --confirm-reconcile --json
Neocortex curate recovery status --json
Neocortex curate restore preview ACTION_ID --json
```

La CLI ordinaria devuelve `backend_unavailable` sin un run firmado y un backend
inyectado, por diseño fail-closed; los tests de 0.11 ejecutan el mismo contrato
sólo sobre raíces temporales contenidas.

La tranche 0.12 añade a `neocortex.api.public` y `neocortex.sdk` las funciones
`curation_checkpoint_create_payload`, `curation_checkpoint_status_payload` y
`curation_checkpoint_resume_payload`. Su uso exige un directorio de estado
explícito, conserva root/source/plan/snapshot digests y publica sucesores
idempotentes por página; no selecciona el corpus por defecto, no crea efectos y
no está registrado en MCP.

## Plataforma y rutas

La única plataforma objetivo vigente es Kubuntu/Linux.

```text
Fuente:      ~/Neocortex/Repository
Corpus:      ${XDG_DOCUMENTS_DIR}/NeoCortex/Corpus
Estado:      ${XDG_STATE_HOME:-~/.local/state}/Neocortex/state
Datos:       ${XDG_DATA_HOME:-~/.local/share}/Neocortex
Launcher:    ~/.local/share/Neocortex/bin/Neocortex
Alias:       ~/.local/bin/Neocortex
```

La instalación canónica usa `tools/release_linux.py` y un wheelhouse local
autenticado. No instales dependencias globalmente ni valides una release mediante
imports desde el checkout.

## Documentación

- [Visión de File Intelligence & Curation](docs/FILE_INTELLIGENCE_AND_CURATION.md)
- [Arquitectura](docs/ARCHITECTURE.md)
- [CLI](docs/CLI.md)
- [Operación](docs/OPERATIONS.md)
- [Persistencia](docs/PERSISTENCE.md)
- [Seguridad](docs/SECURITY.md)
- [Knowledge](docs/KNOWLEDGE.md)
- [Recuperación](docs/RECOVERY.md)
- [Kubuntu/Linux](docs/LINUX_KUBUNTU.md)
- [Roadmap](docs/ROADMAP_90_DAYS.md)
- [Registro de cambios](docs/CHANGELOG.md)

Las reglas para contribuir mediante Codex están en [AGENTS.md](AGENTS.md). El
estado reanudable de una campaña activa vive en el único handoff vigente, no en
los contratos estables del producto.
