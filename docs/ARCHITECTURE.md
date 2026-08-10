# Arquitectura de NeoCortex

> **Estado del documento.** Contrato derivado del árbol inspeccionado el
> 9 de agosto de 2026. Describe el comportamiento observado y separa los
> cambios previstos de los ya implementados. No certifica por sí solo la suite
> completa ni la instalación empaquetada. El árbol auditado declara la versión
> `0.9.0`; la versión instalada debe comprobarse con
> `Neocortex --version`.

## Finalidad y principios

NeoCortex es un framework local para Windows y Linux que permite descubrir, identificar, extraer,
indexar, relacionar, clasificar, revisar y buscar contenido personal de forma
incremental. Sus rutas actuales cubren PDF, DOCX, otros documentos Office,
ZIP anidados, texto físico/correo/Office heredado, audio, video, imágenes y
código.

La arquitectura persigue estos invariantes:

- los archivos originales permanecen intactos salvo autorización explícita;
- el estado derivado conserva identidad, evidencia, incertidumbre y
  procedencia;
- una interrupción no debe convertir trabajo parcial en verdad publicada;
- los lotes, colas, procesos y consumo de recursos deben permanecer acotados;
- las superficies CLI, GUI y Python deben converger en los mismos contratos;
- la evidencia probabilística o semántica nunca autoriza por sí sola una
  eliminación, movimiento o rename.

Los riesgos todavía abiertos se enumeran al final. La publicación generacional
de inventario, catálogo y semántica existe en la fuente `0.7.0`, pero sólo sus
lectores oficiales aplican el contrato; SQL externo sobre tablas legacy puede
eludirlo.

## Fuentes de verdad

| Tema | Fuente primaria |
|---|---|
| Entry point instalado | `[project.scripts]` de `pyproject.toml` y `neocortex.cli` |
| Parser y validación CLI | `cli_parser.py` y `cli_validation.py`, con superficies extraídas `cli_{audio,code,semantic,knowledge}_surface.py` |
| Configuración efectiva de una corrida | `models.py`; fachada plana compatible `application_config.py`, proyecciones en `application_config_projections.py` y construcción CLI en `cli_config.py` |
| Plataforma y rutas canónicas por usuario | `neocortex/platform_policy.py` y `app_paths.py` |
| Protección de rutas propias | `internal_paths.py`, `inventory_boundary.py` e `incremental_gate.py` |
| Orden y adaptadores de rutas | `route_selection.py` y `route_registry.py` |
| Coordinación de corridas | `orchestrator.py` |
| Knowledge Plane read-only | `knowledge_contracts.py`, `knowledge_snapshot.py`, `knowledge_planner.py`, `knowledge_search.py`, `knowledge_context.py` y `knowledge_service.py` |
| Planner semántico read-only | `semantic_planner.py` y contratos en `semantic_service_contracts.py` |
| SDK, consulta y capacidades públicas | `neocortex/sdk`, `neocortex/read_api.py`, `neocortex/human_cli.py`, `neocortex/agent_server.py`, `neocortex/capabilities.py` y markers `py.typed` |
| Apertura SQLite compartida | `neocortex/sqlite_connection.py`; su adopción actual no es universal |
| Esquemas persistentes | módulos `*_schema.py` y propietarios `*_state.py`/repositorios |
| Estado operacional | Windows: `%LOCALAPPDATA%\Neocortex\state`; Linux: `${XDG_STATE_HOME:-~/.local/state}/Neocortex/state` |
| Comportamiento comprobable | código ejecutado y pruebas; la documentación no lo sustituye |

La topología Windows derivada de la política central y `app_paths.py` separa
los árboles propios:

```text
Fuente:       %USERPROFILE%\Neocortex\Repository
Runtime:      %LOCALAPPDATA%\Programs\Neocortex\versions\<runtime-id>\venv
Launcher:     %LOCALAPPDATA%\Programs\Neocortex\bin\Neocortex.exe
Estado:       %LOCALAPPDATA%\Neocortex\state
Autoanálisis: %LOCALAPPDATA%\Neocortex\self-analysis
```

Los runtimes son versionados; `bin\Neocortex.exe` es la única ruta estable de
promoción y se valida contra el artefacto exacto antes de incorporarla al
`PATH`.

En Linux, estado, configuración y datos respetan XDG. Las releases inmutables
viven en `~/.local/share/Neocortex/releases`, `current` selecciona la activa,
los modelos compartidos viven en `~/.local/share/Neocortex/models`, el launcher
estable es `~/.local/share/Neocortex/bin/Neocortex` y el alias público es
`~/.local/bin/Neocortex`.

La guía de bases, migraciones, backup y retención es
[PERSISTENCE.md](PERSISTENCE.md). La operación diaria se explica en
[OPERATIONS.md](OPERATIONS.md), la recuperación en
[RECOVERY.md](RECOVERY.md) y los límites de seguridad en
[SECURITY.md](SECURITY.md). El perfil code-only protegido se especifica en
[SELF_ANALYSIS.md](SELF_ANALYSIS.md) y los contratos y límites del plano de
conocimiento se documentan en [KNOWLEDGE.md](KNOWLEDGE.md).

## Vista de alto nivel

```text
                   Neocortex / python -m neocortex
                                  │
                   neocortex.cli:entrypoint
                     ┌────────────┼────────────┐
                     │            │            │
                  CLI normal     --ui     --gui-worker
                     │            │            │
            cli_app / cli_direct  │       worker supervisado
                     │            └──── QProcess ─────┘
                     │
          FrameworkOrchestrator / operaciones directas
                     │
          framework.lock + estado común de corrida
                     │
       ┌─────────────┴─────────────────────────────┐
       │                                           │
 enumeración portable + USN opcional        rutas de contenido
       │                       ┌────┬────┬────┬────┬────┬────┬─────┬─────┬────┐
 inventario y deduplicación    PDF DOCX Office ZIP Text Audio Video Image Code
       │                       └────┴────┴────┴────┴────┴────┴─────┴─────┴────┘
 checkpoint durable                          │
       │                     catálogo documental / revisión / semántica
       └───────────────────────────────┬─────┘
                                      │
                              SQLite por propietario
```

La separación física entre bases reduce contención y permite que cada ruta sea
propietaria de su contrato. No constituye una transacción distribuida: la
coherencia entre bases depende del orden del orquestador, identificadores de
run, checkpoints y reglas de publicación de cada subsistema.

## Knowledge Plane de sólo lectura

La Fase 1 agrega una frontera de recuperación común sobre las bases que ya
poseen inventario, extractores, FTS, catálogo, semantic y código. No agrega una
base `knowledge.sqlite3`, DDL ni migraciones, y sus operaciones no indexan ni
modifican el corpus. Los contratos inmutables `ResourceRef`, `RevisionRef`,
`EvidenceRef`, `KnowledgeHit`, `KnowledgeSnapshot` y `ContextBundle` conservan
identidad, revisión, localizador, procedencia, score de origen y completitud.

`collect_knowledge_snapshot()` abre únicamente owners existentes en modo de
lectura, observa sus publicaciones o watermarks dos veces y reintenta el
conjunto una vez si detecta cambios. Ese snapshot es una vista lógica, no una
transacción distribuida. `KnowledgeSearchService` vuelve a capturar el estado
antes y después de cada consulta; ante un primer cambio repite la recuperación
completa una vez y, ante otro, devuelve evidencia parcial con el cambio
explícito en vez de fingir atomicidad.

