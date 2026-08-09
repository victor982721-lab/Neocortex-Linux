# Auditoría técnica integral y evolución arquitectónica de NeoCortex

> **DOCUMENTO HISTÓRICO.** Conserva evidencia del corte indicado y no describe
> el estado vigente. Consulte [README.md](../README.md), [CLI.md](CLI.md) y el
> comando `Neocortex --knowledge-status` para el producto actual.

**Fecha de corte:** 2026-07-24  
**Repositorio auditado:** `C:\Users\Neocortex`  
**Interfaz pública canónica:** `Neocortex ...`  
**Estado de este documento:** reporte final basado en la línea base y en la barrera
de validación ejecutada sobre el árbol ya integrado. Las cargas sintéticas se
identifican como tales y no se presentan como medición del corpus vivo.

## 1. Resumen ejecutivo

La auditoría recorrió el árbol de producción, pruebas, documentación, configuración de empaquetado, fachadas públicas, rutas operativas y contratos SQLite. Se ejecutaron pruebas, análisis estático, cobertura y métricas de complejidad antes de modificar. La ampliación principal es una ruta integrada `code`, no un programa paralelo: consume el inventario de deduplicación, participa en el orquestador y el estado común, conserva versiones e invalidaciones en una base separada, expone búsqueda exacta/estructural/semántica y genera reconstrucciones conceptuales sin modificar los originales.

El trabajo también corrigió defectos confirmados en transacciones ante `BaseException`, arrendamientos semánticos, propiedad de caché PDF, validación de contratos SQLite, persistencia del grafo de código y preparación/proyección de solicitudes de la GUI. Las correcciones tienen pruebas focales.

No se declara resuelto todo el riesgo sistémico. Permanecen pendientes, entre otros:

1. el aislamiento generacional del inventario de deduplicación (`files.path` sigue siendo clave global);
2. la publicación de un inventario aunque el recorrido acumule errores;
3. el avance del cursor USN cuando quedaron rutas no resueltas;
4. la publicación generacional integral de catálogos y embeddings;
5. mutaciones vinculadas a un `FileId`/handle y recuperación idempotente después de una caída;
6. el acotamiento generacional de la transacción global de `finalize_graph`;
7. un benchmark posterior con corpus real representativo y no confidencial; el
   benchmark final de esta entrega usa un corpus sintético fijo y comparable.

La conclusión técnica es, por tanto, **avance sustancial y validado por componentes**, no una afirmación de perfección ni de cobertura total de la especificación.

## 2. Alcance, metodología y reglas de evidencia

### 2.1 Alcance examinado

- Enumeración NTFS/USN y su índice de rutas.
- Inventario, identidad, hashing, deduplicación y planes.
- Orquestación, rutas PDF/DOCX/Office/audio/imagen, catálogos, revisión y acciones.
- Estado común, esquemas y migraciones SQLite.
- Indexación y búsqueda semánticas.
- CLI, fachada Python, lanzador y GUI.
- Dependencias y empaquetado en `pyproject.toml`/`constraints.txt`.
- Pruebas unitarias, de integración, regresión y contratos.
- Nueva ruta de inteligencia de código, búsquedas y reconstrucción conceptual.

### 2.2 Método

1. Inventario del repositorio y contratos públicos.
2. Línea base de pruebas, cobertura, Ruff, mypy, vulture y radon.
3. Lectura dirigida por riesgo de esquemas, transacciones, concurrencia, cachés y mutaciones.
4. Reproducciones con bases y corpus sintéticos temporales; no se reprocesó el corpus vivo.
5. Pruebas de regresión y cambios pequeños por subsistema.
6. Validaciones focales después de cada incremento.
7. Registro separado de defectos confirmados, riesgos verificables, limitaciones deliberadas, oportunidades e hipótesis.

### 2.3 Criterio de clasificación

| Clase | Significado en este reporte |
|---|---|
| Error confirmado | El código o una reproducción demuestra un comportamiento incorrecto. |
| Riesgo verificable | La secuencia peligrosa existe; falta una reproducción de daño en condiciones reales o depende de una carrera. |
| Limitación deliberada | Profundidad reducida, explícita y con degradación estable. |
| Oportunidad | Mejora razonable sin evidencia suficiente para justificar un cambio inmediato. |
| Hipótesis | Requiere medición o corpus adicional. |
| No cambiar | El reemplazo propuesto no demostró ventaja o elevaría el riesgo. |

## 3. Inventario y línea base

### 3.1 Tamaño observado antes de los cambios

- 566 archivos totales, incluidos 299 `.pyc`.
- 257 archivos `.py`.
- Producción: 171 módulos y aproximadamente 64 573 líneas.
- Pruebas: 85 módulos y aproximadamente 29 520 líneas.

Los `.pyc` se contabilizaron para entender el árbol, pero no se trataron como fuentes ni como cobertura funcional.

### 3.2 Entorno de validación vivo

| Componente | Versión observada |
|---|---:|
| Python | 3.13.14 |
| SQLite | 3.50.4 |
| pytest | 9.1.0 |
| Ruff | 0.15.17 |
| mypy | 2.1.0 |
| Coverage.py | 7.14.1 |
| vulture | 2.16 |
| radon | 6.0.1 |

La instalación declarada exige Python `>=3.13,<3.14`. Las dependencias pesadas (`faster-whisper`, NudeNet, Pillow, PyMuPDF, OCR, PySide6 y Rich) siguen en dependencias base; `fastembed` y NumPy están en el extra `semantic`.

### 3.3 Resultados basales comparables

| Validación | Resultado basal |
|---|---|
| Suite completa | `907 passed, 41 subtests passed in 97.00s` (cronómetro 99.362 s) |
| Cobertura | 23 898 sentencias; 3 580 sin cubrir; 7 044 ramas; 1 574 parciales; **81 %** total |
| Ruff, reglas predeterminadas | Limpio |
| mypy, 171 archivos de producción | Limpio en 19.114 s |
| vulture | 17 candidatos, mayoritariamente falsos positivos que requieren revisión humana |
| radon | 2 265 bloques; promedio A, 4.08 |
| Ayuda CLI, 5 procesos fríos comparables | 174.02, 150.16, 154.90, 152.02 y 147.54 ms; media **155.73 ms** |
| Importaciones durante ayuda CLI | 50 módulos |

Puntos de complejidad basales destacados: contrato de esquema SQLite D24, esquema semántico D23 y resolución de búsqueda semántica D23. Una ejecución Ruff con todas las reglas señaló 67 casos C901. Estas métricas localizaron código difícil; no se refactorizó sólo para mejorar una cifra.

### 3.4 Barrera final posterior a los cambios

