# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-29 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

> Modo de trabajo vigente: desarrollo rápido por lotes materiales, con controles
> focales durante la organización y un único gate canónico al congelar la cohorte;
> ningún candidato provisional tiene aceptación hasta contar con receipt local.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte Plataforma aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `ee4bdec1359fab0cc9fed18af6973f57f792bf33`
- Árbol verificado limpio; `main` sigue sin publicar. El snapshot de la cohorte
  foundation quedó publicado únicamente en `codex/neocortex-local-20260829`;
  no hay merge ni push a `main`.
- Archive, DOCX, Audio, Image, Office, PDF, Video y Text viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio,image,office,pdf,text,video}`.
  Las fachadas `_04_Nucleo_Operativo` correspondientes son compatibilidad
  explícita; las sondas runtime están en `neocortex/capabilities/runtime.py`.
- La cohorte compartida de plataforma vive físicamente en
  `neocortex/platform/{architecture_projection,capability_registry,capability_registry_specs,content_types,zip_safety}.py`.
  Las rutas `_04_Nucleo_Operativo.platform.shared.*` y los aliases planos de
  tipos/ZIP son fachadas de compatibilidad, sin una segunda implementación.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  las ocho familias hacia foundation/core. El registro canónico actual cubre 454 módulos productivos y su fingerprint es
  `core-architecture-target-v1:sha256:5b1f31c493ee544417c1ac44f59e84f26aaa4a0bc3fd5fb46530733997af99af`; los aliases legacy permanecen fuera de la implementación.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline e0f65f8ae2ca054a161b9a4100f7ab3b1a8c473e`
  sobre `ee4bdec` terminó `passed`: selección `full`, 338 pruebas reportadas,
  17 barreras, Coverage trusted-deep de 24 shards, experimentos, wheel,
  replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/ee4bdec1359fab0cc9fed18af6973f57f792bf33-44a05ed08f4da0b7fc5568952862ab1dc533a2bfa5ecb456b6acedbe3582aa9d.json`
  (`sha256:d6135e44d001e1aa8e269576671192171351bd83f0859cd36a72ed2daa0067c5`).
- El lote rápido posterior trasladó runtime, workflow, Knowledge, Semantic, Code,
  Documents, Safety, Inventory, Persistence y API a namespaces canónicos, con
  aliases legacy de identidad comprobada; los commits principales son `6305e98`,
  `e0b2137`, `4a366b0`, `ec5b7a1`, `d276b23`, `b1262ed`, `50f0e42`, `a741b41`,
  `3478e30`, `63955f3` y `eab777f`. Focales de migración, CLI, arquitectura,
  Semgrep, quality gate y selector de cambios pasaron; no existe aún receipt
  canónico integral ni release instalada para este árbol posterior.
- Trusted-deep publicó `run_id=56` con 987 archivos, 981 candidatos,
  `code_processed=0`, `code_cache_hits=981`, y el replay `run_id=57` conservó
  los mismos contadores, ambos sin errores.
- La release vigente se reconstruyó desde el SHA ejecutable aceptado:
  `0.9.0-ee4bdec1359f-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: un ZIP produjo
  `processed=1`, `cache_hits=0`, `complete=1`, `members=1`, `indexed=1` y
  `errors=0`; el replay mantuvo `processed=1`, `cache_hits=1`, `complete=1`,
  `members=1`, `indexed=1` y `errors=0`, ambos con exit 0. Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-platform-cohort/`.

## Corte foundation aceptado en rama de snapshot

- El candidato `c183afc088aaa78d82efcd0841085568d543416a` mueve
  `file_identity.py` y `processing_provenance.py` a `neocortex/foundation` y
  deja las fachadas legacy verificables. La aceptación canónica del corte
  quedó ligada al árbol `1ed6be61bcfdf7bd2c3239ae22e96f6d66c52b4d`, con
  baseline `ff58f0ed7d1e230d3d3a1bcaa7eff488c5cd1c68`, `status=passed`, 17
  barreras, 5800 pruebas recolectadas y Coverage trusted-deep de 24 shards.
- Receipt: `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/1ed6be61bcfdf7bd2c3239ae22e96f6d66c52b4d-59545ac64382e64d27ae18c0f115558d67607fc87eeff4ffea3b28ae2e334f40.json`,
  digest `sha256:53da4228853257451405e525ade378646eb053fe0044870e8bb695a3f7529358`.
- La release `0.9.0-1ed6be61bcfd-cp314-linux-x86_64` se instaló y verificó con
  `current`, manifiesto y launcher alineados. El E2E desde
  `/home/winterboss/.local/bin/Neocortex` procesó un ZIP sintético en la primera
  corrida (`processed=1`, `cache_hits=0`, `complete=1`, `members=1`,
  `indexed=1`, `errors=0`) y en el replay (`processed=1`, `cache_hits=1`,
  `complete=1`, `members=1`, `indexed=1`, `errors=0`), ambos con exit 0.
- El snapshot se publicó en `origin/codex/neocortex-local-20260829` con SHA
  `1ed6be61bcfdf7bd2c3239ae22e96f6d66c52b4d`; `origin/main` permanece en
  `d1adefc4cdafbd16a97e0f40bd349827fc74f98e`.
- Evidencia durable:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-foundation-cohort/summary.json`.

## Próximo corte, en orden

1. Mantener como evidencia vigente el receipt, la release y el E2E del corte
   foundation aceptado; la corrida interrumpida anterior no se usa como prueba.
2. El árbol canónico ya está organizado; mantenerlo congelado en `03b9fba` y
   ejecutar una sola `Neocortex code validate --baseline HEAD^` como aceptación
   integral, seguida de release Linux desde el SHA aceptado, instalación,
   launcher público y E2E/replay con contadores de caché verificables.
3. Conservar `main` sin merge ni push hasta que el receipt, la release y el E2E
   final estén comprobados; los snapshots locales y remotos no equivalen a la
   integración final.

## Límites

- Linux/Kubuntu es la única plataforma activa; Windows y GitHub Actions quedan
  fuera de alcance.
- Durante gates observa únicamente stream, transcript, proceso y systemd en el
  namespace real; no abras SQLite cercadas con lectores ordinarios.
- pip-audit sólo se renueva mediante su productor explícito autorizado, sin
  `--fix` ni otros proveedores remotos.
