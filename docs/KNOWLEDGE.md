# Knowledge

> Contrato funcional de las publicaciones existentes; no certifica que el
> estado local tenga cobertura útil.

## Propósito

Knowledge ofrece una vista local y trazable sobre inventario, extractores,
catálogo y Semantic. No crea una segunda indexación ni genera una respuesta
con un LLM. Su producto es evidencia y contexto citado con cobertura explícita.

```text
owners publicados → snapshot lógico → rankings independientes
                   → fusión por evidencia → contexto presupuestado
```

## Frontera read-only

`status`, `search`, `context`, `evidence`, `operational_query`, `health` e
inspección de linaje:

- abren sólo owners existentes mediante lectores compatibles;
- no crean, migran, reparan ni hacen checkpoint;
- no recorren corpus ni cargan modelos;
- no autorizan rename, move, Papelera o borrado;
- se abstienen ante schemas futuros, corrupción o snapshot inestable.

`review value --refresh` no pertenece a esta frontera: escribe una página de
Review en Framework y lo declara expresamente.

## Identidad

El recurso físico canónico es `resource:file` también en Linux cuando
`birthtime_ns=-1`. La identidad portable usa los campos demostrables por la
plataforma y no convierte `ctime` en nacimiento. Las rutas son observaciones y
pueden cambiar; un join no se resuelve sólo por nombre o extensión.

Una revisión distingue bytes/contenido en un momento determinado. Resultados de
otra revisión, firma de procesamiento o snapshot no se mezclan silenciosamente.

Los miembros de un archivo compuesto son recursos virtuales: usan el namespace
`resource:archive:*`, conservan su cadena de miembros y localizadores publicados,
pero no exponen `physical_identity`. La ausencia se declara como
`physical_identity_unresolved`; una clave de miembro nunca puede alimentar un
join de inventario físico ni convertirse en identidad por su forma textual.

## Snapshot y owners

Knowledge captura el vector contractual de `STATE_STORE_REGISTRY`. Cada owner
queda `ready`, `missing`, `partial`, `future`, `incompatible`, `corrupt` o
`blocked` según su lector. Un owner grave sólo bloquea la consulta cuando forma
parte de sus fuentes requeridas; los demás siguen visibles y la cobertura total
se marca parcial.

Las publicaciones generacionales se leen desde su head. Para owners
no generacionales, la salida declara esa limitación. Una publicación cross-owner
pendiente sólo bloquea lectores cuyos owners intersectan el cambio.

## Rankings y evidencia

Los canales de identidad, ruta, metadatos, FTS y Semantic mantienen
scores independientes. Semantic sólo aporta evidencia cuando existe un head,
modelo y espacio vectorial compatibles. Discovery puede proponer candidatos,
pero un título o ruta no crea evidencia corporal.

La búsqueda exacta usa un único orden total en el escaneo SQLite, el índice
persistido escalar y el camino NumPy: score descendente, `item_id`, `entity_id`
y firma de modelo ascendentes, y `ref_id` descendente sólo cuando la identidad
semántica completa empata. Ese orden selecciona el representante de cada grupo,
retiene el top K, ordena la salida y calcula `raw_rank`. Para un mismo snapshot
y presupuesto de escaneo, K es prefijo de una ventana mayor. Discovery agrupa
por item; la búsqueda de evidencia agrupa por item y entidad. Los diagnósticos
no convierten un escaneo incompleto en un rango global ni alteran sus límites.
El cambio es de consulta: no requiere reconstruir índices ni migrar vectores.

`SemanticVectorSearch` es la frontera sustituible de ranking. El request
envuelve `ExactSearchQuery` completo: vector, firma y espacio del modelo,
dimensiones, modalidad y selección de modelos, junto con snapshot del owner,
scope, granularidad de evidencia, cursor y diagnósticos solicitados. El
presupuesto conserva K, máximo de vectores y tamaño de lote. La página
transporta candidatos ordenados, filas inspeccionadas, cursor, cobertura,
backend y motivo de fallback; no concede autoridad a citas ni a inferencias.
`NativeExactVectorSearch` conserva el escaneo exacto como oráculo y
`PersistedExactVectorSearch` usa un handle preparado y verificado explícitamente.
Las funciones `search_exact_page` y `search_exact_evidence_page` aceptan
`vector_backend=`; el parámetro existente `exact_index=` usa el mismo adapter.
`backend_diagnostics=` permite observar esa decisión sin cambiar la página
pública ni los diagnósticos de ranking existentes.

Un backend sólo puede declinar antes de escanear. Una incompatibilidad de scope
o diagnósticos puede volver al exacto directo con el mismo presupuesto y
snapshot; un cambio de fuente, cambio del artefacto durante el escaneo o
respuesta mal formada produce abstención sin reintento implícito. La hidratación
y la comprobación de revisiones siguen en el owner semántico. Una consulta nueva
captura su propia vista; una vista derivada no se reconstruye desde una lectura.

Las normas NumPy persistidas conservan el binding v1 del binario, versión,
features de CPU, byteorder y runtime Python. El adapter privado de NumPy 2
resuelve una sola extensión, `numpy._core._multiarray_umath`, y valida su
estructura y ELF antes de reutilizar el binding. `_core` no es una API pública
estable. El nombre lógico histórico `numpy.core._multiarray_umath` se conserva
porque el alias y la extensión son idénticos en las wheels comprobadas por la
prueba de equivalencia; cada wheel del perfil offline debe pasar esa prueba.
Cambiar el runtime o sus features invalida exclusivamente la vista vinculada;
no autoriza borrar índices, reconstruirlos ni tocar originales.