El planner determinista selecciona rankings independientes de identidad/ruta
exacta, FTS por owner, semantic publicado, código estructural y metadatos de
catálogo. La fusión RRF mantiene scores y modelos en sus espacios originales y
opera por evidencia concreta, no sólo por archivo. El modo `evidence` permite
varias páginas, segmentos o chunks de un recurso; `discovery` conserva la
semántica compatible de un mejor hit semantic por recurso. El compilador de
contexto aplica un presupuesto duro, citas estables, contradicciones
estructuradas y estados de ausencia o abstención.

La API Python, los comandos `--knowledge-*`, los aliases humanos
`status/search/ask` y MCP/stdio consumen esa misma frontera. `read_api` sólo
resuelve los scopes fijos `personal` y `framework`; `all` conserva rankings
independientes. `agent_server` registra únicamente tools read-only y no abre
red. Un grafo transversal entre owners permanece para una fase posterior. La
especificación completa está en
[KNOWLEDGE.md](KNOWLEDGE.md).

La telemetría Knowledge schema 1 ya se especifica allí: usa nanosegundos y
conserva intentos, snapshots, owners, rankings, fusión, broker y contexto sin
alterar los contratos sin telemetría. No constituye una publicación ni un owner
persistente adicional.

## Planificador semántico read-only

`plan_semantic_index()` y `--semantic-plan {text,image,all}` calculan un
preflight determinista sobre estado durable existente. No adquieren modelos,
no crean jobs y no modifican las bases propietarias. Usan un SQLite scratch
privado, con cuota dura predeterminada de 512 MiB, para deduplicar y proyectar
contenido con memoria acotada; exceso, cancelación o bloqueo persistente fallan
cerrado.

Cada base física se abre en su propia transacción de lectura y se protege con
versión de esquema, fingerprint XXH3 del snapshot y un fence `data_version`
antes/después. Los logical owners Office que comparten archivo se leen dentro
de una sola transacción física. Esto evita mezclar vistas de una misma base,
pero no crea atomicidad cross-database; el contrato lo declara explícitamente.

Cada workload conserva modalidad, rol, modelo, versión, proveedor, espacio
vectorial, dimensiones, dtype, normalización, distancia, procedencia y firma de
procesamiento. El planner calcula reutilización preexistente y entre workloads,
bytes vectoriales como cota inferior y solicitudes al modelo como rango entre
contenido único nuevo y entidades aún no reutilizadas. El rango de segundos de
modelo sólo aparece con una calibración exacta de ejecución/procesamiento/
workload/modelo/rol; sin ella queda desconocido en vez de extrapolar una tasa.

Imagen y `all` se planifican sólo desde caché. No se abren originales, por lo
que `originals_verified=false`, `execution_ready=null` y `complete=false` son
resultados deliberados, no una inferencia de disponibilidad. El JSON estable y
la salida humana exponen esos límites junto con la cuota y los hashes de
snapshot.

## Paquetes y responsabilidades

### `neocortex`

Paquete de instalación mínimo:

- declara la versión pública en `neocortex.__version__`;
- expone `neocortex.cli:entrypoint`;
- soporta `python -m neocortex`;
- ofrece la fachada read-only de scopes, CLI humana y MCP/stdio;
- contiene utilidades compartidas de ciclo de vida y contrato SQLite.

No implementa el pipeline completo. Su función es ofrecer una frontera estable
y evitar imports pesados durante ayuda, versión o selección de modo.

### `_01_Enumeracion`

Frontera de enumeración por plataforma:

- Windows conserva enumeración MFT y lectura/resolución de registros USN;
- Linux ejecuta un recorrido completo portable, case-sensitive y sin seguir
  enlaces simbólicos;
- los snapshots conservan identidad durable y metadatos: FileId/volumen en
  Windows, `st_dev`/`st_ino` en Linux;
- Linux persiste `birthtime_ns=-1` si el filesystem no expone nacimiento real;
  nunca usa `ctime` como sustituto;
- el índice SQLite auxiliar de rutas usa la collation de cada plataforma.

Produce observaciones; no decide eliminación ni clasificación. El
`SqlitePathIndex` es una API auxiliar soportada y probada, pero no se confirmó
un consumidor dentro de la corrida integrada actual.

### `_02_Deduplicacion`

Propietario del inventario común:

- scans e inventarios;
- publicaciones por raíz con cursor USN opcional;
- fingerprints no criptográficos de contenido propio;
- grupos y planes de duplicados;
- comparación exacta inmediatamente antes de una mutación autorizada.

El esquema fuente actual es v9. Conserva la clave `(scan_id, path)` y los scans
`building`, `complete` y `partial` introducidos por v7, liga cada scan nuevo a
su `inventory_policy_signature` y publica un checkpoint que referencia una
generación completa. El cursor USN del checkpoint es opcional y sólo acelera
la siguiente enumeración; la publicación portable sigue siendo consumible por
Knowledge y Semantic. Las migraciones históricas v6→v7, v7→v8 y v8→v9 tienen
regresiones específicas; los diagnósticos no migran una base existente y la
actualización debe seguir el procedimiento respaldado de
[PERSISTENCE.md](PERSISTENCE.md).

### `_03_Progreso`

Contratos de eventos y reporteros. Separa el progreso del motor de la
representación Rich, texto o protocolo de GUI. Las rutas no deben depender de
widgets ni escribir directamente a una terminal para informar avance.

### `_04_Nucleo_Operativo`

Núcleo de aplicación. Contiene:

- configuración, parser, validación y reporte CLI;
- fachada plana `ApplicationConfig` compatible con `FrameworkConfig`, nueve
  proyecciones de ruta y una proyección de límites globales calculadas desde el
  valor vigente;
- superficies de registro/validación CLI separadas para Audio, Video, Code,
  Semantic y Knowledge, sin cambiar sus flags planos;
- orquestador, locking, cancelación y heartbeat;
- selección y registro de rutas;
- coordinador global de recursos;
- extractores, clasificadores, cachés y repositorios por formato;
- catálogo documental, organización, revisión y evidencia;
- búsqueda PDF/DOCX/audio/video/código y servicio semántico;
- plataforma de evidencia externa, métricas/relaciones portables, contratos de
  arquitectura y proyecciones de status/review/diff para el autoanálisis;
- contratos, snapshot lógico, planner, recuperación, fusión y contexto de la
  Knowledge Plane read-only;
- watcher incremental foreground;
- acciones autorizadas sobre archivos, recibos de efecto, eventos append-only y
  conciliación de sólo lectura. Una operación `record` separada puede conservar
  la observación como evento append-only; decisión, autorización, recuperación
  y verificación productivas todavía no existen.

Es el paquete más grande y concentra integración, pero las rutas mantienen
bases y modelos propios para limitar transacciones cruzadas.

### `_05_Interfaz`

Frontend PySide6:

- transforma el formulario en una solicitud canónica;
- inicia un único worker hijo mediante `QProcess`;
- intercambia eventos estructurados y acotados;
- permite cancelación supervisada;
- consulta estado mediante conexiones cortas de sólo lectura;
- añade una página Consulta sobre `read_api` y `value_cli_adapter`, con scopes
  fijos, presentación acotada y sin controles de mutación.

La GUI ofrece PDF, DOCX, Office, ZIP, texto/correo, audio, video, imagen y Code. En
Linux presenta modo portátil, no solicita elevación y desactiva los controles
de mutación, sin retirar inventario, procesamiento o búsqueda.

