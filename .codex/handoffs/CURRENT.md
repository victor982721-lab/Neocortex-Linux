# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-09T21:50:53-06:00, `America/Mexico_City`
**Checkout:** `/home/winterboss/Neocortex/Repository`
**Fuente de verdad:** estado vivo de `main`/`origin/main`,
`PENDIENTES.md`, `HISTORIAL.md` y receipts canónicos fechados

**Estado vivo:** `current` apunta a
`0.13.0-5b1873294ba7-cp314-linux-x86_64`
(`source_sha=5b1873294ba71959a621b288468d082b57568a5f`), el rollback inmediato es
`0.13.0-c6d3985f7a45-cp314-linux-x86_64` y `.staging` está vacío. El checkout
local contiene la nueva tranche en `686f1ddbec4c5c5e181ab897b10055a29a9642da`,
todavía no instalada ni publicada en `origin/main`; no se reescribió historia.

## Alcance actual

La tranche post-0.13 quedó implementada, publicada e instalada en el artefacto
activo desde `5b1873294ba71959a621b288468d082b57568a5f`; los cambios entre el
SHA ejecutable anterior y éste son documentales. Mantiene las nueve rutas
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

El checkout añade además la tranche `686f1dd`: límites compartidos de
`KnowledgeReadBudget` para curación `scan/verify/apply` y la proyección pública
estable de evidencia Knowledge v1 con exports API/SDK. Esta tranche ya pasó sus
focales, pero aún no forma parte de `current`; sus gates de release y publicación
siguen abiertos.

La suite integral desde el SHA ejecutable terminó con **6959 pasadas, 67
omitidas y 42 subtests**. Ruff y Mypy quedaron limpios; Pyright terminó con 0
errores y 123 warnings clasificados; Semgrep terminó con 0 hallazgos en 7 reglas.

La release activa verificó manifest, árbol, launcher y procedencia. Sus hashes son:

- manifest `e5b0a7ba84dd01437416984b99cf0310b3000a1983298946f97ca210429eb4fc`;
- árbol `556a1eb44795860a8c797477048011dc84137fc7bd007f801cdeb6affd1fdaf1`;
- launcher `9862394e8ddb286626cb6d2286c26131f45a8c7afa06173c0848aca0d6afa6fe`;
- wheel `cb5f50308b0c2a39f5565c28ccd3cbc6a5f2677b83a56c7d24ec49093e15d4cc`;
- manifest de fuente `d69555412b618360200f3ca2b8cf363b3ae54884247a8ec5661c5e318713327c`.

La candidata construida desde el SHA documental más reciente quedó promovida al
namespace canónico después de cerrar únicamente los procesos históricos
`agent serve` `87428` y `114389`; los procesos de la release anterior que siguen
activos se conservaron como rollback inmediato. No se modificó `current`
manualmente para sortear el fence.

La verificación canónica del artefacto activo terminó `verified=true`; la
evidencia de smoke/replay de la misma fuente en namespace aislado sobre 23
fixtures temporales terminó `RC1=0` y `RC2=0` en nueve rutas, con Semantic
completo usando modelos locales existentes, Archive y Text con `cache_hits` en
replay y `action_mode=dry-run`. El alcance Code `projects`
excluyó el archivo fuera de un proyecto configurado, sin ejecutarlo. Los hashes
de bytes de fixtures y estado temporal permanecieron estables entre corridas.
El primer smoke con cache de modelos aislado, `RC1=2`/`RC2=2` por
`SemanticModelUnavailableError`, permanece registrado como intento incompleto.

## Gates y siguiente paso

1. Conservar `current` en `0.13.0-5b1873294ba7-cp314-linux-x86_64` y el rollback
   inmediato `0.13.0-c6d3985f7a45-cp314-linux-x86_64`; no iniciar una release
   histórica ni retirar el rollback durante la siguiente tranche.
2. Validar, construir e instalar la release desde `686f1dd`, y publicar el SHA
   final sólo después de comprobar la suite proporcional, launcher, manifest,
   `current` y rollback.
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
vigente del lifecycle 0.13, que corresponde al SHA `5b1873294ba7` y su artefacto
instalado.

## Gates y siguiente paso

1. Mantener `current` y el rollback inmediato, sin iniciar `agent serve` sobre
   una release que deba retirarse durante futuras promociones.
2. Mantener separadas Semantic 17, R1–R4 y cualquier operación física o poda de
   estado; no se incluyen en el cierre de este lifecycle.

Los warnings y wheels de analizadores ausentes de la línea previa conservan su
clasificación independiente; no reabrir Windows, R1–R4 ni las superficies de
mutación para cerrar este objetivo.
