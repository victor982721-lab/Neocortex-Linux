# Autoanálisis de código y evidencia externa

> **Estado del contrato.** Esta capacidad pertenece a la fuente `0.7.2` bajo
> `%USERPROFILE%\Neocortex\Repository`. Debe ejecutarse desde un runtime
> versionado bajo `%LOCALAPPDATA%\Programs\Neocortex\versions` y promoverse
> únicamente mediante el launcher estable
> `%LOCALAPPDATA%\Programs\Neocortex\bin\Neocortex.exe` después de validar el
> artefacto exacto.

## Finalidad y frontera

`--self-analysis` analiza una raíz de código explícita como evidencia no
confiable sin autorizar trabajo común sobre el corpus. El preset:

- fija `corpus_access_mode=analyze_only` y ejecuta exactamente la ruta `code`;
- alimenta `CodeRoute` desde el snapshot de inventario, no desde candidatos
  MIME;
- omite `DedupPlanner`, `FrameworkActions`, catálogo y organización;
- excluye código generado y vendorizado;
- escribe estado derivado en un directorio separado, pero no modifica los
  archivos de la raíz analizada;
- ejecuta una suite versionada de proveedores externos sobre cada Python vigente
  que Code publicó con fingerprint exacto —incluidos parseos internos
  parciales—, con ejecución aislada, acotada y sin autoridad de mutación.

No es una consulta read-only: crea o actualiza `framework.sqlite3`,
`dedup.sqlite3` y `code.sqlite3` en el estado indicado. En `protected` y
`trusted-static`, el código observado se trata sólo como datos y no se ejecuta.
La única excepción es el perfil explícito `trusted-deep`, cuya finalidad es
ejecutar la suite declarada de la raíz canónica bajo límites duros; ni el código
ni sus resultados adquieren autoridad para pedir herramientas, permisos, red o
mutaciones.

## Plataforma genérica de evidencia externa

Code entrega a cada proveedor una lista explícita de versiones Python vigentes,
con fingerprint exacto, no generadas y no vendorizadas; ningún proveedor
descubre la raíz por su cuenta. NeoCortex verifica cada versión por handle,
materializa una copia temporal con el mismo árbol relativo bajo el estado
disjunto y vuelve a verificar los originales después del proceso. Cualquier
ruta desconocida, cambio concurrente, salida inválida, timeout, overflow o
límite alcanzado falla o abstiene sólo al proveedor afectado sin ocultar el
estado AST ni los demás resultados válidos.

`--analysis-profile` admite tres perfiles públicos:

| Perfil | Proveedores | Configuración y confianza |
|---|---|---|
| `protected` (predeterminado) | `ruff-protected-basic` | Ruff `E4,E7,E9,F` con `--isolated`, sin configuración del proyecto. Es la frontera `untrusted-safe`. |
| `trusted-static` | `ruff-protected-basic`, `ruff-trusted-project`, `mypy-trusted-project`, `pyright-trusted-project`, `vulture-unused-static`, `ruff-analyze-imports`, `grimp-architecture`, `complexipy-cognitive` | Añade política estática, tipos, candidatos de no uso, grafo de imports, contratos arquitectónicos y complejidad cognitiva sólo para una raíz declarada confiable. Rechaza extensiones Ruff, plugins/`mypy_path` de Mypy y rutas externas de Pyright. |
| `trusted-deep` | Los ocho de `trusted-static` más `pytest-coverage-trusted-deep` | Ejecuta el código, pruebas y `conftest.py` declarados únicamente para la identidad física exacta de `C:\Users\Victor\Neocortex\Repository`; mide branch coverage y contextos dinámicos por test. Es la frontera `trusted-execution`, nunca predeterminada. |

Ruff trusted selecciona `E4,E7,E9,F,B,C4,PIE,RUF`. Omite deliberadamente
`I,PT,SIM,UP`: ordenar imports, convenciones pytest, simplificación y
modernización generan señal demasiado amplia para la prioridad actual y no son
gates de esta plataforma. La política sigue derivándose del `pyproject.toml`
con el subconjunto permitido fijado por el adaptador; no es una invitación a
ejecutar todas las reglas configuradas en el repositorio.

Ruff y Mypy se resuelven desde el mismo intérprete de NeoCortex; ambos forman
parte de la base Python. Pyright `1.1.411` se mantiene como paquete npm aislado
junto al runtime y se ejecuta mediante Node, no mediante scripts del proyecto.
El entrypoint expone tanto el shim bajo
`venv\tools\pyright\node_modules\.bin` como el Node propiedad del runtime bajo
`tools\node`; la disponibilidad de Pyright no depende del `PATH` heredado del
shell que invoque el launcher.
Grimp `3.15` y Complexipy `6.2.0` pertenecen también a la base Python. La
selección focal conservó Grimp directo en lugar de envolver Import Linter
`2.13`: ambos resultaron viables, pero Grimp entrega el grafo directamente en
una API legible por máquina, mientras el reporte de contratos de Import Linter
no ofrece un contrato JSON y duplicaría la misma dimensión. Complexipy se usa
mediante su API `file_complexity`; su CLI devuelve un código distinto de cero al
superar su umbral predeterminado, semántica que no equivale por sí misma a un
fallo de proveedor.

