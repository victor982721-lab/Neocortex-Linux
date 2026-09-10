# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-09T19:46:13-06:00, `America/Mexico_City`
**Checkout:** `/home/winterboss/Neocortex/Repository`
**Fuente de verdad:** estado vivo de `main`/`origin/main`,
`PENDIENTES.md`, `HISTORIAL.md` y receipts canónicos fechados

**Estado vivo:** la verificación de integración y publicación confirma `HEAD ==
main == origin/main == c6d3985f7a45fc3120bd03e9561195674f2b8ac2` y árbol limpio.
`current` apunta a `0.13.0-c6d3985f7a45-cp314-linux-x86_64`
(`source_sha=c6d3985f7a45fc3120bd03e9561195674f2b8ac2`), el rollback inmediato es
`0.13.0-1567fe46821b-cp314-linux-x86_64` y `.staging` está vacío.

## Alcance actual

La candidata post-0.13 quedó implementada, publicada e instalada. El artefacto
activo mantiene las nueve rutas (`pdf`, `docx`, `office`, `archive`, `text`,
`audio`, `video`, `image`, `code`), inventario, catalogación/deduplicación,
Semantic y Code bajo el lifecycle Framework reanudable, con manifest, stages,
checkpoints, presupuesto global, replay idempotente y publicación staged/CAS.
Code sigue siendo contenido no ejecutable.

La tranche añade sucesores copy-on-write e identidad por digest de contenido,
catalogación v9 con manifest/fence/CAS y triggers inmutables, materialización
binding-aware de Archive/Code, localizadores e hidratación bounded, Context v2
con entidades/relaciones/contradicciones/telemetría, `content-diagnostics/v2`,
`KnowledgeReadBudget`, lectura fenced de curación, sincronización de caches
move/rename sólo en fixtures, panel GUI read-only y el contrato de principal
autenticado. MCP no recibe autorización, aplicación ni conciliación escrita.

La suite integral desde este SHA terminó con **6959 pasadas, 67 omitidas y 42
subtests**. Ruff y Mypy quedaron limpios; Pyright terminó con 0 errores y 123
warnings clasificados; Semgrep terminó con 0 hallazgos en 7 reglas. La
validación focal adicional de los contratos nuevos permanece cubierta por sus
regresiones y no sustituye la suite integral.

La release verificó manifest, árbol, launcher y procedencia. Sus hashes son:

- manifest `6f2a44f6e7ab937a3b92fac3f71be196cbffa707eddb21c7fac58972b7823b84`;
- árbol `6e67d6c6314268dacab90a69e17d755b7c9caa164301987b30b52a42706196be`;
- launcher `18d4bbee8d79045f9749ee27cc5f5a8cad13d91a53148ef139518c64f484eff1`;
- wheel `7e7a73eb30c7ceaa026c2d70a21f0e218abd08151a608db74e7ba74185f479aa`;
- manifest de fuente `a5d3e05123eb4a7fe3bb4a742eecc3eb3c8e85cdc0a5dc8eb0fe6de5f6b8d50f`.

`tools/release_linux.py verify --corpus-root /tmp/neocortex-post013-release-corpus`
terminó `verified=true`. El receipt de instalación es
`/home/winterboss/.local/state/Neocortex/state/installation-receipts/20260910T014132.624563Z-install-0.13.0-c6d3985f7a45-cp314-linux-x86_64.json`.
La verificación de modelos existentes quedó `runtime_verified=false`; no se
adquirieron modelos nuevos.

El smoke instalado sobre 23 fixtures temporales terminó `RC1=0` y `RC2=0` en
nueve rutas, con Semantic completo usando modelos locales existentes, Archive y
Text con `cache_hits` en replay y `action_mode=dry-run`; el alcance Code
`projects` excluyó el archivo fuera de un proyecto configurado, sin ejecutarlo.
Los hashes de bytes de las fixtures y del estado temporal permanecieron
estables entre las dos corridas. El primer smoke con cache de modelos aislado,
que terminó `RC1=2`/`RC2=2` por `SemanticModelUnavailableError`, permanece
registrado como intento incompleto y no se cuenta como éxito.

## Gates y siguiente paso

1. Mantener `current` y el rollback inmediato; cualquier cambio de
   código/configuración/build exige resolver un SHA final nuevo y repetir el
   procedimiento de release desde el artefacto.
2. Mantener `NEO-FUN-002` en `ESPERA_TERCERO`: CA-12 sigue `PARTIAL` y CA-15
   conserva 33.1% de excerpt, sin reabrir R1–R4 ni hacer tuning.
3. Mantener `NEO-AUTH-001` en `EN_CURSO`: falta principal autenticado confiable
   para ampliar MCP; KIO/desktop y cualquier efecto físico conservan su gate
   humano separado.
4. Mantener `NEO-SEM-002` en `ESPERA_VICTOR`: la generación 17 y la política de
   retención no se reanudan, borran ni compactan por inferencia.
5. No abrir corpus personal, SQLite productiva, KIO real, MCP mutante ni
   GitHub Actions durante esta tranche; el contrato preparado de fixtures no
   acredita promoción física.

El cierre de esta implementación se limita a código, documentación, SSOT y
release candidata verificable; las decisiones y gates anteriores siguen siendo
pendientes independientes.

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