| Validación | Resultado final |
|---|---|
| Suite completa | **988 passed, 56 subtests passed** en 88.89 s |
| Suite bajo Coverage.py | **988 passed, 56 subtests passed** en 117.85 s |
| Cobertura de producción | 27 072 sentencias; 4 127 sin cubrir; 8 054 ramas; 1 805 parciales; **81 %** |
| Ruff predeterminado | Limpio en todo el árbol |
| mypy | Limpio en **185 archivos de producción** |
| vulture, confianza ≥80 | 18 candidatos: 13 parámetros `exc_type` de protocolos/context managers y 5 reexportaciones de fachada semántica; no se eliminaron automáticamente |
| radon | 2 542 bloques; promedio **A, 4.1625** |
| complexipy 6.2.0 | hotspot `manifest_evidence` **103→4**; `CodeRoute.run` **48→19**; máximo final global 45 |
| Pylint R0801, mínimo 8 líneas | 16 grupos; predominan contratos/modelos y factories SQLite deliberadamente paralelos |
| Ruff diagnóstico de tamaño/ramificación | 275 avisos C901/PLR0912/PLR0913/PLR0915; informativos, no se refactorizó sólo para bajar la cifra |
| Dependencias | `pip check`: sin requisitos rotos |
| Ayuda CLI, 5 procesos | 170.53, 158.78, 165.91, 152.98 y 147.47 ms; media **159.13 ms** |

La media de ayuda final es 2.18 % mayor que la basal de 155.73 ms y sus rangos
se solapan; no hay evidencia de una regresión material de arranque. La suite
final contiene 81 pruebas y 15 subpruebas más que la basal, por lo que sus
tiempos totales no son un benchmark de rendimiento comparable.

El comando fiable en este entorno es `py -3 -m pytest`; `pytest.exe` resolvió
una copia obsoleta instalada en `site-packages` durante una verificación y no
se usó como autoridad. La primera recolección de cobertura intentó incluir un
módulo temporal eliminado por una prueba; la medición final restringió
explícitamente `--source` a los paquetes de producción y terminó correctamente.

## 4. Mapa arquitectónico actualizado

```text
Neocortex (entrada pública)
  └─ neocortex.cli / _04_Nucleo_Operativo.cli_app
      ├─ validación y traducción de configuración
      ├─ operaciones directas de sólo lectura
      ├─ watcher incremental en primer plano
      └─ FrameworkOrchestrator
          ├─ _01_Enumeracion
          │   ├─ lector NTFS/USN
          │   └─ índice de rutas MFT (auxiliar, no SSOT del pipeline)
          ├─ _02_Deduplicacion
          │   ├─ recorrido seguro y snapshots físicos
          │   ├─ fingerprints XXH3 y caché
          │   └─ inventario/checkpoint compartido
          ├─ estado común y progreso (_03_Progreso, framework*.sqlite3)
          ├─ registro perezoso de rutas
          │   ├─ PDF
          │   ├─ DOCX
          │   ├─ Office
          │   ├─ audio
          │   ├─ imagen
          │   └─ code
          │       ├─ detección de artefacto/lenguaje/codificación
          │       ├─ analizadores perezosos Python/Rust/genérico
          │       ├─ estado versionado `code.sqlite3`
          │       ├─ grafo de símbolos/proyectos/linaje
          │       ├─ búsqueda exacta, FTS, estructural e híbrida
          │       └─ reconstrucción conceptual no destructiva
          ├─ catálogo/revisión/evidencia
          ├─ índice semántico común
          └─ acciones sobre archivos, sólo con autorización explícita

_05_Interfaz
  └─ prepara solicitudes del mismo CLI, consume eventos/estado común
```

### 4.1 Límites reales de responsabilidad

- **Enumeración** descubre cambios; no debe decidir contenido.
- **Inventario/deduplicación** establece observaciones físicas y fingerprints reutilizables.
- **Rutas de contenido** leen snapshots del inventario, extraen evidencia específica y publican estado propio.
- **Catálogo/revisión** combina evidencia y registra incertidumbre; no autoriza por sí solo una mutación.
- **Búsqueda** proyecta estado vigente y proveniencia; semántica complementa señales exactas.
- **Organización/acciones** planifica por separado y exige `--apply`; la identidad debe revalidarse inmediatamente antes de mutar.
- **GUI** es cliente del contrato CLI/estado, no un segundo orquestador.

### 4.2 Contratos públicos

- Entrada instalada: `Neocortex = neocortex.cli:entrypoint`.
- Orden integrado de rutas: `pdf`, `docx`, `office`, `audio`, `image`, `code` en `_04_Nucleo_Operativo/route_selection.py`.
- Registro perezoso: `_04_Nucleo_Operativo/route_registry.py`.
- Modelos operativos: `_04_Nucleo_Operativo/models.py`.
- Contrato extensible de analizadores: `LanguageAnalyzer` en `_04_Nucleo_Operativo/code_contracts.py`.
- Fachadas de compatibilidad: paquete `neocortex` y `_04_Nucleo_Operativo/__init__.py`; deben permanecer delgadas y probar aislamiento de importaciones.

## 5. Hallazgos confirmados y riesgos

La severidad combina probabilidad, impacto sobre estado y dificultad de recuperación.

### NC-AUD-001 — Reasignación entre generaciones del inventario

- **Clase/estado:** error estructural confirmado, pendiente.
- **Severidad:** crítica.
- **Ubicación exacta:** `_02_Deduplicacion/inventory_schema.py`, DDL `files(path TEXT PRIMARY KEY COLLATE NOCASE, ..., scan_id)`; `_02_Deduplicacion/inventory_scan.py`, `FILE_UPSERT_SQL`; `_02_Deduplicacion/inventory.py`, `apply_reconciliation`.
- **Comportamiento observado:** `ON CONFLICT(path)` actualiza `scan_id`. Una nueva exploración puede apropiarse de una fila que todavía representa el checkpoint válido anterior.
- **Causa verificada:** la clave de la tabla es global por ruta, no `(scan_id, path)` ni una generación publicada mediante puntero.
- **Impacto:** una caída entre escritura y publicación, dos raíces solapadas o una ejecución concurrente pueden hacer incompleta una vista que antes era válida; también compromete reanudación y comparación entre generaciones.
- **Reproducción controlada:** crear dos scans; insertar la misma ruta en el primero y ejecutar el UPSERT con el segundo; `snapshots(scan_1)` deja de verla antes de publicar el segundo.
- **Corrección recomendada:** migración compatible a filas aisladas por generación, publicación atómica de un checkpoint/puntero activo por raíz y política explícita para raíces solapadas. Conservar generaciones previas hasta confirmar publicación y después podar por lotes.
- **Prueba requerida:** caída después de N lotes, dos raíces con intersección, reanudación, publicación atómica y lectura concurrente del checkpoint anterior.
- **Efectos secundarios:** mayor almacenamiento temporal e índices compuestos; requiere migración cuidadosa, no una alteración in-place improvisada.

### NC-AUD-002 — Un recorrido con errores se marca completado

