# File Intelligence & Curation

## Visión

NeoCortex debe permitir que Víctor pase de “tengo una carpeta caótica” a
“entiendo qué contiene, revisé un plan, autoricé efectos concretos y verifiqué el
resultado” sin crear un script nuevo para cada caso.

El producto sirve a dos consumidores con los mismos contratos:

- una persona que usa la CLI;
- un agente local que necesita evidencia paginada, citable y accionable.

El agente no recibe más autoridad que la persona. El contenido del corpus, una
clasificación, una similitud o una recomendación nunca autorizan efectos.
La falta de autoridad para mutar no debe impedir que NeoCortex observe, relacione,
explique la incertidumbre y prepare una propuesta útil para revisión.

## Etiquetas de estado

- **CURRENT:** frontera operativa y de seguridad vigente; no acredita el SHA de
  una instalación.
- **IMPLEMENTED:** código y pruebas presentes en el checkout; la disponibilidad
  en el launcher se comprueba contra su manifest y su interfaz pública.
- **TARGET:** contrato futuro que todavía no existe como capacidad pública.

## Recorrido del producto

1. **Descubrir:** enumerar con límites, progreso y checkpoint.
2. **Identificar:** conservar identidad física, revisión de contenido y linaje.
3. **Comprender:** detectar tipo real, extraer estructura y representar
   procedencia, confianza e incertidumbre.
4. **Relacionar:** separar duplicado exacto, versión, similitud y pertenencia.
5. **Proponer:** crear un plan inmutable y paginado con razones y cobertura.
6. **Aplicar:** con `--apply`, ejecutar sólo propuestas automáticas seguras.
7. **Verificar:** demostrar origen/destino/Papelera, identidad, bytes y conteos.
8. **Recuperar:** ante caída o ambigüedad, observar antes de reintentar.

Las antiguas capas ReviewTask, value-review, `curate review`/`decide` y
AuthorizationGrant fueron retiradas: no existe una cola humana obligatoria ni
una ceremonia separada de autorización. La evidencia, incertidumbre, receipts y
recovery permanecen funcionales; `UNKNOWN` se conserva como KEEP.

## Evidencia y autoridad

Una observación indica productor, versión, fecha, identidad, cobertura y
localizador. Las afirmaciones se tipan como hecho, inferencia, efecto observado
o incertidumbre. Los scores de texto e imagen no se convierten en certeza ni
autorización. Un duplicado destructivo exige verificación byte a byte; una
huella rápida sólo reduce candidatos.

## Estado actual

La fuente aporta inventario, extracción multimodal, catálogos, búsqueda,
Knowledge, Semantic, planes de duplicados/organización, receipts y recovery.

- **CURRENT:** `curate scan`, `curate plan`, `curation_scan`, `curation_plan` y
  `--curation-preview` consultan el plan local paginado sin escribir estado o
  corpus.
- **IMPLEMENTED:** `curate verify` y `curation_verify` comprueban identidad,
  hash completo y bytes de los grupos duplicados con límites y abstenciones
  tipadas; no crean tareas, colas, grants ni `file_actions`.
- **IMPLEMENTED:** `--apply` es el único gate de usuario para acciones de alta
  confianza dentro de la raíz seleccionada. Las fences, receipts y
  `recovery_required` no se omiten.
- **IMPLEMENTED:** la lectura de recovery conserva intentos, receipts y
  clasificaciones sin exponer una autorización humana.

## Decisión Linux para Papelera

La fuente ya prepara la foundation `neocortex.safety.kio_trash`, que descubre el
primer cliente disponible entre `kioclient6`, `kioclient5` y `kioclient`, valida
configuración y snapshot, ejecuta mediante un runner inyectable
`move <origen> trash:/` y exige verificación del caller antes de emitir receipt.
`--all --apply` y `--dedupe --apply` la consumen en Linux con lotes bounded;
cualquier canaria física queda contenida en fixtures privados.

La integración automática revalida raíz, límites e identidad y conserva ledger
y recovery con backends inyectados. La canaria KIO instalada
demuestra permisos y locks efectivos, ausencia del origen, entrada esperada en
`trash:/`, metadata de restauración y receipt en un árbol privado; la
restauración visual en el escritorio real permanece separada.
Un timeout, error ambiguo, configuración KDE no escribible, symlink,
hard link no admitido, mount inseguro o cambio concurrente deja
`recovery_required` y no se reintenta a ciegas.

No se usará `gio trash`, `unlink`, borrado permanente ni una carpeta de
cuarentena como fallback. Las pruebas de la foundation y la canaria instalada
usan runner, resolver y verificador sobre fixtures contenidos, sin reusar la
configuración global.

## Interfaces CURRENT, IMPLEMENTED y TARGET

**CURRENT — plan y evidencia:**

```text
Neocortex curate plan [--limit N] [--cursor TOKEN] [--json]
Neocortex curate scan [--limit N] [--cursor TOKEN] [--json]
Neocortex curate verify PLAN_ID [--item-id ITEM_ID ...] [--limit N]
  [--cursor TOKEN] [--json]
MCP: curation_plan, curation_scan, curation_verify
```

Estas superficies son read-only y conservan `source_heads`, identidad,
cobertura, razones e incertidumbre.

**Aplicación automática segura:**

```text
Neocortex --root ROOT --all --apply
```

