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
normalización bounded de ZIP, clasificación y publicación, (3) factory reset y
retención de estado sin backup implícito y (4) coordinación adaptativa de
recursos. Cada frontera conserva su owner y motivo de operación; las operaciones
que tienen receipt lo mantienen en su propia frontera. Factory reset no crea un
receipt adicional. Materializar un ZIP
no retira su contenedor automáticamente y las exclusiones Semantic sólo
proyectan visibilidad, sin borrar vectores ni diagnósticos.

## Implementación funcional integrada

La ruta integrada conserva los owners y contratos existentes. `--all` selecciona
las ocho rutas de contenido (`pdf`, `docx`, `office`, `archive`, `text`, `audio`,
`video` e `image`) y converge en este orden: `preflight/lock → inventory →
identify → normalize → policy/redlist → dedupe → routes → organize → semantic
→ finalize`. Identify compara contenido físico bounded (no la extensión
observada) y Normalize corrige extensiones demostrables antes de cualquier hash
completo. `extension != content identity`; la incertidumbre queda
`UNKNOWN/KEEP`.

En `--all --apply`, redlist sólo actúa después de Identify/Normalize. Los
archivos no normalizables permanecen intactos. Redlist, rename y Trash comparten
la frontera de root, identidad, no-reemplazo y receipt. `recovery_required` sólo
representa un efecto físico que pudo cruzar la frontera y no puede confirmarse;
un `blocked/protected` sin syscall es un skip parcial, no recuperación.

Los documentos y datos útiles dentro de `AppData`, cachés o ZIP mixtos siguen
procesándose; no se excluye un árbol completo sólo por su nombre. Un miembro
virtual de un archivo compuesto no se convierte en un objetivo físico: la
redlist se aplica a archivos físicos del Corpus.

El stage Semantic integrado considera Archive y Video cuando sus owners,
heads y dependencias están disponibles. Una dependencia ausente degrada
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

Una publicación Semantic pendiente posterior a epoch 0 se recupera mediante el
mismo productor, manifest y heads de todos los modelos. El borrado operativo
completo del estado sólo se solicita de forma explícita con
`Neocortex --factory-reset`. No crea backup,
snapshot SQL, plan, digest ni receipt adicional, no acepta `--apply`/`--yes` y
deja fuera el corpus, la instalación, los modelos y los `installation-receipts`.

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
- buscar evidencia mediante CLI, API Python y MCP local; MCP permanece read-only
  y no publica colas ni decisiones humanas;
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
  explícitos;
- restablecer el estado operativo completo con `Neocortex --factory-reset`, sin
  procesar el corpus.

### Factory reset operativo

`Neocortex --factory-reset` elimina todo el estado operativo administrado dentro
de la raíz de estado seleccionada: las bases de NeoCortex y sus sidecars, las
materializaciones de ZIP administradas bajo esa raíz (incluido
`state/archive-materialized`), las cachés y
los metadatos de procesamiento. No toca destinos externos producidos por APIs
standalone. No es un alias de
`databases purge`, no elige scopes y no procesa archivos del corpus: no usa
`--root`, rutas de contenido ni modelos para reconstruir nada.

La orden es deliberadamente directa: no crea backup, snapshot SQL, plan, digest
de aprobación ni receipt adicional, y no admite `--apply` ni `--yes`. Protege el
corpus (incluidos los ZIP originales), la instalación, los modelos y los
`installation-receipts`. La invocación principal es:

```bash
Neocortex --factory-reset
```

`--state-directory` queda disponible como override para fixtures aislados. La
operación toma sus locks y verifica writers, procesos y rutas/montajes antes de
retirar. Los symlinks dentro de la raíz se desvinculan sin tocar sus targets;
no se siguen ni se borran targets externos. Rutas o montajes ajenos, permisos
insuficientes y cambios concurrentes producen un error con conteos parciales.
La CLI termina con código distinto de cero y no presenta ese efecto como factory
reset completo.

El recorrido físico Linux usa el backend KIO nativo receipt-bound con claim
same-filesystem/no-replace, sin `gio trash`, borrado directo ni fallback
destructivo. `--apply` es la autorización explícita del usuario para las
acciones seguras de la corrida; `dedupe --apply` y `--all --apply` reutilizan
la cadena de acciones y recovery. La incertidumbre se conserva como KEEP con
razón y evidencia, nunca como una cola humana obligatoria.
La canaria KIO debe demostrar cuotas sin autovaciado y restauración automática;
la restauración visual única desde Dolphin permanece como gate humano separado.

