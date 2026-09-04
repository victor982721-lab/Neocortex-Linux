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
- `curate scan`, `curate plan` y `curate verify` consultan evidencia publicada y
  archivos regulares sin crear efectos; `curate review`, `curate decide` y
  `curate authorize` escriben sólo el estado contractual previsto. El tramo
  físico `apply → verify → reconcile` aún no tiene consumidor y
  `--apply`/`--organization-apply` se abstienen.

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

## 0.10.x implementado en el checkout

1. `verification_mode` queda persistido y separa candidatos fast, parciales y
   bytewise verificados.
2. `curate scan`/`curate verify` exponen snapshots, `source_heads`, identidad,
   límites y razones de abstención, sin `file_actions`, KIO ni mutación del corpus.
3. Los grants nuevos persisten heads/digests de ReviewTask; autorización por MCP
   continúa omitida hasta resolver un principal autenticado.
4. El gate restante es la consolidación final de superficies, regresiones SQLite
   y release 0.10.x; `apply` físico pertenece a 0.11.0.

Los riesgos de fences SQLite, restore de owners ausentes, carreras de workers,
aislamiento de procesos y reproducibilidad de release siguen abiertos y deben
resolverse en sus superficies respectivas antes de `0.11.0`.

## Pendientes relacionados

- `NEO-FIC-001`, `NEO-AUTH-001` y `NEO-CUR-001` permanecen `EN_CURSO`.
- `NEO-GIT-001` y `NEO-DOC-001` cubren esta migración y su reconciliación
  documental; `NEO-EVO-001` queda para el siguiente vertical.
- La evidencia detallada vive fuera de este handoff y se enlaza desde
  `PENDIENTES.md` cuando sea necesaria.
