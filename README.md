# NeoCortex

NeoCortex es un framework local, incremental y multimodal para comprender y
organizar archivos personales en Linux. Su objetivo es sustituir inventarios,
auditorías y scripts improvisados por un flujo reproducible que conserve
identidad, evidencia, incertidumbre y trazabilidad.

La fuente vigente declara `0.14.1`. La release Linux activa se comprueba desde
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
- consultar un inventario federado bounded de la máquina, con categorías,
  ownership/procedencia, estados y métricas explicables, sin leer payloads ni
  producir efectos;
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

## Preparación federada de `hygiene`

`hygiene` es una superficie nueva de preparación end-to-end, local y bounded.
En esta etapa sólo observa y devuelve un registro/manifest de preview:
`read_only=true`, cero `file_actions`, cero eliminaciones y ningún cambio en
corpus, owners, configuración o sistemas externos. No es un limpiador global ni
un alias de `maintenance`, `state reset`, `curate apply` o
`external-maintenance`; un eventual `--apply` pertenece a una etapa posterior.

El registro de higiene relaciona cada observación con su raíz, identidad física,
owner, fuente, procedencia, categoría, estado, snapshot/digest, límites y
razones. Los manifests son evidencia versionada para comparar y revalidar, no
instrucciones ni autorización. La federación consume las vistas bounded de
scratch registrado, retención por owner, `machine-inventory` y diagnóstico
externo; cada owner conserva autoridad sobre sus datos y un owner ausente no se
rellena por inferencia.

La salida estructurada usa `neocortex.hygiene/v1`; sus campos de seguridad
mantienen `read_only=true`, `effects_enabled=false`, `preview_only=true`,
`deletion_performed=0`, `actions_ready=false`,
`physical_effect_applied=false` y `mutation_authorized=false`. Un
`fingerprint` permite detectar drift en una reconsulta, pero no congela el
filesystem ni convierte `eligible` en una orden.

Las categorías canónicas no equivalen a una decisión de retiro:

| Categoría | Tratamiento de la preparación |
|---|---|
| `canonical` | fuente, evidencia o estado que debe preservarse; requiere owner y procedencia |
| `operational` | estado vivo necesario para operar; se preserva mientras no exista una política del owner |
| `rebuildable` | derivado reconstruible sólo si el owner demuestra entradas y receta; sigue siendo conservable |
| `temporary` | workspace acotado y registrado, con lifecycle y manifest verificables; no incluye todo `/tmp` |
| `cache` | caché de aplicación, modelo o índice; su invalidez o reconstruibilidad son decisiones del owner |

Corpus, fotografías, correo, configuraciones, modelos, backups, sesiones y
otros datos personales pueden ser canónicos u operativos; la preparación no
supone que sólo código y documentación sean conservables. Lo desconocido,
externo, truncado, activo o ambiguo se conserva o queda bloqueado.

El preview captura claims bounded de identidad, montaje, permisos, actividad,
manifest, owner-head y bytes. Si una fase futura llegara a actuar, tendría que
releer esos claims y rechazar cualquier drift antes del efecto. La secuencia
reservada es `preview → review → authorize → apply → verify → recovery`:
revisar no autoriza, autorizar sólo emite un grant, aplicar cruza la frontera
física, verificar demuestra la postcondición y toda ambigüedad queda para
`recovery`. Esta preparación no adelanta ninguno de esos efectos.

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
Neocortex hygiene --hygiene-preview --hygiene-json
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

### Inventario federado de máquina

`machine-inventory` es la consulta bounded y read-only del control plane local.
Su forma predeterminada observa un conjunto conservador de perfiles: `HOME`,
`/tmp`, las raíces XDG de cache y configuración, y las raíces de estado, datos y
corpus de NeoCortex. No escanea `/` por omisión. Las raíces explícitas se pasan
con `--machine-root` repetible y deben ser absolutas; `--machine-root /` sigue
siendo posible sólo como una observación acotada solicitada explícitamente.

```bash
Neocortex machine-inventory --machine-json
Neocortex machine-inventory \
  --machine-root "$HOME" \
  --machine-root /tmp \
  --machine-max-entries 20000 \
  --machine-max-depth 8 \
  --machine-max-bytes 1073741824 \
  --machine-json
```

La salida JSON es **compacta por defecto**: conserva el resumen de cada raíz
efectiva y los agregados globales, pero no vuelca todos los registros de cada
entrada. El modo de detalle se solicita explícitamente con
`--machine-json=records` (el modo predeterminado equivale a `compact`); aun en
ese modo los registros siguen acotados y los límites del escáner no cambian.
En ambos modos `root_count` cuenta las
raíces seleccionadas, incluidas las que no alcanzaron turno de recorrido, y
el resumen por raíz conserva `status`, `reason_code`, `counts`, `bytes`,
`truncated` y `truncation_reasons`. El owner conserva una ranura por cada raíz;
si una cota excepcional de presentación recorta también esa lista, lo indica
en `serialization.root_summaries_omitted` y `presentation_truncated`, en vez de
silenciar la omisión.

