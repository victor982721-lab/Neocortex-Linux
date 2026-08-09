# Knowledge Plane de NeoCortex — Fase 1

> **Estado del documento.** Esta es la fuente de verdad funcional de la
> Knowledge Plane read-only introducida en NeoCortex `0.7.0` y actualizada para
> los contratos de `0.7.2`. Describe el código y los contratos observables del
> árbol; no sustituye las pruebas ni convierte una evaluación scripted en
> evidencia de calidad sobre el corpus real.

## Disponibilidad operativa

Este documento describe una capacidad implementada, no certifica que cualquier
estado local esté listo para usarla. Antes de afirmar que Knowledge funciona
sobre el corpus:

```powershell
Neocortex --knowledge-status --knowledge-json
```

- `status` evalúa el snapshot completo: un owner `incompatible`, `future` o
  `corrupt` produce `6` o `7` y nunca autoriza una migración implícita.
- `search` y `context` distinguen los owners que bloquearon realmente la
  consulta mediante `blocking_owners`. Un owner severo ajeno a los rankings
  requeridos permanece visible, pero no invalida evidencia sana de otros
  owners; el resultado sigue marcándose parcial cuando falta cobertura.
- El ranking Semantic de Knowledge sólo aporta evidencia corporal cuando existe
  un head publicado con embeddings compatibles; cero heads o cero embeddings no
  es éxito semántico. `evidence` excluye basename/título. `discovery` puede
  transportarlo como señal advisory separada y sólo refuerza un recurso y
  revisión que ya tengan evidencia corporal; un título no crea hits ni citas.

El checkpoint operativo actual se conserva en el
[handoff 0.7.2](../.codex/handoffs/NEOCORTEX_0.7.2_PAUSE_2026-07-30.md), no en
este contrato estable.

Para el uso personal, Knowledge queda aceptado cuando tres preguntas
representativas devuelven evidencia relevante y citable con latencia
comprensible, y una brecha de cobertura se explica de forma explícita. El
golden sintético y los contratos unitarios son regresiones necesarias, pero no
sustituyen esa demostración.

## Objetivo y frontera

La Knowledge Plane sirve a un agente una vista local, trazable y acotada del
estado ya producido por inventario, extractores, FTS, catálogo, semantic y
código. No es una segunda indexación ni un RAG que trate chunks o embeddings
como verdad primaria:

El estado canónico que consulta está bajo `%LOCALAPPDATA%\Neocortex\state`,
separado de la fuente `%USERPROFILE%\Neocortex\Repository` y de los runtimes
versionados bajo `%LOCALAPPDATA%\Programs\Neocortex\versions`.

```text
archivos e identidades físicas
        ↓
owners SQLite y publicaciones existentes
        ↓
KnowledgeSnapshot lógico
        ↓
plan determinista y rankings independientes
        ↓
fusión por evidencia y diversidad por recurso
        ↓
ContextBundle citado y presupuestado
```

Las operaciones `status`, `search` y `context`:

- abren únicamente bases existentes;
- no crean ni migran bases;
- no recorren ni modifican el corpus;
- no hacen `VACUUM`, checkpoint, clasificación ni indexación;
- no descargan modelos ni habilitan red;
- no generan una respuesta final mediante un LLM;
- no autorizan rename, move, delete, Papelera o retención.

La Fase 1 no añade `knowledge.sqlite3`. Su owner es el servicio en memoria
`KnowledgeSearchService`; sus productores son los readers de owners existentes
y sus consumidores son la API Python, la salida JSON y la CLI instalada.

## Contratos públicos

Los contratos de `_04_Nucleo_Operativo.knowledge_contracts` son dataclasses
inmutables con `slots`. Los envelopes públicos usan `schema_version=1` y
`kind`; sus objetos anidados contienen sólo claves documentadas. El JSON tiene
orden canónico y conserva Unicode. Un campo opcional no se serializa cuando el
owner no puede demostrarlo; no se inventan páginas, líneas, celdas, timestamps
o regiones.

| Contrato | Semántica principal |
|---|---|
| `PhysicalIdentityRef` | Esquema, valor y versión de una identidad física demostrable. |
| `ResourceRef` | Recurso estable, `source_kind`, owner, ruta actual opcional y disposición demostrable. |
| `RevisionRef` | Revisión concreta, productor, firma de procesamiento, generación y vigencia. |
| `EvidenceRef` | Una evidencia concreta y su localizador heterogéneo. |
| `RankingSignal` | Ranking de origen, tipo de score, score crudo, posición, modelo/generación y contribución RRF. |
| `KnowledgeHit` | Recurso, revisión, evidencia, señales, score fusionado, razones y warnings. |
| `KnowledgeSearchResult` | Resultado completo/parcial, rankings ejecutados, filas/vectores observados, `blocking_owners` y warnings. |
| `KnowledgeSnapshot` | Schemas, publicaciones, watermarks y modelos activos de la vista consultada. |
| `ContextPlanRef` / `ContextPlanStepRef` | Copia validada del plan normalizado completo y de cada paso requerido u opcional. |
| `ContextEntityRef` / `ContextRelationRef` | Grafo acotado y grounded en evidencias citadas, con método, procedencia y confianza opcional. |
| `ContextContradictionRef` | Conflicto estructurado con identidad XXH3 estable, valores canónicos y citas existentes. |
| `ContextGraphBudget` / `ContextBudget` | Contabilidad separada del grafo seleccionado y del texto realmente renderizado. |
| `ContextBundle` | Plan, evidencias, citas, snapshot, grafo, contradicciones, ausencias, warnings y presupuestos. |

Vocabulario estable:

