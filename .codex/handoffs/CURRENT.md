# Handoff operativo vigente — NeoCortex

**Ronda:** NEO-AUDIT-UNIFIED-20260918.
**Actualización:** 2026-09-18T04:31:03.917000+00:00.
**Objetivo autorizado:** corregir las auditorías de limpieza, EndToEnd y Text,
publicar e integrar el código remoto, construir e instalar la suite offline y
validarla en un laboratorio aislado. La autorización incluye preparación local
de modelos y herramientas; no requiere nuevas confirmaciones de esta ronda.

## Fuente y publicación

El remoto verificado al iniciar es `victor982721-lab/Neocortex-Linux`,
`main=f3bbf439d7c5b192f1509b1675bc64a139602320`. La candidata
`2a5a4d13577b8e79af23d4b15c97eedf61fdf25f` está publicada en
`fix/unified-audit-20260918`; la integración de esta ronda a `main` permanece
pendiente de la aceptación final. No se reescribe historia. El cierre exige
consultar otra vez el remoto y verificar `HEAD == main == origin/main` y árbol
limpio; la referencia inicial no acredita ese cierre.

## Cambios integrados y aceptación

El código implementa el inventario de lifecycle y sus owners, reset con
recuperación durable y preservación de cambios concurrentes, compensación de
artefactos, mantenimiento exacto y acotado, observación de scratch con checkpoints,
preparación global de rutas, búsqueda FTS Text, orden semántico y vector search
reemplazable, y distribución Linux con identidad nativa de SQLite acreditada.
El detalle de los 34 hallazgos y 63 requisitos transversales se conserva en el
paquete unificado de la ronda, junto a parches, fuentes, hashes y pruebas.

La revisión independiente de recuperación cerró sus nueve hallazgos. El ensayo
H11 observó un millón de archivos físicos vacíos con hashes, cancelación y
reanudación: cero duplicados u omisiones, unos 71 segundos y 42,45 MiB de RSS.
Su alcance es namespace/checkpoints, no rendimiento de un corpus voluminoso.

La suite integral R2 fue interrumpida para corregir causas compartidas: 2.169
pruebas pasaron, 80 fallaron, 47 se omitieron y 6.333 no se ejecutaron. No es un
gate aprobado. Se identificaron el fence ReviewTask 22 frente a Framework 23,
el contrato exacto de imports diferidos y un basetemp del runner fuera de TMPDIR.
La espera de Text se resolvió liberando cache de archivos propios: cuatro
canarias públicas pasan con mediciones reales, sin cambiar el código de admisión
ni el presupuesto. Los seis casos de aislamiento temporal pasan con basetemp
dentro de TMPDIR. El esquema ReviewTask, su compatibilidad y curación tienen
regresiones focales finales; arquitectura pasa nueve comprobaciones y docs/export
pasan 69. Knowledge reabre la lectura cercada entre observaciones; 64 pruebas
focales de snapshot/salud/kernel pasan conservando zero-copy y límites. La nueva
suite completa y la aceptación instalada siguen pendientes.

## Instalación y entorno

El laboratorio usa CPython 3.14.7, SQLite 3.53.1, dependencias del lock cp314 y
modelos locales. Cinco modelos tienen procedencia y hashes comprobados; nueve
comprobaciones funcionales de motores/modelos pasaron con sockets IPv4/IPv6
bloqueados por seccomp y sin telemetría. El conjunto incluye inferencias
FastEmbed, transcripción Whisper, PDF/QPDF, OCR en español, FFprobe y Qt offscreen.

La primera instalación de la candidata rechazó correctamente un enlace externo
de Python. El instalador se corrigió para materializar copias del ejecutable,
manteniendo el rechazo de enlaces ajenos y la identidad nativa aprobada después
del rename. La repetición de instalación, verify y aceptación de la interfaz
instalada permanece pendiente. No hay una promoción de esta ronda certificada.

Este laboratorio no es la máquina Kubuntu del usuario: su Corpus, releases,
estado, sesión Plasma/KIO y entorno personal no se han abierto ni modificado.
El resultado offscreen no certifica una sesión gráfica real. Las evidencias,
activos y herramientas del laboratorio permanecen fuera del árbol productivo.

## Siguiente gate

1. Cerrar las causas compartidas y ejecutar la suite integral con fixtures aisladas.
2. Publicar la candidata corregida; instalar y verificar bajo red denegada,
   conservando receipts y rollback, y validar la interfaz pública instalada.
3. Integrar a main, comprobar el remoto vivo y actualizar este handoff con los
   resultados verificables; entregar un ZIP, un plan y un prompt únicos.

El handoff anterior queda conservado en
[NEOCORTEX_STATE_RESET_2026-09-17.md](NEOCORTEX_STATE_RESET_2026-09-17.md).
Sus rutas personales, receipts y afirmaciones de instalación son historia; no
constituyen evidencia de lo realizado en el laboratorio de esta ronda.
