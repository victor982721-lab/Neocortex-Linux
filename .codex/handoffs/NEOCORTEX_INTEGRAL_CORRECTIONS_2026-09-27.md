# NEO-CORRECCION-INTEGRAL-20260927

## Entrega verificada

Código publicado: `e5d038cf88e2ad58516c67b681310f9042e9738d`.
Release instalada: `0.14.1-e5d038cf88e2-cp313-linux-x86_64`.
Rollback inmediato: `0.14.1-12dddb20d227-cp313-linux-x86_64`.
SQLite continúa acreditada bajo política
`6abbc22df418fdd8c2ed1a4bc4c8605d5cf604926c6fab63c4c0ed6ca01726ac`.

- PDF enumera una selección mínima estable en su owner; no copia texto ni
  históricos para leer candidatos. Framework usa recursos reales para la
  proyección y una vista por iterador. El límite público de 256 MiB permanece.
- ODT conserva orden/tails y libera XML; XLSX no duplica sharedStrings.
  Firmas selectivas ODT/XLSX, sin invalidación de PPTX o cachés completas.
- Audio separa enumeración/probe/transcripción, conserva etapa de errores,
  publica retries y hace replay cancelable/acotado sin reservar modelos.
- Scheduler evita ciclos productor/grants y admite completions independientes;
  I/O usa identidad común. Identify integrado adapta sólo el caso GIL rápido.
- DOCX valida caché antes de ZIP; Text reduce a una transacción por hit,
  conservando reads/hashes estrictos, receipts e identidad física.
- E2E encontró y corrigió la CLI JSON/Semantic de resume y el registro de rutas
  recuperadas en el coordinador existente. No se relajan presupuestos/fences.

## Evidencia y límites

Expediente local:
`/home/ubuntu/Documents/NeoCortex/Auditorias/2026-09-27-correccion-integral/informe.md`.

Suite principal: 671 passed, 3 skipped, 7 subtests; revalidaciones focales
adicionales (incluidos errores transportables). Ruff/Semgrep pasan. Mypy
121→121 y Pyright122→120, sin errores nuevos; una advertencia preexistente.

Escala sintética de metadatos:100k/300k/1M, con historial y WAL grandes,
dos lectores;1M pasa en19.09s, RSS76,836KiB, vista429,711,360B. Sin WAL
pasa también. No equivale a procesar1M archivos con modelos.

El launcher instalado pasó todas las fases positivas y adversas del E2E:
fría/replay, cambio de una fuente, error/recuperación, cancelación130 y
resume0, modelos locales y Semantic. Owner PDF274,116,608B aprobado con
cache-hit sin extracción. Archive apply/replay usa únicamente Trash de fixture.

A/B Texto1010archivos/3repeticiones: replay2021→1011 transacciones BEGIN,
mismas lecturas/hashes y cero extracciones nuevas. DOCX103fixtures: replay
103→0 aperturas ZIP y cero llamadas reales a extract_docx. No se promete
un porcentaje para el corpus personal. Audio mantiene una barrera acotada
entre fases cuyo coste está medido/declarado.

No se procesó ni reanudó el corpus real. La corrida real6 permanece como
se encontró; sus métricas muestran presión/esperas acumuladas, no permiten
atribuir los42min a subfases concretas ni demostrar un OOM. No se tocaron
modelos originales ni se descargaron dependencias/modelos globales.

El AGENTS.md ajeno conserva su hash y no entra al commit. Build desde copia
Git limpia del main publicado. Un cierre documental posterior sólo cambia
este handoff y CURRENT; conserva el SHA de código y su verificación instalada.