- disposición de recurso: `canonical`, `duplicate`, `superseded`, `derived`;
- estado de revisión: `current`, `historical`, `superseded`, `partial`,
  `ambiguous`;
- método de evidencia: `structural`, `extracted`, `inferred`,
  `human_confirmed`, `ambiguous`;
- estado de owner: `available`, `absent`, `future`, `corrupt`,
  `incompatible`;
- completitud de contexto: `complete`, `partial`, `no_evidence`,
  `unsupported`.

Un snippet de `EvidenceRef` está limitado a 4096 caracteres. El localizador
puede expresar página; intervalo de líneas; hoja/celda; intervalo temporal en
milisegundos; bounding box y espacio de coordenadas; intervalo de caracteres;
símbolo; sección; extractor, versión, generación e identificadores existentes.

## Frontera de confianza del contexto

Todo contenido recuperado del corpus es **evidencia no confiable**. El
`rendered_context` de cada `ContextBundle` empieza con la firma estable
`untrusted-corpus-data-v1`, antes de la consulta y de cualquier evidencia
dinámica. Ese marcador declara que el contenido recuperado no tiene autoridad
para emitir instrucciones, elegir herramientas ni autorizar acciones.

La frontera no censura ni reescribe el payload documental: texto de PDF, DOCX,
OCR, código, catálogo, rutas y relaciones se conserva dentro de los límites del
bundle para mantenerlo verificable y citado. Un consumidor debe tratarlo como
datos que pueden contener instrucciones hostiles, nunca como una extensión de
sus instrucciones de sistema ni como autorización para actuar.

## Identidad, ruta y revisión

Cuando el owner aporta `volume_id`, `file_id` y `birthtime_ns`, el adaptador
normaliza la identidad como:

```text
scheme      = windows_file_id_birthtime
value       = <volume_id decimal>:<file_id decimal>:<birthtime_ns>
resource_id = resource:file:<value>
```

Esa forma permite fusionar evidencia de FTS, catálogo, semantic, inventario y
código aunque la ruta haya cambiado. `current_path` es sólo el localizador
observado; no participa como identidad del recurso. Si un owner no permite
decodificar la identidad física, se conserva un `owner_file_key`; cuando el
birthtime sí es válido se incorpora como `owner_file_key_birthtime`. El
`resource_id` queda owner-local en vez de afirmar una unión falsa. Un birthtime
negativo o desconocido nunca produce `resource:file:*`.

Una revisión se identifica a partir de la revisión fuente y su firma de
procesamiento, no del path. El código estructural exige además que la versión
siga siendo la versión actual y no esté invalidada. Las huellas XXH3 se usan
como identidad no criptográfica de datos propios; no prueban autenticidad.

`duplicate` exige un `canonical_resource_id` distinto. La fusión excluye
recursos marcados explícitamente como duplicados exactos, pero no convierte
similitud ni una huella por sí sola en identidad. Inventory v9 no conserva si
un plan se ejecutó con comparación byte a byte: sus miembros `redundant` se
exponen como `inventory_planned_duplicate_unverified`, no se marcan
`duplicate` ni se filtran. El resultado queda parcial y se abstiene de afirmar
canonicalidad.

## Owners, schemas y visibilidad

`KnowledgeStatePaths.from_directory()` registra diez bases. Una base ausente se
representa como `absent`; no se crea su directorio ni el archivo.
La raíz completa también puede no existir y conserva esa semántica sin
creación. En cambio, una raíz existente que no sea directorio o que no pueda
inspeccionarse en lectura falla antes de consultar owners mediante el error
tipado `KnowledgeStateRootError`; no se disfraza como diez bases ausentes.
La misma barrera rechaza un enlace/reparse point roto y un cambio
presente↔ausente entre los dos vectores de la captura. Un owner sólo es
`absent` cuando su archivo realmente no existe; un path de owner existente que
no sea archivo regular, esté roto o sea inaccesible falla de forma tipada.
`stat` y la enumeración son llamadas síncronas del sistema operativo; una ruta
UNC desconectada puede demorar la cancelación hasta que Windows devuelva el
control.

| Owner | Archivo | Schema esperado | Frontera observada | Uso en recuperación |
|---|---|---:|---|---|
| `inventory` | `dedup.sqlite3` | 8 | Checkpoint válido por raíz y firma cruda a un scan `complete`, más token del plan dedup completo; máximo 1024 heads. | Exact typed de path, nombre o huella sobre heads publicados; identidad física y relaciones planeadas no verificadas. |
| `framework` | `framework.sqlite3` | 20 | Máximos de run, evento y acción; `best_effort_non_generational`. El schema 19 se admite sólo en lectura cuando pasa su validador estructural exacto y se marca `legacy_schema_read_compatible:19->20`. | Estado transversal; no produce ranking de contenido. |
| `catalog` | `document_catalog.sqlite3` | 6 | Publicación `published` por `source_kind`. | Membership de filtros y exact typed de path, nombre o identificador en la generación publicada. |
| `pdf` | `pdf.sqlite3` | 11 | Conteo, último `updated_ns` y run; no generacional. | FTS por página y fuentes semantic. |
| `docx` | `docx.sqlite3` | 5 | Conteo, último `updated_ns` y run; no generacional. | FTS documental y partes semantic. |
| `office` | `office.sqlite3` | 1 | Conteo, último `updated_ns` y run; no generacional. | FTS documental de XLSX/PPTX/ODT y semantic. |
| `audio` | `audio.sqlite3` | 1 | Conteo, último `updated_ns` y run; no generacional. | FTS de transcripción y segmentos semantic. |
| `image` | `image.sqlite3` | 5 | Conteo, último `updated_ns` y run; no generacional. | Imagen y OCR retenido mediante semantic cuando están publicados. |
| `semantic` | `semantic.sqlite3` | 6 | Head `ready` por firma de modelo. | Texto e imagen por espacio/modelo publicado, resueltos contra la revisión DB-local vigente. |
| `code` | `code.sqlite3` | 2 | Archivos actuales, última versión y último run; `best_effort_non_generational`. | FTS, exact typed, estructura, símbolos, relaciones owner-local y enlaces exactos a chunks Semantic publicados. |

