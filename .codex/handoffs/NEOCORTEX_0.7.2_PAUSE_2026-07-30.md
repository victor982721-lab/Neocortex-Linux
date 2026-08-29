# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-28 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte Audio aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `8b5191b4bb8bdd4484b810acc37904667614258f`
- Árbol verificado limpio; `main` sigue sin publicar y está 43 commits delante
  de `origin/main`.
- Archive, DOCX y Audio viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio}`. Las fachadas
  `_04_Nucleo_Operativo` correspondientes son compatibilidad explícita; las
  sondas runtime están en `neocortex/capabilities/runtime.py`.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  las tres familias hacia foundation/core, pendientes de las cohortes de plataforma.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline 77e432f23202c9e8fd048123755f8dad16c36760`
  sobre `8b5191b` terminó `passed`: 17 barreras, 335 pruebas seleccionadas,
  Coverage 24/24, experimentos, wheel, replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/8b5191b4bb8bdd4484b810acc37904667614258f-0f0eb93677950c2f6e9c1b851654bfb2cd72ccbe0294754672863d0dd133cfd2.json`
  (`sha256:6bac865288b9399f97e7568dd2c0cf78c2e648307f80d4aa890644f57dd673fc`).
- La release vigente se reconstruyó desde el SHA ejecutable aceptado:
  `0.9.0-8b5191b4bb8b-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: un WAV sintético
  procesó un archivo (`processed=1`, `cache_hits=0`, `errors=0`) y el replay
  reutilizó su resultado (`processed=1`, `cache_hits=1`, `errors=0`), ambos con
  exit 0. Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-28-neocortex-audio-cohort/`.

## Próximo corte, en orden

1. Migrar Image físicamente a `neocortex/capabilities/formats/image`, con OCR,
   clasificación, contratos, fachadas y consumidores separados de Audio.
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
