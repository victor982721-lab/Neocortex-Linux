# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-09, `America/Mexico_City`
**Checkout:** `/home/winterboss/Neocortex/Repository`
**Fuente de verdad:** estado vivo de `main`/`origin/main`,
`PENDIENTES.md`, `HISTORIAL.md` y receipts canónicos fechados

**Estado vivo:** la última verificación de integración confirmó `HEAD == main ==
origin/main == 1567fe46821b923be5e90ba4223abdaf81a9924c` y árbol limpio antes de
esta actualización documental, que es docs-only. `current` apunta a
`0.13.0-1567fe46821b-cp314-linux-x86_64` (`source_sha=1567fe46821b923be5e90ba4223abdaf81a9924c`),
el rollback inmediato es `0.13.0-42115cd060f3-cp314-linux-x86_64` y `.staging`
está vacío.

## Alcance actual

El artefacto de NeoCortex 0.13.0 está instalado en `current` y contiene el
lifecycle durable de `--all`. El objetivo coordina las nueve rutas (`pdf`,
`docx`, `office`, `archive`, `text`, `audio`, `video`, `image`, `code`),
inventario, catalogación/deduplicación, Semantic y Code bajo un run Framework
reanudable, con manifest, stages, checkpoints, presupuesto global, replay
idempotente y publicación staged/CAS. Code sigue siendo contenido no ejecutable.

La aceptación integral C0–C7 queda confirmada exactamente sobre
`source_sha=1567fe46821b923be5e90ba4223abdaf81a9924c`: la suite final registró
**6917 pasadas, 68 omitidas y 42 subtests**, y los dos tests de empaquetado se
repitieron después de retirar una ruta privada del changelog. Ruff, Mypy,
Pyright y Semgrep terminaron sin errores/hallazgos bloqueantes; las advertencias
Pyright existentes permanecen clasificadas. Esta evidencia no se transfiere a
otros SHAs.

La fuente contiene el vertical de contratos `neocortex.run-manifest/v1`,
`neocortex.run-budget/v1`, `neocortex.lifecycle-stage/v1` y
`neocortex.lifecycle-envelope/v1`, además de lecturas bounded de estado. La
evidencia histórica de `source_sha=1bb73907d9ab...` permanece separada; la
aceptación vigente corresponde al receipt de validación y al expediente E2E
fechados del SHA `1567fe4`.

La release canónica se construyó desde `1567fe4`, verificó manifest, árbol,
launcher y procedencia, y pasó `tools/release_linux.py verify` con corpus de
smoke vacío. El receipt de instalación del artefacto es
`/home/winterboss/.local/state/Neocortex/state/installation-receipts/20260909T215132.124961Z-install-0.13.0-1567fe46821b-cp314-linux-x86_64.json`;
el expediente E2E final está en
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-09-knowledge-tranche-1567fe4/`.

El piloto instalado sobre 37 fixtures aisladas pasó dos corridas (`RC1=0`,
`RC2=0`), nueve rutas, Semantic `completed`, replay con cachés y bytes de
fixtures sin cambios. La tranche de Knowledge verificó que los miembros ZIP
son virtuales, conservan localizadores y no exponen identidad física; el límite
de cursores MCP operacionales quedó alineado a 8 KiB.

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

Estas observaciones son históricas y permanecen separadas de la aceptación
vigente del lifecycle 0.13, que corresponde al SHA `1567fe4` y su artefacto
instalado.

## Gates y siguiente paso

1. Mantener `current` y el rollback inmediato, sin iniciar `agent serve` sobre
   una release que deba retirarse durante futuras promociones.
2. Mantener separadas Semantic 17, R1–R4 y cualquier operación física o poda de
   estado; no se incluyen en el cierre de este lifecycle.

Los warnings y wheels de analizadores ausentes de la línea previa conservan su
clasificación independiente; no reabrir Windows, R1–R4 ni las superficies de
mutación para cerrar este objetivo.
