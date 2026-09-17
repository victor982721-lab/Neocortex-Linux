# Handoff operativo vigente — NeoCortex

**Ronda actual:** NEO-HYGIENE-C01-C12 — continuidad de higiene operativa integral.
**Actualización:** 2026-09-16T23:06:00-06:00 (America/Mexico_City).
**Fuente viva:** `HEAD == main == origin/main` y árbol limpio se comprueban antes de cerrar.

**Estado vivo:** `HEAD == main == origin/main` (verificado en esta entrega); la integración funcional publicada parte de `7db30481c498f87ee5e12c5418a58e73e0bb645a`, versión fuente `0.14.1`. El artefacto candidato final se reconstruirá desde el SHA final comprobado; no se promueve `current` personal ni se selecciona corpus/HOME/SQLite productiva.

**Goal:** C01–C12 sigue ABIERTO hasta aceptar la candidata final. C01–C08 tienen implementación, regresiones y owner físico sintético: sello común, reconciliación explícita de fallos y poda de tombstones con receipt/replay; C09 tiene frontera base import-light; C10 conserva texto 20, video 1 GiB y degradación `--all`; C11 tiene interfaz/documentación; C12 requiere reconstruir/instalar desde el SHA final, repetir los cuatro headless, recopilar base y verificar origen/artefacto. La promoción `current` personal permanece fuera por CTBI.

**Matriz C01–C12:** C01 claims canónicas completas y bytes físicos sin corte; C02 política del registry/scratch bajo lock; C03 corrupción/ausencia de consumidores fail-closed; C04 intención durable y `recover_retirements()` sin repetir unlink; C05 sello top-level de miembros/identidad/contenido, publicación no-replace y mantenimiento paritario; C06 límites efectivos, batch guard y lecturas lineales 20/40/80/160; C07 `AgentActivity` pública, owner canónico, fallo explícito, proceso externo/reanudación y publicación; C08 `TerminalRetentionPolicy/Plan`, reconciliación `failed-retained`, cuotas y poda de tombstones con receipt; C09 base import-light y skips opcionales; C10 texto 20, video 1 GiB y `--all` degradación documentada; C11 docs/Operations/README/help reales; C12 build/install/pip-check/origen/help/smoke y aceptación final por cerrar.

**Validación de continuidad:** focales runtime/hygiene/CLI/productores/actividad/retención/docs pasan; la ronda nueva añade reconciliación terminal, sello común y retención de tombstones. La candidata instalada final aún debe repetir los cuatro workflows headless y la colección base `7571/7632` sin PIL/PySide6; la corrida base extensa histórica conserva fallos/errors no relacionados de caracterizaciones, semgrep y sdist, separados y no ocultos. Mypy mantiene errores preexistentes de dependencias opcionales.

**Evidencia:** reauditoría adjunta bajo `/home/winterboss/Descargas/` contrastada; scripts del ZIP no estaban disponibles en las ubicaciones acotadas. El paquete compacto de esta ronda se conservará fuera del árbol productivo bajo `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-17-hygiene-c01-c12/`. No se declara C01∧…∧C12.

## Evidencia de entregas anteriores (histórica)

El material siguiente se conserva como procedencia. Sus hashes, corridas,
procesos y estados no son una comprobación viva de la ronda actual.

### Handoff previo conservado

**Última verificación:** 2026-09-10T13:29:51-06:00, `America/Mexico_City`
**Checkout:** `/home/winterboss/Neocortex/Repository`
**Fuente de verdad:** estado vivo de `main`/`origin/main`,
`PENDIENTES.md`, `HISTORIAL.md` y receipts canónicos fechados

**Estado vivo:** la verificación de integración confirma `HEAD == main ==
origin/main` y árbol limpio. El SHA funcional de `NEO-EVO-008` es
`cee69f3cc4c3863fc873b22205a08121281e9ff1`; el SHA final documental instalado
es `41a91382a7890a47044a1b0e80d53723534ae47e`. `current`
apunta a `0.13.0-41a91382a789-cp314-linux-x86_64`
(`source_sha=41a91382a7890a47044a1b0e80d53723534ae47e`), el rollback inmediato es
`0.13.0-c2066dcbd967-cp314-linux-x86_64` y `.staging` está vacío. No se
reescribió historia.

El checkout publicado contiene la tranche API/SDK/CLI read-only y sus correcciones
de límites en `cee69f3cc4c3863fc873b22205a08121281e9ff1`; quedó instalada desde
el SHA final `41a91382a7890a47044a1b0e80d53723534ae47e`. El PID histórico `398501`
se cerró con la autorización de Víctor; los servidores `527799`, `540752` y
`573603` quedaron intactos. No se forzó GC manual ni se interrumpió ninguna tarea
MCP separada.

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
Knowledge v1 con exports API/SDK. El artefacto canónico instalado corresponde al
SHA final `41a91382a789`; el código funcional de la proyección permanece en
`cee69f3`.

