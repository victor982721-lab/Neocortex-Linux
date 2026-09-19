# Handoff operativo vigente — NeoCortex

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
