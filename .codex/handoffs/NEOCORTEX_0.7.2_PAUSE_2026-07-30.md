# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-30 14:25 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

> Modo de trabajo vigente: desarrollo rápido por lotes materiales, con controles
> focales durante la organización y un único gate canónico al congelar la cohorte;
> ningún candidato provisional tiene aceptación hasta contar con receipt local.

## Objetivo activo

La migración física del producto ya quedó aplicada en el checkout candidato:
`neocortex` es la única raíz productiva, la carpeta `_04_Nucleo_Operativo` ya no
existe, el paquete raíz sólo conserva metadatos y `__main__.py`, y los módulos de
API, plataforma, persistencia, capacidades y tooling viven bajo sus propietarios
canónicos. No se conservaron alias de compatibilidad ni implementaciones
duplicadas en las rutas retiradas. El cierre técnico todavía requiere ejecutar
el gate canónico, construir la release Linux y demostrar el recorrido público
instalado.

## Corte canónico de implementación en verificación final

- Checkout: `/home/winterboss/Neocortex/Repository`
- El árbol candidato quedó congelado en `04757c37f86dc1bfcc54abadc58be1a59633f037`;
  no se reutiliza ningún receipt anterior como aceptación de esta migración.
- La verificación viva observó 452 módulos Python canónicos, sin referencias de
  código a `_04_Nucleo_Operativo` y sin la carpeta legacy en el checkout.
- El fingerprint del registro exhaustivo actual es
  `core-architecture-target-v1:sha256:2b1ad2c3a2b01a76477c69c7205979eb06537fb6c228765f88caf925b2cadbc2`.
- La suite integral del árbol ejecutable inmediatamente anterior (`144055a…`)
  pasó con 5,520 pruebas, 144 omitidas, 110 subtests y una advertencia no
  bloqueante; después de retirar el entrypoint obsoleto, las focales y la
  recolección pasaron, pero la suite integral y el gate canónico aún deben
  ejecutarse sobre este SHA.
- Archive, DOCX, Audio, Image, Office, PDF, Video y Text viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio,image,office,pdf,text,video}`.
  Las rutas de formato y sus consumidores viven únicamente en esos módulos;
  las sondas runtime están en `neocortex/capabilities/runtime.py`.
- La cohorte compartida de plataforma vive físicamente en
  `neocortex/platform/{architecture_projection,capability_registry,capability_registry_specs,content_types,zip_safety}.py`.
- El registro de arquitectura conserva explícitos los cruces permitidos de las
  familias hacia foundation/core, sin una matriz de aliases legacy.

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

1. Resolver la renovación del snapshot pip-audit y ejecutar una sola validación
  canónica sobre `04757c37…`, incluyendo la suite y los controles arquitectónicos
  sin abrir SQLite cercadas.
2. Instalar desde el SHA validado, verificar launcher/manifiesto y repetir el
   E2E con replay; sólo entonces queda listo el cierre Git final.

## Límites

- Linux/Kubuntu es la única plataforma activa; Windows y GitHub Actions quedan
  fuera de alcance.
- Durante gates observa únicamente stream, transcript, proceso y systemd en el
  namespace real; no abras SQLite cercadas con lectores ordinarios.
- pip-audit sólo se renueva mediante su productor explícito autorizado, sin
  `--fix` ni otros proveedores remotos.
