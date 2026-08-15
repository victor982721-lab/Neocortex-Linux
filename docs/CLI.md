# Guía de la interfaz de línea de comandos

La interfaz pública canónica de NeoCortex es el ejecutable instalado
`Neocortex`. La definición exacta de argumentos vive en
`_04_Nucleo_Operativo/cli_parser.py`; esta guía resume los contratos operativos
que conviene conocer antes de usar `--help`.

## Comandos cotidianos

| Necesidad | Comando |
|---|---|
| Guía humana breve | `Neocortex help` |
| Estado publicado | `Neocortex status --scope all` |
| Buscar evidencia | `Neocortex search "consulta" --scope personal --limit 20` |
| Preparar contexto citado | `Neocortex ask "consulta" --scope personal --limit 12` |
| Inspeccionar Code | `Neocortex inspect code "consulta" --scope framework` |
| Explicar una derivación | `Neocortex inspect lineage IDENTIFICADOR --scope personal` |
| Revisar valor sin cambios | `Neocortex review value --scope personal` |
| Avanzar una página durable de revisión | `Neocortex review value --refresh --scope personal` |
| Diagnóstico de una corrida | `Neocortex --status --status-json` |

Empiece por consultas sobre estado publicado. Si debe producir cobertura nueva,
siga el piloto de 20–50 elementos y 10–15 minutos de
[OPERATIONS.md](OPERATIONS.md).

## Comprobación previa de la instalación

Ejecute primero comandos que no recorren el corpus ni escriben estado:

```powershell
Neocortex --version
Neocortex --help
Neocortex --ui --help
```

La versión mostrada debe coincidir con la versión que se pretende operar. Si
`--version` no existe, la versión no coincide o la ayuda no contiene las rutas
esperadas, deténgase: el launcher instalado y el árbol fuente no representan la
misma entrega. No use una ruta nueva hasta actualizar y volver a comprobar el
entrypoint.

Esta fuente declara `0.9.0`. En Windows, la fuente canónica está en
`%USERPROFILE%\Neocortex\Repository`; los runtimes versionados viven bajo
`%LOCALAPPDATA%\Programs\Neocortex\versions` y el launcher estable es
`%LOCALAPPDATA%\Programs\Neocortex\bin\Neocortex.exe`. En Linux, la fuente está
en `~/Neocortex/Repository`, las releases en
`${XDG_DATA_HOME:-~/.local/share}/Neocortex/releases`, `current` selecciona la
activa y `~/.local/bin/Neocortex` es el alias público. Valide primero el
ejecutable exacto del runtime y promueva el launcher sólo después de esa
barrera.

Desde la raíz del repositorio, el siguiente comando sirve únicamente para
diagnosticar el árbol fuente; no sustituye la validación del ejecutable
instalado:

```powershell
py -3 -m neocortex --version
```

En Linux, el diagnóstico equivalente del árbol fuente es
`python3.14 -m neocortex --version`; la instalación canónica se gestiona con
`python3.14 tools/release_linux.py`.

## Sintaxis y rutas

```text
Neocortex [opciones]
```

`--root` selecciona la raíz que se observará. Si se omite, se usa el perfil del
usuario actual. Confirme siempre la ruta antes de iniciar una corrida:

```powershell
$Root = 'C:\Datos'
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "La raíz no existe o no es un directorio: $Root"
}
```

Las rutas de contenido vigentes en la CLI son:

| Nombre | Contenido principal |
|---|---|
| `pdf` | Extracción, OCR, FTS, perfiles y relaciones PDF. |
| `docx` | Documentos y plantillas OOXML de texto. |
| `office` | XLSX, PPTX y ODT. |
| `archive` | Miembros de ZIP y ZIP anidados, incluido OCR acotado de imágenes y PDF escaneados. |
| `text` | Texto imprimible, EML y Office heredado DOC/XLS/PPT. |
| `audio` | Audio y pistas de vídeo admitidas mediante Whisper. |
| `video` | Streams, escenas, keyframes, frames, OCR y timestamps mediante FFmpeg. |
| `image` | Clasificación, OCR, huella completa y evidencia de imágenes. |
| `code` | Texto, estructura, símbolos y relaciones de código fuente. |

El primer piloto usa una sola ruta y un límite explícito:

```powershell
Neocortex --root $Root --route pdf --MaxCount 25 --strict-exit-codes
```

Estos comandos **no son consultas de sólo lectura**: recorren contenido y
actualizan las bases de estado aunque no se especifique `--apply`. Sin
`--apply` no deben mutar los archivos del corpus, pero sí producen inventario,
cachés, ejecuciones, diagnósticos y planes persistentes.

Después de aprobar cada ruta y su proyección se acepta una lista separada por
comas o `--all`. `--all` no se combina con `--route` ni con operaciones
directas de consulta o diagnóstico.

La corrida `--all` ejecuta primero el autoanálisis protegido de
`~/Neocortex/Repository` —o su equivalente canónico Windows— usando el estado
separado `self-analysis`. Después ejecuta las nueve rutas del corpus y, si no
hubo errores de acciones u organización, avanza Semantic sobre las cachés
disponibles de PDF, DOCX, XLSX, PPTX, ODT, audio, Archive, texto/correo e
imágenes. El canal visual sólo se ejecuta cuando existe `image.sqlite3`. Sus
límites integrados son 100 000 items, 1 000 000 de jobs y 172 800 segundos.
Una truncación limpia se informa como progreso reanudable y conserva exit `0`;
errores o estado stale conservan exit `2`. Code sólo participa cuando se
selecciona expresamente con `--semantic-source code`, porque es una carga de
análisis distinta y no debe consumir implícitamente el presupuesto documental.
El conteo real se consulta con `--semantic-status`; la guía no fija cifras
históricas de vectores o chunks como si fueran estado vigente.

El autoanálisis y la etapa documental son fronteras independientes. Si la raíz
del corpus no existe, `--all` conserva el autoanálisis ya ejecutado, informa
`ERROR corpus_unavailable: ...` y sale con código `2` sin traceback ni creación
parcial del estado documental.

## Modos de ejecución

### Corrida integrada

Una corrida normal actualiza el inventario común y después ejecuta las rutas
seleccionadas. La omisión de `--apply` es el modo predeterminado no mutador del
corpus. Amplíe a varias rutas sólo después del piloto:

```powershell
Neocortex --root $Root --route pdf,docx --MaxCount 25 --docx-max-count 25 --strict-exit-codes
```

`InternalPathsPolicy` reserva por ruta e identidad el repositorio, runtime,
datos de aplicación, laboratorio de autoanálisis y launcher. Una raíz normal
dentro de esos árboles se rechaza; sus descendientes internos se excluyen del
inventario. El estado no puede ser igual ni ancestro del corpus. La firma
efectiva durable combina la firma cruda de exclusión con la firma de rutas
internas.

### Autoanálisis de código

El preset `--self-analysis` exige raíz y estado explícitos, fuerza exactamente
la ruta `code` en modo `analyze_only` y rechaza `--all`, `--apply`,
route-only/resume, selección, catálogo, organización y opciones que no consume.
Ese rechazo protege la invocación manual combinada; `--all` reutiliza el mismo
servicio mediante una invocación interna separada y canónica, sin mezclar sus
raíces ni sus bases.
Los árboles de raíz y estado deben ser completamente disjuntos:

```powershell
$Lab = Join-Path $env:LOCALAPPDATA 'Neocortex\self-analysis\fixtures'
$MiniRoot = Join-Path $Lab 'mini-root'
$MiniState = Join-Path $Lab 'mini-state'
Neocortex --self-analysis --root $MiniRoot --state-directory $MiniState
Neocortex --self-analysis --analysis-profile trusted-static --root $MiniRoot --state-directory $MiniState
Neocortex --state-directory $MiniState --code-status --code-json
Neocortex --state-directory $MiniState --code-review
```

La corrida usa el inventario como entrada directa de code, no crea candidatos
MIME y sólo completa si candidatos, acciones y organización conservan conteos
exactos de cero. Su manifest guarda policy/firma, identidades, frescura y los
argv canónicos `analyze`/`status` como arrays, no como texto de shell.
`--analysis-profile protected` es el valor predeterminado: Ruff observa los
Python vigentes publicados por Code con fingerprint exacto y usa la política
aislada `E4,E7,E9,F`. `trusted-static` conserva ese proveedor y añade doce
proveedores: Ruff proyecto, Mypy, Pyright, Ruff Analyze, Grimp, Complexipy,
Vulture, Semgrep, Deptry, pip-audit, inventario del entorno instalado e historial
Git local. Ruff
trusted usa `E4,E7,E9,F,B,C4,PIE,RUF` y omite `I,PT,SIM,UP`. Ruff Analyze actúa
como oráculo diferencial del grafo; Grimp produce imports, fan-in/fan-out, SCC,
ciclos y contratos; Complexipy produce complejidad cognitiva por símbolo y
módulo. Semgrep ejecuta tres invariantes locales versionadas con autofix
deshabilitado; Deptry contrasta imports con `pyproject.toml`; pip-audit captura
un snapshot fechado de vulnerabilidades de PyPI; el inventario verifica
versiones, constraints, metadata de licencia y `RECORD` del wheel instalado;
Git correlaciona historia local por módulo. La suite completa suma 13
proveedores. No importa módulos del proyecto ni
aplica fixes, y toda su evidencia es advisory. La única excepción offline es
pip-audit, que declara el acceso de red de su snapshot; su replay exacto no
consulta la red. El inventario instalado se recalcula en cada corrida.
`vulture-unused-static` publica sólo candidatos heurísticos
`unused_code` y tampoco posee autoridad de fix, borrado o mutación.

