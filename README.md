# NeoCortex

NeoCortex es un framework local, incremental y multimodal para comprender y
organizar archivos personales en Linux. Su objetivo es sustituir inventarios,
auditorías y scripts improvisados por un flujo reproducible que conserve
identidad, evidencia, incertidumbre y trazabilidad.

La fuente vigente declara `0.13.0`. La última integración verificó
`HEAD == main == origin/main == c6d3985f7a45fc3120bd03e9561195674f2b8ac2` y
árbol limpio. El ejecutable `current` es
`0.13.0-c6d3985f7a45-cp314-linux-x86_64`, construido desde ese `source_sha`; el
rollback inmediato es `0.13.0-1567fe46821b-cp314-linux-x86_64` y `.staging` está
vacío. El estado instalado y el estado del checkout se comprueban por separado;
la tranche post-0.13 ya está instalada y verificada desde el mismo SHA.

La aceptación C0–C7 de 0.13 está confirmada sobre el SHA final: 6917 pasadas,
68 omitidas, 42 subtests, calidad estática sin errores/hallazgos bloqueantes,
build reproducible, smoke/replay instalado y piloto de 37 fixtures sin cambios
en sus bytes.

## Tranche post-0.13 instalada y verificada

La fuente y el artefacto activo incorporan publicaciones inmutables de
inventario/catálogo, digest de contenido contra reescrituras con el mismo
`size/mtime`, materialización segura de recursos Archive/Code virtuales,
localizadores y hydration ampliados, Context v2 con grafo/telemetría,
`content-diagnostics/v2`, `KnowledgeReadBudget`, lectura fenced de grants y
recovery, sincronización de caches sólo sobre fixtures y un contrato de
principal autenticado que aún no habilita autorización MCP.

La validación desde el SHA final terminó con 6,959 pruebas aprobadas, 67 omitidas
y 42 subtests, y la promoción a `current` conservó el corpus personal cerrado y
no ejecutó KIO real.

La release tiene manifest `6f2a44f6e7ab937a3b92fac3f71be196cbffa707eddb21c7fac58972b7823b84`,
árbol `6e67d6c6314268dacab90a69e17d755b7c9caa164301987b30b52a42706196be`,
wheel `7e7a73eb30c7ceaa026c2d70a21f0e218abd08151a608db74e7ba74185f479aa` y
receipt de instalación en
`/home/winterboss/.local/state/Neocortex/state/installation-receipts/20260910T014132.624563Z-install-0.13.0-c6d3985f7a45-cp314-linux-x86_64.json`.

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

Estas operaciones consultan el estado publicado sin actualizar los owners ni
modificar archivos del corpus:

```bash
Neocortex --version
Neocortex --help
Neocortex help
Neocortex status --scope all
Neocortex search "consulta" --scope personal --limit 20
Neocortex ask "consulta" --scope personal --limit 12
Neocortex ask "consulta" --scope personal --characters 12000 --json
Neocortex inspect code "consulta" --scope personal
Neocortex inspect lineage IDENTIFICADOR --scope personal
Neocortex curate plan --limit 20
Neocortex --pdf-diagnostics 20 --diagnostics-json
Neocortex --text-errors 20 --diagnostics-json
Neocortex --archive-issues 20 --diagnostics-json
Neocortex --root "$Root" --content-diagnostics 20 --diagnostics-owner all --diagnostics-json
```

Las preguntas explícitas sobre estado del corpus, por ejemplo
`Neocortex ask "¿Qué PDFs están protegidos?"`, consultan los diagnósticos
publicados por sus owners en lugar de tratar una conversación que menciona un
error como si fuera el archivo afectado. El MCP equivalente es
`operational_query`; ambos conservan snapshot, cursor y el límite advisory
read-only.

