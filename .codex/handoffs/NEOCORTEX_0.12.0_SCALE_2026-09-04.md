# Handoff NeoCortex 0.12.0 — primera tranche de verificación acotada

## Estado

- La release instalada y vigente sigue siendo `0.11.1-976bae8c9ba1-cp314-linux-x86_64`.
- La primera tranche ejecutable de 0.12 está publicada en `main` y
  `origin/main` (`c9c043d8179920ed84703061be6b2cd0b6cbca35`), con
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
- El benchmark opt-in completó 100,001 archivos sintéticos, 800,008 bytes,
  98 batches/commits y 18,558 archivos/s, con digest de fixture
  `1e82ea93bc55f9a5e3fa9f35561ee103d2d41bd3aa9554e0c2bb0db65d4e06ab`.
  El recibo final es `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-04-neocortex-012/benchmark-100001-c9c043d.json`.
- La planificación de duplicados descarta un candidato que cambia durante la
  comparación exacta, evitando grupos falsos; los previews de restore leen
  grants y acciones por una única sesión SQLite fenced, también con WAL activo.

## Validación local

- Foco curation/dedup: **232 passed, 6 skipped, 6 subtests passed**.
- Incluye fixtures de 48 entradas, replay/paginación, límites, cancelación,
  lectura read-only con snapshot temporal y regresión de mutación exacta.
- Ruff, `compileall` y `git diff --check` pasan para las superficies cambiadas.
- No se ejecutó KIO real, no se invocó la Papelera del escritorio y no se tocó
  el corpus personal ni una SQLite productiva.

## Gates restantes para 0.12.0

- Implementar checkpoint/reanudación DFS de inventario y eliminar la
  reconstrucción O(n) por página, conservando el contrato page-level ya
  publicado.
- Integrar límites globales de disco temporal y cancelación durante flush/commit,
  y comparar el resume de inventario contra una corrida limpia.
- Mantener MCP sin `authorize`, `apply`, restore ni conciliación escrita, y
  mantener KIO/GUI de escritorio como gates humanos separados.
- Sólo después de esos gates: versionar `0.12.0`, construir desde el SHA final,
  verificar wheelhouse/manifest/launcher/current/rollback y ejecutar smoke
  público desde la instalación, sin `PYTHONPATH`.
