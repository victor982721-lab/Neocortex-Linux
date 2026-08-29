# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-29 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte Video aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `249c7b300ab3258ebbfc0ceafd9f5b30d7a26462`
- Árbol verificado limpio; `main` sigue sin publicar y está 55 commits delante
  de `origin/main`.
- Archive, DOCX, Audio, Image, Office, PDF y Video viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio,image,office,pdf,video}`. Las fachadas
  `_04_Nucleo_Operativo` correspondientes son compatibilidad explícita; las
  sondas runtime están en `neocortex/capabilities/runtime.py`.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  las siete familias hacia foundation/core, pendiente la cohorte Text y las
  cohortes de plataforma.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline bf9f729ab597228811703f9fc1ddb3c2ba32faa2`
  sobre `249c7b3` terminó `passed`: 17 barreras, selección afectada de 55
  pruebas, Coverage de 20 shards, experimentos, wheel, replay e identidades
  públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/249c7b300ab3258ebbfc0ceafd9f5b30d7a26462-30a5d08c404885eab364fc0bb8058d15d385cd58eda1c88881b1559298e6f73c.json`
  (`sha256:d3458c9d469df79b24c3f95b06b6e742984c2311f6288213311573f2a32f0656`).
- La release vigente se reconstruyó desde el SHA ejecutable aceptado:
  `0.9.0-249c7b300ab3-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: un MP4 sintético
  produjo `processed=1`, `frames_sampled=1`, `visual_only=1`, `partial=1` y
  `errors=0`; el replay obtuvo `cache_hits=1`, ambos con exit 0. La condición
  parcial es esperada para la muestra sin audio. Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-video-cohort/`.

## Próximo corte, en orden

1. Migrar Text físicamente a `neocortex/capabilities/formats/text`, con el
   extractor, worker Office heredado, contratos, fachadas y consumidores
   separados de Video.
2. Mantener el lote material, ejecutar pruebas focales, congelar un commit,
   renovar trusted-static con la consulta pip-audit autorizada, ejecutar una sola
   aceptación canónica, instalar desde el SHA y repetir el E2E instalado.
3. No iniciar otra vertical mientras falte el receipt, release o E2E del corte;
   no hacer push hasta el cierre integral de `NEO-CORE-004`.

## Límites

- Linux/Kubuntu es la única plataforma activa; Windows y GitHub Actions quedan
  fuera de alcance.
- Durante gates observa únicamente stream, transcript, proceso y systemd en el
  namespace real; no abras SQLite cercadas con lectores ordinarios.
- pip-audit sólo se renueva mediante su productor explícito autorizado, sin
  `--fix` ni otros proveedores remotos.