Vulture `2.16` pertenece a la base Python y se invoca mediante su API
`Vulture.scavenge/get_unused_code` sobre el input staged exacto. No carga
configuración del proyecto ni ejecuta contenido. Sus findings `unused_code` y
confidence son señales heurísticas que requieren correlación posterior.

Los proveedores arquitectónicos conservan responsabilidades separadas:

- `ruff-analyze-imports` ejecuta Ruff Analyze como oráculo diferencial de
  imports, no como segundo dueño de los contratos;
- `grimp-architecture` produce el grafo normalizado, relaciones
  `module_import`, fan-in/fan-out, SCC/ciclos y evalúa los contratos v1;
- `complexipy-cognitive` publica complejidad cognitiva por símbolo y agregados
  total/máximo por módulo.

El dominio arquitectónico exacto son `neocortex`, `_01_Enumeracion`,
`_02_Deduplicacion`, `_03_Progreso`, `_04_Nucleo_Operativo` y
`_05_Interfaz`. `tests`, `tools`, `benchmarks` y el módulo de compatibilidad
independiente `Orquestador.py` quedan fuera de ese grafo de producción. Los
contratos `neocortex.code-architecture-contracts/v1` fijan fronteras reales,
allowlists explícitas y la membresía exacta de los SCC ya conocidos como
baseline `no-new`; no afirman que la arquitectura actual sea acíclica.

Cada adaptador usa salida estructurada, cwd/entorno controlados, caché efímera o
deshabilitada, límites de proceso, tiempo, memoria, inputs, diagnósticos y
salida. Todas las formas de fix permanecen deshabilitadas. Los descriptores
estáticos declaran `imports_content=false`, `executes_content=false` y
`uses_network=false`; el proveedor profundo declara las tres capacidades como
`true` porque Pytest carga contenido confiable y no impone un sandbox de red.
Todos declaran `authority=advisory` y `mutation_authority=false`.

Mypy y Pyright publican findings `typing` separados. `type_consensus` compara
únicamente proveedores completos y compatibles; cuenta `both_report`,
`mypy_only` y `pyright_only` por ruta, línea y categoría. No fusiona reglas ni
interpreta silencio como aprobación. Si uno no está listo, el resumen queda
`not_comparable`. El estado reserva la categoría `contradictory`, pero la
versión vigente no infiere contradicciones semánticas entre mensajes.

### Consenso advisory de código potencialmente no usado

`neocortex.code-unused-analysis/v1` exige Vulture y Pyright completos y alinea
sus candidatos con símbolos vigentes. Después correlaciona referencias/calls e
imports del grafo, reexports, `__all__`, entry points, callbacks, registries,
fixtures, Protocols, nombres especiales y Coverage observada. Su precedencia
produce exactamente `explained_usage`, `dynamic_usage_possible`,
`probable_unused_high_consensus` o `insufficient_evidence`; una observación de
Coverage puede explicar uso, pero la ausencia de cobertura nunca prueba no uso.

Un fixture etiquetado de calibración y un holdout separado publican precision,
recall, abstención, denominadores y firmas. Los gates de precisión de ambos
conjuntos deben pasar antes de crear un paquete de caracterización. Incluso
entonces el candidato sigue siendo advisory, requiere confirmación humana y
conserva cero autoridad de borrado o mutación. El análisis legacy
`probable_dead_symbol` permanece como conteo histórico separado, no se usa como
consenso.

Los gates son `calibration_probable_unused_precision`,
`calibration_probable_unused_recall_observed`,
`holdout_probable_unused_precision` y
`holdout_probable_unused_recall_observed`. El umbral de señal Vulture para
consenso alto es 0.90, pero sólo se evalúa después de las evidencias de uso y
contratos dinámicos; no equivale a una probabilidad calibrada de no uso.

Cada ejecución registra contrato, inputs y findings normalizados. Code schema
v4 conserva compatibilidad de lectura/migración con v1-v3 y añade
`external_metrics` y `external_relations`: identidades portables, sujeto o
extremos tipados, categoría/nombre/valor/unidad, dirección, confianza y metadata
determinista. Estas tablas tienen productor en los proveedores arquitectónicos
y consumidores en status, review, diff y work packages; no son un almacén
genérico sin uso. También registra contadores como archivos/bytes verificados,
bytes leídos o staged,
invocaciones, stdout/stderr, tiempo, findings, errores, timeouts y hits/misses de
caché. Un replay exacto vuelve a verificar todos los fingerprints, registra
`execution=cache_replay`, referencia la publicación completa y no abre procesos
ni duplica findings, métricas o relaciones. Counters de replay deben declarar
`process_invocations=0`, hits de caché y el costo real de volver a verificar los
inputs; no se reetiqueta ese costo como cero. La suite, sus proyecciones
compatibles y la finalización
de Code se confirman atómicamente. Una corrida parcial, indisponible o fallida
no puede aprobar su gate ni aparentar frescura.