`ask`, `ask --json`, `--knowledge-context` y la herramienta MCP `context` usan
el contexto compacto v2: fuentes sin repetición, fragmentos citables y cobertura
explícita, con presupuesto para la respuesta completa. `ask --response-version 1`
o `--knowledge-response-version 1` conservan el contrato anterior;
`SharedReadClient`, la GUI y las conveniencias SDK solicitan v2 por defecto,
mientras la función Python de bajo nivel conserva v1 hasta una deprecación
explícita. Un resultado completo de búsqueda no prueba que la pregunta tenga
respuesta ni autoriza acciones.

Las consultas de diagnóstico respetan `--root` (o el corpus predeterminado),
distinguen cero resultados de owner ausente/error y exponen cursores ligados a
su ámbito. MCP conserva `content_diagnostics` v1 y añade
`content_diagnostics_v2` para la vista federada. Una recomendación declara
evidencia y comprobaciones faltantes, no permiso de borrar.

Si el estado no tiene cobertura, prueba una sola ruta sobre una raíz que ya
contenga únicamente 20–50 archivos autorizados.
Esta corrida sí lee contenido y actualiza estado, aunque no modifica los
originales:

```bash
Root="$HOME/Documentos/NeoCortex/Pilot"
test -d "$Root" || exit 2
Neocortex --root "$Root" --route pdf --max-count 25 --strict-exit-codes
```

Repite el mismo comando y revisa cache, errores y tiempo antes de ampliar el
alcance. `--max-count` limita PDFs, no el inventario completo. `--all` ejecuta
todas las rutas de contenido registradas, incluida Code
como contenido; no ejecuta el código observado ni reintroduce el antiguo
autoanálisis del propio repositorio.

## Ruta de curación actual

**CURRENT:** `curate plan` consulta la página estable y paginada del plan local
sin abrir una ruta nueva ni escribir estado. **IMPLEMENTED:**
`curate review` publica ReviewTasks advisory, `curate decide` registra por CAS
una decisión humana y `curate authorize` emite un grant durable separado. Estas
interfaces forman parte de 0.12.0, pero compartir versión no implica compartir
SHA: los cambios posteriores requieren comprobar el manifest instalado.

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

La planificación de duplicados acepta decisiones explícitas con
`--dedup-keep FILE` y ubicaciones preferidas con
`--dedup-prefer-root DIRECTORY`, ambas repetibles dentro de la raíz de entrada.
Estas opciones ejecutan inventario y plan, escriben estado interno y nunca
autorizan borrar archivos. `--show-groups` explica la elección; dos decisiones
de conservación incompatibles en un mismo grupo impiden publicar el plan.

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

La instalación personal usa `tools/release_linux.py` y un wheelhouse local
autenticado. Una extracción ordinaria del repositorio también permite construir
e instalar el mismo paquete en un venv CPython 3.13 sin Git, red, paquetes
globales ni promoción de release: véase [instalación offline y capacidades](docs/LINUX_KUBUNTU.md#instalación-ordinaria-desde-una-extracción).
Los wheels de desarrollo incluidos no forman parte del paquete instalado.

## Documentación

- [Visión de File Intelligence & Curation](docs/FILE_INTELLIGENCE_AND_CURATION.md)
- [Arquitectura](docs/ARCHITECTURE.md)
- [CLI](docs/CLI.md)
- [Operación](docs/OPERATIONS.md)
- [Persistencia](docs/PERSISTENCE.md)
- [Seguridad](docs/SECURITY.md)
- [Knowledge](docs/KNOWLEDGE.md)
- [Consultas operacionales de Knowledge](docs/KNOWLEDGE_OPERATIONAL_QUERY.md)
- [Recuperación](docs/RECOVERY.md)
- [Kubuntu/Linux](docs/LINUX_KUBUNTU.md)
- [Roadmap](docs/ROADMAP_90_DAYS.md)
- [Registro de cambios](docs/CHANGELOG.md)

Las reglas para contribuir mediante Codex están en [AGENTS.md](AGENTS.md). El
estado reanudable de una campaña activa vive en el [handoff operativo vigente](.codex/handoffs/CURRENT.md),
no en los contratos estables del producto.
