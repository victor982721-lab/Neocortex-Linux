# Neocortex — handoff operativo actual

> Actualizado: 2026-08-05, salida real 74 corregida, empaquetada y promovida.
> Este archivo conserva su nombre anterior sólo para mantener la ruta conocida.
> `Resultado actual` y `Próximos pasos` son la única guía vigente; los
> checkpoints restantes conservan evidencia histórica y no son planes activos.

## Resultado actual

La corrida real 74 terminó con exit `2` después de 58 minutos. Code no fue el
fallo: reutilizó 170/170 archivos, publicó 4 933 símbolos y 31 029 referencias
sin errores. El bloqueo provenía de `action_errors=13`: cuatro paquetes
históricos dentro de árboles `dist` no admiten lectura ni consulta de ACL, y la
búsqueda final de directorios vacíos entraba en subárboles que el inventario ya
había excluido, acumulando otros nueve errores sin ruta diagnóstica. Semantic no
llegó a arrancar porque el contrato integrado se abstiene ante errores de
acciones u organización.

La fuente ahora excluye por ruta exacta `build`, `dist` y `wheelhouse` sólo bajo
Neocortex, el framework EPS canónico y la referencia EPS histórica en OneDrive;
los mismos nombres siguen siendo elegibles en el resto del corpus. La fase de
acciones recibe y reutiliza la política compilada completa del inventario, por
lo que no vuelve a atravesar dependencias, temporales, rutas protegidas ni esos
artefactos reconstruibles.

Los 26 errores de perfil PDF tampoco eran 26 documentos nuevos: eran una cola
persistente con evidencia de página parcial y dos documentos de 5 033 páginas
cuya extracción seguía en `PdfDocumentTimeout`. El perfilado ahora difiere esos
documentos hasta completar extracción, ordena los candidatos por tamaño,
conserva lotes de página ya publicados, procesa sólo páginas faltantes,
reconstruye el perfil documental desde SQLite y persiste el tipo/mensaje acotado
de cualquier fallo en `document_warnings`/`profile-error`.

La validación aislada aprobó 97 pruebas integradas y 14/14 acciones focales
(se excluyó únicamente la regresión histórica de 258 duplicados, que consume
varios minutos), además de dos regresiones de ruta PDF, Ruff/format, compileall,
`git diff --check` y Mypy focal de siete módulos. Mypy transitivo conserva nueve
deudas anteriores fuera de este cambio. No se reprocesó el corpus ni se modificó
el estado durable.

La corrección quedó consolidada en el commit `5146674`. El wheel promovido está
en `C:\Users\Victor\Neocortex\Laboratory\neocortex-runtime-output-fix\candidate-5146674\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`;
tiene 1 605 370 bytes, SHA-256
`1917ab85f82912557648f50fd47983f9340a512c80d48ee79660f660f38d00ce` y
xxh3_128 `4704b5ae8a35c1d60473066aeeff316c`. El runtime aislado `full`
quedó con `pip check` limpio y 8/8 capacidades disponibles. Su aceptación
procesó 20/20 PDF sintéticos, publicó 20 páginas FTS y 20 perfiles, con
`action_errors=0`, `profile_errors=0` y exit `0`; el replay reutilizó 20/20
extracciones y clasificaciones, sin trabajo ni errores nuevos.

El runtime versionado instalado es
`0.7.2-wheel-xxh3_128-4704b5ae8a35c1d60473066aeeff316c`. El launcher estable
se promovió mediante la transición NTFS registrada y quedó en SHA-256
`44d4b33bc386266d290a92bf321b7e9cab359041e5b567fb17d731c4cf725f0b`;
`Neocortex --version` informa `0.7.2` y el doctor estable confirma 8/8
capacidades. El recibo es
`2ee597a8741613c7595049dfe5aa18fb3b4c2bbf829ee3736abfcf8295674709.result.json`
y el rollback externo conserva el launcher anterior por su SHA-256
`021a982f58ee5e0156d1cd3d8589541bff80c99d6a8e3d6edbb9b3378af56619`.
No se volvió a ejecutar `--all` ni se tocó el corpus o el estado durable vivo.

El inventario normal v3 excluye antes de leer contenido los árboles internos de
Neocortex (`Laboratory`, `Laboratories`, `TestTemp`, `Lab`, `Checkpoints`,
`Backups` y `external_backups`), metadatos VCS, entornos Python,
`site-packages`, `node_modules`, `__pycache__`, `.CDX`, temporales reconocibles
de `pytest`/`tmp`/`basetemp`/`inline-snapshot`, cachés de herramientas y archivos
`.pyc`/`.pyo`. `build` y `dist` permanecen elegibles para no ocultar contenido
propio por un nombre genérico.

El corte `593e584` redujo los hotspots publicados de clonación de miembros
Semantic y del preset protegido de autoanálisis sin cambiar sus contratos de
atomicidad, reanudación, orden de fases o cancelación. La barrera focal aprobó
29 pruebas Semantic y 125 pruebas CLI/self-analysis; Ruff y `git diff --check`
quedaron limpios. El replay aislado final quedó `ready`, retiró
`cli_validation.apply_self_analysis_preset` del paquete de trabajo y dejó como
siguiente recomendación `external_supply_chain_audit.execute_installed_package_inventory`.

El siguiente corte descompone `external_supply_chain_audit.execute_installed_package_inventory`
en preparación de contexto, construcción de métricas/relaciones de paquetes,
relaciones de dependencias y publicación de resultados, sin cambiar la firma ni
la evidencia emitida. La suite focal de supply-chain pasó 12 pruebas; Ruff y
Mypy local del módulo quedaron limpios. El autoanálisis aislado v6 con el runtime
completo procesó 607 archivos, 579 candidatos, cero errores de inventario y cero
errores Code; su review quedó `ready` y retiró el hotspot objetivo. El diff
v6 contra v4 quedó `ready`, con 0 hotspots añadidos, 0 cambiados, 1 retirado
(el objetivo) y 0 resoluciones de llamadas perdidas; el veredicto global es
`incomparable` porque v4 tenía proveedores externos ausentes y v6 los ejecutó.
La siguiente recomendación publicada es
`bounded_subprocess.run_bounded_capture`; las abstenciones de proveedores se
conservan como evidencia advisory, no como autorización de mutación.

El corte siguiente separa la orquestación de
`bounded_subprocess.run_bounded_capture` en validación, preparación de stdin,
lectores, espera, finalización y resolución de errores, conservando la
precedencia de timeout/overflow/excepción y la limpieza del proceso descendiente.
Las regresiones focales pasaron 19 pruebas; Ruff y Mypy local quedaron limpios.
El autoanálisis aislado v7 procesó 607 archivos, 579 candidatos, cero errores de
inventario y cero errores Code; su review quedó `ready` y retiró el hotspot.
El diff v7 contra v6 quedó `ready` y `equivalent_under_observed_metrics`, con
0 hotspots añadidos, 0 cambiados, 1 retirado y 0 llamadas corregidas o perdidas.
La siguiente recomendación publicada es
`semantic_image_index.index_image_embeddings`.

El corte siguiente separa `semantic_image_index.index_image_embeddings` en
preparación de modelos/generaciones, staging de registros, finalización de
staging y ejecución de generaciones, sin cambiar el orden de publicación,
cancelación, cursores ni los espacios vectoriales imagen/imagen-OCR. Las
regresiones de Semantic imagen/OCR y CLI pasaron 116 pruebas; Ruff y Mypy local
quedaron limpios. La aserción restante de `test_semantic_service_facade.py` es
una deuda previa de firmas de fachada (`progress`) y el módulo modificado no la
usa. El autoanálisis aislado v9 procesó 607 archivos, 579 candidatos, cero
errores de inventario y cero errores Code; su review quedó `ready` y retiró el
hotspot. El diff v9 contra v7 quedó `ready` y
`equivalent_under_observed_metrics`, con 0 hotspots añadidos, 0 cambiados, 1
retirado y 0 llamadas corregidas o perdidas. La siguiente recomendación
publicada es `external_evidence_store._publish_external_provider`.

El corte siguiente separa la publicación del proveedor externo en creación del
run/contrato, entradas y contadores, replay y proyecciones de findings,
métricas y relaciones, conservando la publicación bajo SAVEPOINT, el rollback
atómico y el orden de validaciones. La suite focal de publicación, arquitectura,
supply-chain y plataforma pasó 36 pruebas; Ruff y Mypy local quedaron limpios.
El autoanálisis aislado v10 procesó 607 archivos, 579 candidatos, cero errores
de inventario y cero errores Code; su review quedó `ready` y retiró el hotspot.
El diff v10 contra v9 quedó `ready` y `equivalent_under_observed_metrics`, con
0 hotspots añadidos, 0 cambiados, 1 retirado y 0 llamadas corregidas o
perdidas. La siguiente recomendación publicada es
`external_mutation_cosmic_ray.execute_cosmic_ray_mutation`.

Después se alineó el contrato de configuración Code con la allowlist personal
intencional: `ApplicationConfig` declara 132 campos y las proyecciones públicas
conservan `code_project_roots` como `explicit_project_roots`, mientras el modo
de autoanálisis sigue usando exclusivamente su raíz explícita. Las pruebas
focales pasaron 27 casos y el carril `standard` reproducido localmente pasó
385 pruebas con una sola exclusión prevista; Ruff y el formato quedaron limpios.
No se ejecutó el corpus ni se modificó estado durable. La verificación remota
queda pendiente de la PR que transporte este ajuste.

Una interrupción conserva el último lote inventariado y finaliza la generación
como `partial`. El arranque real siguiente recuperó automáticamente la corrida
27 cancelada y abrió la 28; ésta recorrió 115 200 archivos pero terminó
`partial` con 52 errores de acceso y no fue publicada. El diagnóstico posterior
aisló laboratorios y temporales internos omitidos por v2. Con la política v3, el
recorrido de metadatos instalado completó 119 198 entradas en 4.72 s con cero
errores, sin leer contenido ni escribir estado.

La selección pública de Code usa ahora `--code-scope projects` por defecto y
una allowlist exacta de dos raíces: `C:\Users\Victor\Neocortex\Repository` y
`C:\Users\Victor\Frameworks\Generador de bitácoras EPS`. Cuando existe esa
allowlist, los manifiestos de cualquier otro árbol no pueden ampliar el límite.
Antes de leer bytes se excluyen archivos externos, laboratorios, dependencias,
generados y cachés. `--code-project-root` reemplaza explícitamente la allowlist;
`--code-scope broad` conserva la selección histórica amplia sólo como override
deliberado; `--select-path` sigue admitiendo una ruta exacta y `--self-analysis`
conserva su raíz explícita.

El wheel promovido está en
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-code-allowlist-20260804-rc1\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.
Tiene 1 600 082 bytes, SHA-256
`b3435ce8090147af52a48a89f7be128d38ed7a7052f12079b2dbdc0f6a8a8afd` y
xxh3_128 `c6dc77fbd570195ed7c736d8f8ddaef4`. El runtime versionado es
`0.7.2-wheel-xxh3_128-c6dc77fbd570195ed7c736d8f8ddaef4`; `pip check` quedó
limpio, 23 pruebas focales aprobaron, Ruff quedó limpio y el doctor instalado
informa 8/8 capacidades. El launcher estable fue promovido mediante la
transición NTFS con rollback y quedó en SHA-256
`021a982f58ee5e0156d1cd3d8589541bff80c99d6a8e3d6edbb9b3378af56619`.
El recibo es
`c4f2e555a7eeeaccb4f0f7da056b20c641631ddb048d1a11595db67b215337a6.result.json`.

La aceptación instalada inventarió una muestra aislada de 20 archivos y
detectó 10 candidatos propios dentro de dos proyectos. Omitió 2 archivos fuera
de proyecto, 4 dependencias, 2 generados y 2 cachés, con 0 errores. El replay
`route-only` reutilizó los 10 resultados, procesó 0, leyó 0 bytes y conservó los
mismos conteos. La barrera afectada aprobó 232 pruebas y 2 subtests; Ruff y el
Mypy directo de producción quedaron limpios. El carril GitHub `standard`
aprobó además 384 pruebas instaladas; el ajuste mecánico exigido por el contrato
Ruff quedó incorporado en rc3.

No se recorrió ni modificó el corpus vivo. Los resultados Code amplios ya
existentes se preservan como historia; la próxima corrida Code completa marcará
fuera de la vista vigente lo que ya no pertenezca a proyectos, sin borrar sus
versiones ni evidencia. Semantic implícito continúa priorizando documentos y
audio; Code semántico sólo se selecciona de forma explícita.

`Neocortex --all` ejecuta las seis rutas y, si acciones y organización no
reportan errores, avanza Semantic textual sobre PDF, DOCX, XLSX, PPTX, ODT y
audio. El preset integrado usa límites duros y reanudables de 100 000 items,
1 000 000 de jobs y 172 800 segundos. Una truncación limpia conserva progreso y
exit `0`; errores o estado stale conservan exit `2`. La acción directa
`--semantic-index` mantiene su contrato estricto de exit `2` ante truncación.