### Ejecución profunda, Coverage y reanudación

`trusted-deep` no acepta mini-roots ni raíces configurables: antes de crear el
run exige que ruta resuelta e identidad física coincidan exactamente con
`C:\Users\Victor\Neocortex\Repository`. Su estado sigue siendo externo y
disjunto; el ejemplo operativo usa exclusivamente Laboratory:

```powershell
$Root = 'C:\Users\Victor\Neocortex\Repository'
$State = 'C:\Users\Victor\Neocortex\Laboratory\self-analysis\trusted-deep'
Neocortex --self-analysis --analysis-profile trusted-deep --root $Root --state-directory $State
```

Sin `--deep-test-selector`, Pytest recolecta la suite declarada completa. El
selector es repetible y acepta una ruta relativa bajo `tests/` o un node id. La
selección explícita se publica como `selected`; la ausencia de selectores, como
`full`. Los controles son:

| Opción | Predeterminado | Rango | Función |
|---|---:|---:|---|
| `--deep-max-tests` | 3000 | 1–5000 | Máximo de tests admitidos; una recolección mayor queda parcial. |
| `--deep-time-budget-seconds` | 600 | 30–900 | Presupuesto duro total de recolección y ejecución. |
| `--deep-shard-size` | 20 | 1–50 | Node ids exactos por shard reanudable. |

El proveedor inicia Coverage con branch coverage antes de cargar Pytest y
cambia el contexto dinámico por node id y fase (`setup`, `call`, `teardown`).
Normaliza resultados test→líneas→símbolos→módulos como métricas y relaciones
portables consumidas por status, review, diff y work packages. Coverage mide
sólo el proceso principal: no recoge subprocesses y publica explícitamente
`coverage_main_process_only` y `subprocess_coverage_not_collected`. Los archivos
de soporte ignorados por Git tampoco entran en la firma de soporte; la
limitación `git_ignored_support_files_excluded_from_support_signature` impide
presentar esa firma como observación exhaustiva de artefactos locales ignorados.

En Windows, el entorno efímero del worker fija `core.longpaths=true` mediante
`GIT_CONFIG_COUNT/KEY/VALUE` sólo para sus procesos hijos. No cambia la
configuración global, local ni del sistema de Git. Los diagnósticos extensos de
Pytest conservan tanto el encabezado como la causa final al truncarse, para no
ocultar el error operativo que cerró una prueba.

Cada shard queda ligado a fingerprints de código, soporte de pruebas, suite,
selección, configuración y versiones de Python/Pytest/Coverage. Sólo se conserva
un checkpoint si todas sus pruebas terminaron aprobadas. Una reanudación puede
reutilizar esos shards; los fallidos, incompletos, corruptos o incompatibles se
ejecutan de nuevo. Un replay exacto de toda la publicación conserva además el
contrato genérico de caché del proveedor.

## Preflight y disjunción obligatoria

La CLI exige que `--root` y `--state-directory` aparezcan de forma explícita.
Antes de crear estado valida una raíz local canónica, sin reparse, captura su
identidad física y comprueba que los dos árboles sean disjuntos en ambas
direcciones: el estado no puede ser la raíz, descendiente de ella ni ancestro
de ella. La frontera se vuelve a comprobar antes y después de crear el estado y
en los fences de E/S; un alias, reparse, cambio de identidad o intersección
indemostrable causa abstención.

El preset rechaza `--all`, `--apply`, `--route-only`, `--candidate-run`,
`--resume-run`, selecciones, operaciones directas, rutas distintas de `code`,
catálogo, organización y opciones de otras rutas. También rechaza habilitar
generated o vendored. Estos rechazos son parte del contrato, no sugerencias de
uso.

## Comandos canónicos y argv reproducible

Con un artefacto instalado que coincida con esta fuente, la forma canónica es:

```powershell
$Root = 'C:\Users\Victor\Neocortex\Repository'
$State = 'C:\Users\Victor\Neocortex\Laboratory\self-analysis\run-id'
# Elija un perfil para el estado de esta secuencia:
Neocortex --self-analysis --root $Root --state-directory $State
# o, sólo para una raíz confiable:
Neocortex --self-analysis --analysis-profile trusted-static --root $Root --state-directory $State
Neocortex --state-directory $State --code-status --code-json
Neocortex --state-directory $State --code-review
```

El manifest no guarda una cadena para reinterpretar en un shell. Guarda dos
arrays `argv` acotados, `commands.analyze` y `commands.status`, cuyo primer
elemento es literalmente `Neocortex`. El argv de análisis incorpora los
límites efectivos de `code`, `--no-code-generated`, `--no-code-vendored` y,
cuando aplican, `--code-max-count` y `--retry-code-errors`. Esto permite
reproducir la configuración sin perder quoting ni depender de una línea humana
abreviada.

Antes de promover `bin\Neocortex.exe`, valide `--version` y `--help` mediante
la ruta exacta `versions\<runtime-id>\venv\Scripts\Neocortex.exe`. La forma
`py -3 -m neocortex` sólo diagnostica el árbol fuente y no sustituye la
instalación canónica.

