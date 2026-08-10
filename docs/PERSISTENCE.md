# Persistencia, esquemas y migraciones

> **Estado del documento.** Contrato actualizado el 10 de agosto de 2026. El
> árbol fuente `0.9.0` declara inventario Dedup v10, framework v20,
> catálogo v6 y semántica v6; la barrera integral y el paquete final se registran
> por separado. En una auditoría histórica, bases vivas se inspeccionaron sin
> migrarlas: dedup permanecía
> en v6, framework en v16 y catálogo en v5; semantic no existía. La lectura
> confirmó integridad lógica, pero SQLite creó
> o modificó auxiliares WAL/SHM incluso con `mode=ro` y `query_only=ON`; esa
> incidencia se detalla más adelante.

## Alcance

NeoCortex distribuye su estado entre varias bases SQLite. Este documento define:

- propietario y finalidad de cada base;
- versión y mecanismo de versionado observados;
- política de conexión, transacciones y claves foráneas;
- semántica generacional actual y riesgos no generacionales;
- requisitos para migración, backup, restauración y downgrade;
- retención, crecimiento y mantenimiento seguro;
- riesgos persistentes que un operador no debe confundir con garantías.

Los comandos operativos están en [OPERATIONS.md](OPERATIONS.md). El procedimiento
ejecutable de backup y restauración se mantiene en
[RECOVERY.md](RECOVERY.md); aquí se define su contrato para no duplicar scripts.

## Ubicaciones

`neocortex/platform_policy.py` y `app_paths.py` definen la topología canónica
por usuario. En Windows:

```text
Fuente:       %USERPROFILE%\Neocortex\Repository
Runtime:      %LOCALAPPDATA%\Programs\Neocortex\versions\<runtime-id>\venv
Launcher:     %LOCALAPPDATA%\Programs\Neocortex\bin\Neocortex.exe
Estado:       %LOCALAPPDATA%\Neocortex\state
Autoanálisis: %LOCALAPPDATA%\Neocortex\self-analysis
```

La configuración visual está fuera de las bases:

```text
%LOCALAPPDATA%\Neocortex\ui.ini
```

La caché predeterminada de FastEmbed está en el directorio hermano:

```text
%LOCALAPPDATA%\Neocortex\models\fastembed
```

En Linux:

```text
Fuente:        ~/Neocortex/Repository
Release:       ${XDG_DATA_HOME:-~/.local/share}/Neocortex/releases/<release-id>
Launcher:      ~/.local/share/Neocortex/bin/Neocortex
Estado:        ${XDG_STATE_HOME:-~/.local/state}/Neocortex/state
Configuración: ${XDG_CONFIG_HOME:-~/.config}/Neocortex
Modelos:       ${XDG_DATA_HOME:-~/.local/share}/Neocortex/models
```

No migre bases ni identidades NTFS a Linux. Copie únicamente originales y
construya un estado Linux nuevo; `birthtime_ns=-1` es el sentinel portable
cuando no existe nacimiento real.

`--state-directory` puede seleccionar otra ubicación para una invocación. No
mezcle bases de dos directorios de estado ni restaure una sola base sin revisar
la compatibilidad del conjunto.

`InternalPathsPolicy` captura ruta e identidad física de repositorio, runtime,
datos de aplicación, autoanálisis y launcher. En una corrida normal, Dedup v10
guarda en cada scan la firma cruda de `InventoryExclusionPolicy`; Framework
guarda la firma efectiva que combina esa evidencia con la firma versionada de
rutas internas. Un estado igual o ancestro del corpus se rechaza porque su
exclusión podaría la raíz completa; el autoanálisis exige además disjunción en
ambas direcciones.

## Reglas de propiedad

1. Cada base tiene un único módulo propietario de DDL y migraciones.
2. Todo writer debe entrar por la factory del propietario.
3. Los lectores administrativos deben usar URI SQLite `mode=ro` y, cuando la
   factory lo establezca, `query_only=ON`.
4. `PRAGMA foreign_keys` se configura **por conexión**; que una factory lo
   active no protege conexiones abiertas directamente.
5. No hay foreign keys entre archivos SQLite. La coherencia cruzada se apoya en
   `run_id`, `scan_id`, identidades, firmas y orden de publicación.
6. Un run de framework completado no demuestra que una base secundaria haya
   publicado una generación integral.
7. Una base futura, malformada o con objetos desconocidos debe provocar
   abstención; nunca borrado o reconstrucción improvisada.

## Matriz de bases y versiones observadas

Las versiones siguientes provienen de constantes del árbol, no de una consulta
a los archivos vivos.

| Base o API | Propietario | Versión declarada | Finalidad y tablas principales | Versionado |
|---|---|---:|---|---|
| Base de `SqlitePathIndex` | `_01_Enumeracion.path_index_schema` / `path_index` | 1 | índice auxiliar MFT de `nodes` y `metadata` | `metadata.schema_version`; no migraciones legacy admitidas |
| `dedup.sqlite3` | `_02_Deduplicacion.inventory_schema` / `DedupIndex` | **10 en fuente**; 6 en la base viva histórica inspeccionada | scans generacionales ligados a firma de inventario, checkpoint portable/USN opcional, archivos, fingerprints, summaries y grupos/miembros de plan | `metadata.schema_version`; migraciones 1→10 |
| `framework.sqlite3` | `framework_schema`, `FrameworkState`, `FrameworkRouteState` | **20** | runs, fases, policy/identidad de corpus, acciones con snapshot protegido, eventos append-only de transición/conciliación/manifest, candidatos de ruta, caché de tipo, revisión y evidencia | `metadata.schema_version`; migraciones secuenciales |
| `pdf.sqlite3` | `pdf_schema`, `pdf_state`, `PdfRoute`, `PdfDerivedIndexer` | 12 | inventario, documentos, páginas, OCR efectivo/OSD/confianza/fallback, staging, errores, warnings, FTS, firmas, similitud y layout | `metadata.schema_version`; migraciones secuenciales |
| `docx.sqlite3` | `docx_schema`, `docx_state`, `DocxRoute` | 5 | inventario, documentos, partes, diagnósticos, FTS, layouts y contrapartes PDF | `metadata.schema_version`; migraciones secuenciales |
| `office.sqlite3` | `office_state`, `OfficeRoute` | 2 | inventario, documentos, celdas XLSX tipadas y FTS | `metadata.schema_version` |
| `archive.sqlite3` | `archive_state`, `ArchiveRoute` | 1 | contenedores ZIP, miembros y cadenas anidadas, incidencias, texto comprimido y FTS | `metadata.schema_version`; sin estado legacy |
| `text.sqlite3` | `text_state`, `TextRoute` | 1 | texto físico, EML y Office heredado; título/autor, metadata, texto comprimido, errores y FTS | `metadata.schema_version`; sin estado legacy |
| `audio.sqlite3` | `audio_state`, `AudioRoute` | 1 | inventario, documentos, segmentos y FTS de transcripción | `metadata.schema_version` |
| `video.sqlite3` | `video_state`, `VideoRoute` | 1 | documentos, streams, frames, selección, OCR, timestamps, métricas y FTS | `metadata.schema_version`; sin estado legacy |
| `image.sqlite3` | `image_state`, `ImageRoute` | 5 | imágenes, estado de extracción/clasificación y metadata | `metadata.schema_version`; migraciones aditivas |
| `document_catalog.sqlite3` | `document_catalog_schema`, `document_catalog` | **6** | runs, generaciones/staging, publicación por fuente, proyección de documentos, historial y planes de organización | `metadata.schema_version`; migraciones secuenciales |
| `code.sqlite3` | `code_schema`, `code_state` | 4 | proyectos, runs, archivos/versiones, símbolos, referencias, dependencias, grafo, chunks, FTS, métricas/relaciones y evidencia externa normalizada | metadata + `PRAGMA user_version` + `schema_migrations` exacto; migraciones secuenciales 1→4 |
| `semantic.sqlite3` | `semantic_schema`, repositorios y servicio semántico | **6** | espacios/modelos, revisiones inmutables, miembros de generación, heads publicados, jobs, payloads, prototipos y evidencia | metadata + `PRAGMA user_version` + `schema_migrations` exacto |

La base del índice MFT es una API auxiliar con ruta elegida por el llamador y
no forma parte de las propiedades predeterminadas de `FrameworkConfig`.
`code.sqlite3` y `semantic.sqlite3` pueden no existir hasta usar esas
capacidades.

### Matriz de propietarios del snapshot Knowledge

Knowledge conserva los diez propietarios históricos y agrega `archive` y
`text` sólo cuando existe la base correspondiente. No incorpora la base
auxiliar de `SqlitePathIndex`. Antes de leer datos valida la versión y el
contrato del propietario correspondiente; una instalación que todavía no
ejecutó esas rutas mantiene el vector histórico de diez owners:

| Owner Knowledge | Archivo | Esquema esperado | Head o watermark lógico |
|---|---|---:|---|
| `inventory` | `dedup.sqlite3` | 10 | scan publicado por raíz y firma, checkpoint portable/USN opcional y señal de plan de duplicados completado |
| `framework` | `framework.sqlite3` | 20 | máximos de run, evento y acción; `best_effort_non_generational` |
| `catalog` | `document_catalog.sqlite3` | 6 | generación publicada por `source_kind` |
| `pdf` | `pdf.sqlite3` | 12 | filas actuales, último update/run; `best_effort_non_generational` |
| `docx` | `docx.sqlite3` | 5 | filas actuales, último update/run; `best_effort_non_generational` |
| `office` | `office.sqlite3` | 2 | filas actuales, último update/run; `best_effort_non_generational` |
| `archive` (aditivo si existe) | `archive.sqlite3` | 1 | miembros actuales, último update/run; `best_effort_non_generational` |
| `text` (aditivo si existe) | `text.sqlite3` | 1 | documentos actuales, último update/run; `best_effort_non_generational` |
| `audio` | `audio.sqlite3` | 1 | filas actuales, último update/run; `best_effort_non_generational` |
| `image` | `image.sqlite3` | 5 | imágenes actuales, último update/run; `best_effort_non_generational` |
| `semantic` | `semantic.sqlite3` | 6 | generación `ready` publicada por modelo, espacio y firma de procesamiento |
| `code` | `code.sqlite3` | 4 | archivos actuales, última versión/run; `best_effort_non_generational` |

Una base ausente se representa como `absent`; no se crea para completar la
matriz. Una versión menor, futura, inconsistente o un contrato malformado
produce abstención explícita. Knowledge no inicializa, migra, repara ni
reconstruye bases.

### Interpretación de versiones

- En path index, dedup, framework, PDF, DOCX, Office, Archive, texto, audio,
  imagen y catálogo,
  la autoridad es `metadata.schema_version`.
- En code y semantic, metadata debe coincidir con `PRAGMA user_version` y con
  todas las filas esperadas de `schema_migrations`.
- Un `PRAGMA user_version=0` en una base cuyo contrato usa metadata no significa
  automáticamente que carezca de versión.
- Nunca iguale manualmente ambos mecanismos para forzar apertura.

### Snapshot lógico cross-owner de Knowledge

`--knowledge-status`, `--knowledge-search` y `--knowledge-context` abren sólo
archivos existentes mediante URI SQLite `mode=ro`, habilitan y comprueban
`foreign_keys`, fijan `query_only=ON`, usan timeout de 60 segundos y cierran la
conexión incluso ante `BaseException`. Si el directorio de estado no existe, la
consulta no lo crea. Este contrato evita DDL/DML y creación de bases; no promete
neutralidad byte por byte de `-shm` cuando SQLite participa en un WAL existente,
limitación documentada en la sección de inspección viva.

No existe una transacción SQLite distribuida entre los diez archivos históricos
ni los owners aditivos Archive/texto. El snapshot Knowledge es una observación
lógica y acotada:

1. abre un propietario y valida su esquema;
2. bajo una transacción de lectura registra heads/watermarks y
   `PRAGMA data_version`;
3. confirma esa lectura y repite la misma observación en otra transacción de la
   misma conexión;
4. compara versión, heads, watermarks y `data_version`, y cierra la conexión;
5. repite el conjunto completo de propietarios **una sola vez** si cualquiera
   cambió;
6. si vuelve a cambiar, devuelve `snapshot_changed` con dos intentos en vez de
   fingir estabilidad.

Search/context agregan otra barrera alrededor de la recuperación: capturan un
snapshot antes y otro después de buscar. Si sus identidades lógicas difieren,
repiten la consulta completa una sola vez; un segundo cambio conserva los hits
de la última ejecución, los marca incompletos y devuelve la condición
`snapshot_changed`. No hay reintentos ilimitados ni bloqueo de writers.

Los heads están limitados a 1024 por propietario. Inventario, catálogo y
semántica exponen publicaciones que fijan una generación concreta. En cambio,
framework, PDF, DOCX, Office, Archive, texto, audio, imagen y code exponen agregados con
`visibility=best_effort_non_generational`: permiten detectar deriva entre las
dos observaciones, pero no fijan cada fila consumida ni convierten la lectura
cross-owner en atómica. Un resultado que requiere una fuente ausente,
incompatible o que cambió debe declararse parcial o abstenerse.

#### Watermark del plan de duplicados

Cada head de inventario corresponde exclusivamente a un checkpoint válido cuyo
scan está `complete`. Cuando ese scan tiene `duplicate_plan_summaries` terminado,
el head incorpora esta señal:

```text
duplicate-plan-v1:<completed_ns>:<group_count>:<redundant_files>:<reclaimable_bytes>
```

La recuperación Knowledge de relaciones de duplicados vuelve a exigir el mismo
`scan_id` y los cuatro valores del summary antes de aceptar filas de
`planned_duplicate_groups`/`planned_duplicate_members`. Una señal ausente no
inventa un plan; una señal malformada, relaciones contradictorias o una cota
excedida causan abstención/incompletitud, no descarte silencioso de evidencia.
Este watermark vincula la relación al plan observado, pero no es un commit
cross-database ni garantiza que propietarios no generacionales permanezcan
inmutables.

## Tablas y relaciones por dominio

### Inventario común

`dedup.sqlite3` conserva la observación común que consumen el plan de duplicados
y las rutas. En la fuente v9, `files` usa la clave `(scan_id, path)`, cada fila
pertenece a una generación, el scan conserva su firma cruda de inventario y el
checkpoint referencia el scan publicado de una raíz. La base viva continuaba
en v6, donde `files.path` era global; no fue migrada durante esa auditoría.

### Estado de framework

`framework.sqlite3` es la bitácora de coordinación, no un almacén de contenido.
Contiene:

- `initial_runs`, `run_events`, `route_runs` y `route_phase_runs`;
- `run_actions` y `file_actions`;
- `file_action_events`, append-only mediante triggers;
- `file_action_reconciliation_events`, append-only mediante triggers;
- `route_candidates` y `content_type_cache`;
- `review_candidates`, `review_decisions`, ejemplos y progreso de evidencia.

`file_action_events` referencia `file_actions`; el resto de relaciones de
framework continúa siendo principalmente lógico. V18 añade clave idempotente,
identidad esperada, recibo de efecto y timestamp de frontera sin asignar esos
datos retrospectivamente a filas legacy.

V19 agrega un log inicialmente vacío para observaciones de conciliación. Cada
evento conserva action/idempotency/run, clasificación, identidad y recibo
observados, actor, procedencia, firma, timestamps, recomendación y motivo de
abstención. Una clave XXH3 hace idempotente la misma solicitud y la secuencia
con FK compuesta aplica CAS sobre el predecesor; triggers impiden update/delete.
`--action-recovery-status` sigue en `mode=ro` y `query_only`; sólo
`--action-recovery-record`, con confirmación explícita, abre el writer existente
y puede aplicar la migración aditiva soportada. El evento declara
`authorizes_filesystem_mutation=false`: todavía no hay estados durables de
decisión humana, autorización, ejecución de recuperación o verificación.

V20 agrega a `initial_runs` modo de acceso, identidad física de raíz, estado y
firma de policy, y copia a `file_actions` el modo y la raíz/identidad protegidas.
Los runs legacy se conservan como `normal` con evidencia nueva nula; no se
reinterpretan como autoanálisis. Checks y triggers ligan cada snapshot de
acción a su owner, vuelven inmutable la frontera y rechazan acciones para
`analyze_only`.

#### Manifest durable de autoanálisis

Un autoanálisis persiste un run `self_analysis/analyze_only`, publica el scan
con cero candidatos y ejecuta una única ruta `code` desde
`inventory_snapshot`. `complete_self_analysis_run()` abre `BEGIN IMMEDIATE`,
vuelve a verificar policy, identidad, scan, journal disponible o explícitamente
no disponible, una ruta completada y
ceros exactos en candidatos, acciones y organización. En la misma transacción
cambia el owner a `completed` e inserta un único evento con el manifest
`neocortex.self-analysis-manifest/v2`, limitado a 256 KiB. El decoder conserva
compatibilidad de lectura estricta con v1.

El manifest contiene policy y firma
`inventory-exclusion-policy-v2:xxh3_128:...`, evidencia code, conteos cero y
argv `analyze`/`status` como arrays acotados. No es una tabla paralela ni una
fuente independiente. `--code-status --code-json` lo vincula al último run de
code y calcula frescura contra framework, checkpoint Dedup, identidad y USN.
Sin acceso USN, las cuatro columnas del cursor permanecen nulas, no existe
checkpoint para ese scan y el manifest registra `journal.status=unavailable`;
por contrato nunca resulta `current=true`.
Para evitar la incidencia histórica de sidecars, cada owner exige
`mode=ro&immutable=1`, `query_only`, fences pre/post y ausencia de `-wal`,
`-shm` y `-journal`. Cualquier auxiliar —incluso vacío o desacoplado— o cerca
inestable en code, framework o Dedup causa abstención total con código `2`, sin
vista parcial ni modificación del estado. Ningún caso crea, migra o hace
checkpoint. Véase
[SELF_ANALYSIS.md](SELF_ANALYSIS.md).