- **Clase/estado:** error confirmado, pendiente.
- **Severidad:** alta.
- **Ubicación:** `_02_Deduplicacion/inventory_scan.py`, `InventoryScanner.scan` y `_complete_scan` (actualiza `completed_ns` y `errors`).
- **Comportamiento:** los errores de `scandir`/`stat` incrementan contadores, pero la exploración se completa y queda reutilizable.
- **Causa:** el contrato no separa `completed` de `partial/error`, aunque `errors` está almacenado.
- **Impacto:** archivos no observados pueden interpretarse como ausentes y provocar invalidaciones aguas abajo.
- **Reproducción:** fixture de directorio cuyo iterador lanza `PermissionError`; verificar `completed_ns` no nulo con `errors>0`.
- **Corrección recomendada:** estado explícito `partial`, impedir que publique una generación autoritativa y conservar el checkpoint anterior.
- **Prueba requerida:** error de enumeración, recuperación posterior y ausencia de invalidación por omisión parcial.
- **Efectos secundarios:** algunas ejecuciones que hoy parecen completas pasarán correctamente a degradadas.

### NC-AUD-003 — El cursor USN puede avanzar con rutas sin resolver

- **Clase/estado:** riesgo verificable de pérdida de eventos, pendiente.
- **Severidad:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/reconcile.py`, `_apply_reconcile_batch` y `reconcile_usn_window`.
- **Comportamiento:** se construye el checkpoint con `batch.cursor_after.next_usn`; la ruta marca `requires_rescan` para algunos casos, pero no toda resolución fallida impide avanzar.
- **Impacto:** un registro no aplicado podría quedar detrás de un cursor durable y no volver a procesarse.
- **Reproducción:** simular un registro cuyo `FileId` no pueda resolverse y persistir checkpoint; reiniciar desde `cursor_after`.
- **Corrección recomendada:** no publicar el cursor como válido si quedó cualquier identidad/ruta no resuelta; persistir evidencia y exigir rescan/reintento.
- **Prueba requerida:** resolución fallida transitoria, reinicio y posterior recuperación del evento.
- **Efectos secundarios:** más rescans controlados; es preferible a pérdida silenciosa.

### NC-AUD-004 — Rollback incompleto ante cancelaciones no derivadas de `Exception`

- **Clase/estado:** error confirmado, corregido.
- **Severidad original:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/framework_schema.py`; `_04_Nucleo_Operativo/framework_state_writer.py`.
- **Causa:** fronteras transaccionales que no garantizaban rollback/cierre para toda `BaseException`.
- **Impacto:** una interrupción podía dejar transacción o conexión abiertas y estado parcialmente inicializado.
- **Corrección:** rollback/cierre explícitos ante `BaseException`, conservando propagación.
- **Pruebas:** pruebas focales de contratos/esquema de framework y cancelación; incluidas en los 25 casos de framework informados.
- **Efectos secundarios:** ninguno funcional esperado; mejora la semántica de interrupción.

### NC-AUD-005 — Carrera de arrendamiento semántico

- **Clase/estado:** error confirmado, corregido.
- **Severidad original:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/semantic_generation_repository.py`, leasing, heartbeat, finalización y fallo de jobs.
- **Comportamiento anterior:** un worker expirado podía intentar completar o marcar fallido un trabajo ya recuperado por otro.
- **Corrección:** comprobación atómica de `status='leased'`, propietario y `lease_until_ns`; actualización acotada y rechazo si cambió el lease.
- **Prueba:** `tests/test_semantic_state.py::test_expired_worker_failure_cannot_overwrite_reclaimed_lease` y heartbeat por lote.
- **Efectos secundarios:** workers tardíos reciben error explícito en vez de sobrescribir estado vigente.

### NC-AUD-006 — Propietario huérfano de caché PDF

- **Clase/estado:** error confirmado, corregido.
- **Severidad original:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/pdf_route_storage.py`, `_delete_document_cache` y resolución de conflicto de ruta.
- **Causa:** retirar sólo la fila propietaria no limpiaba siempre el estado derivado/checkpoint asociado.
- **Impacto:** conflicto persistente o datos derivados atribuidos a una identidad obsoleta.
- **Corrección:** usar la eliminación centralizada de caché y crear el reemplazo sólo después del checkpoint recuperable correspondiente.
- **Pruebas:** regresiones de conflicto en `tests/test_cache_path_conflicts.py` y conjunto PDF informado.
- **Efectos secundarios:** más trabajo de limpieza en el conflicto, pero dentro de una transacción coherente.

### NC-AUD-007 — Preparación y proyección inconsistentes en GUI

- **Clase/estado:** errores confirmados, corregidos en su alcance.
- **Severidad original:** media-alta.
- **Ubicación:** `_05_Interfaz/run_request.py`, `worker.py`, `status_repository.py`, `main_window.py`; nueva proyección común en `issue_projection.py`.
- **Comportamiento anterior:** `route-only` podía serializar `--all`, aceptar `--apply`, fallar antes de emitir evento estructurado o mostrar resúmenes sin errores visibles.
- **Corrección:** rechazo de aplicar en `route-only`, emisión estructurada de fallo de preparación, proyección compartida y advertencias visibles.
- **Pruebas:** `tests/test_ui_run_request.py`, `test_ui_worker_shutdown.py`, `test_ui_status_repository.py`, `test_ui_smoke.py` y conjuntos UI informados.
- **Límite explícito:** la GUI aún presenta cinco rutas; ahora siempre serializa
  ese conjunto explícito y nunca `--all`/`--route all`, por lo que no inicia
  `code` silenciosamente. La ruta de código se expone por la CLI canónica.

### NC-AUD-008 — Validación SQLite dependía de texto DDL superficial

- **Clase/estado:** debilidad confirmada, corregida.
- **Severidad original:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/sqlite_schema_contract.py`, fachada correspondiente y `_04_Nucleo_Operativo/docx_schema.py`.
- **Causa:** la canonicalización previa podía ignorar diferencias semánticas o confundir comentarios, literales y opciones FTS.
- **Corrección:** lexer/canonicalizador acotado que conserva `CHECK`, `COLLATE`, columnas generadas, `AUTOINCREMENT`, `ON CONFLICT`, deferrabilidad FK, módulo/tokenizador/opciones FTS y orden contractual; límites de 2 MiB, 200 000 tokens y profundidad 256.
- **Defecto adicional descubierto por la barrera final:** el contrato exacto
  demostró que upgrades históricos de `review_candidates` y
  `review_decisions` habían omitido varios `CHECK`; no era un falso positivo
  del lexer.
- **Corrección adicional:** framework schema **v17** reconstruye únicamente las
  dos variantes legacy conocidas dentro de la transacción de migración. Valida
  columnas/orden/declaraciones/opciones/FK/índices/triggers, compara conteos y
  se abstiene con rollback ante cualquier columna desconocida, sin descartarla.
- **Pruebas:** `tests/test_sqlite_schema_contract.py`,
  `tests/test_framework_schema_contract.py` y migraciones 13/14 de
  `tests/test_review_candidates.py`; incluyen columna adicional preservada y
  rollback explícito. DOCX v2 declara compatibilidad explícita.
- **Efectos secundarios:** bases con deriva antes tolerada ahora fallan pronto y requieren migración explícita.

### NC-AUD-009 — Defectos de persistencia y coordinación de la ruta de código

- **Clase/estado:** errores confirmados durante el desarrollo, corregidos.
- **Severidad:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/code_state.py`, `code_route.py`,
  `code_contracts.py` y `route_registry.py`.
