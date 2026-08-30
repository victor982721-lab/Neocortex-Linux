# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-30 03:45 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

> Modo de trabajo vigente: desarrollo rápido por lotes materiales, con controles
> focales durante la organización y un único gate canónico al congelar la cohorte;
> ningún candidato provisional tiene aceptación hasta contar con receipt local.

## Objetivo activo

La implementación productiva ya está organizada en el namespace único
`neocortex`, con responsabilidades explícitas y límites de dependencia
comprobados, pero la migración física todavía no está cerrada: las raíces
numeradas conservan cientos de fachadas de compatibilidad. El siguiente tramo
debe inventariar sus consumidores y retirar esas fachadas por cohortes, sin
declarar final la reorganización mientras `_04_Nucleo_Operativo` siga siendo una
carpeta poblada de aliases.

## Corte canónico de implementación aceptado (no cierre físico)

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `5455b90992ef2e6f2b0d49590a99448f0342aeea`
- Árbol verificado limpio; la rama local conserva commits locales posteriores a
  su snapshot remoto y no se hizo merge ni push a `main`.
- Este corte acepta la implementación canónica y su comportamiento, no la
  eliminación física de la raíz legacy; el cierre solicitado por Víctor sigue
  pendiente hasta retirar o reducir explícitamente esa compatibilidad.
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

- `Neocortex code validate --baseline 1930aeabd73343468e119dea577c9466848dd075`
  sobre `5455b90` terminó `passed`: selección `full` con 354 selectores,
  17 barreras, 5,843 pruebas y Coverage trusted-deep 24/24; wheel candidato,
  replay y dos procesos de revisión pública quedaron estables. Digest del gate:
  `sha256:e560a0fabf37593e205bec93f26fc3a85b6c587dc7a0bd58e01eca84252d5a18`.
  Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/5455b90992ef2e6f2b0d49590a99448f0342aeea-e560a0fabf37593e205bec93f26fc3a85b6c587dc7a0bd58e01eca84252d5a18.json`
  (`sha256:38b13f6d89fecdb24a582cd2a83b279e4c3ad4c1e3ad3c4c1e99620a2b51299a`).
- La evidencia durable de la cohorte conserva el gate completo, la equivalencia
  docs-only, las métricas de cobertura/replay y la correspondencia de la release
  actualmente instalada:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-canonical-refactor/summary.json`.
- `release_linux.py verify` devolvió `verified=true` para el `current` activo;
  el manifest, el launcher y su `source_sha` se conservan en el resumen durable
  junto con el recibo de instalación.
- El launcher público `/home/winterboss/.local/bin/Neocortex`, sin
  `PYTHONPATH` ni `--apply`, procesó un fixture aislado en la primera corrida y
  reutilizó Archive, Text y Semantic en el replay; ambos exits fueron 0 y los
  contadores de caché quedan registrados en la evidencia durable.

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

1. Levantar un inventario read-only de imports, entry points, tests, packaging y
   consumers que aún atraviesan `_04_Nucleo_Operativo`, distinguiendo aliases
   necesarios de residuos retirables.
2. Retirar la compatibilidad en cohortes pequeñas, actualizar consumidores y
   añadir regresiones; no borrar la raíz ni sus archivos antes de comprobar cada
   frontera.
3. Congelar después el nuevo árbol, ejecutar el gate canónico, instalar la
   release y repetir el E2E/replay; `main` permanece sin merge ni push.

## Límites

- Linux/Kubuntu es la única plataforma activa; Windows y GitHub Actions quedan
  fuera de alcance.
- Durante gates observa únicamente stream, transcript, proceso y systemd en el
  namespace real; no abras SQLite cercadas con lectores ordinarios.
- pip-audit sólo se renueva mediante su productor explícito autorizado, sin
  `--fix` ni otros proveedores remotos.
