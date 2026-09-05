# Handoff NeoCortex 0.12.0 — escala, checkpoint y verificación acotada

## Estado

- La implementación de la tranche de escala quedó en los commits funcionales
  `9d1299fe9cd51e5ef74b435d99151d2af39d3103` y
  `d5f63b841a76d83e4c556fdefd0135ead3ade0b8`; la release `0.12.0` ya está
  instalada desde el `main` final, con su rollback inmediato conservado.
- El árbol conserva
  `CurationWorkBudget` opcional para la verificación exacta,
  contabilidad incremental de items/archivos/bytes, deadline monotónico,
  cancelación cooperativa y razones bounded para resultados parciales.
- `curate scan` conserva cardinalidad y errores tipados, falla cerrado cuando un
  productor combina error con cobertura completa y la CLI muestra por separado
  `persisted_mode` y `observed_mode`.
- El contrato `neocortex.curation-checkpoint/v1` ya permite crear, leer, validar
  y reanudar páginas mediante API/SDK, con root/source/plan/snapshot digests,
  batch digest, presupuesto acumulado, escritura no-replace y sucesores
  deterministas; no es un checkpoint DFS de inventario.
- El inventario ya implementa `neocortex.inventory-resume/v1`: orden DFS
  determinista por bytes, cursor que respeta el orden de descendientes,
  identidad de raíz y directorios abiertos, digests de prefijo/directorio/lote,
  owner externo canónico `0600`, lock no bloqueante, avance monotónico y
  reanudación que borra sólo el tail no confirmado. `InventoryWorkBudget` aplica
  límites globales de archivos/bytes, deadline y cancelación antes y después
  de cada transacción bounded.
- El benchmark opt-in completó 100,001 archivos sintéticos, 800,008 bytes,
  98 batches/commits y 14,345 archivos/s, con digest de fixture
  `1e82ea93bc55f9a5e3fa9f35561ee103d2d41bd3aa9554e0c2bb0db65d4e06ab`.
  El recibo final es
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-04-neocortex-012/benchmark-100001-d5f63b8-final.json`, SHA-256
  `c7a5091c3c8de4fbf1ae6d1157f5689b6bf0d9e0bf9231185ee4f8d109b70747`.
- La planificación de duplicados descarta un candidato que cambia durante la
  comparación exacta, evitando grupos falsos; los previews de restore leen
  grants y acciones por una única sesión SQLite fenced, también con WAL activo.

## Validación local

- Foco curation/dedup e inventario: **290 passed, 6 skipped** en la ejecución
  focal de inventario, curación, deduplicación, seguridad y control, y
  **44 passed** en la regresión DFS nueva y
  las suites de generaciones/política; el foco previo de curation/dedup queda
  en **284 passed, 7 skipped, 124 subtests**.
- Incluye fixtures de 48 entradas, replay/paginación, límites, cancelación,
  lectura read-only con snapshot temporal y regresión de mutación exacta.
- Ruff, `compileall` y `git diff --check` pasan para las superficies cambiadas.
- La colección amplia Linux (sin el módulo Windows/NTFS fuera de alcance) terminó
  con **4,651 passed, 17 failed, 76 skipped, 156 subtests**; los fallos son
  dependencias opcionales ausentes en el entorno y la distribución instalada
  anterior, no regresiones del inventario nuevo. El log completo permanece en
  `/tmp/neocortex-full-pytest-20260904.log` para reanudar el diagnóstico si se
  prepara el entorno de análisis correspondiente.
- No se ejecutó KIO real, no se invocó la Papelera del escritorio y no se tocó
  el corpus personal ni una SQLite productiva.

## Verificación posterior al release

- Resolver sólo las dependencias de análisis necesarias para repetir los 17
  fallos ambientales si se requiere una suite integral, sin convertir Windows
  ni proveedores opcionales en un gate de NeoCortex Linux.
- Mantener MCP sin `authorize`, `apply`, restore ni conciliación escrita, y
  mantener KIO/GUI de escritorio como gates humanos separados.
- El siguiente trabajo es `NEO-EVO-004` (`0.13.0`), no una corrección de la
  release 0.12.0 ya verificada.
