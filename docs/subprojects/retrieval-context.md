# Semantic y Knowledge

## Ownership

`neocortex/semantic` mantiene sus publicaciones, embeddings, búsqueda y lifecycle;
`neocortex/knowledge` compone recuperación, relaciones, evidencia y contexto sobre
owners compatibles. No concentres todos los datos en otro almacén para reorganizar
el trabajo. Consulta [Knowledge](../KNOWLEDGE.md), [consulta operacional](../KNOWLEDGE_OPERATIONAL_QUERY.md)
y [Architecture](../ARCHITECTURE.md#catálogo-semantic-y-knowledge).

## Fronteras

- Sólo heads publicados y snapshots compatibles sostienen resultados; parcial,
  omitido y no comprobado siguen separados de completo.
- Hechos, inferencias, confirmaciones y ambigüedad son evidencia tipada. Un score
  ordena candidatos, no es certeza ni permiso; no fusiones scopes por proximidad.
- Contexto entrega pasajes, citas y procedencia verificables. Conserva
  `answer_sufficiency=not_assessed`: `ask` no tiene que redactar por sí mismo una
  respuesta LLM ni declarar que una referencia verificada resuelve la pregunta.
- Diferencia salud del índice, condición del archivo, contenido documentado y
  preferencia organizativa; no diagnostiques el host desde un documento que lo menciona.
- Un cambio de retrieval no autoriza nuevos modelos, proveedores, procesamiento
  del corpus ni ajuste de reservas selladas. El estado de aceptación se consulta
  en CURRENT/SSOT, no se reabre ni recalifica por esta ficha.
- Readers, hidratación y snapshots heredan presupuestos y fences del kernel de
  persistencia; no inspecciones SQLite productivas en paralelo a writers.

## Validación proporcional

Prueba la consulta y referencias focales con fixtures controlados; verifica
scope, localizadores, selección de fragmentos, presupuesto y procedencia. Reproduce
el defecto observado sin presentar un mecanismo plausible como causa confirmada.
Para cambios de caché/publicación comprueba replay cuando aplique, sin convertir
un smoke positivo en equivalencia universal ni una referencia en suficiencia.
