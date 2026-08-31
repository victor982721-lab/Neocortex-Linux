# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-31 04:41 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

> Modo de trabajo vigente: cierre de producto por lote material, validaciones
> individuales proporcionales y una sola release al final. No existe un quality
> gate agregador ni un receipt de autoanálisis como requisito del producto.

## Objetivo activo

La simplificación física de `neocortex/code` quedó reconciliada sin wrappers:
conserva ingesta, detección, representación, persistencia, búsqueda y
relaciones semánticas. También se redujo la supervisión de workers a sesiones y
grupos POSIX con `RLIMIT_AS`, retirando la capa activa de Job Objects Windows.
La mutación Linux sigue bloqueada por contrato: `--apply` y
`--organization-apply` deben rechazarse antes de crear estado con
`linux_mutation_backend_unavailable`; el prototipo POSIX/KIO auditado no se
promueve ni se incluye en el runtime.

El checkout final es `fe0fafae0dee79bcd9f7b61efd0ca02576eacce9` y la release activa
`0.9.0-fe0fafae0dee-cp314-linux-x86_64` identifica ese mismo SHA; `release_linux.py
verify` pasó. La suite completa quedó en 4,307 pruebas, 127 omitidas y 114
subtests, y la evidencia durable está en
`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-31-neocortex-linux-only-simplification/summary.json`.

Las proyecciones Semantic mutables tienen un scrub explícito y probado para
retirar claves adultas sin alterar el resto del JSON. No se detectó una base
Semantic viva bajo el estado local durante esta sesión, por lo que no se ejecutó
una migración destructiva sobre datos del usuario.

## Corte físico anterior (referencia histórica)

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

## Evidencia histórica de aceptación y release (no normativa)

Los siguientes receipts, gates y releases describen cortes anteriores. Se
conservan para trazabilidad, pero no son requisitos ni instrucciones para el
trabajo vigente y no deben reactivar el autoanálisis retirado.

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

## Corte foundation aceptado en rama de snapshot (referencia histórica)

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

1. Mantener el runtime Linux en solo lectura para mutación del corpus mientras
   rija `linux_mutation_backend_unavailable`.
2. Si Víctor lo solicita de nuevo, auditar de forma separada la retirada
   preservativa de adaptadores Windows/NTFS históricos, con alcance y evidencia
   explícitos antes de borrar cualquier archivo.
3. No iniciar una corrida real del corpus ni crear `curate --apply` bajo la
   política actual.

## Límites

- Linux/Kubuntu es la única plataforma activa; Windows y GitHub Actions quedan
  fuera de alcance.
- Las herramientas de desarrollo se ejecutan directamente, de forma focal y
  proporcional; no se mantiene ni se crea un agregador de calidad.
- No se ejecuta `pip-audit` ni ningún proveedor remoto de forma implícita.
- Las bases históricas sólo se migran tras backup verificable y sobre una copia
  aislada durante las pruebas; no se modifica el corpus real en el smoke.