## Política de inventario y firma

Full scan y reconciliación USN consumen el mismo
`InventoryExclusionPolicy`. El perfil excluye explícitamente el directorio de
estado, `<ROOT>\.codex-lab`, `<ROOT>\docs\audit_evidence`,
`<ROOT>\Laboratory`, el `*.egg-info` canónico y los directorios transitorios de
pruebas detectados de forma acotada en la raíz (`.pytest-*` y `.test-tmp*`).
También excluye VCS, entornos, cachés, build/dist/target/out, cobertura,
vendored, temporales, backups, bytecode, logs y bases SQLite mediante reglas
acotadas que se guardan completas en el manifest.

La firma pública de esa política tiene la forma
`inventory-exclusion-policy-v2:xxh3_128:<digest>`. XXH3 es una identidad de
configuración no criptográfica; no es autenticación. Cambiar cualquier regla,
raíz explícita o versión cambia la firma y bloquea la reutilización de estado
incompatible.

## Puerta incremental de tres evidencias

Un checkpoint sólo autoriza reconciliación incremental cuando coinciden tres
propietarios:

1. el **último** run durable del framework para la raíz es `self_analysis`,
   `analyze_only`, tiene la misma firma de política y conserva la misma
   identidad física; no se retrocede a un run histórico compatible si el más
   reciente no coincide;
2. el checkpoint publicado por Dedup v9 es válido, referencia exactamente el
   mismo `scan_id`, firma cruda de exclusión y cursor durable, y el scan
   completo conserva raíz, identidad y conteo de archivos coherentes;
3. la raíz viva mantiene identidad y el cursor USN vivo es compatible con el
   límite durable.

Si una evidencia falta o discrepa, `allow_incremental=False` fuerza un scan
completo sin invalidar el checkpoint existente. Por tanto, un checkpoint
publicado por un run que después falló no basta para autorizar incremental.

Si la consulta inicial del journal falla por acceso o indisponibilidad, el
autoanálisis ejecuta un único recorrido completo portable sin reconciliación
USN. En ese modo persiste nulos `journal_volume`, `journal_id`, `start_usn` y
`end_usn`, no publica checkpoint y registra `journal.status=unavailable`. No es
un snapshot atómico ni se presenta como inventario incremental. La ruta `code`
sí conserva su caché por identidad/metadatos, de modo que un replay sin cambios
relee el árbol pero no vuelve a extraer ni analizar contenido. La corrida normal
usa la misma enumeración portable cuando USN no está disponible, pero sí publica
un checkpoint Dedup v9 con cursor nulo para que sus consumidores comparen el
snapshot contra sus caches; USN es sólo un acelerador opcional.

## Ceros durables y guards de mutación

La publicación final exige exactamente una ruta `code` completada y verifica
en la misma transacción que los conteos de `route_candidates`, `file_actions`,
`run_actions` y eventos de organización sean todos cero. Sólo entonces marca el
run `completed` y agrega su manifest; cualquier discrepancia revierte la
finalización.

La defensa no depende sólo del preset. Framework v20 persiste el modo, raíz e
identidad protegidas y la firma de inventario; sus triggers vuelven inmutables
esas fronteras y rechazan vincular acciones a un run `analyze_only`. Además,
`CorpusMutationGuard` rechaza el run junto a los owners de acciones,
organización y recuperación, vuelve a verificar identidad y propaga
`ProtectedAnalysisRootError` en lugar de degradarlo a un fallo operativo
permisivo. Las primitivas físicas mantienen sus preflights identity-bound y
*no-replace*; no existe un fallback por ruta para el autoanálisis.

## Manifest y status estrictamente read-only

La finalización publica un único
`neocortex.self-analysis-manifest/v2`, limitado a 256 KiB, en el evento
`self-analysis-manifest`. Run completado y manifest se confirman juntos. El
documento liga:

- run, modo, raíz, identidad y estado;
- scan, modo de inventario, journal disponible con cursores o estado
  `unavailable`, reglas y firma de política;
- ruta `code`, `input_source=inventory_snapshot`, firma, summary y contadores
  de evidencia externa efectivos;
- los cuatro conteos de seguridad en cero;
- los dos arrays argv canónicos.

En `trusted-deep` añade `deep_analysis`: declara
`content_executed=true`, `suite_selection`, selectores, límites y una firma de
configuración. La ausencia, duplicación o incoherencia de esos controles invalida
el manifest en lugar de reconstruir supuestos.

La consulta compatible es `--code-status --code-json`. Sólo añade
`self_analysis` cuando el último run de code está ligado a un autoanálisis; un
run normal conserva `self_analysis: null`. `manifest_status` puede ser
`valid`, `missing`, `ambiguous` o `invalid`. La frescura separa identidad de
raíz, vínculo code/framework, checkpoint de inventario y estado del journal
(`unchanged`, `advanced`, `discontinuous` o `unavailable`); `current=true`
requiere todas las cercas positivas y journal sin cambios.