#### Publicación del snapshot de enrutamiento

Un run inicial no expone candidatos reutilizables al persistir el inventario.
Primero termina `FrameworkActions.execute()` y materializa todos los
`route_candidates`; después `publish_initial_routing_snapshot()` compara el
conteo durable y, en una sola transacción, enlaza `initial_runs.scan_id`, guarda
los contadores de inventario e inserta el evento
`neocortex.routing-snapshot/v1`. El valor `inventory_attempts=0` es válido para
una reconciliación incremental. `begin_route_runs()` exige que el vínculo ya
esté publicado y la finalización aplica CAS sobre el mismo scan y metadatos.

La compatibilidad con runs legacy sin `scan_id` es cerrada: sólo se recupera un
run inicial `interrupted` con evidencia de inventario única, bien formada y
acotada, candidatos cuyo conteo coincide y al menos un `route_run` durable. El
scan de `dedup.sqlite3` debe estar completo, sin errores, pertenecer a la misma
ruta e identidad física de raíz y conservar `files_seen == COUNT(files)`. La
recuperación y su evento `neocortex.inventory-recovery/v1` se confirman en la
misma transacción; una ambigüedad o conflicto de metadatos provoca abstención.

### Cachés por formato

PDF, DOCX, Office, Archive, texto, audio, video e imagen conservan resultados
especializados para no reprocesar archivos sin cambios. Sus claves se derivan
de identidad durable, metadatos y firmas de procesamiento. Una caché no es
respaldo del original.

PDF v12 añade por página perfil/idiomas OCR efectivos, resultado OSD,
confianza, fallback y procedencia; su migración 11→12 es aditiva y la
reextracción actualiza la firma. Office v2 conserva cada celda XLSX no vacía con
libro, hoja/ordinal, A1, tipo, valor lógico y raw, fórmula, valor cacheado,
estilo/formato y proyección `XLSX_CELL` a texto/FTS; la migración v1→v2 no
inventa celdas legacy, que aparecen al reprocesar.

PDF y DOCX incluyen FTS y estructuras derivadas; Archive conserva miembros
virtuales/cadenas ZIP y OCR; texto conserva cuerpo, asunto/autor y tipo; audio
almacena segmentos. Video v1 conserva documentos, inventario, streams/probe,
frames con razones de muestreo, timestamp, dimensiones, XXH3, OCR/provenance y
FTS, además del vínculo exacto al transcript Audio cuando existe. Imagen
almacena clasificación/evidencia y su ruta productora garantiza la huella
completa XXH3-128 en Dedup. La poda de una caché sólo debe
ocurrir después de una reconciliación que demuestre qué filas dejaron de ser
vigentes.

### Catálogo documental

El catálogo unifica resultados de PDF, DOCX, Office, texto y audio para
clasificación y organización. Archive permanece fuera porque sus miembros no
son archivos físicos organizables. `catalog_runs` registra ejecuciones; `documents` contiene la
vista actual; `classification_history` conserva resultados por firma y ruta;
`organization_plans` registra intención y sincronización posterior.

V6 construye una generación aislada por `source_kind` en
`catalog_generation_documents`. `catalog_publications` selecciona la vigente y
la publicación CAS reemplaza la proyección compatible `documents` en la misma
transacción. Los batches de un build fallido o cancelado permanecen invisibles
para los lectores oficiales.

Los streams de candidatos de PDF e imagen poseen la conexión SQLite que abren:
creación, iteración y cierre ocurren en el mismo thread. El `finally` del
productor cierra el generator también ante excepción o cancelación; otro thread
no debe desenrollarlo ni cerrar indirectamente esa conexión.

### Código

La base de código separa proyecto, archivo lógico y versión. Sí declara foreign
keys extensas y conserva historial de migración. La construcción del grafo se
publica actualmente dentro de una transacción global de finalización; hacer
batches sin una generación y un puntero publicados no sería una corrección
segura (`NC-AUD-015`).

El esquema vigente es 4. Un cache hit con ruta exacta actualiza los run IDs de
presencia/observación sin DML sobre `code_fts`. Una ruta distinta no reutiliza la
versión: la publicación normal invalida la vigente y crea una sucesora con su
propio `path_observed`, FTS y evidencia, conservando la versión anterior.

En reconciliaciones sin límite ni selección, `mark_missing` invalida por lotes
keyset las identidades no vistas antes de resolver el grafo. `finalize_graph`
reinicia únicamente membresías derivadas y diagnósticos/edges reconstruibles de
versiones vigentes. Después de resolver membresías y conflictos, un mapa TEMP
con `version_id` indexado sincroniza en un solo scan únicamente los labels FTS
distintos; las membresías de manifest, versiones y labels históricos permanecen
como evidencia.

El resolver v4 carga símbolos y dependencias vigentes en conjuntos TEMP
indexados. Resuelve primero llamadas con ámbito demostrable dentro del mismo
módulo o clase y dependencias Python relativas mediante candidatos léxicos
`module.py`/`module\__init__.py`; si ambos candidatos existen se abstiene. El
fallback global por nombre cualificado o simple sólo publica una resolución
única. Conflictos y ausencias permanecen ambiguos o no resueltos, sin fabricar
edges.

`metadata['code_graph_completion_v3']` es un fence derivado tipado y versionado,
no un head generacional. Registra schema, `analysis_run_id` y
`code-graph-resolver-v4`, y avanza en la misma transacción que cambia su
`analysis_run` de `running` a `completed`. El fastpath sólo acepta el run
inmediatamente anterior, completo, con la misma firma, summary válido y
todos los candidatos como cache hits compatibles con los analizadores del
runtime. Cualquier discrepancia —o una base existente todavía sin fence— fuerza
una finalización completa; los estados cacheados `partial` y `error` siguen
contabilizándose en el nuevo summary.

Schema 3 conserva `external_tool_runs` y la proyección Ruff legacy de v2, y
añade cinco tablas normalizadas:

- `external_run_contracts`: identidad de proveedor, perfil/confianza, raíz,
  firmas de configuración, entorno, inputs y comparabilidad, estrategia,
  límites lógicos y declaraciones de autoridad;
- `external_run_inputs`: versión Code, identidad portable, ruta, digest, tamaño
  y cobertura de cada input elegible;
- `external_findings`: identidad portable, owner Code, categoría/regla,
  severidad, rango, metadata, confianza y enlace opcional a `diagnostics`;
- `external_run_replays`: ejecución completa reutilizada y firma/conteos de la
  verificación exacta;
- `external_run_counters`: contadores no negativos de cobertura, bytes,
  procesos, salida, tiempo, findings, comparabilidad, caché y errores.

Cada proveedor inserta una fila terminal en `external_tool_runs` y su contrato,
inputs, findings/counters y proyección `diagnostics.source='external:*'` dentro
de la transacción que finaliza Code. Un fallo, timeout o proveedor indisponible
publica su estado terminal y no deja una proyección vieja aparentando frescura;
no borra las publicaciones válidas de los otros proveedores.

Un input exacto sólo reutiliza una línea base con el mismo proveedor, perfil,
versión, configuración, entorno, raíz y firma de inputs. El replay registra
`execution=cache_replay`, vuelve a verificar todos los archivos y bytes, enlaza
la ejecución completa y no duplica findings ni invoca el proceso externo. La
lectura recomputa el digest y valida la proyección; cualquier discordancia causa
abstención fail-closed. Mypy y Pyright permanecen separados; el consenso de
tipos es una proyección reconstruible, no otra fuente de verdad.

No se añadieron generación, staging ni CAS de head. La transacción global no
admite cancelación dentro de una sentencia SQLite; los empates de resolución se
conservan ambiguos y la firma del registro de analizadores sigue siendo global,
por lo que un cambio de contrato puede invalidar otros lenguajes.

### Semántica

La base semántica separa espacios vectoriales incompatibles, modelos,
revisiones de texto, chunks, embeddings, payloads, jobs y evidencia. V6 congela
revisiones y miembros por generación y selecciona una generación completa por
modelo mediante `published_embedding_heads`. La búsqueda oficial sólo consulta
esos heads; `ready_partial` y `building` no son visibles.