- **Correcciones aplicadas:**
  - validación opcional de caché por XXH3 de bytes cuando `cache_validation=full`;
  - `metadata` y `full` ya no generan firmas de análisis incompatibles: cambiar
    sólo la fuerza de validación conserva resultados válidos; versiones de
    analizadores sí forman parte de la firma y se comprueban al cargar;
  - una selección sólo por ruta funciona en la primera observación, mientras
    filtros de estado/diagnóstico consultan la versión vigente;
  - clave de proyecto incluye raíz, evitando fusionar homónimos dispersos;
  - conflictos ambiguos no eligen automáticamente por `mtime`;
  - el grafo vuelve a enlazar referencias cuando cambia la versión objetivo;
  - diagnósticos derivados obsoletos se retiran;
  - `mark_missing` usa lotes keyset idempotentes;
  - los tiempos acumulan nanosegundos antes de convertir a milisegundos, sin
    perder cientos de operaciones submilisegundo;
  - la ruta usa admisión global por candidato y para el grafo, con estimadores
    acotados; cache hits por metadatos no reservan recursos;
  - `CancellationRequested` se propaga y hay checkpoints durante lectura y
    antes/después del parser, en vez de persistirse como `analyzer_failure`.
- **Pruebas:** regresiones en
  `tests/test_code_state_persistence_regressions.py`,
  `tests/test_code_cache_signature.py`,
  `tests/test_code_route_selection.py`,
  `tests/test_code_global_resources.py` y
  `tests/test_code_intelligence.py` cubren estas fronteras.
- **Efectos secundarios:** `full` relee bytes para verificar caché; es una decisión explícita de mayor integridad con costo de I/O.

### NC-AUD-010 — Ventana TOCTOU en mutaciones por ruta

- **Clase/estado:** riesgo verificable, pendiente.
- **Severidad:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/actions.py`, `_execute_trash_operation` (`send2trash(paths)`), `_retry_trash_candidate` y `_rename_mismatch` (`source.rename(target)`); ruta de aplicación de organización documental.
- **Comportamiento:** existe una revalidación fuerte e inmediatamente cercana, pero la API muta por nombre después de cerrar/soltar la observación; otro proceso aún puede sustituir la entrada entre validación y llamada.
- **Impacto:** actuar sobre un archivo distinto o reemplazar un destino bajo carrera.
- **Corrección recomendada:** primitivas Windows vinculadas a handle/FileId, comprobación de directorio padre y operación no-replace; registrar la identidad efectivamente mutada.
- **Prueba requerida:** carrera determinista que intercambie la entrada entre preflight y syscall; destino existente en Windows y POSIX.
- **Efectos secundarios:** implementación específica de plataforma detrás de una interfaz; mantener simulación y fallback que se abstenga.

### NC-AUD-011 — Recuperación de acciones no reconcilia el efecto real

- **Clase/estado:** error de recuperación confirmado por lectura, pendiente.
- **Severidad:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/framework_state_writer.py::mark_abandoned_actions`; `actions.py` separa `begin_file_action`, syscall y `finish_file_action`.
- **Comportamiento:** un crash después de mover/enviar a papelera pero antes de `finish` deja `started`; el siguiente arranque lo cambia a `failed` sin comprobar si el efecto ocurrió.
- **Impacto:** bitácora falsa, reintento no idempotente o pérdida de trazabilidad.
- **Reproducción:** inyectar terminación entre syscall y finalización y reiniciar.
- **Corrección recomendada:** estados `planned/applying/applied/recovery_required`, identificador idempotente, identidad pre/post y rutina de conciliación que no repita una mutación incierta.
- **Prueba requerida:** fault injection en cada frontera y recuperación de rename/trash.
- **Efectos secundarios:** esquema y máquina de estados más complejos; no debe implantarse sin migración.

### NC-AUD-012 — Visibilidad semántica no filtrada por generación publicada