Para una publicación profunda, el mismo status añade `test_coverage` con el
estado de `pytest-coverage-trusted-deep`, suite `full` o `selected`, completitud,
resultados de pruebas, totales de líneas y ramas, conteos de módulos/símbolos y
relaciones test→símbolo, gates y limitaciones. La salida de consola permanece
acotada; JSON conserva ejemplos limitados y conteos totales.

En ambos perfiles trusted, status añade `unused_analysis`: proveedores,
firmas, cuatro estados, calibration/holdout, gates, conteos y ejemplos acotados.
Si Vulture o Pyright no están listos, esta dimensión se abstiene sin invalidar
los demás proveedores.

El decoder conserva lectura estricta del manifest histórico v1. Un manifest v2
con journal no disponible puede ser válido como evidencia de una corrida
completada, pero necesariamente expone
`inventory_checkpoint_current=false`, `journal_status=unavailable` y
`current=false`.

Este status no crea, migra, repara ni hace checkpoint. Abre cada SQLite como
`mode=ro&immutable=1`, activa `query_only`, y compara identidad, tamaño y mtime
antes y después. La presencia de `-wal`, `-shm` o `-journal` junto a
`code.sqlite3`, `framework.sqlite3` o `dedup.sqlite3` —incluso un auxiliar vacío
o desacoplado— o una cerca inestable en cualquiera de ellas causa abstención
total con código `2`. No emite una vista parcial ni crea sidecars.

## Revisión determinista de la publicación

`--code-review` es el primer consumidor de mantenimiento del autoanálisis. No
se combina con `--self-analysis`: el productor debe completar y publicar
primero; después, la consulta lee ese snapshot con las mismas cercas estrictas.
No crea bases, no migra, no hace checkpoint y no modifica código ni estado.

```powershell
Neocortex --state-directory $State --code-review
Neocortex --state-directory $State --code-review --code-json
Neocortex --state-directory $State --code-review --code-review-limit 50 --code-json
```

El contrato `neocortex.code-review/v8` conserva compatibilidad con v2-v7 y la proyección
legacy `external_evidence`; añade `external_evidence_suite` con perfil, estado,
proveedores, cobertura, counters, gates y consenso de tipos. Añade además
`architecture`, que consume métricas/relaciones persistidas y resume
módulos, símbolos, imports, SCC, ciclos, contratos y tres estados explícitos:
`import_graph_consensus`, `architecture_contracts` y
`module_complexity_displacement`. Cada estado puede quedar `not_evaluated`; la
ausencia de un proveedor o baseline comparable nunca equivale a aprobación.
`test_coverage` añade los resultados profundos sin reinterpretar una suite
parcial como completa: enumera ejemplos acotados de módulos/símbolos con líneas
o ramas faltantes y relaciones test→símbolo observadas.
Esta evidencia no
cambia ranking, actionability o selección de paquetes. `findings` selecciona
sólo diagnósticos Python confirmados `high_complexity` y `long_function`,
enumera hasta 10 000 hotspots y mantiene el ranking v2 auditable. Devuelve 10
por defecto, primero uno por archivo y luego un segundo hasta completar;
`--code-review-limit` admite de 1 a 50 y exige JSON por encima de 10. La
puntuación bruta no cambió:

```text
complexity_bp
+ floor(length_bp / 4)
+ 250 * min(callers_estáticos_resueltos, 20)
```

Cada finding conserva rango, firma, fingerprints del archivo, versión del
analizador, valor/umbral y hasta tres callers resueltos. Separa callers y
módulos consumidores de producción, pruebas, fixtures, herramientas y
compatibilidad. `hotspot_id` identifica establemente la evidencia física y el
símbolo; `finding_id` identifica la interpretación bajo versiones concretas de
ranking y actionability.

`recommendations` filtra como máximo tres `act_now` mediante
`python-maintenance-actionability-v1`. La clasificación determinista distingue
algoritmos, builders, classifiers, initializers, lifecycle, orquestadores,
persistencia, recuperación, reglas y validators. Builders se difieren;
validators, reglas e invariantes exigen caracterización; y una construcción no
reconocida devuelve `insufficient_evidence`. Cada recomendación enumera riesgo,
evidencia exacta, contratos a preservar y validación sugerida. El score bruto y
todos los hotspots siguen disponibles: esta capa no es ground truth humano ni
autoriza modificar código. Cero hallazgos o cero `act_now` son respuestas
válidas; en el segundo caso `recommendation_status=abstained` explica la brecha.