Los watermarks no generacionales son detectores acotados de cambio, no una
publicación equivalente a inventario, catálogo o semantic. En particular,
`code` schema 2 todavía no tiene generación/head de grafo.

Cada head de inventario incluye, cuando existe un plan terminado, el token
`duplicate-plan-v1:<completed_ns>:<groups>:<redundant>:<bytes>`. Así se detecta
que `begin_duplicate_plan()` borre o reconstruya el plan bajo el mismo
`scan_id`; el token no convierte un plan rápido en duplicado exacto.

## Snapshot lógico y consistencia

No existe una transacción distribuida entre estos archivos SQLite.
`collect_knowledge_snapshot()` hace explícito el límite:

1. abre cada owner por URI `mode=ro` con timeout/busy timeout de 60 s,
   `foreign_keys=ON` y `query_only=ON`;
2. valida la versión y el schema exacto esperado; la única compatibilidad
   legacy explícita es framework 19→20, condicionada al contrato estructural
   exacto y sin migrar ni escribir;
3. observa publicaciones o watermarks dentro de una transacción de lectura;
4. repite la observación sobre la misma conexión;
5. compara el estado lógico y `PRAGMA data_version`;
6. reintenta el conjunto completo una sola vez si algún owner cambió;
7. cierra siempre la conexión.

Un schema mayor se marca `future`; uno legacy, sin versión canónica o
estructuralmente incompatible se marca `incompatible`; `SQLITE_CORRUPT` y
`SQLITE_NOTADB` se distinguen como `corrupt`. Los errores se reportan sin
reparación ni migración.

El snapshot conserva versión fuente, UTC, reloj monotónico, owners, schemas,
heads, watermarks, modelos activos, consistencia y warnings. Los modelos activos
proceden únicamente de heads semantic `ready` y registran firma, espacio
vectorial, modalidad, dimensiones y generación.

El `snapshot_id` usa una identidad XXH3 estable sobre versión fuente, identidad
lógica ordenada de owners, modelos activos y consistencia. Excluye timestamp,
reloj monotónico, warnings y `data_version`: dos capturas del mismo estado
lógico conservan ID; no es un hash criptográfico ni un backup.

`KnowledgeSearchService.search()` añade otra barrera alrededor de la consulta:
captura snapshot antes, ejecuta el plan, captura después y compara IDs. Ante el
primer cambio reintenta toda la recuperación una vez. Si vuelve a cambiar,
conserva los hits como resultado parcial y devuelve
`consistency=snapshot_changed` con los IDs antes/después y los owners cambiados.

`mode=ro` y `query_only` impiden escrituras SQL intencionales, pero no deben
describirse como apertura byte-neutra: según el journal y el estado del archivo,
SQLite puede participar en, crear o actualizar auxiliares `-wal`/`-shm`. Una
espera de lock tampoco ejecuta necesariamente el callback cooperativo antes de
que SQLite devuelva el control.

## Planificador determinista

`plan_knowledge_query()` no invoca un LLM. Normaliza una `KnowledgeQuery`,
reconoce señales sintácticas y produce un `KnowledgePlan` estable con un
`plan_id` XXH3, intents, pasos, límites y filtros.

Límites de la API:

| Campo | Predeterminado | Límite |
|---|---:|---:|
| texto | — | 1..4096 caracteres |
| resultados | 20 | 1..1000 |
| evidencias por recurso | 3 | 1..100 |
| distancia mínima de sección | 128 | 0..1 000 000 caracteres |
| presupuesto total de vectores semantic | 500 000 | 1..10 000 000 |
| valores por filtro | — | 64 |
| términos exactos | — | 64 |

El planner reconoce rutas Windows/UNC, POSIX y relativas seguras; nombres con
extensión; seriales con frontera explícita; símbolos cualificados; símbolos
bare sólo con contexto de código; huellas e identificadores. Antes de tocar un
owner, `classify_plan_exact_terms()` normaliza y deduplica cada término como
`path`, `name`, `identifier`, `serial`, `hash` o `symbol`. Sus pasos posibles
son:

- canal `exact`, ranking `exact_identifiers`: exact typed;
- canal `lexical`, ranking `owner_fts`: FTS de owners, siempre planificado y
  requerido cuando el scope admite una fuente lexical;
- canal `semantic`, ranking `semantic_text`: complemento semantic, requerido
  para scopes de imagen y para consultas sin un scope que lo excluya;
- `code_structural`: código, símbolo, definición, import, llamada o relación de
  código;
- `catalog_metadata`: source kind, formato, proyecto, identificador o fecha;
- `verified_relations`: intención relacional;
- `published_history`: intención temporal o histórica.

El lookup exact aplica `source_kinds` y `formats` en cada SQL antes de su
`ORDER BY`/top-K. Reporta por término y owner `complete`, `partial`,
`unsupported` o `unavailable`. Un serial se tipa de forma estable, pero Fase 1
lo devuelve `unsupported` porque ningún owner expone un campo serial
contractual. Inventory y code devuelven `partial` incluso cuando encuentran una
coincidencia: sus heads son mutables o best-effort no generacionales y no
demuestran una lectura as-of reproducible. Catálogo sí puede devolver
`complete` cuando la generación, la cobertura y el estado de la fuente son
demostrables.

