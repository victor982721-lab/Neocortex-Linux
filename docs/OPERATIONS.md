# Guía operativa

Esta guía cubre ejecución normal, watcher, recursos, cancelación, diagnóstico y
mantenimiento. La estructura de componentes se describe en
[ARCHITECTURE.md](ARCHITECTURE.md) y los propietarios y versiones de las bases
en [PERSISTENCE.md](PERSISTENCE.md). La consulta cross-owner de solo lectura se
documenta en [KNOWLEDGE.md](KNOWLEDGE.md); no se duplican esos contratos aquí.

## Flujo personal recomendado

Éste es el flujo predeterminado; las secciones posteriores son referencia
cuando una frontera concreta lo requiera:

1. Preflight read-only: versión, capacidades, estado general y estado del
   subsistema implicado.
2. Una sola ruta y una muestra representativa de 20–50 elementos, con límite
   duro de 10–15 minutos.
3. Una salida que Victor pueda usar: búsqueda, evidencia, clasificación o
   preview; registre errores, tiempo y throughput.
4. La misma corrida una segunda vez para probar caché, reanudación e
   incrementalidad.
5. Una búsqueda o revisión real y una proyección antes de escalar.

Para el preflight y la consulta cotidiana prefiera la fachada corta:

```bash
Neocortex status --scope all
Neocortex search "consulta representativa" --scope personal
Neocortex ask "pregunta concreta" --scope personal
Neocortex review value --scope personal
```

Esos comandos no crean o migran estado. En cambio una corrida sin `--apply`
preserva el corpus, pero sí escribe inventario y cachés.

Si el piloto falla o excede el límite, deténgalo y corrija la causa. `--all`, un
watcher, una indexación Semantic completa, una migración, un rollback o una
auditoría integral no son el punto de partida.

## Bootstrap autenticado de pip

Antes de cualquier instalación Python, los flujos locales mantenidos ejecutan el
helper independiente del `pip` ambiental:

```bash
python -I tools/bootstrap_pip.py
```

El helper descarga únicamente el wheel oficial fijado de `pip 26.1.2`, valida
su nombre y SHA-256 antes de ejecutarlo, instala con aislamiento, sin índice ni
dependencias, y verifica la versión exacta. `--wheel` permite entregar ese mismo
artefacto ya descargado en un flujo offline; no relaja la autenticación.

## Condiciones previas

1. Confirme que no haya otra ejecución de NeoCortex usando el mismo directorio
   de estado.
2. Verifique el launcher y la ayuda:

   ```bash
   Neocortex --version
   Neocortex --help
   ```

   Esta guía corresponde a la fuente `0.9.0`. Si `--version` no existe o no
   informa `0.9.0`, el launcher operativo no coincide con esta entrega: no use
   sus contratos nuevos sobre estado real hasta validar el artefacto correcto.
   En Linux añada `Neocortex doctor platform --json` y confirme
   `compatible=true`, inventario portable y contención POSIX antes de abrir
   estado real.

3. Confirme la raíz exacta y que no sea un symlink, junction o punto de
   reanálisis.
4. El recorrido portable funciona sin USN. Para probar su acelerador opcional,
   use un volumen NTFS local y los permisos de lectura ya disponibles; no eleve
   la corrida cotidiana sólo para habilitarlo. Las rutas UNC y otros sistemas de
   archivos no ofrecen identidad/USN equivalentes, pero sí pueden usar el
   baseline portable si cumplen el resto de las protecciones de la raíz.
5. Antes de una actualización, migración o acción sobre archivos, siga
   [RECOVERY.md](RECOVERY.md).

En Linux, fuente, estado y release permanecen separados:

```text
Fuente:    ~/Neocortex/Repository
Corpus:    ~/Documentos/NeoCortex/Corpus
Release:   ~/.local/share/Neocortex/releases/<release-id>
Activa:    ~/.local/share/Neocortex/current
Launcher:  ~/.local/share/Neocortex/bin/Neocortex
Alias:     ~/.local/bin/Neocortex
Estado:    ~/.local/state/Neocortex/state
Modelos:   ~/.local/share/Neocortex/models
```

`XDG_CONFIG_HOME`, `XDG_STATE_HOME` y `XDG_DATA_HOME` sustituyen sus defaults
cuando están definidos. Consulte [LINUX_KUBUNTU.md](LINUX_KUBUNTU.md).

## Flujo normal no mutador del corpus

Una corrida sin `--apply` puede leer el corpus y escribir inventario, cachés,
eventos y planes; no es una consulta de sólo lectura. Empiece con un conjunto
acotado:

```bash
$Root = 'C:\Datos'
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "La raíz no existe o no es un directorio: $Root"
}
Neocortex --root $Root --route pdf --MaxCount 25 --strict-exit-codes
```

Equivalente Linux, siempre sin flags de mutación:

```bash
Root="$HOME/Documentos/NeoCortex/Pilot"
test -d "$Root"
Neocortex --root "$Root" --route pdf --MaxCount 25 --strict-exit-codes
```

La frontera normal captura `InternalPathsPolicy`: una raíz situada dentro del
repositorio, runtime, datos de aplicación o laboratorio interno se rechaza; si
esos árboles son descendientes del corpus se excluyen del inventario. El estado
no puede ser igual ni ancestro del corpus, porque esa exclusión podaría la raíz
completa. Framework persiste la firma efectiva que combina la firma cruda de
exclusión con la identidad de esas rutas internas.

Después inspeccione la ejecución:

```powershell
Neocortex --status
Neocortex --status --status-json
```

Después de aprobar cada ruta por separado se puede probar una lista aún
acotada. `--all` selecciona PDF, DOCX, Office, ZIP, texto/correo, audio, video,
imagen y código, actualiza el catálogo técnico y se reserva para cuando exista una
proyección aceptada. Al final avanza Semantic sobre las cachés documentales y,
si existe la caché de imagen, también sobre CLIP visión/OCR; Code requiere
`--semantic-source code` explícito.
Antes de esa etapa, reutiliza el servicio de autoanálisis protegido sobre el
checkout canónico y su estado separado. Si el corpus no está disponible, ese
autoanálisis se conserva y el comando devuelve `2` con
`corpus_unavailable`, sin traceback:

```powershell
Neocortex --root $Root --route pdf,docx --MaxCount 25 --docx-max-count 25 --strict-exit-codes
```

No use una corrida amplia como prueba de instalación. Ayuda, versión y doctors
son la barrera inicial apropiada.

### Piloto ZIP y replay

Un piloto de ZIP debe fijar cantidad y conservar el estado fuera de la muestra:

```bash
Neocortex --root "$Root" --state-directory "$State" --route archive \
  --archive-max-count 25 --strict-exit-codes
Neocortex --state-directory "$State" --archive-status
Neocortex --state-directory "$State" --archive-search "término representativo"
```

Repita exactamente el primer comando. El segundo resumen debe informar los ZIP
seleccionados como `cache_hits`, sin volver a descomprimirlos. Compruebe una
ruta profunda como `contenedor.zip!/otro.zip!/documento.txt` y confirme
`inside_zip=1`. Una incidencia de traversal, cifrado, symlink, duplicado,
profundidad o expansión deja el contenedor `partial` y el miembro inseguro sin
leer; no se relajan límites para convertir ese resultado en éxito. La ruta no
extrae archivos, no usa `--apply` y no organiza físicamente el corpus.

Incluya en el fixture al menos un PDF con texto nativo, un PDF escaneado y una
imagen con texto dentro de un ZIP anidado. Con `--ocr auto`, las páginas con
menos de 40 caracteres nativos y las imágenes admitidas pasan por el worker
aislado. Compruebe que una dependencia/idioma OCR ausente se informe como
incidencia y que el miembro siga visible, en lugar de aceptar texto ficticio.

### Piloto de texto, EML y Office heredado

```bash
Neocortex --root "$Root" --state-directory "$State" --route text \
  --text-max-count 25 --strict-exit-codes
Neocortex --state-directory "$State" --knowledge-status
Neocortex --state-directory "$State" --knowledge-search "término representativo"
Neocortex --state-directory "$State" --catalog-preview 25
```

La muestra debe combinar texto plano/Markdown, CSV o TSV, HTML/XML/JSON, un EML
multipart y DOC/XLS/PPT reales. Repita el productor: el segundo resumen debe
convertir los documentos sin cambios en `cache_hits`. El asunto del EML debe
aparecer como título/nombre sugerido cuando sea más útil que el basename. Un
Office heredado sin LibreOffice ni fallback local queda como error explícito;
no se intenta interpretar el CFB como texto plano.