La fusión preserva:

- recurso y revisión;
- owner/productor y firma;
- score por canal;
- evidencia corporal;
- cobertura, warnings e incertidumbre;
- localizador realmente disponible.

## Localizadores

Knowledge puede transportar páginas, partes OOXML, celdas, slides, segmentos de
audio/video, miembros ZIP o archivo completo sólo cuando el owner los publicó.
El manifest de una capacidad no sustituye una materialización; la precisión no
demostrada se omite.

Los ZIP anidados conservan la cadena de miembros. Audio y Video usan
`start_ms/end_ms` cuando el resultado procede de un segmento.

## Contexto para agentes

`ContextBundle` empieza con la frontera `untrusted-corpus-data-v1`. Declara
`instruction_authority=false`, `tools_authorized=false` y
`actions_authorized=false`. Las citas permiten verificar el origen, no conceden
permisos.

El compilador aplica presupuesto y diversidad por recurso para evitar que un
archivo monopolice el contexto. La salida conserva consultas, fuentes, hits,
omisiones y cobertura suficiente para que un agente decida si puede responder o
debe pedir más evidencia.

La proyección `neocortex.context-response/v2` añade entidades, relaciones,
contradicciones, presupuesto del grafo y telemetría bounded, sin cambiar la
frontera de confianza ni evaluar `answer_sufficiency`. La función Python de bajo
nivel conserva v1 por compatibilidad explícita; GUI, SDK y MCP solicitan v2.
`KnowledgeReadBudget` permite limitar filas, vectores, temporales, deadline y
cancelación sin escribir estado.

## Interfaces

```bash
Neocortex --knowledge-status --knowledge-json
Neocortex --knowledge-search "consulta" --knowledge-json
Neocortex search "consulta" --scope personal
Neocortex ask "consulta" --scope personal
Neocortex knowledge health RESOURCE_ID --scope all --json
```

API Python y MCP proyectan los mismos contratos; `curation_plan` añade la vista
read-only paginada de propuestas sin conceder autoridad y la evidencia acepta
IDs estables con snapshot esperado. No deben parsear la salida humana. Los
detalles de argumentos están en [CLI.md](CLI.md).

`content-diagnostics/v2` federa los ocho owners de contenido con cursores
ligados a raíz, filtros y snapshots; `content_diagnostics` v1 permanece legible.

`operational_query` y las preguntas operacionales de `ask` consultan directamente
los diagnósticos publicados por sus owners, conservan snapshot y cursor, y
clasifican el resultado como evidencia de archivo, procesamiento, índice,
política o condición documental. La recomendación de curación permanece
advisory y nunca crea autorización ni efectos físicos.

## Criterio de utilidad

Knowledge está operativamente útil cuando consultas representativas devuelven
evidencia relevante con localizadores verificables, latencia comprensible y una
explicación precisa de la cobertura faltante. Fixtures y pruebas de contrato son
regresiones necesarias, pero no sustituyen esa demostración sobre un estado
autorizado.

La evaluación independiente de reglas usa JSONL con textos y etiquetas
sintéticos, rangos emitidos, actores y localizadores. Las etiquetas se fijan
antes de observar predicciones y los escenarios y paráfrasis permanecen en un
único split. `tools/knowledge_evidence_evaluation.py` verifica su hash, ejecuta
la proyección pública de contexto y compara el fragmento finalmente emitido,
incluyendo `query_role_counterevidence`, scope y offsets. Reporta resultados por
dominio, familia, sujeto y split, junto con abstenciones, cobertura y matriz de
soporte sobre las etiquetas evaluables. Las abstenciones no cuentan como
negativos correctos; un denominador vacío no permite estimar precisión.

La aceptación sintética exige cero soportes espurios y mantiene
`answer_sufficiency=not_assessed` y `document_completeness=not_asserted`.
Coincidencias en filename o locator, scores altos y verificación de una cita no
sustituyen testigos del fragmento. El panel de preguntas generales puede revelar
cobertura muy baja: las reglas sólo reconocen familias léxicas explícitas y no
realizan entailment general. Los estados individuales de los markers se
reportan, pero no reciben métricas de precisión sin etiquetas independientes
por marker. Los fixtures de desarrollo y estos paneles no acreditan relevancia
en el corpus personal; esa evaluación requiere un conjunto local autorizado y
etiquetado.

`benchmarks/semantic_vector_backend_benchmark.py` mide vectores sintéticos a
través de la misma frontera usada por las consultas. Separa latencia de
ranking, construcción y reapertura del artefacto, filas escaneadas, igualdad con
el oráculo y costes de snapshots nuevos o reutilizados. Registra la abstención
por drift y la recuperación explícita después de una publicación interrumpida.
Su decisión inicial conserva exacto mientras no haya evidencia que justifique
ANN. Un benchmark sintético no mide calidad de embeddings, corpus personal ni
requisitos universales de rendimiento; las mediciones y decisiones de cada
ensayo se conservan fuera del árbol productivo.

Las brechas de localización, publicación y curación se priorizan en
[ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md).
