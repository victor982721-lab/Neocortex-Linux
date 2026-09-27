# Handoff operativo vigente — NeoCortex

**Ronda:** NEO-WHISPER-ZIP-QA-20260927.
**Estado:** código publicado, release instalada y verificación terminada.
**Detalle:** [NEOCORTEX_WHISPER_ZIP_QA_2026-09-27.md](NEOCORTEX_WHISPER_ZIP_QA_2026-09-27.md).

Release activa: `0.14.1-d854809926ca-cp313-linux-x86_64`.
Whisper valida identidad efectiva sin confundir el conteo observado de GPU en
CPU; ZIP aparece inicialmente y su terminal cuenta el lote completo. QA está
adaptado a CPython 3.13.15 en un entorno separado del producto.

248 pruebas focales, 2 subtests y verificación instalada aprobadas. Ruff y
Semgrep pasan; Mypy/Pyright conservan deuda previa sin diagnósticos nuevos.
No se procesó el Corpus real. Su corrida 4 permanece fallida y tuvo 9
abstenciones ZIP por límites/colisión/corrupción, no un procesamiento tardío.

---

# Handoff anterior — NeoCortex

**Ronda:** NEO-CLI-PROGRESS-TABLES-20260927.
**Estado:** código publicado, release instalada y verificación terminada.
**Detalle:** [NEOCORTEX_CLI_PROGRESS_TABLES_2026-09-27.md](NEOCORTEX_CLI_PROGRESS_TABLES_2026-09-27.md).

La vista principal usa tablas alineadas con las mismas columnas para todas las
rutas y separación visual entre etapas. Los campos específicos quedan en una
vista de detalle explícita. Los conteos proceden de los eventos originales y
un dato ausente se representa con `—`, distinto de cero.

La release activa es `0.14.1-9ce341cf2f1d-cp313-linux-x86_64`, construida del
commit `9ce341cf2f1d7f43f8dc80d2796a9cb3555f625c`. Se conservó
`0.14.1-62d3c608231b-cp313-linux-x86_64` como rollback. El proceso de Corpus que
estaba activo terminó naturalmente; antes de promover no quedaban procesos
CLI ni intérpretes de releases en uso.

Pasaron 43 pruebas focales y la revisión independiente. La verificación final y
el PTY del paquete instalado usaron datos sintéticos y rutas temporales. No se
procesó el Corpus real para validar la corrección ni se prepararon modelos.

---

# Handoff anterior — NeoCortex

**Ronda:** NEO-CLI-PROGRESS-20260927.
**Estado:** implementación, publicación, instalación y verificación terminadas.
**Fuente de detalle:** [NEOCORTEX_CLI_PROGRESS_2026-09-27.md](NEOCORTEX_CLI_PROGRESS_2026-09-27.md).

La CLI ahora mide el PTY en cada redibujado y muestra una fila compacta y estable
por `(operation, phase)`, agrupada por etapa general. El flujo usa exclusivamente
`ProgressEvent` y sus métricas tipadas; el JSON de progreso permanece igual.
La release activa deriva del commit de código `62d3c608231b47c8c79101a389be2f384f669528`
y conserva la release anterior como rollback.

La validación usó eventos sintéticos, PTY y un corpus temporal vacío; no procesó
el Corpus real ni preparó modelos. No se encontró una corrida
`Neocortex --all --apply` activa antes de promover. Ruff/Mypy/Pyright/Semgrep no
se ejecutaron: el inventario local de analizadores es histórico para CPython
3.14 y no hay un entorno de calidad CPython 3.13 autenticado.

---

# Handoff anterior — NeoCortex

**Ronda:** NEO-OPT-20260926.
**Alcance:** segunda pasada de simplificación de código privado inalcanzable y
reducción de trabajo repetido, con equivalencia funcional y E2E aislado.

Se conservan las comprobaciones físicas pre/post efecto, las publicaciones
completas, los contratos de lectura y los puntos de compatibilidad. La revisión
independiente descarta optimizaciones que cambian resultados o no benefician
un consumidor real; menos líneas o menos tiempo aislado no acreditan la entrega.