### Compatibilidad de raíz

`Orquestador.py` conserva imports históricos y delega en los módulos actuales.
Se incluye en el paquete y tiene consumidores de prueba; por ello sigue siendo
compatibilidad necesaria. `python -m _02_Deduplicacion` es un wrapper
explícitamente obsoleto que delega en la aplicación integrada y no activa
acciones destructivas.

## Superficies públicas

### CLI instalada

La invocación canónica es:

```powershell
Neocortex --help
```

`neocortex.cli` selecciona perezosamente cuatro modos:

1. subcomandos humanos `help/status/search/ask/inspect/review/agent`;
2. CLI normal: delega en `_04_Nucleo_Operativo.cli_app`;
3. `--ui`: inicia la aplicación de escritorio;
4. `--gui-worker`: protocolo interno del frontend, no comando de usuario.

Las operaciones directas se registran declarativamente y cargan su handler de
forma lazy. Archive, texto, audio, Code, Semantic y Knowledge separan registro
y validación en sus módulos `cli_*_surface.py`; los handlers conservan sus
módulos de dominio.
Las operaciones que escriben estado adquieren el lock común cuando su contrato
lo requiere. La lista de comandos y códigos de salida está en
[CLI.md](CLI.md).

`Neocortex doctor capabilities [--json]` es un alias canónico estrecho que
`neocortex.cli` traduce a flags planos internos ocultos. El handler inspecciona
specs, metadata y ejecutables sin cargar engines/modelos ni crear estado; no
introduce un `--doctor` o `--json` global.

La Knowledge Plane se expone mediante operaciones directas mutuamente
excluyentes y no destructivas:

```powershell
Neocortex --knowledge-status
Neocortex --knowledge-search "protección diferencial" --knowledge-mode evidence
Neocortex --knowledge-context "protección diferencial" --knowledge-limit 12
```

Estas operaciones sólo abren estado existente y pueden informar owners
ausentes, incompatibles, futuros o corruptos sin crearlos ni migrarlos. Sus
formatos, opciones auxiliares y códigos de salida se detallan en
[KNOWLEDGE.md](KNOWLEDGE.md).

`Neocortex agent serve` adapta la misma API a MCP por stdio. El transporte
CPython 3.14 usa pipes asyncio nativos para evitar delegar stdin/stdout a
workers AnyIO; limita cada línea a 1 MiB y termina limpiamente al cerrar stdin.
No registra HTTP, rutas de estado ni productores.

`--action-recovery-status` es una excepción deliberada: abre
`framework.sqlite3` sin crearla ni migrarla y clasifica acciones inciertas sin
repetir una syscall. Su salida JSON pertenece sólo a esa familia.

### API Python

`_04_Nucleo_Operativo.__init__` expone perezosamente configuraciones, summaries,
rutas, orquestador, búsquedas, doctors y coordinador de recursos. Las clases de
ruta y `PdfDerivedIndexer` son superficies de bajo nivel: un consumidor que las
invoque fuera del orquestador debe respetar inicialización de esquema,
cancelación, recursos y exclusión de writers. La ejecución canónica mediante
`FrameworkOrchestrator` es la frontera que aplica el contrato integrado.

`neocortex.sdk` es la fachada pública lazy y tipada PEP 561 para Knowledge. Sus
símbolos conservan identidad con los imports legacy y tanto el paquete canónico
como el shim de implementación distribuyen `py.typed`.

La superficie diferida también exporta `ResourceRef`, `RevisionRef`,
`EvidenceRef`, `KnowledgeHit`, `KnowledgeSnapshot`, `ContextBundle`,
`KnowledgeQuery`, `KnowledgePlan`, `RetrievalMode`, `KnowledgeStatePaths`,
`KnowledgeSearchResult`, `KnowledgeSearchService` y
`plan_knowledge_query`. Estas APIs consultan estado persistente; no sustituyen
la corrida que lo produce.

Los exports diferidos de `route_registry` existen para compatibilidad y emiten
`DeprecationWarning`; los nuevos consumidores deben importar desde el módulo de
la ruta correspondiente.

## Corrida integrada

Una corrida normal sigue este orden lógico:

1. validar argumentos, raíz, estado y compatibilidad;
2. adquirir `%STATE%\framework.lock` mediante un lock del sistema operativo;
3. inicializar esquemas y marcar runs/acciones abandonados según la política
   vigente;
4. abrir el run común y su heartbeat;
5. intentar capturar el cursor USN inicial, sin exigirlo;
6. preparar inventario completo o incremental y su checkpoint;
7. construir el plan de deduplicación;
8. ejecutar sólo las acciones expresamente autorizadas;
9. publicar atómicamente el vínculo al inventario y el conjunto completo de
   candidatos de ruta;
10. iniciar las rutas de contenido seleccionadas;
11. actualizar catálogo y organización cuando corresponda;
12. podar estado transitorio permitido;
13. completar el run y detener el heartbeat;
14. liberar el lock.

Errores y cancelación toman ramas distintas. `KeyboardInterrupt` solicita
cancelación cooperativa y el launcher devuelve `130`; una ruta fallida no debe
presentarse como completada.

La enumeración, el inventario y las rutas escriben distintas bases. Por ello la
finalización del run común no reemplaza los invariantes locales de publicación
de cada propietario.

La publicación del snapshot de enrutamiento ocurre después de que
`FrameworkActions.execute()` termina de persistir todos los candidatos. El
`scan_id`, los contadores de inventario y el evento versionado
`neocortex.routing-snapshot/v1` se confirman en la misma transacción de
`framework.sqlite3`; una ruta no puede iniciarse mientras ese vínculo no exista.

Para corridas normales, `NormalInventoryBoundary` captura raíz, estado,
`InternalPathsPolicy` y exclusiones. La policy reserva por ruta e identidad
física el repositorio, runtime, datos de aplicación, autoanálisis y launcher;
detecta aliases/reparses y el hardlink del launcher. Los árboles internos que
quedan bajo un corpus permitido se excluyen, pero una raíz situada dentro de
ellos se rechaza. El estado tampoco puede ser igual ni ancestro del corpus. La
firma cruda de `InventoryExclusionPolicy` se guarda en Dedup v9; Framework y
watcher usan la firma efectiva versionada que combina esa firma con la de
`InternalPathsPolicy`.

## Autoanálisis de código

`FrameworkOrchestrator.run_self_analysis()` es una rama vertical distinta de la
corrida común. El preflight exige `analyze_only`, raíz/estado disjuntos y
una única ruta `code` cuyo `RouteAdapter.input_source` sea
`inventory_snapshot`. Después captura las identidades, crea el estado sólo tras
validarlas y repite la frontera en los fences de E/S. No instancia el planner
de duplicados, `FrameworkActions`, catálogo ni organización.

El inventario completo y USN comparten una policy concreta y su firma
`inventory-exclusion-policy-v2:xxh3_128:...`. La reutilización incremental se
autoriza sólo por la conjunción del último binding durable del framework, el
checkpoint Dedup del mismo scan/cursor y la identidad/cursor vivos. Un fallo en
cualquiera fuerza full scan sin invalidar el checkpoint; no se recupera una
firma histórica detrás de un run durable más reciente incompatible.

