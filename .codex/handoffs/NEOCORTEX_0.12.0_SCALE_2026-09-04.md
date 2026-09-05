# Handoff NeoCortex 0.12.0 — escala, checkpoint y verificación acotada

## Estado verificado al 5 de septiembre de 2026

- La implementación de la tranche de escala quedó en los commits funcionales
  `9d1299fe9cd51e5ef74b435d99151d2af39d3103` y
  `d5f63b841a76d83e4c556fdefd0135ead3ade0b8`. La instalación observada declara
  `source_sha=bd5a599a94f0610852dd46bf71b8933d674cb643`, mientras el baseline
  publicado de esta revisión es `4e20e57f8ffcdcb934f8da96d11b4b5fcbf25996`;
  no son el mismo árbol ejecutable aunque ambos declaren `0.12.0`.
- El manifest y el launcher identifican `0.12.0-bd5a599a94f0-cp314-linux-x86_64`.
  La auditoría detectó que el launcher exportaba un corpus temporal de pruebas,
  `/tmp/neocortex-release-corpus-012`; ese hallazgo impide tratar el smoke previo
  como prueba de configuración operativa correcta. La reparación del launcher
  y una nueva instalación son operaciones distintas.
- El árbol conserva
  `CurationWorkBudget` opcional para la verificación exacta,
  contabilidad incremental de items/archivos/bytes, deadline monotónico,
  cancelación cooperativa y razones bounded para resultados parciales.
- `curate scan` conserva cardinalidad y errores tipados, falla cerrado cuando un
  productor combina error con cobertura completa y la CLI muestra por separado
  `persisted_mode` y `observed_mode`.
- La fuente actual emite `neocortex.curation-checkpoint/v2` y conserva lectura
  de v1; permite crear, leer, validar
  y reanudar páginas mediante API/SDK, con root/source/plan/snapshot digests,
  batch digest, presupuesto acumulado, escritura no-replace y sucesores
  deterministas; no es un checkpoint DFS de inventario.
- El inventario ya implementa `neocortex.inventory-resume/v1`: orden DFS
  determinista por bytes, cursor que respeta el orden de descendientes,
  identidad de raíz y directorios abiertos, digests de prefijo/directorio/lote,
  owner externo canónico `0600`, lock no bloqueante, avance monotónico y
  reanudación que borra sólo el tail no confirmado. `InventoryWorkBudget` aplica
  límites globales de archivos/bytes, deadline y cancelación antes y después
  de cada transacción bounded.
- El benchmark opt-in completó 100,001 archivos sintéticos, 800,008 bytes,
  98 batches/commits y 14,345 archivos/s, con digest de fixture
  `1e82ea93bc55f9a5e3fa9f35561ee103d2d41bd3aa9554e0c2bb0db65d4e06ab`.
  El recibo final es
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-04-neocortex-012/benchmark-100001-d5f63b8-final.json`, SHA-256
  `c7a5091c3c8de4fbf1ae6d1157f5689b6bf0d9e0bf9231185ee4f8d109b70747`.
- La planificación de duplicados descarta un candidato que cambia durante la
  comparación exacta, evitando grupos falsos; los previews de restore leen
  grants y acciones por una única sesión SQLite fenced, también con WAL activo.

## Validación histórica del corte 0.12.0

- Foco curation/dedup e inventario: **290 passed, 6 skipped** en la ejecución
  focal de inventario, curación, deduplicación, seguridad y control, y
  **44 passed** en la regresión DFS nueva y
  las suites de generaciones/política; el foco previo de curation/dedup queda
  en **284 passed, 7 skipped, 124 subtests**.
- Incluye fixtures de 48 entradas, replay/paginación, límites, cancelación,
  lectura read-only con snapshot temporal y regresión de mutación exacta.
- Ruff, `compileall` y `git diff --check` pasaron para las superficies de ese corte.
- La colección amplia Linux (sin el módulo Windows/NTFS fuera de alcance) terminó
  con **4,651 passed, 17 failed, 76 skipped, 156 subtests**. Se atribuyeron los
  fallos a dependencias opcionales y a la distribución instalada anterior, pero
  ese resultado no acredita una suite integral verde ni descarta regresiones
  nuevas. Los receipts canónicos de la cohorte residen en
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-04-neocortex-012/`;
  no se usa la existencia de un log temporal como gate durable.
- No se ejecutó KIO real, no se invocó la Papelera del escritorio y no se tocó
  el corpus personal ni una SQLite productiva.

## Continuidad

- Verificar cada corrección posterior con herramientas individuales y fixtures
  contenidos, sin convertir Windows ni proveedores remotos en un gate Linux.
- Ante `ImmutableSQLiteUnavailable` durante rutas de `--all`, conservar el
  diagnóstico y separar los candidatos publicados de los eventos del writer;
  no relajar fences ni abrir owners productivos para observar la corrida.
- Mantener MCP sin `authorize`, `apply`, restore ni conciliación escrita, y
  mantener KIO/GUI de escritorio como gates humanos separados.
- El siguiente objetivo funcional es `NEO-EVO-004` (`0.13.0`), descrito como
  TARGET en `docs/ROADMAP_90_DAYS.md`; no sustituye la corrección de defectos
  comprobados ni prueba que el código publicado esté instalado.
