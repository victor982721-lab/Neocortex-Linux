# Curación y efectos

## Ownership

`neocortex/curation` coordina plan, checkpoints, verificación y recovery;
`neocortex/workflow` contiene acciones, evidencia y retención. Los efectos
automáticos seguros pertenecen a la corrida estándar con `--apply`. `safety` mantiene fronteras físicas compartidas. Consulta
[File Intelligence & Curation](../FILE_INTELLIGENCE_AND_CURATION.md),
[Security](../SECURITY.md) y [Recovery](../RECOVERY.md).

## Fronteras

- Observación, plan, aplicación, verificación y recuperación son estados
  diferentes. La clasificación automática no convierte score o evidencia en
  permiso; `UNKNOWN` y cualquier incertidumbre se conservan como KEEP.
- `--apply` es la autorización explícita del usuario para propuestas de alta
  confianza dentro de la raíz seleccionada. No existe una cola humana
  obligatoria ni una ceremonia de autorización separada.
- Cada efecto revalida identidad, raíz, no-follow y límites junto a la frontera
  física. Un fallo incierto deja abstención o `recovery_required`, no éxito ni
  reintento ciego. Receipts y recovery permanecen separados del plan.
- La ruta de Framework puede preparar KIO sólo cuando la corrida `--apply` lo
  requiere y pasa su gate de plataforma. No se selecciona un backend físico por
  una consulta read-only ni por MCP.
- Papelera precede al borrado cuando la política lo permite. Nunca uses
  `gio trash` ni invoques KIO real sin gate explícito y fixtures contenidos.

## Validación proporcional

Selecciona pruebas de fences físicos, presupuestos, checkpoints o recuperación
según la frontera cambiada. Usa árboles temporales
contenidos y owners aislados, nunca corpus personal ni efectos de escritorio.
Una consulta read-only no exige autorizar efectos ni activar backends.
