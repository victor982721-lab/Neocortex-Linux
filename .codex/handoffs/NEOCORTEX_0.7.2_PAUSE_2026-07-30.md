# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-29 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte PDF aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `da6679221e3a2803664d382779d95714ac0231b4`
- Árbol verificado limpio; `main` sigue sin publicar y está 53 commits delante
  de `origin/main`.
- Archive, DOCX, Audio, Image, Office y PDF viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio,image,office,pdf}`. Las fachadas
  `_04_Nucleo_Operativo` correspondientes son compatibilidad explícita; las
  sondas runtime están en `neocortex/capabilities/runtime.py`.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  las seis familias hacia foundation/core, pendientes de la cohorte Video y
  de las cohortes de plataforma.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline 70770bbd79746a3f8b87e91a25a2b2178f483c6e`
  sobre `da66792` terminó `passed`: 17 barreras, 336 pruebas seleccionadas,
  Coverage 24/24, experimentos, wheel, replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/da6679221e3a2803664d382779d95714ac0231b4-fd2724eb342c41127bc01b386794a9fca01f0173347221cc6edcc74afaa5ce56.json`
  (`sha256:ccb87ef30bfc7733112289c18f7fc57f72412002fc0b3e7a821343522c5b8925`).
- La release vigente se reconstruyó desde el SHA ejecutable aceptado:
  `0.9.0-da6679221e3a-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: un PDF sintético
  procesó un documento (`processed=1`, `new_documents=1`, `extracted=1`,
  `errors=0`) y el replay reutilizó su resultado (`processed=1`, `cache_hits=1`,
  `new_documents=0`, `extracted=0`, `errors=0`), ambos con exit 0. Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-pdf-cohort/`.

## Próximo corte, en orden

1. Migrar Video físicamente a `neocortex/capabilities/formats/video`, con
   frames, probe, OCR acotado, estado, fachadas y consumidores separados de
   PDF.
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
