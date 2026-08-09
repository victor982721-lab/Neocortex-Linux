# Cierre del programa de autoanálisis multianalizador

> **DOCUMENTO HISTÓRICO.** Fecha de aceptación: 2026-08-03. Este informe describe el candidato 0.7.2
> instalado desde wheel y el estado aislado del laboratorio. No describe una
> promoción del launcher estable ni una ejecución sobre corpus personal. El
> estado actual se consulta con `Neocortex --code-status` y
> `Neocortex --knowledge-status`.

## Veredicto

Los siete hitos del programa están implementados e integrados en una sola
experiencia pública mediante `Neocortex`. La plataforma ya puede producir,
publicar, reproducir, revisar, comparar y consultar evidencia de código por
proveedor, categoría, módulo, estado, delta y work package. No calcula un score
agregado ni una probabilidad de defecto y toda su autoridad permanece
`advisory`, con `mutation_authority=false`.

El resultado no significa que el código analizado esté limpio. Significa que
los hallazgos, abstenciones, discrepancias, costos y gates se conservan de forma
explicable y pueden usarse para elegir y verificar trabajo real sin repetir una
auditoría completa.

## Hitos publicados

| Hito | Entrega funcional | Publicación |
|---|---|---|
| 1 | Plataforma genérica, Code schema v3 compatible, Ruff/Mypy/Pyright, perfiles, consenso, replay y superficies públicas | PR #14, merge `7f79696` |
| 2 | Ruff Analyze, Grimp, Complexipy, contratos arquitectónicos, métricas y deltas por módulo | PR #15, merge `359de7c` |
| 3 | `trusted-deep`, Pytest/Coverage con ramas y contextos, test-to-symbol y gates | PR #16, merge `385ee37` |
| 4 | Consenso advisory de código potencialmente no usado, calibración y holdout | PR #17, merge `634c083` |
| 5 | Semgrep local, Deptry, pip-audit e inventario instalado/supply chain | PR #18, merge `8868eac` |
| 6 | Mutación focal con Cosmic Ray, historia Git y analítica explicable del grafo | PR #19, merge `e54cc17` |
| 7 | Consultas multidimensionales, fixtures/adversariales/property tests, CI por carriles y cierre instalado | PR #20 |

Los schemas públicos finales son:

- `neocortex.code-status/schema-v4`;
- `neocortex.code-review/v10`, compatible con v2-v9;
- `neocortex.code-publication-diff/v9`, compatible con v1-v8 y con
  relocalizaciones tipadas separadas de findings añadidos/resueltos;
- `neocortex.code-analysis-query/v1`;
- `neocortex.code-architecture-analysis/v2`;
- `neocortex.code-engineering-analytics/v1`;
- los schemas v1 propios de cada proveedor externo.

## Corte posterior: relocations tipadas y robustez de pip-audit

El primer work package posterior al programa elimina churn falso del diff sin
debilitar sus gates. `publication-diff/v9` empareja por multiplicidad findings
Mypy/Pyright que conservan evidencia semántica exacta y sólo cambian de rango;
publica IDs y posiciones antes/después como `relocated`. Los cambios reales de
mensaje o metadata siguen apareciendo como `added/resolved`.

La aceptación instalada final contra rc22 obtuvo:

- Mypy: 424 comunes, 19 relocalizados, 0 añadidos y 6 resueltos;
- Pyright: 725 comunes, 18 relocalizados, 0 añadidos y 8 resueltos;
- query real: 37 registros de relocation, sin truncación;
- replay: 592/592 hits, 0 bytes Code, 12 proveedores desde caché y 0 errores;
- los 13 proveedores de `trusted-static` en estado `ready`.

La misma aceptación reveló que pip-audit 2.10.1 puede repetir un advisory en su
JSON. El productor agrupa ahora por ID case-insensitive y une aliases y versiones
de fix con límites deterministas. No corrige paquetes ni adquiere autoridad de
mutación. El wheel aceptado mide 1 593 465 bytes, contiene 298 miembros y tiene
SHA-256
`95EDEBFD8014B1F080A6D787FDA21656668E6AB7BC944991BC68507F28A14597`.

## Experiencia pública final

