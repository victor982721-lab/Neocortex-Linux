# Handoff — progreso adaptable de la CLI

**Ronda:** NEO-CLI-PROGRESS-20260927.
**Estado:** implementación, publicación, instalación y verificación terminadas.
**Commit de código:** `62d3c608231b47c8c79101a389be2f384f669528`.
**Release instalada:** `0.14.1-62d3c608231b-cp313-linux-x86_64`.

## Causa y cambio

Antes del cambio, una sola corrida sintética en PTY empezó con 112 columnas,
bajó a 38 y volvió a 112. `RichProgress` conservaba `Console.width == 112` tras
el resize a 38. `Rich.Live` calculaba el ancho y la limpieza con esas dimensiones
antiguas, mientras el PTY envolvía la fila ancha; esto reproduce el desfasaje que
deja fragmentos y filas fantasma.

La consola interactiva consulta el tamaño del PTY en cada redibujado e ignora
`COLUMNS`/`LINES` obsoletos. La vista tiene una fila sin wrap por clave
`(operation, phase)`, agrupada en preparación, inventario/validación, rutas,
catálogos/Semantic y cierre. El resumen ordena avance/unidad, caché, trabajo
nuevo, errores, esperas y estado. Descripción, tiempos y métricas secundarias
aparecen sólo cuando caben; contadores y estado se conservan completos. Reusar
una clave finalizada reinicia el reloj y los campos de la tarea sin crear otra
fila.

La renderización consume `ProgressEvent` y `ProgressMetric`. `LineProgress`, el
envelope JSON y `NEOCORTEX_PROGRESS_STREAM` no cambiaron.

## Validación

- 24 pruebas focales aprobaron en CPython 3.13.15: renderer/progreso, namespace,
  flujo JSON de resultados y errores tipados de rutas.
- PTY sintético comprobó anchos 44, 160 y 40; resize estrecho → amplio → estrecho,
  una tarea estable, conteos/estados completos y la actualización tras finalizar.
- La release pasó `tools/release_linux.py verify`: `verified=true`,
  `source_sha=62d3c608231b47c8c79101a389be2f384f669528`, corpus de verificación
  `ephemeral_empty_v1` y release anterior conservada como rollback.
- El launcher instalado informó `Neocortex 0.14.1`. Un PTY sintético desde el
  intérprete instalado confirmó redibujado tras resize y no leyó el Corpus real.
- Antes de promover, no había un proceso activo `Neocortex --all --apply`.

## Límites

No se ejecutaron Ruff, Mypy, Pyright ni Semgrep: el inventario de analizadores
disponible es histórico para CPython 3.14 y no hay un entorno autenticado de
calidad CPython 3.13. No se validó con archivos reales del Corpus, conforme a
la solicitud. Los metadatos de descripciones largas pueden abreviarse; las
métricas tipadas y el estado permanecen legibles y sin recorte.