`work_packages` añade una tercera capa mediante
`python-maintenance-work-packages-v4`. Conserva como máximo un paquete de
mantenimiento que toma la primera
recomendación como `primary_change_target` y consulta siempre un pool fijo de 50
hotspots, aunque la vista solicitada sea menor. Sólo incorpora
`contract_guard`s alcanzados por una llamada confirmada directa o por dos saltos
a través de un símbolo Python vigente, completo, no generado y de producción.
No usa coincidencias de nombre, ruta, módulo ni caller compartido. Cada relación
conserva profundidad, símbolo puente, confianza mínima y provenance; el paquete
expone riesgo conservador, módulo primario, contratos afectados, cadenas de
imports acotadas, orden de ejecución, validación y los gates
históricos de caracterización exacta, cero hotspots sustitutos, cero
resoluciones corregidas/perdidas y replay completamente incremental. También
expone los gates de proveedor normalizados y los gates arquitectónicos
`architecture_contracts_not_degraded`, `no_new_import_cycles` y
`module_complexity_not_displaced`. Cada uno sólo puede aprobarse frente
a una línea base comparable del mismo adaptador, versión, configuración y
entorno. La evidencia profunda añade la proyección del símbolo objetivo, sus
pruebas protectoras observadas, líneas/ramas faltantes y los gates
`tests_passed`, `coverage_available`, `work_package_target_protected`,
`line_coverage_not_degraded` y `branch_coverage_not_degraded`. Los dos últimos
requieren un publication diff comparable; no encontrar una relación produce
`unprotected`, no un permiso para editar. La primera publicación sana queda
`baseline`; si falta el proveedor o
cambia su firma queda `not_evaluated` o `abstained` sin borrar los demás. Los
guards no son objetivos automáticos y el paquete nunca autoriza modificar código.
Además puede entregar hasta tres paquetes `unused_characterization` para
consenso alto con gates de precisión aprobados. Sólo ordenan caracterización,
pruebas y confirmación humana; declaran `mutation_authority=false`.

`probable_dead_symbol` se informa únicamente como conteo suprimido. Una muestra
portable de 40 entre los 246 candidatos de rc11 encontró 36 usos demostrables,
un contrato externo y sólo tres candidatos de revisión. Su precisión máxima
provisional fue 0.075 y exige abstenerse en 37/40 casos; por tanto falló el gate
de 0.90 y no se habilita como finding ni como recomendación de borrado. La
resolución estática no observa dispatch dinámico, callbacks, registros ni todos
los contratos de importación.

El analizador Python conserva además el binding léxico de imports y aliases. El
resolvedor sólo lo usa cuando no existe shadowing local: primero exige un
qualified name único y, si el nombre procede de una fachada interna, permite un
único salto confirmado por `import_binding` o por un submódulo físico único del
paquete. Imports externos, aliases ambiguos, comprehensions y nombres
redefinidos permanecen sin enlazar. El porcentaje global de calls resueltas es
descriptivo —su denominador incluye builtins, APIs externas y dispatch
dinámico— y no debe convertirse en objetivo aislado de calidad.

La línea base de actionability vive en
`tests/fixtures/code_review/rc6_top10_actionability_v1.json`. La ampliación
representativa está en `rc11_top40_actionability_v2.json`: reúne la unión de
los top 40 de ambos rankings, 41 símbolos etiquetados como builders,
validadores, reglas, algoritmos y orquestadores. El ranking v2 elevó la
`Precision@10` provisional de 0.60 a 0.70 y dejó iguales P@20, P@30 y P@40;
`build_parser` pasó del rango 2 al 39. Es revisión estática reproducible, no
ground truth humano, y el score sigue sin representar riesgo calibrado.

La regresión temporal rc14 retira `execute_knowledge_search`: el rango bruto 1,
`GoldenCase._validate_required_feature`, queda como
`validator/characterize_first`, mientras
`semantic_generation_repository._queue_job_rows_bounded` se convierte en la
primera recomendación `act_now`. rc17 aplica esa recomendación: el orquestador
de persistencia baja de 302 líneas/complejidad 44 a 47/3. El diff rc16→rc17
retira sólo ese hotspot, no añade otro, no cambia evidencia común y conserva
cero resoluciones corregidas o perdidas. El replay instalado obtuvo 515/515
cache hits y cero trabajo de análisis/grafo. Esta secuencia valida el gate sobre
un cambio posterior, pero no demuestra calibración universal.

rc18 aplica la siguiente recomendación: `_derive_context_graph` pasa de 279
líneas/complejidad 43 a un coordinador de nueve líneas con validación,
acumulación y materialización separadas. El diff rc17→rc18 reduce los hotspots
de 183 a 182, retira sólo el objetivo, añade cero y conserva cero resoluciones
nuevas, corregidas o perdidas sobre 58 568 calls comunes. El replay desde el
wheel instalado obtuvo 515/515 cache hits y cero trabajo de lectura, análisis,
persistencia o grafo. Una comparación diferencial rc17/rc18 sobre relación Code
válida, duplicado planeado y evidencia inválida produjo JSON idéntico, incluidos
orden e IDs estables. La primera recomendación `act_now` queda ahora en
`knowledge_exact._lookup_catalog`; Publication Diff v1 fue suficiente para esta
decisión y no justifica todavía ampliar su contrato.

