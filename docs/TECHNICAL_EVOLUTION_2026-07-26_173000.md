# Evolución técnica integral de NeoCortex

> **DOCUMENTO HISTÓRICO.** Los conteos de chunks, jobs, vectores, tiempos e I/O
> son snapshots de esta campaña y no describen el estado vigente. Consulte
> [README.md](../README.md), [CLI.md](CLI.md), `Neocortex --semantic-status` y
> `Neocortex --knowledge-status` para el producto actual.

**Corte documental:** 2026-07-26 17:30:00 -06:00  
**Fuente recibida:** `neocortex-framework 0.7.0`  
**Fuente integrada final:** `neocortex-framework 0.7.1`  
**Estado del documento:** informe técnico de campaña; el cierre se determina en la sección 58  
**Sesión:** `NC-20260726T080001Z-0f35eeaf`

Este informe separa expresamente los hechos comprobados de los campos que aún
dependen de evidencia terminal. Los identificadores campos de cierre tipados son campos de
cierre inequívocos: deben sustituirse por un resultado demostrado o por una
abstención factual antes de emitir el paquete final.

### Alias sanitizados

| Alias | Significado |
|---|---|
| `<REPO>` | raíz del árbol fuente recibido |
| `<CORPUS>` | raíz operativa explícitamente verificada; no se publica su path personal |
| `<STATE>` | raíz de estado operativo |
| `<LAB>` | laboratorio exclusivo de la campaña |
| `<EVIDENCE>` | evidencia compacta sanitizada de la campaña |
| `<HANDOFF>` | paquete preparado para la auditoría siguiente |

No se incluye contenido documental del corpus, bases vivas, modelos, secretos ni
rutas personales innecesarias.

## 1. Resumen ejecutivo

La fuente recibida se identificó como `0.7.0` y la integración actual declara
`0.7.1`; la instalación global `0.3.0` no se promovió. La línea base ejecutable aprobó
la suite monolítica sin Coverage con **1,543 pruebas y 78 subpruebas**, y la
cobertura branch-aware basal acumuló **81.70859538784067 %**. Ruff, mypy sobre
202 módulos y `pip check` quedaron limpios en esa línea base. Se construyeron
wheel y sdist basales y se validó el entrypoint en un entorno limpio sin
dependencias; la instalación no fue hermética porque la closure local carece de
al menos el wheel requerido de `faster-whisper`, y la ejecución operacional del
wheel sin dependencias se abstuvo correctamente por ausencia de `xxhash`.

La campaña reprodujo y corrigió defectos en contención de procesos controlados,
bindings Python, publicación/reanudación del snapshot de routing, frontera de
confianza de contexto y escrituras FTS innecesarias. La contención GUI sigue
abierta y el RunManifest externo permanece parcialmente validado. Sus estados
se consignan en NC-AUD-036..042.

Runs 61/62 se conservan como intentos históricos interrumpidos anteriores al
hardening. No siguen activos ni cuentan como barrera. La cobertura segmentada
63/66/68/69 y la incremental integrada 70 los sustituyeron como evidencia
operacional; run 70 completó las seis rutas con incidencias cacheadas.

**Resultado integral final:** `Cobertura integral segmentada demostrada para inventario/dedup y las seis rutas en runs 63/66/68/69, más la segunda corrida incremental integrada run 70, siempre sin --apply. En Semantic terminó el staging de las 63,749 fuentes seleccionadas, incluidas 52,267 fuentes code con chunks, pero la campaña quedó failed tras 8,060.913785 s: sólo alcanzó 1/5 fases y no ejecutó inferencia ni publicación; quedaron 5,133,824 jobs pending, cero embeddings y ningún head. Knowledge real completó 50 casos en modo degradado y el golden sintético aprobó 17/17. El gate terminal aprobó 1,616 pruebas y 82 subpruebas, Ruff, mypy, pip check, golden, build y validator. La copia SQLite de 10 owners quedó completa, con 35,428,950,016 bytes, integridad/FK limpias y análisis partial/degraded explícito. Antes del handoff quedan integrar el informe, repetir build/validator sobre la fuente congelada, componer el ZIP, limpiar la sesión y verificar Defender.`.

## 2. Alcance y exclusiones

El alcance comprende inspección del árbol recibido, línea base, ejecución
read-only del pipeline sobre `<CORPUS>`, corrida incremental, análisis por
owner, Knowledge real, correcciones con regresiones, validación, documentación,
evidencia sanitizada y ZIP de handoff.

Exclusiones invariantes: no `--apply`, no organización física, rename, move,
delete, Papelera, retención aplicable, `VACUUM`, truncado de WAL, descargas de
modelos o binarios, instalación global, red, elección de licencia, ni uso del
contenido recuperado como instrucciones. No se introduce GraphRAG, MCP o ANN
sin superar su puerta de decisión.

## 3. Objetivo real

NeoCortex debe ser un sustrato local Windows-first, incremental, versionado y
trazable para modelos agénticos. Los archivos, identidades, revisiones y
evidencias son la realidad primaria; catálogo, FTS, embeddings, grafos y
ContextBundle son representaciones derivadas y reconstruibles. La recuperación
debe combinar exactitud, metadatos, texto, vectores, estructura, relaciones,
vigencia y procedencia, con abstención honesta ante evidencia insuficiente.

## 4. Entorno

| Propiedad | Hecho verificado |
|---|---|
| Plataforma | Windows 11, entorno local |
| Versión fuente recibida/final | `0.7.0` / `0.7.1` |
| Entrypoint | `Neocortex = neocortex.cli:entrypoint` |
| Instalación global observada | `0.3.0`, no promovida ni modificada |
| Laboratorio | `<LAB>`, separado del repositorio, corpus y estado |
| Corpus | `<CORPUS>`, raíz explícita; no inferida desde un default peligroso |
| Estado | `<STATE>` |
| Red/descargas | no autorizadas para esta campaña |

Versiones verificadas: Python `3.13.14`, PowerShell `7.6.4` y SQLite
`3.53.4` de 64 bits. El launcher global corresponde a `0.3.0`; el entrypoint
desde la fuente integrada responde `Neocortex 0.7.1`.

## 5. Contención

La campaña usa `<LAB>` para pycache, pytest, Coverage, build, dist, venvs,
temporales, copias, manifiestos, telemetría y handoff. El corpus y el estado vivo
no son destinos de esos artefactos. Se implementaron candidatos de contención
Windows mediante Job Objects con `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` en las
fronteras de proceso aislado y subprocess acotado. Las regresiones cubren nietos,
timeout, overflow, excepciones del caller, rollback de inicialización, cierre y
reap. La ruta GUI/QProcess no comparte aún un contrato equivalente demostrado.

**Barrera final de contención de sesión:** `Run 70 verificó cleanup=true tras crear 616 procesos. La fase Semantic post-fix terminó con Job vacío, proceso reaped y cero identidades residuales; Knowledge verificó cleanup en 17 invocaciones bajo Job y runtime vacío. El cierre global de sesión todavía requiere su manifiesto before/after.`.

## 6. Gobierno de agentes

La configuración global persistida quedó fijada en ocho threads de agentes por
sesión. Antes de esa instrucción se observaron trece subagentes más el agente
principal, **14 agentes totales**; después de su entrada en vigor se respetó el
máximo de **ocho agentes totales**. Los writers por archivo y las cargas pesadas
se mantuvieron serializados desde ese cambio.

## 7. Recursos y telemetría

El RunManifest es un arnés externo de campaña y una proyección derivada; los
eventos y estados durables de NeoCortex conservan autoridad. Las muestras
capturadas no constituyen máximos absolutos porque el muestreo se suspendió por
instrucción posterior del operador.

- Proceso del intento integral: private bytes máximos observados
  `3,541,184,512`, working set `2,085,150,720`, 1,815 handles y 141 threads.
- Muestra del intento integral: CPU host `38.4 %` y commit usado `55.212 %`.
- Muestra anterior al límite de ocho agentes: CPU `100 %` y commit usado
  `57.887 %`.
- Espacio mínimo libre e I/O exacto por fase: no medidos terminalmente; no se
  extrapolan desde muestras parciales.

## 8. Procesos antes/después

Existe un inventario inicial con PID, PPID, creation time y atributos de los
procesos relevantes. La propiedad de proceso se decide por árbol e identidad,
no por nombre. No se autoriza matar `python`, `codex` u otras familias por
coincidencia amplia.

| Resultado | Estado |
|---|---|
| Procesos del baseline | 148 |
| Procesos preexistentes no tocados | `148 procesos en baseline; la cifra final de preexistentes no tocados debe derivarse del manifiesto final, no por nombre.` |
| Procesos creados por la campaña | `Run 70 creó 616 procesos: 592 tesseract.exe, 12 python.exe y 12 conhost.exe. Otros intentos conservan sus árboles por identidad; no se inventa un total agregado donde la captura histórica no fue uniforme.` |
| Cierre normal | `Run 70 terminó durablemente con process_reaped y cleanup=true; Semantic y Knowledge también cerraron sus Jobs. La evidencia embebida conserva esta proyección pre-archive y remite el estado global al snapshot post-build.` |
| Terminación forzada exacta | `Hubo terminación forzada exacta en las recuperaciones de runs 62, 64, 65 y 67 tras ausencia de cancelación cooperativa; el total terminal exacto de procesos/descendientes queda al manifiesto final.` |
| Residuales finales | `Run 70, Semantic y Knowledge terminaron con cero identidades residuales exactas en sus alcances. El resultado global autoritativo se captura post-build, fuera de la cadena embebida para evitar circularidad.` |
| Handles/threads residuales | `Los Jobs observados quedaron vacíos y los procesos fueron reaped. No se certifican ubicaciones inaccesibles; el snapshot global post-build es la autoridad terminal.` |