El autoanálisis admite además un full scan portable cuando USN es inaccesible.
Ese camino no publica checkpoint, conserva nulos los cursores y falla cerrado
en frescura. La corrida normal usa la misma enumeración portable y publica un
checkpoint Dedup v9 con cursor nulo; sus consumidores obtienen incrementalidad
comparando el snapshot contra caches por identidad y metadatos. USN es una
optimización durable, no un requisito de corrección ni una identidad ficticia
del fallback.

Code consume directamente el scan publicado con cero `route_candidates`. La
finalización incorpora una plataforma genérica de proveedores sobre las
versiones Python vigentes con fingerprint exacto, incluidas las parciales.
`protected` ejecuta Ruff basic aislado; `trusted-static` añade Ruff con
configuración versionada del proyecto acotada a `E4,E7,E9,F,B,C4,PIE,RUF`,
Mypy, Pyright, Ruff Analyze, Grimp, Complexipy, Vulture, Semgrep, Deptry,
pip-audit, inventario del entorno instalado e historia Git local como
productores independientes.
`I,PT,SIM,UP` se excluyen para que estilo y modernización no
desplacen la señal de mantenimiento. Cada proveedor conserva
descriptor, firma de entorno/configuración/comparabilidad, inputs, findings y
counters normalizados. La suite y el fence de Code se confirman atómicamente;
los proveedores no participan en el processing signature AST.

Code schema v4 extiende la persistencia compatible v1-v3 con dos proyecciones
portables. `external_metrics` vincula un nombre/valor/unidad con un sujeto
tipado (`file`, `symbol`, `module`, `project`, `run`, `contract` o `scc`);
`external_relations` vincula dos sujetos tipados con dirección, confianza y
metadata determinista. Sus IDs y digests no dependen de IDs SQLite locales. El
replay enlaza esas filas desde el run fuente; status, review, diff y work
packages son sus consumidores, de modo que la plataforma no acumula métricas o
relaciones sin una decisión pública.

La capa arquitectónica divide fuente, política y consumo:

1. `ruff-analyze-imports` normaliza la salida de Ruff Analyze y actúa como
   oráculo diferencial, independiente del productor principal.
2. `grimp-architecture` construye el grafo mediante Grimp `3.15`, publica
   relaciones `module_import`, fan-in/fan-out y componentes fuertemente
   conexos, y evalúa `neocortex.code-architecture-contracts/v1`.
3. `complexipy-cognitive` usa la API `file_complexity` de Complexipy `6.2.0`
   para publicar complejidad cognitiva por símbolo y agregados total/máximo por
   módulo.

El dominio versionado incluye exactamente los seis paquetes de producción;
excluye `tests`, `tools`, `benchmarks` y el módulo de compatibilidad independiente
`Orquestador.py`. Los contratos impiden dependencias transitivas Core→UI y
Foundation→Core/UI, imports de producción hacia namespaces no productivos,
restringen las fronteras Dedup→Core y `neocortex`→Core/UI mediante allowlists, y
fijan los SCC conocidos como baseline de `no-new-production-import-cycles-v1`.
Por tanto un ciclo conocido es una observación versionada, no un aprobado ni la
afirmación de que el grafo sea acíclico.

La proyección `neocortex.code-architecture-analysis/v2` conserva por módulo un
`owner_id` —el primer componente del módulo, no ownership del repositorio—,
los SCC y sus ciclos explícitos. Sobre el mismo grafo publica
`dependency_reach` y `blast_radius`, con banderas `*_truncated` cuando el límite
convierte el valor en una cota inferior, `directed_degree_centrality` y cruces
de owner entrantes y salientes. El corte real aceptado resolvió `283` módulos,
`1115` imports y `4` SCC cíclicos; sus `6` contratos no registraron fallos.

Import Linter `2.13` se midió viable sobre el mismo dominio, pero no quedó en la
ruta productiva: envolverlo repetiría el grafo que ya entrega Grimp y su salida
de contratos no ofrece un contrato JSON directo. Complexipy se consume por API
porque el código de salida de su CLI también representa superar un umbral
predeterminado; esa semántica no debe confundirse con un fallo de herramienta.

El replay exacto valida de nuevo los inputs y enlaza la publicación completa
mediante `external_run_replays`; no duplica findings, métricas o relaciones.
Conserva como costo real el tiempo y bytes de verificación. Los proveedores de
código no abren procesos; pip-audit reutiliza el snapshot vigente sin red y el
inventario instalado vuelve a verificar los hashes y tamaños `RECORD`, por lo
que ese replay conserva trabajo local real. Mypy y
Pyright se mantienen en espacios de evidencia separados y sólo producen un
resumen de coincidencias/discrepancias cuando ambos tienen cobertura completa.
Los trece proveedores `trusted-static` son advisory, no ejecutan contenido del
proyecto, no aplican fixes y no poseen autoridad de mutación. Sólo pip-audit
declara red para capturar el snapshot de PyPI; los otros doce son locales.
`trusted-deep` añade
`pytest-coverage-trusted-deep` únicamente para la identidad física exacta de la
raíz canónica: carga plugins y contenido, ejecuta la suite declarada bajo
límites y reconoce que no impone sandbox de red. Sigue siendo advisory, nunca
predeterminado y carece de autoridad de mutación. En esa misma frontera,
`cosmic-ray-focal-mutation` (`neocortex.cosmic-ray-focal-mutation/v1`) exige un
target y tests declarados, crea una copia staged exacta, muta sólo esa copia y
verifica hashes antes y después bajo límites de tiempo, salida y mutantes. El
corte final fijó un máximo medido de `20` y completó los `20` mutantes: `5`
killed, `5` survived, `10` incompetent y `0` timeout; el score focal fue
`0.5`, excluyendo incompetentes, y el replay exacto abrió `0` procesos Cosmic
Ray. El contrato
sigue siendo advisory, declara `mutation_authority=false` y `uses_network=true`
porque los tests declarados podrían usar red.

`git-history-local` (`neocortex.git-history-local/v1`) lee sólo el repositorio
Git local verificado bajo ventanas y límites explícitos. Publica por identidad
churn, frecuencia de cambio y edad/recencia, además de relaciones de cochange;
la corrida final produjo `10460` métricas y `859` relaciones. Son observaciones
históricas, no una probabilidad de defecto.

`vulture-unused-static` aporta candidatos heurísticos que
`neocortex.code-unused-analysis/v1` correlaciona con Pyright, grafo, exports,
contratos dinámicos y Coverage. Los cuatro estados explicables, calibración y
holdout alimentan status, review, diff y work packages sin score mágico. Incluso
`probable_unused_high_consensus` sólo crea trabajo de caracterización con
confirmación humana; nunca autorización de borrado.

La proyección `neocortex.code-supply-chain-analysis/v1` consume cuatro
proveedores sin crear otro datastore: Semgrep publica invariantes específicas
de NeoCortex; Deptry, higiene entre imports y declaraciones; pip-audit, un
snapshot fechado de vulnerabilidades conocidas; e `importlib.metadata` más
`RECORD`, constraints, integridad y metadata de licencia del wheel instalado.
Las observaciones conservan las categorías `dependency_hygiene`,
`known_vulnerability`, `package_integrity` y `license_inventory`. Seis gates
se evalúan por dimensión y nunca se combinan en un score ni en una probabilidad
de defecto. Status, review, diff y work packages son consumidores reales de
findings, métricas y relaciones; ninguna evidencia concede autoridad de
mutación.

