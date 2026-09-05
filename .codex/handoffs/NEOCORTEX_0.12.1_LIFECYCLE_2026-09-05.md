# Handoff NeoCortex 0.12.1 — integridad SQLite y lifecycle inicial

## Estado verificado

- Commit publicado: `1de273db26c0e7095ee104d6bb7428061b68e5f3`, con
  `HEAD == main == origin/main` y árbol limpio.
- Release instalada desde ese SHA:
  `0.12.0-1de273db26c0-cp314-linux-x86_64`.
- Rollback inmediato retenido:
  `0.12.0-ad109861fa9f-cp314-linux-x86_64`.
- Receipt: `/home/winterboss/.local/state/Neocortex/state/installation-receipts/20260905T201341.497533Z-install-0.12.0-1de273db26c0-cp314-linux-x86_64.json`.

## Cambios

- WAL y rollback journal se materializan sólo en temporales autocontenidos antes
  de una lectura inmutable, con fences y cleanup que conserva la excepción
  primaria.
- `GlobalResourceCoordinator` limpia admisiones interrumpidas y el orquestador
  persiste fallos de workers derivados de `BaseException`.
- Cada corrida publica `neocortex.run-manifest/v1`; `--status --status-json`,
  API y SDK exponen `neocortex.lifecycle-envelope/v1` en modo read-only.
- Se conservan candidatos necesarios para runs en recuperación y se registran
  transiciones durables de terminación.

## Validación

- Focales Linux: `492 passed, 8 skipped, 12 deselected`.
- Imagen: `3 passed`; inference: `3 passed`.
- Ruff, `compileall`, `py_compile` y `git diff --check`: correctos.
- `release_linux.py verify`: `verified=true`, launcher y manifest alineados,
  smoke instalado y replay/cache comprobados.
- No se tocó el corpus real, KIO, MCP escrito ni proveedores remotos.

## Pendiente

- `NEO-EVO-004` conserva como siguiente tramo el presupuesto durable entre
  workers, recuperación tras terminación abrupta y paridad MCP read-only.
- `NEO-EVO-005` queda abierto para presupuestar bytes/tiempo/cancelación de
  snapshots y reutilizar vistas por generación sin alterar fences.