El verificador legacy de run 61 informó cero residuales, pero sus limitaciones
motivaron NC-AUD-041; no se reutiliza como certificación final de la sesión.

## 9. Defender y exclusiones

Microsoft Defender no fue modificado: antivirus, protección en tiempo real y
behavior monitor estaban habilitados al inicio. Existía una exclusión previa
`G:\`, ajena a la campaña; no se añadió ni retiró exclusión de campaña y no hubo
una restauración que realizar.

**After-state Defender:** `La campaña no modificó Defender ni creó exclusiones. Por diseño, la comparación autoritativa de antivirus/RTP/behavior y exclusiones se realiza post-build y queda fuera de la evidencia embebida; si no puede leerse con privilegio legítimo, la barrera se mantiene bloqueada sin alterar protecciones.`.

## 10. Comparación con 0.7

La comparación se hace contra el árbol fuente `0.7.0` recibido y la integración
`0.7.1`, no contra las
cifras históricas como si fueran actuales. La instalación global `0.3.0` se
mantiene sólo como observación ambiental. La campaña conserva schemas
inventario 7, framework 19, catálogo 6, semantic 6 y code 2 salvo evidencia y
migración explícita posteriores.

No hubo DDL ni incremento de schema. Cambiaron la contención Windows, el
analizador Python, la publicación de routing, el ContextBundle y la actualización
FTS de cache hits. La comparación cuantitativa terminal queda en
`La fuente recibida 0.7.0 evolucionó a 0.7.1 sin DDL nuevo. El inventario final de fuente registra 724 archivos y 14,386,035 bytes: 203 módulos Python de producción y 137 archivos Python de pruebas, con 94,546 LOC de producción y 54,949 LOC de pruebas. El gate terminal, SQLite y paquetes se revalidaron sobre esa fuente; no se reutilizan los conteos históricos 0.7 como métricas actuales.`.

## 11. Inventario técnico

La línea base recontó:

- **708 archivos** y **13,293,672 bytes** en el conjunto inventariado de fuente;
- **202 módulos Python de producción** y **134 archivos Python de pruebas**;
- **92,833 líneas físicas Python de producción** y **51,445 de pruebas**;
- schemas principales: inventario 7, framework 19, catálogo 6, semantic 6 y
  code 2;
- Knowledge Plane sin schema propio.

Los inventarios finales por archivo y hash se publicarán en `<EVIDENCE>` sin
usar el contenido del corpus como payload.

## 12. Arquitectura

La arquitectura observada mantiene owners SQLite separados para inventario,
framework, catálogo, semantic y código. Knowledge consume esos owners en modo
read-only mediante contratos, snapshot lógico, planner, búsqueda exacta e
híbrida, compilación de contexto, servicio, evaluación y CLI. No crea una base
Knowledge paralela. Inventario, catálogo y semantic poseen contratos
generacionales; el grafo de código sigue en schema 2 y una transacción global de
finalización.

La capa cognitiva no autoriza mutaciones. Las relaciones unresolved no deben
fabricar aristas y las inferencias no sustituyen evidencia.

## 13. Línea base

| Barrera basal | Resultado verificado |
|---|---|
| Suite monolítica, defaults | 1,543 passed + 78 subtests |
| Statements | 34,790 |
| Statements cubiertos | 29,712 |
| Branches | 11,002 |
| Branches cubiertas | 7,704 |
| Cobertura combinada exacta | 81.70859538784067 % |
| Cobertura redondeada | 82 % |
| Ruff | limpio |
| mypy | 202 módulos, limpio |
| `pip check` | limpio |
| Wheel/sdist basal | construidos e inspeccionados |
| Venv limpio `--no-deps` | version/help aprobados; ruta operacional bloqueada por `xxhash` ausente |
| Closure hermética local | bloqueada: wheel de `faster-whisper` ausente |

Estas cifras son línea base de la campaña, no barrera final.

## 14. Instrucciones AGENTS

El repositorio contiene `<REPO>/AGENTS.md` como constitución estándar y un
archivo legacy convertido en referencia no contradictoria. `AGENTS.md` conserva
la línea base recibida 0.7.0 y los invariantes de identidad, publicación,
SQLite, evidencia, seguridad y no mutación; la fuente integrada declara 0.7.1.
La versión viva de PowerShell es 7.6.4.

**Inclusión de AGENTS/pointer en sdist y ZIP:**
`AGENTS.md canónico y el pointer legacy no contradictorio están incluidos conforme al MANIFEST; el sdist terminal y su validator aprobaron la inclusión esperada. Resta comprobar su presencia y unicidad en el ZIP de auditoría, que aún no se ha compuesto.`.

## 15. Ejecución integral basal

Runs 61/62 fueron intentos históricos: el primero quedó `interrupted` y el
segundo reanudó código sobre scan 25 antes del hardening final. Ya no están
activos y no se presentan como corrida integral. Runs 63/66/68/69 cubrieron las
rutas de forma segmentada y run 70 ejecutó la segunda incremental integrada.

**Clasificación final (monolítica/segmentada/bloqueada):**
`Cobertura integral segmentada en runs 63/66/68/69 y segunda corrida incremental integrada route-only en run 70. Las seis rutas de run 70 quedaron completed; el proceso terminó completed_with_issues/strict exit 2 por estados cacheados.`.

## 16. Comando exacto

Los comandos sanitizados de los intentos operativos comprobados son:

```text
py -3 -B -m neocortex --root <CORPUS> --state-directory <STATE> --all
  --global-memory-budget-mb 3072 --global-min-free-memory-mb 4096
  --global-min-free-commit-mb 4096 --global-cpu-slots 2
  --global-max-cpu-load-percent 72 --pdf-workers 1 --ocr-workers 1
  --pdf-large-document-workers 1 --image-workers 1
  --audio-model-cache <MODELS> --audio-local-models-only --strict-exit-codes

py -3 -B -m neocortex --root <CORPUS> --state-directory <STATE>
  --route code --resume-run 61
  --global-memory-budget-mb 3072 --global-min-free-memory-mb 4096
  --global-min-free-commit-mb 4096 --global-cpu-slots 2
  --global-max-cpu-load-percent 72 --pdf-workers 1 --ocr-workers 1
  --pdf-large-document-workers 1 --image-workers 1
  --audio-model-cache <MODELS> --audio-local-models-only --strict-exit-codes
