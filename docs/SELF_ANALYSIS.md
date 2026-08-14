# Autoanálisis de código y evidencia externa

> **Estado del contrato.** Esta capacidad pertenece a la fuente `0.9.0` bajo
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
| `trusted-deep` | Los ocho de `trusted-static` más `pytest-coverage-trusted-deep` | Ejecuta el código, pruebas y `conftest.py` declarados únicamente para la identidad física exacta de `$HOME\Neocortex\Repository`; mide branch coverage y contextos dinámicos por test. Es la frontera `trusted-execution`, nunca predeterminada. |

Ruff trusted selecciona `E4,E7,E9,F,B,C4,PIE,RUF`. Omite deliberadamente
`I,PT,SIM,UP`: ordenar imports, convenciones pytest, simplificación y
modernización generan señal demasiado amplia para la prioridad actual y no son
gates de esta plataforma. La política sigue derivándose del `pyproject.toml`
con el subconjunto permitido fijado por el adaptador; no es una invitación a
ejecutar todas las reglas configuradas en el repositorio.

Ruff y Mypy se resuelven desde el mismo intérprete de NeoCortex; ambos forman
parte de `analysis` y de la release canónica `full`, no de la base mínima.
Pyright `1.1.411` se mantiene como paquete npm aislado
junto al runtime y se ejecuta mediante Node, no mediante scripts del proyecto.
El entrypoint expone tanto el shim bajo
`venv\tools\pyright\node_modules\.bin` como el Node propiedad del runtime bajo
`tools\node`; la disponibilidad de Pyright no depende del `PATH` heredado del
shell que invoque el launcher.
Grimp `3.15` y Complexipy `6.2.0` pertenecen también a `analysis`/`full`. La
selección focal conservó Grimp directo en lugar de envolver Import Linter
`2.13`: ambos resultaron viables, pero Grimp entrega el grafo directamente en
una API legible por máquina, mientras el reporte de contratos de Import Linter
no ofrece un contrato JSON y duplicaría la misma dimensión. Complexipy se usa
mediante su API `file_complexity`; su CLI devuelve un código distinto de cero al
superar su umbral predeterminado, semántica que no equivale por sí misma a un
fallo de proveedor.

Vulture `2.16` pertenece a `analysis`/`full` y se invoca mediante su API
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
contratos `neocortex.code-architecture-contracts/v1` fijan fronteras reales y
allowlists explícitas. Su baseline
`neocortex-production-imports-2026-08-10/v2` es vacío: el grafo vigente tiene
cero SCC y cualquier ciclo nuevo o histórico reintroducido falla cerrado.

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
`$HOME\Neocortex\Repository`. Su estado sigue siendo externo y
disjunto; el ejemplo operativo usa exclusivamente Laboratory:

