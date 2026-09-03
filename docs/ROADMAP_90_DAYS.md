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
| F1 — lectura segura | `SQLiteReadSession` con `immutable_strict`, `snapshot_temp` y rechazo de `writer_coordinated`; `state-health` contractual v2 | implementado; ampliación de lectores en curso |
| F2 — estado publicado | backup/restore staged, integridad rápida/completa, purge con recaptura de sidecars, epoch y journal idempotente | implementado en API local; superficie pública en integración |
| F3 — contratos | envelope v1, códigos y cobertura comunes, validación MCP/cliente compartido, protocolo UI con secuencias y terminales | implementado en superficies principales |
| F4 — release | parser de IDs, staging con marcador, digest de árbol, lock previo, rollback y retención `current + rollback` | implementado; falta instalar desde el SHA final |
| F5 — multimodal | manifiesto canónico de capacidades, dependencia opcional vídeo→audio, fuente Semantic de vídeo con locators | primera vertical implementada; catálogo/OCR completo queda pendiente |
| M6–M12 — arquitectura | Code Graph generacional, módulos Semantic/Review y aislamiento Linux-first de tooling histórico | siguiente programa, no se presenta como terminado |

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
4. **Release:** build offline reproducible, launcher, manifest y receipt apuntan
   al mismo SHA; staging queda vacío y sólo sobreviven `current` y el rollback
   inmediato, sin borrar una release en uso.
5. **Multimodal:** cada modalidad declara productor, owner, consumidor,
   cobertura, dependencia y locator; una fuente parcial no puede terminar como
   generación completa.

## Orden inmediato

1. Terminar la migración de lectores `mode=ro` al kernel único y ejecutar el
   test arquitectónico de conexiones directas.
2. Integrar backup/restore como comandos de consulta por defecto, manteniendo
   `apply` bloqueado sin token y sin mutar el corpus.
3. Ejecutar la suite completa y los checks focales, limpiar temporales y crear
   un commit único de esta evolución.
4. Construir e instalar la release desde el SHA final, verificar launcher,
   manifest, receipt, smoke público y replay; comprobar retención exacta.
5. Registrar hashes, conteos, tiempos y límites en la evidencia canónica y
   actualizar `PENDIENTES.md`/`HISTORIAL.md` sin copiar evidencia bruta.

## Fuera de alcance vigente

- No se abre el corpus real para pilotos de este programa.
- `--apply` y `--organization-apply` continúan bloqueados en Linux.
- No se usa GitHub Actions ni auditoría remota implícita.
- Windows/NTFS se conserva sólo como compatibilidad histórica hasta demostrar
  consumidores y una migración preservativa.

