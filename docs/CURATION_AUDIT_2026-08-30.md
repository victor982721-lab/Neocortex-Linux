# Auditoría de la iniciativa de curación Linux

Fecha de corte: 2026-08-30, rama `codex/neocortex-local-20260829`.

## Alcance observado

La rama ya ofrece inventario durable, identidad POSIX (`st_dev`/`st_ino`),
fingerprints XXH3, comparación exacta, `DedupPlanner`, catálogo y taxonomía de
documentos, propuestas de nombres, planes de organización, `CorpusMutationGuard`,
ledger de acciones y recuperación/reconciliación. El runtime publicado es
Linux-first y el estado real se conserva en SQLite.

La release `0.9.0-bb656793c005-cp314-linux-x86_64` quedó instalada desde el
SHA `bb656793c0059ff873f8d2ad22757b6e54a26a24`, `release_linux.py verify` pasó y el
launcher público superó un smoke y un replay aislados con texto, audio, vídeo,
imagen y código. La release anterior se conserva para rollback conforme a la
política vigente del proyecto.

## Mapa requisito → pieza existente

| Requisito | Pieza reutilizable | Brecha real |
| --- | --- | --- |
| Inventario e identidad | `deduplication.inventory`, `FileSnapshot`, `CorpusAccessPolicy` | El cursor USN/NTFS sigue mezclado con contratos portables. |
| Duplicados exactos | `DedupPlanner`, fingerprints y `files_equal_exact` | `DuplicateGroup.keep` es una decisión de planificación, no una disposición semántica. |
| Renombre/organización | `document_organization_planning`, `FrameworkActions` | El syscall activo depende de `windows_handle_mutation`; Linux se abstiene. |
| Papelera reversible | `safety.kio_trash`, ledger y reconciliación de `workflow/actions` | KIO ya está implementado; la integración real se limita a archivos regulares y deja directorios en revisión. |
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

Estas piezas son deuda histórica, no requisitos del flujo Linux. La primera
reducción segura es retirar sus imports del runtime, sustituir el punto único de
mutación por contratos POSIX/KIO y dejar los módulos históricos fuera del wheel y
de los gates Linux hasta una limpieza posterior. Borrar el árbol NTFS completo en
el mismo cambio no es seguro: todavía hay modelos de cursor, migraciones y tests
que lo referencian, y una eliminación masiva rompería estado histórico sin un
adaptador de lectura.

## Contrato propuesto para P0

1. `neocortex.safety.posix_mutation`: `renameat2(RENAME_NOREPLACE)` con
   directorios abiertos, mismo dispositivo, fuente regular sin symlink, un solo
   hard-link, verificación de identidad/tamaño/mtime antes y después, y receipt
   `posix-renameat2-noreplace-v1`. Si `renameat2` no está disponible, se abstiene.
2. `neocortex.safety.kio_trash`: detección ordenada `kioclient6`, `kioclient5`,
   `kioclient`; self-test opt-in con fixture tokenizado, enumeración real de
   `trash:/`, restauración y SHA-256. El backend se clasifica
   `reversible_path_bound`, nunca `identity_bound`.
3. `FrameworkActions` y organización consumen ambos contratos; el ledger se
   marca antes de cruzar el frontier y queda `recovery_required` si la
   confirmación falla.
4. `--apply`/`--organization-apply` dejan de rechazarse globalmente en Linux,
   pero exigen todos los fences existentes, backend disponible y revalidación
   inmediata. Los archivos vacíos pasan a `REVIEW`/skip por defecto.

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

El paquete P0 quedó implementado en el checkout: `posix_mutation.py`,
`kio_trash.py`, integración de acciones/organización, habilitación Linux en la
CLI/GUI y política zero-byte en revisión. El self-test KIO real del host
`kioclient5 6.6.4` pasó y se añadió una prueba local opt-in; P1–P4 continúan
pendientes y no se simulan como entregados.

## Paquete mínimo recomendado

La menor modificación arquitectónicamente correcta que habilita mutaciones Linux
sin debilitar invariantes es **P0 acotado**: un único adaptador POSIX para
renombre no-replace, un adaptador KIO separado y la conexión de ambos a los dos
consumidores existentes, manteniendo intactos el guard, el ledger, la
revalidación y la reconciliación. No hace falta crear todavía `neocortex/curation`
ni duplicar el inventario; P1–P4 deben consumir este contrato después de que la
frontera de mutación esté probada.
