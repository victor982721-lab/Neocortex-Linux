# Consultas operacionales de Knowledge

`neocortex.knowledge.knowledge_operational_query` es una superficie
read-only separada del compilador de contexto y de la búsqueda de contenido.
Su función es reconocer preguntas operacionales acotadas y consultar la
evidencia ya publicada por un owner existente, sin abrir el corpus ni crear,
migrar, reparar o autorizar estado.

## Contrato

La entrada es `OperationalQueryRequest(query, state_directory, source_root,
limit, cursor)`. Las raíces son absolutas, la página está limitada a 1--1000
registros y el cursor se entrega al lector del owner seleccionado. Cada
respuesta `OperationalQueryResult` tiene el esquema
`neocortex.knowledge-operational-query/v1`, conserva `snapshot_id`,
`next_cursor`, cobertura y errores tipados, y declara siempre
`read_only=true`, `advisory_only=true` y `mutation_authorized=false`.

Una consulta selecciona un único owner para que snapshot y cursor no puedan
mezclarse silenciosamente entre bases. Las preguntas que necesiten varios
owners deben emitir consultas separadas y comparar sus snapshots fuera de esta
superficie.

## Intenciones y owners

| Intención | Owner consultado | Clase principal |
|---|---|---|
| `pdf_protected` | Framework / review candidates | `processing` |
| `pdf_error` | PDF diagnostics | `processing` |
| `office_error` | Framework / review candidates | `processing` |
| `archive_issue` | Archive diagnostics | `processing` |
| `corpus_error` | PDF, Text, Archive y Review owners | `processing` |
| `curation_disposal` | Framework / review candidates | `policy` |

La clasificación es lexical y conservadora, con `unknown` como resultado
fail-closed. Los hechos usan las clases `file`, `processing`, `index`,
`policy` y `document_condition`, junto con certidumbre `observed`, `inferred`
o `unknown`. La presencia de una recomendación de eliminación nunca crea
autoridad, un `file_action` ni un efecto físico.

La superficie se consume mediante `ask` para preguntas operacionales y mediante
la herramienta MCP `operational_query`; también está disponible en la API de
lectura. Es un seam separado para no mezclar contexto citado con diagnósticos
de owners.

La intención `corpus_error` combina páginas acotadas de los owners sin mezclar
sus cursores, conserva un digest de los snapshots observados y marca como
parcial cualquier owner que requiera continuación; para continuar se consulta
cada owner con su cursor propio.