`code.embedding_links`, introducida en Code v2 y conservada por v4, tiene un
productor y un consumidor integrados. Tras publicar por completo texto de
`source_kind=code`, `code-semantic-link-v1` prepara en una tabla TEMP la
cobertura exacta del head y exige que cada chunk vigente no vacío resuelva por
identidad física, `version_id`, índice y clase. Sólo entonces desactiva enlaces
anteriores del modelo y hace UPSERT de item, modelo, espacio, generación y
procedencia. El replay exacto no ejecuta DML sobre enlaces; una nueva generación
conserva las filas anteriores inactivas. No hay FK cross-database ni atomicidad
distribuida: el lector vuelve a validar ambos heads y la versión Code vigente,
por lo que una interrupción entre publicaciones se convierte en abstención, no
en evidencia cruzada fabricada. Después del commit, el productor hace checkpoint
del WAL de Code y sólo retira sidecars reconstruibles cuando el WAL está vacío,
para que los diagnósticos quiescentes puedan leer el estado publicado.
Si un lector externo retiene sus handles, la limpieza es best-effort: no se
revierte el run completado ni se fuerza el cierre ajeno, y el status estricto
continúa absteniéndose hasta una corrida posterior sin ese lector.
Los lectores operativos de Code evitan crear esos auxiliares: si la base está
quiescente usan URI immutable y verifican identidad, tamaño, mtime y ausencia de
sidecars antes y después; si ya hay sidecars usan read-only convencional y los
dejan intactos. El status estricto conserva su contrato más fuerte.
El lector del owner durable que usa el watcher es aún más estricto: sólo abre
Framework como snapshot immutable cuando el archivo está cercado y sin
sidecars. Así puede recargar el checkpoint entre corridas sin volver a crear un
WAL vacío o SHM; ante actividad concurrente se abstiene y el watcher aplica su
backoff, sin ignorar frames ni limpiar archivos ajenos.

## Política de conexión y PRAGMAs

Los valores son los observados en factories/initializers del árbol. SQLite
aplica algunos PRAGMAs por archivo y otros por conexión; no se deben extrapolar
a una conexión auxiliar.

| Propietario | Timeout/busy | Escritura | Caché y WAL | FK / lector |
|---|---|---|---|---|
| Path index | 60 s | WAL, `synchronous=NORMAL` | `cache_size=-32768`, autocheckpoint 4096 páginas, journal limit 256 MiB | FK/query-only verificados según reader/writer; el esquema no declara relaciones FK |
| Dedup v10 | 60 s | WAL, `synchronous=NORMAL` | `cache_size=-32768`, autocheckpoint 4096 páginas, journal limit 256 MiB | FK de files/checkpoint a scans; cursor USN opcional todo-o-nada; `foreign_keys=ON` y verificado por la factory |
| Framework writer | 60 s | WAL, NORMAL | -32768, 4096, 256 MiB | FK local de eventos de acción; otras relaciones lógicas |
| Framework route / heartbeat | 60 s / 10 s | base existente `mode=rw`; hereda journal del propietario | busy timeout explícito | FK verificado; reader diagnóstico usa `mode=ro` + `query_only` |
| PDF | 60 s / 60 000 ms | WAL, NORMAL | -32768, 4096, 256 MiB | `foreign_keys=ON`; lector URI ro + query_only |
| DOCX | 60 s / 60 000 ms | WAL, NORMAL | -32768, 4096, 256 MiB | FK ON; lector URI ro + query_only |
| Office | 60 s / 60 000 ms | WAL, NORMAL | -32768, 2048, 128 MiB | FK ON; lector URI ro + query_only |
| Archive | 60 s / 60 000 ms | WAL, NORMAL | -32768, 2048, 128 MiB | FK ON; lector URI ro + query_only |
| Texto | 60 s / 60 000 ms | WAL, NORMAL | -32768, 2048, 128 MiB | FK ON; lector URI ro + query_only |
| Audio | 60 s / 60 000 ms | WAL, NORMAL | -65536, 2048, 256 MiB | FK ON; lector URI ro + query_only |
| Imagen | 30 s / 30 000 ms | WAL, NORMAL | sin cache/autocheckpoint/journal limit explícitos | FK ON; lector URI ro + query_only |
| Catálogo | 60 s / 60 000 ms | WAL, NORMAL | -32768, 4096, 256 MiB | FK ON para runs/generaciones/publicación; lector URI ro + query_only |
| Código | 60 s / 60 000 ms | WAL, NORMAL | -32768, 2048, 256 MiB | FK ON; lector URI ro + query_only |
| Semántica | 60 s / 60 000 ms | WAL, NORMAL | -32768, 2048, 256 MiB | FK ON; lector URI ro + query_only |

`journal_size_limit` limita el WAL retenido después de un checkpoint; no reduce
el tamaño máximo histórico del archivo principal. `wal_autocheckpoint` tampoco
garantiza un WAL pequeño si un lector mantiene un snapshot antiguo.

### Factories endurecidas y cobertura residual

El inventario actual clasifica 37 llamadas directas en 21 módulos, dos
dispatchers abstractos y 132 adquisiciones mediante 20 factories de propietario;
no dejó propietarios oficiales sin clasificar. Cuatro conexiones `:memory:` son
probes privados de esquema/FTS y no tienen política de filesystem.

No existe una factory universal. Las familias probadas son: writer propietario,
reader operacional, reader diagnóstico estricto, migrador, backup, proceso hijo
y base temporal. Los writers activan/verifican FK, timeout y su política propia
de WAL/caché; los readers de diagnóstico usan URI escapada `mode=ro`,
`query_only=ON`, FK y cierre ante `BaseException`; writers sobre estado existente
usan `mode=rw` para no recrear una base desaparecida. `FrameworkState` conserva
su modo compatible de crear/inicializar y añade `existing_only=True` para
rehusar creación, aunque una base existente sí puede migrarse.

El contrato no protege SQL externo que evada las factories ni crea relaciones
que el DDL no declara. Tampoco se afirma que `mode=ro` sea byte-neutro ante un
WAL/SHM existente. Las pruebas usaron fixtures temporales: no se abrieron bases
operativas vivas ni se demostró que instalaciones existentes carezcan de
huérfanos.

## Transacciones, bloqueo y WAL

### Bloqueo de aplicación

La corrida integrada y operaciones directas de escritura relevantes adquieren
`framework.lock`. El lock evita dos writers coordinados de NeoCortex sobre el
mismo estado, pero no impide que otra aplicación abra SQLite ni convierte varias
bases en una transacción distribuida.

No borre `framework.lock`, `-wal` o `-shm`. Cierre de forma cooperativa el
proceso propietario y deje que el sistema operativo y SQLite liberen sus
handles.

El watcher añade un lease de vida independiente,
`watcher-life-xxh3-128-<digest>.lock`, derivado de raíz+estado. El byte lock del
SO es autoritativo y el JSON interno sólo diagnóstico; metadata stale se
reemplaza después de adquirir, nunca antes. El archivo puede persistir tras el
cierre y no debe eliminarse como limpieza.

### Transacciones de esquema

Los initializers actuales siguen, con variantes, este patrón:

1. si la base existe, abrir de sólo lectura y leer versión;
2. rechazar versión futura o representación no canónica;
3. validar sin escribir cuando ya está vigente;
4. abrir writer, `BEGIN IMMEDIATE` y volver a leer la versión bajo lock;
5. aplicar pasos monotónicos;
6. validar contrato exacto;
7. commit o rollback ante `BaseException`.

Code y semantic validan además historial y `user_version`. Framework reconstruye
dos tablas de revisión en v17 con conteos de origen/destino y nombre temporal
reservado comprobado; v18 valida el layout v17 exacto de `file_actions`, agrega
cuatro columnas sin reinterpretar filas legacy y crea la bitácora de transición.
V19 valida el layout v18 y agrega la bitácora de conciliación append-only.
V20 preserva esos owners y agrega evidencia inmutable de policy/identidad para
autoanálisis y acciones.

### Migración framework v17→v18

La migración exige las doce columnas v17 conocidas de `file_actions`, rechaza
columnas extra o ausentes, conserva el conteo y agrega `idempotency_key`,
`expected_identity_json`, `effect_receipt_json` y `applying_ns`. Después crea
índices, `file_action_events`, su FK y triggers append-only y valida el contrato
actual antes del commit. Filas legacy conservan `NULL` en la evidencia nueva;
no se inventa una identidad o recibo. Un objeto desconocido o cualquier
`BaseException` revierte DDL, datos y versión dentro de la transacción.

### Migración framework v18→v19

La migración es aditiva: exige el contrato v18 exacto, crea
`file_action_reconciliation_events`, su secuencia/FK compuesta, índices, checks
y triggers no-update/no-delete, y actualiza metadata sólo dentro de la misma
transacción. No altera `file_actions` ni inventa observaciones para filas
legacy. Se probó sobre bases pobladas, con objetos desconocidos, dos aperturas,
lector WAL concurrente y rollback por `RuntimeError`/`KeyboardInterrupt`. Una
versión limitada a v18 debe abstenerse ante v19; el rollback operativo restaura
backup y paquete, no reduce el número.

### Migración framework v19→v20

La migración es aditiva y conserva los conteos de `initial_runs` y
`file_actions`. Añade modo, identidad, estado y firma a los runs, y un snapshot
de modo/raíz/identidad a las acciones. Los defaults `normal` mantienen
compatibilidad con filas v19; los campos de identidad permanecen `NULL` en vez
de inventar procedencia. Después se validan checks, triggers de coherencia e
inmutabilidad y el contrato completo antes del commit.

Un reader limitado a v19 debe abstenerse ante v20. No hay downgrade por DDL: el
rollback exige restaurar base consistente y paquete compatible. Abra o migre
bases operativas sólo con el runtime versionado validado para esta fuente.

### Migraciones PDF v11→v12 y Office v1→v2