`--apply` ejecuta sólo acciones automáticas admitidas por política y evidencia.
Una identidad o precondición incierta se abstiene y queda en KEEP; un efecto
físico ambiguo queda en `recovery_required` con receipt parcial o clasificación
de recovery. No se crea ReviewTask, cola, evento o grant.

**Retirado:** `curate review`, `curate decide`, `curate authorize`, `curate apply`
y `curate reconcile`, junto con sus APIs, SDK, flags, schemas y herramientas
MCP. Restore operativo y consulta de recovery permanecen en sus contratos
separados y no constituyen una autorización humana intermedia.

## Criterio de completitud

La curación estará entregada cuando una muestra representativa pueda recorrer el
lifecycle completo, repetir sin rehacer trabajo, recuperarse de una interrupción
y dejar el plan, las decisiones, los efectos y el estado final consultables desde
los owners locales. Abstenerse de forma segura es necesario, pero no sustituye
resolver los casos soportados.

Las entregas y fechas se controlan en [ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md);
la arquitectura implementada se documenta en [ARCHITECTURE.md](ARCHITECTURE.md).


## Contrato público de KIO

**IMPLEMENTED:** `neocortex.safety.kio_trash.KioTrashService` es responsable del
ciclo físico común a la aplicación automática y los lotes de deduplicación
exacta. La corrida conserva validación del root y keeper, intents, transiciones
del ledger y vinculación de recibos al efecto. El servicio no abre SQLite,
selecciona políticas de autorización ni añade mutaciones MCP.

- `move(snapshot, *, source_digest)` devuelve un `KioTrashResult`.
- `move_many(items: Sequence[KioTrashBatchItem])` devuelve un resultado por
  entrada y en el mismo orden. Ambos usan `move_many_to_trash`, también para
  un lote de un elemento, y comparten claims, verificación y durabilidad.
- `trash_receipt_paths(evidence, expected, source_digest)` valida estructura,
  identidad, digest y layout declarado sin leer el filesystem.
- `verify_trash_receipt_evidence(evidence, expected, source_digest)` reobserva
  identidad completa, objeto regular único, digest, ausencia del original y
  metadatos de restauración vinculados al source. Es read-only y rechaza cambios.
- `read_claim_recovery_detail(detail, *, source_path)` resuelve localizadores
  v1/v2 contra el source exacto del resultado o acción, sin I/O ni restauración.

El constructor mantiene los parámetros explícitos de verifier, runner, which,
environment, home_directory, timeout y los tres controles privados. Se conservan
`move_to_trash`, `move_many_to_trash`, `restore_trash_receipt`, los tipos públicos,
los imports y firmas de `KioTrashBackend`, sus aliases de aplicación y el nombre
`kio-trash-path-bound-v1`. Los recibos de éxito y el esquema SQLite siguen en v1.

El modo nativo conserva configuración KDE privada, D-Bus privado y claim vecino
mediante `renameat2(RENAME_NOREPLACE)` en el mismo filesystem. Fuente y ejecutable
se revalidan junto a sus fronteras físicas. No hay fallback de copia, reemplazo,
GIO ni unlink del original. Un runner inyectado conserva el seam de fixtures:
recibe las rutas originales y no activa claims ni configuración de escritorio.

`applied` exige retorno satisfactorio, ausencia del original, evidencia exacta
Trash, flush de directorios y eliminación del claim seguida de fsync de su
padre. `blocked` sólo representa rechazo previo al efecto, con claims creados
restaurados. Timeout, interrupción, verificación incierta o fallo de restauración
requieren recovery; nunca un reintento automático de una operación ambigua.

El JSON de recuperación no se recorta como texto. Si el envelope v1 excedería
los 4096 bytes admitidos por `BackendOutcome`, v2 conserva basename del directorio de
claim, identidad física completa y SHA-256 de la ruta source. Esa ruta absoluta
ya está en el resultado o acción. El lector público reconstruye el claim exacto
y rechaza un source distinto; sólo el diagnóstico opcional puede reducirse.
Los envelopes históricos v1 íntegros de hasta 65.536 bytes continúan siendo
legibles. Esta preservación de metadatos no declara soporte KIO completo para
todos los nombres POSIX; esa compatibilidad conserva su validación específica.

Los lotes se separan por cantidad antes de invocar KIO. Se permite dividir por
argv sólo cuando todos los miembros devolvieron `blocked` con
`kio_batch_arguments_too_large`, después de restaurar todos sus claims. Los
resultados ya verificados se conservan si falla un lote posterior. Si todos los
resultados físicos ya están resueltos y falla retirar configuración temporal,
se conserva cada recibo y se registra el diagnóstico correspondiente.
Una interrupción del operador conserva recovery para el lote que ya cruzó la
frontera y marca `kio_cancelled_before_effect` en los sublotes aún no invocados;
no inicia efectos nuevos después de Ctrl+C.

`curation.application` traduce evidencia del servicio a `BackendOutcome`; su
replay, `curation.recovery` y `workflow.actions.file_action_recovery` consumen
validadores públicos, sin helpers privados de safety. `FrameworkActions`
conserva una fila y un recibo por source. Consultar un recibo o claim no autoriza
restaurar, reintentar ni ampliar la ejecución nativa. No se requieren migración,
factory reset del estado ni reclasificación de acciones históricas.