El productor sigue siendo un solo comando sobre una raíz explícitamente
confiable y un estado aislado:

```powershell
Neocortex --self-analysis --analysis-profile trusted-deep `
  --root C:\Users\Victor\Neocortex\Repository `
  --state-directory C:\ruta\aislada\al\estado `
  --deep-test-selector tests/test_external_deep_coverage.py `
  --deep-mutation-target _04_Nucleo_Operativo/external_deep_coverage.py `
  --deep-mutation-symbol external_deep_coverage._normalize `
  --deep-mutation-max-mutants 20 `
  --deep-mutation-timeout-seconds 30 `
  --deep-mutation-time-budget-seconds 600
```

Los consumidores read-only son:

```powershell
Neocortex --state-directory $State --code-status --code-json
Neocortex --state-directory $State --code-review --code-json
Neocortex --state-directory $State --code-publication-diff $Baseline --code-json
Neocortex --state-directory $State --code-query review `
  --code-query-provider cosmic-ray-focal-mutation `
  --code-query-module _04_Nucleo_Operativo.external_deep_coverage `
  --code-query-work-package reduce_confirmed_hotspots_without_contract_regression `
  --code-json
```

Los filtros repetibles se combinan con AND entre dimensiones y OR dentro de una
dimensión. La enumeración está limitada a 1-500 registros, usa rutas de schema
explícitas y nunca recorre JSON arbitrario.

## Perfiles y herramientas

`protected` ejecuta sólo `ruff-protected-basic`. `trusted-static` publica 13
proveedores y `trusted-deep` agrega Coverage y mutación focal para un total de
15. `trusted-deep` nunca es predeterminado y sólo acepta la identidad física de
la raíz canónica.

| Herramienta | Versión aceptada | Proveedor o función | Declaración observada |
|---|---:|---|---|
| Ruff | 0.15.17 | basic, trusted y Analyze imports | MIT |
| Mypy | 2.1.0 | tipos de proyecto | MIT |
| Pyright | 1.1.411 | tipos de proyecto mediante Node 24.18.1 | MIT |
| Grimp | 3.15 | grafo y contratos | BSD-2-Clause |
| Complexipy | 6.2.0 | complejidad cognitiva | MIT |
| Vulture | 2.16 | candidatos estáticos de uso | MIT |
| Semgrep | 1.172.0 | invariantes locales, autofix deshabilitado | LGPL-2.1-or-later |
| Deptry | 0.25.1 | higiene de dependencias | MIT |
| pip-audit | 2.10.1 | snapshot de vulnerabilidades | classifier Apache Software License |
| Pytest / Coverage | 9.1.0 / 7.14.1 | pruebas, líneas, ramas y contextos | MIT / Apache-2.0 |
| Cosmic Ray | 8.4.6 | mutación focal sobre copia staged | MIT |
| Git | 2.55.0.windows.3 | historia local | ejecutable externo, no redistribuido |
| `importlib.metadata+RECORD` | NeoCortex 0.7.2 | integridad/licencias del entorno efectivo | proveedor interno |

La selección focal conservó Grimp en vez de Import Linter 2.13. Ambos fueron
viables, pero Grimp entrega directamente un grafo legible por máquina; envolver
el reporte de contratos sin JSON de Import Linter habría duplicado la misma
dimensión. Ruff Analyze quedó como oráculo diferencial independiente. No se
conservó un segundo motor de mutación: Cosmic Ray cubre el alcance focal y
acotado exigido. Vulture y Semgrep se integraron sin autoridad de modificación.

## Candidato instalado

