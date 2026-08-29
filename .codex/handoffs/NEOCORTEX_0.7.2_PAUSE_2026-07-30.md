# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-29 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte Office aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `fcad813008e4369c1e03961b06f4075cc0aecdd1`
- Árbol verificado limpio; `main` sigue sin publicar y está 49 commits delante
  de `origin/main`.
- Archive, DOCX, Audio, Image y Office viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio,image,office}`. Las fachadas
  `_04_Nucleo_Operativo` correspondientes son compatibilidad explícita; las
  sondas runtime están en `neocortex/capabilities/runtime.py`.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  las cinco familias hacia foundation/core, pendientes de las cohortes de plataforma.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline d0cd0d919e6b553ba8d13a54284d64c5d0666a65`
  sobre `fcad813` terminó `passed`: 17 barreras, 335 pruebas seleccionadas,
  Coverage 24/24, experimentos, wheel, replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/fcad813008e4369c1e03961b06f4075cc0aecdd1-81f4217e542abf3a01ce0705c1c4873f9d447e055c25e6b5fe87aa24bdd3d594.json`
  (`sha256:c63923bc68b72689f4e859d9b8ea14b9909f1cee76b40e5282f12a7d4f90486b`).
- La release vigente se reconstruyó desde el SHA ejecutable aceptado:
  `0.9.0-fcad813008e4-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: un XLSX sintético
  procesó un documento (`processed=1`, `extracted=1`, `errors=0`) y el replay
  reutilizó su resultado (`processed=1`, `cache_hits=1`, `extracted=0`,
  `errors=0`), ambos con exit 0. Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-office-cohort/`.

## Próximo corte, en orden

1. Migrar PDF físicamente a `neocortex/capabilities/formats/pdf`, con sus
   rutas de administración, derivación, OCR/aislamiento, fachadas y
   consumidores separados de Office.
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