`trusted-deep` añade Pytest + Coverage y Cosmic Ray a los 13 proveedores
estáticos, para un total de 15. Nunca
es el perfil predeterminado y sólo acepta la identidad física exacta de
`$HOME\Neocortex\Repository`; cualquier otra raíz se rechaza antes de
crear un run. Este perfil sí ejecuta código declarado del proyecto, pruebas y
`conftest.py`, por lo que debe usar estado aislado:

```powershell
$Root = Join-Path $HOME 'Neocortex\Repository'
$State = Join-Path $HOME 'Neocortex\Laboratory\self-analysis\trusted-deep'
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State

# Selección focal repetible; omitirla significa suite declarada completa.
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State `
  --deep-test-selector tests/test_bounded_subprocess.py `
  --deep-max-tests 3000 --deep-time-budget-seconds 600 --deep-shard-size 20

# Mutación focal del módulo/símbolo del work package.
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State `
  --deep-test-selector tests/test_external_deep_coverage.py `
  --deep-mutation-target _04_Nucleo_Operativo/external_deep_coverage.py `
  --deep-mutation-symbol external_deep_coverage._normalize `
  --deep-mutation-max-mutants 20 --deep-mutation-timeout-seconds 30 `
  --deep-mutation-time-budget-seconds 600
```

`--deep-test-selector` acepta sólo una ruta relativa bajo `tests/` o un node id
de Pytest y puede repetirse. `--deep-max-tests` admite 1–5000 (3000 por defecto),
`--deep-time-budget-seconds` 30–900 (600) y `--deep-shard-size` 1–50 (20). La
selección vacía se publica como `full`; una o más selecciones, como `selected`.
El manifest declara `content_executed=true`, la selección y la firma de estos
controles.
`--deep-mutation-target` acepta un `.py` relativo a la raíz y exige al menos un
`--deep-test-selector`; `--deep-mutation-symbol` es opcional y no puede existir
sin target. Los límites de mutación son 1–100 mutantes (20 por defecto), 1–120
segundos por mutante (30) y 10–900 segundos totales (600). Estas cinco opciones
no pueden repetirse. Cosmic Ray modifica únicamente la copia staged, ejecuta
las pruebas seleccionadas —que pueden usar red— y publica evidencia advisory
con cero autoridad sobre el repositorio original.

Si USN no está disponible, el preset hace un full scan portable sin checkpoint
y publica `journal.status=unavailable`; code puede reutilizar caché, pero el
status no afirma frescura actual. En una corrida normal, el mismo caso publica
un checkpoint portable de Dedup sin cursor y reporta
`journal_usn_span=unavailable`; USN sólo acelera la enumeración incremental.

`--code-status --code-json` consulta ese manifest sin crear ni migrar estado.
Cada propietario exige un snapshot SQLite immutable y sidecar-free. Cualquier
`-wal`, `-shm` o `-journal` —incluso vacío o desacoplado— junto a
`code.sqlite3`, `framework.sqlite3` o `dedup.sqlite3`, o una cerca inestable en
cualquiera de ellas, causa abstención total con código `2` sin tocar el estado.
La salida añade `analysis_profile` y `external_evidence_suite`: lista cada
proveedor, versión, ejecución, cobertura, findings, comparabilidad, gate y
counters; `type_consensus` conserva por separado coincidencias y discrepancias
Mypy/Pyright. `architecture_analysis` v2 muestra consenso del grafo, módulos,
imports, SCC, contratos, complejidad y limitaciones. Un proveedor ausente o una
publicación no comparable queda `not_evaluated`, no `passed`. En review, el
work package expone `architecture_contracts_not_degraded`,
`no_new_import_cycles` y `module_complexity_not_displaced`; no son permisos de
edición. Para `trusted-deep`, `test_coverage` añade selección y completitud,
resultados de pruebas, totales de líneas/ramas, relaciones test→símbolo, ejemplos
faltantes, versiones, firmas y limitaciones. Coverage usa contextos dinámicos
por node id y fase Pytest; mide sólo el proceso principal, no subprocesses.
`engineering_analytics` v1 correlaciona por módulo complejidad, cobertura,
mutación, historia y grafo sin emitir score agregado ni probabilidad de defecto;
expone gates de baseline, completitud y score de mutación.

En `trusted-static` y `trusted-deep`, `unused_analysis` correlaciona Vulture con
Pyright, grafo, imports, reexports, `__all__`, callbacks, registries, fixtures,
entry points, Protocols y Coverage disponible. Cada candidato queda exactamente
en `explained_usage`, `dynamic_usage_possible`, `insufficient_evidence` o
`probable_unused_high_consensus`. La salida `CODE_UNUSED` y su JSON incluyen
conteos, ejemplos acotados, firmas, precision/recall/abstención de calibración y
holdout, gates y limitaciones. Ausencia de cualquiera de los dos proveedores
estáticos causa abstención del consenso; Coverage puede explicar uso observado,
pero su ausencia nunca prueba que un símbolo no se use.

La proyección `supply_chain` separa `dependency_hygiene`,
`known_vulnerability`, `package_integrity` y `license_inventory`; no las reduce
a un score. Publica seis gates: invariantes Semgrep, declaración de
dependencias, frescura del snapshot, ausencia de vulnerabilidades conocidas,
integridad del paquete e inventario de licencias. Un gate fallido conserva sus
findings y relaciones explicables, pero nunca autoriza una modificación. Status,
review y work packages consumen la misma evidencia; la ausencia o caducidad de
un proveedor obliga a abstener sólo la dimensión afectada.

`--code-review` consume esa publicación sin volver a analizar la raíz. El
envelope `neocortex.code-review/v19` no declara compatibilidad con schemas
anteriores. Usa `neocortex.code-analysis-epistemics/v1`, una proyección general
de preguntas con fingerprint de spec y evidencia resuelta contra IDs de
registros Code. Publica observaciones estructurales confirmadas y separa hipótesis,
readiness de pregunta, evidencia faltante, contraevidencia, siguiente acción y
readiness de decisión. Los hotspots quedan `experiment_required`, con
`construction=unknown`, `change_risk=unknown`, cero recomendaciones y cero
packages de cambio. La resolución prueba procedencia y concordancia del
diagnóstico; no prueba daño, cohesión ni necesidad de refactor.

La familia `neocortex.code-class-surface/v1` observa clases Python actuales y
sus miembros AST directos confirmados. La selección `span >= 500` o
`direct_methods >= 20` es deliberadamente un filtro provisional de atención.
No deduce rol, ownership, cohesión, consumidores ni riesgo desde el nombre o la
ruta; una clase de pruebas, un `Protocol` o un composition root siguen visibles
como controles negativos y quedan `experiment_required`. El límite solicitado
se aplica por familia de pregunta.

v19 conserva `neocortex.code-interface-surface/v1`: observa módulos seleccionados
por span/superficie directa, estructura de configuraciones JSON/TOML y llamadas
estáticas `argparse`. No expone valores de configuración, no ejecuta módulos y
no presenta option strings sintácticos como reachability o comportamiento del
comando público. Formatos text-only o no soportados permanecen explícitamente
incompletos.

La proyección general incluye además preguntas sobre el grafo estático
Ruff/Grimp y los contratos de imports versionados. Un contrato `failed` se
conserva como observación, pero no crea automáticamente un patch. El primer
segmento del módulo continúa siendo sólo `path_namespace_id`. El registry v1 de
logical owners declara selectores exactos para `text`, `semantic`, `knowledge`,
`review`, `retention` y `framework`; es parcial, publica unmapped/overlap y no
asigna un owner por defecto. Su pregunta queda lista para caracterización, no
para una decisión de cambio.

Cuando `--code-review` consume el estado canónico protegido, v19 también publica
`neocortex.code-state-projection/v1`. La observación compara revisiones Text
elegibles (`complete`, revisión presente, blob presente y `text_chars > 0`) con
miembros del head Semantic de texto publicado. Las lecturas usan `immutable=1`,
exigen WAL vacío/sidecars inactivos y verifican fences antes/después. En un
estado de fixture o una ruta no canónica esta dimensión se abstiene con
`document_state_not_configured_for_noncanonical_code_review`; no busca ni crea
otro estado por convención de ruta.

También publica `neocortex.code-retention-analysis/v1`: observa un plan dry-run
acotado sobre los cuatro owners de Retention, preserva holds faltantes, stores
bloqueados y cursores como gaps, y exige reproducibilidad antes/después. No
expone una orden de borrado ni convierte un fixture aprobado en autoridad de
mutación.

El output incluye además `CODE_STATE_TOPOLOGY`, `CODE_RETENTION_ANALYSIS`,
`CODE_CHANGE_EVOLUTION`,
`CODE_ASSURANCE`, `CODE_CAPABILITY_REACHABILITY`,
`CODE_ANALYZER_EFFECTIVENESS` y `CODE_INTERFACE_SURFACE`. Las preguntas de
seguridad y dependencias se alimentan del mismo `supply_chain`; un proveedor no
registrado o stale permanece faltante. La proyección de autoeficacia compara el
snapshot publicado contra archivos Git visibles por digest y no publica
precision/recall ni decision rate sin etiquetas independientes.

v19 conserva `CODE_STATE_INTERACTIONS`, `CODE_INVARIANT_ASSURANCE`,
`CODE_ROUTE_CAPABILITIES`, `CODE_ANALYZER_CALIBRATION` y
`CODE_EXPERIMENT_PLAN`. SQL literal se parsea con el dialecto SQLite y se liga a
store/workflow sólo por contratos explícitos. Los placeholders SQLite `?NNN`
se normalizan de forma token-aware únicamente para el parser. El assurance de invariantes sólo
acepta outcomes de todos los nodeids exactos registrados para cada escenario;
un selector parametrizado se expande y debe quedar cubierto por completo.
`passed` es evidencia del escenario, no prueba universal. La calibración conserva
las 40 etiquetas existentes como
`provisional_not_human_validated` y no calcula precision/recall con ellas.

El review imprime hasta 20 `CODE_EXPERIMENT_PROPOSAL` ejecutables. Para ejecutar
uno de forma explícita:

```text
Neocortex --state-directory STATE --code-experiment-run PROPOSAL_ID --code-json
```

El ID debe pertenecer al plan reconstruido en esa misma invocación. El runner
allow-listed usa trusted-deep, presupuesto acotado y manifest exacto; si cambia
fuente, proposal, provider o base Code, falla cerrado. Hoy sólo son ejecutables
`architecture.declared_import_contract_acceptance` (tres nodeids y cuatro
gates), `capability.public_route_acceptance` (un nodeid),
`state.runtime_sql_trace` (cuatro nodeids),
`state.semantic_process_death_recovery` (un nodeid con tres gates),
`evolution.code_schema_upgrade_matrix` (cinco nodeids con cuatro gates) y
`retention.durable_hold_safety` (catorce nodeids exactos y cuatro gates). El registry
general contiene otros
escenarios de assurance/calibración, pero no por ello son ejecutables desde esta
opción. El escenario arquitectónico verifica sólo los contratos de imports
declarados, el grafo vivo y controles negativos seleccionados; no observa
dispatch dinámico ni prueba que el diseño completo sea el correcto.

Pytest corre directamente sobre la raíz canónica confiable. El temporal fuera
del repo aloja runtime y checkpoints: no es una copia de la fuente ni un sandbox;
el provider declara `uses_network=true` y conserva el `HOME` canónico. Antes y
después se recalcula la firma de los inputs Python publicados y del soporte Git
observado; una diferencia falla cerrado. El digest before/after cerca además
`code.sqlite3` durante la ejecución. No hay lock continuo del checkout y el
corpus y otros owners quedan fuera. El receipt medido
`neocortex.code-experiment-receipt/v3` agrega por escenario únicamente después
de outcomes terminales y gates tipados para todos sus nodeids.

Al terminar, el comando **sí escribe** una evidencia acotada: inserta el receipt
en la tabla append-only de Code schema v6 y, con `--code-json`, devuelve el
envelope `neocortex.code-experiment-store/v1` que contiene ese receipt. Por eso
`code_database_unchanged=true` no significa que la invocación completa sea
read-only. El review v19 posterior evalúa el terminal más nuevo del proposal y
la processing signature vigentes; puede reutilizar un `passed` de un run Code
completado previo cuando el vigente es un replay exacto con la misma firma. Un
terminal posterior `failed` o `abstained`, o uno stale, corrupto o sin binding,
permanece auditable pero no satisface evidencia. El envelope digest liga todo el
contexto durable. El enlace no suplanta a un actor humano. El verificador
técnico allow-listed de v19 puede publicar
`no_change_required_within_verified_scope` tras volver a comprobar contrato,
gates y controles negativos exactos; la disposición es advisory, conserva
riesgos residuales y no autoriza un patch. Preguntas completas sin una política
exacta quedan `unresolved`.

`python-maintenance-work-packages-v5` puede entregar, de forma independiente,
hasta tres paquetes
`unused_characterization` únicamente cuando pasan los gates de precisión de
calibración y holdout. Todos sus pasos son de caracterización, exigen revisión
dinámica y confirmación humana, y declaran `mutation_authority=false`. La
proyección Coverage demuestra ejecución por una suite passing y líneas/ramas no
observadas; no demuestra que un test proteja un invariante. `--code-review-limit N
--code-json` permite inspeccionar entre 1 y 50 observaciones por familia. No
admite `--apply`,
`--route` ni otra operación directa.

`--code-publication-diff BASELINE_STATE` compara ese baseline con el owner Code
de `--state-directory`. Es estrictamente read-only y falla cerrado si falta un
run completado, el schema no coincide o existe cualquier sidecar SQLite. El
envelope `neocortex.code-publication-diff/v10`, sin declarar compatibilidad
estructural con wires anteriores, informa
calls comunes y exclusivas, resoluciones nuevas/corregidas/perdidas, cambios de hotspots y el
delta meramente descriptivo de `probable_dead`. También compara por separado
los proveedores cuyas firmas coinciden, informa findings añadidos/resueltos,
gate y veredicto agregado; los restantes quedan `not_evaluated` con su
limitación. En Mypy/Pyright clasifica como `relocated` únicamente el mismo
finding semántico en la misma ruta con otro rango, publica ejemplos con ambas
posiciones y no lo convierte en regresión. Cuando la arquitectura es comparable añade deltas por módulo,
imports, SCC/ciclos, contratos y complejidad desplazada. También compara líneas
y ramas de Coverage cuando suite, alcance de medición, configuración y versiones
son equivalentes; en cualquier otro caso publica `not_evaluated`. Nunca aplica
cambios. `unused_analysis` compara candidatos añadidos/retirados, cambios entre
los cuatro estados y consenso alto añadido/resuelto sólo cuando coinciden
proveedores, policy, calibración y holdout. Su gate falla ante consenso alto
nuevo, pero sigue siendo observacional y jamás autoriza borrar o modificar.
`supply_chain` compara por proveedor, categoría, gate, observación y relación;
si el baseline no contiene los cuatro proveedores nuevos o difieren versiones,
frescura o firmas, publica la dimensión como `not_evaluated` o baseline/current
sin inventar mejora o regresión. `engineering_analytics` compara sus cinco
dimensiones y sólo calcula delta de mutation score con alcance comparable.
El contrato, la puerta incremental de tres evidencias y el mini-root permitido
se detallan en [SELF_ANALYSIS.md](SELF_ANALYSIS.md).

#### Consulta multidimensional de publicaciones Code

`--code-query {status,review,diff}` consulta las mismas publicaciones mediante
una interfaz acotada, sin ejecutar otra vez el autoanálisis y sin crear, migrar,
hacer checkpoint ni escribir sus bases:

```powershell
Neocortex --state-directory $State --code-query status
Neocortex --state-directory $State --code-query review `
  --code-query-provider $Provider --code-query-category $Category `
  --code-query-module $Module --code-query-status $Status `
  --code-query-work-package $WorkPackage --code-query-limit 100 --code-json
Neocortex --state-directory $CurrentState --code-query diff `
  --code-query-baseline $BaselineState --code-query-delta added --code-json
