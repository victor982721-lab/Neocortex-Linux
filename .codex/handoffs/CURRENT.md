# Handoff operativo vigente — NeoCortex

**Ronda:** NEO-AUDIT-UNIFIED-20260918.
**Actualización:** 2026-09-18T10:05:06.715953+00:00.
**Objetivo autorizado:** corregir las auditorías de limpieza, EndToEnd y Text,
publicar e integrar el código remoto, construir e instalar la suite offline y
validarla en un laboratorio aislado. La autorización incluye preparación local
de modelos y herramientas; no requiere nuevas confirmaciones de esta ronda.

## Fuente y publicación

El remoto verificado al iniciar es `victor982721-lab/Neocortex-Linux`,
`main=f3bbf439d7c5b192f1509b1675bc64a139602320`. La candidata
`127ab8df69ebfb21bbfd05eb1b5255400f25caa8` está publicada en
`fix/unified-audit-20260918`; la integración de esta ronda a `main` permanece
pendiente de la aceptación final. Este commit estabiliza las dos fixtures identificadas en R5; su SHA se obtendrá de Git después de publicarlo. No se reescribe historia. El cierre exige
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
suite completa posterior a esas correcciones se registra más abajo.

R3 recogió 8.635 casos y se interrumpió por presión de cache durante una
instalación concurrente: 759 pasaron, tres fallaron, seis se omitieron y 7.867
quedaron sin ejecutar. Sus tres fallos de pipes se reprodujeron también en la
base. La fixture ahora espera una señal explícita del descendiente después de
`setsid` y conserva los plazos y las aserciones: 15 repeticiones frías y cinco
casos independientes pasan; el módulo completo pasa 19, con tres omisiones
exclusivas de Windows. R4 debe ejecutarse sin instalaciones ni empaquetado en
paralelo. No se modificó la admisión de recursos ni el runner productivo de
procesos para obtener estos resultados.

La aceptación instalada detectó una salida plana de Tesseract aceptada como
OCR sin texto. El bundle local ya incluye sus configuraciones oficiales; el
parser comprueba ahora las doce columnas TSV, rechaza cabeceras inválidas y
conserva el caso válido sin palabras. El contrato v4 cambia las firmas Image
y Video, incluido el modo OCR deshabilitado por su composición existente,
sin alterar Text, schemas ni fingerprints originales. Pasan 38 pruebas y siete
subpruebas del autor y ocho casos independientes con siete subpruebas; la
revisión independiente está conservada en la evidencia de la ronda. La
preparación previa de R4 sobre 8b5e3b7 quedó superada sin ejecutar tests.
R4 y la aceptación instalada 04 acreditaron la fuente f3b01845 con este arreglo.

La comprobación previa a instalar esa candidata detectó cachés Python añadidas
en la release 60ea. Al excluir sólo esas entradas, el digest coincidió exactamente
con el manifest; se retiraron las cachés y se restauraron los modos. Las sondas
aisladas de inventario, pip y SQLite ahora pasan `-B` porque `-I` ignora
`PYTHONDONTWRITEBYTECODE`. Dos regresiones reales con venv escribible y carga
desde `.pth` fallaron antes del arreglo por cambio del árbol. La aceptación
headless y el guion externo también usan `-I -B`. Este incidente tiene causa
separada de la reaparición de staging y no justifica relajar el digest.

## Resultado integral R4 y correcciones de cierre

R4 sobre `f3b01845dec901c7927ef4185a59096c3d01f271` ejecutó los 8.642 casos
recogidos: 8.559 aprobados, 73 omitidos y diez fallidos, sin casos pendientes;
49 subpruebas aprobaron y se cuentan aparte. Ninguno de los 1.476 archivos de
fuente cambió durante la validación. No se excluyeron capacidades; tres módulos
exclusivos de Windows se excluyeron antes del import. R4 no es un gate aprobado.

La denegación por exceder el límite de serialización del grant se transforma en
`CurationAuthorizationError`, también para metadatos raíz inválidos, antes de
publicar autorización o acciones. API conserva `authorization_denied` y exit 1;
la CLI sólo anuncia un grant cuando la respuesta completa contiene su ID. El
límite de 65.536 bytes, la expansión de todos los miembros y los originales se
conservan. Las regresiones distinguen autorización completa dentro del límite,
denegación sin efectos y clasificación de la identidad raíz inválida.

Los otros fallos se analizaron por contrato. Scratch conserva su control durable
y tombstones al retirar workspaces; Framework usa su versión canónica 23; los
dos módulos semánticos nuevos tienen identidad y origen explícitos. La fixture
del benchmark se crea bajo su raíz real `/tmp`; inventario ya no depende del
orden de readdir para observar ambos límites. DOCX admite el rechazo tipado por
quick_check sin exigir una excepción SQL subyacente, y conserva la comparación
de bytes del SQLite/WAL/SHM. El timeout de readiness del proceso aislado no se
reprodujo en 20 ensayos (15 fríos y cinco independientes); se añadió diagnóstico
al fallo sin cambiar sus plazos ni el código productivo de procesos. Su causa
permanece sin demostrar. No se consideran esos nueve casos nueve bugs nuevos
de producto ni se rebajan los presupuestos para obtener un resultado verde.

El focal de once módulos cerró 125 aprobados y una omisión de benchmark instalado
opt-in, en 35,79 segundos. Ruff aprobó los trece archivos Python modificados.
La siguiente corrida integral R5 debe acreditar esta fuente exacta; no se
solapará con instalaciones o empaquetado pesado.