Code queda fuera de Semantic implícito. La inspección read-only del estado vivo
encontró 5 133 824 chunks/jobs y cero embeddings publicados; Code aporta
4 890 200 chunks, aproximadamente 95 %, frente a 243 624 de documentos y audio.
El usuario aún puede seleccionarlo deliberadamente con
`--all --semantic-source code`. Así se conserva Code↔Semantic sin hacer que la
publicación documental cotidiana herede la generación histórica patológica.

El piloto instalado sobre 20 PDF sintéticos publicó 40/40 embeddings, cero
errores y una generación `ready`. El replay exacto tuvo 20/20 cache hits,
`new_jobs=0`, `embedded=0` y otro head `ready` por clon de los 40 miembros. Una
consulta textual completa devolvió cinco hits útiles con exit `0`. La aceptación
final rc2 repitió el replay en 6.815 s mediante el ejecutable instalado.

El wheel rc2 está en
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-all-semantic-20260803-rc2\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.
Tiene 1 594 454 bytes, 298 miembros, ZIP íntegro y SHA-256
`017983AF1C79CB71E9E29DDCFF18BACCF1C0B91ED1594A52D6C8D02FB3BDF043`.
Los cuatro módulos de producción modificados son byte-idénticos entre fuente y
wheel; `pip check` está limpio, `doctor capabilities --json` reporta las ocho
capacidades disponibles y el preset instalado confirmó `100000/1000000/172800`.

El corte publica `neocortex.code-publication-diff/v9`, compatible con v1-v8, y
clasifica como `relocated` un finding Mypy/Pyright cuyo path, categoría, código,
severidad, mensaje normalizado y metadata permanecen exactos pero cuya posición
cambió. Conserva multiplicidad, IDs antes/después y rangos completos; resta las
parejas relocalizadas de `added/resolved` y no falla el gate por desplazamiento.
Un cambio real de mensaje sigue produciendo un añadido y un resuelto y falla el
gate. `--code-query diff --code-query-delta relocated` expone cada ejemplo con
sus posiciones exactas.

El diff instalado final contra rc22 está `ready`, schema v9, digest
`170aa7010b318839f26bdcad549c940f` y 450 141 bytes. Mypy conserva 424 findings
exactos, clasifica 19 relocalizados, añade 0 y resuelve 6; Pyright conserva 725,
clasifica 18, añade 0 y resuelve 8. Ambos gates están `passed`. Ruff trusted,
Ruff protected, Semgrep, arquitectura y supply chain no presentan regresiones.
El veredicto agregado permanece `mixed` por 45 candidatos Vulture añadidos y 37
resueltos; no se presenta esa evidencia advisory como defecto ni como autoridad
de borrado. Coverage/Mutation no son comparables porque la publicación actual
es `trusted-static`. Calls conserva 0 corregidas y 0 perdidas; hotspots tiene 0
añadidos, 0 removidos y 3 evidencias cambiadas.

La consulta pública real devolvió 39/39 registros sin truncar: dos deltas de
proveedor y 37 `provider_finding_relocation` —19 Mypy y 18 Pyright—. Status
final registra la corrida 7 `completed`, 592/592 cache hits, 0 procesados, 0
errores y los 13 proveedores `ready`. Review v10 está `ready` y publica un único
paquete, `code-review-work-package-v1:xxh3_128:2588847e578d56daf97138187d173cdd`,
para `_04_Nucleo_Operativo.cli_validation` /
`cli_validation.apply_self_analysis_preset`.

El replay exacto final terminó en 14.235 s: 592/592 hits, 0 bytes Code, 0 ms de
read/analyze/persist/graph, 12 publicaciones externas desde caché, 3.019 s
externos y 0 errores. `installed-package-inventory` conserva su reverificación
local; ninguna herramienta de análisis de contenido volvió a ejecutarse. Una
corrida anterior reintentó únicamente Semgrep después de un timeout transitorio
y dejó el proveedor `ready` antes del replay final.

El autoanálisis real encontró y cerró dos fallos de la ruta canónica. pip-audit
2.10.1 puede repetir el mismo advisory en su JSON; el productor ahora agrupa por
ID case-insensitive y une aliases/fix versions con límites deterministas en vez
de abortar, y productor/adapter comparten una sola constante de limitaciones.
Además, cuatro errores tipados introducidos por las regresiones nuevas quedaron
corregidos; el diff final no añade findings Mypy ni Pyright.

El wheel candidato tiene 1 593 465 bytes, 298 miembros, ZIP y `pip check`
limpios, SHA-256
`95EDEBFD8014B1F080A6D787FDA21656668E6AB7BC944991BC68507F28A14597`.
Los seis módulos de producción decisivos son byte-idénticos entre fuente, wheel
e instalación aislada. Las correcciones posteriores sólo estrechan tipos en
tests no empaquetados. Las barreras afectadas aprobaron 78, 12, 40 y 14 pruebas;
Ruff/format y Mypy focal de producción están limpios.

La línea base comparable está en
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-normalize-work-package-20260803-rc1\pilot-state-rc1`,
pero fue producida por el Python de
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-hito7-integration-20260803-rc1\acceptance-venv`.
Esa procedencia es necesaria porque la firma de entorno distingue el ejecutable
físico; usar otro venv hace que el diff se abstenga correctamente. El baseline
original permaneció intacto salvo dos sidecars vacíos creados por un diagnóstico
read-only y retirados tras verificar WAL=0, SHM=32 KiB y ausencia de procesos.
No se tocó corpus ni estado durable vivo y no se promovió el launcher estable.

Los Hitos 1 a 7 están publicados mediante los PR #14 a #20. Como línea base del
corte, el candidato
`codex/neocortex-self-analysis-integration-v1` cerró la aceptación instalada y
publicación del **Hito 7**, y con ello el programa multianalizador completo.
`Neocortex --code-query` consume status, review y diff publicados y filtra por
proveedor, categoría, módulo, estado, delta y work package. Su schema es
`neocortex.code-analysis-query/v1`; conserva `aggregate_score=null`,
`defect_probability=null`, `authority=advisory` y
`mutation_authority=false`.

El wheel de la línea base Hito 7 está en
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-hito7-integration-20260803-rc1\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.
Tiene 1 589 930 bytes, 298 miembros, integridad ZIP y `pip check` limpios, y
SHA-256
`3B5687DE15B51EA5A2A22190AF999D11CFC720CED8BA617AD068D08D0B4B9087`.
Los cuatro módulos públicos comparados fueron byte-idénticos entre fuente,
wheel e instalación. La aceptación usó Python 3.13.14, Node 24.18.1 y Pyright
1.1.411; `doctor capabilities` confirmó la capacidad Code del venv candidato.
El launcher estable no fue promovido.

La corrida instalada 11 inventarió 604 archivos y evaluó 591 candidatos Code:
procesó 64, reutilizó 527, publicó 18 444 símbolos, 98 681 referencias y 1 584
diagnósticos de los quince proveedores, sin errores. Duró 356 807 ms, de los
cuales 319 299 ms pertenecen a la ruta externa. Los proveedores abrieron 70
procesos, leyeron 122 267 845 bytes, staged 95 242 230 y verificaron
114 800 744, con 0 timeouts y 0 errores.

La corrida 12 demostró replay exacto en 25 076 ms: 591/591 hits, 0 bytes Code,
0 ms de read/analyze/persist/graph, catorce publicaciones externas desde caché
y 2 751 ms externos. `installed-package-inventory` se recalculó; los probes
de replay sumaron tres procesos acotados. El status final registra 15
proveedores `ready` y cero errores.

Las superficies instaladas devolvieron JSON válido y stderr vacío: status v4
en 33 029 ms, review v10 en 41 311 ms y diff v8 contra Hito 6 en 66 279 ms.
Las tres consultas públicas demostraron 3/256 registros para Pyright, 1/591 para
la combinación mutación+módulo+estado+work package y 1/32 para
`deptry-project-dependencies + unchanged`. El diff está `ready` con veredicto
`incomparable` por cambio de firma; supply chain conserva deltas iguales a cero
y no se inventa una mejora o regresión.

Arquitectura quedó `ready`: 284 módulos, 1 116 imports, 4 SCC conocidos, 6
contratos y 0 discrepancias del grafo. Coverage aprobó 8/8 pruebas; la medición
parcial fue 1 669/57 965 líneas (2.8793 %) y 248/18 602 ramas (1.3332 %). El
símbolo objetivo obtuvo 152/169 líneas (89.94 %) y 48/66 ramas (72.73 %) con
siete pruebas protectoras. Mutación conserva score 0.50 y sus tres gates
aprobados. El consenso no usado publicó 255 candidatos: 47 explicados, 14 con
uso dinámico posible, 194 insuficientes y 0 de consenso alto.

Permanecen visibles 3 findings Deptry, 3 advisories actuales de `mcp`, 157
findings Ruff trusted y la deuda SQLite `journal_mode=delete`. Sus gates fallidos
o no evaluados no se ocultan. En esa línea base el paquete vigente era
`external_deep_coverage._normalize`; el candidato actual ya lo cerró y publicó
`cli_validation.apply_self_analysis_preset` como sucesor.

El workflow `Neocortex CI` separa carriles Windows/Python 3.13 `fast`,
`standard` y `deep`; standard construye e instala el wheel, y deep permanece
semanal/manual y limitado a contratos/fixtures. El informe factual único está
en `docs/SELF_ANALYSIS_PROGRAM_REPORT_2026-08-03.md`. No se procesó corpus,
no se tocó estado durable vivo y no se promovió el launcher estable.

## Checkpoints anteriores

El candidato de la rama `codex/neocortex-unused-consensus-v1` ya superó la
aceptación instalada del **Hito 4**. Añade
`vulture-unused-static` 2.16 a `trusted-static` y `trusted-deep` como octavo
proveedor estático; el perfil profundo queda con nueve proveedores. Vulture
produce findings heurísticos `unused_code` sobre copias verificadas, sin cargar
configuración del proyecto, ejecutar contenido, usar red, aplicar fixes ni tener
autoridad de mutación.

`neocortex.code-unused-analysis/v1` correlaciona Vulture y Pyright con el grafo
Code, imports, reexports, `__all__`, callbacks, registries, fixtures, entry
points, Protocols y Coverage disponible. Cada candidato se publica exactamente
como `explained_usage`, `dynamic_usage_possible`, `insufficient_evidence` o
`probable_unused_high_consensus`. Coverage observada puede explicar uso; su
ausencia nunca fortalece una hipótesis de no uso. Todos los candidatos conservan
`authority=advisory`, `mutation_authority=false` y cero autoridad de borrado.

El contrato incluye fixture de calibración y holdout separados, con precision,
recall, abstención, denominadores, firmas y gates. Review v8, publication diff
v6 y el planificador de work packages v4 consumen la misma evidencia. Hasta tres
paquetes `unused_characterization` sólo pueden aparecer si pasan los gates de
precisión de calibración y holdout; exigen caracterización dinámica, pruebas,
confirmación humana y replay comparable, nunca una eliminación automática.

El wheel candidato instalado tiene 1 493 459 bytes, 288 miembros, integridad ZIP
y `pip check` limpios, y SHA-256
`D3068DF16E1E06930E896B3246B96C57B3BCE73F06BF5D8F324819194FF7D8A6`.
La primera corrida instalada detectó que Pyright no estaba aprovisionado dentro
del runtime candidato; se corrigió la ruta canónica del propio runtime y
`code-doctor` confirmó los nueve proveedores disponibles, schema válido y cero
violaciones de foreign keys. La corrida corregida (`analysis_run_id=16`) dejó
los nueve proveedores `ready`, 1 393 findings, 54 027 métricas y 2 601
relaciones, con siete cache hits, tres procesos y 46.482 s agregados de
proveedores. No se promovió el launcher estable.

El replay exacto (`analysis_run_id=17`) inventarió 571 archivos y reutilizó
558/558 candidatos Code y 9/9 proveedores. No releyó bytes Code ni repitió
análisis, persistencia o grafo; conservó 17 113 símbolos, 92 072 referencias y
426 diagnósticos Code. Terminó en aproximadamente 18.43 s de pared; los
contadores de proveedores sumaron 723 ms, 70 175 430 bytes verificados, una
invocación acotada de preparación de Coverage y 20 776 bytes de stdout. No hubo
errores, timeouts ni proveedores indisponibles.

La publicación real conserva 0 findings Ruff protected, 157 Ruff trusted, 388
Mypy, 677 Pyright y 171 Vulture. El consenso tipado registra 242 coincidencias,
92 sólo Mypy, 293 sólo Pyright, cero contradicciones y cero incomparables. El
consenso de uso evaluó 216 candidatos: 48 `explained_usage`, 14
`dynamic_usage_possible`, 154 `insufficient_evidence` y cero
`probable_unused_high_consensus`. No confundió ausencia de Coverage con desuso.
Calibración obtuvo precision 1.0, recall 0.6667 y abstención 0.25; holdout obtuvo
precision 1.0, recall 0.3333 y abstención 0.50. Los cuatro gates de precision y
recall observado aprobaron; la autoridad permanece advisory y sin mutación.

