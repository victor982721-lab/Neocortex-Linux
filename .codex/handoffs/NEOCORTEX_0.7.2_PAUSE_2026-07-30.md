# NeoCortex — handoff operativo actual

> Actualizado: 2026-08-09. El nombre del archivo es histórico para conservar
> una ruta estable. La campaña de autoanálisis está detenida por instrucción de
> Víctor; este documento describe únicamente la frontera funcional vigente.

## Preferencia operativa de Víctor

- GitHub debe conservar sólo `main` como rama visible.
- No crear PR ni ramas para evoluciones ordinarias de este proyecto personal.
- Cada entrega aceptada se integra con commits atómicos directos en `main`, una
  release Linux del SHA final exacto, verificación del launcher público y push
  directo a `origin/main`.
- No usar `--apply` ni `--organization-apply` en Linux. Ningún resultado de
  búsqueda, OCR, clasificación o similitud autoriza una mutación.

## Frontera entregada

Los commits funcionales de esta entrega son:

- `144902784131`: ZIP anidado, OCR Archive, texto/EML/Office heredado,
  integración Knowledge/Semantic, calidad textual y huellas de imagen;
- `8a7a0e8e3c43`: límite duro y verificable de `--image-max-count`, incluidos
  aciertos de caché y huellas completas pendientes.

El cierre exacto no se identifica por la versión `0.7.2`: el contrato exige que
`git rev-parse HEAD`, `current/release-manifest.json:source_sha` y la release que
verifica `tools/release_linux.py verify` coincidan.

### Archive y ZIP anidado

- La ruta `archive` forma parte de `--all` y nunca extrae miembros al
  filesystem.
- La procedencia pública usa
  `contenedor.zip!/subcarpeta/otro.zip!/documento.txt` y declara siempre
  `location=archive_member inside_zip=1`, contenedor, miembro, cadena y
  profundidad. Un archivo normal declara `location=physical inside_zip=0`.
- El fixture público validó nueve ZIP, tres nodos ZIP anidados, profundidad 3,
  OCR de imagen, OCR de PDF escaneado, búsqueda y replay íntegro de caché.
- El corpus vivo contiene 31 ZIP: 27 `complete`, 4 `partial`, 0 `error`, 258
  miembros, 240 indexados, 18 visibles sólo por metadata, 3 ZIP anidados y
  5,089,368 caracteres. El replay reutilizó los 31 contenedores.
- Las ocho incidencias acotadas permanecen explícitas: cuatro fallos de
  extracción PDF interna, dos fallos de decodificación textual, un límite de
  caracteres y un límite de tamaño de miembro. No se relajaron barreras para
  ocultarlas.

### Texto físico, correo y Office heredado

- La ruta `text` indexa texto imprimible, Markdown, CSV/TSV, HTML/XML/JSON,
  EML y DOC/XLS/PPT heredados; alimenta catálogo, Knowledge y Semantic.
- En el corpus vivo procesó 48 candidatos: 47 extraídos y un error permanente.
  El replay no hizo conversiones nuevas.
- El único error es
  `O2/f12459456.ppt`: contenedor CFB corrupto/sin texto visible. LibreOffice y
  `catppt` no produjeron contenido; no se inventó texto ni se interpretó el
  binario como texto plano.

### Semantic e imagen

- El estado vivo publicado schema 6 tiene 791 items, 3,382 chunks/embeddings
  textuales, 431 embeddings de imagen, 3,591 payloads vectoriales y 3,813 jobs
  totales. No existen millones de vectores pendientes en el estado vigente;
  consulte siempre `Neocortex --semantic-status` en vez de congelar conteos en
  documentación cotidiana.
- Los heads activos son generación 18 para Jina texto/OCR y generación 17 para
  CLIP visión. Ambos están `ready`, sin jobs pendientes, leased, failed, stale
  ni errores.
- CLIP se publicó en lotes reanudables de 25 bajo un deadline global de 15
  minutos. Cubrió 431/431 imágenes; el replay recorrió 431 y creó 0 jobs.