PDF 11→12 añade columnas de procedencia OCR por página y las deja nulas para
filas legacy; no atribuye idioma, OSD, confianza o fallback retroactivamente.
Office 1→2 valida primero el esquema exacto, crea `xlsx_cells` con FK/cascade y
el trigger que sincroniza `workbook` al cambiar `documents.path`; conserva
documentos, texto y FTS legacy. En ambos casos la evidencia nueva requiere un
reproceso productor con la firma vigente. Una publicación parcial nunca se
presenta como celdas o OCR completos.

### Migración Dedup v7→v8

La migración añade `scans.inventory_policy_signature` sin inventar evidencia
para generaciones legacy. Conserva el número de scans y checkpoints, así como
el conteo y suma de bytes de `files`, y después invalida todos los checkpoints
v7 porque carecen de una firma demostrable. Los scans nuevos guardan la firma
cruda de exclusión; la firma efectiva normal continúa perteneciendo al binding
de Framework. No hay downgrade: el rollback restaura base y paquete compatibles.

### Migración Dedup v8→v9

La migración desacopla la publicación generacional del acelerador USN. Conserva
sin cambios scans, archivos y checkpoints v8, reconstruye únicamente
`inventory_checkpoints` y permite que `volume`, `journal_id` y `next_usn` sean
los tres `NULL` o los tres no nulos. Un checkpoint portable válido sigue
publicando su `scan_id` para Knowledge, Semantic y los lectores atómicos, pero
no autoriza reconciliación incremental USN. La migración valida exactamente la
fuente v8, compara conteos, ejecuta `foreign_key_check` y se revierte completa
ante una estructura desconocida o una excepción.

### Migración Dedup v9→v10

La migración es aditiva y conserva el DDL v9 como contrato histórico exacto:
antes de escribir rechaza tablas, índices o triggers desconocidos. Después crea
únicamente estos dos índices para la consulta real de relaciones de Knowledge:

```sql
CREATE INDEX files_identity_birth_scan_idx
ON files(volume_id, file_id, birthtime_ns, scan_id);

CREATE INDEX planned_members_identity_idx
ON planned_duplicate_members(volume_id, file_id, birthtime_ns);
```

La misma transacción compara antes y después el conteo y la suma de `size` tanto
de `files` como de `planned_duplicate_members`, ejecuta
`PRAGMA foreign_key_check` y sólo entonces avanza `metadata.schema_version` a
10. Una anomalía revierte índices y versión. La apertura posterior valida el
contrato v10 exacto y no repite DDL.

En una copia aislada del inventario, la consulta que motivó los índices pasó de
aproximadamente 1.33 s a 0.106 s. Es evidencia orientativa, no un SLA. La
regresión automatizada evita umbrales de reloj: ejecuta la sentencia productiva
y exige mediante `EXPLAIN QUERY PLAN`, con índices automáticos desactivados, que
SQLite seleccione explícitamente ambos índices v10.

### Migración histórica del catálogo v1→v2

`NC-AUD-018` reprodujo dos pérdidas silenciosas en la antigua migración v1→v2
de `classification_history`: una columna extra con datos y una tabla que
colisionaba con el nombre temporal. La corrección existe desde `0.5.0`:

- valida exactamente la definición v1 y rechaza triggers desconocidos;
- se abstiene si existe el objeto reservado `classification_history_v2`;
- usa `INSERT` sin `OR IGNORE`;
- compara conteos antes de retirar la fuente;
- conserva el rollback exterior ante `BaseException`.

Las regresiones para columna, trigger y tabla reservada viven en
`test_audit_continuation_regressions.py`. Una instalación con catálogo v1 debe
respaldarse y migrarse primero sobre una copia; el informe técnico fechado
registra la barrera ejecutada.

## Publicación generacional de inventario: Dedup v10

### Estado de fuente y estado vivo

La fuente `0.9.0` declara esquema v10. La base viva inspeccionada históricamente conservaba
`metadata.schema_version='6'`; se abrió sólo para checks y no se permitió que el
initializer la migrara. Antes de usar esa base con `0.9.0` debe aplicarse el
procedimiento de backup y actualización de este documento.

Los hallazgos `NC-AUD-001`, `NC-AUD-002` y `NC-AUD-003` quedaron corregidos en
el incremento v7 y conservan regresiones específicas. La barrera exacta de esta
entrega pertenece al informe técnico fechado.

### Esquema y estados

V10 conserva la semántica generacional de v9, incluidos los estados
introducidos por v7, y liga cada scan nuevo a `inventory_policy_signature`:

| Estado | Significado | Visible mediante el lector publicado | Puede recibir checkpoint válido |
|---|---|---|---|
| `building` | scan iniciado y todavía no finalizado | no | no |
| `complete` | scan terminado con `errors=0` y agregados completos | sólo si el checkpoint lo referencia | sí, después de validar conteos |
| `partial` | scan terminado con uno o más errores de recorrido | no | no |

`scans.status` tiene un `CHECK` limitado a esos tres valores. `files` usa
`PRIMARY KEY(scan_id, path)` y una foreign key restrictiva hacia `scans`;
`inventory_checkpoints.scan_id` tiene la misma FK. La factory operativa activa
y comprueba `PRAGMA foreign_keys=ON`.

Una ruta observada en dos scans produce dos filas independientes. Construir una
generación ya no transfiere mediante `ON CONFLICT(path)` la fila que pertenece
al checkpoint anterior.

### Construcción y publicación

El scan empieza como `building`. Al finalizar, el scanner guarda contadores y
lo clasifica `complete` o `partial`. Los batches de la generación nueva pueden
confirmarse porque los lectores publicados todavía resuelven el checkpoint
anterior.

`bind_inventory_checkpoint` sólo acepta un scan:

- de la misma raíz normalizada;
- con estado `complete` y `completed_ns`;
- con todos los agregados presentes;
- con `errors=0`;
- cuyo `files_seen` coincide con `COUNT(files)`;
- cuyos `bytes_seen` coinciden con `SUM(size)`.

Después de validar, el reemplazo de `inventory_checkpoints` ocurre dentro de una
transacción SQLite. El checkpoint contiene raíz, `scan_id`, validez y timestamp,
y resuelve la firma cruda desde su scan. `volume`, `journal_id` y `next_usn`
están presentes juntos cuando USN acota la generación o permanecen juntos en
`NULL` para una publicación portable. Hasta ese cambio, la generación complete
es sólo candidata y no visible por la API publicada.

Una excepción antes del commit conserva el checkpoint anterior. Una
discontinuidad del journal invalida su reutilización incremental y exige una
reconciliación/full scan acorde con el orquestador. Si USN no está disponible,
el coordinador ejecuta un full scan portable, publica cursor nulo y las rutas
reutilizan sus caches sobre el nuevo snapshot.

### Lector público atómico

La API pública para leer la generación vigente es:

```python
index.published_snapshots(root)
```

La implementación une `inventory_checkpoints` y `files` en **una sentencia
SQL**. El cursor conserva el snapshot SQLite que existía al iniciar la lectura;
una publicación y poda concurrentes no mezclan filas de dos generaciones.

No emule este contrato con:

```python
checkpoint = index.inventory_checkpoint(root)
rows = index.snapshots(checkpoint.scan_id)
```

Entre esas dos consultas otro writer puede publicar y podar la generación
seleccionada. `inventory_checkpoint` sigue siendo útil para cursor/diagnóstico,
pero el pairing con `snapshots` queda desaconsejado para lectura autoritativa.

### Exploraciones parciales y cursor USN

Un error de recorrido termina el scan como `partial`, conserva su estado y hace
que `scan()` eleve `InventoryError`; no se crea checkpoint. El coordinador
también rechaza un summary con errores y conserva el checkpoint previo.

La reconciliación USN acumula un batch sólo cuando puede resolverlo. Si una
identidad o ruta ambigua exige rescan, no aplica ni publica el batch inseguro ni
adelanta su cursor. La prueba focal usa un FileId sintético no resoluble y
verifica ausencia de aplicación/avance.

Una caída todavía puede dejar un scan `building`. V7 lo mantiene invisible,
pero no implementa aún expiración, ownership durable o conciliación automática
de un `building` abandonado; ese crecimiento residual debe diagnosticarse antes
de diseñar retención.

### Poda generacional

`prune_obsolete_state()` borra por lotes planes, archivos y fingerprints no
referenciados. Es una poda específica del propietario de inventario, no el
ejecutor del planificador general. Exige que el coordinador entregue
explícitamente todos los `scan_id` retenidos por otros stores; si recibe
`protected_scan_ids=None`, falla cerrado y no borra nada. Su conjunto de
retención incluye:

- todo scan `building`;
- el scan de cada checkpoint válido;
- la publicación completa inmediatamente anterior de cada raíz;
- todo `scan_id` incluido en los holds cross-store explícitos;
- un scan `complete`, consistente y más nuevo que el publicado para su raíz,
  porque puede ser candidato inmediato de publicación.

