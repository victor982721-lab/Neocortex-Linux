# Handoff NeoCortex 0.11.0 — apply grant-bound

## Estado

- El vertical grant-bound está implementado en `neocortex/curation/application.py`.
- `AuthorizationGrant` conserva manifests opcionales de raíz, source heads y
  efectos físicos. Un grant legacy sin esos manifests sigue siendo legible, pero
  `apply_authorization_grant` lo rechaza antes de crear `file_actions`.
- `PosixRenameBackend` usa una operación same-filesystem no-replace acotada a
  fixtures; `KioTrashBackend` sólo funciona con runner/verifier inyectados y
  exige evidencia estructurada de Papelera.
- `reconcile_curation_actions` observa y registra eventos bounded, append-only e
  idempotentes, sin reintentar ni convertir observación en autorización.
- La CLI expone `curate apply` y `curate reconcile`, pero `curate apply` falla
  cerrada si no recibe un backend y un run firmado, por lo que no toca el corpus
  instalado ni invoca KIO automáticamente. MCP mantiene fuera apply y
  conciliación escrita.

## Validación local

- Fixture temporal con duplicado exacto: apply `trash` produce
  `started → applying → applied`, receipt ligado a grant/efecto y replay sin
  segunda llamada al backend.
- Mutación de bytes conservando tamaño/mtime: preflight `blocked`, cero
  `file_actions` y cero llamadas al backend.
- Rename fixture: destino existente no se sobreescribe; destino same-filesystem
  inexistente se mueve y conserva identidad/hash.
- Recovery: timeout/resultado ambiguo queda `recovery_required`, replay no
  reintenta y reconcile registra una única observación idempotente.
- Suite focal aplicada: `tests/test_curation_application.py`, autorización,
  recovery, KIO y lifecycle pasan; la matriz MCP depende de `mcp` opcional no
  instalado en el entorno de desarrollo.

## Gates restantes

- No se ejecutó KIO real ni se modificó la Papelera del escritorio.
- La GUI aún no presenta grant/intento; la sincronización posterior de catálogo,
  caches y Semantic queda para el siguiente corte.
- La publicación y release 0.11.0 deben ejecutarse desde el SHA final
  (`da48afe` como base actual), con el
  wheelhouse local autenticado, manifest, launcher, smoke/replay y rollback.
- Mantener los pendientes `NEO-FIC-001`, `NEO-AUTH-001` y `NEO-CUR-001` abiertos
  hasta completar esos gates, y registrar el cierre evolutivo con un ID nuevo en
  `PENDIENTES.md`.
