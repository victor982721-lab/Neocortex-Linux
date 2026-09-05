# Handoff NeoCortex 0.13.0 — presupuesto durable y snapshots bounded

## Estado verificado

- Commit funcional publicado: `408562626016eec3888559259c5ded65afd9c63e`;
  el test MCP posterior quedó en `78f9aa2cb31c`.
- Release instalada desde ese SHA final:
  `0.12.0-78f9aa2cb31c-cp314-linux-x86_64`.
- Rollback inmediato retenido:
  `0.12.0-f94a568f1ac1-cp314-linux-x86_64`.
- Receipt: `/home/winterboss/.local/state/Neocortex/state/installation-receipts/20260905T212422.401852Z-install-0.12.0-78f9aa2cb31c-cp314-linux-x86_64.json`.

## Cambios

- `neocortex.run-budget/v1` persiste límites de items/bytes/tiempo, reservas
  idempotentes por ruta, cancelación durable, deadline, consumo y recuperación.
- El orquestador reserva el workload de cada ruta antes de crear el worker y
  revisa cancelación/deadline durante la espera, sin cambiar el contrato de
  rutas antiguas que no tienen manifest.
- Snapshots SQLite aceptan límites bounded de bytes temporales, tiempo de
  preparación y cancelación, con métricas de intentos, generación y reutilización.
- MCP ofrece `lifecycle_status` como consulta read-only de manifest, budget,
  recovery y rutas, sin iniciar, reanudar, autorizar ni mutar corridas.

## Validación

- Focal Linux: `501 passed, 8 skipped, 12 deselected`.
- Imagen: `3 passed`; inference: `3 passed`.
- Ruff, `compileall`, `py_compile` y `git diff --check`: correctos.
- `release_linux.py verify`: `verified=true`; smoke/replay instalado muestran
  `type_cache_hits=1` en la segunda corrida y status con los schemas de
  lifecycle y budget.
- El servidor instalado registra `lifecycle_status`; no se tocó el corpus real,
  KIO ni MCP escrito.

## Siguiente alcance

- Integrar Semantic y publicación cross-owner en el manifest completo de `--all`,
  y ampliar recuperación tras terminación abrupta con fixtures de 20–50 entradas.