El coordinador obtiene de framework los scans referenciados antes de invocar la
poda. Como SQLite no ofrece foreign keys entre archivos, si no puede construir
ese conjunto completo debe abstenerse: una referencia cross-store no puede
inferirse desde `dedup.sqlite3`. Las pruebas focales conservan building,
candidatos, publicación vigente, publicación anterior y un scan histórico
referenciado por framework. Los scans `building` abandonados se retienen
indefinidamente por seguridad, lo que constituye un riesgo de crecimiento aún
abierto. Las filas de `scans` conservan además historial aunque se poden sus
archivos derivados. El segundo punto de restauración durable sigue siendo el
backup consistente; conservar dos publicaciones locales no sustituye ese
backup.

## Migración dedup v6→v7 implementada

La migración se ejecuta dentro del lifecycle transaccional del propietario:

1. valida de forma **exacta** el contrato v6 antes de DDL destructivo;
2. se abstiene ante columna, índice u objeto desconocido;
3. rechaza flags de checkpoint no booleanos y referencias huérfanas;
4. cuenta archivos y checkpoints;
5. renombra temporalmente las dos tablas v6 conocidas;
6. añade `status` y deriva `complete` sólo si `errors=0`, contadores no
   negativos y conteo/suma de archivos coinciden; el resto queda `building` o
   `partial`;
7. crea las tablas v7 con PK/FK nuevas y copia los datos sin `OR IGNORE`;
8. conserva un checkpoint válido sólo si su scan quedó `complete`;
9. compara los conteos de origen y destino;
10. retira las tablas temporales conocidas, crea índices y ejecuta
    `foreign_key_check`;
11. actualiza metadata a v7 y valida el contrato exacto antes del commit.

Ante `BaseException`, el lifecycle revierte. Una base v6 con estructura
desconocida conserva v6 y sus datos; no se intenta “repararla”. El downgrade
sigue consistiendo en restaurar el backup v6 y el paquete compatible, nunca en
cambiar el número de versión.

### Validación focal v7

Las nueve pruebas sintéticas y acotadas cubren:

- migración v6 poblada, reapertura idempotente, conteos, bytes, checkpoint,
  integrity y FK;
- abstención ante columna v6 desconocida y preservación del payload;
- misma ruta aislada en dos generaciones y switch de checkpoint;
- scan parcial sin publicación;
- rechazo de un scan complete con agregados inconsistentes;
- lector publicado concurrente con publicación y poda;
- retención de building y candidato complete;
- FileId ambiguo sin aplicación ni avance de cursor;
- avance incremental exitoso y checkpoint consistente.

La suite completa, cobertura, lint, typing, build e instalación pertenecen a la
barrera final del informe técnico, no a este contrato de persistencia.

## Publicación generacional semántica y de catálogo

### NC-AUD-012 — semántica v6

Cada `model_signature` publica exactamente un head. Un build `building` conserva
su `base_generation_id`, fija ID, estado, modelo, high-watermark y conteo de esa
base, y clona por lotes los miembros publicados. Tras cada página confirma en
`cursor_json.base_clone` el último `member_id`, el número recorrido y la marca de
completitud; un deadline conserva ese prefijo para reanudarlo. Agrega jobs y
revisiones inmutables sin cambiar la vista del lector. La finalización completa
verifica jobs, miembros, clon y que la base fijada no cambió; marca la generación
`ready` y cambia
`published_embedding_heads` por CAS dentro de `BEGIN IMMEDIATE`. Fallos, leases
pendientes, cancelación o `ready_partial` no publican. Un worker tardío no puede
adjuntar resultados a una generación que ya no esté `building`.

El staging textual abre una sola conexión por fuente y usa transacciones
`BEGIN IMMEDIATE` acotadas a 128 items o 128 chunks; un item con más chunks se
parte en varias transacciones. Items, chunks y jobs del lote comparten commit.
Ante `RuntimeError`, cancelación o cualquier otra `BaseException`, la factory
revierte el lote activo y conserva el prefijo ya confirmado dentro del build.
Ese prefijo sigue invisible para lectores publicados y el mismo refresh puede
reanudarlo sin duplicar estado. Sólo después de recorrer toda la fuente se
desactivan items ausentes. No cambian el esquema 6, las APIs ni los contratos
JSON.

Antes de cada `claim_embedding_jobs`, el worker repite
`reuse_cached_jobs` hasta alcanzar un punto fijo. La igualdad exige
`model_signature`, XXH3-128, bytes y guarda XXH3-64; un vector persistido al
completar el batch N queda disponible para duplicados pendientes antes del
batch N+1. Si la construcción de requests, heartbeat o registro se interrumpe,
los leases todavía propios pierden owner/deadline y vuelven a retry o a error
terminal según sus intentos; `RuntimeError`, `KeyboardInterrupt` y
`BaseException` no dejan esos jobs en `leased` ni publican el build. No se
coalescen duplicados ya reclamados en el mismo batch y las transiciones de
éxito/fallo siguen abriendo operaciones por job.

`resolve_search_hits` conserva miembro, contenido e identidad de la revisión
publicada, pero resuelve el localizador `path` desde el elemento actual sólo si
también coinciden `item_id`, `source_kind` y `source_identity`. Un move seguro
puede actualizar la ruta sin reescribir evidencia; una reasignación de identidad
se abstiene con error en vez de apuntar un hit histórico a otro objeto. Espacio
vectorial y modalidad también deben coincidir con `embedding_models`; un hit
fabricado no puede cambiar por sí mismo el contrato del modelo.

Para Code, la resolución añade una segunda cerca: `search_code` sólo traduce un
hit Semantic a un `code_chunk` si existe un `embedding_links` activo con el mismo
item, modelo, espacio vectorial y generación publicada, y si ese chunk pertenece
a la versión actual. La procedencia lo marca como
`authority=retrieval_evidence_only` y `calibration=uncalibrated_similarity`.
Ausencia del modelo en cache, cobertura incompleta o deriva de cualquiera de las
dos bases deja el canal indisponible; no provoca descarga ni fallback permisivo.

La migración exacta v5→v6 conserva las tablas legacy e importa, por modelo, sólo
la vista v5 que los lectores podían observar: items/chunks activos cuyos hashes
coinciden con el embedding. Compara el conteo de miembros, crea un head `ready`,
valida FK, integridad y el contrato v6 exacto antes del commit y registra la
migración 6. Columnas, índices, triggers u objetos v5 desconocidos impiden el
commit y la transacción se revierte; no se promete una apertura byte-neutra de
WAL/SHM. Un `BaseException` también revierte. El rollback operativo consiste en
restaurar el backup v5 y el paquete compatible.

### NC-AUD-013 — catálogo v6

Cada `source_kind` tiene una generación `building` y un puntero en
`catalog_publications`. Los UPSERT por lote sólo tocan
`catalog_generation_documents`. Al finalizar, una única transacción comprueba
el head base, reemplaza la proyección compatible `documents`, agrega historial,
invalida planes que ya no apuntan a documentos publicados y cambia el puntero
por CAS. Fallo/cancelación deja la generación anterior; una publicación
competidora marca la atrasada `superseded`.

La migración v5→v6 exige el contrato v5 exacto, conserva `documents`, historial,
planes y datos, crea una generación publicada por `source_kind`, compara
conteos y ejecuta `foreign_key_check`. El índice de destino de organización se
amplía para que `recovery_required` reserve la ruta. Una anomalía revierte toda
la transacción.

### Límites de ambos contratos

Las garantías corresponden a repositorios/consultas oficiales. SQL externo que
lea directamente tablas legacy mutables puede observar otra semántica. No hay
todavía poda por lotes ni política de edad/cuota para builds fallidos,
cancelados, `superseded`, `ready_partial` o abandonados; se preservan por
diagnóstico. La generación anterior se mantiene durante construcción y fallo,
pero la conservación histórica indefinida no sustituye un backup consistente.

## Foreign keys e integridad

### Cobertura declarada

- Path index, Office e imagen no declaran foreign keys en sus esquemas actuales.
- Framework v20 conserva relaciones append-only de transiciones y conciliaciones
  a acciones y añade policy/identidad protegidas; catálogo
  v6 declara las relaciones de run/base/publicación con sus generaciones. Las
  demás asociaciones de ambos dominios continúan siendo lógicas.
- Dedup v10 declara FK restrictivas desde `files` y `inventory_checkpoints` a
  `scans` y activa su enforcement en la factory.
- PDF, DOCX y audio declaran algunas relaciones locales.
- Code y semantic declaran relaciones extensas.
- No existen foreign keys entre bases diferentes.

`PRAGMA foreign_key_check` sólo puede comprobar relaciones declaradas. Un
resultado vacío en framework o catálogo no demuestra que `run_id`, `scan_id` o
planes apunten a filas lógicamente válidas.

### Reglas para factories

Toda factory debe fijar y probar:

- timeout y `busy_timeout`;
- `foreign_keys=ON` cuando el contrato contenga FK;
- journal/synchronous/cache/autocheckpoint/journal limit para writers;
- URI `mode=ro` y, cuando aporte defensa adicional, `query_only=ON` para
  readers;
