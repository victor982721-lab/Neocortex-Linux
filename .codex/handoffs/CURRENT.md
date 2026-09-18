# Handoff operativo vigente — NeoCortex

**Ronda:** NEO-PERFORMANCE-FIXES-20260918.
**Actualizado:** 2026-09-18T20:04:10.074113+00:00.
**Base remota autenticada:** `c0b7ae873a5c29a518582e624d29d2587cc759c3`.
**Árbol de código/tests aceptado:** `a4ae1e7d29fe2a9b0871a235772d54bea20117b8`.

## Alcance y resultado

El usuario autorizó aplicar en el repositorio remoto las correcciones de los
17 hallazgos de velocidad, estabilidad, caché y aprovechamiento de contratos.
Se implementaron mediante seis agentes distribuidos por dominios, con un solo
responsable de integración/Git y revisión independiente de fronteras críticas.
La publicación va directamente a `main`, sin ramas, PR, force push ni Actions.
Esta ronda publica fuente; no incluye una nueva instalación.

Las mejoras conservan identidad, evidencia, publicación completa, revalidación
junto al efecto, cancelación y abstención ante incertidumbre. En particular:

- Code calcula el digest agregado por streaming, indexa identidad FTS y reutiliza
  publicaciones mediante recibos ligados al productor original.
- Audio evita ffprobe en hits vigentes; Office/Audio/Video evitan escrituras FTS
  idénticas; seis owners usan lookups TEMP tipados bajo ownership de writer.
- Text transporta una representación descomprimida validada y revalida su
  observación bajo transacción antes de publicar.
- Dedup reutiliza hashes completos dentro de una planificación tras revalidar
  identidad/ctime; Inventory portable conserva el scan sin reinsertar files
  cuando el recorrido íntegro demuestra que no cambió.
- Knowledge comparte presupuesto durante las operaciones y reusa observaciones
  sólo dentro del intento estable; Semantic usa recibos acotados ligados a los
  fences de fuente y destino. El sidecar derivado participa del reset exacto.
- Framework indexa candidatos, propaga source_root, conserva organización
  pendiente con recuperación durable y libera sucesores por dependencia concreta.
- SQLite permite cancelación durante preparación fuente y restaura callbacks;
  el governor evita descontar otra vez la CPU propia y estima conservadoramente
  caché inactiva limpia recuperable sin retirar cuotas, reservas ni memory.high.

## Validación exacta

El cierre por nodeid acredita **8.840 casos únicos: 8.767 aprobados, 73 omitidos,
0 fallidos y 0 pendientes**. La colección final reproduce los mismos 8.840
identificadores, con todas las capacidades seleccionadas y tres módulos Windows
excluidos antes de importación. Las omisiones y subpruebas se registran por
separado. La identidad de importación pasó 5/5, sin omisiones; los metadatos se
generaron desde la misma fuente privada con CPython 3.14.7/SQLite 3.53.1.

No fue una única corrida completa verde. La pasada integral histórica terminó
por interrupción controlada con 8.273 aprobados, 73 omitidos, 103 fallidos y
391 pendientes. Sus logs y resultados permanecen intactos. Faltaba espacio en
el laboratorio y algunas fixtures reflejaban contratos anteriores o reservas
inadecuadas para sus entradas pequeñas. Tras retirar sólo temporales de corridas
terminadas, la fuente final repitió 53 módulos completos: **639 aprobados y
1 omitido en 219,98 s**. Cubren todos los fallos, pendientes y fixtures cambiadas.
SHA256 demuestra que el único delta respecto de la fuente integral son siete
fixtures revisadas independientemente; no cambió producción ni configuración.
Se conservan aserciones, baselines exactos, gobernador y planner propietario.

Ruff pasó en 124 archivos Python modificados. Mypy comparó 69 fuentes comunes
de base y 81 actuales siguiendo imports: 55 diagnósticos previos, 54 actuales,
cero nuevos y uno retirado. Las 54 incidencias restantes son deuda previa.

La evidencia externa de esta ronda conserva `acceptance-integrated-01/`,
`acceptance-final-delta-04/`, matriz de 17 contratos, revisiones, probes,
mediciones, manifests y `publication/closure.json`. El cierre exige lectura
fresca de GitHub, `HEAD == main == origin/main` y árbol limpio. El SHA final
se obtiene de Git y del informe de entrega, sin autorreferencia circular aquí.
El delta posterior a la fuente aceptada sólo actualiza este handoff y archiva
el anterior.

## Compatibilidad y límites

Framework migra 23→24; Code 7→8; Inventory 14→15. Sus consumidores y validadores
legacy mantienen contratos exactos. El sidecar Semantic es opcional, derivado,
acotado a 2 MiB/64 entradas; corrupción o drift fuerzan revalidación.

Las mediciones sintéticas prueban trabajo evitado, no una aceleración global:
5→3 lecturas completas de hash en tres archivos de 9 MiB; 1.000→0 escrituras
files en replay portable idéntico; 2→1 descompresiones Text; 8→1 validaciones por
owner en Knowledge. Las publicaciones modificadas de Code/Inventory mantienen
copias completas y Catalog conserva verificaciones O(N) bajo BEGIN IMMEDIATE.

Queda una limitación preexistente observada fuera de los 17 hallazgos: Image
puede esperar dos timeouts de admisión consecutivos al limpiar dos trabajos
encolados con un worker y memoria insuficiente. El probe reproduce el mismo
comportamiento en la base y en la fuente actual. Su futura corrección debe
cancelar la cola de forma cooperativa preservando excepción y cleanup.

No se modificaron corpus, modelos, estado ni instalación personal. Las pruebas
Linux de este laboratorio no certifican sesión Kubuntu/Plasma/KIO real, Windows
ni los escenarios nativos que las fixtures declaran omitidos.

El handoff anterior se conserva íntegro en
[NEOCORTEX_UNIFIED_AUDIT_2026-09-18.md](NEOCORTEX_UNIFIED_AUDIT_2026-09-18.md).
