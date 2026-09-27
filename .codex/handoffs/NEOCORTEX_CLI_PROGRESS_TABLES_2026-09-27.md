# Handoff — tablas de progreso de la CLI

**Ronda:** NEO-CLI-PROGRESS-TABLES-20260927.
**Estado:** publicación, instalación y verificación terminadas.
**Commit de código:** `9ce341cf2f1d7f43f8dc80d2796a9cb3555f625c`.
**Release:** `0.14.1-9ce341cf2f1d-cp313-linux-x86_64`.

## Corrección de la presentación

La revisión visual de la entrega anterior mostró filas densas, sin columnas
alineadas y con campos distintos para cada ruta. La vista principal ahora usa
tablas con encabezados comunes y espacio entre las etapas. Los contadores
exactos y el estado se conservan completos; al estrechar el terminal se ocultan
columnas enteras para todas las filas de la tabla. Un dato no publicado queda
como `—`; los ceros reales se muestran como `0`.

El ancho adicional no activa detalles automáticamente.
`NEOCORTEX_PROGRESS_DETAILS=1` solicita un bloque separado con descripciones,
ETA y métricas específicas, incluidas las columnas ocultas por ancho. La
consola conserva la medición dinámica del PTY y una tarea por operación/fase.

La revisión independiente detectó cierres de fallos/cancelaciones y pausas
Semantic que sólo comunicaban el resultado en texto humano. Esos productores
ahora publican el estado mediante la métrica tipada existente. El renderer
también interpreta `completion_status` y traduce los códigos Semantic
existentes sin parsear descripciones. El envelope de progreso conserva sus
11 claves JSON; se agrega estado estructurado donde antes faltaba.

## Evidencia acotada

- 43 pruebas focales aprobadas: tablas, anchos 40/60/80/120/240, datos ausentes
  frente a cero, conteos exactos mayores de 2^53, terminal parcial/reanudación,
  JSON/CLI outcomes y pausas/reanudaciones de Texto e Imagen/OCR.
- Revisión independiente del actor `progress_ui_review`
  (`gpt-5.6-luna/max`): UI1 presentación uniforme, UI2 PTY/redibujado y UI3
  estado/stream aprobados. Las regresiones de staging reales verificaron
  `partial → completed` sin cambiar contadores ni generaciones.
- `tools/release_linux.py verify`: `verified=true`, source SHA exacto,
  `current` coincide y corpus de verificación `ephemeral_empty_v1`.
- Launcher instalado: `Neocortex 0.14.1`. Smoke del módulo instalado fuera del
  checkout y sin PYTHONPATH: resize `40 → 160 → 80 → 40 → 160`, tareas
  estables, redibujados ANSI y un fallo del orquestador real mostrado como
  `Fallido`. El stream mantuvo las métricas fuente sin traducir.
- La corrida productiva inicial PID 367736 terminó naturalmente. Antes de
  instalar no se encontraron procesos CLI ni intérpretes de releases en uso.
- Rollback inmediato: `0.14.1-62d3c608231b-cp313-linux-x86_64`.

## Límites

La validación usó eventos sintéticos y fixtures temporales; no procesó el Corpus
real ni descargó modelos. Ruff/Mypy/Pyright/Semgrep siguen sin un entorno de
calidad autenticado CPython 3.13 en este host; el inventario CPython 3.14 es
histórico. La revisión no declara una suite general ni mediciones de throughput.