Matriz, comparaciones, propuestas descartadas, validación integrada y estado
exacto de publicación/instalación/rollback se consultan en
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-26-segunda-pasada-01a0dbb9/`.
El SSOT conserva los gates vigentes. La ronda no procesa el corpus real, no
abre sus owners operativos y no reanuda experimentos pausados.

---

# Handoff anterior — NeoCortex

**Ronda:** NEO-AUD-20260925.
**Alcance:** auditoría correctiva del flujo integrado, publicación Semantic,
admisión de formatos, identidad y efectos, con pruebas de aplicación y replay
sobre corpus sintético acotado.

La observación integrada de heads usa una proyección coordinada del owner
existente, no una copia completa de su base y WAL. Las lecturas públicas
conservan sus fences y límites. ZIP y Trash requieren identidad/evidencia junto
al efecto; una publicación o un efecto incierto no se presentan como éxito.
Organización diferencia propuestas advisory no ejecutables de errores reales.

La evidencia vigente, el resultado de la suite, las canarias de formatos y de
owner grande, el SHA publicado y la verificación de instalación/rollback se
consultan en `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-25-auditoria-codigo-01a0dbb9/`.
Este cambio no procesa, reinicia ni repara por inferencia el corpus o el estado
productivos: los ejercicios usan HOME/XDG/owners/caches privados. El estado de
entrega lo gobiernan ese expediente y el SSOT, no este puntero.

---

# Handoff anterior — NeoCortex

**Ronda:** NEO-CURACION-AUTONOMA-20260919.
**Alcance:** admisión granular del corpus y Papelera de terceros regenerables con prueba local, más hotfixes del ZIP aportado.

Los intereses predeterminados de Code son las raíces de NeoCortex, MTF y bitácoras EPS. Los marcadores no añaden proyectos. La admisión se aplica antes de los extractores de contenido y por miembro de Archive, sin excluir documentos/datos por estar en AppData o una caché. Las altas y los opt-ins de Code son explícitos y forman parte de las firmas de procesamiento y replay.

La señal de origen no autoriza Papelera: se requiere un miembro idéntico de wheel/nupkg/npm local retenido o bytecode reproducido desde una copia privada de su fuente. Fuentes, licencias, fixtures, credenciales e inciertos permanecen protegidos. Inventario, límites, ledger, identidad, revalidación y receipt mantienen sus fronteras. Resume no reutiliza candidatos bajo otra política ni migra entradas antiguas silenciosamente.

La entrega no reinicia la corrida detenida ni procesa/limpia el corpus real. Las pruebas usan owners, HOME/XDG y efectos reversibles privados. Las cabezas históricas no se regeneran por instalar código. La evidencia final, decisiones de los 50 IDs del ZIP, publicación e instalación autorizada se registran fuera del producto en `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-19-curacion-autonoma-01a0ba3f/`.

---

# Handoff anterior — NeoCortex

**Ronda:** NEO-FACTORY-RESET-20260919.
**Alcance:** reemplazo del reset selectivo por `Neocortex --factory-reset`.

La orden elimina el estado operacional bajo la raíz seleccionada sin abrir
SQLite, crear backups, planes/digests, snapshots ni recibos de reset. Incluye
bases/sidecars, materializaciones Archive, caches y metadatos operacionales.
Conserva Corpus/originales, instalación/modelos, receipts de instalación y los
inodes de control de locks para no romper exclusión de writers. No sigue rutas
externas desde claims ni symlinks. Backup/restore independientes no cambiaron.

Se retiran el motor de reset selectivo, transformaciones por owner, su API/SDK,
CLI anterior y compensación exclusiva; los lectores de pisos legacy conservan
compatibilidad con estado anterior. Las reglas compartidas de lifecycle siguen
sirviendo backup/restore y topology, no el nuevo factory reset.

La aceptación usa fixtures con HOME/XDG privados: owner sparse de 14 GiB con
WAL no vacío, materializaciones, bases inválidas/ausentes, locks activos, rutas
protegidas, enlaces externos, cambios de mount y repetición idempotente. La
aceptación instalada exige procesar → factory reset → procesar nuevamente sin
reutilizar el estado anterior. No se ejecuta factory reset sobre estado real.
Los resultados de publicación/instalación se registran fuera del producto en
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-19-factory-reset/`.

