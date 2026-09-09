# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-09, `America/Mexico_City`
**Checkout:** `/home/winterboss/Neocortex/Repository`
**Fuente de verdad:** estado vivo de `main`/`origin/main`,
`PENDIENTES.md`, `HISTORIAL.md` y receipts canónicos fechados

**Estado vivo:** el checkout actual está en `main` con `HEAD == main ==
origin/main == 0a28e92f91b0906dc8f16669783268ac1c42e13c`; la última verificación
previa a esta reconciliación confirmó el árbol limpio; el estado debe volver a
verificarse al integrar los cambios documentales y de código. Este checkout
contiene la reconciliación documental posterior al
artefacto y no es el `source_sha` de la release activa. `current` apunta a
`0.13.0-42115cd060f3-cp314-linux-x86_64` (`source_sha=42115cd060f347a8f95aec8b46a92c1c502d13c8`),
el rollback inmediato es `0.13.0-1bb73907d9ab-cp314-linux-x86_64` y `.staging`
está vacío.

## Alcance actual

El artefacto de NeoCortex 0.13.0 está instalado en `current` y contiene el
lifecycle durable de `--all`. El objetivo coordina las nueve rutas (`pdf`,
`docx`, `office`, `archive`, `text`, `audio`, `video`, `image`, `code`),
inventario, catalogación/deduplicación, Semantic y Code bajo un run Framework
reanudable, con manifest, stages, checkpoints, presupuesto global, replay
idempotente y publicación staged/CAS. Code sigue siendo contenido no ejecutable.

La aceptación integral del lifecycle queda condicionada a que la raíz confirme
la suite C0–C7 y calidad exactamente sobre `source_sha=42115cd...`. La evidencia
integral disponible de **6912 pasadas, 59 omitidas y 42 subtests** corresponde a
la línea previa `source_sha=1bb73907d9ab...`; no se transfiere por similitud de
artefacto ni por el receipt de instalación. Mientras esa corrida exacta no esté
registrada, este handoff no declara 0.13 aceptado.

La fuente contiene el vertical de contratos `neocortex.run-manifest/v1`,
`neocortex.run-budget/v1`, `neocortex.lifecycle-stage/v1` y
`neocortex.lifecycle-envelope/v1`, además de lecturas bounded de estado. La
evidencia registrada para C0–C7 en la línea previa (`source_sha=1bb73907d9ab...`)
reporta **6912 pasadas, 59 omitidas y 42 subtests**, Ruff/Mypy/Pyright sin
errores en las cohortes afectadas y Semgrep sin hallazgos de producto; la
evidencia está en
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-08-lifecycle-013/`.

La release canónica se construyó desde `42115cd`, verificó manifest, árbol,
launcher y procedencia, y pasó `tools/release_linux.py verify` con corpus de
smoke vacío. El receipt de instalación del artefacto es
`/home/winterboss/.local/state/Neocortex/state/installation-receipts/20260909T165618.879737Z-install-0.13.0-42115cd060f3-cp314-linux-x86_64.json`;
el expediente E2E es
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-09-lifecycle-013/`.
Estas comprobaciones prueban la procedencia y el estado instalado del artefacto,
no sustituyen la validación fuente-exacta pendiente.

El piloto instalado sobre 29 fixtures aisladas pasó dos corridas (`RC1=0`,
`RC2=0`), nueve rutas, Semantic `completed`, replay con cachés y bytes de
fixtures sin cambios. La corrección del lock integrado quedó incluida en el
artefacto; los procesos `agent serve` bloqueantes se cerraron con `SIGTERM`
antes de promover.

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

- la suite, Ruff, Mypy, Pyright y Semgrep de la línea previa
  (`source_sha=1bb73907d9ab...`) están registradas
  en `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-08-stabilization-3b00d03/`;
- la release de la línea previa tuvo doble-build, verificación de
  manifest/launcher y smoke headless aislado, con `current` y rollback inmediato;
- el piloto previo usó 28 fixtures heterogéneas, presupuesto por proceso de
  180 s y replay, incluido el smoke de audio local;
- el hash de metadata del corpus real permaneció idéntico en esa operación y
  `.staging` quedó vacío.

Estas observaciones son históricas y permanecen separadas de la aceptación del
lifecycle 0.13. El expediente fechado del artefacto `42115cd` conserva la
evidencia de instalación/piloto, pero la aceptación fuente-exacta sigue
pendiente de confirmación de la raíz.

## Gates y siguiente paso

1. Confirmar la validación exacta de `42115cd` antes de declarar aceptado el
   lifecycle 0.13; no inferirla del receipt de instalación ni de la suite de
   `1bb73907d9ab`.
2. Mantener `current` y el rollback inmediato, sin iniciar `agent serve` sobre
   una release que deba retirarse durante futuras promociones.
3. Mantener separadas Semantic 17, R1–R4 y cualquier operación física o poda de
   estado; no se incluyen en el cierre de este lifecycle.

Los warnings y wheels de analizadores ausentes de la línea previa conservan su
clasificación independiente; no reabrir Windows, R1–R4 ni las superficies de
mutación para cerrar este objetivo.
