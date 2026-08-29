# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-28 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte DOCX aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `a8083e5fcb740c1d632a7b02d7a8d7e692dee594`
- SHA final docs-only: `6725cdf077ee215abbbb7ab73cc63629cd9d05fa`; sólo actualiza este
  handoff respecto del árbol ejecutable aceptado.
- Árbol verificado limpio; `main` sigue sin publicar y está 40 commits delante
  de `origin/main`.
- Archive y DOCX viven físicamente en
  `neocortex/capabilities/formats/{archive,docx}`. Las fachadas
  `_04_Nucleo_Operativo` correspondientes son compatibilidad explícita; las
  sondas runtime están en `neocortex/capabilities/runtime.py`.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  ambas familias hacia foundation/core, pendientes de las cohortes de plataforma.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline 40aad1fd9e1797d5e94d136b1bbf842137e90a7c`
  sobre `a8083e5` terminó `passed`: 17 barreras, 335 pruebas seleccionadas,
  Coverage 24/24, experimentos, wheel, replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/a8083e5fcb740c1d632a7b02d7a8d7e692dee594-08c4afb88f4b969fb4caeb8d4d26c10a7119bf0a2f5d7b5e85cb645d6ecef61a.json`
  (`sha256:257d9868c5919b7164b7ba6f2456c8c20d78e6624e687d7a5f6b713645e5752e`).
- La release vigente se reconstruyó desde el SHA final docs-only:
  `0.9.0-6725cdf077ee-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: una muestra DOCX real
  procesó un documento (`processed=1`, `new_documents=1`) y el replay reutilizó
  su resultado (`cache_hits=1`, `new_documents=0`), ambos con exit 0. Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-28-neocortex-docx-cohort/`.

## Próximo corte, en orden

1. Migrar Audio físicamente a `neocortex/capabilities/formats/audio`, con
   workers, contratos, fachadas y consumidores separados de DOCX.
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
