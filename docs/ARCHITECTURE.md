# Arquitectura de NeoCortex

> Describe la arquitectura implementada en el checkout vigente; las entregas
> futuras viven en [ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md).

## Principios

NeoCortex es un framework local Linux-first para descubrir, identificar,
extraer, indexar, relacionar, revisar y buscar contenido. Sus invariantes son:

- los originales prevalecen sobre sus proyecciones;
- la identidad física no se reduce a una ruta;
- sólo una publicación completa puede ser vigente;
- evidencia, inferencia, decisión y autorización son clases distintas;
- una interrupción deja estado reanudable o `recovery_required`, nunca éxito;
- las interfaces comparten contratos y no reinterpretan texto de terminal;
- los recursos y lotes tienen límites observables.

## Topología

```text
Neocortex / python -m neocortex
             │
     interface.entrypoint
             │
     ┌───────┼────────┐
     │       │        │
    CLI     GUI      MCP/API
     └───────┼────────┘
             │
       runtime/orchestrator
             │
   inventory + content routes
             │
  owners SQLite y publicaciones
             │
 Catalog / Semantic / Knowledge / Review
```

La fuente vive en `~/Neocortex/Repository`; el estado, las releases, los modelos
y el launcher se resuelven mediante XDG. El corpus nunca debe contener los
árboles propios del producto.

## Capas

### Foundation y plataforma

`neocortex.foundation` define identidad y procedencia compartidas.
`neocortex.platform` contiene políticas Linux, tipos de contenido, manifests de
capacidades y utilidades de seguridad de contenedores. La enumeración portable
usa el filesystem; los módulos NTFS conservados son legado y no forman parte de
la ruta Linux normal.

### Inventario y deduplicación

`neocortex.deduplication` conserva snapshots, generaciones, fingerprints y
planes no destructivos. Reduce candidatos por tamaño y huella, pero la igualdad
destructiva exige comparación byte a byte. `mark_abandoned_scans()` concilia
scans `building` abandonados y el coordinador lo invoca antes de continuar; no
debe documentarse esa conciliación como ausente.

### Rutas de contenido

El registro de rutas compone PDF, DOCX, Office, Archive, Text, Audio, Video,
Image y Code. Cada ruta declara inputs, límites, progreso, owner y resultado.
Las implementaciones no tienen la misma riqueza: algunos formatos publican
localizadores estructurales y otros sólo texto o archivo completo. Esa brecha se
expone como cobertura, no se rellena con localizadores inventados.

### Progreso y cancelación

`neocortex.progress` define `ProgressEvent` y métricas estructuradas. Terminal,
GUI y grabadores consumen el mismo evento. En Linux los procesos externos usan
sesión/grupo propios; la cancelación alcanza el árbol y registra el estado final.

### Catálogo, Semantic y Knowledge

Catálogo y Semantic son proyecciones reconstruibles con heads publicados.
Knowledge crea un snapshot lógico sobre owners compatibles y fusiona rankings
sin convertir scores heterogéneos en una sola certeza. Puede entregar evidencia
y contexto citado, pero no genera autoridad de mutación.

### Review y curación

Framework conserva batches, tareas, decisiones y eventos. **CURRENT:**
`curate plan` y `--curation-preview` componen propuestas existentes sin crear
estado ni tocar archivos.

**IMPLEMENTED:** `neocortex.curation.lifecycle` enlaza un `plan_digest` completo
con el owner ReviewTask existente. `curate review` publica una página como tareas
advisory, conserva el item y snapshot, pagina con cursor, usa una source fence y
reproduce el mismo batch de forma idempotente. `curate decide` vuelve a comprobar
digest y snapshot y añade por CAS un evento humano `resolved` o `dismissed` con
scope y actor. Ninguna de las dos operaciones crea `file_actions`, invoca KIO,
autoriza efectos o toca corpus/sistemas externos.

`neocortex.curation.authorization` implementa `curate authorize`. Exige
un plan completo, ReviewTasks `resolved`, digest/snapshot/fence vigentes y un
efecto permitido; persiste un `AuthorizationGrant` inmutable y acotado en la
extensión opcional `curation_authorization_grants` del owner Framework. La
extensión se crea sólo por la operación explícita, no añade otro owner y bloquea
UPDATE/DELETE. El grant liga actor, acción, backend Linux, items/tareas,
`max_actions`, `max_bytes`, emisión y expiración.

