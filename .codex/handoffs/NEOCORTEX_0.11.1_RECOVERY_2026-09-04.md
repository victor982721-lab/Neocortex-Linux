# Handoff NeoCortex 0.11.1 — recovery y restore grant-bound

## Estado

- El siguiente corte está implementado en `neocortex/curation/recovery.py` y
  `neocortex/api/curation_recovery_api.py`.
- `curate recovery status` y `curate restore preview` usan lectura fenced y no
  crean sidecars ni migran owners.
- `restore_curation_action` exige actor y token derivado exactamente del
  `action_id` y del receipt original, crea un `restore_curation` intent antes
  del efecto y conserva `applying → applied|recovery_required`.
- `PosixRestoreBackend` verifica root/Trash, `.trashinfo`, identidad, hash,
  mismo filesystem y destino inexistente, usa `renameat2(RENAME_NOREPLACE)` y
  elimina metadata sólo después de verificar el archivo restaurado.
- MCP continúa sin apply, restore, authorize ni conciliación escrita. La CLI
  sólo selecciona un backend mediante una integración explícita, nunca desde el
  estado instalado por defecto.

## Validación local

- Fixtures cubren preview read-only, confirmación incorrecta, restore exitoso,
  replay `already_restored`, colisión de destino, crash después del rename,
  recovery conciliable, `.trashinfo`, hashes y KIO evidence estructurada.
- Focos actuales: tests de curation application/recovery API/recovery/KIO,
  fachadas públicas, CLI, documentación y compilación pasan con Ruff.
- El restore real de escritorio y la Papelera real no se ejecutaron.

## Gates restantes

- Publicar el SHA final y construir la release `0.11.1` desde ese SHA usando el
  wheelhouse Linux local, con manifest, launcher, smoke/replay y rollback.
- Mantener el restore de owners SQLite separado de este restore de archivos,
  y no usar `os.replace` ni eliminar owners ausentes por inferencia.
- Preparar el salto 0.12 con streaming, checkpoints, presupuesto global y
  cancelación sobre fixtures sintéticos, sin escalar al corpus personal.
