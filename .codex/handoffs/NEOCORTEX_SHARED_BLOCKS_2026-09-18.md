# Handoff operativo vigente — NeoCortex

**Ronda:** NEO-NEXT-PERFORMANCE-20260918.
**Actualizado:** 2026-09-18T22:07:14.513659+00:00.
**Base remota autenticada:** `0a78108d7b17ce0799198bf013a62bf06a9b2584`.
**Árbol de fuente/tests aceptado:** `c41d432ba109f5a95cff25a7b287e4013457e11d`.

## Alcance y resultado

El usuario autorizó continuar en el repositorio remoto las cuatro mejoras
pendientes del cierre anterior: limpieza de admisión Image, copias de generaciones
Code, materialización Inventory y validación Catalog dentro del writer. Se
implementaron con autores por dominio y revisiones independientes, preservando
un único responsable de Git/integración y publicación directa en main.
La fuente publicada no equivale a una nueva instalación del producto.

- Image señala cancelación antes de esperar el executor, cancela trabajos
  pendientes, cierra todos los decoders y conserva la excepción original y los
  resultados ya consumidos. Registry comparte un token hijo entre Image y su
  gate: hereda la cancelación de Framework sin transmitir un fallo local al
  padre ni a rutas hermanas. El coordinador consulta el token de cada petición
  durante la cola y antes de conceder recursos.
- Code migra aditivamente 8→9 y graph generation 1→2, con seis tablas derivadas
  de bloques compartidos y manifiestos completos. Valida payload, digest y
  vínculo al productor original antes de reutilizar; sus lectores comprueban
  también inputs y membresías. Conserva lectura directa graph v1/Code v8 y
  todos los payloads/digests anteriores tras la migración. La retención protege
  bloques referenciados por heads y builds, aunque el productor esté podado.
- Inventory portable materializa una vez la observación actual completa y
  calcula su digest fuera del writer. Revalida identidad, checkpoint, revisión,
  summary y digest bajo lock; conserva historia, planes, rollback y presupuestos.
- Catalog valida replay y prepara digests/recuentos en una transacción lectora.
  Antes del efecto compara el fence de conexión/archivo, fuente, raíz y CAS.
  La adquisición del writer y el registro de una cancelación tienen esperas
  acotadas y preservan el timeout original y el error primario.
- Dedup aplica sus límites sobre la lectura efectiva legacy/bloques antes de
  materializar; Knowledge observa formas exactas Code v7/v8 sin migrarlas.
  Diagnóstico, manifiesto de capacidades y reset reconocen Code v9.

La matriz externa conserva once contratos aplicables. Se amplió de diez a once
al demostrar que cancelar el token compartido de Image podía ocultar el fallo
original como interrupción del usuario y detener otras rutas. La revisión
independiente también reprodujo y cerró una segunda espera al registrar la
cancelación de Catalog y dos huecos de validación de bloques Code: inputs de la
publicación y procedencia de sus cabeceras.

## Validación exacta

La aceptación integrada ejecutó **107 módulos, 1.331 casos únicos**:
**1.296 aprobados, 35 omitidos, cero fallos y cero pendientes** en 137,08 s.
Las siete subpruebas aprobadas se cuentan aparte. La colección y JUnit coinciden
exactamente por identificador, sin duplicados ni casos faltantes. Las 35
omisiones corresponden exclusivamente a contratos Windows; no hubo omisiones
de capacidades ni deselected. Una advertencia previa de `record_property` con
JUnit xunit2 permanece registrada.

La copia privada completa contiene 1.510 blobs comprobados contra el árbol Git;
ningún input de fuente/tests cambió durante colección, pruebas o análisis.
Metadatos generados desde esa misma copia, identidad 5/5 sin omisiones,
CPython 3.14.7 y SQLite 3.53.1. El SDK MCP real está disponible en el tooling
privado y su prueba pública pasó. No se usaron corpus, modelos ni estado del
usuario ni se instaló una release.

Ruff pasó en los 29 archivos Python modificados. Mypy analizó 17 fuentes base
más sus imports frente a 18 actuales: reproduce los mismos 65 diagnósticos,
cero nuevos y cero retirados. Incluyen deuda previa y dependencias opcionales
de inferencia ausentes de este laboratorio; Mypy no se presenta como limpio.

Los focos de autor y los probes independientes permanecen en la evidencia,
sin sumarse de nuevo al total integrado. Se conservan también los intentos
anteriores: Inventory corrigió sólo la ubicación de basetemp dentro de TMPDIR;
el foco MCP inicial falló por SDK ausente y pasó tras preparar ese tooling;
Code descartó una copia incompleta de licencias antes de su aceptación final.

## Medición y límites

- Image, con el mismo timeout sintético de un segundo por admisión, pasa por
  Registry de 2,051 a 1,049 s. La espera después del primer error pasa de
  1,002 s a 0,00086 s; padre intacto, error original y cero reservas/cola.
  Los defaults de 60/300 s no se rebajaron.
- Code, con 1.024 archivos y tres pares alternados, reduce payload nuevo de
  12.290 a 3.485 filas al cambiar uno y de 12.278 a 2.281 al borrar uno.
  Las medianas de publicación son 626→454 ms y 636→453 ms respectivamente.
  Replay conserva cero escrituras, pero su mediana total es 231→245 ms;
  crear el esquema pasa de 182 a 232 ms. Se documentan esos costes adicionales.
- Inventory, al retirar el 90 % de 10.000 archivos, pasa de 10.000 inserts más
  9.000 deletes a 1.000 inserts; mediana de cinco pares 159,2→51,0 ms.
  Un solo cambio aún materializa N actual: N+1→N escrituras. Sus tiempos
  presentan rangos solapados y no prueban aceleración universal.
- Catalog, con 1.024 documentos, reduce el writer del replay de 132–137 ms
  a 0,32 ms, conservando un total cercano a 140 ms. Una publicación cambiada
  reduce el writer de 200,2 a 48,7 ms, pero mantiene la proyección O(N).

Las mediciones usan fixtures sintéticas en un entorno compartido. Code conserva
captura/validación O(N) y ordenación O(N log N); Inventory conserva observación
y materialización completas. Catalog conserva su RLock de proceso y proyección
atómica; no congela archivos externos después de comprobarlos individualmente.
La cancelación sigue siendo cooperativa. Los resultados completos aún no
consumidos de Image pueden requerir reintento.

## Evidencia y publicación

Evidencia externa: `acceptance-integrated-01/`, `acceptance-static-01/`, matriz
de contratos, handoffs de autores, revisiones y probes con hashes, mediciones
base/actual y `publication/closure.json`. El cierre de publicación exige lectura
fresca de GitHub, igualdad HEAD/main/origin/main y árbol limpio. El SHA final
se obtiene de Git y del informe de entrega, sin autorreferencia circular aquí.
El delta posterior al árbol aceptado sólo actualiza este handoff y archiva el
anterior íntegro en
[NEOCORTEX_PERFORMANCE_FIXES_2026-09-18.md](NEOCORTEX_PERFORMANCE_FIXES_2026-09-18.md).
