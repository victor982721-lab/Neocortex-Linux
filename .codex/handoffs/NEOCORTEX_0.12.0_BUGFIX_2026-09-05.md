# Handoff NeoCortex 0.12.0 — correcciones de seguridad y escala

## Alcance

Se corrigieron los defectos reproducidos en la auditoría del 5 de septiembre
de 2026, sin modificar el corpus personal, una release productiva ni los
owners SQLite canónicos fuera de fixtures. La captura POSIX de subprocesses,
fingerprinting, preflight ZIP, paginación y verificación de curación conservan
sus contratos existentes y fallan cerrado ante evidencia incompleta.

## Cambios verificables

- Fingerprinting abre sólo archivos regulares mediante descriptores POSIX sin
  seguimiento de symlinks ni bloqueo de FIFO, y detecta cambios antes/después de
  leer, truncamiento y crecimiento.
- Subprocesses drenan pipes POSIX con `selectors` y `os.read`, comparten un
  deadline total y reportan `cleanup incomplete` cuando un descendiente fuera del
  grupo conserva los pipes.
- ZIP inspecciona registros reales del directorio central, EOCD/ZIP64, tamaños,
  offsets y longitudes antes de crear `ZipFile`; Office legado usa el mismo
  preflight.
- Curation publica el digest completo una vez por generación en una caché
  acotada y lee páginas con keyset, mientras grupos grandes recuperan todos sus
  miembros desde el owner SQLite cercado para verificación/autorización.
- PDF coordina lecturas y escrituras del owner durante extracción mediante un
  hilo dedicado, y `test-base` declara `setuptools==83.0.0` fuera del runtime.
- Rich mantiene indeterminados los terminales fallidos/cancelados y la API de
  verificación conserva métricas acotadas; la reanudación de inventario tiene
  una prueba real de interrupción, reapertura y replay.

## Validación

- Focal Linux desde el árbol: 138 pasadas, 4 omitidas legítimas, 1 deseleccionada
  y 2 subtests en las superficies corregidas.
- Dedupe adicional: 63 pasadas, 2 omitidas y 6 subtests.
- Documentos/PDF/ZIP desde el árbol con dependencias locales: 110 pasadas y 2
  omitidas.
- Paquete instalado en un venv temporal, con cwd `/tmp` y sin `PYTHONPATH` para
  el smoke público: `Neocortex --version`, ejecución de texto y replay pasaron;
  las regresiones instaladas sumaron 94 pasadas, 4 omitidas y 2 subtests.
- Ruff, `py_compile`, `compileall` y `git diff --check` pasaron en las superficies
  modificadas.

## Estado

El cierre requiere publicar el commit final por fast-forward en `main`, verificar
`HEAD == main == origin/main` y dejar el árbol limpio. No se promueve la release
personal ni se procesa el corpus real como parte de este handoff.
