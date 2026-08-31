# Auditoría de la iniciativa de curación Linux

Fecha de corte: 2026-08-30, rama `codex/neocortex-local-20260829`.

## Alcance observado

La rama ya ofrece inventario durable, identidad POSIX (`st_dev`/`st_ino`),
fingerprints XXH3, comparación exacta, `DedupPlanner`, catálogo y taxonomía de
documentos, propuestas de nombres, planes de organización, `CorpusMutationGuard`,
ledger de acciones y recuperación/reconciliación. El runtime publicado es
Linux-first y el estado real se conserva en SQLite.

La auditoría se ejecutó sobre la rama de trabajo; la release pública vigente
continúa siendo `0.9.0-bb656793c005-cp314-linux-x86_64`, que conserva el contrato
Linux de abstención. Los prototipos POSIX/KIO explorados en el árbol de trabajo
no se promueven ni se exponen como capacidad productiva porque las instrucciones
vigentes exigen rechazar `--apply`/`--organization-apply` antes de crear estado.
Las releases anteriores se conservan para rollback conforme a la política
vigente del proyecto.

## Mapa requisito → pieza existente

| Requisito | Pieza reutilizable | Brecha real |
| --- | --- | --- |
| Inventario e identidad | `deduplication.inventory`, `FileSnapshot`, `CorpusAccessPolicy` | El cursor USN/NTFS sigue mezclado con contratos portables. |
| Duplicados exactos | `DedupPlanner`, fingerprints y `files_equal_exact` | `DuplicateGroup.keep` es una decisión de planificación, no una disposición semántica. |
| Renombre/organización | `document_organization_planning`, `FrameworkActions` | El syscall activo depende de `windows_handle_mutation`; Linux se abstiene. |
| Papelera reversible | ledger y reconciliación de `workflow/actions` | No existe un backend KIO productivo mientras rija la abstención Linux; la estrategia KIO queda como diseño futuro. |
| Zero-byte | `_trash_empty_files` | La política actual `size=0;policy=trash-all-empty` autoriza demasiado. |
| Tipo físico/funcional | `platform.content_types`, extractores y taxonomía | Falta una proyección explícita por capas. |
| Nombres recuperados | `documents.document_naming` | Hay heurísticas útiles, pero no una propuesta ligada a evidencia y colisiones para curación. |
| Review | `workflow.review` y Value Review | Faltan reason codes y un consumidor de disposiciones de duplicados. |
| SQLite | `persistence.sqlite_*` | No existe inspección de bundles `.sqlite/-wal/-shm` read-only. |
| Sincronización | no hay guard genérico | Debe añadirse antes de una mutación masiva. |
| Reporte/post-audit | receipts de acciones y estado durable | Falta una salida única autocontenida de una corrida de curación. |

## Complejidad Windows/NTFS detectada

El árbol activo todavía contiene cinco herramientas de release Windows, el
adaptador Win32 de `neocortex.safety`, enumeración USN/MFT en
`neocortex.enumeration.ntfs`, un índice SQLite NTFS, ramas de política Windows,
protecciones de perfil Windows y una presentación llamada `windows`. Dos
consumidores productivos importan directamente el adaptador Win32:
`workflow.actions` y `documents.document_organization_application`. El
orquestador también conserva el cursor USN aunque en Linux siempre termina en
inventario portable.

Estas piezas son deuda histórica, no requisitos del flujo Linux. La reducción
segura aplicada en este corte elimina la capa activa de Job Objects para workers
y limita el sdist a herramientas Linux, pero conserva adaptadores Windows/NTFS
históricos sin cargarlos en el runtime. Retirar también los modelos de cursor,
migraciones y tests requiere una campaña separada de compatibilidad de lectura;
no se borra de forma masiva en este lote.

## Contrato propuesto para P0

1. `neocortex.safety.posix_mutation`: `renameat2(RENAME_NOREPLACE)` con
   directorios abiertos, mismo dispositivo, fuente regular sin symlink, un solo
   hard-link, verificación de identidad/tamaño/mtime antes y después, y receipt
   `posix-renameat2-noreplace-v1`. Si `renameat2` no está disponible, se abstiene.
2. `neocortex.safety.kio_trash`: detección ordenada `kioclient6`, `kioclient5`,
   `kioclient`; self-test opt-in con fixture tokenizado, enumeración real de
   `trash:/`, restauración y SHA-256. El backend se clasifica
   `reversible_path_bound`, nunca `identity_bound`.
3. Si la política de plataforma se autoriza en el futuro, `FrameworkActions` y
   organización podrán consumir ambos contratos; el ledger se marca antes del
   frontier y queda `recovery_required` si la confirmación falla.
4. El runtime actual mantiene `--apply`/`--organization-apply` bloqueados en
   Linux antes de crear estado; los archivos vacíos deben pasar a `REVIEW` antes
   de habilitar cualquier backend.

## Estados y receipts

Las acciones conservan `planned`, `applying`, `confirmed`, `not_performed`,
`recovery_required`, `ambiguous`, `blocked` o `failed`. Cada receipt debe ligar
fuente, destino, dispositivo/inode, tamaño, mtime, SHA cuando corresponda,
backend, binario, operación y timestamp; el fingerprint del plan invalida un
`apply` si cambia cualquier precondición.

## Estrategia de pruebas

El paquete P0 se valida con pruebas focales para `renameat2` no-replace,
identidad/tamaño/keeper/destino mutados, filesystem cruzado, symlinks, backend
ausente y KIO con nombre prefijado (`0-...`). Se añade una integración local
opt-in que usa el KIO real y limpia sólo su fixture. Después se ejecutan Ruff,
MyPy focal, `compileall`, smoke público y replay desde la release instalada.

## Desglose P0–P4

- **P0:** contrato POSIX, KIO, receipts y gates; es el primer paquete.
- **P1:** plan durable/fingerprint, preflight, self-test, apply y post-audit.
- **P2:** disposición semántica de duplicados y eliminación de `trash-all-empty`.
- **P3:** tipos por capas, nombres recuperados, artefactos técnicos, sync,
  SQLite y handles activos.
- **P4:** Review/reporting, ZIP único, CLI de curación y ergonomía de recovery.

## Estado del paquete mínimo

P0 queda **bloqueado por política**, no entregado: la menor modificación futura
sería el adaptador POSIX/KIO descrito arriba, pero el contrato vigente no permite
exponerla ni ejecutar mutaciones Linux. La simplificación efectiva de este corte
retira la supervisión Windows de workers y mantiene el producto en modo
Linux/read-only; P1–P4 continúan pendientes y no se simulan como entregados.

## Paquete mínimo recomendado

La menor modificación arquitectónicamente correcta para habilitar mutaciones
Linux, si la política superior cambiara, seguiría siendo **P0 acotado**: un
adaptador POSIX no-replace y un adaptador KIO separados, conectados a los dos
consumidores existentes y manteniendo intactos guard, ledger, revalidación y
reconciliación. Bajo la política actual esa modificación no puede promoverse;
por ahora no se crea `neocortex/curation` ni se ejecuta `curate --apply`.