Para explicar causalmente un resultado Text ya publicado, use la identidad
exacta que devolvió Knowledge o search; no derive un ID desde la ruta:

```bash
Neocortex knowledge health 'resource:file:1:2:-1' \
  --scope personal --json
```

La consulta selecciona Text o PDF por identidad y evidencia publicada, nunca
por ruta/extensión; lee Inventory, owner fuente, Catalog y Knowledge dos veces y
reintenta una sola vez si cambia el snapshot. `healthy` exige los facts
aplicables completos, publicados y causalmente alineados. Para PDF schema 13
verifica además estado tipado, páginas, staging, errores, FTS y recovery
estructural reconocido. Un mismatch, owner ausente, schema futuro/corrupto,
publicación incompleta, WAL activo o segundo cambio produce un estado acotado o
abstención; nunca repara el estado. Esta vertical cubre sólo Text/PDF y no
demuestra contenido/OCR, verdad semántica, calidad visual, otros owners ni
pérdida de energía.

## Autoanálisis de código en laboratorio

Use un mini-root sintético y un estado hermano, nunca contenido dentro de la
raíz analizada:

```powershell
$Lab = Join-Path $env:LOCALAPPDATA 'Neocortex\self-analysis\fixtures'
$MiniRoot = Join-Path $Lab 'mini-root'
$MiniState = Join-Path $Lab 'mini-state'

Neocortex --self-analysis --root $MiniRoot --state-directory $MiniState
Neocortex --state-directory $MiniState --code-status --code-json
```

Ese comando usa el perfil `protected`. Para una raíz explícitamente confiable,
el piloto del perfil estático es:

```powershell
Neocortex --self-analysis --analysis-profile trusted-static --root $MiniRoot --state-directory $MiniState
Neocortex --state-directory $MiniState --code-review --code-review-limit 10 --code-json
```

La segunda invocación es el resultado consumible del auditor. Debe emitir
`neocortex.code-review/v22`, enlazar cada evaluación a evidencia publicada,
mantener `recommendations=[]`, `decision=null` y `mutation_authority=false`, y
explicar por pregunta qué provider, contraevidencia o experimento falta. Un run
de proveedores por sí solo no constituye el cierre del autoanálisis.

Si el review publica `CODE_EXPERIMENT_PROPOSAL`, el operador puede copiar su ID
exacto y ejecutar sólo ese experimento con `--code-experiment-run`. No se admite
selector pytest ni comando arbitrario: los registries runtime/template v11 fijan escenarios, sus nodeids
parametrizados ya expandidos, timeout y gates tipados. El comando vuelve a
validar el plan, la raíz canónica y el manifest actual. Actualmente ejecuta sólo
`architecture.declared_import_contract_acceptance` (tres nodeids y cuatro
gates), `capability.public_route_acceptance` (un nodeid),
`state.runtime_sql_trace` (cuatro nodeids),
`state.semantic_process_death_recovery` (un nodeid y tres gates),
`evolution.code_schema_upgrade_matrix` (cinco nodeids y cuatro gates),
`retention.durable_hold_safety` (catorce nodeids exactos y cuatro gates),
`security.bounded_boundary_scenarios` (diez nodeids y siete gates),
`framework.review_task_protocol_acceptance` (ocho nodeids y cinco gates),
`interfaces.public_cli_contract_acceptance` v3 (veintiséis nodeids y cinco gates) y
`knowledge.asset_health_causal_acceptance` (doce nodeids y cuatro gates), y
`knowledge.pdf_asset_health_causal_acceptance` (doce nodeids y cuatro gates,
5/3/3/1). El
template arquitectónico liga el diff Python productivo a la pregunta exacta de
contratos de imports y abstiene si no puede cerrar el receipt o su disposición
técnica; sus controles no prueban dispatch dinámico ni intención arquitectónica
completa. ReviewTask usa estado XDG/SQLite temporal y la ruta pública
`show/claim/decide/history`; sus fallos inyectados no prueban power loss y el
actor sintético declarado no equivale a identidad humana autenticada. Pytest corre sobre el checkout
canónico confiable; el temporal externo aloja runtime/checkpoints, no una copia
ni un sandbox. Trusted-deep puede usar red y conserva `HOME`.
La matriz CLI v3 cubre ayuda/traducción, dispatch, rechazo acotado y lectores
focales; no cada handler, GUI/MCP/worker o efecto externo. Las matrices Health
usan fixtures Text/PDF bajo `pytest_tmp_path`; la PDF liga nueve relaciones de
contraevidencia y doce para el resultado completo. No generalizan a todos los
owners, contenido/OCR, fidelidad visual/semántica ni power loss.

Antes y después de Pytest, el provider vuelve a calcular la firma exacta de los
inputs Python publicados y del soporte Git observado; una diferencia rechaza el
receipt. Un fence Linux before/after compara identidad, sidecars y anclas
acotadas de `code.sqlite3` durante la ejecución sin releer todo su historial.
Estas barreras no son un lock continuo de la fuente y no incluyen el
corpus ni otros stores. Si los nodeids y gates terminan `passed`, retorna `0`;
`failed` o `abstained` retornan `2`. Después, aun en esos estados terminales, la
CLI inserta un receipt append-only en Code schema v6 y devuelve
`neocortex.code-experiment-store/v1` con
`neocortex.code-experiment-receipt/v3` anidado. Por eso esta operación no es
read-only y el digest unchanged no incluye la escritura posterior. No la use
sobre una publicación stale: regenere antes el autoanálisis. No la ejecute sobre
código que no confíe; la allowlist limita el selector, no los efectos del código
de tests.

El siguiente review v22 enlaza sólo el terminal más nuevo del proposal y la
processing signature exactos, con bindings de gates registrados. El terminal
puede pertenecer a un run Code completado anterior cuando el run vigente es un
replay exacto con la misma firma. El envelope digest liga y verifica run,
evaluación, pregunta, sujeto, review, timestamp y payload. Un terminal posterior
fallido o abstenido, o uno stale, corrupto o sin binding, no satisface evidencia.
Incluso con evidencia completa, el receipt no suplanta a un actor humano. El
verificador técnico allow-listed v6 de v22 puede publicar
`no_change_required_within_verified_scope` sólo tras volver a comprobar el
contrato exacto, sus gates y controles negativos. Esa disposición es advisory,
expone riesgos residuales, no genera recomendación y no concede autoridad de
mutación; preguntas sin política exacta quedan `unresolved`.

### Pregunta focal y observabilidad de almacenamiento Code

Para consultar la única pregunta que hoy tiene lector focal, sin construir el
review global:

```bash
Neocortex code question \
  structure.static_cli_calls_require_runtime_contract_evidence \
  --limit 10 --json
```

La salida debe indicar `source_surface=interface_surface`, preservar evidencia
advisory y no crear estado. El lector exige un último run Code completado,
publicación elegible y cercas estables. Una pregunta distinta devuelve
`unsupported` con fallback `automatic=false`; el operador decide si después
ejecuta una consulta global. No interprete ese fallback como resultado de
review ni como cobertura de otra familia.

Para medir forma y crecimiento del owner sin escribirlo:

```bash
Neocortex code storage --run-limit 20 --row-scan-limit 250000 \
  --retain-runs 5 --json
```

Detenga primero los writers. El reader immutable abstiene ante base ausente,
schema no vigente, sidecars incompatibles o cambio de fence. Los conteos que
alcanzan el límite son cotas inferiores y el delta compara filas externas de
dos runs completados, no bytes históricos de toda la base. `--retain-runs` es
una simulación `preview_only`; no ejecute `DELETE`, prune, `VACUUM`, checkpoint
ni eliminación de WAL/SHM a partir de esa vista.

`trusted-static` ejecuta 13 proveedores independientes: Ruff basic, Ruff
con la política acotada `E4,E7,E9,F,B,C4,PIE,RUF`, Mypy, Pyright, Ruff Analyze,
Grimp, Complexipy, Vulture, Semgrep, Deptry, pip-audit, inventario del entorno
instalado e historial Git local. Las familias
Ruff `I,PT,SIM,UP` quedan fuera para priorizar defectos y mantenibilidad sobre
estilo/modernización. No escale si status muestra un
proveedor `abstained`/`not_recorded`, cobertura incompleta o una limitación que
impida interpretar el resultado. La falta de Pyright no invalida la evidencia
Ruff/Mypy, pero deja el consenso de tipos `not_comparable`.