La selección `trusted-deep` aprobó 34/34 pruebas. Coverage publicó 47 799
métricas y 457 relaciones: 5 705/53 178 líneas (10.7281 %) y 279/17 016 salidas
de rama (1.6396 %), con `tests_passed` y `coverage_available` aprobados. Review
v8 está `ready`, digest `febf319f1a25baa73004a9cbf3d66765`, y propone un
único paquete de mantenimiento para `external_deep_coverage._normalize`; no
creó paquete de borrado ni de código no usado.

Publication diff v6 contra una copia quiescente y migrada del baseline Hito 3
está `ready`, digest `c45530b9439944acc82dab80393ce363`, con veredicto
`incomparable`: el baseline no tenía Vulture ni Coverage comparable y cambiaron
versiones o firmas de los proveedores. Conserva cero calls corregidas o perdidas
y no inventa mejora o regresión. El launcher estable, el corpus personal y el
estado durable vivo permanecieron intactos.

El último checkpoint publicado cierra el **Hito 3 de la plataforma
multianalizador** en `codex/neocortex-trusted-deep-v1`. El perfil explícito
`trusted-deep` sólo
acepta la identidad física exacta de
`C:\Users\Victor\Neocortex\Repository`; nunca es predeterminado ni se admite
sobre mini-roots o raíces arbitrarias. Añade Pytest 9.1.0 y Coverage 7.14.1 a
los siete proveedores estáticos, con branch coverage, contextos dinámicos,
selección declarada, límites duros, shards reanudables y replay exacto.

La aceptación instalada final procesó 551 candidatos Code y dejó los ocho
proveedores `ready`, sin errores externos. La selección representativa fue
honesta y parcial: 22/22 pruebas aprobaron; Coverage enlazó 708 relaciones
test→símbolo, 5 310 símbolos y 272 módulos. Midió 3 429/51 802 líneas
(6.6194 %) y 322/16 532 salidas de rama (1.9477 %). Ambos gates públicos
—`tests_passed` y
`coverage_available`— quedaron `passed`; la publicación conserva explícitamente
que no es cobertura completa del proyecto y que no observa subprocesses.

La ejecución final (`analysis_run_id=13`) empleó 29 procesos externos y
214.436 s en los contadores de proveedores; la ruta externa completa informó
214.765 s y Pytest/Coverage ocupó 112.837 s. El replay exacto
(`analysis_run_id=14`) terminó en 20.381 s de pared: reutilizó 551/551 entradas
Code y 8/8 proveedores, no releyó bytes Code ni repitió análisis, persistencia
o grafo y redujo la ruta externa a 1.922 s. Los proveedores estáticos abrieron
cero procesos; trusted-deep conservó una invocación acotada de preparación para
verificar 10 194 651 bytes de soporte y 8 950 931 bytes de inputs antes de
reutilizar su publicación.

La corrida real cerró tres defectos operativos en vez de rodearlos: preserva
`HOME`/`USERPROFILE`, `PATH`/`PATHEXT` y los arcos negativos que Coverage usa
para salidas de función; acorta el scratch interno y fija
`core.longpaths=true` sólo mediante el entorno efímero del worker. El manifest
v2 con `deep_analysis` ahora se valida exactamente contra su argv y firma, sin
relajar el fallo cerrado. El status instalado sobre el estado existente pasó de
`invalid` a `valid` sin rerun y conserva `current=false` únicamente porque USN
está indisponible y no existe checkpoint de inventario; raíz y vínculo Framework
sí son actuales.

Review v7 está `ready` y el work package vigente es
`external_deep_coverage._normalize`. El propio consumidor reveló que la
evidencia Coverage empaquetada no se enlazaba al nombre corto del analizador;
la corrección resuelve sólo el sufijo cualificado único, conserva nombres
locales y se abstiene ante dos candidatos. La evidencia real del objetivo es
91.72 % de líneas y 77.27 % de ramas, enlaza ocho pruebas protectoras y aprueba
`work_package_target_protected`. Diff v5 contra Hito 2 es factual pero no
comparable: Hito 2 no tenía Coverage y las firmas de proveedores cambiaron con
los nuevos inputs; no se inventa un veredicto de mejora o regresión.

La publicación final conserva 0 findings Ruff protected, 157 Ruff trusted,
386 Mypy y 643 Pyright. El consenso tipado registra 242 coincidencias, 90 sólo
Mypy, 259 sólo Pyright, cero contradicciones y cero incomparables. Ruff Analyze
y Grimp publican 1 058 relaciones cada uno; Complexipy publica 5 014 métricas y
Grimp 1 103. Los gates de providers, imports y contratos están `passed`; los
gates de cobertura global permanecen honestamente no comparables frente a Hito
2.

El wheel final instalado tiene 1 466 105 bytes, 285 miembros, SHA-256
`074095F0F9A39DD9F49246D5C4D6C957034156E77FD03FDC35CE2CA29380E092`,
integridad ZIP y `pip check` limpios. `doctor capabilities` informa todas las
capacidades disponibles y `code-doctor` confirma schema `ok`, cero violaciones
de foreign keys y los ocho proveedores disponibles. El launcher estable no se
promovió, no se procesó corpus personal y no se modificó estado durable vivo.

El **Hito 2 de la plataforma multianalizador** quedó publicado desde
`codex/neocortex-architecture-analysis-v1`. Code schema v4 conserva las
migraciones v1→v2→v3→v4 y añade métricas y relaciones genéricas con productor y
consumidores reales. `trusted-static` ejecuta siete proveedores separados:
Ruff protected/trusted, Mypy, Pyright, Ruff Analyze, Grimp y Complexipy. Status,
review v6, publication diff v4 y work packages consumen la evidencia
arquitectónica por módulo sin ejecutar contenido ni otorgar autoridad de
mutación.

La selección focal retuvo Grimp `3.15` directamente y Complexipy `6.2.0`.
Import Linter `2.13` fue viable, pero se rechazó porque duplicaba el grafo de
Grimp sin ofrecer un contrato JSON equivalente. Ruff Analyze queda como oráculo
diferencial: en la publicación final coincidió con Grimp en 1 040/1 040 imports,
cero discrepancias y cuatro SCC conocidos. El dominio contiene 269 módulos; los
seis contratos v1 fueron evaluados sin violaciones. Complexipy publicó 4 842
métricas: 4 304 símbolos y agregados total/máximo para los 269 módulos.

El propio autoanálisis detectó antes de publicar que la primera proyección
contaba identidades de símbolos como módulos: mostraba 632 frente a los 269 de
Grimp y fallaba `import_graph_consensus`. La corrección conserva la metadata
`module` emitida por el proveedor y el gate final queda `passed`, con 269 = 269.
La regresión focal y los cuatro consumidores públicos quedaron verdes antes de
repetir únicamente la evidencia afectada.

La prueba posterior sobre el commit exacto del handoff encontró una segunda
regresión real: cambiar sólo un archivo no Python reconstruía el grafo y borraba
correctamente sus diagnósticos derivados, pero el replay de proveedores
registraba la caché sin rematerializar esas proyecciones. La evidencia
normalizada seguía íntegra; status se abstenía para Ruff trusted, Mypy y Pyright
porque sus diagnósticos derivados faltaban. El replay ahora valida primero el
source normalizado, digest, contadores y versiones vigentes, reconstruye sólo
la proyección diagnóstica y verifica el resultado antes de publicar el registro
de replay. Las regresiones cubren finding presente, borrado de proyección, dos
replays consecutivos y proveedor sin findings que elimina una proyección
obsoleta, sin duplicar findings, métricas o relaciones.

