# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-29 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte Image aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `cf431f8bd01fcc97e1bbbaf14db094a8bf9e8c2b`
- Árbol verificado limpio; `main` sigue sin publicar y está 46 commits delante
  de `origin/main`.
- Archive, DOCX, Audio e Image viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio,image}`. Las fachadas
  `_04_Nucleo_Operativo` correspondientes son compatibilidad explícita; las
  sondas runtime están en `neocortex/capabilities/runtime.py`.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  las cuatro familias hacia foundation/core, pendientes de las cohortes de plataforma.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline 49498351f88d489cae13d476747e1e187096e0fa`
  sobre `cf431f8` terminó `passed`: 17 barreras, 335 pruebas seleccionadas,
  Coverage 24/24, experimentos, wheel, replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/cf431f8bd01fcc97e1bbbaf14db094a8bf9e8c2b-8869ef385e0f5cba461170faa8cee7cb77f917946c8f54134458d91270f57d4b.json`
  (`sha256:a7a9c2a3b070e9de6eff55549490dc272459fde8c867110aee7f4cc53634d52c`).
- La release vigente se reconstruyó desde el SHA ejecutable aceptado:
  `0.9.0-cf431f8bd01f-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: un PNG sintético
  procesó una imagen (`processed=1`, `cache_hits=0`, `new_images=1`, `errors=0`)
  y el replay reutilizó su resultado (`processed=1`, `cache_hits=1`,
  `new_images=0`, `errors=0`), ambos con exit 0. Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-image-cohort/`.

## Próximo corte, en orden

1. Migrar Office físicamente a `neocortex/capabilities/formats/office`, con
   extracción XLSX, contratos, fachadas y consumidores separados de Image.
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