Ruff Analyze es el oráculo diferencial del grafo de imports; Grimp es el
productor de relaciones, SCC y contratos, y Complexipy produce complejidad
cognitiva. El status arquitectónico debe mostrar
`import_graph_consensus`, `architecture_contracts` y
`module_complexity_displacement`. En la primera publicación, las dimensiones
que necesitan comparación permanecen `baseline` o `not_evaluated`; sólo un diff
comparable permite aprobar que no hubo degradación o desplazamiento.

### Supply chain y dependencias

Los cuatro proveedores de Hito 5 son observacionales y están integrados en el
mismo status, review, publication diff y work package:

- `semgrep-neocortex-invariants` ejecuta tres reglas locales versionadas, sin
  métricas remotas ni autofix, y excluye únicamente sus fixtures propios del
  gate del proyecto;
- `deptry-project-dependencies` correlaciona imports con dependencias runtime,
  desarrollo y opcionales de `pyproject.toml`;
- `pip-audit-known-vulnerabilities` consulta PyPI para crear un snapshot
  fechado, sin descripciones ni fixes; un replay exacto vigente reutiliza ese
  snapshot sin red;
- `installed-package-inventory` verifica constraints, metadata de licencia y
  hashes/tamaños `RECORD` del entorno donde corre `Neocortex`.

Después de publicar, consulte `supply_chain` en `--code-status --code-json` y
`--code-review --code-json`. Debe distinguir cuatro categorías —higiene de
dependencias, vulnerabilidades conocidas, integridad e inventario de
licencias— y seis gates explícitos. Un snapshot vencido, un `RECORD` alterado o
un proveedor ausente obliga a abstener la dimensión; un finding no autoriza
actualizar, desinstalar, editar ni aplicar fixes. Para comparar:

```powershell
Neocortex --state-directory $State --code-publication-diff $BaselineState --code-json
```

La comparación sólo declara deltas cuando proveedor, versión, configuración y
frescura permiten hacerlo. Un baseline anterior a Hito 5 queda honestamente
incomparable en supply chain.

### Consenso de código potencialmente no usado

`vulture-unused-static` pertenece a `trusted-static` y también se conserva en
`trusted-deep`. Analiza las copias verificadas del inventario Python sin cargar
configuración del proyecto, ejecutar contenido, usar red ni aplicar fixes. Sus
findings son candidatos heurísticos; nunca son prueba suficiente de no uso.

Después de publicar, consulte `--code-status --code-json` y revise
`unused_analysis`. El consumidor exige Vulture y Pyright listos y correlaciona
grafo, imports, reexports, `__all__`, callbacks, registries, fixtures, entry
points, Protocols y Coverage disponible. La precedencia operativa es:

1. evidencia de uso observada → `explained_usage`;
2. contrato dinámico plausible → `dynamic_usage_possible`;
3. Vulture/Pyright completos y alineados, confianza Vulture ≥ 0.90 y ninguna
   evidencia de uso → `probable_unused_high_consensus`;
4. cualquier brecha restante → `insufficient_evidence`.

Calibration y holdout son fixtures etiquetados independientes. Status y review
deben publicar precision, recall, abstención, denominadores, firmas y gates por
separado; no mezcle ambos conjuntos ni presente sus métricas como probabilidad
de defecto. Si un gate de precisión no pasa, no se crean paquetes de
caracterización. Si pasa, review puede crear como máximo tres paquetes
`unused_characterization`; cada uno exige revisión de usos dinámicos, pruebas,
confirmación humana y replay comparable. `mutation_authority=false` y la
ausencia total de autoridad de borrado se mantienen incluso para consenso alto.

El publication diff sólo compara esta dimensión cuando coinciden proveedor,
policy, calibración y holdout. Un candidato de consenso alto nuevo falla su gate
observacional para exigir revisión; no autoriza editar ni borrar. Coverage puede
explicar una ejecución observada, pero la falta de cobertura nunca fortalece la
hipótesis de no uso.

### Consulta unificada sin reanálisis

Use `--code-query` para filtrar `status`, `review` o `diff` ya publicados sin
abrir una nueva corrida ni escribir los owners:

```powershell
Neocortex --state-directory $State --code-query status --code-query-provider $Provider
Neocortex --state-directory $State --code-query review --code-query-module $Module --code-query-work-package $WorkPackage --code-json
Neocortex --state-directory $State --code-query diff --code-query-baseline $BaselineState --code-query-delta added --code-json
```

Puede repetir `--code-query-provider`, `--code-query-category`,
`--code-query-module`, `--code-query-status`, `--code-query-delta` y
`--code-query-work-package`. La consulta aplica OR dentro de cada dimensión y
AND entre dimensiones; un módulo incluye coincidencia exacta y descendientes.
El límite es 1–500, con 50 por defecto. El baseline se exige exclusivamente
para `diff`. Una publicación ausente, incompatible o no comparable produce su
abstención/limitación; no dispara autoanálisis, migraciones ni rutas alternas.
La salida sigue siendo advisory y multidimensional, sin score agregado,
probabilidad de defecto ni autoridad de mutación.

### Perfil `trusted-deep`

Úselo sólo para ejecutar la suite declarada de Neocortex y correlacionar pruebas,
mutación focal e historia con líneas, ramas, símbolos, módulos y el work package
vigente. Conserva los 13 proveedores estáticos y añade Coverage y Cosmic Ray,
para un total de 15. No es
predeterminado y la CLI rechaza cualquier raíz que no sea la identidad física
exacta de `$HOME\Neocortex\Repository`. El perfil ejecuta código del
proyecto, pruebas y `conftest.py`; no procesa corpus ni modifica el estado
durable vivo:

```powershell
$Root = Join-Path $HOME 'Neocortex\Repository'
$State = Join-Path $HOME 'Neocortex\Laboratory\self-analysis\trusted-deep'

# Suite declarada completa, dentro de los límites predeterminados.
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State

# Alternativa focal: el selector puede repetirse y acepta node ids de Pytest.
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State `
  --deep-test-selector tests/test_bounded_subprocess.py `
  --deep-time-budget-seconds 600 --deep-max-tests 3000 --deep-shard-size 20

# Objetivo focal publicado por el work package H6.
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State `
  --deep-test-selector tests/test_external_deep_coverage.py `
  --deep-mutation-target _04_Nucleo_Operativo/external_deep_coverage.py `
  --deep-mutation-symbol external_deep_coverage._normalize `
  --deep-mutation-max-mutants 20 --deep-mutation-timeout-seconds 30 `
  --deep-mutation-time-budget-seconds 600
