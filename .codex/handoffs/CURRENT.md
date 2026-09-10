# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-10T07:38:26-06:00, `America/Mexico_City`
**Checkout:** `/home/winterboss/Neocortex/Repository`
**Fuente de verdad:** estado vivo de `main`/`origin/main`,
`PENDIENTES.md`, `HISTORIAL.md` y receipts canónicos fechados

**Estado vivo:** la verificación de integración confirma `HEAD == main ==
origin/main` y árbol limpio. `current` apunta a
`0.13.0-c2066dcbd967-cp314-linux-x86_64`
(`source_sha=c2066dcbd967e8804df559f1fc9d15abdbd783bc`), el rollback inmediato es
`0.13.0-38283df08491-cp314-linux-x86_64` y `.staging` está vacío. Los commits
posteriores al SHA ejecutable son documentales; no se reescribió historia.

## Alcance actual

La tranche post-0.13 quedó implementada, publicada e instalada en el artefacto
activo desde `c2066dcbd967e8804df559f1fc9d15abdbd783bc`; mantiene las nueve rutas
(`pdf`, `docx`, `office`, `archive`, `text`, `audio`, `video`, `image`, `code`),
inventario, catalogación/deduplicación, Semantic y Code bajo el lifecycle
Framework reanudable, con manifest, stages, checkpoints, presupuesto global,
replay idempotente y publicación staged/CAS. Code sigue siendo contenido no
ejecutable.

La tranche añade sucesores copy-on-write e identidad por digest de contenido,
catalogación v9 con manifest/fence/CAS y triggers inmutables, materialización
binding-aware de Archive/Code, localizadores e hidratación bounded, Context v2
con entidades/relaciones/contradicciones/telemetría, `content-diagnostics/v2`,
`KnowledgeReadBudget`, lectura fenced de curación, sincronización de caches
move/rename sólo en fixtures, panel GUI read-only y el contrato de principal
autenticado. MCP no recibe autorización, aplicación ni conciliación escrita.

La tranche incorpora los límites compartidos de `KnowledgeReadBudget` para
curación `scan/verify/apply` y la proyección pública estable de evidencia
Knowledge v1 con exports API/SDK. Sus focales pasaron y el artefacto instalado
corresponde al SHA final `c2066dc`.

La suite integral anterior terminó con **6959 pasadas, 67 omitidas y 42
subtests**. Para la nueva tranche, los focales fueron **42** pasadas de curación
y **202** de Knowledge/API; Ruff quedó limpio y Pyright terminó sin errores en
el foco. Tras la corrección determinista del reloj, la suite completa desde el
checkout final terminó **6984 pasadas, 60 omitidas y 42 subtests**, sin fallos.
El test de deadline corregido pasó **20/20**.

La release activa verificó manifest, árbol, launcher y procedencia. Sus hashes son:

- manifest `2f071256ead3f78b4dd3ce6e1758abe7c8cc1474d7de3e67995a6507362e9418`;
- árbol `73854c3b95d96a43c79c460ecbcabc0e990410dd7acfa16d6e035ac01631ef1a`;
- launcher `59186ce96555fd8d43a795c30337b92e3ce3aa2bfb43d4717ecc750094ea3a1a`;
- wheel `7d2fe292a55e6c49d43363f18011907ff79b6781c116bc44dea791395d1d8c40`;
- manifest de fuente `19f2cce725bb9a7d1a60e05a75b3cb4d3401f1f35309d9bf4abb7bd2c517e1b9`.

La release final quedó promovida al namespace canónico después de cerrar los
`agent serve` idle de las releases históricas; `agent serve` quedó en cero, no se
modificó `current` manualmente y se conservó sólo el rollback inmediato.

La verificación canónica del artefacto activo terminó `verified=true` con corpus
efímero, modelos locales preparados, launcher, manifest y rollback comprobados.
La evidencia de smoke/replay aislado de 23 fixtures y nueve rutas de la candidata
anterior permanece conservada por separado; el alcance Code `projects`
excluyó el archivo fuera de un proyecto configurado, sin ejecutarlo. Los hashes
de bytes de fixtures y estado temporal permanecieron estables entre corridas.
El primer smoke con cache de modelos aislado, `RC1=2`/`RC2=2` por
`SemanticModelUnavailableError`, permanece registrado como intento incompleto.

## Gates y siguiente paso

1. Conservar `current` en `0.13.0-c2066dcbd967-cp314-linux-x86_64` y el rollback
   inmediato `0.13.0-38283df08491-cp314-linux-x86_64`; no retirar el rollback.
2. Preparar la siguiente tranche de interfaces Knowledge/API read-only con
   ownership separado; no conectar `agent_server.py` ni MCP en esta etapa.
3. Mantener `NEO-FUN-002` en `ESPERA_TERCERO`: CA-12 sigue `PARTIAL` y CA-15
   conserva 33.1% de excerpt, sin reabrir R1–R4 ni hacer tuning.
4. Mantener `NEO-AUTH-001` en `EN_CURSO`: falta principal autenticado confiable
   para ampliar MCP; KIO/desktop y cualquier efecto físico conservan su gate
   humano separado.
5. Mantener `NEO-SEM-002` en `ESPERA_VICTOR`: la generación 17 y la política de
   retención no se reanudan, borran ni compactan por inferencia.
6. No abrir corpus personal, SQLite productiva, KIO real, MCP mutante ni
   GitHub Actions durante esta tranche; el contrato preparado de fixtures no
   acredita promoción física.

El cierre de esta implementación incluye código, documentación, SSOT y release
verificada/promovida; las decisiones y gates anteriores siguen siendo
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
vigente del lifecycle 0.13, que corresponde al SHA `c2066dcbd967` y su artefacto
instalado.

## Gates y siguiente paso

1. Mantener `current` y el rollback inmediato, sin iniciar `agent serve` sobre
   una release que deba retirarse durante futuras promociones.
2. Mantener separadas Semantic 17, R1–R4 y cualquier operación física o poda de
   estado; no se incluyen en el cierre de este lifecycle.

Los warnings y wheels de analizadores ausentes de la línea previa conservan su
clasificación independiente; no reabrir Windows, R1–R4 ni las superficies de
mutación para cerrar este objetivo.