La tranche `NEO-EVO-008` añade `knowledge_search_projection_payload`, wrapper y
metadatos aditivos de búsqueda, exports lazy API/SDK y `--knowledge-projection`
opt-in en CLI. `search_payload` y las salidas por defecto permanecen intactas;
`cee69f3` conserva identifiers como procedencia, marca `blocking_owners` en la
cobertura parcial, valida los mappings de `KnowledgeReadBudget` y copia
defensivamente los payloads. Los focales API/SDK/CLI/proyección pasaron **239
pruebas**, con Ruff, Pyright y compileall limpios. La release activa corresponde
al SHA final `41a91382a789`.

La suite integral anterior terminó con **6959 pasadas, 67 omitidas y 42
subtests**. Para la nueva tranche, los focales de curación fueron **42** y los
focales API/SDK/CLI/proyección **239**; Ruff quedó limpio y Pyright terminó sin
errores. La suite completa aislada desde `cee69f3`, con HOME/XDG/estado/corpus
efímeros, modo offline y `umask 022`, terminó **7007 pasadas, 60 omitidas y 42
subtests** sin fallos. Un primer intento con `umask 077` produjo un falso fallo
en `test_checkpoint_parent_must_be_private` porque el umask ocultó los permisos
inseguros; el nodeid pasó con `umask 022` y la corrida final quedó limpia.

La release activa `0.13.0-41a91382a789-cp314-linux-x86_64` verificó manifest,
árbol, launcher, procedencia y smoke aislado. Sus hashes son:

- manifest `5e6c596f409b77c31d48198dfafb71b6addd2bc937a52dfefe0cc18429f26de0`;
- árbol `fdd2029f6183d484cdb392b59d5bea68f7d8a4878bc1b5be2e3f9ae99e18eedd`;
- launcher `d0beb1fadebe92ad824d3e7c589c3271ed92f25ce96a1daeb6a7a5ee527128e6`;
- wheel `49c10d1b8db42b35e413ed2d4d57e00840f0dc8d3ee2f7e00adf4bbcd534eca0`;
- manifest de fuente `ff8c8be7f9fcb1df364960acd92738a8360f788a0f99f14891349c9c3c1b1953`.

La candidata aislada `0.13.0-cee69f3cc4c3-cp314-linux-x86_64` se conserva como
evidencia previa. La instalación canónica desde `41a91382a789` terminó `success`
con `release_linux.py verify` `verified=true`, receipt
`/home/winterboss/.local/state/Neocortex/state/installation-receipts/20260910T184412.312378Z-install-0.13.0-41a91382a789-cp314-linux-x86_64.json`,
retuvo sólo current y rollback inmediato y retiró transaccionalmente los slots
`38283df08491` y `3a7eba599629`. El manifest de rutas/tamaños/mtime del corpus
operativo antes/después fue idéntico: `8cb8e590a53541e2e890db44c675ce7e11b6a8cfd9d420e47f3d0b0dfeb3e03b`.

La verificación canónica del artefacto activo terminó `verified=true` con corpus
efímero, launcher, manifest y rollback comprobados; el receipt final conserva
`models_prepared=false` porque no se adquirieron modelos nuevos.
El launcher instalado ejecutó además `--knowledge-search relay --knowledge-projection
--knowledge-json` fuera del checkout, sin `PYTHONPATH`, con schema
`neocortex.knowledge-evidence-projection/v1`, `scope=personal`, `items=0` y
`coverage.status=partial` esperado para estado vacío; exit code `4` corresponde a
la cobertura parcial. Receipt de smoke:
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-10-evo008-cee69f3-candidate/INSTALLED_PROJECTION_SMOKE.json`.
La evidencia de smoke/replay aislado de 23 fixtures y nueve rutas de la candidata
anterior permanece conservada por separado; el alcance Code `projects`
excluyó el archivo fuera de un proyecto configurado, sin ejecutarlo. Los hashes
de bytes de fixtures y estado temporal permanecieron estables entre corridas.
El primer smoke con cache de modelos aislado, `RC1=2`/`RC2=2` por
`SemanticModelUnavailableError`, permanece registrado como intento incompleto.

## Gates y siguiente paso

1. Conservar `current` en `0.13.0-41a91382a789-cp314-linux-x86_64` y el rollback
   inmediato `0.13.0-c2066dcbd967-cp314-linux-x86_64`; `.staging` está vacío y no
   quedan slots antiguos.
2. Mantener separado el estado canónico ya promovido de los gates independientes
   de corpus, MCP, KIO, Semantic 17 y autoridad física.
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

La implementación, publicación, candidata, promoción y verificación canónica de
esta tranche quedaron completadas; las decisiones y gates anteriores siguen siendo
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
instalado; `cee69f3` permanece como código publicado con candidata aislada
verificada, pendiente de promoción canónica.

## Gates y siguiente paso

1. Mantener `current` y el rollback inmediato, sin iniciar `agent serve` sobre
   una release que deba retirarse durante futuras promociones.
2. Mantener separadas Semantic 17, R1–R4 y cualquier operación física o poda de
   estado; no se incluyen en el cierre de este lifecycle.

Los warnings y wheels de analizadores ausentes de la línea previa conservan su
clasificación independiente; no reabrir Windows, R1–R4 ni las superficies de
mutación para cerrar este objetivo.