La proyección pública `architecture_analysis` mantiene por separado
`import_graph_consensus`, `architecture_contracts` y
`module_complexity_displacement`. Review v10 y publication diff v8 sólo aprueban
los gates comparables `architecture_contracts_not_degraded`,
`no_new_import_cycles` y `module_complexity_not_displaced`; en un baseline,
ante cobertura parcial o firma incompatible quedan `baseline` o
`not_evaluated`. Los work packages añaden módulo primario, cadenas de imports
acotadas, contratos afectados y esos gates, pero conservan autoridad advisory.
El consumidor correlaciona estas señales con los callers/callees estáticos ya
publicados por Code: fan-in/fan-out y dependencias aportan contexto modular,
mientras el grafo de llamadas conserva alcance por símbolo. No duplica ese
grafo en otra tabla ni combina ambas dimensiones dentro de un score mágico.

`neocortex.code-engineering-analytics/v1` correlaciona por identidad publicada
las dimensiones separadas de complejidad, cobertura, mutación, historia y
grafo. Conserva procedencia, limitaciones y abstenciones por dimensión; nunca
produce un score agregado ni una probabilidad de defecto, y permanece advisory
sin autoridad de mutación.

La finalización adquiere una transacción propia y exige exactamente una ruta code
completada, identidad vigente y ceros en candidatos, `file_actions`,
`run_actions` y organización. El cambio del run a `completed` y el único
manifest `neocortex.self-analysis-manifest/v2` se confirman juntos. Framework
v20 conserva modo, identidad, estado y firma; sus triggers y
`CorpusMutationGuard` forman una segunda defensa en los propietarios de mutación.

`--code-status --code-json` proyecta el manifest y su frescura sin crear o
migrar estado. Sus lectores usan SQLite `immutable`, `query_only` y fences
pre/post. Cualquier sidecar, incluso vacío o desacoplado, o una cerca inestable
en code, framework o Dedup causa abstención total con código `2`. El diseño
completo, argv reproducible y límites de validación están en
[SELF_ANALYSIS.md](SELF_ANALYSIS.md).

## Mutación ligada a identidad y recuperación

Las mutaciones soportadas de rename y organización usan
`windows_handle_mutation.rename_no_replace_by_identity`. La primitiva mantiene
abiertos el archivo fuente y el directorio destino, verifica volumen/FileId y
opera de forma relativa al handle del padre con semántica *no-replace*. El
contrato es deliberadamente estrecho: Windows, NTFS local, archivo regular, un
solo hard link y mismo volumen. UNC, otros filesystems, reparses, directorios,
hard links múltiples y movimientos entre volúmenes provocan abstención; no hay
fallback permisivo por ruta.

Ese backend es exclusivamente Windows. En Linux, `--apply` y
`--organization-apply` se rechazan antes de crear estado con código `2` y razón
`linux_mutation_backend_unavailable`; no existe un fallback con `Path.rename`.

`file_actions` conserva en el esquema framework v20 la frontera incorporada en v18:

```text
started -> applying -> applied
                    \-> recovery_required
```

`applying` se persiste con identidad esperada justo antes de la llamada nativa;
`applied` exige un recibo posterior. Si el proceso o el registro fallan después
de cruzar la frontera, la acción queda `recovery_required`. Cada transición
agrega una fila a `file_action_events`; triggers impiden actualizar o borrar
esos eventos. Al reiniciar, una acción `started` abandonada antes de la
frontera queda `failed` con evidencia de que no se intentó el efecto; una
`applying` abandonada queda `recovery_required`. Ninguna se repite
automáticamente.

El conciliador observa origen y destino y devuelve `confirmed`,
`not_performed`, `ambiguous` o `impossible_to_check`. `status` es idempotente y
de sólo lectura. `record` agrega a `file_action_reconciliation_events` una
observación append-only con CAS, key idempotente, actor, procedencia, firma y
evidencia, pero declara que no autoriza una mutación. No hay todavía contratos
`decide`, `authorize`, `recover` o `verify`. Un recibo de Papelera sólo
confirma la acción si liga las rutas origen/destino de esa misma acción, aunque
la aplicación de Papelera sigue deshabilitada. Los planes de organización
conservan su propio
`recovery_required`, excluido del selector automático y del reintento; además
reserva el destino para evitar que otro plan lo reutilice.

La API de Papelera disponible era path-bound. Por ello `0.7.0` conserva la
planeación y validación en dry-run, pero `--apply` se abstiene y registra esas
acciones como `skipped`. `Send2Trash` fue retirado y no se ofrece un override
inseguro.

## Registro y ejecución de rutas

El orden estable es:

| Ruta | Entrada principal | Salida persistente | Consumidor adicional |
|---|---|---|---|
| `pdf` | snapshots identificados como PDF | texto, páginas, OCR, warnings, FTS, similitud y layout | catálogo documental |
| `docx` | OOXML Word validado | partes, texto, diagnósticos, FTS, layout y vínculos PDF | catálogo documental |
| `office` | OOXML/ODF de otros documentos | texto, XLSX por celda tipada, estado y FTS | catálogo documental |
| `archive` | ZIP y ZIP anidados validados | miembros virtuales, cadena de contenedores, texto nativo/OCR, incidencias y FTS | Knowledge y Semantic; no organización física |
| `text` | texto imprimible, EML y CFB DOC/XLS/PPT | texto visible, título/autor, metadata, errores y FTS | catálogo documental, Knowledge y Semantic |
| `audio` | audio/vídeo sondeado | transcripción, segmentos y FTS | catálogo documental |
| `video` | streams visuales sondeados | escenas/keyframes, frames, OCR, timestamps, métricas y FTS | búsqueda directa y revisión; visual-only admitido |
| `image` | imágenes no documentales o candidatas de documento | clasificación, OCR/evidencia, estado y huella completa Dedup | revisión y Semantic; no catálogo documental actual |
| `code` | archivos de texto/código acotados | proyectos, versiones, AST/símbolos, referencias, grafo, chunks y FTS | búsqueda y puente semántico |

El grafo de código conserva esquema 4 y una transacción global en
`finalize_graph`. Lectores concurrentes observan el snapshot anterior hasta el
commit y los fallos por fase revierten el estado completo. Se descartó
fragmentar esa transacción: antes se requiere un esquema sucesor que defina build,
membresía, head/CAS, writer, reanudación, publicación, migración, rollback y
poda como un único contrato.

La reutilización exige la misma ruta observada, metadatos, firma y analizador
efectivo. Un hit de ruta invariable actualiza sólo presencia/observación y hace
cero DML en `code_fts`; una ruta distinta rechaza la caché y el productor
publica una versión sucesora, en vez de mutar la evidencia histórica.

En una corrida completa sin límite ni selección, `mark_missing` precede al
grafo. Una finalización real elimina y reconstruye las membresías derivadas y
sincroniza en una sola sentencia las etiquetas FTS vigentes realmente distintas
mediante un mapa temporal indexado; las etiquetas históricas permanecen
inmutables. El resolver v4 materializa conjuntos temporales indexados de
símbolos y dependencias vigentes. Primero enlaza llamadas dentro del mismo
módulo o clase y módulos relativos por su ruta léxica exacta; después aplica el
fallback global por nombre cualificado o simple sólo cuando la coincidencia es
única. Los empates y ausencias permanecen ambiguos o no resueltos; no se
fabrican aristas. Sólo se omite si no cambió
ninguna entrada,
todos los candidatos fueron cache hits compatibles con el runtime y un fence
tipado `code-graph-resolver-v4` identifica exactamente el run completo
inmediatamente anterior con la misma firma. El fence avanza atómicamente con la
finalización de ese `analysis_run`; ausencia, corrupción, run intermedio o la
primera corrida sobre una base existente sin fence fallan cerrados hacia
`finalize_graph`.