## Resultado integral R5 y fixtures estabilizadas

R5 sobre `127ab8df69ebfb21bbfd05eb1b5255400f25caa8` ejecutó sus 8.644 casos:
8.569 aprobados, 73 omitidos y dos fallidos; no quedó ninguno sin ejecutar.
Las 49 subpruebas aprobaron y se cuentan aparte. Los diez fallos de R4 ya no
aparecieron y los 1.476 archivos de la copia de fuente permanecieron intactos.
R5 conserva su resultado no aprobado; sus dos fallos no se borran de la evidencia.

La fixture de AgentActivity lanzaba dos intérpretes dentro del plazo de 0,4 s;
la nueva usa un único intérprete con fork y publica el PID real del descendiente
desde el padre. Conserva el plazo, el límite total de dos segundos, el estado
failed-retained y las comprobaciones del descendiente y de los pipes. En R5 ya
habían pasado las tres primeras aserciones; faltó child.ready para comprobar el
PID. No se demostró un fallo de la terminación productiva ni la causa exacta de
la demora de esa ejecución.

Los helpers de metadata y schema de las pruebas SQLite usaban el contexto de
transacción de Connection sin cerrarla. Una reproducción controlada sobre la
fuente R5 fuerza GC entre la captura del fence y la apertura del guard: retira
SHM y reproduce exactamente FileNotFoundError/OperationalError. Con
contextlib.closing, el mismo probe pasa y no desaparece SHM entre comprobaciones.
El cierre productivo, las fences, el DDL v0, la metadata y WAL/DELETE se conservan.

Los tres módulos afectados pasan 80 pruebas y omiten tres exclusivas de Windows;
Ruff pasa ambos archivos modificados. Estas correcciones cambian sólo fixtures,
no código productivo. R6 debe acreditar la nueva fuente congelada antes del cierre.

## Instalación y entorno

El laboratorio usa CPython 3.14.7, SQLite 3.53.1, dependencias del lock cp314 y
modelos locales. Cinco modelos tienen procedencia y hashes comprobados; nueve
comprobaciones funcionales de motores/modelos pasaron con sockets IPv4/IPv6
bloqueados por seccomp y sin telemetría. El conjunto incluye inferencias
FastEmbed, transcripción Whisper, PDF/QPDF, OCR en español, FFprobe y Qt offscreen.

La primera instalación de la candidata rechazó correctamente un enlace externo
de Python. El instalador se corrigió para materializar copias del ejecutable,
manteniendo el rechazo de enlaces ajenos y la identidad nativa aprobada después
del rename. La instalación de `60ea7bea3711d3c7a07dce4c159bb23021c67f00` terminó
con cinco modelos preparados y un recibo real. `candidate-verify-04` pasó con
`verified=true`, fuente y wheelhouse acreditados, y runtime sólo de producto.

El laboratorio conservó en cuarentena árboles temporales que reaparecieron con
inodos nuevos después de un movimiento. Retiró los duplicados conocidos bajo
lock y restauró permisos de sólo lectura tras verificar todos los bytes contra
el manifest; una comprobación posterior conserva staging vacío y modos 0555.
No se estableció una causa definitiva de la reaparición ni se atribuye sin
reproducción a un defecto de producto. Los logs del incidente se preservan fuera
del repositorio. La aceptación instalada 04 de f3b01845 pasó sus once etapas bajo denegación real
de red: identidad, comprobación headless (ocho aprobadas y una omisión de la
variante negativa sin motores), Text, mantenimiento, reset, migraciones,
recuperación, Semantic, Audio y modelos. La instalación y verificación canónicas
pasaron con rollback retenido. Es evidencia de esa candidata; el artefacto final
con las correcciones de R4 requiere nueva instalación y aceptación.

En observaciones posteriores, el enlace current volvió a apuntar a la release
60ea y reaparecieron copias de staging con inodos distintos. Los ensayos acotados
de reemplazo atómico persistieron en procesos nuevos, sin demostrar una causa
para esa reaparición. El cierre requiere comprobar en un proceso nuevo el destino
current, su recibo y digest; no basta con un registro anterior aprobado.

Se preparó Noto Sans con procedencia para las fixtures de OCR del contenedor.
Los wrappers locales de QPDF y Tesseract resuelven su destino al invocarse por
symlink con PATH restringido; las pruebas conservan los requisitos de idiomas.
Estas preparaciones pertenecen al laboratorio y no al equipo del usuario.

Este laboratorio no es la máquina Kubuntu del usuario: su Corpus, releases,
estado, sesión Plasma/KIO y entorno personal no se han abierto ni modificado.
El resultado offscreen no certifica una sesión gráfica real. Las evidencias,
activos y herramientas del laboratorio permanecen fuera del árbol productivo.

## Siguiente gate

1. Ejecutar R6 completa sobre la candidata con las fixtures estabilizadas de R5.
2. Publicar la candidata corregida; instalar y verificar bajo red denegada,
   conservando receipts y rollback, y validar la interfaz pública instalada.
3. Integrar a main, comprobar el remoto vivo y actualizar este handoff con los
   resultados verificables; entregar un ZIP, un plan y un prompt únicos.

El handoff anterior queda conservado en
[NEOCORTEX_STATE_RESET_2026-09-17.md](NEOCORTEX_STATE_RESET_2026-09-17.md).
Sus rutas personales, receipts y afirmaciones de instalación son historia; no
constituyen evidencia de lo realizado en el laboratorio de esta ronda.
