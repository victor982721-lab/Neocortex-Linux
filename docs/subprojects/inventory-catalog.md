# Inventario y catálogo

## Ownership

`neocortex/deduplication` controla inventario, grupos, fingerprints y planes;
`neocortex/documents` controla catálogo, bindings, taxonomía y organización.
`integrations/inventory` es una superficie compartida regida por el AGENTS raíz,
no un nuevo repositorio; su backend activo es la observación portable Linux.
Consulta
[Architecture](../ARCHITECTURE.md#inventario-y-deduplicación) y [Persistence](../PERSISTENCE.md).

## Fronteras

- Separa `FileIdentity` física y `ResourceRef` virtual con su codec/owner.
  Una ruta o un miembro de ZIP no se convierte por inferencia en objeto movible.
- Originales primero: chunks, FTS, vectores y grafos son reconstruibles; sus
  índices no sustituyen la fuente. Conserva identidad, ámbitos y heads publicados.
- Fingerprint reduce candidatos; igualdad destructiva exige comparación exacta.
  Bytes nominales redundantes no prueban espacio físico recuperable y mtime no
  representa revisión documental ni concede autoridad para elegir keeper.
- Un plan de organización no mueve archivos. Distingue clasificación, elegibilidad,
  propuesta, operación ejecutable y autorización; no introduzcas fuentes de `/tmp`
  o de otra raíz en el ámbito del corpus por compartir el catálogo.
- La observación, diagnóstico de incertidumbre y planificación read-only pueden
  continuar aunque una mutación deba abstenerse. No amplíes una consulta a ingesta.

## Validación proporcional

Selecciona fixtures de identidad, bindings, deduplicación y organización según
el contrato tocado; prueba mezcla de ámbitos y recursos virtuales cuando aplique.
Las lecturas comparten el kernel de persistencia y sus fences, no clientes SQLite
paralelos. No uses el corpus real como fixture ni cambies reservas selladas.