```

Cada filtro puede repetirse: valores de la misma dimensión se unen con OR y
dimensiones diferentes con AND. `module` coincide con el módulo exacto y sus
descendientes; los demás filtros son valores exactos publicados. El límite
predeterminado es 50 y el rango válido es 1–500. Baseline es obligatorio para
`diff` y se rechaza con `status` o `review`. Sin `--code-json`, la salida humana
usa `CODE_QUERY`, `CODE_QUERY_FILTERS`, `CODE_QUERY_MATCH` y
`CODE_QUERY_LIMITATION`; JSON conserva el envelope completo. Ambas vistas son
advisory: fijan `aggregate_score` y `defect_probability` explícitamente en
`null`, no estiman ninguno de los dos y no autorizan cambios.

### Ruta sobre un snapshot retenido

`--route-only` omite inventario, planeación de duplicados, detección común y
acciones de archivos. Requiere al menos una ruta, usa por defecto el snapshot
retenido más reciente y rechaza `--apply`:

```powershell
Neocortex --route pdf --route-only
Neocortex --route pdf --route-only --candidate-run 40
```

`--resume-run RUN_ID` implica `--route-only` y continúa fases incompletas del
snapshot indicado:

```powershell
Neocortex --resume-run 40
```

La ruta code consume el inventario y admite un snapshot con cero candidatos:

```powershell
$State = 'C:\Estado\Neocortex'
Neocortex --root $Root --state-directory $State --route code --route-only
Neocortex --root $Root --state-directory $State --route code --route-only --candidate-run 40
Neocortex --root $Root --state-directory $State --resume-run 40
```

Sin `--candidate-run`, code examina el owner durable más reciente de la raíz
exacta y exige que sea `normal`; si no coincide, falla sin retroceder a un run
histórico aunque éste tenga filas MIME. Cero candidatos se acepta únicamente si todas las rutas seleccionadas
declaran `input_source=inventory_snapshot`; cualquier ruta MIME o combinación
mixta falla antes de crear o ejecutar el run. El preset `--self-analysis` sigue
rechazando route-only/resume.

La corrida fuente debe conservar un snapshot de enrutamiento publicado: scan
completo, candidatos durables cuando la ruta los consume y raíz con la misma
ruta e identidad física. Los runs actuales publican ese vínculo sólo después de
terminar la generación de candidatos. Un run legacy interrumpido sin `scan_id`
sólo puede recuperarse si su evento de inventario es único y válido, los conteos coinciden y ya existe
evidencia durable de ejecución de rutas; cualquier ambigüedad rechaza la
reanudación sin reconstruir estado por inferencia.

No presuponga que cualquier corrida antigua continúa retenida. Compruebe
primero `--status`.

### Interfaz gráfica

```powershell
Neocortex --ui
Neocortex --ui --root $Root
```

La GUI supervisa el mismo orquestador y expone PDF, DOCX, Office, ZIP,
texto/correo, audio, video, imagen y Code. Su quinta página **Consulta** consume
los mismos contratos `status`, `search`, `ask` y `review value` de sólo lectura,
con scopes fijos, citas, cobertura e incertidumbre; no acepta rutas de estado ni
presenta controles de mutación. En Linux muestra “modo portátil Linux”, no
solicita elevación y desactiva los controles de mutación; inventario,
procesamiento y búsqueda se conservan. El worker `--gui-worker` es un contrato
interno y no debe invocarse manualmente. La GUI puede mostrar la consulta
read-only que ahora consume una cola vigente, pero una vista gráfica para
refrescar o decidir `ReviewTask` permanece **PLANNED**; la CLI ya expone el
lifecycle mediante la API compartida.

### Consulta humana y agentes locales

Los subcomandos humanos son una fachada sobre las APIs publicadas; no sustituyen
las rutas productoras ni retiran los flags históricos:

```bash
Neocortex status --scope personal
Neocortex search "protección diferencial" --scope all --limit 10
Neocortex ask "¿qué evidencia existe de la prueba FAT?" --scope personal
Neocortex inspect code "validación de schema" --scope framework --mode hybrid
Neocortex inspect lineage IDENTIFICADOR --scope personal
Neocortex review value --scope personal --limit 50
Neocortex review value --refresh --scope personal --limit 50
Neocortex review task show TASK_ID --scope personal
Neocortex review task history TASK_ID --scope personal
Neocortex review task claim TASK_ID --expected-event-id EVENT_ID --actor ACTOR
Neocortex review task decide TASK_ID --expected-event-id EVENT_ID \
  --decision resolved --decision-scope until-source-change --actor ACTOR
