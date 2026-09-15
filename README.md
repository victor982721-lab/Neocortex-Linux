# NeoCortex

NeoCortex es un framework local, incremental y multimodal para comprender y
organizar archivos personales en Linux. Su objetivo es sustituir inventarios,
auditorías y scripts improvisados por un flujo reproducible que conserve
identidad, evidencia, incertidumbre y trazabilidad.

La fuente vigente declara `0.14.0`. La release Linux activa se comprueba desde
el SHA final mediante `tools/release_linux.py verify`; el receipt canónico es
la fuente viva de `source_sha`, `current` y el rollback inmediato. No se usan
hashes históricos escritos en esta entrada como evidencia actual.

Esta entrega cierra el primer comportamiento operativo: deduplicación exacta
desde la CLI instalada, backend KDE/KIO con recuperación receipt-bound,
admisión de contenido y ayuda/JSON coherentes. La aplicación física sobre el
corpus personal sigue siendo una invocación explícita posterior; las canarias
destructivas usan fixtures aisladas.

El cierre operativo reúne cuatro fronteras en el mismo lifecycle: (1) dedupe
Linux/KIO recuperable, (2) `--all` con admisión, reutilización, reparación,
normalización bounded de ZIP, clasificación y publicación, (3) reset/retención
de estado sin backup implícito y (4) coordinación adaptativa de recursos. Cada
frontera conserva su owner, receipt y motivo de operación; materializar un ZIP
no retira su contenedor automáticamente y las exclusiones Semantic sólo
proyectan visibilidad, sin borrar vectores ni diagnósticos.

## Implementación funcional integrada

La ruta integrada conserva los owners y contratos existentes. `--all` selecciona
las nueve rutas (`pdf`, `docx`, `office`, `archive`, `text`, `audio`, `video`,
`image` y `code`); para esa modalidad Code conserva el alcance seguro
`projects` y sólo admite raíces de proyectos configuradas, sin ejecutar el
código observado. Un escaneo amplio requiere el opt-in explícito
`--code-scope broad`; sus límites por formato
permanecen efectivos y los límites globales sólo aparecen cuando se expresan de
forma explícita: no se añade un techo global oculto y los flags repetibles siguen
siendo acumulativos.

La procedencia de Code distingue señales fuertes de dependencia/vendor,
generado/build/cache y binario, pero no infiere autoría. En el flujo normal
`--all` prepara automáticamente la limpieza de terceros; `--apply` es el gate
que la ejecuta. Sólo usa candidatos con evidencia suficiente y conserva la
revalidación/receipt KIO; lo ambiguo se deja intacto. La política `keep` queda
disponible para overrides internos de Codex.

El stage Semantic integrado considera también Archive, Code y Video cuando sus
owners, heads y dependencias están disponibles. Una dependencia ausente degrada
la ruta afectada y deja resultado `partial`/`incomplete` con causa tipada; no
oculta el trabajo independiente. La actualización del catálogo ocurre después
de cada productor y conserva `protected`, `no_speech`, `no_audio` y
`metadata_only` como observaciones parciales, sin fabricar texto.

Las rutas reparan FTS y derivados desde una representación durable válida sin
repetir OCR, transcripción o análisis que ya sean íntegros. Un reintento sólo se
admite con evidencia estructurada `retryable` y una vez por archivo y corrida;
un mensaje que contenga la palabra «retry» no concede permiso. Las propuestas de
organización son reversibles y se aplican sólo dentro de la raíz autorizada
cuando se solicita `--apply`; mover, renombrar o retirar fuera de esa frontera
sigue siendo rechazado.

La GUI usa la misma orden de rutas, estados y stage Semantic que la CLI: el perfil
completo se traduce al lifecycle `--all`, mientras un subconjunto guardado no se
expande por sorpresa y el perfil piloto mantiene límites acotados. Una publicación
Semantic pendiente posterior a epoch 0 se recupera mediante el mismo productor,
manifest y heads de todos los modelos; no se reinicia ni se resetea el estado
automáticamente.
Un reset destructivo sólo ocurre mediante `Neocortex state reset` con un alcance
seleccionado, preview y confirmación explícita. Sin `--backup-directory` no se
crea un backup persistente; los datos no regenerables se preservan por owner.
Si la compatibilidad no puede demostrarse, el resultado es `recovery_required`
explicable.

## Qué resuelve hoy

NeoCortex puede:

- inventariar archivos sin seguir enlaces simbólicos;
- detectar tipos y extraer contenido de documentos, archivos comprimidos,
  imágenes, audio, video, texto y código;
- conservar resultados en owners SQLite separados y publicar proyecciones
  incrementales;
- planear duplicados, clasificación y organización sin modificar originales;
- ejecutar `--dedupe`/`dedupe` con igualdad byte a byte y enviar redundantes
  verificados a la Papelera KDE, conservando receipts y restauración no-replace;
- buscar evidencia mediante CLI, API Python, GUI y MCP local; las únicas
  escrituras MCP actuales publican o deciden ReviewTasks advisory;
- exponer cobertura, errores, procedencia y localizadores cuando el productor
  puede demostrarlos;
- auditar una raíz histórica absoluta de forma bounded y, sólo con un manifest
  de aplicación y una adopción verificables, retirar entradas elegibles sin
  tocar el corpus, SQLite productiva, releases, modelos ni `/tmp` por defecto;
