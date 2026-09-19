# Handoff operativo vigente — NeoCortex

**Ronda:** NEO-BOUNDED-RETRIEVAL-20260919.
**Actualizado:** 2026-09-19T01:33:36.887329+00:00.
**Base remota comprobada:** `dbcfd64ef95a94b93396ed46c0aba16c2ae3d2db`.
**Árbol integrado validado:** `aff8203b994187618f686e636fb21852ae45088d`.

## Resultado y alcance

El usuario autorizó implementar y publicar la siguiente ronda propuesta:
recuperación literal acotada, metadatos Dedup por lotes y reutilización al
publicar Code/Catalog. Seis agentes con el modelo heredado trabajaron en cuatro
frentes; dos revisores separados cubrieron Semantic y Dedup, y los autores de
Code/Catalog revisaron el frente ajeno. Raíz conservó Git, integración y SSOT.
Los nueve contratos tienen aceptación independiente en hashes congelados.
La publicación corresponde a código en main; no acredita una release instalada.

- S1/S2: Semantic y Knowledge preparan una consulta acotada una vez y recorren
  texto en bloques de 4.096 caracteres, sin listas completas de tokens/matches.
  Soporte y fragmento comparten análisis del mismo chunk. Se conservan Unicode,
  negaciones, cobertura, frases, posiciones, desempates y testigos. Los checks
  de cancelación y deadline ocurren también dentro del recorrido.
- S3: la revisión encontró un consumidor léxico independiente sin contexto
  ambiental de lectura. Su callback explícito ahora llega al procesamiento y
  conserva la excepción, sin devolver un lote posterior a la cancelación.
- D1/D2: Dedup lee hasta 128 solicitudes por consulta, con muestras de alias
  limitadas a 128 y recuentos/enlaces completos. Identidades, ruta preferida,
  keeper, procedencia y digests permanecen exactos. La evidencia se consume en
  store mientras viven las observaciones TEMP del tamaño actual. El progreso
  usa 100 ms de cadencia sin multiplicar eventos. El planner no expone deadline.
- C1/C2: Code conserva evidencia de filas sólo durante la publicación, con
  16 MiB contabilizados. Vuelve a leer y comparar valores y tipos antes de
  reutilizar un digest; ante diferencia, ausencia o presupuesto agotado usa el
  validador completo. Descarta la evidencia también en rollback/cancelación.
  Los lectores publicados mantienen validación completa.
- G1/G2: Catalog desactiva sólo filas retiradas o trasladadas antes del UPSERT,
  manteniendo las observaciones/fechas y la publicación atómica. Replay compara
  todas las columnas por claves únicas, sin los ordenados del doble EXCEPT;
  preserva valores almacenados, NULL, cobertura inversa y conflictos de rutas.

## Validación y límites del entorno

La fuente privada contiene 1.525 blobs exactos del árbol Git,
metadata generada en esa misma fuente e identidad 5/5. Los hashes se conservaron
durante colección, ejecución y análisis estático. CPython 3.14.7, SQLite 3.53.1,
NumPy 2.4.6; fixtures sintéticas y estado de pruebas aislado.

La batería integrada ejecutó **239 módulos y 4.227 casos únicos**: **4.211 PASS,
14 SKIP y 2 FAIL previos**, sin regresiones nuevas en este alcance. Añade 68
casos respecto a la misma batería base. Colección y JUnit coinciden sin faltantes
ni duplicados; duración 369,07 s. Los 14 omitidos conservan los mismos
identificadores que baseline; sus razones figuran en results.json.

Los FAIL conservan su estado y no se presentan como PASS:

- test_knowledge_exact.test_lookup_exact_orchestration_preserves_primary_state_bytes
  difiere del fixture en 16.000 pasos SQLite frente a 15.000 esperados y el hash
  que incluye ese contador. El hash observado coincide entre base y candidato;
  la caracterización verbose previa conserva matches, informes y timings.
- test_t_framework.OrchestratorTests.test_primary_orchestrator_runs_and_resumes_image_route
  falla antes del procesamiento porque el preflight OCR no encuentra spa.