- row factory esperada;
- commit, rollback ante `BaseException` y cierre.

La prueba de conexión debe enumerar factories públicas, workers, procesos hijos
y helpers internos que abren SQLite directamente.

### Validación de una copia

Con writers detenidos, cada backup debe pasar:

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

Además debe validarse el contrato de esquema de su propietario y sus invariantes
lógicos. No ejecute migraciones sobre el único original para “ver si abre”.

## Validación viva read-only del 24 de julio de 2026

### Preconditions y método

Antes de abrir SQLite se confirmó que no había procesos `Neocortex`, `py`,
`python` o `pythonw`, que `framework.lock` podía adquirirse y que los metadatos
de `.sqlite3`, WAL y SHM permanecían estables durante tres segundos. El auditor
mantuvo `framework.lock` durante toda la inspección.

Cada base presente se validó en un subproceso con timeout según tamaño. La
conexión usó exclusivamente:

- URI `file:...?mode=ro`;
- `PRAGMA query_only=ON`, comprobado como `1`;
- `PRAGMA busy_timeout=5000`;
- `PRAGMA integrity_check`;
- `PRAGMA foreign_key_check`;
- lecturas de journal, versión, metadata, páginas y freelist.

No se ejecutó migración, checkpoint, `VACUUM`, DDL ni DML. `code.sqlite3` y
`semantic.sqlite3` no existían; se validaron las ocho bases presentes.

### Resultados

| Base | Tamaño | Metadata / user_version | Integrity | FK | Duración total | Páginas / libres |
|---|---:|---|---:|---:|---:|---:|
| audio | 3,674,112 B | 1 / 0 | `ok` (0.027 s) | 0 (0.015 s) | 0.061 s | 897 / 62 |
| dedup | 167,231,488 B | **6** / 0 | `ok` (4.768 s) | 0 (<0.001 s) | 4.790 s | 40,828 / 22,426 |
| document catalog | 726,437,888 B | 5 / 0 | `ok` (29.908 s) | 0 (<0.001 s) | 29.932 s | 177,353 / 0 |
| DOCX | 310,702,080 B | 5 / 0 | `ok` (8.944 s) | 0 (3.239 s) | 12.206 s | 75,855 / 7,544 |
| framework | 178,352,128 B | **16** / 0 | `ok` (4.849 s) | 0 (<0.001 s) | 4.874 s | 43,543 / 3,677 |
| image | 188,354,560 B | 5 / 0 | `ok` (6.033 s) | 0 (<0.001 s) | 6.057 s | 45,985 / 12,336 |
| Office | 40,435,712 B | 1 / 0 | `ok` (0.920 s) | 0 (<0.001 s) | 0.933 s | 9,872 / 2,969 |
| PDF | 1,517,887,488 B | 11 / 0 | `ok` (39.398 s) | 0 (0.364 s) | 39.771 s | 370,578 / 93,013 |

Resultado lógico: 8/8 `integrity_check=['ok']`, 8/8 sin filas de
`foreign_key_check`, ningún timeout; suma de los subprocesos, aproximadamente
98.625 s. Un FK check vacío no valida relaciones lógicas no declaradas.

La fuente soporta framework v20, Dedup v10, catálogo v6 y semántica v6, mientras
las bases vivas seguían en framework v16, dedup v6 y catálogo v5; semantic no
existía. Esa diferencia es esperable antes de actualizar, pero demuestra que
leer el árbol no sustituye consultar la instalación. No se migraron para cerrar
la diferencia. `user_version=0` es coherente con estas ocho bases, que versionan
por metadata; code/semantic, las que sincronizan `user_version`, estaban
ausentes.

### Incidencia WAL/SHM: la inspección no fue byte-neutra

Pese a `mode=ro` y `query_only=ON`, SQLite necesita el wal-index compartido para
leer una base en modo WAL cuando el directorio es escribible. La comparación
antes/después detectó:

- ninguna modificación de tamaño o `mtime` en los ocho archivos `.sqlite3`;
- ningún cambio de tamaño o `mtime` en los WAL que ya existían, incluido el WAL
  PDF de 436,752 B;
- creación de `-wal` vacío y `-shm` de 32 KiB para audio, dedup, catálogo e
  imagen, que no tenían auxiliares al iniciar;
- cambio de `mtime` en los SHM de DOCX, framework, Office y PDF. La comparación
  global de auxiliares dejó de ser idéntica por las creaciones y cambios, pero
  no se conservó un diff por byte de cada SHM preexistente.

No se eliminaron esos auxiliares ni se intentó “restaurar” sus bytes. Borrarlos
habría añadido una operación destructiva y podría interferir con otro lector.
Por tanto, los checks lógicos son válidos, pero la condición «no escribir
WAL/SHM» **no se cumplió byte por byte** con el driver SQLite estándar. Una
auditoría que requiera neutralidad forense debe operar sobre un backup
consistente cerrado, no sobre el estado WAL vivo. No use `immutable=1` sobre una
base cuyo WAL contenga frames que deban leerse, porque puede omitirlos.

## Backup consistente

NeoCortex no contiene actualmente un comando general incorporado de backup. El
procedimiento operativo canónico está en [RECOVERY.md](RECOVERY.md) y usa
`sqlite3.Connection.backup`.

Contrato:

1. cancelar cooperativamente y detener todos los writers propios;
2. confirmar el directorio de estado exacto;
3. elegir un destino nuevo fuera de ese árbol;
4. descubrir todas las bases `*.sqlite3` presentes, sin asumir que code o
   semantic existen;
5. abrir el origen `mode=ro` y usar la API de backup hacia una base nueva;
6. validar cada destino con integrity, FK y versión/contrato;
7. conservar una manifestación de nombres, versión de aplicación, fecha y
   resultado;
8. no borrar un destino parcial si el procedimiento falla: marcarlo incompleto
   y crear otro después de resolver la causa.

Copiar sólo `archivo.sqlite3` no es válido si existe WAL. La API de backup
incorpora las páginas confirmadas visibles para SQLite; no requiere copiar
`-wal` ni `-shm` al destino.

Aunque SQLite permite backup con un writer activo por base, el conjunto de
NeoCortex no tiene una transacción distribuida. Detener writers evita snapshots
de instantes incompatibles entre dedup, framework, rutas y catálogo.

La caché de modelos y `ui.ini` no forman parte del backup SQLite. Los modelos
pueden respaldarse por separado si se necesita reproducibilidad exacta de los
bytes, pero nunca mientras se estén descargando o reemplazando.

## Restauración

No existe un comando público general de restauración. Restaurar es una operación
explícita y potencialmente destructiva sobre el estado actual:

1. instalar primero una versión compatible con el backup;
2. detener writers y crear un backup pre-restauración del estado actual;
3. validar sin migrar el conjunto que se restaurará;
4. restaurar cada base mediante la API SQLite, no por copia sobre un archivo con
   WAL;
5. mantener nombres y propietarios;
6. validar inmediatamente integrity, FK, versión y contrato;
7. si una base falla, detenerse; no terminar con una mezcla deliberada de dos
   conjuntos;
8. ejecutar status/doctors antes de cualquier reproceso o acción.

Una restauración lógica puede dejar archivos WAL/SHM del destino controlados por
SQLite. No los elimine manualmente. La secuencia y el código de referencia están
en [RECOVERY.md](RECOVERY.md).

## Actualización, migración y downgrade

### Actualización

Antes de la primera apertura de estado real con un paquete nuevo:

1. verificar wheel/sdist e instalación limpia por separado;
2. registrar versión actual y status;
3. crear y validar backup de todas las bases;
4. probar migraciones sobre copias pobladas representativas;
5. detener watchers y writers;
6. abrir con la nueva versión y conservar la salida exacta;
7. validar contratos y doctors antes de ejecutar el corpus.

Las migraciones deben ser monotónicas, transaccionales, idempotentes y
abstenerse ante información no reconocida. Cambiar un número de versión sin
transformar y validar datos no es una migración.

### Downgrade

No hay downgrade destructivo automático. Un binario anterior debe rechazar una
base con versión futura.

El único rollback soportable es:

- reinstalar el paquete compatible; y
- restaurar el backup completo creado antes de migrar.

No edite `metadata.schema_version`, `PRAGMA user_version` ni
`schema_migrations`. Ninguna base nueva vuelve a su versión anterior cambiando
el entero: columnas, índices, heads, eventos y filas ya tienen otro contrato.

### Fallo durante migración

Si la transacción no llegó a commit, el initializer debe hacer rollback y la
base conservar su versión anterior. Preserve base, WAL/SHM y error. Si el commit
terminó pero el paquete posterior falla, no intente DDL inverso: restaure el
backup completo compatible.

## Retención y crecimiento

### Estado observado

La fotografía inicial de metadatos de archivos observó:

| Archivo | Tamaño aproximado |
|---|---:|
| `pdf.sqlite3` | 1,517,887,488 B |
| `document_catalog.sqlite3` | 726,437,888 B |
| `docx.sqlite3` | 310,702,080 B |
| `image.sqlite3` | 188,354,560 B |
| `framework.sqlite3` | 178,352,128 B |
| `dedup.sqlite3` | 167,231,488 B |
| `office.sqlite3` | 40,435,712 B |
| `audio.sqlite3` | 3,674,112 B |