```

Los límites admitidos son 30–900 segundos nominales, 1–5000 tests y shards de
1–50; 600/3000/20 son los valores predeterminados. Después de comprobar un
shard terminado o reutilizado, Coverage puede consumir una única extensión
acotada a 2x. Publica progreso al recolectar y al iniciar, reutilizar o terminar
cada shard. Sin selector, la publicación
declara `suite_selection=full`; con uno o más, `selected`. Si `max_tests` trunca
lo recolectado, `measurement_complete=false` y los gates que necesitan cobertura
completa se abstienen.

La mutación requiere target y al menos un selector explícito; symbol es
opcional. Admite 1–100 mutantes (20 por defecto), 1–120 segundos por mutante
(30) y 10–900 segundos totales (600). Cosmic Ray sólo muta la copia staged y
nunca el repositorio, pero ejecuta las pruebas elegidas y éstas pueden usar red.
Toda la salida es advisory y conserva `mutation_authority=false`.

Coverage usa branch coverage y contextos dinámicos por test/fase de Pytest, pero
mide sólo el proceso principal. Un subprocess creado por las pruebas puede
ejecutar código sin quedar atribuido y la publicación lo declara mediante
`coverage_main_process_only` y `subprocess_coverage_not_collected`. Los shards se
firman con inputs, suite, configuración y versiones. Sólo un shard con todas sus
pruebas aprobadas produce checkpoint reanudable; uno fallido, incompleto o
incompatible se vuelve a ejecutar. En la validación canónica esos eventos y la
salida del hijo se retransmiten inmediatamente por `stderr`; un heartbeat cada
30 segundos permite distinguir actividad prolongada de una pérdida de señal.

Después de cerrar writers, consulte el mismo `$State` con `--code-status
--code-json` y `--code-review --code-json`. `test_coverage` debe explicar
selección, completitud, resultados, líneas/ramas y limitaciones. Los contextos
dinámicos prueban qué tests ejecutaron una línea o símbolo; no prueban por sí
solos que exista una aserción o un invariante protector. Coverage v2 lo expresa
como `executing_tests` y `work_package_target_executed_by_passing_suite`. Un
publication diff sólo puede aprobar los deltas
de líneas y ramas cuando suite, alcance, configuración y herramientas coinciden.

La corrida canónica H6 Run 9 terminó en 343.168 s: 585 candidatos, 2 procesados,
583 por caché, 15 proveedores y 0 errores. Sobre
`_04_Nucleo_Operativo.external_deep_coverage` /
`external_deep_coverage._normalize`, Cosmic Ray seleccionó y completó 20/20 de
524 mutantes generados: 5 killed, 5 survived, 10 incompetent, 0 timeout y score
0.50. Run 10 tardó 23.996 s con 585/585 candidatos por caché, cero
bytes/analyze/persist/graph y 14 replays; `installed-package-inventory` se
recalculó. Las consultas read-only status, review y diff tardaron 38.982,
47.675 y 57.856 s. Esos artefactos históricos usaron architecture v2,
engineering v1, review v10 y publication diff v8. El contrato vigente de review
es `neocortex.code-review/v22`, no declara schemas compatibles, publica
observaciones estructurales con inferencia abstained y evidencia enlazada a IDs
Code después de resolución read-only; incluye clases seleccionadas por superficie
AST directa, con umbrales provisionales explícitos, y no genera recomendaciones
ni paquetes de cambio. Sólo puede publicar paquetes
`unused_characterization`, advisory y sin autoridad de mutación.

En el estado canónico, v22 consulta además Text/Semantic mediante conexiones
`immutable=1` y fences de main/WAL/SHM; nunca checkpointa ni elimina sidecars.
Un WAL no vacío, layout no demostrado o cambio de fence produce abstención de
esa dimensión. `aligned` significa exclusivamente igualdad de sets en el head
publicado y contratos owner/materialization correctos; no demuestra crash
recovery ni atomicidad cross-owner.

v22 emite también topología Text, interacciones SQL/transaccionales,
cambio/schema evolution, assurance, invariantes, seguridad/dependencias,
reachability de `text.extract`, las nueve rutas built-in, superficies de
módulo/configuración/CLI, calibración y autoeficacia. Ninguna dimensión ejecuta
código del repositorio durante la consulta; sólo el comando separado y explícito
`--code-experiment-run` ejecuta los escenarios allow-listed. La autoeficacia usa `git ls-files` sin locks y
digests de contenido; distingue `content_stale` de `scope_incomplete` y de su
combinación. Los proveedores faltantes permanecen explícitos y los indicadores de
precision/recall/decision rate no existen sin outcomes independientes.

La proyección Retention v1 usa el mismo planner productivo exclusivamente en
dry-run. Lee Semantic, Catalog, Inventory y Framework con un lote de 100,
conserva holds y cursores exactos, y repite la observación antes de cerrar el
review. Un owner bloqueado, schema incompatible o hold declarado ausente queda
como gap. El escenario allow-listed asociado sólo verifica fixtures y controles
negativos; no habilita `DELETE`, `VACUUM` ni una futura ruta apply.

El registro general también proyecta el grafo Ruff/Grimp y los contratos de
imports. `path_namespace_id` no es ownership: el contrato v1 módulo→logical-owner
usa selectores explícitos, cubre sólo seis owners y conserva unmapped/overlap.
Nunca deduce owner por nombre, asigna un default ni confunde logical owner con
state owner.

El manifest guarda `Neocortex` como primer elemento de su argv canónico. Antes
de promover el launcher estable, use la ruta exacta del runtime versionado para
validar `--version`, `--help` y este preset.

La corrida escribe bases en `$MiniState`, pero omite acciones, candidatos MIME,
catálogo y organización. El status es read-only: exige ausencia de sidecars o
la disposición inactiva exacta de WAL vacío más SHM de 32 KiB. Un rollback
journal, WAL no vacío, SHM inválido o una cerca inestable junto a
`code.sqlite3`, `framework.sqlite3` o `dedup.sqlite3` causa abstención total con
código `2` sin tocar el estado. Consulte
[SELF_ANALYSIS.md](SELF_ANALYSIS.md) para preflight, policy/firma, puerta
incremental, manifest y conteos cero.

La ruta Code hace checkpoint al publicar e intenta retirar sidecars vacíos de
forma segura. Si permanece exactamente un WAL vacío con SHM de 32 KiB, el
status puede demostrar que está inactivo; cualquier otro layout abstiene hasta
que los writers cierren y una corrida posterior pueda limpiar los auxiliares.
Una búsqueda o listado sobre una base quiescente usa `immutable=1` con cercas y
no crea sidecars; si ya hay un writer activo usa read-only convencional y nunca
borra ni hace checkpoint de auxiliares ajenos.

No eleve el proceso sólo para habilitar USN en este preset. Si el journal no es
accesible, el autoanálisis recorre la raíz completa, no publica checkpoint y
declara `journal_status=unavailable`; el replay todavía debe demostrar hits de
caché y cero bytes de código releídos. La operación normal también puede
recorrer y publicar un snapshot portable; el checkpoint conserva sus tres
campos USN en `NULL` y las rutas reutilizan caches por identidad/metadata.

Sólo un cambio al autoanálisis o un cierre de release requiere un smoke de la
raíz canónica. En ese caso analiza `%USERPROFILE%\Neocortex\Repository` con un
estado externo nuevo bajo `$HOME\Neocortex\Laboratory\self-analysis`.
Un cambio cotidiano no debe convertirse por rutina en un análisis completo del
repositorio.

## Reanudación

`--status` muestra runs, rutas y fases con un límite predeterminado de cinco. Se
puede ampliar hasta 1000:

```powershell
Neocortex --status --status-limit 20
Neocortex --status --status-run 40 --status-json
```

Para continuar fases incompletas de un run cuyo snapshot siga retenido:

```powershell
Neocortex --resume-run 40
```

La reanudación implica `--route-only`. No ejecuta el inventario común ni
acciones de archivos. Si el snapshot falta, es incompatible o quedó obsoleto,
la operación debe abstenerse; no reconstruya filas SQLite manualmente.

Code puede reutilizar directamente un inventario durable aunque el snapshot
conserve cero candidatos MIME:

```powershell
$State = 'C:\Estado\Neocortex'
Neocortex --root $Root --state-directory $State --route code --route-only
Neocortex --root $Root --state-directory $State --route code --route-only --candidate-run 40
Neocortex --root $Root --state-directory $State --resume-run 40
```

La selección pública predeterminada es `--code-scope projects`: el inventario
sigue cubriendo el corpus compartido, pero Code sólo consume árboles con un
manifiesto de proyecto y omite dependencias, caches y outputs. Use
`--code-scope broad` únicamente cuando se quiera analizar deliberadamente
código suelto fuera de proyectos.

Sin `--candidate-run`, se examina el owner durable más reciente de la raíz
exacta y se exige modo `normal`; una discrepancia falla sin retroceder a un run
histórico por tener candidatos. Cero candidatos sólo se
admite cuando **todas** las rutas seleccionadas declaran
`input_source=inventory_snapshot`; una ruta MIME o selección mixta falla antes
de crear o ejecutar el nuevo run. `--self-analysis` continúa rechazando
route-only/resume por diseño.

Un run actual se vuelve reanudable sólo después de que terminó de generar todos
los candidatos y publicó atómicamente su `scan_id`, conteos y evento de
enrutamiento. Al abrirlo de nuevo se validan raíz normalizada, identidad física
de la raíz, scan completo sin errores y conteo de archivos. Para un run legacy
interrumpido sin vínculo se exige además evidencia de inventario única y al
menos un `route_run` durable; si una comprobación falla, ejecute una corrida
nueva en vez de forzar la reanudación.

## Watcher incremental en primer plano

El watcher vive exclusivamente en el proceso y terminal actuales. No instala
servicios, tareas programadas ni procesos desprendidos.

Actívelo sólo después de aprobar, para una ruta, el piloto y su segunda corrida
incremental. El watcher actual dispara corridas de contenido y catálogo; no
ejecuta `--semantic-index` ni `--semantic-classify` y todavía no demuestra el
daemon multimodal completo.

```powershell
Neocortex --root $Root --watch --route pdf
```

Opciones y valores predeterminados:

| Opción | Predeterminado | Contrato |
|---|---:|---|
| `--watch-bootstrap` | `if-needed` | Bootstrap siempre, cuando sea necesario o nunca. |
| `--watch-poll-timeout-seconds` | `1` | De 1 a 300 segundos. |
| `--watch-debounce-seconds` | `2` | Puede ser cero. |
| `--watch-max-debounce-seconds` | `30` | Positivo y no menor que debounce. |
| `--watch-error-backoff-initial-seconds` | `1` | Puede ser cero. |
| `--watch-error-backoff-max-seconds` | `60` | No menor que el inicial. |
| `--watch-error-backoff-multiplier` | `2` | Mínimo 1. |
| `--watch-portable-interval-seconds` | `300` | De 1 a 86 400; sólo gobierna el recorrido normal cuando no hay USN. |

Ejemplo con política explícita para una ruta ya aprobada:

```powershell
Neocortex --root $Root --watch --route pdf `
  --watch-bootstrap if-needed `
  --watch-poll-timeout-seconds 2 `
  --watch-debounce-seconds 1 `
  --watch-max-debounce-seconds 15