## Preparación federada de `hygiene`

`hygiene` es una superficie nueva de preparación end-to-end, local y bounded.
En esta etapa sólo observa y devuelve un registro/manifest de preview:
`read_only=true`, cero `file_actions`, cero eliminaciones y ningún cambio en
corpus, owners, configuración o sistemas externos. No es un limpiador global ni
un alias de `maintenance`, `--factory-reset` o
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

`maintenance --scope historical-temp` requiere una raíz absoluta explícita.
La inspección legacy conserva su descubrimiento acotado. Para retirar un
artefacto se selecciona su ruta exacta con `--select` y su claim de productor
con `--provenance-artifact`, o se aporta un `--selection-file` JSON. La ruta
puede tener cualquier nombre y estar bajo una raíz compartida sticky, pero su
procedencia, permisos, identidad y copia conservada se verifican por separado.

La secuencia es `--prepare-adoption`, `--approve-adoption DIGEST` y
`--apply-adoption DIGEST --apply`. Cada paso consume el mismo plan; la
aprobación se conserva en un recibo privado autenticado fuera del payload.
Un campo `approved` dentro del artefacto, su edad o su prefijo no conceden
permiso. Los elementos desconocidos, activos, únicos sin copia protegida o
cambiados se conservan. Consulta el [procedimiento completo](docs/OPERATIONS.md#auditoría-histórica-explícita).

`external-maintenance` es únicamente diagnóstico: exige root y categoría
explícitos, no admite `--apply`, no descubre rutas desde HOME y no usa red,
SQLite, KIO, sudo ni otro cleaner. Categorías sin owner (miniaturas KDE,
caches generales, journal, coredumps, sesiones Codex, Papelera y backups
externos) se reportan como `out_of_profile`/`preserved`, nunca como bytes
recuperables.

### Actividad externa registrada

Una actividad de agente que necesite auxiliares propios usa la fachada pública
instalada `neocortex.api.agent_activity.AgentActivity`. La fachada prepara un
workspace bajo un `state_directory` explícito, conserva el owner externo
declarado, ejecuta el proceso únicamente sobre ese workspace, publica el
entregable en una raíz separada con política no-replace y permite cerrar,
reanudar/reconciliar y retirar conforme al registry y al mantenimiento. No se
escriben manifests desde el agente y no se adoptan `HOME`, `.codex`, sesiones,
modelos, cachés compartidas, `/tmp` completo ni el corpus.

El owner `neocortex-framework` es el productor canónico compatible con la CLI
de mantenimiento; un owner distinto sólo es válido si la integración lo
registra expresamente. Ante una caída, el proceso nuevo debe reabrir la
actividad por su identificador durable y observar la recuperación antes de
retirar. Una actividad fallida o con publicación incierta permanece conservada
o `recovery_required`; edad, PID ausente o TTL no son autorización de borrado.
Consulta [Operación → actividad externa](docs/OPERATIONS.md#actividad-externa-determinista-sobre-el-mismo-lifecycle)
para el recorrido y [CLI → matriz de aceptación](docs/CLI.md#matriz-compacta-de-aceptación-del-circuito)
para la evidencia independiente.

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
Las fachadas CLI, MCP y SDK solicitan v2 por defecto, mientras la función Python
de bajo nivel conserva v1 hasta una deprecación explícita. Un resultado completo
de búsqueda no prueba que la pregunta tenga
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
las ocho rutas de contenido registradas; sus efectos sobre el Corpus requieren
`--apply` y una raíz temporal durante la validación.

## Ruta de curación actual

`curate plan`, `curate scan` y `curate verify` son consultas acotadas del plan
publicado y de su evidencia física. No crean ReviewTask, colas, eventos,
grants ni otro estado de autorización. La clasificación automática conserva
`uncertainty`, `reason` y `evidence`; una precondición incierta permanece en
KEEP.

```bash
Neocortex curate plan --limit 20 --json
Neocortex curate scan --limit 20 --json
Neocortex curate verify PLAN_ID --limit 100 --json
```

Para una corrida autorizada, `--apply` ejecuta únicamente acciones automáticas
seguras dentro de la raíz seleccionada. Cada efecto revalida identidad y
contención, registra receipt y deja `recovery_required` cuando la frontera
física es ambigua. No existe una ceremonia separada de review/decide/authorize;
el usuario controla el gate con `--apply`.

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