El analizador Python `neocortex-python-ast` versión 3 conserva además el nivel y
módulo de imports relativos —incluido `from . import módulo`— para que paquetes
con basenames repetidos no se mezclen. Sólo publica símbolos de
asignación para nombres realmente enlazados por objetivos `Name`,
`Tuple`/`List` y `Starred`. Atributos y subscripts no crean símbolos globales o
de clase espurios, y un nombre repetido en la misma asignación se conserva una
sola vez.

El puente Code↔Semantic conserva los propietarios separados. Una publicación
textual completa de `source_kind=code` termina primero el head Semantic v6 y,
bajo el lock común del framework, proyecta en `code.embedding_links` un enlace
por chunk vigente. Cada enlace fija item Semantic, modelo, espacio vectorial,
generación y procedencia; no existe FK ni transacción distribuida entre las dos
bases. La proyección se construye completa en TEMP y falla antes de mutar si un
miembro no resuelve exactamente por identidad, versión, índice y clase de chunk.
La búsqueda Code acepta un hit semántico sólo si ese enlace sigue activo y la
versión continúa vigente. Los enlaces de generaciones anteriores se conservan
inactivos como historia reconstruible; no son evidencia publicada actual.

`RouteAdapter` recibe un `RouteExecutionContext` con configuración, raíz,
`run_id`, `scan_id`, estado corto de framework, cancelación y coordinador global.
La summary debe ser dataclass o mapping serializable.

PDF e imagen tratan el stream de candidatos como un recurso con afinidad de
thread: creación, iteración y cierre ocurren en el thread propietario de la
conexión SQLite. Un `finally` del productor lo cierra también ante error o
cancelación; el coordinador no desenrolla ese generator desde otro thread.

Las rutas PDF, DOCX, Office, texto y audio alimentan el catálogo por lotes.
Archive, imagen y código conservan repositorios especializados; no deben
presentarse como documentos catalogados si no existe ese consumidor. Knowledge
consume FTS de Archive y texto directamente; Archive conserva la cadena
`ZIP!/miembro` como procedencia y no entra a organización física.

## Concurrencia y cancelación

### Exclusión global

`FrameworkRunLock` bloquea un byte de `framework.lock`. El orquestador y varias
operaciones directas de escritura lo adquieren para impedir dos corridas
integradas simultáneas sobre el mismo estado. El archivo no registra PID ni
línea de comandos; un error de contención sólo prueba que otro handle mantiene
el lock.

No se debe borrar el lock para “desbloquear” una ejecución. El sistema operativo
libera el bloqueo al cerrar el handle; primero debe identificarse el proceso
propietario.

### Paralelismo de rutas

Las rutas seleccionadas se envían a un `ThreadPoolExecutor` acotado por su
cantidad. Cada una recibe:

- un estado de framework de vida corta;
- token de cancelación común;
- coordinador global de memoria, commit, CPU y carga;
- su propia base de ruta.

El orquestador espera las rutas, conserva errores por nombre y cancela de forma
cooperativa si una ruta falla o el usuario interrumpe. Algunas rutas usan
procesos `spawn` supervisados para aislar bibliotecas nativas y timeouts.

El lock PDF adicional es un `RLock` **local al proceso**. Serializa writers del
proceso padre, pero no sustituye `framework.lock` ni protege consumidores Python
externos en otro proceso.

### Watcher

El watcher:

- corre en primer plano;
- usa lotes USN como señal de que debe reconciliarse;
- no publica un cursor independiente;
- vuelve a cargar el checkpoint durable después de cada corrida;
- aplica debounce y backoff acotados.

Además del `threading.Lock` por instancia, `WatcherLifeLease` mantiene un byte
lock del sistema operativo durante toda la vida del proceso para la identidad
canónica `(root,state_directory)`. Su nombre usa XXH3-128 y sus metadatos
acotados registran PID, creación del proceso, host, versión, argv, raíz, estado e
inicio. El lock del SO es la autoridad: un owner vivo provoca abstención; JSON
stale sólo se reemplaza después de adquirirlo y nunca se mata un proceso. El
handle se libera en cierre normal o caída. Raíces distintas no colisionan y las
corridas directas conservan `framework.lock` por corrida.

## GUI y worker

El proceso de UI no ejecuta el pipeline dentro del event loop. `WorkerController`
crea un proceso hijo con el mismo intérprete y el módulo `_05_Interfaz.worker`.
El worker:

- reconstruye parser y configuración canónicos;
- emite eventos estructurados de progreso y terminales;
- mantiene heartbeat supervisado;
- escucha cancelación;
- captura `KeyboardInterrupt` y `BaseException` para emitir un cierre
  observable;
- no se desprende ni se instala como servicio.

Las líneas y buffers están limitados. La ventana conserva un historial visual
acotado; ese historial no sustituye las tablas persistentes de eventos.

La página Consulta no usa el worker productor: llama de forma diferida a la API
read-only compartida, valida schema/kind/scope/exit code y limita la
presentación a 200 filas y 128 KiB. Status y revisión no requieren texto;
search/ask aceptan hasta 4096 caracteres. Una incompatibilidad provoca
abstención visible y no intenta migrar ni reparar.

## Persistencia y flujo de datos

Las ubicaciones persistentes por usuario son:

```text
Estado normal: %LOCALAPPDATA%\Neocortex\state
Autoanálisis:  %LOCALAPPDATA%\Neocortex\self-analysis
Modelos:       %LOCALAPPDATA%\Neocortex\models
```

En Linux:

```text
Estado normal: ${XDG_STATE_HOME:-~/.local/state}/Neocortex/state
Configuración: ${XDG_CONFIG_HOME:-~/.config}/Neocortex
Releases:      ${XDG_DATA_HOME:-~/.local/share}/Neocortex/releases
Modelos:       ${XDG_DATA_HOME:-~/.local/share}/Neocortex/models
```

Las bases principales son `dedup`, `framework`, `pdf`, `docx`, `office`,
`archive`, `text`, `audio`, `video`, `image`, `document_catalog`, `code` y
`semantic`.
No todas existen antes de usar su ruta. La UI persiste configuración aparte, en
`%LOCALAPPDATA%\Neocortex\ui.ini`, y FastEmbed usa el directorio hermano
`models\fastembed`. En Linux la UI usa el árbol de configuración XDG y
FastEmbed el cache compartido `models/fastembed`.

La Knowledge Plane no es otro owner persistente: conserva los diez owners
históricos y agrega Archive y texto sólo cuando existen sus bases. Su snapshot
y resultados viven en memoria y no introducen una migración propia.

En Dedup v9, `DedupIndex.published_snapshots(root)` es el lector público para
recorrer la generación vigente: checkpoint y filas se seleccionan en una sola
sentencia SQL y conservan el snapshot del lector ante una publicación y poda
concurrentes. Cada scan nuevo conserva su firma cruda de exclusión. La migración
7→8 preserva scans, archivos y bytes, pero invalida checkpoints legacy sin firma
en vez de inventar evidencia; 8→9 conserva publicaciones y vuelve opcional el
cursor USN como una terna indivisible. No combine por cuenta propia
`inventory_checkpoint(root)` con `snapshots(scan_id)`; entre ambas llamadas otro
writer puede publicar y podar la generación elegida.