```

El watcher rechaza `--apply`, `--route-only`, `--resume-run` y
`--candidate-run`. Los cambios USN actúan como señales para nuevas corridas;
no convierten el journal en un backup ni prueban por sí solos que una
exploración parcial sea completa. Sin cursor USN compatible, espera el intervalo
portable y ejecuta la corrida integrada normal: el inventario vuelve a recorrer
la raíz, mientras las rutas reutilizan sus caches por identidad y versión. No se
crea un cursor sintético, un datastore ni un indexador paralelo.
La recarga del owner durable entre ciclos usa una instantánea immutable cercada:
no crea `framework.sqlite3-wal/-shm` sobre una publicación quiescente y se
abstiene con el backoff normal si detecta un writer o sidecars activos.

Durante toda su vida adquiere un lease del sistema operativo por la combinación
canónica de raíz y directorio de estado. El archivo
`watcher-life-xxh3-128-<digest>.lock` conserva metadatos acotados de PID, tiempo
de creación, host, versión, argv, raíz/estado e inicio. Un segundo watcher con
la misma identidad se abstiene y devuelve `2`; otra raíz puede operar sin
colisión. El byte lock, no el JSON, determina ownership y se libera al cerrar o
caer el proceso. No borre el archivo: un owner stale se reemplaza sólo después
de que el nuevo proceso adquiere el lock. Esta exclusión no mata procesos ni
reemplaza `framework.lock`, que sigue protegiendo cada corrida integrada.
En el fixture sintético comparable, adquirir y persistir el lease costó
aproximadamente 11.86 ms una sola vez al iniciar el watcher; no es una medición
del corpus vivo.

### Cancelación del watcher

- El primer `Ctrl+C` solicita cancelación cooperativa y despierta las esperas de
  recursos.
- Un segundo `Ctrl+C` vuelve a interrumpir el hilo principal si el cierre no
  concluye.
- La cancelación interactiva termina con código `130`.
- Errores de fuente o corridas fallidas retenidas producen código `2`.

No cierre procesos por coincidencia amplia de nombre. Si fuera indispensable
intervenir, confirme PID, proceso padre y línea de comandos y actúe sólo sobre
el proceso propio.

## Límites y recursos

Los valores siguientes son contratos predeterminados del parser/configuración,
no promesas de RSS real. Los presupuestos son admisión estimada; bibliotecas
nativas y procesos hijos también consumen memoria.

| Ruta | Límites predeterminados relevantes |
|---|---|
| PDF | 4 workers y 2 permisos OCR; render máximo 40 000 000 píxeles por página; texto máximo 5 000 000 caracteres por página; timeout base 600 s en modo adaptativo, máximo 1200 s; reserva mínima 512 MiB por worker; máximo 2 documentos sobre 128 MiB. No hay límite predeterminado de cantidad, tamaño ni páginas. |
| DOCX | Texto máximo 20 000 000 caracteres; presupuesto 512 MiB; margen físico y de commit de 1024 MiB; espera 60 s. Sin límite predeterminado de tamaño o cantidad. |
| Office | Texto máximo 20 000 000 caracteres; presupuesto 512 MiB; margen físico y de commit de 1024 MiB; espera 60 s. Sin límite predeterminado de tamaño o cantidad. |
| ZIP | Profundidad 5; 20 000 miembros visibles; directorio central 32 MiB; 64 MiB por miembro; 512 MiB expandidos y 20 000 000 caracteres por contenedor; ratio 200; PDF interno de hasta 500 páginas con worker de 768 MiB/60 s; OCR hasta 50 páginas, 200 dpi, 40 000 000 píxeles y 30 s por llamada. Sin límite predeterminado de ZIP físicos. |
| Texto | 64 MB decimales por archivo; 4 000 000 caracteres; conversor Office heredado aislado con 1024 MiB y 60 s. Sin límite predeterminado de cantidad. |
| Imagen | 4 workers; presupuesto 512 MiB; margen físico y de commit de 1024 MiB; espera 60 s; timeout de worker 120 s y OCR documental 12 s; cada imagen seleccionada obtiene/reutiliza huella completa XXH3-128 en Dedup. `--image-max-count` limita candidatos completos, incluidos cache hits. Sin límite predeterminado de tamaño o cantidad. |
| Audio | Duración máxima 6 h; transcripción máxima 5 000 000 caracteres y 100 000 segmentos; timeout por archivo 3600 s; arranque de worker 1800 s; reserva declarada de worker 4096 MiB, presupuesto de ruta 2048 MiB, márgenes físico/commit de 2048 MiB y espera 300 s. Sin límite predeterminado de tamaño o cantidad. |
| Video | Duración máxima 6 h; 48 frames; intervalos de 30 s más escenas/keyframes; 2 073 600 píxeles por frame y lado 1920; 40 MP OCR totales, 16 KiB OCR por frame, scratch máximo 512 MiB; probe 30 s, discovery 60 s, frame 20 s, archivo 300 s y worker 2 GiB. |
| Código | Archivo máximo 8 MiB; texto máximo 4 000 000 caracteres; chunks de 12 000 caracteres; sin límite predeterminado de cantidad; scope `projects` excluye dependencias, generado y vendorizado salvo inclusión explícita. |

Un piloto Video debe incluir al menos un clip audio+visual y uno visual-only,
comprobar timestamps contra FFprobe y repetir la corrida. En visual-only, Audio
debe publicar `no_audio` sin cargar Whisper y Video debe terminar completo; un
archivo cuyo MIME sea realmente audio conserva el error si carece de stream.
`--video-doctor` verifica FFmpeg/FFprobe y los idiomas OCR sin crear estado.

El coordinador global usa por defecto un máximo de carga CPU del 90 % y una
espera de recursos de 300 s; los presupuestos globales de memoria, commit y
slots CPU se calculan cuando no se fijan explícitamente.

Para una primera ejecución use límites de tamaño/cantidad compatibles con la
ruta. Los valores `--*-max-mb` usan megabytes decimales; en PDF `1000` equivale
a 1 GB:

```powershell
Neocortex --root $Root --route pdf --MaxMB 1000 --MaxCount 25
Neocortex --root $Root --route archive --archive-max-mb 1000 --archive-max-count 25
Neocortex --root $Root --route text --text-max-mb 64 --text-max-count 25
Neocortex --root $Root --route image --image-max-mb 100 --image-max-count 100
Neocortex --root $Root --route video --video-max-count 25
Neocortex --root $Root --route code --code-max-count 500
```

No reduzca OCR, límites de texto o validación de caché para declarar éxito sin
registrar que cambió la carga y el contrato de resultados.

En PDF e imagen, el productor que abre el stream de candidatos debe consumirlo
y cerrarlo en su propio thread. Un fallo de admisión, una excepción o una
cancelación se desenrollan mediante el `finally` de ese productor; el
coordinador no debe cerrar el generator desde otro thread.

### Ruta code: cache y grafo estable

Un hit con la misma ruta actualiza presencia y observación, pero ejecuta cero
DML sobre `code_fts`. Si cambia la ruta, no es un hit: se procesa una versión
sucesora y la anterior queda como historia. Los hits de resultados `partial` o
`error` conservan esos contadores; `--retry-code-errors` solicita reprocesarlos.

El fastpath del grafo sólo aplica a una corrida completa de `code`, sin
`--code-max-count` ni filtros de selección. Primero se ejecuta `mark_missing`;
si no hubo invalidaciones ni trabajo nuevo, todos los candidatos fueron hits
compatibles con el runtime y el run completo inmediatamente anterior publicó el
fence tipado exacto con `resolver_signature=code-graph-resolver-v4`, se reutiliza
el conteo de proyectos. Esa versión resuelve símbolos y dependencias mediante
conjuntos temporales indexados, prioriza ámbito local y rutas relativas exactas,
y sincroniza los labels FTS distintos en una pasada, no con una consulta o
actualización por relación o versión. Una
base existente sin fence, un run intermedio, un manifest/move o cualquier
evidencia malformada fuerzan `finalize_graph` y reconstruyen membresías y FTS.

La primera corrida completa posterior a esta actualización puede por ello
realizar una finalización larga; las siguientes sólo prueban estado estable si
usan el mismo corpus, configuración y firma. El esquema vigente es 4. Durante
`finalize_graph`, un progress handler SQLite acotado consulta cancelación dentro
de la transacción, revierte antes de propagar la excepción original y se retira
al salir; esto no convierte el grafo en una publicación generacional.

## Modelos y herramientas externas

```powershell
Neocortex --pdf-doctor
Neocortex --audio-doctor
Neocortex --video-doctor --video-ocr-profile auto-multilingual
Neocortex --code-doctor
Neocortex models status --json
```

- Tesseract y los idiomas `spa`, `eng`, `deu`, `chi_sim`, `chi_tra` y `osd`
  son externos a Python. Los perfiles multilingües seleccionan un conjunto
  acotado y persisten idiomas efectivos, OSD, confianza y fallback.
- FFprobe se requiere para el sondeo de audio y video; Video necesita FFmpeg
  para escenas, keyframes y frames. Ambos se informan en sus doctors.
- qpdf es opcional y sólo participa en recuperación estructural PDF.
- DOC heredado prioriza LibreOffice y usa `catdoc` como fallback. XLS y PPT
  priorizan `xls2csv` y `catppt`, respectivamente, y usan LibreOffice si falta
  el extractor específico. Todos se ejecutan con entrada, salida, memoria y
  tiempo acotados.
- El cierre `full` requiere el Microsoft Visual C++ v14 Redistributable x64
  vigente para sus wheels nativos. Antes de promover, importa PyMuPDF, ONNX
  Runtime, PySide6, PyAV, CTranslate2 y OpenCV desde el runtime candidato;
  `pip check` no detecta una DLL del sistema ausente.
- Ruff pertenece a la superficie de distribución `analysis`, instalada por la
  release personal mediante `full`. `--code-doctor --code-json` debe mostrar su
  distribución y versión desde el mismo intérprete de Neocortex; una copia
  global encontrada en `PATH` no satisface esta capacidad.
- Mypy sigue la misma superficie `analysis`/`full` y se ejecuta como módulo del mismo
  intérprete, con caché efímera propiedad de la corrida.
- Pyright `1.1.411` se instala con `python -I tools/pyright_runtime.py install`
  desde el manifest/lock versionados: valida hashes e integridad npm, usa
  `npm ci` sin scripts ni optional packages y verifica Node `24.18.1`, el grafo
  instalado y la versión viva. Después se invoca mediante Node.
  `--code-doctor --code-json` informa por separado los
  13 proveedores estáticos y los dos proveedores profundos; la corrida incorpora la
  resolución exacta a su firma de entorno y comparabilidad.
- Vulture `2.16` pertenece a `analysis`/`full` y se invoca mediante su API
  programática aislada. Su finding es advisory y sólo el consumidor de consenso
  puede explicarlo o abstenerse; nunca autoriza borrar.
- Grimp `3.15` y Complexipy `6.2.0` pertenecen a `analysis`/`full`. Grimp se
  consume directamente como grafo legible por máquina; Import Linter `2.13` se
  midió viable pero no se integra porque duplicaría esa dimensión sin salida de
  contratos JSON. Complexipy se invoca por API para separar findings reales de
  la semántica de umbral de su CLI.
- Deptry `0.25.1` y pip-audit `2.10.1` pertenecen a `analysis`/`full`;
  Packaging `26.2` sí forma parte de la base mínima. Semgrep `1.172.0` no se
  instala en base, `analysis` ni `full`: su wrapper fijo de scan, constraints y
  excepciones MCP viven en un tool-runtime administrado y separado. Deptry no
  instala ni retira dependencias; pip-audit nunca ejecuta `--fix`; el inventario
  instalado es local y no emite conclusiones jurídicas.
- Git alimenta únicamente la historia local; Cosmic Ray `8.4.6` pertenece a
  `analysis`/`full` y sólo se activa con target y tests focales en `trusted-deep`.
- En Linux operativo, audio usa Whisper `small` CPU/int8 y sólo modelos
  locales. `Neocortex models prepare --json` adquiere explícita y
  secuencialmente Whisper, Jina, MiniLM compacto y CLIP texto/visión, y valida
  el modelo NudeNet incluido. `models status` nunca crea rutas ni descarga.
- El bakeoff offline ES/EN/DE/ZH dejó MiniLM como candidato shadow: mejoró
  top-1/MRR/recall y latencia caliente agregados, pero retrocedió en inglés.
  Jina sigue publicado hasta un A/B real etiquetado y calibración propia; no se
  mezclan dimensiones ni scores entre modelos.
- La fachada histórica `--semantic-prepare-models` se conserva para el dominio
  Semantic; indexar y clasificar siguen siendo pasos separados.

`--semantic-index` aplica por defecto 50 items nuevos o cambiados, 1 500 jobs
durables nuevos o reactivados y 900 segundos. El presupuesto es compartido por
texto, imagen y OCR; `all` no reinicia el reloj entre modalidades. Los replays
exactos no consumen los límites de items o jobs, pero todavía enumeran la fuente
en O(n); cuando no hay cambios reutilizan el head publicado sin clonarlo. Una
generación con altas, bajas o cambios todavía materializa su base en O(n), pero
el clon avanza por páginas con cursor durable, high-watermark fijado y deadline
compartido; al reanudar no repite el prefijo ya confirmado.

El texto se ajusta con el tokenizador real antes de persistir cada job y el
backend rechaza truncamiento. Cada fuente confirma staging por lotes; fallo,
cancelación o deadline conservan el prefijo reanudable sin mover el head. Sólo
una enumeración `bounded-v1` completa puede publicar. Si se agota un límite, la
CLI informa `truncated=1`, devuelve `2` y conserva el head anterior.

Antes de crear jobs, `semantic-text-quality-v1` rechaza únicamente ruido de
alta confianza: Base64/binario codificado, dumps densos de fórmulas, mojibake,
tokens desmedidos y repetición mecánica. También colapsa chunks idénticos del
mismo item. La caché fuente permanece completa y reconstruible. La recuperación
Jina mixta aplica piso `0.42` a cuerpo y título de todos los owners textuales
soportados; es abstención de retrieval, no probabilidad de relevancia.

CLIP no hereda ese piso. La calibración humana positiva/negativa actual mostró
solapamiento y el corte seguro sobre el estado vivo conservaría sólo 32% de los
positivos; por ello la búsqueda visual se abstiene sin cargar el backend hasta
recibir un contrato de calibración compatible y medido.

Cada item textual incorpora al final una sección de título
`semantic_metadata_title`, firmada por `semantic-content-aware-title-v3`.
Prefiere un título propio de la fuente, usa un encabezado inicial humano cuando
el basename es genérico y conserva el basename como fallback. El orden
cuerpo→título preserva IDs y ordinales corporales; el cache puede reutilizar
cuerpos sin inferencia. La búsqueda pondera título `0.5` frente a cuerpo `1.0`,
aplica la misma abstención y limita la repetición por documento; clasificación,
evidencia y Knowledge `evidence` usan sólo el cuerpo. Knowledge `discovery`
admite el título únicamente como prior de un recurso ya sustentado por cuerpo.

No ejecute una cola operativa grande antes de demostrar en un estado aislado
20–50 elementos, publicación, búsquedas representativas y segunda corrida
incremental. Tests de staging no sustituyen esa prueba end-to-end.

No ejecute manualmente herramientas externas ni descargue modelos para validar
una instalación básica. `--self-analysis` supervisa por sí mismo la suite; la
validación del wheel `full` debe confirmar Ruff, Mypy, Grimp, Complexipy,
Vulture, Pytest, Coverage, Cosmic Ray, Deptry, pip-audit y Packaging. Semgrep se
confirma exclusivamente mediante el recibo y la verificación de su tool-runtime
separado; los perfiles trusted confirman además Git, Node y Pyright aislado.

Para probar incrementalidad, ejecute una sola segunda corrida sobre el mismo
estado y los mismos bytes. En `--code-status --code-json`, los proveedores
reutilizables deben declarar `execution=cache_replay`, `cache_hits=1` y
contadores de verificación coherentes. El replay no reejecuta el workload del
analizador, tests o mutantes; probes de validación específicos pueden ser
distintos de cero y deben quedar explicados y costeados. En Run 10, Git conservó
dos probes, Coverage uno y Cosmic Ray cero; el inventario del entorno instalado
se recalcula deliberadamente. Findings, métricas y relaciones se
referencian desde la publicación original, sin duplicarse. El tiempo y bytes de
la verificación siguen siendo costos reales del replay; cero procesos no
significa costo cero. El replay verifica inputs; no significa
que se haya omitido la comprobación de frescura.

## Validación local Linux

GitHub Actions está deshabilitado y el repositorio no conserva workflows. La
barrera canónica para todo cambio nuevo se ejecuta localmente con:

```bash
Neocortex code validate
```

Es un orquestador del autoanalizador, no otro linter: captura el diff; selecciona
pruebas afectadas, completa huecos con fronteras públicas/escenarios registrados
y escala a la suite Linux sólo ante cambios de packaging, schema o gates; ejecuta las barreras
estática y arquitectónica existentes; publica `trusted-deep`; consume el review
v22; ejecuta experimentos registrados; instala y prueba el wheel candidato fuera
del checkout; y repite la misma publicación para demostrar replay. Un gate
fallido produce `failed`, evidencia insuficiente produce `abstained`, y ambos
devuelven código 2. La salida JSON canónica se obtiene con `--json`.
La selección Linux excluye sólo los módulos que prueban el runtime Windows/NTFS
retirado; no ejecuta ese legado como barrera y no confunde fixtures portables
con soporte activo de Windows.

La entrada hace preflight read-only de `MemAvailable`, swap y PSI y mantiene un
lock exclusivo. El árbol completo se ejecuta en un servicio de usuario
systemd/cgroup v2 con reserva adaptativa para KDE/Chrome, `MemoryHigh=75%` del
presupuesto, `MemoryMax` adaptado (máximo 4 GiB), `MemorySwapMax` (máximo
512 MiB), hasta cuatro CPUs, 512 tareas y una cota de seguridad de 75 minutos.
El watchdog observa el
host cada 500 ms y detiene cooperativamente el grupo con SIGINT si desaparece
la reserva del escritorio o si la presión cruza el umbral mientras también
falta el headroom físico reservado. El reclaim aislado por `MemoryHigh` con
memoria abundante no se interpreta como riesgo global.
Cada comando acotado usa además su propio grupo de proceso. Trusted-deep parte
del presupuesto nominal solicitado y sólo habilita su cota 2x después de avance
validado; el proceso padre expone salida, eventos y heartbeats en tiempo real.
Un subreaper Linux adopta y termina mediante `pidfd` los descendientes que
creen otra sesión con `setsid()`. Al vencer la cota correspondiente, el árbol recibe
SIGINT y dispone de una gracia para publicar su terminalización durable
antes de que el runner escale a SIGKILL. Un fallo de D-Bus, cgroup, preflight o
watchdog es abstención operativa; nunca habilita un fallback sin contención.
El servicio declara además `PrivateNetwork=yes`: el árbol no tiene ruta al host
ni a Internet durante la validación. La admisión
`neocortex.code-validation-resources/v3` no confía sólo en el entorno: dentro
del worker, NeoCortex compara el unit declarado con `/proc/self/cgroup` y
consulta en systemd su `PrivateNetwork=yes`. Como esa propiedad puede
conservarse aunque el namespace no llegue a crearse, el unit restringe además
las familias a `AF_UNIX` y el worker demuestra que el kernel rechaza AF_INET y
AF_INET6.

La selección experimental también está ligada al diff mediante un registro
versionado de rutas/tests→preguntas/sujetos. Un registry gap relevante o una
pregunta sin disposición técnica exacta después del replay produce
`abstained`; `not_required` sólo aparece cuando ese binding demuestra que la
pregunta es disjunta al cambio. La política v6 incorpora bindings exactos para
la CLI pública y para Knowledge Asset Health Text/PDF; cambiar sus contratos o
tests de control exige los templates de veintiún/cinco, doce/cuatro y
doce/cuatro respectivamente.

El delta `added/resolved` publicado por cada provider sigue visible para
investigación histórica, pero no sustituye al baseline Git: puede comparar con
una corrida compatible mucho más antigua y cambiar por movimiento de líneas.
La aceptación estática se decide antes mediante el baseline versionado de
Ruff/Mypy/Pyright por path, regla y conteo; un provider `ready` con delta global
no se trata como fallo de ejecución.

Para el árbol sucio normal, el baseline predeterminado es `HEAD`. Después de
crear el commit local y antes del push se usa el padre explícito:

```bash
Neocortex code validate --baseline HEAD^
```

`tools/quality_gate.py` permanece como infraestructura interna y diagnóstico
especializado. Las sesiones no deben ejecutar Ruff, Mypy, Pyright, pytest u
otros componentes como rutas de aceptación independientes cuando `code
validate` puede orquestarlos y emitir el recibo único. Windows es legado fuera
del alcance vigente.

El productor general `pip-audit` puede observar la red en una ejecución
explícita distinta, pero `code validate` fuerza política offline. Su verdict sólo
puede resolver el snapshot publicado previo mientras siga vigente, tenga cero
vulnerabilidades y su inventario exacto de distribuciones/versiones coincida con
el actual; cambios en packaging o política supply invalidan esa resolución. No
se usa un resultado stale ni se interpreta la falta de red como ausencia de
vulnerabilidades.
El inventario local es `environment_bound` y se reobserva en el replay. Sus dos
proyecciones deben ser idénticas en métricas, relaciones, findings y versiones;
sólo el timestamp/ID del snapshot se excluye del digest comparado.

Las validaciones deben terminar antes del push y quedar ligadas al SHA local
comprometido. El push sólo transporta ese SHA a `origin/main`; no sustituye los
gates locales ni dispara una segunda validación remota.

El baseline de cobertura sólo cambia mediante una medición completa, verde y
aprobada. La actualización es explícita y su diff debe revisarse; una corrida
ordinaria nunca escribe el baseline:

```bash
python tools/quality_gate.py coverage \
  --data-file "${TMPDIR:-/tmp}/neocortex-coverage-data" \
  --report "${TMPDIR:-/tmp}/neocortex-coverage.json" \
  --write-baseline