Emitirlo escribe sólo estado: no crea `file_actions`, no invoca KIO y mantiene
`physical_effect_applied=false`. **IMPLEMENTED sobre fixtures y backends
inyectados:** `neocortex.curation.application` consume el manifest físico del
grant, revalida plan, heads, expiración, raíz, identidad, hash y presupuestos,
registra un `file_action` por efecto, verifica el receipt y conserva
`recovery_required` ante ambigüedad. `PosixRenameBackend` usa no-replace
same-filesystem y `KioTrashBackend` exige evidencia estructurada de destino.
`neocortex.curation.recovery` añade preview read-only y restore con un intento
`restore_curation`, confirmación exacta, `renameat2(RENAME_NOREPLACE)`, hash,
identidad de raíz/Trash y receipt durable; una caída posterior al movimiento se
concilia sin reintento. La CLI no selecciona backend automáticamente; la
promoción KIO real y restore de escritorio siguen siendo gates posteriores. La
conciliación append-only se expone mediante `reconcile_curation_actions` y no
reintenta efectos. El contrato se describe en
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).

### Code como contenido

Code detecta proyectos y lenguajes, extrae símbolos/relaciones, conserva
versiones y permite búsqueda/reconstrucción. No ejecuta el código observado ni
importa herramientas de desarrollo. Pytest, Ruff, Mypy/Pyright y Semgrep se
ejecutan fuera del runtime.

## Persistencia

`STATE_STORE_REGISTRY` es el inventario contractual de owners:

```text
inventory, framework, catalog, pdf, docx, office, audio,
video, image, semantic, code, archive, text
```

Cada owner controla su schema y migraciones. `SQLiteReadSession` selecciona un
modo compatible con la actividad del owner; abrir una base viva con `mode=ro`
ordinario no es una observación garantizada porque SQLite puede tocar sidecars.

Las publicaciones owner-local usan staging y cambio atómico de head. Las vistas
multi-owner se validan contra el protocolo de publicación cross-owner y se
abstienen cuando una transición relevante queda pendiente o inconsistente. No
se promete una transacción física distribuida entre archivos SQLite.

El producto sí expone backup y restore generales mediante `Neocortex databases`.
Persistencia define el contrato; el procedimiento está en
[RECOVERY.md](RECOVERY.md).

## Interfaces públicas

- **CLI instalada:** `Neocortex`; el parser es la fuente exacta de argumentos.
- **API Python:** contratos tipados en `neocortex.api` y `neocortex.sdk`.
- **GUI:** presentación PySide6 que delega trabajo a workers; no redefine reglas.
- **MCP:** servidor stdio local con consultas read-only y las escrituras de
  estado advisory `curation_review`/`curation_decide`; estas últimas declaran
  `readOnlyHint=false`, `destructiveHint=false` y no conceden autoridad. No
  expone `authorize` mientras no exista un principal autenticado.

Las cuatro superficies deben conservar operación, scope, cobertura, epoch,
errores y evidencia equivalentes. La salida estructurada es contrato; el texto
humano no debe convertirse de nuevo en datos mediante parsing.

La tranche 0.12 incorpora `CurationWorkBudget` como límite opcional de la
verificación exacta, con contabilidad de items, archivos y bytes, deadline
monotónico y cancelación cooperativa. Un corte por presupuesto conserva los
resultados ya observados y materializa el resto como `not_verified`. El contrato
`neocortex.curation-checkpoint/v2` publica manifests bounded con root/source/plan
digests, cursor, batch digest, presupuesto acumulado y tamaño de página. La
API/SDK admite sólo el trabajo que cabe en el presupuesto restante y revalida
el snapshot incluso en replay terminal. El fin del recorrido y la cobertura de
las fuentes son independientes: una página final no completa evidencia parcial.
Los manifests `neocortex.curation-checkpoint/v1` permanecen legibles sin
reescribir sus bytes; los sucesores incorporan el contrato vigente. V1 no
demuestra cobertura ni conserva el tamaño de página: una continuación parcial
sin cursor se rechaza por ambigüedad, en vez de reiniciar trabajo por inferencia.