Ruff pasa en los 13 archivos Python modificados. Mypy sobre ocho fuentes y sus
imports mantiene los mismos 24 diagnósticos previos, cero nuevos o retirados.
No se cambia el fixture para ocultar fallos ni se declara Mypy limpio.

## Medidas y aceptación independiente

- Semantic, resolve_search_hits público sobre un hit de 600.007 caracteres:
  mediana de medianas 386,8 a 160,0 ms; pico Python medido por separado 29,41 a
  2,45 MB. Tres pares alternados con cinco muestras de tiempo por proceso y
  respuesta completa idéntica. El auxiliar del soporte aislado baja de 31,30 MB
  a 0,417 MB; el de fragmento, de 25,93 MB a 6,6 kB. Entrada y salida completas
  aún cuestan memoria proporcional al texto. Revisión: 855 entradas, 3.693
  fragmentos, 15 resoluciones y 36 contextos idénticos, con 22 casos de aborto.
- Dedup, 200 miembros: 600 a 2 consultas enviadas a SQLite; plan cold 35,97 a
  34,50 ms y replay 35,33 a 32,94 ms en tres pares. Con 1.024 alias por identidad,
  3,771 a 1,961 millones de instrucciones SQLite. No se eliminan las búsquedas
  indexadas internas ni los recuentos completos. Nueve rutas/11 ejecuciones
  independientes conservan dataclasses, pruebas y digests, incluidos fallo
  después de 256 grupos, retry, hardlinks, alias y límites de partición.
- Code, 1.024 archivos: publicación cold 388 a 340 ms, un cambio 346 a 305 ms,
  una retirada 340 a 312 ms. Tres pares alternados y 12 reaperturas públicas.
  Para un cambio, filas hasheadas de lotes 22.532 a 11.266; las 11.266 se vuelven
  a leer y comparar. La memoria Python adicional medida es 1,46 MiB. Replay ya
  evitaba publicar y conserva 0 ms. El límite de 16 MiB no es una garantía RSS.
- Catalog, 1.024 documentos: un cambio reduce escrituras de proyección de
  2.048 a 1.024; una retirada, de 2.047 a 1.024. La comprobación SQL aislada usa
  12,28 a 9,00 ms de CPU y cero árboles temporales frente a cuatro. Los tiempos
  de ruta se solapan; no se demuestra aceleración total consistente. Seis
  muestras controladas reabren, más 16 reaperturas en revisión independiente.
  La revisión diferencial cubre 832 combinaciones de valores, NULL y colación.

Code/Catalog siguen capturando y validando autoridad O(N); esto no convierte
la publicación completa en O(cambios). Las medidas son locales, no un p95 de
producción ni un compromiso de velocidad universal. No hubo cambio de esquema,
motor de procesamiento, defaults, modelos ni instalación.

## Incidencias de la ronda y evidencia

Dedup 02 fue retirado por más trabajo SQLite y exceso de eventos; 03, por coste
de preparar SQL extenso. Sólo 04 compacto fue integrado y aceptado. Semantic 02
fue corregido por el callback del consumidor independiente; sólo 03 se integró.

Las primeras mediciones SQLite en el workspace se excluyen. Un protocolo de
cuatro muestras registró ausencia de WAL/SHM tras finalizar los productores y,
en otro comando, aparición de sidecars en una base y un candidato con el mismo
SHA256 del main. Se conservan las copias; el componente que los reintroduce no
está identificado. Seis muestras nuevas de Code bajo RUN privado en /tmp
conservaron archivos y SHA256 y pasaron 12/12 reaperturas. No se atribuye esa
alteración a la optimización ni se modificó el kernel SQLite para aceptarla.

La evidencia fechada externa reúne contracts.json, cuatro entregas y revisiones,
acceptance-baseline, acceptance-integrated, acceptance-static, reconciliación,
mediciones y publication/closure.json. El cierre verifica por lectura fresca
GitHub que HEAD/main/origin/main coinciden, árbol limpio y blobs remotos exactos.
El commit final se obtiene de Git y del informe para evitar autorreferencia aquí.
El delta posterior al árbol validado sólo actualiza CURRENT y archiva íntegro
el handoff anterior en
[NEOCORTEX_RESOURCE_CACHE_2026-09-18.md](NEOCORTEX_RESOURCE_CACHE_2026-09-18.md).