La aceptación final usó el wheel `0.7.2` instalado en un runtime aislado bajo
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-hito2-architecture-20260803-rc2`.
El checkout final produjo 534 archivos inventariados y 528 candidatos Code; la
corrida incremental final (`analysis_run_id=7`) procesó los dos archivos
corregidos, reutilizó 526 y volvió a ejecutar los siete proveedores sin errores.
El replay exacto (`analysis_run_id=8`) empleó 6.207 s de la ruta Code, reutilizó
528/528 entradas Code y 7/7 proveedores, abrió cero procesos externos y
revalidó honestamente 49 685 085 bytes de inputs externos; Code no releyó
bytes ni repitió análisis, persistencia o grafo.

La publicación final conserva 0 findings Ruff protected, 157 Ruff trusted, 367
Mypy y 761 Pyright. El consenso tipado registra 236 coincidencias, 94 sólo Mypy,
391 sólo Pyright, cero contradicciones y cero incomparables. La ejecución full
de proveedores del commit final empleó 25 procesos y 102.820 s agregados: diez
lotes por cada Ruff de archivos, y una invocación para Mypy, Pyright, Ruff
Analyze, Grimp y Complexipy. El replay conserva sus costos reales de
verificación y registra `process_invocations=0` en los siete proveedores.

El diff contra el commit publicado rc22/Hito 1 usa el mismo wheel, schema v4 y
la misma ruta física. Su veredicto es `equivalent_under_observed_metrics`.
Arquitectura queda comparable y aprueba `no_new_import_cycles`,
`architecture_contracts_not_degraded` y `module_complexity_not_displaced`: cero
ciclos o contratos fallidos añadidos y cero complejidad desplazada. Ruff
protected y los tres proveedores arquitectónicos también son comparables. Ruff
trusted, Mypy y Pyright quedan honestamente `not_evaluated` en el diff porque el
`pyproject.toml` cambió al declarar las dependencias del Hito 2; sus resultados
actuales sí están completos y el consenso actual permanece disponible en
status/review.

Review sigue `ready` sobre 487/487 Python completos, con 205 hotspots y un único
work package vigente para `bounded_subprocess.run_bounded_capture`. El paquete
incluye 20 cadenas de imports, ningún contrato arquitectónico afectado y gates
explícitos de tipos, proveedores, ciclos, contratos y complejidad desplazada.
El wheel final contiene 282 miembros, `ZipFile.testzip()` no encontró corrupción,
`pip check` está limpio, el módulo instalado es byte-idéntico a la fuente y
`doctor capabilities` informa las ocho capacidades disponibles. `code-doctor`
abre el estado aislado con schema `ok`, cero violaciones de foreign keys y los
siete proveedores disponibles; Pyright `1.1.411` se resuelve desde el runtime
propiedad de NeoCortex.

El **Hito 1** quedó publicado mediante el PR #14 y fusionado en `main` como
`7f79696daa17903b7e488f5204616faff540cb31`. Su plataforma genérica, schema v3,
perfiles protected/trusted-static, cuatro proveedores iniciales y replay exacto
son la base compatible del Hito 2.

El candidato instalado `rc5` cierra los cinco pasos recomendados sobre estados
aislados: abstención Semantic/Knowledge por contrato exacto; clon Semantic
durable, reanudable y sujeto a deadline; lectores Code/Framework que preservan
quiescencia; decisión de imagen v10 validada con OCR; y watcher portable que usa
USN sólo como acelerador. Conserva además inventario Dedup v9, enlace exacto
Code↔Semantic y analizador Python v3/resolver v4.

El autoanálisis portable quedó fusionado mediante el PR #3 en
`2c5a046177ce3a1f2fdb5e882d847a8fbf136405`. El corte focal `rc12` quedó
fusionado mediante el PR #4 en
`147a500cc659b859e9eb997d261f9912b97ef847`: añade
`Neocortex --code-publication-diff`, amplía la calibración del ranking a 41
símbolos y cambia a `python-confirmed-hotspots-v2`. La `Precision@10`
provisional sube de 0.60 a 0.70 sin degradar P@20/30/40 y `build_parser` baja
del rango 2 al 39. Una muestra portable de 40 `probable_dead_symbol` encontró
36 usos demostrables, un contrato externo y tres candidatos de revisión; la
señal falla el gate de 0.90 y permanece suprimida.

El corte rc14 cierra el primer hotspot accionable producido por ese
ranking. `knowledge_search.execute_knowledge_search` pasa de 416 líneas y
complejidad 75 a un orquestador de 26 líneas; las fases conservan seams, orden,
cancelación, telemetría y completitud. El diff rc12→rc14 retira exactamente ese
hotspot, no añade ninguno y conserva cero resoluciones nuevas, corregidas o
perdidas sobre las calls comunes.

El corte focal rc16 añade el Actionability Gate al consumidor read-only. El
contrato `neocortex.code-review/v2` conserva el ranking bruto, pero antepone
hasta tres recomendaciones `act_now`, separa callers de producción/pruebas,
clasifica la construcción y expone riesgo, contratos y validación sugerida. En
la publicación real rc16 el validator del rango bruto 1 queda en
`characterize_first`; `_queue_job_rows_bounded`, rango bruto 2, es la primera
recomendación. Un rc15 intermedio fue rechazado porque el propio gate creó un
hotspot; la partición final lo retiró antes de aceptar el candidato.

El corte rc17 cierra la primera recomendación emitida por ese gate.
`semantic_generation_repository._queue_job_rows_bounded` pasa de 302 líneas y
complejidad 44 a un orquestador transaccional de 47/3. Las fases extraídas no
hacen commit y conservan orden, límite de jobs nuevos, reutilización lazy de la
base, rebind de metadata y reanudación. El diff rc16→rc17 retira sólo ese
hotspot, añade cero y conserva cero resoluciones corregidas o perdidas.

El corte rc18 cierra la recomendación siguiente.
`knowledge_context._derive_context_graph` pasa de 279 líneas/complejidad 43 a
un coordinador de nueve líneas. Validación, acumulación y materialización quedan
separadas sin cambiar orden, IDs estables ni rechazo atómico de evidencia
inconsistente. El diff rc17→rc18 retira sólo ese hotspot, añade cero y conserva
cero resoluciones nuevas, corregidas o perdidas.

El corte rc19 cierra `knowledge_exact._lookup_catalog`. El wrapper pasa de 225
líneas/complejidad 44 a 58/5; preflight generacional, decodificación, cobertura,
razones y warnings quedan en fases acotadas sin cambiar firma, ranking, límites,
provenance ni lectura estricta. Un primer candidato se rechazó porque la nueva
regresión apareció como hotspot; la partición final retira sólo el objetivo,
añade cero y conserva cero resoluciones nuevas, corregidas o perdidas. La primera
recomendación `act_now` pasa a `document_taxonomy.classify_document`.

El corte rc20 convierte esa recomendación aislada en un paquete de mantenimiento
explicable. `neocortex.code-review/v3` conserva todos los campos v2 y añade un
único paquete construido desde el primer `act_now`, con objetivo primario,
guardas contractuales y relaciones de llamadas estáticas confirmadas a uno o dos
saltos dentro de un pool fijo de 50 candidatos. El paquete rc19 agrupó
`document_taxonomy.classify_document` con `_normative_document_evidence` y
`_plausible_authority_identifier`; sólo el primero quedó autorizado como objetivo
de cambio. Treinta casos byte-estables y dos seams ambiguos fijaron exactamente
tipo, evidencia, confianza, incertidumbre y abstención antes del refactor. El diff
rc19→rc20 retira los hotspots de `classify_document` y
`_normative_document_evidence`, añade cero, cambia cero evidencia y conserva cero
resoluciones corregidas o perdidas. La siguiente raíz pasa a
`knowledge_exact.lookup_exact`. La revisión independiente final detectó y cerró
tres bordes antes de publicar: el pool ya no depende de `--code-review-limit`,
las calls repetidas se colapsan antes del límite de pares y el rol de un bridge
se clasifica relativo a su project root.

El corte rc21 cierra la raíz siguiente emitida por ese paquete.
`knowledge_exact.lookup_exact` pasa de 191 líneas/complejidad 35 a un
orquestador de 27/2. La selección de scopes, reportes unsupported, despacho por
owner, presupuesto compartido y materialización quedaron en fases privadas; la
mayor ocupa 49 líneas/complejidad 12. Una caracterización byte-estable sobre los
tres owners reales fijó API, orden de términos, ejecución
`inventory → code → catalog` aun con scopes y snapshot desordenados, reportes,
timings, provenance y bytes de las tres bases primarias. La regresión restringe
cualquier efecto de filesystem al WAL/SHM que SQLite administra al leer un owner
ya configurado en WAL; no lo presenta como una instantánea quiescente sin
sidecars. También quedó explícito que el presupuesto es global y que la
cancelación conserva la misma excepción. El diff rc20→rc21
retira sólo `lookup_exact`, añade y cambia cero hotspots y conserva cero
resoluciones nuevas, corregidas o perdidas. La siguiente raíz pasa a
`semantic_image_index.index_image_embeddings`, con
`semantic_generation_repository._clone_published_members` únicamente como
guarda contractual alcanzable a dos saltos.

El corte rc22 integra la primera evidencia externa prudente al autoanálisis:
Ruff `E4,E7,E9,F`, y sólo Ruff. El proveedor analiza copias verificadas en un
staging separado de la raíz, con configuración aislada, límites de archivos,
bytes, tiempo, memoria, salida y diagnósticos; no carga configuración del
repositorio ni aplica fixes. La publicación conserva herramienta, versión,
firma, cobertura, digests, registros canónicos y decisión comparable en las
tablas Code existentes. Status expone el subestado externo de forma cerrada ante
evidencia ausente, parcial, alterada o incompatible; review y publication-diff
conservan su estado interno `ready`, pero dejan únicamente el subestado/gate
externo en `not_evaluated`. El AST interno útil conserva su propio estado. El
replay exacto no vuelve a ejecutar Ruff.
Mypy y Vulture permanecen deliberadamente fuera de este corte hasta tener un
contrato y una calibración separados.

El launcher candidato rc22 final autoanalizó el repositorio completo después de
cerrar las regresiones adversariales. La publicación procesó 524 candidatos, de
los cuales 473 eran Python elegibles para Ruff; publicó 15 245 símbolos, 82 985
referencias, 203 diagnósticos internos y cero errores. Ruff cubrió 473/473 con
cero hallazgos bajo el perfil fijo. El replay reutilizó 524/524 entradas Code y
la evidencia Ruff exacta. El diff inicial→replay añadió/resolvió cero
diagnósticos externos, cambió cero hotspots y perdió/corrigió cero resoluciones.
La revisión v4 queda `ready` y su siguiente paquete real pasa a
`bounded_subprocess.run_bounded_capture`; `semantic_image_index` queda segundo.

La mejora es visible mediante el launcher del wheel. Una consulta positiva
conservó 10 resultados útiles; una consulta fuera de dominio descartó sus 30
candidatos y terminó con `abstained=1`, cero hits. El watcher rc5 ejecutó tres
ciclos portables sobre 20 archivos, todos exitosos, y después de `Ctrl+C`
`--code-status` devolvió `0` con cero sidecars SQLite.

No se promovió el launcher estable, no se tocó el estado durable ni el corpus
personal y no se movió, renombró ni borró ningún original. El único recorrido
completo fue el código del propio repositorio, sobre estado aislado.

## Verdad del entorno

- Fuente: `C:\Users\Victor\Neocortex\Repository`.
- Toda esta continuación se ejecutó con PowerShell 7.6.4 (`pwsh`).
- Base publicada al iniciar el Hito 2: `main` en
  `7f79696daa17903b7e488f5204616faff540cb31`, idéntico a `origin/main`.
- El checkout fuente es `0.7.2`. El Hito 2 se desarrolla en
  `codex/neocortex-architecture-analysis-v1`; el identificador final del PR queda
  en la historia de Git/GitHub después de publicarlo. La
  igualdad final entre `main` y `origin/main` se verifica después del merge
  porque el commit no puede autorreferenciar su propio hash desde este handoff.
- Launcher estable exacto:
  `C:\Users\Victor\AppData\Local\Programs\Neocortex\bin\Neocortex.exe`.
- El estable sigue en `Neocortex 0.7.1`, SHA-256
  `1D4FC0C654ACF0B34D300ABEC99839C5D263B44F05AA499947F44B12215716B1`.
- El comando de producto sigue siendo `Neocortex`. La aceptación del Hito 2 usa
  deliberadamente el launcher del runtime candidato aislado para no promover el
  estable; el launcher añade al proceso únicamente el shim Pyright propiedad de
  ese runtime.
- Node `24.18.1` está disponible en PATH y Pyright `1.1.411` permanece instalado
  bajo `sys.prefix\tools\pyright`, no como dependencia global del proyecto.
- `.codex/config.toml` conserva cambios locales de Victor y no forma parte del
  Hito 2.
- La normalización ACL/NTFS que permanece sólo en el checkout local
  (`tools/release_windows_ntfs_native.py` y su regresión) no forma parte de este
  corte Semantic/Knowledge. No publicarla, aplicarla ni integrarla sin cerrar su
  autorización y sus barreras de release por separado.
- También se preservó sin editar el cambio preexistente de Victor en
  `.codex/config.toml`.

## Estado vivo preservado

- No se ejecutó productor, migración, checkpoint ni compactación sobre
  `%LOCALAPPDATA%\Neocortex\state`.
- Sólo se hicieron status/search/verify read-only acotados.
- El autoanálisis del repositorio escribió exclusivamente en un estado aislado
  bajo `C:\Users\Victor\Neocortex\Laboratory`; no reutilizó estado vivo.
- Semantic live conserva aproximadamente 5.13 millones de jobs pendientes y
  cero embeddings publicados. No reanudar esa generación.
- No se modificó, movió, renombró ni borró ningún archivo del corpus.

## Slice A — Knowledge útil

Se corrigió la abstención global excesiva sin migrar el estado vivo:

- `knowledge status` mantiene la vista global y devuelve `6`/`7` ante cualquier
  owner incompatible/corrupto;
- `search` y `context` sólo se abstienen por los owners realmente presentes en
  `blocking_owners`;
- framework schema 19 se admite únicamente en lectura si satisface exactamente
  el contrato estructural 20 y se marca
  `legacy_schema_read_compatible:19->20`;
- inventory schema 7 continúa visible como incompatible, pero no bloquea una
  consulta que no necesita su ranking;
- DOCX FTS materializa primero su ranking acotado.

Evidencia live read-only:

- `protección diferencial de transformador`: `23.214 s` antes, `4.883 s`
  después; DOCX bajó de `19.347 s` a `1.134 s`, con las mismas filas, orden,
  snippets y scores;
- `IEC 61850 protección diferencial`: `4.530 s`;
- contexto de mantenimiento: `3.982 s`;
- el candidato final devolvió evidencia DOCX/PDF/XLSX útil en `6.033 s`; exit
  `4` fue parcial explícito por rankings ausentes, no un fallo.

## Slice B — Semantic publicado y acotado

La CLI y los servicios comparten un presupuesto: 50 items, 1 500 jobs nuevos o
reactivados y 900 segundos. Un límite conserva el head anterior, deja la
generación sin publicar y devuelve `2`. Sólo una enumeración `bounded-v1`
completa puede publicar.

El guard `exact-token-guard-v2` usa el tokenizador real antes de persistir,
revalida en el backend, falla si falta el contador y firma límite/revisión del
tokenizador. El replay exacto compara fingerprint y revisiones inmutables: no
crea jobs ni consume límites de items/jobs. Ahora tampoco crea otra generación,
clona membresía ni mueve el head publicado. Al cambiar el perfil, el head elimina
el perfil anterior sólo en las fuentes seleccionadas. La CLI también escapa
caracteres del corpus no codificables por la consola Windows, incluido JSON de
Knowledge.

Cada item textual publica al final un `semantic_metadata_title` derivado sólo
del basename, sin directorios ni extensión final, bajo
`semantic-basename-title-v1`. Cuerpo y título comparten una sola vectorización
de consulta y se fusionan por RRF con pesos `1.0`/`0.5`; la salida conserva la
procedencia y prefiere el snippet corporal. El título es mutable y advisory:
clasificación, evidencia y Knowledge `evidence` consumen sólo cuerpo. Knowledge
`discovery` usa un plan v3 con `semantic_title` opcional; el título sólo refuerza
la mejor evidencia del mismo recurso y revisión, nunca crea un hit ni aparece
como `EvidenceRef`. Un head legado sin títulos informa
`title_channel_not_indexed` sin bloquear la evidencia corporal.

La generación inicia el clon de la base de forma lazy y fija el head fuente,
conteo y high-watermark. `cursor_json.base_clone` conserva cursor, páginas y
conteo durable; cada página hace checkpoint, respeta el deadline común y puede
reanudar sin repetir lo confirmado. Antes de publicar verifica conteo y
high-watermark contra el snapshot fijado. Un replay exacto elide el candidato
completo; una revisión sólo de metadata reatacha el payload ya publicado sin
inferencia; una revisión de contenido obliga trabajo nuevo. Si otro builder
publica primero, el perdedor CAS queda terminal `failed` con diagnóstico
reintentable y la siguiente corrida parte del head vigente.

### Piloto fallido preservado

`C:\Users\Victor\Neocortex\Laboratory\semantic-pilot-20260801-1200`:

- 35 documentos, 972 páginas, 945 881 caracteres;
- 1 260 chunks/jobs, `606.756 s`, exit `2`, sin head;
- 70 `TextTokenLimitExceededError`, máximo 650 frente al límite 512.

El fallo se detuvo, se corrigió y nunca escaló a live.

### Piloto final

`C:\Users\Victor\Neocortex\Laboratory\semantic-pilot-20260801-token-guard-v2`:

- `pdf.sqlite3` SHA-256
  `04DC27BDF700F887D865E0824F497EC79B0F4889964FFE79A8F89061292AB816`;
- 35 documentos, 972 páginas, 1 272 chunks; p95 482 tokens, máximo 511,
  cero mayores de 512 e identidad idéntica en dos pasadas;
- primera publicación: `856.604 s`, 3 reusos, 1 269 inferencias, cero errores;
- generación 8 añadió 35 títulos en `9.349 s`, con 35 jobs y sin cambiar
  payload, revisión de item o revisión de chunk de ninguno de los 1 272 cuerpos;
- generación 9 repitió la fuente en `6.177 s`, con cero jobs;
- el candidato final produjo generación 11 en `7.336 s`, `ready`, 1 307
  miembros, cero jobs e integridad SQLite `ok`;
- replays de fuente: `7.995 s` y `7.618 s`, ambos
  `new_jobs=queued=reused=embedded=0` bajo límites `1/1`;
- replay con la implementación actual sobre copia aislada: `7.380 s`, devuelve
  directamente head 11, `new_jobs=queued=reused=embedded=0` y conserva exactamente
  11 generaciones, 17 948 miembros y 3 851 jobs. Sólo renueva
  `refresh_token`/`updated_ns` de los 35 items y 1 307 chunks observados;
- `semantic.sqlite3` final SHA-256
  `75EC03B4DD5237D7F3526B5E231415E13E1F127254BC3869E4374A9E806E2FA6`;
- el PDF fuente conservó exactamente su SHA-256 y cuerpos de generación 7 a 11
  tuvieron cero miembros ausentes o distintos.

La primera publicación observó ~1.48 chunks/s. Proyectar mecánicamente 5.13
millones de jobs daría unos 40 días continuos; no es autorización para live.

### Reevaluación canónica del ranking

Esta medición inicial de cinco consultas PDF se conserva como línea base; la
evaluación posterior de 12 consultas PDF/Code aparece en `Knowledge cross-owner`
y no borra las regresiones locales observadas aquí.

La corrida mediante el servicio real y el candidato instalado no reprodujo los
valores anteriores de `10/11`, `2/2` y MRR `0.750`; esos valores quedan
retirados. Semantic cuerpo+título fue completo en las cinco consultas, escaneó
1 272 cuerpos + 35 títulos y obtuvo `9/11`, `1/2` paráfrasis y MRR `0.700`, con
mediana `3.898 s` y máxima `4.073 s`.

Knowledge se comparó contra el candidato anterior, que no consumía título:

| Métrica | Knowledge cuerpo | `discovery` + título | Resultado |
|---|---:|---:|---|
| Hit@5, tres anclas | 3/3 | 3/3 | conserva |
| Hit@5, dos paráfrasis | 1/2 | 1/2 | no cierra |
| Hit@10 total | 4/5 | 5/5 | mejora |
| FamilyRecall@10 micro | 7/11 (63.6 %) | 8/11 (72.7 %) | mejora, bajo gate |
| FamilyRecall@10 macro | 60.0 % | 80.0 % | mejora, bajo gate |
| MRR@5 medio | 0.567 | 0.550 | regresión leve |
| Latencia `discovery` | — | mediana `4.598 s`; máxima `7.695 s` | bajo 15 s |

Cada consulta escaneó 1 307 vectores. Hubo señales `semantic_title` en hits,
pero cero `semantic_metadata_title` como evidencia. El resultado fue parcial
porque la copia sólo contiene owners PDF/Semantic; la cobertura ausente se
reportó con exit `4`. Se ensayaron top-5, top-10 y decaimientos RRF generales;
ninguno cerró simultáneamente recall y MRR, y todos se revirtieron.

## Artefactos no promovidos

### Candidato Hito 1 — plataforma multianalizador

- Raíz aislada:
  `C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-platform-v2-final`.
- Wheel final:
  `dist\neocortex_framework-0.7.2-py3-none-any.whl`; instalación nueva en
  `venv`, `Neocortex 0.7.2`, `pip check` limpio.
- Runtime del corte: Ruff 0.15.17, Mypy 2.1.0 y Pyright npm 1.1.411 mediante
  Node 24.18.1. `--code-doctor --code-json` informa disponibles los cuatro
  proveedores desde el runtime candidato.
- Estado aceptado: `current-state`; corrida completa `analysis_run_id=6` y
  replay exacto `analysis_run_id=7`.
- Full por proveedor, todos con 477/477 y 8 354 960 bytes verificados:
  Mypy 356 findings, un proceso, 24 606 ms; Pyright 781, un proceso,
  33 391 ms; Ruff protected cero, diez procesos acotados, 7 464 ms; Ruff
  trusted 157, diez procesos acotados, 7 451 ms. Los cuatro tuvieron cero
  timeouts y cero errores.
- Replay: cuatro cache hits, cero misses, cero procesos, cero bytes staged y
  digests de resultados idénticos; cada proveedor volvió a verificar los
  fingerprints de los mismos 8 354 960 bytes. La ruta completa bajó de
  84.399 s a 4.879 s.
- Gates: no added Ruff basic/project, Mypy y Pyright `passed`.
  `public_type_surface_not_degraded` y `type_coverage_not_degraded` permanecen
  `not_evaluated` porque aún no existe una métrica comparable publicada para
  esas dimensiones.
- Evidencia guardada: `code-status-accepted.json`,
  `code-review-accepted.json`, `code-publication-diff-rc22-final.json`, logs de
  full/replay y manifests SHA-256 antes/después bajo la raíz candidata.
- El launcher estable 0.7.1, el estado durable y el corpus personal no se
  modificaron.

### Candidato focal `rc22` — evidencia externa Ruff

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc22-final-external-ruff-evidence\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `695FF83E01FBA4923D5FB200807EF1CC27749EABE9166A58B323E24013C07B2F`;
- 1 356 996 bytes, 275 miembros, `ZipFile.testzip()` limpio,
  `Neocortex 0.7.2`, perfil full nuevo con 54 distribuciones y `pip check`
  limpio;
- Ruff 0.15.17 se resuelve desde el mismo runtime instalado, no desde PATH;
  `--code-doctor --code-json` lo declara disponible con procedencia
  `runtime-distribution`;
- los módulos empaquetados de supervisor, proveedor y repositorio Code son
  byte-idénticos a la fuente final. El launcher completó publicación, replay y
  diff usando exclusivamente estado aislado. Este runtime no se promovió al
  launcher estable.

### Candidato focal `rc21` — cierre de `lookup_exact`

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc21-knowledge-exact-orchestration\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `91066E02F35EC922F142D57A3218DC7E0E18F90AC2419254C94A16F99555E15E`;
- 1 340 108 bytes, 274 miembros y `ZipFile.testzip()` limpio;
- `Neocortex 0.7.2`, `pip check` limpio e import de `knowledge_exact.py`
  confirmado desde `site-packages`; fuente e instalación comparten SHA-256
  `9CAFC6AC48574BA4EDA7E3A0F49C20E5A55641EF5C81B95B489D5C89F7C939A9`;
- el launcher instalado completó piloto 30/30, publicación 522 candidatos con
  30 hits reutilizados y replay 522/522, sin bytes ni tiempo de lectura,
  análisis, persistencia o grafo. Este runtime no se promovió al launcher
  estable.

### Candidato focal `rc20` — paquetes de mantenimiento y taxonomy

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc20-final2-work-packages-taxonomy\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `ACAB258A8FC3EB8102B8CEE247D97D63CEFA20DE80F36B9FF6E52FD6E13E21A3`;
- 1 339 546 bytes, 274 miembros y `ZipFile.testzip()` limpio;
- `Neocortex 0.7.2`, `pip check` limpio e imports de
  `code_review_work_packages.py` y `document_taxonomy.py` confirmados desde el
  `site-packages` del venv rc20-final2;
- el launcher instalado reproduce review/diff sin cambiar ninguna SQLite ni
  crear sidecars y el replay incremental final obtiene 521/521 cache hits con
  cero lectura, análisis, persistencia o grafo. Este runtime no se promovió al
  launcher estable.

### Candidato focal `rc19` — cierre de hotspot Knowledge Exact

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc19-knowledge-exact\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `22A300CF3B6EBF9E725B4D50AAF67BAB7EA9E15453D4172D070DAB71C4B263A0`;
- 1 331 901 bytes, 271 miembros y `ZipFile.testzip()` limpio;
- `Neocortex 0.7.2`, `pip check` limpio e import de `knowledge_exact.py`
  confirmado desde el `site-packages` del venv rc19;
- fuente e instalación comparten SHA-256
  `8B601F9C499FB08734D396F126713089E15C62C6438931C650E0710B788D3743`;
  `_lookup_catalog` ocupa 58 líneas y complejidad 5 en la publicación final;
- el launcher instalado reprodujo 515/515 cache hits con cero lectura,
  análisis, persistencia o grafo, y reprodujo review/diff sin cambiar ninguna
  SQLite ni crear sidecars. Este runtime no se promovió al launcher estable.

### Candidato focal `rc18` — cierre de hotspot Knowledge context

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc18-knowledge-context\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `6A4826238775A3DFCEBFE0D909172C40FBE5F788B7F726EADFF47C436008C8C8`;
- 1 331 108 bytes, 271 miembros y `ZipFile.testzip()` limpio;
- `Neocortex 0.7.2`, `pip check` limpio e import de
  `knowledge_context.py` confirmado desde el `site-packages` del venv rc18;
  fuente e instalación comparten SHA-256
  `EC0C946EE4F3DCF5FE8506989434D8BA21222763EAAB12D4C750543477CB2BB5` y
  el coordinador instalado ocupa nueve líneas;
- una comparación diferencial rc17/rc18 de 19 399 bytes sobre relación Code
  válida, duplicado planeado y evidencia inválida produjo JSON exactamente
  idéntico, SHA-256
  `A1695E6856AF0E9C3876EDDD40C02A5721BF341BABD86F30536D3C1B20F1403E`;
- el launcher instalado reprodujo 515/515 cache hits con cero lectura,
  análisis, persistencia o grafo, y reprodujo review/diff sin cambiar ninguna
  SQLite ni crear sidecars. Este runtime no se promovió al launcher estable.

### Candidato focal `rc17` — cierre de hotspot Semantic queue

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc17-semantic-queue\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `3EEB5A7CEC59A5441F7CE0EA540A8F1BC615A0FC1397483F1E9BC7CAB7988608`;
- 1 330 500 bytes, 271 miembros y `ZipFile.testzip()` limpio;
- `Neocortex 0.7.2`, `pip check` limpio e import de
  `semantic_generation_repository.py` confirmado desde el `site-packages` del
  venv rc17; el símbolo instalado ocupa 47 líneas;
- el venv focal usa `--system-site-packages`; verifica este wheel, no sustituye
  la validación full hermética de rc5;
- el launcher instalado reprodujo review/diff sin cambiar ninguna SQLite ni
  crear sidecars. Este runtime no se promovió al launcher estable.

### Candidato focal `rc16` — Actionability Gate

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc16-actionability\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `152ADFA1B9EA0EA501CF974980BC8099C0030685FA5949FF04F1F3D2CB864A25`;
- 1 329 688 bytes, 271 miembros y `ZipFile.testzip()` limpio; los dos módulos
  nuevos de actionability/modelos están presentes;
- `Neocortex 0.7.2`, `pip check` limpio e imports confirmados desde el
  `site-packages` del venv rc16;
- el launcher instalado emitió `neocortex.code-review/v2`, 50 findings y tres
  recomendaciones, sin cambiar el SHA-256 de ninguna SQLite ni crear sidecars;
- este runtime no se promovió al launcher estable.

### Candidato focal `rc14` — cierre de hotspot Knowledge

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc14\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `1A2836C9B718E85F47BBB2691453BC0CDDA6C421E78706C54E545431FDEB4F54`;
- 1 322 579 bytes, 269 miembros y `ZipFile.testzip()` limpio;
- `Neocortex 0.7.2`, `pip check` limpio e import de `knowledge_search.py`
  confirmado desde el `site-packages` del propio venv;
- `execute_knowledge_search` instalado conserva su firma pública y ocupa 26
  líneas;
- el launcher rc14 reprodujo el diff rc12→rc14 sin modificar el SHA-256 de
  ninguna SQLite. Este runtime no se promovió al launcher estable.

### Candidato focal `rc12` — calibración y diff de publicaciones

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc12\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `AE95DB50A4ACB8EC958B90D89EF0A72DF88E00D862B3A87726C8E303F44A7D21`;
- 1 322 090 bytes y 269 miembros;
- `ZipFile.testzip()` limpio; `RECORD`, entry point y
  `_04_Nucleo_Operativo/code_publication_diff.py` presentes.

Runtime de smoke:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc12\venv`.

