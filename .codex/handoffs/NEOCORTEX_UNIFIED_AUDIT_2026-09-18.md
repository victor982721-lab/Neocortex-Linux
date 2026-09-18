# Handoff operativo vigente — NeoCortex

**Ronda:** NEO-AUDIT-UNIFIED-20260918.
**Actualizado:** 2026-09-18T11:08:20.876225+00:00.
**Fuente integral acreditada:** `51b345cff039dd62ccf35a84c40148c0ed5c2078`. Este cambio sólo actualiza el handoff;
el código, los tests, los locks y las herramientas coinciden con esa fuente.

## Alcance autorizado y resultado de fuente

Se implementaron las correcciones de las auditorías de limpieza, EndToEnd y
Text. La autorización del usuario comprende publicación, integración a main,
instalación y validación offline en un laboratorio privado y entrega de un ZIP,
un plan y un prompt únicos; no requiere nuevas confirmaciones para esta ronda.
El remoto inicial autenticado fue `victor982721-lab/Neocortex-Linux`,
`main=f3bbf439d7c5b192f1509b1675bc64a139602320`. Se conserva su historia mediante
commits sucesivos en `fix/unified-audit-20260918`, sin force push.

R7 terminó con **8644 casos únicos recogidos:
8571 aprobados, 73 omitidos,
0 fallidos y 0 sin ejecutar**.
Las subpruebas se cuentan por separado: `{"passed": 49}`.
El resultado exacto, los nodeids, motivos de omisión, JUnit, logs y el manifest
de los 1.476 archivos congelados se conservan en
`work/implementation/full_validation/acceptance-r7-final/` del paquete de entrega.
No se excluyeron capacidades del producto. Los límites de plataforma y fixtures
opcionales siguen identificados en esos recibos; no equivalen a pruebas Windows.

Ruff pasó en los archivos Python modificados. Mypy comparó 83 archivos afectados
de producción/herramientas: 58 diagnósticos en la base, 37 actuales, cero nuevos
y 21 retirados. Las 37 incidencias previas permanecen como deuda; esto no es una
certificación de tipado limpio de todo el repositorio.

## Correcciones concretas incorporadas

- Lifecycle de 13 owners, reset operacional con barreras de frescura, recuperación
  durable, separación de selección/resultado físico y preservación de cambios
  concurrentes. Text/Semantic/Code protegidos se rechazan antes de efectos cuando
  no existe una transformación autorizada; el reset no promete borrar todo.
- Manifiesto y journal Archive durables independientes del estado derivado,
  prueba de reconstruibilidad por identidad/SHA/CRC/tamaño y parentesco
  estructural; compensación y retiro sólo después de acreditar el contrato.
- Scratch registrado, adopción exacta, sellado que incluye manifests anidados,
  política POSIX por descriptor, observación incremental con cancelación y
  checkpoints, cuotas, límites de montaje y abstención tipada ante encoding no
  soportado. El control durable y tombstones legítimos se conservan.
- Preparación nativa de rutas integrada en el orquestador, antes de efectos,
  con deadline y cancelación; aislamiento KIO y contratos públicos de lectura.
- Text usa lookups TEMP indexados por conexión writer y mantiene rowid, FTS,
  identidad, schema durable y cache hits. No requiere reprocesamiento masivo.
- Orden/rank semántico global coherente con top K y proyección vectorial exacta
  sustituible. El benchmark compara el backend nativo y persistido; no introduce
  ANN ni atribuye calidad del Corpus a un microbenchmark.
- Parser de imports relativos corregido: cero ciclos eager y nueve conjuntos
  diferidos exactos, con miembros/aristas/contextos revisados. El DTO trasladado
  conserva su alias y compatibilidad; los ciclos retenidos no se presentan como
  eliminados.
- Grants que exceden 65.536 bytes o tienen metadata inválida devuelven
  CurationAuthorizationError antes de publicar autorización/acciones. La API
  mantiene authorization_denied/exit 1 y la CLI sólo anuncia un grant completo
  con ID. No se amplió el límite ni se truncó la evidencia autorizada.
- Distribución Linux con copias del ejecutable de venv, identidad nativa SQLite
  acreditada y probes readonly `-I -B`, también en headless; `-I` por sí solo no
  respeta PYTHONDONTWRITEBYTECODE. Digest y modos inmutables siguen siendo gates.