El inventario Linux añade `neocortex.inventory-resume/v1`: el productor
checkpointado recorre cada directorio en orden determinista por bytes, conserva
un cursor DFS, identidad de la raíz y digest de los directorios abiertos, además
de los digests del prefijo y del último lote. El owner se escribe fuera del
corpus con bytes canónicos, permisos `0600`, lock y avance monotónico; la
reanudación elimina sólo el tail posterior al cursor, revalida el prefijo y
rechaza drift de identidad, política, ancestros o lote antes de publicar. Un
replay terminal valida el inventario vigente y devuelve el mismo `scan_id`; no
expone mutación ni se anuncia como herramienta MCP.

El checkpoint terminal incorpora también directorios observados después del
último lote, y cancelación/deadline se consultan aunque no haya entradas. Los
nombres POSIX no representables en SQLite TEXT se aíslan con
`unsupported_path_encoding`: se conservan las observaciones independientes y
el scan queda parcial, sin renombrar el corpus ni sustituir bytes de la ruta.
Se revalidan cambios en directorios activos, sin prometer un snapshot atómico
global del filesystem.

No existe una superficie de exportación o ZIP para el lifecycle de curación;
Archive/ZIP sigue siendo únicamente una ruta de contenido.

## Efectos sobre archivos

Las rutas genéricas `--apply` y `--organization-apply` continúan rechazándose
con `linux_mutation_backend_unavailable`. La ruta nueva `curate apply` sólo
consume un grant confirmado y un backend inyectado en una raíz contenida, por lo
que no habilita mutación implícita del corpus instalado.

La fuente ya contiene `neocortex.safety.kio_trash`: una foundation preparada que
descubre `kioclient6`, `kioclient5` o `kioclient`, valida configuración y snapshot,
ejecuta `move <origen> trash:/` mediante un runner inyectable y clasifica
`blocked`, `recovery_required` o `applied` sólo después de un verificador del
caller. Es reversible pero path-bound y está intencionalmente desconectada de
Linux `--apply`; no fue promovida ni probada contra KIO real en esta cohorte.

La integración actual hace que `apply` lea y revalide el grant, además de
identidades, guard same-filesystem, ledger y expiración, y que `reconcile`
registre la observación sin reintentar. Un timeout o resultado ambiguo permanece
`recovery_required`; KIO real y la sincronización posterior de caches no se
consideran promovidos.

## Concurrencia y recuperación

Los probes de control Linux incorporan cgroups v2 y afinidad a los datos del
host. La memoria distingue límite duro y margen antes de presión por
`memory.high`, y CPU conserva cuota fraccional para el diagnóstico y un número
conservador de workers para los consumidores existentes. No introduce una
política fija de recursos ni un segundo coordinador.

Las superficies de ayuda y los contratos de configuración no importan motores
ni Qt. El diagnóstico de paquetes usa requisitos de `pyproject.toml` en fuente
o `Requires-Dist` del paquete instalado, sin una lista paralela de versiones;
inspección de metadata, localización de ejecutable, archivos de modelo y éxito
de procesamiento no son equivalentes.

El coordinador limita CPU/memoria y registra fases. Writers toman exclusión
cooperativa; backup, restore y purge requieren exclusión más fuerte. Los
subprocesos tardíos no pueden publicar sobre un head nuevo. Un fallo alrededor
de la frontera de efecto produce un estado conciliable, no un reintento ciego.

## Brechas vigentes

- la deduplicación rápida puede ser evidencia insuficiente para disposición;
- la cobertura y precisión de localizadores varían por formato;
- varias fuentes todavía tienen publicación no generacional;
- progreso, límites y replay no son uniformes en todas las rutas;
- MCP expone plan/scan/verify y review/decide, pero no autorización con actor autenticado;
- la ruta física y restore sólo están habilitados mediante backends inyectados y
  fixtures;
- faltan sincronización de caches y presentación GUI del grant/restore;

La prioridad y los criterios de aceptación están en
[ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md); seguridad y owners se detallan en
[SECURITY.md](SECURITY.md) y [PERSISTENCE.md](PERSISTENCE.md).
