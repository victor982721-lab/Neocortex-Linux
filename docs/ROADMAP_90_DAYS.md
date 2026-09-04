# Hoja de ruta de evolución de NeoCortex

Este documento es la frontera operativa del programa de confiabilidad y no un
gate agregador del producto. Cada corte se valida con pruebas individuales,
fixtures aislados y el ejecutable instalado; el corpus real y las mutaciones
Linux permanecen fuera del alcance hasta una autorización posterior.

## Objetivo de producto

Víctor debe poder consultar e incrementar su corpus local sin que una lectura
cree sidecars SQLite, una publicación parcial aparezca como completa o una
release deje más de un rollback físico. Las proyecciones siguen siendo
reconstruibles y la fuente original conserva prioridad.

## Estado de los cortes

| Corte | Resultado verificable | Estado |
|---|---|---|
| F0 — línea base | inventario de owners, topología, release y fixtures de carreras | implementado en pruebas y auditoría local |
| F1 — lectura segura | `SQLiteReadSession` con `immutable_strict`, `snapshot_temp` y rechazo de `writer_coordinated`; `state-health` contractual v2 | implementado en las superficies públicas y con prueba arquitectónica |
| F2 — estado publicado | backup/restore staged, integridad rápida/completa, purge con recaptura de sidecars, epoch y journal idempotente | implementado en API y fachada `databases`, con manifest de heads y gate fail-closed para publicaciones cross-owner; la atomicidad física de varios archivos sigue fuera de la garantía |
| F3 — contratos | envelope v1, códigos y cobertura comunes, validación MCP/cliente compartido, protocolo UI con secuencias y terminales | implementado en superficies principales |
| F4 — release | parser de IDs, staging con marcador, digest de árbol, lock previo, rollback y retención `current + rollback` | instalado y verificado desde el SHA final; supply-chain offline reproducible/modelos criptográficos quedan pendientes |
| F5 — multimodal | manifiesto canónico de capacidades, dependencia opcional vídeo→audio, fuente Semantic de vídeo con locators | adapter y selección explícita del planner para vídeo implementados; dependencia vídeo→audio, catálogo/OCR completo y propagación final quedan pendientes |
| M6–M12 — arquitectura | Code Graph generacional, módulos Semantic/Review y aislamiento Linux-first de tooling histórico | ledger Code aditivo implementado, integración del productor y modularización siguen pendientes |

## Gates por corte

1. **Lectura y health:** las operaciones públicas de consulta dejan bytes,
   inodos, mtimes y topología de sidecars sin cambios; un WAL no demostrable
   produce `blocked` o `partial`, nunca lectura silenciosa.
2. **Backup y publicación:** el manifest contiene owner, schema, hashes,
   permisos, sidecars, época e integridad; restore valida todo en staging y
   sólo publica con confirmación y digest.
3. **Contratos:** CLI plana, fachada humana, MCP y UI expresan la misma
   operación, scope, cobertura, error y `observed_epoch`; datos del corpus se
   sanitizan antes de terminal o interfaz.
4. **Release:** launcher, manifest y receipt apuntan al mismo SHA; staging queda
   vacío y sólo sobreviven `current` y el rollback inmediato, sin borrar una
   release en uso. La construcción offline reproducible con hashes por
   dependencia es una barrera posterior, no un hecho ya demostrado.
5. **Multimodal:** cada modalidad declara productor, owner, consumidor,
   cobertura, dependencia y locator; una fuente parcial no puede terminar como
   generación completa.

## Orden inmediato

1. Completar fixtures herméticos de owners completos, ausentes, WAL activo,
   sidecars huérfanos y carreras, sin abrir el estado productivo durante el
   piloto.
2. Integrar el gate de publicación cross-owner en cada consumidor multi-owner y
   ejercitar recuperación de journals, manteniendo `apply` bloqueado sin token
   y sin mutar el corpus.
3. Integrar el ledger generacional Code con el productor principal y conservar
   el lector legacy hasta probar equivalencia, backup y restore.
4. Ejecutar por separado el hardening de supply-chain offline, modelos y
   atestación nativa antes de promover otro release.
5. Registrar hashes, conteos, tiempos y límites en la evidencia canónica y
   actualizar `PENDIENTES.md`/`HISTORIAL.md` sin copiar evidencia bruta.

## Fuera de alcance vigente

- No se abre el corpus real para pilotos de este programa.
- `--apply` y `--organization-apply` continúan bloqueados en Linux.
- No se usa GitHub Actions ni auditoría remota implícita.
- Windows/NTFS se conserva sólo como compatibilidad histórica hasta demostrar
  consumidores y una migración preservativa.