```

Los scopes válidos son `personal`, `framework` y `all`. `all` ejecuta cada
snapshot independientemente y no fusiona scores. `status`, `search`, `ask` e
`inspect code`/`inspect lineage` aceptan `--json`; `search`/`ask` acotan la
consulta a 4096 caracteres y como máximo 100 resultados por scope.
`review value` es advisory, declara `mutation_authorized=false` y no mueve,
archiva ni elimina. Sin `--refresh` es estrictamente read-only: consulta la cola
Framework v22 sólo si coincide con el snapshot fuente y, si todavía no existe,
usa el preview legacy sin crear o migrar estado.

`--refresh` es la única variante escritora de esta familia. Sólo admite un scope
fijo `personal` o `framework` (`all` se rechaza), puede crear/migrar
`framework.sqlite3` y avanza exactamente una página keyset de 100 observaciones.
Escribe únicamente batches, memberships, tareas, eventos, progreso y el head
fuente generacional owner-local; no modifica Inventory, Catalog ni archivos.
Ejecútelo otra vez para avanzar la página siguiente, incluso sobre más de
25,000 observaciones. La época de evaluación queda fijada desde la primera
página: un scan incompleto reanuda su cursor aunque cruce medianoche. El último
head completo permanece visible como `stale` mientras una época nueva está en
curso o cambió el owner fuente; el reader conserva decisiones humanas actuales
y no presenta páginas parciales como verdad publicada.

`review task show/history` son read-only. `claim` y `decide` escriben un único
evento Framework append-only mediante CAS y requieren `--expected-event-id` y
actor explícitos. `decide` exige `resolved|dismissed` y un scope durable:
`until-source-change`, `until-policy-change` o `permanent`. El retry semántico
idéntico es idempotente; una tarea/evento distinto falla como snapshot cambiado.
No se modifica el corpus y `all` se rechaza. La GUI consumidora sigue
**PLANNED**.

En JSON, `queue.scan_complete` indica fin del cursor y
`queue.evidence_complete` indica que todas las páginas tuvieron evidencia
íntegra. Pueden ser `true` y `false`, respectivamente; en ese caso la salida y
el código siguen siendo `partial`, `queue.evidence_reason` conserva la causa y
el refresh no retira tareas previas sólo porque ya no aparecieron.

#### `inspect lineage`

La inspección de linaje consulta cómo se produjo estado ya persistido; no
ejecuta extractores ni reconstruye las bases propietarias:

```bash
Neocortex inspect lineage revision:text:... --scope personal
Neocortex inspect lineage materialization:text:... --scope personal --json
Neocortex inspect lineage semantic:chunk:... --scope framework
```

`IDENTIFICADOR` admite un file key/path Text, revisión,
materialización o `WorkReceipt`, y un `chunk_id` Semantic. Use el identificador
exacto emitido por los contratos JSON; los prefijos del ejemplo son
ilustrativos y no deben fabricarse. El scope predeterminado es `personal`;
`all` consulta Personal y Framework por separado sin unir sus grafos.

La salida Text muestra revisión, atribución, receipts, materializaciones,
heads y dependencias; para una revisión Text también enumera hasta 100 chunks
Semantic dependientes. La salida de un chunk Semantic distingue origen,
staged/publicado, receipts y embeddings. Los readers acotan cada ventana Text a
1,000 filas, los eventos de proyección a 100,000 y marcan truncamiento; no
cargan el historial completo de forma silenciosa.

El comando abre exclusivamente `text.sqlite3`/`semantic.sqlite3` ya existentes
bajo las raíces fijas, valida schema y permanece read-only. No crea directorios,
migra bases, hace checkpoint, carga modelos, recorre el corpus ni autoriza una
mutación. El estado humano `ready` devuelve `0`; `not_found`, `partial`, schema
incompatible que impide toda resolución y corrupción conservan respectivamente
los códigos `3`, `4`, `6` y `7` (federados por scope). Si otro owner aporta
evidencia válida pese al schema incompatible, el resultado es `4 partial` con
warning. El lector admite Semantic v6 como legado no atribuible y v7 como
contrato atribuible; nunca migra la base durante `inspect lineage`.

```bash
Neocortex agent serve
```

Ese comando inicia un servidor MCP local sólo por stdio. Expone exclusivamente
`status`, `search`, `context`, `evidence` e `inspect_code`; no abre un listener,
no acepta paths arbitrarios y marca todas las tools read-only, no destructivas e
idempotentes. El texto del corpus se trata siempre como datos no confiables.

## Consultas y diagnósticos sin recorrido

Los siguientes ejemplos no inician un inventario ni autorizan mutaciones del
corpus:

```powershell
Neocortex --status
Neocortex --status --status-run 40 --status-json
Neocortex doctor capabilities
Neocortex doctor capabilities --select text.extract --mime-type text/plain --input-bytes 4096
Neocortex doctor platform
Neocortex doctor platform --json
Neocortex models status
Neocortex models status --json
Neocortex --pdf-doctor
Neocortex --pdf-verify
Neocortex --audio-doctor
Neocortex --video-doctor
Neocortex --video-status
Neocortex --code-status
Neocortex --code-review
Neocortex --code-doctor
Neocortex --semantic-status
Neocortex --action-recovery-status --action-recovery-limit 100
Neocortex --retention-status
```

Una base ausente, dañada o con esquema incompatible puede producir salida `2`;
eso no convierte el diagnóstico en una operación de reparación.
`doctor capabilities` comprueba la presencia de dependencias sin cargar
modelos; los diagnósticos profundos siguen siendo específicos de PDF/OCR,
audio, código y estado semántico.

## Validación canónica de cambios de código

```bash
Neocortex code validate
Neocortex code validate --baseline HEAD^ --json
```

Esta es la única entrada de aceptación local para una implementación nueva. El
primer comando valida el árbol de trabajo contra `HEAD`; el segundo valida un
commit ya creado contra su padre. Las opciones acotadas son `--max-tests N`
(1–5000; 5000 por defecto) y `--time-budget-seconds N` (30–900; 900 por
defecto). El presupuesto predeterminado cubre la medición real observada de la
selección afectada; no amplía el límite global del cgroup.

El proceso padre no ejecuta los gates directamente: hace preflight de memoria,
swap y PSI, conserva una reserva adaptativa para el escritorio y reejecuta la
validación completa en un servicio de usuario systemd/cgroup v2. El grupo tiene
límites de memoria, swap, CPU, tareas y 45 minutos; un watchdog lo detiene si
`MemAvailable` cae por debajo de la reserva. La falta de headroom o contención
produce código 2, nunca una corrida sin límites. Sólo puede existir una
validación canónica a la vez. `PrivateNetwork=yes` aísla por kernel el árbol
completo, por lo que ningún provider puede producir egress durante este gate.

El resultado `neocortex.code-change-validation/v3` enlaza el snapshot Git, la
selección de pruebas, cada gate, los experimentos allow-listed ejecutados, el
smoke del wheel candidato instalado fuera del checkout y el replay exacto del
perfil `trusted-deep`, además de la admisión
`neocortex.code-validation-resources/v3`. El worker verifica en el kernel su
cgroup exacto, consulta en systemd el `PrivateNetwork=yes` y demuestra que la
restricción `AF_UNIX` rechaza sockets AF_INET/AF_INET6; un payload de entorno no
basta.
El gate enlaza además el diff con preguntas de aceptación versionadas: una
pregunta relevante sin runner o disposición técnica produce `abstained`, y
`not_required` exige evidencia explícita de que el diff no la afecta. `passed`
devuelve 0; `failed` o `abstained` devuelven 2.
El recibo nunca concede autoridad de mutación, push o release.

Los conteos históricos `added/resolved` de cada provider se publican como
observaciones advisory. No son una comparación contra `--baseline`: sus
identidades portables incluyen coordenadas y su baseline comparable puede ser
anterior. El gate estático versionado por path/regla/conteo es quien bloquea
regresiones Ruff/Mypy/Pyright antes de consumir el review.

La validación canónica no abre red. Sólo puede reutilizar como evidencia un
audit `pip-audit` ya publicado y aún fresco, con cero findings, enlazado a un
inventario instalado exactamente idéntico y cuando el diff no cruza
packaging/política supply. La resolución queda identificada en el recibo; sin
esas condiciones el resultado se abstiene.
El inventario instalado no finge un cache hit: se captura dos veces y el gate
exige igualdad del digest semántico completo después de retirar únicamente los
campos del reloj de observación.

Sin opciones de selección, `doctor capabilities [--json]` conserva exactamente
el reporte agregado schema 1 de `RuntimeCapabilitySpec`, incluido su orden y
semántica de salida. El broker por trabajo es opt-in y en este corte sólo admite
`text.extract`:

```bash
Neocortex doctor capabilities --select text.extract \
  --mime-type text/plain --input-bytes 4096
