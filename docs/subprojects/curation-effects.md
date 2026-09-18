# Curación y efectos

## Ownership

`neocortex/curation` coordina plan, autorización, aplicación, checkpoints,
verificación y recovery; `neocortex/workflow` contiene review, grants, acciones y
retención. `safety` mantiene fronteras físicas compartidas. Consulta
[File Intelligence & Curation](../FILE_INTELLIGENCE_AND_CURATION.md),
[Security](../SECURITY.md) y [Recovery](../RECOVERY.md).

## Fronteras

- Observación, plan, revisión, autorización, aplicación, verificación y recuperación
  son estados diferentes. Una clasificación, score o decisión ReviewTask no autoriza
  efectos; una propuesta probabilística puede seguir siendo útil para revisión.
- `curate authorize` emite sólo un grant append-only vinculado a un plan completo
  y ReviewTasks humanas resueltas, sin `file_actions` ni cambios al corpus.
- El consumo del grant pertenece a `curate apply`; los backends se inyectan
  explícitamente sobre fixtures. No promociones selección automática de backend,
  KIO real, restore de escritorio ni mutación MCP por una tarea de código.
- La ruta de Framework tiene un punto de construcción distinto:
  `FrameworkOrchestrator._execute_initial_actions` crea `KioTrashBackend` en Linux
  cuando `apply_actions` lo requiere, después del gate de plataforma. Esto no
  cambia la inyección explícita de `curate apply` ni acredita KDE/KIO o restore
  de escritorio; esas comprobaciones pertenecen al entorno instalado autorizado.
- `--apply` y `--organization-apply` conservan su rechazo previo a efectos según
  el contrato vigente. Cualquier ampliación requiere una instrucción expresa,
  no una interpretación de la autorización de publicación en main.
- Revalida identidad junto al efecto; fallo incierto deja abstención o recovery,
  no éxito ni reintento ciego. Backup y verificación permanecen separados del permiso.
- Papelera precede al borrado cuando se autorice esa capacidad. Nunca uses
  `gio trash` ni invoques KIO real sin gate explícito y fixtures contenidos;
  la foundation preparada no constituye una promoción al escritorio.

## Validación proporcional

Selecciona pruebas de grants, decisiones CAS, fences físicos, presupuestos,
checkpoints o recuperación según la frontera cambiada. Usa árboles temporales
contenidos y owners aislados, nunca corpus personal ni efectos de escritorio.
Una consulta read-only no exige autorizar efectos ni activar backends.
