# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-08, `America/Mexico_City`
**Checkout:** `/home/winterboss/Neocortex/Repository`
**Fuente de verdad:** estado vivo de `main`/`origin/main`,
`PENDIENTES.md`, `HISTORIAL.md` y receipts canónicos fechados

## Alcance actual

NeoCortex 0.13.0 está en desarrollo para cerrar el lifecycle durable de
`--all`. El objetivo coordina las nueve rutas (`pdf`, `docx`, `office`,
`archive`, `text`, `audio`, `video`, `image`, `code`), inventario,
catalogación/deduplicación, Semantic y Code bajo un run Framework reanudable,
con manifest, stages, checkpoints, presupuesto global, replay idempotente y
publicación staged/CAS. Code sigue siendo contenido no ejecutable.

La fuente contiene el vertical de contratos `neocortex.run-manifest/v1`,
`neocortex.run-budget/v1`, `neocortex.lifecycle-stage/v1` y
`neocortex.lifecycle-envelope/v1`, además de lecturas bounded de estado. Eso es
estado de fuente, no evidencia de aceptación integral, instalación o
promoción. No se declara una release 0.13 instalada ni una corrida `--all`
completa hasta cerrar C0–C7 desde el artefacto final.

Semantic se registra dentro del mismo lifecycle, pero el Semantic pesado es
opt-in. Archive, Code y Video son fuentes Semantic explícitas; `--all` coordina
sus rutas de contenido sin inferir una indexación Semantic pesada. Ausencias de
modelos, herramientas o rutas producen `unavailable`/`blocked` e `incomplete`,
nunca éxito vacío ni skip silencioso.

Linux/Kubuntu es la única plataforma activa. No se usa GitHub Actions,
proveedores remotos, KIO real, MCP escrito ni el corpus personal durante el
piloto. La autoridad física permanece fuera de este alcance.

## Evidencia previa y límites de interpretación

La evidencia de estabilización anterior permanece disponible, pero no acredita
el cierre de 0.13:

- la suite, Ruff, Mypy, Pyright y Semgrep de la línea previa están registradas
  en `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-08-stabilization-3b00d03/`;
- la release de la línea previa tuvo doble-build, verificación de
  manifest/launcher y smoke headless aislado, con `current` y rollback inmediato;
- el piloto previo usó 28 fixtures heterogéneas, presupuesto por proceso de
  180 s y replay, incluido el smoke de audio local;
- el hash de metadata del corpus real permaneció idéntico en esa operación y
  `.staging` quedó vacío.

Estas observaciones son históricas y permanecen separadas de la aceptación del
lifecycle 0.13. No hay aquí un receipt de instalación/promoción 0.13 ni una
matriz C0–C7 que permita presentarla como terminada.

## Gates y siguiente paso

1. Integrar el lifecycle completo y verificar C0–C7 sobre 20–50 fixtures
   temporales, con las nueve rutas, dos reanudaciones, replay terminal,
   dependencias ausentes, límites, cancelación, drift y paridad CLI/API/SDK/MCP.
2. Confirmar que cada run conserva root/identidad, snapshot, configuración,
   owner heads, checkpoints, presupuesto restante y capacidades de replay, y
   que Semantic/Code no publican epochs parciales o ambiguos.
3. Ejecutar Pytest, Ruff, Mypy, Pyright y Semgrep individualmente y la suite
   integral después de integrar los cambios; registrar resultados y hashes fuera
   de `docs/`.
4. Sólo después de la aceptación de fuente, construir dos veces, verificar
   wheel/manifest/launcher/`source_sha`, instalar con HOME/XDG aislados y
   `NEOCORTEX_TEST_PYTHON`, y probar smoke/resume/replay desde el artefacto. La
   promoción exige además `HEAD == main == origin/main`, árbol limpio, staging
   vacío y evidencia de corpus intacto.

Los warnings y wheels de analizadores ausentes de la línea previa conservan su
clasificación independiente; no reabrir Windows, R1–R4 ni las superficies de
mutación para cerrar este objetivo.