- `Neocortex 0.7.2`, `pip check` limpio e imports confirmados desde el
  `site-packages` del propio venv, no desde el checkout;
- el launcher rc12 reprodujo rc6→rc11 con digest
  `7870b9de799ff095c8c54ae3fbfc83f2`: 1 622 resoluciones nuevas, 57
  correcciones, cero pérdidas, dos hotspots añadidos y uno retirado;
- los SHA-256 de ambas SQLite permanecieron idénticos antes/después del diff;
- este venv usa `--system-site-packages` para un smoke focal. No sustituye la
  prueba full hermética de `rc5` ni autoriza promover el launcher estable.

### Candidato focal `rc11` — autoanálisis y code-review

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc11\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `950BDEA5161241C57A276A8C5A60FA8A01F4FB41B8C63CDCA588C867EE19139D`;
- 1 315 047 bytes y 268 miembros;
- `ZipFile.testzip()` limpio; `RECORD`, entry point y
  `_04_Nucleo_Operativo/code_review.py`/`code_state.py` presentes.

Runtime de smoke:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc11\venv`.

- `Neocortex 0.7.2`, Python analyzer v5, graph resolver v7, módulo importado
  desde ese venv y `pip check` limpio;
- el launcher instalado devolvió exit `0`, 10 findings y digest
  `33d8ba5de1b0f005b7763f12fc814ed8` sobre la publicación rc11;
- dos lecturas JSON consecutivas fueron byte a byte idénticas;
- este venv usa `--system-site-packages` para un smoke focal de empaquetado. No
  sustituye la prueba full hermética de `rc5` ni autoriza promover el launcher.

### Candidato full `rc5`

Wheel:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-final-20260802-rc5\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.

- SHA-256
  `F06F7DFCD5B72F87CC5A1A6EEEDC446E9FEF9A4903955235A9BB764CB2AAC74C`;
- XXH3-128 `9b0034b5c1dc944cdfc1cb426a3da97c`;
- 1 304 214 bytes;
- 267 miembros; `RECORD`, entry point y ambos marcadores `py.typed` presentes;
- `ZipFile.testzip()` limpio; contiene las correcciones finales de provenance
  Semantic, quiescencia Framework y el puente Code↔Semantic.

Runtime aislado validado:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-final-20260802-rc5\venv`.