- **Clase/estado:** riesgo verificable, pendiente.
- **Severidad:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/semantic_generation_repository.py` (persistencia por job), `semantic_search_repository.py` (filtra elementos/chunks activos, no estado de la generación).
- **Comportamiento:** los embeddings se vuelven consultables por fila antes de una publicación atómica de toda la generación.
- **Impacto:** búsqueda mezcla resultados completos y parciales después de interrupción.
- **Corrección recomendada:** puntero de generación publicada o condición de búsqueda contra una generación `completed`; conservar la actual hasta completar la siguiente.
- **Prueba requerida:** interrumpir una generación después de algunos jobs y comparar búsqueda durante/tras recuperación.
- **Efectos secundarios:** puede aumentar almacenamiento mientras coexisten dos generaciones.

### NC-AUD-013 — Catálogo documental actualiza filas visibles antes de terminar el run

- **Clase/estado:** riesgo verificable, pendiente.
- **Severidad:** alta.
- **Ubicación:** `_04_Nucleo_Operativo/document_catalog.py`, `catalog_runs`, UPSERT de `documents` y manejo de fallo.
- **Comportamiento:** el run puede quedar `failed`, pero filas ya actualizadas permanecen como vista corriente.
- **Impacto:** consumidores observan un catálogo mixto entre ejecuciones.
- **Corrección recomendada:** generación de catálogo y publicación atómica, o versión por documento con puntero vigente.
- **Prueba requerida:** fallo a mitad del lote y lectura concurrente.
- **Efectos secundarios:** migración de consultas y retención temporal de versiones.

### NC-AUD-014 — Trabajo/estado no acotado en rutas existentes

- **Clase/estado:** debilidades de rendimiento confirmadas por código, pendientes.
- **Severidad:** media.
- **Ubicación:** poda de estado obsoleto en `_04_Nucleo_Operativo/audio_state.py` y `office_state.py`; tabla `scans` de `_02_Deduplicacion/inventory_schema.py`.
- **Comportamiento:** algunas podas materializan el conjunto de claves; `scans` no tiene política completa de retención.
- **Impacto:** memoria y base crecen con corpus/historial.
- **Corrección recomendada:** keyset por lotes, límites observables y retención generacional conservadora.
- **Prueba requerida:** decenas de miles de fixtures con máximo de memoria y tamaño de transacción.
- **Efectos secundarios:** poda tarda más rondas, pero mantiene memoria acotada.

### NC-AUD-015 — Publicación del grafo de código en una transacción global

- **Clase/estado:** riesgo de escalabilidad, pendiente.
- **Severidad:** media-alta.
- **Ubicación:** `_04_Nucleo_Operativo/code_state.py::finalize_graph`.
- **Comportamiento:** asignación de proyectos, resolución, duplicados, conflictos, aristas y diagnósticos se ejecutan bajo una sola transacción.
- **Impacto:** bloqueo WAL y rollback costoso en corpus grande.
- **Corrección recomendada:** construir una generación de grafo en lotes reanudables y publicar un puntero al finalizar; no fraccionar la transacción actual sin esa semántica.
- **Prueba requerida:** interrupción por fase, lectores concurrentes y publicación completa.
- **Efectos secundarios:** tablas/generation_id adicionales.

### NC-AUD-016 — Importaciones pesadas en rutas ligeras

- **Clase/estado:** oportunidad medida, pendiente.
- **Severidad:** media.
- **Ubicación:** `_03_Progreso/reporters.py` importa Rich en módulo; componentes de imagen/semántica importan Pillow; dependencias base en `pyproject.toml`.
- **Evidencia:** carga de progreso Rich medida alrededor de 264 ms/5.2 MiB y servicio semántico alrededor de 792 ms/~9.9 MiB en la auditoría; ayuda CLI fría media 155.73 ms con 50 módulos.
- **Corrección recomendada:** imports perezosos por operación y considerar extras de capacidad sólo tras medir instalación/compatibilidad.
- **Prueba requerida:** benchmark frío/caliente con misma máquina, entorno y caché; prueba de aislamiento de `--help`.
- **No cambiar aún:** no mover dependencias entre extras sin verificar instalación reproducible y comandos canónicos.

### NC-AUD-017 — Integridad referencial desigual en estado común

- **Clase/estado:** riesgo verificable, pendiente.
- **Severidad:** media-alta.
- **Ubicación:** `_04_Nucleo_Operativo/framework_schema.py` y conexiones de rutas que no activan uniformemente `PRAGMA foreign_keys=ON`.
- **Impacto:** filas huérfanas o estados imposibles no rechazados por SQLite.
- **Corrección recomendada:** auditoría tabla por tabla, migración aditiva/rebuild controlado y activación uniforme de FK por factory de conexión.
- **Prueba requerida:** inserciones inválidas por cada conexión pública y `PRAGMA foreign_key_check`.
- **Efectos secundarios:** datos heredados incompatibles deben diagnosticarse, no borrarse.

## 6. Cambios que deliberadamente no se realizaron

- No se reemplazó el inventario canónico por el índice MFT auxiliar: no hay evidencia de equivalencia funcional ni de recuperación.
- No se reescribieron masivamente rutas maduras para reducir C901.
- No se añadió ANN como autoridad: faltan un corpus etiquetado y una comparación de recall/latencia.
- No se ejecutaron `cargo check`, Clippy, LSP ni compiladores sobre código indexado sin una política explícita de confianza, tiempo y sandbox.
- No se materializó una reconstrucción en disco ni se movió archivo alguno.
- No se reprocesó el corpus vivo para “demostrar” la ruta nueva.
- No se automatizó eliminación, cuarentena, rename ni canonicalización de versiones.

## 7. Diseño e implementación de la ruta `code`

### 7.1 Integración

`code` está registrado como sexta ruta integrada. Consume
`DedupIndex.snapshots(scan_id)`, eventos de progreso, cancelación, estado de
ejecución y el coordinador global de CPU/memoria. Su base es `code.sqlite3`
dentro del directorio de estado. Los analizadores opcionales se resuelven
perezosamente y un fallo de parser degrada a texto buscable.

Flujo:

1. seleccionar candidatos plausibles por nombre/extensión;
2. aplicar filtros de selección/reintento;
3. validar caché por metadatos o bytes (`full`);
4. rechazar enlaces, reparse points y no regulares;
5. leer una vez con límite y comprobar `fstat` antes/después;
6. detectar binario, codificación, lenguaje y rol del artefacto;
7. admitir recursos acotados y analizar con adaptador de lenguaje o fallback;
8. publicar una versión y su estructura en una transacción;
9. admitir por separado y resolver proyectos, referencias, duplicados y diagnósticos derivados;
10. invalidar ausentes sólo tras un recorrido completo, sin límite ni selección.

### 7.2 Clasificación de artefactos

`_04_Nucleo_Operativo/code_detection.py` distingue:

- código fuente y scripts;
- configuración y manifests;
- datos estructurados;
- plantillas;
- documentación y texto plano;
- generado;
- vendorizado;
- binario/no analizable.

La evidencia puede provenir de extensión, nombre, shebang, ruta, encabezado generado y contenido. Se conservan confianza y evidencia; la clasificación no modifica el archivo.

### 7.3 Interfaz extensible de analizadores

`LanguageAnalyzer` define `analyzer_id`, `analyzer_version`, conjunto de lenguajes y `analyze(CodeFileInput, CodeRouteConfig) -> CodeAnalysis`. `AnalyzerRegistry` es perezoso y permite registrar adaptadores sin cargar herramientas hasta seleccionarlas.

Implementaciones actuales:

- **Python:** `ast` estándar cuando es válido; símbolos, firmas, parámetros/tipos, docstrings, decoradores, imports, llamadas, raises/catches, variables, herencia, entrypoint, rangos de líneas/bytes y complejidad. `SyntaxError` conserva línea/columna/contexto; el texto sigue indexable.
- **Rust:** analizador léxico acotado para módulos, structs, enums, traits, impls, funciones, macros, `use` y llamadas. Marca las inferencias como no confirmadas y registra `parser_kind='rust-lexical-fallback'`; no afirma haber ejecutado Cargo.
- **Genérico:** definiciones/imports comunes, diagnósticos JSON/TOML, bloques de código Markdown y fragmentación de texto.

### 7.4 Límites de recursos

Valores predeterminados del contrato:

- 8 MiB por archivo;
- 4 000 000 de caracteres buscables;
- chunks de 12 000 caracteres;
- máximo de documentos opcional;
- lecturas de hasta 1 MiB por iteración;
- transacciones por versión;
- invalidación de ausentes en lotes keyset de 512.

La reserva de análisis estima `4 MiB + 2×bytes + 12×caracteres_acotados`; la
del grafo usa `8 MiB + min(DB+WAL+SHM, 64 MiB)`. Son estimaciones de admisión,
no afirmaciones de RSS exacto. El AST estándar de Python no puede cancelarse a
mitad de una llamada; el tamaño del archivo lo acota y se comprueba cancelación
inmediatamente antes y después.

Los límites forman parte de la firma de procesamiento. Archivos grandes, binarios o fallidos conservan una observación/diagnóstico versionado, no desaparecen silenciosamente.

## 8. Modelo de datos de código y migraciones

### 8.1 Esquema actual

`_04_Nucleo_Operativo/code_schema.py` declara `CODE_SCHEMA_VERSION = 2`, contrato exacto y FK activas.

| Tabla/índice | Propósito |
|---|---|
| `metadata`, `schema_migrations` | versión canónica e historial monotónico |
| `analysis_runs` | estado, firma, contadores y error de cada ejecución |
| `files` | identidad física, ruta vigente y versión actual |
| `file_versions` | observación, fingerprints raw/texto/normalizado/tokens/estructura, analizador, parser, proveniencia e invalidación |
| `invalidation_history` | causa, reemplazo y evidencia |
| `symbols` | clases, funciones, métodos, variables y rangos |
| `code_references` | definiciones/referencias/llamadas/herencia/implements con confianza |
| `dependencies` | imports, manifests y resolución probable |
| `diagnostics` | parser/linter/grafo, herramienta, versión, severidad y vigencia por versión |
| `metrics` | complejidad y métricas con proveniencia |
| `code_chunks` | texto buscable acotado y relación con símbolo |
| `code_fts` | FTS5 sobre ruta, proyecto, símbolo, firma y cuerpo |
| `version_relations` | exacto, normalizado, tokens, estructura, predecesor y divergencia de nombre |
| `projects` | instancia probable de proyecto/ecosistema/raíz/confianza |
| `project_memberships` | pertenencia, ruta propuesta, selección, conflicto y evidencia |
| `project_edges` | dependencias entre proyectos |
| `embedding_links` | vínculo opcional con espacio/modelo/generación semánticos |
| `external_tool_runs` | cargo/linter/compilador externo y configuración, incluso indisponibilidad/timeout |

Índices cubren ruta, lenguaje, hashes, símbolo/nombre calificado, complejidad, referencias, dependencias, diagnóstico, proyecto, membresía y embeddings activos. La identidad byte a byte usa XXH3 no criptográfico; la normalizada es una relación distinta y nunca reemplaza la identidad original.

### 8.2 Política de migración

- Creación y migración ocurren bajo `BEGIN IMMEDIATE`.
- Toda `BaseException` provoca rollback.
- v1 crea versiones, estructura, diagnósticos, chunks/FTS y relaciones.
- v2 añade proyectos, membresías, aristas, enlaces semánticos y ejecuciones externas.
- `metadata.schema_version`, `PRAGMA user_version` e historial deben concordar.
- El contrato exacto se valida antes del commit.
- Migraciones desconocidas o historia incompleta fallan de forma explícita.

La migración es aditiva y conserva evidencia. No existe downgrade destructivo automático.

## 9. Búsqueda integral de código

`_04_Nucleo_Operativo/code_search.py` ofrece modos:

`literal`, `fts`, `path`, `language`, `symbol`, `definition`, `reference`, `import`, `dependency`, `call`, `signature`, `diagnostic`, `complexity`, `semantic` e `hybrid`.

La búsqueda híbrida usa fusión por rango recíproco ponderada. La señal semántica sólo aparece si existe `semantic.sqlite3`, el modelo está disponible y hay chunks `code` indexados; nunca se fabrica a partir de coincidencias léxicas. Cada resultado devuelve ruta, proyecto probable, lenguaje, artefacto, símbolo/firma, líneas, snippet, tipos de match, evidencia, `version_id`, tamaño/mtime observados y estado del análisis.

Ejemplos canónicos:

```powershell
# Crear/actualizar la inteligencia de código con operación incremental.
Neocortex --root C:\Codigo --route code

