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

Las intenciones específicas conservan el cursor nativo del owner. `office_error`
y `corpus_error` usan un cursor federado canónico que contiene un cursor
independiente para cada owner, además de snapshots, consulta, scope y raíces.
El token incluye un digest del payload y se rechaza si fue adulterado, si se
reutiliza con otra consulta, scope o raíz, o si un owner cambia de snapshot
durante la continuación. La respuesta no mezcla la página nueva con hechos de
una página anterior cuando detecta ese cambio.

## Intenciones y owners

| Intención | Owner consultado | Clase principal |
|---|---|---|
| `pdf_protected` | Framework / review candidates | `processing` |
| `pdf_error` | PDF diagnostics | `processing` |
| `office_error` | Framework / review candidates | `processing` |
| `archive_issue` | Archive diagnostics | `processing` |
| `corpus_error` | PDF, Text, Archive y Office | `processing` |
| `curation_disposal` | Framework / review candidates | `policy` |

La clasificación es lexical y conservadora, con `unknown` como resultado
fail-closed. Los hechos usan las clases `file`, `processing`, `index`,
`policy` y `document_condition`, junto con certidumbre `observed`, `inferred`
o `unknown`. La presencia de una recomendación de eliminación nunca crea
autoridad, un `file_action` ni un efecto físico.

La superficie puede consumirse mediante `ask`, la herramienta MCP
`operational_query` o la API de lectura, manteniéndose separada del contexto
citado para no mezclar diagnósticos de owners.

La intención `corpus_error` combina páginas acotadas de los owners, conserva un
cursor federado con la posición independiente de cada owner y marca
`snapshot_changed` sin publicar hechos cuando la continuación detecta deriva.