La API acepta filtros `source_kinds`, `formats`, `project`, `date_from` y
`date_to`. Los filtros restringen candidatos; nunca se convierten en score. En
Fase 1 el catálogo no tiene una fecha documental fiable. Un filtro de fecha se
reporta como `catalog_content_date_filter_unsupported`, vacía los rankings
afectados y deja la búsqueda parcial; `mtime` o `birthtime` no se presentan como
fecha del contenido.

`code_structural` sí consume relaciones owner-local de código. En cambio,
`verified_relations` sigue representando un grafo transversal y
`published_history` un lector histórico uniforme; esos dos rankings no están
disponibles todavía. Solicitarlos produce un reporte explícito y una búsqueda
parcial, no una respuesta fabricada.

## Recuperación unificada y fusión

La frontera `execute_knowledge_search()` reutiliza los owners existentes:

| Ranking | Fuente real | Score preservado |
|---|---|---|
| `exact_inventory_*` | Checkpoints y filas publicadas de inventario. | Coincidencia exacta; cobertura `partial`. |
| `exact_code_*` | Estado actual de archivos, huellas y símbolos de code. | Coincidencia exacta; cobertura `partial`. |
| `exact_catalog_*` | Generaciones publicadas del catálogo. | Coincidencia exacta y generación fijada. |
| `fts_pdf`, `fts_docx`, `fts_office`, `fts_audio`, `fts_archive` | FTS5 de cada owner disponible; Archive conserva contenedor, miembro, cadena y profundidad. | BM25 del owner y posición original. |
| `semantic_text`, `semantic_image` | Servicio semantic v6 local; `semantic_text` materializa sólo contenido corporal. | Coseno, firmas de modelo consultor/indexado, espacio y generación. |
| `semantic_title` | Canal opcional sólo en planes `discovery` v3; basename durable, mutable y advisory. | Rango semántico con peso `0.5`; sólo refuerza evidencia corporal del mismo recurso y revisión. |
| `code_structural` | `search_code` sin reentrar a semantic. | RRF propio de código y evidencia estructural. |
| `catalog_metadata` | Generación de catálogo fijada por el snapshot. | Sin señal de relevancia: sólo membership/filtro y telemetría. |

`catalog_metadata` nunca entra a RRF: restringe membership por source, formato o
proyecto y luego se retira. Una coincidencia exacta de catálogo sólo puede
entrar como evidencia del adapter typed `exact_catalog_*`; la confianza del
clasificador no convierte un documento arbitrario del proyecto en respuesta.
Un `source_status` incompleto, `catalog_status` de review o incertidumbre alta
conserva la evidencia como revisión `partial`, warning observable y reporte
incompleto. Las filas catalogadas como error se excluyen del membership regular;
si un lookup exact de path las observa, también quedan explícitamente partial.

Los espacios y scores crudos permanecen separados. No se compara BM25 con
coseno, confianza de catálogo o score de código como si fueran probabilidades
calibradas. La fusión usa Reciprocal Rank Fusion con `k=60`:

```text
contribución = 1 / (60 + posición_en_ranking)
```

Los rankings de relevancia usan ventanas acotadas, normalmente hasta tres veces
el límite solicitado y nunca más de 1000 candidatos; los adapters exactos
añaden sus propios límites de filas y pasos SQLite. La clave de fusión es
`(resource_id, revision_id, evidence_id)`, no sólo el recurso. El hit final
conserva todas las señales, posiciones, contribuciones, razones y warnings.

Después de recuperar evidencia suficiente se aplican:

1. exclusión de recursos marcados `duplicate`;
2. exclusión predeterminada de revisiones `historical` o `superseded`;
3. límite global;
4. límite por recurso;
5. deduplicación de la misma sección y de intervalos solapados o demasiado
   próximos.

`include_history=True` permite candidatos históricos que un owner realmente
entregue; no crea un lector uniforme de historia ni incluye duplicados. Los
adapters actuales consultan principalmente estado vigente/publicado, por lo que
`--knowledge-history` es permiso de inclusión, no garantía de cobertura
histórica.

### Vigencia y ejecución semantic

Al resolver un hit publicado, semantic conserva `published_revision_id` y
calcula `current_revision_id` contra el item DB-local activo, su identidad, su
huella de contenido y su revisión fuente. Una discrepancia se convierte en
`RevisionState.HISTORICAL`, warning `stale_revision` y exclusión predeterminada;
`include_history=True` sólo permite conservarla. Un `source_status` parcial se
propaga como revisión `partial`. `RankingSignal` distingue además
`query_model_signature` de la firma del modelo que indexó el vector, algo
necesario para consulta texto→imagen.

Knowledge usa exclusivamente cache local y `local_files_only=True`: no descarga
modelos ni crea un cache ausente. Si el modelo local falta, está incompleto o no
puede cargarse, ese ranking semantic queda indisponible/incompleto y los
rankings lexicales independientes se conservan. En la frontera Knowledge,
`max_vectors` es un presupuesto total: cuando se lanzan texto e imagen se
reparte entre ambos y `vectors_scanned` agregado no puede consumir dos veces el
valor del plan. El callback cooperativo atraviesa preparación/vectorización,
escaneo exacto de vectores, resolución por lotes y lexical semantic; una llamada
nativa o espera SQLite sólo puede observarlo en el siguiente checkpoint.

