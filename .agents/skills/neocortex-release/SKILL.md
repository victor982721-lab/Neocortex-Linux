---
name: neocortex-release
description: Construir, instalar y verificar una release Linux de NeoCortex cuando esa entrega esté expresamente en el alcance. Mantiene separados publicación de código, corpus, modelos y promoción.
---

# Release Linux bajo alcance explícito

Lee el [contrato de distribución](../../../docs/subprojects/development-release.md)
y la ayuda del `tools/release_linux.py` vigente antes de elegir argumentos.

- Confirma SHA, Git, evidencia de validación y quiescencia real de los owners,
  checkout, wrapper y cgroup afectados; no detengas tareas ajenas por instalar.
- Resuelve wheelhouse y dependencias locales autenticadas. Usa el instalador
  existente y su frontera de promoción; no descargues modelos ni uses red como
  fallback, ni conviertas ausencia en una instalación global.
- Construye desde el SHA final y verifica manifest, launcher y procedencia.
  El smoke usa el comando instalado fuera del checkout, fixtures y HOME/XDG/estado
  aislados, sin `PYTHONPATH`; `NEOCORTEX_TEST_PYTHON` apunta al artefacto cuando aplique.
- Repite sólo la entrada necesaria para demostrar replay/caché/idempotencia.
  Si una validación previa se reutiliza por equivalencia documental, demuestra
  el diff exacto y conserva sus dos SHA y evidencia, sin repetir un gate costoso por rutina.
- Sólo tras verificación conserva current y rollback inmediato, sin borrar una
  release activa ni ampliar retención a otros archivos. La raíz registra cierre
  y límites; nunca presenta un candidato construido como instalación verificada.

La autorización de una release incluida en la tarea no se vuelve a pedir, pero
no cubre corpus, privacidad, nuevos modelos ni operaciones destructivas adicionales.
