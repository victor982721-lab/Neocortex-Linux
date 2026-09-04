# NeoCortex — handoff operativo vigente

> Actualizado: 2026-09-04, America/Mexico_City. Fuente canónica: `main` del
> repositorio `victor982721-lab/Neocortex-Linux`.

## Estado comprobado

- El checkout y el remoto canónico están en `main` con la línea vigente de
  NeoCortex; el repositorio anterior se conserva como `legacy` sin cambios.
- La versión declarada continúa en `0.9.0`; la release activa se mantiene
  ligada al SHA anterior hasta que un cambio ejecutable o una instalación desde
  un SHA final exija otra promoción.
- Linux/Kubuntu es la única plataforma activa. No se usa GitHub Actions, no se
  envían corpus o secretos a proveedores externos y no se procesa el corpus real
  durante los pilotos.
- `curate plan`, `curate review`, `curate decide` y `curate authorize` escriben
  sólo el estado contractual previsto; `apply → verify → reconcile` aún no
  tiene consumidor físico y `--apply`/`--organization-apply` se abstienen.

## Decisiones vigentes

1. Code se trata como contenido y las herramientas de desarrollo permanecen
   fuera del runtime productivo.
2. La autoridad sigue separada entre evidencia, ReviewTask, AuthorizationGrant,
   efecto, verificación y recovery.
3. Un `AuthorizationGrant` no demuestra un efecto físico y no crea
   `file_actions`.
4. Una huella rápida sólo reduce candidatos; `trash` requiere comparación
   byte-a-byte verificable.
5. La foundation KIO permanece preparada pero no integrada ni ejecutada contra
   la Papelera real.

## Siguiente gate: 0.10.x

1. Persistir `verification_mode` y separar candidatos fast de duplicados
   bytewise verificados.
2. Completar `curate scan`/`curate verify` con snapshots, source heads,
   identidad física, paginación, límites y razones de abstención.
3. Persistir los heads/digests de ReviewTask que se validaron al emitir un
   grant, sin exponer autorización por MCP mientras falte un principal
   autenticado.
4. Validar replay e idempotencia sobre fixtures de 20–50 elementos, sin
   `file_actions`, KIO ni mutación del corpus.

Los riesgos de fences SQLite, restore de owners ausentes, carreras de workers,
aislamiento de procesos y reproducibilidad de release siguen abiertos y deben
resolverse en sus superficies respectivas antes de `0.11.0`.

## Pendientes relacionados

- `NEO-FIC-001`, `NEO-AUTH-001` y `NEO-CUR-001` permanecen `EN_CURSO`.
- `NEO-GIT-001` y `NEO-DOC-001` cubren esta migración y su reconciliación
  documental; `NEO-EVO-001` queda para el siguiente vertical.
- La evidencia detallada vive fuera de este handoff y se enlaza desde
  `PENDIENTES.md` cuando sea necesaria.
