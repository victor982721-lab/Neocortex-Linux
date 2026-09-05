# Handoff NeoCortex 0.13.0 — lifecycle durable, Semantic y snapshots bounded

## Estado verificado

- Corrección funcional publicada por fast-forward en `main`: `deb461d596a62797b5af24ee640de2e9c4245586`.
- `HEAD == main == origin/main` y árbol limpio después de la publicación funcional.
- La release instalada desde ese SHA es `0.12.0-deb461d596a6-cp314-linux-x86_64`; el rollback inmediato retenido por la herramienta es `0.12.0-aeb293e8581f-cp314-linux-x86_64`.
- Receipt: `/home/winterboss/.local/state/Neocortex/state/installation-receipts/20260905T215428.815187Z-install-0.12.0-deb461d596a6-cp314-linux-x86_64.json`.
- `release_linux.py verify --corpus-root /home/winterboss/Documentos/NeoCortex/Corpus` devolvió `verified=true`, con manifest, launcher, `source_sha`, current y retención de dos releases alineados; el contrato vigente no crea un symlink `rollback`, la release previa se conserva como directorio inmediato identificado en el receipt.

## Cambios funcionales

- `neocortex.lifecycle-stage/v1` enlaza el manifest inmutable del Framework con etapas de Semantic, resultado, base local y observación read-only del estado cross-owner, sin abrir ni mutar owners Semantic desde el lector.
- `mark_abandoned_runs()` recupera también una etapa Semantic `running` cuando el Framework padre ya había terminado, y la transición es idempotente ante reapertura.
- Los `route_input_sources` de cada corrida quedan ligados antes de crear workers, los readers validan el digest del manifest y `begin_operational_run()` rechaza una fuente todavía viva.
- Las rutas canceladas o fallidas conservan sus candidatos hasta una recuperación o publicación terminal; una prueba con 24 entradas ejecuta dos recuperaciones sin duplicar candidatos ni efectos observados.
- SQLite estricto respeta cancelación y deadline, los reintentos comparten el límite temporal, el checkpoint final verifica el límite, el writer rechaza snapshots que no caben antes del backup y `SQLiteSnapshotReuseCache` valida coherencia de parámetros, afinidad de hilo y capacidad.

## Validación local

- Focales Linux de la tranche: **160 pasadas**, sin fallos, con la segunda recuperación sobre 24 fixtures heterogéneos y MCP/API/SDK/status cubiertos.
- Focales SQLite: **31 pasadas**; `compileall`, `py_compile` y `git diff --check` correctos.
- Ruff no está disponible como binario en el entorno de esta sesión y queda `no verificado` para este SHA; CPython 3.13 también permanece `no verificado` por ausencia de intérprete local válido.
- Smoke instalado desde `/tmp` y fuera del checkout: dos corridas públicas `--all` sobre 24 fixtures, segunda corrida con `type_cache_hits=24`, ambos runs `completed` y etapa Semantic `running → completed`, sin tocar el corpus personal.

## Límites y siguiente gate

- Linux/Kubuntu sigue siendo el único objetivo, sin GitHub Actions, KIO real, MCP con escritura, proveedores remotos ni corpus personal.
- `NEO-EVO-004` permanece `EN_CURSO` hasta declarar explícitamente por ruta `phase_resume`, `safe_replay` o `not_resumable` y decidir si las publicaciones Semantic/Code deben pasar por una transacción cross-owner completa en lugar de la observación read-only actual.
- No se promovieron modelos ni se ejecutó una corrida sobre el corpus canónico; current y rollback se conservan únicamente como instalación y recuperación inmediata verificadas.
