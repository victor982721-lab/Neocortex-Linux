# Guía de la interfaz de línea de comandos

La interfaz pública canónica de NeoCortex es el ejecutable instalado
`Neocortex`. La definición exacta de argumentos vive en
`_04_Nucleo_Operativo/cli_parser.py`; esta guía resume los contratos operativos
que conviene conocer antes de usar `--help`.

## Comandos cotidianos

| Necesidad | Comando |
|---|---|
| Estado general | `Neocortex --status` |
| Disponibilidad de Knowledge | `Neocortex --knowledge-status --knowledge-json` |
| Buscar evidencia | `Neocortex --knowledge-search "consulta" --knowledge-limit 20` |
| Compilar contexto citado | `Neocortex --knowledge-context "consulta" --knowledge-limit 12` |
| Buscar en un owner concreto | `Neocortex --pdf-search "consulta"` o `Neocortex --code-search "consulta"` |

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

Esta fuente declara `0.7.2`. En Windows, la fuente canónica está en
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
| `audio` | Audio y pistas de vídeo admitidas mediante Whisper. |
| `image` | Clasificación y evidencia de imágenes. |
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
separado `self-analysis`. Después ejecuta las seis rutas del corpus y, si no
hubo errores de acciones u organización, avanza Semantic textual sobre los
caches disponibles de PDF, DOCX, XLSX, PPTX, ODT y audio. Sus límites
integrados son 100 000 items, 1 000 000 de jobs y 172 800 segundos. Una
truncación limpia se informa como progreso reanudable y conserva exit `0`;
errores o estado stale conservan exit `2`. Code sólo participa en Semantic
cuando se selecciona expresamente con `--semantic-source code`, para no
reintroducir inventarios de millones de chunks en la publicación documental
cotidiana.

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
`C:\Users\Victor\Neocortex\Repository`; cualquier otra raíz se rechaza antes de
crear un run. Este perfil sí ejecuta código declarado del proyecto, pruebas y
`conftest.py`, por lo que debe usar estado aislado:

```powershell
$Root = 'C:\Users\Victor\Neocortex\Repository'
$State = 'C:\Users\Victor\Neocortex\Laboratory\self-analysis\trusted-deep'
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
envelope `neocortex.code-review/v10` conserva compatibilidad con v2-v9 y
la proyección Ruff legacy —hasta 10 hotspots brutos por defecto y tres
recomendaciones `act_now`—. Añade `external_evidence_suite` sin modificar el
ranking, además de un `work_package`
determinista con una sola recomendación raíz, guards alcanzados por llamadas
confirmadas a uno o dos saltos, riesgo agregado, módulo primario, contratos
afectados, cadenas de imports acotadas, pruebas protectoras observadas, líneas y
ramas faltantes, evidencia y gates de supply chain, pasos y gates. Los gates
profundos son `tests_passed`,
`coverage_available`, `work_package_target_protected`,
`line_coverage_not_degraded` y `branch_coverage_not_degraded`; ausencia o falta
de comparabilidad nunca aprueba uno. El planificador v4 conserva como máximo un
paquete de mantenimiento y puede entregar, de forma independiente, hasta tres
paquetes `unused_characterization`. Sólo los crea para
`probable_unused_high_consensus` cuando pasan los gates de precisión de
calibración y holdout; exigen revisión dinámica, pruebas y confirmación humana,
y declaran `mutation_authority=false`. El work package enlaza además el perfil
y los gates de `engineering_analytics`; en la validación H6 su objetivo fue
`_04_Nucleo_Operativo.external_deep_coverage` /
`external_deep_coverage._normalize`. El pool del
planificador siempre es el top 50, independientemente de la vista; no
agrupa por nombre o directorio y los guards exigen caracterización antes de
cualquier cambio. `--code-review-limit N --code-json` permite inspeccionar entre
1 y 50 hotspots; valores mayores a 10 exigen JSON. No admite `--apply`, `--route`
ni otra operación directa. Un snapshot full completado con USN indisponible
sigue siendo consultable como `freshness=publication_only` y `current=false`;
journal avanzado/discontinuo, manifest inválido o vínculo de raíz/framework
incompatible devuelve `2` sin crear estado.

`--code-publication-diff BASELINE_STATE` compara ese baseline con el owner Code
de `--state-directory`. Es estrictamente read-only y falla cerrado si falta un
run completado, el schema no coincide o existe cualquier sidecar SQLite. El
envelope `neocortex.code-publication-diff/v9`, compatible con v1-v8, informa
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

La GUI supervisa el mismo orquestador y expone PDF, DOCX, Office, audio, imagen
y Code. En Linux muestra “modo portátil Linux”, no solicita elevación y
desactiva los controles de mutación; inventario, procesamiento y búsqueda se
conservan. El worker `--gui-worker` es un contrato interno y no debe invocarse
manualmente.

## Consultas y diagnósticos sin recorrido

Los siguientes ejemplos no inician un inventario ni autorizan mutaciones del
corpus:

```powershell
Neocortex --status
Neocortex --status --status-run 40 --status-json
Neocortex doctor capabilities
Neocortex doctor platform
Neocortex doctor platform --json
Neocortex models status
Neocortex models status --json
Neocortex --pdf-doctor
Neocortex --pdf-verify
Neocortex --audio-doctor
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
En el contrato exacto Jina/body de PDF y Code, resultados por debajo de sus
pisos medidos (`0.50` y `0.46`) aparecen como abstenciones y la salida agrega
`calibrated_abstentions`. Son filtros de recuperación por owner, no confianza ni
probabilidad; otros contratos conservan su etiqueta no calibrada. Si un vector
fue reutilizado por contenido exacto, el contrato se toma de su
`payload_provenance`; valores contradictorios no reciben el piso.

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
el basename durable sin directorios ni extensión final con peso `0.5`. Ambos
declaran peso y procedencia, comparten una sola vectorización de consulta y el
resultado fusionado conserva de preferencia el snippet corporal. El título es
advisory: no participa en clasificación ni evidencia materializada. Knowledge
`evidence` lo excluye; Knowledge `discovery` puede usarlo sólo para reforzar un
recurso y revisión que ya tengan evidencia corporal, nunca como cita. Un head
anterior a esta política mantiene la búsqueda corporal y declara
`title_channel_not_indexed` hasta ser republicado de forma acotada.

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

