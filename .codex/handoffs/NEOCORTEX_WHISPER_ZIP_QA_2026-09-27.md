# Handoff — Whisper, ingestión ZIP y QA CPython 3.13

**Ronda:** NEO-WHISPER-ZIP-QA-20260927.
**Estado:** corrección publicada, release instalada y verificada; corpus real no reanudado.
**Commit de código:** `d854809926cac73d34eb082791623d23914f5d50`.
**Release activa:** `0.14.1-d854809926ca-cp313-linux-x86_64`.
**Rollback:** `0.14.1-9ce341cf2f1d-cp313-linux-x86_64`.

Whisper compara los cuatro campos de identidad efectiva de su firma, no la
observación de cantidad de GPU cuando procesa en CPU. Versiones/dispositivo/
tipo de cómputo distintos y runtimes malformados siguen siendo rechazados.
La diferencia padre/worker se reprodujo con el límite de dirección de 4 GiB.

ZIP Intake ya precedía a Identify y las rutas. Se corrigieron su agrupación
visual, conteos de contenedores y cierre del lote: una fase interna de miembros
no lo deja terminado pero mostrando En curso. La aceptación usa ZIP genérico
anidado, paquete atómico, publicación, orden, replay y fallos privados.

QA dispone de CPython 3.13.15 en un venv dedicado fuera del runtime:
`/home/ubuntu/.local/share/Neocortex/tooling/quality-cp313/bin/python`.
Los locks y provenance activos no sustituyen el inventario histórico CPython
3.14 ni los locks del producto. La descarga de los cuatro analizadores fue
expresamente autorizada; sus 74 ruedas están autenticadas. Semgrep usa
`EIO_BACKEND=posix --jobs 1` en este host, sin modificar límites globales.

## Verificación y límites

- 248 pruebas focales aprobadas, 1 exclusiva de Windows omitida, 2 subtests.
  Incluyen 5 de identidad de la fuente con metadatos generados desde ella.
- Ruff pasa. Semgrep ejecutó 6 reglas sobre 4 archivos productivos, 0 hallazgos.
  Mypy 94→94 y Pyright 17→17: sin nuevos diagnósticos, no globalmente limpios.
- Revisión independiente `archive_review`: W1/Z1/Q1 aceptados. El hallazgo de
  dispositivo malformado no hashable se corrigió y volvió a probar.
- Instalador/verify existente: SHA exacto, `verified=true`, runtime SQLite
  acreditado, corpus de smoke `ephemeral_empty_v1`.
- API instalada: ZIP publicado antes de Identify/rutas, replay sin repetir
  efecto y originales/paquete atómico preservados en fixtures privados.
- CLI instalada: audio sintético de 1 s con modelo local CPU/int8, 1 procesado,
  0 errores; repetición con 1 cache hit, firma igual y fuente intacta.
- La corrida productiva 4 permanece fallida; Audio es la única ruta fallida.
  ZIP Intake tuvo 1781 paquetes atómicos y 9 abstenciones (7 límites, 1 colisión,
  1 corrupción), sin extracción/publicación ZIP. No se relajaron protecciones
  ni se ejecutó `--all --apply` real. Organización/Semantic siguen pendientes.

Evidencia e informe: `/home/ubuntu/Documents/NeoCortex/Auditorias/2026-09-27-whisper-zip-qa/`.
El cambio previo de AGENTS.md quedó intacto y fuera del commit. Para construir
se usó un clon limpio del main publicado con el mismo SHA; el checkout canónico
no se declaró limpio mientras conserva ese cambio ajeno. Una actualización
posterior exclusivamente de estos handoffs no cambia los inputs ejecutables
validados ni la identidad del código instalado.
