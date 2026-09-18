# Handoff operativo vigente — NeoCortex

**Ronda:** NEO-RESOURCE-CACHE-20260918.
**Actualizado:** 2026-09-18T23:16:41.898998+00:00.
**Base remota comprobada:** `e2e994c1e9213400271f0a17d399f97b12ed1526`.
**Árbol integrado validado:** `cca804b0b4b8666263047fd94b5f60a4a068906f`.

## Resultado y alcance

El usuario autorizó continuar auditando e implementando optimizaciones en el
remoto. La ronda usa seis actores internos, incluido un descendiente real, y
raíz como único responsable de Git, integración y SSOT. Doce contratos cuentan
con aceptación independiente; no quedan contratos de esta ronda pendientes.
La publicación es código en main; no representa instalación de una release.

- R1/R2/R3: PDF transmite su cancelación local a la admisión global; los gates
  comprueban el token después de los probes y antes de conceder recursos.
  Las señales explícitas CPU comparten presión e histéresis; el sampler por
  defecto conserva su función de telemetría.
- F1/F2: Office comprueba cancelación alrededor de lecturas XML y antes de
  devolver el resultado. DOCX valida una sola vez el hit al consumirlo y liga
  el efecto a identidad, firma y estado vigentes dentro de la transacción.
- K1: Semantic consulta cancelación/deadline entre descompresión, fragmento,
  soporte literal y materialización final, preservando error y cargos de filas.
- C1/C2: Code serializa fragmentos acotados con el encoder nativo, con digests
  idénticos y fallback incremental. Símbolos/definiciones comparten una consulta
  dentro del snapshot, conservando señales RRF y cargos lógicos separados.
- W1/W2: Catalog escribe el binding junto con clasificación/error; el hit copia
  el binding validado sin UPDATE posterior. Estimar carga deja de recorrer la
  fuente cancelada, incluso ante descartes o finalización del iterador.
- P1: el parser SQLite reutiliza tokens por SQL exacto, con 2 MiB contabilizados,
  256 entradas y 4.096 caracteres por entrada. Repite todas las observaciones
  estructurales; no cachea la aceptación de una base.
- C3 amplía explícitamente la ronda al detectar un consumidor incompatible:
  Catalog rechazaba Code v9 en la ruta pública de Framework. Admite ahora esa
  versión con identidad hexadecimal y conserva rechazo de futuro/legacy ambiguo.

## Validación y límites del entorno

Se verificaron 1.520 blobs contra el árbol Git y se mantuvieron sin cambios
mientras se ejecutaban metadata propia, identidad 5/5, colección y pruebas.
CPython 3.14.7, SQLite 3.53.1, NumPy 2.4.6. No se usaron corpus ni estado de
usuario; no se descargaron modelos ni se instalaron componentes globales.

La batería integrada comprende **158 módulos y 2.135 casos únicos**: **2.096
aprobados, 37 omitidos Windows y 2 fallos de entorno previos**, cero regresiones
nuevas. Las 23 subpruebas se cuentan aparte. Colección y JUnit coinciden sin
faltantes ni duplicados; ejecución final 183,46 s. La advertencia previa de
record_property con JUnit xunit2 permanece registrada.

Los dos fallos son test_pdf_route.PdfRouteTests.test_auto_ocr_only_for_page_without_native_text
y test_verified_recycle_resolves_only_current_unrecoverable_reason. Ambos fallan
igual en baseline porque falta spa de Tesseract, antes de construir el gate o
admitir recursos. El runtime tiene eng/osd; los assets offline no incluyen spa.
Permanecen FAIL de entorno, no se presentan como PASS ni se cambian defaults.

La primera ejecución registró además 20 fallos por NumPy ausente. Se preparó
sólo el wheel 2.4.6 y SHA256 fijados por el lock del repositorio en el tooling
privado; pip check pasó y 4.678 archivos anteriores conservaron sus hashes.
El total conocido de descargas es 93.759.868 bytes, bajo el techo previo de
100 MB. Se repitió toda la batería, incluidos los backends vectorizados.

Ruff pasó en los 23 archivos Python modificados. Mypy sobre 13 fuentes más sus
imports reproduce 62 diagnósticos en baseline y candidato, cero nuevos y cero
retirados. No se presenta Mypy como limpio; antes de añadir NumPy eran 66.

## Medidas reproducibles

- Code, 1.024 archivos y tres pares alternados: cambiar uno pasa de 804,4 a
  682,6 ms de ruta y de 463 a 349 ms de publicación. Borrar uno pasa de 854,7 a
  680,4 ms de ruta y de 455 a 362 ms de publicación. La forma lógica es igual.
  Replay mantiene 1.024 hits, cero procesados y publicación reutilizada;
  mediana 238,3 a 223,2 ms, con rangos solapados, sin promesa universal.
- Aperturas calientes CodeState, tres pares de 30 muestras: mediana 34,64 a
  15,10 ms. Tras warmup evita 17.080 ejecuciones del lexer en 40 aperturas;
  repite sus 17.080 consultas al wrapper y usa 1.606.600 bytes contabilizados.
- DOCX conserva cache_hits=1, extracted=0 y FTS=0; validaciones 2→1,
  descompresiones 6→3 y caracteres decodificados 6.300.092→3.150.046.
- Catalog elimina 40 UPDATE adicionales tanto en frío como con 1 cambio y
  39 hits. El estimador cancelado en la fila 10 consume 10 de 5.000 filas.
- Office deja de leer 1.753.125 bytes tras cancelar y no publica FTS. Semantic
  pasa de 7 a 1 descompresiones ante cancelación en la primera, sin tupla tardía.

Son fixtures sintéticas locales. Code/Catalog mantienen validación/proyección
completas; la cancelación es cooperativa. Quedan observados para otra ronda los
600 SELECT de metadatos Dedup para 200 miembros y la materialización de matches
al construir fragmentos/soporte literal. No se implementaron ni se contaron como
contratos cerrados. No hubo migración tecnológica ni cambio de esquema/defaults.

## Evidencia y publicación

La evidencia externa incluye contracts.json, handoffs/revisiones, perfiles,
acceptance-integrated-recheck, acceptance-static-recheck, integrated-measurements,
numpy-validation y publication/closure.json. Se conserva la primera ejecución
fallida y sus causas. El cierre requiere lectura fresca de GitHub, árbol limpio
e igualdad HEAD/main/origin/main; el SHA final se obtiene de Git y del informe,
sin autorreferencia circular aquí. El delta posterior al árbol validado sólo
actualiza este handoff y archiva el anterior íntegro en
[NEOCORTEX_SHARED_BLOCKS_2026-09-18.md](NEOCORTEX_SHARED_BLOCKS_2026-09-18.md).