Las ocho bases sumaban 3,133,075,456 B; con auxiliares WAL/SHM,
3,133,643,280 B. `code.sqlite3` y `semantic.sqlite3` no estaban presentes. La
caché FastEmbed medía 1,504,224,615 B. No es un benchmark. La validación viva
posterior sí leyó `page_count` y `freelist_count`, consignados arriba; una página
libre es reutilizable, pero no demuestra que todo ese espacio pueda truncarse ni
autoriza `VACUUM`.

### Poda existente

Se observó poda acotada de:

- archivos, planes y fingerprints de generaciones dedup no retenidas. V7
  conserva publicación vigente y anterior, todo `building`, candidatos
  `complete` más nuevos y holds cross-store explícitos. La llamada falla
  cerrado sin esos holds y no expira un building abandonado;
- cachés PDF/DOCX/Office/Archive/texto/audio/imagen que una reconciliación satisfactoria marca
  como ausentes;
- candidatos de ruta transitorios;
- caché de detección de tipo.

No existe una política de eliminación global para historiales de
runs/acciones/revisión, generaciones de catálogo o semántica fallidas/parciales,
payloads vectoriales, modelos o compactación del archivo principal. El árbol
actual sí permite inventariar una página protegida/elegible sin borrar.

### Planificador dry-run actual

`Neocortex --retention-status` abre únicamente bases existentes y reconoce los
contratos exactos de framework v20, inventario v10, catálogo v6 y semántica v6.
No crea ni migra estado. `--retention-store` acota propietarios,
`--retention-batch-size` limita 1..1000 y los cursores
`--retention-<store>-after` avanzan por keyset, nunca por `OFFSET`.

El planner fija `keep_published=2` y protege publicación vigente/anterior,
el último run `completed` de framework aunque existan runs fallidos o
cancelados posteriores, builders/leases vivos, generaciones base, checkpoints,
acciones `recovery_required`, eventos de auditoría y evidencia humana. Una fila
de `semantic_evidence` que referencia una generación semántica actúa como hold
y bloquea su elegibilidad. Deriva del esquema, dependencias incomprensibles o
FK no activables bloquean el store. Sin `--retention-min-age-days`, la política
queda `policy_not_configured` y no declara elegibilidad por antigüedad. Los
bytes estimados sólo suman payload `TEXT`/`BLOB`: son una cota inferior, no
bytes que el archivo vaya a liberar.

Cada base se lee bajo su propio snapshot estable; no hay snapshot distribuido
entre archivos. Una apertura `mode=ro` puede crear/tocar SHM al participar en un
WAL existente. El comando no hace checkpoint, `VACUUM`, delete ni enforcement
de cuotas, y devuelve `2` si algún store está bloqueado.

No existen comandos productivos `--retention-prepare`, `--retention-apply` o
`--retention-verify`, ni un journal durable de progreso de poda. La ejecución
genérica permanece bloqueada: las referencias lógicas cross-DB no pueden
protegerse con una transacción SQLite única y todavía no existe un protocolo
write-ahead de holds más un journal owner-local que haga cada lote reanudable e
idempotente. La salida de `--retention-status` no autoriza convertir candidatos
en `DELETE` manuales.

### Política para una futura ejecución

Clasifique antes de podar:

| Clase | Ejemplos | Política segura |
|---|---|---|
| Evidencia y auditoría | decisiones, acciones, errores, runs de incidente | conservar o archivar según política explícita; no borrar por edad sola |
| Generaciones recuperables | publicada, candidatos, building y evidencia partial | respetar el predicado del propietario; el rollback durable usa backup y un building abandonado requiere política explícita |
| Caché recomputable | FTS/derivados/model cache que tiene productor confirmado | candidata a poda explícita con dry-run y conteos |
| Estado contractual | metadata, migraciones, checkpoints, firmas | nunca podar como caché |

Una futura operación de mantenimiento con escritura —no disponible en la CLI
actual— debe:

1. ser de sólo diagnóstico por defecto;
2. informar filas/bytes estimados y referencias;
3. adquirir el lock apropiado;
4. exigir backup validado;
5. borrar por keyset y lotes acotados;
6. conservar la generación publicada y rollback;
7. validar integridad y conteos;
8. mantener holds cross-store durables y un journal reanudable por propietario;
9. compactar sólo de forma explícita, sin writers y después de medir freelist.

No ejecute `VACUUM`, cambie `journal_mode` ni borre WAL/SHM para reducir tamaño
sin esa política. Borrar filas permite reutilizar páginas, pero no garantiza que
el archivo se reduzca.

## Rendimiento de consultas administrativas

`semantic_status` conserva los conteos exactos de nueve tablas; desde `0.5.0`
eliminó la conexión y la consulta adicional por generación: abre una sola
conexión read-only, inicia un snapshot WAL y agrega los summaries de hasta 1000
ids en una consulta. Así evita N+1 y mezcla de publicaciones concurrentes
(`NC-AUD-019`). Los nueve `COUNT(*)` siguen siendo scans potencialmente
costosos. En el fixture sintético idéntico de 250 generaciones pasó de 251 a 1
conexión y de 1512 a 19 statements. En 20 invocaciones la mediana final fue
13.852 ms wall; en cinco lotes de 50 fue 14.063 ms wall y 5.625 ms CPU por
invocación. El lector mantuvo un único snapshot consistente. Es una medición
sintética, no una cifra del corpus vivo.

## Checklist para cambiar un esquema

Antes de aceptar una migración:

- [ ] propietario y consumidor identificados;
- [ ] versión monotónica y contrato anterior exacto;
- [ ] backup y restauración documentados;
- [ ] datos/columnas/objetos desconocidos provocan abstención;
- [ ] DDL y transformación dentro de una transacción adecuada;
- [ ] conteos y relaciones verificados antes de retirar estructuras legacy;
- [ ] rollback ante `sqlite3.Error`, `RuntimeError`, `KeyboardInterrupt` y otra
      `BaseException`;
- [ ] base poblada, idempotencia e interrupción probadas;
- [ ] readers concurrentes y WAL considerados;
- [ ] factory y foreign keys probadas en todas las conexiones;
- [ ] actualización y downgrade documentados;
- [ ] retención/compatibilidad de generaciones definida;
- [ ] suite, lint, typing, paquete e instalación validados antes de publicar.

## Riesgos y límites que deben permanecer visibles

| ID | Estado en este borrador | Límite |
|---|---|---|
| NC-AUD-001 | corregido y conservado | v7 usa PK `(scan_id,path)`, checkpoint y lector publicado atómico |
| NC-AUD-002 | corregido y conservado | scan con errores queda `partial` y no publica |
| NC-AUD-003 | corregido y conservado | reconciliación ambigua no aplica ni adelanta cursor |
| NC-AUD-010 | corregido en el subconjunto soportado | rename/organización ligan identidad por handles; casos no soportados y Papelera se abstienen |
| NC-AUD-011 | corregido y revalidado para observaciones | recibos/eventos y `recovery_required`; status no escribe y record conserva clasificación append-only/CAS sin autorizar recuperación; plan incierto no se reintenta |
| NC-AUD-012 | corregido para lectores oficiales v6 | heads por modelo, revisiones congeladas y publicación CAS completa |
| NC-AUD-013 | corregido para lectores oficiales v6 | staging por fuente y proyección/publicación CAS atómica |
| NC-AUD-014 | corregido parcialmente | planner dry-run keyset protege estado crítico, evidencia semántica y último run completado; la poda dedup exige holds explícitos y conserva dos publicaciones, pero no hay ejecución genérica, cuotas ni poda global |
| NC-AUD-015 | pendiente reproducido | fastpath estable y reconstrucción fail-closed reducen trabajo repetido, pero esquema 4 sigue no generacional, global, sin reanudación ni cancelación dentro de sentencia; no se fragmentó sin contrato completo |
| NC-AUD-017 | corregido y revalidado para propietarios oficiales | 37 llamadas directas/21 módulos y 132 adquisiciones/20 factories clasificadas; SQL externo y relaciones no declaradas quedan fuera |
| NC-AUD-018 | corregido y conservado | migración catálogo se abstiene ante estructura/trigger/objeto desconocido y compara conteos |
| NC-AUD-019 | corregido estructuralmente | un solo snapshot/conexión; permanecen nueve conteos completos cuyo costo se mide aparte |
| NC-AUD-020 | corregido en la fuente | lease OS cross-process por raíz+estado; conflicto vivo se abstiene y stale se recupera sin matar procesos |
| NC-AUD-021 | pendiente de decisión humana | el proyecto no declara licencia propia ni un NOTICE jurídico |

La tabla registra estado técnico, no una orden de ejecutar migraciones sobre el
estado vivo. Las pruebas deben usar bases temporales o copias autorizadas. La
conclusión final de la auditoría debe actualizar cada fila según los cambios y
la barrera realmente ejecutada.