- OCR TSV v4 exige las doce columnas únicas, permite reordenación/extensiones
  y TSV válido sin palabras, y rechaza formato plano inválido. Las firmas Image
  y Video incorporan el contrato sin migrar el schema Text ni los originales.

## Evidencia histórica y fixtures

La revisión independiente de recuperación cerró nueve defectos. R4 ejecutó
8.642 casos: 8.559 pass, 73 skip, 10 fail; R5 ejecutó 8.644: 8.569 pass,
73 skip, 2 fail; R6 ejecutó 8.644: 8.570 pass, 73 skip, 1 fail. Cada corrida
terminó sin casos pendientes y con 49 subpruebas aprobadas; sus fallos y
diagnósticos se conservan como historia, sin reetiquetarlos como verdes.

Las correcciones de fixture mantuvieron los presupuestos y contratos del
producto: cierre explícito de conexiones SQLite de prueba mediante
contextlib.closing; un intérprete/fork con PID real para AgentActivity; y
`-I -S -B` más `os._exit(0)` después del ACK en retained pipes. Esta última
conserva 0,25 s, cota total menor de 0,5 s y cleanup incomplete; pasó 19 casos
focales (3 skips Windows) y 15 procesos fríos antes de R7. La reproducción GC
demostró la carrera SHM de la fixture SQLite; las demoras particulares de
readiness/ACK de los otros casos no tienen una causa productiva demostrada.

H11 observó un millón de archivos físicos vacíos con cero duplicados/omisiones,
cancelación y reanudación en otro proceso: 71,098 s y 42,45 MiB de RSS. Es una
medición de namespace/checkpoints; no de un millón de documentos voluminosos.
El intento posterior de retirar esa fixture no acreditó postcondición estable,
aunque un millón de unlinks devolvieron éxito; se conserva como incidente de
laboratorio sin atribución causal al producto.

## Distribución e instalación de la misma implementación

El cierre operativo se acredita con los recibos externos del paquete:
`release/final-closure-01/RESULTADO.json`,
`installed_validation/acceptance-05/RESULTADO.json`,
`release/FINAL_REMOTE_STATE.json` y `CIERRE_ESTADO.json`. La secuencia exige
R7 aprobada, delta exclusivamente documental, instalación canónica, verify,
once etapas instaladas bajo denegación física IPv4/IPv6 y verificación posterior
en un proceso nuevo; después integra main mediante fast-forward y comprueba
HEAD/main/origin-main y árbol limpio. La presencia de este handoff no sustituye
esos recibos. El SHA de esta actualización se obtiene de Git y de la entrega,
sin una referencia circular en el propio commit.

El paquete incluye CPython 3.14.7/SQLite 3.53.1, 68 wheels de producto/build,
diez wheels adicionales de calidad, cinco modelos locales con hashes y
procedencia, herramientas nativas y fixtures pequeñas. Es una distribución para
un host Linux compatible; no una imagen completa del sistema operativo.

La aceptación instalada anterior 04 acreditó la fuente f3b01845 y sus once
etapas. Observaciones posteriores mostraron current apuntando otra vez a 60ea
y staging reaparecido con inodos nuevos. No se estableció la causa; el cierre
final exige una lectura fresca del enlace, recibo y digest y no recicla aquella
aceptación como prueba de la instalación final.

## Límites que permanecen explícitos

El Corpus, releases, estado y sesión Kubuntu/Plasma/KIO del usuario no se han
abierto ni modificado. Qt offscreen no acredita una sesión gráfica personal.
Este contenedor no permite un UID ordinario representativo, montajes/bind reales,
cgroups delegados ni crear AF_UNIX; sus contratos se prueban hasta el alcance
que detallan las fixtures, sin afirmar acreditación nativa de esos escenarios.

La evaluación léxica sintética de 48 casos terminó en abstención completa,
cobertura cero y diez omisiones de positivos. Es evidencia de límites de esas
reglas; no demuestra calidad semántica del Corpus real. El backend vectorial y
las inferencias offline tienen sus pruebas funcionales separadas.

El plan de entrega mantiene 34 hallazgos y 63 requisitos transversales con causa,
receta, archivos/símbolos y evidencia. Son contratos de distinto alcance;
8.644 tests compartidos no se convierten en 97 certificaciones independientes.
El handoff anterior permanece en
[NEOCORTEX_STATE_RESET_2026-09-17.md](NEOCORTEX_STATE_RESET_2026-09-17.md), como
historia y sin atribuir su instalación personal al laboratorio de esta ronda.