En semantic v6, cada `model_signature` tiene un único
`published_embedding_heads`. Una generación `building` clona de forma acotada
los miembros de una base fijada. El clon confirma por páginas un cursor durable
con high-watermark y conteo; comparte el deadline del productor y reanuda el
prefijo confirmado. Adjunta resultados a revisiones inmutables y sólo un cierre
completo, después de revalidar la base, cambia el head dentro de la transacción
de finalización. Un
cierre parcial queda `ready_partial` y no publica; un CAS perdido obliga a
rebase. Las búsquedas oficiales fijan los heads al inicio y resuelven hits desde
sus miembros/revisiones congelados. El contenido y la identidad publicados
permanecen inmutables, pero el localizador `path` se toma de `semantic_items`
sólo cuando coinciden `item_id`, `source_kind` y `source_identity`; así un move
confirmado no deja resultados apuntando al origen ni una identidad reasignada
puede redirigir evidencia histórica. El resolver contrasta además
`vector_space` y modalidad del hit con el modelo persistido; no confía en esos
campos suministrados por el llamador.

El staging textual mantiene una única sesión SQLite por `source_kind` y agrupa
cada transacción en un máximo de 128 items o chunks. Un item mayor se divide en
lotes de hasta 128 chunks. Error, cancelación o cualquier `BaseException`
revierte sólo la transacción en curso; el prefijo ya confirmado permanece
idempotente y reanudable dentro de la generación `building`. La desactivación de
miembros no observados ocurre al finalizar la fuente, y ningún prefijo parcial
cambia el head publicado. Este cambio no altera schema, API Python ni JSON.

El worker alcanza un punto fijo de reutilización exacta antes de cada claim:
agota jobs pendientes cuyo modelo, XXH3, longitud y guarda coinciden con un
payload durable. Por ello el payload creado por el batch N satisface duplicados
que sigan pendientes antes del claim N+1. Todo lease aún propio se libera ante
`RuntimeError`, `KeyboardInterrupt` u otra `BaseException` sin ocultar la
excepción original. Permanecen dos límites explícitos: duplicados ya incluidos
en el mismo batch pueden llegar juntos al backend y los commits/fallos por job
todavía realizan persistencia N+1.

En catálogo v6, cada `source_kind` construye filas en
`catalog_generation_documents`. Los lectores siguen viendo la proyección
`documents` anterior hasta que una transacción reemplaza esa fuente, agrega el
historial, reconcilia planes y cambia `catalog_publications` mediante CAS. Un
fallo o cancelación conserva el puntero previo; dos publicaciones competidoras
marcan la atrasada `superseded`.

Ambos contratos preservan generaciones fallidas o abandonadas para diagnóstico.
Un planificador dry-run puede inventariarlas y proteger publicaciones, bases y
leases. También bloquea generaciones semánticas referenciadas por evidencia y
protege el último run completado del framework. Todavía no existen
`prepare/apply/verify`, poda ni enforcement de cuotas. Consumidores externos
que consulten directamente las tablas legacy mutables no reciben estas
garantías.

La poda owner-local del inventario sólo puede ejecutarse cuando el coordinador
entrega explícitamente todos los `scan_id` retenidos por framework. Sin esos
holds falla cerrado; con ellos conserva la publicación vigente, la anterior y
cualquier referencia cross-store. No es un motor de retención genérico ni
elimina evidencia humana o acciones inciertas.

La propiedad de un esquema implica:

- un solo módulo decide DDL y migraciones;
- los writers deben usar su factory canónica;
- los lectores deben abrir `mode=ro` cuando no modifican;
- las relaciones entre bases se expresan mediante identificadores y evidencia,
  no mediante foreign keys cruzadas;
- un run global no vuelve atómica una publicación local incompleta.

`neocortex.sqlite_connection` centraliza modos explícitos y salvaguardas
connection-local, pero su adopción productiva actual se limita a las factories
de PDF, DOCX y catálogo. `FrameworkRouteState` conserva una apertura separada
de estado existente mediante URI `mode=rw`; no se forzó una factory universal.
El inventario de esta fase registra 42 connects en 25 módulos y 132
adquisiciones mediante 20 factories de propietario. Consulte
[PERSISTENCE.md](PERSISTENCE.md) para la matriz exacta y los límites de SQL
externo/WAL.

Una conexión URI `mode=ro` con `query_only=ON` no debe describirse como
byte-neutra: SQLite todavía puede participar en `-wal`/`-shm`. La barrera de
esta continuación validó únicamente bases nuevas dentro del laboratorio; no
abrió ni migró bases operativas vivas.

## Recursos y procesos externos

Controles observados:

- futuros de trabajo PDF e imagen acotados aproximadamente a `workers * 2`;
- batches del catálogo de 100 filas y de escritura semántica de hasta 500;
- staging semántico textual de hasta 128 items o chunks por transacción y una
  sesión SQLite por fuente;
- colas multiprocessing pequeñas para PDF, imagen y Whisper;
- límites de miembros, expansión y central directory antes de abrir ZIP/OOXML;
- OCR de PDF e imágenes dentro de ZIP y conversión de Office heredado en
  procesos aislados con tiempo, memoria y salida acotados;
- límites de píxeles, texto, páginas, duración y segmentos por ruta;
- subprocess con argumentos, timeout, drenaje concurrente y límite de salida;
- limpieza de temporales después de cerrar procesos y handles;
- admisión global según memoria física, commit, carga y slots CPU.