## Handoff anterior (histórico; no acredita el reset nuevo)

**Ronda:** NEO-ADAPTIVE-OPTIMIZATION-20260919.
**Estado:** implementación y validación proporcional terminadas.
**Entrega:** integración directa en main; la raíz conserva Git y los recibos de publicación.
**Base:** `453f74344f71f54befe6edb3321846aed7c53905`.

La entrega conserva el trabajo adaptativo recuperado y las optimizaciones de seis
frentes con revisión independiente: inventario/catálogo/organización, contenido y
Code, multimedia, Semantic/Knowledge, runtime y persistencia/workflow. Los cambios
observables se describen en Changelog y Architecture; métricas y receipts se
entregan fuera del árbol productivo en el informe de optimización y sus evidencias.

El coordinador comparte capacidad desde inventario hasta recuperación y distingue
límites explícitos por formato de capacidad automática. Las reservas contemplan
residencia y trabajo pendiente; las ventanas semánticas, el catálogo y ReviewTask
reducen recorridos repetidos. ZIP retiene marcadores acotados y Code reutiliza mapas
de posiciones. Organización entrega clasificaciones por lotes. El clasificador v17
corrige los prefijos de texto y exige una reclasificación compatible antes del replay.

La integración corrigió el crédito de memoria privada de hijos registrados cuando
faltan listados de descendientes, el cierre de renovaciones en el contexto de su
apertura y la fuga del contexto de la reserva retenida de la proyección Code hacia
otras rutas. Los límites, la identidad y la liberación permanecen comprobados.

La suite general con CPython 3.14.7 y capacidades base,documents,image terminó con
9207 aprobadas, 2 fallidas, 79 omitidas, 34 no seleccionadas y 52 subtests aprobados.
Las dos incidencias eran fixtures: argumentos obligatorios de replay Office y un
reloj de cancelación que incluía preparación anterior al bloqueo. Se corrigieron
conservando las aserciones funcionales y el límite de un segundo; las verificaciones
posteriores aprobaron 20 casos FTS/replay y 4 de cancelación/espera. No se repitió
la suite general tras esos ajustes. No quedan fallos pendientes de esa ejecución.

Ruff pasa en 177 Python modificados. La comparación de Mypy en el mismo alcance
heredado dio 406→402 diagnósticos sin mensajes nuevos; un fixture nuevo se corrigió
y pasó su comprobación focal. Coordinador, sampler y proyección finales también
pasan Mypy focal. Mypy global conserva deuda previa, no se declara limpio.

El wheel 0.14.1 se construyó sin descargas, comprobó 546 archivos productivos y se
instaló en un entorno privado con pip check correcto. La validación instalada cubre
primera ejecución y replay en siete rutas sobre 23 fuentes sintéticas, con trabajo
y caché verificados, sin errores de ruta y con originales intactos. El resumen de
rutas precede al cierre de la proyección Code y conserva sus 8 MiB hasta ese cierre;
las regresiones verifican su liberación final. La comprobación del paquete no es una
medición de rendimiento global sobre el corpus habitual.

La evidencia contiene pruebas con PDF nativo, NumPy/BLAS, FFmpeg y OCR inglés.
La calibración con corpus representativo, GPU/modelos reales y OCR español sigue
fuera de esta validación. Organización aún mantiene su transacción de planificación
y un conjunto de ubicaciones proporcional al historial pertinente. Las observaciones
incompletas del sistema conservan un comportamiento prudente.

Estado anterior: [recuperación acotada](NEOCORTEX_BOUNDED_RETRIEVAL_2026-09-19.md).