El resultado informa por ranking `executed`, `available`, `complete`, hits,
filas, vectores y razón de indisponibilidad. También informa truncamiento,
candidatos omitidos, tiempo y completitud global. `rows_scanned` no pretende ser
el número físico de filas examinadas por el motor: cada ranking publica
`row_count_semantics=materialized_lower_bound` y el resultado agregado
`sum_of_materialized_lower_bounds`. Código cuenta hits materializados por su API
y exact cuenta filas observadas bajo su ventana; por tanto estos valores —y los
omitidos sólo observables dentro de ventanas acotadas— son cotas inferiores, no
prueba de cobertura completa ni un benchmark de I/O.

### Telemetría de consulta por fase

Las búsquedas y los contextos producidos por `KnowledgeSearchService` pueden
incluir el envelope aditivo `KnowledgeQueryTelemetry` schema 1. Su unidad
canónica es `duration_ns` y su reloj está declarado por
`clock_signature=python-perf-counter-ns-v1`. La telemetría es observacional:
queda fuera de `plan_id`, `snapshot_id`, identidades de recursos/evidencias,
igualdad semántica de los bundles, citas, `ContextBudget` y
`rendered_context`.

Las fases se conservan en orden de ejecución:

- `planner`, una vez por operación;
- `snapshot_before` y `snapshot_after`, con `snapshot_id` y
  `service_attempt=1|2`;
- `owner_ranking`, con owner y uno o más nombres de ranking;
- `fusion` y `broker`, por intento;
- `context_compile`, una vez y sólo en una operación de contexto.

FTS publica una medición independiente para `pdf`, `docx`, `office` y `audio`.
Semantic separa texto e imagen. Exact mide un batch por owner
(`inventory`, `code` o `catalog`) y enumera los rankings por término cubiertos;
no copia el mismo tiempo a cada término ni a `exact_coverage`. Código,
catálogo e inventario publican respectivamente `code_structural`,
`catalog_metadata` e `inventory_duplicate_plan`. Un owner consultado sólo para
comprobar indisponibilidad conserva `executed=false` en vez de inventar trabajo
de retrieval.

`total_duration_ns` se mide directamente de extremo a extremo. Las fases están
anidadas —por ejemplo, `broker` contiene owners y fusión— y **no deben sumarse**
para reconstruir el total. Si el snapshot cambia, se retienen las fases de los
dos intentos aunque sólo los hits del último sean retornados. El campo legacy
`elapsed_milliseconds` permanece compatible y sigue siendo el tiempo del broker
del intento retornado, truncado a milisegundos; no representa planner,
snapshots, retry ni compilación de contexto.

`build_context_bundle()` continúa puro y no lee el reloj. El servicio mide el
builder alrededor de la llamada y adjunta la telemetría fuera del texto
renderizado. Comparar rendimiento requiere además el mismo fixture,
`snapshot_id` y condiciones de cache registradas por la campaña; el envelope no
puede inferir ni certificar por sí solo el estado de cache del sistema operativo
o de modelos.

### Discovery frente a evidence

La búsqueda vectorial histórica conserva por defecto el mejor hit por
`item_id`; ese contrato de discovery sigue siendo compatible. La ruta nueva de
evidencia conserva el mejor hit por `(item_id, entity_id)`, de modo que varias
páginas, segmentos o chunks del mismo recurso puedan llegar a la fusión.

| Modo | Semántica |
|---|---|
| `discovery` | Un mejor hit semantic por recurso/item; útil para explorar recursos distintos. |
| `evidence` | Varias entidades concretas por recurso, sujetas después a diversidad y límites. |

Knowledge usa `evidence` por defecto. Las APIs semantic preexistentes conservan
`discovery` como default para no romper consumidores.

## Localizadores y citas por formato

La precisión disponible depende del owner:

| Fuente | Localizador realmente emitido en Fase 1 |
|---|---|
| Inventario | Ruta actual exacta dentro del scan publicado. |
| PDF FTS/semantic | Número de página del owner; el chunk semantic puede añadir intervalo de caracteres. |
| DOCX FTS | Documento completo (`fulltext`), sin página. |
| DOCX semantic | Parte OOXML o cuerpo y, cuando existe, intervalo de caracteres; no página ficticia. |
| Office FTS/semantic | Documento/cuerpo completo. No se afirma hoja o celda si el adapter no la conserva. |
| Audio FTS | Transcripción completa, sin timestamp. |
| Audio semantic | Segmento con `start_ms` y `end_ms`. |
| Imagen | Imagen u OCR retenido mediante semantic; bbox sólo si una evidencia real lo aporta. |
| Código | Versión vigente, líneas, símbolo y tipo de coincidencia; los chunks semantic conservan índice, clase y líneas. |
| Catálogo | Clasificación documental inferida, generación, identificadores y confianza; no página. |

La corrección del puente semantic→código valida que `section_id` sea un entero
decimal canónico dentro del rango SQLite, exige `version_id`, identidad física,
versión actual y `chunk kind` exactos, y recupera ese `code_chunk`. Un ID
inválido, chunk ausente o revisión cambiada se omite; nunca cae silenciosamente
al primer chunk del archivo.

La ruta Code directa añade el contrato persistente inverso: después de una
publicación textual completa, `code.embedding_links` fija item, modelo, espacio
y generación por chunk. Un hit de `search_code` sólo cruza el puente cuando esa
fila está activa y coincide exactamente con el head Semantic y la versión Code
vigente. Knowledge conserva su ranking `semantic_text` como owner Semantic y su
ranking `code_structural` sin reentrar a Semantic; los enlaces permiten que la
superficie Code híbrida sea útil sin confundir ni duplicar esos propietarios.
Los scores siguen sin calibrar y sólo transportan evidencia de recuperación.

### Relaciones reales de código