En Windows, los procesos aislados y `run_bounded_capture()` crean el hijo
suspendido, lo asocian por su handle exacto a un Job Object con
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` y sólo entonces lo reanudan. Timeout,
overflow, cancelación y excepciones terminan el Job, esperan al hijo directo,
cierran pipes y liberan el handle; así los descendientes propios no sobreviven
a la frontera supervisada.

En POSIX, el hijo usa sesión/grupo de proceso propio. Timeout, overflow,
cancelación y excepciones alcanzan todo el árbol con `SIGTERM` y después
`SIGKILL`. Un límite de memoria solicitado se impone mediante `RLIMIT_AS` o
`/usr/bin/prlimit`; si ninguna vía está disponible, la operación se abstiene en
vez de ejecutar sin contención.

Herramientas externas posibles:

- Tesseract para OCR;
- FFprobe/FFmpeg para audio y vídeo;
- qpdf opcional para recuperación estructural PDF;
- Ruff y Mypy como evidencia advisory del autoanálisis, resueltos desde el
  runtime Python; Ruff usa política basic aislada o política trusted del proyecto;
- Pyright `1.1.411` como paquete npm aislado junto al runtime, ejecutado mediante
  Node únicamente en `trusted-static`;
- Ruff Analyze como oráculo diferencial de imports, ejecutado por el mismo Ruff
  del runtime y sin configuración extensible;
- Grimp `3.15` y Complexipy `6.2.0` como productores Python aislados de grafo,
  contratos y complejidad cognitiva en `trusted-static`;
- Vulture `2.16` para candidatos heurísticos de código potencialmente no usado;
- Semgrep `1.172.0` con tres reglas locales y autofix deshabilitado, Deptry
  `0.25.1`, pip-audit `2.10.1` y Packaging `26.2` para la evidencia separada de
  supply chain;
- FastEmbed y Faster-Whisper para inferencia local.

No se observó `shell=True` en el motor auditado. La presencia de límites no
equivale a sandbox completo; véase [SECURITY.md](SECURITY.md).

## Empaquetado y dependencias opcionales

El paquete se construye con setuptools y exige Python `>=3.13,<3.15`, validado
en Windows y Linux con CPython 3.13 y 3.14. Incluye
los seis paquetes de producción, `neocortex`, el shim `Orquestador.py`, las
reglas Semgrep y assets de la GUI. La base exacta incluye Complexipy, Coverage,
Deptry, Grimp, Mypy, Packaging, pip-audit, Pytest, Rich, Ruff, Semgrep, Vulture
y xxHash; `documents`, `audio`,
`image`, `semantic` y `ui` declaran runtimes opcionales, y `full` es su unión compatible.
`neocortex.capabilities` inspecciona esa disponibilidad de forma estática; no
certifica inferencia, caché de modelos ni compatibilidad resuelta.

La ayuda y versión deben arrancar sin cargar rutas pesadas. La instalación, el
wheel y el sdist deben validarse en un entorno limpio antes de publicar; este
documento no afirma que esa barrera final ya haya ocurrido.

En Linux, `tools/release_linux.py` instala el wheel `full` desde artefactos
binarios, integra Node/Pyright dentro de una release inmutable, activa
`current` bajo `flock`, conserva releases anteriores y publica launcher, alias
y KDE sólo después de validar modelos y runtime. Los recibos viven en el estado
XDG.

El inventario técnico de metadata/licencias y archivos redistribuidos está en
[THIRD_PARTY_LICENSE_INVENTORY.md](THIRD_PARTY_LICENSE_INVENTORY.md). No declara
una licencia propia ni concede permisos; las decisiones de licencia/NOTICE
pertenecen al propietario.

## Extensibilidad

Una ruta nueva debe definir antes de integrarse:

1. nombre estable y posición o política de orden;
2. tipos de entrada y detección;
3. configuración y límites;
4. base propietaria o contrato explícito de reutilización;
5. firma de procesamiento y política de caché;
6. summary serializable;
7. eventos de progreso y cancelación;
8. interacción con catálogo, revisión y semántica;
9. pruebas de error, reanudación, recursos y empaquetado;
10. documentación de dependencias y herramientas externas.

No debe añadirse una base, repositorio o clasificación sin productor y
consumidor confirmados.

## Compatibilidad y retirada de legacy

Clasificación actual:

| Elemento | Estado | Criterio de retirada |
|---|---|---|
| `Orquestador.py` | necesario | retirar sólo tras deprecación y prueba de ausencia de consumidores |
| `_02_Deduplicacion.__main__` | temporalmente necesario/deprecable | versión anunciada y migración de invocaciones |
| exports diferidos de `route_registry` | deprecables | eliminar después del periodo documentado y búsqueda de consumidores |
| fachadas `state`/`semantic_state`/`semantic_service` | necesarias | hoy tienen consumidores internos y de prueba |
| `SqlitePathIndex` | auxiliar soportado, integración no verificada | decidir explícitamente si se integra o se depreca; no eliminar por análisis automático |

Una métrica de complejidad, vulture o ausencia de import interno no basta para
eliminar una API empaquetada.

## Riesgos arquitectónicos pendientes

Los siguientes límites deben permanecer visibles:

- `NC-AUD-001`, `NC-AUD-002` y `NC-AUD-003` quedaron corregidos en el código
  v7 y se conservan en v8 con regresiones de migración
  poblada/abstencionista, aislamiento, publicación, scan parcial, lectura
  concurrente, poda y cursor USN ambiguo; la barrera integral se registra
  aparte;
- la poda vigente de v9 conserva generaciones `building` y candidatos `complete` aún no
  publicados para evitar carreras; el planner dry-run diagnostica candidatos,
  pero todavía no ejecuta expiración/conciliación de un build abandonado;
- semántica v6 y catálogo v6 aíslan el staging y publican por puntero/CAS para
  sus lectores oficiales (`NC-AUD-012` y `NC-AUD-013`); SQL externo sobre
  tablas legacy no hereda el contrato;
- el grafo de código conserva esquema 4 no generacional y una transacción global
  extensa (`NC-AUD-015`); es atómica para lectores, pero carece de reanudación y
  de cancelación dentro de una sentencia SQL. Los empates permanecen ambiguos y
  la firma global del registro puede invalidar lenguajes no afectados; no debe
  fragmentarse sin el diseño generacional completo;
- la Knowledge Plane no implementa un grafo transversal entre owners;
  relaciones verificadas e historial transversal se reportan como capacidades
  incompletas en vez de inferirse. MCP es sólo una fachada read-only y no añade
  ese grafo;
- el golden Knowledge vigente ejecuta candidatos de owner scripted; comprueba
  contratos y fórmulas, no una evaluación humana ni calidad representativa del
  corpus;
- la calibración visual medida no separa positivos y negativos con un umbral
  escalar robusto, de modo que CLIP permanece fail-closed; MiniLM sigue como
  shadow hasta un A/B real etiquetado y una generación propia;
- el planner semántico valida tipo y longitud de payloads reutilizados, pero el
  writer `semantic_generation_repository.reuse_cached_jobs` aún no replica esa
  guarda; esa convergencia pertenece a Fase 2;
- el máximo configurable de scratch (16 TiB) es un límite de validación, no una
  promesa de que toda build de SQLite acepte ese `max_page_count`; el default
  operativo permanece en 512 MiB y el planner falla cerrado;
- los propietarios SQLite oficiales quedaron clasificados y sus familias
  verifican existencia/FK/query-only/timeout/rollback/cierre (`NC-AUD-017`);
  SQL externo puede evadirlas y no se comprobaron bases operativas vivas;
- rename y organización sólo operan con identidad ligada por handles dentro del
  subconjunto NTFS soportado; Papelera se abstiene y la conciliación de
  `file_actions` es idempotente y su observación puede persistirse append-only,
  pero decisión/autorización/recuperación no están implementadas y los planes
  de organización continúan en diagnóstico manual;
- `semantic_status` eliminó N+1 de conexiones y summaries, y conserva una sola
  conexión/snapshot; sus nueve conteos completos todavía pueden ser costosos
  (`NC-AUD-019`);
- el watcher tiene exclusión cross-process de por vida por raíz+estado y se
  abstiene ante owner vivo (`NC-AUD-020`); el archivo de diagnóstico persiste y
  no debe borrarse mientras un proceso pueda poseerlo;
- no hay comando general incorporado de backup/restauración; retención sólo
  ofrece dry-run, sin delete/cuotas/compactación, por lo que generaciones
  fallidas o abandonadas pueden crecer (`NC-AUD-014`);
- este corte no promovió el launcher estable; la validación del artefacto,
  dependencias, versión y ayuda sigue siendo una barrera posterior explícita;
- el proyecto no declara licencia propia ni NOTICE jurídico; el inventario
  técnico de terceros no sustituye la decisión del propietario (`NC-AUD-021`).

Los detalles, estados y procedimientos seguros pertenecen al informe técnico y
a [PERSISTENCE.md](PERSISTENCE.md), [RECOVERY.md](RECOVERY.md) y
[SECURITY.md](SECURITY.md). Una suite aprobada no convertiría automáticamente
estos riesgos de diseño en resueltos.