- diagnosticar raíces externas de forma explícita y read-only, separando
  observación, preservación y categorías fuera del perfil sin convertirlas en
  candidatos de limpieza;
- respaldar, restaurar, inspeccionar y purgar el estado mediante comandos
  explícitos.

El estado derivado también puede limpiarse de forma seleccionable, siempre con
preview y confirmación explícitos:

```bash
Neocortex state reset --scope runs
Neocortex state reset --scope runs-and-caches
Neocortex state reset --scope all
```

Estas variantes no tocan el corpus, releases, modelos ni backups externos.
`--apply --yes` enlaza un preview nuevo de forma no interactiva; también se
conserva la forma legacy con digest y `RESET_STATE`. `--backup-directory` sólo
se usa cuando se solicita expresamente.

El recorrido físico Linux usa el backend KIO nativo receipt-bound con claim
same-filesystem/no-replace, sin `gio trash`, borrado directo ni fallback
destructivo. `curate apply` conserva su frontera grant-bound independiente;
`dedupe --apply` y `--all --apply` reutilizan la cadena de acciones y recovery.
La canaria KIO debe demostrar cuotas sin autovaciado y restauración automática;
la restauración visual única desde Dolphin permanece como gate humano separado.

## Empieza por una consulta

Estas operaciones consultan el estado publicado sin actualizar los owners ni
modificar archivos del corpus:

```bash
Neocortex --version
Neocortex --help
Neocortex help
Neocortex --root "$Root" --dedupe --dedupe-json
Neocortex --root "$Root" --all --json
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
Neocortex maintenance --scope owned-temp --maintenance-json
Neocortex maintenance --scope historical-temp \
  --maintenance-audit-root "/ruta/raiz-historica" --maintenance-json
Neocortex external-maintenance --external-root "/ruta/externa" \
  --external-category application_cache --external-json
```

Las preguntas explícitas sobre estado del corpus, por ejemplo
`Neocortex ask "¿Qué PDFs están protegidos?"`, consultan los diagnósticos
publicados por sus owners en lugar de tratar una conversación que menciona un
error como si fuera el archivo afectado. El MCP equivalente es
`operational_query`; ambos conservan snapshot, cursor y el límite advisory
read-only.

`maintenance --scope owned-temp|audit-work` consulta únicamente el scratch
registrado bajo `state_directory/scratch`; no crea la raíz ausente ni escanea
`/tmp`. Su forma `--apply` sólo retira workspaces propios `completed` y se
documenta separadamente, sin tocar corpus, cachés externas, releases ni SQLite
productiva.

`maintenance --scope historical-temp` es una frontera distinta: exige
`--maintenance-audit-root PATH` absoluto y explícito. No reutiliza `--root`, el
estado ni una ruta predeterminada a `/tmp`, y su plan no crea la raíz ni produce
efectos. Sólo el owner histórico puede clasificar hijos directos con prefijo
`neocortex-` y manifests allow-listed; `--apply` vuelve a observar y retira
únicamente entradas con identidad, actividad, manifest y adopción verificadas.
Lo desconocido, activo, no adoptado, ambiguo o cambiado se conserva o queda
bloqueado. El flujo no llama limpiadores externos ni KIO y no abre SQLite ni el
corpus.

Para una raíz grande puedes elevar explícitamente sus límites bounded:
`--maintenance-max-entries`, `--maintenance-max-depth` y
`--maintenance-max-bytes`. La salida incluye `status_counts`, `reason_summary`
con explicación humana y muestras acotadas, además de `largest_records`; así
se distingue falta de manifest, permisos inseguros, actividad, recovery y
cobertura truncada sin convertir ninguna categoría en permiso de borrado.

`external-maintenance` es únicamente diagnóstico: exige root y categoría
explícitos, no admite `--apply`, no descubre rutas desde HOME y no usa red,
SQLite, KIO, sudo ni otro cleaner. Categorías sin owner (miniaturas KDE,
caches generales, journal, coredumps, sesiones Codex, Papelera y backups
externos) se reportan como `out_of_profile`/`preserved`, nunca como bytes
recuperables.

`ask`, `ask --json`, `--knowledge-context` y la herramienta MCP `context` usan
el contexto compacto v2: fuentes sin repetición, fragmentos citables y cobertura
explícita, con presupuesto para la respuesta completa. `ask --response-version 1`
o `--knowledge-response-version 1` conservan el contrato anterior;
`SharedReadClient`, la GUI y las conveniencias SDK solicitan v2 por defecto,
mientras la función Python de bajo nivel conserva v1 hasta una deprecación
explícita. Un resultado completo de búsqueda no prueba que la pregunta tenga
respuesta ni autoriza acciones.

La búsqueda Semantic admite un [índice exacto derivado explícito](docs/OPERATIONS.md#índice-exacto-derivado-de-semantic),
apagado por defecto. Su preparación y apertura verifican un head publicado;
reutilizar el handle puede reducir el costo de consultas repetidas sin ANN ni
cambio de precisión. La CLI incluye el costo frío de verificar el artefacto,
y no lo construye ni lo descubre automáticamente.

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

No existe una ruta histórica canónica: `historical-temp` sólo acepta el
`--maintenance-audit-root` absoluto de esa invocación.

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