rc19 aplica esa recomendación: `_lookup_catalog` pasa de 225 líneas/complejidad
44 a un wrapper de 58/5. Preflight generacional, decodificación, ordenamiento,
cobertura y reportes quedan separados; una regresión multitérmino fija orden,
límites, provenance y ausencia de escritura de contenido SQLite. El primer
candidato rc19 se rechazó porque la propia regresión apareció como hotspot; al
dividir sus verificaciones, el diff final rc18→rc19 reduce los hotspots de 182 a
181, retira sólo `_lookup_catalog`, no añade otro y conserva cero resoluciones
nuevas, corregidas o perdidas sobre 58 451 calls comunes. El replay desde el
wheel instalado obtuvo 515/515 cache hits y cero trabajo de lectura, análisis,
persistencia o grafo. La primera recomendación pasa a
`document_taxonomy.classify_document`; Publication Diff v1 vuelve a ser
suficiente para decidir.

rc20 añade el planificador y lo prueba primero sobre la publicación rc19. El
paquete raíz `document_taxonomy.classify_document` enlaza, mediante cadenas
confirmadas a dos saltos, `_normative_document_evidence` y
`_plausible_authority_identifier` como guards; Knowledge y Semantic permanecen
fuera. Una matriz sintética congela 30 payloads completos y dos seams de
ambigüedad antes del refactor. `classify_document` queda como coordinador y la
regla normativa separa exclusiones CFE, señales formales, referencias directas,
bloqueos operativos y fallback de ruta sin cambiar `CLASSIFIER_VERSION` ni un
solo fingerprint. El primer candidato rc20 fue rechazado porque el propio
autoanálisis detectó `review_code_state` con complejidad 15; la partición final
elimina ese reemplazo. El diff rc19→rc20 retira los dos hotspots Taxonomy, añade
cero, no cambia evidencia común y conserva cero resoluciones nuevas, corregidas
o perdidas. La siguiente raíz pasa a `knowledge_exact.lookup_exact`.

La consulta exige manifest válido, último run Code completado e identidades de
raíz/framework ligadas. Un snapshot full terminado con journal no disponible
puede leerse como `freshness=publication_only`, `current=false` y limitación
explícita: demuestra qué se publicó, no que el árbol vivo no cambió después.
Journal `advanced`/`discontinuous`, vínculo incompatible, sidecars o schema no
admitido causan abstención con código `2`. El digest excluye timestamps, rutas
locales, IDs SQLite, firma de input local y modo full/replay. Sí incorpora la
comparabilidad y los gates externos: adquirir por primera vez una línea base
comparable cambia honestamente la decisión aunque los findings permanezcan
iguales.

## Comparación read-only entre publicaciones

`--code-publication-diff` convierte la comparación de dos publicaciones Code
en una operación canónica, acotada y determinista. El envelope v6 conserva
compatibilidad declarada con v1-v5 y añade deltas de consenso unused a los
deltas de Coverage, arquitectónicos y por proveedor y al veredicto agregado. El
argumento identifica el estado baseline; `--state-directory` identifica la
publicación actual:

```powershell
Neocortex --state-directory $CurrentState --code-publication-diff $BaselineState
Neocortex --state-directory $CurrentState --code-publication-diff $BaselineState --code-json
```

Ambos estados deben contener un último run completado, schema compatible y
`code.sqlite3` quiescente sin `-wal`, `-shm` ni `-journal`. La consulta abre las
bases como snapshots immutable, no migra, no hace checkpoint y no escribe
ningún owner. Compara como máximo 250 000 calls y 20 000 hotspots; conserva
conteos totales y hasta 20 ejemplos por clase.

Una call común exige la misma ruta relativa, rango de bytes y nombre. Sobre
esas calls informa resoluciones nuevas, corregidas, perdidas, estables y aún no
resueltas; los cambios de texto que desplazan un rango aparecen honestamente
como sitios exclusivos de una publicación. Los hotspots se identifican por
ruta relativa y qualified name. El conteo `probable_dead` se muestra sólo como
delta no calibrado y nunca autoriza cambios de código o corpus.
Cada proveedor se compara únicamente cuando ambas publicaciones están listas y
conservan su firma de comparabilidad; entonces informa IDs comunes, añadidos y
resueltos y el gate correspondiente. Un baseline histórico Ruff-only se lee por
la proyección compatible; los proveedores ausentes o incompatibles producen
`not_evaluated`, no corrupción ni un falso aprobado. El veredicto no convierte
una suite parcial en certeza y conserva las limitaciones observadas.

Cuando los proveedores arquitectónicos y su dominio son comparables, el diff
informa por módulo deltas de fan-in, fan-out y complejidad total/máxima, cambios
de relaciones de import y SCC/ciclos, así como contratos que mejoran o se
degradan. `module_complexity_displacement` sólo puede evaluarse en esa
comparación: detecta que una reducción aparente de un objetivo reaparezca en
otro símbolo o módulo relacionado, sin convertir la métrica en probabilidad de
defecto. Un primer snapshot queda `baseline`; cambio de versión, configuración,
entorno, raíz o cobertura deja la dimensión `not_evaluated` y explica la causa.

Coverage sólo es comparable si ambas publicaciones están listas y coinciden
proveedor, versiones, configuración, selección, firma de suite y alcance de
medición. En ese caso el diff informa deltas de líneas y ramas y evalúa
`line_coverage_not_degraded` y `branch_coverage_not_degraded`. Una suite parcial,
un selector distinto o el límite `max_tests` alcanzado deja esos gates
`not_evaluated`; no se extrapola cobertura completa.

