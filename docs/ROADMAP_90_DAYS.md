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
| Deduplicación | Planificación y `curate verify` implementados; la disposición pública sigue bloqueada hasta `apply` |
| Catálogo y organización | Planes disponibles; recorrido end-to-end parcial |
| Knowledge y contexto para agentes | Implementado read-only; cobertura/localizadores varían por owner |
| Review | **IMPLEMENTED:** `curate review` publica ReviewTasks y `curate decide` añade decisiones humanas por CAS |
| Curación integrada | **CURRENT:** scan/plan/verify; **IMPLEMENTED:** review/decide advisory y AuthorizationGrant durable; **TARGET:** apply/verify/reconcile físico |
| Mutación Linux | Foundation KIO preparada; no integrada/promovida, y `--apply`/`--organization-apply` se abstienen |
| Backup/restore/purge | Implementados mediante `Neocortex databases` |
| MCP | **IMPLEMENTED:** plan/scan/verify/review/decide; authorize se omite hasta resolver un principal autenticado |

## 0.10.0 — Evidencia y plan de curación

**Resultado:** una persona o agente puede inspeccionar, paginar y revisar un plan
completo sin mutar el corpus.

**IMPLEMENTED en el checkout:** `curate scan` y `curate plan` consultan el digest
paginado; `curate verify` revalida identidad, hash completo y bytes de grupos
duplicados sin escribir estado; `curate review` publica páginas idempotentes como
ReviewTask y `curate decide` registra `resolved`/`dismissed` mediante digest y
event-head CAS. API, SDK y MCP proyectan estas operaciones. Scan/plan/verify son
read-only; review/decide escriben sólo Framework, mantienen
`actions_authorized=false` y crean cero `file_actions`.

Las superficies de scan, plan y verify comparten `source_heads` para inventario y
catálogo, con revisión, digest, cobertura, modo de verificación y razón de
abstención; verify admite `--cursor` para recorrer páginas posteriores sin
confundirlas con un cambio del snapshot.

`curate authorize` y `curation_authorize_payload` validan plan completo,
ReviewTasks resueltas, acción, actor, expiración y presupuestos, y persisten un
grant append-only con manifiesto de heads, versiones, eventos, fingerprints y
digest agregado en la extensión Framework. Está expuesto por CLI/API/SDK, no por
MCP; declara autoridad acotada, pero `physical_effect_applied=false` y crea cero
`file_actions`.

Entregas restantes:

1. proyección común de tipo real, procedencia, valor, duplicado, versión,
   similitud, disposición y evidencia;
2. ampliar el plan inmutable ya paginado con reason codes y localizadores
   públicos comprobables;
3. resolver autenticación antes de considerar un tool MCP de autorización;
4. límites uniformes de elementos, tiempo, RAM y disco, con progreso y
   cancelación;
5. corregir la paridad de `--all`, `resume` y las fachadas públicas;
6. cerrar las regresiones de fences SQLite y restore que afectan la siguiente
   cohorte física.

Criterios de aceptación:

- fixture heterogéneo de 20–50 elementos recorre scan, plan, verify, review y
  decide con paginación/replay;
- segunda corrida no rehace trabajo compatible;
- cada propuesta enlaza evidencia y explica incertidumbre;
- igualdad exacta exige comparación byte a byte;
- CLI, SDK, GUI y MCP proyectan el mismo schema;
- cero cambios en bytes/rutas del corpus, cero `file_actions` y cero autoridad
  derivada de una decisión; el grant sólo aparece tras `curate authorize` y no
  demuestra aplicación física.

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

1. `apply` consume el AuthorizationGrant vigente y crea el intento `file_actions`;
2. revalidación de grant, expiración, digest, ReviewTask heads, identidad,
   tamaño, mtime y hash junto a la frontera;
3. aplicación KIO/rename dentro del scope, acción y presupuestos concedidos;
4. verificación física con receipt y evidencia de Papelera/destino;
5. `reconcile` resuelve cada punto de caída y conserva `recovery_required`;
6. lotes pequeños con límite de acciones/bytes y cancelación entre efectos;
7. GUI que presenta grant e intento, pero no aporta una autoridad distinta.

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

1. Consolidar la verificación de snapshots, source heads, límites y envelopes de
   `scan/verify` en las superficies públicas.
2. Mantener `verification_mode` explícito y ningún candidato fast como duplicado
   bytewise, además de cerrar las regresiones SQLite que afecten estos lectores.
3. Implementar `apply → verify → reconcile` como consumidor estricto del grant;
   nunca derivar autoridad directamente de ReviewTask.
4. Integrar la foundation KIO preparada y completar sus pruebas de producto con
   runner/verificador inyectados y fixtures same-filesystem; reservar cualquier
   prueba contra KIO real para un gate explícito posterior.
5. Habilitar 0.11.0 sólo para lotes pequeños y revisión humana, y medir una
   carga grande antes de promover watcher o escala automática.

## Límites

- No usar GitHub Actions ni proveedores remotos implícitos.
- No abrir el corpus real durante desarrollo o validación sin autorización.
- No reintroducir el antiguo subsistema de autoanálisis.
- Windows/NTFS no forma parte de estas entregas.
- Los informes de auditoría y evidencia bruta viven fuera de `docs/`.

La visión estable está en
[FILE_INTELLIGENCE_AND_CURATION.md](FILE_INTELLIGENCE_AND_CURATION.md).