```

El gate histórico `pre-push` continúa disponible internamente para una campaña
integral explícita, pero ya no es la interfaz ordinaria ni reemplaza el recibo
diff-aware del autoanalizador.

Los gates no descargan pesos de modelos reales; los contratos usan dobles y la
conformidad de los pesos se comprueba durante la instalación local. Ninguna
prueba aislada sustituye la identidad física ni el estado vivo exigidos por una
corrida local real.

## Cancelación de una corrida normal

El primer `Ctrl+C` solicita cierre cooperativo. La corrida se registra como
`cancelled`, distinta de `failed`, y el launcher devuelve `130`. Espere la
liberación de workers y procesos hijos antes de iniciar otra corrida con el
mismo estado.

La implementación histórica Windows usa Job Objects, pero permanece fuera del
alcance activo y no constituye una barrera vigente.

En Linux, cada subproceso usa una sesión/grupo propio; timeout y cancelación
terminan hijos y nietos con `SIGTERM` y después `SIGKILL`. Los límites de memoria
se imponen con `RLIMIT_AS` o `/usr/bin/prlimit`; si se solicita uno y no puede
imponerse, la operación se abstiene en vez de ejecutar sin contención.

Si se interrumpió una operación autorizada sobre archivos, **no la repita
automáticamente**. Siga la sección de acciones inciertas de
[RECOVERY.md](RECOVERY.md).

En `0.9.0`, los rename y movimientos admitidos son únicamente de archivos
regulares con un hard link en NTFS local y mismo volumen, mediante handles
retenidos y sin reemplazo. Los demás casos se abstienen. La aplicación de
candidatos de Papelera está deshabilitada; el dry-run continúa registrando el
plan y un `--apply` los marca `skipped` sin llamar a `Send2Trash`.

Linux no expone aún ese backend de mutación. `--apply` y
`--organization-apply` se rechazan antes de crear estado con salida `2` y razón
`linux_mutation_backend_unavailable`.

## Diagnóstico operativo

Diagnóstico cotidiano mínimo, sin modificar el corpus:

```powershell
Neocortex --version
Neocortex doctor capabilities
Neocortex doctor platform --json
Neocortex models status --json
Neocortex --status --status-limit 20
```

Añada únicamente el status o doctor de la capacidad que está usando, por
ejemplo `--knowledge-status` o `--semantic-status`. `pip check`, recovery,
retención y todos los doctors se reservan para fallos de dependencias,
operaciones inciertas o validación de una instalación.

Preserve la salida exacta, código de salida, hora, versión y `run_id`. No adjunte
contenido confidencial del corpus a diagnósticos sin autorización.

`--action-recovery-status` abre sólo la base existente y clasifica
`applying`/`recovery_required` sin escribir ni repetir operaciones. Use
`--action-recovery-after` para paginar, `--action-recovery-run` para acotar y
`--action-recovery-json` para JSON Lines. Devuelve `2` si una fila es ambigua o
imposible de comprobar; `confirmed` y `not_performed` siguen requiriendo una
decisión humana antes de cualquier cambio persistente.

Para conservar la observación, no la mutación, use después un `record`
explícito con actor y confirmación:

```powershell
Neocortex --action-recovery-record 42 --action-recovery-actor "Victor" --confirm-reconciliation-record --action-recovery-json
```

El evento es append-only e idempotente; `--action-recovery-expected-event`
protege una observación posterior mediante CAS. Un código `2` puede acompañar
un registro correcto si la clasificación sigue ambigua o imposible. Verifique
la salida y el `event_id`. No existe todavía un comando de recuperación o
verificación y ningún evento autoriza por sí mismo una mutación.

Los planes documentales `recovery_required` tampoco se reintentan y conservan
reservado su destino:

```powershell
Neocortex --organization-preview 100 --organization-preview-status recovery_required
```

## Crecimiento y mantenimiento

Obtenga primero un plan de sólo lectura. La edad es deliberadamente explícita;
si se omite, no se declara elegibilidad por antigüedad:

```powershell
Neocortex --retention-status
Neocortex --retention-status --retention-store semantic --retention-store catalog --retention-min-age-days 30 --retention-batch-size 100
```

El resultado protege como mínimo las publicaciones vigente y anterior,
builders y leases vivos, bases de generaciones, checkpoints, el último run
`completed` de framework aunque haya runs fallidos o cancelados posteriores, y
evidencia humana o incierta. Una referencia desde `semantic_evidence` es un
hold y bloquea la elegibilidad de esa generación. Se pagina con cursores
`--retention-<store>-after`. Los bytes son una cota inferior del payload SQLite
y el snapshot no es atómico entre bases. Un store con deriva queda `blocked` y
el comando devuelve `2`.

`--retention-status` es exclusivamente read-only/dry-run. No existen comandos
productivos `--retention-prepare`, `--retention-apply` ni
`--retention-verify`; el plan tampoco autoriza `DELETE` manuales. La ejecución
genérica permanece bloqueada hasta que las referencias cross-DB tengan holds
write-ahead durables y cada propietario disponga de journal reanudable e
idempotente. SQLite no proporciona una transacción atómica entre esas bases.

- Las rutas podan determinadas cachés obsoletas sólo después de una corrida
  satisfactoria; no todas las tablas históricas tienen una política global de
  retención demostrada.
- La poda legacy del inventario es una operación específica del propietario,
  separada de `--retention-status`. El coordinador debe entregarle todos los
  holds cross-store explícitos; si no puede hacerlo, falla cerrado sin borrar.
  Conserva siempre la publicación actual y la anterior de cada raíz, además de
  builders, candidatos y scans referenciados.
- Catálogo v6 y semántica v6 preservan la generación publicada durante staging,
  fallo o cancelación. Existe un planificador dry-run, pero no una poda ni
  enforcement de cuotas para generaciones fallidas, canceladas, superseded,
  `ready_partial` o builds abandonados.
- Supervise tamaño de `.sqlite3`, `-wal`, cachés de modelos y espacio libre.
- No elimine generaciones, runs, modelos, WAL o SHM por antigüedad aparente.
- No ejecute `VACUUM`, checkpoints, cambios de `journal_mode` ni manipulación
  de `PRAGMA user_version` como mantenimiento rutinario.
- Antes de cualquier intervención, detenga writers y cree un backup mediante la
  API SQLite según [RECOVERY.md](RECOVERY.md).
- Un WAL que crece requiere identificar primero el writer/lector que impide el
  checkpoint; no se corrige borrando el archivo.

## Actualización y rollback (procedimiento condicional)

Esta sección se usa sólo al instalar, promover, migrar o restaurar una versión.
No forma parte del flujo cotidiano ni de una corrección focal.

En Kubuntu/Linux, la herramienta mantenida construye y verifica una release
inmutable, activa `current` bajo `flock`, conserva las anteriores y escribe un
recibo en el estado:

```bash
python3.14 tools/release_linux.py install \
  --corpus-root "$HOME/Documentos/NeoCortex/Corpus" \
  --prepare-models --desktop