```

Los comandos terminales posteriores quedaron resumidos en
`Runs 63-70 usaron raíces sanitizadas, sin --apply y audio local-only. Semantic ejecutó offline el staging text-quality para pdf/docx/xlsx/pptx/odt/audio/code; terminó las fuentes seleccionadas, pero la campaña sólo alcanzó 1/5 fases y falló antes de inferencia/publicación. Knowledge ejecutó status, probes, context, prompt injection, evaluación real de 50 y golden sintético read-only/offline. El gate terminal serial ejecutó compileall, Ruff, mypy, pip check, pytest+Coverage, golden, build, validator, venv y entrypoints; su evidencia está en el manifiesto del gate terminal de 20:44 UTC. El análisis SQLite reutilizó la copia completa y publicó sus manifests sin VACUUM ni checkpoint. La validación del ZIP se registra post-build para evitar circularidad en la evidencia embebida.`. Ningún comando comprobado contiene
`--apply`.

## 17. Perfil de recursos

El perfil efectivo usó 3,072 MiB de presupuesto global; pisos de 4,096 MiB de
memoria física y commit; dos slots CPU; techo objetivo de 72 %; un worker PDF,
OCR, PDF grande e imagen; cache local de audio y `--audio-local-models-only`.
Las rutas pesadas se serializaron. Cualquier cambio posterior de perfil debe
quedar en `Run 63 mantuvo piso de commit 4 GiB y bloqueó code/PDF/image; las ejecuciones segmentadas redujeron el piso de commit según evidencia. Run 68 usó dos PDF workers, OCR 1, large-document 1 y presupuesto PDF 2.7 GB. Documentar razones sin presentar perfiles distintos como benchmark directo.`.

## 18. Resultados por ruta

Los campos conservan la semántica propia de cada owner; un error cacheado no se
normaliza como error nuevo ni `completed` significa “sin incidencias”.

| Ruta | Resultado histórico/pre-fix | Estado terminal |
|---|---|---|
| Inventario/dedup | run 61: scan 25, inventario full, un intento | `Run 63/scan 26 completó 110,421 archivos, 10,321 directorios y 108,200,976,422 bytes; 39,906 candidatos, 14 links omitidos, 3 directorios excluidos y 0 errores. Dedup: 4 grupos, 5 redundantes, 8,762 bytes; sólo plan, sin apply.` |
| PDF | run 61 interrumpido durante extracción | `Run 70 completó 7,515 PDF en 5,213.9689496 s: 7,511 cache hits, 4 reintentos/extraídos, 3 timeouts, 3 parciales, 115 páginas OCR y 27 esperas de memoria. Fases exactas: extraction 3,820.4509425 s; text_dedup 13.7448253 s; derived 1,358.7742499 s; catalog 18.9298422 s. Derived indexó 500 páginas FTS, construyó 1 perfil y registró 26 errores de perfil.` |
| DOCX | 2,540 procesados; 1,749 cache hits; 786 nuevos; 5 errores cacheados; 2 parciales cacheados | `Run 70 completó 2,540 candidatos: 2,535 cache hits, 5 errores cacheados, 2 parciales cacheados, 0 documentos nuevos y 0 extraídos.` |
| Office | 981 procesados/cacheados; 0 nuevos; 1 error cacheado | `Run 70 completó 981 candidatos/cache hits, 1 error cacheado y 0 extracciones nuevas.` |
| Audio | 912 procesados; 491 transcritos; 290 sin voz; 131 errores | `Run 70 completó 912/912 desde cache, 131 errores cacheados, cero transcripciones nuevas y local_models_only=true; ffprobe siguió no disponible para trabajo nuevo.` |
| Imagen | run 61 interrumpido | `Run 70 completó 26,480 candidatos en 39.9943062 s: 26,458 cache hits, 22 errores cacheados, 0 clasificaciones nuevas y 0 OCR nuevos.` |
| Código | runs 61/62: intentos históricos pre-fix, no usados como cierre | `Run 70 completó 60,405 candidatos, todos cache hits, 0 procesados, 5 errores persistentes y bytes_read=0. El fast path v3 publicó 4 proyectos y graph_milliseconds=71; el recorrido de analysis aun consumió 2,526.1578692 s.` |
| Catálogo | publicación parcial derivada de rutas completas; falta consolidación transversal | `Run 70 completó catálogo para audio, DOCX, Office y PDF, pero volvió a publicar catalog_run_id 389-394 sobre fuentes casi totalmente cacheadas; PDF clasificó 1 de 7,480. La publicación innecesaria permanece como defecto NC-AUD-055.` |
| Semantic/publicación | staging textual completado; inferencia/publicación no completadas | `Intento post-fix semantic-campaign-20260727T122658726Z-179cd480e3d0: failed tras 8,060.913785 s en text_quality_first; cleanup_verified=true, Job vacío y proceso reaped. Terminó el staging de todas las fuentes seleccionadas: 63,749 active items, de los cuales 52,267 son code con chunks; 60,405 corresponde al cache total de la ruta code y no es el denominador Semantic. Dejó 5,133,824 active chunks/jobs pending, 0 embedded chunks y 0 published heads. Sólo alcanzó 1/5 fases y no ejecutó inferencia/publicación; image, repeat y status tampoco corrieron. semantic.sqlite3 llegó a 18,306,666,496 bytes; el proceso acumuló 605,820,263,841 bytes leídos y 535,755,803,087 escritos. No descargó modelos ni mutó el corpus.` |

## 19. Inventario y duplicados

Totales de `<CORPUS>`, bytes, formatos, tamaños, inaccesibles, duplicados
exactos, near-duplicates, corruptos, vacíos, hard links, reparse y churn:
`Scan 26 demostró 110,421 archivos, 10,321 directorios y 108,200,976,422 bytes; dedup planificó 4 grupos, 5 redundantes y 8,762 bytes sin aplicar acciones. El análisis terminal de la copia quedó completo, pero con calidad partial/degraded: la distribución de formatos alcanzó el límite de 100 grupos y quedó truncada; special_files no es derivable de schema 7. No se inventa near-duplicate, corrupción ni elegibilidad de borrado.`.

Ninguna detección se convierte en autorización de borrado o movimiento.

## 20. Extracción por formato

Los resultados estratificados de PDF, DOCX, Office, audio, imagen y código
incluirán candidatos, cache, updates, parciales, errores, OCR, locators,
extractor/firma, reanudación y recursos. No se copiarán contenidos sensibles.

**Resultado estratificado terminal:** `Run 70 demostró estado incremental: audio 912 cache; DOCX 2,535/2,540 cache; Office 981/981 cache; imagen 26,458/26,480 cache; código 60,405/60,405 cache; PDF 7,511/7,515 cache con 4 reintentos. Semantic seleccionó y staged 52,267 fuentes code que sí tenían chunks, 7,480 pdf, 2,533 docx, 967 xlsx, 491 audio, 10 pptx y 1 odt, total 63,749. Ese staging terminó, pero la campaña no ejecutó inferencia/publicación: 5,133,824 jobs pending, cero embeddings y ningún head.`.

## 21. Catálogo y clasificación

Tipos, dominios, confianza, review/partial/error, evidencia, revisiones,
superseded, propuestas de organización y falsos positivos observables:
`La proyección read-only terminal observó 11,486 documentos activos: 9,012 en estados clasificados y 2,474 en review, repartidos en 23 generaciones y seis source kinds. Integridad y FK quedaron limpias. Las generaciones all-cache y publicaciones sin trabajo material siguen como defecto de churn; ninguna propuesta de organización se aplicó y las métricas no sustituyen revisión humana de falsos positivos.`.

Los planes de organización no se aplican.

## 22. Semantic y embeddings

Modelos y firmas, chunks por owner/formato, cobertura, ausencias, reutilización,
tamaños, duplicación, cutoff, p50/p95, I/O, stale y modalidades:
`Semantic schema 6 conserva generation 1 building con el staging de todas las fuentes seleccionadas terminado: 52,267 code con chunks, 7,480 pdf, 2,533 docx, 967 xlsx, 491 audio, 10 pptx y 1 odt, total 63,749 active items. Los 60,405 elementos de la ruta code incluyen errores/entradas sin chunks y no son un denominador de staging. Quedaron 5,133,824 active chunks/jobs pending, 0 done/error/leased, 0 embedded chunks y 0 heads. La campaña sólo alcanzó 1/5 fases y murió antes de inferencia/publicación tras 8,060.913785 s y más de 1.14 TB de I/O acumulado. Fixed-point reuse devuelve leases exactos ante BaseException; 6 focales, matriz de 17 archivos/180 pruebas, Ruff y mypy aprobaron con revisión GO. El benchmark quality a 4 threads midió 4.309850749843586 textos/s y proyectó 212.6910465982922 h para 3.3 M claves únicas; excluye SQLite, leases y publicación y no es ETA operativa. No existe cobertura vectorial publicada.`.

Espacios incompatibles permanecen separados y los embeddings no autorizan
acciones.

## 23. Code graph

El schema observado es code 2. `finalize_graph` mantiene
atomicidad mediante una transacción global y carece de contrato generacional
completo. Run 61 reprodujo un conflicto `UNIQUE` en bindings de asignaciones
Python; la corrección limita símbolos a bindings reales, deduplica nombres y
eleva la firma del analizador. Run 62 quedó como intento histórico; runs 66/70
aportan la revalidación operacional terminal del resolver y su fast path.

Antes de reanudar, `code.sqlite3` ocupaba `2,237,329,408` bytes. Durante run 62
se observaron `13,451,091,968` bytes en la base principal y `57,119,712` bytes
en WAL. Es evidencia de escala de NC-AUD-015, no atribución terminal de bloat:
faltan el cierre del writer y la descomposición read-only por payload, historial,
FTS y páginas reutilizables.

NC-AUD-042 reprodujo además una actualización FTS en cache hits sin cambio de
ruta. El cache hit estable ya no ejecuta ese DML. El contrato posterior hace que
un cambio de path deje de ser cache hit: invalida la versión anterior, publica
una sucesora y reconstruye membresías/FTS; por ello reemplaza la formulación
anterior de actualización in-place. Run 62 arrancó antes de estos cambios y no
es medición post-fix.

El slice posterior conserva schema 2 y añade un fence tipado en `metadata`, key
`code_graph_completion_v3`, con `schema_version=1`, el `analysis_run_id` dueño y
`resolver_signature=code-graph-resolver-v1`. Una base existente sin fence hace
una finalización completa una vez. Sólo una corrida full estable, con todos los
candidatos reutilizados, cero procesados/invalidados y el fence exacto de una
corrida completa, puede omitir `finalize_graph`; una selección parcial, un
cambio, una ausencia o un fence incompatible fuerzan reconstrucción.

La reconciliación full persiste también generated/vendored excluidos como
observaciones acotadas y ejecuta `mark_missing` antes de decidir reuso o rebuild.
La publicación terminal de run y fence es una sola transición CAS; cancelación
o fallo no avanzan el fence. El rebuild elimina derivados actuales y vuelve a
calcular memberships, proyectos y etiquetas FTS, preservando la evidencia
histórica invalidada.

Resultados reales de lenguajes, símbolos, relaciones, conflictos, duración,
WAL, estabilidad y barrera final de NC-AUD-038:
`Code schema 2 conserva su riesgo no generacional, pero run 70 probó el fast path code-graph-resolver-v3: 60,405 cache hits, 0 procesados, bytes_read=0 y graph 71 ms. La fase analysis completa siguió costando 2,526.1578692 s, por lo que no se declara costo incremental óptimo.`.

## 24. Knowledge Plane real

`knowledge status`, `knowledge search` y `knowledge context` se ejecutaron
contra el estado generado, midiendo exact/lexical, código, no-answer,
partial/cutoff, locators, presupuesto y frontera de contenido no confiable. La
ausencia de head Semantic impidió certificar recuperación vectorial. **Resultado
real:** `Knowledge real completó 50 casos contra snapshot estable de 10 owners en modo degraded_no_semantic_head: 30 deterministic_from_source, 17 agent_provisional, 3 unverified y 0 human_verified. En la barrera determinista recall@10=0.8333333333333334, MRR=0.6986111111111111, nDCG@10=0.7315464876785729, evidence coverage=0.8 y citation truth precision=0.7628205128205128; búsqueda p50=3,371 ms/p95=14,213 ms, 2,510 filas y 0 vectores. Exact/lexical/context y tres carriers de prompt injection completaron, pero la evaluación no mide recuperación semántica publicada.`.

## 25. Segunda corrida incremental

La segunda corrida comparable medirá cache hits, trabajo evitado, reaperturas,
embeddings reutilizados, publicaciones innecesarias, IDs, revisiones, tiempos,
recursos, errores y residuos.

Run 62 es una reanudación, no una segunda corrida estable. **Resultado e
idempotencia:** `Demostrada por run 70 sobre candidate run 69: las seis rutas completed, duración total 5,220.0532761 s, strict exit 2/completed_with_issues, cleanup=true, sin --apply y audio local-only. La estabilidad funcional y el fast path code quedaron demostrados, pero el no-op fue caro: PDF consumió 5,213.9689496 s y code analysis 2,526.1578692 s en ejecución solapada.`.

## 26. Golden sintético

La línea base sintética de 17 casos verificó:

| Métrica | Valor basal |
|---|---:|
| Recall@10 | 0.9102564102564104 |
| MRR | 0.9615384615384616 |
| nDCG@10 | 0.9356412992914329 |
| Cobertura de evidencia | 18/21 |
| Precisión de cita | 18/19 |
| Precisión de locator | 18/18 |

Los candidatos/rankings controlados no demuestran calidad real. El rerun
terminal posterior a todos los cambios aprobó la barrera sintética:
`Rerun final aprobado: 17/17 escenarios, gate_passed=true, recall@k 0.9102564102564104, MRR 0.9615384615384616, nDCG 0.9356412992914329, evidence coverage 18/21, citation precision 18/19 y locator precision 1.0. Sigue siendo un fixture scripted y no sustituye calidad del corpus.`.

## 27. Golden real

El conjunto real separó `human_verified`, `deterministic_from_source`,
`agent_provisional` y `unverified`. La evaluación terminal sobre el corpus sí
existe y completó 50 casos:
`Evaluación completada sobre 50 casos: barrera determinista de 30 con recall@10=0.8333333333333334, MRR=0.6986111111111111 y nDCG=0.7315464876785729; 17 casos agent_provisional obtuvieron recall@10=0.7142857142857143 y no-answer accuracy=0.0 sobre 3 casos. Los 3 unverified y la ausencia de human_verified no cuentan como promoción. Cero vectores fueron consultados; el resultado caracteriza el fallback degradado, no un Knowledge híbrido completo.`.

Lo pendiente no es crear una evaluación real adicional, sino incorporar casos
`human_verified`, evidencia multihop y recuperación híbrida después de publicar
un head Semantic. Las etiquetas provisionales no cuentan como aprobación humana.

## 28. Análisis de fallos

Clases reproducidas y disposición terminal:

1. una ruta de código podía emitir dos símbolos incompatibles para una misma
   asignación y provocar una violación `UNIQUE`; corregido y en integración real;
2. el binding temprano `run→scan` podía preceder la persistencia completa de
   candidatos y dejar una frontera de reanudación no atómica; corregido en
   código/focales y revalidado en la cadena operacional 63–70, incluida la
   corrida incremental 70;
3. subprocess controlado podía cerrar sólo el hijo directo y dejar nietos;
   corregido para procesos acotados, con la brecha GUI separada en NC-AUD-037;
4. ContextBundle no declaraba de forma suficientemente explícita que corpus,
   OCR, metadatos y relaciones son evidencia no confiable; corregido con
   `untrusted-corpus-data-v1` y regresiones de siete carriers;
5. el arnés externo podía perder drenaje, sobrerredactar estructura o atribuir
   incorrectamente un run; hardening externo parcial, sin rehabilitar
   retroactivamente los manifests de runs 61/62;
6. cache hits de código reescribían FTS aunque la ruta no cambiara; corregido
   eliminando DML en el hit estable y publicando una versión sucesora ante
   cambio de path;
7. una corrida estable siempre repetía `finalize_graph` y reconciliaba missing
   demasiado tarde; corregido con fence tipado, orden `mark_missing→graph` y
   fast path fail-closed;
8. cambios de manifest/path podían dejar memberships y etiquetas FTS derivadas
   obsoletas; corregido con reset/rebuild de derivados actuales y preservación
   de evidencia histórica;
9. la terminación del analysis run y la vigencia del grafo no compartían un CAS
   y fence cooperativamente cancelable; corregido con publicación atómica y
   lifecycle running/completed/cancelled/failed;
10. la disponibilidad runtime de un analizador opcional podía reutilizar un
    fallback bajo la misma firma declarada; corregido comparando la identidad
    real del analizador que ejecutaría;
11. los cache hits ocultaban contadores persistentes de partial, error,
    text-only y skips; corregido proyectando el estado durable cacheado.

Frecuencia, severidad operativa y comparación antes/después:
`Run 70 confirmó graph fast path en 71 ms, pero analysis code aún costó 2,526.1578692 s; PDF y catálogo conservaron trabajo no-op. Semantic terminó el staging de todas las fuentes seleccionadas: 52,267 code con chunks —no 52,267/60,405; 60,405 es el total de cache de la ruta code e incluye entradas sin chunks o con error— y 11,482 fuentes de otros owners. La frontera dominante quedó después del staging: 5,133,824 jobs, DB de 18.3067 GB, 605.8 GB leídos/535.8 GB escritos, cero inferencias publicadas y sólo 1/5 fases. Batching cerró la explosión de conexiones y fixed-point reuse evita inferencia repetida entre batches, pero ni la transacción por resultado ni la escala de 5.13 M jobs quedan resueltas. Knowledge sin vectores recuperó 83.33 % @10 de la barrera, pero citation truth fue 76.28 %, todos los casos observaron cutoff y no-answer provisional fue 0 %. El primer gate detectó 15 filenames virtuales PySide6/shiboken; la corrección exacta quedó revalidada en el gate terminal completo.`.

## 29. Hallazgos corregidos

### Revalidación histórica NC-AUD-001..035

La revalidación combinó fuente viva y regresiones específicas; una suite amplia
no se usó por sí sola para promover cada ID.

| ID | Título abreviado | Estado de campaña | Evidencia principal |
|---|---|---|---|
| NC-AUD-001 | Reasignación entre generaciones de inventario | corregido y revalidado | PK por scan/path y pruebas de aislamiento/publicación |
| NC-AUD-002 | Scan con errores publicado como completo | corregido y revalidado | persistencia `partial` sin publicación |
| NC-AUD-003 | Cursor USN ante rutas ambiguas | corregido y revalidado | reconcile ambiguo y checkpoint atómico |
| NC-AUD-004 | Rollback ante `BaseException` | corregido y revalidado | schema/framework rollback y cierre |
| NC-AUD-005 | Carrera de lease semántico | corregido y revalidado | reclaim/heartbeat atómicos |
| NC-AUD-006 | Caché PDF sin owner | corregido y revalidado | eliminación completa del owner obsoleto |
| NC-AUD-007 | Preparación/proyección GUI inconsistente | corregido y revalidado | suites UI request/worker/status/smoke |
| NC-AUD-008 | Validación SQLite superficial | corregido y revalidado | contratos de schema por owner |
| NC-AUD-009 | Persistencia/coordinación de código | corregido y revalidado; escala en 015 | persistencia, cache signature, selección y recursos |
| NC-AUD-010 | TOCTOU en mutaciones por ruta | corregido y revalidado | handles Windows; casos no soportados abstienen |
| NC-AUD-011 | Lifecycle de recovery incierto | corregido parcialmente | `status`/`record`; faltan decide/authorize/recover/verify |
| NC-AUD-012 | Visibilidad semántica no publicada | corregido y revalidado | building/partial/CAS loser invisibles |
| NC-AUD-013 | Catálogo visible antes de publicar | corregido y revalidado | publicación generacional de catálogo |
| NC-AUD-014 | Trabajo/retención no acotados | corregido parcialmente | planner read-only; faltan apply/journal/cuotas/lifecycle |
| NC-AUD-015 | `finalize_graph` en transacción global | pendiente reproducido operacionalmente | code schema 2 y escala observada en run 62 |
| NC-AUD-016 | Importaciones pesadas en rutas ligeras | pendiente por evidencia estructural | Rich ávido y dependencias base pesadas |
| NC-AUD-017 | Política SQLite/FK desigual | corregido y revalidado para callsites oficiales | 40 callsites/24 módulos; readers Knowledge query-only |
| NC-AUD-018 | Migración de catálogo podía perder historial | corregido y revalidado | legado poblado, objetos desconocidos y `BaseException` |
| NC-AUD-019 | N+1/snapshots en status semantic | corregido y revalidado | una conexión/snapshot |
| NC-AUD-020 | Watcher sin lease de vida | corregido y revalidado | watcher life lease |
| NC-AUD-021 | Licencia/NOTICE propios ausentes | corregido parcialmente; decisión humana | inventario mejorado; no existen LICENSE/NOTICE ni licencia propia |
| NC-AUD-022 | Fixture simulaba downgrade | corregido y revalidado | fixture legado real |
| NC-AUD-023 | Path histórico obsoleto en búsqueda | corregido y revalidado | resolución conserva locator/current path |
| NC-AUD-024 | Resolver confiaba en vector space del caller | corregido y revalidado | rechazo de identidad/espacio incompatible |
| NC-AUD-025 | Fallback temporal fuera del laboratorio | corregido y revalidado | guard de destinos e identidad del lab |
| NC-AUD-026 | Metadatos/licencias de terceros contradictorios | pendiente reproducido/humano | NudeNet, PyMuPDF/Qt, modelos y assets sin resolución jurídica |
| NC-AUD-027 | Abandono prefrontera como efecto incierto | corregido y revalidado | intent sin efecto no se clasifica uncertain |
| NC-AUD-028 | Recibo de Papelera asociable a otra acción | corregido y revalidado | recibo ligado a fuente exacta |
| NC-AUD-029 | Repetición contradictoria aceptada | corregido y revalidado | transición terminal contradictoria rechazada |
| NC-AUD-030 | Recovery interpretaba schema futuro | corregido y revalidado | reader rechaza framework futuro |
| NC-AUD-031 | Lab no retenía identidad de subraíces | corregido y revalidado | reemplazo en mismo path rechazado |
| NC-AUD-032 | Evidencia semantic no retenía generación | corregido y revalidado | hold `referenced_by_semantic_evidence` |
| NC-AUD-033 | Último run válido elegible para poda | corregido y revalidado | hold `last_completed_run` |
| NC-AUD-034 | Poda podía perder rollback/cross-store | corregido y revalidado en consumidores inspeccionados | holds y regresiones de prune |
| NC-AUD-035 | Cancelación SQL escapaba como `OperationalError` | corregido y revalidado | traducción a cancelación de dominio |

### Hallazgos reservados de esta campaña

| ID | Título | Estado terminal | Evidencia principal |
|---|---|---|---|
| NC-AUD-036 | Contención de descendientes de subprocess | corregido y revalidado | Job kill-on-close; nieto, timeout, overflow, excepciones, rollback y reap |
| NC-AUD-037 | Contención de hijos GUI/QProcess | pendiente por evidencia estructural | QProcess/ShellExecuteEx sin árbol Job demostrado |
| NC-AUD-038 | Binding Python duplicado causa `UNIQUE` | corregido y revalidado; operación terminal por cerrar | bindings reales, deduplicación, analyzer v2; `corregido y revalidado operacionalmente: run 66 completó análisis/graph sin repetir el UNIQUE de bindings` |
| NC-AUD-039 | Snapshot de routing publicado antes de completar candidatos | corregido parcialmente | publicación/CAS/recovery atómicos y focales; `publicación atómica validada en la cadena scan 26→runs 63/66/68/69; no obstante, run 63 perdió candidatos retenidos para reanudar PDF y abre NC-AUD-054` |
| NC-AUD-040 | ContextBundle sin frontera explícita de prompt injection | corregido parcialmente | marker versionado y siete carriers; `regresiones de siete carriers y tres contextos reales de prompt injection aprobaron la frontera untrusted-corpus-data-v1; permanece corregido parcialmente porque no se probó obediencia de un LLM ni un host MCP capaz de cambiar su plan de herramientas` |
| NC-AUD-041 | Integridad/atribución del RunManifest externo | corregido parcialmente | hardening y accounting del Job; gate LAB 94+3, estática estricta limpia; manifests anteriores no rehabilitados; `arnés usado en runs 63-69 con asociación durable; cleanup observado aprobado salvo run 68, cuya proyección retuvo 2,048 de 2,092 procesos y no permite certificación exhaustiva` |
| NC-AUD-042 | Cache hit de código reescribía FTS sin cambio de path | corregido en focales; operación post-fix pendiente | SQL-trace 1→0 en hit estable; cambio de path publica sucesora; `corregido por trace focal (1 UPDATE→0 en cache hit estable) y run 66 all-cache; no confundir con el defecto FTS de graph corregido aparte en v3` |
| NC-AUD-043 | Corrida estable siempre finalizaba graph y reconciliaba missing después | corregido y revalidado operacionalmente | fence `code-graph-resolver-v1`, `mark_missing` previo y full reconciliation; `corregido y revalidado operacionalmente: run 70 aceptó el fence v3 estable, reutilizó 60,405 resultados y completó graph en 71 ms sin lecturas de payload` |
| NC-AUD-044 | Memberships/FTS derivados podían quedar stale ante manifest/path changes | corregido y revalidado operacionalmente | versión sucesora, reset/rebuild derivado y proyectos históricos; `run 66 completó reconciliación/memberships/proyectos/FTS y publicó terminalmente; schema 2 y falta de generación siguen abiertos` |
| NC-AUD-045 | Terminal CAS/cancelación/lifecycle de analysis run y fence | corregido y revalidado focalmente | publicación atómica; owner running exacto; cancel/fail no avanzan fence |
| NC-AUD-046 | Disponibilidad runtime podía reutilizar fallback bajo firma declarada | corregido en focales; costo residual | identidad real analyzer/fallback se valida; firma global aún invalida otros lenguajes |
| NC-AUD-047 | Cache hits ocultaban contadores persistentes incompletos | corregido y revalidado operacionalmente | partial/error/text-only/binary/skip y evidencia durable; `corregido y revalidado: run 70 conservó contadores de 60,405 cache hits, 11,151 partial, 18,613 text_only, 7,770 binary, 362 skipped_limit y 5 error` |
| NC-AUD-048 | Race Windows en fixtures de readiness | corregido y revalidado | test nuevo de subprocess y fixture watcher preexistente; reproducción 2/10 y gate con 2 fallos; temp+replace, polling parseable y `finally`; stress 50/50, matriz 423 |
| NC-AUD-049 | sdist omitía informe fechado y helpers de tests | corregido y revalidado; rebuild terminal post-informe separado | validator estricto reprodujo; MANIFEST corregido; rebuild provisional aprobado |

En NC-AUD-041 se conservan como evidencia las corridas fallidas: el primer
smoke invocó por error el launcher global 0.3.0 y la primera revisión expuso
fallos mypy y una carrera de accounting cuando el root transitorio salía antes
de observarse. El hardening posterior pasó 94 pruebas más tres gates; Ruff,
mypy strict, compile y help quedaron limpios. Esto mejora el arnés, pero no
reescribe la procedencia de manifests anteriores.

## 30. Evolución implementada

Cambios implementados: contención Windows de subprocess,
normalización/deduplicación de bindings Python, publicación atómica del snapshot
de routing, frontera de confianza versionada y supresión de DML FTS en cache
hits sin cambio de path. El slice de código añade reconciliación full honesta,
versión sucesora ante cambio de path, rebuild de derivados actuales, fence
tipado, fast path estable fail-closed, terminal CAS y contadores cacheados
completos. Son evoluciones elegidas por defectos reproducidos, no por intuición.
La estabilización de readiness Windows y el validator estricto de sdist cierran
fallos de prueba/distribución; no se presentan como nuevas capacidades del
pipeline cognitivo.

**Promoción después de integración terminal:** `Promover resolver/FTS v3, ownership de generators y staging Semantic por sesión/lotes: tienen regresiones y evidencia operacional, aunque Semantic no publicó head. Promover también fixed-point cache reuse por 6 focales, matriz Semantic exacta de 17 archivos/180 pruebas, Ruff/mypy y revisión GO; conserva schema/API y deja same-batch/cross-worker coalescing fuera de alcance. El speedup staging 57.2388x es sintético y el benchmark del modelo demuestra que 3.3 M únicas requieren ~212.691 h incluso a 4 threads; no se afirma mejora viva end-to-end. La corrección Coverage para 15 paths virtuales quedó aprobada en el gate terminal completo. No promover GraphRAG, MCP, feedback ni ANN.`.

## 31. Grafo transversal o decisión de aplazar

**Decisión: aplazado.** Code schema 2 no posee generación/head/CAS reanudable.
La evaluación real de 50 casos ya existe, pero fue exact/lexical degradada: no
hubo head Semantic, vectores, casos `human_verified` ni evidencia multihop
suficiente para demostrar mejora de un grafo transversal. Añadir otro owner
duplicaría riesgo antes de estabilizar publicación generacional y locators.

## 32. MCP o decisión de aplazar

La API Python/JSON/CLI read-only sí fue ejercitada sobre el estado real mediante
Knowledge status, search, context y la evaluación degradada de 50 casos. Esa
evidencia no incluye recuperación híbrida porque Semantic no publicó head.

**Decisión: aplazado.** No se autorizó descargar un SDK MCP, no existe closure
local demostrada del SDK y la closure hermética general sigue incompleta. MCP
debe esperar un head Semantic estable y pruebas de protocolo; no se habilitará
SQL arbitrario.

## 33. Feedback/mejora controlada

No existe autorización para convertir feedback o inferencias en mutaciones del
corpus. Cualquier persistencia futura debe ser append-only, explícita y separada
de índices derivados, con promoción humana.

**Decisión: no implementado y aplazado.** El evaluador externo no constituye un
store de feedback durable ni promoción humana.

## 34. Escala vectorial

No se añade ANN sin medir cantidad de vectores, p50/p95, CPU, memoria, I/O,
cutoff, publicación y recall frente a búsqueda exacta. La exacta debe conservarse
como oracle/fallback.

**Decisión: aplazado.** No se demostró un cutoff o cuello de botella de búsqueda
exacta ni un beneficio de recall; la puerta ANN no se superó.

## 35. Recovery y retention pendientes

Estado histórico revalidable:

- NC-AUD-011: `status` y `record` existen; faltan `decide`, `authorize`,
  `recover` y `verify`.
- NC-AUD-014: el planner read-only existe; faltan apply, journal, cuotas,
  lifecycle y poda global segura.
- NC-AUD-015: code schema 2 mantiene transacción global y carece de reanudación
  generacional.

Estados de campaña: NC-AUD-011 y 014, corregidos parcialmente; NC-AUD-015,
pendiente reproducido operacionalmente. No se ejecutaron recovery físico ni
retención aplicable sobre estado vivo.

## 36. SQLite

Los schemas basales son inventario 7, framework 19, catálogo 6, semantic 6 y
code 2. Antes de reanudación se obtuvieron backups online de nueve bases; las
copias verificaron integridad y cero violaciones FK. El total de fuente de esas
copias fue aproximadamente 5.513 GB. Esta cifra no contiene ni publica las
bases.

Integridad/FK final por owner, factories tocadas, query-only, timeouts,
cancelación y cierre: `El backup/copia terminal de 10 owners quedó completo con 35,428,950,016 bytes. Los 10 schemas abrieron read-only/query_only con versiones esperadas, integrity_check limpio y foreign_key_check sin violaciones. Semantic schema 6 confirmó 63,749 active items, 5,133,824 active chunks/jobs pending, 0 embedded y 0 heads. El análisis terminó, pero su calidad es partial/degraded: agregados Semantic chunks y chunk_sizes agotaron 900 s/10,000,000,000 VM steps, la distribución de formatos de inventario quedó truncada y code graph timing/inventory special_files no existen estructuralmente. No se ejecutó VACUUM ni checkpoint.`.

## 37. Migraciones

No se introdujo migración ni DDL nuevo. Los cambios mantienen inventory 7,
framework 19, catalog 6, semantic 6 y code 2. Las migraciones existentes se
ensayaron sobre copias, nunca sobre la base viva. Cualquier cambio futuro debe
probar creación, cada versión soportada, base poblada, objetos desconocidos,
schema futuro/corrupto, rollback e idempotencia sobre copia, nunca sobre la base
viva sin preflight/backup.

**Matriz terminal de migraciones existentes:** `No se introdujo DDL: inventario 7, framework 19, catálogo 6, semantic 6 y code 2 fueron observados en la copia. El backup terminal de 10 owners sumó 35,428,950,016 bytes, abrió read-only/query_only y aprobó integrity_check y foreign_key_check; los tests de migración existentes aprobaron dentro de la suite completa. No se ejecutó VACUUM, checkpoint destructivo ni migración sobre bases vivas.`.

## 38. Concurrencia

Las escrituras SQLite, migraciones, builds, Coverage y rutas pesadas se
serializan. Los agentes no comparten archivos de escritura. Los casos reader/
writer, leases, fencing, CAS y límite de agentes se consolidarán en la barrera.

**Resultado terminal de concurrencia:** `CAS, fencing y readiness tienen focales aprobadas. Semantic staging probó rollback y liberación del write lock por commit; fixed-point probó retry de leases ante fallo sistémico/KeyboardInterrupt, no publicación parcial y matriz de 180. Knowledge adquirió/liberó el writer guard y ejecutó 17 Jobs con cleanup. La suite terminal del árbol congelado aprobó. Tras la instrucción se respetó máximo ocho agentes, aunque antes se observaron catorce totales.`.

## 39. Cancelación

Los contratos requieren cancelación cooperativa, timeout, espera/reap,
clasificación de causa y liberación de handles. SQLite debe conservar la
excepción original salvo traducción explícita del token cancelado.

Casos `KeyboardInterrupt`, `RuntimeError`, `BaseException`, timeout y proceso
que ignora cierre: `Bounded subprocess/Job aprobó timeout y excepciones; runs 62/64/65/67 y el intento Semantic legacy requirieron cierre exacto por falta de cancelación cooperativa. El intento post-fix falló con Job vacío/reap verificado. Fixed-point devuelve sólo leases propios a pending ante BaseException y conserva KeyboardInterrupt sin head parcial. La suite terminal completa aprobó estas regresiones, pero esto no cierra cancelación del backend ni de todas las rutas; NC-AUD-058 permanece abierto.`.

## 40. Process containment

NC-AUD-036 cubre subprocess/descendientes y NC-AUD-037 reserva la brecha GUI.
La implementación usa Job Objects exactos, creación suspendida/asociación previa
a reanudar cuando aplica y cierre de pipes. Las focales cubren nieto, timeout,
overflow, excepciones, rollback incompleto, close/reap y cierre idempotente del
Job. PID reuse, crash de sesión y la ruta GUI se separan como barreras de cierre.

El arnés externo corrigió además el accounting del Job cuando el root transitorio
termina antes del primer muestreo. La corrida fallida inicial y su posterior gate
94+3 permanecen registradas bajo NC-AUD-041, no como hallazgo de producto.

**Resultado Windows final:** `Fronteras isolated/bounded corregidas; GUI/QProcess sigue abierta. Runs 63/66/69/70, el fallo Semantic post-fix, 17 invocaciones Knowledge y el gate terminal terminaron sus procesos controlados con reap/cleanup en sus alcances; run 68 sólo admite identidad individual acotada. El manifiesto global de cierre aún debe confirmar cero residuales antes del handoff.`.

## 41. Seguridad y prompt injection

PDF, Office, OCR, audio, imágenes, código, comentarios, paths, metadatos y
resultados de grafo son evidencia no confiable y no tienen autoridad para
autorizar acciones. ContextBundle antepone
`untrusted-corpus-data-v1` con `instruction_authority=false`,
`tools_authorized=false` y `actions_authorized=false`, sin ocultar el payload.

La regresión parametrizada cubre PDF, DOCX, OCR, código, catálogo, path y grafo.
Demuestra serialización y autoridad declarada; no pretende demostrar el
comportamiento universal de un LLM externo. **Barrera terminal:**
`ContextBundle conserva untrusted-corpus-data-v1 y siete carriers focales. Knowledge real completó tres casos de prompt injection, bajo presupuesto y sin acciones de corpus/DB; los contextos sanitizados conservaron la frontera. Esto valida serialización y recuperación read-only, no obediencia universal de un LLM ni un host MCP capaz de cambiar su plan de herramientas.`.

## 42. Rendimiento

Las comparaciones finales usarán la misma carga, corpus, perfil y estado de
cache. No se calificará como mejora una reducción de costo que disminuya
cobertura, cambie el dataset o desplace trabajo a publicación/segunda corrida.

El antes/después SQL-trace de NC-AUD-042 pasa de una actualización FTS a cero en
un cache hit estable. Un cambio de path ya no reutiliza esa versión: publica una
sucesora y fuerza reconciliación. Las focales de NC-AUD-043 demuestran que una
primera apertura de base sin fence hace full finalize y que la siguiente corrida
full estable omite `finalize_graph`; cualquier cambio, missing, selección parcial,
run incompleto o firma de resolver distinta lo fuerza. Esto demuestra trabajo
evitado, no todavía mejora comparable de wall time. **Comparación operativa:**
`Código v3 redujo graph de horas a 174.24 s y luego 71 ms en fast path, aunque analysis siguió en 2,526.1578692 s. Semantic staging sintético mejoró 57.2388x y la corrida viva terminó el staging de todas las 63,749 fuentes seleccionadas, pero consumió 8,060.913785 s, 605.8 GB read/535.8 GB write y no llegó a inferencia/publicación; no hubo mejora end-to-end demostrada. El modelo quality midió 4.309850749843586 textos/s a 4 threads, proyección 212.6910465982922 h para 3.3 M únicas, no ETA operativa. Knowledge degradado mostró search p50=3,371 ms/p95=14,213 ms y 0 vectores.`.

## 43. Benchmarks

Los benchmarks terminales separan resolver/FTS, staging, inferencia y
retrieval; no confunden cargas ni estados de caché distintos. No hubo benchmark
vectorial porque no se publicó ningún vector. **Resultados:**
`Resolver/FTS y staging Semantic conservan benchmarks sintéticos comparables. El benchmark quality ejecutó 512 textos por perfil, 8 batches medidos y threads 1/2/4; 4 threads logró 4.30985075 textos/s, p50 batch 14.8868 s y proyección model-only de 212.691 h para 3.3 M únicas. La proyección no es ETA, usa pasajes sintéticos de 1,027 bytes y excluye SQLite/leases/publicación. El resultado vivo Semantic confirma que ANN no resuelve el cuello de ingestión.`.

## 44. Suite y cobertura

Línea base: 1,543 pruebas + 78 subpruebas, 34,790 statements, 29,712 cubiertos,
11,002 branches y 7,704 branches cubiertas; combinado exacto
81.70859538784067 %. La suite terminal se ejecutó monolíticamente bajo
Coverage y sus resultados aparecen en la barrera final de esta sección.

La matriz focal post-cambio aprobó **423** pruebas. La race de readiness afectó
un test nuevo de bounded subprocess y un fixture watcher preexistente: se
reprodujo con dos fallos en diez intentos y dos fallos del gate. Después de
publicación temporal+replace, polling de contenido parseable y cleanup en
`finally`, el stress aprobó **50/50**. El arnés externo aprobó **94** pruebas y
**3** gates. Estos resultados focales complementan, pero no sustituyen, la suite monolítica terminal ya ejecutada.

**Barrera final:** `Gate terminal: pytest monolítico bajo Coverage aprobó 1,616 pruebas y 82 subpruebas; JUnit registró 1,698 tests, 0 failures/errors/skips. Coverage final: 35,398 statements, 30,226 cubiertos y 5,172 ausentes (85.38900502853268 %); 11,208 branches, 7,859 cubiertas y 3,349 ausentes (70.11955745895789 %); branch-aware exacta 81.71694631592499 % (81.72 %). La corrida previa falló después de la suite al proyectar 15 paths virtuales PySide6/shiboken; la corrección y el rerun terminal completo aprobaron, sin ocultar esa incidencia.`.

## 45. Ruff, mypy y pip check

Línea base: Ruff limpio; mypy limpio sobre 202 módulos; `pip check` limpio.
Después del slice completo, compileall aprobó, Ruff quedó limpio, mypy aprobó
los **202 módulos** canónicos y `pip check` quedó limpio. El arnés externo aprobó
además Ruff, mypy strict, compile y help. Los fallos mypy iniciales se conservan
en la cronología y no se ocultan.

**Proyección terminal de comandos/evidencia estática:**
`El gate terminal congelado aprobó compileall, Ruff, mypy sobre el conjunto canónico y pip check. También aprobó la corrección del runner Coverage que omite sólo los 15 paths virtuales PySide6/shiboken sin ocultar fuentes físicas ausentes.`.

## 46. Build e instalación

Wheel y sdist basales se construyeron e inspeccionaron. Un venv limpio
`--no-deps` ejecutó version/help, pero una ruta operacional falló por `xxhash`
ausente, como corresponde a una instalación incompleta. La closure hermética
local permanece bloqueada por un wheel faltante de `faster-whisper`; no se usa
`--system-site-packages` como prueba hermética.

El validator estricto reprodujo que el sdist omitía este informe fechado y
helpers Python necesarios para las pruebas. `MANIFEST.in` incluye ahora el
patrón `docs/TECHNICAL_EVOLUTION_*.md`, el pointer legacy y la regla
`recursive-include tests *.py`. Un rebuild provisional pasó el validator; no se
reutiliza como artefacto final mientras continúen cambios y barreras.

RECORD, contenidos, sdist, wheel, venv final y entrypoints:
`Build terminal aprobado: wheel neocortex_framework-0.7.1-py3-none-any.whl de 1,088,892 bytes y sdist de 1,550,358 bytes; el validator de contenidos/RECORD aprobó. La instalación aislada --no-deps aprobó Neocortex --version y --help. Neocortex status y Knowledge status terminaron exit 1 por dependencias ausentes en ese entorno mínimo; no se presentan como smoke funcional. No existe closure hermética local completa y no se usó --system-site-packages como evidencia.`.

## 47. Licencias y distribución

NC-AUD-021/026 permanecen sujetos a decisión humana. No se elige licencia ni se
presenta el inventario técnico de terceros como dictamen jurídico. El paquete no
debe incorporar modelos, payloads ONNX ajenos, bases, corpus o dist-info de
terceros.

**Estado técnico de distribución:** `El validator terminal aprobó wheel/sdist y no detectó corpus, bases vivas, modelos ni terceros accidentales en esos paquetes. NC-AUD-021/026 permanecen bloqueados por decisión humana; no se eligió licencia. La misma inspección deberá repetirse sobre el ZIP de auditoría antes de entregarlo.`.

## 48. Documentación

Este archivo es nuevo y no sobrescribe auditorías. La actualización de AGENTS,
README, arquitectura, CLI, operaciones, persistencia, Knowledge, recovery,
seguridad, changelog, instalación offline y licencias se limita a superficies
afectadas.

La regla de distribución ya incluye informes `TECHNICAL_EVOLUTION_*` y helpers
Python de tests; su presencia exacta se comprobará nuevamente en el sdist final.

**Documentos finalmente modificados y validados:**
`Los docs estables describen resolver v3, FTS set-oriented, runs operativos, cierre de generators en su thread propietario, staging Semantic por sesión/lotes de 128 y fixed-point reuse, separando el speedup sintético 57.2388x de cualquier afirmación viva. El inventario final de fuente registra 724 archivos/14,386,035 bytes, 203 módulos Python de producción, 137 pruebas Python y 94,546/54,949 LOC. El sdist/validator aprobaron; este informe LAB todavía debe integrarse y el ZIP debe validarse sin marcadores ni contenido sensible.`.

## 49. Compatibilidad

No cambió el shape de los contratos JSON/API ni hubo DDL. El analyzer Python v2
invalida cache deliberadamente; `rendered_context` añade una línea estática
versionada; routing usa eventos/CAS bajo framework 19; el fix FTS no altera
schema. El fence del grafo se almacena como metadata tipada bajo code schema 2;
no crea una generación ni un head nuevo. El cambio de path publica una versión
sucesora y la finalización reconstruye sólo derivados actuales. La validación de
analyzer usa la identidad runtime efectiva; la firma global de registry se
conserva y todavía puede invalidar lenguajes no afectados. Esos cambios de
comportamiento son intencionales y están documentados.

**Regresión terminal de compatibilidad:** `No hubo DDL ni cambio intencional de JSON/API/CLI. Semantic staging conserva schema 6/wrappers públicos y fixed-point sólo cambia orden de reuse/cleanup de leases; la matriz de 180 aprobó. Knowledge evaluó JSON real en modo degradado explícito y no leyó building como publicado. La monolítica y el gate terminal completo aprobaron los contratos observables; el bloqueo de status/Knowledge en el venv --no-deps es falta de closure, no una prueba de incompatibilidad del paquete.`.

## 50. Rollback

Rollback por incremento: restauración byte-exacta de módulos/documentos;
para schemas, backup SQLite consistente más paquete compatible, nunca edición
manual de `schema_version`/`user_version`. No se ejecutará rollback destructivo
sobre `<STATE>` sin preflight y autorización.

Manifiesto exacto de rollback: `El source delta del handoff registra la fuente congelada y su comparación con la línea base. Los backups byte-exactos privados quedan fuera del ZIP; no se promete rollback de estado vivo porque no se ensayó ni autorizó una restauración operativa.`.

## 51. Manifiesto por archivo

`<EVIDENCE>/file_manifest.json` contendrá paths relativos/alias, tamaño, hash de
integridad de paquete cuando corresponda, clasificación y razón del cambio. Los
fingerprints propios de NeoCortex seguirán los algoritmos no criptográficos del
framework; hashes criptográficos se reservan para protocolos de paquete y
confianza.

**Manifiesto final:** `El inventario v12 observó 724 archivos y 14,386,035 bytes en el árbol amplio, con 203 módulos Python de producción, 137 de pruebas y 94,546/54,949 LOC. El file_manifest del handoff usa una política comparable distinta, excluye evidencia para evitar recursión y se regenera desde la fuente congelada; no incluye bases, modelos ni backups privados.`.

## 52. Artefactos incidentales

No se conservarán capturas, rasterizados, reportes de prueba redundantes,
caches, `.pyc`, venvs, logs sensibles ni temporales visibles. Los respaldos
privados no entran en evidencia ni ZIP.

Inventario y limpieza final: `La selección del handoff rechaza pyc, caches, venvs, logs, bases, WAL/SHM, modelos, links y backups privados. Por circularidad, la validación del ZIP y el snapshot global de procesos/Defender se registran post-build fuera de la evidencia embebida.`.

## 53. Limitaciones

Limitaciones ya demostradas:

- la closure de instalación hermética local está incompleta; el venv `--no-deps`
  sólo aprobó `--version` y `--help`, mientras status/Knowledge quedaron
  bloqueados por dependencias ausentes;
- Semantic terminó el staging de texto seleccionado, pero sólo alcanzó 1/5
  fases: faltan inferencia, publicación, head y repetición incremental Semantic;
- el golden real de 50 casos carece de `human_verified`, vectores y cobertura
  híbrida/multihop;
- dos agregados Semantic agotaron su presupuesto y la distribución de formatos
  de inventario quedó truncada;
- code schema 2 no es generacional; recovery posterior a `record` y retención
  aplicable siguen incompletos;
- no existe grafo transversal general, MCP, ANN ni feedback durable promovido;
- los runs 61/62 son intentos históricos anteriores al hardening y no se usan
  como gates; runs 66/70 y el gate terminal los sustituyen como evidencia;
- el gate construyó y validó wheel/sdist para el snapshot previo al informe
  terminal, pero el rebuild final debe repetirse tras integrar este informe y
  congelar la fuente de handoff;
- provenance de audio reportó `ffprobe` no disponible y 131 errores;
- no se capturaron máximos absolutos de recursos después de suspender muestreo;
- las ubicaciones inaccesibles no pueden certificarse como libres de fugas y la
  equivalencia física byte a byte del corpus requiere el cierre terminal.

Limitaciones adicionales posteriores: `Run 70 fue estable pero costoso. Semantic terminó el staging de 63,749 fuentes, pero dejó 5,133,824 jobs, DB 18.3067 GB, más de 1.14 TB de I/O acumulado, 1/5 fases y cero vectores/head; el benchmark proyecta ~212.691 h model-only para 3.3 M únicas a 4 threads. Knowledge real es exact/lexical degradado: 0 vectores, cutoff rate 1.0, citation truth 0.7628 y no-answer provisional 0.0. Dos agregados Semantic agotaron sus presupuestos de 900 s y 10,000,000,000 VM steps; formatos de inventario quedó truncado. El gate terminal y SQLite concluyeron, pero la closure hermética es falsa y status/Knowledge del venv mínimo quedaron bloqueados. Handoff, cleanup global y Defender se atestiguan post-build para evitar circularidad.`.

## 54. Puntos no verificados

Permanecen sin verificación terminal:

- inferencia/publicación Semantic, head completo y una repetición incremental
  Semantic que demuestre reuse después de los 5,133,824 jobs staged;
- calidad `human_verified`, multihop e híbrida del golden real;
- los dos agregados Semantic que agotaron presupuesto y el inventario de
  formatos más allá del límite observado;
- closure hermética offline y smoke funcional de status/Knowledge en un venv
  con todas las dependencias locales;
- rebuild y validator finales de wheel/sdist después de integrar el informe;
- evidencia/ZIP, reconciliación global de procesos/recursos, after-state de
  Defender y equivalencia física terminal del corpus.

Incremental run 70, resultados por owner disponibles, Knowledge real, golden
sintético/real degradado, suite/Coverage/estática y el build de gate ya se
ejecutaron; no se listan de nuevo como trabajo ausente.

## 55. Riesgos residuales

Riesgos abiertos: NC-AUD-011, 014, 015, 016, 021, 026 y 037, más los
hallazgos operativos 053–058 documentados en esta campaña. NC-AUD-038, 042, 043,
044 y 047 quedaron corregidos y revalidados operacionalmente en runs 66/70;
no requieren otra corrida para conservar ese estado. NC-AUD-039, 040 y 041
quedan parciales sólo por sus límites explícitos de candidate retention,
obediencia de un LLM/MCP y cobertura exhaustiva del run 68. NC-AUD-045/048
están revalidados; NC-AUD-046 conserva el costo de invalidación por firma global.
NC-AUD-049 queda limitado al rebuild/validator final después de congelar la
fuente de handoff.

Orden terminal por severidad: `Prioridad: rediseñar/particionar inferencia y publicación Semantic para completar 5.13 M jobs tras un staging ya finalizado, sin transacción por resultado ni head parcial; evaluar perfiles/modelos más económicos antes de ANN. Después: cancelación/schema generacional code (015/058), PDF no-op (053), candidate retention (054), catálogo all-cache (055), GUI containment (037), ffprobe/hermeticidad (056/016), recovery/retention (011/014) y licencia humana (021/026). Handoff/ZIP, cleanup global y Defender after-state son riesgos de cierre; GraphRAG, MCP, feedback y ANN siguen aplazados.`.

## 56. Cambios deliberadamente no realizados

No se aplicó organización, deduplicación destructiva, rename/move/delete,
retención, recovery físico, `VACUUM`, migración improvisada, instalación global,
descarga, exclusión Defender, licencia, GraphRAG, MCP, ANN o autopromoción de
feedback. Estas abstenciones preservan contratos y no son funcionalidad
entregada.

## 57. Próximos pasos

1. particionar y reanudar inferencia/publicación Semantic hasta publicar un head
   sin sacrificar fencing, bounded memory ni rollback; después repetir Semantic
   para medir reuse e idempotencia de su estado estable;
2. añadir revisión `human_verified` y casos multihop/híbridos al golden real una
   vez disponible el head Semantic;
3. rediseñar o acotar los dos agregados Semantic agotados y completar la
   distribución de formatos sin consultas ilimitadas;
4. cerrar la wheelhouse offline y ejecutar smoke funcional de status/Knowledge
   en un venv hermético;
5. integrar el informe, congelar fuente, reconstruir wheel/sdist y repetir el
   validator estricto;
6. producir evidencia sanitizada/ZIP y cerrar procesos, recursos, corpus,
   Defender y laboratorio;
7. después priorizar code schema 3 generacional y los contratos pendientes de
   recovery/retention. GraphRAG, MCP y ANN permanecen detrás de esas puertas.

## 58. Cierre de barrera

| Criterio | Estado |
|---|---|
| Árbol e instrucciones | `aprobada para fuente y sdist: AGENTS.md canónico, pointer legacy no contradictorio, versión 0.7.1 y validator de paquete limpios; la comprobación del ZIP pertenece al handoff pendiente` |
| Línea base | completada |
| Ejecución de todas las rutas disponibles | `parcial con evidencia: inventario/dedup y seis rutas de extracción quedaron cubiertas y el incremental run 70 aprobó con incidencias. Semantic terminó el staging de las 63,749 fuentes seleccionadas, incluidas 52,267 code con chunks, pero la campaña sólo alcanzó 1/5 fases y terminó sin inferencia, vectores ni head` |
| Segunda corrida incremental | `aprobada con costo residual: run 70 completó las seis rutas en 5,220.0532761 s, completed_with_issues/strict exit 2 y cleanup=true; la idempotencia funcional no implica eficiencia` |
| Knowledge sobre estado real | `completada en modo degradado explícito: 50 casos, snapshot estable de 10 owners y cleanup; recall@10 determinista 0.833333, sin vectores/head Semantic, por lo que no certifica recuperación híbrida completa` |
| Golden sintético final | `aprobada: rerun 17/17, recall 0.9102564102564104, MRR 0.9615384615384616 y nDCG 0.9356412992914329; sólo fixtures scripted` |
| Evaluación real/provisional separada | `completada como evaluación determinista/provisional degradada de 50 casos; 0 human_verified, 3 unverified, no-answer provisional 0.0 y cero vectores impiden tratarla como promoción humana o barrera híbrida plena` |
| Correcciones con regresión e integración | `aprobada: resolver/FTS, ownership de generators, staging por sesión/lotes, fixed-point reuse y corrección de Coverage tienen focales, integración y gate terminal completo aprobados` |
| Suite/Coverage/estática | `aprobada para fuente, pruebas, estática, golden, SQLite y paquetes, con limitaciones declaradas: 1,616 pruebas + 82 subpruebas; 35,398 statements/30,226 cubiertos; 11,208 branches/7,859 cubiertas; branch-aware exacta 81.71694631592499 %. La instalación aislada no prueba closure hermética y status/Knowledge quedaron bloqueados por dependencias locales ausentes` |
| Wheel/sdist/venv/entrypoint | `parcial por closure: wheel/sdist, validator, instalación aislada --no-deps, --version y --help aprobaron; status y Knowledge no arrancaron por dependencias locales ausentes. El rebuild exacto post-informe se valida en el manifiesto externo de packaging.` |
| Evidencia y ZIP | `not_applicable_pre_archive: la evidencia embebida no puede atestiguar retrospectivamente su propio contenedor; la validación streaming post-build y el HANDOFF_MANIFEST son la autoridad.` |
| Corpus intacto/no `--apply` | `no se usó --apply ni se autorizó mutación; equivalencia física final sólo si el inventario terminal la demuestra, de lo contrario limitar la afirmación` |
| Procesos residuales cero | `run 70, Semantic post-fix y 17 invocaciones Knowledge terminaron con Jobs vacíos/reap/cleanup. El snapshot global autoritativo se ejecuta post-build y no se autoatestigua dentro del ZIP.` |
| Defender no modificado | `no se modificó ni se creó exclusión; la comparación final con el baseline se registra post-build fuera de la evidencia embebida.` |

**Estado final de barrera:** `parcial por límites funcionales demostrados: rutas e incremental están demostradas; Semantic terminó todo el staging seleccionado, pero sólo alcanzó 1/5 fases y quedó sin inferencia, vectores o head; Knowledge real y golden sintético terminaron; suite, Coverage, estática, SQLite, wheel/sdist y validator aprobaron. La closure hermética sigue bloqueada. Handoff/ZIP, procesos y Defender se cierran mediante atestaciones post-build no circulares`.

## Comentarios y criterio técnico del agente

### Hechos

- El gate terminal aprobó la suite monolítica bajo Coverage, 1,616 pruebas
  y 82 subpruebas, además de Ruff, mypy y pip check; esto demuestra consistencia
  contractual del árbol final, no calidad híbrida del corpus.
- El golden sintético tiene métricas altas pero candidatos/rankings controlados;
  sirve como regresión de contratos, no como validación del sistema cognitivo en
  producción.
- Los defectos reproducidos en bindings, routing, procesos y FTS ocurren en
  fronteras de integridad/reanudación/costo; por ello tuvieron prioridad sobre
  GraphRAG, MCP o ANN.
- El crecimiento de `code.sqlite3` y la duración de graph fueron el resultado
  operacional más inesperado y elevaron NC-AUD-015 sobre nuevas representaciones.
- El fence `code-graph-resolver-v3` evitó repetir graph en run 70 sin fingir
  publicación generacional; 60,405 cache hits y un estado idéntico accedieron al
  fast path en 71 ms, aunque analysis conservó costo residual.
- Las carreras de readiness y accounting fueron defectos de fixtures/arnés, no
  fallos demostrados del pipeline; conservar sus primeras corridas fallidas evita
  convertir infraestructura reparada en evidencia retroactiva.
- El validator de distribución encontró una omisión real que build por sí solo
  no detectaba; MANIFEST se corrigió y el gate aprobó. El rebuild final sigue
  pendiente únicamente porque este informe aún debe integrarse antes de congelar
  la fuente de handoff.
- La instalación aislada sin dependencias prueba empaquetado/entrypoint, no una
  closure hermética operacional.

### Interpretación y confianza

- **Alta:** conservar SQLite por owner, publicación generacional y snapshot
  lógico es una decisión arquitectónica adecuada mientras no exista una
  limitación medida que justifique otro backend.
- **Alta:** publicar el binding de routing antes de persistir candidatos era una
  frontera incorrecta; la implementación atómica/CAS/idempotente fue revalidada
  en la cadena 63–70, incluida la incremental 70.
- **Alta:** la marca estática `untrusted-corpus-data-v1` corrige la ambigüedad de
  autoridad del ContextBundle sin eliminar ni alterar la evidencia citada.
- **Media:** los módulos Knowledge grandes constituyen deuda de mantenibilidad,
  pero no justifican refactor amplio sin métricas de complejidad y regresiones
  que conserven JSON/API.
- **Alta:** el RunManifest debe seguir como evidencia derivada y fail-closed.
  Runs 61/62 conservan valor histórico limitado; runs 63–70 probaron el arnés
  endurecido y son la evidencia operacional promovida.
- **Alta:** el fence tipado, `mark_missing` previo y CAS terminal son una mejora
  preservadora adecuada para schema 2. No sustituyen schema 3: no hay building,
  head publicado, lotes reanudables ni retención generacional.
- **Alta:** publicar versión sucesora ante cambio de path es más honesto que
  mutar un locator dentro de una versión cacheada; conserva historia y obliga a
  reconstruir memberships/FTS actuales.
- **Media:** validar el analyzer runtime evita cache incorrecta, pero la firma
  global sigue teniendo un radio de invalidación mayor que el analizador usado.
- **Alta:** temp+replace y polling de payload parseable son el contrato correcto
  para readiness Windows; esperar sólo existencia era inherentemente racy.
- **Alta:** el build/validator del gate demuestra que MANIFEST corrige la
  selección. Debe repetirse una vez más tras integrar el informe para que los
  artefactos correspondan exactamente a la fuente congelada de handoff.

### Qué no puede concluirse todavía

No puede afirmarse recuperación híbrida/semántica, calidad `human_verified`,
closure hermética, ausencia global final de procesos ni paquete de handoff
correcto. Sí quedó demostrada la segunda corrida incremental funcional y la
estabilidad de IDs/fast path observada; no se confunde esa idempotencia funcional
con costo no-op óptimo ni con idempotencia Semantic, que no llegó a inferencia.

### Prioridad recomendada para la siguiente auditoría

La prioridad técnica siguiente es completar inferencia/publicación Semantic
de forma reanudable y acotada, publicar un head y medir su repetición estable.
Después corresponde code schema 3, recovery y retention; GraphRAG, MCP y ANN
deben seguir aplazados. Confianza **alta** para el cuello Semantic y **media**
para el orden posterior.

### Hipótesis que debe revisar la siguiente auditoría

1. que una repetición Semantic después de publicar head reutiliza trabajo sin
   nuevas generaciones innecesarias;
2. que particionar inference/publication reduce I/O sin perder fencing, leases o
   rollback;
3. que los locators reales sobreviven a fusión, presupuesto y evidencia híbrida;
4. que el cierre global no deja hijo/nieto, handles, threads ni cambios Defender;
5. que `untrusted-corpus-data-v1` no puede confundirse con instrucción de sistema;
6. que la closure offline faltante no oculta dependencias no declaradas;
7. que code schema 3 puede conservar el fast path v3 y la historia publicada;
8. que una firma por analizador reduce invalidación sin aceptar cache stale.
