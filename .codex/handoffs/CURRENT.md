# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-10T11:45:00-06:00, `America/Mexico_City`
**Checkout:** `/home/winterboss/Neocortex/Repository`
**Fuente de verdad:** estado vivo de `main`/`origin/main`,
`PENDIENTES.md`, `HISTORIAL.md` y receipts canónicos fechados

**Estado vivo:** la verificación de integración confirma `HEAD == main ==
origin/main` en `cee69f3cc4c3863fc873b22205a08121281e9ff1` y árbol limpio. `current`
apunta a `0.13.0-c2066dcbd967-cp314-linux-x86_64`
(`source_sha=c2066dcbd967e8804df559f1fc9d15abdbd783bc`), el rollback inmediato es
`0.13.0-38283df08491-cp314-linux-x86_64` y `.staging` está vacío. No se
reescribió historia.

El checkout publicado contiene la tranche API/SDK/CLI read-only y sus correcciones
de límites en `cee69f3cc4c3863fc873b22205a08121281e9ff1`, pero todavía no está
instalada en `current`. En el host real, el `agent serve` PID `398501` sigue usando
el rollback histórico; el PID `527799` usa la release actual y no se toca. No se
fuerza GC ni se interrumpe la tarea MCP separada.

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
Knowledge v1 con exports API/SDK. El artefacto instalado corresponde al SHA
anterior `c2066dc`; la nueva tranche aún no está instalada.

La tranche `NEO-EVO-008` añade `knowledge_search_projection_payload`, wrapper y
metadatos aditivos de búsqueda, exports lazy API/SDK y `--knowledge-projection`
opt-in en CLI. `search_payload` y las salidas por defecto permanecen intactas;
`cee69f3` conserva identifiers como procedencia, marca `blocking_owners` en la
cobertura parcial, valida los mappings de `KnowledgeReadBudget` y copia
defensivamente los payloads. Los focales API/SDK/CLI/proyección pasaron **239
pruebas**, con Ruff, Pyright y compileall limpios. La release activa aún
corresponde a `c2066dc`.

La suite integral anterior terminó con **6959 pasadas, 67 omitidas y 42
subtests**. Para la nueva tranche, los focales de curación fueron **42** y los
focales API/SDK/CLI/proyección **239**; Ruff quedó limpio y Pyright terminó sin
errores. La suite completa aislada desde `cee69f3`, con HOME/XDG/estado/corpus
efímeros, modo offline y `umask 022`, terminó **7007 pasadas, 60 omitidas y 42
subtests** sin fallos. Un primer intento con `umask 077` produjo un falso fallo
en `test_checkpoint_parent_must_be_private` porque el umask ocultó los permisos
inseguros; el nodeid pasó con `umask 022` y la corrida final quedó limpia.

La release activa verificó manifest, árbol, launcher y procedencia. Sus hashes son:

- manifest `2f071256ead3f78b4dd3ce6e1758abe7c8cc1474d7de3e67995a6507362e9418`;
- árbol `73854c3b95d96a43c79c460ecbcabc0e990410dd7acfa16d6e035ac01631ef1a`;
- launcher `59186ce96555fd8d43a795c30337b92e3ce3aa2bfb43d4717ecc750094ea3a1a`;
- wheel `7d2fe292a55e6c49d43363f18011907ff79b6781c116bc44dea791395d1d8c40`;
- manifest de fuente `19f2cce725bb9a7d1a60e05a75b3cb4d3401f1f35309d9bf4abb7bd2c517e1b9`.

La candidata aislada `0.13.0-cee69f3cc4c3-cp314-linux-x86_64` se construyó,
instaló y verificó `verified=true` en un namespace temporal con corpus vacío; la
evidencia canónica está en
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-10-evo008-cee69f3-candidate/`.
La release canónica `current` todavía no se modificó: el gate de promoción real
mantiene el PID `398501` y no se detuvo ningún `agent serve`.

La verificación canónica del artefacto activo anterior terminó `verified=true` con
corpus efímero, modelos locales preparados, launcher, manifest y rollback
comprobados; esa evidencia no acredita una instalación desde `cee69f3`.
La evidencia de smoke/replay aislado de 23 fixtures y nueve rutas de la candidata
anterior permanece conservada por separado; el alcance Code `projects`
excluyó el archivo fuera de un proyecto configurado, sin ejecutarlo. Los hashes
de bytes de fixtures y estado temporal permanecieron estables entre corridas.
El primer smoke con cache de modelos aislado, `RC1=2`/`RC2=2` por
`SemanticModelUnavailableError`, permanece registrado como intento incompleto.

## Gates y siguiente paso

1. Conservar `current` en `0.13.0-c2066dcbd967-cp314-linux-x86_64` y el rollback
   inmediato `0.13.0-38283df08491-cp314-linux-x86_64`; no retirar el rollback.
2. Promover/verificar la release canónica desde `cee69f3` sólo cuando el fence de
   `398501` tenga ownership resuelto; no forzar GC ni detenerlo desde esta tranche.
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

La implementación de código y validación de esta tranche está publicada; el
cierre total aún requiere la promoción/verificación canónica desde `cee69f3` y
la actualización documental externa. Las decisiones y gates anteriores siguen
siendo pendientes independientes.

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