El presupuesto de entradas conserva un techo global, pero ya no deja que la
primera raíz lo consuma completo. `root_quota_policy` declara
`equal_fair_share_v1` y `root_entry_quotas` publica la cuota efectiva por
`root_index`; cada resumen de raíz repite su `entry_quota`. La cuota se calcula
dinámicamente como `ceil(entradas_globales_restantes / raíces_restantes)` y el
remanente de una raíz que termina antes se redistribuye a las siguientes. Así,
la cuota es una reserva bounded de observación, no un segundo presupuesto ni
una garantía de cobertura: profundidad, bytes, permisos, carreras o
cancelación todavía pueden detener una raíz antes. `entry_quota=0` significa
que la raíz conserva su resumen pero no recibió registros por agotarse la
frontera global; no significa que esté ausente. `max_depth` y `max_bytes` siguen
siendo límites globales independientes.

Los contadores distinguen tres niveles. `records_scanned` suma únicamente
registros de entradas observados por el scanner; nunca cuenta los marcadores de
raíz. `record_status_counts`, `record_category_counts` y
`record_reason_counts` cuentan sólo esos registros. Los nombres históricos
`status_counts`, `category_counts` y `reason_counts` pueden incluir además un
marcador sintético de raíz ausente, bloqueada o desconocida, mientras que
`root_status_counts` cuenta exactamente el estado de cada raíz efectiva y los
mapas `root_marker_*` desglosan los marcadores que alimentan los nombres
históricos. Usa
los mapas `record_*` para analizar entradas y los mapas de raíz para analizar
cobertura de raíces; no sumes ambos niveles.

El envelope separa dos fronteras de cobertura. `scanner_truncated` (y sus
`truncation_reasons`) describe que el recorrido no pudo completar la cobertura
por `max_entries`, `max_depth`, `max_bytes`, permisos, carrera o cancelación;
es evidencia del owner del inventario. `presentation_truncated` indica que una
segunda cota de serialización tuvo que truncar el detalle; la omisión deliberada
del modo `compact` se cuenta en la faceta `presentation` pero no convierte por
sí sola la cobertura en `truncated`. `serialization.mode`,
`records_included`, `records_returned` y `records_omitted` explican esa
proyección. No uses la presencia de
`[contenido omitido por límite]` ni el tamaño de la salida para inferir que el
escáner terminó: son capas distintas y pueden truncarse de forma independiente.
La vista Python equivalente es `to_summary_dict()`: su `root_summaries`,
`coverage_metadata` y `omissions` mantienen la misma separación sin incluir
`records`.

El recorrido usa `lstat`/no-follow y sólo metadata: identidad física
(`st_dev`, `st_ino` y birthtime o sentinel), tipo, owner, permisos, montaje,
symlink/hardlink y tamaños aparente/asignado. No lee cuerpos ni payload del
corpus, no abre SQLite (tampoco con `mode=ro`), no escribe estado, no usa red,
KIO, sudo ni cleaners y no crea una raíz ausente. Un cambio concurrente puede
degradar un registro a `blocked` o `unknown`; la salida no se presenta como un
snapshot atómico del filesystem.

El envelope cerrado `neocortex.machine-inventory/v1` declara
`read_only=true`, la operación, las raíces y `root_count`, límites efectivos,
`truncated`, registros acotados, `root_quota_policy`,
`root_entry_quotas`, agregados `record_status_counts`,
`record_category_counts`, `record_reason_counts` y `root_status_counts`, además
de los agregados históricos por estado/categoría/razón,
`reason_summary`, `reason_explanations`, el registro de categorías/owners/procedencia y bytes
`observed`, aparentes y asignados. En la contabilidad, `apparent` es la suma de
`st_size` (tamaño lógico), `allocated` es la suma de los bloques reportados por
el filesystem (`st_blocks * 512`) y `observed` es la métrica de presupuesto
`apparent + allocated`; no es espacio libre ni una medida independiente de
duplicación física. Los límites de bytes se aplican a `observed` globalmente,
por lo que sus agregados son créditos bounded y no una auditoría completa del
uso del disco. Los estados posibles son `absent`,
`observed`, `preserved`, `blocked`, `unknown` y `out_of_profile`; un hallazgo o
un recorrido truncado no es un permiso de limpieza. El presupuesto de
`entries`, `depth` y `bytes` es global para la invocación; `entries` se reparte
con la política fair-share, sin retirar el techo ni ampliarlo para terminar una
raíz.

La clasificación separa los perfiles administrados de NeoCortex
(`neocortex_state`, `neocortex_data`, `neocortex_corpus`) de `tmp`, `cache`,
`config`, `home` y `external`. El perfil no es una autorización: las categorías
sin owner probado se conservan o quedan fuera de perfil, y tamaño, antigüedad,
nombre o una recomendación no convierten un elemento en recuperable.

El comando no acepta `--apply` ni operaciones de corpus (`--root`, `--all`,
`--dedupe` o rutas). Las acciones requieren un gate posterior e independiente:
owner y procedencia verificables, política y selección explícitas, preview,
autorización humana, revalidación fresca de identidad/topología/actividad y un
backend reversible con receipt, postcondición y recovery. Ese plano de acción
no forma parte de `machine-inventory`.

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