python3.14 tools/release_linux.py verify
python3.14 tools/release_linux.py rollback
```

Una preparación incompleta de modelos no promueve el runtime ni publica KDE.
Rollback cambia sólo el enlace activo; no elimina releases ni sustituye la
recuperación de bases.

1. Termine sólo los procesos propios de NeoCortex y confirme que no quede un
   watcher activo.
2. Capture `Neocortex --version` y `Neocortex --status --status-json`.
3. Cree un backup consistente de todas las bases.
4. Instale el artefacto ya validado conforme al README de la entrega.
5. Compruebe versión, ayuda, dependencias y doctors antes de abrir estado real.
6. Permita migraciones únicamente con la versión compatible y conserve el
   backup previo.
7. Si la actualización falla, no reduzca números de esquema. Restaure el paquete
   compatible y las bases completas siguiendo [RECOVERY.md](RECOVERY.md).

Una instalación limpia en un entorno temporal valida el paquete, pero no
actualiza por sí sola el launcher operativo del sistema. Compruebe ambos de
forma independiente.

La actualización `0.5.0` eleva `framework.sqlite3` 17→18,
`semantic.sqlite3` 5→6 y `document_catalog.sqlite3` 5→6. Las migraciones
preservan datos y se abstienen ante contratos v5/v17 desconocidos, pero no
ofrecen downgrade. El rollback exige restaurar el conjunto respaldado y el
paquete compatible; nunca edite los números de esquema.

La actualización `0.6.0` eleva únicamente `framework.sqlite3` 18→19 y agrega
el log de conciliación vacío. La operación `--action-recovery-record` puede
aplicar esta migración aditiva a una base existente después de la confirmación;
`status` y retención nunca migran. El rollback sigue requiriendo restaurar la
copia consistente y el paquete 0.5.0, no editar `schema_version`.

La actualización `0.7.0` no eleva ningún esquema ni crea una base Knowledge.
Sus comandos `--knowledge-status`, `--knowledge-search` y
`--knowledge-context` abren únicamente los propietarios ya existentes en modo
de solo lectura. Por tanto, un rollback del paquete a `0.6.0` no requiere un
downgrade de base atribuible a Knowledge; cualquier otra migración o cambio de
estado realizado por comandos distintos conserva su propio contrato de
recuperación.

La fuente `0.9.0` declara framework v22, Dedup v10, PDF v13, DOCX v6, Office
v3, Audio v2, Video v2 y catálogo v7. Framework 19→20 preserva
filas legacy como `normal`; Dedup 7→8 agrega la firma cruda de inventario a los
scans, conserva scans/archivos/bytes e invalida checkpoints sin firma en vez de
inventar evidencia. Dedup 8→9 conserva esas publicaciones y permite que
`volume`, `journal_id` y `next_usn` sean todos `NULL` o todos presentes, para
separar publicación de aceleración USN. Dedup 9→10 añade únicamente los índices
para joins Knowledge ligados a identidad y preserva conteos y bytes de archivos
y miembros planeados. PDF 11→12 y Office 1→2 fueron migraciones aditivas; las
migraciones posteriores de paths reconstruyen cada owner con la collation de
la plataforma, sin fusionar filas Linux case-distinct. Video se crea sólo al
ejecutar su ruta. Ninguna migración ofrece downgrade. Abra bases vivas sólo
con el runtime versionado validado; el rollback exige paquete compatible y
backup completo, nunca editar `schema_version`.

## Auditorías técnicas

Cuando Victor solicite explícitamente una auditoría integral o un cierre de
release, debe conservar los informes anteriores y seguir
[AUDIT_REPORTING_STANDARD.md](AUDIT_REPORTING_STANDARD.md) para evidencia,
manifiesto, barrera y cierre visible. Ese estándar no aplica a documentación,
configuración, correcciones focales ni slices verticales ordinarios.
