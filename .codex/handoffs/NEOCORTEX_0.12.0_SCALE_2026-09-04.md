# Handoff NeoCortex 0.12.0 — primera tranche de verificación acotada

## Estado

- La release instalada y vigente sigue siendo `0.11.1-976bae8c9ba1-cp314-linux-x86_64`.
- La primera tranche ejecutable de 0.12 está en el commit de código
  `ea47e46`: `CurationWorkBudget` opcional para la verificación exacta,
  contabilidad incremental de items/archivos/bytes, deadline monotónico,
  cancelación cooperativa y razones bounded para resultados parciales.
- `curate scan` conserva cardinalidad y errores tipados, falla cerrado cuando un
  productor combina error con cobertura completa y la CLI muestra por separado
  `persisted_mode` y `observed_mode`.
- La planificación de duplicados descarta un candidato que cambia durante la
  comparación exacta, evitando grupos falsos; los previews de restore leen
  grants y acciones por una única sesión SQLite fenced, también con WAL activo.

## Validación local

- Foco curation/dedup: **210 passed, 6 skipped, 6 subtests passed**.
- Incluye fixtures de 48 entradas, replay/paginación, límites, cancelación,
  lectura read-only con snapshot temporal y regresión de mutación exacta.
- Ruff, `compileall` y `git diff --check` pasan para las superficies cambiadas.
- No se ejecutó KIO real, no se invocó la Papelera del escritorio y no se tocó
  el corpus personal ni una SQLite productiva.

## Gates restantes para 0.12.0

- Persistir checkpoints durables con root identity, source heads, plan digest,
  cursor, batch digest, presupuesto y publicación únicamente terminal.
- Implementar reanudación/replay contra una corrida limpia, streaming con
  memoria acotada y un benchmark sintético de más de 100,000 elementos.
- Mantener MCP sin `authorize`, `apply`, restore ni conciliación escrita, y
  mantener KIO/GUI de escritorio como gates humanos separados.
- Sólo después de esos gates: versionar `0.12.0`, construir desde el SHA final,
  verificar wheelhouse/manifest/launcher/current/rollback y ejecutar smoke
  público desde la instalación, sin `PYTHONPATH`.