# Verificación más fuerte de caché cuando los metadatos pueden preservarse.
Neocortex --root C:\Codigo --route code --code-cache-validation full

# Búsqueda híbrida explicada.
Neocortex --code-search "dónde se valida el acceso a SQLite" `
  --code-search-mode hybrid --code-json

# Exacta/estructural con filtros.
Neocortex --code-search "connect" --code-search-mode definition `
  --code-language python --code-min-complexity 10

# Diagnóstico y disponibilidad, sin analizar archivos.
Neocortex --code-status --code-json
Neocortex --code-doctor

# Construir embeddings de los chunks de código en el índice común.
Neocortex --semantic-index text --semantic-source code
```

## 10. Reconstrucción conceptual de proyectos dispersos

La reconstrucción en `_04_Nucleo_Operativo/code_projects.py` es de sólo lectura. Agrupa manifiestos, raíces, imports/dependencias, nombres y relaciones versionadas. Nombres de proyecto ambiguos obligan a indicar un ID. No crea directorios, no copia, no mueve y no sobrescribe.

Estrategias:

- `latest`: versión observada más reciente por ruta propuesta;
- `coherent`: variante con mayor evidencia de pertenencia/coherencia;
- `branches`: conserva todas las variantes y marca conflictos.

Ejemplo sobre un fixture conceptual (no sobre el corpus vivo):

```powershell
Neocortex --code-projects --code-json
Neocortex --code-reconstruct 17 `
  --code-reconstruct-strategy branches --code-json
```

Salida conceptual esperada:

```json
{
  "kind": "code-reconstruction",
  "project_id": 17,
  "project_name": "framework-x",
  "strategy": "branches",
  "entries": [
    {
      "proposed_path": "src/core.py",
      "source_path": "D:/disperso/copia-a/core.py",
      "version_id": 101,
      "xxh3_128": "HUELLA_XXH3_128_A",
      "relation": "under_manifest_root",
      "confidence": 0.93,
      "selected": false,
      "conflict_group": "project-17-path-1"
    },
    {
      "proposed_path": "src/core.py",
      "source_path": "E:/backup/copia-b/core.py",
      "version_id": 144,
      "xxh3_128": "HUELLA_XXH3_128_B",
      "relation": "inferred_root",
      "confidence": 0.71,
      "selected": false,
      "conflict_group": "project-17-path-1"
    }
  ]
}
```

En estrategia `branches`, `selected=false` es deliberado: el sistema conserva
todas las variantes y no elige una canónica sin otra instrucción. `latest` y
`coherent` marcan una elección por ruta propuesta y explican conflictos.

Para una restauración futura en una ubicación nueva se requiere otra operación explícita, autorización, validación de destinos y un manifiesto materializado con origen, fingerprint, versión y criterio. Esa operación no forma parte del cambio actual.

## 11. Observabilidad

La ruta registra:

- candidatos, procesados, cache hits, texto parcial, límites y binarios;
- generado/vendorizado;
- inventario obsoleto;
- símbolos, referencias, diagnósticos y proyectos;
- versiones invalidadas y errores;
- bytes/caracteres leídos;
- milisegundos de lectura, análisis, persistencia y grafo;
- firma de procesamiento, analizador/parser/versiones y proveniencia.