- 54 distribuciones y `pip check` limpio;
- `Neocortex 0.7.2` y las ocho capacidades `available`;
- consulta positiva Semantic: 1 974 vectores recorridos, 30 candidatos exactos,
  17 retenidos tras los pisos y 10 hits fusionados;
- consulta OOD: 30/30 candidatos excluidos, `calibrated_abstentions=1` y cero
  hits; Knowledge devolvió cero evidencia y explicó
  `semantic_candidate_limit_reached_after_calibrated_abstention`;
- replay imagen v10: 30/30 cache hits, 3 candidatos documentales, 6 de contexto
  industrial, 8 fotos y cero errores/OCR nuevo.

Este runtime es candidato de laboratorio. No se creó ni promovió un nuevo
launcher en `bin`.

## Autoanálisis del propio framework

El vertical slice portable nació en `--self-analysis` y ya se generalizó al
flujo normal. Si la consulta USN falla con `NtfsUsnError` u `OSError`, una única
enumeración completa produce el snapshot durable y Code compara ese snapshot
contra sus versiones actuales. La corrida registra `inventory_mode=full`,
`attempts=1`, cero reconciliaciones y journal `unavailable`; publica un
checkpoint de inventario ligado a la política, sin cursor USN ficticio. USN es
un acelerador opcional para Windows, no una dependencia de corrección.

La política `inventory-exclusion-policy-v2` excluye el estado, Git, caches,
builds, bases derivadas, logs y raíces transitorias de pytest/laboratorio. El
manifest v2 representa explícitamente journal disponible/no disponible; el
decoder conserva lectura estricta de v1. Un status journal-free puede validar
la evidencia terminada, pero devuelve `current=false` porque una consulta
read-only no puede demostrar frescura posterior sin volver a recorrer la raíz.

Baseline calibrado rc12:
`C:\Users\Victor\Neocortex\Laboratory\code-review-self-analysis-20260802-rc12-state`.

- piloto inicial acotado: inventario 517 archivos, 509 candidatos, 509
  procesados, 8 888 058 bytes, 14 651 símbolos, 80 979 referencias, cero
  errores/acciones y 18.157 s de pared. El límite explícito de 1 000 conservó
  correctamente el run como `partial`, aunque no se alcanzó;
- segunda corrida completa: 0 procesados/509 cache hits, run `completed` y
  publicación elegible; la tercera corrida repitió 509/509 cache hits en
  3.759 s con cero bytes y cero ms de lectura, análisis, persistencia y grafo;
- `--code-review` publicó en ambos replays el digest estable
  `51196515f21c2268766e8d3d4aed1dc5`; 464/464 Python completos, 185 hotspots
  únicos y 18 508/58 739 calls resueltas;
- el top 10 v2 inicia con `knowledge_search.execute_knowledge_search`
  (complejidad 75, 416 líneas, 37 callers) y ya no contiene el builder
  declarativo `cli_parser.build_parser`;
- la muestra de actionability contiene 24 `actionable` y 17 `defer` en la unión
  de 41 candidatos. P@10 pasa de 6/10 a 7/10; P@20, P@30 y P@40 permanecen
  iguales;
- la muestra SHA-256 portable de dead code exige 37/40 abstenciones. Sólo
  `_query_vector`, `_text_probe` y `_enqueue_text_chunk_batch` quedaron como
  candidatos de revisión; no son autorización de borrado;
- el diff rc11→rc12 encontró 49 775 calls comunes, cero resoluciones nuevas,
  corregidas o perdidas y hotspots 185→185. Los sitios exclusivos reflejan
  desplazamientos de rango y tres Python nuevos, como declara la limitación del
  contrato.

Publicación del refactor rc14:
`C:\Users\Victor\Neocortex\Laboratory\code-review-self-analysis-20260802-rc14-state`.

- 517 archivos inventariados, 509 candidatos, 509 procesados, 8 894 324 bytes,
  14 679 símbolos, 80 993 referencias, 199 diagnósticos del analizador y cero
  errores/acciones en 18.260 s;
- 464/464 Python completos, 18 518/58 747 calls resueltas y 246 dead
  suprimidos;
- `--code-review` publica 184 hotspots y digest
  `e7e45591ab39c850d5049adacabedde3`;
- el diff rc12→rc14, digest `acc663603c843d26e8591433c64cecb3`,
  retira sólo `knowledge_search.execute_knowledge_search`, añade cero hotspots
  y conserva cero resoluciones nuevas, corregidas o perdidas;
- replay final mediante el launcher instalado rc14: 0 procesados/509 cache
  hits en 3.706 s, cero bytes y cero ms de lectura, análisis, persistencia y
  grafo; los digests de review/diff permanecieron estables;
- rc13 fue un piloto intermedio detenido: retiró el hotspot objetivo pero creó
  dos auxiliares y empeoró 185→186. La segunda partición eliminó ambos y cerró
  185→184; no se publicó ese wheel como resultado final.

Publicación del Actionability Gate rc16:
`C:\Users\Victor\Neocortex\Laboratory\code-review-self-analysis-20260802-rc16-actionability-state`.

- piloto explícitamente acotado: 525 archivos inventariados, 46 directorios
  excluidos, 515 candidatos procesados, 8 932 466 bytes, 14 785 símbolos,
  81 229 referencias, 201 diagnósticos, un proyecto y cero errores; el límite
  explícito conservó correctamente el run como `partial`;
- segunda corrida: 0 procesados/515 cache hits y publicación `completed`; el
  replay final repitió 515/515 cache hits, cero bytes y cero ms de lectura,
  análisis, persistencia y grafo;
- review de 50 findings: tres recomendaciones y digest estable
  `be6eb7aafddd34270136ad7b1093e468`; la primera es
  `semantic_generation_repository._queue_job_rows_bounded` (rango bruto 2),
  seguida de `_derive_context_graph` y `_lookup_catalog`;
- diff rc14→rc16, digest `75d3349478676823776bf37740091e64`:
  184 hotspots comunes, cero añadidos, retirados o con evidencia cambiada;
  58 200 calls comunes, 39 856 aún no resueltas, 18 344 resueltas sin cambio y
  cero resoluciones corregidas o perdidas;
- rc15 fue rechazado al añadir el hotspot
  `code_review_actionability._construction`. La partición rc16 lo retiró y
  restauró exactamente la evidencia de hotspots de rc14.

Publicación del refactor Semantic rc17:
`C:\Users\Victor\Neocortex\Laboratory\code-review-self-analysis-20260802-rc17-semantic-queue-state`.

- piloto acotado: 525 archivos, 46 directorios excluidos, 515 candidatos,
  8 939 566 bytes, 14 813 símbolos, 81 253 referencias, 199 diagnósticos, un
  proyecto y cero errores; la corrida explícitamente limitada permaneció
  `partial` como exige el contrato;
- tras incorporar la regresión, la publicación final contiene 14 815 símbolos
  y 81 264 referencias. El replay desde el wheel rc17 obtuvo 0 procesados/515
  cache hits, cero bytes y cero ms de lectura, análisis, persistencia y grafo;
- review de 50 findings: digest
  `aca6e380664ca2c1947288f6b88a7b74`; el objetivo ya no aparece y la primera
  recomendación pasa a `knowledge_context._derive_context_graph`;
- diff rc16→rc17, digest `0f7857652222f06dd0943b1cc0027c30`:
  hotspots 184→183, 183 comunes, cero añadidos/cambiados y sólo el objetivo
  retirado; 58 063 calls comunes, 39 769 aún no resueltas, 18 294 resueltas sin
  cambio y cero resoluciones nuevas, corregidas o perdidas;
- las consultas review/diff conservaron idénticos todos los SHA-256 SQLite y
  dejaron cero sidecars.

Publicación del refactor Knowledge context rc18:
`C:\Users\Victor\Neocortex\Laboratory\code-review-self-analysis-20260802-rc18-knowledge-context-state`.

- piloto con límite explícito: 525 archivos, 46 directorios excluidos, 515
  candidatos, 8 948 192 bytes, 14 851 símbolos, 81 301 referencias, 197
  diagnósticos, un proyecto y cero errores/acciones; el run Code permaneció
  `partial` por contrato;
- la finalización reutilizó 515/515 entradas y el replay desde el wheel rc18
  repitió 515/515 cache hits con cero bytes y cero ms de lectura, análisis,
  persistencia y grafo;
- review de 50 findings: digest
  `30675d2f4c06ec708900f7436bba77e5`; el objetivo desaparece y la primera
  recomendación pasa a `knowledge_exact._lookup_catalog`;
- diff rc17→rc18, digest `21053e149aff5292f448a747b6e4044b`:
  hotspots 183→182, 182 comunes, cero añadidos/cambiados y sólo el objetivo
  retirado; 58 568 calls comunes, 40 112 aún no resueltas, 18 456 resueltas sin
  cambio y cero resoluciones nuevas, corregidas o perdidas;
- status/review/diff mediante el launcher rc18 conservaron idénticos todos los
  SHA-256 SQLite y dejaron cero sidecars. `current=false` es la limitación
  explícita esperada de `journal_status=unavailable`, no una publicación
  inválida;
- una segunda publicación aislada desde los mismos bytes reprodujo el digest
  de review `30675d2f4c06ec708900f7436bba77e5`; su diff contra rc18 encontró 58 969
  calls y 182 hotspots comunes, cero exclusivos y cero evidencia cambiada.

Publicación del refactor Knowledge Exact rc19:
`C:\Users\Victor\Neocortex\Laboratory\code-review-self-analysis-20260802-rc19-knowledge-exact-state`.

- piloto con límite explícito: 525 archivos, 46 directorios excluidos, 515
  candidatos, 8 957 488 bytes, 14 868 símbolos, 81 340 referencias, 196
  diagnósticos, un proyecto y cero errores/acciones; el run Code permaneció
  `partial` por contrato;
- la finalización reutilizó 515/515 entradas. Después de fijar la regresión que
  el primer diff señaló como hotspot, una actualización procesó sólo ese archivo
  y conservó 514 hits; el replay desde el wheel rc19 repitió 515/515 cache hits,
  cero bytes y cero ms de lectura, análisis, persistencia y grafo;
- review de 50 findings: digest
  `588782e9ec4693947c99891b2a023f17`; `_lookup_catalog` desaparece y la primera
  recomendación pasa a `document_taxonomy.classify_document`;
- diff rc18→rc19, digest `56a5c40d4ace5953e02d53bbdac1743c`:
  hotspots 182→181, 181 comunes, cero añadidos/cambiados y sólo el objetivo
  retirado; 58 451 calls comunes, 40 151 aún no resueltas, 18 300 resueltas sin
  cambio y cero resoluciones nuevas, corregidas o perdidas;