`search_code` entrega `CodeSearchRelation` para referencias y dependencias.
Knowledge materializa cada relación como una `EvidenceRef` separada con
`section_kind=code_relation`, identidad estable de fila owner, familia, tipo,
nombre, endpoints, confianza, confirmación, scope, versión y procedencia. El
source siempre debe resolver a una versión actual. El target sólo se publica
como `resource_id` cuando también resuelve contra metadata vigente; una relación
unresolved o cuyo target cambió conserva su hint y warnings, pero nunca fabrica
un endpoint.

El compilador de contexto crea una arista owner-local únicamente para relaciones
con ambos endpoints reales. Una relación confirmada usa método `structural`; una
resuelta pero no confirmada queda `inferred` y hace parcial el ranking. La
relación planeada de inventario `planned_duplicate_of` se conserva aparte con
método `ambiguous`: no equivale a duplicación exacta.

## Compilador de contexto

`build_context_bundle()` es una función pura: recibe un
`KnowledgeSearchResult` inmutable y no abre bases, ejecuta retrieval ni invoca
modelos. Ordena y deduplica hits, selecciona evidencias y asigna citas estables
`K1`, `K2`, ... El envelope incluye un `ContextPlanRef` autocontenido con query,
modo, intents, exact terms, filtros, historia, límites, notices y todos los
`ContextPlanStepRef`. Cuando cabe el encabezado normal, el mismo plan se incluye
como `plan=...` dentro de `rendered_context`; con un presupuesto diminuto se
usa el fallback visible y el plan estructurado permanece en el envelope. Cada
bloque de evidencia contiene:

- target con `resource_id`, `revision_id`, `evidence_id` y localizador;
- razón de inclusión y ranking de recuperación;
- snippet normalizado o una ausencia explícita.

El presupuesto es estricto sobre codepoints Unicode:

- 12 000 caracteres y 12 hits como defaults de la API del builder;
- máximo 1 000 000 de caracteres y 100 hits;
- hasta 2000 hits de entrada antes del límite interno;
- estimación `ceil(caracteres / 4)` con firma
  `unicode-codepoints-ceil-div4-v1`;
- truncamiento visible `…[truncated]`;
- citas y localizadores nunca se cortan silenciosamente.

Si ni siquiera el encabezado y el estado caben, se emite un fallback visible
dentro del límite. `ContextBudget` registra caracteres usados, tokens estimados,
candidatos omitidos e IDs de evidencias truncadas.

El grafo se deriva sólo de identificadores estructurados de las evidencias que
sí fueron seleccionadas. `ContextEntityRef` enlaza sus recursos y evidencias;
`ContextRelationRef` exige dos entidades existentes y evidencia común que
fundamente ambos endpoints, además de método, procedencia y confianza opcional.
Todas las entidades y
relaciones incluidas aparecen literalmente en las secciones `ENTITIES` y
`RELATIONS` de `rendered_context`, dentro del mismo límite de caracteres: no hay
un grafo oculto fuera del prompt. El builder selecciona evidencia y grafo de
forma atómica; si no caben, omite esa evidencia completa y lo contabiliza en
`ContextBudget`.

`ContextGraphBudget` usa el scope `selected_evidence_graph` y registra
identificadores considerados, entidades/relaciones incluidas y contadores de
omisión. Si un productor futuro usa esos contadores de omisión, el contrato
exige completitud `partial` y un aviso de grafo visible en el texto renderizado.

Las contradicciones no se infieren de texto libre. Sólo se declaran cuando al
menos dos evidencias seleccionadas contienen identificadores estructurados
`claim:<topic>` con valores distintos. `ContextContradictionRef` ordena los
valores canónicamente, conserva dos o más citas existentes y calcula un
`contradiction_id` XXH3 estable sobre tipo, topic y valores; tanto su resumen
como sus citas quedan dentro de `rendered_context`.

La completitud distingue:

- `complete`: evidencia seleccionada sin ranking incompleto ni recorte;
- `no_evidence`: búsqueda completa, snapshot estable y cero coincidencias;
- `unsupported`: ningún owner/ranking pudo ejecutar la consulta;
- `partial`: ranking requerido o capacidad explícitamente planificada no
  disponible, límite alcanzado, snapshot cambiado, snippet omitido/truncado,
  contradicción u otra evidencia faltante posible.

NeoCortex entrega este contexto verificable; el consumidor externo decide cómo
usarlo y debe conservar las citas.

## CLI instalada

La interfaz sigue siendo option-based. No existen subcomandos `knowledge`.

```powershell
Neocortex --knowledge-status
Neocortex --knowledge-status --knowledge-json
Neocortex --knowledge-search "mantenimiento de transformadores" --knowledge-limit 20
Neocortex --knowledge-search "protección diferencial" --knowledge-mode discovery --knowledge-json
Neocortex --knowledge-context "¿Qué evidencia existe de la prueba FAT?" --knowledge-limit 12 --knowledge-context-characters 24000
Neocortex --knowledge-context "versión anterior del procedimiento" --knowledge-history --knowledge-json
```

| Opción | Contrato |
|---|---|
| `--knowledge-status` | Snapshot lógico sin consulta de contenido. |
| `--knowledge-search QUERY` | Plan y hits unificados. |
| `--knowledge-context QUERY` | Búsqueda seguida de ContextBundle. |
| `--knowledge-json` | JSON canónico del contrato correspondiente. |
| `--knowledge-limit N` | Límite de resultados: 1..1000 en search y 1..100 en context. |
| `--knowledge-context-characters N` | Presupuesto de ContextBundle: default 12000, rango 1..1000000; requiere context. |
| `--knowledge-mode discovery|evidence` | Default `evidence`. |
| `--knowledge-history` | Permite revisiones históricas entregadas por owners. |
| `--state-directory PATH` | Selecciona el conjunto de bases existente. |