```powershell
$Root = Join-Path $HOME 'Neocortex\Repository'
$State = Join-Path $HOME 'Neocortex\Laboratory\self-analysis\trusted-deep'
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
$Root = Join-Path $HOME 'Neocortex\Repository'
$State = Join-Path $HOME 'Neocortex\Laboratory\self-analysis\run-id'
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
2. el checkpoint publicado por Dedup v10 es válido, referencia exactamente el
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
un checkpoint Dedup v10 con cursor nulo para que sus consumidores comparen el
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
`--code-review-limit` acota cada familia de observaciones de 1 a 50 y exige
JSON por encima de 10. La
puntuación bruta no cambió:

```text
complexity_bp
+ floor(length_bp / 4)
+ 250 * min(callers_estáticos_resueltos, 20)
```

Cada finding conserva rango, firma, fingerprints del archivo, versión del
analizador, valor/umbral y hasta tres callers resueltos. La separación por
`production`, `test`, `fixture`, `tool` y `compatibility` se publica
explícitamente como **convención de ruta**, no como ownership ni reachability de
runtime. `hotspot_id` identifica establemente la evidencia física y el símbolo;
`finding_id` identifica la interpretación versionada.

El envelope vigente es `neocortex.code-review/v17` y no declara compatibilidad
con schemas anteriores: conserva el corte de autoridad y usa el contrato general
`neocortex.code-analysis-epistemics/v1`. Cada finding separa
observación, hipótesis, readiness
de pregunta, evidencia satisfecha/faltante, contraevidencia pendiente,
readiness de decisión y siguiente acción. Un umbral estructural confirma sólo
la observación y abre la pregunta
`maintenance.structural_hotspot_requires_change/v1`; la inferencia se abstiene,
la decisión queda `experiment_required`, `construction` y `change_risk` quedan
`unknown`, y `mutation_authority=false`.

Cada evaluación general incluye `question_spec_fingerprint`, identidad de
snapshot/revisión, IDs de origen, un digest de la proyección y la versión del
resolver. Antes de publicarla, Code vuelve a consultar los registros read-only.
Para funciones exige que rango, archivo, hashes, umbral, valor y procedencia
coincidan. Para clases, `neocortex.code-class-surface/v1` exige el símbolo actual
y el agregado completo de miembros AST directos confirmados. Esa resolución
confirma el origen de la observación; no resuelve semántica ni propiedad lógica.

La primera extensión más allá de funciones selecciona una clase cuando su span
es de al menos 500 líneas o tiene al menos 20 métodos directos. Esos valores son
un filtro provisional de atención y se publican como tales, no como umbral de
defecto. Nombre, ruta, bases y decorators no alteran la selección. La pregunta
`maintenance.class_surface_requires_change/v1` conserva dos hipótesis: una
superficie puede combinar responsabilidades accidentales o puede ser cohesiva,
declarativa, protocolaria o un composition root intencional. Hasta resolver rol,
consumidores, cohesión método-estado, historia y contraevidencia, toda evaluación
queda `experiment_required`, sin recomendación ni autoridad de mutación.

`neocortex.code-interface-surface/v1` amplía la observación a módulos,
configuraciones y CLI. Para módulos conserva span, símbolos directos, API
pública, dependencias y referencias confirmadas; los thresholds sólo
seleccionan atención. Para configuración decodifica el texto publicado con
límites, parsea JSON/TOML y publica exclusivamente conteos y nombres de keys,
nunca valores. Para CLI selecciona archivos por calls
`add_argument`/`add_parser`, reabre el AST publicado y separa literales de
construcción dinámica. No ejecuta builders ni afirma que esos parsers lleguen
al comando público.

La arquitectura publicada deja de ser sólo una sección paralela: v17 deriva
preguntas generales para el grafo estático comparable y para las evaluaciones de
los contratos de imports. Sus evidence refs conservan snapshot, digest, gates,
conteos, discrepancias y violaciones; incluso un contrato fallido permanece
advisory y sin autoridad de mutación. Una tercera pregunta hace explícito el
límite y la cobertura de un registry versionado, deliberadamente parcial, para
`text`, `semantic`, `knowledge`, `review`, `retention` y `framework`. La
proyección módulo→owner conserva módulos no mapeados, solapamientos y edges
cross-owner; no asigna un owner por defecto. El campo histórico
`path_namespace_id` nunca se promueve a ownership y logical owner tampoco se
confunde con state owner o state store.

La primera pregunta durable cross-owner no intenta fabricar un grafo SQL.
`neocortex.code-state-projection/v1` compara, por modelo de texto publicado, el
set de `documents.revision_id` realmente elegible para el adaptador Text con el
set de revisiones capturado en `embedding_generation_members` y
`semantic_item_revisions` del head Semantic. Verifica además owner `text` y
materialización `text_representation`. Cada owner se abre por separado con
`immutable=1`; el lector exige schema actual, contrato exacto, WAL de cero bytes
o ausencia de sidecars y fences idénticos antes/después. No usa `ATTACH`, no
escribe, no checkpointa y no llama transacción distribuida a la comparación.

Una fila Text `complete` con cero caracteres se conserva como control excluido,
no como revisión faltante. `aligned` y `delta_observed` son observaciones; ambas
mantienen inferencia `abstained`, decisión `experiment_required` y
`mutation_authority=false`. Un delta exige inspeccionar build, freshness,
recovery y reconciliación antes de formular una hipótesis de corrupción. Incluso
una alineación exacta no prueba muerte de proceso, power loss ni recovery.

Existe ya un experimento aislado específico para una incertidumbre más acotada:
un proceso hijo `spawn` ejecuta staging de 130 items y termina con `os._exit(77)`
al comenzar el item 129, después del prefijo durable de 128. El padre exige
exactamente 128 items/256 chunks/256 jobs, generación `building` y ningún head;
reanuda la misma generación y sólo después de completar 130/260/260 el worker
publica atómicamente 260 miembros. Esto demuestra supervivencia y reanudación
para ese crash point de proceso sobre SQLite; no simula power loss, corrupción
de almacenamiento, todas las fronteras ni recuperación cross-owner.

v17 integra además `code-state-topology`, `code-state-interactions`,
`code-change-evolution`, `code-assurance`, `code-invariant-assurance`,
`code-capability-reachability`, `code-route-capabilities`,
`code-analyzer-calibration` y `code-analyzer-effectiveness`. Topología verifica
el cierre relacional terminal Text owner-local. El analizador de interacciones
reabre el AST publicado, analiza SQL literal con SQLGlot/SQLite y separa
READ/WRITE/DDL, SQL dinámico, errores de parseo, BEGIN/COMMIT/ROLLBACK y fronteras
workflow declaradas. Los parámetros numerados `?NNN` se adaptan mediante tokens
al placeholder anónimo que entiende SQLGlot; no se modifica texto entre comillas
ni la evidencia/digest original. No infiere store por `connect`, `execute`, nombre de módulo
o suffix `repository`.

Evolución separa contenido/API, relocations, historia Git y el schema Code;
assurance distingue ejecución de tests, mutación, ASSERTS y escenarios; el
registry de invariantes enlaza cuatro escenarios exactos —diez nodeids una vez
expandidos los parámetros— y sólo acepta outcomes trusted-deep actuales para el
conjunto completo de cada escenario. Reachability enlaza manifests Text con
receipts/outbox/heads, mientras el registry de rutas demuestra sólo el nivel de
evidencia realmente disponible para las nueve rutas built-in. Calibración
preserva labels provisionales sin convertirlas en ground truth y autoeficacia
compara por digest la publicación más reciente contra el inventario Git visible.
Las preguntas de seguridad/dependencias consumen los seis gates ya existentes de
`supply_chain`. Provider ausente, stale, parcial o incompatible se publica como
evidencia faltante, nunca como ausencia de problema. Precision, recall y
finding→decision permanecen sin calcular hasta enlazar outcomes humanos o
defectos escapados independientes.

El planner `neocortex.code-experiment-plan/v2` cubre toda evaluación
`experiment_required`, elige por coste/atención/timeout y conserva alternativas.
La identidad portable de cada propuesta incluye una proyección de contratos,
facts, completitud y requirements, pero excluye IDs locales de captura. Por
ello un replay exacto conserva receipts y un cambio real de evidencia los
invalida aunque el subject lógico conserve su nombre.
Templates sin runner siguen como planes de caracterización. La CLI sólo ejecuta
dos templates source-versioned con gates medidos: la ruta pública Text, con un
nodeid, y el workflow SQL/transaccional Text, con cuatro. Los cuatro escenarios
de invariantes y sus diez nodeids pertenecen al assurance registry, pero no son
automáticamente propuestas ejecutables.

`--code-experiment-run` exige un proposal ID del plan vigente y ejecuta
pytest/coverage con timeout sobre el checkout canónico confiable. El temporal
externo aloja runtime/checkpoints; no copia la fuente ni crea un sandbox de
seguridad. Trusted-deep conserva el `HOME` canónico y declara
`uses_network=true`. El receipt `neocortex.code-experiment-receipt/v3` conserva
identidad del provider, manifest, outcomes y gates de los nodeids seleccionados,
además del digest Code before/after. El provider recalcula antes y después la
firma de los inputs Python publicados y del soporte Git observado; una
diferencia rechaza el resultado. El digest cerca `code.sqlite3` durante la fase
ejecutora. Ninguna de las dos barreras es un lock continuo ni prueba
inmutabilidad del corpus u otros stores.

Code schema v6 agrega el resultado terminal a
`code_experiment_receipts`, tabla append-only protegida contra update/delete. La
salida JSON del comando es `neocortex.code-experiment-store/v1` y contiene el
receipt medido. La escritura ocurre **después** del digest after: por tanto
`code_database_unchanged=true` no describe la invocación completa como read-only.
Se almacenan `passed`, `failed` y `abstained`, ligados al run, processing
signature, evaluación, pregunta, sujeto, proposal, template y digest del review;
un envelope digest recalculable cubre también timestamp y digests/tamaño del
payload.
El payload canónico está limitado a 1 MiB, el store admite hasta 32 receipts por
proposal y el review resuelve como máximo 256 proposals ejecutables. Repetir el
mismo `receipt_id` y contexto es idempotente; una colisión o un bound excedido
falla cerrado.

v17 incorpora un linker fail-closed y de alcance explícito. Para cada proposal
y processing signature vigentes, el review evalúa el terminal más nuevo y sólo
proyecta `passed`; un `failed` o `abstained` posterior invalida un pass anterior.
El receipt puede venir de un owner Code completado previo si la publicación
actual es un replay exacto con la misma firma. Sólo reemplaza requisitos que
tengan un binding tipado a gates efectivamente aprobados. Hoy esos bindings
cubren
`capability.route_reaches_user_visible_outcome` con
`capability.public_route_acceptance` y
`state.declared_workflow_sql_matches_implementation` con
`state.runtime_sql_trace`. Un receipt fallido, abstenido, stale, corrupto o sin
binding no puede avanzar readiness; una contradicción del store
abstiene el review.

Cuando todos los requisitos de decisión de una de esas preguntas quedan
satisfechos, la evaluación avanza como máximo a `human_review_required`. El gate
demuestra el contrato de tests exactos, no una verdad formal. `recommendations`
permanece vacío, `recommendation_status=abstained`, no nace una decisión ni un
package semántico y `mutation_authority=false`. Nombres como `repository`,
`commit`, `build`, `read` o `run`, mover el archivo o añadir un wrapper tampoco
pueden producir una recomendación. Los constructors y factories públicos fallan
cerrado ante `act_now`, una decisión autodeclarada o un package de cambio.

`python-maintenance-work-packages-v5` publica únicamente paquetes
`unused_characterization`, y sólo cuando los dos gates de precisión del
consenso de no-uso están aprobados. Sus pasos son exclusivamente de
caracterización, exigen confirmación humana y conservan
`mutation_authority=false`; no contienen `primary_change_target`. Coverage v2
puede demostrar ejecución por una suite passing y exponer líneas/ramas no
observadas. Sus términos `executing_tests` y
`work_package_target_executed_by_passing_suite` delimitan expresamente esa
evidencia y **no demuestran** que un test afirme un invariante.

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

Las líneas base históricas de actionability viven en
`tests/fixtures/code_review/rc6_top10_actionability_v1.json`. La ampliación
representativa está en `rc11_top40_actionability_v2.json`: reúne la unión de
los top 40 de ambos rankings, 41 símbolos etiquetados como builders,
validadores, reglas, algoritmos y orquestadores. El ranking v2 elevó la
`Precision@10` provisional de 0.60 a 0.70 y dejó iguales P@20, P@30 y P@40;
`build_parser` pasó del rango 2 al 39. Es revisión estática reproducible, no
ground truth humano, y el score sigue sin representar riesgo calibrado. v14
las conserva sólo como evidencia histórica y regresión; no autorizan una
decisión ni un package de cambio.

Los párrafos rc14–rc20 siguientes documentan resultados del contrato anterior;
no representan autoridad vigente de v14. La regresión temporal rc14 retira
`execute_knowledge_search`: el rango bruto 1,
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

Para cambios en la raíz canónica no se ensamblan manualmente Ruff, tipos,
pytest, review y empaquetado. La superficie pública es:

```bash
Neocortex code validate
```

El orquestador liga toda la evidencia a un `GitChangeSnapshot`, publica una
selección `trusted-deep`, consume el review vigente, ejecuta únicamente runners
registrados, instala el wheel candidato en un venv efímero y exige un segundo
run con replay exacto. Si el grafo no permite elegir pruebas, un proveedor no
queda listo, la medición es incompleta o el snapshot cambia durante la corrida,
se abstiene o falla: nunca traduce ausencia de evidencia en verde. Use
`--baseline HEAD^` para verificar un commit ya integrado localmente.

La observación empieza antes del primer provider: el padre captura memoria,
swap y PSI, reserva headroom para el escritorio y crea un único cgroup de
usuario para todo el proceso y sus descendientes. La evaluación conserva el
recibo de admisión `neocortex.code-validation-resources/v1`; systemd mide el
pico real y un watchdog cancela el grupo ante pérdida de reserva. Esta frontera
evita que Pyright, Coverage u otro hijo satisfaga sus métricas a costa de OOM
global. `PrivateNetwork=yes` impide además cualquier egress de providers en
esta ruta canónica.

La evidencia network-bound de `pip-audit` conserva su propia caducidad, pero
`code validate` no abre red. Puede enlazar un snapshot previo aún vigente
únicamente tras comprobar igualdad exacta del inventario instalado y ausencia
de cambios supply; de otro modo se abstiene. Este enlace queda en el receipt y
no altera ni republica el snapshot histórico.
El provider de inventario instalado se ejecuta de nuevo por diseño: el replay
compara su proyección semántica completa y sólo normaliza reloj/ID de captura,
en vez de exigir un `cache_replay` imposible o aceptar sólo el conteo de
paquetes.

El repositorio canónico es `$HOME/Neocortex/Repository`; su estado de
autoanálisis vive en
`${XDG_STATE_HOME:-$HOME/.local/state}/Neocortex/self-analysis`, nunca dentro
del repositorio. Cada smoke debe usar el ejecutable exacto del runtime que se
pretende promover. Un mini-root valida el contrato técnico, pero no sustituye
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