- `--image-max-count 25` sobre el corpus vivo informó 431 elegibles, 25
  seleccionadas y 406 omitidas. Un upgrade aislado de cinco imágenes legacy
  calculó sólo dos huellas; el replay reutilizó exactamente esas dos.
- La inspección visual confirmó que `industrial transformer` coloca primero una
  fotografía real de transformador. El canal textual conserva piso Jina `0.42`
  y se abstiene ante ruido de alta confianza.

## Seguridad e integridad demostradas

- El corpus continúa con 844 archivos, 103 directorios y 430,572,271 bytes.
- Los inventarios 19 y 27 tienen conjuntos idénticos de ruta, identidad física,
  tamaño, `mtime_ns` y `birthtime_ns`; no hay filas distintas en ninguna
  dirección. El hash NUL de rutas relativas sigue siendo
  `d568fc06ce6bb569abefb0ed9c058303f6c270ad72490d81756f18ac31ae24fb`.
- Las doce bases vivas aprobaron `integrity_check` y `foreign_key_check` sin
  violaciones después de los productores.
- No se usó `--apply`, `--organization-apply` ni se materializaron miembros ZIP.

## Carencias observadas que aún no deben ocultarse

1. **Abstención visual:** CLIP siempre devuelve sus vecinos top-k. Una consulta
   ajena como `receta de pastel volcánico cuántico` hace que texto se abstenga,
   pero el modo `all` todavía muestra falsos positivos de imagen. Falta una
   calibración visual con positivos/negativos humanos; no debe copiarse el piso
   textual ni inferirse uno de esta muestra.
2. **Completitud de Knowledge:** las búsquedas vivas entregan evidencia útil y
   estable, pero con índices grandes pueden terminar `complete=0`/código 4 por
   `semantic_candidate_limit_reached`. Debe preservarse el resultado parcial y
   corregirse paginación/presupuesto antes de presentarlo como respuesta
   exhaustiva.
3. **Ruido lexical:** FTS Archive puede elevar coincidencias por palabras muy
   comunes —por ejemplo `de` o `control` en JSONL históricos—. Conviene
   caracterizar tokenización, términos obligatorios y stopwords con consultas
   reales antes de cambiar el ranking.
4. **Archive Semantic vivo:** la búsqueda lexical Archive y el Semantic
   aislado están probados, pero el plan vivo completo de Archive proyecta 7,287
   chunks, dominados por JSONL/Markdown archivados. No se publicó sin una
   campaña acotada y una justificación de valor.
5. **Organización Linux:** clasificación, catálogo, búsqueda y previews están
   disponibles; la mutación sigue intencionalmente deshabilitada hasta existir
   un backend POSIX ligado a identidad con garantías equivalentes a Windows.

## Próximos pasos, en orden

1. Usar las búsquedas ya publicadas para preparar con Víctor un primer conjunto
   pequeño de información útil, empezando por `catalog-preview`, Knowledge y
   `organization-preview`, sin mutar archivos.
2. Corregir primero la señal de completitud/paginación de Knowledge para que una
   búsqueda útil no termine como incompleta sólo por el límite interno de
   candidatos.
3. Construir un fixture visual humano de 20–50 consultas positivas y negativas;
   sólo entonces calibrar abstención CLIP y su peso en modo `all`.
4. Evaluar Archive Semantic por selectores o lotes únicamente si sus resultados
   mejoran sobre FTS; no indexar miles de chunks históricos por inercia.
5. Mantener el PPT corrupto y las ocho incidencias Archive como límites
   visibles, o repararlos con fixtures equivalentes; nunca convertir ausencia
   de texto en éxito.

## Criterio de cierre

Una entrega futura no termina sólo por código o pruebas. Deben coincidir el SHA
de `main`, `origin/main`, la release activa y el launcher `Neocortex`; CI de
push debe quedar verde y GitHub sólo debe exponer `main`. Para búsquedas, se
exige evidencia relevante o una abstención honesta; para organización, preview
y aprobación humana previa a cualquier acción compatible con la plataforma.