- status/review/diff mediante el launcher rc19 conservaron idénticos todos los
  SHA-256 SQLite y dejaron cero sidecars. `current=false` sigue siendo la
  limitación explícita esperada de `journal_status=unavailable`.

Publicación del planner y refactor taxonomy rc20:
`C:\Users\Victor\Neocortex\Laboratory\code-review-self-analysis-20260802-rc20-final2-work-packages-taxonomy-state`.

- piloto acotado a 30 archivos antes de completar la misma raíz; la publicación
  final contiene 531 archivos, 46 directorios excluidos, 521 candidatos, 15 011
  símbolos, 81 699 referencias, 193 diagnósticos, un proyecto y cero errores;
- la finalización procesó 491 entradas y reutilizó las 30 del piloto; una corrida
  absorbió únicamente la actualización de este handoff (1 procesado/520 hits) y
  el replay posterior desde el wheel rc20-final2 repitió 521/521 cache hits con
  cero bytes y cero ms de lectura, análisis, persistencia y grafo;
- review v3 byte-estable: digest `a626ac5237e013d6dfc420c85cadf45c`,
  471/471 Python completos, 179 hotspots, 59 240 calls y 18 734 resueltas; el
  siguiente paquete contiene únicamente la raíz `knowledge_exact.lookup_exact`;
- diff rc19→rc20, digest `071308ea3a489433ed226b91396c5173`:
  179 hotspots comunes, cero añadidos/cambiados y dos retirados; 57 231 calls
  comunes, 39 136 aún no resueltas, 18 095 resueltas sin cambio y cero
  resoluciones nuevas, corregidas o perdidas;
- un primer candidato rc20 fue rechazado porque creó el hotspot
  `code_review.review_code_state`; la extracción mínima del cálculo de estado lo
  retiró antes de aceptar el corte, sin relajar umbrales;
- review/diff conservaron idénticos todos los SHA-256 SQLite y dejaron cero
  sidecars. `current=false` conserva la limitación explícita y honesta de un
  journal no disponible en una consulta read-only.

Publicación del refactor exacto rc21:
`C:\Users\Victor\Neocortex\Laboratory\code-review-self-analysis-20260802-rc21-knowledge-exact-orchestration-state`.

- piloto acotado a 30 archivos en 2.98 s, 336 símbolos, 1 375 referencias,
  siete diagnósticos y cero errores; la finalización procesó 492 entradas y
  reutilizó las 30 del piloto;
- publicación completa: 532 archivos, 46 directorios excluidos, 522 candidatos,
  15 034 símbolos, 81 751 referencias, 192 diagnósticos, un proyecto y cero
  errores; replay posterior 522/522 cache hits con cero bytes y cero ms en las
  cuatro fases Code;
- review v3 byte-estable: digest `29a9aa1f9cbb0931f4c8009aa458eb28`,
  471/471 Python completos, 178 hotspots, 59 289 calls y 18 768 resueltas;
- diff rc20→rc21, digest `38df89a20e6493770c634d3f44b1604b`:
  178 hotspots comunes, uno retirado, cero añadidos/cambiados; 58 084 calls
  comunes, 39 914 aún no resueltas, 18 170 resueltas sin cambio y cero
  resoluciones nuevas, corregidas o perdidas;
- review y diff conservaron idénticos todos los SHA-256 SQLite de rc20 y rc21 y
  dejaron cero sidecars. `current=false` permanece como
  `freshness=publication_only` por `journal_status=unavailable`.

Publicación de evidencia externa rc22:
`C:\Users\Victor\Neocortex\Laboratory\neocortex-0.7.2-self-analysis-20260802-rc22-final-external-ruff-evidence\full-repository-release-state`.

- un piloto previo de 30 archivos justificó ampliar la corrida; la evidencia
  de release siguiente se repitió desde cero después del último hardening;
- publicación completa: 534 archivos inventariados, 46 directorios excluidos,
  524 candidatos, 9 180 861 bytes, 15 245 símbolos, 82 985 referencias, 203
  diagnósticos internos, un proyecto y cero errores en 27.448 s;
- Ruff 0.15.17 cubrió 473/473 Python en 7.306 s, sin hallazgos bajo el perfil
  fijo `E4,E7,E9,F`; esto demuestra la barrera seleccionada, no ausencia de
  cualquier posible defecto;
- replay completo: 0 procesados/524 hits, cero bytes y cero ms en lectura,
  análisis y persistencia; grafo 1 ms, evidencia Ruff reutilizada en 112 ms y
  pared total 4.164 s;
- dos lecturas consecutivas del review v4 por defecto sobre el replay fueron
  byte-idénticas: digest `212d50c187a82fe2ef43487a1906751e`, 188 hotspots,
  473/473 Python, 60 277 calls y 19 045 resueltas. El digest distinto observado
  al adquirir por primera vez la línea base Ruff es esperado porque el gate
  cambia de `baseline` a `passed`. Su primer paquete es
  `bounded_subprocess.run_bounded_capture`, seguido por
  `semantic_image_index.index_image_embeddings`;
- el diff
  inicial→replay conservó 188 hotspots comunes, cero exclusivos/cambiados y
  60 277 calls comunes: 19 045 resueltas sin cambio, 41 232 aún no resueltas y
  cero resoluciones nuevas, corregidas o perdidas;
- status/review/diff conservaron idénticos los SHA-256 de las seis bases
  iniciales/finales y dejaron cero sidecars. El estado histórico rc21 sigue
  legible y byte-inmutable: declara Ruff `not_recorded`; su diff hacia rc22
  conserva el estado interno `ready`, añade diez hotspots y cambia evidencia de
  uno, y deja el gate externo `not_evaluated` sin inventar comparabilidad.

Las publicaciones rc6, rc11 y
`graph-resolver-v4-20260801-rc1\full-state` se conservan como baselines
históricos; no se mutaron. La línea base anterior de 58 imports relativos no
resueltos y 1 000 dead candidates queda retirada. Los 246 dead restantes
continúan siendo candidatos diagnósticos, no una orden de refactor ni borrado.

## Inventario normal portable — Dedup v9

El schema Dedup v9 permite que un checkpoint publicado conserve la terna USN
completa o la omita por completo. Raíz, política, scan publicado y timestamp
siguen siendo obligatorios; una terna parcial falla cerrada. La migración
exacta v8→v9 reconstruye sólo `inventory_checkpoints`, conserva las filas y
verifica conteos, claves foráneas e idempotencia. Un schema v8 desconocido se
rechaza sin mutarlo.

El coordinador intenta el recorrido USN cuando existe un cursor compatible y
cae al recorrido completo portable si el journal no está disponible o falla
durante la preparación. Un checkpoint portable nunca se reutiliza como cursor
USN. La reconciliación normal conserva la misma verdad publicada, caché de Code,
cancelación y reanudación. El watcher ya acepta checkpoints sin cursor: espera
`--watch-portable-interval-seconds` —300 s por defecto— y dispara una corrida
normal portable. Un fallo `NtfsUsnError`/`OSError` en el lector USN usa el mismo
fallback; otros errores conservan backoff. No crea cursor, base ni indexador
paralelo y todavía no se presenta como daemon multimodal completo.

Piloto previo de Dedup v9, conservado como evidencia:
`C:\Users\Victor\Neocortex\Laboratory\portable-inventory-v9-20260801-rc1`.

- Wheel:
  `wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`;
- SHA-256
  `4E7129974824580222358297844F932471A4083DEDB285364479E0CDA359B11D`;
- runtime con 53 paquetes, `pip check` limpio y las ocho capacidades
  disponibles;
- piloto CLI normal aislado de 25 archivos: primera corrida 25 procesados y 50
  símbolos en 1.875 s; replay 0 procesados/25 hits y cero bytes en 1.704 s;
  alta+modificación+rename+borrado 3 procesados/22 hits en 1.679 s; replay final
  0/25 y cero bytes en 1.648 s;
- la reconciliación final sin límite dejó Code `completed`, 25 archivos
  actuales, 50 símbolos, cero diagnósticos y checkpoint Dedup v9 portable;
- una copia poblada real migró v8→v9 y ejecutó autoanálisis: 500 archivos de
  inventario, 494 candidatos Code, 27 procesados/467 hits, 14 232 símbolos,
  70 868 referencias, 189 diagnósticos y cero errores en 4.963 s;
- el SHA-256 de la base v8 fuente permaneció
  `8DFA95A4159C8D6DDB4E532FC5A1AE4867387CBE16D234BC79F3FD411C0A1490`;
  sólo la copia aislada se migró;
- el launcher estable, el estado durable y el corpus real permanecieron
  intactos.

## Code ↔ Semantic y grafo v4

`code-semantic-link-v1` sincroniza `code.embedding_links` sólo después de una
publicación Semantic `ready`. Exige coincidencia exacta de identidad física,
versión Code, chunk, item, modelo, espacio y generación. El lector revalida
ambos heads; los scores conservan `retrieval_evidence_only` y
`uncalibrated_similarity` y nunca autorizan mutación.

Piloto:
`C:\Users\Victor\Neocortex\Laboratory\code-semantic-20260801-rc1\pilot-code-30`.

- 30 archivos, 1 326 símbolos y 7 045 referencias;
- 732 embeddings y 91 enlaces Code activos/vigentes;
- replay exacto con cero jobs y mismo head;
- seis consultas: lexical Hit@5 `2/6`, MRR `0.333`; Semantic/hybrid Hit@5
  `5/6`, MRR `0.708`; las cuatro paráfrasis de familia fueron recuperadas;
- sin modelo local, `semantic` se abstiene con exit `2` y `hybrid` conserva los
  canales deterministas.

La calibración del grafo etiquetó 40 imports y 40 dead candidates antes de
editar: 40/40 imports sí tenían target indexado y 37/40 dead candidates tenían
una referencia estática demostrable. Analyzer v3 preserva imports relativos y
resolver v4 prioriza ruta léxica y scope de módulo/clase; ante ambigüedad se
abstiene y conserva el fallback global sólo para targets únicos.

Al publicar Code se hace checkpoint del WAL y se retiran sidecars sólo si son
reconstruibles y el WAL está vacío. Un lector externo puede diferir la limpieza
sin revertir el run; el status quiescente se abstiene hasta una corrida posterior.
Las búsquedas/listados Code sobre una base quiescente usan una instantánea
immutable cercada y ya no recrean sidecars. Si detectan un writer, conservan el
lector WAL read-only y nunca limpian archivos ajenos.

La recarga del dueño durable del watcher aplica el mismo contrato estricto a
Framework. El defecto real se reprodujo: `mode=ro` recreaba un WAL vacío y SHM
después de cada publicación. `rc5` usa el snapshot immutable sólo cuando no hay
sidecars y entra a backoff ante actividad. Tres ciclos portables más cancelación
dejaron Code, Framework y Dedup sin sidecars; `--code-status` terminó en `0`.

## Knowledge cross-owner

Estado combinado:
`C:\Users\Victor\Neocortex\Laboratory\knowledge-cross-owner-20260801-rc1`.

Se combinaron los pilotos publicados de 35 PDF y 30 archivos Code. La generación
13 quedó `ready` en 490.616 s con 732 embeddings Code nuevos, cero errores y 91
enlaces. El head contiene 2 039 miembros: 1 272 cuerpos PDF, 35 títulos PDF, 702
cuerpos Code y 30 títulos Code. El replay con límites `1/1` devolvió el mismo
head, cero jobs y exit `0`.

Doce consultas etiquetadas —seis PDF, seis Code— cubrieron 18 targets; tres
consultas adicionales midieron abstención. Todas las capturas válidas tuvieron
snapshot estable:

| Variante | Targets @10 | Hit@5 | MRR | Recall macro | Mediana | Máxima |
|---|---:|---:|---:|---:|---:|---:|
| FTS sola | 5/18 | 3/12 | 0.2500 | 0.2500 | 1 ms | 30 ms |
| FTS + cuerpo (`evidence`) | 16/18 | 11/12 | 0.8194 | 0.8889 | 3.049 s | 3.296 s |
| FTS + cuerpo + título (`discovery`) | 17/18 | 12/12 | 0.9167 | 0.9583 | 3.109 s | 3.287 s |

`discovery` produjo 119 hits con `semantic_title` y cero títulos como evidencia.
Ganó la paráfrasis GOOSE y completó los tres targets del flujo portable de
inventario, pero perdió uno de dos targets en una consulta Semantic; no se cambió
el peso `0.5`.

FTS se abstuvo en 3/3 consultas fuera de dominio. La campaña posterior añadió
15 positivos, OOD claros y negativos técnicamente cercanos. Para el contrato
exacto Jina/body se fijaron pisos por owner: PDF `0.50`, Code `0.46`. El filtro
exige firma, backend, pipeline y owner exactos; vectores reutilizados obtienen el
contrato desde `payload_provenance` y un conflicto queda sin calibrar. La política
no forma parte del modelo registrado, por lo que los heads existentes continúan
compatibles.