### Knowledge Plane de sólo lectura (`0.7.2`)

Knowledge ofrece tres acciones planas y mutuamente excluyentes. Todas leen el
estado ya publicado; no recorren el corpus, crean directorios o bases, migran
esquemas, reparan estado ni descargan modelos:

```powershell
Neocortex --knowledge-status
Neocortex --knowledge-status --knowledge-json
Neocortex --knowledge-search 'protección de transformador' --knowledge-limit 50
Neocortex --knowledge-context 'protección de transformador' --knowledge-limit 20 --knowledge-context-characters 24000
```

`--knowledge-status` captura el estado lógico acotado de los diez propietarios.
Si el directorio indicado por `--state-directory` no existe, informa cada
propietario como `absent`, devuelve `0` y deja la ruta sin crear. Search y
context compilan una consulta sobre los propietarios disponibles; con todo el
estado ausente devuelven `4` (parcial), no un falso “sin resultados”.
Si la ruta existe pero no es un directorio, o no puede abrirse y enumerarse en
lectura, Knowledge falla de forma cerrada: no la transforma en diez owners
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
| `--knowledge-json` | Snapshot, resultado de búsqueda o contexto Knowledge; exige exactamente una acción `--knowledge-*`. |
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
| `2` | Error de argumentos detectado por `argparse` o por la validación posterior, como una combinación incompatible o una solicitud Linux de `--apply`/`--organization-apply`; estado requerido ausente o incompatible; diagnóstico fallido —incluida abstención total de `--code-status`/`--code-review` ante sidecars, cerca inestable o publicación no elegible—; generación Semantic incompleta, truncada o no publicada; error de acciones u organización; conciliación con efecto ambiguo/imposible —incluso si su evento fue registrado—; plan de retención bloqueado; o, con `--strict-exit-codes`, errores/parciales retenidos por una ruta. El watcher también devuelve `2` si conserva corridas fallidas o errores de fuente. |
| `3` | Knowledge terminó con snapshot estable y cobertura completa, pero search/context no obtuvo evidencia. |
| `4` | Knowledge produjo una respuesta parcial o no soportada; incluye propietarios necesarios ausentes. |
| `5` | El snapshot Knowledge volvió a cambiar durante el único reintento global acotado. |
| `6` | Knowledge status encontró un schema futuro/incompatible; en search/context, uno de esos owners figura en `blocking_owners` y obliga a abstenerse. |
| `7` | Knowledge status detectó una base corrupta; en search/context, la base figura en `blocking_owners` y obliga a abstenerse. |
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
que satisfacen el contrato físico de `0.7.2`. Los rename de extensión y los
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