- Wheel:
  `C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-hito7-integration-20260803-rc1\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.
- Tamaño: 1 589 930 bytes; 298 miembros; ZIP legible.
- SHA-256:
  `3B5687DE15B51EA5A2A22190AF999D11CFC720CED8BA617AD068D08D0B4B9087`.
- Python 3.13.14; `neocortex-framework 0.7.2`; Pyright 1.1.411.
- `pip check`: `No broken requirements found.`
- Los cuatro módulos nuevos o modificados de la consulta son byte-idénticos
  entre fuente, wheel e instalación y se importaron desde `site-packages`, no
  desde el checkout.
- `Neocortex doctor capabilities --json` confirmó `code=available`, incluidos
  Ruff, Mypy, Vulture, Grimp, Complexipy, Node y Pyright. Las capacidades
  multimodales ausentes pertenecen sólo al venv mínimo de esta aceptación.

El inventario efectivo registró 124 distribuciones: 121 con metadata de
licencia, 46 declaraciones ambiguas y 3 sin metadata. Las 14 dependencias base
aplicables estaban instaladas y compatibles; 301 entradas `RECORD` aprobaron
hash y tamaño, sin faltantes, rutas inseguras ni discrepancias.

## Autoanálisis real y replay

La corrida instalada 11 procesó la raíz canónica sobre una copia aislada y
quiescente del estado de Hito 6:

| Medición | Corrida 11 |
|---|---:|
| Duración total | 356.807 s |
| Archivos inventariados / candidatos Code | 604 / 591 |
| Procesados / cache hits | 64 / 527 |
| Símbolos / referencias | 18 444 / 98 681 |
| Bytes leídos por la ruta Code | 1 812 977 |
| Read / analyze / persist / graph | 19 / 1 359 / 1 102 / 10 550 ms |
| Proveedores / diagnósticos externos | 15 / 1 584 |
| Tiempo externo | 319 299 ms |
| Invocaciones de procesos de proveedores | 70 |
| Bytes de proveedor leídos / staged / verificados | 122 267 845 / 95 242 230 / 114 800 744 |
| Timeouts / errores | 0 / 0 |

La corrida 12 repitió exactamente la configuración en 25.076 s: 591/591 hits,
0 bytes Code, 0 ms de read/analyze/persist/graph, 14 publicaciones externas
reproducidas desde caché y 2 751 ms externos. El inventario instalado se
recalculó; los probes explícitos de replay sumaron tres procesos, 0 timeouts y
0 errores. No se procesó corpus ni se produjeron acciones de archivos.

## Resultado público

- Status v4: 33.029 s, JSON válido, stderr vacío.
- Review v10: 41.311 s, JSON válido, stderr vacío; 10 findings de hotspots,
  3 recomendaciones y 1 work package.
- Diff v8 contra Hito 6: 66.279 s, JSON válido, stderr vacío, estado `ready` y
  veredicto `incomparable` porque cambió la firma de procesamiento. No se
  inventó un resultado de mejora o regresión.
- Query status por Pyright: 3/256 registros en 33.166 s.
- Query review con proveedor, categoría, módulo, estado y work package: 1/591
  registros en 41.137 s.
- Query diff `deptry-project-dependencies + unchanged`: 1/32 registros en
  64.139 s, con deltas cero de findings, métricas y relaciones.

Las consultas publican `aggregate_score=null`, `defect_probability=null`,
`authority=advisory` y `mutation_authority=false`.

## Hallazgos, consenso y gates

### Tipos y estática

- Ruff protected: 0 findings; Ruff trusted: 157.
- Mypy: 449; Pyright: 751.
- Consenso tipado: 289 coincidencias, 100 sólo Mypy, 299 sólo Pyright, 0
  contradicciones y 0 incomparables.
- Los gates de “no added” para Ruff/Mypy/Pyright aprobaron. Los dos gates de
  cobertura tipada quedaron `not_evaluated` porque no existe todavía una
  métrica comparable publicada.

### Arquitectura e ingeniería

- 284 módulos, 1 116 imports, 4 SCC cíclicos conocidos, 6 contratos y 0
  discrepancias Ruff Analyze/Grimp. Consenso de grafo y contratos aprobaron.
- Historia Git: 10 540 métricas y 871 relaciones.
- Mutación focal: score 0.50, 20 mutantes seleccionados (5 killed, 5 survived,
  10 incompetent, 0 timeout), 15 findings, 13 métricas y 3 relaciones. Los
  gates de baseline, completitud y score registrado aprobaron.
- No se publica una probabilidad de defecto ni se agregan las dimensiones.

### Coverage y test-to-symbol

- Suite seleccionada: 8/8 pruebas aprobadas, medición completa.
- Proyecto observado: 1 669/57 965 líneas (2.8793 %) y 248/18 602 salidas de
  rama (1.3332 %). Es cobertura parcial declarada, no cobertura del proyecto.
- Símbolo objetivo `_normalize`: 152/169 líneas (89.94 %) y 48/66 ramas
  (72.73 %), con 7 pruebas protectoras, 17 líneas y 18 arcos pendientes.

### Código potencialmente no usado

- 255 candidatos: 47 `explained_usage`, 14 `dynamic_usage_possible`, 194
  `insufficient_evidence` y 0 `probable_unused_high_consensus`.
- Calibración: precision 1.0, recall 0.6667, abstención 0.25.
- Holdout: precision 1.0, recall 0.3333, abstención 0.50.
- Los cuatro gates aprobaron; no existe autoridad de borrado.

### Supply chain

- Semgrep: 0 findings; gate de invariantes aprobado.
- Deptry: 3 findings `DEP001`: `ctranslate2`, `radon` y un import entre módulos
  de tests no están reconocidos por la declaración analizada. El gate de
  integridad de dependencias falló y el resultado permanece visible.
- pip-audit: snapshot del 2026-08-03 con tres advisories para `mcp`
  (`PYSEC-2026-3481`, `3482`, `3483`). Freshness aprobó; el gate de ausencia de
  vulnerabilidades falló.
- Integridad `RECORD` y disponibilidad del inventario de licencias aprobaron,
  conservando 46 ambigüedades de metadata y 3 ausencias.

## Work package vigente

El único paquete público es
`code-review-work-package-v1:xxh3_128:1d6f368c9dfc94017753d0ca8ca8e56d`:

- módulo `_04_Nucleo_Operativo.external_deep_coverage`;
- símbolo `external_deep_coverage._normalize`;
- objetivo `reduce_confirmed_hotspots_without_contract_regression`;
- riesgo `medium`, sin requerir confirmación humana para caracterizar el cambio;
- 7 pruebas protectoras, 15 cadenas de import acotadas y gates explícitos de
  arquitectura, Coverage, mutación, tipos y supply chain.

Su orden es caracterizar, cambiar conservando contrato, validar fixtures y
consumidores, y publicar un único diff/replay. El paquete no autoriza cambios
por sí mismo.

## CI y barreras locales

`.github/workflows/ci.yml` define un solo workflow Windows/Python 3.13:

- `fast`: Ruff sobre Python modificado en pull request o push;
- `standard`: build, instalación desde wheel y suite integrada curada;
- `deep`: cron semanal o `workflow_dispatch`, sólo contratos y fixtures
  aislados; no finge ejecutar la identidad local de `trusted-deep`.

Las regresiones focales locales aprobaron 13 casos del motor, 4 de CLI y 108
casos integrados de query/CLI/parser/compatibilidad. La selección SQLite aprobó
58 casos y dejó 1 deselect explícito: el owner Code histórico sigue abriendo con
`journal_mode=delete`. Ruff y Mypy focales quedaron limpios. El baseline Ruff
completo conserva 157 findings y por eso no se presenta como gate verde global.

## Limitaciones y siguiente acción

- Sin USN, el inventario completo portable es correcto y cacheable, pero status
  no afirma freshness viva; `journal.status=unavailable` queda explícito.
- Materializar status/review/diff cuesta hoy 33-66 s; las consultas filtran una
  superficie completa y no son todavía una proyección de baja latencia.
- Coverage es deliberadamente parcial y la medición del proceso principal no
  incluye subprocesses.
- El diff Hito 6→Hito 7 no es comparable por cambio de firma; supply chain sí
  demuestra deltas observados iguales a cero.
- Permanecen tres findings Deptry, tres advisories de `mcp`, 157 Ruff trusted y
  la deuda SQLite `journal_mode=delete`.

La siguiente acción funcional recomendada es ejecutar el work package de
`external_deep_coverage._normalize` con sus siete pruebas y gates, y publicar un
solo replay/diff. Antes de aceptar ese cambio deben reconciliarse los gates de
dependencias/vulnerabilidades o declararse explícitamente no aplicables con
evidencia; no deben ocultarse. La latencia de 33-66 s ya es un bloqueo medido
para una futura proyección de consultas, pero no invalida el cierre funcional
de este programa.