Las tres acciones son mutuamente excluyentes. Las opciones auxiliares requieren
una acción Knowledge; `limit`, `mode` e `history` requieren search o context.
Knowledge rechaza `--apply`, `--route` y otras operaciones directas.

Una consulta vacía se rechaza; el máximo es 4096 caracteres. Search admite
`--knowledge-limit 1..1000`; context rechaza en validación CLI valores mayores
de 100 antes de ejecutar el handler. `--knowledge-context-characters` expone el
presupuesto de caracteres de context con el mismo rango `1..1000000` que la API
Python y se valida antes del handler.

### Códigos de salida

| Código | Significado Knowledge |
|---:|---|
| `0` | Éxito; status también usa 0 cuando todos los owners están ausentes. |
| `1` | Fallo fatal normalizado, incluido `KnowledgeStateRootError`. |
| `2` | Error de uso/argumentos. |
| `3` | Búsqueda completa sin resultados o ContextBundle `no_evidence`. |
| `4` | Resultado parcial o capacidad solicitada no soportada. |
| `5` | Snapshot cambió después del único reintento. |
| `6` | `status`: algún schema futuro/incompatible. `search/context`: uno de esos owners bloqueó realmente la consulta. |
| `7` | `status`: alguna base corrupta/no SQLite. `search/context`: esa base bloqueó realmente la consulta. |
| `130` | Cancelación cooperativa o teclado. |

La precedencia es corrupción, schema incompatible, snapshot cambiado, parcial
y finalmente sin resultados. Para `search/context`, corrupción e
incompatibilidad se aplican sólo a `blocking_owners`; `status` conserva la vista
global. Una consulta sobre un directorio de estado ausente devuelve parcial
(`4`), no un falso “sin resultados”, y no crea el directorio.
Una ruta existente no-directorio o inaccesible devuelve `1` y no produce un
snapshot de owners `absent`.

## API Python estable

La fachada lazy `_04_Nucleo_Operativo` exporta `ContextBundle`, `EvidenceRef`,
`KnowledgeHit`, `KnowledgePlan`, `KnowledgeQuery`, `KnowledgeSearchResult`,
`KnowledgeSearchService`, `KnowledgeSnapshot`, `KnowledgeStatePaths`,
`KnowledgeStateRootError`, `ResourceRef`, `RetrievalMode`, `RevisionRef` y
`plan_knowledge_query`.

```python
from pathlib import Path

from _04_Nucleo_Operativo import (
    KnowledgeQuery,
    KnowledgeSearchService,
    KnowledgeStatePaths,
    RetrievalMode,
)

paths = KnowledgeStatePaths.from_directory(
    Path.home() / "AppData" / "Local" / "Neocortex" / "state"
)
service = KnowledgeSearchService(paths)

snapshot = service.status()
result = service.search(
    KnowledgeQuery(
        "protección de transformador",
        retrieval_mode=RetrievalMode.EVIDENCE,
        source_kinds=("pdf", "docx"),
        limit=20,
        max_per_resource=3,
        max_vectors=500_000,
    )
)
bundle = service.context(
    KnowledgeQuery("evidencia de mantenimiento"),
    max_characters=12_000,
    max_hits=12,
)

print(snapshot.to_json())
print(result.to_json())
print(bundle.to_json())
```

`status`, `search` y `context` aceptan un `cancellation_check` cooperativo. El
servicio propaga el mismo callback a la captura del snapshot, que lo observa
entre owners, entre las dos observaciones de cada owner y antes de reintentar
un vector global inestable; cualquier excepción del callback conserva su tipo
e identidad. Cada transacción read-only del snapshot instala temporalmente un
progress handler que observa el callback durante consultas SQLite largas y se
retira siempre al salir de ese scope; ante una interrupción se retira antes del
rollback y del cierre. `stat`/enumeración y la espera de un lock SQLite no
ejecutan necesariamente ese handler: en esos tramos la cancelación se observa
cuando la llamada devuelve el control, con el `busy_timeout` de 60 segundos
como límite actual para la espera SQLite. La API no acepta SQL arbitrario. Los
filtros avanzados de formato, proyecto y fecha pertenecen a `KnowledgeQuery`;
la CLI de Fase 1 sólo expone texto, modo, historia, límites de hits y
presupuesto de caracteres del contexto.

## Evaluación reproducible

La evaluación está separada del servicio productivo:

- fixture versionado:
  `tests/fixtures/knowledge/phase1_golden_v1.json`;
- cargador/métricas:
  `_04_Nucleo_Operativo/knowledge_evaluation.py`;
- regresiones: `tests/test_knowledge_evaluation.py`.

El fixture v1 contiene exactamente 17 escenarios:

1. `exact_identifier`;
2. `lexical`;
3. `semantic_paraphrase`;
4. `relevant_hit_chunk_2_of_3`;
5. `multiple_evidence_same_resource`;
6. `two_sources_formats_same_answer`;
7. `code_and_documentation`;
8. `current_vs_superseded`;
9. `exact_duplicate`;
10. `contradiction`;
11. `available_multihop` con dos relaciones de código;
12. `no_answer`;
13. `incomplete_by_limit`;
14. `snapshot_changes` durante dos intentos;
15. `absent_owner_base`;
16. `future_schema`;
17. `unicode_spaces_hash_path`, cuya ruta exacta queda honestamente partial.

El JSON es input-only: contiene expectativas y candidatos owner scripted
acotados, pero no `actual_*`, evidencias recuperadas, citas producidas,
telemetría ni el booleano derivado `scripted_fixture`. El runner ejecuta para
los 17 casos el planner, la fusión y el compilador de contexto live; el caso de
snapshot cambiante cruza además el reintento real de
`KnowledgeSearchService` mediante seams read-only inyectados.