Neocortex doctor capabilities --select text.extract \
  --mime-type application/msword --input-bytes 120000 --json
```

`--mime-type` debe ser un MIME exacto y `--input-bytes` un entero no negativo;
ambos son obligatorios con `--select`. La solicitud fija además modalidad
documental, schemas Text y plataforma viva; admite `environment_bound` o
`non_replayable` según el manifest elegido. Sólo los MIME del provider builtin
exige `incremental=true`; para DOC/XLS/PPT la solicitud no impone
incrementalidad, porque su manifest declara `incremental=false`. La política
`neocortex-text-local-v1` prohíbe red, sólo admite privacidad `local_only` y no
presupone GPU.

La salida humana nombra implementación, provider/versión, razones de selección
y evaluación de cada candidato. Con `--json` emite un documento canónico schema
`neocortex.capability-selection/v1` con request, política, candidato elegido o
`null`, readiness, rechazos/preferencias y fingerprints de request, política,
manifest y selección. `status=selected` devuelve `0`; una abstención explicable
`status=unavailable` devuelve `2`; un error fatal del probe devuelve `1`. La
consulta no abre ni crea estado, no carga providers/engines/modelos y no descarga
modelos.

La selección productiva se ejecuta también por cada candidato de la ruta Text.
Texto/EML usa `neocortex.text.builtin` sin depender de LibreOffice; DOC/XLS/PPT
heredado sólo elige `neocortex.text.legacy-office-worker` si existe
`soffice`/`libreoffice` o el backend exacto del MIME. La salida muestra la
identidad SHA-256 del ejecutable fijado. El worker no cambia de backend si éste
falla. El builtin es `environment_bound` y cacheable; el worker Office v2 se
declara `best_effort`/`non_replayable` e `incremental=false` porque la identidad
sólo atesta el launcher, no todos los engines, librerías o descendientes que
pueda invocar. Text vuelve a ejecutar siempre un candidato Office heredado y no
reutiliza ni su resultado ni su fallo previos, incluso con la misma firma. El
tradeoff operativo es explícito: Office sigue seleccionable, pero esta vertical
no promete procesar sólo archivos legacy cambiados. En ausencia de un provider
elegible, confirma un receipt de fallo y no publica materializaciones parciales.

**PLANNED — no disponible mediante `--select`.** PDF, DOCX, la ruta Office,
Semantic y plugins/providers externos todavía no consumen este broker. Una
solicitud distinta de `text.extract` se rechaza durante validación.

`doctor platform` tampoco crea estado. Su esquema versionado informa sistema,
rutas, backend de inventario, identidad, contención, elevación y mutación. En
Linux debe indicar `portable-full-scan`, `posix-st_dev-st_ino`, contención por
sesión/grupo/rlimit, elevación no requerida y mutación intencionalmente no
disponible; esto no vuelve incompatible a la plataforma.

`models status` sólo inspecciona cachés locales y metadata instalada. La
adquisición es una operación distinta y explícita:

```bash
Neocortex models prepare
Neocortex models prepare --json
```

Prepara secuencialmente Jina, MiniLM compacto, CLIP texto, CLIP visión y
Whisper `small` CPU/int8, y valida NudeNet. Conserva descargas parciales
reanudables si no puede completar el conjunto.

`--code-doctor --code-json` proyecta además
`external_evidence_providers` para `ruff-protected-basic`,
`ruff-trusted-project`, `mypy-trusted-project` y
`pyright-trusted-project`, `vulture-unused-static`, `ruff-analyze-imports`,
`grimp-architecture`, `complexipy-cognitive`, `semgrep-neocortex-invariants`,
`deptry-project-dependencies`, `pip-audit-known-vulnerabilities` e
`installed-package-inventory`, `git-history-local`, además de
`pytest-coverage-trusted-deep` y `cosmic-ray-focal-mutation`, con disponibilidad,
versión y autoridad advisory.
La ausencia de un proveedor trusted degrada ese perfil; no sustituye ni invalida
por sí sola al proveedor protected.

También son consultas directas las búsquedas y vistas persistidas, por ejemplo:

```powershell
Neocortex --pdf-search 'transformador AND mantenimiento'
Neocortex --docx-search 'transformador AND mantenimiento'
Neocortex --office-search 'transformador AND mantenimiento'
Neocortex --audio-search 'transformador AND mantenimiento'
Neocortex --video-search 'placa del transformador' --video-search-limit 20
Neocortex --archive-search 'transformador AND mantenimiento'
Neocortex --knowledge-search 'transformador mantenimiento' --knowledge-limit 20
Neocortex --code-search 'sqlite3' --code-search-mode import --code-language python
Neocortex --semantic-search 'transformador mantenimiento' --semantic-search-mode all
Neocortex --catalog-preview 100
Neocortex --organization-preview 100 --organization-preview-status planned
Neocortex --review-candidates 100
Neocortex --review-decisions 100
Neocortex --review-evidence-list 100 --review-json
```

Estas consultas leen las bases existentes. No crean evidencia que todavía no
haya sido materializada y pueden terminar con `2` cuando el estado requerido no
está disponible. La búsqueda semántica usa modelos ya preparados en modo local;
no autoriza una descarga implícita. Ejecute primero `--semantic-status`:
`--semantic-search` sólo lee embeddings y modelos publicados. Cero heads o cero
embeddings significa que esa señal está indisponible, no que Semantic haya sido
entregado.
El contrato exacto Jina/body mixto aplica un piso de recuperación `0.42` a
Archive, audio, Code, DOCX, imagen OCR, ODT, PDF, PPTX, texto y XLSX; el canal de
título pasa por la misma barrera. Las exclusiones aparecen como
`calibrated_abstentions`. Es un filtro de vecinos de baja evidencia, no
confianza ni probabilidad. Antes de persistir embeddings, la política
`semantic-text-quality-v1` omite Base64/binario codificado, volcados densos de
fórmulas, mojibake, tokens desmedidos y repetición mecánica, y colapsa chunks
idénticos del mismo item. Las cachés de extracción completas no se borran. Si
un vector fue reutilizado por contenido exacto, el contrato se toma de su
`payload_provenance`; valores contradictorios no reciben el piso.

La búsqueda lexical conserva la intersección estricta como primera estrategia.
Sólo ante cero hits elimina stopwords ES/EN/DE y permite un fallback acotado;
las consultas Han de al menos dos caracteres activan, después de fallar FTS,
substring exacto sobre un máximo de 50 000 filas por fuente. La procedencia
declara `sqlite_bounded_cjk_substring` y no se compara como si fuera un score
vectorial.

La recuperación CLIP requiere un contrato de calibración positivo/negativo
ligado a firma de modelo, pipeline y backend. Sin él devuelve cero hits,
`scanned=0` y no carga el backend. La evaluación humana actual no produjo un
umbral escalar robusto, por lo que la CLI no suministra uno por defecto.

Code integra el canal Semantic mediante enlaces persistidos exactos, no por una
coincidencia posterior de rutas:

```powershell
Neocortex --semantic-index text --semantic-source code
Neocortex --code-search 'dónde se enlazan chunks con la generación publicada' --code-search-mode hybrid
Neocortex --code-search 'validación de la versión vigente' --code-search-mode semantic --code-json
```

La indexación publica primero el head Semantic y luego sincroniza un enlace por
chunk vigente de Code, con modelo, espacio y generación. Su salida añade
`SEMANTIC_CODE_LINKS`; `--code-status --code-json` informa enlaces
`active/current/stale`. La búsqueda emite `CODE_SEARCH_CHANNEL` o el objeto JSON
`code-search-channel`. El modo `semantic` devuelve `2` cuando ese canal no puede
demostrar head, cobertura y modelo local; `hybrid` continúa con las señales
léxicas o estructurales. `--semantic-model-cache` y `--semantic-threads` también
se admiten con una búsqueda Code `semantic`/`hybrid`; el override de cache es
para laboratorios o instalaciones no canónicas y nunca descarga modelos.

Los hits semánticos sólo se materializan cuando el enlace activo coincide con la
versión actual, item Semantic, firma de modelo, espacio vectorial y generación
publicada. Su `raw_score` permanece como similitud no calibrada y autoridad
`retrieval_evidence_only`; no es confianza de clasificación ni permiso para
renombrar, mover o eliminar.

En modo `text`, la salida separa los rankings `semantic_text` y
`semantic_title`. El primero busca contenido con peso RRF `1.0`; el segundo usa
con peso `0.5` el título durable de la fuente, un encabezado humano acotado si
el basename es genérico o, como último recurso, el basename sin directorios ni
extensión. Ambos declaran peso, base y procedencia, comparten una sola
vectorización de consulta, aplican la misma abstención y conservan de
preferencia el snippet corporal. La diversidad retiene un recurso por documento
en discovery y hasta dos evidencias en evidence. El título es advisory: no
participa en clasificación ni evidencia materializada. Knowledge `evidence` lo
excluye; Knowledge `discovery` puede reforzar sólo un recurso y revisión ya
sustentados por cuerpo, nunca crear una cita. Un head anterior a esta política
mantiene la búsqueda corporal y declara `title_channel_not_indexed` hasta ser
republicado de forma acotada.

### Indexación Semantic acotada

`--semantic-index text|image|all` escribe staging bajo un presupuesto único:

| Opción | Predeterminado | Contrato |
|---|---:|---|
| `--semantic-max-items N` | `50` | Items completos nuevos o cambiados; replay exacto no consume el límite. |
| `--semantic-max-new-jobs N` | `1500` | Jobs durables nuevos o reactivados por cambio de fingerprint; replay exacto no consume el límite. |
| `--semantic-time-budget-seconds N` | `900` | Deadline monotónico compartido por texto, imagen y OCR. |

Estas opciones se admiten con `--semantic-index` y también permiten acotar la
etapa integrada de `--all`. En una acción directa, agotar un límite produce
`truncated=1`, conserva la generación sin publicar y devuelve `2`; no constituye
una corrida completa ni autoriza escalar. En `--all`, la misma truncación limpia
es progreso durable reanudable y no convierte en fallida una corrida cuyas rutas
sí terminaron; errores o estado stale siguen devolviendo `2`. Un replay exacto
sigue enumerando y reconciliando O(n) miembros para detectar cambios, aunque no
cree jobs, clone el head ni haga inferencia. Si existen altas, bajas o cambios,
el sucesor todavía materializa la base en O(n).

Cuando `--semantic-source code` termina una publicación textual completa, el
servicio sincroniza el puente Code↔Semantic antes de devolver éxito. Una
incompatibilidad, un chunk sin correspondencia exacta o un head distinto falla
cerrado y no deja una cobertura parcial activa. Un replay exacto revalida el
puente sin clonar el head ni crear jobs.

### Índice ZIP de sólo lectura

Después de ejecutar la ruta `archive`, estas operaciones consultan únicamente
`archive.sqlite3`; no recorren el corpus ni crean estado ausente:

```powershell
Neocortex --archive-status
Neocortex --archive-search 'protección de transformador' --archive-search-limit 50
Neocortex --archive-list 50 --archive-container 'contenedor.zip'
Neocortex --archive-list 50 --archive-json
```

Search y list devuelven `3` cuando el estado es válido pero no hay resultados;
estado ausente, schema incompatible o corrupción devuelven `2`. Los límites de
search/list son `1..1000`. `--archive-container` es un filtro literal de
fragmento escapado y sólo se admite con search/list. Las tres acciones son
mutuamente excluyentes, rechazan `--apply` y `--route`, y `--archive-json`
requiere una de ellas.

Cada resultado declara `location=archive_member inside_zip=1`. `container` es
el ZIP físico, `member` el nombre dentro de su contenedor inmediato y `chain`
la cadena completa; por ejemplo
`contenedor.zip!/subcarpeta/otro.zip!/documento.txt`. La ruta productora no
materializa esos miembros en disco y aplica límites explícitos de profundidad,
miembros, directorio central, tamaño individual, expansión total, ratio de
compresión, texto y PDF. En OCR `auto`, cada página PDF con menos de 40
caracteres de texto nativo y cada imagen BMP/GIF/JPEG/PNG/TIFF/WebP admitida se
procesan dentro del worker aislado. Se respetan `--ocr`, `--ocr-lang`,
`--pdf-dpi`, `--max-ocr-pages`, `--pdf-max-render-pixels`, `--ocr-timeout` y
los límites Archive; una dependencia o idioma ausente queda como incidencia,
no como texto vacío presentado como éxito. Los controles se consultan en
`Neocortex --help`; para un piloto use `--archive-max-count 20..50`.

### Texto, correo y Office heredado

La ruta `text` detecta por contenido texto imprimible, HTML/XML/JSON, CSV/TSV,
Markdown, EML con estructura RFC 5322 y contenedores CFB con extensión conocida
DOC/XLS/PPT. Es una ruta productora, por lo que recorre el corpus y escribe
`text.sqlite3`:

```powershell
Neocortex --root $Root --route text --text-max-count 25 --strict-exit-codes
Neocortex --knowledge-search 'mantenimiento de transformador' --knowledge-limit 20
Neocortex --semantic-index text --semantic-source text --semantic-max-items 25
Neocortex --catalog-preview 25
```

`--text-max-mb` limita cada archivo; `--text-max-count`, cantidad;
`--text-max-chars`, texto persistido; y `--text-worker-timeout`/
`--text-worker-memory-mb`, el conversor aislado. `--libreoffice-path` permite un
ejecutable explícito y `--retry-text-errors` vuelve a intentar errores sin
cambios. EML conserva asunto y autor. DOC prioriza LibreOffice y conserva
`catdoc` como fallback; XLS y PPT priorizan `xls2csv` y `catppt`,
respectivamente, y usan LibreOffice si falta el extractor específico. No existe
una operación directa `--text-search`: FTS se consume por Knowledge, el
catálogo y Semantic para no crear otra superficie paralela.

### Video y OCR multilingüe

`video` es una ruta productora separada de Audio. FFprobe valida streams y
FFmpeg selecciona escenas, keyframes y muestras periódicas dentro de límites
duros; cada evidencia conserva timestamp, ordinal, razón de selección y OCR:

```bash
Neocortex --root "$Root" --route video --video-max-count 25 --strict-exit-codes
Neocortex --video-status
Neocortex --video-search "placa de datos" --video-search-limit 20
Neocortex --video-doctor --video-ocr-profile auto-multilingual
```

Un video sin audio termina `visual_only` y Audio registra `no_audio` benigno sin
cargar el transcriber. MIME de audio sin stream conserva el error. Los límites
de producto son 48 frames, 40 MP totales de OCR, 16 KiB de OCR por frame,
512 MiB de scratch y 2 GiB de memoria virtual del worker; los overrides siguen
validados por la CLI.

`--ocr-profile`, `--image-ocr-profile` y `--video-ocr-profile` aceptan
`configured`, `latin`, `han-simplified`, `han-traditional` o
`auto-multilingual`. El default conserva el `--*-ocr-lang` configurado. Los
otros perfiles exigen OSD y seleccionan `spa+eng+deu`, `chi_sim+eng` o
`chi_tra+eng`; auto usa el script detectado y como máximo un fallback de
variante. Perfil, idiomas efectivos, OSD, confianza, fallback y huellas de
traineddata quedan ligados a la procedencia y a la caché.

### Knowledge Plane de sólo lectura (`0.9.0`)

Knowledge ofrece tres acciones planas y mutuamente excluyentes. Todas leen el
estado ya publicado; no recorren el corpus, crean directorios o bases, migran
esquemas, reparan estado ni descargan modelos:

```powershell
Neocortex --knowledge-status
Neocortex --knowledge-status --knowledge-json
Neocortex --knowledge-search 'protección de transformador' --knowledge-limit 50
Neocortex --knowledge-context 'protección de transformador' --knowledge-limit 20 --knowledge-context-characters 24000
```

`--knowledge-status` captura los diez propietarios históricos y añade los
owners `archive` y `text` sólo cuando sus bases existen.
Si el directorio indicado por `--state-directory` no existe, informa cada
propietario como `absent`, devuelve `0` y deja la ruta sin crear. Search y
context compilan una consulta sobre los propietarios disponibles; con todo el
estado ausente devuelven `4` (parcial), no un falso “sin resultados”.
Si la ruta existe pero no es un directorio, o no puede abrirse y enumerarse en
lectura, Knowledge falla de forma cerrada: no la transforma en owners
`absent`, no emite un JSON engañoso y la CLI devuelve el código fatal `1` con
`KnowledgeStateRootError`. Esto incluye enlaces o reparse points cuyo destino
ya no existe y cambios de presencia de la raíz durante una captura. Un archivo
de owner sólo se declara `absent` cuando su path realmente no existe; si el
path existe pero es directorio, enlace roto o inaccesible, se aplica el mismo
fallo fatal. La inspección del sistema de archivos es síncrona: en una ruta UNC
o unidad de red desconectada, la cancelación sólo puede observarse cuando
Windows devuelve el control de `stat`/enumeración.

Las opciones de consulta son:

| Opción | Contrato |
|---|---|
| `--knowledge-limit N` | Predeterminado `20`. Search acepta `1..1000`; context, `1..100`. |
| `--knowledge-context-characters N` | Presupuesto máximo de ContextBundle. Predeterminado `12000`; context acepta `1..1000000`. |
| `--knowledge-mode evidence` | Predeterminado. En el canal semántico conserva la mejor coincidencia por `(item, entidad)` para no perder chunks o evidencias distintas. |
| `--knowledge-mode discovery` | En el canal semántico conserva la mejor coincidencia por item para una vista más colapsada. |
| `--knowledge-history` | Incluye revisiones `historical`/`superseded`, excluidas de forma predeterminada, y activa la ruta temporal del plan. |
| `--knowledge-json` | Emite el contrato JSON de la acción seleccionada en lugar de la presentación humana. |

El top-k solicitado es una ventana normal, no truncamiento. Cuando existen más
candidatos, `result_window_full=true` y `window_omitted_candidates` lo declaran,
pero `complete` puede seguir siendo verdadero y la CLI no convierte ese caso en
código 4. Sólo un corte duro —por ejemplo `max_vectors`— marca `truncated=true`,
propaga `next_cursor`/`cutoff_score` y vuelve parcial la respuesta. Si existen
hits, el compilador reserva primero una cita utilizable antes de diagnósticos.

Los aliases humanos `Neocortex status/search/ask` consumen estos contratos con
scopes fijos. No aceptan `--state-directory`; esa restricción evita que una GUI
o un agente elijan una base arbitraria.

Search y context exigen una consulta no vacía de hasta 4096 caracteres. Las
opciones limit/history/mode sólo se admiten con esas dos acciones;
`--knowledge-context-characters` exige context y se valida antes de ejecutar el
handler. `--knowledge-json` también se admite con status. Knowledge rechaza
`--apply`, `--route` y cualquier segunda acción directa. Ejemplos estructurados:

```powershell
Neocortex --knowledge-search 'IEC-61850' --knowledge-mode discovery --knowledge-json
Neocortex --knowledge-search 'protección de relevador' --knowledge-history --knowledge-limit 100
Neocortex --knowledge-context 'mantenimiento de interruptor' --knowledge-mode evidence --knowledge-json
```

La salida humana marca cada hit normal como
`location=physical inside_zip=0`. Los hits del owner Archive usan
`location=archive_member inside_zip=1` y muestran `container`, `member` y
`chain`; el JSON conserva los mismos datos en los identificadores de evidencia.

El snapshot es lógico, no una transacción distribuida. Si cambia durante las
dos observaciones se reintenta una vez el conjunto completo; un segundo cambio
se informa mediante código `5` en vez de presentar la vista como estable. Los
detalles de publicaciones y watermarks están en
[PERSISTENCE.md](PERSISTENCE.md).

### Conciliación de acciones inciertas

El conciliador de `file_actions` es acotado, paginado por keyset, idempotente y
de sólo lectura. No crea ni migra `framework.sqlite3` y nunca repite una
mutación:

```powershell
Neocortex --action-recovery-status --action-recovery-limit 100
Neocortex --action-recovery-status --action-recovery-after 250 --action-recovery-run 40
Neocortex --action-recovery-status --action-recovery-json
```

Sólo inspecciona estados `applying` y `recovery_required`. Clasifica cada efecto
como `confirmed`, `not_performed`, `ambiguous` o `impossible_to_check` y emite
una recomendación, sin modificar el estado. Los filtros y
`--action-recovery-json` exigen `--action-recovery-status`. El límite admitido
es 1..1000, `--action-recovery-after` no puede ser negativo y el run debe ser
positivo. El código es `2` únicamente si aparece una clasificación
`ambiguous`/`impossible_to_check` o si la consulta no puede abrir/validar el
estado; una página vacía o sólo confirmada/no realizada devuelve `0`.
`confirmed` sólo documenta que el efecto original parece ocurrido;
`not_performed` tampoco convierte la intención original en reutilizable.
Ninguna clasificación autoriza una nueva syscall. Una versión framework futura
o metadata de versión no canónica se rechaza con `2`.

`status` permanece estrictamente de sólo lectura. Para conservar una observación
en framework v19 use una operación `record` explícita y separada:

```powershell
Neocortex --action-recovery-record 42 --action-recovery-actor "Victor" --confirm-reconciliation-record
Neocortex --action-recovery-record 42 --action-recovery-actor "Victor" --confirm-reconciliation-record --action-recovery-json
Neocortex --action-recovery-record 42 --action-recovery-actor "operador-2" --action-recovery-expected-event 7 --confirm-reconciliation-record
```

`record` vuelve a clasificar la acción, abre sólo una base existente y agrega un
evento append-only con CAS, clave idempotente, actor, procedencia, firma y
evidencia. La confirmación autoriza la escritura SQLite y una migración aditiva
soportada de la base existente; nunca crea la base ni autoriza una mutación de
archivos. Repetir exactamente el mismo registro devuelve el mismo evento. Un
predecesor obsoleto o una observación incompatible se rechaza.

Un registro correcto de `ambiguous` o `impossible_to_check` devuelve `2` para
que la incertidumbre no quede oculta, aunque el evento sí haya sido confirmado
en SQLite. No existen todavía comandos `decide`, `authorize`, `recover` ni
`verify`, ni una decisión o autorización humana durable para una nueva
mutación. No intente emular esas fases cambiando filas o reutilizando una
autorización original.

### Plan de retención no destructivo

`--retention-status` inspecciona páginas acotadas de `semantic`, `catalog`,
`inventory` y `framework` sin crear, migrar, eliminar, hacer checkpoint o
ejecutar `VACUUM`:

```powershell
Neocortex --retention-status
Neocortex --retention-status --retention-store semantic --retention-min-age-days 30 --retention-batch-size 100
Neocortex --retention-status --retention-store semantic --retention-semantic-after 250 --retention-json
```

`--retention-store` puede repetirse. El lote permitido es 1..1000 y los cursores
`--retention-<store>-after` son keyset. Sin edad explícita, el plan informa
`policy_not_configured` y no declara filas elegibles por antigüedad. Conserva
siempre las publicaciones vigente y anterior, el último estado válido,
builders/leases vivos, cadenas base y evidencia humana o incierta; en particular
las referencias `semantic_evidence` y el último run `completed` de framework
actúan como holds. Los bytes son una cota inferior del payload `TEXT`/`BLOB`,
no espacio físico garantizado. Cada base tiene un snapshot estable, pero la
consulta no es atómica entre bases y una apertura SQLite read-only puede
participar en WAL/SHM. Devuelve `2` si algún store queda bloqueado por deriva o
dependencia incompatible; ausencia segura o un plan listo devuelve `0`.

No existen opciones `--retention-prepare`, `--retention-apply` o
`--retention-verify`. La salida de status no autoriza un `DELETE` manual ni
demuestra que todas las referencias cross-store hayan permanecido estables.

## Caché, selección y reintentos

La validación rápida de caché usa metadatos por defecto. Para volver a comprobar
bytes antes de reutilizar resultados se dispone de:

```powershell
Neocortex --root $Root --route pdf --pdf-cache-validation full
Neocortex --root $Root --route code --code-cache-validation full
```

`full` aumenta la E/S; no cambia la semántica del contenido ya validado. No hay
un comando público general para “limpiar toda la caché”. No borre bases, WAL o
SHM manualmente.

Code selecciona proyectos por defecto. Detecta sus raíces mediante manifiestos
fuertes y excluye archivos fuera de ellas, dependencias instaladas, caches y
salidas generadas antes de leer contenido:

```powershell
Neocortex --root $Root --route code
Neocortex --root $Root --route code --code-scope broad
```

El segundo comando es el override deliberado que restaura la selección textual
amplia anterior. `--code-generated` y `--code-vendored` permiten esas capas
dentro de proyectos; no son el valor predeterminado. Los campos
`code_project_scope`, `code_project_roots`, `code_outside_project_skips`,
`code_dependency_skips`, `code_generated_scope_skips` y `code_cache_skips`
explican la frontera aplicada.

Los errores permanentes o ya cacheados no se reintentan sólo por usar `--all`.
Los overrides explícitos son:

```text
--retry-pdf-errors
--retry-docx-errors
--retry-office-errors
--retry-archive-errors
--retry-text-errors
--retry-audio-errors
--retry-image-errors
--retry-code-errors
```

Use los filtros `--select-status`, `--select-error-type`,
`--select-recommendation`, `--select-path` y `--failed-pages-only` únicamente
con una ruta y un snapshot compatibles. Consulte la ayuda viva para rangos y
combinaciones exactos:

```powershell
Neocortex --help
```

## Salida JSON

No existe un `--json` global. Los contratos estructurados actuales se activan
por familia:

| Opción | Alcance |
|---|---|
| `--status-json` | `--status`; exige `--status`. |
| `--review-json` | Candidatos, decisiones y evidencia de revisión; emite JSON Lines determinista. |
| `--code-json` | Estado, manifest/frescura de autoanálisis, revisión top-10, búsquedas, proyectos o reconstrucción conceptual de código. |
| `--action-recovery-json` | JSON determinista por acción o evento; exige `--action-recovery-status` o `--action-recovery-record`. |
| `--retention-json` | Un documento JSON del plan dry-run; exige `--retention-status`. |
| `--archive-json` | Estado o resultados ZIP; exige exactamente una acción `--archive-*`. |
| `--knowledge-json` | Snapshot, resultado de búsqueda o contexto Knowledge; exige exactamente una acción `--knowledge-*`. |
| `inspect lineage --json` | Linaje owner-local y proyección causal acotada para el identificador; no migra estado. |
| `review value --json` | Consulta advisory schema `neocortex.value-review/v1`; con `--refresh`, avance de una página Framework schema `neocortex.value-review-refresh/v1`, sin mutación del corpus. |
| `doctor capabilities --json` | Reporte agregado schema 1; con `--select text.extract --mime-type MIME --input-bytes BYTES`, selección explicable schema `neocortex.capability-selection/v1`. |
| `doctor platform --json` | Un documento JSON versionado de política y capacidades de plataforma. |
| `models prepare/status --json` | Un documento JSON versionado del conjunto de modelos gestionados. |

No combine una opción JSON con una operación de otra familia. La salida humana
puede evolucionar; para automatización use sólo el contrato JSON correspondiente
y compruebe siempre el código de salida.

## Códigos de salida

| Código | Contrato observado |
|---:|---|
| `0` | Ayuda/versión o ejecución/consulta completada según su contrato. |
| `1` | Excepción fatal no normalizada o fallo interno del worker de GUI. No es el código de una validación ordinaria de argumentos. |
| `2` | Error de argumentos detectado por `argparse` o por la validación posterior, como una combinación incompatible o una solicitud Linux de `--apply`/`--organization-apply`; abstención explicable de `doctor capabilities --select`; estado requerido ausente o incompatible; diagnóstico fallido —incluida abstención total de `--code-status`/`--code-review` ante sidecars, cerca inestable o publicación no elegible—; generación Semantic incompleta, truncada o no publicada; error de acciones u organización; conciliación con efecto ambiguo/imposible —incluso si su evento fue registrado—; plan de retención bloqueado; o, con `--strict-exit-codes`, errores/parciales retenidos por una ruta. El watcher también devuelve `2` si conserva corridas fallidas o errores de fuente. |
| `3` | Knowledge terminó con snapshot estable y cobertura completa, pero search/context no obtuvo evidencia; Archive search/list también lo usa cuando no hay miembros coincidentes; `inspect lineage` no encontró el identificador. |
| `4` | Knowledge produjo una respuesta parcial o no soportada; incluye propietarios necesarios ausentes. `inspect lineage` encontró evidencia incompleta, legacy o truncada. `review value` también lo usa para una cola parcial o `stale`. |
| `5` | El snapshot Knowledge volvió a cambiar durante el único reintento global acotado, o el refresh ReviewTask observó un cambio antes de publicar. |
| `6` | Knowledge status encontró un schema futuro/incompatible; en search/context, uno de esos owners figura en `blocking_owners` y obliga a abstenerse. `inspect lineage` lo usa cuando el schema incompatible impide toda resolución; si otro owner aporta evidencia válida, devuelve `4 partial` con warning. |
| `7` | Knowledge status detectó una base corrupta; en search/context, la base figura en `blocking_owners` y obliga a abstenerse. `inspect lineage` también lo usa ante corrupción SQLite. |
| `130` | Cancelación por teclado o cancelación del watcher. |
| otro no cero | Fallo no normalizado. Trátelo como fatal y preserve la evidencia. |

Para las acciones Knowledge la precedencia es `7`, `6`, `5`, `4`, `3`, `0`.
`status` aplica integridad y compatibilidad al snapshot completo; search/context
las elevan sólo cuando el owner severo aparece en `blocking_owners`, por lo que
una base ajena no oculta evidencia válida. El status con propietarios
simplemente ausentes conserva `0`; la ausencia pasa a `4` cuando impide
completar search/context.

Sin `--strict-exit-codes`, errores de documentos individuales pueden quedar
registrados aunque la corrida general termine con `0`. Automatice primero una
ruta acotada; `--all` se reserva para cuando cada ruta y su costo ya fueron
aceptados:

```powershell
Neocortex --root $Root --route pdf --MaxCount 25 --strict-exit-codes
```

## Operaciones que requieren autorización explícita

Esta sección describe exclusivamente el backend seguro de Windows. En Linux,
`--apply` y `--organization-apply` se rechazan antes de validar la raíz o crear
estado con código `2` y razón estable
`linux_mutation_backend_unavailable`. Inventario, procesamiento, catálogo y
búsqueda permanecen disponibles; no se usa `Path.rename` como sustituto.

`--apply` permite que una corrida integrada ejecute únicamente las mutaciones
que satisfacen el contrato físico de `0.9.0`. Los rename de extensión y los
movimientos de organización requieren NTFS local, mismo volumen, archivo
regular con un único hard link, ausencia de reparse y operación ligada a handles
retenidos con *no-replace*. UNC, otros filesystems, directorios, múltiples hard
links y movimientos entre volúmenes se abstienen.

Los candidatos de Papelera (duplicados, vacíos y PDF irrecuperables) se siguen
planeando en dry-run, pero su aplicación está deshabilitada porque la API
disponible opera por ruta. Con `--apply` terminan `skipped` con evidencia de
abstención; no se invoca `Send2Trash`. No hay flag para degradar a la operación
path-bound.

La organización persistida dispone además de una autorización directa distinta:

```powershell
Neocortex --organization-apply --organization-max-actions 100
```

Un plan que cruzó la frontera nativa sin confirmación queda
`recovery_required`, reserva su destino y no vuelve a seleccionarse para
aplicación. Se consulta sin mutar con:

```powershell
Neocortex --organization-preview 100 --organization-preview-status recovery_required
```

No copie estos comandos como prueba de instalación. Antes de cualquiera de las
dos autorizaciones, revise [SECURITY.md](SECURITY.md) y
[RECOVERY.md](RECOVERY.md), cree un backup SQLite consistente y confirme la raíz
y los planes. El watcher y `--route-only` rechazan `--apply`.

## Operaciones con otros efectos laterales

- `models prepare` descarga explícita y secuencialmente los modelos gestionados;
  `models status` no descarga ni crea rutas.
- `--semantic-prepare-models` adquiere o carga explícitamente modelos.
- En Linux, audio es local-only por defecto y usa Whisper CPU/int8 del cache
  compartido; no descarga implícitamente durante una ruta.
- En Windows, una primera ruta de audio puede descargar el modelo Whisper salvo
  que se use `--audio-local-models-only`.
- `--semantic-index`, `--semantic-classify`, `--catalog-documents`,
  `--organization-plan`, `--review-record` y `--review-evidence-sync` escriben
  estado, aunque no muten archivos originales.
- Semántica y catálogo construyen staging invisible y sólo cambian su
  generación publicada mediante una transacción CAS completa.
- `--watch` permanece en primer plano hasta cancelarse y genera nuevas corridas
  integradas. Usa USN como señal cuando existe; de otro modo ejecuta inventario
  normal portable cada `--watch-portable-interval-seconds` (300 por defecto).

Consulte [OPERATIONS.md](OPERATIONS.md) antes de usar watcher, reanudación,
límites de recursos o mantenimiento.