`--code-status`, `--code-doctor` y las salidas JSON permiten inspeccionar bases/FTS/analizadores sin abrir SQLite manualmente. Falta una exportación unificada Prometheus/OTel; es una oportunidad, no un requisito para operar la ruta actual.

## 12. Pruebas añadidas y cobertura por riesgo

### 12.1 Nuevos conjuntos

- `tests/test_code_intelligence.py`: roles textuales, codificaciones, incrementalidad, búsqueda, no modificación, cache `full`, fallback, binarios/límites, proyectos dispersos y esquema.
- `tests/test_code_state_persistence_regressions.py`: historial de reintentos, cambio con metadatos conservados, grafo, diagnósticos obsoletos, homónimos/conflictos y lotes idempotentes.
- `tests/test_code_cli.py`: traducción sin imports ansiosos, combinaciones inválidas, operaciones directas sin crear estado y fuente semántica `code`.
- `tests/test_code_schema_migration_v1_v2.py`: migración poblada, FTS/FK,
  idempotencia y rollback ante DDL fallido.
- `tests/test_code_cache_signature.py`, `test_code_route_selection.py` y
  `test_code_global_resources.py`: compatibilidad de caché, filtros iniciales,
  admisión/liberación y cancelación.
- `tests/test_code_semantic_search.py`: resolución semántica opcional y filtros
  sin descargar modelos.
- `tests/test_code_manifest_evidence.py`: Python/Cargo/Node y configuración
  inválida tras dividir el hotspot cognitivo.
- `tests/test_cli_version.py`: contrato público `Neocortex --version`.

### 12.2 Regresiones ampliadas

- contrato SQLite y compatibilidad DOCX;
- rollback/cierre del framework;
- leases semánticos;
- conflicto de caché PDF;
- aislamiento del registro de rutas y operaciones CLI;
- preparación, worker, estado y visualización de GUI.

### 12.3 Cobertura pendiente por riesgo

Aún deben añadirse o ampliarse:

- archivos realmente enormes con aserción de memoria/CPU, no sólo límite funcional;
- interrupción durante `finalize_graph` y futura publicación generacional;
- carrera TOCTOU determinista de acciones;
- fallo parcial de inventario y protección del checkpoint anterior;
- USN no resuelto y reanudación;
- Rust con parser nativo/Tree-sitter y Cargo simulado;
- proyecto comprimido y variantes históricas incompatibles;
- benchmark con corpus real representativo, no confidencial y etiquetado.

## 13. Rendimiento y consumo de recursos

### 13.1 Benchmark sintético final comparable

Se ejecutó sobre un corpus fijo de **360 archivos / 49 189 bytes**: 120 Python,
120 Rust, 60 JSON y 60 Markdown. Todos los escenarios usaron el mismo árbol,
intérprete, límites y máquina; las bases fueron temporales y no se tocó el
corpus vivo. `tracemalloc` mide asignaciones Python, no RSS total del proceso.

| Escenario | Pared (ms) | CPU (ms) | Pico Python (bytes) | Hits | Procesados | Invalidados | Bytes de contenido contabilizados |
|---|---:|---:|---:|---:|---:|---:|---:|
| Frío, política `metadata` | 2 722.645 | 1 921.875 | 1 059 226 | 0 | 360 | 0 | no separado |
| Caliente, sin cambios | 444.416 | 406.250 | 662 155 | 360 | 0 | 0 | 0 |
| Cambio de política después de la corrección | 567.747 | 515.625 | 267 622 | 360 | 0 | 0 | 49 150 |
| Firma legacy incompatible, reproducida en clon | 1 647.115 | 1 562.500 | 446 473 | 0 | 360 | 360 | no separado |
| Un archivo cambiado | 435.018 | 359.375 | 350 379 | 359 | 1 | 1 | 266 |

El cuarto escenario no es una ejecución de un build anterior: reproduce de
forma exacta, en una copia de la base, el efecto de la firma incompatible que
el defecto producía. Frente a ese efecto, cambiar sólo la política de
validación después de la corrección reduce pared **65.5 %**, CPU **67.0 %** y
el pico de asignaciones Python **40.1 %**, y evita 360 reprocesos,
invalidaciones y escrituras de resultados. La pasada de un cambio confirma la
granularidad incremental: 359 hits y sólo 266 bytes del candidato modificado.

La ayuda CLI final promedió **159.13 ms** frente a **155.73 ms** basal
(+2.18 %, rangos solapados), por lo que no hay evidencia de una regresión
material de arranque. No se comparan tiempos de las suites porque la final
contiene más pruebas.

### 13.2 Medición aún requerida

Falta repetir frío/caliente/incremental con un corpus real representativo,
etiquetado y no confidencial, incluyendo RSS pico del proceso, I/O del sistema,
WAL, percentiles por tamaño/lenguaje y equivalencia exacta de resultados. La
medición sintética demuestra la regresión corregida y el comportamiento
incremental; no demuestra el costo ni la precisión sobre el corpus operativo.

## 14. Limitaciones restantes y prioridades

### Prioridad P0 — integridad antes de ampliar capacidad

1. Aislar/publicar generaciones del inventario (NC-AUD-001).
2. No publicar inventarios parciales (NC-AUD-002).
3. No avanzar USN con resolución incompleta (NC-AUD-003).
4. Recuperación idempotente y binding a identidad de mutaciones (NC-AUD-010/011).

### Prioridad P1 — vistas consistentes y escalabilidad

1. Publicación generacional semántica y documental (NC-AUD-012/013).
2. Generación/lotes para el grafo de código (NC-AUD-015).
3. FK/checks uniformes y migración preservadora (NC-AUD-017).
4. Benchmark representativo real y no confidencial; la barrera sintética y la
   suite completa ya se ejecutaron.

### Prioridad P2 — profundidad de análisis

1. Parser Rust real y adaptador Tree-sitter/LSP versionado.
2. Resolución interarchivo más precisa para Python y Rust.
3. Ejecución externa controlada con timeout, límites, trust y proveniencia.
4. Calibración de agrupación/abstención con proyectos etiquetados.
5. Optimizar imports pesados sólo tras medición comparable.

### Limitaciones deliberadas actuales

- Rust es lexical y no confirmado; Cargo/Clippy no se ejecutan.
- Otros lenguajes degradan a genérico/texto sin perder búsqueda.
- La búsqueda semántica requiere índice/modelo existente; exacta y estructural siguen disponibles.
- La reconstrucción es conceptual exclusivamente.
- Los proyectos dentro de archivos comprimidos no se extraen ni reconstruyen;
  sólo puede analizarse el contenido que el inventario exponga como archivo.
- La GUI mantiene deliberadamente cinco rutas visibles; `code` se opera por la
  CLI canónica y nunca se activa de forma implícita desde `--all` de la GUI.
