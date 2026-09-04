# Roadmap de NeoCortex

> Actualizado el 4 de septiembre de 2026. Un estado aquí no sustituye código,
> pruebas ni una release instalada desde el SHA final.

## Convención de estado

- **CURRENT:** frontera que rige el producto ahora.
- **IMPLEMENTED:** presente en el checkout y cubierto por pruebas focales, pero
  pendiente de promoción desde el SHA final cuando corresponda.
- **TARGET:** todavía no implementado.

## Resultado buscado

Víctor debe poder convertir una raíz caótica en inventario, comprensión,
relaciones, plan revisable, efectos autorizados y verificación sin encargar un
script diferente por etapa.

La seguridad se mide por separación de efectos, identidad, revalidación,
Papelera reversible y recovery. Abstenerse es correcto ante una precondición
incierta, pero no cuenta como funcionalidad entregada para los casos soportados.

## Línea base 0.9.0

| Capacidad | Estado |
|---|---|
| Inventario e identidad Linux | Implementado; generaciones, snapshots y conciliación de scans abandonados |
| Extracción multimodal | Implementada con cobertura desigual por formato |
| Deduplicación | Planificación implementada; la prueba exacta y disposición pública necesitan un contrato uniforme |
| Catálogo y organización | Planes disponibles; recorrido end-to-end parcial |
| Knowledge y contexto para agentes | Implementado read-only; cobertura/localizadores varían por owner |
| Review | **IMPLEMENTED:** `curate review` publica ReviewTasks y `curate decide` añade decisiones humanas por CAS |
| Curación integrada | **CURRENT:** plan read-only; **IMPLEMENTED:** review/decide state-only; **TARGET:** autorización y efectos |
| Mutación Linux | Foundation KIO preparada; no integrada/promovida, y `--apply`/`--organization-apply` se abstienen |
| Backup/restore/purge | Implementados mediante `Neocortex databases` |
| MCP | **IMPLEMENTED:** `curation_plan`, `curation_review` y `curation_decide`; los dos últimos escriben ReviewTask advisory, no corpus |

## 0.10.0 — Evidencia y plan de curación

**Resultado:** una persona o agente puede inspeccionar, paginar y revisar un plan
completo sin mutar el corpus.

**IMPLEMENTED en el checkout:** `curate plan` consulta el digest paginado;
`curate review` publica páginas idempotentes como ReviewTask y `curate decide`
registra `resolved`/`dismissed` mediante digest y event-head CAS. API, SDK y MCP
proyectan los mismos envelopes. Review/decide escriben sólo Framework,
mantienen `actions_authorized=false` y crean cero `file_actions`.

Entregas restantes:

1. completar `curate scan` y `curate verify` alrededor del plan/review ya
   implementado;
2. proyección común de tipo real, procedencia, valor, duplicado, versión,
   similitud, disposición y evidencia;
3. `verification_mode` explícito; ningún candidato fast se publica como
   duplicado bytewise;
4. ampliar el plan inmutable ya paginado con source heads y reason codes;
5. añadir consultas MCP de status/verificación sin convertir review/decide en
   autorización;
6. límites uniformes de elementos, tiempo, RAM y disco, con progreso y
   cancelación;
7. localizadores públicos comprobables por cada capacidad declarada.

Criterios de aceptación:

- fixture heterogéneo de 20–50 elementos recorre plan, review y decide con
  paginación/replay, y después incorpora scan/verify;
- segunda corrida no rehace trabajo compatible;
- cada propuesta enlaza evidencia y explica incertidumbre;
- igualdad exacta exige comparación byte a byte;
- CLI, SDK, GUI y MCP proyectan el mismo schema;
- cero cambios en bytes/rutas del corpus, cero `file_actions` y cero autoridad
  derivada de una decisión.

No se añadirá exportación ni ZIP de curación en este corte. JSON/JSONL son
respuestas de interfaz, no artefactos de entrega.

## 0.11.0 — TARGET: efectos Linux reversibles

**Resultado:** un plan aprobado puede mover, renombrar o enviar a Papelera un
lote pequeño y luego demostrar o recuperar el efecto.

Decisión de backend:

- reutilizar `neocortex.safety.kio_trash`, ya preparado pero no promovido ni
  validado contra KIO real;
- Papelera KDE mediante el primer cliente disponible entre `kioclient6`,
  `kioclient5` y `kioclient`, con `move <origen> trash:/`;
- preflight de identidad y revalidación para compensar la resolución path-bound;
- rename POSIX no-replace separado del backend de Papelera;
- ningún fallback a `gio trash`, `unlink`, borrado directo o copia+delete;
- timeout o efecto ambiguo dejan recovery pendiente, sin reintento automático.

Entregas:

1. ledger `plan → decision → authorization → attempt → effect → verification`;
2. autorización ligada a actor, scope, expiración, límites, source heads y
   digest;
3. revalidación de identidad, tamaño, mtime y hash junto a la frontera;
4. creación segura y fsync de `.trashinfo`;
5. receipt que permita localizar y restaurar aunque cambie el nombre interno;
6. conciliación de cada punto de caída y estados `recovery_required`;
7. lotes pequeños con límite de acciones/bytes y cancelación entre efectos;
8. GUI que presenta el plan, pero no aporta una autoridad distinta.

Criterios de aceptación:

- mismo filesystem aprobado; `EXDEV` se abstiene;
- symlink, hard link no soportado, destino existente o fuente mutada se abstienen;
- crash antes/después de metadata y rename produce estado conciliable;
- restore usa no-replace y verifica bytes;
- una segunda aplicación del mismo plan no repite efectos;
- el piloto no toca contenido fuera de su raíz y límites.

## 0.12.0 — Escala e inteligencia ampliada

**Resultado:** la ruta aprobada mantiene utilidad sobre árboles de más de
100,000 archivos.

Entregas:

- streaming y batches medidos en el camino crítico;
- checkpoints y reanudación sin reconstrucciones O(n) innecesarias;
- procedencia y localizadores estructurales para más formatos;
- búsqueda visual y temporal calibrada;
- cobertura generacional ampliada a owners que hoy son best-effort;
- políticas de canonicalización y versiones con evaluación representativa;
- acciones MCP opcionales sólo con concesión humana externa y el mismo ledger.

Criterios de aceptación:

- benchmark reproducible informa archivos/s, bytes/s, memoria, commits y ETA;
- cancelación deja checkpoint válido;
- el estado final concilia conteos de entrada, decisiones, efectos y salida en los
  owners locales;
- precisión/recall y falsos positivos se miden en fixtures etiquetados;
- no se reduce seguridad para ganar throughput.

## Orden inmediato

1. Validar y promover plan/review/decide desde el SHA final sin ampliar su
   autoridad.
2. Completar scan/verify, deduplicación exacta y cobertura del plan 0.10.0.
3. Diseñar el contrato de autorización separado; ninguna ReviewTask será su
   sustituto.
4. Integrar la foundation KIO preparada y completar sus pruebas de producto con
   runner/verificador inyectados y fixtures same-filesystem; reservar cualquier
   prueba contra KIO real para un gate explícito posterior.
5. Habilitar 0.11.0 sólo para lotes pequeños y revisión humana.
6. Medir una carga grande antes de promover watcher o escala automática.

## Límites

- No usar GitHub Actions ni proveedores remotos implícitos.
- No abrir el corpus real durante desarrollo o validación sin autorización.
- No reintroducir el antiguo subsistema de autoanálisis.
- Windows/NTFS no forma parte de estas entregas.
- Los informes de auditoría y evidencia bruta viven fuera de `docs/`.

La visión estable está en
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).