La dimensión `unused_analysis` sólo es comparable cuando coinciden provider,
policy y firmas de calibración/holdout. Informa candidatos añadidos/retirados,
cambios entre los cuatro estados y consenso alto añadido/resuelto. Su gate es
observacional y nunca autoriza borrar o modificar.

## Consulta multidimensional de publicaciones

La superficie pública unificada es:

```powershell
Neocortex --state-directory $State --code-query status
Neocortex --state-directory $State --code-query review --code-json
Neocortex --state-directory $CurrentState --code-query diff --code-query-baseline $BaselineState --code-json
```

No es otro productor. `status`, `review` y `diff` reutilizan sus envelopes
publicados y después aplican una proyección acotada; la operación no recorre la
raíz, ejecuta proveedores, crea/migra estado, hace checkpoint ni escribe las
bases. `--code-query-baseline` es obligatorio y exclusivo de `diff`.

Las seis dimensiones de filtro son repetibles:

| Dimensión | Opción |
| --- | --- |
| proveedor | `--code-query-provider VALUE` |
| categoría | `--code-query-category VALUE` |
| módulo | `--code-query-module VALUE` |
| estado | `--code-query-status VALUE` |
| delta | `--code-query-delta VALUE` |
| work package | `--code-query-work-package VALUE` |

Los valores repetidos dentro de una dimensión se combinan con OR; dimensiones
distintas se combinan con AND. El módulo seleccionado incluye su coincidencia
exacta y descendientes, sin convertir prefijos de texto ajenos en módulos. El
límite es 50 por defecto y acepta 1–500. La salida humana conserva resumen,
filtros, matches y limitaciones mediante líneas `CODE_QUERY*`; `--code-json`
entrega el envelope auditable.

La consulta conserva categorías, gates, evidencia, deltas, estados y work
packages como dimensiones separadas. Publica `aggregate_score=null` y
`defect_probability=null`, no estima ninguno de los dos y no transforma
consenso, cobertura, mutación, historia o centralidad en autoridad de cambio.

## Mini-root de laboratorio

Use contenido sintético y dos hermanos disjuntos. No copie el corpus real para
convertirlo en fixture:

```powershell
$Lab = Join-Path $env:LOCALAPPDATA 'Neocortex\self-analysis\fixtures'
$MiniRoot = Join-Path $Lab 'mini-root'
$MiniState = Join-Path $Lab 'mini-state'

Neocortex --self-analysis --root $MiniRoot --state-directory $MiniState
Neocortex --state-directory $MiniState --code-status --code-json
Neocortex --state-directory $MiniState --code-review --code-json
```

El ejemplo presupone que un fixture sintético de 20–50 archivos ya creó
`$MiniRoot`; esa raíz acotada es el límite físico y permite una publicación
completa del perfil `protected`. Para probar `trusted-static`, el fixture debe
incluir su `pyproject.toml` versionado y el runtime debe contener los ocho
proveedores. `trusted-deep` nunca acepta este mini-root: exige exclusivamente la
raíz canónica y un estado separado bajo Laboratory. No autoriza
crear, copiar o limpiar datos fuera del laboratorio. Use un estado nuevo por
secuencia aislada; para validar full→incremental/no-op/cambio, reutilice ese
mismo estado sólo dentro de la secuencia controlada y con la misma configuración.
Cierre writers y confirme quiescencia antes de ejecutar status.

## Topología y validación operativa

El repositorio canónico es `%USERPROFILE%\Neocortex\Repository`; sus estados de
autoanálisis pertenecen a árboles nuevos bajo
`%LOCALAPPDATA%\Neocortex\self-analysis`, nunca dentro del repositorio. Cada
smoke debe usar un estado independiente y el ejecutable exacto del runtime que
se pretende promover. Un mini-root valida el contrato técnico, pero no sustituye
la validación explícita de la raíz completa ni autoriza reutilizar su estado.

## Route-only/resume con cero candidatos

El modo genérico code-only ya usa `RouteAdapter.input_source` y puede consumir
un inventario durable aunque haya cero `route_candidates`:

```powershell
$Root = 'C:\Datos'
$State = 'C:\Estado\Neocortex'
$RunId = 40
Neocortex --root $Root --state-directory $State --route code --route-only
Neocortex --root $Root --state-directory $State --route code --route-only --candidate-run $RunId
Neocortex --root $Root --state-directory $State --resume-run $RunId
```

Sin `--candidate-run`, examina el owner durable más reciente de la raíz exacta
y exige que sea `normal`; una discrepancia falla sin retroceder a un run
histórico aunque contenga filas MIME. Cero candidatos sólo se
admite cuando todas las rutas seleccionadas declaran
`input_source=inventory_snapshot`; una ruta MIME o selección mixta falla antes
de crear o ejecutar el run. Este contrato no relaja el preset:
`--self-analysis` continúa rechazando route-only y resume por diseño.
