# Knowledge

> Contrato funcional de las publicaciones existentes; no certifica que el
> estado local tenga cobertura útil.

## Propósito

Knowledge ofrece una vista local y trazable sobre inventario, extractores,
catálogo, Semantic y Code. No crea una segunda indexación ni genera una respuesta
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

Los canales de identidad, ruta, metadatos, FTS, Semantic y Code mantienen
scores independientes. Semantic sólo aporta evidencia cuando existe un head,
modelo y espacio vectorial compatibles. Discovery puede proponer candidatos,
pero un título o ruta no crea evidencia corporal.

La fusión preserva:

- recurso y revisión;
- owner/productor y firma;
- score por canal;
- evidencia corporal;
- cobertura, warnings e incertidumbre;
- localizador realmente disponible.

## Localizadores

Knowledge puede transportar páginas, partes OOXML, celdas, slides, segmentos de
audio/video, miembros ZIP, símbolos/rangos de código o archivo completo sólo
cuando el owner los publicó. El manifest de una capacidad no sustituye una
materialización; la precisión no demostrada se omite.

Los ZIP anidados conservan la cadena de miembros. Audio y Video usan
`start_ms/end_ms` cuando el resultado procede de un segmento. Code conserva
proyecto, archivo, símbolo y rango cuando el analizador los conoce.

## Contexto para agentes

`ContextBundle` empieza con la frontera `untrusted-corpus-data-v1`. Declara
`instruction_authority=false`, `tools_authorized=false` y
`actions_authorized=false`. Las citas permiten verificar el origen, no conceden
permisos.

El compilador aplica presupuesto y diversidad por recurso para evitar que un
archivo monopolice el contexto. La salida conserva consultas, fuentes, hits,
omisiones y cobertura suficiente para que un agente decida si puede responder o
debe pedir más evidencia.

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

Las brechas de localización, publicación y curación se priorizan en
[ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md).