Calcula `recall@k`, MRR, nDCG, cobertura de evidencia, precisión de citas, tasas
stale/duplicado, abstención esperada/real, exactitud de outcome, latencia,
filas, vectores y tamaño de contexto. Los numeradores y denominadores stale y
duplicate son explícitos; nDCG se acota a `[0,1]` y los conjuntos sin relevancia
tienen semántica cero/`None` declarada. El schema del fixture es estricto,
limita el archivo a 4 MiB y exige cobertura de todas las categorías.

Ejecución focal:

```powershell
py -3 -m pytest -q tests/test_knowledge_evaluation.py
```

Las pruebas funcionales complementarias cubren contratos/Unicode, espacios y
`#` en rutas, planner, FTS, múltiples evidencias, presupuesto, snapshots,
servicio, CLI y la regresión de chunk semantic de código.

El fixture es scripted: valida contratos, orquestación, contabilidad y fórmulas
de métricas. La telemetría no está precalculada en el JSON: el runner deriva
filas de los candidatos que materializó, vectores de los rankings semantic,
caracteres del `rendered_context` realmente construido y latencia del reloj de
la ejecución. Esas observaciones describen el harness scripted, no el costo de
SQLite, modelos o corpus de producción, y no demuestran calidad de embeddings o
clasificadores sobre un corpus representativo. Esa evaluación requiere datos
etiquetados autorizados y una línea base comparable. Por tanto, el golden
sintético no es una medición de calidad del corpus real; la evaluación real se
mantiene como una campaña separada, con estado, ground truth y métricas propios.

## Alcance probado, límites y rollback

Las regresiones automatizadas del árbol ejercitan el tipado exacto y sus
estados, vigencia semantic, fallback de cache local, cancelación, membership de
catálogo sin rank, relaciones reales de código, grounding del grafo, IDs
estables de contradicción, presupuesto de contexto y los 17 casos del golden.
Esto demuestra esos contratos sobre fixtures acotados; no demuestra recall ni
latencia sobre el corpus vivo.

Limitaciones abiertas:

- Semantic es opcional. Sólo usa modelos locales ya preparados y heads
  publicados; si faltan, el ranking se declara indisponible. La búsqueda es
  exacta, comparte un presupuesto total `max_vectors` y no tiene ANN. Knowledge
  `discovery` ya transporta `semantic_title` sin convertirlo en evidencia. El
  contrato Jina/body exacto aplica pisos iniciales separados para PDF (`0.50`) y
  Code (`0.46`) y reporta las exclusiones como abstención calibrada; títulos,
  otros owners, modelos o backends no heredan esos cortes. Los vectores
  reutilizados conservan el contrato en `payload_provenance`; un conflicto de
  procedencia se mantiene explícitamente sin calibrar.
  En el piloto combinado de 35 PDF y 30 archivos Code, 12 consultas/18 targets
  obtuvieron `5/18` con FTS, `16/18` con cuerpo+FTS y `17/18` con
  `discovery`+título; esta última variante logró 12/12 Hit@5, MRR `0.9167` y
  cero títulos como evidencia. Tres consultas fuera de dominio sí se
  abstuvieron con FTS; la campaña posterior añadió positivos, fuera de dominio y
  negativos cercanos para fijar esos pisos sin perder los targets etiquetados.
  Algunos negativos técnicamente cercanos permanecen por encima del piso, por
  lo que el score sigue siendo similitud de recuperación, no probabilidad ni
  certeza. El cuerpo continúa siendo la única evidencia citable y clasificable.
- Los seriales exactos son `unsupported`. Las coincidencias exactas de Inventory
  y code se reportan `partial` porque no ofrecen una publicación as-of
  inmutable.
- Catálogo no soporta fecha documental y no sustituye fecha por `mtime`.
- Historia no es uniforme entre owners; `include_history` sólo permite lo que
  un owner ya entregó.
- Las relaciones owner-local de código son productivas, pero el grafo
  transversal y la temporalidad uniforme sólo aparecen como planes no
  disponibles. No existe un knowledge graph transversal generacional.
- Inventory v9 no persiste `verification_mode`: una relación
  `planned_duplicate_of` no prueba duplicación exacta. El caso golden
  `exact_duplicate` prueba la política de fusión con una disposición scripted,
  no que el owner productivo pueda inferirla.
- Read-only significa ausencia de SQL mutador intencional, no byte-neutralidad:
  SQLite puede coordinar auxiliares WAL/SHM y una espera de lock puede diferir
  la cancelación hasta 60 s; `stat` o enumeración UNC también pueden bloquear
  hasta que Windows devuelva el control.
- No existe servidor MCP ni `QueryObservation` durable en Fase 1.
- No existe feedback writer desde las operaciones read-only.
- Scores semantic, catálogo y heurísticas no son verdad ni probabilidades
  calibradas.
- La ruta actual puede quedar obsoleta después del snapshot; antes de cualquier
  operación física deben aplicarse de nuevo las garantías identity-bound.
- La cobertura de citas depende de la precisión realmente retenida por cada
  owner; hoja/celda y bbox permanecen ausentes cuando no existen.

La Fase 1 no cambia schemas ni datos. Su rollback consiste en volver a instalar
el paquete anterior compatible; no se debe editar `schema_version`, restaurar
una base ni ejecutar una migración inversa por este cambio. Los owners y el
corpus permanecen intactos.

Consulte también [Arquitectura](ARCHITECTURE.md), [CLI](CLI.md),
[Persistencia](PERSISTENCE.md), [Seguridad](SECURITY.md) y el
[README del núcleo](../_04_Nucleo_Operativo/README.md).