- El modo de caché predeterminado por metadatos favorece I/O bajo; `full` existe cuando se exige detectar cambios con metadatos preservados.
- Los archivos fuera de límites conservan diagnóstico, no AST completo.
- `ast.parse` de Python no admite cancelación cooperativa durante la llamada;
  tamaño, admisión y checkpoints antes/después acotan la exposición.

## 15. Recuperación y rollback no destructivo

1. Detener sólo la ejecución propia de NeoCortex y confirmar PID/línea de comandos; no terminar procesos por coincidencia de nombre.
2. Antes de intervenir una base en uso, crear una copia consistente con la API de backup de SQLite; copiar sólo el `.sqlite3` sin coordinar WAL/SHM no es un respaldo válido.
3. Para rollback de código, restaurar las copias validadas de los archivos cambiados como un conjunto compatible.
4. `code.sqlite3` está separado y su esquema vigente es v2: una versión anterior
   puede ignorarlo. No borrarlo; preservarlo para auditoría o restaurarlo desde
   backup si se revierte el esquema.
5. El esquema común vigente incluye la migración preservadora v17. No intentar
   bajar `schema_version`/`user_version` de ninguna base manualmente: no existe
   migración inversa y debe restaurarse una copia coherente previa a la
   migración.
6. Validar después de restaurar: contrato de esquema, `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, comandos `Neocortex --code-status/--code-doctor` y pruebas focales.
7. Si la ejecución se interrumpió durante una mutación, no repetirla automáticamente: inspeccionar `file_actions`, identidad/ruta real y marcar recuperación explícita.
8. No reprocesar el corpus vivo como parte del rollback salvo autorización; primero validar con bases/fixtures temporales.

## 16. Manifiesto lógico de archivos modificados

Este repositorio no usa Git. El manifiesto enumera archivos confirmados por los incrementos de esta auditoría; no pretende reconstruir historial anterior por timestamps.

### 16.1 Nuevos archivos de producción

- `_04_Nucleo_Operativo/code_contracts.py`
- `_04_Nucleo_Operativo/code_detection.py`
- `_04_Nucleo_Operativo/code_analyzer_common.py`
- `_04_Nucleo_Operativo/code_analyzers.py`
- `_04_Nucleo_Operativo/code_python.py`
- `_04_Nucleo_Operativo/code_rust.py`
- `_04_Nucleo_Operativo/code_generic.py`
- `_04_Nucleo_Operativo/code_schema.py`
- `_04_Nucleo_Operativo/code_state.py`
- `_04_Nucleo_Operativo/code_route.py`
- `_04_Nucleo_Operativo/code_search.py`
- `_04_Nucleo_Operativo/code_projects.py`
- `_04_Nucleo_Operativo/cli_code.py`
- `_05_Interfaz/issue_projection.py`

### 16.2 Nuevas pruebas

- `tests/test_code_intelligence.py`
- `tests/test_code_state_persistence_regressions.py`
- `tests/test_code_cli.py`
- `tests/test_code_schema_migration_v1_v2.py`
- `tests/test_code_cache_signature.py`
- `tests/test_code_route_selection.py`
- `tests/test_code_semantic_search.py`
- `tests/test_code_manifest_evidence.py`
- `tests/test_code_global_resources.py`
- `tests/test_cli_version.py`

### 16.3 Archivos existentes modificados

Integración de ruta/CLI/semántica:

- `_04_Nucleo_Operativo/__init__.py`
- `_04_Nucleo_Operativo/models.py`
- `_04_Nucleo_Operativo/route_selection.py`
- `_04_Nucleo_Operativo/route_registry.py`
- `_04_Nucleo_Operativo/orchestrator.py`
- `_04_Nucleo_Operativo/cli_parser.py`
- `_04_Nucleo_Operativo/cli_operations.py`
- `_04_Nucleo_Operativo/cli_config.py`
- `_04_Nucleo_Operativo/cli_validation.py`
- `_04_Nucleo_Operativo/cli_reporting.py`
- `_04_Nucleo_Operativo/cli_direct.py`
- `_04_Nucleo_Operativo/semantic_sources.py`
- `neocortex/__init__.py`

Correcciones de núcleo/esquemas:

- `_04_Nucleo_Operativo/framework_schema.py`
- `_04_Nucleo_Operativo/framework_state_writer.py`
- `_04_Nucleo_Operativo/semantic_generation_repository.py`
- `_04_Nucleo_Operativo/pdf_route_storage.py`
- `_04_Nucleo_Operativo/sqlite_schema_contract.py`
- `_04_Nucleo_Operativo/docx_schema.py`
- `neocortex/sqlite_schema_contract.py`

GUI:

- `_05_Interfaz/run_request.py`
- `_05_Interfaz/worker.py`
- `_05_Interfaz/status_repository.py`
- `_05_Interfaz/main_window.py`

Pruebas existentes ampliadas:

- `tests/test_route_registry_isolation.py`
- `tests/test_cli_operations_registry.py`
- `tests/test_sqlite_schema_contract.py`
- `tests/test_framework_schema_contract.py`
- `tests/test_semantic_state.py`
- `tests/test_cache_path_conflicts.py`
- `tests/test_ui_run_request.py`
- `tests/test_ui_worker_shutdown.py`
- `tests/test_ui_status_repository.py`
- `tests/test_ui_smoke.py`
- `tests/test_lazy_package_api.py`
- `tests/test_semantic_service_facade.py`
- `tests/test_packaging_entrypoint.py`
- `tests/test_cli_semantic.py`
- `tests/test_t_framework.py`
- `tests/test_review_candidates.py`

Documentación existente actualizada:

- `README.md`
- `_04_Nucleo_Operativo/README.md`

Documentación creada por esta auditoría:

- `docs/TECHNICAL_AUDIT_2026-07-24.md`

## 17. Cierre de la barrera de esta entrega

Sobre el árbol integrado se cumplieron la suite completa, Ruff, mypy,
cobertura, migración poblada v1→v2, migraciones legacy del framework, comandos
canónicos, análisis no modificador y benchmark sintético comparable. El doctor
se verificó contra un estado temporal inexistente y no creó artefactos; la
versión pública es `0.4.0`.

Esto cierra la **barrera técnica de esta entrega**, no los riesgos sistémicos
P0/P1: inventario generacional, publicación parcial, cursor USN y recuperación
de mutaciones siguen explícitamente pendientes. Tampoco sustituye el benchmark
real representativo indicado en 13.2.

---

**Conclusión:** NeoCortex dispone ahora de una base coherente para tratar código como información estructurada relacionada con documentos y semántica, conservando búsqueda textual ante degradación. La nueva capacidad es incremental, versionada, explicable y no destructiva. La auditoría también confirmó que los riesgos más graves restantes están en publicación generacional y mutaciones recuperables; deben resolverse antes de considerar que el framework ofrece integridad completa bajo fallos y concurrencia.