En el smoke rc5 la positiva retuvo 17/30 candidatos y sus 10 primeros hits
relevantes; la OOD descartó 30/30 y devolvió cero. Algunos negativos cercanos
permanecen por encima del piso: los scores siguen siendo similitud de recuperación,
no probabilidad o certeza. El exit `4` de Knowledge refleja owners/canales
ausentes y su candidate limit, no evidencia inventada.

## Barreras

- Hito 3 trusted-deep: la barrera focal del worker, Coverage, registry y
  configuración aprobó `27 passed` bajo una ruta Windows autorreferencial larga;
  incluye Git real dentro del `tmp_path`, arcos negativos, HOME, PATH y replay
  de checkpoints. Manifest/finalización/orquestador/status aprobó `79 passed` y
  cuatro mutaciones deep válidas/incoherentes; el alias Coverage empaquetado
  aprobó cuatro regresiones con ambigüedad fail-closed. Ruff y formato están
  limpios; Mypy local no transitivo está limpio. El piloto rc4 y su replay
  instalados aprobaron 22/22 pruebas, ocho proveedores y ambos gates sin tocar
  launcher estable, corpus ni estado durable vivo.
- Hito 1 multianalizador: 13/13 regresiones de proveedores y 3/3 de publication
  diff aprobadas; la barrera integrada final del corte obtuvo 102 aprobadas. La barrera
  Code ampliada alcanzó 335 aprobadas y aisló un defecto del corte, luego cerrado
  con regresión focal, además del límite estructural preexistente de
  `knowledge_search_code.py` (907 líneas frente a 900), fuera de los archivos
  modificados. Ruff y formato quedaron limpios en los Python finales; el wheel
  instalado, `pip check`, doctor, full real, replay, status, review, diff rc22 y
  hashes del checkout quedaron verificados. No se presenta la barrera ampliada
  inicial como suite integral verde.
- Semantic completo más CLI, Code↔Semantic y extracción Knowledge: `448 passed`.
- Watcher, cancelación, Framework/status y frontera normal: `96 passed` en la
  barrera final; la ampliada inventario/watcher/imagen aprobó `139 passed`.
- Imagen v10: `68 passed`; replay real 30/30 cache hits y cero errores.
- Code ampliado: `144 passed`, `1 deselected` por el límite estructural conocido.
- Code-review y fronteras CLI focales: `139 passed`, `2 subtests`; Ruff y Mypy
  limpios sobre su implementación y contratos. La prueba real aislada devolvió
  exit `0`, 10 findings y cero mutación de estado.
- Ranking v2, diff de publicaciones, calibración dead, Code/CLI y autoanálisis:
  `279 passed`, `2 subtests`; la barrera focal previa aprobó `80 passed`.
  Ruff/format limpios en 11 archivos y Mypy sin errores en los cinco módulos
  fuente del corte.
- Actionability Gate v2, CLI, diff, persistencia y autoanálisis: `190 passed`;
  la regresión focal aprobó `38 passed`. Ruff/format limpios en ocho archivos y
  Mypy sin errores en los cinco módulos fuente. Wheel rc16 íntegro, `pip check`
  limpio y review read-only con cero cambios SQLite/sidecars.
- Refactor Semantic queue: línea base `53 passed`, barrera focal final
  `54 passed` y barrera integrada `194 passed`. La regresión inyecta un fallo
  después del upsert y comprueba rollback completo del slice. Ruff/format y
  Mypy limpios; wheel rc17, procedencia del import, replay, hashes y sidecars
  verificados.
- Refactor Knowledge context: línea base `36 passed` y barrera focal final
  `37 passed`; la barrera Knowledge/CLI amplia obtuvo `802 passed` y reprodujo
  sólo los dos límites estructurales preexistentes de 907/910 líneas frente a
  900 en archivos no modificados. Ruff/format, `git diff --check` y Mypy limpios;
  wheel rc18, procedencia, replay, hashes y sidecars verificados.
- Refactor Knowledge Exact: línea base `61 passed`, regresión contractual sobre
  rc18 `1 passed`, barrera focal final `62 passed` y barrera Knowledge/CLI amplia
  `804 passed`, `2 deselected` por los mismos límites preexistentes de 907/910
  líneas. Ruff/format, `git diff --check` y Mypy limpios; wheel rc19,
  procedencia, replay, hashes y sidecars verificados.
- Paquetes de mantenimiento y taxonomy rc20: línea base focal `196 passed`,
  caracterización previa `38 passed`, barrera integrada final `277 passed`,
  regresiones finales del planner `35 passed` y matriz contractual exacta
  `33 passed`. Ruff/format y Mypy 2.1 quedaron limpios en ocho módulos;
  `git diff --check`, wheel rc20, procedencia, piloto, replay, hashes y sidecars
  quedaron verificados. La
  barrera amplia aprobó 786 casos y aisló 17 rechazos causados por una frontera
  temporal mal alineada; la repetición de los tres archivos completos bajo el
  laboratorio correcto aprobó `38 passed`, incluidos los 17 rechazados.
- Refactor de `lookup_exact` rc21: caracterización previa y posterior
  byte-estable; barrera focal `68 passed`, consumidores Knowledge `149 passed`,
  Ruff/format y Mypy limpios. Wheel rc21, procedencia, piloto 30, publicación,
  diff formal read-only y replay 522/522 quedaron verificados; ningún helper
  alcanzó el umbral de hotspot.
- Evidencia externa Ruff rc22: barrera focal final `111 passed`; incluye
  staging disjunto, orden Unicode, casing de Windows, stdin sin temporal,
  abstención por proyección acotada y reparación de proyección alterada. Ruff
  limpio, formato limpio en 21 archivos y Mypy 2.1 sin errores en 14 módulos
  fuente. Wheel, perfil full, procedencia, `pip check`, publicación 524,
  cobertura Ruff 473/473, replay exacto y diff formal read-only quedaron
  verificados.
- La ampliación rc22 alcanzó `2372 passed`, `4 deselected` y `78 subtests` antes
  de detenerse en la barrera que exige activar explícitamente el laboratorio
  NTFS. La continuación por archivos obtuvo bloques de `764`, `16` y `145`
  aprobados y reprodujo sólo dos fallos preexistentes, en los contratos/política
  de conexión SQLite de Code; ambos archivos y sus implementaciones están
  intactos respecto de `fcb8528d`. Una de las cuatro exclusiones, la competencia
  de destino, se validó aparte en 11.512 s; la prueba estructuralmente costosa de
  grupo grande quedó excluida. Esta evidencia no se presenta como suite completa
  verde.
- Refactor Knowledge: línea base y dos repeticiones de `84 passed`; la barrera
  amplia final obtuvo `770 passed`, `2 deselected` después de reproducir por
  separado los dos límites estructurales preexistentes de 907/910 líneas frente
  a 900, fuera del archivo modificado.
- Resolver imports/aliases/reexports y refactor del hotspot: `137 passed`; Ruff
  0.15 y Mypy 2.1 limpios en los tres módulos fuente modificados. El piloto de
  30 archivos tuvo 30/30 cache hits en replay y la publicación completa 504/504.
- Knowledge + CLI amplio previo: `800 passed`; dos fallos estructurales
  preexistentes permanecen fuera de este slice:
  `knowledge_search_code.py` 907 líneas y
  `knowledge_search_inventory.py` 910 frente al límite 900. No maquillarlos
  borrando blancos.
- Ruff y formato limpios sobre todos los Python modificados del corte; Mypy 2.1
  sin errores en 37 módulos fuente.
- Wheel de 267 miembros, venv full nuevo, 54 distribuciones, `pip check`, versión
  y ocho capacidades verificados. Los smokes finales usaron el launcher rc5.
- Wheel rc12 de 269 miembros, `ZipFile.testzip()`, `pip check`, procedencia del
  import y launcher instalados verificados. El estable no se modificó.
- `git diff --check` limpio; sólo avisos de futura conversión LF→CRLF.
- Plataforma Hito 2: 111 casos focales de proveedores/plataforma contabilizados,
  31 consumidores de arquitectura/diff/review/work packages y 20 casos del
  launcher/capabilities aprobados. La regresión específica del conteo de módulos
  aprobó 2/2 antes de la barrera final de 31. Ruff quedó limpio y Mypy 2.1 no
  reportó errores en los módulos corregidos.
- Wheel final Hito 2 de 282 miembros: integridad ZIP, `pip check`, procedencia
  neutral, igualdad SHA-256 fuente/instalado, ocho capacidades, siete proveedores,
  foreign keys, aceptación instalada, replay y diff rc22 verificados. El estable
  no se promovió y ningún estado durable o corpus personal fue modificado.
- Plataforma Hito 4: barrera integrada de 34 casos aprobada; wheel de 288
  miembros con integridad y dependencias limpias; autoanálisis instalado con los
  nueve proveedores `ready`; replay exacto 558/558 + 9/9; status, review v8,
  diff v6, calibración, holdout, capacidades, schema y foreign keys verificados.
  El consenso real se abstuvo de producir falsos candidatos de alta confianza.
- Plataforma Hito 5: wheel de 293 miembros con integridad y dependencias
  limpias; los doce proveedores quedaron `ready` y sin errores; replay 575/575,
  once replays exactos más reverificación íntegra de `RECORD`; status, review
  v9, diff v7, seis gates, capacidades, schema y foreign keys verificados. Los
  seis findings reales permanecen visibles y no tienen autoridad de mutación.
- Plataforma Hito 6: wheel de 297 miembros con integridad y dependencias
  limpias; 23 módulos de producción byte-idénticos entre fuente, wheel e
  instalación; quince proveedores `ready`; corrida 9 y replay exacto 585/585;
  mutación focal completa, historia Git, analítica de ingeniería y arquitectura
  v2 consumidas por status, review v10, diff v8 y work package. JSON, stderr,
  sidecars y ausencia de autoridad de mutación quedaron verificados.

## Próximos pasos, en orden

Actualización local `2026-08-07`: el árbol `9d6fa1c` más el cambio pendiente de
compatibilidad CPython 3.14 produjo el wheel `0.7.2` SHA-256
`c390d9505758bebe2daa17a2a491ca7dea88070da700b6908dc6259c8e3f3b8d`.
Se instaló `full` con Python `3.14.6` en el runtime versionado
`0.7.2-py314-c390d950`, se promovió el launcher estable y no existían estado ni
corpus vivos que migrar. Pasaron 52 pruebas contractuales, 385 pruebas core
desde el wheel y el slice PDF sintético con replay de caché; los imports nativos
también pasaron después de instalar el Visual C++ v14 Redistributable x64. Audio,
Code, Semantic, UI, DOCX y Office están disponibles; PDF e imagen conservan sólo
la degradación opcional por Tesseract/qpdf ausentes.

1. **Versionar y publicar el cambio de compatibilidad.** Revisar el diff
   pendiente, crear una frontera Git intencional y no atribuirla al commit base
   `9d6fa1c`.
2. **Confirmar la matriz instalada en GitHub.** El carril `standard` debe
   construir e instalar `full`, importar sus wheels nativos y pasar la suite
   core tanto con Python 3.13 como con 3.14 antes de una entrega remota.
3. **Repetir la corrida real sólo cuando Victor lo decida.** Ejecutar
   `Neocortex --all`, sin `--apply`, sobre la raíz y estado canónicos. Las
   corridas 27 y 28 ya están `partial`; Code seleccionará proyectos por defecto
   y Semantic implícito priorizará documentos/audio.
4. **Consumir la publicación, no sólo observar progreso.** Al terminar, comprobar
   `--semantic-status` y consultas representativas en modo textual; registrar
   head, embeddings, errores, tiempo y cobertura faltante. Si se interrumpe,
   repetir el mismo `--all`: la generación es durable y reanudable.
5. **Retomar el siguiente work package publicado.** Caracterizar y reducir
   `external_mutation_cosmic_ray.execute_cosmic_ray_mutation` con regresiones de
   orden de fases y cancelación; conservar la publicación atómica y el estado
   sólo publicado.
   Mantener la evidencia de proveedores ausentes como abstención; no convertirla
   en una autorización de mutación ni en una instalación global.
6. **Cerrar la siguiente brecha de actionability sólo cuando bloquee el paquete.**
   Diff ya separa relocations, pero los findings realmente `added/resolved` aún
   exponen conteos y no ejemplos tipados públicos. Esta aceptación necesitó una
   lectura diagnóstica interna para localizar cuatro additions; si el siguiente
   paquete falla ese gate, añadir ejemplos acotados y consultables antes de
   seguir corrigiendo código.
7. **Tratar la latencia sólo como bloqueo medido.** Status/review/diff tardan
   34-71 s; una proyección publicada de consultas es trabajo futuro justificable,
   pero no invalida la entrega funcional actual.

Imagen, calibración Semantic y soak del watcher quedan detrás del programa de
autoanálisis. Preservar `Neocortex --all --apply` como interfaz cotidiana
simplificada después de pilotos y protecciones.
